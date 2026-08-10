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
``references`` / ``in_reply_to`` (str | None, both read via ``getattr`` so a
caller carrying neither still works). Both the imap-tools ``Email`` returned by
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


# A msg-id as RFC 5322 writes it: angle-bracketed, no whitespace inside. Used to
# tokenize a References / In-Reply-To chain by its own grammar rather than by
# whitespace — see `parse_message_ids`.
_MSG_ID_RE = re.compile(r"<[^>\s]+>")


def parse_message_ids(value: str | None) -> list[str]:
    """The message ids in a References / In-Reply-To chain, in order.

    Splitting on whitespace is the obvious implementation and it is wrong for a
    header that arrived RFC 2047-encoded. Decoding is required to read one at
    all (Q-encoding writes a space as ``_``), but decoding *also* discards the
    linear whitespace between adjacent encoded-words, because RFC 2047 says that
    whitespace is not part of the value. For an identifier header it is the
    delimiter, so where the sender folded decides what survives:

    - fold inside an id (what production sent) — the halves rejoin correctly,
      and only concatenation gets the id back
    - fold at an id boundary — the two ids glue into ``<a@x><b@y>``, which no
      whitespace split can separate

    Both are real, so neither "join the chunks" nor "split on whitespace" is
    right on its own. The angle brackets are the grammar, and reading them is
    what handles both shapes with one rule.

    Falls back to whitespace splitting when nothing is bracketed, so a
    non-conforming sender emitting bare ids is no worse off than before.
    """
    if not value:
        return []
    ids = _MSG_ID_RE.findall(value)
    return ids if ids else value.split()


def match_thread(conn, email) -> "db.SentEmail | None":
    """Return the ``sent_emails`` row this inbound email replies to, or None.

    References first, because it is the fuller chain: the last reference (the
    most likely direct parent), then any reference. In-Reply-To is then tried on
    its own.

    That fallback is not redundant. References does **not** subsume In-Reply-To
    — the two are separate headers written by the sender, and one can arrive
    unusable while the other names our message exactly. A long thread whose
    References came back as a run of RFC 2047 encoded-words is the case that
    forced this: nothing in it could be compared to a message id, while
    In-Reply-To was clean and pointed straight at the row. Reading only
    References dropped the reply — no owner resolved means no task and no
    notification on any surface, which is silent by construction.

    Both headers are sender-controlled and neither is evidence of identity. That
    is unchanged by widening to the second one: a sender able to put an id in
    References could always put the same id in In-Reply-To. Everything that
    guards the thread route still applies — the caller drops a matched row whose
    ``user_id`` isn't the resolved user, and an untrusted sender still meets the
    confirmation gate.
    """
    ref_ids = parse_message_ids(getattr(email, "references", None))
    if ref_ids:
        match = db.find_sent_email_by_message_id(conn, ref_ids[-1])
        if match:
            return match
        match = db.find_sent_email_by_references(conn, ref_ids)
        if match:
            return match

    # One id per RFC 5322, but a non-conforming sender can emit several, so this
    # arm takes the `IN (…)` lookup rather than the single-id one. The two arms
    # therefore tie-break differently on duplicate `sent_emails.message_id` rows
    # (this one by most recent, the arm above arbitrarily) — deliberate, and
    # noted because it is the kind of asymmetry a later refactor flattens.
    irt_ids = parse_message_ids(getattr(email, "in_reply_to", None))
    if irt_ids:
        match = db.find_sent_email_by_references(conn, irt_ids)
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
