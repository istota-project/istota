# TODO

## Priority 1: Core Functionality

### Setup & Configuration
- [ ] Configure rclone remote for Nextcloud WebDAV
- [x] Native Python email handling (imap-tools + smtplib)
- [ ] Set up Nextcloud bot account and app password
- [ ] Register Talk webhook with shared secret

### Testing ✅
- [x] Test with actual Claude Code execution (non-dry-run)
- [x] Test Talk polling integration (migrated from webhook to polling)
- [x] Test briefing generation

## Priority 2: Enhancements

### Conversation Context ✅
- [x] Retrieve conversation history from same Talk room
- [x] Use Sonnet to select relevant previous messages
- [x] Include selected context in Claude Code prompt
- [x] CLI flags for testing (`-t`, `--no-context`)
- [x] Graceful degradation on context selection failure
- [x] Debug logging for context selection (use `-v` with scheduler)

### Calendar Integration ✅
- [x] CalDAV client with helper functions
- [x] Auto-discover user calendars via CalDAV ownership
- [x] CLI commands: `istota calendar discover|test`
- [x] Test read/write access with `--test-write` flag
- [x] Event CRUD operations (create, read, update, delete)
- [x] Calendar CLI: `update` subcommand, `--week` flag for list

### Briefing Management
- [ ] CLI commands to add/edit/delete briefing configs
- [ ] Weather integration for briefings
- [ ] Custom briefing templates
- [x] Briefing format skill (document expected output style for Claude)
- [x] Random reminders from REMINDERS notes_file
- [x] Deterministic delivery: JSON output format, email skill excluded, scheduler handles delivery

### Email Input Channel ✅
- [x] Poll INBOX for new emails from known senders
- [x] Create tasks from emails with conversation threading
- [x] Send replies via email after task completion
- [x] CLI commands: `istota email poll|list|test`
- [x] Native Python email (imap-tools + smtplib, replaced himalaya)
- [x] Proper MIME headers (Content-Type, charset)
- [x] Email threading with In-Reply-To and References headers
- [x] Full RFC 5322 References chain (parent's references + parent's message_id)
- [x] HTML stripping for newsletter content in briefings

### Channel-Specific Style Guides ✅
- [x] Separate response styles for email vs Talk (formal vs conversational)
- [x] Channel guidelines system (`config/guidelines/{source_type}.md`)
- [x] Email: salutation, sign-off, structured formatting
- [x] Talk: brief, conversational, no formal greeting

### Nextcloud Directory Structure ✅
- [x] Design bot's Nextcloud directory layout for per-user file segregation
- [x] `/Users/{username}/` for user-specific bot-managed files
- [x] `/Users/{username}/inbox/` for files users want bot to process
- [x] `/Users/{username}/exports/` for files bot generates for user
- [x] Handle user-shared folders (tracked in user_resources) vs bot-owned files
- [x] Document directory conventions in CLAUDE.md

### User Long-Term Memory ✅
- [x] Design hybrid memory system (DB short-term + Nextcloud long-term)
- [x] Per-user memory file at `/Users/{username}/USER.md`
- [x] Bot reads memory at task start for user preferences/context
- [x] Bot appends when user says "remember this"
- [x] User can manually review/edit memory file
- [x] Memory survives DB rebuilds
- [x] Freeform markdown format with template
- [x] Direct Nextcloud writes via rclone (multi-user safe)

### Multi-User Talk Room Participation ✅
- [x] Only respond when @mentioned in rooms with 3+ participants
- [x] 2-person group rooms (bot + 1 user) treated like DMs
- [x] Participant count caching with 5-min TTL
- [x] Strip bot's own @mention from prompt, resolve others to @DisplayName
- [x] Conversation context shows usernames as speaker labels in group chats
- [x] Group chat note in executor prompt
- [x] Graceful fallback to DM behavior on API errors
- [x] Reply threading (`reply_to`) and @mention notifications in group chat responses

