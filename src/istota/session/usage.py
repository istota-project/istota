"""Per-session cost and token telemetry (Crush refinement item 6).

``ClaudeCodeBrain`` leaves cost opaque — the CLI doesn't surface per-call usage.
The native brain accumulates a ``TaskUsage`` across every turn and attaches it to
``BrainResult.usage``. Cost prefers the provider's own reported figure when the
endpoint returns one (OpenRouter's ``usage.cost``, real charged cost with markup);
otherwise it falls back to the bundled model catalog (item 1). When neither is
available — no reported cost and a catalog price of 0.0 (the default for the
pinned Anthropic IDs) — cost surfaces as 0.0, unknown rather than wrong.

Distillation sub-agents (item 4, deferred) roll their usage into the parent via
``merge``, mirroring Crush's ``updateParentSessionCost()``.
"""

from __future__ import annotations

from dataclasses import dataclass

from istota.llm.catalog import ModelInfo
from istota.llm.types import Usage


def price_usage(usage: Usage, info: ModelInfo) -> float:
    """Cost in USD for one ``Usage`` against a model's catalog prices."""
    return (
        usage.input_tokens / 1_000_000 * info.input_price_per_mtok
        + usage.output_tokens / 1_000_000 * info.output_price_per_mtok
        + usage.cache_read_tokens / 1_000_000 * info.cache_read_price_per_mtok
        + usage.cache_write_tokens / 1_000_000 * info.cache_write_price_per_mtok
    )


@dataclass
class TaskUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0

    def add(self, usage: Usage, info: ModelInfo) -> None:
        """Fold one turn's usage in.

        Cost prefers the provider's reported figure (``usage.cost_usd``,
        e.g. OpenRouter's real charged cost including markup) when present;
        otherwise it falls back to catalog-price computation against ``info``.
        ``None`` means the provider reported nothing (compute from catalog);
        ``0.0`` is a genuine free turn (respected as-is)."""
        self.input_tokens += usage.input_tokens
        self.output_tokens += usage.output_tokens
        self.cache_read_tokens += usage.cache_read_tokens
        self.cache_write_tokens += usage.cache_write_tokens
        if usage.cost_usd is not None:
            self.cost_usd += usage.cost_usd
        else:
            self.cost_usd += price_usage(usage, info)
        self.turns += 1

    def merge(self, other: "TaskUsage") -> None:
        """Roll a sub-agent's accumulated usage into this one."""
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cost_usd += other.cost_usd
        self.turns += other.turns
