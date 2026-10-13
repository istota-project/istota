# Configuration overview

## Config file locations

`ISTOTA_CONFIG_PATH` wins over the search order below — it is how a subprocess resolves the same config the daemon loaded rather than searching afresh. It does not beat an explicit `-c PATH`, and a path that does not exist is ignored rather than fatal, falling through to the search. Config is otherwise loaded from the first file found in this order:

1. `config/config.toml` (relative to working directory)
2. `~/src/config/config.toml`
3. `~/.config/istota/config.toml`
4. `/etc/istota/config.toml`

Override with `-c PATH` on the CLI.

On the stack the file is `/srv/app/istota/config/config.toml` on the VM, which the containers see as `/data/config/config.toml` through `ISTOTA_CONFIG_PATH`. `istota setup` writes it once, and the running services mount it read-only. Change it from the [web config editor](web-editor.md), with `istota-stack apply PLAN` (an `istota apply` plan), or by running `istota-stack setup --force` again; see the [CLI reference](../reference/cli.md#istota-stack-on-the-vm).

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

Per-user data lives in DB tables (`user_profiles`, `user_resources`, `briefing_configs`, `secrets`) populated by the matching `istota … ensure` CLI commands or the web UI. The `[users.NAME]` block in the main config is also accepted (`istota setup` writes one for the first admin, which seeds that user's profile on first boot); DB rows win at config-load time. The retired `config/users/{user_id}.toml` mechanism is gone. See [per-user configuration](per-user.md).

## Credentials

Istota has two credential tiers: **global** (bot identity, in TOML/env vars) and **per-user** (user accounts, in the encrypted `secrets` table). Instance-wide secrets can be provided as env var overrides instead of storing them in TOML; on the stack each is a file under `/srv/app/istota/secrets/` that the container exports under its override name. Per-user credentials are provisioned via the web UI at `/istota/settings` or `istota secret ensure`.

See [credentials](credentials.md) for the full inventory, runtime flow, and the decision heuristic for new integrations.

## Admin users

Admin user IDs are listed in the admins file: plain text, one per line, `#` comments allowed. On the stack it is `/srv/app/istota/config/admins` on the VM, `/data/config/admins` in the containers, and `istota setup` creates it naming the first admin. Elsewhere it is `ISTOTA_ADMINS_FILE`, or `/etc/istota/admins` when that is unset. An empty or missing file makes the sole user an admin on a single-user install. With two or more users it makes nobody an admin, and `istota doctor` fails `security.admins` until the file names someone. The web admin pages always treat it as no admins at all.

Override path via `ISTOTA_ADMINS_FILE` env var.

## CalDAV

By default CalDAV settings are derived from Nextcloud credentials: `{url}/remote.php/dav` with the same username and app password. Nothing to configure in the common case.

An explicit `[caldav]` section overrides that derivation, which is what an install without Nextcloud uses to reach an external CalDAV server:

```toml
[caldav]
url = "https://caldav.example.com/dav"
username = "alice"
password = ""    # or via the encrypted secrets store
```

## Derived properties

| Property | Derived from |
|---|---|
| `bot_dir_name` | `bot_name` sanitized for filesystem (ASCII lowercase, spaces to underscores) |
| `caldav_url` | `[caldav] url` if set, else `nextcloud.url + /remote.php/dav` |
| `caldav_username` | `[caldav] username` if set, else `nextcloud.username` |
| `caldav_password` | `[caldav] password` if set, else `nextcloud.app_password` |
| `has_workspace` | `True` if `workspace_path` is set |
