"""Configuration loading for istota.executor module."""

import json
from unittest.mock import patch, MagicMock

import pytest

from istota.executor import (
    _compose_full_result,
    _text_similarity,
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
    API_RETRY_MAX_ATTEMPTS,
    API_RETRY_DELAY_SECONDS,
    TRANSIENT_STATUS_CODES,
    _execute_streaming,
)
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
    def _make_config(self, tmp_path):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        return Config(
            db_path=db_path,
            scheduler=SchedulerConfig(task_timeout_minutes=1),
            temp_dir=tmp_path / "temp",
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="testuser", source_type="cli")
        return db.get_task(conn, task_id)

    @patch("istota.executor._execute_streaming_once")
    @patch("istota.executor.time.sleep")
    def test_retries_on_transient_error(self, mock_sleep, mock_exec_once, tmp_path):
        """Should retry on transient 500 errors before giving up."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp").mkdir(exist_ok=True)
        result_file = tmp_path / "result.txt"

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)

        # First call fails with 500, second succeeds
        error_500 = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"},"request_id":"req_123"}'
        mock_exec_once.side_effect = [
            (False, error_500, None, None),
            (True, "Success after retry", None, None),
        ]

        success, result, actions, trace = _execute_streaming(
            ["claude", "-p", "test"],
            {},
            config,
            task,
            None,
            result_file,
        )

        assert success is True
        assert result == "Success after retry"
        assert mock_exec_once.call_count == 2
        mock_sleep.assert_called_once_with(API_RETRY_DELAY_SECONDS)

    @patch("istota.executor._execute_streaming_once")
    @patch("istota.executor.time.sleep")
    def test_no_retry_on_permanent_error(self, mock_sleep, mock_exec_once, tmp_path):
        """Should not retry on permanent 401 errors."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp").mkdir(exist_ok=True)
        result_file = tmp_path / "result.txt"

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)

        error_401 = 'API Error: 401 {"type":"error","error":{"type":"authentication_error","message":"Invalid API key"}}'
        mock_exec_once.return_value = (False, error_401, None, None)

        success, result, actions, trace = _execute_streaming(
            ["claude", "-p", "test"],
            {},
            config,
            task,
            None,
            result_file,
        )

        assert success is False
        assert "401" in result
        assert mock_exec_once.call_count == 1
        mock_sleep.assert_not_called()

    @patch("istota.executor._execute_streaming_once")
    @patch("istota.executor.time.sleep")
    def test_no_retry_on_non_api_error(self, mock_sleep, mock_exec_once, tmp_path):
        """Should not retry on non-API errors like OOM."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp").mkdir(exist_ok=True)
        result_file = tmp_path / "result.txt"

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)

        mock_exec_once.return_value = (False, "Claude Code was killed (likely out of memory)", None, None)

        success, result, actions, trace = _execute_streaming(
            ["claude", "-p", "test"],
            {},
            config,
            task,
            None,
            result_file,
        )

        assert success is False
        assert "out of memory" in result
        assert mock_exec_once.call_count == 1
        mock_sleep.assert_not_called()

    @patch("istota.executor._execute_streaming_once")
    @patch("istota.executor.time.sleep")
    def test_gives_up_after_max_retries(self, mock_sleep, mock_exec_once, tmp_path):
        """Should give up after max retry attempts."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp").mkdir(exist_ok=True)
        result_file = tmp_path / "result.txt"

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)

        error_500 = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"Internal server error"}}'
        mock_exec_once.return_value = (False, error_500, None, None)

        success, result, actions, trace = _execute_streaming(
            ["claude", "-p", "test"],
            {},
            config,
            task,
            None,
            result_file,
        )

        assert success is False
        assert "500" in result
        assert mock_exec_once.call_count == API_RETRY_MAX_ATTEMPTS
        assert mock_sleep.call_count == API_RETRY_MAX_ATTEMPTS - 1

    @patch("istota.executor._execute_streaming_once")
    def test_success_on_first_try_no_retry(self, mock_exec_once, tmp_path):
        """Should not retry if first attempt succeeds."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp").mkdir(exist_ok=True)
        result_file = tmp_path / "result.txt"

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)

        mock_exec_once.return_value = (True, "Immediate success", None, None)

        success, result, actions, trace = _execute_streaming(
            ["claude", "-p", "test"],
            {},
            config,
            task,
            None,
            result_file,
        )

        assert success is True
        assert result == "Immediate success"
        assert mock_exec_once.call_count == 1

    @patch("istota.executor._execute_streaming_once")
    def test_actions_taken_passed_through(self, mock_exec_once, tmp_path):
        """Should pass through actions_taken from _execute_streaming_once."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp").mkdir(exist_ok=True)
        result_file = tmp_path / "result.txt"

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)

        actions = '["📄 Reading file.py", "✏️ Editing file.py"]'
        mock_exec_once.return_value = (True, "Done", actions, '[]')

        success, result, actions_taken, trace = _execute_streaming(
            ["claude", "-p", "test"],
            {},
            config,
            task,
            None,
            result_file,
        )

        assert success is True
        assert result == "Done"
        assert actions_taken == actions

    @patch("istota.executor._execute_streaming_once")
    @patch("istota.executor.time.sleep")
    def test_actions_taken_from_successful_retry(self, mock_sleep, mock_exec_once, tmp_path):
        """On retry, should use actions_taken from the successful attempt."""
        config = self._make_config(tmp_path)
        (tmp_path / "temp").mkdir(exist_ok=True)
        result_file = tmp_path / "result.txt"

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)

        error_500 = 'API Error: 500 {"type":"error","error":{"type":"api_error","message":"err"},"request_id":"req_1"}'
        actions = '["📄 Reading config"]'
        mock_exec_once.side_effect = [
            (False, error_500, None, None),
            (True, "ok", actions, None),
        ]

        success, result, actions_taken, trace = _execute_streaming(
            ["claude", "-p", "test"], {}, config, task, None, result_file,
        )

        assert success is True
        assert actions_taken == actions


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
            bundled_skills_dir=tmp_path / "_empty_bundled",
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
        # Default allowlisted endpoints are present
        assert "GET /api/v4/projects/" in api_content
        assert "POST /api/v4/projects/" in api_content
        assert "merge_requests" in api_content
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
            bundled_skills_dir=tmp_path / "_empty_bundled",
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
        # URL and owner are still set (for clone URLs, etc.)
        assert env["GITHUB_URL"] == "https://github.com"

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
            bundled_skills_dir=tmp_path / "_empty_bundled",
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
    def _make_config(self, tmp_path, karakeep_resources=None):
        db_path = tmp_path / "test.db"
        db.init_db(db_path)
        skills_dir = tmp_path / "config" / "skills"
        skills_dir.mkdir(parents=True)
        (skills_dir / "_index.toml").write_text('[files]\ndescription = "File ops"\nalways_include = true\n')
        (skills_dir / "files.md").write_text("File operations guide.")
        mount_path = tmp_path / "mount"
        mount_path.mkdir(parents=True)
        resources = karakeep_resources or []
        users = {"alice": UserConfig(resources=resources)}
        return Config(
            db_path=db_path,
            skills_dir=skills_dir,
            bundled_skills_dir=tmp_path / "_empty_bundled",
            temp_dir=tmp_path / "temp",
            nextcloud_mount_path=mount_path,
            users=users,
            security=SecurityConfig(skill_proxy_enabled=False),
        )

    def _make_task(self, conn):
        task_id = db.create_task(conn, prompt="test", user_id="alice", source_type="talk")
        return db.get_task(conn, task_id)

    @patch("istota.executor.subprocess.run")
    def test_karakeep_env_vars_set_when_configured(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, karakeep_resources=[
            ResourceConfig(
                type="karakeep", name="Bookmarks",
                base_url="https://keep.example.com/api/v1",
                api_key="kk-secret",
            ),
        ])
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["KARAKEEP_BASE_URL"] == "https://keep.example.com/api/v1"
        assert env["KARAKEEP_API_KEY"] == "kk-secret"

    @patch("istota.executor.subprocess.run")
    def test_karakeep_env_vars_not_set_when_no_resource(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, karakeep_resources=[])
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
    def test_karakeep_env_vars_not_set_when_credentials_empty(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, karakeep_resources=[
            ResourceConfig(type="karakeep", name="Bookmarks", base_url="", api_key=""),
        ])
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
    def test_karakeep_uses_first_resource_when_multiple(self, mock_run, tmp_path):
        config = self._make_config(tmp_path, karakeep_resources=[
            ResourceConfig(
                type="karakeep", name="Primary",
                base_url="https://primary.example.com/api/v1",
                api_key="primary-key",
            ),
            ResourceConfig(
                type="karakeep", name="Secondary",
                base_url="https://secondary.example.com/api/v1",
                api_key="secondary-key",
            ),
        ])
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
            from istota.executor import execute_task
            execute_task(task, config, [], conn=conn)

        env = mock_run.call_args[1]["env"]
        assert env["KARAKEEP_BASE_URL"] == "https://primary.example.com/api/v1"
        assert env["KARAKEEP_API_KEY"] == "primary-key"


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
            bundled_skills_dir=tmp_path / "_empty_bundled",
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
                mock_talk_ctx.return_value = None  # Fall through to DB context
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

    @patch("istota.memory_search.search")
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

    @patch("istota.memory_search.search")
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

    @patch("istota.memory_search.search")
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


