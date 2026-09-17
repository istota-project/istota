"""`repair_session`: re-pairing from a running bridge, against a fake sidecar.

Every property here is about **evidence**. There is no advisory lock on the
session directory, so nothing can ask whether a Baileys client still holds it;
the sequence causes the answer instead, by asking a connected sidecar to stop
and moving the directory only after the link drop its own frame produced. So
the assertions are orderings over what crossed the socket, not outcomes:

**A drop the frame did not cause is not evidence**, and the case that says so
drives a sidecar that leaves without being asked. `index.js` starts the Baileys
session without waiting for the daemon link and retries the link on its own
timer, so a live sidecar holds the directory for a window at every boot while
`connected` reads false — renaming there leaves the survivor writing the old
dead credential into the freshly created directory, which is a window that
waits out its TTL for a code that cannot come.

**An abort leaves the original directory holding its original file.** Asserting
that the call returned false would pass against an implementation that moved
the credential and then reported a failure, which is the one outcome an
operator cannot undo from a status line.

**One rename implementation, reached by both entry points.** A second copy of a
credential-moving path passes its own tests and drifts the first time either is
fixed, so the sharing is asserted by behaviour rather than read.

**A send inside an open window settles `failed` in `sent_whatsapp`.** The
sequence clears the permanent-fatal latch — `resume()`'s fresh supervisor reads
it at its first statement, so it has to — and `logical_key` is UNIQUE with
nothing deleting from that table, so an `unknown` row there is a task result, a
confirmation prompt or an admin alert that can never be sent. The assertion is
on the row, not on the refusal.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
from pathlib import Path

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.transport.whatsapp import baileys_protocol as proto, outbound
from istota.transport.whatsapp import baileys_bridge as bridge_module
from istota.transport.whatsapp import pairing_relay
from istota.transport.whatsapp.baileys_bridge import (
    PAIRING_ALREADY,
    PAIRING_COOLDOWN,
    PAIRING_OK,
    PAIRING_SESSION_DIR_UNUSABLE,
    PAIRING_SESSION_LIVE,
    PAIRING_SHAPE_UNSUPPORTED,
    PAIRING_SIDECAR_ABSENT,
    PAIRING_STOP_TIMEOUT,
    BaileysBridge,
)
from istota.transport.whatsapp.providers._types import (
    WhatsAppProviderAdapter,
    WhatsAppProviderCaps,
)

from .support.baileys_sidecar import FakeSidecar, SocketDir, mode_of, wait_for
from .support.whatsapp_config import build_whatsapp_config

USER = "alice"
USER_NUMBER = "+15551234567"
USER_JID = "15551234567@s.whatsapp.net"
BAILEYS = db.WHATSAPP_BAILEYS_PROVIDER

#: The argv the spawned shape is given: a child that stays alive until it is
#: reaped, so the supervisor's own state is what the tests drive rather than a
#: crash loop.
IDLE_CHILD = ("/bin/sh", "-c", "sleep 30")

#: What lives in the session directory, so an abort can be asserted to have
#: left it there and a move can be asserted to have carried it.
CREDS = '{"me":"the paired device"}'

BAILEYS_CAPS = WhatsAppProviderCaps(
    metered=False, has_service_window=False,
    supports_templates=False, delivery_receipts=True,
    address_field="jid", service_body_limit=4096, interactive_body_limit=4096,
)


@pytest.fixture
def sockets():
    directory = SocketDir()
    yield directory
    directory.cleanup()


@pytest.fixture
def config(tmp_path) -> Config:
    path = tmp_path / "istota.db"
    db.init_db(path)
    return Config(
        db_path=path,
        temp_dir=tmp_path / "tmp",
        whatsapp=build_whatsapp_config(
            enabled=True, provider=BAILEYS,
            business_phone_number="+15551230000",
        ),
        users={USER: UserConfig()},
    )


def make_bridge(config, sockets, **kwargs) -> BaileysBridge:
    """A bridge with every pairing bound short enough to be reachable.

    The two waits are 60s and 15s in production, so a test that took them as
    written would either hang or assert nothing. The relay path is explicit
    rather than derived, because `default_pairing_relay_path` reads the
    config's workspace and this file is about the sequence, not about where the
    file lands.
    """
    kwargs.setdefault("send_timeout", 2.0)
    kwargs.setdefault("sidecar_return_timeout", 0.3)
    kwargs.setdefault("sidecar_stop_timeout", 0.3)
    kwargs.setdefault("pairing_window_seconds", 30.0)
    kwargs.setdefault("pairing_relay_path", sockets.path / "whatsapp-pairing.json")
    return BaileysBridge(
        config, socket_path=sockets.socket, session_dir=sockets.session, **kwargs,
    )


@contextlib.asynccontextmanager
async def running(config, sockets, **kwargs):
    instance = make_bridge(config, sockets, **kwargs)
    await instance.start()
    (sockets.session / "creds.json").write_text(CREDS)
    try:
        yield instance
    finally:
        await instance.stop()


class Sidecar:
    """One fake sidecar plus a background reader, so frames are timestamped.

    `FakeSidecar.received` only fills when a test reads a frame by hand, which
    is no use for an ordering assertion about a frame nobody was waiting for.
    This reads continuously and stamps each one against the loop clock.

    `closes_on_shutdown` is the shipped sidecar's own behaviour —
    `session.stop().finally(() => process.exit(0))`, with no cancel — and it is
    a switch because a sidecar that ignores the frame is one of the cases.
    """

    def __init__(self, fake: FakeSidecar, *, closes_on_shutdown: bool) -> None:
        self._fake = fake
        self._closes = closes_on_shutdown
        self.frames: list[tuple[float, dict]] = []
        self.closed_at: float | None = None
        self._reader: asyncio.Task | None = None

    async def _read(self) -> None:
        while True:
            try:
                payload = await self._fake.next_message(timeout=60.0)
            except (asyncio.CancelledError, GeneratorExit):
                raise
            except Exception:
                return
            self.frames.append((asyncio.get_running_loop().time(), payload))
            if self._closes and payload.get("type") == proto.MSG_SHUTDOWN:
                self.closed_at = asyncio.get_running_loop().time()
                await self._fake.drop()
                return

    def start(self) -> None:
        self._reader = asyncio.ensure_future(self._read())

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._reader
            self._reader = None

    @property
    def types(self) -> list[str]:
        return [payload["type"] for _, payload in self.frames]

    @property
    def shutdowns(self) -> int:
        return self.types.count(proto.MSG_SHUTDOWN)

    def first_at(self, message_type: str) -> float | None:
        for at, payload in self.frames:
            if payload["type"] == message_type:
                return at
        return None

    async def drop(self) -> None:
        await self._fake.drop()

    async def say(self, message_type: str, /, **fields) -> None:
        await self._fake.say(message_type, **fields)


@contextlib.asynccontextmanager
async def sidecar(instance, sockets, *, closes_on_shutdown: bool = True):
    """A sidecar the bridge has actually adopted.

    `open_unix_connection` returns when the kernel completes the connect, which
    is before `_on_connect` has negotiated and taken the writer — so a test
    that calls `repair_session` immediately is racing the bridge's own
    `_writer` assignment and would see the sequence wait rather than send.
    """
    fake = FakeSidecar(sockets.socket)
    await fake.connect()
    await wait_for(lambda: instance.status.connected is True)
    peer = Sidecar(fake, closes_on_shutdown=closes_on_shutdown)
    peer.start()
    try:
        yield peer
    finally:
        await peer.stop()
        await fake.close()


async def latch_fatal(instance, peer) -> None:
    """Put the bridge in the state a logged-out session leaves it in."""
    await peer.say(proto.MSG_FATAL, reason="logged_out")
    await wait_for(lambda: instance.status.fatal_is_permanent is True)


def archives(sockets) -> list[Path]:
    """Every `.old-<stamp>` sibling of the session directory."""
    return sorted(
        path
        for path in sockets.session.parent.iterdir()
        if path.is_dir() and path.name.startswith(sockets.session.name + ".")
    )


def record_renames(monkeypatch) -> list[float]:
    """When `os.rename` ran, on the loop clock.

    The same instrument `TestTheSessionReset` uses to put its single-writer
    probe on the rename itself: asserting after the call returns would pass
    against an implementation that moved first and settled second.
    """
    stamps: list[float] = []
    real = os.rename

    def recording(src, dst, *args, **kwargs):
        stamps.append(asyncio.get_running_loop().time())
        return real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "rename", recording)
    return stamps


def bind_user(config: Config) -> None:
    with db.get_db(config.db_path) as conn:
        db.set_whatsapp_binding(
            conn, USER, bootstrap_phone_number=USER_NUMBER,
        )
        db.latch_whatsapp_jid(conn, USER, jid=USER_JID)


def use_bridge_as_adapter(monkeypatch, instance) -> None:
    adapter = WhatsAppProviderAdapter(
        name="baileys", caps=BAILEYS_CAPS, parse_webhook=None,
        send=instance.send, verify_signature=None,
    )
    monkeypatch.setattr(outbound, "active_adapter", lambda config: adapter)


def ledger_row(config: Config, logical_key: str) -> sqlite3.Row:
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT * FROM sent_whatsapp WHERE logical_key = ?", (logical_key,),
        ).fetchone()


# ---------------------------------------------------------------------------
# The evidence
# ---------------------------------------------------------------------------


class TestTheFrameIsTheEvidence:
    """The rename follows a drop the daemon caused, and nothing else."""

    async def test_the_archive_appears_only_after_the_frame_and_the_drop(
        self, config, sockets, monkeypatch,
    ):
        """The ordering the whole design turns on, asserted over the socket.

        Three stamps, and each has to be behind the next: the `shutdown` frame
        reaching the sidecar, the close that frame caused, and the rename. An
        assertion that merely required the archive to exist would pass against
        a sequence that renamed first and asked afterwards.
        """
        renames = record_renames(monkeypatch)
        async with running(config, sockets) as instance:
            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                result = await instance.repair_session(USER)

            assert result.ok is True, result.message
            assert result.reason == PAIRING_OK
            assert result.restart_spent is True

            frame_at = peer.first_at(proto.MSG_SHUTDOWN)
            assert frame_at is not None, peer.types
            assert peer.closed_at is not None
            assert peer.closed_at >= frame_at
            assert renames, "the session directory was never moved"
            assert renames[0] >= peer.closed_at

            moved = result.moved_to
            assert moved is not None
            assert (moved / "creds.json").read_text() == CREDS
            assert sockets.session.is_dir()
            assert list(sockets.session.iterdir()) == []
            assert mode_of(sockets.session) == 0o700

    async def test_a_drop_the_frame_did_not_cause_is_not_evidence(
        self, config, sockets, monkeypatch,
    ):
        """The negative control, and it is the whole design.

        One sidecar connects and leaves without being asked; a second connects
        and ignores `shutdown`. Nothing may move: the first drop says nothing
        about who holds the directory now, and the second sidecar is still
        holding it.

        **The control is removing *both* `_link_dropped.clear()` calls** — the
        one in `_on_connect` and the one in `_repair_by_frame` — which turns
        the latch into "has any disconnect ever happened", i.e. the passive
        draft. Removing either alone turns nothing red, measured: each carries
        the property on its own, so the pair is defence in depth and a control
        naming one of them would be a control that could not fail.

        With both gone this case goes red on the reason, because the
        returned-sidecar check after the wait still refuses. Removing that as
        well is what turns the archive and marker assertions red, which is the
        corruption itself: `ok=True` with the credential moved out from under a
        sidecar that is connected at that moment.

        It also asserts the frame reached the *second* sidecar, so the
        neighbouring mutation of skipping the frame turns it red for its own
        reason rather than passing on this one's assertion.
        """
        renames = record_renames(monkeypatch)
        async with running(config, sockets) as instance:
            async with sidecar(instance, sockets) as first:
                await latch_fatal(instance, first)
                await first.drop()
            await wait_for(lambda: instance.status.connected is False)

            async with sidecar(
                instance, sockets, closes_on_shutdown=False,
            ) as second:
                result = await instance.repair_session(USER)

                assert result.ok is False
                assert result.reason == PAIRING_STOP_TIMEOUT
                assert second.shutdowns == 1, second.types

            assert renames == []
            assert archives(sockets) == []
            assert (sockets.session / "creds.json").read_text() == CREDS

    async def test_a_sidecar_that_ignores_the_frame_keeps_its_directory(
        self, config, sockets, monkeypatch,
    ):
        """Past the bound the directory is left alone, and the message says
        the restart is spent anyway.

        A sidecar slow past `sidecar_stop_timeout` still exits, so there is no
        version of this abort that costs nothing — which is why the
        confirmation copy promises the restart up front. The assertion is that
        the original directory still holds its original file, not that the
        call returned false: an implementation that moved the credential and
        then reported a failure satisfies the second and is the one outcome an
        operator cannot undo.
        """
        renames = record_renames(monkeypatch)
        async with running(config, sockets) as instance:
            async with sidecar(
                instance, sockets, closes_on_shutdown=False,
            ) as peer:
                await latch_fatal(instance, peer)
                result = await instance.repair_session(USER)

                assert result.reason == PAIRING_STOP_TIMEOUT
                assert result.restart_spent is True
                assert result.moved_to is None
                assert "restart is spent" in result.message
                # **The cooldown is stamped at the write, not at success**, so
                # it is already running: this abort cost a reconnect just as
                # surely as a re-pair that finished, and the churn is what the
                # cooldown rations. Stamping on success instead turns this
                # assertion red, and no other case reaches it.
                again = await instance.repair_session(USER)
                assert again.reason == PAIRING_COOLDOWN
                assert peer.shutdowns == 1, peer.types

            assert renames == []
            assert (sockets.session / "creds.json").read_text() == CREDS
            assert instance.pairing_window is None

    async def test_the_frame_goes_to_a_sidecar_that_arrives_late(
        self, config, sockets,
    ):
        """Waiting is for a peer to cause the drop *on*, never for a gap.

        Skipping the frame because nothing was connected is the passive path
        by another name, so the arriving sidecar has to receive it.
        """
        async with running(config, sockets) as instance:
            async with sidecar(instance, sockets) as first:
                await latch_fatal(instance, first)
                await first.drop()
            await wait_for(lambda: instance.status.connected is False)

            pending = asyncio.ensure_future(
                instance.repair_session(USER, force=True),
            )
            await asyncio.sleep(0.05)
            async with sidecar(instance, sockets) as second:
                result = await asyncio.wait_for(pending, timeout=5.0)

            assert result.ok is True, result.message
            assert second.shutdowns == 1, second.types
            assert result.moved_to is not None

    async def test_nothing_connecting_costs_nothing(self, config, sockets):
        """The one abort in the flow that really is a clean no-op.

        This is ISSUE-497's residual reached before anything is spent: a
        failing `npm ci` in the update cron stops the unit and aborts before
        either restart arm, so the sidecar can be left down with the reason
        only in the update log. No frame is written, nothing moves, and the
        result says the restart was not spent.
        """
        async with running(config, sockets) as instance:
            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                await peer.drop()
                await wait_for(lambda: instance.status.connected is False)
                before = peer.shutdowns

                result = await instance.repair_session(USER)

                assert peer.shutdowns == before
            assert result.reason == PAIRING_SIDECAR_ABSENT
            assert result.restart_spent is False
            assert result.moved_to is None
            assert archives(sockets) == []
            assert (sockets.session / "creds.json").read_text() == CREDS
            assert instance.pairing_window is None


# ---------------------------------------------------------------------------
# The refusals in front of the frame
# ---------------------------------------------------------------------------


class TestTheRefusalsBeforeAnythingIsSpent:
    """Nothing that can refuse on what it already knows runs after the write."""

    async def test_a_working_session_is_refused_without_force(
        self, config, sockets,
    ):
        """`force` is a confirmation, not a mechanism switch — so the default
        cannot disconnect a link that may be working or about to come back."""
        async with running(config, sockets) as instance:
            async with sidecar(instance, sockets) as peer:
                result = await instance.repair_session(USER)

                assert result.ok is False
                assert result.reason == PAIRING_SESSION_LIVE
                assert peer.shutdowns == 0, peer.types
            assert (sockets.session / "creds.json").read_text() == CREDS

    async def test_a_working_session_proceeds_with_force(self, config, sockets):
        """The other half. One mechanism, two confirmations: the same frame is
        sent, and what differs is whether the caller was asked."""
        async with running(config, sockets) as instance:
            async with sidecar(instance, sockets) as peer:
                result = await instance.repair_session(USER, force=True)

                assert result.ok is True, result.message
                assert peer.shutdowns == 1, peer.types
            assert result.moved_to is not None

    async def test_an_unusable_session_directory_aborts_before_the_frame(
        self, config, sockets, monkeypatch,
    ):
        """Validate first. `ensure_session_dir` refuses a directory owned by
        another uid and a non-directory at the name, and hearing that after
        the sidecar is down is a restart spent on a sequence that was never
        going to finish."""
        async with running(config, sockets) as instance:
            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                monkeypatch.setattr(
                    bridge_module,
                    "ensure_session_dir",
                    lambda path: (_ for _ in ()).throw(
                        PermissionError("owned by another uid"),
                    ),
                )
                result = await instance.repair_session(USER)

                assert result.reason == PAIRING_SESSION_DIR_UNUSABLE
                assert result.restart_spent is False
                assert peer.shutdowns == 0, peer.types
            assert (sockets.session / "creds.json").read_text() == CREDS

    async def test_a_second_concurrent_request_is_refused(
        self, config, sockets,
    ):
        """Single-flight, and it is not the same question as "a window is
        open": the window opens at the last step, so between the first call's
        arrival and that point the whole destructive half would otherwise be
        re-entrant."""
        async with running(config, sockets) as instance:
            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                await peer.drop()
                await wait_for(lambda: instance.status.connected is False)
                before = peer.shutdowns

                first = asyncio.ensure_future(instance.repair_session(USER))
                await asyncio.sleep(0.02)
                second = await instance.repair_session("bob")

                assert second.ok is False
                assert second.reason == PAIRING_ALREADY
                assert peer.shutdowns == before

                outcome = await asyncio.wait_for(first, timeout=5.0)
                assert outcome.reason == PAIRING_SIDECAR_ABSENT

    async def test_an_open_window_refuses_a_second_repair(
        self, config, sockets,
    ):
        """The other single-flight guard, which is the one the route's 409
        reads. Asserted apart from the in-flight flag because they cover
        different halves of the sequence."""
        async with running(config, sockets) as instance:
            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                first = await instance.repair_session(USER)
                assert first.ok is True, first.message

            second = await instance.repair_session("bob")
            assert second.reason == PAIRING_ALREADY
            assert second.window_id == first.window_id

    async def test_a_second_repair_inside_the_cooldown_is_refused(
        self, config, sockets,
    ):
        """`RESET_COOLDOWN` stands in for `reset_session`'s once-per-bridge
        flag, which is right for a one-shot CLI process and far too tight for
        a bridge that lives as long as the daemon.

        **The window is cancelled first, and without that this case asserts
        nothing**: with window #1 still open the `already_pairing` guard
        answers, so removing the cooldown — the named negative control — would
        leave it green. It asserts the reason string for the same reason the
        seven `SessionResetRefused` sites are asserted with `match=`.
        """
        async with running(config, sockets) as instance:
            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                assert (await instance.repair_session(USER)).ok is True
            assert await instance.cancel_pairing() is True

            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                result = await instance.repair_session(USER)

                assert result.ok is False
                assert result.reason == PAIRING_COOLDOWN
                assert peer.shutdowns == 0, peer.types

    async def test_without_the_cooldown_a_second_repair_goes_through(
        self, config, sockets,
    ):
        """The control for the case above, run in the product rather than as a
        mutation: with the cooldown at zero the second re-pair is served, which
        is the loop `reset_session`'s flag exists to prevent and this
        deployment's bridge cannot use that flag for."""
        async with running(config, sockets, reset_cooldown=0.0) as instance:
            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                assert (await instance.repair_session(USER)).ok is True
            assert await instance.cancel_pairing() is True

            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                result = await instance.repair_session(USER)

                assert result.ok is True, result.message
                assert peer.shutdowns == 1, peer.types


