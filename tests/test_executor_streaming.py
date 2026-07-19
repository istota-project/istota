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
from istota.events import EventWriter
from istota import db


class _RaisingSubscriber:
    """Subscriber whose on_event always raises — EventWriter must swallow it."""

    def on_event(self, event):
        raise RuntimeError("kaboom")

    def on_finish(self):
        pass


class _RecordingSubscriber:
    """Captures every TaskEvent the writer emits, for assertions."""

    def __init__(self):
        self.events = []

    def on_event(self, event):
        self.events.append(event)

    def on_finish(self):
        pass

    def kinds(self):
        return [e.kind for e in self.events]


def _writer(task, config, *, subscriber=None):
    """An EventWriter with DB writes off (executor-path tests don't assert rows)."""
    w = EventWriter(task.id, str(config.db_path), enabled=False)
    if subscriber is not None:
        w.subscribe(subscriber)
    return w


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
    "istota.skills._loader.load_skill_index",
    "istota.skills._loader.select_skills",
    "istota.skills._loader.load_skills",
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
            success, result, _actions, _trace = execute_task(task, config, [])

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
            success, result, _actions, _trace = execute_task(task, config, [])

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
            success, result, _actions, _trace = execute_task(task, config, [])

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
            success, result, _actions, _trace = execute_task(task, config, [])

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
            success, result, _actions, _trace = execute_task(task, config, [])

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
        assert "--allowedTools" not in captured_cmd
        assert "--dangerously-skip-permissions" in captured_cmd

    def test_custom_system_prompt_in_command(self, tmp_path):
        """--system-prompt-file is added when custom_system_prompt is True."""
        config = _make_config(tmp_path)
        config.custom_system_prompt = True
        # Create the system-prompt.md file where executor expects it
        sp_path = config.skills_dir.parent / "system-prompt.md"
        sp_path.write_text("# Test system prompt\n")
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

        assert "--system-prompt-file" in captured_cmd
        idx = captured_cmd.index("--system-prompt-file")
        assert captured_cmd[idx + 1] == str(sp_path)

    def test_no_custom_system_prompt_by_default(self, tmp_path):
        """--system-prompt-file is NOT added when custom_system_prompt is False."""
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

        assert "--system-prompt-file" not in captured_cmd

    def test_custom_system_prompt_missing_file(self, tmp_path):
        """--system-prompt-file is skipped when enabled but file doesn't exist."""
        config = _make_config(tmp_path)
        config.custom_system_prompt = True
        # Don't create the file
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

        assert "--system-prompt-file" not in captured_cmd

    def test_cli_not_found(self, tmp_path):
        """FileNotFoundError from subprocess.run is handled gracefully."""
        config = _make_config(tmp_path)
        task = _make_task()

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.run", side_effect=FileNotFoundError),
        ]
        with contextmanager_chain(patches):
            success, result, _actions, _trace = execute_task(task, config, [])

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
                "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "The answer is 42."}]},
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
            success, result, _actions, _trace = execute_task(
                task, config, [], event_writer=_writer(task, config),
            )

        assert success is True
        assert result == "The answer is 42."

    def test_model_recorded_from_init_event(self, tmp_path):
        """The model from the stream-json init frame is recorded as model_used."""
        config = _make_config(tmp_path)
        task = _make_task()  # no per-task model; config.model empty

        stream_lines = [
            json.dumps({
                "type": "system", "subtype": "init", "cwd": "/tmp",
                "model": "claude-opus-4-8",
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success", "result": "done",
            }) + "\n",
        ]

        mock_process = self._make_mock_process(stream_lines)

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
        ]
        with contextmanager_chain(patches):
            success, _result, _actions, _trace = execute_task(
                task, config, [], event_writer=_writer(task, config),
            )

        assert success is True
        # Executor records the actually-used model on the task object (model_used,
        # not the override `model`) so the scheduler can surface it in the
        # terminal `done` event / chat meta, and so a retry still re-resolves the
        # default rather than pinning this attempt's model.
        assert task.model_used == "claude-opus-4-8"
        assert task.model is None

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
            success, result, _actions, _trace = execute_task(
                task, config, [], event_writer=_writer(task, config),
            )

        assert success is False
        assert result == "Something went wrong"

    def test_progress_callback_called_for_tool_use(self, tmp_path):
        """The executor emits tool_start events for ToolUseEvents."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_tool_use = True
        config.scheduler.progress_show_text = False
        task = _make_task()

        stream_lines = [
            json.dumps({"type": "system", "subtype": "init", "cwd": "/tmp"}) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/tmp/data.txt"}},
                ]},
            }) + "\n",
            json.dumps({"type": "user", "message": {"role": "user"}}) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "t2", "name": "Bash",
                     "input": {"command": "wc -l /tmp/data.txt", "description": "Count lines"}},
                ]},
            }) + "\n",
            json.dumps({"type": "user", "message": {"role": "user"}}) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Done."}]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success", "result": "File has 42 lines.",
            }) + "\n",
        ]

        mock_process = self._make_mock_process(stream_lines)
        rec = _RecordingSubscriber()

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
        ]
        with contextmanager_chain(patches):
            success, result, _actions, _trace = execute_task(
                task, config, [], event_writer=_writer(task, config, subscriber=rec),
            )

        assert success is True
        # Only the ResultEvent text is returned
        assert result == "File has 42 lines."
        tool_starts = [e for e in rec.events if e.kind == "tool_start"]
        assert len(tool_starts) == 2
        assert tool_starts[0].payload["description"] == "📄 Reading data.txt"
        assert tool_starts[0].payload["tool_call_id"] == "t1"  # real block id threaded
        assert tool_starts[1].payload["description"] == "⚙️ Count lines"
        assert tool_starts[1].payload["tool_call_id"] == "t2"

    def test_text_progress_when_enabled(self, tmp_path):
        """progress_text events are emitted for TextEvents when progress_show_text=True."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_tool_use = False
        config.scheduler.progress_show_text = True
        task = _make_task()

        stream_lines = [
            json.dumps({"type": "system", "subtype": "init", "cwd": "/tmp"}) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Checking things..."}]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success", "result": "Done.",
            }) + "\n",
        ]

        mock_process = self._make_mock_process(stream_lines)
        rec = _RecordingSubscriber()

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
        ]
        with contextmanager_chain(patches):
            success, result, _actions, _trace = execute_task(
                task, config, [], event_writer=_writer(task, config, subscriber=rec),
            )

        assert success is True
        texts = [e for e in rec.events if e.kind == "progress_text"]
        assert len(texts) == 1
        assert texts[0].payload["text"] == "Checking things..."

    def test_callback_exception_does_not_affect_execution(self, tmp_path):
        """If on_progress raises, task still completes normally."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_tool_use = True
        task = _make_task()

        stream_lines = [
            json.dumps({
                "type": "assistant",
                "message": {"stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/tmp/x.txt"}},
                ]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success", "result": "All good.",
            }) + "\n",
        ]

        mock_process = self._make_mock_process(stream_lines)

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
        ]
        with contextmanager_chain(patches):
            success, result, _actions, _trace = execute_task(
                task, config, [],
                event_writer=_writer(task, config, subscriber=_RaisingSubscriber()),
            )

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
            execute_task(task, config, [], event_writer=_writer(task, config))

        assert "--output-format" in captured_cmd
        idx = captured_cmd.index("--output-format")
        assert captured_cmd[idx + 1] == "stream-json"
        assert "--verbose" in captured_cmd
        assert "--allowedTools" not in captured_cmd
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
            success, result, _actions, _trace = execute_task(
                task, config, [], event_writer=_writer(task, config),
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
            success, result, _actions, _trace = execute_task(
                task, config, [], event_writer=_writer(task, config),
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
            success, result, _actions, _trace = execute_task(
                task, config, [], event_writer=_writer(task, config),
            )

        assert success is False
        assert "API key expired" in result

    def test_only_result_event_text_returned(self, tmp_path):
        """Only the ResultEvent text is returned; intermediate text blocks are not prepended."""
        config = _make_config(tmp_path)
        task = _make_task()

        stream_lines = [
            json.dumps({"type": "system", "subtype": "init", "cwd": "/tmp"}) + "\n",
            # First assistant turn: diagnostic text
            json.dumps({
                "type": "assistant",
                "message": {"stop_reason": "end_turn", "content": [
                    {"type": "text", "text": "I found the root cause: the config is misconfigured."},
                ]},
            }) + "\n",
            # Tool call
            json.dumps({
                "type": "assistant",
                "message": {"stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/tmp/config.toml"}},
                ]},
            }) + "\n",
            json.dumps({"type": "user", "message": {"role": "user"}}) + "\n",
            # Final turn: self-contained summary (this is what ResultEvent contains)
            json.dumps({
                "type": "assistant",
                "message": {"stop_reason": "end_turn", "content": [{"type": "text", "text": "Fixed it."}]},
            }) + "\n",
            json.dumps({
                "type": "result", "subtype": "success", "result": "Fixed it.",
            }) + "\n",
        ]

        mock_process = self._make_mock_process(stream_lines)

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
        ]
        with contextmanager_chain(patches):
            success, result, _actions, _trace = execute_task(
                task, config, [], event_writer=_writer(task, config),
            )

        assert success is True
        assert result == "Fixed it."
        assert "root cause" not in result

    def test_no_output_at_all(self, tmp_path):
        """When streaming Claude produces nothing, descriptive error is returned."""
        config = _make_config(tmp_path)
        task = _make_task()

        mock_process = self._make_mock_process([], returncode=1)

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
        ]
        with contextmanager_chain(patches):
            success, result, _actions, _trace = execute_task(
                task, config, [], event_writer=_writer(task, config),
            )

        assert success is False
        assert "no output" in result.lower()

    def test_sigterm_cancel_detected_via_db_flag(self, tmp_path):
        """When !stop sends SIGTERM and kills the process before the in-loop
        cancel check fires, the post-loop DB check still detects the
        cancellation and returns 'Cancelled by user' (not a retryable error)."""
        config = _make_config(tmp_path)
        task = _make_task()

        # Process killed by SIGTERM — no ResultEvent, rc=-15
        mock_process = self._make_mock_process([], returncode=-15)

        patches = _patch_executor() + [
            patch("istota.executor.subprocess.Popen", return_value=mock_process),
            # The post-loop DB check should find cancel_requested=1
            patch("istota.executor.db.is_task_cancelled", return_value=True),
            patch("istota.executor.db.update_task_pid"),
        ]
        with contextmanager_chain(patches):
            success, result, _actions, _trace = execute_task(
                task, config, [], event_writer=_writer(task, config),
            )

        assert success is False
        assert result == "Cancelled by user"


class TestStreamSurfaceCoalescing:
    """Stage 2 — the executor's text_delta coalescer + stream/push gating.

    The 'push untouched' guard lives here: a push task (source_type → talk) must
    emit zero text_delta rows and keep progress_text behaviour; a stream task
    (source_type='web') routes answer text into text_delta instead.
    """

    def _make_mock_process(self, stdout_lines, returncode=0):
        mock = MagicMock()
        mock.stdout = iter(stdout_lines)
        mock.stderr = iter([])
        mock.returncode = returncode
        mock.wait.return_value = returncode
        mock.kill = MagicMock()
        return mock

    def _run(self, config, task, stream_lines, rec):
        patches = _patch_executor() + [
            patch(
                "istota.executor.subprocess.Popen",
                return_value=self._make_mock_process(stream_lines),
            ),
        ]
        with contextmanager_chain(patches):
            return execute_task(
                task, config, [],
                event_writer=_writer(task, config, subscriber=rec),
            )

    def test_push_task_emits_no_text_delta(self, tmp_path):
        """Push surface (talk): TextEvent → progress_text, never text_delta."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_text = True
        task = _make_task(source_type="cli")  # → talk surface (push)

        stream_lines = [
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "Checking things"}]},
            }) + "\n",
            json.dumps({"type": "result", "subtype": "success", "result": "Done."}) + "\n",
        ]
        rec = _RecordingSubscriber()
        success, result, _a, _t = self._run(config, task, stream_lines, rec)

        assert success is True
        assert "text_delta" not in rec.kinds()
        assert [e.payload["text"] for e in rec.events if e.kind == "progress_text"] == [
            "Checking things"
        ]

    def test_stream_task_routes_text_to_text_delta(self, tmp_path):
        """Stream surface (web): block TextEvents route into text_delta (coarse),
        not progress_text — even with progress_show_text off."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_text = False
        task = _make_task(source_type="web")

        stream_lines = [
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "First part."}]},
            }) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"id": "m2", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "Second part."}]},
            }) + "\n",
            json.dumps({"type": "result", "subtype": "success",
                        "result": "First part. Second part."}) + "\n",
        ]
        rec = _RecordingSubscriber()
        success, result, _a, _t = self._run(config, task, stream_lines, rec)

        assert success is True
        assert "progress_text" not in rec.kinds()
        deltas = [e for e in rec.events if e.kind == "text_delta"]
        assert deltas, "stream task should produce text_delta events"
        # Coalesced (fast test → no time/size flush): one delta carrying both blocks.
        assert "".join(e.payload["text"] for e in deltas) == "First part.Second part."

    def test_stream_task_discards_pretool_narration(self, tmp_path):
        """Text that precedes a tool call is lead-in narration, not the answer —
        it is DISCARDED (never emitted as text_delta). Only the model's final
        answer (after the last tool) streams, and it lands after the tool."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_tool_use = True
        task = _make_task(source_type="web")

        stream_lines = [
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "Let me look."}]},
            }) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"id": "m2", "stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/tmp/x.txt"}},
                ]},
            }) + "\n",
            json.dumps({"type": "user", "message": {"role": "user"}}) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"id": "m3", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "All set."}]},
            }) + "\n",
            json.dumps({"type": "result", "subtype": "success", "result": "All set."}) + "\n",
        ]
        rec = _RecordingSubscriber()
        success, _r, _a, _t = self._run(config, task, stream_lines, rec)

        assert success is True
        deltas = [e for e in rec.events if e.kind == "text_delta"]
        # The pre-tool narration is gone; only the final answer streamed.
        joined = "".join(e.payload["text"] for e in deltas)
        assert "Let me look." not in joined
        assert joined == "All set."
        # And that surviving answer delta lands AFTER the tool_start.
        kinds = rec.kinds()
        assert kinds.index("text_delta") > kinds.index("tool_start")

    def test_pretool_narration_discarded_even_with_tool_display_off(self, tmp_path):
        """The pre-tool narration discard is a STREAM-SURFACE property, not a
        tool-display one: with progress_show_tool_use=False there is no
        tool_start row, but narration must still be dropped (not flash)."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_tool_use = False
        task = _make_task(source_type="web")

        stream_lines = [
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "Let me look."}]},
            }) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"id": "m2", "stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/tmp/x.txt"}},
                ]},
            }) + "\n",
            json.dumps({"type": "user", "message": {"role": "user"}}) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"id": "m3", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "All set."}]},
            }) + "\n",
            json.dumps({"type": "result", "subtype": "success", "result": "All set."}) + "\n",
        ]
        rec = _RecordingSubscriber()
        success, _r, _a, _t = self._run(config, task, stream_lines, rec)

        assert success is True
        # Tool display is off → no tool_start rows at all …
        assert "tool_start" not in rec.kinds()
        # … but the pre-tool narration is STILL discarded; only the answer streams.
        deltas = [e for e in rec.events if e.kind == "text_delta"]
        joined = "".join(e.payload["text"] for e in deltas)
        assert "Let me look." not in joined
        assert joined == "All set."

    def test_long_pretool_narration_held_by_gate_not_leaked(self, tmp_path):
        """Regression: lead-in narration longer than the coalescer's size
        threshold (120) but shorter than the narration gate (200) must NOT
        leak. Pre-gate, a >120-char block flushed immediately (and in
        production the 250ms timer flushed even short narration) — the
        text_delta reached the browser before the tool boundary could discard
        it. The gate holds the whole run until it crosses the ceiling, so a
        tool-followed narration run is discarded intact."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_tool_use = True
        task = _make_task(source_type="web")

        narration = (
            "Let me take a careful look through your calendar for the rest of "
            "this week so I can find any scheduling conflicts before I give you "
            "a final answer here."
        )
        assert 120 < len(narration) < 200  # exactly the window the old flush leaked

        stream_lines = [
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": narration}]},
            }) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"id": "m2", "stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/tmp/x.txt"}},
                ]},
            }) + "\n",
            json.dumps({"type": "user", "message": {"role": "user"}}) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"id": "m3", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "All set."}]},
            }) + "\n",
            json.dumps({"type": "result", "subtype": "success", "result": "All set."}) + "\n",
        ]
        rec = _RecordingSubscriber()
        success, _r, _a, _t = self._run(config, task, stream_lines, rec)

        assert success is True
        deltas = [e for e in rec.events if e.kind == "text_delta"]
        joined = "".join(e.payload["text"] for e in deltas)
        assert "calendar" not in joined  # narration never streamed
        assert joined == "All set."

    def test_over_gate_text_unlocks_and_streams(self, tmp_path):
        """A text run longer than the gate (280) unlocks and streams — the gate
        only suppresses short, tool-followed narration, never a real answer."""
        config = _make_config(tmp_path)
        task = _make_task(source_type="web")

        answer = "x" * 360  # comfortably over the 280-char gate
        stream_lines = [
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": answer}]},
            }) + "\n",
            json.dumps({"type": "result", "subtype": "success", "result": answer}) + "\n",
        ]
        rec = _RecordingSubscriber()
        success, _r, _a, _t = self._run(config, task, stream_lines, rec)

        assert success is True
        deltas = [e for e in rec.events if e.kind == "text_delta"]
        assert "".join(e.payload["text"] for e in deltas) == answer

    def test_substantial_block_before_tool_preserved_in_full(self, tmp_path):
        """A substantial intermediate block (the model writes analysis, then acts
        on it via a tool) must reach the stream IN FULL — its unflushed tail at
        the tool boundary is flushed, not discarded. Delivered as token-level
        partial deltas so a flush window remains buffered at the boundary (the
        gap the old discard-everything path dropped); the whole-block dedup then
        suppresses the trailing TextEvent, so the streamed text equals the block
        exactly (no doubling)."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_tool_use = True
        task = _make_task(source_type="web")

        # 320 chars in 10-char tokens: unlocks at 280 (flush), then 40 chars stay
        # buffered (< the 120 flush window) until the tool boundary.
        block = "".join(f"{i:09d}." for i in range(32))  # 32 * 10 = 320 chars
        assert len(block) == 320
        tokens = [block[i:i + 10] for i in range(0, len(block), 10)]

        stream_lines = (
            [
                self._partial({
                    "type": "content_block_delta", "index": 0,
                    "delta": {"type": "text_delta", "text": tok},
                })
                for tok in tokens
            ]
            + [
                json.dumps({
                    "type": "assistant",
                    "message": {"id": "m1", "stop_reason": "end_turn",
                                "content": [{"type": "text", "text": block}]},
                }) + "\n",
                json.dumps({
                    "type": "assistant",
                    "message": {"id": "m2", "stop_reason": "tool_use", "content": [
                        {"type": "tool_use", "id": "t1", "name": "Edit",
                         "input": {"file_path": "/tmp/x.txt"}},
                    ]},
                }) + "\n",
                json.dumps({"type": "user", "message": {"role": "user"}}) + "\n",
                json.dumps({
                    "type": "assistant",
                    "message": {"id": "m3", "stop_reason": "end_turn",
                                "content": [{"type": "text", "text": "Done."}]},
                }) + "\n",
                json.dumps({"type": "result", "subtype": "success", "result": "Done."}) + "\n",
            ]
        )
        rec = _RecordingSubscriber()
        success, result, _a, _t = self._run(config, task, stream_lines, rec)

        assert success is True
        deltas = [e for e in rec.events if e.kind == "text_delta"]
        joined = "".join(e.payload["text"] for e in deltas)
        # The full intermediate block survived the tool boundary (tail flushed,
        # not dropped) and streamed exactly once (the trailing whole-block
        # TextEvent was deduped).
        assert block in joined
        assert joined.count("000000031.") == 1  # the tail token, exactly once
        # The short final answer ("Done.", after the last tool) never crosses the
        # gate and rides the canonical result event rather than the delta channel
        # (where it would be deduped against the already-streamed deltas). So the
        # delta stream is exactly the substantial block, and the answer is the
        # result. Both reach the UI: the block as its own prose group, the answer
        # as the trailing text.
        assert joined == block
        assert result == "Done."
        # The substantial block streamed BEFORE the tool boundary.
        kinds = rec.kinds()
        assert kinds.index("text_delta") < kinds.index("tool_start")

    def test_stream_task_routes_thinking_to_thinking_event(self, tmp_path):
        """Stream surface (web): a ClaudeCode thinking block → coalesced
        `thinking` event, never `text_delta` / `progress_text`."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_text = False
        task = _make_task(source_type="web")

        stream_lines = [
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "content": [
                    {"type": "thinking", "thinking": "The user wants the answer."},
                ]},
            }) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"id": "m2", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "42."}]},
            }) + "\n",
            json.dumps({"type": "result", "subtype": "success", "result": "42."}) + "\n",
        ]
        rec = _RecordingSubscriber()
        success, _r, _a, _t = self._run(config, task, stream_lines, rec)

        assert success is True
        thinks = [e for e in rec.events if e.kind == "thinking"]
        assert thinks, "stream task should produce thinking events"
        assert "".join(e.payload["text"] for e in thinks) == "The user wants the answer."
        # Thinking is its own kind — not leaked into the answer channels.
        assert "progress_text" not in rec.kinds()
        # Thinking row precedes the answer text_delta (boundary flush keeps order).
        kinds = rec.kinds()
        assert kinds.index("thinking") < kinds.index("text_delta")

    def test_push_task_emits_no_thinking(self, tmp_path):
        """Push surface (talk): a thinking block produces ZERO thinking events —
        reasoning is web/repl-only, with no progress_text fallback."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_text = True
        task = _make_task(source_type="cli")  # → talk surface (push)

        stream_lines = [
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "content": [
                    {"type": "thinking", "thinking": "Reasoning the user never sees."},
                ]},
            }) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"id": "m2", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "Visible."}]},
            }) + "\n",
            json.dumps({"type": "result", "subtype": "success", "result": "Done."}) + "\n",
        ]
        rec = _RecordingSubscriber()
        success, _r, _a, _t = self._run(config, task, stream_lines, rec)

        assert success is True
        assert "thinking" not in rec.kinds()
        # The thinking text must not appear in any emitted payload on a push task.
        for e in rec.events:
            assert "Reasoning the user never sees" not in str(e.payload)

    def test_thinking_buffer_independent_of_text_buffer(self, tmp_path):
        """Thinking flushes into a `thinking` row before the answer's `text_delta`
        row — the two buffers never merge (separate render targets)."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_tool_use = True
        task = _make_task(source_type="web")

        stream_lines = [
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "content": [
                    {"type": "thinking", "thinking": "Planning the search."},
                ]},
            }) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"id": "m2", "stop_reason": "tool_use", "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read",
                     "input": {"file_path": "/tmp/x.txt"}},
                ]},
            }) + "\n",
            json.dumps({"type": "user", "message": {"role": "user"}}) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"id": "m3", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "All set."}]},
            }) + "\n",
            json.dumps({"type": "result", "subtype": "success", "result": "All set."}) + "\n",
        ]
        rec = _RecordingSubscriber()
        success, _r, _a, _t = self._run(config, task, stream_lines, rec)

        assert success is True
        kinds = rec.kinds()
        # thinking flushed at the tool boundary → before the tool_start, and the
        # answer text_delta is a distinct, later row.
        assert kinds.index("thinking") < kinds.index("tool_start")
        thinks = [e for e in rec.events if e.kind == "thinking"]
        deltas = [e for e in rec.events if e.kind == "text_delta"]
        assert "".join(e.payload["text"] for e in thinks) == "Planning the search."
        assert "".join(e.payload["text"] for e in deltas) == "All set."

    @staticmethod
    def _partial(inner: dict) -> str:
        return json.dumps({"type": "stream_event", "event": inner, "session_id": "s"}) + "\n"

    def test_partial_text_deltas_stream_and_whole_block_deduped(self, tmp_path):
        """With --include-partial-messages, ClaudeCodeBrain emits text_delta
        frames *before* the whole assistant block. The deltas stream on a stream
        surface; the trailing whole-block TextEvent (same text) is deduped so the
        answer is not doubled (the bug: final response dumped all at once)."""
        config = _make_config(tmp_path)
        config.scheduler.progress_show_text = False
        task = _make_task(source_type="web")

        answer = ("This is the final answer streamed token by token. " * 6).strip()  # > gate
        # Three deltas that concatenate to `answer`, then the whole block, result.
        third = len(answer) // 3
        chunks = [answer[:third], answer[third:2 * third], answer[2 * third:]]
        stream_lines = [
            self._partial({
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "text_delta", "text": c},
            })
            for c in chunks
        ] + [
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": answer}]},
            }) + "\n",
            json.dumps({"type": "result", "subtype": "success", "result": answer}) + "\n",
        ]
        rec = _RecordingSubscriber()
        success, result, _a, _t = self._run(config, task, stream_lines, rec)

        assert success is True
        assert result == answer
        assert "progress_text" not in rec.kinds()
        deltas = [e for e in rec.events if e.kind == "text_delta"]
        assert deltas, "partial frames should produce text_delta events"
        # Streamed exactly once — the whole-block TextEvent was deduped, not
        # re-streamed (would be answer*2 if the dedup failed).
        assert "".join(e.payload["text"] for e in deltas) == answer

    def test_partial_thinking_deltas_stream_and_whole_block_deduped(self, tmp_path):
        """Thinking deltas from partial frames stream as `thinking`; the trailing
        whole thinking block is deduped (not double-counted)."""
        config = _make_config(tmp_path)
        task = _make_task(source_type="web")

        reasoning = "Let me reason carefully about this step by step. " * 4
        stream_lines = [
            self._partial({
                "type": "content_block_delta", "index": 0,
                "delta": {"type": "thinking_delta", "thinking": reasoning},
            }),
            json.dumps({
                "type": "assistant",
                "message": {"id": "m1", "content": [
                    {"type": "thinking", "thinking": reasoning},
                ]},
            }) + "\n",
            json.dumps({
                "type": "assistant",
                "message": {"id": "m2", "stop_reason": "end_turn",
                            "content": [{"type": "text", "text": "Answer."}]},
            }) + "\n",
            json.dumps({"type": "result", "subtype": "success", "result": "Answer."}) + "\n",
        ]
        rec = _RecordingSubscriber()
        success, _r, _a, _t = self._run(config, task, stream_lines, rec)

        assert success is True
        thinks = [e for e in rec.events if e.kind == "thinking"]
        assert thinks, "partial frames should produce thinking events"
        # Streamed once — the whole thinking block was deduped, not doubled.
        assert "".join(e.payload["text"] for e in thinks) == reasoning


