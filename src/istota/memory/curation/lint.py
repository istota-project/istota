"""Phase-A lint pass for USER.md.

Detects bullets in USER.md that look like temporal facts (events with a
date directly attached to a temporal verb) and surfaces them as lint
candidates so a future PR can decide whether to migrate them to the KG.

This module DOES NOT mutate anything. The nightly curator calls
`find_temporal_bullets()` and writes the result to the audit log with
`entry_kind="lint_candidate"`. Phase B will gate the actual migration on
a config flag and emit `remove`/`add_fact` op pairs into the curator's op
list.

It also exposes `prepend_agents_header_if_missing()`, a one-shot
idempotent migration that prepends an `<!-- agents: ... -->` comment to
existing USER.md files lacking one. Same module by topical proximity —
both are nightly self-heals over USER.md content.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .types import SectionedDoc, classify_line, normalize_bullet_text, top_region_indices

logger = logging.getLogger("istota.memory.curation.lint")

LINT_SEEN_TTL_DAYS = 30

# Temporal-verb → predicate mapping. Conservative: a verb whose mapping
# is ambiguous or context-dependent (e.g. "moved", "joined", "left") is
# omitted from the dict so its bullets are never auto-classified.
_VERB_TO_PREDICATE: dict[str, str] = {
    "ordered": "acquired",
    "bought": "acquired",
    "purchased": "acquired",
    "acquired": "acquired",
    "returned": "disposed_of",
    "sold": "disposed_of",
    "decided": "decided",
    "started": "started",
    "stopped": "stopped",
    "finished": "completed",
    "completed": "completed",
}

# A "disposed of" two-word verb gets normalized into "disposed_of" before
# being looked up in `_VERB_TO_PREDICATE`.
_TWO_WORD_VERB_NORMALIZE = {
    "disposed of": "disposed",
}

# Match form 1 / form 2: `<verb> <body up to 80 chars> on YYYY-MM-DD`.
# We capture the verb, the body (object), and the date.
_VERB_ON_DATE_RE = re.compile(
    r"\b(ordered|bought|purchased|acquired|returned|disposed of|sold|"
    r"decided|started|stopped|finished|completed)\s+"
    r"(.{1,80}?)\s+on\s+(\d{4}-\d{2}-\d{2})\b",
    re.IGNORECASE,
)

# Match form 3: bullet starting with `YYYY-MM-DD:` followed by a verb-shaped
# clause. We don't try to interpret this; we surface it as a candidate
# without a predicate guess (predicate=None). Phase B can decide.
_LEAD_DATE_RE = re.compile(r"^\s*(\d{4}-\d{2}-\d{2}):\s*(.+)$")

# Heading allowlist (case-insensitive substring): bullets under these
# headings are treated as behavioral and never offered as candidates.
_BEHAVIOR_HEADING_TOKENS = (
    "communication style",
    "preferences",
    "behavior",
    "style",
    "tone",
    "defaults",
    "persona",
    "voice",
    "how to",
    "always",
    "never",
    "behalf",
    "instruction",
)


@dataclass
class TemporalBulletCandidate:
    heading: str
    bullet_text: str
    suggested_predicate: str | None
    suggested_object: str | None
    suggested_valid_from: str | None


def _heading_is_behavior(heading: str) -> bool:
    h = heading.lower()
    return any(tok in h for tok in _BEHAVIOR_HEADING_TOKENS)


_ARTICLE_PREFIXES = ("the ", "a ", "an ")


def _normalize_object(text: str) -> str:
    out = text.strip().rstrip(".,;:").lower()
    for prefix in _ARTICLE_PREFIXES:
        if out.startswith(prefix):
            out = out[len(prefix):]
            break
    return out


def find_temporal_bullets(
    doc: SectionedDoc,
    kg_facts_text: str,
    *,
    max_candidates: int = 3,
) -> list[TemporalBulletCandidate]:
    """Scan the top region of every section for date-stamped temporal bullets.

    Conservative on purpose:
      - Date must be the direct object of a temporal verb ("on YYYY-MM-DD").
        Bullets ending with `(noted YYYY-MM-DD)` are NOT a match.
      - Bullets under behavior-allowlisted headings are skipped.
      - Subsections are opaque; bullets under `### subheading` are not scanned.
      - A candidate whose suggested object substring already appears in the
        supplied `kg_facts_text` is dropped (cheap KG dedup pre-check).

    Caps at `max_candidates` to keep nightly churn bounded.
    """
    if not doc.sections:
        return []

    kg_lower = (kg_facts_text or "").lower()
    out: list[TemporalBulletCandidate] = []

    for section in doc.sections:
        if _heading_is_behavior(section.heading):
            continue
        start, end = top_region_indices(section)
        for i in range(start, end):
            line = section.lines[i]
            if classify_line(line) != "bullet":
                continue
            body = normalize_bullet_text(line)
            cand = _classify_bullet(body)
            if cand is None:
                continue
            predicate, obj, valid_from = cand
            if obj and predicate:
                obj_norm = _normalize_object(obj)
                if obj_norm and obj_norm in kg_lower:
                    continue
            out.append(
                TemporalBulletCandidate(
                    heading=section.heading,
                    bullet_text=body,
                    suggested_predicate=predicate,
                    suggested_object=_normalize_object(obj) if obj else None,
                    suggested_valid_from=valid_from,
                )
            )
            if len(out) >= max_candidates:
                return out

    return out


def _classify_bullet(body: str) -> tuple[str | None, str | None, str | None] | None:
    """Return `(predicate, object, valid_from)` or None if the bullet is
    not a temporal-fact candidate."""
    # Form 3 first — explicit date prefix.
    m = _LEAD_DATE_RE.match(body)
    if m:
        date, rest = m.group(1), m.group(2).strip()
        # No predicate guess for free-form lead-date bullets; we leave
        # interpretation to Phase B. Object is the rest verbatim.
        return (None, rest, date)

    m = _VERB_ON_DATE_RE.search(body)
    if not m:
        return None
    verb_raw = m.group(1).lower().strip()
    obj = m.group(2).strip()
    date = m.group(3)
    # Normalize "disposed of" → "disposed_of" lookup key.
    if verb_raw == "disposed of":
        predicate = "disposed_of"
    else:
        predicate = _VERB_TO_PREDICATE.get(verb_raw)
    if predicate is None:
        return None
    return (predicate, obj, date)


def _candidate_hash(heading: str, bullet_text: str) -> str:
    h = hashlib.sha256()
    h.update(heading.encode("utf-8"))
    h.update(b"\x00")
    h.update(bullet_text.encode("utf-8"))
    return h.hexdigest()[:16]


def filter_unseen_candidates(
    candidates: list[TemporalBulletCandidate],
    seen_path: Path,
    *,
    today: date | None = None,
    ttl_days: int = LINT_SEEN_TTL_DAYS,
) -> list[TemporalBulletCandidate]:
    """Drop candidates already logged within `ttl_days`. Persists the seen set.

    The sidecar at `seen_path` stores `{"hashes": {hash: "YYYY-MM-DD"}}`,
    keyed by sha256(heading + NUL + bullet_text)[:16]. Entries older than
    `ttl_days` are pruned on each call so a long-untouched bullet
    eventually re-surfaces. Best-effort I/O — read errors fall back to an
    empty seen set; write errors are logged and dropped (the lint pass
    re-emits next night, which is the existing behavior).
    """
    today = today or datetime.utcnow().date()
    seen: dict[str, str] = {}
    try:
        if seen_path.exists():
            data = json.loads(seen_path.read_text(encoding="utf-8") or "{}")
            seen = data.get("hashes") or {}
            if not isinstance(seen, dict):
                seen = {}
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("lint_seen_read_failed path=%s err=%s", seen_path, e)
        seen = {}

    fresh: dict[str, str] = {}
    for h, iso in seen.items():
        try:
            d = datetime.strptime(iso, "%Y-%m-%d").date()
        except (TypeError, ValueError):
            continue
        if (today - d).days <= ttl_days:
            fresh[h] = iso

    out: list[TemporalBulletCandidate] = []
    today_iso = today.isoformat()
    for cand in candidates:
        h = _candidate_hash(cand.heading, cand.bullet_text)
        if h in fresh:
            continue
        out.append(cand)
        fresh[h] = today_iso

    try:
        seen_path.parent.mkdir(parents=True, exist_ok=True)
        seen_path.write_text(
            json.dumps({"hashes": fresh}, ensure_ascii=False, sort_keys=True),
            encoding="utf-8",
        )
    except OSError as e:
        logger.warning("lint_seen_write_failed path=%s err=%s", seen_path, e)

    return out


_AGENTS_HEADER_MARKER = "<!-- agents:"

_AGENTS_HEADER_BLOCK = (
    "<!-- agents: This file holds behavioral instructions and stable context "
    "only. Temporal events (purchases, decisions, status changes — anything "
    "you'd date-stamp) and stable factual claims (allergies, family, biography) "
    "belong in the knowledge graph via `istota-skill memory_search add-fact`. "
    "Append behavioral instructions only via "
    "`istota-skill memory append --heading \"<existing heading>\"`. "
    "Never use `echo >>` on this file. -->\n"
)


def prepend_agents_header_if_missing(text: str) -> tuple[str, bool]:
    """Idempotently prepend the agents-header HTML comment.

    Returns `(new_text, changed)`. Detection is a simple substring match
    on `_AGENTS_HEADER_MARKER` to keep migrations stable across small
    wording tweaks of the comment block.
    """
    if _AGENTS_HEADER_MARKER in text:
        return text, False
    if text and not text.startswith("\n"):
        return _AGENTS_HEADER_BLOCK + text, True
    return _AGENTS_HEADER_BLOCK + text, True
