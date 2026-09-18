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

import asyncio
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
ANNOUNCE = "whatsapp-pairing-announce"
CANCEL = "whatsapp-pairing-cancel"
ADOPT = "whatsapp-pairing-adopt"


def _refuse_adopt(coro, *, name: str):
    """A `spawn_task` double whose runtime is stopping, for adoption alone.

    The orphan close is reachable by exactly one route since ISSUE-504 — a
    refused adopt spawn — so a test about the *message* that close writes has
    to drive it rather than assume the arm still fires on its own.
    """
    coro.close()
    if name == ADOPT:
        raise RuntimeError("runtime is stopping")
    return None


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
        self.cancelled: list[str | None] = []
        self.adoptions: list[tuple] = []
        self.relay_clears = 0

    async def repair_session(self, requested_by: str, *, force: bool = False):
        self.repairs.append(requested_by)
        return self.result

    def adopt_pairing_window(
        self, window_id: str, requested_by: str, expires_at_wall: float,
        *, destructive: bool = False, codes_relayed: bool = False,
    ):
        """Records at **call** time and hands back a coroutine to schedule.

        A plain method rather than an `async def`, deliberately: the `spawns`
        fixture closes every coroutine unrun, so an `async def` would record
        nothing and the arguments — the row's own id and deadline, which are
        the whole property — would be untestable without running a loop. The
        call site does not know the difference: it passes the result to
        `spawn_task` either way.
        """
        self.adoptions.append(
            (window_id, requested_by, expires_at_wall, destructive,
             codes_relayed)
        )
        return self._adopted()

    async def _adopted(self):
        return None

    async def cancel_pairing(self, *, window_id: str | None = None):
        # Records rather than accepting anything: the scoping is the property
        # a test here asserts, so a double that ignored `window_id` would make
        # the unscoped mistake invisible.
        self.cancelled.append(window_id)
        return True

    def clear_relay_file(self) -> bool:
        self.relay_clears += 1
        return True


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

    def test_the_web_flows_own_switch_does_not_gate_the_poll(self, tmp_path):
        """`pairing_enabled` gates the **routes** and nothing else.

        The CLI's attach mode writes the same request row and depends on this
        poll, so gating it here would silently disable `istota whatsapp pair`
        on the shape where it matters most — an operator who turned the web
        flow off is exactly the one who will be on a terminal.

        Its own case rather than a row in the table above, because that one
        leaves `pairing_enabled` at its default `True`, where the conjunction
        agrees with the predicate and the mistake is invisible. Negative
        control: `and c.whatsapp.baileys.pairing_enabled` on the gate turns
        this red and leaves the table green, which is what says the table
        could not have covered it.
        """
        cfg = Config(
            db_path=tmp_path / "istota.db",
            whatsapp=build_whatsapp_config(
                enabled=True, provider="baileys", pairing_enabled=False,
            ),
        )
        gate = gate_row(cfg)

        assert runtime.baileys_bridge_wanted(cfg) is True
        assert gate.enabled(cfg) is True

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
        # `inflight` is the load-bearing half — `_spawn_background_check`
        # records its thread there before starting it, so the observation does
        # not race the thread exiting. The count is bounded rather than
        # compared: the suite runs `-n auto` and a daemon thread another test
        # in this worker left winding down can exit between the two reads.
        assert threading.active_count() <= before
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

    def test_any_failed_spawn_reverts_the_claim(self, config, bridge, monkeypatch):
        """`RuntimeError` is the documented case and deliberately not the only
        one caught: anything out of the spawn leaves a row claimed with nothing
        running, which only the deadline arm would close.

        Negative control: narrowing the handler back to `except RuntimeError`
        turns this red, the row staying `servicing`.
        """

        def fake(coro, *, name: str):
            raise ValueError("something else")

        monkeypatch.setattr(async_runtime, "spawn_task", fake)
        request(config.db_path)
        runtime.poll_pairing_request(config)
        assert row(config.db_path)["state"] == db.WHATSAPP_PAIRING_REQUESTED

    def test_a_contended_lock_is_a_skipped_tick_not_a_fault(
        self, config, bridge, monkeypatch, caplog
    ):
        """`_POLL_BUSY_TIMEOUT_MS` turns a held write lock into an
        `OperationalError` rather than a 30s block, which is the point — but at
        `fixed_interval=0` reporting it as a WARNING with a traceback is one per
        tick for as long as the other writer holds its transaction. A lost tick
        costs nothing here: the next is a poll interval away."""
        import sqlite3 as _sqlite3

        def boom(cfg):
            raise _sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(runtime, "_poll_pairing_request", boom)
        with caplog.at_level("DEBUG"):
            runtime.poll_pairing_request(config)
        assert [r.levelname for r in caplog.records if "pairing" in r.getMessage()] == [
            "DEBUG"
        ]

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
        # No *service* spawn. The announce is a spawn of its own now, because
        # it opens a connection and sends per admin and must not do either on
        # the dispatch thread.
        assert GATE not in spawns
        assert spawns == [ANNOUNCE]

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
        assert GATE not in spawns

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

    def test_the_mirror_does_not_destroy_the_archive_path(
        self, config, bridge, spawns, monkeypatch
    ):
        """The reachable, ordinary sequence: a window opens and records where
        the old session went, the sidecar does not come back so the bridge
        demotes the window to `sidecar_absent` **with prose of its own**, the
        process then restarts and the orphan arm has to name the archive.

        Taking the live window's message on the mirror destroyed the only
        durable record of which `.old-<timestamp>` sibling this attempt made,
        and the operator is left with two session directories and no way to
        tell which holds theirs. Reviewer-found; my orphan-message test could
        not see it, because it hand-writes the row rather than mirroring.

        Negative control: `live.message or row["message"]` in
        `_mirror_pairing_window` turns this red on the archive assertion.
        """
        archive = "The previous session was moved aside to /srv/session.old-1."
        request_id = request(config.db_path, window_seconds=3600)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, request_id,
                db.WHATSAPP_PAIRING_AWAITING_SIDECAR,
                archive,
                adopt_window_id="win-live",
            )
        bridge.pairing_window = type(
            "Window", (), {
                "window_id": "win-live",
                "state": pairing_relay.STATE_SIDECAR_ABSENT,
                "message": "no sidecar has connected; check the unit",
            },
        )()

        runtime.poll_pairing_request(config)

        mirrored = row(config.db_path)
        assert mirrored["state"] == db.WHATSAPP_PAIRING_SIDECAR_ABSENT
        assert mirrored["message"] == archive

        # Now the restart. Since ISSUE-504 that is adopted rather than closed,
        # so the archive path has to survive one more hop: it is what
        # `_PairingAdoption` reads `destructive` off, and it is still what the
        # close carries when nothing can adopt. Driven through the one route
        # that still reaches that close — a runtime refusing the spawn.
        bridge.pairing_window = None
        monkeypatch.setattr(async_runtime, "spawn_task", _refuse_adopt)
        runtime.poll_pairing_request(config)

        closed = row(config.db_path)
        assert closed["state"] == db.WHATSAPP_PAIRING_FAILED
        assert "/srv/session.old-1" in closed["message"]

    def test_a_closed_row_names_a_remedy_that_works(
        self, config, bridge, monkeypatch
    ):
        """Not "request a re-pair again".

        After the restart, the session directory is the empty one the previous
        attempt created: the respawned sidecar sits in it in pairing mode, so
        nothing re-latches a permanent fatal, and `repair_session`'s
        confirmation gate then refuses the next request as `session_live` —
        the durable channel carries no `force`, so the unforced default is all
        the poll can ask for. The remedy has to name a surface that can
        confirm.
        """
        request_id = request(config.db_path, window_seconds=3600)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, request_id,
                db.WHATSAPP_PAIRING_AWAITING_SCAN,
                adopt_window_id="win-gone",
            )
        # Since ISSUE-504 this row is adopted, so the close is driven through
        # the one route that still reaches it.
        monkeypatch.setattr(async_runtime, "spawn_task", _refuse_adopt)

        runtime.poll_pairing_request(config)

        message = row(config.db_path)["message"]
        assert "Connections" in message
        assert "istota whatsapp pair" in message
        # The refusal the old wording walked into.
        assert "request a re-pair again" not in message.lower()


