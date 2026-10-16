"""Host-side reads of exactly one approved resource; no personal source context."""

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from istota.briefings.models import normalize_contribution_config
from istota.groups.policy import GroupError
from istota.skills._loader import open_overlay_dir, read_overlay_bytes

FILE_MAX_BYTES = 64 * 1024


def _read_file(config, resource):
    root = config.workspace_root(resource["owner_id"])
    return read_text_file(root, resource["path"].split("/"))


def read_text_file(root, parts):
    """Read one text leaf through Workspace's pinned directory primitives."""
    if root is None:
        return {"status": "refused", "error_code": "file_root_unavailable"}
    *dirs, leaf = parts
    try:
        fd = open_overlay_dir(root, *dirs, raise_errors=True)
    except OSError:
        return {"status": "refused", "error_code": "file_directory_refused"}
    if fd is None:
        return {"status": "refused", "error_code": "file_directory_refused"}
    try:
        data, reason, size = read_overlay_bytes(Path(leaf), max_bytes=FILE_MAX_BYTES + 1, dir_fd=fd)
    except OSError:
        return {"status": "refused", "error_code": "file_read_refused"}
    finally:
        os.close(fd)
    if reason or (size is not None and (size > FILE_MAX_BYTES or len(data or b"") > FILE_MAX_BYTES)):
        return {"status": "refused", "error_code": "file_read_refused"}
    if size is None:
        return {"status": "missing", "content": "", "error_code": "file_missing"}
    try:
        return {"status": "readable", "content": data.decode("utf-8")}
    except UnicodeDecodeError:
        return {"status": "refused", "error_code": "file_not_utf8"}


def _calendar_client(config):
    from istota.skills.calendar import get_caldav_client
    if not config.caldav_url or not config.caldav_username or not config.caldav_password:
        raise GroupError(503, "calendar_unavailable", "Calendar service is unavailable")
    return get_caldav_client(config.caldav_url, config.caldav_username, config.caldav_password, timeout=30)


def verify_resource_ownership(config, kind, resource):
    """Approval uses a live owner check outside the write transaction."""
    from istota.skills.calendar import CalendarNotFound, resolve_owned_calendar
    if kind == "personal_file":
        result = _read_file(config, resource)
        if result["status"] != "readable":
            raise GroupError(422, "file_unavailable", "Select a readable text file in your workspace")
        return
    try:
        with _calendar_client(config) as client:
            resolve_owned_calendar(client, resource["owner_id"], resource["calendar_url"])
    except CalendarNotFound:
        raise GroupError(404, "calendar_not_found", "Calendar not found") from None
    except GroupError:
        raise
    except Exception:
        raise GroupError(503, "calendar_unavailable", "Calendar service is unavailable") from None


def _read_calendar(config, resource, definition, now):
    from istota.skills.calendar import CalendarNotFound, get_events, resolve_owned_calendar
    zone = ZoneInfo(definition["timezone"])
    day = now.astimezone(zone).replace(hour=0, minute=0, second=0, microsecond=0)
    start = day - timedelta(days=resource["lookback_days"])
    end = day + timedelta(days=resource["lookahead_days"])
    try:
        with _calendar_client(config) as client:
            url = resolve_owned_calendar(client, resource["owner_id"], resource["calendar_url"])
            events = get_events(client, url, start, end)
    except CalendarNotFound:
        return {"status": "refused", "error_code": "calendar_not_owned"}
    except Exception:
        # Provider errors can contain event data or credential-bearing URLs.
        return {"status": "unavailable", "error_code": "calendar_unavailable"}
    items = []
    for event in events:
        if event.classification != "PUBLIC" and not resource["include_private"]:
            continue
        try:
            event_zone = ZoneInfo(event.timezone) if event.timezone else zone
        except (ValueError, KeyError):
            try:
                event_zone = datetime.fromisoformat("2000-01-01T00:00:00" + event.timezone.removeprefix("UTC")).tzinfo
            except (ValueError, TypeError):
                return {"status": "unavailable", "error_code": "calendar_time_unavailable"}
        event_start = event.start.replace(tzinfo=event_zone) if event.start.tzinfo is None else event.start
        event_end = event.end.replace(tzinfo=event_zone) if event.end.tzinfo is None else event.end
        if event_start >= end or event_end < start or (event_end == start and event_start < start):
            continue
        projected = {}
        for field in resource["fields"]:
            if field == "time":
                projected[field] = {"start": event.start.isoformat(), "end": event.end.isoformat(),
                                    "all_day": event.all_day, "timezone": event.timezone}
            elif field == "title":
                projected[field] = event.summary
            else:
                projected[field] = getattr(event, field)
        items.append(projected)
    return {"status": "readable", "items": items}


def read_contribution(conn, config, definition_id, source_id, *, expected_grant=None, now=None):
    """Resolve live approval both before and after I/O; return no bytes on loss."""
    from istota.briefings.group_store import get_definition
    from istota.briefings.source_grants import grant_snapshot, resolve_source_grant
    definition = get_definition(conn, definition_id)
    grant = resolve_source_grant(conn, config, definition, source_id)
    snapshot = grant_snapshot(grant)
    if expected_grant is not None and expected_grant != snapshot:
        raise GroupError(409, "source_approval_changed", "Source approval changed")
    resource = grant["resource"]["config"]
    kind = grant["resource"]["kind"]
    # The stored grant is normalized again so corrupt data never widens a read.
    resource = normalize_contribution_config(kind, resource)
    if kind == "personal_file":
        result = _read_file(config, resource)
    else:
        result = _read_calendar(config, resource, definition, now or datetime.now(timezone.utc))
    current = get_definition(conn, definition_id)
    if current is None or current["revision"] != definition["revision"]:
        raise GroupError(409, "revision_conflict", "Briefing changed during source read")
    if grant_snapshot(resolve_source_grant(conn, config, current, source_id)) != snapshot:
        raise GroupError(409, "source_approval_changed", "Source approval changed during source read")
    return result
