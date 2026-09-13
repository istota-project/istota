"""The Baileys bridge, against a real socket and a real fake sidecar.

Three groups of property, and each is here because it cannot be asserted by
reading:

**The credential.** The session directory is a full WhatsApp account — whoever
can read it can send and read as the paired number, with no second factor. So
the directory is 0700 whether it was just created or already existed, a
symlink at its name is refused rather than followed, and the socket is 0600
from the instant the inode exists rather than narrowed to it a moment later.

**The one-send contract.** `definite` is the single bit deciding `failed`
against `unknown` in the ledger, and this stage draws that line at
`writer.write`: above it the message provably never entered the socket, from
it onwards the bytes may be in the kernel buffer whatever happens next. Each
case here drives a real socket into the state it names and asserts on the
`sent_whatsapp` **row**, not on `bridge.send`'s return value — the return value
is what the ledger reads, so asserting it alone would pass against a ledger
that ignored it.

**The concurrency.** The reader must stay free while the worker waits for a
`send_result`, because the worker's own work can be a reply. The STOP case
below is the whole shape end to end: a message arrives, the worker applies it,
the acknowledgement goes back through the ledger and through `bridge.send`, and
the `send_result` answering it can only be read by the loop that is not
blocked.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import sqlite3
from datetime import datetime, timezone

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.transport.whatsapp import baileys_protocol as proto, outbound
from istota.transport.whatsapp import baileys_bridge as bridge_module
from istota.transport.whatsapp import webhook as webhook_module
from istota.transport.whatsapp._types import WhatsAppSendRequest
from istota.transport.whatsapp.baileys_bridge import (
    BaileysBridge,
    ensure_session_dir,
    harden_session_files,
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

#: What the Baileys adapter will declare at Stage 6. Restated here rather than
#: imported from a module that does not exist yet; the gates it drives are
#: Stage 2's and are asserted there.
BAILEYS_CAPS = WhatsAppProviderCaps(
    metered=False, has_service_window=False,
    supports_templates=False, delivery_receipts=True,
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


def bind_user(config: Config) -> None:
    """A user enrolled under the Baileys adapter, as a first inbound leaves them."""
    with db.get_db(config.db_path) as conn:
        db.set_whatsapp_binding(
            conn, USER, bootstrap_phone_number=USER_NUMBER,
        )
        db.latch_whatsapp_jid(conn, USER, jid=USER_JID)


@pytest.fixture
async def bridge(config, sockets):
    instance = BaileysBridge(
        config, socket_path=sockets.socket, session_dir=sockets.session,
        send_timeout=2.0,
    )
    await instance.start()
    try:
        yield instance
    finally:
        await instance.stop()


@pytest.fixture
async def sidecar(bridge, sockets):
    async with connected(bridge, sockets) as fake:
        yield fake


@contextlib.asynccontextmanager
async def connected(bridge, sockets, **kwargs):
    """A sidecar whose connection the bridge has actually accepted.

    `open_unix_connection` returns as soon as the kernel completes the connect,
    which is before the server's callback has run — so a test that sends
    immediately is racing the bridge's own `_writer` assignment, and would see
    a `definite` refusal for a link that was about to exist.
    """
    fake = FakeSidecar(sockets.socket)
    await fake.connect(**kwargs)
    if kwargs.get("hello", True) and kwargs.get("version") is None:
        await wait_for(lambda: bridge.status.connected is True)
    try:
        yield fake
    finally:
        await fake.close()


def baileys_adapter(bridge) -> WhatsAppProviderAdapter:
    """What Stage 6's `build_adapter` will return, built by hand.

    `send` is the bridge's own bound method, which is what makes the ledger
    cases below drive the real socket rather than a double.
    """
    return WhatsAppProviderAdapter(
        name="baileys", caps=BAILEYS_CAPS, parse_webhook=None,
        send=bridge.send, verify_signature=None,
    )


def use_bridge_as_adapter(monkeypatch, bridge) -> None:
    adapter = baileys_adapter(bridge)
    monkeypatch.setattr(outbound, "active_adapter", lambda config: adapter)


def ledger_row(config: Config, logical_key: str) -> sqlite3.Row:
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT * FROM sent_whatsapp WHERE logical_key = ?", (logical_key,),
        ).fetchone()


def inbound_line(**overrides) -> dict:
    payload = {
        "message_id": "BAE5F00D",
        "jid": USER_JID,
        "message_type": "text",
        "text": "check the backup",
        "timestamp": int(datetime.now(timezone.utc).timestamp()),
    }
    payload.update(overrides)
    return payload


# ---------------------------------------------------------------------------
# The session directory and the socket
# ---------------------------------------------------------------------------


class TestTheSessionDirectory:
    """A paired session is a full-account credential with no revocation."""

    def test_a_fresh_directory_is_private(self, sockets):
        path = ensure_session_dir(sockets.session)
        assert mode_of(path) == 0o700

    def test_an_existing_wide_directory_is_narrowed(self, sockets):
        """The case a `mkdir(mode=…)` cannot cover, and the one that happens.

        The mode argument applies only to a directory the call *creates*, so
        every run after the first would leave an operator's `mkdir -p` at
        whatever their umask produced.
        """
        sockets.session.mkdir(mode=0o777)
        os.chmod(sockets.session, 0o777)

        ensure_session_dir(sockets.session)

        assert mode_of(sockets.session) == 0o700

    def test_a_symlink_at_the_name_is_refused_rather_than_followed(self, sockets):
        """`O_NOFOLLOW` is what makes the mode land on the directory itself.

        Without it, `mkdir(exist_ok=True)` succeeds through a symlink and the
        chmod tightens whatever it points at, while the session is written
        somewhere the daemon never chose.
        """
        elsewhere = sockets.path / "elsewhere"
        elsewhere.mkdir()
        sockets.session.symlink_to(elsewhere)

        with pytest.raises(OSError):
            ensure_session_dir(sockets.session)

    def test_a_file_at_the_name_is_refused(self, sockets):
        sockets.session.write_text("not a directory")
        with pytest.raises(NotADirectoryError):
            ensure_session_dir(sockets.session)

    def test_a_wide_session_file_is_narrowed_and_counted(self, sockets):
        """The backstop behind the child's umask.

        The sidecar creates these files, so the daemon cannot set their mode at
        birth. A non-zero count is what tells `doctor` the sidecar is running
        under a umask that is not this module's.
        """
        ensure_session_dir(sockets.session)
        creds = sockets.session / "creds.json"
        creds.write_text("{}")
        os.chmod(creds, 0o644)
        (sockets.session / "already-tight.json").write_text("{}")
        os.chmod(sockets.session / "already-tight.json", 0o600)

        widened = harden_session_files(sockets.session)

        assert widened == 1
        assert mode_of(creds) == 0o600

    async def test_start_makes_the_directory_before_anything_can_pair(
        self, config, sockets
    ):
        instance = BaileysBridge(
            config, socket_path=sockets.socket, session_dir=sockets.session,
        )
        await instance.start()
        try:
            assert mode_of(sockets.session) == 0o700
        finally:
            await instance.stop()

    def test_the_default_sits_beside_the_database_and_the_config_wins(
        self, config, tmp_path
    ):
        assert bridge_module.default_session_dir(config) == (
            tmp_path / bridge_module.SESSION_DIR_NAME
        )
        config.whatsapp.baileys.session_dir = str(tmp_path / "elsewhere")
        assert bridge_module.default_session_dir(config) == tmp_path / "elsewhere"


class TestTheSocket:
    async def test_the_inode_is_private_from_the_moment_it_exists(self, bridge, sockets):
        """0600 by umask rather than by a later `chmod`.

        Between a default-permission create and a narrowing chmod there is a
        window in which another account on the host can connect — and a
        connection here is the right to send as this WhatsApp account.
        """
        assert mode_of(sockets.socket) == 0o600

    async def test_a_stale_socket_is_replaced(self, config, sockets):
        first = BaileysBridge(
            config, socket_path=sockets.socket, session_dir=sockets.session,
        )
        await first.start()
        # A crash: the listener goes away and the inode stays.
        first._server.close()
        assert sockets.socket.exists()

        second = BaileysBridge(
            config, socket_path=sockets.socket, session_dir=sockets.session,
        )
        await second.start()
        try:
            async with connected(second, sockets):
                pass
        finally:
            await second.stop()

    async def test_a_regular_file_at_the_path_is_refused_rather_than_deleted(
        self, config, sockets
    ):
        """Unlinking on a name collision would be this module deleting somebody
        else's data on the strength of a path."""
        sockets.socket.write_text("someone else's file")
        instance = BaileysBridge(
            config, socket_path=sockets.socket, session_dir=sockets.session,
        )
        with pytest.raises(FileExistsError):
            await instance.start()
        assert sockets.socket.read_text() == "someone else's file"

    async def test_an_over_long_path_is_refused_by_name(self, config, sockets):
        """`sun_path` is 104 bytes on macOS and the kernel's own error names
        neither the path nor the limit."""
        deep = sockets.path / ("x" * 90) / "wa.sock"
        instance = BaileysBridge(
            config, socket_path=deep, session_dir=sockets.session,
        )
        with pytest.raises(ValueError, match="too long"):
            await instance.start()

    async def test_stop_removes_the_socket(self, config, sockets):
        instance = BaileysBridge(
            config, socket_path=sockets.socket, session_dir=sockets.session,
        )
        await instance.start()
        await instance.stop()
        assert not sockets.socket.exists()

    async def test_a_second_sidecar_is_turned_away(self, bridge, sidecar, sockets):
        """Two sidecars on one paired session is two readers of one account,
        and whichever a send happened to reach would decide where the reply
        went."""
        intruder = FakeSidecar(sockets.socket)
        await intruder.connect(hello=False)

        await wait_for(lambda: bridge.status.rejected_connections == 1)
        with pytest.raises((EOFError, ConnectionError, asyncio.IncompleteReadError)):
            await intruder.next_message(timeout=1.0)
        await intruder.close()
        # The first sidecar is untouched.
        assert bridge.status.connected is True


