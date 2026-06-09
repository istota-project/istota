"""TmuxClaudeBrain — drives the interactive `claude` TUI in a detached tmux
session, instead of the headless `claude -p` subprocess `ClaudeCodeBrain` uses.

Mechanism: spawn a detached tmux session, launch the interactive TUI, inject the
prompt via a tmux buffer, detect turn completion with a `Stop`-hook sentinel
file, and reconstruct the result + trace by parsing the session transcript JSONL.
It runs the same `claude` binary and uses the same auth as ClaudeCodeBrain, so it
delegates all four model-resolution methods to a composed ``ClaudeCodeBrain`` and
only implements ``execute``.

Production hardening (``Specs/Active/claude-tmux-production-readiness.md``):

- **Per-session hook isolation** — each session gets its own ``CLAUDE_CONFIG_DIR``
  (``<workdir>/config``) so two concurrent same-user tasks can't clobber a shared
  ``settings.json`` and cross-fire each other's Stop sentinel.
- **Fail-fast detection** — ``_wait_for_completion`` distinguishes done / cancel /
  error-marker / dead-pane / stall instead of always burning the full timeout; a
  transient API error retries a fresh session (≤3, 5s apart) without consuming a
  task attempt, matching ``ClaudeCodeBrain``.
- **Automatic fallback** — a launch-level failure returns ``stop_reason="fallback"``
  so the executor reruns the task once headless; a process-global circuit breaker
  trips after repeated launch failures (a CLI upgrade rewording a dialog) and
  short-circuits to fallback for a cooldown, with one operator alert.
- **Live streaming** — on stream surfaces a background ``_TranscriptTailer``
  forwards tool/text/thinking blocks to ``on_progress`` as they flush, instead of
  only at turn end (the Stop-time parse stays authoritative for the result/trace).

``claude_code`` remains the constructible fallback kind.
"""

import itertools
import json
import logging
import os
import shlex
import shutil
import subprocess
import threading
import time
from pathlib import Path