### Confirmation Flow ✅
- [x] Handle confirmation replies in Talk (user says "yes"/"no")
- [x] Track pending confirmations (`pending_confirmation` status)
- [x] Regex pattern detection for confirmation requests in Claude output
- [x] Timeout for pending confirmations (auto-cancel after configurable timeout)

### File Attachments ✅
- [x] Download email attachments and upload to user's Nextcloud inbox
- [x] Include attachment paths in task prompt for Claude Code access
- [x] Download Talk file attachments and upload to user's Nextcloud inbox
- [x] Include Talk attachment paths in task prompt

### TASKS.md File Input Channel ✅
- [x] Users share TASKS.md file with bot for automatic task processing
- [x] Daemon polls file at configurable interval (default: 30s)
- [x] Parse tasks with status markers: `[ ]` pending, `[~]` in progress, `[x]` completed, `[!]` failed
- [x] Stable task ID via SHA-256 hash of normalized content
- [x] Automatic file updates with timestamps and results
- [x] Optional email notifications on task completion
- [x] CLI commands: `istota tasks-file poll|status`
- [x] Database tracking to prevent duplicate processing
- [x] Auto-discovery: detect any shared TASKS.md files via WebDAV owner lookup
- [x] Auto-organize shared files to `/Users/{owner}/shared/`
- [x] Auto-create user_resources entries for shared files

### File-Based Scheduled Jobs (CRON.md) ✅
- [x] `scheduled_jobs` SQLite table with cron, prompt, conversation_token
- [x] Scheduler evaluates cron expressions per user timezone
- [x] Wired into single-run and daemon modes
- [x] CRON.md file-based definitions synced to DB on each scheduler cycle
- [x] Auto-migration from DB-only jobs to CRON.md files
- [x] Skill doc for the bot to manage jobs via CRON.md file editing (was sqlite3)
- [x] Available to all users (removed admin_only restriction)
- [x] Context isolation (scheduled/briefing excluded from Talk context)
- [x] Worker pool isolation (separate fg/bg caps, per-user limits, per-channel gate)
- [x] Silent scheduled jobs (`silent_unless_action` with ACTION/NO_ACTION prefix)
- [x] Failure tracking + auto-disable after N consecutive failures
- [x] `!cron` command for listing/enabling/disabling jobs
- [x] `!status` interactive vs background task grouping
- [x] Shell command jobs (`command` field, mutually exclusive with `prompt`, subprocess execution)
- [x] One-time jobs (`once = true`) auto-removed from DB and CRON.md after successful execution
- [x] Per-job log channel suppression (`skip_log_channel = true` in CRON.md)
- [x] External prompt files (`prompt_file` field loads prompt from file instead of inline)

### User Timezone from Nextcloud ✅
- [x] Fetch user's timezone from Nextcloud API instead of requiring manual config
- [x] Fall back to configured `timezone` in config.toml if API unavailable
- [x] Also hydrate display_name and email from Nextcloud API

### Per-User /tmp Folders ✅
- [x] Isolate per-user temp directories so job prompts/artifacts don't intermingle
- [x] Create `/tmp/istota/{user_id}/` (or configurable base) at task start
- [x] Pass temp dir path to Claude Code execution context
- [x] Clean up temp dir after task completion (or retain for memory processing)

### Persistent Memory System (Sleep Cycle) ✅
- [x] Scheduled `process_memories` job runs nightly (configurable cron per user)
- [x] Processes the day's tmp job prompts, conversations, and DB message history for each user
- [x] Extracts important pieces worth remembering long-term
- [x] Writes dated memory files to `/Users/{user}/memories/` (e.g., `2026-01-28.md`)
- [x] Deletes processed /tmp job prompts for the user after extraction
- [x] At task time, the bot searches dated memory files for relevant context
- [x] Configurable retention policy for dated memory files
- [x] Tail-biased truncation preserves conclusions (head 40% + tail 60%)
- [x] Dynamic per-task budget allocation (proportional from 50K total)
- [x] Conversation grouping by token for threading context
- [x] Improved extraction prompt with depth guidance and examples

