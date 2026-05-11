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


MODULE_NAMES: frozenset[str] = frozenset({"feeds", "money", "location", "health"})

EXPERIMENTAL_MODULES: dict[str, str] = {
    "health": "module_health",
}
