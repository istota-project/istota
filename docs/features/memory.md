# Memory

Istota's memory subsystem is a layered system covering durable per-user notes, per-channel notes, dated extractions, full-text + vector recall, and a structured knowledge graph. All layers cooperate inside a single SQLite database plus a few markdown files in the user's Nextcloud workspace. Personal memory is excluded from briefing prompts to prevent private context from leaking into newsletter-style output.

## Where it lives

```
src/istota/
├── sleep_cycle.py            # Nightly orchestration (user + channel)
└── memory/
    ├── __init__.py             # Public re-exports
    ├── search.py               # Hybrid BM25 + vector search, indexing, retention
    ├── knowledge_graph.py      # Temporal entity-relationship triples
    └── curation/               # Op-based USER.md curation
        ├── types.py              # SectionedDoc / Section dataclasses
        ├── parser.py             # Markdown ⇄ SectionedDoc round-trip
        ├── ops.py                # apply_ops() with validation
        ├── prompt.py             # Prompt builder + JSON-fence stripper
        └── audit.py              # USER.md.audit.jsonl writer
```

`sleep_cycle.py` is the cron pipeline that drives the subsystem each night; the `memory/` package is the subsystem proper. In-repo callers import explicitly (`from istota.memory.search import ...`); the `__init__.py` re-exports exist for back-compat. The `search()` function is intentionally not re-exported because it would shadow the `search` submodule.

## The five layers

### Layer 1 — User memory (USER.md)

Persistent per-user memory at `/Users/{user_id}/{bot_dir}/config/USER.md`. Auto-loaded into every interactive prompt (skipped for briefings). Claude reads and writes this file directly during task execution; it is the slow, deliberate, almost append-only tier.

USER.md is also indexed into `memory_chunks` with `source_type = "user_memory"`. Unlike conversation chunks, these are durable — they refresh on file edit (and after curation) but are never pruned by age.

### Layer 2 — Channel memory (CHANNEL.md)

Per-conversation memory at `/Channels/{conversation_token}/CHANNEL.md`. Loaded into the prompt when `conversation_token` is set. Holds shared context for group conversations: decisions, agreements, project status. Written by Claude during execution and refreshed by the channel sleep cycle.

CHANNEL.md is currently not indexed (only the dated channel memory files written by the channel sleep cycle are). If indexing is added later, it must use a separate source_type so it stays durable like USER.md.

### Layer 3 — Dated memories

Nightly extracts produced by the user sleep cycle, written to `/Users/{user_id}/memories/YYYY-MM-DD.md`. Each entry is a self-contained bullet with task provenance:

```
- Project Alpha migrating from Django to FastAPI, targeting Q2 (2026-01-28, ref:1234)
- Prefers email summaries limited to 5 bullet points (2026-01-28, ref:1235)
```

Auto-loaded into prompts for the last `auto_load_dated_days` days (default 3, set 0 to disable). Files are skipped for briefings. Channel sleep cycle writes the same shape under `/Channels/{conversation_token}/memories/`.

Files are pruned by age via `cleanup_old_memory_files()` using `[sleep_cycle] memory_retention_days` (0 = unlimited). The corresponding `memory_chunks` rows are pruned by the unified retention sweep described below.

### Layer 4 — Memory recall and search index

Hybrid BM25 + vector search over conversations, dated memory files, USER.md, and channel memory files. Source data lives in `memory_chunks` (one row per chunk, with FTS5 virtual table `memory_chunks_fts` and optional vec table `memory_chunks_vec`).

- Text is split at paragraph/sentence/word boundaries with overlap.
- SHA-256 content hashing dedupes chunks per user.
- FTS5 supplies BM25 ranking via the trigger-synced virtual table.
- `sqlite-vec` supplies vector similarity (384-dim `all-MiniLM-L6-v2` embeddings) when the extension and `sentence-transformers` are both available.
- BM25 and vector ranks are fused via Reciprocal Rank Fusion.