class TestTheClosureCleanup:
    """What a closed row leaves behind, and who is allowed to remove it.

    A pairing code is a full-account WhatsApp credential, so the assertion is
    always "is the file gone", and the mechanism matters because a relay this
    bridge may still be writing must be removed under its own lock.
    """

    def test_a_row_closing_under_a_live_window_cancels_it(
        self, config, bridge, spawns
    ):
        """The row's deadline is wall-clock and the window's is monotonic, and
        both are sized from the same TTL — so a row picked up late reaches its
        deadline while a window is live. Left alone, the next QR rotation
        republishes the relay file the closure just removed, and nothing closes
        the row a second time: a live code on disk for the rest of the TTL,
        after the operator was told the window expired.

        Asserted over the *cancel spawn*, because `cancel_pairing` is what
        drops the window, cancels the watchdog and unlinks under `_relay_lock`
        — all three, where a bare unlink does one.
        """
        request_id = request(config.db_path, window_seconds=-1)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, request_id,
                db.WHATSAPP_PAIRING_AWAITING_SCAN,
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

        # The `spawns` fixture closes the coroutine unrun, so the assertion is
        # that the cancel was *scheduled*; the sibling case below runs it and
        # asserts the scoping.
        assert CANCEL in spawns
        assert bridge.relay_clears == 0

    def test_the_cancel_is_scoped_to_the_window_it_meant(
        self, config, bridge, monkeypatch
    ):
        """The decision is taken from a read on the dispatch thread and the
        cancel runs on the runtime loop, so by then the window could have
        closed and a fresh one opened — an unscoped close would cancel a
        pairing somebody is mid-scan on."""
        request_id = request(config.db_path, window_seconds=-1)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, request_id,
                db.WHATSAPP_PAIRING_AWAITING_SCAN,
                adopt_window_id="win-live",
            )
        closure = runtime._PairingClosure(
            window_id="win-live", state=db.WHATSAPP_PAIRING_EXPIRED,
            message="", requested_by=USER, unlink_relay=True,
        )
        bridge.pairing_window = type(
            "Window", (), {"window_id": "win-live", "state": "", "message": ""},
        )()

        def run(coro, *, name: str):
            asyncio.run(coro)
            return None

        monkeypatch.setattr(async_runtime, "spawn_task", run)
        runtime._clean_up_after_closure(config, bridge, closure)

        assert bridge.cancelled == ["win-live"]

    def test_with_no_bridge_the_relay_is_cleared_by_configured_path(
        self, config, tmp_path, monkeypatch
    ):
        """A process whose bridge failed to start still runs the deadline arm,
        and the relay file there is a pairing code with no window and no
        durable row left to drive a later sweep. Cleared by the path the config
        resolves rather than skipped for want of a live object."""
        monkeypatch.setattr(baileys_bridge, "active_bridge", lambda: None)
        relay = baileys_bridge.default_pairing_relay_path(config)
        relay.parent.mkdir(parents=True, exist_ok=True)
        assert pairing_relay.write_relay(
            relay,
            pairing_relay.build_payload(
                window_id="win-orphan",
                state=pairing_relay.STATE_AWAITING_SCAN,
                expires_at=time.time() + 3600.0,
                qr="2@SENTINELqrPAYLOAD/9x+abcDEF==",
            ),
        )
        request_id = request(config.db_path, window_seconds=-1)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, request_id,
                db.WHATSAPP_PAIRING_AWAITING_SCAN,
                adopt_window_id="win-orphan",
            )

        runtime.poll_pairing_request(config)

        assert not relay.exists()

    def test_a_relay_is_cleared_through_the_bridges_lock(self, config, bridge):
        """`pairing_relay.clear_relay`'s own docstring names serialization
        behind the bridge's `_relay_lock` as what makes it safe beside a live
        writer, and this poll runs on a thread that lock never sees. The bridge
        exposes a lock-held unlink for exactly this caller."""
        request_id = request(config.db_path, window_seconds=-1)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, request_id,
                db.WHATSAPP_PAIRING_AWAITING_SCAN,
                adopt_window_id="win-gone",
            )

        runtime.poll_pairing_request(config)

        assert bridge.relay_clears == 1

    def test_a_non_terminal_outcome_is_coerced_to_failed(self, config, bridge):
        """The bridge's five close sites all pass a terminal state today, so the
        coercion is latent — and driving it is the difference between a latent
        defect and a live one.

        A non-terminal value would land the row non-terminal with no window
        behind it, and since every write also stamps `updated_at` the rowcount
        is 1 every time: the reconcile arm would return a fresh closure on every
        tick, one relay clear and one admin alert per poll interval until the
        deadline. Driven through `last_pairing_outcome`, since no product path
        reaches it.

        Negative control: dropping the coercion turns this red, the row landing
        `sidecar_absent`.
        """
        request_id = request(config.db_path, window_seconds=3600)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, request_id,
                db.WHATSAPP_PAIRING_AWAITING_SCAN,
                adopt_window_id="win-odd",
            )
        bridge.last_pairing_outcome = type(
            "Outcome", (), {
                "window_id": "win-odd",
                "state": pairing_relay.STATE_SIDECAR_ABSENT,
                "message": "not a terminal state",
            },
        )()

        runtime.poll_pairing_request(config)

        closed = row(config.db_path)
        assert closed["state"] == db.WHATSAPP_PAIRING_FAILED
        assert closed["state"] in db.WHATSAPP_PAIRING_TERMINAL_STATES

    def test_an_id_less_row_is_cleared_rather_than_left_stuck(
        self, config, bridge
    ):
        """Every write in the poll is guarded on `pairing_window_id` and SQL
        equality never matches NULL, while `request_whatsapp_pairing`'s own
        guard is on the *state* — so a non-terminal row with no id would refuse
        every later pairing request for the life of the deployment with nothing
        able to close it. `clear_whatsapp_pairing` carries no id guard and is
        the one thing that can reach it.

        No writer produces one today; Stage 4 adds routes that write this table
        directly, which is the moment a defensive note stops being enough.

        Negative control: dropping the id-less arm turns this red on the third
        assertion — the row survives and the request is refused.
        """
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO whatsapp_runtime (singleton, pairing_state, "
                "pairing_window_id, pairing_expires_at, updated_at) "
                "VALUES (1, ?, NULL, ?, ?)",
                (
                    db.WHATSAPP_PAIRING_AWAITING_SCAN,
                    db.sql_datetime_from_epoch(time.time() + 3600.0),
                    db.sql_datetime_now(),
                ),
            )

        runtime.poll_pairing_request(config)

        assert row(config.db_path) is None
        with db.get_db(config.db_path) as conn:
            assert db.request_whatsapp_pairing(conn, USER) is not None

    def test_a_pre_window_row_clears_no_relay(self, config, bridge):
        """A `requested` or `servicing` row never had a window, so it never had
        a relay file — and reaching for one would be this poll deleting a file
        a *different* window is using."""
        request(config.db_path, window_seconds=-1)
        runtime.poll_pairing_request(config)
        assert bridge.relay_clears == 0
        assert bridge.cancelled == []


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

    async def test_a_failure_carries_who_asked_for_it(
        self, config, relay_file, monkeypatch
    ):
        """The poll's own closures pass `requested_by` and the announcement
        appends "Requested by …" only when it is set, so leaving it empty here
        made the same notification read differently depending on which path
        closed the row."""
        announced: list = []
        monkeypatch.setattr(
            runtime, "_announce_pairing",
            lambda cfg, outcome: announced.append(outcome),
        )
        instance = FakeBridge(
            relay_file, result=PairingResult(False, PAIRING_SIDECAR_ABSENT),
        )
        await runtime._service_pairing_request(
            config, instance, request(config.db_path), USER,
        )
        assert announced[0].requested_by == USER

    async def test_a_window_opened_over_a_closed_row_is_reported(
        self, config, relay_file, caplog
    ):
        """The deadline arm can expire the row while `repair_session` runs — the
        case `record_whatsapp_pairing_state`'s terminal guard exists for — and
        the adopt write then does not apply. A live window with no durable row
        behind it is why that is said out loud: the next request dead-ends on
        `already_pairing` while the operator has been told this one expired."""
        request_id = request(config.db_path)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, request_id, db.WHATSAPP_PAIRING_EXPIRED, "gone",
            )
        instance = FakeBridge(
            relay_file, result=PairingResult(True, PAIRING_OK, window_id="win-x"),
        )

        with caplog.at_level("WARNING"):
            await runtime._service_pairing_request(
                config, instance, request_id, USER,
            )

        assert any(
            "window_untracked" in record.getMessage() for record in caplog.records
        )
        assert row(config.db_path)["state"] == db.WHATSAPP_PAIRING_EXPIRED

    async def test_a_cancelled_repair_records_the_interruption(
        self, config, relay_file
    ):
        """`repair_session` re-raises cancellation and `AsyncRuntime._shutdown`
        cancels pending tasks, so a daemon stopping mid-repair would leave the
        row `servicing` with nothing to close it but the deadline arm — minutes
        later, under a message about a bridge that never picked it up. The
        attempt may already have moved the credential aside, and the row is the
        only place that could say so."""

        class Cancelling(FakeBridge):
            async def repair_session(self, requested_by, *, force=False):
                raise asyncio.CancelledError()

        request_id = request(config.db_path)
        with pytest.raises(asyncio.CancelledError):
            await runtime._service_pairing_request(
                config, Cancelling(relay_file), request_id, USER,
            )

        closed = row(config.db_path)
        assert closed["state"] == db.WHATSAPP_PAIRING_FAILED
        assert "daemon stopped" in closed["message"]

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


