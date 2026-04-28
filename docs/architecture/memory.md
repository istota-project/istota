# Memory subsystem

The memory subsystem lives under `src/istota/memory/`. This page describes how the subsystem is wired into the rest of Istota — the executor (read path), the scheduler (write path), and the on-disk storage layout. For the conceptual layering and configuration, see [Memory](../features/memory.md).

```
src/istota/memory/
├── __init__.py
├── sleep_cycle.py        # Cron pipeline (user + channel extraction, curation, retention)
├── search.py             # Hybrid BM25 + vector index, retention sweep
├── knowledge_graph.py    # Temporal triples (subject, predicate, object)
└── curation/
    ├── types.py          # SectionedDoc / Section
    ├── parser.py         # parse / serialize markdown sections
    ├── ops.py            # apply_ops with validation
    ├── prompt.py         # curation prompt + JSON-fence stripper
    └── audit.py          # USER.md.audit.jsonl writer
```

`memory/__init__.py` re-exports the public surface for back-compat. In-repo callers import explicitly (`from istota.memory.search import ...`). The `search()` function is intentionally not re-exported because it would shadow the `search` submodule.

## Read path: executor

The executor is the only consumer of memory data at task time. During prompt assembly (see [executor](executor.md)) it injects memory in this fixed order:

1. **User memory** (USER.md) — `read_user_memory_v2(config, user_id)` from `storage.py`. Auto-loaded into every interactive prompt, skipped for briefings.
2. **Knowledge graph facts** — `select_relevant_facts()` returns identity facts (subject == user_id) plus any fact whose subject or object appears in the prompt. Capped by `max_knowledge_facts`. Skipped for briefings.
3. **Channel memory** (CHANNEL.md) — `read_channel_memory(config, conversation_token)` when a token is set.
4. **Dated memories** — `read_dated_memories()` reads the last `auto_load_dated_days` files from `memories/YYYY-MM-DD.md`. Skipped for briefings.
5. **Recalled memories** — `_recall_memories()` runs a hybrid search using the task prompt as the query, keyed on the user's namespace plus `channel:{token}` when applicable. Off by default (`auto_recall = false`).

If the resulting memory section exceeds `max_memory_chars`, `_apply_memory_cap()` truncates in this order: recalled → knowledge facts → dated. User and channel memory are always preserved.

The read path is pure I/O and FTS5 lookups — there is no LLM call in the executor's memory layer.

## Write paths

### Per-task indexing (scheduler)

After every successful task, the scheduler indexes the conversation into `memory_chunks`:

```
process_one_task → execute_task → success
                ↓
                index_conversation(conn, user_id, task_id, prompt, result)
                ↓ (if conversation_token is set)
                index_conversation under channel:{token} as well
```

Two filters apply before indexing:

- `[memory_search] enabled` and `auto_index_conversations` must both be true.
- Silent scheduled jobs (`task.heartbeat_silent = True`) are skipped — high-volume retrieve-and-render crons have no recall value and were inflating `memory_chunks`.

`index_conversation()` chunks the text, content-hash dedupes, inserts into `memory_chunks` (FTS5 syncs via trigger), and embeds + writes `memory_chunks_vec` rows when `sqlite-vec` and `sentence-transformers` are both available. Indexing failures are caught and logged but never affect task completion.

### Nightly extraction (sleep cycle)

`check_sleep_cycles()` and `check_channel_sleep_cycles()` run from the scheduler's main loop on `briefing_check_interval` (default 60 s). Each evaluates a per-user (or per-channel) cron expression and calls into `memory/sleep_cycle.py` when due.

`process_user_sleep_cycle()`:

