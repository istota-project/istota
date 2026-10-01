---
paths:
  - "src/istota/executor.py"
---

# Executor Internals

## `execute_task()`
```python
def execute_task(task, config, user_resources, dry_run=False, use_context=True,
                 conn=None, event_writer=None) -> tuple[bool, str, str | None, str | None]
```
No `on_progress` parameter (task-event-streaming spec): the scheduler passes an `EventWriter`; the executor emits `task_started` and adapts the brain's `StreamEvent`s through one `executor_stream.TaskStreamAdapter` per task. `None` on dry-run and CLI paths.

Returns `(success, result_text, actions_taken_json, execution_trace_json)`. The trace is interleaved `{"type": "tool"|"text", "text": …}` entries; a Bash `tool` entry also carries `"raw"`, the verbatim command (`_tool_invocation` in `agent/events.py`), so playbooks quote the real invocation (ISSUE-174 fix 1).

### Flow
1. **Temp dirs**: `temp_dir/user_id`, plus the control dir `temp_dir/.control/user_id/task_{id}` (`ensure_task_control_dir`, 0700 per level). Failure is a **returned** failure, never a raise: `process_one_task` handles no exception from `execute_task`.
1b. **Deferred briefing prompt** (ISSUE-143): for a briefing task, `build_deferred_briefing_prompt` builds the prompt here, off the dispatch thread. Failure keeps the placeholder.
1c. **Attachments**: `_pre_transcribe_attachments()` then `prepare_image_attachments()`, before selection and assembly so all see the same paths and OCR.
2. **Resources**: DB + config into `db.UserResource`s.
3. **Skills**: `select_skills()` (deterministic, the only pass) gives the **eager** set; `eligible_skill_names(exclude = selected ∪ their exclude_skills)` gives the **menu**, rendered by `build_disclosure_index` as `skills_index`. Selected means eager, else eligible means menu; no `progressive_disclosure` flag. The menu replaced LLM Pass 2. Logs `skills: eager=N menu=M`.
4. **Skills changelog**: `_SKILLS_CHANGELOG_SOURCE_TYPES` only.
5. **Context**: `_INTERACTIVE_SOURCE_TYPES` with a `conversation_token`.
6-8. USER.md (not briefings), CHANNEL.md (with a token), CalDAV discovery.
8b. Dated memories (`auto_load_dated_days`, not briefings).
8c. `_recall_memories()`, BM25 over `retrieval_query`, with `include_user_ids=[channel:<token>]`. A task is indexed under both namespaces, so `search()` collapses by `content_hash` before `[:limit]` (ISSUE-471).
8d. Knowledge facts, filtered by `retrieval_query`, capped by `max_knowledge_facts`.
8d2. `_recall_playbooks()` (`playbooks.enabled`, not automated/`skip_memory`); `os.utime`s hits so retention keys on last use (ISSUE-174).
8e. `_apply_memory_cap()` truncates recalled, facts, dated, then playbooks (most protected).
9. Confirmation context from `task.confirmation_prompt`.
10. `build_prompt()` returns a `ComposedPrompt` (`.system`, `.user`).
11. Dry run returns both halves via `render_composed_prompt()` (`===== SYSTEM =====` / `===== USER =====`); no file, no request.
12. Writes `prompt.txt` (user) and `system_prompt.txt` (system), `O_NOFOLLOW`, 0600.
13. `task_env.build_task_runtime()` returns a `TaskRuntime`. Credentials split by `_split_credential_env()` twice, proxy-only first. Proxies come back constructed, not entered: the `ExitStack` around step 15 enters them, since they must live across primary, reroute and fallback. Orderings are in its docstring.
14. `BrainRequest`: user half as `prompt`, `composed_system_prompt_path`, tools, env, model/effort, two sandbox-wrap closures, callbacks, `images`.
15. `run_with_failover` over `make_brain(resolve_brain_kind(task.source_type, config.brain, override=task.brain))`.
16. `_compose_full_result`. 16b. Image notes (see below).
17. On success, writes the skills fingerprint on the same `_shows_skills_changelog` local step 4 read: the changelog is spent, not merely shown.

## Which source types are interactive

`_INTERACTIVE_SOURCE_TYPES` (`talk`, `email`, `repl`, `web`, `sms`, `whatsapp`): a live user is behind the turn. Gates conversation context and sticky skills (both also need a token). Not personal memory: `_recall_memories` keys on `exclude_memory` metadata.

