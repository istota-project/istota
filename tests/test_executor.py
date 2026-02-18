"""Configuration loading for istota.executor module."""

import json
from unittest.mock import patch, MagicMock

import pytest

from istota.executor import (
    parse_api_error,
    is_transient_api_error,
    build_prompt,
    load_persona,
    _pre_transcribe_attachments,
    _AUDIO_EXTENSIONS,
    API_RETRY_MAX_ATTEMPTS,
    API_RETRY_DELAY_SECONDS,
    TRANSIENT_STATUS_CODES,
    _execute_streaming,
)
from pathlib import Path

from istota.config import Config, DeveloperConfig, ResourceConfig, SchedulerConfig, SiteConfig, UserConfig
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
            (False, error_500, None),
            (True, "Success after retry", None),
        ]

        success, result, actions = _execute_streaming(
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
        mock_exec_once.return_value = (False, error_401, None)

        success, result, actions = _execute_streaming(
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

        mock_exec_once.return_value = (False, "Claude Code was killed (likely out of memory)", None)

        success, result, actions = _execute_streaming(
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
        mock_exec_once.return_value = (False, error_500, None)

        success, result, actions = _execute_streaming(
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

        mock_exec_once.return_value = (True, "Immediate success", None)

        success, result, actions = _execute_streaming(
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
        mock_exec_once.return_value = (True, "Done", actions)

        success, result, actions_taken = _execute_streaming(
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
            (False, error_500, None),
            (True, "ok", actions),
        ]

        success, result, actions_taken = _execute_streaming(
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
            success, result, _actions = execute_task(task, config, [], conn=conn)

        # Verify changelog was in the prompt
        call_args = mock_run.call_args
        prompt_text = call_args[0][0][2]  # cmd[2] is the prompt after "-p"
        assert "What's New in Skills" in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_changelog_not_included_when_fingerprint_matches(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        (config.skills_dir / "CHANGELOG.md").write_text("## v1\n- New feature")
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        # Pre-store the current fingerprint
        from istota.skills_loader import compute_skills_fingerprint
        fp = compute_skills_fingerprint(config.skills_dir, bundled_dir=config.bundled_skills_dir)

        with db.get_db(config.db_path) as conn:
            db.set_user_skills_fingerprint(conn, "alice", fp)
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            success, result, _actions = execute_task(task, config, [], conn=conn)

        call_args = mock_run.call_args
        prompt_text = call_args[0][0][2]
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
            success, result, _actions = execute_task(task, config, [], conn=conn)

        call_args = mock_run.call_args
        prompt_text = call_args[0][0][2]
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
            success, result, _actions = execute_task(task, config, [], conn=conn)

        call_args = mock_run.call_args
        prompt_text = call_args[0][0][2]
        assert "What's New in Skills" not in prompt_text

    @patch("istota.executor.subprocess.run")
    def test_fingerprint_updated_after_success(self, mock_run, tmp_path):
        config = self._make_config(tmp_path)
        (tmp_path / "temp" / "alice").mkdir(parents=True)
        mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")

        from istota.skills_loader import compute_skills_fingerprint
        expected_fp = compute_skills_fingerprint(config.skills_dir, bundled_dir=config.bundled_skills_dir)

        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn, source_type="talk")
            from istota.executor import execute_task
            success, result, _actions = execute_task(task, config, [], conn=conn)
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
            success, result, _actions = execute_task(task, config, [], conn=conn)
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
            success, result, _actions = execute_task(task, config, [], conn=conn)
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

    def test_admin_prompt_has_subtask_rule(self, tmp_path):
        config = self._make_config(tmp_path)
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task = self._make_task(conn)
        prompt = build_prompt(task, [], config, is_admin=True)
        assert "create subtasks" in prompt.lower()
        assert "ISTOTA_DEFERRED_DIR" in prompt

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

        prompt_text = mock_run.call_args[0][0][2]
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

        prompt_text = mock_run.call_args[0][0][2]
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
