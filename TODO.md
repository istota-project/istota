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
- [ ] Test briefing generation

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

### Briefing Management
- [ ] CLI commands to add/edit/delete briefing configs
- [ ] Weather integration for briefings
- [ ] Custom briefing templates
- [x] Briefing format skill (document expected output style for Claude)
- [x] Random reminders from REMINDERS notes_file

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
- [x] Worker pool isolation (reserved slots for interactive tasks)
- [x] Silent scheduled jobs (`silent_unless_action` with ACTION/NO_ACTION prefix)
- [x] Failure tracking + auto-disable after N consecutive failures
- [x] `!cron` command for listing/enabling/disabling jobs
- [x] `!status` interactive vs background task grouping
- [x] Shell command jobs (`command` field, mutually exclusive with `prompt`, subprocess execution)

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

### Invoicing System ✅
- [x] Config-driven invoice generation from INVOICING.md (markdown + TOML)
- [x] Work log tracking via _INVOICES.md with TOML entries
- [x] PDF generation via WeasyPrint with professional layout
- [x] Beancount A/R integration (generate + paid postings)
- [x] Service types: hours, days, flat, other
- [x] Per-item discount field with conditional display
- [x] Client bundle rules (group or separate services)
- [x] Configurable account names (income_account, ar_account, defaults)
- [x] String payment terms ("On receipt") alongside numeric days
- [x] Auto-create INVOICING.md and work log from templates
- [x] CLI: invoice generate/list/paid/create
- [x] Multi-entity support (multiple companies per config, per-entity accounts/currency/logo)
- [x] Uninvoiced entry selection with invoice stamping (prevents duplicate invoicing)
- [x] Optional `--period` flag (upper date bound instead of exact month match)

### Monarch Money Integration ✅
- [x] Config file (ACCOUNTING.md) with TOML block for credentials, account mappings, tag filters
- [x] `sync-monarch` CLI command with API-based transaction sync
- [x] Fixed CSV import column order (Date,Merchant,Category,Account,Original Statement,Notes,Amount,Tags,Owner)
- [x] Tag filtering for both CSV import and API sync
- [x] Deduplication via SQLite tables (monarch_synced_transactions, csv_imported_transactions)
- [x] Using monarchmoneycommunity fork with correct API endpoint
- [x] Tag reconciliation: auto-recategorize when business tag removed (creates reversal entries)

### Semantic Memory Search ✅
- [x] Hybrid BM25 + vector search over conversations and memory files
- [x] FTS5 for keyword search, sqlite-vec + sentence-transformers for semantic similarity
- [x] Reciprocal Rank Fusion to combine BM25 and vector results
- [x] Graceful degradation to BM25-only if sqlite-vec/torch unavailable
- [x] Auto-indexing hooks in scheduler (post-completion) and sleep cycle (post-write)
- [x] CLI skill: search, index, reindex, stats commands
- [x] Content-hash dedup, user isolation, source type filtering
- [x] Channel sleep cycle with memory search integration (channel:{token} namespace)

### Git/GitLab Developer Skill ✅
- [x] Clone repos to persistent bare clones with worktree-based branch isolation
- [x] Branch management (create feature branches, `{bot_name}/<task-id>-<slug>` naming)
- [x] Commit and push changes (git credential helper for transparent HTTPS auth)
- [x] Create merge requests via GitLab API (`$GITLAB_API_CMD` wrapper)
- [x] Handle authentication (token passed via env var, helper scripts read `$GITLAB_TOKEN` at runtime)
- [x] Cleanup: `git worktree remove` + `git branch -d` after MR merged
- [x] API endpoint allowlist (configurable, blocks merge/delete/admin by default)
- [x] Default namespace resolution (`gitlab_default_namespace` for short repo names)
- [x] Dedicated bot account support (separate `gitlab_username` for auth vs namespace)

### Direct Email Sending ✅
- [x] CLI command `python -m istota.skills.email send` for direct sending from Claude Code
- [x] Executor passes SMTP/IMAP credentials as env vars
- [x] Email skill doc updated: CLI for non-email channels, JSON for email-reply tasks

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

### Graceful API Error Handling ✅
- [x] Auto-retry transient API errors (5xx, 429) before counting against task attempts
- [x] User-friendly error messages in Talk (no raw JSON)
- [x] Suppress error emails to users (log only)
- [x] Preserve debugging info (request_id) in logs

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

## Priority 3: Deployment

### Production Setup ✅
- [x] Ansible role for deployment (ported to `deploy/ansible/`)
- [x] Standalone `install.sh` with interactive wizard
- [x] Config generator (`deploy/render_config.py`)
- [x] Systemd service templating
- [x] Log rotation configuration
- [ ] Monitoring/alerting

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
- [ ] Network proxy for agent subprocesses (domain allowlist)

## Priority 4: Future Ideas

### Architecture
- [x] Simplify file syncing by mounting rclone Nextcloud remote as local folder
- [x] Rebuild bot as regular Nextcloud user instead of Nextcloud Talk bot (polling-based, no webhook server needed)

### Script State & Data Access
- [x] Key-value store (`istota_kv` table) with CLI for script runtime state
- [ ] Migrate existing scripts to use KV store or `data/` directory for state
- [ ] Bulk data read/write commands for larger JSON blobs

### Skills
- [x] Web browsing skill via Dockerized Playwright with stealth and VNC captcha fallback
- [x] Audio transcription via faster-whisper (CPU, int8) with auto model selection and RAM guard
- [ ] More specialized skills with specific commands/tools
