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
import signal
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


# Pattern to detect Anthropic API errors carrying a JSON body.
API_ERROR_PATTERN = re.compile(r"API Error: (\d{3}) (\{.*\})", re.DOTALL)

# The CLI does not always attach a JSON body — `API Error: 529 Overloaded`,
# `API Error: 500` and `API Error: 400 Bad Request` are all real shapes. Matching
# only the JSON form meant a bare 529 parsed as *nothing*: not transient, so not
# retried, classified as a generic error, and therefore not a fallback trigger
# (ISSUE-212). The tail stops at the newline so a stack trace below the banner
# doesn't become the "message".
_API_ERROR_PLAIN_PATTERN = re.compile(r"API Error:?\s+(\d{3})\b[ \t]*([^\n]*)")

# "API Error" in any of its punctuations — `API Error: …`, `API Error (…)`. Used
# as a *gate* on the text-shaped predicates below so ordinary prose that happens
# to discuss a connection reset is never dragged onto the retry path.
_API_ERROR_MARKER = re.compile(r"API Error\b", re.IGNORECASE)

# Transient HTTP status codes that warrant retry. Documentation of the common
# cases — the live rule is `_status_is_transient`, which treats *every* 5xx as
# transient. Enumerating was a latent version of the bug this fixes: a
# Cloudflare-fronted provider emits 520-526 ("Web Server Returned an Unknown
# Error" / "Connection Timed Out"), none of which were listed, so each would
# dead-end exactly as the 529 did.
TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 529}  # 529 = overloaded

# Non-5xx statuses that are still capacity/timing signals rather than a problem
# with the request: 408 Request Timeout, 425 Too Early, 429 Too Many Requests.
_TRANSIENT_4XX = frozenset({408, 425, 429})


def _status_is_transient(status: int) -> bool:
    """Whether an HTTP status is a capacity/availability signal worth retrying."""
    return status >= 500 or status in _TRANSIENT_4XX

# Request-shaped statuses: retrying or switching brains cannot help, and doing
# either wastes a call (and on a paid fallback, money). The complement of
# TRANSIENT_STATUS_CODES for the codes we actually see.
PERMANENT_STATUS_CODES = frozenset({400, 401, 403, 404, 405, 413, 414, 422})

# Network/transport failures the CLI reports as an API error. Capacity-shaped in
# the same sense as a 529: the request was fine, the path to the provider wasn't.
_NETWORK_TRANSIENT_RE = re.compile(
    r"connection (?:error|reset|refused|closed|aborted)|"
    r"(?:request|read|socket|connect)?\s*time[d]?\s?out|"
    r"socket hang ?up|network error|fetch failed|premature close|"
    r"getaddrinfo|dns (?:lookup )?fail|"
    # The CLI's own documented capacity-throttle banner: "Server is
    # temporarily limiting requests (not your usage limit)". Explicitly a
    # server-side throttle, so it belongs in the fallback trigger set.
    r"temporarily limiting requests|limiting requests",
    re.IGNORECASE,
)

# Node/libc errno strings are diagnostic enough to stand without the API Error
# marker — they don't occur in prose, and the CLI doesn't always wrap them.
_NET_ERRNO_RE = re.compile(
    r"\b(ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b"
)

# Request-shaped bodies: the *content* of the request is the problem, so no
# amount of retrying or brain-switching changes the outcome.
_REQUEST_SHAPED_RE = re.compile(
    r"invalid_request_error|authentication_error|permission_error|not_found_error|"
    r"content[_ ]filter|prompt is too long|"
    r"context[ _-]?(?:length|window|size)|maximum context",
    re.IGNORECASE,
)

# `retry-after: 30`, `"retry_after": 30`, `Retry-After 30`. `[ \t]*` rather than
# `\s*`: the pair `\s*[:=]?\s*` is ambiguous and backtracks quadratically on a
# long whitespace run, and the input here is unbounded provider/stderr text.
_RETRY_AFTER_RE = re.compile(
    r"retry[-_ ]?after[\"']?[ \t]*[:=]?[ \t]*[\"']?(\d+(?:\.\d+)?)", re.IGNORECASE
)

