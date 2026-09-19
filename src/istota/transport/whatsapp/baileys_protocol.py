"""The wire format between the daemon and the Baileys sidecar.

One JSON object per line, terminated by ``\\n``, over a Unix socket. The
daemon listens and the sidecar dials, which is `devbox_proxy`'s arrangement
and is what lets the spec's "a local Unix socket the daemon owns, 0600" be
literally true — the daemon creates the inode and sets its mode, so the
sidecar's own umask cannot widen it.

`devbox_exec_protocol.py`'s shape, one property short of it. That one frames
binary in both directions because it carries file bodies and a command's
stdout; every message here is a small JSON object, so a line is enough and a
line is what a Node `readline` on the other side already produces. What is
kept is the discipline: the cap is enforced on **both** ends, a decoder raises
rather than guessing, and no field is inferred from another.

**It imports `._types` and `.media`, and nothing else from the package.** The
first is the same boundary `session/session_log.py` draws around
`istota.llm.types`: that module is plain data importing nothing itself, so
naming it costs no import graph and buys the one thing a bare framing module
could not have — the normalizers. Those are the whole test surface of a wire
format, and putting them in `baileys_bridge.py` would mix them with an accept
loop and a subprocess supervisor. The Cloud half is arranged the same way:
`webhook.py` holds `parse_webhook` and `normalize_payload` together.

The second used to be forbidden by this paragraph, which said `._types` and
nothing else. `media.is_staged_name` is the rule for whether a value off the
wire may be joined under the staging root, and this is the module that reads
that value — so the alternative was a second copy of a containment test, which
is the duplication class the tree spends most of its guards on. `media.py`'s
own docstring carries the measurement that the boundary was never real in
either direction: this module already pulls `db`, `storage` and `config`
through the package `__init__` on its own.

**Nothing here logs a value off the wire**, which is narrower than the "nothing
here logs" this file used to claim and is what that claim was protecting: a
`qr` payload is a pairing credential for the whole WhatsApp account, and an
`inbound` message carries the sender's number and their words. A media field
this side refuses is named by *field*, never by value — the spec's own wording,
"naming the failure and not the value" — because a value it refused is exactly
the one most worth not writing down.

**The other side is TypeScript, so there is no vendored Python copy of this
file and no byte pin for one.** `docker/devbox/lib/istota_forge_cli.py` and its
neighbour are byte copies because both ends are Python; the Node sidecar is a
reimplementation, which `.claude/rules/devbox.md` already has a precedent for
in `istota_devbox_client.py` — pinned behaviourally rather than by bytes.
Stage 6 owns that pin.

"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from . import media as media_rules
from ._types import (
    InboundWhatsAppEvent,
    WhatsAppDeliveryEvent,
    WhatsAppDeliveryStatus,
    WhatsAppInboundMedia,
    WhatsAppSendFailure,
    WhatsAppSendOutcome,
    WhatsAppSendRequest,
    WhatsAppSendResult,
    WhatsAppUserIdentity,
)

logger = logging.getLogger("istota.transport.whatsapp.baileys_protocol")

#: Bumped whenever a field changes meaning. The sidecar states its version in
#: the `hello` frame and the bridge refuses a version it does not know, rather
#: than reading an older shape's fields out of a newer frame.
PROTOCOL_VERSION = 1

# ---- Message types ---------------------------------------------------------

MSG_HELLO = "hello"
MSG_READY = "ready"
MSG_QR = "qr"
MSG_INBOUND = "inbound"
MSG_RECEIPT = "receipt"
MSG_SEND_RESULT = "send_result"
MSG_FATAL = "fatal"

MSG_SEND = "send"
MSG_SHUTDOWN = "shutdown"

#: Sidecar to daemon.
UP_MESSAGES: frozenset[str] = frozenset({
    MSG_HELLO, MSG_READY, MSG_QR, MSG_INBOUND, MSG_RECEIPT,
    MSG_SEND_RESULT, MSG_FATAL,
})

#: Daemon to sidecar.
DOWN_MESSAGES: frozenset[str] = frozenset({MSG_SEND, MSG_SHUTDOWN})

#: One line's ceiling, enforced by `encode` and by `decode` both. A cap only on
#: the reader lets a writer build a line it can never deliver; a cap only on the
#: writer lets a garbled peer ask for an unbounded buffer. The number is
#: `webhook.MAX_WEBHOOK_BODY`'s, because the largest thing that crosses here is
#: one WhatsApp message body and that surface already sized it.
MAX_LINE_BYTES = 256 * 1024

#: A provider message id goes onto a uniquely-indexed ledger column, so its
#: length is bounded at the wire rather than at the column. `webhook._handle_
#: inbound` applies the same 255 to Meta's.
MAX_MESSAGE_ID_CHARS = 255

#: WhatsApp's own protocol ceiling for a text message. Bounded here because
#: **this is the first surface where it is not bounded upstream**: Meta refuses
#: a Cloud message past 4,096 characters before it ever reaches
#: `webhook.normalize_payload`, while a Baileys `inbound` line is bounded only
#: by `MAX_LINE_BYTES` — so a quarter-megabyte body could reach a task prompt.
#: Past this the line is refused rather than truncated: a message longer than
#: WhatsApp itself permits did not come from WhatsApp, and silently cutting a
#: person's words is worse than refusing a frame that cannot be genuine.
MAX_INBOUND_TEXT_CHARS = 65536

#: A display name, bounded for the same reason one field over. It reaches
#: `db.touch_whatsapp_binding`'s `username` column and no further.
MAX_USERNAME_CHARS = 256

#: A declared media type, bounded because it is a string off the wire that
#: reaches a log line. It is **advisory** and nothing branches on it:
#: `media.stage_to_attachment` sniffs the bytes and names the inbox copy from
#: its own answer, because the sender chose what they uploaded. 128 is well
#: past any real `type/subtype; parameters` and short enough to log whole —
#: which is also why the value is held to printable characters beside the
#: length: a log line is where it goes, and a newline or an ANSI escape off
#: the wire forges one there.
MAX_MEDIA_MIME_CHARS = 128

#: Every `media_error` the sidecar may name, and the local sentence each
#: becomes. `_SEND_REASONS`' rule, for its reason: the sidecar's own words
#: carry the destination JID and, on a Boom error, the whole request, so the
#: reason crosses as a key and the prose is written here.
_MEDIA_ERRORS: dict[str, str] = {
    "download_failed": "the image could not be downloaded from WhatsApp",
    "over_the_cap": "the image was larger than this surface accepts",
    "write_failed": "the image could not be written to disk",
}
_UNKNOWN_MEDIA_ERROR = "the image could not be fetched"

#: Baileys' receipt vocabulary mapped onto the ledger's. A status this surface
#: does not model yields `None` and the caller drops the receipt — inventing a
#: state would put a row where no transition rule covers it, which is the rule
#: `webhook._STATUS_MAP` states for Meta's `played`.
_STATUS_MAP: dict[str, WhatsAppDeliveryStatus] = {
    "sent": "sent",
    "delivered": "delivered",
    "read": "read",
    "failed": "failed",
    "error": "failed",
}

#: Failure reasons a `send_result` may name, and the local text each becomes.
#: **The sidecar's own words never reach a `safe_reason`.** Baileys' errors
#: carry the destination JID and, on a Boom error, the whole request — the same
#: hazard `.claude/rules/whatsapp.md` records for Meta's prose, answered the
#: same way: a fixed table, and a numeric-or-slug error code alongside.
_SEND_REASONS: dict[str, str] = {
    "not_connected": "the WhatsApp session is not connected",
    "logged_out": "the WhatsApp session was logged out",
    "not_on_whatsapp": "the destination is not a WhatsApp user",
    "rejected": "WhatsApp rejected the message",
    "timeout": "the send timed out",
    "internal": "the sidecar could not send the message",
}
_UNKNOWN_SEND_REASON = "the sidecar could not send the message"

#: What `safe_reason` says for a failure this module decided rather than the
#: sidecar. Public because `baileys_bridge` settles its own pre-write refusals
#: with them and a second spelling would drift.
REASON_NO_SIDECAR = "no WhatsApp sidecar is connected"
REASON_SESSION_FATAL = "the WhatsApp session needs re-pairing"
REASON_SEND_TIMEOUT = "the sidecar did not answer the send"
REASON_LINK_LOST = "the sidecar connection dropped during the send"
REASON_ENCODE_FAILED = "the send could not be encoded"
#: What a send inside an open pairing window is refused with. Its own string
#: rather than `REASON_SESSION_FATAL`, because the two are different states an
#: operator reads differently: that one means the session needs re-pairing,
#: this one means somebody is re-pairing it right now. Both are `definite`,
#: which is the point — `repair_session` clears the permanent-fatal latch so
#: the supervisor can resume, and without a refusal of its own every send in
#: the window would be written to an unpaired sidecar and settle `unknown`.
REASON_PAIRING = "the WhatsApp session is being re-paired"
#: The catch-all for a send that failed before its line entered the socket and
#: for no reason above. It is its own string rather than `REASON_LINK_LOST`
#: because the two settle the ledger differently — this one is `definite`, so
#: an operator reading the row needs to be able to tell them apart.
REASON_NOT_WRITTEN = "the send failed before it reached the sidecar"


class BaileysProtocolError(ValueError):
    """A line this side refuses to act on.

    Raised by `decode` and by every normalizer. The bridge answers it by
    dropping the line and counting it, never by guessing at the missing half:
    a frame the daemon cannot read is a sidecar the daemon cannot trust about
    that message, and a message attributed to a guessed sender is the identity
    failure `webhook._identity_for` exists to prevent, one transport over.
    """


# ---- Framing ---------------------------------------------------------------


def encode(message_type: str, /, **fields: Any) -> bytes:
    """One newline-terminated line, or a raised `BaileysProtocolError`.

    `separators` without spaces and `ensure_ascii` left on: the line is a
    single `\\n`-framed unit, and `json.dumps` escapes an embedded newline in
    a string value, so nothing a caller passes can forge a frame boundary.

    The envelope's type is **positional-only**: an `inbound` line carries a
    `message_type` field of its own, and a keyword parameter of that name
    would collide with it at every call site that spreads a payload.
    """
    payload = {"type": message_type, **fields}
    try:
        line = json.dumps(payload, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise BaileysProtocolError(f"unserializable {message_type} message") from exc
    raw = line.encode("utf-8") + b"\n"
    if len(raw) > MAX_LINE_BYTES:
        raise BaileysProtocolError(
            f"{message_type} message exceeds {MAX_LINE_BYTES} bytes"
        )
    return raw


def decode(line: bytes | str) -> dict[str, Any]:
    """Parse one line into its envelope, or raise.

    The envelope alone — a JSON object carrying a non-empty string `type`.
    What each type's fields mean is the normalizers' question, so a caller can
    switch on the type before deciding whether it cares.
    """
    raw = line.encode("utf-8") if isinstance(line, str) else bytes(line)
    if len(raw) > MAX_LINE_BYTES:
        raise BaileysProtocolError(f"line exceeds {MAX_LINE_BYTES} bytes")
    stripped = raw.strip()
    if not stripped:
        raise BaileysProtocolError("empty line")
    try:
        parsed = json.loads(stripped)
    except (ValueError, RecursionError):
        # `RecursionError` is not hypothetical and is not a `ValueError`: a
        # deeply nested body blows the interpreter stack inside the decoder.
        # `webhook.parse_webhook` catches the same pair for the same reason.
        raise BaileysProtocolError("invalid json line") from None
    if not isinstance(parsed, dict):
        raise BaileysProtocolError("line must be a JSON object")
    message_type = parsed.get("type")
    if not isinstance(message_type, str) or not message_type:
        raise BaileysProtocolError("line has no message type")
    return parsed


# ---- Field readers ---------------------------------------------------------


def _text(values: dict[str, Any], name: str) -> str:
    value = values.get(name)
    if not isinstance(value, str) or not value:
        raise BaileysProtocolError(f"missing {name}")
    return value


def _optional_text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _bounded_text(value: object, name: str, limit: int) -> str | None:
    """An optional string, refused rather than truncated past `limit`."""
    text = _optional_text(value)
    if text is not None and len(text) > limit:
        raise BaileysProtocolError(f"{name} exceeds {limit} characters")
    return text


def _error_code(value: object) -> str | None:
    """A provider error code as text, or `None`.

    `bool` is excluded explicitly because it is a subclass of `int`, so the
    obvious `isinstance(value, (int, str))` renders `true` as the string
    `"True"` into `sent_whatsapp.error_code` and every operator surface reading
    it. `_event_time` and `hello_version` guard the same way for the same
    reason.
    """
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        return None
    return str(value) or None


def _event_time(values: dict[str, Any]) -> datetime:
    """`timestamp`, epoch seconds, as an aware UTC datetime.

    Required rather than defaulted to now: the value reaches
    `webhook._window_stamp`, and a receiver inventing a timestamp would be
    asserting when somebody wrote. That clamp is what bounds a wrong one; a
    missing one has nothing to clamp.
    """
    raw = values.get("timestamp")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise BaileysProtocolError("invalid event timestamp")
    try:
        return datetime.fromtimestamp(int(raw), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise BaileysProtocolError("invalid event timestamp") from None


def _dropped_media(field: str) -> WhatsAppInboundMedia:
    """Say a media field was refused, by field name and never by value.

    The value is a string a sidecar chose and this side just decided it could
    not read, which makes it the one most worth keeping out of the log.

    **It answers a failed record rather than `None`, and that is what keeps
    the log line's own claim true.** `None` means "this message carried no
    file", which `_dispatch_inbound`'s narrowed gate reads together with the
    message type: an `image` with no record is a type the surface cannot read
    with nothing attached, so it takes the unsupported reply and the caption
    is never looked at. Dropping the record would therefore cost the whole
    message — `STOP` typed on a photograph included — where this module's
    rule, stated one function down, is that a malformed field costs the media
    and never the message.

    The staged file is **not** unlinked here, and cannot be: this module
    refuses to turn a value off the wire into a path at all, which is the
    whole of `staged_path`'s containment story, and on the arms below the name
    is exactly what could not be read. The sweep takes it.
    """
    logger.warning(
        "whatsapp.baileys.media_dropped field=%s: the message is kept and its "
        "image is not",
        field,
    )
    return WhatsAppInboundMedia(
        staged_path="", mime_type="", byte_count=0,
        attached_for_user="", error=_UNKNOWN_MEDIA_ERROR,
    )


def _inbound_media(payload: dict[str, Any]) -> WhatsAppInboundMedia | None:
    """The four media fields as a record, or `None` for a message without one.

    **A malformed field costs the media and never the message.** That is
    `webhook.handle_whatsapp_batch`'s own rule — a media failure must cost the
    media, never the batch — restated at the decoder, and it is why nothing
    here raises `BaileysProtocolError`: a caption somebody typed must not be
    thrown away over a field no authoritative reader consults.

    **`staged_path` is the bare name the sidecar minted, not a path**, and
    that is a boundary rather than a convenience. A frame is an untrusted
    string, so this module refuses to turn one into a path at all: it
    validates the value as one ordinary component with `media.is_staged_name`
    and hands the component on. `baileys_runtime` joins it under the staging
    directory it resolved from config, which is the only place a path is
    built — so no value off the wire can name a file outside it however this
    decoder is later edited. The Cloud path has no such problem and fills an
    absolute path, because there the daemon opened the file itself.

    **`attached_for_user` is `""` here** on `NO_CLOUD_ACCOUNT`'s convention:
    the unlocked pre-check has not run at decode time and the field has no
    honest value yet. `baileys_runtime` fills it from `media.precheck`, and
    the transaction compares it against the authoritative resolution.

    `mime_type` and `byte_count` are the sidecar's declared values and are
    both advisory: `stage_to_attachment` sniffs the bytes for the type and
    re-measures the file for the size. They are carried for the log line, so
    a disagreement is a debug matter rather than a decision.

    An `error` outranks a name. The two never arrive together from our own
    sidecar, and reading the name first would put a path on a record whose
    contract says `staged_path` is `""` when `error` is set.
    """
    error_raw = payload.get("media_error")
    if error_raw is not None:
        if not isinstance(error_raw, str) or not error_raw:
            return _dropped_media("media_error")
        return WhatsAppInboundMedia(
            staged_path="",
            mime_type="",
            byte_count=0,
            attached_for_user="",
            error=_MEDIA_ERRORS.get(error_raw.strip().lower(),
                                    _UNKNOWN_MEDIA_ERROR),
        )

    name = payload.get("media_name")
    if name is None:
        return None
    if not media_rules.is_staged_name(name):
        return _dropped_media("media_name")

    mime_raw = payload.get("media_mime")
    if mime_raw is None:
        mime = ""
    elif (
        isinstance(mime_raw, str)
        and len(mime_raw) <= MAX_MEDIA_MIME_CHARS
        and mime_raw.isprintable()
    ):
        mime = mime_raw
    else:
        return _dropped_media("media_mime")

    # Absent reads as nought, matching the mime arm above: both are advisory,
    # so losing an image over a missing log label would be the strictness
    # landing on the wrong field. A value of the wrong *type* or a negative
    # one still drops, because that is a sidecar this side cannot read.
    byte_count = payload.get("media_bytes", 0)
    if byte_count is None:
        byte_count = 0
    if isinstance(byte_count, bool) or not isinstance(byte_count, int):
        return _dropped_media("media_bytes")
    if byte_count < 0:
        return _dropped_media("media_bytes")

    return WhatsAppInboundMedia(
        staged_path=name,
        mime_type=mime,
        byte_count=byte_count,
        attached_for_user="",
        error=None,
    )


def _message_id(values: dict[str, Any], name: str = "message_id") -> str:
    value = _text(values, name)
    if len(value) > MAX_MESSAGE_ID_CHARS:
        raise BaileysProtocolError(f"{name} exceeds {MAX_MESSAGE_ID_CHARS} characters")
    return value


# ---- Normalizers -----------------------------------------------------------

#: What a Baileys event fills Meta's three account identifiers with.
#:
#: `InboundWhatsAppEvent.waba_id` / `.phone_number_id` and
#: `WhatsAppDeliveryEvent.recipient_id` are Cloud-shaped and a Baileys event
#: has no honest value for any of them — there is no WhatsApp Business Account
#: and no phone-number id, and the recipient is the JID the ledger deliberately
#: does not store. Nothing downstream reads any of the three: they are checked
#: inside `webhook.normalize_payload` against the configured Cloud account
#: *before* the record is built, and no reader exists past that point.
#:
#: So the empty string, and the fields stay **required** on the dataclasses
#: rather than gaining a default. A default would let a future Cloud path omit
#: one silently, and growing a per-adapter record for three fields with no
#: reader is the widening Stage 2 declined to make to `WhatsAppProviderCaps`
#: for the same reason: a seam answered by two adapters and then widened is a
#: seam whose existing answers were never considered.
NO_CLOUD_ACCOUNT = ""


def inbound_event(payload: dict[str, Any]) -> InboundWhatsAppEvent:
    """One `inbound` line as the record the common code consumes.

    The identity is the JID and only the JID. `bsuid` is set to `""` rather
    than to anything derived, because `identity._resolve_baileys` reads `jid`
    and a populated `bsuid` on a Baileys event could only ever be a value some
    later reader mistakes for a Cloud identity — the cross-adapter takeover
    `WhatsAppUserIdentity`'s own docstring is about. `wa_id` stays `None` for
    the same reason: the subscriber number is inside the JID, and
    `identity.jid_number` is what takes it out.

    A group message is typed `group` here, matching `webhook._inbound_event`,
    so `_handle_inbound` refuses it **before** any identity lookup. The flag is
    read off the line rather than off the JID's domain: the sidecar knows which
    chat a message arrived in, and inferring it from a string is how a
    `@g.us` spelling change becomes a third party's text in somebody's task
    history. **The media goes with the text on that branch**, because nothing
    will ever consume a file for a message refused above every identity
    lookup; the staged file orphans and the sweep takes it.

    An image's caption rides `text` rather than a field of its own, so that
    every gate in `_dispatch_inbound` can apply to it with no new code —
    `STOP` typed as a caption opting out, a caption starting with `!` being a
    command, a caption that parses as a confirmation answer answering one.
    **None of that is live in this tree yet**: `_dispatch_inbound` returns for
    any `message_type` outside `_TEXT_TYPES` well above the STOP/START/HELP
    block, so today an image's caption reaches none of them. Narrowing that
    gate is the stage that makes the sentence true; carrying the caption on
    `text` is what makes it cost no new code when it does. `_inbound_media`
    owns the four media fields.
    """
    message_type = _optional_text(payload.get("message_type")) or "unknown"
    text = _bounded_text(payload.get("text"), "text", MAX_INBOUND_TEXT_CHARS)
    callback_data = _bounded_text(
        payload.get("callback_data"), "callback_data", MAX_MESSAGE_ID_CHARS,
    )
    inbound_media = _inbound_media(payload)
    if payload.get("group") is True:
        message_type = "group"
        text = None
        callback_data = None
        inbound_media = None
    return InboundWhatsAppEvent(
        message_id=_message_id(payload),
        waba_id=NO_CLOUD_ACCOUNT,
        phone_number_id=NO_CLOUD_ACCOUNT,
        from_user=WhatsAppUserIdentity(
            bsuid="",
            wa_id=None,
            username=_bounded_text(
                payload.get("username"), "username", MAX_USERNAME_CHARS,
            ),
            jid=_text(payload, "jid"),
        ),
        message_type=message_type,
        text=text,
        callback_data=callback_data,
        reply_to_message_id=_optional_text(payload.get("reply_to_message_id")),
        sent_at=_event_time(payload),
        media=inbound_media,
    )


def delivery_event(payload: dict[str, Any]) -> WhatsAppDeliveryEvent | None:
    """One `receipt` line as a delivery event, or `None` for one to drop.

    Every pricing field is `None`, and that is a statement rather than a
    placeholder: `billable = None` means the provider said nothing, which is
    exactly true here — Baileys has no pricing concept at all. Writing `False`
    would assert a pricing fact nobody observed, and the free-guard circuit
    arms on `True` alone, so `None` leaves it closed without claiming anything.
    """
    mapped = _STATUS_MAP.get(
        (_optional_text(payload.get("status")) or "").strip().lower()
    )
    if mapped is None:
        return None
    return WhatsAppDeliveryEvent(
        message_id=_message_id(payload),
        waba_id=NO_CLOUD_ACCOUNT,
        phone_number_id=NO_CLOUD_ACCOUNT,
        recipient_id=NO_CLOUD_ACCOUNT,
        status=mapped,
        occurred_at=_event_time(payload),
        error_code=_error_code(payload.get("error_code")),
        billable=None,
        pricing_model=None,
        pricing_category=None,
        pricing_type=None,
    )


def send_outcome(payload: dict[str, Any]) -> WhatsAppSendOutcome:
    """One `send_result` line as the outcome the ledger settles on.

    **`definite` is read, never derived**, and the default is the ambiguous
    one. `definite` says the message provably never left, which only the side
    that tried to send it can know; absent, malformed, or any value that is not
    `True` means the sidecar did not say so, and the ledger then settles
    `unknown` rather than `failed`. Erring towards ambiguous costs an operator
    a row they have to read; erring the other way reports a message as never
    sent when it may be on somebody's phone.
    """
    if payload.get("ok") is True:
        return WhatsAppSendResult(message_id=_message_id(payload))
    reason_key = (_optional_text(payload.get("reason")) or "").strip().lower()
    return WhatsAppSendFailure(
        definite=payload.get("definite") is True,
        error_code=_error_code(payload.get("error_code")),
        safe_reason=_SEND_REASONS.get(reason_key, _UNKNOWN_SEND_REASON),
    )


def local_failure(reason: str, *, definite: bool) -> WhatsAppSendFailure:
    """The outcome for a send this side refused or lost track of.

    `error_code` is `None` because no provider produced one. The caller decides
    `definite`: `baileys_bridge` passes `True` only where the send line
    provably never entered the socket.
    """
    return WhatsAppSendFailure(definite=definite, error_code=None, safe_reason=reason)


def send_payload(request_id: str, request: WhatsAppSendRequest) -> dict[str, Any]:
    """A `WhatsAppSendRequest` flattened for the wire.

    `kind` crosses even though a Baileys deployment can only ever send
    `service`: the field is the ledger's own record of which rendering it
    claimed, and a sidecar that received `template` should refuse it loudly
    rather than silently sending a service message the ledger will report as a
    template. The template name and language are deliberately *not* sent —
    they are Meta account state with no meaning here.
    """
    return {
        "request_id": request_id,
        "to": request.to,
        "text": request.text,
        "kind": request.kind,
        "reply_to_message_id": request.reply_to_message_id,
        "buttons": [list(pair) for pair in request.buttons],
    }


def hello_version(payload: dict[str, Any]) -> int:
    """The protocol version a `hello` frame declares."""
    version = payload.get("protocol_version")
    if isinstance(version, bool) or not isinstance(version, int):
        raise BaileysProtocolError("hello has no protocol version")
    return version


def result_request_id(payload: dict[str, Any]) -> str:
    """The `request_id` a `send_result` answers."""
    return _text(payload, "request_id")


__all__ = [
    "BaileysProtocolError",
    "DOWN_MESSAGES",
    "MAX_INBOUND_TEXT_CHARS",
    "MAX_LINE_BYTES",
    "MAX_MEDIA_MIME_CHARS",
    "MAX_MESSAGE_ID_CHARS",
    "MAX_USERNAME_CHARS",
    "MSG_FATAL",
    "MSG_HELLO",
    "MSG_INBOUND",
    "MSG_QR",
    "MSG_READY",
    "MSG_RECEIPT",
    "MSG_SEND",
    "MSG_SEND_RESULT",
    "MSG_SHUTDOWN",
    "NO_CLOUD_ACCOUNT",
    "PROTOCOL_VERSION",
    "REASON_ENCODE_FAILED",
    "REASON_LINK_LOST",
    "REASON_NOT_WRITTEN",
    "REASON_NO_SIDECAR",
    "REASON_PAIRING",
    "REASON_SEND_TIMEOUT",
    "REASON_SESSION_FATAL",
    "UP_MESSAGES",
    "decode",
    "delivery_event",
    "encode",
    "hello_version",
    "inbound_event",
    "local_failure",
    "result_request_id",
    "send_outcome",
    "send_payload",
]
