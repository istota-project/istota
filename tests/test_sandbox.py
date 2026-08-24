"""Tests for bubblewrap sandbox (build_bwrap_cmd)."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, DevboxConfig, DeveloperConfig, NetworkConfig, SecurityConfig
from istota.executor import (
    _build_network_allowlist,
    build_bwrap_cmd,
    custom_system_prompt_path,
    native_fs_confinement_active,
    native_fs_roots,
)


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
    """Patch _bwrap_available to return True (skips real subprocess probe)."""
    return patch("istota.executor._bwrap_available", return_value=True)


def _run_bwrap(config, task, is_admin, resources=None, user_temp=None):
    """Helper to call build_bwrap_cmd with Linux patches applied."""
    if user_temp is None:
        user_temp = config.temp_dir / task.user_id
        user_temp.mkdir(parents=True, exist_ok=True)
    if resources is None:
        resources = []
    with _patch_linux():
        return build_bwrap_cmd(
            ["claude", "-p", "test"],
            config, task, is_admin, resources, user_temp,
        )


def _tmpfs_masks(result):
    """The mask set: every `--tmpfs` emitted after the last bind.

    `build_bwrap_cmd` mounts `--tmpfs /tmp` early, beside `--proc` and `--dev`,
    and the masks last — after every bind, which is the ordering they depend
    on. So "after the last bind" is exactly the masks. Matters on Linux, where
    `tmp_path` is itself under `/tmp` and the namespace's own tmpfs would
    otherwise read as a mask.
    """
    binds = [i for i, a in enumerate(result) if a in ("--bind", "--ro-bind")]
    after = max(binds) if binds else -1
    return [
        result[i + 1] for i in range(after + 1, len(result) - 1)
        if result[i] == "--tmpfs"
    ]


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

        with patch("istota.executor._bwrap_available", return_value=False):
            result = build_bwrap_cmd(cmd, sandbox_config, task, False, [], user_temp)

        assert result == cmd

    def test_returns_cmd_unchanged_when_bwrap_missing(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        cmd = ["claude", "-p", "test"]
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True)

        with patch("istota.executor._bwrap_available", return_value=False):
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

    def test_etc_alternatives_bound_at_its_own_path(self, sandbox_config, make_sandbox_task):
        """The `/etc` binds are selective, and Debian's `/usr/bin` needs this one.

        `awk`, `cc`, `vi`, `editor`, `pager`, `which` and `nc` are all symlinks
        into `/etc/alternatives`. Binding `/usr` carries the links in; without
        their target directory every one of them is dangling inside the
        namespace, and the command fails with `No such file or directory` for a
        binary `ls` shows sitting right there.

        Skipped where the host has no such directory — a darwin developer box
        has nothing to assert. `tests/linux/test_sandbox_real.py` runs the
        binary inside the real namespace.
        """
        if not Path("/etc/alternatives").is_dir():
            pytest.skip("no /etc/alternatives on this host")

        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, False)
        ro_pairs = _get_bind_pairs(result, "--ro-bind")
        assert any(dest == "/etc/alternatives" for _, dest in ro_pairs), \
            f"/etc/alternatives not in --ro-bind pairs: {ro_pairs}"

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

    def test_user_dir_mounted_rw_not_full_mount(self, sandbox_config, make_sandbox_task):
        """Admin gets scoped user dir, not the full Nextcloud mount."""
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, True)
        mount = sandbox_config.nextcloud_mount_path.resolve()
        full_mount = str(mount)
        user_dir = str(mount / "Users" / "alice")
        bind_pairs = _get_bind_pairs(result, "--bind")
        assert any(src == user_dir for src, _ in bind_pairs), \
            f"Admin user dir not in bind pairs: {bind_pairs}"
        assert not any(src == full_mount for src, _ in bind_pairs), \
            "Full Nextcloud mount should not be exposed to admin"

    def test_admin_channel_dir_mounted_rw(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, True)
        mount = sandbox_config.nextcloud_mount_path.resolve()
        channel_dir = str(mount / "Channels" / "room123")
        bind_pairs = _get_bind_pairs(result, "--bind")
        assert any(src == channel_dir for src, _ in bind_pairs)

    def test_admin_resource_mount_ro(self, sandbox_config, make_sandbox_task):
        """Admin per-resource mounts work (previously only non-admin had them)."""
        task = make_sandbox_task()
        shared_path = sandbox_config.nextcloud_mount_path / "Shared" / "report.csv"
        shared_path.parent.mkdir(parents=True, exist_ok=True)
        shared_path.touch()
        resource = db.UserResource(
            id=1, user_id="alice", resource_type="shared_file",
            resource_path="/Shared/report.csv", display_name="report",
            permissions="read",
        )
        result = _run_bwrap(sandbox_config, task, True, resources=[resource])
        resolved = str(shared_path.resolve())
        ro_pairs = _get_bind_pairs(result, "--ro-bind")
        assert any(src == resolved for src, _ in ro_pairs)

    def test_admin_resource_mount_rw(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        shared_path = sandbox_config.nextcloud_mount_path / "Shared" / "data.csv"
        shared_path.parent.mkdir(parents=True, exist_ok=True)
        shared_path.touch()
        resource = db.UserResource(
            id=1, user_id="alice", resource_type="shared_file",
            resource_path="/Shared/data.csv", display_name="data",
            permissions="readwrite",
        )
        result = _run_bwrap(sandbox_config, task, True, resources=[resource])
        resolved = str(shared_path.resolve())
        bind_pairs = _get_bind_pairs(result, "--bind")
        assert any(src == resolved for src, _ in bind_pairs)

    def test_db_not_bound_for_admin(self, sandbox_config, make_sandbox_task):
        """Admins used to get an RO bind of the framework DB. They no longer do.

        Full coverage of the replacement invariant — including the masks and
        the module DBs — lives in tests/test_sandbox_db_isolation.py.
        """
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, True)
        db_str = str(sandbox_config.db_path.resolve())
        for bind_type in ("--bind", "--ro-bind", "--bind-try", "--ro-bind-try"):
            assert not any(
                src == db_str for src, _ in _get_bind_pairs(result, bind_type)
            ), f"{db_str} bound with {bind_type}"

    def test_db_sidecars_not_bound_for_admin(self, sandbox_config, make_sandbox_task):
        """The -wal/-shm binds went with it; they were the live-read path."""
        task = make_sandbox_task()
        joined = " ".join(_run_bwrap(sandbox_config, task, True))
        for suffix in ["-wal", "-shm"]:
            assert str(sandbox_config.db_path) + suffix not in joined

    def test_developer_repos_mounted(self, sandbox_config, make_sandbox_task, tmp_path):
        repos_dir = tmp_path / "repos"
        (repos_dir / "alice").mkdir(parents=True)
        sandbox_config.developer = DeveloperConfig(
            enabled=True,
            repos_dir=str(repos_dir),
        )
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, True)
        own = str((repos_dir / "alice").resolve())
        bind_pairs = _get_bind_pairs(result, "--bind")
        assert any(src == own for src, _ in bind_pairs)

    def test_no_repos_when_developer_disabled(self, sandbox_config, make_sandbox_task, tmp_path):
        repos_dir = tmp_path / "repos"
        (repos_dir / "alice").mkdir(parents=True)
        sandbox_config.developer = DeveloperConfig(
            enabled=False,
            repos_dir=str(repos_dir),
        )
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, True)
        assert str(repos_dir.resolve()) not in result
        assert str((repos_dir / "alice").resolve()) not in result


class TestBuildBwrapCmdCredentials:
    """Tests for Claude Code credential file mount."""

    def test_credentials_json_mounted_ro(self, sandbox_config, make_sandbox_task):
        """~/.claude/.credentials.json should be --ro-bind, not --bind."""
        task = make_sandbox_task()
        home = Path(os.environ.get("HOME", "/tmp"))
        claude_dir = home / ".claude"
        creds = claude_dir / ".credentials.json"

        # Only test if the file actually exists on this machine
        if not creds.exists():
            pytest.skip("No .credentials.json on this machine")

        result = _run_bwrap(sandbox_config, task, False)
        creds_str = str(creds.resolve())

        ro_pairs = _get_bind_pairs(result, "--ro-bind")
        rw_pairs = _get_bind_pairs(result, "--bind")

        assert any(src == creds_str for src, _ in ro_pairs), \
            ".credentials.json not in --ro-bind pairs"
        assert not any(src == creds_str for src, _ in rw_pairs), \
            ".credentials.json should not be in --bind (RW) pairs"


class TestBuildBwrapCmdDeveloperDir:
    """Tests for .developer/ directory read-only mount."""

    def test_developer_dir_mounted_ro_when_present(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True)
        dev_dir = user_temp / ".developer"
        dev_dir.mkdir()

        result = _run_bwrap(sandbox_config, task, False, user_temp=user_temp)
        resolved = str(dev_dir.resolve())
        ro_pairs = _get_bind_pairs(result, "--ro-bind")
        assert any(src == resolved for src, _ in ro_pairs), \
            f".developer/ not in --ro-bind pairs: {ro_pairs}"

    def test_developer_dir_after_user_temp_bind(self, sandbox_config, make_sandbox_task):
        """The --ro-bind for .developer/ must come after the --bind for user_temp."""
        task = make_sandbox_task()
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True)
        dev_dir = user_temp / ".developer"
        dev_dir.mkdir()

        result = _run_bwrap(sandbox_config, task, False, user_temp=user_temp)
        temp_resolved = str(user_temp.resolve())
        dev_resolved = str(dev_dir.resolve())

        # Find positions
        bind_idx = None
        ro_bind_idx = None
        for i, arg in enumerate(result):
            if arg == "--bind" and i + 1 < len(result) and result[i + 1] == temp_resolved:
                bind_idx = i
            if arg == "--ro-bind" and i + 1 < len(result) and result[i + 1] == dev_resolved:
                ro_bind_idx = i
        assert bind_idx is not None, "user_temp --bind not found"
        assert ro_bind_idx is not None, ".developer --ro-bind not found"
        assert ro_bind_idx > bind_idx, \
            f"--ro-bind for .developer/ ({ro_bind_idx}) should come after --bind for user_temp ({bind_idx})"

    def test_no_developer_dir_no_extra_bind(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True)
        # No .developer/ directory created

        result = _run_bwrap(sandbox_config, task, False, user_temp=user_temp)
        result_str = " ".join(result)
        assert ".developer" not in result_str


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


class TestCustomSystemPromptBind:
    """`--system-prompt-file` is the one config-dir file the CLI opens itself.

    Everything else in the config directory (emissaries, persona, guidelines,
    skill bodies) reaches the model as content the daemon read, so only this
    one needs to exist in the namespace. It used to arrive via the
    `sandbox_ro_paths = ["/srv/app"]` default; when that became `[]` every task
    on a custom_system_prompt install failed with "System prompt file not
    found". The file is bound, never its directory — config.toml is beside it.
    """

    def _write_prompt(self, config):
        path = config.skills_dir.parent / "system-prompt.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("custom prompt")
        return path

    def test_bound_ro_at_its_own_path(self, sandbox_config, make_sandbox_task):
        sandbox_config.custom_system_prompt = True
        sp = self._write_prompt(sandbox_config)

        result = _run_bwrap(sandbox_config, make_sandbox_task(), False)

        assert (str(sp.resolve()), str(sp)) in _get_bind_pairs(result, "--ro-bind")

    def test_not_bound_when_disabled(self, sandbox_config, make_sandbox_task):
        sandbox_config.custom_system_prompt = False
        sp = self._write_prompt(sandbox_config)

        result = _run_bwrap(sandbox_config, make_sandbox_task(), False)

        assert str(sp) not in result

    def test_config_dir_itself_not_bound(self, sandbox_config, make_sandbox_task):
        """config.toml is its neighbour and must stay out of the sandbox."""
        sandbox_config.custom_system_prompt = True
        sp = self._write_prompt(sandbox_config)
        (sp.parent / "config.toml").write_text("nc_pass = 'hunter2'")

        result = _run_bwrap(sandbox_config, make_sandbox_task(), False)

        config_dir = str(sp.parent.resolve())
        for src, dest in _get_bind_pairs(result, "--ro-bind") + _get_bind_pairs(result):
            assert src != config_dir and dest != config_dir

    def test_missing_file_binds_nothing(self, sandbox_config, make_sandbox_task):
        sandbox_config.custom_system_prompt = True
        expected = sandbox_config.skills_dir.parent / "system-prompt.md"

        result = _run_bwrap(sandbox_config, make_sandbox_task(), False)

        assert str(expected) not in result

    def test_relative_skills_dir_gives_an_absolute_path(self, sandbox_config, make_sandbox_task, monkeypatch, tmp_path):
        """A relative skills_dir must still produce an absolute bind + flag.

        The CLI runs with its own --chdir, so a relative path would not open.
        """
        monkeypatch.chdir(tmp_path)
        sandbox_config.custom_system_prompt = True
        sandbox_config.skills_dir = Path("config/skills")
        sp = tmp_path / "config" / "system-prompt.md"
        sp.parent.mkdir(parents=True, exist_ok=True)
        sp.write_text("custom prompt")

        resolved = custom_system_prompt_path(sandbox_config)
        assert resolved is not None and resolved.is_absolute()

        result = _run_bwrap(sandbox_config, make_sandbox_task(), False)
        assert str(resolved) in result

    def test_path_is_none_when_disabled(self, sandbox_config):
        sandbox_config.custom_system_prompt = False
        assert custom_system_prompt_path(sandbox_config) is None


class TestSecurityConfigSandboxFields:
    """Test that sandbox config fields load correctly."""

    def test_defaults(self):
        sc = SecurityConfig()
        assert sc.sandbox_enabled is True
        assert sc.skill_proxy_enabled is True
        assert sc.sandbox_ro_paths == []

    def test_from_config_load(self, tmp_path):
        from istota.config import load_config
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[security]
mode = "restricted"
sandbox_enabled = true
sandbox_ro_paths = ["/opt/some-service"]
""")
        config = load_config(config_file)
        assert config.security.sandbox_enabled is True
        assert config.security.sandbox_ro_paths == ["/opt/some-service"]


