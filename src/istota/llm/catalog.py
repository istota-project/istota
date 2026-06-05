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
from dataclasses import dataclass
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


def get_model_info(model_id: str) -> ModelInfo:
    """Return bundled metadata for ``model_id``, or the conservative default."""
    return _CATALOG.get(model_id, _DEFAULT)
