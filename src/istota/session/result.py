"""Result composition and malformed-output detection.

Brain-agnostic post-processing of a model run's final text. Operates on the
``(result_text, execution_trace)`` pair that any brain produces, so it lives in
the session layer rather than inside a specific brain. Extracted verbatim from
``executor.py`` in Phase 0 of the agent-loop migration; the executor re-exports
every public symbol for backward compatibility.

Two mechanisms share one ``_last_substantial_region()`` walker and both
**replace** ``result_text`` outright — never prepend or glue:

- **Mechanism A — CM-aware (ISSUE-026):** runs whenever ``cm_boundary`` events
  exist in the trace. Segments by ``cm_boundary`` and returns the last region
  ≥ ``_CM_SEGMENT_MIN_CHARS``. Runs for automated tasks too.
- **Mechanism B — terse-recovery (ISSUE-025):** runs only on non-automated
  tasks whose ``result_text`` is terse. Segments by both ``tool`` and
  ``cm_boundary`` and returns the last region ≥ ``_TRAILING_REGION_MIN_CHARS``.
"""

import logging
import re

logger = logging.getLogger("istota.session.result")


# Patterns that indicate leaked tool-call XML in model output.
# These are Claude Code's internal framing and should never appear in user-facing text.
_TOOL_SYNTAX_PATTERN = re.compile(
    r"</parameter>|</invoke>|<invoke\s|<parameter\s|</?antml:|</?thinking>",
)

# Matches fenced code blocks (``` ... ```) to strip before strict checking
_CODE_FENCE_PATTERN = re.compile(r"```[\s\S]*?```")


def detect_malformed_result(
    text: str,
    output_target: str | None = None,
) -> str | None:
    """Detect model output that is leaked tool-call XML rather than a real response.

    When output_target is "talk" (or "both"/"all"), applies stricter checking:
    Talk output should be valid markdown, so any tool-call XML outside of code
    fences is flagged regardless of how much other content surrounds it.

    Returns a reason string if malformed, None if the result looks okay.
    """
    if not text or not text.strip():
        return None

    stripped = text.strip()
    # Strict mode applies whenever Talk is one of the resolved delivery
    # destinations (Talk output must be valid markdown). Parse the descriptor
    # through the routing helpers so talk / both / all / talk:<token> all gate
    # strict, rather than matching a hardcoded string set.
    from ..transport import parse_output_target, plan_has_surface
    strict = plan_has_surface(parse_output_target(output_target), "talk")

    # Check for leaked tool-call XML syntax
    if strict:
        # Strip code fences first — XML in code blocks is fine
        outside_fences = _CODE_FENCE_PATTERN.sub("", stripped)
        if _TOOL_SYNTAX_PATTERN.search(outside_fences):
            return f"leaked tool-call XML in Talk output ({len(stripped)} chars)"
    else:
        if _TOOL_SYNTAX_PATTERN.search(stripped):
            # Only flag if the entire content is syntax fragments
            non_syntax = _TOOL_SYNTAX_PATTERN.sub("", stripped).strip()
            if len(non_syntax) < 20:
                return f"leaked tool-call XML ({len(stripped)} chars, {len(non_syntax)} chars of non-syntax content)"

    return None


# Minimum joined-region length for a CM segment to override result_text.
_CM_SEGMENT_MIN_CHARS = 200

# Below this absolute char count, result_text counts as "terse" and is eligible
# for replacement by a substantial trailing trace region.
_TERSE_RESULT_MAX_CHARS = 150

# Minimum joined-region length for terse-recovery to override result_text.
# Calibration is empirical; log overrides and tune over a sprint of data.
_TRAILING_REGION_MIN_CHARS = 500

# Source types whose tasks emit structured / programmatic output and never
# benefit from terse-recovery (Mechanism B). Mechanism A still runs for these.
_AUTOMATED_SOURCE_TYPES = frozenset({"scheduled", "briefing"})

# Result texts that are clearly references rather than the answer itself,
# regardless of length.
_TERSE_REFERENCE_RE = re.compile(
    r"^(see above|as (shown|stated)( above)?|done|ok|✓|"
    r"that's everything|that('s| is) it|all done)\.?$",
    re.IGNORECASE,
)


def _text_similarity(a: str, b: str) -> float:
    """Return 0.0–1.0 Jaccard similarity between two strings using word bigrams.

    More robust than SequenceMatcher for repetitive text patterns.
    For very long strings, compare just the first 8000 chars to stay fast.
    """
    limit = 8000
    a_words = a[:limit].lower().split()
    b_words = b[:limit].lower().split()
    if len(a_words) < 2 or len(b_words) < 2:
        return 1.0 if a[:limit] == b[:limit] else 0.0
    shingles_a = {(a_words[i], a_words[i + 1]) for i in range(len(a_words) - 1)}
    shingles_b = {(b_words[i], b_words[i + 1]) for i in range(len(b_words) - 1)}
    intersection = len(shingles_a & shingles_b)
    union = len(shingles_a | shingles_b)
    return intersection / union if union else 0.0


