# > istota

A self-hosted AI agent that lives in your Nextcloud instance. Powered by Claude Code. ([istota.xyz](https://istota.xyz))

## Quick start (Docker)

> **Experimental.** The Docker deployment is functional but should be considered unstable. The stable, canonical deployment method is to a dedicated Debian/Ubuntu VM using the [install script](#quick-start-bare-metal) or the Ansible role at `deploy/ansible/`.

The Docker setup spins up a complete stack from scratch: Postgres, Redis, a fresh Nextcloud instance, and the Istota scheduler. If you already have a Nextcloud instance, skip to [bare metal](#quick-start-bare-metal) — the Docker Compose creates its own Nextcloud and is meant for evaluation or standalone deployments, not for connecting to an existing one.

### 1. Configure

```bash
cd docker
cp .env.example .env
```

Edit `.env` and set at minimum:

- `CLAUDE_CODE_OAUTH_TOKEN` — generate one with `claude setup-token` (or set `ANTHROPIC_API_KEY` for direct API access)
- `ADMIN_PASSWORD`, `POSTGRES_PASSWORD`, `BOT_PASSWORD`, `USER_PASSWORD` — pick something real
- `USER_NAME` — your Nextcloud username

Optional but recommended:

- `USER_DISPLAY_NAME` — your full name
- `USER_TIMEZONE` — e.g. `America/New_York` (defaults to UTC)
- `USER_EMAIL` — enables email-related features

### 2. Start

```bash
docker compose up -d
```

First start takes a few minutes: Nextcloud initializes the database, creates user accounts, installs apps (Talk, Calendar, External Storage), sets up shared folders, and creates a Talk room between you and the bot. The Istota container waits for all of this before starting the scheduler.

### 3. Chat

Open `http://localhost:8080`, log in with your `USER_NAME` / `USER_PASSWORD`, go to Talk, and start chatting. The bot responds through the same Talk interface.

### Optional services

The browser container (Playwright with bot-detection countermeasures) and the GPS webhook receiver run as Docker Compose profiles:

```bash
docker compose --profile browser up -d              # Add browser
docker compose --profile location up -d             # Add GPS webhook receiver
docker compose --profile browser --profile location up -d  # Both
```

The browser container requires an x86-64 host (Chrome has no ARM packages).

### Configuration

The `.env` file exposes most of the same settings available in the Ansible role: scheduler intervals, conversation context tuning, progress updates, sleep cycle, memory search, email (IMAP/SMTP), ntfy notifications, developer skill (GitLab/GitHub), and per-user overrides. See `.env.example` for the full list with defaults.

The config file at `/data/config/config.toml` inside the container is generated on first start and not overwritten on subsequent starts. To change settings after initial setup, either delete the config and restart (it regenerates from env vars), or edit it directly:

```bash
docker compose exec istota vi /data/config/config.toml
docker compose restart istota
```

### Differences from bare metal

The Docker deployment differs from a bare metal / Ansible installation in a few ways:

- **No network proxy.** The CONNECT-based network proxy (domain allowlist) is disabled — Docker's own network isolation serves the same purpose. Bubblewrap filesystem sandboxing and the skill credential proxy are enabled and work inside the container (bubblewrap gracefully degrades if the kernel doesn't allow user namespaces — add `--cap-add SYS_ADMIN` to the istota service if needed).
- **Single user.** The Docker setup provisions one human user. Additional users can be added by editing `config.toml` directly and creating them in Nextcloud.
- **Bundled Nextcloud.** The Compose file creates a new Nextcloud instance. If you already run Nextcloud, use the bare metal installer or Ansible role instead — they connect to your existing instance without creating a second one.
- **No backups or auto-update.** The Ansible role sets up cron-based DB backups and optional auto-update. In Docker, volume backups are your responsibility.
- **All Python extras installed.** The Docker image includes every optional dependency (whisper, memory-search, etc.) so all skills are available without rebuilding.

## Quick start (bare metal)

Requirements: a Nextcloud instance, a Debian/Ubuntu VM, and a Claude Code OAuth token.

```bash
curl -fsSL https://raw.githubusercontent.com/muinyc/istota/main/deploy/install.sh -o install.sh
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

**Skills** — Loaded on demand based on prompt keywords, resource types, and source types. Ships with: Nextcloud file management, CalDAV calendar, email, web browsing (Dockerized Playwright with bot-detection countermeasures), git/GitLab/GitHub workflows, accounting via Moneyman API (ledger, invoicing, transactions), GPS location tracking (Overland), Karakeep bookmarks, voice transcription (faster-whisper), OCR (Tesseract), Miniflux RSS feed management, and more. Skills are a curated standard library, not a plugin marketplace.

**Memory** — Per-user persistent memory (USER.md, auto-loaded into prompts), per-channel memory (CHANNEL.md), dated memory files from nightly extraction, and BM25 auto-recall. Temporal knowledge graph stores structured facts as entity-relationship triples with validity windows — freeform predicates, automatic supersession for single-valued relations, fuzzy dedup. Configurable memory cap to limit total prompt size. Hybrid BM25 + vector search (sqlite-vec, MiniLM) across conversations and memory files.

**Scheduling** — Cron jobs via CRON.md (AI prompts or shell commands), natural-language reminders as one-shot cron entries, scheduled briefings with calendar/markets/headlines/news/todos components.

**Briefings** — Configurable morning/evening summaries. Components include calendar events, market data (futures, indices via yfinance + FinViz), headlines (pre-fetched frontpages from AP, Reuters, Guardian, FT, Al Jazeera, Le Monde, Der Spiegel), email newsletter digests, todos, and reminders. Output to Talk, email, or both.

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

Istota is built around Nextcloud — it uses your files, calendars, contacts, and chat directly rather than wrapping them in API adapters. This tight integration is by design: your assistant lives where your data already is. That said, like any agent, it can be extended with skills to connect to Google Workspace, Microsoft 365, or other services as needed.

## Vs OpenClaw

[OpenClaw](https://github.com/openclaw/openclaw) is the most popular open-source AI agent project. Both are self-hosted AI assistants, but they make different design choices.

| | Istota | OpenClaw |
|---|---|---|
| Architecture | Server daemon with SQLite task queue and per-user worker pool | Long-running Node.js gateway with WebSocket control plane |
| LLM | Claude Code (subprocess) | Multi-provider (Claude, GPT, DeepSeek, Ollama) |
| Messaging | Nextcloud Talk + email | WhatsApp, Telegram, Slack, Discord, Signal, Teams, Matrix, and more |
| Multi-user | Native: per-user config, resources, sandboxing, worker isolation | Single-user per instance; run multiple containers for multiple users |
| Storage | Nextcloud (WebDAV/rclone mount), includes files, calendars, contacts | Local filesystem |
| Memory | USER.md + dated memories + channel memory + knowledge graph (temporal triples) + nightly curation + BM25/vector search + memory cap | Daily logs + MEMORY.md + hybrid search + pre-compaction flush |
| Scheduling | CRON.md + briefings + heartbeats | Built-in cron + webhooks + Gmail Pub/Sub |
| Skills | ~20 built-in skills with YAML frontmatter manifests, two-pass selection (keyword + semantic) | 5,700+ community skills via ClawHub registry, three tiers |
| Security | Bubblewrap filesystem sandbox, credential stripping, admin/non-admin isolation, deferred DB writes | DM pairing policy; community skills are an acknowledged risk vector |
| Voice | Whisper transcription (input only) | ElevenLabs TTS + always-on speech wake |
| Browser | Dockerized Playwright with bot-detection countermeasures | Built-in Chrome DevTools Protocol |
| Companion apps | None (Nextcloud has its own web and mobile apps) | Native macOS, iOS, and Android apps |
| Language | Python | TypeScript/Node.js |
| License | MIT | MIT |

OpenClaw connects to many messaging platforms and has a large community skill registry. Istota talks through Nextcloud and ships a smaller set of built-in skills with filesystem sandboxing and multi-user isolation.

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
uv sync --extra all                        # Install all dependencies
uv run pytest tests/ -v                    # Run tests (~2960 unit tests)
uv run pytest -m integration -v            # Integration tests (needs live config)
uv run istota task "hello" -u alice -x     # Test execution
```

Most skill dependencies are optional. Install everything with `--extra all`, or pick individual groups:

```bash
uv sync --extra calendar         # caldav + icalendar
uv sync --extra email            # imap-tools
uv sync --extra markets          # yfinance
uv sync --extra transcribe       # pytesseract + Pillow (OCR)
uv sync --extra memory-search    # sqlite-vec + sentence-transformers for semantic search
uv sync --extra whisper          # faster-whisper for audio transcription
uv sync --extra location         # fastapi + uvicorn + geopy for GPS location receiver
```

Skills with missing dependencies are automatically excluded from prompt selection. Use `!skills` in Talk to see which are available.

## Further reading

- [Documentation](https://istota.xyz/docs) — full docs (also buildable locally with `mkdocs serve`)
- [DEVLOG.md](DEVLOG.md) — changelog

## License

[MIT](LICENSE)

***
© 2026 [Stefan Kubicki](https://kubicki.org) • a [CYNIUM](https://cynium.com) release • shipped from the [Atoll](https://kubicki.org/atoll)
***
Canonical URL: https://forge.cynium.com/cynium/istota
