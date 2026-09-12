"""Construction and lookup for configured WhatsApp provider adapters."""

from __future__ import annotations

import asyncio
import importlib
import logging
from collections.abc import Callable, Mapping

from ....config import (
    Config,
    WHATSAPP_PROVIDER_NAMES,
    whatsapp_provider_has_values,
    whatsapp_provider_missing_fields,
)
from ._types import WhatsAppProviderAdapter, WhatsAppProviderName

logger = logging.getLogger(__name__)

AdapterBuilder = Callable[[Config], WhatsAppProviderAdapter]


class WhatsAppProviderRegistry:
    """Configured provider adapters with one active send adapter."""

    def __init__(
        self,
        *,
        active_name: WhatsAppProviderName,
        adapters: Mapping[WhatsAppProviderName, WhatsAppProviderAdapter],
        active_enabled: bool = True,
    ) -> None:
        self._active_name = active_name
        self._adapters = dict(adapters)
        self._active_enabled = active_enabled

    def get(self, name: str) -> WhatsAppProviderAdapter | None:
        return self._adapters.get(name)  # type: ignore[arg-type]

    def active(self) -> WhatsAppProviderAdapter | None:
        if not self._active_enabled:
            return None
        return self._adapters.get(self._active_name)

    def names(self) -> tuple[WhatsAppProviderName, ...]:
        return tuple(self._adapters)

    def callback_only_names(self) -> tuple[WhatsAppProviderName, ...]:
        """Adapters built for something other than sending.

        **Never a mount decision.** `config.whatsapp_webhooks_enabled` is the
        only gate on serving `/webhooks/whatsapp`, and it reads `enabled` and
        the *active* provider — turning the block off is turning the account
        off, where SMS instead keeps a switched-away provider's routes alive.
        This list follows the SMS registry's shape and does name every built
        adapter on a disabled deployment, because what it answers is which
        credentials the deployment still holds, not which routes it should
        serve. Reading it as the mount would restore exactly the SMS rule this
        surface does not follow: Meta's route alive on a deployment that
        switched away from Cloud.
        """
        if not self._active_enabled:
            return tuple(self._adapters)
        return tuple(name for name in self._adapters if name != self._active_name)


def _contract_fault(name: str, adapter: WhatsAppProviderAdapter) -> str | None:
    """What this adapter gets wrong about the record it fills in, or ``None``.

    Three checks a provider module cannot fail in production without somebody
    having edited it, which is why they are worth making at build rather than
    at use: each one's symptom at use is silent, and two of them are silent in
    the direction the ledger cannot recover from.

    The awaitability check is the one that earns its place. `send` is declared
    `Awaitable` and `_send_claimed` awaits it, so a synchronous `send` raises
    `TypeError` *inside the claim-to-settle region* — where the backstop
    catches it and settles the row `unknown`, the one state an operator can
    never resolve, for a message that was never sent. A row spent that way is
    unrecoverable; a refusal here is a failed import in a test run.
    """
    if adapter.name != name:
        return (
            f"WhatsApp adapter registered as {adapter.name!r}, expected {name!r}"
        )
    if (adapter.parse_webhook is None) != (adapter.verify_signature is None):
        # `_types` calls an adapter with a webhook and no signature scheme "a
        # thing to refuse loudly rather than to express"; this is where it is
        # refused.
        return (
            f"WhatsApp adapter {name!r} must declare parse_webhook and "
            "verify_signature together, or neither"
        )
    if not asyncio.iscoroutinefunction(adapter.send):
        return (
            f"WhatsApp adapter {name!r} must declare an awaitable send; a "
            "synchronous one settles the ledger row `unknown`"
        )
    return None


def _load_builder(provider: WhatsAppProviderName) -> AdapterBuilder:
    module = importlib.import_module(f"{__package__}.{provider}")
    return module.build_adapter


def make_provider_registry(
    config: Config,
    *,
    builders: Mapping[WhatsAppProviderName, AdapterBuilder] | None = None,
) -> WhatsAppProviderRegistry:
    """Build the active adapter and any callback-only ones, without provider
    knowledge.

    The SMS registry's loop with one gate split in two, and the split is the
    difference between the two surfaces rather than a liberty taken with the
    copy. There, both roles are gated on the provider block holding some
    values *and* holding all of them, because every SMS provider's credentials
    live in the config file. Here they need not: an adapter whose credential
    is a paired session on disk declares no config fields at all, so a
    populated-block test reads False for it forever and would leave a
    deployment that selected it with no active adapter and every send recorded
    ``unconfigured``.

    So the *active* provider is built whenever nothing it declares is missing
    — which for a provider declaring nothing is always, and its credential is
    then refused at use, the ISSUE-058 rule this surface already follows for
    Meta's three secrets. A *non-active* one is built only if it is also
    populated, because the sole reason to keep it is a late delivery callback
    authenticating against credentials a switched-away deployment still holds,
    and a provider with nothing configured has no such callback to answer.
    """
    active_name = config.whatsapp.provider
    if active_name not in WHATSAPP_PROVIDER_NAMES:
        raise ValueError(f"unsupported WhatsApp provider {active_name!r}")

    adapters: dict[WhatsAppProviderName, WhatsAppProviderAdapter] = {}
    for raw_name in WHATSAPP_PROVIDER_NAMES:
        name: WhatsAppProviderName = raw_name  # type: ignore[assignment]
        if whatsapp_provider_missing_fields(config, name):
            continue
        if name != active_name and not whatsapp_provider_has_values(config, name):
            continue
        builder = builders[name] if builders is not None else _load_builder(name)
        adapter = builder(config)
        fault = _contract_fault(name, adapter)
        if fault is not None:
            if name != active_name:
                # Dropped rather than raised, and the asymmetry is the point.
                # Every fault here is a programming error in a provider module,
                # but the only caller in `src/` is `outbound.active_adapter`,
                # which catches everything and returns `None` — so a raise for
                # a *callback-only* adapter would take the active one down with
                # it and stop every send on the surface with `unconfigured`. A
                # broken provider nobody selected costs its own adapter and
                # nothing else.
                logger.error("whatsapp.provider.contract_violation %s", fault)
                continue
            raise ValueError(fault)
        adapters[name] = adapter

    return WhatsAppProviderRegistry(
        active_name=active_name,
        adapters=adapters,
        active_enabled=config.whatsapp.enabled,
    )
