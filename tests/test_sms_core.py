"""Provider-neutral SMS rendering, state, ingest, and transport contracts."""

from __future__ import annotations

import asyncio
import threading
from dataclasses import replace

import pytest

from istota import db, notifications, surfaces
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
            "istota.transport.sms.make_provider_registry", lambda _config: _providers(adapter)
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

    def test_inactive_provider_and_unknown_number_are_acknowledged_without_rows(self, tmp_path):
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

    def test_failed_callback_enqueues_alert_without_network_delivery(
        self, tmp_path, monkeypatch
    ):
        config = _config(tmp_path)
        providers = _providers(_adapter(lambda _req: SmsSendResult("opaque-1", "sent", 1)))
        asyncio.run(deliver_sms(
            config, providers, logical_key="task-result:1", user_id="alice", text="done"
        ))
        monkeypatch.setattr(
            notifications, "send_notification",
            lambda *_args, **_kwargs: pytest.fail("callback must not deliver notifications"),
        )

        result = asyncio.run(_handle(
            config, providers,
            SmsDeliveryEvent("twilio", "event-failed", "opaque-1", "failed", "30007", None),
        ))

        assert result.disposition == "delivery_updated"
        with db.get_db(config.db_path) as conn:
            assert conn.execute(
                "SELECT count(*) FROM notifications WHERE source = 'task_alert'"
            ).fetchone()[0] == 1


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
            "istota.transport.sms.make_provider_registry", lambda _config: providers
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
            "istota.transport.sms.make_provider_registry", lambda _config: providers
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