# ---------------------------------------------------------------------------
# The two shapes
# ---------------------------------------------------------------------------


class TestTheTwoShapes:
    """Which evidence is available decides which path runs."""

    async def test_the_spawned_shape_delegates_rather_than_sending_the_frame(
        self, config, sockets, monkeypatch,
    ):
        """`_supervise` returns its respawn delay to `RESPAWN_BASE_SECONDS`
        for any child that ran a while, so the sidecar this sequence asks to
        exit is back about a second later — into the directory the rename is
        about to move. `reset_session` settles with the supervisor instead, by
        awaiting the task and refusing if it outlived the wait.

        **The frame count is snapshotted after the fatal**, because
        `_supervise`'s own permanent-fatal arm sends a `shutdown` before
        `repair_session` is ever called: asserting zero would fail on a frame
        this sequence did not send.
        """
        monkeypatch.setattr(bridge_module, "RESPAWN_BASE_SECONDS", 0.001)
        entered: list[str] = []
        async with running(
            config, sockets, sidecar_argv=IDLE_CHILD,
        ) as instance:
            real_reset = instance.reset_session

            async def watched():
                entered.append("reset_session")
                return await real_reset()

            monkeypatch.setattr(instance, "reset_session", watched)

            async with sidecar(
                instance, sockets, closes_on_shutdown=False,
            ) as peer:
                await latch_fatal(instance, peer)
                await wait_for(lambda: instance._supervisor.done() is True, timeout=15.0)
                before = peer.shutdowns

                result = await instance.repair_session(USER)

                assert result.ok is True, result.message
                assert entered == ["reset_session"]
                assert peer.shutdowns == before, peer.types
            assert result.moved_to is not None
            assert (result.moved_to / "creds.json").read_text() == CREDS

    async def test_the_spawned_shape_refuses_a_forced_repair(
        self, config, sockets,
    ):
        """The cost of delegating, said out loud rather than left as a silent
        refusal: `reset_session` requires a permanent fault, so a forced
        re-pair of a working session is not available on the shape the
        developer checkout runs, which `istota whatsapp pair` already owns."""
        async with running(
            config, sockets, sidecar_argv=IDLE_CHILD,
        ) as instance:
            async with sidecar(instance, sockets) as peer:
                result = await instance.repair_session(USER, force=True)

                assert result.ok is False
                assert result.reason == PAIRING_SHAPE_UNSUPPORTED
                assert "istota whatsapp pair --reset" in result.message
                assert peer.shutdowns == 0, peer.types

    async def test_the_spawned_shape_runs_a_sidecar_again(
        self, config, sockets, monkeypatch,
    ):
        """**A spawn, not a flag.** Both permanent-fatal arms in `_supervise`
        `return`, and clearing the latch does not resurrect a coroutine that
        has already ended — so a case asserting `fatal_is_permanent is False`
        passes while the deployment stays dead for ever. This is the resume
        `_supervise`'s docstring recorded as owed.
        """
        monkeypatch.setattr(bridge_module, "RESPAWN_BASE_SECONDS", 0.001)
        async with running(
            config, sockets, sidecar_argv=IDLE_CHILD,
        ) as instance:
            await wait_for(lambda: instance._process is not None, timeout=5.0)
            first_child = instance._process
            assert first_child is not None

            async with sidecar(
                instance, sockets, closes_on_shutdown=False,
            ) as peer:
                await latch_fatal(instance, peer)
                await wait_for(lambda: instance._supervisor.done() is True, timeout=15.0)
                assert instance._process is None

                result = await instance.repair_session(USER)

            assert result.ok is True, result.message
            await wait_for(lambda: instance._process is not None, timeout=5.0)
            assert instance._process is not first_child
            assert instance._process.returncode is None
            assert instance.status.fatal_is_permanent is False

    async def test_the_external_unit_shape_spawns_nothing(
        self, config, sockets,
    ):
        """On `sidecar_argv=()` there is no supervisor to resume and the unit's
        own `Restart=always` has already scheduled the start. The latch is
        cleared, which is what `_handle_qr`'s window branch and the resumed
        sidecar both need, and no child is created."""
        async with running(config, sockets) as instance:
            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                result = await instance.repair_session(USER)

            assert result.ok is True, result.message
            assert instance.resume() is False
            assert instance._supervisor is None
            assert instance._process is None
            assert instance.status.fatal_is_permanent is False
            assert instance.status.fatal_reason is None