# Retry configuration for transient API errors
API_RETRY_MAX_ATTEMPTS = 3
API_RETRY_DELAY_SECONDS = 5
# Ceiling on a provider-supplied Retry-After. A worker parked on the provider's
# word for an hour is worse than failing the attempt and letting the task's own
# retry ladder (1/4/16 min) or the fallback brain take over.
RETRY_AFTER_MAX_SECONDS = 60.0
# Slice length for the retry backoff, so `!stop` lands within a slice
# instead of waiting out a (now potentially 60s) provider-requested delay.
_RETRY_SLEEP_SLICE_SECONDS = 0.5


def parse_api_error(text: str) -> dict | None:
    """Parse API error string into structured data.

    Returns dict with status_code, message, request_id on match, or None.
    Prefers the JSON-bodied form; falls back to the bodyless
    ``API Error: NNN <text>`` shape the CLI also emits.
    """
    if not text:
        return None
    match = API_ERROR_PATTERN.search(text)
    if match:
        status_code = int(match.group(1))
        try:
            payload = json.loads(match.group(2))
            return {
                "status_code": status_code,
                "message": payload.get("error", {}).get("message", "Unknown error"),
                "request_id": payload.get("request_id"),
            }
        except json.JSONDecodeError:
            return {
                "status_code": status_code,
                "message": "Unknown error",
                "request_id": None,
            }

    plain = _API_ERROR_PLAIN_PATTERN.search(text)
    if not plain:
        return None
    return {
        "status_code": int(plain.group(1)),
        "message": plain.group(2).strip() or "Unknown error",
        "request_id": None,
    }


def parse_retry_after(text: str) -> float | None:
    """The provider's requested wait in seconds, capped, or None.

    Capped at ``RETRY_AFTER_MAX_SECONDS`` and floored at 0 — a negative or
    absurd value is treated as absent rather than obeyed.
    """
    if not text:
        return None
    match = _RETRY_AFTER_RE.search(text)
    if not match:
        return None
    try:
        value = float(match.group(1))
    except ValueError:
        return None
    if value <= 0:
        return None
    return min(value, RETRY_AFTER_MAX_SECONDS)


def _looks_like_api_error(text: str) -> bool:
    """Whether ``text`` carries a provider API-error signal at all."""
    if not text:
        return False
    return bool(
        parse_api_error(text)
        or _API_ERROR_MARKER.search(text)
        or _NET_ERRNO_RE.search(text)
    )


def is_transient_api_error(text: str) -> bool:
    """Check if the error text represents a transient API error worth retrying.

    Two signals: a capacity/gateway *status code* (429 + 5xx + 529), or a
    network-level failure. The network branch is gated on the ``API Error``
    marker (or an unambiguous errno) so ordinary prose mentioning a connection
    reset can't route a task onto the retry/fallback path — this predicate is
    also run against arbitrary tmux pane text.

    A 429 whose body signals *quota/subscription exhaustion* is NOT transient —
    ``is_usage_limit_error`` catches that case first at every call site, so a
    usage limit reroutes to the configured fallback brain instead of being
    retried against the same exhausted primary.
    """
    if not text:
        return False
    parsed = parse_api_error(text)
    if parsed:
        status = parsed["status_code"]
        if _status_is_transient(status):
            return True
        if status in PERMANENT_STATUS_CODES:
            # An explicit request-shaped status is authoritative: a 400 whose
            # body quotes "connection reset" is still a client error (NB-13a).
            return False
        # An unclassified status (a provider's own 4xx extension) falls through
        # to the text signals rather than being declared non-transient.
    if _NET_ERRNO_RE.search(text):
        return True
    return bool(
        _API_ERROR_MARKER.search(text) and _NETWORK_TRANSIENT_RE.search(text)
    )


def is_permanent_api_error(text: str) -> bool:
    """True when the error is request-shaped: no retry, no fallback attempt.

    400 / 401 / 403 / 404 / 413 and friends, plus context-length and
    content-filter refusals. The status code wins over the body text, so a 529
    whose message happens to quote a request-shaped phrase stays transient.
    """
    if not text:
        return False
    parsed = parse_api_error(text)
    if parsed:
        status = parsed["status_code"]
        if _status_is_transient(status):
            return False
        if status in PERMANENT_STATUS_CODES:
            return True
    # Text-only fall-through. Gated on the API-error marker for the same reason
    # the transient one is: "the model's context window is 200k tokens" is an
    # answer, not a failure. A network signal wins, so an unparseable
    # "API Error: connection reset while building the context window" doesn't
    # read as permanent on the strength of a phrase in its message.
    if not _looks_like_api_error(text) or _NETWORK_TRANSIENT_RE.search(text):
        return False
    return bool(_REQUEST_SHAPED_RE.search(text))


