"""The CLI tool harness (cli-brain-tools spec, Stages 6 and 7).

The executor's half: what it hands a `claude_code` brain so the CLI can run
outside the sandbox with its tools in the tool server (when a request gets a
`HarnessSpec`, the retired switch, the CLI's env, the
harness root and its sweep, the per-harness `CLAUDE_CONFIG_DIR`).

The brain's half, from "ClaudeCodeBrain on the harness" down: the argv, the
`system/init` check, the failure order, the pid rules and the seam. Those run
`ClaudeCodeBrain.execute` for real against `tests/support/fake_claude_cli.py`
on PATH as `claude`, which starts the real launcher from the harness's MCP
config, so every call goes CLI, launcher-exec'd tool server, dispatcher. The
tool server runs unwrapped here; `tests/linux/test_cli_harness_direct.py` runs
it under bwrap.
"""

from __future__ import annotations

import ast
import dataclasses
import json
import os
import signal
import stat
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import executor
from istota.brain import claude_code
from istota.brain._cli_config_dir import make_cli_config_dir
from istota.brain._events import InitEvent, parse_stream_line
from istota.brain._types import BrainRequest, HarnessSpec
from istota.brain.claude_code import (
    HARNESS_DENIED_TOOLS,
    ClaudeCodeBrain,
    build_claude_cli_flags,
)
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
from istota.session.tools import WebFetchPolicy
from tests.support.drift import source_of

OAUTH = "OAUTH-SENTINEL"
DEPLOY_KEY = "DEPLOY-KEY-SENTINEL"
USER_KEY = "USER-KEY-SENTINEL"


# -- the switch is gone ----------------------------------------------------------


class TestTheSwitchIsGone:
    """`[brain] tool_harness` was part 1's switch; part 2, Stage 6 removed it
    with the old in-sandbox CLI path."""

    def test_it_is_no_field(self):
        assert not hasattr(BrainConfig(), "tool_harness")

    def test_a_config_still_naming_it_loads_and_is_told(self, tmp_path):
        from istota.config_mapper import collect_problems

        path = tmp_path / "config.toml"
        path.write_text("[brain]\ntool_harness = true\n")
        with collect_problems() as problems:
            load_config(path)
        about_it = [p["kind"] for p in problems if "tool_harness" in str(p["path"])]
        assert about_it == ["retired"]


# -- when a request gets a harness ---------------------------------------------


def _config(*, sandbox=False) -> Config:
    config = Config(security=SecurityConfig(sandbox_enabled=sandbox))
    config.brain = BrainConfig(kind="claude_code")
    return config


def _wrap(cmd):
    return cmd


class TestWhenARequestGetsAHarness:
    def test_a_claude_code_request_with_tools(self):
        assert harness_wanted(
            _config(), brain_kinds=["claude_code"], allowed_tools=["Bash"],
            tool_server_wrap=_wrap,
        )

    def test_another_brain(self):
        assert not harness_wanted(
            _config(), brain_kinds=["native"], allowed_tools=["Bash"],
            tool_server_wrap=_wrap,
        )

    def test_a_native_task_that_may_fall_back_to_claude_code(self):
        assert harness_wanted(
            _config(), brain_kinds=["native", "claude_code"], allowed_tools=["Bash"],
            tool_server_wrap=_wrap,
        )

    def test_a_text_only_request(self):
        assert not harness_wanted(
            _config(), brain_kinds=["claude_code"], allowed_tools=[],
            tool_server_wrap=_wrap,
        )

    def test_sandboxed_with_no_wrap(self):
        config = _config(sandbox=True)
        with patch.object(executor, "_bwrap_available", return_value=True):
            assert not harness_wanted(
                config, brain_kinds=["claude_code"], allowed_tools=["Bash"],
                tool_server_wrap=None,
            )
            assert harness_wanted(
                config, brain_kinds=["claude_code"], allowed_tools=["Bash"],
                tool_server_wrap=_wrap,
            )

    def test_unsandboxed_with_no_wrap(self):
        assert harness_wanted(
            _config(sandbox=False), brain_kinds=["claude_code"], allowed_tools=["Bash"],
            tool_server_wrap=None,
        )


