"""WhatsApp provider adapter contracts and construction."""

from ._types import WhatsAppProviderAdapter, WhatsAppProviderCaps
from .registry import WhatsAppProviderRegistry, make_provider_registry

__all__ = [
    "WhatsAppProviderAdapter",
    "WhatsAppProviderCaps",
    "WhatsAppProviderRegistry",
    "make_provider_registry",
]
