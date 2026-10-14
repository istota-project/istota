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


def contact_from_person(person: dict, *, source: str, writable: bool) -> Contact:
    def first(key):
        return next(iter(person.get(key, [])), {})

    name = first("names")
    emails = [ContactField(one_line(item.get("value", "")), item.get("type"))
              for item in person.get("emailAddresses", [])]
    phones = [ContactField(one_line(item.get("value", "")), item.get("type"))
              for item in person.get("phoneNumbers", [])]
    display = name.get("displayName") or name.get("unstructuredName")
    display = display or " ".join(filter(None, (name.get("givenName"), name.get("familyName"))))
    birthday = first("birthdays").get("date", {})
    date = None
    if birthday.get("month") and birthday.get("day"):
        year = f'{birthday["year"]:04d}' if birthday.get("year") else "-"
        date = f'{year}-{birthday["month"]:02d}-{birthday["day"]:02d}'
    sources = person.get("metadata", {}).get("sources", [])
    contact_source = next((item for item in sources if item.get("type") == "CONTACT"), {})
    return Contact(
        id=source + ":" + person["resourceName"], source=source,
        book="Other contacts" if source == "google-other" else "Google Contacts",
        name=one_line(display or (emails[0].value if emails else "(no name)")),
        given=one_line(name.get("givenName", "")) or None,
        family=one_line(name.get("familyName", "")) or None,
        nickname=first("nicknames").get("value"), emails=emails, phones=phones,
        org=first("organizations").get("name"), title=first("organizations").get("title"),
        addresses=[ContactField(one_line(item.get("formattedValue", "")), item.get("type"))
                   for item in person.get("addresses", [])],
        birthday=date, note=first("biographies").get("value"),
        writable=writable and source == "google",
        etag=contact_source.get("etag") or person.get("etag"),
    )


def person_from_input(changes: ContactInput, existing: dict | None = None) -> dict:
    """Return only edited People API fields; the caller uses them as the mask."""
    from copy import deepcopy
    from datetime import date

    existing = existing or {}
    result = {}
    if any(value is not None for value in (changes.name, changes.given, changes.family)):
        name = deepcopy(next(iter(existing.get("names", [])), {}))
        for key in ("metadata", "displayName", "displayNameLastFirst"):
            name.pop(key, None)
        if changes.name:
            # A free-form rename must not leave old structured parts in control.
            name = {"unstructuredName": one_line(changes.name)}
        elif changes.name is not None:
            name.pop("unstructuredName", None)
        for attr, key in (("given", "givenName"), ("family", "familyName")):
            if getattr(changes, attr) is not None:
                name[key] = one_line(getattr(changes, attr))
                if changes.name is None:
                    name.pop("unstructuredName", None)
        if not any(str(value).strip() for key, value in name.items()
                   if key in {"unstructuredName", "givenName", "familyName", "middleName"}):
            raise ValueError("name_required")
        result["names"] = [name]
    for attr, key in (("emails", "emailAddresses"), ("phones", "phoneNumbers")):
        additions = getattr(changes, attr) + getattr(changes, "add_" + attr)
        removals = getattr(changes, "remove_" + attr)
        if additions or removals:
            entries = [deepcopy(item) for item in existing.get(key, []) if item.get("value") not in removals]
            for item in additions:
                entries.append({"value": one_line(item.value), **({"type": item.kind} if item.kind else {})})
            result[key] = entries
    if changes.org is not None or changes.title is not None:
        entries = deepcopy(existing.get("organizations", [])) or [{}]
        for attr, key in (("org", "name"), ("title", "title")):
            value = getattr(changes, attr)
            if value is not None:
                if value:
                    entries[0][key] = one_line(value)
                else:
                    entries[0].pop(key, None)
        result["organizations"] = [item for item in entries if any(k != "metadata" for k in item)]
    for attr, key in (("nickname", "nicknames"), ("note", "biographies")):
        value = getattr(changes, attr)
        if value is not None:
            result[key] = ([{"value": value, "contentType": "TEXT_PLAIN"}] if attr == "note"
                           else [{"value": one_line(value)}]) if value else []
    if changes.birthday is not None:
        result["birthdays"] = []
        if changes.birthday:
            raw = changes.birthday
            try:
                parsed = date.fromisoformat("2000" + raw[1:] if raw.startswith("--") else raw)
            except ValueError:
                raise ValueError("invalid_birthday") from None
            birthday = {"month": parsed.month, "day": parsed.day}
            if not raw.startswith("--"):
                birthday["year"] = parsed.year
            result["birthdays"] = [{"date": birthday}]
    if not result:
        raise ValueError("nothing_to_change")
    return result
