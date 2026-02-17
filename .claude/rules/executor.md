# Executor Internals

## `execute_task()` (L383-749)
```python
def execute_task(
    task: db.Task, config: Config, user_resources: list[db.UserResource],
    dry_run: bool = False, use_context: bool = True,
    conn: "db.sqlite3.Connection | None" = None,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[bool, str, str | None]:
```
Returns `(success, result_text, actions_taken_json)`. `actions_taken` is a JSON array of tool use descriptions from streaming execution, or `None` for simple/dry-run/error paths.

### Flow
1. **Setup temp dir** (L401-403): `config.temp_dir / task.user_id`
2. **Merge resources** (L405-415): DB resources + config resources → `db.UserResource` list
3. **Load skills** (L417-442): `load_skill_index()` → `select_skills()` → `load_skills()`
4. **Skills changelog** (L443-464): fingerprint compare, interactive only
5. **Context loading** (L466-549): skip for scheduled/briefing
6. **User memory** (L551-564): `read_user_memory_v2()`, skip for briefings
7. **Channel memory** (L566-574): `read_channel_memory()`, only if `conversation_token`
8. **CalDAV discovery** (L576-588): `get_calendars_for_user()`
9. **Build prompt** (L600-605)
10. **Dry run check** (L618-619): return prompt text
11. **Write prompt file** (L621-623): `task_{id}_prompt.txt`
12. **Build command**: `--allowedTools` (restricted) or `--dangerously-skip-permissions` (permissive)
13. **Build env** (L643-725): see env var table below
14. **Execute** (L726-729): streaming or simple
15. **Update fingerprint** (L731-742): on success, interactive only

## `build_prompt()` (L166-380)
```python
def build_prompt(
    task: db.Task, user_resources: list[db.UserResource], config: Config,
    skills_doc: str | None = None, conversation_context: str | None = None,
    user_memory: str | None = None, discovered_calendars: list[tuple[str, str, bool]] | None = None,
    user_email_addresses: list[str] | None = None, dated_memories: str | None = None,
    channel_memory: str | None = None, skills_changelog: str | None = None,
) -> str:
```

### Prompt Section Order
1. Header: role, user_id, datetime, task_id, conversation_token, db_path (L341-346)
2. Persona: user workspace `PERSONA.md` overrides `config/persona.md` (L244-248)
3. Resources: calendars, folders, todos, email_folders, notes, reminders (L180-242)
4. User memory: USER.md (L267-276)
5. Channel memory: CHANNEL.md (L278-288)
6. Dated memories: (reserved, currently None) (L290-298)
7. Tools: file access, browser, CalDAV, sqlite3, email (L312-357)
8. Rules: resource restrictions, confirmation, subtasks, output (L359-367)
9. Context: previous messages (L300-310)
10. Request: prompt + attachments (L369-371)
11. Guidelines: `config/guidelines/{source_type}.md` (L250-254)
12. Skills changelog (L374-375)
13. Skills doc (L377-378)

## Environment Variable Mapping (L643-725)

| Resource/System | Env Var | Source |
|---|---|---|
| Core | `ISTOTA_TASK_ID` | `str(task.id)` |
| Core | `ISTOTA_USER_ID` | `task.user_id` |
| Core | `ISTOTA_DB_PATH` | `str(config.db_path)` |
| Core | `ISTOTA_CONVERSATION_TOKEN` | `task.conversation_token` |
| Core | `ISTOTA_DEFERRED_DIR` | `str(user_temp_dir)` — always set, for deferred DB writes |
| Nextcloud | `NC_URL`, `NC_USER`, `NC_PASS` | `config.nextcloud.*` |
| Nextcloud | `NEXTCLOUD_MOUNT_PATH` | `str(config.nextcloud_mount_path)` |
| CalDAV | `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD` | `config.caldav_*` |
| Browser | `BROWSER_API_URL`, `BROWSER_VNC_URL` | `config.browser.*` (if enabled) |
| Email | `SMTP_HOST/PORT/USER/PASSWORD`, `SMTP_FROM` | `config.email.*` |
| Email | `IMAP_HOST/PORT/USER/PASSWORD` | `config.email.*` |
| Ledger | `LEDGER_PATHS` | JSON array `[{"name": ..., "path": ...}]` |
| Ledger | `LEDGER_PATH` | First ledger path (backward compat) |
| Invoicing | `INVOICING_CONFIG` | Path to INVOICING.md (auto-created from template) |
| Accounting | `ACCOUNTING_CONFIG` | Path to ACCOUNTING.md (auto-created from template) |
| Developer | `DEVELOPER_REPOS_DIR` | `config.developer.repos_dir` (if enabled) |
| Developer | `GITLAB_URL` | `config.developer.gitlab_url` (if enabled) |
| Developer | `GITLAB_DEFAULT_NAMESPACE` | `config.developer.gitlab_default_namespace` (if enabled + set) |
| Developer | `GITLAB_API_CMD` | Path to API wrapper script (if enabled + token set) |
| Developer | `GIT_CONFIG_*` | Git credential helper for HTTPS auth (if enabled + token set) |

