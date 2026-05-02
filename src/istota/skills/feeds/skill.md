---
name: feeds
triggers: [feed, feeds, rss, subscribe, subscription, add feed, remove feed, unsubscribe, opml]
description: Native RSS/Atom/Tumblr/Are.na feed manager (in-tree, no Miniflux)
cli: true
resource_types: [feeds]
companion_skills: [untrusted_input]
env: [{"var":"FEEDS_USER","from":"user_id"}]
---
# Feeds (native)

Manage RSS/Atom/Tumblr/Are.na feed subscriptions through the in-tree feeds module. Per-user SQLite under `{workspace}/feeds/data/feeds.db`; subscriptions live in `FEEDS.toml` (or `FEEDS.md` with a fenced toml block).

## CLI

Run `istota-skill feeds --help` for the live list. Output is JSON.

```bash
istota-skill feeds list                                  # List subscriptions (DB view)
istota-skill feeds categories                            # List categories
istota-skill feeds entries [--status unread|read|removed] [--feed-id N] [--category SLUG] [--limit N] [--offset N] [--before UNIX_TS]
istota-skill feeds add --url URL [--title T] [--category SLUG] [--poll-interval-minutes N]
istota-skill feeds remove --url URL                      # or --id N
istota-skill feeds refresh [--id N]                      # Clear next_poll_at to mark feeds due now
istota-skill feeds poll [--limit N]                      # Poll every feed whose next_poll_at is past
istota-skill feeds run-scheduled [--limit N]             # Wrapper used by the scheduler module-job
istota-skill feeds import-opml PATH [--no-write-config]  # Import OPML; rewrites bridger URLs
istota-skill feeds export-opml [--output PATH]           # Export as OPML 2.0
```

## URL schemes

- `https?://...` — RSS/Atom feed (parsed via `feedparser`).
- `tumblr:USERNAME` — Tumblr blog via the API v2 provider.
- `arena:CHANNEL_SLUG` — Are.na channel via the Are.na API provider.

OPML imports automatically rewrite bridger URLs (`http://127.0.0.1:8900/{provider}/{id}/feed.xml`) to the bare `{provider}:{id}` form so the same FEEDS.toml works after the bridger VM is decommissioned.

## Environment variables

| Variable | Description |
|---|---|
| `FEEDS_USER` | Istota user id (set by the executor) |
| `TUMBLR_API_KEY` | Tumblr API v2 key (optional). Can also be set via `extra.tumblr_api_key` on the user's `[[resources]] type = "feeds"` entry — context value wins, env var is a fallback for the migration window when the production deploy still has the key in the bridger systemd unit. |

## Notes

- Subscriptions and categories are stored in `FEEDS.toml`; entries + read state live only in SQLite. `FEEDS.toml` is the source of truth — `add` / `remove` mutate it and re-sync.
- `run-scheduled` runs every 15 minutes via the `_module.feeds.run_scheduled` job that the scheduler auto-seeds when the user has a `[[resources]] type = "feeds"` entry.
