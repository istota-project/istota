"""TmuxClaudeBrain — drives the interactive `claude` TUI in a detached tmux
session, instead of the headless `claude -p` subprocess `ClaudeCodeBrain` uses.

Mechanism: spawn a detached tmux session, launch the interactive TUI, inject the
prompt via a tmux buffer, detect turn completion with a `Stop`-hook sentinel
file, and reconstruct the result + trace by parsing the session transcript JSONL.
It runs the same `claude` binary and uses the same auth as ClaudeCodeBrain, so it
delegates all four model-resolution methods to a composed ``ClaudeCodeBrain`` and
only implements ``execute``.

Prototype maturity — ``claude_code`` stays the default and fallback. See
``Specs/Active/tmux-subscription-brain-feasibility.md``.
"""

import itertools
import json
import logging
import os
import shlex
import shutil
import subprocess
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
from .claude_code import ClaudeCodeBrain

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

# Per-process session-name counter (BrainRequest carries no task id by default).
_SESSION_COUNTER = itertools.count(1)


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


class TmuxClaudeBrain:
    """Brain that drives the interactive `claude` TUI inside a tmux session.

    Model resolution is delegated to an internal ``ClaudeCodeBrain``: this brain
    runs the same `claude` CLI binary against the same Anthropic model namespace,
    so duplicating ``MODEL_ALIASES`` / ``DEFAULT_ROLE_TARGETS`` would only invite
    drift. Only ``execute`` is genuinely new.
    """

    def __init__(self) -> None:
        # Composed, not inherited: we forward the four resolution methods and
        # own execute. The CLI brain holds no per-instance state, so a fresh
        # one here is free.
        self._cli = ClaudeCodeBrain()

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
        if shutil.which("tmux") is None:
            return BrainResult(
                success=False,
                result_text="tmux not found. The tmux_claude brain needs tmux on PATH.",
                stop_reason="not_found",
            )

        session = req.session_label or f"istota-tmux-{os.getpid()}-{next(_SESSION_COUNTER)}"

        # Base everything in the sandbox-shared RW region. Under bwrap the Stop
        # hook runs *inside* the sandbox and writes the sentinel; the brain reads
        # it from *outside*. Only ISTOTA_DEFERRED_DIR (= user_temp_dir) is
        # RW-bound at the same path inside and out AND is bwrap's --chdir target,
        # so the sentinel, the prompt file, and the project .claude/settings.json
        # (discovered from the sandbox cwd) must live there. A private mkdtemp in
        # /tmp would land on the sandbox's own tmpfs — invisible to the brain.
        # Off-sandbox (mac/dev/Docker) ISTOTA_DEFERRED_DIR may be unset; fall
        # back to req.cwd.
        base_dir = Path(req.env.get("ISTOTA_DEFERRED_DIR") or req.cwd)
        workdir = base_dir / f".tmux-{session}"
        sentinel = workdir / "stop.json"
        prompt_file = workdir / "prompt.txt"
        claude_dir = base_dir / ".claude"
        settings_path = claude_dir / "settings.json"

        try:
            workdir.mkdir(parents=True, exist_ok=True)
            prompt_file.write_text(req.prompt)
            self._write_stop_hook(claude_dir, settings_path, sentinel)

            self._new_session(session, req.env)
            self._launch_claude(session, req, base_dir)

            ready_deadline = time.monotonic() + min(_READY_TIMEOUT_S, req.timeout_seconds)
            if not self._wait_ready(session, ready_deadline):
                self._kill(session)
                return BrainResult(
                    success=False,
                    result_text="Interactive claude REPL never became ready",
                    stop_reason="timeout",
                )

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

            wait_deadline = time.monotonic() + req.timeout_seconds
            status = self._wait_sentinel(sentinel, wait_deadline, req.cancel_check)
            self._kill(session)

            if status == "cancelled":
                return BrainResult(
                    success=False, result_text="Cancelled by user", stop_reason="cancelled",
                )
            if status != "done":
                timeout_min = req.timeout_seconds // 60
                return BrainResult(
                    success=False,
                    result_text=f"Task execution timed out after {timeout_min} minutes",
                    stop_reason="timeout",
                )

            return self._build_result(sentinel, req)
        except FileNotFoundError as e:
            # tmux disappeared mid-run, or a helper hit a missing binary.
            return BrainResult(
                success=False, result_text=f"tmux/claude not found: {e}", stop_reason="not_found",
            )
        except Exception as e:
            logger.exception("TmuxClaudeBrain.execute raised")
            self._kill(session)
            return BrainResult(
                success=False, result_text=f"Execution error: {e}", stop_reason="error",
            )
        finally:
            shutil.rmtree(workdir, ignore_errors=True)

    # --- Result assembly --------------------------------------------------

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

    def _build_result(self, sentinel: Path, req: BrainRequest) -> BrainResult:
        """Read the Stop-hook payload + transcript, emit progress events, and
        compose the BrainResult with the same actions/trace shapes ClaudeCodeBrain
        produces (so downstream DB writes are unchanged)."""
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

        # Forward whole-turn events to on_progress (no token-level streaming —
        # the Stop hook fires at turn end). ResultEvent is the return value, not
        # a progress event, so it is never forwarded.
        if req.on_progress is not None:
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

    # --- tmux primitives (mocked in unit tests) ---------------------------

    @staticmethod
    def _write_stop_hook(claude_dir: Path, settings_path: Path, sentinel: Path) -> None:
        """Write a project-local settings.json whose Stop hook dumps the hook's
        stdin payload to the sentinel. Existence of the sentinel = turn finished;
        its contents = the payload (transcript_path + last_assistant_message)."""
        claude_dir.mkdir(parents=True, exist_ok=True)
        settings = {
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command",
                                "command": f"cat > {shlex.quote(str(sentinel))}"}]}
                ]
            }
        }
        settings_path.write_text(json.dumps(settings))

    @staticmethod
    def _tmux(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["tmux", *args], capture_output=True, text=True, check=False,
        )

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
        parts = ["claude"]
        if req.model:
            parts += ["--model", req.model]
        if req.effort:
            parts += ["--effort", req.effort]
        if req.custom_system_prompt_path and req.custom_system_prompt_path.exists():
            parts += ["--system-prompt-file", str(req.custom_system_prompt_path)]
        # Empty allowed_tools = text-only invocation (sleep cycle); skip tool
        # flags entirely, matching ClaudeCodeBrain. Otherwise mirror its
        # Agent-disallow.
        if req.allowed_tools:
            parts += ["--allowedTools", *req.allowed_tools, "--disallowedTools", "Agent"]
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
        while time.monotonic() < deadline:
            pane = self._capture(name)
            if _BYPASS_WARNING_MARKER in pane and _BYPASS_ACCEPT_MARKER in pane:
                # Select option 2 ("Yes, I accept") — a bare Enter would exit.
                self._tmux("send-keys", "-t", name, "2")
                time.sleep(_READY_POLL_S)
                self._tmux("send-keys", "-t", name, "Enter")
                time.sleep(_READY_POLL_S)
                continue
            if any(m in pane for m in _TRUST_MARKERS):
                self._tmux("send-keys", "-t", name, "Enter")
                time.sleep(_READY_POLL_S)
                continue
            if any(m in pane for m in _READY_MARKERS):
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

    def _wait_sentinel(self, sentinel: Path, deadline: float, cancel_check) -> str:
        """Poll for the Stop-hook sentinel. Returns 'done' / 'cancelled' / 'timeout'."""
        while True:
            if sentinel.exists():
                return "done"
            if cancel_check is not None:
                try:
                    if cancel_check():
                        return "cancelled"
                except Exception:
                    logger.debug("cancel_check raised", exc_info=True)
            if time.monotonic() >= deadline:
                return "timeout"
            time.sleep(_SENTINEL_POLL_S)

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
