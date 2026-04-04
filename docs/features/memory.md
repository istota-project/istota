# Memory

Istota has a multi-tiered memory system. Each tier has different scope, lifecycle, and loading behavior. All personal memory is excluded from briefing prompts to prevent private context leaking into newsletter-style output.

## Tier 1: user memory (USER.md)

Persistent per-user memory at `/Users/{user_id}/{bot_dir}/config/USER.md`. Auto-loaded into every interactive prompt. Claude reads and writes this file during task execution. Contains preferences, facts, and ongoing context about the user.

Optional nightly curation: when `curate_user_memory = true`, the sleep cycle runs a second pass that promotes durable facts from dated memories into USER.md and removes outdated entries.

## Tier 2: channel memory (CHANNEL.md)

Per-conversation memory at `/Channels/{conversation_token}/CHANNEL.md`. Loaded when `conversation_token` is set. Contains shared context for group conversations: decisions, agreements, project status. Written by Claude during execution and by the channel sleep cycle.

## Tier 3: dated memories

Written by the nightly sleep cycle to `/Users/{user_id}/memories/YYYY-MM-DD.md`. Auto-loaded into prompts for the last N days (configurable via `auto_load_dated_days`, default 3, set 0 to disable). Each entry includes task provenance references (`ref:TASK_ID`) for traceability.

Retention controlled by `memory_retention_days` (0 = unlimited).

## Tier 4: memory recall (BM25 auto-recall)

When `auto_recall = true`, the executor performs a BM25 full-text search using the task prompt as query against indexed conversations and memory files. Returns up to `auto_recall_limit` (default 5) results. No LLM call needed -- just SQLite FTS5.

When a `conversation_token` is set, also searches the channel namespace (`channel:{token}`).

## Memory search index

Hybrid BM25 + vector search using `sqlite-vec` and `sentence-transformers`:

- Text is chunked at paragraph/sentence/word boundaries with overlap
- Content-hash deduped
- FTS5 provides BM25 ranking
- `sqlite-vec` provides vector similarity (384-dim `all-MiniLM-L6-v2` embeddings)
- Results fused via Reciprocal Rank Fusion

Degrades to BM25-only if `sqlite-vec` or `sentence-transformers` are not installed.

Auto-indexed after task completion and after sleep cycle writes. Indexing failures never affect core processing.

## Memory size cap

`max_memory_chars` (default 0 = unlimited) limits total memory injected into prompts. When exceeded, components are truncated in order:

1. Recalled memories (removed first)
2. Dated memories
3. User memory and channel memory are preserved (most stable tiers)

## Sleep cycle

Nightly memory extraction runs as a direct subprocess (not a queued task), evaluated per user's timezone:

1. Gather completed tasks from the last 24 hours
2. Invoke `claude -p` with a memory extraction prompt (excludes existing USER.md to avoid duplication)
3. Extracted memories include task provenance: `- Fact learned (2026-01-28, ref:1234)`
4. Write extracted memories to dated file, or output `NO_NEW_MEMORIES`
5. Cleanup old files per retention policy
6. Trigger memory search indexing
7. If `curate_user_memory` enabled: second pass to update USER.md (outputs `NO_CHANGES_NEEDED` if nothing to update)

Task data uses tail-biased truncation (40% head + 60% tail) to preserve conclusions, with dynamic per-task budget allocation proportional to content length. Tasks sharing a conversation are grouped as threads.

Channel sleep cycle works the same way but runs in UTC and writes to `/Channels/{token}/memories/`.

## Prompt order

Memory appears in the prompt in this order:

1. User memory (USER.md)
2. Channel memory (CHANNEL.md)
3. Dated memories (last N days)
4. Recalled memories (BM25 results)

## Configuration

### Sleep cycle (`[sleep_cycle]`)

| Setting | Default | Purpose |
|---|---|---|
| `enabled` | `true` | Enable nightly extraction |
| `cron` | `"0 2 * * *"` | Schedule (user's timezone) |
| `memory_retention_days` | 0 | Auto-delete old files (0 = unlimited) |
| `lookback_hours` | 24 | How far back to look |
| `auto_load_dated_days` | 3 | Days of dated memories to auto-load (0 = disabled) |
| `curate_user_memory` | `false` | Nightly USER.md curation |

### Channel sleep cycle (`[channel_sleep_cycle]`)

| Setting | Default | Purpose |
|---|---|---|
| `enabled` | `true` | Enable channel extraction |
| `cron` | `"0 3 * * *"` | Schedule (UTC) |
| `lookback_hours` | 24 | How far back to look |
| `memory_retention_days` | 0 | Auto-delete old files (0 = unlimited) |

### Memory search (`[memory_search]`)

| Setting | Default | Purpose |
|---|---|---|
| `enabled` | `true` | Enable memory search |
| `auto_index_conversations` | `true` | Index after task completion |
| `auto_index_memory_files` | `true` | Index after sleep cycle |
| `auto_recall` | `false` | BM25 auto-recall in prompts |
| `auto_recall_limit` | 5 | Max recall results |
