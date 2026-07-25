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
from ._aliases import CANONICAL_ROLES
from ._roles import get_role_override_target, get_role_overrides
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
    """Check if the error text represents a transient API error worth retrying.

    A 429 whose body signals *quota/subscription exhaustion* is NOT transient —
    ``is_usage_limit_error`` catches that case first at every call site, so a
    usage limit reroutes to the configured fallback brain instead of being
    retried against the same exhausted primary.
    """
    parsed = parse_api_error(text)
    if not parsed:
        return False
    return parsed["status_code"] in TRANSIENT_STATUS_CODES or parsed["status_code"] == 429


# Substrings (case-insensitive) that mark a subscription/quota/billing limit —
# a *persistent* "brain unavailable until the window resets" condition, distinct
# from a transient overload 429. Kept as a plain keyword set rather than tied to
# the ``API Error: NNN`` shape so the same detector works on ClaudeCodeBrain's
# CLI output, the tmux TUI transcript/pane text, and NativeBrain's arbitrary
# OpenAI-compatible error bodies. Best-effort and tunable against real output
# (see the spec's "Real usage-limit output samples" open question).
_USAGE_LIMIT_KEYWORDS: tuple[str, ...] = (
    "usage limit",
    "session limit",
    "limit reached",
    "quota",
    "insufficient_quota",
    "credit balance",
    "out of credit",
    "billing",
    "plan limit",
    "monthly limit",
    "usage cap",
    "spending limit",
)

# "...exceeded ... limit" where the two words are close together (an explicit
# limit-exceeded phrasing). Requires "exceeded" to precede "limit" so a plain
# "rate limit exceeded" (transient) does NOT match.
_EXCEEDED_LIMIT_RE = re.compile(r"exceeded[^.]{0,40}?\blimit\b", re.IGNORECASE)

# Claude Code's subscription-limit stem: "You've hit your <scope> limit · resets …".
# The scope varies — session / weekly / Opus / org's monthly spend (per the
# Claude Code error docs, https://code.claude.com/docs/en/errors, plus the live
# "You've hit your org's monthly spend limit · ask your admin to raise it"
# banner) — and all are a persistent "brain unavailable until reset" condition.
# Anchoring on "hit your … limit" catches every current phrasing (and a future
# scope word) without enumerating each noun, and the "hit your" anchor keeps a
# transient "rate limit" from matching.
_HIT_LIMIT_RE = re.compile(r"hit your[^.]{0,40}?\blimit\b", re.IGNORECASE)

# Standalone credit-exhaustion banner ("Credit balance is too low", per the docs).
_CREDIT_BALANCE_LOW_RE = re.compile(r"credit balance is too low", re.IGNORECASE)

# "<scope> limit reached" — the legacy/API banner phrasing ("usage limit
# reached · resets …"). "reached" (past-tense, adjacent to "limit") is a banner
# signal a normal answer rarely produces; the length gate in
# ``is_usage_limit_banner`` guards the residual risk.
_LIMIT_REACHED_RE = re.compile(r"\blimit reached\b", re.IGNORECASE)

# Claude Code's explicit "this is a server-side capacity throttle, NOT your quota"
# disclaimer ("API Error: Server is temporarily limiting requests (not your usage
# limit)", per the docs). It contains the substring "usage limit", so without this
# guard the broad keyword set below would misread a transient throttle as a
# persistent usage limit and needlessly trip the fallback breaker.
_NOT_USAGE_LIMIT_RE = re.compile(r"not your usage limit", re.IGNORECASE)

# A genuine usage-limit *banner* is a short standalone one-liner delivered as the
# whole result. A real answer that merely quotes a limit word (e.g. a memory
# extraction summarising a past "usage limit" incident) is longer and is not a
# banner; this ceiling is what keeps such content off the strict success-frame
# path (see ``is_usage_limit_banner``).
_BANNER_MAX_CHARS = 400


def is_usage_limit_error(text: str) -> bool:
    """True if ``text`` indicates a subscription/quota/billing usage limit.

    Shared by all three brains to classify a persistent "primary unavailable"
    condition as ``stop_reason="usage_limit"`` (which reroutes to the configured
    fallback brain) rather than a transient retry or a generic error.

    This is the **broad** detector, meant for genuine *error* bodies (native
    provider error JSON, ``claude`` stderr, a failure result). It matches the
    keyword set liberally, so it must NOT be run against a *successful* answer —
    use :func:`is_usage_limit_banner` there instead. A server-side throttle that
    explicitly says "(not your usage limit)" is excluded so a transient capacity
    error can't be mistaken for a quota outage.
    """
    if not text:
        return False
    if _NOT_USAGE_LIMIT_RE.search(text):
        return False
    low = text.lower()
    if any(keyword in low for keyword in _USAGE_LIMIT_KEYWORDS):
        return True
    return bool(_EXCEEDED_LIMIT_RE.search(text)) or bool(_HIT_LIMIT_RE.search(text))


