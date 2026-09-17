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
import json
import logging
import os
import threading
import time
from pathlib import Path

import pytest

from istota.config import Config, UserConfig
from istota.transport.whatsapp import baileys_protocol as proto
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
            expires_at=1789000000.0,
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
            expires_at=1789000000.0,
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
                expires_at=1789000000.0,
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
            expires_at=1789000000.0,
            qr=QR,
        )
        with caplog.at_level(logging.DEBUG):
            assert pairing_relay.write_relay(path, payload) is False
        assert mentions_payload(caplog) == []
        joined = " ".join(record.getMessage() for record in caplog.records)
        assert "missing-dir" in joined
        assert "ENOENT" in joined or "errno=2" in joined

    def test_two_concurrent_writes_both_complete(self, tmp_path):
        """A fixed staging name fails the second with `FileNotFoundError`.

        The defect `atomic_write.py`'s docstring records, and the relay is a
        re-entered writer with a watchdog task beside it — the exact shape that
        bit `health/documents.py`.
        """
        path = tmp_path / "relay.json"
        start = threading.Barrier(2)
        results: list[bool] = []
        lock = threading.Lock()

        def writer(seq: int) -> None:
            payload = pairing_relay.build_payload(
                window_id="w1",
                state=pairing_relay.STATE_AWAITING_SCAN,
                expires_at=1789000000.0,
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
                expires_at=1789000000.0,
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
                        expires_at=1789000000.0,
                        qr=QR * 40,
                        qr_seq=seq,
                    ),
                )
        finally:
            stop.set()
            thread.join(timeout=5)
        assert torn == []

    def test_clearing_is_idempotent(self, tmp_path):
        path = tmp_path / "relay.json"
        pairing_relay.write_relay(
            path,
            pairing_relay.build_payload(
                window_id="w1",
                state=pairing_relay.STATE_AWAITING_SIDECAR,
                expires_at=1789000000.0,
            ),
        )
        assert pairing_relay.clear_relay(path) is True
        assert not path.exists()
        assert pairing_relay.clear_relay(path) is True

    def test_the_leaf_reaches_only_the_neighbouring_atomic_write(self):
        """The reader runs in the web process and must not pull in the bridge."""
        source = Path(pairing_relay.__file__).read_text()
        package_imports = [
            line.strip()
            for line in source.splitlines()
            if line.startswith(("from .", "import istota", "from istota"))
        ]
        assert package_imports == [
            "from ...atomic_write import write_bytes_atomic",
        ]


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
        # The watchdog closes it on its own; drive the expiry by hand so the
        # handler is reached with a window object that is past its deadline.
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

    async def test_a_late_sidecar_still_pairs(self, bridge, sidecar):
        await bridge.open_pairing_window(USER)
        await wait_for(
            lambda: bridge.status.pairing_state
            == pairing_relay.STATE_SIDECAR_ABSENT,
            timeout=3.0,
        )
        await sidecar.say(proto.MSG_QR, qr=QR)
        await wait_for(
            lambda: bridge.status.pairing_state == pairing_relay.STATE_AWAITING_SCAN
        )
        read = pairing_relay.read_relay(relay_path(bridge))
        assert read["qr"] == QR

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
        block_for = 0.6
        budget = 0.3
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
