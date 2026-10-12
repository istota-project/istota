# Development setup

## Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/) for package management
- A Nextcloud instance (for integration tests)

## Install

```bash
git clone https://github.com/istota-project/istota.git
cd istota
uv sync --extra test
```

`test` is `all` minus the two heavy ML extras, and it is what the suite needs — 291 MB against the 1.1 GB `--all-extras` costs, which is worth having when the venv is per-worktree. Use `--extra all` instead when you want the real torch and faster-whisper to hand-test against; the one test that needs them carries the `ml` marker and is deselected either way. See [Testing](testing.md).

`scripts/setup.sh` does this install along with the pre-commit hook setup below.

To install only specific feature groups:

```bash
uv sync                          # Core only — NOT enough to run the suite
uv sync --extra test             # every extra the suite needs
uv sync --extra calendar         # caldav + icalendar
uv sync --extra email            # imap-tools
uv sync --extra markets          # yfinance
uv sync --extra transcribe       # pytesseract (OCR; Pillow is a core dep)
uv sync --extra memory-search    # sqlite-vec + sentence-transformers
uv sync --extra whisper          # faster-whisper for audio transcription
uv sync --extra feeds            # feedparser + bleach (native RSS/Atom feeds)
uv sync --extra location         # fastapi + uvicorn + geopy
uv sync --extra web              # fastapi + uvicorn + authlib
```

