"""Configuration loading for istota.executor module."""

import logging
import os
import sys
from unittest.mock import patch, MagicMock

import pytest

from istota.executor import (
    _compose_full_result,
    _resolve_user_tz,
    _is_automated_task,
    _is_terse,
    _last_substantial_region,
    _NO_FINAL_ANSWER_NOTICE,
    _TERSE_RESULT_MAX_CHARS,
    detect_malformed_result,
    parse_api_error,
    is_transient_api_error,
    build_prompt,
    load_persona,
    load_emissaries,
    _pre_transcribe_attachments,
    _detect_notification_reply,
    _apply_recency_window_talk,
    _apply_recency_window_db,
    _AUDIO_EXTENSIONS,
    _PRE_TRANSCRIBE_TOTAL_TIMEOUT_SECONDS,
    API_RETRY_MAX_ATTEMPTS,
    API_RETRY_DELAY_SECONDS,
    TRANSIENT_STATUS_CODES,
)
from istota.sandbox import credential_shim
from istota import db as _db
from istota import executor
from istota.skills import developer as developer_skill
from tests.support.drift import source_of
from istota.brain import BrainRequest, ClaudeCodeBrain
from istota.brain import claude_code
from tests.support.monotonic_spy import monotonic_spy
from tests.support.sleep_spy import sleep_spy
from istota.brain._types import BrainResult
from istota.sandbox.host_paths import path_under_roots, workspace_roots
import json
from pathlib import Path

from istota.config import Config, DeveloperConfig, EmailConfig as AppEmailConfig, NextcloudConfig, SecurityConfig, SiteConfig, UserConfig
from istota import db


def _system_half(config, user_id="alice", task_id=1) -> str:
    """The standing instructions `execute_task` wrote for this task.

    Since the prompt split, `input=` on the CLI subprocess carries the *user*
    half alone — the request, retrieved memory and conversation history. Skill
    bodies, the skills changelog and the workspace vocabulary are standing
    instructions and travel as `system_prompt.txt` in the task's control
    directory, which the brain passes with `--append-system-prompt-file`. A
    test asserting on one of those reads this file, and a *negative* assertion
    about one has to read it or it passes for the wrong reason.
    """
    from istota.executor import get_task_control_dir

    return (
        get_task_control_dir(config, user_id, task_id) / "system_prompt.txt"
    ).read_text(encoding="utf-8")



_FILES_INDEX = '[files]\ndescription = "File ops"\nalways_include = true\n'


def _skills_config(tmp_path, *, files_skill=True, mount=False, **kw):
    """A Config over a fresh DB and a project skills dir under tmp_path.

    `bundled_skills_dir` defaults to an empty directory; pass `None` to load
    the real bundled skills, whose manifests some env-var tests depend on.
    """
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    skills_dir = tmp_path / "config" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    if files_skill:
        (skills_dir / "_index.toml").write_text(_FILES_INDEX)
        (skills_dir / "files.md").write_text("File operations guide.")
    if mount:
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True, exist_ok=True)
        kw.setdefault("workspace_path", mount_path)
    kw.setdefault("bundled_skills_dir", tmp_path / "_empty_bundled")
    return Config(
        db_path=db_path, skills_dir=skills_dir, temp_dir=tmp_path / "temp", **kw,
    )


def _bare_config(tmp_path):
    db_path = tmp_path / "test.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        skills_dir=tmp_path / "_empty_skills",
        bundled_skills_dir=tmp_path / "_empty_bundled",
        temp_dir=tmp_path / "temp",
        security=SecurityConfig(sandbox_enabled=False, skill_proxy_enabled=False),
    )


def _execute(
    config, mock_run, *, user_id="alice", source_type="talk", prompt="test",
    failed=False, **task_kw,
):
    """Create a task and run it through `execute_task` against a faked CLI."""
    (config.temp_dir / user_id).mkdir(parents=True, exist_ok=True)
    if failed:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")
    else:
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(
            conn, prompt=prompt, user_id=user_id, source_type=source_type, **task_kw,
        )
        task = db.get_task(conn, task_id)
        # Release the writer lock so a secrets read can bump last_accessed_at.
        conn.commit()
        return executor.execute_task(task, config, [], conn=conn)


def _env(mock_run):
    return mock_run.call_args[1]["env"]


def _api_error(code, err_type="api_error", message="Internal server error"):
    return (
        f'API Error: {code} {{"type":"error","error":{{"type":"{err_type}",'
        f'"message":"{message}"}}}}'
    )


# ---------------------------------------------------------------------------
# TestParseApiError
# ---------------------------------------------------------------------------


class TestParseApiError:
    @pytest.mark.parametrize("error_text, expected", [
        (
            'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"},"request_id":"req_abc123"}',
            {"status_code": 500, "message": "Internal server error", "request_id": "req_abc123"},
        ),
        (
            'API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"},"request_id":"req_xyz"}',
            {"status_code": 429, "message": "Rate limit exceeded", "request_id": "req_xyz"},
        ),
        (
            'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"Invalid API key"},"request_id":"req_auth"}',
            {"status_code": 401, "message": "Invalid API key"},
        ),
        (
            'Some prefix text before API Error: 503 {"type":"error","error":{"type":"overloaded_error","message":"Service overloaded"}}',
            {"status_code": 503, "message": "Service overloaded"},
        ),
        # Malformed JSON with closing brace but invalid content.
        (
            'API Error: 500 {broken json}',
            {"status_code": 500, "message": "Unknown error", "request_id": None},
        ),
        # The JSON pattern needs a closing brace, so this used to parse as "not
        # an API error at all" — which meant a truncated 500 was never retried
        # and never reached the fallback (ISSUE-212). A 500 is a 500; only the
        # message is lost.
        ('API Error: 500 {broken json', {"status_code": 500, "request_id": None}),
        (
            'API Error: 500 {"type":"error","request_id":"req_123"}',
            {"status_code": 500, "message": "Unknown error", "request_id": "req_123"},
        ),
    ], ids=[
        "500", "429", "401", "prefix_text", "malformed_json", "unclosed_json",
        "missing_error_field",
    ])
    def test_parses(self, error_text, expected):
        result = parse_api_error(error_text)
        assert result is not None
        for key, value in expected.items():
            assert result[key] == value, key

    @pytest.mark.parametrize("text", [
        "Claude Code was killed (likely out of memory)",
        "Task completed successfully",
    ])
    def test_returns_none_for_non_api_text(self, text):
        assert parse_api_error(text) is None


# ---------------------------------------------------------------------------
# TestIsTransientApiError
# ---------------------------------------------------------------------------


class TestIsTransientApiError:
    @pytest.mark.parametrize("code, err_type, expected", [
        (500, "api_error", True),
        (502, "api_error", True),
        (503, "api_error", True),
        (504, "api_error", True),
        (529, "overloaded_error", True),
        (429, "rate_limit_error", True),
        (401, "authentication_error", False),
        (403, "permission_error", False),
        (400, "invalid_request_error", False),
    ])
    def test_status_code(self, code, err_type, expected):
        assert is_transient_api_error(_api_error(code, err_type, "x")) is expected

    def test_non_api_error_is_not_transient(self):
        assert is_transient_api_error("Claude Code was killed (likely out of memory)") is False
        assert is_transient_api_error("Task execution timed out") is False
        assert is_transient_api_error("Cancelled by user") is False


# ---------------------------------------------------------------------------
# TestTransientStatusCodes
# ---------------------------------------------------------------------------


class TestTransientStatusCodes:
    def test_includes_server_errors_and_anthropic_overloaded(self):
        for code in (500, 502, 503, 504, 529):
            assert code in TRANSIENT_STATUS_CODES, code

    def test_excludes_client_errors(self):
        for code in (400, 401, 403, 404):
            assert code not in TRANSIENT_STATUS_CODES, code


# ---------------------------------------------------------------------------
# TestRetryConfiguration
# ---------------------------------------------------------------------------


class TestRetryConfiguration:
    def test_max_attempts_is_reasonable(self):
        assert API_RETRY_MAX_ATTEMPTS >= 2
        assert API_RETRY_MAX_ATTEMPTS <= 5

    def test_delay_is_reasonable(self):
        assert API_RETRY_DELAY_SECONDS >= 3
        assert API_RETRY_DELAY_SECONDS <= 30


# ---------------------------------------------------------------------------
# TestExecuteStreamingRetry
# ---------------------------------------------------------------------------


_STREAMING_ONCE = "istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once"


class TestExecuteStreamingRetry:
    """Retry logic for transient API errors lives in ClaudeCodeBrain.

    Tests use the static _execute_streaming_once method as the mock target
    and drive the public _execute_streaming wrapper, which is the same
    layering the executor used to have.
    """

    def _run(self, tmp_path) -> BrainResult:
        request = BrainRequest(
            prompt="test",
            allowed_tools=["Bash"],
            cwd=tmp_path,
            env={},
            timeout_seconds=60,
            streaming=True,
            result_file=tmp_path / "result.txt",
        )
        return ClaudeCodeBrain()._execute_streaming([], request)

    @patch(_STREAMING_ONCE)
    def test_retries_on_transient_error(self, mock_exec_once, tmp_path, monkeypatch):
        """Should retry on transient 500 errors before giving up."""
        slept = sleep_spy(monkeypatch, claude_code)
        mock_exec_once.side_effect = [
            BrainResult(False, _api_error(500), stop_reason="error"),
            BrainResult(True, "Success after retry"),
        ]

        result = self._run(tmp_path)

        assert result.success is True
        assert result.result_text == "Success after retry"
        assert mock_exec_once.call_count == 2
        # The backoff is slept in slices so a `!stop` lands during it rather
        # than after (ISSUE-212 — the wait can now be a provider-supplied
        # Retry-After, not just the fixed 5s). The total is the contract.
        assert sum(slept) == pytest.approx(API_RETRY_DELAY_SECONDS)

    @pytest.mark.parametrize("text, stop_reason, needle", [
        (_api_error(401, "authentication_error", "Invalid API key"), "error", "401"),
        ("Claude Code was killed (likely out of memory)", "oom", "out of memory"),
    ], ids=["permanent_401", "non_api_oom"])
    @patch(_STREAMING_ONCE)
    def test_no_retry(
        self, mock_exec_once, tmp_path, monkeypatch, text, stop_reason, needle,
    ):
        slept = sleep_spy(monkeypatch, claude_code)
        mock_exec_once.return_value = BrainResult(False, text, stop_reason=stop_reason)

        result = self._run(tmp_path)

        assert result.success is False
        assert needle in result.result_text
        assert mock_exec_once.call_count == 1
        assert slept == []

    @patch(_STREAMING_ONCE)
    def test_gives_up_after_max_retries(self, mock_exec_once, tmp_path, monkeypatch):
        slept = sleep_spy(monkeypatch, claude_code)
        mock_exec_once.return_value = BrainResult(
            False, _api_error(500), stop_reason="error",
        )

        result = self._run(tmp_path)

        assert result.success is False
        assert "500" in result.result_text
        assert mock_exec_once.call_count == API_RETRY_MAX_ATTEMPTS
        assert sum(slept) == pytest.approx(
            API_RETRY_DELAY_SECONDS * (API_RETRY_MAX_ATTEMPTS - 1)
        )

    @patch(_STREAMING_ONCE)
    def test_first_try_success_passes_actions_through(self, mock_exec_once, tmp_path):
        actions = '["📄 Reading file.py", "✏️ Editing file.py"]'
        mock_exec_once.return_value = BrainResult(
            True, "Done", actions_taken=actions, execution_trace='[]',
        )

        result = self._run(tmp_path)

        assert result.success is True
        assert result.result_text == "Done"
        assert result.actions_taken == actions
        assert mock_exec_once.call_count == 1

    @patch(_STREAMING_ONCE)
    def test_actions_taken_from_successful_retry(
        self, mock_exec_once, tmp_path, monkeypatch,
    ):
        """On retry, should use actions_taken from the successful attempt."""
        sleep_spy(monkeypatch, claude_code, record=False)
        actions = '["📄 Reading config"]'
        mock_exec_once.side_effect = [
            BrainResult(False, _api_error(500, message="err"), stop_reason="error"),
            BrainResult(True, "ok", actions_taken=actions),
        ]

        result = self._run(tmp_path)

        assert result.success is True
        assert result.actions_taken == actions


# ---------------------------------------------------------------------------
# TestBuildPromptSkillsChangelog
# ---------------------------------------------------------------------------


class TestBuildPromptSkillsChangelog:
    def _prompt(self, tmp_path, **kw):
        config = _skills_config(tmp_path, files_skill=False)
        task = db.Task(
            id=1, status="running", source_type="talk", user_id="alice",
            prompt="hello", conversation_token="room1",
        )
        return build_prompt(task, [], config, **kw).system

    def test_changelog_included_when_provided(self, tmp_path):
        prompt = self._prompt(
            tmp_path, skills_changelog="## 2026-02-08\n- New feature added",
        )
        assert "## What's New in Skills" in prompt
        assert "New feature added" in prompt

    def test_changelog_not_included_when_none(self, tmp_path):
        prompt = self._prompt(tmp_path, skills_changelog=None)
        assert "What's New in Skills" not in prompt

    def test_changelog_appears_before_skills_doc(self, tmp_path):
        prompt = self._prompt(
            tmp_path,
            skills_doc="## Skills Reference (v: abc123)\n\n### Files\n\nFile ops.",
            skills_changelog="## 2026-02-08\n- Updated files skill",
        )
        assert prompt.index("What's New in Skills") < prompt.index("Skills Reference")


# ---------------------------------------------------------------------------
# TestResolveUserTz (ISSUE-099)
# ---------------------------------------------------------------------------


class TestResolveUserTz:
    """`_resolve_user_tz` must reflect live web-UI timezone edits.

    The web UI writes timezone to the ``user_profiles`` DB row, but the
    scheduler's in-memory ``Config`` is only built once at startup. Reading
    the timezone from the DB (with the in-memory ``UserConfig`` as fallback)
    means a travelling user's timezone change takes effect on the next task
    without a daemon restart. Mirrors the ``Config.is_module_enabled`` pattern.
    """

    def _make_config(self, tmp_path, *, user_tz="America/Los_Angeles"):
        db_path = tmp_path / "test.db"
        _db.init_db(db_path)
        return Config(
            db_path=db_path,
            temp_dir=tmp_path / "temp",
            users={"alice": UserConfig(timezone=user_tz)},
        )

    def test_db_profile_wins_over_stale_in_memory_config(self, tmp_path):
        from istota import user_profiles
        config = self._make_config(tmp_path, user_tz="America/Los_Angeles")
        # Simulate a web-UI timezone change written to the DB after startup.
        user_profiles.ensure_profile(config.db_path, "alice", timezone="Europe/Lisbon")

        tz, tz_str = _resolve_user_tz(config, "alice")
        assert tz_str == "Europe/Lisbon"
        assert tz.key == "Europe/Lisbon"

    def test_falls_back_to_in_memory_config_when_no_db_row(self, tmp_path):
        config = self._make_config(tmp_path, user_tz="America/New_York")
        tz, tz_str = _resolve_user_tz(config, "alice")
        assert tz_str == "America/New_York"

    def test_falls_back_to_utc_for_unknown_user(self, tmp_path):
        config = self._make_config(tmp_path)
        tz, tz_str = _resolve_user_tz(config, "nobody")
        assert tz_str == "UTC"

    def test_invalid_db_timezone_falls_back_to_utc(self, tmp_path):
        from istota import user_profiles
        config = self._make_config(tmp_path)
        user_profiles.ensure_profile(config.db_path, "alice", timezone="Not/AZone")
        tz, tz_str = _resolve_user_tz(config, "alice")
        assert tz_str == "UTC"

    def test_invalid_timezone_warns_once(self, tmp_path, caplog):
        from istota import user_profiles

        config = self._make_config(tmp_path)
        # "PDT" is an abbreviation, not an IANA name — the real-world bug.
        user_profiles.ensure_profile(config.db_path, "alice", timezone="PDT")
        executor._INVALID_TZ_WARNED.discard(("alice", "PDT"))

        with caplog.at_level(logging.WARNING, logger="istota.executor"):
            _resolve_user_tz(config, "alice")
            _resolve_user_tz(config, "alice")  # second call must not re-warn

        warnings = [
            r for r in caplog.records if "Invalid timezone" in r.getMessage()
        ]
        # Deduped: exactly one WARNING for the (user, tz) pair.
        assert len(warnings) == 1
        assert "PDT" in warnings[0].getMessage()
        assert "America/Los_Angeles" in warnings[0].getMessage()

    def test_no_db_path_uses_in_memory_config(self, tmp_path):
        # DB-less contexts (init/tests) must still resolve via UserConfig.
        config = Config(
            db_path=None,
            temp_dir=tmp_path / "temp",
            users={"alice": UserConfig(timezone="Asia/Tokyo")},
        )
        tz, tz_str = _resolve_user_tz(config, "alice")
        assert tz_str == "Asia/Tokyo"


# ---------------------------------------------------------------------------
# TestSkillsFingerprintIntegration
# ---------------------------------------------------------------------------


