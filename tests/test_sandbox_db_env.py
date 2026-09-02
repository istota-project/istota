"""Database paths reach the skill proxy, never the model's subprocess.

Companion to ``test_sandbox_db_isolation.py``: that file proves the DB *files*
are absent from the sandbox, this one proves the *paths* are too, and that the
host-side skill CLIs still get everything they need — including
``ISTOTA_DEFERRED_DIR``, whose absence is what makes a skill CLI take its
direct-write fallback instead of deferring.
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import (
    Config,
    DeveloperConfig,
    NextcloudConfig,
    SchedulerConfig,
    SecurityConfig,
)


def _write_skill_md(base_dir: Path, name: str, frontmatter: dict, body: str = "") -> Path:
    skill_dir = base_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    lines = ["---"]
    for key, value in frontmatter.items():
        if isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, list) and value and isinstance(value[0], dict):
            lines.append(f"{key}: {json.dumps(value, separators=(',', ':'))}")
        elif isinstance(value, list):
            lines.append(f"{key}: [{', '.join(str(v) for v in value)}]")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    lines.append(body or f"{name} docs")
    md_path = skill_dir / "skill.md"
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path


@pytest.fixture
def env_config(tmp_path):
    """Config with the proxy on and one skill declaring a proxy-only path var."""
    empty_bundled = tmp_path / "_empty_bundled"
    empty_bundled.mkdir()
    skills_dir = tmp_path / "skills"
    skills_dir.mkdir()
    # A non-secret var that must still be withheld from the model: it names a
    # database file. Mirrors HEALTH_DB_PATH / LOCATION_DB_PATH, which come from
    # setup_env hooks and so can't be exercised from a synthetic manifest.
    _write_skill_md(skills_dir, "widget", {
        "description": "Widget store",
        "always_include": True,
        "cli": True,
        "env": [{
            "var": "WIDGET_DB_PATH", "from": "config",
            "config_path": "db_path", "proxy_only": True,
        }],
    })
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)

    return Config(
        db_path=tmp_path / "data" / "istota.db",
        module_data_dir=tmp_path / "data" / "modules",
        nextcloud=NextcloudConfig(
            url="https://nc.example.com", username="bot", app_password="nc_secret",
        ),
        nextcloud_mount_path=mount,
        skills_dir=skills_dir,
        bundled_skills_dir=empty_bundled,
        temp_dir=tmp_path / "temp",
        scheduler=SchedulerConfig(task_timeout_minutes=5),
        security=SecurityConfig(skill_proxy_enabled=True, skill_proxy_timeout=30),
    )


def _run_task(config, admin_users=None):
    """Execute one task; return (claude_env, proxy_kwargs).

    ``proxy_kwargs`` is ``(args, kwargs)`` of the SkillProxy constructor call,
    or ``None`` when no proxy was started.
    """
    config.admin_users = admin_users or set()
    config.db_path.parent.mkdir(parents=True, exist_ok=True)
    (config.temp_dir / "alice").mkdir(parents=True, exist_ok=True)
    db.init_db(config.db_path)

    with patch("istota.executor.subprocess.run") as mock_run, \
            patch("istota.skill_proxy.SkillProxy") as mock_proxy:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        mock_proxy.return_value.__enter__ = lambda s: s
        mock_proxy.return_value.__exit__ = lambda s, *a: False
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="test", user_id="alice", source_type="talk",
            )
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

    claude_env = mock_run.call_args[1]["env"]
    proxy_call = mock_proxy.call_args if mock_proxy.call_args else None
    return claude_env, proxy_call


def _proxy_base_env(proxy_call):
    """The SkillProxy ``base_env`` positional argument."""
    return proxy_call[0][2]


class TestProxyIsUnconditional:
    """`istota-skill` must never fall back to running inside the sandbox."""

    def test_proxy_starts_without_any_credential(self, env_config):
        """The old `if credential_env:` gate left tasks with no proxy at all."""
        env_config.nextcloud = NextcloudConfig()  # no app_password => no secrets
        claude_env, proxy_call = _run_task(env_config)
        assert proxy_call is not None, "proxy must start even with no credentials"
        assert "ISTOTA_SKILL_PROXY_SOCK" in claude_env

    def test_proxy_starts_with_credentials(self, env_config):
        claude_env, proxy_call = _run_task(env_config)
        assert proxy_call is not None
        assert "ISTOTA_SKILL_PROXY_SOCK" in claude_env


class TestClaudeRuntimeEnvIsNotInTheProxyBaseEnv:
    """ISSUE-390's second route, and the less obvious one.

    The skill proxy's `base_env` is snapshotted from the same task env the
    brains get, and `CLAUDE_CODE_OAUTH_TOKEN` is declared in no skill manifest —
    so neither `derive_credential_set` nor `derive_proxy_only_set` takes it out,
    and it rode into every host-side skill CLI. Those run unsandboxed as the
    daemon user, and the model reaches them through the same Bash tool the
    `NativeBrain` strips cleaned. Nothing there reads it: no skill invokes the
    `claude` binary.
    """

    def test_the_token_does_not_reach_a_skill_cli(self, env_config):
        with patch.dict(
            os.environ, {"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat-fake-for-tests"},
        ):
            claude_env, proxy_call = _run_task(env_config)
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in _proxy_base_env(proxy_call)
        # Control: the variable really was set for this task, so the assertion
        # above is not passing merely because the daemon had no token.
        assert claude_env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-fake-for-tests"


class TestFrameworkDbPathRouting:
    """ISTOTA_DB_PATH goes to the proxy, for every user, and to no one else."""

    @pytest.mark.parametrize(
        "admin_users", [set(), {"bob"}], ids=["admin", "non-admin"],
    )
    def test_not_in_claude_env(self, env_config, admin_users):
        claude_env, _ = _run_task(env_config, admin_users=admin_users)
        assert "ISTOTA_DB_PATH" not in claude_env

    @pytest.mark.parametrize(
        "admin_users", [set(), {"bob"}], ids=["admin", "non-admin"],
    )
    def test_in_proxy_base_env(self, env_config, admin_users):
        """Non-admins get scoped reads too — the boundary is the SQL, not the env."""
        _, proxy_call = _run_task(env_config, admin_users=admin_users)
        base_env = _proxy_base_env(proxy_call)
        assert base_env["ISTOTA_DB_PATH"] == str(env_config.db_path)


class TestProxyOnlyVars:
    """A manifest can mark a non-secret var as proxy-only."""

    def test_not_in_claude_env(self, env_config):
        claude_env, _ = _run_task(env_config)
        assert "WIDGET_DB_PATH" not in claude_env

    def test_in_proxy_base_env(self, env_config):
        _, proxy_call = _run_task(env_config)
        assert _proxy_base_env(proxy_call)["WIDGET_DB_PATH"] == str(env_config.db_path)

    def test_bundled_module_db_vars_are_proxy_only(self, tmp_path):
        """The two real cases, read off the shipped manifests."""
        from istota.executor import derive_proxy_only_set
        from istota.skills._loader import load_skill_index

        overrides = tmp_path / "no_overrides"
        overrides.mkdir()
        proxy_only = derive_proxy_only_set(load_skill_index(overrides))
        assert "HEALTH_DB_PATH" in proxy_only
        assert "LOCATION_DB_PATH" in proxy_only


class TestSandboxMarker:
    """The marker tells an in-sandbox `istota-skill` to refuse rather than guess."""

    def test_set_in_claude_env_when_sandbox_active(self, env_config):
        env_config.security.sandbox_enabled = True
        with patch("istota.executor._bwrap_available", return_value=True):
            claude_env, _ = _run_task(env_config)
        assert claude_env["ISTOTA_SANDBOXED"] == "1"

    def test_absent_when_sandbox_disabled(self, env_config):
        """Patch bwrap available, so this tests the config half of the `and`.

        Without the patch `_bwrap_available()` is already False off Linux and
        the assertion holds for the wrong reason on every dev machine.
        """
        env_config.security.sandbox_enabled = False
        with patch("istota.executor._bwrap_available", return_value=True):
            claude_env, _ = _run_task(env_config)
        assert "ISTOTA_SANDBOXED" not in claude_env

    def test_absent_when_bwrap_unavailable(self, env_config):
        """The Docker-without-CAP_SYS_ADMIN shape: configured on, not in effect."""
        env_config.security.sandbox_enabled = True
        with patch("istota.executor._bwrap_available", return_value=False):
            claude_env, _ = _run_task(env_config)
        assert "ISTOTA_SANDBOXED" not in claude_env

    def test_absent_when_proxy_disabled(self, env_config):
        """With no proxy there is no socket, so the marker would break every
        skill CLI — including the many that never open a database."""
        env_config.security.skill_proxy_enabled = False
        with patch("istota.executor._bwrap_available", return_value=True):
            claude_env, _ = _run_task(env_config)
        assert "ISTOTA_SANDBOXED" not in claude_env


class TestDbPathWithheldWithoutProxy:
    """Turning the proxy off makes skill CLIs unreachable, not the path safe."""

    def test_db_path_not_in_claude_env(self, env_config):
        env_config.security.skill_proxy_enabled = False
        claude_env, proxy_call = _run_task(env_config)
        assert proxy_call is None
        assert "ISTOTA_DB_PATH" not in claude_env
        assert "WIDGET_DB_PATH" not in claude_env

    def test_absent_from_proxy_base_env(self, env_config):
        """The proxy runs skills on the host; they are not sandboxed."""
        env_config.security.sandbox_enabled = True
        with patch("istota.executor._bwrap_available", return_value=True):
            _, proxy_call = _run_task(env_config)
        assert "ISTOTA_SANDBOXED" not in _proxy_base_env(proxy_call)


class TestDeferredWritesStillDefer:
    """Host-side execution must not silently switch skills to direct writes.

    Every deferring skill CLI (`kv`, `memory_search`, `health`, `email`) keys
    off ``ISTOTA_DEFERRED_DIR`` and falls back to writing the DB itself when it
    is unset. Losing it from the proxy env would quietly drop the post-success
    gating that makes deferred ops safe.
    """

    def test_deferred_dir_in_proxy_base_env(self, env_config):
        _, proxy_call = _run_task(env_config)
        base_env = _proxy_base_env(proxy_call)
        assert base_env["ISTOTA_DEFERRED_DIR"] == str(env_config.temp_dir / "alice")

    def test_task_id_in_proxy_base_env(self, env_config):
        _, proxy_call = _run_task(env_config)
        assert _proxy_base_env(proxy_call)["ISTOTA_TASK_ID"]

    def test_user_id_in_proxy_base_env(self, env_config):
        """The scoping boundary for every framework-DB read."""
        _, proxy_call = _run_task(env_config)
        assert _proxy_base_env(proxy_call)["ISTOTA_USER_ID"] == "alice"


class TestSkillClientRefusesInSandbox:
    """Fail closed, not silently local."""

    def test_direct_run_refused_when_sandboxed(self, monkeypatch, capsys):
        from istota.skill_client import _run_direct

        monkeypatch.setenv("ISTOTA_SANDBOXED", "1")
        monkeypatch.delenv("ISTOTA_SKILL_PROXY_SOCK", raising=False)
        with pytest.raises(SystemExit) as exc:
            _run_direct("kv", ["get", "x"])
        assert exc.value.code == 1
        assert "skill proxy" in capsys.readouterr().err.lower()

    @patch("istota.skill_client.subprocess.run")
    def test_direct_run_allowed_when_not_sandboxed(self, mock_run, monkeypatch):
        """Cron `command:` rows and heartbeat shells run unsandboxed and still work."""
        from istota.skill_client import _run_direct

        monkeypatch.delenv("ISTOTA_SANDBOXED", raising=False)
        mock_run.return_value = MagicMock(returncode=0)
        with pytest.raises(SystemExit) as exc:
            _run_direct("kv", ["get", "x"])
        assert exc.value.code == 0
        assert mock_run.call_args[0][0][1:3] == ["-m", "istota.skills.kv"]


class TestForgeCliPathPrepend:
    """ISTOTA_PATH_PREPEND reaches the model's PATH and nothing else's.

    The developer hook puts `.developer` ahead of /usr/local/bin so the model
    typing `gh` reaches the wrapper. That directory must not appear on the PATH
    the skill proxy hands host-side skill CLIs: those run outside bwrap as the
    daemon user, and two of them resolve a binary by bare name —
    google_workspace does os.execvp("gws", ...), devbox does
    shutil.which("docker"). A task-temp directory on that PATH is a host-side
    code-execution path.

    The executor comment calls this ordering load-bearing. These tests are what
    make that true rather than merely stated.
    """

    @pytest.fixture
    def dev_config(self, env_config, tmp_path):
        env_config.developer = DeveloperConfig(
            enabled=True,
            repos_dir=str(tmp_path / "repos"),
            gitlab_url="https://gitlab.example.com",
            gitlab_token="glpat-test",
            gitlab_username="bot",
        )
        # The real bundled skills dir, so the developer manifest and its
        # setup_env hook are actually in the index. env_config points at an
        # empty one, under which the hook never runs and every assertion below
        # would pass for the wrong reason.
        env_config.bundled_skills_dir = None
        return env_config

    @staticmethod
    def _dev_bin_entries(path_value, temp_dir):
        """PATH entries under the task's .developer, matched by realpath.

        /var is a symlink to /private/var on macOS, so comparing the strings
        the hook emitted against a resolved expectation fails for a reason
        that has nothing to do with the property under test.
        """
        target = os.path.realpath(temp_dir / "alice" / ".developer")
        return [
            e for e in path_value.split(os.pathsep)
            if e and os.path.realpath(e) == target
        ]

    def test_developer_dir_is_on_the_models_path(self, dev_config):
        claude_env, _ = _run_task(dev_config)
        assert self._dev_bin_entries(claude_env["PATH"], dev_config.temp_dir)

    def test_developer_dir_comes_before_the_system_path(self, dev_config):
        """Behind /usr/local/bin it would resolve to the real gh, unwrapped."""
        claude_env, _ = _run_task(dev_config)
        entries = claude_env["PATH"].split(os.pathsep)
        target = os.path.realpath(dev_config.temp_dir / "alice" / ".developer")
        assert os.path.realpath(entries[0]) == target

    def test_developer_dir_is_absent_from_the_proxy_base_env(self, dev_config):
        """The finding this whole ordering exists to prevent."""
        _, proxy_call = _run_task(dev_config)
        assert proxy_call is not None
        base_env = _proxy_base_env(proxy_call)
        assert not self._dev_bin_entries(
            base_env.get("PATH", ""), dev_config.temp_dir,
        )
        # Nothing under the task temp root at all, not just .developer.
        temp_root = os.path.realpath(dev_config.temp_dir)
        for entry in base_env.get("PATH", "").split(os.pathsep):
            if not entry:
                continue
            assert not os.path.realpath(entry).startswith(temp_root), entry

    def test_reserved_key_reaches_neither_environment(self, dev_config):
        """It is plumbing; the model should never see it, and neither should
        a skill CLI that might be tempted to honour it."""
        claude_env, proxy_call = _run_task(dev_config)
        assert "ISTOTA_PATH_PREPEND" not in claude_env
        assert "ISTOTA_PATH_PREPEND" not in _proxy_base_env(proxy_call)

    def test_no_prepend_without_the_developer_skill(self, env_config):
        """A deployment with no developer tokens gets an untouched PATH."""
        claude_env, proxy_call = _run_task(env_config)
        assert "ISTOTA_PATH_PREPEND" not in claude_env
        assert claude_env["PATH"] == _proxy_base_env(proxy_call)["PATH"]
