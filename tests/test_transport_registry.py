"""Tests for the transport registry and source_type → surface mapping."""

from unittest.mock import patch

from istota import db
from istota.config import Config
from istota.transport import (
    EmailTransport,
    TalkTransport,
    TransportRegistry,
    make_registry,
)
from istota.transport.registry import _surface_for_source_type


def _task(source_type: str) -> db.Task:
    return db.Task(
        id=1, status="completed", source_type=source_type,
        user_id="alice", prompt="hi",
    )


class TestSurfaceForSourceType:
    def test_email_maps_to_email(self):
        assert _surface_for_source_type("email") == "email"

    def test_talk_and_briefing_map_to_talk(self):
        assert _surface_for_source_type("talk") == "talk"
        assert _surface_for_source_type("briefing") == "talk"

    def test_repl_maps_to_repl(self):
        assert _surface_for_source_type("repl") == "repl"

    def test_unknown_defaults_to_talk(self):
        assert _surface_for_source_type("scheduled") == "talk"
        assert _surface_for_source_type("subtask") == "talk"
        assert _surface_for_source_type("totally-unknown") == "talk"


class TestMakeRegistry:
    def test_includes_only_enabled_surfaces(self):
        config = Config()
        config.talk.enabled = True
        config.email.enabled = True
        reg = make_registry(config)
        assert isinstance(reg.get("talk"), TalkTransport)
        assert isinstance(reg.get("email"), EmailTransport)

    def test_ntfy_and_istota_file_registered_unconditionally(self):
        from istota.transport.istota_file import IstotaFileTransport
        from istota.transport.ntfy import NtfyTransport
        config = Config()
        config.talk.enabled = False
        config.email.enabled = False
        reg = make_registry(config)
        assert isinstance(reg.get("ntfy"), NtfyTransport)
        assert isinstance(reg.get("istota_file"), IstotaFileTransport)

    def test_talk_disabled_excluded(self):
        config = Config()
        config.talk.enabled = False
        config.email.enabled = True
        reg = make_registry(config)
        assert reg.get("talk") is None
        assert isinstance(reg.get("email"), EmailTransport)

    def test_email_disabled_excluded(self):
        config = Config()
        config.talk.enabled = True
        config.email.enabled = False
        reg = make_registry(config)
        assert isinstance(reg.get("talk"), TalkTransport)
        assert reg.get("email") is None

    def test_construction_does_no_network(self):
        config = Config()
        config.talk.enabled = True
        config.email.enabled = True
        config.nextcloud.url = "https://nc.example.com"
        with patch("httpx.Client.request") as mock_req, \
                patch("httpx.AsyncClient.request") as mock_areq:
            make_registry(config)
            assert mock_req.call_count == 0
            assert mock_areq.call_count == 0


class TestForTask:
    def setup_method(self):
        config = Config()
        config.talk.enabled = True
        config.email.enabled = True
        self.reg = make_registry(config)

    def test_talk_task_resolves_talk(self):
        assert isinstance(self.reg.for_task(_task("talk")), TalkTransport)

    def test_briefing_task_resolves_talk(self):
        assert isinstance(self.reg.for_task(_task("briefing")), TalkTransport)

    def test_email_task_resolves_email(self):
        assert isinstance(self.reg.for_task(_task("email")), EmailTransport)

    def test_unknown_task_resolves_talk(self):
        assert isinstance(self.reg.for_task(_task("scheduled")), TalkTransport)

    def test_for_task_returns_none_when_surface_disabled(self):
        config = Config()
        config.talk.enabled = False
        config.email.enabled = True
        reg = make_registry(config)
        assert reg.for_task(_task("talk")) is None

    def test_pollers_lists_all_registered(self):
        names = {t.name for t in self.reg.pollers()}
        # ntfy + istota_file + repl are registered unconditionally (per-user /
        # stream gating happens in their resolve_target/deliver).
        assert names == {"talk", "email", "ntfy", "istota_file", "repl"}


class TestEmptyRegistry:
    def test_get_missing_returns_none(self):
        reg = TransportRegistry({})
        assert reg.get("talk") is None
        assert reg.pollers() == []