@patch("istota.executor.subprocess.run")
class TestSkillsFingerprintIntegration:
    def _make_config(self, tmp_path, changelog=True):
        config = _skills_config(tmp_path)
        if changelog:
            (config.skills_dir / "CHANGELOG.md").write_text("## v1\n- New feature")
        return config

    def _current_fingerprint(self, config):
        from istota.skills._loader import compute_skills_fingerprint
        return compute_skills_fingerprint(
            config.skills_dir, bundled_dir=config.bundled_skills_dir,
        )

    def _stored_fingerprint(self, config):
        with db.get_db(config.db_path) as conn:
            return db.get_user_skills_fingerprint(conn, "alice")

    def test_changelog_included_when_fingerprint_changed(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        _execute(config, mock_run)
        # The changelog is a standing instruction, so it is in the system
        # half — the file, not stdin.
        assert "What's New in Skills" in _system_half(config)

    def test_changelog_not_included_when_fingerprint_matches(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        with db.get_db(config.db_path) as conn:
            db.set_user_skills_fingerprint(
                conn, "alice", self._current_fingerprint(config),
            )
        _execute(config, mock_run)
        assert "What's New in Skills" not in _system_half(config)

    @pytest.mark.parametrize("source_type", ["briefing", "scheduled"])
    def test_changelog_not_included_for_automated(
        self, mock_run, tmp_path, source_type,
    ):
        config = self._make_config(tmp_path)
        _execute(config, mock_run, source_type=source_type)
        assert "What's New in Skills" not in _system_half(config)

    def test_fingerprint_updated_after_success(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, changelog=False)
        success, *_ = _execute(config, mock_run)
        assert success is True
        assert self._stored_fingerprint(config) == self._current_fingerprint(config)

    @pytest.mark.parametrize("source_type, failed", [
        ("scheduled", False), ("talk", True),
    ], ids=["non_interactive", "failure"])
    def test_fingerprint_not_updated(self, mock_run, tmp_path, source_type, failed):
        config = self._make_config(tmp_path, changelog=False)
        success, *_ = _execute(
            config, mock_run, source_type=source_type, failed=failed,
        )
        assert success is (not failed)
        assert self._stored_fingerprint(config) is None


# ---------------------------------------------------------------------------
# TestDeveloperEnvVars
# ---------------------------------------------------------------------------

class TestDeveloperEnvVars:
    """The developer skill's setup_env hook.

    The hook used to generate `gitlab-api` / `github-api` shell scripts whose
    bodies were a case statement built from an endpoint allowlist. Those are
    gone: the model drives the real `gh` and `glab` through the wrapper in
    src/istota/sandbox/forge_cli.py. What is asserted here is what the hook installs
    and what it hands back, not the contents of a generated script.
    """

    def _make_config(
        self, tmp_path, developer_enabled=True, github=False,
        skill_proxy_enabled=False, **dev_kw,
    ):
        kw = dict(
            enabled=developer_enabled,
            # Under tmp_path, never a real host path: the hook creates
            # `{repos_dir}/{user_id}` and sweeps it, so a literal `/srv/repos`
            # here would have the suite writing outside its own directory.
            repos_dir=str(tmp_path / "repos"),
            gitlab_url="https://gitlab.example.com",
            gitlab_token="glpat-test",
            gitlab_username="istotabot",
            gitlab_default_namespace="example",
        )
        if github:
            kw.update(
                github_url="https://github.com",
                github_token="ghp-test",
                github_username="istotabot",
            )
        kw.update(dev_kw)
        # A test may build two configs from one tmp_path to compare how the
        # hook behaves across settings; `_skills_config` tolerates that.
        return _skills_config(
            tmp_path,
            bundled_skills_dir=None,
            developer=DeveloperConfig(**kw),
            security=SecurityConfig(skill_proxy_enabled=skill_proxy_enabled),
        )

    def _hook_env(self, config, tmp_path):
        from istota.skills.developer import setup_env

        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True, exist_ok=True)

        class _Ctx:
            pass

        ctx = _Ctx()
        ctx.config = config
        ctx.user_temp_dir = str(user_temp)
        # The hook creates and scrubs the repos subtree only for an admin,
        # matching the bind's own gate.
        ctx.is_admin = True
        # The hook reads the task's user id to name the repos subtree it
        # creates and sweeps; without one it takes the fail-closed branch and
        # every assertion below would be made against a hook that did half its
        # work. The exec socket path is derived from the same user id, so this
        # one task is what both halves of the hook read.
        ctx.task = db.Task(
            id=1, prompt="test", user_id="alice",
            source_type="talk", status="running", conversation_token="",
        )
        return setup_env(ctx), user_temp

    def _policy(self, user_temp):
        return json.loads(
            (user_temp / ".developer" / "forge-policy.json").read_text()
        )

    def _helper_body(self, user_temp):
        return (user_temp / ".developer" / "git-credential-helper").read_text()

    def test_disabled_developer_returns_nothing(self, tmp_path):
        config = self._make_config(tmp_path, developer_enabled=False)
        env, _ = self._hook_env(config, tmp_path)
        assert env == {}

    def test_git_credential_helper_written(self, tmp_path):
        config = self._make_config(tmp_path)
        env, user_temp = self._hook_env(config, tmp_path)
        helper = user_temp / ".developer" / "git-credential-helper"
        assert helper.exists()
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert env["GIT_CONFIG_KEY_0"] == "credential.https://gitlab.example.com.helper"

    def test_forge_wrappers_installed_under_every_name(self, tmp_path):
        config = self._make_config(tmp_path)
        _, user_temp = self._hook_env(config, tmp_path)
        dev_bin = user_temp / ".developer"
        canonical = (
            Path(__file__).resolve().parent.parent / "src/istota/sandbox/forge_cli.py"
        ).read_bytes()
        for name in ("gh", "glab", "github-api", "gitlab-api"):
            installed = dev_bin / name
            assert installed.exists(), name
            assert installed.read_bytes() == canonical, name
            assert installed.stat().st_mode & 0o777 == 0o700, name

    def test_path_prepend_is_the_only_env_var_the_wrapper_needs(self, tmp_path):
        """Everything else travels in the policy file. The wrapper runs as a
        child of the model's shell, so an env-supplied path is a path the model
        chooses — an ISTOTA_FORGE_POLICY pointing at a toothless file would be
        a one-token bypass of the whole rule set."""
        config = self._make_config(tmp_path, github=True)
        env, user_temp = self._hook_env(config, tmp_path)
        assert env["ISTOTA_PATH_PREPEND"] == str(user_temp / ".developer")
        for retired in (
            "ISTOTA_FORGE_POLICY", "ISTOTA_GH_CONFIG_DIR",
            "ISTOTA_GLAB_CONFIG_DIR", "ISTOTA_GH_URL", "ISTOTA_GITLAB_URL",
            "ISTOTA_GH_REAL", "ISTOTA_GLAB_REAL", "ISTOTA_FORGE_STATE_DIR",
            # The retired API-command vars.
            "GITLAB_API_CMD", "GITHUB_API_CMD",
        ):
            assert retired not in env, retired
        # No token value appears in the returned env.
        joined = " ".join(env.values())
        assert "glpat-test" not in joined
        assert "ghp-test" not in joined

    def test_policy_carries_the_settings_the_wrapper_must_not_trust(self, tmp_path):
        config = self._make_config(
            tmp_path, github=True, gh_bin_path="/opt/gh", glab_bin_path="/opt/glab",
        )
        _, user_temp = self._hook_env(config, tmp_path)
        dev_bin = user_temp / ".developer"
        policy = self._policy(user_temp)
        gh = policy["github"]
        assert gh["real_bin"] == "/opt/gh"
        assert gh["url"] == "https://github.com"
        assert gh["config_dir"] == str(dev_bin / "github-config")
        assert gh["data_dir"] == str(dev_bin / "github-data")
        assert Path(gh["state_dir"]).is_dir()
        assert policy["gitlab"]["real_bin"] == "/opt/glab"
        assert policy["gitlab"]["url"] == "https://gitlab.example.com"

    def test_unconfigured_bin_path_resolves_from_the_daemon_path(self, tmp_path, monkeypatch):
        """The binary and the config key ship separately: Ansible installs gh
        into /usr/bin, but only a full play run rewrites config.toml, and the
        auto-update cron pulls code without running Ansible. In that window the
        key is absent, the code default stands, and nothing exists at it."""
        import shutil as _shutil

        from istota.skills import developer as _dev

        monkeypatch.setattr(_dev.os.path, "exists", lambda p: False)
        monkeypatch.setattr(
            _shutil, "which", lambda name: f"/usr/bin/{name}",
        )
        config = self._make_config(tmp_path, github=True)
        _, user_temp = self._hook_env(config, tmp_path)
        policy = self._policy(user_temp)
        assert policy["github"]["real_bin"] == "/usr/bin/gh"
        assert policy["gitlab"]["real_bin"] == "/usr/bin/glab"

    def test_explicit_bin_path_is_never_second_guessed(self, tmp_path, monkeypatch):
        """An operator who named a path gets that path even when it is missing.
        Silently exec'ing a different binary found on PATH is the wrong
        surprise; the start-up warning is how a bad path gets reported."""
        import shutil as _shutil

        monkeypatch.setattr(_shutil, "which", lambda name: f"/usr/bin/{name}")
        config = self._make_config(
            tmp_path, github=True,
            gh_bin_path="/opt/nonexistent/gh", glab_bin_path="/opt/nonexistent/glab",
        )
        _, user_temp = self._hook_env(config, tmp_path)
        policy = self._policy(user_temp)
        assert policy["github"]["real_bin"] == "/opt/nonexistent/gh"
        assert policy["gitlab"]["real_bin"] == "/opt/nonexistent/glab"

    def test_policy_grants_direct_tokens_only_with_the_proxy_off(self, tmp_path):
        """The permission lives in the policy file because that is the one
        input the model cannot redirect. An env flag would let it opt itself
        into reading whatever token it had planted."""
        _, user_temp = self._hook_env(
            self._make_config(tmp_path, skill_proxy_enabled=False), tmp_path,
        )
        assert self._policy(user_temp)["github"]["direct_token"] is True

        _, user_temp2 = self._hook_env(
            self._make_config(tmp_path, skill_proxy_enabled=True), tmp_path,
        )
        assert self._policy(user_temp2)["github"]["direct_token"] is False

    def test_data_dir_is_pinned_and_empty(self, tmp_path):
        """gh execs gh-<name> from $XDG_DATA_HOME/gh/extensions for an unknown
        first argument, which no argv rule can see."""
        config = self._make_config(tmp_path)
        _, user_temp = self._hook_env(config, tmp_path)
        data = Path(self._policy(user_temp)["github"]["data_dir"])
        assert data.is_dir()
        assert list(data.iterdir()) == []

    @pytest.mark.parametrize("dev_kw, denied, allowed", [
        ({}, ["repo", "delete", "x"], ["pr", "create"]),
        (
            {"forge_cli_extra_denied": ["gh pr merge"], "forge_cli_permit": ["gh repo delete"]},
            ["pr", "merge", "1"], ["repo", "delete", "x"],
        ),
    ], ids=["baseline", "operator_knobs"])
    def test_policy_file_is_loadable_and_carries_the_rules(
        self, tmp_path, dev_kw, denied, allowed,
    ):
        from istota.sandbox.forge_cli import FORGE_GITHUB, denied_reason, load_policy

        config = self._make_config(tmp_path, **dev_kw)
        _, user_temp = self._hook_env(config, tmp_path)
        policy = load_policy(
            str(user_temp / ".developer" / "forge-policy.json"), FORGE_GITHUB,
        )
        assert denied_reason(FORGE_GITHUB, denied, policy)
        assert denied_reason(FORGE_GITHUB, allowed, policy) is None

    def test_cli_config_dirs_seeded_at_the_mode_glab_demands(self, tmp_path):
        """glab refuses any mode but 0600; gh accepts either. Measured against
        glab 1.114 — see the integration tests in test_forge_cli_exec.py."""
        config = self._make_config(tmp_path)
        _, user_temp = self._hook_env(config, tmp_path)
        policy = self._policy(user_temp)
        for forge in ("github", "gitlab"):
            config_yml = Path(policy[forge]["config_dir"]) / "config.yml"
            assert config_yml.exists(), forge
            assert config_yml.stat().st_mode & 0o777 == 0o600, forge

    def test_no_token_means_no_forge_wrappers(self, tmp_path):
        config = self._make_config(tmp_path, gitlab_token="", github_token="")
        env, user_temp = self._hook_env(config, tmp_path)
        assert "ISTOTA_PATH_PREPEND" not in env
        assert not (user_temp / ".developer" / "gh").exists()

    def test_the_git_helper_calls_the_framework_shim(self, tmp_path):
        """The proxy branch of setup_env. With the proxy on, the helper must
        not hold the token itself — it shells out to the framework credential
        shim's `env` verb, which asks the proxy for it at call time.

        The shim itself is written by `task_env`, not by this hook, and it is
        written *after* this hook runs; the path is built from the same rule
        both sides read.
        """
        config = self._make_config(tmp_path, skill_proxy_enabled=True)
        _, user_temp = self._hook_env(config, tmp_path)
        shim = credential_shim.shim_path(user_temp)
        body = self._helper_body(user_temp)
        assert f'echo password="$({shim} env GITLAB_TOKEN)"' in body
        assert "glpat-test" not in body

    def test_the_hook_no_longer_generates_a_socket_client(self, tmp_path):
        """Two socket clients for one protocol is the duplication this removed.

        Source assertion as well as a filesystem one: the generated program was
        a string literal inside `setup_env`, so a copy reintroduced there would
        pass the `exists()` half on a fresh temp dir.
        """
        config = self._make_config(tmp_path, skill_proxy_enabled=True)
        _, user_temp = self._hook_env(config, tmp_path)
        assert not (user_temp / ".developer" / "credential-fetch").exists()
        source = source_of(developer_skill.setup_env)
        assert "socket.AF_UNIX" not in source

    def test_a_stale_credential_fetch_is_removed(self, tmp_path):
        """`user_temp_dir` persists across tasks, so a copy written before this
        change would otherwise stay reachable on the model's PATH for the life
        of the deployment."""
        config = self._make_config(tmp_path, skill_proxy_enabled=True)
        user_temp = tmp_path / "temp" / "alice"
        (user_temp / ".developer").mkdir(parents=True, exist_ok=True)
        stale = user_temp / ".developer" / "credential-fetch"
        stale.write_text("#!/bin/sh\necho leftover\n")

        self._hook_env(config, tmp_path)

        assert not stale.exists()

    def test_the_helper_reads_the_variable_directly_when_the_proxy_is_off(
        self, tmp_path,
    ):
        """git wants the value verbatim; an unquoted expansion is word-split
        by sh and rejoined by echo on single spaces, so it is quoted."""
        config = self._make_config(tmp_path, skill_proxy_enabled=False)
        _, user_temp = self._hook_env(config, tmp_path)
        body = self._helper_body(user_temp)
        assert 'echo password="$GITLAB_TOKEN"' in body
        assert "istota-credential" not in body

    def test_seeded_config_is_truncated_every_run(self, tmp_path):
        """user_temp_dir persists across tasks. gh expands aliases from
        config.yml before dispatch, so a file that survived one run would be
        honoured by every later one."""
        config = self._make_config(tmp_path)
        _, user_temp = self._hook_env(config, tmp_path)
        cfg = Path(self._policy(user_temp)["github"]["config_dir"]) / "config.yml"
        cfg.write_text("aliases:\n    pwn: repo delete\n")
        self._hook_env(config, tmp_path)
        assert cfg.read_text() == ""


class TestPlainHttpGitlabReachesTheConfiguredHost:
    """A `http://` gitlab_url has to survive into glab's own config file.

    glab discards the scheme inside `GITLAB_HOST` and keeps the port, so a
    deployment configured against `http://gitlab.internal:8080` has every call
    fail with "tls: first record does not look like a TLS handshake" — measured
    on glab 1.114.0, the version the image pins. The only lever glab offers is a
    per-host `api_protocol` in its config file, and that file is the one
    `_seed_cli_config_dir` truncates on every task, so the entry has to be
    written by the same code that empties it.

    Kept as its own class because the property is about `_seed_cli_config_dir`
    rather than about the returned env, and because the truncation invariant it
    sits next to is the thing most likely to be broken by a later edit here.
    """

    def _seed(self, tmp_path, url, forge="gitlab", name=None):
        from istota.skills.developer import _seed_cli_config_dir

        target = _seed_cli_config_dir(
            tmp_path, name or f"{forge}-config", forge=forge, forge_url=url
        )
        return (target / "config.yml").read_text()

    @pytest.mark.parametrize("url, forge, name", [
        # The entry is glab's, and gh must not receive it. gh refuses a scheme
        # in `GH_HOST` outright, so there is nothing the entry could fix for it
        # (the port half is `forge_cli._gh_host`'s, ISSUE-279). Worse, gh
        # *reads* a `hosts:` block and runs its multi-account migration, writing
        # a `hosts.yml` beside the config that nothing truncates — in a
        # directory whose whole design is that nothing survives a task.
        ("http://ghe.internal:8080", "github", None),
        # The rule lives in the function, not in the directory name the caller
        # passed: a later refactor is free to change the name without reading
        # this.
        ("http://ghe.internal:8080", "github", "confusingly-named"),
        # The overwhelmingly common case must not grow a config surface:
        # anything written here is honoured by glab before dispatch.
        ("https://gitlab.example.com", "gitlab", None),
        ("", "gitlab", None),
        # The one case where making it work would be worse than leaving it
        # broken. Measured on glab 1.114.0: its lookup key includes the
        # userinfo, so a matching entry would have to carry the password into
        # `config.yml`, which is bound *readable* into the sandbox. The token
        # belongs in `gitlab_token`; `developer.forge_transport` says why the
        # call fails.
        ("http://user:s3cr3t-value@gitlab.internal:8080", "gitlab", None),
    ], ids=["gh_plain_http", "forge_not_dir_name", "https", "no_url", "password_in_url"])
    def test_seeds_an_empty_file(self, tmp_path, url, forge, name):
        assert self._seed(tmp_path, url, forge=forge, name=name) == ""

    def test_plain_http_writes_the_protocol_for_that_host_only(self, tmp_path):
        body = self._seed(tmp_path, "http://127.0.0.1:18080")

        assert "api_protocol: http" in body, body
        # The host key carries the port. glab looks the entry up by the netloc
        # it derived from GITLAB_HOST, so an entry filed under the bare
        # hostname is never consulted and the call still forces https.
        assert "127.0.0.1:18080" in body, body

    def test_the_entry_is_valid_yaml_shaped_the_way_glab_reads_it(self, tmp_path):
        """Parsed, not pattern-matched.

        The host key contains a colon, which is exactly the shape that turns an
        unquoted YAML mapping key into something a parser reads differently
        from how it was meant. Asserting on substrings alone would pass on a
        file glab cannot load.
        """
        yaml = pytest.importorskip("yaml")
        parsed = yaml.safe_load(self._seed(tmp_path, "http://gitlab.internal:8080"))

        assert parsed["hosts"]["gitlab.internal:8080"] == {
            "api_protocol": "http",
            "api_host": "gitlab.internal:8080",
        }, parsed

    @pytest.mark.parametrize("url, key", [
        ("http://gitlab.internal", "gitlab.internal"),
        # glab looks the entry up by a lowercased key. Measured on 1.114.0:
        # the key written verbatim finds nothing and forces https.
        ("http://GitLab.Internal:8080", "gitlab.internal:8080"),
        # `build_invocation` puts the *whole* URL in GITLAB_HOST — a subpath
        # install is a supported shape (`tests/test_forge_cli.py::
        # test_gitlab_host_keeps_port_and_subpath`), and glab's lookup key
        # carries the path.
        ("http://forge.internal/gitlab", "forge.internal/gitlab"),
        ("http://forge.internal/", "forge.internal"),
    ], ids=["default_port_bare_host", "uppercase_lowered", "subpath_kept", "trailing_slash_dropped"])
    def test_the_host_key(self, tmp_path, url, key):
        yaml = pytest.importorskip("yaml")
        parsed = yaml.safe_load(self._seed(tmp_path, url))

        assert set(parsed["hosts"]) == {key}, parsed

    def test_the_file_is_still_replaced_rather_than_appended(self, tmp_path):
        """The truncation invariant, asserted on the branch that writes content.

        `test_seeded_config_is_truncated_every_run` covers the empty branch. The
        risk here is different and worse: a branch that writes a file could be
        implemented as an append, and an alias table the model planted would
        then be preserved *and* joined by a protocol entry that made it look
        deliberate.
        """
        from istota.skills.developer import _seed_cli_config_dir

        url = "http://127.0.0.1:18080"
        target = _seed_cli_config_dir(
            tmp_path, "gitlab-config", forge="gitlab", forge_url=url
        )
        (target / "config.yml").write_text("aliases:\n    pwn: repo delete\n")

        _seed_cli_config_dir(tmp_path, "gitlab-config", forge="gitlab", forge_url=url)

        assert "pwn" not in (target / "config.yml").read_text()
        assert (target / "config.yml").stat().st_mode & 0o777 == 0o600

    def test_no_password_survives_into_the_file_by_any_route(self, tmp_path):
        """Stated over the whole output rather than over the branch.

        A later change that starts emitting the netloc again would satisfy the
        empty-file assertion only if it also returned early — this one fails
        whatever route the value took.
        """
        for url in (
            "http://user:s3cr3t-value@gitlab.internal:8080",
            "https://user:s3cr3t-value@gitlab.internal",
            "http://s3cr3t-value@gitlab.internal:8080",
        ):
            assert "s3cr3t-value" not in self._seed(tmp_path, url), url


class TestPathPrependOrdering:
    """A secondary guard on the *shape* of the ordering, not its effect.

    The behavioural tests are TestForgeCliPathPrepend in
    tests/test_sandbox_db_env.py: they run a real task and assert the model's
    PATH carries .developer while the skill proxy's does not. Those are what
    prove the property, and they do fail when the two statements are swapped.

    This one exists because the property is easy to break by *moving code*
    while keeping every behaviour test green in some future refactor that
    also changes the fixtures. It asserts the merge loop still skips the key
    and the application still sits after the snapshot. If it ever fights a
    legitimate refactor, delete it — the behavioural tests are the contract.
    """

    def test_reserved_key_is_not_merged_into_env(self):
        """The hook loop skips it, so it cannot ride into proxy_base_env."""
        from istota.sandbox import task_env
        from tests.support.drift import source_of

        # Reads `build_task_runtime` rather than `execute_task`: the env
        # assembly moved to `task_env` whole, and the ordering it guards moved
        # with it. Same two statements, same order, one function along.
        src = source_of(task_env.build_task_runtime)
        assert "if k == HOOK_PATH_PREPEND_KEY:" in src
        # ...and the application site is after the snapshot, not before.
        # Anchored on the assignment target rather than its right-hand side:
        # the property is the *ordering* of the snapshot against the PATH
        # application, and the expression being snapshotted is free to change
        # (ISSUE-390 wrapped it in `without_claude_runtime_env`).
        assert src.index("proxy_base_env = ") < src.index(
            "_path_prepend = hook_env.get(HOOK_PATH_PREPEND_KEY"
        )


class TestWebsiteEnvVars:
    """The agent-writable static web root was removed (ISSUE-194): a
    publicly-served directory the agent could write to with a plain ``cp`` was
    an outbound egress channel the confirmation model classified as a benign
    local write. No env var may hand a task a path to one.
    """

    @patch("istota.executor.subprocess.run")
    def test_website_env_vars_never_set(self, mock_run, tmp_path):
        config = _skills_config(
            tmp_path, mount=True,
            site=SiteConfig(hostname="istota.example.com"),
            users={"alice": UserConfig()},
        )
        _execute(config, mock_run)

        env = _env(mock_run)
        assert "WEBSITE_PATH" not in env
        assert "WEBSITE_URL" not in env


@patch("istota.executor.subprocess.run")
class TestKarakeepEnvVars:
    """Karakeep env vars come from the encrypted secrets table after the
    modules / connected services refactor — the karakeep resource type was
    retired with that change.
    """

    def _make_config(self, tmp_path, monkeypatch, secrets):
        from istota.credentials import store as secrets_store

        monkeypatch.setenv("ISTOTA_SECRET_KEY", "x" * 64)
        config = _skills_config(
            tmp_path, mount=True,
            # Real bundled skills dir so the bookmarks manifest is loaded.
            bundled_skills_dir=None,
            users={"alice": UserConfig()},
            security=SecurityConfig(skill_proxy_enabled=False),
        )
        for key, value in secrets.items():
            secrets_store.set_secret(config.db_path, "alice", "karakeep", key, value)
        return config

    def test_karakeep_env_vars_set_when_secrets_configured(
        self, mock_run, tmp_path, monkeypatch,
    ):
        config = self._make_config(tmp_path, monkeypatch, {
            "base_url": "https://keep.example.com/api/v1",
            "api_key": "kk-secret",
        })
        _execute(config, mock_run)

        env = _env(mock_run)
        assert env["KARAKEEP_BASE_URL"] == "https://keep.example.com/api/v1"
        assert env["KARAKEEP_API_KEY"] == "kk-secret"

    # With only ``base_url`` configured, ``bookmarks`` does not auto-authorize
    # (its sensitive spec ``KARAKEEP_API_KEY`` does not resolve), so none of
    # its env vars flow: the half-configured user gets nothing, a cleaner
    # failure mode than a partial env.
    @pytest.mark.parametrize("secrets", [
        {}, {"base_url": "https://keep.example.com/api/v1"},
    ], ids=["no_secrets", "only_base_url"])
    def test_karakeep_env_vars_not_set(
        self, mock_run, tmp_path, monkeypatch, secrets,
    ):
        config = self._make_config(tmp_path, monkeypatch, secrets)
        _execute(config, mock_run)

        env = _env(mock_run)
        assert "KARAKEEP_BASE_URL" not in env
        assert "KARAKEEP_API_KEY" not in env


class TestWebsitePromptSection:
    """The prompt must not advertise a writable public web root (ISSUE-194)."""

    def test_website_never_in_prompt(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        config = Config(
            db_path=db_path,
            workspace_path=mount_path,
            site=SiteConfig(hostname="istota.example.com"),
            users={"alice": UserConfig()},
        )
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="build my website", user_id="alice", source_type="talk")
            task = db.get_task(conn, task_id)
        # Both halves: the claim is that the primitive is gone from the prompt,
        # not that it landed on one side of the split.
        composed = build_prompt(task, [], config)
        whole = composed.system + composed.user
        assert "Web Root" not in whole
        assert "istota.example.com" not in whole


# ---------------------------------------------------------------------------
# TestAdminIsolation
# ---------------------------------------------------------------------------


class TestAdminPromptIsolation:
    def _composed(self, tmp_path, is_admin, sandbox_enabled=False):
        """Build the prompt for alice; a non-admin is one outside admin_users."""
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        config = Config(
            db_path=tmp_path / "test.db",
            # Admin vs non-admin mount-path scoping is a Nextcloud multi-user
            # feature — the "mounted at" wording requires a Nextcloud backend.
            nextcloud=NextcloudConfig(url="https://cloud.example.com"),
            workspace_path=mount_path,
            admin_users=set() if is_admin else {"bob"},
        )
        if sandbox_enabled:
            config.security.sandbox_enabled = True
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
            task = db.get_task(conn, task_id)
        return config, build_prompt(task, [], config, is_admin=is_admin)

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_prompt_never_states_the_db_path(self, tmp_path, is_admin):
        """Naming a file that has been masked out of the sandbox is worse than
        saying nothing: a failed open reads as a broken command, not a boundary."""
        config, composed = self._composed(tmp_path, is_admin)
        # Both halves: "the prompt never names the database path" is a boundary
        # claim about everything the model is shown, not a claim about where a
        # layer was classified. A half-scoped assertion would go quiet the day
        # a path started leaking through the other one.
        assert str(config.db_path) not in composed.system + composed.user
        assert "Database: reachable only through skill CLIs" in composed.system

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_absence_claim_only_when_sandbox_is_in_effect(self, tmp_path, is_admin):
        with patch("istota.executor._bwrap_available", return_value=True):
            _, composed = self._composed(tmp_path, is_admin, sandbox_enabled=True)
        assert "the directories that hold them are empty here" in composed.system

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_prohibition_kept_where_there_is_no_sandbox(self, tmp_path, is_admin):
        """Docker without CAP_SYS_ADMIN, and the standalone install.

        The databases really are on the model's filesystem on those shapes, so
        claiming they aren't would be a false boundary — the exact failure this
        change set exists to correct. The older prohibition wording covers it.
        """
        with patch("istota.executor._bwrap_available", return_value=False):
            _, composed = self._composed(tmp_path, is_admin, sandbox_enabled=True)
        prompt = composed.system
        assert "the directories that hold them are empty here" not in prompt
        assert "Never open a database file directly" in prompt
        assert "no filesystem sandbox" in prompt

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_standing_rules_present_for_everyone(self, tmp_path, is_admin):
        import re

        _, composed = self._composed(tmp_path, is_admin)
        prompt = composed.system
        # ISSUE-091 — UTC anchor + elapsed-time rule.
        assert re.search(r"Current UTC: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", prompt), (
            "Current UTC ISO 8601 line missing from prompt header"
        )
        assert "normalize both to ISO 8601 UTC" in prompt, (
            "Elapsed-time arithmetic rule missing from rules section"
        )
        # ISSUE-155 — dates in fetched content must not override the prompt date.
        assert "publication or authorship dates" in prompt, (
            "Fetched-content date rule missing from rules section"
        )
        # Subtask creation instructions belong in the tasks skill doc, loaded
        # only when relevant, not in the hardcoded prompt.
        assert "create subtasks" not in prompt.lower()
        # The sqlite3 tool was removed in favour of deferred JSON operations.
        assert "sqlite3 for the task database" not in prompt

    def test_admin_prompt(self, tmp_path):
        config, composed = self._composed(tmp_path, is_admin=True)
        assert "Privileges: admin" in composed.system
        assert f"mounted at '{config.workspace_path}'" in composed.system

    def test_non_admin_prompt(self, tmp_path):
        config, composed = self._composed(tmp_path, is_admin=False)
        prompt = composed.system
        assert "Privileges: standard user" in prompt
        assert "Privileges: admin" not in prompt
        scoped = str(config.workspace_path / "Users" / "alice")
        assert f"mounted at '{scoped}'" in prompt
        assert "You can ONLY access files under" in prompt
        assert "do NOT have access to the task database" in prompt


@patch("istota.executor.subprocess.run")
class TestAdminEnvVarIsolation:
    def _make_config(self, tmp_path, is_admin=True):
        return _skills_config(
            tmp_path, mount=True, admin_users=set() if is_admin else {"bob"},
        )

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_no_db_path_env(self, mock_run, tmp_path, is_admin):
        """Admins used to get ISTOTA_DB_PATH in Claude's env. Nobody does now.

        It goes to the skill proxy instead — see
        tests/test_sandbox_db_env.py::TestFrameworkDbPathRouting, which also
        covers the non-admin half (the path reaches the proxy for every user,
        which is what un-broke scoped reads for non-admins).
        """
        _execute(self._make_config(tmp_path, is_admin), mock_run)
        assert "ISTOTA_DB_PATH" not in _env(mock_run)

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_mount_path_env_is_the_real_root(self, mock_run, tmp_path, is_admin):
        # The mount env var is the REAL root for non-admins too. Every consumer
        # (memory / memory_search CLIs, the schedules/reminders skill docs)
        # prepends `Users/<uid>` to it; a previously "scoped" mount
        # (real/Users/<uid>) doubled that segment, so a non-admin's USER.md write
        # landed at real/Users/<uid>/Users/<uid>/… — a phantom path never read
        # back. Filesystem isolation is enforced by the bwrap bind (only the
        # user's own Users/<uid> dir is bound), not by this env var.
        config = self._make_config(tmp_path, is_admin)
        _execute(config, mock_run)

        env = _env(mock_run)
        assert env["NEXTCLOUD_MOUNT_PATH"] == str(config.workspace_path)
        # Specifically NOT the doubled/scoped form.
        assert env["NEXTCLOUD_MOUNT_PATH"] != str(
            config.workspace_path / "Users" / "alice"
        )

    @pytest.mark.parametrize("is_admin", [True, False])
    def test_admin_only_skills_follow_admin_status(self, mock_run, tmp_path, is_admin):
        """Admin users get admin-only skills like schedules in the prompt;
        non-admins do not."""
        config = self._make_config(tmp_path, is_admin)
        (config.skills_dir / "_index.toml").write_text(
            _FILES_INDEX + '\n'
            '[schedules]\ndescription = "Scheduled jobs"\nsource_types = ["talk"]\nadmin_only = true\n'
        )
        (config.skills_dir / "schedules.md").write_text("Admin scheduling reference.")
        _execute(config, mock_run, prompt="set up a schedule")

        assert ("Admin scheduling reference" in _system_half(config)) is is_admin


@patch("istota.executor.subprocess.run")
class TestDeferredDirEnvVar:
    """ISTOTA_DEFERRED_DIR env var should always be set."""

    @pytest.mark.parametrize("admin_users", [set(), {"bob"}], ids=["admin", "non_admin"])
    def test_deferred_dir_set(self, mock_run, tmp_path, admin_users):
        config = _skills_config(tmp_path, mount=True, admin_users=admin_users)
        _execute(config, mock_run)
        assert _env(mock_run)["ISTOTA_DEFERRED_DIR"] == str(tmp_path / "temp" / "alice")

    def test_experimental_features_propagated(self, mock_run, tmp_path):
        """LLM-path subprocess must carry ISTOTA_EXPERIMENTAL_FEATURES so
        skills invoked via the skill proxy (which forwards env to skill CLIs)
        see consistent gating with the scheduler subprocess paths."""
        from istota.config import ExperimentalConfig
        config = _skills_config(tmp_path, mount=True)
        config.experimental = ExperimentalConfig(features=["money_tax", "money_wash_sales"])
        _execute(config, mock_run)
        assert _env(mock_run)["ISTOTA_EXPERIMENTAL_FEATURES"] == "money_tax,money_wash_sales"

    def test_experimental_features_empty_when_unset(self, mock_run, tmp_path):
        """Always-set contract: even with no features enabled, the var
        exists (empty string) so consumers don't have to dance around
        os.environ.get(...) returning None."""
        _execute(_skills_config(tmp_path, mount=True), mock_run)
        assert _env(mock_run)["ISTOTA_EXPERIMENTAL_FEATURES"] == ""


# ---------------------------------------------------------------------------
# TestCalDAVCredentialScoping
# ---------------------------------------------------------------------------


@patch("istota.executor.subprocess.run")
class TestCalDAVCredentialScoping:
    """CalDAV credentials should only be injected when user has calendars."""

    def _make_config(self, tmp_path):
        return _skills_config(
            tmp_path, mount=True,
            # Real bundled skills dir so the calendar manifest's
            # gate_has_discovered_calendars CALDAV_* specs are loaded.
            bundled_skills_dir=None,
            nextcloud=NextcloudConfig(
                url="https://nc.example.com",
                username="bot",
                app_password="secret",
            ),
        )

    @pytest.mark.parametrize("calendars, expected", [
        ([("Personal", "https://cal/personal", True)], True),
        ([], False),
    ], ids=["has_calendars", "no_calendars"])
    @patch("istota.executor.get_caldav_client")
    @patch("istota.executor.get_calendars_for_user")
    def test_caldav_creds_follow_calendars(
        self, mock_cals, mock_client, mock_run, tmp_path, calendars, expected,
    ):
        mock_cals.return_value = calendars
        _execute(self._make_config(tmp_path), mock_run)

        env = _env(mock_run)
        assert ("CALDAV_URL" in env) is expected
        assert ("CALDAV_USERNAME" in env) is expected

    def test_caldav_creds_absent_when_no_caldav_config(self, mock_run, tmp_path):
        """No CalDAV configured at all — creds should not appear."""
        config = self._make_config(tmp_path)
        config.nextcloud = NextcloudConfig()  # No URL = no CalDAV
        _execute(config, mock_run)

        env = _env(mock_run)
        assert "CALDAV_URL" not in env
        assert "CALDAV_USERNAME" not in env


# ---------------------------------------------------------------------------
# TestUserIdSubstitution
# ---------------------------------------------------------------------------


class TestUserIdSubstitution:
    """Skill docs should have {user_id} replaced with actual user ID."""

    @patch("istota.executor.subprocess.run")
    def test_user_id_substituted_in_skills_doc(self, mock_run, tmp_path):
        config = _skills_config(tmp_path, files_skill=False, mount=True)
        (config.skills_dir / "_index.toml").write_text(
            '[memory]\ndescription = "Memory"\nalways_include = true\n'
        )
        skill_dir = config.skills_dir / "memory"
        skill_dir.mkdir()
        (skill_dir / "skill.toml").write_text(
            'description = "Memory"\nalways_include = true\n'
        )
        (skill_dir / "skill.md").write_text(
            "Memory file at /Users/{user_id}/bot/config/USER.md"
        )
        _execute(config, mock_run)

        # The skill body carrying the placeholder is a standing instruction,
        # so the substitution is visible in the system half.
        composed = _system_half(config)
        assert "/Users/alice/bot/config/USER.md" in composed
        assert "{user_id}" not in composed


# ---------------------------------------------------------------------------
# TestLoadPersona
# ---------------------------------------------------------------------------


class TestLoadPersona:
    def _make_config(self, tmp_path, has_workspace=True):
        config_dir = tmp_path / "config"
        skills_dir = config_dir / "skills"
        skills_dir.mkdir(parents=True)
        kwargs = dict(skills_dir=skills_dir, bundled_skills_dir=tmp_path / "_empty_bundled")
        if has_workspace:
            mount = tmp_path / "mount"
            mount.mkdir()
            kwargs["workspace_path"] = mount
        return Config(**kwargs)

    def _plant_global(self, tmp_path, text="Global persona"):
        (tmp_path / "config" / "persona.md").write_text(text)

    def _user_config_dir(self, config, bot_dir="istota"):
        user_dir = config.workspace_path / "Users" / "alice" / bot_dir / "config"
        user_dir.mkdir(parents=True)
        return user_dir

    def test_user_persona_overrides_global(self, tmp_path):
        config = self._make_config(tmp_path)
        self._plant_global(tmp_path)
        (self._user_config_dir(config) / "PERSONA.md").write_text("Custom persona for Alice")

        assert load_persona(config, user_id="alice") == "Custom persona for Alice"

    def test_empty_user_persona_falls_back_to_global(self, tmp_path):
        config = self._make_config(tmp_path)
        self._plant_global(tmp_path)
        (self._user_config_dir(config) / "PERSONA.md").write_text("   ")

        assert load_persona(config, user_id="alice") == "Global persona"

    @pytest.mark.parametrize("has_workspace, user_id", [
        (True, "alice"), (False, "alice"), (True, None),
    ], ids=["missing_user_persona", "no_mount", "no_user_id"])
    def test_falls_back_to_global(self, tmp_path, has_workspace, user_id):
        config = self._make_config(tmp_path, has_workspace=has_workspace)
        self._plant_global(tmp_path)

        assert load_persona(config, user_id=user_id) == "Global persona"

    def test_bot_name_substituted_in_user_persona(self, tmp_path):
        config = self._make_config(tmp_path)
        config.bot_name = "Jarvis"
        (self._user_config_dir(config, "jarvis") / "PERSONA.md").write_text(
            "You are {BOT_NAME}, a helpful bot."
        )

        assert load_persona(config, user_id="alice") == "You are Jarvis, a helpful bot."


class TestLoadPersonaPlantedPaths(TestLoadPersona):
    """PERSONA.md sits in a directory bound read-write into the user's own
    sandbox, and `load_persona` reads it host-side, in the daemon's filesystem
    view. Whatever it returns becomes prompt text on the next task (ISSUE-339).

    Subclasses `TestLoadPersona` so the cases above run again against the
    hardened reader: the refusals below are only worth anything if the ordinary
    paths still work, and a guard that rejects everything would otherwise pass
    every test in this class.
    """

    def test_a_symlink_at_persona_is_not_followed(self, tmp_path):
        config = self._make_config(tmp_path)
        self._plant_global(tmp_path)
        secret = tmp_path / "credentials.json"
        secret.write_text("TOP SECRET TOKEN")
        (self._user_config_dir(config) / "PERSONA.md").symlink_to(secret)

        assert load_persona(config, user_id="alice") == "Global persona"

    def test_a_fifo_at_persona_is_refused_without_blocking(self, tmp_path):
        # Prompt assembly runs before the BrainRequest exists, so nothing
        # times this out: one mkfifo wedges every later task for this user.
        from .support.blocking import fails_if_it_blocks

        config = self._make_config(tmp_path)
        self._plant_global(tmp_path)
        os.mkfifo(self._user_config_dir(config) / "PERSONA.md")

        with fails_if_it_blocks(what="load_persona"):
            assert load_persona(config, user_id="alice") == "Global persona"

    def test_a_symlinked_config_dir_cannot_redirect_persona(self, tmp_path):
        config = self._make_config(tmp_path)
        self._plant_global(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "PERSONA.md").write_text("TOP SECRET TOKEN")
        bot_dir = config.workspace_path / "Users" / "alice" / "istota"
        bot_dir.mkdir(parents=True)
        (bot_dir / "config").symlink_to(elsewhere, target_is_directory=True)

        assert load_persona(config, user_id="alice") == "Global persona"

    def test_an_ancestor_symlink_inside_the_users_own_tree_is_allowed(self, tmp_path):
        config = self._make_config(tmp_path)
        self._plant_global(tmp_path)
        base = config.workspace_path / "Users" / "alice"
        real = base / "istota" / "real_config"
        real.mkdir(parents=True)
        (real / "PERSONA.md").write_text("Custom persona for Alice")
        (base / "istota" / "config").symlink_to(real, target_is_directory=True)

        assert load_persona(config, user_id="alice") == "Custom persona for Alice"

    def test_bot_name_substituted_in_global_persona(self, tmp_path):
        config = self._make_config(tmp_path)
        config.bot_name = "Jarvis"
        self._plant_global(tmp_path, "You are {BOT_NAME}.")

        assert load_persona(config) == "You are Jarvis."

    def test_no_persona_files_returns_none(self, tmp_path):
        config = self._make_config(tmp_path)
        assert load_persona(config, user_id="alice") is None


# ---------------------------------------------------------------------------
# TestLoadEmissaries
# ---------------------------------------------------------------------------


class TestLoadChannelGuidelines:
    """Guidelines are templated docs; the placeholders have to resolve.

    web.md's file-handover section names a concrete workspace path, so a
    literal ``{user_id}`` reaching the model would hand the user a broken link.
    """

    def _make_config(self, tmp_path, web_md=None):
        config_dir = tmp_path / "config"
        (config_dir / "skills").mkdir(parents=True)
        (config_dir / "guidelines").mkdir()
        if web_md is not None:
            (config_dir / "guidelines" / "web.md").write_text(web_md)
        return Config(
            skills_dir=config_dir / "skills",
            bundled_skills_dir=tmp_path / "_empty_bundled",
            bot_name="Istota",
        )

    @pytest.mark.parametrize("web_md, user_id, expected", [
        ("path=/Users/{user_id}/istota/report.csv", "alice",
         "path=/Users/alice/istota/report.csv"),
        ("{BOT_NAME} in {BOT_DIR} for {user_id}", "alice", "Istota in istota for alice"),
        # No user id leaves the placeholder rather than crashing.
        ("hi {user_id}", None, "hi {user_id}"),
        (None, "alice", None),
    ], ids=["user_id", "bot_placeholders", "no_user_id", "missing_file"])
    def test_load(self, tmp_path, web_md, user_id, expected):
        from istota.executor import load_channel_guidelines

        config = self._make_config(tmp_path, web_md)
        args = (config, "web") if user_id is None else (config, "web", user_id)
        assert load_channel_guidelines(*args) == expected


def _shipped_guideline(name):
    import istota
    repo = Path(istota.__file__).resolve().parents[2]
    return (repo / "config" / "guidelines" / name).read_text()


class TestShippedWebGuidelines:
    """The shipped web.md must actually carry the handover rule.

    Without it the model quotes a filesystem path the browser user cannot open,
    or reaches for a public share link to show someone their own file.
    """

    def test_points_at_the_download_endpoint_and_not_a_share_link(self):
        text = _shipped_guideline("web.md")
        assert "/api/chat/files?path=" in text
        assert "public share link" in text

    def test_teaches_the_inline_image_form_and_its_limits(self):
        # The prompt goldens cannot witness this: `test_prompt_golden.py`
        # writes its own one-line guideline stubs into a tmp config dir, so a
        # change to the shipped file diffs nothing there.
        text = _shipped_guideline("web.md")
        assert "![" in text
        for fmt in ("PNG", "JPEG", "GIF", "WebP"):
            assert fmt in text


class TestShippedTalkGuidelines:
    """The shipped talk.md must carry `share-file` and the token caveat.

    The command works end to end and was documented nowhere, and the token in
    the prompt is the room's canonical one — on a room promoted out of web
    chat it is not the Talk conversation's, and the share 404s.
    """

    def test_names_the_share_file_verb_and_the_promoted_room_caveat(self):
        text = _shipped_guideline("talk.md")
        assert "nextcloud talk share-file" in text
        assert "404" in text


class TestLoadEmissaries:
    def _make_config(self, tmp_path, text=None):
        config_dir = tmp_path / "config"
        skills_dir = config_dir / "skills"
        skills_dir.mkdir(parents=True)
        if text is not None:
            (config_dir / "emissaries.md").write_text(text)
        return Config(skills_dir=skills_dir, bundled_skills_dir=tmp_path / "_empty_bundled")

    def test_returns_none_when_absent(self, tmp_path):
        assert load_emissaries(self._make_config(tmp_path)) is None

    def test_returns_content_when_present(self, tmp_path):
        config = self._make_config(tmp_path, "# Emissaries\n\nBe good.")
        assert load_emissaries(config) == "# Emissaries\n\nBe good."

    def test_no_bot_name_substitution(self, tmp_path):
        config = self._make_config(tmp_path, "Agent {BOT_NAME} principles")
        config.bot_name = "Jarvis"
        assert load_emissaries(config) == "Agent {BOT_NAME} principles"

    def test_returns_none_when_disabled(self, tmp_path):
        config = self._make_config(tmp_path, "# Emissaries\n\nBe good.")
        config.emissaries_enabled = False
        assert load_emissaries(config) is None


class TestEmissariesInPrompt:
    def _make_task(self):
        return db.Task(
            id=1, status="running", prompt="hello", user_id="alice",
            source_type="talk", conversation_token="room1",
            created_at="2024-01-01T00:00:00",
        )

    def test_emissaries_appears_in_prompt(self):
        result = build_prompt(
            self._make_task(), [], Config(), emissaries="# Emissaries\n\nBe good.",
        ).system
        assert "# Emissaries" in result
        assert "Be good." in result

    def test_emissaries_before_persona(self, tmp_path):
        config_dir = tmp_path / "config"
        skills_dir = config_dir / "skills"
        skills_dir.mkdir(parents=True)
        (config_dir / "persona.md").write_text("# Persona\n\nBe helpful.")
        config = Config(skills_dir=skills_dir, bundled_skills_dir=tmp_path / "_empty_bundled")

        result = build_prompt(
            self._make_task(), [], config, emissaries="# Emissaries\n\nBe good.",
        ).system
        assert result.index("# Emissaries") < result.index("# Persona")

    def test_emissaries_absent_when_no_file(self):
        result = build_prompt(self._make_task(), [], Config()).system
        assert "Emissaries" not in result


# ---------------------------------------------------------------------------
# TestPreTranscribeAttachments
# ---------------------------------------------------------------------------


_TRANSCRIBE_PATCH = "istota.executor.transcribe_audio_out_of_process"


class TestPreTranscribeAttachments:
    def test_no_attachments_returns_prompt_unchanged(self):
        assert _pre_transcribe_attachments(None, "hello") == "hello"
        assert _pre_transcribe_attachments([], "hello") == "hello"

    def test_non_audio_attachments_returns_prompt_unchanged(self):
        result = _pre_transcribe_attachments(["/tmp/photo.jpg", "/tmp/doc.pdf"], "[photo.jpg]")
        assert result == "[photo.jpg]"

    @patch(_TRANSCRIBE_PATCH)
    def test_audio_attachment_transcribed_successfully(self, mock_transcribe):
        mock_transcribe.return_value = {"status": "ok", "text": "remind me to buy groceries"}
        result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "[voice.mp3]")
        assert "remind me to buy groceries" in result
        assert "voice.mp3" in result
        assert "Transcribed voice message:" in result
        assert mock_transcribe.call_count == 1
        assert mock_transcribe.call_args[0][0] == "/tmp/voice.mp3"

    @patch(_TRANSCRIBE_PATCH)
    def test_the_task_identity_is_handed_to_the_runner(self, mock_transcribe):
        """The child is a skill CLI and its path argument is scoped.

        `whisper transcribe` resolves `audio_path` against the allowlist the
        *child's* environment names (ISSUE-447), so the daemon has to say who
        the task belongs to. Without it the child's allowlist is empty, every
        path is refused, and the failure is logged at debug and swallowed —
        which is what makes this worth pinning at the call rather than
        leaving to the runner's own tests.
        """
        mock_transcribe.return_value = {"status": "ok", "text": "call the plumber"}
        _pre_transcribe_attachments(
            ["/mnt/shared/Users/alice/voice.mp3"], "",
            user_id="alice",
            mount_path="/mnt/shared",
            deferred_dir="/tmp/istota/alice",
        )
        kwargs = mock_transcribe.call_args.kwargs
        assert kwargs["user_id"] == "alice"
        assert kwargs["mount_path"] == "/mnt/shared"
        assert kwargs["deferred_dir"] == "/tmp/istota/alice"

    @patch(_TRANSCRIBE_PATCH)
    def test_empty_prompt_becomes_the_transcript(self, mock_transcribe):
        """A voice memo sent with nothing typed: the transcript is the prompt."""
        mock_transcribe.return_value = {"status": "ok", "text": "call the plumber"}
        result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "")
        assert result.startswith("Transcribed voice message: call the plumber")

    @patch(_TRANSCRIBE_PATCH)
    def test_accompanying_text_is_kept(self, mock_transcribe):
        """Text sent alongside a voice memo is the instruction the audio was
        sent under — the transcript is appended, never a replacement."""
        mock_transcribe.return_value = {"status": "ok", "text": "call the plumber"}
        result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "summarize this")
        assert result.startswith("summarize this")
        assert "call the plumber" in result

    @pytest.mark.parametrize("outcome", [
        {"status": "error", "error": "corrupted file"},
        RuntimeError("boom"),
        # The dependency is missing *in the child*, which reports it as an
        # ordinary error result rather than raising in the daemon.
        {
            "status": "error",
            "error": "faster-whisper not installed. Install with: uv sync --extra whisper",
        },
        {"status": "ok", "text": "  "},
    ], ids=["failure", "exception", "faster_whisper_missing", "empty_transcription"])
    @patch(_TRANSCRIBE_PATCH)
    def test_unusable_transcription_returns_prompt_unchanged(self, mock_transcribe, outcome):
        if isinstance(outcome, Exception):
            mock_transcribe.side_effect = outcome
        else:
            mock_transcribe.return_value = outcome
        result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "[voice.mp3]")
        assert result == "[voice.mp3]"

    @patch(_TRANSCRIBE_PATCH)
    def test_mixed_audio_and_non_audio_attachments(self, mock_transcribe):
        mock_transcribe.return_value = {"status": "ok", "text": "schedule a meeting"}
        result = _pre_transcribe_attachments(
            ["/tmp/photo.jpg", "/tmp/memo.m4a", "/tmp/doc.pdf"],
            "[photo.jpg] [memo.m4a]",
        )
        assert "schedule a meeting" in result
        assert "memo.m4a" in result
        assert mock_transcribe.call_count == 1
        assert mock_transcribe.call_args[0][0] == "/tmp/memo.m4a"

    @patch(_TRANSCRIBE_PATCH)
    def test_multiple_audio_attachments(self, mock_transcribe):
        mock_transcribe.side_effect = [
            {"status": "ok", "text": "first part"},
            {"status": "ok", "text": "second part"},
        ]
        result = _pre_transcribe_attachments(
            ["/tmp/a.mp3", "/tmp/b.wav"],
            "[a.mp3] [b.wav]",
        )
        for needle in ("first part", "second part", "a.mp3", "b.wav"):
            assert needle in result

    def test_all_audio_extensions_recognized(self):
        for ext in ["mp3", "wav", "ogg", "flac", "m4a", "opus", "webm", "mp4", "aac", "wma"]:
            assert ext in _AUDIO_EXTENSIONS