class TestTheChildEnvironment:
    def test_the_sidecar_is_handed_no_credential_the_daemon_holds(
        self, config, sockets, monkeypatch
    ):
        """An allowlist rather than `os.environ` minus a pattern list.

        The daemon's environment carries the Claude token, the Nextcloud
        password, the forge tokens and the master Fernet key. A Node program
        that needs none of them is given none of them, so a new credential name
        nobody thought to filter is excluded by construction.
        """
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-cross")
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "master-key")
        monkeypatch.setenv("NC_PASS", "nextcloud-password")
        monkeypatch.setenv("PATH", "/usr/bin")
        instance = BaileysBridge(
            config, socket_path=sockets.socket, session_dir=sockets.session,
        )

        env = instance._child_env()

        assert set(env) == {
            "PATH", bridge_module.ENV_SOCKET, bridge_module.ENV_SESSION_DIR,
        } | ({"HOME"} if "HOME" in os.environ else set()) | (
            {"LANG"} if "LANG" in os.environ else set()
        ) | ({"LC_ALL"} if "LC_ALL" in os.environ else set()) | (
            {"TZ"} if "TZ" in os.environ else set()
        ) | ({"NODE_ENV"} if "NODE_ENV" in os.environ else set())
        assert env[bridge_module.ENV_SOCKET] == str(sockets.socket)
        assert env[bridge_module.ENV_SESSION_DIR] == str(sockets.session)


