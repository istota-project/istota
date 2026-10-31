"""The per-sender record of suspected injection attempts on mail.

One row per flag in ``email_sender_flags``, scoped to ``(user, address)``. This
module owns the rules: the reason set, the address key, the window and the
exemptions. Neither the mail's body nor the model's wording is stored; the only
model-chosen value is a reason from ``REASONS``.

A flag ages out by a ``WHERE`` clause, so the count does not depend on when
``prune`` last ran.
"""

import sqlite3
from dataclasses import dataclass
from email.utils import parseaddr

from istota.config import Config
from istota.mail.support import own_addresses

REASONS = (
    "instruction_override",
    "impersonation",
    "pre_authorization",
    "encoded_payload",
    "hidden_content",
    "data_request",
    "other",
)


@dataclass(frozen=True)
class SenderFlagSummary:
    address: str
    count: int
    last_flagged_at: str
    reasons: tuple[str, ...]
    holding: bool


def address_key(sender: str | None) -> str | None:
    """The lowercased addr-spec, or None. No fallback to the raw string, unlike
    ``inbound._sender_key``: an unparseable sender has no address to hold a
    durable record against."""
    return (parseaddr(sender or "")[1] or "").strip().lower() or None


def normalize_reason(value: object) -> str:
    """Anything outside the set is ``other``: the model did say the mail was
    suspicious, and what is stored stays from the fixed set."""
    if isinstance(value, str) and value in REASONS:
        return value
    return "other"


def _window_filter(config: Config) -> tuple[str, tuple[str, ...]]:
    days = config.scheduler.email_sender_flag_window_days
    if days <= 0:
        return "", ()
    return " AND flagged_at > datetime('now', ?)", (f"-{int(days)} days",)


def count_in_window(
    conn: sqlite3.Connection, config: Config, user_id: str, address: str,
) -> int:
    """Counted flags for an ``address_key`` value."""
    clause, params = _window_filter(config)
    row = conn.execute(
        "SELECT COUNT(*) FROM email_sender_flags WHERE user_id = ? AND sender = ?" + clause,
        (user_id, address, *params),
    ).fetchone()
    return row[0]


def holding_count(
    conn: sqlite3.Connection, config: Config, user_id: str, sender: str | None,
) -> int:
    """The counted flags if they hold this sender's mail to this user, else 0."""
    threshold = config.scheduler.email_sender_flag_hold_threshold
    if threshold <= 0:
        return 0
    address = address_key(sender)
    if address is None:
        return 0
    if address in own_addresses(config, user_id):
        return 0
    if config.is_sender_named_as_trusted(user_id, address, conn):
        return 0
    count = count_in_window(conn, config, user_id, address)
    if count < threshold:
        return 0
    return count


def list_flags(
    conn: sqlite3.Connection, config: Config, user_id: str,
) -> list[SenderFlagSummary]:
    """Every address with a counted flag, most recently flagged first."""
    clause, params = _window_filter(config)
    rows = conn.execute(
        "SELECT sender, reason, flagged_at FROM email_sender_flags WHERE user_id = ?"
        + clause
        + " ORDER BY flagged_at DESC, id DESC",
        (user_id, *params),
    ).fetchall()

    counts: dict[str, int] = {}
    last_flagged: dict[str, str] = {}
    reasons: dict[str, list[str]] = {}
    for sender, reason, flagged_at in rows:
        if sender not in counts:
            counts[sender] = 0
            last_flagged[sender] = flagged_at
            reasons[sender] = []
        counts[sender] += 1
        if reason not in reasons[sender]:
            reasons[sender].append(reason)

    summaries = []
    for address, count in counts.items():
        summaries.append(SenderFlagSummary(
            address=address,
            count=count,
            last_flagged_at=last_flagged[address],
            reasons=tuple(reasons[address]),
            holding=holding_count(conn, config, user_id, address) > 0,
        ))
    return summaries


def clear(conn: sqlite3.Connection, user_id: str, sender: str) -> int:
    """Delete every row for this user and address, in or out of the window."""
    address = address_key(sender)
    if address is None:
        return 0
    cursor = conn.execute(
        "DELETE FROM email_sender_flags WHERE user_id = ? AND sender = ?",
        (user_id, address),
    )
    return cursor.rowcount


def prune(conn: sqlite3.Connection, window_days: int) -> int:
    """Delete the rows the window stopped counting. 0 or less keeps everything."""
    if window_days <= 0:
        return 0
    cursor = conn.execute(
        "DELETE FROM email_sender_flags WHERE flagged_at <= datetime('now', ?)",
        (f"-{int(window_days)} days",),
    )
    return cursor.rowcount
