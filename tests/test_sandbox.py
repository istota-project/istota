"""Tests for bubblewrap sandbox (build_bwrap_cmd)."""

import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, DeveloperConfig, SecurityConfig
from istota.executor import build_bwrap_cmd


@pytest.fixture
def sandbox_config(tmp_path):
    """Config with sandbox enabled and realistic directory structure."""
    mount = tmp_path / "mount"
    mount.mkdir()
    (mount / "Users" / "alice").mkdir(parents=True)
    (mount / "Channels" / "room123").mkdir(parents=True)

    db_file = tmp_path / "data" / "istota.db"
    db_file.parent.mkdir(parents=True)
    db_file.touch()

    return Config(
        db_path=db_file,
        temp_dir=tmp_path / "temp",
        nextcloud_mount_path=mount,
        skills_dir=tmp_path / "skills",
        security=SecurityConfig(
            mode="restricted",
            sandbox_enabled=True,
        ),
    )


@pytest.fixture
def make_sandbox_task():
    def _make(**overrides):
        defaults = {
            "id": 1,
            "prompt": "test",
            "user_id": "alice",
            "source_type": "talk",
            "status": "running",
            "conversation_token": "room123",
        }
        defaults.update(overrides)
        return db.Task(**defaults)
    return _make


def _patch_linux():
    """Patch sys.platform to linux and shutil.which to find bwrap."""
    return (
        patch.object(sys, "platform", "linux"),
        patch.object(shutil, "which", return_value="/usr/bin/bwrap"),
    )


def _run_bwrap(config, task, is_admin, resources=None, user_temp=None):
    """Helper to call build_bwrap_cmd with Linux patches applied."""
    if user_temp is None:
        user_temp = config.temp_dir / task.user_id
        user_temp.mkdir(parents=True, exist_ok=True)
    if resources is None:
        resources = []
    p1, p2 = _patch_linux()
    with p1, p2:
        return build_bwrap_cmd(
            ["claude", "-p", "test"],
            config, task, is_admin, resources, user_temp,
        )


def _get_bind_pairs(result, bind_type="--bind"):
    """Extract (src, dest) pairs for a given bind type from bwrap args."""
    pairs = []
    i = 0
    while i < len(result):
        if result[i] == bind_type and i + 2 < len(result):
            pairs.append((result[i + 1], result[i + 2]))
            i += 3
        else:
            i += 1
    return pairs


class TestBuildBwrapCmdDisabled:
    """Tests for cases where bwrap should not be applied."""

    def test_returns_cmd_unchanged_on_non_linux(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        cmd = ["claude", "-p", "test"]
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True)

        with patch.object(sys, "platform", "darwin"):
            result = build_bwrap_cmd(cmd, sandbox_config, task, False, [], user_temp)

        assert result == cmd

    def test_returns_cmd_unchanged_when_bwrap_missing(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        cmd = ["claude", "-p", "test"]
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True)

        with patch.object(sys, "platform", "linux"), \
             patch.object(shutil, "which", return_value=None):
            result = build_bwrap_cmd(cmd, sandbox_config, task, False, [], user_temp)

        assert result == cmd