def api_error_stop_reason(text: str) -> str | None:
    """The ``stop_reason`` for a provider API error, or None if not one.

    The single classifier the execution paths use, so "is this worth a retry /
    a fallback attempt?" is answered the same way everywhere. Precedence:
    a usage limit is persistent and outranks its own 429 status; a
    request-shaped failure is permanent; capacity and network failures are
    transient; anything else that still looks like an API error is a plain
    error (unknown status → don't gamble a fallback call on it).
    """
    if not text:
        return None
    if is_usage_limit_error(text):
        return "usage_limit"
    if not _looks_like_api_error(text):
        return None
    if is_permanent_api_error(text):
        return "error"
    if is_transient_api_error(text):
        return "transient_api_error"
    return "error"


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


# A bare provider error delivered as the whole answer opens with the marker;
# a real answer that *discusses* one has it somewhere in the middle. Anchored at
# the start (past up to 8 chars of decoration — an emoji, a bullet, a quote
# marker) and length-gated, mirroring ``is_usage_limit_banner``.
_API_ERROR_BANNER_RE = re.compile(
    r"^[\s\W]{0,8}API Error\b[\s:(\[,-]*"       # marker + whatever separates it
    r"(?:(?P<status>\d{3})\b[ \t]*)?"           # optional status code
    r"(?P<tail>[^\n]*)",
    re.IGNORECASE,
)


def is_api_error_banner(text: str) -> bool:
    """True iff ``text`` *is* a bare provider API-error banner.

    For the paths where ``claude`` reports an API failure as a **successful**
    result frame (rc 0, the error text as the whole answer) — the mechanism that
    put a raw ``API Error: 529 Overloaded`` in front of the user as the final
    reply (ISSUE-212), and the same one already handled for usage limits by
    :func:`is_usage_limit_banner`.

    Strict on purpose: it must not fire on a genuine answer that *mentions* an
    API error, because the callers act on it destructively — the brain reroutes
    to a (paid) fallback, and ``scheduler``'s masquerading-success guard fails
    the task outright.

    Three gates. **Anchored** at the start, so an answer discussing an error
    mid-sentence is out. **Length**-gated, so a long answer that merely opens
    with the phrase is out. And the token after the marker (or after the status
    code) must be **JSON or Title-cased** — every real banner is a reason
    phrase (``529 Overloaded``, ``500 Internal Server Error``, ``Connection
    error.``) or a JSON body, whereas prose continues in lowercase
    (``API Error: 529 means the provider is overloaded``). That last gate is
    what separates the banner from a sentence that legitimately starts with it.
    """
    if not text:
        return False
    stripped = text.strip()
    if not stripped or len(stripped) > _BANNER_MAX_CHARS:
        return False
    match = _API_ERROR_BANNER_RE.match(stripped)
    if not match:
        return False
    if not match.group("status") and not _looks_like_api_error(stripped):
        return False
    tail = match.group("tail").strip()
    if not tail:
        # "API Error: 529" — a status and nothing else is unambiguous. Without a
        # status it is a bare "API Error" with no content, which is not one.
        return bool(match.group("status"))
    return tail[0].isupper() or tail[0] in "{[\"'"


def _interruptible_sleep(seconds: float, req: BrainRequest) -> bool:
    """Sleep in slices, polling ``req.cancel_check`` between them.

    A provider-supplied Retry-After can be far longer than the old fixed 5s, and
    `time.sleep` is not cancellable — a `!stop` issued during the backoff would
    otherwise sit unanswered for the whole wait. Returns True if the caller
    should stop (cancellation requested).
    """
    # Counts the slices down rather than watching a deadline: the loop must
    # terminate on its own arithmetic, not on wall-clock progress it can't
    # observe (and a patched time.sleep would otherwise spin forever).
    remaining = max(0.0, seconds)
    while remaining > 0:
        if req.cancel_check is not None:
            try:
                if req.cancel_check():
                    return True
            except Exception:
                logger.debug("cancel_check raised during retry backoff", exc_info=True)
        slice_seconds = min(_RETRY_SLEEP_SLICE_SECONDS, remaining)
        time.sleep(slice_seconds)
        remaining -= slice_seconds
    return False


