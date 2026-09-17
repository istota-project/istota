"""The pairing window and the relay file it publishes a QR through.

A pairing QR is the full-account credential for one WhatsApp number: anything
that scans it is linked as a device. So every property here is about where that
payload may and may not go, and each one is asserted rather than read:

**It reaches no log, at any level, on any path.** `_handle_qr` has never logged
it and must not start, so the no-window case scans every captured record —
message, args and formatted output — for the payload rather than only checking
that no file appeared. A test that looked at the file alone would not see a
regression in the log line beside it.

**It reaches no disk outside an armed window.** The relay is written only inside
an admin-initiated, TTL-bounded window. Without that constraint every unpaired
boot would publish a credential to anyone who can read the state directory, and
the bridge receives those frames on every deployment already.

**No relay touch happens on the read loop.** The loop dispatches and never
works: an inbound event that blocks it deadlocks the link, and the same loop
carries the `send_result` frames the outbound ledger waits on inside its
claim-to-settle region. `_call_back` does not solve that — it runs the callback
inline and schedules an awaitable on the *same* loop — so the assertion is
about the loop still servicing frames while a relay write is blocked, not about
the file eventually appearing.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from istota.config import Config, UserConfig
from istota.transport.whatsapp import baileys_protocol as proto
from istota.transport.whatsapp import baileys_bridge as baileys_bridge_module
from istota.transport.whatsapp import pairing_relay
from istota.transport.whatsapp.baileys_bridge import (
    PAIRING_WINDOW_SECONDS,
    SIDECAR_STOP_TIMEOUT,
    BaileysBridge,
    BridgeStatus,
    default_pairing_relay_path,
    sidecar_return_timeout,
)

from .support.baileys_sidecar import FakeSidecar, SocketDir, wait_for
from .support.whatsapp_config import build_whatsapp_config

USER = "alice"
BAILEYS = "baileys"

#: This checkout's root, for the leaf-boundary guard.
REPO_ROOT = Path(__file__).resolve().parents[1]

#: A deadline comfortably ahead of any test run. `read_relay` checks it on
#: every read, so a fixed past epoch would read as expired.
FUTURE = time.time() + 3600.0

#: Distinctive enough that a substring scan over captured log records cannot
#: match it by accident, and shaped like the real thing (a `2@…` ref).
QR = "2@SENTINELqrPAYLOAD/9x+abcDEF=="


@pytest.fixture
def sockets():
    directory = SocketDir()
    yield directory
    directory.cleanup()


@pytest.fixture
def config(tmp_path) -> Config:
    return Config(
        db_path=tmp_path / "state" / "istota.db",
        temp_dir=tmp_path / "tmp",
        whatsapp=build_whatsapp_config(enabled=True, provider=BAILEYS),
        users={USER: UserConfig()},
    )


@pytest.fixture
async def bridge(config, sockets):
    """A bridge with a fast window, so the watchdog's deadlines are reachable."""
    instance = BaileysBridge(
        config,
        socket_path=sockets.socket,
        session_dir=sockets.session,
        send_timeout=2.0,
        pairing_relay_path=sockets.path / "whatsapp-pairing.json",
        pairing_window_seconds=1.5,
        sidecar_return_timeout=0.2,
    )
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.fixture
async def sidecar(bridge, sockets):
    fake = FakeSidecar(sockets.socket)
    await fake.connect()
    await wait_for(lambda: bridge.status.connected is True)
    try:
        yield fake
    finally:
        await fake.close()


def relay_path(bridge) -> Path:
    return bridge.pairing_relay_path


def mode_of(path: Path) -> int:
    return os.stat(path).st_mode & 0o777


def mentions_payload(caplog) -> list[str]:
    """Every captured record that carries the payload, however it was passed."""
    hits = []
    for record in caplog.records:
        rendered = record.getMessage()
        if QR in rendered or QR in str(record.args) or QR in str(record.msg):
            hits.append(rendered)
    return hits


# ---------------------------------------------------------------------------
# The leaf
# ---------------------------------------------------------------------------


