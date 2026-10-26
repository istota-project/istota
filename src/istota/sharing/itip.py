"""Invitations by mail (iMIP, RFC 6047) for Istota's own calendar, and what comes back.

Outbound. An event applied under ``invite`` delivery (`sharing.events`) is
mailed to the first of the user's addresses as an iTIP ``REQUEST`` (RFC 5546),
or ``CANCEL`` once cancelled, from ``[email] bot_email``, which is also the
``ORGANIZER``, so the user's answer comes back to the mailbox the poller reads.
The send goes through `mail.outbound_policy.recipients_require_hold` and
`skills.email.send_email(..., calendar=...)`, the one daemon send path, and
runs after the apply has committed (`deliver_invitations`), never under a
write lock. There is no retry: a failure records ``send_failed`` on the event
and raises a fixed-text ``task_alert``; the next change to the event sends
again. A user with no address gets ``no_address`` and one alert.

Inbound. The poller keeps every calendar part of a message
(`skills.email.Email.calendar_parts`, bounded by `MAX_PARTS` and
`MAX_PART_BYTES`). `find_reply` finds a ``REPLY`` for one of our UIDs; the
poller consumes such a mail whatever it says, and `apply_reply` records the
answer only when the mail authenticated (`_answer_refusal`), came from one of
the event owner's own addresses, and names that address as its only
``ATTENDEE``. `record_invitations` keeps a ``REQUEST`` or ``CANCEL`` someone
else organizes in ``received_invitations``, only when the caller says the mail
was admitted without a hold and authenticated, and only where the
authenticated From is the ``ORGANIZER``; rows are keyed on that sender, so a
sender reaches only rows for events it organizes. Nothing is ever sent back.

Every text field read here is third-party text. It is bounded on the way in
and fenced by its reader (the calendar skill), never interpolated into a
prompt's system half or a push.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timezone

from istota.sharing import events

logger = logging.getLogger("istota.sharing.itip")

MAX_PARTS = 4
MAX_PART_BYTES = 256 * 1024

METHODS = ("REQUEST", "REPLY", "CANCEL")
PARTSTATS = ("ACCEPTED", "DECLINED", "TENTATIVE", "DELEGATED", "NEEDS-ACTION")
MAX_RECEIVED_PER_USER = 1000
INVITATIONS_CALENDAR = "feed:invitations"

ALERT_SEND_FAILED = "istota-calendar:send-failed"
ALERT_NO_ADDRESS = "istota-calendar:no-address"

_ADDRESS_RE = re.compile(r"\A[^\s@<>\"\\,;:]{1,64}@[A-Za-z0-9.-]{1,253}\Z")
_UID_MAX_CHARS = 255
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u0085\u2028\u2029]")


@dataclass(frozen=True)
class ItipMessage:
    method: str
    uid: str
    sequence: int
    organizer: str | None
    attendees: tuple[tuple[str, str | None], ...]
    summary: str | None = None
    description: str | None = None
    location: str | None = None
    dtstart: str | None = None
    dtend: str | None = None
    all_day: bool = False


# --- parsing ---------------------------------------------------------------

def _address(value) -> str | None:
    """A calendar user address as a bare lowercase mail address, or None."""
    if value is None:
        return None
    text = str(value).strip()
    if text.lower().startswith("mailto:"):
        text = text[7:]
    text = text.strip().lower()
    return text if _ADDRESS_RE.fullmatch(text) else None


def _text(value, limit: int, *, multiline: bool = False) -> str | None:
    if value is None:
        return None
    text = str(value)
    if multiline:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    else:
        text = text.replace("\r", " ").replace("\n", " ")
    text = _CONTROL_RE.sub(" ", text).strip()
    return text[:limit] or None


def _stored_time(value) -> tuple[str | None, bool]:
    """A ``DTSTART``/``DTEND`` as the store keeps it: UTC ``...Z``, a date, or floating time."""
    if value is None:
        return None, False
    moment = getattr(value, "dt", value)
    if isinstance(moment, datetime):
        if moment.tzinfo is not None:
            return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), False
        return moment.replace(microsecond=0).isoformat(), False
    if isinstance(moment, date):
        return moment.isoformat(), True
    return None, False


def _attendees(vevent) -> tuple[tuple[str, str | None], ...]:
    raw = vevent.get("ATTENDEE")
    if raw is None:
        return ()
    items = raw if isinstance(raw, list) else [raw]
    found = []
    for item in items[:64]:
        address = _address(item)
        if address is None:
            # An attendee we cannot read still counts, so a reply naming two
            # people, one unreadable, is not taken for a reply naming one.
            address = ""
        params = getattr(item, "params", {}) or {}
        partstat = str(params.get("PARTSTAT", "") or "").strip().upper() or None
        found.append((address, partstat))
    return tuple(found)


def parse_itip(part: bytes) -> ItipMessage | None:
    """One calendar part as an iTIP message, or None when it is not one we read.

    Only ``REQUEST``, ``REPLY`` and ``CANCEL`` with a ``VEVENT`` carrying a
    ``UID``. A recurring series' master is preferred over an override. Never raises.
    """
    if not isinstance(part, (bytes, bytearray)) or not part or len(part) > MAX_PART_BYTES:
        return None
    try:
        from icalendar import Calendar

        cal = Calendar.from_ical(bytes(part))
        if getattr(cal, "name", "") != "VCALENDAR":
            return None
        method = str(cal.get("METHOD", "") or "").strip().upper()
        if method not in METHODS:
            return None
        vevents = [c for c in cal.walk("VEVENT")]
        if not vevents:
            return None
        masters = [c for c in vevents if c.get("RECURRENCE-ID") is None]
        vevent = (masters or vevents)[0]
        uid = str(vevent.get("UID", "") or "").strip()
        if not uid or len(uid) > _UID_MAX_CHARS or _CONTROL_RE.search(uid) or "\n" in uid:
            return None
        try:
            sequence = max(0, min(int(str(vevent.get("SEQUENCE", 0))), 2**31 - 1))
        except (TypeError, ValueError):
            sequence = 0
        dtstart, all_day = _stored_time(vevent.get("DTSTART"))
        dtend, _ = _stored_time(vevent.get("DTEND"))
        return ItipMessage(
            method=method, uid=uid, sequence=sequence,
            organizer=_address(vevent.get("ORGANIZER")),
            attendees=_attendees(vevent),
            summary=_text(vevent.get("SUMMARY"), events.SUMMARY_MAX_CHARS),
            description=_text(vevent.get("DESCRIPTION"), events.DESCRIPTION_MAX_CHARS,
                              multiline=True),
            location=_text(vevent.get("LOCATION"), events.LOCATION_MAX_CHARS),
            dtstart=dtstart, dtend=dtend, all_day=all_day,
        )
    except Exception:  # noqa: BLE001 - third-party bytes; anything unreadable is not iTIP
        return None


# --- building --------------------------------------------------------------

def _build(method: str, event: events.IstotaEvent, *, organizer: str, attendee: str,
           organizer_name: str = "", now: datetime | None = None) -> bytes:
    from icalendar import Calendar, Event, vCalAddress, vText

    cal = Calendar()
    cal.add("prodid", "-//Istota//istota calendar//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", method)
    item = Event()
    item.add("uid", event.uid)
    item.add("sequence", event.sequence)
    item.add("dtstamp", (now or datetime.now(timezone.utc)).replace(microsecond=0))
    item.add("dtstart", events.ical_time(event.dtstart, event.all_day))
    if event.dtend:
        item.add("dtend", events.ical_time(event.dtend, event.all_day))
    item.add("summary", event.summary)
    if event.location:
        item.add("location", event.location)
    if event.description:
        item.add("description", event.description)
    item.add("status", "CANCELLED" if method == "CANCEL" else "CONFIRMED")
    who = vCalAddress(f"mailto:{organizer}")
    if organizer_name:
        who.params["cn"] = vText(organizer_name)
    item["organizer"] = who
    guest = vCalAddress(f"mailto:{attendee}")
    guest.params["role"] = vText("REQ-PARTICIPANT")
    guest.params["partstat"] = vText("NEEDS-ACTION")
    if method == "REQUEST":
        guest.params["rsvp"] = vText("TRUE")
    item.add("attendee", guest, encode=0)
    cal.add_component(item)
    return cal.to_ical()


def build_request(event: events.IstotaEvent, *, organizer: str, attendee: str,
                  organizer_name: str = "", now: datetime | None = None) -> bytes:
    """The event as a ``METHOD:REQUEST`` calendar inviting the one attendee."""
    return _build("REQUEST", event, organizer=organizer, attendee=attendee,
                  organizer_name=organizer_name, now=now)


def build_cancel(event: events.IstotaEvent, *, organizer: str, attendee: str,
                 organizer_name: str = "", now: datetime | None = None) -> bytes:
    """The event as a ``METHOD:CANCEL`` calendar with ``STATUS:CANCELLED``."""
    return _build("CANCEL", event, organizer=organizer, attendee=attendee,
                  organizer_name=organizer_name, now=now)


# --- sending ---------------------------------------------------------------

def _when(event: events.IstotaEvent) -> str:
    if event.all_day:
        return f"On {event.dtstart}" + (f" until {event.dtend} (exclusive)" if event.dtend else "")
    start = event.dtstart.replace("T", " ").rstrip("Z")
    end = (event.dtend or "").replace("T", " ").rstrip("Z")
    return f"From {start} to {end} UTC" if end else f"At {start} UTC"


def _message(config, event: events.IstotaEvent, method: str) -> tuple[str, str]:
    bot = config.bot_name or "Istota"
    if method == "CANCEL":
        subject = f"Cancelled: {event.summary}"
        closing = f"{bot} cancelled this event."
    else:
        subject = f"{'Updated' if event.sequence else 'Invitation'}: {event.summary}"
        closing = f"{bot} put this event on its calendar for you. Accept it to add it to yours."
    lines = [event.summary, _when(event)]
    if event.location:
        lines.append(f"Where: {event.location}")
    lines += ["", closing]
    return subject, "\n".join(lines)


def send_invitation(config, conn: sqlite3.Connection, user_id: str,
                    event: events.IstotaEvent, method: str) -> str:
    """Mail one invitation to the user; the ``invite_status`` to record.

    ``conn`` is for the outbound policy's read, which needs the runtime trust
    table; the caller holds no write transaction on it, since this sends.
    """
    user = config.users.get(user_id)
    addresses = [a.strip() for a in (getattr(user, "email_addresses", None) or []) if a and a.strip()]
    if not addresses:
        return "no_address"
    address = addresses[0]
    bot_email = config.email.bot_email
    if not config.email.enabled or not bot_email:
        return "send_failed"

    from istota.mail.outbound_policy import recipients_require_hold
    from istota.mail.support import get_email_config
    from istota.skills.email import send_email

    try:
        hold = recipients_require_hold(config, conn, user_id, [address])
    except Exception:  # noqa: BLE001 - a gate that cannot run sends nothing
        logger.warning("Invitation for user %s not sent: the outbound check failed", user_id)
        return "send_failed"
    if hold is not None:
        logger.warning("Invitation for user %s not sent: held (%s)", user_id, hold)
        return "send_failed"
    build = build_cancel if method == "CANCEL" else build_request
    ics = build(event, organizer=bot_email, attendee=address, organizer_name=config.bot_name or "")
    subject, body = _message(config, event, method)
    try:
        send_email(
            to=address, subject=subject, body=body, config=get_email_config(config),
            from_addr=bot_email, from_name=config.bot_name or "", calendar=(method, ics),
        )
    except Exception as exc:  # noqa: BLE001 - recorded on the event; no retry
        logger.warning(
            "Invitation %s for user %s event %d failed to send (%s)",
            method, user_id, event.id, type(exc).__name__,
        )
        return "send_failed"
    logger.info("Sent invitation %s for user %s event %d", method, user_id, event.id)
    return "sent"


def _alert(conn, user_id: str, kind: str):
    from istota.notifications.resolvers import task_alert

    if kind == "send_failed":
        return task_alert.write(
            conn, user_id, dedup_key=ALERT_SEND_FAILED,
            title="A calendar invitation could not be sent",
            body=(
                "An event was saved to Istota's calendar, but the invitation to your "
                "calendar could not be mailed. The next change to the event sends it again."
            ),
            severity="warning", params={"status": "send_failed"},
        )
    return task_alert.write(
        conn, user_id, dedup_key=ALERT_NO_ADDRESS,
        title="No address for calendar invitations",
        body=(
            "An event was saved to Istota's calendar, but your profile has no email "
            "address to send the invitation to. Add one in Settings, or choose the "
            "calendar feed."
        ),
        severity="warning", params={"status": "no_address"},
    )


def deliver_invitations(config, user_id: str, uids: list[str]) -> None:
    """Send the invitations an apply left owed, after its transaction committed.

    One send per UID, with the event as it now stands: ``CANCEL`` when it is
    cancelled, else ``REQUEST``. Records ``invite_status`` and raises the
    alerts. Never raises.
    """
    from istota import db
    from istota.notifications.store import deliver_pending

    pending = []
    for uid in dict.fromkeys(uids):
        try:
            with db.get_db(config.db_path) as conn:
                event = events.get_event(conn, user_id, uid)
                if event is None:
                    continue
                method = "CANCEL" if event.status == "cancelled" else "REQUEST"
                status = send_invitation(config, conn, user_id, event, method)
            with db.get_db(config.db_path) as conn:
                first_without_address = status == "no_address" and conn.execute(
                    "SELECT 1 FROM istota_events WHERE user_id = ? AND invite_status = 'no_address'",
                    (user_id,),
                ).fetchone() is None
                conn.execute(
                    "UPDATE istota_events SET invite_status = ? WHERE id = ? AND user_id = ?",
                    (status, event.id, user_id),
                )
                if status == "send_failed":
                    pending.append(_alert(conn, user_id, "send_failed"))
                elif first_without_address:
                    pending.append(_alert(conn, user_id, "no_address"))
                conn.commit()
        except Exception:  # noqa: BLE001 - the event stands; only the invitation is lost
            logger.warning("Could not deliver an invitation for user %s", user_id, exc_info=True)
    if pending:
        try:
            deliver_pending(config, pending)
        except Exception:  # noqa: BLE001 - the bell rows stand
            logger.warning("Could not push the invitation alerts for user %s", user_id, exc_info=True)


# --- replies ---------------------------------------------------------------

def find_reply(conn: sqlite3.Connection, parts) -> tuple[ItipMessage, events.IstotaEvent] | None:
    """The first ``REPLY`` among ``parts`` whose UID is one of our events, with that event."""
    for part in parts or ():
        parsed = parse_itip(part)
        if parsed is None or parsed.method != "REPLY":
            continue
        row = conn.execute(
            "SELECT user_id FROM istota_events WHERE uid = ?", (parsed.uid,),
        ).fetchone()
        if row is None:
            continue
        event = events.get_event(conn, row[0], parsed.uid)
        if event is not None:
            return parsed, event
    return None


def apply_reply(conn: sqlite3.Connection, config, reply: ItipMessage, event: events.IstotaEvent,
                *, sender: str, refusal: str | None) -> str:
    """Record the attendee's answer when the reply proves it is theirs; a reason code.

    ``refusal`` is the poller's `_answer_refusal` for this mail: None only for
    a DMARC pass under our own ``authserv_id`` aligned with the From. Returns
    ``applied`` or why not, and writes only on ``applied``.
    """
    from istota.mail.support import own_addresses

    if refusal is not None:
        return f"unauthenticated:{refusal}"
    if event.status != "confirmed":
        return "event_cancelled"
    address = (sender or "").strip().lower()
    if not address or address not in own_addresses(config, event.user_id):
        return "not_owner"
    if len(reply.attendees) != 1 or reply.attendees[0][0] != address:
        return "attendee_mismatch"
    partstat = reply.attendees[0][1]
    if partstat not in PARTSTATS:
        return "unknown_partstat"
    conn.execute(
        "UPDATE istota_events SET attendee_status = ? WHERE id = ? AND user_id = ?",
        (partstat, event.id, event.user_id),
    )
    return "applied"


# --- invitations other people send -----------------------------------------

def record_invitation(conn: sqlite3.Connection, user_id: str, parsed: ItipMessage, *,
                      sender: str, message_id: str | None) -> bool:
    """Keep one ``REQUEST`` or ``CANCEL`` whose ORGANIZER is the authenticated sender.

    A higher ``SEQUENCE`` replaces the row; an equal or lower one is ignored,
    and a ``CANCEL`` at or above the row's sequence cancels it. True when a
    row was written. The caller decides admission and authentication.
    """
    organizer = (sender or "").strip().lower()
    if parsed.method not in ("REQUEST", "CANCEL") or not organizer or parsed.organizer != organizer:
        return False
    existing = conn.execute(
        "SELECT id, sequence, status FROM received_invitations"
        " WHERE user_id = ? AND uid = ? AND organizer = ?",
        (user_id, parsed.uid, organizer),
    ).fetchone()
    status = "cancelled" if parsed.method == "CANCEL" else "confirmed"
    fields = (parsed.sequence, status, parsed.summary, parsed.description, parsed.location,
              parsed.dtstart, parsed.dtend, int(parsed.all_day), (message_id or "")[:998] or None)
    if existing is None:
        count = conn.execute(
            "SELECT COUNT(*) FROM received_invitations WHERE user_id = ?", (user_id,),
        ).fetchone()[0]
        if count >= MAX_RECEIVED_PER_USER:
            logger.warning("User %s has %d received invitations; a new one was not kept", user_id, count)
            return False
        conn.execute(
            "INSERT INTO received_invitations (user_id, uid, organizer, sequence, status, summary,"
            " description, location, dtstart, dtend, all_day, message_id)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (user_id, parsed.uid, organizer, *fields),
        )
        return True
    if parsed.method == "CANCEL":
        if existing["status"] == "cancelled" or parsed.sequence < existing["sequence"]:
            return False
        conn.execute(
            "UPDATE received_invitations SET status = 'cancelled', sequence = ?,"
            " message_id = ?, received_at = datetime('now') WHERE id = ?",
            (parsed.sequence, fields[-1], existing["id"]),
        )
        return True
    if parsed.sequence <= existing["sequence"]:
        return False
    conn.execute(
        "UPDATE received_invitations SET sequence = ?, status = ?, summary = ?, description = ?,"
        " location = ?, dtstart = ?, dtend = ?, all_day = ?, message_id = ?,"
        " received_at = datetime('now') WHERE id = ?",
        (*fields, existing["id"]),
    )
    return True


def record_invitations(conn: sqlite3.Connection, user_id: str, parts, *, sender: str,
                       message_id: str | None) -> int:
    """Every invitation among ``parts`` `record_invitation` keeps; the count written."""
    written = 0
    seen: set[tuple[str, str, int]] = set()
    for part in parts or ():
        parsed = parse_itip(part)
        if parsed is None:
            continue
        key = (parsed.method, parsed.uid, parsed.sequence)
        if key in seen:
            continue
        seen.add(key)
        if record_invitation(conn, user_id, parsed, sender=sender, message_id=message_id):
            written += 1
    return written


@dataclass(frozen=True)
class ReceivedInvitation:
    uid: str
    organizer: str
    sequence: int
    summary: str | None
    description: str | None
    location: str | None
    dtstart: str | None
    dtend: str | None
    all_day: bool


def invitations_for_user(conn: sqlite3.Connection, user_id: str) -> list[ReceivedInvitation]:
    """The user's confirmed received invitations, earliest start first."""
    rows = conn.execute(
        "SELECT uid, organizer, sequence, summary, description, location, dtstart, dtend, all_day"
        " FROM received_invitations WHERE user_id = ? AND status = 'confirmed'"
        " ORDER BY dtstart, id",
        (user_id,),
    ).fetchall()
    return [
        ReceivedInvitation(
            uid=r["uid"], organizer=r["organizer"], sequence=r["sequence"], summary=r["summary"],
            description=r["description"], location=r["location"], dtstart=r["dtstart"],
            dtend=r["dtend"], all_day=bool(r["all_day"]),
        )
        for r in rows
    ]
