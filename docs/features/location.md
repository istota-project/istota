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

Places have an optional `notes` text field for free-form annotations.

Place detection uses hysteresis (2 consecutive pings required) to avoid flapping at geofence boundaries. Pings with horizontal accuracy above `accuracy_threshold_m` (default 100 m) are stored but skipped for place matching. A periodic reconciler re-derives closed visits from stored pings so historical visits recover from state-machine drift. Updating a place's location or radius triggers automatic ping reassignment.

## Database tables

| Table | Purpose |
|---|---|
| `location_pings` | Raw GPS data |
| `places` | Named geofences with coordinates and radius |
| `visits` | Detected place visits (arrival/departure) |
| `location_state` | Per-user tracking state |
| `dismissed_clusters` | Clusters the user chose not to save as places |

Old pings are cleaned after `location_ping_retention_days` (default 365).

## Network access

The webhook receiver must be reachable from the Overland app on your phone. Two approaches:

**Reverse proxy (recommended)**: expose the webhook endpoint through nginx or another reverse proxy with TLS. The Ansible role generates the nginx config automatically when `istota_location_enabled` is true:

```nginx
location /webhooks/ {
    proxy_pass http://127.0.0.1:8765/webhooks/;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
    client_max_body_size 10m;
}
```

This is the same reverse proxy you'd use for the web UI — if you're already running nginx for istota, location webhooks are included. Point Overland at `https://{your-hostname}/webhooks/location` with your ingest token.

**VPN (alternative)**: if you don't want to expose the endpoint publicly, connect your phone to your server's network via WireGuard or Tailscale. Point Overland at the server's internal IP and webhook port directly (e.g., `http://10.0.0.5:8765/webhooks/location`). This keeps the endpoint off the public internet but requires the VPN to be active for location tracking to work.

A reverse proxy is strongly recommended since it also covers the web UI and any other istota services. The Overland endpoint is authenticated per-user via the ingest token in the URL path, so public exposure is safe as long as TLS is enabled and tokens are kept secret.

## Web interface

The [web interface](web-interface.md) provides location pages:

- **Today view**: current position, day summary, trips
- **History**: date picker, activity filter, heatmap
- **Places**: discover unknown clusters (with dismiss option), create/edit/delete places, visit statistics
