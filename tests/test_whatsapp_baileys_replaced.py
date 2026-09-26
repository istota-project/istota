"""A second client on the same Baileys session, seen from the daemon (ISSUE-553).

WhatsApp closes a connection with 440 (`connectionReplaced`) when another
client logs in with the same credential. On the reported outage two copies of
one session directory knocked each other off every few seconds for five and a
half hours, and nothing alerted: each reopen sent a `ready`, so the bridge read
healthy between closes, and a transient `fatal` raises nothing by design.

The sidecar now yields after five replaced connections in ten minutes and
reports the run in a `connection_replaced` fatal. These are the daemon's half:
the bridge keeps the latch and the count, refuses sends while latched, alerts
once per outage on the sidecar's `announce` level, and treats a given-up run as
a permanent fault. `doctor` reports all of it. The sidecar's half is
`tests/test_whatsapp_sidecar_vendoring.py::TestAReplacedConnectionIsBounded`.
"""

from __future__ import annotations

import asyncio

import pytest

from istota import db, doctor
from istota.transport.whatsapp import baileys_bridge, baileys_runtime, outbound
from istota.transport.whatsapp import baileys_protocol as proto

from .support.baileys_sidecar import SocketDir, wait_for
from .support.monotonic_spy import monotonic_spy
from .test_whatsapp_baileys_doctor import _Status, _run as _doctor_run
from .test_whatsapp_baileys_doctor import _config as _doctor_config
from .test_whatsapp_baileys_runtime import USER, _config
from .test_whatsapp_pairing_bridge import (
    bind_user,
    ledger_row,
    running,
    sidecar,
    use_bridge_as_adapter,
)


def _frame(**fields) -> dict:
    fields.setdefault("reason", "connection_replaced")
    fields.setdefault("permanent", False)
    fields.setdefault("replacements", 1)
    fields.setdefault("latched", False)
    fields.setdefault("announce", 0)
    return fields


@pytest.fixture
def sockets():
    directory = SocketDir()
    yield directory
    directory.cleanup()


@pytest.fixture
def config(tmp_path):
    return _config(tmp_path)


@pytest.fixture(autouse=True)
def _no_leaked_bridge():
    yield
    baileys_bridge.clear_active_bridge()


def _bridge(tmp_path, **kwargs):
    fatals, replaced = [], []
    bridge = baileys_bridge.BaileysBridge(
        _config(tmp_path),
        on_fatal=fatals.append,
        on_replaced=lambda level, count: replaced.append((level, count)),
        **kwargs,
    )
    return bridge, fatals, replaced


