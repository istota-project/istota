"""The fixed WhatsApp webhook: bounds, authentication, normalization, ingest.

Three phases, and the order between them is the module's whole design.

**Authenticate, then read.** Bounds and content type first, then the HMAC over
the exact raw bytes, and only then `json.loads`. A signature check that runs
after the parse has already fed a parser to an unauthenticated caller, and a
403 that arrives as a 400 has told them the body was read.

**Normalize the whole batch before the transaction opens.** Meta may put
several entries, changes, messages and statuses in one POST, and PyWa's own
parsers read `entry[0]["changes"][0]["value"]["messages"][0]` — the first of
each and nothing else. Walking all of them here is one half of why the
normalizer is local; the other is that a malformed element must not leave the
first message applied behind a 200 nobody will retry. Nothing is written until
every element has parsed.

**One transaction for the batch.** The dedup claim, the identity enrollment,
the confirmation answer and the task creation commit together or not at all,
which is why the binding CRUD takes a connection rather than a path: a second
connection opened under an open write transaction waits out the full busy
timeout and then raises (`.claude/rules/notifications.md`). A database failure
rolls back and answers 503, so Meta retries; a complete or wholly duplicate
batch answers an empty 200.

**Nothing here touches a room.** No registration, no binding, no membership,
no canonical `messages` row, no mirror. WhatsApp is its own external
conversation, and `ingest_message` reaches the non-room branch because the
surface owns no rooms and `mirror_to_room` is False. The conversation token
names no registered room, so `transcript_room` resolves nothing for it and
there is no special case to add anywhere in `ingest`.
"""

from __future__ import annotations

import hmac
import json
import logging
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from ... import commands, confirmations, db
from ...config import Config
from ...user_profiles import is_e164, short_fingerprint
from .._types import IncomingMessage
from ..ingest import ingest_message
from . import whatsapp_conversation_token
from ._types import (
    InboundWhatsAppEvent,
    WhatsAppDeliveryEvent,
    WhatsAppDeliveryStatus,
    WhatsAppEvent,
    WhatsAppUserIdentity,
)
from .client import SIGNATURE_HEADER, verify_signature

logger = logging.getLogger(__name__)

#: The spec's application limit. nginx keeps its own, set high enough above
#: this that an oversize body still reaches the application and gets istota's
#: own answer rather than nginx's 413 page.
MAX_WEBHOOK_BODY = 256 * 1024

#: Meta's challenge is a decimal integer. It is echoed back verbatim on an
#: unauthenticated-until-the-token-matches endpoint, so the shape is bounded
#: rather than reflected as given.
MAX_CHALLENGE_CHARS = 64

#: How far ahead of the receiving instant an event timestamp may sit and still
#: be taken as given. Meta's timestamp is authenticated but is not our clock,
#: and it is the sole input to the 24-hour service window — a value hours ahead
#: would authorise a free-form send long after the user stopped writing.
FUTURE_EVENT_MARGIN = timedelta(minutes=5)

UNSUPPORTED_REPLY = (
    "That WhatsApp message type is not supported yet. "
    "Please resend the request as text."
)
HELP_REPLY = (
    "This is an Istota assistant. Send a request as plain text and you will "
    "get one reply. Send STOP to stop messages, START to resume."
)
STOP_REPLY = "You will get no further WhatsApp messages. Send START to resume."
START_REPLY = "WhatsApp messages are on again."

_TEXT_TYPES = frozenset({"text"})
_CALLBACK_TYPES = frozenset({"interactive", "button"})

#: Meta's status vocabulary, mapped onto the ledger's. `played` is deliberately
#: absent: it is a voice-message event the ledger has no state for, and
#: inventing one would put a row into a state no transition rule covers.
_STATUS_MAP: dict[str, WhatsAppDeliveryStatus] = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
}


class WhatsAppWebhookError(ValueError):
    """A safe HTTP-facing rejection. The reason never carries payload data."""

    def __init__(self, reason: str, status_code: int = 403) -> None:
        super().__init__(reason)
        self.status_code = status_code


