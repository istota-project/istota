"""Per-vaccine educational explainer.

Curated, static content loaded from
:mod:`istota.health.data.immunization_explainers.json`. No runtime model
calls — the JSON is hand-reviewed and shipped with the codebase.

Returned payload:

* ``summary`` — substantive paragraph(s) about the vaccine
* ``why_it_matters`` — list of concrete reasons it matters

The legacy ``considerations`` field is gone; status-specific framing is
gone as well — content is keyed only on vaccine name.
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from pathlib import Path

from istota.health.models import HealthContext  # noqa: F401  (re-exported for callers)


logger = logging.getLogger(__name__)


_DISCLAIMER = (
    "Educational information only — not medical advice or diagnosis. "
    "Discuss vaccination decisions with your clinician."
)

_DATA_PATH = Path(__file__).parent / "data" / "immunization_explainers.json"


@lru_cache(maxsize=1)
def _load_explainers() -> dict[str, dict]:
    """Read the bundled JSON once; return ``{name: {summary, why_it_matters}}``."""
    try:
        raw = _DATA_PATH.read_text(encoding="utf-8")
    except OSError as e:
        logger.warning("health_imm_explainer_data_read_failed error=%s", e)
        return {}
    try:
        entries = json.loads(raw)
    except json.JSONDecodeError as e:
        logger.error("health_imm_explainer_data_parse_failed error=%s", e)
        return {}
    out: dict[str, dict] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            continue
        summary = entry.get("summary") or ""
        why = entry.get("why_it_matters") or []
        if not isinstance(why, list):
            why = []
        out[name] = {
            "summary": summary,
            "why_it_matters": [w for w in why if isinstance(w, str) and w.strip()],
        }
    return out


def get_explainer(
    *,
    name: str,
    display_name: str,
    status: str,
) -> dict:
    """Return the static explainer payload for ``name``.

    The shape is always the same; the UI gates rendering on ``source``.
    ``source`` is ``"static"`` for curated content, or ``"fallback"`` if
    the vaccine has no entry in the bundled JSON.
    """
    data = _load_explainers().get(name)
    if data is None:
        return {
            "name": name,
            "display_name": display_name,
            "status": status,
            "summary": (
                f"{display_name} is recommended for many adults; the current "
                "coverage indicator shows that records or doses may be "
                "incomplete. Confirm your history and the current recommended "
                "schedule with a clinician."
            ),
            "why_it_matters": [],
            "disclaimer": _DISCLAIMER,
            "source": "fallback",
            "generated_at": None,
        }
    return {
        "name": name,
        "display_name": display_name,
        "status": status,
        "summary": data["summary"],
        "why_it_matters": data["why_it_matters"],
        "disclaimer": _DISCLAIMER,
        "source": "static",
        "generated_at": None,
    }