from ..agent.events import _describe_tool_use
from ._events import (
    ResultEvent,
    StreamEvent,
    TextEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from ._types import BrainRequest, BrainResult
from .claude_code import (
    API_RETRY_DELAY_SECONDS,
    API_RETRY_MAX_ATTEMPTS,
    ClaudeCodeBrain,
    build_claude_cli_flags,
    is_transient_api_error,
)

logger = logging.getLogger("istota.brain.tmux_claude")

# Wide, fixed pane geometry reduces TUI reflow noise if pane scraping is needed.
_PANE_WIDTH = 220
_PANE_HEIGHT = 50

# How long to wait for the REPL to become ready before injecting the prompt.
_READY_TIMEOUT_S = 30.0
_READY_POLL_S = 0.4
# Sentinel poll cadence while waiting for the Stop hook to fire.
_SENTINEL_POLL_S = 0.4
# After the Stop hook fires, the assistant's final turn may still be flushing to
# the transcript JSONL. Poll briefly for the final turn to land before parsing so
# the execution trace isn't truncated/empty (the result text is unaffected — it
# comes from the Stop payload). Best-effort: parse whatever exists after the
# budget rather than block.
_TRANSCRIPT_SETTLE_S = 3.0
_TRANSCRIPT_SETTLE_POLL_S = 0.15
# How long to poll for the early UserPromptSubmit/SessionStart sentinel that
# carries transcript_path, before falling back to globbing the project dir.
_STARTED_SENTINEL_WAIT_S = 5.0

# Per-tmux-subprocess timeout default — a wedged tmux server must not hang the
# brain. Overridable via [brain.tmux] tmux_command_timeout.
_TMUX_COMMAND_TIMEOUT_S = 10.0

# Circuit-breaker defaults (§4). Overridable via [brain.tmux].
_FALLBACK_TRIP_THRESHOLD = 5
_FALLBACK_COOLDOWN_S = 300.0

# Pane substrings that mean the REPL is ready to accept a prompt. Pinned to
# claude 2.1.168 during the Stage 1 spike; heuristic, may need updating on CLI
# upgrades.
_READY_MARKERS = ("bypass permissions on", "? for shortcuts", "for shortcuts")
# Pane substrings for the first-access workspace trust dialog —
# --dangerously-skip-permissions does NOT bypass it (Stage 1 finding).
# Option 1 ("Yes, I trust this folder") is pre-selected → a bare Enter accepts.
_TRUST_MARKERS = ("trust this folder", "Is this a project you")
# Pane substrings for the "Bypass Permissions mode" warning. It appears under
# the bwrap sandbox (Stage 3′ finding) because bwrap tmpfs'es ~/.claude, so the
# CLI never remembers a prior acceptance — unlike a persistent ~/.claude (mac),
# where it's a one-time prompt. Here option 1 is "No, exit" (pre-selected) and
# option 2 is "Yes, I accept", so we must actively select 2 — a bare Enter would
# EXIT claude.
_BYPASS_WARNING_MARKER = "Bypass Permissions mode"
_BYPASS_ACCEPT_MARKER = "Yes, I accept"
# Pane substrings that mean the turn cannot make progress (fail fast, §3).
_ERROR_MARKERS = ("API Error", "session limit reached", "Context low")

# Flags the interactive TUI rejects vs the headless `-p` path. Stage 1 verifies
# this against the pinned CLI; the prototype passed --effort and
# --system-prompt-file successfully, so the default gap set is empty. Populate
# here (or override behavior via build_claude_cli_flags) if a CLI version starts
# rejecting one.
_TMUX_UNSUPPORTED_FLAGS: frozenset[str] = frozenset()

# Per-process session-name counter (BrainRequest carries no task id by default).
_SESSION_COUNTER = itertools.count(1)

# Max pane text length captured into a log line on error/timeout.
_PANE_LOG_CHARS = 2000


def parse_transcript(path: Path) -> list[StreamEvent]:
    """Reconstruct StreamEvents from an interactive `claude` transcript JSONL.

    The interactive transcript is an append-only session log — one JSON record
    per line, each with a top-level ``type`` (``assistant`` / ``user`` /
    ``system`` / ``attachment`` / ``mode`` / …). Unlike ``-p``'s stream-json it
    carries **no** terminal ``result`` record, so the final answer is
    synthesized from the last ``end_turn`` assistant turn's text blocks.

    Returns the ordered events with a terminal ``ResultEvent`` appended:
    ``ToolUseEvent`` per ``tool_use`` block, ``TextEvent`` per ``text`` block,
    ``ThinkingEvent`` per ``thinking`` block (document order within each
    assistant record), then ``ResultEvent(success, text)``. Non-assistant
    records (the user prompt, tool results, system notices) contribute no
    events — they exist only as turn structure.

    Malformed / blank lines are skipped (defensive: a partially-flushed
    transcript at sentinel time must not crash the parse).
    """
    events: list[StreamEvent] = []
    # Track the text of the last assistant turn that ended the conversation
    # (stop_reason == "end_turn"); that is the final answer. Fall back to the
    # last assistant text seen if no end_turn turn exists (degenerate session).
    final_answer: str | None = None
    last_text_any: str | None = None
    saw_assistant = False
    seen_tool_ids: set[str] = set()

    for raw in _iter_records(path):
        if raw.get("type") != "assistant":
            continue
        message = raw.get("message")
        if not isinstance(message, dict):
            continue
        saw_assistant = True
        content = message.get("content")
        if not isinstance(content, list):
            continue

        turn_text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "tool_use":
                block_id = block.get("id", "")
                if block_id and block_id in seen_tool_ids:
                    continue
                if block_id:
                    seen_tool_ids.add(block_id)
                name = block.get("name", "")
                desc = _describe_tool_use(name, block.get("input", {}))
                events.append(
                    ToolUseEvent(tool_name=name, description=desc, tool_call_id=block_id)
                )
            elif btype == "text":
                text = (block.get("text") or "").strip()
                if not text:
                    continue
                events.append(TextEvent(text=text))
                turn_text_parts.append(text)
            elif btype == "thinking":
                think = (block.get("thinking") or "").strip()
                if not think:
                    continue
                events.append(ThinkingEvent(text=think))

        if turn_text_parts:
            joined = "\n".join(turn_text_parts)
            last_text_any = joined
            if message.get("stop_reason") == "end_turn":
                final_answer = joined

    if final_answer is not None:
        result_text = final_answer
    elif last_text_any is not None:
        result_text = last_text_any
    else:
        result_text = ""

    # Success = we saw at least one assistant turn. An empty transcript (no
    # assistant records at all) means the turn never produced output.
    events.append(ResultEvent(success=saw_assistant, text=result_text))
    return events


def _transcript_has_final_turn(path: Path) -> bool:
    """True if the transcript's last assistant record ended the turn
    (``stop_reason == "end_turn"``).

    This is the structural "the final turn is fully written" signal used to
    settle the post-Stop flush race. The tmux brain runs one prompt per session,
    so the only ``end_turn`` record is this turn's — when it appears, the turn
    (and everything before it) has flushed. Returns False on a missing/empty/
    partially-flushed transcript."""
    last_assistant = None
    for rec in _iter_records(path):
        if rec.get("type") == "assistant":
            last_assistant = rec
    if last_assistant is None:
        return False
    message = last_assistant.get("message")
    if not isinstance(message, dict):
        return False
    return message.get("stop_reason") == "end_turn"


def _iter_records(path: Path):
    """Yield parsed JSON records from a JSONL transcript, skipping blanks and
    malformed lines."""
    try:
        text = Path(path).read_text()
    except OSError as e:
        logger.warning("parse_transcript: cannot read %s: %s", path, e)
        return
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            logger.debug("parse_transcript: skipping non-JSON line: %s", line[:100])
            continue


# ---------------------------------------------------------------------------
# Circuit breaker (§4) — process-global launch-failure tracking.
#
# Tracks consecutive launch-level failures (ready timeout, markers never
# matched, tmux missing, rejected flag) across tasks. After a threshold the
# circuit "opens": execute() short-circuits straight to fallback for a cooldown
# without trying tmux at all, and one operator alert is armed. Any tmux success
# resets it. State is per-process (the daemon); a restart resets it, which is
# also when a fixed CLI version would be picked up.
# ---------------------------------------------------------------------------


class _CircuitBreaker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.consecutive = 0
        self.opened_at: float | None = None  # monotonic when opened, else None
        self._alert_pending = False

    def should_skip(self, cooldown: float) -> bool:
        """True while the circuit is open and the cooldown hasn't elapsed."""
        with self._lock:
            if self.opened_at is None:
                return False
            return (time.monotonic() - self.opened_at) < cooldown

    def record_launch_failure(self, threshold: int) -> bool:
        """Count a launch-level failure. Returns True iff this failure just
        opened the circuit (so the caller arms exactly one alert)."""
        with self._lock:
            self.consecutive += 1
            just_opened = False
            if self.consecutive >= threshold:
                if self.opened_at is None:
                    just_opened = True
                    self._alert_pending = True
                # (Re)arm the cooldown window from now, whether newly opened or a
                # post-cooldown probe that failed again.
                self.opened_at = time.monotonic()
            return just_opened

    def record_success(self) -> bool:
        """Reset on a tmux success. Returns True iff the circuit had been open."""
        with self._lock:
            was_open = self.opened_at is not None
            self.consecutive = 0
            self.opened_at = None
            self._alert_pending = False
            return was_open

    def pop_alert(self) -> bool:
        """Return and clear the pending-alert flag (executor fires the alert)."""
        with self._lock:
            v = self._alert_pending
            self._alert_pending = False
            return v

    def reset(self) -> None:
        with self._lock:
            self.consecutive = 0
            self.opened_at = None
            self._alert_pending = False


_BREAKER = _CircuitBreaker()

# One-shot per-process CLI-version check (§6): warn once if the installed
# `claude` version doesn't match the pin, since the readiness/dialog markers are
# pinned to a CLI version.
_VERSION_CHECKED = False


def _warn_cli_version_once(pin: str) -> None:
    global _VERSION_CHECKED
    if _VERSION_CHECKED or not pin:
        return
    _VERSION_CHECKED = True
    if shutil.which("claude") is None:
        return
    try:
        out = subprocess.run(
            ["claude", "--version"], capture_output=True, text=True,
            check=False, timeout=10,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return
    if pin not in out:
        logger.warning(
            "tmux_brain cli_version_mismatch installed=%r pinned=%r — readiness/"
            "dialog markers may be stale; update [brain.tmux] markers if tasks "
            "start timing out at readiness",
            out, pin,
        )


def consume_circuit_open_alert() -> bool:
    """Executor-facing: True exactly once after the circuit opens, so the
    executor sends a single operator alert. The brain itself has no Config, so
    notification dispatch lives in the executor's fallback branch."""
    return _BREAKER.pop_alert()


def reset_circuit_breaker() -> None:
    """Test/teardown helper — clear process-global breaker state."""
    _BREAKER.reset()


class _Params:
    """Resolved per-instance knobs: read from the optional TmuxBrainConfig when
    present, else the module-constant defaults (so ``TmuxClaudeBrain()`` with no
    config keeps the prototype's exact behavior)."""

    def __init__(self, config) -> None:
        def g(name, default):
            return getattr(config, name, default) if config is not None else default

        self.fallback_trip_threshold = int(g("fallback_trip_threshold", _FALLBACK_TRIP_THRESHOLD))
        self.fallback_cooldown_seconds = float(g("fallback_cooldown_seconds", _FALLBACK_COOLDOWN_S))
        self.ready_timeout_seconds = float(g("ready_timeout_seconds", _READY_TIMEOUT_S))
        self.tmux_command_timeout = float(g("tmux_command_timeout", _TMUX_COMMAND_TIMEOUT_S))
        self.cli_version_pin = str(g("cli_version_pin", "2.1.168"))
        self.ready_markers = tuple(g("ready_markers", _READY_MARKERS))
        self.trust_markers = tuple(g("trust_markers", _TRUST_MARKERS))
        self.bypass_warning_marker = str(g("bypass_warning_marker", _BYPASS_WARNING_MARKER))
        self.bypass_accept_marker = str(g("bypass_accept_marker", _BYPASS_ACCEPT_MARKER))
        self.error_markers = tuple(g("error_markers", _ERROR_MARKERS))


class _TranscriptTailer(threading.Thread):
    """Background thread that tails an interactive transcript JSONL *during* the
    turn and forwards each new ``tool_use`` / ``text`` / ``thinking`` block to
    ``on_progress`` as it lands (§10). This restores event/block-level live
    streaming on stream surfaces, where the prototype only delivered whole-turn
    at Stop.

    Progress-only: the Stop-time parse (``_parse_transcript_settled``) remains
    the record of truth for the persisted result + trace, so a tailer that
    missed or double-emitted a block cannot corrupt the result. Exceptions are
    caught and logged, never propagated — a streaming glitch must not fail the
    task.
    """

    def __init__(self, path: Path, on_progress) -> None:
        super().__init__(daemon=True, name="tmux-transcript-tailer")
        self._path = Path(path)
        self._on_progress = on_progress
        self._stop = threading.Event()
        # Dedup: tool_use by id; text/thinking by (record_index, block_index)
        # so a partially-flushed-then-rewritten line isn't emitted twice.
        self._seen_tool_ids: set[str] = set()
        self._seen_blocks: set[tuple[int, int]] = set()

    def stop(self) -> None:
        self._stop.set()

    def run(self) -> None:
        try:
            while not self._stop.is_set():
                self._drain_once()
                if self._stop.wait(_SENTINEL_POLL_S):
                    break
            # Final drain after the stop signal so trailing blocks aren't lost.
            self._drain_once()
        except Exception:  # never let a streaming glitch escape
            logger.debug("transcript tailer raised", exc_info=True)

    def _drain_once(self) -> None:
        for rec_idx, raw in enumerate(_iter_records(self._path)):
            if raw.get("type") != "assistant":
                continue
            message = raw.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for blk_idx, block in enumerate(content):
                if not isinstance(block, dict):
                    continue
                self._emit_block(rec_idx, blk_idx, block)

    def _emit_block(self, rec_idx: int, blk_idx: int, block: dict) -> None:
        btype = block.get("type")
        ev: StreamEvent | None = None
        if btype == "tool_use":
            block_id = block.get("id", "")
            if block_id and block_id in self._seen_tool_ids:
                return
            if block_id:
                self._seen_tool_ids.add(block_id)
            name = block.get("name", "")
            desc = _describe_tool_use(name, block.get("input", {}))
            ev = ToolUseEvent(tool_name=name, description=desc, tool_call_id=block_id)
        elif btype == "text":
            key = (rec_idx, blk_idx)
            if key in self._seen_blocks:
                return
            text = (block.get("text") or "").strip()
            if not text:
                return
            self._seen_blocks.add(key)
            ev = TextEvent(text=text)
        elif btype == "thinking":
            key = (rec_idx, blk_idx)
            if key in self._seen_blocks:
                return
            think = (block.get("thinking") or "").strip()
            if not think:
                return
            self._seen_blocks.add(key)
            ev = ThinkingEvent(text=think)
        if ev is not None:
            try:
                self._on_progress(ev)
            except Exception:
                logger.debug("tailer on_progress raised", exc_info=True)


class TmuxClaudeBrain:
    """Brain that drives the interactive `claude` TUI inside a tmux session.

    Model resolution is delegated to an internal ``ClaudeCodeBrain``: this brain
    runs the same `claude` CLI binary against the same Anthropic model namespace,
    so duplicating ``MODEL_ALIASES`` / ``DEFAULT_ROLE_TARGETS`` would only invite
    drift. Only ``execute`` is genuinely new.
    """

    def __init__(self, config=None) -> None:
        # Composed, not inherited: we forward the four resolution methods and
        # own execute. The CLI brain holds no per-instance state, so a fresh
        # one here is free.
        self._cli = ClaudeCodeBrain()
        self._p = _Params(config)
        _warn_cli_version_once(self._p.cli_version_pin)

    # --- Model resolution (delegated to ClaudeCodeBrain) -------------------

    def resolve_alias(self, alias):
        return self._cli.resolve_alias(alias)

    def resolve_model_name(self, name):
        return self._cli.resolve_model_name(name)

    def list_aliases(self):
        return self._cli.list_aliases()

    def validate_role_override(self, role, target):
        return self._cli.validate_role_override(role, target)

    # --- Execution --------------------------------------------------------

    def execute(self, req: BrainRequest) -> BrainResult:
        # One-shot cleanup of any legacy shared hook a prior prototype build left
        # behind (§2) — best-effort, never fatal.
        self._cleanup_legacy_hook(req)

        if shutil.which("tmux") is None:
            # On a full switch a missing-tmux host would otherwise fail *every*
            # task. Feed the breaker and signal fallback so the instance degrades
            # to headless (loudly) instead. stop_reason stays the clear
            # "not_found" diagnostic; the executor reruns headless on it too.
            logger.error("tmux_brain not_found: tmux missing on PATH")
            _BREAKER.record_launch_failure(self._p.fallback_trip_threshold)
            return BrainResult(
                success=False,
                result_text="tmux not found. The tmux_claude brain needs tmux on PATH.",
                stop_reason="not_found",
            )

        # Circuit open → skip tmux entirely for the cooldown, straight to fallback.
        if _BREAKER.should_skip(self._p.fallback_cooldown_seconds):
            logger.error("tmux_brain circuit_open: short-circuiting to fallback")
            return BrainResult(
                success=False,
                result_text="tmux brain circuit open; falling back to claude_code",
                stop_reason="fallback",
            )

        # Transient-API retry loop around the whole session lifecycle (§3): a
        # fresh session per retry, ≤ API_RETRY_MAX_ATTEMPTS, API_RETRY_DELAY_SECONDS
        # apart, NOT counting against the task's attempt_count — identical contract
        # to ClaudeCodeBrain.
        last_result: BrainResult | None = None
        for attempt in range(API_RETRY_MAX_ATTEMPTS):
            result, retryable = self._run_session(req, attempt)
            last_result = result
            if result.success:
                _BREAKER.record_success()
                return result
            if result.stop_reason == "fallback":
                _BREAKER.record_launch_failure(self._p.fallback_trip_threshold)
                return result
            if retryable and attempt < API_RETRY_MAX_ATTEMPTS - 1:
                logger.warning(
                    "tmux_brain transient error (attempt %d/%d); retrying fresh session in %ds",
                    attempt + 1, API_RETRY_MAX_ATTEMPTS, API_RETRY_DELAY_SECONDS,
                )
                time.sleep(API_RETRY_DELAY_SECONDS)
                continue
            return result
        return last_result  # exhausted retries

    def _run_session(self, req: BrainRequest, attempt: int) -> tuple[BrainResult, bool]:
        """Run one tmux session lifecycle. Returns (result, retryable) where
        retryable=True means a transient API error worth a fresh-session retry."""
        session = req.session_label or f"istota-tmux-{os.getpid()}-{next(_SESSION_COUNTER)}"
        if attempt > 0:
            session = f"{session}-r{attempt}"

        # Base everything in the sandbox-shared RW region. Under bwrap the Stop
        # hook runs *inside* the sandbox and writes the sentinel; the brain reads
        # it from *outside*. Only ISTOTA_DEFERRED_DIR (= user_temp_dir) is
        # RW-bound at the same path inside and out AND is bwrap's --chdir target,
        # so the sentinel, the prompt file, and the per-session config dir must
        # live there. A private mkdtemp in /tmp would land on the sandbox's own
        # tmpfs — invisible to the brain. Off-sandbox (mac/dev/Docker)
        # ISTOTA_DEFERRED_DIR may be unset; fall back to req.cwd.
        base_dir = Path(req.env.get("ISTOTA_DEFERRED_DIR") or req.cwd)
        workdir = base_dir / f".tmux-{session}"
        sentinel = workdir / "stop.json"
        started_sentinel = workdir / "started.json"
        prompt_file = workdir / "prompt.txt"
        # Per-session CLAUDE_CONFIG_DIR — the clobber fix (§2). cwd-independent, so
        # it sidesteps the fixed bwrap --chdir target with no executor change.
        config_dir = workdir / "config"

        ready_ms = 0
        wait_ms = 0
        tools = 0
        outcome = "error"
        tailer: _TranscriptTailer | None = None
        try:
            workdir.mkdir(parents=True, exist_ok=True)
            config_dir.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text(req.prompt)
            self._write_hooks(config_dir, sentinel, started_sentinel)

            env = dict(req.env)
            env["CLAUDE_CONFIG_DIR"] = str(config_dir)
            self._new_session(session, env)
            self._launch_claude(session, req, base_dir)

            ready_t0 = time.monotonic()
            ready_deadline = ready_t0 + min(self._p.ready_timeout_seconds, req.timeout_seconds)
            if not self._wait_ready(session, ready_deadline):
                pane = self._capture(session)[:_PANE_LOG_CHARS]
                logger.error(
                    "tmux_brain ready_timeout session=%s pane=%r", session, pane,
                )
                self._kill(session)
                outcome = "fallback"
                return (
                    BrainResult(
                        success=False,
                        result_text="Interactive claude REPL never became ready",
                        stop_reason="fallback",
                    ),
                    False,
                )
            ready_ms = int((time.monotonic() - ready_t0) * 1000)

            # Report the pane pid (best-effort) before the long wait so !stop
            # has something to target. Session-kill is the real cancel path.
            if req.on_pid is not None:
                pid = self._pane_pid(session)
                if pid is not None:
                    try:
                        req.on_pid(pid)
                    except Exception:
                        logger.debug("on_pid callback raised", exc_info=True)

            self._inject_prompt(session, prompt_file)

            # Stream surfaces: start the live tailer once we know the transcript
            # path (§10). Push/non-streaming tasks skip it (no behavior change).
            if req.streaming and req.on_progress is not None:
                tpath = self._learn_transcript_path(started_sentinel, base_dir)
                if tpath is not None:
                    tailer = _TranscriptTailer(tpath, req.on_progress)
                    tailer.start()

            wait_t0 = time.monotonic()
            wait_deadline = wait_t0 + req.timeout_seconds
            status, pane = self._wait_for_completion(
                session, sentinel, wait_deadline, req.cancel_check
            )
            wait_ms = int((time.monotonic() - wait_t0) * 1000)
            self._kill(session)

            if status == "cancelled":
                outcome = "cancelled"
                return (
                    BrainResult(
                        success=False, result_text="Cancelled by user",
                        stop_reason="cancelled",
                    ),
                    False,
                )
            if status == "error":
                # Classify against the same rule ClaudeCodeBrain uses. A transient
                # API error is retryable (fresh session); anything else fails to
                # the normal task-retry path.
                retryable = is_transient_api_error(pane)
                logger.warning(
                    "tmux_brain error session=%s transient=%s pane=%r",
                    session, retryable, pane[:_PANE_LOG_CHARS],
                )
                outcome = "error"
                return (
                    BrainResult(
                        success=False,
                        result_text="claude reported an error mid-turn",
                        stop_reason="error",
                    ),
                    retryable,
                )
            if status != "done":
                outcome = "timeout"
                timeout_min = req.timeout_seconds // 60
                logger.warning(
                    "tmux_brain timeout session=%s pane=%r",
                    session, pane[:_PANE_LOG_CHARS],
                )
                return (
                    BrainResult(
                        success=False,
                        result_text=f"Task execution timed out after {timeout_min} minutes",
                        stop_reason="timeout",
                    ),
                    False,
                )

            # done — the tailer already forwarded progress events on stream
            # surfaces; tell _build_result not to re-forward them (avoid double
            # emission). On push/non-streaming, _build_result forwards as before.
            result = self._build_result(sentinel, req, forward_progress=tailer is None)
            tools = self._count_tools(result)
            outcome = "done" if result.success else "error"
            return (result, False)
        except FileNotFoundError as e:
            # tmux disappeared mid-run, or a helper hit a missing binary.
            outcome = "not_found"
            return (
                BrainResult(
                    success=False, result_text=f"tmux/claude not found: {e}",
                    stop_reason="not_found",
                ),
                False,
            )
        except Exception as e:
            logger.exception("TmuxClaudeBrain._run_session raised")
            self._kill(session)
            outcome = "error"
            return (
                BrainResult(
                    success=False, result_text=f"Execution error: {e}", stop_reason="error",
                ),
                False,
            )
        finally:
            if tailer is not None:
                tailer.stop()
                tailer.join(timeout=2.0)
            shutil.rmtree(workdir, ignore_errors=True)
            dialogs = self._last_dialogs
            logger.info(
                "tmux_brain session=%s outcome=%s ready_ms=%d wait_ms=%d "
                "dialogs=%s tools=%d retries=%d",
                session, outcome, ready_ms, wait_ms,
                ",".join(dialogs) if dialogs else "-", tools, attempt,
            )

    # --- Result assembly --------------------------------------------------

    @staticmethod
    def _count_tools(result: BrainResult) -> int:
        if not result.actions_taken:
            return 0
        try:
            return len(json.loads(result.actions_taken))
        except (json.JSONDecodeError, TypeError):
            return 0

    def _parse_transcript_settled(self, path: Path) -> list[StreamEvent]:
        """Parse the transcript once its final turn has flushed.

        The Stop hook fires the sentinel at turn end, but the assistant's final
        record may still be flushing to the JSONL — reading immediately can yield
        a truncated or empty trace (the result text is unaffected; it comes from
        the Stop payload). Poll the structural completion signal for a bounded
        budget, then parse. Falls through to a best-effort parse if the signal
        never appears (e.g. a session that ended on a tool_use turn) so the trace
        degrades rather than blocks."""
        deadline = time.monotonic() + _TRANSCRIPT_SETTLE_S
        while not _transcript_has_final_turn(path):
            if time.monotonic() >= deadline:
                logger.debug("transcript did not settle within budget: %s", path)
                break
            time.sleep(_TRANSCRIPT_SETTLE_POLL_S)
        return parse_transcript(path)

    def _build_result(
        self, sentinel: Path, req: BrainRequest, *, forward_progress: bool = True
    ) -> BrainResult:
        """Read the Stop-hook payload + transcript, emit progress events, and
        compose the BrainResult with the same actions/trace shapes ClaudeCodeBrain
        produces (so downstream DB writes are unchanged).

        ``forward_progress`` is False when a live tailer already forwarded the
        turn's events to ``on_progress`` (stream surfaces) — re-forwarding here
        would double-emit. It stays True for push/non-streaming tasks."""
        try:
            payload = json.loads(sentinel.read_text())
        except (OSError, json.JSONDecodeError) as e:
            return BrainResult(
                success=False,
                result_text=f"Could not read Stop-hook sentinel: {e}",
                stop_reason="error",
            )

        transcript_path = payload.get("transcript_path")
        last_msg = payload.get("last_assistant_message")

        events: list[StreamEvent] = []
        if transcript_path:
            events = self._parse_transcript_settled(Path(transcript_path))

        # Forward whole-turn events to on_progress (no token-level streaming on
        # this path — the Stop hook fires at turn end). ResultEvent is the return
        # value, not a progress event, so it is never forwarded. Skipped when a
        # live tailer already forwarded these events.
        if forward_progress and req.on_progress is not None:
            for ev in events:
                if isinstance(ev, (ToolUseEvent, TextEvent, ThinkingEvent)):
                    try:
                        req.on_progress(ev)
                    except Exception:
                        logger.debug("on_progress raised", exc_info=True)

        actions: list[str] = []
        trace: list[dict] = []
        terminal_text = ""
        for ev in events:
            if isinstance(ev, ToolUseEvent):
                actions.append(ev.description)
                trace.append({"type": "tool", "text": ev.description})
            elif isinstance(ev, TextEvent):
                trace.append({"type": "text", "text": ev.text})
            elif isinstance(ev, ResultEvent):
                terminal_text = ev.text

        # last_assistant_message from the Stop payload is canonical; fall back to
        # the transcript-derived terminal text when the payload omits it.
        result_text = last_msg if last_msg else terminal_text

        return BrainResult(
            success=True,
            result_text=result_text,
            actions_taken=json.dumps(actions) if actions else None,
            execution_trace=json.dumps(trace) if trace else None,
            stop_reason="completed",
        )

    def _learn_transcript_path(self, started_sentinel: Path, base_dir: Path) -> Path | None:
        """Read the early UserPromptSubmit/SessionStart sentinel for the
        transcript path so the tailer can start mid-turn (§10). Polls briefly for
        the sentinel; on miss, falls back to globbing the project transcript dir
        for the newest JSONL — a documented best-effort path."""
        deadline = time.monotonic() + _STARTED_SENTINEL_WAIT_S
        while time.monotonic() < deadline:
            if started_sentinel.exists():
                try:
                    payload = json.loads(started_sentinel.read_text())
                    tpath = payload.get("transcript_path")
                    if tpath:
                        return Path(tpath)
                except (OSError, json.JSONDecodeError):
                    pass
            time.sleep(_SENTINEL_POLL_S)
        # Glob fallback: newest *.jsonl under ~/.claude/projects (best-effort).
        try:
            projects = Path.home() / ".claude" / "projects"
            candidates = list(projects.rglob("*.jsonl"))
            if candidates:
                newest = max(candidates, key=lambda p: p.stat().st_mtime)
                logger.debug("tmux_brain transcript glob fallback: %s", newest)
                return newest
        except OSError:
            pass
        logger.debug("tmux_brain: could not learn transcript path for live tailing")
        return None

    # --- tmux primitives (mocked in unit tests) ---------------------------

    @staticmethod
    def _write_hooks(config_dir: Path, sentinel: Path, started_sentinel: Path) -> None:
        """Write a per-session settings.json into CLAUDE_CONFIG_DIR declaring two
        hooks (§2/§10):

        - ``Stop`` → ``cat > <sentinel>``: existence = turn finished, contents =
          the stdin payload (transcript_path + last_assistant_message).
        - ``UserPromptSubmit`` + ``SessionStart`` → ``cat > <started_sentinel>``:
          fire early enough to learn ``transcript_path`` for live tailing. Both
          are declared; whichever fires (and carries the path) wins. SessionStart
          is the fallback if UserPromptSubmit lacks transcript_path on the CLI.

        Living in the per-session CLAUDE_CONFIG_DIR (not a shared project
        ``.claude/``) is the clobber fix: concurrent same-user tasks get distinct
        config dirs, so their Stop hooks can't cross-fire."""
        config_dir.mkdir(parents=True, exist_ok=True)
        stop_cmd = f"cat > {shlex.quote(str(sentinel))}"
        start_cmd = f"cat > {shlex.quote(str(started_sentinel))}"
        settings = {
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": stop_cmd}]}],
                "UserPromptSubmit": [{"hooks": [{"type": "command", "command": start_cmd}]}],
                "SessionStart": [{"hooks": [{"type": "command", "command": start_cmd}]}],
            }
        }
        (config_dir / "settings.json").write_text(json.dumps(settings))

    @staticmethod
    def _cleanup_legacy_hook(req: BrainRequest) -> None:
        """Best-effort one-shot removal of a shared ``base_dir/.claude`` the
        prototype wrote (§2). Old deploys must not leave a shared hook behind that
        a non-config-dir CLI could still discover from cwd."""
        try:
            base_dir = Path(req.env.get("ISTOTA_DEFERRED_DIR") or req.cwd)
            legacy = base_dir / ".claude" / "settings.json"
            if legacy.exists():
                legacy.unlink()
                logger.debug("tmux_brain: removed legacy shared hook %s", legacy)
        except OSError:
            pass

    def _tmux(self, *args: str) -> subprocess.CompletedProcess:
        try:
            return subprocess.run(
                ["tmux", *args],
                capture_output=True,
                text=True,
                check=False,
                timeout=self._p.tmux_command_timeout,
            )
        except subprocess.TimeoutExpired:
            logger.warning("tmux_brain: tmux %s timed out", args[0] if args else "")
            cp: subprocess.CompletedProcess = subprocess.CompletedProcess(
                ["tmux", *args], returncode=1, stdout="", stderr="tmux timeout"
            )
            return cp

    def _new_session(self, name: str, env: dict[str, str]) -> None:
        # -e passes env into the session so the detached pane doesn't inherit a
        # stale tmux-server environment (the Stage 1 auth gotcha): the OAuth
        # token must reach the pane or claude runs unauthenticated.
        args = [
            "new-session", "-d", "-s", name,
            "-x", str(_PANE_WIDTH), "-y", str(_PANE_HEIGHT),
        ]
        for k, v in env.items():
            args += ["-e", f"{k}={v}"]
        self._tmux(*args)

    def _launch_claude(self, name: str, req: BrainRequest, launch_cwd: Path) -> None:
        parts = ["claude"] + build_claude_cli_flags(req, unsupported=_TMUX_UNSUPPORTED_FLAGS)
        parts += ["--dangerously-skip-permissions"]

        # Sandbox the *claude* process, not tmux. The brain + tmux server run
        # unsandboxed in the daemon; sandbox_wrap turns the claude argv into a
        # `bwrap … -- claude …` argv that the pane runs. No nesting: tmux itself
        # is never wrapped. No-op off-sandbox (mac/dev) — sandbox_wrap returns
        # the cmd unchanged. bwrap supplies its own --chdir, so the leading `cd`
        # only matters for the unwrapped path.
        if req.sandbox_wrap is not None:
            parts = req.sandbox_wrap(parts)

        launch = " ".join(shlex.quote(p) for p in parts)
        cmd = f"cd {shlex.quote(str(launch_cwd))} && {launch}"
        # -l sends the command literally (no key-name interpretation), then a
        # separate Enter submits it.
        self._tmux("send-keys", "-t", name, "-l", cmd)
        self._tmux("send-keys", "-t", name, "Enter")

    def _capture(self, name: str) -> str:
        return self._tmux("capture-pane", "-t", name, "-p").stdout

    def _wait_ready(self, name: str, deadline: float) -> bool:
        """Poll the pane until the REPL is ready, scripting past the launch
        dialogs as they appear. Returns False on deadline.

        Two dialogs can gate the prompt, in either order depending on prior
        state; the loop handles whichever is on screen each tick:
        - workspace trust ("Is this a project you trust?") — option 1
          pre-selected, so a bare Enter accepts.
        - "Bypass Permissions mode" warning — option 1 is "No, exit"
          (pre-selected), so we must send "2" to select "Yes, I accept" before
          Enter. Surfaces under the bwrap sandbox (tmpfs'd ~/.claude)."""
        self._last_dialogs: list[str] = []
        while time.monotonic() < deadline:
            pane = self._capture(name)
            if self._p.bypass_warning_marker in pane and self._p.bypass_accept_marker in pane:
                if "bypass" not in self._last_dialogs:
                    self._last_dialogs.append("bypass")
                # Select option 2 ("Yes, I accept") — a bare Enter would exit.
                self._tmux("send-keys", "-t", name, "2")
                time.sleep(_READY_POLL_S)
                self._tmux("send-keys", "-t", name, "Enter")
                time.sleep(_READY_POLL_S)
                continue
            if any(m in pane for m in self._p.trust_markers):
                if "trust" not in self._last_dialogs:
                    self._last_dialogs.append("trust")
                self._tmux("send-keys", "-t", name, "Enter")
                time.sleep(_READY_POLL_S)
                continue
            if any(m in pane for m in self._p.ready_markers):
                return True
            time.sleep(_READY_POLL_S)
        return False

    def _inject_prompt(self, name: str, prompt_file: Path) -> None:
        # Buffer load+paste avoids shell-escaping / pipe-buffer hazards for large
        # prompts (mirrors ClaudeCodeBrain's stdin feeder rationale).
        buf = f"istota-{name}"
        self._tmux("load-buffer", "-b", buf, str(prompt_file))
        self._tmux("paste-buffer", "-t", name, "-b", buf, "-d")
        self._tmux("send-keys", "-t", name, "Enter")

    def _wait_for_completion(
        self, name: str, sentinel: Path, deadline: float, cancel_check
    ) -> tuple[str, str]:
        """Multi-signal wait (§3). Returns (status, pane_text) where status is
        ``done`` / ``cancelled`` / ``error`` / ``timeout``. ``pane_text`` is the
        last captured pane on error/timeout (for transient classification + the
        log), empty otherwise.

        Each tick, in order: sentinel exists → done; cancel_check → cancelled;
        error marker in the pane → error (fail fast); pane/session dead → error;
        else continue, logging a one-shot stall warning at the halfway mark."""
        start = time.monotonic()
        total = max(deadline - start, 0.0)
        warned_stall = False
        while True:
            if sentinel.exists():
                return ("done", "")
            if cancel_check is not None:
                try:
                    if cancel_check():
                        return ("cancelled", "")
                except Exception:
                    logger.debug("cancel_check raised", exc_info=True)

            pane = self._capture(name)
            if any(m in pane for m in self._p.error_markers):
                return ("error", pane)
            if not self._pane_alive(name):
                logger.warning("tmux_brain pane died session=%s", name)
                return ("error", pane)

            now = time.monotonic()
            if not warned_stall and total > 0 and (now - start) >= total / 2:
                warned_stall = True
                logger.warning(
                    "tmux_stall session=%s halfway with no completion; pane=%r",
                    name, pane[:_PANE_LOG_CHARS],
                )
            if now >= deadline:
                return ("timeout", pane)
            time.sleep(_SENTINEL_POLL_S)

    def _pane_alive(self, name: str) -> bool:
        cp = self._tmux("list-panes", "-t", name, "-F", "#{pane_pid}")
        return cp.returncode == 0 and bool(cp.stdout.strip())

    def _pane_pid(self, name: str) -> int | None:
        out = self._tmux("list-panes", "-t", name, "-F", "#{pane_pid}").stdout.strip()
        if not out:
            return None
        try:
            return int(out.splitlines()[0])
        except ValueError:
            return None

    def _kill(self, name: str) -> None:
        self._tmux("kill-session", "-t", name)

    # Default dialog record (overwritten per _wait_ready run).
    _last_dialogs: list[str] = []
