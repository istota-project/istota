"""Tests for progress config fields."""

from pathlib import Path

from istota.config import Config, SchedulerConfig, load_config


class TestProgressConfigDefaults:
    def test_default_values(self):
        config = Config()
        assert config.scheduler.progress_updates is True
        assert config.scheduler.progress_min_interval == 8
        assert config.scheduler.progress_max_messages == 5
        assert config.scheduler.progress_show_tool_use is True
        assert config.scheduler.progress_show_text is False


class TestProgressConfigParsing:
    def test_parse_progress_settings(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[scheduler]
progress_updates = false
progress_min_interval = 15
progress_max_messages = 3
progress_show_tool_use = false
progress_show_text = true
""")
        config = load_config(config_file)
        assert config.scheduler.progress_updates is False
        assert config.scheduler.progress_min_interval == 15
        assert config.scheduler.progress_max_messages == 3
        assert config.scheduler.progress_show_tool_use is False
        assert config.scheduler.progress_show_text is True

    def test_missing_progress_settings_use_defaults(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[scheduler]
poll_interval = 10
""")
        config = load_config(config_file)
        assert config.scheduler.progress_updates is True
        assert config.scheduler.progress_min_interval == 8
        assert config.scheduler.progress_max_messages == 5

    def test_partial_progress_settings(self, tmp_path):
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[scheduler]
progress_max_messages = 10
""")
        config = load_config(config_file)
        assert config.scheduler.progress_updates is True  # default
        assert config.scheduler.progress_max_messages == 10  # overridden
        assert config.scheduler.progress_min_interval == 8  # default
