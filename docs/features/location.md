# GPS location tracking

Overland GPS webhook receiver for location tracking with place detection and visit logging.

## Architecture

The webhook receiver (`webhook_receiver.py`) runs as a separate FastAPI service ingesting location pings from the [Overland](https://overland.p3k.app/) iOS/Android app. It detects transitions between named places and logs visits.

```
Overland app -> POST /overland/{token} -> webhook_receiver.py -> SQLite
```

## Setup

Enable in config:

```toml
[location]
enabled = true
webhooks_port = 8765
```

Per-user config via `[[resources]]`:

```toml
[[resources]]
type = "overland"
name = "GPS"
extra = { ingest_token = "secret-token-here", default_radius = 100 }
```

Install the location extras:

```bash
uv sync --extra location
```

Run the receiver:

```bash
uvicorn istota.webhook_receiver:app --port 8765
```

## Places

Named geofences stored in the `places` DB table. Manage via CLI or web UI:

- **CLI**: `learn`, `update`, `delete` subcommands via `python -m istota.skills.location`
- **Web UI**: create from discovered clusters, edit form, drag-to-reposition on map

Place detection uses hysteresis (2 consecutive pings required) to avoid flapping at geofence boundaries. Updating a place's location or radius triggers automatic ping reassignment.

## Database tables

| Table | Purpose |
|---|---|
| `location_pings` | Raw GPS data |
| `places` | Named geofences with coordinates and radius |
| `visits` | Detected place visits (arrival/departure) |
| `location_state` | Per-user tracking state |

Old pings are cleaned after `location_ping_retention_days` (default 365).

## Web interface

The [web interface](web-interface.md) provides location pages:

- **Today view**: current position, day summary, trips
- **History**: date picker, activity filter, heatmap
- **Places**: discover unknown clusters, create/edit/delete places, visit statistics
