"""`maintenance/workspace_localize`: moving a mounted workspace onto local disk.

Real trees in `tmp_path` and a real framework database. The copy reads the
source with `O_NOFOLLOW` at every level, so the cases plant the things it must
not follow or copy (a symlink, a FIFO) rather than describing them.
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tomllib
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import apply
from istota.config import Config, NextcloudConfig, UserConfig
from istota.maintenance import workspace_localize as wl
from istota.memory.search import _insert_chunks

REPO = Path(__file__).resolve().parent.parent


def _init_db(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript((REPO / "schema.sql").read_text())
    return conn


def _chunk(conn, user_id: str, source_type: str, source_id: str, text: str) -> None:
    with patch("istota.memory.search.ensure_vec_table", return_value=False):
        inserted = _insert_chunks(
            conn, user_id, source_type, source_id, [text], {"file_path": source_id},
        )
    assert inserted == 1


def _sources(conn) -> set[tuple[str, str]]:
    return set(conn.execute("SELECT user_id, source_id FROM memory_chunks").fetchall())


class TestForgetRoot:
    """The delete half of the memory-index move. `index_file` keys a row on the
    file's resolved path and a reindex only replaces the rows under the path it
    indexes now, so rows under the old root outlive the move without this."""

    @pytest.fixture
    def conn(self, tmp_path):
        conn = _init_db(tmp_path / "istota.db")
        _chunk(conn, "alice", "memory_file", "/mnt/shared/Users/alice/memories/a.md", "alpha old")
        _chunk(conn, "alice", "user_memory", "/mnt/shared/Users/alice/istota/config/USER.md", "user old")
        _chunk(conn, "alice", "memory_file", "/data/workspace/Users/alice/memories/a.md", "alpha new")
        _chunk(conn, "alice", "conversation", "42", "a conversation")
        _chunk(conn, "channel:room1", "channel_memory_durable", "/mnt/shared/Channels/room1/CHANNEL.md", "room old")
        _chunk(conn, "bob", "memory_file", "/mnt/shared/Users/bob/memories/b.md", "bob old")
        _chunk(conn, "alice", "memory_file", "/mnt/shared-elsewhere/notes.md", "a sibling root")
        yield conn
        conn.close()

    def test_rows_under_the_old_root_are_removed(self, conn):
        removed = wl.forget_root(conn, "/mnt/shared", ["alice", "channel:room1"])
        assert removed == 3
        remaining = _sources(conn)
        assert ("alice", "/mnt/shared/Users/alice/memories/a.md") not in remaining
        assert ("alice", "/mnt/shared/Users/alice/istota/config/USER.md") not in remaining
        assert ("channel:room1", "/mnt/shared/Channels/room1/CHANNEL.md") not in remaining

    def test_rows_under_the_new_root_and_conversations_are_kept(self, conn):
        wl.forget_root(conn, "/mnt/shared", ["alice", "channel:room1"])
        remaining = _sources(conn)
        assert ("alice", "/data/workspace/Users/alice/memories/a.md") in remaining
        assert ("alice", "42") in remaining

    def test_another_users_rows_are_kept(self, conn):
        wl.forget_root(conn, "/mnt/shared", ["alice"])
        assert ("bob", "/mnt/shared/Users/bob/memories/b.md") in _sources(conn)

    def test_a_sibling_root_sharing_the_prefix_is_kept(self, conn):
        """`/mnt/shared-elsewhere` starts with `/mnt/shared`; the match is on a
        path boundary."""
        wl.forget_root(conn, "/mnt/shared/", ["alice"])
        assert ("alice", "/mnt/shared-elsewhere/notes.md") in _sources(conn)

    def test_the_search_index_follows_the_rows(self, conn):
        wl.forget_root(conn, "/mnt/shared", ["alice", "channel:room1"])
        hits = conn.execute(
            "SELECT rowid FROM memory_chunks_fts WHERE memory_chunks_fts MATCH 'old'"
        ).fetchall()
        assert len(hits) == 1  # bob's

    @pytest.mark.parametrize("root", ["", "/", "relative/path"])
    def test_a_root_that_names_everything_is_refused(self, conn, root):
        with pytest.raises(ValueError):
            wl.forget_root(conn, root, ["alice"])
        assert len(_sources(conn)) == 7


# --- the copy -------------------------------------------------------------------

BOT_DIR = "istota"
OLD_MTIME = 1_700_000_000


def _put(root: Path, rel: str, text: str = "x", *, mode: int = 0o644) -> Path:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    path.chmod(mode)
    os.utime(path, (OLD_MTIME, OLD_MTIME))
    return path


@pytest.fixture
def mount(tmp_path) -> Path:
    """A bot's Nextcloud files: the workspace's own entries and three that are not."""
    root = tmp_path / "mount"
    _put(root, f"Users/alice/{BOT_DIR}/config/USER.md", "# alice\n", mode=0o600)
    _put(root, "Users/alice/memories/2026-01-01.md", "a dated memory\n")
    _put(root, "Users/bob/inbox/photo.jpg", "jpeg bytes")
    _put(root, "Channels/room1/CHANNEL.md", "room notes\n")
    _put(root, "Groups/g1/GROUP.md", "group notes\n")
    _put(root, "PERSONA.md", "persona\n")
    _put(root, "Talk/shared.png", "a Talk file")
    _put(root, "Backups/db/snapshots/x.db", "a snapshot")
    _put(root, "shared-by-carol/doc.md", "a folder a user shared")
    return root


def _tree(root: Path) -> dict[str, str]:
    return {
        str(p.relative_to(root)): p.read_text()
        for p in sorted(root.rglob("*")) if p.is_file() and not p.is_symlink()
    }


class TestCopy:
    def test_the_workspace_entries_are_copied_and_nothing_else(self, mount, tmp_path):
        dest = tmp_path / "local"
        report = wl.copy_workspace(mount, dest, bot_dir_name=BOT_DIR)
        assert report.exit_code() == wl.EXIT_OK, report.to_dict()
        assert _tree(dest) == {
            f"Users/alice/{BOT_DIR}/config/USER.md": "# alice\n",
            "Users/alice/memories/2026-01-01.md": "a dated memory\n",
            "Users/bob/inbox/photo.jpg": "jpeg bytes",
            "Channels/room1/CHANNEL.md": "room notes\n",
            "Groups/g1/GROUP.md": "group notes\n",
            "PERSONA.md": "persona\n",
        }
        assert report.entries["Users"].files == 3
        assert report.entries["PERSONA.md"].bytes == len("persona\n")

    def test_mtimes_are_kept_and_modes_are_the_workspaces(self, mount, tmp_path):
        dest = tmp_path / "local"
        wl.copy_workspace(mount, dest, bot_dir_name=BOT_DIR)
        user_md = dest / f"Users/alice/{BOT_DIR}/config/USER.md"
        assert user_md.stat().st_mtime == OLD_MTIME
        assert user_md.stat().st_mode & 0o777 == 0o644
        assert (dest / "Users/alice").stat().st_mode & 0o777 == 0o755

    def test_a_symlink_and_a_fifo_are_skipped_and_reported(self, mount, tmp_path):
        outside = _put(tmp_path, "outside/secret.txt", "not the workspace's")
        (mount / "Users/alice/memories/link.md").symlink_to(outside)
        (mount / "Users/alice/escape").symlink_to(outside.parent)
        os.mkfifo(mount / "Channels/room1/pipe")
        dest = tmp_path / "local"
        report = wl.copy_workspace(mount, dest, bot_dir_name=BOT_DIR)
        assert report.exit_code() == wl.EXIT_SKIPPED
        assert sorted(report.skipped) == [
            ("Channels/room1/pipe", "fifo"),
            ("Users/alice/escape", "symlink"),
            ("Users/alice/memories/link.md", "symlink"),
        ]
        assert not (dest / "Users/alice/memories/link.md").exists()
        assert not (dest / "Users/alice/escape").exists()
        assert not (dest / "Channels/room1/pipe").exists()
        assert "not the workspace's" not in _tree(dest).values()

    def test_a_symlinked_top_level_entry_is_skipped(self, mount, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        _put(elsewhere, "g2/GROUP.md", "someone else's")
        shutil.rmtree(mount / "Groups")
        (mount / "Groups").symlink_to(elsewhere)
        report = wl.copy_workspace(mount, tmp_path / "local", bot_dir_name=BOT_DIR)
        assert ("Groups", "symlink") in report.skipped
        assert not (tmp_path / "local/Groups").exists()

    def test_a_destination_with_files_is_refused_without_resume(self, mount, tmp_path):
        dest = tmp_path / "local"
        _put(dest, "Users/alice/already.md", "here first")
        with pytest.raises(wl.LocalizeRefused, match="--resume"):
            wl.copy_workspace(mount, dest, bot_dir_name=BOT_DIR)
        assert _tree(dest) == {"Users/alice/already.md": "here first"}

    def test_an_empty_destination_directory_is_accepted(self, mount, tmp_path):
        dest = tmp_path / "local"
        dest.mkdir()
        assert wl.copy_workspace(mount, dest, bot_dir_name=BOT_DIR).exit_code() == wl.EXIT_OK

    def test_overlapping_trees_are_refused(self, mount):
        with pytest.raises(wl.LocalizeRefused, match="overlap"):
            wl.copy_workspace(mount, mount / "Users" / "local", bot_dir_name=BOT_DIR)

    def test_resume_keeps_what_matches_and_copies_the_rest(self, mount, tmp_path):
        dest = tmp_path / "local"
        wl.copy_workspace(mount, dest, bot_dir_name=BOT_DIR)
        _put(mount, "Users/alice/memories/2026-01-02.md", "a new day\n")
        _put(mount, "Channels/room1/CHANNEL.md", "room notes, longer now\n")
        report = wl.copy_workspace(mount, dest, bot_dir_name=BOT_DIR, resume=True)
        assert report.exit_code() == wl.EXIT_OK
        assert (dest / "Users/alice/memories/2026-01-02.md").read_text() == "a new day\n"
        assert (dest / "Channels/room1/CHANNEL.md").read_text() == "room notes, longer now\n"
        assert report.entries["Users"].unchanged == 3
        assert report.entries["Users"].files == 1
        assert report.entries["Channels"].files == 1

    def test_a_dry_run_writes_nothing(self, mount, tmp_path):
        before = _tree(mount)
        dest = tmp_path / "local"
        report = wl.copy_workspace(mount, dest, bot_dir_name=BOT_DIR, dry_run=True)
        assert not dest.exists()
        assert _tree(mount) == before
        assert report.entries["Users"].files == 3
        assert report.dry_run

    @pytest.mark.requires_dac
    def test_an_unreadable_file_stops_the_copy_with_the_path(self, mount, tmp_path):
        (mount / "Groups/g1/GROUP.md").chmod(0)
        report = wl.copy_workspace(mount, tmp_path / "local", bot_dir_name=BOT_DIR)
        assert report.exit_code() == wl.EXIT_IO
        assert report.error.startswith("Groups/g1/GROUP.md: ")


class TestNotes:
    def test_moved_md_is_written_to_the_source_only(self, mount, tmp_path):
        dest = tmp_path / "local"
        report = wl.copy_workspace(mount, dest, bot_dir_name=BOT_DIR)
        wl.write_notes(mount, report, bot_dir_name=BOT_DIR, text="moved\n")
        assert (mount / f"Users/alice/{BOT_DIR}/{wl.NOTE_NAME}").read_text() == "moved\n"
        assert not (dest / f"Users/alice/{BOT_DIR}/{wl.NOTE_NAME}").exists()
        # bob has no bot folder, so no note and no folder made for one.
        assert report.noted == ["alice"]
        assert not (mount / f"Users/bob/{BOT_DIR}").exists()

    def test_a_resumed_copy_does_not_carry_the_note(self, mount, tmp_path):
        dest = tmp_path / "local"
        report = wl.copy_workspace(mount, dest, bot_dir_name=BOT_DIR)
        wl.write_notes(mount, report, bot_dir_name=BOT_DIR, text="moved\n")
        wl.copy_workspace(mount, dest, bot_dir_name=BOT_DIR, resume=True)
        assert not (dest / f"Users/alice/{BOT_DIR}/{wl.NOTE_NAME}").exists()

    def test_a_symlinked_bot_folder_gets_no_note(self, mount, tmp_path):
        target = tmp_path / "target"
        target.mkdir()
        _put(mount, "Users/carol/placeholder", "")
        (mount / f"Users/carol/{BOT_DIR}").symlink_to(target)
        report = wl.Report("", "", False)
        wl.write_notes(mount, report, bot_dir_name=BOT_DIR, text="moved\n")
        assert list(target.iterdir()) == []
        assert [user for user, _ in report.note_failures] == ["carol"]

    def test_the_note_says_where_the_files_went(self):
        text = wl.note_text("Istota", datetime(2026, 10, 12, tzinfo=timezone.utc))
        assert "web UI" in text and "2026-10-12" in text and "does not reach Istota" in text


# --- the config -------------------------------------------------------------------

WIZARD_CONFIG = """\
# Istota configuration, written once by `istota setup` and yours to edit.
# Nothing regenerates it.

