"""ClaudeCodeBrain — wraps the `claude` CLI subprocess.

Owns:
- Building the `claude -p - --allowedTools ...` command.
- Wrapping the command with bubblewrap (via the caller-supplied sandbox_wrap).
- Spawning the subprocess, writing the prompt over stdin.
- Parsing --output-format stream-json into StreamEvents and forwarding them.
- Auto-retry on transient Anthropic API errors (5xx/429).

Result reconciliation (CM-aware composition, malformed-output detection)
stays in the executor — both brains will produce result_text + execution_trace
and need the same downstream cleanup.
"""

import json
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

from ._events import (
    ContextManagementEvent,
    ResultEvent,
    StreamEvent,
    TextEvent,
    ToolUseEvent,
    make_stream_parser,
)
from ._types import BrainRequest, BrainResult

logger = logging.getLogger("istota.brain.claude_code")


# Pattern to detect Anthropic API errors in output
API_ERROR_PATTERN = re.compile(r"API Error: (\d{3}) (\{.*\})", re.DOTALL)

# Transient HTTP status codes that warrant retry
TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 529}  # 529 = overloaded

# Retry configuration for transient API errors
API_RETRY_MAX_ATTEMPTS = 3
API_RETRY_DELAY_SECONDS = 5


def parse_api_error(text: str) -> dict | None:
    """Parse API error string into structured data.

    Returns dict with status_code, message, request_id on match, or None.
    """
    match = API_ERROR_PATTERN.search(text)
    if not match:
        return None
    status_code = int(match.group(1))
    try:
        payload = json.loads(match.group(2))
        return {
            "status_code": status_code,
            "message": payload.get("error", {}).get("message", "Unknown error"),
            "request_id": payload.get("request_id"),
        }
    except json.JSONDecodeError:
        return {"status_code": status_code, "message": "Unknown error", "request_id": None}


def is_transient_api_error(text: str) -> bool:
    """Check if the error text represents a transient API error worth retrying."""
    parsed = parse_api_error(text)
    if not parsed:
        return False
    return parsed["status_code"] in TRANSIENT_STATUS_CODES or parsed["status_code"] == 429