def _is_retryable(result: "BrainResult") -> bool:
    """Whether a failed attempt should be retried against the same primary.

    Reads the ``stop_reason`` first (the ``_execute_*_once`` paths already
    classified the text) and falls back to re-classifying the text for any path
    that returned a bare ``error``.

    ``work_committed`` vetoes the retry outright: a run that reached the model
    and then reported a provider error may already have executed tools, so
    re-invoking the same prompt would repeat those side effects. Such a failure
    is reroute-only — the executor sends it to the fallback brain.
    """
    if result.work_committed:
        return False
    if result.stop_reason == "transient_api_error":
        return True
    if result.stop_reason in ("usage_limit", "cancelled", "timeout", "oom", "not_found"):
        return False
    return is_transient_api_error(result.result_text)


def _success_frame_stop_reason(text: str) -> str | None:
    """The stop_reason when a *successful* CLI result actually carries a
    provider failure banner, or None when it is a genuine answer.

    ``claude -p`` reports both a subscription limit and a provider API error as
    a success (rc 0 / ``subtype:"success"``) with the banner as the whole
    answer. Left alone, both are delivered to the user verbatim as the final
    reply and neither can ever reach the fallback brain.
    """
    if is_usage_limit_banner(text):
        return "usage_limit"
    if is_api_error_banner(text):
        return api_error_stop_reason(text) or "error"
    return None


def _failure_stop_reason(text: str) -> str:
    """Classify a failure's text into ``usage_limit`` / ``transient_api_error``
    / ``error``.

    Used at ClaudeCodeBrain's error-return points so a usage-limit body carries
    the distinct stop_reason before the generic ``error`` path swallows it, and
    so a capacity error is visibly transient rather than an anonymous failure
    the fallback trigger set can't match.
    """
    return api_error_stop_reason(text) or "error"


_OOM_TEXT = "Claude Code was killed (likely out of memory)"
_TERMINATED_PREFIX = "Claude Code was terminated by "


def is_signal_termination(text: str) -> bool:
    """True when a failure text is the brain's signal-death message.

    The executor drops ``stop_reason`` at its return boundary, so the scheduler
    classifies failures by their text (the same way it recognizes OOM and
    cancellation). This keeps the marker string in one place.
    """
    return text.startswith(_TERMINATED_PREFIX)