class TestNetworkProxyBwrapIntegration:
    """Tests for --unshare-net and shell wrapper in bwrap command."""

    def test_unshare_net_added(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        user_temp = sandbox_config.temp_dir / task.user_id
        user_temp.mkdir(parents=True)
        sock = sandbox_config.temp_dir / "net.sock"
        sock.touch()
        with _patch_linux():
            result = build_bwrap_cmd(
                ["claude", "-p", "-"], sandbox_config, task, False,
                [], user_temp, net_proxy_sock=sock,
            )
        assert "--unshare-net" in result

    def test_no_unshare_net_without_sock(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, False)
        assert "--unshare-net" not in result

    def test_proxy_socket_bind_mounted(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        user_temp = sandbox_config.temp_dir / task.user_id
        user_temp.mkdir(parents=True)
        sock = sandbox_config.temp_dir / "net.sock"
        sock.touch()
        with _patch_linux():
            result = build_bwrap_cmd(
                ["claude", "-p", "-"], sandbox_config, task, False,
                [], user_temp, net_proxy_sock=sock,
            )
        ro_pairs = _get_bind_pairs(result, "--ro-bind")
        assert any(src == str(sock.resolve()) for src, _ in ro_pairs)

    def test_shell_wrapper_present(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        user_temp = sandbox_config.temp_dir / task.user_id
        user_temp.mkdir(parents=True)
        sock = sandbox_config.temp_dir / "net.sock"
        sock.touch()
        with _patch_linux():
            result = build_bwrap_cmd(
                ["claude", "-p", "-"], sandbox_config, task, False,
                [], user_temp, net_proxy_sock=sock,
            )
        sep_idx = result.index("--")
        after_sep = result[sep_idx + 1:]
        # Should start with /bin/sh -c
        assert after_sep[0] == "/bin/sh"
        assert after_sep[1] == "-c"
        # Shell script should reference HTTPS_PROXY
        shell_cmd = after_sep[2]
        assert "HTTPS_PROXY=" in shell_cmd
        assert "HTTP_PROXY=" in shell_cmd
        assert "NO_PROXY=" in shell_cmd
        assert "net-bridge" in shell_cmd
        # The backgrounded bridge must not share the prompt pipe on stdin.
        assert "net-bridge" in shell_cmd and "</dev/null &" in shell_cmd
        # No blind `sleep` gating claude's start — it eats stdin-deadline margin
        # for no benefit (the bridge binds before any network call is made).
        assert "sleep" not in shell_cmd
        # Original cmd should follow as positional args
        assert "claude" in after_sep
        assert "-p" in after_sep

    def test_original_cmd_preserved_in_wrapper(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        user_temp = sandbox_config.temp_dir / task.user_id
        user_temp.mkdir(parents=True)
        sock = sandbox_config.temp_dir / "net.sock"
        sock.touch()
        with _patch_linux():
            result = build_bwrap_cmd(
                ["claude", "-p", "-", "--allowedTools", "Read"],
                sandbox_config, task, False, [], user_temp,
                net_proxy_sock=sock,
            )
        sep_idx = result.index("--")
        after_sep = result[sep_idx + 1:]
        # "sh" is $0, then the original cmd follows
        assert after_sep[3] == "sh"
        assert after_sep[4:] == ["claude", "-p", "-", "--allowedTools", "Read"]


class TestDevboxDockerProxyBind:
    """The Docker-API proxy socket is bound at the conventional docker path,
    unconditionally (devbox enabled + proxy enabled + socket present), and the
    raw docker socket is never bound."""

    def _devbox_config(self, base: Config, sock_dir: Path, *, api_proxy_enabled=True):
        cli = sock_dir / "docker"
        cli.touch()
        base.devbox = DevboxConfig(
            enabled=True,
            api_proxy_enabled=api_proxy_enabled,
            api_proxy_socket_dir=str(sock_dir),
            docker_cli=str(cli),
            docker_socket="/var/run/docker.sock",
        )
        return base

    def test_proxy_socket_bound_at_conventional_path(self, sandbox_config, make_sandbox_task, tmp_path):
        sock_dir = tmp_path / "dockproxy"
        sock_dir.mkdir()
        (sock_dir / "alice.sock").touch()
        config = self._devbox_config(sandbox_config, sock_dir)
        task = make_sandbox_task(user_id="alice")
        result = _run_bwrap(config, task, False)
        bind_pairs = _get_bind_pairs(result, "--bind")
        proxy_src = str((sock_dir / "alice.sock").resolve())
        assert any(
            src == proxy_src and dest == "/var/run/docker.sock"
            for src, dest in bind_pairs
        ), bind_pairs

    def test_bind_not_gated_on_selection(self, sandbox_config, make_sandbox_task, tmp_path):
        sock_dir = tmp_path / "dockproxy"
        sock_dir.mkdir()
        (sock_dir / "alice.sock").touch()
        config = self._devbox_config(sandbox_config, sock_dir)
        task = make_sandbox_task(user_id="alice")
        # No selected_skills passed at all — bind must still happen.
        result = _run_bwrap(config, task, False)
        proxy_src = str((sock_dir / "alice.sock").resolve())
        assert any(a == proxy_src for a in result)

    def test_raw_docker_socket_never_bound(self, sandbox_config, make_sandbox_task, tmp_path):
        sock_dir = tmp_path / "dockproxy"
        sock_dir.mkdir()
        (sock_dir / "alice.sock").touch()
        config = self._devbox_config(sandbox_config, sock_dir)
        task = make_sandbox_task(user_id="alice")
        result = _run_bwrap(config, task, False)
        # The raw /var/run/docker.sock is never a bind *source*.
        bind_pairs = _get_bind_pairs(result, "--bind")
        assert not any(src == "/var/run/docker.sock" for src, _ in bind_pairs)

    def test_per_user_socket_path(self, sandbox_config, make_sandbox_task, tmp_path):
        sock_dir = tmp_path / "dockproxy"
        sock_dir.mkdir()
        (sock_dir / "bob.sock").touch()  # only bob's socket exists
        config = self._devbox_config(sandbox_config, sock_dir)
        # alice has no socket -> no bind
        alice = make_sandbox_task(user_id="alice")
        (config.nextcloud_mount_path / "Users" / "bob").mkdir(parents=True, exist_ok=True)
        result_alice = _run_bwrap(config, alice, False)
        assert not any(str(sock_dir / "alice.sock") in a for a in result_alice)
        # bob's socket exists -> bound
        bob = make_sandbox_task(user_id="bob")
        result_bob = _run_bwrap(config, bob, False)
        bob_src = str((sock_dir / "bob.sock").resolve())
        assert any(a == bob_src for a in result_bob)

    def test_no_bind_when_api_proxy_disabled(self, sandbox_config, make_sandbox_task, tmp_path):
        sock_dir = tmp_path / "dockproxy"
        sock_dir.mkdir()
        (sock_dir / "alice.sock").touch()
        config = self._devbox_config(sandbox_config, sock_dir, api_proxy_enabled=False)
        task = make_sandbox_task(user_id="alice")
        result = _run_bwrap(config, task, False)
        assert not any(str(sock_dir / "alice.sock") in a for a in result)


class TestBuildNetworkAllowlist:
    """Tests for _build_network_allowlist."""

    def _make_config(self, **overrides):
        net_kw = {}
        for k in ("enabled", "allow_pypi", "extra_hosts"):
            if k in overrides:
                net_kw[k] = overrides.pop(k)
        network = NetworkConfig(**net_kw) if net_kw else NetworkConfig()
        return Config(security=SecurityConfig(network=network), **overrides)

    def test_default_hosts_always_present(self):
        config = self._make_config()
        hosts = _build_network_allowlist(config, [])
        assert "api.anthropic.com:443" in hosts
        assert "mcp-proxy.anthropic.com:443" in hosts

    def test_pypi_hosts_when_allowed(self):
        config = self._make_config(allow_pypi=True)
        hosts = _build_network_allowlist(config, [])
        assert "pypi.org:443" in hosts
        assert "files.pythonhosted.org:443" in hosts

    def test_no_pypi_hosts_when_disabled(self):
        config = self._make_config(allow_pypi=False)
        hosts = _build_network_allowlist(config, [])
        assert "pypi.org:443" not in hosts
        assert "files.pythonhosted.org:443" not in hosts

    def test_extra_hosts_included(self):
        config = self._make_config(extra_hosts=["registry.example.com:443"])
        hosts = _build_network_allowlist(config, [])
        assert "registry.example.com:443" in hosts

    def test_developer_gitlab_host(self):
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            developer=DeveloperConfig(
                enabled=True,
                repos_dir="/tmp/repos",
                gitlab_url="https://gitlab.example.com",
            ),
        )
        hosts = _build_network_allowlist(config, ["developer"])
        assert "gitlab.example.com:443" in hosts

    def test_developer_github_host(self):
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            developer=DeveloperConfig(
                enabled=True,
                repos_dir="/tmp/repos",
                github_url="https://github.com",
            ),
        )
        hosts = _build_network_allowlist(config, ["developer"])
        assert "github.com:443" in hosts
        assert "api.github.com:443" in hosts

    def test_developer_actions_log_host(self):
        """`gh run view --log-failed` fetches job logs from a second host.

        Measured through a logging CONNECT proxy against gh 2.98: one stable
        hostname, identical across independent runs, so an exact entry works
        and NetworkProxy needs no wildcard matching."""
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            developer=DeveloperConfig(
                enabled=True,
                repos_dir="/tmp/repos",
                github_url="https://github.com",
            ),
        )
        hosts = _build_network_allowlist(config, ["developer"])
        assert "results-receiver.actions.githubusercontent.com:443" in hosts

    def test_azure_blob_storage_is_not_allowlisted(self):
        """`gh run download` pulls artifacts from
        productionresultssa<N>.blob.core.windows.net, where the shard varies
        (4 and 7 observed for one repository). The only entry that would cover
        it is *.blob.core.windows.net — all of Azure Blob Storage, which is a
        general-purpose exfiltration channel. Artifacts are not worth that, so
        the verb stays unadvertised rather than the allowlist widened."""
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            developer=DeveloperConfig(
                enabled=True,
                repos_dir="/tmp/repos",
                github_url="https://github.com",
            ),
        )
        hosts = _build_network_allowlist(config, ["developer"])
        assert not any("blob.core.windows.net" in h for h in hosts)

    def test_actions_log_host_not_added_for_enterprise_server(self):
        """The log host is a github.com service. A GHE Server deployment serves
        its own, so adding this one there would be noise, not access."""
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            developer=DeveloperConfig(
                enabled=True,
                repos_dir="/tmp/repos",
                github_url="https://ghe.example.com",
            ),
        )
        hosts = _build_network_allowlist(config, ["developer"])
        assert "ghe.example.com:443" in hosts
        assert "results-receiver.actions.githubusercontent.com:443" not in hosts

    def test_developer_hosts_only_when_skill_selected(self):
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            developer=DeveloperConfig(
                enabled=True,
                repos_dir="/tmp/repos",
                gitlab_url="https://gitlab.example.com",
            ),
        )
        hosts = _build_network_allowlist(config, ["calendar"])
        assert "gitlab.example.com:443" not in hosts

    def test_developer_custom_port(self):
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            developer=DeveloperConfig(
                enabled=True,
                repos_dir="/tmp/repos",
                gitlab_url="https://gitlab.example.com:8443",
            ),
        )
        hosts = _build_network_allowlist(config, ["developer"])
        assert "gitlab.example.com:8443" in hosts

    def test_developer_npm_registry_host(self):
        """A complete `npm ci` of this repo's web/package-lock.json (213
        packages) made 15 CONNECTs through a logging proxy, every one of them
        to registry.npmjs.org. Metadata and tarballs share the host, so one
        entry is the whole of npm's reach."""
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            developer=DeveloperConfig(enabled=True, repos_dir="/tmp/repos"),
        )
        hosts = _build_network_allowlist(config, ["developer"])
        assert "registry.npmjs.org:443" in hosts

    def test_developer_cargo_registry_hosts(self):
        """`cargo fetch` on serde and its transitive dependencies contacted the
        sparse index and the download host, and nothing else."""
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            developer=DeveloperConfig(enabled=True, repos_dir="/tmp/repos"),
        )
        hosts = _build_network_allowlist(config, ["developer"])
        assert "index.crates.io:443" in hosts
        assert "static.crates.io:443" in hosts

    def test_crates_io_itself_is_not_allowlisted(self):
        """`crates.io` is the API host — publish, search and yank. The measured
        fetch never contacted it, and building does not need it, so it stays
        out rather than arriving as a guess alongside the two that do.

        Asserted together with the two hosts that *are* expected, so a revert
        turns this red. On its own the negative holds against an unmodified
        tree and could only ever fail if someone later added the host."""
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            developer=DeveloperConfig(enabled=True, repos_dir="/tmp/repos"),
        )
        hosts = _build_network_allowlist(config, ["developer"])
        assert "index.crates.io:443" in hosts
        assert "static.crates.io:443" in hosts
        assert "crates.io:443" not in hosts

    def test_registry_hosts_absent_when_developer_not_authorized(self):
        """The registries are gated on `developer` being in `authorized_skills`,
        which is why no separate `allow_npm` flag exists."""
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            developer=DeveloperConfig(enabled=True, repos_dir="/tmp/repos"),
        )
        hosts = _build_network_allowlist(config, ["calendar"])
        assert "registry.npmjs.org:443" not in hosts
        assert "index.crates.io:443" not in hosts
        assert "static.crates.io:443" not in hosts

    def test_registry_hosts_ride_authorization_not_selection(self):
        """`authorized_skills` is not the selected set. `derive_authorized_skills`
        adds any skill whose credentials resolve, and `developer` auto-authorizes
        as soon as either forge token is configured — so on such a deployment
        these hosts are present for *every* task of that user, including one that
        never chose the skill. That is the same gate the forge hosts already ride
        and it is deliberate, but it is a wider reach than "the developer skill
        was selected" and the test says so rather than leaving it to be
        rediscovered."""
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            developer=DeveloperConfig(
                enabled=True, repos_dir="/tmp/repos",
                gitlab_url="https://gitlab.example.com",
            ),
        )
        # What execute_task passes when the user has a forge token but the task
        # selected something else entirely.
        hosts = _build_network_allowlist(config, ["developer", "browse"])
        assert "registry.npmjs.org:443" in hosts
        assert "gitlab.example.com:443" in hosts

    def test_registry_hosts_absent_when_developer_disabled(self):
        """`developer.enabled` is the operator's switch, and it gates the
        registries the same way it gates the forge hosts."""
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            developer=DeveloperConfig(enabled=False, repos_dir="/tmp/repos"),
        )
        hosts = _build_network_allowlist(config, ["developer"])
        assert "registry.npmjs.org:443" not in hosts

    def test_registries_do_not_depend_on_a_configured_forge(self):
        """The forge URLs and the registries are independent: a deployment with
        no gitlab_url or github_url still installs dependencies."""
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            developer=DeveloperConfig(
                enabled=True, repos_dir="/tmp/repos",
                gitlab_url="", github_url="",
            ),
        )
        hosts = _build_network_allowlist(config, ["developer"])
        assert "registry.npmjs.org:443" in hosts