class TestTheRequestBuilder:
    """Through `execute_task`, with the brain stubbed at `make_brain`."""

    def _run(self, tmp_path, *, kind="claude_code", fallback=""):
        from tests.test_executor_advisor_model import _FakeAnthropicBrain
        from tests.test_executor_streaming import (
            _make_config,
            _make_task,
            _patch_executor,
            contextmanager_chain,
        )

        config = _make_config(tmp_path)
        config.brain = BrainConfig(kind=kind, fallback=fallback)
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

    def test_a_claude_code_task_gets_the_harness(self, tmp_path):
        config, task, req = self._run(tmp_path)

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
        # The directory the tool server's wrap binds read-only, where the
        # harness hands the CLI-spawned server its hello and gate (#862).
        assert req.harness.control_dir == Path(req.composed_system_prompt_path).parent

    def test_the_request_env_holds_no_credential(self, tmp_path, monkeypatch):
        """The tool server's env is `req.env`; the CLI's credential is only in
        `HarnessSpec.cli_env`."""
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", OAUTH)
        _config, _task, req = self._run(tmp_path)

        assert not [k for k in req.env if providers.is_model_credential_name(k)]
        assert OAUTH not in req.env.values()
        assert req.env["ISTOTA_CLI_AUTH"] == "subscription"
        assert req.harness.cli_env["CLAUDE_CODE_OAUTH_TOKEN"] == OAUTH

    def test_a_native_task_falling_back_to_claude_code_carries_one(self, tmp_path):
        """`_run_fallback` reruns this request on the fallback brain, and a
        `claude_code` brain runs tools only on the harness."""
        _config, _task, req = self._run(tmp_path, kind="native", fallback="claude_code")

        assert req.allowed_tools
        assert req.harness is not None

    def test_a_native_task_with_no_cli_fallback_makes_none(self, tmp_path):
        config, _task, req = self._run(tmp_path, kind="native")

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
        # Else the CLI reads `.claude/scheduled_tasks.json` in its cwd.
        assert env["CLAUDE_CODE_DISABLE_CRON"] == "1"
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

    def test_a_direct_caller_gets_a_spec_on_claude_code_only(self, root_config):
        """`build_daemon_harness`: health OCR, the code reviewer and the doctor's
        probe have no task, so their root is `task_0`; any other brain kind
        reads no harness and gets none."""
        spec = executor.build_daemon_harness(
            root_config, "alice", brain_kind="claude_code", timeout_seconds=100,
        )
        assert spec.harness_root == get_task_harness_dir(root_config, "alice", 0)
        assert spec.harness_root.is_dir()
        assert spec.cli_env["MCP_TOOL_TIMEOUT"] == str(130 * 1000)
        for name, value in HARNESS_CLI_SWITCHES.items():
            assert spec.cli_env[name] == value
        assert executor.build_daemon_harness(
            root_config, "alice", brain_kind="native", timeout_seconds=100,
        ) is None

    def test_a_direct_caller_may_name_its_own_root(self, root_config, tmp_path):
        mine = tmp_path / "doctor-root"
        mine.mkdir()
        spec = executor.build_daemon_harness(
            root_config, "alice", brain_kind="claude_code", timeout_seconds=10,
            harness_root=mine,
        )
        assert spec.harness_root == mine
        assert not (root_config.temp_dir / ".harness").exists()

    def test_a_direct_caller_with_no_root_to_make_raises(self, root_config):
        with pytest.raises(RuntimeError):
            executor.build_daemon_harness(
                root_config, "", brain_kind="claude_code", timeout_seconds=10,
            )

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

        for profile in (SandboxProfile.NATIVE,):
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

    def test_the_harness_holds_a_stub_git_that_exits_128(self, tmp_path):
        """The unwrapped CLI runs `git` in its cwd at start-up; the stub it
        finds first answers "not a repository" and reads nothing."""
        import subprocess

        from istota.brain._tool_harness import tool_harness

        workspace = tmp_path / "ws"
        workspace.mkdir()
        root = tmp_path / "harness-root"
        root.mkdir()
        req = BrainRequest(
            prompt="", allowed_tools=["Read"], cwd=workspace,
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin")},
            timeout_seconds=60, task_id=7, attempt=1,
            harness=HarnessSpec(cli_env={}, harness_root=root),
        )
        with tool_harness(req, brain_label="test brain", on_failure=lambda: None) as handle:
            assert handle.failure is None
            stub = handle.bin_dir / "git"
            assert handle.bin_dir.parent == handle.config_dir.parent
            assert sorted(p.name for p in handle.bin_dir.iterdir()) == ["git"]
            assert stat.S_IMODE(handle.bin_dir.stat().st_mode) == 0o700
            assert stat.S_IMODE(stub.lstat().st_mode) == 0o700
            assert stat.S_ISREG(stub.lstat().st_mode)
            ran = subprocess.run(
                [str(stub), "-C", str(workspace), "ls-files"], cwd=workspace,
                capture_output=True, timeout=10,
            )
        assert ran.returncode == 128
        assert ran.stdout == b"" and ran.stderr == b""


