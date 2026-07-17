"""Shared email ownership resolution.

Determines which istota user (if any) owns an inbound email, using the same
plus-address → sender-match → thread-match precedence that ``poll_emails`` uses
to route. Extracted here so the inbound poll (``transport/email/inbound.py``)
and the read-side scope filter (``skills/email``) agree byte-for-byte on
ownership — the moment the skill can ``list``/``search`` a shared box, an
unscoped read would otherwise hand one user's mail to another.

Depends only on ``config`` (for the user table + bot address) and ``db`` (for
the ``sent_emails`` thread lookup). It deliberately does NOT import
``skills.email`` so it can be used from both the transport layer and the skill
without an import cycle (``email_support`` already imports ``skills.email``,
forming one edge of a potential loop).

The email object passed to these helpers is duck-typed: it only needs
``sender`` (str), ``to`` / ``cc`` (iterables of address strings), and
``references`` (str | None). Both the imap-tools ``Email`` returned by
``skills.email.read_email`` and the enriched ``EmailEnvelope`` satisfy this.
"""

from __future__ import annotations

import logging
import re

from . import db
from .config import Config

logger = logging.getLogger("istota.email_ownership")


def extract_user_from_recipient(config: Config, email) -> str | None:
    """Extract user_id from a plus-addressed recipient.

    Checks To and Cc for the ``bot+{user_id}@domain`` pattern. Returns the
    user_id when the plus-tag names a known user, else None. An unknown plus
    tag is logged and ignored (not treated as ownership).
    """
    if not config.email.bot_email or "@" not in config.email.bot_email:
        return None

    bot_local, bot_domain = config.email.bot_email.split("@", 1)

    pattern = re.compile(
        rf"^{re.escape(bot_local)}\+(.+)@{re.escape(bot_domain)}$",
        re.IGNORECASE,
    )

    for addr in list(getattr(email, "to", ()) or ()) + list(getattr(email, "cc", ()) or ()):
        match = pattern.match(addr)
        if match:
            candidate = match.group(1).lower()
            if candidate in config.users:
                return candidate
            else:
                logger.warning(
                    "Plus-address user '%s' not found in config (from %s)",
                    candidate, addr,
                )
    return None


def match_thread(conn, email) -> "db.SentEmail | None":
    """Return the ``sent_emails`` row this inbound email replies to, or None.

    Checks the References chain (which subsumes In-Reply-To for imap-tools
    messages): the last reference first (most likely direct parent), then any
    reference.
    """
    references = getattr(email, "references", None)
    if references:
        ref_ids = references.split()
        if ref_ids:
            match = db.find_sent_email_by_message_id(conn, ref_ids[-1])
            if match:
                return match
            match = db.find_sent_email_by_references(conn, ref_ids)
            if match:
                return match

    return None


def resolve_email_owner(config: Config, conn, email) -> str | None:
    """Resolve the owning user_id for an inbound email, or None if unowned.

    Precedence mirrors ``poll_emails``: plus-address → sender-match →
    thread-match. ``None`` means the mail belongs to no configured user — the
    *shared pool* (mail sent to the bare bot address by a stranger, or a plus
    tag for a user who isn't configured).

    ``conn`` may be ``None`` (e.g. a caller without DB access); the thread arm
    is skipped in that case. Never raises.
    """
    uid = extract_user_from_recipient(config, email)
    if uid:
        return uid

    sender = getattr(email, "sender", "") or ""
    uid = config.find_user_by_email(sender)
    if uid:
        return uid

    if conn is not None:
        match = match_thread(conn, email)
        if match:
            return match.user_id

    return None


def owner_in_scope(owner: str | None, scope: str, user_id: str) -> bool:
    """Scope-membership test.

    - ``mine``:   the mail is owned by ``user_id``.
    - ``shared``: the mail is owned by nobody (the shared pool).
    - ``all``:    ``mine`` ∪ ``shared``.

    Mail owned by *another* user is never in scope, in any mode. There is no
    scope value that returns another user's mail.
    """
    if scope == "mine":
        return owner == user_id
    if scope == "shared":
        return owner is None
    # "all" (default)
    return owner is None or owner == user_id
