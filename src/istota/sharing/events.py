"""Istota's own calendar: single events Istota organizes for a user, and their feed.

The calendar skill writes an event as a deferred op (`OP_UPSERT`,
`OP_CANCEL`) into the task's ``istota_events`` file, and the scheduler applies
the file after the task succeeds (`apply_ops`, through
`scheduler_deferred._process_deferred_istota_events`). The file is
model-writable, so the apply step trusts none of it: the user is the task's,
a UID another user owns is refused, every field is checked again, and at most
`[sharing] events_per_task` entries are applied, refused ones counted.

Times are stored and served in UTC (``YYYY-MM-DDTHH:MM:SSZ``), or as dates
for an all-day event, so the feed needs no ``VTIMEZONE``. A UID is
``<uuid4>@<site host>`` and stays with the event; an update or a cancel bumps
``sequence``.

The feed (`render_feed`) is every confirmed event of one user as a
``VCALENDAR``, served at ``/istota/calendar/<token>.ics`` by
`sharing.feed_routes`. The token is 32 random bytes (`lib.tokens`), shown
once and stored as its SHA-256 digest; a user has at most one, and a reset
replaces it. The feed carries every confirmed event whatever
`calendar_delivery` says, so a user who switches from invitations to the feed
loses nothing. Under ``invite`` delivery `apply_ops` lists the UIDs owed an
invitation in its report, and `sharing.itip.deliver_invitations` mails them
after the apply commits; until then ``invite_status`` is NULL.
"""

from __future__ import annotations

import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone

from istota.lib import tokens

logger = logging.getLogger("istota.sharing.events")

CALENDAR_ID = "istota"
OP_UPSERT = "istota_event_upsert"
OP_CANCEL = "istota_event_cancel"
DEFERRED_SUFFIX = "istota_events"
DELIVERY_VALUES = ("invite", "feed")

SUMMARY_MAX_CHARS = 500
LOCATION_MAX_CHARS = 500
DESCRIPTION_MAX_CHARS = 8000
FEED_PATH = "/istota/calendar/"

_UID_RE = re.compile(r"\A[A-Za-z0-9._-]{1,64}@[A-Za-z0-9.-]{1,253}\Z")
_UTC_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")
_DATE_RE = re.compile(r"\A\d{4}-\d{2}-\d{2}\Z")
_TOKEN_RE = re.compile(r"\A[A-Za-z0-9_-]{43}\Z")
# Control characters, CR included. LF is allowed in a description only, which
# the format escapes; in a one-line field some readers take it as a new property.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\u0085  ]")
_FIELDS = ("summary", "description", "location", "dtstart", "dtend", "all_day")

_COLUMNS = (
    "id, user_id, uid, sequence, status, summary, description, location, dtstart, dtend,"
    " all_day, attendee_status, invite_status, created_by_task, created_at, updated_at,"
    " cancelled_at"
)


class EventError(ValueError):
    """An op or field that cannot be applied, as a fixed reason code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class IstotaEvent:
    id: int
    user_id: str
    uid: str
    sequence: int
    status: str
    summary: str
    description: str | None
    location: str | None
    dtstart: str
    dtend: str | None
    all_day: bool
    attendee_status: str | None
    invite_status: str | None
    created_by_task: int | None
    created_at: str
    updated_at: str
    cancelled_at: str | None


@dataclass
class ApplyReport:
    applied: int = 0
    refused: list[tuple[int, str]] = field(default_factory=list)
    dropped: int = 0
    # UIDs owed an invitation under `invite` delivery, in first-touch order,
    # for `sharing.itip.deliver_invitations` once the apply has committed.
    invitations: list[str] = field(default_factory=list)


def _from_row(row) -> IstotaEvent:
    return IstotaEvent(
        id=row["id"], user_id=row["user_id"], uid=row["uid"], sequence=row["sequence"],
        status=row["status"], summary=row["summary"], description=row["description"],
        location=row["location"], dtstart=row["dtstart"], dtend=row["dtend"],
        all_day=bool(row["all_day"]), attendee_status=row["attendee_status"],
        invite_status=row["invite_status"], created_by_task=row["created_by_task"],
        created_at=row["created_at"], updated_at=row["updated_at"],
        cancelled_at=row["cancelled_at"],
    )


def new_uid(site_hostname: str) -> str:
    """A fresh UID, ``<uuid4>@<host>``: the site's host part only, never its port."""
    host = (site_hostname or "").strip().lower().rsplit("@", 1)[-1]
    host = host.split(":", 1)[0] if not host.startswith("[") else ""
    if not re.fullmatch(r"[a-z0-9.-]{1,253}", host or ""):
        host = "istota.invalid"
    return f"{uuid.uuid4()}@{host}"