`sms`/`whatsapp` joined in ISSUE-500 (before, every text was a first message), at Talk's depth with no cap of their own, since history is all that says what a push-surface conversation is about. Nothing downstream changed: `_build_db_context`'s `_exclude_types` is a denylist; `db._messages_caught_up` is scoped to `_CONVERSATIONAL_SOURCE_TYPES` (`talk`, `web`), so these fall through to the `tasks` reconstruction (the "task history" `sms.md`/`whatsapp.md` name); `tasks.withheld_from_room` stays False since `ingest` resolves a transcript token only for a registered room; the reply-parent branch is inert (no webhook sets `platform_message_id`), and `_build_talk_api_context` is Talk-only. Consequence: conversation triage now runs on these surfaces past `conversation.skip_selection_threshold` (default 3). Tune the threshold, not the tuple.

`_SKILLS_CHANGELOG_SOURCE_TYPES` (`talk`, `email`, `repl`, `web`) is spelled out so a new surface must decide. The changelog is spent where first shown, so an SMS segment or a 1,024-char WhatsApp body would burn it unreadably. Both sites read one local: show-without-spend repeats it, spend-without-show burns it.

Other copies: `transport.routing._INTERACTIVE_SOURCE_TYPES` must stay a subset (held by a test); `web_app._INTERACTIVE_SOURCES` is wider (adds `cli`, `istota_file`); `commands.py` has a narrower one for `!status` / `!stop`.

## `build_prompt()`

Returns `ComposedPrompt`. Recent parameters: `effective_prompt` (what `## User's request` renders: typed request, audio transcript, rendered OCR; a parameter rather than a `task.prompt` mutation because selection and three retrieval passes read the same string; `None` falls back to `task.prompt`), `attachment_status` (path to status phrase), `conn`, `skills_index`, `cli_skills_text`, `confirmation_context`, `knowledge_facts`, `playbooks`.

### The two prompt halves

Split by **authority**, not size (ISSUE-375): standing instructions go to `.system`, outside native compaction; task material to `.user`, which a compaction summary carries. Before, NativeBrain's first compaction replaced identity, rules and tools with a summary. Full rules in `.claude/rules/prompts.md`.
- **No system line may point at user-half material.** Accessible resources (rule 1), `Today's date` / `Current time` / `User timezone` (rules 7, 8) and `Current UTC` (rule 9) are system-half beside the rules naming them; the group line dropped "below". `tests/test_prompt_split.py` asserts each pairing.
- Header scalars (bot name, user id, source, output target, email, token, timezone) go through `_one_line()`. Persona, emissaries, guidelines, changelog and overlays stay multiline.

**System**: header (role, ids, datetime, token, source, output target, email, a database line naming no path, privileges); emissaries (not briefings); persona, workspace `PERSONA.md` over `config/persona.md` (not briefings or `skip_persona`); workspace layout line plus calendars (system because rule 1 names it); tools, then `skills_index` if the menu is non-empty; rules; `config/guidelines/{source_type}.md`; skills changelog; eager skill bodies with overlays.

File-access framing is storage-backend-aware (storage-agnostic-vocabulary spec): Nextcloud via mount, via rclone, or local, keyed on `config.storage_backend`. Local mode says the workspace is the managed area, not the limit of what an unsandboxed bot can read. The executor is the single home of storage framing; skill bodies use `{workspace}` / `{storage}`.

**User**: USER.md, knowledge facts, CHANNEL.md, dated memories, recalled memories, playbooks, conversation context, confirmation context, then the request (`effective_prompt`, OCR framed as untrusted) and the attachment list with per-line location and vision status. `## Response format` precedes the request, since guidelines are instructions.

### The two prompt files

Written to `{temp_dir}/.control/{user_id}/task_{id}/` before the request is built, unconditionally. `prompt.txt` is the exact stdin / tmux / native initial message; `system_prompt.txt` is `composed_system_prompt_path`; `cli_settings.json` is `cli_settings_path`, the `claude` CLI's `--settings` document. `briefing_meta.json` and `attachments/` live there too: every per-task file the daemon authors, nothing the model writes. `task_{id}_result.txt` stays in the per-user temp dir because the model writes it.

The guards name the **directory**, a sibling of the per-user temp dir. `temp_dir` is bound at no path, so nothing model-writable is an ancestor and there is no mkdir-to-mount symlink window (`.developer` survives that window only because the repos bind buries it, ISSUE-320). `get_task_control_dir` refuses an empty or non-`str` `user_id`, one escaping the root (the `get_user_repos_dir` equality) or one casefold-equal to `.control`. `ensure_task_control_dir` makes each level 0700, re-asserts the mode, opens `O_NOFOLLOW | O_DIRECTORY` and refuses a non-directory or one the daemon does not own. The composed path resolves the directory (the in-namespace destination) but not the filename, keeping `O_NOFOLLOW` meaningful; belt-and-braces, kept on purpose.

