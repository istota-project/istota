# Local single-user install

Istota's default shape is a multi-user server deployment backed by Nextcloud (files, Talk chat, CalDAV, OAuth login), isolated per-user with bubblewrap. This page covers the other shape: a slimmed-down **local, single-user install** you run on your own mac or Linux box, like a locally-installed agent harness. No Nextcloud, no server, no sandbox, no login.

The workspace is a plain local folder (default `~/.istota`). The web UI runs on loopback with authentication bypassed. It is always single-user and always trusted.

## Trust model — read this first

A local install runs **unsandboxed**. There is no bubblewrap isolation, no skill proxy, and no network proxy. The agent's subprocesses run with **your user account's full privileges** — full filesystem access and open network. A prompt injection carried in ingested content (an email, a browsed page, a feed item) therefore has real reach.

Only give a local instance content and instructions you trust. The content-trust guardrails (`untrusted_input` companion on the ingest skills, `sensitive_actions`) stay in place, but they are about content provenance, not process isolation.

If you need isolation between untrusted content and your host, use the server deployment (Linux + bubblewrap), not the local install.

## Requirements

- macOS or Linux (Windows is not supported).
- Python 3.11+ with [`uv`](https://docs.astral.sh/uv/).
- For the default model backend: the [`claude` CLI](https://docs.anthropic.com/en/docs/build-with-claude/claude-code), installed and logged in (reuses your existing Claude Code subscription). Alternatively, an API key for any OpenAI-compatible endpoint.

## Install

```bash
uv tool install 'istota[local]'
```

The `local` extra is the lean footprint: the core agent, the web UI, and the light pure-Python modules (feeds, calendar, email, markets). The guided `install.sh --standalone` installs `local` plus `money` and `location` — both are light on disk, so there's nothing to gate at install time. Which modules are actually *enabled* is a choice made in `istota setup`, not a packaging one. If you install by hand, add the same extras:

```bash
uv tool install 'istota[local,money,location]'
```

Heavier, genuinely optional extras (`memory-search`, `whisper`, `transcribe`, `health`, `garmin`) stay off unless you name them:

```bash
uv tool install 'istota[local,money,location,memory-search,whisper,transcribe]'
```

A module whose extra isn't installed hides itself — the app skips it and its web UI tab doesn't appear rather than showing a broken tab.

> **weasyprint (invoice PDFs).** The `money` extra pulls weasyprint, whose native libs (pango/cairo) are only touched when you *render an invoice PDF*. Everything else in the money module — the ledger, queries, balances, the Money tab — works without them. On macOS that one path needs `brew install pango`; until then invoice-PDF generation is the only thing that errors.

## Set up

```bash
istota setup
```

The interactive wizard:

1. **Workspace** — where your data lives (default `~/.istota`).
2. **Model backend** — if the `claude` CLI is detected it offers to use it (no extra keys). Otherwise it asks for an OpenAI-compatible base URL, model, and API key.
3. **Identity** — a user id (default your OS username), display name, timezone.
4. **Web port** — default `8766`.
5. **Modules & surfaces** — everything ships installed, so this only chooses what's *enabled*: GPS/location tracking (off by default — it needs an Overland ingest token to receive pings), the money module (on by default; opt out here or later via `disabled_modules`), and email (off by default — needs IMAP/SMTP credentials).

It writes `~/.config/istota/config.toml` and a sibling `~/.config/istota/istota.env` (secrets — API key, session key; `chmod 600`), initializes the database, and seeds your workspace.

`setup` is idempotent. Re-running prompts before touching an existing config; `--force` overwrites. For scripted installs, `--yes` takes defaults plus flags:

```bash
istota setup --yes --workspace ~/.istota --user me --port 8766 --brain claude_code
# or, with an API-key backend:
istota setup --yes --brain native --native-model claude-sonnet-4-6 \
  --native-base-url https://api.anthropic.com/v1 --native-api-key sk-...
```

## Run

```bash
istota serve
```

This runs the task worker and the web server in one process. Open the printed URL (`http://127.0.0.1:8766/istota`). There is no login — you are the single configured user, and you are admin. `Ctrl-C` stops both cleanly.

`serve` sources `~/.config/istota/istota.env` itself, so you don't need to export anything. Point it at a non-standard config with `-c`, override the bind with `--host`/`--port`, or a different env file with `--env-file`.

The **REPL** works too, in a separate terminal, whether or not `serve` is running:

```bash
istota repl
```

## Updating

```bash
istota update
```

Pulls the latest code from the checkout `install.sh` recorded (under `~/.local/share/istota/src`), reinstalls, and runs any database migrations. When it finishes, restart `istota serve` to pick up the new code — a running process holds the old code in memory until then. Pass `--force` to update even if that checkout has uncommitted changes (it discards them with `git reset --hard`).

By default `update` follows the **stable** channel — the latest tagged release. To ride the development branch instead (newer, less tested), run `istota update --channel main`; switch back with `istota update --channel stable`. The choice is remembered, so you set it once. (An install made before this option existed keeps tracking `main` until you pick a channel.)

`update` only applies to this standalone shape and needs the install record `install.sh` writes; a hand-run `uv tool install` won't have it, so re-run `install.sh --standalone` once. A server (Nextcloud/auth) deployment is updated separately and `update` declines to run there.

## What works, what's off

- **Web chat** — the primary surface. Fully local (SQLite + local files).
- **REPL** — secondary, fully local, inline execution.
- **TASKS.md** — the `~/.istota/Users/<user>/<bot>/config/TASKS.md` file, polled while `serve` runs.
- **Scheduled jobs, briefings, heartbeat, cron** — run in the same process.
- **Nextcloud Talk** — off. Chat is the web UI and REPL.
- **Email / ntfy** — off by default; enable in `setup` or config.
- **GPS location webhooks** — off by default.
- **Calendar** — off unless you point the new `[caldav]` fields at an external CalDAV server (Radicale, Fastmail, Google); see below.

The Admin pane (`/istota/admin`) shows a "Running in standalone mode" notice listing exactly what's off in your install, so a feature that intentionally doesn't work reads as expected, not broken.

## Enabling optional pieces

**Calendar (external CalDAV).** A local install has no Nextcloud, so calendar is off by default. Point it at any CalDAV server by adding to `config.toml`:

```toml
[caldav]
url = "https://dav.fastmail.com"
username = "you@fastmail.com"
password = "app-specific-password"
```

**Email.** Set `[email] enabled = true` with your IMAP/SMTP host/user in `config.toml`, and the passwords in `istota.env` (`ISTOTA_EMAIL_IMAP_PASSWORD`, `ISTOTA_EMAIL_SMTP_PASSWORD`). `setup --email` collects these interactively.

**Heavy modules.** Install the matching extra (above), then the module is on by default (opt out per user via `disabled_modules`).

## Notes

- **Loopback only.** No-auth mode refuses to start on a non-loopback bind — you cannot accidentally expose an unauthenticated instance on the network. Use the server deployment if you need remote access.
- **One instance.** `serve` holds a lock; a second `serve` reports "already running" and exits.
- **Backups.** `setup` writes an explicit `[scheduler] db_backup_dir` (under the workspace) so local snapshots run even though the workspace isn't a mountpoint.
- **Everything in one folder.** The database, module databases, and workspace all live under the workspace directory — back it up or move it as a unit.
- **Storage vocabulary follows the backend.** The bot describes storage based on whether a Nextcloud server backs it, keyed on `[nextcloud] url` presence (not the standalone flag). With no URL — the local shape — the prompt and skill docs talk about "your workspace" (a local folder) instead of a Nextcloud mount, and tell the model it also has ordinary access to the rest of the machine's filesystem. Set a `[nextcloud] url` and the vocabulary switches back to Nextcloud/rclone.
