---
paths:
  - "src/istota/brain/**"
  - "src/istota/agent/**"
  - "src/istota/llm/**"
  - "src/istota/session/**"
---

# Brain Module (`src/istota/brain/`)

The executor composes prompt, env and sandbox and hands a `BrainRequest` to a `Brain`, which owns the model call and stream parsing. Memory, skills, sandboxing, deferred writes, result composition (`_compose_full_result`) and malformed-output detection stay in the executor.

**The prompt is three channels**: `prompt` (user half, the user turn), `composed_system_prompt_path` (Istota's standing instructions, system authority), `custom_system_prompt_path` (operator file, each backend's override semantics). The split stops a compacting brain from compacting away its instructions (ISSUE-375). Direct text-only callers supply only `prompt`.

## Layout
`__init__.py`, `_types.py`, `_events.py`, `_aliases.py`, `_roles.py`, `_resolve.py`, `_fallback.py`, `_postures.py`, `claude_code.py` (`build_claude_cli_flags` shared with tmux), `native.py`, `tmux_claude.py`.

## Brain protocol
`model_namespace` (`anthropic` for claude_code and tmux_claude, `openai_compat` for native), `execute(req)`, `resolve_alias`, `resolve_model_name`, `list_aliases`, `validate_alias_override`, and properties `default_model` / `default_effort` (the brain's own configured default, unresolved; per brain since ISSUE-418). Consumers use `make_brain`, never a brain's tables.

## Per-brain model defaults (ISSUE-418)

Defaults live in `[brain.claude_code]`, `[brain.tmux]`, `[brain.native]` `model` / `effort`. The executor sends a task pin or nothing; the brain's own `or` chain applies the default. The old top-level keys were claude_code's defaults applied to every brain, so a native room ran an Anthropic id against OpenRouter.

- Retired top-level keys still load: `config._apply_legacy_brain_defaults` migrates them onto `[brain.claude_code]` and `[brain.tmux]` (same namespace and binary), with a warning, only into an unset block. **Never onto `[brain.native]`**.
- `Brain.with_defaults(req)` is idempotent. Effort: request > block `effort` > effort of the alias the block's `model` names (else the explicit key is unreachable behind an effort-carrying alias).
- `config._warn_native_lost_its_only_model` warns once when native is reachable with empty `model` and the retired key set; a warning because a failed load stops the daemon.
- `model_namespace_for_kind(kind)` is a lookup, not a construction (ISSUE-417; constructing tmux probes the CLI). Used by `webui.app._brain_catalogue`, `commands._model_namespace`, the executor's fallback crossing rule, `scheduler_deferred._inherited_model` (ISSUE-421). `None` means not established, never "same namespace". Buildability is still a construction, asked only for allowlisted kinds.
- `configured_default_model_effort(brain_config)`: same lookup for reporting callers (log line, admin dashboard), unresolved.

## Model identity (single source of truth)

**`:effort`**: `<base>:<effort>`, effort in `EFFORT_LEVELS` (`low|medium|high|xhigh|max`). `split_effort` peels via `rpartition(":")` only for a known level and non-empty base (`provider/model` slugs untouched); called first by every resolver. `opus-high` forms are gone; `opus:high` only.

1. **Operator overrides** (`_roles.py`): `name -> namespace -> RoleTarget(model, effort=None)`; namespace is a `model_namespace` or `"*"` (legacy flat), so a value never leaks onto another brain's wire. `parse_alias_overrides(raw, log=)` builds an immutable `AliasTable` (overrides plus the `portable = true` names) and touches no state; `set_alias_overrides` installs one (`current_alias_table()`). `AliasTable.target(name, ns)`: per-namespace > `"*"` > None. `AliasTable.portable_names()` is the one portable set: the executor's crossing rule parses the config's aliases with it (the old `executor.config_alias_portable_names` duplicate is gone). A table with `effort` and no `model` is an effort-only override: the alias keeps its own target.
2. **The resolver** (`_resolve.py`, `resolve_selection`): split `:effort` -> alias (override, `default`, the tiers, which name families in code, `DEFAULT_TIER_TARGETS` fast = haiku / general = sonnet / smart = opus, and the family shortcuts, each the newest current `anthropic` model of that family in the **live** catalogue) -> the preferred provider's own id -> qualified ref (`provider/id`, first segment a configured provider; with `for_brain`, only a provider that brain reaches) -> bare id across providers (ambiguous refuses with candidates) -> pattern (`allow_patterns`, chat commands only). Then `model_not_runnable` (`for_brain`, via `llm.providers.brain_can_run`), `model_not_enabled` (`enabled_only`; aliases exempt) and `no_tools` (`needs_tools`). Returns `Resolution(ref, info, effort_requested, source, alias, brain_default)` or `ResolveError(code, message, candidates, target)`. `native_reach` is `[brain.native] providers` (`native_reach_ids(config)`, native's default first), passed by every caller that has a config. Strict; the brains stay lenient. `enabled_refs`, `unmatched_patterns`, `default_enabled_patterns` (`anthropic/*` plus native's model) and `enabled_patterns` (`[models] enabled` when set, `[]` included) are the enabled-set helpers.

**A person's selection** (`resolve_for_person`, D9): `for_brain` the brain that will run it, `enabled_only=not is_admin`, `needs_tools`, patterns only where the caller allows them (chat commands, D10). Every writer goes through it: `!model` (`commands.parse_model_prefix` with `config` and the sender), `!room model`, the web room PATCH (`webui.app._room_model_selection`, 422 with `{error, code, candidates}`, a value the room already holds not judged again) and CRON.md (`cron_loader._validate_model`: an admin's unresolvable model is only warned about; a non-admin's refused one sets `CronJob.model_refused`, the file keeps the line and the sync stores no model). What is stored is `stored_selection`: the alias name, else the ref (a bare id or a pattern becomes its ref). **Resolution happens at task creation** (D6): `record_inbound` (`selection_for_task`, on the brain the task runs) and the scheduler's `resolve_pinned_model_effort` write the ref to `tasks.model`, with an alias's effort where none was set, except a portable alias (`is_portable_selection`: the tiers and `portable = true`), whose name is kept so a fallback re-resolves it on the other brain (the model that ran is `tasks.model_used` and `task_usage.model`); what does not resolve to a ref (an alias meaning the brain's own model, an id the catalogue lacks) is kept as written. No enabled-set check there, so an admin's pin runs for every member. `!models` shows the viewer's set by provider; an admin also gets a count per provider and `!models all <provider>`. The seam test is `tests/test_selection_policy.py::test_a_web_room_pick_reaches_the_provider_it_names` (room PATCH, `record_inbound`, then `execute_task` run for real with `make_provider` and the tool server stubbed): `dry_run=True` returns once the prompt is rendered and never builds a `BrainRequest`, so it cannot witness the ref, provider record or clamped effort that reach the request.
3. **`DEFAULT_ALIASES`** in `claude_code`: the shipped view (`OPUS`/`SONNET`/`HAIKU` at import), kept importable for one release; resolution no longer reads it.

