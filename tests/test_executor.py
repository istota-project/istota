"""Configuration loading for istota.executor module."""

import json
from unittest.mock import patch, MagicMock

import pytest

from istota.executor import (
    _compose_full_result,
    _resolve_user_tz,
    _is_automated_task,
    _is_terse,
    _last_substantial_region,
    _text_similarity,
    _AUTOMATED_SOURCE_TYPES,
    _CM_SEGMENT_MIN_CHARS,
    _TERSE_RESULT_MAX_CHARS,
    _TRAILING_REGION_MIN_CHARS,
    detect_malformed_result,
    parse_api_error,
    is_transient_api_error,
    build_prompt,
    load_persona,
    load_emissaries,
    _pre_transcribe_attachments,
    _preshrink_image_attachments,
    _IMAGE_MAX_EDGE,
    _detect_notification_reply,
    _apply_recency_window_talk,
    _apply_recency_window_db,
    _AUDIO_EXTENSIONS,
    API_RETRY_MAX_ATTEMPTS,
    API_RETRY_DELAY_SECONDS,
    TRANSIENT_STATUS_CODES,
)
from istota import db as _db
from istota.brain import BrainRequest, ClaudeCodeBrain
from istota.brain._types import BrainResult
from pathlib import Path

from istota.config import Config, DeveloperConfig, EmailConfig as AppEmailConfig, NextcloudConfig, ResourceConfig, SchedulerConfig, SecurityConfig, SiteConfig, UserConfig
from istota import db


# ---------------------------------------------------------------------------
# TestParseApiError
# ---------------------------------------------------------------------------


class TestParseApiError:
    def test_parses_500_error(self):
        error_text = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"},"request_id":"req_abc123"}'
        result = parse_api_error(error_text)
        assert result is not None
        assert result["status_code"] == 500
        assert result["message"] == "Internal server error"
        assert result["request_id"] == "req_abc123"

    def test_parses_429_error(self):
        error_text = 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limit exceeded"},"request_id":"req_xyz"}'
        result = parse_api_error(error_text)
        assert result is not None
        assert result["status_code"] == 429
        assert result["message"] == "Rate limit exceeded"
        assert result["request_id"] == "req_xyz"

    def test_parses_401_error(self):
        error_text = 'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"Invalid API key"},"request_id":"req_auth"}'
        result = parse_api_error(error_text)
        assert result is not None
        assert result["status_code"] == 401
        assert result["message"] == "Invalid API key"

    def test_parses_error_with_prefix_text(self):
        error_text = 'Some prefix text before API Error: 503 {"type":"error","error":{"type":"overloaded_error","message":"Service overloaded"}}'
        result = parse_api_error(error_text)
        assert result is not None
        assert result["status_code"] == 503
        assert result["message"] == "Service overloaded"

    def test_returns_none_for_non_api_error(self):
        error_text = "Claude Code was killed (likely out of memory)"
        result = parse_api_error(error_text)
        assert result is None

    def test_returns_none_for_regular_text(self):
        result = parse_api_error("Task completed successfully")
        assert result is None

    def test_handles_malformed_json(self):
        # Malformed JSON with closing brace but invalid content
        error_text = 'API Error: 500 {broken json}'
        result = parse_api_error(error_text)
        assert result is not None
        assert result["status_code"] == 500
        assert result["message"] == "Unknown error"
        assert result["request_id"] is None

    def test_returns_none_for_unclosed_json(self):
        # JSON without closing brace cannot be matched
        error_text = 'API Error: 500 {broken json'
        result = parse_api_error(error_text)
        assert result is None

    def test_handles_missing_error_field(self):
        error_text = 'API Error: 500 {"type":"error","request_id":"req_123"}'
        result = parse_api_error(error_text)
        assert result is not None
        assert result["status_code"] == 500
        assert result["message"] == "Unknown error"
        assert result["request_id"] == "req_123"


# ---------------------------------------------------------------------------
# TestIsTransientApiError
# ---------------------------------------------------------------------------


class TestIsTransientApiError:
    def test_500_is_transient(self):
        error_text = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"}}'
        assert is_transient_api_error(error_text) is True

    def test_502_is_transient(self):
        error_text = 'API Error: 502 {"type":"error","error":{"type":"api_error","message":"Bad gateway"}}'
        assert is_transient_api_error(error_text) is True

    def test_503_is_transient(self):
        error_text = 'API Error: 503 {"type":"error","error":{"type":"api_error","message":"Service unavailable"}}'
        assert is_transient_api_error(error_text) is True

    def test_504_is_transient(self):
        error_text = 'API Error: 504 {"type":"error","error":{"type":"api_error","message":"Gateway timeout"}}'
        assert is_transient_api_error(error_text) is True

    def test_529_is_transient(self):
        error_text = 'API Error: 529 {"type":"error","error":{"type":"overloaded_error","message":"Overloaded"}}'
        assert is_transient_api_error(error_text) is True

    def test_429_is_transient(self):
        error_text = 'API Error: 429 {"type":"error","error":{"type":"rate_limit_error","message":"Rate limited"}}'
        assert is_transient_api_error(error_text) is True

    def test_401_is_not_transient(self):
        error_text = 'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"Unauthorized"}}'
        assert is_transient_api_error(error_text) is False

    def test_403_is_not_transient(self):
        error_text = 'API Error: 403 {"type":"error","error":{"type":"permission_error","message":"Forbidden"}}'
        assert is_transient_api_error(error_text) is False

    def test_400_is_not_transient(self):
        error_text = 'API Error: 400 {"type":"error","error":{"type":"invalid_request_error","message":"Bad request"}}'
        assert is_transient_api_error(error_text) is False

    def test_non_api_error_is_not_transient(self):
        assert is_transient_api_error("Claude Code was killed (likely out of memory)") is False
        assert is_transient_api_error("Task execution timed out") is False
        assert is_transient_api_error("Cancelled by user") is False


# ---------------------------------------------------------------------------
# TestTransientStatusCodes
# ---------------------------------------------------------------------------


class TestTransientStatusCodes:
    def test_includes_common_server_errors(self):
        assert 500 in TRANSIENT_STATUS_CODES
        assert 502 in TRANSIENT_STATUS_CODES
        assert 503 in TRANSIENT_STATUS_CODES
        assert 504 in TRANSIENT_STATUS_CODES

    def test_includes_anthropic_overloaded(self):
        assert 529 in TRANSIENT_STATUS_CODES

    def test_excludes_client_errors(self):
        assert 400 not in TRANSIENT_STATUS_CODES
        assert 401 not in TRANSIENT_STATUS_CODES
        assert 403 not in TRANSIENT_STATUS_CODES
        assert 404 not in TRANSIENT_STATUS_CODES


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


class TestExecuteStreamingRetry:
    """Retry logic for transient API errors lives in ClaudeCodeBrain.

    Tests use the static _execute_streaming_once method as the mock target
    and drive the public _execute_streaming wrapper, which is the same
    layering the executor used to have.
    """

    def _make_request(self, tmp_path: Path) -> BrainRequest:
        return BrainRequest(
            prompt="test",
            allowed_tools=["Bash"],
            cwd=tmp_path,
            env={},
            timeout_seconds=60,
            streaming=True,
            result_file=tmp_path / "result.txt",
        )

    @patch("istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once")
    @patch("istota.brain.claude_code.time.sleep")
    def test_retries_on_transient_error(self, mock_sleep, mock_exec_once, tmp_path):
        """Should retry on transient 500 errors before giving up."""
        error_500 = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"},"request_id":"req_123"}'
        mock_exec_once.side_effect = [
            BrainResult(False, error_500, stop_reason="error"),
            BrainResult(True, "Success after retry"),
        ]

        brain = ClaudeCodeBrain()
        result = brain._execute_streaming([], self._make_request(tmp_path))

        assert result.success is True
        assert result.result_text == "Success after retry"
        assert mock_exec_once.call_count == 2
        mock_sleep.assert_called_once_with(API_RETRY_DELAY_SECONDS)

    @patch("istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once")
    @patch("istota.brain.claude_code.time.sleep")
    def test_no_retry_on_permanent_error(self, mock_sleep, mock_exec_once, tmp_path):
        """Should not retry on permanent 401 errors."""
        error_401 = 'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"Invalid API key"}}'
        mock_exec_once.return_value = BrainResult(False, error_401, stop_reason="error")

        brain = ClaudeCodeBrain()
        result = brain._execute_streaming([], self._make_request(tmp_path))

        assert result.success is False
        assert "401" in result.result_text
        assert mock_exec_once.call_count == 1
        mock_sleep.assert_not_called()

    @patch("istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once")
    @patch("istota.brain.claude_code.time.sleep")
    def test_no_retry_on_non_api_error(self, mock_sleep, mock_exec_once, tmp_path):
        """Should not retry on non-API errors like OOM."""
        mock_exec_once.return_value = BrainResult(
            False, "Claude Code was killed (likely out of memory)", stop_reason="oom",
        )

        brain = ClaudeCodeBrain()
        result = brain._execute_streaming([], self._make_request(tmp_path))

        assert result.success is False
        assert "out of memory" in result.result_text
        assert mock_exec_once.call_count == 1
        mock_sleep.assert_not_called()

    @patch("istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once")
    @patch("istota.brain.claude_code.time.sleep")
    def test_gives_up_after_max_retries(self, mock_sleep, mock_exec_once, tmp_path):
        """Should give up after max retry attempts."""
        error_500 = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"}}'
        mock_exec_once.return_value = BrainResult(False, error_500, stop_reason="error")

        brain = ClaudeCodeBrain()
        result = brain._execute_streaming([], self._make_request(tmp_path))

        assert result.success is False
        assert "500" in result.result_text
        assert mock_exec_once.call_count == API_RETRY_MAX_ATTEMPTS
        assert mock_sleep.call_count == API_RETRY_MAX_ATTEMPTS - 1

    @patch("istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once")
    def test_success_on_first_try_no_retry(self, mock_exec_once, tmp_path):
        """Should not retry if first attempt succeeds."""
        mock_exec_once.return_value = BrainResult(True, "Immediate success")

        brain = ClaudeCodeBrain()
        result = brain._execute_streaming([], self._make_request(tmp_path))

        assert result.success is True
        assert result.result_text == "Immediate success"
        assert mock_exec_once.call_count == 1

    @patch("istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once")
    def test_actions_taken_passed_through(self, mock_exec_once, tmp_path):
        """Should pass through actions_taken from _execute_streaming_once."""
        actions = '["📄 Reading file.py", "✏️ Editing file.py"]'
        mock_exec_once.return_value = BrainResult(
            True, "Done", actions_taken=actions, execution_trace='[]',
        )

        brain = ClaudeCodeBrain()
        result = brain._execute_streaming([], self._make_request(tmp_path))

        assert result.success is True
        assert result.result_text == "Done"
        assert result.actions_taken == actions

    @patch("istota.brain.claude_code.ClaudeCodeBrain._execute_streaming_once")
    @patch("istota.brain.claude_code.time.sleep")
    def test_actions_taken_from_successful_retry(self, mock_sleep, mock_exec_once, tmp_path):
        """On retry, should use actions_taken from the successful attempt."""
        error_500 = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"err"},"request_id":"req_1"}'
        actions = '["📄 Reading config"]'
        mock_exec_once.side_effect = [
            BrainResult(False, error_500, stop_reason="error"),
            BrainResult(True, "ok", actions_taken=actions),
        ]

        brain = ClaudeCodeBrain()
        result = brain._execute_streaming([], self._make_request(tmp_path))

        assert result.success is True
        assert result.actions_taken == actions


# ---------------------------------------------------------------------------
# TestBuildPromptSkillsChangelog
# ---------------------------------------------------------------------------