### Invoicing & Accounting (removed — moved to Moneyman)
- [x] Direct beancount accounting skill removed in favor of Moneyman API service
- [x] Invoice scheduler removed (monthly auto-generation, reminders, overdue detection)
- [x] INVOICING.md, ACCOUNTING.md, FEEDS.md config file seeding removed

### Semantic Memory Search ✅
- [x] Hybrid BM25 + vector search over conversations and memory files
- [x] FTS5 for keyword search, sqlite-vec + sentence-transformers for semantic similarity
- [x] Reciprocal Rank Fusion to combine BM25 and vector results
- [x] Graceful degradation to BM25-only if sqlite-vec/torch unavailable
- [x] Auto-indexing hooks in scheduler (post-completion) and sleep cycle (post-write)
- [x] CLI skill: search, index, reindex, stats commands
- [x] Content-hash dedup, user isolation, source type filtering
- [x] Channel sleep cycle with memory search integration (channel:{token} namespace)
- [x] `!search` command for synchronous conversation history search (memory index + Talk API, room scoping)
- [x] `!search` date filtering (`--since`, `--week`)
- [x] `!search` source type filtering (`--memories`)

### Git/GitLab/GitHub Developer Skill ✅
- [x] Clone repos to persistent bare clones with worktree-based branch isolation
- [x] Branch management (create feature branches, `{bot_name}/<task-id>-<slug>` naming)
- [x] Commit and push changes (git credential helper for transparent HTTPS auth)
- [x] Create merge requests via GitLab API (`$GITLAB_API_CMD` wrapper)
- [x] Handle authentication (token passed via env var, helper scripts read `$GITLAB_TOKEN` at runtime)
- [x] Cleanup: `git worktree remove` + `git branch -d` after MR merged
- [x] API endpoint allowlist (configurable, blocks merge/delete/admin by default)
- [x] Default namespace resolution (`gitlab_default_namespace` for short repo names)
- [x] Dedicated bot account support (separate `gitlab_username` for auth vs namespace)
- [x] GitHub PR workflows (create, list, merge, review requests via `$GITHUB_API_CMD`)
- [x] GitHub credential helper (`x-access-token` default, configurable username)
- [x] GitHub Enterprise support (auto-detects `{url}/api/v3` vs `api.github.com`)
- [x] Multi-platform support (GitLab + GitHub simultaneously, dynamic `GIT_CONFIG_COUNT`)

### Direct Email Sending ✅
- [x] CLI command `python -m istota.skills.email send` for direct sending from Claude Code
- [x] Executor passes SMTP/IMAP credentials as env vars
- [x] Email skill doc updated: CLI for non-email channels, JSON for email-reply tasks
- [x] Dedicated `email output` tool replaces inline JSON pattern for email replies/scheduled jobs
- [x] Deferred email output file (`task_{id}_email_output.json`) with scheduler pickup
- [x] Smart-quote normalization fallback for legacy inline JSON path

### Emissary Email Threads (Phase 1-2) ✅
- [x] Outbound email tracking (`sent_emails` table, Message-ID capture)
- [x] Thread-matched inbound routing (References header → `sent_emails` lookup)
- [x] Emissary replies routed to originating user via Talk (`output_target="talk"`)
- [x] Deferred sent email recording (sandbox-safe via JSON file)
- [x] Draft + approval flow for emissary responses (confirmation context injection + stale confirmation cancellation)
- [x] Multi-turn thread continuity (Phase 1 routing + Talk conversation context handles repeat replies naturally)

### Per-User Email Ingest (Plus-Addressing) ✅
- [x] Plus-addressed routing (`bot+user_id@domain`) as highest-priority inbound path
- [x] Per-user `SMTP_FROM` (outbound uses `bot+user_id@domain` so replies route back)
- [x] `routing_method` tracking in `processed_emails` (plus_address, sender_match, thread_match, discarded)
- [x] Per-user email address exposed in prompt header