There is no `docs` extra: the documentation site is Docusaurus, and its toolchain is npm. See [Documentation](#documentation) below.

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

## The edit-run loop on a Mac

A Mac checkout runs Istota as one unsandboxed process on loopback, which is what `istota serve` is for. It is a development loop, not a deployment: there is no bubblewrap on macOS, so nothing confines a task, and the web app skips sign-in.

There are two ways to give that process a config: let the wizard write one in your home directory, or [write one in the checkout by hand](#initialize-by-hand). Pick one. When both exist, the checkout's `config/config.toml` wins, because the config search order checks it before `~/.config/istota/config.toml`.

```bash
uv run istota setup              # writes ~/.config/istota/config.toml and istota.env, workspace ~/.istota
uv run istota serve              # scheduler + web app at http://127.0.0.1:8766/istota
npm --prefix web run dev         # optional: the Vite dev server, proxying the API to :8766
```

`serve` serves `web/build` when it exists, so `npm --prefix web run build` is the other way to see frontend changes. `setup` here is the same wizard the deprecated [standalone install](../getting-started/local-install.md) uses; that page lists what it writes and what is off in this shape.

If the checkout also has a `config/config.toml`, a bare `istota serve` run from the repo root loads that file and ignores the one the wizard wrote. Name the wizard's file with the global `-c`, which goes before the subcommand: `uv run istota -c ~/.config/istota/config.toml serve`.

To run what operators run, sandbox included, use the Lima VM from the [quickstart](../getting-started/quickstart-server.md#on-a-mac). `scripts/test-vm.sh` drives the same template for the `vm` test tier.

## Initialize by hand

The alternative to the wizard is a config in the checkout.

```bash
uv run istota init               # Create database from schema.sql
```

Create `config/config.toml` from `config/config.example.toml` and fill in what your test setup needs. Commands run from the repo root load it ahead of anything under `~/.config/istota/`.

`istota serve` reads secrets from an env file, separate from the config. With no `-c` it reads `~/.config/istota/istota.env`, even when the config it loads is the checkout's. With `-c` it reads the `istota.env` beside the named config, which for `-c config/config.toml` is `config/istota.env`, a path `.gitignore` does not cover. `--env-file PATH` overrides both, and keeps the secrets out of the checkout: `uv run istota -c config/config.toml serve --env-file ~/.config/istota/istota.env`.

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
uv run istota briefings schedule|blocks|sources|shared|archive   # Briefings
uv run istota doctor [--only NAME]                    # Runtime self-check
uv run istota secret ensure|list|remove               # Encrypted credential store
uv run istota calendar discover|test                  # Calendar commands
uv run istota nextcloud ...                           # Nextcloud helpers
uv run istota experimental list                       # Operator feature flags
uv run istota tasks-file poll|status [-u USER]        # TASKS.md commands
uv run istota kv get|set|list|delete|namespaces       # Key-value store
uv run istota list [-s STATUS] [-u USER]              # List tasks
uv run istota show <task-id>                          # Task details
```

`istota update` belongs to the deprecated [standalone install](../getting-started/local-install.md) and does nothing for a dev checkout. The full command surface is in the [CLI reference](../reference/cli.md).

## Project layout

```
src/istota/          # Python package
config/              # Configuration files
tests/               # pytest test suite
testbed/             # Staging environment for the deployment tiers -- compose
                     #   shapes, service stubs, DB probe. Its own pyproject.toml,
                     #   beside src/ rather than inside tests/ because it is not
                     #   part of the shipped application
web/                 # SvelteKit frontend
docker/              # Docker Compose stack and images
host/                # Provisioning and istota-stack for the dedicated VM
ansible/             # Optional management role for an estate-managed VM
scripts/             # Setup and runner scripts
schema.sql           # Database schema
pyproject.toml       # Project metadata and dependencies
```

Which tests to run, and when, is [testing](testing.md). The short version: `scripts/qt` while iterating (only the tests your change affects), `scripts/qtest uv run pytest` once before the commit.

## External tooling

These are not Python packages -- they're system-level tools used at runtime:

| Tool | Purpose | Required |
|---|---|---|
| `claude` CLI | Claude Code execution engine | yes |
| `rclone` | Nextcloud file access, when testing against a Nextcloud | optional |
| `bwrap` (bubblewrap) | Filesystem sandbox (Linux only; the image has it) | optional on a dev host |
| `tesseract` | OCR engine: the `transcribe` skill, and the automatic pass over every image attachment | optional |
| Docker | The image, smoke and full test tiers | optional |
| Lima | The `vm` test tier, and the stack on a Mac | optional |
| Node.js | SvelteKit frontend build | optional (web UI only) |

## Dependencies

Core (always installed): `httpx`, `requests`, `croniter`, `tomli`, `cryptography`, `Pillow`, `pillow-heif`. Pillow and its HEIC plugin are core rather than an extra because every image attachment is decoded, rotated, resized and re-encoded before the model sees it (`istota/image_attachments.py`); `cryptography` backs the encrypted secrets store.

Automatic OCR is the part that degrades. Without the `transcribe` extra or the `tesseract` binary, every image attachment still reaches the model through the vision path — the prompt's OCR section says `OCR unavailable` per image instead of carrying text, and nothing fails. With Pillow missing, which a core dependency makes unlikely, images are left as ordinary attachment paths and each one is named to the model as unavailable rather than passed over in silence.

Optional extras add feature-specific dependencies. Notable packages across extras: `caldav` + `icalendar` (calendar), `imap-tools` (email), `yfinance` (markets), `feedparser` (RSS), `pytesseract` (OCR), `faster-whisper` (audio), `sqlite-vec` + `sentence-transformers` (memory search), `fastapi` + `uvicorn` (web/location).

Three extras compose others rather than naming packages: `local` (a lean single-user install), `all` (every module), and `test` (`all` minus `memory-search` and `whisper`, for a checkout that runs the suite). Dependencies that only the tests need live in the `dev` dependency group, which `uv sync` installs by default — not in an extra, so that a lean install cannot silently lose them.

## Documentation

The site is [Docusaurus](https://docusaurus.io/), in `docs-site/`. The markdown itself stays at the repository root in `docs/` — hundreds of references across the tree name those paths (`docs/features/whatsapp.md` in docker-compose comments, in `.claude/rules/`, in source docstrings), so the site points at `../docs` rather than the files moving.

```bash
npm --prefix docs-site ci
npm --prefix docs-site start      # Local preview at http://localhost:3000/docs/
npm --prefix docs-site run build  # Static site to docs-site/build/
```

`build` is also the link check: `onBrokenLinks` and `onBrokenAnchors` are both `throw`, so a relative link or a heading anchor that no longer resolves fails the build rather than shipping. Run it before committing a docs change that moves or renames a page.

Two things about the markdown are worth knowing before editing:

- **`.md` is CommonMark, not MDX** (`markdown.format: 'detect'` in `docusaurus.config.ts`). That is deliberate — under MDX every `{BOT_NAME}` placeholder and every `<your-token>` in prose is a compile error, and both are ordinary throughout these files. A page that genuinely wants JSX takes the `.mdx` extension, which also changes its path and therefore every cross-tree reference to it.
- **A new page has to be listed in `docs-site/sidebars.ts`.** The sidebar is explicit rather than autogenerated, because the order is curated and an autogenerated one would sort alphabetically and lose it.