_POPEN = "istota.skills.whisper.out_of_process.subprocess.Popen"


def _whisper_child(text):
    """A finished whisper CLI child, as `Popen` would hand it back."""
    proc = MagicMock()
    proc.pid = 99
    proc.returncode = 0
    proc.communicate.return_value = (json.dumps({"status": "ok", "text": text}), "")
    return proc


class TestTheAudioTheChildIsActuallyHandedIsInReach:
    """The end-to-end half of the identity plumbing, through the real chain.

    Passing the task identity is necessary and is not sufficient, and the two
    halves fail differently: without the identity the child has an empty
    allowlist, and with it the child still refuses three of the four shapes a
    Talk attachment arrives in. `download_talk_attachments` produces all
    four — `{mount}/Talk/<name>` when the bot's own view holds the file,
    `/mnt/nc-data/<user>/files/Talk/<name>` when Nextcloud kept it in the
    sender's data dir instead, the bare relative `Talk/<name>` when neither
    resolves, and `{temp_dir}/<name>` on the rclone branch, which is a
    *sibling* of the per-user temp dir rather than a child.

    So these drive `_pre_transcribe_attachments` with a path shaped like each
    producer's output and assert on the argv the child would actually be
    given, with only `Popen` faked. Asserting that three kwargs reached a
    `MagicMock` is what left this uncovered: it pins the first hop of a chain
    whose second hop is where the refusal happens.
    """

    @staticmethod
    def _identity(tmp_path):
        mount = tmp_path / "mount"
        (mount / "Talk").mkdir(parents=True)
        (mount / "Users" / "alice").mkdir(parents=True)
        temp = tmp_path / "temp" / "alice"
        temp.mkdir(parents=True)
        return {
            "user_id": "alice",
            "mount_path": mount,
            "deferred_dir": temp,
        }

    @staticmethod
    def _spawn(monkeypatch):
        """Fake `Popen`, returning the argv the child would have run."""
        seen = {}

        def fake_popen(argv, **kwargs):
            seen["argv"] = argv
            seen["env"] = kwargs.get("env") or {}
            return _whisper_child("buy milk")

        monkeypatch.setattr(_POPEN, fake_popen)
        return seen

    def test_a_talk_attachment_on_the_mount_is_passed_through(
        self, tmp_path, monkeypatch,
    ):
        identity = self._identity(tmp_path)
        seen = self._spawn(monkeypatch)
        audio = identity["mount_path"] / "Talk" / "memo.m4a"
        audio.write_bytes(b"not really audio")

        out = executor._pre_transcribe_attachments([str(audio)], "", **identity)

        assert "buy milk" in out
        # Handed through unchanged: it was already in reach, so nothing is
        # copied and the prompt still names the file the user shared.
        assert seen["argv"][-1] == str(audio)

    @pytest.mark.parametrize("shape", ["nc_data", "sibling_temp"])
    def test_an_attachment_outside_the_roots_is_staged_into_them(
        self, tmp_path, monkeypatch, shape,
    ):
        """The two shipped shapes the identity alone does not reach.

        Both were transcribable before the path argument was scoped, and a
        refusal here is logged at debug and swallowed — so without the
        staging step this regression would have been invisible in production
        and green in the suite.
        """
        identity = self._identity(tmp_path)
        seen = self._spawn(monkeypatch)
        if shape == "nc_data":
            source = tmp_path / "nc-data" / "bob" / "files" / "Talk" / "memo.m4a"
        else:
            source = tmp_path / "temp" / "memo.m4a"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"not really audio")

        out = executor._pre_transcribe_attachments([str(source)], "", **identity)

        assert "buy milk" in out
        handed = Path(seen["argv"][-1])
        assert handed != source
        assert handed.read_bytes() == b"not really audio"
        # In reach means: under a root the child derives from the very
        # environment it was given, asked of the shared rule rather than
        # recomputed here.
        roots = workspace_roots(
            mount=seen["env"].get("NEXTCLOUD_MOUNT_PATH"),
            user_id=seen["env"].get("ISTOTA_USER_ID", ""),
            deferred_dir=seen["env"].get("ISTOTA_DEFERRED_DIR"),
            talk=True,
        )
        assert path_under_roots(handed.resolve(), roots), (handed, roots)

    def test_an_attachment_that_cannot_be_staged_is_skipped_loudly(
        self, tmp_path, monkeypatch, caplog,
    ):
        """Roots to scope against, a path outside them, nowhere to copy to.

        Every other outcome of this pass leaves a prompt that is merely
        shorter than it could be. A path the child would refuse is the one
        that reads as a broken feature, so it is the one that warns rather
        than joining the debug line the rest of the failures share.
        """
        identity = self._identity(tmp_path)
        identity["deferred_dir"] = None  # a mount to scope by, nowhere to stage
        seen = self._spawn(monkeypatch)
        source = tmp_path / "nc-data" / "bob" / "memo.m4a"
        source.parent.mkdir(parents=True)
        source.write_bytes(b"not really audio")

        with caplog.at_level(logging.WARNING, logger="istota.executor"):
            out = executor._pre_transcribe_attachments([str(source)], "typed", **identity)

        assert out == "typed"
        assert "argv" not in seen, "the child was spawned with a path it must refuse"
        assert any("outside the roots" in r.message for r in caplog.records)

    def test_a_caller_that_supplied_no_identity_still_reaches_the_child(
        self, tmp_path, monkeypatch,
    ):
        """Silence is not a boundary.

        With no identity there is nothing to scope against, so the path goes
        through and the child answers — refusing, with a message naming the
        missing variables. Deciding here instead would turn a caller's
        omission into a skip with no spawn and nothing said by the one
        component that knows why.
        """
        seen = self._spawn(monkeypatch)
        source = tmp_path / "memo.m4a"
        source.write_bytes(b"not really audio")

        executor._pre_transcribe_attachments([str(source)], "")

        assert seen["argv"][-1] == str(source)


