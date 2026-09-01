"""NativeBrain — istota's own agent loop behind the Brain protocol.

The executor calls ``brain.execute(req)`` and gets a ``BrainResult`` back, exactly
as it does for ``ClaudeCodeBrain``. It doesn't know which brain is behind the
protocol. Internally ``NativeBrain`` runs the three-layer stack — provider
(``istota.llm``) → generic loop (``istota.agent``) → session management (this
module + ``istota.session``).

What the executor still owns and ``NativeBrain`` consumes: the fully-composed
prompt (``req.prompt`` → the user message), the optional system-prompt file, the
per-task env, the cwd, and the cancel check. What it ignores: ``sandbox_wrap``
(each tool sandboxes its own subprocess per-execution), ``on_pid`` and
``result_file`` (subprocess concerns).

Wired here: compaction via the loop's ``prepare_next_turn`` hook, output-aware
loop detection + a max-turns cap as composable stop conditions, transient-error
retry at the provider boundary, orphan tool-pair repair at the converter
boundary, and per-task cost/token telemetry attached to ``BrainResult.usage``.

Model-namespace resolution (``resolve_alias`` etc.) is deliberately minimal: the
sole provider (``openai_compat``) may point at *any* endpoint, so Anthropic
provider aliases (``opus``/``sonnet``/``haiku``) are never translated — an
explicit model id passes through untouched. The three built-in *role* aliases
(``fast``/``general``/``smart``) resolve to the single configured native model
unless the operator remapped them via ``[models.aliases]``, so stock config's
``extraction_model``/``curation_model = "general"`` never reaches the wire as a
literal alias string (NB-3).
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import hashlib
import json
import logging
import re
import statistics
import threading
import time
from pathlib import Path
from urllib.parse import urlparse

from istota import __version__ as _ISTOTA_VERSION
from istota import usage as usage_types
from istota.agent.events import AgentEvent, _describe_tool_use, _tool_invocation
from istota.agent.loop import run_agent_loop, run_agent_loop_continue
from istota.agent.sanitize import sanitize_tool_pairs
from istota.agent.types import (
    AgentContext,
    AgentLoopConfig,
    AgentMessage,
    StopDecision,
)
from istota.llm import make_provider
from istota.llm.catalog import get_model_info
from istota.llm.provider import (
    StreamDone,
    StreamError,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
)
from istota.llm.types import (
    AssistantMessage,
    ImageContent,
    Message,
    TextContent,
    ToolResultMessage,
    UserMessage,
)
from istota.session.compaction import (
    compact_messages,
    derive_keep_recent_tokens,
    derive_reserve_tokens,
    estimate_context_tokens,
    find_cut_point,
    plan_image_pin,
    should_compact,
)
from istota.session.loop_detection import detect_repeated_tool_calls
from istota.session.messages import CompactionSummaryMessage
from istota.session.retry import classify_error, extract_status_code
from istota.session.session_log import (
    SessionLogIdentity,
    SessionLogPolicy,
    SessionLogWriter,
    resolve_session_log_dir,
)
from istota.session.usage import TaskUsage

from ._aliases import CANONICAL_ROLES, split_effort
from ._roles import get_alias_override_target, get_alias_overrides
from ._types import BrainRequest, BrainResult, ImageInput
from .claude_code import is_usage_limit_error

logger = logging.getLogger("istota.brain.native")

# What a usage row records as the brain that ran. One of KNOWN_BRAIN_KINDS.
BRAIN_KIND = "native"

# Process-global coordinator for the live OpenRouter model-catalog fetch
# (ISSUE-182). Each task runs on its own worker thread + event loop, so without
# a guard every first task would stampede OpenRouter's /models endpoint. The
# lock serializes the first-use fetch (held across the network call — simple,
# and the stampede window at process start is tiny given low task concurrency);
# ``_CATALOG_FETCHED_AT`` records the wall time of the last successful install
# so a task within the TTL skips the fetch entirely. Reset in tests.
_CATALOG_FETCH_LOCK = threading.Lock()
_CATALOG_FETCHED_AT: float | None = None


def _reset_catalog_fetch_state() -> None:
    """Test hook: forget the process-global fetch timestamp so a test can drive
    ``_ensure_fetched_catalog`` from a clean slate."""
    global _CATALOG_FETCHED_AT
    _CATALOG_FETCHED_AT = None


# What the model is told instead of an image it will not receive. Both name the
# image, because "attached" alone is not evidence of sight: the failure this
# whole path exists to prevent is a confident answer about an image the model
# never saw, and silence is what produces it.
_NO_VISION_NOTICE = (
    "[image {name} omitted: selected model does not declare vision support; "
    "OCR context may still be available]"
)
_UNREADABLE_NOTICE = "[image {name} omitted: could not be read at send time ({reason})]"

# Refuse to read a prepared image larger than this. `image_attachments` already
# bounds a task's whole payload (`MAX_ENCODED_BYTES`, 8 MiB of base64), but it
# bounds the file as it stood at preparation time — and this is a *second* read,
# of a path under the user temp dir, which bwrap binds read-write into that
# user's own sandboxes. Another task of the same user can replace the file
# between the two reads, so the bound is asserted here rather than inherited.
# Restated rather than imported: a brain module importing `image_attachments`
# would close a cycle back through `brain/__init__.py`, so the two are held
# equal by `tests/native/test_input_images.py` instead.
_MAX_IMAGE_BYTES = 6 * 1024 * 1024


def _read_image_bytes(path: Path) -> bytes:
    """The prepared file's bytes, refusing one that outgrew its budget."""
    size = path.stat().st_size
    if size > _MAX_IMAGE_BYTES:
        raise ValueError(f"{size} bytes exceeds the {_MAX_IMAGE_BYTES}-byte cap")
    return path.read_bytes()


def _initial_user_content(
    prompt: str,
    images: list[ImageInput] | None,
    supports_vision: bool,
    model: str = "",
) -> list:
    """Build the first user message's content blocks.

    Text prompt first, then one image block each, which is the order
    OpenRouter's image-understanding guide documents — a stated contract rather
    than an incidental choice.

    Encoding happens here, immediately before the first provider call, so the
    base64 exists for exactly as long as the request does and never rides on the
    ``BrainRequest``. A file that vanished, or grew, between preparation and the
    send becomes a named text notice and the remaining images still go: one bad
    attachment must not cost the task its other images or its text.

    With no declared vision support nothing is read at all — reading bytes only
    to discard them is pure cost — and each image gets its own named omission so
    the request stays valid and the model is told exactly what it is missing.
    """
    content: list = [TextContent(text=prompt)]
    images = images or []
    if images and not supports_vision:
        # The model-facing notice says this, but only the model reads it. An
        # operator seeing every attachment dropped needs to be able to tell a
        # model that has no vision from a catalog that never learned it has:
        # `supports_vision` defaults to False, and the live catalog is fetched
        # for OpenRouter base URLs only, so a direct-Anthropic endpoint resolves
        # to `_DEFAULT` and reads as no-vision until an override says otherwise.
        logger.warning(
            "native brain: %d image attachment(s) sent as text notices — model "
            "%r declares no vision support; set [brain.native.model_overrides] "
            "supports_vision if the catalog is wrong about it",
            len(images), model,
        )
    for image in images:
        name = image.display_name or Path(image.path).name
        if not supports_vision:
            content.append(TextContent(text=_NO_VISION_NOTICE.format(name=name)))
            continue
        try:
            encoded = base64.b64encode(_read_image_bytes(Path(image.path))).decode("ascii")
        except Exception as e:  # noqa: BLE001 — one bad file must not fail the task
            # Metadata only: no bytes, no base64, and the basename rather than
            # the full user-controlled path.
            logger.warning(
                "native brain: prepared image %s unreadable (%s)",
                name, type(e).__name__,
            )
            content.append(
                TextContent(
                    text=_UNREADABLE_NOTICE.format(name=name, reason=type(e).__name__)
                )
            )
            continue
        content.append(
            ImageContent(
                media_type=image.media_type, data=encoded, display_name=name
            )
        )
    return content


# Compact coding-hygiene block prepended to the native brain's system prompt on
# tool-bearing tasks (empty allowed_tools — e.g. the sleep cycle — gets no
# coding prompt). Generic hygiene only: project-specific conventions come from
# the executor's storage framing and the repo's own guidance files, not here.
# The claude_code/tmux brains take their prompt from the CLI, so this never
# reaches them — the coding steering is scoped to exactly the native path.
CODING_SYSTEM_PROMPT = """\
You are operating as a coding agent. Work carefully and verify your changes.

- Read a file before editing it; base edits on its current contents, not memory.
- Prefer the `Edit` tool over `Write` for existing files. Use `Write` only for a
  new file or a full rewrite.
- To change several separate locations in one file, make a single `Edit` call
  with multiple entries in `edits[]` rather than many calls.
- Keep each `old_string` minimal but unique — include just enough surrounding
  context to be unambiguous; do not pad it with large unchanged regions.
- After changing code, verify it: run the project's tests or build via `Bash`
  when feasible.
- Be concise in your explanations.

Additional tools may be available beyond the ones already described; discover
them as needed."""

_API_RETRY_MAX_ATTEMPTS = 3
_API_RETRY_BASE_DELAY = 5.0
_API_RETRY_MAX_DELAY = 120.0

# Built-in role aliases — the portable CANONICAL_ROLES (single source of truth,
# shared with claude_code's DEFAULT_ALIASES tier keys). On the native brain these
# all resolve to the one configured endpoint model unless the operator remapped
# them via [models.aliases] — so stock config's extraction_model/curation_model=
# "general" never reaches the wire as the literal string "general" (NB-3), and a
# portable role survives a cross-provider fallback.
_BUILTIN_ROLE_NAMES = CANONICAL_ROLES

# URL harvester for require_url_provenance (Stage 3b). Matches http(s) URLs in
# free text; trailing punctuation the wrapping prose contributes is trimmed.
_URL_RE = re.compile(r"https?://[^\s<>\"'`\])}]+", re.IGNORECASE)


def _extract_urls(text: str) -> frozenset[str]:
    if not text:
        return frozenset()
    return frozenset(m.rstrip(".,;:!?") for m in _URL_RE.findall(text))

# The BrainResult.stop_reason vocabulary the executor documents. The loop's raw
# agent_end reasons are normalized into this set so a future stop_reason-keyed
# dispatch can't be surprised (NB-18). The two agent-loop backstops —
# ``max_turns`` / ``loop_detected`` — are first-class members (not collapsed
# to "completed") so a capped/looping run stays visible in ``stop_reason``
# rather than masquerading as a natural completion.
_DOCUMENTED_STOP_REASONS = frozenset(
    {
        "completed", "cancelled", "timeout", "oom", "transient_api_error",
        "usage_limit", "error", "not_found",
        "max_turns", "loop_detected", "soft_timeout",
    }
)

# Best-effort transient signal for arbitrary OpenAI-compatible error bodies
# (native talks to any endpoint, so there is no ``API Error: NNN`` shape to
# parse). Matches the HTTP status the provider layer stamps into its
# ``StreamError`` text (``HTTP 429: …`` / ``HTTP 503: …``).
_NATIVE_TRANSIENT_STATUS_RE = re.compile(r"HTTP (429|5\d\d)\b")

# The provider layer wraps every connect/timeout/transport failure as
# ``Connection error: <exc>``. That is a capacity/connectivity signal, not a
# request-shaped one, so it reroutes to the fallback brain rather than
# dead-ending (ISSUE-212).
_NATIVE_NETWORK_RE = re.compile(
    r"connection error|connection (?:reset|refused|aborted|closed)|"
    r"time[d]? ?out|socket hang ?up|fetch failed|getaddrinfo|"
    r"\b(ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND)\b",
    re.IGNORECASE,
)


