# Configuration overview

## Config file locations

Config is loaded from the first file found in this search order:

1. `config/config.toml` (relative to working directory)
2. `~/src/config/config.toml`
3. `~/.config/istota/config.toml`
4. `/etc/istota/config.toml`

Override with `-c PATH` on the CLI.

## Structure

The config file is TOML with nested sections:

```toml
bot_name = "Istota"
db_path = "data/istota.db"

[nextcloud]
url = "https://nextcloud.example.com"
username = "istota"
app_password = "xxxxx-xxxxx-xxxxx-xxxxx-xxxxx"

[talk]
enabled = true

[email]
enabled = false

[conversation]
# ...

[scheduler]
# ...

[security]
# ...

[skills]
# ...

[sleep_cycle]
# ...

[memory_search]
# ...

[users.alice]
display_name = "Alice"
timezone = "America/New_York"
```

See the [full reference](reference.md) for all available settings.

## Per-user config

Per-user data lives in DB tables (`user_profiles`, `user_resources`, `briefing_configs`, `secrets`) populated by the matching `istota … ensure` CLI commands or the web UI. The `[users.NAME]` block in the main config is also accepted (the docker entrypoint relies on it); DB rows win at config-load time. The retired `config/users/{user_id}.toml` mechanism is gone. See [per-user configuration](per-user.md).

## Credentials

Istota has two credential tiers: **global** (bot identity, in TOML/env vars) and **per-user** (user accounts, in the encrypted `secrets` table). Instance-wide secrets can be provided as env var overrides (e.g., from systemd `EnvironmentFile=`) instead of storing them in TOML. Per-user credentials are provisioned via the web UI at `/istota/settings` or `istota secret ensure`.

See [credentials](credentials.md) for the full inventory, runtime flow, and the decision heuristic for new integrations.

## Admin users

Admin user IDs are listed in `/etc/istota/admins` (plain text, one per line, `#` comments allowed). Empty file or missing file = all users are admin (backward compatibility).

Override path via `ISTOTA_ADMINS_FILE` env var.

## CalDAV

CalDAV settings are derived from Nextcloud credentials automatically: `{url}/remote.php/dav` with the same username and app password. No separate configuration needed.

## Derived properties

| Property | Derived from |
|---|---|
| `bot_dir_name` | `bot_name` sanitized for filesystem (ASCII lowercase, spaces to underscores) |
| `caldav_url` | `nextcloud.url + /remote.php/dav` |
| `caldav_username` | `nextcloud.username` |
| `caldav_password` | `nextcloud.app_password` |
| `use_mount` | `True` if `nextcloud_mount_path` is set |
