# Brain Module (`src/istota/brain/`)

Pluggable model-invocation backend. The executor builds a fully composed
prompt + env + sandbox config and hands a `BrainRequest` to a `Brain`
implementation. Brains own the call to the model and stream parsing;
everything else (memory, skills, sandboxing, deferred DB writes,
result composition, malformed-output detection) stays in the executor.

## Layout
```
brain/
├── __init__.py     # Brain protocol re-exports + make_brain factory
├── _types.py       # BrainRequest, BrainResult, BrainConfig, Brain Protocol
├── _events.py      # StreamEvent types + Claude Code stream-json parser
├── _roles.py       # Global operator role-override state (provider-agnostic)
├── claude_code.py  # ClaudeCodeBrain — wraps `claude` CLI subprocess +
│                   # owns the Anthropic model namespace (canonical IDs,
│                   # MODEL_ALIASES, DEFAULT_ROLE_TARGETS, resolver methods).
│                   # Also exports build_claude_cli_flags() — the shared
│                   # model/effort/tool/system-prompt flag builder both the
│                   # headless and tmux paths use.
├── native.py       # NativeBrain — in-process agent loop (see below)
└── tmux_claude.py  # TmuxClaudeBrain — drives the interactive `claude` TUI in
                    # a detached tmux session (subscription billing). Composes
                    # ClaudeCodeBrain for model resolution; see below.
```

`stream_parser.py` at the package root is a backward-compat shim that
re-exports from `brain._events` for tests and a few internal callers.

## Brain protocol
```python
class Brain(Protocol):
    def execute(self, req: BrainRequest) -> BrainResult: ...

    # Each brain owns its own model namespace. Consumers never reach into
    # a brain module's tables — they go through make_brain(config.brain)
    # and call these methods.
    def resolve_alias(self, alias: str) -> tuple[str | None, str | None] | None: ...
    def resolve_model_name(self, name: str | None) -> str: ...
    def list_aliases(self) -> list[tuple[str, str | None, str | None]]: ...
```

## Model identity (single source of truth)

Every model ID in the codebase resolves through the active brain. There
are three layers, top to bottom:

1. **Operator role overrides** (`brain/_roles.py`, global) — provider-
   agnostic. Operators write `[models.roles] smart = "opus-46-high"` in
   TOML; `set_role_overrides(...)` is called once at config-load time.
2. **Default role targets** (per-brain, e.g. `claude_code.DEFAULT_ROLE_TARGETS`) —
   each brain decides what `fast` / `general` / `smart` mean for *that*
   brain's namespace if the operator hasn't overridden.
3. **Provider aliases** (per-brain, e.g. `claude_code.MODEL_ALIASES`) —
   short names like `opus-high` for `(model_id, effort)` pairs. Brain-
   specific (Anthropic short names won't make sense under OpenRouter).

`Brain.resolve_alias` consults all three (override > default role > provider
alias) and returns `(model_id, effort) | None`. `Brain.resolve_model_name`
collapses any name to a canonical ID; `Brain.list_aliases` exposes the
merged table for the `!models` Talk command and the `!help` listing.

ClaudeCodeBrain pins to versioned IDs:
- `OPUS = "claude-opus-4-8"` (current default Opus)
- `OPUS_47 = "claude-opus-4-7"` (prior Opus, kept for prod pinning; `opus-47` / `opus-47-high` aliases)
- `OPUS_46 = "claude-opus-4-6"` (older Opus, kept for prod pinning)
- `SONNET = "claude-sonnet-4-6"`
- `HAIKU = "claude-haiku-4-5"`

Convention: bare alias names (`opus`, `sonnet`, `haiku`) always resolve
to the *current latest* version constant. Older-version constants are
added only when there's a concrete reason to pin (production stability,
reproducibility) — not exhaustive. Bumping `OPUS = "claude-opus-5-0"`
ripples through every consumer automatically.

Adding a new brain: implement the four Brain methods (`execute`,
`resolve_alias`, `resolve_model_name`, `list_aliases`) plus your own
canonical-ID constants and alias tables in your brain module. Operator
overrides plug in for free via `_roles.py`.