# ============================================================================
# ClaudeCodeBrain on the harness (Stage 7)
# ============================================================================

FAKE_CLI = Path(__file__).parent / "support" / "fake_claude_cli.py"
_SIX = ["Read", "Write", "Edit", "Grep", "Glob", "Bash"]
_WAIT = 30.0


def _flag_values(argv: list[str], flag: str) -> list[str]:
    """The values after ``flag``, up to the next ``--`` flag."""
    values = []
    for value in argv[argv.index(flag) + 1:]:
        if value.startswith("--"):
            break
        values.append(value)
    return values


class TestTheHarnessArgv:
    def _req(self, tools):
        return BrainRequest(
            prompt="", allowed_tools=tools, cwd=Path("/tmp"), env={}, timeout_seconds=60,
        )

    def test_websearch_in_the_grant_is_the_one_builtin(self):
        argv = build_claude_cli_flags(
            self._req(["Read", "Bash", "WebSearch"]), mcp_config_path=Path("/h/mcp.json"),
        )
        assert _flag_values(argv, "--tools") == ["WebSearch"]
        assert _flag_values(argv, "--mcp-config") == ["/h/mcp.json"]
        assert "--strict-mcp-config" in argv
        denied = _flag_values(argv, "--disallowedTools")
        assert denied == ["Agent", "Workflow", *HARNESS_DENIED_TOOLS]
        assert "WebSearch" not in denied
        assert {"Bash", "Read", "Write", "Edit", "Task", "WebFetch"} <= set(denied)

    def test_a_grant_without_websearch_gets_no_builtins(self):
        argv = build_claude_cli_flags(self._req(["Read", "Bash"]), mcp_config_path=Path("/h/m.json"))
        assert argv[argv.index("--tools") + 1] == ""

    def test_a_read_only_grant_takes_the_harness_branch(self):
        argv = build_claude_cli_flags(self._req(["Read", "Grep"]), mcp_config_path=Path("/h/m.json"))
        assert "--mcp-config" in argv
        assert argv[argv.index("--tools") + 1] == ""

    def test_without_the_config_path_the_argv_is_unchanged(self):
        req = self._req(["Read", "Bash", "WebSearch"])
        req.harness = HarnessSpec(cli_env={}, harness_root=Path("/h"))
        plain = build_claude_cli_flags(self._req(["Read", "Bash", "WebSearch"]))
        assert build_claude_cli_flags(req) == plain
        assert "--mcp-config" not in plain

    def test_the_command_streams_whatever_the_request_asked(self):
        req = self._req(["Read"])
        req.streaming = False
        cmd = ClaudeCodeBrain._build_command(req, mcp_config_path=Path("/h/m.json"))
        assert cmd[:3] == ["claude", "-p", "-"]
        assert "--dangerously-skip-permissions" in cmd
        assert _flag_values(cmd, "--output-format") == ["stream-json"]


class TestTheInitFrame:
    def test_a_frame_with_tools_parses(self):
        event = parse_stream_line(json.dumps({
            "type": "system", "subtype": "init", "tools": ["WebSearch", "mcp__istota__Read"],
            "mcp_servers": [{"name": "istota", "status": "connected", "source": "dynamic"}],
        }))
        assert event == InitEvent(
            tools=("WebSearch", "mcp__istota__Read"), mcp_servers=(("istota", "connected"),),
        )

    def test_a_frame_without_tools_stays_nothing(self):
        assert parse_stream_line(json.dumps({"type": "system", "subtype": "init", "cwd": "/"})) is None


