# Native brain — operator runbook

Istota has three model-invocation backends behind one protocol:

- **`claude_code`** (default) — wraps the headless `claude -p` CLI subprocess. Battle-tested; delegates the agentic loop, tool use, and context management to Claude Code.
- **`native`** — istota's own in-process agent loop against an OpenAI-compatible provider. Gives istota direct control over the loop, tool execution, context compaction, and model selection.
- **`tmux_claude`** — drives the interactive `claude` TUI in a detached tmux session (keeps traffic on subscription billing), with a launch circuit breaker. Set `[brain] fallback = "claude_code"` to reroute an unavailable tmux primary. Configured under `[brain.tmux]`; see `config.example.toml` for the full block.

All coexist permanently and are switchable per instance or per task. Switching does not touch executor orchestration (memory, skills, sandbox, deferred writes) — only which `Brain` implementation runs.

This runbook covers the `native` backend.

## Enabling the native brain

Instance-wide:

```toml
[brain]
kind = "native"

[brain.native]
provider = "anthropic"                     # required: anthropic, openrouter, or your own
model = "claude-sonnet-4-6"                # explicit id at that provider
effort = ""                                # default reasoning effort (see below)
max_turns = 100                            # hard cap on assistant turns per task
max_tokens = 16384                         # per-completion output cap
```

`provider` names a provider: one endpoint and the key for it. It has no default, so a native brain with no provider is unavailable rather than quietly billing a service; doctor's `runtime.native_brain` says which field to set. `anthropic` (`https://api.anthropic.com`) and `openrouter` (`https://openrouter.ai/api/v1`) are built in and need no table. A request to a provider carries that provider's key, whichever brain sends it.

The key does not go in the TOML file. Set the provider's variable, `ISTOTA_PROVIDER_<ID>_API_KEY`:

```
ISTOTA_PROVIDER_ANTHROPIC_API_KEY=sk-...
```

On a server it is the secret file `istota_provider_anthropic_api_key` (or `istota_provider_openrouter_api_key`). Headers go the same way, as `ISTOTA_PROVIDER_<ID>_EXTRA_HEADERS`, a JSON object.

An endpoint of your own (a local server, a gateway) is a provider of your own. Its id is lowercase letters, digits and `_`. Its key stays in `config.toml`, since the stack's secret files have fixed names:

```toml
[providers.local]
kind = "openai_compat"                     # anthropic | openrouter | openai_compat
base_url = "http://192.0.2.10:8080/v1"
api_key = ""

[brain.native]
provider = "local"
model = "qwen3-32b"
```

The provider's `kind` decides what the endpoint is sent: caching breakpoints by default for `anthropic`, the reported-cost request and the attribution headers for `openrouter`, neither for `openai_compat`. Each of `prompt_caching`, `prompt_cache_key`, `prompt_cache_ttl`, `strict_tool_schemas` and `reasoning_replay` can be set on the provider's table and wins over the kind's default.

### From the old `[brain.native]` endpoint

Before providers, native's endpoint and key lived in `[brain.native]` (`base_url`, `api_key`, `extra_headers`, the wire settings, `model_catalog_*`) and in `ISTOTA_BRAIN_NATIVE_API_KEY` / `ISTOTA_BRAIN_NATIVE_EXTRA_HEADERS`. For one release these are still read, as the fields of a provider the base URL implies: `api.anthropic.com` is `anthropic` (its trailing `/v1` dropped), `openrouter.ai` is `openrouter`, anything else an own provider called `native`. A current table or variable wins over the old one. `istota doctor` and `istota apply --check` name each old source still in use and what to write instead; a re-run of `istota setup` writes the current shape and keeps the old config as `config.toml.pre-providers`. A config with neither an old `base_url` nor a `provider`, which used to mean Anthropic, is now unconfigured: set `provider = "anthropic"`.

### Model id format

- `openai_compat` needs an **explicit** model id (e.g. `claude-sonnet-4-6`). It does not understand role aliases (`smart`) or Claude-CLI short names (`opus`).
- The `claude_code` brain (default) keeps Claude Code's aliasing — `opus` resolves to the latest Opus. The native brain does not; map role names with `[models.aliases]` if you want them.

### OpenRouter

OpenRouter is a built-in provider, so it needs a model and a key:

```toml
[brain]
kind = "native"

[brain.native]
provider = "openrouter"
model = "anthropic/claude-sonnet-4"   # OpenRouter ids are slash-namespaced: <vendor>/<model>

[providers.openrouter]
prompt_caching = true                 # REQUIRED for OpenRouter — see below
```

```
ISTOTA_PROVIDER_OPENROUTER_API_KEY=sk-or-v1-...
```

Two things to get right:

- **Model id.** OpenRouter uses slash-namespaced ids (`anthropic/claude-sonnet-4`, `openai/gpt-4o`, `google/gemini-2.5-pro`). Because `openai_compat` does no aliasing (above), paste the id exactly as OpenRouter lists it on its models page — a bare `claude-sonnet-4-6` will not resolve there.
- **Prompt caching must be explicit.** The kind default turns caching on only for an `anthropic` provider. For `openrouter` it is **off**, so set `prompt_caching = true` on `[providers.openrouter]` to get `cache_control` breakpoints on caching-capable models routed through OpenRouter.

## Gradual rollout

Keep the default brain and move background work to native with `[brain.source_type_overrides]`:

```toml
[brain]
kind = "claude_code"

[brain.native]
provider = "anthropic"
model = "claude-sonnet-4-6"

[brain.source_type_overrides]
scheduled = "native"
heartbeat = "native"
```

The API key is the provider's, `ISTOTA_PROVIDER_ANTHROPIC_API_KEY`; on a server it is the secret file `istota_provider_anthropic_api_key`.

## `[brain.native.web_fetch]`

The native harness ships its own daemon-side `WebFetch` tool. It runs in the daemon's network namespace, so it is *not* gated by the sandbox CONNECT allowlist — but it is credential-free (no cookies, `trust_env=False`) and SSRF-hardened: every resolved IP is validated against a private/reserved blocklist on each request and each redirect hop, the connection is pinned to the validated IP to close DNS rebinding, and it is GET/text-only with size and time caps. Fetched content is wrapped in an untrusted-content delimiter.

| Setting | Default | Description |
|---|---|---|
| `enabled` | `true` | Master switch; false omits the tool entirely |
| `allow_http` | `false` | Permit cleartext `http://` |
| `timeout_seconds` | `20` | Total wall-clock per fetch |
| `max_bytes` | `5000000` | Response body cap (streamed) |
| `max_content_chars` | `100000` | Extracted-text cap returned to the model |
| `max_redirects` | `5` | Redirect hops before giving up |
| `require_url_provenance` | `false` | Only fetch URLs that appeared in the task prompt — for sensitive deployments. The corpus is the prompt, never a prior tool result, so this also blocks a WebSearch-then-read chain |
| `admin_only` | `false` | Withhold the tool from non-admins. The tool was admin-only until ISSUE-449; the fields in this table are what bound where a fetch may go, and they bound it the same way for every user. Set this where a deployment wants who-scoping back — it decides whether the tool is registered, so unlike everything else here it never reaches the fetch itself |
| `allow_hosts` | `[]` | If non-empty, a host allowlist (suffix match) |
| `block_hosts` | `[]` | Always-denied hosts (suffix match) |

## Availability fallback

`[brain]` carries a failover mechanism independent of which brain is primary:

| Setting | Default | Description |
|---|---|---|
| `fallback` | `""` | Brain to rerun a request on when the primary is unavailable |
| `fallback_on_transient` | `true` | Also reroute a persistent `transient_api_error` |
| `fallback_cooldown_seconds` | `900` | Skip an unavailable primary at most this long before retrying it; a usage limit against a Claude subscription instead ends at the quota's own reset (floor 60s). 0 disables |

The same availability breaker is what the nightly sleep cycle consults before deciding to run at all. Pin `istota_brain_native_model` whenever native is either primary *or* fallback — an empty model id 400s on failover, which is the worst moment to discover it.

Key handling:

- A built-in provider's key is the secret `istota_provider_<id>_api_key` (`istota_secrets` in the management role), never written to `config.toml`; vault it. Its headers are `istota_provider_<id>_extra_headers`, a JSON object.
- **Per-user keys** are declared in the user's `secrets` in `istota_plan`, with the value in `istota_plan_secrets`. The service is `provider_keys`, keyed by provider id; it is in the connected-service schema, flagged `cli_only` so it's operator-provisioned only, not exposed in the web UI. A user's key replaces the deployment's for requests to that provider in that user's tasks:

  ```yaml
  istota_plan:
    users:
      alice:
        secrets: [{service: provider_keys, keys: [openrouter]}]
  istota_plan_secrets:
    users:
      alice: {provider_keys: {openrouter: "{{ vault_alice_openrouter_key }}"}}
  ```

- The old `istota_brain_native_api_key` and `istota_brain_native_extra_headers` files are still declared and read for one release (see "From the old `[brain.native]` endpoint").

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
provider = "anthropic"
model = "claude-sonnet-4-6"

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

Zero-cost live loop: point `[brain.native]` at a local Ollama model, as a provider of your own (`kind = "openai_compat"`, `base_url = "http://localhost:11434/v1"`). Quality is lower — small models loop and mis-call tools, which is itself useful for exercising the loop detector and JSON repair — but it validates the whole stack offline.

