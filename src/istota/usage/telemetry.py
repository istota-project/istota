"""Normalized token/cost telemetry for one brain attempt.

Every brain reports usage in its own shape. ``ClaudeCodeBrain`` reads it off the
CLI's terminal ``result`` frame plus the per-request ``message_delta`` frames;
``NativeBrain`` accumulates a ``TaskUsage`` (``istota.session.usage``) whose
``input_tokens`` follows the OpenAI-compat convention. This module is the one
place those get converted to a single vocabulary, so the DB schema and the read
surfaces never learn which brain produced a row.

Two groups of measures live here and they are deliberately not comparable:

* **Totals** sum across every request in the run — ``billed_input_tokens``,
  ``cache_read_tokens``, ``cache_write_tokens``, ``output_tokens``. For the CLI
  these come from ``modelUsage``, which is the billing basis: ``result.usage``
  covers only the main agent's conversation and under-reports spend by the CLI's
  own out-of-band calls. ``cost_usd`` comes from the frame's ``total_cost_usd``,
  which the per-model ``costUSD`` figures reproduce exactly.
* **Context measures** do not sum — ``initial_context_tokens`` and
  ``peak_context_tokens`` are a first and a max over per-request prompt sizes.

``billed_input_tokens`` excludes cache reads and cache writes throughout.

This module is pure: no DB, no config, no brain imports, no I/O, and neither
adapter raises. A renamed CLI field yields a zero in one column rather than an
exception in the brain's return path. ``TaskUsage`` is referenced under
``TYPE_CHECKING`` only and ``from_task_usage`` is structurally typed, so the
runtime import graph stays clean. It is *not* a "stdlib-only leaf" in the sense
AGENTS.md uses for modules a skill subprocess imports — no skill imports it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from istota.session.usage import TaskUsage


# What the CLI's init frame calls the credential it authenticated with, mapped
# to what the number it later reports actually means. A new CLI spelling is a
# one-line change here rather than a hunt through the surfaces.
#
# Two caveats on the subscription set. "none" was measured; the managed-login
# spelling is not confirmed against a capture, and an unrecognized value falls
# to "unknown" rather than being guessed, so a wrong spelling suppresses a
# label and never invents one. And "none" means "no Anthropic API key", which
# is also true of a Bedrock or Vertex deployment, where the reported figure IS
# real money — this mapping assumes first-party auth, which is the only shape
# this deployment runs.
API_KEY_SOURCES = frozenset({"ANTHROPIC_API_KEY", "apiKeyHelper"})
SUBSCRIPTION_KEY_SOURCES = frozenset({"none", "/login managed key"})

# SQLite stores INTEGER as signed 64-bit and raises OverflowError on bind for
# anything wider. Clamping here keeps a nonsense frame from costing the whole
# row in the best-effort writer downstream.
_SQLITE_INT_MIN = -(2**63)
_SQLITE_INT_MAX = 2**63 - 1

# What a usage row records as its owner when the spend has no single user: the
# channel sleep-cycle pass, shared briefing blocks. One spelling rather than one
# per caller, so a per-user grouping does not grow two buckets for the same idea.
SYSTEM_USER_ID = "__system__"

COST_BASIS_API = "api"
COST_BASIS_SUBSCRIPTION = "subscription"
COST_BASIS_ESTIMATED = "estimated"
COST_BASIS_UNKNOWN = "unknown"

TOTALS_SOURCE_MODEL_USAGE = "model_usage"
TOTALS_SOURCE_DERIVED = "derived"
TOTALS_SOURCE_UNKNOWN = "unknown"


def _int(value: Any, default: int = 0) -> int:
    """Coerce a JSON value to int, returning ``default`` for anything else.

    The CLI's frames are not a contract we control; a field that changes type
    must not raise out of the brain's return path. ``bool`` is excluded because
    it is an ``int`` subclass in Python and ``True`` would otherwise become a
    token count of 1.

    Non-finite floats are rejected rather than converted: ``json.loads`` accepts
    the bare tokens ``NaN`` and ``Infinity`` by default and the stream parser
    calls it without a ``parse_constant`` hook, so those values do reach here,
    and ``int(float("nan"))`` raises. Out-of-range integers are rejected for the
    same reason one layer down — see ``_SQLITE_INT_MAX``.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value if _SQLITE_INT_MIN <= value <= _SQLITE_INT_MAX else default
    if isinstance(value, float):
        if not math.isfinite(value):
            return default
        as_int = int(value)
        return as_int if _SQLITE_INT_MIN <= as_int <= _SQLITE_INT_MAX else default
    return default