def is_usage_limit_banner(text: str) -> bool:
    """True iff ``text`` *is* a standalone Claude Code usage-limit / credit banner.

    Stricter than :func:`is_usage_limit_error`, for the paths where ``claude``
    reports a subscription limit as a **successful** result frame (rc 0, the
    limit banner as the whole answer). It must not fire on a genuine answer that
    merely mentions a limit word — e.g. a nightly memory-extraction summarising a
    conversation *about* a past usage limit, whose successful output otherwise
    re-classified as ``usage_limit`` and kept the availability breaker armed long
    after the real limit cleared (the observed feedback loop).

    Robustness comes from matching only the *precise published banner shapes*
    (`https://code.claude.com/docs/en/errors`) — "You've hit your <scope> limit",
    "Credit balance is too low", or an explicit "exceeded … limit" — and only
    when the text is short enough to be a standalone banner rather than a real
    answer. The broad keyword set is deliberately not consulted here.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > _BANNER_MAX_CHARS:
        return False
    if _NOT_USAGE_LIMIT_RE.search(stripped):
        return False
    return bool(
        _HIT_LIMIT_RE.search(stripped)
        or _CREDIT_BALANCE_LOW_RE.search(stripped)
        or _LIMIT_REACHED_RE.search(stripped)
        or _EXCEEDED_LIMIT_RE.search(stripped)
    )


def _failure_stop_reason(text: str) -> str:
    """Classify a failure's text into ``usage_limit`` (persistent) or ``error``.

    Used at ClaudeCodeBrain's error-return points so a usage-limit body carries
    the distinct stop_reason before the generic ``error`` path swallows it.
    """
    return "usage_limit" if is_usage_limit_error(text) else "error"


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
# The keys are the portable CANONICAL_ROLES (single source of truth) — every
# brain must map every canonical role to a real model (enforced by the
# role-contract test), so a portable intent survives the cross-provider fallback.
DEFAULT_ROLE_TARGETS: dict[str, str] = {
    "fast":    HAIKU,
    "general": SONNET,
    "smart":   OPUS,
}
assert set(DEFAULT_ROLE_TARGETS) == set(CANONICAL_ROLES), (
    "ClaudeCodeBrain must map every canonical role tier"
)


def _resolve_target_with_effort(target: str) -> tuple[str, str | None]:
    """Translate an override RHS through MODEL_ALIASES to ``(canonical_id, effort)``.

    Operator wrote e.g. ``smart = "opus-high"``: this returns
    ``("claude-opus-4-8", "high")`` — the alias's effort is preserved (fixing
    the effort-drop). Unknown strings pass through unchanged with no effort so
    raw canonical IDs (``"claude-opus-4-8"``) work as override targets too.
    """
    if not target:
        return target, None
    pair = MODEL_ALIASES.get(target.lower())
    if pair is not None and pair[0] is not None:
        return pair[0], pair[1]
    return target, None


def _resolve_target(target: str) -> str:
    """Translate an override RHS through MODEL_ALIASES to a canonical ID (id only)."""
    return _resolve_target_with_effort(target)[0]


class ClaudeCodeBrain:
    """Brain that delegates to the `claude` CLI as a subprocess."""

    # The headless `claude -p` subprocess reads its whole prompt on stdin, then
    # stdin closes — there is no open channel to the running model, so mid-flight
    # steering is impossible by construction (see the !steer spec).
    supports_steering = False

    # This brain speaks the Anthropic model namespace. Operators key an
    # ``[models.roles.<role>]`` sub-table on this string; TmuxClaudeBrain shares
    # it (same `claude` binary), so an ``anthropic`` value covers both.
    model_namespace = "anthropic"

    # --- Model resolution (Brain Protocol) ---------------------------------

    def resolve_alias(
        self, alias: str
    ) -> tuple[str | None, str | None] | None:
        """Resolve a `!model <alias>` to (model_id, effort).

        Roles win over provider aliases (operator override > default role
        target > MODEL_ALIASES). Returns None for unknown. A role override's
        target is resolved through this brain's *own* alias table, and the
        alias's effort (``opus-high`` → ``high``) is preserved — an explicit
        ``RoleTarget.effort`` overrides it.
        """
        alias_lower = alias.lower()
        # 1. Operator-overridden role (per-namespace, effort-carrying)
        rt = get_role_override_target(alias_lower, self.model_namespace)
        if rt is not None:
            model_id, alias_effort = _resolve_target_with_effort(rt.model)
            return (model_id, rt.effort or alias_effort)
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
        # Roles: defaults merged with overrides (resolved in this brain's own
        # namespace, effort preserved); overrides win.
        roles: dict[str, tuple[str, str | None]] = {
            role: (target, None) for role, target in DEFAULT_ROLE_TARGETS.items()
        }
        for role in get_role_overrides():
            rt = get_role_override_target(role, self.model_namespace)
            if rt is not None:
                model_id, alias_effort = _resolve_target_with_effort(rt.model)
                roles[role] = (model_id, rt.effort or alias_effort)
        for role in sorted(roles):
            model_id, effort = roles[role]
            out.append((role, model_id, effort))
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

            # A usage/quota limit is persistent — do NOT retry it against the
            # same exhausted primary (a quota 429 matches is_transient_api_error,
            # so this short-circuit must precede that check). It reroutes to the
            # configured fallback brain at the executor level.
            if result.stop_reason == "usage_limit":
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

        # A session/quota limit is reported by `claude -p` as a *successful*
        # completion (rc 0, the limit text as the answer), so classify it on the
        # success branch too — otherwise it defaults to stop_reason="completed",
        # never matches the fallback trigger set, and gets delivered as the reply.
        if result.returncode == 0 and output:
            if is_usage_limit_banner(output):
                return BrainResult(
                    success=False, result_text=output, stop_reason="usage_limit",
                )
            return BrainResult(success=True, result_text=output)
        if result.returncode == 0 and req.result_file and req.result_file.exists():
            file_text = req.result_file.read_text().strip()
            if is_usage_limit_banner(file_text):
                return BrainResult(
                    success=False, result_text=file_text, stop_reason="usage_limit",
                )
            return BrainResult(success=True, result_text=file_text)
        if output:
            return BrainResult(
                success=False, result_text=output,
                stop_reason=_failure_stop_reason(output),
            )
        if result.stderr.strip():
            stderr = result.stderr.strip()
            return BrainResult(
                success=False, result_text=stderr,
                stop_reason=_failure_stop_reason(stderr),
            )
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

            # Persistent usage/quota limit — reroute (not retry). Precedes the
            # transient check because a quota 429 also matches it.
            if result.stop_reason == "usage_limit":
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
                    tool_entry = {"type": "tool", "text": event.description}
                    if event.invocation:
                        tool_entry["raw"] = event.invocation
                    execution_trace.append(tool_entry)
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
                # `claude -p` reports a session/quota limit as a success result
                # frame (subtype:"success", the limit banner as `result`). Classify
                # it here so it reroutes to the fallback brain instead of being
                # delivered as the answer with the default stop_reason="completed".
                # Use the strict *banner* detector: the broad keyword one would
                # misread a genuine answer that merely quotes a limit word (e.g. a
                # memory extraction summarising a past outage) as a fresh outage.
                if is_usage_limit_banner(result_text):
                    return BrainResult(
                        success=False,
                        result_text=result_text,
                        execution_trace=trace_json,
                        stop_reason="usage_limit",
                        model_used=model_seen or req.model,
                    )
                return BrainResult(
                    success=True,
                    result_text=result_text,
                    actions_taken=actions_json,
                    execution_trace=trace_json,
                    model_used=model_seen or req.model,
                )
            failure_text = result_text or stderr_output or "Unknown error"
            return BrainResult(
                success=False,
                result_text=failure_text,
                execution_trace=trace_json,
                stop_reason=_failure_stop_reason(failure_text),
                model_used=model_seen or req.model,
            )

        if req.result_file and req.result_file.exists():
            output = req.result_file.read_text()
            if process.returncode == 0:
                if is_usage_limit_banner(output):
                    return BrainResult(
                        success=False,
                        result_text=output.strip(),
                        execution_trace=trace_json,
                        stop_reason="usage_limit",
                        model_used=model_seen or req.model,
                    )
                return BrainResult(
                    success=True,
                    result_text=output.strip(),
                    actions_taken=actions_json,
                    execution_trace=trace_json,
                    model_used=model_seen or req.model,
                )
            # A limit message written to the result file (rather than a
            # ResultEvent) must still classify as usage_limit, not a generic
            # error — otherwise it's not a fallback trigger.
            return BrainResult(
                success=False,
                result_text=output.strip(),
                execution_trace=trace_json,
                stop_reason=_failure_stop_reason(output),
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
                execution_trace=trace_json,
                stop_reason=_failure_stop_reason(stderr_output),
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