class TestAWhatsAppVoiceNoteIsTranscribed:
    """The WhatsApp half end to end, from the task row to the transcript.

    The staging step names the inbox copy `/Users/<uid>/inbox/...`, the
    scheduler maps it onto the mount, and the pass this file covers takes it
    from there. Only the child is stubbed.
    """

    @patch(_TRANSCRIBE_PATCH)
    def test_a_localized_ogg_is_transcribed_into_the_prompt(
        self, mock_transcribe, tmp_path,
    ):
        from istota.scheduler import localize_workspace_attachments
        from istota.transport.ingest import describe_attachment_only_message

        mount = tmp_path / "mount"
        inbox = mount / "Users" / "alice" / "inbox"
        inbox.mkdir(parents=True)
        fixture = Path(__file__).parent / "fixtures" / "audio" / "voice.ogg"
        (inbox / "whatsapp_ab12-cd34.ogg").write_bytes(fixture.read_bytes())
        deferred = tmp_path / "temp" / "alice"
        deferred.mkdir(parents=True)
        config = Config(workspace_path=mount, temp_dir=tmp_path / "temp")
        row = ["/Users/alice/inbox/whatsapp_ab12-cd34.ogg"]
        mock_transcribe.return_value = {"status": "ok", "text": "buy milk"}

        attachments = localize_workspace_attachments(config, "alice", row)
        out = _pre_transcribe_attachments(
            attachments, describe_attachment_only_message(row),
            user_id="alice", mount_path=mount, deferred_dir=deferred,
        )

        assert mock_transcribe.call_count == 1
        assert os.path.isfile(mock_transcribe.call_args[0][0])
        assert out.startswith("Voice message (see attached audio).")
        assert "Transcribed voice message: buy milk" in out


