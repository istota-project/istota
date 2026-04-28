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
└── claude_code.py  # ClaudeCodeBrain — wraps `claude` CLI subprocess
```

`stream_parser.py` at the package root is a backward-compat shim that
re-exports from `brain._events` for tests and a few internal callers.

## Brain protocol
```python
class Brain(Protocol):
    def execute(self, req: BrainRequest) -> BrainResult: ...
```

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
| `on_progress: Callable[[StreamEvent], None] \| None` | Per-event callback (ToolUse/Text); brain filters internally |
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

1. **Command construction** — `claude -p - --allowedTools ... --disallowedTools Agent`,
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
kind = "claude_code"  # only Phase 1 option
```

`Config.brain: BrainConfig` follows the existing dataclass-with-defaults
convention. Future phases add nested per-brain blocks (`[brain.openrouter]`
etc.) with their own dataclasses.

## Adding a new brain
1. Create `brain/<name>.py` with a class implementing `Brain.execute()`.
2. Add the kind string to `make_brain()` in `brain/__init__.py`.
3. Extend `BrainConfig` (or add a nested config dataclass) for new knobs.
4. Update `_build_network_allowlist()` in `executor.py` if the brain calls
   a new external host (e.g. `openrouter.ai:443`).
5. Tests: instantiate the brain, mock its transport (HTTP / subprocess),
   verify it produces correct `BrainResult` shapes for the standard cases
   (success, transient retry, cancel, timeout, oom, malformed output).
