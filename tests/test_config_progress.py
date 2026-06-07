"""Tests for progress / event-streaming config fields."""

from istota.config import Config, load_config


class TestProgressConfigDefaults:
    def test_default_values(self):
        config = Config()
        assert config.scheduler.progress_updates is True
        assert config.scheduler.progress_show_tool_use is True
        assert config.scheduler.progress_show_text is False
        assert config.scheduler.event_log_enabled is True
        assert config.scheduler.push_notification_threshold_seconds == 30
        # ntfy is opt-in: no source type triggers a push by default.
        assert config.scheduler.push_notification_sources == []


class TestProgressConfigParsing:
    def test_parse_progress_settings(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[scheduler]
progress_updates = false
progress_show_tool_use = false
progress_show_text = true
event_log_enabled = false
push_notification_threshold_seconds = 60
push_notification_sources = ["talk", "email"]
""")
        config = load_config(config_file)
        assert config.scheduler.progress_updates is False
        assert config.scheduler.progress_show_tool_use is False
        assert config.scheduler.progress_show_text is True
        assert config.scheduler.event_log_enabled is False
        assert config.scheduler.push_notification_threshold_seconds == 60
        assert config.scheduler.push_notification_sources == ["talk", "email"]

    def test_missing_progress_settings_use_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[scheduler]
poll_interval = 10
""")
        config = load_config(config_file)
        assert config.scheduler.progress_updates is True
        assert config.scheduler.progress_show_tool_use is True
        assert config.scheduler.event_log_enabled is True

    def test_partial_progress_settings(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[scheduler]
progress_show_text = true
""")
        config = load_config(config_file)
        assert config.scheduler.progress_updates is True  # default
        assert config.scheduler.progress_show_text is True  # overridden
        assert config.scheduler.event_log_enabled is True  # default
