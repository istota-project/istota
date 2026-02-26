"""Tests for executor streaming (Popen + stream-json parsing) and simple execution."""

import json
import os
import subprocess
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

from contextlib import ExitStack

from istota.config import Config, SchedulerConfig, SleepCycleConfig, UserConfig
from istota.executor import execute_task, get_user_temp_dir
from istota import db


def _make_task(**kwargs):
    """Create a minimal Task dataclass for testing."""
    defaults = dict(
        id=1,
        prompt="test prompt",
        user_id="testuser",
        source_type="cli",
        status="running",
    )
    defaults.update(kwargs)
    return db.Task(**defaults)


def _make_config(tmp_path: Path) -> Config:
    config = Config()
    config.temp_dir = tmp_path / "temp"
    config.temp_dir.mkdir()
    config.db_path = tmp_path / "test.db"
    config.skills_dir = tmp_path / "skills"
    config.skills_dir.mkdir()
    # Write empty _index.toml
    (config.skills_dir / "_index.toml").write_text("")
    return config


# Common patches for all executor tests
_EXECUTOR_PATCHES = [
    "istota.executor.select_relevant_context",
    "istota.executor.read_user_memory_v2",
    "istota.executor.ensure_user_directories_v2",
    "istota.executor.read_channel_memory",
    "istota.executor.ensure_channel_directories",
    "istota.executor.get_caldav_client",
    "istota.executor.get_calendars_for_user",
    "istota.skills_loader.load_skill_index",
    "istota.skills_loader.select_skills",
    "istota.skills_loader.load_skills",
]

_EXECUTOR_PATCH_RETURNS = [
    [],     # select_relevant_context
    None,   # read_user_memory_v2
    None,   # ensure_user_directories_v2
    None,   # read_channel_memory
    None,   # ensure_channel_directories
    None,   # get_caldav_client
    None,   # get_calendars_for_user
    {},     # load_skill_index
    [],     # select_skills
    None,   # load_skills
]


def _patch_executor():
    """Return a list of patch context managers for executor dependencies."""
    patches = []
    for name, ret in zip(_EXECUTOR_PATCHES, _EXECUTOR_PATCH_RETURNS):
        patches.append(patch(name, return_value=ret))
    return patches


def contextmanager_chain(patches):
    """Apply a list of patch context managers using ExitStack."""
    stack = ExitStack()
    for p in patches:
        stack.enter_context(p)
    return stack


class TestSimpleExecution:
    """Test the simple (non-streaming) execution path using subprocess.run."""

    def test_successful_execution(self, tmp_path):
        """Simple execution returns stdout on success."""
        config = _make_config(tmp_path)
        task = _make_task()

        mock_result = MagicMock()
        mock_result.stdout = "The answer is 42."
        mock_result.stderr = ""
        mock_result.returncode = 0

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.run", return_value=mock_result),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(task, config, [])

        assert success is True
        assert result == "The answer is 42."

    def test_error_execution(self, tmp_path):
        """Simple execution returns stderr on failure."""
        config = _make_config(tmp_path)
        task = _make_task()

        mock_result = MagicMock()
        mock_result.stdout = "Something went wrong"
        mock_result.stderr = ""
        mock_result.returncode = 1

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.run", return_value=mock_result),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(task, config, [])

        assert success is False
        assert result == "Something went wrong"

    def test_no_output(self, tmp_path):
        """Simple execution returns descriptive error when no output."""
        config = _make_config(tmp_path)
        task = _make_task()

        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_result.returncode = 1

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.run", return_value=mock_result),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(task, config, [])

        assert success is False
        assert "no output" in result.lower()

    def test_stderr_on_failure(self, tmp_path):
        """Simple execution returns stderr when no stdout."""
        config = _make_config(tmp_path)
        task = _make_task()

        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = "Error: API key expired"
        mock_result.returncode = 1

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.run", return_value=mock_result),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(task, config, [])

        assert success is False
        assert "API key expired" in result

    def test_result_file_fallback(self, tmp_path):
        """Simple execution falls back to result file when stdout is empty."""
        config = _make_config(tmp_path)
        task = _make_task()

        # Per-user temp dir: result file goes under user subdirectory
        user_temp = config.temp_dir / task.user_id
        user_temp.mkdir(parents=True, exist_ok=True)
        result_file = user_temp / f"task_{task.id}_result.txt"

        def fake_run(cmd, **kwargs):
            result_file.write_text("Result from file")
            mock = MagicMock()
            mock.stdout = ""
            mock.stderr = ""
            mock.returncode = 0
            return mock

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.run", side_effect=fake_run),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(task, config, [])

        assert success is True
        assert result == "Result from file"

    def test_no_stream_json_flag(self, tmp_path):
        """Simple path does NOT include --output-format stream-json."""
        config = _make_config(tmp_path)
        task = _make_task()

        captured_cmd = []

        def capture_run(cmd, **kwargs):
            captured_cmd.extend(cmd)
            mock = MagicMock()
            mock.stdout = "ok"
            mock.stderr = ""
            mock.returncode = 0
            return mock

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.run", side_effect=capture_run),
        ]
        with contextmanager_chain(patches):
            execute_task(task, config, [])

        assert "--output-format" not in captured_cmd
        # Default permissive mode uses --dangerously-skip-permissions
        assert "--dangerously-skip-permissions" in captured_cmd

    def test_cli_not_found(self, tmp_path):
        """FileNotFoundError from subprocess.run is handled gracefully."""
        config = _make_config(tmp_path)
        task = _make_task()

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.run", side_effect=FileNotFoundError),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(task, config, [])

        assert success is False
        assert "not found" in result.lower()


