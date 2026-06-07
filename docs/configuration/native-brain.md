# Native brain — operator runbook

Istota has two model-invocation backends behind one protocol:

- **`claude_code`** (default) — wraps the `claude` CLI subprocess. Battle-tested; delegates the agentic loop, tool use, and context management to Claude Code.
- **`native`** — istota's own in-process agent loop against an OpenAI-compatible provider. Gives istota direct control over the loop, tool execution, context compaction, and model selection.

Both coexist permanently and are switchable per instance or per task. Switching does not touch executor orchestration (memory, skills, sandbox, deferred writes) — only which `Brain` implementation runs.

## Enabling the native brain

Instance-wide:

```toml
[brain]
kind = "native"

[brain.native]
provider = "openai_compat"                 # only provider currently
model = "claude-sonnet-4-6"                # explicit id — openai_compat has no aliasing
effort = ""                                # default reasoning effort (see below)
base_url = "https://api.anthropic.com/v1"  # any OpenAI-compatible endpoint
max_turns = 100                            # hard cap on assistant turns per task
max_tokens = 16384                         # per-completion output cap
# prompt_caching                           # omit to derive from base_url (see below)
```

The API key never goes in the TOML file. Set it via the env override:

```
ISTOTA_BRAIN_NATIVE_API_KEY=sk-...
```

(loaded from the systemd `EnvironmentFile=`, direnv, or `.env`).

### Model id format

- `openai_compat` needs an **explicit** model id (e.g. `claude-sonnet-4-6`). It does not understand role aliases (`smart`) or Claude-CLI short names (`opus`).
- The `claude_code` brain (default) keeps Claude Code's aliasing — `opus` resolves to the latest Opus. The native brain does not; map role names with `[models.roles]` if you want them.

## Ansible deployment

The role renders the `[brain]` block from inventory variables. The `[brain.native]` and `[brain.source_type_overrides]` tables are only written when `istota_brain_kind` is `native` or `istota_brain_source_type_overrides` is non-empty, so existing deployments stay byte-identical until you opt in. After templating, `files/validate_config.py` parses the rendered config and gates the scheduler restart, so a malformed brain block fails the play instead of the running daemon.

Instance-wide native brain:

```yaml
istota_brain_kind: "native"
istota_brain_native_provider: "openai_compat"
istota_brain_native_model: "claude-sonnet-4-6"
istota_brain_native_base_url: "https://api.anthropic.com/v1"
istota_brain_native_effort: ""                    # default reasoning effort (thinking models only)
# istota_brain_native_prompt_caching             # "" (default) derives from base_url; set true/false to force
istota_brain_native_api_key: "{{ vault_native_api_key }}"   # → ISTOTA_BRAIN_NATIVE_API_KEY
```

Gradual rollout (keep the default brain, move background work to native):

```yaml
istota_brain_kind: "claude_code"
istota_brain_native_model: "claude-sonnet-4-6"
istota_brain_native_api_key: "{{ vault_native_api_key }}"
istota_brain_source_type_overrides:
  scheduled: native
  heartbeat: native
```

The full variable set (all defaulted to the code defaults) is documented in `deploy/ansible/defaults/main.yml`: `istota_brain_native_{provider,model,effort,base_url,extra_headers,context_window,max_turns,max_tokens,prompt_caching,api_key}` and `istota_brain_source_type_overrides`. `istota_brain_native_prompt_caching` defaults to `""` (derive from `base_url`); set it to `true`/`false` only to force.

Key handling:

- `istota_brain_native_api_key` is **never** written to `config.toml`. With `istota_use_environment_file: true` (the default) it's rendered into the systemd `EnvironmentFile` as `ISTOTA_BRAIN_NATIVE_API_KEY`; vault it.
- **Per-user keys** go through the existing `istota_user_secrets` mechanism (the `native_brain` service is in the connected-service schema, flagged `cli_only` so it's operator-provisioned only — not exposed in the web UI), and overlay the instance key for that user's tasks:

  ```yaml
  istota_user_secrets:
    alice:
      - { service: native_brain, key: api_key, value: "{{ vault_alice_native_key }}" }
  ```

- `istota_brain_native_extra_headers` is rendered as a `[brain.native.extra_headers]` sub-table (a TOML inline table would be mis-emitted by the JSON filter), so header names with dots or dashes (`anthropic-beta`) are safe.

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

- **Cost telemetry.** The native brain computes per-task token usage and cost (priced from the bundled model catalog; pinned Anthropic ids ship at price 0.0 until set) and writes it to `task_logs` (a `usage {...}` info line) plus an `native_usage` log line. `claude_code` leaves usage opaque — the CLI doesn't surface per-call usage.
- **Per-user API keys.** Beyond the instance-wide `[brain.native] api_key` / `ISTOTA_BRAIN_NATIVE_API_KEY`, each user can have their own provider key in the encrypted secrets table: `istota secret ensure -u <user> -s native_brain -k api_key -v <key>`. This is operator-provisioned only (CLI/Ansible) — it's deliberately not in the web UI, since it overrides only the key and not the provider/model/base_url, so a self-serve knob would imply more than it delivers. The per-user key overlays the instance key for that user's tasks (execution and Pass-2 routing).
- **Reasoning effort.** `[brain.native] effort` (`low` / `medium` / `high` / `xhigh` / `max`, default empty) sets a default reasoning budget; per-task overrides (e.g. `!model opus-high`, `[models.roles]`) win. It is sent as the OpenAI-compatible `reasoning_effort` field **only** when the target model is thinking-capable (`supports_thinking` in the bundled catalog) — for a non-reasoning endpoint it is dropped silently so the request never 400s. `xhigh` and `max` fold to `high` on the wire (the compat field exposes no finer knob); the original tier still tracks on the task row. Extended-thinking output is parsed but excluded from the visible result.
- **Prompt caching.** `[brain.native] prompt_caching` adds `cache_control` breakpoints covering the tool definitions, the system message, the first user message, and a rolling breakpoint on the latest message each turn (up to Anthropic's 4-breakpoint cap), which is what produces cross-turn cache hits. **The default is derived from `base_url`:** on for `api.anthropic.com`, off for any other endpoint. Set it explicitly to force either way — a plain-OpenAI, LM Studio, Ollama, or vLLM endpoint that doesn't understand the extension needs `prompt_caching = false`. A per-task cache hit-rate line is logged at task end (`native cache hit_rate=… read=… input=…`).
- **Context-overflow recovery.** If a turn exceeds the context window mid-task, the native brain force-compacts the accumulated transcript and continues from the summary instead of failing — up to two recovery attempts, sharing the task's wall-clock deadline. The proactive compaction hook (`prepare_next_turn`) is the first line of defense; this is the reactive safety net beneath it.
- **Image tool results.** A tool result carrying image content renders as a follow-up `role:"user"` block on vision-capable models (`supports_vision`); on a no-vision model the image is dropped with a text note so the request still validates.
- **Model ids.** `openai_compat` needs explicit ids and does not translate Anthropic aliases — `opus` is sent verbatim, not turned into `claude-opus-4-8` (that mapping is the `claude_code` brain's, not the native brain's). Map role names per deployment with `[models.roles]` if you want `fast`/`general`/`smart` under native.
- **Cancellation / `!stop`.** Works on both brains. The native brain bridges the scheduler's cancel poll into an `asyncio.Event` threaded through the loop, tools, and retry backoff. A failing cancel poll (e.g. transient SQLite lock) is tolerated rather than silently disabling `!stop`.
- **Task timeout.** The native loop runs under a wall-clock deadline of `scheduler.task_timeout_minutes` (`istota_scheduler_task_timeout_minutes`, default 30). On expiry it signals abort (killing any in-flight bash subprocess at the next poll), waits a short grace, then hard-cancels, and returns `stop_reason="timeout"`. This matches `claude_code` and prevents a runaway loop from outliving the scheduler's stuck-task reclaim (which would otherwise double-execute the task). `max_turns` is a second, coarser backstop.
- **Context management.** The native brain owns compaction (runs in `prepare_next_turn`, file-operation aware across cycles). `claude_code` delegates it to Claude Code. The two are independent.
- **Sandboxing.** `claude_code` runs the whole subprocess inside bwrap. The native brain runs the loop in-process and sandboxes each tool execution per-call (the loop itself never runs user-controlled code). Validate the per-tool sandbox on Linux, not on the Mac.
- **Pass-2 skill routing.** Semantic skill routing (`[skills] semantic_routing`) runs its classification through the active brain — under native it uses the configured `[brain.native]` provider and **its own model** (role aliases like `fast` are claude_code-namespace only and aren't sent to a non-Anthropic endpoint). Two knobs matter for a slow or reasoning-heavy local model: raise `[skills] semantic_routing_timeout` (default 3 s is tight for a local model), and note that reasoning models emit `reasoning_content` before any answer — the classifier already gives them generous output headroom, but an empty result logs `pass2_no_result` and falls back to Pass-1-only selection. To skip the cost entirely, set `semantic_routing = false`.

## Rollback

Set `[brain] kind = "claude_code"` (or remove the `source_type_overrides` entry) and restart the scheduler. `ClaudeCodeBrain` is never removed — rollback is a one-line config change.
