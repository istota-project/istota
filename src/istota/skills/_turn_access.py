"""Whether a skill CLI's calling task answers its user's own turn.

A correspondent's admitted mail on an email thread room runs as the host at
full reach, so identity alone does not say who asked. A skill that writes
content other users read (a shared briefing block, a schedule, shared KV)
refuses a turn somebody else wrote. The task is read from ``ISTOTA_TASK_ID``,
which the proxy sets from the task, never from the command line.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)


def calling_task_id() -> int | None:
    """The calling task's id from ``ISTOTA_TASK_ID``, or None outside a task."""
    raw = os.environ.get("ISTOTA_TASK_ID", "").strip()
    # isascii: str.isdigit() accepts "²", which int() refuses.
    return int(raw) if raw.isascii() and raw.isdigit() else None


def calling_turn_passes(user_id: str, check=None) -> bool:
    """Whether the calling task answers ``user_id``'s own turn, ancestors included.

    ``check(conn, task)``, when given, must also hold. Anything unanswerable
    (no task, no database, a read error) is False.
    """
    from istota import db
    from istota.sandbox.skill_proxy import _owners_own_turn

    task_id = calling_task_id()
    db_path = os.environ.get("ISTOTA_DB_PATH", "")
    if task_id is None:
        return False
    if not db_path or not Path(db_path).is_file():
        logger.warning("could not read the calling task: no database at ISTOTA_DB_PATH")
        return False
    try:
        with db.get_db(Path(db_path)) as conn:
            if not _owners_own_turn(conn, task_id, user_id):
                return False
            return check is None or bool(check(conn, db.get_task(conn, task_id)))
    except Exception as exc:  # noqa: BLE001 — an unreadable task refuses
        logger.warning("could not read the calling task: %s", exc)
        return False
