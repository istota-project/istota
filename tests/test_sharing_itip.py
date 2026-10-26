"""Invitations for Istota's own calendar (`sharing/itip.py`), and what comes back.

Outbound: an event applied under `invite` delivery is mailed to the user as an
iMIP `REQUEST` (or `CANCEL`) through the one daemon send path,
`skills/email.send_email(..., calendar=...)`, whose `text/calendar` part
carries the same method in its Content-Type as in its body.

Inbound: `_msg_to_email` keeps every calendar part of a message, bounded. A
`REPLY` for one of our UIDs is always consumed with no task, and applied only
when it authenticates (`_answer_refusal`), comes from the event owner's
address and names that address as its one ATTENDEE. A `REQUEST` or `CANCEL`
from someone else is recorded in `received_invitations` only when the mail
was admitted without a hold, authenticates, and its From is its ORGANIZER.

Stage 1 captured no provider replies, so the REPLY fixtures are built from
RFC 6047 section 2.4 and RFC 5546 section 3.2.3. Every address is on
example.org or example.com.
"""

from __future__ import annotations

import email as email_lib
import email.policy
from email.message import EmailMessage
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, EmailConfig, SiteConfig, UserConfig
from istota.skills.email import Email, EmailEnvelope

icalendar = pytest.importorskip("icalendar")

from istota.sharing import events, itip  # noqa: E402

BOT = "istota@example.org"
OWNER = "carol"
OWNER_ADDR = "carol@example.org"
CORRESPONDENT = "dana@example.com"
STRANGER = "mallory@example.com"
AUTHSERV = "mx.example.org"
UID = "7d3f0c52-1c1e-4a57-8f6b-2f1f6c1d9a10@istota.example.org"
OTHER_UID = "b5f4a1d0-0000-4000-8000-000000000001@elsewhere.example.com"


def _pass(domain):
    return (f"{AUTHSERV}; dmarc=pass header.from={domain}",)


def _fail(domain):
    return (f"{AUTHSERV}; dmarc=fail header.from={domain}",)


@pytest.fixture
def config(db_path, tmp_path):
    config = Config()
    config.db_path = db_path
    config.temp_dir = tmp_path / "temp"
    config.temp_dir.mkdir(exist_ok=True)
    config.skills_dir = tmp_path / "skills"
    config.skills_dir.mkdir(exist_ok=True)
    config.bot_name = "Zorg"
    config.site = SiteConfig(hostname="istota.example.org")
    config.email = EmailConfig(
        enabled=True, imap_host="imap.example.org", imap_port=993,
        imap_user="istota", imap_password="pw",  # private-data-ok
        smtp_host="smtp.example.org", smtp_port=587, bot_email=BOT,
        authserv_id=AUTHSERV,
    )
    config.users = {
        OWNER: UserConfig(email_addresses=[OWNER_ADDR],
                          trusted_email_senders=["*@example.com"]),
    }
    return config


def _seed_event(config, *, uid=UID, user=OWNER):
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(conn, "block thursday", user, source_type="cli")
        task = db.get_task(conn, task_id)
        events.apply_ops(conn, config, task, [events.upsert_op(uid, create=True, fields={
            "summary": "Tax review", "dtstart": "2026-10-29T13:00:00Z",
            "dtend": "2026-10-29T15:00:00Z", "all_day": False})])
        conn.commit()


def _event(config, uid=UID, user=OWNER):
    with db.get_db(config.db_path) as conn:
        return events.get_event(conn, user, uid)


# --- RFC-built fixtures ----------------------------------------------------

def _reply_ics(*, uid=UID, attendee=OWNER_ADDR, partstat="ACCEPTED", sequence=0,
               attendees=None) -> bytes:
    """A REPLY as RFC 5546 section 3.2.3 shapes it: one ATTENDEE with PARTSTAT."""
    lines = [
        "BEGIN:VCALENDAR", "PRODID:-//Example Corp.//Calendar Client//EN", "VERSION:2.0",
        "METHOD:REPLY", "BEGIN:VEVENT", f"UID:{uid}", f"SEQUENCE:{sequence}",
        "DTSTAMP:20261026T090000Z", "DTSTART:20261029T130000Z", "DTEND:20261029T150000Z",
        "SUMMARY:Tax review", f"ORGANIZER;CN=Zorg:mailto:{BOT}",
    ]
    for address in attendees if attendees is not None else [attendee]:
        lines.append(f"ATTENDEE;PARTSTAT={partstat};CN=Someone:mailto:{address}")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines).encode()