def _float(value: Any, default: float = 0.0) -> float:
    """Coerce a JSON value to float, returning ``default`` for anything else.

    Non-finite values are rejected: a ``NaN`` reaching ``cost_usd`` poisons
    every later ``SUM`` and ``AVG`` over the column silently, which is worse
    than reporting nothing. ``openai_compat`` drops them on its own path for
    the same reason.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        as_float = float(value)
        return as_float if math.isfinite(as_float) else default
    return default


@dataclass
class RequestUsage:
    """One API request's usage, from a ``message_delta`` frame.

    ``prompt_tokens`` is the whole prompt the model saw — input plus cache read
    plus cache write — which is the quantity the context window bounds.
    """

    prompt_tokens: int = 0
    output_tokens: int = 0
    model: str = ""


@dataclass
class ModelUsage:
    """One model's share of a run, from a ``modelUsage`` entry."""

    model: str
    billed_input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    context_window: int = 0
    max_output_tokens: int = 0
    web_search_requests: int = 0


@dataclass
class BrainUsage:
    """One brain attempt's normalized usage.

    ``billed_input_tokens`` excludes cache reads and cache writes throughout.
    The three context fields are ``None`` rather than ``0`` when unmeasured, so
    SQL ``AVG`` skips them: a zero would average in and halve a mixed-brain mean
    with no indication why.
    """

    billed_input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    cost_basis: str = COST_BASIS_UNKNOWN
    totals_source: str = TOTALS_SOURCE_UNKNOWN
    # False when the run ended before a result frame. The token columns are then
    # meaningless zeroes, so every token aggregate filters on this.
    has_totals: bool = False
    turns: int = 0
    model_requests: int = 0
    subagent_requests: int = 0
    compacted_requests: int = 0
    initial_context_tokens: int | None = None
    peak_context_tokens: int | None = None
    context_window: int | None = None
    duration_ms: int = 0
    duration_api_ms: int = 0
    service_tier: str = ""
    session_id: str = ""
    model: str = ""
    models: list[ModelUsage] = field(default_factory=list)
    rate_limit: dict | None = None


def total_prompt_tokens(usage: BrainUsage) -> int:
    """Every token the run paid for on the input side, at any rate."""
    return (
        usage.billed_input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    )


def cache_hit_rate(usage: BrainUsage) -> float:
    """Share of prompt tokens served from cache, clamped to ``[0, 1]``.

    The clamp is not decoration: surfaces render this as a percentage, and only
    ``from_task_usage`` floors its subtraction at zero, so a frame reporting a
    negative count would otherwise print a rate above 100%.

    Note this denominator differs from ``native._log_cache_telemetry``'s, which
    divides by an inclusive ``input_tokens``. That function runs before
    conversion and is unchanged.
    """
    total = total_prompt_tokens(usage)
    if total <= 0:
        return 0.0
    return min(1.0, max(0.0, usage.cache_read_tokens / total))


def context_headroom_pct(usage: BrainUsage) -> float | None:
    """How much of the context window the run's peak prompt left unused.

    ``None`` when either the peak or the window is unknown — a headroom figure
    against an unknown limit would be an invention.
    """
    window = usage.context_window
    peak = usage.peak_context_tokens
    if not window or peak is None:
        return None
    return max(0.0, 1.0 - peak / window) * 100.0


def cost_basis_from_api_key_source(value: str | None) -> str:
    """What a cost figure means, given the credential the CLI authenticated with.

    Never guesses: an unrecognized spelling is ``unknown``, not ``api``, because
    labelling a plan-equivalent as real spend is the failure that matters.
    """
    if value is None:
        return COST_BASIS_UNKNOWN
    if value in API_KEY_SOURCES:
        return COST_BASIS_API
    if value in SUBSCRIPTION_KEY_SOURCES:
        return COST_BASIS_SUBSCRIPTION
    return COST_BASIS_UNKNOWN