def _classify_native_error(text: str) -> str:
    """Classify a native error body into ``usage_limit`` / ``transient_api_error``
    / ``error``.

    A quota/billing/subscription exhaustion → ``usage_limit`` (reroutes to the
    fallback brain). A transient overload/rate-limit 429, a 5xx, or a
    network-level failure → ``transient_api_error``. Anything else stays a
    generic ``error``. Usage-limit is checked first so a quota 429 doesn't read
    as a plain transient. Best-effort and tunable against the operator's actual
    endpoint.

    A request-shaped status (400/401/403/404/413 …) is authoritative and short-
    circuits the text heuristics: retrying it, or paying for a fallback attempt
    that would fail identically, buys nothing (ISSUE-212).
    """
    if is_usage_limit_error(text):
        return "usage_limit"
    status = extract_status_code(text or "")
    if status is not None:
        if status == 429 or status >= 500:
            return "transient_api_error"
        return "error"
    low = (text or "").lower()
    if (
        _NATIVE_TRANSIENT_STATUS_RE.search(text or "")
        or "overloaded" in low
        or "rate limit" in low
        or _NATIVE_NETWORK_RE.search(text or "")
    ):
        return "transient_api_error"
    return "error"


def _compaction_input_chars(window: int) -> int:
    """Char budget for the text fed to the summarizer so the summary request
    itself fits the window (NB-10). ~3 tokens of window as chars (window*3 ≈
    window*4 chars minus room for the summary prompt scaffolding + output),
    floored so a tiny/zero window still yields a usable budget."""
    return max(8000, window * 3)

# Reactive overflow recovery: how many force-compact + continue attempts a single
# task may make before giving up and returning the overflow error. Bounded so a
# genuinely too-large single turn can't thrash compaction forever.
_MAX_OVERFLOW_RECOVERIES = 2

_RECOVERY_NUDGE = "[context was compacted; continue]"

# NB-15: markers appended to a final answer whose last turn ended on a
# non-clean finish reason, so a truncated/filtered response is visibly flagged
# rather than delivered as a complete one. Keyed on the provider's mapped
# stop_reason (see openai_compat._FINISH_REASON_MAP).
_TRUNCATION_MARKERS = {
    "max_tokens": "[truncated: the response hit the output token limit]",
    "content_filter": "[note: the response was cut short by the model provider's content filter]",
}

# Stop reasons whose delivery carries a marker saying the answer is incomplete
# (see _build_result). Only there may the run fall back to the last text-bearing
# turn when the final turn produced none — everywhere else that would ship
# mid-flight narration as a clean answer (ISSUE-211).
#
# ``soft_timeout`` joins the two backstops rather than the hard ``timeout``
# because it is the same *kind* of stop: the loop decided to end a run that was
# still coherent, at a boundary of its own choosing, with the work intact. The
# hard timeout is the opposite — a turn cut mid-stream — and stays outside
# (ISSUE-373).
# The marker each partial-answer stop delivers its text under. This table is the
# declaration; the frozenset below is derived from it, so a fourth stop added to
# one cannot go missing from the other — `_build_result` subscripts the table
# unguarded, and a `KeyError` there turns a salvageable run into an exception on
# the result-construction path.
_PARTIAL_ANSWER_MARKERS = {
    "max_turns": (
        "(stopped: reached the maximum number of steps without a final answer)"
    ),
    "loop_detected": (
        "(stopped: detected a repeating tool-call loop with no progress)"
    ),
    "soft_timeout": (
        "(stopped: ran out of time before reaching a final answer — this is "
        "the work as it stood)"
    ),
}

_PARTIAL_ANSWER_STOP_REASONS = frozenset(_PARTIAL_ANSWER_MARKERS)

# How a mid-flight steer (`!steer`) is framed when injected as a user turn. The
# explicit wording tells the model the message is *additive* — a live nudge, not
# a correction that invalidates work already in progress — so it adjusts course
# without discarding what it has done or losing the thread.
_STEER_FRAME = (
    "[The user sent this while you were working. Adjust course as needed; "
    "you don't have to abandon work already in progress.] {text}"
)

# How often the background steer poller reads the control channel, matching the
# cancel-poll cadence.
_STEER_POLL_INTERVAL_SECONDS = 2.0

# Turn-budget nudge (ISSUE-187 defect 3). The notice is wire-role *user* (the LLM
# layer has no mid-conversation system role, and Anthropic rejects one), so the
# frame carries the "this is environment metadata, not a new user instruction"
# semantics explicitly — the same distinction the steer frame draws in the other
# direction. The budget is always framed as a *shrinking* resource ("~N
# remaining"), never an upfront allotment, so the number reads as a ceiling
# running out rather than a target to spend to (the anchoring pitfall).
_TURN_BUDGET_FRAME = "[Automatic system notice — not from the user: {body}]"

# Upfront pacing line appended to the coding system prompt when the nudge is on
# (mechanism A). Deliberately NON-numeric: stating the cap value up front anchors
# it as a target and compounds sprawl on exactly the tasks that hit the cap.
_TURN_BUDGET_UPFRONT = (
    "Work efficiently. If you cannot fully finish the task, produce the best "
    "deliverable you can rather than leaving the work mid-stream."
)


def _turn_budget_nudge_message(remaining: int, phase: str) -> UserMessage:
    """Frame a budget notice for injection as an environment note.

    ``phase`` is ``"early"`` (the ~halfway reminder), or ``"late"`` (an
    absolute-remaining wrap-up / urgent notice, escalating as ``remaining``
    shrinks). Leads with the absolute steps-remaining so the framing is
    anchoring-resistant.
    """
    if phase == "early":
        body = (
            f"you're about halfway through this task's step budget "
            f"(~{remaining} steps remaining). Keep it in mind: if you can't "
            f"complete the request, prioritize delivering the best partial answer "
            f"you can rather than leaving the work mid-stream"
        )
    elif remaining <= 5:
        body = (
            f"only ~{remaining} steps remain before this task stops "
            f"automatically. Deliver your best answer now, even if partial"
        )
    else:
        body = (
            f"~{remaining} steps remain before this task stops automatically. "
            f"Start wrapping up — if you can't finish, summarize what you have "
            f"and deliver a partial answer now"
        )
    return UserMessage(content=[TextContent(text=_TURN_BUDGET_FRAME.format(body=body))])


# How many recent turn latencies the clock-based estimate averages over, and
# the fewest it will act on. A rolling window rather than the whole run because
# pace changes within a task — a long grep phase and a long write phase are not
# the same speed, and the estimate that matters is the one for the turns still
# to come.
_LATENCY_WINDOW = 5
_LATENCY_SAMPLES_MIN = 3


