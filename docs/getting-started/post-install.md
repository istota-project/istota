# Post-install

After the [quickstart](quickstart-server.md), complete these steps on the machine, as root. `istota` there runs the CLI inside the daemon container. On the [local VM](local-install.md), run them on the Mac as yourself: the Mac's `istota-stack` and `istota` forward each command into the VM.

:::tip[Phase 6 — DB-backed user profiles]
Profile fields (display_name, timezone, email addresses, log/alerts channels, worker overrides, trusted email senders, disabled skills) live in the `user_profiles` SQLite table. They're auto-seeded the first time a user logs into the web UI, or you can pre-seed them with the CLI (the [Ansible management role](../deployment/ansible.md) runs the same command):
```bash
istota user ensure --name alice --display-name "Alice" \
  --tz "America/Los_Angeles" --email alice@example.com
```
The web UI at `/istota/settings` lets each user edit their own profile and add per-user resources (calendars, folders, modules) without touching `config.toml`. Operator-supplied resources from `config.toml` show up alongside as read-only.
:::

## Claude authentication

`istota setup` collects a long-lived Claude Code OAuth token (the "Claude Code OAuth token" prompt, which writes it to `secrets/claude_code_oauth_token`), and the entrypoint provisions the credentials file for you. Generate the token with `claude setup-token`. If you supplied it during install, Claude auth is already done — skip this section.

Only if you skipped the token at install time (and aren't using `ANTHROPIC_API_KEY` or a native-brain provider key), authenticate manually:

```bash
istota-stack exec claude login
```

## Open the web app

Open the hostname you gave the wizard (`http://localhost:8080` in a Lima VM) and sign in. Web chat works with or without Nextcloud.

## Invite the bot to Talk

With Nextcloud integration, open Talk and add the bot's user (default: `istota`) to a conversation. The bot automatically starts polling conversations it's a member of.

## Test execution

```bash
istota task "Hello" -u alice -x
```

## Service management

```bash
systemctl status istota-stack
istota-stack down && istota-stack up
istota-stack compose logs -f daemon
```

## User workspace

After the bot runs for the first time for a user, it creates a directory structure in the workspace: the Nextcloud mount with Nextcloud integration, `/data/workspace` in the state directory with local storage.

```
/Users/alice/
├── istota/              # Shared with user via OCS
│   ├── config/
│   │   ├── USER.md          # Persistent memory
│   │   ├── TASKS.md         # File-based task queue
│   │   ├── CRON.md          # Scheduled jobs
│   │   └── HEARTBEAT.md     # Health monitoring config
│   ├── exports/             # Bot-generated files
│   ├── scripts/             # User's reusable Python scripts
│   └── examples/            # Reference documentation
├── inbox/               # Files from email attachments
├── memories/            # Dated memories from sleep cycle (YYYY-MM-DD.md)
└── shared/              # Auto-organized files shared with bot
```

With Nextcloud, users can edit these files through the Nextcloud web UI, desktop client, or any text editor with WebDAV access. See [per-user configuration](../configuration/per-user.md) for details on each file.

## What to try next

- Send a message in web chat, or in Talk, and watch the bot respond
- Edit `PERSONA.md` at the root of the bot's files (beside `Users/`) to change the bot's personality for everyone; see [Persona](../configuration/persona.md)
- Set up a [briefing](../features/briefings.md) for morning summaries
- Configure [scheduled jobs](../features/scheduling.md) via CRON.md
- Check out the [skills index](../reference/skills-index.md) to see what the bot can do
