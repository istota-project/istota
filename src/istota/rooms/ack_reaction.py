"""A reaction instead of a reply, when the speech gate reads a turn as an ack (ISSUE-655).

In a ``friendly`` room the classifier can mark a turn ``kind: "ack"``: thanks,
an acknowledgement of what the bot just said. On a surface that has reactions
(Talk, a WhatsApp group on Baileys) that turn is answered with an emoji
rather than a model run and a message.

**The classifier names a type, the operator names the emoji** (ISSUE-657). An
ack carries an ``ack_type`` (`speech_gate.ACK_TYPES`, else ``default``), and
``[speech_gate.ack_reactions]`` maps each type to a list of emoji; one is
picked from the list by a hash of the inbound message id, so a message seen
twice always gets the same one. A type with no valid entry uses ``default``;
with no table, ``default`` is ``[speech_gate] ack_reaction``. Nothing a
participant writes can post an emoji the operator did not list.

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
a reply, NULL not tried) and the emoji sent in ``reaction``, beside the
``ack_type`` the gate wrote, so tuning can tell the two apart. Nothing else needs
counting: a reacted ack runs no model, so it has no `task_usage` row.
"""

from __future__ import annotations

import hashlib
import logging
import sqlite3
import unicodedata
from datetime import datetime, timedelta, timezone

from .speech_gate import ACK_TYPES, DEFAULT_ACK_TYPE, KIND_ACK, GateDecision

logger = logging.getLogger("istota.rooms.ack_reaction")

#: How long a held task waits for its reaction. Longer than either surface's
#: reaction call can take, short enough that a daemon that died mid-way
#: still answers the turn within a couple of minutes.
HOLD_SECONDS = 120

#: A reaction is one emoji, a skin-tone or ZWJ sequence included. Anything
#: longer is not one and is refused rather than posted.
MAX_REACTION_CHARS = 16

_warned: set[str] = set()

_KNOWN_KEYS = frozenset((*ACK_TYPES, DEFAULT_ACK_TYPE))


def _one_emoji(value: object) -> str | None:
    """``value`` as one reaction, or None.

    Empty is None, silently. Plain ASCII (an emoji never is), whitespace, a
    control character, or more than `MAX_REACTION_CHARS` is refused with one
    warning per value.
    """
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
                "speech gate: an ack_reaction entry is not a single emoji, dropping it",
            )
        return None
    return value


def _entries(value: object) -> list[str]:
    """A table value's valid emoji, in order. A bare string is a one-item list."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return []
    return [e for e in (_one_emoji(v) for v in value) if e is not None]


def reactions_for(config, ack_type: str | None = None) -> list[str]:
    """The emoji an ack of ``ack_type`` may be answered with; empty is off.

    ``[speech_gate] ack_reaction = ""`` is off everywhere, table or not, so an
    operator who turned reactions off is never opted back in. Otherwise the
    type's list from ``[speech_gate.ack_reactions]``, then that table's
    ``default``, then ``ack_reaction`` itself, the first with a valid entry.
    """
    gate = getattr(config, "speech_gate", None)
    single = getattr(gate, "ack_reaction", None)
    if not isinstance(single, str) or not single.strip():
        return []
    table = getattr(gate, "ack_reactions", None)
    table = table if isinstance(table, dict) else {}
    for key in table:
        if key not in _KNOWN_KEYS and f"key:{key}" not in _warned:
            _warned.add(f"key:{key}")
            logger.warning(
                "speech gate: ack_reactions has an unknown kind %r, ignoring it", key,
            )
    kind = ack_type if ack_type in ACK_TYPES else DEFAULT_ACK_TYPE
    for key in dict.fromkeys((kind, DEFAULT_ACK_TYPE)):
        found = _entries(table.get(key))
        if found:
            return found
    fallback = _one_emoji(single)
    return [fallback] if fallback is not None else []


def pick(config, ack_type: str | None, message_key: object) -> str | None:
    """The reaction for one ack, or None when reactions are off for it.

    Stable rather than random: chosen by a hash of the inbound message id
    (Talk's id, WhatsApp's stanza id), so a message the poller sees twice, or
    a turn settled late, gets the same emoji.
    """
    choices = reactions_for(config, ack_type)
    if not choices:
        return None
    digest = hashlib.sha256(str(message_key).encode("utf-8", "replace")).digest()
    return choices[int.from_bytes(digest[:8], "big") % len(choices)]


def should_hold(config, decision: GateDecision | None, *, can_react: bool) -> bool:
    """Whether a turn's task is created held for a reaction."""
    return (
        can_react and decision is not None and decision.speak
        and decision.kind == KIND_ACK
        and bool(reactions_for(config, decision.ack_type))
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
    reaction: str | None = None,
) -> bool:
    """Close a held task after its reaction; True when the task was removed.

    ``reacted`` removes the task while it is still the held, unattempted row.
    If a worker already took it (the hold ran out first) it is left to answer,
    and the turn gets both. Otherwise the task is released to run now.
    ``reaction`` is the emoji sent, recorded only when ``reacted``. Runs in
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
                "UPDATE speech_gate_decisions SET reacted = ?, reaction = ? "
                "WHERE id = (SELECT MAX(id) FROM speech_gate_decisions "
                "WHERE message_id = ? AND spoke = 1 AND kind = ?)",
                (
                    1 if reacted else 0, reaction if reacted else None,
                    message_id, KIND_ACK,
                ),
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