def _request_ics(*, method="REQUEST", uid=OTHER_UID, organizer=CORRESPONDENT, sequence=0,
                 summary="Quarterly planning", start="20261103T100000Z",
                 end="20261103T110000Z") -> bytes:
    lines = [
        "BEGIN:VCALENDAR", "PRODID:-//Example Corp.//Calendar Server//EN", "VERSION:2.0",
        f"METHOD:{method}", "BEGIN:VEVENT", f"UID:{uid}", f"SEQUENCE:{sequence}",
        "DTSTAMP:20261026T090000Z", f"DTSTART:{start}", f"DTEND:{end}",
        f"SUMMARY:{summary}", "LOCATION:Room 2", f"ORGANIZER;CN=Dana:mailto:{organizer}",
        f"ATTENDEE;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{OWNER_ADDR}",
    ]
    if method == "CANCEL":
        lines.append("STATUS:CANCELLED")
    lines += ["END:VEVENT", "END:VCALENDAR", ""]
    return "\r\n".join(lines).encode()


def _calendar_only(ics: bytes) -> bytes:
    """`text/calendar; method=REPLY` alone, as RFC 6047 section 2.4 allows."""
    msg = EmailMessage()
    msg["From"] = OWNER_ADDR
    msg["To"] = BOT
    msg["Subject"] = "Accepted: Tax review"
    msg.set_content(ics.decode(), subtype="calendar", params={"method": "REPLY"})
    return msg.as_bytes()


def _alternative(ics: bytes) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = OWNER_ADDR
    msg["To"] = BOT
    msg["Subject"] = "Accepted: Tax review"
    msg.set_content("Carol has accepted.")
    msg.add_alternative("<p>Carol has accepted.</p>", subtype="html")
    msg.add_alternative(ics.decode(), subtype="calendar", params={"method": "REPLY"})
    return msg


def _mixed(ics: bytes, attachment_type: str) -> bytes:
    """The alternative above inside `multipart/mixed`, plus an `invite.ics` attachment."""
    outer = EmailMessage()
    outer["From"] = OWNER_ADDR
    outer["To"] = BOT
    outer["Subject"] = "Accepted: Tax review"
    outer.make_mixed()
    inner = _alternative(ics)
    for header in ("From", "To", "Subject"):
        del inner[header]
    outer.attach(inner)
    maintype, subtype = attachment_type.split("/")
    outer.add_attachment(ics, maintype=maintype, subtype=subtype, filename="invite.ics")
    return outer.as_bytes()


def _mail_message(raw: bytes):
    from imap_tools import MailMessage

    return MailMessage.from_bytes(raw)


# --- outbound --------------------------------------------------------------

def _sent_messages():
    sent = []

    def _capture(msg, config, recipients=None, sent_folder=None):
        sent.append(msg)
        return "none"

    return sent, patch("istota.skills.email._send_smtp", side_effect=_capture)


def _calendar_part(msg):
    parts = [p for p in msg.walk() if p.get_content_type() == "text/calendar"]
    assert len(parts) == 1
    return parts[0]


def _body_method(part) -> str:
    cal = icalendar.Calendar.from_ical(part.get_content())
    return str(cal["METHOD"])


