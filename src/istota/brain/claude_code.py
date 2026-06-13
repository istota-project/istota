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
import os
import re
import subprocess
import threading
import time
from pathlib import Path

from ._events import (
    ContextManagementEvent,
    ResultEvent,
    StreamEvent,
    TextDeltaEvent,
    TextEvent,
    ThinkingDeltaEvent,
    ThinkingEvent,
    ToolUseEvent,
    make_stream_parser,
)
from ._roles import get_role_overrides
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


def _is_root() -> bool:
    """True when the process runs as uid 0 (Unix). `claude` refuses
    --dangerously-skip-permissions as root unless IS_SANDBOX=1 is set. Shared by
    both the headless and tmux launch paths."""
    geteuid = getattr(os, "geteuid", None)
    return geteuid is not None and geteuid() == 0


# Flags already warned-about as unsupported, so the "dropped a flag" WARNING
# fires once per flag per process rather than every task. Module-global on
# purpose (the warning is operator-facing, not per-request).
_WARNED_UNSUPPORTED_FLAGS: set[str] = set()


def build_claude_cli_flags(
    req: BrainRequest, *, unsupported: frozenset[str] = frozenset()
) -> list[str]:
    """Build the `claude` CLI flags shared by both the headless (`-p`) and the
    interactive (tmux) launch paths.

    Covers the model / effort / tool / system-prompt flags both brains need; it
    deliberately does NOT add ``-p -`` or the ``--output-format stream-json``
    flags (headless-only) nor ``--dangerously-skip-permissions`` (which both
    brains append themselves) — each brain appends its own path-specific flags
    around this common core.

    ``unsupported`` names flags the *target* CLI surface rejects (the interactive
    TUI may not accept every ``-p`` flag; Stage 1 of the tmux production spec
    verifies which). A flag in this set is dropped from the argv and warned about
    once per process rather than passed through to a launch failure. The default
    (empty set) reproduces the headless argv exactly, so ``ClaudeCodeBrain``'s
    output is byte-for-byte unchanged.
    """
    flags: list[str] = []
    # Empty allowed_tools means text-only invocation (e.g. sleep cycle): skip the
    # tool flags entirely so claude's defaults stay out of the equation. The
    # prompt itself, plus the absence of --dangerously-skip-permissions, is what
    # keeps the call text-only.
    if req.allowed_tools:
        # Both brains run non-interactively with --dangerously-skip-permissions
        # (added per-brain), so the model gets its full default toolset and an
        # --allowedTools allowlist would only restrict it below that, blocking
        # tools we didn't think to enumerate. The bwrap sandbox + network proxy
        # are the security boundary, not an interactive permission prompt; Bash
        # is permitted anyway, which is effectively unrestricted inside the
        # sandbox. So we drop --allowedTools and rely on skip-permissions.
        #
        # We DO still explicitly deny the harness's built-in multi-agent
        # orchestration tools (Agent + Workflow): deny rules win even under
        # --dangerously-skip-permissions, so this keeps Istota orchestrating
        # through its own skills / subtasks rather than Claude Code's fan-out,
        # whose dozens-of-subagents cost profile we don't want a task reaching
        # for unprompted.
        #
        # Workflow had briefly been dropped from this list (ISSUE-110 follow-up)
        # because the old --allowedTools allowlist already excluded it — the only
        # reason to name it then was to suppress a harness auto-inject reminder
        # that stopped firing in 2.1.162. Now that the allowlist is gone (we run
        # with --dangerously-skip-permissions), the allowlist no longer
        # implicitly blocks Workflow, so it must be denied explicitly again.
        flags += ["--disallowedTools", "Agent", "Workflow"]

    def _add(flag: str, *values: str) -> None:
        if flag in unsupported:
            if flag not in _WARNED_UNSUPPORTED_FLAGS:
                _WARNED_UNSUPPORTED_FLAGS.add(flag)
                logger.warning("tmux_brain unsupported_flag flag=%s (dropped)", flag)
            return
        flags.extend([flag, *values])

    if req.model:
        _add("--model", req.model)
    if req.effort:
        _add("--effort", req.effort)
    if req.custom_system_prompt_path and req.custom_system_prompt_path.exists():
        _add("--system-prompt-file", str(req.custom_system_prompt_path))
    return flags


# ---------------------------------------------------------------------------
# Anthropic model namespace
#
# These tables describe the models *this* brain can run. A future
# OpenRouter / Anthropic-direct brain ships its own analogous tables in
# its own module; consumers never reach in here directly — they go through
# Brain.resolve_alias / Brain.resolve_model_name.
#
# Versioning: bare aliases like ``opus`` always resolve to a *specific*
# version constant (``OPUS = "claude-opus-4-8"``) so a model release can't
# silently re-route us. Prior versions get first-class constants
# (``OPUS_47``, ``OPUS_46``) only when there's a concrete reason to pin to
# them (e.g., production stability) — this is not meant to be exhaustive.
# ---------------------------------------------------------------------------

