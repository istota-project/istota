# Istota Development Log

> Istota was forked from a private project (Zorg) in February 2026. Entries before the fork reference the original name.

## 2026-04-01: Feed viewed tracking

Added viewport-based read tracking to the SvelteKit feed page. Entries are marked as read in Miniflux after being visible in the viewport for 1.5 seconds, using IntersectionObserver with a 0.5 threshold. API calls are batched and debounced (flush every 3s or on page leave).

**Key changes:**
- Batch endpoint for marking multiple entries as read in one API call.
- IntersectionObserver action on FeedCard with 1.5s timer, skips already-read entries.
- Read cards shown at 85% opacity (full on hover), lightbox unaffected.
- "New" filter chip to show only unread entries.
- Status badge shows unread count alongside total.

**Files added/modified:**
- `src/istota/web_app.py` - Added `PUT /api/feeds/entries/batch` endpoint
- `web/src/lib/api.ts` - Added `updateEntriesStatus()` batch function
- `web/src/lib/components/FeedCard.svelte` - Added `trackView` action, `onViewed` prop, read class
- `web/src/routes/feeds/+page.svelte` - Batch queue, "new" filter chip, read card styling, unread count
- `AGENTS.md` - Updated web interface docs with read tracking details

## 2026-04-01: Fix context management causing duplicate response delivery (ISSUE-026)

When Claude Code's context management (CM) fires mid-response, the model restarts and produces a new response covering the same ground. Both pre-CM and post-CM text were delivered to the user, and the result text contained both versions concatenated. Jaccard-based deduplication couldn't catch this because the model rephrases when restarting (similarity ~0.04).

The fix makes the stream parser emit `ContextManagementEvent` markers instead of silently dropping CM events. The executor records these as `cm_boundary` entries in the execution trace. `_compose_full_result` now segments by CM boundaries and uses the last substantial segment as the authoritative response, falling back to `result_text` when all non-CM text is thin progress notes (real response only in CM replay).

Validated against 13 production JSONL session traces covering high-CM (20-36 events), medium (3-9), and low (1-2) scenarios. Correctly deduplicates in all cases without data loss.

**Key changes:**
- `stream_parser.py`: Added `ContextManagementEvent` dataclass, CM events emit marker instead of `None`
- `executor.py`: Handle `ContextManagementEvent` in event loop (record `cm_boundary` in trace, skip progress)
- `executor.py`: CM-aware `_compose_full_result` — segments trace at boundaries, walks backwards for last substantial segment
- `executor.py`: Result file fallback now also goes through `_compose_full_result`

**Files modified:**
- `src/istota/stream_parser.py` — `ContextManagementEvent`, updated `StreamEvent` union
- `src/istota/executor.py` — CM-aware composition, event loop handler, result file fallback
- `tests/test_stream_parser.py` — 3 tests updated for `ContextManagementEvent`
- `tests/test_executor.py` — 8 new tests in `TestComposeFullResultCM`
- `.claude/rules/executor.md` — Updated docs for CM-aware composition
- `.gitignore` — Ignore production JSONL session traces in test fixtures

## 2026-04-01: Fix installer bugs found via Docker-based testing

Tested install.sh in a Debian 13 amd64 Docker container (with systemd) on Mac M2. Found and fixed five bugs in the bootstrap and Ansible role.

**Key changes:**
- Fixed `pipx ensurepath` prose output being eval'd as shell commands (causing "Success!: command not found")
- Added `unzip` to system deps (rclone installer requires it)
- Added `cron` to system deps (backup cron needs `/etc/cron.d`)
- Fixed Ansible yaml callback: `community.general.yaml` was removed in v12, switched to built-in `ANSIBLE_CALLBACK_RESULT_FORMAT=yaml`
- Fixed rclone password always empty: `set_fact` can't override `--extra-vars` (highest Ansible precedence), so `settings_to_vars.py` now omits empty `istota_rclone_password_obscured` from vars file
- Changed rclone obscure from `shell` to `command` with full path for reliability

**Files modified:**
- `deploy/install.sh` — pipx ensurepath fix, Ansible output formatting
- `deploy/ansible/tasks/main.yml` — unzip/cron deps, rclone obscure via `command`
- `deploy/settings_to_vars.py` — skip empty rclone_password_obscured in vars output

## 2026-04-01: Fix stream parser dropping tool calls and interrupted responses

The ISSUE-024 fix for duplicate progress messages used a blanket `stop_reason is None → skip` filter, which silently dropped all tool call events (Claude Code emits tool_use exclusively with `stop_reason: null`). This also dropped text from interrupted turns when a background task notification arrived mid-response (ISSUE-025), causing complete response loss.

Replaced the `stop_reason`-based filtering with block-level deduplication: tool_use blocks are deduplicated by their unique block ID, text blocks by message ID + content hash. Context management replays are still filtered by their `context_management` field. The executor now uses a stateful `make_stream_parser()` instead of the stateless `parse_stream_line()`.

**Key changes:**
- `stream_parser.py`: Added `make_stream_parser()` stateful wrapper with block-level dedup
- `stream_parser.py`: Removed `stop_reason is None` blanket filter, all content types pass through
- `executor.py`: Uses `make_stream_parser()` for per-task dedup state
- Integration test with real Claude CLI `stream-json` output confirming tool calls are captured

**Files modified:**
- `src/istota/stream_parser.py` — Block-level dedup replacing message-level filtering
- `src/istota/executor.py` — Import and use `make_stream_parser()`
- `tests/test_stream_parser.py` — 46 tests (was 28): dedup tests, real CLI output integration test

## 2026-04-01: Add !search command for conversation history

Added a synchronous `!search` command that searches Talk conversation history across three data sources: the semantic memory index (BM25 + vector), the Nextcloud Talk unified search API, and exported conversation files. Results are scoped to the current room by default, with `--all` and `--room <token>` flags for broader searches.

**Key changes:**
- New `!search` command registered in command dispatch system
- Memory search integration via `_search_memory()` — resolves task IDs to conversation tokens and dates
- Nextcloud Talk API search via `_search_talk_api()` — queries unified search provider for recent unindexed messages
- Argument parsing with `--all`, `--room <token>` scoping flags
- Result deduplication across sources by task ID, capped at 8 results
- Chunk summarization (`_summarize_chunk`) extracts 1-2 sentences from memory chunks
- Output format with numbered results, room/date context, and task/link references

**Files modified:**
- `src/istota/commands.py` — Added `cmd_search`, `_search_memory`, `_search_talk_api`, `_parse_search_args`, `_summarize_chunk`, `_format_search_results`
- `tests/test_commands.py` — Added 19 tests across 3 test classes (TestCmdSearch, TestSearchMemory, TestSearchTalkApi)

## 2026-03-31: Replace install.sh with Ansible bootstrap

Replaced the monolithic 1765-line `install.sh` with a thin ~375-line bootstrap that installs Ansible and delegates all provisioning to the existing Ansible role. This eliminates the maintenance burden of two parallel install paths (bash script vs Ansible role). The settings.toml format is preserved for backward compatibility with existing installations.

**Key changes:**
- `install.sh` rewritten as bootstrap: ensures Python/pipx/ansible-core, runs wizard, converts settings, calls `ansible-playbook` in local mode
- Interactive wizard extracted to standalone `wizard.sh` (unchanged behavior)
- New `settings_to_vars.py` converts settings.toml (without `istota_` prefix) to Ansible vars YAML (with prefix)
- New `local-playbook.yml` applies the bundled role with `connection: local`
- Ansible role gains rclone password auto-obscure (runs `rclone obscure` when password not pre-provided)
- Ansible role gains Claude OAuth credentials file deployment (writes `.claude/.credentials.json`)
- `render_config.py` deleted (600 lines of duplicated config generation, replaced by Ansible templates)

**Files added:**
- `deploy/wizard.sh` — extracted interactive setup wizard
- `deploy/settings_to_vars.py` — settings.toml to Ansible vars YAML converter
- `deploy/local-playbook.yml` — local-mode Ansible playbook

**Files modified:**
- `deploy/install.sh` — rewritten as bootstrap (1765 → 375 lines)
- `deploy/ansible/tasks/main.yml` — rclone auto-obscure + Claude credentials tasks
- `deploy/README.md` — rewritten for single Ansible-based deployment path
- `AGENTS.md` — updated project structure

**Files deleted:**
- `deploy/render_config.py` — replaced by Ansible templates

## 2026-03-31: Authenticated web interface with SvelteKit + Nextcloud OIDC

Added a login-protected web UI with a SvelteKit frontend and FastAPI backend. Nextcloud serves as the OpenID Connect identity provider. The feeds page reads directly from the Miniflux API (no static file generation), with a masonry card grid matching the existing static feed page design.

**Key changes:**
- `WebConfig` dataclass with OIDC issuer, client ID/secret, session secret key
- FastAPI backend: OIDC auth flow, `/istota/api/me` (user info), `/istota/api/feeds` (Miniflux proxy with HTML sanitization and image extraction), `/istota/api/feeds/entries/{id}` (mark as read)
- SvelteKit frontend (`web/`): adapter-static with `/istota` base path, dark theme, dashboard with feature cards, feeds page with masonry grid, image/text filters, sort toggles, grid/list view, image lightbox
- Login page shows landing with "Log in with Nextcloud" link (prevents auto-re-auth on logout)
- `web` extras group in pyproject.toml (authlib, itsdangerous, python-multipart)
- Ansible: Node.js build step (`npm ci` + `npm run build`), systemd service, nginx proxy, secrets.env, restart triggers on code/config changes

**Files added:**
- `src/istota/web_app.py` — FastAPI backend (auth + API + static file serving)
- `tests/test_web_app.py` — 20 tests (auth flow, API endpoints, image extraction, sanitization)
- `web/` — SvelteKit project (routes, components, API client, CSS)
- `deploy/ansible/templates/istota-web.service.j2` — systemd service template

**Files modified:**
- `src/istota/config.py` — WebConfig dataclass, [web] parsing, env var overrides
- `pyproject.toml` — web extras group, added to all
- `config/config.example.toml` — documented [web] section
- `.gitignore` — node_modules, .svelte-kit, web/src/lib exception
- `deploy/ansible/defaults/main.yml` — web defaults
- `deploy/ansible/templates/config.toml.j2` — [web] block
- `deploy/ansible/templates/secrets.env.j2` — OIDC and session secrets
- `deploy/ansible/templates/istota.conf.j2` — nginx /istota/ proxy block
- `deploy/ansible/handlers/main.yml` — restart istota-web handler
- `deploy/ansible/tasks/main.yml` — Node.js build, web service deploy/remove, restart triggers

## 2026-03-31: Remove !usage command

Anthropic now blocks non-official clients from using the `/api/oauth/usage` endpoint (403). This is a policy change enforced via client fingerprinting — subscription OAuth tokens are restricted to the official Claude Code binary. The `!usage` command cannot work under these restrictions, so it's removed entirely.

**Key changes:**
- Removed `cmd_usage`, `_read_claude_oauth_token`, `_format_utilization` from commands.py
- Removed `httpx` import (only used by usage command)
- Removed `TestReadClaudeOauthToken`, `TestFormatUtilization`, `TestCmdUsage` test classes
- Updated AGENTS.md and ARCHITECTURE.md command references

**Files modified:**
- `src/istota/commands.py` — removed usage command and helpers
- `tests/test_commands.py` — removed ~25 usage-related tests
- `AGENTS.md` — removed `!usage` from commands list
- `ARCHITECTURE.md` — removed `!usage` row from commands table

## 2026-03-30: Remove garmin skill

Garmin changed their SSO auth flow in March 2026, breaking the `garth` library that `garminconnect` depends on. The SSO endpoint returns 429/errors permanently for programmatic logins. Rather than chasing the moving target of Garmin's auth, the garmin skill is removed entirely. Garmin data access will be handled through the browse skill with cron prompts (real browser session avoids SSO issues).

**Key changes:**
- Removed `src/istota/skills/garmin/` directory and `tests/test_garmin.py`
- Removed `garmin` extras group from `pyproject.toml` (drops `garminconnect` dependency)
- Removed Garmin credential env vars (`GARMIN_EMAIL`, `GARMIN_PASSWORD`, `GARMIN_CONFIG`) from executor, scheduler, skill proxy, and config parser
- Removed garmin resource type from config parsing, Ansible templates, and example configs
- Updated AGENTS.md and `.claude/rules/` docs

## 2026-03-30: Fix !usage 401 and briefing digest path

Two minor bug fixes from the issues tracker.

**Key changes:**
- `_read_claude_oauth_token()` now checks `CLAUDE_CODE_OAUTH_TOKEN` env var first, falling back to `~/.claude/.credentials.json`. Production uses the env var; the credentials file token expires.
- Added `get_user_data_path()` to `storage.py` for runtime state files (parallel to `get_user_config_path()` for config).
- Briefing digest (`.briefing_digest.md`) moved from user's `config/` dir to `data/` dir where runtime state belongs.

**Files modified:**
- `src/istota/commands.py` — env var check in `_read_claude_oauth_token()`, added `import os`
- `src/istota/storage.py` — added `get_user_data_path()`
- `src/istota/skills/briefing/__init__.py` — `_get_briefing_digest_path()` uses `get_user_data_path()`
- `tests/test_commands.py` — 3 new tests for env var token precedence
- `tests/test_storage.py` — test for `get_user_data_path()`
- `tests/test_briefing.py` — updated digest path assertion

## 2026-03-30: Remove invoice scheduler and accounting skill

Invoice scheduling (monthly auto-generation, pre-generation reminders, overdue detection) and the direct beancount accounting skill have been removed. All accounting functionality now goes through the Moneyman API service. The config files INVOICING.md, ACCOUNTING.md, and FEEDS.md are no longer auto-created or seeded for new users.

**Key changes:**
- Removed `invoice_scheduler.py` module (379 lines) and all supporting DB functions/tables
- Removed `skills/accounting/` directory (beancount CLI, invoicing engine, Monarch sync)
- Removed `invoicing_notifications` and `invoicing_conversation_token` from user config
- Removed INVOICING.md, ACCOUNTING.md, and FEEDS.md template constants and auto-seeding from storage.py
- Removed `LEDGER_PATH(S)`, `INVOICING_CONFIG`, `ACCOUNTING_CONFIG`, `MONARCH_SESSION_TOKEN` env var setup from executor
- Removed `_restart_fava_service()` from scheduler (was triggered after accounting tasks)
- Removed `accounting` extras group from pyproject.toml (beancount, fava, weasyprint, monarchmoney)
- Removed mutual `exclude_skills` between accounting and moneyman
- Updated notifications to resolve conversation token from briefing config only

**Files deleted:**
- `src/istota/invoice_scheduler.py`
- `src/istota/skills/accounting/` (entire directory)
- `tests/test_invoice_scheduler.py`, `tests/test_skills_accounting.py`, `tests/test_skills_invoicing.py`

**Files modified:**
- `src/istota/scheduler.py` — removed invoice check calls, Fava restart function
- `src/istota/executor.py` — removed ledger/invoicing/accounting env vars, MONARCH_SESSION_TOKEN from proxy
- `src/istota/db.py` — removed InvoiceScheduleState and 6 related functions
- `src/istota/config.py` — removed invoicing user config fields
- `src/istota/notifications.py` — simplified token resolution
- `src/istota/storage.py` — removed templates, examples, path helpers, file seeding
- `src/istota/skill_proxy.py` — removed accounting from allowed skills
- `src/istota/skills/moneyman/skill.toml` — removed exclude_skills
- `schema.sql` — removed invoice_schedule_state and invoice_overdue_notified tables
- `pyproject.toml` — removed accounting extras group
- `tests/test_storage.py`, `tests/test_notifications.py`, `tests/test_scheduler.py`, `tests/test_security.py`, `tests/test_skill_moneyman.py`
- `config/config.example.toml`, `config/users/alice.example.toml`
- `deploy/render_config.py`, `deploy/ansible/templates/user.toml.j2`
- `AGENTS.md`, `.claude/rules/` docs

## 2026-03-30: Fix near-duplicate text blocks in compose_full_result

`_compose_full_result` was duplicating near-identical text blocks when the model restated itself with minor wording changes. The existing deduplication used exact substring matching, which missed blocks that differed by a few words.

**Key changes:**
- Added `_text_similarity()` using word-bigram Jaccard similarity for fuzzy matching
- `_compose_full_result` now deduplicates blocks that are >= 50% similar (by bigram overlap) to the result text or to previously accepted blocks
- Chose bigram Jaccard over `difflib.SequenceMatcher` because SequenceMatcher's autojunk heuristic produces unreliable results for repetitive text

**Files modified:**
- `src/istota/executor.py` — Added `_text_similarity()`, rewrote dedup logic in `_compose_full_result`
- `tests/test_executor.py` — Added 3 new tests (near-duplicate blocks, near-duplicate of result, distinct blocks), updated existing test data to use realistic non-repetitive strings
- `.claude/rules/executor.md` — Updated output validation docs

## 2026-03-30: Moneyman skill — API-based accounting

Added a new `moneyman` skill that wraps the Moneyman REST API (a standalone FastAPI service deployed separately), replacing direct beancount file access with HTTP calls. The old `accounting` skill remains available for users without a Moneyman instance. Also fixed a bug in the feeds skill.toml where a `[skill]` header prevented field loading.

Production debugging revealed that skill keyword selection required adding `monarch`, `sync-monarch`, and `moneyman` as keywords since `select_skills` only picks resource-type skills when a keyword also matches. The skill supports two transport modes: CLI subprocess (preferred for same-host deployments) and HTTP REST API (for remote access).

**Key changes:**
- New `moneyman` skill: dual-mode client (CLI subprocess or HTTP) for ledger operations, transactions, invoicing, and work log management
- `build_bwrap_cmd` gains `extra_ro_binds` parameter for service config dirs
- Mutual exclusion with `accounting` skill via `exclude_skills` on both sides
- `MONEYMAN_API_KEY` added to credential proxy vars and skill credential map
- Moneyman Ansible role: CLI access, /etc/moneyman secrets binding
- Fixed feeds skill.toml: removed `[skill]` TOML header that caused fields to be ignored by the loader

**Files added:**
- `src/istota/skills/moneyman/__init__.py` — dual-mode client (CLI + HTTP)
- `src/istota/skills/moneyman/__main__.py` — entry point
- `src/istota/skills/moneyman/skill.toml` — manifest with env var declarations
- `src/istota/skills/moneyman/skill.md` — Claude documentation
- `tests/test_skill_moneyman.py` — 36 tests

**Files modified:**
- `src/istota/executor.py` — credential vars, skill map, network allowlist, extra_ro_binds for /etc/moneyman
- `src/istota/config.py` — resource config parsing
- `src/istota/skill_proxy.py` — added moneyman to allowed skills
- `src/istota/skills/accounting/skill.toml` — added `exclude_skills`
- `src/istota/skills/feeds/skill.toml` — removed erroneous `[skill]` header
- `config/users/alice.example.toml` — moneyman resource example
- `deploy/ansible/defaults/main.yml` — moneyman resource config
- `deploy/ansible/templates/user.toml.j2` — moneyman resource rendering

## 2026-03-22: v0.5.0 release — documentation sweep

Updated all architecture documentation to reflect the last week of changes. Bumped version to 0.5.0 and tagged the release.

**Key changes:**
- Updated AGENTS.md: `!more` command, `miniflux` resource type, execution traces, malformed output detection, credential proxy decoupling, sleep cycle improvements, `feeds.py` in project structure
- Updated executor.md: 4-tuple return type (`execution_trace`), output validation section (`detect_malformed_result`, `_compose_full_result`), stream parsing changes
- Updated scheduler.md: malformed output guard in task flow, `execution_trace` column, `feed_page_regen_interval`, feed page regen in daemon loop
- Updated config.md: fixed stale scheduler reference, added `miniflux` resource type, fixed `_index.toml` → `skill.toml`
- Updated skills.md: `feeds/` CLI docs, `select_skills()` signature with `disabled_skills`, `feeds.py` top-level library reference
- Updated README.md: Miniflux feeds description, test count

**Files modified:**
- `AGENTS.md`, `README.md` — Architecture and feature descriptions
- `.claude/rules/executor.md` — Executor internals
- `.claude/rules/scheduler.md` — Scheduler and DB internals
- `.claude/rules/config.md` — Config module docs
- `.claude/rules/skills.md` — Skills system docs
- `pyproject.toml` — Version bump to 0.5.0

## 2026-03-21: Fix false captcha detection on passive reCAPTCHA

The browse skill's captcha detection was triggering on sites using passive reCAPTCHA (v2 invisible / v3). These load a small visible badge iframe (e.g. 256x60) that matched the `google.com/recaptcha` iframe URL check, causing the API to return `{"status": "captcha"}` even though the page was fully accessible.

**Key changes:**
- Added bounding box size check to the iframe captcha detector — iframes smaller than 400x200 are treated as passive badges and ignored
- Only large captcha iframes (blocking challenges) now trigger detection

**Files modified:**
- `docker/browser/browsing.py` — Added size threshold after visibility check in `detect_captcha()`

## 2026-03-21: Malformed model output detection (ISSUE-019, partial)

Added validation layer between Claude Code's result and task completion. Under heavy context pressure (23 tool calls, 187K chars), the model can emit raw XML fragments instead of a response. Previously this was accepted as a successful completion and delivered to the user.

**Key changes:**
- `detect_malformed_result()` function with strict mode for Talk (any tool-call XML outside code fences) and lenient mode for other targets (only flags when entire output is fragments)
- Proportionality heuristic flags results that are too short relative to tool call count (10+ tools threshold)
- Scheduler guard after the existing API error guard; malformed results route through existing retry logic (exponential backoff, 3 attempts)
- Result quality metrics logging (chars, tool count) for all completed tasks
- Task ID (`#<id>`) appended to ack messages and "Done" summaries for traceability

**Deferred:** stream-level detection in parser (E), model-based validation gate with Sonnet/Haiku (F)

**Files modified:**
- `src/istota/executor.py` — Added `detect_malformed_result()`, `_TOOL_SYNTAX_PATTERN`, `_CODE_FENCE_PATTERN`
- `src/istota/scheduler.py` — Malformed result guard, quality logging, task ID in ack/done messages
- `tests/test_executor.py` — 24 tests (`TestDetectMalformedResult`)
- `tests/test_scheduler.py` — 7 tests (`TestMalformedResultGuard`, `TestTaskIdInProgress`)

## 2026-03-21: Miniflux migration — replace built-in feed polling with Miniflux

Migrated the feed system from direct RSS/Tumblr/Are.na polling (feed_poller.py, SQLite storage) to Miniflux as the RSS aggregator. Non-RSS services (Tumblr, Are.na) are bridged via a standalone FastAPI app (rss-bridger) that converts API responses to standard Atom feeds Miniflux can subscribe to.

**Key changes:**
- New `feeds` skill with Miniflux API CLI (list, add, remove, categories, entries, refresh)
- New `miniflux` resource type following the karakeep pattern (base_url + api_key per user)
- Static HTML page generation now pulls from Miniflux API instead of local DB
- Image extraction from HTML content as fallback when no enclosures present
- Duplicate image stripping (content images matching card image removed from excerpt)
- Title overlay bar for image cards (visible without clipping)
- Meta date links to original item URL
- Credential isolation: MINIFLUX_API_KEY routed through skill proxy
- Network allowlist: Miniflux host added when feeds skill selected
- Removed: feed_poller.py, FeedState/FeedItem DB models, feed_state/feed_items tables, feedparser/requests deps, feeds_config skill
- Config: `feed_check_interval` renamed to `feed_page_regen_interval`, `feed_item_retention_days` removed

**New project:** `~/Repos/rss-bridger` — FastAPI bridge app with Tumblr and Are.na providers, deployed via miniflux Ansible role.

**Files added:**
- `src/istota/feeds.py` — Miniflux API client + HTML page generation (preserved from feed_poller.py)
- `src/istota/skills/feeds/` — skill.toml, skill.md, CLI (__init__.py, __main__.py)
- `tests/test_feeds.py` — 36 tests (mapping, HTML generation, page regeneration)
- `tests/test_feeds_skill.py` — 13 tests (CLI subcommands)

**Files removed:**
- `src/istota/feed_poller.py`, `tests/test_feed_poller.py`, `src/istota/skills/feeds_config/`

**Files modified:**
- `src/istota/executor.py` — Miniflux env vars, proxy credentials, network allowlist
- `src/istota/scheduler.py` — Replaced feed poll with page regeneration, removed feed cleanup
- `src/istota/config.py` — Renamed feed_check_interval, removed feed_item_retention_days
- `src/istota/db.py` — Removed feed dataclasses and functions
- `src/istota/storage.py` — Removed get_user_feeds_path
- `src/istota/skill_proxy.py` — Added feeds to allowed skills
- `schema.sql` — Removed feed_state and feed_items tables
- `pyproject.toml` — Removed feedparser and requests from core deps
- `deploy/ansible/` — Updated defaults, config template, user template (miniflux resource)
- `deploy/render_config.py`, `docker/istota/entrypoint.sh` — Config field renames

## 2026-03-20: Sleep cycle memory extraction quality (ISSUE-018)

The nightly sleep cycle was producing thin, lossy memories. Five compounding problems: fixed 2K/3K per-task truncation cut off conclusions, no conversation threading, no significance weighting, conservative extraction prompt, and Sonnet's aggressive compression.

**Key changes:**
- Tail-biased excerpts: new `_excerpt()` helper keeps first 40% + last 60% of text, preserving conclusions and decisions that appear at the end of conversations
- Dynamic budget allocation: per-task budget distributed proportionally from the 50K char total instead of fixed 2K prompt + 3K result. A single long conversation now gets the full budget
- Conversation grouping: tasks sharing a `conversation_token` are grouped with thread headers so the extraction model sees conversational structure
- Prompt tuning: extraction prompt now asks for self-contained bullets with reasoning, includes good/bad examples, and encourages extraction on light days
- Same truncation fixes applied to `gather_channel_data()`

**Files modified:**
- `src/istota/sleep_cycle.py` — `_excerpt()`, refactored `gather_day_data()` and `gather_channel_data()`, improved `build_memory_extraction_prompt()`
- `tests/test_sleep_cycle.py` — 11 new tests (excerpt helper, dynamic budget, conversation grouping, prompt quality)

## 2026-03-20: Default sender policy for external emails (ISSUE-017)

When asked to email external contacts, the bot was composing as the user (first person, signed with the user's name). Added instruction-level fix: bot defaults to sending as itself (assistant identity) for external contacts, only sends as the user when explicitly asked.

**Files modified:**
- `src/istota/skills/email/skill.md` — Added "Sender identity" section with default policy
- `config/guidelines/email.md` — Added matching etiquette rule

## 2026-03-20: Deterministic briefing delivery

Briefing tasks were unreliable because Claude had access to the email skill and would sometimes send emails directly (HTML-formatted, double-delivery). Fixed by making Claude return structured JSON and having the scheduler handle delivery deterministically.

**Key changes:**
- Briefing prompt now requires JSON output: `{"subject": "Morning Briefing", "body": "..."}`
- Scheduler parses JSON from briefing results and delivers the extracted body (Talk, email, ntfy, digest)
- Falls back to `strip_briefing_preamble()` if result isn't valid JSON (backward compat)
- Added `exclude_skills` field to `SkillMeta` — skills can declare other skills to exclude from selection
- Briefing skill declares `exclude_skills = ["email"]` so the email skill is never loaded for briefing tasks
- Added `parse_briefing_json()` with robust parsing (code fences, preamble, smart quotes)

**Files modified:**
- `src/istota/skills/_types.py` — Added `exclude_skills` field to `SkillMeta`
- `src/istota/skills/_loader.py` — Parse `exclude_skills` from skill.toml, apply exclusion after selection
- `src/istota/skills/briefing/skill.toml` — Added `exclude_skills = ["email"]`
- `src/istota/skills/briefing/skill.md` — Added JSON output format requirement
- `src/istota/skills/briefing/__init__.py` — Added `parse_briefing_json()`, updated `build_briefing_prompt()` for JSON output
- `src/istota/scheduler.py` — All briefing delivery paths use parsed JSON body
- `tests/test_skills_loader.py` — 3 tests for exclude_skills behavior
- `tests/test_briefing.py` — 8 tests for parse_briefing_json
- `tests/test_scheduler.py` — 3 tests for briefing JSON delivery

## 2026-03-17: Emissary draft-approve-send flow (ISSUE-016, Phase 2)

When a confirmed task is re-executed (user said "yes" to a confirmation prompt), the bot previously got the same original prompt with no awareness that it was confirmed. It would re-draft instead of executing the confirmed action. Now the bot's previous output is injected into the re-execution prompt so it knows to proceed.

**Key changes:**
- `confirmed_at` field added to Task dataclass and loaded from DB (column existed but was never read back)
- `confirmation_context` parameter in `build_prompt()` injects a "Confirmed action" section with the bot's previous output and instructions to execute, not re-draft
- `execute_task()` detects confirmed tasks and passes `confirmation_prompt` as confirmation context
- New `cancel_pending_confirmations()` DB function cancels stale confirmations per-conversation per-user
- Talk poller cancels pending confirmations when the user sends a new message (instead of yes/no), preventing stale confirmations from lingering

**Files modified:**
- `src/istota/db.py` — `confirmed_at` on Task, `cancel_pending_confirmations()`, `confirmed_at` in SELECT queries
- `src/istota/executor.py` — `confirmation_context` parameter in `build_prompt()`, detection in `execute_task()`
- `src/istota/talk_poller.py` — Cancel pending confirmations before creating new tasks
- `tests/test_db.py` — 6 new tests for confirmed_at roundtrip and cancel_pending_confirmations
- `tests/test_executor.py` — 3 new tests for confirmation context in prompts
- `tests/test_talk_poller.py` — 2 new tests for stale confirmation cancellation

## 2026-03-17: Emissary email thread tracking (ISSUE-016, Phase 1)

When the bot sends an email on a user's behalf and the recipient replies, the reply was silently dropped because the email poller only creates tasks for known senders. Phase 1 adds outbound email tracking and thread-matched inbound routing so replies from external contacts are surfaced in Talk.

**Key changes:**
- New `sent_emails` table tracks outbound emails (Message-ID, recipient, subject, originating user and Talk conversation)
- `send_email()` and `reply_to_email()` now return the generated Message-ID
- `cmd_send()` writes a deferred JSON file (`task_{id}_sent_emails.json`) for scheduler pickup (sandbox-safe)
- Scheduler records sent emails from both deferred files and direct `post_result_to_email()` calls
- Email poller checks References header of unknown-sender emails against `sent_emails` before discarding
- Thread-matched replies create tasks with `output_target="talk"` routed to the originating Talk conversation
- Emissary reply prompt instructs the bot to notify the user and draft a response

**Files added/modified:**
- `schema.sql` — New `sent_emails` table with message_id and user indexes
- `src/istota/db.py` — `SentEmail` dataclass, `record_sent_email()`, `find_sent_email_by_message_id()`, `find_sent_email_by_references()`
- `src/istota/skills/email/__init__.py` — Return Message-ID from send functions, `_write_deferred_sent_email()`
- `src/istota/scheduler.py` — `_process_deferred_sent_emails()`, `_record_sent_email()` helper, recording in `post_result_to_email()`
- `src/istota/email_poller.py` — `_match_thread()`, thread matching before unknown-sender discard, emissary reply prompt and routing
- `tests/test_db.py` — 7 new tests for sent email DB functions
- `tests/test_email_poller.py` — 14 new tests for send return values, deferred files, thread matching, and poller routing
- `tests/test_scheduler.py` — 6 new tests for deferred processing and sent email recording

## 2026-03-17: Fix cross-user calendar data leak in briefings (ISSUE-015)

The briefing pipeline could leak one user's calendar events into another user's briefing. When `_fetch_calendar_events()` returned `None` for a user with no calendars (e.g. max), the fallback emitted an unscoped "Today's calendar events" instruction. The agent then discovered stefan's calendars via CalDAV and included his events in max's output. The sleep cycle then faithfully extracted these contaminated events into max's memory files.

**Key changes:**
- Removed unscoped calendar fallback — if a user has no calendars, the calendar component is skipped entirely instead of emitting a bare instruction the agent tries to fulfill with whatever calendars it finds
- CalDAV credentials (`CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD`) now only injected into subprocess env when the user has discovered calendars (defense-in-depth)
- `{user_id}` placeholder in skill templates now programmatically substituted (was left for LLM interpretation, which is fragile when context mentions other users)

**Files modified:**
- `src/istota/skills/briefing/__init__.py` — Replaced calendar fallback with skip + debug log
- `src/istota/executor.py` — Conditional CalDAV credential injection, `{user_id}` substitution in skills_doc
- `tests/test_briefing.py` — Updated 5 tests for new behavior (no unscoped fallback)
- `tests/test_executor.py` — Added 4 new tests: CalDAV credential scoping (3), `{user_id}` substitution (1)

## 2026-03-16: Fix location history timezone and ping limit

The `location history --date` command had two bugs: it filtered by naive UTC timestamps (so a PST user asking for March 16 got midnight-to-midnight UTC, missing the evening), and it capped results at 20 pings regardless of `--date` (a typical day has 100+ pings, so most data was silently dropped). The daily log nightly task was already switched to use `day-summary` which doesn't have these problems, but `history` itself still needed fixing.

**Key changes:**
- `history --date` now converts local day boundaries to UTC using ZoneInfo, matching the pattern already used by `day-summary` and `attendance`
- Added `--tz` parameter (defaults to `TZ` env var or `America/Los_Angeles`)
- `--date` without `--limit` returns all pings for that day; explicit `--limit N` still caps results
- No-date path unchanged (still defaults to 20 most recent pings)

**Files modified:**
- `src/istota/skills/location/__init__.py` — Timezone-aware date filtering, no-limit default for `--date`, `--tz` arg
- `src/istota/skills/location/skill.md` — Updated docs with `--tz` flag
- `tests/test_location.py` — 3 new tests: timezone boundaries, all-pings default, explicit limit

## 2026-03-16: Headlines briefing component

Added a `headlines` component to the briefing system that pre-fetches frontpages from major news sources via the browser container API. This replaces the standalone news cron jobs (news-morning/news-evening) with a proper briefing component that gets dedup, scheduling, and multi-component merging for free.

**Key changes:**
- `HEADLINE_SOURCES` registry with 7 built-in sources: AP, Reuters, Guardian, FT, Al Jazeera, Le Monde, Der Spiegel
- `_fetch_headlines()` pre-fetches frontpage text via the browse skill's browser container API, with per-source error handling and text truncation
- `headlines` component in `build_briefing_prompt()` injects raw frontpage text with synthesis instructions (group by theme, cross-source attribution, 10-15 stories)
- `headlines: dict` field on `BriefingDefaultsConfig` enables `headlines = true` boolean expansion from admin defaults
- Coexists with existing `news` component (email newsletters) — both can be enabled, Claude merges stories from both sources
- Briefing skill.md updated with cross-source attribution rules for frontpage sources

**Files added/modified:**
- `src/istota/skills/briefing/__init__.py` — Added `HEADLINE_SOURCES`, `_fetch_headlines()`, headlines component in prompt builder
- `src/istota/skills/briefing/skill.md` — Cross-source attribution, headline+newsletter merge instructions
- `src/istota/config.py` — `headlines` field on `BriefingDefaultsConfig`
- `config/config.example.toml` — `[briefing_defaults.headlines]` with default source list
- `deploy/ansible/defaults/main.yml` — Headlines defaults
- `deploy/ansible/templates/config.toml.j2` — Headlines section rendering
- `deploy/render_config.py` — Headlines section generation
- `tests/test_briefing.py` — 29 new tests (source registry, fetch function, prompt integration)

## 2026-03-16: Briefing deduplication and calendar timezone support

Two improvements to reduce repetition in briefings and give the bot visibility into calendar event timezones.

**Key changes:**
- Briefing digest: after each briefing, the result is saved to `.briefing_digest.md`. The next briefing prompt includes the previous digest with instructions to focus on new stories and only revisit a story if there's a material update.
- Calendar `timezone` field: `CalendarEvent` dataclass now captures the original TZID from iCalendar data. The CLI JSON output includes a `timezone` field (`"floating"` when no timezone is set), so the bot can answer questions about timezone handling without diving into source code.
- Calendar `--tz` flag on `create` and `update` CLI commands: events can now be created with proper timezone-aware datetimes (e.g., `--tz America/Los_Angeles` for flight bookings).
- Sensitive actions: calendar event deletion now explicitly requires confirmation.
- Fixed pre-existing test failures in `TestMigrateWorkspaceFiles` (tests didn't pre-create `workspace/` dir).

**Files modified:**
- `src/istota/skills/briefing/__init__.py` — Added `save_briefing_digest()`, `load_previous_briefing_digest()`, injected previous digest into prompt
- `src/istota/scheduler.py` — Save briefing digest after successful completion
- `src/istota/skills/calendar/__init__.py` — `timezone` field on `CalendarEvent`, TZID extraction, `--tz` CLI flag
- `src/istota/skills/calendar/skill.md` — Documented timezone field, `--tz` flag, floating vs tz-aware guidance
- `src/istota/skills/sensitive_actions/skill.md` — Explicit calendar deletion confirmation
- `tests/test_briefing.py` — `TestBriefingDigest` class (7 tests)
- `tests/test_storage.py` — Fixed workspace migration tests to pre-create `workspace/` dir

## 2026-03-15: Scripts directory and credential resources

Moved user scripts directory from `/Users/{user_id}/scripts/` into the bot directory at `/Users/{user_id}/{bot_dir}/scripts/`, consistent with exports/ and config/. Existing scripts auto-migrate on directory init.

Also added per-user Garmin Connect and Monarch Money credentials as resource entries (`type = "garmin"`, `type = "monarch"`) following the same pattern as Karakeep. Credentials land in `ResourceConfig.extra` dict and the executor reads them to set env vars. The `[garmin]`/`[monarch]` TOML section syntax is also supported and auto-converted to resources during config parsing. Garmin skill now declares `resource_types = ["garmin"]` for automatic skill selection when the user has credentials configured.

**Key changes:**
- Scripts directory moved under bot dir with auto-migration from old location
- Garmin/Monarch credentials use `[[resources]]` pattern instead of dedicated `UserConfig` fields
- Removed `garmin_email`, `garmin_password`, `monarch_session_token` from `UserConfig`
- Executor reads credentials from `resource.extra` dict (same pattern as Karakeep's `base_url`/`api_key`)
- Garmin skill.toml gets `resource_types = ["garmin"]`
- Ansible role updated: `user.toml.j2` renders garmin/monarch as resources, `defaults/main.yml` shows examples

**Files modified:**
- `src/istota/storage.py` — Scripts path under bot dir, migration logic, directory structure
- `src/istota/config.py` — Remove UserConfig credential fields, parse `[garmin]`/`[monarch]` sections as resources
- `src/istota/executor.py` — Read garmin/monarch from resource.extra instead of UserConfig fields
- `src/istota/skills/garmin/__init__.py` — Env var precedence for credentials
- `src/istota/skills/garmin/skill.toml` — Add `resource_types = ["garmin"]`
- `src/istota/skills/accounting/__init__.py` — Env var precedence for Monarch token
- `config/users/alice.example.toml` — Resource-based credential examples
- `deploy/ansible/defaults/main.yml` — Garmin/Monarch resource examples
- `deploy/ansible/templates/user.toml.j2` — Render garmin/monarch as resources
- `tests/test_storage.py` — Updated for scripts path and directory structure changes

## 2026-03-16: External prompt files for CRON.md jobs

Added `prompt_file` field to CRON.md scheduled jobs so long prompts can live in separate files instead of being inlined in the TOML block.

**Key changes:**
- `CronJob` dataclass gets `prompt_file: str` field. The loader resolves the file path against the Nextcloud mount and reads its contents as the prompt at parse time, so downstream code (scheduler, executor) sees a normal prompt string with no changes needed.
- `generate_cron_md()` preserves `prompt_file` references on round-trip serialization instead of inlining the resolved prompt text back into the TOML.
- Validation: `prompt_file` is mutually exclusive with `prompt` and `command`. Missing files are warned and skipped.
- Skill docs updated with the new field.

**Files modified:**
- `src/istota/cron_loader.py` — `prompt_file` field, file resolution in `load_cron_jobs()`, round-trip in `generate_cron_md()`
- `src/istota/skills/schedules/skill.md` — Documented `prompt_file` field
- `tests/test_cron_loader.py` — `TestPromptFile` class (8 tests)

## 2026-03-15: Progress message fixes and self-contained responses

Fixed several progress message edge cases and added a prompt-level fix for incomplete final responses. Also added an opt-in live text message feature for intermediate progress text (off by default).

**Key changes:**
- Fixed ack message stuck on "Riffing..." when task has zero tool calls (`all_descriptions` empty list was falsy).
- Fixed progress falling back to legacy mode after scheduler restart — reruns now always post an ack ("*Retrying…*") so edit-in-place works.
- Added opt-in live text message (`progress_show_text = true`, off by default): posts/edits a second Talk message with intermediate text events, then edits it with the final result on completion.
- Added Talk and email guidelines telling the model its final response is the only text shown, so it should be self-contained and not reference earlier status updates.
- Intermediate text prepending in executor (safety net) still active alongside the prompt-level approach.

**Files modified:**
- `src/istota/scheduler.py` — Ack on rerun, empty `all_descriptions` fix, live text message callback, result edit-in-place
- `config/guidelines/talk.md` — Self-contained response instruction
- `config/guidelines/email.md` — Self-contained response instruction
- `tests/test_progress_callback.py` — `TestLiveTextMessage` class (8 tests), updated edit-mode text event tests
- `tests/test_scheduler.py` — Updated rerun test

## 2026-03-15: FinViz fetch retry logic

Evening briefing on Mar 14 had no FinViz market data due to a transient browser API failure with no retry. The fetch function silently returned None and the briefing went out without market headlines, movers, futures, or economic data.

**Key changes:**
- `fetch_finviz_data()` now retries up to 2 times (3 total attempts) with increasing delays (5s, 10s) before giving up.
- Added warning log when FinViz data is missing from an evening briefing (`Evening briefing for {user_id} will have no FinViz data`).
- New tests for retry-then-succeed and retry-exhausted scenarios.

**Files modified:**
- `src/istota/skills/markets/finviz.py` — Retry loop with configurable `retries` parameter
- `src/istota/skills/briefing/__init__.py` — Warning log on missing FinViz data
- `tests/test_skills_finviz.py` — Retry tests, `retries=0` on failure tests to avoid slow sleeps

## 2026-03-14: Docker deployment expansion

Expanded the Docker deployment from a minimal quickstart to a fully configurable stack approaching parity with the Ansible role. All skills are now available, security layers are preserved, and optional services (browser, GPS webhooks) are integrated via Compose profiles.

**Key changes:**
- Dockerfile installs `--extra all` (accounting, whisper, garmin, memory-search) plus system deps (weasyprint libs, tesseract-ocr, bubblewrap).
- Entrypoint generates config.toml from ~170 environment variables covering every config section: scheduler intervals, conversation context, progress updates, sleep cycle, memory search, email, ntfy, browser, location, developer skill.
- Browser container and GPS webhook receiver integrated into main docker-compose.yml via Compose profiles (`--profile browser`, `--profile location`).
- Bubblewrap sandbox and skill credential proxy enabled by default in Docker. Bwrap gracefully degrades with a cached runtime probe (`_bwrap_available()`) when the kernel denies namespace creation (common in containers without CAP_SYS_ADMIN).
- OAuth token passthrough conditioned on actual bwrap availability, not just `sandbox_enabled` config — fixes auth failure when sandbox is configured but degrades at runtime, while preserving credential isolation when bwrap is functional.
- README quickstart expanded with step-by-step guide, optional services, configuration notes, and differences from bare metal. Marked as experimental.

**Files modified:**
- `docker/istota/Dockerfile` — `--extra all`, bubblewrap, weasyprint/tesseract system deps
- `docker/istota/entrypoint.sh` — Full config generation from env vars, security settings
- `docker/docker-compose.yml` — ~170 env var passthrough, browser + webhooks services
- `docker/.env.example` — Comprehensive configuration reference with defaults
- `src/istota/executor.py` — `_bwrap_available()` cached probe, conditional OAuth passthrough
- `README.md` — Expanded Docker quickstart, experimental notice

## 2026-03-14: Docker Compose deployment

Full Docker Compose stack that takes a new user from zero to a working Istota instance with a single `docker compose up`. Bundles Nextcloud, PostgreSQL, Redis, and the Istota scheduler in four containers with automatic provisioning.

**Key changes:**
- Docker Compose stack with postgres, redis, nextcloud, and istota services plus an init container for volume permissions.
- Istota Dockerfile: Python 3.12 + Node.js 20 + Claude Code + uv, builds at ~795MB.
- Nextcloud post-install provisioning script creates bot/user accounts, enables Talk/Calendar/External Storage apps, configures per-user external storage mounts on a shared Docker volume.
- Istota entrypoint handles API-based provisioning (Talk room creation), config.toml generation, DB init, and scheduler startup.
- Dual-mount file access: shared Docker volume (`/mnt/shared`) as the bot workspace (read-write), Nextcloud data volume (`/mnt/nc-data`) for read-only access to users' native NC files (Talk attachments, etc.).
- External Storage app presents the shared volume to users via the Nextcloud web UI, replacing the traditional NC share approach.
- Talk attachment fallback: when mount path lookup fails, scans `/mnt/nc-data/*/files/Talk/` for the file across all user directories.
- Pass `CLAUDE_CODE_OAUTH_TOKEN` through to Claude subprocess when sandbox is disabled (Docker deployments).
- Fixed vestigial `workspace/` directory creation in storage migration code.

**Files added:**
- `docker/docker-compose.yml` — Full compose stack definition
- `docker/istota/Dockerfile` — Istota container image
- `docker/istota/entrypoint.sh` — Container entrypoint with provisioning and config generation
- `docker/istota/provision-nc.sh` — Nextcloud post-install hook (user/app/storage setup)
- `docker/.env.example` — Documented environment variables
- `.dockerignore` — Build context exclusions

**Files modified:**
- `src/istota/executor.py` — Pass OAuth token when sandbox disabled
- `src/istota/scheduler.py` — Talk attachment fallback to NC data directory
- `src/istota/storage.py` — Don't create vestigial `workspace/` directory

## 2026-03-13: Fix network proxy killing streaming API connections

The CONNECT proxy's 30-second request-parsing timeout was persisting into the tunnel phase, causing streaming API responses to die mid-generation when Claude Code didn't send data for >30 seconds (normal during response streaming — the client is just receiving).

**Key changes:**
- Clear socket timeouts to `None` on both client and upstream sockets after the CONNECT tunnel is established, so the tunnel stays alive for the full duration of the API call.
- Enable TCP keepalive on upstream connections to detect silently dead connections (NAT timeouts, load balancer drops) instead of blocking forever on `recv`.
- Fix bridge script inside sandbox to use `shutdown(SHUT_WR)` instead of `close()` for graceful half-close, preventing data loss when one direction finishes before the other.
- Add debug logging in the bridge forwarding loop (direction, target host, errors) instead of silent `except OSError: pass`.
- Remove unused `timeout` parameter from `NetworkProxy.__init__` (was accepted and stored but never used).

**Files modified:**
- `src/istota/network_proxy.py` — Timeout fix, TCP keepalive, bridge script fix, debug logging, removed dead code
- `src/istota/executor.py` — Removed unused `timeout=` kwarg from `NetworkProxy()` call

## 2026-03-13: Fix lost intermediate text + Talk poller latency

Two fixes for response quality and Talk polling performance.

**Key changes:**
- Intermediate text blocks (status updates between tool calls) were being lost from task results. The `ResultEvent` from Claude Code's stream-json only contains the last assistant turn's text, so intermediate `TextEvent`s are now accumulated during streaming and prepended to the final result (deduplicated against the ResultEvent text).
- Talk poller `list_conversations()` and 6 other TalkClient methods used httpx's default 5s timeout, causing frequent ReadTimeout errors when Nextcloud was busy during task execution. Increased default to 15s via `DEFAULT_TIMEOUT` class constant.
- Conversation list is now cached with a 60s TTL. Most poll cycles skip the API call entirely. On timeout/error the cached list is used as fallback instead of aborting the cycle.
- Downgraded `list_conversations` failure from ERROR to WARNING (transient, self-recovering).
- Replaced manual `_participant_cache.clear()` in tests with an autouse fixture that resets both caches.

**Files modified:**
- `src/istota/executor.py` — Accumulate `TextEvent` texts, prepend missing ones to result
- `src/istota/talk.py` — Added `DEFAULT_TIMEOUT = 15`, applied to all bare `httpx.AsyncClient()` calls
- `src/istota/talk_poller.py` — Conversation list cache (60s TTL + error fallback), log level fix
- `tests/test_executor_streaming.py` — Tests for intermediate text accumulation and deduplication
- `tests/test_talk_poller.py` — Autouse cache reset fixture, conversation cache tests

## 2026-03-13: Network isolation for sandbox (--unshare-net + CONNECT proxy)

Outbound network access from the bwrap sandbox is now restricted to an allowlist of host:port pairs. Each task gets its own network namespace via `--unshare-net`. A CONNECT proxy on a Unix socket (outside the sandbox) tunnels allowed traffic; a TCP-to-Unix bridge inside the sandbox forwards from `127.0.0.1:18080` to the proxy socket. Direct network access from the sandbox is impossible.

**Key changes:**
- `NetworkProxy` class in `network_proxy.py`: Unix socket CONNECT proxy with domain allowlist, per-task lifecycle (same pattern as SkillProxy). No MITM — TLS is end-to-end.
- Bridge script (`net-bridge`): written to `.developer/` (RO inside sandbox), started as background process by a shell wrapper before `exec`ing Claude Code with `HTTPS_PROXY`/`HTTP_PROXY` env vars.
- `build_bwrap_cmd()` adds `--unshare-net`, bind-mounts proxy socket, wraps command in shell that starts bridge and execs claude with proxy env vars. Uses `"$@"` to preserve original argv.
- `_build_network_allowlist()`: merges default hosts (Anthropic API), PyPI (configurable), operator extras, and developer git remote hosts from config.
- Default allowlist: `api.anthropic.com:443`, `mcp-proxy.anthropic.com:443`, `pypi.org:443`, `files.pythonhosted.org:443`. Git hosts added when developer skill is selected.
- `[security.network]` config section: `enabled` (default true), `allow_pypi` (default true), `extra_hosts` (operator additions).
- Executor uses `contextlib.ExitStack` to manage both SkillProxy and NetworkProxy lifecycles.

**Files added:**
- `src/istota/network_proxy.py` — CONNECT proxy, bridge script, `write_bridge_script()`
- `tests/test_network_proxy.py` — Proxy lifecycle, blocking (403), tunneling (200 + bidirectional data), bridge script validation

**Files modified:**
- `src/istota/config.py` — Added `NetworkConfig` dataclass, nested in `SecurityConfig`, parsed from `[security.network]`
- `src/istota/executor.py` — Added `_build_network_allowlist()`, `net_proxy_sock` param on `build_bwrap_cmd()`, network proxy lifecycle with ExitStack
- `tests/test_sandbox.py` — Added `TestNetworkProxyBwrapIntegration`, `TestBuildNetworkAllowlist`, `TestNetworkConfigParsing`
- `config/config.example.toml` — Documented `[security.network]` section
- `deploy/ansible/defaults/main.yml` — Added `istota_security_network_*` vars
- `deploy/ansible/templates/config.toml.j2` — Renders `[security.network]` section

## 2026-03-13: Sandbox security fixes (red team findings 1-4)

Four fixes from a red team assessment that found the bwrap sandbox had credential exposure vectors. Network isolation (finding #5) is deferred.

**Key changes:**
- Scoped credential-fetch and skill CLI credentials per-skill via `_CREDENTIAL_SKILL_MAP`. The proxy now only returns credentials needed by the task's selected skills, and each skill CLI subprocess only gets its own credentials merged into env. Backward-compatible (None = allow all).
- Removed `CLAUDE_CODE_OAUTH_TOKEN` from `build_clean_env()` — Claude Code reads auth from `.credentials.json` directly, so the env var was an unnecessary exposure surface.
- Mounted `~/.claude/.credentials.json` as read-only in the sandbox (was RW) to prevent token persistence attacks.
- Mounted `.developer/` directory (credential-fetch scripts, git helpers) as read-only inside the sandbox to prevent script replacement attacks.
- Scoped Nextcloud mount for admin users — admins now get the same per-user mount as non-admins (own user dir + channel dir + explicit resources) instead of the full Nextcloud content tree. Cross-user file access requires a UserResource or the Nextcloud API skill.

**Files modified:**
- `src/istota/executor.py` — Added `_CREDENTIAL_SKILL_MAP`, `_allowed_credentials_for_skills()`, `_build_skill_credential_map()`. Removed OAuth token passthrough. Changed `.credentials.json` to RO mount. Added `.developer/` RO overlay. Unified admin/non-admin Nextcloud mount scoping. Removed `not is_admin` guard on per-resource mounts.
- `src/istota/skill_proxy.py` — Added `allowed_credentials` and `skill_credential_map` params to `SkillProxy`. Credential-fetch checks scope before returning. Skill CLI env merge uses per-skill credential map.
- `tests/test_security.py` — Added `TestCredentialSkillScoping` (15 tests) and OAuth token exclusion test.
- `tests/test_skill_proxy.py` — Added `TestProxyScopedCredentials` (7 tests) for credential-fetch and skill CLI scoping.
- `tests/test_sandbox.py` — Added `TestBuildBwrapCmdCredentials` and `TestBuildBwrapCmdDeveloperDir`. Updated admin mount tests for scoped behavior.

## 2026-03-13: Fix skill proxy socket invisible inside bwrap sandbox

The proxy socket at `/tmp/istota-proxy-{task_id}.sock` was invisible inside the bwrap sandbox because bwrap mounts a fresh tmpfs at `/tmp`. Restructured the executor flow so the proxy starts before `build_bwrap_cmd` runs, and the socket file is explicitly bind-mounted into the sandbox.

Also fixed `_warn_orphaned_email_output` deleting deferred email files for briefings. The function was meant to catch Talk users whose tasks mistakenly used `email output`, but it ran unconditionally and nuked legitimate deferred files before `post_result_to_email` could read them. Added a guard to skip tasks with email-related output targets.

**Key changes:**
- `build_bwrap_cmd` now accepts `proxy_sock` parameter and bind-mounts it (RO) into the sandbox
- Executor restructured: proxy starts before bwrap args are built (socket must exist for bind-mount)
- `_warn_orphaned_email_output` skips tasks with `source_type="email"` or `output_target` in `("email", "both", "all")`

**Files modified:**
- `src/istota/executor.py` — Added `proxy_sock` param to `build_bwrap_cmd`, restructured proxy/bwrap ordering
- `src/istota/scheduler.py` — Added guard condition to `_warn_orphaned_email_output`
- `tests/test_scheduler.py` — Added 3 tests for orphaned email skip cases

## 2026-03-13: Skill proxy phase 2 — isolate developer tokens

Extended the skill proxy to cover `GITLAB_TOKEN` and `GITHUB_TOKEN`. These were previously passed directly to Claude's env because they're consumed by shell scripts (git credential helpers, API wrappers), not by `istota-skill` CLI commands. Now when the proxy is enabled, a `credential-fetch` helper script queries the proxy socket at runtime, and the shell scripts call it instead of reading env vars.

**Key changes:**
- Added `credential` request type to proxy protocol (`{"type": "credential", "name": "GITLAB_TOKEN"}` → `{"value": "glpat-xxx"}`)
- Added `GITLAB_TOKEN` and `GITHUB_TOKEN` to `_PROXY_CREDENTIAL_VARS` (stripped from Claude's env when proxy enabled)
- New `credential-fetch` Python script written to `{dev_bin}/` — queries proxy socket for credential values
- Git credential helpers and API wrappers rewritten to use `credential-fetch` when proxy is enabled, env vars when disabled
- Fixed proxy socket path to use `/tmp` to avoid AF_UNIX path length limits

**Files modified:**
- `src/istota/skill_proxy.py` — Added credential lookup handler in `_handle_connection`
- `src/istota/executor.py` — Added tokens to `_PROXY_CREDENTIAL_VARS`, credential-fetch script generation, proxy-aware shell script templates
- `tests/test_skill_proxy.py` — Added `TestProxyCredentialLookup` (4 tests), updated token stripping test, added completeness checks
- `tests/test_executor.py` — Added `TestDeveloperProxyAwareScripts` (5 tests) for proxy-aware script generation

## 2026-03-12: Credential isolation via skill proxy

Added a Unix socket proxy that runs skill CLI commands with credentials injected server-side. When enabled, the Claude subprocess never sees secret env vars (CALDAV_PASSWORD, NC_PASS, SMTP_PASSWORD, IMAP_PASSWORD, KARAKEEP_API_KEY). A thin client (`istota-skill`) replaces `python -m istota.skills.X` in all skill docs and prompt templates, with a direct-execution fallback when the proxy is disabled.

**Key changes:**
- SkillProxy class: Unix socket server as context manager, threaded accept loop, skill allowlist validation, concurrent request handling
- `istota-skill` console script: connects to proxy socket or falls back to direct `python -m` execution
- Executor splits env into credential and clean dicts when `skill_proxy_enabled` is true, starts proxy around Claude execution
- All 13 skill.md files updated: `python -m istota.skills.X` → `istota-skill X`
- Prompt templates in executor.py updated to use `istota-skill` syntax
- Config: `skill_proxy_enabled` (default false) and `skill_proxy_timeout` (300s) in SecurityConfig

**Files added/modified:**
- `src/istota/skill_proxy.py` — New: SkillProxy server with allowlist, protocol, timeout handling
- `src/istota/skill_client.py` — New: thin client with proxy and direct-exec paths
- `tests/test_skill_proxy.py` — New: 28 tests (proxy lifecycle, protocol, client, credential splitting, allowlist validation)
- `pyproject.toml` — Added `istota-skill` entry point
- `src/istota/config.py` — Added `skill_proxy_enabled`, `skill_proxy_timeout` to SecurityConfig
- `src/istota/executor.py` — Added `_split_credential_env()`, `_PROXY_CREDENTIAL_VARS`, proxy lifecycle, updated prompt references
- `src/istota/skills/*/skill.md` (13 files) — CLI example updates
- `config/config.example.toml` — Documented new security fields
- `deploy/ansible/defaults/main.yml` — Added ansible vars, enabled by default
- `deploy/ansible/templates/config.toml.j2` — Added config template lines

## 2026-03-12: Consolidate briefing system and remove legacy code

Moved all briefing logic into `src/istota/skills/briefing/__init__.py`, replaced hardcoded `source_type == "briefing"` checks in executor.py with generic flags driven by skill metadata, then removed all backward-compatibility shims and dead code.

**Key changes:**
- Added `exclude_memory` and `exclude_resources` fields to `SkillMeta` in `_types.py`
- Created `skills/briefing/__init__.py` consolidating code from `briefing.py`, `briefing_loader.py`, and `scheduler.py`
- Executor computes `_skip_memory` and `_excluded_resource_types` from selected skill metas
- Deleted `briefing.py`, `briefing_loader.py`, `skills_loader.py` (were re-export shims)
- Deleted unused DB code: `BriefingConfig` dataclass, `get_briefing_configs()`, `add_briefing_config()`, `update_briefing_last_run()`
- Removed deprecated `progress_edit_mode` config field and its fallback logic
- Removed 9 legacy `account`/`himalaya_downloads_dir` parameters from email skill functions

**Files added/modified/deleted:**
- `src/istota/skills/briefing/__init__.py` — New: all briefing logic consolidated
- `src/istota/skills/_types.py` — Added `exclude_memory`, `exclude_resources` to SkillMeta
- `src/istota/skills/_loader.py` — Read new fields in loaders
- `src/istota/skills/briefing/skill.toml` — Added exclude flags
- `src/istota/executor.py` — Flag-based checks, import from `skills._loader` directly
- `src/istota/scheduler.py` — Changed imports, removed moved functions
- `src/istota/config.py` — Removed `progress_edit_mode` field and fallback
- `src/istota/db.py` — Removed dead briefing DB code
- `src/istota/skills/email/__init__.py` — Removed legacy parameters
- `src/istota/briefing.py` — Deleted
- `src/istota/briefing_loader.py` — Deleted
- `src/istota/skills_loader.py` — Deleted

## 2026-03-12: Skill dependency isolation and enhanced !skills

Moved heavy optional dependencies out of the base `dependencies` list into extras groups. Skills now declare their Python dependencies in `skill.toml`, and modules use import guards so the system stays importable without optional packages. The `!skills` command now shows availability status with install hints.

**Key changes:**
- Moved 11 packages (caldav, icalendar, yfinance, imap-tools, beancount, beanquery, fava, weasyprint, monarchmoney, pytesseract, garminconnect) from base deps to 6 new extras groups
- Added `all` extras group for full installs: `uv sync --extra all`
- Added `dependencies` field to 6 skill.toml files (garmin, transcribe, markets, accounting, calendar, email)
- Added `try/except ImportError` guards in garmin, transcribe, calendar, email modules
- Added `get_skill_availability()` to `_loader.py` for checking dep status
- Rewrote `!skills` command: groups by available/unavailable/disabled, shows install hints
- Added `!skills <name>` detail view (status, triggers, dependencies)
- Updated Ansible/install.sh to use `--extra all` by default (`istota_install_all_extras` flag)
- Deleted stale `packages/` directory from reverted external-package experiment

**Files modified:**
- `pyproject.toml` — Extras groups, reduced base deps to httpx/croniter/tomli/feedparser/requests
- `src/istota/skills/{garmin,transcribe,calendar,email}/__init__.py` — Import guards
- `src/istota/skills/{garmin,transcribe,markets,accounting,calendar,email}/skill.toml` — Added dependencies
- `src/istota/skills/_loader.py` — Added `get_skill_availability()`
- `src/istota/skills_loader.py` — Re-exported new function
- `src/istota/commands.py` — Rewrote `cmd_skills` with grouped output and detail view
- `deploy/ansible/{defaults/main.yml,tasks/main.yml,templates/istota-update.sh.j2}` — `--extra all` support
- `deploy/install.sh` — Default to `--extra all`
- `tests/test_skills_loader.py` — 8 new tests (availability, dependency exclusion)
- `tests/test_commands.py` — 3 new tests + updated existing for new output format

## 2026-03-09: Reverse geocoding and day summary for location skill

Integrated the standalone `reverse_geocode.py` script into the location skill. Coordinates are reverse geocoded via Nominatim and cached in a new `reverse_geocode_cache` table in the main DB (replacing the script's separate cache DB). GPS pings are clustered into stops with a greedy sequential algorithm, then resolved to place names via saved places, proximity matching, or reverse geocoding.

**Key changes:**
- Added `reverse_geocode_cache` table with rounded REAL keys (4 decimal places, ~11m precision)
- Added `reverse_geocode()` and `cluster_pings()` to shared `geo.py` module
- Added `reverse-geocode` and `day-summary` CLI subcommands to location skill
- Day summary: clusters pings → filters transit → resolves names → merges consecutive same-location stops

**Files added/modified:**
- `schema.sql` — Added `reverse_geocode_cache` table
- `src/istota/db.py` — Added `get_reverse_geocode()` and `cache_reverse_geocode()`
- `src/istota/geo.py` — Added `reverse_geocode()` (cache + Nominatim fallback) and `cluster_pings()`
- `src/istota/skills/location/__init__.py` — Added `cmd_reverse_geocode` and `cmd_day_summary`
- `src/istota/skills/location/skill.toml` — Added keywords for new subcommands
- `src/istota/skills/location/skill.md` — Documented new subcommands with output examples
- `tests/test_location.py` — 21 new tests across 5 test classes (96 total)

## 2026-03-09: Fix DST double-fire in scheduled jobs and briefings

After the spring-forward DST transition (March 8), scheduled jobs and briefings fired twice — once at the wrong time and once at the correct time. croniter miscomputes the next fire time when given a tz-aware datetime that crosses a DST boundary (e.g. PST→PDT). The fix strips timezone info before passing to croniter, working entirely with naive wall-clock times for the should-run comparison.

**Key changes:**
- Both `check_briefings()` and `check_scheduled_jobs()` now convert datetimes to naive local wall-clock times before croniter evaluation
- Conversion chain: UTC from DB → tz-aware UTC → `.astimezone(user_tz)` → `.replace(tzinfo=None)` → croniter
- All comparison paths naivified: last_run_at, created_at fallback, and today_start fallback
- `check_scheduled_jobs()` now uses `_now()` wrapper for testability (was `datetime.now()` directly)

**Files modified:**
- `src/istota/scheduler.py` — Naive datetime handling in `check_briefings()` and `check_scheduled_jobs()`
- `tests/test_scheduler.py` — 4 new DST tests: spring-forward no-double-fire and correct-time-fire for both jobs and briefings

## 2026-03-01: Calendar attendance via GPS correlation

Cross-references calendar events with GPS pings to confirm whether the user attended in-person events. Resolves event locations by matching against known places first, then forward geocoding via Nominatim (cached in DB). Filters out all-day events, events without locations, and virtual meetings (Zoom, Teams, etc). Uses a 30-minute buffer around event times and configurable radius (from matched place or default 200m).

**Key changes:**
- Extracted `haversine()` from `webhook_receiver.py` into shared `geo.py` module
- Added `geocode_cache` table for caching Nominatim results
- Added `attendance` subcommand to location skill CLI
- Added `geopy>=2.4` to location extras for forward geocoding
- Added `calendar` resource type to location skill for skill selection

**Files added/modified:**
- `src/istota/geo.py` — New shared module with `haversine()` and `_EARTH_RADIUS_M`
- `src/istota/webhook_receiver.py` — Imports haversine from `geo.py`
- `src/istota/db.py` — Added `get_cached_geocode()` and `cache_geocode()`
- `src/istota/skills/location/__init__.py` — Added `cmd_attendance()` with CalDAV integration, place matching, geocoding, proximity check
- `src/istota/skills/location/skill.toml` — Added `resource_types = ["calendar"]` and `"attendance"` keyword
- `src/istota/skills/location/skill.md` — Documented attendance subcommand
- `schema.sql` — Added `geocode_cache` table
- `pyproject.toml` — Added `geopy>=2.4` to location extras
- `tests/test_location.py` — 19 new tests (75 total): geocode cache, virtual detection, place matching, geocoding, full attendance command

## 2026-03-01: Per-job log channel suppression

Added `skip_log_channel` option for scheduled jobs. Frequent scheduled jobs (e.g., every few minutes) were spamming the per-user log channel with tool-by-tool execution logs. `silent_unless_action` already controls whether results are posted to Talk, but there was no way to suppress log channel output. The new field flows from CRON.md through the full pipeline: CronJob → ScheduledJob (DB) → Task → checked in process_one_task().

**Key changes:**
- `skip_log_channel: bool` field on CronJob, ScheduledJob, and Task dataclasses
- DB migrations for both `scheduled_jobs` and `tasks` tables
- Schema, sync, and generation support in cron_loader.py
- Scheduler suppresses log channel setup when task.skip_log_channel is True

**Files modified:**
- `src/istota/cron_loader.py` — CronJob field, parsing, generation, sync
- `src/istota/db.py` — ScheduledJob + Task fields, migrations, create_task, row readers
- `src/istota/scheduler.py` — Flow-through in check_scheduled_jobs, suppression in process_one_task
- `schema.sql` — Column additions to both tables
- `tests/test_cron_loader.py` — Parsing, generation, sync tests
- `tests/test_log_channel.py` — Suppression test in process_one_task
- `tests/test_scheduler.py` — Flow-through test in check_scheduled_jobs

## 2026-03-01: Webhook receiver review and fixes

Reviewed the location/webhooks implementation and fixed several issues. Renamed `location_receiver.py` to `webhook_receiver.py` and the systemd service from `istota-location` to `istota-webhooks` to support future webhook endpoints beyond GPS. Renamed nginx config from `istota-site.conf.j2` to `istota.conf.j2` since it now serves both site hosting and webhooks.

**Key changes:**
- Fixed wrong endpoint URL in LOCATION.md template (`/location` → `/webhooks/location`)
- Simplified dead code in state machine (removed `get_open_visit()` after `close_visit()` that always returned None)
- Added `client_max_body_size 10m` to nginx webhooks location block
- Renamed `istota_site_hostname` → `istota_hostname` across all Ansible templates (backward compat via Jinja2 `default()` filter)
- Added location ping cleanup to scheduler (`location_ping_retention_days`, default 365 days)
- Added Ansible migration tasks to stop/remove old `istota-location` service
- nginx is now also installed when `istota_location_enabled` is true (was site-only)

**Files modified:**
- `src/istota/webhook_receiver.py` — Simplified state machine dead code
- `src/istota/storage.py` — Fixed endpoint URL in LOCATION.md template
- `src/istota/config.py` — Added `location_ping_retention_days` to SchedulerConfig
- `src/istota/scheduler.py` — Added location ping cleanup to `run_cleanup_checks()`
- `deploy/ansible/defaults/main.yml` — Renamed hostname variable, added ping retention default
- `deploy/ansible/tasks/main.yml` — Service rename, migration tasks, nginx condition fix
- `deploy/ansible/handlers/main.yml` — Handler rename
- `deploy/ansible/templates/` — Renamed and updated nginx, service, config, and HTML templates

## 2026-03-01: Location awareness (Overland GPS tracking)

Added GPS-based location tracking using the Overland iOS app as data source. This is the first HTTP-receiving endpoint in istota — everything else is polling-based. The receiver runs as a separate FastAPI service behind nginx, accepting Overland's GeoJSON batch payloads.

Places are defined in LOCATION.md (TOML-in-markdown, same pattern as CRON.md and BRIEFINGS.md). A hysteresis state machine requires 2 consecutive pings at a new place before confirming a transition, preventing spurious enter/exit events. On transitions, configurable actions fire via ntfy, Talk, or cron_prompt.

**Key changes:**
- New DB tables: `location_pings`, `places`, `visits`, `location_state` with indexes
- Location loader (`location_loader.py`): parses LOCATION.md, syncs places to DB, builds token-to-user map
- FastAPI receiver (`webhook_receiver.py`): POST /webhooks/location endpoint with token auth, haversine place resolution, visit state machine with hysteresis
- Location skill CLI: `current`, `history`, `places`, `learn` subcommands. `learn` saves current GPS position as a named place in both DB and LOCATION.md
- Storage seeding: LOCATION.md template and example, added to user workspace setup
- Config: `LocationReceiverConfig` dataclass with `enabled` and `webhooks_port` fields
- Ansible: systemd service template, conditional deployment, handler for restarts
- Optional dependency group `[location]` for fastapi + uvicorn

**Files added:**
- `src/istota/location_loader.py` — LOCATION.md parser and place sync
- `src/istota/webhook_receiver.py` — FastAPI webhook receiver with location state machine
- `src/istota/skills/location/` — Skill directory (CLI, skill.toml, skill.md, __main__.py)
- `deploy/ansible/templates/istota-webhooks.service.j2` — Systemd service for webhook receiver
- `tests/test_location.py` — 45 tests covering loader, DB, haversine, state machine, CLI

**Files modified:**
- `schema.sql` — Added location tables and indexes
- `src/istota/db.py` — Location DB functions (~300 lines)
- `src/istota/storage.py` — Location path, template, seeding
- `src/istota/config.py` — LocationReceiverConfig
- `pyproject.toml` — `location` optional dependency group
- `deploy/ansible/` — defaults, tasks, handlers, config template

## 2026-02-28: Review and fix garmin skill

Reviewed the new garmin skill committed from the Zorg fork. Fixed several issues: env var names didn't match what executor.py provides, skill.toml description was stale from an earlier calendar-sync design, workspace path was hardcoded to "zorg" instead of using `bot_dir_name`, and token cache used a global path instead of per-user temp directories.

**Key changes:**
- Fixed env var names: `NEXTCLOUD_MOUNT` → `NEXTCLOUD_MOUNT_PATH`, `ISTOTA_USER` → `ISTOTA_USER_ID`
- Removed `_default_config_path()` that hardcoded `zorg` workspace name — skill now requires `GARMIN_CONFIG` env var (set by executor) or `--config` flag
- Added `GARMIN_CONFIG` wiring in executor.py, following same pattern as `INVOICING_CONFIG`/`ACCOUNTING_CONFIG` (resolves via `storage.get_user_config_path()`)
- Token cache now uses `$ISTOTA_DEFERRED_DIR/garmin_tokens` (per-user temp dir) instead of global `/srv/app/zorg/data/garmin_tokens`
- Updated skill.toml: fixed description, removed incorrect `resource_types = ["calendar"]`, added more keywords
- Added `garminconnect>=0.2.0` dependency (was already in pyproject.toml, ran `uv sync`)

**Files modified:**
- `src/istota/skills/garmin/__init__.py` — Fixed env vars, removed hardcoded paths, per-user token dir
- `src/istota/skills/garmin/skill.toml` — Corrected description and keywords
- `src/istota/skills/garmin/skill.md` — Updated config path and token dir docs
- `src/istota/executor.py` — Added `GARMIN_CONFIG` env var wiring
- `uv.lock` — Added garminconnect and transitive deps

## 2026-02-28: Fix DB lock during briefing prefetch, browser stability

Production crash: `check_briefings()` held a DB write transaction open while doing slow network I/O (yfinance, FinViz, IMAP newsletter fetching). When FinViz timed out (~2 min), the Talk poller thread couldn't access the DB, causing "database is locked" errors and eventually a deadlocked main loop.

Also improved browser container stability with memory-aware session management and automatic health-based restarts.

**Key changes:**
- Split `check_briefings()` into 3 phases: short DB read (check schedules), network prefetch (no DB held), short DB write (create tasks). Function now takes `db_path` instead of `conn` and manages its own connections.
- Browser container: added memory pressure checks (reject new sessions above 85%, evict idle above 80%), renderer process limits, JS heap caps.
- Browser session management: close tabs fully instead of navigating to about:blank, adjust tab indices after close.
- Added browser health check cron (`/etc/cron.d/`): restarts container if unhealthy (every 2 min check) plus daily forced restart at 5 AM.

**Files added/modified:**
- `src/istota/scheduler.py` — Refactored `check_briefings()` to avoid holding DB locks during network I/O
- `tests/test_scheduler.py` — Updated all `check_briefings` calls to pass `db_path` instead of `conn`
- `docker/browser/browse_api.py` — Memory pressure checks, tab closing, session eviction
- `docker/browser/chrome.py` — Added `--renderer-process-limit=4` and `--js-flags=--max-old-space-size=256`
- `deploy/ansible/templates/istota-browser-health.cron.j2` — New cron template for health check + daily restart
- `deploy/ansible/tasks/main.yml` — Added browser health cron deployment tasks
- `deploy/ansible/defaults/main.yml` — Added `istota_browser_restart_cron` default

## 2026-02-26: Email skill clarity, progress fixes, fava restart

Three targeted fixes addressing bugs found in production usage.

**Key changes:**
- Clarified email skill docs: added decision section explaining `send` vs `output`, with explicit warning about the common mistake of using `output` for Talk-originated email requests (which silently drops the email since the scheduler only delivers deferred output for email-source tasks).
- Progress messages in Talk now collapse multi-line tool output (e.g. inline python scripts) to a single line before display. Applied to both Talk progress callback and log channel callback.
- Moved fava restart from accounting skill to scheduler. The `_restart_fava()` call inside the accounting CLI was running inside the bubblewrap sandbox where `sudo`/`systemctl` aren't available, so it silently failed. The scheduler now restarts fava after any successful task that invoked the accounting skill, running outside the sandbox.

**Files modified:**
- `src/istota/skills/email/skill.md` — Rewrote with `send` vs `output` decision guide and common mistake callout
- `src/istota/scheduler.py` — Added `_restart_fava_service()`, newline collapsing in progress callbacks
- `src/istota/skills/accounting/__init__.py` — Removed dead `_restart_fava()` and its call sites
- `tests/test_progress_callback.py` — Added test for multi-line message collapsing
- `tests/test_scheduler.py` — Added `TestRestartFavaService` tests
- `tests/test_skills_accounting.py` — Replaced old `TestRestartFava` with `TestAppendToLedgerNoRestart`

## 2026-02-26: Per-user log channel & progress style modes

Added a per-user log channel for verbose task execution logs and replaced the binary progress edit mode with a configurable progress style system.

**Log channel:**
- Dedicated Talk room per user that receives real-time tool call streaming during task execution
- Enabled by setting `log_channel` (Talk room token) in per-user config; no separate toggle
- Posts initial message on first tool call, edits in-place on every subsequent call (no rate limiting)
- Resolves conversation tokens to display names via Talk API with process-level caching
- Final message shows success/failure status with error details on failure
- Source label format: `[task_id channel_name]` or `[task_id source_type]` for non-Talk sources
- Composes with existing Talk progress callback as a composite when both are active

**Progress style:**
- Replaced `progress_edit_mode` (bool) with `progress_style` (string): `"replace"`, `"full"`, `"legacy"`, `"none"`
- `"replace"` (new default): shows latest tool call + elapsed time, e.g. `⏳ *Reading config.py…* (4s)`. No rate limiting. Final: `Done — 12 actions (18s)`
- `"full"`: previous append-all behavior (maps from `progress_edit_mode = true`)
- `"legacy"`: individual posted messages (maps from `progress_edit_mode = false`)
- `"none"`: silent, still accumulates descriptions for log channel and `actions_taken`
- Backward compat: existing configs with `progress_edit_mode` auto-map to the right style

**Files added:**
- `tests/test_log_channel.py` — 28 tests for log channel (config, formatting, callbacks, integration)

**Files modified:**
- `src/istota/config.py` — Added `log_channel` to UserConfig, `progress_style` to SchedulerConfig
- `src/istota/scheduler.py` — Log channel functions (`_resolve_channel_name`, `_make_log_channel_callback`, `_finalize_log_channel`), rewritten `_make_talk_progress_callback` with style branching, composite callback propagation
- `config/config.example.toml` — Updated progress docs
- `config/users/alice.example.toml` — Documented `log_channel`
- `deploy/ansible/defaults/main.yml` — Added `log_channel` to user example, replaced `progress_edit_mode` with `progress_style`
- `deploy/ansible/templates/user.toml.j2` — Renders `log_channel`
- `deploy/ansible/templates/config.toml.j2` — Renders `progress_style`
- `deploy/render_config.py` — Added `log_channel` to user field list
- `tests/test_progress_callback.py` — Updated existing tests for style system, added replace/none/backward-compat tests

## 2026-02-26: Talk progress edit-in-place

Progress updates during task execution now edit the initial ack message in-place instead of posting multiple separate messages. This reduces conversation noise from up to 5 progress posts per task down to a single message that updates with a running list of tool actions. The final result still posts as a new message.

**Key changes:**
- `TalkClient.edit_message()` — new method using `PUT /ocs/v2.php/apps/spreed/api/v1/chat/{token}/{messageId}`
- `edit_talk_message()` async helper in scheduler (wraps the client, returns bool)
- `_format_progress_body()` — formats accumulated tool descriptions with plain header, italicized tool lines, `[+N earlier]` truncation for long lists
- `_make_talk_progress_callback()` rewritten to support edit mode: accumulates tool descriptions only (text events skipped), edits ack message in-place (rate-limited), no `progress_max_messages` cap needed
- Ack message ID captured from initial post and passed through to callback
- Final cleanup edit after task completes shows "Done — N actions taken" summary
- Legacy multi-post mode preserved when `progress_edit_mode=False` (text progress and deduplication still work)
- Two new config fields: `progress_edit_mode` (default: true), `progress_max_display_items` (default: 20)
- Descriptions use their native emoji from stream_parser (📄, ✏️, ⚙️, etc.) — no double-prefixing

**Files modified:**
- `src/istota/talk.py` — Added `edit_message()` to TalkClient
- `src/istota/config.py` — Added `progress_edit_mode` and `progress_max_display_items` to SchedulerConfig
- `src/istota/scheduler.py` — Added `edit_talk_message()`, `_format_progress_body()`, rewrote progress callback, added ack ID capture and final cleanup edit
- `config/config.example.toml` — Documented new fields
- `deploy/ansible/defaults/main.yml` — Added Ansible defaults
- `deploy/ansible/templates/config.toml.j2` — Added template vars
- `tests/test_talk.py` — 2 new tests for `edit_message()` (success + HTTP error)
- `tests/test_progress_callback.py` — 14 new tests for edit mode, format helper, text event filtering, and `edit_talk_message`

## 2026-02-25: !export command for conversation history

New `!export` command that exports a Talk channel's complete conversation history to a file in the user's Nextcloud workspace. Uses the Talk API directly (rather than the pruned DB cache) to fetch the full history. Supports incremental appends for repeated exports of the same channel.

**Key changes:**
- `!export [markdown|text]` command — exports to `/Users/{user_id}/{bot_dir}/exports/conversations/{token}.md` (or `.txt`)
- Full history pagination via Talk API (`lookIntoFuture=0` with `lastKnownMessageId`, backwards through all messages)
- Incremental exports: metadata header tracks `last_id`, subsequent `!export` appends only new messages
- Message coalescing: consecutive messages from the same actor grouped under a single header
- System messages (joins, leaves, calls) filtered out, only user comments included
- Frontmatter with channel title (from `get_conversation_info`) and participant list
- Three new `TalkClient` methods: `get_conversation_info()`, `fetch_full_history()`, `fetch_messages_since()`

**Files modified:**
- `src/istota/talk.py` — Added `get_conversation_info()`, `fetch_full_history()`, `fetch_messages_since()` methods
- `src/istota/commands.py` — Added `!export` command handler with formatting helpers (`_format_messages_markdown`, `_format_messages_text`, `_parse_export_metadata`, `_filter_user_messages`)
- `tests/test_talk.py` — 12 new tests for Talk API methods (conversation info, full history pagination, forward pagination)
- `tests/test_commands.py` — 15 new tests for export command and helpers (full/incremental/empty/filtered/format variants)

## 2026-02-24: Browse skill anti-hallucination & click-through navigation

When researching news for briefings, Claude would fetch hub pages (apnews.com, bbc.com) then fabricate plausible-looking article URLs from headline text instead of using actual `href` values. Additionally, some sites detect bot behavior when URLs are loaded directly rather than clicked from the referring page. Fixed both problems with documentation and a new convenience subcommand.

**Key changes:**
- New "Researching from hub/index pages" section in skill.md documenting the click-through workflow (`interact --click` navigates within session, preserving cookies/referrer/state, returns full page content)
- New "URL discipline" rules: never construct/guess URLs from text, always use `href` values from links array
- New `links` subcommand: fetches a page and returns only the links array (no page text), with optional `--selector` for extracting links from specific elements via CSS
- `links --selector` parses `<a href>` from HTML fragments returned by `/extract`, supports both URL+selector and session+selector modes

**Files modified:**
- `src/istota/skills/browse/skill.md` — Added research workflow, URL discipline, and `links` command docs
- `src/istota/skills/browse/__init__.py` — Added `cmd_links()`, `links` subparser, href extraction from HTML
- `tests/test_skills_browse.py` — 8 new tests (3 parser + 5 command: basic, selector, session+selector, empty, error)

## 2026-02-24: Personality pass — cybernetic space octopus

Coherent personality sweep to make Istota feel more like a cybernetic space octopus and less like a generic bot. Updated persona, progress messages, error messages, and emoji usage across the codebase.

**Key changes:**
- Persona: established octopus identity ("cybernetic space octopus", "eight arms", "comfortable in the deep end")
- Progress messages: replaced bland acks, added cephalopod verbs (Inking, Tentacling, Suckering, Jetting) and cheeky ones (Instigating, Scheming, Finagling, Gallivanting, Machinating)
- Error messages: subtle octopus flavor ("the deep stared back", "all eight arms", "drifted too deep", "resurfacing")
- Dropped "Sorry," prefix on error messages (contradicted persona's no-groveling rule), replaced with 🐙
- Talk guidelines: ✓ → 🐙 for done/ack emoji
- Task agent progress emoji: 🤖 → 🐙

**Files modified:**
- `config/persona.md` — Octopus identity in character description
- `config/guidelines/talk.md` — Ack emoji change
- `src/istota/scheduler.py` — Progress messages (44 total), error messages, error prefix
- `src/istota/stream_parser.py` — Task tool emoji
- `tests/test_scheduler.py` — Updated error message assertion
- `tests/test_stream_parser.py` — Updated tool emoji assertions

## 2026-02-24: Memory system improvements

Six-part enhancement to the multi-tiered memory system. Dated memories are now auto-loaded into prompts, BM25 recall surfaces relevant past context without LLM calls, and a memory size cap prevents prompt bloating. Sleep cycle extraction now includes task provenance references, and an optional nightly USER.md curation pass promotes durable facts from dated memories.

**Key changes:**
- Auto-load recent dated memories into prompts (`auto_load_dated_days`, default 3 days, skip briefings)
- BM25 memory recall using task prompt as query (`auto_recall` config, independent of context triage)
- Memory size cap (`max_memory_chars`) with truncation order: recalled → dated → warn
- Memory provenance in sleep cycle extraction (`ref:TASK_ID` references in dated memories)
- Optional USER.md curation via sleep cycle (`curate_user_memory` config, Claude Sonnet second pass)
- Changed defaults: `MemorySearchConfig.enabled` → `True`, `ChannelSleepCycleConfig.enabled` → `True`
- 44 new tests covering all features (2275 total)

**Files modified:**
- `src/istota/config.py` — New fields: `auto_load_dated_days`, `curate_user_memory`, `auto_recall`, `auto_recall_limit`, `max_memory_chars`
- `src/istota/executor.py` — `_recall_memories()`, `_apply_memory_cap()`, dated memories auto-load, `recalled_memories` in `build_prompt()`
- `src/istota/sleep_cycle.py` — `build_curation_prompt()`, `curate_user_memory()`, `ref:TASK_ID` in extraction prompt
- `config/config.example.toml` — Documented new config fields
- `deploy/ansible/defaults/main.yml` — Added ansible variables for all new fields
- `deploy/ansible/templates/config.toml.j2` — Template entries for new fields
- `tests/test_executor.py` — 20 new tests (recall, cap, dated memories, build_prompt)
- `tests/test_sleep_cycle.py` — 14 new tests (provenance, curation prompt, curate_user_memory)
- `tests/test_config.py` — 10 new tests (defaults and TOML loading for new fields)

## 2026-02-24: Sleep cycle default + no-mount state bug fix

Sleep cycle was disabled by default (`enabled = false`), which meant deployments that didn't explicitly set it in config.toml silently had no nightly memory extraction. Changed the default to `true` (both in code and Ansible role).

Also fixed a bug where `process_user_sleep_cycle()` and `process_channel_sleep_cycle()` returned without updating state when `config.use_mount` was `False`. This caused the sleep cycle to reprocess the same tasks on every run, never advancing its cursor — an infinite loop of wasted Claude calls that produced no output.

**Key changes:**
- `SleepCycleConfig.enabled` default: `False` → `True`
- `istota_sleep_cycle_enabled` Ansible default: `false` → `true`
- Added `_update_state()` / `_update_channel_state()` calls before early return in no-mount path
- Tightened AGENTS.md (455 → 188 lines, moved implementation detail to `.claude/rules/`)

**Files modified:**
- `src/istota/config.py` — Changed `SleepCycleConfig.enabled` default to `True`
- `src/istota/sleep_cycle.py` — Added state update in no-mount early return (both user and channel)
- `deploy/ansible/defaults/main.yml` — Changed `istota_sleep_cycle_enabled` to `true`
- `AGENTS.md` — Condensed from 455 to 188 lines
- `tests/test_config.py` — Updated default assertions for sleep cycle
- `tests/test_channel_sleep_cycle.py` — Added state verification to no-mount test

## 2026-02-24: Conversation context recency window and lookback fix

Added time-based recency filtering for conversation context. Instead of always including a fixed number of messages, the system now includes a guaranteed minimum (default 10) plus any additional messages within a configurable time window (e.g., 2 hours). This means a rapid 20-message chat session loads fully, while 20 messages spread over 3 days only loads today's messages.

Also fixed a bug where `lookback_count` was not applied when `use_selection=false` — the Talk path was loading all messages from the cache (up to `talk_context_limit=100`) instead of capping at `lookback_count`.

Also switched the deploy update script from `git pull` to `git reset --hard origin/$BRANCH` to prevent "divergent branches" errors when the agent accidentally modifies tracked files on the server.

**Key changes:**
- New config fields: `context_recency_hours` (0 = disabled) and `context_min_messages` (default 10)
- Recency filter applied in both Talk and DB context paths, before selection/triage
- `lookback_count` now caps message list before the selection check, acting as a hard limit regardless of triage mode
- Fixed `load_config()` not parsing `context_recency_hours` and `context_min_messages` from TOML (fields existed on dataclass but explicit constructor call didn't include them)
- Lookback cap applied before recency window in Talk path (was after, so recency ran on ~48 messages then lookback still gave 25)
- Deploy update script uses `git reset --hard` instead of `git pull`/`git checkout`

**Files modified:**
- `src/istota/config.py` — Added fields to `ConversationConfig` and `load_config()` parser
- `src/istota/executor.py` — Added `_apply_recency_window_talk()` and `_apply_recency_window_db()`, lookback cap before recency in Talk path
- `src/istota/context.py` — Moved `lookback_count` cap before `use_selection` check in `select_relevant_talk_context()`
- `deploy/ansible/defaults/main.yml` — Added new config vars with inline docs for all conversation settings
- `deploy/ansible/templates/config.toml.j2` — Added new config fields
- `deploy/ansible/templates/istota-update.sh.j2` — Replaced `git pull`/`git checkout` with `git reset --hard`
- `tests/test_executor.py` — Added `TestRecencyWindowTalk` (7 tests) and `TestRecencyWindowDb` (6 tests)

## 2026-02-24: Notification reply context scoping

When a user replies to a scheduled job's output (e.g., "Drinking" in reply to a water reminder), the bot was loading 25+ messages of conversation history, picking up unrelated topics, and sending confusing multi-sentence responses. Fixed by scoping context for replies to scheduled/briefing notifications — now only the parent notification is loaded as context, with a prompt hint nudging brief responses for simple acknowledgments.

**Key changes:**
- Added `_detect_notification_reply()` in executor.py to identify when a task is a reply to a scheduled or briefing notification
- Modified context loading in `execute_task()` to scope context narrowly for notification replies instead of loading full conversation history
- Fixed `get_reply_parent_task()` SELECT in db.py to include `actions_taken`, `scheduled_job_id`, and `queue` columns

**Files modified:**
- `src/istota/executor.py` — Added `_detect_notification_reply()`, modified context block with notification reply branch
- `src/istota/db.py` — Added missing columns to `get_reply_parent_task()` SELECT
- `tests/test_executor.py` — Added `TestDetectNotificationReply` (5 tests) and `TestNotificationReplyContextScoping` (3 tests)

## 2026-02-24: Skill selection cleanup

Reduced unnecessary context pollution by tightening skill selection. Previously, skills with `resource_types` (e.g., accounting, calendar, bookmarks) were loaded into every task prompt just because the user had a matching resource configured — even when the conversation had nothing to do with that skill. Now resource-type skills also require a keyword match in the prompt before loading.

Also removed the unused `notes_file` resource type and `notes` skill, which was redundant with `reminders_file` and `todo_file`.

**Key changes:**
- Resource-type skills now require both a matching resource AND a keyword hit in the prompt. Skills with `source_types` (e.g., calendar/notes for briefings) still load unconditionally for those source types.
- Removed `notes` skill directory (`skill.toml` + `skill.md`), executor resource section for `notes_file`, example config entry, and test references.

**Files modified:**
- `src/istota/skills/_loader.py` — Changed resource_types from standalone match to keyword gate
- `src/istota/skills/notes/` — Removed (skill.toml + skill.md)
- `src/istota/executor.py` — Removed `notes_file` resource section in `build_prompt()`
- `src/istota/config.py` — Removed `notes_file` from resource type comment
- `config/users/alice.example.toml` — Removed `notes_file` example resource
- `tests/test_skills_loader.py` — Updated and added tests for new selection behavior
- `tests/test_config.py` — Updated resource test from `notes_file` to `reminders_file`

## 2026-02-24: Talk message cache bug fixes — bot responses in context

Extended debugging session to fix bot responses being completely absent from conversation context after the poller-fed cache migration. The root cause was a multi-layered issue involving streaming progress deduplication, race conditions between the poller and scheduler threads, and SQLite upsert semantics.

**Problem chain:**
1. When `progress_text_max_chars=0`, the full bot response is sent as a streaming progress message. The dedup logic then sets `post_talk_message=None`, so `post_result_to_talk()` is never called — no `:result` message is ever posted to Talk.
2. All bot messages in the cache had `:ack` or `:progress` reference_ids, which `build_talk_context()` correctly filters out (they're noise, not final answers). Zero `:result` messages existed.
3. Initial fix attempts tried re-tagging the last `:progress` message to `:result` via an UPDATE query after task completion. But the poller and scheduler run in separate threads — the progress message might not be cached yet when the re-tag runs (race condition), causing the UPDATE to match 0 rows.
4. Even when the re-tag succeeded, the poller's `INSERT OR REPLACE` would overwrite the re-tagged reference_id back to `:progress` on its next poll.

**Key changes:**
- Capture the Talk message ID returned by `post_result_to_talk()` in the progress callback (`last_progress_msg_id`). Previously the return value was discarded.
- After task completion, use the captured progress message ID to directly upsert a `:result` cache entry. This works whether the poller has cached the message or not — no race condition.
- Changed `upsert_talk_messages` from `INSERT OR REPLACE` to `ON CONFLICT DO UPDATE` with a CASE clause that preserves `:result` reference_ids. This prevents the poller from overwriting result tags on subsequent polls.
- Show task IDs in formatted context output: `Bot (task 42):` instead of generic `Bot:`.
- Changed `cleanup_old_talk_messages` from time-based retention (which triggered a backfill cycle every minute) to per-conversation cap (`talk_cache_max_per_conversation`, default 200).
- Made `talk_cache_max_per_conversation` configurable via Ansible.

**Commits in this session:**
- `caade40` Fix talk cache cleanup/backfill cycle
- `4a9228e` Make talk cache cap configurable
- `a56d8ef` Show task IDs in talk context formatting
- `a891231` Cache bot result messages immediately after posting
- `b49e536` Cache result even when deduped as progress
- `021fae1` Fix NameError in result cache when deduped
- `e3acb85` Add info log for result cache debugging
- `122b5b8` Re-tag progress as result instead of synthetic cache entry
- `bdf010a` Preserve :result tags in cache across poller upserts
- `4737a8f` Fix race condition in talk message cache result tagging (final fix)

**Files modified:**
- `src/istota/scheduler.py` — Progress callback captures msg ID, result cache logic simplified from re-tag to direct upsert
- `src/istota/db.py` — `upsert_talk_messages` uses `ON CONFLICT DO UPDATE` with `:result` preservation CASE; `cleanup_old_talk_messages` changed to per-conversation cap
- `src/istota/context.py` — `format_talk_context_for_prompt` shows task IDs for bot messages
- `src/istota/config.py` — Added `talk_cache_max_per_conversation` to `SchedulerConfig`
- `config/config.example.toml` — Documented `talk_cache_max_per_conversation`
- `deploy/ansible/defaults/main.yml` — Added `istota_scheduler_talk_cache_max_per_conversation`
- `deploy/ansible/templates/config.toml.j2` — Renders `talk_cache_max_per_conversation`
- `tests/test_db.py` — Tests for per-conversation cap, `:result` preservation in upsert
- `tests/test_talk_context.py` — Updated format tests for task ID display

## 2026-02-23: Poller-fed Talk message cache

Eliminated per-task HTTP calls to the Talk API for conversation context. The talk poller already sees every message via long-polling — now it stores them in a local `talk_messages` SQLite table. Context building reads from this cache (fast local query, zero API calls). Backfills existing conversations on first encounter via `fetch_chat_history()`. Old messages cleaned up on the same retention schedule as tasks.

**Key changes:**
- New `talk_messages` table with composite PK `(conversation_token, message_id)`.
- Four DB functions: `upsert_talk_messages`, `get_cached_talk_messages`, `has_cached_talk_messages`, `cleanup_old_talk_messages`.
- Poller stores all received messages in cache before per-message processing.
- Backfill on first encounter: if no cache exists for a conversation, fetches history via API (non-fatal on failure).
- `_build_talk_api_context()` reads from DB cache instead of calling `asyncio.run(client.fetch_chat_history())`.
- Cleanup of old cached messages added to `run_cleanup_checks()`, reusing `task_retention_days`.

**Files modified:**
- `schema.sql` — Added `talk_messages` table
- `src/istota/db.py` — Cache CRUD functions
- `src/istota/talk_poller.py` — Store after polling, backfill on first encounter
- `src/istota/executor.py` — `_build_talk_api_context` reads from DB cache
- `src/istota/scheduler.py` — Cleanup in `run_cleanup_checks()`
- `tests/test_db.py` — 10 new tests (`TestTalkMessageCache`)
- `tests/test_talk_poller.py` — 3 new tests (`TestTalkMessageCacheIntegration`)

## 2026-02-23: Talk API-based conversation context

Replaced the DB-only conversation context pipeline with one that fetches recent messages directly from the Talk chat API. This gives the bot the actual conversation visible to users — including messages from all participants in group chats, not just bot-processed interactions. Bot messages are tagged with `referenceId` fields for correlation back to tasks (actions_taken enrichment). Falls back to DB-based context on API failure.

**Key changes:**
- Added `reference_id` parameter to `TalkClient.send_message()` and tagged all bot messages (ack, progress, result) with `istota:task:{id}:{tag}`.
- Extracted `clean_message_content()` from `talk_poller.py` to `talk.py` for reuse.
- Added `fetch_chat_history()` to `TalkClient` for context fetching.
- Added `TalkMessage` dataclass and `get_task_metadata_for_context()` batch lookup in `db.py`.
- New Talk context pipeline in `context.py`: `build_talk_context()`, `select_relevant_talk_context()`, `format_talk_context_for_prompt()`.
- Wired into `executor.py` with graceful fallback to DB path on Talk API failure.
- Added `talk_context_limit` config field (default 100).

**Files added/modified:**
- `src/istota/talk.py` — `reference_id` param, `fetch_chat_history()`, `clean_message_content()` moved here
- `src/istota/talk_poller.py` — Imports `clean_message_content` from `talk.py`
- `src/istota/db.py` — `TalkMessage` dataclass, `get_task_metadata_for_context()`
- `src/istota/context.py` — Talk API context pipeline functions
- `src/istota/executor.py` — `_build_talk_api_context()`, `_build_db_context()` refactor
- `src/istota/scheduler.py` — `reference_id` on all `send_message()` calls
- `src/istota/config.py` — `talk_context_limit` field
- `config/config.example.toml` — Documented `talk_context_limit`
- `deploy/ansible/defaults/main.yml` — `istota_conversation_talk_context_limit`
- `deploy/ansible/templates/config.toml.j2` — Renders `talk_context_limit`
- `tests/test_talk_context.py` — New: 36 tests for Talk-based context
- `tests/test_talk.py` — Extended: referenceId, fetch_chat_history, clean_message_content tests
- `tests/test_talk_integration.py` — Extended: referenceId round-trip, context fetch tests
- `tests/test_scheduler.py` — Updated assertions for reference_id parameter

## 2026-02-23: Fix auto-update script blocking on dirty uv.lock

The auto-update cron job was failing because `uv sync` regenerates `uv.lock` on the server, and the next `git pull` refuses to merge over the dirty file. Added `git checkout -- uv.lock` before both the branch pull and tag checkout paths.

**Files modified:**
- `deploy/ansible/templates/istota-update.sh.j2` — Reset uv.lock before git pull/checkout

## 2026-02-23: Fix scheduled notification replies losing context

When a user replies "Done" to a scheduled notification (e.g., vitamins reminder), the bot had no context because `get_conversation_history` excludes scheduled tasks by design, and the previous single-task injection could be displaced by a silent NO_ACTION job in the same room. Extended the injection to fetch the last N tasks instead of just 1.

**Key changes:**
- Renamed `get_previous_task()` → `get_previous_tasks()` in `db.py` — returns a list of up to N recent completed tasks (oldest-first), unfiltered by source_type.
- Updated executor injection block to iterate over the list, dedup against existing history, and sort after injection.
- Added `previous_tasks_count` config field to `[conversation]` section (default 3) so the injection depth is configurable.

**Files modified:**
- `src/istota/db.py` — Renamed function, added `limit` parameter, returns `list[ConversationMessage]`
- `src/istota/executor.py` — Updated injection block to handle list, uses config value
- `src/istota/config.py` — Added `previous_tasks_count` to `ConversationConfig`
- `config/config.example.toml` — Documented new field
- `deploy/ansible/defaults/main.yml` — Added `istota_conversation_previous_tasks_count`
- `deploy/ansible/templates/config.toml.j2` — Renders new field
- `tests/test_db.py` — Added `TestGetPreviousTasks` (8 tests)

## 2026-02-23: Fix briefing email formatting + auto-update cron

Fixed two bugs in briefing email delivery introduced when the deferred email output file was wired up for briefings. Also added an Ansible-managed auto-update mechanism.

**Key changes:**
- Briefing emails contained raw markdown syntax (bold, italic, links) because the deferred file path bypassed `_strip_markdown()`. Added markdown stripping for briefing plain text emails in the deferred file path as a safety net.
- Duplicate HTML-formatted briefing emails were sent because the email skill was keyword-matched for briefing tasks, causing the model to call `email send` directly during execution on top of the scheduler's own delivery. Added instruction to briefing prompt telling the model not to use email commands.
- Added Ansible-managed auto-update cron job (`istota_auto_update_enabled`, disabled by default). Polls git for new commits/tags every 5 minutes, runs `uv sync`, DB migrations, and restarts the scheduler. Supports both branch mode and tag-based semver mode.

**Files added/modified:**
- `src/istota/briefing.py` — Added "do not use email commands" instruction to briefing prompt
- `src/istota/scheduler.py` — Added `_strip_markdown()` in deferred file path for briefing emails
- `deploy/ansible/defaults/main.yml` — Added `istota_auto_update_enabled` and `istota_auto_update_cron` vars
- `deploy/ansible/tasks/main.yml` — Auto-update script and cron deployment tasks
- `deploy/ansible/templates/istota-update.sh.j2` — Update script (git fetch, uv sync, DB migrate, restart)
- `deploy/ansible/templates/istota-update.cron.j2` — Cron entry template

## 2026-02-22: Emissaries sync script

Added a script to sync `config/emissaries.md` from the canonical public emissaries repo at `https://forge.cynium.com/stefan/emissaries`. This keeps istota's constitutional principles up to date with the upstream source without needing git submodules or CI pipelines.

**Key changes:**
- Added `scripts/sync-emissaries.sh` to fetch the latest `emissaries.md` via curl from the raw file URL.
- Updated `config/emissaries.md` to match the current canonical version.

**Files added/modified:**
- `scripts/sync-emissaries.sh` — New script to fetch latest emissaries.md from upstream
- `config/emissaries.md` — Updated to latest canonical version

## 2026-02-21: Fix E2BIG for large prompts

Prompts were passed as CLI arguments to the `claude` command, which hit the Linux 128KB `execve()` argument limit when conversation context or emissaries made the assembled prompt too large. Switched to passing prompts via stdin instead.

**Key changes:**
- Prompt removed from the `cmd` list — now passed via `input=` to `subprocess.run()` (simple mode) and written to `process.stdin` (streaming mode).
- `_execute_simple()`, `_execute_streaming_once()`, and `_execute_streaming()` accept a `prompt` parameter, threaded from `execute_task()`.
- Bypasses the kernel limit entirely since stdin has no size constraint.

**Files modified:**
- `src/istota/executor.py` — Removed prompt from cmd, added stdin-based prompt passing to all execution paths
- `tests/test_executor.py` — Updated 6 test assertions to read prompt from `call_args.kwargs["input"]` instead of `cmd[2]`

## 2026-02-21: Documentation sync

Updated all project documentation to reflect recent changes: nextcloud client refactor, calendar skill enhancements, cron catch-up fix, config search path addition, and test count growth.

**Key changes:**
- AGENTS.md: nextcloud skill upgraded from doc-only to CLI, calendar skill description updated, config search path now includes `~/src/config/config.toml`, added cron expression change catch-up prevention note, test count updated to ~2170/48 files.
- ARCHITECTURE.md: added `nextcloud_client.py` to subsystems table, added nextcloud and calendar skill CLIs, fixed per-channel gate description (queues instead of rejects), test count updated.
- `.claude/rules/skills.md`: added calendar `update` subcommand and `--week` flag, added nextcloud skill CLI section.
- `.claude/rules/config.md`: updated config search order.
- README.md: test count updated.

**Files modified:**
- `AGENTS.md` — 6 updates across project structure, config, skills, scheduling, testing sections
- `ARCHITECTURE.md` — 5 updates across subsystems, skills, scheduler, testing sections
- `.claude/rules/skills.md` — Calendar CLI update + nextcloud CLI section
- `.claude/rules/config.md` — Config search order
- `README.md` — Test count
- `TODO.md` — Checked off nextcloud client refactor

## 2026-02-21: Cron catch-up prevention + email double-send fix

Fixed two scheduling/delivery bugs: cron expression changes triggering catch-up runs for past slots, and duplicate email delivery when Claude sends directly during execution.

**Key changes:**
- `sync_cron_jobs_to_db()` now resets `last_run_at` to `datetime('now')` when a job's cron expression changes, preventing immediate catch-up runs for past time slots in the new expression.
- `_parse_email_output()` returns `None` instead of raw-text fallback when no structured email JSON is found, preventing the scheduler from sending a duplicate email when Claude already sent via `email send` during execution.
- `post_result_to_email()` skips delivery when no structured output is available (deferred file or inline JSON), logging that the email was likely sent directly.

**Files modified:**
- `src/istota/cron_loader.py` — Detect cron expression change and reset last_run_at
- `src/istota/scheduler.py` — Remove raw-text fallback from email parser, add None guard in delivery
- `tests/test_cron_loader.py` — Split state preservation test, add cron change reset test
- `tests/test_scheduler.py` — Update fallback tests to expect None instead of raw-text dict

## 2026-02-21: Nextcloud client refactor + calendar/scheduling fixes

Consolidated scattered Nextcloud HTTP code (OCS + WebDAV) into a shared `nextcloud_client.py` module, and fixed several skill gaps found during a calendar/scheduling audit.

**Key changes:**
- Extracted `nextcloud_client.py` — shared OCS GET/POST, WebDAV owner lookup, share management, sharee search. Replaces duplicated httpx code in `nextcloud_api.py`, `storage.py`, `shared_file_organizer.py`, and `tasks_file_poller.py`.
- Added Nextcloud skill CLI (`python -m istota.skills.nextcloud`) for share management: `list-shares`, `create-share`, `delete-share`, `search-sharees`, `share-folder`.
- Fixed email output misuse bug: scheduled Talk-targeted jobs were incorrectly using `python -m istota.skills.email output`, producing empty output. Root cause — prompt never told Claude the task's output target. Now `build_prompt()` includes `Source:` and `Output target:` header lines, and the email tool instruction explicitly says "Do NOT use when output target is talk".
- Added calendar `update` CLI subcommand (summary, start, end, location, description, clear flags).
- Added `--week` flag to calendar `list` (next 7 days, mutually exclusive with `--date`).
- Documented `once = true` field in schedules skill.md (auto-delete job after successful execution).
- Updated calendar skill.md with `update` examples, `--week` flag, and missing API functions (`get_week_events`, `get_event_by_uid`).
- AGENTS.md: corrected admin_only skill list (schedules is no longer admin-only).

**Files added:**
- `src/istota/nextcloud_client.py` — Shared Nextcloud HTTP client
- `src/istota/skills/nextcloud/__init__.py` — Nextcloud skill CLI
- `src/istota/skills/nextcloud/__main__.py` — CLI entry point
- `tests/test_nextcloud_client.py` — Unit tests for nextcloud_client
- `tests/test_nextcloud_client_integration.py` — Live integration tests
- `tests/test_nextcloud_skill_cli.py` — Skill CLI tests

**Files modified:**
- `src/istota/executor.py` — Source/output_target in prompt header, fixed email tool line
- `src/istota/nextcloud_api.py` — Delegates to nextcloud_client
- `src/istota/storage.py` — Delegates to nextcloud_client
- `src/istota/shared_file_organizer.py` — Delegates to nextcloud_client
- `src/istota/tasks_file_poller.py` — Delegates to nextcloud_client
- `src/istota/skills/calendar/__init__.py` — `update` subcommand, `--week` flag, `_get_date_range()` helper
- `src/istota/skills/calendar/skill.md` — Updated docs
- `src/istota/skills/schedules/skill.md` — Documented `once` field
- `src/istota/skills/nextcloud/skill.md` — Updated with CLI commands
- `tests/test_executor.py` — Prompt output target tests
- `tests/test_skills_calendar.py` — Calendar CLI tests (update, --week, parser)

## 2026-02-21: Tag-based release deployment

Deployments now pin to semver tags instead of tracking the tip of `main`. Both `install.sh` and Ansible support a `repo_tag` setting (`"latest"` resolves to the highest `v*` tag, a specific tag like `"v0.2.0"` checks out that tag directly, empty string falls back to branch tracking). New installs default to `repo_tag = "latest"`.

Also added `istota --version` which prints the version from `pyproject.toml` (currently `0.1.0`).

**Files modified:**
- `deploy/install.sh` — `deploy_code()` resolves and checks out tags; new `REPO_TAG` variable; wizard writes `repo_tag = "latest"`
- `deploy/ansible/defaults/main.yml` — `istota_repo_tag: "latest"`
- `deploy/ansible/tasks/main.yml` — Tag fetch/resolve/checkout block after git clone
- `src/istota/cli.py` — `--version` flag via `importlib.metadata`
- `DEVLOG.md` — This entry

## 2026-02-21: Deployment polish — Nextcloud note, Docker CPU fix

Small deployment improvements for new users and single-core VMs.

**Key changes:**
- Added Nextcloud All-in-One link to README.md and deploy/README.md prerequisites for users starting fresh
- Fixed browser container failing on single-core machines (`cpus: "2"` exceeded available CPUs)
- install.sh now uses `$(nproc)` to cap CPU limit to available cores
- Ansible default changed from hardcoded `"2"` to `{{ ansible_processor_vcpus }}`
- Removed CPU limit from development docker-compose and deploy docs (memory limit is sufficient)

**Files modified:**
- `README.md` — Added Nextcloud All-in-One parenthetical in Requirements
- `deploy/README.md` — Added Nextcloud All-in-One note, removed CPU limit from browser example
- `deploy/install.sh` — Dynamic CPU limit via `$(nproc)`
- `deploy/ansible/defaults/main.yml` — CPU limit uses `ansible_processor_vcpus`
- `docker/docker-compose.browser.yml` — Removed CPU limit

## 2026-02-21: Claude CLI npm fallback verification fix

When the prebuilt Claude CLI binary fails (e.g., unsupported CPU on older VMs), install.sh falls back to installing via npm. Previously, the verification step only checked `$ISTOTA_HOME/.local/bin/claude` — which doesn't exist after an npm install — so it would report the CLI as missing even though it was functional on the system PATH.

**Key changes:**
- After npm fallback install, create a symlink at `$ISTOTA_HOME/.local/bin/claude` pointing to the npm-installed binary so the rest of the script (systemd PATH, services) works uniformly
- Verification now falls back to `command -v claude` if the `.local/bin` path doesn't exist
- npm fallback verifies `command_exists claude` after install before declaring success

**Files modified:**
- `deploy/install.sh` — npm fallback symlink, resilient verification

## 2026-02-21: Install wizard optional feature prompts and setup

The install wizard previously prompted for some optional features (email, memory search, sleep cycle, browser) but didn't actually set up several of them. Other optional features documented in deploy/README.md (whisper, ntfy, backups, channel sleep cycle) weren't prompted at all. Now all optional features are prompted in the wizard and deployed during installation.

**Key changes:**
- Wizard prompts for channel sleep cycle, whisper (with model selection), ntfy (server/topic/token), automated backups, and browser VNC password
- New `setup_browser_container()` — installs Docker, creates browser.env and docker-compose.browser.yml, builds and starts the container
- New `setup_whisper()` — pre-downloads the selected whisper model after venv is ready
- New `setup_backups()` — deploys backup script with path substitution and cron for DB (every 6h) and files (nightly)
- `render_config.py` adds `WHISPER_MAX_MODEL` env var to systemd service when whisper enabled
- `deploy_code()` reads `whisper.enabled` and adds `--extra whisper` to `uv sync`
- Review screen and settings file include all new feature states
- Post-install summary only lists Fava and Nginx as features not set up by the script

**Files modified:**
- `deploy/install.sh` — Wizard state vars, feature prompts, setup functions, main flow, summary
- `deploy/render_config.py` — WHISPER_MAX_MODEL env var in systemd service
- `deploy/README.md` — Updated optional features intro

## 2026-02-21: Email output tool + developer skill hardening

Addressed four open issues from the Zorg issue tracker. The headline change replaces the fragile JSON-as-text email output pattern with a dedicated CLI tool that writes structured output to a deferred file — eliminating the transcription corruption (smart-quote substitution) that caused raw JSON to be delivered to users. The developer skill gained mandatory pre-submission checks for namespace verification, MR/PR response verification, and a prohibition on editing live production source files.

**Key changes:**
- New `email output` CLI subcommand writes `task_{id}_email_output.json` to deferred dir (same pattern as subtasks/tracking)
- Scheduler checks for deferred email output file before falling back to `_parse_email_output()` (backward compat)
- Smart-quote normalization (Try 4) added to `_parse_email_output()` as a safety net for the legacy path
- Warning log when fallback body looks like malformed JSON
- Developer skill: namespace verification before MR/PR creation (abort on mismatch)
- Developer skill: response verification after MR/PR creation (parse response, verify via list query)
- Developer skill: no live source editing rule (`/srv/app/*/src/` is off-limits)
- Updated prompt instruction, email skill docs, and email guidelines to reference the output tool

**Files added/modified:**
- `src/istota/skills/email/__init__.py` — Added `cmd_output()` and `output` CLI subcommand
- `src/istota/scheduler.py` — Added `_load_deferred_email_output()`, smart-quote Try 4, malformed JSON warning
- `src/istota/executor.py` — Updated prompt instruction for email output tool
- `src/istota/skills/email/skill.md` — Rewrote reply format docs for output tool
- `config/guidelines/email.md` — Updated for output tool
- `src/istota/skills/developer/skill.md` — Pre-submission checks, response verification, namespace assertion
- `tests/test_scheduler.py` — 10 new tests (deferred email output + smart quotes)
- `tests/test_skills_email.py` — 5 new tests (output CLI)

## 2026-02-21: Install script hardening

End-to-end testing of install.sh on a fresh Debian VM uncovered a series of issues with the mount service, Claude CLI installation, file permissions, and system dependencies.

**Key changes:**
- Fixed rclone mount service: `Type=notify` → `Type=simple` (rclone doesn't send sd_notify)
- Added `fuse3` to system packages, switched `ExecStop` to `fusermount3` for Debian 13+
- Install Claude CLI as the istota user (not root), so files are owned correctly
- Always create `/usr/local/bin/claude` symlink (not just on fresh install)
- Run `setup_claude_cli` on `--update` too, not just fresh install
- Chown mount point directory to istota user before starting mount service
- Fixed `${#_WIZ_USER_IDS[@]:-0}` bad substitution in summary output
- Verification now shows Claude binary path for easier debugging
- Sleep cycle (nightly memory extraction) enabled by default
- Fixed test command in summary to use full venv path

**Files modified:**
- `deploy/install.sh` — Mount service, Claude CLI install, permissions, defaults

## 2026-02-21: Interactive install wizard

Rewrote `deploy/install.sh` with a polished 7-step interactive wizard for first-time setup on Debian/Ubuntu VMs. The wizard validates Nextcloud connectivity and credentials in real time, auto-generates the obscured rclone password (eliminating a confusing manual step), and produces all config files through the existing `render_config.py` pipeline. Added `--dry-run` mode that runs the full wizard and generates config into a temp directory without touching the system — useful for testing on macOS or previewing what would be deployed.

**Key changes:**
- Pre-flight checks: OS detection, internet connectivity, disk space, Python version
- Nextcloud URL validation (tests `/status.php`) and credential verification (OCS API)
- Auto-obscure rclone password from the app password after rclone install
- Claude OAuth token can be provided during setup or authenticated later
- Post-install verification: checks service status, mount, CLI, database
- `--dry-run` flag for local testing without root or Linux
- `render_config.py`: added GitHub developer settings, `CLAUDE_CODE_OAUTH_TOKEN` to secrets.env

**Files modified:**
- `deploy/install.sh` — Major rewrite (717 → 1200 lines)
- `deploy/render_config.py` — GitHub secrets, Claude OAuth token support

## 2026-02-21: README rewrite

Replaced the emoji-laden listicle README with a cleaner, more functional version. Requirements and quick start moved to the top. Features consolidated into prose paragraphs grouped by theme. Dropped the "Why Istota?" and "Should I try Istota?" sections (those belong on the website). Added optional dependency groups and git clone to quick start.

**Files modified:**
- `README.md` — Full rewrite

## 2026-02-21: Emissaries / persona split

Separated constitutional principles ("emissaries") from persona/character into distinct layers. Emissaries define what the agent is and owes — foundational principles about autonomy, responsibility, the public/private distinction, obligations to third parties, and cognitive hygiene. These are global-only and not user-overridable. Persona defines character, communication style, and operational behavior — customizable per user via PERSONA.md. Emissaries are injected before persona in every system prompt.

Also tightened both documents to remove cross-layer repetition (power/access principle now owned by emissaries, push-back/opinions consolidated, "ask first" deduplicated) and aligned persona to use "principal" terminology consistent with emissaries.

**Key changes:**
- New `config/emissaries.md` — constitutional principles document
- Updated `config/persona.md` — character layer only, references emissaries for principles
- `emissaries_enabled` config field (default true) with TOML parsing
- `load_emissaries()` in executor (global only, no `{BOT_NAME}` substitution)
- `build_prompt()` accepts and injects emissaries before persona
- Ansible defaults and config template updated

**Files added/modified:**
- `config/emissaries.md` — New constitutional principles document
- `config/persona.md` — Replaced with character-only persona template
- `src/istota/config.py` — Added `emissaries_enabled` field and parsing
- `src/istota/executor.py` — Added `load_emissaries()`, updated `build_prompt()` and `execute_task()`
- `config/config.example.toml` — Documented `emissaries_enabled` setting
- `deploy/ansible/defaults/main.yml` — Added `istota_emissaries_enabled`
- `deploy/ansible/templates/config.toml.j2` — Added template line
- `tests/test_executor.py` — 7 new tests (load + prompt integration)

## 2026-02-19: CLAUDE_CODE_OAUTH_TOKEN env var for Claude CLI auth

Claude CLI previously required running `claude login` interactively on the server, which could break when tokens expired. Added support for passing `CLAUDE_CODE_OAUTH_TOKEN` as an environment variable — generated locally via `claude setup-token` and stored in Ansible vault. Claude CLI picks this up automatically with no credentials file or refresh needed.

**Key changes:**
- Added `istota_claude_code_oauth_token` Ansible variable with vault comment
- Token templated into `secrets.env.j2` (loaded via systemd `EnvironmentFile=`)
- `build_clean_env()` in executor passes token through in restricted mode (permissive inherits it via `os.environ`)
- Heartbeat and `!check` execution tests inherit the token automatically through `build_clean_env()`
- Ansible login reminder updated to mention both auth options

**Files modified:**
- `deploy/ansible/defaults/main.yml` — Added `istota_claude_code_oauth_token` variable
- `deploy/ansible/templates/secrets.env.j2` — Added `CLAUDE_CODE_OAUTH_TOKEN` template line
- `src/istota/executor.py` — Pass `CLAUDE_CODE_OAUTH_TOKEN` in restricted-mode `build_clean_env()`
- `deploy/ansible/tasks/main.yml` — Updated reminder message with both auth methods

## 2026-02-19: Queue gated messages instead of discarding them

When a user sent a second message while the bot was still processing the first, the per-channel gate sent "Still working on a previous request" but permanently discarded the message by advancing the poll state past it. The user's message was lost and never processed. Fixed by removing the `continue` after the gate check so the message falls through to normal task creation. The scheduler already handles ordering — tasks are processed serially per user via `claim_task()`.

**Key changes:**
- Per-channel gate now queues gated messages as tasks instead of discarding them
- "Still working" notification still sent so the user knows there's a queue
- Test updated: asserts both notification sent AND task created

**Files modified:**
- `src/istota/talk_poller.py` — Removed `continue` from channel gate block
- `tests/test_talk_poller.py` — Updated gate test to expect task creation
- `AGENTS.md` — Updated per-channel gate documentation

## 2026-02-19: Progress text dedup and intermediate output guidance

When `progress_show_text` is enabled with `progress_text_max_chars = 0` (unlimited), intermediate assistant text sent as progress could repeat in the final result. Added deduplication that handles both exact matches (skip entirely) and prefix matches (strip already-seen prefix from final output). Dedup only applies when text is unlimited to avoid dangling partial sentences from truncated progress. Also added prompt guidance telling Claude to keep intermediate text minimal and save detailed output for the final response.

**Key changes:**
- Progress callback tracks sent texts via `callback.sent_texts` attribute
- Final result dedup: exact match → skip, prefix match → strip (only when `progress_text_max_chars = 0`)
- Dedup compares against actually-sent text (truncated `msg`), not raw input
- Prompt rules updated: intermediate text should be brief, detailed output saved for final response

**Files modified:**
- `src/istota/scheduler.py` — Progress text tracking, dedup logic before `post_result_to_talk`
- `src/istota/executor.py` — Updated output rules for both admin and non-admin prompts

## 2026-02-19: Make progress text max chars configurable

The progress callback had a hardcoded 200-char truncation for intermediate assistant text messages. Made this configurable via `progress_text_max_chars` (default 200, 0 = unlimited) so that `progress_show_text = true` can surface full intermediate responses when needed.

**Key changes:**
- Added `progress_text_max_chars` to `SchedulerConfig` dataclass and `load_config()`
- Progress callback uses configurable limit instead of hardcoded `message[:200]`
- Log line also uses the truncated `msg` instead of re-slicing

**Files modified:**
- `src/istota/config.py` — Added `progress_text_max_chars: int = 200`
- `src/istota/scheduler.py` — Updated `_make_talk_progress_callback` to use config value
- `deploy/ansible/defaults/main.yml` — Added `istota_scheduler_progress_text_max_chars`
- `deploy/ansible/templates/config.toml.j2` — Added template line
- `config/config.example.toml` — Documented new setting

## 2026-02-19: Fix channel gate blocking after !stop

After `!stop`, the cancelled task stays in `running` status until the worker thread cleans up. If the user sends a new message in that window, the per-channel gate sees the still-running task and rejects the message with "Still working on a previous request." Fixed by excluding tasks with `cancel_requested = 1` from the gate check. Also added a prompt note telling the bot where its JSONL execution logs live, so it can retrieve full output from previous tasks when users report truncated responses.

**Key changes:**
- `has_active_foreground_task_for_channel()` now excludes cancelled tasks (`AND cancel_requested = 0`)
- Added prompt rule pointing the bot to `~/.claude/projects/` for execution JSONL logs

**Files modified:**
- `src/istota/db.py` — Added `cancel_requested = 0` filter to channel gate query
- `src/istota/executor.py` — Added rule 8 about JSONL log location to admin prompt
- `tests/test_db.py` — Added `test_false_when_cancel_requested`

## 2026-02-19: Fix load_config default for user_max_foreground_workers

Code review of the multi-worker feature found a mismatch: the dataclass default for `user_max_foreground_workers` was updated to 2 but the `load_config()` fallback was left at 1. In production (config loaded from TOML), deployments without an explicit setting would silently get 1 worker per user instead of the intended 2.

**Key changes:**
- Fixed `load_config()` fallback from 1 to 2 to match the `SchedulerConfig` dataclass default
- Added regression test verifying `load_config()` defaults match dataclass defaults
- Updated `.claude/rules/scheduler.md` docs (still showed old default of 1)

**Files modified:**
- `src/istota/config.py` — Fixed `sched.get("user_max_foreground_workers", 1)` → `2`
- `tests/test_config.py` — Added `test_load_config_user_worker_defaults_match_dataclass`
- `.claude/rules/scheduler.md` — Updated default in config intervals table

## 2026-02-19: Multi-worker per user

The previous worker pool keyed workers by `(user_id, queue_type)`, which silently capped each user to exactly 1 foreground worker regardless of `user_max_foreground_workers`. A user with an active task in Room A couldn't get a concurrent worker for Room B. Fixed by switching to 3-tuple keys with slot indices and correcting two bugs in the dispatch formula.

**Key changes:**
- Worker keys changed from `(user_id, queue_type)` to `(user_id, queue_type, slot)` 3-tuple
- Fixed dispatch formula: `min(cap, pending) - active` → `min(cap - active, pending)` — old formula wouldn't spawn a second worker when one was busy with a running task
- Fixed slot assignment to fill gaps (e.g., if slot 0 exits while slot 1 is running, reuse slot 0)
- Changed `user_max_foreground_workers` default from 1 to 2 (matching Ansible defaults)
- `UserWorker` takes a `slot` parameter, thread names include slot index
- Added `count_pending_tasks_for_user_queue()` DB function to avoid spawning idle workers

**Files modified:**
- `src/istota/scheduler.py` — 3-tuple worker keys, multi-slot dispatch logic, updated `UserWorker` and `_on_worker_exit`
- `src/istota/config.py` — Changed `user_max_foreground_workers` default to 2
- `src/istota/db.py` — Added `count_pending_tasks_for_user_queue()`
- `config/config.example.toml` — Updated default value
- `tests/test_scheduler.py` — 10 new tests in `TestMultiWorkerPerUser`, 5 existing tests updated
- `tests/test_db.py` — 5 new tests for `count_pending_tasks_for_user_queue`
- `tests/test_config.py` — Updated default assertion
- `AGENTS.md`, `.claude/rules/scheduler.md` — Updated worker pool docs

## 2026-02-19: Configurable worker concurrency

Replaced the single `max_total_workers`/`reserved_interactive_workers` approach with three-tier concurrency control: per-channel gate (reject duplicate foreground tasks), separate instance-level fg/bg caps, and per-user worker limits with global defaults.

**Key changes:**
- Added `max_foreground_workers` (default 5) and `max_background_workers` (default 3) instance-level caps to SchedulerConfig
- Added `user_max_foreground_workers` (default 2) and `user_max_background_workers` (default 1) global per-user defaults
- Added per-user `max_foreground_workers` and `max_background_workers` overrides in UserConfig (0 = use global default)
- Resolution chain: per-user override > global per-user default > hardcoded default
- Added per-channel gate in Talk poller — rejects messages when an active fg task exists for the same conversation, sends "still working" response
- Added `has_active_foreground_task_for_channel()` DB query
- Rewrote `WorkerPool.dispatch()` to use separate fg/bg caps instead of shared total cap
- Removed legacy `max_total_workers` and `reserved_interactive_workers` fields entirely

**Files added/modified:**
- `src/istota/config.py` — New SchedulerConfig/UserConfig fields, `effective_user_max_fg/bg_workers()` on Config
- `src/istota/db.py` — Added `has_active_foreground_task_for_channel()`
- `src/istota/talk_poller.py` — Per-channel gate before task creation
- `src/istota/scheduler.py` — Rewrote WorkerPool.dispatch() for separate fg/bg caps
- `tests/test_db.py` — 7 new tests for channel gate query
- `tests/test_config.py` — 9 tests for config fields, global defaults, resolution chain
- `tests/test_talk_poller.py` — 3 new tests for channel gate behavior
- `tests/test_scheduler.py` — 3 new + 4 updated tests for concurrency caps
- `config/config.example.toml` — Updated worker pool settings
- `config/users/alice.example.toml` — Added per-user worker limit examples
- `deploy/ansible/` — Updated defaults, config template, user template
- `deploy/render_config.py` — Updated scheduler field list
- `ARCHITECTURE.md`, `AGENTS.md`, `.claude/rules/scheduler.md`, `.claude/rules/config.md` — Updated docs

## 2026-02-19: Group chat reply threading — final response only

Reply threading and @mentions in group chats were being applied to every message including intermediate progress updates (ack, tool use notifications), making the chat noisy. Fixed so only the final response gets reply_to and @mention; progress updates are sent as plain messages.

**Key changes:**
- Added `use_reply_threading` parameter to `post_result_to_talk()` (default `False`)
- Only the final result delivery passes `use_reply_threading=True`
- Ack messages and streaming progress updates use the default (no threading)
- Added test verifying progress updates skip reply threading in group chats

**Files modified:**
- `src/istota/scheduler.py` - Gated reply threading on `use_reply_threading` param
- `tests/test_scheduler.py` - Updated existing tests, added progress update test (5 tests total)

## 2026-02-19: Group chat reply threading and @mentions

In multi-user Talk rooms, the bot now replies to the original message and @mentions the triggering user. This makes it clear what message the bot is responding to and ensures the user gets a notification.

**Key changes:**
- `post_result_to_talk()` passes `reply_to=task.talk_message_id` for the first message part in group chats
- First message part prepends `@{user_id}` so the user gets a Nextcloud Talk notification
- Subsequent split parts remain standalone (no reply threading or @mention)
- DM behavior unchanged

**Files modified:**
- `src/istota/scheduler.py` - Updated `post_result_to_talk()` with group chat reply_to and @mention logic
- `tests/test_scheduler.py` - Added `TestPostResultToTalk` (4 tests: DM, group chat, split messages, missing message ID)

## 2026-02-18: Multi-user Talk room participation

In group rooms with multiple participants, istota now only responds when @mentioned instead of replying to every message. Rooms with exactly 2 participants (bot + 1 user) still behave like DMs. Conversation context in group chats shows usernames as speaker labels for multi-user attribution.

**Key changes:**
- `is_bot_mentioned()` checks `messageParameters` for `mention-user`/`mention-federated-user` matching bot username (excludes `mention-call`/@all)
- `clean_message_content()` updated to strip bot's own @mention from prompt and replace other mentions with `@DisplayName`
- `_is_multi_user_room()` async function with 5-min TTL participant count cache; type 1 (DM) always False, type 2/3 checks count via `get_participants()` API
- `poll_talk_conversations()` gates on @mention in multi-user rooms, passes `is_group_chat=True` to task creation
- `ConversationMessage` now includes `user_id` field; context formatter and triage use it as speaker label
- `build_prompt()` adds group chat note when `task.is_group_chat` is set
- Falls back to DM behavior (respond to everything) on participants API failure

**Files modified:**
- `src/istota/talk.py` - Added `get_participants()` method to `TalkClient`
- `src/istota/talk_poller.py` - Added `is_bot_mentioned()`, `_is_multi_user_room()`, mention handling in `clean_message_content()`, group room logic in `poll_talk_conversations()`
- `src/istota/db.py` - Added `user_id` to `ConversationMessage`, updated `get_conversation_history()` and `get_previous_task()` queries
- `src/istota/context.py` - Multi-user attribution in `format_context_for_prompt()` and `_format_triage_msg()`
- `src/istota/executor.py` - `user_id` passthrough in `_ensure_reply_parent_in_history()`, group chat note in `build_prompt()`
- `AGENTS.md` - Documented multi-user room behavior in Talk Integration section
- `tests/test_talk.py` - Added `TestGetParticipants`
- `tests/test_talk_poller.py` - Added `TestIsBotMentioned` (7), `TestCleanMessageContentMentions` (4), `TestIsMultiUserRoom` (5), `TestPollTalkConversationsGroupRoom` (4)
- `tests/test_context.py` - Added multi-user attribution tests (4)

## 2026-02-18: Fix TOML quoting in CRON.md generation

The `generate_cron_md()` function wrapped all values in basic TOML double quotes without escaping inner `"` characters. When `remove_job_from_cron_md()` rewrote the file after a once-job fired, a command containing `--subject "Operation's Tent"` produced invalid TOML that broke parsing for all 23 jobs. Fixed by using triple-quoted TOML strings (`"""..."""`) whenever a value contains double quotes or newlines.

**Key changes:**
- New `_toml_string()` helper in cron_loader.py — uses triple quotes when value contains `"` or `\n`
- Both `command` and `prompt` fields now route through `_toml_string()` for safe quoting
- 2 new round-trip tests for commands and prompts with inner double quotes

**Files modified:**
- `src/istota/cron_loader.py` - Added `_toml_string()`, updated `generate_cron_md()` to use it
- `tests/test_cron_loader.py` - Added quote round-trip tests

## 2026-02-17: Once-fire cron jobs + Claude session log cleanup

One-time scheduled jobs (`once = true`) are now automatically removed from both DB and CRON.md after successful execution. This replaces the previous approach where the reminders skill doc told Claude to manually clean up spent entries (unreliable). Also added periodic cleanup of Claude's JSONL session logs which grow unbounded in the bwrap sandbox.

**Key changes:**
- New `once` field on CronJob dataclass, parsed from TOML, synced to DB, round-trips through generate/migrate
- New `remove_job_from_cron_md()` function — loads, filters, rewrites cleanly via `generate_cron_md()`
- New `get_scheduled_job()` and `delete_scheduled_job()` DB functions
- Scheduler auto-removes once jobs after success (DB delete + CRON.md removal), keeps on failure for retry
- New `cleanup_old_claude_logs()` — deletes old `.jsonl`/`.txt`/`.json` from `~/.claude/{projects,debug,todos}`
- Hooked into `run_cleanup_checks()` using same `temp_file_retention_days` retention
- Reminders skill.md updated: `once = true` in template, simplified cleanup section
- `schema.sql` and DB migration for `once INTEGER DEFAULT 0` on `scheduled_jobs`
- 23 new tests: once field parsing/sync/removal (13 in test_cron_loader), once-job auto-removal + JSONL cleanup (10 in test_scheduler)

**Files added/modified:**
- `src/istota/cron_loader.py` - `once` field on CronJob, `remove_job_from_cron_md()`
- `src/istota/db.py` - `once` on ScheduledJob, migration, `get_scheduled_job()`, `delete_scheduled_job()`
- `src/istota/scheduler.py` - Once-job auto-removal on success, `cleanup_old_claude_logs()`
- `src/istota/skills/reminders/skill.md` - `once = true` in template, auto-cleanup docs
- `schema.sql` - `once INTEGER DEFAULT 0` column on `scheduled_jobs`
- `AGENTS.md` - Documented one-time jobs in Scheduled Jobs section
- `tests/test_cron_loader.py` - TestOnceField (7 tests), TestRemoveJobFromCronMd (5 tests)
- `tests/test_scheduler.py` - TestOnceJobAutoRemoval (4 tests), TestCleanupOldClaudeLogs (6 tests)

## 2026-02-17: Pre-transcribe audio for skill selection + companion skills + whisper max model

Voice memos arriving as `[audio.mp3]` had no meaningful text for skill selection — keyword-based skills like reminders, schedules, and calendar never loaded. Fixed by pre-transcribing audio attachments before skill selection so the enriched prompt contains the actual spoken words. Also added `companion_skills` as a generic skill.toml feature and capped whisper auto-selection with a configurable max model.

**Key changes:**
- New `_pre_transcribe_attachments()` in executor.py runs before `select_skills()`, transcribes audio and enriches `task.prompt`
- Enriched prompt flows through to `build_prompt()` and context selection so Claude sees the transcription
- Graceful fallback if faster-whisper not installed or transcription fails
- New `companion_skills` field on SkillMeta — when a skill is selected, listed companions are pulled in (respects admin_only + dependency checks)
- Whisper model auto-selection capped by `WHISPER_MAX_MODEL` env var (default: "small") to prevent OOM on servers with lots of RAM
- Configurable RAM headroom via `RAM_HEADROOM_MB` env var (default: 0.3 GB)
- Ansible: new `istota_whisper_max_model` variable, passed as env var in scheduler service
- 10 new pre-transcription tests, companion skill tests, updated whisper model tests

**Files added/modified:**
- `src/istota/executor.py` - Added `_pre_transcribe_attachments()`, `_AUDIO_EXTENSIONS`, integrated before skill selection
- `src/istota/skills/_types.py` - Added `companion_skills` field to SkillMeta
- `src/istota/skills/_loader.py` - Companion skill resolution in `select_skills()`, load from skill.toml
- `src/istota/skills/whisper/models.py` - Max model cap, configurable headroom, env var overrides
- `src/istota/skills/whisper/skill.toml` - Removed `companion_skills` (pre-transcription handles it)
- `deploy/ansible/defaults/main.yml` - Added `istota_whisper_max_model`
- `deploy/ansible/templates/istota-scheduler.service.j2` - Pass `WHISPER_MAX_MODEL` env var
- `tests/test_executor.py` - 10 new TestPreTranscribeAttachments tests
- `tests/test_skills_loader.py` - Companion skill tests
- `tests/test_skills_whisper.py` - Updated model selection tests for max model + headroom
- `AGENTS.md` - Documented companion_skills and pre-transcription in skill selection

## 2026-02-17: Reminders skill (doc-only)

Added a doc-only `reminders` skill that teaches the bot how to set time-based reminders by writing one-shot entries to CRON.md. Previously the bot would sometimes hallucinate reminders — telling the user it set one without actually writing anything. The skill doc gives explicit step-by-step instructions: parse the time, compute the cron expression, write the CRON.md entry, and confirm. Reminders use `@{user_id}` mentions so Nextcloud Talk triggers a notification.

**Key changes:**
- New `reminders` skill with `skill.toml` (keyword triggers) and `skill.md` (instructions)
- Keywords cover natural phrases: "remind me", "don't forget", "alert me", "in an hour", "at 3pm", etc.
- Prompt template uses `@{user_id}` mention for Talk notification alerts
- Covers one-shot reminders, recurring reminders, cleanup of spent entries, listing, and cancellation
- Critical rule: never claim a reminder was set without writing to CRON.md
- 7 new tests for keyword matching in skills loader

**Files added/modified:**
- `src/istota/skills/reminders/skill.toml` - New skill metadata with keyword triggers
- `src/istota/skills/reminders/skill.md` - Reminder instructions for the bot
- `tests/test_skills_loader.py` - Added TestRemindersSkillSelection (7 tests)

## 2026-02-17: README security model + origin story

Added a new "Security model" section to the README explaining the three-layer isolation: dedicated VM separation from Nextcloud, bubblewrap sandboxing per Claude Code invocation, and per-user sandbox isolation in multi-user setups. Also rewrote the "Why Istota?" origin story to explain how it started as a mobile Claude Code wrapper for development on the go and evolved into a full assistant.

**Key changes:**
- New "Security model" section: dedicated VM isolation, bubblewrap sandbox (same as Claude Code on Linux), per-user filesystem isolation, credential stripping, deferred DB writes
- Rewrote origin story: started as thin Claude Code wrapper for mobile dev without SSH, grew into full assistant
- Updated deployment section to emphasize dedicated VM requirement

**Files modified:**
- `README.md` - Added security model section, rewrote origin story, updated deployment wording

## 2026-02-17: Markets skill interactive CLI

The markets skill was previously briefing-only — it had no keywords and no CLI, so users couldn't ask "what happened in the markets today" in interactive chat. Added keyword triggers and a full CLI with three subcommands.

**Key changes:**
- Added keywords to `skill.toml` so the skill loads for interactive market questions (market, stock, futures, nasdaq, etc.)
- Added CLI with `quote`, `summary`, and `finviz` subcommands (all JSON output)
- `quote AAPL MSFT` fetches quotes for specific symbols via yfinance
- `summary` fetches broad market snapshot (S&P 500, Nasdaq, Dow, VIX, Gold, Oil, 10Y Treasury)
- `finviz` fetches FinViz homepage data via browser API
- Added `__main__.py` for `python -m istota.skills.markets` support
- Updated `skill.md` with interactive usage documentation
- 10 new tests covering parser, all three commands, and error handling

**Files added/modified:**
- `src/istota/skills/markets/skill.toml` - Added keywords and updated description
- `src/istota/skills/markets/__init__.py` - Added CLI (build_parser, main, cmd_quote, cmd_summary, cmd_finviz)
- `src/istota/skills/markets/__main__.py` - New, for `python -m` support
- `src/istota/skills/markets/skill.md` - Added interactive use section
- `tests/test_markets.py` - Added 10 CLI tests (27 total)

## 2026-02-17: Feed management skill + README updates

Added `feeds_config` doc-only skill so the bot knows how to create and edit a user's `FEEDS.md` file when asked to add/remove RSS, Tumblr, or Are.na feeds via Talk. Also updated the README with previously undocumented features (Karakeep bookmarks, feed reader).

**Key changes:**
- New `feeds_config` skill: documents FEEDS.md file format, location, all three feed types, entry fields, and common operations
- Keywords: feed, feeds, rss, tumblr, are.na, arena, subscribe, add feed, remove feed
- README: added feed reader and Karakeep bookmarks to features list

**Files added/modified:**
- `src/istota/skills/feeds_config/skill.toml` - Skill manifest with keywords
- `src/istota/skills/feeds_config/skill.md` - FEEDS.md format reference doc
- `README.md` - Added feed reader and Karakeep bookmarks to features

## 2026-02-17: Skills plugin architecture

Restructured the entire skills system from flat files in `config/skills/` with a central `_index.toml` into self-contained directory packages under `src/istota/skills/`. Each skill is now a directory with a `skill.toml` manifest and `skill.md` doc, optionally containing Python modules. This eliminates the need to edit 3-6 scattered files when adding a skill.

**Key changes:**
- New infrastructure: `_types.py` (SkillMeta, EnvSpec dataclasses), `_loader.py` (directory-based discovery with layered priority), `_env.py` (declarative env var resolver + setup_env() hook dispatch)
- `skills_loader.py` is now a thin re-export wrapper delegating to `skills/_loader.py`
- Skill manifests (`skill.toml`) declare metadata, keywords, resource/source types, dependencies, and env var wiring via `[[env]]` sections
- Discovery layers: legacy `_index.toml` < bundled `skill.toml` dirs < operator override dirs in `config/skills/`
- `ResourceConfig.extra: dict` captures arbitrary TOML keys from `[[resources]]` entries
- `Config.bundled_skills_dir` override for test isolation
- All 22 skills migrated to directory format
- Shared libraries moved into their skill packages: `finviz.py` → `markets/finviz.py`, `invoicing.py` → `accounting/invoicing.py`
- Dashed skill names normalized to underscores: `sensitive-actions` → `sensitive_actions`, `memory-search` → `memory_search`, `briefings-config` → `briefings_config`
- `config/skills/` is now an empty operator override directory
- Executor wired to resolve declarative env vars and dispatch `setup_env()` hooks

**Files added:**
- `src/istota/skills/_types.py` - SkillMeta and EnvSpec dataclasses
- `src/istota/skills/_loader.py` - Skill discovery, manifest loading, doc resolution
- `src/istota/skills/_env.py` - Declarative env var resolver + hook dispatch
- `src/istota/skills/*/skill.toml` - Manifest for each of 22 skills
- `src/istota/skills/*/skill.md` - Docs moved from `config/skills/*.md`
- `tests/test_skill_env.py` - 20 tests for env resolution

**Files modified:**
- `src/istota/skills_loader.py` - Now thin wrapper re-exporting from `skills/_loader.py`
- `src/istota/executor.py` - Wired declarative env resolution and setup_env hooks
- `src/istota/config.py` - Added `ResourceConfig.extra`, `Config.bundled_skills_dir`
- `src/istota/briefing.py` - Updated finviz import path
- `src/istota/invoice_scheduler.py` - Updated invoicing import path
- `tests/test_skills_loader.py` - Rewritten for directory-based discovery
- `tests/test_executor.py` - Updated for bundled_skills_dir isolation

**Files removed:**
- `config/skills/_index.toml` - Replaced by per-skill `skill.toml` manifests
- `config/skills/*.md` - Moved to `src/istota/skills/*/skill.md`
- `src/istota/skills/invoicing.py` - Moved to `accounting/invoicing.py`
- `src/istota/skills/finviz.py` - Moved to `markets/finviz.py`

## 2026-02-17: Fix Ansible namespace handling

Fixed several hardcoded `istota-` references in the Ansible role that broke deployments using a custom namespace (e.g., `zorg`). The scheduler service file was being written to `istota-scheduler.service` instead of `{{ istota_namespace }}-scheduler.service`, so the actual service never got updated. Same issue with the enable/start task and the deployment info message.

**Key changes:**
- Service file dest: `istota-scheduler.service` → `{{ istota_namespace }}-scheduler.service`
- Service enable task: hardcoded name → `{{ istota_namespace }}-scheduler`
- Deployment info message: hardcoded names → namespace-aware

**Files modified:**
- `deploy/ansible/tasks/main.yml` - Fixed service dest, enable task, and display message to use `istota_namespace`

## 2026-02-17: Architecture doc and Ansible repo dir cleanup

Added comprehensive ARCHITECTURE.md covering the full system architecture. Renamed the Ansible git clone destination from `{{ istota_home }}/src` to `{{ istota_repo_dir }}` (defaults to `{{ istota_home }}/istota`) to avoid the confusing `src/src/istota` path nesting on the server.

**Key changes:**
- ARCHITECTURE.md: core data flow, module map, scheduler internals, executor/prompt assembly, context selection, skills system, four-layer memory model, multi-user isolation, sandbox, Nextcloud integration, database schema, briefings, cron jobs, heartbeat, deployment, testing, design decisions
- Ansible role: added `istota_repo_dir` variable, replaced all hardcoded `{{ istota_home }}/src` references across defaults, tasks, and templates

**Files added/modified:**
- `ARCHITECTURE.md` - New comprehensive architecture document
- `deploy/ansible/defaults/main.yml` - Added `istota_repo_dir` variable
- `deploy/ansible/tasks/main.yml` - Updated all `{{ istota_home }}/src` → `{{ istota_repo_dir }}`
- `deploy/ansible/templates/config.toml.j2` - Updated skills_dir path
- `deploy/ansible/templates/istota-scheduler.service.j2` - Updated WorkingDirectory and ExecStart paths
- `deploy/ansible/templates/docker-compose.browser.yml.j2` - Updated build context path

## 2026-02-17: GitHub PR support for developer skill

Added GitHub pull request workflows alongside existing GitLab merge request support. Same security model: token via env var, credential helper per host, API wrapper with endpoint allowlist. Both platforms can be configured simultaneously with dynamic `GIT_CONFIG_COUNT`. GitHub Enterprise detection uses `{url}/api/v3` instead of `api.github.com`. Also documented that `deploy/ansible/` is the canonical location for the Ansible role (ansible-server symlinks here).

**Key changes:**
- `DeveloperConfig`: added `github_url`, `github_token`, `github_username`, `github_default_owner`, `github_reviewer`, `github_api_allowlist` fields
- `executor.py`: GitHub credential helper (`x-access-token` default), API wrapper with `Authorization: Bearer` header, dynamic git config indexing for multi-platform support
- `ISTOTA_GITHUB_TOKEN` env var override for systemd `EnvironmentFile=` usage
- Developer skill doc expanded with GitHub PR creation, listing, merging, and API quick reference
- Ansible role updated (defaults, config template, secrets.env)
- 7 new executor tests, 3 new config tests (TDD)

**Files added/modified:**
- `src/istota/config.py` - Added github_* fields to DeveloperConfig, env var override
- `src/istota/executor.py` - GitHub env vars, credential helper, API wrapper, dynamic GIT_CONFIG_COUNT
- `config/skills/developer.md` - Renamed to "Git, GitLab & GitHub Workflows", added GitHub sections
- `config/skills/_index.toml` - Added "github" keyword, updated description
- `config/config.example.toml` - Documented github_* config fields
- `deploy/ansible/defaults/main.yml` - Added istota_developer_github_* variables
- `deploy/ansible/templates/config.toml.j2` - Added GitHub config rendering
- `deploy/ansible/templates/secrets.env.j2` - Added ISTOTA_GITHUB_TOKEN
- `tests/test_config.py` - GitHub config defaults, TOML parsing, env var override tests
- `tests/test_executor.py` - TestGitHubEnvVars class (7 tests)
- `.claude/rules/config.md` - Updated DeveloperConfig reference
- `.claude/rules/executor.md` - Added GitHub env var table entries
- `AGENTS.md` - Noted deploy/ansible/ is canonical (ansible-server symlinks here)

## 2026-02-17: OSS deployment infrastructure

Ported the private Ansible role into the repo at `deploy/ansible/` and created a standalone `install.sh` script with interactive setup wizard. External Ansible role dependencies (Docker, rclone, rclone-mount, nginx, Node.js) inlined as direct tasks. Added `render_config.py` (stdlib-only) that generates all config files from a single settings TOML file. Deleted the old `scripts/deploy/` placeholder scripts.

**Key changes:**
- Ported Ansible role: defaults, handlers, tasks (with inlined deps), 14 templates
- `istota-site.conf.j2`: replaced private nginx includes with inline private-network ACL
- `deploy/render_config.py`: generates config.toml, user configs, admins, secrets.env, systemd service, logrotate from settings file
- `deploy/install.sh`: full deployment script with `--interactive` wizard, `--update` mode, env var overrides
- `install.sh` repo-root wrapper
- Deleted `scripts/deploy/` (setup-server.sh, install-services.sh, obsolete service files)

**Files added/modified:**
- `deploy/ansible/defaults/main.yml` - Ported from private role (org-specific defaults replaced)
- `deploy/ansible/handlers/main.yml` - Direct copy
- `deploy/ansible/tasks/main.yml` - Ported with inlined role dependencies
- `deploy/ansible/templates/` - 14 Jinja2 templates (config, services, backup, nginx, fava, etc.)
- `deploy/ansible/README.md` - Ansible role usage docs
- `deploy/render_config.py` - Python config generator for install.sh path
- `deploy/install.sh` - Main deployment script
- `deploy/README.md` - Top-level deployment docs (both paths)
- `install.sh` - Repo-root thin wrapper
- `README.md` - Updated deployment section
- `AGENTS.md` - Updated project structure to show deploy/
- `scripts/deploy/` - Deleted (4 files)

## 2026-02-17: Per-user persona override, persona rewrite, license

Added per-user persona override so each user's workspace `PERSONA.md` takes precedence over the global `config/persona.md`. Rewrote persona with clearer structure (Character, Communication, How you work, Boundaries) and leaned into the Culture drone identity. Moved source directory restriction from persona to system prompt. Added AGPL-3.0-or-later license.

**Key changes:**
- Per-user persona: user workspace `PERSONA.md` overrides global `config/persona.md`
- `load_persona()` checks user workspace first (via mount), falls back to global
- `ensure_user_directories_v2()` seeds `PERSONA.md` by copying global persona on first run
- Added `get_user_persona_path()` to storage
- Persona rewritten: restructured into Character / Communication / How you work / Boundaries
- Source directory restriction moved from persona to `build_prompt()` rules section
- License: AGPL-3.0-or-later (pyproject.toml, README.md, LICENSE file)
- Removed HARDENING.md (content already captured in AGENTS.md)
- README.md rewritten: friendlier tone, emoji feature list, simplified structure, dropped exhaustive CLI/config reference

**Files added/modified:**
- `src/istota/storage.py` - Added `get_user_persona_path()`, PERSONA.md seeding, updated workspace README
- `src/istota/executor.py` - `load_persona()` accepts `user_id`, user override logic, source dir rule in `build_prompt()`
- `config/persona.md` - Complete rewrite with Culture drone character
- `tests/test_executor.py` - 8 new `TestLoadPersona` tests
- `tests/test_storage.py` - 4 new persona path/seeding tests
- `AGENTS.md` - Updated persona references
- `.claude/rules/executor.md` - Updated persona docs
- `pyproject.toml` - Added AGPL-3.0-or-later license
- `README.md` - Complete rewrite (friendly intro, emoji features, simplified quick start)
- `LICENSE` - New file
- `HARDENING.md` - Deleted

## 2026-02-16: Remove legacy storage defaults, harden bot_dir_name

Removed all hardcoded `DEFAULT_BOT_DIR` fallbacks and backward-compat aliases from storage, making `bot_dir` a required parameter throughout. Fixed `bot_dir_name` regex to ASCII-only for locale-independent filesystem paths, and fixed `skills_loader.py` to use properly sanitized `bot_dir` instead of naive `bot_name.lower()`.

**Key changes:**
- Removed `DEFAULT_BOT_DIR = "istota"` constant and `get_user_zorg_path`/`get_user_workspace_path` aliases
- Made `bot_dir` a required parameter on all storage path functions (no silent defaults)
- Fixed v2 rclone fallback paths to pass `config.bot_dir_name` correctly
- Migrated `email_poller.py` from rclone-only functions to v2 (config-aware)
- Fixed `skills_loader.py` `load_skills()` to accept `bot_dir` param instead of `bot_name.lower()`
- Changed `bot_dir_name` regex from `[^\w\-]` to `[^a-z0-9_\-]` (ASCII-only, no unicode surprises)
- Fixed schema.sql comments ("Zorg"/"ZORG.md" → "Istota"/"TASKS.md")
- Cleaned unused non-v2 imports from `cli.py`

**Files modified:**
- `src/istota/storage.py` - Removed legacy defaults/aliases, required `bot_dir` param everywhere
- `src/istota/config.py` - ASCII-only regex in `bot_dir_name`
- `src/istota/skills_loader.py` - Added `bot_dir` parameter to `load_skills()`
- `src/istota/executor.py` - Passes `config.bot_dir_name` to `load_skills()` and path functions
- `src/istota/email_poller.py` - Migrated to v2 storage functions
- `src/istota/cli.py` - Removed unused non-v2 imports
- `src/istota/cron_loader.py`, `briefing_loader.py`, `heartbeat.py`, `invoice_scheduler.py`, `tasks_file_poller.py`, `feed_poller.py` - Pass `config.bot_dir_name` to path functions
- `schema.sql` - Fixed comments
- `tests/test_config.py` - Added unicode and hyphen tests for `bot_dir_name`
- `tests/test_storage.py`, `test_cron_loader.py`, `test_scheduler.py`, `test_email_poller.py` - Updated for new signatures

## 2026-02-16: Fork from Zorg → Istota

Forked the zorg codebase to create istota as a standalone, open-sourceable project. All technical identifiers use "istota" while the user-facing bot name is configurable via `bot_name` in config.toml (default: "Istota").

**Key changes:**
- Renamed package `zorg` → `istota` (src/, pyproject.toml, CLI entry points)
- All env vars `ZORG_*` → `ISTOTA_*`
- DB tables `zorg_file_tasks`/`zorg_kv` → `istota_file_tasks`/`istota_kv`
- Added `bot_name` config field with `bot_dir_name` property (lowercase, spaces→underscores, special chars stripped)
- Nextcloud user folder paths use `config.bot_dir_name` (e.g. `/Users/{uid}/istota/`)
- Persona, guidelines, and skill docs use `{BOT_NAME}`/`{BOT_DIR}` placeholders substituted at load time
- Notification subjects, email signatures, CLI output all use `config.bot_name`
- Scrubbed all deployment-specific references (hostnames, personal usernames, company names)
- Removed `.claude/settings.local.json` and `uv.lock` for clean start
- Fresh git repo with no history

**Files added/modified:**
- `src/istota/config.py` - Added `bot_name` field, `bot_dir_name` property with sanitization
- `src/istota/storage.py` - Renamed `get_user_zorg_path` → `get_user_bot_path`, accepts `bot_dir` param
- `src/istota/executor.py` - Added `_apply_bot_name()` helper for template substitution
- `src/istota/skills_loader.py` - `{BOT_NAME}`/`{BOT_DIR}` substitution in loaded skill docs
- `config/persona.md` - Uses `{BOT_NAME}` placeholder
- `config/guidelines/email.md` - Uses `{BOT_NAME}` for signature
- `config/skills/*.md` - Uses `{BOT_DIR}` for folder paths
- All test files updated to match new identifiers

## 2026-02-13: RSS Feed Inline Images & Text Formatting

Improved rendering of text-heavy RSS feeds (like feuilleton blogs) that embed images inside post content rather than in enclosures or media tags. Also improved text readability in feed cards.

**Key changes:**
- Extract card image from first `<img>` in content HTML when no enclosure/media_content image exists
- Added `img` to sanitizer allowed tags with `src`/`alt` attribute whitelisting and `loading="lazy"`
- Brighter excerpt text color (`#999` → `#bbb`), increased paragraph spacing
- CSS for inline excerpt images (responsive, rounded corners, block display)
- Bold/italic emphasis styling for better contrast in dark theme

**Files modified:**
- `src/zorg/feed_poller.py` - Inline image extraction, sanitizer img support, CSS improvements

## 2026-02-13: Track Actions Taken per Task

Store tool use descriptions from Claude Code streaming execution and surface them in conversation context. This lets zorg see what tools it used previously (e.g., "Reading CRON.md", "Editing CRON.md") so it can skip redundant searches and go straight to relevant files on follow-up requests.

**Key changes:**
- Added `actions_taken TEXT` column to tasks table (schema + migration)
- `_execute_streaming_once()` now accumulates `ToolUseEvent.description` into a JSON array
- All executor functions return 3-tuple `(success, result, actions_taken)` (was 2-tuple)
- `update_task_status()` stores `actions_taken` on completion
- `ConversationMessage` carries `actions_taken` for context formatting
- `format_context_for_prompt()` appends compact `[Actions: ...]` line after bot responses
- Actions included in triage text for the selection model
- Capped at 15 actions per message in context display, pipe-separated

**Files modified:**
- `schema.sql` - Added `actions_taken` column
- `src/zorg/db.py` - Task dataclass, ConversationMessage, update_task_status, get_conversation_history, migration
- `src/zorg/executor.py` - 3-tuple returns from all execution paths, action accumulation in streaming
- `src/zorg/context.py` - `_format_actions_line()`, actions in format and triage
- `src/zorg/scheduler.py` - Unpacks 3-tuple, passes actions to DB
- `tests/test_db.py` - 2 new tests for actions_taken storage/retrieval
- `tests/test_context.py` - 3 new tests for actions formatting
- `tests/test_executor.py` - 2 new tests for actions pass-through, updated all return unpacking
- `tests/test_executor_streaming.py` - Updated all return unpacking to 3-tuple
- `tests/test_scheduler.py` - 2 new tests for actions storage, updated all mock return values

## 2026-02-13: Fix Tumblr Feed Pagination & Mobile UI

Tumblr posts were silently being missed because `fetch_tumblr` used a `since_id` parameter that the Tumblr `/blog/{blog}/posts` API endpoint doesn't support — the API silently ignored it and always returned the latest 20 posts. High-volume blogs posting 20+ items between 3-hour poll intervals were losing posts every cycle.

**Key changes:**
- Replaced `since_id` with offset-based pagination in `fetch_tumblr()`
- Added pagination loop: fetches successive pages of 20 until catching up with known items or hitting 5-page cap (100 posts max per cycle)
- Added `feed_item_exists()` DB function for duplicate checking during pagination
- Added Tumblr rate-limit header logging (`X-Ratelimit-Perday-Remaining`, `X-Ratelimit-Perhour-Remaining`) at INFO level
- Fixed `latest_id` tracking to store newest (first) item instead of oldest (last)
- Responsive mobile CSS for filter pills: smaller font/padding at ≤640px to prevent line wrapping
- Are.na images now use `original` URL (CloudFront JPEG/PNG) instead of `display` URL (base64-encoded webp transform)

**Files modified:**
- `src/zorg/feed_poller.py` - Tumblr pagination, rate-limit logging, Are.na original images, mobile CSS
- `src/zorg/db.py` - Added `feed_item_exists()` function
- `tests/test_feed_poller.py` - Added 4 pagination tests, updated Are.na image test

## 2026-02-12: Deferred DB Operations for Sandbox-Safe Writes

With bubblewrap sandbox enabled and DB mounted read-only, Claude and skill CLIs couldn't write to the DB directly. Implemented a deferred operations pattern where JSON request files are written to the always-RW user temp dir and processed by the scheduler after successful task completion.

**Key changes:**
- `_process_deferred_subtasks()` and `_process_deferred_tracking()` in scheduler.py
- `_write_deferred_tracking()` helper in accounting skill with fallback to direct DB
- `ZORG_DEFERRED_DIR` env var always set to user temp dir
- Removed sqlite3 tool from prompt; subtask creation via JSON file
- Admin-only enforcement on deferred subtask creation
- Deferred files only processed on success (not failure, not confirmation)

**Files modified:**
- `src/zorg/scheduler.py` - Added deferred processing functions + integration in process_one_task()
- `src/zorg/executor.py` - Added ZORG_DEFERRED_DIR env var, removed sqlite3 tool, updated subtask rule
- `src/zorg/skills/accounting.py` - Added _write_deferred_tracking(), updated import/sync commands
- `config/skills/tasks.md` - Rewritten for JSON file approach
- `tests/test_scheduler.py` - Added TestDeferredOperations (10 tests)
- `tests/test_executor.py` - Added TestDeferredDirEnvVar (2 tests), updated sqlite3 tool tests
- `tests/test_skills_accounting.py` - Added TestDeferredTracking (3 tests)

## 2026-02-12: Key-Value Store for Script State

Added a scoped KV store backed by a dedicated `zorg_kv` table, giving scripts persistent structured storage through CLI commands without direct DB access. User-isolated and namespace-scoped.

**Key changes:**
- New `zorg_kv` table with composite PK `(user_id, namespace, key)`, JSON-encoded values
- 5 DB functions: `kv_get`, `kv_set`, `kv_delete`, `kv_list`, `kv_namespaces`
- CLI: `zorg kv {get|set|list|delete|namespaces}` with JSON output and input validation
- All operations scoped by user and namespace for isolation

**Files modified:**
- `schema.sql` - Added `zorg_kv` table and index
- `src/zorg/db.py` - Added KV store functions
- `src/zorg/cli.py` - Added `kv` subcommand group with 5 subcommands

**Files added:**
- `tests/test_kv.py` - 32 tests (21 DB + 11 CLI)

## 2026-02-12: Whisper Audio Transcription Skill

Added local CPU-based audio transcription using faster-whisper. First package-style skill (multi-file) with model selection, RAM guard, and subtitle output formats.

**Key changes:**
- New `src/zorg/skills/whisper/` package with CLI entry point, model management, and transcription logic
- RAM guard via psutil: auto-selects the largest model that fits in available memory, or validates user's choice
- Output formats: JSON (with word-level timestamps), plain text, SRT, VTT subtitles
- `--save` flag writes output to file alongside the audio file
- Model management: `models` command lists available/downloaded status, `download` pre-fetches models
- Sandbox support: huggingface cache mounted RO in bwrap so pre-downloaded models are accessible
- Optional dependency group (`whisper`) — `faster-whisper>=1.1.0` and `psutil>=5.9.0`

**Files added:**
- `src/zorg/skills/whisper/__init__.py` — Package init
- `src/zorg/skills/whisper/__main__.py` — CLI entry point
- `src/zorg/skills/whisper/cli.py` — Argparse CLI (transcribe, models, download)
- `src/zorg/skills/whisper/models.py` — Model selection with RAM guard
- `src/zorg/skills/whisper/transcribe.py` — Core transcription, SRT/VTT formatting
- `config/skills/whisper.md` — Skill documentation for Claude
- `tests/test_skills_whisper.py` — 37 tests

**Files modified:**
- `config/skills/_index.toml` — Added whisper skill with audio/voice keywords
- `pyproject.toml` — Added `whisper` optional dependency group
- `src/zorg/executor.py` — Added `~/.cache/huggingface/` RO bind mount in bwrap sandbox

## 2026-02-11: Self-Check Heartbeat + Per-Check Intervals

Added a new heartbeat check type `self-check` that runs the same diagnostics as the `!check` command but deterministically through the heartbeat system, alerting only on failure. Also added `interval_minutes` to control per-check frequency, since expensive checks like `self-check` shouldn't run every 60-second cycle.

**Key changes:**
- Added `_check_self()` handler mirroring `!check` diagnostics: Claude binary, bwrap (if sandbox enabled), DB health, recent task failure rate, and optional Claude CLI execution test
- Execution test configurable via `execution_test` config field (default: true) — invokes Claude with echo command, with sandbox wrapping if enabled
- Added `interval_minutes` field to `HeartbeatCheck` — checks with this set are skipped when `last_check_at` is too recent, using existing heartbeat state. Checks without it run every cycle as before
- Parsed from HEARTBEAT.md and excluded from the type-specific `config` dict

**Files modified:**
- `src/zorg/heartbeat.py` — Added `_check_self()` handler, `interval_minutes` field and skip logic in `check_heartbeats()`
- `tests/test_heartbeat.py` — Added `TestCheckSelf` (11 tests) and interval tests (4 tests: config parsing, skip recent, run after elapsed, no-interval always runs). Suite at 60 tests
- `config/skills/heartbeat.md` — Documented self-check type, interval_minutes, and config examples
- `AGENTS.md` — Added self-check to check types list and documented check interval feature

## 2026-02-11: Auto-Restart Fava After Ledger Changes

Fava running via systemd didn't pick up beancount ledger changes automatically because inotify doesn't work over the rclone mount (VFS caching). Added a mechanism to restart the user's Fava service after any ledger write.

**Key changes:**
- Added `_restart_fava()` helper that calls `sudo systemctl restart zorg-fava-{user_id}.service` after ledger modifications
- Called from `_append_to_ledger()` (covers monarch import/sync, invoice paid) and `cmd_add_transaction()` (direct yearly file writes)
- Fails silently if no sudo access or no Fava service exists (non-interactive sudo, timeout, capture output)
- Ansible: new sudoers rule (`/etc/sudoers.d/zorg-fava`) granting the zorg user passwordless restart access for Fava services
- Sudoers file validated with `visudo` and auto-removed when Fava is disabled

**Files added/modified:**
- `src/zorg/skills/accounting.py` — Added `_restart_fava()`, called after all ledger write paths
- `tests/test_skills_accounting.py` — Added `TestRestartFava` (4 tests: systemctl args, no-op without user, ignores failures, integration)
- `ansible-server/roles/zorg/templates/zorg-fava-sudoers.j2` — New sudoers template for Fava restart
- `ansible-server/roles/zorg/tasks/main.yml` — Deploy/remove sudoers file based on `zorg_fava_enabled`

## 2026-02-11: Fix Briefing Showing Reminders When Not Configured

Evening briefings were always showing the same Flaubert quote in the REMINDER section, even when `reminders` was not enabled in the briefing components. Root cause: the `reminders_file` resource path was still listed in the prompt, so Claude read the file on its own and picked the first quote every time (bypassing the shuffle-queue rotation).

**Key changes:**
- Excluded `reminders_file` resources from the prompt for briefing tasks — reminders are pre-fetched by the briefing builder when enabled, so the file path should never be exposed to Claude directly
- Updated briefing skill doc to explicitly state the REMINDER section should only appear when a pre-selected reminder is provided in the prompt

**Files modified:**
- `src/zorg/executor.py` — Gate `reminders_file` resource display on `task.source_type != "briefing"`
- `config/skills/briefing.md` — Clarify REMINDER section requires pre-selected reminder; never read files directly

## 2026-02-11: Production Sandbox Verification and Fixes

Deployed the bwrap sandbox to production and ran a full integration test (via zorg itself). Fixed several issues discovered during testing.

**Key changes:**
- Fixed venv PATH resolution: `sys.executable` follows symlinks to system python (`/usr/bin/python3.13`), giving `/usr/bin/` as the venv bin dir. Changed to `sys.prefix` which gives the actual venv root without following binary symlinks. All `python -m zorg.skills.*` commands now work inside the sandbox.
- Added static site directory (`config.site.base_path`) as RW bind mount for feed generation and HTML writes
- Added `HARDENING.md` documenting the full security posture against each finding in the security audit
- Updated the original security audit document with 3G (bwrap sandbox) implementation status

**Files added/modified:**
- `src/zorg/executor.py` — Fixed `build_clean_env()` venv PATH (sys.prefix instead of sys.executable), added site dir bind mount
- `tests/test_security.py` — Updated PATH assertions for sys.prefix
- `HARDENING.md` — New file: security hardening status against all audit findings
- `ansible-server/roles/zorg/defaults/main.yml` — Added `zorg_site_base_path`
- `ansible-server/roles/zorg/templates/config.toml.j2` — Added `base_path` to `[site]` section

## 2026-02-10: Fix Bubblewrap Sandbox on Debian 13

Fixed three issues preventing the bubblewrap sandbox from working on the production Debian 13 server.

**Bug 1: Unprivileged user namespaces disabled.** The dev-sec hardening role sets `kernel.unprivileged_userns_clone=0`, which blocks bwrap from creating namespaces. Added an Ansible sysctl task to re-enable it when sandbox is configured.

**Bug 2: Merged-usr symlink breakage.** Debian 13 uses merged-usr where `/bin`, `/lib`, `/lib64`, `/sbin` are symlinks to `/usr/*`. The old code used `_ro_bind()` which resolved these symlinks via `Path.resolve()`, but the symlinks themselves didn't exist inside the sandbox — causing the dynamic linker at `/lib64/ld-linux-x86-64.so.2` to be missing. Fixed by using bwrap's `--symlink` directive for paths that are symlinks on the host.

**Bug 3: Bind dest path resolution broke symlinked /etc files.** `_ro_bind()` and `_bind()` resolved both source AND dest paths. For `/etc/resolv.conf` (symlink to `/run/systemd/resolve/resolv.conf`), this meant the file appeared at `/run/...` inside the sandbox instead of `/etc/resolv.conf`. DNS resolution failed completely — causing `FailedToOpenSocket` API errors. Fixed by preserving the original path as the bind destination.

**Bug 4: OAuth credentials read-only.** `.credentials.json` was bound RO inside the sandbox. Claude Code uses OAuth (not API keys), and token refresh needs write access. Changed to RW bind.

**Key changes:**
- `_ro_bind()`/`_bind()` now preserve the original path as dest instead of resolving it (fixes symlinked /etc files)
- Merged-usr compat: `/bin`, `/lib`, `/lib64`, `/sbin` use `--symlink` when they're symlinks on host
- `.credentials.json` bound RW for OAuth token refresh
- Ansible sysctl task: `kernel.unprivileged_userns_clone=1` via `/etc/sysctl.d/99-zorg-sandbox.conf`

**Files modified:**
- `src/zorg/executor.py` — Fixed `_ro_bind`/`_bind` dest resolution, merged-usr symlinks, OAuth credentials RW
- `ansible-server/roles/zorg/tasks/main.yml` — Added sysctl task for unprivileged userns

## 2026-02-10: Move Sleep Cycle from Per-User to Global Config

Simplified configuration by making sleep cycle a global setting instead of per-user. The cron expression is already evaluated in each user's timezone, so "0 2 * * *" means 2am local time for everyone — no reason for it to differ. Also removed the `conversation_token` notification feature (sleep cycle should be an invisible background process, not something that messages users).

This mirrors how `channel_sleep_cycle` was already structured as a global config section.

**Key changes:**
- `SleepCycleConfig` moved from `UserConfig` to `Config` (global `[sleep_cycle]` TOML section)
- Removed `conversation_token` field from `SleepCycleConfig`
- Removed `_post_sleep_summary()` Talk notification function
- `check_sleep_cycles()` now reads from `config.sleep_cycle` and iterates all users when enabled
- `process_user_sleep_cycle()` reads sleep settings from `config.sleep_cycle` instead of a parameter

**Files modified:**
- `src/zorg/config.py` — Moved `SleepCycleConfig` to global `Config`, removed from `UserConfig`, added TOML loading
- `src/zorg/sleep_cycle.py` — Updated to use global config, removed notification code
- `config/config.example.toml` — Added global `[sleep_cycle]` section, removed per-user sleep_cycle
- `config/users/alice.example.toml` — Removed `[sleep_cycle]` section
- `tests/test_sleep_cycle.py` — Updated fixtures and assertions for global config
- `tests/test_config.py` — Rewrote sleep cycle config tests for global pattern
- `tests/test_executor_streaming.py` — Updated sleep cycle reference to global config
- `tests/conftest.py` — Removed `sleep_cycle` from user config fixture
- `ansible-server/roles/zorg/defaults/main.yml` — Added `zorg_sleep_cycle_*` vars, removed per-user example
- `ansible-server/roles/zorg/templates/config.toml.j2` — Added `[sleep_cycle]` section
- `ansible-server/roles/zorg/templates/user.toml.j2` — Removed `[sleep_cycle]` block

## 2026-02-10: Per-User Filesystem Sandbox via Bubblewrap

Implemented per-user filesystem isolation using bubblewrap (`bwrap`). When `sandbox_enabled = true` in the `[security]` config section, each Claude Code invocation runs inside a mount namespace that restricts what the agent can see on the filesystem. The scheduler process itself remains unsandboxed since it needs cross-user DB access for task dispatch.

Non-admin agents see only their own Nextcloud subtree, their temp dir, the active channel dir, and any explicitly configured resources. The DB file, other users' directories, `/etc/zorg/` secrets, and `config/users/*.toml` are all hidden. Admin agents get the full Nextcloud mount, DB access (RO by default, configurable), and developer repos.

The implementation uses selective `/etc` binds (DNS, TLS, user lookup, timezone, linker cache only), PID namespaces for process isolation, tmpfs masking for sensitive config directories, and `Path.resolve()` on all bind paths to prevent symlink escapes. Gracefully degrades on non-Linux or when bwrap is not installed.

**Key changes:**
- `SecurityConfig` gains `sandbox_enabled` (default false) and `sandbox_admin_db_write` (default false) fields
- `build_bwrap_cmd()` function in executor.py (~120 lines) constructs the bwrap wrapper command
- `execute_task()` calls `build_bwrap_cmd()` after building the Claude CLI command, before execution
- Ansible deploys `bubblewrap` package and sets `sandbox_enabled = true` by default

**Files added/modified:**
- `src/zorg/config.py` — Added `sandbox_enabled`, `sandbox_admin_db_write` to `SecurityConfig` + parsing
- `src/zorg/executor.py` — Added `build_bwrap_cmd()`, wired into `execute_task()`
- `config/config.example.toml` — Added sandbox config comments to `[security]` section
- `AGENTS.md` — Added "Per-User Filesystem Sandbox" documentation under Key Design Decisions
- `tests/test_sandbox.py` — 24 new tests (disabled cases, non-admin mounts, admin mounts, path resolution, config loading)
- `ansible-server/roles/zorg/defaults/main.yml` — Added `zorg_security_sandbox_enabled`, `zorg_security_sandbox_admin_db_write`
- `ansible-server/roles/zorg/templates/config.toml.j2` — Added sandbox fields to `[security]`
- `ansible-server/roles/zorg/tasks/main.yml` — Added `bubblewrap` to apt package list

## 2026-02-10: GitLab Token Removed from Temp Scripts

Moved GitLab token out of the git credential helper and API wrapper scripts that were written to the per-user temp directory. Previously, both scripts contained the token as a literal string, leaving credentials on disk after task execution. Now both scripts read from the `$GITLAB_TOKEN` environment variable, which is passed to the Claude Code subprocess and dies with the process.

**Key changes:**
- `git-credential-helper` script now uses `echo password=$GITLAB_TOKEN` instead of embedding the literal token
- `gitlab-api` wrapper script now uses `--header "PRIVATE-TOKEN: $GITLAB_TOKEN"` instead of embedding the literal token
- `GITLAB_TOKEN` env var added to the subprocess environment when developer skill is enabled
- No secrets are written to disk in any temp scripts

**Files modified:**
- `src/zorg/executor.py` — Scripts reference `$GITLAB_TOKEN` env var instead of literal value
- `tests/test_executor.py` — Updated assertions: token in env, not in script files

## 2026-02-10: Security Hardening — Clean Env, AllowedTools, Credential Stripping

Implemented the first four tiers of the security hardening plan: clean subprocess environment for Claude Code, `--allowedTools` flag instead of `--dangerously-skip-permissions` in restricted mode, credential stripping from heartbeat/cron subprocesses, and Ansible `EnvironmentFile=` support for secrets management.

**Key changes:**
- `SecurityConfig` dataclass with `mode` ("permissive"/"restricted") and `passthrough_env_vars`, gated behind `[security]` TOML section
- `build_clean_env(config)` — restricted mode gives Claude subprocess only PATH/HOME/PYTHONUNBUFFERED plus configured passthrough vars; permissive mode inherits full os.environ
- `build_stripped_env()` — always-on credential stripping for heartbeat shell commands and cron command tasks (strips vars matching PASSWORD/SECRET/TOKEN/API_KEY/NC_PASS/PRIVATE_KEY/APP_PASSWORD)
- `build_allowed_tools()` — permits Read/Write/Edit/Grep/Glob/Bash (all Bash commands allowed since clean env is the security boundary)
- Restricted mode uses `--allowedTools` flag; permissive mode retains `--dangerously-skip-permissions` for backward compat
- Environment variable overrides for 6 secrets: `ZORG_NC_APP_PASSWORD`, `ZORG_IMAP_PASSWORD`, `ZORG_SMTP_PASSWORD`, `ZORG_GITLAB_TOKEN`, `ZORG_NTFY_TOKEN`, `ZORG_NTFY_PASSWORD` — loaded after TOML, enables `EnvironmentFile=` in systemd
- Ansible: `secrets.env.j2` template deployed to `/etc/zorg/secrets.env` (root:zorg, 0640), `EnvironmentFile=` in scheduler service, passwords conditionally omitted from config.toml when `zorg_use_environment_file: true`
- Default Ansible deployment uses `zorg_security_mode: "restricted"` and `zorg_use_environment_file: true`

**Files added/modified:**
- `src/zorg/config.py` — Added `SecurityConfig`, env var overrides in `load_config()`
- `src/zorg/executor.py` — Added `build_clean_env()`, `build_stripped_env()`, `build_allowed_tools()`, refactored `execute_task()` command/env construction
- `src/zorg/heartbeat.py` — `_check_shell_command()` uses `build_stripped_env()`
- `src/zorg/scheduler.py` — `_execute_command_task()` uses `build_stripped_env()`
- `config/config.example.toml` — Added `[security]` section with documentation
- `tests/test_security.py` — 25 new tests covering all security functions and config overrides
- `ansible-server/roles/zorg/templates/secrets.env.j2` — New secrets environment file
- `ansible-server/roles/zorg/templates/zorg-scheduler.service.j2` — `EnvironmentFile=` support
- `ansible-server/roles/zorg/templates/config.toml.j2` — `[security]` section, conditional password omission
- `ansible-server/roles/zorg/defaults/main.yml` — `zorg_security_mode`, `zorg_use_environment_file`
- `ansible-server/roles/zorg/tasks/main.yml` — Deploy/remove secrets.env task

## 2026-02-10: Browser Container Robustness — Auto-Recovery, Session Limits, Docker Healthcheck

Improved the Dockerized browser container to handle Chrome crashes gracefully. Previously, if Chrome died inside the container, the Flask API would keep running but all browse requests would fail with no recovery path. Now the API detects a dead browser and restarts it automatically, and Docker provides a fallback restart if auto-recovery also fails.

**Key changes:**
- `_ensure_browser()` checks `browser.is_connected()` on every request via `@app.before_request`; if Chrome is dead, triggers `_restart_browser()` which tears down and re-initializes Playwright
- `_restart_browser()` clears all dead sessions, best-effort cleanup of old context/playwright, then re-initializes fresh
- Session/tab limit: `MAX_SESSIONS=3` (configurable via `MAX_BROWSER_SESSIONS` env var), enforced in `_create_session()` by evicting oldest session when at capacity
- Docker `HEALTHCHECK` hits `/health` and verifies `browser_connected`; after 3 failures Docker marks container unhealthy for restart via `restart: unless-stopped`
- Browse skill client handles HTTP 503 (browser restarting) with clear retry message
- Health endpoint now reports `max_sessions` in response
- Ansible role updated: `zorg_browser_max_sessions` variable, passed as `MAX_BROWSER_SESSIONS` in `browser.env`

**Files added/modified:**
- `docker/browser/browse_api.py` — Added `_ensure_browser()`, `_restart_browser()`, session limit logic, before_request browser check
- `docker/browser/Dockerfile` — Added `HEALTHCHECK` instruction
- `src/zorg/skills/browse.py` — Added HTTP 503 error handling for browser restart scenarios
- `ansible-server/roles/zorg/defaults/main.yml` — Added `zorg_browser_max_sessions: 3`
- `ansible-server/roles/zorg/tasks/main.yml` — Pass `MAX_BROWSER_SESSIONS` in browser.env

## 2026-02-09: Feed Page Improvements — Source Labels, Lightbox, Truncation Fix

Several improvements to the static feed page: replaced author display with feed source type labels, switched from per-image popover lightbox to a shared JS lightbox (cutting page size by ~33%), and moved the status notice div before the grid to survive HTML truncation from large pages served through nginx reverse proxy over FUSE mounts.

**Key changes:**
- Show feed source type (rss, tumblr, are.na) in card meta instead of author name
- `feed_types` dict passed from FEEDS.md config through to HTML builder
- Replaced per-image `[popover]` lightbox with single shared `<div>` + JS click handler
- Removed duplicate `<img>` tags for lightbox overlays — each image now just has `data-full` attribute
- Page size at 1000 items with realistic URLs: 2.1 MB → 1.4 MB (33% reduction)
- Moved status notice `<div>` before the feed grid (position:fixed, DOM order doesn't affect rendering but survives truncation)
- Vertically centered meta row items with `align-items:center`

**Files modified:**
- `src/zorg/feed_poller.py` — Source labels, JS lightbox, status notice placement, meta alignment
- `tests/test_feed_poller.py` — Updated lightbox and gallery assertions for new markup

## 2026-02-09: Feed Poller Logging Improvements

Added comprehensive logging to the feed poller to improve visibility into feed polling and troubleshoot API/RSS issues. Previously, successful polls with 0 new items produced zero log output, making it impossible to distinguish "feed ran and found nothing new" from "feed was silently skipped."

**Key changes:**
- Per-feed INFO logging on new items: `Feed user/name: N new item(s) (fetched M)`
- Per-feed error recovery logging: `Feed user/name: recovered after N consecutive errors`
- Improved error messages with consecutive error count
- DEBUG logging in all three fetchers (RSS, Tumblr, Are.na) with HTTP status codes and entry/post/block counts
- DEBUG logging for RSS 304 Not Modified responses
- DEBUG logging for interval skips and zero-item polls
- Poll cycle summary at DEBUG level even when 0 items found
- Page generation skip logged at DEBUG level
- Removed duplicate log from scheduler.py (feed_poller now handles all logging)

**Files modified:**
- `src/zorg/feed_poller.py` — Added ~15 log statements across fetchers, polling, and page generation
- `src/zorg/scheduler.py` — Removed duplicate feed poll log (L1522-1523)

## 2026-02-09: Feed Page Status Notice

Added a small fixed status notice to the bottom-right corner of the generated feed page showing when it was last built, how many new items were pulled, and total item count.

**Key changes:**
- `_build_status_text()` helper formats parts with `·` separators (timestamp, +N new, total items)
- `_build_feed_page_html()` accepts `generated_at` and `new_item_count` params, renders a fixed `.status-notice` div
- `generate_static_feed_page()` passes timestamp and new item count through from caller
- `check_feeds()` passes per-user new item count to page generation
- 6 new tests: 2 for HTML integration, 4 for `_build_status_text` helper

**Files modified:**
- `src/zorg/feed_poller.py` — Added `_build_status_text()`, updated `_build_feed_page_html()` / `generate_static_feed_page()` / `check_feeds()` signatures
- `tests/test_feed_poller.py` — Added `TestBuildStatusText` class and status notice integration tests

## 2026-02-09: Remove agent-task from Heartbeat System

Removed the `agent-task` check type from the heartbeat monitoring system. Users should use CRON.md with `silent_unless_action = true` instead, which provides the same functionality through the established scheduled jobs system. Updated documentation for both HEARTBEAT.md and CRON.md to clarify their distinct purposes (monitoring vs scheduling).

**Key changes:**
- Removed `_check_agent_task()` function and agent-task handling from `check_heartbeats()`
- Removed `heartbeat_check_name` from Task dataclass, `create_task()`, and all SQL queries
- Removed `pending_task_id` from HeartbeatState dataclass and related DB functions
- Removed heartbeat task result/cancellation/failure handling from scheduler
- Simplified silent job logic (no longer checks `heartbeat_check_name`)
- Rewrote heartbeat.md skill doc with purpose clarification, removed agent-task section
- Added purpose clarification intros to both heartbeat.md and schedules.md skill docs
- Updated storage templates (HEARTBEAT_EXAMPLE, CRON_EXAMPLE)
- Kept `heartbeat_silent` on Task and `_strip_action_prefix()` — still used by silent scheduled jobs

**Files modified:**
- `src/zorg/heartbeat.py` — Removed `_check_agent_task()`, agent-task handling in `check_heartbeats()`
- `src/zorg/db.py` — Removed `heartbeat_check_name` from Task, `pending_task_id` from HeartbeatState, all related SQL
- `schema.sql` — Removed columns from tasks and heartbeat_state tables
- `src/zorg/scheduler.py` — Removed heartbeat task handling blocks, simplified silent job logic
- `src/zorg/executor.py` — Removed "heartbeat" from excluded source types
- `config/skills/heartbeat.md` — Rewrote: removed agent-task, added purpose intro
- `config/skills/schedules.md` — Added purpose clarification intro
- `src/zorg/storage.py` — Updated HEARTBEAT_EXAMPLE and CRON_EXAMPLE templates
- `tests/test_heartbeat.py` — Removed TestCheckAgentTask, TestCheckHeartbeatsAgentTask, test_pending_task_id
- `tests/test_scheduler.py` — Removed 7 heartbeat-specific task tests
- `AGENTS.md` — Updated heartbeat section, removed agent-task references

## 2026-02-09: Shell Command Scheduled Jobs

Extended CRON.md to support direct shell command execution alongside Claude Code prompts. Command jobs flow through the same task queue (getting retry logic, `!stop`, failure tracking, and auto-disable for free) but execute via `subprocess.run()` instead of Claude Code. Each job must have exactly one of `prompt` or `command`.

**Key changes:**
- `CronJob` dataclass: added `command` field, mutual exclusivity validation with `prompt`
- `schema.sql`: added `command TEXT` column to both `tasks` and `scheduled_jobs` tables
- `db.py`: `Task` and `ScheduledJob` dataclasses, migrations, `create_task()`, all SELECT/RETURNING queries updated
- `scheduler.py`: new `_execute_command_task()` runs shell commands with core zorg env vars (`ZORG_TASK_ID`, `ZORG_USER_ID`, `ZORG_DB_PATH`, `NEXTCLOUD_MOUNT_PATH`, `ZORG_CONVERSATION_TOKEN`); `process_one_task()` branches on `task.command`
- `commands.py`: `!cron` list shows `(cmd)` indicator for command jobs
- `schedules.md`: documented `command` field with example
- 18 new tests across `test_cron_loader.py` (8) and `test_scheduler.py` (10)

**Files modified:**
- `src/zorg/cron_loader.py` — CronJob dataclass, parsing, generation, sync, migration
- `src/zorg/db.py` — Task/ScheduledJob dataclasses, migrations, create_task, queries
- `src/zorg/scheduler.py` — `_execute_command_task()`, branching in `process_one_task()`, `check_scheduled_jobs()`
- `src/zorg/commands.py` — `!cron` display with `(cmd)` indicator
- `schema.sql` — `command TEXT` on tasks and scheduled_jobs
- `config/skills/schedules.md` — Documented command field
- `tests/test_cron_loader.py` — 8 new tests for command job parsing, validation, generation, sync
- `tests/test_scheduler.py` — 10 new tests for command execution, integration, failure tracking

## 2026-02-09: File-Based Scheduled Jobs (CRON.md)

Moved scheduled job definitions from sqlite3-only to user-editable CRON.md files in each user's `zorg/config/` folder. The file uses the same markdown-with-TOML-block pattern as BRIEFINGS.md and HEARTBEAT.md. Definitions sync to the DB on each scheduler cycle, so downstream systems (cron evaluation, failure tracking, `!cron` command) continue working unchanged.

**Key changes:**
- New `cron_loader.py` module: `CronJob` dataclass, file loading, DB sync, and one-time migration from DB→file
- CRON.md file format: TOML `[[jobs]]` blocks with name, cron, prompt, target, room, enabled, silent_unless_action
- Sync logic preserves DB state fields (last_run_at, consecutive_failures) while updating definitions from file
- Enabled logic: file `enabled=false` disables in DB; file `enabled=true` does NOT re-enable (preserves `!cron disable`)
- Orphaned DB jobs (removed from file) are deleted during sync
- Auto-migration: if user has DB jobs but no CRON.md, file is auto-generated from DB entries
- Removed `admin_only` from schedules skill — non-admin users can now manage their own scheduled jobs via file editing
- Updated `schedules.md` skill doc to teach CRON.md file editing instead of sqlite3 commands
- Storage module: CRON.md template/example, seeded on user directory creation, included in workspace README

**Files added:**
- `src/zorg/cron_loader.py` — Core module: load, generate, sync, migrate
- `tests/test_cron_loader.py` — 30 tests for parsing, sync, migration, error handling

**Files modified:**
- `src/zorg/storage.py` — Added `get_user_cron_path()`, CRON templates, seeding in `ensure_user_directories_v2()`
- `src/zorg/scheduler.py` — Added `_sync_cron_files()` called at top of `check_scheduled_jobs()`
- `config/skills/schedules.md` — Rewritten for CRON.md file editing (was sqlite3 instructions)
- `config/skills/_index.toml` — Removed `admin_only = true` from `[schedules]`
- `tests/test_scheduler.py` — Added `@patch("zorg.scheduler._sync_cron_files")` to existing tests, new sync test
- `AGENTS.md` — Updated Scheduled Jobs section and project structure

## 2026-02-09: Admin/Non-Admin User Isolation

Added a root-owned admins file (`/etc/zorg/admins`) that defines which users get full system access. Non-admin users get a restricted prompt and environment: no DB access, scoped Nextcloud mount path, no admin-only skills. When no admins file exists, all users are admins (backward compatible).

**Key changes:**
- `Config.admin_users` set loaded from `/etc/zorg/admins` (or `ZORG_ADMINS_FILE` env var) at config load time
- `Config.is_admin(user_id)` returns True if set is empty (no file) or user is in set
- `SkillMeta.admin_only` field — skills marked `admin_only = true` are filtered out for non-admin users
- `schedules` and `tasks` skills marked admin-only (they teach sqlite3 DB operations)
- Non-admin `build_prompt()`: scoped mount path to `Users/{user_id}`, no DB path, no sqlite3 tool, no subtask creation rule
- Non-admin env vars: `ZORG_DB_PATH` omitted, `NEXTCLOUD_MOUNT_PATH` scoped to user directory
- Ansible role deploys `/etc/zorg/admins` as root:root 0644 (not editable by zorg user or Claude)

**Files added/modified:**
- `src/zorg/config.py` — Added `admin_users` field, `is_admin()` method, `load_admin_users()` function
- `src/zorg/skills_loader.py` — Added `admin_only` to `SkillMeta`, filtering in `select_skills()`
- `src/zorg/executor.py` — Admin-aware `build_prompt()` and `execute_task()` with scoped env vars
- `config/skills/_index.toml` — Added `admin_only = true` to `[schedules]` and `[tasks]`
- `tests/test_config.py` — 10 new tests for admin user loading and is_admin
- `tests/test_skills_loader.py` — 7 new tests for admin_only skill filtering
- `tests/test_executor.py` — 13 new tests for prompt and env var isolation
- `ansible: defaults/main.yml` — Added `zorg_admin_users: []`
- `ansible: tasks/main.yml` — Deploy `/etc/zorg/admins` file
- `ansible: templates/admins.j2` — New template for admins file

## 2026-02-09: Remove invoicing resource type requirement

INVOICING.md lives in the user's `zorg/config/` folder by convention and invoicing is built-in functionality, so it shouldn't need an explicit resource entry. Removed the `invoicing` resource type — the system now always resolves INVOICING.md from the user's config folder.

**Key changes:**
- Executor always resolves INVOICING.md from `zorg/config/` via `get_user_invoicing_path()`, no longer checks for an `invoicing` resource
- Invoice scheduler's `_resolve_invoicing_path()` simplified to only check the default config folder location
- Removed `"invoicing"` from accounting skill's `resource_types` in `_index.toml` (keywords already trigger skill loading)
- Removed test for resource-path-takes-precedence behavior

**Files modified:**
- `src/zorg/executor.py` — Removed invoicing resource lookup branch
- `src/zorg/invoice_scheduler.py` — Simplified `_resolve_invoicing_path()` to use default path only
- `config/skills/_index.toml` — Removed `"invoicing"` from accounting `resource_types`
- `tests/test_invoice_scheduler.py` — Removed obsolete resource precedence test

## 2026-02-09: Website Hosting Migration to Nextcloud Mount

Migrated static website hosting so user tilde pages (`~user`) are served from the Nextcloud mount instead of a standalone `/srv/app/zorg/html` directory. Fixed the zorg-scheduler service failing to start due to a stale `ReadWritePaths` referencing the removed directory.

**Key changes:**
- Fixed zorg-scheduler NAMESPACE error (systemd couldn't mount non-existent `/srv/www/bot.example.com`)
- Website paths now resolve to `Users/{user_id}/zorg/html` on the Nextcloud mount
- Added `www-data` to `nextcloud-mount` group so nginx can serve files from the FUSE mount
- Created separate `user-index.html.j2` Ansible template for per-user tilde homepages
- Removed `zorg_site_base_path` Ansible variable (no longer needed)
- Removed `base_path` from `[site]` config section
- Added `X-Robots-Tag: noindex, nofollow` header to nginx site config
- Fixed pre-existing streaming progress test (gerund removal)

**Files added/modified:**
- `src/zorg/executor.py` — Website path resolution uses `nextcloud_mount_path` instead of `site.base_path`
- `src/zorg/feed_poller.py` — Feed page generation uses Nextcloud mount path
- `config/skills/website.md` — Updated path description, removed chgrp instructions
- `tests/test_executor.py` — Updated website env var and prompt tests
- `tests/test_feed_poller.py` — Updated feed page generation tests
- `tests/test_executor_streaming.py` — Fixed progress callback assertion

**Ansible role changes (ansible-server):**
- `roles/zorg/templates/user-index.html.j2` — New per-user tilde homepage template
- `roles/zorg/templates/zorg-site.conf.j2` — Added X-Robots-Tag noindex/nofollow
- `roles/zorg/templates/zorg-scheduler.service.j2` — Removed stale ReadWritePaths
- `roles/zorg/templates/config.toml.j2` — Removed `base_path` from `[site]` section
- `roles/zorg/defaults/main.yml` — Removed `zorg_site_base_path` variable
- `roles/zorg/tasks/main.yml` — Added www-data to nextcloud-mount group task
- `roles/zorg/handlers/main.yml` — Added nginx restart handler

## 2026-02-09: Rename workspace/ → zorg/ with config/ Subfolder

Renamed the per-user `workspace/` directory to `zorg/` and moved all configuration files (USER.md, TASKS.md, BRIEFINGS.md, etc.) into a new `zorg/config/` subfolder. Also moved `exports/` inside `zorg/` to consolidate bot-managed content.

**New directory layout:**
```
/Users/{user_id}/
├── zorg/              # Shared with user via OCS (was workspace/)
│   ├── config/        # Configuration files
│   │   ├── USER.md, TASKS.md, BRIEFINGS.md, HEARTBEAT.md,
│   │   └── INVOICING.md, ACCOUNTING.md, FEEDS.md
│   ├── exports/       # Bot-generated files (was top-level)
│   ├── examples/
│   └── README.md
├── inbox/
├── memories/
├── shared/
└── scripts/
```

**Key changes:**
- Renamed `get_user_workspace_path()` → `get_user_zorg_path()` (backward-compat alias kept)
- Added `get_user_config_path()` — all config file paths now resolve through this
- All path helpers (`get_user_memory_path`, `get_user_tasks_file_path`, etc.) updated to use `zorg/config/`
- Added `_migrate_workspace_to_zorg()` migration: renames `workspace/` → `zorg/`, moves config .md files into `zorg/config/`
- Old `exports/` contents migrated to `zorg/exports/` automatically
- Migration chain: `notes/` → `workspace/` → `zorg/` (all three steps preserved for old installs)

**Files modified:**
- `src/zorg/storage.py` — Core path functions, directory setup, migration logic, README templates
- `src/zorg/tasks_file_poller.py` — Uses `get_user_config_path` for TASKS.md discovery
- `src/zorg/commands.py` — Updated `!memory` command path
- `src/zorg/executor.py` — Updated comments for config file resolution
- `src/zorg/invoice_scheduler.py` — Updated comments
- `src/zorg/memory_search.py` — Updated USER.md indexing path
- `src/zorg/cli.py` — Updated directory listing output
- `src/zorg/scheduler.py` — Updated comment
- `config/skills/memory.md` — Updated all path references and directory diagram
- `config/skills/accounting.md` — Updated INVOICING.md resource path
- `config/skills/briefings-config.md` — Updated BRIEFINGS.md path and descriptions
- `config/skills/heartbeat.md` — Updated HEARTBEAT.md path reference
- `config/skills/_index.toml` — Updated skill descriptions
- `AGENTS.md` — Updated directory structure diagram and all path references
- `tests/test_storage.py` — Updated all path assertions, added `TestMigrateWorkspaceToZorg` class
- `tests/test_tasks_file_poller.py` — Updated workspace → zorg/config paths
- `tests/test_invoice_scheduler.py` — Updated workspace → zorg/config paths
- `tests/test_heartbeat.py` — Updated workspace → zorg/config paths
- `tests/test_briefing_loader.py` — Updated workspace → zorg/config paths
- `tests/test_commands.py` — Updated workspace → zorg/config paths
- `tests/test_feed_poller.py` — Updated workspace → zorg/config paths
- `tests/test_skills_invoicing.py` — Updated example paths

---

## 2026-02-09: ntfy Push Notifications & Centralized Notification Dispatcher

Added ntfy as a broadcast notification surface alongside Talk and Email. Created a centralized `notifications.py` module that extracts the duplicated notification logic from `invoice_scheduler.py` and `heartbeat.py` into a single dispatcher. Supports `"talk"`, `"email"`, `"ntfy"`, `"both"` (talk+email), and `"all"` (talk+email+ntfy) surface values throughout the system.

**Key changes:**
- New `NtfyConfig` dataclass with server_url, topic, token (bearer), username/password (basic auth), and priority
- Per-user `ntfy_topic` override on `UserConfig` — topic resolution: explicit call param > user config > global config
- New `src/zorg/notifications.py` — central `send_notification()` dispatcher with `_send_talk()`, `_send_email()`, `_send_ntfy()` helpers
- `invoice_scheduler.py` delegates to `notifications.py` (thin `_send_notification` wrapper preserved for internal callers)
- `heartbeat.py` `send_heartbeat_alert()` converted from async to sync, delegates to `notifications.send_notification()` with `surface=check.channel` — heartbeats now support ntfy and email alerts
- `scheduler.py` output_target resolution extended for `"ntfy"` and `"all"` surfaces in both success and failure delivery paths
- 30 new tests in `test_notifications.py` covering all surfaces, auth modes, topic resolution, and error handling

**Files added/modified:**
- `src/zorg/config.py` — `NtfyConfig` dataclass, `ntfy` on `Config`, `ntfy_topic` on `UserConfig`, `[ntfy]` parsing in `load_config()`
- `src/zorg/notifications.py` — **New** central notification dispatcher
- `src/zorg/invoice_scheduler.py` — Removed `_send_talk_notification`, `_send_email_notification`, `_resolve_conversation_token`; delegates to `notifications.py`
- `src/zorg/heartbeat.py` — `send_heartbeat_alert()` now sync, uses `notifications.send_notification()`; removed `asyncio` import
- `src/zorg/scheduler.py` — Extended output_target for `"ntfy"` and `"all"` in success/failure delivery
- `config/config.example.toml` — Documented `[ntfy]` section
- `config/users/alice.example.toml` — Documented `ntfy_topic` per-user field
- `tests/test_notifications.py` — **New** 30 tests
- `tests/test_invoice_scheduler.py` — Updated mock targets for delegated notification calls
- `tests/test_heartbeat.py` — Removed `AsyncMock` for `send_heartbeat_alert`
- Ansible `defaults/main.yml` — `zorg_ntfy_*` variables
- Ansible `config.toml.j2` — `[ntfy]` section
- Ansible `user.toml.j2` — `ntfy_topic` per-user field

---

## 2026-02-09: Briefing Preamble Stripping, Calendar Pre-Fetching & Market Emoji Fix

Fixed three briefing issues: agent "thoughts" leaking into output, calendar timezone handling relying on the agent to pass --tz correctly, and missing red/green emoji indicators on futures quotes.

**Key changes:**
- Added `strip_briefing_preamble()` in scheduler.py — detects first emoji section header and strips everything before it
- Applied preamble stripping to both Talk and email delivery paths for briefing tasks
- Strengthened prompt instruction: "Your response must start with the first emoji section header"
- Added `_fetch_calendar_events()` in briefing.py — pre-fetches today's/tomorrow's events with correct timezone via CalDAV, just like markets and TODOs
- Calendar pre-fetch falls back to agent-fetched instruction if CalDAV is unavailable
- Updated briefing skill doc to show 🟢/🔴/⚪ emoji format for all tickers (futures, indices, commodities)
- 14 new tests covering preamble stripping, calendar pre-fetching, and prompt integration

**Files modified:**
- `src/zorg/briefing.py` — Added `_fetch_calendar_events()`, updated calendar component to pre-fetch, tightened prompt instruction
- `src/zorg/scheduler.py` — Added `strip_briefing_preamble()`, applied to briefing result delivery (Talk + email)
- `config/skills/briefing.md` — Updated MARKETS section format to show emoji indicators for all quote types
- `tests/test_briefing.py` — Added `TestFetchCalendarEvents` (6 tests), `TestCalendarPreFetchInPrompt` (2 tests), updated existing test
- `tests/test_scheduler.py` — Added `TestStripBriefingPreamble` (6 tests)

---

## 2026-02-08: Tumblr Feed Fixes & Gallery Improvements

Fixed multiple issues with Tumblr feed integration: TLS fingerprint rejection, missing reblog images, multi-image photoset rendering, date sorting, and feed item retention.

**Key changes:**
- Switched Tumblr API fetcher from httpx to requests (TLS fingerprint fix for 403s)
- Extract images from reblog `trail[].content[]` in addition to top-level `content[]`
- Multi-image support: collect all images per post, store as JSON array in `image_url` column
- Gallery rendering: 2x2 thumbnail grid in grid view with "+N" overlay badge for large photosets
- List view shows all images at full width with natural aspect ratio
- Card layout fix: title/excerpt wrapped in `.card-body` with `overflow:hidden`, meta bar pinned at bottom
- Images use `object-fit:contain` with dark background in grid view so tall images stay fully visible
- Normalized Tumblr dates to ISO format for correct cross-feed chronological sorting
- Configurable `feed_item_retention_days` setting (default 30, was hardcoded)
- `get_feed_items` now filters by `max_age_days` so the static page matches retention window
- Bumped feed page item limit from 200 to 1000
- Added `requests` as explicit dependency

**Files added/modified:**
- `src/zorg/feed_poller.py` — requests swap, trail extraction, multi-image JSON, gallery HTML/CSS, date normalization, retention filtering
- `src/zorg/config.py` — `feed_item_retention_days` on `SchedulerConfig`
- `src/zorg/db.py` — `max_age_days` parameter on `get_feed_items`
- `src/zorg/scheduler.py` — Use configurable retention days for feed cleanup
- `tests/test_feed_poller.py` — Tests for trail extraction, multi-image, gallery rendering
- `pyproject.toml` / `uv.lock` — Added `requests` dependency
- Ansible: `defaults/main.yml` + `templates/config.toml.j2` — `zorg_scheduler_feed_item_retention_days`

---

## 2026-02-08: Remove Gerund Conversion from Progress Descriptions

Removed the automatic imperative-to-gerund verb conversion (`_to_gerund`) that was transforming Bash tool descriptions in streaming progress updates (e.g., "List files" became "Listing files"). Descriptions now pass through as-is.

**Key changes:**
- Removed `_to_gerund()` function and `_VOWELS` constant from `stream_parser.py`
- Bash tool descriptions now displayed verbatim instead of being converted to present participle
- Removed `TestToGerund` test class (13 tests) and updated 3 assertions in remaining tests

**Files modified:**
- `src/zorg/stream_parser.py` — Removed `_to_gerund()`, `_VOWELS`; pass description through directly
- `tests/test_stream_parser.py` — Removed `TestToGerund` class, updated expected values

---

## 2026-02-08: Developer Skill Security Hardening

Added API endpoint allowlist and namespace resolution to the developer skill, reducing the blast radius of the GitLab token. Split `gitlab_username` into separate auth and namespace fields to support a dedicated bot account with Developer role.

**Key changes:**
- `gitlab_api_allowlist` config field — configurable list of `METHOD /path/*` patterns enforced in the generated API wrapper script via shell `case` statement
- Default allowlist: read all endpoints, create MRs/issues, post comments. Merge, delete, settings, and admin operations blocked
- Query strings stripped before matching (`${ENDPOINT%%\?*}`) so `?state=opened` doesn't break patterns
- Fixed piping issue with `$GITLAB_API_CMD` — removed `exec` from wrapper script so output flows through pipes correctly
- Split `gitlab_username` (for HTTPS auth) from new `gitlab_default_namespace` (for resolving short repo names like "nebula" → "example/nebula")
- `GITLAB_DEFAULT_NAMESPACE` env var passed to Claude instead of `GITLAB_USERNAME`
- Shell integration test verifies case globs actually work (runs `sh -c` with generated patterns)
- Skill doc updated: removed merge section, documented endpoint restrictions, added piping workaround guidance
- 7 new tests (suite now at 1421)

**Files modified:**
- `src/zorg/config.py` — `gitlab_api_allowlist` + `gitlab_default_namespace` on `DeveloperConfig`, TOML parsing
- `src/zorg/executor.py` — `_allowlist_pattern_to_case()` helper, allowlist enforcement in wrapper script, `GITLAB_DEFAULT_NAMESPACE` env var
- `config/skills/developer.md` — Removed merge section, added allowlist docs + namespace resolution + piping guidance
- `config/config.example.toml` — Documented new fields
- `tests/test_executor.py` — `TestAllowlistPatternConversion` (5 tests) + 2 wrapper allowlist tests
- `.claude/rules/config.md` — Updated `DeveloperConfig` reference
- `.claude/rules/executor.md` — Updated env var mapping table
- Ansible: `defaults/main.yml` + `templates/config.toml.j2` — `zorg_developer_gitlab_default_namespace` + `zorg_developer_gitlab_api_allowlist`

---

## 2026-02-08: Developer Skill (Git + GitLab Workflows)

Skill-doc-only developer skill that teaches Claude Code git worktree workflows and GitLab merge request management. Uses bare clones for repo storage and git worktrees for branch isolation, with GitLab API for MR lifecycle.

**Key changes:**
- New skill doc (`config/skills/developer.md`) covering clone, worktree, commit, push, MR create/merge/list, follow-up, cleanup
- `DeveloperConfig` dataclass with `enabled`, `repos_dir`, `gitlab_url`, `gitlab_token`, `gitlab_username`
- Credential security: GitLab token is never exposed as an env var — executor writes a git credential helper script and a `gitlab-api` wrapper script to the user's temp dir, passes paths instead
- Git auth configured via `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_0`/`GIT_CONFIG_VALUE_0` env vars pointing to credential helper
- API calls use `$GITLAB_API_CMD METHOD /api/v4/...` pattern — token stays out of prompt and command output
- Token requires only `api` scope (covers both repository access and MR management)
- Helper scripts recreated on every task execution (ephemeral, scoped to task lifetime)
- 9 new tests (5 config, 4 executor); suite now at 1414 tests

**Files added:**
- `config/skills/developer.md` — Skill doc with git worktree + GitLab API workflows

**Files modified:**
- `config/skills/_index.toml` — Added `[developer]` entry with keywords
- `src/zorg/config.py` — Added `DeveloperConfig`, `[developer]` TOML parsing
- `src/zorg/executor.py` — Credential helper + API wrapper script generation, env var setup
- `config/config.example.toml` — Documented `[developer]` section
- `tests/test_config.py` — `TestDeveloperConfig` (5 tests)
- `tests/test_executor.py` — `TestDeveloperEnvVars` (4 tests)
- `.claude/rules/config.md` — Added `DeveloperConfig` reference
- `.claude/rules/executor.md` — Added developer env vars to mapping table
- `.claude/rules/skills.md` — Added developer to skill index table
- Ansible: `defaults/main.yml` + `templates/config.toml.j2` — `zorg_developer_*` variables

---

## 2026-02-07: Scheduled Job Isolation

Comprehensive isolation improvements for scheduled/background jobs to prevent them from polluting interactive conversations, hogging worker slots, and producing noisy output.

**Key changes:**
- Context isolation: `get_conversation_history()` now excludes `scheduled`, `briefing`, and `heartbeat` source types from interactive Talk/email context
- Worker pool isolation: Two-phase `dispatch()` prioritizes interactive (talk/email) tasks over background jobs, with configurable `reserved_interactive_workers` (default: 2)
- Output suppression: Scheduled jobs support `silent_unless_action` — only posts output when response contains `ACTION:` prefix, suppresses `NO_ACTION:` results
- Failure tracking: `consecutive_failures`, `last_error`, `last_success_at` columns on `scheduled_jobs` table
- Auto-disable: Jobs automatically disabled after N consecutive failures (default: 5, configurable via `scheduled_job_max_consecutive_failures`, 0 to disable)
- `!status` command now groups tasks into interactive vs background sections
- New `!cron` command: list all scheduled jobs, `!cron enable <name>`, `!cron disable <name>`
- Extracted `_strip_action_prefix()` helper for ACTION/NO_ACTION parsing (shared by heartbeat + silent scheduled jobs)
- 34 new tests (suite now at ~1384 tests)

**Files modified:**
- `src/zorg/db.py` — `exclude_source_types` on `get_conversation_history()`, `scheduled_job_id` on tasks, new ScheduledJob fields + query functions, worker isolation queries
- `src/zorg/scheduler.py` — Two-phase `WorkerPool.dispatch()`, `_strip_action_prefix()`, silent scheduled job handling, failure tracking + auto-disable in `process_one_task()`
- `src/zorg/executor.py` — Passes exclusion list to context history queries
- `src/zorg/commands.py` — `!status` interactive/background grouping, `!cron` command
- `src/zorg/config.py` — `reserved_interactive_workers`, `scheduled_job_max_consecutive_failures` on SchedulerConfig
- `schema.sql` — `scheduled_job_id` on tasks, new columns on `scheduled_jobs`
- `config/skills/schedules.md` — Documented `silent_unless_action` and tracking columns
- `tests/test_db.py` — 14 new tests (context isolation, scheduled jobs, worker queries)
- `tests/test_scheduler.py` — 13 new tests (strip prefix, silent jobs, failure tracking, auto-disable, worker isolation)
- `tests/test_commands.py` — 9 new tests (status grouping, cron command)
- `roles/zorg/defaults/main.yml` — New scheduler variables
- `roles/zorg/templates/config.toml.j2` — New scheduler config lines

---

## 2026-02-07: Unlimited Memory Retention

Changed default `memory_retention_days` from 90 to 0 (unlimited). Dated memory files are small, not auto-loaded into prompts, and only accessed on demand via memory search — no practical reason to delete them. Setting `memory_retention_days = 0` now skips cleanup entirely.

**Key changes:**
- Both `cleanup_old_memory_files()` and `cleanup_old_channel_memory_files()` return early when `retention_days <= 0`
- Default changed from 90 to 0 on `SleepCycleConfig` and `ChannelSleepCycleConfig` dataclasses and TOML parsing fallbacks
- Updated config example, Ansible defaults, and user template to match

**Files modified:**
- `src/zorg/sleep_cycle.py` — Early return guard in both cleanup functions
- `src/zorg/config.py` — Default changed to 0, updated comments
- `config/config.example.toml` — Updated comments to document `0 = unlimited`
- `tests/test_sleep_cycle.py` — Added `test_skips_cleanup_when_retention_zero`
- `tests/test_channel_sleep_cycle.py` — Added `test_skips_cleanup_when_retention_zero`
- `tests/test_config.py` — Updated default assertions
- `roles/zorg/defaults/main.yml` — Updated defaults to 0
- `roles/zorg/templates/user.toml.j2` — Updated Jinja2 default to 0

---

## 2026-02-07: Channel Sleep Cycle with Memory Search Integration

Added channel-level nightly memory extraction that runs parallel to user sleep cycles. Auto-discovers active channels from recent completed tasks — no explicit channel list needed. Extracts shared decisions, agreements, and project status from channel conversations, writes dated files to `/Channels/{token}/memories/`, and indexes them into the semantic memory search system under `channel:{token}` namespace.

**Key changes:**
- `ChannelSleepCycleConfig` dataclass with `enabled`, `cron`, `lookback_hours`, `memory_retention_days`
- Auto-discovery of active channels via `get_active_channel_tokens()` — queries recent completed tasks with conversation tokens
- Channel memory extraction prompt focused on shared context (decisions, agreements, project status)
- Memory search integration: channel conversations indexed under `channel:{token}` namespace at task completion time
- Search and stats automatically include channel memories when `ZORG_CONVERSATION_TOKEN` is set
- Reindex scans `/Channels/*/memories/*.md` for channel memory files
- `channel_sleep_cycle_state` table for per-channel state tracking
- 54 new tests across channel sleep cycle, config, DB, and memory search (suite at ~1296 tests)

**Files added:**
- `tests/test_channel_sleep_cycle.py` — 25 tests for channel sleep cycle

**Files modified:**
- `src/zorg/sleep_cycle.py` — Added channel sleep cycle functions (gather, extract, process, cleanup, check)
- `src/zorg/config.py` — Added `ChannelSleepCycleConfig` dataclass + parsing
- `src/zorg/db.py` — Added channel sleep cycle state table + queries, active channel discovery
- `src/zorg/scheduler.py` — Channel sleep cycle check + processing in main loop
- `src/zorg/memory_search.py` — Channel namespace support, `include_user_ids` parameter
- `src/zorg/skills/memory_search.py` — Channel token support in CLI
- `src/zorg/executor.py` — Pass `ZORG_CONVERSATION_TOKEN` env var
- `schema.sql` — Added `channel_sleep_cycle_state` table
- `config/config.example.toml` — Added `[channel_sleep_cycle]` section
- `tests/test_config.py` — Channel sleep cycle config tests
- `tests/test_db.py` — Channel sleep cycle state DB tests
- `tests/test_memory_search.py` — Channel namespace search tests
- `tests/test_skills_memory_search.py` — Channel token CLI tests

---

## 2026-02-07: Workspace Example Files

Split workspace file templates into minimal user configs and comprehensive `examples/` reference files. User files now contain just a header and commented-out TOML starter block, with a pointer to `examples/` for full documentation. Example files are always overwritten on startup to stay current with the codebase. Moved the Ansible-managed INVOICING.md reference block into zorg's own example files.

**Key changes:**
- Split all 6 workspace templates (`README`, `TASKS`, `BRIEFINGS`, `HEARTBEAT`, `INVOICING`, `ACCOUNTING`) into minimal `*_TEMPLATE` + comprehensive `*_EXAMPLE` constants
- `ensure_user_directories_v2()` now creates `workspace/examples/` and writes all example files on every run
- Removed Ansible `blockinfile` task and `invoicing-reference.md.j2` template — reference docs now managed by zorg directly
- Added `agent-task` check type to HEARTBEAT example, cron format reference to BRIEFINGS example
- 4 new tests for example file creation, content verification, and overwrite behavior (suite at 1296 tests)

**Files modified:**
- `src/zorg/storage.py` — Refactored templates, added `*_EXAMPLE` constants, updated `ensure_user_directories_v2()`
- `tests/test_storage.py` — Added example file tests, updated briefings assertion
- `roles/zorg/tasks/main.yml` — Removed INVOICING.md blockinfile tasks

**Files removed:**
- `roles/zorg/templates/invoicing-reference.md.j2`

---

## 2026-02-07: Semantic Memory Search

Added hybrid BM25 + vector search over conversations and memory files. Uses FTS5 for keyword search and sqlite-vec + sentence-transformers for semantic similarity, fused via Reciprocal Rank Fusion. Gracefully degrades to BM25-only if sqlite-vec or torch is unavailable. Disabled by default — enable via `[memory_search]` config section.

**Key changes:**
- New `memory_chunks` table with FTS5 virtual table and auto-sync triggers
- Core module with chunking (paragraph/sentence/word boundaries with overlap), content-hash dedup, lazy-loaded embedding model, and hybrid search
- CLI skill following standard `build_parser()`/`main()` pattern: `search`, `index`, `reindex`, `stats` commands
- Post-completion conversation indexing hook in scheduler.py (non-critical, debug-logged on failure)
- Post-write memory file indexing hook in sleep_cycle.py
- `MemorySearchConfig` dataclass with `enabled`, `auto_index_conversations`, `auto_index_memory_files` fields
- Optional dependency group `memory-search` for sqlite-vec + sentence-transformers
- 52 new tests (36 core + 16 CLI), full suite at 1292 tests across 33 files

**Files added:**
- `src/zorg/memory_search.py` — Core module: embedding, chunking, indexing, hybrid search, RRF fusion
- `src/zorg/skills/memory_search.py` — CLI skill with search/index/reindex/stats commands
- `config/skills/memory-search.md` — Skill documentation for Claude Code
- `tests/test_memory_search.py` — 36 tests for core module
- `tests/test_skills_memory_search.py` — 16 tests for CLI skill

**Files modified:**
- `schema.sql` — Added `memory_chunks` table, FTS5 virtual table, sync triggers
- `src/zorg/config.py` — Added `MemorySearchConfig` dataclass + parsing
- `src/zorg/scheduler.py` — Post-completion conversation indexing hook
- `src/zorg/sleep_cycle.py` — Post-write memory file indexing hook
- `config/skills/_index.toml` — Registered `memory-search` skill with keywords
- `pyproject.toml` — Added `memory-search` optional dependency group
- `roles/zorg/defaults/main.yml` — Added `zorg_memory_search_enabled` var
- `roles/zorg/templates/config.toml.j2` — Added `[memory_search]` section

---

## 2026-02-07: Ansible-managed INVOICING.md Config Reference

Added an Ansible-managed reference block that gets appended to each user's INVOICING.md file during deployment. Documents all available config options (global settings, company/entity, clients, client invoicing, services, work log entries). Uses `ini` fenced code blocks instead of `toml` to prevent the invoicing parser from treating reference examples as actual config.

**Key changes:**
- New Jinja2 template covering all config option groups with defaults and descriptions
- Ansible tasks: `stat` check for existing INVOICING.md files + `blockinfile` to insert/update the reference
- `blockinfile` markers (`<!-- BEGIN/END ANSIBLE MANAGED BLOCK -->`) ensure idempotent updates on re-deploy

**Files added:**
- `roles/zorg/templates/invoicing-reference.md.j2` — Config reference template

**Files modified:**
- `roles/zorg/tasks/main.yml` — Added stat + blockinfile tasks between ledger backups and Fava sections

---

## 2026-02-07: Scheduled Invoice Generation with Reminders

Added automatic invoice generation to the scheduler. Clients with `schedule = "monthly"` in INVOICING.md now get invoices auto-generated on their configured `day`. A configurable reminder is sent N days before generation, and a summary notification is sent after invoices are created. Notifications are sent directly (not via Claude tasks) through Talk, email, or both — surface is configurable per-client, per-config, and per-user with a fallback chain.

**Key changes:**
- New `invoice_scheduler.py` module with `check_scheduled_invoices()` — checks all users/clients for due reminders and generations
- Added `reminder_days` (default 3, 0 disables) and `notifications` fields to `ClientConfig` in invoicing.py
- Added `notifications` field to `InvoicingConfig` for global default
- Added `invoicing_notifications` and `invoicing_conversation_token` to `UserConfig` in config.py
- New `invoice_schedule_state` DB table for tracking reminder/generation timestamps per user/client
- Integrated into both `run_scheduler()` (one-shot) and `run_daemon()` (continuous, on `briefing_check_interval` cadence)
- Notification surface resolution chain: client override > config global > user default > "talk"
- 39 new tests covering timing logic, notification delivery, state tracking, and integration
- 5 new config field tests in existing invoicing test suite (152 → 157 tests)
- Updated Ansible deployment role: `user.toml.j2` template and `defaults/main.yml`

**Files added:**
- `src/zorg/invoice_scheduler.py` — Core scheduling + notification logic
- `tests/test_invoice_scheduler.py` — 39 tests

**Files modified:**
- `src/zorg/skills/invoicing.py` — Added `reminder_days`, `notifications` to `ClientConfig`/`InvoicingConfig` + parsing
- `src/zorg/config.py` — Added `invoicing_notifications`, `invoicing_conversation_token` to `UserConfig`
- `src/zorg/db.py` — Added `InvoiceScheduleState` dataclass and get/set functions
- `schema.sql` — Added `invoice_schedule_state` table
- `src/zorg/scheduler.py` — Integrated `check_scheduled_invoices` into both scheduler loops
- `tests/test_skills_invoicing.py` — 5 new config field parsing tests
- `config/skills/accounting.md` — Documented scheduled invoicing and config options
- `config/users/alice.example.toml` — Added invoice notification examples
- `CLAUDE.md` — Added `invoice_scheduler.py` to structure, documented scheduling, updated test count
- `roles/zorg/templates/user.toml.j2` — Added invoicing notification fields
- `roles/zorg/defaults/main.yml` — Added example invoicing notification config

---

## 2026-02-07: Fix abs() Bug in Ledger Transaction Dedup

Fixed a bug in `_parse_ledger_transactions()` where parsed amounts from the ledger were not wrapped in `abs()`, causing hash mismatches with callers (e.g. `cmd_import_monarch`, `cmd_sync_monarch`) that compute hashes using `abs(amount)`. This could cause duplicate imports when a ledger entry had a negative amount posting.

**Key changes:**
- Applied `abs()` to parsed amounts in `_parse_ledger_transactions()` so content hashes match callers
- Added test for negative amount parsing in ledger dedup

**Files modified:**
- `src/zorg/skills/accounting.py` — `abs()` fix in `_parse_ledger_transactions()`
- `tests/test_skills_accounting.py` — New `test_applies_abs_to_negative_amounts` test

---

## 2026-02-07: Fava Web GUI for Beancount Ledgers

Added Fava as a per-user systemd service managed by the Ansible role. Each user with ledger resources and a configured `fava_port` gets their own Fava instance on a dedicated port, providing a web-based beancount ledger viewer. Access is restricted to wireguard/private networks via existing UFW rules.

**Key changes:**
- Added `fava>=1.29` as a project dependency (installs alongside beancount in shared venv)
- New Ansible variables: `zorg_fava_enabled`, `zorg_fava_host` (defaults to `0.0.0.0`)
- Per-user systemd service template (`zorg-fava.service.j2`) with security hardening and read-only Nextcloud mount
- Ansible tasks for deploying/enabling Fava services per user, with full cleanup when disabled
- Restart handler for all `zorg-fava-*` services
- Users opt in by setting `fava_port` in their config; users without it are skipped

**Files added/modified:**
- `pyproject.toml` — Added `fava>=1.29` dependency
- `roles/zorg/defaults/main.yml` — Added `zorg_fava_enabled` and `zorg_fava_host` variables
- `roles/zorg/templates/zorg-fava.service.j2` — **New** per-user systemd unit template
- `roles/zorg/tasks/main.yml` — Added Fava deploy/enable/cleanup tasks (~55 lines)
- `roles/zorg/handlers/main.yml` — Added `restart fava services` handler

---

## 2026-02-07: Monarch Sync Tag Reconciliation

Added auto-recategorization for Monarch transactions when the business tag is removed. When a previously-synced transaction loses its qualifying tag in Monarch, the sync creates a reversal entry that moves the expense to a personal account (default: `Expenses:Personal-Expense`). This handles the case where transactions are initially categorized as business but later reclassified as personal.

**Key changes:**
- Extended `monarch_synced_transactions` schema with metadata for reconciliation: `tags_json`, `amount`, `merchant`, `posted_account`, `txn_date`, `recategorized_at`
- Added `recategorize_account` config option (default: `Expenses:Personal-Expense`) in `[monarch.sync]` section
- New `_format_recategorization_entry()` helper generates reversal postings
- New DB functions: `get_active_monarch_synced_transactions()`, `mark_monarch_transaction_recategorized()`
- Updated `track_monarch_transaction()` and `track_monarch_transactions_batch()` to store full metadata
- `cmd_sync_monarch()` now performs reconciliation after syncing: checks all active synced transactions against current Monarch state, creates reversals for those missing the business tag
- Recategorization entries written to separate staging file (`monarch_recategorize_*.beancount`)
- 4 new tests, all 98 accounting tests pass

**Files modified:**
- `schema.sql` — Extended `monarch_synced_transactions` with reconciliation columns
- `src/zorg/db.py` — `MonarchSyncedTransaction` dataclass, updated tracking functions, new reconciliation functions, migrations
- `src/zorg/skills/accounting.py` — `recategorize_account` config, `_format_recategorization_entry()`, reconciliation logic in `cmd_sync_monarch()`
- `tests/test_skills_accounting.py` — New tests for reconciliation tracking and recategorization formatting

---

## 2026-02-07: Monarch Money API Integration

Added Monarch Money API integration for automated beancount ledger syncing. Transactions can now be synced directly from the Monarch Money API using a session token, with support for account/category mapping and tag-based filtering. Also fixed the existing CSV import which had incorrect column mappings, and added deduplication tracking to prevent duplicate imports.

**Key changes:**
- New `ACCOUNTING.md` config template with TOML block for Monarch Money settings (credentials, account/category mappings, tag filters)
- New `sync-monarch` CLI command with `--dry-run` flag for API-based transaction sync
- Fixed `_parse_monarch_csv()` to use correct column order (Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner)
- Tag filtering support for both CSV import (`--tag`/`--exclude-tag`) and API sync (via config)
- Deduplication via SQLite tables: `monarch_synced_transactions` (API transaction IDs), `csv_imported_transactions` (content hashes)
- Config parsing dataclasses: `MonarchCredentials`, `MonarchSyncSettings`, `MonarchTagFilters`, `MonarchConfig`
- Using `monarchmoneycommunity` fork which has correct API endpoint (api.monarch.com) and gql 4.0 support
- `ACCOUNTING_CONFIG` env var handling in executor

**Files added/modified:**
- `src/zorg/skills/accounting.py` — Config dataclasses, `_extract_toml_from_markdown()`, `parse_accounting_config()`, fixed `_parse_monarch_csv()`, `_fetch_monarch_transactions()`, `cmd_sync_monarch()`, tag filtering functions
- `src/zorg/storage.py` — `ACCOUNTING_TEMPLATE`, `get_user_accounting_path()`, seeding in `ensure_user_directories_v2()`
- `src/zorg/db.py` — Deduplication functions: `is_monarch_transaction_synced()`, `track_monarch_transaction()`, `compute_transaction_hash()`, `is_csv_transaction_imported()`, `track_csv_transaction()`, batch variants
- `src/zorg/executor.py` — `ACCOUNTING_CONFIG` env var handling (after invoicing section)
- `schema.sql` — New tables: `monarch_synced_transactions`, `csv_imported_transactions`
- `pyproject.toml` — `monarchmoneycommunity` dependency via git, `tool.hatch.metadata.allow-direct-references`
- `tests/test_skills_accounting.py` — New test classes: `TestTagParsing`, `TestAccountingConfigParsing`, `TestDeduplicationFunctions`

---

## 2026-02-05: Uninvoiced Entry Selection & Invoice Stamping

Changed invoice generation from date-based entry selection to "uninvoiced entries" selection. After generation, processed entries are stamped with `invoice = "INV-000042"` in the work log file. This prevents duplicate invoicing and provides an audit trail. The `--period` flag is now optional (acts as upper date bound instead of exact month match).

**Key changes:**
- New `invoice` field on `WorkEntry` dataclass, parsed from work log TOML
- New `select_uninvoiced_entries()` function — primary filter is entries where `invoice` field is empty; `--period` acts as optional upper date bound (`date <= last day of month`)
- New `stamp_work_log_entries()` function — regex-based write-back to raw markdown, processes entries in reverse index order to avoid position shifts
- `generate_invoices_for_period()` now uses uninvoiced selection, tracks entry indices through bundle grouping via `id(entry)`, and stamps entries after generation (skipped on dry run)
- `--period` is now optional in CLI — omitting it selects all uninvoiced entries
- Updated `WORK_LOG_TEMPLATE` with commented `invoice` field example
- 29 new tests, all 1115 tests pass

**Files modified:**
- `src/zorg/skills/invoicing.py` — `WorkEntry.invoice` field, `select_uninvoiced_entries()`, `stamp_work_log_entries()`, updated `parse_work_log()`, `generate_invoices_for_period()`, `WORK_LOG_TEMPLATE`
- `src/zorg/skills/accounting.py` — `--period` optional, updated response messages
- `tests/test_skills_invoicing.py` — 29 new tests in 5 classes: `TestWorkEntryInvoiceField`, `TestSelectUninvoicedEntries`, `TestStampWorkLogEntries`, `TestGenerateInvoicesStamping`, `TestCLIPeriodOptional`

---

## 2026-02-05: Multi-Entity Invoicing Support

Added support for multiple billing entities (e.g., personal and LLC) in the invoicing system. Users can now define multiple companies in their config and assign clients and work entries to specific entities. Each entity can have its own logo, payment instructions, A/R account, bank account, and currency. Fully backward compatible — existing single `[company]` configs continue to work unchanged.

**Key changes:**
- New `[companies.<key>]` config format alongside existing `[company]` (backward compat: single company wrapped as key `"default"`)
- Entity resolution chain: `entry.entity > client.entity > config.default_entity`
- Per-entity overrides: `ar_account`, `bank_account`, `currency`, `logo`, `payment_instructions`
- Account resolution chains: A/R (`client > entity > config default`), bank (`entity > config default`), currency (`entity > config default`)
- Invoice generation groups entries by `(client, entity)` — never mixes entities in one invoice
- Per-entity logo resolution in both `generate` and `create` commands
- `--entity/-e` CLI flag on `invoice generate` and `invoice create`
- Global invoice numbering (single sequence across all entities)
- 33 new tests covering config parsing, entity resolution, work log parsing, invoice generation grouping, A/R postings, and CLI flags
- All 149 existing invoicing tests still pass (backward compatibility confirmed)

**Files modified:**
- `src/zorg/skills/invoicing.py` — Added `key`/`ar_account`/`bank_account`/`currency` to `CompanyConfig`, `entity` to `ClientConfig` and `WorkEntry`, `companies`/`default_entity` to `InvoicingConfig`. New resolution helpers: `resolve_entity()`, `resolve_ar_account()`, `resolve_bank_account()`, `resolve_currency()`. Updated parser, work log, generation, and A/R posting.
- `src/zorg/skills/accounting.py` — Added `--entity/-e` flag to `invoice generate` and `invoice create`. Entity validation and resolution in `cmd_invoice_create`.
- `src/zorg/storage.py` — Updated `INVOICING_TEMPLATE` with multi-entity config examples
- `tests/test_skills_invoicing.py` — 33 new tests in 6 classes: `TestMultiEntityConfigParsing`, `TestEntityResolution`, `TestWorkLogEntityField`, `TestMultiEntityInvoiceGeneration`, `TestMultiEntityArPosting`, `TestMultiEntityCLI`
- `CLAUDE.md` — Updated invoicing section with multi-entity documentation

---

## 2026-02-04: Invoicing System

Added a full invoicing system for config-driven invoice generation with PDF export and beancount A/R integration. The system reads config from `INVOICING.md` (markdown with TOML), tracks billable work in `_INVOICES.md`, and generates professional PDF invoices via WeasyPrint. All invoice generation is deterministic — no data sent to Claude.

**Key changes:**
- New `invoicing.py` module with config parsing, work log parsing, invoice generation, HTML/PDF export, and beancount posting creation
- CLI commands: `invoice generate`, `invoice list`, `invoice paid`, `invoice create` (added to existing accounting skill)
- Service billing types: `hours`, `days`, `flat`, `other` (for expenses/reimbursements)
- Per-item discount field with conditional display column on invoices
- Client bundle rules: group related services into one invoice, or separate specific services
- Configurable beancount account names (`income_account` per service, `ar_account` per client, `default_ar_account`/`default_bank_account`/`currency` at top level)
- Default A/R account is `Assets:Accounts-Receivable` (used directly, no client suffix appended)
- String payment terms support (e.g., "On receipt") alongside numeric day counts
- Postings append directly to main ledger file via `LEDGER_PATH` env var
- Auto-creates `INVOICING.md` in user workspace (alongside TASKS.md, BRIEFINGS.md, HEARTBEAT.md)
- Auto-creates work log file from template when missing
- Fixed pre-existing `resource_name` → `display_name` bug in executor.py for ledger resources

**Files added:**
- `src/zorg/skills/invoicing.py` — Core invoicing module (~780 lines) with dataclasses, parsing, HTML/PDF generation, beancount integration
- `tests/test_skills_invoicing.py` — 78 tests covering config parsing, work log, bundling, line items, HTML output, A/R postings, CLI commands

**Files modified:**
- `src/zorg/skills/accounting.py` — Added `invoice` subcommand with `generate`/`list`/`paid`/`create` subparsers
- `src/zorg/executor.py` — Added `INVOICING_CONFIG` and `NEXTCLOUD_MOUNT_PATH` env vars, auto-creation logic, fixed `resource_name` bug
- `src/zorg/storage.py` — Added `INVOICING_TEMPLATE`, `get_user_invoicing_path()`, creation in `ensure_user_directories_v2`
- `config/skills/accounting.md` — Full rewrite with invoicing documentation, config format, work log format, CLI examples
- `config/skills/_index.toml` — Registered invoicing resource type and keywords
- `config/users/alice.example.toml` — Added invoicing resource example
- `pyproject.toml` — Added `weasyprint>=62.0` dependency
- Ansible role: `tasks/main.yml` (WeasyPrint system deps), `defaults/main.yml` (invoicing resource example)

---

## 2026-02-04: Graceful API Error Handling

Implemented user-friendly error handling for Anthropic API failures. Users now see personality-infused messages instead of raw JSON error payloads. Transient errors (5xx, rate limits) are automatically retried before counting against task attempts.

**Key changes:**
- Auto-retry transient API errors (500, 502, 503, 504, 529, 429) up to 3 times with 5-second delays
- User-friendly error messages with Zorg personality (e.g., "Lost contact with the mothership...")
- Email errors are silently logged — users don't receive confusing error emails
- Full error details (including request_id) preserved in logs and DB for debugging

**Error messages now match the Culture Drone persona:**
- 5xx: "Lost contact with the mothership. Anthropic's having a moment — try again shortly."
- 429: "Being throttled by the mothership. Apparently I'm too chatty. Give it a minute."
- Auth: "Can't authenticate with Anthropic — I've been locked out of my own brain."
- OOM: "Ran out of memory — tried to hold too much in my head at once."
- Timeout: "Got lost in thought and timed out."

**Files added:**
- `tests/test_executor.py` — 30 tests for API error parsing and retry logic

**Files modified:**
- `src/zorg/executor.py` — Added `parse_api_error()`, `is_transient_api_error()`, retry wrapper
- `src/zorg/scheduler.py` — Added `_format_error_for_user()` with personality
- `tests/test_scheduler.py` — Added 12 tests for error formatting

---

## 2026-02-04: OCR Transcription Skill

Added an OCR skill that runs Tesseract and returns structured text. Claude Code (which already has vision) can use this as a complementary data source for images with text content.

**Key changes:**
- New `transcribe` skill with `ocr` command for text extraction from images
- Returns text, confidence score (0-1), and word count
- Optional `--preprocess` flag applies grayscale + contrast enhancement for low-quality images
- Skill doc teaches reconciliation: trust OCR for exact characters, trust vision for context
- Keyword-triggered loading: "transcribe", "ocr", "screenshot", "text in image", "handwriting"

**CLI usage:**
```bash
python -m zorg.skills.transcribe ocr /path/to/image.png
python -m zorg.skills.transcribe ocr /path/to/image.png --preprocess
```

**Files added:**
- `src/zorg/skills/transcribe.py` — CLI skill with Tesseract OCR wrapper
- `config/skills/transcribe.md` — Skill documentation with reconciliation guidelines
- `tests/test_skills_transcribe.py` — 18 unit tests

**Files modified:**
- `config/skills/_index.toml` — Registered transcribe skill with keywords
- `pyproject.toml` — Added `pytesseract>=0.3.10` dependency

---

## 2026-02-04: Accounting Skill - Add Transaction Command

Added `add-transaction` CLI command to the accounting skill for deterministic transaction entry, preventing hallucination risks when Claude handles financial data.

**Key changes:**
- New `add-transaction` command for single transaction entry with validation
- Validates date format (YYYY-MM-DD), amount (positive numeric), escapes quotes
- Appends transactions to year-based files (e.g., `transactions/2026.beancount`)
- Runs bean-check validation after adding, returns errors if validation fails
- Updated skill documentation to emphasize CLI-first approach over direct file editing

**Design principle:**
Claude should orchestrate deterministic scripts, not do financial calculations:
- All number input → validated CLI commands
- All number output → bean-query results
- No manual file editing for amounts

**CLI usage:**
```bash
python -m zorg.skills.accounting add-transaction \
  --date 2026-02-04 \
  --payee "Whole Foods" \
  --narration "Weekly groceries" \
  --debit "Expenses:Food:Groceries" \
  --credit "Assets:Bank:Checking" \
  --amount 85.50
```

**Files modified:**
- `src/zorg/skills/accounting.py` — Added `cmd_add_transaction()`, parser entry, command dispatch
- `config/skills/accounting.md` — Replaced "Direct Ledger Editing" with CLI-first guidance
- `tests/test_skills_accounting.py` — Added 9 new tests for add-transaction command

---

## 2026-02-03: Agent-Task Heartbeat Check Type

Added a new heartbeat check type `agent-task` that queues natural language prompts as tasks for Claude to process asynchronously. Includes "silent unless action taken" behavior to prevent alert fatigue for routine checks.

**Key changes:**
- New `agent-task` check type in heartbeat system that creates queued tasks
- Tasks run through normal executor workflow with priority 3
- `silent_unless_action` mode (default: true) only posts results if Claude prefixes response with `ACTION:`
- Duplicate prevention via `pending_task_id` tracking in heartbeat state
- Heartbeat state updated after task completion (not at queue time)
- Failure handling updates heartbeat state with error count

**Prompt injection for silent mode:**
- When `silent_unless_action = true`, prompt is wrapped with instructions to prefix response with `ACTION:` (made changes) or `NO_ACTION:` (nothing needed)
- Scheduler strips prefix before posting, suppresses `NO_ACTION:` results entirely

**Files modified:**
- `schema.sql` — Added `heartbeat_check_name`, `heartbeat_silent` to tasks table; `pending_task_id` to heartbeat_state
- `src/zorg/db.py` — Added new columns to Task dataclass, create_task(), _row_to_task(), all SELECT queries; added pending_task_id support to HeartbeatState
- `src/zorg/heartbeat.py` — Added `_check_agent_task()` handler, updated `check_heartbeats()` for special handling
- `src/zorg/scheduler.py` — Handle heartbeat task results: strip ACTION:/NO_ACTION: prefixes, update heartbeat state, clear pending_task_id
- `config/skills/heartbeat.md` — Documented agent-task check type with examples
- `tests/test_heartbeat.py` — 9 new tests (TestCheckAgentTask, TestCheckHeartbeatsAgentTask, pending_task_id)
- `tests/test_scheduler.py` — 6 new tests (TestProcessHeartbeatTask)

## 2026-02-02: Heartbeat Monitoring System

Added a periodic health check system that evaluates user-defined conditions and alerts when something needs attention. Checks run directly in the scheduler loop (no LLM involved) for lightweight, fast monitoring.

**Key changes:**
- New `heartbeat.py` module with config loading, check handlers, alerting, and quiet hours support
- Five check types: `file-watch`, `shell-command`, `url-health`, `calendar-conflicts`, `task-deadline`
- Cooldown system prevents alert fatigue (per-check or global `default_cooldown_minutes`)
- Quiet hours suppress alerts but checks still run; cross-midnight ranges supported
- State tracked in `heartbeat_state` table (last_check_at, last_alert_at, last_healthy_at, consecutive_errors)
- Template `HEARTBEAT.md` seeded in new user workspaces with examples and documentation
- Skill doc teaches Claude Code the HEARTBEAT.md format

**Files added:**
- `src/zorg/heartbeat.py` — Core module with config loading, check handlers, alerting
- `config/skills/heartbeat.md` — Skill documentation for Claude
- `tests/test_heartbeat.py` — 45 unit tests

**Files modified:**
- `schema.sql` — Added `heartbeat_state` table
- `src/zorg/db.py` — Added `HeartbeatState` dataclass, `get_heartbeat_state()`, `update_heartbeat_state()`
- `src/zorg/config.py` — Added `heartbeat_check_interval` to `SchedulerConfig`
- `src/zorg/scheduler.py` — Integrated heartbeat checking into daemon and single-run loops
- `src/zorg/storage.py` — Added `HEARTBEAT_TEMPLATE`, `_build_heartbeat_seed()`, workspace seeding, updated README
- `config/skills/_index.toml` — Registered heartbeat skill with keywords
- `CLAUDE.md` — Documented heartbeat system
- `~/Repos/ansible-server/roles/zorg/defaults/main.yml` — Added `zorg_scheduler_heartbeat_check_interval`
- `~/Repos/ansible-server/roles/zorg/templates/config.toml.j2` — Added heartbeat config line

## 2026-02-02: BRIEFINGS.md (Markdown with embedded TOML)

Changed user-facing briefing configuration from `BRIEFINGS.toml` to `BRIEFINGS.md` — a Markdown file with embedded TOML in a fenced code block. This makes the file more user-friendly by including documentation and component reference directly in the config file.

**Key changes:**
- Renamed `get_user_briefings_path()` return from `BRIEFINGS.toml` to `BRIEFINGS.md`
- New `BRIEFINGS_TEMPLATE` includes description, component reference docs, and TOML code block
- Loader uses regex to extract first ` ```toml...``` ` block, parses with `tomli.loads()`
- Files with no TOML block return empty list (falls back to admin config)
- Updated skill doc with new format and example

**Files modified:**
- `src/zorg/storage.py` — Path, template, and seeding for BRIEFINGS.md
- `src/zorg/briefing_loader.py` — Regex extraction from Markdown, text-mode parsing
- `config/skills/briefings-config.md` — Updated format documentation
- `tests/test_briefing_loader.py` — New tests for Markdown wrapper, no-TOML-block cases
- `tests/test_storage.py` — Updated path assertions
- `CLAUDE.md`, `README.md` — Updated references
- `config/config.example.toml`, `config/users/alice.example.toml` — Updated comments

## 2026-02-02: !command dispatch system

Added a `!command` dispatch system that intercepts `!`-prefixed messages in the Talk poller before they enter the task queue. Simple deterministic operations (`!help`, `!stop`, `!status`, `!memory`) now execute immediately without spinning up a Claude Code agent session.

**Key changes:**
- Created `commands.py` with decorator-based command registry, parser, and async dispatcher
- `!help` lists all registered commands with descriptions
- `!stop` cancels user's active task via DB flag + SIGTERM to stored PID
- `!status` shows user's active/pending tasks and global system stats
- `!memory user` / `!memory channel` displays full memory file contents, split across multiple Talk messages if needed
- Talk poller intercepts `!` messages after user validation but before confirmation check and task creation
- Added `cancel_requested` and `worker_pid` columns to tasks table (idempotent migration)
- Executor stores subprocess PID and checks cancellation flag on each stream event
- Scheduler handles "Cancelled by user" result without retry

**Files added:**
- `src/zorg/commands.py` — Command registry, parser, dispatcher, and 4 command handlers
- `tests/test_commands.py` — 33 tests (parser, dispatch, all commands, DB helpers, poller interception)

**Files modified:**
- `src/zorg/talk_poller.py` — 6-line insertion for command dispatch before task creation
- `src/zorg/db.py` — Migration columns (`cancel_requested`, `worker_pid`), `update_task_pid()`, `is_task_cancelled()`
- `src/zorg/executor.py` — PID storage after Popen, cancellation check in streaming loop
- `src/zorg/scheduler.py` — Cancellation handling in `process_one_task()` (no retry for cancelled tasks)

## 2026-02-02: Per-user concurrent task queues

Converted Zorg's single-worker FIFO queue into per-user concurrent queues using threading. Users never block each other; tasks within a single user's queue run serially. The main scheduler loop now dispatches worker threads instead of processing tasks directly.

**Key changes:**
- Added `UserWorker` daemon thread class that processes tasks serially for one user, exits after idle timeout
- Added `WorkerPool` class that manages per-user workers with thread-safe dispatch and concurrency cap
- `run_daemon()` now creates a `WorkerPool` and calls `pool.dispatch()` each loop iteration instead of calling `process_one_task()` directly
- `claim_task()` accepts optional `user_id` parameter to only claim tasks for a specific user
- Added `get_users_with_pending_tasks()` DB function for efficient worker dispatch
- `get_worker_id()` now generates user-scoped IDs (`hostname-pid-user_id`) for per-user workers
- `process_one_task()` accepts optional `user_id` parameter passed through to claiming
- `run_scheduler()` one-shot mode unchanged (stays single-threaded)
- All periodic checks (briefings, email, TASKS.md, cleanup, etc.) remain in main loop unchanged
- Added `max_total_workers` (default 5) and `worker_idle_timeout` (default 30s) config fields

**Thread safety notes:**
- `get_db()` creates fresh connections per call — safe for concurrent threads
- WAL mode + 30s timeout handles concurrent writers
- `claim_task()` atomic UPDATE...RETURNING prevents double-claiming
- `asyncio.run()` in workers creates new event loop per call — safe from threads

**Files modified:**
- `src/zorg/config.py` - Added `max_total_workers` and `worker_idle_timeout` to `SchedulerConfig`
- `src/zorg/db.py` - Added `user_id` param to `claim_task()`, added `get_users_with_pending_tasks()`
- `src/zorg/scheduler.py` - Added `UserWorker`, `WorkerPool` classes; updated `get_worker_id()`, `process_one_task()`, `run_daemon()`
- `config/config.example.toml` - Added worker pool config examples
- `tests/test_db.py` - Added `TestClaimTaskUserFilter` (5 tests), `TestGetUsersWithPendingTasks` (4 tests)
- `tests/test_scheduler.py` - Added `TestGetWorkerId` (3 tests), `TestWorkerPool` (4 tests)
- `~/Repos/ansible-server/roles/zorg/defaults/main.yml` - Added worker pool defaults
- `~/Repos/ansible-server/roles/zorg/templates/config.toml.j2` - Added worker pool template lines

## 2026-02-02: Fix email header newline injection

Fixed email delivery failures caused by newlines in header fields. The initial fix only sanitized Subject, but the real culprit was `In-Reply-To` and `References` headers from the original email containing RFC 5322 folded newlines (long headers get wrapped with CRLF+whitespace by mail servers). When imap-tools reads these back, the folding is preserved, and Python's `EmailMessage` rejects them.

**Key changes:**
- Added `_sanitize_header()` helper that strips `\r` and `\n` from header values
- Applied to Subject in `send_email()` and `reply_to_email()`
- Applied to `In-Reply-To` and `References` headers in `reply_to_email()` — this was the actual bug causing task 644's failure
- Changed `post_result_to_email()` to return `bool` so failed email delivery marks the task as `failed` instead of `completed`
- Added 9 new tests: header sanitization (5), subject sanitization (2), threading header sanitization (1), email failure marks task failed (1)

**Files modified:**
- `src/zorg/skills/email.py` - Added `_sanitize_header()`, applied to Subject, In-Reply-To, and References
- `src/zorg/scheduler.py` - `post_result_to_email()` returns bool, `process_one_task()` handles failure
- `tests/test_skills_email.py` - Added sanitization tests for all affected headers
- `tests/test_scheduler.py` - Added `test_email_send_failure_marks_task_failed`

## 2026-02-02: Refactor reminders_file to use resources system

Replaced the `reminders_file` string field on `UserConfig` with a `reminders_file` resource type, matching how `todo_file` works. This standardizes all file-based user config under the resources system.

**Key changes:**
- Removed `reminders_file: str` field from `UserConfig`, replaced with `ResourceConfig(type="reminders_file", ...)`
- Added backward-compat migration in config parsing: legacy `reminders_file` TOML key auto-creates a resource
- Changed `_fetch_random_reminder()` to look up resources (like `_fetch_todo_items`), supports multiple reminders files
- Added `reminders_file` resource rendering in executor prompts
- Updated Ansible template to emit `reminders_file` as a `[[resources]]` block

**Files modified:**
- `src/zorg/config.py` - Removed field, added migration in `_parse_user_data()`, updated type comment
- `src/zorg/briefing.py` - Rewritten `_fetch_random_reminder(config, user_id)` using resources lookup
- `src/zorg/executor.py` - Added `reminders_file` resource type rendering block
- `config/users/alice.example.toml` - Replaced string with `[[resources]]` block
- `config/config.example.toml` - Replaced string with `[[users.alice.resources]]` block
- `tests/test_briefing.py` - Updated reminder tests to use resource-based config
- `tests/test_config.py` - Replaced assertion with backward-compat migration test
- `tests/conftest.py` - Removed `reminders_file` from fixture defaults
- `CLAUDE.md` - Added `reminders_file` to resource types list
- `~/Repos/ansible-server/roles/zorg/templates/user.toml.j2` - Emit as resource block
- `~/Repos/ansible-server/roles/zorg/defaults/main.yml` - Updated example comment

## 2026-02-02: Briefing Bug Fixes + Memory Isolation

Fixed three bugs preventing briefings from firing and leaking private data into briefing output.

**Key changes:**
- Fixed empty/commented-out BRIEFINGS.toml suppressing admin briefings — empty `[]` was truthy for `is not None` check, blocking fallback to admin config
- Fixed email-only briefings silently skipped — `conversation_token` was required for all briefings, but email output doesn't need a Talk room
- Excluded user memory (USER.md) from briefing prompts to prevent private context (portfolio positions, personal decisions) from leaking into newsletter-style output
- Stopped auto-loading dated memories into all prompts — they remain stored at `/Users/{user_id}/memories/` for Claude to read on demand, avoiding prompt bloat

**Files modified:**
- `src/zorg/briefing_loader.py` - Changed `is not None` to truthy check for workspace briefings fallback
- `src/zorg/scheduler.py` - Only require `conversation_token` for talk/both output targets
- `src/zorg/executor.py` - Skip user memory and dated memories for briefing tasks; removed dated memory auto-loading entirely
- `tests/test_briefing_loader.py` - Updated empty file test, renamed test for clarity
- `tests/test_scheduler.py` - Added email briefing without token test, renamed token skip test
- `tests/test_executor_streaming.py` - Added briefing memory exclusion tests, removed dated memory auto-load tests

## 2026-02-02: Hybrid Context Selection + Prompt Observability

Overhauled conversation context selection to use a hybrid approach: recent messages are always included without a model call, while older messages are triaged by Haiku. Added prompt size logging with per-component breakdown, configurable response truncation, and improved log formatting.

**Key changes:**
- Hybrid context selection: `always_include_recent` (default 5) messages guaranteed, older messages triaged by selection model
- Added `use_selection` config option to disable LLM selection entirely (includes all lookback messages)
- Added `context_truncation` config option to control bot response truncation in context (0 = disabled)
- Added `always_include_recent` config option for hybrid triage threshold
- Increased default `lookback_count` from 10 to 25 and disabled truncation by default
- Prompt size breakdown logged at INFO level: total, context, memory, skills, other
- Fixed JSON parsing for context selection when model returns explanation text before JSON
- Fixed log column alignment with fixed-width fields for level and logger name
- Prefixed scheduler startup logs with `STARTUP` for consistent formatting and grep filtering

**Files added/modified:**
- `src/zorg/context.py` - Hybrid selection with `_triage_older_messages()`, robust JSON extraction
- `src/zorg/config.py` - Added `use_selection`, `always_include_recent`, `context_truncation` to ConversationConfig
- `src/zorg/executor.py` - Prompt size breakdown logging, pass truncation config to formatter
- `src/zorg/logging_setup.py` - Fixed-width `%(levelname)-5s` and `%(name)-28s` in all log formats
- `src/zorg/scheduler.py` - `STARTUP` prefix on daemon startup log lines
- `config/config.example.toml` - New conversation config options documented
- `tests/test_context.py` - Rewritten for hybrid selection: 22 tests covering triage, fallbacks, edge cases

## 2026-02-01: Bias Context Selection Toward Inclusion

Updated the conversation context selection prompt to err on the side of including more potentially relevant messages rather than excluding them. The previous rules were conservative ("only include messages that directly help"), which could cause the model to drop messages that provided useful background context.

**Key changes:**
- Added "when in doubt, INCLUDE" as the primary selection rule
- Broadened inclusion criteria: messages that "could help or provide background" instead of only "directly help"
- Added rule to include messages about ongoing topics even if not directly referenced
- Narrowed exclusion to only "clearly unrelated" messages (different topic, fully resolved, trivial small talk)

**Files modified:**
- `src/zorg/context.py` - Updated selection prompt rules to favor inclusion over exclusion

## 2026-02-01: Workspace Briefings + Nextcloud User Metadata Hydration

Two new features: user-editable briefing schedules via workspace BRIEFINGS.toml, and automatic user metadata enrichment from the Nextcloud API.

**Key changes:**
- Users can create `BRIEFINGS.toml` in their workspace to control briefing schedules, delivery, and components
- Workspace briefings override admin config at the briefing name level (merge by name)
- Added `[briefing_defaults]` admin config section for shared market tickers and news sources
- Boolean component values (`markets = true`) expand using admin defaults; dict values pass through unchanged
- New `briefing_loader.py` module handles workspace loading, boolean expansion, and precedence merging
- New `nextcloud_api.py` module hydrates user configs from Nextcloud OCS API at scheduler startup
- API fills in display_name (if empty/matches user_id), email (appended if not present), timezone (if default UTC)
- Config values always take precedence over API — hydration only fills gaps
- Graceful degradation: API failures silently logged and skipped
- New `briefings-config.md` skill teaches Claude Code the BRIEFINGS.toml format
- 28 new tests (18 briefing loader, 10 nextcloud API)

**Files added:**
- `src/zorg/briefing_loader.py` — Workspace BRIEFINGS.toml loading, boolean expansion, merging
- `src/zorg/nextcloud_api.py` — Nextcloud OCS API user info, timezone, hydration
- `config/skills/briefings-config.md` — Skill doc for BRIEFINGS.toml format
- `tests/test_briefing_loader.py` — 18 tests for parsing, expansion, merging
- `tests/test_nextcloud_api.py` — 10 tests for API calls, hydration, graceful degradation

**Files modified:**
- `src/zorg/config.py` — Added `BriefingDefaultsConfig` dataclass and `briefing_defaults` field
- `src/zorg/storage.py` — Added `get_user_briefings_path()`, updated WORKSPACE_README
- `src/zorg/scheduler.py` — Uses `get_briefings_for_user()` in check_briefings(), hydration calls in run_daemon/run_scheduler
- `config/skills/_index.toml` — Registered `briefings-config` skill with keywords
- `config/config.example.toml` — Added `[briefing_defaults]` section
- `config/users/alice.example.toml` — Note about workspace BRIEFINGS.toml alternative
- `CLAUDE.md` — Documented workspace briefings, defaults, precedence, hydration, updated structure
- `~/Repos/ansible-server/roles/zorg/defaults/main.yml` — Added `zorg_briefing_defaults` variable
- `~/Repos/ansible-server/roles/zorg/templates/config.toml.j2` — Added `[briefing_defaults]` section

## 2026-02-01: Direct Email Sending from Claude Code via CLI

Added a `python -m zorg.skills.email send` CLI command so Claude Code can send emails directly during execution, instead of outputting JSON that the scheduler routes. This fixes the problem where a Talk user asking "email me X" would see raw JSON in the chat instead of receiving an email.

**Key changes:**
- Added CLI entry point to `email.py` with `send` subcommand (`--to`, `--subject`, `--body`, `--body-file`, `--html`)
- Config built from env vars (`SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, `SMTP_FROM`, plus IMAP vars for save-to-sent)
- Executor passes email credentials as env vars when `config.email.enabled`
- Email skill doc rewritten: CLI for direct sending from any channel, JSON output format retained for email-reply tasks only
- Added `source_types = ["email"]` to `_index.toml` so the skill also loads for email-source tasks
- Documented the CLI pattern for skills in CLAUDE.md (same pattern as `browse.py`)

**Files modified:**
- `src/zorg/skills/email.py` — Added `_config_from_env()`, `cmd_send()`, `build_parser()`, `main()`, `__main__` block
- `src/zorg/executor.py` — Added SMTP/IMAP env vars to subprocess environment
- `config/skills/email.md` — Rewritten with CLI send section + email-reply JSON section
- `config/skills/_index.toml` — Added `source_types = ["email"]`
- `tests/test_skills_email.py` — Added 10 tests for CLI (env config, send command, main, error handling)
- `CLAUDE.md` — Updated email skill description, added skill CLI pattern note

## 2026-01-31: Briefing Polish — Weekend Skip, News/Market Split, Quote Formatting

Series of improvements to briefing generation: weekend market quote skip, LLM-driven news/market story sorting, source attribution, yfinance quote authority, cleaner quote formatting with color indicators, and enforced section ordering.

**Key changes:**
- Market quote fetching skipped on Saturdays and Sundays (newsletters still fetched)
- Newsletter content fetched once; prompt instructs Claude to sort stories by topic into NEWS (politics, world events, policy) vs MARKETS (earnings, central bank, commodities, economic data)
- Source attribution (`[Semafor, NYT]`) added to each story paragraph, derived from newsletter sender headers
- Story count targets: 5 general news stories, 3 market stories
- yfinance quotes are now explicitly marked as authoritative — prompt tells Claude not to substitute prices from newsletters
- Removed yfinance ticker symbols from quote display (no more `(ES=F)` or `(^GSPC)`)
- Added colored circle emoji prefixes: 🟢 up, 🔴 down, ⚪ flat
- Enforced section ordering: prompt now instructs "Output sections in the exact order shown in the briefing skill"

**Files modified:**
- `src/zorg/briefing.py` — Weekend detection, newsletter split, yfinance authority instruction, section order enforcement
- `src/zorg/skills/markets.py` — Removed ticker from `format_quote()`, added color emoji prefix
- `config/skills/briefing.md` — Section descriptions, story targets, source attribution, updated quote examples
- `tests/test_briefing.py` — Weekend skip tests, newsletter split tests, fixed weekday-dependent test

## 2026-01-31: Web Browsing Skill via Dockerized Playwright

Added a headless browser skill backed by a Dockerized Playwright container with stealth anti-fingerprinting and VNC captcha fallback. Claude Code calls a thin Python CLI client that talks to a Flask API inside the container. For captcha-protected sites, the user gets a noVNC link to solve the challenge manually.

**Key changes:**
- Docker container with Playwright + Chromium, playwright-stealth, Xvfb/x11vnc/noVNC
- Flask API with endpoints: `/browse`, `/screenshot`, `/extract`, `/interact`, `/sessions`, `/health`
- Session management with 10-minute auto-expiry for multi-step browsing workflows
- Captcha detection heuristics (Cloudflare, reCAPTCHA, hCaptcha, generic patterns)
- Python CLI client (`python -m zorg.skills.browse`) with get/screenshot/extract/interact/close commands
- `BrowserConfig` dataclass in config.py with `enabled`, `api_url`, `vnc_url` fields
- Executor passes `BROWSER_API_URL` and `BROWSER_VNC_URL` env vars to Claude Code subprocess
- Browser tool listed in prompt's "Available tools" section when enabled
- Skill document with usage examples and captcha handling instructions
- Keyword-triggered loading: "browse", "website", "web page", "scrape", "screenshot", "url", etc.
- Ansible deployment: conditional docker compose build/start, VNC password, external URL config

**Files added:**
- `docker/browser/Dockerfile` — Playwright container with Xvfb + VNC stack
- `docker/browser/browse_api.py` — Flask API with session management and captcha detection
- `docker/browser/entrypoint.sh` — Starts Xvfb, x11vnc, noVNC proxy, Flask
- `docker/browser/requirements.txt` — playwright, playwright-stealth, flask
- `docker/docker-compose.browser.yml` — Standalone compose (API localhost:9223, noVNC :6080)
- `src/zorg/skills/browse.py` — CLI client to container API
- `config/skills/browse.md` — Skill document for Claude Code
- `tests/test_skills_browse.py` — 24 unit tests

**Files modified:**
- `config/skills/_index.toml` — Added `[browse]` entry with keyword triggers
- `src/zorg/config.py` — Added `BrowserConfig` dataclass and `[browser]` parsing
- `src/zorg/executor.py` — Browser env vars and prompt integration
- `~/Repos/ansible-server/roles/zorg/defaults/main.yml` — Added `zorg_browser_*` variables
- `~/Repos/ansible-server/roles/zorg/templates/config.toml.j2` — Added `[browser]` section
- `~/Repos/ansible-server/roles/zorg/tasks/main.yml` — Conditional Docker compose tasks

## 2026-01-30: Add Nextcloud OCS API skill

Added `nextcloud.md` skill teaching Claude Code how to use the Nextcloud OCS Share API for creating user shares, public links, listing/deleting shares, and looking up sharees. Credentials exposed as `NC_URL`, `NC_USER`, `NC_PASS` env vars in executor alongside existing CalDAV vars.

**Key changes:**
- New skill covering OCS Share API (create user share, public link, list/delete shares, sharee lookup)
- Curl examples using `$NC_URL`, `$NC_USER`, `$NC_PASS` env vars, consistent with CalDAV pattern
- Permission values reference (1=read, 2=update, 4=create, 8=delete, 16=share, 31=all)
- Keyword-triggered loading: "share", "sharing", "public link", "unshare", "nextcloud", "permission", "access"

**Files added/modified:**
- `config/skills/nextcloud.md` — New skill with curl examples for OCS Share API
- `config/skills/_index.toml` — Added nextcloud skill entry with keyword triggers
- `src/zorg/executor.py` — Added NC_URL/NC_USER/NC_PASS to execution environment
- `tests/test_skills_loader.py` — Added nextcloud to test index and 2 keyword match tests
- `CLAUDE.md` — Added nextcloud.md to project structure and available skills list

## 2026-01-30: Move USER.md and TASKS.md into workspace/

Moved the user-facing files (USER.md, TASKS.md) from the user root directory into `workspace/`, so everything the user interacts with lives in the single shared folder. The workspace directory is the only folder auto-shared with the user via OCS, making it a single pane of glass for user interaction.

**Key changes:**
- `get_user_memory_path()` now returns `/Users/{id}/workspace/USER.md`
- `get_user_tasks_file_path()` now returns `/Users/{id}/workspace/TASKS.md`
- Added `_migrate_workspace_files()` migration: moves USER.md and TASKS.md from root to workspace/ if not already migrated
- `discover_tasks_files()` now scans `workspace/` instead of user root
- Updated `WORKSPACE_README` to describe all workspace files (USER.md, TASKS.md, other content)
- All path references updated across codebase, docs, config examples, and Ansible role

**Files modified:**
- `src/zorg/storage.py` — Updated path helpers, added migration, rewrote WORKSPACE_README
- `src/zorg/tasks_file_poller.py` — Scan workspace/ for TASKS.md discovery
- `src/zorg/cli.py` — Updated user init output message
- `config/skills/memory.md` — Updated paths and directory structure diagram
- `config/users/alice.example.toml` — Updated TASKS.md resource path
- `CLAUDE.md` — Updated directory structure and prose references
- `README.md` — Updated user memory and TASKS.md path references
- `tests/test_storage.py` — Updated path assertions, added 6 migration tests (124 total)
- `tests/test_tasks_file_poller.py` — Updated all path references, verified workspace scanning
- `~/Repos/ansible-server/roles/zorg/defaults/main.yml` — Updated example TASKS.md path

## 2026-01-30: Rename notes/ to workspace/

Renamed the bot-managed `notes/` directory to `workspace/` and reframed it as a bidirectional collaboration folder where both the user and zorg have read/write access. Includes automatic migration from existing `notes/` directories.

**Key changes:**
- Renamed `get_user_notes_path()` → `get_user_workspace_path()` in storage.py
- Renamed `NOTES_README` → `WORKSPACE_README` with updated content explaining the shared collaboration space
- Added `_migrate_notes_to_workspace()` migration: renames `notes/` → `workspace/` if workspace doesn't exist yet
- Migrations now run before directory creation to avoid conflicts
- Updated all subdirs lists across rclone and mount code paths
- Updated Ansible example path for `reminders_file`

**Files modified:**
- `src/zorg/storage.py` — Renamed function/constant, added migration, updated subdirs
- `src/zorg/cli.py` — Updated user init output message
- `config/skills/memory.md` — Updated directory structure diagram
- `config/users/alice.example.toml` — Updated example paths
- `CLAUDE.md` — Updated directory structure and prose
- `tests/test_storage.py` — Updated all notes → workspace references, added 2 migration tests (71 total)
- `~/Repos/ansible-server/roles/zorg/defaults/main.yml` — Updated example reminders_file path

## 2026-01-30: Channel Directory Restructure & Prompt Metadata

Restructured channel directories to mirror the user directory pattern and added `conversation_token` to prompt metadata so the bot knows which room it's responding in. Also reduced rclone mount `--dir-cache-time` from 5m to 5s (server is localhost) so files shared in Talk appear on the mount almost immediately.

**Key changes:**
- Channel memory moved from `/Channels/{token}/context/memory.md` to `/Channels/{token}/CHANNEL.md`
- Added `/Channels/{token}/memories/` directory for future dated channel summaries
- Migration logic copies old `context/memory.md` → `CHANNEL.md` on first access
- Removed `get_channel_context_path()` helper (no longer needed)
- Added `get_channel_memories_path()` helper
- `conversation_token` now exposed in prompt metadata (shows `none` when not set)
- Reduced rclone `--dir-cache-time` from 5m to 5s for near-instant file visibility
- Removed deprecated `logs/` directory from Ansible zorg role

**Files modified:**
- `src/zorg/executor.py` — Added `Conversation token:` to prompt metadata
- `src/zorg/storage.py` — Restructured channel paths, added migration, removed context path helper
- `config/skills/memory.md` — Updated channel memory paths, added directory structure diagram
- `CLAUDE.md` — Updated channel directory structure and mount cache settings
- `tests/test_storage.py` — Updated channel tests, added migration tests (2 new)
- `tests/test_executor_streaming.py` — Added conversation_token prompt tests (2 new)
- Ansible `roles/rclone-mount/templates/mount-nextcloud.service.j2` — `--dir-cache-time 5s`
- Ansible `roles/zorg/tasks/main.yml` — Removed `logs/` directory creation
- Ansible `roles/zorg/README.md` — Removed `logs/` from directory structure

## 2026-01-30: Fix Slow First Response in New Talk Rooms

Fixed a bug where the first message in a newly discovered Talk room was skipped, causing a 10-20s delay before zorg responded. The first poll would save the latest message ID without processing it, so the user had to wait for the next poll cycle.

**Fix:** On first discovery of a group/public room, set `lastKnownMessageId` to `latest_id - 1` and fall through to the normal poll batch. This picks up the triggering message on the same cycle.

**Files modified:**
- `src/zorg/talk_poller.py` — Changed first-poll behavior to process the latest message immediately
- `tests/test_talk_poller.py` — Replaced 1 test with 3 tests covering the new behavior (28 → 30 tests)

## 2026-01-30: Sleep Cycle & Per-User Temp Dirs

Implemented the persistent memory system (sleep cycle) that extracts long-term memories from the day's interactions and writes dated memory files. Also added per-user temp directories as a prerequisite to isolate task artifacts.

**Key changes:**
- Per-user temp directories: task prompt/result files now go to `{temp_dir}/{user_id}/` instead of flat `{temp_dir}/`
- Nightly sleep cycle: configurable per-user cron job that gathers completed tasks, calls Claude CLI to extract memories, writes dated `YYYY-MM-DD.md` files to `/Users/{user}/context/`
- Dated memory files are auto-loaded into prompts as "Recent context (from previous days)" section
- Memory retention policy: old dated files automatically cleaned up after configurable days (default: 90)
- Sleep cycle uses direct subprocess (like `context.py`) instead of task queue to avoid competing with user tasks
- `NO_NEW_MEMORIES` sentinel allows Claude to skip writing when nothing worth saving
- Optional Talk notification when sleep cycle completes
- Updated `cleanup_old_temp_files()` to recursively handle per-user subdirectories
- Added sleep cycle state tracking in SQLite (`sleep_cycle_state` table)

**Files added:**
- `src/zorg/sleep_cycle.py` — Main module (gather, extract, process, cleanup, cron check)
- `tests/test_sleep_cycle.py` — 22 tests covering all functions

**Files modified:**
- `src/zorg/config.py` — Added `SleepCycleConfig` dataclass, `sleep_cycle` field on `UserConfig`
- `src/zorg/db.py` — Added sleep cycle state functions, `get_completed_tasks_since()`
- `src/zorg/executor.py` — `get_user_temp_dir()`, per-user temp dirs, dated memories in prompt
- `src/zorg/storage.py` — `read_dated_memories()`, `get_user_context_path()`
- `src/zorg/scheduler.py` — Sleep cycle check in daemon/single-run, recursive temp cleanup
- `schema.sql` — Added `sleep_cycle_state` table
- `config/skills/memory.md` — Documented dated memory files
- `config/config.example.toml` — Added sleep cycle example config
- `tests/test_config.py` — 7 new sleep cycle config tests
- `tests/test_db.py` — 10 new tests (state table, completed tasks query)
- `tests/test_storage.py` — 10 new tests (read_dated_memories)
- `tests/test_executor_streaming.py` — 4 new tests (per-user temp dirs, dated memories injection)
- `tests/conftest.py` — Added `sleep_cycle` to fixture defaults
- Ansible `templates/config.toml.j2` — Sleep cycle template block
- Ansible `defaults/main.yml` — Sleep cycle example in comments


## 2026-01-29: Comprehensive Test Suite

Built a full test suite covering all modules, growing from 4 test files (~65 tests) to 20 test files (~513 tests). Adopted test-driven development as the project's standard going forward.

**Key changes:**
- Created `tests/conftest.py` with shared fixtures: `db_path` (real SQLite via schema.sql), `make_task`, `make_config`, `make_user_config` factories
- Added unit tests for all untested modules: db, config, skills_loader, context, zorg_file_poller, email_poller, talk_poller, talk, storage, briefing, scheduler, shared_file_organizer, markets, skills/files, skills/email
- Added `test_talk_integration.py` with 22 integration tests that verify real Nextcloud Talk connectivity (authentication, message sending/polling, reply tracking, poll state continuity)
- Integration tests gated behind `@pytest.mark.integration` marker, skipped by default
- Added pytest configuration to `pyproject.toml`: `integration` marker, `addopts = "-m 'not integration'"`, `asyncio_mode = "auto"`
- Updated CLAUDE.md with Testing section documenting TDD workflow, test patterns, and commands
- Updated README.md Development section to note TDD approach
- Replaced CLAUDE.md "Verified Working" / "Needs Testing" sections with "Test Coverage" section listing all test files

**Files added:**
- `tests/conftest.py` — Shared fixtures
- `tests/test_db.py` — 80 tests (task lifecycle, stale handling, retry, confirmations, resources, history, cleanup)
- `tests/test_config.py` — 33 tests (defaults, TOML loading, methods, email config)
- `tests/test_skills_loader.py` — 18 tests (index parsing, skill selection, file loading)
- `tests/test_context.py` — 20 tests (context selection, prompt formatting)
- `tests/test_zorg_file_poller.py` — 44 tests (normalization, hashing, parsing, polling, completion)
- `tests/test_email_poller.py` — 23 tests (subject normalization, thread IDs, polling, cleanup)
- `tests/test_talk_poller.py` — 26 tests (attachments, message cleaning, confirmations, polling)
- `tests/test_talk.py` — 11 tests (TalkClient methods, message truncation)
- `tests/test_storage.py` — 27 tests (path helpers, mount ops, rclone ops)
- `tests/test_briefing.py` — 30 tests (HTML stripping, reminders, market data, prompt building)
- `tests/test_scheduler.py` — 45 tests (confirmation pattern, email parsing, briefings, jobs, task processing)
- `tests/test_shared_file_organizer.py` — 15 tests (file owner detection, discovery/organization)
- `tests/test_markets.py` — 17 tests (quotes, formatting, market summary)
- `tests/test_skills_files.py` — 22 tests (mount and rclone file operations)
- `tests/test_skills_email.py` — 15 tests (IMAP/SMTP operations, date parsing)
- `tests/test_talk_integration.py` — 22 integration tests (real Nextcloud Talk API)

**Files modified:**
- `pyproject.toml` — Added pytest marker config, asyncio_mode
- `CLAUDE.md` — Added Testing section with TDD workflow, replaced Verified/Needs Testing with Test Coverage
- `README.md` — Updated Development section with TDD and test commands

---

## 2026-01-29: Real-Time Progress Updates During Task Execution

Switched from `subprocess.run` to `subprocess.Popen` with `--output-format stream-json` to parse Claude Code's streaming events in real time. For Talk tasks, meaningful progress messages (tool use descriptions like "Reading TODO.txt", "Running script...") are now sent to the chat during execution, so users can see what the bot is doing instead of waiting in silence.

**Key changes:**
- New `stream_parser.py` module that parses stream-json lines into typed events (`ToolUseEvent`, `TextEvent`, `ResultEvent`). Extracts human-readable descriptions from tool use blocks (Bash descriptions, Read/Write filenames, Grep patterns, etc.).
- Rewrote `executor.py` execution to use `Popen` with line-by-line stdout parsing. Added `on_progress` callback parameter. Uses `threading.Timer` for 10-minute timeout. Falls back to result file or raw stdout if stream parsing fails.
- Added rate-limited progress callback in `scheduler.py` that posts italic progress messages to Talk with configurable debounce interval and max message count. Logs progress to `task_logs` for debugging.
- Added 5 new config fields: `progress_updates`, `progress_min_interval` (8s), `progress_max_messages` (5), `progress_show_tool_use`, `progress_show_text` (off by default).
- Added `pytest` as dev dependency and wrote 52 unit tests covering stream parser, executor streaming, progress callback, and config parsing.
- Updated Ansible deployment role (`defaults/main.yml`, `templates/config.toml.j2`) with new progress config variables.
- Added note to CLAUDE.md about keeping Ansible role in sync after config changes.

**Files added:**
- `src/zorg/stream_parser.py` - Stream-json event parser with tool description extraction
- `tests/__init__.py` - Test package init
- `tests/test_stream_parser.py` - 31 tests for parser and tool descriptions
- `tests/test_executor_streaming.py` - 11 tests for Popen streaming, callbacks, timeout, fallbacks
- `tests/test_progress_callback.py` - 6 tests for rate-limiting, max messages, exception handling
- `tests/test_config_progress.py` - 4 tests for config defaults and TOML parsing

**Files modified:**
- `src/zorg/executor.py` - Popen + stream-json parsing, on_progress callback
- `src/zorg/config.py` - Progress config fields in SchedulerConfig
- `src/zorg/scheduler.py` - `_make_talk_progress_callback()`, wired into `process_one_task`
- `config/config.example.toml` - Documented progress settings
- `CLAUDE.md` - Added Ansible deployment role sync reminder, updated structure
- `~/Repos/ansible-server/roles/zorg/defaults/main.yml` - Progress config variables
- `~/Repos/ansible-server/roles/zorg/templates/config.toml.j2` - Progress template lines

---

## 2026-01-29: Simplify Reminder Parsing for Briefings

Replaced the rigid line-by-line reminder parser with a block-based approach. The old parser split multi-line entries (long quotes with attributions, bullet points with explanations) into individual line fragments, meaning most selected "reminders" were incomplete. The new parser splits on blank lines so each block stays together as one selectable item.

**Key changes:**
- Rewrote `_parse_reminders()` to split content on blank lines into blocks instead of parsing individual lines. Headers and horizontal rules are filtered out; multi-line entries stay intact.
- Removed forced `*"Quote text."* —Author Name` formatting from briefing skill — reminders are now included as-is since most aren't short attributed quotes.
- Simplified prompt instruction from "formatted with quotes and attribution" to just "include this reminder at the end."
- Also updated briefing NEWS section spec to emphasize global perspective and paragraph format, and added market news summary instruction.

**Files modified:**
- `src/zorg/briefing.py` - Rewrote `_parse_reminders()` block-based parser, simplified prompt instruction
- `config/skills/briefing.md` - Flexible reminder formatting, updated NEWS/MARKETS section specs

---

## 2026-01-28: Fix DB Lock in Talk/Email Result Posting

Found the root cause of briefings not appearing in Talk despite tasks completing successfully. `post_result_to_talk()` and `post_result_to_email()` were called inside the `with db.get_db()` block that holds the write transaction. If the Talk/email post fails, the error handler opened a *second* DB connection to log the error, causing `sqlite3.OperationalError: database is locked`. This crashed the entire process, potentially leaving task status in an inconsistent state.

**Key changes:**
- Moved all Talk/email posting outside the `with db.get_db()` block in `process_one_task()`, using the same pattern already used for zorg_file handling
- Changed `post_result_to_talk()` error handling from nested DB write to `logger.error()`
- Changed `post_result_to_email()` error handling the same way
- Result delivery variables (`post_talk_message`, `post_email`) are set inside the DB transaction, then acted on after it closes

**Files modified:**
- `src/zorg/scheduler.py` - Move result posting outside DB context, fix nested DB connections in error handlers

---

## 2026-01-28: Improve Briefing Format and Fix Reminder Bug

Fixed reminder file reading broken on mount-based deployments, rewrote briefing formatting spec, and improved newsletter HTML stripping.

**Key changes:**
- Fixed `_fetch_random_reminder()` to use mount-aware `read_text(config, path)` instead of `rclone_read_text()` which silently fails when rclone CLI isn't configured
- Rewrote `config/skills/briefing.md` with section-by-section format spec using emoji headers (NEWS, MARKETS, CALENDAR, TODOS, NOTES, EMAIL, REMINDER)
- Simplified `config/guidelines/briefing.md` to high-level rules only (concise, time-sensitive first, local timezone, data-only sections)
- Added `<style>` tag removal and `@media` CSS block stripping to `_strip_html()` for cleaner newsletter content
- Added tracking pixel `<img>` removal (width/height 0 or 1)
- Updated briefing prompt builder: removed "### headings" instruction (conflicted with no-headings rule), removed market commentary instruction, aligned with emoji-prefixed section headers

**Files modified:**
- `src/zorg/briefing.py` - Fixed reminder reading, improved HTML stripping, updated prompt format instructions
- `config/skills/briefing.md` - Full rewrite with emoji-headed section format spec
- `config/guidelines/briefing.md` - Trimmed to four high-level rules

---

## 2026-01-28: Aggressive Stale Task Expiry

Stuck and old tasks now fail fast instead of being endlessly retried. Previously, `claim_task()` would blindly reset stuck running tasks to pending regardless of age, causing hour-old Talk messages and briefings to get retried and produce irrelevant responses.

**Key changes:**
- `claim_task()` now checks task age before deciding retry vs fail: tasks created less than `max_retry_age_minutes` ago (default: 60) get retried, older tasks are failed immediately
- Same age-based logic applied to both stuck running tasks and stale locked tasks
- Lowered `stale_pending_fail_hours` default from 24 to 2 (safety net for anything that slips through)
- New config option `max_retry_age_minutes` (default: 60) controls the retry age cutoff
- Updated Ansible role defaults and config template to match

**Files modified:**
- `src/zorg/config.py` - Added `max_retry_age_minutes`, changed `stale_pending_fail_hours` default to 2
- `src/zorg/db.py` - Split stuck-task recovery in `claim_task()` into age-based retry vs fail paths
- `src/zorg/scheduler.py` - Pass `max_retry_age_minutes` config to `claim_task()`
- `~/Repos/ansible-server/roles/zorg/defaults/main.yml` - Updated defaults
- `~/Repos/ansible-server/roles/zorg/templates/config.toml.j2` - Added `max_retry_age_minutes` line

---

## 2026-01-28: Structured JSON Email Output

Standardized Claude Code's email output as JSON with `subject`, `body`, and `format` keys. This gives Claude control over email subjects (especially for new/scheduled emails) and enables HTML formatting when appropriate.

**Key changes:**
- Added `_parse_email_output()` helper to scheduler that parses Claude's JSON output with backward-compatible fallback to raw text
- Updated `post_result_to_email()` to use parsed subject, body, and format for both reply and fresh email paths
- Added `content_type` parameter to `send_email()` and `reply_to_email()` in the email skill, using `msg.set_content(body, subtype=content_type)`
- Updated `email.md` skill to instruct Claude to output JSON with `subject`, `body`, `format` keys
- Updated `email.md` guidelines with sections for plain text and HTML formatting rules
- Updated executor prompt to reference JSON output format for email tasks

**JSON schema:**
```json
{
  "subject": "Optional subject line",
  "body": "The email content",
  "format": "plain"
}
```

**Backward compatibility:** If Claude outputs raw text instead of JSON, the parser falls back to `{subject: null, body: message, format: "plain"}`, matching previous behavior exactly.

**Files modified:**
- `src/zorg/scheduler.py` - Added `json` import, `_parse_email_output()` helper, updated `post_result_to_email()`
- `src/zorg/skills/email.py` - Added `content_type` parameter to `send_email()` and `reply_to_email()`
- `config/skills/email.md` - Added JSON output format instructions
- `config/guidelines/email.md` - Updated with plain text and HTML formatting sections
- `src/zorg/executor.py` - Updated available tools prompt to reference JSON email output

---

## 2026-01-28: DB-Driven Scheduled Jobs

Added runtime-manageable recurring cron jobs via a `scheduled_jobs` SQLite table. The scheduler evaluates cron expressions each loop and queues matching jobs as tasks. Zorg can create, modify, and delete scheduled jobs directly via sqlite3 commands — no code changes needed per new job.

**Key changes:**
- Added `scheduled_jobs` table with cron expression, prompt, optional conversation token, and per-user unique name constraint
- Added `ScheduledJob` dataclass and query functions (`get_enabled_scheduled_jobs`, `set_scheduled_job_last_run`) to db.py
- Added `check_scheduled_jobs()` to scheduler following the same pattern as `check_briefings()` (timezone-aware cron evaluation via croniter)
- Wired scheduled job checking into both single-run (`run_scheduler`) and daemon (`run_daemon`) modes
- Created `config/skills/schedules.md` skill teaching zorg to manage jobs via sqlite3 CLI
- Registered `[schedules]` in `_index.toml` with keyword matching for "schedule", "recurring", "cron", "daily", "weekly", etc.

**Files added/modified:**
- `schema.sql` - Added `scheduled_jobs` table with `UNIQUE(user_id, name)` constraint and user index
- `src/zorg/db.py` - Added `ScheduledJob` dataclass, `get_enabled_scheduled_jobs()`, `set_scheduled_job_last_run()`
- `src/zorg/scheduler.py` - Added `check_scheduled_jobs()`, wired into single-run and daemon loops
- `config/skills/schedules.md` - New skill with sqlite3 commands for CRUD on scheduled_jobs
- `config/skills/_index.toml` - Added `[schedules]` entry with keyword triggers

---

## 2026-01-28: Add Scripts Skill

Taught zorg to create and maintain reusable Python scripts in a persistent `Scripts/` directory on its Nextcloud mount. When zorg recognizes a recurring or automatable task, it can now proactively offer to script it rather than doing it by hand each time.

**Key changes:**
- Created `config/skills/scripts.md` skill file with guidelines for script creation, naming, style, and directory management
- Registered `[scripts]` in `config/skills/_index.toml` with `always_include = true` so zorg always knows scripting is an option
- Added "Think in scripts" line to `config/persona.md` proactive behavior section

**Files added/modified:**
- `config/skills/scripts.md` - New skill: scripts live at `/srv/mount/nextcloud/content/Scripts/`, Python functional style, standalone
- `config/skills/_index.toml` - Added `[scripts]` entry with `always_include = true`
- `config/persona.md` - One-line addition about recognizing scriptable patterns

---

## 2026-01-28: Fix Email Threading References Header

Fixed email threading by implementing proper RFC 5322 References header handling. Previously, zorg's replies only included the parent's Message-ID in the References header, breaking thread continuity in email clients when conversations extended beyond two messages.

**Problem:**
- User's second email had correct References chain: `<original-msg-id> <zorg-reply-1-msg-id>`
- Zorg's second reply had broken References: only `<user-msg-2-id>` instead of the full ancestry chain
- Email clients couldn't properly thread conversations beyond the first reply

**RFC 5322 requires:**
- `In-Reply-To`: Just the Message-ID of the message being replied to (single ID)
- `References`: The parent's References header + the parent's Message-ID (full ancestry chain)

**Key changes:**
- Added `references` field to `Email` dataclass and capture it from IMAP headers
- Added `references` column to `processed_emails` table for persistence
- Updated `mark_email_processed()` to accept and store References header
- Updated `get_email_for_task()` to return References with ProcessedEmail
- Build correct References chain when replying: `parent.references + parent.message_id`
- Quote `"references"` in SQL (reserved keyword in SQLite)

**Files modified:**
- `schema.sql` - Added `"references"` column to `processed_emails`
- `src/zorg/db.py` - Updated `ProcessedEmail` dataclass, `mark_email_processed()`, `get_email_for_task()`
- `src/zorg/skills/email.py` - Added `references` field to `Email`, capture in `read_email()`
- `src/zorg/email_poller.py` - Pass `references=email.references` when marking processed
- `src/zorg/scheduler.py` - Build RFC 5322 compliant References chain in `post_result_to_email()`

**Database migration:**
```sql
ALTER TABLE processed_emails ADD COLUMN "references" TEXT;
```

---

## 2026-01-27: Fix Context Selection to Always Include Most Recent Message

Fixed a bug where follow-up questions like "What's it about?" failed to connect to the previous message. The root cause was that the most recent message was subject to Sonnet's relevance judgment, which could incorrectly exclude it. Additionally, any error (timeout, parse failure) returned an empty list, leaving zorg with no context.

**Problem:**
- User asks about "The Machine Stops" in message 1
- User follows up with "What's it about?" in message 2
- Zorg explained "The Culture series" (from its persona) instead of the book mentioned in the previous message
- The most recent message could be excluded by Sonnet's selection or on any error

**Solution:**
- Most recent message is now ALWAYS included unconditionally
- Sonnet selection only runs on older messages in the history
- On any error (timeout, parse error, etc.), returns `[most_recent]` instead of `[]`
- Selection prompt updated to clarify it's selecting from OLDER messages only

**Key changes:**
- Extract `most_recent = history[-1]` before any selection logic
- Run Sonnet selection on `older_history = history[:-1]` only
- Return `selected_older + [most_recent]` on success (chronological order)
- Return `[most_recent]` on any error (preserves basic conversational continuity)
- Elevated context logging from DEBUG to INFO for better visibility

**Files modified:**
- `src/zorg/context.py` - Core logic change: always include most recent, select from older only
- `src/zorg/executor.py` - Elevated context skip/empty logging from DEBUG to INFO

---

## 2026-01-27: Replace Himalaya with Native Python Email

Replaced the himalaya Rust CLI tool with native Python email handling using `imap-tools` for IMAP and stdlib `smtplib` for SMTP. This eliminates external dependency issues, subprocess overhead, and parsing workarounds.

**Why the change:**
- Himalaya had issues parsing emails with emojis in subjects (required stderr parsing workaround)
- Inconsistent date formats in output
- Subprocess calls for every operation added overhead
- Missing encryption settings caused IMAP connection failures
- Config generation complexity

**Key changes:**
- Rewrote `src/zorg/skills/email.py` with imap-tools and smtplib
- Created `EmailConfig` dataclass for clean configuration passing
- Simplified attachment handling (direct download to target directory)
- Removed all emoji workaround code
- Deleted `src/zorg/email_setup.py` (himalaya config generation no longer needed)
- Updated briefing.py to pre-fetch full newsletter content (no CLI access needed)
- Added HTML stripping for newsletter emails (removes tags, invisible Unicode chars)
- Wrapped Talk progress messages in markdown italic formatting

**Email threading fix:**
- SMTP doesn't auto-save to Sent folder - added `_save_to_sent()` to append via IMAP
- Generate unique Message-ID for all outgoing emails
- Add Date header to all outgoing emails
- Threading headers (In-Reply-To, References) now properly preserved in Sent Items

**Newsletter HTML stripping:**
- Strips HTML tags while preserving structure (newlines for block elements)
- Removes invisible Unicode characters (nbsp, zero-width spaces, BOM, etc.)
- Handles Substack-style padding at top of emails
- Decodes HTML entities

**Files added/modified:**
- `src/zorg/skills/email.py` - Complete rewrite with imap-tools/smtplib, Sent folder save, Message-ID generation
- `src/zorg/email_poller.py` - Updated for new email module, added `get_email_config()`
- `src/zorg/scheduler.py` - Removed himalaya setup, updated reply sending, italic progress messages
- `src/zorg/briefing.py` - Pre-fetch newsletter content, HTML stripping
- `src/zorg/cli.py` - Removed setup/show-config commands
- `config/skills/email.md` - Updated documentation
- `pyproject.toml` - Added imap-tools dependency
- `CLAUDE.md` - Removed all himalaya references
- Deleted `src/zorg/email_setup.py`

---

## 2026-01-27: Simplify Skills for Mount-Only File Access

Updated all skills documentation to use the rclone mount exclusively. Removed references to `/tmp` temp files and rclone CLI commands since Nextcloud is now mounted as a local filesystem at `/srv/mount/nextcloud/content`.

**Key changes:**
- Simplified `files.md` to mount-only access (removed rclone CLI section)
- Updated `memory.md` to use direct filesystem writes instead of rclone commands
- Updated `todos.md` and `notes.md` to use mount paths
- Updated CLAUDE.md: removed dual rclone/mount docs, simplified to mount-only
- Removed temp file retention config (no longer using `/tmp/zorg`)
- Updated Talk attachment docs to reference direct mount access

**Files modified:**
- `config/skills/files.md` - Mount-only file operations
- `config/skills/memory.md` - Direct filesystem writes to memory file
- `config/skills/todos.md` - Mount path for TODO files
- `config/skills/notes.md` - Mount path for notes files
- `CLAUDE.md` - Simplified Nextcloud file access section, updated local testing

---

## 2026-01-27: Migrate Talk from Webhook to Polling Architecture

Completely replaced the webhook-based Talk integration with a polling-based architecture. Zorg now runs as a regular Nextcloud user (not a registered bot) and polls conversations directly via the Nextcloud Talk user API.

**Why the change:**
- Webhook-based bot API required registering a bot and running a FastAPI webhook server
- Polling allows zorg to run as a regular user with simpler deployment (no webhook server needed)
- Long-polling provides near-instant message detection
- Cleaner architecture: just run the scheduler daemon

**Key changes:**
- Deleted `main.py` (FastAPI webhook server no longer needed)
- Removed FastAPI, uvicorn dependencies from `pyproject.toml`
- Rewrote `talk.py` to use user API instead of bot API (room listing uses v4, chat uses v1)
- Created `talk_poller.py` module for conversation polling logic
- Added `talk_poll_state` table to track last seen message per conversation
- Polls all rooms concurrently using `asyncio.gather()` for instant detection
- First poll initializes state without processing historical messages

**API version discovery:**
- Room listing endpoint requires API v4: `/ocs/v2.php/apps/spreed/api/v4/room`
- Chat endpoints require API v1: `/ocs/v2.php/apps/spreed/api/v1/chat/{token}`
- Both require `Accept: application/json` header (defaults to XML)

**Configuration changes:**
```toml
[talk]
enabled = true
bot_username = "zorg"  # to filter own messages
# webhook_secret removed - no longer needed

[scheduler]
talk_poll_timeout = 30  # long-poll server timeout
```

**Deployment:**
- No webhook server needed - just run `uv run zorg-scheduler -d`
- Zorg user must be added as participant in Talk conversations
- Scheduler polls all rooms concurrently (instant detection regardless of room count)

**Files added/modified:**
- `src/zorg/talk_poller.py` - NEW: Polling logic with concurrent room monitoring
- `src/zorg/talk.py` - Rewritten for user API (v4 rooms, v1 chat, Accept headers)
- `src/zorg/db.py` - Added `get_talk_poll_state()`, `set_talk_poll_state()` functions
- `src/zorg/scheduler.py` - Integrated Talk polling into daemon loop
- `schema.sql` - Added `talk_poll_state` table
- `pyproject.toml` - Removed FastAPI, uvicorn dependencies
- Deleted `src/zorg/main.py` - Webhook server no longer needed

---

## 2026-01-27: Nextcloud Mount Support & Path Reorganization

Added support for mounting Nextcloud WebDAV as a local filesystem via rclone mount. This enables direct filesystem access without subprocess overhead for every file operation. Also reorganized the bot-managed directory structure from `/Zorg/users/` to `/Users/` for cleaner paths.

**Key changes:**
- Added `nextcloud_mount_path` config option for mount-based file access
- Created mount-aware wrapper functions in `skills/files.py` (list_files, read_text, write_text, etc.)
- Created mount-aware storage functions (`*_v2` versions) for user directories and memory
- Updated scheduler to use mount paths directly for Talk attachments when available
- Updated executor to show mount vs rclone instructions in prompts based on mode
- Reorganized directory paths from `/Zorg/users/{user}/` to `/Users/{user}/`

**Ansible changes (in ansible-server repo):**
- Created `roles/rclone-mount/tasks/nextcloud.yml` - Nextcloud mount setup tasks
- Created `roles/rclone-mount/templates/mount-nextcloud.service.j2` - systemd service
- Updated `roles/rclone-mount/tasks/main.yml` - Added conditional import for nextcloud
- Updated `roles/rclone-mount/defaults/main.yml` - Added nextcloud mount variables
- Updated `roles/zorg/defaults/main.yml` - Added `zorg_use_nextcloud_mount` option
- Updated `roles/zorg/tasks/main.yml` - Conditionally includes rclone-mount role
- Updated `roles/zorg/templates/config.toml.j2` - Added mount path config

**Configuration:**
```toml
# Option 1: rclone CLI (default)
rclone_remote = "nextcloud"

# Option 2: Local mount (faster, requires mount-nextcloud.service)
nextcloud_mount_path = "/srv/mount/nextcloud/content"
```

**New directory structure:**
```
/Users/{user_id}/
├── inbox/      # Files user wants bot to process
├── exports/    # Files bot generates for user
├── shared/     # Auto-organized files shared by user
└── context/
    └── memory.md   # Persistent memory file
```

**Files modified:**
- `src/zorg/config.py` - Added `nextcloud_mount_path` field and `use_mount` property
- `src/zorg/skills/files.py` - Added mount-aware wrapper functions
- `src/zorg/storage.py` - Added `*_v2` mount-aware functions, changed path from `/Zorg/users` to `/Users`
- `src/zorg/scheduler.py` - Mount-aware Talk attachment handling
- `src/zorg/shared_file_organizer.py` - Uses mount-aware file operations
- `src/zorg/zorg_file_poller.py` - Uses mount-aware file operations
- `src/zorg/executor.py` - Mount-aware storage functions, dynamic prompt generation
- `config/skills/files.md` - Documents both mount and rclone usage
- `config/skills/memory.md` - Updated paths
- `config/config.example.toml` - Documents mount path option
- `CLAUDE.md` - Added mount documentation, updated paths
- `README.md` - Updated paths

---

## 2026-01-27: Fix Memory File Multi-User Issue & Add Temp Cleanup

Removed the auto-sync approach for memory files because `/tmp/memory.md` is shared across all users - a single temp file can't work for a multi-user system. Memory writes now go directly to Nextcloud using rclone.

Also added automatic cleanup of old temp files since all permanent storage should be in Nextcloud.

**Key changes:**
- Removed `sync_user_memory()` function from storage.py
- Removed auto-sync code block from executor.py
- Updated memory.md skill with direct rclone instructions (one-liner and multi-step approaches)
- Added `temp_file_retention_days` config option (default: 7 days)
- Added `cleanup_old_temp_files()` function to scheduler
- Temp files older than retention period are automatically deleted

**Configuration:**
```toml
[scheduler]
temp_file_retention_days = 7  # Delete temp files older than N days, 0 to disable
```

**Files modified:**
- `src/zorg/executor.py` - Removed auto-sync code and import
- `src/zorg/storage.py` - Removed `sync_user_memory()` function
- `config/skills/memory.md` - Updated with direct rclone instructions
- `src/zorg/config.py` - Added `temp_file_retention_days` setting
- `src/zorg/scheduler.py` - Added `cleanup_old_temp_files()`, integrated into cleanup checks
- `CLAUDE.md` - Updated documentation

---

## 2026-01-27: Memory Auto-Sync After Task Completion (Reverted)

**Note:** This approach was reverted in the next commit due to multi-user issues.

Fixed issue where Claude Code would claim to update the user's memory file but changes weren't persisted to Nextcloud. The problem was that Claude Code would write to `/tmp/memory.md` but not always run the `rclone copy` command to sync back.

**Solution:**
- Executor now automatically checks for `/tmp/memory.md` after task completion
- If the file exists, it's synced to Nextcloud and then deleted locally
- This is a fail-safe that doesn't rely on Claude Code following instructions perfectly

**Key changes:**
- Added `sync_user_memory()` helper function to storage.py
- Executor auto-syncs memory file after Claude Code completes
- Updated memory.md skill to recommend writing to `/tmp/memory.md` (auto-synced)
- Simplified skill instructions with one-liner alternative

**Files modified:**
- `src/zorg/storage.py` - Added `sync_user_memory()` function
- `src/zorg/executor.py` - Added auto-sync logic after task execution
- `config/skills/memory.md` - Simplified instructions, mention auto-sync

---

## 2026-01-27: Fix Talk File Attachments

Fixed Talk file attachment handling. Previously, downloads failed because the webhook provided paths in the sender's storage which weren't accessible to the bot's WebDAV credentials.

**Root cause:** Talk shares files with the conversation, not as public shares. The share link and file ID endpoints return 404 when accessed with the bot's credentials.

**Solution:** When the zorg Nextcloud user is added as a conversation participant (not just the bot), files shared in Talk automatically appear in zorg's `/Talk/` folder. The webhook handler now just returns the path to this local copy instead of trying to download.

**Key changes:**
- `extract_attachments()` no longer downloads/uploads - returns `Talk/{filename}` path directly
- `extract_message()` now replaces `{file0}` placeholders with `[filename]` for clarity
- Removed unused imports and simplified the attachment extraction logic
- Reverted complex `download_attachment()` method to simple WebDAV version

**Files modified:**
- `src/zorg/main.py` - Simplified attachment handling, added file placeholder replacement
- `src/zorg/talk.py` - Reverted download method to simple version

**Requirement:** The zorg Nextcloud user must be added as a participant in Talk conversations (in addition to the bot) for file attachments to work.

---

## 2026-01-27: Auto-Organize Shared Files

Added functionality to automatically discover files/folders shared with the zorg Nextcloud user, move them to `/Zorg/users/{owner}/shared/`, and auto-create resource entries. Updated ZORG.md polling to use the new location.

**Key changes:**
- Created `shared_file_organizer.py` module for discovering and organizing shared files
- Added `shared/` directory to bot-managed user directories
- Added `get_user_shared_path()` helper function to storage.py
- Added `shared_file_check_interval` scheduler config option (default: 120 seconds)
- Updated ZORG.md poller to scan `/Zorg/users/{user}/shared/` instead of root level
- Owner is now determined from path structure (no PROPFIND needed for ZORG.md discovery)

**How it works:**
1. Scheduler periodically scans root level for files/folders (configurable interval)
2. Owner is determined via WebDAV PROPFIND (`oc:owner-id` property)
3. Files from configured users are moved to `/Zorg/users/{owner}/shared/`
4. Resource entries are auto-created in `user_resources` table
5. Files already in `/Zorg/` path are skipped (already bot-managed)

**Benefits:**
- No manual resource setup needed - just share a file with the bot
- Clean organization: all user files under `/Zorg/users/{user}/shared/`
- ZORG.md files are automatically moved and tracked

**Configuration:**
```toml
[scheduler]
shared_file_check_interval = 120  # seconds (default)
```

**Files added/modified:**
- `src/zorg/shared_file_organizer.py` - NEW: Discovery and organization logic
- `src/zorg/storage.py` - Added `get_user_shared_path()`, updated directory lists
- `src/zorg/config.py` - Added `shared_file_check_interval` setting
- `src/zorg/scheduler.py` - Added shared file polling in single-run and daemon modes
- `src/zorg/zorg_file_poller.py` - Updated to scan shared/ folders instead of root
- `CLAUDE.md` - Updated documentation with new feature

---

## 2026-01-27: Briefing Formatting and Random Reminders

Improved briefing output for Nextcloud Talk with Talk-compatible formatting guidelines and random reminder selection from user's REMINDERS file.

**Key changes:**
- Created `config/skills/briefing.md` with Talk-compatible formatting rules (no tables, basic markdown only)
- Added random reminder selection from `notes_file` resources containing "REMINDERS" in the path
- Added `_fetch_random_reminder()` and `_parse_reminders()` functions to briefing.py
- Updated `build_briefing_prompt()` to accept `user_resources` parameter
- Added `reminders = { enabled = true }` briefing component option

**Bug fixes:**
- Fixed `_fetch_random_reminder()` to use `UserResource` attributes instead of dict `.get()` access
- Fixed newsletter date filtering - added `_parse_email_date()` to handle both RFC 2822 and ISO 8601 date formats (was including old emails because ISO dates failed to parse)

**Configuration:**
```toml
[users.alice.briefings.components]
reminders = { enabled = true }  # Random quote from notes_file named REMINDERS
```

**Files added/modified:**
- `config/skills/briefing.md` - NEW: Talk-compatible formatting guidelines
- `config/skills/_index.toml` - Added `[briefing]` skill entry with `source_types = ["briefing"]`
- `src/zorg/briefing.py` - Added reminder fetching, updated signature with `user_resources`
- `src/zorg/skills/email.py` - Added `_parse_email_date()` for RFC 2822 and ISO 8601 support
- `src/zorg/scheduler.py` - Pass `user_resources` to `build_briefing_prompt()`
- `config/config.example.toml` - Documented `reminders` component

---

## 2026-01-27: Auto-Delete Old Emails from IMAP Inbox

Added scheduler cleanup to automatically delete emails older than a configurable threshold from the IMAP inbox, following the existing scheduler cleanup patterns.

**Key changes:**
- Added `email_retention_days` config field (default: 7 days, 0 to disable)
- Added `delete_email()` function to himalaya wrapper
- Added `cleanup_old_emails()` function to email poller
- Integrated email cleanup into scheduler's `run_cleanup_checks()`
- Cleanup runs at same interval as other cleanup checks (briefing_check_interval)

**Configuration:**
```toml
[scheduler]
email_retention_days = 7  # Delete emails older than N days, 0 to disable
```

**Files modified:**
- `src/zorg/config.py` - Added `email_retention_days` to `SchedulerConfig`
- `src/zorg/skills/email.py` - Added `delete_email()` function
- `src/zorg/email_poller.py` - Added `cleanup_old_emails()` function
- `src/zorg/scheduler.py` - Integrated cleanup into `run_cleanup_checks()`, added daemon startup message
- `config/config.example.toml` - Documented new setting

---

## 2026-01-27: Fix Briefing Results Not Posted to Talk

Fixed a bug where scheduled briefings executed successfully but results never appeared in the target Nextcloud Talk room.

**Problem:**
- Briefings have `source_type="briefing"` but the result posting logic only handled `source_type == "talk"`
- Briefings fell through the conditional with no action taken

**Key changes:**
- Modified scheduler to include `"briefing"` in Talk posting conditions
- Added `--source-type` CLI option for testing different source types

**Files modified:**
- `src/zorg/scheduler.py` - Changed conditions at lines 111 and 131 to `source_type in ("talk", "briefing")`
- `src/zorg/cli.py` - Added `--source-type` argument to task command for testing

---

## 2026-01-26: Scheduler Robustness Improvements

Added automated cleanup for problematic tasks that get stuck in various states. The scheduler now handles stuck confirmations, stale pending tasks, ancient tasks, and database bloat automatically.

**Key changes:**
- Auto-cancel tasks in `pending_confirmation` after configurable timeout (default: 2 hours)
- Log warnings for tasks pending longer than expected (default: 30 minutes)
- Auto-fail tasks pending too long without being processed (default: 24 hours)
- Delete old completed/failed/cancelled tasks to prevent database bloat (default: 7 days retention)
- User notifications via Talk when confirmations expire or tasks fail
- Cleanup checks run every 60 seconds in daemon mode

**Configuration:**
```toml
[scheduler]
confirmation_timeout_minutes = 120  # Auto-cancel pending_confirmation
stale_pending_warn_minutes = 30     # Log warning for stale tasks
stale_pending_fail_hours = 24       # Auto-fail ancient pending tasks
task_retention_days = 7             # Delete old completed tasks
```

**Files modified:**
- `src/zorg/config.py` - Added 4 new fields to `SchedulerConfig`
- `src/zorg/db.py` - Added cleanup functions: `expire_stale_confirmations()`, `get_stale_pending_tasks()`, `fail_ancient_pending_tasks()`, `cleanup_old_tasks()`
- `src/zorg/scheduler.py` - Added `run_cleanup_checks()`, integrated into daemon loop
- `config/config.example.toml` - Documented new robustness options
- `CLAUDE.md` - Added "Scheduler Robustness" section

---

## 2026-01-27: ZORG.md Auto-Discovery

Replaced manual per-user ZORG.md configuration with automatic discovery. Any `ZORG.md` or `_ZORG.md` file shared with the zorg Nextcloud user is now automatically detected and processed.

**Key changes:**
- Auto-discover ZORG files at root level via rclone listing
- Determine file owner via WebDAV PROPFIND (`oc:owner-id` property)
- Match owner to configured users for processing
- Removed `ZorgFileConfig` from user config (no manual setup needed)
- Fixed database lock issue by moving completion handler outside db context
- Updated pattern to match `_ZORG.md.md` variant (Nextcloud edge case)

**How it works:**
1. Scheduler polls root directory for files matching `*ZORG.md` pattern
2. For each match, queries WebDAV to get `oc:owner-id`
3. If owner is a configured user, processes the file for that user
4. Email notifications sent automatically if user has `email_addresses` configured

**Files modified:**
- `src/zorg/zorg_file_poller.py` - Added `discover_zorg_files()`, `get_file_owner()`, refactored polling
- `src/zorg/config.py` - Removed `ZorgFileConfig` dataclass
- `src/zorg/cli.py` - Updated `zorg-file poll/status` for auto-discovery
- `src/zorg/scheduler.py` - Fixed db lock by moving zorg handler outside connection block
- `config/config.example.toml` - Removed per-user zorg_file config, updated docs
- `CLAUDE.md` - Updated ZORG.md documentation for auto-discovery

---

## 2026-01-26: Fix Himalaya Email Processing

Fixed email polling error where the scheduler failed to read emails with `Expecting value: line 1 column 1 (char 0)`. The root cause was an invalid `id:{email_id}` query syntax introduced in commit 21272e4 - himalaya doesn't support `id:` queries, only `date`, `before`, `after`, `from`, `to`, `subject`, `body`, `flag`.

**Key changes:**
- Refactored `read_email()` to accept optional envelope metadata parameter
- When envelope is passed (from caller who already has it), uses metadata directly
- Fallback path fetches all envelopes and finds matching ID (no invalid query)
- Updated `poll_emails()` to pass the envelope it already has from `list_emails()`

**Files modified:**
- `src/zorg/skills/email.py` - Added `envelope` parameter to `read_email()`, removed broken `id:` query
- `src/zorg/email_poller.py` - Pass envelope to `read_email()` to avoid redundant fetch

---

## 2026-01-26: ZORG.md Live TODO Monitoring

Added a new input channel where users can share a `ZORG.md` file with the bot for automatic task processing. The daemon monitors the file continuously, picks up new pending tasks, processes them, and updates the file with results.

**Key features:**
- File format with status markers: `[ ]` pending, `[~]` in progress, `[x]` completed, `[!]` failed
- Stable task identification using SHA-256 hash of normalized content (survives edits)
- Automatic file updates when task status changes (adds timestamps, results, errors)
- Optional email notifications on task completion
- Configurable poll interval (default: 30 seconds)
- CLI commands for manual polling and status checking

**Database changes:**
- Added `zorg_file_tasks` table to track tasks from ZORG.md files
- Content hash ensures duplicate prevention and stable task identity

**Configuration:**
```toml
[users.alice.zorg_file]
enabled = true
file_path = "/alice/ZORG.md"
email_results = true

[scheduler]
zorg_file_poll_interval = 30
```

**Files added/modified:**
- `src/zorg/zorg_file_poller.py` - NEW: Core module with parsing, hashing, file updates, polling
- `src/zorg/config.py` - Added `ZorgFileConfig` dataclass, updated `UserConfig` and `SchedulerConfig`
- `src/zorg/db.py` - Added `ZorgFileTask` dataclass and CRUD functions
- `src/zorg/scheduler.py` - Added polling loop, completion handler for zorg_file tasks
- `src/zorg/cli.py` - Added `zorg-file poll` and `zorg-file status` commands
- `schema.sql` - Added `zorg_file_tasks` table with indexes
- `config/config.example.toml` - Added example configuration and documentation
- `CLAUDE.md` - Updated project structure, architecture, added feature documentation

---

## 2026-01-26: Modular Skills with Selective Loading

Replaced the monolithic `config/skills.md` (328 lines) with individual skill files that are selectively loaded based on task relevance. This significantly reduces prompt size by only including skills needed for each task.

**Key changes:**
- Split skills.md into 9 individual skill files in `config/skills/`
- Created `_index.toml` with skill metadata (keywords, resource types, source types)
- Implemented skills_loader.py with selection logic based on:
  - Core skills always included (files, sensitive-actions)
  - Resource-based: user has calendar → include calendar skill
  - Source-based: briefing tasks → include markets, notes skills
  - Keyword-based: prompt contains "email" → include email skill
- Updated executor to use selective skill loading instead of loading entire file

**Prompt size reduction:**
- Simple query, no resources: 328 → 43 lines (87% smaller)
- Query with calendar/todo resources: significantly smaller than all skills

**Files added/modified:**
- `config/skills/*.md` - Individual skill files (files, email, calendar, todos, memory, tasks, markets, notes, sensitive-actions)
- `config/skills/_index.toml` - Skill metadata for selection
- `src/zorg/skills_loader.py` - NEW: Skill loading and selection logic
- `src/zorg/executor.py` - Uses skills_loader instead of loading single file
- `src/zorg/config.py` - Changed `skills_doc_path` to `skills_dir`
- `config/config.example.toml` - Updated config setting
- `CLAUDE.md` - Documented new skills structure
- Deleted `config/skills.md` - Replaced by modular skills

---

## 2026-01-26: Task Locking Hardening & Context Selection Fixes

Hardened the task locking mechanism to prevent potential race conditions and reverted context selection to use Sonnet (from Haiku) for better judgment.

**Task locking improvements:**
- Added SQLite `busy_timeout=30.0` to wait for locks instead of failing immediately
- Added daemon lockfile (`/tmp/zorg-scheduler-daemon.lock`) to prevent multiple scheduler instances
- Added cleanup for stuck 'running' tasks older than 15 minutes (with retry logic)
- Tasks exhausting retries while stuck are marked as failed

**Context selection changes:**
- Reverted selection model from Haiku back to Sonnet for better context judgment
- Increased selection timeout from 15s to 30s for Sonnet
- Added `skip_selection_threshold` config option (include all messages if history ≤ threshold)
- Added timestamps to selection prompt for better recency judgment

**Files modified:**
- `src/zorg/db.py` - Added busy_timeout, running task cleanup in claim_task()
- `src/zorg/scheduler.py` - Added daemon lockfile with fcntl locking
- `src/zorg/config.py` - Updated defaults to Sonnet, added skip_selection_threshold
- `src/zorg/context.py` - Added timestamps, recency hints
- `config/config.example.toml` - Updated conversation config
- `CLAUDE.md` - Updated documentation

---

## 2026-01-27: Documentation Update & Email MIME Encoding

Comprehensive documentation review and update to sync CLAUDE.md and README.md with the actual implementation. Also fixed email sending to use proper MIME headers and quoted-printable encoding.

**Documentation updates:**
- Added all CLI commands including calendar discover/test, user init/status, task list/show
- Documented config file search locations (config/config.toml, ~/.config/zorg/, /etc/zorg/)
- Added CalDAV auto-derivation explanation (derived from Nextcloud settings)
- Documented webhook server endpoints (/webhook/talk, /health, /tasks, etc.)
- Added Task Status Values section listing all statuses
- Updated architecture descriptions with worker ID, retry backoff, execution timeout
- Added Talk message truncation limit (4000 chars) and confirmation pattern detection
- Synced README.md with CLAUDE.md (fixed @mention requirement, added scheduler section)

**Email MIME encoding:**
- Added `_encode_quoted_printable()` helper function
- Send emails with proper MIME headers (MIME-Version, Content-Type, Content-Transfer-Encoding)
- Body encoded as quoted-printable for proper UTF-8 character handling
- Fixed line endings to use CRLF per RFC 5322
- Improved Message-ID extraction to be case-insensitive

**Files modified:**
- `CLAUDE.md` - Comprehensive update with all features and commands
- `README.md` - Synced with CLAUDE.md, added scheduler config, user memory section
- `TODO.md` - Checked off completed items (memory, confirmation flow, attachments, calendar, docs)
- `config/config.example.toml` - Added CalDAV auto-derivation comment
- `config/skills.md` - Added format_event_for_display and format_day_schedule functions
- `src/zorg/skills/email.py` - MIME headers, quoted-printable encoding, improved Message-ID parsing

---

## 2026-01-27: Fix Conversation Context Selection

Fixed critical bug where conversation context was never being loaded due to invalid CLI flags, plus improved truncation limits and added debugging capabilities.

**Problem identified:**
- Users reported bot couldn't remember previous messages (e.g., "email this to me" after receiving a detailed response failed with "I don't know what 'this' refers to")
- Root cause: `--max-tokens` flag doesn't exist in Claude CLI, causing context selection to fail silently
- Secondary issue: 500-character truncation was too aggressive for detailed bot responses

**Key changes:**
- Removed invalid `--max-tokens` flag from Claude CLI call in context selection
- Increased context truncation limit from 500 to 3000 characters
- Added comprehensive logging throughout context selection flow
- Added `-v/--verbose` flag to scheduler for debug logging
- Logging now shows: skip reasons, history count, selection results, errors

**Debugging improvements:**
- `executor.py` logs why context was skipped (disabled, wrong source type, no token, etc.)
- `context.py` logs selection failures (timeout, parse error, invalid format)
- Scheduler configures logging when `-v` flag is used

**Files modified:**
- `src/zorg/context.py` - Removed `--max-tokens`, increased truncation, added logging
- `src/zorg/executor.py` - Added context loading diagnostics
- `src/zorg/scheduler.py` - Added `-v/--verbose` flag with logging configuration

---

## 2026-01-26: Email Attachments & Threading

Added support for email attachments and proper reply threading. Users can now send emails with attachments (e.g., "summarize this PDF") and have them processed by the bot. Replies are now properly threaded in email clients.

**Key changes:**
- Email attachments are downloaded via himalaya and uploaded to user's Nextcloud inbox
- Attachments stored at `/Zorg/users/{user_id}/inbox/{uuid}_{filename}`
- Added `message_id` tracking for RFC 5322 email threading
- Replies include `In-Reply-To` and `References` headers for proper threading
- Himalaya config now includes `downloads-dir` for attachment handling

**Email attachment flow:**
1. Email poller downloads attachments to temp directory via himalaya
2. Attachments uploaded to user's Nextcloud inbox with unique prefix
3. Attachment paths (Nextcloud) included in task prompt
4. Claude Code can read attachments via rclone
5. Temp files cleaned up, persistent copy remains in Nextcloud

**Files modified:**
- `src/zorg/skills/email.py` - Added `download_attachments()`, `message_id` capture, threading headers in `reply_to_email()`
- `src/zorg/email_poller.py` - Download attachments, upload to Nextcloud, pass `message_id`
- `src/zorg/email_setup.py` - Added `downloads-dir` to himalaya config
- `src/zorg/storage.py` - Added `upload_file_to_inbox()` function
- `src/zorg/db.py` - Added `message_id` to `ProcessedEmail` and related functions
- `src/zorg/scheduler.py` - Pass `message_id` for reply threading
- `schema.sql` - Added `message_id` column to `processed_emails`

---

## 2026-01-26: Email Input Channel

Added email as an input channel for Zorg. The bot now polls for new emails, creates tasks from messages sent by known users, and replies via email when tasks complete.

**Key changes:**
- Extended `EmailConfig` with IMAP/SMTP settings (host, port, user, password, poll_folder, bot_email)
- Added `email_addresses` field to `UserConfig` for mapping email addresses to users
- Created `processed_emails` table to track processed emails and avoid duplicates
- Built `email_setup.py` module to generate himalaya config from zorg settings
- Implemented `email_poller.py` for polling inbox and creating tasks from known senders
- Updated `scheduler.py` to poll emails on each run and send replies after task completion
- Enabled conversation context for email threads (using subject+participants hash as thread_id)
- Added CLI commands: `zorg email setup|show-config|poll|list|test` and `zorg user list|lookup`

**Email flow:**
1. Scheduler polls INBOX via himalaya (configured from zorg settings)
2. Emails from known senders (mapped via `email_addresses`) create tasks with `source_type="email"`
3. Thread ID computed from normalized subject + participants for conversation context
4. Claude Code processes task, result sent as email reply via himalaya
5. Unknown senders are marked as processed but no task created

**Files added/modified:**
- `src/zorg/email_setup.py` - NEW: Generate himalaya config from zorg settings
- `src/zorg/email_poller.py` - NEW: Poll emails and create tasks
- `src/zorg/config.py` - Added IMAP/SMTP fields, user email_addresses, find_user_by_email()
- `src/zorg/db.py` - Added ProcessedEmail dataclass and functions
- `src/zorg/scheduler.py` - Email polling integration, post_result_to_email()
- `src/zorg/executor.py` - Enable conversation context for email source
- `src/zorg/skills/email.py` - Fixed send_email() and reply_to_email() for himalaya
- `src/zorg/cli.py` - Added email and user subcommands
- `schema.sql` - Added processed_emails table
- `config/config.example.toml` - Full email settings and user email_addresses examples

---

## 2026-01-26: Conversation Context Curator

Implemented conversation context feature that uses Sonnet to intelligently select relevant previous messages before each Claude Code execution. This enables the bot to maintain conversational continuity across multiple exchanges in a Talk room.

**Key changes:**
- Added `ConversationConfig` dataclass to config with `enabled`, `lookback_count`, `selection_model`, and `selection_timeout` fields
- Created `get_conversation_history()` in db.py to retrieve completed tasks from the same conversation token
- Built new `context.py` module with Sonnet-based context selection via Claude CLI
- Updated `build_prompt()` to include conversation context section before user's request
- Updated `execute_task()` to fetch history, select relevant messages, and format context
- Added CLI flags: `-t/--conversation-token` for testing context, `--no-context` to disable

**Flow:**
1. New message arrives with conversation_token (Talk room ID)
2. Retrieve recent completed tasks from same room
3. Sonnet analyzes history and selects relevant messages (JSON response with IDs)
4. Selected messages formatted and included in Claude Code prompt
5. On any error (timeout, parse error), proceeds without context (graceful degradation)

**Files added/modified:**
- `src/zorg/context.py` - NEW: Context selection with `select_relevant_context()` and `format_context_for_prompt()`
- `src/zorg/config.py` - Added `ConversationConfig` dataclass
- `src/zorg/db.py` - Added `ConversationMessage` dataclass and `get_conversation_history()`
- `src/zorg/executor.py` - Integrated context into prompt building and task execution
- `src/zorg/cli.py` - Added `-t/--conversation-token` and `--no-context` flags
- `config/config.example.toml` - Added `[conversation]` section

---

## 2026-01-26: Claude Code Execution Testing

Tested the full execution pipeline with actual Claude Code (not dry-run). Created a local testing configuration using rclone's local filesystem backend to simulate Nextcloud file operations.

**Key changes:**
- Created `config/config.toml` for local testing with `testlocal` rclone remote
- Configured rclone local remote for testing without Nextcloud
- Verified end-to-end task execution: queue → executor → Claude Code → result
- Tested TODO file operations: read, add items, mark complete
- Confirmed Claude Code correctly parses user resources and uses rclone for file I/O

**Test results:**
- Simple questions: Working
- Resource awareness: Claude Code lists user's assigned resources
- Read TODO file: Successfully parsed pending/completed tasks via rclone
- Write TODO file: Added new items correctly
- Update TODO file: Marked tasks complete with checkbox update

**Files added/modified:**
- `config/config.toml` - Local testing configuration with testlocal rclone remote

---

## 2026-01-26: Initial Project Setup

Built the core infrastructure for Zorg, a Claude Code-powered bot with Nextcloud Talk interface. The system uses a task queue architecture where messages from Talk (or CLI) are queued in SQLite, then processed by a scheduler that invokes Claude Code with appropriate context and skills.

**Key changes:**
- Created project structure with uv for package management
- Implemented SQLite-based task queue with atomic locking and retry logic
- Built FastAPI webhook handler for Nextcloud Talk integration
- Added multi-user resource permission system (users can only access their assigned calendars, folders, TODO files)
- Created CLI for local testing without needing Nextcloud (`uv run zorg task "..." -u user -x --dry-run`)
- Implemented briefing system with cron-based scheduling for morning/evening summaries
- Added skills modules for calendar (CalDAV), email (himalaya), and files (rclone)
- Created skills reference document that gets included in Claude Code prompts

**Files added:**
- `src/zorg/cli.py` - CLI interface for testing and administration
- `src/zorg/config.py` - TOML configuration loading
- `src/zorg/db.py` - SQLite operations (tasks, resources, briefings, logs)
- `src/zorg/executor.py` - Claude Code execution wrapper with prompt building
- `src/zorg/main.py` - FastAPI webhook server
- `src/zorg/scheduler.py` - Task processor and briefing scheduler
- `src/zorg/talk.py` - Nextcloud Talk API client
- `src/zorg/skills/calendar.py` - CalDAV helper functions
- `src/zorg/skills/email.py` - himalaya wrapper functions
- `src/zorg/skills/files.py` - rclone wrapper functions
- `config/config.example.toml` - Example configuration
- `config/skills.md` - Skills reference document for Claude Code
- `schema.sql` - Database schema
- `scripts/setup.sh` - Setup script
- `scripts/scheduler.sh` - Cron wrapper for scheduler
- `pyproject.toml` - Project configuration
- `README.md` - Documentation