## BrainRequest fields
| Field | Notes |
|---|---|
| `prompt: str` | Fully composed prompt (emissaries+persona+memory+skills+context+request) |
| `allowed_tools: list[str]` | From `executor.build_allowed_tools()`. For ClaudeCodeBrain / TmuxClaudeBrain this is now effectively a **non-empty = give the model tools** signal (they run with `--dangerously-skip-permissions`, not an allowlist); the specific names only matter to NativeBrain, which filters its in-process tool set by them. Empty list = text-only invocation: ClaudeCodeBrain emits no tool flags and no skip-permissions (sleep-cycle path). |
| `cwd: Path` | Subprocess working dir (`config.temp_dir`) |
| `env: dict[str,str]` | Per-task env (already credential-stripped if proxy enabled) |
| `timeout_seconds: int` | `config.scheduler.task_timeout_minutes * 60` |
| `model: str` | `task.model` or `config.model`; brain default if empty |
| `effort: str` | `task.effort` or `config.effort`; brain default if empty |
| `custom_system_prompt_path: Path \| None` | Override system prompt (claude_code-specific knob) |
| `streaming: bool` | True when `on_progress` callback is supplied |
| `on_progress: Callable[[StreamEvent], None] \| None` | Per-event callback. Widened `StreamEvent` union (task-event-streaming spec): `ToolUseEvent` (carries a real `tool_call_id`) \| `TextEvent` \| `TextDeltaEvent` (per-token incremental answer text — NativeBrain per provider `TextDelta`, ClaudeCodeBrain via the CLI's `--include-partial-messages` `text_delta` frames) \| `ResultEvent` \| `ContextManagementEvent` \| `ToolEndEvent` (NativeBrain only — `success` + loop-measured `duration_ms`) \| `ToolProgressEvent` (NativeBrain only) \| `ThinkingEvent` (whole reasoning block) \| `ThinkingDeltaEvent` (incremental reasoning — NativeBrain `reasoning` deltas, ClaudeCodeBrain `thinking_delta` partials). The executor's `_on_brain_event` adapter maps these to `TaskEvent`s via `EventWriter` (`istota/events.py`): `TextDeltaEvent` → coalesced `text_delta` on stream surfaces (web/repl), dropped on push surfaces; `ThinkingDeltaEvent`/`ThinkingEvent` → coalesced `thinking`, stream surfaces only. A loop-based brain MUST dispatch this callback off its event loop (NativeBrain's `run_in_executor` hop) so the synchronous Talk/log subscribers' `asyncio.run` calls don't collide (ISSUE-111 generalized). Both brains stay surface-agnostic — they emit both per-token deltas *and* whole-block `TextEvent`/`ThinkingEvent`s; the executor dedupes deltas-vs-whole-block per surface (stream: keep deltas, drop the redundant whole block; push: drop deltas, forward intermediate `TextEvent`s as `progress_text`, drop thinking). NativeBrain additionally suppresses the **final** turn's `TextEvent` (its text becomes the result). |
| `cancel_check: Callable[[], bool] \| None` | Polled between events; True → kill subprocess, return `cancelled` |
| `on_pid: Callable[[int], None] \| None` | Called once with subprocess PID after spawn |
| `sandbox_wrap: Callable[[list[str]], list[str]] \| None` | Wraps raw cmd (e.g. with bwrap); no-op if not provided |
| `fs_read_roots: list[Path] \| None` / `fs_write_roots: list[Path] \| None` | NativeBrain-only file-tool path allowlist (NB-1). Populated by the executor (`native_fs_roots`) only under effective sandboxing; other brains ignore them (bwrap already confines their tools). `None` = unconfined (dev / no bwrap). |
| `result_file: Path \| None` | claude_code-specific fallback file path |

## BrainResult fields
| Field | Notes |
|---|---|
| `success: bool` | Final success/failure |
| `result_text: str` | Final response text |
| `actions_taken: str \| None` | JSON-encoded list of tool-use descriptions |
| `execution_trace: str \| None` | JSON-encoded `[{type:"tool"\|"text"\|"cm_boundary", ...}]` |
| `stop_reason: str` | `completed` / `cancelled` / `timeout` / `oom` / `transient_api_error` / `usage_limit` / `error` / `not_found` / `fallback`. `usage_limit` = a subscription/quota/billing limit (a persistent "brain unavailable" condition the executor reroutes to the configured fallback brain — see "Brain fallback" below). |

## ClaudeCodeBrain
Wraps the `claude` CLI subprocess. Owns:

1. **Command construction** — `claude -p - --disallowedTools Agent Workflow
   --dangerously-skip-permissions`, plus optional `--model`, `--effort`,
   `--system-prompt-file`, and (in streaming mode) `--output-format stream-json
   --verbose --include-partial-messages`. The last flag makes the CLI emit
   answer / reasoning text token-by-token as `stream_event` frames *before* the
   whole `assistant` block lands — without it the final response would arrive as
   one block and dump all at once on stream surfaces. There is **no
   `--allowedTools` allowlist**: the run is non-interactive (a per-tool
   permission prompt can't be answered in `-p` mode and would auto-deny), so it
   relies on `--dangerously-skip-permissions` for the model's full default
   toolset, with the bwrap sandbox + network proxy as the security boundary (the
   same posture the tmux brain uses; `build_claude_cli_flags` is shared). `Agent`
   and `Workflow` stay explicitly denied — deny rules win even under
   skip-permissions — so Istota keeps orchestrating through its own skills, not
   Claude Code's multi-agent fan-out (whose dozens-of-subagents cost we don't
   want a task reaching for unprompted; the old allowlist implicitly excluded
   `Workflow`, so dropping it required denying `Workflow` explicitly again).
   Text-only invocations (empty `allowed_tools`, e.g. the sleep cycle)
   get neither tool flags nor skip-permissions, so they can't reach a tool. As
   root (the Docker container-as-sandbox case) `execute()` sets `IS_SANDBOX=1`
   for tool-bearing tasks, since `claude` refuses skip-permissions as root
   otherwise (`_is_root`, shared with the tmux brain).
2. **Sandbox wrap** — calls `req.sandbox_wrap(cmd)` if provided so the
   executor's bwrap configuration applies without the brain knowing about
   bubblewrap.
3. **Subprocess** — `Popen` (streaming) or `subprocess.run` (simple),
   prompt via stdin (avoids E2BIG on large prompts), stderr drained on
   a background thread to prevent deadlock.
4. **Stream parsing** — line-by-line via `make_stream_parser()` from
   `_events.py`, dispatching ResultEvent → final result, ToolUseEvent /
   TextEvent → trace + on_progress, ContextManagementEvent → `cm_boundary`
   marker in trace. The `stream_event` partial frames parse into
   `TextDeltaEvent` / `ThinkingDeltaEvent` and go to `on_progress` only (never
   the trace); the trailing whole-block `TextEvent` / `ThinkingEvent` still
   records the trace and is deduped against the deltas executor-side.
5. **Cancellation** — polls `req.cancel_check()` between events; final
   re-check after subprocess exit catches SIGTERM-style external kills.
6. **Timeout** — `threading.Timer` kills the process after
   `req.timeout_seconds`; result tagged `stop_reason="timeout"`.
7. **OOM detection** — returncode `-9` → `stop_reason="oom"`.
8. **API retry** — wraps single-attempt execution in a 3-attempt loop with
   5s fixed sleep when `is_transient_api_error()` matches (5xx/429).
   Retries do NOT count against the task's `attempt_count`.
9. **Result fallback** — prefers ResultEvent > result_file > stderr.

`_compose_full_result()` does NOT live in the brain — both brains will
produce `(result_text, execution_trace)` and the executor reconciles them.

## API error helpers
| Function | Purpose |
|---|---|
| `parse_api_error(text) -> dict \| None` | Match `API Error: (\d{3}) (\{...\})` and parse status_code/message/request_id |
| `is_transient_api_error(text) -> bool` | True if status_code in `TRANSIENT_STATUS_CODES \| {429}` |
| `is_usage_limit_error(text) -> bool` | True if the text carries a subscription/quota/billing usage-limit signal (keyword set + an "exceeded…limit" regex). Provider-agnostic (works on CLI output, tmux transcript/pane text, and native error bodies). Checked *before* the transient predicate at every call site so a quota 429 classifies as `usage_limit`, not a retry. |

All three are re-exported from `executor` for `scheduler.py` and tests; canonical
home is `brain/claude_code.py`.

## Configuration
```toml
[brain]
kind = "claude_code"  # "claude_code" | "native" | "tmux_claude"
# Availability failover (see "Brain fallback" below). "" = none.
fallback = "native"               # brain kind to fall back to when primary unavailable
fallback_on_transient = false     # also reroute a persistent transient_api_error
fallback_cooldown_seconds = 900   # skip an unavailable primary this long; 0 disables stickiness

[brain.native]         # only used when kind = "native" (or routed-to/fallen-back-to below)
provider = "openai_compat"
model = "claude-sonnet-4-6"
effort = ""            # default reasoning effort; capability-gated on supports_thinking
base_url = "https://api.anthropic.com/v1"
# prompt_caching       # omit to derive from base_url (on for api.anthropic.com); set true/false to force
# api_key via ISTOTA_BRAIN_NATIVE_API_KEY env override (kept out of TOML)

[brain.native.web_fetch]  # daemon-side WebFetch tool (native-only). Safe defaults.
enabled = true            # false omits the tool entirely
allow_http = false        # permit cleartext http:// (off = HTTPS-only, matches CONNECT-only posture)
timeout_seconds = 20.0    # total wall-clock per fetch
max_bytes = 5_000_000     # response body byte cap (streamed)
max_content_chars = 100_000  # extracted-text cap returned to the model
max_redirects = 5
allowed_ports = [80, 443]
# allow_hosts = []        # if non-empty, a suffix-match allowlist (default-open by design)
# block_hosts = []        # always-denied hosts (suffix match)
# extra_blocked_cidrs = []  # operator additions to the private/reserved IP blocklist
require_url_provenance = false  # only fetch URLs seen in the task (blocks model-fabricated URLs)

[brain.tmux]           # only used when kind = "tmux_claude". All defaulted —
                       # an empty/absent block reproduces the prototype exactly.
fallback_trip_threshold = 5       # consecutive launch failures before the circuit opens
fallback_cooldown_seconds = 300   # how long the circuit stays open
ready_timeout_seconds = 30        # REPL-ready deadline
tmux_command_timeout = 10         # per-tmux-subprocess timeout
cli_version_pin = "2.1.168"       # supported claude CLI; mismatch logs a WARNING
# ready_markers / trust_markers / theme_markers / bypass_warning_marker /
# bypass_accept_marker / error_markers / usage_limit_markers — pane-substring
# heuristics; override on a CLI reword. usage_limit_markers (checked before
# error_markers) classify a pane limit hit as stop_reason=usage_limit → fallback.

[brain.source_type_overrides]   # per-source-type routing (gradual rollout)
scheduled = "native"
heartbeat = "native"
```

## Brain fallback (availability failover)

When the primary brain is **unavailable**, the executor reruns the same attempt
(no new DB row, no `attempt_count` increment) through a configured fallback
brain. Generalizes the old hardcoded tmux→claude_code rerun; wired at the
executor level (brains have no `Config` for the operator alert; the
same-attempt rerun already lives there). Three cooperating pieces:

- **Unavailability classification.** Each brain classifies "I am unavailable"
  into a `stop_reason`. `usage_limit` (new, shared `is_usage_limit_error`
  detector) covers subscription/quota/billing exhaustion on all three brains:
  ClaudeCodeBrain wires it into both exec paths (before the transient check —
  a quota 429 is not retried); NativeBrain's `_classify_native_error` maps a
  quota/billing error body → `usage_limit`, a plain overload/rate-limit →
  `transient_api_error`; TmuxClaudeBrain detects it in the transcript/Stop-payload
  body (`_build_result`) and via `usage_limit_markers` pane match in
  `_wait_for_completion`, guarded so it never feeds the launch `_CircuitBreaker`
  or tmux's own headless fallback.

- **Portable alias layer** (`brain/_aliases.py`). `CANONICAL_ROLES =
  ("fast","general","smart")` is the single source of truth (both brains' role
  tables import it); a contract test asserts every brain resolves every canonical
  role. `is_portable_alias(name, role_overrides)` decides whether a requested
  model name is a portable *intent* (a role tier + operator `[models.roles]`
  custom roles) that re-resolves in the fallback namespace, or a non-portable
  provider pin (`opus-high`, `claude-opus-4-8`) that can't cross the boundary.

- **Availability breaker + routing** (`brain/_fallback.py`, wired in
  `executor.py`). See the trigger/cooldown sets and the executor path in
  `.claude/rules/executor.md` "Brain fallback". `PrimaryAvailabilityBreaker` is a
  process-global, thread-safe breaker keyed by primary kind — distinct from
  `tmux_claude._BREAKER` (which governs tmux launch fast-fail); the two compose.
  `effective_fallback_kind(brain_config)` encodes the tmux back-compat default
  (a `tmux_claude` primary falls back to `claude_code` unless `fallback` is set).

**Trigger set** (reroute this attempt): `{usage_limit, not_found, fallback}` +
`transient_api_error` iff `fallback_on_transient`. **Cooldown set** (open the
breaker → skip the primary on subsequent tasks for `fallback_cooldown_seconds`):
`{usage_limit, not_found}` only — `fallback` is excluded so tmux keeps being
probed per-task (its own breaker decides when to stop). **Never fallback:**
`oom` / `timeout` / `cancelled` / `error` (task-level outcomes, flow through the
normal path). Config keys: `[brain] fallback` / `fallback_on_transient` /
`fallback_cooldown_seconds`; `_validate_brain_fallback` (config load) neutralizes
an unknown kind or a self-fallback with one WARNING. Single fallback level only;
if the fallback is also unavailable the task fails/retries normally. On a dropped
non-portable pin the successful reply gets a one-line italic model note.

NativeBrain pi-parity capabilities (over `openai_compat`, the sole transport):
- **Reasoning effort.** `req.effort or native.effort` → the OpenAI-compat
  `reasoning_effort` field, gated on `get_model_info(model).supports_thinking`
  (dropped + DEBUG-logged for non-reasoning endpoints). `xhigh`/`max` fold to
  `high` at the wire (provider-side `_REASONING_EFFORT_WIRE`); the raw tier stays
  on the task row. Extended-thinking deltas (`reasoning_content` / `reasoning`)
  parse into a `ThinkingContent` block excluded from `result_text`.
- **Prompt caching.** `_apply_cache_breakpoints` marks up to 4 `cache_control`
  breakpoints — tool defs (last tool), system, first user, and a rolling
  breakpoint on the last message each turn (the cross-turn-hit win).
  `make_provider` defaults caching ON for `api.anthropic.com` and OFF elsewhere
  unless `prompt_caching_explicit` (set when the TOML key is present). Usage
  captures `cache_creation_input_tokens` → `Usage.cache_write_tokens`; a per-task
  `native cache hit_rate=…` line logs at task end.
- **Overflow recovery.** A mid-task context-length error triggers a bounded
  (≤2) force-compact + `run_agent_loop_continue`, sharing the wall-clock deadline
  via `_run_loop_once`. `_build_recovery_context` force-compacts (aggressive
  `_aggressive_cut` fallback when `find_cut_point` returns 0) and appends a
  synthetic user nudge when the tail ends on an assistant message.
- **Image tool results.** `_tool_image_followup` renders an image-bearing tool
  result as a follow-up `role:"user"` block on vision models
  (`render_tool_images` = `supports_vision`); a no-vision model gets a text note.
- **Bash `exclude_from_context`.** The Bash tool takes an optional
  `exclude_from_context` boolean: the full output still streams to the user via
  `on_update`, but the model gets a short `[output shown to user; N bytes
  omitted from context]` stub instead of the body — for noisy commands the model
  doesn't need to reason over. Failure markers (`[exit code: N]` /
  `[command aborted]` / `[command timed out …]`) are appended to the stub so a
  failure still surfaces even when the body is omitted.

NativeBrain hardening (2026-07-18 audit, NB-1…NB-24 — see the audit doc in the
project notes for the full list):
- **File-tool confinement (NB-1).** The in-process file tools run outside bwrap,
  so `ToolEnv` enforces a symlink-resolved read/write path allowlist. The
  executor computes the same user-data roots bwrap would bind
  (`executor.native_fs_roots`) and passes them via `BrainRequest.fs_read_roots`/
  `fs_write_roots`, active only when `native_fs_confinement_active(config)`
  (`sandbox_enabled` + bwrap available) — matching the claude_code boundary.
  Other brains ignore the fields (bwrap already confines their tools).
- **Model resolution (NB-3).** Built-in role aliases (`fast`/`general`/`smart`)
  resolve to `native.model` unless remapped via `[models.roles]`; provider
  aliases (`opus`/`sonnet`/`haiku`) pass through untranslated. Per-model
  capability/window overrides via `[brain.native.model_overrides]` (NB-4).
- **Wire integrity (NB-2/15).** The `openai_compat` SSE parser surfaces
  mid-stream `{"error":…}` frames and EOF-without-`[DONE]`/`finish_reason` as
  `StreamError` (not a false clean `StreamDone`); `content_filter` is preserved
  and a `max_tokens`/`content_filter` final answer gets a visible marker. OpenAI's
  own o-series/gpt-5 use `max_completion_tokens` (NB-12).
- **`stop_reason` vocabulary (NB-18).** `BrainResult.stop_reason` is normalized
  to the documented set (`completed`/`cancelled`/`timeout`/`oom`/
  `transient_api_error`/`error`/`not_found`); the loop's raw `max_turns`/
  `loop_detected` map to `completed` with an informative message (no empty
  success). The agent loop's own `agent_end.stop_reason` is unchanged.
- **Robustness.** Adjacency-based loop-pair detection (NB-5), hook-exception
  containment in both execution modes (NB-8), off-loop cancel poll (NB-9), Bash
  process-group kill + chunked reads + `try/finally` reap (NB-6/7/11), overflow-
  recovery input bounding + retrying-provider + empty-summary fail (NB-10),
  window-relative compaction sizing (NB-14), per-task httpx client close (NB-17).

### Native WebFetch tool (daemon-side, SSRF-hardened)

The native harness's only web-reaching tool is Bash, which runs sandboxed behind
`--unshare-net` + the tight CONNECT-proxy allowlist — so it can't fetch an
arbitrary page. `session/tools/web_fetch.py` (`make_web_fetch_tool`) adds a
`WebFetch` tool that runs **in the daemon process** (host netns), so it is not
gated by the CONNECT allowlist. It is `build_default_tools`-registered
(native-only) iff `env.web_fetch` is set and enabled; `NativeBrain._build_tools`
maps `[brain.native.web_fetch]` (`WebFetchConfig`) → `session.tools.WebFetchPolicy`
onto `ToolEnv.web_fetch` (`_web_fetch_policy()`), and the tool passes the
`allowed_tools` filter because `executor.build_allowed_tools` already lists
`WebFetch`. Empty `allowed_tools` (text-only, e.g. sleep cycle) still yields no
tools.

Because it runs in the daemon netns (bypassing the CONNECT boundary), its
hardening carries the whole load:
- **Credential-free**: own `httpx.AsyncClient` with `trust_env=False` (no ambient
  proxy/auth), no cookies (cleared per hop), fixed User-Agent; never sees secret
  env. GET-only, text-only.
- **SSRF-hardened** (`_ip_is_public`): every resolved destination IP is validated
  against a private/loopback/link-local/CGNAT/benchmarking/reserved/multicast
  blocklist (IPv4 + IPv6, with IPv4-mapped-IPv6 unwrapping) on the initial request
  **and every redirect hop**, failing closed if *any* resolved IP is non-public.
  The connection is **pinned to the validated IP** (custom Host header + TLS SNI
  extension) so there's no getaddrinfo→connect DNS-rebinding TOCTOU. Manual
  redirect handling (`follow_redirects=False`) re-validates each hop, and refuses
  an https→http downgrade when `allow_http` is off.
- **Capped**: streamed body cap (`max_bytes`), extracted-text cap
  (`max_content_chars`), redirect cap, total wall-clock `timeout_seconds`, honors
  the `abort` event. HTML→text via a stdlib `html.parser` extractor (no new dep);
  text/JSON/XML returned as-is; binary content returns a short `[non-text …]` note.
- **Untrusted framing**: content is wrapped in `[UNTRUSTED WEB CONTENT …]` with a
  `Fetched: <final-url> (HTTP <status>, <mime>)` provenance header. Because a core
  tool doesn't drive `companion_skills`, the executor folds `untrusted_input` into
  the **eager** skill set when a task routes to the native brain with WebFetch
  enabled (`_native_web_fetch_enabled`), so its inbound-handling guidance reaches
  the prompt.
- **Residual**: model-driven exfiltration via a GET query string is not
  eliminated (a GET is a canonical exfil channel), but it's the same bounded
  residual the `browse` skill already carries. `require_url_provenance` (default
  off) tightens it — only URLs present in the task/prior tool output may be
  fetched — for sensitive deployments; the corpus is threaded onto
  `ToolEnv.web_fetch_url_corpus` only when the knob is on.

`Config.brain: BrainConfig` follows the dataclass-with-defaults convention.
`source_type_overrides` maps a task's `source_type` to a brain kind, overriding
`kind` for matching tasks — the gradual-rollout knob (cron/heartbeat on native,
interactive on claude_code). `brain.resolve_brain_kind(source_type, brain_config)`
returns the routed `BrainConfig` (same object when no override applies; unknown
target kinds are logged and ignored so a routing typo never wedges a task). The
executor calls it per task: `make_brain(resolve_brain_kind(task.source_type, config.brain))`.

## TmuxClaudeBrain (`brain/tmux_claude.py`)

Drives the **interactive** `claude` TUI in a detached tmux session instead of the
headless `claude -p` subprocess. Same `claude` binary, same `CLAUDE_CODE_OAUTH_TOKEN`
auth — so it keeps traffic on subscription usage limits rather than the metered
Agent-SDK credit `claude -p` draws from after 2026-06-15. Model resolution is
delegated wholesale to a composed `ClaudeCodeBrain` (same Anthropic namespace);
only `execute` is genuinely new. Selected with `brain.kind = "tmux_claude"` (a
**full instance switch** — every source type, interactive chat included, routes
through it; `claude_code` stays the constructible *fallback* kind, not a parallel
route).

**Mechanism per attempt.** Per-session workdir under `ISTOTA_DEFERRED_DIR`
(`.tmux-<session>/`) holds a per-session `CLAUDE_CONFIG_DIR` (`config/`), the Stop
sentinel (`stop.json`), the early sentinel (`started.json`), and the prompt file.
`settings.json` in the config dir declares a `Stop` hook (`cat > stop.json` — its
stdin payload carries `transcript_path` + `last_assistant_message`) plus
`UserPromptSubmit`/`SessionStart` hooks (`cat > started.json` — early
transcript-path signal for streaming). `_seed_onboarding` also pre-writes a
per-session `.claude.json` (`theme`, `hasCompletedOnboarding`,
`bypassPermissionsModeAccepted`, per-project trust keys) so the fresh config dir
doesn't re-trigger first-run onboarding. A detached `tmux new-session -e K=V`
passes `req.env` + `CLAUDE_CONFIG_DIR` into the pane (the detached-session env
gotcha: the OAuth token must reach the pane); under uid 0 the brain also sets
`IS_SANDBOX=1` so the TUI accepts `--dangerously-skip-permissions` as root (the
container-as-sandbox case — left unset on a non-root deploy where the flag is
allowed without it). `claude` is launched sandbox-wrapped (`req.sandbox_wrap` —
bwrap wraps the *claude* process, never tmux, so no nesting). `_wait_ready`
scripts past the first-run theme picker, the workspace-trust dialog, and the
Bypass-Permissions warning as a version-tolerant safety net; the prompt is
buffer-pasted, submitted, and the submit is confirmed (`_turn_started`) before
`_wait_for_completion` polls; the Stop hook fires → sentinel → parse the
transcript JSONL → `BrainResult`. Result text prefers the Stop payload's
`last_assistant_message`; the trace is reconstructed from the transcript
(`parse_transcript`, settled via `_transcript_has_final_turn`). The host needs
`tmux` on `PATH` (a missing binary → `not_found` → headless fallback); the Docker
image installs it.

**Production hardening** (`Specs/Active/claude-tmux-production-readiness.md`):

- **Per-session hook isolation (§2).** Each session's hook lives in its own
  `CLAUDE_CONFIG_DIR`, not a shared project `.claude/` — so two concurrent
  same-user tasks can't clobber a shared `settings.json` and cross-fire each
  other's Stop sentinel. The whole workdir (config dir included) is `rmtree`d in
  `finally`; a one-shot best-effort cleanup removes any legacy `base_dir/.claude`
  a prior prototype left.
- **Fail-fast completion (§3).** `_wait_for_completion` is multi-signal:
  sentinel→`done`, cancel→`cancelled`, an `error_markers` pane match→`error`
  (fail fast, classified for transient retry), dead pane→`error`, else continue
  to the hard timeout with a one-shot `tmux_stall` warning at the halfway mark.
- **Transient-API retry parity (§3).** An error-marker pane is run through
  `is_transient_api_error` (reused from `claude_code`); a transient match retries
  a **fresh session** up to `API_RETRY_MAX_ATTEMPTS` (3), `API_RETRY_DELAY_SECONDS`
  (5) apart, **not** counting against the task's `attempt_count` — identical
  contract to `ClaudeCodeBrain`.
- **`stop_reason="fallback"` + circuit breaker (§4).** A launch-level failure
  (REPL never ready, markers never matched, missing tmux→`not_found`) returns
  `fallback`/`not_found`; the executor reruns that *same attempt* once through a
  `claude_code` brain (no new DB row, no attempt increment) so the instance keeps
  completing (at metered cost) instead of failing en masse. A process-global
  `_CircuitBreaker` opens after `fallback_trip_threshold` consecutive launch
  failures: `execute` short-circuits straight to `fallback` for
  `fallback_cooldown_seconds` without trying tmux, logs `circuit_open`, and arms
  one operator alert (the executor fires it via `consume_circuit_open_alert()` →
  `notifications.send_notification(purpose="alert")`, since the brain has no
  `Config`). Any tmux success resets it; per-process state, reset on daemon
  restart (also when a fixed CLI version lands). This tmux launch alert is
  **preserved** by the generalized fallback path (see "Brain fallback" above +
  `.claude/rules/executor.md`): `fallback`/`not_found` are in the general trigger
  set (so the executor reruns through the effective fallback = `claude_code`), but
  `fallback` is *not* in the availability-breaker cooldown set, so tmux keeps being
  probed per-task and its own `_CircuitBreaker` (+ this alert) still governs the
  eventual skip. A tmux `usage_limit` (see the classification bullet above) routes
  through the *configured* fallback instead and never feeds this launch breaker.
- **Live streaming recovery (§10).** On stream-eligible tasks
  (`req.streaming and req.on_progress`) a background `_TranscriptTailer` tails the
  transcript JSONL *during* the turn and forwards each new `tool_use`/`text`/
  `thinking` block to `on_progress` as it lands (dedup by tool id + block index),
  instead of only whole-turn at Stop. The Stop-time parse stays **authoritative**
  for the persisted result/trace — the tailer is progress-only, so a missed or
  double-emitted block can't corrupt the result (`_build_result(forward_progress=
  tailer is None)` avoids double emission). Tailer exceptions are caught, never
  propagated. Token-level animation (Tier 2) stays a documented stretch, gated on
  a partial-flush probe. The brain can't distinguish push (Talk) from stream
  (web/repl) surfaces — `req` carries no surface — so the tailer runs whenever
  `streaming`; push consumers coalesce the incremental events identically.
- **Observability (§7).** One structured INFO line per attempt on logger
  `istota.brain.tmux_claude`: `tmux_brain session=… outcome=… ready_ms=… wait_ms=…
  dialogs=… tools=… retries=…`. Ready/error/stall events log at WARNING/ERROR with
  a (length-capped) pane snapshot.

**Interactive-TUI launch hardening** (surfaced during the live docker rollout):

- **First-run onboarding.** The per-session `CLAUDE_CONFIG_DIR` is empty each
  task, so the interactive TUI would re-run onboarding (theme picker → trust →
  bypass) every time. `_seed_onboarding` writes a per-session `.claude.json` with
  the onboarding-skip keys so the gauntlet is skipped. `_wait_ready` still scripts
  past the theme picker (`theme_markers`, a dark option pre-selected → bare
  `Enter`) as a safety net if a CLI version renames a seeded key.
- **Root containers.** When the process runs as uid 0 (`_is_root`), the brain
  sets `IS_SANDBOX=1` in the pane env so `claude` allows
  `--dangerously-skip-permissions` (it refuses it as root otherwise). Accurate in
  a container where the container itself is the isolation boundary and bwrap is
  off. Non-root deploys leave it unset. The Docker image installs `tmux` (without
  it every task would `not_found` → fall back to headless).
- **Race-proof prompt submission.** A large prompt arrives as a bracketed paste
  the TUI collapses to a `[Pasted text]` placeholder; an `Enter` sent before the
  paste is ingested gets absorbed, leaving the prompt unsent (the turn then hangs
  to the hard timeout). `_inject_prompt` pastes, settles, sends `Enter`, then
  confirms a turn actually started (`_turn_started` — the `UserPromptSubmit` hook
  fired, or the transcript file appeared) and only resends `Enter` if it didn't,
  up to `_SUBMIT_MAX_ATTEMPTS` — never a blind resend that could append a stray
  empty `Enter`. Every tmux path (interactive tasks + background sleep-cycle / OCR
  / explainer calls) goes through this.

**`[brain.tmux]` config** (`TmuxBrainConfig`, all defaulted to the prototype's
hardcoded values, so an empty/absent block is behavioral parity):
`fallback_trip_threshold` (5), `fallback_cooldown_seconds` (300),
`ready_timeout_seconds` (30), `tmux_command_timeout` (10), `cli_version_pin`
("2.1.168" — readiness/dialog markers are pinned to a CLI version; a reword is a
config hotfix via the marker lists), `ready_markers`, `trust_markers`,
`theme_markers`, `bypass_warning_marker`, `bypass_accept_marker`, `error_markers`.

**Known gaps / live-only gates** (the spec's Stage 1/6 prod-host probes — they
can't run off-Linux/off-bwrap):
- `CLAUDE_CONFIG_DIR` hook discovery *under bwrap* is the §2 primary mechanism
  (assumed working — cwd-independent). The documented fallback if it doesn't is a
  per-session bwrap `--chdir` (a localized executor change behind the kind).
- Interactive-TUI flag support: `build_claude_cli_flags(req, unsupported=…)` drops
  any flag the TUI rejects and warns once. `_TMUX_UNSUPPORTED_FLAGS` is empty by
  default (the prototype passed `--effort`/`--system-prompt-file`); populate if a
  CLI version starts rejecting one.
- Early-path hook reliability + the partial-flush streaming ceiling, and live
  network isolation (`--unshare-net` + CONNECT bridge) — validated on the prod
  host, not in unit tests.

## Adding a new brain
1. Create `brain/<name>.py` with a class implementing `Brain.execute()`.
2. Add the kind string to `make_brain()` in `brain/__init__.py`.
3. Extend `BrainConfig` (or add a nested config dataclass) for new knobs.
4. Update `_build_network_allowlist()` in `executor.py` if the brain calls
   a new external host (e.g. `openrouter.ai:443`).
5. Tests: instantiate the brain, mock its transport (HTTP / subprocess),
   verify it produces correct `BrainResult` shapes for the standard cases
   (success, transient retry, cancel, timeout, oom, malformed output).
