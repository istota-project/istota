"""The CLI tool harness, executor side (cli-brain-tools spec, Stage 6).

What the executor hands a `claude_code` brain so the CLI can run outside the
sandbox with its tools in the tool server: the switch, the four conditions
under which a request gets a `HarnessSpec`, the CLI's env, the harness root
and its sweep, and the per-harness `CLAUDE_CONFIG_DIR`. Stage 7 adds the
brain's half (argv, the init check, the failure order, the seam test).
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import executor
from istota.brain._cli_config_dir import make_cli_config_dir
from istota.config import BrainConfig, Config, SecurityConfig, load_config
from istota.executor import (
    HARNESS_CLI_SWITCHES,
    HARNESS_DIR_NAME,
    build_harness_cli_env,
    ensure_task_harness_dir,
    get_task_harness_dir,
    harness_wanted,
)
from istota.llm import providers
from istota.llm.providers import resolve_process_auth

OAUTH = "OAUTH-SENTINEL"
DEPLOY_KEY = "DEPLOY-KEY-SENTINEL"
USER_KEY = "USER-KEY-SENTINEL"


# -- the switch ----------------------------------------------------------------


class TestTheSwitch:
    def test_it_is_off_by_default(self):
        assert Config().brain.tool_harness is False

    def test_it_loads_from_the_brain_table(self, tmp_path):
        path = tmp_path / "config.toml"
        path.write_text("[brain]\ntool_harness = true\n")
        assert load_config(path).brain.tool_harness is True


# -- the four conditions -------------------------------------------------------


def _config(*, on=True, sandbox=False) -> Config:
    config = Config(security=SecurityConfig(sandbox_enabled=sandbox))
    config.brain = BrainConfig(kind="claude_code", tool_harness=on)
    return config


def _wrap(cmd):
    return cmd


class TestTheFourConditions:
    def test_all_four_hold(self):
        assert harness_wanted(
            _config(), brain_kind="claude_code", allowed_tools=["Bash"],
            native_sandbox_wrap=_wrap,
        )

    def test_the_switch_off(self):
        assert not harness_wanted(
            _config(on=False), brain_kind="claude_code", allowed_tools=["Bash"],
            native_sandbox_wrap=_wrap,
        )

    @pytest.mark.parametrize("kind", ["native", "tmux_claude"])
    def test_another_brain(self, kind):
        assert not harness_wanted(
            _config(), brain_kind=kind, allowed_tools=["Bash"],
            native_sandbox_wrap=_wrap,
        )

    def test_a_text_only_request(self):
        assert not harness_wanted(
            _config(), brain_kind="claude_code", allowed_tools=[],
            native_sandbox_wrap=_wrap,
        )

    def test_sandboxed_with_no_native_wrap(self):
        config = _config(sandbox=True)
        with patch.object(executor, "_bwrap_available", return_value=True):
            assert not harness_wanted(
                config, brain_kind="claude_code", allowed_tools=["Bash"],
                native_sandbox_wrap=None,
            )
            assert harness_wanted(
                config, brain_kind="claude_code", allowed_tools=["Bash"],
                native_sandbox_wrap=_wrap,
            )

    def test_unsandboxed_with_no_native_wrap(self):
        assert harness_wanted(
            _config(sandbox=False), brain_kind="claude_code", allowed_tools=["Bash"],
            native_sandbox_wrap=None,
        )


class TestTheRequestBuilder:
    """Through `execute_task`, with the brain stubbed at `make_brain`."""

    def _run(self, tmp_path, *, on):
        from tests.test_executor_advisor_model import _FakeAnthropicBrain
        from tests.test_executor_streaming import (
            _make_config,
            _make_task,
            _patch_executor,
            contextmanager_chain,
        )

        config = _make_config(tmp_path)
        config.brain = BrainConfig(kind="claude_code", tool_harness=on)
        config.security.sandbox_enabled = False
        brain = _FakeAnthropicBrain()
        patches = _patch_executor() + [
            patch("istota.executor.make_brain", return_value=brain),
        ]
        with contextmanager_chain(patches):
            task = _make_task(source_type="cli", model="")
            execute_task_result = executor.execute_task(task, config, [])
        assert execute_task_result[0], execute_task_result
        return config, task, brain.received_reqs[0]

    def test_the_switch_on_sets_the_harness(self, tmp_path):
        config, task, req = self._run(tmp_path, on=True)

        assert req.allowed_tools
        assert req.harness is not None
        root = req.harness.harness_root
        assert root == get_task_harness_dir(config, task.user_id, task.id)
        assert root.is_dir()
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert req.harness.cli_env["MCP_TOOL_TIMEOUT"] == str(
            (req.timeout_seconds + 30) * 1000
        )
        assert req.harness.bash_spill_full_output == config.brain.native.bash_spill_full_output

    def test_the_switch_off_leaves_it_unset(self, tmp_path):
        config, task, req = self._run(tmp_path, on=False)

        assert req.harness is None
        assert not (Path(config.temp_dir) / HARNESS_DIR_NAME).exists()


# -- the CLI's env -------------------------------------------------------------


@pytest.fixture
def env_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH)

    def make(mode: str) -> Config:
        config = Config(
            db_path=tmp_path / "istota.db", temp_dir=tmp_path / "temp",
        )
        config.brain.claude_code.auth = mode
        config.providers["anthropic"].api_key = DEPLOY_KEY
        resolve_process_auth(config, dict(os.environ))
        return config

    return make


class TestTheCliEnv:
    def test_it_carries_the_credential_and_the_harness_switches(self, env_config):
        env = build_harness_cli_env(env_config("subscription"), "alice", timeout_seconds=600)

        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH
        for name, value in HARNESS_CLI_SWITCHES.items():
            assert env[name] == value
        assert env["CLAUDE_CODE_DISABLE_ATTACHMENTS"] == "1"
        assert env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] == "1"
        assert env["DISABLE_AUTOUPDATER"] == "1"
        assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1"
        assert env["MCP_TOOL_TIMEOUT"] == str(630 * 1000)
        assert env["ISTOTA_CLI_AUTH"] == "subscription"

    def test_it_carries_nothing_of_the_task(self, env_config):
        env = build_harness_cli_env(env_config("subscription"), "alice", timeout_seconds=60)

        for name in ("ISTOTA_SKILL_PROXY_SOCK", "ISTOTA_DEFERRED_DIR", "ISTOTA_TASK_ID",
                     "ISTOTA_USER_ID", "ISTOTA_TASK_ATTEMPT", "ISTOTA_DB_PATH",
                     "ISTOTA_CONVERSATION_TOKEN", "ISTOTA_SANDBOXED",
                     "CLAUDE_CONFIG_DIR", "TMPDIR"):
            assert name not in env, name

    def test_the_users_own_anthropic_key_wins_under_api_key(self, env_config):
        config = env_config("api_key")

        def own_key(_config, user_id, provider_id):
            return USER_KEY if user_id == "alice" else None

        with patch.object(providers, "user_provider_key", side_effect=own_key):
            alice = build_harness_cli_env(config, "alice", timeout_seconds=60)
            bob = build_harness_cli_env(config, "bob", timeout_seconds=60)

        assert alice["ANTHROPIC_API_KEY"] == USER_KEY
        assert bob["ANTHROPIC_API_KEY"] == DEPLOY_KEY
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in alice


# -- the harness root ------------------------------------------------------------


@pytest.fixture
def root_config(tmp_path):
    temp = tmp_path / "temp"
    temp.mkdir()
    return Config(db_path=tmp_path / "data" / "istota.db", temp_dir=temp)


class TestTheHarnessRoot:
    def test_it_names_the_documented_path(self, root_config):
        assert get_task_harness_dir(root_config, "alice", 42) == (
            root_config.temp_dir.resolve() / ".harness" / "alice" / "task_42"
        )

    def test_it_is_created_0700_at_each_level(self, root_config):
        path = ensure_task_harness_dir(root_config, "alice", 42)

        for level in (path, path.parent, path.parent.parent):
            assert stat.S_IMODE(level.stat().st_mode) == 0o700

    @pytest.mark.parametrize("user_id", [".harness", ".HARNESS", ".control", "", "..", "a/b"])
    def test_a_user_id_that_names_a_root_or_escapes_is_refused(self, root_config, user_id):
        assert get_task_harness_dir(root_config, user_id, 1) is None
        with pytest.raises(RuntimeError, match="task harness directory"):
            ensure_task_harness_dir(root_config, user_id, 1)

    def test_the_control_dir_refuses_a_user_named_after_the_harness_root(self, root_config):
        assert executor.get_task_control_dir(root_config, ".harness", 1) is None

    def test_a_symlinked_root_is_refused(self, root_config, tmp_path):
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (root_config.temp_dir / ".harness").symlink_to(elsewhere)

        assert get_task_harness_dir(root_config, "alice", 1) is None

    def test_the_sweep_takes_a_stale_harness_tree(self, root_config):
        from istota.scheduler import cleanup_old_temp_files

        root = ensure_task_harness_dir(root_config, "alice", 3)
        harness = root / "h-1-abcd"
        (harness / "config" / "projects").mkdir(parents=True)
        session = harness / "config" / "projects" / "s.jsonl"
        session.write_text("{}")
        target = root_config.temp_dir / "daemon-credentials.json"
        target.write_text("secret")
        link = harness / "config" / ".credentials.json"
        link.symlink_to(target)
        old = time.time() - 30 * 86400
        for path in (session, harness / "config" / "projects", harness / "config",
                     harness, root, root.parent, root.parent.parent):
            os.utime(path, (old, old))
        os.utime(link, (old, old), follow_symlinks=False)
        os.utime(target, (time.time(), time.time()))

        cleanup_old_temp_files(root_config, retention_days=7)

        assert not session.exists()
        assert not link.is_symlink()
        assert target.read_text() == "secret"


class TestTheMountPlanNeverNamesTheHarnessRoot:
    def test_no_bind_names_anything_under_it(self, tmp_path):
        from istota import db
        from istota.sandbox.plan import SandboxProfile

        mount = tmp_path / "mount"
        (mount / "Users" / "alice").mkdir(parents=True)
        temp = tmp_path / "temp"
        temp.mkdir()
        config = Config(
            db_path=tmp_path / "data" / "istota.db", temp_dir=temp,
            workspace_path=mount, skills_dir=tmp_path / "skills",
            security=SecurityConfig(sandbox_enabled=True),
        )
        task = db.Task(
            id=9, prompt="x", user_id="alice", source_type="talk",
            status="running", conversation_token="room1",
        )
        harness_root = ensure_task_harness_dir(config, "alice", 9)
        control = executor.ensure_task_control_dir(config, "alice", 9)
        user_temp = temp / "alice"
        user_temp.mkdir()

        for profile in (SandboxProfile.CLAUDE, SandboxProfile.NATIVE):
            with patch.object(executor, "_bwrap_available", return_value=True):
                argv = executor.build_bwrap_cmd(
                    ["true"], config, task, True, [], user_temp,
                    extra_ro_binds=[control], profile=profile,
                )
            for arg in argv:
                assert HARNESS_DIR_NAME not in arg, (profile, arg)
            assert str(control) in argv
        assert harness_root.is_dir()


# -- the CLI's config dir --------------------------------------------------------


class TestTheCliConfigDir:
    def _home(self, tmp_path) -> Path:
        home = tmp_path / "daemon-home"
        claude = home / ".claude"
        (claude / "skills" / "planted").mkdir(parents=True)
        (claude / "skills" / "planted" / "SKILL.md").write_text("planted")
        (claude / "settings.json").write_text(json.dumps({"hooks": {"Stop": []}}))
        (claude / "CLAUDE.md").write_text("planted")
        (claude / ".credentials.json").write_text("{}")
        (home / ".claude.json").write_text("{}")
        return home

    def test_subscription_links_the_credentials_and_seeds_onboarding(self, tmp_path):
        home = self._home(tmp_path)
        harness = tmp_path / "h-1-ab"
        harness.mkdir()
        cwd = tmp_path / "work"

        config_dir = make_cli_config_dir(harness, "subscription", home=str(home), launch_cwd=cwd)

        assert config_dir == harness / "config"
        assert stat.S_IMODE(config_dir.stat().st_mode) == 0o700
        assert sorted(p.name for p in config_dir.iterdir()) == [
            ".claude.json", ".credentials.json", "settings.json",
        ]
        link = config_dir / ".credentials.json"
        assert link.is_symlink()
        assert Path(os.readlink(link)) == home / ".claude" / ".credentials.json"
        assert json.loads((config_dir / "settings.json").read_text()) == {}
        seeded = json.loads((config_dir / ".claude.json").read_text())
        assert seeded["hasCompletedOnboarding"] is True
        assert seeded["projects"][str(cwd)]["hasTrustDialogAccepted"] is True
        for name in ("settings.json", ".claude.json"):
            assert stat.S_IMODE((config_dir / name).stat().st_mode) == 0o600

    def test_api_key_links_nothing(self, tmp_path):
        home = self._home(tmp_path)
        harness = tmp_path / "h-1-ab"
        harness.mkdir()

        config_dir = make_cli_config_dir(harness, "api_key", home=str(home), launch_cwd=tmp_path)

        assert sorted(p.name for p in config_dir.iterdir()) == [".claude.json", "settings.json"]

    def test_an_existing_config_dir_is_refused(self, tmp_path):
        harness = tmp_path / "h-1-ab"
        (harness / "config").mkdir(parents=True)

        with pytest.raises(FileExistsError):
            make_cli_config_dir(harness, None, home="", launch_cwd=tmp_path)

    def test_the_tool_harness_builds_it_from_the_spec(self, tmp_path):
        """Through `tool_harness`: the mode and the daemon's HOME come from
        `HarnessSpec.cli_env`, and nothing of a planted `~/.claude` arrives."""
        from istota.brain._tool_harness import tool_harness
        from istota.brain._types import BrainRequest, HarnessSpec

        home = self._home(tmp_path)
        workspace = tmp_path / "ws"
        workspace.mkdir()
        root = tmp_path / "harness-root"
        root.mkdir()
        req = BrainRequest(
            prompt="", allowed_tools=["Read"], cwd=workspace,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            timeout_seconds=60, task_id=7, attempt=1,
            harness=HarnessSpec(
                cli_env={"ISTOTA_CLI_AUTH": "subscription", "HOME": str(home)},
                harness_root=root,
            ),
        )
        with tool_harness(req, brain_label="test brain", on_failure=lambda: None) as handle:
            assert handle.failure is None
            names = sorted(p.name for p in handle.config_dir.iterdir())
            seeded = json.loads((handle.config_dir / ".claude.json").read_text())

        assert names == [".claude.json", ".credentials.json", "settings.json"]
        assert str(handle.cwd) in seeded["projects"]