# ---------------------------------------------------------------------------
# Version negotiation and malformed input
# ---------------------------------------------------------------------------


class TestTheHelloFrame:
    async def test_a_mismatched_version_drops_the_connection(self, bridge, sockets):
        """Carrying on would mean reading an older shape's fields out of a
        newer frame and resolving a principal from whatever came out."""
        fake = FakeSidecar(sockets.socket)
        await fake.connect(version=proto.PROTOCOL_VERSION + 1)

        await fake.wait_closed()
        await wait_for(lambda: bridge.status.connected is False)
        assert bridge.status.protocol_version is None
        await fake.close()

    async def test_a_first_line_that_is_not_hello_drops_the_connection(
        self, bridge, sockets
    ):
        fake = FakeSidecar(sockets.socket)
        await fake.connect(hello=False)
        await fake.say(proto.MSG_READY)

        await fake.wait_closed()
        await wait_for(lambda: bridge.status.connected is False)
        await fake.close()

    async def test_a_good_hello_records_the_version(self, bridge, sidecar):
        await wait_for(lambda: bridge.status.protocol_version == proto.PROTOCOL_VERSION)


class TestMalformedLines:
    async def test_a_bad_line_is_counted_and_the_link_survives(self, bridge, sidecar):
        """The spec's rule: logged and dropped, with a run of them visible to
        `doctor` — which is what the counter is for."""
        await sidecar.write_raw(b"not json at all\n")
        await wait_for(lambda: bridge.status.malformed_lines == 1)

        await sidecar.say(proto.MSG_READY)
        await wait_for(lambda: bridge.status.ready is True)

    async def test_an_unreadable_event_is_dropped_rather_than_guessed_at(
        self, bridge, sidecar, monkeypatch
    ):
        """A message with no sender is not a message that can be attributed."""
        applied = []
        monkeypatch.setattr(
            webhook_module, "handle_whatsapp_batch",
            lambda *a, **k: applied.append(k) or [],
        )
        payload = inbound_line()
        payload.pop("jid")
        await sidecar.say(proto.MSG_INBOUND, **payload)

        await wait_for(lambda: bridge.status.malformed_lines == 1)
        assert applied == []

    async def test_an_unmodelled_receipt_status_is_dropped_quietly(
        self, bridge, sidecar, monkeypatch
    ):
        applied = []
        monkeypatch.setattr(
            webhook_module, "handle_whatsapp_batch",
            lambda *a, **k: applied.append(k) or [],
        )
        await sidecar.say(
            proto.MSG_RECEIPT, message_id="BAE5F00D", status="played",
            timestamp=int(datetime.now(timezone.utc).timestamp()),
        )
        await sidecar.say(proto.MSG_READY)

        await wait_for(lambda: bridge.status.ready is True)
        assert applied == []
        assert bridge.status.malformed_lines == 0


