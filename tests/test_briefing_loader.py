"""Configuration loading for istota.briefing_loader module."""

from pathlib import Path

import pytest

from istota.briefing_loader import (
    _expand_boolean_components,
    _load_workspace_briefings,
    get_briefings_for_user,
)
from istota.config import (
    BriefingConfig,
    BriefingDefaultsConfig,
    Config,
    UserConfig,
)


class TestExpandBooleanComponents:
    def test_simple_booleans_unchanged(self):
        defaults = BriefingDefaultsConfig()
        result = _expand_boolean_components(
            {"calendar": True, "todos": True}, defaults,
        )
        assert result == {"calendar": True, "todos": True}

    def test_markets_true_expands(self):
        defaults = BriefingDefaultsConfig(
            markets={"futures": ["ES=F"], "indices": ["^GSPC"]},
        )
        result = _expand_boolean_components({"markets": True}, defaults)
        assert result == {
            "markets": {"enabled": True, "futures": ["ES=F"], "indices": ["^GSPC"]},
        }

    def test_news_true_expands(self):
        defaults = BriefingDefaultsConfig(
            news={"lookback_hours": 12, "sources": [{"type": "domain", "value": "x.com"}]},
        )
        result = _expand_boolean_components({"news": True}, defaults)
        assert result == {
            "news": {
                "enabled": True,
                "lookback_hours": 12,
                "sources": [{"type": "domain", "value": "x.com"}],
            },
        }

    def test_dict_passthrough(self):
        defaults = BriefingDefaultsConfig(
            markets={"futures": ["ES=F"]},
        )
        custom = {"enabled": True, "futures": ["NQ=F"]}
        result = _expand_boolean_components({"markets": custom}, defaults)
        assert result == {"markets": custom}

    def test_false_unchanged(self):
        defaults = BriefingDefaultsConfig(markets={"futures": ["ES=F"]})
        result = _expand_boolean_components({"markets": False}, defaults)
        assert result == {"markets": False}

    def test_empty_defaults_bool_stays(self):
        defaults = BriefingDefaultsConfig()
        result = _expand_boolean_components({"markets": True}, defaults)
        assert result == {"markets": True}

    def test_mixed_components(self):
        defaults = BriefingDefaultsConfig(
            markets={"futures": ["ES=F"]},
            news={"lookback_hours": 6},
        )
        result = _expand_boolean_components(
            {"calendar": True, "markets": True, "news": {"enabled": True, "lookback_hours": 3}},
            defaults,
        )
        assert result["calendar"] is True
        assert result["markets"] == {"enabled": True, "futures": ["ES=F"]}
        assert result["news"] == {"enabled": True, "lookback_hours": 3}


def _wrap_toml(toml_content: str) -> str:
    """Wrap TOML content in a Markdown file with code block."""
    return f"""# Briefing Schedule

Some description here.

## Settings

```toml
{toml_content}
```

## Notes

Additional notes.
"""


