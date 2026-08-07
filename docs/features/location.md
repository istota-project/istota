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

The settings page can also mint one for you (`POST /api/settings/secrets/overland/ingest_token/generate`), which returns the token and the assembled webhook URL. That response is the only time either is readable — afterwards the secret is write-only like every other one, and it is the only endpoint in the app that returns a secret in a response body. Generating again rotates: the previous token stops working immediately, on every device using it, which is also how you revoke a lost phone.

Minting refuses in three cases, and every check runs before the write, so a refusal never rotates a token that was working: the location module is off for that user (409), `[site] hostname` is unset (409), or `ISTOTA_SECRET_KEY` is missing (503). The hostname check exists because the assembled URL would otherwise be a relative path, which the phone's decoder rejects as not being `https://` — the device would then blame the code rather than the configuration. In practice only a standalone install reaches it, since under Nextcloud auth a blank hostname already fails the origin check.

Having minted one, the page renders it as a QR code the native iOS app scans (Settings → Location → **This device** → Scan), and prints the full webhook URL beside it for the third-party Overland app, which has no scanner. The payload is a small JSON envelope — `{v, endpoint, token}`, version 1 — carrying the endpoint and the token, deliberately not the webhook URL itself: a bare `https://` code is offered for opening by every generic scanner, including the iOS Camera app, which would put the token in Safari's address bar and history in exchange for a 405. The query string is stripped from the endpoint, so the token appears in the payload exactly once; the tracker sends it as a bearer header. The decoder rejects rather than repairs — wrong version, non-`https` endpoint, or anything over its size caps is refused whole rather than partially honoured. Leaving the settings page loses the code, because there is nowhere to read the token back from; the next device needs a new one.

The receiver holds the token map in memory and runs in a different process from the web app, so a write through the web UI leaves a stamped sentinel (`.location_ingest_reload`, beside `istota.db`) that the receiver stats on each ingest request and reloads from. Before that existed, a freshly provisioned token was refused until the receiver restarted. Stamping is best-effort: if it fails, the token still works after the next restart. **The CLI path above does not stamp it** — `istota secret ensure` writes the secret and nothing else, so a token provisioned that way applies on the receiver's next restart or SIGHUP.

Install the location extras:

```bash
uv sync --extra location
```

Run the receiver:

```bash
uvicorn istota.webhook_receiver:app --port 8765
```

On a local install there is no separate receiver process: `istota serve` includes the ingest router into the web app, so the endpoint is `/webhooks/location` on the **web** port and `webhooks_port` is unused. That shape has no SIGHUP handler either — the handler belongs to the receiver's own startup — so the sentinel above is the only way a new token reaches it without a restart.

## Tracking on the device

The native app's tracker is configured on the phone and nowhere else. That is forced rather than chosen: one account can have two phones, so any tracker setting kept server-side would have the two devices overwriting each other's row. The **This device** card on `/location/settings` therefore talks to the app directly and renders only inside it; in a browser it says so in one line rather than disappearing, so a user who set this up on their phone can tell the section is absent by design.

What it exposes is deliberately smaller than Overland's fifteen settings:

| Setting | Notes |
|---|---|
| Tracking on/off | Reads **Paused** rather than On when iOS has suspended updates because the phone hasn't moved — a battery saving, so the card says so as information rather than a warning, and notes that moving resumes it. "On" beside an hours-old last-sent time and an empty queue is what a tracker that has silently died looks like, which is the one reading this card exists to make trustworthy. Stop stays available: tracking is still armed |
| Profile: Detailed or Places | **Detailed** logs a continuous line and sends every minute, at a battery cost. **Places** logs arrivals and departures only and sends every five minutes. Switching while tracking re-arms in place, so it costs no gap in coverage; switching while stopped is held and applied by the next Start, since starting is the only thing that can set a profile |
| Permission, and a way into iOS Settings | Only Settings.app can restore a *denied* Always authorization, so that is the one state where the button replaces Start/Stop rather than sitting beside it. **While in use** — the case the card mostly exists to catch — keeps Stop reachable and shows the prompt alongside it |
| Queued points, points dropped, last sent, last error, endpoint host, device id | The readout that says tracking is still alive. The failure worth preventing is tracking stopping silently and the gap being found weeks later. Only the endpoint's host is shown; the token is never echoed back |
| Send now, rescan code | |

Send interval and batch size are not separately exposed. The interval follows the profile — there is nothing to send every minute when fixes arrive a kilometre apart — and a numeric field with no basis for setting it invites a five-second interval and a dead battery.

Both profiles request 100 m accuracy, which is not a knob because the server drops any ping worse than `accuracy_threshold_m` (default 100) from place matching. A genuinely coarse profile would keep storing pings — the map would still fill in — while quietly ceasing to detect being anywhere.