class TestBuildBwrapCmdNonAdmin:
    """Tests for non-admin user sandbox."""

    def test_starts_with_bwrap(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, False)
        assert result[0] == "bwrap"

    def test_ends_with_original_cmd(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, False)
        assert result[-3:] == ["claude", "-p", "test"]

    def test_separator_before_cmd(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, False)
        sep_idx = result.index("--")
        assert result[sep_idx + 1:] == ["claude", "-p", "test"]

    def test_has_system_ro_binds(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, False)
        assert "--ro-bind" in result

    def test_has_pid_namespace(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, False)
        assert "--unshare-pid" in result
        assert "--proc" in result

    def test_has_die_with_parent(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, False)
        assert "--die-with-parent" in result

    def test_user_dir_mounted_rw(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, False)
        mount = sandbox_config.nextcloud_mount_path.resolve()
        user_dir = str(mount / "Users" / "alice")
        bind_pairs = _get_bind_pairs(result, "--bind")
        assert any(src == user_dir for src, _ in bind_pairs), \
            f"User dir {user_dir} not in bind pairs: {bind_pairs}"

    def test_channel_dir_mounted_rw(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, False)
        mount = sandbox_config.nextcloud_mount_path.resolve()
        channel_dir = str(mount / "Channels" / "room123")
        bind_pairs = _get_bind_pairs(result, "--bind")
        assert any(src == channel_dir for src, _ in bind_pairs)

    def test_no_channel_mount_without_token(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task(conversation_token=None)
        result = _run_bwrap(sandbox_config, task, False)
        mount = sandbox_config.nextcloud_mount_path.resolve()
        result_str = " ".join(result)
        assert str(mount / "Channels") not in result_str

    def test_db_not_visible(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, False)
        db_str = str(sandbox_config.db_path.resolve())
        assert db_str not in result

    def test_config_users_masked_with_tmpfs(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, False)
        assert "--tmpfs" in result

    def test_resource_extra_mount_ro(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        shared_path = sandbox_config.nextcloud_mount_path / "Shared" / "data.csv"
        shared_path.parent.mkdir(parents=True, exist_ok=True)
        shared_path.touch()
        resource = db.UserResource(
            id=1, user_id="alice", resource_type="shared_file",
            resource_path="/Shared/data.csv", display_name="data",
            permissions="read",
        )
        result = _run_bwrap(sandbox_config, task, False, resources=[resource])
        resolved = str(shared_path.resolve())
        ro_pairs = _get_bind_pairs(result, "--ro-bind")
        assert any(src == resolved for src, _ in ro_pairs)

    def test_resource_extra_mount_rw(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        shared_path = sandbox_config.nextcloud_mount_path / "Shared" / "data.csv"
        shared_path.parent.mkdir(parents=True, exist_ok=True)
        shared_path.touch()
        resource = db.UserResource(
            id=1, user_id="alice", resource_type="shared_file",
            resource_path="/Shared/data.csv", display_name="data",
            permissions="readwrite",
        )
        result = _run_bwrap(sandbox_config, task, False, resources=[resource])
        resolved = str(shared_path.resolve())
        bind_pairs = _get_bind_pairs(result, "--bind")
        assert any(src == resolved for src, _ in bind_pairs)

    def test_resource_inside_user_dir_not_duplicated(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        resource = db.UserResource(
            id=1, user_id="alice", resource_type="todo_file",
            resource_path="/Users/alice/tasks.md", display_name="Tasks",
            permissions="read",
        )
        f = sandbox_config.nextcloud_mount_path / "Users" / "alice" / "tasks.md"
        f.touch()
        result = _run_bwrap(sandbox_config, task, False, resources=[resource])
        resolved = str(f.resolve())
        ro_pairs = _get_bind_pairs(result, "--ro-bind")
        assert not any(src == resolved for src, _ in ro_pairs)


class TestBuildBwrapCmdAdmin:
    """Tests for admin user sandbox."""

    def test_full_mount_rw(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, True)
        mount = str(sandbox_config.nextcloud_mount_path.resolve())
        bind_pairs = _get_bind_pairs(result, "--bind")
        assert any(src == mount for src, _ in bind_pairs)

    def test_db_ro_by_default(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, True)
        db_str = str(sandbox_config.db_path.resolve())
        ro_pairs = _get_bind_pairs(result, "--ro-bind")
        assert any(src == db_str for src, _ in ro_pairs)

    def test_db_rw_when_configured(self, sandbox_config, make_sandbox_task):
        sandbox_config.security.sandbox_admin_db_write = True
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, True)
        db_str = str(sandbox_config.db_path.resolve())
        bind_pairs = _get_bind_pairs(result, "--bind")
        assert any(src == db_str for src, _ in bind_pairs)

    def test_developer_repos_mounted(self, sandbox_config, make_sandbox_task, tmp_path):
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        sandbox_config.developer = DeveloperConfig(
            enabled=True,
            repos_dir=str(repos_dir),
        )
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, True)
        repos_str = str(repos_dir.resolve())
        bind_pairs = _get_bind_pairs(result, "--bind")
        assert any(src == repos_str for src, _ in bind_pairs)

    def test_no_repos_when_developer_disabled(self, sandbox_config, make_sandbox_task, tmp_path):
        repos_dir = tmp_path / "repos"
        repos_dir.mkdir()
        sandbox_config.developer = DeveloperConfig(
            enabled=False,
            repos_dir=str(repos_dir),
        )
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, True)
        repos_str = str(repos_dir.resolve())
        assert repos_str not in result


class TestBuildBwrapCmdPathResolution:
    """Test that paths are resolved (no symlinks leak through)."""

    def test_all_bind_paths_are_absolute(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True)

        result = _run_bwrap(sandbox_config, task, False, user_temp=user_temp)

        i = 0
        while i < len(result):
            if result[i] in ("--bind", "--ro-bind") and i + 2 < len(result):
                src, dest = result[i + 1], result[i + 2]
                assert os.path.isabs(src), f"Non-absolute source path: {src}"
                assert os.path.isabs(dest), f"Non-absolute dest path: {dest}"
                i += 3
            else:
                i += 1


class TestSecurityConfigSandboxFields:
    """Test that sandbox config fields load correctly."""

    def test_defaults(self):
        sc = SecurityConfig()
        assert sc.sandbox_enabled is False
        assert sc.sandbox_admin_db_write is False

    def test_from_config_load(self, tmp_path):
        from istota.config import load_config
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[security]
mode = "restricted"
sandbox_enabled = true
sandbox_admin_db_write = true
""")
        config = load_config(config_file)
        assert config.security.sandbox_enabled is True
        assert config.security.sandbox_admin_db_write is True
