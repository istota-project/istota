"""Run-now for a shared block, handed from the web process to the daemon.

The web container cannot generate a shared block correctly on the Docker
stack: it is not on the browser's network, so a ``browse`` source comes back
empty and the partial gather replaces the last good block, and its brain
breaker is not the daemon's (ISSUE-700). So the admin route leaves a request
here and the daemon's ``shared-block-request`` gate takes it on its next tick,
the shape the Health pane's doctor request already has (``doctor.request_run``,
``take_request``, ``record_answered``), except that a request stays pending until
it is answered.

A reserved ``shared_kv`` namespace, written by the web route and the daemon,
never by a task. Keys are ``request:<name>``, ``answered:<name>`` and, for an
agent block's research task, ``agent:<name>`` (``shared_block_agent``); block
names are slugs, so no prefix can collide with one.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger(__name__)

RUN_NAMESPACE = "_shared_block_run"
_REQUEST_PREFIX = "request:"
_ANSWERED_PREFIX = "answered:"
AGENT_PREFIX = "agent:"
# The poll runs on the scheduler's dispatch loop every tick, so a held write
# lock skips the tick rather than stalling it.
_POLL_BUSY_MS = 2000

# What a run came to, recorded for the settings page.
REGENERATED = "regenerated"
KEPT_PRIOR = "kept_prior"
BRAIN_UNAVAILABLE = "brain_unavailable"
FAILED = "failed"
NOT_FOUND = "not_found"
# An agent block's run: its task was queued, one was already running, or the
# block was deleted or left agent mode before its task's result was captured.
QUEUED = "queued"
IN_FLIGHT = "in_flight"
DISCARDED = "discarded"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def request_run(config: "Config", name: str) -> dict:
    """Ask the daemon to generate ``name`` on its next tick, joining a request
    still pending for the same block."""
    from istota import db

    with db.get_db(config.db_path) as conn:
        # One write transaction from the read, so two presses make one request.
        conn.execute("BEGIN IMMEDIATE")
        row = db.shared_kv_get(conn, RUN_NAMESPACE, _REQUEST_PREFIX + name)
        if row is not None:
            return json.loads(row["value"])
        request = {"id": uuid.uuid4().hex[:12], "name": name, "at": _now()}
        db.shared_kv_set(conn, RUN_NAMESPACE, _REQUEST_PREFIX + name,
                         json.dumps(request), "web")
    return request


def requests_pending(config: "Config") -> bool:
    """Whether any run is waiting. One indexed read, for the dispatch loop."""
    if not Path(config.db_path).exists():
        return False
    from istota import db

    with db.get_db(config.db_path, busy_timeout_ms=_POLL_BUSY_MS) as conn:
        row = conn.execute(
            "SELECT 1 FROM shared_kv WHERE namespace = ? AND key LIKE ? LIMIT 1",
            (RUN_NAMESPACE, _REQUEST_PREFIX + "%"),
        ).fetchone()
    return row is not None


def next_request(config: "Config") -> dict | None:
    """The oldest pending request, left in place until it is answered.

    Not removed here, so a daemon restarted mid-run answers it on its next
    tick rather than leaving the page waiting on a request nobody holds.
    """
    if not Path(config.db_path).exists():
        return None
    from istota import db

    with db.get_db(config.db_path) as conn:
        row = conn.execute(
            "SELECT value FROM shared_kv WHERE namespace = ? AND key LIKE ? "
            "ORDER BY updated_at, key LIMIT 1",
            (RUN_NAMESPACE, _REQUEST_PREFIX + "%"),
        ).fetchone()
    return json.loads(row["value"]) if row is not None else None


def record_answered(config: "Config", request: dict, outcome: str) -> None:
    """Mark a request answered with what the run came to, and remove it, in
    one transaction. Never raises."""
    from istota import db

    name = request["name"]
    value = json.dumps({"id": request["id"], "outcome": outcome, "at": _now()})
    try:
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            db.shared_kv_set(conn, RUN_NAMESPACE, _ANSWERED_PREFIX + name, value, "daemon")
            db.shared_kv_delete(conn, RUN_NAMESPACE, _REQUEST_PREFIX + name)
    except Exception as exc:  # noqa: BLE001 - a record of the run, not the run
        logger.warning("shared_block_run_answer_unwritten name=%s err=%s", name, exc)


def forget(conn, name: str) -> None:
    """Drop a block's pending request, last answer and agent run, on the
    caller's connection, so a block made again under the name starts with none.

    Tidy-up only for the agent run: capture discards a run whose block has no
    live row, since not every door that deletes a block calls this."""
    from istota import db

    for prefix in (_REQUEST_PREFIX, _ANSWERED_PREFIX, AGENT_PREFIX):
        db.shared_kv_delete(conn, RUN_NAMESPACE, prefix + name)


def run_state(conn, name: str) -> dict:
    """The pending request, the last answer and the agent run for one block,
    each or None."""
    from istota import db

    out: dict = {"pending": None, "answered": None, "agent": None}
    for field, prefix in (
        ("pending", _REQUEST_PREFIX), ("answered", _ANSWERED_PREFIX), ("agent", AGENT_PREFIX),
    ):
        row = db.shared_kv_get(conn, RUN_NAMESPACE, prefix + name)
        if row is not None:
            out[field] = json.loads(row["value"])
    return out
