# Development setup

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for package management
- A Nextcloud instance (for integration tests)

## Install

```bash
git clone https://forge.cynium.com/cynium/istota.git
cd istota
uv sync --extra all
```

This installs all optional dependencies. To install only specific feature groups:

```bash
uv sync                          # Core only (httpx, croniter, tomli)
uv sync --extra calendar         # caldav + icalendar
uv sync --extra email            # imap-tools
uv sync --extra markets          # yfinance
uv sync --extra transcribe       # pytesseract + Pillow (OCR)
uv sync --extra memory-search    # sqlite-vec + sentence-transformers
uv sync --extra whisper          # faster-whisper for audio transcription
uv sync --extra location         # fastapi + uvicorn + geopy
uv sync --extra web              # fastapi + uvicorn + authlib
uv sync --extra docs             # mkdocs + mkdocs-material
```

Skills with missing dependencies are automatically excluded from prompt selection. Use `!skills` in Talk to check availability.

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
uv run istota resource add -u USER -t TYPE -p PATH   # Add resource
uv run istota resource list -u USER                   # List resources
uv run istota run [--once] [--briefings]              # Process tasks
uv run istota email list|poll|test                    # Email commands
uv run istota user list|lookup|init|status            # User management
uv run istota calendar discover|test                  # Calendar commands
uv run istota tasks-file poll|status [-u USER]        # TASKS.md commands
uv run istota kv get|set|list|delete|namespaces       # Key-value store
uv run istota list [-s STATUS] [-u USER]              # List tasks
uv run istota show <task-id>                          # Task details
```

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

Core (always installed): `httpx`, `croniter`, `tomli`.

Optional extras add feature-specific dependencies. Notable packages across extras: `caldav` + `icalendar` (calendar), `imap-tools` (email), `yfinance` (markets), `beancount` + `beanquery` (accounting), `weasyprint` (PDF invoice generation), `feedparser` (RSS), `pytesseract` (OCR), `faster-whisper` (audio), `sqlite-vec` + `sentence-transformers` (memory search), `fastapi` + `uvicorn` (web/location).

## Documentation

```bash
uv sync --extra docs
mkdocs serve         # Local preview at http://localhost:8000
mkdocs build         # Build static site to site/
```
