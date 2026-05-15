# Executor Internals

## `execute_task()`
```python
def execute_task(
    task: db.Task, config: Config, user_resources: list[db.UserResource],
    dry_run: bool = False, use_context: bool = True,
    conn: "db.sqlite3.Connection | None" = None,
    on_progress: Callable[[str], None] | None = None,
) -> tuple[bool, str, str | None, str | None]:
```
Returns `(success, result_text, actions_taken_json, execution_trace_json)`. `actions_taken` is a JSON array of tool use descriptions from streaming execution, or `None` for simple/dry-run/error paths. `execution_trace` is a JSON array of interleaved `{"type": "tool", "text": "..."}` and `{"type": "text", "text": "..."}` events, or `None`.

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
8d. **Knowledge facts**: load from `knowledge_graph`, relevance-filtered by prompt, capped by `max_knowledge_facts`
8e. **Memory cap**: `_apply_memory_cap()`, truncates recalled → knowledge facts → dated if `max_memory_chars` exceeded
9. **Confirmation context**: load from `task.confirmation_prompt` if confirmed task
10. **Build prompt**: includes `confirmation_context` when set
11. **Dry run check**: return prompt text
12. **Write prompt file**: `task_{id}_prompt.txt`
13. **Build env**: see env var table below; credential vars split via `_split_credential_env()` when proxy enabled
14. **Build BrainRequest**: prompt + allowed_tools + env + model/effort + sandbox_wrap closure + on_progress/cancel_check/on_pid callbacks
15. **Execute**: `make_brain(config.brain).execute(req)` — see `.claude/rules/brain.md`
16. **Compose result**: `_compose_full_result(result, trace)` reconciles result-text vs trace (CM-aware + terse-result recovery)
17. **Update fingerprint**: on success, interactive only

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
    knowledge_facts: str | None = None,
) -> str:
```

### Prompt Section Order
1. Header: role, user_id, datetime, task_id, conversation_token, db_path
2. Emissaries: `config/emissaries.md` constitutional principles (skipped for briefings)
3. Persona: user workspace `PERSONA.md` overrides `config/persona.md` (skipped for briefings or `skip_persona`)
4. Resources: calendars, folders, todos, email_folders, notes_folders, reminders
5. User memory: USER.md (skipped for briefings)
5b. Knowledge facts: relevance-filtered KG triples (skipped for briefings)
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
| Core | `ISTOTA_EXPERIMENTAL_FEATURES` | CSV of `config.experimental.features`. Read by `experimental.enabled_features_from_env()` and `@requires_feature`. Propagated by every subprocess builder: `executor.execute_task` (LLM path), `scheduler._execute_skill_task`, `scheduler._execute_command_task`, `heartbeat._check_shell_command`. Not credential-flavored — passes through the skill proxy and `build_stripped_env` untouched. |
| Core | `ISTOTA_SKILL_PROXY_SOCK` | Skill proxy socket path (if proxy enabled) |
| Nextcloud | `NC_URL`, `NC_USER`, `NC_PASS` | `config.nextcloud.*` |
| Nextcloud | `NEXTCLOUD_MOUNT_PATH` | `str(config.nextcloud_mount_path)` |
| CalDAV | `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD` | `config.caldav_*` |
| Browser | `BROWSER_API_URL`, `BROWSER_VNC_URL` | `config.browser.*` (if enabled) |
| Devbox | `ISTOTA_DEVBOX_CONTAINER`, `ISTOTA_DEVBOX_DOCKER_CLI`, `ISTOTA_DEVBOX_DOCKER_SOCKET`, `ISTOTA_DEVBOX_EXEC_TIMEOUT`, `ISTOTA_DEVBOX_MAX_OUTPUT_BYTES` | `config.devbox.*` (if enabled). Container name defaults to `f"{container_prefix}{task.user_id}"`. `build_bwrap_cmd` additionally `--ro-bind`s `docker_cli` and `--bind`s `docker_socket` so the `devbox` skill CLI can reach the host docker daemon from inside the sandbox. |
| Email | `SMTP_HOST/PORT/USER/PASSWORD`, `SMTP_FROM` | `config.email.*` (`SMTP_FROM` is plus-addressed: `bot+user_id@domain`) |
| Email | `IMAP_HOST/PORT/USER/PASSWORD` | `config.email.*` |
| Karakeep | `KARAKEEP_BASE_URL`, `KARAKEEP_API_KEY` | From resource config `extra` |
| Monarch | `MONARCH_SESSION_ID`, `MONARCH_CSRFTOKEN` | From the encrypted `secrets` table (cookie-pair auth). The legacy `MONARCH_EMAIL` / `MONARCH_PASSWORD` / `MONARCH_SESSION_TOKEN` were removed when the API switched to Django CSRF auth on `/graphql` — the cookie pair is the only credential. |
| Money | `MONEY_CONFIG`, `MONEY_USER` | From the user's `money` resource (in-process; `MONEY_USER` defaults to istota user_id) |
| Feeds | `FEEDS_USER` | From the user's `feeds` resource (in-process; defaults to istota user_id) |
| Location | `LOCATION_DB_PATH` | `istota.location.resolve_for_user(user_id, config).db_path` via the location skill's `setup_env` hook. Per-user `{workspace}/location/data/location.db`. Skill subcommands needing the framework geocode caches (`reverse_geocode`, `day_summary`) open a second conn to `ISTOTA_DB_PATH`. |
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

## Brain invocation
The executor no longer spawns `claude` directly — it composes a `BrainRequest`
and calls `make_brain(config.brain).execute(req)`. The brain owns command
construction, sandboxing (via the supplied `sandbox_wrap` callback),
subprocess/HTTP, stream parsing, and transient-API retries. Phase 1 ships
only `ClaudeCodeBrain`; details in `.claude/rules/brain.md`.

Per-task BrainRequest fields the executor populates:
- `prompt`, `allowed_tools` (from `build_allowed_tools`), `cwd=config.temp_dir`,
  `env` (built per task), `timeout_seconds=config.scheduler.task_timeout_minutes * 60`
- `model = (task.model or config.model)`, `effort = (task.effort or config.effort)`
- `custom_system_prompt_path = config/system-prompt.md` when `custom_system_prompt = true`
- `streaming = on_progress is not None`
- `on_progress`: closure that filters `StreamEvent` by `progress_show_tool_use`
  / `progress_show_text` and forwards `(message, italicize=False)` to the
  scheduler's progress callback
- `cancel_check`: closure that polls `db.is_task_cancelled()`
- `on_pid`: closure that calls `db.update_task_pid()` for `!stop` support
- `sandbox_wrap`: closure over `build_bwrap_cmd(...)` so the brain can wrap
  its raw cmd without knowing anything about bwrap; no-op when sandbox disabled
- `result_file = {user_temp_dir}/task_{task_id}_result.txt`

After `brain.execute()` returns, the executor:
1. Calls `_compose_full_result(result_text, trace)` on success to reconcile
   the final ResultEvent text against substantial intermediate text blocks
   (CM-aware + terse-result recovery — same logic both brains will need).
2. Updates the user skills fingerprint when interactive task succeeded.
3. Returns `(success, result, actions_taken_json, execution_trace_json)` —
   shape unchanged from before the refactor.

## Result composition (`_compose_full_result`)
Stays in the executor (not the brain) because it operates on the
brain-agnostic `(result_text, execution_trace)` pair. Two mechanisms
sharing one `_last_substantial_region()` walker; both **replace**
`result_text` outright — never prepend / glue:
1. **Mechanism A — CM-aware** (ISSUE-026): runs whenever any
   `cm_boundary` entries exist in the trace. Segments by `cm_boundary`,
   returns the last region ≥ `_CM_SEGMENT_MIN_CHARS` (200). Always runs
   for automated tasks too — scheduled tasks truncated mid-response by
   CM still get the fix. Falls back to `result_text` if no segment
   qualifies.
2. **Mechanism B — terse-recovery** (ISSUE-025): segments by both
   `tool` and `cm_boundary`, returns the last region
   ≥ `_TRAILING_REGION_MIN_CHARS` (500). Gated on
   `not _is_automated_task(task)` (source_type ∉ {scheduled, briefing}
   plus structural fallbacks `heartbeat_silent` / `scheduled_job_id`)
   AND `_is_terse(result_text)` (< 150 chars or matches a short
   reference regex like "see above" / "done" / "ok"). Skipped when CM
   events exist (Mechanism A wins) and when the recovered region is
   already a substring of `result_text`.

Every override logs one INFO line
(`compose_full_result: mechanism=… task_id=… source_type=… original_chars=… recovered_chars=…`)
so the 500-char floor can be calibrated against real production data.
The legacy Jaccard near-duplicate gluing path is gone; `_text_similarity`
remains in the source as a dead helper but is no longer called.

## API retry constants (re-exported from brain.claude_code)
- `TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 529}` + `429`
- `API_RETRY_MAX_ATTEMPTS = 3`
- `API_RETRY_DELAY_SECONDS = 5` (fixed, not exponential)
- Pattern: `API Error: (\d{3}) (\{.*\})`
- Retries do NOT count against task attempts
- `parse_api_error`, `is_transient_api_error` re-exported from `executor`
  for `scheduler.py` and tests; canonical home is `brain/claude_code.py`.

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
| `_PROXY_CREDENTIAL_VARS` | Frozenset of specific env vars stripped when proxy enabled (CALDAV_PASSWORD, NC_PASS, SMTP_PASSWORD, IMAP_PASSWORD, KARAKEEP_API_KEY, GITLAB_TOKEN, GITHUB_TOKEN, MONARCH_SESSION_ID, MONARCH_CSRFTOKEN, GOOGLE_WORKSPACE_CLI_TOKEN) |
| `_CREDENTIAL_SKILL_MAP` | Maps each credential env var to the set of skills that need it (scopes proxy responses) |
| `_authorized_skills_from_credentials(skill_index, credential_env)` | Returns skills authorized for credential access this task — any skill in `_CREDENTIAL_SKILL_MAP` is authorized if at least one of its mapped credentials is present in `credential_env`. Includes doc-only skills (notably `developer`) whose creds are consumed via `credential-fetch` lookups from helper scripts the executor writes (git credential helper, gitlab-api wrapper). Decoupled from skill selection: a keyword miss in Pass 1 / Pass 2 doesn't lock out a skill the user has clearly configured. Threat model is unchanged because `credential_env` only contains creds the user's resources / instance config supplied. |

## Skill Proxy Authorization Model

The proxy (`skill_proxy.py`) takes two distinct skill sets:
- `allowed_skills` (frozenset): all CLI skills (`cli: true`) — global whitelist used to reject typos / non-existent skill names.
- `authorized_skills` (frozenset): per-task subset returned by `_authorized_skills_from_credentials()`. Used purely for the informative-rejection error message returned to the client, and logged at proxy startup as `proxy_authorization task_id=… selected=… authorized=… …`.

The `skill_credential_map` (built from `authorized_skills` via `_build_skill_credential_map`) controls which credential env vars actually get injected for a given skill CLI invocation — that is the real enforcement boundary. Selection (Pass 1 + Pass 2) controls only which skill *docs* go in the prompt; it no longer gates credential access.

Every proxy rejection emits a structured WARNING — `proxy_rejected task_id=… type=skill|credential … reason=unknown_skill|not_authorized|not_authorized_credential|credential_not_present`. Use these to count selection misses vs. real abuse attempts.

## Output Validation
| Function | Purpose |
|---|---|
| `detect_malformed_result(text, tool_count, ...)` | Validates model output for leaked tool-call XML. Strict mode (Talk): any `</parameter>`, `</invoke>`, `<thinking>` outside code fences is flagged. Lenient mode (other targets): only flags when entire output is syntax fragments (< 20 chars of real content). Malformed results are reclassified as failures and retried. |
| `_compose_full_result(result_text, execution_trace, task=None)` | Two replace-only mechanisms sharing `_last_substantial_region()`: (A) CM-aware — runs whenever `cm_boundary` events exist, returns last segment ≥ 200 chars; (B) terse-recovery — runs only on non-automated tasks with terse `result_text`, segments by `tool` + `cm_boundary`, returns last region ≥ 500 chars. See "Result composition" section above. Logs every override. |
| `_last_substantial_region(trace, delimiters, min_chars)` | Shared walker: groups text events into regions split by `delimiters`, returns the joined text of the last region whose length crosses `min_chars`. |
| `_is_automated_task(task)`, `_is_terse(text)` | Gates for Mechanism B. Automated = source_type in `{scheduled, briefing}` or `heartbeat_silent` or `scheduled_job_id`. Terse = empty, < 150 chars, or matches short-reference regex. |

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