# ---------------------------------------------------------------------------
# One rename implementation
# ---------------------------------------------------------------------------


class TestOneRenameImplementation:
    """A second copy of a credential-moving path is the expensive kind of
    duplication: cheap to write, passing its own tests, and drifting the first
    time either one is fixed. Asserted by behaviour, because reading the two
    is exactly what would not notice."""

    async def test_repair_session_reaches_the_shared_core(
        self, config, sockets, monkeypatch,
    ):
        async with running(config, sockets) as instance:
            reached = wrap_move_aside(monkeypatch, instance)
            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                result = await instance.repair_session(USER)

            assert result.ok is True, result.message
            assert reached == [True]

    async def test_reset_session_reaches_the_shared_core(
        self, config, sockets, monkeypatch,
    ):
        monkeypatch.setattr(bridge_module, "RESPAWN_BASE_SECONDS", 0.001)
        async with running(
            config, sockets, sidecar_argv=IDLE_CHILD,
        ) as instance:
            reached = wrap_move_aside(monkeypatch, instance)
            async with sidecar(
                instance, sockets, closes_on_shutdown=False,
            ) as peer:
                await latch_fatal(instance, peer)
                moved = await instance.reset_session()

            assert moved is not None
            assert reached == [True]

    async def test_an_already_empty_directory_is_not_archived_again(
        self, config, sockets,
    ):
        """The routine case, not an edge one: the reference deployment
        restarts its units on every commit, so a 300s window meets one fairly
        often and the recovery is another window against the directory that
        move already emptied. Archiving nothing a second time would accumulate
        directories nothing sweeps while telling the operator two of them
        matter."""
        async with running(config, sockets) as instance:
            (sockets.session / "creds.json").unlink()
            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                result = await instance.repair_session(USER)

            assert result.ok is True, result.message
            assert result.moved_to is None
            assert archives(sockets) == []
            assert instance.status.fatal_is_permanent is False
            assert instance.pairing_window is not None
            assert instance.pairing_window.destructive is False


