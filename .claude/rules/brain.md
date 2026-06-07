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
└── claude_code.py  # ClaudeCodeBrain — wraps `claude` CLI subprocess +
                    # owns the Anthropic model namespace (canonical IDs,
                    # MODEL_ALIASES, DEFAULT_ROLE_TARGETS, resolver methods)
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
| `allowed_tools: list[str]` | From `executor.build_allowed_tools()` — ["Read","Write","Edit","Grep","Glob","Bash"]. Empty list = text-only invocation; ClaudeCodeBrain skips `--allowedTools`/`--disallowedTools` entirely (sleep-cycle path). |
| `cwd: Path` | Subprocess working dir (`config.temp_dir`) |
| `env: dict[str,str]` | Per-task env (already credential-stripped if proxy enabled) |
| `timeout_seconds: int` | `config.scheduler.task_timeout_minutes * 60` |
| `model: str` | `task.model` or `config.model`; brain default if empty |
| `effort: str` | `task.effort` or `config.effort`; brain default if empty |
| `custom_system_prompt_path: Path \| None` | Override system prompt (claude_code-specific knob) |
| `streaming: bool` | True when `on_progress` callback is supplied |
| `on_progress: Callable[[StreamEvent], None] \| None` | Per-event callback. Widened `StreamEvent` union (task-event-streaming spec): `ToolUseEvent` (carries a real `tool_call_id`) \| `TextEvent` \| `ResultEvent` \| `ContextManagementEvent` \| `ToolEndEvent` (NativeBrain only — `success` + loop-measured `duration_ms`) \| `ToolProgressEvent` (NativeBrain only). The executor's `_on_brain_event` adapter maps these to `TaskEvent`s via `EventWriter` (`istota/events.py`). A loop-based brain MUST dispatch this callback off its event loop (NativeBrain's `run_in_executor` hop) so the synchronous Talk/log subscribers' `asyncio.run` calls don't collide (ISSUE-111 generalized). NativeBrain suppresses the final turn's `TextEvent` (its text becomes the result). |
| `cancel_check: Callable[[], bool] \| None` | Polled between events; True → kill subprocess, return `cancelled` |
| `on_pid: Callable[[int], None] \| None` | Called once with subprocess PID after spawn |
| `sandbox_wrap: Callable[[list[str]], list[str]] \| None` | Wraps raw cmd (e.g. with bwrap); no-op if not provided |
| `result_file: Path \| None` | claude_code-specific fallback file path |

## BrainResult fields
| Field | Notes |
|---|---|
| `success: bool` | Final success/failure |
| `result_text: str` | Final response text |
| `actions_taken: str \| None` | JSON-encoded list of tool-use descriptions |
| `execution_trace: str \| None` | JSON-encoded `[{type:"tool"\|"text"\|"cm_boundary", ...}]` |
| `stop_reason: str` | `completed` / `cancelled` / `timeout` / `oom` / `transient_api_error` / `error` / `not_found` |

## ClaudeCodeBrain
Wraps the `claude` CLI subprocess. Owns:

1. **Command construction** — `claude -p - --allowedTools ... --disallowedTools Agent Workflow`,
   plus optional `--model`, `--effort`, `--system-prompt-file`, and
   (in streaming mode) `--output-format stream-json --verbose`.
2. **Sandbox wrap** — calls `req.sandbox_wrap(cmd)` if provided so the
   executor's bwrap configuration applies without the brain knowing about
   bubblewrap.
3. **Subprocess** — `Popen` (streaming) or `subprocess.run` (simple),
   prompt via stdin (avoids E2BIG on large prompts), stderr drained on
   a background thread to prevent deadlock.
4. **Stream parsing** — line-by-line via `make_stream_parser()` from
   `_events.py`, dispatching ResultEvent → final result, ToolUseEvent /
   TextEvent → trace + on_progress, ContextManagementEvent → `cm_boundary`
   marker in trace.
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

Both are re-exported from `executor` for `scheduler.py` and tests; canonical
home is `brain/claude_code.py`.

## Configuration
```toml
[brain]
kind = "claude_code"  # "claude_code" | "native"

[brain.native]         # only used when kind = "native" (or routed-to below)
provider = "openai_compat"
model = "claude-sonnet-4-6"
effort = ""            # default reasoning effort; capability-gated on supports_thinking
base_url = "https://api.anthropic.com/v1"
# prompt_caching       # omit to derive from base_url (on for api.anthropic.com); set true/false to force
# api_key via ISTOTA_BRAIN_NATIVE_API_KEY env override (kept out of TOML)

[brain.source_type_overrides]   # per-source-type routing (gradual rollout)
scheduled = "native"
heartbeat = "native"
```

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

`Config.brain: BrainConfig` follows the dataclass-with-defaults convention.
`source_type_overrides` maps a task's `source_type` to a brain kind, overriding
`kind` for matching tasks — the gradual-rollout knob (cron/heartbeat on native,
interactive on claude_code). `brain.resolve_brain_kind(source_type, brain_config)`
returns the routed `BrainConfig` (same object when no override applies; unknown
target kinds are logged and ignored so a routing typo never wedges a task). The
executor calls it per task: `make_brain(resolve_brain_kind(task.source_type, config.brain))`.

## Adding a new brain
1. Create `brain/<name>.py` with a class implementing `Brain.execute()`.
2. Add the kind string to `make_brain()` in `brain/__init__.py`.
3. Extend `BrainConfig` (or add a nested config dataclass) for new knobs.
4. Update `_build_network_allowlist()` in `executor.py` if the brain calls
   a new external host (e.g. `openrouter.ai:443`).
5. Tests: instantiate the brain, mock its transport (HTTP / subprocess),
   verify it produces correct `BrainResult` shapes for the standard cases
   (success, transient retry, cancel, timeout, oom, malformed output).