class TestComposeFullResult:
    """Test recovery of substantial text blocks from execution trace."""

    def _substantial_text(self):
        return ("Here are the top findings from the search. " * 10).strip()  # ~450 chars

    def test_no_trace_returns_result_as_is(self):
        assert _compose_full_result("Done.", []) == "Done."

    def test_no_substantial_blocks_returns_result(self):
        trace = [
            {"type": "text", "text": "Let me check."},
            {"type": "tool", "text": "Read file.py"},
            {"type": "text", "text": "Running the search."},
        ]
        assert _compose_full_result("Done.", trace) == "Done."

    def test_substantial_block_prepended_to_result(self):
        findings = self._substantial_text()
        trace = [
            {"type": "text", "text": "Let me search."},
            {"type": "tool", "text": "Browse OLX"},
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write tracking file"},
        ]
        result = _compose_full_result("All results posted above.", trace)
        assert findings in result
        assert result.startswith(findings)

    def test_result_already_contains_substantial_content(self):
        findings = self._substantial_text()
        trace = [
            {"type": "tool", "text": "Read file"},
            {"type": "text", "text": findings},
        ]
        # ResultEvent.text IS the substantial content
        result = _compose_full_result(findings, trace)
        assert result == findings  # no duplication

    def test_multiple_substantial_blocks_all_included(self):
        block1 = (
            "The calendar shows three meetings today: a standup at 9am, "
            "a design review at 11am, and a planning session at 2pm. "
            "All meetings are in the main conference room on the second floor. "
            "The standup has seven attendees and typically runs 15 minutes."
        )
        block2 = (
            "Market summary: the S&P 500 gained 1.2% on strong earnings reports "
            "from tech companies. European markets closed mixed, with the DAX up "
            "0.3% while the FTSE declined 0.1% on weaker consumer data. "
            "Asian markets were mostly higher overnight with the Nikkei up 0.8%."
        )
        trace = [
            {"type": "text", "text": block1},
            {"type": "tool", "text": "More browsing"},
            {"type": "text", "text": block2},
        ]
        result = _compose_full_result("See above.", trace)
        assert block1 in result
        assert block2 in result

    def test_terse_result_dropped_when_dwarfed_by_substantial(self):
        findings = self._substantial_text()
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result("See above.", trace)
        # The terse closing should be dropped since it's just a reference
        assert result == findings

    def test_substantial_result_not_augmented_with_comparable_blocks(self):
        """When the result is comparable length to trace blocks, no recovery."""
        block = self._substantial_text()  # ~450 chars
        long_result = ("Here is a detailed conclusion. " * 10).strip()  # ~300 chars
        trace = [
            {"type": "text", "text": block},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result(long_result, trace)
        # Block is only ~1.5x the result — not recovered
        assert result == long_result

    def test_short_result_with_slightly_longer_block_no_recovery(self):
        """When trace block is only slightly longer than result, no recovery."""
        block = "A" * 250
        short_result = "B" * 200
        trace = [
            {"type": "text", "text": block},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result(short_result, trace)
        # 250 <= 200 * 2 = 400, so no recovery
        assert result == short_result

    def test_empty_trace_entries_ignored(self):
        trace = [
            {"type": "text", "text": ""},
            {"type": "text", "text": "   "},
        ]
        assert _compose_full_result("Done.", trace) == "Done."

    def test_near_duplicate_blocks_deduplicated(self):
        """When the model restates itself with minor edits, only keep one copy."""
        block_v1 = (
            "That's the only feedback memory I have saved. One file: "
            "feedback_no_parallel_mutations.md — Never run mutation commands "
            "in parallel subagents. Learned the hard way during yesterday's "
            "moneyman pipeline test when I fired two subagents for every step, "
            "doubled all the writes, and then blamed moneyman for the chaos."
        )
        block_v2 = (
            "That's the only feedback memory I have. One file: "
            "feedback_no_parallel_mutations.md — Never run mutation commands "
            "in parallel subagents. Learned the hard way during the moneyman "
            "pipeline test when I fired two subagents at every step, "
            "duplicated entries, and then blamed moneyman for bugs that were "
            "entirely self-inflicted."
        )
        trace = [
            {"type": "text", "text": block_v1},
            {"type": "tool", "text": "Read memory file"},
            {"type": "text", "text": block_v2},
        ]
        result = _compose_full_result("See above.", trace)
        # Should contain only one version, not both
        assert block_v1 in result
        assert block_v2 not in result

    def test_near_duplicate_of_result_text_dropped(self):
        """A trace block that's a near-duplicate of result_text is skipped."""
        result_text = (
            "Here are the findings from the analysis. The data shows a clear "
            "trend toward increased usage over the past quarter. " * 5
        ).strip()
        # Slightly different version in trace
        trace_block = (
            "Here are the findings from my analysis. The data shows a clear "
            "trend towards increased usage over the last quarter. " * 5
        ).strip()
        trace = [
            {"type": "text", "text": trace_block},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result(result_text, trace)
        assert result == result_text

    def test_distinct_blocks_not_deduplicated(self):
        """Blocks with genuinely different content are both kept."""
        block1 = (
            "The calendar shows three meetings today: a standup at 9am, "
            "a design review at 11am, and a planning session at 2pm. "
            "All meetings are in the main conference room on the second floor. "
            "The standup has seven attendees and typically runs 15 minutes."
        )
        block2 = (
            "Market summary: the S&P 500 gained 1.2% on strong earnings reports "
            "from tech companies. European markets closed mixed, with the DAX up "
            "0.3% while the FTSE declined 0.1% on weaker consumer data. "
            "Asian markets were mostly higher overnight with the Nikkei up 0.8%."
        )
        trace = [
            {"type": "text", "text": block1},
            {"type": "tool", "text": "Check markets"},
            {"type": "text", "text": block2},
        ]
        result = _compose_full_result("Done.", trace)
        assert block1 in result
        assert block2 in result


class TestComposeFullResultCM:
    """Test CM-aware result composition (ISSUE-026)."""

    def _substantial_text(self, variant="a"):
        if variant == "a":
            return ("Here are the top findings from the search. " * 10).strip()
        return ("The market data shows a clear upward trend. " * 10).strip()

    def test_cm_boundary_uses_last_substantial_segment(self):
        """When CM fires between two substantial text blocks, use the last one."""
        pre_cm = self._substantial_text("a")
        post_cm = self._substantial_text("b")
        trace = [
            {"type": "text", "text": pre_cm},
            {"type": "cm_boundary"},
            {"type": "text", "text": post_cm},
        ]
        # result_text would contain both (as Claude Code concatenates them)
        doubled_result = f"{pre_cm}\n\n{post_cm}"
        result = _compose_full_result(doubled_result, trace)
        assert result == post_cm

    def test_cm_boundary_with_thin_last_segment_trusts_result(self):
        """When last segment is thin progress text, trust result_text."""
        trace = [
            {"type": "text", "text": "Let me check."},
            {"type": "cm_boundary"},
            {"type": "tool", "text": "Read file"},
            {"type": "cm_boundary"},
            {"type": "text", "text": "Now let me write the patch."},
        ]
        good_result = self._substantial_text("a")
        result = _compose_full_result(good_result, trace)
        assert result == good_result

    def test_cm_boundary_with_tools_after_last_cm(self):
        """When last segment has only tools (no text), trust result_text."""
        real_response = self._substantial_text("a")
        trace = [
            {"type": "text", "text": real_response},
            {"type": "cm_boundary"},
            {"type": "tool", "text": "Write file"},
            {"type": "tool", "text": "Edit config"},
        ]
        result = _compose_full_result(real_response, trace)
        assert result == real_response

    def test_cm_boundary_empty_last_segment_trusts_result(self):
        """When CM is the very last entry, last segment is empty."""
        real_response = self._substantial_text("a")
        trace = [
            {"type": "text", "text": real_response},
            {"type": "cm_boundary"},
        ]
        result = _compose_full_result(real_response, trace)
        assert result == real_response

    def test_no_cm_boundary_falls_through_to_existing_logic(self):
        """Without CM boundaries, existing terse-result recovery applies."""
        findings = self._substantial_text("a")
        trace = [
            {"type": "text", "text": findings},
            {"type": "tool", "text": "Write file"},
        ]
        result = _compose_full_result("See above.", trace)
        assert findings in result

    def test_multiple_cm_boundaries_uses_last_substantial(self):
        """Multiple CM firings: use last segment with substantial text."""
        block1 = self._substantial_text("a")
        block2 = self._substantial_text("b")
        trace = [
            {"type": "text", "text": block1},
            {"type": "cm_boundary"},
            {"type": "text", "text": "Let me rethink."},
            {"type": "cm_boundary"},
            {"type": "text", "text": block2},
            {"type": "cm_boundary"},
        ]
        doubled = f"{block1}\n\n{block2}"
        result = _compose_full_result(doubled, trace)
        # Last substantial segment is before the final CM boundary
        assert result == block2

    def test_cm_with_multiple_texts_in_last_segment(self):
        """Last segment has multiple text blocks — join them all."""
        block1 = self._substantial_text("a")
        block2 = self._substantial_text("b")
        trace = [
            {"type": "text", "text": "Old analysis."},
            {"type": "cm_boundary"},
            {"type": "text", "text": block1},
            {"type": "tool", "text": "Read file"},
            {"type": "text", "text": block2},
        ]
        result = _compose_full_result("Doubled.", trace)
        assert block1 in result
        assert block2 in result
        assert result == f"{block1}\n\n{block2}"

    def test_cm_real_pattern_pre_and_post_cm_responses(self):
        """Real pattern: model gives answer, CM fires, model restates."""
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
        result = _compose_full_result(doubled_result, trace)
        assert result == post_cm
        assert pre_cm not in result


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
            bundled_skills_dir=tmp_path / "_empty_bundled",
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
