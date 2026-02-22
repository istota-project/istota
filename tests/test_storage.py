"""Configuration loading for istota.storage module."""

from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from datetime import datetime, timedelta

from istota.storage import (
    get_user_base_path,
    get_user_memory_path,
    get_user_memories_path,
    get_user_bot_path,
    get_user_config_path,
    get_user_tasks_file_path,
    get_user_shared_path,
    get_user_scripts_path,
    get_user_briefings_path,
    get_user_persona_path,
    get_user_inbox_path,
    get_channel_base_path,
    get_channel_memory_path,
    get_channel_memories_path,
    ensure_user_directories,
    ensure_user_directories_v2,
    ensure_channel_directories,
    read_user_memory,
    read_user_memory_v2,
    read_channel_memory,
    read_dated_memories,
    init_user_memory,
    init_user_memory_v2,
    init_channel_memory,
    get_memory_line_count,
    get_memory_line_count_v2,
    upload_file_to_inbox,
    upload_file_to_inbox_v2,
    user_directories_exist,
    user_directories_exist_v2,
    MEMORY_TEMPLATE,
    CHANNEL_MEMORY_TEMPLATE,
    BOT_USER_BASE,
    _rclone_mkdir,
    _rclone_path_exists,
    _rclone_cat,
    _rclone_rcat,
)
from istota.storage import (
    share_folder_with_user,
    _migrate_old_layout,
    _migrate_notes_to_workspace,
    _migrate_workspace_files,
    _migrate_workspace_to_bot_dir,
    _build_briefings_seed,
    WORKSPACE_README,
    WORKSPACE_README_EXAMPLE,
    TASKS_FILE_TEMPLATE,
    TASKS_FILE_EXAMPLE,
    BRIEFINGS_TEMPLATE,
    BRIEFINGS_EXAMPLE,
    HEARTBEAT_EXAMPLE,
    INVOICING_EXAMPLE,
    ACCOUNTING_EXAMPLE,
)
from istota.config import Config, NextcloudConfig


class TestPathHelpers:
    def test_user_base_path(self):
        assert get_user_base_path("alice") == "/Users/alice"

    def test_user_memory_path(self):
        assert get_user_memory_path("alice", "istota") == "/Users/alice/istota/config/USER.md"

    def test_user_memories_path(self):
        assert get_user_memories_path("alice") == "/Users/alice/memories"

    def test_user_bot_path(self):
        assert get_user_bot_path("alice", "istota") == "/Users/alice/istota"

    def test_user_bot_path_custom(self):
        assert get_user_bot_path("alice", "mister_jones") == "/Users/alice/mister_jones"

    def test_user_config_path(self):
        assert get_user_config_path("alice", "istota") == "/Users/alice/istota/config"

    def test_user_tasks_file_path(self):
        assert get_user_tasks_file_path("alice", "istota") == "/Users/alice/istota/config/TASKS.md"

    def test_user_shared_path(self):
        assert get_user_shared_path("alice") == "/Users/alice/shared"

    def test_user_scripts_path(self):
        assert get_user_scripts_path("alice") == "/Users/alice/scripts"

    def test_user_briefings_path(self):
        assert get_user_briefings_path("alice", "istota") == "/Users/alice/istota/config/BRIEFINGS.md"

    def test_user_persona_path(self):
        assert get_user_persona_path("alice", "istota") == "/Users/alice/istota/config/PERSONA.md"

    def test_user_inbox_path(self):
        assert get_user_inbox_path("alice") == "/Users/alice/inbox"


