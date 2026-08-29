"""Retiring workspace ``BRIEFINGS.md`` as an input.

The file used to sit on top of the whole stack: ``get_briefings_for_user``
applied it over ``UserConfig.briefings``, which the ``briefing_configs``
overlay had already written into. So a schedule set in the web UI was
overridden, silently, by a file the UI never mentions.

These tests pin the two halves of the retirement: the read path no longer
consults the file, and a one-shot import carries whatever was in it into the
table before that happens.
"""

from __future__ import annotations

from istota import user_briefings
from istota.config import BriefingConfig, Config, UserConfig


def _write_briefings_md(mount, user_id, toml_body, bot_dir="istota"):
    config_dir = mount / "Users" / user_id / bot_dir / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / "BRIEFINGS.md"
    path.write_text(
        "# Briefing Schedule\n\nSome prose.\n\n```toml\n" + toml_body + "```\n"
    )
    return path


class TestTheReadPathIgnoresTheFile:
    def test_a_file_briefing_does_not_override_the_config_one(self, tmp_path):
        """The defect this retires: the file beat the briefing_configs row.

        ``_apply_user_briefings`` has already put the DB row on
        ``UserConfig.briefings`` by the time the scheduler reads. Before this
        change the file's ``cron``/``conversation_token``/``output`` replaced
        all three, so the settings page showed 09:00 to one room and the
        daemon ran 06:00 to another.
        """
        from istota.skills.briefing import get_briefings_for_user

        mount = tmp_path / "mount"
        _write_briefings_md(
            mount, "alice",
            '[[briefings]]\n'
            'name = "morning"\n'
            'cron = "0 6 * * *"\n'
            'conversation_token = "old-room"\n'
            'output = "talk"\n',
        )

        config = Config(nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[
            BriefingConfig(
                name="morning",
                cron="0 9 * * *",
                conversation_token="new-room",
                output="email",
            ),
        ])

        got = get_briefings_for_user(config, "alice")
        assert [b.name for b in got] == ["morning"]
        assert got[0].cron == "0 9 * * *"
        assert got[0].conversation_token == "new-room"
        assert got[0].output == "email"

    def test_a_file_only_briefing_is_not_scheduled(self, tmp_path):
        """A briefing that exists only in the file no longer runs.

        The import below is what stops this being data loss on upgrade.
        """
        from istota.skills.briefing import get_briefings_for_user

        mount = tmp_path / "mount"
        _write_briefings_md(
            mount, "alice",
            '[[briefings]]\nname = "extra"\ncron = "0 6 * * *"\n',
        )

        config = Config(nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        assert get_briefings_for_user(config, "alice") == []

    def test_the_workspace_loader_is_gone(self):
        """No caller may reintroduce the file as an input by importing it."""
        from istota.skills import briefing

        assert not hasattr(briefing, "_load_workspace_briefings")


class TestTheOneShotImport:
    def test_a_file_only_briefing_lands_in_the_table(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        _write_briefings_md(
            mount, "alice",
            '[[briefings]]\n'
            'name = "morning"\n'
            'cron = "0 6 * * *"\n'
            'conversation_token = "room1"\n'
            'output = "talk,email"\n',
        )
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        assert user_briefings.import_from_workspace_files(db_path, config) == 1

        rows = user_briefings.list_briefings(db_path, "alice")
        assert [r.name for r in rows] == ["morning"]
        assert rows[0].cron == "0 6 * * *"
        assert rows[0].conversation_token == "room1"
        assert rows[0].output == "talk,email"
        assert rows[0].enabled is True

    def test_the_file_value_wins_over_an_existing_row(self, db_path, tmp_path):
        """Freezing today's behaviour, not today's stored value.

        The file was the live authority before this change, so a user with
        both must keep running the file's schedule across the upgrade. Any
        other choice starts honouring a web-UI edit that was never in effect.
        """
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning",
            cron="0 9 * * *", conversation_token="new-room", output="email",
        )

        mount = tmp_path / "mount"
        _write_briefings_md(
            mount, "alice",
            '[[briefings]]\n'
            'name = "morning"\n'
            'cron = "0 6 * * *"\n'
            'conversation_token = "old-room"\n'
            'output = "talk"\n',
        )
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        user_briefings.import_from_workspace_files(db_path, config)

        row = user_briefings.get_briefing(db_path, "alice", "morning")
        assert row is not None
        assert row.cron == "0 6 * * *"
        assert row.conversation_token == "old-room"
        assert row.output == "talk"

    def test_it_runs_once_per_user(self, db_path, tmp_path):
        """A second pass must not resurrect a briefing deleted in the UI."""
        mount = tmp_path / "mount"
        _write_briefings_md(
            mount, "alice",
            '[[briefings]]\nname = "morning"\ncron = "0 6 * * *"\n',
        )
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        assert user_briefings.import_from_workspace_files(db_path, config) == 1
        user_briefings.delete_briefing(db_path, "alice", "morning")

        assert user_briefings.import_from_workspace_files(db_path, config) == 0
        assert user_briefings.list_briefings(db_path, "alice") == []

    def test_a_later_edit_is_not_overwritten(self, db_path, tmp_path):
        """After the one-shot, the table is the only authority."""
        mount = tmp_path / "mount"
        _write_briefings_md(
            mount, "alice",
            '[[briefings]]\nname = "morning"\ncron = "0 6 * * *"\n',
        )
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        user_briefings.import_from_workspace_files(db_path, config)
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning", cron="0 9 * * *",
        )
        user_briefings.import_from_workspace_files(db_path, config)

        row = user_briefings.get_briefing(db_path, "alice", "morning")
        assert row is not None and row.cron == "0 9 * * *"

    def test_components_in_the_file_are_not_carried_over(self, db_path, tmp_path):
        """The read path has discarded these since blocks became the content model.

        Importing them would feed ``_migrate_components`` and hand the user
        content they have not had for releases.
        """
        mount = tmp_path / "mount"
        _write_briefings_md(
            mount, "alice",
            '[[briefings]]\n'
            'name = "morning"\n'
            'cron = "0 6 * * *"\n'
            '\n'
            '[briefings.components]\n'
            'markets = true\n'
            'news = true\n',
        )
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        user_briefings.import_from_workspace_files(db_path, config)

        row = user_briefings.get_briefing(db_path, "alice", "morning")
        assert row is not None and row.components == {}

    def test_no_mount_is_a_noop(self, db_path):
        config = Config(db_path=db_path, nextcloud_mount_path=None)
        config.users["alice"] = UserConfig(briefings=[])
        assert user_briefings.import_from_workspace_files(db_path, config) == 0

    def test_a_missing_file_is_a_noop(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])
        assert user_briefings.import_from_workspace_files(db_path, config) == 0

    def test_an_unparseable_file_does_not_burn_the_sentinel(self, db_path, tmp_path):
        """A transient read failure must not cost the user their schedule.

        Marking the user done on a parse error means the file is never looked
        at again and whatever was in it is gone for good.
        """
        mount = tmp_path / "mount"
        path = _write_briefings_md(mount, "alice", "this is not [[[ valid toml\n")
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        assert user_briefings.import_from_workspace_files(db_path, config) == 0

        path.write_text(
            "# Briefing Schedule\n\n```toml\n"
            '[[briefings]]\nname = "morning"\ncron = "0 6 * * *"\n'
            "```\n"
        )
        assert user_briefings.import_from_workspace_files(db_path, config) == 1

    def test_an_entry_without_a_cron_is_skipped(self, db_path, tmp_path):
        mount = tmp_path / "mount"
        _write_briefings_md(
            mount, "alice",
            '[[briefings]]\nname = "morning"\n\n'
            '[[briefings]]\nname = "evening"\ncron = "0 18 * * *"\n',
        )
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        assert user_briefings.import_from_workspace_files(db_path, config) == 1
        assert [r.name for r in user_briefings.list_briefings(db_path, "alice")] == [
            "evening",
        ]


class TestNothingSeedsTheFileAnyMore:
    def test_ensure_user_directories_writes_no_briefings_md(self, tmp_path):
        from istota.storage import ensure_user_directories_v2

        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(nextcloud_mount_path=mount, bot_name="Istota")

        assert ensure_user_directories_v2(config, "alice") is True
        config_dir = mount / "Users" / "alice" / "istota" / "config"
        assert not (config_dir / "BRIEFINGS.md").exists()

    def test_the_shipped_example_points_at_the_real_surface(self):
        from istota.storage import BRIEFINGS_EXAMPLE

        assert "[[briefings]]" not in BRIEFINGS_EXAMPLE
        assert "[briefings.components]" not in BRIEFINGS_EXAMPLE

    def test_the_skill_body_does_not_teach_the_file(self):
        """The skill is prompt text, so a stale example is a wrong action.

        It used to document ``[briefings.components]`` in full, which the read
        path discarded — the model would write the file, confirm, and change
        nothing.
        """
        from pathlib import Path

        import istota

        body = (
            Path(istota.__file__).parent
            / "skills" / "briefings_config" / "skill.md"
        ).read_text()

        assert "[briefings.components]" not in body
        assert "istota briefing" in body