The card is gated on the app's version, and the two halves are gated separately: tracking needs shell 0.6.0, scanning a code needs 0.7.0. A 0.6.0 app therefore gets a working status card that says it cannot scan and points at TestFlight, rather than a Scan button that does nothing. The gate reads the app's own `IstotaApp/<version>` user-agent token rather than the presence of a plugin object, so a page carrying a look-alike shim outside the app never calls through. A scan that finds nothing distinguishes three outcomes — cancelled (which says nothing, because the user chose it), a code that isn't ours, and a build too old to scan.

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
| `location_pings` | Raw GPS data (carries a `source` column: `overland` for native phone pings, `garmin` for imported watch tracks; and an optional `client_id` — see below) |
| `places` | Named geofences with coordinates and radius |
| `visits` | Detected place visits (arrival/departure) |
| `location_state` | Per-user tracking state |
| `dismissed_clusters` | Clusters the user chose not to save as places |

Old pings are cleaned after `location_ping_retention_days` (default 365).

### Re-sent batches

A client that keeps points in a queue and deletes them only once the server acknowledges the batch will re-send a batch whose response was lost. If the point's GeoJSON properties carry a `client_id` — a value the client mints once per point — the second delivery is recognised and writes nothing. Uniqueness is enforced by a partial index, so points without one (stock Overland, the Garmin importer) never collide with each other and genuinely repeated fixes all land.

This matters beyond a duplicate row: every ping at your current place increments the open visit's `ping_count`, so a re-sent batch would otherwise inflate a visit that never grew.

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
scripts/import_garmin_tracks.py --user alice --days-back 30 --dry-run

# Nightly rolling import
scripts/import_garmin_tracks.py --user alice --days-back 7
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

**VPN (alternative)**: if you don't want to expose the endpoint publicly, connect your phone to your server's network via WireGuard or Tailscale. Point Overland at the server's internal IP and webhook port directly (e.g., `http://10.0.0.5:8765/webhooks/location`). This keeps the endpoint off the public internet but requires the VPN to be active for location tracking to work. It is a stock-Overland arrangement only: the URL has to be typed in by hand, since QR provisioning refuses a plain-`http` endpoint and minting refuses to run at all without `[site] hostname`.

A reverse proxy is strongly recommended since it also covers the web UI and any other istota services. The endpoint is authenticated per-user by the ingest token, supplied either as a `?token=` query parameter (what Overland sends) or as an `Authorization: Bearer` header (what the native tracker sends). A missing token is a 401 and an unknown one a 403. Public exposure is safe as long as TLS is enabled and tokens are kept secret.

## Web interface

The [web interface](web-interface.md) provides location pages:

- **Today view**: current position, day summary, trips, elevation profile
- **History**: date picker, activity filter, heatmap, elevation profile
- **Places**: discover unknown clusters (with dismiss option), create/edit/delete places, visit statistics

## Following the timezone on travel

`user_profiles.timezone` drives the `User timezone:` prompt header, and through it every briefing, calendar read and scheduled prompt. The scheduler can keep it in step with where the user actually is: once the newest ping and a ping at least an hour older resolve to the same IANA zone, sit within 100 km of each other, and that zone puts the clock somewhere other than the stored one does, the profile is updated and the user is told.

It is **opt-in per user** — `timezone_follow_location` on the profile, the "Update timezone when I travel" checkbox beside the timezone field in Settings, off by default. The setting being rewritten is one the user chose, so it is something they switch on and an event they are notified about, rather than an inference made quietly. It also needs the location module enabled and the `location` extra installed (`timezonefinder`, which resolves coordinates offline — no network call, and the user's position is not sent anywhere).

**The distance test, not the hour, is what keeps a journey from moving the timezone.** "Has been in this zone for an hour" is true of a plane over the Midwest: a continental flight spends over two hours above a single zone, so a dwell-only rule would set the timezone to whatever is being flown over and then set it again on landing. Requiring that the two samples are also near each other asks the question actually intended — have you stayed somewhere. Open water is covered separately: it resolves to an `Etc/GMT±N` offset rather than a place, and those are rejected outright.

Three further guards. A track that has gone stale (nothing within two hours) is ignored, since yesterday's position says nothing about today. A ping with no accuracy figure, or one worse than `[location] accuracy_threshold_m`, is not used — an imported watch track carries no accuracy at all, and a junk fix can land across a border. And a zone that merely *renames* the stored one (`US/Pacific` against `America/Los_Angeles`, both of which Nextcloud seeds) is not a move, so it is compared by the wall clock it produces rather than by name.

The change is recorded, and the same zone is not written again for 24 hours. Detection compares where you are against what is stored and remembers nothing by itself, so without that record a user who prefers home time abroad and sets it back by hand would be overridden on the next pass, and again on the one after.
