---
paths:
  - "src/istota/brain/**"
  - "src/istota/agent/**"
  - "src/istota/llm/**"
  - "src/istota/session/**"
---

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
├── _aliases.py     # CANONICAL_ROLES, EFFORT_LEVELS, split_effort, is_portable_alias
├── _roles.py       # Global operator alias-override state (provider-agnostic)
├── claude_code.py  # ClaudeCodeBrain — wraps `claude` CLI subprocess +
│                   # owns the Anthropic model namespace (canonical IDs,
│                   # DEFAULT_ALIASES, resolver methods).
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
    # The namespace this brain resolves role/alias names in. Operators key a
    # per-namespace role override on it. "anthropic" (claude_code + tmux_claude,
    # by delegation) | "openai_compat" (native).
    model_namespace: str

    def execute(self, req: BrainRequest) -> BrainResult: ...

    # Each brain owns its own model namespace. Consumers never reach into
    # a brain module's tables — they go through make_brain(config.brain)
    # and call these methods.
    def resolve_alias(self, alias: str) -> tuple[str | None, str | None] | None: ...
    def resolve_model_name(self, name: str | None) -> str: ...
    def list_aliases(self) -> list[tuple[str, str | None, str | None]]: ...
```

## Model identity (single source of truth)

Every model ID in the codebase resolves through the active brain. There are two
layers plus the orthogonal `:effort` modifier.

**The `:effort` modifier** (`brain/_aliases.py`): effort is an axis orthogonal to
model choice, appended to *any* reference as `<base>:<effort>` where `<effort>` ∈
`EFFORT_LEVELS` (`low|medium|high|xhigh|max`). `split_effort(raw) -> (base,
effort|None)` peels it (via `rpartition(":")`, only when the suffix is a known
effort level and the base is non-empty; an OpenRouter `provider/model` slug's `/`
is untouched). Every brain's `resolve_alias` / `resolve_model_name` calls it
first. This replaced the hand-maintained model×effort cross-product (`opus-high`,
`opus-xhigh`, …) — those forms no longer resolve; `opus:high` is the only
spelling.

The two resolution layers, top to bottom:

1. **Operator alias overrides** (`brain/_roles.py`, global) — **per-namespace**.
   An override is stored `name -> namespace -> RoleTarget`, where
   `RoleTarget(model, effort=None)` carries an optional effort. The namespace
   key is a brain's `model_namespace` (`"anthropic"` / `"openai_compat"`) or the
   reserved `"*"` for a *legacy flat* value. Each brain resolves in its own
   namespace and a value written for one namespace can never leak onto another
   brain's wire. `set_alias_overrides(...)` (called once at config-load)
   normalizes a bare string → `{"*": RoleTarget(str)}`, `{ns: "str"}`, and
   `{ns: {model, effort}}`, and strips the reserved `portable = true` sibling key
   into a separate `_portable_names` set (`get_portable_alias_names()`).
   `get_alias_override_target(name, namespace)` precedence: per-namespace value >
   legacy `"*"` > None.
2. **Shipped defaults — the unified `DEFAULT_ALIASES`** (per-brain, e.g.
   `claude_code.DEFAULT_ALIASES`): one table mapping each base alias name →
   `(model_id, default_effort)` in that brain's namespace. Holds the portable
   tiers (`fast`/`general`/`smart`, the `CANONICAL_ROLES`) AND the provider
   shortcuts (`opus`/`sonnet`/`haiku`/`default`) together, base names only. This
   is the code floor the operator's `[models.aliases]` overlays. It replaced the
   old split `MODEL_ALIASES` + `DEFAULT_ROLE_TARGETS`.

`Brain.resolve_alias` (per brain): `split_effort` → resolve the base
(override → `DEFAULT_ALIASES` → canonical `claude-*` id passthrough → `None`) →
merge effort (the `:effort` suffix wins over the entry's own default effort). An
override target is itself resolved through the brain's `DEFAULT_ALIASES`, and an
explicit `RoleTarget.effort` wins over the target's alias-derived effort. Returns
`(model_id, effort) | None`. `Brain.resolve_model_name` collapses any name to a
canonical ID (effort stripped); `Brain.list_aliases` exposes the merged table
(tiers first, then shortcuts, then custom) for `!models` and `!help`.

**Config surface** (`[models.aliases]`) — three forms:
```toml
# Legacy flat (namespace-agnostic, stored under "*"):
[models.aliases]
smart = "opus:high"