def wrap_move_aside(monkeypatch, instance) -> list[bool]:
    """Record that the shared core ran, and let it run."""
    reached: list[bool] = []
    real = instance._move_session_aside

    def watched():
        reached.append(True)
        return real()

    monkeypatch.setattr(instance, "_move_session_aside", watched)
    return reached


# ---------------------------------------------------------------------------
# The ledger inside a window
# ---------------------------------------------------------------------------


class TestASendInsideAWindow:
    """The consequence of clearing the latch, paid for in the same stage.

    `_send`'s only session gate was `fatal_is_permanent`. Clear it and nothing
    re-arms until a human scans, so for the whole window the bridge is
    attached to a sidecar restarting into an unpaired session: `writer.write`
    succeeds, the mark is set, and the answer either never comes or comes back
    as a link failure — both of which settle **`unknown`**, the one state
    `.claude/rules/whatsapp.md` says an operator can never resolve.
    `logical_key` is UNIQUE and nothing deletes from `sent_whatsapp`, so a
    task result, a confirmation prompt or an admin alert caught inside a
    window would be unsendable for good.

    Every case asserts the **row**. The refusal is not the thing that cannot
    be repaired afterwards.
    """

    async def test_a_send_inside_an_open_window_settles_failed(
        self, config, sockets, monkeypatch,
    ):
        """The arm on its own, with the state it has to discriminate against
        arranged deliberately: no latched fault, a connected sidecar, and no
        `send_result` ever answered. **Remove the `_send` window arm and this
        row settles `unknown`** at `send_timeout`, which is where the control
        has to go red.
        """
        bind_user(config)
        async with running(config, sockets) as instance:
            use_bridge_as_adapter(monkeypatch, instance)
            async with sidecar(instance, sockets) as peer:
                window = await instance.open_pairing_window(USER)
                assert window is not None

                record = await asyncio.wait_for(
                    outbound.deliver_whatsapp(
                        config, logical_key="task-result:1", user_id=USER,
                        text="the backup finished",
                    ),
                    timeout=5.0,
                )

                assert record.status == "failed"
                assert ledger_row(config, "task-result:1")["status"] == "failed"
                assert peer.types.count(proto.MSG_SEND) == 0, peer.types

    async def test_a_send_after_a_real_repair_settles_failed(
        self, config, sockets, monkeypatch,
    ):
        """The combination, which is the stage line's stated hazard: the latch
        really is cleared here, so the only thing keeping the row honest is the
        window arm. A case that opened a window by hand leaves the latch set
        and would pass on the old gate alone."""
        bind_user(config)
        async with running(config, sockets) as instance:
            use_bridge_as_adapter(monkeypatch, instance)
            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                result = await instance.repair_session(USER)
            assert result.ok is True, result.message
            assert instance.status.fatal_is_permanent is False

            async with sidecar(instance, sockets):
                record = await asyncio.wait_for(
                    outbound.deliver_whatsapp(
                        config, logical_key="task-result:2", user_id=USER,
                        text="the backup finished",
                    ),
                    timeout=5.0,
                )

            assert record.status == "failed"
            assert ledger_row(config, "task-result:2")["status"] == "failed"

    async def test_a_send_outside_a_window_is_unaffected(
        self, config, sockets, monkeypatch,
    ):
        """The control for the arm's placement: with the window closed the
        send reaches the socket exactly as it did before, so the arm cannot be
        a blanket refusal wearing a window's name."""
        bind_user(config)
        async with running(config, sockets) as instance:
            use_bridge_as_adapter(monkeypatch, instance)
            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                assert (await instance.repair_session(USER)).ok is True
                assert await instance.cancel_pairing() is True

            async with sidecar(
                instance, sockets, closes_on_shutdown=False,
            ) as peer:
                sending = asyncio.ensure_future(
                    outbound.deliver_whatsapp(
                        config, logical_key="task-result:3", user_id=USER,
                        text="the backup finished",
                    ),
                )
                await wait_for(
                    lambda: peer.types.count(proto.MSG_SEND) == 1, timeout=5.0,
                )
                request = next(
                    payload
                    for _, payload in peer.frames
                    if payload["type"] == proto.MSG_SEND
                )
                await peer.say(
                    proto.MSG_SEND_RESULT,
                    request_id=request["request_id"],
                    ok=True,
                    message_id="BAE5CAFE",
                )
                record = await asyncio.wait_for(sending, timeout=5.0)

            assert record.status == "accepted"
            assert ledger_row(config, "task-result:3")["status"] == "accepted"