OPUS: str = "claude-opus-4-8"
OPUS_47: str = "claude-opus-4-7"
OPUS_46: str = "claude-opus-4-6"
SONNET: str = "claude-sonnet-4-6"
HAIKU: str = "claude-haiku-4-5"

# Provider aliases — `(model_id, effort)` pairs. ``effort=None`` means "let
# the model decide" (no ``--effort`` flag). Adding an alias here is the only
# place a new shortcut needs to be defined; every surface (``!model`` prefix,
# ``!help`` output, scheduled-job model overrides) reads from this table.
MODEL_ALIASES: dict[str, tuple[str | None, str | None]] = {
    "default":      (None, None),
    "opus":         (OPUS, None),
    "opus-high":    (OPUS, "high"),
    "opus-xhigh":   (OPUS, "xhigh"),
    "opus-max":     (OPUS, "max"),
    "opus-47":      (OPUS_47, None),
    "opus-47-high": (OPUS_47, "high"),
    "opus-46":      (OPUS_46, None),
    "opus-46-high": (OPUS_46, "high"),
    "sonnet":       (SONNET, None),
    "sonnet-high":  (SONNET, "high"),
    "haiku":        (HAIKU, None),
}

# Default role-target mapping for *this brain*. Operators override the
# target via [models.roles] TOML; the override RHS is resolved through
# MODEL_ALIASES so they can write provider-aware shortcuts like
# ``smart = "opus-46-high"`` without having to type the canonical ID.
DEFAULT_ROLE_TARGETS: dict[str, str] = {
    "fast":    HAIKU,
    "general": SONNET,
    "smart":   OPUS,
}


def _resolve_target(target: str) -> str:
    """Translate an override RHS through MODEL_ALIASES to a canonical ID.

    Operator wrote e.g. ``smart = "opus-46-high"``: this returns
    ``"claude-opus-4-6"``. Unknown strings pass through unchanged so raw
    canonical IDs (``"claude-opus-4-8"``) work as override targets too.
    """
    if not target:
        return target
    pair = MODEL_ALIASES.get(target.lower())
    if pair is not None and pair[0] is not None:
        return pair[0]
    return target