class TestTheSeam:
    def test_claude_code_names_only_the_four_harness_names(self):
        """The unified tool server (#862) replaces what is behind these four;
        anything else named here would be a second thing it has to rewrite."""
        tree = ast.parse(source_of(claude_code))
        named: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and (node.module or "").endswith("_tool_harness"):
                named |= {alias.name for alias in node.names}
            elif isinstance(node, ast.Import):
                assert not any(a.name.endswith("_tool_harness") for a in node.names)
            elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                assert node.value.id != "_tool_harness", node.attr
        assert named == {"tool_harness", "HarnessHandle", "set_cli_root", "release_calls"}


class _Harnessed:
    """A harness request for `ClaudeCodeBrain.execute`, with the fake CLI on
    PATH, a workspace, a harness root and the callbacks recorded."""

    def __init__(self, tmp_path, *, plan=None, tools=None, **req_kw):
        self.ws = tmp_path / "ws"
        self.ws.mkdir(exist_ok=True)
        self.root = tmp_path / "harness-root"
        self.root.mkdir(exist_ok=True)
        self.record = tmp_path / "record.json"
        self.plan_path = tmp_path / "plan.json"
        self.plan_path.write_text(json.dumps({"record": str(self.record), **(plan or {})}))
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(exist_ok=True)
        shim = bin_dir / "claude"
        shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLI}" "$@"\n')
        shim.chmod(0o755)
        self.pids: list[int] = []
        self.cancel = threading.Event()
        path = os.environ.get("PATH", "/usr/bin:/bin")
        base = dict(
            prompt="go",
            allowed_tools=list(_SIX if tools is None else tools),
            cwd=self.ws,
            env={"PATH": path, "ISTOTA_DEFERRED_DIR": str(self.ws)},
            timeout_seconds=60,
            task_id=7,
            attempt=1,
            model="claude-sonnet-4-5",
            streaming=False,
            on_pid=self.pids.append,
            cancel_check=self.cancel.is_set,
            harness=HarnessSpec(
                cli_env={
                    "PATH": f"{bin_dir}:{path}",
                    "FAKE_CLAUDE_PLAN": str(self.plan_path),
                    "ISTOTA_CLI_AUTH": "api_key",
                },
                harness_root=self.root,
            ),
        )
        base.update(req_kw)
        self.req = BrainRequest(**base)

    def run(self):
        return ClaudeCodeBrain().execute(self.req)

    def recorded(self) -> dict:
        return json.loads(self.record.read_text())

    def server_pid(self) -> int:
        deadline = time.monotonic() + _WAIT
        while not self.pids and time.monotonic() < deadline:
            time.sleep(0.05)
        return self.pids[0]


