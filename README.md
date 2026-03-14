# > istota

A self-hosted AI agent that lives in your Nextcloud instance. Powered by Claude Code. ([istota.xyz](https://istota.xyz))

## Quick start (Docker)

```bash
cd docker
cp .env.example .env
# Edit .env: set CLAUDE_CODE_OAUTH_TOKEN and passwords
docker compose up -d
```

Nextcloud at `http://localhost:8080`. Log in with the credentials you set in `.env`, open Talk, and start chatting. Everything is provisioned automatically: Nextcloud users, Talk rooms, shared folders, and the Istota scheduler.

## Quick start (bare metal)

Requirements: a Nextcloud instance, a Debian/Ubuntu VM, and a Claude Code OAuth token.

```bash
curl -fsSL https://raw.githubusercontent.com/stefankubicki/istota/main/deploy/install.sh -o install.sh
sudo bash install.sh
```

The installer walks you through connecting to Nextcloud, setting up users, and choosing optional features. After installation:

```bash
sudo -u istota HOME=/srv/app/istota claude login
```

To update: `sudo bash install.sh --update`. An Ansible role is also available at `deploy/ansible/`.

## How it works

```
Talk message ──>┐
Email ─────────>├──> SQLite queue -> Scheduler -> Claude Code -> Response
TASKS.md ──────>│
CLI ───────────>┘
```

Messages arrive through Talk polling, IMAP, TASKS.md file watching, or the CLI. The scheduler claims tasks from a SQLite queue, builds a prompt with the user's resources, skills, memory, and conversation context, then invokes Claude Code in a sandbox. Responses go back through the same channel.

Per-user worker threads handle concurrency. Foreground tasks (chat) and background tasks (scheduled jobs, briefings) run on separate pools so a long-running job never blocks a conversation.

## Features

**Messaging** — Nextcloud Talk (DMs and multi-user rooms with @mention support), email (IMAP/SMTP with threading), TASKS.md file polling, CLI.

**Skills** — Loaded on demand based on prompt keywords, resource types, and source types. Ships with: Nextcloud file management, CalDAV calendar, email, web browsing (Dockerized Playwright with bot-detection countermeasures), git/GitLab/GitHub workflows, beancount accounting with invoicing, GPS location tracking (Overland), Garmin Connect fitness data, Karakeep bookmarks, voice transcription (faster-whisper), OCR (Tesseract), RSS/Atom/Tumblr/Are.na feeds, and more. Skills are a curated standard library, not a plugin marketplace.

**Memory** — Per-user persistent memory (USER.md, auto-loaded into prompts), per-channel memory (CHANNEL.md), dated memory files from nightly extraction, and BM25 auto-recall. Configurable memory cap to limit total prompt size. Hybrid BM25 + vector search (sqlite-vec, MiniLM) across conversations and memory files.

**Scheduling** — Cron jobs via CRON.md (AI prompts or shell commands), natural-language reminders as one-shot cron entries, scheduled briefings with calendar/markets/news/todos components, invoice generation schedules.

**Briefings** — Configurable morning/evening summaries. Components include calendar events, market data (futures, indices via yfinance + FinViz), news from RSS feeds, todos, email summaries, and reminders. Output to Talk, email, or both.

**Heartbeat monitoring** — User-defined health checks: file age, shell commands, URL health, calendar conflicts, task deadlines, and system self-checks. Cooldowns, quiet hours, and per-check intervals.

**Multi-user** — Per-user config files, resource permissions, worker pools, and filesystem sandboxing. Admin/non-admin isolation. Each user gets their own Nextcloud workspace with config files, exports, and memory. Multiple bot instances can coexist on the same Nextcloud, each running as its own Nextcloud user with a separate namespace, and they can interact with each other through Talk rooms like any other participant.

**Security** — Bubblewrap sandbox per invocation (PID namespace, restricted mounts, credential isolation). Non-admin users can't see the database, other users' files, or system config. Deferred DB writes via JSON files for sandboxed operations. Credential stripping from subprocess environments.

**Constitution** — An [Emissaries](https://commontask.org/emissaries/) layer defines how the agent reasons about data, handles the boundary between private and public action, and what it owes to people beyond its operator. Per-user persona customization sits on top.

## Why Nextcloud?

Most AI assistant projects treat infrastructure as someone else's problem. They connect to third-party APIs for storage, calendars, contacts, and messaging, accumulating credentials and vendor dependencies. Istota takes a different approach: it lives inside a Nextcloud instance as a regular user.

The bot gets files, calendars, contacts, Talk messaging, and sharing through the same protocols every other Nextcloud user uses. File sharing works by sharing a folder with the bot's user account. Calendar access works through standard CalDAV. Talk conversations work through the regular user API. No webhooks, no OAuth apps, no server plugins.

In practice this means:

- **Zero Nextcloud configuration.** Create a user account, invite it to a chat. No admin panel changes, no app installation, no API tokens on the Nextcloud side.
- **File sharing is native.** Users share files with the bot the same way they share with colleagues. The bot shares files back the same way. Permissions, links, and access control are handled by Nextcloud.
- **Multi-user comes free.** Nextcloud already handles user isolation, file ownership, and access control. Istota inherits all of it rather than reimplementing it.
- **Self-hosted end to end.** Your data stays on your Nextcloud server and the VM running Istota. No external services required beyond the Claude API.
- **User self-service.** Config files (persona, briefings, cron jobs, heartbeat checks) live in the user's shared Nextcloud folder. Users edit them with any text editor or the Nextcloud web UI, no CLI access needed.

The tradeoff is that Istota only works with Nextcloud. If you use Google Workspace or Microsoft 365, this isn't for you. If you already run Nextcloud (or are willing to), you get an assistant that uses your existing infrastructure directly rather than wrapping it in API adapters.

## Vs OpenClaw

[OpenClaw](https://github.com/openclaw/openclaw) is the most popular open-source AI agent project. Both are self-hosted AI assistants, but they make different design choices.

| | Istota | OpenClaw |
|---|---|---|
| Architecture | Server daemon with SQLite task queue and per-user worker pool | Long-running Node.js gateway with WebSocket control plane |
| LLM | Claude Code (subprocess) | Multi-provider (Claude, GPT, DeepSeek, Ollama) |
| Messaging | Nextcloud Talk + email | WhatsApp, Telegram, Slack, Discord, Signal, Teams, Matrix, and more |
| Multi-user | Native: per-user config, resources, sandboxing, worker isolation | Single-user per instance; run multiple containers for multiple users |
| Storage | Nextcloud (WebDAV/rclone mount), includes files, calendars, contacts | Local filesystem |
| Memory | USER.md + dated memories + channel memory + nightly curation + BM25/vector search + memory cap | Daily logs + MEMORY.md + hybrid search + pre-compaction flush |
| Scheduling | CRON.md + briefings + heartbeats + invoice schedules | Built-in cron + webhooks + Gmail Pub/Sub |
| Skills | ~23 built-in Python CLIs with TOML manifests, keyword-based selection | 5,700+ community skills via ClawHub registry, three tiers |
| Security | Bubblewrap filesystem sandbox, credential stripping, admin/non-admin isolation, deferred DB writes | DM pairing policy; community skills are an acknowledged risk vector |
| Voice | Whisper transcription (input only) | ElevenLabs TTS + always-on speech wake |
| Browser | Dockerized Playwright with bot-detection countermeasures | Built-in Chrome DevTools Protocol |
| Companion apps | None (Nextcloud has its own web and mobile apps) | Native macOS, iOS, and Android apps |
| Language | Python | TypeScript/Node.js |
| License | MIT | MIT |

OpenClaw is a consumer-oriented personal assistant with broad channel support, voice interaction, native companion apps, and a large community skill ecosystem. It works well as a single user's always-available AI across many messaging platforms.

Istota is a server-oriented system for households and small teams that already run Nextcloud. It trades channel breadth for direct Nextcloud integration, native multi-user isolation, and operational features (task queuing, scheduling, monitoring, sandboxing) built for unattended long-running operation.

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
│   ├── HEARTBEAT.md     # Health monitoring config
│   └── LOCATION.md      # GPS location tracking config
├── exports/             # Bot-generated files
└── examples/            # Reference documentation
```

## Development

```bash
uv sync --extra all                        # Install all dependencies
uv run pytest tests/ -v                    # Run tests (~2500 unit tests)
uv run pytest -m integration -v            # Integration tests (needs live config)
uv run istota task "hello" -u alice -x     # Test execution
```

Most skill dependencies are optional. Install everything with `--extra all`, or pick individual groups:

```bash
uv sync --extra calendar         # caldav + icalendar
uv sync --extra email            # imap-tools
uv sync --extra markets          # yfinance
uv sync --extra accounting       # beancount + fava + weasyprint + monarchmoney
uv sync --extra transcribe       # pytesseract (OCR)
uv sync --extra garmin           # garminconnect
uv sync --extra memory-search    # sqlite-vec + sentence-transformers for semantic search
uv sync --extra whisper          # faster-whisper for audio transcription
uv sync --extra location         # fastapi + uvicorn + geopy for GPS location receiver
```

Skills with missing dependencies are automatically excluded from prompt selection. Use `!skills` in Talk to see which are available.

## Further reading

- [ARCHITECTURE.md](ARCHITECTURE.md) — system architecture and design decisions
- [DEVLOG.md](DEVLOG.md) — changelog

## License

[MIT](LICENSE)

***
© 2026 [Stefan Kubicki](https://kubicki.org) • a [CYNIUM](https://cynium.com) release • shipped from the [Atoll](https://kubicki.org/atoll)
***
Canonical URL: https://forge.cynium.com/cynium/istota
