# Karakeep Bookmarks

Search, browse, and manage bookmarks from the user's Karakeep vault.

## Commands

```bash
# Search bookmarks (full-text, ranked by relevance)
istota-skill bookmarks search "machine learning papers"
istota-skill bookmarks search "recipe" --limit 5 --sort desc

# List/browse bookmarks
istota-skill bookmarks list --limit 10
istota-skill bookmarks list --favourited
istota-skill bookmarks list --archived
istota-skill bookmarks list --tag "programming"
istota-skill bookmarks list --in-list "Read Later" --limit 10

# Get a single bookmark's details
istota-skill bookmarks get BOOKMARK_ID
istota-skill bookmarks get BOOKMARK_ID --include-content

# Add a bookmark (link or text)
istota-skill bookmarks add "https://example.com/article"
istota-skill bookmarks add "https://example.com" --title "Great article" --tags "tech,reading" --note "Must read"
istota-skill bookmarks add "Note to self: check this pattern" --text --tags "idea"

# Tag operations
istota-skill bookmarks tags                    # list all tags
istota-skill bookmarks tags --search "prog"    # filter tags by name
istota-skill bookmarks tag BOOKMARK_ID "newtag1,newtag2"
istota-skill bookmarks untag BOOKMARK_ID "oldtag"

# Lists
istota-skill bookmarks lists                   # show all lists
istota-skill bookmarks list-bookmarks LIST_ID  # bookmarks in a list

# AI summarization
istota-skill bookmarks summarize BOOKMARK_ID

# User stats
istota-skill bookmarks stats
```

## Output

All commands return JSON with `status: ok|error`:

```json
{
  "status": "ok",
  "count": 3,
  "bookmarks": [
    {
      "id": "bk_abc123",
      "title": "Example Article",
      "url": "https://example.com/article",
      "tags": ["tech", "reading"],
      "favourited": true,
      "summary": "An article about examples.",
      "created": "2026-02-10T12:00:00Z"
    }
  ]
}
```

## Notes

- Search uses Karakeep's full-text search with relevance ranking
- Tags are created automatically if they don't exist when tagging
- The `--include-content` flag on `get` returns full HTML content (can be large)
- Bookmark IDs are opaque strings (e.g. `ieidlxygmwj87oxz5hxttoc8`)
- Summarize triggers Karakeep's AI — the summary appears asynchronously
- The `--in-list` flag on `list` accepts a list name (case-insensitive) and resolves it to its ID automatically
