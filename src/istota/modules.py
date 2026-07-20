"""Module registry for istota.

A "module" is an on-by-default feature with its own UI tab and a settings
page reachable via a cog icon (feeds, money, location). Distinct from:

* **Resources** — paths/identifiers the user owns (calendars, folders, todos).
* **Connected services** — per-user external API credentials (karakeep,
  google_workspace) consumed by skills, no module page.

Modules are enabled for everyone unless disabled per user via
``UserConfig.disabled_modules`` / ``user_profiles.disabled_modules``.
``Config.is_module_enabled(user_id, module)`` is the single source of
truth — every module gate goes through it.

``EXPERIMENTAL_MODULES`` maps a module name to its operator-scoped
experimental feature flag. A module listed here is hidden everywhere
(API mount, nav link, scheduler auto-seeded jobs) unless the operator
opts in via ``[experimental] features = ["<flag>"]`` in ``config.toml``.
This lets in-progress modules ship in the tree without leaking into
standard installs.
"""

from __future__ import annotations

import importlib.util


MODULE_NAMES: frozenset[str] = frozenset({"feeds", "money", "location", "health", "briefings"})

EXPERIMENTAL_MODULES: dict[str, str] = {}

# Modules whose functionality needs an optional install extra. When the extra
# isn't installed the module is treated as *unavailable* — hidden from the web
# UI and skipped by the scheduler — instead of half-present and crashing on
# first use. This is what lets the lean local install omit money entirely (no
# beancount) while a `uv tool install 'istota[local,money]'` lights it up. Each
# value is the top-level import name to probe. Modules absent from this map are
# always available (their deps ship in the core / `local` footprint).
MODULE_DEPENDENCIES: dict[str, tuple[str, ...]] = {
    "money": ("beancount",),
}

_AVAILABILITY_CACHE: dict[str, bool] = {}


def module_available(module: str) -> bool:
    """Whether a module's required Python dependencies are importable.

    ``find_spec`` checks importability without importing (fast, no side
    effects). Cached — installed packages don't change within a process.
    Modules with no declared dependency are always available.
    """
    deps = MODULE_DEPENDENCIES.get(module)
    if not deps:
        return True
    cached = _AVAILABILITY_CACHE.get(module)
    if cached is None:
        try:
            cached = all(importlib.util.find_spec(dep) is not None for dep in deps)
        except (ImportError, ValueError):  # parent package missing / bad name
            cached = False
        _AVAILABILITY_CACHE[module] = cached
    return cached
