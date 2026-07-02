# Istota

[![License](https://img.shields.io/github/license/istota-project/istota)](LICENSE)
[![Last commit](https://img.shields.io/github/last-commit/istota-project/istota?logo=github)](https://github.com/istota-project/istota/commits/main)
[![Docs](https://img.shields.io/badge/docs-istota.cynium.com-blue)](https://istota.cynium.com/docs)

**Istota is a self-hosted personal AI assistant with its own web UI.** It runs on your own server and works with any model — use Claude through the [Claude Code](https://docs.anthropic.com/en/docs/build-with-claude/claude-code) CLI, or point it at any OpenAI-compatible endpoint like OpenRouter or a local model. Talk to it over the built-in web chat, email, or Nextcloud Talk. It integrates with Nextcloud for files, calendars, and messaging — a deep integration, though not a hard requirement.

It ships with a set of skills the agent loads on demand — calendar, email, web browsing, git, accounting, transcription, and more — plus native web modules: multi-room chat, an RSS reader, location tracking, and health and accounting dashboards. It is multi-user out of the box, with per-user memory, filesystem sandboxing, and resource permissions.

## How it works

```
Talk message ──>┐
Web chat ──────>│
Email ─────────>├──> SQLite queue -> Scheduler -> Brain -> Response
TASKS.md ──────>│
CLI / REPL ────>┘
```

Messages arrive through Talk polling, the in-app web chat, IMAP, TASKS.md file watching, the REPL, or the CLI. The scheduler claims tasks from a SQLite queue, builds a prompt with the user's resources, skills, memory, and conversation context, then hands it to a **Brain** in a sandbox. Per-user worker threads keep foreground chat and background jobs on separate pools, so a long-running job never blocks a conversation.

## Features

| Area | What you get |
|------|--------------|
| **Messaging** | Nextcloud Talk (DMs + group rooms with @mentions), always-on web chat with live streaming, email (IMAP/SMTP threading), TASKS.md polling, REPL, CLI. Talk and web chat share one room model — continue a conversation on either surface with shared history, and promote a web room to a real Talk conversation. |
| **Skills** | ~35 skills the agent loads on demand: calendar, email, web browsing (Dockerized Chrome), git/GitLab/GitHub, beancount accounting, GPS tracking, Karakeep bookmarks, voice transcription, OCR, RSS feeds, health, Google Workspace, and more. A curated standard library, not a plugin marketplace. |
| **Memory** | Per-user (USER.md) and per-channel memory, nightly-extracted dated memories, hybrid BM25 + vector recall, and a temporal knowledge graph. Optional learned playbooks distilled from successful multi-step tasks. |
| **Web UI** | Authenticated SvelteKit dashboard (Nextcloud OAuth2): multi-room chat, RSS reader, location/places map, money and health dashboards, and per-user settings. |
| **Scheduling** | Cron jobs via CRON.md (prompts, prompt files, or shell commands), natural-language reminders, and scheduled briefings with calendar / markets / headlines / news / todos, delivered to Talk or email. |
| **Health** | Body-stat time series, bloodwork OCR + CSV import, biomarker trends with LLM explainers, Garmin Connect sync, immunization registry, and medical history. Metric storage with unit-aware display. |
| **Monitoring** | Heartbeat checks — file age, shell commands, URL health, calendar conflicts, task deadlines, self-checks — with cooldowns, quiet hours, and per-check intervals. |
| **Multi-user** | Per-user config, resource permissions, worker pools, and admin/non-admin isolation. Multiple bot instances can share one Nextcloud and interact with each other through Talk rooms. |
| **Security** | Bubblewrap sandbox per task, credential stripping from subprocess environments, network isolation via a CONNECT proxy, and deferred DB writes for sandboxed operations. |
| **Pluggable brain** | Swap the model backend behind one protocol: the Claude Code CLI, Istota's own in-process agentic loop against any OpenAI-compatible endpoint (Anthropic, OpenRouter, Ollama, LM Studio, vLLM), or the Claude TUI over tmux. Route whole instances or specific task types to either. |
| **Constitution** | An [Emissaries](https://github.com/istota-project/emissaries) layer defines how the agent handles data, the boundary between private and public action, and what it owes to people beyond its operator. |

## Install

Bare metal is the canonical deployment and connects to an existing Nextcloud. Docker bundles its own Nextcloud (Postgres, Redis, the web UI, and an nginx reverse proxy) for evaluation or standalone use.

```bash
# Bare metal (Debian/Ubuntu VM, connects to your Nextcloud) — recommended
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | sudo bash

# Docker (bundles its own Nextcloud)
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | bash -s -- --docker
```

Both run the same interactive wizard (Nextcloud connection, users, optional features). Glance at [`install.sh`](install.sh) before you pipe it into a shell.

To update: `sudo bash install.sh --update` (bare metal) or `cd ~/istota && git pull && docker compose -f docker/docker-compose.yml up -d --build` (Docker). An Ansible role is available at `deploy/ansible/`.

Full walkthroughs, optional services, and configuration: **[Docker quickstart](https://istota.cynium.com/docs/getting-started/quickstart-docker/)** · **[Bare metal quickstart](https://istota.cynium.com/docs/getting-started/quickstart-bare-metal/)**.

## Why Nextcloud?

Most AI assistant projects connect to third-party APIs for storage, calendars, and messaging, accumulating credentials and vendor dependencies. Istota instead runs on its own server and, when you connect it to Nextcloud, integrates as a regular user — files, calendars, contacts, Talk messaging, and sharing all work through standard Nextcloud protocols. No webhooks, no OAuth apps, no server plugins.

- **Zero Nextcloud configuration.** Create a user account, invite it to a chat.
- **File sharing is native.** Users share files with the bot like they share with colleagues.
- **Multi-user comes free.** Nextcloud handles user isolation, file ownership, and access control.
- **Self-hosted end to end.** Your data stays on your server; the only external dependency is a model provider.

Config files (persona, briefings, cron jobs, heartbeat checks) live in each user's Nextcloud folder, editable with any text editor. See [Why Nextcloud](https://istota.cynium.com/docs) for the full rationale.

## Development

```bash
uv sync --extra all                        # Install all dependencies
uv run pytest tests/ -v                    # Run tests (~6500 unit tests)
uv run pytest -m integration -v            # Integration tests (needs live config)
uv run istota task "hello" -u alice -x     # Test execution
```

Most skill dependencies are optional — install everything with `--extra all`, or pick groups (`calendar`, `email`, `markets`, `transcribe`, `memory-search`, `whisper`, `location`). Skills with missing dependencies are excluded from selection automatically; run `!skills` in Talk to see what's available.

## Further reading

- [Documentation](https://istota.cynium.com/docs) — full docs (also buildable locally with `mkdocs serve`)
- [Architecture overview](https://istota.cynium.com/docs/architecture/overview/) — how the system fits together
- [CHANGELOG.md](CHANGELOG.md) — release notes
- [DEVLOG.md](DEVLOG.md) — development journal

## License

[MIT](LICENSE)

***
© 2026 [Stefan Kubicki](https://kubicki.org) • A [CYNIUM Lamplight](https://lamplight.cynium.com) Release
