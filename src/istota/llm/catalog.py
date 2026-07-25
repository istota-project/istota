"""Per-model metadata resolution (config-first + live enrichment).

Compaction needs a per-model context window; cost telemetry needs per-model
pricing. There is no longer a bundled, hand-maintained catalog file — it went
stale and only a handful of native deployments would ever read it. Model
*identity* stays pinned in the brain (see ``.claude/rules/brain.md``); this only
*describes* the pinned identity (window, max output, capabilities, price).

Consumed **only** by ``NativeBrain`` (``brain/native.py``), its cost helper
(``session/usage.py``), and the config override plumbing (``config.py``). The
default brains (``claude_code`` / ``tmux_claude``) never read it — they hand
short model ids to the ``claude`` CLI, which owns its own metadata.

Resolution is a three-layer chain, ``get_model_info`` staying pure + synchronous:

    operator model_overrides / context_window   (highest — partial, merged on top)
            ▼
    live-fetched catalog (_FETCHED)              (OpenRouter, installed by NativeBrain)
            ▼
    conservative default (_DEFAULT)              (unknowns; context_window = 200_000)

For the real deployment shape (native → OpenRouter) the fetched layer supplies
correct, self-updating window/capabilities/prices. For anything else (a local
vLLM/Ollama, a direct-Anthropic native we don't run), metadata comes from
operator config (``[brain.native.model_overrides]`` / ``context_window``) or the
default — never from a bundled file that can be wrong. A non-OpenRouter native
deployment declares its window in config; that is the documented contract.
"""

from dataclasses import dataclass, fields, replace


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


# The real floor for an unknown model — no bundled catalog sits above it now.
# 200k is a pure last-resort that OpenRouter enrichment covers in practice, and
# keeping it at 200k means zero regression versus the old bundled Anthropic
# entries. A local/self-hosted native deployment sets ``context_window`` (or a
# ``model_overrides`` entry). Overflow from an over-large window is recoverable
# (≤2 compact-and-retry); premature compaction from an under-large one is merely
# wasteful. Zero price → cost surfaces as unknown rather than wrong.
_DEFAULT = ModelInfo(id="unknown", context_window=200_000, max_output_tokens=16384)

# Fields an operator override may set (everything but the id, which is the key).
_OVERRIDABLE_FIELDS = {f.name for f in fields(ModelInfo)} - {"id"}

# Operator-supplied per-model overrides ([brain.native.model_overrides]).
# Rebound atomically by set_model_overrides; merged over the fetched entry (or
# the conservative default) in get_model_info. This is the NB-4 lever: a model
# no live catalog knows — or a single wrong field on one it does — can declare
# its real capabilities/window without a code change.
_OVERRIDES: dict[str, dict] = {}

# Live-fetched catalog layer (e.g. OpenRouter). Populated by NativeBrain when it
# talks to an OpenRouter endpoint; empty otherwise (→ _DEFAULT). Sits below
# operator overrides, above _DEFAULT.
_FETCHED: dict[str, ModelInfo] = {}


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


def set_fetched_catalog(entries: dict[str, ModelInfo] | None) -> None:
    """Install (or clear) the live-fetched catalog layer (e.g. OpenRouter).

    Rebinds atomically so a concurrent reader sees a coherent table. ``None`` /
    ``{}`` clears the layer, dropping resolution back to operator overrides over
    the conservative default. Sits below operator overrides, above ``_DEFAULT``.
    """
    global _FETCHED
    if not entries:
        _FETCHED = {}
        return
    _FETCHED = {k: v for k, v in entries.items() if isinstance(v, ModelInfo)}


def get_model_info(model_id: str) -> ModelInfo:
    """Return metadata for ``model_id``: operator override merged over the
    live-fetched entry (or the conservative default when the model is unknown).

    The returned ``id`` is always ``model_id`` (``_DEFAULT.id == "unknown"``, so
    a miss would otherwise leak the sentinel id to the caller).
    """
    base = _FETCHED.get(model_id) or _DEFAULT
    override = _OVERRIDES.get(model_id)
    if override is None:
        return base if base.id == model_id else replace(base, id=model_id)
    return replace(base, id=model_id, **override)