class ClaudeCodeBrain:
    """Brain that delegates to the `claude` CLI as a subprocess."""

    def execute(self, req: BrainRequest) -> BrainResult:
        try:
            cmd = self._build_command(req)
            if req.sandbox_wrap is not None:
                cmd = req.sandbox_wrap(cmd)

            if req.streaming:
                return self._execute_streaming(cmd, req)
            return self._execute_simple(cmd, req)
        except FileNotFoundError:
            return BrainResult(
                success=False,
                result_text="Claude Code CLI not found. Is it installed and in PATH?",
                stop_reason="not_found",
            )
        except Exception as e:
            logger.exception("ClaudeCodeBrain.execute raised")
            return BrainResult(
                success=False,
                result_text=f"Execution error: {e}",
                stop_reason="error",
            )

    @staticmethod
    def _build_command(req: BrainRequest) -> list[str]:
        cmd = ["claude", "-p", "-", "--allowedTools"] + req.allowed_tools + [
            "--disallowedTools", "Agent",
        ]
        if req.model:
            cmd += ["--model", req.model]
        if req.effort:
            cmd += ["--effort", req.effort]
        if req.custom_system_prompt_path and req.custom_system_prompt_path.exists():
            cmd += ["--system-prompt-file", str(req.custom_system_prompt_path)]
        if req.streaming:
            cmd += ["--output-format", "stream-json", "--verbose"]
        return cmd

    # --- non-streaming path ---

    def _execute_simple(self, cmd: list[str], req: BrainRequest) -> BrainResult:
        """Subprocess.run with auto-retry on transient API errors."""
        last_error = ""
        for attempt in range(API_RETRY_MAX_ATTEMPTS):
            result = self._execute_simple_once(cmd, req)
            if result.success:
                return result

            if not is_transient_api_error(result.result_text):
                return result

            last_error = result.result_text
            parsed = parse_api_error(result.result_text)
            request_id = parsed.get("request_id", "unknown") if parsed else "unknown"

            if attempt < API_RETRY_MAX_ATTEMPTS - 1:
                logger.warning(
                    "Transient API error (attempt %d/%d, request_id=%s), retrying in %ds...",
                    attempt + 1, API_RETRY_MAX_ATTEMPTS, request_id, API_RETRY_DELAY_SECONDS,
                )
                time.sleep(API_RETRY_DELAY_SECONDS)
            else:
                logger.error(
                    "Transient API error persisted after %d attempts (request_id=%s)",
                    API_RETRY_MAX_ATTEMPTS, request_id,
                )

        return BrainResult(
            success=False,
            result_text=last_error,
            stop_reason="transient_api_error",
        )

    @staticmethod
    def _execute_simple_once(cmd: list[str], req: BrainRequest) -> BrainResult:
        result = subprocess.run(
            cmd,
            input=req.prompt,
            capture_output=True,
            text=True,
            timeout=req.timeout_seconds,
            cwd=str(req.cwd),
            env=req.env,
        )

        output = result.stdout.strip()

        if result.returncode == -9:
            return BrainResult(
                success=False,
                result_text="Claude Code was killed (likely out of memory)",
                stop_reason="oom",
            )

        if result.returncode == 0 and output:
            return BrainResult(success=True, result_text=output)
        if result.returncode == 0 and req.result_file and req.result_file.exists():
            return BrainResult(success=True, result_text=req.result_file.read_text().strip())
        if output:
            return BrainResult(success=False, result_text=output, stop_reason="error")
        if result.stderr.strip():
            return BrainResult(success=False, result_text=result.stderr.strip(), stop_reason="error")
        return BrainResult(
            success=False,
            result_text=f"Claude Code produced no output (rc={result.returncode})",
            stop_reason="error",
        )

    # --- streaming path ---

    def _execute_streaming(self, cmd: list[str], req: BrainRequest) -> BrainResult:
        """Popen + stream-json parsing with auto-retry on transient API errors."""
        last_error = ""
        last_trace = None

        for attempt in range(API_RETRY_MAX_ATTEMPTS):
            result = self._execute_streaming_once(cmd, req)

            if result.success:
                return result

            last_trace = result.execution_trace

            if not is_transient_api_error(result.result_text):
                return result

            last_error = result.result_text
            parsed = parse_api_error(result.result_text)
            request_id = parsed.get("request_id", "unknown") if parsed else "unknown"

            if attempt < API_RETRY_MAX_ATTEMPTS - 1:
                logger.warning(
                    "Transient API error (attempt %d/%d, request_id=%s), retrying in %ds...",
                    attempt + 1, API_RETRY_MAX_ATTEMPTS, request_id, API_RETRY_DELAY_SECONDS,
                )
                time.sleep(API_RETRY_DELAY_SECONDS)
            else:
                logger.error(
                    "Transient API error persisted after %d attempts (request_id=%s)",
                    API_RETRY_MAX_ATTEMPTS, request_id,
                )

        return BrainResult(
            success=False,
            result_text=last_error,
            execution_trace=last_trace,
            stop_reason="transient_api_error",
        )

    @staticmethod
    def _execute_streaming_once(cmd: list[str], req: BrainRequest) -> BrainResult:
        actions_descriptions: list[str] = []
        execution_trace: list[dict] = []
        stderr_lines: list[str] = []

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(req.cwd),
            env=req.env,
        )

        # Notify caller of PID (used for !stop) BEFORE stdin write so a stop
        # signal arriving in the stdin-write window finds a recorded PID.
        if req.on_pid is not None:
            try:
                req.on_pid(process.pid)
            except Exception:
                logger.debug("on_pid callback raised", exc_info=True)

        # Write prompt to stdin and close to signal EOF
        try:
            process.stdin.write(req.prompt)
            process.stdin.close()
        except BrokenPipeError:
            pass  # process may have exited early

        def _read_stderr() -> None:
            for line in process.stderr:
                stderr_lines.append(line)

        stderr_thread = threading.Thread(target=_read_stderr, daemon=True)
        stderr_thread.start()

        # Timeout via timer
        timed_out = threading.Event()

        def _kill() -> None:
            timed_out.set()
            process.kill()

        timer = threading.Timer(req.timeout_seconds, _kill)
        timer.start()

        final_result: ResultEvent | None = None
        raw_stdout_lines: list[str] = []
        cancelled = False
        parse_line = make_stream_parser()

        try:
            for line in process.stdout:
                raw_stdout_lines.append(line)
                event = parse_line(line)
                if event is None:
                    continue

                if isinstance(event, ResultEvent):
                    final_result = event
                elif isinstance(event, ContextManagementEvent):
                    execution_trace.append({"type": "cm_boundary"})
                    continue  # don't stream CM markers
                elif isinstance(event, ToolUseEvent):
                    actions_descriptions.append(event.description)
                    execution_trace.append({"type": "tool", "text": event.description})
                elif isinstance(event, TextEvent):
                    execution_trace.append({"type": "text", "text": event.text})

                if isinstance(event, (ToolUseEvent, TextEvent)) and req.on_progress is not None:
                    try:
                        req.on_progress(event)
                    except Exception:
                        logger.debug("on_progress raised", exc_info=True)

                # Cancellation poll between events
                if isinstance(event, (ToolUseEvent, TextEvent)) and req.cancel_check is not None:
                    try:
                        if req.cancel_check():
                            logger.info("Cancellation requested, killing subprocess")
                            process.kill()
                            cancelled = True
                            break
                    except Exception:
                        logger.debug("cancel_check raised", exc_info=True)

            process.wait()
            stderr_thread.join(timeout=5)
        finally:
            timer.cancel()

        actions_json = json.dumps(actions_descriptions) if actions_descriptions else None
        trace_json = json.dumps(execution_trace) if execution_trace else None

        # Final cancellation check — SIGTERM from !stop may kill the process
        # before the in-loop check runs.
        if not cancelled and req.cancel_check is not None:
            try:
                if req.cancel_check():
                    cancelled = True
            except Exception:
                pass

        if cancelled:
            return BrainResult(
                success=False,
                result_text="Cancelled by user",
                stop_reason="cancelled",
            )

        if timed_out.is_set():
            timeout_min = req.timeout_seconds // 60
            return BrainResult(
                success=False,
                result_text=f"Task execution timed out after {timeout_min} minutes",
                stop_reason="timeout",
            )

        if process.returncode == -9:
            return BrainResult(
                success=False,
                result_text="Claude Code was killed (likely out of memory)",
                stop_reason="oom",
            )

        stderr_output = "".join(stderr_lines).strip()

        # Extract result: prefer ResultEvent, fall back to result file, then stderr.
        if final_result is not None:
            result_text = final_result.text.strip()
            if final_result.success:
                return BrainResult(
                    success=True,
                    result_text=result_text,
                    actions_taken=actions_json,
                    execution_trace=trace_json,
                )
            return BrainResult(
                success=False,
                result_text=result_text or stderr_output or "Unknown error",
                execution_trace=trace_json,
                stop_reason="error",
            )

        if req.result_file and req.result_file.exists():
            output = req.result_file.read_text()
            if process.returncode == 0:
                return BrainResult(
                    success=True,
                    result_text=output.strip(),
                    actions_taken=actions_json,
                    execution_trace=trace_json,
                )
            return BrainResult(
                success=False,
                result_text=output.strip(),
                execution_trace=trace_json,
                stop_reason="error",
            )

        logger.warning(
            "No ResultEvent parsed from stream-json (rc=%s, stderr=%s, stdout_lines=%d)",
            process.returncode,
            stderr_output[:200] if stderr_output else "(empty)",
            len(raw_stdout_lines),
        )

        if stderr_output:
            return BrainResult(
                success=False, result_text=stderr_output,
                execution_trace=trace_json, stop_reason="error",
            )
        if raw_stdout_lines:
            return BrainResult(
                success=False,
                result_text=f"Stream parsing failed (rc={process.returncode}, {len(raw_stdout_lines)} lines)",
                execution_trace=trace_json,
                stop_reason="error",
            )
        return BrainResult(
            success=False,
            result_text=f"Claude Code produced no output (rc={process.returncode})",
            stop_reason="error",
        )
