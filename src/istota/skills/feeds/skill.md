# Feeds (Miniflux RSS)

Manage RSS feed subscriptions via Miniflux. Supports listing, adding, and removing feeds, browsing entries, and triggering refreshes.

## CLI

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