class TestStreamingStdinDelivery:
    """Regression: the prompt must be written to the subprocess stdin
    concurrently with the on_pid callback, never gated behind it.

    The `claude` CLI aborts its stdin read after ~3s and then runs with an
    empty prompt. The on_pid callback is `db.update_task_pid()`, a SQLite
    write that can block on the write lock under daemon load. If prompt
    delivery waits for that callback to return, a slow DB write pushes the
    write past the CLI's stdin deadline and the task fails with "produced no
    output". Delivery therefore happens on its own thread, started before
    on_pid is invoked.
    """

    def _result_stream(self):
        return iter([
            json.dumps({"type": "system", "subtype": "init", "cwd": "/tmp"}) + "\n",
            json.dumps({"type": "result", "subtype": "success", "result": "ok"}) + "\n",
        ])

    def test_prompt_delivered_while_on_pid_blocks(self, tmp_path):
        from istota.brain.claude_code import ClaudeCodeBrain
        from istota.brain._types import BrainRequest

        stdin_written = threading.Event()
        observed = {}
        written = []

        def record_write(data):
            written.append(data)
            stdin_written.set()
            return len(data)

        def slow_on_pid(_pid):
            # Stand in for a contended db.update_task_pid() write. The prompt
            # must already be (or become) delivered by the writer thread while
            # we sit here — it must NOT depend on this callback returning.
            observed["delivered_during_on_pid"] = stdin_written.wait(timeout=5)

        mock_process = MagicMock()
        mock_process.stdout = self._result_stream()
        mock_process.stderr = iter([])
        mock_process.returncode = 0
        mock_process.wait.return_value = 0
        mock_process.pid = 4242
        mock_process.stdin.write.side_effect = record_write

        req = BrainRequest(
            prompt="THE-PROMPT-PAYLOAD",
            allowed_tools=[],
            cwd=tmp_path,
            env={},
            timeout_seconds=30,
            streaming=True,
            on_progress=lambda _e: None,
            on_pid=slow_on_pid,
        )

        with patch("istota.brain.claude_code.subprocess.Popen", return_value=mock_process):
            result = ClaudeCodeBrain().execute(req)

        assert result.success is True
        # The decisive assertion: the write landed while on_pid was still
        # blocked. With a synchronous write-after-on_pid this is False.
        assert observed["delivered_during_on_pid"] is True
        assert "".join(written) == "THE-PROMPT-PAYLOAD"
        mock_process.stdin.close.assert_called_once()


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
            patch("istota.skills._loader.load_skill_index", return_value={}),
            patch("istota.skills._loader.select_skills", return_value=[]),
            patch("istota.skills._loader.load_skills", return_value=None),
        ):
            success, result, _actions, _trace = execute_task(task, config, [], dry_run=True)

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
            success, result, _actions, _trace = execute_task(task, config, [], dry_run=True)

        assert "Recent context (from previous days)" not in result

    def test_briefing_excludes_user_memory(self, tmp_path):
        """Briefing tasks should not include user memory to avoid leaking private context."""
        from istota.skills._types import SkillMeta
        config = _make_config(tmp_path)
        task = _make_task(source_type="briefing")

        briefing_meta = SkillMeta(name="briefing", description="Briefing", exclude_memory=True)
        with ExitStack() as stack:
            _apply_executor_patches(stack, {
                "istota.executor.read_user_memory_v2": "Portfolio: 5% SGOL position",
                "istota.skills._loader.load_skill_index": {"briefing": briefing_meta},
                "istota.skills._loader.select_skills": ["briefing"],
            })
            success, result, _actions, _trace = execute_task(task, config, [], dry_run=True)

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
            success, result, _actions, _trace = execute_task(task, config, [], dry_run=True)

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
            success, result, _actions, _trace = execute_task(task, config, [], dry_run=True)

        assert "## Channel memory" in result
        assert "Channel note" in result
        mocks["istota.executor.read_channel_memory"].assert_called_once_with(config, "room42")

    def test_execute_task_no_channel_memory_without_token(self, tmp_path):
        """execute_task skips channel memory when no conversation_token."""
        config = _make_config(tmp_path)
        task = _make_task()  # no conversation_token

        with ExitStack() as stack:
            mocks = _apply_executor_patches(stack)
            success, result, _actions, _trace = execute_task(task, config, [], dry_run=True)

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
            success, result, _actions, _trace = execute_task(task, config, [])

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
            success, result, _actions, _trace = execute_task(task, config, [])

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
            success, result, _actions, _trace = execute_task(task, config, [])

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
            success, result, _actions, _trace = execute_task(task, config, [])

        assert success is True
        assert result == "Finally succeeded"
        assert call_count == 3


