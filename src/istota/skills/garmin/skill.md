# Garmin Skill

General-purpose Garmin Connect data access layer. Exposes activity, stats, and health data as structured JSON. No calendar logic — use in scripts or cron jobs that need Garmin data.

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

The `GARMIN_CONFIG` env var is set automatically by the executor, pointing to `Users/<user>/{BOT_DIR}/config/GARMIN.md`. Override with `--config` flag.

Tokens are cached in `token_dir` (default: `$ISTOTA_DEFERRED_DIR/garmin_tokens`, i.e. the per-user temp directory) to avoid repeated password auth.

## CLI

```bash
# Test authentication
python -m istota.skills.garmin connect

# User profile and devices
python -m istota.skills.garmin user

# Activities (default: today, limit 10)
python -m istota.skills.garmin activities
python -m istota.skills.garmin activities --date 2026-02-15
python -m istota.skills.garmin activities --limit 20
python -m istota.skills.garmin activities --type running

# Daily stats: steps, calories, stress, body battery
python -m istota.skills.garmin stats
python -m istota.skills.garmin stats --date 2026-02-15

# Health metrics: resting HR, sleep, HRV
python -m istota.skills.garmin health
python -m istota.skills.garmin health --date 2026-02-15

# All commands accept --config to override config file path
python -m istota.skills.garmin activities --config /path/to/GARMIN.md
```

All output is JSON.

## Output Examples

### connect

```json
{
  "status": "ok",
  "email": "you@example.com",
  "display_name": "Your Name"
}
```

### activities

```json
[
  {
    "activityId": 12345678,
    "activityName": "Morning Run",
    "activityType": "running",
    "startTimeLocal": "2026-02-15 07:30:00",
    "duration": 2730.0,
    "distance": 8420.0,
    "averageHR": 152,
    "maxHR": 172,
    "calories": 420,
    "averageSpeed": 3.08
  }
]
```

### stats

```json
{
  "date": "2026-02-15",
  "steps": 12400,
  "totalKilocalories": 2800,
  "activeKilocalories": 650,
  "floorsAscended": 8,
  "floorsDescended": 7,
  "stressAvg": 32,
  "bodyBattery": 78
}
```

### health

```json
{
  "date": "2026-02-15",
  "restingHeartRate": 52,
  "avgSleepStress": 21,
  "sleepDuration": 25920,
  "hrvWeeklyAverage": 48
}
```

## Usage in Scripts

Output pipes cleanly into `jq` or Python:

```bash
# Get today's run distance in km
python -m istota.skills.garmin activities --type running | jq '.[0].distance / 1000'

# Check if a run was completed today
python -m istota.skills.garmin activities --type running | python3 -c "import sys,json; acts=json.load(sys.stdin); print('run' if acts else 'no run')"
```

## Dependencies

Requires `garminconnect` (added to `pyproject.toml`). Run `uv sync` after installing to pull it in.