class TestTheOutboundMessage:
    @pytest.mark.parametrize("method", ["REQUEST", "CANCEL"])
    def test_the_content_type_method_is_the_body_method(self, config, method):
        from istota.mail.support import get_email_config
        from istota.skills.email import send_email

        _seed_event(config)
        event = _event(config)
        build = itip.build_request if method == "REQUEST" else itip.build_cancel
        ics = build(event, organizer=BOT, attendee=OWNER_ADDR, organizer_name="Zorg")
        sent, smtp = _sent_messages()
        with smtp:
            send_email(to=OWNER_ADDR, subject="x", body="summary", config=get_email_config(config),
                       calendar=(method, ics))
        (msg,) = sent
        assert msg.get_content_type() == "multipart/alternative"
        part = msg.get_payload()[-1]
        assert part.get_content_type() == "text/calendar"
        assert part.get_param("method") == method
        assert part.get_param("charset").lower() == "utf-8"
        assert _body_method(part) == method
        assert msg.get_payload()[0].get_content_type() == "text/plain"

    def test_the_calendar_part_follows_an_html_part(self, config):
        from istota.mail.support import get_email_config
        from istota.skills.email import send_email

        _seed_event(config)
        ics = itip.build_request(_event(config), organizer=BOT, attendee=OWNER_ADDR)
        sent, smtp = _sent_messages()
        with smtp:
            send_email(to=OWNER_ADDR, subject="x", body="text", html_body="<p>html</p>",
                       config=get_email_config(config), calendar=("REQUEST", ics))
        types = [p.get_content_type() for p in sent[0].get_payload()]
        assert types == ["text/plain", "text/html", "text/calendar"]

    def test_a_malformed_method_is_refused(self, config):
        from istota.mail.support import get_email_config
        from istota.skills.email import send_email

        sent, smtp = _sent_messages()
        with smtp, pytest.raises(ValueError):
            send_email(to=OWNER_ADDR, subject="x", body="y", config=get_email_config(config),
                       calendar=("REQUEST\r\nX-Evil: 1", b"BEGIN:VCALENDAR"))
        assert sent == []

    def test_a_request_names_the_organizer_and_one_attendee(self, config):
        _seed_event(config)
        cal = icalendar.Calendar.from_ical(itip.build_request(
            _event(config), organizer=BOT, attendee=OWNER_ADDR, organizer_name="Zorg"))
        assert str(cal["METHOD"]) == "REQUEST"
        (vevent,) = cal.walk("VEVENT")
        assert str(vevent["UID"]) == UID and int(vevent["SEQUENCE"]) == 0
        assert "DTSTAMP" in vevent
        assert str(vevent["ORGANIZER"]).lower() == f"mailto:{BOT}"
        attendee = vevent["ATTENDEE"]
        assert not isinstance(attendee, list)
        assert str(attendee).lower() == f"mailto:{OWNER_ADDR}"
        assert attendee.params["PARTSTAT"] == "NEEDS-ACTION" and attendee.params["RSVP"] == "TRUE"
        raw = itip.build_request(_event(config), organizer=BOT, attendee=OWNER_ADDR).decode()
        assert "DTSTART:20261029T130000Z" in raw and "VTIMEZONE" not in raw

    def test_a_cancel_is_cancelled(self, config):
        _seed_event(config)
        raw = itip.build_cancel(_event(config), organizer=BOT, attendee=OWNER_ADDR).decode()
        assert "METHOD:CANCEL" in raw and "STATUS:CANCELLED" in raw


# --- sending on apply ------------------------------------------------------

def _apply_file(config, ops, *, user=OWNER):
    import json

    from istota.scheduler_deferred import _process_deferred_istota_events

    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, db.create_task(conn, "calendar", user, source_type="cli"))
        conn.commit()
    user_dir = config.temp_dir / "deferred"
    user_dir.mkdir(exist_ok=True)
    (user_dir / f"task_{task.id}_istota_events.json").write_text(json.dumps(ops), encoding="utf-8")
    return _process_deferred_istota_events(config, task, user_dir)


def _create_op(uid=UID):
    return events.upsert_op(uid, create=True, fields={
        "summary": "Tax review", "dtstart": "2026-10-29T13:00:00Z",
        "dtend": "2026-10-29T15:00:00Z", "all_day": False})


def _alerts(config, user=OWNER):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT dedup_key, title, body FROM notifications WHERE user_id = ? AND source = 'task_alert'",
            (user,),
        ).fetchall()


