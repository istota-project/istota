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
from ._aliases import CANONICAL_ROLES, split_effort
from ._roles import get_alias_override_target, get_alias_overrides
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
# silently re-route us. A model release bumps the constant in one place and
# ripples through every alias + role that points at it. Prior versions are NOT
# enumerated as aliases — an operator who needs one types the canonical id with
# an optional ``:effort`` modifier (``claude-opus-4-7:high``), which resolves via
# the canonical passthrough below.
# ---------------------------------------------------------------------------

OPUS: str = "claude-opus-4-8"
SONNET: str = "claude-sonnet-5"
HAIKU: str = "claude-haiku-4-5"

# The unified alias registry for *this brain* — the code-shipped floor. Maps a
# base alias name → ``(model_id, default_effort)`` in the Anthropic namespace.
# Holds the portable tiers (CANONICAL_ROLES) AND the provider shortcuts together,
# base names only: effort is an orthogonal ``:effort`` modifier applied generically
# at resolution (``opus:high``), never baked into a name. Operators overlay this
# via ``[models.aliases]`` TOML; a model release edits one constant here.
# Every surface (``!model`` prefix, ``!models`` output, scheduled-job overrides)
# reads through this table via ``Brain.resolve_alias`` / ``.list_aliases``.
DEFAULT_ALIASES: dict[str, tuple[str | None, str | None]] = {
    # Portable tiers (CANONICAL_ROLES) — code-floor efforts stay None; every
    # brain must map every canonical role (enforced by the role-contract test),
    # so a portable intent survives the cross-provider fallback.
    "fast":    (HAIKU, None),
    "general": (SONNET, None),
    "smart":   (OPUS, None),
    # Provider shortcuts (pins) — base names, no effort variants.
    "opus":    (OPUS, None),
    "sonnet":  (SONNET, None),
    "haiku":   (HAIKU, None),
    # Explicit "no override — use the brain/config default model".
    "default": (None, None),
}
assert set(CANONICAL_ROLES) <= set(DEFAULT_ALIASES), (
    "ClaudeCodeBrain must map every canonical role tier"
)

# The non-tier subset of the registry — the provider shortcuts. An operator
# alias override whose NAME collides with one of these silently changes what
# ``!model opus`` resolves to (almost always a typo for a tier override), so
# ``validate_alias_override`` warns on it. Overriding a tier is the normal case
# and never warns.
_SHORTCUT_NAMES: frozenset[str] = frozenset(set(DEFAULT_ALIASES) - set(CANONICAL_ROLES))


def _looks_canonical(name: str) -> bool:
    """Whether ``name`` is a raw Anthropic model id (passthrough target)."""
    return name.startswith("claude-")


def _resolve_target_with_effort(target: str) -> tuple[str, str | None]:
    """Translate an override RHS through ``DEFAULT_ALIASES`` to ``(model_id, effort)``.

    Splits an optional ``:effort`` modifier first, then resolves the base name.
    Operator wrote e.g. ``smart = "opus:high"`` → ``("claude-opus-4-8", "high")``
    (the modifier's effort wins over the alias's default). A bare shortcut
    ``smart = "opus"`` → ``("claude-opus-4-8", None)``. An unknown / canonical
    base passes through unchanged (raw ids like ``claude-opus-4-7`` work as
    targets), carrying only the modifier effort.
    """
    if not target:
        return target, None
    base, suffix_effort = split_effort(target)
    pair = DEFAULT_ALIASES.get(base.lower())
    if pair is not None and pair[0] is not None:
        return pair[0], (suffix_effort or pair[1])
    return base, suffix_effort


