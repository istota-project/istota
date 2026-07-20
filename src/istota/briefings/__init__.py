"""Native briefings module — first-class informational landing page.

A briefing is an ordered list of content *blocks*; each block fans in 1..N
*sources* (rss / email / browse / structured built-ins) that the LLM
synthesizes into one coherent titled section at generation time. Each rendered
briefing is archived for the landing page.

Per-user workspace under ``{workspace}/briefings`` with the SQLite DB relocated
to local disk (``Config.module_db_path``). Mirrors :mod:`istota.feeds`.
Scheduling + delivery stay framework-owned (``briefing_configs`` /
``briefing_state`` / ``check_briefings``); this module owns *content + archive*.
"""

from istota.briefings._loader import (
    BriefingsContext,
    UserNotFoundError,
    list_users,
    resolve_for_user,
)
from istota.briefings._migrate import blocks_from_components, ensure_initialised


__version__ = "0.1.0"
__all__ = [
    "BriefingsContext",
    "UserNotFoundError",
    "blocks_from_components",
    "ensure_initialised",
    "list_users",
    "resolve_for_user",
]
