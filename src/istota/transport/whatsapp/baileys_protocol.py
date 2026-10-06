"""The wire format between the daemon and the Baileys sidecar.

One JSON object per line, terminated by ``\\n``, over a Unix socket. The
daemon listens and the sidecar dials, which is `devbox_proxy`'s arrangement
and is what lets the spec's "a local Unix socket the daemon owns, 0600" be
literally true — the daemon creates the inode and sets its mode, so the
sidecar's own umask cannot widen it.

`devbox/exec_protocol.py`'s shape, one property short of it. That one frames
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
import re
from datetime import datetime, timezone
from typing import Any

from . import media as media_rules
from ._types import (
    InboundWhatsAppEvent,
    WhatsAppDeliveryEvent,
    WhatsAppDeliveryStatus,
    WhatsAppGroupContext,
    WhatsAppGroupMember,
    WhatsAppGroupRoster,
    WhatsAppInboundMedia,
    WhatsAppMention,
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
#: A WhatsApp group's roster, read from `groupMetadata` (multiplayer D6).
MSG_GROUP_ROSTER = "group_roster"

MSG_SEND = "send"
MSG_SHUTDOWN = "shutdown"
#: Leave a WhatsApp group: D14, the host left it.
MSG_LEAVE_GROUP = "leave_group"
#: React to an inbound message with one emoji (ISSUE-655). Answered with a
#: `send_result` under the same request id. A sidecar that predates it logs
#: the frame as unexpected and never answers, which the bridge reads as a
#: failed reaction after its timeout, so the version does not move.
MSG_REACT = "react"
#: Fetch the file of an inbound message the sidecar still holds, for a later
#: turn that claims it (ISSUE-658). Answered with a `media_result` under the
#: same request id; a sidecar that predates it never answers, which the
#: bridge reads as nothing to claim, so the version does not move.
MSG_FETCH_MEDIA = "fetch_media"
MSG_MEDIA_RESULT = "media_result"

# The two group types are additive and the version does not move: a sidecar
# that predates them sends a group message with no sender, which the daemon
# still refuses, and logs a `leave_group` it does not know as unexpected.

#: Sidecar to daemon.
UP_MESSAGES: frozenset[str] = frozenset({
    MSG_HELLO, MSG_READY, MSG_QR, MSG_INBOUND, MSG_RECEIPT,
    MSG_SEND_RESULT, MSG_FATAL, MSG_GROUP_ROSTER, MSG_MEDIA_RESULT,
})

#: Daemon to sidecar.
DOWN_MESSAGES: frozenset[str] = frozenset({
    MSG_SEND, MSG_SHUTDOWN, MSG_LEAVE_GROUP, MSG_REACT, MSG_FETCH_MEDIA,
})

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

#: A JID or LID off the wire, before `identity` decides what it names.
MAX_JID_CHARS = 128

#: WhatsApp caps a group at 1,024 members; a roster past this is not one.
MAX_GROUP_MEMBERS = 1024

#: The sidecar's own cut on a message's mention list (`MAX_MENTIONS`).
MAX_MENTIONS = 32

#: A mention token as WhatsApp writes it: `@` and the digits of a JID's user
#: part. Anything else is not a token this side rewrites.
_MENTION_TOKEN = re.compile(r"@[0-9]{1,32}")

#: Every `media_error` the sidecar may name, and the local sentence each
#: becomes. `_SEND_REASONS`' rule, for its reason: the sidecar's own words
#: carry the destination JID and, on a Boom error, the whole request, so the
#: reason crosses as a key and the prose is written down in the daemon.
#:
#: **The prose itself lives in `media.py`**, because the Cloud adapter says the
#: same three things about its own fetch — there the daemon *is* the fetcher,
#: so `client.fetch_media` raises them directly with no wire to cross. What a
#: user is told is exactly what two copies would drift on. The wire keys stay
#: here, since they are this adapter's vocabulary and the sidecar's own, and
#: each maps onto a kind-free `media.reason` key: the sidecar's key says what
#: went wrong, and the record's kind says what it went wrong with.
_MEDIA_ERROR_KEYS: dict[str, str] = {
    "download_failed": "fetch_failed",
    "over_the_cap": "over_cap",
    "write_failed": "write_failed",
}
_UNKNOWN_MEDIA_ERROR_KEY = "fetch_unknown"
#: The image sentences, which `_media_error` answers for an `image` record.
_MEDIA_ERRORS: dict[str, str] = {
    wire: media_rules.reason("image", key)
    for wire, key in _MEDIA_ERROR_KEYS.items()
}
_UNKNOWN_MEDIA_ERROR = media_rules.reason("image", _UNKNOWN_MEDIA_ERROR_KEY)


def _media_error(kind: str, wire: str | None) -> str:
    """The sentence a record of *kind* carries for the sidecar's *wire* key.

    `None` and any key outside `_MEDIA_ERROR_KEYS` are the unknown failure.
    """
    key = _MEDIA_ERROR_KEYS.get(wire or "", _UNKNOWN_MEDIA_ERROR_KEY)
    return media_rules.reason(kind, key)

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
#: What a send is refused with while the sidecar has stopped reconnecting
#: because another client keeps replacing the connection (ISSUE-553). Its own
#: string, because the remedy is to stop the other client rather than to
#: re-pair.
REASON_CONNECTION_REPLACED = "another client is using the WhatsApp session"
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


def _group_mentions(value: object) -> tuple[WhatsAppMention, ...]:
    """The `mentions` list, with malformed entries dropped (ISSUE-601).

    Dropped rather than refused, unlike a roster member: a mention only
    decides how the turn's text reads, and refusing the line would lose the
    turn. An entry dropped here keeps its `@<id>` token in the text.
    """
    if not isinstance(value, list):
        return ()
    out: list[WhatsAppMention] = []
    dropped = 0
    for entry in value[:MAX_MENTIONS]:
        token = entry.get("token") if isinstance(entry, dict) else None
        jid = entry.get("jid", "") if isinstance(entry, dict) else None
        lid = entry.get("lid", "") if isinstance(entry, dict) else None
        if (
            not isinstance(token, str) or not _MENTION_TOKEN.fullmatch(token)
            or not isinstance(jid, str) or len(jid) > MAX_JID_CHARS
            or not isinstance(lid, str) or len(lid) > MAX_JID_CHARS
        ):
            dropped += 1
            continue
        out.append(WhatsAppMention(
            token=token, jid=jid, lid=lid, bot=entry.get("bot") is True,
        ))
    if dropped:
        logger.warning("whatsapp.baileys.mentions_dropped count=%d", dropped)
    return tuple(out)


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


def _dropped_media(field: str, kind: str = "image") -> WhatsAppInboundMedia:
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
        "whatsapp.baileys.media_dropped field=%s kind=%s: the message is kept "
        "and its media is not",
        field, kind,
    )
    return WhatsAppInboundMedia(
        staged_path="", mime_type="", byte_count=0,
        attached_for_user="", error=_media_error(kind, None), kind=kind,
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

    **The kind is `message_type`**, `image` or `audio`, never the sniff. Media
    fields on any other type are dropped, and the error sentence is the
    kind's: the sidecar's `media_error` key is kind-free.
    """
    error_raw = payload.get("media_error")
    name = payload.get("media_name")
    if error_raw is None and name is None:
        return None

    # The kind is what the sidecar said the message was, and a media field on
    # any other type is a combination our own sidecar never sends. It is
    # refused like every other unreadable field: the record carries a failure,
    # so STOP, commands and answers still work and a request takes the
    # media-failed reply rather than running without the file.
    declared = payload.get("message_type")
    if declared not in media_rules.MEDIA_KINDS:
        return _dropped_media("message_type")
    kind = declared

    if error_raw is not None:
        if not isinstance(error_raw, str) or not error_raw:
            return _dropped_media("media_error", kind)
        return WhatsAppInboundMedia(
            staged_path="",
            mime_type="",
            byte_count=0,
            attached_for_user="",
            error=_media_error(kind, error_raw.strip().lower()),
            kind=kind,
        )

    if not media_rules.is_staged_name(name):
        return _dropped_media("media_name", kind)

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
        return _dropped_media("media_mime", kind)

    # Absent reads as nought, matching the mime arm above: both are advisory,
    # so losing an image over a missing log label would be the strictness
    # landing on the wrong field. A value of the wrong *type* or a negative
    # one still drops, because that is a sidecar this side cannot read.
    byte_count = payload.get("media_bytes", 0)
    if byte_count is None:
        byte_count = 0
    if isinstance(byte_count, bool) or not isinstance(byte_count, int):
        return _dropped_media("media_bytes", kind)
    if byte_count < 0:
        return _dropped_media("media_bytes", kind)

    return WhatsAppInboundMedia(
        staged_path=name,
        mime_type=mime,
        byte_count=byte_count,
        attached_for_user="",
        error=None,
        kind=kind,
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

    A group message carries a `WhatsAppGroupContext` and `from_user` is its
    sender, not the chat (multiplayer D6). The flag is read off the line
    rather than off the JID's domain: the sidecar knows which chat a message
    arrived in. One with no sender — an older sidecar — is typed `group` and
    refused before any identity lookup, as every group message used to be.
    Group media is carried on the first branch only (ISSUE-646).

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
    if message_type == "audio":
        # Audio carries no caption, and the daemon holds that rather than the
        # sidecar: text on a voice note would reach STOP, the confirmation
        # parse and `!` dispatch, and spoken words never drive those gates.
        text = None
    unsupported_kind = None
    if message_type == "unsupported":
        # An allowlisted label and nothing else: the value is only ever
        # rendered, and an unknown one reads as "a message".
        declared_kind = payload.get("unsupported_kind")
        if isinstance(declared_kind, str) and declared_kind in media_rules.UNSUPPORTED_LABELS:
            unsupported_kind = declared_kind
    sender = _text(payload, "jid")
    group = None
    if payload.get("group") is True:
        # A group message names its sender beside the chat (multiplayer D6).
        # One that names none came from a sidecar that predates groups, and
        # no principal can be resolved for it, so it keeps the old refusal.
        # A button belongs to a direct chat. Media is carried (ISSUE-646):
        # `stage_inbound_media` asks `groups.media_recipient` whose inbox, if
        # anyone's, the file goes to.
        callback_data = None
        sender_jid = _bounded_text(
            payload.get("sender_jid"), "sender_jid", MAX_JID_CHARS,
        ) or ""
        sender_lid = _bounded_text(
            payload.get("sender_lid"), "sender_lid", MAX_JID_CHARS,
        ) or ""
        if len(sender) > MAX_JID_CHARS or not (sender_jid or sender_lid):
            message_type = "group"
            text = None
            inbound_media = None
        else:
            group = WhatsAppGroupContext(
                group_jid=sender, sender_lid=sender_lid,
                mentions_bot=payload.get("mentions_bot") is True,
                mentions=_group_mentions(payload.get("mentions")),
            )
            sender = sender_jid
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
            jid=sender,
        ),
        message_type=message_type,
        text=text,
        callback_data=callback_data,
        reply_to_message_id=_optional_text(payload.get("reply_to_message_id")),
        sent_at=_event_time(payload),
        media=inbound_media,
        group=group,
        unsupported_kind=unsupported_kind,
    )


