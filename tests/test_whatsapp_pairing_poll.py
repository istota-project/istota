"""The `whatsapp-pairing` gate body: what it costs, what it claims, what it hands off.

This gate runs on **every tick** of the scheduler's dispatch loop, on every
Baileys deployment, for ever. Almost every one of those ticks finds nothing to
do, so the properties worth asserting here are about *cost* and about the claim
rather than about a return value — an outcome-only test passes at any cost, and
the whole reason the gate is not `background` is that a thread per tick is about
17,000 a day to answer one indexed read.

The two negative controls the durable expiry needs fail on **different tests**,
and this file holds one of them. Collapsing `_expire_stale_pairing`'s two arms
into the single "any non-terminal row with no in-memory window behind it" form
turns `TestTheClaim::test_a_fresh_request_is_serviced` red — a fresh `requested`
row has no window behind it by construction, so that predicate expires the
request it exists to service, unconditionally and inside a single invocation —
while leaving the scheduler-restart test in `tests/test_whatsapp_pairing_db.py`
green. Removing the orphan arm is the other control and does the opposite.
"""

from __future__ import annotations

import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from istota import async_runtime, db
from istota.config import Config, UserConfig
from istota.scheduler import _tick_interval_gates, build_interval_gates
from istota.transport.whatsapp import baileys_bridge, pairing_relay
from istota.transport.whatsapp import baileys_runtime as runtime
from istota.transport.whatsapp.baileys_bridge import (
    PAIRING_OK,
    PAIRING_SIDECAR_ABSENT,
    PairingResult,
)

from .support.whatsapp_config import build_whatsapp_config

USER = "alice"
GATE = "whatsapp-pairing"


# ---------------------------------------------------------------------------
# Fixtures and doubles
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path) -> Path:
    path = tmp_path / "state" / "istota.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db.init_db(path)
    return path


@pytest.fixture
def config(tmp_path, db_path) -> Config:
    return Config(
        db_path=db_path,
        temp_dir=tmp_path / "tmp",
        whatsapp=build_whatsapp_config(enabled=True, provider="baileys"),
        users={USER: UserConfig()},
    )


class FakeBridge:
    """The two things the poll reads off a bridge, plus a scripted re-pair.

    Deliberately not a `MagicMock`: `pairing_window` and `last_pairing_outcome`
    are the pair the orphan arm discriminates on, and a mock answers both with a
    truthy object — which is the "double more permissive than the thing it
    stands for" failure this repo keeps finding.
    """

    def __init__(self, relay: Path, *, result: PairingResult | None = None):
        self.pairing_relay_path = relay
        self.pairing_window = None
        self.last_pairing_outcome = None
        self.result = result or PairingResult(True, PAIRING_OK, window_id="win-1")
        self.repairs: list[str] = []

    async def repair_session(self, requested_by: str, *, force: bool = False):
        self.repairs.append(requested_by)
        return self.result


@pytest.fixture
def relay_file(tmp_path) -> Path:
    return tmp_path / "state" / "whatsapp-pairing.json"


@pytest.fixture
def bridge(config, relay_file, monkeypatch) -> FakeBridge:
    instance = FakeBridge(relay_file)
    monkeypatch.setattr(baileys_bridge, "set_active_bridge", lambda b: None)
    monkeypatch.setattr(baileys_bridge, "active_bridge", lambda: instance)
    return instance


@pytest.fixture
def spawns(monkeypatch) -> list[str]:
    """Every `spawn_task` the poll made, with the coroutine closed unrun.

    Patched on `istota.async_runtime` — the poll imports the name at function
    scope, so this reaches it; a module-scope binding in the product would
    make the patch inert while these tests still passed.
    """
    recorded: list[str] = []

    def fake(coro, *, name: str):
        coro.close()
        recorded.append(name)
        return None

    monkeypatch.setattr(async_runtime, "spawn_task", fake)
    return recorded


def gate_row(config: Config):
    return {g.name: g for g in build_interval_gates(config)}[GATE]


def row(db_path: Path) -> dict | None:
    with db.get_db(db_path) as conn:
        return db.read_whatsapp_pairing(conn)


def request(db_path: Path, **kwargs) -> str:
    with db.get_db(db_path) as conn:
        value = db.request_whatsapp_pairing(conn, USER, **kwargs)
    assert value
    return value