class TestTheRelayLeaf:
    """Reader and writer, stdlib only, never raising."""

    def test_a_payload_round_trips_at_0600(self, tmp_path):
        path = tmp_path / "relay.json"
        payload = pairing_relay.build_payload(
            window_id="w1",
            state=pairing_relay.STATE_AWAITING_SCAN,
            expires_at=FUTURE,
            qr=QR,
            qr_seq=3,
        )
        assert pairing_relay.write_relay(path, payload) is True
        assert mode_of(path) == 0o600
        read = pairing_relay.read_relay(path)
        assert read is not None
        assert read["window_id"] == "w1"
        assert read["qr"] == QR
        assert read["qr_seq"] == 3

    def test_a_qr_is_absent_unless_the_state_is_awaiting_scan(self, tmp_path):
        path = tmp_path / "relay.json"
        payload = pairing_relay.build_payload(
            window_id="w1",
            state=pairing_relay.STATE_AWAITING_SIDECAR,
            expires_at=FUTURE,
            qr=QR,
        )
        assert "qr" not in payload
        pairing_relay.write_relay(path, payload)
        assert QR.encode() not in path.read_bytes()

    def test_a_file_from_another_window_is_ignored(self, tmp_path):
        path = tmp_path / "relay.json"
        pairing_relay.write_relay(
            path,
            pairing_relay.build_payload(
                window_id="old",
                state=pairing_relay.STATE_AWAITING_SCAN,
                expires_at=FUTURE,
                qr=QR,
            ),
        )
        assert pairing_relay.read_relay(path, expected_window_id="old") is not None
        assert pairing_relay.read_relay(path, expected_window_id="new") is None

    def test_a_deadline_already_past_is_ignored(self, tmp_path):
        path = tmp_path / "relay.json"
        pairing_relay.write_relay(
            path,
            pairing_relay.build_payload(
                window_id="w1",
                state=pairing_relay.STATE_AWAITING_SCAN,
                expires_at=100.0,
                qr=QR,
            ),
        )
        assert pairing_relay.read_relay(path, now=99.0) is not None
        assert pairing_relay.read_relay(path, now=101.0) is None

    def test_the_deadline_is_checked_without_being_asked(self, tmp_path):
        """The shortest call is the one a later reader copies, so it has to
        fail closed — an expired relay is exactly what a skipped unlink
        leaves behind."""
        path = tmp_path / "relay.json"
        pairing_relay.write_relay(
            path,
            pairing_relay.build_payload(
                window_id="w1",
                state=pairing_relay.STATE_AWAITING_SCAN,
                expires_at=time.time() - 1.0,
                qr=QR,
            ),
        )
        assert pairing_relay.read_relay(path) is None
        assert pairing_relay.read_relay(path, expected_window_id="w1") is None

    def test_a_torn_or_foreign_file_reads_as_nothing(self, tmp_path):
        path = tmp_path / "relay.json"
        path.write_text('{"window_id": "w1", "state": "awaiting_sc')
        assert pairing_relay.read_relay(path) is None
        path.write_text('["not", "an", "object"]')
        assert pairing_relay.read_relay(path) is None
        path.write_text('{"state": "awaiting_scan"}')
        assert pairing_relay.read_relay(path) is None
        assert pairing_relay.read_relay(tmp_path / "absent.json") is None

    def test_an_unwritable_destination_reports_the_errno_and_the_path(
        self, tmp_path, caplog,
    ):
        path = tmp_path / "missing-dir" / "relay.json"
        payload = pairing_relay.build_payload(
            window_id="w1",
            state=pairing_relay.STATE_AWAITING_SCAN,
            expires_at=FUTURE,
            qr=QR,
        )
        with caplog.at_level(logging.DEBUG):
            assert pairing_relay.write_relay(path, payload) is False
        assert mentions_payload(caplog) == []
        joined = " ".join(record.getMessage() for record in caplog.records)
        assert "missing-dir" in joined
        assert "ENOENT" in joined or "errno=2" in joined

    def test_two_concurrent_writes_both_complete(self, tmp_path):
        """Two writers at once both publish, and the file is one of them.

        Deliberately **not** claiming to catch a fixed staging name: that name
        is `tempfile.mkstemp`'s, inside `atomic_write`, so the defect is
        unreachable from here without editing that module — and two barrier
        -synchronised threads would not reliably collide even then.
        `tests/test_atomic_write.py` owns the staging-name property. What this
        pins is that the relay is safe as a re-entered writer at all, which is
        the shape `health/documents.py` was not.
        """
        path = tmp_path / "relay.json"
        start = threading.Barrier(2)
        results: list[bool] = []
        lock = threading.Lock()

        def writer(seq: int) -> None:
            payload = pairing_relay.build_payload(
                window_id="w1",
                state=pairing_relay.STATE_AWAITING_SCAN,
                expires_at=FUTURE,
                qr=QR,
                qr_seq=seq,
            )
            start.wait(timeout=5)
            ok = pairing_relay.write_relay(path, payload)
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=writer, args=(n,)) for n in (1, 2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)
        assert results == [True, True]
        assert json.loads(path.read_text())["qr_seq"] in (1, 2)

    def test_a_reader_never_observes_a_partial_file(self, tmp_path):
        """Publishing is `os.replace` of a fully written file."""
        path = tmp_path / "relay.json"
        pairing_relay.write_relay(
            path,
            pairing_relay.build_payload(
                window_id="w1",
                state=pairing_relay.STATE_AWAITING_SCAN,
                expires_at=FUTURE,
                qr=QR,
                qr_seq=0,
            ),
        )
        stop = threading.Event()
        torn: list[str] = []

        def reader() -> None:
            while not stop.is_set():
                read = pairing_relay.read_relay(path)
                if read is None:
                    torn.append("unreadable")

        thread = threading.Thread(target=reader)
        thread.start()
        try:
            for seq in range(1, 60):
                pairing_relay.write_relay(
                    path,
                    pairing_relay.build_payload(
                        window_id="w1",
                        state=pairing_relay.STATE_AWAITING_SCAN,
                        expires_at=FUTURE,
                        qr=QR * 40,
                        qr_seq=seq,
                    ),
                )
        finally:
            stop.set()
            thread.join(timeout=5)
        assert torn == []

    def test_clearing_sweeps_a_staging_file_left_by_a_killed_writer(
        self, tmp_path, caplog,
    ):
        """An orphaned staging file holds the code and nothing else sweeps it.

        `atomic_write` says so itself: a staging file outlives the call when
        the process dies between the write and the rename, the names are
        unique per call so no later run reclaims one, and nothing sweeps them.
        For most callers that is inert. Here it is a pairing credential at
        0600 for as long as the deployment lasts.
        """
        path = tmp_path / "relay.json"
        orphan = tmp_path / f".{path.name}.abc123"
        orphan.write_text(json.dumps({"qr": QR}))
        os.chmod(orphan, 0o600)
        unrelated = tmp_path / ".something-else.tmp"
        unrelated.write_text("keep me")
        with caplog.at_level(logging.DEBUG):
            assert pairing_relay.clear_relay(path) is True
        assert not orphan.exists()
        assert unrelated.exists()
        assert mentions_payload(caplog) == []

    def test_clearing_is_idempotent(self, tmp_path):
        path = tmp_path / "relay.json"
        pairing_relay.write_relay(
            path,
            pairing_relay.build_payload(
                window_id="w1",
                state=pairing_relay.STATE_AWAITING_SIDECAR,
                expires_at=FUTURE,
            ),
        )
        assert pairing_relay.clear_relay(path) is True
        assert not path.exists()
        assert pairing_relay.clear_relay(path) is True

    def test_the_leaf_reaches_only_the_neighbouring_atomic_write(self):
        """The reader runs in the web process and must not pull in the bridge.

        Asserted **transitively** and over *stripped* lines, which is
        `tests/native/test_session_log.py`'s shape and for its reason. Two
        weaker forms miss the cases that matter: an unstripped scan cannot see
        a function-scope `from ...db import get_db`, which is the leaf
        convention's own failure mode, and a check on this module alone would
        pass the day the one helper it permits grows a `config` import and
        brings the whole graph in behind an already-approved name.
        """
        permitted = {
            "src/istota/transport/whatsapp/pairing_relay.py": {
                "from ...atomic_write import write_bytes_atomic",
            },
            "src/istota/atomic_write.py": set(),
        }
        for relative, allowed in permitted.items():
            source = (REPO_ROOT / relative).read_text()
            found = {
                line.strip()
                for line in source.splitlines()
                if line.strip().startswith(
                    ("from .", "import istota", "from istota"),
                )
            }
            assert found == allowed, relative


# ---------------------------------------------------------------------------
# Where the relay lives
# ---------------------------------------------------------------------------