class ClaudeCodeBrain:
    """Brain that delegates to the `claude` CLI as a subprocess."""

    # The headless `claude -p` subprocess reads its whole prompt on stdin, then
    # stdin closes — there is no open channel to the running model, so mid-flight
    # steering is impossible by construction (see the !steer spec).
    supports_steering = False

    # This brain speaks the Anthropic model namespace. Operators key an
    # ``[models.aliases.<name>]`` sub-table on this string; TmuxClaudeBrain shares
    # it (same `claude` binary), so an ``anthropic`` value covers both.
    model_namespace = "anthropic"

    # --- Model resolution (Brain Protocol) ---------------------------------

    def resolve_alias(
        self, alias: str
    ) -> tuple[str | None, str | None] | None:
        """Resolve a `!model <alias>` (with optional ``:effort``) to (model_id, effort).

        Splits a ``:effort`` modifier first, then the base name resolves:
        operator override > ``DEFAULT_ALIASES`` (tiers + shortcuts) > canonical
        id passthrough (``claude-*``) > None (unknown). Effort precedence: the
        ``:effort`` suffix wins over the entry's own default effort. A role
        override's target is itself resolved through this brain's alias table
        (``smart = "opus"`` → ``claude-opus-4-8``), and an explicit
        ``RoleTarget.effort`` wins over the target's alias-derived effort.
        """
        if not alias:
            return None
        base, suffix_effort = split_effort(alias)
        base_lower = base.lower()
        # 1. Operator-overridden alias (per-namespace, effort-carrying)
        rt = get_alias_override_target(base_lower, self.model_namespace)
        if rt is not None:
            model_id, target_effort = _resolve_target_with_effort(rt.model)
            return (model_id, suffix_effort or rt.effort or target_effort)
        # 2. Shipped default (tier or shortcut)
        pair = DEFAULT_ALIASES.get(base_lower)
        if pair is not None:
            model_id, default_effort = pair
            return (model_id, suffix_effort or default_effort)
        # 3. Canonical id passthrough (carry the modifier effort)
        if _looks_canonical(base):
            return (base, suffix_effort)
        # 4. Unknown
        return None

    def resolve_model_name(self, name: str | None) -> str:
        """Resolve any name to a canonical Anthropic model ID.

        Empty/None → ``""`` (caller falls back to brain default).
        Unknown → pass-through with any ``:effort`` stripped (raw IDs typed into
        config still work; the effort never leaks into the model id).
        """
        if not name:
            return ""
        resolved = self.resolve_alias(name)
        if resolved is not None and resolved[0] is not None:
            return resolved[0]
        return split_effort(name)[0]

    def validate_alias_override(self, name: str, target: str) -> list[str]:
        """Surface operator typos at load time.

        Two checks:
        1. Alias name collides with a provider shortcut (e.g.
           ``[models.aliases] opus = "haiku"`` silently makes ``!model opus``
           resolve to Haiku — usually a typo for a tier override). Overriding a
           tier is the normal case and never warns.
        2. Override target is neither a known alias nor a canonical ``claude-*``
           id (it'd pass through to the CLI and fail at task time). A ``:effort``
           modifier on the target is stripped before the check.
        """
        warnings: list[str] = []
        name_lower = name.lower()
        if name_lower in _SHORTCUT_NAMES:
            warnings.append(
                f"alias override {name!r} shadows the built-in provider shortcut "
                f"of the same name; future `!model {name}` calls will resolve to "
                f"{target!r} instead of the shipped default"
            )
        if target:
            base, _effort = split_effort(target)
            known = base.lower() in DEFAULT_ALIASES
            if not _looks_canonical(base) and not known:
                warnings.append(
                    f"alias override {name!r} target {target!r} is neither a "
                    f"canonical model id nor a known alias; tasks using this "
                    f"alias will fail at execution time"
                )
        return warnings

    def list_aliases(self) -> list[tuple[str, str | None, str | None]]:
        """Merged alias table for display — base names + resolved default effort.

        Tiers sorted first, then the shipped shortcuts in declaration order, then
        any custom operator aliases (sorted). Operator overrides are reflected
        (resolved in this brain's own namespace, effort preserved). Used by
        ``!models`` and the composer autocomplete.
        """
        resolved: dict[str, tuple[str | None, str | None]] = dict(DEFAULT_ALIASES)
        for name in get_alias_overrides():
            rt = get_alias_override_target(name, self.model_namespace)
            if rt is not None:
                model_id, target_effort = _resolve_target_with_effort(rt.model)
                resolved[name] = (model_id, rt.effort or target_effort)
        tiers = sorted(n for n in resolved if n in CANONICAL_ROLES)
        shortcuts = [n for n in DEFAULT_ALIASES if n not in CANONICAL_ROLES]
        extras = sorted(
            n for n in resolved if n not in DEFAULT_ALIASES and n not in CANONICAL_ROLES
        )
        out: list[tuple[str, str | None, str | None]] = []
        for name in tiers + shortcuts + extras:
            model, effort = resolved[name]
            out.append((name, model, effort))
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