def _signal_result(returncode: int | None, execution_trace: str | None) -> BrainResult | None:
    """Classify a process killed by a signal. Returns None if it wasn't.

    A negative returncode means the subprocess died on signal ``-returncode``.
    Only SIGKILL used to be recognized (the OOM killer's and systemd-oomd's
    signature); every other signal fell through to the generic stream-parse
    catch-all and was reported as "Stream parsing failed (rc=-15, N lines)" — a
    symptom, not a cause. SIGTERM in particular is what ``systemctl restart``
    delivers to the whole cgroup under systemd's default KillMode, so it is a
    routine event that deserves a name (ISSUE-191).
    """
    if returncode is None or returncode >= 0:
        return None
    signum = -returncode
    if signum == signal.SIGKILL:
        return BrainResult(
            success=False,
            result_text=_OOM_TEXT,
            execution_trace=execution_trace,
            stop_reason="oom",
        )
    try:
        name = signal.Signals(signum).name
    except ValueError:
        name = "signal"
    return BrainResult(
        success=False,
        result_text=f"{_TERMINATED_PREFIX}{name} (signal {signum})",
        execution_trace=execution_trace,
        stop_reason="terminated",
    )


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
            pair = DEFAULT_ALIASES.get(base.lower())
            if pair is not None and pair[0] is None:
                # A known alias that pins no model (the reserved ``default``).
                # Used as a target it'd be sent to the CLI as the literal string
                # "default" rather than resolving to a real model.
                warnings.append(
                    f"alias override {name!r} target {target!r} resolves to no "
                    f"model (it is the reserved 'use the brain default' alias); "
                    f"tasks using this alias would send it verbatim — pin a "
                    f"concrete model id or tier instead"
                )
            elif pair is None and not _looks_canonical(base):
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

            if not _is_retryable(result):
                return result

            last_error = result.result_text
            parsed = parse_api_error(result.result_text)
            request_id = parsed.get("request_id", "unknown") if parsed else "unknown"
            delay = parse_retry_after(result.result_text) or API_RETRY_DELAY_SECONDS

            if attempt < API_RETRY_MAX_ATTEMPTS - 1:
                logger.warning(
                    "Transient API error (attempt %d/%d, request_id=%s), retrying in %ss...",
                    attempt + 1, API_RETRY_MAX_ATTEMPTS, request_id, delay,
                )
                if _interruptible_sleep(delay, req):
                    return BrainResult(
                        success=False,
                        result_text="Cancelled by user",
                        stop_reason="cancelled",
                    )
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

        signal_death = _signal_result(result.returncode, None)
        if signal_death is not None:
            return signal_death

        # A session/quota limit or a provider API error is reported by
        # `claude -p` as a *successful* completion (rc 0, the banner as the
        # answer), so classify both on the success branch too — otherwise they
        # default to stop_reason="completed", never match the fallback trigger
        # set, and get delivered to the user as the reply.
        if result.returncode == 0 and output:
            reclassified = _success_frame_stop_reason(output)
            if reclassified:
                return BrainResult(
                    success=False, result_text=output, stop_reason=reclassified,
                    work_committed=True,
                )
            return BrainResult(success=True, result_text=output)
        if result.returncode == 0 and req.result_file and req.result_file.exists():
            file_text = req.result_file.read_text().strip()
            reclassified = _success_frame_stop_reason(file_text)
            if reclassified:
                return BrainResult(
                    success=False, result_text=file_text, stop_reason=reclassified,
                    work_committed=True,
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

            if not _is_retryable(result):
                return result

            last_error = result.result_text
            parsed = parse_api_error(result.result_text)
            request_id = parsed.get("request_id", "unknown") if parsed else "unknown"
            delay = parse_retry_after(result.result_text) or API_RETRY_DELAY_SECONDS

            if attempt < API_RETRY_MAX_ATTEMPTS - 1:
                logger.warning(
                    "Transient API error (attempt %d/%d, request_id=%s), retrying in %ss...",
                    attempt + 1, API_RETRY_MAX_ATTEMPTS, request_id, delay,
                )
                if _interruptible_sleep(delay, req):
                    return BrainResult(
                        success=False,
                        result_text="Cancelled by user",
                        stop_reason="cancelled",
                    )
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

        # A signal death outranks every remaining branch: the process was killed
        # from outside, so whatever it had (or hadn't) written to stdout says
        # nothing about why. The trace rides along — the tools that ran before
        # the kill are the only diagnostic left (ISSUE-183/191).
        signal_death = _signal_result(process.returncode, trace_json)
        if signal_death is not None:
            logger.warning(
                "claude subprocess died on a signal: %s (stdout_lines=%d)",
                signal_death.result_text, len(raw_stdout_lines),
            )
            return signal_death

        stderr_output = "".join(stderr_lines).strip()

        # Extract result: prefer ResultEvent, fall back to result file, then stderr.
        if final_result is not None:
            result_text = final_result.text.strip()
            if final_result.success:
                # `claude -p` reports a session/quota limit — and a provider API
                # error — as a success result frame (subtype:"success", the
                # banner as `result`). Classify both here so they reroute to the
                # fallback brain instead of being delivered as the answer with
                # the default stop_reason="completed". Use the strict *banner*
                # detectors: the broad keyword ones would misread a genuine
                # answer that merely quotes a limit word or an earlier API error
                # (e.g. a memory extraction summarising a past outage).
                reclassified = _success_frame_stop_reason(result_text)
                if reclassified:
                    return BrainResult(
                        success=False,
                        result_text=result_text,
                        execution_trace=trace_json,
                        stop_reason=reclassified,
                        model_used=model_seen or req.model,
                        work_committed=True,
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
                reclassified = _success_frame_stop_reason(output)
                if reclassified:
                    return BrainResult(
                        success=False,
                        result_text=output.strip(),
                        execution_trace=trace_json,
                        stop_reason=reclassified,
                        model_used=model_seen or req.model,
                        work_committed=True,
                    )
                return BrainResult(
                    success=True,
                    result_text=output.strip(),
                    actions_taken=actions_json,
                    execution_trace=trace_json,
                    model_used=model_seen or req.model,
                )
            # A limit or API-error message written to the result file (rather
            # than a ResultEvent) must still carry its own stop_reason, not a
            # generic error — otherwise it's not a fallback trigger.
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