class TestTheDefaultRelayPath:
    def test_it_sits_beside_the_database(self, tmp_path):
        config = Config(
            db_path=tmp_path / "state" / "istota.db",
            workspace_path=tmp_path / "workspace",
            temp_dir=tmp_path / "tmp",
        )
        assert default_pairing_relay_path(config) == (
            tmp_path / "state" / "whatsapp-pairing.json"
        )

    def test_it_moves_off_the_workspace_where_the_two_collapse(self, tmp_path):
        """The standalone shape puts `db_path`, the workspace and `tmp` in one
        directory, and that directory is the one the sandbox binds per user."""
        workspace = tmp_path / "istota"
        config = Config(
            db_path=workspace / "istota.db",
            workspace_path=workspace,
            temp_dir=workspace / "tmp",
        )
        assert default_pairing_relay_path(config) == (
            workspace / "tmp" / "whatsapp-pairing.json"
        )

    def test_the_configured_key_wins_over_both_derivations(self, tmp_path):
        """**One resolver, so every caller inherits the key.** The bridge's
        constructor default, the web routes and the poll's no-bridge relay
        sweep all reach the path through this function, which is what makes an
        operator-set `pairing_relay_path` reach the sweep as well as the write
        — a key read at only some of the callers is a deployment whose orphan
        clear misses the file it exists to remove.

        Negative control: dropping the configured branch turns this red and
        turns both `sandbox_bound_reason` refusals in
        `tests/test_whatsapp_pairing_web.py` red with it.
        """
        from istota.config import WhatsAppBaileysConfig, WhatsAppConfig

        config = Config(
            db_path=tmp_path / "state" / "istota.db",
            workspace_path=tmp_path / "workspace",
            temp_dir=tmp_path / "tmp",
            whatsapp=WhatsAppConfig(
                provider="baileys",
                baileys=WhatsAppBaileysConfig(
                    pairing_relay_path="/srv/relay/qr.json",
                ),
            ),
        )

        assert default_pairing_relay_path(config) == Path("/srv/relay/qr.json")

    def test_a_tilde_in_the_configured_path_is_expanded(self, tmp_path):
        """`~` is what an operator writes and what no syscall understands."""
        from istota.config import WhatsAppBaileysConfig, WhatsAppConfig

        config = Config(
            db_path=tmp_path / "state" / "istota.db",
            temp_dir=tmp_path / "tmp",
            whatsapp=WhatsAppConfig(
                provider="baileys",
                baileys=WhatsAppBaileysConfig(pairing_relay_path="~/qr.json"),
            ),
        )
        resolved = default_pairing_relay_path(config)

        assert "~" not in str(resolved)
        assert resolved.is_absolute()

    def test_whitespace_alone_falls_back_to_the_derivation(self, tmp_path):
        """An operator who cleared the key in a generated file leaves a space,
        and a path of one space is not what they asked for."""
        from istota.config import WhatsAppBaileysConfig, WhatsAppConfig

        config = Config(
            db_path=tmp_path / "state" / "istota.db",
            workspace_path=tmp_path / "workspace",
            temp_dir=tmp_path / "tmp",
            whatsapp=WhatsAppConfig(
                provider="baileys",
                baileys=WhatsAppBaileysConfig(pairing_relay_path="   "),
            ),
        )

        assert default_pairing_relay_path(config) == (
            tmp_path / "state" / "whatsapp-pairing.json"
        )


class TestTheTwoWaits:
    """`SIDECAR_RETURN_TIMEOUT` is additive; only one of its terms scales.

    Asserted over two declared intervals rather than one: a single value passes
    against a multiplier, a constant and an additive form alike.
    """

    def test_only_the_declared_interval_scales_the_return_timeout(self):
        assert sidecar_return_timeout(0) == 30.0
        assert sidecar_return_timeout(30) == 60.0
        assert sidecar_return_timeout(5) == 35.0
        assert (
            sidecar_return_timeout(30) - sidecar_return_timeout(5)
            == pytest.approx(25.0)
        )

    def test_the_stop_timeout_does_not_depend_on_the_interval(self):
        assert SIDECAR_STOP_TIMEOUT == 15.0
        assert PAIRING_WINDOW_SECONDS == 300.0

    def test_a_nonsense_declaration_costs_only_the_fixed_allowance(self):
        assert sidecar_return_timeout(-10) == 30.0
        assert sidecar_return_timeout("nonsense") == 30.0
        assert sidecar_return_timeout(None) == 30.0


# ---------------------------------------------------------------------------
# The QR handler
# ---------------------------------------------------------------------------


class TestTheQrHandlerWithNoWindow:
    """Today's behaviour, byte for byte: a log line and a discarded payload."""

    async def test_it_writes_no_file_and_logs_no_payload(
        self, bridge, sidecar, caplog,
    ):
        with caplog.at_level(logging.DEBUG):
            await sidecar.say(proto.MSG_QR, qr=QR)
            await wait_for(
                lambda: any(
                    "qr_offered" in record.getMessage()
                    for record in caplog.records
                ),
            )
        assert not relay_path(bridge).exists()
        assert mentions_payload(caplog) == []
        assert bridge.pairing_window is None

    async def test_an_expired_window_falls_through_to_that_same_path(
        self, bridge, sidecar, caplog,
    ):
        window = await bridge.open_pairing_window(USER)
        assert window is not None
        # **The watchdog is stopped first**, or this case can pass through the
        # plain no-window path with identical assertions: it wakes within
        # ~0.2s of the open, reads the hand-mutated deadline and closes the
        # window, and `_handle_qr` then sees `None` rather than the
        # expired-but-open branch under test.
        bridge._pairing_watchdog.cancel()
        window.expires_at = asyncio.get_running_loop().time() - 1.0
        with caplog.at_level(logging.DEBUG):
            await sidecar.say(proto.MSG_QR, qr=QR)
            await wait_for(
                lambda: any(
                    "qr_offered" in record.getMessage()
                    for record in caplog.records
                ),
            )
        assert mentions_payload(caplog) == []
        # Still installed, so the branch that was skipped was the window one
        # rather than the window itself having gone.
        assert bridge.pairing_window is window
        read = pairing_relay.read_relay(relay_path(bridge))
        assert read is None or "qr" not in read

    async def test_the_own_sidecar_callback_still_receives_it(
        self, config, sockets,
    ):
        """`istota whatsapp pair`'s own-sidecar mode keeps its callback."""
        seen: list[str] = []
        instance = BaileysBridge(
            config,
            socket_path=sockets.socket,
            session_dir=sockets.session,
            pairing_relay_path=sockets.path / "relay.json",
            on_qr=seen.append,
        )
        await instance.start()
        try:
            fake = FakeSidecar(sockets.socket)
            await fake.connect()
            await wait_for(lambda: instance.status.connected is True)
            await fake.say(proto.MSG_QR, qr=QR)
            await wait_for(lambda: seen == [QR])
            await fake.close()
        finally:
            await instance.stop()
        assert not (sockets.path / "relay.json").exists()


