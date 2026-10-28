"""Calendar operations: CalDAV calendars, and the ICS feeds a user shared with Istota.

A CalDAV calendar is named by its URL and read over the bot account's
credential. A feed is named ``feed:<id>``, is read-only, and is read from the
cache the scheduler keeps (`sharing.ics`), never the network: the skill reads
the framework database host-side, and every text field from a feed is fenced.
Istota's own calendar is named ``istota`` (`sharing.events`): read from the
database, and written by queuing a deferred op that the scheduler applies
once the task succeeds, at most `[sharing] events_per_task` per task.
``feed:invitations`` is the invitations other people mailed the user
(`sharing.itip`, ``received_invitations``): read-only, behind the same gate,
every text field and the organizer fenced.

Also provides a CLI for calendar operations from Claude Code:
    python -m istota.skills.calendar list [--calendar URL|feed:ID] [--date today|tomorrow|DATE | --week] [--tz TZ]
    python -m istota.skills.calendar calendars
    python -m istota.skills.calendar create --calendar URL --summary TEXT --start DATETIME [--end DATETIME]
    python -m istota.skills.calendar update --calendar URL --uid UID [--summary TEXT] [--start DT] [--end DT] [--all-day] ...
    python -m istota.skills.calendar delete --calendar URL --uid UID
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from istota.lib.dav_xml import principal_user
from istota.skills._cli import parse_and_resolve, run_skill_cli

try:
    import caldav
except ImportError:
    caldav = None

try:
    from icalendar import Calendar, Event
except ImportError:
    Calendar = None
    Event = None


@dataclass
class CalendarEvent:
    uid: str
    summary: str
    start: datetime
    end: datetime
    location: str | None = None
    description: str | None = None
    all_day: bool = False
    timezone: str | None = None  # Original TZID, None = floating
    classification: str = "PUBLIC"
    attendees: list[str] = field(default_factory=list)
    links: list[str] = field(default_factory=list)


def _require_caldav():
    if caldav is None:
        raise ImportError("caldav not installed. Install with: uv sync --extra calendar")


def get_caldav_client(url: str, username: str, password: str, *, timeout: int | None = None):
    """Create a CalDAV client. ``timeout`` (seconds) bounds each request."""
    _require_caldav()
    return caldav.DAVClient(url=url, username=username, password=password, timeout=timeout)


def list_calendars(client: caldav.DAVClient) -> list[tuple[str, str]]:
    """List available calendars. Returns list of (name, url) tuples."""
    principal = client.principal()
    calendars = principal.calendars()
    return [(cal.name, str(cal.url)) for cal in calendars]


_OWNER = "{DAV:}owner"
_PRIVILEGE_SET = "{DAV:}current-user-privilege-set"
_OC_READ_ONLY = "{http://owncloud.org/ns}read-only"
_WRITE_PRIVILEGES = ("{DAV:}write", "{DAV:}all")


def _access_props(calendar: caldav.Calendar) -> dict:
    """The calendar's owner, privilege set and ``oc:read-only``, as raw elements.

    caldav has no element for ``current-user-privilege-set``, and its parsed
    path logs a traceback on it, so this asks for unparsed elements instead.
    A calendar whose properties cannot be read gives ``{}``.
    """
    from caldav.elements.base import BaseElement

    class CurrentUserPrivilegeSet(BaseElement):
        tag = _PRIVILEGE_SET

    class ReadOnly(BaseElement):
        tag = _OC_READ_ONLY

    try:
        return calendar.get_properties(
            [caldav.dav.Owner(), CurrentUserPrivilegeSet(), ReadOnly()],
            parse_props=False,
        )
    except Exception:
        return {}


def get_calendar_owner(owner) -> str | None:
    """The user id in a ``DAV:owner`` element, or None when it names no user."""
    return principal_user(owner)


def _calendar_writable(props: dict) -> bool:
    read_only = props.get(_OC_READ_ONLY)
    if read_only is not None and (read_only.text or "").strip().lower() in {"true", "1"}:
        return False
    privileges = props.get(_PRIVILEGE_SET)
    if privileges is None:
        # No privilege set reported: keep the old answer so a server that
        # does not send one is not made read-only.
        return True
    # Only the whole `write` (or `all`) counts: a read-only Nextcloud share
    # still carries `write-properties`.
    for privilege in privileges.findall("{DAV:}privilege"):
        for granted in privilege:
            if granted.tag in _WRITE_PRIVILEGES:
                return True
    return False


def get_calendars_for_user(
    client: caldav.DAVClient,
    username: str,
) -> list[tuple[str, str, bool]]:
    """
    Get calendars owned by a specific user.

    Returns list of (name, url, writable) tuples for calendars where the
    DAV owner matches the given username. ``writable`` is what the
    connecting account may do with the calendar.
    """
    principal = client.principal()
    calendars = principal.calendars()

    result = []
    for cal in calendars:
        props = _access_props(cal)
        if get_calendar_owner(props.get(_OWNER)) != username:
            continue
        result.append((cal.name, str(cal.url), _calendar_writable(props)))

    return result


class CalendarNotFound(LookupError):
    """A calendar URL that is not one of the task user's own calendars."""


def owned_calendars(client: caldav.DAVClient, user_id: str) -> list[tuple[str, str]]:
    """The ``(name, url)`` pairs of the calendars ``user_id`` owns.

    The bot's account sees every calendar any user shared with it, so a skill
    CLI reads only this set (ISSUE-673). Same owner rule as prompt discovery.
    """
    return [(name, url) for name, url, _ in get_calendars_for_user(client, user_id)]


def _calendar_key(url: str) -> tuple[str, str]:
    parts = urlsplit(url.strip())
    return (parts.netloc.lower(), unquote(parts.path).rstrip("/"))


def resolve_owned_calendar(client: caldav.DAVClient, user_id: str, url: str) -> str:
    """Return the discovered URL of ``url`` if ``user_id`` owns it.

    Anything else, a calendar that does not exist included, is one refusal, so
    the answer does not say whether another user's calendar is there.
    """
    wanted_host, wanted_path = _calendar_key(url)
    for _, owned_url in owned_calendars(client, user_id):
        owned_host, owned_path = _calendar_key(owned_url)
        # A bare path names a calendar on the account's own server.
        if owned_path == wanted_path and wanted_host in ("", owned_host):
            return owned_url
    raise CalendarNotFound(f"Calendar not found: {url}")