class ClaudeCodeBrain:
    """Brain that delegates to the `claude` CLI as a subprocess."""

    # --- Model resolution (Brain Protocol) ---------------------------------

    def resolve_alias(
        self, alias: str
    ) -> tuple[str | None, str | None] | None:
        """Resolve a `!model <alias>` to (model_id, effort).

        Roles win over provider aliases (operator override > default role
        target > MODEL_ALIASES). Returns None for unknown.
        """
        alias_lower = alias.lower()
        # 1. Operator-overridden role
        override = get_role_overrides().get(alias_lower)
        if override is not None:
            return (_resolve_target(override), None)
        # 2. Default role target
        if alias_lower in DEFAULT_ROLE_TARGETS:
            return (DEFAULT_ROLE_TARGETS[alias_lower], None)
        # 3. Provider alias
        return MODEL_ALIASES.get(alias_lower)

    def resolve_model_name(self, name: str | None) -> str:
        """Resolve any name to a canonical Anthropic model ID.

        Empty/None → ``""`` (caller falls back to brain default).
        Unknown → pass-through (raw IDs typed into config still work).
        """
        if not name:
            return ""
        resolved = self.resolve_alias(name)
        if resolved is not None and resolved[0] is not None:
            return resolved[0]
        return name

    def validate_role_override(self, role: str, target: str) -> list[str]:
        """Surface operator typos at load time.

        Two checks:
        1. Role name shadows a provider alias (e.g. ``[models.roles] opus = "haiku"``
           silently makes ``!model opus`` resolve to Haiku — almost always a typo).
        2. Override target is neither a known provider alias nor a canonical
           ``claude-*`` ID (it'll pass through to the CLI and fail at task time).
        """
        warnings: list[str] = []
        role_lower = role.lower()
        if role_lower in MODEL_ALIASES:
            warnings.append(
                f"role override {role!r} shadows the provider alias of the "
                f"same name; future `!model {role}` calls will resolve to "
                f"{target!r} instead of the built-in alias"
            )
        if target:
            target_lower = target.lower()
            looks_canonical = target.startswith("claude-")
            known_alias = target_lower in MODEL_ALIASES
            if not looks_canonical and not known_alias:
                warnings.append(
                    f"role override {role!r} target {target!r} is neither a "
                    f"canonical model id nor a known provider alias; tasks "
                    f"using this role will fail at execution time"
                )
        return warnings

    def list_aliases(self) -> list[tuple[str, str | None, str | None]]:
        """Merged alias table for display.

        Roles first (sorted, with operator overrides reflected), then
        provider aliases in declaration order. Used by the ``!models``
        Talk command and any other surface that wants "what does X
        resolve to right now".
        """
        out: list[tuple[str, str | None, str | None]] = []
        seen: set[str] = set()
        # Roles: defaults merged with overrides; overrides win.
        roles: dict[str, str] = dict(DEFAULT_ROLE_TARGETS)
        for role, override in get_role_overrides().items():
            roles[role] = _resolve_target(override)
        for role in sorted(roles):
            out.append((role, roles[role], None))
            seen.add(role)
        for alias, (model, effort) in MODEL_ALIASES.items():
            if alias in seen:
                continue
            out.append((alias, model, effort))
        return out

    # --- Execution (Brain Protocol) ----------------------------------------

    def execute(self, req: BrainRequest) -> BrainResult:
        try:
            # --dangerously-skip-permissions (added by _build_command for
            # tool-bearing tasks) is refused under root/sudo unless IS_SANDBOX=1
            # signals an external isolation boundary. That's the Docker
            # container-as-sandbox case (bwrap off, runs as root); on the
            # non-root prod VM service user the flag is allowed without it, so we
            # leave it unset. Mirrors the tmux brain's root handling.
            if req.allowed_tools and _is_root() and "IS_SANDBOX" not in req.env:
                req.env["IS_SANDBOX"] = "1"

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
        cmd = ["claude", "-p", "-"] + build_claude_cli_flags(req)
        if req.allowed_tools:
            # Run non-interactively without per-tool permission prompts (which
            # can't be answered in -p mode and would otherwise auto-deny tools).
            # The sandbox + network proxy are the boundary; an allowlist buys
            # nothing here. Skipped for text-only invocations (no tools), so
            # those stay tool-less. Mirrors the tmux brain.
            cmd += ["--dangerously-skip-permissions"]
        if req.streaming:
            # --include-partial-messages emits content deltas as they arrive so
            # the final answer streams token-by-token on stream surfaces instead
            # of landing as one whole block. Without it the CLI only emits
            # complete ``assistant`` messages, so the answer would dump all at
            # once (the whole-block TextEvent). Parsed in brain._events.
            cmd += [
                "--output-format", "stream-json", "--verbose",
                "--include-partial-messages",
            ]
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

        # Feed the prompt to stdin on a dedicated thread, started immediately
        # after spawn. The `claude` CLI aborts its stdin read after ~3s
        # ("no stdin data received in 3s, proceeding without it") and then
        # runs with an *empty* prompt — so prompt delivery must not be gated
        # behind anything slow. A synchronous write here would sit behind the
        # on_pid DB write below (which can block on the SQLite write lock under
        # daemon load); if that gap exceeds the CLI's stdin deadline the task
        # fails with "produced no output". Threading also avoids a deadlock
        # when the prompt exceeds the OS pipe buffer (~64KB) before any reader
        # has drained it. Mirrors subprocess.run(input=...)'s feeder thread,
        # which is why the non-streaming path was never affected.
        def _write_stdin() -> None:
            try:
                process.stdin.write(req.prompt)
                process.stdin.close()
            except (BrokenPipeError, OSError):
                pass  # process may have exited / closed stdin early

        stdin_thread = threading.Thread(target=_write_stdin, daemon=True)
        stdin_thread.start()

        # Notify caller of PID (used for !stop). The stdin write is already in
        # flight on its own thread, so a slow DB write here no longer delays
        # prompt delivery.
        if req.on_pid is not None:
            try:
                req.on_pid(process.pid)
            except Exception:
                logger.debug("on_pid callback raised", exc_info=True)

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
        # The model the CLI actually used. The stream-json ``system``/``init``
        # frame carries it (it reflects the resolved default when --model was
        # omitted), so this is more accurate than req.model for the default case.
        model_seen = ""
        parse_line = make_stream_parser()

        try:
            for line in process.stdout:
                raw_stdout_lines.append(line)
                if not model_seen and '"model"' in line:
                    try:
                        _d = json.loads(line)
                        if _d.get("type") == "system" and _d.get("model"):
                            model_seen = str(_d["model"])
                    except (json.JSONDecodeError, AttributeError):
                        pass
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
                # ThinkingEvent / TextDeltaEvent / ThinkingDeltaEvent are
                # intentionally NOT added to execution_trace: reasoning and the
                # token-level answer deltas are live-stream-only concerns
                # (``thinking`` / ``text_delta`` task events on stream surfaces),
                # never persisted in the trace, so result composition / history
                # reconstruction stay unchanged. The whole-block TextEvent above
                # is the trace's record of the answer text.

                if isinstance(
                    event,
                    (
                        ToolUseEvent,
                        TextEvent,
                        ThinkingEvent,
                        TextDeltaEvent,
                        ThinkingDeltaEvent,
                    ),
                ) and req.on_progress is not None:
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
            stdin_thread.join(timeout=5)
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
                    model_used=model_seen or req.model,
                )
            return BrainResult(
                success=False,
                result_text=result_text or stderr_output or "Unknown error",
                execution_trace=trace_json,
                stop_reason="error",
                model_used=model_seen or req.model,
            )

        if req.result_file and req.result_file.exists():
            output = req.result_file.read_text()
            if process.returncode == 0:
                return BrainResult(
                    success=True,
                    result_text=output.strip(),
                    actions_taken=actions_json,
                    execution_trace=trace_json,
                    model_used=model_seen or req.model,
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
