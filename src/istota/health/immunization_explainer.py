"""Per-vaccine educational explainer.

Mirrors :mod:`istota.health.explainer` but keyed on (vaccine name,
coverage status). Triggered for the routine + booster vaccines whose
status is overdue / never_recorded / series_incomplete — the cases
where the user would benefit from a short, hedged primer.

Hard rules baked into the prompt:

* Never claim a specific disease will or won't occur.
* Never prescribe vaccines, dosages, or contraindications.
* Always hedge ("may", "can", "is generally recommended for").
* Always close with "discuss with your clinician".
* JSON output only.

Parser is strict: a JSON object with ``summary``, ``why_it_matters``,
``considerations`` (the last two are arrays of short strings).
Anything else is rejected and the route returns a generic fallback so
the UI never shows raw model output.
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path

from istota.health import db as health_db
from istota.health.models import HealthContext


logger = logging.getLogger(__name__)


_DISCLAIMER = (
    "Educational information only — not medical advice or diagnosis. "
    "Discuss vaccination decisions with your clinician."
)

# Statuses that warrant a generated explainer. Up-to-date and risk-based
# vaccines don't auto-prompt.
EXPLAINABLE_STATUSES = frozenset({
    "overdue", "never_recorded", "series_incomplete", "expired",
})


def _build_prompt(
    *,
    name: str,
    display_name: str,
    status: str,
    category: str,
    typical_age_range: str | None,
    schedule: str,
) -> str:
    """Compose the brain prompt for one vaccine × status."""
    age_clause = (
        f"The vaccine is typically recommended for {typical_age_range}. "
        if typical_age_range else ""
    )
    return f"""You are writing a brief educational primer for a personal health-tracking app.

The user's coverage for the {display_name} vaccine is currently **{status}** ({category} category, schedule: {schedule}). {age_clause}Help the user understand the vaccine in general terms — not whether *they* should get it.

Produce a JSON object with exactly these keys:
- "summary": one or two plain sentences explaining what the vaccine protects against and who is generally recommended to receive it. Lay-readable, no diagnosis.
- "why_it_matters": an array of 3 to 5 short strings, each one a general reason this vaccine is recommended (population-level benefits, severity of the disease, indirect protection, etc.). Use "may", "can", "is associated with". Never assert it will or won't affect the reader specifically.
- "considerations": an array of 3 to 5 short strings, each one a non-prescriptive thing to consider (e.g. "discuss with your clinician whether you're due", "check your records or your state's immunization registry", "review contraindications with your provider before booking"). Never prescribe a specific schedule, brand, or contraindication.

