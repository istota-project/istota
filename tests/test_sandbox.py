"""Tests for bubblewrap sandbox (build_bwrap_cmd)."""

import os
import stat
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
    resolve_sandbox_cache_dir,
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
        """The *per-user* root, and only it.

        `repos_dir` is a root holding every user's worktrees. Binding the root
        would give one admin's task read and write access to another's
        checkouts — which is also the reach a devbox mounting the root would
        hand the container, and the reason the layout went per user.
        """
        repos_dir = tmp_path / "repos"
        mine = repos_dir / "alice"
        theirs = repos_dir / "bob"
        mine.mkdir(parents=True)
        theirs.mkdir(parents=True)
        sandbox_config.developer = DeveloperConfig(
            enabled=True,
            repos_dir=str(repos_dir),
        )
        task = make_sandbox_task()
        result = _run_bwrap(sandbox_config, task, True)
        bind_pairs = _get_bind_pairs(result, "--bind")
        sources = {src for src, _ in bind_pairs}
        assert str(mine.resolve()) in sources
        assert str(repos_dir.resolve()) not in sources
        assert str(theirs.resolve()) not in sources

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


class TestNoDockerReachesTheSandbox:
    """The Docker API left the sandbox, and the `docker` binary with it.

    This used to be `TestDevboxDockerProxyBind`, asserting that a per-user
    allowlist proxy was bound at `/var/run/docker.sock` **unconditionally** —
    every task of every user on a devbox deployment, including tasks built from
    email, feeds and fetched pages. That was safe on its own terms (the proxy
    refused create, run, build, privileged and host-mount) and it is gone
    anyway, because nothing in a task reaches Docker now: project code goes to
    the container over the exec transport, and the devbox skill's one Docker
    verb, `reset`, runs host-side in the skill CLI's own process.

    Asserted with the capability **on**, which is the configuration that used to
    produce the bind. With it off there was never anything to find.
    """

    def _devbox_config(self, base: Config, tmp_path: Path):
        cli = tmp_path / "docker"
        cli.touch()
        base.devbox = DevboxConfig(enabled=True, docker_cli=str(cli))
        return base, cli

    def test_nothing_is_bound_at_the_conventional_docker_path(
        self, sandbox_config, make_sandbox_task, tmp_path
    ):
        config, _ = self._devbox_config(sandbox_config, tmp_path)
        task = make_sandbox_task(user_id="alice")

        result = _run_bwrap(config, task, False)

        assert "/var/run/docker.sock" not in result
        for flag in ("--bind", "--ro-bind"):
            assert not any(
                dest == "/var/run/docker.sock" for _, dest in _get_bind_pairs(result, flag)
            )

    def test_the_raw_socket_is_never_a_bind_source_either(
        self, sandbox_config, make_sandbox_task, tmp_path
    ):
        config, _ = self._devbox_config(sandbox_config, tmp_path)
        task = make_sandbox_task(user_id="alice")

        result = _run_bwrap(config, task, False)

        for flag in ("--bind", "--ro-bind"):
            assert not any(
                src == "/var/run/docker.sock" for src, _ in _get_bind_pairs(result, flag)
            )

    def test_the_docker_client_binary_is_not_bound(
        self, sandbox_config, make_sandbox_task, tmp_path
    ):
        """The explicit bind went with the socket it existed to serve.

        Not the thing that makes Docker unreachable: `/usr` is `--ro-bind`ed
        whole, so `/usr/bin/docker` is in the namespace on any host with the
        client installed. The socket is the boundary; this is the redundant
        bind that used to point at it.
        """
        config, cli = self._devbox_config(sandbox_config, tmp_path)
        task = make_sandbox_task(user_id="alice")

        result = _run_bwrap(config, task, False)

        assert str(cli) not in result


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
            return sorted(
                src for src, _ in _get_bind_pairs(argv, "--bind")
                if src.startswith(root + os.sep)
            )

        # Two binds under the root per task, not one: the subtree and the
        # package cache derived inside it. An exhaustive list rather than a
        # containment check, because what would go wrong is an *extra* entry
        # naming somebody else.
        for user, argv in (("alice", alice), ("bob", bob)):
            assert _repos_binds(argv) == sorted([
                str((repos / user).resolve()),
                str((repos / user / ".package-caches").resolve()),
            ]), f"{user}'s argv binds something else under the repos root"

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
            is_admin = True

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
            is_admin = True

        ctx = _Ctx()
        ctx.task = None
        setup_env(ctx)

        assert list(repos.iterdir()) == []
        assert repos.stat().st_mode & 0o777 == 0o755


    def test_a_planted_symlink_is_refused_by_both_sides(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """The entry is model-plantable, and this is the shape that makes it so.

        Every deployment running the shared bind gave an admin task read-write
        access to the whole root, so `{repos_dir}/{user_id}` may already be a
        symlink a task left there. `_bind` and `_add` resolve their source and
        `chmod` follows a link, so without a containment check the sandbox
        binds the target read-write and the hook chmods and rewrites git
        configs under it.
        """
        from istota.executor import get_user_repos_dir
        from istota.skills.developer import setup_env

        repos = tmp_path / "repos"
        repos.mkdir()
        outside = tmp_path / "outside"
        outside.mkdir()
        (outside / "victim").mkdir()
        (repos / "alice").symlink_to(outside)
        sandbox_config.developer = DeveloperConfig(enabled=True, repos_dir=str(repos))

        assert get_user_repos_dir(sandbox_config, "alice") is None

        result = _run_bwrap(sandbox_config, make_sandbox_task(), True)
        assert str(outside.resolve()) not in result
        assert str((outside / "victim").resolve()) not in result

        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True, exist_ok=True)

        class _Ctx:
            config = sandbox_config
            user_temp_dir = str(user_temp)
            is_admin = True

        ctx = _Ctx()
        ctx.task = make_sandbox_task()
        before = outside.stat().st_mode & 0o777
        setup_env(ctx)
        assert outside.stat().st_mode & 0o777 == before, \
            "the hook chmodded a directory outside the repos root"

    @pytest.mark.parametrize("user_id", [".", "..", "/etc", "a/b"])
    def test_a_user_id_that_is_not_one_component_is_refused(
        self, sandbox_config, tmp_path, user_id,
    ):
        """Truthiness is not containment. `.` collapses to the shared root,
        `..` to its parent, an absolute component replaces the root outright,
        and a nested one lands in some other user's tree — each of them a path
        the bind, the native write roots, the `chmod` and the scrub's rewrites
        would all be pointed at."""
        from istota.executor import get_user_repos_dir

        sandbox_config.developer = DeveloperConfig(
            enabled=True, repos_dir=str(tmp_path / "repos"),
        )
        assert get_user_repos_dir(sandbox_config, user_id) is None

    def test_a_failed_chmod_still_scrubs(
        self, sandbox_config, make_sandbox_task, tmp_path, monkeypatch,
    ):
        """The two failures are not one. `mkdir(exist_ok=True)` succeeds on a
        directory another uid owns and `chmod` then raises EPERM — and
        `build_bwrap_cmd` binds that directory regardless, because its gate is
        the path's existence. Returning early on the chmod would bind an
        unscrubbed tree, which is ISSUE-270 back on the one shape (a migrator
        or an operator made the directory) where it is most likely.
        """
        from istota.skills import developer as developer_skill

        repos = tmp_path / "repos"
        (repos / "alice").mkdir(parents=True)
        sandbox_config.developer = DeveloperConfig(enabled=True, repos_dir=str(repos))
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True, exist_ok=True)

        real_chmod = os.chmod

        def _refuse(path, mode, **kw):
            # Only the subtree: the hook chmods its generated helper scripts too.
            if Path(path) == repos / "alice":
                raise PermissionError(1, "Operation not permitted")
            return real_chmod(path, mode, **kw)

        monkeypatch.setattr(developer_skill.os, "chmod", _refuse)
        scrubbed = []
        monkeypatch.setattr(
            developer_skill, "scrub_and_report",
            lambda root, **kw: scrubbed.append(Path(root)),
        )

        class _Ctx:
            config = sandbox_config
            user_temp_dir = str(user_temp)
            is_admin = True

        ctx = _Ctx()
        ctx.task = make_sandbox_task()
        developer_skill.setup_env(ctx)

        assert scrubbed == [repos / "alice"]

    def test_a_non_admin_task_does_not_create_a_subtree(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """The bind is admin-gated, so creating and chmodding a subtree for a
        user no sandbox ever binds is churn on every task and every heartbeat
        tick. The two gates agree instead."""
        from istota.skills.developer import setup_env

        repos = tmp_path / "repos"
        repos.mkdir()
        sandbox_config.developer = DeveloperConfig(enabled=True, repos_dir=str(repos))
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True, exist_ok=True)

        class _Ctx:
            config = sandbox_config
            user_temp_dir = str(user_temp)
            is_admin = False

        ctx = _Ctx()
        ctx.task = make_sandbox_task()
        env = setup_env(ctx)

        assert list(repos.iterdir()) == []
        # and the rest of the hook still ran
        assert "GIT_CONFIG_COUNT" in env or env == {}


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

    def test_the_developer_repos_root_is_still_on_the_list(self, sandbox_config, tmp_path):
        """A cache mounted over `repos_dir` would cover every user's subtree
        beneath it and bind them RW for a non-admin, past the admin gate the
        repos bind itself carries.

        Asserted against the list rather than end to end, and the reason is
        worth knowing before someone "restores" the end-to-end case. The
        derivation means `security.sandbox_cache_dir` is read only where
        `developer.repos_dir` is *unset*, and this entry is appended only where
        it is set — so no config can currently produce the refusal, and a
        bwrap-level test of it would be asserting something that cannot happen.
        The entry stays because the list is the answer to one question (what
        must a cache never be mounted above) and `repos_dir` belongs on it on
        the merits; a second reader of the key would want it already here.
        """
        repos = tmp_path / "repos"
        repos.mkdir()
        sandbox_config.developer.enabled = True
        sandbox_config.developer.repos_dir = str(repos)

        from istota.executor import _sandbox_bind_targets
        assert repos in _sandbox_bind_targets(sandbox_config)

    def test_the_nextcloud_mount_root_is_refused(self, sandbox_config, make_sandbox_task):
        result = self._argv(
            sandbox_config, make_sandbox_task(), sandbox_config.nextcloud_mount_path,
        )
        pairs = _get_bind_pairs(result, "--bind")
        mount = str(sandbox_config.nextcloud_mount_path)
        assert (mount, mount) not in pairs


