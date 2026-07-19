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

The `local` extra is the lean footprint: the core agent, the web UI, and the light pure-Python modules (feeds, calendar, email, markets). Heavier modules stay opt-in — add them explicitly if you want them:

```bash
uv tool install 'istota[local,money,health,location,memory-search,whisper,transcribe]'
```

A module whose extra isn't installed hides itself: the app skips it and its web UI tab doesn't appear, rather than showing a broken tab. So `money` (double-entry accounting via beancount; also pulls weasyprint for invoice PDFs) is absent unless you add its extra — the guided `install.sh --standalone` asks whether you want it (the extra is chosen at install time, so it's an installer question, not a `setup` one). Add it later at any time by re-running `uv tool install` with `money` in the extras, and the Money tab appears on the next `serve`.

## Set up

```bash
istota setup
```

The interactive wizard:

1. **Workspace** — where your data lives (default `~/.istota`).
2. **Model backend** — if the `claude` CLI is detected it offers to use it (no extra keys). Otherwise it asks for an OpenAI-compatible base URL, model, and API key.
3. **Identity** — a user id (default your OS username), display name, timezone.
4. **Web port** — default `8766`.
5. **Optional surfaces** — email (IMAP/SMTP) and GPS/location webhooks, both off by default.

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
