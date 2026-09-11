"""Construction and lookup for configured SMS provider adapters."""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping

from ....config import (
    Config,
    SMS_PROVIDER_NAMES,
    sms_provider_has_values,
    sms_provider_missing_fields,
)
from ._types import SmsProviderAdapter, SmsProviderName

AdapterBuilder = Callable[[Config], SmsProviderAdapter]


class SmsProviderRegistry:
    """Configured provider adapters with one active send adapter."""

    def __init__(
        self,
        *,
        active_name: SmsProviderName,
        adapters: Mapping[SmsProviderName, SmsProviderAdapter],
        active_enabled: bool = True,
    ) -> None:
        self._active_name = active_name
        self._adapters = dict(adapters)
        self._active_enabled = active_enabled

    def get(self, name: str) -> SmsProviderAdapter | None:
        return self._adapters.get(name)  # type: ignore[arg-type]

    def active(self) -> SmsProviderAdapter | None:
        if not self._active_enabled:
            return None
        return self._adapters.get(self._active_name)

    def names(self) -> tuple[SmsProviderName, ...]:
        return tuple(self._adapters)

    def callback_only_names(self) -> tuple[SmsProviderName, ...]:
        if not self._active_enabled:
            return tuple(self._adapters)
        return tuple(name for name in self._adapters if name != self._active_name)


def _load_builder(provider: SmsProviderName) -> AdapterBuilder:
    module = importlib.import_module(f"{__package__}.{provider}")
    return module.build_adapter


def make_provider_registry(
    config: Config,
    *,
    builders: Mapping[SmsProviderName, AdapterBuilder] | None = None,
) -> SmsProviderRegistry:
    """Build active and callback-only adapters without provider knowledge."""
    active_name = config.sms.provider
    if active_name not in SMS_PROVIDER_NAMES:
        raise ValueError(f"unsupported SMS provider {active_name!r}")

    adapters: dict[SmsProviderName, SmsProviderAdapter] = {}
    for raw_name in SMS_PROVIDER_NAMES:
        name: SmsProviderName = raw_name  # type: ignore[assignment]
        if sms_provider_missing_fields(config, name):
            continue
        if not sms_provider_has_values(config, name):
            continue
        builder = builders[name] if builders is not None else _load_builder(name)
        adapter = builder(config)
        if adapter.name != name:
            raise ValueError(
                f"SMS adapter registered as {adapter.name!r}, expected {name!r}"
            )
        adapters[name] = adapter

    return SmsProviderRegistry(
        active_name=active_name,
        adapters=adapters,
        active_enabled=config.sms.enabled,
    )