@dataclass(frozen=True)
class WhatsAppEventResult:
    """What one normalized event did, for the caller to act on after commit.

    Everything that reaches Meta happens **after** this record crosses the
    commit: a command dispatch, a fixed reply, an operator alert. Calling out
    from inside the transaction would hold a write lock across a network round
    trip, and a send that succeeded inside a transaction that then rolled back
    would be a message with no record of itself.
    """

    disposition: str
    user_id: str | None = None
    task_id: int | None = None
    response_text: str | None = None
    response_logical_key: str | None = None
    command_text: str | None = None
    pending_alert: object | None = None


# ---------------------------------------------------------------------------
# GET: Meta's subscription handshake
# ---------------------------------------------------------------------------


def verify_subscription(config: Config, params: Mapping[str, str]) -> str:
    """The challenge to echo back as plain text, or a raised rejection.

    `hub.verify_token` is a shared secret that Meta sends as a *query value*,
    which is why the deployment's nginx must not log the query string for this
    path. The comparison is constant time for the same reason any secret
    comparison is, and an empty configured token refuses outright rather than
    matching an empty presented one.
    """
    whatsapp = config.whatsapp
    if not whatsapp.verify_token:
        raise WhatsAppWebhookError("whatsapp verify token not configured", 503)
    if params.get("hub.mode") != "subscribe":
        raise WhatsAppWebhookError("unsupported hub mode", 400)
    presented = params.get("hub.verify_token") or ""
    if not hmac.compare_digest(presented, whatsapp.verify_token):
        raise WhatsAppWebhookError("verify token mismatch", 403)
    challenge = params.get("hub.challenge") or ""
    if not challenge.isdecimal() or len(challenge) > MAX_CHALLENGE_CHARS:
        raise WhatsAppWebhookError("invalid hub challenge", 400)
    return challenge


# ---------------------------------------------------------------------------
# POST: bounds, signature, normalization
# ---------------------------------------------------------------------------


def _header(headers: Mapping[str, str], name: str) -> str:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return ""


