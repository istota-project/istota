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

from pathlib import Path

from istota import user_briefings
from istota.config import BriefingConfig, Config, UserConfig


def _sentinel_set_for(db_path, user_id) -> bool:
    """Whether the one-shot has marked this user done."""
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        row = conn.execute(
            "SELECT 1 FROM _migration_state WHERE name = ?",
            (f"briefings_md_import_v1:{user_id}",),
        ).fetchone()
    finally:
        conn.close()
    return row is not None


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

    def test_a_missing_file_with_a_live_workspace_marks_the_user_done(
        self, db_path, tmp_path,
    ):
        """The user genuinely had no file. Nothing will appear later."""
        mount = tmp_path / "mount"
        (mount / "Users" / "alice" / "istota" / "config").mkdir(parents=True)
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        assert user_briefings.import_from_workspace_files(db_path, config) == 0
        assert _sentinel_set_for(db_path, "alice") is True

    def test_an_absent_workspace_does_not_burn_the_sentinel(self, db_path, tmp_path):
        """The failure the per-user sentinel exists to survive.

        An rclone mount that has not come up leaves a plain empty directory,
        so every path under it is ENOENT and ``Path.exists()`` answers False
        rather than raising. Reading that as "this user had no file" marks them
        done for ever, and their schedule may live only in that file — nothing
        reads it again and the seed that would recreate it is gone.
        """
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        assert user_briefings.import_from_workspace_files(db_path, config) == 0
        assert _sentinel_set_for(db_path, "alice") is False

        # The mount comes back on a later boot; the file is still imported.
        _write_briefings_md(
            mount, "alice",
            '[[briefings]]\nname = "morning"\ncron = "0 6 * * *"\n',
        )
        assert user_briefings.import_from_workspace_files(db_path, config) == 1

    def test_a_nextcloud_shape_requires_a_real_mount_point(self, db_path, tmp_path):
        """``ismount`` is the discriminator, but only where something mounts.

        ``ensure_user_directories_v2`` runs earlier in the same boot and will
        create the workspace on the underlying disk of a dropped mount, so the
        directory existing is not evidence on its own. The local single-user
        install points ``nextcloud_mount_path`` at a plain directory nothing
        ever mounts, which is why the check is gated on ``storage_is_nextcloud``
        — the test above covers that shape.
        """
        mount = tmp_path / "mount"
        (mount / "Users" / "alice" / "istota" / "config").mkdir(parents=True)
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.nextcloud.url = "https://cloud.example.com"
        config.users["alice"] = UserConfig(briefings=[])
        assert config.storage_is_nextcloud is True

        assert user_briefings.import_from_workspace_files(db_path, config) == 0
        assert _sentinel_set_for(db_path, "alice") is False

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


class TestWhatTheFileDoesNotWin:
    """Two columns stay with the row, and both are behaviour changes.

    The import's rule is that the file wins on the three things it actually
    controlled — cron, conversation_token, output. ``enabled`` and ``title``
    are deliberately not among them, because in both cases the old file
    override was suppressing a web-UI setting rather than expressing one.
    """

    def test_a_disabled_row_stays_disabled(self, db_path, tmp_path):
        """Re-enabling here would make the retired bug durable.

        ``_apply_user_briefings`` drops a row with ``enabled=0`` from
        ``UserConfig.briefings``, and the old file merge added the briefing
        straight back by name — so a briefing switched off in the web UI kept
        running. Importing ``enabled = 1`` would carry that forward as a stored
        fact instead of ending it.
        """
        b, _ = user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning", cron="0 9 * * *",
            enabled=False,
        )
        assert b.enabled is False

        mount = tmp_path / "mount"
        _write_briefings_md(
            mount, "alice",
            '[[briefings]]\nname = "morning"\ncron = "0 6 * * *"\n',
        )
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        user_briefings.import_from_workspace_files(db_path, config)

        row = user_briefings.get_briefing(db_path, "alice", "morning")
        assert row is not None
        assert row.enabled is False          # the user's choice stands
        assert row.cron == "0 6 * * *"       # the schedule still comes over

    def test_a_row_title_survives(self, db_path, tmp_path):
        """The file's entry carried a blank title and blanked the row's."""
        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning", cron="0 9 * * *",
            title="First Thing",
        )

        mount = tmp_path / "mount"
        _write_briefings_md(
            mount, "alice",
            '[[briefings]]\nname = "morning"\ncron = "0 6 * * *"\n',
        )
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        user_briefings.import_from_workspace_files(db_path, config)

        row = user_briefings.get_briefing(db_path, "alice", "morning")
        assert row is not None and row.title == "First Thing"

    def test_a_named_entry_with_no_cron_is_reported_by_name(
        self, db_path, tmp_path, caplog,
    ):
        """The one silent resumption, so it is made loud instead.

        An entry with a name and no cron used to *suppress* the briefing: the
        old loader built an entry with an empty cron, it replaced the config
        one by name, and ``check_briefings`` skips a briefing with no cron.
        Skipping the entry leaves the existing row's own cron in place, so the
        briefing starts running again. An absent cron is as likely
        half-finished as deliberate, so it is not inferred as a disable — but
        it is named.
        """
        import logging

        user_briefings.ensure_briefing(
            db_path, user_id="alice", name="morning", cron="0 9 * * *",
        )

        mount = tmp_path / "mount"
        _write_briefings_md(
            mount, "alice", '[[briefings]]\nname = "morning"\n',
        )
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        with caplog.at_level(logging.WARNING, logger="istota.user_briefings"):
            user_briefings.import_from_workspace_files(db_path, config)

        assert any(
            "morning" in r.getMessage() and "no cron" in r.getMessage()
            for r in caplog.records
        ), caplog.text

        row = user_briefings.get_briefing(db_path, "alice", "morning")
        assert row is not None and row.cron == "0 9 * * *"