class TestNetworkConfigParsing:
    def test_defaults(self):
        nc = NetworkConfig()
        assert nc.enabled is True
        assert nc.allow_pypi is True
        assert nc.extra_hosts == []

    def test_security_config_includes_network(self):
        sc = SecurityConfig()
        assert isinstance(sc.network, NetworkConfig)
        assert sc.network.enabled is True

    def test_from_config_load(self, tmp_path):
        from istota.config import load_config
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[security]
sandbox_enabled = true

[security.network]
enabled = false
allow_pypi = false
extra_hosts = ["custom.example.com:443"]
""")
        config = load_config(config_file)
        assert config.security.network.enabled is False
        assert config.security.network.allow_pypi is False
        assert config.security.network.extra_hosts == ["custom.example.com:443"]

    def test_defaults_when_network_section_missing(self, tmp_path):
        from istota.config import load_config
        config_file = tmp_path / "config.toml"
        config_file.write_text("""
[security]
sandbox_enabled = true
""")
        config = load_config(config_file)
        assert config.security.network.enabled is True
        assert config.security.network.allow_pypi is True


class TestNativeFsRoots:
    """NB-1: NativeBrain's in-process file tools confine to the same user-data
    roots that build_bwrap_cmd would bind for the claude_code path."""

    def test_confinement_active_only_with_bwrap(self, sandbox_config):
        with patch("istota.executor._bwrap_available", return_value=True):
            assert native_fs_confinement_active(sandbox_config) is True
        with patch("istota.executor._bwrap_available", return_value=False):
            assert native_fs_confinement_active(sandbox_config) is False

    def test_confinement_inactive_when_sandbox_disabled(self, sandbox_config):
        sandbox_config.security.sandbox_enabled = False
        with patch("istota.executor._bwrap_available", return_value=True):
            assert native_fs_confinement_active(sandbox_config) is False

    def _roots(self, config, task, is_admin, resources=None, user_temp=None):
        if user_temp is None:
            user_temp = config.temp_dir / task.user_id
            user_temp.mkdir(parents=True, exist_ok=True)
        return native_fs_roots(config, task, is_admin, resources or [], user_temp)

    def test_user_temp_dir_is_writable(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True)
        read, write, _ = self._roots(sandbox_config, task, False, user_temp=user_temp)
        assert user_temp.resolve() in write
        assert user_temp.resolve() in read

    def test_user_mount_and_channel_writable(self, sandbox_config, make_sandbox_task):
        task = make_sandbox_task()
        _, write, _ = self._roots(sandbox_config, task, False)
        mount = sandbox_config.nextcloud_mount_path.resolve()
        assert (mount / "Users" / "alice").resolve() in write
        assert (mount / "Channels" / "room123").resolve() in write

    def test_talk_is_read_only(self, sandbox_config, make_sandbox_task):
        (sandbox_config.nextcloud_mount_path / "Talk").mkdir()
        task = make_sandbox_task()
        read, write, _ = self._roots(sandbox_config, task, False)
        talk = (sandbox_config.nextcloud_mount_path / "Talk").resolve()
        assert talk in read
        assert talk not in write

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_db_absent_for_everyone(self, sandbox_config, make_sandbox_task, is_admin):
        """Admins lost the RO read root along with the bwrap bind."""
        task = make_sandbox_task()
        read, write, _ = self._roots(sandbox_config, task, is_admin)
        db_path = sandbox_config.db_path.resolve()
        assert db_path not in read
        assert db_path not in write

    def test_temp_dir_parent_not_a_root(self, sandbox_config, make_sandbox_task):
        # The shared temp_dir parent must NOT be a root — only the per-user dir.
        task = make_sandbox_task()
        read, _, _ = self._roots(sandbox_config, task, False)
        assert sandbox_config.temp_dir.resolve() not in read

    def test_developer_dir_denied_for_writes(self, sandbox_config, make_sandbox_task):
        """build_bwrap_cmd re-binds .developer read-only after binding its
        parent read-write, so the model can't replace credential-fetch or the
        git credential helpers. The native file tools must carve out the same
        hole, or the claim in this function's docstring is false."""
        task = make_sandbox_task()
        user_temp = sandbox_config.temp_dir / "alice"
        dev_dir = user_temp / ".developer"
        dev_dir.mkdir(parents=True)
        read, write, denied = self._roots(
            sandbox_config, task, False, user_temp=user_temp,
        )
        assert dev_dir.resolve() in denied
        assert user_temp.resolve() in write   # the parent stays writable
        assert dev_dir.resolve() not in write
        assert user_temp.resolve() in read    # and .developer stays readable

    def test_developer_dir_denied_before_it_exists(
        self, sandbox_config, make_sandbox_task,
    ):
        """The deny list is built once per task; build_bwrap_cmd re-checks on
        every Bash call. Gating on existence here would leave a .developer
        created mid-run writable for the file tools and read-only for Bash."""
        task = make_sandbox_task()
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True)
        _, _, denied = self._roots(sandbox_config, task, False, user_temp=user_temp)
        assert (user_temp.resolve() / ".developer") in denied


