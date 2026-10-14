"""Contact normalization and edits through the real vCard parser."""

from dataclasses import replace
from uuid import UUID

import pytest

from istota.skills.contacts.model import (
    ContactField, ContactInput, apply_changes_to_vcard, contact_from_vcard,
    match_score, new_vcard, one_line,
)


def card(*lines, version="3.0"):
    return "\r\n".join(["BEGIN:VCARD", f"VERSION:{version}", *lines, "END:VCARD", ""])


def parse(data):
    return contact_from_vcard(
        data, contact_id="nc:book/person.vcf", book="Personal", writable=True, etag='"v1"',
    )


@pytest.mark.parametrize("version", ["3.0", "4.0"])
def test_vcard_round_trip_and_normalized_fields(version):
    data = card(
        "UID:person", "FN:Jane Doe", "N:Doe;Jane;;;", "NICKNAME:JD",
        "EMAIL;TYPE=INTERNET,WORK:jane@example.com",
        "TEL;TYPE=VOICE,CELL:+1 (202) 555-0100", "ORG:Acme;Research", "TITLE:Engineer",
        "ADR;TYPE=HOME:;;1 Main St;Example City;Region;12345;Country",
        "BDAY:20000102", "NOTE:First line\\nSecond line", version=version,
    )
    contact = parse(data)
    assert contact.name == "Jane Doe"
    assert (contact.given, contact.family, contact.nickname) == ("Jane", "Doe", "JD")
    assert contact.emails == [ContactField("jane@example.com", "work")]
    assert contact.phones == [ContactField("+1 (202) 555-0100", "cell")]
    assert contact.org == "Acme / Research"
    assert contact.title == "Engineer"
    assert contact.addresses == [ContactField("1 Main St, Example City, Region, 12345, Country", "home")]
    assert contact.birthday == "2000-01-02"
    assert contact.note == "First line\nSecond line"
    assert (contact.id, contact.source, contact.book, contact.writable, contact.etag) == (
        "nc:book/person.vcf", "nextcloud", "Personal", True, '"v1"',
    )
    updated = apply_changes_to_vcard(data, ContactInput(title="Lead"))
    assert f"VERSION:{version}" in updated
    assert parse(updated) == replace(contact, title="Lead")


@pytest.mark.parametrize(("lines", "name"), [
    (["N:Doe;Jane;Middle;Dr.;Jr."], "Dr. Jane Middle Doe Jr."),
    (["EMAIL:jane@example.com"], "jane@example.com"),
    (["UID:empty"], "(no name)"),
])
def test_missing_fn_falls_back(lines, name):
    assert parse(card(*lines)).name == name


@pytest.mark.parametrize("data", ["not a vcard", "BEGIN:VCARD\r\nFN:Broken", "BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n"])
def test_malformed_card_is_skipped(data):
    assert parse(data) is None


@pytest.mark.parametrize(("parameter", "expected"), [
    (";TYPE=VOICE,WORK", "work"), (";TYPE=PREF,CELL", "cell"),
    (';TYPE="HOME,VOICE"', "home"), (";TYPE=CUSTOM", "custom"), ("", None),
])
def test_type_mapping(parameter, expected):
    assert parse(card("FN:Jane", f"TEL{parameter}:5550100")).phones[0].kind == expected


def test_create_uses_uid_and_escaped_values():
    import vobject

    fields = ContactInput(
        name="Jane, Doe", given="Jane", family="Doe",
        emails=[ContactField("jane@example.com", "work")],
        phones=[ContactField("+1 202 555 0100", "cell")],
        org="Acme; Labs", birthday="--01-02", note="First line\nSecond line",
    )
    data = new_vcard(fields)
    parsed = vobject.readOne(data)
    assert parsed.version.value == "3.0"
    assert str(UUID(parsed.uid.value)) == parsed.uid.value
    assert parsed.prodid.value == "-//Istota//contacts//EN"
    assert parse(data).name == fields.name
    assert parse(data).org == fields.org
    assert parse(data).birthday == "--01-02"
    assert parse(data).note == fields.note