# ---------------------------------------------------------------------------
# Inbound
# ---------------------------------------------------------------------------


class TestTheInboundReceiver:
    async def test_it_names_baileys_as_the_provenance(
        self, bridge, sidecar, monkeypatch
    ):
        """Stage 4's explicit deferral, settled.

        The parameter defaults to `whatsapp_cloud` so every existing caller is
        unchanged, and the default fails loudly rather than silently — a
        receiver that forgets it claims Cloud provenance and every message it
        produces is gated out as `inactive_provider` on the Baileys deployment
        it is running on. It still has to be passed, and this is what says it
        was.
        """
        seen = {}

        def spy(conn, config, events, *, provider=db.WHATSAPP_LEGACY_PROVIDER):
            seen["provider"] = provider
            seen["events"] = events
            return []

        monkeypatch.setattr(webhook_module, "handle_whatsapp_batch", spy)
        await sidecar.say(proto.MSG_INBOUND, **inbound_line())

        await wait_for(lambda: "provider" in seen)
        assert seen["provider"] == BAILEYS
        assert seen["events"][0].from_user.jid == USER_JID

    async def test_a_message_reaches_the_common_path_and_becomes_a_task(
        self, bridge, sidecar, config
    ):
        """End to end against a real database, with no seam in the middle.

        `handle_whatsapp_batch` is the same entry point Meta's signed webhook
        calls, so dedup, identity, the claim row and task creation are all
        common code exercised here rather than restated.
        """
        bind_user(config)
        await sidecar.say(proto.MSG_INBOUND, **inbound_line())

        def disposition():
            with db.get_db(config.db_path) as conn:
                row = conn.execute(
                    "SELECT disposition, user_id, task_id FROM processed_whatsapp "
                    "WHERE message_id = 'BAE5F00D'"
                ).fetchone()
            return row

        row = await wait_for(disposition)
        assert row["disposition"] == "task"
        assert row["user_id"] == USER
        assert row["task_id"] is not None

    async def test_an_unknown_sender_creates_nothing(self, bridge, sidecar, config):
        await sidecar.say(
            proto.MSG_INBOUND, **inbound_line(jid="15559990000@s.whatsapp.net")
        )
        await sidecar.say(proto.MSG_READY)
        await wait_for(lambda: bridge.status.ready is True)

        with db.get_db(config.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM processed_whatsapp"
            ).fetchone()[0] == 0

    async def test_the_transaction_is_retried_and_then_lost_loudly(
        self, bridge, sidecar, monkeypatch
    ):
        """The spec's error-handling rule: a database failure is parked and
        retried rather than dropped, and a run of failures is a counted,
        terminal loss rather than a silent one.

        The webhook answers 503 and lets Meta redeliver; nothing redelivers
        over a socket, so the retry is this side's.
        """
        monkeypatch.setattr(bridge_module, "INBOUND_RETRY_BASE_SECONDS", 0.001)
        attempts = []

        def always_fails(*args, **kwargs):
            attempts.append(1)
            raise sqlite3.OperationalError("database is locked")

        monkeypatch.setattr(webhook_module, "handle_whatsapp_batch", always_fails)
        await sidecar.say(proto.MSG_INBOUND, **inbound_line())

        await wait_for(lambda: bridge.status.failed_events == 1)
        assert len(attempts) == bridge_module.INBOUND_ATTEMPTS

    async def test_a_transient_failure_is_survived(
        self, bridge, sidecar, monkeypatch
    ):
        monkeypatch.setattr(bridge_module, "INBOUND_RETRY_BASE_SECONDS", 0.001)
        attempts = []

        def flaky(*args, **kwargs):
            attempts.append(1)
            if len(attempts) == 1:
                raise sqlite3.OperationalError("database is locked")
            return []

        monkeypatch.setattr(webhook_module, "handle_whatsapp_batch", flaky)
        await sidecar.say(proto.MSG_INBOUND, **inbound_line())

        await wait_for(lambda: bridge.status.inbound_applied == 1)
        assert bridge.status.failed_events == 0

    async def test_a_full_queue_drops_loudly_rather_than_blocking_the_reader(
        self, bridge, sidecar, monkeypatch
    ):
        """Back-pressure onto the reader would be a deadlock, not a slowdown.

        The worker's own work can be a reply, and a reply awaits a
        `send_result` only the reader can deliver — so a reader blocked on a
        full queue waits for a line it is itself responsible for reading.
        """
        monkeypatch.setattr(bridge, "_queue", asyncio.Queue(maxsize=1))
        bridge._queue.put_nowait((proto.MSG_INBOUND, inbound_line()))
        await sidecar.say(proto.MSG_INBOUND, **inbound_line(message_id="BAE5F00E"))

        await wait_for(lambda: bridge.status.dropped_events == 1)
        # Still reading.
        await sidecar.say(proto.MSG_READY)
        await wait_for(lambda: bridge.status.ready is True)


