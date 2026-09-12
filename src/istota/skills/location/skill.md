---
name: location
triggers: [location, gps, where, place, places, visit, visits, track, position, coordinates, attendance, reverse geocode, day summary, neighborhood, summary]
description: Location tracking, place recognition, visit history, and calendar attendance
cli: true
env: [{"var":"LOCATION_DB_PATH","from":"setup_env","proxy_only":true},{"var":"CALDAV_URL","from":"config","config_path":"caldav_url","when":"caldav_url","gate_has_discovered_calendars":true},{"var":"CALDAV_USERNAME","from":"config","config_path":"caldav_username","when":"caldav_url","gate_has_discovered_calendars":true},{"var":"CALDAV_PASSWORD","from":"config","config_path":"caldav_password","when":"caldav_url","gate_has_discovered_calendars":true,"sensitive":true}]
---
# Location Skill

GPS-based location tracking via the Overland iOS app. Tracks location pings, resolves named places, and records visits.

## Places

Places (named geofences) are stored in the database. Full CRUD via CLI:
- **`places`** — list all saved places (includes `id` for each)
- **`learn`** — save a named place, at given coordinates or (with none) at the current GPS position
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

All commands output JSON. The CLI resolves its own databases — `LOCATION_DB_PATH` (your per-user location database, set by the skill's `setup_env` hook) and, for `reverse-geocode` and `day-summary`, `ISTOTA_DB_PATH` for the shared reverse-geocode cache. Both are handed to the CLI, not to you: the files live on local disk outside your sandbox, so this CLI is the only way to reach them.

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

# Save a named place (inserts into DB)
# Takes effect immediately on the next incoming ping
istota-skill location learn "coffee shop"
istota-skill location learn "gym" --category gym --radius 75
istota-skill location learn "office" --notes "side entrance, 4th floor"

# Name a stop you have already left — the usual case, since a day summary or
# `discover` surfaces the stop hours later. Without --lat/--lon the place is
# saved at the device's CURRENT position, which by then is somewhere else.
istota-skill location learn "hardware store" --lat 40.75012 --lon -73.98771 --radius 50

# Better: snap to the discovered cluster nearest that point and adopt its
# fitted centroid and radius. Coordinates read off a summary are one ping's
# position; a cluster's centre is weighted by every ping in it. --radius is
# ignored here; the cluster's own fitted radius wins.
#
# `discover` hides any cluster within 200m of a place that is already saved,
# and --from-cluster resolves through that same filter — so this refuses when
# re-siting an existing place, or naming a stop next to one. Drop the flag and
# pass the coordinates directly in that case.
istota-skill location learn "hardware store" --lat 40.75012 --lon -73.98771 --from-cluster

# --backfill assigns the pings already inside the new geofence to this place.
# Without it the visit that prompted the naming keeps reading as an unnamed
# coordinate in every later day-summary and place-stats. It rewrites history
# and a generous radius can absorb a neighbour's pings, so it is opt-in; the
# response reports how many rows moved.
#
# It rewrites pings only. `day-summary` and `place-stats` are derived from
# pings and so pick the naming up; the `visits` table is re-derived on a
# rolling recent window, so a backfill of older pings does not reach it.
istota-skill location learn "hardware store" --lat 40.75012 --lon -73.98771 --backfill

# Update an existing place — identify by --name or --id
# Only specified fields are changed; others are left as-is
istota-skill location update --name "coffee shop" --category food
istota-skill location update --name "old name" --rename "new name"
istota-skill location update --id 42 --radius 200 --notes "back entrance"
istota-skill location update --id 42 --notes ""  # clear notes
istota-skill location update --name "office" --lat 40.71 --lon -74.01

# Moving or resizing a place leaves its pings on the old footprint. --backfill
# reassigns them: pings now outside the geofence are released, unassigned ones
# now inside are adopted. Reported as `reassigned_pings`.
istota-skill location update --id 42 --radius 200 --backfill

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
istota-skill location reverse-geocode --lat 40.71 --lon -74.01

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
istota-skill location dismiss-cluster --lat 40.71 --lon -73.98 --radius 200

# List / un-dismiss
istota-skill location list-dismissed
istota-skill location restore-dismissed 7
```

## Output Examples

### current

Aliased as `last` (`istota-skill location last`).

```json
{
  "last_ping": {
    "timestamp": "2026-02-20T10:30:00Z",
    "lat": 40.71,
    "lon": -73.98,
    "altitude": 12.4,
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
    "lat": 40.71,
    "lon": -73.98,
    "altitude": 12.4,
    "accuracy": 5,
    "place": "home",
    "activity_type": "stationary"
  }
]
```

`altitude` is metres as the device reported them, and is `null` on the ~5% of
pings that got a horizontal fix without a vertical one, on a fix the device
flagged as vertically invalid, and on a point the client *declared* rather than
measured (its wifi-zone feature substitutes a coordinate while the device is on
a configured network). What it is measured against varies by source — a phone
reports one reference, an imported watch track another — so treat it as good
for "this was a climb" and never as an altimeter reading.

### places

```json
[
  {
    "id": 1,
    "name": "home",
    "lat": 40.71,
    "lon": -73.98,
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
  "lat": 40.75,
  "lon": -73.99,
  "radius_meters": 100,
  "source": "argument",
  "backfilled_pings": 4,
  "released_pings": 0,
  "message": "Saved 'coffee shop' at 40.7500, -73.9900"
}
```

`source` says which input sited the place: `argument` for `--lat`/`--lon`,
`cluster` for `--from-cluster`, `latest_ping` when neither was given. Check it
— a place saved at the device's current position looks identical otherwise.

`learn` on a name that already exists **moves** that place rather than failing.
With `--backfill` that means pings can be detached as well as adopted, so both
counts are reported: `backfilled_pings` took the new geofence,
`released_pings` fell outside it and went back to unassigned. Both are `null`
without `--backfill`.

A `--from-cluster` response also carries a `cluster` object with the ping
count, the fitted radius and the first and last times the cluster was seen,
plus `radius_overridden` when a `--radius` was passed and ignored in favour of
the fitted one.

### update

```json
{
  "status": "ok",
  "place": {
    "id": 42,
    "name": "coffee shop",
    "lat": 40.75,
    "lon": -73.99,
    "radius_meters": 100,
    "category": "food",
    "notes": null
  },
  "reassigned_pings": {"assigned": 0, "released": 1}
}
```

`reassigned_pings` is `null` without `--backfill` and on an edit that did not
change `lat`, `lon` or `radius` — there is nothing to reassign. Otherwise
`released` left the geofence and `assigned` entered it.

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
      "lat": 40.7580,
      "lon": -73.9855,
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
      "lat": 40.71,
      "lon": -73.98,
      "radius_meters": 100,
      "dismissed_at": "2026-04-20T12:00:00Z"
    }
  ]
}
```

### reverse-geocode

```json
{
  "display_name": "123 Main St, New York, NY 10001, USA",
  "neighborhood": "Downtown",
  "suburb": "Central LA",
  "road": "Main St",
  "city": "New York",
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
      "lat": 40.71,
      "lon": -74.01
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
      "lat": 40.78,
      "lon": -73.96
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
      "event_lat": 40.71,
      "event_lon": -73.98,
      "radius_meters": 200,
      "attended": true,
      "first_nearby_ping": "2026-02-20T09:45:00Z",
      "last_nearby_ping": "2026-02-20T10:55:00Z",
      "nearby_ping_count": 12
    }
  ]
}
```