class TestTheQrHandlerInsideAWindow:
    async def test_an_armed_window_suppresses_the_callback(self, config, sockets):
        """One code, one channel.

        A bridge carrying both an `on_qr` callback and an open window must
        deliver through the relay alone. Double delivery is the "two channels"
        the design rejects, and a suite that only covers the callback with no
        window open would not see it.
        """
        seen: list[str] = []
        instance = BaileysBridge(
            config,
            socket_path=sockets.socket,
            session_dir=sockets.session,
            pairing_relay_path=sockets.path / "relay.json",
            pairing_window_seconds=5.0,
            sidecar_return_timeout=5.0,
            on_qr=seen.append,
        )
        await instance.start()
        try:
            fake = FakeSidecar(sockets.socket)
            await fake.connect()
            await wait_for(lambda: instance.status.connected is True)
            await instance.open_pairing_window(USER)
            await fake.say(proto.MSG_QR, qr=QR)
            await wait_for(
                lambda: (
                    pairing_relay.read_relay(sockets.path / "relay.json") or {}
                ).get("qr") == QR
            )
            await asyncio.sleep(0.05)
            assert seen == []
            await fake.close()
        finally:
            await instance.stop()

    async def test_it_publishes_0600_with_the_window_id(self, bridge, sidecar):
        window = await bridge.open_pairing_window(USER)
        assert window is not None
        await sidecar.say(proto.MSG_QR, qr=QR)
        await wait_for(
            lambda: (pairing_relay.read_relay(relay_path(bridge)) or {}).get("qr")
        )
        read = pairing_relay.read_relay(relay_path(bridge))
        assert read["window_id"] == window.window_id
        assert read["state"] == pairing_relay.STATE_AWAITING_SCAN
        assert read["qr"] == QR
        assert read["qr_seq"] == 1
        assert mode_of(relay_path(bridge)) == 0o600

    async def test_each_rotation_increments_the_sequence(self, bridge, sidecar):
        await bridge.open_pairing_window(USER)
        for expected in (1, 2, 3):
            await sidecar.say(proto.MSG_QR, qr=f"{QR}{expected}")
            await wait_for(
                lambda expected=expected: (
                    pairing_relay.read_relay(relay_path(bridge)) or {}
                ).get("qr_seq") == expected
            )

    async def test_the_payload_still_reaches_no_log(self, bridge, sidecar, caplog):
        await bridge.open_pairing_window(USER)
        with caplog.at_level(logging.DEBUG):
            await sidecar.say(proto.MSG_QR, qr=QR)
            await wait_for(
                lambda: (
                    pairing_relay.read_relay(relay_path(bridge)) or {}
                ).get("qr")
            )
        assert mentions_payload(caplog) == []

    async def test_a_malformed_frame_arms_nothing(self, bridge, sidecar):
        await bridge.open_pairing_window(USER)
        before = bridge.status.malformed_lines
        await sidecar.say(proto.MSG_QR, qr="")
        await wait_for(lambda: bridge.status.malformed_lines == before + 1)
        read = pairing_relay.read_relay(relay_path(bridge))
        assert read is not None
        assert "qr" not in read


# ---------------------------------------------------------------------------
# The window's lifetime
# ---------------------------------------------------------------------------


class TestOpeningAWindow:
    async def test_it_publishes_the_initial_state(self, bridge):
        window = await bridge.open_pairing_window(USER)
        assert window is not None
        read = pairing_relay.read_relay(relay_path(bridge))
        assert read["state"] == pairing_relay.STATE_AWAITING_SIDECAR
        assert read["window_id"] == window.window_id
        assert "qr" not in read
        assert window.requested_by == USER
        assert window.destructive is False

    async def test_a_second_request_is_refused_and_the_first_survives(self, bridge):
        first = await bridge.open_pairing_window(USER)
        assert await bridge.open_pairing_window("bob") is None
        assert bridge.pairing_window is first

    async def test_a_window_the_bridge_stopped_under_is_not_returned(
        self, config, sockets, monkeypatch,
    ):
        """Both gates are re-read after the awaits.

        `reset_session`'s own rule, for its reason: they were answered before
        two awaits, and handing back a window `stop()` has since closed would
        have the caller write a durable request row for a window that does not
        exist — and then wait out its deadline for a code nothing will relay.
        """
        path = sockets.path / "relay.json"
        instance = BaileysBridge(
            config,
            socket_path=sockets.socket,
            session_dir=sockets.session,
            pairing_relay_path=path,
        )
        await instance.start()
        released = threading.Event()
        entered = threading.Event()
        real_write = pairing_relay.write_relay

        def blocking_write(target, payload):
            entered.set()
            released.wait(timeout=8)
            return real_write(target, payload)

        monkeypatch.setattr(pairing_relay, "write_relay", blocking_write)
        try:
            opening = asyncio.ensure_future(instance.open_pairing_window(USER))
            assert await asyncio.to_thread(entered.wait, 8) is True
            stopping = asyncio.ensure_future(instance.stop())
            await asyncio.sleep(0.05)
            released.set()
            assert await opening is None
            await stopping
        finally:
            released.set()
        assert instance.pairing_window is None
        assert not path.exists()

    async def test_the_status_reports_the_open_window_and_not_the_payload(
        self, bridge, sidecar,
    ):
        assert bridge.status.pairing_state is None
        assert bridge.status.pairing_expires_at is None
        window = await bridge.open_pairing_window(USER)
        assert bridge.status.pairing_state == pairing_relay.STATE_AWAITING_SIDECAR
        assert bridge.status.pairing_expires_at == window.expires_at_wall
        await sidecar.say(proto.MSG_QR, qr=QR)
        await wait_for(
            lambda: bridge.status.pairing_state == pairing_relay.STATE_AWAITING_SCAN
        )
        assert QR not in json.dumps(
            {
                key: value
                for key, value in vars(bridge.status).items()
            },
            default=str,
        )


