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

`ClaudeCodeBrain`, the default brain, builds and invokes:

```
claude -p - --allowedTools Read Write Edit Grep Glob Bash --disallowedTools Agent Workflow \
  --output-format stream-json --verbose
```

with optional `--model`, `--effort`, and `--system-prompt-file` flags. See [brain](brain.md) for the full implementation.

## Environment variables

The executor builds a minimal, clean environment for the subprocess. `build_clean_env()` starts with only PATH, HOME, PYTHONUNBUFFERED, and configured passthrough vars (`LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`). The only env vars the executor injects directly are the core identity ones (`ISTOTA_TASK_ID`, `ISTOTA_USER_ID`, `ISTOTA_DB_PATH`, `ISTOTA_CONVERSATION_TOKEN`, `ISTOTA_DEFERRED_DIR`, `ISTOTA_SKILL_PROXY_SOCK`) plus a few path/runtime vars (`NEXTCLOUD_MOUNT_PATH`, `BROWSER_API_URL`, `BROWSER_VNC_URL`, `WEBSITE_PATH`, `WEBSITE_URL`).

Everything else — Nextcloud / CalDAV / IMAP / SMTP credentials, service tokens, per-user secrets — is **manifest-derived**. Each skill's `skill.md` frontmatter declares its env vars in the `env:` block; `build_skill_env()` walks the loaded skill index and resolves each `EnvSpec` against the task's `EnvContext`. This replaces the hardcoded credential-injection block in `execute_task` that used to duplicate the same wiring across the executor, the proxy strip-set, and the auth map.

`EnvSpec` sources: `config` (dotted config path with `when` guard), `resource` (resource mount path), `resource_json` (all resources of a type as JSON), `user_resource_config` (TOML `extras` field), `template_file` (auto-create from template), `secret` (per-user encrypted secret), `setup_env` (skill-defined hook in `__init__.py:setup_env(ctx)` — used by `developer` for the git credential helper + API wrappers it bind-mounts into the sandbox).

Two pre-resolution gates filter out specs that shouldn't fire:

- `gate_user_has_resource: "<type>"` — only resolve when the user owns at least one resource of that type
- `gate_has_discovered_calendars: true` — only resolve when CalDAV discovery returned at least one calendar

CalDAV discovery is itself a best-effort step: `discover_calendars_for_task(task, config)` returns `[]` when CalDAV is unconfigured / unreachable / the user owns no calendars. The same helper is reused by the scheduler's two subprocess paths (`_execute_skill_task`, `_execute_command_task`) so the gate fires consistently across LLM, skill-task, and command-task dispatch.

See [environment variables reference](../reference/environment-variables.md) for the full mapping and [credentials](../configuration/credentials.md) for the two-tier credential architecture (global vs per-user).

## Credential proxy and authorization

When the skill proxy is enabled (default), credential vars are split out of Claude's environment via `_split_credential_env(env, credential_set)` and routed through a Unix socket proxy. The credential set is itself manifest-derived — `derive_credential_set(skill_index)` returns every env var declared with `sensitive: true` across all skills.

Authorization is decoupled from skill selection. `derive_authorized_skills(selected, skill_index, ctx)` returns the union of selected skills plus any skill whose sensitive `EnvSpec`s actually resolve under the task's context. So a user with Karakeep configured can always reach `KARAKEEP_API_KEY`, even if Pass 1/Pass 2 missed the bookmarks skill on a given prompt. Critical correctness note: the auth-side resolution passes `fallbacks_disabled=True` so an instance-wide `EnvironmentFile` value cannot fan a global secret out to per-user auto-authorization.

`derive_skill_credential_map(authorized, skill_index)` builds the per-skill map the proxy uses to scope credential injection — a skill CLI invocation only ever sees credentials its own manifest declared. `derive_lookup_allowlist(authorized, skill_index)` is the union the proxy will respond to over `credential-fetch`, with `_PROXY_LOOKUP_BLOCKED = {"ISTOTA_SECRET_KEY"}` subtracted as a defense-in-depth hard reject so a buggy `setup_env` hook can't expose the master Fernet key over the lookup channel.

