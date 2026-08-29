"""Configuration loading for istota.briefing_loader module.

``get_briefings_for_user`` used to apply a workspace ``BRIEFINGS.md`` over the
config's own briefings. That file is retired as an input — see
``tests/test_briefings_md_retirement.py`` for the read path ignoring it and for
the one-shot import that carries its contents into ``briefing_configs``. What
is left here is the config read itself, plus the parser the import inherited.
"""

from istota.skills.briefing import get_briefings_for_user
from istota.config import (
    BriefingConfig,
    Config,
    UserConfig,
)
from istota.user_briefings import parse_briefings_md


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


class TestParseBriefingsMd:
    """The file parser, now owned by the one-shot import.

    ``None`` and ``[]`` mean different things to the caller: ``None`` is "could
    not read this", which leaves the user's sentinel unset for another try,
    and ``[]`` is "read fine, named nothing".
    """

    def test_valid_toml(self):
        entries = parse_briefings_md(_wrap_toml(
            '[[briefings]]\n'
            'name = "morning"\n'
            'cron = "0 7 * * *"\n'
            'conversation_token = "room1"\n'
            'output = "talk"\n'
        ))
        assert entries == [{
            "name": "morning",
            "cron": "0 7 * * *",
            "conversation_token": "room1",
            "output": "talk",
        }]

    def test_components_are_returned_but_the_caller_drops_them(self):
        entries = parse_briefings_md(_wrap_toml(
            '[[briefings]]\n'
            'name = "morning"\n'
            'cron = "0 7 * * *"\n'
            '\n'
            '[briefings.components]\n'
            'calendar = true\n'
        ))
        assert entries is not None
        assert entries[0]["components"] == {"calendar": True}

    def test_invalid_toml_is_unreadable(self):
        assert parse_briefings_md(_wrap_toml("this is not valid [[[ toml")) is None

    def test_a_non_list_briefings_key_is_unreadable(self):
        assert parse_briefings_md(_wrap_toml('briefings = "morning"\n')) is None

    def test_empty_file_reads_as_no_briefings(self):
        assert parse_briefings_md("") == []

    def test_markdown_without_a_toml_block_reads_as_no_briefings(self):
        assert parse_briefings_md(
            "# Briefings\n\nJust some notes about briefings.\n"
        ) == []

    def test_multiple_briefings(self):
        entries = parse_briefings_md(_wrap_toml(
            '[[briefings]]\nname = "morning"\ncron = "0 7 * * *"\n'
            '\n'
            '[[briefings]]\nname = "evening"\ncron = "0 18 * * *"\n'
        ))
        assert [e["name"] for e in entries] == ["morning", "evening"]

    def test_a_non_table_entry_is_dropped(self):
        entries = parse_briefings_md(_wrap_toml('briefings = ["morning"]\n'))
        assert entries == []


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

    def test_components_not_expanded(self, tmp_path):
        # The legacy boolean-component expansion is retired: get_briefings_for_user
        # returns admin briefings verbatim, no defaults merged in.
        mount = tmp_path / "mount"
        mount.mkdir()
        admin_briefing = BriefingConfig(
            name="morning", cron="0 6 * * *",
            conversation_token="room1", components={"markets": True},
        )
        user = UserConfig(briefings=[admin_briefing])
        config = Config(nextcloud_mount_path=mount, users={"alice": user})

        result = get_briefings_for_user(config, "alice")
        assert len(result) == 1
        # Verbatim — no {"enabled": True, ...} expansion.
        assert result[0].components == {"markets": True}

    def test_the_returned_list_is_a_copy(self, tmp_path):
        """A caller mutating the result must not edit the loaded config."""
        mount = tmp_path / "mount"
        mount.mkdir()
        user = UserConfig(briefings=[
            BriefingConfig(name="morning", cron="0 6 * * *"),
        ])
        config = Config(nextcloud_mount_path=mount, users={"alice": user})

        get_briefings_for_user(config, "alice").clear()
        assert len(config.users["alice"].briefings) == 1