class TestLoadWorkspaceBriefings:
    def test_no_mount_returns_none(self):
        config = Config(nextcloud_mount_path=None)
        assert _load_workspace_briefings(config, "alice") is None

    def test_file_not_found(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(nextcloud_mount_path=mount)
        assert _load_workspace_briefings(config, "alice") is None

    def test_valid_toml(self, tmp_path):
        mount = tmp_path / "mount"
        workspace = mount / "Users" / "alice" / "istota" / "config"
        workspace.mkdir(parents=True)
        (workspace / "BRIEFINGS.md").write_text(_wrap_toml(
            '[[briefings]]\n'
            'name = "morning"\n'
            'cron = "0 7 * * *"\n'
            'conversation_token = "room1"\n'
            'output = "talk"\n'
            '\n'
            '[briefings.components]\n'
            'calendar = true\n'
        ))
        config = Config(nextcloud_mount_path=mount)
        result = _load_workspace_briefings(config, "alice")
        assert result is not None
        assert len(result) == 1
        assert result[0].name == "morning"
        assert result[0].cron == "0 7 * * *"
        assert result[0].conversation_token == "room1"
        assert result[0].components == {"calendar": True}

    def test_malformed_toml_returns_none(self, tmp_path):
        mount = tmp_path / "mount"
        workspace = mount / "Users" / "bob" / "istota" / "config"
        workspace.mkdir(parents=True)
        (workspace / "BRIEFINGS.md").write_text(_wrap_toml("this is not valid [[[ toml"))
        config = Config(nextcloud_mount_path=mount)
        result = _load_workspace_briefings(config, "bob")
        assert result is None

    def test_empty_file(self, tmp_path):
        mount = tmp_path / "mount"
        workspace = mount / "Users" / "alice" / "istota" / "config"
        workspace.mkdir(parents=True)
        (workspace / "BRIEFINGS.md").write_text("")
        config = Config(nextcloud_mount_path=mount)
        result = _load_workspace_briefings(config, "alice")
        # No TOML block found → empty list
        assert result == []

    def test_no_toml_block(self, tmp_path):
        """File with markdown but no toml code block returns empty list."""
        mount = tmp_path / "mount"
        workspace = mount / "Users" / "alice" / "istota" / "config"
        workspace.mkdir(parents=True)
        (workspace / "BRIEFINGS.md").write_text(
            "# Briefings\n\nJust some notes, no TOML block.\n"
        )
        config = Config(nextcloud_mount_path=mount)
        result = _load_workspace_briefings(config, "alice")
        assert result == []

    def test_markdown_with_valid_toml_block(self, tmp_path):
        """File with markdown content and valid TOML block works."""
        mount = tmp_path / "mount"
        workspace = mount / "Users" / "alice" / "istota" / "config"
        workspace.mkdir(parents=True)
        content = """# My Briefing Config

Here's my custom schedule configuration.

## Current Settings

```toml
[[briefings]]
name = "custom"
cron = "0 9 * * *"
conversation_token = "custom_room"
```

## Notes

This is just for testing.
"""
        (workspace / "BRIEFINGS.md").write_text(content)
        config = Config(nextcloud_mount_path=mount)
        result = _load_workspace_briefings(config, "alice")
        assert result is not None
        assert len(result) == 1
        assert result[0].name == "custom"
        assert result[0].cron == "0 9 * * *"

    def test_defaults_for_missing_fields(self, tmp_path):
        mount = tmp_path / "mount"
        workspace = mount / "Users" / "alice" / "istota" / "config"
        workspace.mkdir(parents=True)
        (workspace / "BRIEFINGS.md").write_text(_wrap_toml(
            '[[briefings]]\n'
            'name = "quick"\n'
            'cron = "0 8 * * *"\n'
        ))
        config = Config(nextcloud_mount_path=mount)
        result = _load_workspace_briefings(config, "alice")
        assert len(result) == 1
        assert result[0].conversation_token == ""
        assert result[0].output == "talk"
        assert result[0].components == {}


class TestGetBriefingsForUser:
    def test_no_user_config(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path, users={})
        result = get_briefings_for_user(config, "nobody")
        assert result == []

    def test_admin_only(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        briefing = BriefingConfig(
            name="morning", cron="0 6 * * *",
            conversation_token="room1", components={"calendar": True},
        )
        user = UserConfig(briefings=[briefing])
        config = Config(nextcloud_mount_path=mount, users={"alice": user})
        result = get_briefings_for_user(config, "alice")
        assert len(result) == 1
        assert result[0].name == "morning"

    def test_workspace_overrides_admin(self, tmp_path):
        mount = tmp_path / "mount"
        workspace = mount / "Users" / "alice" / "istota" / "config"
        workspace.mkdir(parents=True)
        (workspace / "BRIEFINGS.md").write_text(_wrap_toml(
            '[[briefings]]\n'
            'name = "morning"\n'
            'cron = "0 7 * * *"\n'
            'conversation_token = "room2"\n'
        ))
        admin_briefing = BriefingConfig(
            name="morning", cron="0 6 * * *",
            conversation_token="room1", components={"calendar": True},
        )
        user = UserConfig(briefings=[admin_briefing])
        config = Config(nextcloud_mount_path=mount, users={"alice": user})

        result = get_briefings_for_user(config, "alice")
        assert len(result) == 1
        assert result[0].cron == "0 7 * * *"
        assert result[0].conversation_token == "room2"

    def test_workspace_adds_new_briefing(self, tmp_path):
        mount = tmp_path / "mount"
        workspace = mount / "Users" / "alice" / "istota" / "config"
        workspace.mkdir(parents=True)
        (workspace / "BRIEFINGS.md").write_text(_wrap_toml(
            '[[briefings]]\n'
            'name = "afternoon"\n'
            'cron = "0 14 * * *"\n'
            'conversation_token = "room1"\n'
        ))
        admin_briefing = BriefingConfig(
            name="morning", cron="0 6 * * *",
            conversation_token="room1",
        )
        user = UserConfig(briefings=[admin_briefing])
        config = Config(nextcloud_mount_path=mount, users={"alice": user})

        result = get_briefings_for_user(config, "alice")
        names = [b.name for b in result]
        assert "morning" in names
        assert "afternoon" in names

    def test_boolean_expansion_with_defaults(self, tmp_path):
        mount = tmp_path / "mount"
        workspace = mount / "Users" / "alice" / "istota" / "config"
        workspace.mkdir(parents=True)
        (workspace / "BRIEFINGS.md").write_text(_wrap_toml(
            '[[briefings]]\n'
            'name = "morning"\n'
            'cron = "0 6 * * *"\n'
            'conversation_token = "room1"\n'
            '\n'
            '[briefings.components]\n'
            'calendar = true\n'
            'markets = true\n'
        ))
        user = UserConfig()
        defaults = BriefingDefaultsConfig(
            markets={"futures": ["ES=F", "NQ=F"]},
        )
        config = Config(
            nextcloud_mount_path=mount,
            users={"alice": user},
            briefing_defaults=defaults,
        )

        result = get_briefings_for_user(config, "alice")
        assert len(result) == 1
        comps = result[0].components
        assert comps["calendar"] is True
        assert comps["markets"] == {"enabled": True, "futures": ["ES=F", "NQ=F"]}

    def test_no_workspace_file_uses_admin(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        # No BRIEFINGS.md on disk
        admin_briefing = BriefingConfig(
            name="morning", cron="0 6 * * *",
            conversation_token="room1", components={"calendar": True},
        )
        user = UserConfig(briefings=[admin_briefing])
        config = Config(nextcloud_mount_path=mount, users={"alice": user})

        result = get_briefings_for_user(config, "alice")
        assert len(result) == 1
        assert result[0].name == "morning"

    def test_empty_workspace_falls_back_to_admin(self, tmp_path):
        """Empty BRIEFINGS.md with no [[briefings]] should fall back to admin."""
        mount = tmp_path / "mount"
        workspace = mount / "Users" / "alice" / "istota" / "config"
        workspace.mkdir(parents=True)
        (workspace / "BRIEFINGS.md").write_text("")

        admin_briefing = BriefingConfig(
            name="morning", cron="0 6 * * *",
            conversation_token="room1",
        )
        user = UserConfig(briefings=[admin_briefing])
        config = Config(nextcloud_mount_path=mount, users={"alice": user})

        result = get_briefings_for_user(config, "alice")
        # Workspace exists but has no briefings → admin preserved
        assert len(result) == 1
        assert result[0].name == "morning"

    def test_markdown_without_toml_falls_back_to_admin(self, tmp_path):
        """BRIEFINGS.md with only markdown (no toml block) falls back to admin."""
        mount = tmp_path / "mount"
        workspace = mount / "Users" / "alice" / "istota" / "config"
        workspace.mkdir(parents=True)
        (workspace / "BRIEFINGS.md").write_text(
            "# Briefings\n\nJust some notes about briefings.\n"
        )

        admin_briefing = BriefingConfig(
            name="morning", cron="0 6 * * *",
            conversation_token="room1",
        )
        user = UserConfig(briefings=[admin_briefing])
        config = Config(nextcloud_mount_path=mount, users={"alice": user})

        result = get_briefings_for_user(config, "alice")
        assert len(result) == 1
        assert result[0].name == "morning"
