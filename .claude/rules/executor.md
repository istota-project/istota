---
paths:
  - "src/istota/executor.py"
---

# Executor Internals

## `execute_task()`
```python
def execute_task(
    task: db.Task, config: Config, user_resources: list[db.UserResource],
    dry_run: bool = False, use_context: bool = True,
    conn: "db.sqlite3.Connection | None" = None,
    event_writer: "events.EventWriter | None" = None,
) -> tuple[bool, str, str | None, str | None]:
```
The old `on_progress: Callable[[str], None]` parameter is gone (task-event-streaming spec). The scheduler builds an `EventWriter`, subscribes consumers, and passes it; the executor emits `task_started` then adapts the brain's widened `StreamEvent` stream to `TaskEvent`s via `_on_brain_event` → `event_writer.emit(...)`. `None` on dry-run / CLI paths.
Returns `(success, result_text, actions_taken_json, execution_trace_json)`. `actions_taken` is a JSON array of tool use descriptions from streaming execution, or `None` for simple/dry-run/error paths. `execution_trace` is a JSON array of interleaved `{"type": "tool", "text": "..."}` and `{"type": "text", "text": "..."}` events, or `None`. A `tool` entry additionally carries `"raw": "<verbatim command>"` for Bash calls (the literal command, untruncated; `_tool_invocation` in `agent/events.py`) so the sleep cycle can distil playbooks that quote the real invocation rather than the paraphrased description (ISSUE-174 fix 1). Additive — consumers that read `text`/`type` are unaffected.