### Emissary Email Threads (Phase 3 — deferred)
- [ ] Delegated autonomy levels: per-user `emissary_mode` config (`notify` = just surface reply, `draft` = draft + confirm, `auto` = send autonomously). Requires config schema change, prompt branching in emissary reply prompt, per-user override in user config
- [ ] Structured email draft storage: `istota-skill email draft` command writes `task_{id}_email_draft.json` instead of relying on confirmation_prompt text. Useful if the prompt-based approach proves unreliable for preserving exact draft content across re-execution
- [ ] Threading headers on confirmed replies: ensure the bot uses `reply_to_email()` with proper In-Reply-To/References when sending confirmed drafts (currently relies on the bot figuring this out from context — monitor for correctness)
- [ ] Thread summary in prompt: when multiple exchanges have occurred in an emissary thread, compile the full email chain from `sent_emails` + `processed_emails` tables rather than relying on quoted text in email bodies

### Agent-Task Heartbeat Check (Removed)
- [x] ~~Queue natural language prompts as heartbeat tasks~~ — Removed in favor of CRON.md with `silent_unless_action`
- [x] ~~Silent unless action mode~~ — Now handled by CRON.md scheduled jobs
- [x] ~~Duplicate prevention via pending_task_id tracking~~ — No longer needed
- [x] ~~Heartbeat state updated after task completion~~ — No longer needed

### ntfy Push Notifications ✅
- [x] NtfyConfig with server_url, topic, token (bearer), username/password (basic auth), priority
- [x] Per-user ntfy_topic override, topic resolution chain (explicit > user > global)
- [x] Central notifications.py dispatcher (Talk, Email, ntfy) replacing duplicated code
- [x] Surface values: "talk", "email", "ntfy", "both" (talk+email), "all" (talk+email+ntfy)
- [x] Heartbeat alerts support ntfy and email (was Talk-only)
- [x] Scheduler output_target extended for ntfy/all
- [x] Ansible role updated (defaults, config template, user template)

### Per-User Log Channel ✅
- [x] Dedicated Talk room per user for verbose task execution logs
- [x] Real-time tool call streaming via message editing (no rate limiting)
- [x] Conversation token → display name resolution with process-level cache
- [x] Composite callback when both Talk progress and log channel are active
- [x] Progress style system: replace (latest + elapsed), full (append), legacy, none

### Graceful API Error Handling ✅
- [x] Auto-retry transient API errors (5xx, 429) before counting against task attempts
- [x] User-friendly error messages in Talk (no raw JSON)
- [x] Suppress error emails to users (log only)
- [x] Preserve debugging info (request_id) in logs

### Malformed Output Detection (ISSUE-019)
- [x] Result content validation guard (leaked tool-call XML, strict mode for Talk)
- [x] ~~Proportionality heuristic~~ (removed — false positives outweighed benefit)
- [x] Automatic retry on malformed output (existing retry logic)
- [x] Result quality metrics logging
- [x] Task ID in ack messages and done summaries
- [x] Execution trace collection and `!more #<task_id>` command for post-hoc inspection
- [x] Result composition from streaming output (`_compose_full_result` recovers lost text blocks)
- [x] CM-aware result composition (ISSUE-026): `ContextManagementEvent` markers, trace segmentation, last-substantial-segment selection
- [ ] Stream-level detection in parser (MalformedOutputEvent)
- [ ] Model-based result validation gate (Sonnet/Haiku for high-tool-count tasks)

### Integration Tests ✅
- [x] Comprehensive unit test suite (~842 tests across 26 files)
- [x] Talk API integration tests (22 tests against real Nextcloud)
- [x] Test task retry logic (unit tested in test_db.py and test_scheduler.py)
- [x] TDD workflow adopted as project standard

### Documentation ✅
- [x] Update CLAUDE.md with all CLI commands
- [x] Document configuration file locations
- [x] Document CalDAV auto-derivation from Nextcloud settings
- [x] Document webhook server endpoints
- [x] Document task status values
- [x] Update README.md to match implementation
- [x] Comprehensive ARCHITECTURE.md

