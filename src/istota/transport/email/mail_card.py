"""The incoming-mail card's metadata: one schema, both sides of the column.

`received_mail_meta` builds it from the parsed mail at intake, for
`messages.received_mail` and `processed_emails.mail_meta` alike;
`stored_received_mail` reads it back out of either column. The read re-asserts
every cap and shape the build writes, since the column is JSON nothing else
validates, and it lives here so a field added or a cap changed on one side
cannot be silently dropped or re-cut on the other (ISSUE-642).

A leaf apart from `inbound`, because `threads` reads the column and `inbound`
imports `threads`.
"""

from __future__ import annotations

import json
from email.utils import parseaddr

#: Addresses kept per list, and attachments per mail.
MAIL_META_LIST_CAP = 50
#: Characters kept per string: the RFC 5321 path limit.
MAIL_META_STRING_CAP = 320
#: The sender badge's values (`inbound.sender_check_for`).
SENDER_CHECKS = frozenset({"verified", "failed", "none"})


def meta_str(value: object) -> str:
    return value[:MAIL_META_STRING_CAP] if isinstance(value, str) else ""


def _meta_person(entry: object, names: dict) -> dict | None:
    name, address = parseaddr(str(entry or ""))
    address = address.strip()
    if not address:
        return None
    if not name:
        known = names.get(address.lower())
        name = known if isinstance(known, str) else ""
    return {"name": meta_str(name), "address": meta_str(address)}


def _meta_people(entries: object, names: dict) -> list[dict]:
    people: list[dict] = []
    for entry in entries or ():
        person = _meta_person(entry, names)
        if person is not None:
            people.append(person)
        if len(people) == MAIL_META_LIST_CAP:
            break
    return people


def _attachment(item: object, path: object) -> dict | None:
    if not isinstance(item, dict) or not meta_str(item.get("filename")):
        return None
    entry: dict = {"filename": meta_str(item["filename"])}
    if isinstance(item.get("size"), int) and not isinstance(item["size"], bool):
        entry["size"] = item["size"]
    if isinstance(item.get("content_type"), str):
        entry["content_type"] = meta_str(item["content_type"])
    if isinstance(path, str) and path:
        entry["path"] = path
    return entry


def received_mail_meta(
    email, *, sender_check: str, trusted: bool,
    stored_paths: "dict[str, str] | None" = None,
) -> dict:
    """The card's metadata, built from the parsed mail at intake.

    So the card never renders a raw header at read time. Addresses are stored
    as written. The parsed mail carries no Bcc, so none is stored.
    ``stored_paths`` maps an attachment's leaf name to the inbox copy written
    for it.
    """
    from istota.skills.email import attachment_leaf_name  # noqa: PLC0415

    names = getattr(email, "display_names", None) or {}
    stored = stored_paths or {}
    manifest = list(getattr(email, "attachment_manifest", None) or [])
    if not manifest:
        manifest = [{"filename": n} for n in getattr(email, "attachments", None) or []]
    attachments: list[dict] = []
    for item in manifest[:MAIL_META_LIST_CAP]:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            continue
        entry = _attachment(item, stored.get(attachment_leaf_name(item["filename"])))
        if entry is not None:
            attachments.append(entry)
    return {
        "from": _meta_person(getattr(email, "sender", None), names)
        or {"name": "", "address": ""},
        "to": _meta_people(getattr(email, "to", None), names),
        "cc": _meta_people(getattr(email, "cc", None), names),
        "date": meta_str(getattr(email, "date", None)),
        "subject": meta_str(getattr(email, "subject", None)),
        "message_id": meta_str(getattr(email, "message_id", None)),
        "in_reply_to": meta_str(getattr(email, "in_reply_to", None)),
        "attachments": attachments,
        "sender_check": sender_check if sender_check in SENDER_CHECKS else "none",
        "trusted": bool(trusted),
    }


def _stored_person(value: object) -> dict | None:
    if not isinstance(value, dict):
        return None
    address = meta_str(value.get("address"))
    if not address:
        return None
    return {"name": meta_str(value.get("name")), "address": address}


def _stored_people(values: object) -> list[dict]:
    if not isinstance(values, list):
        return []
    people = (_stored_person(v) for v in values[:MAIL_META_LIST_CAP])
    return [p for p in people if p is not None]


def stored_received_mail(raw: object) -> dict | None:
    """The card's metadata read back out of its column, or None.

    None for anything that is not a JSON object. Otherwise every field is
    coerced to the shape `received_mail_meta` writes, with the same caps, so
    the builder's own output reads back unchanged. An attachment's ``path`` is
    returned as stored: whether a viewer may follow it is the reader's call.
    """
    if not raw:
        return None
    try:
        stored = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(stored, dict):
        return None
    attachments: list[dict] = []
    items = stored.get("attachments")
    for item in (items if isinstance(items, list) else [])[:MAIL_META_LIST_CAP]:
        entry = _attachment(item, item.get("path") if isinstance(item, dict) else None)
        if entry is not None:
            attachments.append(entry)
    check = stored.get("sender_check")
    return {
        "from": _stored_person(stored.get("from")) or {"name": "", "address": ""},
        "to": _stored_people(stored.get("to")),
        "cc": _stored_people(stored.get("cc")),
        "date": meta_str(stored.get("date")),
        "subject": meta_str(stored.get("subject")),
        "message_id": meta_str(stored.get("message_id")),
        "in_reply_to": meta_str(stored.get("in_reply_to")),
        "attachments": attachments,
        "sender_check": check if check in SENDER_CHECKS else "none",
        "trusted": stored.get("trusted") is True,
    }