Two guards, both needed:
- `_extra_ro_binds` hands the directory to `build_bwrap_cmd`, applied after every other bind, under both profiles (a model may `Read` a prepared attachment). `mask_protected_paths` names `temp_dir`, so no DB mask can shadow it.
- `native_fs_roots` returns it in `read_only` and `write_denied`. `read_roots = None` means unconfined, so the deny entry is the only guard on macOS, standalone and shipped Docker; `execute_task` seeds `_fs_write_denied_roots` with it outside the confinement branch. Under confinement the read entry makes it readable. Without the deny a native task could rewrite its own system prompt, and a reroute would read the rewrite.

Only this task's directory is bound, never `.control/{user_id}`, so no task reads another's user half (memory, history, request) or overwrites its system half. Widening routes: `security.sandbox_ro_paths` (`load_config` warns on overlap, once per entry, gated on `sandbox_enabled`) and a `user_resources` row (bounded by `workspace_path` only; `doctor.runtime.task_control_dir` reports it). Deferred-op files stay in the per-user temp dir, model-authored by design. `cleanup_old_temp_files` owns deletion, since the scheduler reads `briefing_meta.json` after `execute_task` returns.

## Image attachments

`prepare_image_attachments()` (`image_attachments.py`) never raises; a failure is a bounded notice plus a metadata-only log. Returns `attachments`, `images` (`ImageInput`s) and `ocr_blocks`. `task.attachments` is updated in memory only, so a retry regenerates rather than stacks.
- Selection and assembly get `effective_prompt` (OCR framed). The three retrieval passes get `retrieval_query` with OCR **unframed** (`ocr_query_text`): `memory.search` ANDs tokens with no `allow_or_fallback`, so framing words would zero every image task's recall.
- The audio transcript still lands on `task.prompt` (the scheduler indexes it after return, and an audio-only send carries the stand-in "Process the attached file(s)"); OCR never does, so a retry does not stack it.
- `untrusted_input` is added to the eager set explicitly when images or notices exist, as defence against metadata change.
- Paths are `resolve()`d, since `_bind` uses the resolved source as destination. An image under none of `image_bind_roots(...)` is copied into `{control_dir}/attachments/` (the nc-data fallback path is bound nowhere); `bind_roots` passed only under `effective_sandboxing`.
- `image_attachment_status()` says `VISION_PREPARED` or the omission reason, never "vision supplied": the brain is not chosen yet and a non-vision model, breaker skip or reroute could falsify it.
- `unread_images(req.images, trace)` + `_append_unread_images_note` name images a CLI brain never `Read`; errs toward silence (a failed `Read` counts, basenames can collide). `brain_delivers_vision(kind, model)` + `_append_vision_dropped_note` mark a blind answer; `None` means no note.

## Environment variable mapping

| Var | Source and rule |
|---|---|
| `ISTOTA_TASK_ID`, `ISTOTA_USER_ID`, `ISTOTA_CONVERSATION_TOKEN` | task fields |
| `ISTOTA_TASK_ATTEMPT` | `attempt_count + 1`, bound once as `task_attempt` and also used for `BrainRequest.attempt`, since `tasks transcript` excludes the live log by equality and drift would be permissive. Set by all three task paths. Derived per call before, which the liveness reaper broke (ISSUE-377). In `_EXECUTOR_PROXY_ONLY_VARS`. |
| `ISTOTA_DB_PATH` | Set for every user, split into the proxy's base env; never in the sandbox. Set unsplit by the unsandboxed cron, skill-task and heartbeat paths. |
| `HEALTH_DB_PATH`, `LOCATION_DB_PATH` | Manifest `proxy_only: true`. Location via `setup_env`. |
| `ISTOTA_SANDBOXED` | `"1"` when `skill_proxy_enabled and effective_sandboxing`. Model env only, added after the proxy snapshot. `skill_client._run_direct` refuses when set. |
| `ISTOTA_DEFERRED_DIR` | user temp dir, always |
| `ISTOTA_EXPERIMENTAL_FEATURES` | CSV of `config.experimental.features`, propagated by every subprocess builder; not credential-flavoured. |
| `ISTOTA_SKILL_PROXY_SOCK` | proxy socket |
| `NC_*`, `CALDAV_*`, `BROWSER_*`, `SMTP_*`, `IMAP_*` | config (`SMTP_FROM` is `bot+user_id@domain`) |
| `ISTOTA_WORKSPACE_PATH`, `NEXTCLOUD_MOUNT_PATH` | `workspace_path`; old name is an alias |
| `ISTOTA_DEVBOX_CONTAINER`, `_DOCKER_CLI`, `_MAX_OUTPUT_BYTES` | `config.devbox.*` when enabled. No socket path is exported and no Docker socket bound; `docker` in `/usr` fails at connect. `ISTOTA_DEVBOX_EXEC_SOCKET` must never exist (ISSUE-284): a path in the model's env can be swapped for a fake answering exit 0. The skill CLI reads it from config. |
| `KARAKEEP_*` | resource `extra` |
| `MONARCH_SESSION_ID`, `MONARCH_CSRFTOKEN` | `secrets` table, the only stored credential; the login route takes email/password transiently. |
| `MONEY_USER`, `FEEDS_USER` | user id / feeds resource |
| `DEVELOPER_REPOS_DIR` | `{repos_dir}/{user_id}` via `setup_env`, admins only, never the root |
| `GITLAB_*`, `GITHUB_*`, `DEVELOPER_AUTHOR_CREDIT`, `GIT_CONFIG_*` | `config.developer.*` |
| `ISTOTA_PATH_PREPEND` | `.developer` (forge token set) and `.developer/exec-shims` (devbox deployment: `[devbox] enabled` + `developer.enabled` + `repos_dir`, replacing the retired `[developer.container] backend`), `.developer` first so wrappers win collisions. Folded onto PATH and stripped. Shims are gated on configuration, never selection: `developer` is menu-only and selected only via sticky skills, so a selection gate would 403 the first build. The socket bind is gated separately on `authorized_skills`. Absent from `proxy_base_env`. |

