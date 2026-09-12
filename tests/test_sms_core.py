"""Provider-neutral SMS rendering, state, ingest, and transport contracts."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from istota import db, notifications, surfaces
from istota.notification_resolvers import confirmation as confirmation_source
from istota.config import Config, SmsConfig, UserConfig
from istota.transport import make_registry
from istota.transport.routing import (
    Destination,
    origin_descriptor,
    parse_output_target,
    resolve_delivery_plan,
)
from istota.transport.sms import SmsTransport, sms_conversation_token
from istota.transport.sms.outbound import deliver_sms, render_sms
from istota.transport.sms.providers._types import (
    InboundSmsEvent,
    SmsDeliveryEvent,
    SmsProviderAdapter,
    SmsSendFailure,
    SmsSendResult,
    SmsWebhookRequest,
    SmsWebhookResult,
)
from istota.transport.sms.providers.registry import SmsProviderRegistry
from istota.transport.sms.webhook import deliver_event_response, handle_provider_event


SERVICE_NUMBER = "+15551230000"
USER_NUMBER = "+15551234567"


def _config(tmp_path, *, enabled: bool = True) -> Config:
    path = tmp_path / "istota.db"
    db.init_db(path)
    return Config(
        db_path=path,
        temp_dir=tmp_path / "tmp",
        sms=SmsConfig(
            enabled=enabled,
            provider="twilio",
            service_numbers=[SERVICE_NUMBER],
            default_sender_number=SERVICE_NUMBER,
            max_segments=2,
        ),
        users={"alice": UserConfig(sms_phone_number=USER_NUMBER)},
    )


def _adapter(send):
    def parse(_request: SmsWebhookRequest) -> SmsWebhookResult:
        raise AssertionError("provider parsing is outside common SMS code")

    return SmsProviderAdapter(name="twilio", parse_webhook=parse, send=send)


def _named_adapter(name, send):
    adapter = _adapter(send)
    return SmsProviderAdapter(name=name, parse_webhook=adapter.parse_webhook, send=send)


def _providers(adapter: SmsProviderAdapter) -> SmsProviderRegistry:
    return SmsProviderRegistry(active_name="twilio", adapters={"twilio": adapter})


def _inbound(**changes) -> InboundSmsEvent:
    values = dict(
        provider="twilio",
        provider_event_id="event-1",
        provider_message_id="message-1",
        from_number=USER_NUMBER,
        to_number=SERVICE_NUMBER,
        text="check the backup",
        media_count=0,
        opt_out_action=None,
    )
    values.update(changes)
    return InboundSmsEvent(**values)


async def _handle(config, providers, event):
    with db.get_db(config.db_path) as conn:
        result = handle_provider_event(
            conn, config, event,
            active_provider_ready=providers.active() is not None,
        )
    await deliver_event_response(config, providers, result)
    return result


class TestSmsRendering:
    def test_sanitizes_markdown_and_counts_gsm_extension_septets(self):
        rendered = render_sms("# Result\n\n**Use** `x` | y\n```\ncode\n```\n^{}", 2)

        assert rendered.text == "Result\n\nUse x | y\ncode\n^{}"
        assert rendered.encoding == "gsm7"
        assert rendered.estimated_segments == 1

    def test_uses_utf16_units_and_truncates_without_splitting_surrogates(self):
        rendered = render_sms("🙂" * 100, 1)

        assert rendered.encoding == "ucs2"
        assert rendered.estimated_segments == 1
        assert rendered.text.endswith("[Reply shortened. Send a narrower follow-up.]")
        assert "\ufffd" not in rendered.text
        assert len(rendered.text.encode("utf-16-le")) // 2 <= 70

    @pytest.mark.parametrize(
        "text, segments",
        [("a" * 160, 1), ("a" * 161, 2), ("€" * 80, 1), ("🙂" * 35, 1)],
    )
    def test_segment_boundaries(self, text, segments):
        assert render_sms(text, 2).estimated_segments == segments


class TestSmsIdentityAndRouting:
    def test_conversation_token_is_stable_and_contains_no_provider_or_number(self):
        first = sms_conversation_token("alice")
        second = sms_conversation_token("alice")

        assert first == second
        assert first.startswith("sms-") and len(first) == 28
        assert "twilio" not in first
        assert "1555" not in first

    def test_sms_is_non_room_and_bare_route_only(self, tmp_path):
        config = _config(tmp_path)
        adapter = _adapter(lambda _req: SmsSendResult("opaque-1", "accepted", 1))
        transport = SmsTransport(config, providers=_providers(adapter))

        assert transport.capabilities.room_view is None
        assert transport.capabilities.inbound_room_role is None
        assert surfaces.room_role("sms") is None
        assert parse_output_target("both") == [Destination("talk"), Destination("email")]
        assert parse_output_target("all") == [
            Destination("talk"), Destination("email"), Destination("ntfy")
        ]
        assert parse_output_target("sms") == [Destination("sms")]
        task = db.Task(1, "completed", "sms", "alice", "hi")
        assert transport.resolve_target(task) == USER_NUMBER
        assert origin_descriptor(task) == "sms"
        assert resolve_delivery_plan(
            config, replace(task, output_target="sms:+15557654321"),
            type("Registry", (), {"get": lambda _self, name: transport if name == "sms" else None})(),
        ) == [Destination("sms", USER_NUMBER, "push")]

    def test_enabled_sms_is_registered_and_interactive_fallback_uses_origin(
        self, tmp_path, monkeypatch
    ):
        config = _config(tmp_path)
        adapter = _adapter(lambda _req: SmsSendResult("opaque-1", "accepted", 1))
        monkeypatch.setattr(
            "istota.transport.sms.providers.registry.make_provider_registry", lambda _config: _providers(adapter)
        )

        registry = make_registry(config)
        task = db.Task(
            1, "completed", "sms", "alice", "hi", output_target="missing"
        )
        assert registry.get("sms") is not None
        assert resolve_delivery_plan(config, task, registry) == [
            Destination("sms", USER_NUMBER, "push")
        ]


class TestInboundDomainHandling:
    def test_ordinary_message_creates_one_task_and_no_room_rows(self, tmp_path):
        config = _config(tmp_path)
        providers = _providers(_adapter(lambda _req: SmsSendResult("opaque", "accepted", 1)))

        first = asyncio.run(_handle(config, providers, _inbound()))
        second = asyncio.run(_handle(config, providers, _inbound()))

        assert first.disposition == "task"
        assert second.disposition == "duplicate"
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, first.task_id)
            assert task.source_type == "sms"
            assert task.conversation_token == sms_conversation_token("alice")
            assert task.output_target == "sms"
            assert conn.execute("SELECT count(*) FROM rooms").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0
            assert conn.execute("SELECT count(*) FROM processed_sms").fetchone()[0] == 1

    @pytest.mark.parametrize(
        "event_changes, disposition, opted_out",
        [
            ({"text": "STOP", "opt_out_action": "stop"}, "stop", True),
            ({"text": "HELP", "opt_out_action": "help"}, "help", False),
            ({"text": "", "provider_message_id": "empty"}, "empty", False),
        ],
    )
    def test_non_task_dispositions(self, tmp_path, event_changes, disposition, opted_out):
        config = _config(tmp_path)
        providers = _providers(_adapter(lambda _req: SmsSendResult("opaque", "accepted", 1)))

        result = asyncio.run(_handle(config, providers, _inbound(**event_changes)))

        assert result.disposition == disposition
        assert result.task_id is None
        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM sms_opt_outs WHERE phone_number = ?", (USER_NUMBER,)
            ).fetchone()
            assert (row is not None) is opted_out

    def test_start_clears_opt_out_and_media_sends_one_fixed_reply(self, tmp_path):
        calls = []

        def send(req):
            calls.append(req)
            return SmsSendResult("opaque-media", "accepted", 1)

        config = _config(tmp_path)
        providers = _providers(_adapter(send))
        asyncio.run(_handle(
            config, providers, _inbound(
                provider_message_id="stop", provider_event_id="event-stop",
                opt_out_action="stop",
            )
        ))
        started = asyncio.run(_handle(
            config, providers, _inbound(
                provider_message_id="start", provider_event_id="event-start",
                opt_out_action="start",
            )
        ))
        media = asyncio.run(_handle(
            config, providers, _inbound(
                provider_message_id="media", provider_event_id="event-media",
                media_count=1,
            )
        ))

        assert started.disposition == "start"
        assert media.disposition == "unsupported_media"
        assert [call.text for call in calls] == [
            "MMS is not supported. Please resend the request as text."
        ]
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM sms_opt_outs").fetchone()[0] == 0

    def test_bare_answer_and_command_use_sms_without_transcript_rows(self, tmp_path):
        calls = []

        def send(req):
            calls.append(req.text)
            return SmsSendResult(f"opaque-{len(calls)}", "accepted", 1)

        config = _config(tmp_path)
        providers = _providers(_adapter(send))
        token = sms_conversation_token("alice")
        with db.get_db(config.db_path) as conn:
            held = db.create_task(
                conn, prompt="delete it", user_id="alice", source_type="sms",
                conversation_token=token, output_target="sms",
            )
            db.set_task_confirmation(conn, held, "May I delete it?")

        answer = asyncio.run(_handle(
            config, providers, _inbound(
                provider_message_id="yes", provider_event_id="event-yes", text="YES",
            )
        ))
        command = asyncio.run(_handle(
            config, providers, _inbound(
                provider_message_id="help", provider_event_id="event-help", text="!help",
            )
        ))

        assert answer.disposition == "confirmation_answer"
        assert command.disposition == "command"
        assert calls[0] == "Confirmed."
        assert "Available commands" in calls[1]
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, held).status == "pending"
            assert conn.execute("SELECT count(*) FROM messages").fetchone()[0] == 0

    def test_inactive_provider_and_unknown_number_are_acknowledged_without_rows(
        self, tmp_path, caplog
    ):
        caplog.set_level("INFO")
        config = _config(tmp_path)
        providers = _providers(_adapter(lambda _req: SmsSendResult("opaque", "accepted", 1)))

        inactive = asyncio.run(_handle(
            config, providers, _inbound(provider="telnyx")
        ))
        unknown = asyncio.run(_handle(
            config, providers, _inbound(provider_message_id="unknown", from_number="+15559876543")
        ))

        assert inactive.disposition == "inactive_provider"
        assert unknown.disposition == "unknown_sender"
        assert "sms.inbound.rejected" in caplog.text
        assert USER_NUMBER not in caplog.text
        assert "+15559876543" not in caplog.text
        assert "6543" in caplog.text
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM processed_sms").fetchone()[0] == 0

    @pytest.mark.parametrize(
        "config_enabled, changes, disposition",
        [
            (False, {}, "unconfigured_provider"),
            (True, {"provider_message_id": ""}, "invalid_message_id"),
            (True, {"from_number": "555-123-4567"}, "invalid_sender"),
        ],
    )
    def test_disabled_or_invalid_inbound_creates_no_rows(
        self, tmp_path, config_enabled, changes, disposition
    ):
        config = _config(tmp_path, enabled=config_enabled)
        providers = _providers(_adapter(lambda _req: SmsSendResult("opaque", "accepted", 1)))

        result = asyncio.run(_handle(config, providers, _inbound(**changes)))

        assert result.disposition == disposition
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM processed_sms").fetchone()[0] == 0


class TestOutboundLedger:
    def test_one_logical_send_is_claimed_once(self, tmp_path):
        calls = []

        def send(req):
            calls.append(req)
            return SmsSendResult("opaque-1", "accepted", 1)

        config = _config(tmp_path)
        providers = _providers(_adapter(send))
        first = asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:1", user_id="alice", text="done"
        ))
        second = asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:1", user_id="alice", text="changed"
        ))

        assert first.status == second.status == "accepted"
        assert len(calls) == 1
        with db.get_db(config.db_path) as conn:
            row = conn.execute("SELECT * FROM sent_sms").fetchone()
            assert row["provider_message_id"] == "opaque-1"
            assert row["body_sha256"]
            assert "done" not in tuple(row)

    def test_concurrent_delivery_has_one_provider_attempt(self, tmp_path):
        calls = 0
        started = threading.Event()
        release = threading.Event()

        def send(_req):
            nonlocal calls
            calls += 1
            started.set()
            assert release.wait(2)
            return SmsSendResult("opaque-1", "accepted", 1)

        config = _config(tmp_path)
        providers = _providers(_adapter(send))

        async def run_both():
            first = asyncio.create_task(deliver_sms(
                config, providers, logical_key="task-result:1",
                user_id="alice", text="done",
            ))
            assert await asyncio.to_thread(started.wait, 2)
            second = await deliver_sms(
                config, providers, logical_key="task-result:1",
                user_id="alice", text="done",
            )
            release.set()
            return await first, second

        first, second = asyncio.run(run_both())

        assert first.status == "accepted"
        assert second.status == "pending"
        assert calls == 1

    def test_unclaimed_pending_row_can_be_claimed(self, tmp_path):
        calls = 0

        def send(_req):
            nonlocal calls
            calls += 1
            return SmsSendResult("opaque-1", "accepted", 1)

        config = _config(tmp_path)
        providers = _providers(_adapter(send))
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO sent_sms (logical_key, provider, user_id, to_number, "
                "from_number, status, estimated_segments, body_chars, body_sha256, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', 1, 4, ?, "
                "datetime('now'), datetime('now'))",
                ("task-result:1", "twilio", "alice", USER_NUMBER, SERVICE_NUMBER, "hash"),
            )

        result = asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:1", user_id="alice", text="done"
        ))

        assert result.status == "accepted"
        assert calls == 1

    def test_unclaimed_pending_row_moves_to_active_provider(self, tmp_path):
        calls = []
        config = _config(tmp_path)
        config.sms.provider = "telnyx"
        adapter = _named_adapter(
            "telnyx",
            lambda req: calls.append(req) or SmsSendResult("opaque-new", "accepted", 1),
        )
        providers = SmsProviderRegistry(
            active_name="telnyx", adapters={"telnyx": adapter},
        )
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO sent_sms (logical_key, provider, user_id, to_number, "
                "from_number, status, estimated_segments, body_chars, body_sha256, "
                "created_at, updated_at) VALUES (?, ?, ?, ?, ?, 'pending', 1, 4, ?, "
                "datetime('now'), datetime('now'))",
                ("task-result:1", "twilio", "alice", USER_NUMBER, SERVICE_NUMBER, "hash"),
            )

        result = asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:1", user_id="alice", text="done"
        ))

        assert result.provider == "telnyx"
        assert len(calls) == 1

    def test_post_acceptance_database_failure_becomes_unknown(
        self, tmp_path, monkeypatch
    ):
        from istota.transport.sms import outbound

        config = _config(tmp_path)
        providers = _providers(_adapter(
            lambda _req: SmsSendResult("opaque-accepted", "accepted", 1)
        ))
        original = outbound._set_outcome
        failed_once = False

        def fail_once(*args, **kwargs):
            nonlocal failed_once
            if not failed_once and args[2] == "accepted":
                failed_once = True
                raise OSError("one-shot database failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(outbound, "_set_outcome", fail_once)
        result = asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:1", user_id="alice", text="done"
        ))

        assert result.status == "unknown"
        with db.get_db(config.db_path) as conn:
            row = conn.execute("SELECT status, attempted_at FROM sent_sms").fetchone()
            assert row["status"] == "unknown"
            assert row["attempted_at"] is not None

    @pytest.mark.parametrize(
        "failure, status",
        [
            (SmsSendFailure(True, "rejected", False, "request rejected"), "failed"),
            (SmsSendFailure(False, None, False, "delivery outcome unknown"), "unknown"),
        ],
    )
    def test_normalizes_failures_without_resend(self, tmp_path, failure, status):
        calls = 0

        def send(_req):
            nonlocal calls
            calls += 1
            return failure

        config = _config(tmp_path)
        providers = _providers(_adapter(send))
        result = asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:1", user_id="alice", text="done"
        ))
        again = asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:1", user_id="alice", text="done"
        ))

        assert result.status == again.status == status
        assert calls == 1

    def test_opt_out_and_removed_binding_never_call_provider(self, tmp_path):
        def send(_req):
            raise AssertionError("provider must not be called")

        config = _config(tmp_path)
        providers = _providers(_adapter(send))
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO sms_opt_outs VALUES (?, datetime('now'), datetime('now'))",
                (USER_NUMBER,),
            )
        blocked = asyncio.run(deliver_sms(
            config, providers, logical_key="blocked", user_id="alice", text="no"
        ))
        config.users["alice"].sms_phone_number = ""
        missing = asyncio.run(deliver_sms(
            config, providers, logical_key="missing", user_id="alice", text="no"
        ))

        assert blocked.status == "blocked_opt_out"
        assert missing.status == "unconfigured"


class TestDeliveryEvents:
    def test_status_callbacks_advance_without_regression_and_report_segments(self, tmp_path):
        config = _config(tmp_path)
        providers = _providers(_adapter(lambda _req: SmsSendResult("opaque-1", "accepted", 2)))
        asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:1", user_id="alice", text="done"
        ))

        sent = asyncio.run(_handle(
            config, providers,
            SmsDeliveryEvent("twilio", "event-sent", "opaque-1", "sent", None, None),
        ))
        stale = asyncio.run(_handle(
            config, providers,
            SmsDeliveryEvent("twilio", "event-queued", "opaque-1", "queued", None, None),
        ))
        delivered = asyncio.run(_handle(
            config, providers,
            SmsDeliveryEvent("twilio", "event-delivered", "opaque-1", "delivered", None, 3),
        ))

        assert sent.disposition == "delivery_updated"
        assert stale.disposition == "delivery_stale"
        assert delivered.disposition == "delivery_updated"
        with db.get_db(config.db_path) as conn:
            row = conn.execute("SELECT * FROM sent_sms").fetchone()
            assert row["status"] == "delivered"
            assert row["reported_segments"] == 3
            assert row["provider_event_id"] == "event-delivered"

    def test_failed_callback_enqueues_alert_in_transaction_and_pushes_after(
        self, tmp_path, monkeypatch
    ):
        """The write is in the transaction; the push is after it, and happens.

        Two halves, and the second is the one that was missing: the alert was
        written and returned to nobody, so a `failed` callback left a row the
        bell would show and pushed nothing — while the identical failure on the
        *send* path did push. Asserting only "no delivery" passes equally
        against that bug and against the fix, so each half is pinned
        separately: `send_notification` is forbidden while the transaction is
        open, and required once it has committed.
        """
        config = _config(tmp_path)
        providers = _providers(_adapter(lambda _req: SmsSendResult("opaque-1", "sent", 1)))
        asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:1", user_id="alice", text="done"
        ))
        config.users["alice"].routing = {"alert": "ntfy"}
        event = SmsDeliveryEvent("twilio", "event-failed", "opaque-1", "failed", "30007", None)

        monkeypatch.setattr(
            notifications, "send_notification",
            lambda *_a, **_k: pytest.fail("no push while the transaction is open"),
        )
        with db.get_db(config.db_path) as conn:
            result = handle_provider_event(
                conn, config, event, active_provider_ready=True,
            )
        assert result.disposition == "delivery_updated"
        assert result.pending_alert is not None

        pushed = []
        monkeypatch.setattr(
            notifications, "send_notification",
            lambda *_a, **kw: pushed.append(kw.get("surface")) or True,
        )
        asyncio.run(deliver_event_response(config, providers, result))

        assert pushed, "the committed alert was never pushed"
        assert all("sms" not in (s or "") for s in pushed), (
            "an SMS failure must not be reported over SMS"
        )
        with db.get_db(config.db_path) as conn:
            assert conn.execute(
                "SELECT count(*) FROM notifications WHERE source = 'task_alert'"
            ).fetchone()[0] == 1

    def test_task_log_distinguishes_acceptance_from_delivery(self, tmp_path, caplog):
        caplog.set_level("INFO")
        config = _config(tmp_path)
        providers = _providers(_adapter(lambda _req: SmsSendResult("opaque-1", "accepted", 1)))
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="check", user_id="alice", source_type="sms",
                conversation_token=sms_conversation_token("alice"), output_target="sms",
            )
        asyncio.run(deliver_sms(
            config, providers, logical_key=f"task-result:{task_id}",
            user_id="alice", text="done", task_id=task_id,
        ))
        asyncio.run(_handle(
            config, providers,
            SmsDeliveryEvent("twilio", "event-delivered", "opaque-1", "delivered", None, 1),
        ))

        with db.get_db(config.db_path) as conn:
            messages = [
                row[0] for row in conn.execute(
                    "SELECT message FROM task_logs WHERE task_id = ? ORDER BY id", (task_id,)
                )
            ]
        assert messages == ["SMS accepted by provider", "SMS delivered"]
        assert "sms.outbound.accepted" in caplog.text
        assert "sms.delivery.updated" in caplog.text


class TestNotificationsAndIsolation:
    def test_sms_configuration_probe_checks_binding_and_opt_out(
        self, tmp_path, monkeypatch
    ):
        config = _config(tmp_path)
        providers = _providers(_adapter(lambda _req: SmsSendResult("opaque", "accepted", 1)))
        from istota.transport.sms import outbound
        original = outbound.is_sms_configured
        monkeypatch.setattr(
            outbound, "is_sms_configured",
            lambda cfg, user_id: original(cfg, user_id, providers),
        )
        assert notifications.is_channel_configured(config, "alice", "sms") is True
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO sms_opt_outs VALUES (?, datetime('now'), datetime('now'))",
                (USER_NUMBER,),
            )
        assert notifications.is_channel_configured(config, "alice", "sms") is False

    def test_notification_dispatch_uses_fake_adapter_and_sms_failure_does_not_recurse(
        self, tmp_path, monkeypatch
    ):
        calls = []

        def send(req):
            calls.append(req.text)
            if len(calls) == 1:
                return SmsSendResult("opaque-notice", "accepted", 1)
            return SmsSendFailure(True, "rejected", False, "request rejected")

        config = _config(tmp_path)
        config.users["alice"].routing = {"alert": "sms"}
        providers = _providers(_adapter(send))
        monkeypatch.setattr(
            "istota.transport.sms.providers.registry.make_provider_registry",
            lambda _config: providers,
        )

        assert notifications.send_notification(
            config, "alice", "ordinary notice", surface="sms"
        ) is True
        failed = asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:failed",
            user_id="alice", text="failed result", task_id=42,
        ))

        assert failed.status == "failed"
        assert calls == ["ordinary notice", "failed result"]
        with db.get_db(config.db_path) as conn:
            assert conn.execute(
                "SELECT count(*) FROM notifications WHERE source = 'task_alert'"
            ).fetchone()[0] == 1

    def test_sms_notification_keeps_title_and_stable_reference_blocks_retry(
        self, tmp_path, monkeypatch
    ):
        calls = []
        outcomes = [
            SmsSendFailure(False, None, False, "delivery outcome unknown"),
            SmsSendResult("must-not-send", "accepted", 1),
        ]
        config = _config(tmp_path)
        config.users["alice"].routing = {"alert": "sms"}
        providers = _providers(_adapter(lambda req: calls.append(req.text) or outcomes.pop(0)))
        monkeypatch.setattr(
            "istota.transport.sms.providers.registry.make_provider_registry",
            lambda _config: providers,
        )

        first = notifications.send_notification(
            config, "alice", "Title\n\nBody", surface="sms", title="Title",
            reference_id="notification:17",
        )
        second = notifications.send_notification(
            config, "alice", "Title\n\nBody", surface="sms", title="Title",
            reference_id="notification:17",
        )
        title_only = notifications.send_notification(
            config, "alice", "", surface="sms", title="Title only",
            reference_id="notification:18",
        )

        assert first is second is False
        assert title_only is True
        assert calls == ["Title\n\nBody", "Title only"]
        with db.get_db(config.db_path) as conn:
            rows = conn.execute("SELECT logical_key, status FROM sent_sms").fetchall()
            assert [tuple(row) for row in rows] == [
                ("notification:17", "unknown"),
                ("notification:18", "accepted"),
            ]

    def test_failure_alert_dispatches_off_the_persistent_runtime_loop(
        self, tmp_path, monkeypatch
    ):
        from istota.async_runtime import run_coro

        config = _config(tmp_path)
        providers = _providers(_adapter(
            lambda _req: SmsSendFailure(True, "rejected", False, "request rejected")
        ))

        async def sent_talk(*_args, **_kwargs):
            return 7

        monkeypatch.setattr(notifications, "_send_talk", sent_talk)
        monkeypatch.setattr(
            notifications, "resolve_conversation_token", lambda *_args: "talk-token",
        )

        result = run_coro(deliver_sms(
            config, providers, logical_key="task-result:1",
            user_id="alice", text="done",
        ))

        assert result.status == "failed"

    def test_common_modules_do_not_name_provider_payload_or_credentials(self):
        from tests.support.drift import source_of
        from istota.transport.sms import outbound, webhook

        common = source_of(outbound) + source_of(webhook)
        for forbidden in (
            "AccountSid", "MessagingServiceSid", "MessageSid", "OptOutType",
            "messaging_profile_id", "autoresponse_type", "telnyx-signature-ed25519",
        ):
            assert forbidden not in common


class TestSchedulerSmsDelivery:
    def test_completed_task_delivers_once_through_sms_ledger(self, tmp_path, monkeypatch):
        calls = []

        def send(req):
            calls.append(req.text)
            return SmsSendResult("opaque-result", "accepted", 1)

        config = _config(tmp_path)
        providers = _providers(_adapter(send))
        monkeypatch.setattr(
            "istota.transport.sms.providers.registry.make_provider_registry", lambda _config: providers
        )
        monkeypatch.setattr(
            "istota.scheduler.execute_task", lambda *_args, **_kwargs: (
                True, "Finished the check.", None, None,
            ),
        )
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="check", user_id="alice", source_type="sms",
                conversation_token=sms_conversation_token("alice"), output_target="sms",
            )

        from istota.scheduler import process_one_task
        assert process_one_task(config) == (task_id, True)
        assert calls == ["Finished the check."]
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "completed"
            row = conn.execute("SELECT * FROM sent_sms").fetchone()
            assert row["logical_key"] == f"task-result:{task_id}"

    def test_sms_confirmation_parks_and_includes_answer_instruction(
        self, tmp_path, monkeypatch
    ):
        calls = []

        def send(req):
            calls.append(req.text)
            return SmsSendResult("opaque-confirm", "accepted", 1)

        config = _config(tmp_path)
        providers = _providers(_adapter(send))
        monkeypatch.setattr(
            "istota.transport.sms.providers.registry.make_provider_registry", lambda _config: providers
        )
        monkeypatch.setattr(
            "istota.scheduler.execute_task", lambda *_args, **_kwargs: (
                True, "I need your confirmation before deleting the file.", None, None,
            ),
        )
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="delete", user_id="alice", source_type="sms",
                conversation_token=sms_conversation_token("alice"), output_target="sms",
            )

        from istota.scheduler import process_one_task
        assert process_one_task(config) == (task_id, True)
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending_confirmation"
            row = conn.execute("SELECT logical_key FROM sent_sms").fetchone()
            notification_id = conn.execute(
                "SELECT id FROM notifications WHERE source = 'confirmation'"
            ).fetchone()[0]
            assert row["logical_key"] == f"confirmation:{notification_id}"
        assert len(calls) == 1
        assert f"Task #{task_id}. Reply YES or NO." in calls[0]

    def test_a_prompt_never_claims_the_task_result_key_when_no_row_is_written(
        self, tmp_path, monkeypatch
    ):
        # `write_notification` never raises — it returns None — so the
        # confirmation row is not guaranteed to exist. With the prompt's key
        # derived only from that row, it fell through to `task-result:`, which
        # belongs to the task's own final answer. `sent_sms.logical_key` is
        # UNIQUE and the ledger is one-send, so the answer the user had just
        # approved was refused against the settled prompt row and never sent,
        # permanently and with no retry (ISSUE-489).
        from istota import confirmations
        from istota.scheduler import process_one_task

        calls = []

        def send(req):
            calls.append(req.text)
            return SmsSendResult(f"opaque-{len(calls)}", "accepted", 1)

        config = _config(tmp_path)
        providers = _providers(_adapter(send))
        monkeypatch.setattr(
            "istota.transport.sms.providers.registry.make_provider_registry",
            lambda _config: providers,
        )
        monkeypatch.setattr(
            "istota.scheduler.confirmation_source.write",
            lambda *_args, **_kwargs: None,
        )
        answers = iter([
            (True, "I need your confirmation before deleting the file.", None, None),
            (True, "Deleted the file.", None, None),
        ])
        monkeypatch.setattr(
            "istota.scheduler.execute_task", lambda *_args, **_kwargs: next(answers),
        )
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="delete", user_id="alice", source_type="sms",
                conversation_token=sms_conversation_token("alice"),
                output_target="sms",
            )

        assert process_one_task(config) == (task_id, True)
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending_confirmation"
            assert conn.execute(
                "SELECT logical_key FROM sent_sms"
            ).fetchone()["logical_key"] == f"confirmation-task:{task_id}"

        # The user says yes, and the answer has to arrive.
        with db.get_db(config.db_path) as conn:
            confirmations.apply_answer(
                conn, db.get_task(conn, task_id),
                confirmations.Answer(approve=True, trust_sender=False),
            )
            conn.commit()
        assert process_one_task(config) == (task_id, True)

        assert calls[-1] == "Deleted the file."
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "completed"
            keys = [
                row["logical_key"] for row in conn.execute(
                    "SELECT logical_key FROM sent_sms ORDER BY id"
                )
            ]
        assert keys == [f"confirmation-task:{task_id}", f"task-result:{task_id}"]

    def test_a_fallback_key_cannot_swallow_another_tasks_prompt(
        self, tmp_path, monkeypatch
    ):
        # `logical_key` is UNIQUE across the whole table and nothing deletes
        # from it, so a claim is permanent. A task id and a notification id are
        # independent small integers, so sharing one `confirmation:` prefix let
        # task 1's row-less fallback claim the very key task 2's notification
        # row would derive — and the second prompt came back `accepted` off the
        # settled row, so nothing reported it undelivered and the task waited on
        # a question nobody had seen (ISSUE-489 review).
        from istota.scheduler import process_one_task

        calls = []

        def send(req):
            calls.append(req.text)
            return SmsSendResult(f"opaque-{len(calls)}", "accepted", 1)

        config = _config(tmp_path)
        config.users["bob"] = UserConfig(sms_phone_number="+15551239999")
        providers = _providers(_adapter(send))
        monkeypatch.setattr(
            "istota.transport.sms.providers.registry.make_provider_registry",
            lambda _config: providers,
        )
        monkeypatch.setattr(
            "istota.scheduler.execute_task", lambda *_args, **_kwargs: (
                True, "I need your confirmation before deleting the file.",
                None, None,
            ),
        )
        with db.get_db(config.db_path) as conn:
            first = db.create_task(
                conn, prompt="delete", user_id="alice", source_type="sms",
                conversation_token=sms_conversation_token("alice"),
                output_target="sms",
            )

        # Task 1 parks with no notification row, so it takes the fallback.
        write = confirmation_source.write
        monkeypatch.setattr(
            "istota.scheduler.confirmation_source.write",
            lambda *_args, **_kwargs: None,
        )
        assert process_one_task(config) == (first, True)
        monkeypatch.setattr("istota.scheduler.confirmation_source.write", write)

        # Task 2 parks normally. Its notification row is the first ever written,
        # so `notifications.id` is 1 — the same integer as task 1's id.
        with db.get_db(config.db_path) as conn:
            second = db.create_task(
                conn, prompt="delete", user_id="bob", source_type="sms",
                conversation_token=sms_conversation_token("bob"),
                output_target="sms",
            )
        assert process_one_task(config) == (second, True)

        with db.get_db(config.db_path) as conn:
            notification_id = conn.execute(
                "SELECT id FROM notifications WHERE source = 'confirmation'"
            ).fetchone()[0]
            keys = [
                row["logical_key"] for row in conn.execute(
                    "SELECT logical_key FROM sent_sms ORDER BY id"
                )
            ]
        # The collision this guards is only meaningful while the two integers
        # are equal, so assert that rather than assuming it.
        assert notification_id == first
        assert keys == [
            f"confirmation-task:{first}", f"confirmation:{notification_id}",
        ]
        assert len(calls) == 2


class TestTheDeliveryPathStaysOffTheRuntimeLoop:
    def test_deliver_sms_opens_no_database_connection_on_the_calling_loop(
        self, tmp_path, monkeypatch
    ):
        """`deliver_sms` must not touch SQLite on the thread awaiting it.

        Two callers submit this to the process-global runtime loop through
        `run_coro` — the loop the Talk poller runs on. A synchronous
        `db.get_db` there waits for the WAL write lock from inside the loop
        thread, so a coroutine already holding that lock can never be resumed
        and the whole runtime stalls until the 30s busy timeout. `.claude/
        rules/transport.md` states the rule; `WebTransport.deliver` is the
        existing implementation of it.

        Asserting "it still returns a record" would pass either way, so this
        records the *thread* of every connection open instead.
        """
        from istota import sqlite_util

        config = _config(tmp_path)
        providers = _providers(_adapter(lambda _req: SmsSendResult("opaque-1", "sent", 1)))
        opens: list[int] = []
        real_open = sqlite_util.open_db

        def recording_open(*args, **kwargs):
            opens.append(threading.get_ident())
            return real_open(*args, **kwargs)

        monkeypatch.setattr(sqlite_util, "open_db", recording_open)

        async def drive():
            loop_thread = threading.get_ident()
            await deliver_sms(
                config, providers, logical_key="task-result:9",
                user_id="alice", text="done",
            )
            return loop_thread

        loop_thread = asyncio.run(drive())

        assert opens, "the probe recorded nothing; deliver_sms opened no database"
        assert loop_thread not in opens, (
            "deliver_sms opened a SQLite connection on the awaiting loop thread"
        )


class TestAnUnconfiguredSmsBindingIsRecordedNotDropped:
    def _unbound(self, tmp_path):
        config = _config(tmp_path)
        config.users["alice"].sms_phone_number = ""
        return config

    def test_the_plan_keeps_the_sms_leg_so_the_failure_is_recorded(self, tmp_path):
        """A dropped destination empties the plan, and an empty plan is silent.

        The answer is discarded with nothing but a daemon WARNING: no ledger
        row, no `unconfigured` status, and no task alert — while the spec asks
        for the last two by name.
        """
        config = self._unbound(tmp_path)
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="check", user_id="alice", source_type="sms",
                conversation_token=sms_conversation_token("alice"), output_target="sms",
            )
            task = db.get_task(conn, task_id)

        plan = resolve_delivery_plan(config, task, make_registry(config))

        assert [d.surface for d in plan] == ["sms"]
        assert plan[0].channel in (None, "")

    def test_an_unbound_user_parks_a_confirmation_instead_of_completing_it(
        self, tmp_path, monkeypatch
    ):
        """The consequence that is worse than a lost answer.

        With the leg dropped, `_own_origin_sms` is False, so the task is not a
        confirmable surface and a "may I delete this?" completes — which
        applies its deferred ops — instead of parking for an answer.
        """
        config = self._unbound(tmp_path)
        providers = _providers(_adapter(lambda _req: SmsSendResult("x", "accepted", 1)))
        monkeypatch.setattr(
            "istota.transport.sms.providers.registry.make_provider_registry",
            lambda _config: providers,
        )
        monkeypatch.setattr(
            "istota.scheduler.execute_task", lambda *_a, **_k: (
                True, "I need your confirmation before deleting the file.", None, None,
            ),
        )
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="delete", user_id="alice", source_type="sms",
                conversation_token=sms_conversation_token("alice"), output_target="sms",
            )

        from istota.scheduler import process_one_task
        assert process_one_task(config) == (task_id, True)

        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending_confirmation"
            row = conn.execute("SELECT status FROM sent_sms").fetchone()
        assert row is not None, "no ledger row: the SMS leg was dropped"
        assert row["status"] == "unconfigured"


class TestAWithheldConfirmationIsOwedBackWhenTheSmsFails:
    def test_a_failed_confirmation_send_pushes_the_held_notification(
        self, tmp_path, monkeypatch
    ):
        """The SMS half of ISSUE-404's owed-notification arm.

        An SMS-origin confirmation withholds its notification at the park
        because `post_sms_message` is set. If the send then reaches nobody, the
        question is pushed nowhere and the task sits parked until
        `expire_stale_confirmations` kills it two hours later. The separate
        `sms-failure` alert says an SMS failed; it carries neither the question
        nor its `!confirm` verbs, so it is not a substitute.
        """
        config = _config(tmp_path)
        providers = _providers(_adapter(
            lambda _req: SmsSendFailure(True, "30007", False, "provider rejected message")
        ))
        monkeypatch.setattr(
            "istota.transport.sms.providers.registry.make_provider_registry",
            lambda _config: providers,
        )
        monkeypatch.setattr(
            "istota.scheduler.execute_task", lambda *_a, **_k: (
                True, "I need your confirmation before deleting the file.", None, None,
            ),
        )
        pushed = []
        monkeypatch.setattr(
            "istota.scheduler.deliver_pending",
            lambda _config, results: pushed.extend(r for r in results if r is not None),
        )
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="delete", user_id="alice", source_type="sms",
                conversation_token=sms_conversation_token("alice"), output_target="sms",
            )

        from istota.scheduler import process_one_task
        assert process_one_task(config) == (task_id, True)

        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending_confirmation"
            sources = [
                r[0] for r in conn.execute(
                    "SELECT source FROM notifications"
                ).fetchall()
            ]
        assert "confirmation" in sources
        assert pushed, "the withheld confirmation was never owed back"


class TestATextedAnswerResolvesOnlyItsOwnConversation:
    """A phone number must not be able to answer another surface's question.

    `confirmations.resolve` falls through to Path C — the user's single open
    question on *any* surface — which is right for Talk and web and wrong here:
    the number is the weakest credential any surface authenticates with, and a
    texted `Y` reaching Path C approves the untrusted-email gate and can write
    a permanent `trust_sender` row for the sender parked in it.
    """

    def _config_with_sms(self, tmp_path):
        return _config(tmp_path)

    def test_a_texted_yes_does_not_approve_an_email_origin_confirmation(
        self, tmp_path
    ):
        config = self._config_with_sms(tmp_path)
        with db.get_db(config.db_path) as conn:
            email_task = db.create_task(
                conn, prompt="act on this mail", user_id="alice",
                source_type="email", conversation_token="email-thread-1",
            )
            db.set_task_confirmation(conn, email_task, "May I trust this sender?")

        result = asyncio.run(_handle(config, _providers(_adapter(
            lambda _req: SmsSendResult("opaque-1", "accepted", 1)
        )), _inbound(text="yes")))

        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, email_task).status == "pending_confirmation", (
                "a texted yes approved a confirmation parked by another surface"
            )
        # Not an answer to anything, so it is an ordinary turn: a new task.
        assert result.disposition == "task"

    def test_a_texted_yes_still_answers_its_own_parked_question(self, tmp_path):
        """The control for the narrowing above: it must not refuse everything."""
        config = self._config_with_sms(tmp_path)
        token = sms_conversation_token("alice")
        with db.get_db(config.db_path) as conn:
            sms_task = db.create_task(
                conn, prompt="delete it", user_id="alice", source_type="sms",
                conversation_token=token, output_target="sms",
            )
            db.set_task_confirmation(conn, sms_task, "May I delete it?")

        result = asyncio.run(_handle(config, _providers(_adapter(
            lambda _req: SmsSendResult("opaque-2", "accepted", 1)
        )), _inbound(text="yes")))

        assert result.disposition == "confirmation_answer"
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, sms_task).status != "pending_confirmation"


# ---------------------------------------------------------------------------
# A status callback that overtakes our own provider_message_id write
# ---------------------------------------------------------------------------


def _parked(config):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT provider, provider_message_id, status, error_code, "
            "reported_segments, opted_out, parked_at "
            "FROM sms_parked_status ORDER BY id"
        ).fetchall()


def _sent_rows(config):
    with db.get_db(config.db_path) as conn:
        return conn.execute("SELECT * FROM sent_sms ORDER BY id").fetchall()


def _delivery(status="sent", message_id="opaque-1", **changes):
    values = dict(
        provider="twilio",
        provider_event_id=f"event-{status}",
        provider_message_id=message_id,
        status=status,
        error_code=None,
        reported_segments=None,
    )
    values.update(changes)
    return SmsDeliveryEvent(**values)


def _callback_during_send(config, box, *events):
    """An adapter whose `send` fires status callbacks before it answers.

    The race, made deterministic. At this moment the ledger row is claimed
    `pending` and the provider's message id has not been written, because the
    provider mints it in the reply this call has not yet returned.
    """
    dispositions: list[str] = []

    def send(_request):
        for event in events:
            with db.get_db(config.db_path) as conn:
                result = handle_provider_event(
                    conn, config, event, active_provider_ready=True,
                )
            dispositions.append(result.disposition)
        return SmsSendResult("opaque-1", "accepted", 1)

    box.append(_providers(_adapter(send)))
    return dispositions


class TestAnSmsStatusThatOvertakesTheIdWrite:
    def test_a_failed_status_still_fails_the_row_and_alerts(self, tmp_path):
        config = _config(tmp_path)
        box: list = []
        dispositions = _callback_during_send(
            config, box, _delivery(status="failed", error_code="30006"),
        )

        record = asyncio.run(deliver_sms(
            config, box[0], logical_key="task-result:1", user_id="alice",
            text="done",
        ))

        assert dispositions == ["delivery_parked"]
        row = _sent_rows(config)[0]
        assert row["status"] == "failed"
        assert row["error_code"] == "30006"
        # The caller is told the send failed, not that the provider took it.
        assert record.status == "failed"
        with db.get_db(config.db_path) as conn:
            alerts = conn.execute("SELECT source FROM notifications").fetchall()
        assert [alert["source"] for alert in alerts] == ["task_alert"]
        assert _parked(config) == []

    def test_parked_statuses_apply_in_arrival_order_and_stop_at_failed(
        self, tmp_path,
    ):
        # Replayed through `apply_delivery_event`, so the monotonic ladder is
        # what stops the trailing `delivered` reopening a row the `failed`
        # closed.
        config = _config(tmp_path)
        box: list = []
        dispositions = _callback_during_send(
            config, box,
            _delivery(status="sent"),
            _delivery(status="failed", error_code="30006"),
            _delivery(status="delivered"),
        )

        asyncio.run(deliver_sms(
            config, box[0], logical_key="task-result:1", user_id="alice",
            text="done",
        ))

        assert dispositions == ["delivery_parked"] * 3
        row = _sent_rows(config)[0]
        assert row["status"] == "failed" and row["error_code"] == "30006"
        with db.get_db(config.db_path) as conn:
            count = conn.execute("SELECT count(*) FROM notifications").fetchone()[0]
        assert count == 1

    def test_a_parked_opt_out_still_stores_the_opt_out(self, tmp_path):
        # `opted_out` is a side effect the ladder performs on the row's own
        # number, so losing a parked one silently keeps texting somebody who
        # asked to stop.
        config = _config(tmp_path)
        box: list = []
        _callback_during_send(
            config, box, _delivery(status="failed", opted_out=True),
        )

        asyncio.run(deliver_sms(
            config, box[0], logical_key="task-result:1", user_id="alice",
            text="done",
        ))

        with db.get_db(config.db_path) as conn:
            numbers = [
                row["phone_number"]
                for row in conn.execute("SELECT phone_number FROM sms_opt_outs")
            ]
        assert numbers == [USER_NUMBER]

    def test_a_status_with_no_send_in_flight_is_discarded_as_before(self, tmp_path):
        # The bound. Parking every unmatched id would hand a number previously
        # used by another application an unbounded write.
        config = _config(tmp_path)
        providers = _providers(_adapter(
            lambda _req: SmsSendResult("opaque-1", "accepted", 1),
        ))
        asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:1", user_id="alice",
            text="done",
        ))

        result = asyncio.run(_handle(
            config, providers, _delivery(status="failed", message_id="nothing"),
        ))

        assert result.disposition == "delivery_unknown"
        assert _parked(config) == []

    def test_a_status_for_another_provider_is_not_parked(self, tmp_path):
        # The in-flight test is scoped by provider, which the SMS ledger can do
        # and the WhatsApp one cannot: the event names its provider and the
        # ledger key is the pair.
        config = _config(tmp_path)
        box: list = []
        dispositions = _callback_during_send(
            config, box, _delivery(status="failed", provider="telnyx"),
        )

        asyncio.run(deliver_sms(
            config, box[0], logical_key="task-result:1", user_id="alice",
            text="done",
        ))

        assert dispositions == ["delivery_unknown"]
        assert _parked(config) == []
        assert _sent_rows(config)[0]["status"] == "accepted"

    def test_the_window_is_pruned_even_when_nothing_is_in_flight(self, tmp_path):
        # The prune runs before the in-flight gate. Behind it, only a
        # successful park clears the table, so a deployment that went quiet —
        # where a stranded row is most likely — keeps its rows for good.
        config = _config(tmp_path)
        providers = _providers(_adapter(
            lambda _req: SmsSendResult("opaque-1", "accepted", 1),
        ))
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO sms_parked_status (provider, provider_message_id, "
                "status, parked_at) VALUES (?, ?, ?, datetime('now', '-1 day'))",
                ("twilio", "stranded", "failed"),
            )

        result = asyncio.run(_handle(
            config, providers, _delivery(status="failed", message_id="nothing"),
        ))

        assert result.disposition == "delivery_unknown"
        assert _parked(config) == []

    def test_a_settled_row_is_never_reopened_by_a_second_outcome(self, tmp_path):
        # `_deliver_sms_blocking` writes an outcome on several paths, and a
        # replayed status can have taken the row terminal in between. The write
        # refuses a row already terminal rather than trusting its callers.
        from istota.transport.sms.outbound import _set_outcome

        config = _config(tmp_path)
        providers = _providers(_adapter(
            lambda _req: SmsSendResult("opaque-1", "accepted", 1),
        ))
        asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:1", user_id="alice",
            text="done",
        ))
        asyncio.run(_handle(
            config, providers, _delivery(status="failed", error_code="30006"),
        ))

        record = _set_outcome(config, "task-result:1", "unknown")

        assert record.status == "failed" and record.error_code == "30006"
        assert _sent_rows(config)[0]["status"] == "failed"

    def test_a_failing_replay_never_costs_the_accepted_id(self, tmp_path, monkeypatch):
        # The replay is its own transaction, after the outcome has committed.
        # Sharing one would let a raise in a replay unwind the
        # `provider_message_id` write, and a message the provider accepted
        # whose id was never recorded can never be matched by a later status.
        from istota.transport.sms import outbound as outbound_module

        config = _config(tmp_path)
        box: list = []
        _callback_during_send(config, box, _delivery(status="failed"))

        def boom(*_args, **_kwargs):
            raise RuntimeError("replay exploded")

        monkeypatch.setattr(outbound_module, "_drain_parked_statuses", boom)
        record = asyncio.run(deliver_sms(
            config, box[0], logical_key="task-result:1", user_id="alice",
            text="done",
        ))

        row = _sent_rows(config)[0]
        assert row["status"] == "accepted"
        assert row["provider_message_id"] == "opaque-1"
        assert record.status == "accepted"


    def test_a_drained_failure_alert_is_never_pushed_over_sms(
        self, tmp_path, monkeypatch,
    ):
        # Routing names SMS and nothing else, which is what makes this
        # discriminate: the surface is filtered out and the push reaches
        # nobody. `deliver_pending` would send it happily, to the number whose
        # message just failed. The neighbouring callback test routes alerts to
        # ntfy, so its "no sms" assertion holds whichever call is used.
        config = _config(tmp_path)
        config.users["alice"].routing = {"alert": "sms"}
        box: list = []
        _callback_during_send(
            config, box, _delivery(status="failed", error_code="30006"),
        )
        pushed = []
        monkeypatch.setattr(
            notifications, "send_notification",
            lambda *_a, **kw: pushed.append(kw.get("surface")) or True,
        )

        asyncio.run(deliver_sms(
            config, box[0], logical_key="task-result:1", user_id="alice",
            text="done",
        ))

        assert pushed == [], f"an SMS failure was reported over SMS: {pushed}"
        assert _sent_rows(config)[0]["status"] == "failed"

    def test_a_callback_failure_alert_is_never_pushed_over_sms(
        self, tmp_path, monkeypatch,
    ):
        # The same rule on the webhook's own path, which had the same defect
        # before this change and was covered by a test that routed away from
        # SMS.
        config = _config(tmp_path)
        config.users["alice"].routing = {"alert": "sms"}
        providers = _providers(_adapter(
            lambda _req: SmsSendResult("opaque-1", "accepted", 1),
        ))
        asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:1", user_id="alice",
            text="done",
        ))
        pushed = []
        monkeypatch.setattr(
            notifications, "send_notification",
            lambda *_a, **kw: pushed.append(kw.get("surface")) or True,
        )

        asyncio.run(_handle(
            config, providers, _delivery(status="failed", error_code="30006"),
        ))

        assert pushed == [], f"an SMS failure was reported over SMS: {pushed}"

    def test_a_second_callback_for_one_status_merges_rather_than_drops(
        self, tmp_path,
    ):
        # Not a redelivery: the same status seen twice, with the error code and
        # the segment count only on the second. The park must not invert
        # `apply_delivery_event`'s own latest-non-NULL-wins rule for the length
        # of the window.
        config = _config(tmp_path)
        box: list = []
        _callback_during_send(
            config, box,
            _delivery(status="failed"),
            _delivery(status="failed", error_code="30006", reported_segments=2),
        )

        asyncio.run(deliver_sms(
            config, box[0], logical_key="task-result:1", user_id="alice",
            text="done",
        ))

        row = _sent_rows(config)[0]
        assert row["status"] == "failed"
        assert row["error_code"] == "30006"
        assert row["reported_segments"] == 2

    def test_a_missing_parked_table_costs_the_status_not_the_request(
        self, tmp_path,
    ):
        # The deployment shape the guard exists for: new code against a
        # database `init_db` has not re-run over. The park used to be a bare
        # return and could not fail; it must not now turn a callback the route
        # answered into a 503 the provider retries into the same failure.
        config = _config(tmp_path)
        box: list = []
        dispositions = _callback_during_send(config, box, _delivery(status="failed"))
        with db.get_db(config.db_path) as conn:
            conn.execute("DROP TABLE sms_parked_status")

        asyncio.run(deliver_sms(
            config, box[0], logical_key="task-result:1", user_id="alice",
            text="done",
        ))

        assert dispositions == ["delivery_unknown"]
        assert _sent_rows(config)[0]["status"] == "accepted"

    def test_the_window_is_pruned_on_the_drain_too(self, tmp_path):
        # Both paths that touch the table prune it. Only the park path is
        # covered by the sibling above; this one drains without ever parking,
        # by planting the parked row directly.
        config = _config(tmp_path)
        providers = _providers(_adapter(
            lambda _req: SmsSendResult("opaque-1", "accepted", 1),
        ))
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "INSERT INTO sms_parked_status (provider, provider_message_id, "
                "status, parked_at) VALUES (?, ?, ?, datetime('now', '-1 day'))",
                ("twilio", "stranded", "failed"),
            )
            conn.execute(
                "INSERT INTO sms_parked_status (provider, provider_message_id, "
                "status, error_code, parked_at) "
                "VALUES (?, ?, ?, ?, datetime('now'))",
                ("twilio", "opaque-1", "failed", "30006"),
            )

        asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:1", user_id="alice",
            text="done",
        ))

        # The fresh one was drained, the stale one pruned, and neither is left.
        assert _parked(config) == []
        assert _sent_rows(config)[0]["status"] == "failed"

    def test_an_unbounded_or_empty_provider_message_id_is_never_parked(
        self, tmp_path,
    ):
        # The inbound branch bounds this field at 255 characters; the delivery
        # branch validated nothing, and the park is what makes an unbounded
        # provider-chosen string a durable row on a uniquely-indexed column.
        config = _config(tmp_path)
        box: list = []
        dispositions = _callback_during_send(
            config, box,
            _delivery(status="failed", message_id="x" * 256),
            _delivery(status="failed", message_id=""),
        )

        asyncio.run(deliver_sms(
            config, box[0], logical_key="task-result:1", user_id="alice",
            text="done",
        ))

        assert dispositions == ["delivery_unknown", "delivery_unknown"]
        assert _parked(config) == []

    def test_a_status_racing_the_id_write_is_never_lost(self, tmp_path):
        """Two real threads at the window, rather than an argument about locks.

        The delivery branch takes `BEGIN IMMEDIATE` before its row lookup and
        holds it through the park; the outcome write needs that same lock. So
        there are only two orders — the callback commits its park first and the
        outcome drains it, or it looks up after the outcome committed and finds
        the row. Either way the `failed` lands.
        """
        for attempt in range(12):
            root = tmp_path / f"race{attempt}"
            root.mkdir()
            config = _config(root)
            barrier = threading.Barrier(2)
            event = _delivery(status="failed", error_code="30006")

            def send(_request):
                barrier.wait(5)
                return SmsSendResult("opaque-1", "accepted", 1)

            providers = _providers(_adapter(send))

            def outbound():
                return asyncio.run(deliver_sms(
                    config, providers, logical_key="task-result:1",
                    user_id="alice", text="done",
                ))

            def callback():
                barrier.wait(5)
                with db.get_db(config.db_path) as conn:
                    return handle_provider_event(
                        conn, config, event, active_provider_ready=True,
                    )

            with ThreadPoolExecutor(max_workers=2) as pool:
                sending = pool.submit(outbound)
                delivering = pool.submit(callback)
                sending.result(10)
                delivering.result(10)

            row = _sent_rows(config)[0]
            assert row["status"] == "failed", f"attempt {attempt}"
            assert row["error_code"] == "30006", f"attempt {attempt}"
            assert _parked(config) == [], f"attempt {attempt}"