class TestTheHarnessRun:
    def test_calls_run_in_the_tool_server_and_the_cli_in_the_harness(self, tmp_path):
        (tmp_path / "ws").mkdir()
        (tmp_path / "ws" / "note.txt").write_text("harness note\n")
        native_wrapped = []

        def native_wrap(cmd):
            native_wrapped.append(cmd)
            return cmd

        h = _Harnessed(tmp_path, plan={"calls": [
            {"name": "Read", "args": {"file_path": str(tmp_path / "ws" / "note.txt")}},
            {"name": "Bash", "args": {"command": "echo ran-$((6*7))", "description": "probe"}},
        ]}, tool_server_wrap=native_wrap)

        result = h.run()

        assert result.success, result.result_text
        # The wrap went round the tool server, and nothing round the CLI.
        assert len(native_wrapped) == 1
        assert "istota.sandbox.tool_server" in " ".join(native_wrapped[0])
        assert "harness note" in result.result_text
        assert "ran-42" in result.result_text
        tools = [e for e in json.loads(result.execution_trace) if e["type"] == "tool"]
        assert tools[-1]["raw"] == "echo ran-$((6*7))"
        assert "mcp__" not in result.execution_trace

        seen = h.recorded()
        assert _flag_values(seen["argv"], "--tools") == [""]
        assert "--strict-mcp-config" in seen["argv"]
        assert Path(seen["cwd"]).resolve() == h.ws.resolve()
        env = seen["env"]
        harness_dir = Path(env["CLAUDE_CONFIG_DIR"]).parent
        assert harness_dir.parent == h.root
        assert env["HOME"] == str(harness_dir / "home")
        assert env["TMPDIR"] == str(harness_dir / "tmp")
        assert env["CLAUDE_CODE_DISABLE_ADVISOR_TOOL"] == "1"
        assert "ISTOTA_DEFERRED_DIR" not in env
        # The tool server's env carries none of the CLI's switches.
        assert "CLAUDE_CODE_DISABLE_ADVISOR_TOOL" not in h.req.env

        # `on_pid` saw the tool server and then its reap, never the CLI.
        assert len(h.pids) == 2 and h.pids[1] == 0
        assert seen["pid"] not in h.pids

    def test_the_stub_git_is_first_on_the_cli_path_and_absent_from_bash(self, tmp_path):
        """The CLI finds the harness's stub `git` first; the model's Bash runs
        in the tool server with the task's own PATH and its real git."""
        h = _Harnessed(tmp_path, plan={"calls": [
            {"name": "Bash", "args": {"command": "echo BASHPATH=$PATH", "description": "probe"}},
        ]})

        result = h.run()

        assert result.success, result.result_text
        env = h.recorded()["env"]
        bin_dir = Path(env["CLAUDE_CONFIG_DIR"]).parent / "bin"
        cli_path = env["PATH"].split(os.pathsep)
        assert cli_path[0] == str(bin_dir)
        assert cli_path[1:] == h.req.harness.cli_env["PATH"].split(os.pathsep)

        bash_path = next(
            line.split("=", 1)[1] for line in result.result_text.splitlines()
            if line.startswith("BASHPATH=")
        )
        assert str(bin_dir) not in bash_path
        assert bash_path == h.req.env["PATH"]
        assert str(bin_dir) not in h.req.env["PATH"]

    def test_an_unexpected_tool_in_init_kills_the_cli_before_any_call(self, tmp_path):
        marker = tmp_path / "ws" / "ran"
        h = _Harnessed(tmp_path, plan={
            "init_extra_tools": ["CronCreate"],
            "calls": [{"name": "Bash", "args": {"command": f"touch {marker}"}}],
        })

        result = h.run()

        assert result.stop_reason == "error"
        assert "unexpected tools ['CronCreate']" in result.result_text
        assert not marker.exists()

    def test_an_mcp_server_that_is_not_connected_fails_the_attempt(self, tmp_path):
        h = _Harnessed(tmp_path, plan={"init_server_status": "failed"})

        result = h.run()

        assert result.stop_reason == "error"
        assert "istota (failed)" in result.result_text

    def test_a_tool_the_cli_dropped_fails_the_attempt(self, tmp_path):
        h = _Harnessed(tmp_path, tools=[*_SIX, "WebSearch"], plan={"init_drop_tools": ["WebSearch"]})

        result = h.run()

        assert result.stop_reason == "error"
        assert "missing tools ['WebSearch']" in result.result_text

    def test_websearch_in_the_grant_passes_the_check(self, tmp_path):
        h = _Harnessed(tmp_path, tools=[*_SIX, "WebSearch"])

        result = h.run()

        assert result.success, result.result_text
        assert _flag_values(h.recorded()["argv"], "--tools") == ["WebSearch"]

    def _web(self, tmp_path, plan=None):
        h = _Harnessed(tmp_path, tools=[*_SIX, "WebFetch"], plan=plan)
        h.req.harness = dataclasses.replace(
            h.req.harness, web_fetch=WebFetchPolicy(block_hosts=("blocked.example",)),
        )
        return h

    def test_webfetch_runs_in_its_own_server(self, tmp_path):
        h = self._web(tmp_path, plan={"calls": [
            {"server": "istota_web", "name": "WebFetch", "args": {"url": "https://blocked.example/x"}},
        ]})

        result = h.run()

        assert result.success, result.result_text
        assert "host blocked.example is blocked" in result.result_text
        tools = [e for e in json.loads(result.execution_trace) if e["type"] == "tool"]
        assert tools and "mcp__" not in result.execution_trace
        # The CLI's own WebFetch stays off; ours is the MCP tool.
        assert _flag_values(h.recorded()["argv"], "--tools") == [""]
        # Only the tool server was reported, then reaped.
        assert len(h.pids) == 2 and h.pids[1] == 0

    def test_a_missing_web_server_fails_the_init_check(self, tmp_path):
        h = self._web(tmp_path, plan={"init_drop_servers": ["istota_web"]})

        result = h.run()

        assert result.stop_reason == "error"
        assert "expected istota (connected), istota_web (connected)" in result.result_text

    def test_a_web_server_that_is_not_connected_fails_the_init_check(self, tmp_path):
        h = self._web(tmp_path, plan={"init_server_status": "failed"})

        result = h.run()

        assert result.stop_reason == "error"
        assert "istota_web (failed)" in result.result_text

    def test_an_extra_server_fails_the_init_check(self, tmp_path):
        h = _Harnessed(tmp_path, plan={"init_extra_servers": ["istota_web"]})

        result = h.run()

        assert result.stop_reason == "error"
        assert "MCP servers" in result.result_text

    def test_a_sandboxed_request_without_a_wrap_is_refused(self, tmp_path):
        h = _Harnessed(tmp_path, fs_read_roots=[tmp_path], tool_server_wrap=None)

        result = h.run()

        assert result.stop_reason == "error"
        assert "refusing to run tools unwrapped" in result.result_text
        assert h.pids == []
        assert not h.record.exists()

    def test_a_tool_grant_with_no_harness_is_refused(self, tmp_path):
        """There is no other path: the old in-sandbox CLI is gone."""
        h = _Harnessed(tmp_path)
        h.req.harness = None

        result = h.run()

        assert result.stop_reason == "error"
        assert "only on the tool harness" in result.result_text
        assert h.pids == []
        assert not h.record.exists()

    def test_a_harness_that_cannot_start_never_spawns_the_cli(self, tmp_path):
        h = _Harnessed(tmp_path)
        h.req.harness = HarnessSpec(cli_env=h.req.harness.cli_env, harness_root=tmp_path / "missing")

        result = h.run()

        assert result.stop_reason == "error"
        assert "could not start the tool harness" in result.result_text
        assert not h.record.exists()


