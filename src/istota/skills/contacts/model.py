"""Shared contact records and vCard conversion. No backend credentials or I/O."""

from dataclasses import dataclass, field, fields
import re
import unicodedata
from uuid import uuid4

import vobject
from vobject.base import Component, ParseError, ValidateError
from vobject.vcard import Name


@dataclass
class ContactField:
    value: str
    kind: str | None = None


@dataclass
class Contact:
    id: str
    source: str
    book: str
    name: str
    given: str | None = None
    family: str | None = None
    nickname: str | None = None
    emails: list[ContactField] = field(default_factory=list)
    phones: list[ContactField] = field(default_factory=list)
    org: str | None = None
    title: str | None = None
    addresses: list[ContactField] = field(default_factory=list)
    birthday: str | None = None
    note: str | None = None
    writable: bool = False
    etag: str | None = None


@dataclass
class AddressBook:
    key: str
    source: str
    label: str
    writable: bool


@dataclass
class ContactInput:
    """None leaves a scalar unchanged; an empty string clears it.

    emails/phones are initial values for create. Updates use add/remove lists
    so unrelated entries and their parameters survive.
    """

    name: str | None = None
    given: str | None = None
    family: str | None = None
    nickname: str | None = None
    emails: list[ContactField] = field(default_factory=list)
    phones: list[ContactField] = field(default_factory=list)
    org: str | None = None
    title: str | None = None
    birthday: str | None = None
    note: str | None = None
    add_emails: list[ContactField] = field(default_factory=list)
    remove_emails: list[str] = field(default_factory=list)
    add_phones: list[ContactField] = field(default_factory=list)
    remove_phones: list[str] = field(default_factory=list)


def one_line(value: str) -> str:
    return re.sub(r"[\r\n\x85\u2028\u2029]+", " ", value)


def _text(value) -> str:
    if isinstance(value, (list, tuple)):
        return " ".join(_text(part) for part in value if part)
    return str(value) if value is not None else ""


def _value(card: Component, prop: str):
    entries = card.contents.get(prop, [])
    return entries[0].value if entries else None


def _optional(card: Component, prop: str) -> str | None:
    value = _value(card, prop)
    return one_line(_text(value)).strip() or None


def _kind(prop) -> str | None:
    values = []
    for param in prop.params.get("TYPE", []):
        values.extend(part.strip().lower() for part in param.split(",") if part.strip())
    for value in values:
        if value in {"home", "work", "cell", "mobile", "main", "other", "fax", "pager"}:
            return value
    return values[0] if values else None


def _name_parts(card: Component) -> tuple[str | None, str | None, str]:
    name = _value(card, "n")
    if name is None:
        return None, None, ""
    parts = [one_line(_text(getattr(name, part, ""))).strip()
             for part in ("prefix", "given", "additional", "family", "suffix")]
    return parts[1] or None, parts[3] or None, " ".join(part for part in parts if part)


def _birthday(value: str | None) -> str | None:
    if value and re.fullmatch(r"[0-9]{8}", value):
        return f"{value[:4]}-{value[4:6]}-{value[6:]}"
    if value and re.fullmatch(r"--[0-9]{4}", value):
        return f"{value[:4]}-{value[4:]}"
    return value


def contact_from_vcard(
    data: str, *, contact_id: str, book: str, writable: bool, etag: str | None = None,
) -> Contact | None:
    try:
        card = vobject.readOne(data)
        if card.name != "VCARD":
            return None
        given, family, structured_name = _name_parts(card)
        emails = [ContactField(one_line(_text(prop.value)), _kind(prop))
                  for prop in card.contents.get("email", [])]
        phones = [ContactField(one_line(_text(prop.value)), _kind(prop))
                  for prop in card.contents.get("tel", [])]
        name = _optional(card, "fn") or structured_name
        if not name:
            name = emails[0].value if emails else "(no name)"
        addresses = []
        for prop in card.contents.get("adr", []):
            parts = [one_line(_text(getattr(prop.value, part, ""))).strip()
                     for part in ("box", "extended", "street", "city", "region", "code", "country")]
            addresses.append(ContactField(
                ", ".join(part for part in parts if part), _kind(prop),
            ))
        org = _value(card, "org")
        if isinstance(org, (list, tuple)):
            org = " / ".join(_text(part) for part in org if part)
        note = _value(card, "note")
        return Contact(
            id=contact_id, source="nextcloud", book=one_line(book), name=name,
            given=given, family=family, nickname=_optional(card, "nickname"),
            emails=emails, phones=phones, org=one_line(_text(org)) or None,
            title=_optional(card, "title"), addresses=addresses,
            birthday=_birthday(_optional(card, "bday")),
            note=_text(note) if note is not None else None, writable=writable, etag=etag,
        )
    except (ParseError, ValidateError, ValueError, TypeError, AttributeError, StopIteration):
        return None


