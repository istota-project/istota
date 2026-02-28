# Garmin Skill

Syncs Garmin Connect run activities to the Private CalDAV calendar.

## Behavior

For a given date (defaults to today in LA timezone):

- **RUN event exists + run completed** → updates event with ✅ prefix and run stats (distance, duration, pace, avg HR)
- **RUN event exists + no run recorded** → updates event with ❌ prefix
- **Run completed, no calendar event** → creates a new ✅ RUN event with stats
- **No run, no event** → nothing to do

## Configuration

Credentials are stored in a GARMIN.md file as a TOML block:

```markdown
\```toml
[garmin]
email = "your@email.com"
password = "yourpassword"
token_dir = "/optional/token/cache/path"
\```
```

Default config path: `<NEXTCLOUD_MOUNT>/Users/<user>/zorg/config/GARMIN.md`

Override with the `GARMIN_CONFIG` environment variable or `--config` flag.

Tokens are cached in `token_dir` (default: `/srv/app/istota/data/garmin_tokens`) to avoid repeated password auth.

## CLI

```bash
# Sync today's runs to the Private calendar
python -m istota.skills.garmin sync

# Dry run (no changes)
python -m istota.skills.garmin sync --dry-run

# Specific date
python -m istota.skills.garmin sync --date 2026-02-15

# Custom config file
python -m istota.skills.garmin sync --config /path/to/GARMIN.md

# Custom calendar URL
python -m istota.skills.garmin sync --calendar "https://..."
```

Output is JSON:

```json
{
  "date": "2026-02-15",
  "action": "updated_completed",
  "summary": "✅ RUN",
  "stats": "Distance: 8.42 km\nDuration: 45:30\nPace: 5:24 /km\nAvg HR: 152 bpm",
  "dry_run": false
}
```

Action values: `nothing`, `updated_completed`, `updated_missed`, `created`.

## CalDAV

Uses the same `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD` environment variables as the calendar skill.

## Cron Usage

Add to `CRON.md` to run nightly after the day ends:

```toml
[[job]]
name = "garmin-sync"
schedule = "0 23 * * *"
command = "python -m istota.skills.garmin sync"
user = "alice"
```