class TestTheSendOnApply:
    def test_a_create_mails_a_request_and_records_sent(self, config):
        sent, smtp = _sent_messages()
        with smtp, patch("istota.notifications.store.deliver_pending"):
            assert _apply_file(config, [_create_op()]) == 1
        (msg,) = sent
        assert msg["To"] == OWNER_ADDR and BOT in msg["From"]
        assert _body_method(_calendar_part(msg)) == "REQUEST"
        assert _event(config).invite_status == "sent"

    def test_an_update_resends_with_a_higher_sequence_and_a_cancel_cancels(self, config):
        sent, smtp = _sent_messages()
        with smtp:
            _apply_file(config, [_create_op()])
            _apply_file(config, [events.upsert_op(UID, create=False, fields={"summary": "Moved"})])
            _apply_file(config, [events.cancel_op(UID)])
        sequences = []
        methods = []
        for msg in sent:
            cal = icalendar.Calendar.from_ical(_calendar_part(msg).get_content())
            methods.append(str(cal["METHOD"]))
            sequences.append(int(cal.walk("VEVENT")[0]["SEQUENCE"]))
        assert methods == ["REQUEST", "REQUEST", "CANCEL"]
        assert sequences == [0, 1, 2]

    def test_a_failed_send_records_send_failed_and_alerts_with_fixed_text(self, config):
        with patch("istota.skills.email._send_smtp", side_effect=OSError("smtp down: secret")), \
                patch("istota.notifications.delivery.send_notification", return_value=True) as push:
            _apply_file(config, [_create_op()])
        assert _event(config).invite_status == "send_failed"
        ((key, title, body),) = _alerts(config)
        assert key == "istota-calendar:send-failed"
        assert "Tax review" not in title + body and "secret" not in title + body
        assert push.called
        assert all("Tax review" not in str(call) for call in push.call_args_list)

    def test_no_address_stores_the_event_and_alerts_once(self, config):
        config.users[OWNER] = UserConfig(email_addresses=[])
        sent, smtp = _sent_messages()
        with smtp, patch("istota.notifications.delivery.send_notification",
                         return_value=True) as push:
            _apply_file(config, [_create_op()])
            _apply_file(config, [_create_op("second-1@istota.example.org")])
        assert sent == []
        assert _event(config).invite_status == "no_address"
        assert _event(config, "second-1@istota.example.org").invite_status == "no_address"
        assert [row[0] for row in _alerts(config)] == ["istota-calendar:no-address"]
        assert push.call_count == 1

    def test_feed_delivery_sends_nothing(self, config):
        from istota import user_profiles

        user_profiles.ensure_profile(config.db_path, OWNER)
        user_profiles.update_profile(config.db_path, OWNER, calendar_delivery="feed")
        sent, smtp = _sent_messages()
        with smtp:
            _apply_file(config, [_create_op()])
        assert sent == [] and _event(config).invite_status == "feed"

    def test_create_then_cancel_in_one_task_sends_nothing(self, config):
        sent, smtp = _sent_messages()
        with smtp:
            _apply_file(config, [_create_op(), events.cancel_op(UID)])
        assert sent == []


# --- calendar parts and parsing --------------------------------------------

class TestCalendarParts:
    def test_an_inline_part_is_kept(self):
        from istota.skills.email import _msg_to_email

        email = _msg_to_email(_mail_message(_alternative(_reply_ics()).as_bytes()))
        assert len(email.calendar_parts) == 1
        assert b"METHOD:REPLY" in email.calendar_parts[0]

    @pytest.mark.parametrize("kind", ["application/ics", "text/calendar"])
    def test_an_attached_invite_ics_is_kept_beside_the_inline_part(self, kind):
        from istota.skills.email import _msg_to_email

        email = _msg_to_email(_mail_message(_mixed(_reply_ics(), kind)))
        assert len(email.calendar_parts) == 2
        assert all(b"METHOD:REPLY" in part for part in email.calendar_parts)

    def test_count_and_size_are_bounded(self):
        from istota.skills.email import _msg_to_email

        outer = EmailMessage()
        outer["From"] = OWNER_ADDR
        outer["Subject"] = "many"
        outer.set_content("see attached")
        big = b"BEGIN:VCALENDAR\r\nX-PAD:" + b"x" * (itip.MAX_PART_BYTES + 1) + b"\r\nEND:VCALENDAR\r\n"
        outer.add_attachment(big, maintype="text", subtype="calendar", filename="big.ics")
        for n in range(6):
            outer.add_attachment(_reply_ics(sequence=n), maintype="text", subtype="calendar",
                                 filename=f"invite{n}.ics")
        email = _msg_to_email(_mail_message(outer.as_bytes()))
        assert len(email.calendar_parts) == itip.MAX_PARTS == 4
        assert all(len(part) <= itip.MAX_PART_BYTES for part in email.calendar_parts)
        assert [itip.parse_itip(p).sequence for p in email.calendar_parts] == [0, 1, 2, 3]

    def test_a_message_without_calendar_parts_has_none(self):
        from istota.skills.email import _msg_to_email

        msg = EmailMessage()
        msg["From"] = OWNER_ADDR
        msg.set_content("plain")
        assert _msg_to_email(_mail_message(msg.as_bytes())).calendar_parts == ()