class TestTmuxFallback:
    """Executor in-attempt fallback (tmux-production spec §4): when the tmux
    brain returns stop_reason="fallback"/"not_found", execute_task reruns the
    same attempt once through claude_code — no new task, no attempt increment."""

    def _fake_brain(self, kind, result):
        from istota.brain._types import BrainResult

        class _FakeBrain:
            def __init__(self):
                self.kind = kind
                self.calls = 0

            def execute(self, req):
                self.calls += 1
                return result

            def resolve_model_name(self, name):
                return (name or "").strip()

            def resolve_alias(self, a):
                return None

            def list_aliases(self):
                return []

            def validate_role_override(self, r, t):
                return []

        return _FakeBrain()

    def _run(self, tmp_path, monkeypatch, tmux_result):
        from istota.config import BrainConfig
        from istota.brain._types import BrainResult
        from istota.brain._fallback import reset_availability_breaker

        # The generalized fallback path routes not_found/usage_limit through the
        # process-global availability breaker; reset it so tests don't pollute
        # each other via a lingering open primary.
        reset_availability_breaker()

        config = _make_config(tmp_path)
        config.brain = BrainConfig(kind="tmux_claude")
        config.security.sandbox_enabled = False
        task = _make_task(source_type="cli")

        tmux_brain = self._fake_brain("tmux_claude", tmux_result)
        cc_brain = self._fake_brain(
            "claude_code", BrainResult(True, "headless answer", stop_reason="completed")
        )

        def fake_make_brain(bc):
            return tmux_brain if getattr(bc, "kind", "") == "tmux_claude" else cc_brain

        patches = _patch_executor() + [
            patch("istota.executor.make_brain", side_effect=fake_make_brain),
            # not_found opens the availability breaker → one operator alert; stub
            # it so the test doesn't touch the notification stack.
            patch("istota.notifications.send_notification", return_value=None),
        ]
        with contextmanager_chain(patches):
            success, result, _actions, _trace = execute_task(task, config, [])
        return success, result, tmux_brain, cc_brain

    def test_fallback_reruns_headless_once(self, tmp_path, monkeypatch):
        from istota.brain._types import BrainResult
        from istota.brain import tmux_claude
        tmux_claude.reset_circuit_breaker()
        success, result, tmux_brain, cc_brain = self._run(
            tmp_path, monkeypatch,
            BrainResult(False, "not ready", stop_reason="fallback"),
        )
        assert success is True
        assert result == "headless answer"
        assert tmux_brain.calls == 1
        assert cc_brain.calls == 1

    def test_not_found_also_falls_back(self, tmp_path, monkeypatch):
        from istota.brain._types import BrainResult
        from istota.brain import tmux_claude
        tmux_claude.reset_circuit_breaker()
        success, result, tmux_brain, cc_brain = self._run(
            tmp_path, monkeypatch,
            BrainResult(False, "tmux missing", stop_reason="not_found"),
        )
        assert success is True
        assert cc_brain.calls == 1

    def test_no_fallback_on_normal_error(self, tmp_path, monkeypatch):
        from istota.brain._types import BrainResult
        from istota.brain import tmux_claude
        tmux_claude.reset_circuit_breaker()
        success, result, tmux_brain, cc_brain = self._run(
            tmp_path, monkeypatch,
            BrainResult(False, "boom", stop_reason="error"),
        )
        # A normal error is the brain's own failure → no headless rerun.
        assert cc_brain.calls == 0
        assert success is False
