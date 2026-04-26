---
name: location
triggers: [location, gps, where, place, places, visit, visits, track, position, coordinates, attendance, reverse geocode, day summary, neighborhood, summary]
description: Location tracking, place recognition, visit history, and calendar attendance
cli: true
resource_types: [calendar]
---
# Location Skill

GPS-based location tracking via the Overland iOS app. Tracks location pings, resolves named places, and records visits.

## Places

Places (named geofences) are stored in the database. Full CRUD via CLI:
- **`places`** — list all saved places (includes `id` for each)
- **`learn`** — save current GPS position as a named place
- **`update`** — modify an existing place (category, name, radius, coordinates, notes)
- **`delete`** — remove a place (also clears place assignment from historical pings)
- **`place-stats`** — visit count, first/last/longest visit, total time spent (derived from pings)

Changes take effect on the next incoming GPS ping (no restart needed).

## Discover and dismiss

The web UI surfaces "discovered clusters" — recurring locations that aren't yet saved as named places. The same flow is available via CLI:

- **`discover`** — find clusters of stationary pings not assigned to any place. Filters out clusters near existing places or inside dismissed zones.
- **`dismiss-cluster`** — record a lat/lon/radius zone so future `discover` calls skip it (use when the user doesn't want a place suggested again).
- **`list-dismissed`** — list dismissed cluster zones with their ids.
- **`restore-dismissed`** — un-dismiss a zone by id (so it can surface again).

## CLI

Run `istota-skill location --help` (or `istota-skill location <subcommand> --help`) to see the live argument list.

All commands output JSON. The `ISTOTA_DB_PATH` and `ISTOTA_USER_ID` environment variables are set automatically.

```bash
# Current location + place/visit info
istota-skill location current

# Recent pings (default: last 20; --date returns all pings for that day)
istota-skill location history
istota-skill location history --limit 50
istota-skill location history --date 2026-02-15
istota-skill location history --date 2026-02-15 --tz America/New_York

# List known places (each entry includes id, name, lat, lon, radius_meters, category, notes)
istota-skill location places

# Save current location as a named place (inserts into DB)
# Takes effect immediately on the next incoming ping
istota-skill location learn "coffee shop"
istota-skill location learn "gym" --category gym --radius 75
istota-skill location learn "office" --notes "side entrance, 4th floor"

# Update an existing place — identify by --name or --id
# Only specified fields are changed; others are left as-is
istota-skill location update --name "coffee shop" --category food
istota-skill location update --name "old name" --rename "new name"
istota-skill location update --id 42 --radius 200 --notes "back entrance"
istota-skill location update --id 42 --notes ""  # clear notes
istota-skill location update --name "office" --lat 34.05 --lon -118.25

# Delete a place — identify by --name or --id
# Also removes the place assignment from historical pings
istota-skill location delete --name "coffee shop"
istota-skill location delete --id 42

# Check calendar attendance via GPS pings
# Requires CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD env vars
istota-skill location attendance
istota-skill location attendance --date 2026-02-15
istota-skill location attendance --event "dentist"

# Reverse geocode a single coordinate pair
istota-skill location reverse-geocode --lat 34.05 --lon -118.25

# Day summary: clusters pings into stops, resolves names via saved places
# or reverse geocoding, filters transit, merges consecutive same-location stops
istota-skill location day-summary --date 2026-03-08
istota-skill location day-summary --date 2026-03-08 --tz America/New_York

# Visit statistics for a place (by name or id)
istota-skill location place-stats --name "home"
istota-skill location place-stats --id 42

# Find unknown recurring clusters
istota-skill location discover
istota-skill location discover --min-pings 20

# Dismiss a cluster zone so it stops surfacing in discover
istota-skill location dismiss-cluster --lat 34.05 --lon -118.4 --radius 200

# List / un-dismiss
istota-skill location list-dismissed
istota-skill location restore-dismissed 7
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
    "id": 1,
    "name": "home",
    "lat": 34.05,
    "lon": -118.4,
    "radius_meters": 150,
    "category": "home",
    "notes": null
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

### update

```json
{
  "status": "ok",
  "place": {
    "id": 42,
    "name": "coffee shop",
    "lat": 34.06,
    "lon": -118.39,
    "radius_meters": 100,
    "category": "food",
    "notes": null
  }
}
```

### delete

```json
{
  "status": "ok",
  "deleted": "coffee shop"
}
```

### place-stats

```json
{
  "place_id": 42,
  "total_visits": 12,
  "first_visit": "2026-01-08T09:00:00Z",
  "last_visit": "2026-04-22T18:30:00Z",
  "avg_duration_min": 75,
  "total_duration_min": 902,
  "longest_visit_min": 210
}
```

### discover

```json
{
  "clusters": [
    {
      "lat": 34.0612,
      "lon": -118.4055,
      "total_pings": 38,
      "first_seen": "2026-03-01T08:00:00Z",
      "last_seen": "2026-04-25T17:30:00Z",
      "radius_meters": 75
    }
  ]
}
```

### list-dismissed

```json
{
  "dismissed": [
    {
      "id": 7,
      "lat": 34.05,
      "lon": -118.4,
      "radius_meters": 100,
      "dismissed_at": "2026-04-20T12:00:00Z"
    }
  ]
}
```

### reverse-geocode

```json
{
  "display_name": "123 Main St, Los Angeles, CA 90012, USA",
  "neighborhood": "Downtown",
  "suburb": "Central LA",
  "road": "Main St",
  "city": "Los Angeles",
  "source": "nominatim"
}
```

### day-summary

Clusters the day's pings into stops. Resolves location names by: (1) direct place match from ping data, (2) proximity match against saved places (100m minimum radius), (3) reverse geocoding via Nominatim. Filters out transit clusters (1-2 pings without a place match). Merges consecutive stops at the same location.

```json
{
  "date": "2026-03-08",
  "timezone": "America/Los_Angeles",
  "ping_count": 120,
  "transit_pings": 8,
  "stops": [
    {
      "location": "home",
      "location_source": "saved_place",
      "arrived": "08:00",
      "departed": "09:30",
      "ping_count": 20,
      "lat": 34.05,
      "lon": -118.25
    },
    {
      "location": "Magnolia Park",
      "location_source": "nominatim",
      "road": "Elm St",
      "neighborhood": null,
      "suburb": "Magnolia Park",
      "arrived": "10:15",
      "departed": "12:30",
      "ping_count": 25,
      "lat": 34.18,
      "lon": -118.33
    }
  ]
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