## Priority 3: Deployment

### Production Setup ✅
- [x] Ansible role for deployment (ported to `deploy/ansible/`)
- [x] Standalone `install.sh` with interactive wizard
- [x] Config generator (`deploy/render_config.py`) — replaced by Ansible bootstrap
- [x] Ansible bootstrap: `install.sh` delegates to bundled Ansible role via `settings_to_vars.py`
- [x] Systemd service templating
- [x] Log rotation configuration
- [x] Ansible repo dir cleanup (`istota_repo_dir` variable, no more `src/src/` nesting)
- [x] Tag-based release deployment (`repo_tag` setting, `istota --version`, semver resolution)
- [ ] Monitoring/alerting
- [x] Nextcloud client refactor: shared `nextcloud_client.py` module (OCS + WebDAV) + nextcloud skill CLI
- [ ] Migrate server from old `src/` to `istota/` repo dir (remove `/srv/app/zorg/src` after deploy)

### Docker Compose Deployment
- [x] Docker Compose stack (postgres, redis, nextcloud, istota)
- [x] Automatic Nextcloud provisioning (users, apps, external storage, Talk rooms)
- [x] Shared volume file access with external storage mounts
- [x] Talk attachment fallback to NC data directory
- [x] Full env-based configuration (~170 vars, parity with Ansible role)
- [x] All Python extras installed (whisper, memory-search)
- [x] Browser container via Compose profile (`--profile browser`)
- [x] GPS webhook receiver via Compose profile (`--profile location`)
- [x] Bubblewrap sandbox + skill credential proxy (graceful degradation)
- [ ] Caddy service for automatic TLS (production domains)
- [ ] Health checks on istota container
- [ ] Backup/restore commands
- [ ] Multi-user provisioning (add users after initial setup)
- [ ] Suppress "Failed to share folder" warning when using external storage