def _set_scalar(card: Component, prop: str, value: str) -> None:
    entries = card.contents.get(prop, [])
    if not value:
        card.contents.pop(prop, None)
        return
    # Keep the edited property's group and parameters too.
    entry = entries[0] if entries else card.add(prop)
    entry.value = [value] if prop == "org" else value


def _edit_fields(card: Component, prop: str, added: list[ContactField], removed: list[str]) -> None:
    entries = card.contents.get(prop, [])
    kept = [entry for entry in entries if one_line(_text(entry.value)) not in removed]
    if kept:
        card.contents[prop] = kept
    else:
        card.contents.pop(prop, None)
    for item in added:
        entry = card.add(prop)
        entry.value = one_line(item.value)
        if item.kind:
            entry.params["TYPE"] = [item.kind.upper()]


def _apply(card: Component, changes: ContactInput) -> None:
    for attr, prop in (("name", "fn"), ("nickname", "nickname"), ("org", "org"),
                       ("title", "title"), ("birthday", "bday"), ("note", "note")):
        value = getattr(changes, attr)
        if value is not None:
            _set_scalar(card, prop, value if prop == "note" else one_line(value))
    if changes.given is not None or changes.family is not None:
        name = _value(card, "n")
        if name is None:
            name = Name()
            card.add("n").value = name
        if changes.given is not None:
            name.given = one_line(changes.given)
        if changes.family is not None:
            name.family = one_line(changes.family)
    _edit_fields(card, "email", changes.emails + changes.add_emails, changes.remove_emails)
    _edit_fields(card, "tel", changes.phones + changes.add_phones, changes.remove_phones)
    if not (_optional(card, "fn") or _name_parts(card)[2]):
        raise ValueError("name_required")


def new_vcard(fields: ContactInput) -> str:
    card = vobject.vCard()
    card.add("uid").value = str(uuid4())
    card.add("prodid").value = "-//Istota//contacts//EN"
    _apply(card, fields)
    # FN is required by vCard even when the caller supplied only N.
    if not _optional(card, "fn"):
        card.add("fn").value = _name_parts(card)[2]
    return card.serialize()


def apply_changes_to_vcard(data: str, changes: ContactInput) -> str:
    for attribute in fields(changes):
        value = getattr(changes, attribute.name)
        if value is not None and value != []:
            break
    else:
        raise ValueError("nothing_to_change")
    card = vobject.readOne(data)
    if card.name != "VCARD":
        raise ValueError("invalid_vcard")
    _apply(card, changes)
    # Legacy cards may omit FN; do not invent or rewrite untouched fields.
    return card.serialize(validate=False)


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def match_score(contact: Contact, query: str) -> int:
    query = _normalized(query).strip()
    if not query:
        return 0
    name = _normalized(contact.name)
    if name == query:
        return 4
    if name.startswith(query):
        return 3
    for entry in contact.emails + contact.phones:
        if query in _normalized(entry.value):
            return 2
    digits = "".join(c for c in query if c.isdecimal())
    if len(digits) >= 4:
        for phone in contact.phones:
            number = "".join(c for c in _normalized(phone.value) if c.isdecimal())
            if digits in number:
                return 2
    values = [contact.name, contact.given, contact.family, contact.nickname,
              contact.org, contact.title, contact.birthday, contact.note]
    values.extend(address.value for address in contact.addresses)
    for value in values:
        if value and query in _normalized(value):
            return 1
    return 0
