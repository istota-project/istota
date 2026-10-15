"""Scoped navigation counts; each domain keeps its own unread definition."""

from __future__ import annotations

import logging

from istota import db
from istota.briefings import _loader as briefings_loader, _migrate as briefings_migrate
from istota.briefings import db as briefings_db
from istota.config import Config
from istota.feeds import _loader as feeds_loader, _migrate as feeds_migrate
from istota.feeds import db as feeds_db

logger = logging.getLogger(__name__)


def get_nav_unread_counts(config: Config, username: str) -> dict[str, int | None]:
    """Collect in a worker, opening and closing each database independently."""
    counts: dict[str, int | None] = {"chat": None, "briefings": None, "feeds": None}
    for section in counts:
        try:
            if section != "chat" and not config.is_module_enabled(username, section):
                continue
            if section == "chat":
                with db.get_db(config.db_path) as conn:
                    counts[section] = db.count_user_web_unread_messages(conn, username)
            elif section == "briefings":
                ctx = briefings_loader.resolve_for_user(username, config)
                briefings_migrate.ensure_initialised(ctx, app_config=config)
                with briefings_db.connect(ctx.db_path) as conn:
                    counts[section] = briefings_db.count_unread_archive(conn)
            else:
                ctx = feeds_loader.resolve_for_user(username, config)
                feeds_migrate.ensure_initialised(ctx)
                with feeds_db.connect(ctx.db_path) as conn:
                    counts[section] = feeds_db.count_entries(conn, status="unread")
        except Exception as error:
            counts[section] = None
            logger.warning(
                "Navigation unread count failed for %s: %s", section, error,
                extra={"unread_section": section, "error_type": type(error).__name__},
            )
    return counts