# ---------------------------------------------------------------------------
# The send round trip
# ---------------------------------------------------------------------------


class TestTheSendRoundTrip:
    async def test_a_send_crosses_and_its_result_comes_back(self, bridge, sidecar):
        task = asyncio.ensure_future(
            bridge.send(WhatsAppSendRequest(to=USER_JID, text="hi", kind="service"))
        )
        request = await sidecar.expect(proto.MSG_SEND)
        await sidecar.say(
            proto.MSG_SEND_RESULT, request_id=request["request_id"], ok=True,
            message_id="BAE5CAFE",
        )

        outcome = await asyncio.wait_for(task, timeout=2.0)
        assert request["to"] == USER_JID and request["text"] == "hi"
        assert outcome.message_id == "BAE5CAFE"

    async def test_the_ledger_settles_accepted(self, bridge, sidecar, config, monkeypatch):
        """Asserted on the row rather than on the return value.

        The return value is what the ledger reads; asserting it alone would
        pass against a ledger that ignored it.
        """
        bind_user(config)
        use_bridge_as_adapter(monkeypatch, bridge)
        task = asyncio.ensure_future(
            outbound.deliver_whatsapp(
                config, logical_key="task-result:1", user_id=USER, text="done",
            )
        )
        await sidecar.answer_send(message_id="BAE5CAFE")

        record = await asyncio.wait_for(task, timeout=5.0)
        row = ledger_row(config, "task-result:1")
        assert record.status == "accepted"
        assert row["status"] == "accepted" and row["meta_message_id"] == "BAE5CAFE"

    async def test_two_sends_are_matched_by_request_id_not_by_order(
        self, bridge, sidecar
    ):
        """A single long-lived connection multiplexes, so the correlation is
        the id. Answered in reverse to prove it is not positional."""
        first = asyncio.ensure_future(
            bridge.send(WhatsAppSendRequest(to=USER_JID, text="one", kind="service"))
        )
        request_one = await sidecar.expect(proto.MSG_SEND)
        second = asyncio.ensure_future(
            bridge.send(WhatsAppSendRequest(to=USER_JID, text="two", kind="service"))
        )
        request_two = await sidecar.expect(proto.MSG_SEND)

        await sidecar.say(
            proto.MSG_SEND_RESULT, request_id=request_two["request_id"], ok=True,
            message_id="SECOND",
        )
        await sidecar.say(
            proto.MSG_SEND_RESULT, request_id=request_one["request_id"], ok=True,
            message_id="FIRST",
        )

        assert (await asyncio.wait_for(first, timeout=2.0)).message_id == "FIRST"
        assert (await asyncio.wait_for(second, timeout=2.0)).message_id == "SECOND"
        assert request_one["request_id"] != request_two["request_id"]

    async def test_a_late_result_for_a_settled_send_is_dropped(
        self, bridge, sidecar
    ):
        """Re-settling a settled row is what `_TERMINAL_FOR_STATUS` refuses;
        the bridge must not offer it the chance."""
        await sidecar.say(
            proto.MSG_SEND_RESULT, request_id="nobody-is-waiting", ok=True,
            message_id="BAE5CAFE",
        )
        await sidecar.say(proto.MSG_READY)

        await wait_for(lambda: bridge.status.ready is True)
        assert bridge.status.malformed_lines == 0