class TestTheBridgeReadsTheRun:
    def test_one_replaced_connection_is_counted_and_nothing_latches(self, tmp_path):
        bridge, fatals, replaced = _bridge(tmp_path)

        bridge._handle_fatal(_frame())

        status = bridge.status
        assert status.connection_replaced_recent == 1
        assert status.connection_replaced_latched is False
        assert status.fatal_is_permanent is False
        assert fatals == [] and replaced == []

    def test_the_reason_alone_is_never_permanent(self, tmp_path):
        """Transient unless the sidecar says it has given up: a permanent
        latch opens `pair --reset`, which archives a credential that works."""
        bridge, fatals, _ = _bridge(tmp_path)

        bridge._handle_fatal({"reason": "connection_replaced"})

        assert bridge.status.fatal_is_permanent is False
        assert "connection_replaced" not in baileys_bridge._PERMANENT_FATALS
        assert fatals == []

    def test_a_latch_alerts_once_on_the_sidecars_announce(self, tmp_path):
        bridge, fatals, replaced = _bridge(tmp_path)

        bridge._handle_fatal(_frame(replacements=5, latched=True, announce=1))
        # The same verdict re-announced on a reconnected link, or after a
        # scheduler restart: the sidecar has already had the alert delivered.
        bridge._handle_fatal(_frame(replacements=5, latched=True, announce=0))
        bridge._handle_fatal(_frame(replacements=5, latched=True, announce=0))

        assert bridge.status.connection_replaced_latched is True
        assert replaced == [(1, 5)]
        assert fatals == []

    def test_re_announced_frames_do_not_inflate_the_count(self, tmp_path):
        bridge, _, _ = _bridge(tmp_path)

        for _ in range(4):
            bridge._handle_fatal(_frame(replacements=5, latched=True))

        assert bridge.status.connection_replaced_recent == 5

    def test_a_ready_lifts_the_latch(self, tmp_path):
        """A probe that opens sends `ready`, and sends go again. The alert is
        not re-armed by it: that is the sidecar's `announce`, which a stable
        open resets and a probe does not."""
        bridge, _, replaced = _bridge(tmp_path)
        bridge._handle_fatal(_frame(replacements=5, latched=True, announce=1))

        bridge._dispatch("ready", {})
        bridge._handle_fatal(_frame(replacements=6, latched=True, announce=0))

        assert bridge.status.connection_replaced_latched is True
        assert replaced == [(1, 5)]

    def test_giving_up_is_permanent_and_is_not_the_unlink_alert(self, tmp_path):
        bridge, fatals, replaced = _bridge(tmp_path)
        bridge._handle_fatal(_frame(replacements=5, latched=True, announce=1))

        bridge._handle_fatal(_frame(
            replacements=8, latched=True, permanent=True, announce=2,
        ))

        status = bridge.status
        assert status.fatal_is_permanent is True
        assert status.fatal_reason == "connection_replaced"
        assert bridge._permanent_fatal.is_set()
        assert replaced == [(1, 5), (2, 8)]
        # The unlink alert would tell an operator the device link ended.
        assert fatals == []

    def test_a_re_pair_clears_the_latch(self, tmp_path):
        """Review finding: `_clear_fatal_latch` left it set, so after a re-pair
        the card and doctor still said "do not re-pair, it retries"."""
        bridge, _, _ = _bridge(tmp_path)
        bridge._handle_fatal(_frame(
            replacements=8, latched=True, permanent=True, announce=2,
        ))

        bridge._clear_fatal_latch()

        assert bridge.status.connection_replaced_latched is False
        assert bridge.status.connection_replaced_recent == 0

    def test_a_logged_out_session_is_not_relabelled(self, tmp_path):
        """A probe that meets a 401 is an unlinked device, and the unlink's
        remedy is the one that works."""
        bridge, _, replaced = _bridge(tmp_path)
        bridge._handle_fatal({"reason": "logged_out", "permanent": True})

        bridge._handle_fatal(_frame(replacements=5, latched=True, announce=1))

        assert bridge.status.fatal_reason == "logged_out"
        assert bridge.status.connection_replaced_latched is False
        assert replaced == []

    def test_a_logout_during_a_run_supersedes_it(self, tmp_path):
        bridge, fatals, _ = _bridge(tmp_path)
        bridge._handle_fatal(_frame(replacements=5, latched=True, announce=1))

        bridge._handle_fatal({"reason": "logged_out", "permanent": True})

        assert bridge.status.connection_replaced_latched is False
        assert bridge.status.fatal_reason == "logged_out"
        assert fatals == ["logged_out"]

    def test_a_restored_archive_leaves_its_run_behind(self, tmp_path):
        """`rename(2)` keeps the credential's stamp, so a run carried in with
        the archive would resume a give-up with no probe and no alert."""
        live = tmp_path / "session"
        archive = tmp_path / "session.20260925T000000Z"
        archive.mkdir(mode=0o700)
        (archive / "creds.json").write_text('{"registered": true}')
        (archive / baileys_bridge.REPLACED_RUN_FILE).write_text('{"given_up": true}')

        baileys_bridge.restore_session_archive(live, archive)

        assert (live / "creds.json").exists()
        assert not (live / baileys_bridge.REPLACED_RUN_FILE).exists()

    def test_the_count_ages_out_when_nothing_is_latched(self, tmp_path, monkeypatch):
        bridge, _, _ = _bridge(tmp_path)
        now = [1000.0]
        monotonic_spy(monkeypatch, baileys_bridge, lambda: now[0])
        bridge._handle_fatal(_frame(replacements=2))

        now[0] += baileys_bridge.CONNECTION_REPLACED_WINDOW_SECONDS + 1

        assert bridge.status.connection_replaced_recent == 0

    def test_the_count_holds_while_latched(self, tmp_path, monkeypatch):
        """The draft's review finding: the count decayed to 0 while the latch
        still held, so doctor said "0 times" about an outage in progress."""
        bridge, _, _ = _bridge(tmp_path)
        now = [1000.0]
        monotonic_spy(monkeypatch, baileys_bridge, lambda: now[0])
        bridge._handle_fatal(_frame(replacements=5, latched=True))

        now[0] += 4 * baileys_bridge.CONNECTION_REPLACED_WINDOW_SECONDS

        assert bridge.status.connection_replaced_recent == 5


