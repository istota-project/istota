---
name: feeds
triggers: [feed, feeds, rss, subscribe, subscription, add feed, remove feed, unsubscribe, opml]
description: Native RSS/Atom/Tumblr/Are.na feed manager (in-tree)
cli: true
companion_skills: [untrusted_input]
env: [{"var":"FEEDS_USER","from":"user_id"},{"var":"TUMBLR_API_KEY","from":"secret","service":"feeds","key":"tumblr_api_key","sensitive":true,"fallback_var":"TUMBLR_API_KEY"}]
---
# Feeds (native)

Manage RSS/Atom/Tumblr/Are.na feed subscriptions through the in-tree feeds module. Subscriptions, categories, entries and read state live in a per-user SQLite database the CLI opens for you; exports and other files stay under `{workspace}/{BOT_DIR}/feeds/`. The database itself is on local disk outside your sandbox — go through this CLI, there is nothing to read directly.

## CLI

Run `istota-skill feeds --help` for the live list. Output is JSON.

```bash
istota-skill feeds list                                  # List subscriptions
istota-skill feeds categories                            # List categories
istota-skill feeds entries [--status unread|read|removed] [--feed-id N] [--category SLUG] [--limit N] [--offset N] [--before UNIX_TS]
istota-skill feeds add --url URL [--title T] [--category SLUG] [--poll-interval-minutes N]
istota-skill feeds remove --url URL                      # or --id N
istota-skill feeds refresh [--id N]                      # Clear next_poll_at to mark feeds due now
istota-skill feeds poll [--limit N]                      # Poll every feed whose next_poll_at is past
istota-skill feeds run-scheduled [--limit N]             # Scheduler module-job entry point; caps its burst at 50 by default
istota-skill feeds import-opml PATH                      # Import OPML; rewrites bridger URLs
istota-skill feeds export-opml [--output PATH]           # Export as OPML 2.0
```

## URL schemes

- `https?://...` — RSS/Atom feed (parsed via `feedparser`).
- `tumblr:USERNAME` — Tumblr blog via the API v2 provider.
- `arena:CHANNEL_SLUG` — Are.na channel via the Are.na API provider.

OPML imports automatically rewrite bridger URLs (`http://127.0.0.1:8900/{provider}/{id}/feed.xml`) to the bare `{provider}:{id}` form so old exports import cleanly on fresh machines.

## Environment variables

| Variable | Description |
|---|---|
| `FEEDS_USER` | Istota user id (set by the executor) |
| `TUMBLR_API_KEY` | Tumblr API v2 key (optional). Can also be set via `extra.tumblr_api_key` on the user's `[[resources]] type = "feeds"` entry — context value wins, env var is a fallback for the migration window when the production deploy still has the key in the bridger systemd unit. |

## Notes

- The per-user SQLite is the only source of truth. `add` / `remove` mutate it directly. Don't read or write `feeds.toml` — any pre-existing file gets imported once on first touch then stops being read.
- `run-scheduled` runs every 5 minutes via the `_module.feeds.run_scheduled` job that the scheduler auto-seeds when the user has a `[[resources]] type = "feeds"` entry. It polls at most `DEFAULT_SCHEDULED_POLL_LIMIT` (50) feeds per run unless `--limit` says otherwise; the due list is oldest-first, so a backlog drains over consecutive runs.
- Requests are paced per host (2 s minimum gap), so a set of Are.na or Tumblr feeds does not go out as one burst. An HTTP 429 is reported as a throttle, not a feed error: the run's JSON carries `throttled` beside `errors`, each feed carries `rate_limited` and `retry_after_seconds`, and a run that was rate-limited on every feed exits non-zero. Don't read `status: ok, errors: 0` as "everything fetched" without looking at `throttled`.