# Per-namespace (define once, correct on every brain family):
[models.aliases.smart]
anthropic     = "opus:high"                                          # CLI brains
openai_compat = { model = "anthropic/claude-opus-4.8", effort = "high" }  # native
[models.aliases.deep]
anthropic     = "opus:max"
openai_compat = "anthropic/claude-opus-4.8"
portable      = true                                                # a cross-brain custom tier
```
An alias uses one form (TOML: a key can't be both a string and a table). A
per-namespace table missing the active brain's key falls to that brain's code
floor. `ModelsConfig.aliases` holds the **raw** parsed structure
(`dict[str, str | dict]`); normalization into `RoleTarget`s lives only in
`set_alias_overrides`. Config-load validation is namespace-aware: `anthropic`
entries validate against `claude_code` via `Brain.validate_alias_override`, a flat
`"*"` against the active brain, `openai_compat` against native (no alias table →
no warnings); the reserved `portable` key is skipped; warnings only, never fails
load. **Hard rename:** the old `[models.roles]` key is no longer read — a stale
one present logs a one-time migration WARNING (detection only).

ClaudeCodeBrain pins to versioned IDs, base names only:
- `OPUS = "claude-opus-5"` (current default Opus)
- `SONNET = "claude-sonnet-5"`
- `HAIKU = "claude-haiku-4-5"`

`OPUS_46` / `OPUS_47` and their effort-variant aliases were deleted — a
prior-version pin is the canonical id plus the modifier (`claude-opus-4-7:high`),
which resolves via the `claude-*` passthrough in `resolve_alias`.

Convention: bare alias names (`opus`, `sonnet`, `haiku`) always resolve to the
*current latest* version constant. Bumping `OPUS = "claude-opus-5-0"` ripples
through every consumer + alias automatically — a model release is one constant
edit, no effort variants to enumerate.

`Config.advisor_model` (top-level TOML `advisor_model`, `[brain.advisor_model]`
does NOT exist — it lives beside `model`/`effort`, not under `[brain]`) resolves
through this same table via `resolve_model_name`, which drops any `:effort`
modifier — the CLI's `--advisor` flag takes no effort. Only meaningful for the
anthropic namespace (`claude_code` / `tmux_claude`); `NativeBrain` ignores it
entirely, since the advisor is an Anthropic Messages beta tool with no wire
over `openai_compat`. See `.claude/rules/executor.md` § Brain invocation for
how the executor resolves and drops it per task.

Adding a new brain: implement the four Brain methods (`execute`,
`resolve_alias`, `resolve_model_name`, `list_aliases`, `validate_alias_override`),
set a `model_namespace` class attribute (the key operators use in
`[models.aliases.<name>]`; reuse `"anthropic"` / `"openai_compat"` if you share a
family, else a new label), and ship your own canonical-ID constants and
`DEFAULT_ALIASES`. Read overrides via
`get_alias_override_target(name, self.model_namespace)`; apply `split_effort`
first. Operator overrides plug in for free via `_roles.py`.

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
| `advisor: str` | Anthropic-namespace brains only; `""` = no advisor. Set only by the executor, only when `config.advisor_model` is configured and the task carries no model pin (advisor-model spec). `ClaudeCodeBrain` / `TmuxClaudeBrain` emit `--advisor <value>` when both this and `allowed_tools` are non-empty, and otherwise set `CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1` in the child env so a host's `~/.claude/settings.json` `advisorModel` can't run one Istota didn't ask for. `NativeBrain` ignores it. |
| `custom_system_prompt_path: Path \| None` | Override system prompt (claude_code-specific knob) |
| `streaming: bool` | True when `on_progress` callback is supplied |
| `on_progress: Callable[[StreamEvent], None] \| None` | Per-event callback. Widened `StreamEvent` union (task-event-streaming spec): `ToolUseEvent` (carries a real `tool_call_id`) \| `TextEvent` \| `TextDeltaEvent` (per-token incremental answer text — NativeBrain per provider `TextDelta`, ClaudeCodeBrain via the CLI's `--include-partial-messages` `text_delta` frames) \| `ResultEvent` \| `ContextManagementEvent` \| `ToolEndEvent` (NativeBrain only — `success` + loop-measured `duration_ms`) \| `ToolProgressEvent` (NativeBrain only) \| `ThinkingEvent` (whole reasoning block) \| `ThinkingDeltaEvent` (incremental reasoning — NativeBrain `reasoning` deltas, ClaudeCodeBrain `thinking_delta` partials). The executor's `_on_brain_event` adapter maps these to `TaskEvent`s via `EventWriter` (`istota/events.py`): `TextDeltaEvent` → coalesced `text_delta` on stream surfaces (web/repl), dropped on push surfaces; `ThinkingDeltaEvent`/`ThinkingEvent` → coalesced `thinking`, stream surfaces only. A loop-based brain MUST dispatch this callback off its event loop (NativeBrain's `run_in_executor` hop) so the synchronous Talk/log subscribers' `asyncio.run` calls don't collide (ISSUE-111 generalized). Both brains stay surface-agnostic — they emit both per-token deltas *and* whole-block `TextEvent`/`ThinkingEvent`s; the executor dedupes deltas-vs-whole-block per surface (stream: keep deltas, drop the redundant whole block; push: drop deltas, forward intermediate `TextEvent`s as `progress_text`, drop thinking). NativeBrain additionally suppresses the **final** turn's `TextEvent` (its text becomes the result); if the final turn carries no text the held block is released as progress instead, since it is no longer the answer. |
| `cancel_check: Callable[[], bool] \| None` | Polled between events; True → kill subprocess, return `cancelled` |
| `on_pid: Callable[[int], None] \| None` | Called once with subprocess PID after spawn |
| `sandbox_wrap: Callable[[list[str]], list[str]] \| None` | Wraps raw cmd (e.g. with bwrap); no-op if not provided |
| `fs_read_roots: list[Path] \| None` / `fs_write_roots: list[Path] \| None` | NativeBrain-only file-tool path allowlist (NB-1). Populated by the executor (`native_fs_roots`) only under effective sandboxing; other brains ignore them (bwrap already confines their tools). `None` = unconfined (dev / no bwrap). |
| `fs_write_denied_roots: list[Path]` | RO carve-outs nested inside a write root — what bwrap gets by re-binding a subdirectory `--ro-bind` after its parent's RW bind, and what containment alone cannot express. Today `{user_temp_dir}/.developer`. Note the different empty semantics from the pair above: `[]`, not `None`, because a deny set has no unconfined meaning to signal. Enforced on the write path only (the directory stays readable) and ahead of `ToolEnv`'s unconfined early return. |
| `result_file: Path \| None` | claude_code-specific fallback file path |
| `images: list[ImageInput]` | The task's prepared image attachments, as `(path, media_type, display_name)` — never bytes. Built by `executor.prepare_image_attachments` (`istota/image_attachments.py`) and passed to every request the executor makes; the other eight construction sites leave it empty on purpose, including the three health OCR paths, which run their own vision prompt. `path` is resolved and normalized, `media_type` is derived from what Pillow decoded and the format the rewrite chose, and each brain converts at the last moment so nothing large reaches a task row or a log line. See "Image attachments" below for what each brain does with it. Preserved across the executor's fallback copy for free — that copy is `dataclasses.replace`, which carries every unnamed field |

## BrainResult fields
| Field | Notes |
|---|---|
| `success: bool` | Final success/failure |
| `result_text: str` | Final response text |
| `actions_taken: str \| None` | JSON-encoded list of tool-use descriptions |
| `execution_trace: str \| None` | JSON-encoded `[{type:"tool"\|"text"\|"cm_boundary", ...}]`. A `tool` entry carries an optional `raw` = the verbatim Bash command (`_tool_invocation`), threaded by all three brains for playbook command extraction (ISSUE-174) |
| `stop_reason: str` | `completed` / `cancelled` / `timeout` / `oom` / `terminated` / `transient_api_error` / `usage_limit` / `error` / `not_found` / `fallback`. `usage_limit` = a subscription/quota/billing limit (a persistent "brain unavailable" condition the executor reroutes to the configured fallback brain — see "Brain fallback" below). `terminated` = the subprocess was killed by a signal other than SIGKILL — see "Signal deaths" below. |
| `usage: BrainUsage | None` | Per-attempt token/cost telemetry, normalized across brains (`istota.usage`). **Retyped from `TaskUsage`** — the two vocabularies differ and the difference is load-bearing: `TaskUsage.input_tokens` is OpenAI-compat `prompt_tokens`, *inclusive* of cache reads (and `native._log_cache_telemetry` depends on that), while `BrainUsage.billed_input_tokens` excludes them, matching Anthropic's convention. `from_task_usage` reconciles the two at the boundary and labels the result `totals_source='derived'`; `session/usage.py` keeps its shape and that function still runs on the raw `TaskUsage`, before conversion. Set on **every** return, success or failure — tokens are spent either way. `TmuxClaudeBrain` leaves it `None`: it drives the interactive TUI and reconstructs events from a JSONL transcript, so there is no result frame to read, and a synthetic zero would drag every average. |
| `brain_kind: str` | Which brain produced this result, for the usage row. Set by the brain on the way out rather than threaded from the executor's construction site, so it stays correct on the fallback path for free — there the executor's own variable no longer describes the result it holds. One of `KNOWN_BRAIN_KINDS`; empty for `tmux_claude`. |

## ClaudeCodeBrain
Wraps the `claude` CLI subprocess. Owns:

1. **Command construction** — `claude -p - --disallowedTools Agent Workflow
   --dangerously-skip-permissions`, plus optional `--model`, `--effort`,
   `--system-prompt-file`, and an `--output-format` that depends on the mode:
   `stream-json --verbose --include-partial-messages` when streaming,
   `json --verbose` otherwise (see §11 — `--verbose` is what makes the
   non-streaming shape predictable across CLI versions, and with it the
   `init` frame that carries `apiKeySource`). `--include-partial-messages` makes the CLI emit
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
   a background thread to prevent deadlock (streaming only; `communicate()`
   drains both pipes on the simple path). The **streaming** spawn passes
   `start_new_session=True` so the CLI leads its own process group and every
   kill path can take its bash grandchildren with it (ISSUE-257 — a
   `pytest -n auto` run outlived a bare `process.kill()` and finished on a
   saturated host). Two consequences worth knowing: the pid handed to `on_pid`
   is now a group leader, which is what lets `!stop` and the web cancel
   endpoint reach the group; and the CLI has left the daemon's process group,
   so under the local `istota serve` shape (no cgroup) a Ctrl-C reaches the
   daemon but not an in-flight task's `claude`, which then runs to its own
   timeout. Under systemd this is covered — `KillMode=mixed` SIGKILLs the whole
   cgroup after `TimeoutStopSec`.

   The **simple** path still spawns via `subprocess.run`, so its timeout still
   kills only the direct child and orphans the tree — the deferred half of
   ISSUE-257. Narrower than the streaming path was: `_execute_simple_once`
   never calls `req.on_pid`, so no `worker_pid` is recorded and neither cancel
   endpoint reaches it at all. Fixing it means spawning via `Popen` so the
   group can be killed, and roughly ninety tests across six files patch
   `subprocess.run` to keep the brain from spawning, so those move first.
4. **Stream parsing** — line-by-line via `make_stream_parser()` from
   `_events.py`, dispatching ResultEvent → final result, ToolUseEvent /
   TextEvent → trace + on_progress, ContextManagementEvent → `cm_boundary`
   marker in trace. The `stream_event` partial frames parse into
   `TextDeltaEvent` / `ThinkingDeltaEvent` and go to `on_progress` only (never
   the trace); the trailing whole-block `TextEvent` / `ThinkingEvent` still
   records the trace and is deduped against the deltas executor-side.
5. **Cancellation** — polls `req.cancel_check()` between events; final
   re-check after subprocess exit catches SIGTERM-style external kills.
   The in-loop kill goes through `process_group.kill_process_group`, not
   `process.kill()`.
6. **Timeout** — `threading.Timer` kills the process group after
   `req.timeout_seconds` (same helper); result tagged `stop_reason="timeout"`.
   Both kill sites skip a process that has already been reaped
   (`process.returncode is None`): the timer can still fire during the two 5s
   thread joins that follow `process.wait()`, and a raw pid carries none of the
   protection `Popen.send_signal` gave — the number may by then belong to
   someone else, whose group would be killed.
7. **Signal deaths** — a negative returncode means the subprocess died on
   signal `-rc` (`_signal_result`, both exec paths, checked after the
   cancellation/timeout branches so `!stop` still reports as a cancellation).
   `-9` keeps its OOM wording + `stop_reason="oom"` (SIGKILL is the OOM
   killer's and systemd-oomd's signature); every other signal returns
   `"Claude Code was terminated by <NAME> (signal N)"` with
   `stop_reason="terminated"`, a WARNING, and the execution trace attached.
   Before ISSUE-191 only `-9` was recognized and every other signal fell to the
   generic stream-parse catch-all ("Stream parsing failed (rc=-15, N lines)").
   `is_signal_termination(text)` is the shared marker predicate the scheduler
   classifies on (the executor drops `stop_reason` at its return boundary, so
   the scheduler reads failure *text* — same as OOM and cancellation).
8. **API retry** — wraps single-attempt execution in a 3-attempt loop when
   `is_transient_api_error()` matches (every 5xx, plus 408/425/429). The
   delay is the provider's own `Retry-After` where it supplied one, capped
   at `RETRY_AFTER_MAX_SECONDS`, else `API_RETRY_DELAY_SECONDS`.
   Retries do NOT count against the task's `attempt_count`.
9. **Result fallback** — prefers ResultEvent > result_file > stderr.
10. **Usage capture** — off the same stream, into `BrainResult.usage`. Totals and
    the per-model split come from the terminal frame's `modelUsage`, **not**
    `result.usage`: measured on a two-turn run, `modelUsage` reproduces
    `total_cost_usd` exactly while `result.usage` is 533 input and 14 output
    tokens short, because it covers only the main agent's conversation and not
    the CLI's own out-of-band calls. Totalling from `result.usage` therefore
    under-reports spend *and* breaks the invariant that a parent's totals equal
    the sum of its children.

    Per-request context measures come from `stream_event`/`message_delta`
    frames, one per API request, carrying the final usage for that request.
    Deliberately not the `assistant` frames: `parse_stream_line` returns one
    event per line and that branch already ends in a ladder returning a
    `ToolUseEvent` / `TextEvent` / `ThinkingEvent`, so emitting usage there
    would consume the return slot and drop the tool event — costing a tool chip
    on the live surface, an `actions_taken` entry and the `execution_trace`
    entry the sleep cycle reads for playbooks. `tests/test_stream_parser_usage.py`
    carries the regression guard. `message_delta` is also better data: once per
    request, no `message.id` dedup, and the true output count rather than the
    per-content-block snapshot an `assistant` frame carries.

    Sub-agent frames (`parent_tool_use_id` set on the wrapper) and compaction
    replays are excluded from the context measures and counted instead, so the
    peak means *this* agent's peak and a replay does not inflate the request
    count. Neither `RequestUsageEvent` nor `RateLimitEvent` is forwarded to
    `req.on_progress` or the execution trace — the executor fans progress out to
    live surfaces, and an accounting frame in a user's chat is a bug.

    `cost_basis` comes from the `init` frame's `apiKeySource`, and an
    unrecognized spelling is `unknown` rather than guessed into `api` — a
    subscription's list-price equivalent must never render as spend. Only the
    final in-brain retry attempt's usage is captured (a documented limitation),
    but a retry that exhausts its attempts still records that attempt rather
    than nothing. Both retry loops carry that attempt's usage onto the results
    they build themselves (`last_usage`), so an exhausted ladder or a cancel
    during the backoff still writes a row.

11. **Non-streaming usage capture** — the simple path gets
    `--output-format json --verbose` (no partials) and parses its usage
    out of stdout rather than off a stream, which is what measures the daemon's
    eight task-less origins. **What `json` emits on its own is
    CLI-version-dependent and both shapes are live** (ISSUE-271): 2.1.227 emits
    a JSON *array* of the same frames the streaming path produces, 2.1.238
    emits the bare terminal `result` frame as a single object.
    `_parse_simple_json_output` reads either, wrapping the object as a
    one-element frame list so the array loop is the only implementation.

    `--verbose` is what makes the shape predictable: measured against both
    deployed versions, 2.1.227 and 2.1.239 each emit the array *with* the
    `system`/`init` frame when it is passed. Without it the newer CLI drops
    that frame, and the `cost_basis` degradation below stopped being a rare
    fallback and became every row this path wrote — `sleep_cycle`,
    `code_review` and `shared_blocks` all landing on `unknown` while carrying
    real reported cost, split off from the identically-credentialled `task`
    rows for no reason visible to a reader of the dashboard.

    An object counts as the terminal frame **only** when its `type` is
    `result` — several daemon callers ask the model for a JSON answer, so
    `{`-leading stdout is not on its own evidence of an envelope. Anything
    matching neither shape returns `(None, None)` and the caller keeps raw
    stdout as the answer: that fallback is load-bearing, not defensive, since
    roughly ninety tests across six files patch `subprocess.run` with
    plain-text stdout and a CLI ignoring the flag behaves identically. Output
    that *did* come from the CLI (a known frame `type`, or an envelope-only key
    like `modelUsage`) but carries no terminal frame logs one WARNING — the
    silent fallback is why ISSUE-271 survived three weeks, reading as success
    at every layer above.

    The single-object shape carries no `init` frame, so two fields degrade —
    reachable now only on a CLI that ignores `--verbose`, not in a current
    deployment. `cost_basis` is `unknown` (deliberate — inferring it from
    config is exactly the guess `cost_basis_from_api_key_source` refuses) and
    `model_hint` is
    empty, so `usage.model` comes from `modelUsage`'s dominant child and a
    costed frame with no children lands model-less. Totals and cost are
    unaffected; `modelUsage` is present in both shapes. There are no
    `message_delta` frames on this path either way, so these runs carry totals
    and NULL context columns.

`_compose_full_result()` does NOT live in the brain — both brains will
produce `(result_text, execution_trace)` and the executor reconciles them.

## Image attachments

`BrainRequest.images` arrives as paths and media types, and each brain owns the
conversion to its own provider's shape. That split is what keeps base64 out of
the task row and out of every log line, and keeps the executor from learning a
wire format. What the brains share is the rule underneath: **a model must never
be left to infer that it saw an image.** Every path that cannot deliver the
pixels names the image and says why, because "attached" alone is not evidence
of sight and silence is what produced the confident blind answer this whole
change exists to prevent (ISSUE-366).

**NativeBrain** builds the first user message in `_initial_user_content`: one
`TextContent` with the prompt, then one `ImageContent` per image — text first,
which is the order OpenRouter's image-understanding guide documents.
`OpenAICompatibleProvider._message_to_wire` already renders those as
`data:<media_type>;base64,<data>` URLs, so the provider layer needed no change.
Encoding happens immediately before the first call, so the base64 lives exactly
as long as the request. Three refusals, each per image and none of them fatal to
the rest: the resolved model's `supports_vision` is false (no file is read at
all — reading bytes to discard them is pure cost — and every image gets
`_NO_VISION_NOTICE`, plus one operator WARNING naming the model, since
`supports_vision` defaults false and a direct-Anthropic base URL fetches no
catalog); the file vanished or is unreadable (`_UNREADABLE_NOTICE`, naming the
exception class only); or it outgrew `_MAX_IMAGE_BYTES` (6 MiB). That last bound
is asserted here rather than inherited from preparation because this is a
*second* read of a file under the user temp dir, which bwrap binds read-write
into that user's own sandboxes — another task of theirs can replace it between
the two reads. The constant is restated rather than imported (importing
`image_attachments` from a brain closes a cycle through `brain/__init__.py`) and
`tests/native/test_input_images.py` holds the two equal.

**Compaction must not delete the images in silence** (`session/compaction.py`).
The image-bearing message is at index 0 and `find_cut_point` walks back from the
newest, so an ordinary cut takes it — and `_serialize_for_summary` handles only
`TextContent` and `tool_call`, so the summarizer would never learn an image had
existed. Two halves, and they are deliberately exclusive rather than belt-and-
braces. `plan_image_pin` returns `(pin, summary_input)`: the pin is a small
`UserMessage` holding `_PIN_LABEL` plus the first image-bearing message's
blocks, prepended ahead of the summary, and `summary_input` is the same history
with exactly those blocks removed — so the summary's `[image <name> — no longer
in context]` notice is written only over blocks that really did go. Leaving them
in both places would write a durable summary saying an image was lost at the
moment it was being carried over, and that text is updated forward on every
later cycle. The pin is refused when it would take more than half the
`keep_recent_tokens` budget (`_PIN_TOKEN_SHARE`), because a pin that swallows
the tail budget makes `find_cut_point` return 0 and compaction a permanent
no-op; the loss notice carries the fact instead. The pin is what keeps the
capability, the notice is the floor.

**ClaudeCodeBrain and TmuxClaudeBrain** have no image block to send, so their
vision path is Claude Code's own `Read` tool, which returns visual content
rather than bytes. `build_image_prompt` prepends one of two sections to
`req.prompt`, and a request with no images is returned byte-identical:

- with tools, `IMAGE_DIRECTIVE_HEADER` plus the resolved absolute path of each
  image, requiring one `Read` per image before any answer or other action, and
  requiring a failed `Read` to be reported rather than guessed around. The
  wording follows `health/ocr._build_vision_prompt`, which shipped the same
  instruction first;
- with `allowed_tools=[]`, `IMAGE_OMITTED_HEADER` and the basenames. An empty
  tool list is a caller's policy decision (the sleep cycle, the health OCR
  paths), never a gap to fill: the tool set is not enabled implicitly, which is
  the split `health/ocr.py` already settled with its own
  `allowed_tools=["Read"] if allow_read` line.

The tmux brain puts the same section in `prompt.txt` via `prompt_file_text`,
ahead of the original request, because it submits one buffer per run and a
second paste would be a second turn. Both brains name only basenames in a
notice and only resolved paths in a directive.

**The directive is checked rather than trusted.** The audit lives in the
executor (`unread_images`, `.claude/rules/executor.md`): it counts `Read` calls
in the recorded execution trace and appends a note naming any image that was
never opened. A recorded `ToolUseEvent` is a fact about what the model did,
which is a different thing from grading its prose.

**An image-payload rejection re-issues once, with a notice.**
`ClaudeCodeBrain._execute` applies the image section, runs one attempt, and on
`is_image_payload_rejection` re-issues with `images=[]` and
`build_withdrawn_image_prompt`, which names every withdrawn image and tells the
model not to imply it saw them. Not a silent strip: a blind retry can produce a
confident answer that lost sight, which is the original defect by another route.
Four bounds on that second call. It is skipped when the result was a success (an
answer *quoting* a provider error would otherwise cost a paid call and replace
the user's answer); it is skipped when `result.work_committed` is set, the same
veto `_is_retryable` applies for the same reason — a first-call 413 never
reached the model and arrives with the flag clear, while a 413 later in a run is
the accumulated context and is a reroute rather than a re-issue; `req.result_file`
is unlinked first, or the re-issue can deliver the text the *images* produced
under a prompt saying they were withdrawn; and the timeout is what remains of
the original budget, floored at `_MIN_REISSUE_SECONDS`, since two full attempts
under one `timeout_seconds` would hold a worker for twice its configured bound.
Once only — a rejected re-issue falls through to the ordinary classification.

## API error helpers
| Function | Purpose |
|---|---|
| `parse_api_error(text) -> dict \| None` | status_code/message/request_id from `API Error: NNN {json}` **or** the bodyless `API Error: NNN <text>` the CLI also emits (ISSUE-212 — matching only the JSON form meant a bare `API Error: 529 Overloaded` parsed as nothing: not transient, not retried, not a fallback trigger). Tail stops at the newline |
| `is_transient_api_error(text) -> bool` | True for a capacity status (**every 5xx**, plus 408/425/429 — see `_status_is_transient` below) **or** a network-level failure (connection reset / timeout / DNS / `ECONNRESET`-class errno). The network branch is gated on an `API Error` marker (or an unambiguous errno) because this predicate also runs against arbitrary tmux pane text; an explicit status is authoritative, so a 400 quoting "connection reset" stays permanent (NB-13a) |
| `is_permanent_api_error(text) -> bool` | True for a request-shaped failure: `PERMANENT_STATUS_CODES` (`400/401/403/404/405/413/414/422`), context-length, content-filter, `invalid_request_error`-class bodies. A transient status wins over request-shaped body text |
| `api_error_stop_reason(text) -> str \| None` | The single classifier every execution path uses: `usage_limit` > `error` (permanent) > `transient_api_error` > `None` (not a provider error). `_failure_stop_reason` is `api_error_stop_reason(text) or "error"` |
| `_status_is_transient(status) -> bool` | The live transient rule: **every** 5xx, plus 408/425/429. `TRANSIENT_STATUS_CODES` is kept as documentation of the common cases but is no longer the gate — enumerating was a latent second copy of this bug (a Cloudflare-fronted provider emits 520-526, none of which were listed, so each would have dead-ended exactly as 529 did) |
| `is_api_error_banner(text) -> bool` | True iff the text *is* a bare API-error banner — anchored at the start (past ≤8 chars of decoration) and length-gated, mirroring `is_usage_limit_banner`. `claude -p` reports a provider failure as a **success** result frame with the error as the whole answer, which is how a raw `API Error: 529 Overloaded` reached the user as the final reply; strict so a genuine answer *discussing* an earlier API error isn't converted into a retry + a paid fallback call |
| `parse_retry_after(text) -> float \| None` | The provider's requested wait, capped at `RETRY_AFTER_MAX_SECONDS` (60s) and treating ≤0 as absent. Both retry loops use it in place of the fixed delay; the cap exists so a worker is never parked on the provider's word for an hour when the task's own retry ladder / the fallback could take over |
| `is_usage_limit_error(text) -> bool` | True if the text carries a subscription/quota/billing usage-limit signal (keyword set + an "exceeded…limit" regex). Provider-agnostic (works on CLI output, tmux transcript/pane text, and native error bodies). Checked *before* the transient predicate at every call site so a quota 429 classifies as `usage_limit`, not a retry. |
| `is_image_payload_rejection(text, has_images) -> bool` | True iff the provider is refusing *this* request's images: a 413, or a 400 whose diagnostic names an image (`image`, `media_type`, `attachment`). The 400 arm needs that test and the 413 arm does not — `exceeds`, `too large` and `maximum` are also the vocabulary of a context-length complaint, and matching them buys a second paid run plus a notice blaming images that were not the problem. Checked ahead of the classification above, which puts both statuses in `PERMANENT_STATUS_CODES` and would fail an otherwise valid text task with no answer and no fallback |

`parse_api_error`, `is_transient_api_error` and `is_usage_limit_error` are
re-exported from `executor` for `scheduler.py` and tests; the newer helpers are
imported from `brain.claude_code` directly (nothing needs a back-compat
re-export). Canonical home is `brain/claude_code.py` for all of them.

**Consumers must pick the right strictness.** `parse_api_error` answers "does
this text contain a provider status code" — fine for *formatting* an
already-known failure, wrong for *deciding* something is a failure.
`scheduler`'s masquerading-success guard and `_is_policy_refusal` both discard a
completed answer, so both key on `is_api_error_banner`; widening the parser
without moving them was the ISSUE-212 fix's own near-miss (an answer summarising
yesterday's 529 would have been failed and retried three times).

**Retry vs reroute.** `BrainResult.work_committed` marks a failure whose run
already reached the model and may have executed tools — set by every
success-frame reclassification, since the CLI ran to completion. `_is_retryable`
vetoes the in-brain retry on it, so those failures are reroute-only and a task
that wrote files or sent mail before the provider fell over doesn't repeat that
work three times. The backoff itself is slept in `_RETRY_SLEEP_SLICE_SECONDS`
slices with a `cancel_check` poll between them, so `!stop` lands during a
(now possibly 60s) provider-requested wait rather than after it.

## Configuration
```toml
[brain]
kind = "claude_code"  # "claude_code" | "native" | "tmux_claude"
# Availability failover (see "Brain fallback" below). "" = none.
fallback = "native"               # brain kind to fall back to when primary unavailable
fallback_on_transient = true      # also reroute a persistent transient_api_error (default on)
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
  ("fast","general","smart")` is the single source of truth (every brain's
  `DEFAULT_ALIASES` tier keys import it); a contract test asserts every brain
  resolves every canonical role. `is_portable_alias(name, portable_names)` (with
  `split_effort` applied first, so `smart:low` reads portable) decides whether a
  requested model name is a portable *intent* (a canonical tier, or a custom
  alias the operator flagged `portable = true`) that re-resolves in the fallback
  namespace, or a non-portable pin (shortcut `opus`, canonical `claude-opus-5`)
  that can't cross the boundary. The executor computes `portable_names` via
  `config_alias_portable_names(config)` (`CANONICAL_ROLES` ∪ declared-portable).

- **Availability breaker + routing** (`brain/_fallback.py`, wired in
  `executor.py`). See the trigger/cooldown sets and the executor path in
  `.claude/rules/executor.md` "Brain fallback". `PrimaryAvailabilityBreaker` is a
  process-global, thread-safe breaker keyed by primary kind — distinct from
  `tmux_claude._BREAKER` (which governs tmux launch fast-fail); the two compose.
  `effective_fallback_kind(brain_config)` is the configured `[brain] fallback`
  or None — explicit config only, no implicit target for any kind, and None also
  where the configured value equals *this* config's `kind`, since rerunning the
  same brain cannot help. That last test is here rather than only at config load
  because `brain_config` may be a **routed** config: `resolve_brain_kind` returns
  `replace(brain_config, kind=target)` and the routed config inherits `fallback`,
  so `kind = "claude_code"` + `source_type_overrides = {scheduled = "tmux_claude"}`
  + `fallback = "claude_code"` is a self-fallback for an interactive task and a
  real target for a scheduled one. A
  `tmux_claude` primary used to resolve to `claude_code` there with nothing
  configured; that shim predated the generalized `fallback` key and was removed
  in ISSUE-362, because it left no value of `fallback` meaning "no failover" on
  a tmux deployment and made both of `_validate_brain_fallback`'s "disabling
  fallback" warnings false — blanking the field is what activated it.

**Trigger set** (reroute this attempt): `{usage_limit, not_found, fallback}` +
`transient_api_error` iff `fallback_on_transient` (**on by default** since
ISSUE-212 — a capacity error that survived the primary's own
`API_RETRY_MAX_ATTEMPTS` is precisely what the fallback exists to absorb, and the
alternative is handing the user a raw provider error). **Cooldown set** (open the
breaker → skip the primary on subsequent tasks for `fallback_cooldown_seconds`):
`{usage_limit, not_found}` only — `fallback` is excluded so tmux keeps being
probed per-task (its own breaker decides when to stop). **Never fallback:**
`oom` / `timeout` / `cancelled` / `error` (task-level outcomes, flow through the
normal path). Config keys: `[brain] fallback` / `fallback_on_transient` /
`fallback_cooldown_seconds`; `_validate_brain_fallback` (config load) neutralizes
an unknown kind with one WARNING, and a self-fallback — now "the only kind this
deployment runs", not the bare `fallback == kind` string comparison, so the
routed shape above survives — with another. It also logs one INFO line, once per
process, where `tmux_claude` runs (as `kind` or as an override target) with no
fallback: that pairing was unconfigurable before ISSUE-362, so an upgrade drops
failover silently otherwise, and `load_config` runs in every skill-CLI spawn, so
the notice is process-scoped rather than per call. Single fallback level only;
if the fallback is also unavailable the task fails/retries normally. On a dropped
non-portable pin the successful reply gets a one-line italic model note.

**The cooldown is a deadline, not a duration** (ISSUE-374). `fallback_cooldown_seconds` is the *ceiling*; where the reason is `usage_limit` and the primary is a subscription brain (`claude_code` / `tmux_claude`), the window ends at the quota's own reset instead. `open_primary_breaker` is the one place that decides it, so the in-memory breaker and `brain_availability`'s `expires_at` cannot describe two different windows — the executor's task path and `report_brain_result` both go through it. The reset comes from `subscription_usage.cached_reset_seconds`, which reads the deployment-wide **disk cache only**: no fetch, no credential resolution, no socket on the path a failing task is standing on, which it can afford because `resets_at` is absolute and the cache reader recomputes the countdown against now. `soonest_reset_seconds` takes the earliest *future* window and ignores which one hit its limit — a `stop_reason` does not say, and the asymmetry decides it: too short costs one failed primary attempt and reopens the breaker, too long runs every task in the remainder of the window on a different model. That is the observed failure — a limit hit eleven minutes before the reset held every task on the fallback brain for the remaining forty-nine, with the primary idle and available. Clamped in both directions inside `open`'s lock: never past `opened_at + fallback_cooldown_seconds`, so a wrong reset cannot pin the deployment to its fallback, and never below `MIN_COOLDOWN_SECONDS` (60, itself capped by the cooldown), so a reset seconds away does not produce a breaker that does nothing. `not_found` is excluded — a quota reset says nothing about a missing binary — as is a `native` primary, whose provider has its own quota on its own clock. No cache, a disabled `subscription_usage` or a window that has already reset all fall back to the flat cooldown. A repeat failure inside an open window still never moves the deadline, a later `until` included.

### Direct-caller availability (ISSUE-181)

The sleep cycle (`memory/sleep_cycle.py:_run_sleep_cycle_brain`) and shared-block
synthesis (`briefings/shared_blocks.py:_run_section_brain`) call the primary
brain **directly** (`make_brain(config.brain).execute(req)`), not through the
executor's fallback-wrapped path — so "pause on fallback" reduces to "detect
primary-brain unavailability and skip." Two Config-free helpers in
`brain/_fallback.py` give them the same breaker signal the executor arms:

- **`primary_brain_unavailable(brain_config) -> (available, reason)`** —
  consult before each call (or before a batch). Returns `(False, "unavailable")`
  when the breaker is open for the primary kind, so a degraded primary doesn't
  grind through every channel/block. Honours `fallback_cooldown_seconds` (`0` =
  every caller probes, matching the executor).
- **`report_brain_result(brain_result, brain_config) -> reason | None`** — feed
  a direct caller's `BrainResult` back into the shared breaker. Opens it
  (returns the `stop_reason` only on the closed→open transition → caller arms
  exactly one operator alert) on `usage_limit`/`not_found`; closes it on success.
  Mirrors the executor's task path, so the breaker is a **single shared signal
  across all brain callers** — whichever path first hits the limit opens it and
  alerts; the others see it open and skip silently.

The sleep cycle short-circuits both passes at the top (`check_sleep_cycles` /
`check_channel_sleep_cycles`) and re-checks per-iteration so a mid-pass failure
stops the remaining channels (the four-identical-errors-in-six-seconds pattern
from ISSUE-181). Shared-block synthesis skips the gather+brain and keeps
last-known-good content; one operator alert fires when the breaker opens.
`structured` shared blocks never touch the brain, so they still generate when
degraded. The breaker cooldown gates the next scheduled run, so neither
re-attempts every cycle while the primary stays down; a bounded "still down"
heartbeat re-alerts once per cooldown window (org-monthly limit) until an admin
raises it, then the next probe succeeds and closes the breaker.

These six sites (plus the executor and conversation-context triage) are also
the nine `BrainRequest` construction sites the advisor-model spec enumerates.
All six build their env from `dict(os.environ)` and run unsandboxed, so — unlike the executor, whose
sandbox only RO-binds the host's `~/.claude/settings.json` — they read the
daemon user's **real** settings file directly. Any Claude Code setting that
changes model behaviour (`advisorModel` is the first one Istota has taken a
position on) is inherited here too unless a brain neutralises it structurally;
see `.claude/rules/executor.md` § Environment Variable Mapping and "Model
identity" below.

### Fallback-compatibility posture registry (ISSUE-181, Problem 3)

`brain/_postures.py` declares, for every scheduled/automatic brain-calling
task, one of three postures for what happens when the primary is unavailable:

- **skip** — non-essential tasks (sleep cycle, shared-block synthesis, location
  discovery) that can wait. Don't run against a degraded brain; resume when the
  primary recovers. Implemented via the breaker helpers above.
- **pin** — essential tasks that must produce a real answer and shouldn't ride
  the fallback (briefings — ISSUE-180; scheduled `prompt` jobs via per-job
  `model`).
- **fail_clean** — interactive-but-automatic callers (health OCR, biomarker
  explainer) whose failure should be visible ("couldn't generate — brain
  unavailable") rather than a silent stub.

The registry is a declared, discoverable data structure (`TASK_POSTURES`,
`task_postures_by_name()`) — each entry carries its call site + notes — so the
policy is auditable in one place rather than scattered as ad-hoc per-task
logic. A task not listed routes through the executor's fallback wrapper and
needs no separate posture. ISSUE-180 (briefings pin/fail-clean) is the inverse
face of this policy; together they define the essentialness/skip-pin-fail-clean
contract in both directions.

NativeBrain pi-parity capabilities (over `openai_compat`, the sole transport):
- **Final-turn answer (ISSUE-211).** `result_text` is `final_turn_text` — the
  text of the turn the run actually ended on. It used to be
  `last_assistant_text` (the last turn that happened to carry *any* text), so a
  tool-only or empty final turn shipped an earlier turn's between-tool-calls
  narration verbatim as the answer. An empty final turn now leaves `result_text`
  empty and `session.result._ensure_final_answer` surfaces "the turn ended
  without a final response" instead. Both values are tracked, because the
  **abnormal-stop paths deliberately keep the old behaviour**: a
  `_TRUNCATION_MARKERS` hit (NB-15) or a `_PARTIAL_ANSWER_STOP_REASONS` stop
  (`max_turns` / `loop_detected`, ISSUE-187) delivers the text *with a marker
  saying it is incomplete*, so falling back to `last_assistant_text` there is
  honest — and without it a capped run whose last turn was tool-only would ship
  a bare marker and drop the partial work, while a `max_tokens` turn after a
  real answer would flip a success into a retried failure.
- **Trace document order (ISSUE-211).** The agent loop runs a turn's tools
  *before* emitting its `turn_end` (`agent/loop.py`: `_execute_tool_batch` then
  `turn_end`), so appending tool entries as they fired recorded them **ahead of
  the text the model wrote first** — every native trace was inverted, which is
  measurable: 100% of native traces in production start with a `tool` entry
  against a 53/46 split for the CLI brains. The brain now buffers a turn's tool
  entries (`pending_tools`) and flushes them after that turn's text at
  `turn_end`, with a post-loop flush for a run torn off mid-turn. This matters
  beyond cosmetics: the finality rule in `session/result.py` reads "text after
  the last tool call" as the final message, so an inverted trace made narration
  look like the answer, and the web transcript's render groups showed tools
  ahead of the narration that preceded them.
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
- **Cost source.** `TaskUsage.cost_usd` prefers the provider's own reported
  cost over catalog pricing. OpenRouter returns real per-request cost (markup
  included) in the trailing usage chunk when the request carries
  `"usage": {"include": true}` — `openai_compat` sends that param scoped to
  `openrouter.ai` base URLs (other endpoints may 400 on it) and parses the
  top-level `usage.cost` via `_parse_reported_cost` (finite / non-negative /
  not bool-or-string; `NaN`/`Infinity` from `json.loads` are dropped so one bad
  turn can't poison the task total). `Usage.cost_usd` is three-state: `None` →
  `TaskUsage.add` computes from the catalog (`price_usage`), a number → used
  verbatim, `0.0` → a real free turn (respected). The native loop accumulates
  usage on `total_tokens > 0 or cost_usd is not None`, so a costed zero-token
  turn isn't dropped. Non-OpenRouter endpoints are unchanged (no request param;
  catalog pricing).
- **Model catalog (config-first + live OpenRouter enrichment, ISSUE-182).** Per-
  model metadata (`context_window`, `supports_thinking`/`supports_vision`, prices)
  resolves through `llm.catalog.get_model_info` — a pure, synchronous three-layer
  chain: operator `[brain.native.model_overrides]` (partial, merged on top) >
  live-fetched OpenRouter catalog (`_FETCHED`) > conservative `_DEFAULT`
  (`context_window=200_000`, zero price). **There is no bundled catalog file** —
  `model_catalog.json` was deleted. When `base_url` contains `openrouter.ai` and
  `[brain.native] model_catalog_fetch` is on, `NativeBrain._ensure_fetched_catalog`
  (called once at the top of the async run) fetches OpenRouter's public
  `GET /models` list, parses it (`llm.openrouter_catalog.parse_openrouter_models`
  — per-token USD → per-mtok, `input_modalities`→vision, `supported_parameters`
  `reasoning`→thinking), and installs it via `catalog.set_fetched_catalog`. The
  fetch is lazy, disk-cached (`{db_path.parent}/openrouter_models.json`, TTL
  `model_catalog_cache_ttl_hours`, parsed-fields not raw payload so upstream drift
  can't poison a read), and **never fatal**: fresh cache → live fetch → stale
  cache → 200k default. A process-global lock + `_CATALOG_FETCHED_AT` guard mean
  at most one fetch per process per TTL (no worker-thread stampede). A
  non-OpenRouter native endpoint (local vLLM/Ollama, direct Anthropic we don't
  run) is never fetched — it sets `context_window` (or a `model_overrides` entry)
  as the documented contract, else it gets the 200k default (overflow is
  recoverable; premature compaction is merely wasteful).
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
- **Bash runs under `pipefail`.** The argv is built by `shell_exec.shell_argv`,
  so it is `bash -o pipefail -c` rather than `bash -c` — the counterpart of
  ISSUE-307 on the shell the native brain actually uses. `[exit code: N]` is a
  claim about whether the command worked, and without the option a pipeline
  reported its *last* stage, so `pytest … | tail -3` came back clean on a suite
  that failed. It is the bare name rather than a probed absolute path because
  the argv is handed to `sandbox_wrap`: bubblewrap binds `/usr` and need not
  reproduce the host's `/bin` symlink, so PATH resolution inside the namespace
  is what has always worked here. `exit 141` is SIGPIPE (`| head`, `| grep -q`
  closing the pipe early) and carries `shell_exec.SIGPIPE_NOTE` after the
  marker, since a bare 141 reads as a failure and the command was correct. The
  second cost has no marker and is documented rather than detected: a non-final
  stage exiting non-zero to *report* something (`grep` with no match) now
  colours the pipeline.
- **So do the two CLI brains, by a different route (ISSUE-321).** A
  `ClaudeCodeBrain` or `TmuxClaudeBrain` task runs its commands through the
  Claude Code CLI's *own* Bash tool, which builds `bash -c 'source
  <shell-snapshot> && eval <cmd>'` in a process istota launches and does not
  instrument — so `shell_argv` cannot reach it and that shell started with the
  option off, on the surface where the great majority of tool calls happen. The
  environment is the only lever that does: `executor.build_clean_env` sets
  `SHELLOPTS=pipefail` (`shell_exec.pipefail_env`), which bash reads at startup
  and which survives the sourced snapshot — measured; the snapshot restores
  functions, aliases and PATH and touches no `set -o` option. `SHELLOPTS`
  rather than `BASH_ENV` because it carries option *names* and cannot name a
  file to source, so it opens no exec inlet; see `.claude/rules/executor.md`
  under `build_clean_env` for the full comparison. Being inherited rather than
  an argv flag, it also reaches a pipeline inside a nested `bash script.sh`,
  which `-o pipefail` does not — the two brains therefore agree on an identical
  command string, which is what ISSUE-307 wanted when it left this alone.

NativeBrain hardening (2026-07-18 audit, NB-1…NB-24 — see the audit doc in the
project notes for the full list):
- **File-tool confinement (NB-1).** The in-process file tools run outside bwrap,
  so `ToolEnv` enforces a symlink-resolved read/write path allowlist. The
  executor computes the same user-data roots bwrap would bind
  (`executor.native_fs_roots`) and passes them via `BrainRequest.fs_read_roots`/
  `fs_write_roots`, active only when `native_fs_confinement_active(config)`
  (`sandbox_enabled` + bwrap available) — matching the claude_code boundary.
  Other brains ignore the fields (bwrap already confines their tools).
  `fs_write_denied_roots` carries the RO carve-outs bwrap gets by re-binding a
  subdirectory `--ro-bind` after its parent's RW bind — containment alone can't
  express a hole inside a root. Today that is `{user_temp_dir}/.developer`,
  which holds the credential helpers; a writable copy of those is a
  credential-interception path. Denied is checked before allowed, on the write
  path only, so the directory stays readable.
- **Model resolution (NB-3).** Built-in role aliases (`fast`/`general`/`smart`)
  resolve to `native.model` unless remapped via `[models.aliases]`; provider
  shortcuts (`opus`/`sonnet`/`haiku`) pass through untranslated. A `:effort`
  modifier still applies (`split_effort`). Per-model capability/window overrides
  via `[brain.native.model_overrides]` (NB-4).
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

Native-brain coding enhancements (2026-07-20, `Specs/Done/native-brain-coding-enhancements.md`)
— native-path-only; the `claude_code`/`tmux_claude` brains take their prompt +
tools from the CLI and are byte-unchanged:
- **Fuzzy, multi-edit Edit tool.** `session/tools/edit_engine.py` (pure logic
  ported from pi's `edit-diff.ts`) backs `make_edit_tool`. Matching is
  exact-first then a bounded fuzzy fallback (Unicode NFKC, trailing-whitespace
  strip, smart-quotes/dashes/exotic-spaces → ASCII); it does **not** tolerate
  indentation/internal-whitespace reflow. An optional `edits[]` array applies
  several disjoint edits in one call (uniqueness + overlap enforced); the legacy
  `old_string`/`new_string`/`replace_all` shape is retained (`replace_all` stays
  exact-only — fuzzy+replace_all is disallowed). A `prepare_arguments` shim
  coerces `edits`-as-JSON-string and legacy→one-element-`edits`. Reads/writes
  **raw bytes** (not `read_text`) to preserve CRLF/BOM through the edit. When any
  edit is fuzzy the batch matches in normalized space but writes via
  `apply_replacements_preserving_unchanged_lines` so untouched lines keep their
  exact bytes. Failure messages are actionable (not-found / duplicate / overlap /
  empty / no-op).
- **Coding system prompt.** `native._extract_system_prompt` prepends the
  module-level `CODING_SYSTEM_PROMPT` (generic coding hygiene: read-before-edit,
  prefer Edit over Write, batch multi-site edits into one `edits[]` call, keep
  `old_string` minimal-but-unique, verify with tests) **only when
  `req.allowed_tools` is non-empty** — a text-only invocation (sleep cycle) keeps
  an empty prompt. An operator `custom_system_prompt_path` is appended after it.
- **Parallel tool execution.** `native.py` sets `tool_execution="parallel"`, so
  independent read-only tools (Read/Grep/Glob/WebFetch) run concurrently; the
  loop's existing guards still serialize any batch containing a mutation
  (Write/Edit/Bash are `execution_mode="sequential"`) or two calls to the same
  path (`_has_path_overlap`), and results append in call order.
- **Truncated-tool-call guard.** In `agent/loop.py`, a tool-call assistant
  message with `stop_reason == "max_tokens"` (the provider's map of
  `finish_reason="length"`) is **not executed** — `_truncated_tool_results`
  synthesizes an is-error result per pending call (keeping tool_call/result
  pairing valid) and the loop lets the model re-issue. Mirrors pi's guard.
- **Recovery hints in truncated output.** Read's tail note names the concrete
  `offset=` to continue from; Grep's head-limit note says how to see more; Bash
  spills full over-cap output to a task-scoped temp file (lazily, under
  `ToolEnv.deferred_dir` = `ISTOTA_DEFERRED_DIR`, fallback system temp) and names
  it in the result (`… [output truncated at N bytes; full output: PATH]`) instead
  of silently dropping the tail. `_SpillWriter` is best-effort (degrades to
  cap-only on I/O error) and skipped when `exclude_from_context` is set. Knob:
  `[brain.native] bash_spill_full_output` (default true).
- **Grep context + literal.** `-C`/`context` (integer) adds surrounding context
  lines in `content` mode (ripgrep `path:lineno:` match / `path-lineno-` context
  rendering, `--` between non-adjacent groups); `literal` (`re.escape`) matches a
  plain string. Pure-Python, no ripgrep dependency.

### Turn-budget awareness nudge (ISSUE-187 defect 3)

The `max_turns` cap is a hard safety net the model can't see, so a long
explorative task routinely gets capped mid-plan (the incident: a Lisbon-apartment
search capped at turn 80 on *"let me move to Otodom and OLX next"* — mid-plan,
non-empty narration delivered verbatim as the answer). Defects 1–2 (the masking:
`max_turns`/`loop_detected` collapsed to `completed`; the truncation marker gated
on an empty result) shipped in `6e4cd4e` and made the cap *visible when hit*. This
is defect 3 — making the model *pace itself* so it's hit less often, and so a
capped run produces a deliberate partial deliverable.

Native-only (the CLI brains take prompt + budget from `claude`). Two layered
mechanisms behind the hard cap, both gated on `[brain.native] turn_budget_nudge`
(default on) + a set `max_turns` + a **tool-bearing** task (empty `allowed_tools`,
e.g. the sleep cycle, is untouched):

- **(B) Threshold reminder — the primary mechanism.** As the run nears the cap
  the loop injects an environment notice so the budget surfaces only when
  actionable (short/common tasks never see it — zero anchoring, zero overhead).
  `_pick_turn_budget_nudge(turns, max_turns, early_percent, remaining_levels,
  fired)` counts assistant turns from the loop's `new_messages` accumulator
  (monotonic across compaction — matches `_max_turns_stop` exactly, so a
  threshold never re-fires after a context shrink), and returns the most urgent
  *unfired* crossed threshold. It fires **once** at `turn_budget_nudge_early_percent`
  of the cap (a ~halfway "keep it in mind" reminder), then **once each** as
  absolute steps-remaining crosses each value in `turn_budget_nudge_remaining`
  (default `[15, 5]`, escalating urgency). Each threshold fires at most once
  (`fired` set); when several cross on the same turn (a tiny cap) the most urgent
  wins and the overtaken ones are marked fired so they can't fire stale later.
  `_turn_budget_nudge_message(remaining, phase)` frames the notice as a
  **shrinking** resource ("~N steps remaining", anchoring-resistant), leading with
  absolute remaining, never an upfront allotment.
- **(A) Upfront pacing line — optional flavoring, NON-numeric.**
  `_extract_system_prompt` appends one non-numeric line to the coding-system-prompt
  block ("produce the best deliverable you can rather than leaving the work
  mid-stream") when the nudge is on + tools present + a cap is set. Stating the
  numeric cap up front would anchor it as a target and *compound* the sprawl on
  the exact tasks that hit the cap, so the line carries no number. Compaction-safe
  (the system prompt lives outside `ctx.messages`).

**Injection mechanism.** The nudge rides the `prepare_next_turn` closure (which
already receives `(ctx, new_messages)` every turn — no new loop API). The
threshold logic lives in the `_next_budget_nudge` helper so it doesn't tangle with
the compaction path; the closure combines them (nudge appends to the compacted
list, or to a copy of `ctx.messages` on a non-compaction turn). Injecting via the
returned `PrepareNextTurnResult(messages=…)` puts the notice in `ctx.messages`
only — **not** `new_messages` — so it's invisible to the execution trace and the
turn count, purely model-facing, exactly like the compaction-summary injection.

**Wire role.** The notice is wire-role *user* (the LLM layer has no
mid-conversation system role, and Anthropic rejects one). The `_TURN_BUDGET_FRAME`
carries the "environment metadata, not a new user instruction" semantics
explicitly ("Automatic system notice — not from the user: …") — the mirror of the
`_STEER_FRAME`'s "the user sent this" framing. Between thresholds a compaction may
fold a prior notice into the summary; the count-from-`new_messages` +
fire-each-threshold-once design keeps re-fire correct, and the gap is bounded
until the next threshold. The layered posture: optional non-numeric turn-1 line →
threshold nudge (~50% / ≤15 / ≤5) → hard `max_turns` cap → unmasked `stop_reason`
+ marker (defects 1–2).

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
(`parse_transcript`, settled via `_transcript_has_final_turn`). When the
payload omits the message, `parse_transcript` synthesizes the answer from the
last `end_turn` turn, falling back to the last text-bearing turn **that issued
no tool calls** — a turn that went on to call a tool was narrating, and
promoting that is ISSUE-211. The host needs
`tmux` on `PATH` (a missing binary → `not_found` → headless fallback); the Docker
image installs it.

**Production hardening** (`Specs/Done/claude-tmux-production-readiness.md`):

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
- **Provider-error classification parity (ISSUE-212).** `_build_result` runs the
  transcript body through the shared `_success_frame_stop_reason`, not just
  `is_usage_limit_banner` — on the subscription brain a capacity banner
  delivered as the final assistant message is exactly what the fallback exists
  to absorb, and left alone it was handed to the user verbatim as the answer.
  `_wait_for_completion`'s error branch likewise returns
  `stop_reason="transient_api_error"` when its pane match is retryable, instead
  of a bare `error` that matched no fallback trigger and dead-ended once the
  in-brain session retries were spent.
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

## Task Event Streaming

One persistent, typed event stream per task feeds every output surface. The executor adapts the brain's (widened) `StreamEvent` union into `TaskEvent`s via an `EventWriter` (`events.py`), which persists them to the `task_events` table (WAL, shared scheduler ⇄ web) and notifies in-process subscribers. Event kinds: `task_started`, `tool_start`, `tool_end` (NativeBrain only — carries loop-measured `duration_ms`), `tool_progress` (NativeBrain, SSE only), `progress_text`, `text_delta` (stream surfaces only — incremental answer text coalesced by the executor at ~250ms/120char/boundary; pruned on the terminal path once the canonical `result` lands, so steady state retains zero. **Narration gate (a substance classifier):** a text run streams nothing until it crosses `scheduler.stream_text_gate_chars` (default 280) without an intervening tool call. At a tool boundary the two cases split (`executor._settle_deltas_at_tool_boundary`): a short lead-in ("Let me check…") that stayed under the gate is _dropped_ (it never streamed, so it can't flash in the answer area); a SUBSTANTIAL block that crossed the gate is _kept_ — its unflushed tail is flushed so the full block reaches the stream surface, where the web client renders it as its own prose block (analysis the model wrote, then acted on, is content — not throwaway narration). The gate is thus not an answer-vs-narration split: the final answer (after the last tool) always streams, and a short _final_ answer that never crosses the gate still arrives whole via `result`. The earlier 250ms-timer flush _raced_ the tool boundary and leaked narration permanently; the gate has no time-flush while held. Tune against the `stream_gate:` logs the executor emits per flush/discard. `0` disables the gate), `context_management`, `brain_fallback` (emitted by `execute_task` at the moment it reroutes to the fallback brain, *before* that brain runs — a fallback used to be silent on every stream surface, so a task sat on its `task_started` ack verb for as long as the primary's failure plus the whole fallback run took, and a dropped non-portable pin meant the visible answer came from a different model than the room was configured for with nothing saying so until `done` (ISSUE-278). Payload carries `primary` / `reason` / `fallback` / `model` / `dropped_pin` plus a rendered `text`; the sentence is composed once in `executor.fallback_notice_text` so the web transcript and the REPL cannot word it differently. `model` is what the fallback was *asked* for and is empty exactly when `dropped_pin` is set — the model that actually ran is not known until the run returns, which is what `done` carries. The reroute is treated as a stream boundary like a tool call: `_flush_thinking` + `_settle_deltas_at_tool_boundary` run before the emit, or the failed brain's unflushed tail opens the fallback's answer. **Live-only:** the row survives pruning and an SSE resume, but history rebuilds a finished turn from `execution_trace`, so a reload shows no notice — the durable record of a model substitution stays the italic `_append_model_note` line on a dropped pin), `confirmation`, `result`, `error`, `cancelled`, `done`. Consumers: `TalkEventSubscriber` (edits the ack message in place), `LogChannelSubscriber` (accumulating edit), `PushNotificationSubscriber` (ntfy on long tasks) are in-process subscribers; the web SSE endpoint (`/istota/api/chat/tasks/{id}/stream`), the snapshot endpoint (`…/events`), and the admin endpoint (`/api/admin/tasks/{id}/events`) poll the table directly — the table is the bus, no IPC. **Retry continuity:** the event log is kept across retry-eligible failures (it is _not_ wiped). `set_task_pending_retry` leaves the rows in place, the retry branch emits a `progress_text` "⏳ Attempt failed — retrying in N min…" notice, and the next attempt's `EventWriter` resumes `seq` from `db.get_max_task_event_seq` so it stays monotonic (no UNIQUE(task_id, seq) collision) and a watching web client's resume cursor stays valid — it sees the notice and the next attempt's events instead of a silent spinner. The live view therefore accumulates across attempts (attempt 1's tools, the notice, attempt 2's tools); history reconstruction is unaffected (it reads `execution_trace`, the final attempt's). **Terminal backstop:** the SSE + snapshot endpoints (`web_app._synthetic_terminal_events`) synthesize a terminal frame from the task row — numbered above the client's cursor — whenever a task is terminal in the DB but has no `done` deliverable to that client (a crash that skipped `finish()`, or any future log-reset path). A terminal task always yields a terminal frame. `seq` is monotonic per task, assigned by the writer; events are hand-deleted only in `cleanup_old_tasks` (the `ON DELETE CASCADE` clause is decorative — `PRAGMA foreign_keys` is unset). The brain owns dispatching the executor callback off any event loop (NativeBrain's `run_in_executor` hop), keeping the synchronous subscribers' `asyncio.run` calls safe (ISSUE-111 generalized). Config under `[scheduler]`: `progress_show_tool_use`, `progress_show_text`, `event_log_enabled`, `stream_text_gate_chars`, `push_notification_threshold_seconds`, `push_notification_sources`.