See [security](../deployment/security.md#authorization-model) for the full model and rejection logging.

## Streaming events

The brain emits `StreamEvent`s (defined in `src/istota/brain/_events.py`) which the executor's `on_progress` closure filters and forwards to the scheduler's progress callback:

- **ToolUseEvent** — forwarded as progress updates to Talk (gated by `progress_show_tool_use`)
- **TextEvent** — forwarded as progress with `italicize=False` (gated by `progress_show_text`)
- **ResultEvent** — final result (handled internally by the brain, surfaces as `BrainResult.result_text`)
- **ContextManagementEvent** — marks a context management boundary in the trace (the brain records it as a `cm_boundary` entry)

Cancellation is polled between events via the `cancel_check` callback, which calls `db.is_task_cancelled()`. The brain kills its subprocess and returns `stop_reason="cancelled"` when the check returns True.

## Result composition

The result goes through `_compose_full_result()`, which has two narrowly-scoped mechanisms sharing a `_last_substantial_region()` walker. Both mechanisms **replace** `result_text` outright — they never prepend or glue recovered text in front of the model's final output.

**Mechanism A — CM-aware (ISSUE-026):** When any `cm_boundary` events exist in the trace, segments the trace at those boundaries and returns the last region whose text is at least 200 chars (`_CM_SEGMENT_MIN_CHARS`). Always runs when CM events are present, including for automated tasks (scheduled / briefing / heartbeat). Falls back to `result_text` if no segment qualifies.

**Mechanism B — terse-recovery (ISSUE-025):** Segments the trace by both `tool` and `cm_boundary` events and returns the last region of at least 500 chars (`_TRAILING_REGION_MIN_CHARS`). Gated by **both** `_is_automated_task(task)` returning False **and** `_is_terse(result_text)` returning True (text shorter than 150 chars or matching a short reference regex like "see above" / "done" / "ok"). Structured-output tasks and substantial results bypass this mechanism. Skipped when CM events exist (Mechanism A wins).

Every override emits a single `compose_full_result: mechanism=… task_id=… original_chars=… recovered_chars=…` INFO log so the 500-char floor can be calibrated against production data.

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
| `build_stripped_env()` | `os.environ` minus anything containing `PASSWORD`, `SECRET`, `TOKEN`, `API_KEY`, `APP_PASSWORD`, `NC_PASS`, or `PRIVATE_KEY` in its name. Substring match — no preserve list (`ISTOTA_SECRET_KEY` is stripped). For heartbeat/cron commands. |
| `build_allowed_tools()` | Returns `["Read", "Write", "Edit", "Grep", "Glob", "Bash"]` |
| `_split_credential_env()` | Separates credential vars for proxy routing using a manifest-derived `credential_set` |
| `derive_credential_set()` | Sensitive env-var names across all skill manifests (replaces `_PROXY_CREDENTIAL_VARS`) |
| `derive_authorized_skills()` | Selected skills ∪ skills whose sensitive `EnvSpec`s resolve under this task's context (replaces `_authorized_skills_from_credentials`) |
| `derive_skill_credential_map()` | Per-skill credential map used by the proxy (replaces `_build_skill_credential_map`) |
| `derive_lookup_allowlist()` | Vars the proxy will respond to over `credential-fetch`, minus `_PROXY_LOOKUP_BLOCKED` |
| `discover_calendars_for_task()` | Best-effort CalDAV discovery; returns `[]` on any failure. Reused across LLM and subprocess dispatch paths |
| `build_bwrap_cmd()` | Builds bubblewrap sandbox command wrapper |
| `_build_network_allowlist()` | Builds host:port allowlist for CONNECT proxy |