class TestStreamingExecution:
    """Test the streaming (Popen + stream-json) execution path."""

    def _make_mock_process(self, stdout_lines, stderr_lines=None, returncode=0):
        mock = MagicMock()
        mock.stdout = iter(stdout_lines)
        mock.stderr = iter(stderr_lines or [])
        mock.returncode = returncode
        mock.wait.return_value = returncode
        mock.kill = MagicMock()
        return mock

    def test_successful_result_event(self, tmp_path):
        """Streaming executor extracts result from ResultEvent."""
        config = _make_config(tmp_path)
        task = _make_task()

        stream_lines = [
            json.dumps({"type": "system", "subtype": "init", "cwd": "/tmp"}) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "The answer is 42."}]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success", "result": "The answer is 42.",
            }) + "\n",
        ]

        mock_process = self._make_mock_process(stream_lines)
        progress = []

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(
                task, config, [], on_progress=lambda m: progress.append(m),
            )

        assert success is True
        assert result == "The answer is 42."

    def test_error_result_event(self, tmp_path):
        """Streaming executor reports failure from error ResultEvent."""
        config = _make_config(tmp_path)
        task = _make_task()

        stream_lines = [
            json.dumps({"type": "system", "subtype": "init", "cwd": "/tmp"}) + "\n",
            json.dumps({
                "type": "result", "subtype": "error", "result": "Something went wrong",
            }) + "\n",
        ]

        mock_process = self._make_mock_process(stream_lines, returncode=1)
        progress = []

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(
                task, config, [], on_progress=lambda m: progress.append(m),
            )

        assert success is False
        assert result == "Something went wrong"

    def test_progress_callback_called_for_tool_use(self, tmp_path):
        """on_progress callback is called for ToolUseEvents."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_tool_use = True
        config.scheduler.progress_show_text = False
        task = _make_task()

        stream_lines = [
            json.dumps({"type": "system", "subtype": "init", "cwd": "/tmp"}) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/tmp/data.txt"}},
                ]},
            }) + "\n",
            json.dumps({"type": "user", "message": {"role": "user"}}) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "id": "t2", "name": "Bash",
                     "input": {"command": "wc -l /tmp/data.txt", "description": "Count lines"}},
                ]},
            }) + "\n",
            json.dumps({"type": "user", "message": {"role": "user"}}) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Done."}]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success", "result": "File has 42 lines.",
            }) + "\n",
        ]

        mock_process = self._make_mock_process(stream_lines)
        progress_calls = []

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(
                task, config, [], on_progress=lambda msg: progress_calls.append(msg),
            )

        assert success is True
        assert result == "File has 42 lines."
        assert len(progress_calls) == 2
        assert progress_calls[0] == "📄 Reading data.txt"
        assert progress_calls[1] == "⚙️ Count lines"

    def test_text_progress_when_enabled(self, tmp_path):
        """on_progress is called for TextEvents when progress_show_text=True."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_tool_use = False
        config.scheduler.progress_show_text = True
        task = _make_task()

        stream_lines = [
            json.dumps({"type": "system", "subtype": "init", "cwd": "/tmp"}) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Checking things..."}]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success", "result": "Done.",
            }) + "\n",
        ]

        mock_process = self._make_mock_process(stream_lines)
        progress_calls = []

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(
                task, config, [], on_progress=lambda msg, **kw: progress_calls.append(msg),
            )

        assert success is True
        assert len(progress_calls) == 1
        assert progress_calls[0] == "Checking things..."

    def test_callback_exception_does_not_affect_execution(self, tmp_path):
        """If on_progress raises, task still completes normally."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_tool_use = True
        task = _make_task()

        stream_lines = [
            json.dumps({
                "type": "assistant",
                "message": {"content": [
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/tmp/x.txt"}},
                ]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success", "result": "All good.",
            }) + "\n",
        ]

        mock_process = self._make_mock_process(stream_lines)

        def exploding_callback(msg):
            raise RuntimeError("kaboom")

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(task, config, [], on_progress=exploding_callback)

        assert success is True
        assert result == "All good."

    def test_stream_json_flag_in_command(self, tmp_path):
        """Verify --output-format stream-json and --verbose are passed when streaming."""
        config = _make_config(tmp_path)
        task = _make_task()

        captured_cmd = []

        def capture_popen(cmd, **kwargs):
            captured_cmd.extend(cmd)
            mock = MagicMock()
            mock.stdout = iter([
                json.dumps({"type": "result", "subtype": "success", "result": "ok"}) + "\n"
            ])
            mock.stderr = iter([])
            mock.returncode = 0
            mock.wait.return_value = 0
            mock.kill = MagicMock()
            return mock

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", side_effect=capture_popen),
        ]
        with contextmanager_chain(patches):
            execute_task(task, config, [], on_progress=lambda m: None)

        assert "--output-format" in captured_cmd
        idx = captured_cmd.index("--output-format")
        assert captured_cmd[idx + 1] == "stream-json"
        assert "--verbose" in captured_cmd
        # Default permissive mode uses --dangerously-skip-permissions
        assert "--dangerously-skip-permissions" in captured_cmd

    def test_timeout_kills_process(self, tmp_path):
        """Timeout fires and returns proper error message."""
        config = _make_config(tmp_path)
        task = _make_task()

        mock_process = self._make_mock_process([], returncode=-9)

        class InstantTimer:
            def __init__(self, interval, fn):
                self._fn = fn
            def start(self):
                self._fn()
            def cancel(self):
                pass

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
            patch("istota.executor.threading.Timer", InstantTimer),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(
                task, config, [], on_progress=lambda m: None,
            )

        assert success is False
        assert "timed out" in result.lower()
        mock_process.kill.assert_called_once()

    def test_fallback_to_result_file(self, tmp_path):
        """When no ResultEvent is parsed in stream, falls back to result file."""
        config = _make_config(tmp_path)
        task = _make_task()

        # Per-user temp dir
        user_temp = config.temp_dir / task.user_id
        user_temp.mkdir(parents=True, exist_ok=True)
        result_file_path = user_temp / f"task_{task.id}_result.txt"

        def fake_popen(cmd, **kwargs):
            result_file_path.write_text("Result from file fallback")
            mock = MagicMock()
            mock.stdout = iter([
                json.dumps({"type": "system", "subtype": "init", "cwd": "/tmp"}) + "\n",
            ])
            mock.stderr = iter([])
            mock.returncode = 0
            mock.wait.return_value = 0
            mock.kill = MagicMock()
            return mock

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", side_effect=fake_popen),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(
                task, config, [], on_progress=lambda m: None,
            )

        assert success is True
        assert result == "Result from file fallback"

    def test_stderr_captured_on_failure(self, tmp_path):
        """When streaming Claude fails without ResultEvent, stderr is returned."""
        config = _make_config(tmp_path)
        task = _make_task()

        mock_process = self._make_mock_process(
            stdout_lines=[],
            stderr_lines=["Error: API key expired\n"],
            returncode=1,
        )

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(
                task, config, [], on_progress=lambda m: None,
            )

        assert success is False
        assert "API key expired" in result

    def test_no_output_at_all(self, tmp_path):
        """When streaming Claude produces nothing, descriptive error is returned."""
        config = _make_config(tmp_path)
        task = _make_task()

        mock_process = self._make_mock_process([], returncode=1)

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(
                task, config, [], on_progress=lambda m: None,
            )

        assert success is False
        assert "no output" in result.lower()


class TestDryRun:
    def test_dry_run_returns_prompt(self, tmp_path):
        """Dry run returns prompt without invoking subprocess."""
        config = _make_config(tmp_path)
        task = _make_task()

        with (
            patch("istota.executor.subprocess.Popen") as mock_popen,
            patch("istota.executor.subprocess.run") as mock_run,
            patch("istota.executor.select_relevant_context", return_value=[]),
            patch("istota.executor.read_user_memory_v2", return_value=None),
            patch("istota.executor.ensure_user_directories_v2"),
            patch("istota.executor.read_channel_memory", return_value=None),
            patch("istota.executor.ensure_channel_directories"),
            patch("istota.executor.get_caldav_client"),
            patch("istota.executor.get_calendars_for_user", return_value=None),
            patch("istota.skills_loader.load_skill_index", return_value={}),
            patch("istota.skills_loader.select_skills", return_value=[]),
            patch("istota.skills_loader.load_skills", return_value=None),
        ):
            success, result, _actions = execute_task(task, config, [], dry_run=True)

        assert success is True
        assert "[DRY RUN]" in result
        mock_popen.assert_not_called()
        mock_run.assert_not_called()


def _apply_executor_patches(stack, extra_returns=None):
    """Apply standard executor patches using an ExitStack. Returns dict of mocks."""
    returns = dict(zip(_EXECUTOR_PATCHES, _EXECUTOR_PATCH_RETURNS))
    if extra_returns:
        returns.update(extra_returns)
    mocks = {}
    for name, ret in returns.items():
        mocks[name] = stack.enter_context(patch(name, return_value=ret))
    return mocks


class TestPerUserTempDir:
    def test_get_user_temp_dir(self, tmp_path):
        config = _make_config(tmp_path)
        result = get_user_temp_dir(config, "alice")
        assert result == config.temp_dir / "alice"

    def test_temp_files_go_to_user_dir(self, tmp_path):
        """Prompt and result files should be in user subdirectory."""
        config = _make_config(tmp_path)
        task = _make_task(user_id="alice")

        with ExitStack() as stack:
            mock_run = stack.enter_context(patch("istota.executor.subprocess.run"))
            mock_run.return_value = MagicMock(
                returncode=0,
                stdout="result text",
                stderr="",
            )
            _apply_executor_patches(stack)
            execute_task(task, config, [])

        user_temp = config.temp_dir / "alice"
        assert user_temp.exists()
        prompt_file = user_temp / "task_1_prompt.txt"
        assert prompt_file.exists()


class TestDatedMemoriesInPrompt:
    def test_dated_memories_not_auto_loaded(self, tmp_path):
        """Dated memories are stored for search/reference, not auto-loaded into prompts."""
        config = _make_config(tmp_path)
        config.sleep_cycle = SleepCycleConfig(enabled=True)
        config.users = {
            "testuser": UserConfig(display_name="Test")
        }
        task = _make_task()

        with ExitStack() as stack:
            _apply_executor_patches(stack)
            success, result, _actions = execute_task(task, config, [], dry_run=True)

        assert "Recent context (from previous days)" not in result

    def test_briefing_excludes_user_memory(self, tmp_path):
        """Briefing tasks should not include user memory to avoid leaking private context."""
        config = _make_config(tmp_path)
        task = _make_task(source_type="briefing")

        with ExitStack() as stack:
            _apply_executor_patches(stack, {
                "istota.executor.read_user_memory_v2": "Portfolio: 5% SGOL position",
            })
            success, result, _actions = execute_task(task, config, [], dry_run=True)

        assert "SGOL" not in result
        assert "User memory" not in result

    def test_non_briefing_includes_user_memory(self, tmp_path):
        """Non-briefing tasks should still include user memory."""
        config = _make_config(tmp_path)
        task = _make_task(source_type="talk")

        with ExitStack() as stack:
            _apply_executor_patches(stack, {
                "istota.executor.read_user_memory_v2": "Portfolio: 5% SGOL position",
            })
            success, result, _actions = execute_task(task, config, [], dry_run=True)

        assert "SGOL" in result


class TestChannelMemoryInPrompt:
    def test_build_prompt_with_channel_memory(self, tmp_path):
        """Channel memory section appears in prompt when provided."""
        from istota.executor import build_prompt

        config = _make_config(tmp_path)
        task = _make_task(conversation_token="room42")

        prompt = build_prompt(
            task, [], config,
            channel_memory="- Project uses PostgreSQL",
        )
        assert "## Channel memory" in prompt
        assert "Project uses PostgreSQL" in prompt

    def test_build_prompt_without_channel_memory(self, tmp_path):
        """Channel memory section absent when None."""
        from istota.executor import build_prompt

        config = _make_config(tmp_path)
        task = _make_task()

        prompt = build_prompt(task, [], config, channel_memory=None)
        assert "## Channel memory" not in prompt

    def test_build_prompt_includes_conversation_token(self, tmp_path):
        """Conversation token appears in prompt metadata."""
        from istota.executor import build_prompt

        config = _make_config(tmp_path)
        task = _make_task(conversation_token="room42")

        prompt = build_prompt(task, [], config)
        assert "Conversation token: room42" in prompt

    def test_build_prompt_conversation_token_none(self, tmp_path):
        """Conversation token shows 'none' when not set."""
        from istota.executor import build_prompt

        config = _make_config(tmp_path)
        task = _make_task()

        prompt = build_prompt(task, [], config)
        assert "Conversation token: none" in prompt

    def test_execute_task_loads_channel_memory(self, tmp_path):
        """execute_task calls read_channel_memory when conversation_token is set."""
        config = _make_config(tmp_path)
        task = _make_task(conversation_token="room42")

        with ExitStack() as stack:
            mocks = _apply_executor_patches(stack, {
                "istota.executor.read_channel_memory": "- Channel note",
            })
            success, result, _actions = execute_task(task, config, [], dry_run=True)

        assert "## Channel memory" in result
        assert "Channel note" in result
        mocks["istota.executor.read_channel_memory"].assert_called_once_with(config, "room42")

    def test_execute_task_no_channel_memory_without_token(self, tmp_path):
        """execute_task skips channel memory when no conversation_token."""
        config = _make_config(tmp_path)
        task = _make_task()  # no conversation_token

        with ExitStack() as stack:
            mocks = _apply_executor_patches(stack)
            success, result, _actions = execute_task(task, config, [], dry_run=True)

        assert "## Channel memory" not in result
        mocks["istota.executor.read_channel_memory"].assert_not_called()


class TestSimpleExecutionRetry:
    """Test that _execute_simple retries transient API errors."""

    def _api_error_output(self, status_code=500):
        return f'API Error: {status_code} {{"error": {{"message": "Internal server error"}}, "request_id": "req_123"}}'

    def test_retries_transient_api_error(self, tmp_path):
        """Simple execution retries on 500 API error and succeeds on second attempt."""
        config = _make_config(tmp_path)
        task = _make_task()

        call_count = 0

        def fake_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            mock.stderr = ""
            if call_count == 1:
                mock.stdout = self._api_error_output(500)
                mock.returncode = 1
            else:
                mock.stdout = "Success on retry"
                mock.returncode = 0
            return mock

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.run", side_effect=fake_run),
            patch("istota.executor.time.sleep"),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(task, config, [])

        assert success is True
        assert result == "Success on retry"
        assert call_count == 2

    def test_no_retry_for_non_transient_error(self, tmp_path):
        """Simple execution does NOT retry non-transient errors (e.g. 400)."""
        config = _make_config(tmp_path)
        task = _make_task()

        call_count = 0

        def fake_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            mock.stdout = 'API Error: 400 {"error": {"message": "Bad request"}}'
            mock.stderr = ""
            mock.returncode = 1
            return mock

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.run", side_effect=fake_run),
            patch("istota.executor.time.sleep"),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(task, config, [])

        assert success is False
        assert call_count == 1  # No retry

    def test_fails_after_max_retries(self, tmp_path):
        """Simple execution gives up after 3 transient API errors."""
        config = _make_config(tmp_path)
        task = _make_task()

        call_count = 0

        def fake_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            mock.stdout = self._api_error_output(500)
            mock.stderr = ""
            mock.returncode = 1
            return mock

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.run", side_effect=fake_run),
            patch("istota.executor.time.sleep"),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(task, config, [])

        assert success is False
        assert "API Error" in result
        assert call_count == 3

    def test_retries_429_rate_limit(self, tmp_path):
        """Simple execution retries on 429 rate limit errors."""
        config = _make_config(tmp_path)
        task = _make_task()

        call_count = 0

        def fake_run(cmd, **kwargs):
            nonlocal call_count
            call_count += 1
            mock = MagicMock()
            mock.stderr = ""
            if call_count <= 2:
                mock.stdout = self._api_error_output(429)
                mock.returncode = 1
            else:
                mock.stdout = "Finally succeeded"
                mock.returncode = 0
            return mock

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.run", side_effect=fake_run),
            patch("istota.executor.time.sleep"),
        ]
        with contextmanager_chain(patches):
            success, result, _actions = execute_task(task, config, [])

        assert success is True
        assert result == "Finally succeeded"
        assert call_count == 3