class TestParseItip:
    @pytest.mark.parametrize("shape", ["alone", "alternative", "mixed_application", "mixed_text"])
    def test_every_reply_shape_parses(self, shape):
        from istota.skills.email import _msg_to_email

        ics = _reply_ics()
        raw = {
            "alone": lambda: _calendar_only(ics),
            "alternative": lambda: _alternative(ics).as_bytes(),
            "mixed_application": lambda: _mixed(ics, "application/ics"),
            "mixed_text": lambda: _mixed(ics, "text/calendar"),
        }[shape]()
        parts = _msg_to_email(_mail_message(raw)).calendar_parts
        assert parts
        for part in parts:
            parsed = itip.parse_itip(part)
            assert parsed.method == "REPLY" and parsed.uid == UID and parsed.sequence == 0
            assert parsed.organizer == BOT
            assert parsed.attendees == ((OWNER_ADDR, "ACCEPTED"),)

    def test_a_request_carries_its_fields_in_utc(self):
        parsed = itip.parse_itip(_request_ics(start="20261103T100000", end="20261103T110000"))
        assert parsed.method == "REQUEST" and parsed.organizer == CORRESPONDENT
        assert parsed.summary == "Quarterly planning" and parsed.location == "Room 2"
        utc = itip.parse_itip(_request_ics())
        assert (utc.dtstart, utc.dtend, utc.all_day) == ("2026-11-03T10:00:00Z", "2026-11-03T11:00:00Z", False)

    @pytest.mark.parametrize("raw", [b"", b"not a calendar", b"BEGIN:VCARD\r\nEND:VCARD\r\n",
                                     b"BEGIN:VCALENDAR\r\nMETHOD:REPLY\r\nEND:VCALENDAR\r\n"])
    def test_garbage_is_none(self, raw):
        assert itip.parse_itip(raw) is None


# --- the inbound path ------------------------------------------------------

_UIDS = [9000]


def _poll(config, *, sender, parts=(), auth=(), to=(BOT,), body="Accepted.", subject="Accepted: Tax review"):
    from istota.transport.email.inbound import poll_emails

    _UIDS[0] += 1
    uid = str(_UIDS[0])
    envelope = EmailEnvelope(id=uid, subject=subject, sender=sender,
                             date="Mon, 26 Oct 2026 12:00:00 +0000", is_read=False)
    email = Email(
        id=uid, subject=subject, sender=sender, date="Mon, 26 Oct 2026 12:00:00 +0000",
        body=body, attachments=[], message_id=f"<m{uid}@example.com>", to=tuple(to),
        authentication_results=auth[0] if auth else None,
        authentication_results_all=tuple(auth), calendar_parts=tuple(parts),
    )
    with (
        patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
        patch("istota.transport.email.inbound.read_email", return_value=email),
        patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        patch("istota.transport.email.inbound._deliver_confirmation_prompts") as prompts,
        patch("istota.transport.email.inbound._deliver_dmarc_alerts"),
        patch("istota.transport.email.inbound.deliver_pending"),
        patch("istota.transport.email.inbound._deliver_answer_acks"),
    ):
        created = poll_emails(config)
    queued = [p for call in prompts.call_args_list for p in call.args[1]]
    return uid, created, queued