HARD RULES — violating any of these makes the output unusable:
1. NEVER claim a specific disease will or will not occur.
2. NEVER prescribe vaccines, dosages, intervals, or contraindications.
3. NEVER quantify personal risk ("you have a 30% chance of…").
4. ALWAYS hedge with "may", "can", "is generally recommended for".
5. ALWAYS frame guidance as "discuss with your clinician".
6. Keep each bullet under 25 words.
7. Output ONLY the JSON object. No code fences, no commentary, no leading or trailing text.
"""


def _strip_fences(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = re.sub(r"^```[a-zA-Z]*\n", "", raw)
        if raw.endswith("```"):
            raw = raw[: -3]
    return raw.strip()


def _coerce_str_list(value, *, limit: int = 6) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        if len(out) >= limit:
            break
    return out


def _parse_response(raw: str) -> dict | None:
    """Strict JSON parse: summary (str), why_it_matters (list), considerations (list)."""
    cleaned = _strip_fences(raw)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError:
        m = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if not m:
            return None
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            return None
    if not isinstance(parsed, dict):
        return None
    summary = parsed.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return None
    why = _coerce_str_list(parsed.get("why_it_matters"))
    cons = _coerce_str_list(parsed.get("considerations"))
    if not why or not cons:
        return None
    return {
        "summary": summary.strip(),
        "why_it_matters": why,
        "considerations": cons,
    }


def _call_brain(prompt: str, config) -> str | None:
    try:
        from istota.brain import BrainRequest, make_brain  # noqa: PLC0415
    except ImportError as e:
        logger.warning("health_imm_explainer_brain_import_failed error=%s", e)
        return None
    if config is None:
        return None
    try:
        brain = make_brain(config.brain)
        model = brain.resolve_model_name("general")
    except Exception as e:  # noqa: BLE001
        logger.warning("health_imm_explainer_brain_init_failed error=%s", e)
        return None
    req = BrainRequest(
        prompt=prompt,
        allowed_tools=[],
        cwd=Path(getattr(config, "temp_dir", None) or "/tmp"),
        env=dict(os.environ),
        timeout_seconds=120,
        model=model,
        streaming=False,
    )
    try:
        result = brain.execute(req)
    except Exception as e:  # noqa: BLE001
        logger.warning("health_imm_explainer_brain_failed error=%s", e)
        return None
    if not result.success:
        logger.warning(
            "health_imm_explainer_brain_unsuccessful stop_reason=%s",
            getattr(result, "stop_reason", "?"),
        )
        return None
    return result.result_text or ""


def _fallback(
    *, name: str, display_name: str, status: str,
) -> dict:
    return {
        "name": name,
        "display_name": display_name,
        "status": status,
        "summary": (
            f"{display_name} is a vaccine for which your current coverage shows "
            f"as {status.replace('_', ' ')}. Records can lag, and what's "
            "recommended depends on your personal medical history."
        ),
        "why_it_matters": [
            "Vaccines provide protection against infections that can have serious complications.",
            "Coverage gaps often reflect missing records rather than missed doses — verify before scheduling.",
            "Risk varies by age, occupation, travel plans, and underlying conditions.",
        ],
        "considerations": [
            "Discuss whether you're due with your clinician at your next visit.",
            "Pull records from past providers or your state's immunization registry if available.",
            "Review contraindications and timing with your provider before booking.",
        ],
        "disclaimer": _DISCLAIMER,
        "source": "fallback",
        "generated_at": None,
    }


def get_or_generate(
    ctx: HealthContext,
    *,
    name: str,
    display_name: str,
    status: str,
    category: str,
    schedule: str,
    typical_age_range: str | None = None,
    config=None,
) -> dict:
    """Cache-first lookup. Returns ``{name, display_name, status, summary,
    why_it_matters, considerations, disclaimer, source, generated_at}``.

    ``source`` is ``"cache"`` on a cache hit, ``"generated"`` for fresh
    content, or ``"fallback"`` when the brain returns unusable output.
    Fallback responses are NOT cached.
    """
    if status not in EXPLAINABLE_STATUSES:
        # Caller should gate, but defend here too.
        return _fallback(
            name=name, display_name=display_name, status=status,
        )

    with health_db.connect(ctx.db_path) as conn:
        cached = health_db.get_immunization_explainer(conn, name, status)
    if cached:
        payload = cached["payload"] or {}
        return {
            "name": name,
            "display_name": display_name,
            "status": status,
            "summary": payload.get("summary", ""),
            "why_it_matters": payload.get("why_it_matters", []),
            "considerations": payload.get("considerations", []),
            "disclaimer": _DISCLAIMER,
            "source": "cache",
            "generated_at": cached["created_at"],
        }

    prompt = _build_prompt(
        name=name,
        display_name=display_name,
        status=status,
        category=category,
        typical_age_range=typical_age_range,
        schedule=schedule,
    )
    raw = _call_brain(prompt, config)
    parsed = _parse_response(raw) if raw else None
    if parsed is None:
        return _fallback(name=name, display_name=display_name, status=status)

    with health_db.connect(ctx.db_path) as conn:
        health_db.save_immunization_explainer(
            conn, name=name, status=status, payload=parsed,
        )
        conn.commit()
        stored = health_db.get_immunization_explainer(conn, name, status)
    return {
        "name": name,
        "display_name": display_name,
        "status": status,
        "summary": parsed["summary"],
        "why_it_matters": parsed["why_it_matters"],
        "considerations": parsed["considerations"],
        "disclaimer": _DISCLAIMER,
        "source": "generated",
        "generated_at": stored["created_at"] if stored else None,
    }