**Bare ids keep running where they ran.** The preferred provider (`anthropic` for the CLI brains, `[brain.native] provider` for native) is asked for an exact id *before* the ref reading, so `anthropic/claude-opus-5` on a native brain that uses OpenRouter is OpenRouter's id when OpenRouter lists it. A ref of a provider the brain does not reach is not read as one at all, so the outcome does not depend on whether a discovery cache exists: on an OpenRouter-native brain that does not list `anthropic`, `anthropic/claude-opus-5` is OpenRouter's id with or without OpenRouter's list (it is refused at selection without the list, and sent to OpenRouter as written), never the `anthropic` provider's. Native sends what the resolver cannot attribute to its own provider as written, strips `<its provider>/` only from a ref the catalogue holds, and asks `get_model_info` by the wire id, so a bare Claude id on native keeps `_DEFAULT` and the wire features it had (Stage 1's nine-test finding).

`ClaudeCodeBrain.resolve_alias` is `cli_resolve_alias`: the resolver for `claude_code`, then an override's unresolved target or a Claude-shaped id (`_looks_canonical`: dated ids, models not listed yet) passes through, else None. `resolve_model_name` is `cli_model_name`, which the CLI-run callers (conversation triage, the speech gate) call directly instead of building a `ClaudeCodeBrain()`. `list_aliases`: tiers, shortcuts, custom, each through `resolve_alias`. Every brain has a `kind` class attribute, read by the crossing rule.

`[models.aliases]` takes a flat string, a per-namespace table (`anthropic = "opus:high"`, `openai_compat = {model, effort}`, `{effort}` alone), and `portable = true`. Validation warns only. `[models.roles]` is not read.

**No model id is written outside the catalogue.** `src/istota/llm/data/anthropic.json` is the one place an Anthropic model is named; `scripts/update-anthropic-catalog.py` regenerates it from models.dev plus `anthropic_overrides.json`, and `--check` gates `scripts/release.sh`. `OPUS`, `SONNET` and `HAIKU` in `claude_code` are computed at import as the newest non-deprecated model of each family in that file (`catalog.latest_shipped_id`), kept importable for one release; the live resolver reads `Catalogue.latest` instead, so Anthropic discovery moves the shortcuts and tiers. The setup defaults are functions over the same file (`setup_wizard.default_anthropic_model`). Documents name a family or an alias, never a current id: `tests/test_model_id_docs.py` fails on any family's newest id (or its OpenRouter spelling) in `docs/`, `.claude/rules/`, `config/config.example.toml`, `README.md` or `AGENTS.md` (#548 was those restated ids drifting). A prior version is still written as its id plus modifier (`claude-opus-4-7:high`).

`Config.advisor_model` is top-level (no `[brain.advisor_model]`), resolved via `resolve_model_name` (no effort). Anthropic namespace only; native ignores it. See `.claude/rules/executor.md` § Brain invocation.

## BrainRequest fields
| Field | Notes |
|---|---|
| `prompt` | User half only; `_extract_urls` and `build_image_prompt` read only it, by decision. |
| `allowed_tools` | From `build_allowed_tools()`. CLI brains: non-empty just means tools on (skip-permissions). Empty = text-only, no tool flags, no skip-permissions. |
| `env` | Credential-stripped under the proxy, except the Claude runtime credential. **Mutable and shared**: ClaudeCodeBrain writes `IS_SANDBOX` / `CLAUDE_CODE_DISABLE_ADVISOR_TOOL` in place and `_run_fallback` reuses the object, so filters must copy. |
| `model`, `effort` | Task pin or empty, never a deployment default (ISSUE-418). A model pin without effort carries none. |
| `advisor` | Anthropic brains, set only with `advisor_model` configured and no task model pin. `--advisor` when this and tools are set, else `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` so a host `advisorModel` cannot run one. |
| `custom_system_prompt_path` | Operator file; a missing path is omitted. CLI: `--system-prompt-file` (replace). Native: appended last. |
| `composed_system_prompt_path` | Istota's system half (`system_prompt.txt` in `{temp_dir}/.control/{user_id}/task_<id>/`). `None` for direct callers. Non-`None` is **required** (silent omission recreates ISSUE-375) and **absolute** (opened from two cwds). Survives the reroute's `dataclasses.replace`. |
| `cli_settings_path` | `cli_settings.json` beside it, holding `claude_code.cli_settings_document()`, passed as `--settings <path>`. Same rules: absolute, required once set (the CLI fails a missing file with `Settings file not found`), carried across a reroute. Written for every brain, since a native task can reroute onto a CLI one. With no path, a tool run gets the same document inline. |
| `on_progress` | `StreamEvent`s (tool, text, delta, result, context, thinking; `ToolEndEvent`/`ToolProgressEvent` native only), mapped and deduped by `executor_stream.TaskStreamAdapter`. Loop-based brains dispatch off their loop (ISSUE-111). Native suppresses the final turn's `TextEvent` unless it has no text. |
| `cancel_check`, `on_pid` | Cancel poll; pid report. Native reports its tool server's bwrap pid. Required for skill-proxy peer auth (ISSUE-550). Every brain reports 0 once it has reaped the child (`report_child_reaped`), which clears `worker_pid` (ISSUE-823); a consumer must not treat 0 as a pid. |
| `sandbox_wrap` | CLAUDE profile: the CLI's runtime state (`~/.claude` with `.credentials.json`, `settings.json`, `projects`/`debug`/`todos`) and the operator prompt bind. CLI brains only. |
| `native_sandbox_wrap` | NATIVE profile: no Claude runtime, credential or operator prompt. **Two fields, not one plus a profile**: the reroute's `replace` names neither, so one field would carry CLAUDE into native on `claude_code -> native` (ISSUE-389). Held by `tests/test_brain_types.py`. The control directory is bound read-only under both profiles via `extra_ro_binds`, last. |
| `fs_read_roots`, `fs_write_roots` | Native file-tool allowlist (NB-1) from `native_fs_roots`, only under effective sandboxing; `None` = unconfined. |
| `fs_write_denied_roots` | RO carve-outs: the task control dir always (checked ahead of `ToolEnv`'s unconfined return, so it guards unconfined shapes), plus `{user_temp_dir}/.developer` when confined. `[]` not `None`; write path only (`_in_denied`). Confined, the control dir is also a read root. |
| `images` | `(path, media_type, display_name)`, never bytes, from `executor.prepare_image_attachments`. |
| `is_fallback` | Set only by `executor._run_fallback`; goes to the session-log header (ISSUE-378). |
| `max_turns` | A per-request turn cap, set for an assignment wake (`per_wake_turns`). `NativeBrain._max_turns` runs `min(config.max_turns, max_turns)` everywhere it read the configured cap; the Claude Code brains ignore it. `None` leaves the brain's cap. |

Also `cwd`, `timeout_seconds`, `streaming`, `result_file`.

## BrainResult fields
- `stop_reason`: `completed`/`cancelled`/`timeout`/`oom`/`terminated`/`transient_api_error`/`usage_limit`/`error`/`not_found`/`fallback` (native also `soft_timeout`). `usage_limit` = quota/billing, rerouted.
- `execution_trace`: `{type: tool|text|cm_boundary}`; `tool` entries carry `raw` Bash (`_tool_invocation`) for playbooks (ISSUE-174).
- `usage: BrainUsage | None` (`istota.usage.telemetry`): retyped from `TaskUsage` because `input_tokens` there includes cache reads and `billed_input_tokens` does not; `from_task_usage` reconciles (`totals_source='derived'`). Set on every return; `None` on tmux (no result frame).
- `effort_used`, `model_used`, `brain_kind`: stamped at each brain's `execute` seam (ISSUE-418), so correct on the fallback path. `effort_requested` is the effort after the brain's default was filled and before the clamp; `effort_used` is the clamped level (`""` when the model takes none). Each brain clamps there (`brain._resolve.clamp_effort_level`): the CLI brains through `ClaudeCodeBrain.with_clamped_effort`, over the model's `anthropic/<id>` record, leaving a model the catalogue does not list unchecked, and `--effort` takes the level; native over `get_model_info(model)`, sending the `effort_map` wire value. `brain_kind` in `KNOWN_BRAIN_KINDS`, empty for tmux.
- `partial_text` (ISSUE-372), `work_committed` (vetoes in-brain retry).

## ClaudeCodeBrain

1. **Command**: `claude -p - --disallowedTools Agent Workflow --dangerously-skip-permissions` plus model, effort, prompt and output-format flags. No `--allowedTools`: bwrap and the proxy are the boundary. `Agent`/`Workflow` denied so Istota orchestrates via skills. Text-only gets no tool flags. **A grant made only of `READ_ONLY_TOOLS`** (`Read`, `Grep`, `Glob`: health OCR, the code reviewer) also gets `--tools <the granted ones>` and `--strict-mcp-config`, which drop every other built-in and every MCP server including the account's claude.ai connectors, plus a backstop deny list (`Bash`, `Write`, `Edit`, …). The deny list alone left Monitor, Skill, SendMessage and the connectors (probed on 2.1.280). Not routed through `unsupported`, so a surface cannot drop them and run the grant wide. Any other grant keeps the argv byte for byte.
   - **Settings**: `--setting-sources user` on every call, so no project or local source loads from the CLI's cwd. For a task that cwd is `user_temp_dir` (bwrap `--chdir`), writable by every task of that user, or the shared `config.temp_dir` unsandboxed, so a planted `.claude/settings.json`, `.mcp.json` (with `enableAllProjectMcpServers`) or `CLAUDE.md` used to reach every later task. Repositories under `developer.repos_dir` were never the cwd and never auto-loaded; the one real loss is `istota repl --workspace cwd` inside a repo, which no longer gets its `CLAUDE.md`. `--settings` carries `cli_settings_document()`, the one place istota decides the CLI's settings; today it blanks `attribution.commit` / `attribution.pr`, because the CLI's own prompt tells the model to sign commits and PRs and that instruction beat the `commit` skill's rule. The user source still loads (the host's bound `~/.claude/settings.json`, or tmux's session `CLAUDE_CONFIG_DIR` and its hooks), and `--settings` outranks it. On tmux that `CLAUDE_CONFIG_DIR` is `{user_temp_dir}/.tmux-istota-{id}-{attempt}/config`, predictable and writable from the user's sandbox; the daemon now creates it fresh under an unpredictable name rather than adopting one a task planted (ISSUE-587, under TmuxClaudeBrain below), so nothing left there by an earlier task is read as the user source. A *concurrent* task of the same user can still write into it during the run.
   - **System-prompt flags differ**: operator file = `--system-prompt-file` (replace, `exists()`-gated); composed half = `--append-system-prompt-file`, unchecked, since the CLI fails closed on a missing file.
2. **Subprocess**: streaming uses `Popen(start_new_session=True)` so kills take grandchildren (ISSUE-257). **The simple path is still `subprocess.run`**: it reports its pid (ISSUE-550) but leads no group, so a kill orphans its tree; moving it to `Popen` waits on the ~90 tests patching `subprocess.run`.
3. **Parsing**: `make_stream_parser()`; partial frames go to `on_progress` only, never the trace.
4. **Cancel/timeout**: `cancel_check` between events plus a final re-check; `threading.Timer` timeout; both kill via `kill_process_group` and skip a reaped process (pid reuse).
5. **Signals** (`_signal_result`, ISSUE-191): `-9` -> `oom`, others -> `terminated`; the scheduler reads `is_signal_termination(text)`.
6. **API retry**: 3 attempts on `is_transient_api_error`, delay `Retry-After` capped at `RETRY_AFTER_MAX_SECONDS` else `API_RETRY_DELAY_SECONDS`; not counted in `attempt_count`. Result fallback: `ResultEvent` > `result_file` > stderr.
7. **Usage**: totals from `modelUsage`, not `result.usage`. Context from `message_delta`, never `assistant` frames, where usage would displace the tool event (`tests/test_stream_parser_usage.py`). Unknown `apiKeySource` = `unknown` `cost_basis`. Simple path (ISSUE-271): `_parse_simple_json_output` reads either JSON shape; else raw stdout is the answer.

## Image attachments

Each brain converts paths to its wire shape; base64 never reaches rows or logs. **A model must never be left to infer it saw an image**: every path that cannot deliver pixels names the image and says why (ISSUE-366).

- **Native** `_initial_user_content`: text then images. Per-image refusals: no `supports_vision` (`_NO_VISION_NOTICE`), unreadable, over `_MAX_IMAGE_BYTES` (re-checked, the file can be swapped).
- **Compaction**: `plan_image_pin` returns `(pin, summary_input)`; pinned blocks are removed from the summary input so the "no longer in context" notice covers only real drops. Refused above `_PIN_TOKEN_SHARE` of `keep_recent_tokens` (else `find_cut_point` returns 0 forever).
- **CLI brains** use `Read`: `build_image_prompt` adds `IMAGE_DIRECTIVE_HEADER` with tools, `IMAGE_OMITTED_HEADER` without. An empty tool list is never filled implicitly (ISSUE-395, ISSUE-397). `executor.unread_images` audits the trace.
- **Rejection re-issue** (`is_image_payload_rejection`): once, with `images=[]` and `build_withdrawn_image_prompt`. Skipped on success or `work_committed`; `result_file` unlinked first; timeout is the remainder floored at `_MIN_REISSUE_SECONDS`.

## API error helpers
In `brain/claude_code.py`; `parse_api_error`, `is_transient_api_error`, `is_usage_limit_error` re-exported from `executor`.
- `parse_api_error`: JSON or bodyless `API Error: NNN <text>` (ISSUE-212). For formatting, not deciding.
- `is_transient_api_error`: `_status_is_transient` (every 5xx, 408/425/429; `TRANSIENT_STATUS_CODES` is documentation, enumeration missed 520-526) or a network failure gated on an `API Error` marker (runs on pane text); explicit status wins (NB-13a).
- `is_permanent_api_error`: `PERMANENT_STATUS_CODES` (400/401/403/404/405/413/414/422), context length, content filter.
- `api_error_stop_reason`: `usage_limit` > `error` > `transient_api_error` > None.
- `is_api_error_banner`: text *is* a bare banner (anchored, length-gated). `claude -p` reports failures as success frames. The scheduler's masquerading-success guard and `_is_policy_refusal` key on this, not `parse_api_error`.
- `parse_retry_after`: capped 60s. `is_usage_limit_error`: checked before transient everywhere. `is_image_payload_rejection`: 413, or a 400 naming an image.

**Retry vs reroute**: `_is_retryable` vetoes retry on `work_committed`. Backoff sleeps in `_RETRY_SLEEP_SLICE_SECONDS` slices polling `cancel_check`.

## Configuration
`[brain]`: `kind`, `fallback` (`""` = none), `fallback_on_transient`, `fallback_cooldown_seconds`, `room_selectable`; sub-blocks `claude_code`, `tmux` (`cli_version_pin`, marker lists), `native` (`provider` names a `[providers.<id>]` record and has no default; the key is the provider's), `native.web_fetch`, `native.session_log`, `source_type_overrides`. Reference: `.claude/rules/config.md`.

## Per-room brain selection

`rooms.brain` and `tasks.brain`, nullable. `record_inbound` copies room to task beside `model`/`effort` under the `room_surface` guard (Talk, web and the SMS and WhatsApp phone rooms; not email). A task column so resolution is a pure function of the row, room edits do not change running tasks, and retries/subtasks inherit it (`_create_retry_task`, deferred subtask writer).

**A model pin records its namespace** (ISSUE-420): `rooms.model_namespace` -> `tasks.model_namespace`, preferred by `executor._pin_origin_namespace`, because inferring from `tasks.brain` or the lane is wrong in different cases (ISSUE-421(c)). NULL = old row, old inference. Subtasks carry a model pin only where lanes share a vocabulary (`_inherited_model`, ISSUE-421).

**Order**: `tasks.brain > [brain.source_type_overrides][source_type] > [brain] kind`; an explicit pick beats a rollout knob.

**Two producers, one allowlist**: `rooms.brain` and `scheduled_jobs.brain` (CRON.md, ISSUE-419). `resolve_brain_kind` applies `room_selectable` to any override, so there is no `job_selectable`. CRON.md is model-writable, so `cron_loader.fj_brain_or_none` drops `brain` at sync for non-admins.

Admitted only if buildable **and** listed; each refusal is a WARNING and fallthrough, never a failed task. Shortening the list applies at next dispatch with no row rewrites; `!brain` names the stale pin.

**Refusals log once per process** (ISSUE-422) via `_refusal_is_unreported(arm, kind, source_type)`; `arm` is keyed since one call can refuse twice. Capped (`_WARNED_REFUSAL_CAP`, shared, one suppression line) and truncated (`_REFUSAL_SHOWN_CHARS`) because pins are CRON.md-writable. Survives SIGHUP; `tests/conftest.py` clears it. `_as_text` guards input types; `_shown` is for keys and logs, never the lookup.

**Empty by default and a gate**: kind decides the loop's process, credentials, `SandboxProfile` and tools. Writing is admin-only, reading is not. `room_selectable_kinds` = allowlist intersected with buildable; `config._validate_room_selectable` warns once per load on a non-kind.

**The live delta is egress**: a room's brain covers every member's turns, and native `WebFetch` runs outside the CONNECT allowlist. See "Native WebFetch tool".

### A pinned room has no failover

An admitted override is `replace(brain_config, kind=…, fallback="")`, so failure reports the primary's own `stop_reason`, not `FALLBACK_EXHAUSTED_MARKER`. Pinned cron jobs too: an unattended answer from an unchosen model is worse than a visible failure (retry ladder, auto-disable, `!cron enable`). Hence `reachable_brain_kinds` folds failover over base kind and override targets, **not** `room_selectable`. Pinning the default kind still counts, and `!brain` says so.

The breaker and alert remain: the breaker-open block is not gated on a fallback (ISSUE-362) and `_fire_fallback_alert` fires with `fallback_kind=None`; only `_skip_primary` needs one. `!brain default` restores the inherited brain.

### The model-namespace rule

`rooms.model` holds an alias or a `provider/id` ref since model-catalogue Stage 7 (older rows hold canonical ids), and its namespace is recorded beside it, so the clearing rule below still keys on namespaces.
1. **A cross-namespace brain change clears `rooms.model` and `rooms.effort` together** and says so (`commands._clear_pin_across_namespaces`, also used by the web PATCH). Same namespace keeps it; undeterminable clears.
2. **Every writer and every model-offering surface resolves through the room's brain**: `commands.brain_for_room(config, conn, room_token, source_type)` or `webui.app._brain_for_room_token`. Covers `!model` on Talk (`transport/talk/inbound.py`) and web (`chat_send_message`), `!room model`, `_room_model_selection`, `/chat/commands` (`room_id`), and `!models`/`!help` via `commands._ctx_brain`. The composer autocomplete is unscoped; a stale pick is refused server-side.

`brain_for_room` returns a `BrainConfig` and never raises (`rooms.brain` is untyped TEXT; an exception would hit the Talk poll loop). It skips the per-user native key overlay.

### Surfaces

- `!brain`: bare reports room, lane, default and failover (ungated); `<kind>` sets (admin, allowlisted); `default` clears and is checked before the allowlist (emptying the list is the off switch). `!room` shows brain read-only: one writer.
- `PATCH /api/chat/rooms/{id}`: key-presence contract, admin gate on presence. With `model` and `brain` together: model first, then brain, then the namespace rule. See `.claude/rules/web-chat.md`.
- `doctor` uses `reachable_brain_kinds`; `runtime.native_brain` checks runnable, not buildable.
- `!steer` reads `tasks.brain`; after a mid-attempt fallback it can accept a note nothing reads (pre-existing).

## Brain fallback (availability failover)

The executor reruns the same attempt (no new row, no `attempt_count` bump) through the configured fallback.

- **Classification**: `usage_limit` via `is_usage_limit_error` on all brains (ClaudeCodeBrain before transient; native `_classify_native_error`; tmux in `_build_result` and `usage_limit_markers`, never feeding its launch breaker).
- **Portability**: a contract test requires every brain to resolve each of `CANONICAL_ROLES`. A pin written as a qualified ref (`executor._pin_as_ref`: read on the origin's brain, so not an id the origin's provider lists and not a provider the origin does not reach) crosses when `brain_can_run(target kind, ref)`, else it is dropped; native runs a ref of any provider it reaches and routes it there. Any other pin: `is_portable_alias(name, AliasTable.portable_names())` decides whether it re-resolves in the fallback namespace or is a non-crossing pin. A dropped pin adds an italic model note.
- **Breaker** (`_fallback.py`; executor path in `.claude/rules/executor.md` "Brain fallback"): `PrimaryAvailabilityBreaker`, process-global, per primary kind, separate from tmux's `_BREAKER`. `effective_fallback_kind` = configured `fallback`, None when equal to this config's `kind` (checked here because routed configs inherit `fallback`). The implicit tmux -> claude_code target was removed (ISSUE-362): it left no "off" value.

**Trigger set**: `{usage_limit, not_found, fallback}` + `transient_api_error` iff `fallback_on_transient` (default on, ISSUE-212). **Cooldown set**: `{usage_limit, not_found}` (tmux keeps being probed). **Never**: `oom`, `timeout`, `cancelled`, `error`. `_validate_brain_fallback` warns on an unknown kind and a self-fallback (the only kind the deployment runs), and logs one INFO per process for tmux with no fallback. One level only.

**The cooldown is a deadline** (ISSUE-374): for `usage_limit` on a subscription primary it ends at the quota reset, capped by `fallback_cooldown_seconds` and floored at `MIN_COOLDOWN_SECONDS`. `open_primary_breaker` is the one decision point. `usage.subscription.cached_reset_seconds` reads the disk cache only. Not for `not_found` or native. A repeat failure never moves the deadline.

### Direct-caller availability (ISSUE-181)

Sleep cycle and shared-block synthesis call the primary directly. `_fallback.py` gives them the shared breaker: `primary_brain_unavailable(brain_config) -> (available, reason)` before each call; `report_brain_result(result, brain_config)` opens on `usage_limit`/`not_found` (reason returned only on closed->open: one alert) and closes on success. Shared blocks keep last-known-good; `structured` blocks never call a brain.

The direct `BrainRequest` builders (OCR, explainer, sleep cycle, shared blocks, `code_review`, triage) build env with `executor.build_model_cli_env` (ISSUE-395); roster in `.claude/rules/executor.md`. **The tool grant decides the sandbox**: OCR passes `build_daemon_sandbox(..., extra_ro_binds=[document])` (ISSUE-397), and `code_review` binds its snapshot the same way with every scope withheld, since tools mean skip-permissions and Claude brains ignore `fs_read_roots`. Tool-less callers stay unwrapped and read the real `~/.claude/settings.json`, so settings like `advisorModel` must be neutralised structurally.

### Fallback-compatibility posture registry (ISSUE-181)

`brain/_postures.py` (`TASK_POSTURES`, `task_postures_by_name()`), one per automatic caller with call site: **skip** (sleep cycle, shared blocks, location discovery), **pin** (briefings, ISSUE-180; scheduled `prompt` jobs via `model`), **fail_clean** (health OCR, biomarker explainer). Unlisted tasks use the executor's fallback wrapper.

## NativeBrain

Over the OpenAI-compatible chat API only, against the provider `[brain.native] provider` names: `make_brain` hands `NativeBrain` that provider's `ProviderSpec` (`brain.native_provider_spec`) and every provider record (`brain.native_providers`), and `make_provider(spec)` builds the client. **It picks the provider per request** (D4): `NativeBrain._route` sends a ref of another provider to that provider when the catalogue holds the ref (native's own provider asked for the whole string first, so an OpenRouter id spelling an anthropic ref stays OpenRouter's) and `brain_can_run` says native reaches it; the attempt then runs on a brain built from that spec (`_execute_routed`, `route=False`), with that provider's key, compat and catalogue record (`_model_info`, by ref). The discovery caches are installed before routing. `brain_can_run("native", …)`: its default provider and the providers `[brain.native] providers` lists (opt-in; `native_reach`), nothing else. A key does not add a provider: the `anthropic` key the CLI uses does not make Anthropic a native provider, and neither does being an `openai_compat` provider. `_route` routes only to a listed provider. **Residual until Stage 11 stores the provider apart from the id**: with an `openrouter` default and `anthropic` listed, an `anthropic/...` value is ambiguous; the resolver reads it as OpenRouter's (`reaches` in `resolve_selection`), and doctor's `models.catalogue` warns about the pair. With no provider the brain is still built (config load builds one to validate aliases) and a task fails with the reason; `brain_unconfigured_reason("native", …)` keeps it out of `room_selectable_kinds`, fallback targets and admitted pins. A user's own key (`provider_keys/<id>`) replaces the deployment's in their tasks for native's provider and for a listed provider the task's ref names (`executor._with_user_provider_key(..., model)`); the one-shot triage and speech-gate completers keep the deployment's.
- **Final-turn answer (ISSUE-211)**: `result_text = final_turn_text`; empty -> `session.result._ensure_final_answer` notice. Abnormal stops (`_TRUNCATION_MARKERS`, NB-15; `_PARTIAL_ANSWER_STOP_REASONS`) use `last_assistant_text` under a marker.
- **Trace order (ISSUE-211)**: tools run before `turn_end`, so tool entries buffer in `pending_tools` and flush after the turn's text (plus a post-loop flush). `session/result.py` finality depends on it.
- **Effort**: `reasoning_effort` is `brain._resolve.clamp_effort` of the requested level over `get_model_info(model)`: none for a model without reasoning (an unknown model is `_DEFAULT`, so it gets none, as before), `xhigh`/`max` only where its `effort_map` names them (else clamped to `high`, which is what the provider's retired fold sent), and the map's wire value. The provider sends what it is given.
- **Caching**: `_apply_cache_breakpoints`; on by default only for an `anthropic` provider (`KIND_DEFAULTS`) unless the provider sets `prompt_caching`.
- **Cost**: provider-reported cost wins (OpenRouter only, `_parse_reported_cost`); `cost_usd` None -> catalog, `0.0` -> a real free turn.
- **Model catalog**: `llm.catalog.get_model_info` reads an immutable `Catalogue` snapshot: `model_overrides` > discovered (per provider) > the shipped Anthropic file > `_DEFAULT` (200k, zero price). The shipped file, and Anthropic's discovered records merged over it, are reached by ref (`anthropic/<id>`) only, so a bare Claude id on native still gets `_DEFAULT` (see "Bare ids keep running where they ran" above). `Catalogue.models()` takes each record by its ref, never through `get`, since an OpenRouter id can spell a shipped ref. `price_usage` applies `price_tiers`. **The brain never fetches** (D11): `_ensure_fetched_catalog` only calls `llm.discovery.install_from_cache_dir`, which swaps the discovered layer when a cache file under `{db_path.parent}/model-catalogue/` changed (one stat per file otherwise). The scheduler's `model-catalogue` gate and the daemon's boot refresh do the fetching (`.claude/rules/leaf-modules.md`, "llm/discovery.py"). A provider's declared `models` and `[models.overrides]` are the operator and override layers (`set_config_layers`).
- **Overflow recovery**: <=2 force-compacts + `run_agent_loop_continue` under the shared deadline; a recovery summary that fails (`CompactionOutcome.ok` false) fails the attempt.
- **Compaction failure never cuts**: a failed proactive summary leaves `ctx.messages` alone, logs a `compaction` record with `ok=false`, and is retried at a later boundary (a summary stopped at `max_tokens` counts as failed, as in pi); after `_MAX_PROACTIVE_COMPACTION_FAILURES` (2) the proactive path is off for the attempt and overflow recovery is the safety net. Summary calls count in `TaskUsage` and go out with `prompt_caching=False`.
- **Background compaction** (`compaction_background`, default off; `compaction_background_tokens`, 0 = `derive_background_tokens`): starts the summary in a task below the blocking threshold and places it at a later boundary when the prefix is still in place by identity, else discards it as stale (`placed=false`, usage still counted). Cancelled and awaited on every loop exit. Stage 8 of the pi 1.x round kept it off: it made the primary eval model slower.
- **Reasoning replay** (`reasoning_replay`, default `auto`): `auto` sends stored `reasoning_details` back within the attempt, `on` adds plain-string reasoning under its `source_field`, `off` sends none. Never crosses attempts or compaction summaries. Encrypted items are logged as `{type, length, sha256}`.
- **Re-issue without a feature**: a 400 naming reasoning (`is_reasoning_rejection`) or a strict schema (`is_strict_schema_rejection`) is re-sent once without that feature, which stays off for the attempt (`_REISSUABLE_FEATURES`, `_reissue_once_without` on the retrying provider; one mechanism for both). Logged once and recorded as an `error` record (`reasoning_rejected` / `strict_rejected`).
- **Cache affinity and TTL**: one random `uuid4().hex` per attempt goes out as `prompt_cache_key` when that tri-state resolves on (unset = on for an `openrouter` provider and an `openai_compat` one on `api.openai.com`, off elsewhere including `anthropic`). `prompt_cache_ttl` `"5m"` (default) or `"1h"`; a 1h write is priced at 2x input when no cost is reported and breakpoints were sent. Stage 8 kept 5m: both eval providers cache implicitly and ignore `ttl`.
- **Strict tool schemas** (`strict_tool_schemas`, default `auto`): `strictify_schema` closes objects, requires every property and makes optionals nullable, or returns `None` and that tool goes non-strict; `agent/coercion.py` drops a `None` for an originally optional key. `auto` follows `ModelInfo.supports_structured_outputs` (OpenRouter `supported_parameters` or `model_overrides`), so a catalog without the flag, including the static default for `api.anthropic.com`, sends nothing strict. Default moved to `auto` in stage 8: fewer failed edits on the weaker model, no pass loss.
- **Bash**: runs `bash -o pipefail -c` via `sandbox.shell_exec.shell_argv` (ISSUE-307); 141 carries `SIGPIPE_NOTE`. CLI brains get pipefail via `SHELLOPTS=pipefail` from `build_clean_env` (ISSUE-321); not `BASH_ENV`, which names a file to source.

### Claude runtime credential (ISSUE-390, ISSUE-409)

`build_clean_env` puts the CLI's credential for the process's auth mode in every task env (`[brain.claude_code] auth`, `llm.providers.cli_auth_env`: the OAuth token under "subscription", the `anthropic` provider's key and base URL under "api_key"), from config and never topped up from the daemon's environment, and no manifest declares it. The mode is resolved once per process by `resolve_process_auth` (daemon, web, `istota task -x` / `run` / `repl`, doctor), which also fills `BrainConfig.unconfigured` with native's provider reason and the CLI brains' (no credential, or a non-default `anthropic` base URL under "subscription"); `load_config` resolves nothing, and an env builder in a process that never resolved caches the mode only. Unset, the mode is "subscription" when the token is in the environment, "api_key" when only the legacy `ANTHROPIC_API_KEY` is (the transition row), else none: a provider key alone derives nothing (D15). `sandbox.claude_runtime_env.CLAUDE_RUNTIME_ENV_VARS` + `without_claude_runtime_env` strip those names at **three seams**: `_hello_payload`, `_start_tool_server` (else readable via `/proc/<pid>/environ`), and `proxy_base_env`. `llm.providers.model_caller_env` gives `SKILL_MODEL_CALLERS` (`code_review`) their copy from config; `derive_lookup_allowlist` keeps every model credential name out of lookups.
- Name list, not a `CLAUDE_*` prefix (would eat `passthrough_env_vars`); key-based guard in `tests/test_security.py`.
- Copies, never mutates `req.env`. `{}` and `None` stay distinct (`None` = inherit the daemon env); callers pass `or None` on input.
- Stripped at the seams, not at env build, where the kind is unknown and a `native -> claude_code` fallback would lose auth.

### NativeBrain hardening (NB-1…NB-24)
- **NB-1**: `ToolEnv` enforces symlink-resolved roots from `native_fs_roots` when `native_fs_confinement_active` (`sandbox_enabled` + bwrap); denied before allowed, write path only.
- **NB-3/4**: tiers resolve to `native.model` unless remapped; shortcuts pass through; `[brain.native.model_overrides]`.
- **NB-2/12/15**: SSE `{"error":…}` and EOF without `[DONE]` are `StreamError`; `content_filter` kept; marker on `max_tokens`/`content_filter` answers; `max_completion_tokens` for o-series/gpt-5.
- **NB-18**: `stop_reason` normalized; raw `max_turns`/`loop_detected` -> `completed` with a message.

### Native-brain coding enhancements
- **Edit** (`session/tools/edit_engine.py`): exact then bounded fuzzy, no reflow; `edits[]` multi-edit; `replace_all` exact-only; raw bytes keep CRLF/BOM; fuzzy writes via `apply_replacements_preserving_unchanged_lines`.
- **System prompt** (`_system_prompt_parts`): `CODING_SYSTEM_PROMPT` only with tools, then the composed file, then the operator file (last). The composed part is not tool-gated. Lives on `AgentContext.system_prompt`, untouched by compaction.
- **Parallel tools**: `tool_execution="parallel"`; mutations (Write/Edit/Bash) or `_has_path_overlap` serialize; results in call order.
- **Truncated tool calls**: `stop_reason == "max_tokens"` tool calls are not run; `_truncated_tool_results` returns errors.
- **Recovery hints**: Read names the next `offset=`; Bash spills over-cap output to a temp file (`_SpillWriter`, `bash_spill_full_output`).

### Turn-budget awareness nudge (ISSUE-187 defect 3)

Native, gated on `turn_budget_nudge`, a `max_turns`, and tools.
- `_pick_turn_budget_nudge(turns, max_turns, early_percent, remaining_levels, fired)` counts assistant turns from `new_messages` (monotonic across compaction, like `_max_turns_stop`); once at `turn_budget_nudge_early_percent`, once per `turn_budget_nudge_remaining` level; most urgent wins, overtaken marked fired. Message states steps remaining.
- **Wall clock (ISSUE-373)**: `_turns_left_by_clock` uses the median of the last `_LATENCY_WINDOW` (5) latencies, needing `_LATENCY_SAMPLES_MIN` (3) (median so one long build cannot spend the ladder). Budget `min(max_turns, turns + turns_left_by_clock)` toward the soft deadline; no estimate = unchanged.
- `_extract_system_prompt` adds one non-numeric pacing line (a number would anchor).
- Injected via `prepare_next_turn` (`_next_budget_nudge`) into `ctx.messages` only, not `new_messages`; wire role user, framed by `_TURN_BUDGET_FRAME`.

### Tool-failure nudge

`tool_failure_nudge_after` (default 0 = off). A failure is `is_error` or, for Bash, `bash_result_failed` on the result text (Bash leaves `is_error` False on a non-zero exit). `tool_health` counts consecutive failures across tools and survives overflow recovery; `_next_failure_nudge` fires once per attempt at the threshold, beside and after the budget nudge, never while aborted, tool-bearing tasks only. Its breakdown names the first word of failing Bash commands. Writes a `nudge` record with `phase="tool_failure"`. Stage 8 of the pi 1.x round shipped it off: the same number of failed calls before the first success with it on as off.

### The soft deadline (ISSUE-373)

`_soft_deadline_stop` at `soft_deadline_percent` (90) of the task timeout returns `soft_timeout` (in `_PARTIAL_ANSWER_STOP_REASONS`), salvaging work the hard clock would discard.
- Only on a turn that called tools, and only with `allowed_tools` (else it mislabels a finished run, and text-only callers parse JSON).
- No text -> the `timeout` shape (`success=False`), so retry applies.
- **A cancel outranks every stop condition**: all decline while `abort.is_set()`, or a cancel during tool execution becomes a success.
- `_PARTIAL_ANSWER_STOP_REASONS` derives from `_PARTIAL_ANSWER_MARKERS` (subscripted unguarded). Hard deadline stays; 0 or >=100 disables.

### Partial work on a discarding stop (ISSUE-372)

`partial_text` = last text-bearing turn on `timeout`/`cancelled`. Separate field because the scheduler matches `"Cancelled by user"` by exact equality. Both brains fill it; ClaudeCodeBrain cancel/timeout returns keep `actions_taken` and trace (ISSUE-183).

### The tool server (native-only)

One `python -m istota.sandbox.tool_server` per attempt via `build_bwrap_cmd(..., profile=NATIVE)`, in the task cgroup, pid via `on_pid`; six proxy tools (`session/tools/remote.py`); `WebFetch` stays in the daemon. Replaced a Python path policy and a per-call Bash namespace carrying `.credentials.json` (ISSUE-389).
- Transport: inherited socketpair (`pass_fds`), nothing nameable; `close_fds` keeps it from Bash. Protocol `sandbox/tool_server_protocol.py`.
- A dead server, `fatal` or bad frame fails the attempt naming the tool server, checked before `timed_out`/`aborted` (else "Cancelled by user").
- No enable/disable flag (two paths forever). Without bwrap it runs unwrapped with `ToolEnv` as before. Text-only spawns nothing.

### Native WebFetch tool (daemon-side, SSRF-hardened)

`session/tools/web_fetch.py`, daemon netns, available to every user unless `admin_only` (ISSUE-449), which has no `WebFetchPolicy` counterpart. The egress fields (`allow_hosts`, `block_hosts`, `extra_blocked_cidrs`, `allowed_ports`, `allow_http`) bind every caller alike, which identity never did. `require_url_provenance` was rejected as a non-admin default (WebSearch-then-read fails it). **The prompt's withheld predicate asks the routing question and `build_allowed_tools` does not**: CLI-brain tasks keep the CLI's own `WebFetch`.
- Own `httpx.AsyncClient`, `trust_env=False`, no cookies, GET/text only.
- `_ip_is_public` on every resolved IP of every hop, fail closed; connection pinned to the validated IP (Host + SNI); manual redirects; no https->http unless `allow_http`.
- Caps on bytes, chars, redirects, time; honours abort.
- Output framed `[UNTRUSTED WEB CONTENT …]` with a bounded `Fetched:` header (see `lib/untrusted.py`, `.claude/rules/leaf-modules.md`). The executor folds `untrusted_input` into eager skills when native WebFetch is on and not withheld (`_native_web_fetch_enabled`).
- Residual: GET exfiltration (as `browse`). `require_url_provenance` corpus is `_extract_urls(req.prompt)` only, never tool output.

### Session logs (native-only, `session/session_log.py`)

The CLI brains have their own JSONL (bound into the sandbox on purpose); istota's are bound nowhere.
- **One file per attempt**: `{dir}/{user_id}/{ts}_task-{id}-{attempt}.jsonl`, 0600 in 0700, `O_EXCL` with a `session_id` suffix on collision. `attempt` 1-based (`attempt_count + 1`). Linear records, no resume.
- **`is_fallback`** (ISSUE-378) from `BrainRequest.is_fallback` pairs a transcript with its `task_usage` row; always written; read tri-state (`_as_bool_or_none`). **Open**: `attempt_seq` in the header.
- Records: `session`, `context`, `message`, `compaction`, `steer`, `nudge`, `error`, `result` (uncapped `result_text`), `serialization_error`.
- **Wiring is native.py only**: `message_end -> log.message`; **do not hook `turn_end`** (double writes). `result` once at the end of `_execute_sync`.
- **Header redaction is the caller's**: no `api_key`/`extra_headers`, `base_url` -> `base_url_host`.
- **Caps**: images as metadata plus `sha256`, never bytes; text head+tail at `max_content_chars`; args over `max_args_chars` become a marker; `0` = no cap.
- **Never raises**: first failure warns once and disables. No writer when disabled, `task_id <= 0` or empty `user_id`.
- **Location**: `resolve_session_log_dir`; `""` -> `{db_path.parent}/logs` (`/data/db/logs` on Docker; `/data/logs` would sit outside the mask). Never `user_temp_dir`.
- **The boundary is that nothing binds it; the DB mask is defence in depth**: masked on the stack, whose run contract lets bwrap run; `_mask_dir` refuses where `db_path.parent` is the workspace. "Nothing binds it" is a default: a non-empty `sandbox_ro_paths`, or with the database in the workspace a `user_resources` row `resource_path = "logs"`, would expose it. `tests/test_sandbox.py::TestSessionLogContainment` pins the defaults. Same residual as the task control tree.
- **Doctor** `runtime.session_log_dir` asks `executor.mask_shadowed_by` and `effective_sandboxing` (ISSUE-381), reporting both. Under `probe=False` it uses `effective_sandboxing_if_known`; unknown is `_MASK_UNKNOWN`, never OK.
- **Operator `dir` is trusted** (like `sandbox_cache_dir`); `/`, `.`, `..` and null bytes refused; the sweep unlinks `*.jsonl` at any depth under it.
- **Retention** (step 7b): `retention_days` or `max_total_gb`, gated on `enabled`, so **`enabled = false` stops the sweep too** (open question). Evict largest user first, then oldest; never inside `LIVE_WINDOW_SECONDS`.
- **Reading** via `session/session_log_read.py` (one parser for `istota session …` and `istota-skill tasks transcript`; see `.claude/rules/skills.md`).
- The operator sets them in their own config.

## TmuxClaudeBrain (`brain/tmux_claude.py`)

Interactive `claude` TUI in detached tmux: same binary and OAuth token, on subscription limits rather than metered credit. Resolution delegates to a composed `ClaudeCodeBrain`. `kind = "tmux_claude"` switches the whole instance.

**The server and the pane.** Every tmux argv is `_tmux_argv` (`tmux -u -L istota ...`, a private socket, `-u` since the server has no locale variables) run under `_tmux_server_env` (PATH and HOME only), `_load_buffer` included, so the server never takes the daemon's environment as every pane's global environment; `tests/test_tmux_cli_auth.py` fails on an argv built elsewhere. The pane gets `pane_env(env)` on `-e`: the task env minus every model credential name, since a value on `-e` is on tmux's argv. Under "subscription" the CLI reads the token from `$CLAUDE_CONFIG_DIR/.credentials.json` (it ignores the home copy once `CLAUDE_CONFIG_DIR` is set), so `_link_credentials` links the session's config dir to the home file the CLAUDE wrap binds. Under "api_key" `_write_key_helper` puts the key in a 0600 file in `BrainRequest.cli_key_dir` and a settings document there naming an `apiKeyHelper` (`cat <file>`) with `ANTHROPIC_BASE_URL` in its `env`, which `--settings` then names in place of the control dir's; only the CLAUDE wrap binds that directory, and the attempt removes it when it ends. A missing key or directory fails the attempt with `error`. Both paths are built to the model-catalogue spec's design; the stack-level probe that confirms them in the shipped image is owed.

Per attempt a workdir under `ISTOTA_DEFERRED_DIR` holds a session `CLAUDE_CONFIG_DIR` whose hooks write sentinels. bwrap wraps `claude`, never tmux.

**The workdir is in a task-writable directory** (ISSUE-587). It has to be: the hooks run inside the sandbox and write the sentinels, and the CLI writes its session state under `CLAUDE_CONFIG_DIR`, so neither can move to the read-only control dir. What the daemon does there is constrained instead. The name is the session label plus a random tail (`_workdir_name`), because the label `istota-{id}-{attempt}` is predictable for a later task and is reused by a re-run at the same attempt count (`confirm_task`, `release_task_for_restart`). The workdir and its `config/` are created with `mkdir(mode=0o700)` and no `exist_ok`; anything already at the name fails the attempt (`stop_reason="error"`, never `fallback`, which would feed the launch breaker) and is left alone rather than `rmtree`'d. `settings.json` and `.claude.json` go through `_write_new_file` (`O_CREAT | O_EXCL | O_NOFOLLOW`, 0600), and `_seed_onboarding` no longer swallows a failed write. The prompt is never a file: `_load_buffer` hands it to `tmux load-buffer -` on stdin.

**Everything read back goes through `_SessionDir`, built before the CLI launches.** The session's own model can rename its workdir and leave a symlink at the name pointing at another session's directory, and every read by path would then return that session's answer and transcript as this one's (reproduced in review). So `_SessionDir` holds the workdir open as an `O_DIRECTORY | O_NOFOLLOW` descriptor and reads both sentinels relative to it (`dir_fd`, `O_NOFOLLOW`), and resolves `config/` once into `config_root`, checked against the descriptor's inode. A `transcript_path` from either sentinel is honoured only if it resolves under that fixed string; nothing re-resolves the root through the workdir. The tailer's glob fallback reads `config_root/projects`, not the daemon's `~/.claude/projects`, which holds every `claude_code` task's transcript for every user. `_wait_for_completion` still polls the stop sentinel by path; a swap can only end the wait early, since the read then goes through the descriptor.

Residuals, stated rather than closed. A transcript that passed the check is opened again by path by `parse_transcript` and the tailer, so the model can swap a directory *inside* `config/` between the check and the open; the target then needs a file name the model can predict and contents the Claude transcript parser accepts. A concurrent task of the same user can write into the fresh `config/` in the moments before the CLI starts, and `_write_hooks` / `_seed_onboarding` write by path under the workdir (each refuses an existing final component). Closing either means walking every component with `openat` from the pinned descriptor. Unverified on a live host: that the CLI writes its transcript under `CLAUDE_CONFIG_DIR/projects`; if it does not, the result text still comes from the Stop payload and only the trace is lost, with a warning logged.

Hardening (`Specs/Done/claude-tmux-production-readiness.md`):
- Per-session hooks (§2) so concurrent tasks cannot cross-fire. Completion (§3): sentinel, cancel, `error_markers`, dead pane, else timeout.
- Transient retry (§3): fresh session, `API_RETRY_MAX_ATTEMPTS` (3), `API_RETRY_DELAY_SECONDS` (5), not counted. `_build_result` uses `_success_frame_stop_reason` and pane errors return `transient_api_error` when retryable (ISSUE-212).
- Launch failures return `fallback`/`not_found` (§4). `_CircuitBreaker` opens after `fallback_trip_threshold`, short-circuits for `fallback_cooldown_seconds`, arms one alert via `consume_circuit_open_alert()`; per process, reset by success. It, not the availability breaker, governs tmux skipping.
- Streaming (§10): `_TranscriptTailer` forwards blocks during the turn; the Stop parse stays authoritative (`forward_progress=tailer is None`). Token-level streaming unbuilt.
- `_inject_prompt` confirms submission via `_turn_started` and resends Enter only if needed, up to `_SUBMIT_MAX_ATTEMPTS`.

**Known gaps**: hook discovery under bwrap is assumed (fallback: bwrap `--chdir`). `_TMUX_UNSUPPORTED_FLAGS` is empty; `--append-system-prompt-file` must never go in it, and a TUI rejection of it is a release blocker. Hook reliability, partial-flush and network isolation are validated only on the prod host.

## Adding a new brain
Implement the protocol, add the kind to `make_brain()`, extend `BrainConfig`, update `executor._build_network_allowlist()`, report `on_pid` (else skill-proxy calls are refused) and `report_child_reaped` once the child is gone, and test success, retry, cancel, timeout, oom and malformed output.

## Task Event Streaming

`EventWriter` (`events.py`) persists `TaskEvent`s to `task_events` (the table is the bus) and notifies in-process subscribers. Kinds: `task_started`, `tool_start`, `tool_end`, `tool_progress`, `progress_text`, `text_delta`, `context_management`, `brain_fallback`, `confirmation`, `result`, `error`, `cancelled`, `done`.
- **`text_delta`**: stream surfaces only. **Narration gate**: nothing streams until `scheduler.stream_text_gate_chars` (280) is crossed without a tool call; at a tool boundary (`settle_at_tool_boundary`) a short lead-in is dropped and a crossed block flushed whole.
- **`brain_fallback`** (ISSUE-278): emitted before the fallback runs; payload `primary`, `reason`, `fallback`, `model` (empty iff `dropped_pin`), `dropped_pin`, `text` (`executor.fallback_notice_text`). A stream boundary. Live-only; the durable record is `_append_model_note`.
- Retries keep the log; a "retrying" `progress_text` is emitted and `seq` resumes from `db.get_max_task_event_seq`. `webui.app._synthetic_terminal_events` synthesizes a terminal frame for a terminal task with no deliverable `done`. Rows deleted only in `cleanup_old_tasks` (`ON DELETE CASCADE` is decorative).
