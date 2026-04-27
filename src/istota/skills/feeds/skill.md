---
name: feeds
triggers: [feed, feeds, rss, subscribe, subscription, add feed, remove feed, unsubscribe]
description: RSS feed management via Miniflux
cli: true
resource_types: [miniflux]
companion_skills: [untrusted_input]
env: [{"var":"MINIFLUX_BASE_URL","from":"user_resource_config","resource_type":"miniflux","field":"base_url"},{"var":"MINIFLUX_API_KEY","from":"user_resource_config","resource_type":"miniflux","field":"api_key"}]
---
# Feeds (Miniflux RSS)

Manage RSS feed subscriptions via Miniflux. Supports listing, adding, and removing feeds, browsing entries, and triggering refreshes.

## CLI

Run `istota-skill feeds --help` (or `istota-skill feeds <subcommand> --help`) to see the live argument list.

```bash
istota-skill feeds list                              # List all subscribed feeds
istota-skill feeds add --url URL [--category NAME]   # Subscribe to a feed
istota-skill feeds remove --id ID                    # Unsubscribe from a feed
istota-skill feeds categories                        # List categories
istota-skill feeds entries [--feed-id ID] [--status unread|read|removed] [--limit N] [--search QUERY]
istota-skill feeds refresh [--feed-id ID]            # Trigger feed refresh
```

## Environment variables

| Variable | Description |
|---|---|
| `MINIFLUX_BASE_URL` | Miniflux instance URL (e.g. `https://flux.cynium.com`) |
| `MINIFLUX_API_KEY` | Miniflux API key for authentication |

## Notes

- All output is JSON for easy parsing
- Feed IDs are integers assigned by Miniflux
- Categories group feeds for organization
- Entry status values: `unread`, `read`, `removed`
- Use `refresh` to force an immediate poll of a specific feed or all feeds
