"""A reaction instead of a reply, when the speech gate reads a turn as an ack (ISSUE-655).

In a ``friendly`` room the classifier can mark a turn ``kind: "ack"``: thanks,
an acknowledgement of what the bot just said. On a surface that has reactions
(Talk, a WhatsApp group on Baileys) that turn is answered with one fixed emoji,
``[speech_gate] ack_reaction``, rather than a model run and a message. The
emoji is the operator's, never the model's, so nothing a participant writes
can choose what gets posted.

**The task is created held, and the reaction decides whether it stays.** The
reaction is a network call, so it goes after the inbound transaction commits,
and a reaction that fails has to fall back to the one-line reply. The reply
needs the task `record_inbound` would have built, with everything that
function knows about the turn, so it is built there as usual, with
``scheduled_for`` pushed `HOLD_SECONDS` out so no worker claims it. After the
commit the caller sends the reaction and calls `settle`: on success the held
task is deleted, so a reacted ack leaves no task row; on failure it is
released and runs as the short reply. A daemon that dies in between leaves the
hold to expire, and the turn is answered the old way, which is the right
default.

The decision row records the outcome in ``reacted`` (1 reacted, 0 fell back to
a reply, NULL not tried), so tuning can tell the two apart. Nothing else needs
counting: a reacted ack runs no model, so it has no `task_usage` row.
"""

from __future__ import annotations

import logging
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone

from .speech_gate import KIND_ACK, GateDecision

logger = logging.getLogger("istota.rooms.ack_reaction")

#: How long a held task waits for its reaction. Longer than either surface's
#: reaction call can take, short enough that a daemon that died mid-way
#: still answers the turn within a couple of minutes.
HOLD_SECONDS = 120

#: A reaction is one emoji, a skin-tone or ZWJ sequence included. Anything
#: longer is not one and is refused rather than posted.
MAX_REACTION_CHARS = 16

_warned: set[str] = set()


def reaction_for(config) -> str | None:
    """The configured reaction, or None when reactions are off.

    Empty is off. Plain ASCII (an emoji never is), whitespace, a control
    character, or more than `MAX_REACTION_CHARS` is refused with one warning
    per value, and the ack is answered with the short reply.
    """
    value = getattr(getattr(config, "speech_gate", None), "ack_reaction", None)
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value:
        return None
    malformed = (
        value.isascii()
        or len(value) > MAX_REACTION_CHARS
        or any(ch.isspace() or unicodedata.category(ch) == "Cc" for ch in value)
    )
    if malformed:
        if value not in _warned:
            _warned.add(value)
            logger.warning(
                "speech gate: ack_reaction is not a single emoji, replying instead",
            )
        return None
    return value


def should_hold(config, decision: GateDecision | None, *, can_react: bool) -> bool:
    """Whether a turn's task is created held for a reaction."""
    return (
        can_react and decision is not None and decision.speak
        and decision.kind == KIND_ACK and reaction_for(config) is not None
    )


def hold_until(now: datetime | None = None) -> str:
    """``scheduled_for`` for a held task, in SQLite's ``datetime('now')`` form."""
    now = now or datetime.now(timezone.utc)
    return (now + timedelta(seconds=HOLD_SECONDS)).strftime("%Y-%m-%d %H:%M:%S")


#: The held row and nothing else: still pending, never attempted. A task the
#: hold ran out on, was claimed, failed and is now waiting on the retry ladder
#: is also pending with a future ``scheduled_for``; it has an attempt, and
#: settling it would delete a turn already answered or skip its backoff.
_STILL_HELD = "id = ? AND status = 'pending' AND attempt_count = 0 AND scheduled_for IS NOT NULL"


def settle(
    conn: sqlite3.Connection, *, task_id: int, message_id: int | None, reacted: bool,
) -> bool:
    """Close a held task after its reaction; True when the task was removed.

    ``reacted`` removes the task while it is still the held, unattempted row.
    If a worker already took it (the hold ran out first) it is left to answer,
    and the turn gets both. Otherwise the task is released to run now. Runs in
    the caller's transaction under a savepoint, so a failure part-way writes
    nothing; never raises.
    """
    removed = False
    try:
        conn.execute("SAVEPOINT ack_settle")
    except Exception as e:  # noqa: BLE001 — the hold expiring is the fallback
        logger.warning("speech gate: could not settle a held ack: %s", e)
        return False
    try:
        if reacted:
            cur = conn.execute(f"DELETE FROM tasks WHERE {_STILL_HELD}", (task_id,))
            removed = cur.rowcount == 1
            if removed:
                conn.execute(
                    "UPDATE messages SET task_id = NULL WHERE task_id = ?", (task_id,),
                )
                conn.execute(
                    "UPDATE processed_whatsapp SET task_id = NULL WHERE task_id = ?",
                    (task_id,),
                )
            else:
                logger.warning(
                    "speech gate: task %s was claimed before its reaction settled",
                    task_id,
                )
        else:
            conn.execute(
                f"UPDATE tasks SET scheduled_for = NULL WHERE {_STILL_HELD}", (task_id,),
            )
        if message_id is not None:
            conn.execute(
                "UPDATE speech_gate_decisions SET reacted = ? WHERE id = ("
                "SELECT MAX(id) FROM speech_gate_decisions "
                "WHERE message_id = ? AND spoke = 1 AND kind = ?)",
                (1 if reacted else 0, message_id, KIND_ACK),
            )
        conn.execute("RELEASE ack_settle")
    except Exception as e:  # noqa: BLE001 — the hold expiring is the fallback
        logger.warning("speech gate: could not settle a held ack: %s", e)
        try:
            conn.execute("ROLLBACK TO ack_settle")
            conn.execute("RELEASE ack_settle")
        except Exception:  # noqa: BLE001
            pass
        return False
    return removed