def group_roster(payload: dict[str, Any]) -> WhatsAppGroupRoster:
    """One `group_roster` line as the record `groups.apply_roster` reads.

    Every member is refused rather than skipped when malformed: a roster is
    the room's audience, and a half-read one would end the presence of the
    people this side failed to parse.
    """
    group_jid = _text(payload, "group_jid")
    if len(group_jid) > MAX_JID_CHARS:
        raise BaileysProtocolError("group_jid exceeds the JID bound")
    bot_present = payload.get("bot_present")
    if not isinstance(bot_present, bool):
        raise BaileysProtocolError("group_roster has no bot_present")
    raw_members = payload.get("participants")
    if not isinstance(raw_members, list) or len(raw_members) > MAX_GROUP_MEMBERS:
        raise BaileysProtocolError("group_roster participants are unreadable")
    members = []
    for raw in raw_members:
        if not isinstance(raw, dict):
            raise BaileysProtocolError("a group_roster participant is not an object")
        jid = _bounded_text(raw.get("jid"), "participant jid", MAX_JID_CHARS) or ""
        lid = _bounded_text(raw.get("lid"), "participant lid", MAX_JID_CHARS) or ""
        if not (jid or lid):
            raise BaileysProtocolError("a group_roster participant names nobody")
        members.append(WhatsAppGroupMember(
            jid=jid, lid=lid,
            display_name=_bounded_text(raw.get("name"), "name", MAX_USERNAME_CHARS),
        ))
    return WhatsAppGroupRoster(
        group_jid=group_jid,
        subject=_bounded_text(payload.get("subject"), "subject", MAX_USERNAME_CHARS),
        members=tuple(members),
        added_by=_bounded_text(payload.get("added_by"), "added_by", MAX_JID_CHARS) or "",
        bot_present=bot_present,
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


def react_payload(
    request_id: str, *, to: str, message_id: str, reaction: str,
) -> dict[str, Any]:
    """A `react` frame: which chat, which inbound message, which emoji.

    The sidecar reacts only to a message it still holds in its inbound cache
    for that chat, since the reaction's key is the original message's.
    """
    return {
        "request_id": request_id,
        "to": to,
        "message_id": message_id,
        "reaction": reaction,
    }


def fetch_media_payload(
    request_id: str, *, chat: str, message_id: str,
) -> dict[str, Any]:
    """A `fetch_media` frame: which chat, and which inbound message's file."""
    return {"request_id": request_id, "chat": chat, "message_id": message_id}


def fetched_media(payload: dict[str, Any]) -> WhatsAppInboundMedia | None:
    """A `media_result` as the record a claimed file carries, or None.

    None when the sidecar held nothing to fetch. Otherwise the same decoding
    an `inbound` frame's media fields get, so a staged name is validated as
    one component here as there, and a failed fetch is a record with an
    `error`.
    """
    if payload.get("ok") is not True:
        return None
    return _inbound_media(payload)


def send_payload(request_id: str, request: WhatsAppSendRequest) -> dict[str, Any]:
    """A `WhatsAppSendRequest` flattened for the wire.

    `kind` crosses even though a Baileys deployment can only ever send
    `service`: the field is the ledger's own record of which rendering it
    claimed, and a sidecar that received `template` should refuse it loudly
    rather than silently sending a service message the ledger will report as a
    template. The template name and language are deliberately *not* sent —
    they are Meta account state with no meaning here.
    """
    payload = {
        "request_id": request_id,
        "to": request.to,
        "text": request.text,
        "kind": request.kind,
        "reply_to_message_id": request.reply_to_message_id,
        "buttons": [list(pair) for pair in request.buttons],
    }
    # Optional, and absent on a text send, so the protocol version stays where
    # it is (a bump refuses `hello` and takes the surface down on a deploy that
    # ships one half first, the ISSUE-508 shape). A sidecar predating media
    # ignores the field and sends `text`, the alt-text rendering.
    if request.media is not None:
        payload["media"] = {
            "name": request.media.name,
            "mimetype": request.media.mimetype,
            "kind": request.media.kind,
            "caption": request.media.caption,
        }
    return payload


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
    "MAX_GROUP_MEMBERS",
    "MAX_INBOUND_TEXT_CHARS",
    "MAX_JID_CHARS",
    "MAX_LINE_BYTES",
    "MAX_MEDIA_MIME_CHARS",
    "MAX_MESSAGE_ID_CHARS",
    "MAX_USERNAME_CHARS",
    "MSG_FATAL",
    "MSG_GROUP_ROSTER",
    "MSG_HELLO",
    "MSG_LEAVE_GROUP",
    "MSG_INBOUND",
    "MSG_QR",
    "MSG_READY",
    "MSG_RECEIPT",
    "MSG_SEND",
    "MSG_SEND_RESULT",
    "MSG_SHUTDOWN",
    "NO_CLOUD_ACCOUNT",
    "PROTOCOL_VERSION",
    "REASON_CONNECTION_REPLACED",
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
    "group_roster",
    "hello_version",
    "inbound_event",
    "local_failure",
    "result_request_id",
    "send_outcome",
    "send_payload",
]