# ---------------------------------------------------------------------------
# The forced path, carried on the row
# ---------------------------------------------------------------------------


class TestTheForcedPath:
    """`pairing_force` is the whole carrier of an operator's confirmation.

    The route collects `force` and `confirm_disconnect`; the *poll* is what
    calls `repair_session`, and it is not the process the operator spoke to.
    With nothing on the row to say a confirmation happened, the poll could only
    ever ask unforced — so a live-session re-pair is refused `session_live` and
    recorded `failed`, and the whole `force` half of the design is unreachable
    through the durable channel. Stage 3 deferred the column and this is where
    it lands.

    Deriving the answer here instead was considered and refused: a request
    written while a fatal was latched, serviced after the session recovered,
    would disconnect a working session nobody agreed to disconnect.
    """

    def _forced_calls(self, config, bridge, monkeypatch) -> list[bool]:
        seen: list[bool] = []

        async def repair(requested_by: str, *, force: bool = False):
            seen.append(force)
            return bridge.result

        monkeypatch.setattr(bridge, "repair_session", repair)

        def run(coro, *, name: str):
            asyncio.run(coro)
            return None

        monkeypatch.setattr(async_runtime, "spawn_task", run)
        runtime.poll_pairing_request(config)
        return seen

    def test_an_unforced_row_asks_unforced(self, config, bridge, monkeypatch):
        request(config.db_path)
        assert self._forced_calls(config, bridge, monkeypatch) == [False]

    def test_a_forced_row_asks_forced(self, config, bridge, monkeypatch):
        """Negative control: dropping `force=claim[2]` from the hand-off turns
        this red while its sibling above stays green, which is what says the
        column is read rather than defaulted."""
        request(config.db_path, force=True)
        assert self._forced_calls(config, bridge, monkeypatch) == [True]

    def test_a_legacy_row_with_no_column_value_asks_unforced(
        self, config, bridge, monkeypatch
    ):
        """An upgraded deployment reads NULL there. Three spellings of the same
        answer — NULL, 0 and False — and the gate must read them alike, in the
        safe direction."""
        request(config.db_path)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE whatsapp_runtime SET pairing_force = NULL WHERE singleton = 1"
            )
        assert row(config.db_path)["force"] is False
        assert self._forced_calls(config, bridge, monkeypatch) == [False]

    def test_clearing_the_request_clears_the_confirmation(self, config):
        """Otherwise a confirmation outlives the request it belonged to and the
        next window inherits permission nobody granted it."""
        request(config.db_path, force=True)
        with db.get_db(config.db_path) as conn:
            assert db.clear_whatsapp_pairing(conn)
            value = conn.execute(
                "SELECT pairing_force FROM whatsapp_runtime WHERE singleton = 1"
            ).fetchone()[0]
        assert value is None

    def test_a_fresh_unforced_request_over_a_forced_one_is_unforced(self, config):
        """The upsert's conflict branch writes the column rather than leaving
        it, so a forced row that closed cannot lend its confirmation to the
        next request."""
        first = request(config.db_path, force=True)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, first, db.WHATSAPP_PAIRING_PAIRED, "paired",
            )
        request(config.db_path)
        assert row(config.db_path)["force"] is False


