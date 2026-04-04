# Database

Istota uses SQLite with WAL mode for concurrent access. All operations live in `db.py`. The schema is defined in `schema.sql`.

## Tables

### Core

| Table | Purpose |
|---|---|
| `tasks` | Task queue with full lifecycle: id, status, source_type, user_id, prompt, conversation_token, priority, attempts, execution trace |
| `user_resources` | Per-user resource permissions (calendar, folder, todo_file, email_folder, ledger, etc.) |
| `task_logs` | Structured task-level observability |
| `istota_kv` | Key-value store for script runtime state |

### Messaging

| Table | Purpose |
|---|---|
| `talk_poll_state` | Last message ID per Talk conversation |
| `talk_messages` | Poller-fed message cache for conversation context |
| `processed_emails` | Email dedup with RFC 5322 thread tracking |
| `sent_emails` | Outbound email tracking for emissary thread matching |

### Scheduling

| Table | Purpose |
|---|---|
| `scheduled_jobs` | Cron job definitions (synced from CRON.md) |
| `briefing_configs` | DB-stored briefing configurations |
| `briefing_state` | Last-run timestamps per briefing per user |
| `istota_file_tasks` | Tasks sourced from TASKS.md files (content-hash identity) |

### Memory

| Table | Purpose |
|---|---|
| `sleep_cycle_state` | Per-user nightly memory extraction state |
| `channel_sleep_cycle_state` | Per-channel memory extraction state |
| `memory_chunks` | Text chunks for hybrid search |
| `memory_chunks_fts` | FTS5 virtual table (trigger-synced from memory_chunks) |
| `user_skills_fingerprint` | Skills version tracking for "what's new" |

### Monitoring

| Table | Purpose |
|---|---|
| `heartbeat_state` | Per-check monitoring state (timestamps, consecutive errors) |
| `reminder_state` | Shuffle queue for briefing reminders |

### Tracking

| Table | Purpose |
|---|---|
| `monarch_synced_transactions` | Monarch Money sync dedup |
| `csv_imported_transactions` | CSV import dedup |
| `invoice_schedule_state` | Automated invoice generation timing |
| `invoice_overdue_notified` | Prevents duplicate overdue alerts |

### Feeds

| Table | Purpose |
|---|---|
| `feed_state` | RSS/Tumblr/Are.na polling state |
| `feed_items` | Aggregated feed content |

### Location

| Table | Purpose |
|---|---|
| `location_pings` | Raw GPS data from Overland webhook |
| `places` | Named geofences |
| `visits` | Detected place visits |
| `location_state` | Per-user location tracking state |

## Key operations

### Task lifecycle

```python
create_task(conn, prompt, user_id, source_type="cli", ...)  # -> task_id
claim_task(conn, worker_id, user_id=None)                    # -> Task | None
update_task_status(conn, task_id, status, result=None, ...)  # completed/failed
set_task_pending_retry(conn, task_id, error, delay_minutes)  # exponential backoff
set_task_confirmation(conn, task_id, confirmation_prompt)     # -> pending_confirmation
cancel_task(conn, task_id)                                    # sets cancel_requested
```

### Conversation history

```python
get_conversation_history(conn, token, exclude_task_id=None, limit=10)
# Returns: list[ConversationMessage(id, prompt, result, created_at, actions_taken)]
```

### Cleanup

```python
expire_stale_confirmations(conn, timeout_minutes)  # -> list of expired tasks
fail_ancient_pending_tasks(conn, fail_hours)        # -> list of failed tasks
cleanup_old_tasks(conn, retention_days)             # -> count deleted
```

## WAL mode

SQLite WAL mode allows concurrent reads from multiple threads (talk poller, workers, CLI) while the scheduler thread writes. Each worker creates fresh DB connections per call.

## Schema initialization

The schema is applied via `schema.sql`. The CLI command `istota init` creates the database and applies the schema.