bot_name = "Istota"
workspace_path = "/mnt/shared"
nextcloud_mount_path = "/mnt/shared"

[nextcloud]
url = "https://cloud.example.test"
username = "istota"
dav_prefix = "Shared Files"
auto_share_bot_dir = true

[talk]
enabled = true
"""


class TestLocalizeConfig:
    def test_the_wizards_config_is_pointed_at_the_local_tree(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(WIZARD_CONFIG)
        path.chmod(0o600)
        assert wl.localize_config(path, "/data/workspace") is True
        document = tomllib.loads(path.read_text())
        assert document["workspace_path"] == "/data/workspace"
        assert "nextcloud_mount_path" not in document
        assert document["nextcloud"]["auto_share_bot_dir"] is False
        assert document["nextcloud"]["dav_prefix"] == "Shared Files"
        assert document["talk"] == {"enabled": True}
        assert path.read_text().startswith("# Istota configuration, written once")
        assert path.stat().st_mode & 0o777 == 0o600

    def test_the_previous_file_is_kept_once(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text(WIZARD_CONFIG)
        wl.localize_config(path, "/data/workspace")
        backup = tmp_path / "config.toml.pre-localize"
        assert backup.read_text() == WIZARD_CONFIG
        assert wl.localize_config(path, "/data/workspace") is False
        assert backup.read_text() == WIZARD_CONFIG

    def test_a_config_written_by_apply_is_refused_and_left_alone(self, tmp_path):
        text = apply.CONFIG_HEADER + WIZARD_CONFIG.split("\n\n", 1)[1]
        path = tmp_path / "config.toml"
        path.write_text(text)
        with pytest.raises(wl.LocalizeRefused, match="the plan owns it"):
            wl.localize_config(path, "/data/workspace")
        assert path.read_text() == text
        assert not (tmp_path / "config.toml.pre-localize").exists()

    def test_a_config_without_nextcloud_is_refused(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text('workspace_path = "/data/workspace"\n')
        with pytest.raises(wl.LocalizeRefused, match="no mount to move from"):
            wl.localize_config(path, "/data/workspace")

    def test_a_symlinked_config_is_refused(self, tmp_path):
        real = tmp_path / "real.toml"
        real.write_text(WIZARD_CONFIG)
        (tmp_path / "config.toml").symlink_to(real)
        with pytest.raises(OSError):
            wl.localize_config(tmp_path / "config.toml", "/data/workspace")
        assert real.read_text() == WIZARD_CONFIG


# --- the reindex -------------------------------------------------------------------


class TestReindexMovedWorkspace:
    @pytest.fixture
    def moved(self, tmp_path):
        local = tmp_path / "local"
        _put(local, f"Users/alice/{BOT_DIR}/config/USER.md", "alice likes tea\n")
        _put(local, "Users/alice/memories/2026-01-01.md", "a dated memory about kites\n")
        _put(local, f"Users/alice/{BOT_DIR}/playbooks/deploy.md", "# Deploy\n\nrun the deploy script\n")
        _put(local, "Channels/room1/CHANNEL.md", "room notes about boats\n")
        config = Config(
            db_path=tmp_path / "istota.db",
            temp_dir=tmp_path / "temp",
            workspace_path=local,
            nextcloud=NextcloudConfig(url="https://cloud.example.test", username="istota"),
            users={"alice": UserConfig()},
        )
        conn = _init_db(config.db_path)
        _chunk(conn, "alice", "user_memory", f"/mnt/shared/Users/alice/{BOT_DIR}/config/USER.md", "stale tea")
        _chunk(conn, "alice", "playbook", f"/mnt/shared/Users/alice/{BOT_DIR}/playbooks/deploy.md", "stale deploy")
        _chunk(conn, "channel:room1", "channel_memory_durable", "/mnt/shared/Channels/room1/CHANNEL.md", "stale boats")
        yield config, conn, local
        conn.close()

    def test_rows_move_from_the_old_root_to_the_new(self, moved):
        config, conn, local = moved
        assert config.bot_dir_name == BOT_DIR
        with patch("istota.memory.search.ensure_vec_table", return_value=False), \
                patch("istota.memory.search.embed_batch", return_value=None):
            stats = wl.reindex_moved_workspace(conn, config, "/mnt/shared")
        sources = _sources(conn)
        assert not [s for s in sources if s[1].startswith("/mnt/shared/")], sources
        real = os.path.realpath(local)
        assert ("alice", f"{real}/Users/alice/{BOT_DIR}/config/USER.md") in sources
        assert ("alice", f"{real}/Users/alice/memories/2026-01-01.md") in sources
        assert ("alice", f"{real}/Users/alice/{BOT_DIR}/playbooks/deploy.md") in sources
        assert ("channel:room1", f"{real}/Channels/room1/CHANNEL.md") in sources
        assert stats["removed"] == 3

    def test_an_old_root_holding_the_workspace_is_refused(self, moved):
        config, conn, local = moved
        before = _sources(conn)
        with pytest.raises(wl.LocalizeRefused):
            wl.reindex_moved_workspace(conn, config, str(local.parent))
        assert _sources(conn) == before


# --- the CLI, through `istota.cli.main` -----------------------------------------------


class TestTheCli:
    """`istota workspace localize` and `localize-config` as the stack runs them."""

    @pytest.fixture
    def install(self, mount, tmp_path):
        config = tmp_path / "config" / "config.toml"
        config.parent.mkdir()
        config.write_text(
            f'db_path = "{tmp_path / "db" / "istota.db"}"\n'
            f'temp_dir = "{tmp_path / "tmp"}"\n'
            f'workspace_path = "{mount}"\n'
            f'nextcloud_mount_path = "{mount}"\n\n'
            "[nextcloud]\n"
            'url = "https://cloud.example.test"\n'
            'username = "istota"\n'
            'app_password = "pw"\n'
        )
        return config

    def _istota(self, config: Path, *args: str):
        import subprocess
        import sys

        env = {k: v for k, v in os.environ.items() if not k.startswith("ISTOTA_")}
        return subprocess.run(
            [sys.executable, "-c", "from istota.cli import main; main()", "-c", str(config), *args],
            capture_output=True, text=True, env=env, timeout=120,
        )

    def test_a_copy_without_the_scheduler_stopped_is_refused(self, install, mount, tmp_path):
        result = self._istota(install, "workspace", "localize", "--from", str(mount), "--to", str(tmp_path / "local"))
        assert result.returncode == wl.EXIT_REFUSED, result.stdout + result.stderr
        assert "--scheduler-stopped" in result.stderr
        assert not (tmp_path / "local").exists()

    def test_a_from_that_is_not_the_configured_workspace_is_refused(self, install, tmp_path):
        other = tmp_path / "other"
        other.mkdir()
        result = self._istota(install, "workspace", "localize", "--from", str(other),
                              "--to", str(tmp_path / "local"), "--scheduler-stopped")
        assert result.returncode == wl.EXIT_REFUSED, result.stdout + result.stderr
        assert "not the configured workspace" in result.stderr

    def test_the_copy_reports_writes_the_note_and_then_the_config_moves(self, install, mount, tmp_path):
        dest = tmp_path / "local"
        report = tmp_path / "report.json"
        result = self._istota(install, "workspace", "localize", "--from", str(mount), "--to", str(dest),
                              "--scheduler-stopped", "--report", str(report))
        assert result.returncode == wl.EXIT_OK, result.stdout + result.stderr
        assert (dest / "PERSONA.md").read_text() == "persona\n"
        assert (mount / f"Users/alice/{BOT_DIR}/{wl.NOTE_NAME}").exists()
        assert json.loads(report.read_text())["entries"]["Users"]["files"] == 3

        result = self._istota(install, "workspace", "localize-config", "--workspace", str(dest))
        assert result.returncode == 0, result.stdout + result.stderr
        document = tomllib.loads(install.read_text())
        assert document["workspace_path"] == str(dest)
        assert "nextcloud_mount_path" not in document
        assert (install.parent / "config.toml.pre-localize").exists()

        # The copy refuses once the config no longer says mount.
        result = self._istota(install, "workspace", "localize", "--from", str(mount), "--to", str(dest),
                              "--scheduler-stopped", "--resume")
        assert result.returncode == wl.EXIT_REFUSED
        assert "not mount mode" in result.stderr

    def test_skipped_entries_exit_2(self, install, mount, tmp_path):
        os.mkfifo(mount / "Groups/g1/pipe")
        result = self._istota(install, "workspace", "localize", "--from", str(mount), "--to", str(tmp_path / "local"),
                              "--scheduler-stopped", "--no-note", "--report", str(tmp_path / "r.json"))
        assert result.returncode == wl.EXIT_SKIPPED, result.stdout + result.stderr
        assert "skipped Groups/g1/pipe: fifo" in result.stdout
        assert not (mount / f"Users/alice/{BOT_DIR}/{wl.NOTE_NAME}").exists()
