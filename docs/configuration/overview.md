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

## Per-user config files

In addition to the `[users.NAME]` section in the main config, per-user config files can be placed at `config/users/{user_id}.toml`. These take precedence over the main config. See [per-user configuration](per-user.md).

## Secret env var overrides

Secrets can be provided via environment variables (e.g., from systemd `EnvironmentFile=`), which override the TOML values:

| Env var | Config field |
|---|---|
| `ISTOTA_NC_APP_PASSWORD` | `nextcloud.app_password` |
| `ISTOTA_IMAP_PASSWORD` | `email.imap_password` |
| `ISTOTA_SMTP_PASSWORD` | `email.smtp_password` |
| `ISTOTA_GITLAB_TOKEN` | `developer.gitlab_token` |
| `ISTOTA_GITHUB_TOKEN` | `developer.github_token` |
| `ISTOTA_NTFY_TOKEN` | `ntfy.token` |
| `ISTOTA_NTFY_PASSWORD` | `ntfy.password` |

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