class TestTheFailureOrder:
    """Cancel, then a dead tool server, then timeout, then a signal."""

    def _kill_server_when_running(self, h, *, first=None):
        def go():
            pid = h.server_pid()
            time.sleep(1.0)
            if first is not None:
                first()
            # The CLI spawns it, so it leads no process group of its own.
            os.kill(pid, signal.SIGKILL)

        threading.Thread(target=go, daemon=True).start()

    def test_a_dead_tool_server_reads_as_its_failure_not_oom(self, tmp_path):
        # The CLI outlives its server, as the real one does while the model
        # takes its next turn after a "Connection closed" result.
        h = _Harnessed(tmp_path, plan={
            "calls": [{"name": "Bash", "args": {"command": "sleep 30"}}],
            "sleep_before_result": 30,
        })
        self._kill_server_when_running(h)

        started = time.monotonic()
        result = h.run()

        assert time.monotonic() - started < 25
        # `error`, where the CLI's own SIGKILL from `on_failure` would read as
        # `oom`. The daemon is not the server's parent since #862, so it sees
        # the exit and not the signal; a deploy restart, which kills the CLI
        # too, still reads as the CLI's own signal death.
        assert result.stop_reason == "error", result.result_text
        assert result.result_text == (
            "The Claude Code brain's tool server failed: the tool server exited"
        )
        assert h.pids[-1] == 0

    def test_a_cancel_outranks_the_dead_tool_server(self, tmp_path):
        h = _Harnessed(tmp_path, plan={"calls": [{"name": "Bash", "args": {"command": "sleep 30"}}]})
        self._kill_server_when_running(h, first=h.cancel.set)

        result = h.run()

        assert result.stop_reason == "cancelled"
        assert result.result_text == "Cancelled by user"

    def test_a_timeout_reads_as_a_timeout(self, tmp_path):
        h = _Harnessed(tmp_path, plan={"sleep_before_result": 30}, timeout_seconds=2)

        result = h.run()

        assert result.stop_reason == "timeout"

    def test_a_kill_from_outside_still_reads_as_oom(self, tmp_path):
        h = _Harnessed(tmp_path, plan={"sleep_before_calls": 30})

        def kill_the_cli():
            deadline = time.monotonic() + _WAIT
            while not h.record.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            time.sleep(0.5)
            os.kill(h.recorded()["pid"], signal.SIGKILL)

        threading.Thread(target=kill_the_cli, daemon=True).start()
        result = h.run()

        assert result.stop_reason == "oom", result.result_text