class TestPreTranscriptionStaysOutOfTheDaemon:
    """ISSUE-273.

    `import faster_whisper` costs ~293 MB of resident set, and each
    construct-transcribe-drop cycle leaves ~450 MB on glibc's free lists that
    the daemon never calls `malloc_trim` to get back. Five voice messages over
    one 66-hour run walked the scheduler from 820 MB to 2894 MB in four
    discrete steps, each within three minutes of a transcription. None of that
    memory may be spent in the daemon, so these tests pin *where* the work
    runs, not just what it returns.
    """

    def test_it_spawns_the_whisper_cli_instead_of_importing_the_model(self):
        with patch(_POPEN, return_value=_whisper_child("buy milk")) as popen:
            result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "")

        argv = popen.call_args[0][0]
        assert argv[0] == sys.executable
        assert argv[1:5] == ["-P", "-m", "istota.skills.whisper", "transcribe"]
        assert "buy milk" in result

    def test_the_in_process_transcriber_is_never_called(self):
        """The seam that carried the leak. `transcribe.transcribe_audio` is the
        function that pulls faster_whisper into whichever process calls it."""
        with patch("istota.skills.whisper.transcribe.transcribe_audio") as in_process, \
                patch(_POPEN, return_value=_whisper_child("hi")):
            _pre_transcribe_attachments(["/tmp/voice.mp3"], "")

        in_process.assert_not_called()

    def test_the_timeout_budget_is_shared_across_the_send_not_per_file(self):
        """This runs on a worker thread before the brain call, so
        `scheduler.task_timeout_minutes` does not cover it. A per-file limit
        would let a five-attachment send hold the worker for five times the
        bound — the stall the timeout exists to prevent, not a smaller one."""
        with patch(_TRANSCRIBE_PATCH) as mock_transcribe:
            mock_transcribe.return_value = {"status": "ok", "text": "x"}
            _pre_transcribe_attachments(["/tmp/a.mp3", "/tmp/b.wav", "/tmp/c.m4a"], "")

        budgets = [c.kwargs["timeout"] for c in mock_transcribe.call_args_list]
        assert len(budgets) == 3
        # Strictly decreasing: each call gets what is left, not a fresh grant.
        assert budgets == sorted(budgets, reverse=True)
        assert budgets[0] <= _PRE_TRANSCRIBE_TOTAL_TIMEOUT_SECONDS
        assert sum(budgets) < 3 * _PRE_TRANSCRIBE_TOTAL_TIMEOUT_SECONDS

    def test_files_after_the_budget_runs_out_are_skipped_and_earlier_text_kept(
        self, monkeypatch,
    ):
        def eat_the_budget(path, timeout=None, **identity):
            # First file consumes the whole budget, as a wedged child would.
            if path.endswith("a.mp3"):
                _clock[0] += _PRE_TRANSCRIBE_TOTAL_TIMEOUT_SECONDS + 1
                return {"status": "ok", "text": "first one landed"}
            raise AssertionError(f"should not have been called for {path}")

        _clock = [1000.0]
        monotonic_spy(monkeypatch, executor, lambda: _clock[0])
        with patch(_TRANSCRIBE_PATCH, side_effect=eat_the_budget):
            result = _pre_transcribe_attachments(["/tmp/a.mp3", "/tmp/b.wav"], "")

        assert "first one landed" in result

    def test_each_audio_file_gets_its_own_process(self):
        """One process per file, so the ratchet resets between them rather than
        accumulating across a multi-attachment send."""
        with patch(_POPEN, return_value=_whisper_child("x")) as popen:
            _pre_transcribe_attachments(["/tmp/a.mp3", "/tmp/b.wav"], "")

        assert popen.call_count == 2


# Image preparation moved out of the executor into `image_attachments`; its
# tests live in `tests/test_image_attachments.py` and the executor-side
# integration in `tests/test_executor_images.py`.


# ---------------------------------------------------------------------------
# TestPromptOutputTarget
# ---------------------------------------------------------------------------


class TestPromptOutputTarget:
    """Verify that source_type and output_target appear in the prompt header."""

    def _prompt(self, task_source, task_target=None, **kw):
        task = db.Task(
            id=1, status="running", prompt="hello", user_id="alice",
            source_type=task_source, conversation_token="room1",
            output_target=task_target,
        )
        return build_prompt(task, [], Config(), **kw).system

    @pytest.mark.parametrize("source_type, task_target, output_target", [
        ("talk", None, "talk"), ("scheduled", "email", "email"),
    ])
    def test_source_and_target_in_prompt(self, source_type, task_target, output_target):
        result = self._prompt(
            source_type, task_target,
            source_type=source_type, output_target=output_target,
        )
        assert f"Source: {source_type}" in result
        assert f"Output target: {output_target}" in result

    def test_defaults_when_no_output_target(self):
        result = self._prompt("cli")
        assert "Source: cli" in result
        assert "Output target: text" in result

    def test_email_tool_line_distinguishes_send_and_output(self):
        result = self._prompt("talk")
        assert "email send" in result
        assert "email output" in result
        assert "Only use `output` when this task arrived as an incoming email" in result


# ---------------------------------------------------------------------------
# TestDetectNotificationReply
# ---------------------------------------------------------------------------


def _notified_parent(conn, *, source_type, result, prompt="parent", talk_id=42):
    """A completed task in room1 whose Talk post has id `talk_id`."""
    parent_id = db.create_task(
        conn, prompt=prompt, user_id="alice",
        source_type=source_type, conversation_token="room1",
    )
    db.update_task_status(conn, parent_id, "completed", result=result)
    conn.execute(
        "UPDATE tasks SET talk_response_id = ? WHERE id = ?",
        (talk_id, parent_id),
    )
    conn.commit()
    return parent_id