def get_events(
    client: caldav.DAVClient,
    calendar_url: str,
    start: datetime,
    end: datetime,
) -> list[CalendarEvent]:
    """Get events from a calendar within a date range.

    When the query boundaries are timezone-aware, all-day events are
    post-filtered against the local date range to prevent UTC offset leakage
    (e.g. a Sunday all-day event appearing in a Saturday query because the
    UTC end boundary extends into the next day).
    """
    calendar = client.calendar(url=calendar_url)
    events = calendar.search(start=start, end=end, expand=True)

    # Derive local date boundaries for all-day event filtering.
    # All-day events use DATE (no time component), so we compare dates.
    # The query range [start, end) in local time defines which dates are valid.
    # For a single-day query like "tomorrow" (Feb 14 00:00 → Feb 15 00:00),
    # the valid all-day date range is [Feb 14, Feb 15) — i.e. only Feb 14.
    local_start_date = start.date() if isinstance(start, datetime) else start
    local_end_date = end.date() if isinstance(end, datetime) else end

    result = []
    for event in events:
        ical = Calendar.from_ical(event.data)
        for component in ical.walk():
            if component.name == "VEVENT":
                dtstart = component.get("dtstart")
                dtend = component.get("dtend")

                # Handle all-day events (date vs datetime)
                start_dt = dtstart.dt if dtstart else start
                end_dt = dtend.dt if dtend else start_dt

                all_day = not isinstance(start_dt, datetime)

                # Extract original timezone from DTSTART
                event_tz = None
                if not all_day and dtstart:
                    if hasattr(start_dt, "tzinfo") and start_dt.tzinfo is not None:
                        # Try TZID parameter first, then tzinfo name
                        tzid = dtstart.params.get("TZID") if hasattr(dtstart, "params") else None
                        if tzid:
                            event_tz = str(tzid)
                        elif hasattr(start_dt.tzinfo, "key"):
                            event_tz = start_dt.tzinfo.key
                        elif str(start_dt.tzinfo) == "UTC" or str(start_dt.tzinfo) == "utc":
                            event_tz = "UTC"
                        else:
                            event_tz = str(start_dt.tzinfo)

                if all_day:
                    # Filter out all-day events that leaked in due to
                    # UTC offset extending the CalDAV query window.
                    # An all-day event spans [DTSTART, DTEND) as dates.
                    # It overlaps our range if event_start < local_end_date
                    # AND event_end > local_start_date.
                    event_start_date = start_dt if isinstance(start_dt, date) else start_dt.date()
                    event_end_date = end_dt if isinstance(end_dt, date) else end_dt.date()
                    if event_start_date >= local_end_date or event_end_date <= local_start_date:
                        continue

                    start_dt = datetime.combine(start_dt, datetime.min.time())
                    end_dt = datetime.combine(end_dt, datetime.min.time())
                else:
                    # Strip tzinfo for consistent sorting with all-day events
                    if start_dt.tzinfo is not None:
                        start_dt = start_dt.replace(tzinfo=None)
                    if end_dt.tzinfo is not None:
                        end_dt = end_dt.replace(tzinfo=None)

                result.append(
                    CalendarEvent(
                        uid=str(component.get("uid", "")),
                        summary=str(component.get("summary", "Untitled")),
                        start=start_dt,
                        end=end_dt,
                        location=str(component.get("location", "")) or None,
                        description=str(component.get("description", "")) or None,
                        all_day=all_day,
                        timezone=event_tz,
                        classification=str(component.get("class", "PUBLIC")).upper(),
                        attendees=[str(value) for value in (
                            component.get("attendee", []) if isinstance(component.get("attendee", []), list)
                            else [component.get("attendee")]
                        )],
                        links=[str(component["url"])] if component.get("url") else [],
                    )
                )

    # Sort by start time
    result.sort(key=lambda e: e.start)
    return result


def _local_midnight(tz: str | None = None) -> datetime:
    """Get midnight today in the given timezone, as a timezone-aware datetime."""
    if tz:
        zone = ZoneInfo(tz)
        now = datetime.now(zone)
        return datetime(now.year, now.month, now.day, tzinfo=zone)
    else:
        now = datetime.now()
        return datetime(now.year, now.month, now.day)


def get_today_events(
    client: caldav.DAVClient, calendar_url: str, tz: str | None = None
) -> list[CalendarEvent]:
    """Get today's events. Pass tz (e.g., 'America/Los_Angeles') for correct local date."""
    today = _local_midnight(tz)
    tomorrow = today + timedelta(days=1)
    return get_events(client, calendar_url, today, tomorrow)


def get_tomorrow_events(
    client: caldav.DAVClient, calendar_url: str, tz: str | None = None
) -> list[CalendarEvent]:
    """Get tomorrow's events. Pass tz (e.g., 'America/Los_Angeles') for correct local date."""
    tomorrow = _local_midnight(tz) + timedelta(days=1)
    day_after = tomorrow + timedelta(days=1)
    return get_events(client, calendar_url, tomorrow, day_after)


def get_week_events(
    client: caldav.DAVClient, calendar_url: str, tz: str | None = None
) -> list[CalendarEvent]:
    """Get this week's events. Pass tz (e.g., 'America/Los_Angeles') for correct local date."""
    today = _local_midnight(tz)
    week_end = today + timedelta(days=7)
    return get_events(client, calendar_url, today, week_end)


def create_event(
    client: caldav.DAVClient,
    calendar_url: str,
    summary: str,
    start: date | datetime,
    end: date | datetime,
    location: str | None = None,
    description: str | None = None,
) -> str:
    """Create a new calendar event. Returns the event UID."""
    import uuid

    calendar = client.calendar(url=calendar_url)

    event = Event()
    event.add("uid", str(uuid.uuid4()))
    event.add("summary", summary)
    event.add("dtstart", start)
    event.add("dtend", end)
    event.add("dtstamp", datetime.now())

    if location:
        event.add("location", location)
    if description:
        event.add("description", description)

    cal = Calendar()
    cal.add("prodid", "-//Istota Bot//istota//EN")
    cal.add("version", "2.0")
    cal.add_component(event)

    calendar.save_event(cal.to_ical().decode("utf-8"))
    return str(event["uid"])


