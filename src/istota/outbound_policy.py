"""Outbound email approval policy: does this message's recipient set require a
hold?

Kept separate from :mod:`istota.outbound_drafts` so the email skill can import
the decision without pulling in the drafts store — the skill asks the question
on every send, and only writes a row on the rare hold.

The predicate keys on the **recipient**, never on the content of the message and
never on whether we have corresponded with that address before. That second
exclusion is the whole point of this module, and it is load-bearing:

An earlier attempt at this gate ("Layer A", ``d7aba2d`` + ``f36c6c2``, reverted
two hours later in ``67b5200`` + ``ff381d6``) derived its allowlist from
``sent_emails.to_addr`` and ``processed_emails.sender_email`` — i.e. from
observed correspondence. ``processed_emails`` records "we received mail from
this address", which is not a statement about trust at all, so a single inbound
message from a stranger permanently allowlisted them as an outbound recipient.
That inverts the gate: the addresses it most needs to hold are exactly the ones
that route mail to us. Neither table may be read from here, and "we already
replied to them once" is the same mistake wearing a different hat.

Authorization is explicit only: the user's own addresses, the operator's
configured ``trusted_email_senders`` patterns, and the runtime
``trusted_email_senders`` table the user writes via ``!trust`` / ``yes trust``.
All three are reached through :meth:`Config.is_trusted_email_sender`.

**One list, two meanings — deliberately.** Reusing ``trusted_email_senders``
widens what its existing rows authorize: they were written to mean "their mail
skips the inbound gate" and now also mean "I may mail them without approving
each message". Those are different decisions, and every row predating this
feature was made under the narrower one. Separate inbound and outbound lists
were considered and rejected: the overlap is right in practice, and the cost of
the collapse is paid by saying so — in ``!trust``, in the inbound gate prompt,
and in ``docs/features/email.md`` — rather than by carrying two lists that would
drift. This is the difference from the Layer A inversion above: here a human
deliberately authorized the address, just by answering a differently-worded
question. There, nobody authorized anything at all.
"""

from __future__ import annotations

import logging
import sqlite3
from email.utils import getaddresses
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .config import Config

logger = logging.getLogger(__name__)

# Ordered weakest → strongest. The operator sets a floor and the user may
# tighten but not loosen, so both halves of `effective_policy` compare on this.
_ORDER = {"off": 0, "untrusted": 1, "all": 2}

VALID_POLICIES = tuple(_ORDER)

# The reasons a message is held, as they appear in `outbound_drafts.hold_reason`
# and in the held envelope the skill returns.
HOLD_UNTRUSTED = "untrusted_recipient"
HOLD_ALL_MODE = "all_mode"


def effective_policy(config: "Config", user_id: str) -> str:
    """``max(operator floor, user setting)`` on the ``off < untrusted < all``
    ordering.

    Same shape as the Google scope ceiling (:mod:`istota.google_scopes`), in the
    other direction: there the operator caps what a user may grant, here the
    operator sets a minimum a user may only raise. An unset user value (``""``,
    the column default) resolves to the floor, which is what makes raising the
    floor actually reach every user who never touched the setting.

    A user value that is not one of the three is treated as unset — a
    hand-edited DB row should tighten toward the floor rather than silently
    disable a gate the operator asked for.
    """
    floor = getattr(config.email, "outbound_approval_floor", "untrusted")
    if floor not in _ORDER:
        # Config load validates and rejects this, so reaching it means the
        # Config was built in-process rather than loaded. Fail safe.
        logger.warning(
            "Invalid outbound_approval_floor %r; treating as 'untrusted'", floor,
        )
        floor = "untrusted"

    user = config.users.get(user_id)
    chosen = (getattr(user, "outbound_approval", "") or "").strip() if user else ""
    if chosen and chosen not in _ORDER:
        logger.warning(
            "Unknown outbound_approval %r for user %s; treating as unset "
            "(resolving to the operator floor %r)",
            chosen, user_id, floor,
        )
        chosen = ""
    if not chosen:
        return floor
    return chosen if _ORDER[chosen] > _ORDER[floor] else floor


def _pattern_matches_everything(pattern: str) -> bool:
    """Whether an fnmatch trust pattern clears any address at all.

    ``*`` and ``*@*`` do. So does anything whose domain part is a bare wildcard.
    A pattern like ``*@partner.example.org`` does not — that is the canonical,
    intended shape.
    """
    p = pattern.strip().lower()
    if not p:
        return False
    if "@" not in p:
        # No literal `@` means the pattern can only clear an address by
        # wildcarding across it.
        return "*" in p
    _, _, domain = p.rpartition("@")
    return domain in ("", "*") or set(domain) <= {"*", "?"}


# Warn once per (user, pattern) rather than once per send: this runs on every
# outbound message under the `untrusted` policy.
_warned_catch_all: set[tuple[str, str]] = set()


