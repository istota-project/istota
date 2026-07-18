"""Bundled model metadata catalog.

Compaction needs a per-model context window; cost telemetry needs per-model
pricing. We bundle the catalog rather than fetching it — model *identity* stays
pinned in the brain (see ``.claude/rules/brain.md``); this only *describes* the
pinned identity (window, max output, capabilities, price).

Prior art: Crush's catwalk provider catalog. We adopt the embedded layer only;
remote sync is deferred. Operators can override per-model metadata from config
without a code change.

Entries load from the bundled ``model_catalog.json`` at import. Unknown models
fall back to ``_DEFAULT`` (conservative window, zero price → cost surfaces as
unknown rather than wrong). Prices in the bundled file are 0.0 for the pinned
Anthropic models; populate them via operator config when cost telemetry
matters.
"""

import json
from dataclasses import dataclass, fields, replace
from pathlib import Path

_CATALOG_PATH = Path(__file__).with_name("model_catalog.json")


@dataclass(frozen=True)
class ModelInfo:
    """Static metadata for one model."""

    id: str
    context_window: int
    max_output_tokens: int
    input_price_per_mtok: float = 0.0  # USD per 1M input tokens
    output_price_per_mtok: float = 0.0
    cache_read_price_per_mtok: float = 0.0
    cache_write_price_per_mtok: float = 0.0
    supports_tools: bool = True
    supports_vision: bool = False
    supports_thinking: bool = False


_DEFAULT = ModelInfo(id="unknown", context_window=200_000, max_output_tokens=16384)


def _load_catalog() -> dict[str, ModelInfo]:
    raw = json.loads(_CATALOG_PATH.read_text())
    catalog: dict[str, ModelInfo] = {}
    for model_id, fields in raw.items():
        catalog[model_id] = ModelInfo(id=model_id, **fields)
    return catalog


_CATALOG: dict[str, ModelInfo] = _load_catalog()

# Fields an operator override may set (everything but the id, which is the key).
_OVERRIDABLE_FIELDS = {f.name for f in fields(ModelInfo)} - {"id"}

# Operator-supplied per-model overrides ([brain.native.model_overrides]).
# Rebound atomically by set_model_overrides; merged over the bundled entry (or
# the conservative default) in get_model_info. This is the NB-4 lever: a
# non-Anthropic reasoning/vision model or a small-window local model that the
# bundled catalog doesn't know can declare its real capabilities/window without
# a code change, instead of being silently degraded to no-thinking / no-vision /
# 200k.
_OVERRIDES: dict[str, dict] = {}


def set_model_overrides(overrides: dict | None) -> None:
    """Replace the per-model override table (from ``[brain.native.model_overrides]``).

    Each value is a partial ModelInfo (any subset of window / max_output /
    capability / price fields); unknown keys are dropped. ``{}`` / ``None``
    clears the table. Rebinds atomically so a concurrent reader sees a coherent
    table.
    """
    global _OVERRIDES
    next_overrides: dict[str, dict] = {}
    if overrides:
        for model_id, raw in overrides.items():
            if not isinstance(model_id, str) or not isinstance(raw, dict):
                continue
            clean = {k: v for k, v in raw.items() if k in _OVERRIDABLE_FIELDS}
            if clean:
                next_overrides[model_id] = clean
    _OVERRIDES = next_overrides


def get_model_info(model_id: str) -> ModelInfo:
    """Return metadata for ``model_id``: operator override merged over the
    bundled entry (or the conservative default when the model is unknown)."""
    base = _CATALOG.get(model_id)
    override = _OVERRIDES.get(model_id)
    if override is None:
        return base if base is not None else _DEFAULT
    src = base if base is not None else _DEFAULT
    return replace(src, id=model_id, **override)