# ---------------------------------------------------------------------------
# The gate row
# ---------------------------------------------------------------------------


class TestTheGateRow:
    def test_it_runs_on_every_tick_with_no_interval_of_its_own(self, config):
        gate = gate_row(config)
        assert gate.fixed_interval == 0
        assert gate.field is None

    def test_none_of_the_three_dispatch_flags_is_set(self, config):
        """Each absence has its own reason, stated at the row.

        `background` would create a `threading.Thread` per tick to perform one
        indexed read; `overlap_expected` demotes `_spawn_background_check`'s
        in-flight log and has nothing to demote once the SQL claim replaces
        that registry; and `one_shot` would have `istota run` — a process
        holding no bridge — run the poll, which is exactly where the orphan
        arm's "in a process that holds the bridge" qualifier stops holding.
        """
        gate = gate_row(config)
        assert gate.background is False
        assert gate.overlap_expected is False
        assert gate.one_shot is False

    @pytest.mark.parametrize(
        "enabled,provider,wanted",
        [
            (True, "baileys", True),
            (True, "whatsapp_cloud", False),
            (False, "baileys", False),
        ],
    )
    def test_enabled_is_the_bridge_predicate_and_nothing_else(
        self, tmp_path, enabled, provider, wanted
    ):
        """`baileys_bridge_wanted` alone. The CLI's attach mode writes the same
        request row and depends on this poll, so gating it on the web flow's own
        switch would silently disable `istota whatsapp pair` on the shape where
        it matters most."""
        cfg = Config(
            db_path=tmp_path / "istota.db",
            whatsapp=build_whatsapp_config(enabled=enabled, provider=provider),
        )
        gate = gate_row(cfg)
        assert gate.enabled(cfg) is wanted
        assert gate.enabled(cfg) is runtime.baileys_bridge_wanted(cfg)

    def test_a_backwards_clock_step_does_not_skip_a_tick(self, config, monkeypatch):
        """`fixed_interval=0` bypasses the clock rather than comparing against
        it. `now` is wall-clock, so a backwards NTP step leaves the stored clock
        ahead of it and `now - clock >= 0` would skip the gate for the length of
        the step — switching a recovery surface off by a clock adjustment.
        `backup-stale-alert` carries the same property for the same reason.

        Negative control: `fixed_interval=1` turns this red, the second tick
        never running.
        """
        calls: list[float] = []
        monkeypatch.setattr(
            runtime, "poll_pairing_request", lambda cfg: calls.append(0.0)
        )
        gate = gate_row(config)
        clocks = {GATE: 0.0}
        _tick_interval_gates([gate], clocks, config, 10_000.0, {})
        _tick_interval_gates([gate], clocks, config, 5_000.0, {})
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# The cheap path
# ---------------------------------------------------------------------------


