# Development setup

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for package management
- A Nextcloud instance (for integration tests)

## Install

```bash
git clone https://github.com/istota-project/istota.git
cd istota
uv sync --extra all
```

This installs all optional dependencies. To install only specific feature groups:

```bash
uv sync                          # Core only (see "Dependencies" below)
uv sync --extra calendar         # caldav + icalendar
uv sync --extra email            # imap-tools
uv sync --extra markets          # yfinance
uv sync --extra transcribe       # pytesseract (OCR; Pillow is a core dep)
uv sync --extra memory-search    # sqlite-vec + sentence-transformers
uv sync --extra whisper          # faster-whisper for audio transcription
uv sync --extra feeds            # feedparser + bleach (native RSS/Atom feeds)
uv sync --extra location         # fastapi + uvicorn + geopy
uv sync --extra web              # fastapi + uvicorn + authlib
uv sync --extra docs             # mkdocs + mkdocs-material
```

Skills with missing dependencies are automatically excluded from prompt selection. Use `!skills` in Talk to check availability.

## Enable the pre-commit scan

This repository is public, so commits are scanned for credentials and private
data. The hook is checked in but has to be enabled per clone:

```bash
brew install gitleaks                                # or see the gitleaks README
git config core.hooksPath .githooks                  # scripts/setup.sh also does this
cp .private-data-local.example .private-data-local   # gitignored; add your own terms
```

See [Secret scanning](secret-scanning.md).

## Initialize

```bash
uv run istota init               # Create database from schema.sql
```

Create `config/config.toml` from `config/config.example.toml` and fill in your Nextcloud credentials.

## Run

```bash
# Execute a single task
uv run istota task "Hello" -u alice -x

# Dry run (shows assembled prompt without calling Claude)
uv run istota task "Hello" -u alice -x --dry-run

# Process pending tasks (single pass)
uv run istota run --once

# Start the scheduler daemon
uv run istota-scheduler
```

## CLI commands

```bash
uv run istota task "prompt" -u USER -x [--dry-run]  # Execute task
uv run istota task "prompt" -u USER -t ROOM -x       # With conversation context
uv run istota resource ensure -u USER -t folder -p PATH  # Declare a folder mount
uv run istota resource list -u USER                   # List resources
uv run istota run [--once] [--briefings]              # Process tasks
uv run istota serve                                   # Scheduler + web UI in one process
uv run istota repl                                    # Interactive REPL
uv run istota chat backfill-history                   # Web chat room maintenance
uv run istota email list|poll|test                    # Email commands
uv run istota user ensure|list|show|lookup|remove     # User management
uv run istota briefing ensure|list|remove             # Per-user briefings
uv run istota secret ensure|list|remove               # Encrypted credential store
uv run istota calendar discover|test                  # Calendar commands
uv run istota nextcloud ...                           # Nextcloud helpers
uv run istota experimental list                       # Operator feature flags
uv run istota tasks-file poll|status [-u USER]        # TASKS.md commands
uv run istota kv get|set|list|delete|namespaces       # Key-value store
uv run istota list [-s STATUS] [-u USER]              # List tasks
uv run istota show <task-id>                          # Task details
```

`istota setup` and `istota update` also exist, but they belong to the standalone single-user install rather than a dev checkout — see [local install](../getting-started/local-install.md). The full command surface is in the [CLI reference](../reference/cli.md).

## Project layout

```
src/istota/          # Python package
config/              # Configuration files
tests/               # pytest test suite
web/                 # SvelteKit frontend
deploy/              # Ansible role + install script
docker/              # Docker Compose stack
scripts/             # Setup and runner scripts
schema.sql           # Database schema
pyproject.toml       # Project metadata and dependencies
```

## External tooling

These are not Python packages -- they're system-level tools used at runtime:

| Tool | Purpose | Required |
|---|---|---|
| `claude` CLI | Claude Code execution engine | yes |
| `rclone` | Nextcloud file access (mount or CLI mode) | yes |
| `bwrap` (bubblewrap) | Filesystem sandbox (Linux only) | recommended |
| `tesseract` | OCR engine (for transcribe skill) | optional |
| Docker | Browser container, Docker deployment | optional |
| Node.js | SvelteKit frontend build | optional (web UI only) |

## Dependencies

Core (always installed): `httpx`, `requests`, `croniter`, `tomli`, `cryptography`, `Pillow`, `pillow-heif`. Pillow and its HEIC plugin are core rather than an extra because every task with an image attachment pre-shrinks it before the model sees it; `cryptography` backs the encrypted secrets store.

Optional extras add feature-specific dependencies. Notable packages across extras: `caldav` + `icalendar` (calendar), `imap-tools` (email), `yfinance` (markets), `feedparser` (RSS), `pytesseract` (OCR), `faster-whisper` (audio), `sqlite-vec` + `sentence-transformers` (memory search), `fastapi` + `uvicorn` (web/location).

## Documentation

```bash
uv sync --extra docs
mkdocs serve         # Local preview at http://localhost:8000
mkdocs build         # Build static site to site/
```
