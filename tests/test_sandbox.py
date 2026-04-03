"""Tests for bubblewrap sandbox (build_bwrap_cmd)."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, DeveloperConfig, NetworkConfig, ResourceConfig, SecurityConfig, UserConfig
from istota.executor import (
    _build_network_allowlist,
    build_bwrap_cmd,
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
            f".credentials.json not in --ro-bind pairs"
        assert not any(src == creds_str for src, _ in rw_pairs), \
            f".credentials.json should not be in --bind (RW) pairs"


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


class TestSecurityConfigSandboxFields:
    """Test that sandbox config fields load correctly."""

    def test_defaults(self):
        sc = SecurityConfig()
        assert sc.sandbox_enabled is True
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

    def test_miniflux_host_scoped_to_user(self):
        """Miniflux host comes from user_config, not all users."""
        alice_uc = UserConfig(resources=[
            ResourceConfig(type="miniflux", base_url="https://feeds.alice.example.com", api_key="k"),
        ])
        bob_uc = UserConfig(resources=[
            ResourceConfig(type="miniflux", base_url="https://feeds.bob.example.com", api_key="k"),
        ])
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            users={"alice": alice_uc, "bob": bob_uc},
        )
        hosts = _build_network_allowlist(config, ["feeds"], user_config=alice_uc)
        assert "feeds.alice.example.com:443" in hosts
        assert "feeds.bob.example.com:443" not in hosts

    def test_moneyman_host_scoped_to_user(self):
        """Moneyman host comes from user_config, not all users."""
        alice_uc = UserConfig(resources=[
            ResourceConfig(type="moneyman", base_url="https://money.alice.example.com", api_key="k"),
        ])
        bob_uc = UserConfig(resources=[
            ResourceConfig(type="moneyman", base_url="https://money.bob.example.com", api_key="k"),
        ])
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            users={"alice": alice_uc, "bob": bob_uc},
        )
        hosts = _build_network_allowlist(config, ["moneyman"], user_config=alice_uc)
        assert "money.alice.example.com:443" in hosts
        assert "money.bob.example.com:443" not in hosts

    def test_no_user_config_excludes_resource_hosts(self):
        """When user_config is None, no resource hosts are added."""
        config = Config(
            security=SecurityConfig(network=NetworkConfig()),
            users={"alice": UserConfig(resources=[
                ResourceConfig(type="miniflux", base_url="https://feeds.example.com", api_key="k"),
            ])},
        )
        hosts = _build_network_allowlist(config, ["feeds"], user_config=None)
        assert "feeds.example.com:443" not in hosts


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
