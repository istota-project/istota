# Brain

The Brain layer (`src/istota/brain/`) is the single seam between executor orchestration and model invocation. The executor composes the prompt, the env and the sandbox configuration and hands a `BrainRequest` to a `Brain` implementation. Brains own the call to the model, stream parsing, and transient-API retry. Everything else — memory, skills, context, sandboxing, deferred DB writes, malformed-output detection, and result composition — stays in the executor.

The prompt reaches a brain on three channels, and they are distinct on purpose. `prompt` is the task material and becomes the model's user turn. `composed_system_prompt_path` names a file holding Istota's own standing instructions — identity, emissaries, persona, tool descriptions, rules, response guidelines, skill bodies — and is sent with system authority. `custom_system_prompt_path` is the operator's own file, with each backend's existing override semantics preserved. The split exists because a brain that compacts its own message history was compacting away the instructions that define the assistant (ISSUE-375); a system prompt is outside anything compaction reaches. A brain must not silently drop a composed path it was given: running the user half alone is that bug reintroduced. Direct text-only callers pass no composed path and are unaffected.

Two brains ship behind the same protocol: `ClaudeCodeBrain` (the default, a headless `claude -p` subprocess wrapper) and `NativeBrain` (Istota's own in-process agent loop against any OpenAI-compatible model). The executor doesn't change when you swap between them. `make_brain` selects on `config.brain.kind` (`KNOWN_BRAIN_KINDS = {"claude_code", "native"}`); an unknown kind raises `ValueError` at startup.

## Layout

```
brain/
├── __init__.py     # Brain Protocol re-exports + make_brain factory
├── _types.py       # BrainRequest, BrainResult, BrainConfig, Brain Protocol
├── _events.py      # StreamEvent types + Claude Code stream-json parser
├── _aliases.py     # CANONICAL_ROLES, EFFORT_LEVELS, split_effort, is_portable_alias
├── _roles.py       # Operator alias overrides, parsed into an immutable AliasTable
├── _resolve.py     # resolve_selection (alias, ref, bare id, pattern), the
│                   # enabled set, clamp_effort
├── _fallback.py    # Shared availability breaker: trigger/cooldown state consulted
│                   # by the fallback rerun and by the sleep cycle
├── _postures.py    # Per-role prompt postures
├── claude_code.py  # ClaudeCodeBrain — wraps the `claude` CLI subprocess;
│                   # its resolver methods delegate to _resolve for `anthropic`
├── native.py       # NativeBrain — drives Istota's in-process agent loop
├── _tool_harness.py    # The tool-server start both brains share, and the CLI's
│                       # tool harness
└── _cli_config_dir.py  # A fresh CLAUDE_CONFIG_DIR per harness run
```

The native loop's machinery lives in sibling packages: `llm/` (providers, the model catalogue and its discovery, and the OpenAI-compatible client every provider is reached through), `agent/` (the loop and tool dispatch), and `session/` (turn state, compaction, retry). How providers, the catalogue and selection fit together for an operator is in [Models and providers](../configuration/models.md).

## Brain protocol

```python
class Brain(Protocol):
    model_namespace: str        # "anthropic" | "openai_compat" — the key operator
                                # alias overrides are resolved under

    def execute(self, req: BrainRequest) -> BrainResult: ...

    @property
    def supports_steering(self) -> bool: ...   # can take a mid-run user turn (`!steer`)

    def resolve_alias(self, alias: str) -> tuple[str | None, str | None] | None: ...
    def resolve_model_name(self, name: str | None) -> str: ...
    def list_aliases(self) -> list[tuple[str, str | None, str | None]]: ...
    def validate_alias_override(self, name: str, target: str) -> list[str]: ...
```

Each brain owns its own model namespace. Consumers never reach into a brain module's tables — they go through `make_brain(config.brain)` and call these methods. `resolve_alias` returns `(model_id, effort)` or `None`; `resolve_model_name` collapses any name to a canonical ID; `list_aliases` exposes the merged table for `!models`; `validate_alias_override` returns human-readable warnings for an operator alias override at config-load time (warnings only — it never fails the load). `make_brain(config.brain)` constructs the right implementation; unknown `kind` values raise `ValueError` so misconfiguration fails loudly at startup.

`supports_steering` gates the `!steer` control channel: the brain must be able to take an additional user turn at a loop boundary mid-run. Only NativeBrain is wired for it today — ClaudeCodeBrain's stdin is closed once the prompt is sent. The executor supplies a `poll_steers` callback only to steering-capable brains; the scheduler drops undrained steers at task finalization.

## Model identity

A model is a record in one catalogue (`llm/catalog.py`, a `Catalogue` snapshot), named by a qualified reference `provider/id`: `anthropic/<id>`, `openrouter/<vendor>/<model>`, or an operator provider's own. The catalogue is layered, highest first: `[models.overrides]`, a provider's declared `models`, each provider's discovered list (`llm/discovery.py`, fetched by the scheduler and read from a cache), the shipped Anthropic file (`llm/data/anthropic.json`), and a conservative default for a model nothing knows. No Python constant, example config or document names a current model id; the shipped file is regenerated by `scripts/update-anthropic-catalog.py`, and `tests/test_model_id_docs.py` keeps a current id out of the documents.

Effort is an orthogonal **`:effort` modifier** on any selection: `split_effort(raw)` (in `brain/_aliases.py`) peels a trailing `:<effort>` in `low|medium|high|xhigh|max` (`opus:high`, `smart:low`), and only a known level, so an OpenRouter id with a colon (`:free`) stays whole.

**Resolution** is one function, `brain/_resolve.resolve_selection`, used by every surface. In order: an alias (an operator override from `[models.aliases]`, then `default`, the tiers `fast`/`general`/`smart`, and the family shortcuts `opus`/`sonnet`/`haiku`); the exact id at the brain's own provider (`anthropic` for the CLI brains, `[brain.native] provider` for native), so a stored bare id keeps running where it ran; a qualified ref whose first segment is a configured provider; a bare id across providers, refused as ambiguous with the candidates listed; a pattern, accepted only from the chat commands. The tiers name families in code (fast = haiku, general = sonnet, smart = opus), and a family is the newest current `anthropic` model of that family in the live catalogue, so a model Anthropic's own list reports reaches the tiers without an istota release. On native an unremapped tier means the room's provider's default model. The result carries the ref, the model record, the requested effort and how it was found; a refusal carries a code (`unknown`, `ambiguous`, `model_not_runnable`, `model_not_enabled`, `no_tools`, `provider_admin_only`).

Operator alias overrides are **per-provider** (`brain/_roles.py`): an override is stored `name -> key -> RoleTarget(model, effort)`, the key a provider id (`anthropic` for the CLI brains, any other for a native room on that provider), the old `"openai_compat"` namespace (read for one release as native's default provider) or the reserved `"*"` for a legacy flat value. `brain._resolve.alias_keys` says which keys a brain on a provider reads, so a value written for one provider never reaches another provider's wire. A table with `effort` and no `model` changes only the effort. `parse_alias_overrides` builds an immutable `AliasTable` without touching process state, which is how `istota apply --check` judges a proposed table.

The resolver is strict and the brains stay lenient: `ClaudeCodeBrain` still passes a Claude-shaped id the catalogue does not list (a dated id, a model newer than the file), and `NativeBrain` passes any id through, so nothing that ran before fails at the wire. Doctor's `models.catalogue` reports the strict answer for every configured model field. The old `[models.roles]` key is not read, and the effort-in-name forms (`opus-high`) no longer resolve.

**Selections are stored, then resolved per task.** A room's or a job's model is stored as the alias or the id the person picked, with the provider in a column of its own (`rooms.provider`, `scheduled_jobs.provider`; NULL is the brain's default provider). A task copies both when it is created (`record_inbound`, the scheduler's job queueing), resolves the model within that provider only and writes the id to `tasks.model`; retries and subtasks keep the provider. The executor binds the native brain to the task's provider (`NativeBrain.bound_to`), so no provider is ever read out of a model string for a task. A tier bump therefore reaches every room pinned to a tier on its next task, and a room pinned to an exact model stays on it.

**Effort is clamped where it is sent.** Each brain clamps at its own execute seam, after filling its default (`brain._resolve.clamp_effort_level` over the model's record), not in the executor: only there are the model and a defaulted effort both known, and the fallback rerun resolves them again for the other brain. The requested level is what is stored; the clamped one is what reaches the wire and `effort_used`, and an `effort` trace entry records both when they differ. A model without reasoning gets no effort; `xhigh` and `max` are offered only where the model's `effort_map` names them; an unsupported level moves up to the nearest supported one first, then down.

## BrainRequest

The dataclass the executor populates per task. The brain treats it as immutable input.

| Field | Notes |
|---|---|
| `prompt` | The user half — task material only (memory + knowledge facts + playbooks + conversation and confirmation context + request + attachments). Native compaction may summarize it. |
| `allowed_tools` | From `executor.build_allowed_tools()` — `["Read","Write","Edit","Grep","Glob","Bash","WebSearch","WebFetch"]`. Both brains offer only the granted tools: NativeBrain filters its tool set by them, and ClaudeCodeBrain's tool harness offers the granted ones from the tool server (WebSearch, which runs on Anthropic's side, stays a CLI built-in). A non-empty list is also the signal that distinguishes a tool-bearing task from a text-only one (empty = no tools, no harness, no skip-permissions, e.g. the sleep cycle). |
| `cwd` | Subprocess working directory (`config.temp_dir`) |
| `env` | Per-task env (already credential-stripped if the skill proxy is enabled). It never carries the `claude` CLI's credential: on the harness the CLI gets its own env from `HarnessSpec.cli_env`, and this one goes to the tool server |
| `timeout_seconds` | `config.scheduler.task_timeout_minutes * 60` |
| `model` | The task's pin (`tasks.model`, a resolved ref or the selection as written), or empty; the brain fills its own default |
| `effort` | The task's requested effort, or empty; the brain fills its default and clamps it to the model |
| `custom_system_prompt_path` | The operator's own system-prompt file. Optional: a configured path that no longer exists is omitted rather than failing the attempt. `ClaudeCodeBrain` passes it as `--system-prompt-file`, which replaces the CLI's default harness prompt; `NativeBrain` appends it after its built-in coding block. |
| `composed_system_prompt_path` | Istota's own composed standing instructions, written per task to `system_prompt.txt` in the task's daemon-owned control directory. Required input when set — a brain fails the attempt rather than running without it. Absolute, because the two backends resolve it in different working directories. `None` for every direct text-only caller. |
| `streaming` | True when the executor wants per-event progress callbacks |
| `on_progress` | Per-event callback receiving `StreamEvent`s (the brain handles filtering) |
| `cancel_check` | Polled between events; True → kill subprocess, return `cancelled` |
| `poll_steers` | Drained at loop boundaries for pending `!steer` notes, each injected as a user turn. Supplied only to brains whose `supports_steering` is True |
| `on_pid` | Called once with subprocess PID immediately after spawn |
| `tool_server_wrap` | Closure that wraps the tool server's command with bubblewrap (the NATIVE profile); the brain stays sandbox-agnostic. One wrap for both brains, since both run their tools in the same tool server |
| `harness` | `HarnessSpec` for a `claude_code` request with tools: the CLI's whole env, the harness root, and the WebFetch policy the harness offers. `None` on a text-only request; ClaudeCodeBrain refuses a tool grant without one |
| `result_file` | claude_code-specific fallback file path |
| `advisor` | Advisor-model config: a stronger model the cheap default can consult at moments the advisor tool picks. Anthropic-namespace brains only; skipped when the task pins its own model |
| `fs_read_roots` / `fs_write_roots` | Filesystem roots the tool server's file tools may read from and write to, on either brain. Under bwrap they are the error-message layer above the namespace; without it they are the only confinement |
| `fs_write_denied_roots` | Read-only carve-outs the write roots cannot express. Up to two entries: the task's control directory, which holds the composed standing instructions the row above names and every other per-task file the daemon writes, on every shape; plus its `.developer` directory, which holds the scripts that fetch its own credentials, on a confined one. See [the `.developer` carve-out](../deployment/security.md#the-developer-carve-out) |
| `db_path` | Handed to the brain rather than to the model: `env` no longer carries `ISTOTA_DB_PATH` |

## BrainResult

| Field | Notes |
|---|---|
| `success` | Final success/failure |
| `result_text` | Final response text (executor reconciles against trace via `_compose_full_result`) |
| `actions_taken` | JSON-encoded list of tool-use descriptions |
| `execution_trace` | JSON-encoded `[{"type":"tool"\|"text"\|"cm_boundary", ...}]` |
| `stop_reason` | `completed` / `cancelled` / `timeout` / `oom` / `terminated` / `transient_api_error` / `usage_limit` / `error` / `not_found`. `usage_limit` is a subscription/quota/billing limit — a persistent "brain unavailable" condition, not a retry. `terminated` is death by a signal other than SIGKILL. |

## ClaudeCodeBrain

Wraps the `claude` CLI subprocess. Owns:

1. **Command construction** — `claude -p - --dangerously-skip-permissions --disallowedTools Agent Workflow`, plus optional `--model`, `--effort`, `--system-prompt-file`, `--append-system-prompt-file`, and (in streaming mode) `--output-format stream-json --verbose --include-partial-messages`. A request with tools runs on the tool harness (below): the CLI also gets every built-in that acts on the machine denied, `--tools` naming only the granted built-ins that do not (WebSearch), `--mcp-config` naming the harness's one MCP server, and `--strict-mcp-config`, so the account's claude.ai connectors and every other MCP server are absent. `Agent` + `Workflow` (the CLI's multi-agent fan-out) stay denied so Istota orchestrates through its own skills. Text-only invocations (empty `allowed_tools`, e.g. the sleep cycle) emit no tool flags and no skip-permissions. The `--include-partial-messages` flag makes the CLI emit answer / reasoning text token-by-token as `stream_event` frames *before* the whole `assistant` block lands — without it the final response would arrive as one block and dump all at once on stream surfaces (web / REPL).
2. **Tool harness** — with tools granted, the CLI runs outside the sandbox and its tools run in the tool server under `req.tool_server_wrap`, the same process NativeBrain uses. The CLI starts the tool server itself as its one MCP server, through a launcher that clears the CLI's environment and reports the server's pid to the daemon, which authorizes it with the proxies only when it descends from the CLI; each harness has its own `CLAUDE_CONFIG_DIR` (an empty `settings.json`, an onboarding seed and, under "subscription", a link to the daemon's credentials file), so nothing of the daemon's own `~/.claude` applies. The brain checks the CLI's `system/init` frame lists exactly the expected tools before any call is released. The CLI's credential is in the CLI's env only; nothing the model drives can read it on a sandboxed shape. A tool grant with no `harness` is refused. Rules: `.claude/rules/brain.md`, "The tool server".
3. **Subprocess** — `Popen` (streaming) or `subprocess.run` (simple), prompt via stdin to avoid `E2BIG` on large prompts; stderr drained on a background thread to prevent deadlock. Every harness run streams, as does every `task`-origin call and code review, so a timed-out review still records its usage; the daemon's other model calls (sleep cycle, shared briefing blocks, health OCR and explainer) take the simple path.
4. **Stream parsing** — line-by-line via `make_stream_parser()` from `_events.py`, dispatching `ResultEvent` → final result, `ToolUseEvent` / `TextEvent` → trace + on_progress, `ContextManagementEvent` → `cm_boundary` marker in trace. The `stream_event` partial frames parse into `TextDeltaEvent` / `ThinkingDeltaEvent` and go to `on_progress` only (never the trace); the trailing whole-block `TextEvent` / `ThinkingEvent` still records the trace and is deduped against the deltas executor-side (text via `_delta_seen`, thinking via `_thinking_seen`). On push surfaces (Talk) the deltas are dropped and `TextEvent` → `progress_text` stands.
5. **Cancellation** — polls `req.cancel_check()` between events; final re-check after the subprocess exits catches SIGTERM-style external kills.
6. **Timeout** — `threading.Timer` kills the process after `req.timeout_seconds`; result tagged `stop_reason="timeout"`.
7. **Signal deaths** — a negative returncode means the subprocess died on signal `-rc`, checked after the cancellation and timeout branches so `!stop` still reports as a cancellation. `-9` keeps its OOM wording and `stop_reason="oom"` (SIGKILL is the OOM killer's and systemd-oomd's signature); every other signal returns "terminated by \<NAME\> (signal N)" with `stop_reason="terminated"`, a warning, and the trace attached. Previously only `-9` was recognized and every other signal fell to the generic stream-parse catch-all, which is what made a `systemctl restart` mid-task read as an ordinary failure. `is_signal_termination(text)` is the shared marker predicate the scheduler classifies on (the executor drops `stop_reason` at its return boundary, so the scheduler reads failure *text*).
8. **API retry** — wraps single-attempt execution in a 3-attempt loop when `is_transient_api_error()` matches (every 5xx, plus 408/425/429), waiting the provider's own `Retry-After` where one was supplied (capped at 60 s) and `API_RETRY_DELAY_SECONDS` otherwise. Retries do NOT count against the task's `attempt_count`. A quota/billing 429 is classified `usage_limit` *before* the transient check, so it reroutes to the fallback brain instead of being retried.
9. **Result fallback** — prefers `ResultEvent` → result file → stderr.

### Envelope shapes on the simple path

`--output-format json` emits two live shapes depending on CLI version: an array of frames terminated by a `result` frame (2.1.227), and the bare `result` object (2.1.238). A dict whose `type` is `result` is wrapped as a one-element frame list, so the existing loop stays the only implementation. The gate is on the `type` value specifically — several daemon callers ask the model for a JSON answer, and a `{`-leading stdout is not on its own evidence of an envelope.

Only the simple path is affected, which is why this was invisible for three weeks: `task` streams, and every origin that does not stream got the raw envelope back as its answer and recorded no usage row. Code review was the visible casualty, since its findings parser read the envelope as malformed and the developer skill treats that as a reason not to open the merge request.

Output that came from the CLI but carries no terminal frame now logs one warning, for both shape families rather than only the array one. The single-object shape carries no init frame and therefore no `apiKeySource`, so its `cost_basis` degrades to `unknown` rather than being inferred from config — see [cost basis](../features/usage.md#cost-basis).

`_compose_full_result()` is intentionally NOT in the brain — both brains will produce `(result_text, execution_trace)` and the executor reconciles them (CM-aware composition + terse-result recovery).

## Brain fallback (availability failover)

When the primary brain is **unavailable**, the executor reruns the *same attempt* — no new DB row, no `attempt_count` increment — through a configured fallback brain. Three cooperating pieces:

- **Classification.** Each brain maps "I am unavailable" onto a `stop_reason`. `usage_limit` (a shared `is_usage_limit_error` detector, so it works on CLI output and native error bodies) covers subscription/quota/billing exhaustion; `not_found` a missing binary; `transient_api_error` a provider capacity signal (429 / 5xx / 529 / network-level) that survived the primary's own in-brain retries.
- **Portable aliases.** `CANONICAL_ROLES = ("fast", "general", "smart")` is the single source of truth every brain's `DEFAULT_ALIASES` imports. A requested model that is a canonical tier — or a custom alias the operator flagged `portable = true` — re-resolves in the fallback's namespace (model *and* effort). A pin written as a qualified ref crosses when the fallback brain can run that provider's models (`llm.providers.brain_can_run`), so an `anthropic/...` pin survives a `claude_code` to `native` fallback when native holds an Anthropic key. Any other pin (`opus`, a bare id) can't cross the boundary: the fallback's own default is used and the reply carries a one-line italic note naming the dropped pin.
- **Availability breaker.** A process-global, thread-safe breaker keyed by primary kind. The **trigger set** (reroute this attempt) is `{usage_limit, not_found}`, plus `transient_api_error` when `fallback_on_transient` (**on by default**). The **cooldown set** (skip the primary entirely on later tasks for `fallback_cooldown_seconds`) is `{usage_limit, not_found}` only — `transient_api_error` is excluded because it is transient by definition. `oom` / `timeout` / `cancelled` / `error` never trigger fallback; they are task outcomes. The cooldown is a **deadline**, and `fallback_cooldown_seconds` is its ceiling: on a `usage_limit` from a subscription brain the window ends at the quota's own reset, read from `usage/subscription.py`'s disk cache, floored at 60 seconds and capped at the configured cooldown. Without that, a limit hit shortly before a reset kept every task on the fallback brain for the rest of the hour with the primary idle and available.

Config: `[brain] fallback` (`""` = none, for every brain kind — explicit config only, no implicit target), `fallback_on_transient`, `fallback_cooldown_seconds`. An unknown kind is neutralized at config load with one warning, as is a self-fallback — meaning the configured kind is the only one the deployment runs, so a `source_type_overrides` entry routing elsewhere keeps a value equal to `kind` and gives those routed tasks a real target. `effective_fallback_kind` carries the per-task half: it returns None where the fallback equals the resolved config's own kind. The breaker, the availability record and the operator alert fire on a persistent unavailability whether or not a fallback is configured; only the reroute itself needs one. There is a single fallback level — if the fallback is also unavailable for the *same* class of reason, its failure text is tagged `executor.FALLBACK_EXHAUSTED_MARKER` and `scheduler._format_error_for_user` turns it into "both my primary and backup brains are unavailable" rather than echoing a raw provider error. Otherwise the task fails or retries normally.

### Degraded-brain policy for automatic work

The sleep cycle and shared-block synthesis call the primary brain *directly* rather than through the executor's fallback wrapper, so for them "pause on fallback" reduces to detecting unavailability and skipping. Two config-free helpers give them the executor's signal: `primary_brain_unavailable(brain_config)` (consult before a call or batch) and `report_brain_result(result, brain_config)` (feed the outcome back; returns a reason only on the closed→open transition, so exactly one operator alert fires). The breaker is therefore a single shared signal across every brain caller — whichever path first hits the limit opens it and alerts, and the rest skip silently until the cooldown expires.

`brain/_postures.py` declares, for each scheduled or automatic brain-calling task, one of three postures — **skip** (non-essential: sleep cycle, shared-block synthesis, location discovery), **pin** (essential, must not ride the fallback: briefings, per-job pinned scheduled prompts), or **fail_clean** (visible failure beats a silent stub: health OCR, biomarker explainer). A task not listed just routes through the executor's fallback wrapper. The admin dashboard reads the same breaker (`brain_status`) to show whether the primary is degraded and which brain is actually serving.

## API error helpers

| Function | Purpose |
|---|---|
| `parse_api_error(text)` | status_code / message / request_id from `API Error: NNN {json}` **or** the bodyless `API Error: NNN <text>` the CLI also emits |
| `is_transient_api_error(text)` | True for a capacity status (`429`, `5xx`, `529`) or a network-level failure (connection reset / timeout / DNS). The network branch is gated on the `API Error` marker (or an unambiguous errno) so prose can't trip it, and an explicit status wins over the body text |
| `is_permanent_api_error(text)` | True for a request-shaped failure — `400/401/403/404/405/413/414/422`, context-length, content-filter. No retry, no fallback attempt |
| `api_error_stop_reason(text)` | The single classifier: `usage_limit` > `error` (permanent) > `transient_api_error` > `None` when the text is not a provider error at all |
| `is_api_error_banner(text)` | True iff the text *is* a bare API-error banner. `claude -p` can report a provider failure as a **success** result frame with the error as the whole answer; without this it is delivered to the user verbatim and can never reach the fallback |
| `parse_retry_after(text)` | The provider's requested wait, capped at `RETRY_AFTER_MAX_SECONDS` (60s). Honoured by both brains' retry loops in place of the fixed/exponential delay |

`parse_api_error` and `is_transient_api_error` are re-exported from `executor` for `scheduler.py` and tests; canonical home is `brain/claude_code.py`. The native brain has its own equivalents over arbitrary OpenAI-compatible bodies (`_classify_native_error`, `session.retry.classify_error`); `session.retry.extract_status_code` recovers the status the provider layer stamps in as `HTTP NNN:` so native's message-only call site classifies as precisely as a status-carrying one.

## Configuration

```toml
[brain]
kind = "claude_code"  # "claude_code" | "native"
fallback = "native"               # brain kind to use when the primary is unavailable ("" = none)
fallback_on_transient = true      # also reroute a persistent transient_api_error
fallback_cooldown_seconds = 900   # skip an unavailable primary this long (0 = no stickiness)

[brain.native]         # only when kind = "native" (or routed-to)
provider = "openrouter"           # a [providers.<id>] id; required, no default
model = "z-ai/glm-5.3-flash"      # an id that provider lists
# the key is the provider's: ISTOTA_PROVIDER_OPENROUTER_API_KEY (kept out of TOML)

[brain.source_type_overrides]   # per-source-type routing (gradual rollout)
scheduled = "native"
heartbeat = "native"
```

Defaults to `"claude_code"`, so existing deployments need no changes. `source_type_overrides` maps a task's `source_type` to a brain kind, overriding `kind` for matching tasks — the gradual-rollout knob (`brain.resolve_brain_kind` resolves it per task; unknown kinds are logged and ignored). `"native"` is istota's own in-process agent loop — see the [native brain operator runbook](../configuration/native-brain.md) for enabling it, the dev tiers, and shadow compare.

### Native brain request and context options

These came in with the pi 1.x comparison round, and their defaults were set from that round's evals (stage 8). The loop's own options are on `[brain.native]`:

- `compaction_background` (default `false`) and `compaction_background_tokens` (0 = an eighth of the window, at most 32768): start the compaction summary before the blocking threshold and swap it in at a later turn boundary. Kept off because the primary eval model got slower with it.
- `tool_failure_nudge_after` (default 0 = off): after this many failed tool calls in a row, tell the model once to change approach. Shipped off because it did not reduce failed calls.

What the endpoint is sent is the provider's, on `[providers.<id>]`; unset, each takes the provider kind's default. The same names on `[brain.native]` are read for one release as native's provider's:

- `reasoning_replay` (default `"auto"`): send structured reasoning back on later turns of the same attempt. `"on"` adds plain reasoning text, `"off"` sends none.
- `prompt_cache_key` (unset = on for an `openrouter` provider and for `api.openai.com`, off elsewhere): a random per-attempt key, so an attempt's requests reach one cache.
- `prompt_cache_ttl` (default `"5m"`, or `"1h"`): lifetime of the cache breakpoints. Kept at 5m because both eval providers cache implicitly and ignore the field.
- `strict_tool_schemas` (default `"auto"`): send tool definitions strict for models whose catalogue entry says they support structured outputs. Moved from `"off"` because the weaker eval model made fewer failed edits with it.

A refused reasoning replay or strict schema is re-sent once without the feature, which stays off for the rest of the attempt. A failed compaction summary never drops context: the proactive path keeps the messages and tries again later, and overflow recovery fails the attempt when its own summary fails. The [native brain operator runbook](../configuration/native-brain.md) has the details.

## Adding a new brain

1. Create `brain/<name>.py` with a class implementing `Brain.execute()`.
2. Add the kind string to `make_brain()` in `brain/__init__.py`.
3. Extend `BrainConfig` (or add a nested config dataclass) for new knobs.
4. Update `_build_network_allowlist()` in `executor.py` if the brain calls a new external host (e.g. `openrouter.ai:443`).
5. Tests: instantiate the brain, mock its transport (HTTP / subprocess), verify it produces correct `BrainResult` shapes for the standard cases (success, transient retry, cancel, timeout, oom, malformed output).

The executor doesn't need to know the new brain exists — selection is config-driven.
