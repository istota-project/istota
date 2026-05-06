# Post-install

After deploying via [Docker](quickstart-docker.md) or [bare metal](quickstart-bare-metal.md), complete these steps.

!!! tip "Phase 6 — DB-backed user profiles"
    Profile fields (display_name, timezone, email addresses, log/alerts channels, ntfy topic, worker overrides, trusted email senders, disabled skills) live in the `user_profiles` SQLite table. They're auto-seeded the first time a user logs into the web UI, or you can pre-seed via Ansible:
    ```bash
    istota user ensure --name alice --display-name "Alice" \
      --tz "America/Los_Angeles" --email alice@example.com
    ```
    The web UI at `/istota/settings` lets each user edit their own profile and add per-user resources (calendars, folders, modules) without touching `config.toml`. Operator-supplied resources from `config.toml` show up alongside as read-only.

## Authenticate Claude CLI

=== "Bare metal"

    ```bash
    sudo -u istota HOME=/srv/app/istota claude login
    ```

=== "Docker"

    ```bash
    docker compose exec istota claude login
    ```

## Invite the bot to Talk

In Nextcloud, open Talk and add the bot's user (default: `istota`) to a conversation. The bot automatically starts polling conversations it's a member of.

## Test execution

=== "Bare metal"

    ```bash
    sudo -u istota HOME=/srv/app/istota \
      /srv/app/istota/.venv/bin/istota task "Hello" -u alice -x
    ```

=== "Docker"

    ```bash
    docker compose exec istota istota task "Hello" -u alice -x
    ```

## Service management (bare metal)

```bash
systemctl status istota-scheduler
systemctl restart istota-scheduler
journalctl -u istota-scheduler -f
```

## User workspace

After the bot runs for the first time for a user, it creates a directory structure under the Nextcloud mount:

```
/Users/alice/
├── istota/              # Shared with user via OCS
│   ├── config/
│   │   ├── USER.md          # Persistent memory
│   │   ├── TASKS.md         # File-based task queue
│   │   ├── PERSONA.md       # Personality customization
│   │   ├── BRIEFINGS.md     # Briefing schedule
│   │   ├── CRON.md          # Scheduled jobs
│   │   └── HEARTBEAT.md     # Health monitoring config
│   ├── exports/             # Bot-generated files
│   ├── scripts/             # User's reusable Python scripts
│   └── examples/            # Reference documentation
├── inbox/               # Files from email attachments
├── memories/            # Dated memories from sleep cycle (YYYY-MM-DD.md)
└── shared/              # Auto-organized files shared with bot
```

Users can edit these files through the Nextcloud web UI, desktop client, or any text editor with WebDAV access. See [per-user configuration](../configuration/per-user.md) for details on each file.

## What to try next

- Send a message in Talk and watch the bot respond
- Edit `PERSONA.md` to customize the bot's personality
- Set up a [briefing](../features/briefings.md) for morning summaries
- Configure [scheduled jobs](../features/scheduling.md) via CRON.md
- Check out the [skills index](../reference/skills-index.md) to see what the bot can do