class TestTheCheapPath:
    def test_a_poll_with_nothing_pending_spawns_nothing_and_makes_no_thread(
        self, config, bridge, spawns, monkeypatch
    ):
        """Asserted over spawns and over threads, not over a return value: the
        gate returns `None` whatever it cost.

        `inflight` is the deterministic half — `_spawn_background_check` records
        its thread there before starting it, so the observation does not race
        the thread exiting. The control at the end of this test is what says the
        assertion can fail at all: the same gate with `background=True` puts an
        entry in that registry, which is the shape setting the flag on the
        product row would produce.
        """
        gate = gate_row(config)
        inflight: dict = {}
        before = threading.active_count()

        _tick_interval_gates([gate], {GATE: 0.0}, config, 1.0, inflight)

        assert spawns == []
        assert inflight == {}
        assert threading.active_count() == before
        assert row(config.db_path) is None

        backgrounded = replace(gate, background=True)
        _tick_interval_gates([backgrounded], {GATE: 0.0}, config, 2.0, inflight)
        assert GATE in inflight
        inflight[GATE].join(timeout=5)

    def test_a_closed_row_costs_the_same_as_no_row(self, config, bridge, spawns):
        """A deployment that paired last month has a terminal row sitting there
        for ever, so the terminal case is the steady state rather than an edge.
        """
        request_id = request(config.db_path)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, request_id, db.WHATSAPP_PAIRING_PAIRED, "done"
            )
        runtime.poll_pairing_request(config)
        assert spawns == []
        assert row(config.db_path)["state"] == db.WHATSAPP_PAIRING_PAIRED

    def test_the_poll_never_raises(self, config, bridge, spawns, monkeypatch):
        """It runs on the dispatch thread, where a raise takes the loop."""
        monkeypatch.setattr(
            runtime, "_poll_pairing_request",
            lambda cfg: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        runtime.poll_pairing_request(config)


# ---------------------------------------------------------------------------
# The claim
# ---------------------------------------------------------------------------


class TestTheClaim:
    def test_a_fresh_request_is_serviced(self, config, bridge, spawns):
        """The feature, in one assertion.

        **This is the test the predicate-collapse control has to turn red.**
        `request_whatsapp_pairing` writes the row `requested` and no window
        exists until the last step of `repair_session`, up to a sidecar-return
        plus a stop timeout later — so a poll running the single "no window
        behind it" predicate first stamps this row `expired` and then finds
        nothing pending to service. Pairing would never start once, and the
        scheduler-restart test would pass over a dead feature.
        """
        request(config.db_path)
        runtime.poll_pairing_request(config)
        assert spawns == [GATE]
        assert row(config.db_path)["state"] == db.WHATSAPP_PAIRING_SERVICING

    def test_the_claim_lands_before_the_hand_off(self, config, bridge, relay_file):
        """The claim is committed *before* `spawn_task`, so a second poll cannot
        find the row unclaimed. Observed by reading the row from inside the
        spawn, which is the only point between the two."""
        seen: list[str | None] = []

        def fake(coro, *, name: str):
            coro.close()
            state = row(config.db_path)
            seen.append(None if state is None else state["state"])
            return None

        request(config.db_path)
        original = async_runtime.spawn_task
        async_runtime.spawn_task = fake
        try:
            runtime.poll_pairing_request(config)
        finally:
            async_runtime.spawn_task = original
        assert seen == [db.WHATSAPP_PAIRING_SERVICING]

    def test_a_second_poll_spawns_nothing(self, config, bridge, spawns):
        """The property the claim exists for, and the one `spawn` does not
        provide: its registry is keyed by sequence, so it deduplicates nothing
        by name."""
        request(config.db_path)
        runtime.poll_pairing_request(config)
        runtime.poll_pairing_request(config)
        assert spawns == [GATE]

    def test_the_read_and_the_claim_are_one_transaction(
        self, config, bridge, monkeypatch
    ):
        """Two real polls, with the interleaving *forced* rather than hoped for.

        Negative control: replacing `BEGIN IMMEDIATE` with a deferred `BEGIN`
        turns this red with two spawns. **A plain two-thread barrier race is
        not that control** — measured, it caught the deferred `BEGIN` on one run
        in three, because each poll opens its own connection and does its
        unlocked cheap read first, so the two rarely overlap on their own. So
        one poll is paused between its in-transaction read and its claim, which
        is the only window that matters.

        With `BEGIN IMMEDIATE` the paused poll holds the write lock from the
        `BEGIN`, so the second cannot reach its read at all — it waits out
        `_POLL_BUSY_TIMEOUT_MS`, its `OperationalError` is swallowed by the
        never-raises wrapper, and it claims nothing. With a deferred `BEGIN` it
        reads `requested`, claims, and spawns a second time.
        """
        recorded: list[str] = []
        lock = threading.Lock()

        def fake(coro, *, name: str):
            coro.close()
            with lock:
                recorded.append(name)
            return None

        monkeypatch.setattr(async_runtime, "spawn_task", fake)
        request(config.db_path)

        paused = threading.Thread(target=lambda: None)
        read_reached = threading.Event()
        release = threading.Event()
        original_read = db.read_whatsapp_pairing
        seen = {"count": 0}

        def hooked(conn):
            result = original_read(conn)
            if threading.current_thread() is paused:
                seen["count"] += 1
                # The second call is the authoritative read, inside the
                # transaction; the first is the unlocked cheap one.
                if seen["count"] == 2:
                    read_reached.set()
                    release.wait(timeout=10)
            return result

        monkeypatch.setattr(db, "read_whatsapp_pairing", hooked)

        errors: list[BaseException] = []

        def poll() -> None:
            try:
                runtime._poll_pairing_request(config)
            except BaseException as exc:  # noqa: BLE001 — recorded, not raised
                errors.append(exc)

        paused = threading.Thread(target=poll)
        paused.start()
        assert read_reached.wait(timeout=10)
        runtime.poll_pairing_request(config)
        release.set()
        paused.join(timeout=30)

        assert errors == []
        assert recorded == [GATE], recorded
        assert row(config.db_path)["state"] == db.WHATSAPP_PAIRING_SERVICING

    def test_a_refused_spawn_reverts_the_claim(self, config, bridge, monkeypatch):
        """`spawn` raises `RuntimeError` when the runtime is stopping or was
        never started, and a shutdown landing between the claim and the spawn
        must not strand the row in `servicing` until its deadline."""

        def fake(coro, *, name: str):
            coro.close()
            raise RuntimeError("AsyncRuntime is stopping")

        monkeypatch.setattr(async_runtime, "spawn_task", fake)
        request(config.db_path)
        runtime.poll_pairing_request(config)
        assert row(config.db_path)["state"] == db.WHATSAPP_PAIRING_REQUESTED

    def test_no_bridge_means_no_claim(self, config, spawns, monkeypatch):
        """A scheduler whose bridge failed to start must leave the row
        serviceable rather than claim it and strand it until the deadline."""
        monkeypatch.setattr(baileys_bridge, "active_bridge", lambda: None)
        request(config.db_path)
        runtime.poll_pairing_request(config)
        assert spawns == []
        assert row(config.db_path)["state"] == db.WHATSAPP_PAIRING_REQUESTED


# ---------------------------------------------------------------------------
# The deadline arm
# ---------------------------------------------------------------------------


class TestTheDeadlineArm:
    def test_a_servicing_row_past_its_deadline_is_expired(
        self, config, bridge, spawns
    ):
        """The process-died-mid-service path. `servicing` implies no window was
        opened, so the **deadline** arm is what closes it rather than the orphan
        arm — slower and correct, and the assertion names the state so a later
        change routing it to the orphan arm is a visible decision."""
        request_id = request(config.db_path, window_seconds=-1)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, request_id, db.WHATSAPP_PAIRING_SERVICING
            )
        runtime.poll_pairing_request(config)
        assert row(config.db_path)["state"] == db.WHATSAPP_PAIRING_EXPIRED
        assert spawns == []

    def test_a_requested_row_nobody_picked_up_is_expired(
        self, config, spawns, monkeypatch
    ):
        """The scheduler was down. Closed with no bridge in the process at all,
        which is the shape that reaches this: the deadline arm is durable
        housekeeping and needs nothing in memory."""
        monkeypatch.setattr(baileys_bridge, "active_bridge", lambda: None)
        request(config.db_path, window_seconds=-1)
        runtime.poll_pairing_request(config)
        state = row(config.db_path)
        assert state["state"] == db.WHATSAPP_PAIRING_EXPIRED
        assert "scheduler" in state["message"]
        assert spawns == []

    def test_an_expired_row_lets_the_next_request_through(self, config, bridge):
        request(config.db_path, window_seconds=-1)
        runtime.poll_pairing_request(config)
        with db.get_db(config.db_path) as conn:
            assert db.request_whatsapp_pairing(conn, USER) is not None


