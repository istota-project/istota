# > istota

A self-hosted AI agent that lives in your Nextcloud instance. Powered by Claude Code. ([istota.xyz](https://istota.xyz))

## Requirements

- A Nextcloud instance with a dedicated user account for the bot (if you don't have one yet, [Nextcloud All-in-One](https://github.com/nextcloud/all-in-one) is the easiest way to get started — make sure Nextcloud Talk is enabled)
- A Debian/Ubuntu VM (separate from your Nextcloud server)
- A [Claude Code](https://docs.anthropic.com/en/docs/build-with-claude/claude-code) API key or OAuth token

## Quick start

```bash
curl -fsSL https://raw.githubusercontent.com/alicekubicki/istota/main/deploy/install.sh -o install.sh
sudo bash install.sh
```

The installer walks you through connecting to Nextcloud, setting up users, and choosing optional features (email, memory search, scheduled briefings, etc.). It handles everything: system packages, Python dependencies, rclone mount, database initialization, systemd services.

After installation, authenticate the Claude CLI and invite the bot to a Talk conversation:

```bash
sudo -u istota HOME=/srv/app/istota claude login
```

To update an existing installation (pull latest code, regenerate config, restart):

```bash
sudo bash install.sh --update
```

Preview what the installer would generate without making changes:

```bash
bash deploy/install.sh --dry-run
```

An Ansible role is also available at `deploy/ansible/` for infrastructure-as-code deployments.

## How it works

```
Talk message ──>┐
Email ─────────>├──> SQLite queue -> Scheduler -> Claude Code -> Response
TASKS.md ──────>│
CLI ───────────>┘
```

Messages arrive through Talk polling, IMAP, TASKS.md file watching, or the CLI. The scheduler claims tasks from a SQLite queue, builds a prompt with the user's resources, skills, memory, and conversation context, then invokes Claude Code in a sandbox. Responses go back through the same channel.

Per-user worker threads handle concurrency — foreground tasks (chat) and background tasks (scheduled jobs, briefings) run on separate pools so a long-running job never blocks a conversation.

## Features

**Messaging** — Nextcloud Talk (DMs and multi-user rooms with @mention support), email (IMAP/SMTP with threading), TASKS.md file polling, CLI.

**Skills** — Loaded on demand based on prompt keywords, resource types, and source types. Ships with: Nextcloud file management, CalDAV calendar, email, web browsing (Dockerized Playwright), git/GitLab/GitHub development workflows, beancount accounting with invoicing, Karakeep bookmarks, voice transcription (faster-whisper), OCR (Tesseract), RSS/Atom/Tumblr/Are.na feeds, and more. Skills are a curated standard library rather than a plugin marketplace.

**Memory** — Per-user persistent memory (USER.md, auto-loaded into prompts), per-channel memory (CHANNEL.md), and dated memory files from nightly extraction. Hybrid BM25 + vector search (sqlite-vec, MiniLM) across conversations and memory files.

**Scheduling** — Cron jobs via CRON.md (AI prompts or shell commands), natural-language reminders as one-shot cron entries, scheduled briefings with calendar/markets/news/todos components, invoice generation schedules.

**Briefings** — Configurable morning/evening summaries. Components include calendar events, market data (futures, indices via yfinance + FinViz), news from RSS feeds, todos, email summaries, notes, and reminders. Output to Talk, email, or both.

**Heartbeat monitoring** — User-defined health checks: file age, shell commands, URL health, calendar conflicts, task deadlines, and system self-checks. Cooldowns, quiet hours, and per-check intervals.

**Multi-user** — Per-user config files, resource permissions, worker pools, and filesystem sandboxing. Admin/non-admin isolation. Each user gets their own Nextcloud workspace with config files, exports, and memory. Multiple bot instances can coexist on the same Nextcloud — each runs as its own Nextcloud user with a separate namespace, and they can interact with each other through Talk rooms like any other participant.

**Security** — Bubblewrap sandbox per invocation (PID namespace, restricted mounts, credential isolation). Non-admin users can't see the database, other users' files, or system config. Deferred DB writes via JSON files for sandboxed operations. Credential stripping from subprocess environments.

**Constitution** — An [Emissaries](https://commontask.org/emissaries/) layer defines how the agent reasons about data, handles the boundary between private and public action, and what it owes to people beyond its operator. Customizable per-user persona on top.

## User workspace

Each user gets a shared Nextcloud folder:

```
/Users/alice/istota/
├── config/
│   ├── USER.md          # Persistent memory
│   ├── TASKS.md         # File-based task queue
│   ├── PERSONA.md       # Personality customization
│   ├── BRIEFINGS.md     # Briefing schedule
│   ├── CRON.md          # Scheduled jobs
│   └── HEARTBEAT.md     # Health monitoring config
├── exports/             # Bot-generated files
└── examples/            # Reference documentation
```

## Development

```bash
uv sync                                    # Install dependencies
uv run pytest tests/ -v                    # Run tests (~2275 unit tests)
uv run pytest -m integration -v            # Integration tests (needs live config)
uv run istota task "hello" -u alice -x     # Test execution
```

Optional dependency groups:

```bash
uv sync --extra memory-search    # sqlite-vec + sentence-transformers for semantic search
uv sync --extra whisper          # faster-whisper for audio transcription
```

## Further reading

- [ARCHITECTURE.md](ARCHITECTURE.md) — detailed system architecture and design decisions
- [DEVLOG.md](DEVLOG.md) — detailed changelog

## License

[MIT](LICENSE)

***
© 2026 [Stefan Kubicki](https://kubicki.org) • a [CYNIUM](https://cynium.com) release • shipped from the [Atoll](https://kubicki.org/atoll)
***
Canonical URL: https://forge.example.com/alice/istota