**No database is reachable from the sandbox.** `build_bwrap_cmd` ends with `--tmpfs` masks over `db_path.parent` and `module_db_root()`, each followed by `--remount-ro` (`_bwrap_supports_remount_ro()`): a writable mask lets `sqlite3` create an empty file that reads as corruption. Read-only makes a nested mask fatal, so `_mask_dir` skips a candidate an earlier mask covers, even a refused one. `--disable-userns` ships with the `--unshare-user` it requires. Nothing binds the framework DB; `sandbox_admin_db_write` is gone. Masks rather than "don't bind it" because not binding it did not hold: module DBs sat inside the old `sandbox_ro_paths = ["/srv/app"]` default. That now defaults to `[]` and is actually parsed. The boundary is skill CLIs scoping by `ISTOTA_USER_ID`, plus the files being absent; the proxy therefore starts unconditionally (the old `if credential_env:` gate ran skills inside the sandbox). Not covered: shapes where the bwrap probe fails, and the standalone install.

Claude Code settings are inherited: the sandbox RO-binds `~/.claude/settings.json`, and the six direct brain callers pass the daemon env. `ClaudeCodeBrain` / `TmuxClaudeBrain` set `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` unless the request emits `--advisor` (advisor-model spec, Stage 1).

## Brain invocation
The brain owns command construction, sandboxing (via the wrap closure), transport, parsing and transient retries; see `.claude/rules/brain.md`. The kind is `resolve_brain_kind(task.source_type, config.brain, override=task.brain)`: room pin, then `[brain.source_type_overrides]`, then `[brain] kind`; same object returned when neither applies. An admitted pin clears `fallback`. Three sites resolve it, each handed `task.brain`.

