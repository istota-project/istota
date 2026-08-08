# Brain

The Brain layer (`src/istota/brain/`) is the single seam between executor orchestration and model invocation. The executor builds a fully composed prompt + env + sandbox configuration and hands a `BrainRequest` to a `Brain` implementation. Brains own the call to the model, stream parsing, and transient-API retry. Everything else — memory, skills, context, sandboxing, deferred DB writes, malformed-output detection, and result composition — stays in the executor.

Three brains ship behind the same protocol: `ClaudeCodeBrain` (the default, a headless `claude -p` subprocess wrapper), `NativeBrain` (Istota's own in-process agent loop against any OpenAI-compatible model), and `TmuxClaudeBrain` (drives the interactive `claude` TUI in a detached tmux session, keeping traffic on subscription billing). The executor doesn't change when you swap between them. `make_brain` selects on `config.brain.kind` (`KNOWN_BRAIN_KINDS = {"claude_code", "native", "tmux_claude"}`); an unknown kind raises `ValueError` at startup.

## Layout

```
brain/
├── __init__.py     # Brain Protocol re-exports + make_brain factory
├── _types.py       # BrainRequest, BrainResult, BrainConfig, Brain Protocol
├── _events.py      # StreamEvent types + Claude Code stream-json parser
├── _aliases.py     # CANONICAL_ROLES, EFFORT_LEVELS, split_effort, is_portable_alias
├── _roles.py       # Global operator alias-override state (provider-agnostic)
├── claude_code.py  # ClaudeCodeBrain — wraps the `claude` CLI subprocess +
│                   # owns the Anthropic model namespace (canonical IDs,
│                   # DEFAULT_ALIASES, resolver methods)
├── native.py       # NativeBrain — drives Istota's in-process agent loop
└── tmux_claude.py  # TmuxClaudeBrain — drives the interactive `claude` TUI in a
                    # detached tmux session; delegates model resolution to a
                    # composed ClaudeCodeBrain, only implements execute()
```

The native loop's machinery lives in sibling packages: `llm/` (the provider abstraction — `openai_compat` is the only provider), `agent/` (the loop and tool dispatch), and `session/` (turn state, compaction, retry).

`stream_parser.py` at the package root is now a thin re-export shim of `brain/_events.py`, kept for backward compatibility with tests and a few internal callers.

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

`supports_steering` gates the `!steer` control channel: the brain must be able to take an additional user turn at a loop boundary mid-run. Only NativeBrain is wired for it today — ClaudeCodeBrain's stdin is closed once the prompt is sent, and TmuxClaudeBrain declares support but is held out of the command layer's allowlist. The executor supplies a `poll_steers` callback only to steering-capable brains; the scheduler drops undrained steers at task finalization.

## Model identity

Every model ID in the codebase resolves through the active brain. Effort is an orthogonal **`:effort` modifier** on any reference — `split_effort(raw)` (in `brain/_aliases.py`) peels a trailing `:<effort>` ∈ `low|medium|high|xhigh|max` (`opus:high`, `smart:low`, `claude-opus-5:xhigh`); every brain's resolver applies it first. Then two layers, top to bottom:

1. **Operator alias overrides** (`brain/_roles.py`, global) — **per-namespace**. An override is stored `name -> namespace -> RoleTarget(model, effort)`; each brain reads its own `model_namespace` (`"anthropic"` / `"openai_compat"`, or the reserved `"*"` for a legacy flat value) via `get_alias_override_target(name, namespace)`, so a value written for one namespace never leaks onto another brain's wire. Operators write either a flat `[models.aliases] smart = "opus:high"` or a per-namespace `[models.aliases.smart]` table (`anthropic = "opus:high"`, `openai_compat = { model = "...", effort = "high" }`), with an optional reserved `portable = true` sibling; `set_alias_overrides(...)` normalizes both once at config-load.
2. **Shipped defaults** — the unified `DEFAULT_ALIASES` (per-brain, e.g. `claude_code.DEFAULT_ALIASES`): one table holding both the portable tiers (`fast`/`general`/`smart`) and the provider shortcuts (`opus`/`sonnet`/`haiku`/`default`), base names only. It is the code floor the operator's `[models.aliases]` overlays. A canonical `claude-*` id not in the table passes through.

`Brain.validate_alias_override(name, target)` warns on typos and shortcut-name collisions at config-load time. ClaudeCodeBrain pins to versioned IDs, base names only: `OPUS = "claude-opus-5"` (current default Opus), `SONNET = "claude-sonnet-5"`, `HAIKU = "claude-haiku-4-5"`. A prior-version pin is the canonical id + modifier (`claude-opus-4-7:high`, via the `claude-*` passthrough). Bare shortcuts (`opus`, `sonnet`, `haiku`) always resolve to the current-latest constant, so bumping `OPUS` ripples through every consumer automatically. The old `[models.roles]` key is a hard rename to `[models.aliases]` (a stale one logs a migration warning); the old effort-in-name forms (`opus-high`, `opus-46`) no longer resolve.

## BrainRequest

The dataclass the executor populates per task. The brain treats it as immutable input.

| Field | Notes |
|---|---|
| `prompt` | Fully composed prompt (emissaries + persona + memory + skills + context + request) |
| `allowed_tools` | From `executor.build_allowed_tools()` — `["Read","Write","Edit","Grep","Glob","Bash","WebSearch","WebFetch"]`. For ClaudeCodeBrain / TmuxClaudeBrain the list contents no longer reach the CLI (both run with `--dangerously-skip-permissions`, not an `--allowedTools` allowlist); the names only matter to NativeBrain, which filters its in-process tool set by them. A non-empty list is also the signal that distinguishes a tool-bearing task from a text-only one (empty = no tools, no skip-permissions, e.g. the sleep cycle). |
| `cwd` | Subprocess working directory (`config.temp_dir`) |
| `env` | Per-task env (already credential-stripped if the skill proxy is enabled) |
| `timeout_seconds` | `config.scheduler.task_timeout_minutes * 60` |
| `model` | `task.model` or `config.model`; brain default if empty |
| `effort` | `task.effort` or `config.effort`; brain default if empty |
| `custom_system_prompt_path` | Override system prompt with a file (claude_code-specific) |
| `streaming` | True when the executor wants per-event progress callbacks |
| `on_progress` | Per-event callback receiving `StreamEvent`s (the brain handles filtering) |
| `cancel_check` | Polled between events; True → kill subprocess, return `cancelled` |
| `poll_steers` | Drained at loop boundaries for pending `!steer` notes, each injected as a user turn. Supplied only to brains whose `supports_steering` is True |
| `on_pid` | Called once with subprocess PID immediately after spawn |
| `sandbox_wrap` | Closure that wraps the brain's raw cmd (e.g. with bubblewrap); brain stays sandbox-agnostic |
| `result_file` | claude_code-specific fallback file path |

## BrainResult

| Field | Notes |
|---|---|
| `success` | Final success/failure |
| `result_text` | Final response text (executor reconciles against trace via `_compose_full_result`) |
| `actions_taken` | JSON-encoded list of tool-use descriptions |
| `execution_trace` | JSON-encoded `[{"type":"tool"\|"text"\|"cm_boundary", ...}]` |
| `stop_reason` | `completed` / `cancelled` / `timeout` / `oom` / `terminated` / `transient_api_error` / `usage_limit` / `error` / `not_found` / `fallback`. `usage_limit` is a subscription/quota/billing limit — a persistent "brain unavailable" condition, not a retry. `terminated` is death by a signal other than SIGKILL. `fallback` is a tmux launch-level failure. |

## ClaudeCodeBrain

Wraps the `claude` CLI subprocess. Owns:

1. **Command construction** — `claude -p - --dangerously-skip-permissions --disallowedTools Agent Workflow`, plus optional `--model`, `--effort`, `--system-prompt-file`, and (in streaming mode) `--output-format stream-json --verbose --include-partial-messages`. Tool-bearing tasks no longer pass an `--allowedTools` allowlist — the model gets its full default toolset and the security boundary is the bwrap sandbox + network proxy + clean env, not an interactive permission prompt. `Agent` + `Workflow` (the harness's multi-agent fan-out) stay denied so Istota orchestrates through its own skills. Text-only invocations (empty `allowed_tools`, e.g. the sleep cycle) emit no tool flags and no skip-permissions. The `--include-partial-messages` flag makes the CLI emit answer / reasoning text token-by-token as `stream_event` frames *before* the whole `assistant` block lands — without it the final response would arrive as one block and dump all at once on stream surfaces (web / REPL).
2. **Sandbox wrap** — calls `req.sandbox_wrap(cmd)` if provided so the executor's bwrap configuration applies.
3. **Subprocess** — `Popen` (streaming) or `subprocess.run` (simple), prompt via stdin to avoid `E2BIG` on large prompts; stderr drained on a background thread to prevent deadlock.
4. **Stream parsing** — line-by-line via `make_stream_parser()` from `_events.py`, dispatching `ResultEvent` → final result, `ToolUseEvent` / `TextEvent` → trace + on_progress, `ContextManagementEvent` → `cm_boundary` marker in trace. The `stream_event` partial frames parse into `TextDeltaEvent` / `ThinkingDeltaEvent` and go to `on_progress` only (never the trace); the trailing whole-block `TextEvent` / `ThinkingEvent` still records the trace and is deduped against the deltas executor-side (text via `_delta_seen`, thinking via `_thinking_seen`). On push surfaces (Talk) the deltas are dropped and `TextEvent` → `progress_text` stands.
5. **Cancellation** — polls `req.cancel_check()` between events; final re-check after the subprocess exits catches SIGTERM-style external kills.
6. **Timeout** — `threading.Timer` kills the process after `req.timeout_seconds`; result tagged `stop_reason="timeout"`.
7. **Signal deaths** — a negative returncode means the subprocess died on signal `-rc`, checked after the cancellation and timeout branches so `!stop` still reports as a cancellation. `-9` keeps its OOM wording and `stop_reason="oom"` (SIGKILL is the OOM killer's and systemd-oomd's signature); every other signal returns "terminated by \<NAME\> (signal N)" with `stop_reason="terminated"`, a warning, and the trace attached. Previously only `-9` was recognized and every other signal fell to the generic stream-parse catch-all, which is what made a `systemctl restart` mid-task read as an ordinary failure. `is_signal_termination(text)` is the shared marker predicate the scheduler classifies on (the executor drops `stop_reason` at its return boundary, so the scheduler reads failure *text*).
8. **API retry** — wraps single-attempt execution in a 3-attempt loop when `is_transient_api_error()` matches (every 5xx, plus 408/425/429), waiting the provider's own `Retry-After` where one was supplied (capped at 60 s) and `API_RETRY_DELAY_SECONDS` otherwise. Retries do NOT count against the task's `attempt_count`. A quota/billing 429 is classified `usage_limit` *before* the transient check, so it reroutes to the fallback brain instead of being retried.
9. **Result fallback** — prefers `ResultEvent` → result file → stderr.

`_compose_full_result()` is intentionally NOT in the brain — both brains will produce `(result_text, execution_trace)` and the executor reconciles them (CM-aware composition + terse-result recovery).

## Brain fallback (availability failover)

When the primary brain is **unavailable**, the executor reruns the *same attempt* — no new DB row, no `attempt_count` increment — through a configured fallback brain. Three cooperating pieces:

- **Classification.** Each brain maps "I am unavailable" onto a `stop_reason`. `usage_limit` (a shared `is_usage_limit_error` detector, so it works on CLI output, tmux pane text, and native error bodies) covers subscription/quota/billing exhaustion; `not_found` a missing binary; `fallback` a tmux launch failure; `transient_api_error` a provider capacity signal (429 / 5xx / 529 / network-level) that survived the primary's own in-brain retries.
- **Portable aliases.** `CANONICAL_ROLES = ("fast", "general", "smart")` is the single source of truth every brain's `DEFAULT_ALIASES` imports. A requested model that is a canonical tier — or a custom alias the operator flagged `portable = true` — re-resolves in the fallback's namespace (model *and* effort). A non-portable pin (`opus`, `claude-opus-5`) can't cross the boundary: the fallback's own default is used and the reply carries a one-line italic note naming the dropped pin.
- **Availability breaker.** A process-global, thread-safe breaker keyed by primary kind. The **trigger set** (reroute this attempt) is `{usage_limit, not_found, fallback}`, plus `transient_api_error` when `fallback_on_transient` (**on by default**). The **cooldown set** (skip the primary entirely on later tasks for `fallback_cooldown_seconds`) is `{usage_limit, not_found}` only — `fallback` is excluded so tmux keeps being probed per task and its own launch circuit breaker decides when to stop, and `transient_api_error` is excluded because it is transient by definition. `oom` / `timeout` / `cancelled` / `error` never trigger fallback; they are task outcomes.

Config: `[brain] fallback` (`""` = none; a `tmux_claude` primary still defaults to `claude_code`), `fallback_on_transient`, `fallback_cooldown_seconds`. An unknown kind or a self-fallback is neutralized at config load with one warning. There is a single fallback level — if the fallback is also unavailable for the *same* class of reason, its failure text is tagged `executor.FALLBACK_EXHAUSTED_MARKER` and `scheduler._format_error_for_user` turns it into "both my primary and backup brains are unavailable" rather than echoing a raw provider error. Otherwise the task fails or retries normally.

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
kind = "claude_code"  # "claude_code" | "native" | "tmux_claude"
fallback = "native"               # brain kind to use when the primary is unavailable ("" = none)
fallback_on_transient = true      # also reroute a persistent transient_api_error
fallback_cooldown_seconds = 900   # skip an unavailable primary this long (0 = no stickiness)

[brain.native]         # only when kind = "native" (or routed-to)
provider = "openai_compat"
model = "claude-sonnet-4-6"
base_url = "https://api.anthropic.com/v1"
# api_key via ISTOTA_BRAIN_NATIVE_API_KEY (kept out of TOML)

[brain.tmux]           # only when kind = "tmux_claude" (or routed-to)
# All fields default in code to the prototype's pinned values, so an
# absent block is behavioral parity. See config.example.toml for the
# full set (marker heuristics, circuit-breaker thresholds, CLI pin).

[brain.source_type_overrides]   # per-source-type routing (gradual rollout)
scheduled = "native"
heartbeat = "native"
```

Defaults to `"claude_code"`, so existing deployments need no changes. `source_type_overrides` maps a task's `source_type` to a brain kind, overriding `kind` for matching tasks — the gradual-rollout knob (`brain.resolve_brain_kind` resolves it per task; unknown kinds are logged and ignored). `"native"` is istota's own in-process agent loop — see the [native brain operator runbook](../configuration/native-brain.md) for enabling it, the dev tiers, and shadow compare. `"tmux_claude"` drives the interactive `claude` TUI in a detached tmux session to keep traffic on subscription billing; a launch-level failure returns `stop_reason="fallback"` so the executor reruns the task headless, and a process-global circuit breaker short-circuits to `claude_code` for a cooldown after repeated launch failures.

## Adding a new brain

1. Create `brain/<name>.py` with a class implementing `Brain.execute()`.
2. Add the kind string to `make_brain()` in `brain/__init__.py`.
3. Extend `BrainConfig` (or add a nested config dataclass) for new knobs.
4. Update `_build_network_allowlist()` in `executor.py` if the brain calls a new external host (e.g. `openrouter.ai:443`).
5. Tests: instantiate the brain, mock its transport (HTTP / subprocess), verify it produces correct `BrainResult` shapes for the standard cases (success, transient retry, cancel, timeout, oom, malformed output).

The executor doesn't need to know the new brain exists — selection is config-driven.