### Flow
1. **Setup temp dir**: `config.temp_dir / task.user_id`
1b. **Deferred briefing prompt** (ISSUE-143): when `task.source_type == "briefing"` and `task.briefing_name` is set, `build_deferred_briefing_prompt(task, config)` resolves the live briefing config + timezone and builds the full prompt (`build_briefing_prompt`'s slow news/yfinance/FinViz/IMAP fetch) here, in the worker — `task.prompt` is replaced. This keeps the slow network I/O off the scheduler dispatch thread (the scheduler creates briefing tasks with only the identity + a placeholder). Build failure / unresolvable briefing → keeps the placeholder, never fails the task.
2. **Merge resources**: DB resources + config resources → `db.UserResource` list
3. **Load skills**: `load_skill_index()` → `select_skills()` (deterministic matching, the only selection pass) produces the **eager** set → `load_skills(eager)` for the body + `eligible_skill_names(exclude = selected ∪ ⋃ exclude_skills_of_selected)` for the **menu** (the full eligible catalogue) → `build_disclosure_index(menu)` → `skills_index`. Always on, single-axis (selected ⇒ eager, else eligible ⇒ menu); no `progressive_disclosure` flag, no eager/lazy partition. The menu replaced the removed LLM Pass 2; the executor logs `skills: eager=N menu=M`.
4. **Skills changelog**: fingerprint compare, interactive only
5. **Context loading**: skip for scheduled/briefing
6. **User memory**: `read_user_memory_v2()`, skip for briefings
7. **Channel memory**: `read_channel_memory()`, only if `conversation_token`
8. **CalDAV discovery**: `get_calendars_for_user()`
8b. **Dated memories**: `read_dated_memories()`, skip for briefings, controlled by `auto_load_dated_days`
8c. **Memory recall**: `_recall_memories()`, BM25 search using task prompt, skip for briefings
8d. **Knowledge facts**: load from `knowledge_graph`, relevance-filtered by prompt, capped by `max_knowledge_facts`
8d2. **Playbook recall**: `_recall_playbooks()`, BM25/vector over `source_type="playbook"`, gated on `playbooks.enabled`, skipped for automated/`skip_memory` tasks (Part B). On a hit it `os.utime`s each recalled playbook file so retention keys on last-*use*, not last-write (ISSUE-174 Concern 3)
8e. **Memory cap**: `_apply_memory_cap()`, truncates recalled → knowledge facts → dated → playbooks if `max_memory_chars` exceeded (playbooks truncated last — most protected; returns a 6-tuple)
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
    playbooks: str | None = None,
    excluded_resource_types: set[str] | None = None,
    skip_persona: bool = False,
    cli_skills_text: str | None = None,
    skills_index: str | None = None,
    confirmation_context: str | None = None,
    knowledge_facts: str | None = None,
) -> str:
```

### Prompt Section Order
1. Header: role, user_id, datetime, task_id, conversation_token, and a database line that names no path (the file is masked out of the sandbox; naming it would point at nothing)
2. Emissaries: `config/emissaries.md` constitutional principles (skipped for briefings)
3. Persona: user workspace `PERSONA.md` overrides `config/persona.md` (skipped for briefings or `skip_persona`)
4. Workspace layout: one static line, plus CalDAV-discovered calendars. The Resources sunset replaced the enumerated Folders / TODO Files / Notes / Reminders sections with that single line
5. User memory: USER.md (skipped for briefings)
5b. Knowledge facts: relevance-filtered KG triples (skipped for briefings)
6. Channel memory: CHANNEL.md
7. Dated memories: auto-loaded from `memories/YYYY-MM-DD.md` (configurable via `auto_load_dated_days`)
7b. Recalled memories: BM25 search results (when `auto_recall` enabled)
7c. Learned Playbooks: `_recall_playbooks` BM25/vector hits over `source_type="playbook"` (when `playbooks.enabled`; skipped for automated/`skip_memory` tasks)
9. Tools: file access, browser, CalDAV, email, then `skills_index` ("Available skills (load on demand)" — the menu catalogue) when the menu is non-empty. The **file-access framing is storage-backend-aware** (storage-agnostic-vocabulary spec): it renders in one of three modes keyed on `config.storage_backend` — Nextcloud-via-mount, Nextcloud-via-rclone, or local — and the folders header + attachments prose follow the same switch. Local mode adds a bullet clarifying the workspace is the *managed* area, not the limit of what an unsandboxed local bot can read (fixes the "I can only see the Nextcloud mount" false claim). Server/Nextcloud prompts are byte-unchanged. The executor is the single home of storage framing; skill bodies are storage-neutral and reference paths through the `{workspace}` / `{storage}` placeholders (see below).
10. Rules: resource restrictions, confirmation, subtasks, output
11. Context: previous messages
11b. Confirmation context: previous bot output for confirmed actions — interpolated after the context section, immediately before the request
12. Request: prompt + attachments
13. Guidelines: `config/guidelines/{source_type}.md`
14. Skills changelog
15. Skills doc (eager skills only — the menu skills are surfaced by the index in step 9, not inlined)

## Environment Variable Mapping

| Resource/System | Env Var | Source |
|---|---|---|
| Core | `ISTOTA_TASK_ID` | `str(task.id)` |
| Core | `ISTOTA_USER_ID` | `task.user_id` |
| Core | `ISTOTA_DB_PATH` | `str(config.db_path)` — set for **every** user, then split out of Claude's env into the skill proxy's `base_env` (`derive_proxy_only_set`). It never reaches the sandbox. `scheduler._execute_command_task` / `_execute_skill_task` / `heartbeat` set it unconditionally and unsplit; those paths are unsandboxed by design. |
| Core | `HEALTH_DB_PATH`, `LOCATION_DB_PATH` | Manifest-declared `proxy_only: true` — same routing, no credential semantics. |
| Core | `ISTOTA_SANDBOXED` | `"1"` when `skill_proxy_enabled and effective_sandboxing(config)` — the proxy conjunct matters, since the marker means "the socket is how you run a skill" and with the proxy off there is no socket. Sandbox env only (added *after* the proxy's base env is snapshotted). `skill_client._run_direct` refuses when it is set. |

**No database is reachable from the sandbox.** `build_bwrap_cmd` ends with `--tmpfs` masks over `config.db_path.parent` and `config.module_db_root()` — the **last** mount operations, since bwrap applies argv in order. Each mask is followed by `--remount-ro` on the same path (`_bwrap_supports_remount_ro()`, probed like `--disable-userns`): a writable mask lets `sqlite3 {db_dir}/istota.db` *create* a zero-byte file and answer `no such table`, which reads as a corrupt database rather than a boundary and litters the directory for the rest of the task. A `--remount-ro` is the one thing allowed to follow a mask, since it can only take permissions away. Read-only also makes a mask *nested inside* another fatal (bwrap `mkdir`s the second mountpoint on the first mask's tmpfs, gets EROFS, exits before running anything), so `_mask_dir` skips any candidate an earlier mask already covers — including the case where the outer mask was refused, which the old caller-side check treated as covered. `--disable-userns` now ships with the `--unshare-user` bwrap requires alongside it; without it bwrap exits 1, which is why both the probe and the flag were inert from the day they were added. Nothing binds the framework DB or its `-wal`/`-shm` any more (the admin `--ro-bind` and `security.sandbox_admin_db_write` are both gone), and `native_fs_roots` dropped its matching admin read root.

The masks exist rather than a narrower "don't bind it" because not binding it is what the code already did, and it did not hold. `module_data_dir` defaults under `{db_path.parent}`, the reference deployment puts that under `istota_home`, and `sandbox_ro_paths` defaulted to the `/srv/app` containing it — so one RO bind naming no database exposed the framework DB with live sidecars, every user's module DB, the local backups and the browser profile, to admin and non-admin alike. The word "modules" appeared nowhere near the bind code, which is why two reviews of this area missed it. `sandbox_ro_paths` now defaults to `[]` **and is parsed from TOML at all** (it never was — the advertised knob was inert), but the masks are what makes the property independent of that.

The boundary is therefore two things, in order: the skill CLIs scope by `ISTOTA_USER_ID`, and the files are not there. Reaching them requires the proxy, which is why it is started unconditionally now — the old `if credential_env:` gate let a task with no secrets fall through to `skill_client._run_direct`, running the skill module inside the sandbox. `ISTOTA_SANDBOXED` makes that path fail closed instead.

Not covered by any of this: a deployment where bwrap is unavailable (Docker without `CAP_SYS_ADMIN` — the probe fails and the sandbox is silently skipped) or the standalone local install, which ships `sandbox_enabled = false` + `skill_proxy_enabled = false` by design. Both run the model with the daemon user's own filesystem access; the masks are a server-shape property.
| Core | `ISTOTA_CONVERSATION_TOKEN` | `task.conversation_token` |
| Core | `ISTOTA_DEFERRED_DIR` | `str(user_temp_dir)` — always set, for deferred DB writes |
| Core | `ISTOTA_EXPERIMENTAL_FEATURES` | CSV of `config.experimental.features`. Read by `experimental.enabled_features_from_env()` and `@requires_feature`. Propagated by every subprocess builder: `executor.execute_task` (LLM path), `scheduler._execute_skill_task`, `scheduler._execute_command_task`, `heartbeat._check_shell_command`. Not credential-flavored — passes through the skill proxy and `build_stripped_env` untouched. |
| Core | `ISTOTA_SKILL_PROXY_SOCK` | Skill proxy socket path (if proxy enabled) |
| Nextcloud | `NC_URL`, `NC_USER`, `NC_PASS` | `config.nextcloud.*` |
| Nextcloud | `NEXTCLOUD_MOUNT_PATH` | `str(config.nextcloud_mount_path)` |
| CalDAV | `CALDAV_URL`, `CALDAV_USERNAME`, `CALDAV_PASSWORD` | `config.caldav_*` |
| Browser | `BROWSER_API_URL`, `BROWSER_VNC_URL` | `config.browser.*` (if enabled) |
| Devbox | `ISTOTA_DEVBOX_CONTAINER`, `ISTOTA_DEVBOX_DOCKER_CLI`, `ISTOTA_DEVBOX_MAX_OUTPUT_BYTES` | `config.devbox.*` (set unconditionally when `config.devbox.enabled`, no selection gate). Container name defaults to `f"{container_prefix}{task.user_id}"`. **No socket path of any kind is exported, and `build_bwrap_cmd` binds no Docker socket and no `docker` binary.** The Docker-API allowlist proxy that used to be bound at `/var/run/docker.sock` in every sandbox is retired with its only consumer; the devbox skill's one remaining Docker verb, `reset`, runs host-side in the CLI's own process. `/usr` is `--ro-bind`ed unconditionally, so `/usr/bin/docker` is still *in* the namespace on any host with the client installed — the guarantee is that no socket is bound at any path and no `DOCKER_HOST` is exported, so any `docker` a task finds fails at connect. `ISTOTA_DEVBOX_EXEC_TIMEOUT` went with the 300-second default it carried (the transport imposes none; the task's budget governs). `ISTOTA_DEVBOX_EXEC_SOCKET` does not exist and must not be added, for the reason `config.devbox.docker_socket` was kept out before it was deleted (ISSUE-284): this environment is the model's, so a socket path named here is one the model can replace, and a replaced socket answers `ok` and a fabricated exit 0. The skill CLI reads its socket from config, host-side. |
| Email | `SMTP_HOST/PORT/USER/PASSWORD`, `SMTP_FROM` | `config.email.*` (`SMTP_FROM` is plus-addressed: `bot+user_id@domain`) |
| Email | `IMAP_HOST/PORT/USER/PASSWORD` | `config.email.*` |
| Karakeep | `KARAKEEP_BASE_URL`, `KARAKEEP_API_KEY` | From resource config `extra` |
| Monarch | `MONARCH_SESSION_ID`, `MONARCH_CSRFTOKEN` | From the encrypted `secrets` table (cookie-pair auth). The legacy `MONARCH_EMAIL` / `MONARCH_PASSWORD` / `MONARCH_SESSION_TOKEN` were removed when the API switched to Django CSRF auth on `/graphql` — the cookie pair is the only credential *stored*. It is mintable server-side: `POST /api/money/monarch/login` takes email/password (plus an MFA code or an emailed OTP) transiently, signs in at the endpoint Monarch's own web app uses and with the client version its `version.json` reports, and persists only the resulting pair. Those inputs never reach a task env. |
| Money | `MONEY_USER` | The istota user_id (in-process facade; config resolved from the per-user money DB via `resolve_for_user`). `MONEY_CONFIG` is gone — there is no standalone money config path. |
| Feeds | `FEEDS_USER` | From the user's `feeds` resource (in-process; defaults to istota user_id) |
| Location | `LOCATION_DB_PATH` | `istota.location.resolve_for_user(user_id, config).db_path` via the location skill's `setup_env` hook. Per-user `{workspace}/location/data/location.db`. Skill subcommands needing the framework geocode caches (`reverse_geocode`, `day_summary`) open a second conn to `ISTOTA_DB_PATH`. |
| Developer | `DEVELOPER_REPOS_DIR` | `{config.developer.repos_dir}/{user_id}` via the developer skill's `setup_env` hook (if enabled, and the task is an admin's). The per-user subtree, matching the bind — never the shared root. |
| Developer | `GITLAB_URL` | `config.developer.gitlab_url` (if enabled) |
| Developer | `GITLAB_DEFAULT_NAMESPACE` | `config.developer.gitlab_default_namespace` (if enabled + set) |
| Developer | `GITLAB_REVIEWER` | `config.developer.gitlab_reviewer` (if enabled + set) — the reviewer's GitLab username |
| Developer | `GITHUB_URL` | `config.developer.github_url` (if enabled) |
| Developer | `GITHUB_DEFAULT_OWNER` | `config.developer.github_default_owner` (if enabled + set) |
| Developer | `GITHUB_REVIEWER` | `config.developer.github_reviewer` (if enabled + set) |
| Developer | `DEVELOPER_AUTHOR_CREDIT` | `config.developer.author_credit` (if enabled + set) |
| Developer | `GIT_CONFIG_*` | Git credential helpers for HTTPS auth (if enabled + token set) |
| Developer | `ISTOTA_PATH_PREPEND` | `{user_temp_dir}/.developer`, written by the developer `setup_env` hook when a forge token is configured, **plus `{user_temp_dir}/.developer/exec-shims` when `[developer.container] backend = "devbox"`** — the two halves are independent (a deployment routing builds into the devbox need not have a forge configured), and where both are present `.developer` comes first, so a forge wrapper wins any name collision with a shim. The executor folds them onto the brain's `PATH` and **strips the variable**, so `gh` and `glab` resolve to the wrappers (`src/istota/forge_cli.py`) and the configured `shim_commands` resolve to the exec shims. A separate directory rather than the forge wrappers' own, so a shim whose command left the list can be removed without deciding which files are shims from their contents. The shim half is gated on **configuration alone** — `developer.enabled`, a non-empty `repos_dir`, and the backend — never on selection: `developer` is a menu skill with no `always_include` and no `source_types`, so it reaches `selected_skills` only through sticky skills, which is the *second* turn of a conversation. A selection gate would leave the shims absent on a fresh "work on repo X" and the build would run host-side and 403 at the CONNECT proxy, reading as flakiness. The security half — binding the socket — is gated separately, on `authorized_skills`, which is a set the hook cannot see because hooks are dispatched *before* authorization by design. Deliberately absent from the `proxy_base_env` snapshot: a skill CLI runs host-side and must not pick up the task's wrappers. |

The sandbox RO-binds the host's real `~/.claude/settings.json` back into the
tmpfs'd `~/.claude` (`build_bwrap_cmd`), and the six direct brain callers
(`.claude/rules/brain.md` § Direct-caller availability) pass the daemon's own
environment through unsandboxed — so any Claude Code setting that changes
model behaviour is inherited on both paths unless something explicitly
neutralises it. The advisor tool (`advisorModel` in settings) is the first one
Istota takes a position on: `ClaudeCodeBrain` / `TmuxClaudeBrain` set
`CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` in the child env whenever the request
won't itself emit `--advisor`, closing the inherited channel on every
`BrainRequest` path at once (advisor-model spec, Stage 1).

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
- `advisor = brain.resolve_model_name(_resolve_advisor(task, config))` when
  `brain.model_namespace == "anthropic"`, else `""`. `_resolve_advisor` returns
  `config.advisor_model` unless `task.model` is set (a per-task model pin drops
  the advisor: the CLI's fatal advisor-gate check is "does the *main* model
  support the advisor tool at all," which is pin-dependent — a genuine
  capability mismatch between two otherwise-advisor-capable models only warns
  and the task still completes, mirroring `_resolve_effort`'s pin-drop rule
  one severity up). `_run_fallback` carries `advisor` across an
  anthropic→anthropic reroute and drops it on anthropic→native (advisor-model
  spec, Stage 3).
- `custom_system_prompt_path = config/system-prompt.md` when `custom_system_prompt = true`
- `streaming = event_writer is not None`
- `on_progress = _on_brain_event`: closure that maps the widened `StreamEvent`
  union to `TaskEvent`s on the `EventWriter` — `ToolUseEvent`→`tool_start`,
  `ToolEndEvent`→`tool_end`, `ToolProgressEvent`→`tool_progress`,
  `TextEvent`→`progress_text`, `ContextManagementEvent`→`context_management`.
  `tool_*` gated on `progress_show_tool_use`, `progress_text` on
  `progress_show_text`; `tool_progress` always emitted (SSE only)
- `cancel_check`: closure that polls `db.is_task_cancelled()`
- `on_pid`: closure that calls `db.update_task_pid()` for `!stop` support
- `sandbox_wrap`: closure over `build_bwrap_cmd(...)` so the brain can wrap
  its raw cmd without knowing anything about bwrap; no-op when sandbox disabled
- `result_file = {user_temp_dir}/task_{task_id}_result.txt`

After `brain.execute()` returns, the executor:
1. Calls `_compose_full_result(result_text, trace)` on success to reconcile
   the final ResultEvent text against substantial intermediate text blocks
   (CM-aware + terse-result recovery — same logic both brains will need).
2. On a dropped-pin fallback (see below), appends the visible model note
   **after** composition.
3. Updates the user skills fingerprint when interactive task succeeded.
4. Returns `(success, result, actions_taken_json, execution_trace_json)` —
   shape unchanged from before the refactor.

## Brain fallback (availability failover)
Generalizes the old hardcoded tmux→claude_code in-attempt rerun. The
`brain.execute(req)` call is wrapped in a routing block (replacing the tmux-only
`if _brain_config.kind == "tmux_claude"` block) that reruns the *same attempt*
(no new DB row, no `attempt_count` increment) through a configured fallback
brain when the primary is unavailable. Kept executor-level: brains have no
`Config` for the operator alert, and the rerun/breaker already live here.

- `_fallback_kind = effective_fallback_kind(_brain_config)` (`brain/_fallback.py`);
  `_cooldown = config.brain.fallback_cooldown_seconds`; `_breaker =
  get_availability_breaker()` (process-global `PrimaryAvailabilityBreaker`).
- **Stickiness:** when the breaker `should_skip(primary_kind, cooldown)`, the
  primary is skipped entirely and the task goes straight to the fallback.
- **Trigger set** `{usage_limit, not_found, fallback}` (+ `transient_api_error`
  iff `fallback_on_transient`, **on by default** since ISSUE-212): on a matching `brain_result.stop_reason` with a
  fallback configured, the executor reruns via `_run_fallback`. **Cooldown set**
  `{usage_limit, not_found}` opens the breaker (`open()` returns True once →
  `_fire_fallback_alert`, one operator alert). `fallback` is excluded from the
  cooldown set (tmux keeps being probed per-task); the tmux launch alert
  (`consume_circuit_open_alert`) is still fired for a `tmux_claude` primary.
  A successful primary run (breaker armed) calls `record_success` to close it.
- `_run_fallback(config, brain_config, fallback_kind, task, req)` →
  `(BrainResult | None, dropped_pin)`. Builds the fallback brain
  (`dataclasses.replace(brain_config, kind=fallback_kind)`, overlaying the
  per-user native key when `native`), resolves model/effort via
  `_resolve_fallback_model_effort`, and reruns `dataclasses.replace(req,
  model=…, effort=…)`, passing the result through `_mark_if_exhausted`. A construction failure returns `None` (keep the primary
  result); an unexpected `execute` exception becomes a failed `BrainResult`.
- `_resolve_fallback_model_effort(task, config, fallback_brain, effort)` →
  `(model, effort, dropped_pin)`. Empty requested model → fallback's own default
  (no note). `is_portable_alias(raw, config_alias_portable_names(config))` → re-resolve the
  intent in the fallback namespace **via `fallback_brain.resolve_alias(raw)`**, so
  both the model *and its effort* are the fallback namespace's own (a customized
  `smart` falling back claude_code→native lands on a valid openai_compat slug +
  effort, not the anthropic value — the role-tier-cross-brain-standardization
  fix); falls back to `resolve_model_name(raw)` defensively if the pair is empty
  (no note either way). Non-portable pin → fallback default + `dropped_pin = raw`
  (INFO log + visible note).
- `_append_model_note(result_text, dropped_pin, primary_kind, actual_model)` —
  pure string→string, appended after `_compose_full_result` and only on success;
  a single italic line naming the dropped pin and the model actually used
  (`actual_model` = the persisted `model_used`). Delivers uniformly across
  surfaces (it's part of `result_text`).

- `_mark_if_exhausted(fb_result)` — when the *fallback* also failed for an
  availability reason (`{usage_limit, fallback, transient_api_error}` — `not_found`
  is excluded: a missing fallback binary is an operator misconfiguration, and
  "try again shortly" would be false),
  prefixes `FALLBACK_EXHAUSTED_MARKER` (`"[brain-fallback-exhausted]"`) onto its
  `result_text`. `scheduler._format_error_for_user` checks that marker first and
  says "both my primary and backup brains are unavailable" instead of echoing a
  raw provider error at the user (ISSUE-212). A marker rather than a formatted
  sentence because `execute_task`'s return contract is a plain string — the
  scheduler owns the user-facing wording, and the underlying cause stays in the
  text for the logs. A *task-level* fallback failure (`timeout` / `oom` /
  `cancelled`) is deliberately not marked: it isn't an availability problem.
  Two consumers, because only one of them is Talk: `_format_error_for_user`
  (the Talk push path) and `scheduler._error_event_message`, which the
  terminal `error` **task event** goes through — stream surfaces (web chat,
  REPL) render that payload directly as the turn body and never touch the
  Talk formatter, so without it the marker and the raw provider text would
  reach the user there. It reworders only provider-availability failures;
  every other failure keeps its original text (useful in the REPL), and
  `tasks.error` keeps the raw text either way.

No fallback configured (`fallback = ""`, non-tmux primary) collapses the whole
block to the plain `brain.execute(req)` call. See `.claude/rules/brain.md`
"Brain fallback" for the classification + portable-alias contract.

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

**The finality rule (ISSUE-211)** bounds both mechanisms. The channel
guidelines promise the model that text written between tool calls streams as
a progress indicator and is not the saved reply, so a text region followed by
a `tool` entry is mid-turn narration by construction — the model kept working
after writing it — and must never become the durable answer. Both mechanisms
therefore pass `trailing_only=True` to `_last_substantial_region`, which
slices the trace at the last `tool` entry before walking, so recovery only
ever sees the model's final message. The one exception is
`_is_back_reference(result_text)` (the `_TERSE_REFERENCE_RE` set — "see
above" / "done"): there the model itself says the answer is earlier, which is
exactly ISSUE-025, so reaching back honours it rather than guessing. Before
the rule, a `<150`-char genuine answer or an empty result promoted whatever
narration preceded the last tool call, and Mechanism A additionally glued
narration onto the answer (its `{"cm_boundary"}`-only delimiter set spans tool
calls). This deliberately revokes the earlier "a tool is NOT a CM-mode
delimiter" property; the cost is that a CM-split answer whose post-tool tail is
under the CM floor now keeps the truncated `result_text` instead of recovering.

`_ensure_final_answer(result_text, trace, task)` is the tail of both paths and
closes the abnormal-end case: when `result_text` is empty and nothing was
recovered, it does *not* fall through to narration. Any text after the last
tool call is adopted outright however short (the size floors exist to protect
a non-empty `result_text`, and there is none); otherwise it returns
`_NO_FINAL_ANSWER_NOTICE` — "The turn ended without a final response." — with
the last mid-turn region appended under a label, so the work stays visible
without being passed off as the answer. Automated tasks are exempt: a
briefing body is parsed as JSON and an empty result already flows to that
module's quiet retry, which prose would break. This is why the executor now
calls composition on `if success:` rather than `if success and trace:` — a
successful turn with no trace at all still must not deliver a blank reply.

Every override logs one INFO line
(`compose_full_result: mechanism=… task_id=… source_type=… original_chars=… recovered_chars=…`)
so the 500-char floor can be calibrated against real production data. The
`no_final_answer` path shares the prefix but logs `partial_chars=…` instead of
the original/recovered pair, so a field-keyed query needs both shapes.
The legacy Jaccard near-duplicate gluing path is gone; `_text_similarity`
remains in the source as a dead helper but is no longer called.

## API retry constants (re-exported from brain.claude_code)
- The live transient rule is **every 5xx**, plus `408`/`425`/`429` (`_status_is_transient`). `TRANSIENT_STATUS_CODES = {500, 502, 503, 504, 529}` is kept as documentation of the common cases and is **not** the gate — enumerating was itself the ISSUE-212 bug class
- `PERMANENT_STATUS_CODES = {400, 401, 403, 404, 405, 413, 414, 422}` — no retry,
  no fallback attempt (retrying or paying for a fallback call that would fail
  identically buys nothing)
- `API_RETRY_MAX_ATTEMPTS = 3`
- `API_RETRY_DELAY_SECONDS = 5` — the default when the provider named no wait, not a floor; superseded per attempt
  by `parse_retry_after(text)` when the provider supplied a `Retry-After`,
  capped at `RETRY_AFTER_MAX_SECONDS = 60`
- Patterns: `API Error: (\d{3}) (\{.*\})` first, then the bodyless
  `API Error:?\s+(\d{3})\b[ \t]*([^\n]*)`
- Retries do NOT count against task attempts
- `parse_api_error`, `is_transient_api_error` re-exported from `executor`
  for `scheduler.py` and tests; canonical home is `brain/claude_code.py`.
  `is_permanent_api_error` / `api_error_stop_reason` / `is_api_error_banner` /
  `parse_retry_after` are new and are imported from `brain.claude_code` directly
  (nothing needs a back-compat re-export).

## Key Constants
- Background task types excluded from context: `["scheduled", "briefing"]`
- Prompt file: `{user_temp_dir}/task_{task_id}_prompt.txt`
- Result file: `{user_temp_dir}/task_{task_id}_result.txt`

## Security Functions
| Function | Purpose |
|---|---|
| `build_clean_env(config)` | Minimal env for Claude subprocess (PATH, HOME, PYTHONUNBUFFERED + `USER`/`LOGNAME` + passthrough vars). `USER`/`LOGNAME` are process-identity basics (not secrets) that the macOS Keychain lookup needs — without them the `claude` CLI's login-Keychain OAuth read fails and every task reports "Not logged in" on a standalone mac; harmless on Linux where the credential is a file under `HOME`. Deliberately does **not** set the cache variables: `proxy_base_env` is built from its output and reaches every host-side skill CLI. Those live in `execute_task`, after that snapshot. |
| `resolve_sandbox_cache_dir(config, user_id)` | This user's package-cache directory, created, or `None`. One predicate for the RW bind, the cache environment in `execute_task`, and `native_fs_roots`, so they cannot disagree. **Two shapes.** With `developer.enabled` and `developer.repos_dir` set it is *derived*, not configured: `{repos_dir}/{user_id}/.package-caches`, inside the subtree the repos bind covers, which is the only shape where uv hardlinks a wheel into a venv instead of copying it (`link(2)` compares mounts, not devices). `security.sandbox_cache_dir` is not consulted at all on that branch. Without them it is `{security.sandbox_cache_dir}/{user_id}` — the fallback, for a deployment running the sandbox without the developer skill, where nothing binds an ancestor and a venv pays the copy as it always did. Per user in both, because uv trusts its unpacked wheels on read, so a shared cache is a cross-user code path. **The containment assertion is the layout in one line**: the directory must resolve to exactly the path the layout names, on both branches. On the derived branch the cache's parent is bound read-write into the task's own sandbox, so a symlink at `.package-caches` would otherwise be created, `chmod 0700`-ed and bound by the daemon — ISSUE-319 back through a name. The mode goes on through an `O_NOFOLLOW` fd, since `mkdir(exist_ok=True)` and `os.chmod` both re-traverse by name. Returned **as written**, not resolved, so a cache under a symlinked `repos_dir` lands on the same mount the repos bind does — otherwise `link(2)` returns EXDEV and every worktree pays for a full copy, silently. **Never raises**; every rejection falls open to the pre-ISSUE-305 behaviour, and the branch selection is inside the `try` because `build_bwrap_cmd` reaches this per Bash call under NativeBrain. Rejects a relative path, a root that is not an existing writable directory, anything under a database directory (checked here, since `_validate_workspace_dir` skips a relative `db_path`), the rest of `_validate_workspace_dir`'s blocklist, and anything at or above a path the sandbox already mounts — see `_sandbox_bind_targets`. The protection checks run against the cache's *parent* on both branches, which is conservative in the only direction that matters and has one consequence with no escape hatch left: a `developer.repos_dir` overlapping the source tree, the mount, a database directory or a `$HOME` dotfile directory loses its disk cache on every task, and the fix is to move `repos_dir`. Warns once per process per distinct refusal. |
| `_sandbox_bind_targets(config)` | What `build_bwrap_cmd` mounts, that a cache must not be mounted *above*. bwrap applies argv in order, so the late cache bind would cover an earlier mount whose destination is beneath it: `$HOME/.cache` over the read-only huggingface bind, `config.temp_dir` over every workspace and the `.developer` credential helpers, `$HOME/.local` over the `claude` binary, `developer.repos_dir` over every user's subtree at once. `_mask_protected` solves the same problem for the masks; this is its counterpart. Equal-or-ancestor, not overlap. It answers **one direction only** — what the cache can swallow, never what can swallow the cache, and inferring the second from the first is what kept ISSUE-319 invisible for a release. The `repos_dir` entry is reachable on exactly one shape, and that is worth naming because the obvious reading is that the derivation made it dead. The derived branch is gated on the **pair** (`developer.enabled and developer.repos_dir`) while this list appends on `repos_dir` alone, so a deployment with the skill switched off and the path still set reads `security.sandbox_cache_dir` *and* carries the entry. That is the sandbox-without-developer deployment the fallback branch exists for, and a `sandbox_cache_dir` at or above `repos_dir` there would cover every user's subtree at once. `resolve_sandbox_cache_dir`'s own docstring still says the entry cannot fire; it is wrong for this reason. |
| `build_stripped_env()` | os.environ minus credential vars (PASSWORD/TOKEN/SECRET/API_KEY/NC_PASS/PRIVATE_KEY/APP_PASSWORD). For heartbeat/cron commands. Always-on. |
| `build_model_cli_env(config)` | `build_clean_env` plus an inherited `ANTHROPIC_API_KEY`. The env for a daemon-side `claude` invocation that sends a **prompt** but is not a task: the `!check` / self-check execution test, which spawns the CLI itself, and conversation-context triage, which now builds a `BrainRequest` around this env instead (`context._claude_cli_triage`, ISSUE-272) rather than spawning directly. `build_clean_env` already carries `CLAUDE_CODE_OAUTH_TOKEN`, so both auth shapes work while the master key, the Nextcloud app password and every service token stay out. Triage was the one prompt-bearing spawn with no `env=` at all and inherited `os.environ` wholesale (ISSUE-232); use this for any new one. (The two `claude --version` probes — `commands.py`, `brain/tmux_claude.py` — still inherit the daemon env; they send no prompt and read only a version string.) |
| `build_allowed_tools(is_admin, skill_names)` | Returns `["Read", "Write", "Edit", "Grep", "Glob", "Bash", "WebSearch", "WebFetch"]`. For ClaudeCodeBrain / TmuxClaudeBrain the *list contents* no longer reach the CLI — both run with `--dangerously-skip-permissions` (no `--allowedTools` allowlist), so the model gets its full default toolset and the bwrap sandbox + network proxy + clean env are the boundary. The list survives as (a) NativeBrain's in-process tool filter and (b) the non-empty/empty signal distinguishing a tool-bearing task from a text-only one. `Agent` + `Workflow` (the harness's multi-agent fan-out) stay denied via `--disallowedTools`. |
| `derive_credential_set(skill_index)` | Every sensitive env-var name declared by any skill manifest. **Manifest-derived** — this replaced the hand-maintained `_PROXY_CREDENTIAL_VARS` frozenset, so adding a credential to a skill's `env:` block is the only step needed; there is no list to keep in step. |
| `derive_proxy_only_set(skill_index)` | Env vars routed to the proxy *without* credential semantics (no auto-authorization, no `credential-fetch` lookup, no per-skill scoping): the manifest `proxy_only: true` vars (`HEALTH_DB_PATH`, `LOCATION_DB_PATH`) plus `_EXECUTOR_PROXY_ONLY_VARS` (`ISTOTA_DB_PATH`, which is in no manifest — the executor sets it imperatively). These aren't secrets, so there is nothing to leak between skills; they are withheld because they name databases. |
| `derive_authorized_skills(selected_skills, skill_index, ctx, hook_env=None)` | Skills authorized for credential access this task: a skill qualifies if it was **selected**, or if **any** of its sensitive `EnvSpec`s resolves (the user has at least one of its credentials configured). `any`, not `all`, so a multi-provider skill (`developer` — GitLab *or* GitHub) authorizes when one provider is set up. Decoupled from skill selection, so a selection miss doesn't lock out a skill the user has clearly configured; the threat model is unchanged because only credentials the user supplied ever resolve. Replaced `_authorized_skills_from_credentials`. `hook_env` (the merged `dispatch_setup_env_hooks` output, which is why the executor now dispatches hooks *before* deriving authorization) is the auto-auth signal for a `source="setup_env"` credential — `_resolve_env_spec` returns `None` for that source by design, so without it such a skill can never auto-authorize: its var is sensitive, so it is stripped from Claude's env, and it is in no authorized skill's credential map, so the proxy never injects it back and the CLI runs unauthenticated. `google_workspace` was the live case — no eager selector (menu-only since keyword selection was removed), hook-sourced OAuth token, hence never authorized on any path. A hook value is per-user (derived from that user's stored token), unlike an EnvironmentFile `fallback_var`, so it is a sound signal. |
| `derive_skill_credential_map(authorized_skills, skill_index)` | Per-skill: the sensitive env vars its own manifest declares. The proxy scopes injection with it, so a skill CLI invocation only ever sees its own credentials. Replaced `_build_skill_credential_map`. |
| `derive_lookup_allowlist(authorized_skills, skill_index)` | Union of the credentials any authorized skill may fetch via `credential-fetch` — the path helper scripts use (the git credential helper, the `gh` / `glab` wrapper). Subtracts `_PROXY_LOOKUP_BLOCKED` as a hard reject (today `ISTOTA_SECRET_KEY`). Replaced `_allowed_credentials_for_skills`. |

## Skill Proxy Authorization Model

The proxy (`skill_proxy.py`) takes two distinct skill sets:
- `allowed_skills` (frozenset): all CLI skills (`cli: true`) — global whitelist used to reject typos / non-existent skill names.
- `authorized_skills` (frozenset): per-task subset returned by `derive_authorized_skills()`. Used purely for the informative-rejection error message returned to the client, and logged at proxy startup as `proxy_authorization task_id=… selected=… authorized=… …`.

The `skill_credential_map` (built from `authorized_skills` via `derive_skill_credential_map`) controls which credential env vars actually get injected for a given skill CLI invocation — that is the real enforcement boundary. Skill selection controls only which skill *docs* (eager bodies) go in the prompt; it no longer gates credential access.

Every proxy rejection emits a structured WARNING — `proxy_rejected task_id=… type=skill|credential … reason=unknown_skill|not_authorized|not_authorized_credential|credential_not_present`. Use these to count selection misses vs. real abuse attempts.

## Output Validation
| Function | Purpose |
|---|---|
| `detect_malformed_result(text, tool_count, ...)` | Validates model output for leaked tool-call XML. Strict mode (Talk): any `</parameter>`, `</invoke>`, `<thinking>` outside code fences is flagged. Lenient mode (other targets): only flags when entire output is syntax fragments (< 20 chars of real content). Malformed results are reclassified as failures and retried. |
| `_compose_full_result(result_text, execution_trace, task=None)` | Two replace-only mechanisms sharing `_last_substantial_region()`: (A) CM-aware — runs whenever `cm_boundary` events exist, returns last segment ≥ 200 chars; (B) terse-recovery — runs only on non-automated tasks with terse `result_text`, segments by `tool` + `cm_boundary`, returns last region ≥ 500 chars. Both bounded by the finality rule; both tail into `_ensure_final_answer`. See "Result composition" section above. Logs every override. |
| `_last_substantial_region(trace, delimiters, min_chars, *, trailing_only=False)` | Shared walker: groups text events into regions split by `delimiters`, returns the joined text of the last region whose length crosses `min_chars`. `trailing_only` first slices the trace at the last `tool` entry, so only the model's final message is eligible (ISSUE-211). |
| `_is_automated_task(task)`, `_is_terse(text)` | Gates for Mechanism B. Automated = source_type in `{scheduled, briefing}` or `heartbeat_silent` or `scheduled_job_id`. Terse = empty, < 150 chars, or matches short-reference regex. `_is_automated_task` additionally exempts a task from the `_ensure_final_answer` notice. |
| `_is_back_reference(text)`, `_ensure_final_answer(result_text, trace, task)` | The ISSUE-211 pair. `_is_back_reference` is the `_TERSE_REFERENCE_RE` match that licenses reaching back past a tool boundary. `_ensure_final_answer` guarantees a completed non-automated turn never delivers an empty reply or a promoted status fragment. |
| `is_no_final_answer(text)` | Public predicate: is this the composer's synthesized no-final-answer output rather than something the model wrote? Callers that *interpret* a result must check it — the scheduler's confirmation gate (a "should I proceed?" inside quoted mid-turn text is not a question awaiting an answer) and memory indexing (boilerplate from a broken turn has no recall value) both do. |

## Other Functions
| Function | Purpose |
|---|---|
| `parse_api_error()` | Extract status_code/message from error text |
| `is_transient_api_error()` | Check if error is retryable |
| `get_user_temp_dir()` | `config.temp_dir / user_id` |
| `_ensure_reply_parent_in_history()` | Force-include reply parent in context |
| `load_emissaries()` | Load constitutional principles (global only, not user-overridable) |
| `load_persona()` | Load persona (user workspace > global) |
| `load_channel_guidelines(config, source_type, user_id=None)` | Load guidelines/{source_type}.md, substituting `{BOT_NAME}`/`{BOT_DIR}`/`{user_id}`. `{user_id}` joined the set so web.md's file-handover link can name a concrete workspace path; skill bodies already substituted it. |
| `_split_credential_env()` | Split an env dict into (matched, rest). Called twice: once with the credential set, once with the proxy-only set. `proxy_base_env = {**env, **proxy_only_env}` is snapshotted *before* `ISTOTA_SANDBOXED` is added, so the host-side CLI isn't told it is sandboxed. |
| `_build_network_allowlist()` | Build host:port allowlist for CONNECT proxy |
| `build_bwrap_cmd()` | Build bubblewrap sandbox command wrapper. Binds the task's own developer subtree, `Path(developer.repos_dir) / task.user_id`, RW when it exists and the task is an admin's with the skill enabled — never the shared root, so no other admin's clones, worktrees, model-written git configs or package cache are in the namespace. Binds `resolve_sandbox_cache_dir(config, task.user_id)` RW when set, **before** the repos bind so that bind covers it (one mount, the hardlink property), before the database masks, and gated on neither admin nor the developer skill — any task running a package manager writes a cache, and without the bind that write lands on bwrap's root tmpfs (ISSUE-305). Binds the per-user exec socket **directory** `{[developer.container] exec_socket_dir}/{user_id}` RW when the backend is `devbox` and `"developer" in authorized_skills` — that second conjunct is the one `_build_network_allowlist` already uses to decide the package registries, so the exec socket is bound exactly where the registries are allowed. The directory rather than the socket file, because a server restart unlinks and recreates the inode; the per-user subdirectory rather than the parent, because the parent holds every user's socket. Binds **no** Docker socket and no `docker` CLI: the allowlist proxy and its bind are both gone, and unlike that proxy this bind cannot be ungated — an allowlist is safe to hand every task, an arbitrary-command channel into a permissive-egress container is not. `selected_skills` was a dead parameter and is now `authorized_skills`, which is the set that decides the bind. Also ro-binds `custom_system_prompt_path(config)` — the file, never its directory. |
| `custom_system_prompt_path(config)` | `config/system-prompt.md` as an absolute path (`abspath`, not `resolve` — the bind lands at the name as written, which is also the name the CLI is handed) when `custom_system_prompt` is set, else `None`. One source for both the `BrainRequest` field and the bind. The config dir is otherwise absent from the sandbox and stays that way: it holds `config.toml`, and emissaries / persona / guidelines / skill bodies all reach the model as content the daemon read. This is the one file the *CLI* opens, inside the namespace — which is why it silently depended on the `sandbox_ro_paths = ["/srv/app"]` default that also exposed the databases, and why narrowing that to `[]` made every task on a `custom_system_prompt` install exit with "System prompt file not found". Caveat: the DB masks run last, so a config dir sitting under `db_path.parent` would shadow the bind. |
| `effective_sandboxing(config)` | Whether the filesystem sandbox is actually in place: `sandbox_enabled` (what the operator asked for) **and** `_bwrap_available()` (what they got). The one name for a predicate four sites need — `native_fs_confinement_active`, `build_prompt`'s `db_masked`, the `ISTOTA_SANDBOXED` marker and the REPL `cwd` choice. Three of them spelled it out inline until ISSUE-308, two under comments calling it "effective sandboxing" with nothing of that name to point at; since one of them decides whether the prompt tells the model its databases are masked, a definition drifting between them would have the daemon making a false boundary claim. Consults the bwrap probe, which shells out once per process and caches — that is why prompt assembly touches `subprocess` at all. |
| `native_fs_confinement_active(config)` | Whether NativeBrain's in-process file tools should be path-confined — `effective_sandboxing(config)`, the same predicate the `cwd` choice uses (NB-1). |
| `native_fs_roots(config, task, is_admin, user_resources, user_temp_dir, workspace_dir=None)` | The `(read_roots, write_roots, write_denied_roots)` for a native-brain task — mirrors `build_bwrap_cmd`'s user-data binds (user temp dir, mount user/channel dirs RW, Talk RO, the task's own `{developer.repos_dir}/{user_id}` subtree rather than the root, per-resource). Includes the per-user package-manager cache (`resolve_sandbox_cache_dir`) as a write root, mirroring the bwrap bind, which is gated on neither admin nor skill selection. **No DB root** — the admin read root went with the bwrap bind. No site/website write root — ISSUE-194 removed that primitive entirely; see `.claude/rules/config.md` under `SiteConfig`. The third element is the RO carve-outs nested *inside* a write root, which containment cannot express — today `{user_temp_dir}/.developer`, matching the `--ro-bind` bwrap applies after binding its parent so the credential helpers can't be replaced. Appended without an existence check: the list is built once per task while bwrap re-checks per Bash call, so gating on existence would leave a `.developer` created mid-run writable here and read-only there. Threaded into `BrainRequest.fs_read_roots`/`fs_write_roots`/`fs_write_denied_roots` when confinement is active. |
| `_execute_simple()` | subprocess.run mode |
| `_execute_streaming()` | Retry wrapper for streaming |
| `execute_task_interactive()` | CLI interactive mode |