class TestTheDefiniteLine:
    """Where `failed` stops and `unknown` starts, driven case by case."""

    async def test_no_sidecar_is_a_definite_failure(self, bridge, config, monkeypatch):
        """Nothing was written, so the message provably never left. Spending
        `unknown` here would cost an operator a row they can never resolve for
        a question whose answer is known."""
        bind_user(config)
        use_bridge_as_adapter(monkeypatch, bridge)

        record = await outbound.deliver_whatsapp(
            config, logical_key="task-result:2", user_id=USER, text="done",
        )

        assert record.status == "failed"
        assert ledger_row(config, "task-result:2")["status"] == "failed"

    async def test_a_permanent_fatal_refuses_definitely(
        self, bridge, sidecar, config, monkeypatch
    ):
        """"Sends refuse rather than silently dropping", and the refusal is
        definite because it happens above the socket."""
        bind_user(config)
        use_bridge_as_adapter(monkeypatch, bridge)
        await sidecar.say(proto.MSG_FATAL, reason="logged_out")
        await wait_for(lambda: bridge.status.fatal_is_permanent is True)

        record = await outbound.deliver_whatsapp(
            config, logical_key="task-result:3", user_id=USER, text="done",
        )

        assert record.status == "failed"

    async def test_a_dropped_connection_mid_send_settles_unknown(
        self, bridge, sidecar, config, monkeypatch
    ):
        """The line was written, so the bytes may be on WhatsApp's servers.
        Reporting `failed` for a message that arrived is the error the
        `unknown` state exists to avoid."""
        bind_user(config)
        use_bridge_as_adapter(monkeypatch, bridge)
        task = asyncio.ensure_future(
            outbound.deliver_whatsapp(
                config, logical_key="task-result:4", user_id=USER, text="done",
            )
        )
        await sidecar.expect(proto.MSG_SEND)
        await sidecar.drop()

        record = await asyncio.wait_for(task, timeout=5.0)
        assert record.status == "unknown"
        assert ledger_row(config, "task-result:4")["status"] == "unknown"

    async def test_a_send_the_sidecar_never_answers_settles_unknown(
        self, bridge, sidecar, config, monkeypatch
    ):
        bind_user(config)
        use_bridge_as_adapter(monkeypatch, bridge)
        task = asyncio.ensure_future(
            outbound.deliver_whatsapp(
                config, logical_key="task-result:5", user_id=USER, text="done",
            )
        )
        await sidecar.expect(proto.MSG_SEND)

        record = await asyncio.wait_for(task, timeout=10.0)
        assert record.status == "unknown"

    async def test_a_definite_refusal_from_the_sidecar_settles_failed(
        self, bridge, sidecar, config, monkeypatch
    ):
        bind_user(config)
        use_bridge_as_adapter(monkeypatch, bridge)
        task = asyncio.ensure_future(
            outbound.deliver_whatsapp(
                config, logical_key="task-result:6", user_id=USER, text="done",
            )
        )
        await sidecar.answer_send(
            ok=False, definite=True, reason="not_on_whatsapp", error_code="404",
        )

        record = await asyncio.wait_for(task, timeout=5.0)
        assert record.status == "failed"
        assert ledger_row(config, "task-result:6")["error_code"] == "404"

    async def test_an_ambiguous_refusal_from_the_sidecar_settles_unknown(
        self, bridge, sidecar, config, monkeypatch
    ):
        bind_user(config)
        use_bridge_as_adapter(monkeypatch, bridge)
        task = asyncio.ensure_future(
            outbound.deliver_whatsapp(
                config, logical_key="task-result:7", user_id=USER, text="done",
            )
        )
        await sidecar.answer_send(ok=False, reason="timeout")

        record = await asyncio.wait_for(task, timeout=5.0)
        assert record.status == "unknown"

    async def test_send_never_raises_even_when_the_link_misbehaves(self, bridge):
        """The adapter contract. A raise inside the claim-to-settle region
        settles the row `unknown` for a message that was never sent."""
        class Exploding:
            def is_closing(self):
                raise RuntimeError("transport gone")

        bridge._writer = Exploding()
        outcome = await bridge.send(
            WhatsAppSendRequest(to=USER_JID, text="hi", kind="service")
        )

        assert outcome.definite is False

    async def test_the_bound_method_satisfies_the_registry_contract(self, bridge):
        """`_contract_fault` refuses a synchronous `send` because one raises
        inside the claim-to-settle region. Stage 6 hands this exact object to
        the registry."""
        assert asyncio.iscoroutinefunction(bridge.send)