class TestBuildPromptSkillsChangelog:
    def _make_task(self, source_type="talk"):
        return db.Task(
            id=1,
            status="running",
            source_type=source_type,
            user_id="alice",
            prompt="hello",
            conversation_token="room1",
        )

    def _make_config(self, tmp_path):
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        return Config(
            db_path=tmp_path / "test.db",
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
        )

    def test_changelog_included_when_provided(self, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()
        prompt = build_prompt(
            task, [], config,
            skills_changelog="## 2026-02-08\n- New feature added",
        )
        assert "## What's New in Skills" in prompt
        assert "New feature added" in prompt

    def test_changelog_not_included_when_none(self, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()
        prompt = build_prompt(task, [], config, skills_changelog=None)
        assert "What's New in Skills" not in prompt

    def test_changelog_appears_before_skills_doc(self, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()
        prompt = build_prompt(
            task, [], config,
            skills_doc="## Skills Reference (v: abc123)\n\n### Files\n\nFile ops.",
            skills_changelog="## 2026-02-08\n- Updated files skill",
        )
        changelog_pos = prompt.index("What's New in Skills")
        skills_pos = prompt.index("Skills Reference")
        assert changelog_pos < skills_pos


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
        user_profiles.ensure_profile(config.db_path, "alice", timezone="Europe/Warsaw")

        tz, tz_str = _resolve_user_tz(config, "alice")
        assert tz_str == "Europe/Warsaw"
        assert tz.key == "Europe/Warsaw"

    def test_falls_back_to_in_memory_config_when_no_db_row(self, tmp_path):
        config = self._make_config(tmp_path, user_tz="America/New_York")
        # No user_profiles row written.
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


class TestSkillsFingerprintIntegration:
    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text('[files]\ndescription = "File ops"\nalways_include = true\n')
        (skills_dir / "files.md").write_text("File operations guide.")
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
        )

    def _make_task(self, conn, source_type="talk"):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type=source_type)
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_changelog_included_when_fingerprint_changed(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        (config.skills_dir / "CHANGELOG.md").write_text("## v1\n- New feature")
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(task, config, [], conn=conn)

        # Verify changelog was in the prompt
        call_args = mock_run.call_args
        prompt_text = call_args.kwargs["input"]  # prompt passed via stdin
        assert "What's New in Skills" in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_changelog_not_included_when_fingerprint_matches(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        (config.skills_dir / "CHANGELOG.md").write_text("## v1\n- New feature")
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        # Pre-store the current fingerprint
        from istota.skills._loader import compute_skills_fingerprint
        fp = compute_skills_fingerprint(config.skills_dir, bundled_dir=config.bundled_skills_dir)

        with db.get_db(config.db_path) as conn:
            db.set_user_skills_fingerprint(conn, "alice", fp)
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(task, config, [], conn=conn)

        call_args = mock_run.call_args
        prompt_text = call_args.kwargs["input"]
        assert "What's New in Skills" not in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_changelog_not_included_for_briefing(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        (config.skills_dir / "CHANGELOG.md").write_text("## v1\n- New feature")
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="briefing")
            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(task, config, [], conn=conn)

        call_args = mock_run.call_args
        prompt_text = call_args.kwargs["input"]
        assert "What's New in Skills" not in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_changelog_not_included_for_scheduled(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        (config.skills_dir / "CHANGELOG.md").write_text("## v1\n- New feature")
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="scheduled")
            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(task, config, [], conn=conn)

        call_args = mock_run.call_args
        prompt_text = call_args.kwargs["input"]
        assert "What's New in Skills" not in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_fingerprint_updated_after_success(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        from istota.skills._loader import compute_skills_fingerprint
        expected_fp = compute_skills_fingerprint(config.skills_dir, bundled_dir=config.bundled_skills_dir)

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(task, config, [], conn=conn)
            assert success is True
            stored_fp = db.get_user_skills_fingerprint(conn, "alice")
            assert stored_fp == expected_fp

    @patch("istota.executor.subprocess.run")
    def test_fingerprint_not_updated_for_non_interactive(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="scheduled")
            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(task, config, [], conn=conn)
            assert success is True
            stored_fp = db.get_user_skills_fingerprint(conn, "alice")
            assert stored_fp is None

    @patch("istota.executor.subprocess.run")
    def test_fingerprint_not_updated_on_failure(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="error")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(task, config, [], conn=conn)
            assert success is False
            stored_fp = db.get_user_skills_fingerprint(conn, "alice")
            assert stored_fp is None


# ---------------------------------------------------------------------------
# TestDeveloperEnvVars
# ---------------------------------------------------------------------------


class TestDeveloperEnvVars:
    def _make_config(self, tmp_path, developer_enabled=True):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text('[files]\ndescription = "File ops"\nalways_include = true\n')
        (skills_dir / "files.md").write_text("File operations guide.")
        dev = DeveloperConfig(
            enabled=developer_enabled,
            repos_dir="/srv/repos",
            gitlab_url="https://gitlab.example.com",
            gitlab_token="glpat-test",
            gitlab_username="istotabot",
            gitlab_default_namespace="example",
        )
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            # Use the real bundled skills dir so the developer manifest +
            # setup_env hook are loaded. Phase 2 moved env injection out of
            # the executor and into manifests / hooks.
            bundled_skills_dir=None,
            temp_dir=tmp_path / "temp",
            developer=dev,
            security=SecurityConfig(skill_proxy_enabled=False),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_developer_env_vars_set_when_enabled(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, developer_enabled=True)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        call_args = mock_run.call_args
        env = call_args[1]["env"]
        assert env["DEVELOPER_REPOS_DIR"] == "/srv/repos"
        assert env["GITLAB_URL"] == "https://gitlab.example.com"
        assert env["GITLAB_DEFAULT_NAMESPACE"] == "example"
        assert "GITLAB_API_CMD" in env
        # Token passed via env var (scripts read it, no secrets on disk)
        assert env["GITLAB_TOKEN"] == "glpat-test"
        # Git credential helper configured via GIT_CONFIG_ env vars
        assert env["GIT_CONFIG_COUNT"] == "1"
        assert "credential" in env["GIT_CONFIG_KEY_0"]
        assert "gitlab.example.com" in env["GIT_CONFIG_KEY_0"]

    @patch("istota.executor.subprocess.run")
    def test_developer_helper_scripts_created(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, developer_enabled=True)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        call_args = mock_run.call_args
        env = call_args[1]["env"]
        from pathlib import Path
        api_cmd = Path(env["GITLAB_API_CMD"])
        assert api_cmd.exists()
        assert api_cmd.stat().st_mode & 0o700
        # Scripts reference env var, not literal token (no secrets on disk)
        api_content = api_cmd.read_text()
        assert "PRIVATE-TOKEN: $GITLAB_TOKEN" in api_content
        assert "glpat-test" not in api_content

        # Git credential helper reads from env var too
        cred_helper = Path(env["GIT_CONFIG_VALUE_0"])
        assert cred_helper.exists()
        cred_content = cred_helper.read_text()
        assert "$GITLAB_TOKEN" in cred_content
        assert "glpat-test" not in cred_content

    @patch("istota.executor.subprocess.run")
    def test_developer_api_wrapper_has_allowlist(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, developer_enabled=True)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        call_args = mock_run.call_args
        env = call_args[1]["env"]
        from pathlib import Path
        api_content = Path(env["GITLAB_API_CMD"]).read_text()
        # Allowlist case statement is present
        assert "case" in api_content
        assert "endpoint not allowed" in api_content
        # Default allowlisted endpoints are present (bare paths — the
        # ``/api/v4`` prefix is baked into the curl target instead, so the
        # host-side wrapper and the devbox proxy share one allowlist).
        assert "GET /projects/" in api_content
        assert "POST /projects/" in api_content
        assert "merge_requests" in api_content
        # The curl target carries /api/v4 — keeps existing GitLab URLs
        # reachable without changing every $GITLAB_API_CMD caller.
        assert "/api/v4$ENDPOINT" in api_content
        # Back-compat: callers that still pass `/api/v4/...` get the
        # prefix stripped before matching.
        assert "/api/v4/*" in api_content
        # No exec — plain curl for reliable piping
        assert "exec curl" not in api_content
        assert "curl -s" in api_content

    @patch("istota.executor.subprocess.run")
    def test_developer_api_wrapper_custom_allowlist(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, developer_enabled=True)
        config.developer.gitlab_api_allowlist = ["GET /api/v4/projects/*"]
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        call_args = mock_run.call_args
        env = call_args[1]["env"]
        from pathlib import Path
        api_content = Path(env["GITLAB_API_CMD"]).read_text()
        assert "GET /api/v4/projects/" in api_content
        # Custom list has only one entry — no merge_requests pattern
        assert "merge_requests" not in api_content

    @patch("istota.executor.subprocess.run")
    def test_developer_env_vars_not_set_when_disabled(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, developer_enabled=False)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        call_args = mock_run.call_args
        env = call_args[1]["env"]
        assert "DEVELOPER_REPOS_DIR" not in env
        assert "GITLAB_API_CMD" not in env
        assert "GITLAB_TOKEN" not in env

    @patch("istota.executor.subprocess.run")
    def test_developer_env_vars_not_set_when_no_repos_dir(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, developer_enabled=True)
        config.developer.repos_dir = ""
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        call_args = mock_run.call_args
        env = call_args[1]["env"]
        assert "DEVELOPER_REPOS_DIR" not in env


# ---------------------------------------------------------------------------
# TestGitHubEnvVars
# ---------------------------------------------------------------------------


class TestGitHubEnvVars:
    def _make_config(self, tmp_path, github_token="ghp_test123", gitlab_token="", developer_enabled=True):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text('[files]\ndescription = "File ops"\nalways_include = true\n')
        (skills_dir / "files.md").write_text("File operations guide.")
        dev = DeveloperConfig(
            enabled=developer_enabled,
            repos_dir="/srv/repos",
            gitlab_url="https://gitlab.example.com",
            gitlab_token=gitlab_token,
            gitlab_username="gitlabbot",
            gitlab_default_namespace="example",
            github_url="https://github.com",
            github_token=github_token,
            github_username="githubbot",
            github_default_owner="myorg",
            github_reviewer="reviewer-user",
        )
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            # Use the real bundled skills dir so the developer manifest +
            # setup_env hook are loaded.
            bundled_skills_dir=None,
            temp_dir=tmp_path / "temp",
            developer=dev,
            security=SecurityConfig(skill_proxy_enabled=False),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_github_env_vars_set_when_configured(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["GITHUB_URL"] == "https://github.com"
        assert env["GITHUB_DEFAULT_OWNER"] == "myorg"
        assert env["GITHUB_REVIEWER"] == "reviewer-user"
        assert env["GITHUB_TOKEN"] == "ghp_test123"
        assert "GITHUB_API_CMD" in env
        # Git credential helper configured
        assert "GIT_CONFIG_COUNT" in env
        assert "github.com" in env["GIT_CONFIG_KEY_0"]

    @patch("istota.executor.subprocess.run")
    def test_github_helper_scripts_created(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        from pathlib import Path
        api_cmd = Path(env["GITHUB_API_CMD"])
        assert api_cmd.exists()
        assert api_cmd.stat().st_mode & 0o700
        api_content = api_cmd.read_text()
        assert "Authorization: Bearer $GITHUB_TOKEN" in api_content
        assert "ghp_test123" not in api_content
        # Uses api.github.com for github.com
        assert "api.github.com" in api_content

        # Git credential helper reads from env var
        cred_helper = Path(env["GIT_CONFIG_VALUE_0"])
        assert cred_helper.exists()
        cred_content = cred_helper.read_text()
        assert "$GITHUB_TOKEN" in cred_content
        assert "ghp_test123" not in cred_content
        # Username set in config
        assert "githubbot" in cred_content

    @patch("istota.executor.subprocess.run")
    def test_github_api_wrapper_has_allowlist(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        from pathlib import Path
        api_content = Path(env["GITHUB_API_CMD"]).read_text()
        assert "case" in api_content
        assert "endpoint not allowed" in api_content
        assert "GET /repos/" in api_content
        assert "POST /repos/" in api_content
        assert "pulls" in api_content

    @patch("istota.executor.subprocess.run")
    def test_github_not_set_when_no_token(self, mock_run, tmp_path):
        # Phase 3: build_skill_env runs over authorized_skills only. With
        # neither GitLab nor GitHub tokens configured and ``developer``
        # not selected, the skill is not authorized — none of its env
        # vars (sensitive or not) flow into the subprocess env.
        config = self._make_config(tmp_path, github_token="")
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "GITHUB_API_CMD" not in env
        assert "GITHUB_TOKEN" not in env
        assert "GITHUB_URL" not in env

    @patch("istota.executor.subprocess.run")
    def test_both_platforms_configured(self, mock_run, tmp_path):
        """When both GitLab and GitHub tokens are set, GIT_CONFIG_COUNT=2."""
        config = self._make_config(tmp_path, github_token="ghp_test123", gitlab_token="glpat-test")
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["GIT_CONFIG_COUNT"] == "2"
        # Both API wrappers exist
        assert "GITLAB_API_CMD" in env
        assert "GITHUB_API_CMD" in env
        # Both credential helpers configured at different indices
        keys = {env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_KEY_1"]}
        assert any("gitlab.example.com" in k for k in keys)
        assert any("github.com" in k for k in keys)

    @patch("istota.executor.subprocess.run")
    def test_github_enterprise_api_url(self, mock_run, tmp_path):
        """GitHub Enterprise uses {url}/api/v3 instead of api.github.com."""
        config = self._make_config(tmp_path)
        config.developer.github_url = "https://github.example.com"
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        from pathlib import Path
        api_content = Path(env["GITHUB_API_CMD"]).read_text()
        assert "github.example.com/api/v3" in api_content
        assert "api.github.com" not in api_content

    @patch("istota.executor.subprocess.run")
    def test_github_default_username_x_access_token(self, mock_run, tmp_path):
        """When github_username is empty, credential helper uses x-access-token."""
        config = self._make_config(tmp_path)
        config.developer.github_username = ""
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        from pathlib import Path
        cred_content = Path(env["GIT_CONFIG_VALUE_0"]).read_text()
        assert "x-access-token" in cred_content


# ---------------------------------------------------------------------------
# TestDeveloperProxyAwareScripts
# ---------------------------------------------------------------------------


class TestDeveloperProxyAwareScripts:
    """When skill_proxy_enabled, developer scripts use credential-fetch instead of env vars."""

    def _make_config(self, tmp_path, proxy_enabled=True):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text('[files]\ndescription = "File ops"\nalways_include = true\n')
        (skills_dir / "files.md").write_text("File operations guide.")
        dev = DeveloperConfig(
            enabled=True,
            repos_dir="/srv/repos",
            gitlab_url="https://gitlab.example.com",
            gitlab_token="glpat-test",
            gitlab_username="istotabot",
            github_url="https://github.com",
            github_token="ghp_test123",
            github_username="githubbot",
        )
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            # Real bundled skills dir so the developer setup_env hook fires.
            bundled_skills_dir=None,
            temp_dir=tmp_path / "temp",
            developer=dev,
            security=SecurityConfig(
                skill_proxy_enabled=proxy_enabled,
                skill_proxy_timeout=30,
            ),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @staticmethod
    def _get_claude_env(mock_run, result=None):
        """Extract env dict from the claude subprocess.run call (the one with env kwarg)."""
        for call in mock_run.call_args_list:
            if "env" in call.kwargs:
                return call.kwargs["env"]
        extra = f", result={result}" if result else ""
        pytest.fail(f"No subprocess.run call with env found (calls={mock_run.call_count}{extra})")

    @patch("istota.executor.subprocess.run")
    def test_credential_fetch_script_created(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, proxy_enabled=True)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        cred_fetch = tmp_path / "temp" / "alice" / ".developer" / "credential-fetch"
        assert cred_fetch.exists()
        assert cred_fetch.stat().st_mode & 0o700
        content = cred_fetch.read_text()
        assert "ISTOTA_SKILL_PROXY_SOCK" in content
        assert "credential" in content

    @patch("istota.executor.subprocess.run")
    def test_gitlab_scripts_use_credential_fetch(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, proxy_enabled=True)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = self._get_claude_env(mock_run)
        # Git credential helper uses credential-fetch
        cred_content = Path(env["GIT_CONFIG_VALUE_0"]).read_text()
        assert "credential-fetch" in cred_content
        assert "GITLAB_TOKEN" in cred_content
        assert "$GITLAB_TOKEN" not in cred_content  # Not direct env var

        # API wrapper uses credential-fetch
        api_content = Path(env["GITLAB_API_CMD"]).read_text()
        assert "credential-fetch" in api_content
        assert "GITLAB_TOKEN" in api_content
        # No literal token in scripts
        assert "glpat-test" not in api_content
        assert "glpat-test" not in cred_content

    @patch("istota.executor.subprocess.run")
    def test_github_scripts_use_credential_fetch(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, proxy_enabled=True)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = self._get_claude_env(mock_run)
        # Find the GitHub credential helper (may be index 0 or 1)
        for i in range(int(env.get("GIT_CONFIG_COUNT", "0"))):
            key = env.get(f"GIT_CONFIG_KEY_{i}", "")
            if "github.com" in key:
                gh_cred_content = Path(env[f"GIT_CONFIG_VALUE_{i}"]).read_text()
                assert "credential-fetch" in gh_cred_content
                assert "GITHUB_TOKEN" in gh_cred_content
                break
        else:
            pytest.fail("No GitHub credential helper found")

        # API wrapper uses credential-fetch
        api_content = Path(env["GITHUB_API_CMD"]).read_text()
        assert "credential-fetch" in api_content
        assert "GITHUB_TOKEN" in api_content
        assert "ghp_test123" not in api_content

    @patch("istota.executor.subprocess.run")
    def test_tokens_stripped_from_claude_env(self, mock_run, tmp_path):
        """With proxy enabled, GITLAB_TOKEN and GITHUB_TOKEN should not be in Claude's env."""
        config = self._make_config(tmp_path, proxy_enabled=True)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            result = execute_task(task, config, [], conn=conn)

        env = self._get_claude_env(mock_run, result=result)
        assert "GITLAB_TOKEN" not in env
        assert "GITHUB_TOKEN" not in env
        # Proxy socket should be set
        assert "ISTOTA_SKILL_PROXY_SOCK" in env

    @patch("istota.executor.subprocess.run")
    def test_proxy_disabled_uses_env_vars(self, mock_run, tmp_path):
        """Without proxy, scripts use $GITLAB_TOKEN and $GITHUB_TOKEN env vars directly."""
        config = self._make_config(tmp_path, proxy_enabled=False)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = self._get_claude_env(mock_run)
        # Tokens present in env
        assert env["GITLAB_TOKEN"] == "glpat-test"
        assert env["GITHUB_TOKEN"] == "ghp_test123"

        # Scripts use $TOKEN_NAME directly, not credential-fetch
        api_content = Path(env["GITLAB_API_CMD"]).read_text()
        assert "$GITLAB_TOKEN" in api_content
        assert "credential-fetch" not in api_content

        cred_content = Path(env["GIT_CONFIG_VALUE_0"]).read_text()
        assert "$GITLAB_TOKEN" in cred_content
        assert "credential-fetch" not in cred_content

        # No credential-fetch script created
        cred_fetch = tmp_path / "temp" / "alice" / ".developer" / "credential-fetch"
        assert not cred_fetch.exists()


class TestAllowlistPatternConversion:
    def test_trailing_wildcard(self):
        from istota.executor import _allowlist_pattern_to_case
        assert _allowlist_pattern_to_case("GET /api/v4/projects/*") == '"GET /api/v4/projects/"*'

    def test_middle_wildcard(self):
        from istota.executor import _allowlist_pattern_to_case
        result = _allowlist_pattern_to_case("POST /api/v4/projects/*/merge_requests")
        assert result == '"POST /api/v4/projects/"*"/merge_requests"'

    def test_multiple_wildcards(self):
        from istota.executor import _allowlist_pattern_to_case
        result = _allowlist_pattern_to_case("POST /api/v4/projects/*/merge_requests/*/notes")
        assert result == '"POST /api/v4/projects/"*"/merge_requests/"*"/notes"'

    def test_no_wildcard(self):
        from istota.executor import _allowlist_pattern_to_case
        result = _allowlist_pattern_to_case("GET /api/v4/version")
        assert result == '"GET /api/v4/version"'

    def test_shell_case_matching(self):
        """Verify generated patterns actually work as shell case globs."""
        import subprocess
        from istota.executor import _allowlist_pattern_to_case

        cases = [
            # (pattern, input, should_match)
            ("GET /api/v4/projects/*", "GET /api/v4/projects/123", True),
            ("GET /api/v4/projects/*", "GET /api/v4/projects/123/merge_requests", True),
            ("GET /api/v4/projects/*", "POST /api/v4/projects/123", False),
            ("POST /api/v4/projects/*/merge_requests", "POST /api/v4/projects/123/merge_requests", True),
            ("POST /api/v4/projects/*/merge_requests", "POST /api/v4/projects/123/merge_requests/456/merge", False),
            ("POST /api/v4/projects/*/merge_requests/*/notes", "POST /api/v4/projects/123/merge_requests/456/notes", True),
            ("POST /api/v4/projects/*/merge_requests/*/notes", "POST /api/v4/projects/123/merge_requests/456/merge", False),
        ]
        for pattern, input_str, should_match in cases:
            case_glob = _allowlist_pattern_to_case(pattern)
            script = f'case "{input_str}" in {case_glob}) echo match ;; *) echo no ;; esac'
            result = subprocess.run(["sh", "-c", script], capture_output=True, text=True)
            matched = result.stdout.strip() == "match"
            assert matched == should_match, (
                f"Pattern {pattern!r} vs {input_str!r}: expected {should_match}, "
                f"case glob: {case_glob}"
            )


# ---------------------------------------------------------------------------
# TestWebsiteEnvVars
# ---------------------------------------------------------------------------


class TestWebsiteEnvVars:
    def _make_config(self, tmp_path, site_enabled=True, user_site_enabled=True):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text('[files]\ndescription = "File ops"\nalways_include = true\n')
        (skills_dir / "files.md").write_text("File operations guide.")
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        site = SiteConfig(
            enabled=site_enabled,
            hostname="istota.example.com",
        )
        users = {}
        if user_site_enabled is not None:
            users["alice"] = UserConfig(site_enabled=user_site_enabled)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount_path,
            site=site,
            users=users,
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_website_env_vars_set_when_enabled(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["WEBSITE_PATH"] == str(tmp_path / "mount" / "Users" / "alice" / "istota" / "html")
        assert env["WEBSITE_URL"] == "https://istota.example.com/~alice"

    @patch("istota.executor.subprocess.run")
    def test_website_env_vars_not_set_when_site_disabled(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, site_enabled=False)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "WEBSITE_PATH" not in env
        assert "WEBSITE_URL" not in env

    @patch("istota.executor.subprocess.run")
    def test_website_env_vars_not_set_when_user_not_enabled(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, user_site_enabled=False)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "WEBSITE_PATH" not in env
        assert "WEBSITE_URL" not in env


class TestKarakeepEnvVars:
    """Karakeep env vars come from the encrypted secrets table after the
    modules / connected services refactor — the karakeep resource type was
    retired with that change.
    """

    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text('[files]\ndescription = "File ops"\nalways_include = true\n')
        (skills_dir / "files.md").write_text("File operations guide.")
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        users = {"alice": UserConfig()}
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            # Real bundled skills dir so the bookmarks manifest is loaded.
            bundled_skills_dir=None,
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount_path,
            users=users,
            security=SecurityConfig(skill_proxy_enabled=False),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_karakeep_env_vars_set_when_secrets_configured(self, mock_run, tmp_path, monkeypatch):
        from istota import secrets_store

        monkeypatch.setenv("ISTOTA_SECRET_KEY", "x" * 64)
        config = self._make_config(tmp_path)
        secrets_store.set_secret(
            config.db_path, "alice", "karakeep", "base_url",
            "https://keep.example.com/api/v1",
        )
        secrets_store.set_secret(
            config.db_path, "alice", "karakeep", "api_key", "kk-secret",
        )
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            conn.commit()  # release writer lock so secrets_store can bump last_accessed_at
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["KARAKEEP_BASE_URL"] == "https://keep.example.com/api/v1"
        assert env["KARAKEEP_API_KEY"] == "kk-secret"

    @patch("istota.executor.subprocess.run")
    def test_karakeep_env_vars_not_set_when_no_secrets(self, mock_run, tmp_path, monkeypatch):
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "x" * 64)
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "KARAKEEP_BASE_URL" not in env
        assert "KARAKEEP_API_KEY" not in env

    @patch("istota.executor.subprocess.run")
    def test_karakeep_env_vars_partial_when_only_one_secret(self, mock_run, tmp_path, monkeypatch):
        # Phase 3: ``bookmarks`` auto-authorizes only when its sensitive
        # spec (``KARAKEEP_API_KEY``) resolves. With only ``base_url``
        # configured and ``bookmarks`` not selected, the skill is not
        # authorized and none of its env vars flow. The user-facing
        # signal is "the half-configured user gets nothing" — a cleaner
        # failure mode than the Phase 2 partial-env shape.
        from istota import secrets_store

        monkeypatch.setenv("ISTOTA_SECRET_KEY", "x" * 64)
        config = self._make_config(tmp_path)
        secrets_store.set_secret(
            config.db_path, "alice", "karakeep", "base_url",
            "https://keep.example.com/api/v1",
        )
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            conn.commit()
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "KARAKEEP_BASE_URL" not in env
        assert "KARAKEEP_API_KEY" not in env


class TestWebsitePromptSection:
    def test_website_in_prompt_when_enabled(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount_path,
            site=SiteConfig(enabled=True, hostname="istota.example.com"),
            users={"alice": UserConfig(site_enabled=True)},
        )
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="build my website", user_id="alice", source_type="talk")
            task = db.get_task(conn, task_id)
        prompt = build_prompt(task, [], config)
        assert "https://istota.example.com/~alice" in prompt
        assert "Users/alice/istota/html" in prompt
        assert "Website:" in prompt

    def test_website_not_in_prompt_when_disabled(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        config = Config(
            db_path=db_path,
            site=SiteConfig(enabled=False),
            users={"alice": UserConfig(site_enabled=True)},
        )
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="build my website", user_id="alice", source_type="talk")
            task = db.get_task(conn, task_id)
        prompt = build_prompt(task, [], config)
        assert "Website:" not in prompt

    def test_website_not_in_prompt_when_user_not_enabled(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        config = Config(
            db_path=db_path,
            site=SiteConfig(enabled=True, hostname="istota.example.com"),
            users={"alice": UserConfig(site_enabled=False)},
        )
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="build my website", user_id="alice", source_type="talk")
            task = db.get_task(conn, task_id)
        prompt = build_prompt(task, [], config)
        assert "Website:" not in prompt


# ---------------------------------------------------------------------------
# TestAdminIsolation
# ---------------------------------------------------------------------------


class TestAdminPromptIsolation:
    def _make_config(self, tmp_path, admin_users=None):
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        return Config(
            db_path=tmp_path / "test.db",
            nextcloud_mount_path=mount_path,
            admin_users=admin_users or set(),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    def test_admin_prompt_has_db_path(self, tmp_path):
        config = self._make_config(tmp_path)
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=True)
        assert f"Database path: {config.db_path}" in prompt

    def test_non_admin_prompt_has_restricted_db(self, tmp_path):
        config = self._make_config(tmp_path, admin_users={"bob"})
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=False)
        assert "Database path: (restricted)" in prompt
        assert str(config.db_path) not in prompt

    def test_prompt_has_no_sqlite3_tool(self, tmp_path):
        """sqlite3 tool removed in favor of deferred JSON operations."""
        config = self._make_config(tmp_path)
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=True)
        assert "sqlite3 for the task database" not in prompt

    def test_admin_prompt_no_subtask_instructions(self, tmp_path):
        """Subtask creation instructions should NOT be in the hardcoded prompt.

        They belong in the tasks skill doc, loaded only when relevant.
        """
        config = self._make_config(tmp_path)
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=True)
        assert "create subtasks" not in prompt.lower()

    def test_non_admin_prompt_no_subtask_rule(self, tmp_path):
        config = self._make_config(tmp_path, admin_users={"bob"})
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=False)
        assert "create subtasks" not in prompt

    def test_admin_prompt_has_full_mount_path(self, tmp_path):
        config = self._make_config(tmp_path)
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=True)
        assert f"mounted at '{config.nextcloud_mount_path}'" in prompt

    def test_non_admin_prompt_has_scoped_mount_path(self, tmp_path):
        config = self._make_config(tmp_path, admin_users={"bob"})
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        scoped = str(config.nextcloud_mount_path / "Users" / "alice")
        prompt = build_prompt(task, [], config, is_admin=False)
        assert f"mounted at '{scoped}'" in prompt

    def test_non_admin_prompt_has_restricted_access_rule(self, tmp_path):
        config = self._make_config(tmp_path, admin_users={"bob"})
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=False)
        assert "You can ONLY access files under" in prompt
        assert "do NOT have access to the task database" in prompt

    def test_prompt_includes_utc_anchor_and_elapsed_time_rule(self, tmp_path):
        """ISSUE-091 — UTC anchor + elapsed-time rule must be present."""
        import re
        config = self._make_config(tmp_path)
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        for is_admin in (True, False):
            prompt = build_prompt(task, [], config, is_admin=is_admin)
            assert re.search(r"Current UTC: \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", prompt), (
                "Current UTC ISO 8601 line missing from prompt header"
            )
            assert "normalize both to ISO 8601 UTC" in prompt, (
                "Elapsed-time arithmetic rule missing from rules section"
            )


class TestAdminEnvVarIsolation:
    def _make_config(self, tmp_path, admin_users=None):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text('[files]\ndescription = "File ops"\nalways_include = true\n')
        (skills_dir / "files.md").write_text("File operations guide.")
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount_path,
            admin_users=admin_users or set(),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_admin_gets_db_path_env(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["ISTOTA_DB_PATH"] == str(config.db_path)

    @patch("istota.executor.subprocess.run")
    def test_non_admin_no_db_path_env(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, admin_users={"bob"})
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "ISTOTA_DB_PATH" not in env

    @patch("istota.executor.subprocess.run")
    def test_admin_gets_full_mount_path_env(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["NEXTCLOUD_MOUNT_PATH"] == str(config.nextcloud_mount_path)

    @patch("istota.executor.subprocess.run")
    def test_non_admin_gets_scoped_mount_path_env(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, admin_users={"bob"})
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        expected = str(config.nextcloud_mount_path / "Users" / "alice")
        assert env["NEXTCLOUD_MOUNT_PATH"] == expected

    @patch("istota.executor.subprocess.run")
    def test_admin_skills_include_admin_only(self, mock_run, tmp_path):
        """Admin user should get admin-only skills like schedules in the prompt."""
        config = self._make_config(tmp_path)
        skills_dir = config.skills_dir
        (skills_dir / "_index.toml").write_text(
            '[files]\ndescription = "File ops"\nalways_include = true\n\n'
            '[schedules]\ndescription = "Scheduled jobs"\nkeywords = ["schedule"]\nadmin_only = true\n'
        )
        (skills_dir / "schedules.md").write_text("Admin scheduling reference.")
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="set up a schedule", user_id="alice", source_type="talk")
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        prompt_text = mock_run.call_args.kwargs["input"]
        assert "Admin scheduling reference" in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_non_admin_skills_exclude_admin_only(self, mock_run, tmp_path):
        """Non-admin user should NOT get admin-only skills."""
        config = self._make_config(tmp_path, admin_users={"bob"})
        skills_dir = config.skills_dir
        (skills_dir / "_index.toml").write_text(
            '[files]\ndescription = "File ops"\nalways_include = true\n\n'
            '[schedules]\ndescription = "Scheduled jobs"\nkeywords = ["schedule"]\nadmin_only = true\n'
        )
        (skills_dir / "schedules.md").write_text("Admin scheduling reference.")
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="set up a schedule", user_id="alice", source_type="talk")
            task = db.get_task(conn, task_id)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        prompt_text = mock_run.call_args.kwargs["input"]
        assert "Admin scheduling reference" not in prompt_text


class TestDeferredDirEnvVar:
    """ISTOTA_DEFERRED_DIR env var should always be set."""

    def _make_config(self, tmp_path, admin_users=None):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text('[files]\ndescription = "File ops"\nalways_include = true\n')
        (skills_dir / "files.md").write_text("File operations guide.")
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount_path,
            admin_users=admin_users or set(),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_deferred_dir_set_for_admin(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["ISTOTA_DEFERRED_DIR"] == str(tmp_path / "temp" / "alice")

    @patch("istota.executor.subprocess.run")
    def test_deferred_dir_set_for_non_admin(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, admin_users={"bob"})
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["ISTOTA_DEFERRED_DIR"] == str(tmp_path / "temp" / "alice")

    @patch("istota.executor.subprocess.run")
    def test_experimental_features_propagated(self, mock_run, tmp_path):
        """LLM-path subprocess must carry ISTOTA_EXPERIMENTAL_FEATURES so
        skills invoked via the skill proxy (which forwards env to skill CLIs)
        see consistent gating with the scheduler subprocess paths."""
        from istota.config import ExperimentalConfig
        config = self._make_config(tmp_path)
        config.experimental = ExperimentalConfig(features=["money_tax", "money_wash_sales"])
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["ISTOTA_EXPERIMENTAL_FEATURES"] == "money_tax,money_wash_sales"

    @patch("istota.executor.subprocess.run")
    def test_experimental_features_empty_when_unset(self, mock_run, tmp_path):
        """Always-set contract: even with no features enabled, the var
        exists (empty string) so consumers don't have to dance around
        os.environ.get(...) returning None."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["ISTOTA_EXPERIMENTAL_FEATURES"] == ""


# ---------------------------------------------------------------------------
# TestCalDAVCredentialScoping
# ---------------------------------------------------------------------------


class TestCalDAVCredentialScoping:
    """CalDAV credentials should only be injected when user has calendars."""

    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text(
            '[files]\ndescription = "File ops"\nalways_include = true\n'
        )
        (skills_dir / "files.md").write_text("File operations guide.")
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            # Real bundled skills dir so the calendar manifest's
            # gate_has_discovered_calendars CALDAV_* specs are loaded.
            bundled_skills_dir=None,
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount_path,
            nextcloud=NextcloudConfig(
                url="https://nc.example.com",
                username="bot",
                app_password="secret",
            ),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.get_calendars_for_user")
    @patch("istota.executor.get_caldav_client")
    @patch("istota.executor.subprocess.run")
    def test_caldav_creds_present_when_user_has_calendars(
        self, mock_run, mock_client, mock_cals, tmp_path,
    ):
        mock_cals.return_value = [("Personal", "https://cal/personal", True)]
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "CALDAV_URL" in env
        assert "CALDAV_USERNAME" in env

    @patch("istota.executor.get_calendars_for_user")
    @patch("istota.executor.get_caldav_client")
    @patch("istota.executor.subprocess.run")
    def test_caldav_creds_absent_when_no_calendars(
        self, mock_run, mock_client, mock_cals, tmp_path,
    ):
        mock_cals.return_value = []  # No calendars for this user
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "CALDAV_URL" not in env
        assert "CALDAV_USERNAME" not in env

    @patch("istota.executor.subprocess.run")
    def test_caldav_creds_absent_when_no_caldav_config(self, mock_run, tmp_path):
        """No CalDAV configured at all — creds should not appear."""
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        config = self._make_config(tmp_path)
        config.nextcloud = NextcloudConfig()  # No URL = no CalDAV
        (tmp_path / "temp" / "alice").mkdir(parents=True)

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert "CALDAV_URL" not in env
        assert "CALDAV_USERNAME" not in env


# ---------------------------------------------------------------------------
# TestUserIdSubstitution
# ---------------------------------------------------------------------------


class TestUserIdSubstitution:
    """Skill docs should have {user_id} replaced with actual user ID."""

    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text(
            '[memory]\ndescription = "Memory"\nalways_include = true\n'
        )
        skill_dir = skills_dir / "memory"
        skill_dir.mkdir()
        (skill_dir / "skill.toml").write_text(
            'description = "Memory"\nalways_include = true\n'
        )
        (skill_dir / "skill.md").write_text(
            "Memory file at /Users/{user_id}/bot/config/USER.md"
        )
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount_path,
        )

    def _make_task(self, conn, user_id="alice"):
        task_id = db.create_task(conn, prompt="test", user_id=user_id, source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_user_id_substituted_in_skills_doc(self, mock_run, tmp_path):
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, user_id="alice")
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        # The prompt is passed via stdin — check it contains the substituted user_id
        call_kwargs = mock_run.call_args[1]
        prompt_input = call_kwargs.get("input", "")
        assert "/Users/alice/bot/config/USER.md" in prompt_input
        assert "{user_id}" not in prompt_input


# ---------------------------------------------------------------------------
# TestLoadPersona
# ---------------------------------------------------------------------------


class TestLoadPersona:
    def _make_config(self, tmp_path, use_mount=True):
        config_dir = tmp_path / "config"
        skills_dir = config_dir / "skills"
        skills_dir.mkdir(parents=True)
        kwargs = dict(skills_dir=skills_dir, bundled_skills_dir=tmp_path / "_empty_bundled")
        if use_mount:
            mount = tmp_path / "mount"
            mount.mkdir()
            kwargs["nextcloud_mount_path"] = mount
        return Config(**kwargs)

    def test_user_persona_overrides_global(self, tmp_path):
        config = self._make_config(tmp_path)
        # Create global persona
        (tmp_path / "config" / "persona.md").write_text("Global persona")
        # Create user workspace persona
        user_dir = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config"
        user_dir.mkdir(parents=True)
        (user_dir / "PERSONA.md").write_text("Custom persona for Alice")

        result = load_persona(config, user_id="alice")
        assert result == "Custom persona for Alice"

    def test_empty_user_persona_falls_back_to_global(self, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "config" / "persona.md").write_text("Global persona")
        user_dir = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config"
        user_dir.mkdir(parents=True)
        (user_dir / "PERSONA.md").write_text("   ")

        result = load_persona(config, user_id="alice")
        assert result == "Global persona"

    def test_missing_user_persona_falls_back_to_global(self, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "config" / "persona.md").write_text("Global persona")

        result = load_persona(config, user_id="alice")
        assert result == "Global persona"

    def test_no_mount_falls_back_to_global(self, tmp_path):
        config = self._make_config(tmp_path, use_mount=False)
        (tmp_path / "config" / "persona.md").write_text("Global persona")

        result = load_persona(config, user_id="alice")
        assert result == "Global persona"

    def test_no_user_id_falls_back_to_global(self, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "config" / "persona.md").write_text("Global persona")

        result = load_persona(config, user_id=None)
        assert result == "Global persona"

    def test_bot_name_substituted_in_user_persona(self, tmp_path):
        config = self._make_config(tmp_path)
        config.bot_name = "Jarvis"
        user_dir = config.nextcloud_mount_path / "Users" / "alice" / "jarvis" / "config"
        user_dir.mkdir(parents=True)
        (user_dir / "PERSONA.md").write_text("You are {BOT_NAME}, a helpful bot.")

        result = load_persona(config, user_id="alice")
        assert result == "You are Jarvis, a helpful bot."

    def test_bot_name_substituted_in_global_persona(self, tmp_path):
        config = self._make_config(tmp_path)
        config.bot_name = "Jarvis"
        (tmp_path / "config" / "persona.md").write_text("You are {BOT_NAME}.")

        result = load_persona(config)
        assert result == "You are Jarvis."

    def test_no_persona_files_returns_none(self, tmp_path):
        config = self._make_config(tmp_path)
        result = load_persona(config, user_id="alice")
        assert result is None


# ---------------------------------------------------------------------------
# TestLoadEmissaries
# ---------------------------------------------------------------------------


class TestLoadEmissaries:
    def _make_config(self, tmp_path):
        config_dir = tmp_path / "config"
        skills_dir = config_dir / "skills"
        skills_dir.mkdir(parents=True)
        return Config(skills_dir=skills_dir, bundled_skills_dir=tmp_path / "_empty_bundled")

    def test_returns_none_when_absent(self, tmp_path):
        config = self._make_config(tmp_path)
        assert load_emissaries(config) is None

    def test_returns_content_when_present(self, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "config" / "emissaries.md").write_text("# Emissaries\n\nBe good.")
        result = load_emissaries(config)
        assert result == "# Emissaries\n\nBe good."

    def test_no_bot_name_substitution(self, tmp_path):
        config = self._make_config(tmp_path)
        config.bot_name = "Jarvis"
        (tmp_path / "config" / "emissaries.md").write_text("Agent {BOT_NAME} principles")
        result = load_emissaries(config)
        assert result == "Agent {BOT_NAME} principles"

    def test_returns_none_when_disabled(self, tmp_path):
        config = self._make_config(tmp_path)
        config.emissaries_enabled = False
        (tmp_path / "config" / "emissaries.md").write_text("# Emissaries\n\nBe good.")
        assert load_emissaries(config) is None


class TestEmissariesInPrompt:
    def _make_task(self):
        return db.Task(
            id=1, status="running", prompt="hello", user_id="alice",
            source_type="talk", conversation_token="room1",
            created_at="2024-01-01T00:00:00",
        )

    def test_emissaries_appears_in_prompt(self):
        task = self._make_task()
        result = build_prompt(
            task, [], Config(), emissaries="# Emissaries\n\nBe good.",
        )
        assert "# Emissaries" in result
        assert "Be good." in result

    def test_emissaries_before_persona(self, tmp_path):
        task = self._make_task()
        config_dir = tmp_path / "config"
        skills_dir = config_dir / "skills"
        skills_dir.mkdir(parents=True)
        (config_dir / "persona.md").write_text("# Persona\n\nBe helpful.")
        config = Config(skills_dir=skills_dir, bundled_skills_dir=tmp_path / "_empty_bundled")

        result = build_prompt(
            task, [], config, emissaries="# Emissaries\n\nBe good.",
        )
        emissaries_pos = result.index("# Emissaries")
        persona_pos = result.index("# Persona")
        assert emissaries_pos < persona_pos

    def test_emissaries_absent_when_no_file(self):
        task = self._make_task()
        result = build_prompt(task, [], Config())
        assert "Emissaries" not in result


# ---------------------------------------------------------------------------
# TestPreTranscribeAttachments
# ---------------------------------------------------------------------------


_TRANSCRIBE_PATCH = "istota.skills.whisper.transcribe.transcribe_audio"


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
        assert result.startswith("Transcribed voice message:")
        mock_transcribe.assert_called_once_with("/tmp/voice.mp3")

    @patch(_TRANSCRIBE_PATCH)
    def test_transcription_failure_returns_prompt_unchanged(self, mock_transcribe):
        mock_transcribe.return_value = {"status": "error", "error": "corrupted file"}
        result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "[voice.mp3]")
        assert result == "[voice.mp3]"

    @patch(_TRANSCRIBE_PATCH)
    def test_transcription_exception_returns_prompt_unchanged(self, mock_transcribe):
        mock_transcribe.side_effect = RuntimeError("boom")
        result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "[voice.mp3]")
        assert result == "[voice.mp3]"

    def test_faster_whisper_not_installed_returns_prompt_unchanged(self):
        """When the whisper module can't be imported, graceful fallback."""
        with patch.dict("sys.modules", {"istota.skills.whisper.transcribe": None}):
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
        mock_transcribe.assert_called_once_with("/tmp/memo.m4a")

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
        assert "first part" in result
        assert "second part" in result
        assert "a.mp3" in result
        assert "b.wav" in result

    @patch(_TRANSCRIBE_PATCH)
    def test_empty_transcription_returns_prompt_unchanged(self, mock_transcribe):
        mock_transcribe.return_value = {"status": "ok", "text": "  "}
        result = _pre_transcribe_attachments(["/tmp/voice.mp3"], "[voice.mp3]")
        assert result == "[voice.mp3]"

    def test_all_audio_extensions_recognized(self):
        for ext in ["mp3", "wav", "ogg", "flac", "m4a", "opus", "webm", "mp4", "aac", "wma"]:
            assert ext in _AUDIO_EXTENSIONS


# ---------------------------------------------------------------------------
# TestPreshrinkImageAttachments
# ---------------------------------------------------------------------------


def _make_image(
    path: Path,
    size: tuple[int, int],
    exif_orientation: int | None = None,
    fmt: str = "JPEG",
    mode: str = "RGB",
    color=(200, 50, 50),
    icc_profile: bytes | None = None,
):
    """Helper: write an image of given format/mode/size, optionally with EXIF
    orientation or an ICC profile attached."""
    from PIL import Image
    img = Image.new(mode, size, color=color)
    kwargs: dict = {}
    if fmt == "JPEG":
        kwargs["quality"] = 90
    if exif_orientation is not None:
        exif = img.getexif()
        exif[0x0112] = exif_orientation  # Orientation tag
        kwargs["exif"] = exif.tobytes()
    if icc_profile is not None:
        kwargs["icc_profile"] = icc_profile
    img.save(path, fmt, **kwargs)


class TestPreshrinkImageAttachments:
    def test_no_attachments_passthrough(self, tmp_path):
        assert _preshrink_image_attachments(None, tmp_path, 1) is None
        assert _preshrink_image_attachments([], tmp_path, 1) == []

    def test_non_image_passthrough(self, tmp_path):
        result = _preshrink_image_attachments(
            ["/tmp/voice.mp3", "/tmp/doc.pdf"], tmp_path, 1,
        )
        assert result == ["/tmp/voice.mp3", "/tmp/doc.pdf"]

    def test_small_image_passthrough(self, tmp_path):
        src = tmp_path / "small.jpg"
        _make_image(src, (800, 600))
        result = _preshrink_image_attachments([str(src)], tmp_path, 7)
        assert result == [str(src)]
        # No output directory created for a no-op
        assert not (tmp_path / "attachments" / "task_7").exists()

    def test_large_image_resized(self, tmp_path):
        from PIL import Image
        src = tmp_path / "big.jpg"
        _make_image(src, (4032, 3024))
        result = _preshrink_image_attachments([str(src)], tmp_path, 42)
        assert len(result) == 1
        out = Path(result[0])
        assert out != src
        assert out.parent == tmp_path / "attachments" / "task_42"
        assert out.suffix == ".jpg"
        with Image.open(out) as resized:
            assert max(resized.size) == _IMAGE_MAX_EDGE
            # Aspect ratio is preserved (4:3 → 1568x1176)
            assert resized.size == (_IMAGE_MAX_EDGE, _IMAGE_MAX_EDGE * 3 // 4)

    def test_exif_rotation_applied(self, tmp_path):
        """Orientation=6 (CW 90°) — a 4000x3000 landscape becomes a 3000x4000 portrait
        after transpose; longest edge stays the long side after resize."""
        from PIL import Image
        src = tmp_path / "rotated.jpg"
        _make_image(src, (4000, 3000), exif_orientation=6)
        result = _preshrink_image_attachments([str(src)], tmp_path, 9)
        out = Path(result[0])
        with Image.open(out) as resized:
            w, h = resized.size
            # After EXIF transpose the image is portrait (taller than wide)
            assert h > w
            assert max(w, h) == _IMAGE_MAX_EDGE
            # Output should not carry over a leftover orientation tag
            exif = resized.getexif()
            assert exif.get(0x0112, 1) == 1

    def test_missing_file_passthrough(self, tmp_path):
        result = _preshrink_image_attachments(
            ["/tmp/does_not_exist.jpg"], tmp_path, 1,
        )
        assert result == ["/tmp/does_not_exist.jpg"]

    def test_corrupt_image_passthrough(self, tmp_path):
        src = tmp_path / "corrupt.jpg"
        src.write_bytes(b"not actually a jpeg")
        result = _preshrink_image_attachments([str(src)], tmp_path, 1)
        assert result == [str(src)]

    def test_mixed_attachments(self, tmp_path):
        big = tmp_path / "photo.jpg"
        _make_image(big, (3000, 2000))
        result = _preshrink_image_attachments(
            ["/tmp/voice.mp3", str(big), "/tmp/note.txt"], tmp_path, 11,
        )
        assert result[0] == "/tmp/voice.mp3"
        assert result[2] == "/tmp/note.txt"
        assert result[1] != str(big)
        assert Path(result[1]).exists()

    def test_pil_missing_passthrough(self, tmp_path):
        with patch.dict("sys.modules", {"PIL": None}):
            result = _preshrink_image_attachments(
                ["/tmp/photo.jpg"], tmp_path, 1,
            )
            assert result == ["/tmp/photo.jpg"]

    def test_large_png_resized(self, tmp_path):
        from PIL import Image
        src = tmp_path / "big.png"
        _make_image(src, (3000, 2250), fmt="PNG")
        result = _preshrink_image_attachments([str(src)], tmp_path, 13)
        out = Path(result[0])
        assert out != src
        assert out.suffix == ".jpg"
        with Image.open(out) as resized:
            assert max(resized.size) == _IMAGE_MAX_EDGE

    def test_large_webp_resized(self, tmp_path):
        from PIL import Image
        src = tmp_path / "big.webp"
        _make_image(src, (3000, 2250), fmt="WEBP")
        result = _preshrink_image_attachments([str(src)], tmp_path, 14)
        out = Path(result[0])
        assert out.suffix == ".jpg"
        with Image.open(out) as resized:
            assert max(resized.size) == _IMAGE_MAX_EDGE

    def test_rgba_flattens_onto_white(self, tmp_path):
        """Transparent screenshots should land on white, not the default black."""
        from PIL import Image
        src = tmp_path / "translucent.png"
        # Fully transparent RGBA — every pixel should resolve to the background.
        _make_image(src, (3000, 2000), fmt="PNG", mode="RGBA", color=(0, 0, 0, 0))
        result = _preshrink_image_attachments([str(src)], tmp_path, 15)
        out = Path(result[0])
        with Image.open(out) as resized:
            # JPEG quantization is lossy at quality=85, so allow some slack but
            # the result has to be far closer to white than to black.
            px = resized.convert("RGB").getpixel((resized.size[0] // 2, resized.size[1] // 2))
            assert min(px) > 200, f"expected near-white, got {px}"

    def test_small_rotated_image_is_still_rewritten(self, tmp_path):
        """H1 regression: an 800x600 scan with orientation=6 needs a physically
        rotated copy because Tesseract OCR doesn't honor EXIF."""
        from PIL import Image
        src = tmp_path / "scan.jpg"
        _make_image(src, (800, 600), exif_orientation=6)
        result = _preshrink_image_attachments([str(src)], tmp_path, 16)
        out = Path(result[0])
        assert out != src, "small but rotated image should still be rewritten"
        with Image.open(out) as fixed:
            w, h = fixed.size
            # After applying orientation=6 (CW 90°) the image becomes portrait.
            assert h > w
            # Output drops the orientation tag.
            assert fixed.getexif().get(0x0112, 1) == 1

    def test_colliding_stems_do_not_overwrite(self, tmp_path):
        """M1 regression: two attachments sharing a stem (photo.png + photo.jpg,
        or duplicate IMG_1234.jpg from different dirs) must not overwrite."""
        from PIL import Image
        a_dir = tmp_path / "a"
        b_dir = tmp_path / "b"
        a_dir.mkdir()
        b_dir.mkdir()
        # Distinguishable colors so we can verify content survives.
        _make_image(a_dir / "photo.jpg", (3000, 2000), color=(255, 0, 0))
        _make_image(b_dir / "photo.jpg", (3000, 2000), color=(0, 255, 0))
        result = _preshrink_image_attachments(
            [str(a_dir / "photo.jpg"), str(b_dir / "photo.jpg")],
            tmp_path,
            17,
        )
        assert len(result) == 2
        out_a = Path(result[0])
        out_b = Path(result[1])
        assert out_a != out_b, "outputs must not collide"
        assert out_a.exists() and out_b.exists()
        with Image.open(out_a) as ia, Image.open(out_b) as ib:
            # A is mostly red, B is mostly green.
            pa = ia.getpixel((ia.size[0] // 2, ia.size[1] // 2))
            pb = ib.getpixel((ib.size[0] // 2, ib.size[1] // 2))
            assert pa[0] > pa[1], f"first output should still be red-dominant, got {pa}"
            assert pb[1] > pb[0], f"second output should still be green-dominant, got {pb}"

    def test_icc_profile_preserved(self, tmp_path):
        """Md2: color-managed images keep their ICC profile through the
        re-encode (matters for bloodwork OCR with color reference charts)."""
        from PIL import Image, ImageCms
        # Build a synthetic sRGB ICC profile so we don't depend on system files.
        srgb_profile = ImageCms.createProfile("sRGB")
        icc_bytes = ImageCms.ImageCmsProfile(srgb_profile).tobytes()
        src = tmp_path / "color.jpg"
        _make_image(src, (3000, 2000), icc_profile=icc_bytes)
        result = _preshrink_image_attachments([str(src)], tmp_path, 18)
        out = Path(result[0])
        with Image.open(out) as resized:
            assert resized.info.get("icc_profile"), "ICC profile should be carried over"


# ---------------------------------------------------------------------------
# TestPromptOutputTarget
# ---------------------------------------------------------------------------


class TestPromptOutputTarget:
    """Verify that source_type and output_target appear in the prompt header."""

    def _make_task(self, source_type="talk", output_target=None):
        return db.Task(
            id=1, status="running", prompt="hello", user_id="alice",
            source_type=source_type, conversation_token="room1",
            output_target=output_target,
        )

    def test_talk_source_and_target_in_prompt(self):
        task = self._make_task(source_type="talk")
        result = build_prompt(
            task, [], Config(),
            source_type="talk", output_target="talk",
        )
        assert "Source: talk" in result
        assert "Output target: talk" in result

    def test_scheduled_source_with_email_target(self):
        task = self._make_task(source_type="scheduled", output_target="email")
        result = build_prompt(
            task, [], Config(),
            source_type="scheduled", output_target="email",
        )
        assert "Source: scheduled" in result
        assert "Output target: email" in result

    def test_defaults_when_no_output_target(self):
        task = self._make_task(source_type="cli")
        result = build_prompt(task, [], Config())
        assert "Source: cli" in result
        assert "Output target: text" in result

    def test_email_tool_line_distinguishes_send_and_output(self):
        task = self._make_task(source_type="talk")
        result = build_prompt(task, [], Config())
        assert "email send" in result
        assert "email output" in result
        assert "Only use `output` when this task arrived as an incoming email" in result


# ---------------------------------------------------------------------------
# TestDetectNotificationReply
# ---------------------------------------------------------------------------


class TestDetectNotificationReply:
    def test_returns_parent_for_scheduled_source_type(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            # Create a completed scheduled parent task with a talk_response_id
            parent_id = db.create_task(
                conn, prompt="Drink water", user_id="alice",
                source_type="scheduled", conversation_token="room1",
            )
            db.update_task_status(conn, parent_id, "completed", result="Time to drink water!")
            # Set talk_response_id on the parent
            conn.execute(
                "UPDATE tasks SET talk_response_id = ? WHERE id = ?",
                (42, parent_id),
            )
            conn.commit()

            # Create a reply task
            reply_id = db.create_task(
                conn, prompt="Drinking", user_id="alice",
                source_type="talk", conversation_token="room1",
                reply_to_talk_id=42,
            )
            reply_task = db.get_task(conn, reply_id)

            result = _detect_notification_reply(reply_task, Config(), conn)
            assert result is not None
            assert result.id == parent_id
            assert result.source_type == "scheduled"

    def test_returns_parent_for_briefing_source_type(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Morning briefing", user_id="alice",
                source_type="briefing", conversation_token="room1",
            )
            db.update_task_status(conn, parent_id, "completed", result="Good morning!")
            conn.execute(
                "UPDATE tasks SET talk_response_id = ? WHERE id = ?",
                (99, parent_id),
            )
            conn.commit()

            reply_id = db.create_task(
                conn, prompt="Thanks", user_id="alice",
                source_type="talk", conversation_token="room1",
                reply_to_talk_id=99,
            )
            reply_task = db.get_task(conn, reply_id)

            result = _detect_notification_reply(reply_task, Config(), conn)
            assert result is not None
            assert result.source_type == "briefing"

    def test_returns_none_for_talk_source_type(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="What's up?", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            db.update_task_status(conn, parent_id, "completed", result="Not much!")
            conn.execute(
                "UPDATE tasks SET talk_response_id = ? WHERE id = ?",
                (50, parent_id),
            )
            conn.commit()

            reply_id = db.create_task(
                conn, prompt="Cool", user_id="alice",
                source_type="talk", conversation_token="room1",
                reply_to_talk_id=50,
            )
            reply_task = db.get_task(conn, reply_id)

            result = _detect_notification_reply(reply_task, Config(), conn)
            assert result is None

    def test_returns_none_when_no_reply_to_talk_id(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Hello", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            task = db.get_task(conn, task_id)

            result = _detect_notification_reply(task, Config(), conn)
            assert result is None

    def test_returns_none_when_no_conn(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Hello", user_id="alice",
                source_type="talk", conversation_token="room1",
                reply_to_talk_id=42,
            )
            task = db.get_task(conn, task_id)

        result = _detect_notification_reply(task, Config(), None)
        assert result is None


# ---------------------------------------------------------------------------
# TestNotificationReplyContextScoping
# ---------------------------------------------------------------------------


class TestNotificationReplyContextScoping:
    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text(
            '[files]\ndescription = "File ops"\nalways_include = true\n'
        )
        (skills_dir / "files.md").write_text("File operations guide.")
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
        )

    @patch("istota.executor.subprocess.run")
    def test_notification_reply_scopes_context(self, mock_run, tmp_path):
        """Reply to a scheduled notification gets scoped context, not full history."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            # Create completed scheduled parent
            parent_id = db.create_task(
                conn, prompt="Drink water", user_id="alice",
                source_type="scheduled", conversation_token="room1",
            )
            db.update_task_status(
                conn, parent_id, "completed",
                result="Time to hydrate! Remember to drink water.",
            )
            conn.execute(
                "UPDATE tasks SET talk_response_id = ? WHERE id = ?",
                (42, parent_id),
            )
            conn.commit()

            # Create reply task
            reply_id = db.create_task(
                conn, prompt="Drinking", user_id="alice",
                source_type="talk", conversation_token="room1",
                reply_to_talk_id=42,
            )
            reply_task = db.get_task(conn, reply_id)

            from istota.executor import execute_task
            success, result, _actions, _trace = execute_task(
                reply_task, config, [], conn=conn,
            )

        # Check the prompt contains the notification hint
        call_args = mock_run.call_args
        prompt_text = call_args.kwargs["input"]
        assert "replying to a scheduled notification" in prompt_text
        assert "respond very briefly" in prompt_text
        assert "Time to hydrate" in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_notification_reply_skips_full_context(self, mock_run, tmp_path):
        """Notification reply should not call _build_talk_api_context."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="Reminder", user_id="alice",
                source_type="scheduled", conversation_token="room1",
            )
            db.update_task_status(conn, parent_id, "completed", result="Do the thing")
            conn.execute(
                "UPDATE tasks SET talk_response_id = ? WHERE id = ?",
                (42, parent_id),
            )
            conn.commit()

            reply_id = db.create_task(
                conn, prompt="Done", user_id="alice",
                source_type="talk", conversation_token="room1",
                reply_to_talk_id=42,
            )
            reply_task = db.get_task(conn, reply_id)

            with patch("istota.executor._build_talk_api_context") as mock_talk_ctx:
                from istota.executor import execute_task
                execute_task(reply_task, config, [], conn=conn)
                mock_talk_ctx.assert_not_called()

    @patch("istota.executor.subprocess.run")
    def test_non_notification_reply_uses_normal_context(self, mock_run, tmp_path):
        """Reply to a regular talk message should use normal context loading."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            # Create completed talk parent (not scheduled)
            parent_id = db.create_task(
                conn, prompt="What's the weather?", user_id="alice",
                source_type="talk", conversation_token="room1",
            )
            db.update_task_status(conn, parent_id, "completed", result="It's sunny!")
            conn.execute(
                "UPDATE tasks SET talk_response_id = ? WHERE id = ?",
                (42, parent_id),
            )
            conn.commit()

            reply_id = db.create_task(
                conn, prompt="Thanks", user_id="alice",
                source_type="talk", conversation_token="room1",
                reply_to_talk_id=42,
            )
            reply_task = db.get_task(conn, reply_id)

            with patch("istota.executor._build_talk_api_context") as mock_talk_ctx:
                mock_talk_ctx.return_value = (None, set())  # Fall through to DB context
                from istota.executor import execute_task
                execute_task(reply_task, config, [], conn=conn)
                # Normal context path should be attempted
                mock_talk_ctx.assert_called_once()

        # Prompt should NOT contain notification hint
        call_args = mock_run.call_args
        prompt_text = call_args.kwargs["input"]
        assert "replying to a scheduled notification" not in prompt_text


# ---------------------------------------------------------------------------
# TestRecencyWindow
# ---------------------------------------------------------------------------


class TestRecencyWindowTalk:
    def _make_config(self, recency_hours=2.0, min_messages=10):
        from istota.config import ConversationConfig
        config = Config()
        config.conversation = ConversationConfig(
            context_recency_hours=recency_hours,
            context_min_messages=min_messages,
        )
        return config

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
        config = self._make_config(recency_hours=0)
        msgs = [self._make_talk_msg(i, 1000 + i) for i in range(20)]
        result = _apply_recency_window_talk(msgs, config)
        assert len(result) == 20

    def test_empty_messages(self):
        config = self._make_config()
        assert _apply_recency_window_talk([], config) == []

    def test_fewer_than_min_returns_all(self):
        config = self._make_config(min_messages=10)
        msgs = [self._make_talk_msg(i, 1000 + i) for i in range(8)]
        result = _apply_recency_window_talk(msgs, config)
        assert len(result) == 8

    def test_all_within_window_returns_all(self):
        config = self._make_config(recency_hours=2.0, min_messages=5)
        now = 1000000
        # 15 messages all within last hour
        msgs = [self._make_talk_msg(i, now - (15 - i) * 60) for i in range(15)]
        result = _apply_recency_window_talk(msgs, config)
        assert len(result) == 15

    def test_trims_old_messages_beyond_min(self):
        config = self._make_config(recency_hours=2.0, min_messages=5)
        now = 1000000
        # 5 messages from 10 hours ago
        old = [self._make_talk_msg(i, now - 36000 + i) for i in range(5)]
        # 10 messages from last 30 minutes
        recent = [self._make_talk_msg(10 + i, now - (10 - i) * 60) for i in range(10)]
        msgs = old + recent
        result = _apply_recency_window_talk(msgs, config)
        # 5 guaranteed recent (min) is less than the 10 recent, but all 10 recent
        # are within the 2h window, so we get 10 (within window) + 0 old = 10
        # Wait: min_messages=5 means guaranteed = last 5, older = first 10
        # Of the first 10 (5 old + 5 recent), only the 5 recent are within window
        assert len(result) == 10  # 5 within window from older + 5 guaranteed

    def test_guaranteed_minimum_always_kept(self):
        config = self._make_config(recency_hours=1.0, min_messages=10)
        now = 1000000
        # 20 messages, all from 5 hours ago
        msgs = [self._make_talk_msg(i, now - 18000 + i) for i in range(20)]
        # newest is at now - 18000 + 19, all within ~0 of each other
        # but the newest is the reference, so cutoff = newest - 3600
        # all messages are within 20 seconds of each other, so all within window
        # Let me make a better test: spread them out
        old_msgs = [self._make_talk_msg(i, now - 50000 + i * 100) for i in range(15)]
        recent_msgs = [self._make_talk_msg(15 + i, now - 60 + i * 10) for i in range(5)]
        msgs = old_msgs + recent_msgs
        result = _apply_recency_window_talk(msgs, config)
        # 10 guaranteed (last 10), older 10 checked against window
        # window = newest - 3600, old msgs are ~50000s ago, way outside
        # So result = 10 guaranteed minimum
        assert len(result) == 10

    def test_partial_window_inclusion(self):
        """Some older messages within window, some outside."""
        config = self._make_config(recency_hours=1.0, min_messages=3)
        now = 1000000
        # 2 messages from 5 hours ago (outside window)
        outside = [self._make_talk_msg(i, now - 18000 + i) for i in range(2)]
        # 3 messages from 30 minutes ago (within window)
        inside = [self._make_talk_msg(10 + i, now - 1800 + i * 60) for i in range(3)]
        # 3 messages from 5 minutes ago (guaranteed min)
        recent = [self._make_talk_msg(20 + i, now - 300 + i * 60) for i in range(3)]
        msgs = outside + inside + recent
        result = _apply_recency_window_talk(msgs, config)
        # guaranteed = last 3 (recent), older = outside + inside
        # inside (3) within window, outside (2) not
        assert len(result) == 6  # 3 inside + 3 guaranteed


class TestRecencyWindowDb:
    def _make_config(self, recency_hours=2.0, min_messages=10):
        from istota.config import ConversationConfig
        config = Config()
        config.conversation = ConversationConfig(
            context_recency_hours=recency_hours,
            context_min_messages=min_messages,
        )
        return config

    def _make_msg(self, msg_id, created_at, prompt="q", result="a"):
        return db.ConversationMessage(
            id=msg_id, prompt=prompt, result=result, created_at=created_at,
        )

    def test_disabled_when_zero(self):
        config = self._make_config(recency_hours=0)
        msgs = [self._make_msg(i, "2026-02-23 12:00:00") for i in range(20)]
        result = _apply_recency_window_db(msgs, config)
        assert len(result) == 20

    def test_empty_returns_empty(self):
        config = self._make_config()
        assert _apply_recency_window_db([], config) == []

    def test_fewer_than_min_returns_all(self):
        config = self._make_config(min_messages=10)
        msgs = [self._make_msg(i, f"2026-02-23 12:0{i}:00") for i in range(5)]
        result = _apply_recency_window_db(msgs, config)
        assert len(result) == 5

    def test_trims_old_db_messages(self):
        config = self._make_config(recency_hours=1.0, min_messages=3)
        msgs = [
            self._make_msg(1, "2026-02-23 08:00:00"),  # 4h before newest
            self._make_msg(2, "2026-02-23 09:00:00"),  # 3h before newest
            self._make_msg(3, "2026-02-23 11:30:00"),  # 30m before newest
            self._make_msg(4, "2026-02-23 11:45:00"),  # 15m before newest
            self._make_msg(5, "2026-02-23 12:00:00"),  # newest
        ]
        result = _apply_recency_window_db(msgs, config)
        # min=3 guaranteed (ids 3,4,5), older=[1,2], 1 and 2 are >1h old
        assert len(result) == 3
        assert [m.id for m in result] == [3, 4, 5]

    def test_keeps_within_window_beyond_min(self):
        config = self._make_config(recency_hours=2.0, min_messages=2)
        msgs = [
            self._make_msg(1, "2026-02-23 08:00:00"),  # outside
            self._make_msg(2, "2026-02-23 10:30:00"),  # within 2h
            self._make_msg(3, "2026-02-23 11:00:00"),  # within 2h
            self._make_msg(4, "2026-02-23 11:30:00"),  # guaranteed
            self._make_msg(5, "2026-02-23 12:00:00"),  # guaranteed (newest)
        ]
        result = _apply_recency_window_db(msgs, config)
        # guaranteed = [4,5], older = [1,2,3], within window = [2,3]
        assert len(result) == 4
        assert [m.id for m in result] == [2, 3, 4, 5]

    def test_unparseable_created_at_skips_filter(self):
        config = self._make_config(recency_hours=1.0, min_messages=2)
        msgs = [self._make_msg(i, "not-a-date") for i in range(5)]
        result = _apply_recency_window_db(msgs, config)
        # Can't parse newest, returns all
        assert len(result) == 5


# ---------------------------------------------------------------------------
# TestBuildPromptRecalledMemories
# ---------------------------------------------------------------------------


class TestBuildPromptRecalledMemories:
    def _make_task(self, **overrides):
        defaults = {
            "id": 1, "prompt": "test prompt", "user_id": "alice",
            "source_type": "talk", "status": "running",
        }
        defaults.update(overrides)
        return db.Task(**defaults)

    def test_recalled_section_included_when_provided(self):
        task = self._make_task()
        config = Config()
        prompt = build_prompt(
            task, [], config,
            recalled_memories="- [memory_file] User prefers dark mode\n- [conversation] Discussed project X",
        )
        assert "Recalled memories (from search)" in prompt
        assert "User prefers dark mode" in prompt
        assert "Discussed project X" in prompt

    def test_recalled_section_absent_when_none(self):
        task = self._make_task()
        config = Config()
        prompt = build_prompt(task, [], config, recalled_memories=None)
        assert "Recalled memories" not in prompt

    def test_recalled_section_absent_when_empty_string(self):
        task = self._make_task()
        config = Config()
        prompt = build_prompt(task, [], config, recalled_memories="")
        assert "Recalled memories" not in prompt

    def test_recalled_section_after_dated_memories(self):
        task = self._make_task()
        config = Config()
        prompt = build_prompt(
            task, [], config,
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
    def test_returns_none_when_disabled(self):
        from istota.executor import _recall_memories
        from istota.config import MemorySearchConfig
        config = Config(memory_search=MemorySearchConfig(enabled=True, auto_recall=False))
        task = db.Task(id=1, prompt="test", user_id="alice", source_type="talk", status="running")
        assert _recall_memories(config, None, task) is None

    def test_returns_none_when_search_not_enabled(self):
        from istota.executor import _recall_memories
        from istota.config import MemorySearchConfig
        config = Config(memory_search=MemorySearchConfig(enabled=False, auto_recall=True))
        task = db.Task(id=1, prompt="test", user_id="alice", source_type="talk", status="running")
        assert _recall_memories(config, None, task) is None

    def test_returns_none_when_skip_memory(self):
        from istota.executor import _recall_memories
        from istota.config import MemorySearchConfig
        config = Config(memory_search=MemorySearchConfig(enabled=True, auto_recall=True))
        task = db.Task(id=1, prompt="test", user_id="alice", source_type="talk", status="running")
        assert _recall_memories(config, None, task, skip_memory=True) is None

    @patch("istota.memory.search.search")
    def test_formats_results(self, mock_search):
        from istota.executor import _recall_memories
        from istota.config import MemorySearchConfig

        mock_result = MagicMock()
        mock_result.content = "User likes Python"
        mock_result.source_type = "memory_file"
        mock_search.return_value = [mock_result]

        config = Config(
            memory_search=MemorySearchConfig(enabled=True, auto_recall=True, auto_recall_limit=5),
            db_path=Path("/tmp/test.db"),
        )
        task = db.Task(id=1, prompt="what language?", user_id="alice", source_type="talk", status="running")

        conn = MagicMock()
        result = _recall_memories(config, conn, task)
        assert result is not None
        assert "[memory_file]" in result
        assert "User likes Python" in result

    @patch("istota.memory.search.search")
    def test_returns_none_when_no_results(self, mock_search):
        from istota.executor import _recall_memories
        from istota.config import MemorySearchConfig

        mock_search.return_value = []
        config = Config(
            memory_search=MemorySearchConfig(enabled=True, auto_recall=True),
            db_path=Path("/tmp/test.db"),
        )
        task = db.Task(id=1, prompt="test", user_id="alice", source_type="talk", status="running")
        assert _recall_memories(config, MagicMock(), task) is None

    @patch("istota.memory.search.search")
    def test_includes_channel_in_search(self, mock_search):
        from istota.executor import _recall_memories
        from istota.config import MemorySearchConfig

        mock_search.return_value = []
        config = Config(
            memory_search=MemorySearchConfig(enabled=True, auto_recall=True),
            db_path=Path("/tmp/test.db"),
        )
        task = db.Task(
            id=1, prompt="test", user_id="alice", source_type="talk", status="running",
            conversation_token="room123",
        )
        _recall_memories(config, MagicMock(), task)
        call_kwargs = mock_search.call_args[1]
        assert call_kwargs["include_user_ids"] == ["channel:room123"]


# ---------------------------------------------------------------------------
# TestApplyMemoryCap
# ---------------------------------------------------------------------------


class TestApplyMemoryCap:
    def test_unlimited_when_zero(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=0)
        u, d, c, r, k = _apply_memory_cap(config, "A" * 100, "B" * 100, "C" * 100, "D" * 100)
        assert len(u) == 100
        assert len(d) == 100
        assert len(c) == 100
        assert len(r) == 100

    def test_no_truncation_under_cap(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=500)
        u, d, c, r, k = _apply_memory_cap(config, "A" * 100, "B" * 100, "C" * 100, "D" * 100)
        assert len(u) == 100
        assert len(d) == 100
        assert len(c) == 100
        assert len(r) == 100

    def test_truncates_recalled_first(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=200)
        # total = 300, cap = 200, over = 100, recalled = 100 → removed entirely
        u, d, c, r, k = _apply_memory_cap(config, "A" * 100, "B" * 100, None, "D" * 100)
        assert u == "A" * 100
        assert d == "B" * 100
        assert r is None

    def test_truncates_dated_after_recalled(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=100)
        # total = 300, cap = 100, over = 200
        # recalled (100) removed → over = 100
        # dated (100) removed → over = 0
        u, d, c, r, k = _apply_memory_cap(config, "A" * 100, "B" * 100, None, "D" * 100)
        assert u == "A" * 100
        assert d is None
        assert r is None

    def test_partial_truncation(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=250)
        # total = 300, cap = 250, over = 50
        # recalled (100) → trim to 50 chars + truncation marker
        u, d, c, r, k = _apply_memory_cap(config, "A" * 100, "B" * 100, None, "D" * 100)
        assert u == "A" * 100
        assert d == "B" * 100
        assert r is not None
        assert "truncated" in r

    def test_handles_all_none(self):
        from istota.executor import _apply_memory_cap
        config = Config(max_memory_chars=100)
        u, d, c, r, k = _apply_memory_cap(config, None, None, None, None)
        assert u is None and d is None and c is None and r is None


# ---------------------------------------------------------------------------
# TestDatedMemoriesAutoLoad
# ---------------------------------------------------------------------------


class TestDatedMemoriesAutoLoad:
    def _make_config(self, tmp_path, auto_load_days=3, sleep_enabled=True):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text("")
        mount = tmp_path / "mount"
        mount.mkdir(exist_ok=True)
        from istota.config import SleepCycleConfig
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount,
            sleep_cycle=SleepCycleConfig(
                enabled=sleep_enabled,
                auto_load_dated_days=auto_load_days,
            ),
        )

    def _make_task(self, conn, source_type="talk"):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type=source_type)
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_dated_memories_loaded_when_enabled(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, auto_load_days=3)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        # Create a dated memory file
        from datetime import datetime
        memories_dir = config.nextcloud_mount_path / "Users" / "alice" / "memories"
        memories_dir.mkdir(parents=True)
        today = datetime.now().strftime("%Y-%m-%d")
        (memories_dir / f"{today}.md").write_text("- User prefers dark mode")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        prompt_text = mock_run.call_args.kwargs["input"]
        assert "User prefers dark mode" in prompt_text
        assert "Recent context (from previous days)" in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_dated_memories_skipped_for_briefing(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, auto_load_days=3)
        # Add briefing skill with exclude_memory so flag-based check works
        briefing_dir = config.skills_dir / "briefing"
        briefing_dir.mkdir(parents=True)
        (briefing_dir / "skill.toml").write_text(
            'description = "Briefing"\nsource_types = ["briefing"]\nexclude_memory = true\n'
        )
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        from datetime import datetime
        memories_dir = config.nextcloud_mount_path / "Users" / "alice" / "memories"
        memories_dir.mkdir(parents=True)
        today = datetime.now().strftime("%Y-%m-%d")
        (memories_dir / f"{today}.md").write_text("- Should not appear")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="briefing")
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        prompt_text = mock_run.call_args.kwargs["input"]
        assert "Should not appear" not in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_dated_memories_none_when_zero_days(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, auto_load_days=0)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        from datetime import datetime
        memories_dir = config.nextcloud_mount_path / "Users" / "alice" / "memories"
        memories_dir.mkdir(parents=True)
        today = datetime.now().strftime("%Y-%m-%d")
        (memories_dir / f"{today}.md").write_text("- Should not appear")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        prompt_text = mock_run.call_args.kwargs["input"]
        assert "Recent context (from previous days)" not in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_dated_memories_none_when_sleep_disabled(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, auto_load_days=3, sleep_enabled=False)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        prompt_text = mock_run.call_args.kwargs["input"]
        assert "Recent context (from previous days)" not in prompt_text


# =============================================================================
# TestConfirmationContext
# =============================================================================


class TestConfirmationContext:
    def _make_task(self, **kwargs):
        defaults = dict(
            id=1, status="running", source_type="email",
            user_id="stefan", prompt="Emissary reply from bob@ext.com",
            conversation_token="room1",
        )
        defaults.update(kwargs)
        return db.Task(**defaults)

    def _make_config(self, tmp_path):
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        return Config(
            db_path=tmp_path / "test.db",
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
        )

    def test_confirmation_context_included_in_prompt(self, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()
        previous_output = "I drafted a reply: 'How about Tuesday at 3pm?' Should I send this?"

        prompt = build_prompt(
            task, [], config,
            confirmation_context=previous_output,
        )

        assert "## Confirmed action" in prompt
        assert "How about Tuesday at 3pm?" in prompt
        assert "Do not re-draft" in prompt
        assert "`istota-skill email send`" in prompt

    def test_no_confirmation_context_when_none(self, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()

        prompt = build_prompt(task, [], config, confirmation_context=None)

        assert "## Confirmed action" not in prompt

    def test_confirmation_context_appears_before_user_request(self, tmp_path):
        config = self._make_config(tmp_path)
        task = self._make_task()

        prompt = build_prompt(
            task, [], config,
            confirmation_context="Previous draft here",
        )

        confirmed_pos = prompt.index("## Confirmed action")
        request_pos = prompt.index("## User's request")
        assert confirmed_pos < request_pos


# ---------------------------------------------------------------------------
# TestDetectMalformedResult
# ---------------------------------------------------------------------------


class TestDetectMalformedResult:
    """Test detection of malformed model output (leaked XML, disproportionately short)."""

    def test_normal_text_passes(self):
        assert detect_malformed_result("Here are three painting studios in Warsaw...") is None

    def test_short_normal_text_passes(self):
        assert detect_malformed_result("Done.") is None

    def test_empty_string_passes(self):
        assert detect_malformed_result("") is None

    def test_none_passes(self):
        assert detect_malformed_result(None) is None

    def test_whitespace_only_passes(self):
        assert detect_malformed_result("   \n  ") is None

    def test_xml_parameter_close_detected(self):
        result = detect_malformed_result("</parameter>\n</invoke>")
        assert result is not None
        assert "leaked tool-call XML" in result

    def test_xml_invoke_close_detected(self):
        result = detect_malformed_result("</invoke>")
        assert result is not None
        assert "leaked tool-call XML" in result

    def test_xml_invoke_open_detected(self):
        result = detect_malformed_result("<invoke name='foo'>")
        assert result is not None
        assert "leaked tool-call XML" in result

    def test_antml_prefix_detected(self):
        result = detect_malformed_result("</thinking>")
        assert result is not None
        assert "leaked tool-call XML" in result

    def test_parameter_open_detected(self):
        result = detect_malformed_result("<parameter name='path'>")
        assert result is not None
        assert "leaked tool-call XML" in result

    def test_xml_in_long_response_passes(self):
        """XML patterns embedded in a substantive response should not trigger detection."""
        text = (
            "The model produced an error with </parameter> tags. "
            "This is a known issue when context pressure causes the model to emit "
            "raw XML fragments instead of coherent responses. Here is the analysis..."
        )
        assert detect_malformed_result(text) is None

    # --- Strict mode (output_target="talk") ---

    def test_talk_xml_in_prose_detected(self):
        """XML patterns embedded in prose should be caught in strict Talk mode."""
        text = (
            "The model produced an error with </parameter> tags. "
            "This is a known issue when context pressure causes problems."
        )
        # Non-strict: passes (enough non-syntax content)
        assert detect_malformed_result(text) is None
        # Strict (Talk): flagged
        result = detect_malformed_result(text, output_target="talk")
        assert result is not None
        assert "Talk output" in result

    def test_talk_xml_in_code_fence_passes(self):
        """XML patterns inside code fences should not trigger in strict mode."""
        text = (
            "Here's an example of the XML format:\n\n"
            "```xml\n<parameter name='path'>/foo</parameter>\n```\n\n"
            "This shows the structure."
        )
        assert detect_malformed_result(text, output_target="talk") is None

    def test_talk_clean_markdown_passes(self):
        """Normal markdown should not trigger strict mode."""
        text = "## Results\n\n- Item one\n- Item two\n\nHere's a **bold** conclusion."
        assert detect_malformed_result(text, output_target="talk") is None

    def test_talk_xml_outside_fence_with_fenced_xml_detected(self):
        """XML outside code fences should be caught even if fenced XML exists."""
        text = (
            "```xml\n<parameter>ok</parameter>\n```\n\n"
            "And then </invoke> happened."
        )
        result = detect_malformed_result(text, output_target="talk")
        assert result is not None

    def test_both_target_uses_strict_mode(self):
        text = "Something </invoke> happened"
        assert detect_malformed_result(text, output_target="both") is not None

    def test_all_target_uses_strict_mode(self):
        text = "Something </invoke> happened"
        assert detect_malformed_result(text, output_target="all") is not None

    def test_email_target_uses_lenient_mode(self):
        """Email target should use lenient mode (XML patterns allowed in longer text)."""
        text = (
            "The model produced an error with </parameter> tags. "
            "This is a known issue when context pressure causes problems."
        )
        assert detect_malformed_result(text, output_target="email") is None


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
        result = _compose_full_result(real_summary, trace)
        assert result == real_summary

    def test_empty_trace_entries_ignored(self):
        trace = [
            {"type": "text", "text": ""},
            {"type": "text", "text": "   "},
        ]
        assert _compose_full_result("Done.", trace) == "Done."

    def test_substantial_no_tools_no_recovery(self):
        """A substantial result with no tool boundary in trace: still no
        override — gate is on terseness, not trace shape."""
        block = _block("Findings.", 800)
        trace = [{"type": "text", "text": block}]
        long_result = _block("Result.", 400)
        result = _compose_full_result(long_result, trace)
        assert result == long_result

    # --- terse-pattern recovery ---

    def test_see_above_with_substantial_pre_tool_region(self):
        """Canonical ISSUE-025 shape: substantial text → tool → terse result."""
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result("See above.", trace, task=_make_task())
        assert result == findings

    def test_terse_short_result_with_substantial_trailing_region(self):
        """Result < 150 chars but not a known reference — still triggers."""
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result(
            "Operation completed.", trace, task=_make_task(),
        )
        assert result == findings

    def test_done_with_substantial_pre_tool_region(self):
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        assert _compose_full_result("Done.", trace, task=_make_task()) == findings

    def test_empty_result_with_substantial_trailing_region(self):
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        assert _compose_full_result("", trace, task=_make_task()) == findings

    # --- terse but no qualifying region ---

    def test_terse_result_short_trailing_region_no_override(self):
        """Trailing region must be ≥ TRAILING_REGION_MIN_CHARS to override."""
        short_block = "Brief note about the result. " * 5  # ~145 chars
        trace = [
            {"type": "text", "text": short_block},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result("See above.", trace, task=_make_task())
        # Region < 500 chars → no override
        assert result == "See above."

    def test_terse_result_region_already_in_result(self):
        """If the trailing region appears verbatim in result_text, no override."""
        block = _block("Findings.", 800)
        trace = [{"type": "text", "text": block}]
        # Result already contains the region (followed by a tag) — no override
        embedded = block + "\n\n[done]"
        result = _compose_full_result(embedded, trace, task=_make_task())
        assert result == embedded

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

    def test_scheduled_task_no_terse_recovery(self):
        """Mechanism B is gated for scheduled tasks regardless of trace."""
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result(
            "See above.", trace, task=_make_task(source_type="scheduled"),
        )
        assert result == "See above."

    def test_briefing_task_no_terse_recovery(self):
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result(
            "See above.", trace, task=_make_task(source_type="briefing"),
        )
        assert result == "See above."

    def test_heartbeat_silent_blocks_terse_recovery(self):
        """heartbeat_silent flag gates Mechanism B even when source_type
        isn't in the explicit set."""
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result(
            "See above.", trace,
            task=_make_task(source_type="cli", heartbeat_silent=True),
        )
        assert result == "See above."

    def test_scheduled_job_id_blocks_terse_recovery(self):
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result(
            "See above.", trace,
            task=_make_task(source_type="cli", scheduled_job_id=42),
        )
        assert result == "See above."

    def test_no_task_means_no_automated_gate(self):
        """Backwards-compat: callers passing no task get the original gating
        behavior (no automated-task gate fires)."""
        findings = _block("Findings.", 800)
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result("See above.", trace)
        assert result == findings

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
            {"type": "tool", "text": "Read file"},
            {"type": "text", "text": block2},
        ]
        # Tool is NOT a CM-mode delimiter — both text blocks belong to the
        # post-CM segment and are joined.
        result = _compose_full_result("Doubled.", trace)
        assert result == f"{block1}\n\n{block2}"

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


class TestComposeHelpers:
    """Direct tests for the helper predicates."""

    def test_is_terse_short(self):
        assert _is_terse("Done.")

    def test_is_terse_empty(self):
        assert _is_terse("")
        assert _is_terse("   ")

    def test_is_terse_pattern_see_above(self):
        assert _is_terse("See above.")
        assert _is_terse("see above")
        assert _is_terse("SEE ABOVE")

    def test_is_terse_pattern_done(self):
        assert _is_terse("Done.")
        assert _is_terse("Done")
        assert _is_terse("OK")
        assert _is_terse("✓")

    def test_is_terse_substantial_text_not_terse(self):
        long_text = "A" * (_TERSE_RESULT_MAX_CHARS + 1)
        assert not _is_terse(long_text)

    def test_is_automated_task_none(self):
        assert not _is_automated_task(None)

    def test_is_automated_task_scheduled(self):
        assert _is_automated_task(_make_task(source_type="scheduled"))

    def test_is_automated_task_briefing(self):
        assert _is_automated_task(_make_task(source_type="briefing"))

    def test_is_automated_task_talk_not_automated(self):
        assert not _is_automated_task(_make_task(source_type="talk"))

    def test_is_automated_task_email_not_automated(self):
        assert not _is_automated_task(_make_task(source_type="email"))

    def test_is_automated_task_subtask_not_automated(self):
        assert not _is_automated_task(_make_task(source_type="subtask"))

    def test_is_automated_task_heartbeat_silent_flag(self):
        assert _is_automated_task(
            _make_task(source_type="cli", heartbeat_silent=True),
        )

    def test_is_automated_task_scheduled_job_id_flag(self):
        assert _is_automated_task(
            _make_task(source_type="cli", scheduled_job_id=42),
        )

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

    def _make_task(self, user_id="stefan"):
        return db.Task(
            id=1, status="running", prompt="hello", user_id=user_id,
            source_type="talk", conversation_token="room1",
        )

    def test_per_user_email_shown_when_email_enabled(self):
        config = Config()
        config.email = AppEmailConfig(
            enabled=True,
            imap_host="imap.test", imap_port=993,
            imap_user="u", imap_password="p",
            bot_email="zorg@x.cynium.com",
        )
        task = self._make_task(user_id="stefan")
        result = build_prompt(task, [], config)
        assert "zorg+stefan@x.cynium.com" in result

    def test_per_user_email_not_shown_when_email_disabled(self):
        config = Config()
        config.email = AppEmailConfig(enabled=False)
        task = self._make_task(user_id="stefan")
        result = build_prompt(task, [], config)
        assert "+stefan@" not in result

    def test_per_user_email_not_shown_when_no_bot_email(self):
        config = Config()
        config.email = AppEmailConfig(
            enabled=True,
            imap_host="imap.test", imap_port=993,
            imap_user="u", imap_password="p",
            bot_email="",
        )
        task = self._make_task(user_id="stefan")
        result = build_prompt(task, [], config)
        assert "+stefan@" not in result


# =============================================================================
# TestSmtpFromPlusAddress
# =============================================================================


class TestSmtpFrom:
    """Verify SMTP_FROM uses plain bot email (not plus-addressed)."""

    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            # Real bundled skills dir so the email manifest is loaded.
            bundled_skills_dir=None,
            temp_dir=tmp_path / "temp",
            email=AppEmailConfig(
                enabled=True,
                imap_host="imap.test", imap_port=993,
                imap_user="u", imap_password="p",
                smtp_host="smtp.test", smtp_port=587,
                bot_email="zorg@x.cynium.com",
            ),
            security=SecurityConfig(skill_proxy_enabled=False),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="stefan", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_smtp_from_uses_plain_bot_email(self, mock_run, tmp_path):
        """SMTP_FROM should be the plain bot email; plus-addressing is for inbound only."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "stefan").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        call_args = mock_run.call_args
        env = call_args[1]["env"]
        assert env["SMTP_FROM"] == "zorg@x.cynium.com"