def parse_webhook(
    config: Config, raw_body: bytes, headers: Mapping[str, str]
) -> list[WhatsAppEvent]:
    """Authenticate one POST and return its normalized events.

    Raises :class:`WhatsAppWebhookError` and writes nothing. The order of the
    guards is the contract: size, content type, configured secret, signature,
    decode, normalize.
    """
    if len(raw_body) > MAX_WEBHOOK_BODY:
        raise WhatsAppWebhookError("webhook body too large", 413)
    content_type = _header(headers, "content-type").partition(";")[0].strip()
    if content_type.casefold() != "application/json":
        raise WhatsAppWebhookError("unsupported content type", 415)
    if not config.whatsapp.app_secret:
        # Refusing to serve, not refusing this request: with no secret the
        # validator would verify an HMAC under an empty key. See `client.py`.
        raise WhatsAppWebhookError("whatsapp app secret not configured", 503)
    if not verify_signature(
        config.whatsapp.app_secret, raw_body, _header(headers, SIGNATURE_HEADER),
    ):
        raise WhatsAppWebhookError("invalid signature")
    try:
        payload = json.loads(raw_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise WhatsAppWebhookError("invalid json body", 400) from None
    return normalize_payload(config, payload)


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise WhatsAppWebhookError(f"invalid {name}", 400)
    return value


def _sequence(value: object, name: str) -> Sequence[Mapping[str, object]]:
    if not isinstance(value, list):
        raise WhatsAppWebhookError(f"invalid {name}", 400)
    return [_mapping(item, name) for item in value]


def _required_text(values: Mapping[str, object], name: str, what: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value:
        raise WhatsAppWebhookError(f"missing {what}", 400)
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _event_time(values: Mapping[str, object]) -> datetime:
    raw = values.get("timestamp")
    try:
        seconds = int(str(raw))
    except (TypeError, ValueError):
        raise WhatsAppWebhookError("invalid event timestamp", 400) from None
    try:
        return datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise WhatsAppWebhookError("invalid event timestamp", 400) from None


def _identity(contact: Mapping[str, object]) -> WhatsAppUserIdentity:
    profile = contact.get("profile")
    profile = profile if isinstance(profile, Mapping) else {}
    return WhatsAppUserIdentity(
        bsuid=_optional_text(contact.get("user_id")) or "",
        wa_id=_optional_text(contact.get("wa_id")),
        username=_optional_text(profile.get("username")),
    )


_NO_IDENTITY = WhatsAppUserIdentity(bsuid="", wa_id=None, username=None)


def _identity_for(
    message: Mapping[str, object], contacts: Sequence[Mapping[str, object]]
) -> WhatsAppUserIdentity:
    """Pair a message with the contact that sent it.

    By the sender id, never by position. PyWa takes `contacts[0]` for every
    message in the value, which is fine while a value holds one of each and is
    a **principal takeover** when it does not: the identity is what decides
    which Istota user the message may act as, so attributing the second
    message to the first sender hands one person's tasks to another.

    A sender matching no contact yields the empty identity rather than a
    guess, and the caller drops it as an unknown sender. The one positional
    case left is a message with no `from` at all beside exactly one contact,
    which is unambiguous.
    """
    sender = _optional_text(message.get("from"))
    if sender is None:
        return _identity(contacts[0]) if len(contacts) == 1 else _NO_IDENTITY
    for contact in contacts:
        if sender in (
            contact.get("user_id"), contact.get("wa_id"),
            contact.get("parent_user_id"),
        ):
            return _identity(contact)
    return _NO_IDENTITY


def _message_shape(message: Mapping[str, object]) -> tuple[str, str | None, str | None]:
    """``(message_type, text, callback_data)`` for one message object.

    Every shape istota does not act on gets a distinguishing type rather than
    being lumped into one bucket, because the reply policy differs: an
    unsupported media message earns one fixed reply and an edit, a deletion or
    a reaction earns silence.
    """
    declared = _optional_text(message.get("type")) or "unknown"
    if "edit" in message:
        return "edited", None, None
    if "revoke" in message:
        return "revoked", None, None
    if declared in _TEXT_TYPES:
        body = message.get("text")
        body = body.get("body") if isinstance(body, Mapping) else None
        return "text", body if isinstance(body, str) else "", None
    if declared == "interactive":
        interactive = message.get("interactive")
        interactive = interactive if isinstance(interactive, Mapping) else {}
        reply = interactive.get("button_reply") or interactive.get("list_reply")
        reply = reply if isinstance(reply, Mapping) else {}
        return "interactive", None, _optional_text(reply.get("id"))
    if declared == "button":
        button = message.get("button")
        button = button if isinstance(button, Mapping) else {}
        return "button", None, _optional_text(button.get("payload"))
    return declared, None, None


def _inbound_event(
    message: Mapping[str, object],
    contacts: Sequence[Mapping[str, object]],
    *,
    waba_id: str,
    phone_number_id: str,
) -> InboundWhatsAppEvent:
    message_type, text, callback_data = _message_shape(message)
    context = message.get("context")
    context = context if isinstance(context, Mapping) else {}
    # A group id on the message is what makes it a group chat, and it is read
    # here rather than in the resolver so the caller can refuse it before any
    # identity lookup happens. It rides on the type, because the record models
    # a direct conversation and a group is not one.
    if "group_id" in message:
        message_type = "group"
        text = None
        callback_data = None
    return InboundWhatsAppEvent(
        message_id=_required_text(message, "id", "message id"),
        waba_id=waba_id,
        phone_number_id=phone_number_id,
        from_user=_identity_for(message, contacts),
        message_type=message_type,
        text=text,
        callback_data=callback_data,
        reply_to_message_id=_optional_text(context.get("id")),
        sent_at=_event_time(message),
    )


def _delivery_event(
    status: Mapping[str, object], *, waba_id: str, phone_number_id: str,
) -> WhatsAppDeliveryEvent | None:
    mapped = _STATUS_MAP.get(
        (_optional_text(status.get("status")) or "").strip().lower()
    )
    if mapped is None:
        return None
    pricing = status.get("pricing")
    pricing = pricing if isinstance(pricing, Mapping) else {}
    billable = pricing.get("billable")
    errors = status.get("errors")
    error_code = None
    if isinstance(errors, list):
        for item in errors:
            if isinstance(item, Mapping) and isinstance(item.get("code"), (int, str)):
                error_code = str(item["code"])
                break
    return WhatsAppDeliveryEvent(
        message_id=_required_text(status, "id", "status message id"),
        waba_id=waba_id,
        phone_number_id=phone_number_id,
        recipient_id=_optional_text(status.get("recipient_id")) or "",
        status=mapped,
        occurred_at=_event_time(status),
        error_code=error_code,
        # `billable` is read, never derived. PyWa's own `Pricing.from_dict`
        # infers it from `type == "regular"` when the field is absent; here an
        # absent field stays None, because a guess is what opens — or fails to
        # open — the billing circuit.
        billable=billable if isinstance(billable, bool) else None,
        pricing_model=_optional_text(pricing.get("pricing_model")),
        pricing_category=_optional_text(pricing.get("category")),
        pricing_type=_optional_text(pricing.get("type"))
        or _optional_text(pricing.get("pricing_type")),
    )


def normalize_payload(config: Config, payload: object) -> list[WhatsAppEvent]:
    """Walk an authenticated Meta payload into local records.

    Every entry, every change, every message and every status. A payload naming
    another WABA, object, messaging product or business phone number is a 403
    and yields nothing; a structurally broken one is a 400 and also yields
    nothing, so a partial batch can never be acknowledged.
    """
    whatsapp = config.whatsapp
    body = _mapping(payload, "webhook body")
    if body.get("object") != "whatsapp_business_account":
        raise WhatsAppWebhookError("unexpected webhook object")
    events: list[WhatsAppEvent] = []
    entries = body.get("entry")
    if not isinstance(entries, list) or not entries:
        raise WhatsAppWebhookError("invalid entry list", 400)
    for entry in _sequence(entries, "entry list"):
        waba_id = _required_text(entry, "id", "waba id")
        if waba_id != whatsapp.waba_id:
            raise WhatsAppWebhookError("unexpected whatsapp business account")
        for change in _sequence(entry.get("changes"), "change list"):
            field = _optional_text(change.get("field"))
            if field != "messages":
                # Acknowledged and logged by name only. The value carries
                # message bodies and identifiers whatever the field is.
                logger.info("whatsapp.inbound.ignored_field field=%s", field or "")
                continue
            value = _mapping(change.get("value"), "change value")
            if value.get("messaging_product") != "whatsapp":
                raise WhatsAppWebhookError("unexpected messaging product")
            metadata = _mapping(value.get("metadata"), "metadata")
            if metadata.get("phone_number_id") != whatsapp.phone_number_id:
                raise WhatsAppWebhookError("unexpected business phone number")
            contacts = _sequence(value.get("contacts", []), "contact list")
            for message in _sequence(value.get("messages", []), "message list"):
                events.append(_inbound_event(
                    message, contacts,
                    waba_id=waba_id, phone_number_id=whatsapp.phone_number_id,
                ))
            for status in _sequence(value.get("statuses", []), "status list"):
                delivery = _delivery_event(
                    status,
                    waba_id=waba_id, phone_number_id=whatsapp.phone_number_id,
                )
                if delivery is not None:
                    events.append(delivery)
    return events


# ---------------------------------------------------------------------------
# The inbound transaction
# ---------------------------------------------------------------------------


def _bsuid_fingerprint(value: str) -> str:
    return short_fingerprint("istota-whatsapp-bsuid-v1", value, length=12)


def _message_fingerprint(value: str) -> str:
    return short_fingerprint("istota-whatsapp-message-v1", value, length=12)


def _sql_datetime(moment: datetime) -> str:
    """The `datetime('now')` spelling every column on the binding holds."""
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _window_stamp(sent_at: datetime) -> str:
    """The service-window value an authenticated event may write.

    Clamped forward only. `touch_whatsapp_binding` refuses to move the stored
    value backwards in SQL, so between the two a replayed, delayed or
    forward-dated event can neither reopen a closed window nor extend an open
    one past the margin.
    """
    now = datetime.now(timezone.utc)
    if sent_at > now + FUTURE_EVENT_MARGIN:
        return _sql_datetime(now)
    return _sql_datetime(sent_at)


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


@dataclass(frozen=True)
class _Resolution:
    user_id: str | None
    disposition: str | None = None
    pending_alert: object | None = None


def _write_identity_alert(conn, user_id: str, bsuid: str) -> object | None:
    """One deduplicated operator alert about a binding that stopped matching.

    Off WhatsApp by construction: `task_alert` rows are pushed by the
    notification routes, and the caller strips `whatsapp` from them — a surface
    whose identity is in doubt must not be the surface that reports it.
    Deduplicated on the *pair*, so a line that keeps sending bumps one row
    rather than raising a push per message.
    """
    from ...notification_resolvers import task_alert

    fingerprint = _bsuid_fingerprint(bsuid)
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


def _resolve_user(conn, event: InboundWhatsAppEvent) -> _Resolution:
    """Which Istota user this authenticated message may act as.

    The spec's five rules, in order, and the order is what makes them safe.
    A BSUID is required first, so a phone number is never sufficient once an
    identity exists. A matching BSUID wins outright. Only then may a bootstrap
    number enroll, and only onto a row that carries no BSUID yet. A bootstrap
    number matching a row that carries a *different* BSUID is the recycled-line
    case and fails closed with an alert — latching there would hand the Istota
    principal to whoever holds the number now.
    """
    bsuid = event.from_user.bsuid
    if not bsuid:
        return _Resolution(None, "unknown_sender")
    bound = db.get_whatsapp_binding_by_bsuid(conn, bsuid)
    if bound is not None:
        return _Resolution(bound.user_id)

    number = _e164_from_wa_id(event.from_user.wa_id)
    candidate = db.get_whatsapp_binding_by_phone(conn, number) if number else None
    if candidate is None:
        return _Resolution(None, "unknown_sender")
    if candidate.bsuid:
        return _Resolution(
            None, "identity_mismatch",
            _write_identity_alert(conn, candidate.user_id, bsuid),
        )
    try:
        latched = db.latch_whatsapp_bsuid(
            conn, candidate.user_id,
            bsuid=bsuid,
            send_id=bsuid,
            username=event.from_user.username or "",
        )
    except sqlite3.IntegrityError:
        # Another user already holds this BSUID or send id. The partial unique
        # indexes are the arbiter and this side lost; fail closed rather than
        # taking an identity the database says is somebody else's.
        return _Resolution(None, "identity_conflict")
    if not latched:
        # The row gained a BSUID between the read above and this write.
        return _Resolution(None, "identity_conflict")
    return _Resolution(candidate.user_id)


def _claim(conn, event: InboundWhatsAppEvent, user_id: str, disposition: str) -> bool:
    try:
        conn.execute(
            "INSERT INTO processed_whatsapp "
            "(message_id, user_id, task_id, disposition, message_type, received_at) "
            "VALUES (?, ?, NULL, ?, ?, datetime('now'))",
            (event.message_id, user_id, disposition, event.message_type),
        )
        return True
    except sqlite3.IntegrityError:
        return False


def _set_disposition(conn, event, disposition: str, task_id: int | None = None) -> None:
    conn.execute(
        "UPDATE processed_whatsapp SET disposition = ?, task_id = ? "
        "WHERE message_id = ?",
        (disposition, task_id, event.message_id),
    )


def _own_parked_confirmation(conn, user_id: str, token: str):
    """The question parked in *this* WhatsApp conversation, or ``None``.

    `confirmations.resolve` falls through to Path C — the user's single open
    question on any surface — which is right for Talk and web and is not right
    here, for the reason the SMS webhook gives at the same place: a texted
    `YES` reaching Path C could approve the untrusted-email confirmation gate,
    and `apply_answer` with `trust_sender` then writes a permanent trust row
    for somebody else's correspondent. Path A needs a surface-native response
    id WhatsApp does not have, so this is Path B alone. `!confirm <id> yes|no`
    stays the explicit route to any other surface's question.
    """
    task = db.get_pending_confirmation(conn, token)
    if task is None or task.user_id != user_id:
        return None
    return task


def _parse_callback(callback_data: str) -> tuple[int, str] | None:
    """``confirm:<task_id>:yes|no``, or ``None``.

    The payload carries the confirmation id and the choice and nothing else —
    no authorization secret, deliberately. What authorizes the answer is the
    signed sender identity plus the ownership check below; a token in the
    callback data would be a bearer credential sitting in a WhatsApp message
    on the recipient's own device.
    """
    parts = callback_data.split(":")
    if len(parts) != 3 or parts[0] != "confirm":
        return None
    if not parts[1].isdecimal():
        return None
    choice = parts[2].strip().lower()
    if choice not in ("yes", "no"):
        return None
    return int(parts[1]), choice


def _handle_inbound(
    conn, config: Config, event: InboundWhatsAppEvent
) -> WhatsAppEventResult:
    if not config.whatsapp.enabled:
        return WhatsAppEventResult("unconfigured")
    if not event.message_id or len(event.message_id) > 255:
        return WhatsAppEventResult("invalid_message_id")
    if event.message_type == "group":
        # Before identity lookup, deliberately: a group message is out of scope
        # whoever sent it, and resolving a principal for one would put a
        # third party's text into that user's task history.
        return WhatsAppEventResult("group")

    resolution = _resolve_user(conn, event)
    if resolution.user_id is None:
        logger.info(
            "whatsapp.inbound.rejected reason=%s message=%s identity=%s",
            resolution.disposition,
            _message_fingerprint(event.message_id),
            _bsuid_fingerprint(event.from_user.bsuid),
        )
        return WhatsAppEventResult(
            resolution.disposition or "unknown_sender",
            pending_alert=resolution.pending_alert,
        )

    user_id = resolution.user_id
    if not _claim(conn, event, user_id, event.message_type):
        return WhatsAppEventResult("duplicate", user_id=user_id)

    binding = db.get_whatsapp_binding(conn, user_id)
    db.touch_whatsapp_binding(
        conn, user_id,
        send_id=event.from_user.bsuid or None,
        username=event.from_user.username,
        last_seen_at=_sql_datetime(datetime.now(timezone.utc)),
        last_user_message_at=_window_stamp(event.sent_at),
    )

    result = _dispatch_inbound(conn, config, event, user_id, binding)
    _set_disposition(conn, event, result.disposition, result.task_id)
    logger.info(
        "whatsapp.inbound.accepted disposition=%s message=%s task_id=%s",
        result.disposition, _message_fingerprint(event.message_id), result.task_id,
    )
    return result


def _dispatch_inbound(
    conn, config: Config, event: InboundWhatsAppEvent, user_id: str, binding,
) -> WhatsAppEventResult:
    token = whatsapp_conversation_token(user_id)
    opted_out = binding is not None and binding.opted_out_at is not None

    if event.message_type in ("reaction", "edited", "revoked"):
        # Acknowledged and recorded, answered by nothing. Each is a change to
        # a message that already had its turn.
        return WhatsAppEventResult(event.message_type, user_id=user_id)

    if event.callback_data is not None:
        return _handle_callback(conn, config, event, user_id, token)

    if event.message_type not in _TEXT_TYPES:
        if opted_out:
            return WhatsAppEventResult("opted_out", user_id=user_id)
        return WhatsAppEventResult(
            "unsupported_type", user_id=user_id,
            response_text=UNSUPPORTED_REPLY,
            response_logical_key=f"unsupported:{event.message_id}",
        )

    text = (event.text or "").strip()
    keyword = text.upper()
    if keyword == "STOP":
        db.set_whatsapp_opt_out(conn, user_id, True)
        return WhatsAppEventResult(
            "stop", user_id=user_id, response_text=STOP_REPLY,
            response_logical_key=f"opt-out:{event.message_id}",
        )
    if keyword == "START":
        db.set_whatsapp_opt_out(conn, user_id, False)
        return WhatsAppEventResult(
            "start", user_id=user_id, response_text=START_REPLY,
            response_logical_key=f"opt-in:{event.message_id}",
        )
    if keyword == "HELP":
        return WhatsAppEventResult(
            "help", user_id=user_id, response_text=HELP_REPLY,
            response_logical_key=f"help:{event.message_id}",
        )

    # Above the opt-out gate, and on the same rule the callback branch is:
    # an answer to a question istota asked is a state change the user
    # deliberately made, so it is applied whatever their delivery preference —
    # only the acknowledgement is a send, and the ledger refuses that. Below
    # the gate, a bare `YES` would be refused while the Yes *button* carrying
    # the same answer was accepted, which is one rule with two answers.
    answer = confirmations.parse_answer(text) if text else None
    if answer is not None:
        parked = _own_parked_confirmation(conn, user_id, token)
        if parked is not None:
            response = confirmations.apply_answer(
                conn, parked, answer, config, by="whatsapp",
            )
            return WhatsAppEventResult(
                "confirmation_answer", user_id=user_id, response_text=response,
                response_logical_key=f"confirmation-answer:{event.message_id}",
            )

    if opted_out:
        # Recorded but not acted on. STOP means no outbound WhatsApp, and a
        # task whose only reply route is blocked is an answer nobody reads —
        # as is a command whose whole value is its response.
        return WhatsAppEventResult("opted_out", user_id=user_id)

    if not text:
        return WhatsAppEventResult("empty", user_id=user_id)

    if text.startswith("!"):
        return WhatsAppEventResult(
            "command", user_id=user_id, command_text=text,
            response_logical_key=f"command:{event.message_id}",
        )

    confirmations.cancel_for_conversation(conn, token, user_id, by="whatsapp")
    task_id = ingest_message(
        conn, config,
        IncomingMessage(
            user_id=user_id, text=text, source_type="whatsapp",
            surface="whatsapp", channel_token=token, output_target="whatsapp",
            mirror_to_room=False, queue="foreground",
        ),
    )
    return WhatsAppEventResult("task", user_id=user_id, task_id=task_id)


def _handle_callback(
    conn, config: Config, event: InboundWhatsAppEvent, user_id: str, token: str,
) -> WhatsAppEventResult:
    parsed = _parse_callback(event.callback_data or "")
    if parsed is None:
        return WhatsAppEventResult("callback_unmatched", user_id=user_id)
    task_id, choice = parsed
    task = db.get_task(conn, task_id)
    # Three checks, and none of them is redundant. The signed sender says who
    # is answering; the callback data says which question. Ownership is what
    # ties the two together, and the conversation token is what stops a
    # WhatsApp button answering a question parked on another surface — the
    # Path C hazard `_own_parked_confirmation` exists for, arriving by a
    # different door.
    if (
        task is None
        or task.user_id != user_id
        or task.conversation_token != token
        or task.status != "pending_confirmation"
    ):
        return WhatsAppEventResult("callback_unmatched", user_id=user_id)
    answer = confirmations.parse_answer(choice)
    if answer is None:  # pragma: no cover - `_parse_callback` bounds the choice
        return WhatsAppEventResult("callback_unmatched", user_id=user_id)
    response = confirmations.apply_answer(conn, task, answer, config, by="whatsapp")
    return WhatsAppEventResult(
        "confirmation_answer", user_id=user_id, response_text=response,
        response_logical_key=f"confirmation-answer:{event.message_id}",
    )


def _handle_delivery(
    conn, config: Config, event: WhatsAppDeliveryEvent
) -> WhatsAppEventResult:
    """Stage 3's seam. Normalized here, applied to the ledger there.

    Deliberately inert rather than absent: the normalizer already walks
    statuses, so a batch mixing messages and statuses is exercised end to end
    now, and stage 3 fills in the monotonic transition and the pricing
    observation without moving the transaction boundary they have to commit
    inside.
    """
    logger.debug(
        "whatsapp.delivery.observed status=%s message=%s",
        event.status, _message_fingerprint(event.message_id),
    )
    return WhatsAppEventResult("delivery")


def handle_whatsapp_batch(
    conn, config: Config, events: Sequence[WhatsAppEvent]
) -> list[WhatsAppEventResult]:
    """Apply every event of one authenticated POST in one transaction.

    `BEGIN IMMEDIATE` up front, so the write lock is taken before the first
    read the later writes depend on rather than being upgraded halfway through
    — two concurrent batches for one user would otherwise race between the
    binding read and the latch. Any exception propagates to the caller, whose
    context manager rolls back and answers 503; nothing here swallows a
    database error into a 200.
    """
    if not events:
        return []
    conn.execute("BEGIN IMMEDIATE")
    results: list[WhatsAppEventResult] = []
    for event in events:
        if isinstance(event, WhatsAppDeliveryEvent):
            results.append(_handle_delivery(conn, config, event))
        else:
            results.append(_handle_inbound(conn, config, event))
    return results


# ---------------------------------------------------------------------------
# After the commit
# ---------------------------------------------------------------------------


async def resolve_event_response(
    config: Config, result: WhatsAppEventResult
) -> str | None:
    """The text one committed result owes the sender, if any.

    Runs after the commit, never inside it: `commands.dispatch` opens its own
    connection and several handlers write. Returning the text rather than
    sending it is the stage boundary — the ledger claim, the service-window
    choice and the Cloud API call are stage 3's, and they need a committed
    result to key their logical id off.
    """
    if result.command_text:
        if result.user_id is None:
            return None
        # No registry argument: `dispatch` builds one on demand and
        # `make_registry` does no I/O. An empty one is strictly worse than
        # none — `!route` and its neighbours read it and would report that the
        # deployment has no surfaces at all.
        command = await commands.dispatch(
            config, result.user_id, whatsapp_conversation_token(result.user_id),
            result.command_text, surface="whatsapp",
        )
        return command.text or None
    return result.response_text


def deliver_pending_alerts(config: Config, results: Sequence[WhatsAppEventResult]) -> None:
    """Push the alerts the transaction buffered, with WhatsApp taken out.

    Buffered rather than delivered inline for the reason
    `.claude/rules/notifications.md` gives: the producer holds a write
    transaction, and `deliver_pending` opening a second connection under it
    waits out the full busy timeout and then raises into a never-raises
    contract. The WhatsApp leg is stripped because every alert raised here is
    about WhatsApp being unable to identify or reach somebody — reporting it
    over the failing surface is the loop this rule exists to break.
    """
    from ... import notifications
    from ...notification_store import mark_delivered

    for result in results:
        raised = result.pending_alert
        if raised is None or not raised.deliver:
            continue
        # The row's own user, never the event's: an identity mismatch resolves
        # to no principal at all, and the person who has to act on it is the
        # one the bootstrap number is bound to.
        user_id = raised.user_id
        if not user_id:
            continue
        dests = [
            dest for dest in notifications.resolve_destinations(config, user_id, "alert")
            if dest.surface != "whatsapp"
        ]
        descriptor = ",".join(
            dest.surface if dest.channel is None else f"{dest.surface}:{dest.channel}"
            for dest in dests
        )
        if not descriptor:
            continue
        try:
            delivered = notifications.send_notification(
                config, user_id, raised.text, surface=descriptor,
                title=raised.title,
                reference_id=f"whatsapp-alert:{raised.notification_id}",
            )
        except Exception:
            logger.warning("whatsapp.alert.not_delivered", exc_info=True)
            continue
        if delivered:
            with db.get_db(config.db_path) as conn:
                mark_delivered(conn, [raised.notification_id])


__all__ = [
    "MAX_WEBHOOK_BODY",
    "WhatsAppEventResult",
    "WhatsAppWebhookError",
    "deliver_pending_alerts",
    "handle_whatsapp_batch",
    "normalize_payload",
    "parse_webhook",
    "resolve_event_response",
    "verify_subscription",
]
