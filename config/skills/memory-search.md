# Memory Search

Search across past conversations and memory files using hybrid BM25 + semantic search.

## Usage

```bash
# Search for relevant memories
python -m istota.skills.memory_search search "query text" [--limit 10] [--source-type TYPE]

# Index a specific conversation
python -m istota.skills.memory_search index conversation TASK_ID

# Index a specific file
python -m istota.skills.memory_search index file /path/to/file.md [--source-type TYPE]

# Reindex all conversations and memory files
python -m istota.skills.memory_search reindex [--lookback-days 90]

# Show indexing stats
python -m istota.skills.memory_search stats
```

## Source Types

- `conversation` — past task prompts and results
- `memory_file` — dated memory files from sleep cycle
- `user_memory` — USER.md persistent memory
- `channel_memory` — CHANNEL.md persistent memory

## Output

```json
{
  "status": "ok",
  "query": "project alpha",
  "count": 3,
  "results": [
    {
      "chunk_id": 42,
      "content": "User: What's the status of project alpha?\n\nBot: Project alpha is...",
      "score": 0.032,
      "source_type": "conversation",
      "source_id": "156",
      "bm25_rank": 1,
      "vec_rank": 3
    }
  ]
}
```

## When to Use

**Always search when:**
- User references past conversations, decisions, or agreements
- User mentions preferences or patterns you should already know
- Topic involves a project, person, or system discussed before
- User says "remember," "we talked about," "last time," or similar

**Also search when:**
- Starting work on a project that may have prior context
- User asks about established patterns, conventions, or preferences
- You want to ensure consistency with previous advice or decisions
- A related topic was likely discussed even if user doesn't reference it

**Don't search when:**
- Query is entirely self-contained with no historical dimension
- You already have sufficient context from the current conversation
- Topic is clearly new with no prior discussion possible

### Search tips

- Use specific terms: "project alpha deployment" not "that project"
- Try multiple queries if first results aren't relevant
- Search for both the topic and related concepts
- Check `--source-type memory_file` for curated memories vs raw conversations

## Notes

- Conversations are automatically indexed after completion
- Memory files are indexed after sleep cycle writes them
- Use `reindex` to backfill if memory search was enabled after conversations already occurred
- Results are scoped to the current user — no cross-user leakage
- Falls back to keyword search if semantic search is unavailable
