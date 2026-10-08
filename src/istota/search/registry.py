"""Lazy registration keeps an unavailable module local to its source."""

import importlib
import logging
from functools import lru_cache

from istota.search.core import Provider

logger = logging.getLogger(__name__)


def _register_all() -> list[Provider]:
    found = []
    for name in ("framework", "briefings", "feeds", "health", "location", "money"):
        path = "istota.search.framework" if name == "framework" else f"istota.{name}.search"
        try:
            module = importlib.import_module(path)
            found.extend(module.PROVIDERS if name == "framework" else [module.PROVIDER])
        except Exception as exc:
            logger.warning("Search source %s unavailable (%s)", name, type(exc).__name__)
    return sorted(found, key=lambda p: p.order)


@lru_cache(maxsize=1)
def providers() -> list[Provider]:
    return _register_all()
