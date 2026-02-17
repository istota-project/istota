# 🐙 Istota

A self-hosted AI assistant that lives in your Nextcloud. Powered by [Claude Code](https://docs.anthropic.com/en/docs/build-with-claude/claude-code), Istota is a fully featured AI agent with a curated and growing set of skills — file management, calendar, email, invoicing, accounting, web browsing, audio transcription, dev workflows, and more. It remembers things across conversations, runs scheduled jobs, and works through Nextcloud Talk or email.

## Features

- 💬 **Nextcloud Talk** — Chat with your assistant in any Talk conversation, with real-time progress updates
- 📧 **Email** — Send and receive emails, process attachments, reply in threads
- 📋 **Task files** — Drop tasks in a `TASKS.md` file and they get processed automatically
- 🗓️ **Calendar** — Read, create, and manage CalDAV events (auto-discovered from Nextcloud)
- 🧠 **Memory** — Remembers things about you across conversations (USER.md), with nightly memory extraction
- 🔍 **Semantic search** — Hybrid BM25 + vector search over past conversations and memories
- 📰 **Briefings** — Scheduled morning/evening summaries with calendar, markets, news, and TODOs
- ⏰ **Cron jobs** — Recurring scheduled tasks via `CRON.md` (AI prompts or shell commands)
- 🔧 **Curated skills** — A growing set of secure, practical skill modules loaded on demand:
  - 📂 Nextcloud file management, sharing, and organization
  - 🧾 Invoicing with PDF generation and beancount ledger integration
  - 💰 Accounting with Monarch Money sync and transaction tracking
  - 🛠️ Git/GitLab development workflows (worktrees, merge requests, credential handling)
  - 🌐 Web browsing via Dockerized Playwright with stealth mode
  - 🎙️ Audio transcription via faster-whisper (local, CPU-based)
  - 📸 OCR transcription via Tesseract
  - 📝 TODO management, notes, and script automation
- 👥 **Multi-user** — Per-user resources, worker threads, config files, and filesystem sandboxing
- 🔒 **Security** — Per-user bubblewrap sandbox, credential stripping, deferred DB writes
- 🐙 **Personality** — Customizable per-user persona (ships with a Culture drone-inspired default)

## Why Istota?

There are several Claude Code agent frameworks out there (OpenClaw and friends). Istota started in December 2025 as a Signal wrapper for Claude Code, but Signal CLI's limitations made it frustrating to use. After trying other bot frameworks with similar issues around messaging integrations, I realized Nextcloud — which I was already running for everything — was the right foundation. Nextcloud gives you granular control over what the bot can access, a solid messaging interface (Talk) where you can create separate channels for different topics and tasks, and mature iOS/Android apps with push notifications for managing things remotely.

Istota lives as a regular Nextcloud user (non-admin) on your instance, sharing a workspace folder with each user. You can also share any files or folders you want to collaborate on directly with your Istota user — it just works like sharing with any other Nextcloud user.

## Should I use Istota?

**Probably yes if** you run a homelab, already use Nextcloud (or are open to it), and want an AI assistant that integrates with your existing self-hosted setup — files, calendar, email, all in one place.

**Probably not if** your files live entirely in Google Drive or Dropbox, you want a bot with full root access to your machine, or you'd rather not run Nextcloud. Istota is opinionated about Nextcloud as the foundation — that's its strength, but it does mean you're buying into that ecosystem.

## How it works

```
Talk message ───►┐
Email ──────────►├─► SQLite queue → Scheduler → Claude Code → Response
TASKS.md ───────►│
CLI ────────────►┘
```

1. You send a message in Talk, email, or write a task in `TASKS.md`
2. The scheduler picks it up, builds a prompt with your resources, skills, memory, and conversation context
3. Claude Code executes with access to your Nextcloud files and calendar
4. The response comes back to wherever you asked

## Quick start

```bash
# Install dependencies
uv sync

# Copy and edit config
cp config/config.example.toml config/config.toml

# Initialize the database
uv run istota init

# Test with a dry run (shows the prompt without calling Claude)
uv run istota task "What's on my calendar today?" -u alice -x --dry-run

# Run the scheduler daemon
uv run istota-scheduler -d
```

## Configuration

Istota needs a Nextcloud instance and a Claude Code CLI installation. Config lives in `config/config.toml`:

```toml
[nextcloud]
url = "https://nextcloud.example.com"
username = "istota"
app_password = "xxxxx-xxxxx-xxxxx-xxxxx-xxxxx"

[talk]
enabled = true

[users.alice]
display_name = "Alice"
email_addresses = ["alice@example.com"]
```

CalDAV is auto-derived from Nextcloud credentials. See `config/config.example.toml` for all options.

Per-user config files go in `config/users/` (e.g., `config/users/alice.toml`). Users can also self-configure via files in their Nextcloud workspace (`BRIEFINGS.md`, `CRON.md`, `HEARTBEAT.md`, `PERSONA.md`, etc.).

## User workspace

Each user gets a shared Nextcloud folder with config files and bot output:

```
/Users/alice/istota/
├── config/
│   ├── USER.md          # Persistent memory (auto-loaded into prompts)
│   ├── TASKS.md         # Task queue (write tasks, bot processes them)
│   ├── PERSONA.md       # Customize bot personality
│   ├── BRIEFINGS.md     # Briefing schedule
│   ├── CRON.md          # Scheduled jobs
│   └── HEARTBEAT.md     # Health monitoring
├── exports/             # Bot-generated files
└── examples/            # Documentation
```

## Deployment

Designed to run on a Debian 13+ VM. Two deployment paths:

```bash
# Standalone install (interactive wizard)
sudo ./install.sh --interactive

# Or use the Ansible role in deploy/ansible/
```

See `deploy/README.md` for full documentation, settings file format, and Ansible usage.

## Development

```bash
uv sync                                    # Install dependencies
uv run pytest tests/ -v                    # Run tests (~1850 tests)
uv run pytest -m integration -v            # Integration tests (needs config)
uv run istota task "hello" -u alice -x     # Test execution
```

## License

[AGPL-3.0-or-later](LICENSE)
