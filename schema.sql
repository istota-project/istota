-- Istota task queue and configuration schema

-- Core task queue
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    status TEXT DEFAULT 'pending',  -- pending, locked, running, completed, failed, pending_confirmation, cancelled
    priority INTEGER DEFAULT 5,

    -- Source context
    source_type TEXT NOT NULL,      -- 'talk', 'cli', 'scheduled', 'subtask', 'briefing', 'email'
    conversation_token TEXT,
    user_id TEXT NOT NULL,
    parent_task_id INTEGER,
    is_group_chat INTEGER DEFAULT 0,

    -- Task content
    prompt TEXT NOT NULL DEFAULT '',
    command TEXT,                    -- Shell command (mutually exclusive with prompt)
    attachments TEXT,               -- JSON array of file paths

    -- Execution tracking
    locked_at TEXT,
    locked_by TEXT,
    started_at TEXT,
    completed_at TEXT,
    attempt_count INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,

    -- Results
    result TEXT,
    actions_taken TEXT,             -- JSON array of tool use descriptions from execution
    execution_trace TEXT,           -- JSON array of interleaved tool/text events from execution
    error TEXT,

    -- Confirmation flow
    confirmation_prompt TEXT,
    confirmed_at TEXT,

    -- Scheduling
    scheduled_for TEXT,

    -- Delivery
    output_target TEXT,             -- 'talk', 'email', or NULL (default: inferred from source_type)

    -- Talk message tracking (for reply context)
    talk_message_id INTEGER,        -- Talk API ID of the user's incoming message
    talk_response_id INTEGER,       -- Talk API ID of bot's response message
    reply_to_talk_id INTEGER,       -- Talk API ID of the message being replied to
    reply_to_content TEXT,          -- Fallback text of replied-to message (when parent task not in DB)

    -- Execution control
    cancel_requested INTEGER DEFAULT 0,  -- Flag to signal task cancellation
    worker_pid INTEGER,                  -- PID of worker process
    last_heartbeat TEXT,                 -- Liveness ping from the running worker (ISSUE-112)

    -- Silent mode (for scheduled jobs with silent_unless_action)
    heartbeat_silent INTEGER DEFAULT 0,  -- Whether to suppress output on no-action

    -- Log channel suppression (per-task opt-out)
    skip_log_channel INTEGER DEFAULT 0,  -- Whether to suppress log channel output

    -- Scheduled job tracking
    scheduled_job_id INTEGER,       -- Links task back to originating scheduled job

    -- Worker queue (foreground = interactive, background = scheduled/briefing/subtask)
    queue TEXT NOT NULL DEFAULT 'foreground',

    -- Skill selection tracking (JSON array of skill names)
    selected_skills TEXT,

    -- Per-task model override (e.g. "claude-sonnet-4-6"); empty = use config default
    model TEXT,
    -- Per-task effort override (low/medium/high/xhigh/max); empty = use config default
    effort TEXT,
    -- The model the brain actually ran (resolved canonical ID), recorded post-run.
    -- Distinct from `model`: stays NULL for default-model tasks so retries
    -- re-resolve the current default; surfaces (web-chat meta) read this.
    model_used TEXT,

    -- Real Talk room for this task's notifications. Distinct from
    -- conversation_token, which doubles as an email-thread grouping key for
    -- email-source tasks. NULL falls back to conversation_token at delivery time.
    talk_delivery_token TEXT,

    -- Skill-task dispatch (Phase 1.3 of unified credential resolution).
    -- When skill is non-NULL the scheduler runs `python -m istota.skills.<skill>`
    -- with skill_args (JSON list[str]) and credentials pre-resolved on the
    -- trusted side. Mutually exclusive with prompt and command.
    skill TEXT,
    skill_args TEXT,

    FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_scheduled ON tasks(scheduled_for) WHERE scheduled_for IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(queue, status);