class TestMountOperations:
    """Tests for v2 (mount-aware) storage functions using tmp_path."""

    @pytest.fixture
    def mount_config(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        return Config(nextcloud_mount_path=mount)

    def test_ensure_dirs_creates_all(self, mount_config):
        result = ensure_user_directories_v2(mount_config, "alice")
        assert result is True

        base = mount_config.nextcloud_mount_path / "Users" / "alice"
        for subdir in ["inbox", "memories", "istota", "shared", "scripts"]:
            assert (base / subdir).is_dir()
        # istota subdirectories
        assert (base / "istota" / "config").is_dir()
        assert (base / "istota" / "exports").is_dir()

    def test_ensure_dirs_idempotent(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        result = ensure_user_directories_v2(mount_config, "alice")
        assert result is True

        base = mount_config.nextcloud_mount_path / "Users" / "alice"
        for subdir in ["inbox", "memories", "istota", "shared", "scripts"]:
            assert (base / subdir).is_dir()

    def test_user_dirs_exist_all(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        result = user_directories_exist_v2(mount_config, "alice")
        assert result == {"inbox": True, "memories": True, "istota": True, "shared": True, "scripts": True}

    def test_user_dirs_exist_none(self, mount_config):
        result = user_directories_exist_v2(mount_config, "alice")
        assert result == {"inbox": False, "memories": False, "istota": False, "shared": False, "scripts": False}

    def test_read_memory_not_exists(self, mount_config):
        result = read_user_memory_v2(mount_config, "alice")
        assert result is None

    def test_read_memory_empty(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        mem_path = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "USER.md"
        mem_path.write_text("")
        result = read_user_memory_v2(mount_config, "alice")
        assert result is None

    def test_read_memory_content(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        mem_path = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "USER.md"
        mem_path.write_text("Remember: likes coffee")
        result = read_user_memory_v2(mount_config, "alice")
        assert result == "Remember: likes coffee"

    def test_init_memory_creates_file(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        result = init_user_memory_v2(mount_config, "alice")
        assert result is True

        mem_path = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "USER.md"
        assert mem_path.exists()
        assert mem_path.read_text() == MEMORY_TEMPLATE

    def test_init_memory_creates_parent_dirs(self, mount_config):
        # Don't call ensure_user_directories first — init should create parents
        result = init_user_memory_v2(mount_config, "alice")
        assert result is True

        mem_path = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "USER.md"
        assert mem_path.exists()

    def test_memory_line_count(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        mem_path = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "USER.md"
        mem_path.write_text("line1\nline2\nline3\n")
        result = get_memory_line_count_v2(mount_config, "alice")
        assert result == 3  # "line1\nline2\nline3\n".splitlines() == ['line1', 'line2', 'line3']

    def test_memory_line_count_no_file(self, mount_config):
        result = get_memory_line_count_v2(mount_config, "alice")
        assert result is None

    def test_upload_file_to_inbox(self, mount_config, tmp_path):
        ensure_user_directories_v2(mount_config, "alice")

        # Create a source file outside the mount
        src = tmp_path / "doc.txt"
        src.write_text("file contents")

        result = upload_file_to_inbox_v2(mount_config, "alice", src)
        assert result == "/Users/alice/inbox/doc.txt"

        dest = mount_config.nextcloud_mount_path / "Users" / "alice" / "inbox" / "doc.txt"
        assert dest.exists()
        assert dest.read_text() == "file contents"

    def test_upload_file_not_exists(self, mount_config):
        result = upload_file_to_inbox_v2(mount_config, "alice", Path("/nonexistent/file.txt"))
        assert result is None

    def test_istota_readme_created(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        readme = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "README.md"
        assert readme.exists()
        assert readme.read_text() == WORKSPACE_README

    def test_tasks_file_created(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        tasks_file = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "TASKS.md"
        assert tasks_file.exists()
        assert tasks_file.read_text() == TASKS_FILE_TEMPLATE

    def test_tasks_file_not_overwritten(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        tasks_file = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "TASKS.md"
        tasks_file.write_text("- [ ] my task")

        ensure_user_directories_v2(mount_config, "alice")
        assert tasks_file.read_text() == "- [ ] my task"

    def test_istota_readme_not_overwritten(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        readme = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "README.md"
        readme.write_text("custom content")

        ensure_user_directories_v2(mount_config, "alice")
        assert readme.read_text() == "custom content"

    def test_briefings_file_created(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        bf = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "BRIEFINGS.md"
        assert bf.exists()
        content = bf.read_text()
        assert "Briefing Schedule" in content
        assert "# markets = true" in content

    def test_briefings_file_not_overwritten(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        bf = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "BRIEFINGS.md"
        bf.write_text("# my custom config")

        ensure_user_directories_v2(mount_config, "alice")
        assert bf.read_text() == "# my custom config"

    def test_briefings_seed_includes_conversation_token(self, tmp_path):
        from istota.config import BriefingConfig, UserConfig
        mount = tmp_path / "mount"
        mount.mkdir()
        user = UserConfig(briefings=[
            BriefingConfig(name="morning", cron="0 6 * * *", conversation_token="abc123"),
        ])
        config = Config(nextcloud_mount_path=mount, users={"alice": user})
        ensure_user_directories_v2(config, "alice")
        bf = mount / "Users" / "alice" / "istota" / "config" / "BRIEFINGS.md"
        assert 'conversation_token = "abc123"' in bf.read_text()

    def test_briefings_seed_empty_token_when_no_briefings(self, mount_config):
        content = _build_briefings_seed(mount_config, "alice")
        assert 'conversation_token = ""' in content

    def test_examples_directory_created(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        examples_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "examples"
        assert examples_dir.is_dir()
        for filename in ["README.md", "TASKS.md", "BRIEFINGS.md", "HEARTBEAT.md", "INVOICING.md", "ACCOUNTING.md"]:
            assert (examples_dir / filename).exists()

    def test_examples_contain_documentation(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        examples_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "examples"
        assert (examples_dir / "README.md").read_text() == WORKSPACE_README_EXAMPLE
        assert (examples_dir / "TASKS.md").read_text() == TASKS_FILE_EXAMPLE
        assert (examples_dir / "BRIEFINGS.md").read_text() == BRIEFINGS_EXAMPLE
        assert (examples_dir / "HEARTBEAT.md").read_text() == HEARTBEAT_EXAMPLE
        assert (examples_dir / "INVOICING.md").read_text() == INVOICING_EXAMPLE
        assert (examples_dir / "ACCOUNTING.md").read_text() == ACCOUNTING_EXAMPLE

    def test_examples_always_overwritten(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        examples_dir = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "examples"
        (examples_dir / "README.md").write_text("old content")

        ensure_user_directories_v2(mount_config, "alice")
        assert (examples_dir / "README.md").read_text() == WORKSPACE_README_EXAMPLE

    def test_istota_readme_mentions_examples(self, mount_config):
        ensure_user_directories_v2(mount_config, "alice")
        readme = mount_config.nextcloud_mount_path / "Users" / "alice" / "istota" / "README.md"
        assert "examples/" in readme.read_text()

    def test_persona_seeded_from_global(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (tmp_path / "config" / "persona.md").write_text("You are {BOT_NAME}, a helpful bot.")
        config = Config(nextcloud_mount_path=mount, skills_dir=skills_dir)
        ensure_user_directories_v2(config, "alice")
        persona = mount / "Users" / "alice" / "istota" / "config" / "PERSONA.md"
        assert persona.exists()
        assert persona.read_text() == "You are {BOT_NAME}, a helpful bot."

    def test_persona_not_overwritten_if_exists(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (tmp_path / "config" / "persona.md").write_text("Global persona")
        config = Config(nextcloud_mount_path=mount, skills_dir=skills_dir)
        # Pre-create user persona
        persona_dir = mount / "Users" / "alice" / "istota" / "config"
        persona_dir.mkdir(parents=True)
        (persona_dir / "PERSONA.md").write_text("Custom persona")
        ensure_user_directories_v2(config, "alice")
        assert (persona_dir / "PERSONA.md").read_text() == "Custom persona"

    def test_persona_not_seeded_when_global_missing(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        # No istota.md created in tmp_path/config/
        config = Config(nextcloud_mount_path=mount, skills_dir=skills_dir)
        ensure_user_directories_v2(config, "alice")
        persona = mount / "Users" / "alice" / "istota" / "config" / "PERSONA.md"
        assert not persona.exists()

    def test_notes_migrated_to_istota(self, mount_config):
        """Old notes/ directory is renamed to workspace/ then to istota/."""
        base = mount_config.nextcloud_mount_path / "Users" / "alice"
        notes_dir = base / "notes"
        notes_dir.mkdir(parents=True)
        (notes_dir / "README.md").write_text("old readme")
        (notes_dir / "draft.md").write_text("my draft")

        ensure_user_directories_v2(mount_config, "alice")

        bot_dir = base / "istota"
        assert bot_dir.is_dir()
        assert not notes_dir.exists()
        assert (bot_dir / "draft.md").read_text() == "my draft"

    def test_notes_migration_skips_if_workspace_exists(self, mount_config):
        """Migration does not overwrite existing workspace/."""
        base = mount_config.nextcloud_mount_path / "Users" / "alice"
        notes_dir = base / "notes"
        notes_dir.mkdir(parents=True)
        (notes_dir / "old.md").write_text("old content")

        workspace_dir = base / "workspace"
        workspace_dir.mkdir(parents=True)
        (workspace_dir / "new.md").write_text("new content")

        _migrate_notes_to_workspace(base)

        # workspace/ should be unchanged, notes/ should still exist
        assert notes_dir.is_dir()
        assert (workspace_dir / "new.md").read_text() == "new content"
        assert not (workspace_dir / "old.md").exists()


class TestRcloneOperations:
    """Tests for rclone-based storage functions with mocked subprocess.run."""

    def _mock_run(self, returncode=0, stdout="", stderr=""):
        mock = MagicMock()
        mock.returncode = returncode
        mock.stdout = stdout
        mock.stderr = stderr
        return mock

    @patch("istota.storage.subprocess.run")
    def test_rclone_mkdir(self, mock_run):
        mock_run.return_value = self._mock_run(returncode=0)
        assert _rclone_mkdir("nc", "/Users/alice/inbox") is True
        mock_run.assert_called_once_with(
            ["rclone", "mkdir", "nc:/Users/alice/inbox"],
            capture_output=True,
            text=True,
        )

    @patch("istota.storage.subprocess.run")
    def test_rclone_path_exists_true(self, mock_run):
        mock_run.return_value = self._mock_run(returncode=0)
        assert _rclone_path_exists("nc", "/Users/alice/inbox") is True

    @patch("istota.storage.subprocess.run")
    def test_rclone_path_exists_false(self, mock_run):
        mock_run.return_value = self._mock_run(returncode=1)
        assert _rclone_path_exists("nc", "/Users/alice/inbox") is False

    @patch("istota.storage.subprocess.run")
    def test_rclone_cat_success(self, mock_run):
        mock_run.return_value = self._mock_run(returncode=0, stdout="file content here")
        result = _rclone_cat("nc", "/Users/alice/context/memory.md")
        assert result == "file content here"

    @patch("istota.storage.subprocess.run")
    def test_rclone_cat_failure(self, mock_run):
        mock_run.return_value = self._mock_run(returncode=1)
        result = _rclone_cat("nc", "/Users/alice/context/memory.md")
        assert result is None

    @patch("istota.storage.subprocess.run")
    def test_rclone_rcat_success(self, mock_run):
        mock_run.return_value = self._mock_run(returncode=0)
        assert _rclone_rcat("nc", "/Users/alice/context/memory.md", "content") is True
        mock_run.assert_called_once_with(
            ["rclone", "rcat", "nc:/Users/alice/context/memory.md"],
            input="content",
            capture_output=True,
            text=True,
        )

    @patch("istota.storage.subprocess.run")
    def test_rclone_rcat_failure(self, mock_run):
        mock_run.return_value = self._mock_run(returncode=1)
        assert _rclone_rcat("nc", "/path", "content") is False

    @patch("istota.storage.subprocess.run")
    def test_ensure_dirs_via_rclone(self, mock_run):
        """ensure_user_directories calls rclone mkdir for each subdir + istota/exports."""
        mock_run.return_value = self._mock_run(returncode=0)
        config = Config(nextcloud_mount_path=None, rclone_remote="nc")

        result = ensure_user_directories("nc", "alice", "istota")
        assert result is True
        # 5 top-level subdirs + 1 istota/exports = 6 mkdir calls
        assert mock_run.call_count == 6

    @patch("istota.storage.subprocess.run")
    def test_read_memory_via_rclone(self, mock_run):
        mock_run.return_value = self._mock_run(returncode=0, stdout="memory data")
        result = read_user_memory("nc", "alice", "istota")
        assert result == "memory data"

    @patch("istota.storage.subprocess.run")
    def test_upload_file_via_rclone(self, mock_run, tmp_path):
        mock_run.return_value = self._mock_run(returncode=0)

        src = tmp_path / "report.pdf"
        src.write_text("pdf content")

        result = upload_file_to_inbox("nc", "alice", src)
        assert result == "/Users/alice/inbox/report.pdf"
        mock_run.assert_called_once_with(
            ["rclone", "copyto", str(src), "nc:/Users/alice/inbox/report.pdf"],
            capture_output=True,
            text=True,
        )


class TestDatedMemories:
    """Tests for read_dated_memories function."""

    @pytest.fixture
    def mount_config(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        return Config(nextcloud_mount_path=mount)

    def _make_memories_dir(self, config, user_id="alice"):
        memories_dir = config.nextcloud_mount_path / "Users" / user_id / "memories"
        memories_dir.mkdir(parents=True)
        return memories_dir

    def test_memories_path_helper(self):
        assert get_user_memories_path("alice") == "/Users/alice/memories"

    def test_returns_none_when_no_dir(self, mount_config):
        result = read_dated_memories(mount_config, "alice")
        assert result is None

    def test_returns_none_when_no_dated_files(self, mount_config):
        memories_dir = self._make_memories_dir(mount_config)
        (memories_dir / "readme.md").write_text("not a dated file")
        result = read_dated_memories(mount_config, "alice")
        assert result is None

    def test_reads_recent_file(self, mount_config):
        memories_dir = self._make_memories_dir(mount_config)
        today = datetime.now().strftime("%Y-%m-%d")
        (memories_dir / f"{today}.md").write_text("- Today's memory")

        result = read_dated_memories(mount_config, "alice")
        assert result is not None
        assert "Today's memory" in result
        assert today in result

    def test_newest_first_order(self, mount_config):
        memories_dir = self._make_memories_dir(mount_config)
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        today = datetime.now().strftime("%Y-%m-%d")

        (memories_dir / f"{yesterday}.md").write_text("- Yesterday's memory")
        (memories_dir / f"{today}.md").write_text("- Today's memory")

        result = read_dated_memories(mount_config, "alice")
        today_pos = result.index(today)
        yesterday_pos = result.index(yesterday)
        assert today_pos < yesterday_pos

    def test_excludes_old_files(self, mount_config):
        memories_dir = self._make_memories_dir(mount_config)
        old_date = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
        (memories_dir / f"{old_date}.md").write_text("- Old memory")

        result = read_dated_memories(mount_config, "alice", max_days=7)
        assert result is None

    def test_respects_max_chars(self, mount_config):
        memories_dir = self._make_memories_dir(mount_config)
        today = datetime.now().strftime("%Y-%m-%d")
        (memories_dir / f"{today}.md").write_text("x" * 5000)

        result = read_dated_memories(mount_config, "alice", max_chars=200)
        assert result is not None
        assert len(result) <= 250  # some margin for header and truncation marker

    def test_skips_empty_files(self, mount_config):
        memories_dir = self._make_memories_dir(mount_config)
        today = datetime.now().strftime("%Y-%m-%d")
        (memories_dir / f"{today}.md").write_text("")
        yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
        (memories_dir / f"{yesterday}.md").write_text("- Real memory")

        result = read_dated_memories(mount_config, "alice")
        assert "Real memory" in result

    def test_ignores_non_dated_files(self, mount_config):
        memories_dir = self._make_memories_dir(mount_config)
        (memories_dir / "readme.md").write_text("not dated")
        (memories_dir / "notes.txt").write_text("random")
        today = datetime.now().strftime("%Y-%m-%d")
        (memories_dir / f"{today}.md").write_text("- Dated memory")

        result = read_dated_memories(mount_config, "alice")
        assert "not dated" not in result
        assert "random" not in result
        assert "Dated memory" in result

    def test_returns_none_without_mount(self):
        config = Config(nextcloud_mount_path=None)
        result = read_dated_memories(config, "alice")
        assert result is None


class TestChannelMemory:
    """Tests for channel memory storage functions."""

    @pytest.fixture
    def mount_config(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        return Config(nextcloud_mount_path=mount)

    def test_get_channel_base_path(self):
        assert get_channel_base_path("abc123") == "/Channels/abc123"

    def test_get_channel_memory_path(self):
        assert get_channel_memory_path("abc123") == "/Channels/abc123/CHANNEL.md"

    def test_get_channel_memories_path(self):
        assert get_channel_memories_path("abc123") == "/Channels/abc123/memories"

    def test_ensure_channel_directories(self, mount_config):
        result = ensure_channel_directories(mount_config, "room42")
        assert result is True
        memories_dir = mount_config.nextcloud_mount_path / "Channels" / "room42" / "memories"
        assert memories_dir.is_dir()

    def test_ensure_channel_directories_idempotent(self, mount_config):
        ensure_channel_directories(mount_config, "room42")
        result = ensure_channel_directories(mount_config, "room42")
        assert result is True

    def test_read_channel_memory_not_exists(self, mount_config):
        result = read_channel_memory(mount_config, "room42")
        assert result is None

    def test_read_channel_memory_exists(self, mount_config):
        ensure_channel_directories(mount_config, "room42")
        mem_path = mount_config.nextcloud_mount_path / "Channels" / "room42" / "CHANNEL.md"
        mem_path.write_text("- Project uses Python 3.12")
        result = read_channel_memory(mount_config, "room42")
        assert result == "- Project uses Python 3.12"

    def test_read_channel_memory_empty(self, mount_config):
        ensure_channel_directories(mount_config, "room42")
        mem_path = mount_config.nextcloud_mount_path / "Channels" / "room42" / "CHANNEL.md"
        mem_path.write_text("")
        result = read_channel_memory(mount_config, "room42")
        assert result is None

    def test_read_channel_memory_whitespace_only(self, mount_config):
        ensure_channel_directories(mount_config, "room42")
        mem_path = mount_config.nextcloud_mount_path / "Channels" / "room42" / "CHANNEL.md"
        mem_path.write_text("   \n  \n  ")
        result = read_channel_memory(mount_config, "room42")
        assert result is None

    def test_init_channel_memory(self, mount_config):
        result = init_channel_memory(mount_config, "room42")
        assert result is True
        mem_path = mount_config.nextcloud_mount_path / "Channels" / "room42" / "CHANNEL.md"
        assert mem_path.exists()
        assert mem_path.read_text() == CHANNEL_MEMORY_TEMPLATE
        assert "Channel Memory" in mem_path.read_text()

    def test_ensure_channel_directories_migrates_old_layout(self, mount_config):
        """Old context/memory.md is migrated to CHANNEL.md."""
        base = mount_config.nextcloud_mount_path / "Channels" / "room42"
        old_dir = base / "context"
        old_dir.mkdir(parents=True)
        (old_dir / "memory.md").write_text("- Old channel notes")

        ensure_channel_directories(mount_config, "room42")

        new_memory = base / "CHANNEL.md"
        assert new_memory.exists()
        assert new_memory.read_text() == "- Old channel notes"

    def test_ensure_channel_directories_migration_skips_existing(self, mount_config):
        """Migration does not overwrite existing CHANNEL.md."""
        base = mount_config.nextcloud_mount_path / "Channels" / "room42"
        old_dir = base / "context"
        old_dir.mkdir(parents=True)
        (old_dir / "memory.md").write_text("- Old content")

        base.mkdir(parents=True, exist_ok=True)
        (base / "CHANNEL.md").write_text("- New content")

        ensure_channel_directories(mount_config, "room42")

        assert (base / "CHANNEL.md").read_text() == "- New content"


class TestShareFolderWithUser:
    """Tests for share_folder_with_user OCS API function."""

    @pytest.fixture
    def nc_config(self, tmp_path):
        mount = tmp_path / "mount"
        mount.mkdir()
        return Config(
            nextcloud_mount_path=mount,
            nextcloud=NextcloudConfig(
                url="https://nc.example.com",
                username="istota",
                app_password="secret123",
            ),
        )

    def test_returns_false_without_nextcloud_config(self, tmp_path):
        config = Config(
            nextcloud_mount_path=tmp_path,
            nextcloud=NextcloudConfig(url="", username="", app_password=""),
        )
        result = share_folder_with_user(config, "/Users/alice/notes", "alice")
        assert result is False

    @patch("istota.nextcloud_client.httpx.get")
    @patch("istota.nextcloud_client.httpx.post")
    def test_creates_new_share(self, mock_post, mock_get, nc_config):
        # No existing shares
        mock_get_resp = MagicMock()
        mock_get_resp.json.return_value = {"ocs": {"data": []}}
        mock_get_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_get_resp

        # Share creation succeeds
        mock_post_resp = MagicMock()
        mock_post_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_post_resp

        result = share_folder_with_user(nc_config, "/Users/alice/notes", "alice")
        assert result is True

        # Verify POST was called with correct params
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs["data"]["path"] == "/Users/alice/notes"
        assert call_kwargs.kwargs["data"]["shareWith"] == "alice"
        assert call_kwargs.kwargs["data"]["shareType"] == 0
        assert call_kwargs.kwargs["data"]["permissions"] == 31

    @patch("istota.nextcloud_client.httpx.get")
    @patch("istota.nextcloud_client.httpx.post")
    def test_idempotent_already_shared(self, mock_post, mock_get, nc_config):
        # Existing share found
        mock_get_resp = MagicMock()
        mock_get_resp.json.return_value = {"ocs": {"data": [
            {"share_with": "alice", "share_type": 0, "id": 42},
        ]}}
        mock_get_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_get_resp

        result = share_folder_with_user(nc_config, "/Users/alice/notes", "alice")
        assert result is True

        # POST should NOT be called since share already exists
        mock_post.assert_not_called()

    @patch("istota.nextcloud_client.httpx.get")
    @patch("istota.nextcloud_client.httpx.post")
    def test_different_user_share_not_matching(self, mock_post, mock_get, nc_config):
        # Share exists but for different user
        mock_get_resp = MagicMock()
        mock_get_resp.json.return_value = {"ocs": {"data": [
            {"share_with": "bob", "share_type": 0, "id": 10},
        ]}}
        mock_get_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_get_resp

        mock_post_resp = MagicMock()
        mock_post_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_post_resp

        result = share_folder_with_user(nc_config, "/Users/alice/notes", "alice")
        assert result is True
        mock_post.assert_called_once()

    @patch("istota.nextcloud_client.httpx.get")
    @patch("istota.nextcloud_client.httpx.post")
    def test_post_failure_returns_false(self, mock_post, mock_get, nc_config):
        mock_get_resp = MagicMock()
        mock_get_resp.json.return_value = {"ocs": {"data": []}}
        mock_get_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_get_resp

        mock_post.side_effect = Exception("Connection refused")

        result = share_folder_with_user(nc_config, "/Users/alice/notes", "alice")
        assert result is False

    @patch("istota.nextcloud_client.httpx.get")
    @patch("istota.nextcloud_client.httpx.post")
    def test_get_failure_still_tries_post(self, mock_post, mock_get, nc_config):
        # GET fails (can't check existing shares)
        mock_get.side_effect = Exception("Timeout")

        # POST succeeds
        mock_post_resp = MagicMock()
        mock_post_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_post_resp

        result = share_folder_with_user(nc_config, "/Users/alice/notes", "alice")
        assert result is True
        mock_post.assert_called_once()

    @patch("istota.nextcloud_client.httpx.get")
    @patch("istota.nextcloud_client.httpx.post")
    def test_ensure_dirs_calls_share(self, mock_post, mock_get, nc_config):
        """ensure_user_directories_v2 auto-shares istota/ folder."""
        mock_get_resp = MagicMock()
        mock_get_resp.json.return_value = {"ocs": {"data": []}}
        mock_get_resp.raise_for_status = MagicMock()
        mock_get.return_value = mock_get_resp

        mock_post_resp = MagicMock()
        mock_post_resp.raise_for_status = MagicMock()
        mock_post.return_value = mock_post_resp

        ensure_user_directories_v2(nc_config, "alice")

        # Should have called share for istota path
        mock_post.assert_called_once()
        call_kwargs = mock_post.call_args
        assert call_kwargs.kwargs["data"]["path"] == "/Users/alice/istota"
        assert call_kwargs.kwargs["data"]["shareWith"] == "alice"


class TestMigrateOldLayout:
    """Tests for _migrate_old_layout (context/ → USER.md + memories/)."""

    def test_no_context_dir_is_noop(self, tmp_path):
        """No context/ directory → nothing happens."""
        _migrate_old_layout(tmp_path)
        assert not (tmp_path / "USER.md").exists()

    def test_migrates_memory_to_user_md(self, tmp_path):
        """context/memory.md → USER.md."""
        context = tmp_path / "context"
        context.mkdir()
        (context / "memory.md").write_text("# My memories\n- likes coffee\n")

        _migrate_old_layout(tmp_path)

        assert (tmp_path / "USER.md").exists()
        assert (tmp_path / "USER.md").read_text() == "# My memories\n- likes coffee\n"

    def test_skips_memory_if_user_md_exists(self, tmp_path):
        """Don't overwrite existing USER.md."""
        context = tmp_path / "context"
        context.mkdir()
        (context / "memory.md").write_text("old content")
        (tmp_path / "USER.md").write_text("new content")

        _migrate_old_layout(tmp_path)

        assert (tmp_path / "USER.md").read_text() == "new content"

    def test_migrates_dated_files_to_memories(self, tmp_path):
        """context/YYYY-MM-DD.md → memories/YYYY-MM-DD.md."""
        context = tmp_path / "context"
        context.mkdir()
        (context / "2026-01-28.md").write_text("day 1")
        (context / "2026-01-29.md").write_text("day 2")

        _migrate_old_layout(tmp_path)

        memories = tmp_path / "memories"
        assert (memories / "2026-01-28.md").read_text() == "day 1"
        assert (memories / "2026-01-29.md").read_text() == "day 2"

    def test_skips_existing_dated_files(self, tmp_path):
        """Don't overwrite existing dated files in memories/."""
        context = tmp_path / "context"
        context.mkdir()
        (context / "2026-01-28.md").write_text("old version")

        memories = tmp_path / "memories"
        memories.mkdir()
        (memories / "2026-01-28.md").write_text("already migrated")

        _migrate_old_layout(tmp_path)

        assert (memories / "2026-01-28.md").read_text() == "already migrated"

    def test_ignores_non_dated_files_in_context(self, tmp_path):
        """Non-dated .md files in context/ are not migrated to memories/."""
        context = tmp_path / "context"
        context.mkdir()
        (context / "memory.md").write_text("mem")
        (context / "notes.md").write_text("should stay")

        _migrate_old_layout(tmp_path)

        assert not (tmp_path / "memories" / "notes.md").exists()
        assert (tmp_path / "USER.md").exists()

    def test_full_migration(self, tmp_path):
        """Full migration: memory + dated files + non-dated ignored."""
        context = tmp_path / "context"
        context.mkdir()
        (context / "memory.md").write_text("persistent memory")
        (context / "2026-01-27.md").write_text("day notes")
        (context / "random.txt").write_text("ignore me")

        _migrate_old_layout(tmp_path)

        assert (tmp_path / "USER.md").read_text() == "persistent memory"
        assert (tmp_path / "memories" / "2026-01-27.md").read_text() == "day notes"
        assert not (tmp_path / "memories" / "random.txt").exists()
        # Original files preserved (copy, not move)
        assert (context / "memory.md").exists()


class TestMigrateWorkspaceFiles:
    """Tests for _migrate_workspace_files (USER.md/TASKS.md from root → workspace/)."""

    def test_moves_user_md_to_workspace(self, tmp_path):
        (tmp_path / "USER.md").write_text("my memory")
        _migrate_workspace_files(tmp_path)
        assert not (tmp_path / "USER.md").exists()
        assert (tmp_path / "workspace" / "USER.md").read_text() == "my memory"

    def test_moves_tasks_md_to_workspace(self, tmp_path):
        (tmp_path / "TASKS.md").write_text("- [ ] do thing")
        _migrate_workspace_files(tmp_path)
        assert not (tmp_path / "TASKS.md").exists()
        assert (tmp_path / "workspace" / "TASKS.md").read_text() == "- [ ] do thing"

    def test_moves_both_files(self, tmp_path):
        (tmp_path / "USER.md").write_text("memory")
        (tmp_path / "TASKS.md").write_text("tasks")
        _migrate_workspace_files(tmp_path)
        assert (tmp_path / "workspace" / "USER.md").read_text() == "memory"
        assert (tmp_path / "workspace" / "TASKS.md").read_text() == "tasks"

    def test_skips_if_destination_exists(self, tmp_path):
        (tmp_path / "USER.md").write_text("old")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "USER.md").write_text("new")
        _migrate_workspace_files(tmp_path)
        # Source still exists (not moved), destination unchanged
        assert (tmp_path / "USER.md").exists()
        assert (workspace / "USER.md").read_text() == "new"

    def test_noop_if_no_files(self, tmp_path):
        _migrate_workspace_files(tmp_path)
        assert (tmp_path / "workspace").is_dir()

    def test_runs_in_ensure_user_directories(self, tmp_path):
        """Migration runs as part of ensure_user_directories_v2."""
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(nextcloud_mount_path=mount)

        # Place files at old location (user root)
        base = mount / "Users" / "alice"
        base.mkdir(parents=True)
        (base / "USER.md").write_text("old memory")
        (base / "TASKS.md").write_text("old tasks")

        ensure_user_directories_v2(config, "alice")

        assert not (base / "USER.md").exists()
        assert not (base / "TASKS.md").exists()
        # Files go through workspace_files migration → workspace, then workspace→istota,
        # then config files move to istota/config/
        assert (base / "istota" / "config" / "USER.md").read_text() == "old memory"
        assert (base / "istota" / "config" / "TASKS.md").read_text() == "old tasks"


class TestMigrateWorkspaceToBotDir:
    """Tests for _migrate_workspace_to_bot_dir (workspace/ → istota/ + config/)."""

    def test_renames_workspace_to_bot_dir(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "draft.md").write_text("content")

        _migrate_workspace_to_bot_dir(tmp_path, "istota")

        assert not workspace.exists()
        assert (tmp_path / "istota" / "draft.md").read_text() == "content"

    def test_skips_if_bot_dir_exists(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "old.md").write_text("old")

        bot_dir = tmp_path / "istota"
        bot_dir.mkdir()
        (bot_dir / "new.md").write_text("new")

        _migrate_workspace_to_bot_dir(tmp_path, "istota")

        # istota/ unchanged, workspace/ still exists
        assert workspace.is_dir()
        assert (bot_dir / "new.md").read_text() == "new"

    def test_moves_config_files_to_config_subdir(self, tmp_path):
        bot_dir = tmp_path / "istota"
        bot_dir.mkdir()
        (bot_dir / "USER.md").write_text("memory")
        (bot_dir / "TASKS.md").write_text("tasks")
        (bot_dir / "BRIEFINGS.md").write_text("briefings")
        (bot_dir / "draft.md").write_text("stays here")

        _migrate_workspace_to_bot_dir(tmp_path, "istota")

        config_dir = bot_dir / "config"
        assert (config_dir / "USER.md").read_text() == "memory"
        assert (config_dir / "TASKS.md").read_text() == "tasks"
        assert (config_dir / "BRIEFINGS.md").read_text() == "briefings"
        # Non-config files stay at istota/ root
        assert (bot_dir / "draft.md").read_text() == "stays here"

    def test_does_not_overwrite_existing_config_files(self, tmp_path):
        bot_dir = tmp_path / "istota"
        config_dir = bot_dir / "config"
        config_dir.mkdir(parents=True)
        (bot_dir / "USER.md").write_text("old")
        (config_dir / "USER.md").write_text("already migrated")

        _migrate_workspace_to_bot_dir(tmp_path, "istota")

        assert (config_dir / "USER.md").read_text() == "already migrated"

    def test_full_workspace_to_bot_dir_migration(self, tmp_path):
        """Full migration via ensure_user_directories_v2."""
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(nextcloud_mount_path=mount)

        base = mount / "Users" / "alice"
        workspace = base / "workspace"
        workspace.mkdir(parents=True)
        (workspace / "USER.md").write_text("my memory")
        (workspace / "TASKS.md").write_text("my tasks")
        (workspace / "draft.md").write_text("my draft")

        ensure_user_directories_v2(config, "alice")

        assert not workspace.exists()
        bot_dir = base / "istota"
        assert (bot_dir / "config" / "USER.md").read_text() == "my memory"
        assert (bot_dir / "config" / "TASKS.md").read_text() == "my tasks"
        assert (bot_dir / "draft.md").read_text() == "my draft"

    def test_exports_migrated_to_bot_dir(self, tmp_path):
        """Old exports/ directory contents migrate to istota/exports/."""
        mount = tmp_path / "mount"
        mount.mkdir()
        config = Config(nextcloud_mount_path=mount)

        base = mount / "Users" / "alice"
        old_exports = base / "exports"
        old_exports.mkdir(parents=True)
        (old_exports / "report.pdf").write_text("pdf content")

        ensure_user_directories_v2(config, "alice")

        assert (base / "istota" / "exports" / "report.pdf").read_text() == "pdf content"