class TestClosingAWindow:
    async def test_a_ready_frame_closes_it_and_unlinks_the_relay(
        self, bridge, sidecar,
    ):
        window = await bridge.open_pairing_window(USER)
        await sidecar.say(proto.MSG_QR, qr=QR)
        await wait_for(lambda: relay_path(bridge).exists())
        await sidecar.say(proto.MSG_READY)
        await wait_for(lambda: bridge.pairing_window is None)
        await wait_for(lambda: not relay_path(bridge).exists())
        outcome = bridge.last_pairing_outcome
        assert outcome is not None
        assert outcome.window_id == window.window_id
        assert outcome.state == pairing_relay.STATE_PAIRED
        assert bridge.status.pairing_state is None

    async def test_a_scoped_cancel_refuses_another_windows_id(
        self, bridge, sidecar
    ):
        """`poll_pairing_request` decides to cancel from a read taken on the
        dispatch thread and then schedules the cancel on the runtime loop, so
        by the time it runs the window it meant could have closed and a fresh
        one opened. An unscoped close would then end a pairing somebody is
        mid-scan on.

        Negative control: dropping the id comparison in `cancel_pairing` turns
        this red — the window closes and the relay is unlinked.
        """
        window = await bridge.open_pairing_window(USER)
        await sidecar.say(proto.MSG_QR, qr=QR)
        await wait_for(lambda: relay_path(bridge).exists())

        assert await bridge.cancel_pairing(window_id="some-other-window") is False
        assert bridge.pairing_window is window
        assert relay_path(bridge).exists()

        assert await bridge.cancel_pairing(window_id=window.window_id) is True
        assert bridge.pairing_window is None
        assert not relay_path(bridge).exists()

    async def test_the_lock_held_clear_removes_the_relay(self, bridge, sidecar):
        """The seam `poll_pairing_request` reaches for from another thread.

        `pairing_relay.clear_relay`'s own docstring names serialization behind
        `_relay_lock` as the precondition that makes it safe beside a live
        writer, and the poll runs on a thread that lock never sees — so it must
        not call that function directly.

        **What this case pins is the seam and its idempotency, not the lock.**
        Measured: replacing the body with a bare `pairing_relay.clear_relay`
        turns nothing red, because from one thread taking the lock or not is
        observationally identical. What rules the bypass out is on the other
        side — `tests/test_whatsapp_pairing_poll.py::TestTheClosureCleanup::
        test_a_relay_is_cleared_through_the_bridges_lock` requires the poll to
        come through here rather than reach for that function — plus the
        bridge's own pre-existing discipline, which this method delegates to.
        """
        await bridge.open_pairing_window(USER)
        await sidecar.say(proto.MSG_QR, qr=QR)
        await wait_for(lambda: relay_path(bridge).exists())

        assert bridge.clear_relay_file() is True
        assert not relay_path(bridge).exists()
        # Idempotent, because the poll can reach it for a window that is
        # already gone.
        assert bridge.clear_relay_file() is True

    async def test_cancelling_closes_it_and_unlinks_the_relay(self, bridge, sidecar):
        await bridge.open_pairing_window(USER)
        await sidecar.say(proto.MSG_QR, qr=QR)
        await wait_for(lambda: relay_path(bridge).exists())
        assert await bridge.cancel_pairing() is True
        assert bridge.pairing_window is None
        assert not relay_path(bridge).exists()
        assert await bridge.cancel_pairing() is False

    async def test_stopping_the_bridge_leaves_no_credential_behind(
        self, config, sockets,
    ):
        """A shutdown mid-window must not leave a live QR on disk."""
        path = sockets.path / "relay.json"
        instance = BaileysBridge(
            config,
            socket_path=sockets.socket,
            session_dir=sockets.session,
            pairing_relay_path=path,
        )
        await instance.start()
        try:
            await instance.open_pairing_window(USER)
            assert path.exists()
        finally:
            await instance.stop()
        assert not path.exists()
        assert instance.pairing_window is None

    async def test_a_write_for_a_closed_window_is_dropped(self, bridge):
        """A queued write must not re-create the file after the unlink.

        Nothing in this stage sweeps that path, so a resurrected relay is a
        pairing credential on disk with nothing left to remove it. Driven at
        the publish rather than through the socket, and deliberately: the
        end-to-end version cannot reach this ordering, because the relay lock
        is normally taken by the write *before* the close asks for it — so an
        in-flight write finishes first and the unlink still comes last. The
        case that needs the guard is the other order, where the close gets the
        lock before the queued write's task has started.
        """
        window = await bridge.open_pairing_window(USER)
        assert window is not None
        window.qr_seq = 1
        assert await bridge.cancel_pairing() is True
        assert not relay_path(bridge).exists()
        await bridge._publish_relay_qr(window, 1, QR)
        assert not relay_path(bridge).exists()

    async def test_a_superseded_rotation_is_dropped(self, bridge):
        """Two queued writes cannot publish in the wrong order."""
        window = await bridge.open_pairing_window(USER)
        window.state = pairing_relay.STATE_AWAITING_SCAN
        window.qr_seq = 2
        await bridge._publish_relay_qr(window, 1, QR)
        read = pairing_relay.read_relay(relay_path(bridge))
        assert read is not None
        assert "qr" not in read
        await bridge._publish_relay_qr(window, 2, QR)
        assert pairing_relay.read_relay(relay_path(bridge))["qr"] == QR

    async def test_a_cancelled_write_cannot_land_after_the_unlink(
        self, bridge, monkeypatch,
    ):
        """Cancelling the *awaiter* must not let the write overtake the unlink.

        This is the case an `asyncio.Lock` around the thread hop cannot hold,
        and it is reached on an ordinary shutdown: `AsyncRuntime._shutdown`
        cancels every pending task before it runs the cleanup hook that calls
        `stop()`. Cancelling a task awaiting `asyncio.to_thread` does not stop
        a thread that has already begun — only a future whose work has not
        started can be cancelled — so the `async with` would unwind and
        release the lock with the write still live, the unlink would take the
        free lock, and the orphaned thread's `os.replace` would put a pairing
        code back on disk after its window had closed. Held inside the thread,
        the lock cannot be released early.
        """
        released = threading.Event()
        entered = threading.Event()
        finished = threading.Event()
        real_write = pairing_relay.write_relay

        def blocking_write(path, payload):
            if not payload.get("qr"):
                return real_write(path, payload)
            entered.set()
            released.wait(timeout=8)
            try:
                return real_write(path, payload)
            finally:
                finished.set()

        monkeypatch.setattr(pairing_relay, "write_relay", blocking_write)
        try:
            window = await bridge.open_pairing_window(USER)
            window.state = pairing_relay.STATE_AWAITING_SCAN
            window.qr_seq = 1
            writing = asyncio.ensure_future(
                bridge._publish_relay_qr(window, 1, QR),
            )
            assert await asyncio.to_thread(entered.wait, 8) is True
            writing.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await writing
            # The close, in the order `stop()` performs it.
            assert bridge._end_pairing_window(
                window, pairing_relay.STATE_FAILED, "the bridge stopped",
            ) is True
            clearing = asyncio.ensure_future(bridge._clear_relay())
            await asyncio.sleep(0.05)
            released.set()
            await clearing
        finally:
            released.set()
        # **Wait for the write thread before asserting**, or the assertion
        # races it and passes by luck against exactly the shape it is here to
        # refuse.
        assert await asyncio.to_thread(finished.wait, 8) is True
        assert not relay_path(bridge).exists()

    async def test_an_in_flight_write_still_lands_before_the_unlink(
        self, bridge, sidecar, monkeypatch,
    ):
        """The lock, from the other side: the unlink is always last.

        This is what the end-to-end ordering does establish — a write holding
        the lock is not skipped and the close waits for it — so the two cases
        together say the credential is gone whichever order they arrive in.
        """
        released = threading.Event()
        real_write = pairing_relay.write_relay

        def blocking_write(path, payload):
            if payload.get("qr"):
                released.wait(timeout=5)
            return real_write(path, payload)

        monkeypatch.setattr(pairing_relay, "write_relay", blocking_write)
        timer = threading.Timer(0.3, released.set)
        timer.start()
        try:
            await bridge.open_pairing_window(USER)
            await sidecar.say(proto.MSG_QR, qr=QR)
            await asyncio.sleep(0.05)
            assert await bridge.cancel_pairing() is True
        finally:
            timer.cancel()
            released.set()
        assert not relay_path(bridge).exists()