1. Reads `sleep_cycle_state` (last task id processed for this user).
2. `gather_day_data()` partitions completed tasks since the last run into INTERACTIVE (`talk`, `email`, `cli`) and AUTOMATED (`cron`, `briefing`, `subtask`) sections. Interactive tasks get 80% of a 50,000-char budget; per-task allocation is proportional to content length with tail-biased truncation (40% head + 60% tail).
3. `build_memory_extraction_prompt()` includes the day data, the current USER.md (so Sonnet skips already-known facts), and a list of suggested predicates with usage hints.
4. Invokes `claude -p - --model sonnet` directly via `subprocess.run` (not via the task queue, not via the brain abstraction). The sleep cycle is privileged orchestration — it doesn't go through the same isolation pipeline as user-initiated tasks.
5. `_parse_structured_extraction()` extracts `MEMORIES:` (bullets), `FACTS:` (JSON triples), and `TOPICS:` (JSON map). Missing or malformed sections degrade gracefully.
6. Writes `memories/YYYY-MM-DD.md` with the bullets only.
7. Inserts each fact via `add_fact()` (fuzzy-deduped, single-valued predicates auto-supersede).
8. Picks the dominant topic from the TOPICS map and indexes the dated file with that topic via `index_file(..., source_type="memory_file", topic=...)`.
9. Advances `sleep_cycle_state.last_processed_task_id`.
10. Calls `cleanup_old_memory_files()` (file pruning by date in filename).
11. Calls `cleanup_old_chunks()` (chunk pruning, see [Retention](#retention)).
12. If `curate_user_memory` is on, calls `curate_user_memory()`.

`process_channel_sleep_cycle()` is the same shape, keyed on `conversation_token`, runs in UTC, attributes each task by `user_id`, focuses on shared context, and indexes under namespace `channel:{token}` with `source_type = "channel_memory"`.

### USER.md curation

When `curate_user_memory = true`, the user sleep cycle ends with op-based curation rather than a full file rewrite:

```
curate_user_memory(config, user_id, conn)
  ├── read_user_memory_v2()              # current USER.md
  ├── read_dated_memories(max_days=3)    # last 3 days, capped at 8000 chars
  ├── _load_kg_facts_text()              # current knowledge graph
  ├── parse_sectioned_doc()              # SectionedDoc
  ├── build_op_curation_prompt()         # prompt builder
  ├── claude -p - --model sonnet         # subprocess
  ├── strip_json_fences() + json.loads() # {"ops": [...]}
  ├── apply_ops()                        # (new_doc, applied, rejected)
  ├── (skip-write check on outcomes)
  ├── serialize_sectioned_doc() + write
  ├── index_file(source_type="user_memory")  # re-index for search
  ├── write_audit_log()                  # USER.md.audit.jsonl
  └── _post_curation_summary()           # one-line message to log_channel
```

`apply_ops()` accepts three op shapes (`append`, `add_heading`, `remove`), validates each independently, and never raises. Bad ops accumulate in `rejected` while good ones still apply. Ops only operate on the **top region** of a section — lines before the first `### subheading` — so deeper hand-curated structure is safe from automated edits.

The skip-write decision is outcome-based, not text-based: if every applied op was a no-op (`noop_dup` or `noop_no_match`), the file is left alone. Comparing serialized output against the file's current text would trigger a spurious rewrite whenever USER.md had harmless drift (CRLF, trailing whitespace on headings, missing trailing newline) that the round-trip normalized away.

Detailed semantics — op shapes, validation rules, reject reasons, audit format — live in [Memory § Op-based USER.md curation](../features/memory.md#op-based-userd-curation).

## Retention

`[sleep_cycle] memory_retention_days` is the unified knob. Each nightly user sleep cycle runs:

1. `cleanup_old_memory_files(config, user_id, retention_days)` — deletes dated files in `memories/` whose date prefix is older than the cutoff.
2. `cleanup_old_chunks(conn, user_id, retention_days)` — deletes `memory_chunks` rows where `source_type ∈ ("conversation", "memory_file", "channel_memory")` and `created_at` is older than the cutoff. Vec rows cascade row-by-row (the vec table has no trigger; the FTS5 trigger handles `memory_chunks_fts` automatically). Durable `user_memory` chunks are never pruned by age — they refresh on file edit and after curation re-indexes.

The channel sleep cycle does the same chunk sweep scoped to `channel_memory` only, gated by `[channel_sleep_cycle] memory_retention_days`.

A subtle gotcha worth knowing: `cleanup_old_chunks()` formats its cutoff with `strftime('%Y-%m-%d %H:%M:%S')` so it matches SQLite's `datetime('now')` column default exactly. Python's `isoformat()` would emit a `T` separator that lex-compares greater than the SQLite space form for any same-date row, deleting up to 24 hours of rows on the cutoff day.

## Storage layout

Files written to the user's Nextcloud workspace:

```
/Users/{user_id}/{bot_dir}/config/USER.md           # durable, hand- or curation-edited
/Users/{user_id}/{bot_dir}/config/USER.md.audit.jsonl  # curation audit log (sidecar)
/Users/{user_id}/memories/YYYY-MM-DD.md              # dated memory files

/Channels/{conversation_token}/CHANNEL.md            # durable, hand-edited
/Channels/{conversation_token}/memories/YYYY-MM-DD.md
```

SQLite tables (`schema.sql`):

| Table | Role |
|---|---|
| `sleep_cycle_state` | Per-user `last_run_at`, `last_processed_task_id` |
| `channel_sleep_cycle_state` | Same, keyed on `conversation_token` |
| `memory_chunks` | Indexed text chunks; columns include `source_type`, `topic`, `entities`, `metadata_json`, `content_hash`, `created_at` |
| `memory_chunks_fts` | FTS5 virtual table, trigger-synced from `memory_chunks` |
| `memory_chunks_vec` | sqlite-vec table, lazy-created via `ensure_vec_table()` |
| `knowledge_facts` | Temporal triples; `valid_from` / `valid_until` columns; unique-current index on `(user_id, subject, predicate, object) WHERE valid_until IS NULL` |

`source_type` values used in `memory_chunks`:

| Value | Source | Lifecycle |
|---|---|---|
| `conversation` | `index_conversation()` per task | Ephemeral — pruned by retention |
| `memory_file` | `index_file()` for dated `memories/YYYY-MM-DD.md` | Ephemeral — pruned by retention |
| `user_memory` | `index_file()` for USER.md (after curation or `reindex_all`) | Durable — never pruned by age |
| `channel_memory` | `index_file()` for dated channel memory files | Ephemeral — pruned by retention |

## Knowledge graph integration

`memory/knowledge_graph.py` is consumed in three places:

1. **Sleep cycle.** Extracted facts are inserted via `add_fact()` with `source_type = "extracted"`. Single-valued predicates (`works_at`, `lives_in`, `has_role`, `has_status`) auto-supersede; temporary predicates (`staying_in`, `visiting`) coexist; everything else is multi-valued. Word-level Jaccard similarity (threshold 0.7) on the `predicate object` signature catches near-duplicates.
2. **Executor read path.** `select_relevant_facts()` filters by relevance to the prompt and `format_facts_for_prompt()` renders them into the prompt's "Known facts" section.
3. **Curation prompt.** `_load_kg_facts_text()` includes current facts in the USER.md curation prompt so Sonnet doesn't duplicate structured knowledge as bullets in USER.md.

The graph stores temporal validity in dedicated columns rather than baking dates into the object string. `invalidate_fact()` sets `valid_until = today`; `delete_fact()` is a hard delete.

## Failure modes and degradation

- **`sqlite-vec` missing**: `enable_vec_extension()` returns False; search degrades to BM25-only. Indexing skips the vec write but still inserts `memory_chunks` and `memory_chunks_fts`.
- **`sentence-transformers` missing**: `_get_model()` returns None; same degradation as above.
- **Sleep cycle Claude CLI missing or timeout**: extraction is skipped for that user/channel that night; state advances anyway when the data is empty so we don't reprocess silently.
- **Curation JSON parse failure**: logged with the raw output truncated; nothing is written, no audit entry. The next night re-attempts.
- **Indexing exception**: caught and logged; never affects task completion.
- **Mount unavailable**: sleep cycle skips file writes (mount is required for memory file reads/writes).

## CLI surface

The `memory_search` skill exposes the index and the knowledge graph:

```
istota-skill memory_search search QUERY [--topic ...] [--entity ...] [--since YYYY-MM-DD]
istota-skill memory_search index conversation TASK_ID
istota-skill memory_search index file PATH
istota-skill memory_search reindex
istota-skill memory_search stats
istota-skill memory_search facts [--entity ...]
istota-skill memory_search timeline ENTITY
istota-skill memory_search add-fact …
istota-skill memory_search invalidate ID
istota-skill memory_search delete-fact ID
```

## Related pages

- [Memory](../features/memory.md) — layered design, prompt order, configuration tables.
- [Executor](executor.md) — full prompt-assembly order and result composition.
- [Scheduler](scheduler.md) — when sleep cycles fire and how they're driven from the main loop.
- [Database](database.md) — full schema overview.
