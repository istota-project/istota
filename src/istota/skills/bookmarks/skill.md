# Karakeep Bookmarks

Search, browse, and manage bookmarks from the user's Karakeep vault.

## Commands

```bash
# Search bookmarks (full-text, ranked by relevance)
python -m istota.skills.bookmarks search "machine learning papers"
python -m istota.skills.bookmarks search "recipe" --limit 5 --sort desc

# List/browse bookmarks
python -m istota.skills.bookmarks list --limit 10
python -m istota.skills.bookmarks list --favourited
python -m istota.skills.bookmarks list --archived
python -m istota.skills.bookmarks list --tag "programming"
python -m istota.skills.bookmarks list --in-list "Read Later" --limit 10

# Get a single bookmark's details
python -m istota.skills.bookmarks get BOOKMARK_ID
python -m istota.skills.bookmarks get BOOKMARK_ID --include-content

# Add a bookmark (link or text)
python -m istota.skills.bookmarks add "https://example.com/article"
python -m istota.skills.bookmarks add "https://example.com" --title "Great article" --tags "tech,reading" --note "Must read"
python -m istota.skills.bookmarks add "Note to self: check this pattern" --text --tags "idea"

# Tag operations
python -m istota.skills.bookmarks tags                    # list all tags
python -m istota.skills.bookmarks tags --search "prog"    # filter tags by name
python -m istota.skills.bookmarks tag BOOKMARK_ID "newtag1,newtag2"
python -m istota.skills.bookmarks untag BOOKMARK_ID "oldtag"

# Lists
python -m istota.skills.bookmarks lists                   # show all lists
python -m istota.skills.bookmarks list-bookmarks LIST_ID  # bookmarks in a list

# AI summarization
python -m istota.skills.bookmarks summarize BOOKMARK_ID

# User stats
python -m istota.skills.bookmarks stats
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
