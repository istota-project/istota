# Brain

The Brain layer (`src/istota/brain/`) is the single seam between executor orchestration and model invocation. The executor builds a fully composed prompt + env + sandbox configuration and hands a `BrainRequest` to a `Brain` implementation. Brains own the call to the model, stream parsing, and transient-API retry. Everything else — memory, skills, context, sandboxing, deferred DB writes, malformed-output detection, and result composition — stays in the executor.

Phase 1 ships only `ClaudeCodeBrain` (the existing subprocess wrapper). Future phases add direct-HTTP brains (OpenRouter, Anthropic-direct) without changes to the executor.

## Layout

```
brain/
├── __init__.py     # Brain Protocol re-exports + make_brain factory
├── _types.py       # BrainRequest, BrainResult, BrainConfig, Brain Protocol
├── _events.py      # StreamEvent types + Claude Code stream-json parser
└── claude_code.py  # ClaudeCodeBrain — wraps the `claude` CLI subprocess
```

`stream_parser.py` at the package root is now a thin re-export shim of `brain/_events.py`, kept for backward compatibility with tests and a few internal callers.

## Brain protocol

```python
class Brain(Protocol):
    def execute(self, req: BrainRequest) -> BrainResult: ...
```

A single method. `make_brain(config.brain)` constructs the right implementation; unknown `kind` values raise `ValueError` so misconfiguration fails loudly at startup.

## BrainRequest

The dataclass the executor populates per task. The brain treats it as immutable input.

| Field | Notes |
|---|---|
| `prompt` | Fully composed prompt (emissaries + persona + memory + skills + context + request) |
| `allowed_tools` | From `executor.build_allowed_tools()` — `["Read","Write","Edit","Grep","Glob","Bash"]` |
| `cwd` | Subprocess working directory (`config.temp_dir`) |
| `env` | Per-task env (already credential-stripped if the skill proxy is enabled) |
| `timeout_seconds` | `config.scheduler.task_timeout_minutes * 60` |
| `model` | `task.model` or `config.model`; brain default if empty |
| `effort` | `task.effort` or `config.effort`; brain default if empty |
| `custom_system_prompt_path` | Override system prompt with a file (claude_code-specific) |
| `streaming` | True when the executor wants per-event progress callbacks |
| `on_progress` | Per-event callback receiving `StreamEvent`s (the brain handles filtering) |
| `cancel_check` | Polled between events; True → kill subprocess, return `cancelled` |
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
| `stop_reason` | `completed` / `cancelled` / `timeout` / `oom` / `transient_api_error` / `error` / `not_found` |

## ClaudeCodeBrain

Wraps the `claude` CLI subprocess. Owns:

1. **Command construction** — `claude -p - --allowedTools ... --disallowedTools Agent`, plus optional `--model`, `--effort`, `--system-prompt-file`, and (in streaming mode) `--output-format stream-json --verbose`.
2. **Sandbox wrap** — calls `req.sandbox_wrap(cmd)` if provided so the executor's bwrap configuration applies.
3. **Subprocess** — `Popen` (streaming) or `subprocess.run` (simple), prompt via stdin to avoid `E2BIG` on large prompts; stderr drained on a background thread to prevent deadlock.
4. **Stream parsing** — line-by-line via `make_stream_parser()` from `_events.py`, dispatching `ResultEvent` → final result, `ToolUseEvent` / `TextEvent` → trace + on_progress, `ContextManagementEvent` → `cm_boundary` marker in trace.
5. **Cancellation** — polls `req.cancel_check()` between events; final re-check after the subprocess exits catches SIGTERM-style external kills.
6. **Timeout** — `threading.Timer` kills the process after `req.timeout_seconds`; result tagged `stop_reason="timeout"`.
7. **OOM detection** — returncode `-9` → `stop_reason="oom"`.
8. **API retry** — wraps single-attempt execution in a 3-attempt loop with 5 s fixed sleep when `is_transient_api_error()` matches (5xx / 429). Retries do NOT count against the task's `attempt_count`.
9. **Result fallback** — prefers `ResultEvent` → result file → stderr.

`_compose_full_result()` is intentionally NOT in the brain — both brains will produce `(result_text, execution_trace)` and the executor reconciles them (CM-aware composition + terse-result recovery).

## API error helpers

| Function | Purpose |
|---|---|
| `parse_api_error(text)` | Match `API Error: (\d{3}) (\{...\})` and parse status_code / message / request_id |
| `is_transient_api_error(text)` | True if `status_code in {500, 502, 503, 504, 529, 429}` |

Both are re-exported from `executor` for `scheduler.py` and tests; canonical home is `brain/claude_code.py`.

## Configuration

```toml
[brain]
kind = "claude_code"  # only Phase 1 option
```

Defaults to `"claude_code"`, so existing deployments need no changes. Future phases will add nested per-brain blocks (`[brain.openrouter]`, etc.) with their own dataclasses.

## Adding a new brain

1. Create `brain/<name>.py` with a class implementing `Brain.execute()`.
2. Add the kind string to `make_brain()` in `brain/__init__.py`.
3. Extend `BrainConfig` (or add a nested config dataclass) for new knobs.
4. Update `_build_network_allowlist()` in `executor.py` if the brain calls a new external host (e.g. `openrouter.ai:443`).
5. Tests: instantiate the brain, mock its transport (HTTP / subprocess), verify it produces correct `BrainResult` shapes for the standard cases (success, transient retry, cancel, timeout, oom, malformed output).

The executor doesn't need to know the new brain exists — selection is config-driven.
