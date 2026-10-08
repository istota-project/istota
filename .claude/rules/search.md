---
paths:
  - "src/istota/search/**"
  - "src/istota/lib/text_match.py"
  - "src/istota/*/search.py"
  - "web/src/lib/search/**"
  - "web/src/lib/stores/search.ts"
  - "web/src/lib/components/ui/Search*.svelte"
---

# Unified web search

`GET /istota/api/search` is a read over authoritative stores, scoped to the session user. `search/registry.py` lazily registers framework providers and each module's `search.py:PROVIDER`. `search/core.py` owns query parsing, module gates, deadlines, fixed group order and strict-then-relaxed fallback. A provider returns plain `SearchHit` data and `ProviderResult`; it does not import the web application or create another search index. `modules.module_loader` is the shared module resolver, retained in the web app as `_module_loader` for compatibility.

## Scope and stores

Chats reuse `db.search_messages` and the aggregate pane's membership SQL. Archived and dismissed rooms and `hidden_room_tokens_for_member` are excluded. Room search uses the same visible-room set. Channel memory resolves every live alias through `storage.channel_memory_tokens`; dropping an alias would hide notes after a room migration. Memory excludes conversation chunks and skill overlays. Its `vector=False` path must never load an embedding model in the web process. Facts are current and user-owned. Each module reads its own per-user store; health searches records, never reference or explainer tables.

The transcript and feeds use external-content FTS5 tables with insert/update/delete triggers and one initial rebuild. Framework migration is `db._migrate_messages_fts`; feeds schema v9 owns `feed_entries_fts`, with `search_entries` able to create the index under its deadline if it is missing. Interrupted feed rebuilds can roll back the savepoint themselves, so cleanup checks `conn.in_transaction` before rolling back to it. Index changes need upgraded-database, deletion and trigger parity tests; the upgrade tier is owed before merge.

`lib/text_match.py` is the dependency-free query leaf. It quotes literal FTS terms, escapes LIKE values, and returns plain snippets plus offsets. It caps terms at eight and normalizes NFC. FTS prefixes and phrases differ from LIKE substrings; memory keeps its existing phrase-as-words semantics. Do not turn those differences into another query parser. `make_snippet` and `markers_to_offsets` own snippets; the frontend renders text nodes and marks, never HTML.

## Deadlines and failures

Each SQLite provider opens its own connection through `open_with_deadline`, whose opener accepts `busy_timeout_ms`. Search passes zero before connection setup; `_run_one` retries SQLite busy/locked errors only within the remaining deadline. This also bounds lock waits during the first feed-index rebuild. The progress handler interrupts running SQL and is cleared before close. There are at most eight active provider workers per event loop. Module access reads use a nonblocking framework connection and omit module providers when current access cannot be read. Module resolvers reuse a deadline-bound framework connection rather than opening another default-timeout connection. Strict and relaxed passes share one deadline. A later empty page only relaxes if the first strict page is empty too.

Chats, memory and feeds get two seconds; rooms, facts, briefings, health and location get 1.5 seconds. Money gets eight seconds and runs only when explicitly selected. `money/core/ledger.py:search_transactions` holds the existing transactions BQL filter once; the route keeps its account/year/regex/tag and pagination behavior. Money passes the joined query without a relaxed pass and uses the first ledger, just as the unqualified transactions route does. Calendar-valued health, fact and ledger dates stay as `YYYY-MM-DD`: inventing a midnight UTC timestamp moves the displayed date back a day west of UTC. The existing frontend date formatter already handles bare calendar dates. Its IDs hash the posting data and count identical occurrences, since transaction metadata IDs alone collide across split postings.

`run_bean_query` runs a subprocess. Search passes the remaining deadline as its timeout; `subprocess.run` kills and reaps the child on expiry, and the helper raises `TimeoutError`. Existing callers keep their 120-second timeout and `ValueError` contract. The outer search task also bounds money with `wait_for(timeout_s + 0.5)`. That outer timer cannot terminate the provider thread during user-context resolution or filesystem work; it can finish in the background. The ledger subprocess itself has a deadline and does not survive a query timeout.

A missing module store or ledger yields an empty group. A provider failure yields `error="failed"`; an expired deadline or SQLite interrupt yields `error="timeout"`. Logging contains the source and exception type or elapsed time, never the query, result content or exception message.

## Links and old messages

`search/links.py:ROUTE_PATHS` and `web/src/lib/search/links.ts:ROUTE_PATHS` are held equal by `tests/test_search_links.py`. Route links carry a path plus string params, assembled with `URLSearchParams` in the client. File links use the workspace-relative path accepted by `resolve_chat_file`, with a leading slash when converted from an absolute host path. They never carry a host filesystem root. An unservable file has no link.

Chat hits carry the raw stored `(created_at, id)` cursor in both `cursor` and the `room`/`msg`/`ts` route params. The messages endpoint requires both `until_ts` and `until_id`, and a complete `before` cursor. It returns the inclusive target-to-before band, with the usual auxiliary rows, capped at 2,000 durable rows including system notes. A truncated band omits the target and sets `truncated=true`. Notes-only histories retain a cursor so they can reach older notes. The client extends the older window through `loadOlderRoom`. A system note omitted inside the loaded window is fetched through a one-row cursor band and inserted in timestamp/id order without changing older-page state. Cursorless links retain the five-page fallback. Explicit jumps cancel queued room-switch bottom pinning and scroll atomically.

Feeds fetch a result outside the loaded list through `GET /feeds/entries/{id}` and show it without changing list pagination. Health documents resolve `?id=`, and location selects `?place=`. `createUrlSelection` applies same-route client navigation as well as browser history changes.

## Dialog and verification

`SearchDialog` uses `Modal variant="palette"`, above the viewer and lightbox via `--z-palette` and `--z-palette-panel`. It fills the screen at phone widths. Modal stops page shortcuts even when focus moves from the query to the source dropdown. The store debounces for 200ms, aborts stale requests, checks response query identity and saves only opened queries in browser-local recents.

Provider tests use real stores and the authenticated route for scope. `tests/test_money_search.py` checks shared-query/route parity and the on-demand response against `web/src/lib/test/fixtures/money-search.json`; the dialog consumes that same fixture. To regenerate after an intentional contract change, run that test with `UPDATE_MONEY_SEARCH_FIXTURE=1`, then format the fixture and run the dialog test. Only `elapsed_ms` is normalized. Timeout cleanup uses a real child process and asserts that it was reaped.

Stage-local test files are `test_text_match`, `test_messages_fts`, `test_search_core`, `test_search_framework_providers`, `test_search_links`, `test_memory_search`, `test_chat_history_paging`, and each module's `test_*_search`. Frontend coverage lives beside the search helpers, store and dialog, URL selection, layout shortcuts, chat jump/paging and the affected module routes. Real-browser acceptance covers the palette at 360px and desktop in both themes, old-message jumps and explicit money searches. User behavior and accepted matching limits are in `docs/features/search.md`.