# ---------------------------------------------------------------------------
# The window the sequence leaves behind
# ---------------------------------------------------------------------------


class TestWhatTheSequenceLeavesOpen:
    """The result is what the durable row and the route read, so the fields
    matter as much as the side effects."""

    async def test_the_window_is_armed_and_says_it_is_waiting(
        self, config, sockets,
    ):
        async with running(config, sockets) as instance:
            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)
                result = await instance.repair_session(USER)

            window = instance.pairing_window
            assert window is not None
            assert window.window_id == result.window_id
            assert window.requested_by == USER
            assert window.destructive is True
            assert window.state == pairing_relay.STATE_AWAITING_SIDECAR
            assert instance.status.pairing_state == (
                pairing_relay.STATE_AWAITING_SIDECAR
            )

    async def test_the_archived_path_survives_a_window_that_never_opened(
        self, config, sockets, monkeypatch,
    ):
        """A full-account credential has already moved by the time the window
        is armed, and `open_pairing_window` can answer `None`. A result that
        dropped `moved_to` there would leave an operator with two directories
        and nothing saying which holds their session."""
        async with running(config, sockets) as instance:
            async with sidecar(instance, sockets) as peer:
                await latch_fatal(instance, peer)

                async def refuse(requested_by, *, destructive=False):
                    return None

                monkeypatch.setattr(instance, "open_pairing_window", refuse)
                result = await instance.repair_session(USER)

            assert result.ok is False
            assert result.moved_to is not None
            assert str(result.moved_to) in result.message
            assert (result.moved_to / "creds.json").read_text() == CREDS
