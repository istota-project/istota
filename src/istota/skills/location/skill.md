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
# Reads the most recent ping and writes a new [[places]] entry to LOCATION.md
python -m istota.skills.location learn "coffee shop"
python -m istota.skills.location learn "gym" --category gym --radius 75
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