def _warn_catch_all_patterns(config: "Config", user_id: str) -> None:
    """Say so when a user's trust list disables the gate it is consulted by.

    ``trusted_email_senders`` was an inbound-only allowlist before this feature,
    where a broad pattern only means "don't ask me about mail arriving". It now
    also authorizes outbound, so a catch-all entry turns the `untrusted` policy
    into a no-op while the operator's floor still reads `untrusted` in config
    and in every log line — a gate that is off in a way nothing surfaces.

    A warning rather than a refusal: the entry predates this meaning and may
    have been deliberate for inbound, so failing the load would break a working
    deployment over a setting the user chose under different terms. The
    documented fix is to narrow the pattern.
    """
    user = config.users.get(user_id)
    if not user:
        return
    for pattern in (user.trusted_email_senders or []):
        if not _pattern_matches_everything(pattern):
            continue
        key = (user_id, pattern)
        if key in _warned_catch_all:
            continue
        _warned_catch_all.add(key)
        logger.warning(
            "trusted_email_senders pattern %r for user %s matches every "
            "address, so the 'untrusted' outbound approval policy holds "
            "nothing. That list now authorizes outbound mail as well as "
            "inbound; narrow the pattern to restore the gate.",
            pattern, user_id,
        )


def _own_addresses(config: "Config", user_id: str) -> set[str]:
    user = config.users.get(user_id)
    if not user:
        return set()
    return {a.strip().lower() for a in (user.email_addresses or []) if a}


def _expand(recipient: object) -> list[str] | None:
    """Every addr-spec in one recipient entry, lowercased. ``None`` if the entry
    is not usable, which the caller treats as a hold.

    **The gate must key on the same expansion the SMTP envelope will use.** That
    is the whole reason this is not a simple strip-and-lowercase: one entry may
    carry several addresses, because ``"a@x.test, b@y.test"`` is a legal value
    for the skill's ``--to`` and ``_recipients`` runs ``getaddresses`` over it to
    produce two envelope recipients. Treating that entry as a single opaque
    token hands the comma-joined string to ``fnmatch``, where a canonical
    ``*@domain`` trust pattern matches the whole thing — the ``*`` eats the
    untrusted address in front of the trusted one — and the gate clears a
    message that then goes to both. Expanding here is what keeps "checked" and
    "sent" the same set.

    ``None`` for a non-string, an empty entry, or one containing anything that
    is not an addr-spec. A dropped entry would be worse than a held one: the
    caller still sends it, so silently filtering would mean the set the gate
    checked and the set that goes out are different.
    """
    if not isinstance(recipient, str):
        return None
    raw = recipient.strip()
    if not raw:
        return None
    pairs = getaddresses([raw])
    if not pairs:
        return None
    addrs: list[str] = []
    for _, addr in pairs:
        addr = addr.strip().lower()
        # `getaddresses` echoes a bare token back unchanged
        # (`getaddresses(["garbage"]) == [("", "garbage")]`), so require the
        # shape rather than trusting it parsed something meaningful.
        if not addr or "@" not in addr:
            return None
        addrs.append(addr)
    return addrs


def recipients_require_hold(
    config: "Config",
    conn: sqlite3.Connection,
    user_id: str,
    recipients: list[str],
) -> str | None:
    """``None`` to send. Otherwise the ``hold_reason`` string.

    The single decision point — the skill and the tests both call this, and
    nothing reimplements the predicate.

    Every recipient must clear the bar; one that does not holds the whole
    message. There is deliberately no partial send: splitting a message so the
    trusted subset goes now and the rest waits would deliver something the user
    never read, under a subject line implying the full recipient list saw it.

    An empty recipient list, or one whose entries yield no usable address, holds
    under any policy above ``off``. It should not reach here (the skill resolves
    recipients first), but "nothing to check" must not read as "everything
    checked out".

    ``conn`` is **required**, and passing ``None`` raises rather than answering.
    The runtime ``trusted_email_senders`` table is one of the three
    authorization sources, so without a connection this function cannot tell a
    trusted correspondent from a stranger — and the two remaining branches would
    still answer ``None`` (send) for the user's own address. A gate that answers
    at all with half its inputs missing is a gate that fails open on the caller
    whose database was unavailable. The skill catches this and refuses the send
    outright, which is what the spec asks for.
    """
    if conn is None:
        raise ValueError(
            "recipients_require_hold requires a database connection: the "
            "runtime trusted-sender table is one of the authorization sources, "
            "so a missing connection must refuse the send rather than resolve "
            "it from the remaining two."
        )

    policy = effective_policy(config, user_id)
    if policy == "off":
        return None

    addrs: list[str] = []
    for entry in recipients:
        expanded = _expand(entry)
        if expanded is None:
            # An unusable entry is not evidence of anything. Hold.
            return HOLD_ALL_MODE if policy == "all" else HOLD_UNTRUSTED
        addrs.extend(expanded)
    if not addrs:
        return HOLD_ALL_MODE if policy == "all" else HOLD_UNTRUSTED

    if policy == "untrusted":
        _warn_catch_all_patterns(config, user_id)

    if policy == "all":
        # Only the user's own addresses go out unapproved. This is what keeps a
        # briefing or a self-addressed note fast in the strictest mode; anything
        # addressed outward is the user's decision to make.
        own = _own_addresses(config, user_id)
        if all(a in own for a in addrs):
            return None
        return HOLD_ALL_MODE

    # policy == "untrusted"
    for addr in addrs:
        if not config.is_trusted_email_sender(user_id, addr, conn):
            return HOLD_UNTRUSTED
    return None