class TestASendWhileLatched:
    """Settled `failed`, never `unknown`.

    A probe assigns the sidecar's socket before the connection opens, so a
    send reaching it then is answered ambiguously and the UNIQUE ledger key is
    spent on an `unknown` row. Asserted on the row. Control: remove the
    latched arm from `_send` and the send reaches the sidecar.
    """

    async def test_a_send_is_refused_definitely(self, config, sockets, monkeypatch):
        bind_user(config)
        async with running(config, sockets) as instance:
            use_bridge_as_adapter(monkeypatch, instance)
            async with sidecar(instance, sockets, closes_on_shutdown=False) as peer:
                await peer.say(proto.MSG_READY)
                await peer.say(
                    proto.MSG_FATAL, **_frame(replacements=5, latched=True),
                )
                await wait_for(
                    lambda: instance.status.connection_replaced_latched is True,
                )

                record = await asyncio.wait_for(
                    outbound.deliver_whatsapp(
                        config, logical_key="task-result:553", user_id=USER,
                        text="the backup finished",
                    ),
                    timeout=5.0,
                )

                assert record.status == "failed"
                assert ledger_row(config, "task-result:553")["status"] == "failed"
                assert peer.types.count(proto.MSG_SEND) == 0, peer.types

    async def test_one_replaced_connection_refuses_nothing(
        self, config, sockets, monkeypatch,
    ):
        """The control: an unlatched 440 leaves sends to the sidecar, whose
        dropped socket answers them `not_connected` definitely."""
        bind_user(config)
        async with running(config, sockets) as instance:
            use_bridge_as_adapter(monkeypatch, instance)
            async with sidecar(instance, sockets, closes_on_shutdown=False) as peer:
                await peer.say(proto.MSG_READY)
                await peer.say(proto.MSG_FATAL, **_frame())
                await wait_for(
                    lambda: instance.status.connection_replaced_recent == 1,
                )

                task = asyncio.ensure_future(outbound.deliver_whatsapp(
                    config, logical_key="task-result:554", user_id=USER,
                    text="the backup finished",
                ))
                for _ in range(100):
                    if proto.MSG_SEND in peer.types:
                        break
                    await asyncio.sleep(0.02)
                assert proto.MSG_SEND in peer.types
                task.cancel()


class TestTheAlerts:
    async def test_the_latch_alert_is_its_own_row_and_is_pushed_once(
        self, tmp_path, monkeypatch,
    ):
        config = _config(tmp_path)
        pushed = []
        monkeypatch.setattr(
            baileys_runtime, "_push_baileys_alert",
            lambda cfg, raised: pushed.append(raised),
        )

        await baileys_runtime._announce_replaced(config, 1, 5)
        await baileys_runtime._announce_replaced(config, 1, 5)

        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT user_id, dedup_key FROM notifications"
            ).fetchall()
        assert [tuple(r) for r in rows] == [(USER, "whatsapp:baileys-replaced")]
        assert [r.deliver for r in pushed] == [True, False]

    async def test_giving_up_delivers_under_a_key_of_its_own(
        self, tmp_path, monkeypatch,
    ):
        """A second alert, not a bump of the first: the latch row is still
        open, and a bump does not deliver."""
        config = _config(tmp_path)
        pushed = []
        monkeypatch.setattr(
            baileys_runtime, "_push_baileys_alert",
            lambda cfg, raised: pushed.append(raised),
        )

        await baileys_runtime._announce_replaced(config, 1, 5)
        await baileys_runtime._announce_replaced(config, 2, 8)

        with db.get_db(config.db_path) as conn:
            keys = [r[0] for r in conn.execute(
                "SELECT dedup_key FROM notifications ORDER BY id"
            )]
        assert keys == [
            "whatsapp:baileys-replaced", "whatsapp:baileys-replaced-gave-up",
        ]
        assert [r.deliver for r in pushed] == [True, True]

    def test_the_latch_body_names_the_other_client_and_no_re_pair(self):
        body = baileys_runtime._replaced_alert_body(1, 5)

        assert "another client" in body.lower()
        assert "pair --reset" not in body
        assert "Admin" not in body

    def test_the_give_up_body_names_the_re_pair_and_unlinking_first(self):
        body = baileys_runtime._replaced_alert_body(2, 8)

        assert "Admin, Connections" in body
        assert "Linked Devices" in body
        assert "does not revoke" in body

    def test_the_bridge_is_started_with_the_callback(self, tmp_path, monkeypatch):
        seen = {}

        class _Bridge:
            def __init__(self, config, **kwargs):
                seen.update(kwargs)

            async def start(self):
                return None

            async def stop(self):
                return None

        monkeypatch.setattr(baileys_bridge, "BaileysBridge", _Bridge)
        monkeypatch.setattr(
            baileys_bridge, "resolve_sidecar_argv", lambda config: (),
        )
        monkeypatch.setattr(
            "istota.async_runtime.run_coro", lambda coro, **kw: asyncio.run(coro),
        )
        monkeypatch.setattr(
            "istota.async_runtime.get_async_runtime",
            lambda: type("_RT", (), {"add_cleanup_hook": staticmethod(lambda f: None)})(),
        )

        assert baileys_runtime.start_baileys_bridge(_config(tmp_path)) is True
        assert callable(seen.get("on_replaced"))


