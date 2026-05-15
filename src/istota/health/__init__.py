"""Per-user health module — body stats, bloodwork panels, biomarker trends.

Per-user SQLite at ``{workspace}/health/data/health.db``. On by default;
per-user opt-out via ``disabled_modules``.

Architecturally mirrors :mod:`istota.feeds`, :mod:`istota.location`, and
:mod:`istota.money`: a workspace synthesiser, a per-user loader, a SQLite
layer, FastAPI routes, and an in-process CLI facade exposed as the
``health`` skill.
"""

from istota.health._loader import (
    UserNotFoundError,
    list_users,
    resolve_for_user,
)
from istota.health._migrate import ensure_initialised
from istota.health.db import connect, init_db
from istota.health.models import HealthContext


__version__ = "0.1.0"
__all__ = [
    "HealthContext",
    "UserNotFoundError",
    "connect",
    "ensure_initialised",
    "init_db",
    "list_users",
    "resolve_for_user",
]
