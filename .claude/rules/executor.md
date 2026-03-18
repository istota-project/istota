# Executor Internals

## `execute_task()`
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
1. **Setup temp dir**: `config.temp_dir / task.user_id`
2. **Merge resources**: DB resources + config resources → `db.UserResource` list
3. **Load skills**: `load_skill_index()` → `select_skills()` → `load_skills()`
4. **Skills changelog**: fingerprint compare, interactive only
5. **Context loading**: skip for scheduled/briefing
6. **User memory**: `read_user_memory_v2()`, skip for briefings
7. **Channel memory**: `read_channel_memory()`, only if `conversation_token`
8. **CalDAV discovery**: `get_calendars_for_user()`
8b. **Dated memories**: `read_dated_memories()`, skip for briefings, controlled by `auto_load_dated_days`
8c. **Memory recall**: `_recall_memories()`, BM25 search using task prompt, skip for briefings
8d. **Memory cap**: `_apply_memory_cap()`, truncates recalled → dated if `max_memory_chars` exceeded
9. **Confirmation context**: load from `task.confirmation_prompt` if confirmed task
10. **Build prompt**: includes `confirmation_context` when set
11. **Dry run check**: return prompt text
12. **Write prompt file**: `task_{id}_prompt.txt`
13. **Build command**: `--allowedTools` whitelist, optional `--model` override
14. **Build env**: see env var table below; credential vars split via `_split_credential_env()` when proxy enabled
15. **Execute**: streaming or simple
16. **Update fingerprint**: on success, interactive only

## `build_prompt()`
```python
def build_prompt(
    task: db.Task, user_resources: list[db.UserResource], config: Config,
    skills_doc: str | None = None, conversation_context: str | None = None,
    user_memory: str | None = None, discovered_calendars: list[tuple[str, str, bool]] | None = None,
    user_email_addresses: list[str] | None = None, dated_memories: str | None = None,
    channel_memory: str | None = None, skills_changelog: str | None = None,
    is_admin: bool = True, emissaries: str | None = None,
    source_type: str | None = None, output_target: str | None = None,
    recalled_memories: str | None = None,
    excluded_resource_types: set[str] | None = None,
    skip_persona: bool = False,
    cli_skills_text: str | None = None,
    confirmation_context: str | None = None,
) -> str:
```

### Prompt Section Order
1. Header: role, user_id, datetime, task_id, conversation_token, db_path
2. Emissaries: `config/emissaries.md` constitutional principles (skipped for briefings)
3. Persona: user workspace `PERSONA.md` overrides `config/persona.md` (skipped for briefings or `skip_persona`)
4. Resources: calendars, folders, todos, email_folders, notes, reminders
5. User memory: USER.md (skipped for briefings)
6. Channel memory: CHANNEL.md
7. Dated memories: auto-loaded from `memories/YYYY-MM-DD.md` (configurable via `auto_load_dated_days`)
7b. Recalled memories: BM25 search results (when `auto_recall` enabled)
8. Confirmation context: previous bot output for confirmed actions
9. Tools: file access, browser, CalDAV, sqlite3, email
10. Rules: resource restrictions, confirmation, subtasks, output
11. Context: previous messages
12. Request: prompt + attachments
13. Guidelines: `config/guidelines/{source_type}.md`
14. Skills changelog
15. Skills doc

## Environment Variable Mapping

| Resource/System | Env Var | Source |
|---|---|---|
| Core | `ISTOTA_TASK_ID` | `str(task.id)` |
| Core | `ISTOTA_USER_ID` | `task.user_id` |
| Core | `ISTOTA_DB_PATH` | `str(config.db_path)` |
| Core | `ISTOTA_CONVERSATION_TOKEN` | `task.conversation_token` |
| Core | `ISTOTA_DEFERRED_DIR` | `str(user_temp_dir)` — always set, for deferred DB writes |
| Core | `ISTOTA_SKILL_PROXY_SOCK` | Skill proxy socket path (if proxy enabled) |
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
| Karakeep | `KARAKEEP_BASE_URL`, `KARAKEEP_API_KEY` | From resource config `extra` |
| Garmin | `GARMIN_EMAIL`, `GARMIN_PASSWORD`, `GARMIN_CONFIG` | From resource config `extra` or GARMIN.md |
| Monarch | `MONARCH_SESSION_TOKEN` | From resource config `extra` |
| Website | `WEBSITE_PATH`, `WEBSITE_URL` | `config.site.*` (if enabled + user site_enabled) |
| Developer | `DEVELOPER_REPOS_DIR` | `config.developer.repos_dir` (if enabled) |
| Developer | `GITLAB_URL` | `config.developer.gitlab_url` (if enabled) |
| Developer | `GITLAB_DEFAULT_NAMESPACE` | `config.developer.gitlab_default_namespace` (if enabled + set) |
| Developer | `GITLAB_API_CMD` | Path to API wrapper script (if enabled + token set) |
| Developer | `GITHUB_URL` | `config.developer.github_url` (if enabled) |
| Developer | `GITHUB_DEFAULT_OWNER` | `config.developer.github_default_owner` (if enabled + set) |
| Developer | `GITHUB_REVIEWER` | `config.developer.github_reviewer` (if enabled + set) |
| Developer | `GITHUB_API_CMD` | Path to API wrapper script (if enabled + token set) |
| Developer | `GIT_CONFIG_*` | Git credential helpers for HTTPS auth (if enabled + token set) |