def _dominant_model(models: list[ModelUsage]) -> str:
    """The model carrying the largest cost share.

    Ties break on output tokens, then on the lexicographically **greatest**
    name, so the parent row's ``model`` is deterministic for a given set of
    children. The direction is stated because a surface re-deriving the same
    ranking with an ascending sort would disagree with the stored row.
    """
    if not models:
        return ""
    best = max(models, key=lambda m: (m.cost_usd, m.output_tokens, m.model))
    return best.model


def from_cli_result(
    result_frame: dict | None,
    requests: list[RequestUsage] | None = None,
    api_key_source: str | None = None,
    *,
    model_hint: str = "",
    subagent_requests: int = 0,
    compacted_requests: int = 0,
    rate_limit: dict | None = None,
) -> BrainUsage:
    """Build a ``BrainUsage`` from the CLI's terminal frame and per-request frames.

    Totals come from ``modelUsage``, never from ``result.usage``. Measured on a
    two-turn run: ``modelUsage`` reproduces ``total_cost_usd`` exactly while
    ``result.usage`` is 533 input and 14 output tokens short, because it covers
    only the main agent's conversation and not the CLI's out-of-band calls.

    ``requests`` should already have sub-agent and compacted frames filtered out
    by the caller — the counts it skipped come back in as ``subagent_requests``
    and ``compacted_requests``. Those, ``rate_limit`` and ``model_hint`` are
    arguments rather than fields the caller assigns afterwards, so the returned
    dataclass is finished and this module keeps ownership of its invariants.

    ``model_hint`` is the model sniffed off the ``init`` frame. It is used only
    when ``modelUsage`` yielded no children, which is the one case the dominant
    model cannot be computed from.

    ``has_totals`` gates the four **token** columns and nothing else.
    ``cost_usd``, ``turns`` and the durations are read whenever the frame
    carries them, because a run can report a cost with no usable token
    breakdown, and dropping the figure would lose real spend.

    Pure and total: takes dicts, does no I/O, raises nothing — for any
    ``result_frame`` and for any ``requests`` list, well-formed or not.
    """
    frame = result_frame if isinstance(result_frame, dict) else {}
    try:
        reqs = list(requests) if requests is not None else []
    except TypeError:
        reqs = []
    prompt_sizes = [_int(getattr(r, "prompt_tokens", 0)) for r in reqs]

    usage = BrainUsage()
    usage.cost_basis = cost_basis_from_api_key_source(api_key_source)
    usage.subagent_requests = _int(subagent_requests)
    usage.compacted_requests = _int(compacted_requests)
    usage.rate_limit = rate_limit if isinstance(rate_limit, dict) else None

    model_usage = frame.get("modelUsage")
    if isinstance(model_usage, dict) and model_usage:
        for name, entry in model_usage.items():
            if not isinstance(entry, dict):
                continue
            row = ModelUsage(
                model=str(name),
                billed_input_tokens=_int(entry.get("inputTokens")),
                output_tokens=_int(entry.get("outputTokens")),
                cache_read_tokens=_int(entry.get("cacheReadInputTokens")),
                cache_write_tokens=_int(entry.get("cacheCreationInputTokens")),
                cost_usd=_float(entry.get("costUSD")),
                context_window=_int(entry.get("contextWindow")),
                max_output_tokens=_int(entry.get("maxOutputTokens")),
                web_search_requests=_int(entry.get("webSearchRequests")),
            )
            usage.models.append(row)

    if usage.models:
        usage.has_totals = True
        usage.totals_source = TOTALS_SOURCE_MODEL_USAGE
        for row in usage.models:
            usage.billed_input_tokens += row.billed_input_tokens
            usage.output_tokens += row.output_tokens
            usage.cache_read_tokens += row.cache_read_tokens
            usage.cache_write_tokens += row.cache_write_tokens
        usage.model = _dominant_model(usage.models)
        # The widest window any participating model offered, per spec. Note this
        # can name a different model than `model` does on a multi-model run, so
        # `context_headroom_pct` is a guide rather than a hard limit reading.
        windows = [r.context_window for r in usage.models if r.context_window > 0]
        if windows:
            usage.context_window = max(windows)
    else:
        usage.model = model_hint if isinstance(model_hint, str) else ""

    usage.cost_usd = _float(frame.get("total_cost_usd"))
    usage.turns = _int(frame.get("num_turns"))
    usage.duration_ms = _int(frame.get("duration_ms"))
    usage.duration_api_ms = _int(frame.get("duration_api_ms"))
    session_id = frame.get("session_id")
    usage.session_id = session_id if isinstance(session_id, str) else ""

    raw_usage = frame.get("usage")
    if isinstance(raw_usage, dict):
        tier = raw_usage.get("service_tier")
        usage.service_tier = tier if isinstance(tier, str) else ""

    if prompt_sizes:
        usage.model_requests = len(prompt_sizes)
        usage.initial_context_tokens = prompt_sizes[0]
        usage.peak_context_tokens = max(prompt_sizes)

    return usage