class TestAnUnreadableCredentialSaysOneThing:
    """Branch review: the alert's title said "unlinked", its body said fix the
    owner or mode, doctor said `pair --reset`, and the card said "unlinked".
    One remedy now, fix-and-restart first and a re-pair only for a damaged
    file with no usable backup."""

    def test_the_alert_has_a_title_and_key_of_its_own(self, tmp_path):
        config = _config(tmp_path)

        asyncio.run(baileys_runtime._announce_unlink(config, "credential_unreadable"))

        with db.get_db(config.db_path) as conn:
            rows = [tuple(r) for r in conn.execute(
                "SELECT dedup_key, title, body FROM notifications"
            )]
        assert len(rows) == 1
        key, title, body = rows[0]
        assert key == "whatsapp:baileys-credential-unreadable"
        assert "unlinked" not in title.lower()
        # Stored bodies are flattened (backticks go), so compare that form.
        from istota.notification_resolvers.task_alert import flatten_body

        assert flatten_body(baileys_runtime.CREDENTIAL_UNREADABLE_REMEDY) in body

    def test_doctor_gives_the_same_remedy(self, tmp_path):
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=False,
            fatal_reason="credential_unreadable", fatal_is_permanent=True,
        ))

        result = _doctor_run(_doctor_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.FAIL
        assert result.remedy == baileys_runtime.CREDENTIAL_UNREADABLE_REMEDY
        assert "session ended" not in result.detail

    def test_the_remedy_restarts_before_it_re_pairs(self):
        remedy = baileys_runtime.CREDENTIAL_UNREADABLE_REMEDY

        assert remedy.index("restart") < remedy.index("pair --reset")
        assert "creds.json.bak" in remedy


class TestDoctor:
    def test_a_latched_run_fails_without_a_re_pair(self, tmp_path):
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=False,
            fatal_reason="connection_replaced",
            connection_replaced_recent=5, connection_replaced_latched=True,
        ))

        result = _doctor_run(_doctor_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.FAIL
        assert "another client" in result.detail
        assert "5" in result.detail
        assert "pair --reset" not in result.remedy
        assert "Admin, Connections" not in result.remedy

    def test_a_given_up_run_names_the_re_pair_and_unlinking_first(self, tmp_path):
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=False,
            fatal_reason="connection_replaced", fatal_is_permanent=True,
            connection_replaced_recent=8, connection_replaced_latched=True,
        ))

        result = _doctor_run(_doctor_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.FAIL
        assert "another client" in result.detail
        assert result.remedy == baileys_runtime.REPLACED_GIVE_UP_REMEDY
        assert "Linked Devices" in result.remedy

    def test_recent_replacements_on_an_open_session_warn(self, tmp_path):
        baileys_bridge.set_active_bridge(_Status(
            listening=True, connected=True, ready=True,
            connection_replaced_recent=2,
        ))

        result = _doctor_run(_doctor_config(tmp_path), "whatsapp.baileys_bridge")

        assert result.status == doctor.WARN
        assert "2 times" in result.detail