def test_update_preserves_unedited_properties_and_groups():
    import vobject

    data = card(
        "UID:person", "FN:Jane Doe", "N:Doe;Jane;;;",
        "PHOTO;ENCODING=b;TYPE=JPEG:YWJj", "X-CUSTOM;X-PARAM=literal:Keep this",
        "item1.EMAIL;TYPE=HOME:jane@example.com", "item1.X-ABLabel:Personal",
        "TEL;TYPE=CELL:5550100", "ORG:Acme", "NOTE:Old note",
    )
    before = vobject.readOne(data)
    changed = apply_changes_to_vcard(data, ContactInput(
        name="Jane Smith", org="", note="New note",
        add_emails=[ContactField("jane@work.example.com", "work")],
        remove_phones=["5550100"],
    ))
    after = vobject.readOne(changed)
    for prop in ("uid", "n", "photo", "x-custom", "x-ablabel"):
        assert before.contents[prop][0].serialize() == after.contents[prop][0].serialize()
    assert before.email.serialize() == after.email.serialize()
    assert parse(changed).emails == [
        ContactField("jane@example.com", "home"), ContactField("jane@work.example.com", "work"),
    ]
    assert parse(changed).phones == []
    assert parse(changed).org is None
    assert parse(changed).note == "New note"


def test_remove_only_the_named_email_preserves_other_parameters():
    data = card("FN:Jane", "EMAIL;TYPE=HOME:jane@example.com", "EMAIL;TYPE=WORK;PREF=1:jane@work.example.com")
    updated = apply_changes_to_vcard(data, ContactInput(remove_emails=["jane@example.com"]))
    assert "EMAIL;PREF=1;TYPE=WORK:jane@work.example.com" in updated
    assert parse(updated).emails == [ContactField("jane@work.example.com", "work")]


def test_refuses_empty_edits_and_removing_last_name_source():
    with pytest.raises(ValueError, match="nothing_to_change"):
        apply_changes_to_vcard(card("FN:Jane"), ContactInput())
    with pytest.raises(ValueError, match="name_required"):
        apply_changes_to_vcard(card("FN:Jane", "EMAIL:jane@example.com"), ContactInput(name=""))
    with pytest.raises(ValueError, match="name_required"):
        new_vcard(ContactInput())
    assert parse(apply_changes_to_vcard(card("FN:Jane", "N:Doe;Jane;;;"), ContactInput(name=""))).name == "Jane Doe"


def test_match_score_ordering_unicode_and_phone_digits():
    base = parse(card("FN:Jane Doe"))
    exact = match_score(base, "ＪＡＮＥ ＤＯＥ")
    prefix = match_score(base, "jane")
    email = match_score(replace(base, name="Other", emails=[ContactField("jane@example.com", None)]), "jane")
    other = match_score(replace(base, name="Other", org="Jane's workshop"), "jane")
    assert exact > prefix > email > other > 0
    assert match_score(base, "doe") > 0
    assert match_score(base, "nobody") == 0
    assert match_score(base, "  ") == 0
    phone = replace(base, phones=[ContactField("+1 (202) 555-0100", "cell")])
    assert match_score(phone, "202555") == email
    assert match_score(phone, "555-0100") == email
    assert match_score(phone, "255") == 0


def test_line_breaks_collapse_in_names_emails_and_phones():
    assert one_line("a\r\nb\nc\u0085d\u2028e\u2029f") == "a b c d e f"
    contact = parse(card("FN:Jane\u2028Doe", "EMAIL:jane\\n@example.com", "TEL:555\u20290100"))
    assert contact.name == "Jane Doe"
    assert contact.emails[0].value == "jane @example.com"
    assert contact.phones[0].value == "555 0100"
