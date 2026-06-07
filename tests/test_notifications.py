"""Configuration loading for istota.notifications module."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota.config import (
    BriefingConfig,
    Config,
    EmailConfig,
    NextcloudConfig,
    UserConfig,
)
from istota.notifications import (
    _send_email,
    _send_ntfy,
    _send_talk,
    effective_log_destinations,
    is_channel_configured,
    resolve_conversation_token,
    resolve_destination,
    send_notification,
)
from istota.transport import Destination


def _ntfy_secrets(**values: str):
    """Build a fake ``secrets_store.get_service_secrets`` returning the given values.

    Patched onto ``istota.secrets_store.get_service_secrets`` (the bulk
    helper) — a single SELECT is the production path, so tests mirror it.
    Empty values are dropped to mirror the real "row missing" behaviour.
    """
    table = {k: v for k, v in values.items() if v}

    def fake_get_service_secrets(db_path, user_id, service):
        return dict(table) if service == "ntfy" else {}

    return fake_get_service_secrets


class TestResolveConversationToken:
    def test_returns_briefing_token(self):
        config = Config(users={
            "alice": UserConfig(
                briefings=[BriefingConfig(name="morning", cron="0 6 * * *", conversation_token="room2")],
            ),
        })
        assert resolve_conversation_token(config, "alice") == "room2"

    def test_returns_none_for_unknown_user(self):
        config = Config()
        assert resolve_conversation_token(config, "unknown") is None

    def test_returns_none_when_no_tokens(self):
        config = Config(users={"alice": UserConfig()})
        assert resolve_conversation_token(config, "alice") is None


    def test_falls_back_to_dm_token(self):
        config = Config(users={"alice": UserConfig()})
        with patch("istota.transport.talk.get_dm_token", return_value="dm_room_abc"):
            assert resolve_conversation_token(config, "alice") == "dm_room_abc"

    def test_dm_token_not_used_when_alerts_channel_set(self):
        config = Config(users={
            "alice": UserConfig(alerts_channel="alerts_room"),
        })
        with patch("istota.transport.talk.get_dm_token", return_value="dm_room_abc"):
            assert resolve_conversation_token(config, "alice") == "alerts_room"

    def test_prefers_alerts_channel(self):
        config = Config(users={
            "alice": UserConfig(
                alerts_channel="alerts_room",
                briefings=[BriefingConfig(name="morning", cron="0 6 * * *", conversation_token="briefing_room")],
            ),
        })
        assert resolve_conversation_token(config, "alice") == "alerts_room"

    def test_falls_back_to_briefing_when_no_alerts_channel(self):
        config = Config(users={
            "alice": UserConfig(
                alerts_channel="",
                briefings=[BriefingConfig(name="morning", cron="0 6 * * *", conversation_token="briefing_room")],
            ),
        })
        assert resolve_conversation_token(config, "alice") == "briefing_room"


class TestSendTalk:
    @pytest.mark.asyncio
    async def test_sends_with_explicit_token(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig()},
        )
        with patch("istota.transport.talk.get_talk_client") as MockClient:
            mock_client = AsyncMock()
            mock_client.send_message.return_value = {"ocs": {"data": {"id": 10}}}
            MockClient.return_value = mock_client
            result = await _send_talk(config, "alice", "hello", conversation_token="room1")
        assert result == 10
        # _send_talk now delegates to TalkTransport.deliver, which always passes
        # reply_to / reference_id (both None here). Behaviour at the wire is
        # identical — TalkClient.send_message no-ops on falsy values.
        mock_client.send_message.assert_called_once_with(
            "room1", "hello", reply_to=None, reference_id=None,
        )

    @pytest.mark.asyncio
    async def test_resolves_token_from_user(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig(
                briefings=[BriefingConfig(name="morning", cron="0 6 * * *", conversation_token="room2")],
            )},
        )
        with patch("istota.transport.talk.get_talk_client") as MockClient:
            mock_client = AsyncMock()
            mock_client.send_message.return_value = {"ocs": {"data": {"id": 11}}}
            MockClient.return_value = mock_client
            result = await _send_talk(config, "alice", "hello")
        assert result == 11
        mock_client.send_message.assert_called_once_with(
            "room2", "hello", reply_to=None, reference_id=None,
        )

    @pytest.mark.asyncio
    async def test_returns_none_without_token(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig()},
        )
        result = await _send_talk(config, "alice", "hello")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_none_without_nextcloud(self):
        config = Config(users={"alice": UserConfig(
            briefings=[BriefingConfig(name="morning", cron="0 6 * * *", conversation_token="room1")],
        )})
        result = await _send_talk(config, "alice", "hello")
        assert result is None


    @pytest.mark.asyncio
    async def test_returns_message_id_on_success(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig()},
        )
        with patch("istota.transport.talk.get_talk_client") as MockClient:
            mock_client = AsyncMock()
            mock_client.send_message.return_value = {"ocs": {"data": {"id": 42}}}
            MockClient.return_value = mock_client
            result = await _send_talk(config, "alice", "hello", conversation_token="room1")
        assert result == 42

    @pytest.mark.asyncio
    async def test_returns_none_on_failure(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig()},
        )
        with patch("istota.transport.talk.get_talk_client") as MockClient:
            mock_client = AsyncMock()
            mock_client.send_message.side_effect = Exception("fail")
            MockClient.return_value = mock_client
            result = await _send_talk(config, "alice", "hello", conversation_token="room1")
        assert result is None


class TestSendTalkConfirmation:
    def test_returns_message_id(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc.example.com"),
            users={"alice": UserConfig()},
        )
        with patch("istota.transport.talk.get_talk_client") as MockClient:
            mock_client = AsyncMock()
            mock_client.send_message.return_value = {"ocs": {"data": {"id": 99}}}
            MockClient.return_value = mock_client
            from istota.notifications import send_talk_confirmation
            result = send_talk_confirmation(config, "alice", "Confirm?", conversation_token="room1")
        assert result == 99

    def test_returns_none_without_token(self):
        config = Config(users={"alice": UserConfig()})
        from istota.notifications import send_talk_confirmation
        result = send_talk_confirmation(config, "alice", "Confirm?")
        assert result is None


class TestSendEmail:
    @patch("istota.skills.email.send_email")
    @patch("istota.email_support.get_email_config")
    def test_sends_email(self, mock_get_config, mock_send):
        config = Config(
            email=EmailConfig(enabled=True, bot_email="bot@test.com"),
            users={"alice": UserConfig(email_addresses=["alice@test.com"])},
        )
        result = _send_email(config, "alice", "Subject", "Body")
        assert result is True
        mock_send.assert_called_once()

    def test_returns_false_without_email_addresses(self):
        config = Config(
            email=EmailConfig(enabled=True),
            users={"alice": UserConfig()},
        )
        result = _send_email(config, "alice", "Subject", "Body")
        assert result is False

    def test_returns_false_when_email_disabled(self):
        config = Config(
            email=EmailConfig(enabled=False),
            users={"alice": UserConfig(email_addresses=["alice@test.com"])},
        )
        result = _send_email(config, "alice", "Subject", "Body")
        assert result is False


class TestSendNtfyShim:
    """_send_ntfy is now a thin sync shim over the ntfy transport (the POST
    detail tests live in test_ntfy_transport.py). These verify the shim
    delegates and adapts the sync signature correctly."""

    def test_delegates_to_transport_with_options(self):
        config = Config(users={"alice": UserConfig()})
        with patch("istota.transport.ntfy.send_ntfy_async") as mock_send:
            async def _ok(*a, **k):
                return True
            mock_send.side_effect = _ok
            result = _send_ntfy(config, "alice", "msg", title="T", priority=5, tags="x")
        assert result is True
        call = mock_send.call_args
        assert call[0][0] is config
        assert call[0][1] == "alice"
        assert call[0][2] == "msg"
        opts = call[0][3]
        assert opts.title == "T" and opts.priority == 5 and opts.tags == "x"

    @patch("istota.secrets_store.get_service_secrets")
    def test_returns_false_when_topic_unset(self, mock_get_secrets):
        mock_get_secrets.return_value = {}
        config = Config(users={"alice": UserConfig()})
        assert _send_ntfy(config, "alice", "msg") is False


class TestSendNotification:
    @patch("istota.notifications._send_talk")
    def test_talk_surface(self, mock_talk):
        mock_talk.return_value = True
        config = Config(users={"alice": UserConfig()})
        result = send_notification(config, "alice", "msg", surface="talk")
        assert result is True
        mock_talk.assert_called_once()

    @patch("istota.notifications._send_email")
    def test_email_surface(self, mock_email):
        mock_email.return_value = True
        config = Config(users={"alice": UserConfig()})
        result = send_notification(config, "alice", "msg", surface="email", title="Sub")
        assert result is True
        mock_email.assert_called_once()

    @patch("istota.notifications._send_email")
    @patch("istota.notifications._send_talk")
    def test_both_surface(self, mock_talk, mock_email):
        mock_talk.return_value = True
        mock_email.return_value = True
        config = Config(users={"alice": UserConfig()})
        result = send_notification(config, "alice", "msg", surface="both", title="Sub")
        assert result is True
        mock_talk.assert_called_once()
        mock_email.assert_called_once()

    @patch("istota.notifications._send_ntfy")
    def test_ntfy_surface(self, mock_ntfy):
        mock_ntfy.return_value = True
        config = Config(users={"alice": UserConfig()})
        result = send_notification(config, "alice", "msg", surface="ntfy", title="T")
        assert result is True
        mock_ntfy.assert_called_once()

    @patch("istota.notifications._send_ntfy")
    @patch("istota.notifications._send_email")
    @patch("istota.notifications._send_talk")
    def test_all_surface(self, mock_talk, mock_email, mock_ntfy):
        mock_talk.return_value = True
        mock_email.return_value = True
        mock_ntfy.return_value = True
        config = Config(users={"alice": UserConfig()})
        result = send_notification(config, "alice", "msg", surface="all", title="T")
        assert result is True
        mock_talk.assert_called_once()
        mock_email.assert_called_once()
        mock_ntfy.assert_called_once()

    @patch("istota.notifications._send_talk")
    def test_returns_false_when_delivery_fails(self, mock_talk):
        mock_talk.return_value = False
        config = Config(users={"alice": UserConfig()})
        result = send_notification(config, "alice", "msg", surface="talk")
        assert result is False

    @patch("istota.notifications._send_talk")
    def test_passes_conversation_token(self, mock_talk):
        mock_talk.return_value = True
        config = Config(users={"alice": UserConfig()})
        send_notification(config, "alice", "msg", surface="talk", conversation_token="room1")
        _, kwargs = mock_talk.call_args
        # conversation_token is passed as positional arg to _send_talk
        assert mock_talk.call_args[0][3] == "room1" or "room1" in str(mock_talk.call_args)

    @patch("istota.notifications._send_ntfy")
    def test_passes_priority_and_tags(self, mock_ntfy):
        mock_ntfy.return_value = True
        config = Config(users={"alice": UserConfig()})
        send_notification(
            config, "alice", "msg", surface="ntfy",
            title="T", priority=5, tags="urgent",
        )
        mock_ntfy.assert_called_once_with(
            config, "alice", "msg", title="T", priority=5, tags="urgent",
        )


class TestSendNotificationPurposeRouting:
    """The per-user routing table is consulted when a purpose (not an explicit
    surface) is given — this is what makes routing={'alert': 'ntfy'} live."""

    @patch("istota.notifications._send_ntfy")
    @patch("istota.notifications._send_talk")
    def test_purpose_routes_alert_to_ntfy(self, mock_talk, mock_ntfy):
        mock_ntfy.return_value = True
        config = Config(users={"alice": UserConfig(routing={"alert": "ntfy"})})
        result = send_notification(config, "alice", "msg", purpose="alert", title="T")
        assert result is True
        mock_ntfy.assert_called_once()
        mock_talk.assert_not_called()

    @patch("istota.notifications._send_ntfy")
    @patch("istota.notifications._send_talk")
    def test_purpose_routes_to_multiple_surfaces(self, mock_talk, mock_ntfy):
        mock_talk.return_value = True
        mock_ntfy.return_value = True
        config = Config(users={"alice": UserConfig(routing={"alert": "talk,ntfy"})})
        send_notification(config, "alice", "msg", purpose="alert")
        mock_talk.assert_called_once()
        mock_ntfy.assert_called_once()

    @patch("istota.notifications._send_talk")
    def test_explicit_surface_overrides_purpose(self, mock_talk):
        mock_talk.return_value = True
        # routing says ntfy, but an explicit surface= wins.
        config = Config(users={"alice": UserConfig(routing={"alert": "ntfy"})})
        send_notification(config, "alice", "msg", surface="talk", purpose="alert")
        mock_talk.assert_called_once()

    @patch("istota.notifications._send_talk")
    def test_purpose_falls_back_to_legacy_alerts_channel(self, mock_talk):
        mock_talk.return_value = True
        config = Config(users={"alice": UserConfig(alerts_channel="achan")})
        send_notification(config, "alice", "msg", purpose="alert")
        # talk delivery resolves to the legacy alerts channel.
        assert "achan" in str(mock_talk.call_args)


class TestResolveDestination:
    """Purpose-keyed routing table precedence."""

    def test_routing_entry_wins(self):
        config = Config(users={"alice": UserConfig(
            routing={"alert": "email"}, alerts_channel="achan",
        )})
        assert resolve_destination(config, "alice", "alert") == Destination("email")

    def test_legacy_alerts_channel_for_alert(self):
        config = Config(users={"alice": UserConfig(alerts_channel="achan")})
        assert resolve_destination(config, "alice", "alert") == Destination("talk", "achan")

    def test_legacy_log_channel_for_log(self):
        config = Config(users={"alice": UserConfig(log_channel="lchan")})
        assert resolve_destination(config, "alice", "log") == Destination("talk", "lchan")

    def test_briefing_token_for_briefing_purpose(self):
        config = Config(users={"alice": UserConfig(
            briefings=[BriefingConfig(name="m", cron="0 8 * * *", conversation_token="brt")],
        )})
        assert resolve_destination(config, "alice", "briefing") == Destination("talk", "brt")

    def test_default_destination_fallback(self):
        config = Config(users={"alice": UserConfig(default_destination="email")})
        assert resolve_destination(config, "alice", "notification") == Destination("email")

    def test_instance_fallback_bare_talk(self):
        config = Config(users={"alice": UserConfig()})
        assert resolve_destination(config, "alice", "notification") == Destination("talk", None)


class TestEffectiveLogDestinations:
    """Opt-in log-channel resolution: routing['log'] > legacy log_channel > [].

    Unlike resolve_destinations, this NEVER falls through to default_destination
    or a bare-talk instance default — an unconfigured user gets no log output.
    """

    def test_route_set_wins(self):
        config = Config(users={"alice": UserConfig(routing={"log": "ntfy"})})
        assert effective_log_destinations(config, "alice") == [Destination("ntfy", None)]

    def test_route_wins_over_legacy_log_channel(self):
        config = Config(users={"alice": UserConfig(
            routing={"log": "ntfy"}, log_channel="lchan",
        )})
        assert effective_log_destinations(config, "alice") == [Destination("ntfy", None)]

    def test_legacy_log_channel_only(self):
        config = Config(users={"alice": UserConfig(log_channel="lchan")})
        assert effective_log_destinations(config, "alice") == [Destination("talk", "lchan")]

    def test_neither_configured_is_empty(self):
        # The opt-in regression guard: no route, no log_channel -> disabled.
        config = Config(users={"alice": UserConfig()})
        assert effective_log_destinations(config, "alice") == []

    def test_default_destination_not_used(self):
        # default_destination must NOT silently enable the verbose log.
        config = Config(users={"alice": UserConfig(default_destination="email")})
        assert effective_log_destinations(config, "alice") == []

    def test_unknown_user_is_empty(self):
        assert effective_log_destinations(Config(), "nobody") == []

    def test_drops_unregistered_surface(self):
        # email disabled -> not registered -> the email log route is dropped.
        config = Config(
            email=EmailConfig(enabled=False),
            users={"alice": UserConfig(routing={"log": "email"})},
        )
        assert effective_log_destinations(config, "alice") == []

    def test_email_route_when_enabled(self):
        config = Config(
            email=EmailConfig(enabled=True),
            users={"alice": UserConfig(routing={"log": "email"})},
        )
        assert effective_log_destinations(config, "alice") == [Destination("email", None)]

    def test_drops_non_user_routable_surface(self):
        # repl is registered but user_routable=False -> never a log destination.
        config = Config(users={"alice": UserConfig(routing={"log": "repl"})})
        assert effective_log_destinations(config, "alice") == []

    def test_bare_talk_prefers_log_channel(self):
        # Bare `talk` for the log purpose means "the logs room" — prefer
        # log_channel over the default Talk channel / DM.
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc"),
            users={"alice": UserConfig(
                routing={"log": "talk"}, log_channel="logs", alerts_channel="achan",
            )},
        )
        assert effective_log_destinations(config, "alice") == [Destination("talk", "logs")]

    def test_bare_talk_falls_back_to_default_channel_without_log_channel(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc"),
            users={"alice": UserConfig(routing={"log": "talk"}, alerts_channel="achan")},
        )
        assert effective_log_destinations(config, "alice") == [Destination("talk", "achan")]

    def test_none_disables_even_with_log_channel(self):
        # Explicit "none" overrides a provisioned log_channel (the only way to
        # turn the log fully off from the web UI).
        config = Config(users={"alice": UserConfig(
            routing={"log": "none"}, log_channel="logs",
        )})
        assert effective_log_destinations(config, "alice") == []

    def test_bare_talk_unresolvable_is_dropped(self):
        config = Config(users={"alice": UserConfig(routing={"log": "talk"})})
        assert effective_log_destinations(config, "alice") == []

    def test_explicit_talk_token_kept(self):
        config = Config(users={"alice": UserConfig(routing={"log": "talk:room5"})})
        assert effective_log_destinations(config, "alice") == [Destination("talk", "room5")]

    def test_comma_list_mixed_capability(self):
        config = Config(users={"alice": UserConfig(routing={"log": "talk:room,ntfy"})})
        assert effective_log_destinations(config, "alice") == [
            Destination("talk", "room"),
            Destination("ntfy", None),
        ]

    def test_never_raises_on_registry_failure(self):
        config = Config(users={"alice": UserConfig(log_channel="lchan")})
        with patch("istota.notifications.make_registry", side_effect=RuntimeError("boom")):
            assert effective_log_destinations(config, "alice") == []


class TestResolveConversationTokenRouting:
    def test_routing_alert_talk_route_wins(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc"),
            users={"alice": UserConfig(
                routing={"alert": "talk:routed"}, alerts_channel="achan",
            )},
        )
        assert resolve_conversation_token(config, "alice") == "routed"

    def test_off_talk_routing_does_not_break_talk_fallback(self):
        # alert routed to email; resolve_conversation_token must still fall back
        # to alerts_channel (so is_channel_configured._talk_ok stays True).
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc"),
            users={"alice": UserConfig(
                routing={"alert": "email"}, alerts_channel="achan",
            )},
        )
        assert resolve_conversation_token(config, "alice") == "achan"
        assert is_channel_configured(config, "alice", "talk") is True

    def test_legacy_order_preserved_without_routing(self):
        config = Config(
            nextcloud=NextcloudConfig(url="https://nc"),
            users={"alice": UserConfig(
                alerts_channel="achan",
                briefings=[BriefingConfig(name="m", cron="0 8 * * *", conversation_token="brt")],
            )},
        )
        assert resolve_conversation_token(config, "alice") == "achan"


class TestSendNotificationConversationTokenOverride:
    @patch("istota.notifications._send_email")
    @patch("istota.notifications._send_talk")
    def test_conversation_token_overrides_bare_talk(self, mock_talk, mock_email):
        mock_talk.return_value = True
        mock_email.return_value = True
        config = Config(users={"alice": UserConfig()})
        # heartbeat pattern: surface="both", conversation_token=X → Talk leg X.
        send_notification(config, "alice", "msg", surface="both", conversation_token="roomX")
        assert mock_talk.call_args[0][3] == "roomX"