When `sqlite-vec` or `sentence-transformers` is missing, the search degrades to BM25-only without changing the API.

**Auto-recall.** When `[memory_search] auto_recall = true`, the executor runs an FTS5 query using the task prompt before each interactive task (briefings excluded) and injects the top `auto_recall_limit` (default 5) results into the prompt as a "Recalled memories" section. There is no LLM call — recall is just SQLite FTS5. When a `conversation_token` is set, the channel namespace `channel:{token}` is included in the search.

**Auto-indexing.** After every successful task the conversation is indexed under the user's namespace (and the channel namespace if applicable). Silent scheduled jobs (`heartbeat_silent = True`) skip indexing — high-volume retrieve-and-render crons have no recall value and would otherwise inflate `memory_chunks`.

**Metadata filtering.** Each chunk has optional `topic` (one of `work`, `tech`, `personal`, `finance`, `admin`, `learning`, `meta`) and `entities` (JSON array of lowercase names) populated during sleep-cycle extraction. The `search` CLI exposes:

- `--topic work` — filter to chunks tagged with a topic; NULL-topic rows are always included.
- `--entity alice` — exact match against the JSON entities array via `json_each()`.
- `--since 2026-01-01` — temporal lower bound on chunk creation.

### Layer 5 — Knowledge graph

Structured entity-relationship facts with optional validity windows, stored in `knowledge_facts`. Each fact is `(subject, predicate, object)` plus optional `valid_from`, `valid_until`, `temporary`, `confidence`, and provenance fields (`source_task_id`, `source_type`).

Predicates are freeform — any short snake_case verb is accepted. Three classes of predicates have special handling:

- **Single-valued**: `works_at`, `lives_in`, `has_role`, `has_status`. A new value with the same `(subject, predicate)` invalidates the existing fact (sets `valid_until`).
- **Temporary**: `staying_in`, `visiting`. Coexist with permanent facts and never trigger supersession; intended for trips and short-term states (use `valid_from` / `valid_until` for the dates rather than baking them into the object string).
- **Multi-valued (everything else)**: `works_on`, `uses_tech`, `knows`, `prefers`, `allergic_to`, etc. Concurrent facts are allowed.

Insertion goes through `add_fact()`, which dedupes via word-level Jaccard similarity (threshold 0.7) on the `predicate object` signature to catch near-duplicates like "uses python" vs. "uses python 3". A unique index on `(user_id, subject, predicate, object)` (where `valid_until IS NULL`) prevents exact duplicates.

**Loading into prompts.** `select_relevant_facts()` always includes the user's own identity facts (subject equals `user_id`) and adds any other fact whose subject or object appears in the prompt. The result is formatted as a "Known facts" section between user memory and channel memory. The total is capped by `max_knowledge_facts` (default 0 = unlimited).

**Manual management** via the `memory_search` skill CLI:

```bash
istota-skill memory_search facts                     # list current facts
istota-skill memory_search facts --entity alice      # facts mentioning an entity
istota-skill memory_search timeline alice            # entity timeline including expired
istota-skill memory_search add-fact …                # add interactively
istota-skill memory_search invalidate <id>           # mark fact as ended (valid_until=today)
istota-skill memory_search delete-fact <id>          # permanent removal
```

## Sleep cycle

The user sleep cycle (`process_user_sleep_cycle()` in `sleep_cycle.py`) runs nightly per user in their local timezone:

1. **Load state.** `sleep_cycle_state` records the last processed task ID per user.
2. **Gather day data.** Tasks completed since the last run, partitioned into INTERACTIVE (`talk`, `email`, `cli`) and AUTOMATED (`cron`, `briefing`, `subtask`). Interactive sources get 80% of a 50,000-char budget; tasks within a conversation are grouped; per-task allocation is proportional to content length with tail-biased truncation (40% head + 60% tail) so conclusions survive.
3. **Build extraction prompt.** Includes the gathered data, the current USER.md (so Sonnet skips already-known facts), and a list of suggested predicates with usage hints. The prompt asks for three sections — `MEMORIES:` (bullets), `FACTS:` (JSON triples), `TOPICS:` (JSON map of `ref:N → category`).
4. **Invoke** `claude -p - --model sonnet` directly (not via the task queue or brain abstraction).
5. **Parse the structured output.** A regex-based parser extracts the three sections; missing or malformed sections degrade gracefully (treat the whole response as memories, empty facts, empty topics). Personal attributes and relationships are routed to FACTS only — they're not duplicated as MEMORY bullets.
6. **Write the dated memory file** to `memories/YYYY-MM-DD.md`. The sentinel `NO_NEW_MEMORIES` skips the write but still advances state.
7. **Insert facts** into `knowledge_facts` via `add_fact()` (with fuzzy dedup).
8. **Pick a dominant topic** from the TOPICS map (most common across refs) and pass it to `index_file()` so the dated chunks inherit a topic.
9. **Index** the dated memory file under `source_type = "memory_file"`.
10. **Advance** `sleep_cycle_state.last_processed_task_id`.
11. **Prune old dated files** via `cleanup_old_memory_files()`.
12. **Prune old chunks** via `cleanup_old_chunks()` (see below).
13. **Curate USER.md** if `curate_user_memory = true` (see below).

The channel sleep cycle (`process_channel_sleep_cycle()`) is the same shape but keyed on `conversation_token`, runs in UTC, auto-discovers active channels from the last `lookback_hours`, attributes each task by `user_id`, focuses on shared context (decisions, agreements, action items), and indexes under `channel:{token}` with `source_type = "channel_memory"`.

## Op-based USER.md curation

When `[sleep_cycle] curate_user_memory = true` (opt-in, off by default), the user sleep cycle ends with `curate_user_memory()`, an op-based diff rather than a full file rewrite. The flow:

1. **Parse** the current USER.md into a `SectionedDoc` (preamble + level-2 sections; `### subheadings` and below stay as opaque content inside each section).
2. **Build the curation prompt** with the current section structure, the last 3 days of dated memories (capped at 8000 chars), and the current knowledge-graph facts (so Sonnet doesn't duplicate them in USER.md).
3. **Invoke** `claude -p - --model sonnet` and strip any ` ```json ` fences from the output.
4. **Parse** the response as `{"ops": [...]}`. The applier accepts three op shapes:
   - `{"op": "append", "heading": "...", "line": "..."}` — add a bullet under an existing heading.
   - `{"op": "add_heading", "heading": "...", "lines": [...]}` — create a new heading with one or more bullets.
   - `{"op": "remove", "heading": "...", "match": "..."}` — remove a bullet whose text contains `match` (case-insensitive substring).
5. **Apply** ops via `apply_ops()`. Each op is independently validated — bad ops accumulate in `rejected` while good ones still apply, and the applier never raises on a malformed op.
6. **Audit-log** the run to `USER.md.audit.jsonl` (sidecar JSONL next to USER.md, append-only, one entry per night that produced ops).
7. **Skip the write** when every applied op was a no-op (e.g. all `noop_dup` from dedup, all `noop_no_match` from missing remove targets). The check is outcome-based, not text-based, so harmless formatting drift in USER.md (CRLF, trailing whitespace on headings, missing trailing newline) doesn't trigger a spurious nightly rewrite.
8. **Write** the new USER.md and **re-index** it under `source_type = "user_memory"` to keep search in sync with the file.
9. **Post a one-line summary** to the user's `log_channel` if configured and `curation_log_summary = true` (default). Format: `USER.md curated: +N appended, -N removed, +N new headings`.

### How USER.md should be organized

> **Important.** Curation only edits the **top region** of each `## section` — the lines that appear *before* the first `### subheading` in that section. Anything beneath a `### subheading` is opaque to the curator. This is the deliberate trade-off: deeper structure that a human organized stays untouched.
>
> Practical consequence: if you put new bullets directly under `## Preferences` (no subheadings), curation can append, dedupe, and remove them. If you organize the section as `## Preferences` → `### Communication` → bullets, the bullets under `### Communication` are off-limits — even if newer dated memories obviously contradict them, the curator will leave them alone.
>
> Recommended layout:
>
> ```markdown
> ## Preferences
> - Prefers email summaries under 5 bullets        ← curator can edit
> - Likes morning briefings at 7am                 ← curator can edit
>
> ### Specifics                                    ← anything below here is off-limits
> Detailed prose or hand-curated bullets that you don't want auto-edited.
> ```
>
> If you want curation to manage everything in a section, keep it flat. If you want to protect content from edits, drop a `### subheading` above it.

### Op rules and rejection reasons

Ops only operate on the **top region** of a section — the lines before the first `### subheading`. Subsections are treated as opaque structure that the curation pass cannot edit. This keeps human-curated subsections (deeper structure, longer prose) safe from automated edits.

The applier validates strictly:

- **Heading match** is case-sensitive exact against the parsed structure (the prompt asks the model to copy headings verbatim).
- **Bullet** means a line starting with `-`, `*`, or `1.` (and similar). Paragraphs and `### subheadings` are not bullets and are never touched.
- **`append` dedup**: identical bullet text in the section's top region (case-insensitive, after stripping the bullet marker) produces `noop_dup`. When the new bullet would land directly after a paragraph, a blank line is inserted to prevent the bullet visually fusing onto the paragraph; bullet → bullet adjacency is left as-is.
- **`append` heading-shape rejection**: bullets whose body starts with a heading-shaped token (`# `, `## `, …, `###### `) are rejected with `line_starts_with_hash`. Plain `#` followed by non-space (hashtags, footnote markers, "issue #42") is allowed.
- **`add_heading`**: rejects existing names, empty lines arrays, and headings that begin with `#`.
- **`remove`**: zero matches → `noop_no_match` (quiet); multiple matches → `multiple_matches` rejection (the model must be more specific).

Reject reasons recorded in the audit log: `unknown_op`, `missing_field`, `heading_missing`, `heading_exists`, `empty_line`, `empty_lines`, `empty_heading`, `empty_match`, `line_starts_with_hash`, `heading_starts_with_hash`, `multiple_matches`.

Outcomes recorded for applied entries: `applied`, `noop_dup`, `noop_no_match`.

### Audit log

`USER.md.audit.jsonl` is a sidecar next to USER.md. Each line is a JSON object:

```json
{"ts": "2026-04-28T09:00:00Z", "user_id": "alice", "applied": [...], "rejected": [...]}
```

The log is written when at least one op was rejected even if no ops applied (so silently-bad runs are reviewable). Truly empty runs (no ops at all) leave no audit trace. There is no rotation in v1.

## Unified retention

`[sleep_cycle] memory_retention_days` governs both:

- Dated memory **files** under `memories/` (existing behavior; pruned by `cleanup_old_memory_files()` based on the date in the filename).
- Ephemeral `memory_chunks` rows for the user's namespace, scoped to source types `conversation`, `memory_file`, and `channel_memory` (`cleanup_old_chunks()` in `memory/search.py`).

Durable `user_memory` chunks (USER.md and any future durable channel-side type) are never pruned by age — they refresh on file edit. The channel sleep cycle runs the same chunk sweep scoped to `channel_memory` only, gated by `[channel_sleep_cycle] memory_retention_days`.

`cleanup_old_chunks()` cascades deletes to `memory_chunks_vec` row-by-row (the vec table has no trigger; the FTS5 trigger handles `memory_chunks_fts` automatically). The cutoff is computed with `strftime('%Y-%m-%d %H:%M:%S')` to match SQLite's `datetime('now')` column default exactly — using Python's `isoformat()` would emit a `T` separator that lex-compares greater than the SQLite space form, deleting up to 24 hours of rows on the cutoff day.

## Memory size cap

`max_memory_chars` (default 0 = unlimited) caps the total memory section in prompts. When exceeded, the executor truncates in this order:

1. Recalled memories (removed first)
2. Knowledge graph facts
3. Dated memories
4. User memory and channel memory are always preserved

## Prompt order

The executor injects memory in this order (briefings get none of it):

1. User memory (USER.md)
2. Knowledge graph facts (current, non-expired, relevance-filtered)
3. Channel memory (CHANNEL.md)
4. Dated memories (last `auto_load_dated_days` days)
5. Recalled memories (BM25 results when `auto_recall` is on)
6. Confirmation context (only on confirmed tasks)

## Configuration

### `[sleep_cycle]`

| Setting | Default | Purpose |
|---|---|---|
| `enabled` | `true` | Run nightly memory extraction |
| `cron` | `"0 2 * * *"` | Schedule (user's timezone) |
| `lookback_hours` | `24` | How far back to gather day data |
| `memory_retention_days` | `0` | Prune dated files **and** ephemeral chunks (`conversation`, `memory_file`, `channel_memory`) older than N days. 0 = unlimited |
| `auto_load_dated_days` | `3` | Days of dated memories injected into prompts; 0 disables |
| `curate_user_memory` | `false` | Run op-based USER.md curation after extraction |
| `curation_log_summary` | `true` | Post a one-line summary to `log_channel` after applied curation ops |

### `[channel_sleep_cycle]`

| Setting | Default | Purpose |
|---|---|---|
| `enabled` | `true` | Run channel memory extraction |
| `cron` | `"0 3 * * *"` | Schedule (UTC) |
| `lookback_hours` | `24` | How far back to gather channel data |
| `memory_retention_days` | `0` | Prune dated channel files and `channel_memory` chunks older than N days |

### `[memory_search]`

| Setting | Default | Purpose |
|---|---|---|
| `enabled` | `true` | Master switch for hybrid search and indexing |
| `auto_index_conversations` | `true` | Index after task completion |
| `auto_index_memory_files` | `true` | Index after sleep cycle and after curation writes |
| `auto_recall` | `false` | Inject FTS5 recall results into prompts |
| `auto_recall_limit` | `5` | Max recall results |

### Other knobs

- `max_memory_chars` (top-level Config) — total memory cap; 0 = unlimited.
- `max_knowledge_facts` (top-level Config) — cap on KG facts injected per prompt; 0 = unlimited.

## Schema

Memory tables in SQLite (`schema.sql`):

| Table | Purpose |
|---|---|
| `sleep_cycle_state` | Per-user nightly state (`last_run_at`, `last_processed_task_id`) |
| `channel_sleep_cycle_state` | Same, keyed on `conversation_token` |
| `memory_chunks` | Indexed text chunks (`source_type` ∈ `conversation`, `memory_file`, `user_memory`, `channel_memory`); `topic`, `entities`, `metadata_json` columns |
| `memory_chunks_fts` | FTS5 virtual table, trigger-synced from `memory_chunks` |
| `memory_chunks_vec` | sqlite-vec table (created lazily via `ensure_vec_table()`) |
| `knowledge_facts` | Temporal triples with validity windows; unique-current index prevents duplicate active facts |

## CLI surface

The `memory_search` skill exposes everything via `istota-skill`:

- `search QUERY [--topic ...] [--entity ...] [--since YYYY-MM-DD]`
- `index conversation TASK_ID` / `index file PATH`
- `reindex` — rebuild from current files and conversation history
- `stats` — counts and source-type breakdown
- `facts [--entity ...]` / `timeline ENTITY` / `add-fact …` / `invalidate ID` / `delete-fact ID`