def upsert_op(uid: str, *, create: bool, fields: dict) -> dict:
    """The deferred op that creates (``create``) or changes one event."""
    return {"op": OP_UPSERT, "uid": uid, "create": bool(create), "fields": dict(fields)}


def cancel_op(uid: str) -> dict:
    return {"op": OP_CANCEL, "uid": uid}


def _text(value: object, limit: int, *, required: bool, multiline: bool = False) -> str | None:
    if value is None or value == "":
        if required:
            raise EventError("missing_summary")
        return None
    if not isinstance(value, str) or len(value) > limit or _CONTROL_RE.search(value):
        raise EventError("invalid_text")
    if "\n" in value and not multiline:
        raise EventError("invalid_text")
    return value


def _parse_time(value: object, all_day: bool) -> date | datetime:
    if not isinstance(value, str):
        raise EventError("invalid_time")
    try:
        if all_day:
            if not _DATE_RE.fullmatch(value):
                raise EventError("invalid_time")
            return date.fromisoformat(value)
        if not _UTC_RE.fullmatch(value):
            raise EventError("invalid_time")
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        raise EventError("invalid_time") from None


def check_fields(fields: dict) -> dict:
    """A whole event's fields, checked: what `apply_ops` stores, and what the skill sends."""
    if not isinstance(fields, dict) or set(fields) - set(_FIELDS):
        raise EventError("invalid_fields")
    all_day = fields.get("all_day", False)
    if not isinstance(all_day, bool):
        raise EventError("invalid_fields")
    start = _parse_time(fields.get("dtstart"), all_day)
    end = _parse_time(fields.get("dtend"), all_day)
    if end <= start:
        raise EventError("invalid_time")
    return {
        "summary": _text(fields.get("summary"), SUMMARY_MAX_CHARS, required=True),
        "description": _text(fields.get("description"), DESCRIPTION_MAX_CHARS, required=False,
                             multiline=True),
        "location": _text(fields.get("location"), LOCATION_MAX_CHARS, required=False),
        "dtstart": fields["dtstart"], "dtend": fields["dtend"], "all_day": all_day,
    }


def get_event(conn: sqlite3.Connection, user_id: str, uid: str) -> IstotaEvent | None:
    """The user's event by UID, in any state; another user's reads as missing."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM istota_events WHERE uid = ? AND user_id = ?", (uid, user_id),
    ).fetchone()
    return _from_row(row) if row is not None else None


def events_for_user(
    conn: sqlite3.Connection, user_id: str, *, include_cancelled: bool = False,
) -> list[IstotaEvent]:
    """The user's events, earliest start first."""
    sql = f"SELECT {_COLUMNS} FROM istota_events WHERE user_id = ?"
    if not include_cancelled:
        sql += " AND status = 'confirmed'"
    rows = conn.execute(sql + " ORDER BY dtstart, id", (user_id,)).fetchall()
    return [_from_row(row) for row in rows]


def effective_delivery(config, stored: str | None) -> str:
    """How the user's events reach them now: the stored choice, or feed without email."""
    if not getattr(getattr(config, "email", None), "enabled", False):
        return "feed"
    return stored if stored in DELIVERY_VALUES else "invite"


def _stored_delivery(conn: sqlite3.Connection, user_id: str) -> str | None:
    try:
        row = conn.execute(
            "SELECT calendar_delivery FROM user_profiles WHERE user_id = ?", (user_id,),
        ).fetchone()
    except sqlite3.OperationalError:
        return None
    return row[0] if row is not None else None


