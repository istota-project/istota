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
"""

from __future__ import annotations


MODULE_NAMES: frozenset[str] = frozenset({"feeds", "money", "location"})
