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
`__init__.py`, `_types.py`, `_events.py` (root `stream_parser.py` is a shim), `_aliases.py`, `_roles.py`, `_fallback.py`, `_postures.py`, `claude_code.py` (`build_claude_cli_flags` shared with tmux), `native.py`, `tmux_claude.py`.

## Brain protocol
`model_namespace` (`anthropic` for claude_code and tmux_claude, `openai_compat` for native), `execute(req)`, `resolve_alias`, `resolve_model_name`, `list_aliases`, `validate_alias_override`, and properties `default_model` / `default_effort` (the brain's own configured default, unresolved; per brain since ISSUE-418). Consumers use `make_brain`, never a brain's tables.

## Per-brain model defaults (ISSUE-418)

Defaults live in `[brain.claude_code]`, `[brain.tmux]`, `[brain.native]` `model` / `effort`. The executor sends a task pin or nothing; the brain's own `or` chain applies the default. The old top-level keys were claude_code's defaults applied to every brain, so a native room ran an Anthropic id against OpenRouter.

- Retired top-level keys still load: `config._apply_legacy_brain_defaults` migrates them onto `[brain.claude_code]` and `[brain.tmux]` (same namespace and binary), with a warning, only into an unset block. **Never onto `[brain.native]`**. `render-config.sh` (`ISTOTA_BRAIN_CLAUDE_CODE_MODEL` / `ISTOTA_BRAIN_TMUX_MODEL`, falling back to `ISTOTA_MODEL`) and Ansible (`istota_brain_claude_code_model` / `istota_brain_tmux_model` from `istota_model`) migrate earlier.
- `Brain.with_defaults(req)` is idempotent. Effort: request > block `effort` > effort of the alias the block's `model` names (else the explicit key is unreachable behind an effort-carrying alias).
- `config._warn_native_lost_its_only_model` warns once when native is reachable with empty `model` and the retired key set; a warning because a failed load stops the daemon.
- `model_namespace_for_kind(kind)` is a lookup, not a construction (ISSUE-417; constructing tmux probes the CLI). Used by `web_app._brain_catalogue`, `commands._model_namespace`, the executor's fallback crossing rule, `scheduler_deferred._inherited_model` (ISSUE-421). `None` means not established, never "same namespace". Buildability is still a construction, asked only for allowlisted kinds.
- `configured_default_model_effort(brain_config)`: same lookup for reporting callers (log line, admin dashboard), unresolved.

## Model identity (single source of truth)

**`:effort`**: `<base>:<effort>`, effort in `EFFORT_LEVELS` (`low|medium|high|xhigh|max`). `split_effort` peels via `rpartition(":")` only for a known level and non-empty base (`provider/model` slugs untouched); called first by every resolver. `opus-high` forms are gone; `opus:high` only.

1. **Operator overrides** (`_roles.py`): `name -> namespace -> RoleTarget(model, effort=None)`; namespace is a `model_namespace` or `"*"` (legacy flat), so a value never leaks onto another brain's wire. `set_alias_overrides` normalizes the three forms and strips `portable = true` into `_portable_names` (`get_portable_alias_names()`). `get_alias_override_target(name, ns)`: per-namespace > `"*"` > None.
2. **`DEFAULT_ALIASES`** per brain: base alias -> `(model_id, default_effort)`, tiers (`fast`/`general`/`smart` = `CANONICAL_ROLES`) and shortcuts (`opus`/`sonnet`/`haiku`/`default`). Replaced `MODEL_ALIASES` + `DEFAULT_ROLE_TARGETS`.

`resolve_alias`: split -> override -> `DEFAULT_ALIASES` -> canonical-id passthrough -> None; suffix effort beats entry effort; override targets resolve through `DEFAULT_ALIASES`, explicit `RoleTarget.effort` wins. `resolve_model_name` strips effort. `list_aliases`: tiers, shortcuts, custom.

`[models.aliases]` takes a flat string, a per-namespace table (`anthropic = "opus:high"`, `openai_compat = {model, effort}`), and `portable = true`. Validation warns only. `[models.roles]` is not read.

ClaudeCodeBrain constants (bare `opus`/`sonnet`/`haiku` always mean these):
- `OPUS = "claude-opus-5-5"`
- `SONNET = "claude-sonnet-5"`
- `HAIKU = "claude-haiku-4-5"`

Prior versions are canonical id plus modifier (`claude-opus-4-7:high`). The ids are restated here, in `docs/architecture/brain.md` and `config/config.example.toml`; `tests/test_model_id_docs.py` fails until all match (#548).

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
| `on_progress` | `StreamEvent`s (tool, text, delta, result, context, thinking; `ToolEndEvent`/`ToolProgressEvent` native only), mapped and deduped by `executor_stream.TaskStreamAdapter`. Loop-based brains dispatch off their loop (ISSUE-111). Native suppresses the final turn's `TextEvent` unless it has no text. |
| `cancel_check`, `on_pid` | Cancel poll; pid report. Native reports its tool server's bwrap pid. Required for skill-proxy peer auth (ISSUE-550). |
| `sandbox_wrap` | CLAUDE profile: the CLI's runtime state (`~/.claude` with `.credentials.json`, `settings.json`, `projects`/`debug`/`todos`) and the operator prompt bind. CLI brains only. |
| `native_sandbox_wrap` | NATIVE profile: no Claude runtime, credential or operator prompt. **Two fields, not one plus a profile**: the reroute's `replace` names neither, so one field would carry CLAUDE into native on `claude_code -> native` (ISSUE-389). Held by `tests/test_brain_types.py`. The control directory is bound read-only under both profiles via `extra_ro_binds`, last. |
| `fs_read_roots`, `fs_write_roots` | Native file-tool allowlist (NB-1) from `native_fs_roots`, only under effective sandboxing; `None` = unconfined. |
| `fs_write_denied_roots` | RO carve-outs: the task control dir always (checked ahead of `ToolEnv`'s unconfined return, so it guards unconfined shapes), plus `{user_temp_dir}/.developer` when confined. `[]` not `None`; write path only (`_in_denied`). Confined, the control dir is also a read root. |
| `images` | `(path, media_type, display_name)`, never bytes, from `executor.prepare_image_attachments`. |
| `is_fallback` | Set only by `executor._run_fallback`; goes to the session-log header (ISSUE-378). |

Also `cwd`, `timeout_seconds`, `streaming`, `result_file`.

## BrainResult fields
- `stop_reason`: `completed`/`cancelled`/`timeout`/`oom`/`terminated`/`transient_api_error`/`usage_limit`/`error`/`not_found`/`fallback` (native also `soft_timeout`). `usage_limit` = quota/billing, rerouted.
- `execution_trace`: `{type: tool|text|cm_boundary}`; `tool` entries carry `raw` Bash (`_tool_invocation`) for playbooks (ISSUE-174).
- `usage: BrainUsage | None` (`istota.usage`): retyped from `TaskUsage` because `input_tokens` there includes cache reads and `billed_input_tokens` does not; `from_task_usage` reconciles (`totals_source='derived'`). Set on every return; `None` on tmux (no result frame).
- `effort_used`, `model_used`, `brain_kind`: stamped at each brain's `execute` seam (ISSUE-418), so correct on the fallback path. `brain_kind` in `KNOWN_BRAIN_KINDS`, empty for tmux.
- `partial_text` (ISSUE-372), `work_committed` (vetoes in-brain retry).

## ClaudeCodeBrain

1. **Command**: `claude -p - --disallowedTools Agent Workflow --dangerously-skip-permissions` plus model, effort, prompt and output-format flags. No `--allowedTools`: bwrap and the proxy are the boundary. `Agent`/`Workflow` denied so Istota orchestrates via skills. Text-only gets no tool flags.
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
`[brain]`: `kind`, `fallback` (`""` = none), `fallback_on_transient`, `fallback_cooldown_seconds`, `room_selectable`; sub-blocks `claude_code`, `tmux` (`cli_version_pin`, marker lists), `native` (key via `ISTOTA_BRAIN_NATIVE_API_KEY`), `native.web_fetch`, `native.session_log`, `source_type_overrides`. Reference: `.claude/rules/config.md`.

## Per-room brain selection

`rooms.brain` and `tasks.brain`, nullable. `record_inbound` copies room to task beside `model`/`effort` under the `room_surface` guard (Talk, web; not email). A task column so resolution is a pure function of the row, room edits do not change running tasks, and retries/subtasks inherit it (`_create_retry_task`, deferred subtask writer).

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

`rooms.model` is a canonical id, not an alias, so it cannot cross `anthropic` <-> `openai_compat`.
1. **A cross-namespace brain change clears `rooms.model` and `rooms.effort` together** and says so (`commands._clear_pin_across_namespaces`, also used by the web PATCH). Same namespace keeps it; undeterminable clears.
2. **Every writer and every model-offering surface resolves through the room's brain**: `commands.brain_for_room(config, conn, room_token, source_type)` or `web_app._brain_for_room_token`. Covers `!model` on Talk (`transport/talk/inbound.py`) and web (`chat_send_message`), `!room model`, `_room_model_allowed`, `/chat/commands` (`room_id`), and `!models`/`!help` via `commands._ctx_brain`. The composer autocomplete is unscoped; a stale pick is refused server-side.

`brain_for_room` returns a `BrainConfig` and never raises (`rooms.brain` is untyped TEXT; an exception would hit the Talk poll loop). It skips the per-user native key overlay.

### Surfaces

- `!brain`: bare reports room, lane, default and failover (ungated); `<kind>` sets (admin, allowlisted); `default` clears and is checked before the allowlist (emptying the list is the off switch). `!room` shows brain read-only: one writer.
- `PATCH /api/chat/rooms/{id}`: key-presence contract, admin gate on presence. With `model` and `brain` together: model first, then brain, then the namespace rule. See `.claude/rules/web-chat.md`.
- `doctor` uses `reachable_brain_kinds`; `runtime.native_brain` checks runnable, not buildable.
- `!steer` reads `tasks.brain`; after a mid-attempt fallback it can accept a note nothing reads (pre-existing).

## Brain fallback (availability failover)

The executor reruns the same attempt (no new row, no `attempt_count` bump) through the configured fallback.

- **Classification**: `usage_limit` via `is_usage_limit_error` on all brains (ClaudeCodeBrain before transient; native `_classify_native_error`; tmux in `_build_result` and `usage_limit_markers`, never feeding its launch breaker).
- **Portability**: `CANONICAL_ROLES` feeds every `DEFAULT_ALIASES`; a contract test requires every brain to resolve each role. `is_portable_alias(name, config_alias_portable_names(config))` decides whether a name re-resolves in the fallback namespace or is a non-crossing pin. A dropped pin adds an italic model note.
- **Breaker** (`_fallback.py`; executor path in `.claude/rules/executor.md` "Brain fallback"): `PrimaryAvailabilityBreaker`, process-global, per primary kind, separate from tmux's `_BREAKER`. `effective_fallback_kind` = configured `fallback`, None when equal to this config's `kind` (checked here because routed configs inherit `fallback`). The implicit tmux -> claude_code target was removed (ISSUE-362): it left no "off" value.

**Trigger set**: `{usage_limit, not_found, fallback}` + `transient_api_error` iff `fallback_on_transient` (default on, ISSUE-212). **Cooldown set**: `{usage_limit, not_found}` (tmux keeps being probed). **Never**: `oom`, `timeout`, `cancelled`, `error`. `_validate_brain_fallback` warns on an unknown kind and a self-fallback (the only kind the deployment runs), and logs one INFO per process for tmux with no fallback. One level only.

**The cooldown is a deadline** (ISSUE-374): for `usage_limit` on a subscription primary it ends at the quota reset, capped by `fallback_cooldown_seconds` and floored at `MIN_COOLDOWN_SECONDS`. `open_primary_breaker` is the one decision point. `subscription_usage.cached_reset_seconds` reads the disk cache only. Not for `not_found` or native. A repeat failure never moves the deadline.

### Direct-caller availability (ISSUE-181)

Sleep cycle and shared-block synthesis call the primary directly. `_fallback.py` gives them the shared breaker: `primary_brain_unavailable(brain_config) -> (available, reason)` before each call; `report_brain_result(result, brain_config)` opens on `usage_limit`/`not_found` (reason returned only on closed->open: one alert) and closes on success. Shared blocks keep last-known-good; `structured` blocks never call a brain.

The direct `BrainRequest` builders (OCR, explainer, sleep cycle, shared blocks, `code_review`, triage) build env with `executor.build_model_cli_env` (ISSUE-395); roster in `.claude/rules/executor.md`. **The tool grant decides the sandbox**: OCR passes `build_daemon_sandbox(..., extra_ro_binds=[document])` (ISSUE-397), since tools mean skip-permissions and Claude brains ignore `fs_read_roots`. Tool-less callers stay unwrapped and read the real `~/.claude/settings.json`, so settings like `advisorModel` must be neutralised structurally.

### Fallback-compatibility posture registry (ISSUE-181)

`brain/_postures.py` (`TASK_POSTURES`, `task_postures_by_name()`), one per automatic caller with call site: **skip** (sleep cycle, shared blocks, location discovery), **pin** (briefings, ISSUE-180; scheduled `prompt` jobs via `model`), **fail_clean** (health OCR, biomarker explainer). Unlisted tasks use the executor's fallback wrapper.

## NativeBrain

Over `openai_compat` only.
- **Final-turn answer (ISSUE-211)**: `result_text = final_turn_text`; empty -> `session.result._ensure_final_answer` notice. Abnormal stops (`_TRUNCATION_MARKERS`, NB-15; `_PARTIAL_ANSWER_STOP_REASONS`) use `last_assistant_text` under a marker.
- **Trace order (ISSUE-211)**: tools run before `turn_end`, so tool entries buffer in `pending_tools` and flush after the turn's text (plus a post-loop flush). `session/result.py` finality depends on it.
- **Effort**: `reasoning_effort`, gated on `supports_thinking`; `xhigh`/`max` -> `high` at the wire.
- **Caching**: `_apply_cache_breakpoints`; on by default only for `api.anthropic.com` unless `prompt_caching_explicit`.
- **Cost**: provider-reported cost wins (OpenRouter only, `_parse_reported_cost`); `cost_usd` None -> catalog, `0.0` -> a real free turn.
- **Model catalog (ISSUE-182)**: `llm.catalog.get_model_info` = `model_overrides` > fetched OpenRouter > `_DEFAULT` (200k, zero price); no bundled file. `_ensure_fetched_catalog` fetches only for `openrouter.ai`, disk-cached, never fatal, once per process per TTL.
- **Overflow recovery**: <=2 force-compacts + `run_agent_loop_continue` under the shared deadline.
- **Bash**: runs `bash -o pipefail -c` via `shell_exec.shell_argv` (ISSUE-307); 141 carries `SIGPIPE_NOTE`. CLI brains get pipefail via `SHELLOPTS=pipefail` from `build_clean_env` (ISSUE-321); not `BASH_ENV`, which names a file to source.

### Claude runtime credential (ISSUE-390, ISSUE-409)

`build_clean_env` puts `CLAUDE_CODE_OAUTH_TOKEN` in every task env and no manifest declares it. `claude_runtime_env.CLAUDE_RUNTIME_ENV_VARS` + `without_claude_runtime_env` strip it at **three seams**: `_hello_payload`, `_start_tool_server` (else readable via `/proc/<pid>/environ`), and `proxy_base_env`. `executor.skill_model_credentials` copies it back for `SKILL_MODEL_CALLERS` (`code_review` spawns `claude`); `_PROXY_LOOKUP_BLOCKED` keeps it out of lookups.
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

### The soft deadline (ISSUE-373)

`_soft_deadline_stop` at `soft_deadline_percent` (90) of the task timeout returns `soft_timeout` (in `_PARTIAL_ANSWER_STOP_REASONS`), salvaging work the hard clock would discard.
- Only on a turn that called tools, and only with `allowed_tools` (else it mislabels a finished run, and text-only callers parse JSON).
- No text -> the `timeout` shape (`success=False`), so retry applies.
- **A cancel outranks every stop condition**: all decline while `abort.is_set()`, or a cancel during tool execution becomes a success.
- `_PARTIAL_ANSWER_STOP_REASONS` derives from `_PARTIAL_ANSWER_MARKERS` (subscripted unguarded). Hard deadline stays; 0 or >=100 disables.

### Partial work on a discarding stop (ISSUE-372)

`partial_text` = last text-bearing turn on `timeout`/`cancelled`. Separate field because the scheduler matches `"Cancelled by user"` by exact equality. Both brains fill it; ClaudeCodeBrain cancel/timeout returns keep `actions_taken` and trace (ISSUE-183).

### The tool server (native-only)

One `python -m istota.tool_server` per attempt via `build_bwrap_cmd(..., profile=NATIVE)`, in the task cgroup, pid via `on_pid`; six proxy tools (`session/tools/remote.py`); `WebFetch` stays in the daemon. Replaced a Python path policy and a per-call Bash namespace carrying `.credentials.json` (ISSUE-389).
- Transport: inherited socketpair (`pass_fds`), nothing nameable; `close_fds` keeps it from Bash. Protocol `tool_server_protocol.py`.
- A dead server, `fatal` or bad frame fails the attempt naming the tool server, checked before `timed_out`/`aborted` (else "Cancelled by user").
- No enable/disable flag (two paths forever). Without bwrap it runs unwrapped with `ToolEnv` as before. Text-only spawns nothing.

### Native WebFetch tool (daemon-side, SSRF-hardened)

`session/tools/web_fetch.py`, daemon netns, available to every user unless `admin_only` (ISSUE-449), which has no `WebFetchPolicy` counterpart. The egress fields (`allow_hosts`, `block_hosts`, `extra_blocked_cidrs`, `allowed_ports`, `allow_http`) bind every caller alike, which identity never did. `require_url_provenance` was rejected as a non-admin default (WebSearch-then-read fails it). **The prompt's withheld predicate asks the routing question and `build_allowed_tools` does not**: CLI-brain tasks keep the CLI's own `WebFetch`.
- Own `httpx.AsyncClient`, `trust_env=False`, no cookies, GET/text only.
- `_ip_is_public` on every resolved IP of every hop, fail closed; connection pinned to the validated IP (Host + SNI); manual redirects; no https->http unless `allow_http`.
- Caps on bytes, chars, redirects, time; honours abort.
- Output framed `[UNTRUSTED WEB CONTENT …]` with a bounded `Fetched:` header (see `untrusted.py`, `.claude/rules/leaf-modules.md`). The executor folds `untrusted_input` into eager skills when native WebFetch is on and not withheld (`_native_web_fetch_enabled`).
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
- **The boundary is that nothing binds it; the DB mask is defence in depth**: Ansible masked; Docker only with both container settings (shipped compose has neither); standalone `_mask_dir` refuses. "Nothing binds it" is a default: a non-empty `sandbox_ro_paths`, or on standalone a `user_resources` row `resource_path = "logs"`, would expose it. `tests/test_sandbox.py::TestSessionLogContainment` pins the defaults. Same residual as the task control tree.
- **Doctor** `runtime.session_log_dir` asks `executor.mask_shadowed_by` and `effective_sandboxing` (ISSUE-381), reporting both. Under `probe=False` it uses `effective_sandboxing_if_known`; unknown is `_MASK_UNKNOWN`, never OK.
- **Operator `dir` is trusted** (like `sandbox_cache_dir`); `/`, `.`, `..` and null bytes refused; the sweep unlinks `*.jsonl` at any depth under it.
- **Retention** (step 7b): `retention_days` or `max_total_gb`, gated on `enabled`, so **`enabled = false` stops the sweep too** (open question). Evict largest user first, then oldest; never inside `LIVE_WINDOW_SECONDS`.
- **Reading** via `session/session_log_read.py` (one parser for `istota session …` and `istota-skill tasks transcript`; see `.claude/rules/skills.md`).
- No Docker env knobs or Ansible block, deliberately; adding one needs `render-config.sh` and compose both.

## TmuxClaudeBrain (`brain/tmux_claude.py`)

Interactive `claude` TUI in detached tmux: same binary and OAuth token, on subscription limits rather than metered credit. Resolution delegates to a composed `ClaudeCodeBrain`. `kind = "tmux_claude"` switches the whole instance.

Per attempt a workdir under `ISTOTA_DEFERRED_DIR` holds a session `CLAUDE_CONFIG_DIR` whose hooks write sentinels. bwrap wraps `claude`, never tmux. Without a Stop payload message, `parse_transcript` takes the last `end_turn` turn, else the last text turn **without tool calls** (ISSUE-211).

Hardening (`Specs/Done/claude-tmux-production-readiness.md`):
- Per-session hooks (§2) so concurrent tasks cannot cross-fire. Completion (§3): sentinel, cancel, `error_markers`, dead pane, else timeout.
- Transient retry (§3): fresh session, `API_RETRY_MAX_ATTEMPTS` (3), `API_RETRY_DELAY_SECONDS` (5), not counted. `_build_result` uses `_success_frame_stop_reason` and pane errors return `transient_api_error` when retryable (ISSUE-212).
- Launch failures return `fallback`/`not_found` (§4). `_CircuitBreaker` opens after `fallback_trip_threshold`, short-circuits for `fallback_cooldown_seconds`, arms one alert via `consume_circuit_open_alert()`; per process, reset by success. It, not the availability breaker, governs tmux skipping.
- Streaming (§10): `_TranscriptTailer` forwards blocks during the turn; the Stop parse stays authoritative (`forward_progress=tailer is None`). Token-level streaming unbuilt.
- `_inject_prompt` confirms submission via `_turn_started` and resends Enter only if needed, up to `_SUBMIT_MAX_ATTEMPTS`.

**Known gaps**: hook discovery under bwrap is assumed (fallback: bwrap `--chdir`). `_TMUX_UNSUPPORTED_FLAGS` is empty; `--append-system-prompt-file` must never go in it, and a TUI rejection of it is a release blocker. Hook reliability, partial-flush and network isolation are validated only on the prod host.

## Adding a new brain
Implement the protocol, add the kind to `make_brain()`, extend `BrainConfig`, update `executor._build_network_allowlist()`, report `on_pid` (else skill-proxy calls are refused), and test success, retry, cancel, timeout, oom and malformed output.

## Task Event Streaming

`EventWriter` (`events.py`) persists `TaskEvent`s to `task_events` (the table is the bus) and notifies in-process subscribers. Kinds: `task_started`, `tool_start`, `tool_end`, `tool_progress`, `progress_text`, `text_delta`, `context_management`, `brain_fallback`, `confirmation`, `result`, `error`, `cancelled`, `done`.
- **`text_delta`**: stream surfaces only. **Narration gate**: nothing streams until `scheduler.stream_text_gate_chars` (280) is crossed without a tool call; at a tool boundary (`settle_at_tool_boundary`) a short lead-in is dropped and a crossed block flushed whole.
- **`brain_fallback`** (ISSUE-278): emitted before the fallback runs; payload `primary`, `reason`, `fallback`, `model` (empty iff `dropped_pin`), `dropped_pin`, `text` (`executor.fallback_notice_text`). A stream boundary. Live-only; the durable record is `_append_model_note`.
- Retries keep the log; a "retrying" `progress_text` is emitted and `seq` resumes from `db.get_max_task_event_seq`. `web_app._synthetic_terminal_events` synthesizes a terminal frame for a terminal task with no deliverable `done`. Rows deleted only in `cleanup_old_tasks` (`ON DELETE CASCADE` is decorative).