## Popen Command
```python
cmd = ["claude", "-p", prompt, "--allowedTools", "Read", "Write", "Edit", "Grep", "Glob", "Bash"]
# If streaming (on_progress provided):
cmd += ["--output-format", "stream-json", "--verbose"]
```
- Working dir: `str(config.temp_dir)`
- Timeout: `config.scheduler.task_timeout_minutes * 60`
- Env: `build_clean_env(config)` + task-specific vars (always minimal env)

## API Retry Logic
- `TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 529}` + `429`
- `API_RETRY_MAX_ATTEMPTS = 3`
- `API_RETRY_DELAY_SECONDS = 5` (fixed, not exponential)
- Pattern: `API Error: (\d{3}) (\{.*\})`
- Retries do NOT count against task attempts

## Stream Parsing (`_execute_streaming_once`)
- Line-by-line from stdout via `parse_stream_line()`
- Events: `ResultEvent` → final result, `ToolUseEvent` → progress, `TextEvent` → progress
- Cancellation checked on each event via `db.is_task_cancelled()`
- Result priority: ResultEvent > result file > stderr > fallback error

## Key Constants
- Background task types excluded from context: `["scheduled", "briefing"]`
- Prompt file: `{user_temp_dir}/task_{task_id}_prompt.txt`
- Result file: `{user_temp_dir}/task_{task_id}_result.txt`

## Security Functions
| Function | Purpose |
|---|---|
| `build_clean_env(config)` | Minimal env for Claude subprocess (PATH, HOME, PYTHONUNBUFFERED + passthrough vars) |
| `build_stripped_env()` | os.environ minus credential vars (PASSWORD/TOKEN/SECRET/API_KEY/NC_PASS/PRIVATE_KEY/APP_PASSWORD). For heartbeat/cron commands. Always-on. |
| `build_allowed_tools(is_admin, skill_names)` | Returns `["Read", "Write", "Edit", "Grep", "Glob", "Bash"]`. All Bash allowed — clean env is the boundary. |
| `_PROXY_CREDENTIAL_VARS` | Frozenset of specific env vars stripped when proxy enabled (CALDAV_PASSWORD, NC_PASS, SMTP_PASSWORD, IMAP_PASSWORD, KARAKEEP_API_KEY, GITLAB_TOKEN, GITHUB_TOKEN, GARMIN_EMAIL, GARMIN_PASSWORD, MONARCH_SESSION_TOKEN) |
| `_CREDENTIAL_SKILL_MAP` | Maps each credential env var to the set of skills that need it (scopes proxy responses) |

## Other Functions
| Function | Purpose |
|---|---|
| `parse_api_error()` | Extract status_code/message from error text |
| `is_transient_api_error()` | Check if error is retryable |
| `get_user_temp_dir()` | `config.temp_dir / user_id` |
| `_ensure_reply_parent_in_history()` | Force-include reply parent in context |
| `load_emissaries()` | Load constitutional principles (global only, not user-overridable) |
| `load_persona()` | Load persona (user workspace > global) |
| `load_channel_guidelines()` | Load guidelines/{source_type}.md |
| `_split_credential_env()` | Split env dict into credential vars and clean vars (for proxy) |
| `_build_network_allowlist()` | Build host:port allowlist for CONNECT proxy |
| `build_bwrap_cmd()` | Build bubblewrap sandbox command wrapper |
| `_execute_simple()` | subprocess.run mode |
| `_execute_streaming()` | Retry wrapper for streaming |
| `execute_task_interactive()` | CLI interactive mode |