# ---------------------------------------------------------------------------
# The orphaned-window cancel
# ---------------------------------------------------------------------------


class TestTheOrphanedWindowCancel:
    """A terminal row with a live window behind it, which is what a cancel
    written by the web process looks like from here.

    That process holds no bridge on the Ansible deployment, so `DELETE` can
    only stamp the row and leave the window to whoever owns it. Left alone, the
    next QR rotation republishes the relay about twenty seconds after the
    operator was told the pairing was over, for the rest of the window's TTL.

    The poll's cheap read returns early on a terminal row, so this arm has to
    sit on that path — which is also what closes Stage 3's stated residual, a
    window whose adopt write was refused and whose id therefore never reached
    the row at all.
    """

    def _arm_window(self, bridge, window_id: str) -> None:
        bridge.pairing_window = baileys_bridge.PairingWindow(
            window_id=window_id,
            opened_at=0.0,
            expires_at=1_000.0,
            expires_at_wall=time.time() + 300.0,
            requested_by=USER,
            destructive=True,
        )

    def test_a_terminal_row_cancels_the_live_window(
        self, config, bridge, spawns
    ):
        window = request(config.db_path)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_FAILED, "cancelled from the UI",
            )
        self._arm_window(bridge, window)

        runtime.poll_pairing_request(config)

        assert spawns == [CANCEL]

    def test_the_cancel_is_scoped_to_the_window_the_bridge_actually_holds(
        self, config, bridge, monkeypatch
    ):
        """The live window's id, not the row's. A window whose adopt write was
        refused never put its id on the row, and an unscoped close would cancel
        whatever window happened to be open by the time it ran."""
        row_id = request(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, row_id, db.WHATSAPP_PAIRING_EXPIRED, "expired",
            )
        self._arm_window(bridge, "window-the-row-never-saw")

        def run(coro, *, name: str):
            asyncio.run(coro)
            return None

        monkeypatch.setattr(async_runtime, "spawn_task", run)
        runtime.poll_pairing_request(config)

        assert bridge.cancelled == ["window-the-row-never-saw"]

    def test_a_terminal_row_with_no_window_cancels_nothing(
        self, config, bridge, spawns
    ):
        """The control. A paired window is already closed by the bridge, so
        this arm must not fire on every tick for the rest of the deployment's
        life."""
        window = request(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_PAIRED, "paired",
            )

        runtime.poll_pairing_request(config)

        assert spawns == []

    def test_no_row_at_all_cancels_nothing(self, config, bridge, spawns):
        runtime.poll_pairing_request(config)
        assert spawns == []

    def test_a_refused_spawn_costs_the_cancel_and_nothing_else(
        self, config, bridge, monkeypatch, caplog
    ):
        """The runtime is stopping, so nothing will run the cancel — and a poll
        tick must not raise whatever happens here. The window's own watchdog is
        still the backstop."""
        window = request(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_FAILED, "cancelled",
            )
        self._arm_window(bridge, window)

        def refuse(coro, *, name: str):
            coro.close()
            raise RuntimeError("the runtime is stopping")

        monkeypatch.setattr(async_runtime, "spawn_task", refuse)
        with caplog.at_level("WARNING"):
            runtime.poll_pairing_request(config)

        assert "orphan_cancel_unscheduled" in caplog.text


