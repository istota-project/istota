# Database

Istota uses SQLite with WAL mode for concurrent access. All operations live in `db.py`. The schema is defined in `schema.sql`.

## Tables

### Core

| Table | Purpose |
|---|---|
| `tasks` | Task queue with full lifecycle: id, status, source_type, user_id, prompt, conversation_token, talk_delivery_token, priority, attempts, `last_heartbeat` (worker-liveness ping for stuck-task reclaim, ISSUE-112), execution trace, model/effort overrides, plus `skill` / `skill_args` for skill-task dispatch |
| `user_resources` | Per-user folder mounts (`folder`) + internal `shared_file` organizer state |
| `user_profiles` | Per-user profile fields (display_name, timezone, channels, worker overrides, disabled_skills, disabled_modules, email_addresses, trusted_email_senders) |
| `briefing_configs` | DB-stored briefing configurations (cron, components, conversation_token, enabled flag) |
| `secrets` | Per-user encrypted credentials (Fernet over scrypt-derived `ISTOTA_SECRET_KEY`) |
| `google_oauth_tokens` | Google OAuth access/refresh token pairs (Fernet-encrypted at rest) |
| `task_logs` | Structured task-level observability |
| `istota_kv` | Key-value store for script runtime state |

### Messaging

| Table | Purpose |
|---|---|
| `talk_poll_state` | Last message ID per Talk conversation |
| `talk_messages` | Poller-fed message cache for conversation context |
| `processed_emails` | Email dedup with RFC 5322 thread tracking |
| `sent_emails` | Outbound email tracking for emissary thread matching |
| `task_events` | Task-event-streaming log: `id, task_id, seq, kind, payload (JSON), created_at`, `UNIQUE(task_id, seq)`. One persisted, typed event stream per task feeding Talk / SSE / log / push consumers. `seq` is monotonic per task (writer-assigned, resumed across retries via `get_max_task_event_seq`); rows are deleted only by `cleanup_old_tasks` (retention) |

### Web chat (per-user rooms)

| Table | Purpose |
|---|---|
| `web_chat_rooms` | One row per web chat room: `id, user_id, token (channel id), name, archived, created_at, updated_at`. One room = one `conversation_token`, each with its own `CHANNEL.md` |
| `web_chat_messages` | Bot-delivered (unsolicited) room messages — alerts / logs / notifications routed to the `web` surface via `WebTransport.deliver`: `id, user_id, token, role, title, text, created_at`. Distinct from task-backed turns; merged into room history by time |

### Rooms (unified Talk/web)

The unified Talk/web room-sync model (defined in `schema.sql`) supersedes the de-facto tasks-as-history store with a surface-neutral room + message model.

| Table | Purpose |
|---|---|
| `rooms` | Canonical room registry keyed on `conversation_token`; `origin` (talk\|web), display name, `archived` flag |
| `room_bindings` | One row per (room, surface) exposing a room; maps canonical token to each surface's ref |
| `messages` | Canonical transcript (role user\|assistant\|system, `task_id`, `origin_surface`, `external_ids` mirror ledger) |
| `room_members` | Per-user membership of a shared room; web visibility resolves through this, not the single-owner `rooms.user_id` |
| `room_dismissals` | Per-user "hide this room" tombstone, cleared by the user's own next inbound |
| `room_read_state` | Per-surface, per-user read cursors driving unread badges |
| `trusted_email_senders` | Per-user fnmatch allowlist for the email trust gate |

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
| `memory_chunks` | Text chunks for hybrid search; carries `valid_from` / `valid_until` episode-window columns (ISSUE-109) so a chunk whose episode has closed self-suppresses from recall |
| `memory_chunks_fts` | FTS5 virtual table (trigger-synced from memory_chunks) |
| `knowledge_facts` | Temporal subject/predicate/object triples (freeform predicates, fuzzy dedup); `valid_from` / `valid_until` bound a fact's currency |
| `knowledge_facts_audit` | Append-only audit trail of KG fact add/invalidate/delete ops |
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

Invoice timing tables (`invoice_schedule_state`, `invoice_overdue_notified`) live in the per-user money DB (`money/db.py`), not the framework `istota.db`.

### Feeds (per-user feeds.db)

| Table | Purpose |
|---|---|
| `feed_categories` | User-defined feed categories |
| `feeds` | Subscribed RSS/Atom/Tumblr/Are.na sources + per-feed poll state |
| `feed_entries` | Aggregated feed content + read/star state |
| `schema_meta` | Schema version + global default poll interval |

### Location (per-user location.db)

Location tables live in per-user `{workspace}/location/data/location.db` files, not in the framework DB. The module package at `src/istota/location/` provides `resolve_for_user(user_id, config)`.

| Table | Purpose |
|---|---|
| `location_pings` | Raw GPS data from Overland webhook |
| `places` | Named geofences |
| `visits` | Detected place visits |
| `location_state` | Per-user location tracking state |
| `dismissed_clusters` | Clusters the user chose not to save as places |

The two Nominatim caches (`geocode_cache`, `reverse_geocode_cache`) remain in the framework `istota.db` for cross-user dedup.

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

## Single source of truth for `Task` columns

Every `Task`-returning helper (`claim_task`, `get_task`, `get_pending_confirmation*`, `get_reply_parent_task`, `get_stale_pending_tasks`, `get_completed_*_since`) routes its `SELECT` / `RETURNING` clause through a single `_TASK_COLUMNS` constant. Adding a column means editing one place; missing columns now raise `IndexError` rather than silently returning `None`.

## WAL mode

SQLite WAL mode allows concurrent reads from multiple threads (talk poller, workers, CLI) while the scheduler thread writes. Each worker creates fresh DB connections per call.

## Schema initialization

The schema is applied via `schema.sql`. The CLI command `istota init` creates the database and applies the schema.