class TestTheGuardsWithNoTestBehindThem:
    """Four checks a mutation could delete with the rest of the suite green.

    Each was named by review rather than found by a failure, which is the
    reason they are grouped: the cases above exercise the paths these guards
    sit on without ever putting them in the state they refuse.
    """

    def test_a_hand_written_payload_cannot_smuggle_a_code_past_the_state(
        self, tmp_path,
    ):
        """`build_payload` strips a `qr` from a non-scan state, so no fixture
        built through it can reach the reader's own arm."""
        path = tmp_path / "relay.json"
        path.write_text(
            json.dumps(
                {
                    "window_id": "w1",
                    "state": pairing_relay.STATE_SIDECAR_ABSENT,
                    "expires_at": FUTURE,
                    "qr": QR,
                    "qr_seq": 1,
                    "message": "",
                    "updated_at": time.time(),
                }
            )
        )
        read = pairing_relay.read_relay(path)
        assert read is not None
        assert "qr" not in read

    async def test_a_stopping_bridge_opens_no_window(self, config, sockets):
        path = sockets.path / "relay.json"
        instance = BaileysBridge(
            config,
            socket_path=sockets.socket,
            session_dir=sockets.session,
            pairing_relay_path=path,
        )
        await instance.start()
        await instance.stop()
        assert await instance.open_pairing_window(USER) is None
        assert instance.pairing_window is None
        assert not path.exists()

    async def test_the_first_closer_wins_the_recorded_outcome(self, bridge):
        """A losing closer must not overwrite the outcome the durable row
        reads."""
        window = await bridge.open_pairing_window(USER)
        assert bridge._end_pairing_window(
            window, pairing_relay.STATE_PAIRED, "first",
        ) is True
        assert bridge._end_pairing_window(
            window, pairing_relay.STATE_FAILED, "second",
        ) is False
        assert bridge.last_pairing_outcome.state == pairing_relay.STATE_PAIRED
        assert bridge.last_pairing_outcome.message == "first"

    async def test_the_outcome_is_published_before_the_window_is_dropped(
        self, bridge
    ):
        """The order of those two stores, asserted from inside the gap.

        `poll_pairing_request` reads them the other way round from the
        scheduler's dispatch thread — `pairing_window` first, then
        `last_pairing_outcome` — to tell a close this process performed from a
        window a dead process left behind. These are two separate stores with a
        dataclass construction between them, which the GIL does not fuse, so a
        read landing in the gap under the old order saw no window and no
        matching outcome and recorded a *successful* pairing as `failed`,
        durably and with an admin alert behind it.

        Driven through the `PairingOutcome` constructor, which is what runs
        between the two stores, rather than with a thread — a thread would make
        this a timing test for a property that is an ordering.

        Negative control: swapping the two assignments back turns this red on
        `observed`, which is then `(None, None)`.
        """
        window = await bridge.open_pairing_window(USER)
        observed: list[tuple[object, object]] = []
        real = baileys_bridge_module.PairingOutcome

        def spy(**kwargs):
            observed.append(
                (bridge.pairing_window, bridge.last_pairing_outcome)
            )
            return real(**kwargs)

        baileys_bridge_module.PairingOutcome = spy
        try:
            assert bridge._end_pairing_window(
                window, pairing_relay.STATE_PAIRED, "scanned",
            ) is True
        finally:
            baileys_bridge_module.PairingOutcome = real

        assert len(observed) == 1
        # At the moment the outcome is built, the window is still published —
        # so the dispatch-thread reader's first question answers "live" and it
        # leaves the row alone until the next tick, rather than seeing neither.
        assert observed[0][0] is window
        assert bridge.pairing_window is None
        assert bridge.last_pairing_outcome.state == pairing_relay.STATE_PAIRED

    async def test_settled_relay_jobs_do_not_accumulate(self, bridge, sidecar):
        """Held so the loop cannot collect them mid-flight, discarded after."""
        await bridge.open_pairing_window(USER)
        for seq in range(1, 4):
            await sidecar.say(proto.MSG_QR, qr=f"{QR}{seq}")
            await wait_for(
                lambda seq=seq: (
                    pairing_relay.read_relay(relay_path(bridge)) or {}
                ).get("qr_seq") == seq
            )
        await wait_for(lambda: bridge._relay_tasks == set())


class TestReadyWithoutACode:
    async def test_a_reconnect_inside_a_window_is_not_recorded_as_paired(
        self, bridge, sidecar,
    ):
        """`ready` is re-announced on every daemon-link reconnect.

        So a `ready` with no relayed code behind it is a working session
        saying hello again, not somebody's re-pair completing — and the
        durable row records this state as that request's outcome.
        """
        window = await bridge.open_pairing_window(USER)
        await sidecar.say(proto.MSG_READY)
        await wait_for(lambda: bridge.pairing_window is None)
        await wait_for(lambda: not relay_path(bridge).exists())
        outcome = bridge.last_pairing_outcome
        assert outcome.window_id == window.window_id
        assert outcome.state == pairing_relay.STATE_FAILED
        assert "nothing was re-paired" in outcome.message


