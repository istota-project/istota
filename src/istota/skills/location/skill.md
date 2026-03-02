# Location Skill

GPS-based location tracking via the Overland iOS app. Tracks location pings, resolves named places, and records visits.

## Configuration

Location config is stored in `LOCATION.md` (in the user's config directory) as a TOML block. Define places and actions there.

## CLI

All commands output JSON. The `ISTOTA_DB_PATH` and `ISTOTA_USER_ID` environment variables are set automatically.

```bash
# Current location + place/visit info
python -m istota.skills.location current

# Recent pings (default: last 20)
python -m istota.skills.location history
python -m istota.skills.location history --limit 50
python -m istota.skills.location history --date 2026-02-15

# List known places
python -m istota.skills.location places

# Save current location as a named place
# Reads the most recent ping and appends a [[places]] entry to LOCATION.md
# New places take effect on the next incoming ping (no restart needed)
python -m istota.skills.location learn "coffee shop"
python -m istota.skills.location learn "gym" --category gym --radius 75

# Check calendar attendance via GPS pings
# Requires CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD env vars
python -m istota.skills.location attendance
python -m istota.skills.location attendance --date 2026-02-15
python -m istota.skills.location attendance --event "dentist"
```

## Output Examples

### current

```json
{
  "last_ping": {
    "timestamp": "2026-02-20T10:30:00Z",
    "lat": 34.05,
    "lon": -118.4,
    "accuracy": 5,
    "activity_type": "stationary",
    "place": "home"
  },
  "current_visit": {
    "place_name": "home",
    "entered_at": "2026-02-20T08:00:00Z",
    "duration_minutes": 150,
    "ping_count": 30
  }
}
```

### history

```json
[
  {
    "timestamp": "2026-02-20T10:30:00Z",
    "lat": 34.05,
    "lon": -118.4,
    "accuracy": 5,
    "place": "home",
    "activity_type": "stationary"
  }
]
```

### places

```json
[
  {
    "name": "home",
    "lat": 34.05,
    "lon": -118.4,
    "radius_meters": 150,
    "category": "home"
  }
]
```

### learn

```json
{
  "status": "ok",
  "place": "coffee shop",
  "lat": 34.06,
  "lon": -118.39,
  "radius_meters": 100,
  "message": "Saved 'coffee shop' at 34.0600, -118.3900"
}
```

### attendance

Cross-references calendar events with GPS pings to confirm attendance. Skips all-day events, events without a location, and virtual meetings. Resolves event locations by matching against known places first, then geocoding via Nominatim (results cached in DB). Uses a 30-minute buffer around event times and a default 200m radius (or the place's radius if matched).

```json
{
  "date": "2026-02-20",
  "events": [
    {
      "summary": "Dentist",
      "uid": "abc123",
      "start": "2026-02-20T10:00:00-08:00",
      "end": "2026-02-20T11:00:00-08:00",
      "location": "123 Main St",
      "location_resolved": true,
      "resolution_source": "geocode",
      "event_lat": 34.05,
      "event_lon": -118.4,
      "radius_meters": 200,
      "attended": true,
      "first_nearby_ping": "2026-02-20T09:45:00Z",
      "last_nearby_ping": "2026-02-20T10:55:00Z",
      "nearby_ping_count": 12
    }
  ]
}
```
