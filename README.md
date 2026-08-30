# Istota

[![License](https://img.shields.io/github/license/istota-project/istota)](LICENSE)
[![Last commit](https://img.shields.io/github/last-commit/istota-project/istota?logo=github)](https://github.com/istota-project/istota/commits/main)
[![Docs](https://img.shields.io/badge/docs-istota.cynium.com-blue)](https://istota.cynium.com/docs)

**Istota is a self-hosted personal AI assistant with its own web UI.** It runs on your own server and works with any model — use Claude through the [Claude Code](https://docs.anthropic.com/en/docs/build-with-claude/claude-code) CLI, or point it at any OpenAI-compatible endpoint like OpenRouter or a local model. Talk to it over the built-in web chat, email, or Nextcloud Talk. Nextcloud is a first-class integration (files, calendars, contacts and Talk, over standard protocols with the bot as an ordinary user), but it is an integration rather than a foundation: file storage is a backend choice, and the local single-user install runs with no Nextcloud at all.

It ships with a set of skills the agent loads on demand — calendar, email, web browsing, git, accounting, transcription, and more — plus native web modules: multi-room chat, an RSS reader, location tracking, and health and accounting dashboards. It is multi-user out of the box, with per-user memory, filesystem sandboxing, and resource permissions.

> **Pre-1.0 and under active development.** Istota has not reached a stable 1.0 release. Interfaces, config, and database schema can change between releases, sometimes in breaking ways. Pin to a release and read the CHANGELOG before upgrading.

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
| **Messaging** | Always-on web chat with live streaming, file attachments, recorded voice messages, per-room drafts that survive leaving the page, and authenticated download links for files a task produces; Nextcloud Talk (DMs + group rooms with @mentions); email (IMAP/SMTP threading), TASKS.md polling, REPL, CLI. Talk and web chat share one room model — continue a conversation on either surface with shared history, and promote a web room to a real Talk conversation. |
| **Skills** | 36 skills the agent loads on demand: calendar, email, web browsing (Dockerized Chrome), git/GitLab/GitHub, beancount accounting, GPS tracking, Karakeep bookmarks, voice transcription, OCR, RSS feeds, health, Google Workspace, and more. A curated standard library, not a plugin marketplace. |
| **Memory** | Per-user (USER.md) and per-channel memory, nightly-extracted dated memories, hybrid BM25 + vector recall, and a temporal knowledge graph. Optional learned playbooks distilled from successful multi-step tasks. |
| **Web UI** | SvelteKit dashboard: multi-room chat, RSS reader, location/places map, money and health dashboards, and per-user settings. Login is Nextcloud OAuth2 on a server deployment, or no login at all on a loopback-bound local install. |
| **Scheduling** | Cron jobs via CRON.md (prompts, prompt files, or shell commands), natural-language reminders, and briefings built from reorderable content blocks — each gathering newsletters, RSS, a browsed news frontpage, markets, calendar, or todos — with a web reader and archive, delivered to Talk, email, or ntfy. |
| **Health** | Body-stat time series, bloodwork OCR + CSV import, biomarker trends with LLM explainers, Garmin Connect sync, immunization registry, medical history, and the documents behind it — scans and discharge summaries attached to the encounter, condition or immunization they evidence. Metric storage with unit-aware display. |
| **Monitoring** | Heartbeat checks — file age, shell commands, URL health, calendar conflicts, task deadlines, self-checks — with cooldowns, quiet hours, and per-check intervals. |
| **Files** | Each user gets a workspace the agent reads and writes: notes, config (persona, cron jobs, briefings, heartbeat checks), an inbox, and generated exports. Back it with a Nextcloud account per user — sharing, versioning, mobile clients, and config you can edit from any device — or with a plain folder on disk. |
| **Multi-user** | Per-user config, resource permissions, worker pools, and admin/non-admin isolation. Multiple bot instances can share one Nextcloud and interact with each other through Talk rooms. |
| **Security** | Bubblewrap sandbox per task, credential stripping from subprocess environments, network isolation via a CONNECT proxy, and deferred DB writes for sandboxed operations. |
| **Pluggable brain** | Swap the model backend behind one protocol: the Claude Code CLI, Istota's own in-process agentic loop against any OpenAI-compatible endpoint (Anthropic, OpenRouter, Ollama, LM Studio, vLLM), or the Claude TUI over tmux. Route whole instances or specific task types to either, and configure a fallback backend so a task keeps running when the primary hits a usage limit, is overloaded, or goes unavailable. |
| **Constitution** | An [Emissaries](https://github.com/istota-project/emissaries) layer defines how the agent handles data, the boundary between private and public action, and what it owes to people beyond its operator. |

## Install

Istota comes in two shapes. The **server** deployment is multi-user, sandboxed and Nextcloud-backed: bare metal is the canonical form and connects to an existing Nextcloud, while Docker bundles its own (Postgres, Redis, the web UI, and an nginx reverse proxy) for evaluation or standalone use. The **local** install is single-user and needs no server, no Nextcloud and no login — see below.

```bash
# Bare metal (Debian/Ubuntu VM, connects to your Nextcloud) — recommended
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | sudo bash

# Docker (bundles its own Nextcloud)
curl -fsSL https://raw.githubusercontent.com/istota-project/istota/main/install.sh | bash -s -- --docker
```

Run from a terminal with no flag, `install.sh` first asks whether you want a **Server** install (the multi-user, Nextcloud-backed, sandboxed deployment above) or a **Standalone** single-user install (below); pass `--bare`, `--docker`, or `--standalone` to skip the prompt. The server paths then run the same interactive wizard (Nextcloud connection, users, optional features). Glance at [`install.sh`](install.sh) before you pipe it into a shell.

To update: `sudo bash install.sh --update` (bare metal) or `cd ~/istota && git pull && docker compose -f docker/docker-compose.yml up -d --build` (Docker). An Ansible role is available at `deploy/ansible/`.

### Local single-user install

Want to run Istota on your own machine without a server, Nextcloud, or login? There is a slimmed-down local shape — one `uv tool install`, an interactive `istota setup`, then `istota serve` brings up the web UI and worker in one process on `http://localhost:8766/istota` with no auth. It is single-user, unsandboxed, and trusted by construction. Pick **Standalone** at the `install.sh` prompt (or `install.sh --standalone`, run **without** sudo) to do this in one step. Update it later with `istota update`. See **[docs/getting-started/local-install.md](docs/getting-started/local-install.md)**.

Full walkthroughs, optional services, and configuration: **[Docker quickstart](https://istota.cynium.com/docs/getting-started/quickstart-docker/)** · **[Bare metal quickstart](https://istota.cynium.com/docs/getting-started/quickstart-bare-metal/)**.

## Development

```bash
uv sync --extra all                        # Install all dependencies
uv run pytest tests/                       # Run tests (~10,750 unit tests)
uv run pytest -m integration -v            # Integration tests (needs live config)
uv run istota task "hello" -u alice -x     # Test execution
```

Most skill dependencies are optional — install everything with `--extra all`, or pick groups (`calendar`, `email`, `markets`, `transcribe`, `memory-search`, `whisper`, `location`). Skills with missing dependencies are excluded from selection automatically; run `!skills` in Talk to see what's available.

## Further reading

- [Documentation](https://istota.cynium.com/docs) — full docs (also buildable locally with `mkdocs serve`)
- [Architecture overview](https://istota.cynium.com/docs/architecture/overview/) — how the system fits together
- [CHANGELOG.md](CHANGELOG.md) — release notes

## License

[MIT](LICENSE)

***
© 2026 [Stefan Kubicki](https://kubicki.org) • A [CYNIUM Lamplight](https://lamplight.cynium.com) Release
