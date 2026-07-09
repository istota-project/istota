# GPS location tracking

Overland GPS webhook receiver for location tracking with place detection and visit logging.

## Architecture

The webhook receiver (`webhook_receiver.py`) runs as a separate FastAPI service ingesting location pings from the [Overland](https://overland.p3k.app/) iOS/Android app. It detects transitions between named places and logs visits.

```
Overland app -> POST /webhooks/location?token=... -> webhook_receiver.py -> SQLite
```

## Setup

Enable in config:

```toml
[location]
enabled = true
webhooks_port = 8765
```

Per-user ingest tokens are stored as connected services in the encrypted `secrets` table. Provision via the web settings UI at `/istota/location/settings` or CLI:

```bash
istota secret ensure --user alice --service overland --key ingest_token --value secret-token-here
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

## Database

Location data lives in per-user SQLite files at `{workspace}/location/data/location.db`, not in the framework `istota.db`. The module package at `src/istota/location/` provides `resolve_for_user(user_id, config)` following the same pattern as `feeds` and `money`.

### Tables (per-user location.db)

| Table | Purpose |
|---|---|
| `location_pings` | Raw GPS data (carries a `source` column: `overland` for native phone pings, `garmin` for imported watch tracks) |
| `places` | Named geofences with coordinates and radius |
| `visits` | Detected place visits (arrival/departure) |
| `location_state` | Per-user tracking state |
| `dismissed_clusters` | Clusters the user chose not to save as places |

Old pings are cleaned after `location_ping_retention_days` (default 365).

The two Nominatim caches (`geocode_cache`, `reverse_geocode_cache`) remain in the framework `istota.db` for cross-user dedup. Skill subcommands and web routes that need reverse geocoding open a second connection via `location.db.with_geocode_conn(framework_db_path)`.

## Garmin track import

Overland (the phone tracker) is the normal source of pings, but activities recorded on a Garmin watch without the phone (watch-only runs, hikes where the phone stays packed or dies) leave gaps. `scripts/import_garmin_tracks.py` pulls GPS tracks for watch-recorded running / hiking / walking activities and inserts them into `location.db` as `source='garmin'` pings, **only where Overland has no native coverage** — native always wins.

It authenticates through the shared Garmin connection (Settings → Connected services; the same credentials the health module uses), so connect Garmin once and both features work. The dedup is spatiotemporal: a Garmin point is dropped only when a native ping exists within both a time band (`--guard-band`, default 300 s) and a distance band (`--guard-radius`, default 150 m) of it — so a phone left at home (which keeps emitting stationary pings) never shadows a run happening elsewhere. Imported points are placeless breadcrumbs (`place_id` NULL) that show on the map and in history but don't create place visits. Re-running is idempotent (evict-then-reinsert per activity), and a late Overland upload for a gap-filled window evicts the now-covered imports on the next run.

The importer core lives in `istota.location.garmin_import` (`import_tracks()`), shared by three access points:

- **Web button** — "Import GPS tracks" on the Garmin card (Settings → Connected services) calls `POST /istota/api/garmin/import-tracks` (gated on the location module), which runs the importer inline and reports how many points it added.
- **Assistant / chat** — the `location import-garmin-tracks` skill subcommand. Because the sandbox can write `location.db` but not decrypt the Garmin tokens (the master key is stripped), a sandboxed call *delegates*: it writes a `task_<id>_garmin_import.json` deferred op and the scheduler runs the import in-process post-task, then notifies the user with the result. An operator shell that has `ISTOTA_SECRET_KEY` runs it directly instead.
- **CLI / cron** — `scripts/import_garmin_tracks.py`, a thin wrapper over the same module (below).

```
# Dry-run (read-only) — see what would import over the last 30 days
scripts/import_garmin_tracks.py --user stefan --days-back 30 --dry-run

# Nightly rolling import
scripts/import_garmin_tracks.py --user stefan --days-back 7
```

**Environment / cron.** The script decrypts the Garmin token blob, so it needs `ISTOTA_DB_PATH` (the framework istota.db — also used to resolve the per-user `location.db`, so the script is working-directory independent) and `ISTOTA_SECRET_KEY` in its environment, and must run in the real scheduler/cron environment where `location.db` is writable (never inside a task sandbox, where the DB is read-only). Because istota's CRON.md `command:` jobs deliberately strip `*_SECRET`/`*_TOKEN` vars, wire the nightly run as a **system cron entry or systemd timer that sources the service `EnvironmentFile`** and sets `ISTOTA_DB_PATH`, not as a CRON.md job. `--dry-run` is read-only and safe to run anywhere the env is available.

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
