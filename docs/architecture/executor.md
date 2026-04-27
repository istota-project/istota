# Executor

The executor (`executor.py`) is responsible for assembling prompts, building the per-task environment, and orchestrating a pluggable Brain implementation. The Brain owns model invocation (subprocess or HTTP), stream parsing, and transient-API retry; the executor stays focused on per-task orchestration. See [brain](brain.md) for the protocol and the bundled `ClaudeCodeBrain`.

## Prompt assembly

The prompt is built in a specific order, each section adding context for Claude:

1. **Header**: role definition, user_id, current datetime, task_id, conversation_token, db_path
2. **Emissaries**: constitutional principles from `config/emissaries.md` (skipped for briefings)
3. **Persona**: user workspace `PERSONA.md` overrides `config/persona.md` (skipped for briefings)
4. **Resources**: calendars, folders, todos, email folders, notes, reminders
5. **User memory**: `USER.md` content (skipped for briefings)
6. **Knowledge graph facts**: relevance-filtered entity-relationship triples from `knowledge_facts` table, capped by `max_knowledge_facts` (skipped for briefings)
7. **Channel memory**: `CHANNEL.md` content (when `conversation_token` is set)
8. **Dated memories**: last N days of extracted memories (via `auto_load_dated_days`)
9. **Recalled memories**: BM25 search results (when `auto_recall` is enabled)
10. **Confirmation context**: previous bot output for confirmed actions
11. **Tools**: available tools documentation (file access, browser, CalDAV, sqlite3, email)
12. **Rules**: resource restrictions, confirmation flow, subtask creation, output format
13. **Conversation context**: previous messages (selected by the context module)
14. **Request**: the actual prompt text + file attachments
15. **Guidelines**: channel-specific formatting from `config/guidelines/{source_type}.md`
16. **Skills changelog**: "what's new" if skills updated since last interaction
17. **Skills documentation**: concatenated skill `.md` files, selectively loaded

## Brain invocation