def _apply_one(conn, task, op, delivery: str) -> None:
    if not isinstance(op, dict):
        raise EventError("malformed")
    kind = op.get("op")
    uid = op.get("uid")
    if kind not in (OP_UPSERT, OP_CANCEL) or not isinstance(uid, str) or not _UID_RE.fullmatch(uid):
        raise EventError("malformed")
    owner = conn.execute("SELECT user_id FROM istota_events WHERE uid = ?", (uid,)).fetchone()
    if owner is not None and owner[0] != task.user_id:
        raise EventError("not_owned")
    current = get_event(conn, task.user_id, uid)
    feed_status = "feed" if delivery == "feed" else None

    if kind == OP_CANCEL:
        if current is None:
            raise EventError("not_found")
        if current.status == "cancelled":
            raise EventError("already_cancelled")
        conn.execute(
            "UPDATE istota_events SET status = 'cancelled', sequence = sequence + 1,"
            " cancelled_at = datetime('now'), updated_at = datetime('now')"
            " WHERE id = ? AND user_id = ?",
            (current.id, task.user_id),
        )
        return

    raw = op.get("fields")
    if not isinstance(raw, dict):
        raise EventError("malformed")
    if op.get("create") is True:
        if current is not None:
            raise EventError("uid_taken")
        checked = check_fields(raw)
        conn.execute(
            "INSERT INTO istota_events (user_id, uid, summary, description, location, dtstart,"
            " dtend, all_day, invite_status, created_by_task) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (task.user_id, uid, checked["summary"], checked["description"], checked["location"],
             checked["dtstart"], checked["dtend"], int(checked["all_day"]), feed_status, task.id),
        )
        return

    if op.get("create") is not False:
        raise EventError("malformed")
    if current is None:
        raise EventError("not_found")
    if current.status == "cancelled":
        raise EventError("cancelled")
    if set(raw) - set(_FIELDS):
        raise EventError("invalid_fields")
    merged = {
        "summary": current.summary, "description": current.description,
        "location": current.location, "dtstart": current.dtstart, "dtend": current.dtend,
        "all_day": current.all_day,
    }
    merged.update(raw)
    checked = check_fields(merged)
    conn.execute(
        "UPDATE istota_events SET summary = ?, description = ?, location = ?, dtstart = ?,"
        " dtend = ?, all_day = ?, sequence = sequence + 1,"
        " invite_status = COALESCE(?, invite_status), updated_at = datetime('now')"
        " WHERE id = ? AND user_id = ?",
        (checked["summary"], checked["description"], checked["location"], checked["dtstart"],
         checked["dtend"], int(checked["all_day"]), feed_status, current.id, task.user_id),
    )


def apply_ops(conn: sqlite3.Connection, config, task, ops: list) -> ApplyReport:
    """Apply a task's queued event ops in order, as the task's user, up to the cap.

    Each entry is applied or refused on its own, under a savepoint, so one bad
    entry leaves the others standing. Entries past `[sharing] events_per_task`
    are dropped, refused ones counted. Nothing here commits.
    """
    report = ApplyReport()
    cap = max(int(config.sharing.events_per_task), 0)
    entries = list(ops) if isinstance(ops, list) else []
    report.dropped = max(len(entries) - cap, 0)
    delivery = effective_delivery(config, _stored_delivery(conn, task.user_id))
    touched: list[str] = []
    created_here: set[str] = set()
    for index, op in enumerate(entries[:cap]):
        conn.execute("SAVEPOINT istota_event")
        try:
            _apply_one(conn, task, op, delivery)
        except EventError as exc:
            conn.execute("ROLLBACK TO istota_event")
            report.refused.append((index, exc.code))
        except sqlite3.IntegrityError:
            conn.execute("ROLLBACK TO istota_event")
            report.refused.append((index, "uid_taken"))
        else:
            report.applied += 1
            touched.append(op["uid"])
            if op.get("op") == OP_UPSERT and op.get("create") is True:
                created_here.add(op["uid"])
        finally:
            conn.execute("RELEASE istota_event")
    if delivery == "invite":
        for uid in dict.fromkeys(touched):
            current = get_event(conn, task.user_id, uid)
            # Created and cancelled in one task: the user never had it.
            if current is None or (current.status == "cancelled" and uid in created_here):
                continue
            report.invitations.append(uid)
    return report


# --- The feed --------------------------------------------------------------

