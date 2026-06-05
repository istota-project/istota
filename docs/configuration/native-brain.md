# Native brain — operator runbook

Istota has two model-invocation backends behind one protocol:

- **`claude_code`** (default) — wraps the `claude` CLI subprocess. Battle-tested; delegates the agentic loop, tool use, and context management to Claude Code.
- **`native`** — istota's own in-process agent loop against an OpenAI-compatible (or Claude-CLI) provider. Gives istota direct control over the loop, tool execution, context compaction, and provider/model selection.

Both coexist permanently and are switchable per instance or per task. Switching does not touch executor orchestration (memory, skills, sandbox, deferred writes) — only which `Brain` implementation runs.

## Enabling the native brain

Instance-wide:

```toml
[brain]
kind = "native"

[brain.native]
provider = "openai_compat"                 # "openai_compat" | "claude_code"
model = "claude-sonnet-4-6"                # explicit id — openai_compat has no aliasing
base_url = "https://api.anthropic.com/v1"  # any OpenAI-compatible endpoint
max_turns = 100                            # hard cap on assistant turns per task
max_tokens = 16384                         # per-completion output cap
```

The API key never goes in the TOML file. Set it via the env override:

```
ISTOTA_BRAIN_NATIVE_API_KEY=sk-...
```

(loaded from the systemd `EnvironmentFile=`, direnv, or `.env`).

### Model id format

- `openai_compat` needs an **explicit** model id (e.g. `claude-sonnet-4-6`). It does not understand role aliases (`smart`) or Claude-CLI short names (`opus`).
- `claude_code` (both the default brain and `provider = "claude_code"` under native) keeps Claude Code's aliasing — `opus` resolves to the latest Opus.

## Gradual rollout: per-source-type routing

Rather than flipping the whole instance at once, route specific task types to the native brain while everything else stays on `claude_code`. This is the recommended rollout path: move low-risk background work first, keep interactive talk/email on the proven backend, watch for regressions, then widen.

```toml
[brain]
kind = "claude_code"            # default for everything not listed below

[brain.source_type_overrides]
scheduled = "native"            # cron jobs
heartbeat = "native"            # health checks
```

`source_type` values match the task's origin: `talk`, `email`, `briefing`, `scheduled`, `heartbeat`, `subtask`, `cli`, `istota_file`. A routing typo (unknown brain kind) is logged and ignored — the task falls back to the instance default rather than failing. Each routed task logs one INFO line (`brain routing: task … -> kind=native`).

## Local development

Bubblewrap is Linux-only, so on a Mac dev box run with the sandbox off. Keep a gitignored `config/config.dev.toml` (copy `config/config.dev.toml.example`):

```toml
[brain]
kind = "native"
[brain.native]
provider = "openai_compat"
model = "claude-sonnet-4-6"
base_url = "https://api.anthropic.com/v1"

[security]
sandbox_enabled = false        # bwrap is Linux-only
skill_proxy_enabled = false    # simplifies the inner loop

[users.dev]
display_name = "Dev"
```

> Sandbox correctness cannot be validated on the Mac. "Works locally" means "logic is correct," not "isolation is correct" — check isolation on a Linux box or in the Docker image.

### Dev tiers

**Standalone loop runner** (`scripts/native_repl.py`) — runs one prompt through a `NativeBrain` with no executor/scheduler/Talk/DB. Tools operate in a throwaway temp dir. Prints the streamed events, the `BrainResult`, and `TaskUsage` (so cost is visible).

```bash
# Offline, deterministic — a scripted mock provider drives the loop.
uv run python scripts/native_repl.py --provider mock \
    --script tests/native/fixtures/two_tool_turn.json "write and read a file"

# Replay a recorded SSE session through the real parser (no credits).
uv run python scripts/native_repl.py --provider replay \
    --fixture tests/native/fixtures/text_completion.jsonl --tools "" "summarize this repo"

# Live, against whatever the dev config points at (needs a key).
uv run python scripts/native_repl.py -c config/config.dev.toml --provider live "..."
```

**Recorded-SSE replay** — `ReplayProvider` feeds committed JSONL SSE fixtures through the real provider parser (CI default, offline). `RecordingProvider` regenerates fixtures from the live API (`ISTOTA_NATIVE_RECORD=1` + a real key), run rarely.

**Full CLI task path** — point the existing `istota task` CLI at the dev config:

```bash
uv run istota init -c config/config.dev.toml
uv run istota task "read README and summarize it" -u dev -x -c config/config.dev.toml
```

Zero-cost live loop: point `[brain.native]` at a local Ollama model (`base_url = "http://localhost:11434/v1"`). Quality is lower — small models loop and mis-call tools, which is itself useful for exercising the loop detector and JSON repair — but it validates the whole stack offline.

### Shadow compare

Before flipping a task type to native, run the same prompt through both brains and diff the output:

```bash
uv run python scripts/brain_shadow.py -c config/config.dev.toml \
    "read README and summarize it in one sentence"
```

It diffs result text (similarity + unified diff), tool-call sequence, and native `TaskUsage`. Exact parity is not expected — the brains manage context differently and expose different tool schemas — but outcomes should be equivalent. Large text divergence or wildly different tool sequences are the signal to investigate.

## Operational notes

- **Cost telemetry.** The native brain computes per-task token usage and cost (priced from the bundled model catalog; pinned Anthropic ids ship at price 0.0 until set). `claude_code` leaves usage opaque — the CLI doesn't surface per-call usage.
- **Cancellation / `!stop`.** Works on both brains. The native brain bridges the scheduler's cancel poll into an `asyncio.Event` threaded through the loop, tools, and retry backoff.
- **Context management.** The native brain owns compaction (runs in `prepare_next_turn`, file-operation aware across cycles). `claude_code` delegates it to Claude Code. The two are independent.
- **Sandboxing.** `claude_code` runs the whole subprocess inside bwrap. The native brain runs the loop in-process and sandboxes each tool execution per-call (the loop itself never runs user-controlled code). Validate the per-tool sandbox on Linux, not on the Mac.

## Rollback

Set `[brain] kind = "claude_code"` (or remove the `source_type_overrides` entry) and restart the scheduler. `ClaudeCodeBrain` is never removed — rollback is a one-line config change.