class TestPerUserReposDir:
    """`developer.repos_dir` is a root of per-user subtrees, not one tree.

    Every admin developer task used to get the whole of `repos_dir` bound
    read-write, so one admin's task could read and write another admin's
    clones, worktrees, model-written git configs and package caches. ISSUE-319
    was one instance of that — the shared package-cache root, closed with about
    200 lines of sibling masks — and this is the class it came from. Binding
    `{repos_dir}/{user_id}` closes it structurally: another user's tree is not
    in the namespace at all, so there is no mask to emit and no argv ordering
    to preserve.
    """

    def _setup(self, sandbox_config, tmp_path, users=("alice", "bob")):
        repos = tmp_path / "repos"
        for user in users:
            # A clone in the documented layout, {user}/{namespace}/{project}.git
            (repos / user / "acme" / "widget.git").mkdir(parents=True)
        sandbox_config.developer = DeveloperConfig(
            enabled=True, repos_dir=str(repos),
        )
        return repos

    def test_the_bind_is_the_tasks_own_subtree_and_never_the_root(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        repos = self._setup(sandbox_config, tmp_path)
        result = _run_bwrap(sandbox_config, make_sandbox_task(), True)

        pairs = _get_bind_pairs(result, "--bind")
        own = str((repos / "alice").resolve())
        assert any(src == own for src, _ in pairs), f"alice's subtree is not bound: {pairs}"
        assert not any(src == str(repos.resolve()) for src, _ in pairs), \
            "the shared repos root was bound, which is the exposure itself"

    def test_another_users_subtree_is_not_in_the_namespace_at_all(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """The regression, stated as reachability rather than as a name.

        Asserting that bob's path does not appear in the argv is the version of
        this test that cannot fail: binding the shared root — the exposure
        itself — exposes bob's whole tree while naming only `{repos_dir}`. So
        what is asked of every bind is whether bob's subtree is *under* its
        source, which is the question a `mount` inside the namespace would
        answer.
        """
        repos = self._setup(sandbox_config, tmp_path)
        result = _run_bwrap(sandbox_config, make_sandbox_task(), True)

        bob = (repos / "bob").resolve()
        exposed = []
        for verb in ("--bind", "--bind-try", "--ro-bind", "--ro-bind-try", "--dev-bind"):
            for src, _dest in _get_bind_pairs(result, verb):
                if bob == Path(src) or bob.is_relative_to(Path(src)):
                    exposed.append((verb, src))
        assert exposed == [], f"bob's tree is reachable from alice's sandbox: {exposed}"

    def test_two_users_get_two_different_subtrees(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        repos = self._setup(sandbox_config, tmp_path)
        (sandbox_config.nextcloud_mount_path / "Users" / "bob").mkdir(parents=True)

        alice = _run_bwrap(sandbox_config, make_sandbox_task(), True)
        bob = _run_bwrap(sandbox_config, make_sandbox_task(user_id="bob"), True)

        def _repos_binds(argv):
            root = str(repos.resolve())
            return [
                src for src, _ in _get_bind_pairs(argv, "--bind")
                if src.startswith(root + os.sep)
            ]

        assert _repos_binds(alice) == [str((repos / "alice").resolve())]
        assert _repos_binds(bob) == [str((repos / "bob").resolve())]

    def test_a_non_admin_gets_no_repos_bind(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """Unchanged: the admin gate sits in front of the per-user split, not
        behind it."""
        repos = self._setup(sandbox_config, tmp_path)
        result = _run_bwrap(sandbox_config, make_sandbox_task(), False)
        assert str((repos / "alice").resolve()) not in result
        assert str(repos.resolve()) not in result

    def test_a_user_with_no_subtree_yet_binds_nothing(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """`_bind` skips a path that does not exist. The root must not stand in
        for it — that is the whole change, undone by a fallback."""
        repos = tmp_path / "repos"
        repos.mkdir()
        sandbox_config.developer = DeveloperConfig(enabled=True, repos_dir=str(repos))

        result = _run_bwrap(sandbox_config, make_sandbox_task(), True)
        assert str(repos.resolve()) not in result

    def test_an_empty_user_id_binds_nothing(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """`{repos_dir}/""` is `{repos_dir}`, so a fallback here would hand the
        shared root to a task whose user id went missing. Fail closed."""
        repos = self._setup(sandbox_config, tmp_path)
        result = _run_bwrap(sandbox_config, make_sandbox_task(user_id=""), True)

        root = str(repos.resolve())
        assert not any(
            src == root or src.startswith(root + os.sep)
            for src, _ in _get_bind_pairs(result, "--bind")
        )

    def test_the_native_file_tools_write_only_the_tasks_own_subtree(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """NativeBrain's Read/Write/Edit run in the daemon process with no
        namespace at all, so a bind narrowed on one path and not the other
        leaves the whole exposure open on a `native` brain — which is also the
        configured fallback for an anthropic primary."""
        repos = self._setup(sandbox_config, tmp_path)
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True, exist_ok=True)

        with patch("istota.executor._bwrap_available", return_value=True):
            read, write, _ = native_fs_roots(
                sandbox_config, make_sandbox_task(), True, [], user_temp,
            )

        assert (repos / "alice").resolve() in write
        assert (repos / "bob").resolve() not in write
        assert (repos / "bob").resolve() not in read
        assert repos.resolve() not in write
        assert repos.resolve() not in read

    def test_the_user_id_is_joined_the_same_way_the_temp_dir_joins_it(
        self, sandbox_config, tmp_path,
    ):
        """One rule for how a user id becomes a path component, not two.

        User ids already reach the filesystem through `get_user_temp_dir`. A
        stricter spelling here would mean a user whose task directory exists
        and whose repos directory silently does not; a laxer one would be a new
        hole. So the two are held equal instead, and a deployment that wants to
        constrain user ids constrains them in one place.
        """
        from istota.executor import get_user_repos_dir, get_user_temp_dir

        sandbox_config.developer = DeveloperConfig(
            enabled=True, repos_dir=str(tmp_path / "repos"),
        )
        for user_id in ("alice", "user.with-dots", "MiXeD"):
            repos = get_user_repos_dir(sandbox_config, user_id)
            temp = get_user_temp_dir(sandbox_config, user_id)
            assert repos is not None
            assert repos.name == temp.name
            assert repos.parent == Path(sandbox_config.developer.repos_dir)

    def test_no_configured_root_and_no_user_id_are_both_none(self, sandbox_config, tmp_path):
        from istota.executor import get_user_repos_dir

        sandbox_config.developer = DeveloperConfig(enabled=True, repos_dir="")
        assert get_user_repos_dir(sandbox_config, "alice") is None
        sandbox_config.developer = DeveloperConfig(
            enabled=True, repos_dir=str(tmp_path / "repos"),
        )
        assert get_user_repos_dir(sandbox_config, "") is None

    def test_the_developer_hook_creates_the_directory_the_sandbox_binds(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """The first-task case, end to end across the two modules that state
        the layout separately.

        `setup_env` runs before `build_bwrap_cmd` and is the only thing that
        creates the subtree. Without it a user's first developer task binds
        nothing, the model's first `mkdir -p` lands on bwrap's root tmpfs, and
        the clone it spent minutes on disappears when the task ends.
        """
        from istota.skills.developer import setup_env

        repos = tmp_path / "repos"
        repos.mkdir()
        sandbox_config.developer = DeveloperConfig(enabled=True, repos_dir=str(repos))
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True, exist_ok=True)
        task = make_sandbox_task()

        class _Ctx:
            config = sandbox_config
            user_temp_dir = str(user_temp)

        ctx = _Ctx()
        ctx.task = task
        setup_env(ctx)

        own = repos / "alice"
        assert own.is_dir(), "setup_env did not create the task's repos subtree"
        assert own.stat().st_mode & 0o777 == 0o700

        result = _run_bwrap(sandbox_config, task, True)
        assert any(
            src == str(own.resolve()) for src, _ in _get_bind_pairs(result, "--bind")
        ), "the directory setup_env created is not the one build_bwrap_cmd binds"

    def test_the_hook_creates_nothing_without_a_user_id(
        self, sandbox_config, tmp_path,
    ):
        """The same fail-closed rule on the creating side. A missing user id
        must not turn into a scrub of, and a chmod on, the shared root."""
        from istota.skills.developer import setup_env

        repos = tmp_path / "repos"
        repos.mkdir()
        repos.chmod(0o755)
        sandbox_config.developer = DeveloperConfig(enabled=True, repos_dir=str(repos))
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True, exist_ok=True)

        class _Ctx:
            config = sandbox_config
            user_temp_dir = str(user_temp)

        ctx = _Ctx()
        ctx.task = None
        setup_env(ctx)

        assert list(repos.iterdir()) == []
        assert repos.stat().st_mode & 0o777 == 0o755


class TestBuildBwrapCmdSandboxCacheDir:
    """`security.sandbox_cache_dir` — a disk-backed home for package caches.

    Without it `$HOME/.cache` inside the namespace resolves onto bwrap's own
    root tmpfs, so a `uv sync` unpacks into RAM the host cannot attribute and
    throws it away at task exit (ISSUE-305).
    """

    def _with_cache(self, sandbox_config, cache_dir):
        sandbox_config.security.sandbox_cache_dir = str(cache_dir)
        return sandbox_config

    def test_configured_cache_dir_is_bound_rw_per_user(self, sandbox_config, make_sandbox_task):
        """The bind is `{root}/{user_id}`, never the root itself — a shared cache
        is a surface an admin and a non-admin task both write, and uv trusts its
        unpacked wheels on read."""
        cache = sandbox_config.temp_dir.parent / "uvcache"
        cache.mkdir(parents=True)
        self._with_cache(sandbox_config, cache)

        result = _run_bwrap(sandbox_config, make_sandbox_task(), False)
        per_user = str(cache / "alice")
        assert (per_user, per_user) in _get_bind_pairs(result, "--bind"), \
            f"per-user cache dir not bound RW: {_get_bind_pairs(result, '--bind')}"
        assert (str(cache), str(cache)) not in _get_bind_pairs(result, "--bind"), \
            "the cache root itself was bound — every user would share one directory"

    def test_two_users_get_different_cache_dirs(self, sandbox_config, make_sandbox_task):
        cache = sandbox_config.temp_dir.parent / "uvcache"
        cache.mkdir(parents=True)
        self._with_cache(sandbox_config, cache)

        alice = _run_bwrap(sandbox_config, make_sandbox_task(user_id="alice"), False)
        bob_temp = sandbox_config.temp_dir / "bob"
        bob_temp.mkdir(parents=True, exist_ok=True)
        bob = _run_bwrap(
            sandbox_config, make_sandbox_task(user_id="bob"), False, user_temp=bob_temp,
        )
        assert str(cache / "alice") in alice
        assert str(cache / "alice") not in bob
        assert str(cache / "bob") in bob

    def test_unset_leaves_the_argv_exactly_as_before(self, sandbox_config, make_sandbox_task):
        """Byte-for-byte: an empty key must add nothing at all."""
        cache = sandbox_config.temp_dir.parent / "uvcache"
        cache.mkdir(parents=True)

        without = _run_bwrap(sandbox_config, make_sandbox_task(), False)
        self._with_cache(sandbox_config, cache)
        with_key = _run_bwrap(sandbox_config, make_sandbox_task(), False)

        assert str(cache) not in without
        # The only difference is the cache bind itself — drop that one
        # three-argument group and the two argvs must be identical.
        per_user = str(cache / "alice")
        idx = with_key.index("--bind", 0)
        while with_key[idx + 1] != per_user:
            idx = with_key.index("--bind", idx + 1)
        assert with_key[:idx] + with_key[idx + 3:] == without

    def test_a_missing_directory_falls_open(self, sandbox_config, make_sandbox_task):
        """Configured but absent: build the sandbox without it, don't fail the task."""
        cache = sandbox_config.temp_dir.parent / "never-created"
        self._with_cache(sandbox_config, cache)

        result = _run_bwrap(sandbox_config, make_sandbox_task(), False)
        assert result[0] == "bwrap"
        assert str(cache) not in result

    def test_a_relative_path_falls_open(self, sandbox_config, make_sandbox_task):
        """A relative path would resolve against the daemon's cwd."""
        self._with_cache(sandbox_config, "relative/cache")
        result = _run_bwrap(sandbox_config, make_sandbox_task(), False)
        assert result[0] == "bwrap"
        assert not any("relative/cache" in a for a in result)

    @pytest.mark.requires_dac
    def test_an_unwritable_directory_falls_open(self, sandbox_config, make_sandbox_task):
        cache = sandbox_config.temp_dir.parent / "uvcache"
        cache.mkdir(parents=True)
        cache.chmod(0o500)
        self._with_cache(sandbox_config, cache)
        try:
            result = _run_bwrap(sandbox_config, make_sandbox_task(), False)
            assert result[0] == "bwrap"
            assert str(cache / "alice") not in result
        finally:
            cache.chmod(0o700)

    def test_a_cache_under_the_database_directory_is_refused(self, sandbox_config, make_sandbox_task):
        """The masks run last and are read-only, so a cache under one is a dead
        end uv cannot write. Refuse the cache; never the mask."""
        db_dir = Path(sandbox_config.db_path).parent
        cache = db_dir / "uvcache"
        cache.mkdir(parents=True)
        self._with_cache(sandbox_config, cache)

        result = _run_bwrap(sandbox_config, make_sandbox_task(), False)
        assert str(cache / "alice") not in result
        # The database mask is still there — the cache lost, not the boundary.
        assert str(db_dir.resolve()) in result

    def test_the_cache_bind_precedes_the_database_masks(self, sandbox_config, make_sandbox_task):
        """bwrap applies operations in argv order; a bind after a mask would be
        the one thing the mask block's comment forbids."""
        cache = sandbox_config.temp_dir.parent / "uvcache"
        cache.mkdir(parents=True)
        self._with_cache(sandbox_config, cache)

        result = _run_bwrap(sandbox_config, make_sandbox_task(), False)
        bind_idx = result.index(str(cache / "alice"))
        mask_idx = result.index(str(Path(sandbox_config.db_path).parent.resolve()))
        assert bind_idx < mask_idx, f"cache bind at {bind_idx} lands after the mask at {mask_idx}"


class TestSandboxCacheDirCannotOvermountABind:
    """A cache bind whose destination is an *ancestor* of an earlier mount covers
    it. The bind is emitted late, so this is reachable by configuration alone —
    and each case below revokes a boundary the sandbox is built on.
    """

    def _argv(self, sandbox_config, task, cache_root, user_temp=None):
        sandbox_config.security.sandbox_cache_dir = str(cache_root)
        return _run_bwrap(sandbox_config, task, False, user_temp=user_temp)

    def test_the_task_workspace_root_is_refused(self, sandbox_config, make_sandbox_task):
        """`config.temp_dir` holds every user's workspace, and `.developer`
        inside it carries the credential-fetch helpers behind a read-only
        re-bind. A cache mounted above it makes both writable again."""
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True)
        (user_temp / ".developer").mkdir()

        result = self._argv(
            sandbox_config, make_sandbox_task(), sandbox_config.temp_dir, user_temp=user_temp,
        )
        pairs = _get_bind_pairs(result, "--bind")
        assert (str(sandbox_config.temp_dir), str(sandbox_config.temp_dir)) not in pairs
        # `.developer` is still the last word on that path.
        ro = _get_bind_pairs(result, "--ro-bind")
        assert any(str(user_temp.resolve() / ".developer") == src for src, _ in ro)

    def test_the_huggingface_cache_parent_is_refused(
        self, sandbox_config, make_sandbox_task, tmp_path, monkeypatch,
    ):
        """`$HOME/.cache` is directly above the read-only model-cache bind."""
        fake_home = tmp_path / "home"
        (fake_home / ".cache" / "huggingface").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))

        result = self._argv(sandbox_config, make_sandbox_task(), fake_home / ".cache")
        pairs = _get_bind_pairs(result, "--bind")
        assert (str(fake_home / ".cache"), str(fake_home / ".cache")) not in pairs
        assert str(fake_home / ".cache" / "alice") not in result

    def test_the_claude_binary_directory_is_refused(
        self, sandbox_config, make_sandbox_task, tmp_path, monkeypatch,
    ):
        """`$HOME/.local` holds the `claude` binary the daemon spawns host-side."""
        fake_home = tmp_path / "home"
        (fake_home / ".local" / "bin").mkdir(parents=True)
        monkeypatch.setenv("HOME", str(fake_home))

        result = self._argv(sandbox_config, make_sandbox_task(), fake_home / ".local")
        assert str(fake_home / ".local" / "alice") not in result

    def test_the_developer_repos_root_is_refused(self, sandbox_config, make_sandbox_task, tmp_path):
        """Setting the cache *to* repos_dir — a one-character misreading of the
        docs, which say to put it *under* repos_dir — would bind the repos RW
        for non-admins, past the admin gate the repos bind itself carries."""
        repos = tmp_path / "repos"
        repos.mkdir()
        sandbox_config.developer.enabled = True
        sandbox_config.developer.repos_dir = str(repos)

        result = self._argv(sandbox_config, make_sandbox_task(), repos)
        pairs = _get_bind_pairs(result, "--bind")
        assert (str(repos), str(repos)) not in pairs, \
            "a non-admin task was handed the developer repos directory"

    def test_a_cache_strictly_under_repos_dir_is_the_documented_shape(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """`--disable-userns` is patched on because a cache under `repos_dir` is
        covered by the repos bind, and the sibling masks that close that are
        only a boundary when a nested user namespace is refused — see
        `TestSandboxCacheSiblingMasks`. Without the patch this asks the host's
        own bwrap, and there is no bwrap on darwin."""
        repos = tmp_path / "repos"
        (repos / "cache").mkdir(parents=True)
        sandbox_config.developer.enabled = True
        sandbox_config.developer.repos_dir = str(repos)

        with patch(
            "istota.executor._bwrap_supports_disable_userns", return_value=True,
        ):
            result = self._argv(sandbox_config, make_sandbox_task(), repos / "cache")
        per_user = str(repos / "cache" / "alice")
        assert (per_user, per_user) in _get_bind_pairs(result, "--bind")

    def test_the_nextcloud_mount_root_is_refused(self, sandbox_config, make_sandbox_task):
        result = self._argv(
            sandbox_config, make_sandbox_task(), sandbox_config.nextcloud_mount_path,
        )
        pairs = _get_bind_pairs(result, "--bind")
        mount = str(sandbox_config.nextcloud_mount_path)
        assert (mount, mount) not in pairs


class TestSandboxCacheSiblingMasks:
    """Every *other* user's cache is masked when a later bind covers the root.

    A cache root under a bind emitted after it is covered by that bind, and
    bwrap applies argv in order — so the whole root, every user's subdirectory,
    read-write, reaches every task the covering bind belongs to (ISSUE-319).

    The covering bind is not the defect and is not removed: it is the mount
    that lets `link(2)` work, and both a cache root outside the covering bind
    and one carved back out with a nested bind were measured returning EXDEV.
    The exposure is closed by masking the siblings instead.

    **The covering bind is now `{repos_dir}/{user_id}`, not `repos_dir`**, so
    that is where these fixtures put the cache root. A cache root at
    `{repos_dir}/.package-caches` is no longer covered by anything and needs no
    mask — which is the layout change, not a weakening: with one owner per
    subtree there is no sibling to mask in the first place. The masks stay
    under test until the derivation moves the cache inside the user's own
    subtree and this machinery is deleted with it.
    """

    def _setup(self, sandbox_config, tmp_path, users=("alice", "bob")):
        repos = tmp_path / "repos"
        cache_root = repos / "alice" / ".package-caches"
        for user in users:
            (cache_root / user).mkdir(parents=True)
        sandbox_config.developer.enabled = True
        sandbox_config.developer.repos_dir = str(repos)
        sandbox_config.security.sandbox_cache_dir = str(cache_root)
        return repos / "alice", cache_root

    def _argv(self, sandbox_config, task, is_admin=True, userns=True):
        with patch(
            "istota.executor._bwrap_supports_disable_userns", return_value=userns,
        ):
            return _run_bwrap(sandbox_config, task, is_admin)

    def test_another_users_cache_is_masked(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """The regression. Without the mask, `.package-caches/bob` is reachable
        read-write through the repos bind, and uv trusts its own unpacked
        wheels on read — so one admin's task can plant an archive the next
        admin's `uv sync` hardlinks out of and executes."""
        _, cache_root = self._setup(sandbox_config, tmp_path)
        result = self._argv(sandbox_config, make_sandbox_task())

        assert str(cache_root / "bob") in _tmpfs_masks(result), \
            f"bob's cache is not masked: {_tmpfs_masks(result)}"

    def test_the_tasks_own_cache_is_not_masked(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """Masking it would be a read-only dead end uv cannot write, which is
        the whole point of the bind."""
        _, cache_root = self._setup(sandbox_config, tmp_path)
        result = self._argv(sandbox_config, make_sandbox_task())

        own = str(cache_root / "alice")
        assert own not in _tmpfs_masks(result)
        assert (own, own) in _get_bind_pairs(result, "--bind")

    def test_a_symlinked_entry_drops_the_cache_bind(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """`Path.is_dir()` follows symlinks, so masking one covers its own name
        while the target stays reachable through the repos bind. Degrade closed:
        the cache goes back to the root tmpfs, which costs RAM and nothing else."""
        _, cache_root = self._setup(sandbox_config, tmp_path)
        (cache_root / "carol").symlink_to(cache_root / "bob")

        result = self._argv(sandbox_config, make_sandbox_task())
        assert str(cache_root / "alice") not in result, \
            "the cache was bound with a symlinked sibling in the root"

    @pytest.mark.requires_dac
    def test_an_unlistable_root_drops_the_cache_bind(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """If the root cannot be listed the sibling set is unknown, so the cache
        must not be bound uncovered. The instinct to fall open is the wrong one
        here: this is the only path where the mask *is* the boundary."""
        _, cache_root = self._setup(sandbox_config, tmp_path)
        cache_root.chmod(0o300)  # writable and traversable, not listable
        try:
            result = self._argv(sandbox_config, make_sandbox_task())
            assert str(cache_root / "alice") not in result
        finally:
            cache_root.chmod(0o700)

    def test_nothing_is_masked_when_no_later_bind_covers_the_root(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """A cache root outside `repos_dir` is bound per-user and nothing else
        in the root reaches the namespace, so a sibling mask would only invent
        a directory that was never there."""
        cache_root = tmp_path / "caches"
        (cache_root / "alice").mkdir(parents=True)
        (cache_root / "bob").mkdir(parents=True)
        sandbox_config.security.sandbox_cache_dir = str(cache_root)

        result = self._argv(sandbox_config, make_sandbox_task())
        assert str(cache_root / "bob") not in result
        assert str(cache_root / "alice") in result

    def test_a_non_admin_task_gets_neither_the_repos_bind_nor_the_masks(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """The repos bind is admin-gated, so nothing covers the cache root and
        the siblings were never in the namespace to begin with."""
        user_repos, cache_root = self._setup(sandbox_config, tmp_path)
        result = self._argv(sandbox_config, make_sandbox_task(), is_admin=False)

        assert (str(user_repos), str(user_repos)) not in _get_bind_pairs(result, "--bind")
        assert str(cache_root / "bob") not in _tmpfs_masks(result)
        assert str(cache_root / "alice") in result

    def test_the_masks_land_after_every_bind(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """A mask before the covering repos bind would be overmounted by it,
        which is the same ordering mistake read the other way round."""
        user_repos, cache_root = self._setup(sandbox_config, tmp_path)
        result = self._argv(sandbox_config, make_sandbox_task())

        repos_idx = max(
            i for i, a in enumerate(result)
            if a == "--bind" and result[i + 1] == str(user_repos)
        )
        mask_idx = result.index(str(cache_root / "bob"))
        assert repos_idx < mask_idx

    def test_the_masks_are_read_only(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """A writable empty tmpfs is a place another user's cache can be
        rebuilt under a name the next task's uv would trust."""
        _, cache_root = self._setup(sandbox_config, tmp_path)
        with patch("istota.executor._bwrap_supports_remount_ro", return_value=True):
            result = self._argv(sandbox_config, make_sandbox_task())

        bob = str(cache_root / "bob")
        idx = result.index(bob)
        assert result[idx - 1] == "--tmpfs"
        assert result[idx + 1 : idx + 3] == ["--remount-ro", bob]

    def test_a_file_in_the_root_is_not_masked(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """Only directories are user caches. A stray file is left alone rather
        than covered with a tmpfs bwrap would fail to mount over it."""
        _, cache_root = self._setup(sandbox_config, tmp_path)
        (cache_root / "README").write_text("caches live here\n")

        result = self._argv(sandbox_config, make_sandbox_task())
        assert str(cache_root / "README") not in result
        assert str(cache_root / "bob") in _tmpfs_masks(result)

    def test_without_disable_userns_the_cache_is_not_bound_at_all(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """Here the mask *is* the boundary, not defence in depth behind the
        skill CLIs. A process that can `unshare -Urm` can umount the mask and
        read what is underneath, so on a bwrap that cannot refuse the nested
        namespace the cache goes back to RAM instead."""
        _, cache_root = self._setup(sandbox_config, tmp_path)
        result = self._argv(sandbox_config, make_sandbox_task(), userns=False)

        assert str(cache_root / "alice") not in result
        assert str(cache_root / "bob") not in result

    def test_an_uncovered_cache_survives_a_bwrap_without_disable_userns(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """The userns dependency is a property of the covered placement, not of
        the cache. Outside `repos_dir` there is no mask and nothing to unmount,
        so an old bwrap keeps the disk-backed cache ISSUE-305 bought."""
        cache_root = tmp_path / "caches"
        (cache_root / "alice").mkdir(parents=True)
        sandbox_config.security.sandbox_cache_dir = str(cache_root)

        result = self._argv(sandbox_config, make_sandbox_task(), userns=False)
        assert str(cache_root / "alice") in result


class TestSandboxCacheSiblingMasksHardening:
    """The branches two reviews found, each with the failure it prevents.

    Every one of these is a way the sibling masks silently stop being a
    boundary while the cache bind keeps going out — which is ISSUE-319 back,
    with a passing test suite over it.
    """

    def _setup(self, sandbox_config, repos, users=("alice", "bob")):
        # Inside the task's own subtree, which is what the repos bind covers
        # now — see `TestSandboxCacheSiblingMasks`.
        cache_root = repos / "alice" / ".package-caches"
        for user in users:
            (cache_root / user).mkdir(parents=True)
        sandbox_config.developer.enabled = True
        sandbox_config.developer.repos_dir = str(repos)
        sandbox_config.security.sandbox_cache_dir = str(cache_root)
        return cache_root

    def _argv(self, sandbox_config, task, is_admin=True):
        with patch(
            "istota.executor._bwrap_supports_disable_userns", return_value=True,
        ):
            return _run_bwrap(sandbox_config, task, is_admin)

    def test_a_symlinked_repos_dir_still_gets_the_masks(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """The covering scan compares a bind *destination*, and `_bind` writes
        the path as it was handed — unresolved, deliberately, so the cache and
        the repos tree stay one mount. A resolved-only comparison finds no
        coverer under `/srv -> /realstore`, emits no masks, and disagrees with
        `_sandbox_cache_is_covered`, which resolves both sides and does find
        one. The two halves would then disagree in the direction that exposes.
        """
        real = tmp_path / "realstore" / "repos"
        real.mkdir(parents=True)
        link = tmp_path / "srv-repos"
        link.symlink_to(real)
        cache_root = self._setup(sandbox_config, link)

        result = self._argv(sandbox_config, make_sandbox_task())
        masks = _tmpfs_masks(result)
        real_bob = real / "alice" / ".package-caches" / "bob"
        assert str(cache_root / "bob") in masks or str(real_bob) in masks, \
            f"a symlinked repos_dir left bob's cache unmasked: {masks}"

    def test_too_many_entries_refuses_the_cache(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """The root is inside `repos_dir`, which a task can write, and each mask
        costs up to four argv entries. Enough `mkdir`s push `execve` past E2BIG
        and fail *every later task at launch* — a denial of service on the whole
        daemon from inside one sandbox. Refusing the cache costs RAM instead."""
        from istota.executor import MAX_SANDBOX_CACHE_SIBLINGS

        repos = tmp_path / "repos"
        cache_root = self._setup(sandbox_config, repos)
        for i in range(MAX_SANDBOX_CACHE_SIBLINGS + 1):
            (cache_root / f"pad{i}").mkdir()

        result = self._argv(sandbox_config, make_sandbox_task())
        assert str(cache_root / "alice") not in result, \
            "the cache was bound with an entry count that would blow the argv"
        assert len(_tmpfs_masks(result)) < MAX_SANDBOX_CACHE_SIBLINGS

    def test_an_entry_that_cannot_be_classified_refuses_the_cache(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """`Path.is_symlink` swallows only ENOENT/ENOTDIR/ELOOP/EINVAL/EBADF, so
        an EACCES propagates. Appending such an entry would mask a *symlink* at
        its own name while its target stayed reachable — the exact shape the
        symlink branch refuses — or hand bwrap `--tmpfs <regular file>`, which
        cannot be mounted and fails every task before it runs."""
        repos = tmp_path / "repos"
        cache_root = self._setup(sandbox_config, repos)

        real_is_symlink = Path.is_symlink

        def _raises(self):
            if self.name == "bob":
                raise PermissionError(13, "Permission denied")
            return real_is_symlink(self)

        with patch.object(Path, "is_symlink", _raises):
            result = self._argv(sandbox_config, make_sandbox_task())
        assert str(cache_root / "alice") not in result
        assert str(cache_root / "bob") not in result

    def test_a_refused_sibling_mask_is_never_silent(
        self, sandbox_config, make_sandbox_task, tmp_path, caplog,
    ):
        """`_mask_dir` was written for the database masks, where a refusal costs
        defence in depth behind the skill CLIs and a log line is the honest
        answer. Here it costs the whole boundary, so it must not be a `continue`
        the caller never hears about.

        The root-mask fallback cannot rescue this particular refusal and the
        code says so: `_mask_dir` refuses only a candidate holding a path the
        sandbox needs, and such a path is under the root whenever it is under a
        sibling. So what is asserted is the loud second error, which is the only
        thing left to do — not a mask that provably cannot be emitted.
        """
        repos = tmp_path / "repos"
        cache_root = self._setup(sandbox_config, repos)
        # The one thing `_mask_dir` refuses: a candidate containing a path the
        # sandbox needs. Put the task's workspace under bob's cache.
        user_temp = cache_root / "bob" / "workspace"
        user_temp.mkdir(parents=True)

        with caplog.at_level("ERROR"), \
                patch("istota.executor._bwrap_supports_disable_userns", return_value=True):
            _run_bwrap(sandbox_config, make_sandbox_task(), True, user_temp=user_temp)

        assert "could not be masked either" in caplog.text, (
            "a sibling mask was refused and the sandbox was built anyway with "
            f"nothing said about it:\n{caplog.text}"
        )
        assert "reachable read-write from this task" in caplog.text

    def test_the_native_brain_denies_writes_to_another_users_cache(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """The bwrap masks are one of two paths. NativeBrain's Read/Write/Edit
        run in the daemon process with no namespace at all, and `repos_dir` is
        one of their write roots — so without a matching denial the planted-wheel
        path is wide open on a `native` brain, which is also the configured
        fallback for an anthropic primary."""
        repos = tmp_path / "repos"
        cache_root = self._setup(sandbox_config, repos)
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True, exist_ok=True)

        with patch("istota.executor._bwrap_available", return_value=True), \
                patch("istota.executor._bwrap_supports_disable_userns", return_value=True):
            _, write, denied = native_fs_roots(
                sandbox_config, make_sandbox_task(), True, [], user_temp,
            )

        assert (cache_root / "bob").resolve() in [p.resolve() for p in denied], \
            f"bob's cache is writable by the native file tools: {denied}"
        assert (cache_root / "alice").resolve() in [p.resolve() for p in write], \
            "the task's own cache is not writable, so the denial went too far"