def _turns_left_by_clock(
    seconds_left: float | None, recent_latencies: list[float]
) -> int | None:
    """How many more turns the wall clock has room for, or None if unknowable.

    ISSUE-373: the nudge ladder counts turns, but on a slow brain the constraint
    that actually ends the run is time. This converts one budget into the other
    using a rolling mean of recent turn latency, so the ladder can be run
    against whichever of the two is scarcer.

    Needs at least ``_LATENCY_SAMPLES_MIN`` samples: a single slow first turn
    (cold connection, a large prompt) is not a pace, and estimating from it
    would fire the urgent notices on turn 2 of a run with an hour left.

    **The median, not the mean**, because turn latency is heavy-tailed by
    nature: one `npm install` or one full test run is minutes where its
    neighbours are seconds. A mean lets that single sample set the budget — four
    10s turns and one 400s build average to 88s, which collapses a 100-turn cap
    to 30 and spends the whole nudge ladder on a spike, in one turn, telling the
    model "~15 steps remain" about a run that then continues for 70 more. The
    thresholds are marked fired when crossed, so nothing fires again when the
    pace recovers and the real crossing arrives. The median ignores an outlier
    and only moves once most of the recent window is genuinely slow, which is
    the condition the estimate is meant to describe.
    """
    if seconds_left is None or not recent_latencies:
        return None
    if len(recent_latencies) < _LATENCY_SAMPLES_MIN:
        return None
    typical = statistics.median(recent_latencies)
    if typical <= 0:
        return None
    return max(0, int(seconds_left // typical))


def _pick_turn_budget_nudge(
    turns: int,
    max_turns: int,
    early_percent: int,
    remaining_levels: list[int],
    fired: set[str],
    turns_left_by_clock: int | None = None,
) -> tuple[int, str] | None:
    """Decide which (if any) budget threshold to surface this turn.

    Returns ``(remaining, phase)`` for the most urgent unfired threshold that has
    been crossed, or ``None``. Mutates ``fired`` — every crossed threshold is
    marked fired (so a less-urgent one that was overtaken can't fire stale
    later), and each threshold fires at most once. ``turns`` is counted from the
    loop's ``new_messages`` accumulator (monotonic across compaction), so the
    same threshold never re-fires after a context shrink.

    ``turns_left_by_clock`` (ISSUE-373) is the same budget expressed from the
    wall clock. When it is scarcer than the turn cap it *becomes* the budget:
    the whole ladder — the early reminder's percentage included — is computed
    against ``turns + turns_left_by_clock`` rather than against ``max_turns``.
    Collapsing the two into one effective budget rather than running two ladders
    keeps each threshold firing once, whichever resource crossed it, and keeps
    the message honest: the number the model reads is always the number of steps
    it actually has left. The ``fired`` keys are unchanged, so a threshold
    crossed on the clock cannot be re-fired later by the turn count.
    """
    if not max_turns or max_turns <= 0:
        return None
    budget = max_turns
    if turns_left_by_clock is not None:
        budget = min(budget, turns + turns_left_by_clock)
    remaining = budget - turns
    # (urgency_rank, key, phase) — lower rank = more urgent (fewer remaining).
    crossed: list[tuple[int, str, str]] = []
    for level in sorted({int(x) for x in remaining_levels}):
        if remaining <= level:
            crossed.append((level, f"remaining:{level}", "late"))
    if 0 < early_percent <= 100:
        early_turn = -(-budget * early_percent // 100)  # ceil
        if turns >= early_turn:
            # Least urgent — sort behind every late level.
            crossed.append((max_turns + 1, "early", "early"))
    if not crossed:
        return None
    unfired = [c for c in crossed if c[1] not in fired]
    for _, key, _ in crossed:
        fired.add(key)
    if not unfired:
        return None
    unfired.sort(key=lambda c: c[0])
    _, _, phase = unfired[0]
    return remaining, phase


# --------------------------------------------------------------------------- #
# Session log (per-attempt JSONL transcript)
# --------------------------------------------------------------------------- #


def _disabled_session_log() -> SessionLogWriter:
    """A writer with no root: every method a no-op, ``path`` ``None``.

    A fresh instance rather than a module-level singleton — the class holds
    per-run state and tasks run concurrently on their own threads, so sharing
    one would be a shared mutable across workers for no gain.
    """
    return SessionLogWriter(None, SessionLogIdentity(0, 0, ""), SessionLogPolicy())


def make_session_log(req: BrainRequest, cfg) -> SessionLogWriter:
    """The per-attempt transcript writer for *req*, or the disabled one.

    ``SessionLogWriter(root=None)`` is the off switch — every method a no-op —
    which is the whole reason there is no ``if self._log is not None`` at any
    of the call sites below. Three conditions produce it: the feature is off,
    the caller has no task id, or it has no user id. The last two are the same
    case in practice — a direct brain call (the sleep cycle, the REPL, a test)
    is not a task attempt and has nothing to name a file after.

    Resolving the directory is a pure function and constructing the writer
    opens nothing, so the first filesystem access is ``open()``, which carries
    the never-raises contract itself. This function reads a config object and
    coerces two request fields, and the caller wraps it rather than this
    claiming to be infallible: the writer's contract covers what happens inside
    it, never the construction of its arguments.
    """
    ident = SessionLogIdentity(
        task_id=int(getattr(req, "task_id", 0) or 0),
        attempt=int(getattr(req, "attempt", 0) or 0),
        user_id=getattr(req, "user_id", "") or "",
        source_type=getattr(req, "source_type", "") or "",
        conversation_token=getattr(req, "conversation_token", "") or "",
        is_group_chat=bool(getattr(req, "is_group_chat", False)),
    )
    policy = SessionLogPolicy(
        max_content_chars=cfg.max_content_chars,
        max_args_chars=cfg.max_args_chars,
        include_thinking=cfg.include_thinking,
    )
    if not cfg.enabled or ident.task_id <= 0 or not ident.user_id:
        return SessionLogWriter(None, ident, policy)
    return SessionLogWriter(resolve_session_log_dir(req.db_path, cfg.dir), ident, policy)


def _base_url_host(base_url: str) -> str:
    """The host of the configured endpoint, and nothing else.

    An operator can put a token in a URL *path*, so the whole ``base_url`` must
    never reach the header; the host is the part with diagnostic value.
    """
    try:
        return urlparse(str(base_url or "")).hostname or ""
    except Exception:  # noqa: BLE001 — a header field must not fail a task
        return ""


def _tools_schema_sha(tools) -> str:
    """SHA-256 over the sorted tool schemas.

    Names go in the record whole; the schemas are large, near-identical across
    tasks, and it is their *drift* the hash is there to expose.
    """
    try:
        payload = sorted(
            (dataclasses.asdict(t.schema) for t in (tools or [])),
            key=lambda d: d.get("name", ""),
        )
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()
    except Exception:  # noqa: BLE001 — see above
        return ""


def _estimated_tokens(messages) -> int | None:
    """``estimate_context_tokens`` for a record field, never raising.

    On the proactive path the estimate is a product value the compaction
    decision already needed. On the overflow path nothing but the record wants
    it, and a logging-only computation must not be able to turn a recoverable
    context overflow into a failed task — the writer's own never-raises
    contract covers what happens *inside* it, not the arguments handed to it.
    """
    try:
        return estimate_context_tokens(messages)[0]
    except Exception:  # noqa: BLE001
        return None


def _carries_image(msg) -> bool:
    """Whether *msg* holds an image block.

    How the overflow path answers `image_pinned` without a second copy of
    ``plan_image_pin``'s rule: the pin is one of the messages
    ``_build_recovery_context`` added that were not in the transcript, and it is
    the only one of them that carries an image.
    """
    try:
        return any(isinstance(b, ImageContent) for b in getattr(msg, "content", ()) or ())
    except Exception:  # noqa: BLE001
        return False


def _details_dict(details) -> dict | None:
    """``CompactionDetails`` as JSON, since the writer serializes with stdlib
    ``json`` and a dataclass would land as a ``serialization_error`` line."""
    if details is None:
        return None
    try:
        return dataclasses.asdict(details)
    except Exception:  # noqa: BLE001
        return None


def _usage_dict(usage) -> dict | None:
    if usage is None:
        return None
    try:
        return dataclasses.asdict(usage)
    except Exception:  # noqa: BLE001
        return None


def _drain_one_steer(buffer: list) -> list:
    """Pop one buffered steer text and return it framed as a user turn.

    One-per-call so a burst of steers becomes successive user turns
    (steering_queue_mode="one_at_a_time") rather than one concatenated blob, and
    self-clearing (the agent loop re-polls each turn; a source that re-returned
    the same message would loop forever). Empty buffer → ``[]``.
    """
    if not buffer:
        return []
    text = buffer.pop(0)
    return [UserMessage(content=[TextContent(text=_STEER_FRAME.format(text=text))])]


class _RetryingProvider:
    """Wrap a provider with turn-level retry on *immediate* transient errors.

    Transient failures (429 / 5xx / overloaded) surface as a single
    ``StreamError`` before any content delta — the OpenAI-compatible provider
    yields it the moment a non-200 response arrives. We retry only in that
    window: once any text / tool-call delta has been forwarded, the turn is
    committed and a later error passes straight through (a half-streamed turn
    can't be cleanly replayed).

    Putting retry here — at single-completion granularity — keeps it correct for
    multi-turn tasks: re-issuing one request never replays already-executed
    tools. ``abort`` makes the backoff sleep interruptible.
    """

    def __init__(self, inner, abort: asyncio.Event | None):
        self._inner = inner
        self._abort = abort

    async def stream(
        self,
        system_prompt,
        messages,
        tools,
        *,
        model="",
        max_tokens=16384,
        reasoning_effort=None,
        render_tool_images=False,
    ):
        attempt = 0
        while True:
            committed = False
            pending_error: StreamError | None = None

            async for event in self._inner.stream(
                system_prompt,
                messages,
                tools,
                model=model,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                render_tool_images=render_tool_images,
            ):
                if isinstance(event, StreamError) and not committed:
                    pending_error = event
                    break
                # Only content-bearing events commit the turn. A StreamStart (or
                # any other zero-payload event a provider might emit) is forwarded
                # without committing, so a StreamError arriving *after* a start —
                # but before any real delta — is still retryable. Committing on
                # StreamStart would silently defeat transient-error retry for any
                # provider that announces the stream before failing.
                if isinstance(event, (TextDelta, ToolCallDelta, StreamDone)):
                    committed = True
                yield event

            if pending_error is None:
                return

            cls = classify_error(pending_error.message.error_message or "")
            if not cls.retryable or attempt >= _API_RETRY_MAX_ATTEMPTS:
                yield pending_error
                return

            attempt += 1
            if cls.retry_after is not None:
                # Obey the provider's own Retry-After (ISSUE-212), still capped.
                delay = min(cls.retry_after, _API_RETRY_MAX_DELAY)
            else:
                delay = min(
                    _API_RETRY_BASE_DELAY * (2 ** (attempt - 1)), _API_RETRY_MAX_DELAY
                )
            logger.warning(
                "native provider transient error (attempt %d/%d), waiting %.1fs: %s",
                attempt,
                _API_RETRY_MAX_ATTEMPTS,
                delay,
                (pending_error.message.error_message or "")[:200],
            )
            if self._abort is not None:
                try:
                    await asyncio.wait_for(self._abort.wait(), timeout=delay)
                    yield pending_error  # aborted during backoff
                    return
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(delay)
            # loop to retry


class NativeBrain:
    """Istota-owned agent loop, provider-agnostic, behind the Brain protocol."""

    # The in-process agent loop already polls ``get_steering_messages`` at every
    # boundary, so mid-flight steering (`!steer`) is first-class here — the
    # executor wires ``req.poll_steers`` into that callback.
    supports_steering = True

    # This brain speaks an OpenAI-compatible endpoint (OpenRouter in practice).
    # Operators key an ``[models.aliases.<name>]`` sub-table on this string; a
    # value written here is an endpoint slug and is sent verbatim, never
    # translated from the Anthropic namespace.
    model_namespace = "openai_compat"

    def __init__(self, config, provider=None):
        self._config = config
        # ``provider`` injectable for tests; production builds from config.
        self._provider = provider if provider is not None else make_provider(config)
        # We own (and must close) a provider we built ourselves; an injected one
        # belongs to the caller (NB-17). A fresh NativeBrain + provider is built
        # per task, so its httpx client is closed at task end rather than leaked.
        self._owns_provider = provider is None

    # --- Model resolution --------------------------------------------------
    #
    # The only provider is ``openai_compat``, which can point at any endpoint
    # (Anthropic, OpenRouter, a local qwen, …). Anthropic aliases must NOT be
    # translated — sending "claude-opus-5" to a qwen endpoint would fail — so
    # explicit ids pass through and only operator [models.aliases] overrides
    # resolve.

    def resolve_alias(self, alias):
        if not alias:
            return None
        # Split an optional ``:effort`` modifier first (``smart:low``,
        # ``anthropic/claude-sonnet-4:high``); the ``/`` in a slug is untouched.
        base, suffix_effort = split_effort(alias)
        base_lower = base.lower()
        # 1. Operator-overridden alias — read THIS brain's namespace only, so an
        # anthropic-namespace value (e.g. "opus") never leaks onto the OpenRouter
        # wire. The slug passes through verbatim; no translation. The suffix
        # modifier wins over the override's own effort.
        rt = get_alias_override_target(base_lower, self.model_namespace)
        if rt is not None:
            # A legacy-flat value may carry a baked ``:effort`` (``smart =
            # "opus:high"``); peel it so native resolves the same flat string
            # claude_code does, instead of gluing ``:high`` onto the wire model.
            # A provider slug's ``/`` is untouched by split_effort. Effort
            # precedence: request suffix > explicit RoleTarget.effort > baked.
            target_model, target_effort = (
                split_effort(rt.model) if rt.model else (rt.model, None)
            )
            return (target_model, suffix_effort or rt.effort or target_effort)
        # A built-in role alias with no operator override resolves to the single
        # model this endpoint is configured for (NB-3). The native brain speaks
        # to one endpoint with one model, so fast/general/smart all mean "the
        # configured model" unless the operator remapped them via [models.aliases].
        # This must come BEFORE the generic ``:effort`` passthrough below, or a
        # role tier + effort with an unset model (``general:high``) would fall
        # through and leak the literal "general" onto the wire (NB-3). An empty
        # model collapses to None → resolve_model_name treats it as brain default.
        # Provider shortcuts (opus/sonnet/haiku) are NOT roles and pass through
        # untranslated (None unless an explicit effort modifier was given).
        if base_lower in _BUILTIN_ROLE_NAMES:
            return (self._config.model or None, suffix_effort)
        # An explicit id/slug carrying a ``:effort`` modifier passes through with
        # the effort applied; a plain unknown id stays None (unchanged — the
        # caller's resolve_model_name handles bare-id passthrough).
        if suffix_effort is not None:
            return (base, suffix_effort)
        return None

    def resolve_model_name(self, name):
        if not name:
            return ""
        resolved = self.resolve_alias(name)
        if resolved is not None and resolved[0]:
            return resolved[0]
        base, _effort = split_effort(name)
        # An unoverridden role with an empty native model must still not reach
        # the wire as the literal "general"/"fast"/"smart" — collapse to the
        # (empty) configured model, which downstream treats as "brain default".
        if base.lower() in _BUILTIN_ROLE_NAMES:
            return self._config.model
        return base  # explicit id pass-through (effort stripped); no translation

    def list_aliases(self):
        overrides = get_alias_overrides()
        listed: list[tuple[str, str | None, str | None]] = []
        seen: set[str] = set()
        # Built-in roles first: operator override (in THIS brain's namespace) if
        # present, else the native model. So `!models` shows the truthful
        # resolved table on native.
        for role in _BUILTIN_ROLE_NAMES:
            rt = get_alias_override_target(role, self.model_namespace)
            if rt is not None:
                listed.append((role, rt.model, rt.effort))
            else:
                listed.append((role, self._config.model, None))
            seen.add(role)
        # Any custom operator alias names beyond the three defaults.
        for name in overrides:
            if name in seen:
                continue
            rt = get_alias_override_target(name, self.model_namespace)
            if rt is not None:
                listed.append((name, rt.model, rt.effort))
        return listed

    def validate_alias_override(self, name, target):
        # No alias table to validate against for an arbitrary endpoint.
        return []

    # --- Execution ---------------------------------------------------------

    def execute(self, req: BrainRequest) -> BrainResult:
        """Sync wrapper: run the async loop on a fresh event loop.

        The scheduler calls brains from a thread pool, so ``asyncio.run`` here is
        safe — each task gets its own loop.
        """
        result = self._execute_sync(req)
        # Stamped on the way out, so a return added later cannot forget it.
        result.brain_kind = BRAIN_KIND
        return result

    def _execute_sync(self, req: BrainRequest) -> BrainResult:
        # The session log is built here rather than inside the async body so
        # that the `except` below — the one that catches everything
        # `asyncio.run` lets through — can write the `error` record, and so
        # that the terminal `result` record and the `close()` sit on the *one*
        # path every return in `_execute_async` and every return in
        # `_build_result` funnels through. The spec asks for `result()` at each
        # of those four returns; one site downstream of all of them says the
        # same thing and keeps saying it when a fifth is added.
        #
        # Constructed inside the `try`, because before this change *nothing* in
        # this method ran outside it: a construction that raised would be the
        # one path by which a logging feature crashed a worker, which is the
        # single thing the writer's whole contract exists to prevent.
        stats = {"turns": 0, "compactions": 0}
        started = time.monotonic()
        log = _disabled_session_log()
        try:
            log = make_session_log(req, self._config.session_log)
        except Exception:  # noqa: BLE001
            logger.warning("could not build the session log; running without one",
                           exc_info=True)

        async def _run_and_close() -> BrainResult:
            try:
                return await self._execute_async(req, log, stats)
            finally:
                # Close the per-task httpx client on the loop it was used on, so
                # a long-running daemon doesn't leak an fd/socket per task
                # (NB-17, ISSUE-101 class). Only when we own the provider.
                await self._maybe_close_provider()

        try:
            try:
                result = asyncio.run(_run_and_close())
            except Exception as e:  # noqa: BLE001 — never let the brain crash the worker
                logger.exception("NativeBrain.execute raised")
                # Both records, in this order: `error` says what went wrong,
                # `result` says what the task was told.
                log.error(e)
                result = BrainResult(
                    success=False,
                    result_text=f"Execution error: {e}",
                    stop_reason="error",
                    model_used=req.model or self._config.model,
                )
            except BaseException as e:
                # `CancelledError`, `KeyboardInterrupt`, `SystemExit`. The
                # clause above deliberately does not widen to these — the
                # worker's behaviour on them is not this stage's to change — so
                # no `BrainResult` exists and no `result` record is written.
                # The `error` record is what stops the file being an
                # unexplained truncation, and "an `error` record and no
                # `result`" is exactly what the spec says a run that produced
                # no result should look like.
                log.error(e)
                raise
            log.result(
                success=result.success,
                stop_reason=result.stop_reason,
                # Deliberately uncapped: it is the deliverable, the same
                # reasoning that put `result` in `events._UNCAPPED_EVENT_KINDS`.
                result_text=result.result_text,
                model_used=result.model_used,
                duration_ms=int((time.monotonic() - started) * 1000),
                usage=_usage_dict(result.usage),
                turns=stats["turns"],
                compactions=stats["compactions"],
                # So a reader can tell "the model saw a short result" from
                # "the log is short".
                truncated_records=log.truncated_records,
            )
            return result
        finally:
            log.close()

    @staticmethod
    def _catalog_cache_dir(req: BrainRequest) -> Path | None:
        """Local, writable dir for the OpenRouter cache = ``db_path.parent``.

        Taken from ``BrainRequest.db_path``, which the executor sets from
        ``config.db_path``. It used to read ``ISTOTA_DB_PATH`` out of the task
        env; that var no longer reaches the sandbox env at all (it is routed to
        the skill proxy instead), and this is a daemon-side path anyway.

        Absent (a direct brain call that built no task env, e.g. some
        sleep-cycle paths) → ``None``: the in-memory ``_FETCHED`` table + the
        process-global TTL guard still prevent refetch storms, only the
        cross-process disk cache is skipped.
        """
        if not req.db_path:
            return None
        try:
            return Path(req.db_path).parent
        except Exception:  # noqa: BLE001
            return None

    async def _ensure_fetched_catalog(self, req: BrainRequest) -> None:
        """Install the live OpenRouter model catalog into ``istota.llm.catalog``.

        Gated on ``model_catalog_fetch`` + an OpenRouter ``base_url``. Fetches at
        most once per process per TTL (module-level guard). Resolution order:
        fresh disk cache → live fetch (+write cache) → stale disk cache → leave
        the default. Never raises into task execution — a metadata-resolution
        problem must not fail a task.
        """
        try:
            await self._ensure_fetched_catalog_inner(req)
        except Exception:  # noqa: BLE001 — a fetch bug must never break a task
            logger.debug("catalog fetch coordinator raised; using default/overrides", exc_info=True)

    async def _ensure_fetched_catalog_inner(self, req: BrainRequest) -> None:
        global _CATALOG_FETCHED_AT
        cfg = self._config
        if not getattr(cfg, "model_catalog_fetch", False):
            return
        base_url = cfg.base_url or ""
        if "openrouter.ai" not in base_url:
            return

        from ..llm import catalog as _catalog
        from ..llm import openrouter_catalog as orc

        ttl = float(getattr(cfg, "model_catalog_cache_ttl_hours", 24.0) or 0.0)
        now = time.time()

        # Serialize the first-use stampede across worker threads. Held across the
        # network call by design (see the module-level note); a loser thread
        # blocks until the winner installs, then the freshness gate short-circuits
        # it. Off-loop blocking of a waiting thread is acceptable — it has nothing
        # else to do until the catalog is ready.
        _CATALOG_FETCH_LOCK.acquire()
        try:
            # Already installed within TTL this process → nothing to do.
            if _CATALOG_FETCHED_AT is not None:
                if ttl <= 0 or (now - _CATALOG_FETCHED_AT) <= ttl * 3600:
                    return

            data_dir = self._catalog_cache_dir(req)
            path = orc.cache_path(data_dir) if data_dir is not None else None

            # 1) fresh disk cache
            if path is not None:
                fresh = orc.read_cache(path, ttl, now_ts=now)
                if fresh:
                    _catalog.set_fetched_catalog(fresh)
                    _CATALOG_FETCHED_AT = now
                    return

            # 2) live fetch
            entries: dict = {}
            try:
                entries = await orc.fetch_openrouter_catalog(
                    base_url, api_key=cfg.api_key or ""
                )
            except Exception:  # noqa: BLE001 — network/HTTP; fall through
                logger.warning("openrouter model-catalog fetch failed", exc_info=True)
                entries = {}
            if entries:
                _catalog.set_fetched_catalog(entries)
                if path is not None:
                    orc.write_cache(path, entries, now)
                _CATALOG_FETCHED_AT = now
                return

            # 3) stale disk cache — keep serving it, stamp so we don't refetch
            # every task while the endpoint is down (TTL still governs the next
            # attempt).
            if path is not None:
                stale = orc.read_cache_any_age(path)
                if stale:
                    _catalog.set_fetched_catalog(stale)
                    _CATALOG_FETCHED_AT = now
                    logger.warning("using stale openrouter model-catalog cache")
                    return

            # 4) nothing available: leave _FETCHED as-is (→ conservative default),
            # do NOT stamp so the next task retries the network.
            logger.warning(
                "no openrouter model catalog available; using conservative default"
            )
        finally:
            _CATALOG_FETCH_LOCK.release()

    async def _maybe_close_provider(self) -> None:
        if not self._owns_provider:
            return
        aclose = getattr(self._provider, "aclose", None)
        if aclose is None:
            return
        try:
            await aclose()
        except Exception:  # noqa: BLE001 — cleanup is best-effort
            logger.debug("provider aclose raised", exc_info=True)

    async def _execute_async(
        self,
        req: BrainRequest,
        log: SessionLogWriter | None = None,
        stats: dict | None = None,
    ) -> BrainResult:
        # Both default so a direct caller of this method still works. A `None`
        # log is the disabled writer, never a live one: opening a file from a
        # path that did not go through `_execute_sync` would leave nothing to
        # write the terminal `result` record or to close the handle.
        if log is None:
            log = _disabled_session_log()
        if stats is None:
            stats = {"turns": 0, "compactions": 0}
        abort = asyncio.Event()
        cancel_task: asyncio.Task | None = None
        if req.cancel_check is not None:
            # Bridge the scheduler's polling cancel_check into the event the
            # loop, tools, and provider backoff all wait on. A cancel_check
            # failure (e.g. transient SQLite lock contention) must not abort the
            # run — treat it as "not cancelled" and let the poller keep trying.
            try:
                already_cancelled = req.cancel_check()
            except Exception:  # noqa: BLE001 — see above
                logger.debug("initial cancel_check raised; ignoring", exc_info=True)
                already_cancelled = False
            if already_cancelled:
                abort.set()
            else:
                cancel_task = asyncio.create_task(self._poll_cancel(req.cancel_check, abort))

        # Mid-flight steering (`!steer`). A background poller reads the control
        # channel off the loop (same off-loop DB discipline as _poll_cancel) and
        # buffers raw steer texts; the synchronous get_steering_messages callback
        # the loop polls at each boundary drains one from the buffer and frames
        # it as a user turn. Wired only when the executor supplied a channel
        # (steering-capable brain); otherwise the callback is None and behaviour
        # is byte-identical to before.
        steer_buffer: list[str] = []
        steer_task: asyncio.Task | None = None
        if req.poll_steers is not None:
            steer_task = asyncio.create_task(
                self._poll_steers(req.poll_steers, steer_buffer, abort)
            )

        def _get_steering_messages() -> list:
            # Peeked before the drain so the record carries the raw text the
            # user sent rather than `_STEER_FRAME`'s wrapping of it — the frame
            # is already in the `message` record the injection becomes, and a
            # steered run is otherwise unexplainable: a user turn appears in the
            # middle of an agent loop with nothing saying where it came from.
            #
            # The peek and the pop are one synchronous pair with no `await`
            # between them, and `_poll_steers` extends the same list from the
            # same event-loop thread, so there is no interleaving point.
            #
            # The record means *drained*, not *delivered*: the loop drains at
            # the end of a turn and injects at the top of the next, so an abort
            # landing in that window (a `!stop`, or the deadline) leaves the
            # steer in `pending` and the file holds a `steer` with no user
            # message after it. That is the pre-existing lost-steer case made
            # visible rather than a new one — the record is the only evidence
            # anywhere that the steer was consumed.
            raw = steer_buffer[0] if steer_buffer else None
            drained = _drain_one_steer(steer_buffer)
            if drained and raw is not None:
                log.steer(raw)
            return drained

        model = req.model or self._config.model
        provider = _RetryingProvider(self._provider, abort)
        usage = TaskUsage()
        # Cost provenance, for the usage row's `cost_basis`. `TaskUsage.add`
        # decides per turn whether to take the provider's reported cost or fall
        # back to catalog prices, and records nothing about which it did — so
        # the two counters are tracked here rather than by changing that type.
        # Conservative by contract: `api` only when *every* accumulated turn
        # reported a cost. One catalog-priced turn makes the total partly
        # invented, and the catalog prices an unknown model at zero, so a
        # fabricated 0.0 would otherwise be labelled as real spend.
        turns_accumulated = 0
        turns_costed = 0

        def _all_turns_costed() -> bool:
            return turns_accumulated > 0 and turns_costed == turns_accumulated

        # Live model-catalog enrichment (ISSUE-182). Populate the per-model
        # metadata catalog from OpenRouter before the loop resolves windows /
        # capabilities / prices. No-ops for a non-OpenRouter endpoint or when
        # disabled; never fatal.
        await self._ensure_fetched_catalog(req)

        # Resolve the effort tier and capability-gate it. The compat field
        # (``reasoning_effort``) only makes sense for a reasoning model; sending
        # it to a non-thinking endpoint (a local qwen) would 400. The raw tier
        # passes through — the provider folds xhigh/max → high at the wire.
        effort = (req.effort or self._config.effort or "").strip()
        reasoning_effort: str | None = None
        if effort:
            if get_model_info(model).supports_thinking:
                reasoning_effort = effort
            else:
                logger.debug(
                    "native effort ignored: model=%s does not support thinking", model
                )

        # --- session log --------------------------------------------------
        # Opened here, once the model and the effort are resolved, so the
        # header can carry them. `api_key` and `extra_headers` are never in it
        # and `base_url` is reduced to its host — that rule lives at this end,
        # since `session_log.open` copies the mapping through.
        log.open(
            {
                "brain": BRAIN_KIND,
                "provider": self._config.provider,
                "base_url_host": _base_url_host(self._config.base_url),
                "model": model,
                "effort": effort,
                "reasoning_effort": reasoning_effort,
                "max_turns": self._config.max_turns,
                "max_tokens": self._config.max_tokens,
                "context_window": (
                    self._config.context_window or get_model_info(model).context_window
                ),
                # The provider's resolved answer, not the config's tri-state:
                # `make_provider` reads `None` as "on for api.anthropic.com",
                # which is the default deployment, so recording the config
                # field would write `null` for a run that cached. An injected
                # provider (a test double) exposes none, and there the config
                # value is the best available answer.
                "prompt_caching": getattr(
                    self._provider, "prompt_caching", self._config.prompt_caching
                ),
                "cwd": str(req.cwd),
                "istota_version": _ISTOTA_VERSION,
            }
        )

        # --- event accumulation -------------------------------------------
        trace: list[dict] = []
        actions: list[str] = []
        # The agent loop executes a turn's tools *before* it emits that turn's
        # ``turn_end`` (agent/loop.py: _execute_tool_batch then turn_end), so
        # appending tool entries as they fire records them ahead of the text the
        # model wrote first. That inverts document order for every native trace,
        # which misrepresents the run to anything reading the trace back —
        # including the finality rule in session/result.py, which reads "text
        # after the last tool call" as the model's final message. Buffer a
        # turn's tool entries and flush them after its text at ``turn_end``.
        pending_tools: list[dict] = []
        last_assistant_text = ""
        # The final turn's text — the durable answer (ISSUE-211). Distinct from
        # ``last_assistant_text``, which is the last turn that carried *any*
        # text and stays the backstop for the abnormal-stop paths below.
        final_turn_text = ""
        last_error_message = ""
        # The final assistant turn's stop_reason, so a truncated (max_tokens) or
        # filtered (content_filter) answer can be marked visible rather than
        # delivered as a clean completion (NB-15).
        last_assistant_stop = {"value": ""}

        # Final-turn suppression (see spec "NativeBrain integration"): every
        # turn_end carries assistant text, and the *last* text-bearing turn's
        # text is exactly what becomes BrainResult.result_text. Emitting it as
        # a TextEvent too would double-render (progress_text + result) on a
        # single-turn task. We hold each turn's text and only emit it once a
        # later text-bearing turn proves it wasn't the final one — so the
        # final turn's text is never forwarded as progress.
        pending_text: dict[str, str | None] = {"value": None}

        # Token-level answer streaming (streaming-web-chat-responses spec): the
        # loop already fans provider TextDeltas out as ``message_update`` events.
        # We forward each as a ``TextDeltaEvent`` so the executor can stream it
        # to a stream surface (web chat / repl). The brain stays surface-agnostic
        # — it always emits both the per-token deltas *and* the intermediate-turn
        # whole-text ``TextEvent``s below. The executor, which alone knows the
        # surface, decides what to keep: on a stream surface it drops the
        # redundant whole-turn TextEvent once deltas have flowed; on a push
        # surface (Talk) it drops the deltas and forwards the TextEvents as
        # progress_text. Final-turn suppression (below) is unconditional either
        # way — the last turn's text becomes the result.

        async def emit(event: AgentEvent) -> None:
            nonlocal last_assistant_text, final_turn_text, last_error_message
            nonlocal turns_accumulated, turns_costed
            if event.type == "message_update":
                ae = event.assistant_event
                if isinstance(ae, TextDelta) and ae.text:
                    await self._emit_progress(req, _text_delta_event(ae.text))
                elif isinstance(ae, ThinkingDelta) and ae.thinking:
                    # Stream reasoning fragments as ThinkingDeltaEvents. The
                    # assembled ThinkingContent in StreamDone stays as-is (message
                    # fidelity, excluded from result_text) — no double count since
                    # the loop breaks on StreamDone and never emits it as progress.
                    await self._emit_progress(
                        req, _thinking_delta_event(ae.thinking)
                    )
            elif event.type == "message_end":
                # The whole message path, in one branch. The loop emits
                # `message_end` for every message it *appends* — the assembled
                # prompt, each injected steer or follow-up, each assistant turn
                # (including an aborted one) and each tool result, serial or
                # parallel — so user, assistant and tool_result records land in
                # the exact order the run produced them, with no reordering to
                # undo and no second source to keep in sync.
                # `run_agent_loop_continue` shares `_run_loop`, so the
                # overflow-recovery pass is covered by the same branch;
                # measured before this was written, which is why the
                # `tool_execution_end` route the spec held in reserve is not
                # used.
                #
                # "Appends" is the exact word, and the gap it leaves is
                # deliberate. Compaction *replaces* `ctx.messages` wholesale —
                # `prepare_next_turn`'s returned list on the proactive path,
                # `_build_recovery_context`'s on the overflow one — and the
                # loop emits nothing for a replacement. So the summary message,
                # a pinned image and the recovery nudge reach the model without
                # a `message` record of their own; the `compaction` record is
                # what stands for all three, which is why it carries the
                # summary text, `image_pinned` and the drop count rather than
                # just a marker.
                #
                # Deliberately *not* also hooked to `turn_end`: that event
                # carries the same AssistantMessage `message_end` has already
                # emitted, so recording both would write every assistant turn
                # twice.
                #
                # And deliberately *not* routed through `_emit_progress`'s
                # `run_in_executor` hop, though every neighbouring branch is.
                # That hop exists because the executor's callback calls
                # `asyncio.run` internally, which a file append does not, and
                # here ordering matters more than latency: the record sequence
                # is the artifact. After the writer's caps the largest record
                # is bounded at roughly `max_content_chars` plus overhead — a
                # sub-millisecond write to page cache, flushed but never
                # `fsync`ed, so a crashed daemon loses the buffered tail rather
                # than the loop paying for durability per tool result.
                if event.message is not None:
                    log.message(event.message)
                    # Counted here rather than at `turn_end` so the `result`
                    # record's `turns` always equals the number of assistant
                    # `message` records in the same file. A hard cancel can
                    # land between the two events, and two fields of one file
                    # disagreeing about how many turns there were is a worse
                    # artifact than a count that is one low.
                    if isinstance(event.message, AssistantMessage):
                        stats["turns"] += 1
            elif event.type == "tool_execution_start":
                desc = _describe_tool_use(event.tool_name, event.args)
                entry = {"type": "tool", "text": desc}
                inv = _tool_invocation(event.tool_name, event.args)
                if inv:
                    entry["raw"] = inv
                # Held until this turn's turn_end so the trace keeps document
                # order (text the model wrote, then the tools it went on to
                # call). Progress emission below stays live and unbuffered.
                pending_tools.append(entry)
                actions.append(desc)
                await self._emit_progress(
                    req, _tool_use_event(event.tool_name, desc, event.tool_call_id)
                )
            elif event.type == "tool_execution_end":
                await self._emit_progress(
                    req,
                    _tool_end_event(
                        event.tool_name,
                        event.tool_call_id,
                        not event.is_error,
                        event.duration_ms,
                    ),
                )
            elif event.type == "tool_execution_update":
                if event.update_text:
                    await self._emit_progress(
                        req,
                        _tool_progress_event(
                            event.tool_name, event.tool_call_id, event.update_text
                        ),
                    )
            elif event.type == "turn_end":
                msg = event.message
                if isinstance(msg, AssistantMessage):
                    # Turn latency, for the clock-aware nudge (ISSUE-373).
                    # Measured turn-end to turn-end, so it includes the tool
                    # execution between them — the question the estimate answers
                    # is how long the *next* step takes end to end, not how long
                    # the model spends generating.
                    _now = time.monotonic()
                    turn_clock["recent"].append(_now - turn_clock["last"])
                    turn_clock["last"] = _now
                    del turn_clock["recent"][:-_LATENCY_WINDOW]
                    last_assistant_stop["value"] = msg.stop_reason or ""
                    # Capture the provider's error text so _build_result can
                    # surface it; the scheduler only sees result_text, and an
                    # empty error reads as a generic failure (and a policy
                    # refusal would be retried instead of failed-fast).
                    if msg.stop_reason == "error" and msg.error_message:
                        last_error_message = msg.error_message
                    # Accumulate on any turn that carries tokens OR a
                    # provider-reported cost — a costed turn that reports no
                    # token counts (some OpenRouter free/BYOK responses) would
                    # otherwise silently drop its charge from the task total.
                    if msg.usage.total_tokens > 0 or msg.usage.cost_usd is not None:
                        usage.add(msg.usage, get_model_info(model))
                        turns_accumulated += 1
                        if msg.usage.cost_usd is not None:
                            turns_costed += 1
                    text = msg.text.strip()
                    # The durable answer is the *final* turn's text, not the
                    # last turn that happened to carry text: a tool-only or
                    # empty final turn must not promote an earlier turn's
                    # mid-flight narration to the reply (ISSUE-211). The
                    # abnormal-stop paths after the loop still fall back to
                    # last_assistant_text, because there the text is delivered
                    # with a marker that labels it incomplete.
                    final_turn_text = msg.text
                    if text:
                        trace.append({"type": "text", "text": text})
                        last_assistant_text = msg.text
                    # Now the tools this turn went on to call, after its text.
                    if pending_tools:
                        trace.extend(pending_tools)
                        pending_tools.clear()
                    if text:
                        # Flush the previously-held text (now known not to be
                        # final); hold this one. The last text-bearing turn
                        # stays held → suppressed (its text is the result). The
                        # executor dedupes these against streamed deltas
                        # per-surface, so emitting them unconditionally is safe.
                        if pending_text["value"] is not None:
                            await self._emit_progress(
                                req, _text_event(pending_text["value"])
                            )
                        pending_text["value"] = msg.text
                    elif pending_text["value"] is not None:
                        # A turn with no text followed the held block, so that
                        # block is not the answer after all. Release it as
                        # progress instead of suppressing it into nothing.
                        await self._emit_progress(
                            req, _text_event(pending_text["value"])
                        )
                        pending_text["value"] = None

        # --- the wall clock, computed here rather than at the run site -----
        # The deadline spans the initial run and every overflow-recovery
        # continue (see _run_loop_once below). It is derived up here because
        # three things now read it and all of them are defined before the run:
        # the turn-budget nudge, the soft-deadline stop condition, and the hard
        # `asyncio.wait_for` that ends a turn hanging past it.
        deadline = (
            time.monotonic() + req.timeout_seconds
            if req.timeout_seconds and req.timeout_seconds > 0
            else None
        )
        # ISSUE-373: the loop's own stop, a little before the clock's. The gap
        # is what the hard deadline still covers — a turn that hangs past the
        # soft stop can only be ended by cutting it mid-stream.
        pct = self._config.soft_deadline_percent
        soft_deadline = (
            deadline - req.timeout_seconds * (100 - pct) / 100
            if deadline is not None and 0 < pct < 100
            else None
        )
        # Rolling turn latency, for converting the time budget into a turn
        # budget. Written by the turn_end handler above, read by the nudge.
        turn_clock: dict = {"last": time.monotonic(), "recent": []}

        # --- compaction + turn-budget nudge via prepare_next_turn ---------
        compaction_state = {"summary": None, "details": None}
        # Turn-budget nudge (ISSUE-187 defect 3). Only for tool-bearing tasks with
        # a cap — a text-only run (empty allowed_tools, e.g. the sleep cycle)
        # never sees it. ``fired`` tracks crossed thresholds so each surfaces once.
        budget_nudge_on = (
            self._config.turn_budget_nudge
            and bool(self._config.max_turns)
            and bool(req.allowed_tools)
        )
        budget_state: dict = {"fired": set()}

        def _next_budget_nudge(new_messages) -> UserMessage | None:
            if not budget_nudge_on:
                return None
            turns = sum(1 for m in new_messages if isinstance(m, AssistantMessage))
            # The run's real end is the soft stop where there is one, so that is
            # the horizon the estimate is taken against. Estimating to the hard
            # deadline would promise the model steps the loop has already
            # decided not to take.
            horizon = soft_deadline if soft_deadline is not None else deadline
            by_clock = _turns_left_by_clock(
                horizon - time.monotonic() if horizon is not None else None,
                turn_clock["recent"],
            )
            picked = _pick_turn_budget_nudge(
                turns,
                self._config.max_turns,
                self._config.turn_budget_nudge_early_percent,
                self._config.turn_budget_nudge_remaining,
                budget_state["fired"],
                by_clock,
            )
            if picked is None:
                return None
            remaining, phase = picked
            logger.debug(
                "turn_budget_nudge fired remaining=%s phase=%s turns=%s/%s "
                "by_clock=%s",
                remaining, phase, turns, self._config.max_turns, by_clock,
            )
            # The nudge is injected via prepare_next_turn's returned list, so it
            # never reaches `new_messages` and never emits `message_end`. Its
            # own record is the only thing that explains why the model's tone
            # changes partway through a run.
            log.nudge(
                phase=phase,
                remaining=remaining,
                turns=turns,
                max_turns=self._config.max_turns,
            )
            return _turn_budget_nudge_message(remaining, phase)

        async def prepare_next_turn(ctx: AgentContext, new_messages):
            from istota.agent.types import PrepareNextTurnResult

            nudge = _next_budget_nudge(new_messages)
            info = get_model_info(model)
            window = self._config.context_window or info.context_window
            reserve = self._config.compaction_reserve_tokens or derive_reserve_tokens(window)
            keep_recent = (
                self._config.compaction_keep_recent_tokens or derive_keep_recent_tokens(window)
            )
            tokens, _ = estimate_context_tokens(ctx.messages)
            compacted: list | None = None
            if should_compact(tokens, window, reserve_tokens=reserve):
                cut = find_cut_point(ctx.messages, keep_recent_tokens=keep_recent)
                if cut > 0:
                    to_compact = ctx.messages[:cut]
                    remaining = ctx.messages[cut:]
                    # The task's own image attachments sit in the message at
                    # index 0 and `find_cut_point` walks back from the newest,
                    # so the first compaction is exactly the cut that drops
                    # them. `plan_image_pin` decides what is carried over and
                    # hands back the summarizer's input with those blocks
                    # already removed — the loss notice must not be written
                    # about an image that is being kept. The notice is the
                    # floor for the case where there is nothing to pin; the pin
                    # is what keeps the capability itself.
                    pin, to_summarize = plan_image_pin(to_compact, keep_recent)
                    summary, details = await compact_messages(
                        to_summarize,
                        compaction_state["summary"],
                        compaction_state["details"],
                        # Through the retrying provider so a transient 429 during
                        # the summary call is retried, not treated as a failed
                        # compaction (NB-10).
                        provider,
                        model,
                        self._convert_to_llm,
                        # Bound the summary input so the request can't overflow.
                        max_input_chars=_compaction_input_chars(window),
                    )
                    compaction_state["summary"] = summary
                    compaction_state["details"] = details
                    stats["compactions"] += 1
                    # The messages this dropped are already in the file above
                    # it, so the record does not repeat them. `trigger` is the
                    # whole diagnostic value: proactive is the system working,
                    # overflow (below) is it catching a miss.
                    log.compaction(
                        trigger="proactive",
                        summary=summary,
                        tokens_before=tokens,
                        cut_index=cut,
                        messages_dropped=len(to_compact),
                        image_pinned=pin is not None,
                        details=_details_dict(details),
                        recovery_index=None,
                    )
                    summary_msg = CompactionSummaryMessage(
                        summary=summary, tokens_before=tokens, details=details
                    )
                    compacted = (
                        [pin, summary_msg, *remaining]
                        if pin is not None
                        else [summary_msg, *remaining]
                    )

            # Combine: a nudge injects into whichever message list is current
            # (compacted, or the unchanged context) as a trailing environment
            # note. Injecting via the returned list keeps it out of new_messages
            # (invisible to the trace + turn count) — purely model-facing.
            if nudge is None:
                if compacted is None:
                    return None
                return PrepareNextTurnResult(messages=compacted)
            base = compacted if compacted is not None else list(ctx.messages)
            base.append(nudge)
            return PrepareNextTurnResult(messages=base)

        # --- stop conditions ----------------------------------------------
        max_turns = self._config.max_turns

        # A cancel must outrank every graceful stop (ISSUE-372/373). The loop
        # checks ``abort`` at the *top* of its inner iteration while stop
        # conditions run at the bottom, and ``_stream_assistant_response``
        # catches only an abort landing mid-stream — so an abort set during tool
        # execution reaches the stop conditions first. Any of them firing there
        # converts the cancel into its own reason, and every one of these three
        # returns ``success=True``: the scheduler then marks the task
        # `completed`, posts the marker to the room as the answer after `!stop`
        # already said it stopped, indexes the turn into memory, and replays the
        # run's deferred ops (`_drain_deferred_ops` gates on success alone).
        # Declining here lets the loop's own check win on the next iteration and
        # end the run as ``aborted``. Tool execution is where a long run spends
        # most of its wall clock, which is exactly where a user reaches for
        # `!stop`.
        def _cancel_outranks() -> bool:
            return abort.is_set()

        async def _max_turns_stop(ctx, new_messages) -> StopDecision:
            if _cancel_outranks():
                return StopDecision(stop=False)
            turns = sum(1 for m in new_messages if isinstance(m, AssistantMessage))
            if max_turns and turns >= max_turns:
                return StopDecision(stop=True, reason="max_turns")
            return StopDecision(stop=False)

        async def _loop_detect_stop(ctx, new_messages) -> StopDecision:
            if _cancel_outranks():
                return StopDecision(stop=False)
            if detect_repeated_tool_calls(ctx.messages) is not None:
                return StopDecision(stop=True, reason="loop_detected")
            return StopDecision(stop=False)

        async def _soft_deadline_stop(ctx, new_messages) -> StopDecision:
            """End a run the wall clock is about to end anyway (ISSUE-373).

            Checked at a turn boundary, which is the point of it: the hard
            deadline fires mid-turn and its result discards the model's text,
            while this stops between turns and delivers it under a marker. On a
            brain slow enough that the clock beats ``max_turns``, this is the
            difference between an answer and the words "timed out".

            **Only a turn that called tools may be stopped.** The loop evaluates
            stop conditions after *every* ``turn_end``, the final text-only one
            included — and that turn is the finished answer, with the natural
            exit one check away. Firing there labels a completed run
            ``soft_timeout`` and appends a marker saying it ran out of time,
            which is the opposite of what happened. There is also nothing to
            save: this stop exists to rescue work from a run that would have
            continued, and a run that has stopped on its own has already
            delivered it.

            Gated on ``req.allowed_tools`` for the same reason ``budget_nudge_on``
            is: the native brain's text-only direct callers (the sleep cycle,
            shared briefing blocks, health OCR, conversation triage) parse
            structured output, and appending prose to their JSON would break
            them. A single-turn text-only call is exactly the shape the
            tool-call guard above would also catch, so this is belt and braces.
            """
            if _cancel_outranks():
                return StopDecision(stop=False)
            if soft_deadline is None or not req.allowed_tools:
                return StopDecision(stop=False)
            if time.monotonic() < soft_deadline:
                return StopDecision(stop=False)
            last = next(
                (m for m in reversed(new_messages)
                 if isinstance(m, AssistantMessage)),
                None,
            )
            if last is None or not last.tool_calls:
                return StopDecision(stop=False)
            return StopDecision(stop=True, reason="soft_timeout")

        # One capability answer for both consumers: whether a tool result's
        # images render, and whether this task's own attachments become image
        # blocks at all. Reading it twice invites the two halves disagreeing.
        supports_vision = get_model_info(model).supports_vision

        loop_config = AgentLoopConfig(
            provider=provider,
            model=model,
            convert_to_llm=self._convert_to_llm,
            prepare_next_turn=prepare_next_turn,
            stop_conditions=[_max_turns_stop, _loop_detect_stop, _soft_deadline_stop],
            # Independent read-only tools (Read/Grep/Glob/WebFetch, all
            # execution_mode="parallel") run concurrently; any batch containing a
            # mutation (Write/Edit/Bash are sequential) or two calls to the same
            # path serializes via _execute_tool_batch's existing guards.
            tool_execution="parallel",
            max_tokens=self._config.max_tokens,
            reasoning_effort=reasoning_effort,
            render_tool_images=supports_vision,
            abort=abort,
            get_steering_messages=(
                _get_steering_messages if req.poll_steers is not None else None
            ),
            steering_queue_mode="one_at_a_time",
        )

        context = AgentContext(
            system_prompt=self._extract_system_prompt(req),
            messages=[],
            tools=self._build_tools(req),
        )
        # Line 2 of the file, before the loop emits anything: the system prompt
        # and the tool surface, recorded once rather than on every turn.
        log.context(
            context.system_prompt,
            [t.schema.name for t in (context.tools or [])],
            _tools_schema_sha(context.tools),
            system_prompt_source=self._system_prompt_source(req),
        )
        prompt_msg = UserMessage(
            content=_initial_user_content(
                req.prompt, req.images, supports_vision, model
            )
        )

        # The loop captures agent_end's stop_reason; we sniff it from the final
        # event by subscribing through a wrapper sink.
        final_stop = {"reason": ""}

        async def emit_wrapped(event: AgentEvent) -> None:
            if event.type == "agent_end":
                final_stop["reason"] = event.stop_reason
            await emit(event)

        # The loop runs under the wall-clock ``deadline`` computed above, shared
        # across the initial run and every overflow-recovery continue. Without
        # one, a runaway model or a slow provider could run far past the task
        # timeout; the scheduler would then reclaim the "stuck" task and a second
        # worker would execute it concurrently (duplicate output + duplicate
        # deferred-op replay). On timeout we set ``abort`` first so tools/provider
        # unwind cleanly — the bash tool polls abort and kills its subprocess —
        # then give a short grace period before hard-cancelling.

        async def _run_loop_once(prompts, ctx) -> tuple[list, bool]:
            """Run one loop pass under the *remaining* shared deadline.

            ``prompts`` non-None → ``run_agent_loop``; None → continue. Returns
            ``(new_messages, timed_out)``. The deadline spans all attempts, so a
            recovery continue gets only the time left, never a fresh budget.
            """
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], True
            # Restart the latency clock for this pass. A recovery continue is
            # preceded by a compaction (a summarization request of its own), and
            # without this the first turn after it records that work as a turn
            # latency — deflating the clock estimate and firing the urgent
            # notices early for the next few turns.
            turn_clock["last"] = time.monotonic()
            if prompts is not None:
                coro = run_agent_loop(prompts, ctx, loop_config, emit_wrapped)
            else:
                coro = run_agent_loop_continue(ctx, loop_config, emit_wrapped)
            task = asyncio.create_task(coro)
            if remaining is None:
                return await task, False
            try:
                msgs = await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
                return msgs, False
            except asyncio.TimeoutError:
                abort.set()
                try:
                    msgs = await asyncio.wait_for(asyncio.shield(task), timeout=10)
                    return msgs, True
                except asyncio.TimeoutError:
                    task.cancel()
                    try:
                        return await task, True
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        return [], True

        timed_out = False
        try:
            transcript, timed_out = await _run_loop_once([prompt_msg], context)

            # Reactive overflow recovery: a mid-task context-length error is
            # recoverable — force-compact the accumulated transcript and continue
            # from the summary. The proactive ``prepare_next_turn`` path is the
            # first line of defense; this is the safety net beneath it. Bounded
            # (≤_MAX_OVERFLOW_RECOVERIES) and time-bounded (shares the deadline)
            # so a genuinely too-large turn can't thrash forever.
            recoveries = 0
            while (
                not timed_out
                and final_stop["reason"] == "error"
                and classify_error(last_error_message).is_context_overflow
                and recoveries < _MAX_OVERFLOW_RECOVERIES
            ):
                recoveries += 1
                logger.info(
                    "native overflow recovery %d/%d: compacting and continuing",
                    recoveries,
                    _MAX_OVERFLOW_RECOVERIES,
                )
                _rec_window = self._config.context_window or get_model_info(model).context_window
                # Preserve the overflow error before clearing, so an unusable
                # recovery can fail with the real cause instead of a wrong answer.
                overflow_error = last_error_message
                recovery_ctx, summary, details = await _build_recovery_context(
                    transcript,
                    context.system_prompt,
                    context.tools,
                    compaction_state["summary"],
                    compaction_state["details"],
                    # Retrying provider (NB-10) so a transient error during the
                    # recovery summary call is retried, not swallowed.
                    provider,
                    model,
                    self._convert_to_llm,
                    keep_recent_tokens=(
                        self._config.compaction_keep_recent_tokens
                        or derive_keep_recent_tokens(_rec_window)
                    ),
                    max_input_chars=_compaction_input_chars(_rec_window),
                )
                stats["compactions"] += 1
                # Written before the no-summary check below, deliberately: a
                # recovery that produced nothing records `summary: null` and
                # then the run fails, and this record is what explains the
                # failure.
                #
                # The two triggers write the *same* key set, so a reader never
                # has to ask which one fired before it knows which fields
                # exist. `cut_index` is the one this path cannot fill: the cut
                # rule lives inside `_build_recovery_context`, which does not
                # report it, and recomputing `find_cut_point` here would be a
                # second copy free to disagree with the one that ran. Null
                # rather than absent.
                #
                # Everything else is read back off the two lists by identity.
                # `_build_recovery_context` splices the kept tail by reference,
                # and `transcript` holds every dropped message alive, so no
                # `id()` can have been recycled — and the set is taken before
                # the continue mutates `recovery_ctx.messages`.
                _kept = {id(m) for m in recovery_ctx.messages}
                _added = [m for m in recovery_ctx.messages if id(m) not in {id(t) for t in transcript}]
                log.compaction(
                    trigger="overflow",
                    recovery_index=recoveries,
                    summary=summary or None,
                    tokens_before=_estimated_tokens(transcript),
                    cut_index=None,
                    messages_dropped=sum(1 for m in transcript if id(m) not in _kept),
                    image_pinned=any(_carries_image(m) for m in _added),
                    details=_details_dict(details),
                )
                # If compaction produced no summary (the summary request itself
                # overflowed and there was no prior summary to fall back on),
                # continuing would answer with the user's request gone from
                # context — a confident non-answer. Fail with the overflow error
                # instead (NB-10).
                if not (summary and summary.strip()):
                    logger.warning(
                        "native overflow recovery produced no summary; failing with overflow error"
                    )
                    final_stop["reason"] = "error"
                    last_error_message = (
                        overflow_error or "context window exceeded and compaction failed"
                    )
                    break
                compaction_state["summary"] = summary
                compaction_state["details"] = details
                # Clear the error markers so the post-continue re-check sees the
                # continue's own outcome, not the prior overflow.
                final_stop["reason"] = ""
                last_error_message = ""
                _cont, timed_out = await _run_loop_once(None, recovery_ctx)
                # continue mutates recovery_ctx.messages to the full transcript.
                transcript = recovery_ctx.messages
        finally:
            abort.set()
            for _bg in (cancel_task, steer_task):
                if _bg is not None:
                    _bg.cancel()
                    # Await the cancellation so the loop doesn't log a
                    # "Task was destroyed but it is pending" warning on teardown.
                    try:
                        await _bg
                    except asyncio.CancelledError:
                        pass
            # In ``finally`` so the per-task cache footer is logged at task end
            # even if the recovery body raises a non-overflow exception.
            _log_cache_telemetry(usage)

        # A run that ended mid-turn (timeout, abort, an exception past the last
        # turn_end) can leave a turn's tools buffered. Append them so the trace
        # records the work, accepting that a torn final turn has no text to sit
        # in front of them.
        if pending_tools:
            trace.extend(pending_tools)
            pending_tools.clear()

        if timed_out:
            timeout_min = req.timeout_seconds // 60
            return BrainResult(
                success=False,
                result_text=f"Task execution timed out after {timeout_min} minutes",
                actions_taken=json.dumps(actions) if actions else None,
                execution_trace=json.dumps(trace) if trace else None,
                stop_reason="timeout",
                usage=usage_types.from_task_usage(
                    usage, cost_reported=_all_turns_costed()
                ),
                model_used=model,
                # The prose the run had produced, kept off result_text so the
                # scheduler's "timed out" substring match is untouched
                # (ISSUE-372). The soft deadline above means a slow-but-healthy
                # run rarely reaches here; this covers the turn that hung.
                partial_text=last_assistant_text or None,
            )

        # A soft stop with nothing to save is the hard clock, not a success.
        # `_build_result`'s partial-answer arm makes the marker the whole text
        # and returns `success=True`, which for `max_turns` and `loop_detected`
        # is right — both name a pathology a retry would only repeat, and the
        # comment there says so. `soft_timeout` names *slowness*, which a retry
        # on a fresh budget can legitimately clear, and before ISSUE-373 this
        # exact run reached the hard clock and got up to `max_attempts` of them.
        # Delivering one parenthetical as a completed answer instead would be a
        # regression bought with the fix. Nothing is lost by falling through:
        # there was no work to preserve, which is the condition being tested.
        if (
            final_stop["reason"] == "soft_timeout"
            and not final_turn_text.strip()
            and not last_assistant_text.strip()
        ):
            timeout_min = req.timeout_seconds // 60
            return BrainResult(
                success=False,
                result_text=f"Task execution timed out after {timeout_min} minutes",
                actions_taken=json.dumps(actions) if actions else None,
                execution_trace=json.dumps(trace) if trace else None,
                stop_reason="timeout",
                usage=usage_types.from_task_usage(
                    usage, cost_reported=_all_turns_costed()
                ),
                model_used=model,
            )

        # NB-15: a final answer the model was forced to cut short (output token
        # cap) or that the endpoint's content filter clipped is delivered with a
        # visible marker instead of masquerading as a complete response. A
        # truncation that produced *no* answer content (typically a reasoning
        # model that spent the whole output-token budget on thinking and emitted
        # nothing) is a real failure, not a silent success: return an informative
        # error so the executor's retry path engages and the empty reply is never
        # delivered or archived as a completed task.
        # The answer is the final turn's text (ISSUE-211). The abnormal-stop
        # paths are the deliberate exception: a truncation marker, a max_turns
        # cap or a detected loop all deliver the text *with a marker saying it
        # is incomplete*, so falling back to the last text the run produced is
        # honest there and is what ISSUE-187 and NB-15 shipped. Without the
        # fallback a capped run whose last turn was tool-only would deliver a
        # bare marker and drop the partial work entirely.
        result_text = final_turn_text
        marker = (
            _TRUNCATION_MARKERS.get(last_assistant_stop["value"])
            if not final_stop["reason"]
            else None
        )
        if not result_text.strip() and (
            marker or final_stop["reason"] in _PARTIAL_ANSWER_STOP_REASONS
        ):
            result_text = last_assistant_text
        if marker:
            if result_text.strip():
                # Non-empty but cut short — keep the partial answer and flag it.
                result_text = f"{result_text}\n\n{marker}"
            else:
                # Empty content + truncation — no usable output. Fail with the
                # marker as the error text. _build_result's "error" path runs it
                # through _classify_native_error, which leaves a truncation note
                # as a generic "error" (not usage_limit / transient), so the
                # task flows through the normal retry path rather than rerouting.
                return self._build_result(
                    "error", marker, last_error_message,
                    trace, actions, usage, model,
                    cost_reported=_all_turns_costed(),
                )

        return self._build_result(
            final_stop["reason"], result_text, last_error_message,
            trace, actions, usage, model,
            cost_reported=_all_turns_costed(),
            partial_text=last_assistant_text,
        )

    # --- helpers -----------------------------------------------------------

    @staticmethod
    async def _poll_cancel(cancel_check, abort: asyncio.Event) -> None:
        try:
            while not abort.is_set():
                try:
                    # cancel_check is a *synchronous* DB read (open + query with a
                    # 30s busy timeout). Run it off the event loop so SQLite lock
                    # contention can't freeze the whole loop — streaming, tool
                    # execution, progress emission, and the wall-clock deadline
                    # timer all share it (NB-9; same off-loop discipline as
                    # _emit_progress's run_in_executor hop).
                    cancelled = await asyncio.to_thread(cancel_check)
                    if cancelled:
                        abort.set()
                        return
                except Exception:  # noqa: BLE001
                    # A transient cancel_check failure (e.g. SQLite lock
                    # contention) must not kill the poller — that would
                    # permanently disable !stop for the rest of the run. Log and
                    # keep polling.
                    logger.debug("cancel_check raised; will retry", exc_info=True)
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

    @staticmethod
    async def _poll_steers(poll_steers, buffer: list, abort: asyncio.Event) -> None:
        """Read the steer control channel off the loop and buffer raw texts.

        ``poll_steers`` is a *synchronous* DB read+write (claim pending → mark
        consumed), so it runs via ``to_thread`` — SQLite lock contention can't
        freeze the loop (same discipline as ``_poll_cancel``). New texts are
        appended to ``buffer``, which the synchronous ``get_steering_messages``
        callback drains one-per-turn on the loop thread (no lock needed: both run
        on the single asyncio thread). A transient read failure is logged and
        retried, never fatal — a wedged poll must not silently disable steering.
        """
        try:
            while not abort.is_set():
                try:
                    new = await asyncio.to_thread(poll_steers)
                    if new:
                        buffer.extend(new)
                except Exception:  # noqa: BLE001 — see _poll_cancel
                    logger.debug("poll_steers raised; will retry", exc_info=True)
                await asyncio.sleep(_STEER_POLL_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            return

    def _build_tools(self, req: BrainRequest):
        """Default tools bound to a per-task ToolEnv, filtered by allowed_tools.

        Empty ``allowed_tools`` means a text-only invocation (e.g. sleep cycle) —
        no tools are exposed.
        """
        if not req.allowed_tools:
            return []
        from istota.session.tools import ToolEnv, build_default_tools

        # Filesystem confinement (NB-1): when the executor supplies file-access
        # roots, the in-process file tools are confined to them (the native
        # stand-in for bwrap's filesystem isolation). Relative paths then resolve
        # under the user's own writable dir rather than the shared temp root.
        read_roots = tuple(req.fs_read_roots) if req.fs_read_roots else None
        write_roots = tuple(req.fs_write_roots) if req.fs_write_roots else None
        write_denied_roots = tuple(req.fs_write_denied_roots or ())
        cwd = write_roots[0] if write_roots else Path(req.cwd)

        policy = self._web_fetch_policy()
        # Provenance corpus (Stage 3b): only assembled when the knob is on, so
        # the default path threads nothing new. v1 corpus = URLs present in the
        # task prompt (task text + prior conversation context already folded into
        # req.prompt by the executor).
        corpus = None
        if policy is not None and policy.require_url_provenance:
            corpus = _extract_urls(req.prompt)

        deferred = (req.env or {}).get("ISTOTA_DEFERRED_DIR")
        env = ToolEnv(
            cwd=cwd,
            sandbox_wrap=req.sandbox_wrap,
            subprocess_env=req.env or None,
            bash_timeout_seconds=max(1, req.timeout_seconds),
            read_roots=read_roots,
            write_roots=write_roots,
            write_denied_roots=write_denied_roots,
            web_fetch=policy,
            web_fetch_url_corpus=corpus,
            deferred_dir=Path(deferred) if deferred else None,
            bash_spill_full_output=getattr(self._config, "bash_spill_full_output", True),
            task_cgroup=req.task_cgroup,
        )
        allowed = set(req.allowed_tools)
        return [t for t in build_default_tools(env) if t.schema.name in allowed]

    def _web_fetch_policy(self):
        """Map the configured ``WebFetchConfig`` → the tool's ``WebFetchPolicy``.

        Returns ``None`` (tool omitted) when the config is absent or disabled.
        The native harness has no other web-reaching tool once Bash is confined
        behind the CONNECT allowlist, so this is the daemon-side fetch path.
        """
        from istota.session.tools import WebFetchPolicy

        cfg = getattr(self._config, "web_fetch", None)
        if cfg is None or not cfg.enabled:
            return None
        return WebFetchPolicy(
            enabled=True,
            timeout_seconds=cfg.timeout_seconds,
            max_bytes=cfg.max_bytes,
            max_content_chars=cfg.max_content_chars,
            max_redirects=cfg.max_redirects,
            allow_http=cfg.allow_http,
            allowed_ports=tuple(cfg.allowed_ports),
            user_agent=cfg.user_agent,
            allow_hosts=tuple(cfg.allow_hosts),
            block_hosts=tuple(cfg.block_hosts),
            extra_blocked_cidrs=tuple(cfg.extra_blocked_cidrs),
            require_url_provenance=cfg.require_url_provenance,
        )

    def _convert_to_llm(self, messages: list[AgentMessage]) -> list[Message]:
        """Render AgentMessages to provider wire format, then repair tool pairs.

        Real LLM messages pass through; a ``CompactionSummaryMessage`` becomes a
        user-role note; unknown custom messages are dropped. ``sanitize_tool_pairs``
        then synthesizes results for any orphaned tool_call (and drops stray
        results) so a resumed / compacted context never 400s.
        """
        rendered: list[Message] = []
        for msg in messages:
            if isinstance(msg, (UserMessage, AssistantMessage, ToolResultMessage)):
                rendered.append(msg)
            elif isinstance(msg, CompactionSummaryMessage):
                rendered.append(
                    UserMessage(
                        content=[
                            TextContent(
                                text="[Summary of earlier conversation]\n" + msg.summary
                            )
                        ]
                    )
                )
            # else: unknown custom message — not renderable, skip.
        return sanitize_tool_pairs(rendered)

    def _extract_system_prompt(self, req: BrainRequest) -> str:
        """The composed system prompt. See :meth:`_system_prompt_parts`."""
        return "\n\n".join(text for _, text in self._system_prompt_parts(req))

    def _system_prompt_source(self, req: BrainRequest) -> str:
        """What the composed prompt is made of, for the ``context`` record.

        Derived from the same walk that builds the text rather than from
        ``req.custom_system_prompt_path`` being set, because the two disagree in
        both directions: a custom file is *appended* to the built-in block
        rather than replacing it, and a configured path that does not exist
        contributes nothing at all. Naming the file in either case would have
        the record describe a prompt the run did not use.
        """
        return "+".join(name for name, _ in self._system_prompt_parts(req)) or "empty"

    def _system_prompt_parts(self, req: BrainRequest) -> list[tuple[str, str]]:
        """Compose the native brain's system prompt.

        Tool-bearing tasks (non-empty ``allowed_tools``) get the coding-guidance
        block; a text-only invocation (empty ``allowed_tools``, e.g. the sleep
        cycle) keeps an empty prompt — no behavioural change to that path. When
        the turn-budget nudge is enabled the coding block also carries a
        non-numeric "don't die mid-stream" pacing line (ISSUE-187 mechanism A;
        compaction-safe since the system prompt lives outside ``ctx.messages``).
        An operator's ``custom_system_prompt_path`` is appended after the base so
        it still applies.

        Returns ``(name, text)`` pairs so the ``context`` record can say what the
        prompt was made of without a second copy of these conditions deciding it
        — a checker with its own copy of the rule is free to disagree with the
        thing it describes.
        """
        parts: list[tuple[str, str]] = []
        if req.allowed_tools:
            coding = CODING_SYSTEM_PROMPT
            if self._config.turn_budget_nudge and self._config.max_turns:
                coding = f"{coding}\n\n- {_TURN_BUDGET_UPFRONT}"
            parts.append(("builtin", coding))
        path = req.custom_system_prompt_path
        if path is not None and Path(path).exists():
            parts.append((str(path), Path(path).read_text()))
        return parts

    @staticmethod
    def _build_result(
        stop_reason, text, error_message, trace, actions, usage, model="",
        *, cost_reported: bool = False, partial_text: str = "",
    ) -> BrainResult:
        # Map the loop's agent_end stop_reason to the executor's tag vocabulary.
        # The executor drops stop_reason and the scheduler dispatches purely on
        # result_text string matches (see scheduler.process_one_task), so a
        # cancelled / errored native task MUST carry the same text ClaudeCodeBrain
        # emits — otherwise the scheduler mis-routes it (retries a cancelled task,
        # or retries a policy refusal instead of failing fast with an alert).
        actions_json = json.dumps(actions) if actions else None
        trace_json = json.dumps(trace) if trace else None
        # Converted once here rather than at each return below. `TaskUsage` keeps
        # its shape (its `input_tokens` is OpenAI-compat `prompt_tokens`,
        # inclusive of cache reads, which `_log_cache_telemetry` depends on);
        # the adapter reconciles that with Anthropic's exclusive count at the
        # boundary and labels the result `derived`.
        usage = usage_types.from_task_usage(usage, cost_reported=cost_reported)
        if stop_reason == "aborted":
            return BrainResult(
                success=False,
                # Byte-identical, deliberately: `result == "Cancelled by user"`
                # is an exact-equality match in three places in the scheduler,
                # and a cancelled task that stopped matching would go back
                # through the retry ladder. The work travels beside it instead
                # (ISSUE-372).
                result_text="Cancelled by user",
                actions_taken=actions_json,
                execution_trace=trace_json,
                stop_reason="cancelled",
                usage=usage,
                model_used=model,
                partial_text=partial_text or None,
            )
        if stop_reason == "error":
            # Classify the provider error body: a quota/billing exhaustion becomes
            # ``usage_limit`` (reroutes to the fallback brain); a transient
            # overload/rate-limit stays retryable; else a generic error.
            classified = _classify_native_error(error_message or text)
            return BrainResult(
                success=False,
                result_text=error_message or text or "Native brain execution error",
                actions_taken=actions_json,
                execution_trace=trace_json,
                stop_reason=classified,
                usage=usage,
                model_used=model,
                # Forwarded here too (ISSUE-372). A run that narrates for 25
                # minutes and then dies on a provider error has lost exactly what
                # the field exists to keep, and `result_text` is the error, so
                # there is nowhere else for it to go. The executor gates on
                # `not success`, which this is.
                partial_text=partial_text or None,
            )
        # Natural end, or a backstop stop (max_turns / loop_detected). The two
        # backstops are now first-class stop_reasons (see _DOCUMENTED_STOP_REASONS)
        # so they survive normalization instead of collapsing to "completed".
        result_text = text
        if stop_reason in _PARTIAL_ANSWER_STOP_REASONS:
            # A backstop stop always carries a visible marker — whether or not
            # the final turn produced text. The common case is non-empty
            # narration ("let me try X next"), which would otherwise be
            # delivered verbatim as a complete answer; append the marker so a
            # truncated-by-cap run is never mistaken for a finished one. An
            # empty result keeps the marker as the whole text and avoids a
            # retry storm (retrying a wedged/capped model just wedges again).
            marker = _PARTIAL_ANSWER_MARKERS[stop_reason]
            result_text = (
                f"{result_text}\n\n{marker}" if result_text.strip() else marker
            )
        normalized = stop_reason if stop_reason in _DOCUMENTED_STOP_REASONS else "completed"
        return BrainResult(
            success=True,
            result_text=result_text,
            actions_taken=actions_json,
            execution_trace=trace_json,
            stop_reason=normalized,
            usage=usage,
            model_used=model,
        )

    @staticmethod
    async def _emit_progress(req: BrainRequest, event) -> None:
        """Invoke the sync ``on_progress`` callback off the event loop.

        The scheduler's progress callback edits the Talk message by calling
        ``asyncio.run()``. ``emit`` runs inside this brain's own
        ``asyncio.run`` loop, so calling the callback directly would invoke
        ``asyncio.run()`` from a running loop → ``RuntimeError``, silently
        dropping every in-progress update (ISSUE-111). Running it in a
        default-executor thread gives the callback a thread with no running
        loop, so its ``asyncio.run`` works. We ``await`` it so progress edits
        stay ordered with the events that produced them — matching
        ClaudeCodeBrain, which blocks its stream-parse loop on each edit.
        """
        if req.on_progress is None or event is None:
            return
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, req.on_progress, event)
        except Exception:  # noqa: BLE001 — progress is best-effort
            logger.debug("on_progress callback raised", exc_info=True)


def _aggressive_cut(transcript: list) -> int:
    """Cut index for the cut==0 overflow fallback: keep only the last
    user/tool-result tail.

    Walks back to the most recent ``UserMessage`` / ``ToolResultMessage`` and
    compacts everything before it, guaranteeing forward progress when
    ``find_cut_point`` declined to cut. With no user/tool-result anchor, the
    whole transcript is compacted (``len(transcript)``) so only the summary
    survives.

    The kept tail never starts on a ``ToolResultMessage`` (that would strand the
    result from its owning tool_call, which ``sanitize_tool_pairs`` then drops
    silently — losing the very output recovery meant to preserve). Mirrors
    ``find_cut_point``: advance forward past leading tool_results so the orphan
    lands in the compacted prefix with its call; if that runs off the end (the
    anchor was a trailing result with no newer message), back up so the owning
    assistant message is kept instead.
    """
    anchor = len(transcript)
    for i in range(len(transcript) - 1, -1, -1):
        if isinstance(transcript[i], (UserMessage, ToolResultMessage)):
            anchor = i
            break
    if anchor == len(transcript):
        return anchor  # no anchor — compact everything, only the summary survives

    advanced = anchor
    while advanced < len(transcript) and isinstance(transcript[advanced], ToolResultMessage):
        advanced += 1
    if advanced < len(transcript):
        return advanced
    back = anchor
    while back > 0 and isinstance(transcript[back], ToolResultMessage):
        back -= 1
    return back


async def _build_recovery_context(
    transcript: list,
    system_prompt: str,
    tools,
    prev_summary: str | None,
    prev_details,
    provider,
    model: str,
    convert_to_llm,
    keep_recent_tokens: int = 20000,
    max_input_chars: int = 0,
) -> tuple[AgentContext, str, object]:
    """Force-compact ``transcript`` and return a context ready for continue.

    Ignores ``should_compact`` (this is the reactive safety net — the window was
    already exceeded). Falls back to ``_aggressive_cut`` when ``find_cut_point``
    returns 0 so a turn is always reclaimed. Appends a synthetic user nudge when
    the compacted tail ends on an assistant message, because
    ``run_agent_loop_continue`` refuses to continue from one.
    """
    cut = find_cut_point(transcript, keep_recent_tokens=keep_recent_tokens)
    if cut == 0:
        cut = _aggressive_cut(transcript)
    to_compact = transcript[:cut]
    remaining = transcript[cut:]
    # The same pin as the proactive path, and needed here more rather than
    # less: this path always sheds index 0 (`find_cut_point` walks back from
    # the newest, and `_aggressive_cut` cuts harder still), so without it a
    # task that once overflowed is blind for the rest of the run and no later
    # compaction can restore what is gone. The size guard is what makes that
    # safe on the one path where the window has already been exceeded — an
    # oversized set of blocks is dropped with the summary's loss notice rather
    # than carried into a request that just failed for length.
    pin, to_summarize = plan_image_pin(to_compact, keep_recent_tokens)
    summary, details = await compact_messages(
        to_summarize, prev_summary, prev_details, provider, model, convert_to_llm,
        max_input_chars=max_input_chars,
    )
    messages: list = [
        *([pin] if pin is not None else []),
        CompactionSummaryMessage(summary=summary, tokens_before=0, details=details),
        *remaining,
    ]
    if messages and getattr(messages[-1], "role", None) == "assistant":
        messages.append(UserMessage(content=[TextContent(text=_RECOVERY_NUDGE)]))
    ctx = AgentContext(system_prompt=system_prompt, messages=messages, tools=tools)
    return ctx, summary, details


def _log_cache_telemetry(usage: TaskUsage) -> None:
    """Log the cumulative cross-turn cache-hit rate at task end (Stage 5b).

    ``hit_rate`` is ``cache_read_tokens / input_tokens`` as a percentage. Under
    OpenAI-compat semantics (the sole transport) ``prompt_tokens`` already
    includes ``cached_tokens``, so the read count is a subset of the input and
    the ratio is bounded in [0, 100]. A non-conforming provider that reports
    cache reads *outside* ``prompt_tokens`` could push it past 100%, so the
    value is clamped defensively. With no input recorded the ratio is reported
    as 0% (no divide-by-zero). Mirrors pi's per-task cache footer so Stage 2's
    caching can be validated against production data.
    """
    read = usage.cache_read_tokens
    inp = usage.input_tokens
    rate = min(100.0, read / inp * 100.0) if inp else 0.0
    logger.info(
        "native cache hit_rate=%.1f%% read=%d input=%d write=%d",
        rate,
        read,
        inp,
        usage.cache_write_tokens,
    )


def _tool_use_event(tool_name: str, description: str, tool_call_id: str = ""):
    from ._events import ToolUseEvent

    return ToolUseEvent(
        tool_name=tool_name, description=description, tool_call_id=tool_call_id
    )


def _tool_end_event(tool_name: str, tool_call_id: str, success: bool, duration_ms: int):
    from ._events import ToolEndEvent

    return ToolEndEvent(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        success=success,
        duration_ms=duration_ms,
    )


def _tool_progress_event(tool_name: str, tool_call_id: str, text: str):
    from ._events import ToolProgressEvent

    return ToolProgressEvent(
        tool_name=tool_name, tool_call_id=tool_call_id, text=text
    )


def _text_event(text: str):
    from ._events import TextEvent

    return TextEvent(text=text)


def _text_delta_event(text: str):
    from ._events import TextDeltaEvent

    return TextDeltaEvent(text=text)


def _thinking_delta_event(thinking: str):
    from ._events import ThinkingDeltaEvent

    return ThinkingDeltaEvent(thinking=thinking)
