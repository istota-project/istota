"""Which Istota user an authenticated WhatsApp message may act as.

**Identity is the one thing on this surface that is genuinely per adapter, and
this is the one module that knows it.** Everything else the adapter split
touches is a *capability* — the service window, templates, the billing circuit
— and `providers/_types.WhatsAppProviderCaps` keeps those out of common code by
having each gate read the field that names its own precondition. Identity
cannot be expressed that way: the two adapters do not differ in whether they
have an identity, they differ in *what the identity is*. Meta's is the BSUID, a
business-scoped opaque id it mints and hands over in the webhook payload;
Baileys' is the JID, `<number>@s.whatsapp.net`, which the socket reports. So
the resolution branches on the provider by name, once, here — rather than
being scattered as `if provider ==` through `webhook.py` and whatever Stage 5's
receiver turns out to be.

What does **not** branch is any of the rules. Both arms are the same five
steps in the same order, and the order is what makes them safe:

1. A durable adapter-native identity is required first, so a phone number is
   never sufficient once an identity exists.
2. A matching identity wins outright.
3. Only then may a bootstrap number enroll, and only onto a row carrying no
   identity **of this adapter** yet.
4. A bootstrap number matching a row that carries a *different* identity of
   this adapter is the recycled-line case: refuse, and alert. Latching there
   would hand the Istota principal to whoever holds the number now.
5. A conditional latch that loses its race, or collides with another user's
   row, fails closed rather than rebinding.

**The provider is provenance, never a config read.** It names the adapter that
produced the event, and the caller passes it because the caller is the adapter
(or the route mounted for one). Reading `config.whatsapp.provider` in here
instead would make a Cloud callback arriving on a Baileys deployment resolve
under Baileys rules, against a field it does not carry — the resolver would
answer "unknown sender" for the right reason by accident, and the moment a
second webhook-bearing adapter existed it would answer wrongly. The active
provider is a separate question, asked separately by `webhook._handle_inbound`
before any of this runs.

**The cross-adapter guarantee, stated once:** an event from one adapter can
only ever resolve through that adapter's identity column, and can only latch
onto a row whose corresponding column is unset. A row carrying a Cloud BSUID
and no JID is invisible to a Baileys lookup and available to a Baileys
bootstrap; the latch then stamps `provider` and — because the row belonged to
the other adapter — discards the previous holder's window and opt-out
(`db.latch_whatsapp_jid`). A stale identity of the wrong kind is therefore
never read, and never carried forward as authorization.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass

from ... import db
from ...user_profiles import is_e164
from . import bsuid_fingerprint, jid_fingerprint
from ._types import WhatsAppUserIdentity

logger = logging.getLogger(__name__)

#: The one JID domain this surface accepts. A JID is `<user>@<server>`, and the
#: server is what says which namespace the user part lives in. Only
#: `s.whatsapp.net` carries a phone number, which is what the bootstrap latch
#: compares against an operator's configured bootstrap number — so `@g.us` (a
#: group), `@broadcast` (a list) and `@lid` (WhatsApp's number-hiding linked-id
#: namespace) are refused rather than parsed. `@lid` is the one worth naming:
#: it is a durable per-contact id like the BSUID, so it is a plausible future
#: identity and is deliberately not one today — nothing in the enrollment story
#: could bootstrap it, since it carries no number to match.
JID_USER_DOMAIN = "s.whatsapp.net"

#: A JID's user part may be bounded before it reaches a query or a fingerprint.
#: An E.164 number is at most 15 digits; the slack is for a device suffix.
_MAX_JID_CHARS = 128


@dataclass(frozen=True)
class Resolution:
    """What the resolver concluded, and what the caller owes because of it.

    `user_id` is `None` for every refusal, and `disposition` then names which
    one — the value the dedup row and the log line carry.

    `send_id` is what `db.touch_whatsapp_binding` should write, and it is here
    rather than at the call site because it is a per-adapter answer: Meta
    hands out an opaque destination of its own that has to be recorded, and
    Baileys does not — its destination *is* the JID, already latched. `None`
    means "write nothing", which that function reads as leaving the column
    alone.

    `writes_service_window` is the same shape for the other column, and it is
    the one that would be easy to leave out. `last_user_message_at` is
    documented as the sole input to **Meta's** 24-hour service window, and
    nothing but a Cloud-authenticated message opens that window: a Baileys
    message is not a conversation Meta knows about. The discard in
    `db.latch_whatsapp_jid` covers switching *to* Baileys; it cannot cover
    switching *back*, because a row that still holds its BSUID needs no latch
    and so meets no discard — so a Baileys-era stamp would survive into a
    Cloud deployment and authorize a free-form send under a window Meta does
    not hold. Refusing to write it at all is what closes that, and it costs
    Baileys nothing, since `caps.has_service_window` is False there and no
    gate reads the column.
    """

    user_id: str | None
    disposition: str | None = None
    pending_alert: object | None = None
    send_id: str | None = None
    writes_service_window: bool = True


def normalize_jid(value: object) -> str:
    """A Baileys JID reduced to the one spelling the binding column holds.

    Lowercased, device suffix removed, validated as a phone JID — or `""` for
    anything this surface does not model.

    **One spelling, applied at every lookup and every latch**, and that is the
    load-bearing part rather than the parsing. Baileys reports a sender as
    `<number>@s.whatsapp.net` and, on a message from a linked device, as
    `<number>:<device>@s.whatsapp.net`; the device number changes when the
    user pairs a new phone or re-links. Storing one form and comparing the
    other means a legitimate user's next message misses the lookup, falls
    through to the bootstrap arm, finds their own row already carrying a
    *different* JID, and trips the recycled-number refusal — a takeover alarm
    and a dropped message per message, for somebody who did nothing.

    The user part must be **ASCII** digits: it is compared against an
    operator's configured E.164 bootstrap number to decide an enrollment, and
    a value that is not a phone number gets no benefit of the doubt. Same
    rule, same reason, as `_e164_from_wa_id` applies to Meta's `wa_id`.
    `isascii()` as well as `isdigit()`, because the latter is True for
    Arabic-Indic, Devanagari and a dozen other digit sets — the same pair
    `webhook.verify_subscription` applies to Meta's challenge, and for the
    same reason: the value reaches a uniquely-indexed identity column. It
    fails closed either way, since `is_e164`'s pattern is explicit ASCII
    classes so such a JID could never match a bootstrap number, but a value
    that can never enroll should not be storable as an identity.
    """
    text = value if isinstance(value, str) else ""
    text = text.strip().lower()
    if not text or len(text) > _MAX_JID_CHARS:
        return ""
    user, sep, server = text.partition("@")
    if not sep or server != JID_USER_DOMAIN:
        return ""
    # The device suffix, dropped before anything compares or stores the value.
    user = user.partition(":")[0]
    if not user or not user.isascii() or not user.isdigit():
        return ""
    return f"{user}@{JID_USER_DOMAIN}"


def jid_number(jid: str) -> str:
    """The E.164 number a normalized JID names, or `""`.

    A `+` is prepended and nothing else — no country-code guess, no
    punctuation cleanup — because the result is compared against an operator's
    configured bootstrap number to decide an enrollment. `_e164_from_wa_id`
    applies the identical rule to Meta's `wa_id`, which arrives in the same
    shape (bare digits, no `+`), and the two are deliberately the same rule
    written for two inputs rather than one function taking a flag: a JID needs
    normalizing first and a `wa_id` does not.
    """
    normalized = normalize_jid(jid)
    if not normalized:
        return ""
    candidate = "+" + normalized.partition("@")[0]
    return candidate if is_e164(candidate) else ""


def jid_from_number(number: object) -> str:
    """A configured E.164 number as the JID a Baileys socket can address.

    `jid_number`'s inverse, and the two are kept next to each other because a
    disagreement between them is silent: the bootstrap latch compares a
    *parsed* number against the operator's configured one, and this builds a
    *destination* out of that same configured value — so a spelling that
    round-trips through one and not the other enrolls a user the deployment
    can then never message.

    Refuses anything that is not E.164 rather than stripping punctuation into
    shape. The value goes onto the wire as a destination, and a number this
    function guessed at addresses somebody.
    """
    text = number if isinstance(number, str) else ""
    text = text.strip()
    if not is_e164(text):
        return ""
    return f"{text[1:]}@{JID_USER_DOMAIN}"


def address_for_binding(binding, address_field: str) -> str:
    """Where a message to this binding goes under an adapter, or ``""``.

    The adapter-native identity first, and the operator-configured bootstrap
    number rendered into that adapter's own spelling as the fallback — which
    is what the fallback is for on both adapters alike: a binding enrolled by
    number and not yet written in from, and (on Cloud) one whose send id
    collided with another user's row.

    **The two spellings live here rather than on the capability record**, one
    level up from `outbound._destination`, which now reads
    `caps.address_field` and knows nothing else. They are here specifically
    because this module owns the JID grammar and the E.164 rule that the
    bootstrap comparison uses: a rendering that disagreed with `normalize_jid`
    would produce a destination no later inbound message could ever match back
    to its own row. One table, in the module that has to agree with itself,
    rather than a branch in the send path — which is the distinction the
    spec's "no `if provider == …` scattered through common code" draws.

    An `address_field` this function does not know yields `""` rather than
    guessing, which every caller reads as "not enrolled" — the fail-closed
    direction, and the same answer a missing binding gets. It is reachable
    only from a provider module naming a column that does not exist.
    """
    if binding is None:
        return ""
    native = (getattr(binding, address_field, "") or "")
    native = native.strip() if isinstance(native, str) else ""
    if native:
        return native
    bootstrap = (binding.bootstrap_phone_number or "").strip()
    if not bootstrap:
        return ""
    if address_field == "send_id":
        # Meta accepts the E.164 number as a destination directly.
        return bootstrap
    if address_field == "jid":
        return jid_from_number(bootstrap)
    return ""


def any_identity(binding) -> str:
    """Whatever identity this row holds, for the existence question alone.

    Not a destination. `outbound.current_destination` is asked "is this user
    enrolled at all" by `WhatsAppTransport.resolve_target` on a deployment
    whose adapter could not be built — there is no `address_field` to read
    there and no send that could use the answer, and returning `""` would
    silently drop the WhatsApp leg from a delivery plan for a reason that is
    about the adapter rather than about the user.
    """
    if binding is None:
        return ""
    for value in (binding.send_id, getattr(binding, "jid", ""),
                  binding.bootstrap_phone_number):
        text = (value or "").strip() if isinstance(value, str) else ""
        if text:
            return text
    return ""


def _e164_from_wa_id(wa_id: str | None) -> str:
    """Meta's `wa_id` as E.164, or `''`.

    A `+` is prepended and nothing else: no country-code guess and no
    punctuation cleanup, matching `normalize_whatsapp_phone_number`. A value
    that is not all digits is not a phone number and gets no benefit of the
    doubt, because the result of this is compared against an operator's
    configured bootstrap number to decide an enrollment.
    """
    if not wa_id or not wa_id.isdigit():
        return ""
    candidate = "+" + wa_id
    return candidate if is_e164(candidate) else ""


def _write_identity_alert(conn, user_id: str, bsuid: str) -> object | None:
    """One deduplicated operator alert about a binding that stopped matching.

    Off WhatsApp by construction: `task_alert` rows are pushed by the
    notification routes, and the caller strips `whatsapp` from them — a surface
    whose identity is in doubt must not be the surface that reports it.
    Deduplicated on the *pair*, so a line that keeps sending bumps one row
    rather than raising a push per message.
    """
    from ...notification_resolvers import task_alert

    fingerprint = bsuid_fingerprint(bsuid)
    return task_alert.write(
        conn, user_id,
        dedup_key=f"whatsapp-identity:{fingerprint}",
        title="WhatsApp identity mismatch",
        body=(
            "A WhatsApp message arrived on this user's configured bootstrap "
            "number from a different WhatsApp identity, so it was ignored. "
            "Either the number was reassigned or the identity changed. Check "
            "with the user, then run `istota user ensure <user> "
            "--reset-whatsapp-identity` or set the new BSUID explicitly."
        ),
        params={"identity_fingerprint": fingerprint},
    )


def _write_jid_identity_alert(conn, user_id: str, jid: str) -> object | None:
    """The same alert, for the Baileys identity.

    **Its own dedup namespace, not a shared one**, and that is not tidiness:
    the key is `UNIQUE (user_id, source, dedup_key)`, so a Cloud mismatch and
    a Baileys mismatch sharing a prefix would bump one row instead of raising
    two — and a bump does not deliver, so the second adapter's alarm would be
    silent. The fingerprint is `jid_fingerprint`'s, which has its own salt
    domain again, so the two alerts about the same person do not join.

    The remedy names `--reset-whatsapp-identity`, which clears **both**
    adapters' identities (`db.reset_whatsapp_identity`). That is the honest
    instruction: there is no per-adapter reset verb, and offering one would
    leave the other adapter's stale identity resolving the previous holder.
    """
    from ...notification_resolvers import task_alert

    fingerprint = jid_fingerprint(jid)
    return task_alert.write(
        conn, user_id,
        dedup_key=f"whatsapp-jid-identity:{fingerprint}",
        title="WhatsApp identity mismatch",
        body=(
            "A WhatsApp message arrived on this user's configured bootstrap "
            "number from a different WhatsApp account, so it was ignored. "
            "Either the number was reassigned or the account was re-paired. "
            "Check with the user, then run `istota user ensure <user> "
            "--reset-whatsapp-identity` and have them message the number "
            "again."
        ),
        params={"identity_fingerprint": fingerprint},
    )


def _write_cross_adapter_alert(conn, user_id: str, fingerprint: str) -> object | None:
    """One operator alert: a principal was re-established from a number alone.

    **The alarm on a trade this module makes deliberately.** The bootstrap arm
    refuses a number whose row already carries a *different* identity — but
    only of the adapter being resolved. A row enrolled under the other adapter
    has an empty column here, so after a switch it is bootstrappable from the
    phone number, by whoever holds that number now, with no refusal. Under a
    single adapter that is precisely the recycled-line case the table exists
    to refuse.

    Refusing it is not the answer: on a genuine migration every user's row
    carries the old adapter's identity and none carries the new one, so a
    refusal would lock out the entire deployment and there is nothing in the
    row that tells that case apart from a recycled number. The number is the
    operator's own assertion that it belongs to this user, which is what
    licenses the latch — so the latch happens and the operator is told, rather
    than the decision being made silently either way.

    Its own dedup namespace, for the reason the other two have theirs: the key
    is unique per `(user, source, key)` and a bump does not deliver, so a
    shared prefix would fold this into a mismatch alert and deliver nothing.
    """
    from ...notification_resolvers import task_alert

    return task_alert.write(
        conn, user_id,
        dedup_key=f"whatsapp-cross-adapter:{fingerprint}",
        title="WhatsApp identity re-established after an adapter switch",
        body=(
            "A WhatsApp message arrived on this user's configured bootstrap "
            "number from the other messaging adapter, and was accepted: the "
            "number is what identifies them and this row carried no identity "
            "for this adapter yet. If the number has changed hands since it "
            "was configured, this message was not from this user — run "
            "`istota user ensure <user> --reset-whatsapp-identity` and check "
            "the number. Expected once per user after a deliberate switch."
        ),
        params={"identity_fingerprint": fingerprint},
    )


def _resolve_cloud(conn, identity: WhatsAppUserIdentity) -> Resolution:
    """The Cloud arm: BSUID, then bootstrap by `wa_id`, then refuse."""
    bsuid = identity.bsuid
    if not bsuid:
        return Resolution(None, "unknown_sender")
    bound = db.get_whatsapp_binding_by_bsuid(conn, bsuid)
    if bound is not None:
        return Resolution(bound.user_id, send_id=bsuid)

    number = _e164_from_wa_id(identity.wa_id)
    candidate = db.get_whatsapp_binding_by_phone(conn, number) if number else None
    if candidate is None:
        return Resolution(None, "unknown_sender")
    if candidate.bsuid:
        return Resolution(
            None, "identity_mismatch",
            _write_identity_alert(conn, candidate.user_id, bsuid),
        )
    # Read before the latch, which is about to clear it.
    crossed = bool(candidate.jid)
    try:
        latched = db.latch_whatsapp_bsuid(
            conn, candidate.user_id,
            bsuid=bsuid,
            send_id=bsuid,
            username=identity.username or "",
        )
    except sqlite3.IntegrityError:
        # Another user already holds this BSUID or send id. The partial unique
        # indexes are the arbiter and this side lost; fail closed rather than
        # taking an identity the database says is somebody else's.
        return Resolution(None, "identity_conflict")
    if not latched:
        # The row gained a BSUID between the read above and this write.
        return Resolution(None, "identity_conflict")
    return Resolution(
        candidate.user_id,
        send_id=bsuid,
        pending_alert=(
            _write_cross_adapter_alert(
                conn, candidate.user_id, bsuid_fingerprint(bsuid),
            )
            if crossed else None
        ),
    )


def _resolve_baileys(conn, identity: WhatsAppUserIdentity) -> Resolution:
    """The Baileys arm: JID, then bootstrap by the JID's own number.

    Step for step the Cloud arm above, with three differences that are all
    consequences of what a JID is rather than choices:

    - The enrollment number comes out of the identity itself rather than from
      a second field. Meta reports `wa_id` separately and may omit it for a
      username-only user; a phone JID always carries its number, so the
      bootstrap arm here can never be reached with nothing to match on.
    - `send_id` is `None`. The destination is the JID and the JID is on the
      row; there is no opaque second identifier to record, and writing the JID
      into a column carrying a partial unique index of its own would put one
      value under two uniqueness rules for no reader.
    - The service window is not written. See `Resolution`.
    """
    jid = normalize_jid(identity.jid)
    if not jid:
        return Resolution(
            None, "unknown_sender", send_id=None, writes_service_window=False,
        )
    bound = db.get_whatsapp_binding_by_jid(conn, jid)
    if bound is not None:
        return Resolution(
            bound.user_id, send_id=None, writes_service_window=False,
        )

    number = jid_number(jid)
    candidate = db.get_whatsapp_binding_by_phone(conn, number) if number else None
    if candidate is None:
        return Resolution(
            None, "unknown_sender", send_id=None, writes_service_window=False,
        )
    if candidate.jid:
        # The recycled-line case, one adapter over: this number's operator
        # binding already carries a *different* Baileys identity, so the
        # person holding the number now is not the person the row is about.
        return Resolution(
            None, "identity_mismatch",
            _write_jid_identity_alert(conn, candidate.user_id, jid),
            send_id=None, writes_service_window=False,
        )
    # Read before the latch, which is about to stamp over the provider.
    crossed = bool(candidate.bsuid)
    try:
        latched = db.latch_whatsapp_jid(
            conn, candidate.user_id,
            jid=jid,
            username=identity.username or "",
        )
    except sqlite3.IntegrityError:
        return Resolution(
            None, "identity_conflict", send_id=None, writes_service_window=False,
        )
    if not latched:
        # The row gained a JID between the read above and this write.
        return Resolution(
            None, "identity_conflict", send_id=None, writes_service_window=False,
        )
    return Resolution(
        candidate.user_id, send_id=None, writes_service_window=False,
        pending_alert=(
            _write_cross_adapter_alert(conn, candidate.user_id, jid_fingerprint(jid))
            if crossed else None
        ),
    )


_ARMS = {
    db.WHATSAPP_LEGACY_PROVIDER: _resolve_cloud,
    db.WHATSAPP_BAILEYS_PROVIDER: _resolve_baileys,
}


def resolve_inbound_identity(
    conn, identity: WhatsAppUserIdentity, *, provider: str
) -> Resolution:
    """Which Istota user this authenticated message may act as.

    `provider` is the adapter the event came from. An unrecognised one
    resolves nobody: there is no arm that could read its identity, and
    falling back to either adapter's would resolve a principal from a field
    the event never carried. It is logged, because reaching here means a
    caller passed a name the registry could not have produced.
    """
    arm = _ARMS.get(provider)
    if arm is None:
        logger.warning(
            "whatsapp.inbound.unknown_provider provider=%s: no identity arm, "
            "so no principal is resolved",
            provider,
        )
        return Resolution(None, "unknown_sender")
    return arm(conn, identity)


def identity_fingerprint(identity: WhatsAppUserIdentity, *, provider: str) -> str:
    """What a log line may say about the sender of a refused message.

    Per adapter, so the value is the one the operator can correlate with the
    binding: the BSUID's fingerprint under Cloud, the JID's under Baileys.
    The JID is normalized first, so a device-suffixed and a bare mention of
    one sender produce the same string — otherwise the two log lines this
    exists to join would not.
    """
    if provider == db.WHATSAPP_BAILEYS_PROVIDER:
        return jid_fingerprint(normalize_jid(identity.jid))
    return bsuid_fingerprint(identity.bsuid)


__all__ = [
    "JID_USER_DOMAIN",
    "Resolution",
    "identity_fingerprint",
    "jid_number",
    "normalize_jid",
    "resolve_inbound_identity",
]