# ---------------------------------------------------------------------------
# The window mirror
# ---------------------------------------------------------------------------


class TestTheWindowMirror:
    def test_a_live_windows_state_reaches_the_row(self, config, bridge, spawns):
        """The bridge writes the relay file and no database row, so without the
        mirror the durable row would sit at `awaiting_sidecar` for the whole
        window and two of the orphan arm's three states would be unreachable.
        """
        request_id = request(config.db_path, window_seconds=3600)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, request_id,
                db.WHATSAPP_PAIRING_AWAITING_SIDECAR,
                adopt_window_id="win-live",
            )
        bridge.pairing_window = type(
            "Window", (), {
                "window_id": "win-live",
                "state": pairing_relay.STATE_AWAITING_SCAN,
                "message": "",
            },
        )()

        runtime.poll_pairing_request(config)

        assert row(config.db_path)["state"] == db.WHATSAPP_PAIRING_AWAITING_SCAN
        assert spawns == []


# ---------------------------------------------------------------------------
# The write-back
# ---------------------------------------------------------------------------


class TestTheOutcomeWriteBack:
    async def test_an_opened_window_adopts_its_id_and_deadline(
        self, config, relay_file
    ):
        request_id = request(config.db_path, window_seconds=60)
        before = row(config.db_path)["expires_at"]
        instance = FakeBridge(
            relay_file,
            result=PairingResult(
                True, PAIRING_OK, window_id="win-9",
                message="a pairing window is open",
                moved_to=Path("/srv/session.old-20260101"),
            ),
        )
        # The bridge's own window deadline, which is what the row adopts — the
        # request-time one would otherwise leave the sequence's own wait eating
        # into the time somebody has to scan.
        instance.pairing_window = type(
            "Window", (), {
                "window_id": "win-9",
                "expires_at_wall": time.time() + 4000.0,
            },
        )()

        await runtime._service_pairing_request(config, instance, request_id, USER)

        state = row(config.db_path)
        assert state["state"] == db.WHATSAPP_PAIRING_AWAITING_SIDECAR
        assert state["window_id"] == "win-9"
        assert state["expires_at"] > before
        # The archive is written into the durable message while it is known: a
        # later process has no `PairingResult` and no way to tell which
        # `.old-<timestamp>` sibling this attempt made.
        assert "/srv/session.old-20260101" in state["message"]

    @pytest.mark.parametrize(
        "reason", [PAIRING_SIDECAR_ABSENT, "session_live", "cooldown"],
    )
    async def test_a_refusal_lands_in_a_terminal_state_never_a_window_one(
        self, config, relay_file, monkeypatch, reason
    ):
        """`sidecar_absent` is a refusal reason *and* a window state, and
        writing the reason into the row would have the orphan arm fire on a row
        with no window behind it. Every refusal is `failed`, carrying its own
        prose."""
        monkeypatch.setattr(runtime, "_announce_pairing", lambda cfg, outcome: None)
        request_id = request(config.db_path)
        instance = FakeBridge(
            relay_file,
            result=PairingResult(
                False, reason, message="nothing is bringing the sidecar back",
            ),
        )

        await runtime._service_pairing_request(config, instance, request_id, USER)

        state = row(config.db_path)
        assert state["state"] == db.WHATSAPP_PAIRING_FAILED
        assert state["state"] not in db.WHATSAPP_PAIRING_WINDOW_STATES
        assert "sidecar" in state["message"]

    async def test_a_failed_attempt_names_the_archive_it_left_behind(
        self, config, relay_file, monkeypatch
    ):
        monkeypatch.setattr(runtime, "_announce_pairing", lambda cfg, outcome: None)
        request_id = request(config.db_path)
        instance = FakeBridge(
            relay_file,
            result=PairingResult(
                False, "reset_incomplete", message="the reset did not finish.",
                moved_to=Path("/srv/session.old-20260202"),
            ),
        )

        await runtime._service_pairing_request(config, instance, request_id, USER)

        assert "/srv/session.old-20260202" in row(config.db_path)["message"]

    async def test_an_opened_window_announces_nothing_yet(
        self, config, relay_file, monkeypatch
    ):
        """A window that opened is not an outcome. The announcement belongs to
        the close, which the poll's arms record."""
        announced: list = []
        monkeypatch.setattr(
            runtime, "_announce_pairing", lambda cfg, outcome: announced.append(outcome)
        )
        request_id = request(config.db_path)
        instance = FakeBridge(relay_file)

        await runtime._service_pairing_request(config, instance, request_id, USER)

        assert announced == []

    async def test_a_failure_announces_once(self, config, relay_file, monkeypatch):
        announced: list = []
        monkeypatch.setattr(
            runtime, "_announce_pairing", lambda cfg, outcome: announced.append(outcome)
        )
        request_id = request(config.db_path)
        instance = FakeBridge(
            relay_file, result=PairingResult(False, PAIRING_SIDECAR_ABSENT),
        )

        await runtime._service_pairing_request(config, instance, request_id, USER)

        assert len(announced) == 1
        assert announced[0].state == db.WHATSAPP_PAIRING_FAILED

    async def test_the_service_task_never_raises_out(
        self, config, relay_file, monkeypatch
    ):
        """It runs as a bare task on the runtime loop. A raise there is an
        unretrieved-exception warning and a row left in `servicing`."""
        monkeypatch.setattr(
            runtime, "_write_pairing_outcome",
            lambda *a: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        instance = FakeBridge(relay_file)
        await runtime._service_pairing_request(
            config, instance, request(config.db_path), USER,
        )

    async def test_the_repair_is_not_forced(self, config, relay_file):
        """The durable row has no column for `force`, and deriving one from the
        bridge's live state would disconnect a working session whenever a
        request written while a fatal was latched is serviced after the session
        recovered. The unforced default is the safe one."""
        calls: list[bool] = []

        class Recorder(FakeBridge):
            async def repair_session(self, requested_by, *, force=False):
                calls.append(force)
                return self.result

        await runtime._service_pairing_request(
            config, Recorder(relay_file), request(config.db_path), USER,
        )
        assert calls == [False]


# ---------------------------------------------------------------------------
# The announcement
# ---------------------------------------------------------------------------


class TestTheAnnouncement:
    def test_every_terminal_state_has_a_title_and_a_severity(self):
        """The two tables are keyed by literal, because `db` is imported at
        function scope everywhere in that module. A state renamed on one side
        would otherwise fall through to the generic title with nothing saying
        so."""
        assert set(runtime._PAIRING_ALERT_TITLES) == (
            db.WHATSAPP_PAIRING_TERMINAL_STATES
        )
        assert set(runtime._PAIRING_ALERT_SEVERITY) == (
            db.WHATSAPP_PAIRING_TERMINAL_STATES
        )

    def test_a_terminal_closure_raises_a_row_for_each_admin(
        self, config, monkeypatch
    ):
        pushed: list = []
        monkeypatch.setattr("istota.config.load_admin_users", lambda: set())
        monkeypatch.setattr(
            runtime, "_push_baileys_alert", lambda cfg, item: pushed.append(item)
        )
        outcome = runtime._PairingClosure(
            window_id="win-4", state=db.WHATSAPP_PAIRING_PAIRED,
            message="paired", requested_by=USER, unlink_relay=True,
        )

        runtime._announce_pairing(config, outcome)

        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT user_id, source, dedup_key, severity FROM notifications"
            ).fetchall()
        assert [r["user_id"] for r in rows] == [USER]
        # The key carries the window id: `task_alert` is fire-and-forget and an
        # upsert onto an open row *bumps* rather than delivers, so a fixed key
        # would have the second pairing outcome of a deployment's life reach
        # nobody.
        assert rows[0]["dedup_key"] == "whatsapp:baileys-pairing:win-4"
        assert rows[0]["severity"] == "success"
        assert len(pushed) == 1

    def test_two_outcomes_raise_two_rows(self, config, monkeypatch):
        monkeypatch.setattr("istota.config.load_admin_users", lambda: set())
        monkeypatch.setattr(runtime, "_push_baileys_alert", lambda cfg, item: None)
        for window_id in ("win-a", "win-b"):
            runtime._announce_pairing(
                config,
                runtime._PairingClosure(
                    window_id=window_id, state=db.WHATSAPP_PAIRING_FAILED,
                    message="it failed", requested_by=USER, unlink_relay=True,
                ),
            )
        with db.get_db(config.db_path) as conn:
            keys = [
                r["dedup_key"]
                for r in conn.execute("SELECT dedup_key FROM notifications")
            ]
        assert sorted(keys) == [
            "whatsapp:baileys-pairing:win-a",
            "whatsapp:baileys-pairing:win-b",
        ]

    def test_it_never_raises(self, config, monkeypatch):
        monkeypatch.setattr("istota.config.load_admin_users", lambda: set())
        monkeypatch.setattr(
            runtime, "_write_pairing_alerts",
            lambda cfg, outcome: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        runtime._announce_pairing(
            config,
            runtime._PairingClosure(
                window_id="win-x", state=db.WHATSAPP_PAIRING_FAILED,
                message="", requested_by="", unlink_relay=False,
            ),
        )


def test_the_gate_body_reaches_the_poll(config, monkeypatch):
    """The row's `run` *is* the poll, asserted by driving the gate rather than
    by reading the table: a row whose body was wired to something else would
    pass every assertion above about its flags.

    The closure imports the poll by name at call time, so the patch on the
    defining module reaches it.
    """
    seen: list[Config] = []
    monkeypatch.setattr(
        runtime, "poll_pairing_request", lambda cfg: seen.append(cfg)
    )
    _tick_interval_gates([gate_row(config)], {GATE: 0.0}, config, 1.0, {})
    assert seen == [config]