class TestDerivedSandboxCacheDir:
    """The cache is derived from `developer.repos_dir`, not configured.

    `{repos_dir}/{user_id}/.package-caches`. The repos bind is that user's own
    subtree and is emitted after the cache bind, so it is an ancestor and covers
    it — one mount, which is the only shape where uv hardlinks a wheel into a
    venv instead of copying it. ISSUE-319 was that the covering bind was the
    whole shared tree, so it also exposed every other user's cache; the layout
    is what removed that, and the ~200 lines of sibling masks went with it.

    The classes that used to live here (`TestSandboxCacheSiblingMasks` and its
    hardening twin) asserted the mask machinery. Both are gone deliberately: a
    mask over a directory that is not in the namespace is not a weaker version
    of this property, it is a different property nothing needs.
    """

    def _setup(self, sandbox_config, tmp_path, users=("alice", "bob")):
        repos = tmp_path / "repos"
        for user in users:
            (repos / user).mkdir(parents=True)
        sandbox_config.developer.enabled = True
        sandbox_config.developer.repos_dir = str(repos)
        return repos

    def test_the_cache_derives_from_the_users_own_repos_subtree(
        self, sandbox_config, tmp_path,
    ):
        repos = self._setup(sandbox_config, tmp_path)
        cache = resolve_sandbox_cache_dir(sandbox_config, "alice")

        assert cache == repos / "alice" / ".package-caches"
        assert cache.is_dir()
        assert stat.S_IMODE(cache.stat().st_mode) == 0o700

    def test_the_configured_key_is_not_read_when_repos_dir_is_set(
        self, sandbox_config, tmp_path,
    ):
        """Not a default the key overrides — the derivation is the layout. A
        value left in `security.sandbox_cache_dir` from before this change must
        not quietly put one deployment's caches back in a shared root."""
        repos = self._setup(sandbox_config, tmp_path)
        stale = tmp_path / "old-caches"
        stale.mkdir()
        sandbox_config.security.sandbox_cache_dir = str(stale)

        cache = resolve_sandbox_cache_dir(sandbox_config, "alice")
        assert cache == repos / "alice" / ".package-caches"
        assert not (stale / "alice").exists()

    def test_the_configured_root_is_the_fallback_with_no_repos_dir(
        self, sandbox_config, tmp_path,
    ):
        """A deployment running the sandbox without the developer skill. There
        is no repos tree to put a cache in and ISSUE-305 still applies."""
        cache_root = tmp_path / "caches"
        cache_root.mkdir()
        sandbox_config.security.sandbox_cache_dir = str(cache_root)

        assert resolve_sandbox_cache_dir(sandbox_config, "alice") == cache_root / "alice"

    def test_two_users_get_caches_in_their_own_subtrees(self, sandbox_config, tmp_path):
        repos = self._setup(sandbox_config, tmp_path)
        alice = resolve_sandbox_cache_dir(sandbox_config, "alice")
        bob = resolve_sandbox_cache_dir(sandbox_config, "bob")

        assert alice == repos / "alice" / ".package-caches"
        assert bob == repos / "bob" / ".package-caches"

    def test_the_cache_bind_precedes_the_repos_bind_and_is_covered_by_it(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """The hardlink property, expressed as argv order. bwrap applies
        operations in sequence, so the later ancestor bind covers the earlier
        one and both end up on a single mount — `link(2)` compares mounts rather
        than devices, so the alternative is a full byte copy of every wheel into
        every venv. This is what the sibling masks used to be layered on top of,
        and it must stay asserted now that they are gone.
        """
        repos = self._setup(sandbox_config, tmp_path)
        result = _run_bwrap(sandbox_config, make_sandbox_task(), True)

        dests = [d for _, d in _get_bind_pairs(result, "--bind")]
        cache = str(repos / "alice" / ".package-caches")
        subtree = str(repos / "alice")
        assert cache in dests and subtree in dests
        assert dests.index(subtree) > dests.index(cache), \
            "the repos bind no longer covers the cache bind — uv stops hardlinking"

    def test_no_tmpfs_is_emitted_anywhere_under_the_repos_root(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """The absence is the point. Every mask the ISSUE-319 fix emitted was a
        directory belonging to another user inside a shared root; there is no
        such directory in the namespace now, and masking one would only make
        bwrap invent a mountpoint that was never there.
        """
        repos = self._setup(sandbox_config, tmp_path)
        # A second user's subtree and cache, populated on disk — the exact thing
        # that used to be masked.
        (repos / "bob" / ".package-caches" / "uv").mkdir(parents=True)

        result = _run_bwrap(sandbox_config, make_sandbox_task(), True)

        under_repos = [m for m in _tmpfs_masks(result) if str(repos) in m]
        assert under_repos == [], f"a mask under the repos root survived: {under_repos}"
        # And bob's tree is not in the namespace at all, masked or otherwise.
        assert not any(str(repos / "bob") in a for a in result)

    def test_a_symlinked_cache_directory_is_refused(self, sandbox_config, tmp_path):
        """The containment assertion, and the reason it is not paranoia: the
        cache's parent is bound read-write into this very task's sandbox, so the
        entry is model-plantable. Without the check the daemon would `mkdir`
        through the symlink, `chmod 0700` bob's subtree and bind that target RW
        on the next task — ISSUE-319 back through a name.
        """
        repos = self._setup(sandbox_config, tmp_path)
        (repos / "alice" / ".package-caches").symlink_to(repos / "bob")

        assert resolve_sandbox_cache_dir(sandbox_config, "alice") is None
        # Nothing was created through the link either.
        assert list((repos / "bob").iterdir()) == []

    def test_a_symlinked_per_user_directory_is_refused_in_the_configured_root(
        self, sandbox_config, tmp_path,
    ):
        """Same rule on the fallback branch. The old default put the configured
        root inside `repos_dir`, so a planted entry there is not hypothetical
        either, and one rule is easier to keep true than two."""
        cache_root = tmp_path / "caches"
        (cache_root / "bob").mkdir(parents=True)
        (cache_root / "alice").symlink_to(cache_root / "bob")
        sandbox_config.security.sandbox_cache_dir = str(cache_root)

        assert resolve_sandbox_cache_dir(sandbox_config, "alice") is None

    def test_an_empty_user_id_does_not_collapse_onto_the_root(
        self, sandbox_config, tmp_path,
    ):
        """`root / ""` is the root, so the old code handed a task with no user
        id the shared root as its private cache — silently, and that is the one
        directory every other user's cache lives in."""
        cache_root = tmp_path / "caches"
        cache_root.mkdir()
        sandbox_config.security.sandbox_cache_dir = str(cache_root)

        assert resolve_sandbox_cache_dir(sandbox_config, "") is None

    def test_a_non_admin_gets_a_cache_but_no_repos_bind(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """ISSUE-305 applies to any task that runs a package manager, so the
        cache is gated on neither admin nor skill selection. Its parent is not
        bound for a non-admin, so it is its own mount and a venv in the task
        workspace pays the copy — the same cost as before this change, stated
        here so nobody reads it as a regression.
        """
        repos = self._setup(sandbox_config, tmp_path)
        result = _run_bwrap(sandbox_config, make_sandbox_task(), False)

        dests = [d for _, d in _get_bind_pairs(result, "--bind")]
        assert str(repos / "alice" / ".package-caches") in dests
        assert str(repos / "alice") not in dests

    def test_a_first_task_creates_the_subtree_the_cache_needs(
        self, sandbox_config, tmp_path,
    ):
        """The developer skill's `setup_env` creates `{repos_dir}/{user_id}` for
        an admin only, matching the bind's gate. A non-admin's subtree is made
        here, with parents, or their first task has no disk-backed cache."""
        repos = tmp_path / "repos"
        repos.mkdir()
        sandbox_config.developer.enabled = True
        sandbox_config.developer.repos_dir = str(repos)

        cache = resolve_sandbox_cache_dir(sandbox_config, "carol")
        assert cache == repos / "carol" / ".package-caches"
        assert cache.is_dir()
        # Both levels 0700, not just the leaf: a directory made by
        # `parents=True` takes the umask, and this one is a user's repos
        # subtree on every path but this one.
        assert stat.S_IMODE((repos / "carol").stat().st_mode) == 0o700
        assert stat.S_IMODE(cache.stat().st_mode) == 0o700

    def test_a_missing_repos_root_falls_open_rather_than_creating_a_tree(
        self, sandbox_config, tmp_path,
    ):
        """A typo in `developer.repos_dir` should be a warning and a cache in
        RAM, not the daemon materializing a directory tree from a bad value."""
        sandbox_config.developer.enabled = True
        sandbox_config.developer.repos_dir = str(tmp_path / "never-created")

        assert resolve_sandbox_cache_dir(sandbox_config, "alice") is None
        assert not (tmp_path / "never-created").exists()

    def test_an_old_bwrap_still_gets_a_disk_backed_cache(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """The `--disable-userns` precondition is gone with the masks it
        guarded. A mask is not a revocation — a process that can `unshare -Urm`
        can umount it — which is why a covered cache used to be refused outright
        on a bwrap that could not refuse the nested namespace. No mask holds
        this boundary any more; the layout does, and umounting one reveals the
        task's own subtree. So an old bwrap keeps the disk-backed cache
        ISSUE-305 bought instead of paying RAM for a boundary it is not
        providing.
        """
        repos = self._setup(sandbox_config, tmp_path)
        with patch(
            "istota.executor._bwrap_supports_disable_userns", return_value=False,
        ):
            result = _run_bwrap(sandbox_config, make_sandbox_task(), True)

        cache = str(repos / "alice" / ".package-caches")
        assert cache in [d for _, d in _get_bind_pairs(result, "--bind")]

    def test_the_native_brain_has_no_sibling_denials_left(
        self, sandbox_config, make_sandbox_task, tmp_path,
    ):
        """The other half of the demolition. `write_denied_roots` carried every
        other user's cache because the shared root sat inside a write root and
        the bwrap masks are a namespace-only property. Nothing is denied now
        because nothing else is reachable — and the task's own cache stays
        writable, which is what the bind is for.
        """
        repos = self._setup(sandbox_config, tmp_path)
        (repos / "bob" / ".package-caches").mkdir(parents=True)
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True, exist_ok=True)

        with patch("istota.executor._bwrap_available", return_value=True):
            _, write, denied = native_fs_roots(
                sandbox_config, make_sandbox_task(), True, [], user_temp,
            )

        assert not any("bob" in str(p) for p in denied), denied
        cache = (repos / "alice" / ".package-caches").resolve()
        assert cache in [p.resolve() for p in write]

    def test_the_issue_319_machinery_is_gone(self):
        """Deleted, not bypassed. Each of these existed only because one cache
        root had many owners; a dormant copy left behind invites the next reader
        to wire it back up against a layout that does not need it.
        """
        import istota.executor as executor_mod

        for name in (
            "_sandbox_cache_covering_targets",
            "_sandbox_cache_is_covered",
            "sandbox_cache_sibling_dirs",
            "MAX_SANDBOX_CACHE_SIBLINGS",
            "_BWRAP_BIND_VERBS",
        ):
            assert not hasattr(executor_mod, name), f"{name} survived the demolition"

    def test_the_two_spellings_of_the_cache_name_agree(self):
        """The developer skill restates the constant because it cannot import
        the executor that imports it — `istota.skills` star-imports every skill.
        Two spellings drifting apart would leave the credential scrub walking
        the cache it means to skip, one wheel directory at a time, on every
        task."""
        from istota.executor import SANDBOX_CACHE_ROOT_NAME as executor_name
        from istota.skills.developer import SANDBOX_CACHE_ROOT_NAME as skill_name

        assert executor_name == skill_name

    def test_the_credential_scrub_skips_the_cache_it_now_walks_over(
        self, sandbox_config, make_sandbox_task, tmp_path, monkeypatch,
    ):
        """Before the derivation the cache root sat beside the subtree and the
        skip was inert. It is inside the walked tree now, so this is the
        difference between a scrub bounded by the user's clones and one that
        spends `git_remote_scrub`'s depth budget on unpacked wheels, on every
        task.
        """
        from istota.skills import developer as developer_skill

        repos = self._setup(sandbox_config, tmp_path)
        user_temp = sandbox_config.temp_dir / "alice"
        user_temp.mkdir(parents=True, exist_ok=True)

        calls = []
        monkeypatch.setattr(
            developer_skill, "scrub_and_report",
            lambda root, **kw: calls.append((Path(root), kw.get("skip"))),
        )

        class _Ctx:
            config = sandbox_config
            user_temp_dir = str(user_temp)
            is_admin = True

        ctx = _Ctx()
        ctx.task = make_sandbox_task()
        developer_skill.setup_env(ctx)

        assert len(calls) == 1
        root, skip = calls[0]
        assert root == repos / "alice"
        assert [Path(p) for p in (skip or [])] == [
            repos / "alice" / ".package-caches"
        ], f"the scrub does not skip the cache inside the tree it walks: {skip}"

    def test_the_configured_key_is_honoured_when_the_skill_is_disabled(
        self, sandbox_config, tmp_path,
    ):
        """`repos_dir` set and `developer.enabled` false is a real shape — it is
        the rendered default, since the role writes the `[developer]` block only
        when the skill is on. The derivation's whole justification is that the
        repos bind covers the cache, and that bind is gated on
        `is_admin and config.developer.enabled`; with the skill off there is no
        covering bind, so deriving would take the operator's explicit fallback
        away and give nothing back.
        """
        repos = tmp_path / "repos"
        repos.mkdir()
        cache_root = tmp_path / "caches"
        cache_root.mkdir()
        sandbox_config.developer.enabled = False
        sandbox_config.developer.repos_dir = str(repos)
        sandbox_config.security.sandbox_cache_dir = str(cache_root)

        assert resolve_sandbox_cache_dir(sandbox_config, "alice") == cache_root / "alice"
        assert not (repos / "alice").exists()

    def test_the_never_raises_contract_covers_the_branch_selection(
        self, sandbox_config, tmp_path,
    ):
        """`build_bwrap_cmd` reaches this per Bash call under NativeBrain, so an
        exception here fails the task rather than falling open to the RAM cache.
        The branch selection touches paths, and `get_user_repos_dir` guards only
        `OSError` — `Path.resolve()` raises `ValueError` on an embedded null
        byte and the join raises `TypeError` on a non-str user id, neither of
        which it catches. Both used to be impossible here because the old code
        read a plain string attribute before entering the `try`.
        """
        repos = tmp_path / "repos"
        repos.mkdir()
        sandbox_config.developer.enabled = True
        sandbox_config.developer.repos_dir = str(repos)

        for user_id in ("al\x00ice", 7):
            assert resolve_sandbox_cache_dir(sandbox_config, user_id) is None

    def test_a_symlink_planted_after_the_check_does_not_get_chmodded(
        self, sandbox_config, tmp_path, monkeypatch,
    ):
        """The containment check and the `chmod` are separated by a `mkdir`, and
        both re-traverse the path by name. The parent is bound read-write into a
        live task's sandbox and this function runs on the task path, so the
        writer and the checker are concurrent by construction — `os.chmod`
        follows a symlink, so a swap in that window would set 0700 on another
        user's subtree. `O_NOFOLLOW` refuses it instead.

        The window is simulated by planting the link from inside `mkdir`, which
        is where a real racing task would land it.
        """
        repos = self._setup(sandbox_config, tmp_path)
        cache = repos / "alice" / ".package-caches"
        victim = repos / "bob"
        victim.chmod(0o755)

        real_mkdir = Path.mkdir

        def _plant(self, *a, **kw):
            if self == cache and not cache.exists():
                cache.symlink_to(victim)
                return
            return real_mkdir(self, *a, **kw)

        monkeypatch.setattr(Path, "mkdir", _plant)
        assert resolve_sandbox_cache_dir(sandbox_config, "alice") is None
        assert stat.S_IMODE(victim.stat().st_mode) == 0o755, \
            "another user's subtree was chmodded through a planted symlink"