def from_task_usage(
    task_usage: "TaskUsage | None",
    *,
    cost_reported: bool = False,
) -> BrainUsage:
    """Convert the native brain's ``TaskUsage`` to the shared vocabulary.

    Two conversions matter:

    ``billed_input_tokens = max(0, input_tokens - cache_read_tokens)``, because
    OpenAI-compat ``prompt_tokens`` includes cached reads
    (``llm/openai_compat.py`` maps ``input_tokens = prompt_tokens`` and
    ``cache_read_tokens = prompt_tokens_details.cached_tokens``) while
    Anthropic's input count excludes them.

    **Cache writes are not subtracted**, and that is a labelled imprecision
    rather than an oversight: ``cache_creation_input_tokens`` is an Anthropic
    extension read best-effort from either of two places, and no OpenAI-compat
    spec says whether a proxy folds it into ``prompt_tokens``. Rows therefore
    carry ``totals_source="derived"``, which surfaces must not mix with
    ``model_usage`` rows in any cost-per-token figure.

    ``cost_reported`` says whether the provider returned a cost of its own
    (``Usage.cost_usd is not None``, which means an OpenRouter endpoint) or
    ``TaskUsage.add`` fell back to catalog prices. The catalog prices an unknown
    model at zero, so without the distinction a direct-Anthropic or local
    deployment would write a fabricated ``0.0`` labelled as real spend. The
    caller owns the signal because ``TaskUsage`` does not record it and this
    change does not alter that type.

    ``TaskUsage.add`` makes that choice **per turn**, so the caller must pass
    the conservative reading: ``cost_reported=True`` only when *every*
    accumulated turn reported a cost. One catalog-priced turn among several
    degrades the whole row to ``estimated``, because a total that is partly
    invented is not an ``api`` figure.

    Context columns stay ``None``: the native loop does not track per-request
    prompt sizes, and adding that is a separate change.
    """
    usage = BrainUsage()
    if task_usage is None:
        return usage

    input_tokens = _int(getattr(task_usage, "input_tokens", 0))
    cache_read = _int(getattr(task_usage, "cache_read_tokens", 0))
    usage.billed_input_tokens = max(0, input_tokens - cache_read)
    usage.cache_read_tokens = cache_read
    usage.cache_write_tokens = _int(getattr(task_usage, "cache_write_tokens", 0))
    usage.output_tokens = _int(getattr(task_usage, "output_tokens", 0))
    usage.cost_usd = _float(getattr(task_usage, "cost_usd", 0.0))
    usage.turns = _int(getattr(task_usage, "turns", 0))
    # An attempt that died before its first turn accumulates nothing — the
    # native loop only folds usage in on a non-empty payload — so an all-zero
    # TaskUsage means "unmeasured", not "measured zero". Claiming totals there
    # would drag every native average toward zero, while the identical CLI case
    # (no result frame) is correctly counted as unmeasured.
    usage.has_totals = bool(
        usage.turns
        or usage.billed_input_tokens
        or usage.output_tokens
        or usage.cache_read_tokens
        or usage.cache_write_tokens
    )
    usage.totals_source = TOTALS_SOURCE_DERIVED
    if not usage.has_totals:
        # Nothing was measured, so there is no cost figure for a basis to
        # describe. Labelling it `estimated` would put unmeasured attempts into
        # a basis breakdown as though they carried a catalog price.
        usage.cost_basis = COST_BASIS_UNKNOWN
    else:
        usage.cost_basis = COST_BASIS_API if cost_reported else COST_BASIS_ESTIMATED
    return usage