Request fields:
- `cwd=temp_dir`, `timeout_seconds = task_timeout_minutes * 60`, `streaming = event_writer is not None`, `result_file = {user_temp_dir}/task_{id}_result.txt`, `custom_system_prompt_path` when set.
- `model = task.model or ""`, `effort = task.effort or ""`: never a deployment default (ISSUE-418); the brain fills its own. A pinned model with no effort carries none (`_resolve_effort`).
- `advisor` only for an anthropic-namespace brain, via `_resolve_advisor`; a model pin drops it (the CLI's fatal advisor gate depends on the main model). Kept on an anthropic reroute, dropped to native (Stage 3).
- `on_progress = stream.on_event`: tool use/end/progress, text and context-management events become `tool_start`/`tool_end`/`tool_progress`/`progress_text`/`context_management`; `tool_*` gated on `progress_show_tool_use`, text on `progress_show_text`, progress always (SSE only).
- `cancel_check` polls `db.is_task_cancelled()`.
- `on_pid` calls `SkillProxy.authorize_pid` first (ISSUE-550: a brain that never calls it gets every skill call refused), then cgroup placement and `db.update_task_pid()`.
- `sandbox_wrap` / `native_sandbox_wrap`: closures over `build_bwrap_cmd` with `CLAUDE` and `NATIVE` profiles. Two fields because `_run_fallback`'s `dataclasses.replace` names neither, and one field would hand the Claude namespace to NativeBrain (ISSUE-389).

After execution: on failure with `partial_text`, set `task.partial_result` (ISSUE-372; `result_text` is dispatched on by equality, so it cannot carry it; never on success); compose; append the dropped-pin note after composition; update the fingerprint; return the four-tuple.

## Brain fallback (availability failover)
`run_with_failover(...) -> FailoverOutcome` reruns the same attempt (no new row, no attempt bump) through a fallback brain. Executor-level because brains have no `Config` for the alert. `FailoverOutcome`: `result`, `primary_usage_result`, `ran_fallback` (not derivable: a cooldown skip runs no primary), `usage_effort`, `dropped_pin`, `primary_kind`, `fallback_kind`. The `ExitStack` stays in `execute_task`.

`_failover_notice`: a reroute is a stream boundary, so `flush_thinking()` then `settle_at_tool_boundary()` before the banner, even when `emit_once` dedupes it. No `event_writer` means no-op, and nothing was buffered.

- `effective_fallback_kind(brain_config)`; `fallback_cooldown_seconds`; process-global `PrimaryAvailabilityBreaker`. `should_skip` skips the primary entirely.
- **Trigger set** `{usage_limit, not_found, fallback}` plus `transient_api_error` under `fallback_on_transient` (default on, ISSUE-212). **Cooldown set** `{usage_limit, not_found}` calls `open_primary_breaker(...)`, True once → `_fire_fallback_alert`; it also publishes the availability record, because the window is a deadline off the quota reset (ISSUE-374). `fallback` is not in the cooldown set (tmux is probed per task); `consume_circuit_open_alert` fires for tmux. A primary success calls `record_success`.
- `_run_fallback` → `(BrainResult | None, dropped_pin)`, overlaying the per-user native key; construction failure keeps the primary result; an `execute` exception becomes a failed result. Output goes through `_mark_if_exhausted`.
- `_resolve_crossing_model_effort(..., origin_namespace)`: empty model → fallback default. Same namespace is not a crossing; the pin passes untouched (ISSUE-417). The primary path's origin comes from `_pin_origin_namespace`, which since ISSUE-419 uses `resolve_brain_kind` for an unpinned task (read its docstring first). `None` never matches, so an unknown origin drops the pin (as `commands._clear_pin_across_namespaces` does). A portable alias is re-resolved by `fallback_brain.resolve_alias(raw)` so model and effort are the target's own; a non-portable pin → fallback default plus `dropped_pin` (INFO log and note).
- `_append_model_note`: success only, after composition; one italic line naming the dropped pin and `model_used`.
- `_mark_if_exhausted`: if the fallback also failed on `{usage_limit, fallback, transient_api_error}` (not `not_found`, a misconfiguration), prefix `FALLBACK_EXHAUSTED_MARKER` (`"[brain-fallback-exhausted]"`). A marker because the scheduler owns wording. Read by `scheduler._format_error_for_user` (Talk) and `scheduler._error_event_message` (stream surfaces render the `error` event directly). `tasks.error` keeps raw text; `timeout`/`oom`/`cancelled` are not marked.

No fallback configured (ISSUE-362) skips only the reroute: the breaker still opens and alerts fire, because the sleep cycle and shared blocks read `primary_brain_unavailable`. `_skip_primary` stays gated on a fallback existing.

## Result composition (`_compose_full_result`)
In `session/result.py`, on the brain-agnostic `(result_text, trace)`. Both mechanisms share `_last_substantial_region()` and **replace** `result_text`, never glue:
1. **CM-aware** (ISSUE-026): when `cm_boundary` entries exist, last region ≥ 200 chars (`_CM_SEGMENT_MIN_CHARS`). Automated tasks too.
2. **Terse recovery** (ISSUE-025): split on `tool` and `cm_boundary`, last region ≥ 500 (`_TRAILING_REGION_MIN_CHARS`). Only for `not _is_automated_task` and `_is_terse(result_text)`; skipped with CM events or when already a substring.

**Finality rule (ISSUE-211)**: text before a `tool` entry is narration and never the answer, so both pass `trailing_only=True`. Exception: `_is_back_reference` ("see above", "done"). Cost: a CM-split answer with a sub-floor post-tool tail keeps the truncated result.

`_ensure_final_answer` tails both: with an empty result and nothing recovered, adopt any post-tool text however short, else return `_NO_FINAL_ANSWER_NOTICE` with the last mid-turn region labelled. Automated tasks are exempt (briefings parse JSON). So composition runs on `if success:`.

Overrides log `compose_full_result: mechanism=… original_chars=… recovered_chars=…` (`partial_chars=…` on `no_final_answer`). `_text_similarity` is a dead helper.

## API retry constants (re-exported from brain.claude_code)
- Transient: every 5xx plus 408/425/429 (`_status_is_transient`). `TRANSIENT_STATUS_CODES` is documentation, not the gate (enumerating was the ISSUE-212 bug).
- `PERMANENT_STATUS_CODES = {400, 401, 403, 404, 405, 413, 414, 422}`: no retry, no fallback.
- `API_RETRY_MAX_ATTEMPTS = 3`; `API_RETRY_DELAY_SECONDS = 5` default, superseded by `parse_retry_after`, capped at `RETRY_AFTER_MAX_SECONDS = 60`. Retries do not count as attempts.
- Patterns: `API Error: (\d{3}) (\{.*\})`, then bodyless `API Error:?\s+(\d{3})\b[ \t]*([^\n]*)`.
- `parse_api_error`, `is_transient_api_error` re-exported from `executor`; the newer helpers are imported from `brain.claude_code` directly.

## Constants
Background types excluded from context: `scheduled`, `briefing`. Control dir `CONTROL_DIR_NAME = ".control"`, files 0600: `prompt.txt`, `system_prompt.txt`, `cli_settings.json`, `briefing_meta.json` (read and unlinked by the scheduler), `attachments/`. Result file in the user temp dir, model-written.

## Security functions

**`build_clean_env(config)`**: PATH, HOME, PYTHONUNBUFFERED, `USER`/`LOGNAME` (the macOS Keychain lookup needs them) and passthrough vars. Sets no cache vars, since `proxy_base_env` derives from it.
- Sets `SHELLOPTS=pipefail` last (`shell_exec.pipefail_env`, ISSUE-321), because CLI brains run commands through the CLI's own Bash, which `shell_argv` cannot reach. Not `BASH_ENV`, which names a file to source (an exec inlet); `SHELLOPTS` carries option names only.
- `_SHELL_STARTUP_ENV_VARS` (`BASH_ENV`, `SHELLOPTS`, `BASHOPTS`) is filtered from the passthrough loop as in `build_stripped_env` (an inherited `xtrace` would echo credentials). Strip first, set second.
- `set +o pipefail` is the only escape; no config switch. Bash only: `#!/bin/sh` gets it on macOS, not Debian.

**`resolve_sandbox_cache_dir(config, user_id)`**: the user's cache dir or `None`; one predicate for the bind, the cache env and `native_fs_roots`.
- Derived under `sandbox_cache_is_derived` (`is_admin and developer.enabled and developer.repos_dir`): `{repos_dir}/{user_id}/.package-caches`, on the repos mount so uv hardlinks (`link(2)` compares mounts). Else `{security.sandbox_cache_dir}/{user_id}`. Per user, since uv trusts its wheels.
- Must resolve exactly to the layout path; mode via an `O_NOFOLLOW` fd (ISSUE-319).
- The check-to-mount symlink window is closed by the gate, not the resolver (ISSUE-320, `tests/linux/test_sandbox_cache_dir.py::TestTheCacheBindSymlinkRace`): the covering repos bind is emitted after the cache bind and buries a swap. `native_fs_roots` adds no derived cache root.
- Returned as written, not resolved (else EXDEV and full copies).
- Never raises; refusals fall open to pre-ISSUE-305. Rejects relative paths, non-writable roots, DB dirs, `_validate_workspace_dir`'s blocklist, and anything at or above `_sandbox_bind_targets`. Checked on the parent, so an overlapping `repos_dir` loses its cache. Warns once per refusal.

**`_sandbox_bind_targets(config)`**: mounts a cache must not sit at or above (`$HOME/.cache`, `temp_dir`, `$HOME/.local`, `repos_dir`), counterpart of `_mask_protected`. One direction only (conflating directions hid ISSUE-319). The `repos_dir` entry is live where `repos_dir` is set but derivation is off (skill off, or non-admin).

**`without_claude_runtime_env(env)`**: strips `CLAUDE_RUNTIME_ENV_VARS` (`CLAUDE_CODE_OAUTH_TOKEN`), which NativeBrain never uses and could leak to another provider (ISSUE-390). Three sites: `NativeBrain._hello_payload`, `NativeBrain._start_tool_server` (children read the parent's `/proc/<pid>/environ`), and `proxy_base_env` (no manifest declares the token). Copies, never mutates (`req.env` survives a reroute). `None` and `{}` differ: `ToolEnv.subprocess_env` reads `None` as "inherit the daemon env", so callers put `or None` on the input. Lives in the leaf `claude_runtime_env.py` because `brain/native.py` cannot import `executor`. Proxy-off shapes are ISSUE-393.

**`skill_model_credentials(*sources)`**: copies `SKILL_MODEL_CREDENTIAL_VARS` (token, `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`) for `SKILL_MODEL_CALLERS` (`code_review`) into the per-skill map (ISSUE-409). A copy, not a split, since the task's CLI brain needs it too. A name list, not a manifest flag (`sensitive` is index-wide; a `daemon_env` source would let any skill claim any var). Kept out of lookup by `_PROXY_LOOKUP_BLOCKED`.

**`skill_cli_tls_env` / `skill_model_reachability`** (ISSUE-410): inside `code_review`, `build_model_cli_env` reads `proxy_base_env`, which lacked reachability names. Split on whether the value can harm a CLI that never asked for it. `SKILL_CLI_TLS_VARS` (four trust-store names plus `CURL_CA_BUNDLE`, as `forge_cli._CARRY_EXACT`) only add a CA, so every host-side CLI gets them. `SKILL_MODEL_REACHABILITY_VARS` (proxy triple, `ANTHROPIC_BASE_URL`) redirect traffic, including to this deployment's own loopback services (`browse`), and may carry userinfo, so they go per skill to `SKILL_MODEL_CALLERS` and into `_PROXY_LOOKUP_BLOCKED`. Gap-filler, never override: a name the split moved is not read back (but one manifest declaring `SSL_CERT_FILE` sensitive removes it everywhere). Controls: `tests/test_task_env.py::TestTheReachabilityNames`. Open: `feeds` egress proxying needs a per-skill network policy first.

**`build_stripped_env()`**: `os.environ` minus credential patterns, for heartbeat and cron commands.

**`build_model_cli_env(config)`**: `build_clean_env` plus the proxy triple, TLS names and `ANTHROPIC_*` endpoint vars (ISSUE-395; these callers used to inherit them from `os.environ`). Presence, not truthiness; passthrough wins. Use it for every daemon-side model call that is not a task: `!check`, conversation triage (`context._claude_cli_triage`, ISSUE-272, ISSUE-232), the OCR extractors, `health/explainer.py`, `memory/sleep_cycle.py`, `briefings/shared_blocks.py`, `skills/code_review`. The rule, not the roster. The two `claude --version` probes send no prompt and inherit the daemon env.

**`build_allowed_tools(is_admin, skill_names, *, web_fetch_admin_only=False)`**: the eight core tools. `WebFetch` is dropped for non-admins only under `[brain.native.web_fetch] admin_only` (ISSUE-449); otherwise it is bounded by its own egress policy, not identity. The prompt's Tools section follows the flag and states the withheld case. CLI brains run `--dangerously-skip-permissions`, so the list is NativeBrain's filter and the tool-bearing signal; sandbox, proxy and clean env are the boundary. `Agent` and `Workflow` are denied via `--disallowedTools`.

**`build_daemon_sandbox(config, user_id, *, extra_ro_binds=None)` / `daemon_work_dir`** (ISSUE-397): bwrap for task-less model calls (OCR), which otherwise got the CLI's full toolset host-side. Returns wrap and `cwd` together. `CLAUDE` profile, `db.Task(id=0)`, no token, no `--unshare-net` (must reach the provider). The document is bound by name, read-only. `daemon_work_dir` is shared with the upload routes; its shared-root fallback is the refusal signal. `wrap` is `None` with `sandbox_enabled = false`, and non-`None` does not prove a namespace (ISSUE-381). `is_admin` is the caller's real one: `False` would derive a cache without the covering repos bind (ISSUE-320). Never raises.

**`derive_credential_set`**: every sensitive var in any manifest; declaring it is the only step (replaced `_PROXY_CREDENTIAL_VARS`).
**`derive_proxy_only_set`**: manifest `proxy_only` vars plus `_EXECUTOR_PROXY_ONLY_VARS` (`ISTOTA_DB_PATH`, `ISTOTA_TASK_ATTEMPT`); withheld because they name databases.
**`derive_authorized_skills(selected, index, ctx, hook_env=None)`**: selected, or any sensitive `EnvSpec` resolves (`any`, for multi-provider `developer`). `hook_env` (hooks run before authorization) authorizes `source="setup_env"` credentials, which `_resolve_env_spec` returns `None` for; `google_workspace` was the live case.
**`derive_skill_credential_map`**: per skill, its own sensitive vars; the proxy injects only these.
**`derive_lookup_allowlist`**: union fetchable by the `credential` request (git helper, `gh`/`glab` wrapper), minus `_PROXY_LOOKUP_BLOCKED` (`ISTOTA_SECRET_KEY` plus the model credential and reachability sets). The lookup endpoint is scoped to nothing else: anything holding the socket can ask.

## Skill proxy authorization model
`allowed_skills` (all `cli: true`) rejects unknown names. `authorized_skills` is used only for the rejection message and the `proxy_authorization task_id=… selected=… authorized=…` startup log. `skill_credential_map` is the enforcement boundary; selection only decides which bodies reach the prompt. Every rejection logs `proxy_rejected … reason=unknown_skill|not_authorized|not_authorized_credential|credential_not_present`.

## Output validation
- `detect_malformed_result`: leaked tool-call XML. Strict (Talk): any `</parameter>`, `</invoke>`, `<thinking>` outside fences. Lenient elsewhere: only an output of fragments (< 20 real chars). Malformed becomes a retried failure.
- `_is_automated_task`: `{scheduled, briefing}`, `heartbeat_silent` or `scheduled_job_id`. `_is_terse`: empty, < 150 chars, or the short-reference regex.
- `is_no_final_answer(text)`: callers that interpret a result (the confirmation gate, memory indexing) must check it.

## Other functions
- `get_user_temp_dir()`: `temp_dir / user_id`, a plain join. Its containment lives in the sandbox plan: `sandbox_plan.build_mount_plan` raises `ValueError` when `user_scope.scoped_user_dir(temp_dir, user_id)` is `None`, since `temp_dir` holds every user's `.control/` and the dir is the `--chdir` target (`tests/test_user_dir_containment.py::TestTheSandboxRefusal`).
- `get_task_control_dir`: `None` for a bad id; `task_id` coerced with `int()` (`PurePath` does not collapse `..`). Never raises. `ensure_task_control_dir`: retries once (the temp cleanup can remove an empty level mid-`mkdir`), raises `RuntimeError`, idempotent (`_build_module_briefing_prompt` calls it again).
- `load_channel_guidelines(config, source_type, user_id=None)`: substitutes `{BOT_NAME}`/`{BOT_DIR}`/`{user_id}`.
- `_split_credential_env()`: called twice; `proxy_base_env = {**env, **proxy_only_env}` is snapshotted before `ISTOTA_SANDBOXED`.
- `custom_system_prompt_path(config)`: `abspath` of `config/system-prompt.md` (the name the CLI is handed), else `None`; one source for field and bind. The rest of `config/` is never bound. It once depended on the `/srv/app` default. Caveat: DB masks run last, so a config dir under `db_path.parent` would be shadowed.
- `effective_sandboxing(config)`: `sandbox_enabled and _bwrap_available()`. The one name for `native_fs_confinement_active`, `build_prompt`'s `db_masked`, `ISTOTA_SANDBOXED` and the REPL `cwd` (ISSUE-308); drift would make the prompt claim a false boundary. The probe runs once per process.
- Also: `_ensure_reply_parent_in_history`, `load_emissaries` (global only), `load_persona` (workspace over global), `_build_network_allowlist`, `execute_task_interactive`.

### `build_bwrap_cmd()`
- Binds `{repos_dir}/{user_id}` RW for an admin with the skill enabled; never the root.
- Binds the cache dir RW before the repos bind and the masks, for every task (ISSUE-305).
- Binds the exec socket directory `{[developer.container] exec_socket_dir}/{user_id}` on a devbox deployment when `"developer" in authorized_skills`, the same conjunct that allows package registries. Directory because restarts recreate the inode; per user because the parent holds all sockets. Must stay gated: it is an arbitrary-command channel. No Docker socket, no `docker` CLI.
- Required keyword-only `profile`, no default, so a forgotten one is a `TypeError` (ISSUE-389). It decides only the Claude runtime block (`~/.local/bin`, `~/.local/share/claude`, `~/.local/state/claude`, the `~/.claude` tmpfs) and the custom system prompt file bind.

### `native_fs_roots(...)`
Returns `(read_roots, write_roots, write_denied_roots)`. **Not the boundary** since tools moved into `istota.tool_server` (ISSUE-389); the roots give the model a clear error and are the only confinement on unsandboxed shapes, so they mirror the binds as a projection of the same `MountPlan` (`sandbox_plan.project_fs_roots`). The user dir is scoped in `build_mount_plan` (ISSUE-402). Includes the fallback cache root; no DB root; no site root (ISSUE-194, `.claude/rules/config.md` `SiteConfig`). Denied: RO mounts nested in RW ones (by containment), `{user_temp_dir}/.developer` and `control_dir` (also in `read_only`), appended without an existence check so the list never disagrees with the namespace. Seeded into `BrainRequest.fs_*_roots` under confinement; `execute_task` also seeds the control dir outside it. Gap: a `user_resources` row can reach the control tree only where `temp_dir` is under the workspace; no shipped shape does, and doctor reports it.
