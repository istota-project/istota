"""Where a relay question reaches its recipient, and what it says when it gets there.

One resolver for every destination kind, so the hold, the preview and (from
stage 3 on) the admission re-check all read the same answer. A destination is
resolved once at hold time, frozen into the request by its fingerprint, and
compared again before anything is delivered; nothing here retargets.

Order, decided in the relay spec: the recipient's own `relay_delivery`
preference, then the asker's `--via`, then the recipient's default room. An
unusable preference falls to the default room and never to `--via`, since the
recipient chose and the asker's choice would override them. An unusable `--via`
is refused with its own code, because the asker named something that cannot
work. No room is ever created for a relay.
"""

from __future__ import annotations

import json
import sqlite3

from . import db
from .whatsapp_requests import RequestError, binding_fingerprint, text_hash

KINDS = ("room", "whatsapp", "sms")

# The kinds a held question may be released to today. An SMS destination
# resolves, but nothing delivers one until the SMS arm lands, so a build without
# it refuses rather than holding a question it cannot send.
DELIVERABLE_KINDS = frozenset({"whatsapp", "room"})

_LABEL_MAX = 80


def label_text(value: str) -> str:
    from .confirmations import flatten

    return flatten(value)[:_LABEL_MAX]


def display_name(config, user_id: str) -> str:
    user = config.users.get(user_id)
    return label_text((user.display_name if user else "") or user_id)


def _stored_preference(conn: sqlite3.Connection, recipient_user_id: str) -> str:
    # Read the column directly: `UserProfile` gains the field with the settings
    # work, and the resolver must honour a value however it was written.
    row = conn.execute(
        "SELECT relay_delivery FROM user_profiles WHERE user_id=?", (recipient_user_id,),
    ).fetchone()
    value = row[0] if row is not None else ""
    return value if value in KINDS else ""


def _room(conn, config, recipient_user_id: str) -> dict:
    handle = db.default_web_room(conn, recipient_user_id)
    room = db.get_room(conn, handle.token) if handle is not None else None
    if (room is None or room.archived
            or db.list_room_members(conn, room.token) != [recipient_user_id]):
        raise RequestError("recipient_has_no_private_room")
    talk = db.get_room_binding(conn, room.token, "talk")
    name = label_text(handle.name or room.name or "")
    label = f"{display_name(config, recipient_user_id)}'s room"
    if name:
        label += " #" + name.lstrip("#")
    destination = {"kind": "room", "provider": "room", "room_token": room.token,
                   "talk_ref": talk.surface_ref if talk else None, "label": label}
    destination["fingerprint"] = destination_fingerprint(destination)
    return destination


def _whatsapp(conn, config, recipient_user_id: str) -> dict:
    from .transport.whatsapp.outbound import active_adapter, _destination

    if not config.whatsapp.enabled:
        raise RequestError("whatsapp_unavailable")
    adapter = active_adapter(config)
    if adapter is None:
        raise RequestError("whatsapp_unavailable")
    binding = db.get_whatsapp_binding(conn, recipient_user_id)
    if (binding is None or binding.provider != config.whatsapp.provider
            or not _destination(binding, adapter.caps)):
        raise RequestError("recipient_not_on_whatsapp")
    return {"kind": "whatsapp", "provider": config.whatsapp.provider,
            "fingerprint": binding_fingerprint(config.whatsapp.provider, binding),
            "label": f"{display_name(config, recipient_user_id)}'s WhatsApp"}


def _sms(conn, config, recipient_user_id: str) -> dict:
    if not config.sms.enabled:
        raise RequestError("sms_unavailable")
    number = config.sms_phone_number_for(recipient_user_id)
    if not number:
        raise RequestError("recipient_not_on_sms")
    # The same hash `private_origin` freezes for an SMS origin. The number
    # itself goes nowhere but into it.
    return {"kind": "sms", "provider": config.sms.provider, "fingerprint": text_hash(number),
            "label": f"{display_name(config, recipient_user_id)}'s SMS"}


_RESOLVERS = {"room": _room, "whatsapp": _whatsapp, "sms": _sms}