class TestTheWatchdog:
    async def test_the_ttl_expires_the_window_and_unlinks(self, bridge, sidecar):
        window = await bridge.open_pairing_window(USER)
        await sidecar.say(proto.MSG_QR, qr=QR)
        await wait_for(lambda: relay_path(bridge).exists())
        await wait_for(lambda: bridge.pairing_window is None, timeout=5.0)
        # The window is dropped synchronously and the unlink follows on the
        # same task, one lock acquisition and one thread hop later.
        await wait_for(lambda: not relay_path(bridge).exists())
        outcome = bridge.last_pairing_outcome
        assert outcome.window_id == window.window_id
        assert outcome.state == pairing_relay.STATE_EXPIRED

    async def test_a_silent_sidecar_is_named_without_closing_the_window(
        self, bridge,
    ):
        window = await bridge.open_pairing_window(USER)
        await wait_for(
            lambda: (pairing_relay.read_relay(relay_path(bridge)) or {}).get("state")
            == pairing_relay.STATE_SIDECAR_ABSENT,
            timeout=3.0,
        )
        assert bridge.pairing_window is window
        read = pairing_relay.read_relay(relay_path(bridge))
        assert "qr" not in read
        assert read["message"]
        assert bridge.status.pairing_state == pairing_relay.STATE_SIDECAR_ABSENT

    async def test_a_connected_but_silent_sidecar_is_not_called_absent(
        self, bridge, sidecar,
    ):
        """The message must not send an operator to a unit that is running.

        `doctor` renders a `sidecar_absent` window as a WARN, so "check its
        own unit and the update log" against a peer this same status object
        reports as connected is a false alarm about a stopped service.
        """
        await bridge.open_pairing_window(USER)
        # Waiting on the **file**, not on `status.pairing_state`: the state
        # flips on the loop and the publish happens on a thread, so the status
        # leads the relay and reading it here caught the previous payload.
        await wait_for(
            lambda: (pairing_relay.read_relay(relay_path(bridge)) or {}).get(
                "state"
            ) == pairing_relay.STATE_SIDECAR_ABSENT,
            timeout=3.0,
        )
        assert bridge.status.connected is True
        message = pairing_relay.read_relay(relay_path(bridge))["message"]
        assert "is connected" in message
        assert "update log" not in message
        assert "sidecar.log" in message

    async def test_nothing_connected_names_the_unit_and_the_update_log(
        self, config, sockets,
    ):
        """The ISSUE-497 residual: a failed dependency install can leave the
        unit stopped with the reason only in the update log."""
        path = sockets.path / "relay.json"
        instance = BaileysBridge(
            config,
            socket_path=sockets.socket,
            session_dir=sockets.session,
            pairing_relay_path=path,
            pairing_window_seconds=3.0,
            sidecar_return_timeout=0.2,
        )
        await instance.start()
        try:
            await instance.open_pairing_window(USER)
            await wait_for(
                lambda: (pairing_relay.read_relay(path) or {}).get("state")
                == pairing_relay.STATE_SIDECAR_ABSENT,
                timeout=3.0,
            )
            assert instance.status.connected is False
            message = pairing_relay.read_relay(path)["message"]
            assert "update log" in message
            assert instance.pairing_window is not None
        finally:
            await instance.stop()

    async def test_a_watchdog_that_cannot_run_closes_the_window(
        self, bridge, sidecar, monkeypatch,
    ):
        """Fail closed, not open.

        Logging and returning would leave the window armed with nothing left
        to enforce its TTL and the code on disk until the process stops — the
        opposite of "unlinked the moment the window closes by any route". The
        realistic trigger is `to_thread` answering `RuntimeError` as the
        default executor shuts down, which is the shutdown case.
        """
        await bridge.open_pairing_window(USER)
        assert relay_path(bridge).exists()

        async def boom(_window):
            raise RuntimeError("cannot schedule new futures after shutdown")

        # Patched after the open, so the file is on disk when the watchdog
        # fails — the demotion at `sidecar_return_timeout` is what reaches it.
        monkeypatch.setattr(bridge, "_publish_relay", boom)
        await wait_for(lambda: bridge.pairing_window is None, timeout=3.0)
        await wait_for(lambda: not relay_path(bridge).exists())
        assert bridge.last_pairing_outcome.state == pairing_relay.STATE_FAILED
        assert "watchdog" in bridge.last_pairing_outcome.message

    async def test_a_late_sidecar_still_pairs(self, config, sockets):
        """The demotion is not a close: a code arriving after it still pairs.

        **Its own instance, with a window far longer than the demotion
        deadline, and that gap is what keeps the test honest rather than
        merely green.** The shared `bridge` fixture pairs a 1.5s window with
        this test's own 3.0s wait for the demotion, so on a loaded host the
        window expires before the QR is sent, `_handle_qr` correctly takes its
        no-window path, and `awaiting_scan` never arrives — the test goes red
        over its own timing rather than over the behaviour it names (observed
        once in a 27588-test run, and green six times beside it). The TTL is
        incidental here: what is asserted is that `sidecar_absent` is reached
        and that a later code lifts it, so the window is given room to outlast
        the wait and the short `sidecar_return_timeout` is what the demotion
        still races.
        """
        instance = BaileysBridge(
            config,
            socket_path=sockets.socket,
            session_dir=sockets.session,
            pairing_relay_path=sockets.path / "relay.json",
            pairing_window_seconds=30.0,
            sidecar_return_timeout=0.2,
        )
        await instance.start()
        try:
            fake = FakeSidecar(sockets.socket)
            await fake.connect()
            await wait_for(lambda: instance.status.connected is True)
            await instance.open_pairing_window(USER)
            await wait_for(
                lambda: instance.status.pairing_state
                == pairing_relay.STATE_SIDECAR_ABSENT,
                timeout=3.0,
            )
            await fake.say(proto.MSG_QR, qr=QR)
            # **Wait on the file, not on the state**, which is this file's
            # established idiom (`test_it_publishes_0600_with_the_window_id`)
            # and the reason is a real ordering rather than a style: the state
            # is stamped on the read loop synchronously and the relay write is
            # queued to a worker thread, so `awaiting_scan` is observable
            # before the payload has landed. Asserting the file off the state
            # is a `KeyError: 'qr'` about one run in sixteen.
            await wait_for(
                lambda: (
                    pairing_relay.read_relay(relay_path(instance)) or {}
                ).get("qr")
            )
            read = pairing_relay.read_relay(relay_path(instance))
            assert read["state"] == pairing_relay.STATE_AWAITING_SCAN
            assert read["qr"] == QR
            await fake.close()
        finally:
            await instance.stop()

    async def test_a_qr_already_in_hand_is_not_demoted(self, config, sockets):
        """The demotion applies only to a window still awaiting a sidecar."""
        instance = BaileysBridge(
            config,
            socket_path=sockets.socket,
            session_dir=sockets.session,
            pairing_relay_path=sockets.path / "relay.json",
            pairing_window_seconds=3.0,
            sidecar_return_timeout=0.3,
        )
        await instance.start()
        try:
            fake = FakeSidecar(sockets.socket)
            await fake.connect()
            await wait_for(lambda: instance.status.connected is True)
            await instance.open_pairing_window(USER)
            await fake.say(proto.MSG_QR, qr=QR)
            await wait_for(
                lambda: instance.status.pairing_state
                == pairing_relay.STATE_AWAITING_SCAN
            )
            await asyncio.sleep(0.5)
            assert (
                instance.status.pairing_state == pairing_relay.STATE_AWAITING_SCAN
            )
            await fake.close()
        finally:
            await instance.stop()