# ---------------------------------------------------------------------------
# Receipts, and the concurrency the whole design turns on
# ---------------------------------------------------------------------------


class TestReceipts:
    async def test_a_receipt_settles_the_row_it_names(
        self, bridge, sidecar, config, monkeypatch
    ):
        bind_user(config)
        use_bridge_as_adapter(monkeypatch, bridge)
        task = asyncio.ensure_future(
            outbound.deliver_whatsapp(
                config, logical_key="task-result:8", user_id=USER, text="done",
            )
        )
        await sidecar.answer_send(message_id="BAE5CAFE")
        await asyncio.wait_for(task, timeout=5.0)

        await sidecar.say(
            proto.MSG_RECEIPT, message_id="BAE5CAFE", status="delivered",
            timestamp=int(datetime.now(timezone.utc).timestamp()),
        )

        await wait_for(
            lambda: ledger_row(config, "task-result:8")["status"] == "delivered"
        )

    async def test_a_receipt_before_the_id_parks_and_drains(
        self, bridge, sidecar, config, monkeypatch
    ):
        """The `whatsapp_parked_status` machinery, reached over the socket.

        Baileys mints the id in its `send_result` just as Meta mints it in the
        send reply, so the race is the same one and the table is unchanged. The
        `send_result` is held back until the receipt has been applied, which is
        the state that produces it.
        """
        bind_user(config)
        use_bridge_as_adapter(monkeypatch, bridge)
        task = asyncio.ensure_future(
            outbound.deliver_whatsapp(
                config, logical_key="task-result:9", user_id=USER, text="done",
            )
        )
        request = await sidecar.expect(proto.MSG_SEND)

        # The receipt overtakes the id it names.
        await sidecar.say(
            proto.MSG_RECEIPT, message_id="BAE5CAFE", status="failed",
            error_code="500",
            timestamp=int(datetime.now(timezone.utc).timestamp()),
        )
        await wait_for(lambda: bridge.status.inbound_applied == 1)
        with db.get_db(config.db_path) as conn:
            parked = conn.execute(
                "SELECT COUNT(*) FROM whatsapp_parked_status"
            ).fetchone()[0]
        assert parked == 1

        await sidecar.say(
            proto.MSG_SEND_RESULT, request_id=request["request_id"], ok=True,
            message_id="BAE5CAFE",
        )
        await asyncio.wait_for(task, timeout=5.0)

        row = ledger_row(config, "task-result:9")
        assert row["status"] == "failed"
        assert row["error_code"] == "500"


class TestTheReaderStaysFree:
    async def test_a_reply_the_worker_is_waiting_on_still_lands(
        self, bridge, sidecar, config, monkeypatch
    ):
        """The deadlock this design exists to avoid, driven end to end.

        A STOP arrives; the worker applies it and then owes an acknowledgement,
        which goes through the ledger and back out through `bridge.send`. That
        send waits for a `send_result` only the read loop can deliver — so if
        the reader did the work instead of dispatching it, the acknowledgement
        would wait for a line nobody was reading.
        """
        bind_user(config)
        use_bridge_as_adapter(monkeypatch, bridge)
        await sidecar.say(
            proto.MSG_INBOUND, **inbound_line(text="STOP", message_id="BAE5F00F")
        )

        request = await sidecar.expect(proto.MSG_SEND, timeout=10.0)
        await sidecar.say(
            proto.MSG_SEND_RESULT, request_id=request["request_id"], ok=True,
            message_id="BAE5ACK",
        )

        def settled():
            row = ledger_row(config, "opt-out:BAE5F00F")
            return row if row is not None and row["status"] != "pending" else None

        row = await wait_for(settled, timeout=10.0)
        assert row["status"] == "accepted"
        assert row["meta_message_id"] == "BAE5ACK"
        with db.get_db(config.db_path) as conn:
            assert db.get_whatsapp_binding(conn, USER).opted_out_at is not None