class TestTheOrphanPredicate:
    """Which row states let a live window be read as a leftover.

    Its own class because the answer is not "terminal": a fresh request written
    over a closed one is `requested`, and without that arm the previous
    window survives, `repair_session` refuses the new request
    `already_pairing` against it, and every retry fails for the rest of its
    TTL. `servicing` is the state that must stay out, because a healthy
    re-pair spends it with the row's id and the window's id genuinely
    different.
    """

    @pytest.mark.parametrize(
        "state,orphaned",
        [
            (None, True),
            (db.WHATSAPP_PAIRING_PAIRED, True),
            (db.WHATSAPP_PAIRING_EXPIRED, True),
            (db.WHATSAPP_PAIRING_FAILED, True),
            (db.WHATSAPP_PAIRING_REQUESTED, True),
            (db.WHATSAPP_PAIRING_SERVICING, False),
            (db.WHATSAPP_PAIRING_AWAITING_SIDECAR, False),
            (db.WHATSAPP_PAIRING_AWAITING_SCAN, False),
            (db.WHATSAPP_PAIRING_SIDECAR_ABSENT, False),
        ],
    )
    def test_the_predicate_covers_every_state(self, state, orphaned):
        row = None if state is None else {"state": state, "window_id": "w"}
        assert runtime._pairing_row_owns_no_window(row) is orphaned

    def test_a_fresh_request_cancels_the_previous_window(
        self, config, bridge, spawns
    ):
        """The feature the `requested` arm exists for.

        Negative control: dropping that arm from the predicate turns this red
        and leaves the terminal cases green — the row is non-terminal, so the
        tick would skip the orphan check entirely and `repair_session` would
        refuse the new request against a window nobody owns.
        """
        first = request(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, first, db.WHATSAPP_PAIRING_EXPIRED, "expired",
            )
        bridge.pairing_window = baileys_bridge.PairingWindow(
            window_id=first,
            opened_at=0.0,
            expires_at=1_000.0,
            expires_at_wall=time.time() + 300.0,
            requested_by=USER,
            destructive=True,
        )
        request(config.db_path)

        runtime.poll_pairing_request(config)

        assert CANCEL in spawns

    def test_a_window_being_serviced_is_left_alone(self, config, bridge, spawns):
        """The control for the arm above, and the one that matters most: the
        row carries the *request* id and the window its own for the whole span
        between `open_pairing_window` and the adopt write, so an id comparison
        here would cancel the window it is servicing."""
        window = request(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_SERVICING,
            )
        bridge.pairing_window = baileys_bridge.PairingWindow(
            window_id="a-different-id-the-row-has-not-adopted",
            opened_at=0.0,
            expires_at=1_000.0,
            expires_at_wall=time.time() + 300.0,
            requested_by=USER,
            destructive=True,
        )

        runtime.poll_pairing_request(config)

        assert CANCEL not in spawns
        assert bridge.cancelled == []