## Popen Command
```python
# Restricted mode (config.security.mode == "restricted"):
cmd = ["claude", "-p", prompt, "--allowedTools", "Read", "Write", "Edit", "Grep", "Glob", "Bash"]
# Permissive mode (default, backward compat):
cmd = ["claude", "-p", prompt, "--dangerously-skip-permissions"]
# If streaming (on_progress provided):
cmd += ["--output-format", "stream-json", "--verbose"]
```
- Working dir: `str(config.temp_dir)`
- Timeout: `config.scheduler.task_timeout_minutes * 60`
- Env: `build_clean_env(config)` + task-specific vars (restricted = minimal env; permissive = os.environ)

## API Retry Logic (L915-952)
- `TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 529}` + `429`
- `API_RETRY_MAX_ATTEMPTS = 3`
- `API_RETRY_DELAY_SECONDS = 5` (fixed, not exponential)
- Pattern: `API Error: (\d{3}) (\{.*\})` (L32)
- Retries do NOT count against task attempts

## Stream Parsing (`_execute_streaming_once`, L786-913)
- Line-by-line from stdout via `parse_stream_line()`
- Events: `ResultEvent` → final result, `ToolUseEvent` → progress, `TextEvent` → progress
- Cancellation checked on each event via `db.is_task_cancelled()`
- Result priority: ResultEvent > result file > stderr > fallback error

## Key Constants
- Background task types excluded from context: `["scheduled", "briefing"]` (L483)
- Prompt file: `{user_temp_dir}/task_{task_id}_prompt.txt`
- Result file: `{user_temp_dir}/task_{task_id}_result.txt`

## Security Functions
| Function | Purpose |
|---|---|
| `build_clean_env(config)` | Minimal env for Claude subprocess (restricted) or os.environ (permissive) |
| `build_stripped_env()` | os.environ minus credential vars (PASSWORD/TOKEN/SECRET/API_KEY/NC_PASS/PRIVATE_KEY/APP_PASSWORD). For heartbeat/cron commands. Always-on. |
| `build_allowed_tools(is_admin, skill_names)` | Returns `["Read", "Write", "Edit", "Grep", "Glob", "Bash"]`. All Bash allowed — clean env is the boundary. |
| `_CREDENTIAL_ENV_PATTERNS` | Frozenset of credential substrings to strip |

## Other Functions
| Function | Lines | Purpose |
|---|---|---|
| `parse_api_error()` | 42-60 | Extract status_code/message from error text |
| `is_transient_api_error()` | 63-68 | Check if error is retryable |
| `get_user_temp_dir()` | 71-73 | `config.temp_dir / user_id` |
| `_ensure_reply_parent_in_history()` | 76-145 | Force-include reply parent in context |
| `load_persona()` | 148-154 | Load persona (user workspace > global) |
| `load_channel_guidelines()` | 157-163 | Load guidelines/{source_type}.md |
| `_execute_simple()` | 752-783 | subprocess.run mode |
| `_execute_streaming()` | 915-952 | Retry wrapper for streaming |
| `execute_task_interactive()` | 955-988 | CLI interactive mode |