def resolve_destination(conn: sqlite3.Connection, config, *, recipient_user_id: str,
                        requested: str | None) -> dict:
    """The one destination a question to `recipient_user_id` goes to.

    Returns ``{"kind", "provider", "fingerprint", "label"}`` plus
    ``room_token`` and ``talk_ref`` for a room. Raises `RequestError` with a
    code that names what failed; none of them is the block, which the caller
    checks first and keeps behind `recipient_unavailable`.
    """
    if requested is not None and requested not in KINDS:
        raise RequestError("invalid_via")
    preference = _stored_preference(conn, recipient_user_id)
    if preference:
        try:
            return _RESOLVERS[preference](conn, config, recipient_user_id)
        except RequestError:
            pass
    elif requested:
        return _RESOLVERS[requested](conn, config, recipient_user_id)
    return _room(conn, config, recipient_user_id)


def check_room(conn: sqlite3.Connection, config, *, recipient_user_id: str, fingerprint: str) -> dict:
    """Re-resolve a room destination at admission and require the frozen one.

    Resolved as an ask naming the room would be, so a recipient who has since
    set another preference, re-pinned their default room, shared it or moved
    its Talk binding reads as a change. A change closes the relay; nothing here
    picks a new room.
    """
    try:
        current = resolve_destination(conn, config, recipient_user_id=recipient_user_id, requested="room")
    except RequestError:
        raise RequestError("destination_changed") from None
    if current["kind"] != "room" or current["fingerprint"] != fingerprint:
        raise RequestError("destination_changed")
    return current


def destination_fingerprint(destination: dict) -> str:
    """What admission compares, so a changed destination closes the relay."""
    if destination["kind"] == "room":
        return text_hash(json.dumps([destination["room_token"], destination["talk_ref"]],
                                    ensure_ascii=True, separators=(",", ":")))
    return destination["fingerprint"]


def stored_destination(destination: dict) -> dict:
    """The part of a destination kept on the relay: no number, no binding identity."""
    keep = ("kind", "room_token", "talk_ref", "label") if destination["kind"] == "room" else ("kind", "label")
    return {key: destination[key] for key in keep}


_REPLY_INSTRUCTIONS = {
    "room": "To answer {asker}, reply to this message.",
    "whatsapp": ("To send your answer to {asker}, reply to this message "
                 "or send !relay reply {relay_id} <answer>."),
    "sms": "To answer {asker}, send !relay reply {relay_id} <answer>.",
}


def render_question(config, *, asker: str, text: str, destination: dict, relay_id: str) -> str:
    """The question as the recipient reads it. Only the reply instruction varies."""
    display = display_name(config, asker)
    instruction = _REPLY_INSTRUCTIONS[destination["kind"]].format(asker=display, relay_id=relay_id)
    return (f"{label_text(config.bot_name)}, on behalf of {display} ({asker}):\n\n{text}\n\n"
            f"{instruction} Only that answer will be shared.")


def fit_question(config, destination: dict, wording: str) -> tuple[str, str | None]:
    """The stored service body and optional template, whole or refused.

    A question is never shortened: an asker approves exactly what is sent, so
    anything that would not arrive intact is `invalid_rendering`.
    """
    kind = destination["kind"]
    if kind == "whatsapp":
        from .transport.whatsapp.outbound import (
            active_adapter, render_template_result, render_whatsapp_result, template_available,
        )
        adapter = active_adapter(config)
        if adapter is None:
            raise RequestError("whatsapp_unavailable")
        service, truncated = render_whatsapp_result(wording, limit=adapter.caps.service_body_limit)
        if not service or truncated:
            raise RequestError("invalid_rendering")
        template = None
        if adapter.caps.supports_templates and template_available(config):
            candidate, truncated = render_template_result(wording)
            if candidate and not truncated:
                template = candidate
        return service, template
    if kind == "room":
        from .transport.talk import TalkTransport
        if destination.get("talk_ref") and len(wording) > TalkTransport.capabilities.max_message_length:
            raise RequestError("invalid_rendering")
        return wording, None
    from .transport.sms.outbound import render_sms
    if render_sms(wording, config.sms.max_segments).text != wording:
        raise RequestError("invalid_rendering")
    return wording, None