def ical_time(value: str, all_day: bool):
    if all_day:
        return date.fromisoformat(value)
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def _stamp(value: str | None) -> datetime:
    """A row's SQLite ``datetime('now')`` stamp as a UTC datetime, for ``DTSTAMP``."""
    try:
        return datetime.strptime(value or "", "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return datetime(1970, 1, 1, tzinfo=timezone.utc)


def render_feed(conn: sqlite3.Connection, config, user_id: str) -> bytes:
    """Every confirmed event of the user as one ``VCALENDAR``; the same rows give the same bytes."""
    from icalendar import Calendar, Event

    cal = Calendar()
    cal.add("prodid", "-//Istota//istota calendar//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("x-wr-calname", config.bot_name or "Istota")
    for event in events_for_user(conn, user_id):
        item = Event()
        item.add("uid", event.uid)
        item.add("sequence", event.sequence)
        item.add("dtstamp", _stamp(event.updated_at))
        item.add("last-modified", _stamp(event.updated_at))
        item.add("dtstart", ical_time(event.dtstart, event.all_day))
        if event.dtend:
            item.add("dtend", ical_time(event.dtend, event.all_day))
        item.add("summary", event.summary)
        if event.location:
            item.add("location", event.location)
        if event.description:
            item.add("description", event.description)
        item.add("status", "CONFIRMED")
        cal.add_component(item)
    return cal.to_ical()


# --- Feed tokens -----------------------------------------------------------

def issue_token(conn: sqlite3.Connection, user_id: str) -> str:
    """A new feed token for the user, replacing any earlier one; the plaintext, once."""
    token, digest, _tail = tokens.new_token()
    conn.execute(
        "INSERT INTO calendar_feed_tokens (user_id, token_sha256) VALUES (?, ?)"
        " ON CONFLICT(user_id) DO UPDATE SET token_sha256 = excluded.token_sha256,"
        " created_at = datetime('now'), last_used_at = NULL",
        (user_id, digest),
    )
    return token


def revoke_token(conn: sqlite3.Connection, user_id: str) -> bool:
    cur = conn.execute("DELETE FROM calendar_feed_tokens WHERE user_id = ?", (user_id,))
    return cur.rowcount > 0


def token_info(conn: sqlite3.Connection, user_id: str) -> dict | None:
    """When the user's feed link was made and last read; never the token."""
    row = conn.execute(
        "SELECT created_at, last_used_at FROM calendar_feed_tokens WHERE user_id = ?", (user_id,),
    ).fetchone()
    if row is None:
        return None
    return {"created_at": row[0], "last_used_at": row[1]}


def well_formed_token(token: object) -> bool:
    return isinstance(token, str) and bool(_TOKEN_RE.fullmatch(token))


def user_for_token(conn: sqlite3.Connection, token: str, *, touch: bool = False) -> str | None:
    """The user a feed token belongs to, looked up by its digest; None for anything else.

    ``touch`` records the read, at most once an hour, so a polling calendar
    app does not write a row on every request.
    """
    if not well_formed_token(token):
        return None
    digest = tokens.hash_token(token)
    row = conn.execute(
        "SELECT user_id FROM calendar_feed_tokens WHERE token_sha256 = ?", (digest,),
    ).fetchone()
    if row is None:
        return None
    if touch:
        conn.execute(
            "UPDATE calendar_feed_tokens SET last_used_at = datetime('now')"
            " WHERE token_sha256 = ? AND (last_used_at IS NULL"
            " OR last_used_at < datetime('now', '-1 hour'))",
            (digest,),
        )
    return row[0]


def feed_url(config, token: str) -> str:
    """The address a calendar app subscribes to, on `[site] hostname`."""
    from istota.webui.origin import external_origin

    host, scheme = external_origin(config)
    return f"{scheme}://{host}{FEED_PATH}{token}.ics"


def feed_reachable_elsewhere(config) -> bool:
    """False when `[site] hostname` is a loopback name, so only this machine can read the feed."""
    from istota.webui.origin import external_origin

    try:
        _host, scheme = external_origin(config)
    except ValueError:
        return False
    return scheme == "https"


def delete_for_user(conn: sqlite3.Connection, user_id: str) -> None:
    """Remove the user's events, feed token and received invitations, in the caller's transaction."""
    conn.execute("DELETE FROM istota_events WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM calendar_feed_tokens WHERE user_id = ?", (user_id,))
    conn.execute("DELETE FROM received_invitations WHERE user_id = ?", (user_id,))
