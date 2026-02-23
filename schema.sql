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

    -- Silent mode (for scheduled jobs with silent_unless_action)
    heartbeat_silent INTEGER DEFAULT 0,  -- Whether to suppress output on no-action

    -- Scheduled job tracking
    scheduled_job_id INTEGER,       -- Links task back to originating scheduled job

    -- Worker queue (foreground = interactive, background = scheduled/briefing/subtask)
    queue TEXT NOT NULL DEFAULT 'foreground',

    FOREIGN KEY (parent_task_id) REFERENCES tasks(id)
);

CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_scheduled ON tasks(scheduled_for) WHERE scheduled_for IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tasks_user ON tasks(user_id);
CREATE INDEX IF NOT EXISTS idx_tasks_queue ON tasks(queue, status);

-- User resource permissions
CREATE TABLE IF NOT EXISTS user_resources (
    id INTEGER PRIMARY KEY,
    user_id TEXT NOT NULL,
    resource_type TEXT NOT NULL,    -- 'calendar', 'folder', 'email_folder', 'todo_file'
    resource_path TEXT NOT NULL,
    display_name TEXT,
    permissions TEXT DEFAULT 'read', -- 'read', 'write'
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

-- Invoice overdue notification tracking (prevents duplicate notifications)
CREATE TABLE IF NOT EXISTS invoice_overdue_notified (
    user_id TEXT NOT NULL,
    invoice_number TEXT NOT NULL,
    notified_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (user_id, invoice_number)
);

-- Invoice schedule state (tracks automated invoice generation/reminder timing)
CREATE TABLE IF NOT EXISTS invoice_schedule_state (
    user_id TEXT NOT NULL,
    client_key TEXT NOT NULL,
    last_reminder_at TEXT,     -- When reminder was last sent
    last_generation_at TEXT,   -- When invoices were last generated
    PRIMARY KEY (user_id, client_key)
);

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
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_memory_chunks_user ON memory_chunks(user_id);
CREATE INDEX IF NOT EXISTS idx_memory_chunks_source ON memory_chunks(user_id, source_type, source_id);

-- Per-user skills version fingerprint (for "what's new" detection)
CREATE TABLE IF NOT EXISTS user_skills_fingerprint (
    user_id TEXT PRIMARY KEY,
    fingerprint TEXT NOT NULL,
    updated_at TEXT DEFAULT (datetime('now'))
);

-- Feed polling state (tracks per-feed polling progress)
CREATE TABLE IF NOT EXISTS feed_state (
    user_id TEXT NOT NULL,
    feed_name TEXT NOT NULL,
    last_poll_at TEXT,
    last_item_id TEXT,
    etag TEXT,
    last_modified TEXT,
    consecutive_errors INTEGER DEFAULT 0,
    last_error TEXT,
    PRIMARY KEY (user_id, feed_name)
);

-- Feed items (aggregated content from RSS, Tumblr, Are.na feeds)
CREATE TABLE IF NOT EXISTS feed_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    feed_name TEXT NOT NULL,
    item_id TEXT NOT NULL,
    title TEXT,
    url TEXT,
    content_text TEXT,
    content_html TEXT,
    image_url TEXT,
    author TEXT,
    published_at TEXT,
    fetched_at TEXT DEFAULT (datetime('now')),
    UNIQUE(user_id, feed_name, item_id)
);

CREATE INDEX IF NOT EXISTS idx_feed_items_user ON feed_items(user_id, feed_name);

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