def _last_substantial_region(
    trace: list[dict],
    delimiters: set[str],
    min_chars: int,
) -> str | None:
    """Walk the trace, group text events into regions delimited by event types
    in ``delimiters``, then return the joined text of the last region whose
    length is ≥ ``min_chars``. Returns ``None`` if no region qualifies.

    Adjacent ``text`` events within a region are joined with ``\\n\\n``, so
    a paragraph split into multiple events by streaming aggregates back into
    one region — no per-block size filter is needed.
    """
    regions: list[list[str]] = [[]]
    for entry in trace:
        et = entry.get("type")
        if et in delimiters:
            regions.append([])
        elif et == "text":
            t = entry.get("text", "").strip()
            if t:
                regions[-1].append(t)
    for seg in reversed(regions):
        joined = "\n\n".join(seg)
        if len(joined) >= min_chars:
            return joined
    return None


def _is_automated_task(task) -> bool:
    """True when the task is automated / structured-output and shouldn't
    trigger terse-recovery.

    Checks ``source_type`` plus structural fallbacks (``heartbeat_silent``,
    ``scheduled_job_id``) in case a future code path stamps a non-scheduled
    source_type on a heartbeat-style task. Defense in depth — robust to
    source_type churn without locking the gate to one string set.
    """
    if task is None:
        return False
    if getattr(task, "source_type", None) in _AUTOMATED_SOURCE_TYPES:
        return True
    if getattr(task, "heartbeat_silent", False):
        return True
    if getattr(task, "scheduled_job_id", None) is not None:
        return True
    return False


def _is_terse(text: str) -> bool:
    """True when ``text`` is short enough or matches a known reference
    pattern such that it's likely a stand-in rather than the real answer.
    Empty text is treated as terse (recovery is wanted)."""
    s = text.strip()
    if not s:
        return True
    if len(s) < _TERSE_RESULT_MAX_CHARS:
        return True
    return bool(_TERSE_REFERENCE_RE.match(s))


def _log_compose_override(
    task,
    mechanism: str,
    original: str,
    recovered: str,
) -> None:
    logger.info(
        "compose_full_result: mechanism=%s task_id=%s source_type=%s "
        "original_chars=%d recovered_chars=%d",
        mechanism,
        getattr(task, "id", None),
        getattr(task, "source_type", None),
        len(original.strip()),
        len(recovered),
    )


def _compose_full_result(
    result_text: str,
    execution_trace: list[dict],
    task=None,
) -> str:
    """Reconcile the model's ResultEvent with text events from the trace.

    Recovers from two failure modes:

    - **CM mid-response truncation (ISSUE-026):** context management fires
      mid-response, so ResultEvent only sees the post-CM tail. Mechanism A
      walks segments delimited by ``cm_boundary`` and returns the last one
      whose text crosses ``_CM_SEGMENT_MIN_CHARS``. Always runs when CM
      events are present, including for automated tasks.

    - **Terse-reference ResultEvent (ISSUE-025):** the model writes the real
      answer as a text event, does one more tool call, then ResultEvent
      comes back as ``"see above"`` / ``"done"`` / a one-line reference.
      Mechanism B walks segments delimited by both ``cm_boundary`` and
      ``tool``, returning the last region ≥ ``_TRAILING_REGION_MIN_CHARS``.
      Gated by ``_is_automated_task`` and ``_is_terse(result_text)`` —
      structured-output tasks and substantial results bypass.

    Returns ``result_text`` unchanged when no override is justified. Override
    or trust — never glue. Logs every override for calibration.
    """
    if not execution_trace:
        return result_text

    # Mechanism A — CM-aware. Always runs when CM events exist.
    if any(e.get("type") == "cm_boundary" for e in execution_trace):
        recovered = _last_substantial_region(
            execution_trace, {"cm_boundary"}, _CM_SEGMENT_MIN_CHARS,
        )
        if recovered is not None and recovered.strip() != result_text.strip():
            _log_compose_override(task, "cm_aware", result_text, recovered)
            return recovered
        return result_text

    # Mechanism B — terse-recovery. Source-type and terseness gates.
    if _is_automated_task(task):
        return result_text
    if not _is_terse(result_text):
        return result_text

    recovered = _last_substantial_region(
        execution_trace, {"tool", "cm_boundary"}, _TRAILING_REGION_MIN_CHARS,
    )
    if recovered is None:
        return result_text
    if recovered in result_text:
        return result_text

    _log_compose_override(task, "terse_recovery", result_text, recovered)
    return recovered