class TestNothingTouchesTheDiskOnTheReadLoop:
    """The loop dispatches and never works.

    An inbound event that blocks it deadlocks the link, and the same loop
    carries the `send_result` frames the outbound ledger waits on inside its
    claim-to-settle region.
    """

    async def test_the_loop_keeps_servicing_frames_while_a_write_blocks(
        self, bridge, sidecar, monkeypatch,
    ):
        """Asserted as **elapsed time**, and that is the whole test.

        The obvious form — block the write, then poll until the next frame is
        counted — cannot fail, and the control proved it: a write performed on
        the loop thread blocks the test coroutine too, so the poll makes no
        progress until the block releases and then observes the frame and
        returns green. Nothing in it can measure a stall from inside the stall.

        So the release comes from another thread on a timer, and the
        measurement is how long the loop took to count a frame that arrived
        behind the write. Off the loop that is immediate; on the loop it cannot
        be anything less than the timer.
        """
        block_for = 3.0
        budget = 1.0
        released = threading.Event()
        real_write = pairing_relay.write_relay

        def blocking_write(path, payload):
            if payload.get("qr"):
                released.wait(timeout=5)
            return real_write(path, payload)

        monkeypatch.setattr(pairing_relay, "write_relay", blocking_write)
        timer = threading.Timer(block_for, released.set)
        timer.start()
        try:
            await bridge.open_pairing_window(USER)
            before = bridge.status.malformed_lines
            started = time.monotonic()
            await sidecar.say(proto.MSG_QR, qr=QR)
            # One frame behind it that the loop counts without leaving itself.
            await sidecar.write_raw(b"{not json}\n")
            await wait_for(
                lambda: bridge.status.malformed_lines == before + 1,
                timeout=5.0,
            )
            elapsed = time.monotonic() - started
        finally:
            timer.cancel()
            released.set()
        assert elapsed < budget, (
            f"the read loop took {elapsed:.2f}s to service a frame queued "
            f"behind a relay write that blocked for {block_for}s"
        )
        assert bridge.pairing_window is not None

    async def test_the_loop_keeps_servicing_frames_while_an_unlink_blocks(
        self, bridge, sidecar, monkeypatch,
    ):
        """The unlink is the other half of the stated property.

        It is reached from the `ready` arm, on the read loop, so a regression
        to a direct `clear_relay` call there would be invisible to the write
        case above — the relay's own docstring says both touches happen on a
        thread, and only one of them was measured.
        """
        block_for = 3.0
        budget = 1.0
        released = threading.Event()
        real_clear = pairing_relay.clear_relay

        def blocking_clear(path):
            released.wait(timeout=8)
            return real_clear(path)

        monkeypatch.setattr(pairing_relay, "clear_relay", blocking_clear)
        timer = threading.Timer(block_for, released.set)
        timer.start()
        try:
            await bridge.open_pairing_window(USER)
            await sidecar.say(proto.MSG_QR, qr=QR)
            await wait_for(lambda: bridge.status.pairing_state == (
                pairing_relay.STATE_AWAITING_SCAN
            ))
            before = bridge.status.malformed_lines
            started = time.monotonic()
            await sidecar.say(proto.MSG_READY)
            await sidecar.write_raw(b"{not json}\n")
            await wait_for(
                lambda: bridge.status.malformed_lines == before + 1,
                timeout=8.0,
            )
            elapsed = time.monotonic() - started
        finally:
            timer.cancel()
            released.set()
        assert elapsed < budget, (
            f"the read loop took {elapsed:.2f}s to service a frame queued "
            f"behind a relay unlink that blocked for {block_for}s"
        )

    async def test_the_loop_keeps_servicing_frames_while_the_parent_mkdir_blocks(
        self, config, sockets, monkeypatch,
    ):
        """`_ensure_relay_parent` is a `mkdir`, and a hung filesystem is where
        a `mkdir` costs what a write costs."""
        block_for = 3.0
        budget = 1.0
        released = threading.Event()
        real_mkdir = Path.mkdir

        def blocking_mkdir(self, *args, **kwargs):
            if self == sockets.path:
                released.wait(timeout=8)
            return real_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(Path, "mkdir", blocking_mkdir)
        instance = BaileysBridge(
            config,
            socket_path=sockets.socket,
            session_dir=sockets.session,
            pairing_relay_path=sockets.path / "relay.json",
            pairing_window_seconds=8.0,
            sidecar_return_timeout=8.0,
        )
        monkeypatch.undo()
        await instance.start()
        monkeypatch.setattr(Path, "mkdir", blocking_mkdir)
        timer = threading.Timer(block_for, released.set)
        timer.start()
        try:
            fake = FakeSidecar(sockets.socket)
            await fake.connect()
            await wait_for(lambda: instance.status.connected is True)
            before = instance.status.malformed_lines
            started = time.monotonic()
            opening = asyncio.ensure_future(instance.open_pairing_window(USER))
            await fake.write_raw(b"{not json}\n")
            await wait_for(
                lambda: instance.status.malformed_lines == before + 1,
                timeout=8.0,
            )
            elapsed = time.monotonic() - started
            timer.cancel()
            released.set()
            await opening
            await fake.close()
        finally:
            timer.cancel()
            released.set()
            monkeypatch.undo()
            await instance.stop()
        assert elapsed < budget, (
            f"the read loop took {elapsed:.2f}s to service a frame queued "
            f"behind a relay mkdir that blocked for {block_for}s"
        )


# ---------------------------------------------------------------------------
# The drift guard
# ---------------------------------------------------------------------------


class TestTheStatusCarriesNoPayload:
    """`read_status` feeds `doctor`, which renders into the boot log and the
    admin dashboard.

    Asserted over the dataclass's **field names** rather than over an
    instance's values, per the rule that a value-based check misses a rename.
    """

    def test_no_field_could_hold_a_qr(self):
        names = set(BridgeStatus.__dataclass_fields__)
        assert "pairing_state" in names
        assert "pairing_expires_at" in names
        for name in names:
            assert "qr" not in name.lower()
            assert "payload" not in name.lower()
            assert "code" not in name.lower()