CREATE INDEX IF NOT EXISTS idx_tasks_created_at ON tasks(created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_user_created ON tasks(user_id, created_at);

-- User resource permissions
CREATE TABLE IF NOT EXISTS user_resources (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,    -- 'calendar', 'folder', 'email_folder', 'todo_file'
    resource_path TEXT NOT NULL,
    display_name TEXT,
    permissions TEXT DEFAULT 'read', -- 'read', 'write'
    extras TEXT,                     -- JSON dict of resource-type-specific config (e.g. overland ingest_token, money config_path)
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, resource_type, resource_path)
);

CREATE INDEX IF NOT EXISTS idx_user_resources_user ON user_resources(user_id);

-- Briefing configurations
CREATE TABLE IF NOT EXISTS briefing_configs (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,             -- 'morning', 'evening', etc.
    cron_expression TEXT NOT NULL,  -- '0 7 * * 1-5' for 7am weekdays
    conversation_token TEXT NOT NULL,
    components TEXT NOT NULL,       -- JSON: {"calendar": true, "email": true, "todos": true, "news": {"senders": ["newsletter@example.com"], "max_age_hours": 6}}
    enabled INTEGER DEFAULT 1,
    last_run_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, name)
);

-- Task logs for observability
CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY,
    task_id INTEGER NOT NULL,
    timestamp TEXT DEFAULT (datetime('now')),
    level TEXT NOT NULL,            -- 'debug', 'info', 'warn', 'error'
    message TEXT NOT NULL,
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_task_logs_task ON task_logs(task_id);

-- Task event stream — real-time, persisted observability for every output
-- surface (web SSE, Talk, push, log channel, admin). seq is monotonic per
-- task_id and assigned by the EventWriter. The ON DELETE CASCADE clause is
-- documentation only: SQLite enforces FKs only with PRAGMA foreign_keys=ON,
-- which istota never sets, so task_events is hand-deleted in cleanup_old_tasks
-- and on retry (see db.delete_task_events).
CREATE TABLE IF NOT EXISTS task_events (
    id          INTEGER PRIMARY KEY,
    task_id     INTEGER NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
    seq         INTEGER NOT NULL,
    kind        TEXT NOT NULL,
    payload     TEXT NOT NULL DEFAULT '{}',  -- JSON
    created_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
    UNIQUE (task_id, seq)
);

CREATE INDEX IF NOT EXISTS idx_task_events_task_seq ON task_events (task_id, seq);