### Shadow compare

Before flipping a task type to native, run the same prompt through both brains and diff the output:

```bash
uv run python scripts/brain_shadow.py -c config/config.dev.toml \
    "read README and summarize it in one sentence"
```

It diffs result text (similarity + unified diff), tool-call sequence, and native `TaskUsage`. Exact parity is not expected — the brains manage context differently and expose different tool schemas — but outcomes should be equivalent. Large text divergence or wildly different tool sequences are the signal to investigate.

## Operational notes

- **Cost telemetry.** Every brain attempt writes a row to `task_usage` (plus one `task_usage_models` row per model where the brain reports a split), and a greppable `brain_usage …` INFO line so a figure survives a failed database write. Cost prefers the provider's own reported figure (OpenRouter returns real charged cost); otherwise it falls back to the model catalog's per-mtok prices — which, for an OpenRouter deployment, are the live-fetched real prices, and otherwise 0.0. That distinction is recorded rather than lost: a row's `cost_basis` is `api` only when every accumulated turn reported a cost, and `estimated` when the catalog was used, because the catalog prices an unknown model at zero and a fabricated 0.0 must not be read as real spend. Native rows carry `totals_source = 'derived'` and NULL context columns — the native loop does not track per-request prompt sizes. `claude_code` is no longer the opaque one: it reports a full breakdown, a per-model split with each model's context window, and a cost figure, all captured off the stream.
- **Per-user API keys.** Beyond the provider's own key, each user can have their own key for a provider in the encrypted secrets table: `istota secret ensure -u <user> -s provider_keys -k <provider id> -v <key>`. In that user's tasks a request to that provider carries it; internal calls (the speech gate, conversation triage) keep the deployment's key. This is operator-provisioned only (the CLI): a user can hold a key for a provider the deployment has, never add an endpoint. `istota init` moves the old `native_brain`/`api_key` rows to `provider_keys/<native's provider>` once.
- **Reasoning effort.** `[brain.native] effort` (`low` / `medium` / `high` / `xhigh` / `max`, default empty) sets a default reasoning budget; per-task overrides (e.g. `!model opus:high`, `[models.aliases]`) win. It is sent as the OpenAI-compatible `reasoning_effort` field **only** when the target model is thinking-capable (`supports_thinking`, resolved from the live-fetched OpenRouter catalog or a `model_overrides` entry) — for a non-reasoning endpoint it is dropped silently so the request never 400s. `xhigh` and `max` fold to `high` on the wire (the compat field exposes no finer knob); the original tier still tracks on the task row. Extended-thinking output is parsed but excluded from the visible result.
- **Prompt caching.** `[brain.native] prompt_caching` adds `cache_control` breakpoints covering the tool definitions, the system message, the first user message, and a rolling breakpoint on the latest message each turn (up to Anthropic's 4-breakpoint cap), which is what produces cross-turn cache hits. **The default comes from the provider's kind:** on for `anthropic`, off for the others. Set it on the provider's table to force either way — a plain-OpenAI, LM Studio, Ollama, or vLLM endpoint that doesn't understand the extension needs `prompt_caching = false`. A per-task cache hit-rate line is logged at task end (`native cache hit_rate=… read=… input=…`).
- **Cache key and lifetime.** `prompt_cache_key` sends a random key per task attempt as the `prompt_cache_key` body field, so the attempt's requests reach one cache. It is derived like `prompt_caching`: unset means on for an `openrouter` provider and for an `openai_compat` provider on `api.openai.com`, and off everywhere else, `anthropic` included. `prompt_cache_ttl` is `"5m"` (default) or `"1h"`; a 1h write is priced at twice input when the provider reports no cost. The evals kept 5m because the two eval providers (DeepSeek and GLM through OpenRouter) cache implicitly and ignore the field, so the TTL changed nothing there. An endpoint that honours `cache_control`, such as Anthropic, is where `"1h"` can matter.
- **Reasoning replay.** `reasoning_replay = "auto"` (default) sends the structured `reasoning_details` a model returned back on later turns of the same attempt; `"on"` also sends plain reasoning text, `"off"` sends none. Reasoning never crosses attempts and is not carried into compaction summaries. On the default `api.anthropic.com` endpoint no `reasoning_details` come back, so the request is unchanged.
- **Strict tool schemas.** `strict_tool_schemas = "auto"` (default) sends tool definitions with `"strict": true` when the model's catalog entry has `supports_structured_outputs` (from the OpenRouter catalog, or `supports_structured_outputs = true` in `[brain.native.model_overrides]`). A model without the flag, which includes every model when the catalog is not fetched, gets today's non-strict definitions. `"on"` always sends strict, `"off"` never. The default moved from `"off"` after the evals showed fewer failed edits on the weaker model with no loss in passed cases.
- **Refused features.** If the endpoint answers 400 to replayed reasoning or a strict schema, the request is re-sent once without that feature and the feature stays off for the rest of the attempt. This is logged once at WARNING and recorded in the session log.
- **Tool-failure nudge.** `tool_failure_nudge_after` (default `0`, off) tells the model once per attempt, after that many failed tool calls in a row, to stop retrying what fails. A failure is an error result, or a Bash command whose status suffix reports a failure (a non-zero exit other than 141, a timeout, an abort). It shipped off because the evals showed as many failed calls before the first success with it on as off. `4` is the value it was built for.
- **Context-overflow recovery.** If a turn exceeds the context window mid-task, the native brain force-compacts the accumulated transcript and continues from the summary instead of failing — up to two recovery attempts, sharing the task's wall-clock deadline. If the recovery summary itself fails, the task fails rather than continuing without the dropped turns. The proactive compaction hook (`prepare_next_turn`) is the first line of defense; this is the reactive safety net beneath it.
- **Failed summaries.** A proactive summary that fails never cuts the context: the next request goes out uncompacted, and the summary is tried once more at a later turn. After two failures in one attempt proactive compaction is off for that attempt. Summary requests are counted in the task's usage and are sent without cache breakpoints.
- **Background compaction.** `compaction_background = true` (default `false`) starts the summary in the background `compaction_background_tokens` below the blocking threshold (0 derives an eighth of the window, at most 32768) and swaps it in at a later turn boundary, if the summarized turns are still in place. It stays off because the primary eval model was slower with it on; the weaker model was faster, which is worth a re-measure with more runs.
- **Model lists.** The daemon fetches each provider's model list in the background, never inside a task: a `model-catalogue` check every 15 minutes refreshes a provider whose list is older than its `discover_ttl_hours`, and at a first start the providers with no list yet are fetched together within ten seconds before any task runs. Each list is cached in `model-catalogue/<provider>.json` beside the database, and the native brain and the web process only read those files. A failed fetch keeps the previous list; a model that drops out of a provider's list stays known for seven days. The built-in `openrouter` provider is fetched only when native uses it or it has a key, an operator's own provider whenever `discover` is on, and Anthropic's own list only when the model-listing key file holds a key. `istota doctor` reports each provider's list as `models.catalogue`.
- **Image tool results.** A tool result carrying image content renders as a follow-up `role:"user"` block on vision-capable models (`supports_vision`); on a no-vision model the image is dropped with a text note so the request still validates.
- **Model ids.** `openai_compat` needs explicit ids and does not translate Anthropic aliases — `opus` is sent verbatim, not turned into the current Claude Opus id (that mapping belongs to the `claude_code` brain). Map role names per deployment with `[models.aliases]` if you want `fast`/`general`/`smart` under native.
- **Cancellation / `!stop`.** Works on both brains. The native brain bridges the scheduler's cancel poll into an `asyncio.Event` threaded through the loop, tools, and retry backoff. A failing cancel poll (e.g. transient SQLite lock) is tolerated rather than silently disabling `!stop`.
- **Task timeout.** The native loop runs under a wall-clock deadline of `scheduler.task_timeout_minutes` (30 by default). On expiry it signals abort (killing any in-flight bash subprocess at the next poll), waits a short grace, then hard-cancels, and returns `stop_reason="timeout"`. This matches `claude_code` and prevents a runaway loop from outliving the scheduler's stuck-task reclaim (which would otherwise double-execute the task). `max_turns` is a second, coarser backstop, and it does **not** take the same split: `max_turns` defaults to 100. It was 200 for a while, on the argument that the cap is a loop backstop rather than a budget, and `52a136a1` put it back — at 200 on a slow fallback brain the wall clock arrives long before the turn budget, so the run ends on the stop that discards the model's work rather than the one that delivers it (ISSUE-373). It stays at 100 until the two backstops are sized against each other. `0` disables the cap entirely and leaves the clock as the only backstop.
- **Context management.** The native brain owns compaction (runs in `prepare_next_turn`, file-operation aware across cycles). `claude_code` delegates it to Claude Code. The two are independent.
- **Sandboxing.** `claude_code` runs the whole subprocess inside bwrap. The native brain runs the loop in-process and sandboxes each tool execution per-call (the loop itself never runs user-controlled code). Validate the per-tool sandbox on Linux, not on the Mac.

## Rollback

Set `[brain] kind = "claude_code"` (or remove the `source_type_overrides` entry) and restart the scheduler. `ClaudeCodeBrain` is never removed — rollback is a one-line config change.