class TestTheOrphanedRelaySweep:
    """A relay file that outlived the row, with no window left to cancel.

    The case is a scheduler restart mid-window followed by a `DELETE`: the new
    bridge has no window, so there is nothing to cancel, and
    `_clean_up_after_closure` — the sibling that would have unlinked the file —
    never runs again, because its closure is computed past the terminal early
    return and the deadline arm only looks at non-terminal rows. Left alone the
    last pairing code sits at 0600 for the life of the deployment.
    """

    def _publish(self, path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        assert pairing_relay.write_relay(
            path,
            pairing_relay.build_payload(
                window_id="gone",
                state=pairing_relay.STATE_AWAITING_SCAN,
                expires_at=time.time() + 300.0,
                qr="2@fake-payload",
                qr_seq=1,
            ),
        )

    def test_a_terminal_row_with_no_window_sweeps_the_relay(
        self, config, bridge, relay_file, spawns
    ):
        window = request(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_FAILED, "cancelled from the UI",
            )
        self._publish(relay_file)

        runtime.poll_pairing_request(config)

        assert bridge.relay_clears == 1
        assert spawns == []

    def test_nothing_is_swept_when_no_file_is_there(
        self, config, bridge, relay_file
    ):
        """The cost control. This runs on the dispatch thread on every tick for
        as long as the row stays closed, and `clear_relay` globs the directory
        for staging files — so the existence test has to come first or a
        long-closed deployment pays a directory listing per tick for ever."""
        window = request(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_PAIRED, "paired",
            )

        runtime.poll_pairing_request(config)

        assert bridge.relay_clears == 0

    def test_a_live_window_is_cancelled_rather_than_swept(
        self, config, bridge, relay_file, spawns
    ):
        """Two leftovers, two remedies, and the cancel is the one that also
        unlinks — under the bridge's own lock, which a direct sweep from this
        thread would bypass."""
        window = request(config.db_path)
        with db.get_db(config.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_FAILED, "cancelled",
            )
        self._publish(relay_file)
        bridge.pairing_window = baileys_bridge.PairingWindow(
            window_id=window,
            opened_at=0.0,
            expires_at=1_000.0,
            expires_at_wall=time.time() + 300.0,
            requested_by=USER,
            destructive=True,
        )

        runtime.poll_pairing_request(config)

        assert spawns == [CANCEL]
        assert bridge.relay_clears == 0

    def test_with_no_bridge_the_configured_path_is_swept(
        self, tmp_path, config, monkeypatch
    ):
        """**The configured path, not the default one**, which is Stage 3's
        fourth handoff and was discharged by construction rather than by a
        test. `default_pairing_relay_path` is the single resolver and every
        caller inherits the key through it; this is what says so for the arm
        that has no bridge to ask.
        """
        from istota.transport.whatsapp import baileys_bridge as bb

        relay = tmp_path / "elsewhere" / "qr.json"
        cfg = replace(
            config,
            whatsapp=build_whatsapp_config(
                enabled=True, provider="baileys",
                pairing_relay_path=str(relay),
            ),
        )
        monkeypatch.setattr(bb, "active_bridge", lambda: None)
        window = request(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_FAILED, "cancelled",
            )
        self._publish(relay)
        assert relay.exists()

        runtime.poll_pairing_request(cfg)

        assert not relay.exists()

    def test_a_relay_at_the_default_path_is_left_alone_when_one_is_configured(
        self, tmp_path, config, monkeypatch
    ):
        """The control for the case above: without the key being read, the
        sweep would go to `{db_path.parent}` and miss the operator's file."""
        from istota.transport.whatsapp import baileys_bridge as bb

        configured = tmp_path / "elsewhere" / "qr.json"
        default = Path(config.db_path).parent / bb.PAIRING_RELAY_NAME
        cfg = replace(
            config,
            whatsapp=build_whatsapp_config(
                enabled=True, provider="baileys",
                pairing_relay_path=str(configured),
            ),
        )
        monkeypatch.setattr(bb, "active_bridge", lambda: None)
        window = request(cfg.db_path)
        with db.get_db(cfg.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_FAILED, "cancelled",
            )
        self._publish(default)

        runtime.poll_pairing_request(cfg)

        assert default.exists(), (
            "the sweep went to the default path, so the configured key is not "
            "being read"
        )


# ---------------------------------------------------------------------------
# Adoption after a restart (ISSUE-504)
# ---------------------------------------------------------------------------


class TestTheAdoptionArm:
    """A window a restart orphaned is re-armed, not closed.

    The credential is spent *before* the part that can be interrupted, and on
    the reference deployment the interruption is routine: the update cron
    restarts the scheduler on any commit, so a 300s window overlaps one
    whenever anything lands. The sidecar is a unit of its own and survives, so
    it is still offering a code every twenty seconds — the only thing the
    restart lost is the daemon's record that somebody was waiting for one.

    Each test names the control that makes it able to fail.
    """

    ARCHIVE = "/srv/session.20260101T000000Z"

    def _orphaned(self, config, *, state=None, window_seconds=3600):
        """A row in a window state with no live window behind it."""
        state = state or db.WHATSAPP_PAIRING_AWAITING_SCAN
        request_id = request(config.db_path, window_seconds=window_seconds)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, request_id, state, self.ARCHIVE,
                adopt_window_id="win-orphan",
                expires_at=time.time() + window_seconds,
            )
        return request_id

    def test_a_restart_adopts_the_window_rather_than_closing_it(
        self, config, bridge, spawns
    ):
        """The regression. Control: drop the adopt arm from
        `_expire_stale_pairing` and the row closes `failed` with the credential
        already archived, which is the reported defect.
        """
        self._orphaned(config)

        runtime.poll_pairing_request(config)

        assert spawns == [ADOPT]
        after = row(config.db_path)
        assert after["state"] == db.WHATSAPP_PAIRING_AWAITING_SCAN
        assert after["window_id"] == "win-orphan"

    def test_the_adoption_carries_the_rows_own_id_and_deadline(
        self, config, bridge, spawns
    ):
        """A window minted fresh would outlive the durable deadline, which is
        the bound the issue requires adoption to keep. `destructive` comes off
        the row's message, which is the archive path and nothing else.
        """
        self._orphaned(config, window_seconds=120)

        runtime.poll_pairing_request(config)

        assert bridge.adoptions
        window_id, requested_by, expires_at_wall, destructive, _ = (
            bridge.adoptions[0]
        )
        assert window_id == "win-orphan"
        assert requested_by == USER
        assert destructive is True
        assert 0 < expires_at_wall - time.time() <= 121

    def test_a_row_that_relayed_a_code_says_so_in_the_adoption(
        self, config, bridge, spawns
    ):
        """`awaiting_scan` is the durable record that a code went out, and the
        bridge needs it: without it a scan the previous process relayed closes
        the re-adopted window `failed` and alerts every admin to re-pair.
        """
        self._orphaned(config, state=db.WHATSAPP_PAIRING_AWAITING_SCAN)

        runtime.poll_pairing_request(config)

        assert bridge.adoptions[0][4] is True

    def test_a_row_that_relayed_nothing_says_that_instead(
        self, config, bridge, spawns
    ):
        """The other half, and why it is read off the row rather than assumed.
        `sidecar_absent` is a demotion of `awaiting_sidecar`, so it too means no
        code was ever offered — a `ready` on such a window is a reconnect of a
        working session and must not be recorded as a pairing.
        """
        self._orphaned(config, state=db.WHATSAPP_PAIRING_SIDECAR_ABSENT)

        runtime.poll_pairing_request(config)

        assert bridge.adoptions[0][4] is False

    def test_a_row_past_its_deadline_is_still_expired(
        self, config, bridge, spawns
    ):
        """Control: move the adopt arm ahead of the deadline arm and this goes
        red. Adoption changes *which* arm closes a row, never whether one does.
        """
        request_id = request(config.db_path, window_seconds=0)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, request_id, db.WHATSAPP_PAIRING_AWAITING_SCAN,
                adopt_window_id="win-old", expires_at=time.time() - 1,
            )

        runtime.poll_pairing_request(config)

        assert row(config.db_path)["state"] == db.WHATSAPP_PAIRING_EXPIRED
        assert ADOPT not in spawns

    def test_a_close_this_process_performed_is_still_recorded(
        self, config, bridge, spawns
    ):
        """Control: move the adopt arm ahead of the outcome branch and a
        successful pairing is re-armed over a session that had just come up.
        """
        self._orphaned(config)
        bridge.last_pairing_outcome = type(
            "Outcome", (), {
                "window_id": "win-orphan",
                "state": db.WHATSAPP_PAIRING_PAIRED,
                "message": "paired",
            },
        )()

        runtime.poll_pairing_request(config)

        assert row(config.db_path)["state"] == db.WHATSAPP_PAIRING_PAIRED
        assert ADOPT not in spawns

    def test_a_fresh_request_is_never_adopted(self, config, bridge, spawns):
        """`requested` is not a window state, so there is no window to adopt —
        it is serviced. Control: widen the arm past
        `WHATSAPP_PAIRING_WINDOW_STATES` and the re-pair never runs.
        """
        request(config.db_path)

        runtime.poll_pairing_request(config)

        assert spawns == ["whatsapp-pairing"]

    def test_a_process_with_no_bridge_adopts_nothing(
        self, config, spawns, monkeypatch
    ):
        """The web process holds no bridge on the split Ansible shape."""
        monkeypatch.setattr(baileys_bridge, "active_bridge", lambda: None)
        self._orphaned(config)

        runtime.poll_pairing_request(config)

        assert spawns == []
        assert row(config.db_path)["state"] == db.WHATSAPP_PAIRING_AWAITING_SCAN

    def test_a_refused_spawn_falls_through_to_the_orphan_close(
        self, config, bridge, monkeypatch
    ):
        """The runtime is stopping, so nothing will adopt. This is the one path
        the orphan close is still reached by, and it must still close the row
        and carry the archive path forward.
        """
        monkeypatch.setattr(async_runtime, "spawn_task", _refuse_adopt)
        self._orphaned(config)

        runtime.poll_pairing_request(config)

        closed = row(config.db_path)
        assert closed["state"] == db.WHATSAPP_PAIRING_FAILED
        assert self.ARCHIVE in closed["message"]

    def test_a_row_with_an_unreadable_deadline_is_closed_not_adopted(
        self, config, bridge, spawns
    ):
        """A window this cannot bound is not a window worth arming: the
        watchdog would have no deadline to enforce and the credential would sit
        published for the life of the process.
        """
        request_id = request(config.db_path, window_seconds=3600)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, request_id, db.WHATSAPP_PAIRING_AWAITING_SCAN,
                adopt_window_id="win-noddl",
            )
            conn.execute(
                "UPDATE whatsapp_runtime SET pairing_expires_at = NULL "
                "WHERE singleton = 1"
            )

        runtime.poll_pairing_request(config)

        assert row(config.db_path)["state"] == db.WHATSAPP_PAIRING_FAILED
        assert ADOPT not in spawns

    def test_adoption_is_single_flight_across_two_ticks(
        self, config, bridge, spawns
    ):
        """The second tick finds the window this process now holds and leaves
        it alone — arm 4, unchanged.
        """
        self._orphaned(config)

        runtime.poll_pairing_request(config)
        bridge.pairing_window = type(
            "Window", (), {
                "window_id": "win-orphan",
                "state": db.WHATSAPP_PAIRING_AWAITING_SCAN,
                "message": "",
            },
        )()
        runtime.poll_pairing_request(config)

        assert spawns == [ADOPT]