def delete_event(client: caldav.DAVClient, calendar_url: str, uid: str) -> bool:
    """Delete an event by UID. Returns True if deleted."""
    calendar = client.calendar(url=calendar_url)

    try:
        event = calendar.event_by_uid(uid)
        event.delete()
        return True
    except caldav.error.NotFoundError:
        return False


def get_event_by_uid(
    client: caldav.DAVClient,
    calendar_url: str,
    uid: str,
) -> CalendarEvent | None:
    """Get a single event by UID. Returns None if not found."""
    calendar = client.calendar(url=calendar_url)

    try:
        event = calendar.event_by_uid(uid)
        ical = Calendar.from_ical(event.data)

        for component in ical.walk():
            if component.name == "VEVENT":
                dtstart = component.get("dtstart")
                dtend = component.get("dtend")

                start_dt = dtstart.dt if dtstart else datetime.now()
                end_dt = dtend.dt if dtend else start_dt

                all_day = not isinstance(start_dt, datetime)

                # Extract original timezone
                event_tz = None
                if not all_day and dtstart:
                    if hasattr(start_dt, "tzinfo") and start_dt.tzinfo is not None:
                        tzid = dtstart.params.get("TZID") if hasattr(dtstart, "params") else None
                        if tzid:
                            event_tz = str(tzid)
                        elif hasattr(start_dt.tzinfo, "key"):
                            event_tz = start_dt.tzinfo.key
                        elif str(start_dt.tzinfo) in ("UTC", "utc"):
                            event_tz = "UTC"
                        else:
                            event_tz = str(start_dt.tzinfo)

                if all_day:
                    start_dt = datetime.combine(start_dt, datetime.min.time())
                    end_dt = datetime.combine(end_dt, datetime.min.time())

                return CalendarEvent(
                    uid=str(component.get("uid", "")),
                    summary=str(component.get("summary", "Untitled")),
                    start=start_dt,
                    end=end_dt,
                    location=str(component.get("location", "")) or None,
                    description=str(component.get("description", "")) or None,
                    all_day=all_day,
                    timezone=event_tz,
                )
        return None
    except caldav.error.NotFoundError:
        return None


def update_event(
    client: caldav.DAVClient,
    calendar_url: str,
    uid: str,
    summary: str | None = None,
    start: date | datetime | None = None,
    end: date | datetime | None = None,
    location: str | None = None,
    description: str | None = None,
) -> bool:
    """Update an existing event. Only provided fields are changed.

    Returns False if event not found.
    Raises caldav.error.AuthorizationError if calendar is read-only.
    """
    calendar = client.calendar(url=calendar_url)

    try:
        event = calendar.event_by_uid(uid)
    except caldav.error.NotFoundError:
        return False

    ical = Calendar.from_ical(event.data)

    for component in ical.walk():
        if component.name == "VEVENT":
            if summary is not None:
                component["summary"] = summary
            if start is not None:
                component.pop("dtstart", None)
                component.add("dtstart", start)
            if end is not None:
                component.pop("dtend", None)
                component.add("dtend", end)
            if location is not None:
                if location:
                    component["location"] = location
                elif "location" in component:
                    del component["location"]
            if description is not None:
                if description:
                    component["description"] = description
                elif "description" in component:
                    del component["description"]
            break

    event.data = ical.to_ical().decode("utf-8")
    event.save()
    return True


def format_event_for_display(event: CalendarEvent) -> str:
    """Format an event for human-readable display."""
    if event.all_day:
        time_str = "All day"
    else:
        time_str = f"{event.start.strftime('%H:%M')} - {event.end.strftime('%H:%M')}"

    parts = [f"{time_str}: {event.summary}"]
    if event.location:
        parts.append(f"  Location: {event.location}")

    return "\n".join(parts)


def format_day_schedule(events: list[CalendarEvent], date_label: str = "Today") -> str:
    """Format a day's events for display."""
    if not events:
        return f"{date_label}: No events scheduled"

    lines = [f"{date_label}'s schedule:"]
    for event in events:
        lines.append(f"  • {format_event_for_display(event)}")

    return "\n".join(lines)


# =============================================================================
# CLI Interface
# =============================================================================


def _get_client_from_env() -> caldav.DAVClient:
    """Create CalDAV client from environment variables."""
    url = os.environ.get("CALDAV_URL")
    username = os.environ.get("CALDAV_USERNAME")
    password = os.environ.get("CALDAV_PASSWORD")

    if not all([url, username, password]):
        raise ValueError(
            "Missing CalDAV credentials. Required env vars: "
            "CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD"
        )

    return get_caldav_client(url, username, password)


def task_user_id() -> str:
    """The task's user, from the proxy's env; refuses rather than widening.

    The CalDAV credential is the bot's, so without a user every calendar
    shared with the bot would be in reach.
    """
    user_id = os.environ.get("ISTOTA_USER_ID", "").strip()
    if not user_id:
        raise ValueError("ISTOTA_USER_ID is not set; refusing to read calendars without a user")
    return user_id


def _event_to_dict(event: CalendarEvent) -> dict:
    """Convert CalendarEvent to JSON-serializable dict."""
    d = {
        "uid": event.uid,
        "summary": event.summary,
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "location": event.location,
        "description": event.description,
        "all_day": event.all_day,
    }
    if event.timezone:
        d["timezone"] = event.timezone
    else:
        d["timezone"] = "floating"
    return d


