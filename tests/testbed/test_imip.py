"""Invitations for Istota's own calendar, at the wire (`sharing/itip.py`).

Two halves of iMIP against a real SMTP and IMAP server, with nothing patched:

- an event applied under `invite` delivery goes out through the real SMTP
  submission and arrives with a `text/calendar` part whose Content-Type
  `method` and calendar `METHOD` agree;
- a calendar's `REPLY` put into the bot's mailbox is consumed by
  `poll_emails` with no task, and its answer recorded.

The addresses are the testbed mail server's own domains (`ext.test`,
`bot.test`), which its configuration routes.
"""

from __future__ import annotations

import json
import time

import pytest

from istota import db
from istota.config import SiteConfig
from istota.skills.email import EmailConfig, imap_session
from testbed.services import mail

from .conftest import USER_ADDRESS, USER_ID

pytestmark = pytest.mark.testbed

icalendar = pytest.importorskip("icalendar")

UID = "4f6c2d1e-9a8b-4c3d-8e7f-0a1b2c3d4e5f@istota.test"


def _apply(wire, ops):
    from istota.scheduler_deferred import _process_deferred_istota_events

    with db.get_db(wire.config.db_path) as conn:
        task = db.get_task(conn, db.create_task(conn, "calendar", USER_ID, source_type="cli"))
        conn.commit()
    user_dir = wire.config.temp_dir / "deferred"
    user_dir.mkdir(exist_ok=True)
    (user_dir / f"task_{task.id}_istota_events.json").write_text(json.dumps(ops), encoding="utf-8")
    return _process_deferred_istota_events(wire.config, task, user_dir)


def _create_op():
    from istota.sharing import events

    return events.upsert_op(UID, create=True, fields={
        "summary": "Tax review", "dtstart": "2026-10-29T13:00:00Z",
        "dtend": "2026-10-29T15:00:00Z", "all_day": False})


def _outbox_config(wire) -> EmailConfig:
    """The skill's own config for the catch-all mailbox, where istota's sends land."""
    return EmailConfig(
        imap_host=wire.server.host,
        imap_port=wire.server.imap_starttls_port,
        imap_user=mail.EXTERNAL_ADDRESS,
        imap_password=mail.MAIL_PASSWORD,
        smtp_host=wire.server.host,
        smtp_port=wire.server.smtp_starttls_port,
    )


def _calendar_parts_in_outbox(wire, *, timeout: float = 15.0):
    """Every `text/calendar` MIME part in the catch-all, once one has arrived."""
    deadline = time.monotonic() + timeout
    while True:
        found = []
        with imap_session(_outbox_config(wire)) as mailbox:
            mailbox.folder.set("INBOX")
            for msg in mailbox.fetch(mark_seen=False):
                found += [p for p in msg.obj.walk() if p.get_content_type() == "text/calendar"]
        if found or time.monotonic() >= deadline:
            return found
        time.sleep(0.5)


@pytest.fixture
def inviting(wire):
    wire.config.site = SiteConfig(hostname="istota.test")
    wire.config.bot_name = "Zorg"
    return wire


class TestTheOutboundInvitation:
    def test_a_request_arrives_with_matching_methods(self, inviting):
        assert _apply(inviting, [_create_op()]) == 1

        (part,) = _calendar_parts_in_outbox(inviting)
        cal = icalendar.Calendar.from_ical(part.get_payload(decode=True))
        assert part.get_param("method") == str(cal["METHOD"]) == "REQUEST"
        (vevent,) = cal.walk("VEVENT")
        assert str(vevent["UID"]) == UID
        assert str(vevent["ORGANIZER"]).lower() == f"mailto:{mail.BOT_ADDRESS}"
        assert str(vevent["ATTENDEE"]).lower() == f"mailto:{USER_ADDRESS}"
        with db.get_db(inviting.config.db_path) as conn:
            row = conn.execute("SELECT invite_status FROM istota_events WHERE uid = ?", (UID,)).fetchone()
        assert row["invite_status"] == "sent"


def _reply_ics() -> bytes:
    return "\r\n".join([
        "BEGIN:VCALENDAR", "PRODID:-//Example Corp.//Calendar Client//EN", "VERSION:2.0",
        "METHOD:REPLY", "BEGIN:VEVENT", f"UID:{UID}", "SEQUENCE:0",
        "DTSTAMP:20261026T090000Z", "DTSTART:20261029T130000Z", "DTEND:20261029T150000Z",
        "SUMMARY:Tax review", f"ORGANIZER:mailto:{mail.BOT_ADDRESS}",
        f"ATTENDEE;PARTSTAT=ACCEPTED:mailto:{USER_ADDRESS}",
        "END:VEVENT", "END:VCALENDAR", "",
    ]).encode()


def _wait_for_inbox(wire, *, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while True:
        with wire.inbox() as inbox:
            if inbox.uids():
                return
        if time.monotonic() >= deadline:
            raise AssertionError("the reply never reached the bot's mailbox")
        time.sleep(0.5)


class TestTheReply:
    def test_a_reply_is_consumed_with_no_task_and_applied(self, inviting):
        from istota.sharing import events

        with db.get_db(inviting.config.db_path) as conn:
            task = db.get_task(conn, db.create_task(conn, "calendar", USER_ID, source_type="cli"))
            inviting.config.email.enabled = False
            events.apply_ops(conn, inviting.config, task, [_create_op()])
            inviting.config.email.enabled = True
            conn.commit()

        inviting.send(
            from_addr=USER_ADDRESS, to_addr=mail.BOT_ADDRESS, subject="Accepted: Tax review",
            body="Carol has accepted.",
            headers=[("Authentication-Results", "mail; dmarc=pass header.from=ext.test")],
            attachments=[("invite.ics", "text/calendar", _reply_ics())],
        )
        _wait_for_inbox(inviting)

        assert inviting.poll() == []

        (row,) = [r for r in inviting.processed() if r["subject"] == "Accepted: Tax review"]
        assert row["routing_method"] == "itip_reply" and row["task_id"] is None
        assert [t for t in inviting.tasks() if t["source_type"] == "email"] == []
        with db.get_db(inviting.config.db_path) as conn:
            assert events.get_event(conn, USER_ID, UID).attendee_status == "ACCEPTED"
