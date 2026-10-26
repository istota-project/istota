"""Per-session cost and token telemetry (Crush refinement item 6).

``ClaudeCodeBrain`` leaves cost opaque — the CLI doesn't surface per-call usage.
The native brain accumulates a ``TaskUsage`` across every turn and attaches it to
``BrainResult.usage``. Cost prefers the provider's own reported figure when the
endpoint returns one (OpenRouter's ``usage.cost``, real charged cost with markup);
otherwise it falls back to the model catalog's per-mtok prices (which, for an
OpenRouter deployment, are the live-fetched real prices). When neither is
available — no reported cost and a catalog price of 0.0 (the conservative default
for an unknown model) — cost surfaces as 0.0, unknown rather than wrong.

Distillation sub-agents (item 4, deferred) roll their usage into the parent via
``merge``, mirroring Crush's ``updateParentSessionCost()``.
"""

from __future__ import annotations

from dataclasses import dataclass

from istota.llm.catalog import ModelInfo, PriceTier
from istota.llm.types import Usage


# A 1-hour cache write is priced at twice input (Anthropic, OpenRouter); the
# catalog's one write price is the 5-minute one.
_CACHE_WRITE_1H_MULTIPLIER = 2.0


def cache_write_multiplier_for_ttl(ttl: str) -> float | None:
    """The ``cache_write_multiplier`` for a ``prompt_cache_ttl``; ``None`` keeps the catalog price."""
    return _CACHE_WRITE_1H_MULTIPLIER if ttl == "1h" else None


def price_usage(
    usage: Usage, info: ModelInfo, cache_write_multiplier: float | None = None
) -> float:
    """Cost in USD for one ``Usage`` against a model's catalog prices.

    ``cache_write_multiplier``, when set, prices a cache write as that multiple
    of the input price instead of the catalog's write price.

    When the model has price tiers, the highest tier whose threshold the
    request's total input (cached tokens included) exceeds prices the whole
    request.
    """
    prices: ModelInfo | PriceTier = info
    total_input = usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
    for tier in info.price_tiers:
        if total_input <= tier.input_tokens_above:
            continue
        if prices is info or tier.input_tokens_above > prices.input_tokens_above:
            prices = tier
    if cache_write_multiplier is None:
        write_price = prices.cache_write_price_per_mtok
    else:
        write_price = prices.input_price_per_mtok * cache_write_multiplier
    return (
        usage.input_tokens / 1_000_000 * prices.input_price_per_mtok
        + usage.output_tokens / 1_000_000 * prices.output_price_per_mtok
        + usage.cache_read_tokens / 1_000_000 * prices.cache_read_price_per_mtok
        + usage.cache_write_tokens / 1_000_000 * write_price
    )


@dataclass
class TaskUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost_usd: float = 0.0
    turns: int = 0

    def add(
        self,
        usage: Usage,
        info: ModelInfo,
        cache_write_multiplier: float | None = None,
        *,
        count_turn: bool = True,
    ) -> None:
        """Fold one turn's usage in.

        ``count_turn=False`` adds the spend without a turn: a compaction
        summary call is part of the turn it happened in (ISSUE-845).

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
            self.cost_usd += price_usage(usage, info, cache_write_multiplier)
        if count_turn:
            self.turns += 1

    def merge(self, other: "TaskUsage") -> None:
        """Roll a sub-agent's accumulated usage into this one."""
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens
        self.cost_usd += other.cost_usd
        self.turns += other.turns