def _parse_date(date_str: str, tz: str | None = None) -> datetime:
    """Parse a date string into a datetime at midnight, timezone-aware if tz provided."""
    zone = ZoneInfo(tz) if tz else None

    if date_str.lower() == "today":
        if zone:
            now = datetime.now(zone)
            return datetime(now.year, now.month, now.day, tzinfo=zone)
        else:
            now = datetime.now()
            return datetime(now.year, now.month, now.day)
    elif date_str.lower() == "tomorrow":
        if zone:
            now = datetime.now(zone)
            tomorrow = now + timedelta(days=1)
            return datetime(tomorrow.year, tomorrow.month, tomorrow.day, tzinfo=zone)
        else:
            now = datetime.now()
            tomorrow = now + timedelta(days=1)
            return datetime(tomorrow.year, tomorrow.month, tomorrow.day)
    else:
        # Parse YYYY-MM-DD format
        dt = datetime.strptime(date_str, "%Y-%m-%d")
        if zone:
            return dt.replace(tzinfo=zone)
        return dt


def _parse_datetime(dt_str: str) -> datetime:
    """Parse datetime string in various formats."""
    formats = [
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ]
    for fmt in formats:
        try:
            return datetime.strptime(dt_str, fmt)
        except ValueError:
            continue
    raise ValueError(f"Cannot parse datetime: {dt_str}. Use format: YYYY-MM-DD HH:MM")


