# Calendar Operations

Calendar operations use CalDAV. Credentials are available via environment variables:
- `CALDAV_URL`: CalDAV server URL (e.g., `https://nextcloud.example.com/remote.php/dav`)
- `CALDAV_USERNAME`: Username for CalDAV authentication
- `CALDAV_PASSWORD`: Password/app token for CalDAV authentication

## CLI Commands

The simplest way to interact with calendars is via the CLI:

```bash
# List today's events from all calendars
python -m istota.skills.calendar list --tz "America/Los_Angeles"

# List tomorrow's events
python -m istota.skills.calendar list --date tomorrow --tz "America/Los_Angeles"

# List events for a specific date
python -m istota.skills.calendar list --date 2026-02-15 --tz "America/Los_Angeles"

# List from a specific calendar
python -m istota.skills.calendar list --calendar "https://..." --date today

# Create an event
python -m istota.skills.calendar create \
  --calendar "https://..." \
  --summary "Team Meeting" \
  --start "2026-02-15 14:00" \
  --end "2026-02-15 15:00" \
  --location "Conference Room A"

# Delete an event
python -m istota.skills.calendar delete --calendar "https://..." --uid "event-uid-here"
```

**Always pass `--tz` with the user's timezone** (from prompt metadata) to ensure correct date boundaries.

Output is JSON:
```json
{
  "status": "ok",
  "date": "today",
  "event_count": 2,
  "events": [
    {
      "calendar": "Work",
      "uid": "abc123",
      "summary": "Team Meeting",
      "start": "2026-02-15T14:00:00",
      "end": "2026-02-15T15:00:00",
      "location": "Conference Room A",
      "description": null,
      "all_day": false
    }
  ]
}
```

## Python API

The `istota.skills.calendar` module also provides functions for programmatic access:

| Function | Description | Returns |
|----------|-------------|---------|
| `get_caldav_client(url, username, password)` | Create CalDAV client | `caldav.DAVClient` |
| `list_calendars(client)` | List all accessible calendars | `list[(name, url)]` |
| `get_calendars_for_user(client, username)` | Get calendars owned by a user | `list[(name, url, writable)]` |
| `get_events(client, calendar_url, start, end)` | Get events in date range | `list[CalendarEvent]` |
| `get_today_events(client, calendar_url, tz)` | Get today's events | `list[CalendarEvent]` |
| `get_tomorrow_events(client, calendar_url, tz)` | Get tomorrow's events | `list[CalendarEvent]` |
| `create_event(client, calendar_url, ...)` | Create new event | `str` (event UID) |
| `update_event(client, calendar_url, uid, ...)` | Update existing event | `bool` |
| `delete_event(client, calendar_url, uid)` | Delete event by UID | `bool` |
| `format_event_for_display(event)` | Format event for human display | `str` |
| `format_day_schedule(events, date_label)` | Format day's events | `str` |

### CalendarEvent Dataclass

```python
@dataclass
class CalendarEvent:
    uid: str           # Unique identifier
    summary: str       # Event title
    start: datetime    # Start time
    end: datetime      # End time
    location: str | None
    description: str | None
    all_day: bool
```

### Permissions

Calendars can be shared with read-only or edit permissions:
- **Read-only**: Can view events but not modify them
- **Edit**: Can create, update, and delete events

When attempting to modify a read-only calendar, `update_event()` and `create_event()` will raise `caldav.error.AuthorizationError`.

### Python Example

```python
from istota.skills.calendar import get_caldav_client, get_today_events
import os

client = get_caldav_client(
    url=os.environ["CALDAV_URL"],
    username=os.environ["CALDAV_USERNAME"],
    password=os.environ["CALDAV_PASSWORD"],
)

# Get today's events (always pass user's timezone)
calendar_url = "https://nextcloud.example.com/remote.php/dav/calendars/alice/personal/"
for event in get_today_events(client, calendar_url, tz="America/Los_Angeles"):
    print(f"{event.start.strftime('%H:%M')} - {event.summary}")
```
