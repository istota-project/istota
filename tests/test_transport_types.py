"""Tests for transport core types and protocol conformance."""

from istota.config import Config
from istota.transport import (
    EmailTransport,
    IncomingMessage,
    Transport,
    TransportCapabilities,
)
from istota.transport.talk import TalkTransport


class TestIncomingMessage:
    def test_required_fields_and_defaults(self):
        msg = IncomingMessage(
            user_id="alice",
            text="hello",
            source_type="talk",
            surface="talk",
            channel_token="room1",
        )
        assert msg.user_id == "alice"
        assert msg.text == "hello"
        assert msg.source_type == "talk"
        assert msg.surface == "talk"
        assert msg.channel_token == "room1"
        # Defaults
        assert msg.delivery_token is None
        assert msg.platform_message_id is None
        assert msg.reply_to_message_id is None
        assert msg.reply_to_content is None
        assert msg.attachments == []
        assert msg.is_group_chat is False
        assert msg.output_target is None
        assert msg.model is None
        assert msg.effort is None
        assert msg.raw == {}

    def test_mutable_defaults_are_independent(self):
        a = IncomingMessage("u", "t", "talk", "talk", "c")
        b = IncomingMessage("u", "t", "talk", "talk", "c")
        a.attachments.append("x")
        a.raw["k"] = "v"
        assert b.attachments == []
        assert b.raw == {}


class TestTransportCapabilities:
    def test_defaults(self):
        cap = TransportCapabilities()
        assert cap.supports_edit is False
        assert cap.supports_threading is False
        assert cap.supports_progress_ack is False
        assert cap.supports_typing is False
        assert cap.max_message_length is None

    def test_frozen(self):
        cap = TransportCapabilities()
        try:
            cap.supports_edit = True  # type: ignore[misc]
        except Exception as e:
            assert "frozen" in type(e).__name__.lower() or "cannot assign" in str(e).lower()
        else:
            raise AssertionError("TransportCapabilities should be frozen")


class TestProtocolConformance:
    def test_talk_transport_is_a_transport(self):
        config = Config()
        t = TalkTransport(config)
        assert isinstance(t, Transport)
        assert t.name == "talk"
        assert t.capabilities.supports_edit is True
        assert t.capabilities.supports_progress_ack is True
        assert t.capabilities.max_message_length == 30000

    def test_email_transport_is_a_transport(self):
        config = Config()
        t = EmailTransport(config)
        assert isinstance(t, Transport)
        assert t.name == "email"
        assert t.capabilities.supports_edit is False
        assert t.capabilities.supports_progress_ack is False
        assert t.capabilities.max_message_length is None
