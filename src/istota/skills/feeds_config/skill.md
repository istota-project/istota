# Feed Subscription Configuration

Users can manage their RSS, Tumblr, and Are.na feed subscriptions by editing a `FEEDS.md` file in their `{BOT_DIR}/config/` directory. Items from all feeds are aggregated into a static HTML page.

## File location

```
/Users/{user_id}/{BOT_DIR}/config/FEEDS.md
```

## Format

The file is a Markdown document with TOML configuration in a fenced code block:

```markdown
# Feed Subscriptions

Your description here...

## Settings

` ` `toml
[[feeds]]
name = "hn-best"
type = "rss"
url = "https://hnrss.org/best"
interval_minutes = 30

[[feeds]]
name = "photoblog"
type = "tumblr"
url = "blogname"              # Just the blog name, not full URL
interval_minutes = 180

[tumblr]
api_key = "your-tumblr-api-key"

[[feeds]]
name = "inspiration"
type = "arena"
url = "channel-slug"          # The channel slug from the Are.na URL
interval_minutes = 60
` ` `
```

(Remove spaces from the backticks above — they are shown with spaces to prevent parsing issues.)

## Feed types

### RSS (`type = "rss"`)
Standard RSS/Atom feeds. Set `url` to the full feed URL.
- Default poll interval: 30 minutes
- Supports conditional GET (ETag/Last-Modified) for efficiency

### Tumblr (`type = "tumblr"`)
Tumblr blog posts including photo galleries. Set `url` to just the blog name (not the full URL).
- Default poll interval: 180 minutes
- Requires a `[tumblr]` section with `api_key` (register at api.tumblr.com)
- Supports multi-image photosets and reblog content

### Are.na (`type = "arena"`)
Are.na channel content. Set `url` to the channel slug from the Are.na URL.
- Default poll interval: 60 minutes
- Supports images, text blocks, and links

## Feed entry fields

| Field | Required | Description |
|-------|----------|-------------|
| `name` | Yes | Display name for the feed (used in the HTML page) |
| `type` | Yes | `"rss"`, `"tumblr"`, or `"arena"` |
| `url` | Yes | Feed URL (RSS), blog name (Tumblr), or channel slug (Are.na) |
| `interval_minutes` | No | Poll interval in minutes (defaults vary by type) |

## Common operations

**Add an RSS feed:**
Add a `[[feeds]]` entry with `type = "rss"` and the full feed URL.

**Add a Tumblr blog:**
Add a `[[feeds]]` entry with `type = "tumblr"` and just the blog name. Ensure the `[tumblr]` section has an `api_key`.

**Add an Are.na channel:**
Add a `[[feeds]]` entry with `type = "arena"` and the channel slug (the part after `are.na/` in the URL).

**Remove a feed:**
Delete the corresponding `[[feeds]]` entry from the TOML block.

**Change poll frequency:**
Set `interval_minutes` on the feed entry. Lower values poll more often but increase load.

## Creating the file

Use standard file write operations to create or edit the file at the `{BOT_DIR}/config/` path. The scheduler polls feeds on each check cycle and regenerates the static page when new items are found.