def _parse_all_day_date(date_str: str) -> date:
    """Parse the date value used by an all-day event."""
    try:
        return datetime.strptime(date_str, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(
            f"Cannot parse all-day date: {date_str}. Use format: YYYY-MM-DD"
        ) from exc


def _get_date_range(args) -> tuple[datetime, datetime, str]:
    """Compute (start, end, label) from --week or --date flags."""
    if getattr(args, "week", False):
        start = _parse_date("today", args.tz)
        end = start + timedelta(days=7)
        return start, end, "week"
    start = _parse_date(args.date, args.tz)
    end = start + timedelta(days=1)
    return start, end, args.date


FEED_PREFIX = "feed:"
FEED_LABEL = "calendar feed"


def is_feed_calendar(calendar: str | None) -> bool:
    """Whether ``--calendar`` names a feed rather than a CalDAV URL; no URL starts so."""
    return bool(calendar) and calendar.strip().lower().startswith(FEED_PREFIX)


@dataclass
class SharedFeed:
    calendar: str
    host: str
    name: str | None
    state: str
    last_reason: str | None
    fetched_at: str | None
    body: bytes | None


def _caldav_configured() -> bool:
    return all(os.environ.get(var) for var in ("CALDAV_URL", "CALDAV_USERNAME", "CALDAV_PASSWORD"))


def _withheld_from_env() -> set[str]:
    from istota.sandbox.host_paths import WITHHELD_SCOPES_VAR

    raw = os.environ.get(WITHHELD_SCOPES_VAR, "")
    return {part.strip() for part in raw.split(",") if part.strip()}


@dataclass
class _TaskContext:
    config: object
    db_path: object
    task_id: int
    skill_index: dict


def _task_context() -> _TaskContext | None:
    """The calling task's config and database, or None where only CalDAV can be in reach.

    None with no task (an operator's shell), with ``calendar`` in the
    withheld scopes, or with no database. Each caller then reads the task's
    own row and checks it is this user's (`_own_task`).
    """
    try:
        task_id = int(os.environ.get("ISTOTA_TASK_ID", "") or 0)
    except ValueError:
        task_id = 0
    if task_id <= 0 or "calendar" in _withheld_from_env():
        return None

    from pathlib import Path

    from istota.config import load_config
    from istota.skills._loader import load_skill_index

    config = load_config()
    db_path = Path(os.environ.get("ISTOTA_DB_PATH") or config.db_path)
    if not db_path.exists():
        return None
    skill_index = load_skill_index(config.skills_dir, bundled_dir=config.bundled_skills_dir)
    return _TaskContext(config=config, db_path=db_path, task_id=task_id, skill_index=skill_index)


def _own_task(conn, context: _TaskContext, user_id: str):
    from istota import db

    task = db.get_task(conn, context.task_id)
    return task if task is not None and task.user_id == user_id else None


def shared_feeds(user_id: str) -> list[SharedFeed]:
    """The feeds the calling task may read, with their cached bodies.

    Through `sharing.sources.sources_for_task`, on the task's own row, so a
    guest's turn and a task nobody asked in a shared room read none. No task
    (an operator's shell) or a row for another user reads none either.
    """
    context = _task_context()
    if context is None:
        return []

    from istota import db
    from istota.sharing import ics, sources

    with db.get_db(context.db_path) as conn:
        task = _own_task(conn, context, user_id)
        if task is None:
            return []
        found = sources.sources_for_task(conn, task, kind="ics_feed", skill_index=context.skill_index)
        return [
            SharedFeed(
                calendar=f"{FEED_PREFIX}{source.id}", host=source.remote_host,
                name=source.extras.get("name"), state=source.state,
                last_reason=source.last_reason,
                fetched_at=ics.cache_times(conn, source.id),
                body=ics.read_cached(conn, source.id),
            )
            for source in found
        ]


def _feed_event_dict(event, calendar: str) -> dict:
    """A feed occurrence in `_event_to_dict`'s shape, its provider's text fenced."""
    from istota.lib.untrusted import frame_untrusted

    return {
        "calendar": calendar,
        "uid": event.uid,
        "summary": frame_untrusted(event.summary, FEED_LABEL),
        "start": event.start.isoformat(),
        "end": event.end.isoformat(),
        "location": frame_untrusted(event.location, FEED_LABEL) or None,
        "description": frame_untrusted(event.description, FEED_LABEL) or None,
        "all_day": event.all_day,
        "timezone": event.timezone or "floating",
        "read_only": True,
    }


def _feed_events(feeds: list[SharedFeed], start: datetime, end: datetime) -> tuple[list[dict], dict]:
    """Every feed's occurrences in the window, against one shared cap."""
    from istota.sharing import ics

    events: list[dict] = []
    notes: dict = {}
    budget = ics.MAX_OCCURRENCES
    truncated = []
    for feed in feeds:
        if feed.state == "error" or feed.body is None:
            notes.setdefault("feed_errors", []).append({
                "calendar": feed.calendar, "host": feed.host,
                "reason": feed.last_reason or "not_fetched", "cached_at": feed.fetched_at,
            })
        if feed.body is None:
            continue
        found, cut = ics.expand(feed.body, start, end, limit=max(budget, 0))
        budget -= len(found)
        if cut:
            truncated.append(feed.calendar)
        events.extend(_feed_event_dict(event, feed.calendar) for event in found)
    if truncated:
        notes["truncated"] = True
        notes["truncated_calendars"] = truncated
        notes["occurrence_limit"] = ics.MAX_OCCURRENCES
    return events, notes


def _refuse_feed_write(calendar: str) -> dict | None:
    if is_feed_calendar(calendar):
        return {
            "status": "error", "reason": "read_only_calendar",
            "error": f"{calendar} is a calendar shared with Istota by its feed address; it is read-only",
        }
    return None


ISTOTA_CALENDAR = "istota"


def is_istota_calendar(calendar: str | None) -> bool:
    return bool(calendar) and calendar.strip().lower() == ISTOTA_CALENDAR


def _istota_refusal(reason: str, error: str) -> dict:
    return {"status": "error", "reason": reason, "error": error}


def _istota_reach(user_id: str):
    """``(context, task)`` when this task may use Istota's calendar, else None.

    The feeds' gate: the ``calendar`` scope, on the task's own row.
    """
    context = _task_context()
    if context is None or "calendar" not in context.skill_index:
        return None

    from istota import db
    from istota.rooms import scopes as room_scopes

    with db.get_db(context.db_path) as conn:
        task = _own_task(conn, context, user_id)
        if task is None:
            return None
        if "calendar" in room_scopes.withheld_for_task(conn, task, skill_index=context.skill_index):
            return None
    return context, task


_UTC = ZoneInfo("UTC")
_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _shown_time(value: str, all_day: bool, tz: str | None) -> datetime:
    """A stored time as the listing shows it: a date at midnight, or the moment in ``tz`` (UTC without)."""
    if all_day:
        return datetime.combine(date.fromisoformat(value), datetime.min.time())
    moment = datetime.strptime(value, _UTC_FORMAT).replace(tzinfo=_UTC)
    if tz:
        moment = moment.astimezone(ZoneInfo(tz))
    return moment.replace(tzinfo=None)


def istota_events(user_id: str, start: datetime, end: datetime, tz: str | None) -> list[dict] | None:
    """Istota's own calendar's events in the window, or None where the calendar is not in reach."""
    reach = _istota_reach(user_id)
    if reach is None:
        return None
    context, _task = reach

    from istota import db
    from istota.sharing import events as istota_cal

    window_start = start.astimezone(_UTC)
    window_end = end.astimezone(_UTC)
    first_day = start.date()
    last_day = end.date()
    with db.get_db(context.db_path) as conn:
        rows = istota_cal.events_for_user(conn, user_id)
    shown = []
    for event in rows:
        ends_at = event.dtend or event.dtstart
        if event.all_day:
            overlaps = (date.fromisoformat(event.dtstart) < last_day
                        and date.fromisoformat(ends_at) > first_day)
        else:
            begins = datetime.strptime(event.dtstart, _UTC_FORMAT).replace(tzinfo=_UTC)
            ends = datetime.strptime(ends_at, _UTC_FORMAT).replace(tzinfo=_UTC)
            overlaps = begins < window_end and ends > window_start
        if not overlaps:
            continue
        shown.append({
            "calendar": ISTOTA_CALENDAR,
            "uid": event.uid,
            "summary": event.summary,
            "start": _shown_time(event.dtstart, event.all_day, tz).isoformat(),
            "end": _shown_time(ends_at, event.all_day, tz).isoformat(),
            "location": event.location,
            "description": event.description,
            "all_day": event.all_day,
            "timezone": "floating" if event.all_day else (tz or "UTC"),
        })
    return shown


INVITATIONS_CALENDAR = "feed:invitations"


def is_invitations_calendar(calendar: str | None) -> bool:
    return bool(calendar) and calendar.strip().lower() == INVITATIONS_CALENDAR


def _shown_received_time(value: str, all_day: bool, tz: str | None) -> datetime:
    """A received time for the listing: a date, a UTC moment in ``tz``, or floating as written."""
    if all_day:
        return datetime.combine(date.fromisoformat(value), datetime.min.time())
    if value.endswith("Z"):
        return _shown_time(value, False, tz)
    return datetime.fromisoformat(value).replace(tzinfo=None)


def received_invitations(user_id: str, start: datetime, end: datetime, tz: str | None) -> list[dict] | None:
    """Invitations other people sent the user (`sharing.itip`), in the window, fenced.

    None where the calendar scope is not in reach, the gate Istota's own
    calendar uses. Every text field and the organizer are third-party text.
    """
    reach = _istota_reach(user_id)
    if reach is None:
        return None
    context, _task = reach

    from istota import db
    from istota.lib.untrusted import frame_untrusted
    from istota.sharing import itip

    def naive(moment: datetime) -> datetime:
        if moment.tzinfo is None:
            return moment
        return moment.astimezone(ZoneInfo(tz) if tz else _UTC).replace(tzinfo=None)

    window_start, window_end = naive(start), naive(end)
    with db.get_db(context.db_path) as conn:
        rows = itip.invitations_for_user(conn, user_id)
    shown = []
    for row in rows:
        if not row.dtstart:
            continue
        try:
            begins = _shown_received_time(row.dtstart, row.all_day, tz)
            ends = _shown_received_time(row.dtend or row.dtstart, row.all_day, tz)
        except ValueError:
            continue
        if begins >= window_end or (ends <= window_start and begins < window_start):
            continue
        shown.append({
            "calendar": INVITATIONS_CALENDAR,
            "uid": row.uid,
            "summary": frame_untrusted(row.summary or "", FEED_LABEL),
            "start": begins.isoformat(),
            "end": ends.isoformat(),
            "location": frame_untrusted(row.location, FEED_LABEL) or None,
            "description": frame_untrusted(row.description, FEED_LABEL) or None,
            "organizer": frame_untrusted(row.organizer, FEED_LABEL),
            "all_day": row.all_day,
            "timezone": "floating" if row.all_day or not row.dtstart.endswith("Z") else (tz or "UTC"),
            "read_only": True,
        })
    return shown


def _op_file(context: _TaskContext):
    from pathlib import Path

    from istota.sharing import events as istota_cal

    deferred = os.environ.get("ISTOTA_DEFERRED_DIR", "")
    if not deferred:
        return None
    return Path(deferred) / f"task_{context.task_id}_{istota_cal.DEFERRED_SUFFIX}.json"


def _read_ops(path) -> list:
    from istota.sandbox import deferred

    try:
        ops = deferred.read_json(path)
    except (ValueError, UnicodeDecodeError, OSError, RecursionError):
        return []
    return ops if isinstance(ops, list) else []


def _known_istota_event(conn, user_id: str, uid: str, ops: list) -> dict | None:
    """An event's fields as this task leaves them: the stored row, then this task's queued ops."""
    from istota.sharing import events as istota_cal

    stored = istota_cal.get_event(conn, user_id, uid)
    state = None
    if stored is not None and stored.status == "confirmed":
        state = {
            "summary": stored.summary, "description": stored.description,
            "location": stored.location, "dtstart": stored.dtstart, "dtend": stored.dtend,
            "all_day": stored.all_day,
        }
    for op in ops:
        if not isinstance(op, dict) or op.get("uid") != uid:
            continue
        if op.get("op") == istota_cal.OP_CANCEL:
            state = None
        elif op.get("op") == istota_cal.OP_UPSERT and isinstance(op.get("fields"), dict):
            if op.get("create") is True:
                state = dict(op["fields"])
            elif state is not None:
                state.update(op["fields"])
    return state


def _queue_istota_op(user_id: str, build) -> dict:
    """Append one op to this task's ``istota_events`` file, within the cap.

    ``build(conn, ops, config)`` returns the op or a refusal. The scheduler
    applies the file after the task succeeds and counts the cap again there.
    """
    from istota import db

    reach = _istota_reach(user_id)
    if reach is None:
        return _istota_refusal("source_not_in_scope", "Istota's calendar is not available to this task")
    context, _task = reach
    path = _op_file(context)
    if path is None:
        return _istota_refusal("not_in_task", "Istota's calendar is written only from a task")
    cap = max(int(context.config.sharing.events_per_task), 0)
    ops = _read_ops(path)
    if len(ops) >= cap:
        return _istota_refusal(
            "event_limit", f"This task has reached its limit of {cap} changes to Istota's calendar",
        )
    with db.get_db(context.db_path) as conn:
        op = build(conn, ops, context.config)
    if "status" in op:
        return op
    ops.append(op)
    from istota.sandbox import deferred
    deferred.write_json(path, ops)
    return {"status": "ok", "calendar": ISTOTA_CALENDAR, "uid": op["uid"], "queued": True,
            "note": "Saved to Istota's calendar when this task finishes."}


def _istota_time(value: str, all_day: bool, tz: str | None, *, end: bool = False) -> str:
    """A CLI time as the store keeps it: a date, or UTC. An all-day end is inclusive on the CLI."""
    if all_day:
        day = _parse_all_day_date(value)
        return (day + timedelta(days=1)).isoformat() if end else day.isoformat()
    moment = _parse_datetime(value).replace(tzinfo=ZoneInfo(tz))
    return moment.astimezone(_UTC).strftime(_UTC_FORMAT)


def _istota_create(args, user_id: str) -> dict:
    from istota.sharing import events as istota_cal

    if not args.all_day and args.end is None:
        raise ValueError("Timed events require --end")
    if not args.all_day and not args.tz:
        return _istota_refusal("tz_required", "Pass --tz for a timed event on Istota's calendar")
    fields = {
        "summary": args.summary,
        "description": args.description or None,
        "location": args.location or None,
        "dtstart": _istota_time(args.start, args.all_day, args.tz),
        "dtend": _istota_time(args.end or args.start, args.all_day, args.tz, end=True),
        "all_day": bool(args.all_day),
    }
    try:
        istota_cal.check_fields(fields)
    except istota_cal.EventError as exc:
        return _istota_refusal(exc.code, "That event cannot be put on Istota's calendar")

    def build(conn, ops, config):
        return istota_cal.upsert_op(istota_cal.new_uid(config.site.hostname), create=True, fields=fields)

    result = _queue_istota_op(user_id, build)
    if result["status"] == "ok":
        result.update({"summary": args.summary, "start": fields["dtstart"], "end": fields["dtend"],
                       "all_day": bool(args.all_day)})
    return result


def _istota_update(args, user_id: str) -> dict:
    from istota.sharing import events as istota_cal

    changes: dict = {}
    if args.summary is not None:
        changes["summary"] = args.summary
    if args.all_day and (args.start is not None or args.end is not None):
        if args.start is None or args.end is None:
            raise ValueError("An all-day update requires both --start and --end")
        changes.update(dtstart=_istota_time(args.start, True, None),
                       dtend=_istota_time(args.end, True, None, end=True), all_day=True)
    elif args.start is not None or args.end is not None:
        if not args.tz:
            return _istota_refusal("tz_required", "Pass --tz for a timed event on Istota's calendar")
        if args.start is not None:
            changes["dtstart"] = _istota_time(args.start, False, args.tz)
        if args.end is not None:
            changes["dtend"] = _istota_time(args.end, False, args.tz)
        changes["all_day"] = False
    if args.clear_location:
        changes["location"] = None
    elif args.location is not None:
        changes["location"] = args.location
    if args.clear_description:
        changes["description"] = None
    elif args.description is not None:
        changes["description"] = args.description
    if not changes:
        return {"status": "error", "error": "No fields to update. Provide at least one of --summary, --start, --end, --location, --description, --clear-location, --clear-description."}

    def build(conn, ops, config):
        current = _known_istota_event(conn, user_id, args.uid, ops)
        if current is None:
            return _istota_refusal("event_not_found", f"Event not found: {args.uid}")
        try:
            istota_cal.check_fields({**current, **changes})
        except istota_cal.EventError as exc:
            return _istota_refusal(exc.code, "That change cannot be made to the event")
        return istota_cal.upsert_op(args.uid, create=False, fields=changes)

    result = _queue_istota_op(user_id, build)
    if result["status"] == "ok":
        result["updated_fields"] = list(changes)
    return result


def _istota_delete(args, user_id: str) -> dict:
    from istota.sharing import events as istota_cal

    def build(conn, ops, config):
        if _known_istota_event(conn, user_id, args.uid, ops) is None:
            return _istota_refusal("event_not_found", f"Event not found: {args.uid}")
        return istota_cal.cancel_op(args.uid)

    result = _queue_istota_op(user_id, build)
    if result["status"] == "ok":
        result["deleted"] = True
    return result


def cmd_list(args) -> dict:
    """List calendar events."""
    start, end, label = _get_date_range(args)
    user_id = task_user_id()

    if args.calendar and is_istota_calendar(args.calendar):
        found = istota_events(user_id, start, end, args.tz)
        if found is None:
            raise CalendarNotFound(f"Calendar not found: {args.calendar}")
        found.sort(key=lambda e: e["start"])
        return {"status": "ok", "calendar": ISTOTA_CALENDAR, "date": label,
                "event_count": len(found), "events": found}

    if args.calendar and is_invitations_calendar(args.calendar):
        found = received_invitations(user_id, start, end, args.tz)
        if found is None:
            raise CalendarNotFound(f"Calendar not found: {args.calendar}")
        found.sort(key=lambda e: e["start"])
        return {"status": "ok", "calendar": INVITATIONS_CALENDAR, "date": label,
                "event_count": len(found), "events": found}

    if args.calendar and is_feed_calendar(args.calendar):
        wanted = args.calendar.strip().lower()
        feeds = [feed for feed in shared_feeds(user_id) if feed.calendar == wanted]
        if not feeds:
            raise CalendarNotFound(f"Calendar not found: {args.calendar}")
        events, notes = _feed_events(feeds, start, end)
        events.sort(key=lambda e: e["start"])
        return {
            "status": "ok", "calendar": wanted, "date": label,
            "event_count": len(events), "events": events, **notes,
        }

    feeds = [] if args.calendar else shared_feeds(user_id)
    own = None if args.calendar else istota_events(user_id, start, end, args.tz)
    received = None if args.calendar else received_invitations(user_id, start, end, args.tz)
    if own is not None:
        own = own + (received or [])
    if (feeds or own is not None) and not _caldav_configured():
        # No CalDAV in this task: the bot account's credential is not here,
        # and nothing here asks for it. Feeds, Istota's own calendar and the
        # invitations it was sent only.
        events, notes = _feed_events(feeds, start, end)
        events.extend(own or [])
        events.sort(key=lambda e: e["start"])
        return {"status": "ok", "date": label, "event_count": len(events), "events": events, **notes}

    # ISSUE-101: DAVClient owns urllib3 pools whose watchdog threads
    # leak unless close() is called.
    with _get_client_from_env() as client:
        if not args.calendar:
            all_events = []
            for name, url in owned_calendars(client, user_id):
                try:
                    events = get_events(client, url, start, end)
                    for e in events:
                        all_events.append({
                            "calendar": name,
                            **_event_to_dict(e),
                        })
                except Exception:
                    continue  # Skip calendars we can't read

            feed_events, notes = _feed_events(feeds, start, end)
            all_events.extend(feed_events)
            all_events.extend(own or [])
            all_events.sort(key=lambda e: e["start"])
            return {
                "status": "ok",
                "date": label,
                "event_count": len(all_events),
                "events": all_events,
                **notes,
            }

        calendar_url = resolve_owned_calendar(client, user_id, args.calendar)
        events = get_events(client, calendar_url, start, end)

        return {
            "status": "ok",
            "calendar": args.calendar,
            "date": label,
            "event_count": len(events),
            "events": [_event_to_dict(e) for e in events],
        }


def cmd_calendars(args) -> dict:
    """The calendars this task can read: CalDAV ones, then feeds."""
    from istota.lib.untrusted import frame_untrusted

    user_id = task_user_id()
    calendars: list[dict] = []
    if _caldav_configured():
        # ISSUE-101: close DAVClient to drop the urllib3 watchdog thread.
        with _get_client_from_env() as client:
            for name, url, writable in get_calendars_for_user(client, user_id):
                calendars.append({
                    "calendar": url, "kind": "caldav", "name": name, "writable": writable,
                })
    for feed in shared_feeds(user_id):
        calendars.append({
            "calendar": feed.calendar, "kind": "feed", "host": feed.host,
            "name": frame_untrusted(feed.name, FEED_LABEL) or None,
            "writable": False, "state": feed.state, "last_reason": feed.last_reason,
            "cached_at": feed.fetched_at,
        })
    reach = _istota_reach(user_id)
    if reach is not None:
        config = reach[0].config
        calendars.append({
            "calendar": ISTOTA_CALENDAR, "kind": "istota",
            "name": f"{config.bot_name}'s calendar",
            "writable": config.sharing.events_per_task > 0,
        })
        calendars.append({
            "calendar": INVITATIONS_CALENDAR, "kind": "invitations",
            "name": "Invitations sent to you by mail",
            "writable": False,
        })
    return {"status": "ok", "calendar_count": len(calendars), "calendars": calendars}


def cmd_create(args) -> dict:
    """Create a calendar event."""
    refusal = _refuse_feed_write(args.calendar)
    if refusal:
        return refusal
    if is_istota_calendar(args.calendar):
        return _istota_create(args, task_user_id())
    if args.all_day:
        start = _parse_all_day_date(args.start)
        inclusive_end = _parse_all_day_date(args.end) if args.end else start
        if inclusive_end < start:
            raise ValueError("All-day end date cannot be before start date")
        end = inclusive_end + timedelta(days=1)
    else:
        if args.end is None:
            raise ValueError("Timed events require --end")
        start = _parse_datetime(args.start)
        end = _parse_datetime(args.end)

    if args.tz and not args.all_day:
        tz = ZoneInfo(args.tz)
        start = start.replace(tzinfo=tz)
        end = end.replace(tzinfo=tz)

    user_id = task_user_id()
    # ISSUE-101: close DAVClient to drop the urllib3 watchdog thread.
    with _get_client_from_env() as client:
        uid = create_event(
            client,
            resolve_owned_calendar(client, user_id, args.calendar),
            summary=args.summary,
            start=start,
            end=end,
            location=args.location,
            description=args.description,
        )

    return {
        "status": "ok",
        "uid": uid,
        "summary": args.summary,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "all_day": args.all_day,
    }


def cmd_delete(args) -> dict:
    """Delete a calendar event."""
    refusal = _refuse_feed_write(args.calendar)
    if refusal:
        return refusal
    if is_istota_calendar(args.calendar):
        return _istota_delete(args, task_user_id())
    user_id = task_user_id()
    # ISSUE-101: close DAVClient to drop the urllib3 watchdog thread.
    with _get_client_from_env() as client:
        calendar_url = resolve_owned_calendar(client, user_id, args.calendar)
        deleted = delete_event(client, calendar_url, args.uid)

    if deleted:
        return {"status": "ok", "uid": args.uid, "deleted": True}
    else:
        return {"status": "error", "error": f"Event not found: {args.uid}"}


def cmd_update(args) -> dict:
    """Update a calendar event."""
    refusal = _refuse_feed_write(args.calendar)
    if refusal:
        return refusal
    if is_istota_calendar(args.calendar):
        return _istota_update(args, task_user_id())
    kwargs: dict = {}
    if args.summary is not None:
        kwargs["summary"] = args.summary

    if args.all_day and (args.start is not None or args.end is not None):
        if args.start is None or args.end is None:
            raise ValueError("An all-day update requires both --start and --end")
        start = _parse_all_day_date(args.start)
        inclusive_end = _parse_all_day_date(args.end)
        if inclusive_end < start:
            raise ValueError("All-day end date cannot be before start date")
        kwargs["start"] = start
        kwargs["end"] = inclusive_end + timedelta(days=1)
    elif args.start is not None:
        dt = _parse_datetime(args.start)
        if args.tz:
            dt = dt.replace(tzinfo=ZoneInfo(args.tz))
        kwargs["start"] = dt
    if not args.all_day and args.end is not None:
        dt = _parse_datetime(args.end)
        if args.tz:
            dt = dt.replace(tzinfo=ZoneInfo(args.tz))
        kwargs["end"] = dt

    # --clear-location passes "" to delete the property; --location sets it
    if args.clear_location:
        kwargs["location"] = ""
    elif args.location is not None:
        kwargs["location"] = args.location

    if args.clear_description:
        kwargs["description"] = ""
    elif args.description is not None:
        kwargs["description"] = args.description

    if not kwargs:
        return {"status": "error", "error": "No fields to update. Provide at least one of --summary, --start, --end, --location, --description, --clear-location, --clear-description."}

    user_id = task_user_id()
    # ISSUE-101: close DAVClient to drop the urllib3 watchdog thread.
    with _get_client_from_env() as client:
        calendar_url = resolve_owned_calendar(client, user_id, args.calendar)
        updated = update_event(client, calendar_url, args.uid, **kwargs)

    if updated:
        return {"status": "ok", "uid": args.uid, "updated_fields": list(kwargs.keys())}
    else:
        return {"status": "error", "error": f"Event not found: {args.uid}"}


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m istota.skills.calendar",
        description="Calendar operations CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # list
    p_list = sub.add_parser("list", aliases=["agenda"], help="List calendar events")
    p_list.add_argument(
        "--calendar", "-c",
        help="Calendar URL, feed:ID or istota, as `calendars` lists them (omit to query all)",
    )
    date_group = p_list.add_mutually_exclusive_group()
    date_group.add_argument(
        "--date", "-d", default="today",
        help="Date to query: 'today', 'tomorrow', or YYYY-MM-DD (default: today)"
    )
    date_group.add_argument(
        "--week", "-w", action="store_true", default=False,
        help="Show events for the next 7 days"
    )
    p_list.add_argument("--tz", help="Timezone (e.g., America/Los_Angeles)")

    sub.add_parser("calendars", help="List the calendars this task can read")

    # create
    p_create = sub.add_parser("create", help="Create a calendar event")
    p_create.add_argument("--calendar", "-c", required=True, help="Calendar URL, or istota")
    p_create.add_argument("--summary", "-s", required=True, help="Event title")
    p_create.add_argument(
        "--start", required=True,
        help="Start time, or date with --all-day"
    )
    p_create.add_argument(
        "--end",
        help="End time, or inclusive end date with --all-day (defaults to start date)"
    )
    p_create.add_argument("--all-day", action="store_true", help="Create an all-day event using YYYY-MM-DD dates")
    p_create.add_argument("--location", "-l", help="Event location")
    p_create.add_argument("--description", help="Event description")
    p_create.add_argument("--tz", help="Timezone for start/end times (e.g., America/Los_Angeles)")

    # delete
    p_delete = sub.add_parser("delete", help="Delete a calendar event")
    p_delete.add_argument("--calendar", "-c", required=True, help="Calendar URL, or istota")
    p_delete.add_argument("--uid", required=True, help="Event UID to delete")

    # update
    p_update = sub.add_parser("update", help="Update a calendar event")
    p_update.add_argument("--calendar", "-c", required=True, help="Calendar URL, or istota")
    p_update.add_argument("--uid", required=True, help="Event UID to update")
    p_update.add_argument("--summary", "-s", help="New event title")
    p_update.add_argument("--start", help="New start time (YYYY-MM-DD HH:MM)")
    p_update.add_argument("--end", help="New end time (YYYY-MM-DD HH:MM)")
    p_update.add_argument("--all-day", action="store_true", help="Treat start/end as YYYY-MM-DD dates; end is inclusive")
    p_update.add_argument("--location", "-l", help="New event location")
    p_update.add_argument("--description", help="New event description")
    p_update.add_argument("--tz", help="Timezone for start/end times (e.g., America/Los_Angeles)")
    p_update.add_argument("--clear-location", action="store_true", default=False, help="Remove location")
    p_update.add_argument("--clear-description", action="store_true", default=False, help="Remove description")

    return parser


def main(argv=None):
    parser = build_parser()
    args = parse_and_resolve(parser, argv)

    commands = {
        "list": cmd_list,
        "agenda": cmd_list,  # alias — natural name an LLM reaches for
        "calendars": cmd_calendars,
        "create": cmd_create,
        "delete": cmd_delete,
        "update": cmd_update,
    }

    run_skill_cli(commands, args)


if __name__ == "__main__":
    main()
