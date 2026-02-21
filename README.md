# 🐙 Istota

A self-hosted AI assistant that lives in your Nextcloud. Powered by [Claude Code](https://docs.anthropic.com/en/docs/build-with-claude/claude-code), it handles files, calendar, email, invoicing, accounting, web browsing, audio transcription, dev workflows, and a growing list of other things. It remembers context across conversations, runs scheduled jobs, sets reminders, generates briefings, and talks to you through Nextcloud Talk or email.

## Features

- 💬 **Nextcloud Talk** — Chat with your assistant in any Talk conversation, with real-time progress updates
- 📧 **Email** — Send and receive emails, process attachments, reply in threads
- 📋 **Task files** — Drop tasks in a `TASKS.md` file and they get processed automatically
- 🗓️ **Calendar** — Read, create, and manage CalDAV events (auto-discovered from Nextcloud)
- 🧠 **Memory** — Remembers things about you across conversations (USER.md), with nightly memory extraction
- 🔍 **Semantic search** — Hybrid BM25 + vector search over past conversations and memories
- 📰 **Briefings** — Scheduled morning/evening summaries with calendar, markets, news, and TODOs
- 📡 **Feed reader** — Aggregate RSS, Tumblr, and Are.na feeds into a static HTML page with image galleries and lightbox
- ⏰ **Cron jobs** — Recurring scheduled tasks via `CRON.md` (AI prompts or shell commands)
- 🔔 **Reminders** — Natural language reminders ("remind me in 2 hours") via one-shot cron entries with @mention notifications
- 🔧 **Curated skills** — A growing set of secure, practical skill modules loaded on demand:
  - 📂 Nextcloud file management, sharing, and organization
  - 🧾 Invoicing with PDF generation and beancount ledger integration
  - 💰 Accounting with Monarch Money sync and transaction tracking
  - 🛠️ Git/GitLab/GitHub development workflows (worktrees, merge requests, pull requests)
  - 🌐 Web browsing via Dockerized Playwright with stealth mode
  - 🎙️ Audio transcription via faster-whisper (local, CPU-based)
  - 📸 OCR transcription via Tesseract
  - 🔖 Bookmark management via Karakeep (search, save, tag)
  - 📝 TODO management, notes, and script automation
- 👥 **Multi-user** — Per-user resources, worker threads, config files, and filesystem sandboxing
- 🔒 **Security** — Per-user bubblewrap sandbox, credential stripping, deferred DB writes
- 🐙 **Personality** — Constitutional principles layer (emissaries) plus customizable per-user persona (ships with a Culture drone-inspired default)

## Why Istota?

Istota started in December 2025 as a thin wrapper around Claude Code so I could do development on the go without having to SSH into a VM from my phone like an insane person.

The first version used Signal as the messaging layer, but Signal CLI's quirks and the dependency on an external messaging platform made me look elsewhere. After trying other bot frameworks with similar issues around messaging integrations, I realized Nextcloud — which I was already running for everything — was the right foundation. As a mild form of claude psychosis set in, what was meant to be a mobile-friendly wrapper for Claude Code turned into a fully featured personal operating system with its own skill system, memory, scheduling, and multi-user support.

Nextcloud gives you granular control over what the bot can access, a solid messaging interface (Talk) where you can create separate channels for different topics and tasks, and mature iOS/Android apps with push notifications for managing things remotely. Istota lives as a regular Nextcloud user (non-admin) on your instance, sharing a workspace folder with each user. You can also share any files or folders you want to collaborate on directly with your Istota user — works the same as sharing with any other Nextcloud user.

## Should I try Istota?

**Yes** if you run a homelab, already use Nextcloud (or want to try it) and want an AI assistant that integrates with your existing self-hosted setup — files, calendar, email, but without the YOLO approach of some other options.

**Probably not** if your files live entirely in Google Drive or Dropbox, you want a bot with full unhindered root access to your machine, or you'd rather not use Nextcloud. Istota is (for now, at least) opinionated about Nextcloud as the foundation — that's its strength, but it does mean you're expected to drink the kool-aid if you haven't already.

## Security model

Istota runs on its own VM, separate from your Nextcloud server — it never touches your Nextcloud database or other users' files. It connects as a regular non-admin Nextcloud user that can only see its own stuff and whatever you explicitly share with it.

Each Claude Code invocation runs inside a [bubblewrap](https://github.com/containers/bubblewrap) sandbox (the same tool Claude Code itself uses on Linux). No root access, no visibility into system files it doesn't need, private PID namespace. Only the bare minimum gets mounted: system libraries (read-only), Python runtime (read-only), and the user's own Nextcloud subtree (read-write). Everything else — database, other users' directories, config files, credentials — is hidden behind tmpfs.

In a multi-user setup, each user gets their own sandbox. Non-admin users can only see their own files, can't touch the database, and can't spawn subtasks. Admin users get broader access but are still sandboxed. Credentials are stripped from the subprocess environment, and any DB writes the agent needs go through a deferred JSON file mechanism — the agent drops requests in its temp dir, and the scheduler processes them after the task completes.

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

Expects its own Debian 13 VM, separate from your Nextcloud server. Two deployment paths:

```bash
# Standalone install (interactive wizard)
sudo ./install.sh --interactive

# Or use the Ansible role in deploy/ansible/
```

See `deploy/README.md` for full documentation, settings file format, and Ansible usage.

## Development

```bash
uv sync                                    # Install dependencies
uv run pytest tests/ -v                    # Run tests
uv run pytest -m integration -v            # Integration tests (needs config)
uv run istota task "hello" -u alice -x     # Test execution
```

For a detailed history of changes and design decisions, see [DEVLOG.md](DEVLOG.md).

## License

[AGPL-3.0-or-later](LICENSE)