class TestTheSupervisor:
    async def test_no_argv_supervises_nothing(self, bridge):
        """The Ansible shape: the sidecar is its own systemd unit, so the
        bridge listens and supervises nothing. Passing an argv is the
        `istota serve` shape."""
        assert bridge._supervisor is None
        assert bridge.status.listening is True

    async def test_a_sidecar_that_exits_is_restarted(self, config, sockets, monkeypatch):
        monkeypatch.setattr(bridge_module, "RESPAWN_BASE_SECONDS", 0.001)
        instance = BaileysBridge(
            config, socket_path=sockets.socket, session_dir=sockets.session,
            sidecar_argv=("/bin/sh", "-c", "exit 3"),
        )
        await instance.start()
        try:
            await wait_for(lambda: instance.status.restarts >= 2, timeout=5.0)
        finally:
            await instance.stop()

    async def test_a_permanent_fatal_stops_the_respawn_loop(
        self, config, sockets, monkeypatch
    ):
        """A logged-out session comes back only from `istota whatsapp pair`, so
        respawning burns a process every few seconds and changes nothing."""
        monkeypatch.setattr(bridge_module, "RESPAWN_BASE_SECONDS", 0.001)
        instance = BaileysBridge(
            config, socket_path=sockets.socket, session_dir=sockets.session,
            sidecar_argv=("/bin/sh", "-c", "sleep 30"),
        )
        await instance.start()
        try:
            async with connected(instance, sockets) as fake:
                await fake.say(proto.MSG_FATAL, reason="logged_out")
                await wait_for(lambda: instance.status.fatal_is_permanent is True)
                await wait_for(lambda: instance._supervisor.done(), timeout=5.0)
        finally:
            await instance.stop()

    async def test_a_transient_fatal_leaves_the_loop_alone(self, bridge, sidecar):
        await sidecar.say(proto.MSG_FATAL, reason="connection_closed")
        await wait_for(lambda: bridge.status.fatal_reason == "connection_closed")
        assert bridge.status.fatal_is_permanent is False

    async def test_stop_asks_before_it_insists(self, config, sockets):
        instance = BaileysBridge(
            config, socket_path=sockets.socket, session_dir=sockets.session,
        )
        await instance.start()
        async with connected(instance, sockets) as fake:
            stopping = asyncio.ensure_future(instance.stop())

            message = await fake.expect(proto.MSG_SHUTDOWN)
            assert message["type"] == proto.MSG_SHUTDOWN
            await asyncio.wait_for(stopping, timeout=5.0)


class TestTheQrPayload:
    async def test_it_reaches_the_callback_and_no_log(self, config, sockets, caplog):
        """A QR is the pairing credential for the whole account: anything that
        scans it is linked as a device. It is never logged, at any level."""
        seen = []
        instance = BaileysBridge(
            config, socket_path=sockets.socket, session_dir=sockets.session,
            on_qr=seen.append,
        )
        await instance.start()
        try:
            async with connected(instance, sockets) as fake:
                with caplog.at_level("DEBUG"):
                    await fake.say(proto.MSG_QR, qr="2@SECRETPAIRINGPAYLOAD==")
                    await wait_for(lambda: len(seen) == 1)
        finally:
            await instance.stop()

        assert seen == ["2@SECRETPAIRINGPAYLOAD=="]
        assert "SECRETPAIRINGPAYLOAD" not in caplog.text

    async def test_with_no_callback_it_says_so_without_its_contents(
        self, bridge, sidecar, caplog
    ):
        with caplog.at_level("DEBUG"):
            await sidecar.say(proto.MSG_QR, qr="2@SECRETPAIRINGPAYLOAD==")
            await sidecar.say(proto.MSG_READY)
            await wait_for(lambda: bridge.status.ready is True)

        assert "SECRETPAIRINGPAYLOAD" not in caplog.text
        assert "qr_offered" in caplog.text


class TestWhatALogLineMaySay:
    async def test_a_refused_message_is_not_logged_with_its_text(
        self, bridge, sidecar, config, caplog
    ):
        """A JID is `<number>@s.whatsapp.net` with the subscriber's number in
        plain digits, and the rotating app log is read back by the admin Logs
        pane."""
        with caplog.at_level("DEBUG"):
            await sidecar.say(
                proto.MSG_INBOUND,
                **inbound_line(jid="15559990000@s.whatsapp.net", text="my medication"),
            )
            await sidecar.say(proto.MSG_READY)
            await wait_for(lambda: bridge.status.ready is True)

        assert "my medication" not in caplog.text
        assert "15559990000" not in caplog.text