class TestHostileAndOddFiles:
    """The file is writable by the user and by the model in that user's sandbox."""

    def test_a_non_utf8_file_still_imports(self, db_path, tmp_path):
        """A locale-default decode would fail every boot and never converge.

        ``UnicodeDecodeError`` is a ``ValueError``, so it misses the OSError
        retry and lands in the blanket handler — the sentinel is never set and
        the same file fails again on the next start, for ever.
        """
        mount = tmp_path / "mount"
        config_dir = mount / "Users" / "alice" / "istota" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "BRIEFINGS.md").write_bytes(
            "# Briefing Schedule\n\nCaf\xe9 notes, latin-1 bytes below.\n\n"
            "```toml\n"
            '[[briefings]]\nname = "morning"\ncron = "0 6 * * *"\n'
            "```\n".encode("latin-1")
        )
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        assert user_briefings.import_from_workspace_files(db_path, config) == 1
        row = user_briefings.get_briefing(db_path, "alice", "morning")
        assert row is not None and row.cron == "0 6 * * *"

    def test_an_undeliverable_output_is_not_stored(self, db_path, tmp_path):
        """``ensure_briefing`` refuses one, so storing it writes an unsavable row.

        ``parse_output_target`` resolves ``none`` to no destination.
        """
        mount = tmp_path / "mount"
        _write_briefings_md(
            mount, "alice",
            '[[briefings]]\nname = "morning"\ncron = "0 6 * * *"\n'
            'output = "none"\n',
        )
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        user_briefings.import_from_workspace_files(db_path, config)

        row = user_briefings.get_briefing(db_path, "alice", "morning")
        assert row is not None and row.output == "talk"

    def test_non_string_values_are_rejected_not_stringified(self, db_path, tmp_path):
        """``str()`` on a TOML array writes its Python repr into the column."""
        mount = tmp_path / "mount"
        _write_briefings_md(
            mount, "alice",
            '[[briefings]]\nname = ["a", "b"]\ncron = "0 6 * * *"\n'
            '\n'
            '[[briefings]]\nname = "evening"\ncron = "0 18 * * *"\n'
            'output = ["talk", "email"]\n',
        )
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])

        user_briefings.import_from_workspace_files(db_path, config)

        rows = user_briefings.list_briefings(db_path, "alice")
        assert [r.name for r in rows] == ["evening"]
        assert rows[0].output == "talk"
        assert "[" not in rows[0].output


class TestTheBootPath:
    def test_no_write_lock_is_held_while_the_mount_is_read(self, db_path, tmp_path):
        """The reads happen before the connection is opened.

        One connection wrapping the loop takes a write lock at the first user
        and holds it across every remaining user's stat and read against the
        rclone mount, on the daemon's foreground boot path. A hung FUSE mount
        then blocks every other writer for the 30s busy timeout.
        """
        import sqlite3

        mount = tmp_path / "mount"
        for user in ("alice", "bob"):
            _write_briefings_md(
                mount, user,
                f'[[briefings]]\nname = "{user}-am"\ncron = "0 6 * * *"\n',
            )
        config = Config(db_path=db_path, nextcloud_mount_path=mount)
        config.users["alice"] = UserConfig(briefings=[])
        config.users["bob"] = UserConfig(briefings=[])

        observed = []
        real_read_text = Path.read_text

        def _spy(self, *a, **kw):
            # A second writer must get through while the files are being read.
            other = sqlite3.connect(db_path, timeout=0.2)
            try:
                other.execute(
                    "INSERT OR IGNORE INTO _migration_state (name) VALUES (?)",
                    (f"probe:{len(observed)}",),
                )
                other.commit()
                observed.append(True)
            except sqlite3.OperationalError:
                observed.append(False)
            finally:
                other.close()
            return real_read_text(self, *a, **kw)

        import unittest.mock as _mock
        with _mock.patch.object(Path, "read_text", _spy):
            assert user_briefings.import_from_workspace_files(db_path, config) == 2

        assert observed and all(observed), (
            f"a write lock was held across the mount reads: {observed}"
        )

    def test_the_scheduler_boots_it_in_the_required_order(self):
        """The one call site, and the ordering its comment says is load-bearing.

        Every other test here calls the import directly, so removing the call
        or reordering it would go green while silently losing every affected
        user's briefing. Read by text — this fails loudly (no match, no test)
        rather than drifting.
        """
        import inspect

        from istota import scheduler

        src = inspect.getsource(scheduler.run_daemon)
        seed = src.index("import_from_user_configs(")
        workspace = src.index("import_from_workspace_files(")
        apply = src.index("_apply_user_briefings(config)")

        assert seed < workspace < apply, (
            "the workspace import must land after the TOML seed (so the file "
            "wins, as it did before the retirement) and before the overlay is "
            "re-applied (so the in-memory config sees the imported rows)"
        )