def _ledger(config, uid):
    with db.get_db(config.db_path) as conn:
        row = conn.execute(
            "SELECT routing_method, task_id, user_id FROM processed_emails WHERE email_id = ?",
            (uid,)).fetchone()
    return dict(row) if row else None


def _task_count(config):
    with db.get_db(config.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM tasks WHERE source_type = 'email'").fetchone()[0]


@pytest.fixture(autouse=True)
def _fresh_volume_state():
    from istota.transport.email import inbound

    inbound._reset_volume_state()
    inbound._reset_dmarc_alert_dedup()
    yield
    inbound._reset_volume_state()
    inbound._reset_dmarc_alert_dedup()


class TestTheReplyIntercept:
    def test_an_authenticated_owner_reply_is_applied_with_no_task(self, config):
        _seed_event(config)
        uid, created, queued = _poll(config, sender=OWNER_ADDR, parts=[_reply_ics()],
                                     auth=_pass("example.org"))
        assert created == [] and queued == [] and _task_count(config) == 0
        assert _ledger(config, uid) == {"routing_method": "itip_reply", "task_id": None, "user_id": OWNER}
        assert _event(config).attendee_status == "ACCEPTED"

    def test_a_reply_in_an_attached_part_after_a_non_calendar_one_applies(self, config):
        _seed_event(config)
        _poll(config, sender=OWNER_ADDR, parts=[b"garbage", _reply_ics(partstat="DECLINED")],
              auth=_pass("example.org"))
        assert _event(config).attendee_status == "DECLINED"

    @pytest.mark.parametrize("case", ["forged_from", "dmarc_fail", "attendee_mismatch",
                                      "two_attendees", "unstamped", "no_authserv_id"])
    def test_an_unauthenticated_or_mismatched_reply_is_consumed_and_not_applied(self, config, case):
        _seed_event(config)
        sender, parts, auth = OWNER_ADDR, [_reply_ics()], _pass("example.org")
        if case == "forged_from":
            sender, auth = STRANGER, _pass("example.com")
            parts = [_reply_ics(attendee=STRANGER)]
        elif case == "dmarc_fail":
            auth = _fail("example.org")
        elif case == "attendee_mismatch":
            parts = [_reply_ics(attendee="someone@example.org")]
        elif case == "two_attendees":
            parts = [_reply_ics(attendees=[OWNER_ADDR, "someone@example.org"])]
        elif case == "unstamped":
            auth = ()
        elif case == "no_authserv_id":
            config.email.authserv_id = ""
        uid, created, queued = _poll(config, sender=sender, parts=parts, auth=auth)
        assert created == [] and queued == [] and _task_count(config) == 0
        assert _ledger(config, uid)["routing_method"] == "itip_reply"
        assert _event(config).attendee_status is None

    def test_a_reply_for_an_unknown_uid_is_ordinary_mail(self, config):
        _seed_event(config)
        uid, created, _ = _poll(config, sender=OWNER_ADDR, auth=_pass("example.org"),
                                parts=[_reply_ics(uid="unknown-1@elsewhere.example.com")])
        assert len(created) == 1
        assert _ledger(config, uid)["routing_method"] != "itip_reply"
        assert _event(config).attendee_status is None

    def test_another_users_event_is_not_applied_by_this_owner(self, config):
        config.users["erin"] = UserConfig(email_addresses=["erin@example.org"])
        _seed_event(config, user="erin")
        _poll(config, sender=OWNER_ADDR, parts=[_reply_ics()], auth=_pass("example.org"))
        assert _event(config, user="erin").attendee_status is None


def _invitations(config, user=OWNER):
    with db.get_db(config.db_path) as conn:
        return [dict(r) for r in conn.execute(
            "SELECT uid, organizer, sequence, status, summary, dtstart FROM received_invitations"
            " WHERE user_id = ? ORDER BY id", (user,)).fetchall()]


class TestInboundInvitations:
    def test_a_request_from_its_organizer_is_recorded_and_the_mail_still_processed(self, config):
        _, created, queued = _poll(config, sender=CORRESPONDENT, to=("istota+carol@example.org",),
                                   parts=[_request_ics()], auth=_pass("example.com"),
                                   subject="Invitation: Quarterly planning", body="Join us")
        assert len(created) == 1 and queued == []
        (row,) = _invitations(config)
        assert row == {"uid": OTHER_UID, "organizer": CORRESPONDENT, "sequence": 0,
                       "status": "confirmed", "summary": "Quarterly planning",
                       "dtstart": "2026-11-03T10:00:00Z"}

    def test_a_request_whose_from_is_not_its_organizer_is_not_recorded(self, config):
        _, created, _ = _poll(config, sender=CORRESPONDENT, to=("istota+carol@example.org",),
                              parts=[_request_ics(organizer="boss@example.com")],
                              auth=_pass("example.com"))
        assert len(created) == 1
        assert _invitations(config) == []

    def test_a_held_request_is_not_recorded(self, config):
        _, created, queued = _poll(config, sender="stranger@elsewhere.example.org",
                                   to=("istota+carol@example.org",),
                                   parts=[_request_ics(organizer="stranger@elsewhere.example.org")],
                                   auth=_pass("elsewhere.example.org"))
        assert len(created) == 1 and len(queued) == 1
        assert _invitations(config) == []

    def test_an_unauthenticated_request_is_not_recorded(self, config):
        _poll(config, sender=CORRESPONDENT, to=("istota+carol@example.org",),
              parts=[_request_ics()], auth=_fail("example.com"))
        assert _invitations(config) == []

    def test_sequence_decides_and_a_cancel_cancels(self, config):
        def send(**kw):
            _poll(config, sender=CORRESPONDENT, to=("istota+carol@example.org",),
                  parts=[_request_ics(**kw)], auth=_pass("example.com"))

        send(sequence=2, summary="Planning v2")
        send(sequence=1, summary="Planning v1, late")
        assert [(r["sequence"], r["summary"]) for r in _invitations(config)] == [(2, "Planning v2")]
        send(sequence=3, summary="Planning v3")
        assert [(r["sequence"], r["summary"]) for r in _invitations(config)] == [(3, "Planning v3")]
        send(method="CANCEL", sequence=4)
        assert [(r["sequence"], r["status"]) for r in _invitations(config)] == [(4, "cancelled")]

    def test_a_sender_cannot_replace_another_organizers_row(self, config):
        _poll(config, sender=CORRESPONDENT, to=("istota+carol@example.org",),
              parts=[_request_ics(sequence=1)], auth=_pass("example.com"))
        _poll(config, sender="eve@example.com", to=("istota+carol@example.org",),
              parts=[_request_ics(organizer="eve@example.com", method="CANCEL", sequence=9)],
              auth=_pass("example.com"))
        rows = {(r["organizer"], r["status"]) for r in _invitations(config)}
        assert (CORRESPONDENT, "confirmed") in rows


class TestTheReceivedStore:
    def test_delete_for_user_removes_received_invitations(self, config):
        _poll(config, sender=CORRESPONDENT, to=("istota+carol@example.org",),
              parts=[_request_ics()], auth=_pass("example.com"))
        with db.get_db(config.db_path) as conn:
            events.delete_for_user(conn, OWNER)
            conn.commit()
        assert _invitations(config) == []

    def test_text_is_bounded(self, config):
        with db.get_db(config.db_path) as conn:
            parsed = itip.parse_itip(_request_ics(summary="x" * 5000))
            assert itip.record_invitation(conn, OWNER, parsed, sender=CORRESPONDENT,
                                          message_id="<a@example.com>")
            conn.commit()
        (row,) = _invitations(config)
        assert len(row["summary"]) == events.SUMMARY_MAX_CHARS


def test_parsing_uses_no_raw_header_text():
    """The parser reads the part it is given, never a header; a REPLY in a
    Subject line is not a REPLY."""
    raw = email_lib.message_from_string(
        "Subject: METHOD:REPLY\n\nhello", policy=email.policy.default)
    assert itip.parse_itip(raw.as_bytes()) is None