class TestDetectNotificationReply:
    def _reply(self, conn, reply_to_talk_id=42):
        reply_id = db.create_task(
            conn, prompt="Thanks", user_id="alice",
            source_type="talk", conversation_token="room1",
            reply_to_talk_id=reply_to_talk_id,
        )
        return db.get_task(conn, reply_id)

    @pytest.mark.parametrize("source_type, is_notification", [
        ("scheduled", True), ("briefing", True), ("talk", False),
    ])
    def test_reply_to_a_parent(self, tmp_path, source_type, is_notification):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            parent_id = _notified_parent(
                conn, source_type=source_type, result="Time to drink water!",
            )
            result = _detect_notification_reply(self._reply(conn), Config(), conn)
            if is_notification:
                assert result is not None
                assert result.id == parent_id
                assert result.source_type == source_type
            else:
                assert result is None

    def test_returns_none_when_no_reply_to_talk_id(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            task = self._reply(conn, reply_to_talk_id=None)
            assert _detect_notification_reply(task, Config(), conn) is None

    def test_returns_none_when_no_conn(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            task = self._reply(conn)
        assert _detect_notification_reply(task, Config(), None) is None


# ---------------------------------------------------------------------------
# TestNotificationReplyContextScoping
# ---------------------------------------------------------------------------


@patch("istota.executor.subprocess.run")
class TestNotificationReplyContextScoping:
    def _run_reply(self, tmp_path, mock_run, *, parent_type, parent_result):
        config = _skills_config(tmp_path)
        with db.get_db(config.db_path) as conn:
            _notified_parent(conn, source_type=parent_type, result=parent_result)
        _execute(
            config, mock_run, prompt="Drinking",
            conversation_token="room1", reply_to_talk_id=42,
        )
        return mock_run.call_args.kwargs["input"]

    def test_notification_reply_scopes_context(self, mock_run, tmp_path):
        """Reply to a scheduled notification gets scoped context, not full
        history, and skips the full Talk context fetch."""
        with patch("istota.executor._build_talk_api_context") as mock_talk_ctx:
            prompt_text = self._run_reply(
                tmp_path, mock_run, parent_type="scheduled",
                parent_result="Time to hydrate! Remember to drink water.",
            )
        mock_talk_ctx.assert_not_called()
        assert "replying to a scheduled notification" in prompt_text
        assert "respond very briefly" in prompt_text
        assert "Time to hydrate" in prompt_text

    def test_non_notification_reply_uses_normal_context(self, mock_run, tmp_path):
        """Reply to a regular talk message should use normal context loading."""
        with patch("istota.executor._build_talk_api_context") as mock_talk_ctx:
            mock_talk_ctx.return_value = (None, set())  # Fall through to DB context
            prompt_text = self._run_reply(
                tmp_path, mock_run, parent_type="talk", parent_result="It's sunny!",
            )
        mock_talk_ctx.assert_called_once()
        assert "replying to a scheduled notification" not in prompt_text


# ---------------------------------------------------------------------------
# TestRecencyWindow
# ---------------------------------------------------------------------------


def _recency_config(recency_hours=2.0, min_messages=10):
    from istota.config import ConversationConfig
    config = Config()
    config.conversation = ConversationConfig(
        context_recency_hours=recency_hours,
        context_min_messages=min_messages,
    )
    return config


class TestRecencyWindowTalk:
    def _make_talk_msg(self, message_id, timestamp, content="msg"):
        return db.TalkMessage(
            message_id=message_id,
            actor_id="alice",
            actor_display_name="Alice",
            is_bot=False,
            content=content,
            timestamp=timestamp,
            actions_taken=None,
            message_role="user",
            task_id=None,
        )

    def test_disabled_when_zero(self):
        config = _recency_config(recency_hours=0)
        msgs = [self._make_talk_msg(i, 1000 + i) for i in range(20)]
        assert len(_apply_recency_window_talk(msgs, config)) == 20

    def test_empty_messages(self):
        assert _apply_recency_window_talk([], _recency_config()) == []

    def test_fewer_than_min_returns_all(self):
        config = _recency_config(min_messages=10)
        msgs = [self._make_talk_msg(i, 1000 + i) for i in range(8)]
        assert len(_apply_recency_window_talk(msgs, config)) == 8

    def test_all_within_window_returns_all(self):
        config = _recency_config(recency_hours=2.0, min_messages=5)
        now = 1000000
        # 15 messages all within last hour
        msgs = [self._make_talk_msg(i, now - (15 - i) * 60) for i in range(15)]
        assert len(_apply_recency_window_talk(msgs, config)) == 15

    def test_trims_old_messages_beyond_min(self):
        config = _recency_config(recency_hours=2.0, min_messages=5)
        now = 1000000
        # 5 messages from 10 hours ago
        old = [self._make_talk_msg(i, now - 36000 + i) for i in range(5)]
        # 10 messages from last 30 minutes
        recent = [self._make_talk_msg(10 + i, now - (10 - i) * 60) for i in range(10)]
        result = _apply_recency_window_talk(old + recent, config)
        # guaranteed = last 5, older = first 10; of those only the 5 recent are
        # within the window.
        assert len(result) == 10

    def test_guaranteed_minimum_always_kept(self):
        config = _recency_config(recency_hours=1.0, min_messages=10)
        now = 1000000
        old_msgs = [self._make_talk_msg(i, now - 50000 + i * 100) for i in range(15)]
        recent_msgs = [self._make_talk_msg(15 + i, now - 60 + i * 10) for i in range(5)]
        result = _apply_recency_window_talk(old_msgs + recent_msgs, config)
        # 10 guaranteed (last 10), older 10 checked against window
        # window = newest - 3600, old msgs are ~50000s ago, way outside
        # So result = 10 guaranteed minimum
        assert len(result) == 10

    def test_partial_window_inclusion(self):
        """Some older messages within window, some outside."""
        config = _recency_config(recency_hours=1.0, min_messages=3)
        now = 1000000
        # 2 messages from 5 hours ago (outside window)
        outside = [self._make_talk_msg(i, now - 18000 + i) for i in range(2)]
        # 3 messages from 30 minutes ago (within window)
        inside = [self._make_talk_msg(10 + i, now - 1800 + i * 60) for i in range(3)]
        # 3 messages from 5 minutes ago (guaranteed min)
        recent = [self._make_talk_msg(20 + i, now - 300 + i * 60) for i in range(3)]
        result = _apply_recency_window_talk(outside + inside + recent, config)
        assert len(result) == 6  # 3 inside + 3 guaranteed


class TestRecencyWindowDb:
    def _make_msg(self, msg_id, created_at, prompt="q", result="a"):
        return db.ConversationMessage(
            id=msg_id, prompt=prompt, result=result, created_at=created_at,
        )

    def test_disabled_when_zero(self):
        config = _recency_config(recency_hours=0)
        msgs = [self._make_msg(i, "2026-02-23 12:00:00") for i in range(20)]
        assert len(_apply_recency_window_db(msgs, config)) == 20

    def test_empty_returns_empty(self):
        assert _apply_recency_window_db([], _recency_config()) == []

    def test_fewer_than_min_returns_all(self):
        config = _recency_config(min_messages=10)
        msgs = [self._make_msg(i, f"2026-02-23 12:0{i}:00") for i in range(5)]
        assert len(_apply_recency_window_db(msgs, config)) == 5

    def test_trims_old_db_messages(self):
        config = _recency_config(recency_hours=1.0, min_messages=3)
        msgs = [
            self._make_msg(1, "2026-02-23 08:00:00"),  # 4h before newest
            self._make_msg(2, "2026-02-23 09:00:00"),  # 3h before newest
            self._make_msg(3, "2026-02-23 11:30:00"),  # 30m before newest
            self._make_msg(4, "2026-02-23 11:45:00"),  # 15m before newest
            self._make_msg(5, "2026-02-23 12:00:00"),  # newest
        ]
        result = _apply_recency_window_db(msgs, config)
        # min=3 guaranteed (ids 3,4,5), older=[1,2], 1 and 2 are >1h old
        assert [m.id for m in result] == [3, 4, 5]

    def test_keeps_within_window_beyond_min(self):
        config = _recency_config(recency_hours=2.0, min_messages=2)
        msgs = [
            self._make_msg(1, "2026-02-23 08:00:00"),  # outside
            self._make_msg(2, "2026-02-23 10:30:00"),  # within 2h
            self._make_msg(3, "2026-02-23 11:00:00"),  # within 2h
            self._make_msg(4, "2026-02-23 11:30:00"),  # guaranteed
            self._make_msg(5, "2026-02-23 12:00:00"),  # guaranteed (newest)
        ]
        result = _apply_recency_window_db(msgs, config)
        # guaranteed = [4,5], older = [1,2,3], within window = [2,3]
        assert [m.id for m in result] == [2, 3, 4, 5]

    def test_unparseable_created_at_skips_filter(self):
        config = _recency_config(recency_hours=1.0, min_messages=2)
        msgs = [self._make_msg(i, "not-a-date") for i in range(5)]
        # Can't parse newest, returns all
        assert len(_apply_recency_window_db(msgs, config)) == 5


# ---------------------------------------------------------------------------
# TestBuildPromptRecalledMemories
# ---------------------------------------------------------------------------


class TestBuildPromptRecalledMemories:
    def _prompt(self, **kw):
        task = db.Task(
            id=1, prompt="test prompt", user_id="alice",
            source_type="talk", status="running",
        )
        return build_prompt(task, [], Config(), **kw).user

    def test_recalled_section_included_when_provided(self):
        prompt = self._prompt(
            recalled_memories="- [memory_file] User prefers dark mode\n- [conversation] Discussed project X",
        )
        assert "Recalled memories (from search)" in prompt
        assert "User prefers dark mode" in prompt
        assert "Discussed project X" in prompt

    @pytest.mark.parametrize("recalled", [None, ""])
    def test_recalled_section_absent_when_empty(self, recalled):
        assert "Recalled memories" not in self._prompt(recalled_memories=recalled)

    def test_recalled_section_after_dated_memories(self):
        prompt = self._prompt(
            dated_memories="- Dated memory entry",
            recalled_memories="- Recalled entry",
        )
        dated_pos = prompt.index("Recent context (from previous days)")
        recalled_pos = prompt.index("Recalled memories (from search)")
        assert dated_pos < recalled_pos


# ---------------------------------------------------------------------------
# TestRecallMemories
# ---------------------------------------------------------------------------


class TestRecallMemories:
    def _config(self, enabled=True, auto_recall=True, with_db=True, **kw):
        from istota.config import MemorySearchConfig
        config_kw = {"db_path": Path("/tmp/test.db")} if with_db else {}
        return Config(
            memory_search=MemorySearchConfig(
                enabled=enabled, auto_recall=auto_recall, **kw,
            ),
            **config_kw,
        )

    def _task(self, **kw):
        return db.Task(
            id=1, prompt="test", user_id="alice", source_type="talk",
            status="running", **kw,
        )

    @pytest.mark.parametrize("enabled, auto_recall, skip_memory", [
        (True, False, False), (False, True, False), (True, True, True),
    ], ids=["auto_recall_off", "search_off", "skip_memory"])
    def test_returns_none_when_off(self, enabled, auto_recall, skip_memory):
        from istota.executor import _recall_memories
        config = self._config(enabled=enabled, auto_recall=auto_recall, with_db=False)
        task = self._task()
        kw = {"skip_memory": True} if skip_memory else {}
        assert _recall_memories(config, None, task, task.prompt, **kw) is None

    @patch("istota.memory.search.search")
    def test_formats_results(self, mock_search):
        from istota.executor import _recall_memories

        mock_result = MagicMock()
        mock_result.content = "User likes Python"
        mock_result.source_type = "memory_file"
        mock_search.return_value = [mock_result]

        task = self._task()
        result = _recall_memories(
            self._config(auto_recall_limit=5), MagicMock(), task, "what language?",
        )
        assert result is not None
        assert "[memory_file]" in result
        assert "User likes Python" in result

    @patch("istota.memory.search.search")
    def test_returns_none_when_no_results(self, mock_search):
        from istota.executor import _recall_memories

        mock_search.return_value = []
        task = self._task()
        assert _recall_memories(self._config(), MagicMock(), task, task.prompt) is None

    @patch("istota.memory.search.search")
    def test_includes_channel_in_search(self, mock_search):
        from istota.executor import _recall_memories

        mock_search.return_value = []
        task = self._task(conversation_token="room123")
        _recall_memories(self._config(), MagicMock(), task, task.prompt)
        call_kwargs = mock_search.call_args[1]
        assert call_kwargs["include_user_ids"] == ["channel:room123"]


# ---------------------------------------------------------------------------
# TestApplyMemoryCap
# ---------------------------------------------------------------------------


class TestApplyMemoryCap:
    @pytest.mark.parametrize("cap", [0, 500], ids=["unlimited", "under_cap"])
    def test_no_truncation(self, cap):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=cap)
        u, d, c, r, k, _pb = _apply_memory_cap(config, "A" * 100, "B" * 100, "C" * 100, "D" * 100)
        assert [len(u), len(d), len(c), len(r)] == [100, 100, 100, 100]

    def test_truncates_recalled_first(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=200)
        # total = 300, cap = 200, over = 100, recalled = 100 → removed entirely
        u, d, c, r, k, _pb = _apply_memory_cap(config, "A" * 100, "B" * 100, None, "D" * 100)
        assert u == "A" * 100
        assert d == "B" * 100
        assert r is None

    def test_truncates_dated_after_recalled(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=100)
        # total = 300, cap = 100, over = 200
        # recalled (100) removed → over = 100
        # dated (100) removed → over = 0
        u, d, c, r, k, _pb = _apply_memory_cap(config, "A" * 100, "B" * 100, None, "D" * 100)
        assert u == "A" * 100
        assert d is None
        assert r is None

    def test_partial_truncation(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=250)
        # total = 300, cap = 250, over = 50
        # recalled (100) → trim to 50 chars + truncation marker
        u, d, c, r, k, _pb = _apply_memory_cap(config, "A" * 100, "B" * 100, None, "D" * 100)
        assert u == "A" * 100
        assert d == "B" * 100
        assert r is not None
        assert "truncated" in r

    def test_handles_all_none(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=100)
        u, d, c, r, k, _pb = _apply_memory_cap(config, None, None, None, None)
        assert u is None and d is None and c is None and r is None

    def test_group_memory_counts_toward_the_cap(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=250)
        # Without the group block: 200 <= 250, nothing cut. With it: 300.
        u, d, c, r, k, _pb = _apply_memory_cap(
            config, "A" * 100, None, None, "D" * 100, group_memory="G" * 100,
        )
        assert u == "A" * 100
        assert r is not None and "truncated" in r

    def test_group_memory_is_never_truncated_and_is_named(self, caplog):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=50)
        with caplog.at_level(logging.WARNING, logger="istota.executor"):
            _apply_memory_cap(config, None, None, None, None, group_memory="G" * 100)
        assert any("group_memory=100" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# TestDatedMemoriesAutoLoad
# ---------------------------------------------------------------------------


@patch("istota.executor.subprocess.run")
class TestDatedMemoriesAutoLoad:
    def _make_config(self, tmp_path, auto_load_days=3, sleep_enabled=True, memory=None):
        from datetime import datetime

        from istota.config import SleepCycleConfig
        config = _skills_config(
            tmp_path, files_skill=False, mount=True,
            sleep_cycle=SleepCycleConfig(
                enabled=sleep_enabled,
                auto_load_dated_days=auto_load_days,
            ),
        )
        (config.skills_dir / "_index.toml").write_text("")
        if memory is not None:
            memories_dir = config.workspace_path / "Users" / "alice" / "memories"
            memories_dir.mkdir(parents=True)
            today = datetime.now().strftime("%Y-%m-%d")
            (memories_dir / f"{today}.md").write_text(memory)
        return config

    def _prompt(self, config, mock_run, source_type="talk"):
        _execute(config, mock_run, source_type=source_type)
        return mock_run.call_args.kwargs["input"]

    def test_dated_memories_loaded_when_enabled(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, memory="- User prefers dark mode")
        prompt_text = self._prompt(config, mock_run)
        assert "User prefers dark mode" in prompt_text
        assert "Recent context (from previous days)" in prompt_text

    def test_dated_memories_skipped_for_briefing(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, memory="- Should not appear")
        # Add briefing skill with exclude_memory so flag-based check works
        briefing_dir = config.skills_dir / "briefing"
        briefing_dir.mkdir(parents=True)
        (briefing_dir / "skill.toml").write_text(
            'description = "Briefing"\nsource_types = ["briefing"]\nexclude_memory = true\n'
        )
        assert "Should not appear" not in self._prompt(config, mock_run, "briefing")

    @pytest.mark.parametrize("auto_load_days, sleep_enabled, memory", [
        (0, True, "- Should not appear"), (3, False, None),
    ], ids=["zero_days", "sleep_disabled"])
    def test_dated_memories_none(
        self, mock_run, tmp_path, auto_load_days, sleep_enabled, memory,
    ):
        config = self._make_config(
            tmp_path, auto_load_days=auto_load_days,
            sleep_enabled=sleep_enabled, memory=memory,
        )
        assert "Recent context (from previous days)" not in self._prompt(config, mock_run)


# =============================================================================
# TestConfirmationContext
# =============================================================================


class TestConfirmationContext:
    def _prompt(self, tmp_path, confirmation_context):
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        config = Config(
            db_path=tmp_path / "test.db",
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
        )
        task = db.Task(
            id=1, status="running", source_type="email",
            user_id="carol", prompt="Emissary reply from bob@ext.com",
            conversation_token="room1",
        )
        return build_prompt(
            task, [], config, confirmation_context=confirmation_context,
        ).user

    def test_confirmation_context_included_before_the_request(self, tmp_path):
        prompt = self._prompt(
            tmp_path,
            "I drafted a reply: 'How about Tuesday at 3pm?' Should I send this?",
        )
        assert "## Confirmed action" in prompt
        assert "How about Tuesday at 3pm?" in prompt
        assert "Do not re-draft" in prompt
        assert "`istota-skill email send`" in prompt
        assert prompt.index("## Confirmed action") < prompt.index("## User's request")

    def test_no_confirmation_context_when_none(self, tmp_path):
        assert "## Confirmed action" not in self._prompt(tmp_path, None)


# ---------------------------------------------------------------------------
# TestDetectMalformedResult
# ---------------------------------------------------------------------------


_XML_IN_PROSE = (
    "The model produced an error with </parameter> tags. "
    "This is a known issue when context pressure causes problems."
)


class TestDetectMalformedResult:
    """Test detection of malformed model output (leaked XML, disproportionately short)."""

    @pytest.mark.parametrize("text", [
        "Here are three painting studios in Lisbon...",
        "Done.",
        "",
        None,
        "   \n  ",
        # XML patterns embedded in a substantive response.
        (
            "The model produced an error with </parameter> tags. "
            "This is a known issue when context pressure causes the model to emit "
            "raw XML fragments instead of coherent responses. Here is the analysis..."
        ),
    ], ids=["normal", "short", "empty", "none", "whitespace", "xml_in_long_response"])
    def test_passes(self, text):
        assert detect_malformed_result(text) is None

    @pytest.mark.parametrize("text", [
        "</parameter>\n</invoke>",
        "</invoke>",
        "<invoke name='foo'>",
        "</thinking>",
        "<parameter name='path'>",
    ], ids=["parameter_close", "invoke_close", "invoke_open", "antml_prefix", "parameter_open"])
    def test_leaked_xml_detected(self, text):
        result = detect_malformed_result(text)
        assert result is not None
        assert "leaked tool-call XML" in result

    # --- Strict mode (output_target="talk") ---

    def test_talk_xml_in_prose_detected(self):
        """XML patterns embedded in prose should be caught in strict Talk mode."""
        # Non-strict: passes (enough non-syntax content)
        assert detect_malformed_result(_XML_IN_PROSE) is None
        # Strict (Talk): flagged
        result = detect_malformed_result(_XML_IN_PROSE, output_target="talk")
        assert result is not None
        assert "Talk output" in result

    @pytest.mark.parametrize("text", [
        # XML patterns inside code fences.
        (
            "Here's an example of the XML format:\n\n"
            "```xml\n<parameter name='path'>/foo</parameter>\n```\n\n"
            "This shows the structure."
        ),
        "## Results\n\n- Item one\n- Item two\n\nHere's a **bold** conclusion.",
    ], ids=["xml_in_code_fence", "clean_markdown"])
    def test_talk_passes(self, text):
        assert detect_malformed_result(text, output_target="talk") is None

    def test_talk_xml_outside_fence_with_fenced_xml_detected(self):
        """XML outside code fences should be caught even if fenced XML exists."""
        text = (
            "```xml\n<parameter>ok</parameter>\n```\n\n"
            "And then </invoke> happened."
        )
        assert detect_malformed_result(text, output_target="talk") is not None

    @pytest.mark.parametrize("target", ["both", "all"])
    def test_multi_target_uses_strict_mode(self, target):
        text = "Something </invoke> happened"
        assert detect_malformed_result(text, output_target=target) is not None

    def test_email_target_uses_lenient_mode(self):
        """Email target should use lenient mode (XML patterns allowed in longer text)."""
        assert detect_malformed_result(_XML_IN_PROSE, output_target="email") is None


# ---------------------------------------------------------------------------
# TestComposeFullResult
# ---------------------------------------------------------------------------


def _make_task(
    *,
    source_type: str = "talk",
    heartbeat_silent: bool = False,
    scheduled_job_id=None,
    task_id: int = 1,
):
    """Build a Task for compose tests. Only the fields _is_automated_task
    actually reads need to be set."""
    return _db.Task(
        id=task_id,
        status="running",
        source_type=source_type,
        user_id="test_user",
        prompt="",
        conversation_token="",
        heartbeat_silent=heartbeat_silent,
        scheduled_job_id=scheduled_job_id,
    )


def _block(prefix: str, target_chars: int) -> str:
    """Build a substantive text block of approximately target_chars."""
    sentence = (
        f"{prefix} The data shows a clear pattern, with consistent measurements "
        f"across the observed window and reasonable confidence in the result. "
    )
    n = target_chars // len(sentence) + 1
    return (sentence * n).strip()


def _narrated(text, tool="Write file"):
    """A text block followed by one tool call: the text is pre-tool."""
    return [{"type": "text", "text": text}, {"type": "tool", "text": tool}]


class TestComposeFullResult:
    """Mechanism B (terse-recovery) — tests against the redesigned function."""

    # --- pass-through cases ---

    def test_no_trace_returns_result_as_is(self):
        assert _compose_full_result("Done.", []) == "Done."

    def test_no_substantial_blocks_returns_result(self):
        trace = [
            {"type": "text", "text": "Let me check."},
            {"type": "tool", "text": "Read file.py"},
            {"type": "text", "text": "Running the search."},
        ]
        assert _compose_full_result("Done.", trace) == "Done."

    def test_substantial_result_not_overridden(self):
        """A non-terse result must never be replaced — the regression test
        for the 2026-05-08 incident: 5KB skill-doc preamble + 900-char real
        summary previously got concatenated."""
        preamble = _block("Preamble.", 5000)
        real_summary = _block("Summary.", 900)
        trace = [
            {"type": "text", "text": preamble},
            {"type": "tool", "text": "git log"},
            {"type": "tool", "text": "Read file"},
        ]
        assert _compose_full_result(real_summary, trace) == real_summary

    def test_empty_trace_entries_ignored(self):
        trace = [
            {"type": "text", "text": ""},
            {"type": "text", "text": "   "},
        ]
        assert _compose_full_result("Done.", trace) == "Done."

    def test_substantial_no_tools_no_recovery(self):
        """A substantial result with no tool boundary in trace: still no
        override — gate is on terseness, not trace shape."""
        trace = [{"type": "text", "text": _block("Findings.", 800)}]
        long_result = _block("Result.", 400)
        assert _compose_full_result(long_result, trace) == long_result

    # --- terse-pattern recovery ---

    @pytest.mark.parametrize("result", ["See above.", "Done."])
    def test_back_reference_with_substantial_pre_tool_region(self, result):
        """Canonical ISSUE-025 shape: substantial text → tool → terse result.
        "See above" points at earlier text, so it reaches past the tool."""
        findings = _block("Findings.", 800)
        trace = _narrated(findings)
        assert _compose_full_result(result, trace, task=_make_task()) == findings

    def test_terse_short_result_does_not_reach_back_past_a_tool(self):
        """A short result that isn't an explicit back-reference is a real (if
        brief) answer. Reaching back past a tool call for it would promote
        mid-turn narration — ISSUE-211. Only the trailing region qualifies."""
        trace = _narrated(_block("Findings.", 800))
        result = _compose_full_result(
            "Operation completed.", trace, task=_make_task(),
        )
        assert result == "Operation completed."

    @pytest.mark.parametrize("result", ["Operation completed.", ""])
    def test_terse_result_with_substantial_trailing_region(self, result):
        """The region *after* the last tool call is the model's final message,
        so it wins over a short result that is not a known reference, and over
        an empty one (the brain lost the final message; the trace has it)."""
        findings = _block("Findings.", 800)
        trace = [
            {"type": "tool", "text": "Write file"},
            {"type": "text", "text": findings},
        ]
        assert _compose_full_result(result, trace, task=_make_task()) == findings

    # --- terse but no qualifying region ---

    def test_terse_result_short_trailing_region_no_override(self):
        """Trailing region must be ≥ TRAILING_REGION_MIN_CHARS to override."""
        short_block = "Brief note about the result. " * 5  # ~145 chars
        result = _compose_full_result(
            "See above.", _narrated(short_block), task=_make_task(),
        )
        # Region < 500 chars → no override
        assert result == "See above."

    def test_terse_result_region_already_in_result(self):
        """If the trailing region appears verbatim in result_text, no override."""
        block = _block("Findings.", 800)
        trace = [{"type": "text", "text": block}]
        # Result already contains the region (followed by a tag) — no override
        embedded = block + "\n\n[done]"
        assert _compose_full_result(embedded, trace, task=_make_task()) == embedded

    # --- streaming fragment aggregation ---

    def test_streaming_fragments_aggregate_into_one_region(self):
        """Many small text events between trace boundaries should aggregate."""
        # 12 fragments × ~50 chars = ~600 chars total, joined with \n\n
        fragments = [
            f"Fragment {i}: more detail about the analysis goes here. "
            for i in range(12)
        ]
        trace = [
            *({"type": "text", "text": f} for f in fragments),
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result("See above.", trace, task=_make_task())
        # Should be the joined fragments, not the terse result
        assert "Fragment 0" in result
        assert "Fragment 11" in result
        assert result != "See above."

    # --- automated-task gate ---

    @pytest.mark.parametrize("task_kw", [
        {"source_type": "scheduled"},
        {"source_type": "briefing"},
        # The flags gate Mechanism B even when source_type isn't in the
        # explicit set.
        {"source_type": "cli", "heartbeat_silent": True},
        {"source_type": "cli", "scheduled_job_id": 42},
    ], ids=["scheduled", "briefing", "heartbeat_silent", "scheduled_job_id"])
    def test_automated_task_no_terse_recovery(self, task_kw):
        """Mechanism B is gated for automated tasks regardless of trace."""
        trace = _narrated(_block("Findings.", 800))
        result = _compose_full_result(
            "See above.", trace, task=_make_task(**task_kw),
        )
        assert result == "See above."

    def test_no_task_means_no_automated_gate(self):
        """Backwards-compat: callers passing no task get the original gating
        behavior (no automated-task gate fires)."""
        findings = _block("Findings.", 800)
        assert _compose_full_result("See above.", _narrated(findings)) == findings

    # --- regression — 2026-05-08 incident ---

    def test_regression_5KB_preamble_900_char_summary_scheduled(self):
        """The 2026-05-08 cron incident: 5KB skill-doc preamble + 900-char
        real summary on a scheduled task. Both gates (substantial result AND
        scheduled source_type) must independently block override."""
        preamble = _block("Skill enumeration.", 5000)
        real_summary = _block("Daily devlog summary.", 900)
        trace = [
            {"type": "text", "text": preamble},
            {"type": "tool", "text": "git log"},
            {"type": "tool", "text": "Read DEVLOG.md"},
        ]
        result = _compose_full_result(
            real_summary, trace, task=_make_task(source_type="scheduled"),
        )
        assert result == real_summary
        assert "Skill enumeration." not in result


class TestComposeFullResultCM:
    """Mechanism A (CM-aware) — segmentation by cm_boundary."""

    def test_cm_boundary_uses_last_substantial_segment(self):
        pre_cm = _block("PreCM.", 450)
        post_cm = _block("PostCM.", 450)
        trace = [
            {"type": "text", "text": pre_cm},
            {"type": "cm_boundary"},
            {"type": "text", "text": post_cm},
        ]
        doubled_result = f"{pre_cm}\n\n{post_cm}"
        assert _compose_full_result(doubled_result, trace) == post_cm

    def test_cm_boundary_with_thin_last_segment_trusts_result(self):
        trace = [
            {"type": "text", "text": "Let me check."},
            {"type": "cm_boundary"},
            {"type": "tool", "text": "Read file"},
            {"type": "cm_boundary"},
            {"type": "text", "text": "Now let me write the patch."},
        ]
        good_result = _block("Result.", 450)
        assert _compose_full_result(good_result, trace) == good_result

    def test_cm_boundary_with_tools_after_last_cm(self):
        real_response = _block("Response.", 450)
        trace = [
            {"type": "text", "text": real_response},
            {"type": "cm_boundary"},
            {"type": "tool", "text": "Write file"},
            {"type": "tool", "text": "Edit config"},
        ]
        # Last segment has no text (only tools) → walk back to pre-CM real_response.
        # Equal to result_text (after strip), so we return result_text unchanged.
        assert _compose_full_result(real_response, trace) == real_response

    def test_cm_boundary_empty_last_segment_trusts_result(self):
        real_response = _block("Response.", 450)
        trace = [
            {"type": "text", "text": real_response},
            {"type": "cm_boundary"},
        ]
        assert _compose_full_result(real_response, trace) == real_response

    def test_multiple_cm_boundaries_uses_last_substantial(self):
        block1 = _block("Block1.", 450)
        block2 = _block("Block2.", 450)
        trace = [
            {"type": "text", "text": block1},
            {"type": "cm_boundary"},
            {"type": "text", "text": "Let me rethink."},
            {"type": "cm_boundary"},
            {"type": "text", "text": block2},
            {"type": "cm_boundary"},
        ]
        doubled = f"{block1}\n\n{block2}"
        assert _compose_full_result(doubled, trace) == block2

    def test_cm_with_multiple_texts_in_last_segment(self):
        block1 = _block("BlockA.", 450)
        block2 = _block("BlockB.", 450)
        trace = [
            {"type": "text", "text": "Old analysis."},
            {"type": "cm_boundary"},
            {"type": "text", "text": block1},
            {"type": "text", "text": block2},
        ]
        # Adjacent text blocks with nothing between them are one streamed
        # message and are joined.
        result = _compose_full_result("Doubled.", trace)
        assert result == f"{block1}\n\n{block2}"

    def test_cm_segment_split_by_a_tool_keeps_only_the_trailing_part(self):
        """The original ISSUE-026 fixture, kept with its new expectation.

        "A tool is NOT a CM-mode delimiter" was the documented property before
        ISSUE-211; the finality rule deliberately revokes it, so the same shape
        now yields the post-tool block alone rather than both joined.
        """
        block1 = _block("BlockA.", 450)
        block2 = _block("BlockB.", 450)
        trace = [
            {"type": "text", "text": "Old analysis."},
            {"type": "cm_boundary"},
            {"type": "text", "text": block1},
            {"type": "tool", "text": "Read file"},
            {"type": "text", "text": block2},
        ]
        assert _compose_full_result("Doubled.", trace) == block2

    def test_cm_answer_split_by_a_trailing_tool_falls_back_to_result(self):
        """The cost of the revocation, pinned deliberately: an answer split by
        a trailing tool call whose tail is under the CM floor recovers nothing
        and keeps the (CM-truncated) result rather than gluing the halves."""
        part_a = _block("PartA.", 450)
        trace = [
            {"type": "cm_boundary"},
            {"type": "text", "text": part_a},
            {"type": "tool", "text": "Read file"},
            {"type": "text", "text": "Short tail."},
        ]
        assert _compose_full_result("CM-truncated result.", trace) == "CM-truncated result."

    def test_cm_recovery_stops_at_the_last_tool_call(self):
        """ISSUE-211: a block the model wrote *before* issuing another tool
        call is mid-turn narration, not part of the final message, so CM
        recovery must not glue it onto the answer."""
        narration = _block("Let me look this up.", 450)
        answer = _block("Answer.", 450)
        trace = [
            {"type": "text", "text": "Old analysis."},
            {"type": "cm_boundary"},
            {"type": "text", "text": narration},
            {"type": "tool", "text": "Read file"},
            {"type": "text", "text": answer},
        ]
        result = _compose_full_result("Doubled.", trace)
        assert result == answer

    def test_cm_real_pattern_pre_and_post_cm_responses(self):
        pre_cm = (
            "Found it. The issue is clear from the trace data. "
            "The current fix handles two things correctly: "
            "filtering CM replay events and deduplicating block IDs. "
            "But it misses the case where CM fires between two "
            "legitimate text events with different message IDs. "
            "Both get through because neither has context_management set."
        )
        post_cm = (
            "Found the issue. Let me trace through what happened. "
            "The trace has two text entries — the analysis and the "
            "conclusion. The result text from Claude Code contains "
            "everything concatenated. The compose function needs "
            "CM-aware segmentation to pick the right version."
        )
        trace = [
            {"type": "tool", "text": "Read stream_parser.py"},
            {"type": "tool", "text": "Read executor.py"},
            {"type": "text", "text": pre_cm},
            {"type": "cm_boundary"},
            {"type": "text", "text": post_cm},
            {"type": "cm_boundary"},
        ]
        doubled_result = f"{post_cm}\n\n{pre_cm}"
        assert _compose_full_result(doubled_result, trace) == post_cm

    def test_cm_aware_runs_for_scheduled_tasks(self):
        """The source-type gate is Mechanism-B-only; CM-aware always runs."""
        pre_cm = _block("PreCM.", 450)
        post_cm = _block("PostCM.", 450)
        trace = [
            {"type": "text", "text": pre_cm},
            {"type": "cm_boundary"},
            {"type": "text", "text": post_cm},
        ]
        doubled_result = f"{pre_cm}\n\n{post_cm}"
        result = _compose_full_result(
            doubled_result, trace, task=_make_task(source_type="scheduled"),
        )
        assert result == post_cm

    def test_cm_recovered_equals_result_no_override(self):
        """When the last substantial segment IS result_text after strip,
        no override (avoids no-op log entries)."""
        block = _block("Block.", 450)
        trace = [
            {"type": "text", "text": block},
            {"type": "cm_boundary"},
        ]
        # No segment after final CM has text; walking back finds `block`.
        # If result_text is exactly block, no override.
        assert _compose_full_result(block, trace) == block


class TestFinalAnswerGuard:
    """ISSUE-211 — mid-turn narration must never become the durable reply.

    The guidelines promise the model that text written between tool calls is a
    live progress indicator and is not the saved answer. Recovery may promote
    a region the model wrote *after* its last tool call (that is its final
    message, just missing from the brain's result), and may reach further back
    only when the result is an explicit back-reference ("see above") — there
    the model itself says the answer is earlier. The back-reference half is
    `TestComposeFullResult::test_back_reference_with_substantial_pre_tool_region`.
    """

    def test_short_answer_is_not_replaced_by_pre_tool_narration(self):
        trace = _narrated(_block("Let me check the calendar.", 800), "Read calendar")
        result = _compose_full_result(
            "Your meeting is at 3pm.", trace, task=_make_task(),
        )
        assert result == "Your meeting is at 3pm."

    @pytest.mark.parametrize("narration", [
        _block("Let me check the calendar.", 800),
        # Even a short partial is carried.
        "Let me check the calendar.",
    ], ids=["substantial", "short_partial"])
    def test_empty_answer_labels_narration_instead_of_promoting_it(self, narration):
        trace = _narrated(narration, "Read calendar")
        result = _compose_full_result("", trace, task=_make_task())
        assert result != narration
        assert result.startswith(_NO_FINAL_ANSWER_NOTICE)
        # The work isn't thrown away — it is labelled as progress, not answer.
        assert narration in result

    @pytest.mark.parametrize("answer", [
        _block("Here is the answer.", 600),
        # A brief final message the brain lost is still the answer — the size
        # floors protect a non-empty result, and there is none here.
        "Your meeting is at 3pm.",
    ], ids=["substantial", "short"])
    def test_trailing_region_after_last_tool_is_adopted(self, answer):
        trace = _narrated(_block("Checking.", 600), "Read calendar")
        trace.append({"type": "text", "text": answer})
        assert _compose_full_result("", trace, task=_make_task()) == answer

    def test_cm_recovery_does_not_reach_back_past_a_tool(self):
        trace = [
            {"type": "text", "text": _block("Let me look this up.", 450)},
            {"type": "cm_boundary"},
            {"type": "tool", "text": "Write file"},
        ]
        assert _compose_full_result("Saved.", trace, task=_make_task()) == "Saved."

    @pytest.mark.parametrize("result", ["", "   \n "], ids=["empty", "whitespace_only"])
    def test_empty_result_with_no_trace_yields_the_notice(self, result):
        assert _compose_full_result(result, [], task=_make_task()) == _NO_FINAL_ANSWER_NOTICE

    def test_automated_task_empty_result_left_alone(self):
        """A briefing's body is parsed as JSON and an empty result flows to the
        existing quiet retry — a prose notice would be parsed as the body."""
        trace = _narrated(_block("Narration.", 800), "Read feed")
        assert _compose_full_result(
            "", trace, task=_make_task(source_type="briefing"),
        ) == ""

    def test_real_answer_never_replaced_by_the_notice(self):
        assert _compose_full_result(
            "The answer.", [], task=_make_task(),
        ) == "The answer."


class TestComposeHelpers:
    """Direct tests for the helper predicates."""

    @pytest.mark.parametrize("text", [
        "Done.", "Done", "OK", "✓", "", "   ", "See above.", "see above", "SEE ABOVE",
    ])
    def test_is_terse(self, text):
        assert _is_terse(text)

    def test_is_terse_substantial_text_not_terse(self):
        long_text = "A" * (_TERSE_RESULT_MAX_CHARS + 1)
        assert not _is_terse(long_text)

    @pytest.mark.parametrize("task, expected", [
        (None, False),
        (_make_task(source_type="scheduled"), True),
        (_make_task(source_type="briefing"), True),
        (_make_task(source_type="talk"), False),
        (_make_task(source_type="email"), False),
        (_make_task(source_type="subtask"), False),
        (_make_task(source_type="cli", heartbeat_silent=True), True),
        (_make_task(source_type="cli", scheduled_job_id=42), True),
    ], ids=[
        "none", "scheduled", "briefing", "talk", "email", "subtask",
        "heartbeat_silent_flag", "scheduled_job_id_flag",
    ])
    def test_is_automated_task(self, task, expected):
        assert bool(_is_automated_task(task)) is expected

    def test_last_substantial_region_empty_trace(self):
        assert _last_substantial_region([], {"tool"}, 100) is None

    def test_last_substantial_region_no_qualifying_region(self):
        trace = [
            {"type": "text", "text": "tiny"},
            {"type": "tool", "text": "Read"},
            {"type": "text", "text": "also tiny"},
        ]
        assert _last_substantial_region(trace, {"tool"}, 500) is None

    def test_last_substantial_region_returns_last_substantial(self):
        block1 = _block("Block1.", 600)
        block2 = _block("Block2.", 600)
        trace = [
            {"type": "text", "text": block1},
            {"type": "tool", "text": "Read"},
            {"type": "text", "text": block2},
        ]
        # With tool as delimiter, regions = [[block1], [block2]]
        # Reverse walk: block2 first → returned.
        assert _last_substantial_region(trace, {"tool"}, 500) == block2

    def test_last_substantial_region_walks_back_past_thin(self):
        block = _block("Block.", 600)
        trace = [
            {"type": "text", "text": block},
            {"type": "tool", "text": "Read"},
            {"type": "text", "text": "thin"},
        ]
        # Last region is "thin" (4 chars), walks back to the substantial one.
        assert _last_substantial_region(trace, {"tool"}, 500) == block

    def test_last_substantial_region_aggregates_within_region(self):
        trace = [
            {"type": "text", "text": "Part one. "},
            {"type": "text", "text": "Part two. "},
            {"type": "text", "text": "Part three. "},
            {"type": "tool", "text": "Read"},
        ]
        # Three text events form one region (no delimiter between them).
        # Joined with \n\n.
        result = _last_substantial_region(trace, {"tool"}, 20)
        assert result == "Part one.\n\nPart two.\n\nPart three."


# =============================================================================
# TestPerUserEmailInPrompt
# =============================================================================


class TestPerUserEmailInPrompt:
    """Verify per-user plus-addressed email appears in prompt header."""

    def _composed(self, email):
        config = Config()
        config.email = email
        task = db.Task(
            id=1, status="running", prompt="hello", user_id="carol",
            source_type="talk", conversation_token="room1",
        )
        return build_prompt(task, [], config)

    def test_per_user_email_shown_when_email_enabled(self):
        composed = self._composed(AppEmailConfig(
            enabled=True,
            imap_host="imap.test", imap_port=993,
            imap_user="u", imap_password="p",
            bot_email="istota@example.com",
        ))
        assert "istota+carol@example.com" in composed.system

    @pytest.mark.parametrize("email", [
        AppEmailConfig(enabled=False),
        AppEmailConfig(
            enabled=True,
            imap_host="imap.test", imap_port=993,
            imap_user="u", imap_password="p",
            bot_email="",
        ),
    ], ids=["email_disabled", "no_bot_email"])
    def test_per_user_email_not_shown(self, email):
        # Both halves: a plus-address appearing anywhere in the prompt is the
        # thing being ruled out, whichever section it came from.
        composed = self._composed(email)
        assert "+carol@" not in composed.system + composed.user


# =============================================================================
# TestSmtpFromPlusAddress
# =============================================================================


class TestSmtpFrom:
    """Verify SMTP_FROM uses plain bot email (not plus-addressed)."""

    @patch("istota.executor.subprocess.run")
    def test_smtp_from_uses_plain_bot_email(self, mock_run, tmp_path):
        """SMTP_FROM should be the plain bot email; plus-addressing is for inbound only."""
        config = _skills_config(
            tmp_path, files_skill=False,
            # Real bundled skills dir so the email manifest is loaded.
            bundled_skills_dir=None,
            email=AppEmailConfig(
                enabled=True,
                imap_host="imap.test", imap_port=993,
                imap_user="u", imap_password="p",
                smtp_host="smtp.test", smtp_port=587,
                bot_email="istota@example.com",
            ),
            security=SecurityConfig(skill_proxy_enabled=False),
        )
        _execute(config, mock_run, user_id="carol")
        assert _env(mock_run)["SMTP_FROM"] == "istota@example.com"


class TestWorkspaceDirBwrap:
    """build_bwrap_cmd workspace_dir: RW bind + --chdir + blocklist validation."""

    def _cfg(self, tmp_path):
        # The DB lives in its own subdirectory, as it does everywhere real
        # (`{istota_home}/data/istota.db`). Putting it at tmp_path root would
        # make every sibling fixture dir a child of the now-protected DB
        # directory, which is a property of the fixture, not of the blocklist.
        db_path = tmp_path / "data" / "test.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _db.init_db(db_path)
        return Config(
            db_path=db_path,
            temp_dir=tmp_path / "temp",
            security=SecurityConfig(),
        )

    def _bwrap(self, tmp_path, monkeypatch, workspace_dir):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        cfg = self._cfg(tmp_path)
        with _db.get_db(cfg.db_path) as conn:
            tid = _db.create_task(conn, prompt="x", user_id="alice", source_type="repl")
            task = _db.get_task(conn, tid)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)
        return executor.build_bwrap_cmd(
            ["claude"], cfg, task, True, [], user_temp, workspace_dir=workspace_dir,
            profile=executor.SandboxProfile.CLAUDE,
        )

    def test_workspace_bind_and_chdir(self, tmp_path, monkeypatch):
        ws = tmp_path / "project"
        ws.mkdir()
        cmd = self._bwrap(tmp_path, monkeypatch, ws)
        # chdir targets the workspace, and the workspace is bound RW.
        assert "--chdir" in cmd
        assert cmd[cmd.index("--chdir") + 1] == str(ws.resolve())
        assert str(ws.resolve()) in " ".join(cmd)

    def test_workspace_blocklist_rejects_home_ssh(self, tmp_path, monkeypatch):
        with pytest.raises(ValueError):
            self._bwrap(tmp_path, monkeypatch, Path.home() / ".ssh")

    def test_validate_workspace_rejects_source_tree(self, tmp_path):
        cfg = self._cfg(tmp_path)
        # The istota package dir is inside the source tree → rejected.
        src_dir = Path(executor.__file__).resolve().parent
        with pytest.raises(ValueError):
            executor._validate_workspace_dir(cfg, src_dir)

    def test_validate_workspace_allows_arbitrary_dir(self, tmp_path):
        cfg = self._cfg(tmp_path)
        ok = tmp_path / "safe"
        ok.mkdir()
        assert executor._validate_workspace_dir(cfg, ok) == ok.resolve()


class TestReplInteractiveGate:
    def test_interactive_source_types(self):
        """ISSUE-500 for the push surfaces. Membership rather than behaviour:
        what a text message loses by being absent from this tuple is asserted
        through the assembled prompt in `tests/test_prompt_golden.py`.
        """
        from istota.executor import _INTERACTIVE_SOURCE_TYPES
        for source_type in ("repl", "talk", "email", "sms", "whatsapp"):
            assert source_type in _INTERACTIVE_SOURCE_TYPES, source_type


class TestTheInteractiveSourceTypeSets:
    """The two hand-typed sets that have to move together, and the one that
    does not.

    ISSUE-500 and ISSUE-499 are the same class twice: `sms` and `whatsapp`
    were added as surfaces and three separate spellings of "which source types
    are interactive" were left as they were. A membership test catches a
    spelling no writer produces; it cannot catch a missing one, because the
    test's list and the product's list get written by the same person on the
    same day. These are the subset relations instead, which a missing member
    does break.
    """

    def test_routing_s_interactive_set_is_covered_by_the_executor_s(self):
        """A surface whose reply must never be silently dropped is by
        definition a live conversation, so `routing`'s membership forces this
        one. Subset rather than equality: `executor` could legitimately widen
        (a `cli` turn carrying a conversation token) without `routing`
        following, and that would be a decision rather than drift.
        """
        from istota.executor import _INTERACTIVE_SOURCE_TYPES
        from istota.transport.routing import (
            _INTERACTIVE_SOURCE_TYPES as _ROUTING_INTERACTIVE,
        )

        missing = set(_ROUTING_INTERACTIVE) - set(_INTERACTIVE_SOURCE_TYPES)
        assert not missing, (
            f"{sorted(missing)} can be routed a reply but loads no conversation "
            "context, sticky skills or changelog (ISSUE-500)"
        )

    def test_the_changelog_set_is_a_subset_of_the_interactive_one(self):
        """The changelog is one consumer of the interactive tuple, not a wider
        gate: a source type that shows it must also be one that loads context.
        The other direction is open on purpose — `sms` and `whatsapp` are
        interactive and deliberately do not show it.
        """
        from istota.executor import (
            _INTERACTIVE_SOURCE_TYPES,
            _SKILLS_CHANGELOG_SOURCE_TYPES,
        )

        assert set(_SKILLS_CHANGELOG_SOURCE_TYPES) <= set(_INTERACTIVE_SOURCE_TYPES)
        assert set(_SKILLS_CHANGELOG_SOURCE_TYPES) != set(_INTERACTIVE_SOURCE_TYPES), (
            "the two tuples have converged; if that is deliberate the changelog "
            "no longer needs its own, and if it is not, a push surface has "
            "started spending the changelog on a turn that cannot show it"
        )

    def test_showing_the_changelog_and_spending_it_read_one_predicate(self):
        """`execute_task` injects the changelog and, after a successful run,
        writes the user's skills fingerprint. The two gates must be the same
        predicate: show-without-spend repeats the changelog on every turn for
        ever, and spend-without-show burns it invisibly, which is ISSUE-500's
        own defect with the surfaces swapped. One local, read twice, so there
        is nothing to drift — asserted here because the second read sits
        roughly eight hundred lines below the first.
        """
        from tests.support.drift import source_of
        from istota.executor import execute_task

        src = source_of(execute_task)
        assert "_shows_skills_changelog = task.source_type in " in src
        # Both sites, not just the write: re-gating the injection on a fresh
        # inline `task.source_type in _SKILLS_CHANGELOG_SOURCE_TYPES` would
        # keep every other assertion here green while restoring exactly the
        # two-reads-can-drift shape this test exists to refuse.
        assert src.count("if _shows_skills_changelog:") == 1
        assert src.count("if success and _shows_skills_changelog:") == 1
        assert "set_user_skills_fingerprint" in src
        assert "_is_interactive" not in src, (
            "the changelog gate is not the interactive gate any more; a local "
            "called `_is_interactive` beside it will be read as one"
        )


class TestWorkspacePlaceholderDoesNotClobberSandboxBind:
    """Regression: the {workspace} display string must not clobber the
    execute_task `workspace_dir` parameter (the REPL --workspace bind path,
    blocklist-validated by build_bwrap_cmd).

    Commit f3ab4b6 ("Storage-agnostic prompt/skill vocabulary") reassigned the
    `workspace_dir` parameter to the user's on-mount workspace root just to fill
    the {workspace} placeholder. That value flowed into build_bwrap_cmd, where
    _validate_workspace_dir rejects anything under the workspace root — so
    every sandboxed LLM task on the server failed with
    "workspace ... overlaps a protected path". The fix uses a separate local for
    the display string; the parameter stays None for a normal task.
    """

    @patch("istota.executor.build_bwrap_cmd")
    @patch("istota.executor.subprocess.run")
    def test_normal_task_passes_none_workspace_dir_to_sandbox(
        self, mock_run, mock_bwrap, tmp_path
    ):
        config = _skills_config(
            tmp_path, mount=True,
            nextcloud=NextcloudConfig(url="https://cloud.example.com"),
            security=SecurityConfig(sandbox_enabled=True, skill_proxy_enabled=False),
        )
        (config.workspace_path / "Users" / "alice").mkdir(parents=True)
        # An eager skill whose body references the {workspace} placeholder, so
        # the substitution block that clobbered the variable actually runs.
        (config.skills_dir / "files.md").write_text("Your files live in {workspace}.")
        # build_bwrap_cmd is a no-op wrapper here; we only inspect its kwargs.
        mock_bwrap.side_effect = lambda raw_cmd, *a, **k: raw_cmd

        _execute(config, mock_run, prompt="hi")

        # The sandbox wrapper must have been invoked, and with workspace_dir=None
        # (the mount-subdir value belongs in the {workspace} display string, not
        # the REPL bind path).
        assert mock_bwrap.called
        assert mock_bwrap.call_args.kwargs["workspace_dir"] is None
        # And the {workspace} placeholder still resolved in the prompt. The
        # skill body it lives in is a standing instruction, so it is in the
        # system half — which reaches the CLI as a file rather than on stdin.
        composed = (
            tmp_path / "temp" / ".control" / "alice" / "task_1"
            / "system_prompt.txt"
        ).read_text(encoding="utf-8")
        prompt_text = mock_run.call_args.kwargs["input"]
        assert "{workspace}" not in composed
        assert "{workspace}" not in prompt_text
        assert str((config.workspace_path / "Users" / "alice")) in composed


class TestImagePreparationWritesIntoTheControlDirectory:
    """The destination `execute_task` hands `prepare_image_attachments`.

    The prepared renditions used to land in `{temp_dir}/{user_id}/attachments/
    task_<id>/` — inside the sandbox's own working directory, where the model
    could rewrite the picture it was about to be asked about, and where the
    previous task's renditions were still readable. The function no longer
    derives that layout at all: it takes the directory to write into, and the
    caller is what names it.

    Asserted through the argument rather than through a written file, so the
    wiring is pinned on a deployment with no Pillow and on every case where an
    attachment is screened out before anything is written.
    `tests/test_executor_images.py` is where the real renditions are followed
    to disk.
    """

    def test_the_out_dir_is_inside_the_task_control_directory(self, tmp_path):
        from istota.executor import execute_task, get_task_control_dir, get_user_temp_dir
        from istota.image_attachments import ImagePreparation

        config = _bare_config(tmp_path)
        img = tmp_path / "inbox" / "shot.png"
        img.parent.mkdir(parents=True)
        img.write_bytes(b"not really a png")

        with patch("istota.executor.prepare_image_attachments") as prep, \
                patch("istota.executor.subprocess.run") as mock_run:
            prep.return_value = ImagePreparation([str(img)], [], [])
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            with db.get_db(config.db_path) as conn:
                task_id = db.create_task(
                    conn, prompt="what is this?", user_id="alice",
                    source_type="talk", attachments=[str(img)],
                )
                task = db.get_task(conn, task_id)
                execute_task(task, config, [], conn=conn, use_context=False)

        assert prep.called, "the image pass never ran"
        out_dir = prep.call_args.args[1]
        control = get_task_control_dir(config, "alice", task.id)
        assert out_dir == control / "attachments"
        # And not in the directory the sandbox binds read-write, which is the
        # whole point of the move.
        assert not out_dir.is_relative_to(
            get_user_temp_dir(config, "alice").resolve()
        )


class TestAnUnusableControlDirectoryFailsTheTask:
    """Fail-closed, and *how* it fails closed.

    A task whose control directory cannot be created has nowhere to put its
    standing instructions, so it must not run — but raising out of
    `execute_task` is not the way to say so. `process_one_task` has no handler
    of its own, so the exception reaches the worker's catch-all, which logs and
    moves on with the row still `running`: the task is then recovered only by
    the stuck-worker sweep, minutes later, with the reason nowhere but the
    daemon log. Returning the failure keeps the ordinary accounting and puts
    the path in front of whoever asked.
    """

    def test_a_control_root_that_is_a_file_fails_the_task_by_return(self, tmp_path):
        from istota.executor import CONTROL_DIR_NAME, execute_task

        config = _bare_config(tmp_path)
        config.temp_dir.mkdir(parents=True, exist_ok=True)
        # A real corrupt-state case rather than a patched one: `O_DIRECTORY`
        # is what refuses it, several layers below the assertion.
        (config.temp_dir / CONTROL_DIR_NAME).write_text("not a directory\n")

        with patch("istota.executor.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            with db.get_db(config.db_path) as conn:
                task_id = db.create_task(
                    conn, prompt="hi", user_id="alice", source_type="talk",
                )
                task = db.get_task(conn, task_id)
                success, result, actions, trace = execute_task(
                    task, config, [], conn=conn, use_context=False,
                )

        assert success is False
        assert (actions, trace) == (None, None)
        assert CONTROL_DIR_NAME in result, result
        # And the model was never reached: a task that cannot hold its own
        # standing instructions must not run without them.
        assert not mock_run.called


class TestTheControlDirectoryIsGuardedOnEveryShape:
    """`execute_task`'s two guard entries, read from the request it built.

    They are enforced under different conditions and neither subsumes the
    other, which is the one thing about this pair that is easy to get wrong:

    - `fs_read_roots` is `None` when confinement is off, and `None` means
      *unconfined* in `ToolEnv` — both root lists are then inert. A `read_only`
      entry alone protects nothing on the standalone install or the shipped
      Docker stack.
    - `fs_write_denied_roots` is checked ahead of that unconfined return, so it
      holds on every shape. That is why `execute_task` seeds it outside the
      `native_fs_confinement_active` branch.
    - Under confinement the control directory is inside no write root, so the
      `read_only` entry is what makes it readable while leaving it unwritable.

    `tests/test_sandbox.py::TestNativeFsRootsTaskControlDirectory` is the unit
    half; this is the wiring.
    """

    def _run(self, tmp_path, confined):
        from istota.executor import execute_task

        config = _bare_config(tmp_path)
        captured = {}

        class _Brain:
            model_namespace = "anthropic"
            supports_steering = False
            kind = "claude_code"

            def execute(self, req):
                captured["req"] = req
                return BrainResult(
                    success=True, result_text="answer", stop_reason="completed",
                )

            def resolve_model_name(self, name):
                return (name or "").strip()

            def resolve_alias(self, alias):
                return None

        with patch("istota.executor.make_brain", return_value=_Brain()), \
                patch(
                    "istota.executor.native_fs_confinement_active",
                    return_value=confined,
                ):
            with db.get_db(config.db_path) as conn:
                task_id = db.create_task(
                    conn, prompt="hi", user_id="alice", source_type="talk",
                )
                task = db.get_task(conn, task_id)
                execute_task(task, config, [], conn=conn, use_context=False)

        assert "req" in captured, "the brain was never called"
        return config, task, captured["req"]

    def test_the_unconditional_seed_is_the_control_directory(self, tmp_path):
        """The shapes with nothing else behind it. `build_bwrap_cmd` hands the
        command back unwrapped on macOS, on the standalone install and on the
        shipped Docker stack, and `native_fs_roots` is not called there at all
        — so this seed is the only guard the control directory has."""
        from istota.executor import get_task_control_dir

        config, task, req = self._run(tmp_path, confined=False)

        control = get_task_control_dir(config, task.user_id, task.id)
        assert req.fs_read_roots is None, (
            "the fixture is not actually unconfined, so this asserts nothing "
            "about the shape it is named for"
        )
        assert req.fs_write_denied_roots == [control], req.fs_write_denied_roots

    def test_the_confined_shape_gets_both_entries_once(self, tmp_path):
        from istota.executor import get_task_control_dir

        config, task, req = self._run(tmp_path, confined=True)

        control = get_task_control_dir(config, task.user_id, task.id)
        assert req.fs_write_denied_roots.count(control) == 1, (
            f"fs_write_denied_roots was {req.fs_write_denied_roots!r}; two "
            "producers seeding the same root is the shape of a drift"
        )
        assert control in (req.fs_read_roots or []), req.fs_read_roots
        assert not any(
            control == r or control.is_relative_to(r)
            for r in (req.fs_write_roots or [])
        ), req.fs_write_roots

    def test_the_guard_covers_the_framework_files_and_not_the_model_s(
        self, tmp_path,
    ):
        """The point of a per-directory guard, asserted in both directions.

        Read off the paths `execute_task` put on the *request* rather than off
        a `rglob` of the control directory: enumerating the directory and then
        asserting its contents are under the deny root that is the directory
        is true by construction, and would stay green on exactly the failure
        worth catching — a framework file written somewhere else.

        The result file is the discriminating half. It is written by the model
        from inside the sandbox and read back by the daemon, so it lives in the
        model's own working directory by definition; a guard that covered it
        would break every task, and a test that only checked the deny side
        would pass equally against a task whose whole temp tree was refused.
        """
        from istota.executor import get_task_control_dir

        config, task, req = self._run(tmp_path, confined=True)

        control = get_task_control_dir(config, task.user_id, task.id)
        denied = req.fs_write_denied_roots

        composed = req.composed_system_prompt_path
        assert composed is not None, "nothing named the composed system prompt"
        assert any(composed.is_relative_to(root) for root in denied), (
            f"{composed} is under no deny root: {denied}"
        )
        # The user half, named by no request field, so read from the directory
        # the request's own path points into.
        assert (control / "prompt.txt").exists()

        assert req.result_file is not None
        assert not any(
            Path(req.result_file).is_relative_to(root) for root in denied
        ), f"the result file {req.result_file} was denied: {denied}"


class TestAnOptionalReadNeverCreatesTheDatabase:
    """ISSUE-570: prompt assembly with no database must not make one.

    `Config.db_path` defaults to the relative `data/istota.db`, and each
    optional reader that opened its own connection reached `sqlite3.connect`,
    which creates the file whenever the directory exists. The reader then found
    no table and returned nothing, so the run that made the file passed, and
    every later test in that working directory read a different prompt.
    """

    @pytest.mark.parametrize("source_type", ["talk", "web", "email"])
    def test_a_dry_run_from_a_bare_config_leaves_no_database(
        self, tmp_path, monkeypatch, source_type,
    ):
        monkeypatch.chdir(tmp_path)
        (tmp_path / "data").mkdir()
        config = Config()
        config.temp_dir = tmp_path / "tmp"
        assert not config.db_path.is_absolute()
        task = db.Task(
            id=7, status="running", source_type=source_type, user_id="alice",
            prompt="hello there", conversation_token="room1",
        )

        success, result, _a, _t = executor.execute_task(
            task, config, [], dry_run=True,
        )

        assert success, result
        assert not (tmp_path / "data" / "istota.db").exists()
        assert list((tmp_path / "data").iterdir()) == []