-- Processed emails (to avoid duplicate processing)
CREATE TABLE IF NOT EXISTS processed_emails (
    id INTEGER PRIMARY KEY,
    email_id TEXT NOT NULL UNIQUE,
    sender_email TEXT NOT NULL,
    subject TEXT,
    thread_id TEXT,  -- for conversation context grouping
    message_id TEXT,  -- RFC 5322 Message-ID for reply threading
    "references" TEXT,  -- RFC 5322 References header for thread chain
    user_id TEXT,
    task_id INTEGER,
    routing_method TEXT,  -- plus_address, sender_match, thread_match, discarded
    processed_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_processed_emails_email_id ON processed_emails(email_id);
CREATE INDEX IF NOT EXISTS idx_processed_emails_thread_id ON processed_emails(thread_id);

-- Briefing state (tracks last_run_at for config-based briefings)
CREATE TABLE IF NOT EXISTS briefing_state (
    user_id TEXT NOT NULL,
    briefing_name TEXT NOT NULL,
    last_run_at TEXT,
    PRIMARY KEY (user_id, briefing_name)
);

-- Talk polling state (tracks last message ID per conversation for polling)
CREATE TABLE IF NOT EXISTS talk_poll_state (
    conversation_token TEXT PRIMARY KEY,
    last_known_message_id INTEGER NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- TASKS.md file tasks (tracks tasks from user's TASKS.md files)
CREATE TABLE IF NOT EXISTS istota_file_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    original_line TEXT NOT NULL,
    normalized_content TEXT NOT NULL,
    status TEXT DEFAULT 'pending',
    task_id INTEGER,
    result_summary TEXT,
    error_message TEXT,
    attempt_count INTEGER DEFAULT 0,
    max_attempts INTEGER DEFAULT 3,
    file_path TEXT NOT NULL,
    created_at TEXT DEFAULT (datetime('now')),
    started_at TEXT,
    completed_at TEXT,
    UNIQUE(user_id, content_hash),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_istota_file_tasks_user ON istota_file_tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_istota_file_tasks_status ON istota_file_tasks(status);

-- Scheduled recurring jobs (managed at runtime via sqlite3)
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    cron_expression TEXT NOT NULL,
    prompt TEXT NOT NULL DEFAULT '',
    command TEXT,                    -- Shell command (mutually exclusive with prompt)
    conversation_token TEXT,
    output_target TEXT,             -- 'talk', 'email', or NULL
    enabled INTEGER DEFAULT 1,
    last_run_at TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    silent_unless_action INTEGER DEFAULT 0,  -- Suppress output unless ACTION: prefix
    consecutive_failures INTEGER DEFAULT 0,
    last_error TEXT,
    last_success_at TEXT,
    once INTEGER DEFAULT 0,                 -- One-time job: auto-removed after successful execution
    skip_log_channel INTEGER DEFAULT 0,     -- Suppress log channel output for tasks from this job
    model TEXT,                             -- Per-job model override; empty = use config default
    effort TEXT,                            -- Per-job effort override; empty = use config default
    -- Skill-task dispatch (Phase 1.3). Mutually exclusive with command.
    skill TEXT,
    skill_args TEXT,
    UNIQUE(user_id, name)
);

CREATE INDEX IF NOT EXISTS idx_scheduled_jobs_user ON scheduled_jobs(user_id);

-- Sleep cycle state (tracks last run for nightly memory extraction)
CREATE TABLE IF NOT EXISTS sleep_cycle_state (
    user_id TEXT PRIMARY KEY,
    last_run_at TEXT,
    last_processed_task_id INTEGER
);

-- Heartbeat monitoring state (tracks check execution and alerting)
CREATE TABLE IF NOT EXISTS heartbeat_state (
    user_id TEXT NOT NULL,
    check_name TEXT NOT NULL,
    last_check_at TEXT,           -- When check was last evaluated
    last_alert_at TEXT,           -- When last alert was sent (for cooldown)
    last_healthy_at TEXT,         -- When check last passed (for recovery detection)
    last_error_at TEXT,           -- When check implementation itself failed
    consecutive_errors INTEGER DEFAULT 0,
    PRIMARY KEY (user_id, check_name)
);

-- Reminder rotation state (tracks shuffle queue for briefing reminders)
CREATE TABLE IF NOT EXISTS reminder_state (
    user_id TEXT PRIMARY KEY,
    queue TEXT NOT NULL,          -- JSON array of remaining reminder indices
    content_hash TEXT NOT NULL,   -- Hash of reminders content (reset queue on change)
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Monarch Money API-synced transactions (deduplication + reconciliation tracking)
CREATE TABLE IF NOT EXISTS monarch_synced_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    monarch_transaction_id TEXT NOT NULL,
    synced_at TEXT DEFAULT (datetime('now')),
    -- Reconciliation tracking (added for tag change detection)
    tags_json TEXT,                -- JSON array of tags at sync time
    amount REAL,                   -- Transaction amount for reversal
    merchant TEXT,                 -- Merchant name for reversal narration
    posted_account TEXT,           -- Beancount expense account posted to
    txn_date TEXT,                 -- Transaction date (YYYY-MM-DD)
    recategorized_at TEXT,         -- When reversal was created (NULL if still valid)
    content_hash TEXT,             -- SHA-256 of date+amount+merchant for cross-source dedup
    UNIQUE(user_id, monarch_transaction_id)
);

CREATE INDEX IF NOT EXISTS idx_monarch_synced_user ON monarch_synced_transactions(user_id);
CREATE INDEX IF NOT EXISTS idx_monarch_synced_active ON monarch_synced_transactions(user_id)
    WHERE recategorized_at IS NULL;

-- CSV imported transactions (deduplication via content hash)
CREATE TABLE IF NOT EXISTS csv_imported_transactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,   -- SHA-256 of date+amount+merchant+account
    source_file TEXT,             -- Original filename for reference
    imported_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_csv_imported_user ON csv_imported_transactions(user_id);

-- Channel sleep cycle state (tracks last run for channel-level memory extraction)
CREATE TABLE IF NOT EXISTS channel_sleep_cycle_state (
    conversation_token TEXT PRIMARY KEY,
    last_run_at TEXT,
    last_processed_task_id INTEGER
);

-- Memory search chunks (hybrid BM25 + vector search)
CREATE TABLE IF NOT EXISTS memory_chunks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    source_type TEXT NOT NULL,        -- conversation, memory_file, user_memory, channel_memory
    source_id TEXT NOT NULL,          -- task_id or file path
    chunk_index INTEGER NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,       -- SHA-256 for dedup
    metadata_json TEXT,
    topic TEXT,                       -- coarse classifier: work, tech, personal, finance, admin, learning, meta
    entities TEXT,                    -- JSON array of entity names (lowercase)
    valid_from TEXT,                  -- episode window open (ISSUE-109 #2); NULL = standing
    valid_until TEXT,                 -- episode window close; chunk suppressed from recall once passed
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_memory_chunks_user ON memory_chunks(user_id);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_source ON memory_chunks(user_id, source_type, source_id);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_topic ON memory_chunks(user_id, topic);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_valid_until ON memory_chunks(user_id, valid_until) WHERE valid_until IS NOT NULL;

-- Per-user skills version fingerprint (for "what's new" detection)
CREATE TABLE IF NOT EXISTS user_skills_fingerprint (
    user_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- FTS5 external content table (synced via triggers, no content duplication)
CREATE VIRTUAL TABLE IF NOT EXISTS memory_chunks_fts USING fts5(
    content, content='memory_chunks', content_rowid='id'
);

-- Triggers to keep FTS5 in sync with memory_chunks
CREATE TRIGGER IF NOT EXISTS memory_chunks_ai AFTER INSERT ON memory_chunks BEGIN
    INSERT INTO memory_chunks_fts(rowid, content) VALUES (new.id, new.content);
END;

CREATE TRIGGER IF NOT EXISTS memory_chunks_ad AFTER DELETE ON memory_chunks BEGIN
    INSERT INTO memory_chunks_fts(memory_chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
END;

CREATE TRIGGER IF NOT EXISTS memory_chunks_au AFTER UPDATE ON memory_chunks BEGIN
    INSERT INTO memory_chunks_fts(memory_chunks_fts, rowid, content) VALUES ('delete', old.id, old.content);
    INSERT INTO memory_chunks_fts(rowid, content) VALUES (new.id, new.content);
END;

-- Talk message cache (poller-fed, replaces per-task API fetches for context)
CREATE TABLE IF NOT EXISTS talk_messages (
    message_id INTEGER NOT NULL,
    conversation_token TEXT NOT NULL,
    actor_id TEXT NOT NULL DEFAULT '',
    actor_display_name TEXT NOT NULL DEFAULT '',
    actor_type TEXT NOT NULL DEFAULT 'users',
    message_text TEXT NOT NULL DEFAULT '',
    message_type TEXT NOT NULL DEFAULT 'comment',
    message_parameters TEXT,  -- JSON string (dict or list)
    timestamp INTEGER NOT NULL DEFAULT 0,
    reference_id TEXT,
    deleted INTEGER DEFAULT 0,
    parent_id INTEGER,
    PRIMARY KEY (conversation_token, message_id)
);

-- Key-value store for script runtime state (scoped by user and namespace)
CREATE TABLE IF NOT EXISTS istota_kv (
    user_id TEXT NOT NULL,
    namespace TEXT NOT NULL,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, namespace, key)
);

CREATE INDEX IF NOT EXISTS idx_istota_kv_ns ON istota_kv(user_id, namespace);

-- Trusted email senders (runtime-managed via !trust command / confirmation flow)
CREATE TABLE IF NOT EXISTS trusted_email_senders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    sender_email TEXT NOT NULL,         -- Exact email address (lowercase)
    added_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, sender_email)
);

CREATE INDEX IF NOT EXISTS idx_trusted_email_senders_user ON trusted_email_senders(user_id);

-- Sent emails (outbound email tracking for emissary thread matching)
CREATE TABLE IF NOT EXISTS sent_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    task_id INTEGER,
    message_id TEXT NOT NULL,          -- Generated RFC 5322 Message-ID
    to_addr TEXT NOT NULL,
    subject TEXT,
    thread_id TEXT,                    -- Computed thread ID (same algo as email_poller)
    in_reply_to TEXT,                  -- If this was a reply to another message
    "references" TEXT,                 -- RFC 5322 References thread chain
    conversation_token TEXT,           -- Talk conversation where send was requested
    talk_delivery_token TEXT,          -- Originating task's resolved Talk room (real, not synthetic)
    origin_target TEXT,                -- output_target descriptor of the originating surface (web:tok / talk:tok)
    sent_at TEXT DEFAULT (datetime('now')),
    FOREIGN KEY (task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_sent_emails_message_id ON sent_emails(message_id);
CREATE INDEX IF NOT EXISTS idx_sent_emails_user ON sent_emails(user_id);

-- Note: Per-user location data (location_pings, places, visits,
-- dismissed_clusters, location_state) lives in per-user
-- {workspace}/location/data/location.db files; see src/istota/location/.
-- Framework istota.db keeps only the global geocode caches below.

-- Geocode cache (forward geocoding results for calendar event locations)
CREATE TABLE IF NOT EXISTS geocode_cache (
    location_text TEXT PRIMARY KEY,
    lat REAL NOT NULL,
    lon REAL NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Reverse geocode cache (coords → place name via Nominatim)
CREATE TABLE IF NOT EXISTS reverse_geocode_cache (
    lat_rounded REAL NOT NULL,
    lon_rounded REAL NOT NULL,
    display_name TEXT,
    neighborhood TEXT,
    suburb TEXT,
    road TEXT,
    city TEXT,
    raw_json TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (lat_rounded, lon_rounded)
);

-- Google OAuth tokens (per-user Google Workspace access). access_token and
-- refresh_token are stored as Fernet ciphertext (BLOB) keyed off
-- $ISTOTA_SECRET_KEY -- same primitive as the `secrets` table. SQLite is
-- declared-type-lenient, so existing deployments with TEXT columns keep
-- working; the migration in db._run_migrations encrypts any plaintext rows
-- in place on first run with a key available.
CREATE TABLE IF NOT EXISTS google_oauth_tokens (
    user_id TEXT PRIMARY KEY,
    access_token BLOB NOT NULL,
    refresh_token BLOB NOT NULL,
    token_expiry TEXT NOT NULL,     -- ISO 8601 datetime
    scopes TEXT NOT NULL DEFAULT '[]',  -- JSON array of granted scopes
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Knowledge graph (temporal entity-relationship triples)
CREATE TABLE IF NOT EXISTS knowledge_facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    subject TEXT NOT NULL,
    predicate TEXT NOT NULL,
    object TEXT NOT NULL,
    valid_from TEXT,
    valid_until TEXT,
    temporary INTEGER DEFAULT 0,
    confidence REAL DEFAULT 1.0,
    source_task_id INTEGER,
    source_type TEXT DEFAULT 'extracted',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_kf_user_subject ON knowledge_facts(user_id, subject);
CREATE INDEX IF NOT EXISTS idx_kf_user_predicate ON knowledge_facts(user_id, predicate);
CREATE INDEX IF NOT EXISTS idx_kf_current ON knowledge_facts(user_id, valid_until)
    WHERE valid_until IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS idx_kf_unique_current
    ON knowledge_facts(user_id, subject, predicate, object)
    WHERE valid_until IS NULL;

-- Audit trail for knowledge_facts mutations. Captures inserts,
-- supersessions, fuzzy-dedup skips, invalidations, and deletes so users can
-- inspect why a fact arrived or disappeared. Pruned on the unified retention
-- sweep at 4x the user memory retention window.
CREATE TABLE IF NOT EXISTS knowledge_facts_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    fact_id INTEGER,
    op TEXT NOT NULL,
    before_json TEXT,
    after_json TEXT,
    source_task_id INTEGER,
    source_type TEXT,
    ts TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_kfa_user_ts ON knowledge_facts_audit(user_id, ts);
CREATE INDEX IF NOT EXISTS idx_kfa_fact_id ON knowledge_facts_audit(fact_id);

-- Tier-2 credentials (web-UI-managed, encrypted at rest with a Fernet key
-- derived from $ISTOTA_SECRET_KEY). One row per (user, service, key) — e.g.
-- ("alice", "monarch", "email"), ("alice", "monarch", "password"),
-- ("alice", "karakeep", "api_key"). Encrypted_value is a Fernet token (bytes).
-- last_accessed_at is bumped on read so admins can see which secrets are
-- live vs stale.
CREATE TABLE IF NOT EXISTS secrets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    service TEXT NOT NULL,
    key TEXT NOT NULL,
    encrypted_value BLOB NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now')),
    last_accessed_at TEXT,
    UNIQUE(user_id, service, key)
);
CREATE INDEX IF NOT EXISTS idx_secrets_user_service ON secrets(user_id, service);

-- User profiles (Phase 6 of the Docker onboarding spec).
-- Replaces per-user TOML files (config/users/{user}.toml). Resource entries
-- still live in config.toml under [[users.X.resources]] (deployment-level
-- topology, ansible-managed); profile fields move here so the web UI can
-- write them without touching disk and so Docker can auto-seed at first
-- login. See `.claude/rules/config.md`.
--
-- list/dict columns store JSON arrays/objects.
-- Empty/zero values mean "use defaults" (matches UserConfig dataclass).
CREATE TABLE IF NOT EXISTS user_profiles (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    email_addresses TEXT NOT NULL DEFAULT '[]',          -- JSON array
    timezone TEXT NOT NULL DEFAULT 'UTC',
    log_channel TEXT NOT NULL DEFAULT '',                -- Talk room token
    alerts_channel TEXT NOT NULL DEFAULT '',             -- Talk room token
    site_enabled INTEGER NOT NULL DEFAULT 0,
    max_foreground_workers INTEGER NOT NULL DEFAULT 0,   -- 0 = use global default
    max_background_workers INTEGER NOT NULL DEFAULT 0,
    disabled_skills TEXT NOT NULL DEFAULT '[]',          -- JSON array
    trusted_email_senders TEXT NOT NULL DEFAULT '[]',    -- JSON array (patterns)
    disabled_modules TEXT NOT NULL DEFAULT '[]',         -- JSON array (default-on otherwise)
    routing TEXT NOT NULL DEFAULT '{}',                  -- JSON object: purpose -> output_target descriptor
    default_destination TEXT NOT NULL DEFAULT 'talk',    -- fallback delivery descriptor
    email_reply_routing TEXT NOT NULL DEFAULT 'origin+thread', -- email-reply mirror policy: origin+thread | origin | thread
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Web chat rooms (in-app chat surface). Each room owns a per-user channel
-- token used as the task's conversation_token, so every room gets its own
-- CHANNEL.md memory and sleep-cycle treatment with no special-casing.
-- Always-on surface: there is no per-user opt-out.
CREATE TABLE IF NOT EXISTS web_chat_rooms (
    id          INTEGER PRIMARY KEY,
    user_id     TEXT NOT NULL,
    token       TEXT NOT NULL UNIQUE,            -- conversation_token (channel id)
    name        TEXT NOT NULL,
    archived    INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_web_chat_rooms_user
    ON web_chat_rooms (user_id, archived, id);

-- Unsolicited (bot-delivered) messages posted into a web chat room: alerts,
-- the verbose execution log, and any notification routed to the `web` surface.
-- Unlike task-backed chat turns (in `tasks`) these have no originating user
-- prompt, so they render as a single system message merged into the transcript.
CREATE TABLE IF NOT EXISTS web_chat_messages (
    id          INTEGER PRIMARY KEY,
    user_id     TEXT NOT NULL,
    token       TEXT NOT NULL,                  -- room channel id (web_chat_rooms.token)
    role        TEXT NOT NULL DEFAULT 'system',
    title       TEXT,
    text        TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_web_chat_messages_token
    ON web_chat_messages (token, id);