### Security
- [x] Review Claude Code sandbox restrictions
- [x] Audit resource permission model (admin/non-admin isolation with /etc/istota/admins)
- [x] Clean subprocess env for Claude Code (restricted mode strips inherited env)
- [x] Replace `--dangerously-skip-permissions` with `--allowedTools` in restricted mode
- [x] Strip credentials from heartbeat/cron subprocess env (always-on)
- [x] Env var overrides for secrets (`EnvironmentFile=` in systemd)
- [x] Credential rotation (Nextcloud, IMAP, ntfy, GitLab, Karakeep)
- [ ] Rate limiting for API calls
- [x] Per-user filesystem isolation (bubblewrap sandbox with mount namespaces)
- [x] Deferred DB operations for sandbox-safe writes (subtasks + transaction tracking via JSON files)
- [x] Credential isolation via skill proxy (Unix socket, strips secrets from Claude's env)
- [x] Skill proxy phase 2: GITLAB_TOKEN, GITHUB_TOKEN (credential-fetch helper + proxy-aware shell scripts)
- [x] Fix skill proxy socket visibility inside bwrap sandbox (bind-mount from /tmp)
- [x] Per-skill credential scoping (proxy injects per-skill, but all CLI skills get credentials regardless of selection)
- [x] OAuth token hardening (removed env var passthrough, RO .credentials.json mount)
- [x] .developer/ scripts mounted read-only (prevent credential-fetch script replacement)
- [x] Admin Nextcloud mount scoped to user dir (was full content tree)
- [x] Skill proxy phase 3: Monarch credentials as resources (env vars via skill proxy, no file path needed)
- [x] Network proxy for agent subprocesses (domain allowlist)
- [x] Network allowlist scoped to current user's resources (not all users)
- [x] CSRF Origin header validation on web app state-changing endpoints
- [x] Session rotation on OIDC login (clear before write)
- [x] Deferred sent_emails identity hardening (always use task.user_id)

### Authenticated Web Interface
- [x] Nextcloud OIDC login/callback/logout (authlib + SessionMiddleware)
- [x] SvelteKit frontend with adapter-static, dark theme, `/istota` base path
- [x] Dashboard with feature cards
- [x] Feeds page: masonry card grid, image/text filter, sort, grid/list view, lightbox
- [x] Miniflux API proxy (entries, feeds, mark as read)
- [x] Ansible deployment (Node.js build, systemd service, nginx proxy)
- [x] Mark as read on scroll/click
- [ ] Feed category filtering
- [x] Pagination / infinite scroll
- [ ] Task history page
- [ ] Memory viewer page
- [ ] Remove static feed page generation (once web UI is primary)

### Moneyman Web Integration
- [x] Fava reverse proxy via nginx auth_request + Istota session
- [x] Moneyman API proxy routes (ledgers, fava instances)
- [x] Auth-check endpoint for nginx sub-request validation
- [x] Ledgers page with Fava links
- [x] Auto-port allocation from configurable range (fava_enabled + fava_port_start)
- [ ] Embedded balances dashboard (direct in Istota UI)
- [ ] Work log tracking page (timesheet backed by Moneyman work API)
- [ ] Invoice management page (generate, status, mark paid)

## Priority 4: Future Ideas

### Architecture
- [x] Simplify file syncing by mounting rclone Nextcloud remote as local folder
- [x] Rebuild bot as regular Nextcloud user instead of Nextcloud Talk bot (polling-based, no webhook server needed)

### Script State & Data Access
- [x] Key-value store (`istota_kv` table) with CLI for script runtime state
- [x] Migrate existing scripts to use KV store for state
- [ ] Bulk data read/write commands for larger JSON blobs

### Skills ✅
- [x] Web browsing skill via Dockerized Playwright with stealth and VNC captcha fallback
- [x] Audio transcription via faster-whisper (CPU, int8) with auto model selection and RAM guard
- [x] Plugin architecture: self-contained skill directories with YAML frontmatter metadata, declarative env var wiring, directory-based discovery
- [x] Markets skill: interactive CLI with quote, summary, finviz commands + keyword triggers
- [ ] More specialized skills with specific commands/tools
- [x] Location tracking via Overland GPS (FastAPI receiver, place resolution, visit state machine, full place CRUD CLI)
- [x] Calendar attendance correlation via GPS pings (geocoding, place matching, proximity check)
- [x] Dependency isolation: optional deps in extras groups, import guards, `!skills` availability display
- [x] Service credentials as resources (monarch follows karakeep pattern with ResourceConfig.extra)
- [x] Miniflux migration: feeds skill with CLI, miniflux resource type, bridger app for Tumblr/Are.na
- [x] Moneyman skill: API-based accounting via standalone Moneyman service (mutual exclusion with direct beancount skill)
- [ ] Config validation / "unconfigured" skill status (declarative credential requirements)

## Priority 5: Location Awareness (Phase 2+)

### Reverse Geocoding & Day Summary ✅
- [x] Reverse geocode cache in main DB (replaces standalone script's separate cache)
- [x] `reverse-geocode` CLI subcommand (single lat/lon → place name)
- [x] `day-summary` CLI subcommand (cluster pings, resolve names, filter transit)
- [x] Greedy sequential clustering in shared `geo.py`

### Place Management ✅
- [x] Full CRUD via CLI (`update`, `delete` subcommands by name or ID)
- [x] Web UI: edit form, drag-to-reposition on map, visit stats panel
- [x] Ping reassignment on place move/radius change
- [x] Geofence circle rendering fix (correct scaling at all zoom levels)

### Visits & Analytics
- [x] Visit stats derived from pings (elsewhere-based split, walk-by filter)
- [x] Web UI visit stats panel (count, avg/total/longest duration, first/last dates)
- [ ] Dwell detection (periodic check for open visits exceeding dwell threshold)
- [ ] `stats` CLI subcommand (time-at-place aggregation by month/year)
- [ ] `top` CLI subcommand (ranked places by visit count/duration)
- [ ] Briefing integration (location component in morning summaries)
- [ ] Year-review analytics