Once the prompt and env are built, the executor composes a `BrainRequest` and calls `make_brain(config.brain).execute(req)`. The request bundles the prompt, allowed tools, working directory (`config.temp_dir`), env, timeout (`task_timeout_minutes * 60`), model/effort overrides, optional custom system prompt path (when `custom_system_prompt` is enabled), and the callbacks the brain needs: `on_progress`, `cancel_check`, `on_pid`, and `sandbox_wrap` (a closure that wraps the brain's raw cmd in bubblewrap when the sandbox is enabled — the brain itself stays sandbox-agnostic).

The brain returns a `BrainResult` carrying `(success, result_text, actions_taken, execution_trace, stop_reason)`. The executor then runs result composition (see below) and downstream cleanup (malformed-output detection, deferred file processing).

`ClaudeCodeBrain`, the default and only Phase 1 brain, builds and invokes:

```
claude -p - --allowedTools Read Write Edit Grep Glob Bash --disallowedTools Agent \
  --output-format stream-json --verbose
```

with optional `--model`, `--effort`, and `--system-prompt-file` flags. See [brain](brain.md) for the full implementation.

## Environment variables

The executor builds a minimal, clean environment for the subprocess. `build_clean_env()` starts with only PATH, HOME, PYTHONUNBUFFERED, and configured passthrough vars (`LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`). Task-specific vars are added on top:

| Category | Variables |
|---|---|
| Core | `ISTOTA_TASK_ID`, `ISTOTA_USER_ID`, `ISTOTA_DB_PATH`, `ISTOTA_CONVERSATION_TOKEN`, `ISTOTA_DEFERRED_DIR` |
| Nextcloud | `NC_URL`, `NC_USER`, `NC_PASS`, `NEXTCLOUD_MOUNT_PATH` |
| CalDAV | `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD` |
| Email | `SMTP_HOST/PORT/USER/PASSWORD`, `SMTP_FROM`, `IMAP_HOST/PORT/USER/PASSWORD` |
| Browser | `BROWSER_API_URL`, `BROWSER_VNC_URL` |
| Services | `KARAKEEP_BASE_URL/API_KEY`, `MINIFLUX_BASE_URL/API_KEY`, `MONEY_CONFIG/USER`, `MONARCH_SESSION_TOKEN` |
| Developer | `DEVELOPER_REPOS_DIR`, `GITLAB_URL/TOKEN`, `GITHUB_URL/TOKEN`, `GIT_CONFIG_*` |
| Website | `WEBSITE_PATH`, `WEBSITE_URL` |

When the skill proxy is enabled (default), credential vars are split out via `_split_credential_env()` and routed through a Unix socket proxy instead of being in Claude's environment. The proxy is instantiated with two distinct skill sets: `allowed_skills` (the global CLI whitelist, used to reject typos) and `authorized_skills` (per-task subset returned by `_authorized_skills_from_credentials()`, used for credential scoping and informative rejection messages). The authorized set is derived from credential presence in the task env — not from skill selection — so a keyword miss in Pass 1/Pass 2 doesn't lock out a skill the user has clearly configured. See [security](../deployment/security.md#authorization-model) for the full model.

See [environment variables reference](../reference/environment-variables.md) for the full mapping.

## Streaming events

The brain emits `StreamEvent`s (defined in `src/istota/brain/_events.py`) which the executor's `on_progress` closure filters and forwards to the scheduler's progress callback:

- **ToolUseEvent** — forwarded as progress updates to Talk (gated by `progress_show_tool_use`)
- **TextEvent** — forwarded as progress with `italicize=False` (gated by `progress_show_text`)
- **ResultEvent** — final result (handled internally by the brain, surfaces as `BrainResult.result_text`)
- **ContextManagementEvent** — marks a context management boundary in the trace (the brain records it as a `cm_boundary` entry)

Cancellation is polled between events via the `cancel_check` callback, which calls `db.is_task_cancelled()`. The brain kills its subprocess and returns `stop_reason="cancelled"` when the check returns True.

## Result composition

The result goes through `_compose_full_result()`, which has two modes:

**CM-aware mode**: When context management boundaries exist in the trace, segments by boundary and uses the last segment with substantial text (>= 200 chars). Falls back to `result_text` if no substantial segment.

**Terse-result recovery**: When no context management, detects substantial text blocks emitted as intermediate text but missing from the `ResultEvent`, and prepends them.

Result priority: ResultEvent > result file > stderr > fallback error.

## API retry logic

Transient API errors (status codes 500, 502, 503, 504, 529, 429) are retried inside the brain up to 3 times with a 5 s fixed delay. These retries don't count against task attempts. Pattern: `API Error: (\d{3}) (\{.*\})`. The helpers (`parse_api_error`, `is_transient_api_error`, `API_RETRY_*`, `TRANSIENT_STATUS_CODES`) live in `src/istota/brain/claude_code.py` and are re-exported from `executor.py` for `scheduler.py` and tests.

## Output validation

`detect_malformed_result()` checks for leaked tool-call XML in the output:

- **Strict mode** (Talk): any `</parameter>`, `</invoke>`, `<thinking>` outside code fences is flagged
- **Lenient mode** (other targets): only flags when the entire output is syntax fragments (< 20 chars of real content)

Malformed results are reclassified as failures and retried.

## Security functions

| Function | Purpose |
|---|---|
| `build_clean_env()` | Minimal env for Claude subprocess |
| `build_stripped_env()` | `os.environ` minus credential vars (for heartbeat/cron commands) |
| `build_allowed_tools()` | Returns `["Read", "Write", "Edit", "Grep", "Glob", "Bash"]` |
| `_split_credential_env()` | Separates credential vars for proxy routing |
| `_authorized_skills_from_credentials()` | Returns CLI skills authorized for credential access this task — any skill whose mapped credentials are present in the env |
| `build_bwrap_cmd()` | Builds bubblewrap sandbox command wrapper |
| `_build_network_allowlist()` | Builds host:port allowlist for CONNECT proxy |
