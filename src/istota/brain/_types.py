"""Brain protocol — the boundary between executor orchestration and model invocation.

The executor composes the prompt, the env and the sandbox configuration and
hands a BrainRequest to the brain. The brain owns everything from "we have a
prompt and an env" through "we have a result + trace": building the model call,
running it, parsing streaming events, and retrying on transient API errors.
Result post-processing (malformed-output detection, CM-aware composition) and
deferred file processing stay in the executor.

The prompt is not one string. It reaches the brain on three channels with
different authority — the task material as ``prompt``, Istota's own standing
instructions as ``composed_system_prompt_path``, and the operator's file as
``custom_system_prompt_path`` — so that a brain which compacts its own message
history cannot compact away the instructions that define the assistant.
``BrainRequest`` documents each one. A direct text-only caller supplies only
the first and is unaffected.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ._events import StreamEvent

if TYPE_CHECKING:
    from istota.usage import BrainUsage


@dataclass(frozen=True)
class ImageInput:
    """One prepared image attachment, ready for whichever vision path applies.

    `path` is the normalized file the model is allowed to read, always
    `Path.resolve()`d: `build_bwrap_cmd`'s `_bind` uses the resolved source as
    the in-namespace destination, so on a deployment whose `temp_dir` sits
    behind a symlink an unresolved path names a file that does not exist inside
    the sandbox.

    `media_type` is derived from the decoded Pillow format and the format the
    rewrite chose — never from the sender's extension or claimed MIME type —
    and is always one of `image/png`, `image/jpeg` or `image/webp`, which every
    provider this deployment reaches accepts.

    `display_name` is the basename shown to the model. No image bytes and no
    OCR text travel on this record: each brain reads the file itself, at the
    last moment, so nothing large lands in a task row or a log line.
    """

    path: Path
    media_type: str
    display_name: str


@dataclass
class BrainRequest:
    """Inputs the brain needs to execute one task attempt.

    Three text channels reach the model, and they are distinct on purpose:

    - ``prompt`` — the task material, sent as the model's user turn (stdin on
      the headless CLI path, the pane injection on tmux, the initial
      ``UserMessage`` on the native path). Native compaction may summarize it.
    - ``composed_system_prompt_path`` — Istota's own standing instructions,
      composed by the executor and sent with *system* authority, outside
      anything compaction can reach.
    - ``custom_system_prompt_path`` — the operator's own file, with each
      backend's existing override semantics preserved.
    """

    # The user turn. When ``composed_system_prompt_path`` is set, Istota's
    # standing instructions travel there instead of being prefixed onto this,
    # so what remains here is task material: retrieved memory, conversation and
    # confirmation history, the request itself and its attachments.
    prompt: str
    allowed_tools: list[str]
    cwd: Path
    env: dict[str, str]
    timeout_seconds: int

    # Model selection (empty string = brain default)
    model: str = ""
    effort: str = ""

    # Advisor model (anthropic-namespace brains only; empty = no advisor).
    # A canonical model ID, resolved the same way as `model`. When empty, the
    # brain suppresses the CLI's own `advisorModel` settings-file channel
    # (`CLAUDE_CODE_DISABLE_ADVISOR_TOOL=1`) so a host's `~/.claude/settings.json`
    # can't turn an advisor on behind this field's back. NativeBrain ignores it.
    advisor: str = ""

    # Optional: override system prompt with a file's contents.
    #
    # The *operator's* file (`config/system-prompt.md` under
    # `custom_system_prompt`), and the older of the two system channels. It
    # stays optional in the strict sense: a configured path that no longer
    # exists is omitted rather than failing the attempt, which is the behaviour
    # every deployment has today. Each backend keeps its own override position
    # for it — ClaudeCodeBrain passes `--system-prompt-file`, which *replaces*
    # the CLI's default harness prompt; NativeBrain appends it after its
    # built-in coding block.
    custom_system_prompt_path: Path | None = None

    # Istota's own composed standing instructions, written by the executor to
    # `task_<id>_system_prompt.txt` in the task temp dir. The system half of
    # `executor.ComposedPrompt`: identity, execution constraints, emissaries,
    # persona, accessible resources, tool descriptions, rules, response
    # guidelines, the skills changelog and the eager skill bodies.
    #
    # A third channel rather than a reuse of the field above, because the two
    # have different owners and different contracts:
    #
    # - `None` means this caller has no Istota-composed standing instructions.
    #   That is the default and it is what every direct text-only caller (the
    #   sleep cycle, shared-block synthesis, health OCR and explanation, the
    #   code reviewer, conversation triage) passes, so their behaviour is
    #   unchanged.
    # - A non-`None` value is *required* input. A brain must not quietly ignore
    #   it because the file disappeared: the Claude CLI path emits its flag and
    #   lets the CLI refuse to start, and the native path reads the file with no
    #   `exists()` branch. The existing brain error boundary turns either into
    #   an ordinary failed `BrainResult`. Silent omission would run the task
    #   with the user half alone — no persona, no rules, no tool descriptions —
    #   which is ISSUE-375 recreated by a filesystem race.
    #
    # Carried across a reroute by `_run_fallback`'s `dataclasses.replace`, which
    # names it nowhere, so both prompt channels reach the fallback brain intact
    # and each backend consumes the split its own way.
    #
    # **Must be absolute**, and the producer is what has to guarantee it: a
    # relative value names a different file on each backend and would survive a
    # reroute between them. NativeBrain opens it in the *daemon* process, so it
    # would resolve against the daemon's cwd; the Claude CLI resolves it itself,
    # inside the sandbox, against the task's chdir target — and the tmux path
    # puts another `cd` in front of that. `custom_system_prompt_path` never had
    # the problem because it comes from a resolved config path.
    composed_system_prompt_path: Path | None = None

    # Whether the brain should stream events for progress callbacks. When
    # False, the brain may pick a faster non-streaming path if it has one.
    streaming: bool = True

    # Stream events forwarded to the caller (per ToolUseEvent, TextEvent,
    # and ContextManagementEvent emitted by the brain). The executor wraps
    # this to filter for tool-use vs text and rate-limit Talk progress.
    on_progress: Callable[[StreamEvent], None] | None = None

    # Returns True if the task has been cancelled and execution should stop.
    # Polled between events; the brain kills its subprocess and returns
    # stop_reason="cancelled".
    cancel_check: Callable[[], bool] | None = None

    # Returns any steering messages (raw user text) that have arrived for this
    # task since the last call, marking them consumed (`!steer`). A
    # steering-capable brain (NativeBrain) polls this off its event loop and
    # injects each as a user turn at the next loop boundary; other brains ignore
    # it. ``None`` = no steering channel wired for this task (the common case for
    # non-native / non-steerable brains). The executor supplies it only when the
    # resolved brain ``supports_steering``.
    poll_steers: Callable[[], list[str]] | None = None

    # Called once with the subprocess PID after spawn (for !stop support).
    on_pid: Callable[[int], None] | None = None

    # Per-task cgroup v2 directory (A6), already created with its limits
    # written, or None where the deployment has no delegated subtree.
    #
    # Read by every brain that spawns its own subprocess, which is how the
    # child gets placed *before* it execs — membership is inherited at fork, so
    # the executor's `on_pid` callback arrives too late to catch anything the
    # child forked on the way up, and bwrap forks every time (ISSUE-285).
    # NativeBrain spawns a fresh child per Bash execution and never calls
    # `on_pid` at all; TmuxClaudeBrain has no spawn of its own to hook and stays
    # on the `on_pid` path.
    task_cgroup: Path | None = None

    # Wraps a command list (e.g. for bubblewrap sandboxing). The brain
    # builds its raw command, then calls sandbox_wrap(cmd) before exec
    # if provided. Returning the cmd unchanged is the no-op default.
    #
    # This one is the *Claude* profile: the namespace it builds carries the
    # `claude` CLI's own runtime state — its binary directory, its installed
    # versions, its lock directory, its credential and its session logs —
    # because the process being wrapped is that CLI. ClaudeCodeBrain and
    # TmuxClaudeBrain read it; nothing else should.
    sandbox_wrap: Callable[[list[str]], list[str]] | None = None

    # The same sandbox under the *native* profile: istota's own code is the
    # outer process, so the namespace has no Claude runtime block, no
    # credential and no system-prompt bind (`executor.SandboxProfile`). Read by
    # NativeBrain, for the commands its Bash tool runs.
    #
    # **Two fields rather than one field plus a profile argument**, and the
    # reason is structural rather than stylistic. `executor._run_fallback`
    # copies the request with `dataclasses.replace(req, model=…, effort=…,
    # advisor=…, is_fallback=True)`, which names neither of these — so on the
    # shipped `claude_code -> native` reroute a single wrap field would carry
    # the Claude profile straight into NativeBrain, handing the model's Bash
    # tool the credential the split exists to withhold (ISSUE-389). Two names
    # each carry across harmlessly, because each brain reads only its own.
    native_sandbox_wrap: Callable[[list[str]], list[str]] | None = None

    # Filesystem confinement for in-process file tools (NativeBrain only; NB-1).
    # ClaudeCodeBrain / TmuxClaudeBrain ignore these — their tools run inside
    # bwrap, which already confines the filesystem. NativeBrain's Read/Write/
    # Edit/Grep/Glob run in the daemon process (no bwrap), so the executor
    # passes the same user-data roots bwrap would bind and the file tools
    # enforce them. ``None`` = unconfined (dev / unsandboxed), matching the
    # claude_code path's own posture where bwrap is unavailable.
    fs_read_roots: list[Path] | None = None
    fs_write_roots: list[Path] | None = None
    # RO carve-outs nested inside a write root — bwrap expresses these by
    # re-binding a subdirectory read-only after its parent; containment alone
    # cannot. Empty is the unconfined/no-carve-out case.
    fs_write_denied_roots: list[Path] = field(default_factory=list)

    # The framework DB path, for brains that need a local writable dir of their
    # own (NativeBrain's OpenRouter catalog cache uses its parent). Passed
    # explicitly rather than read out of ``env``: ``env`` is the *sandbox*
    # environment and no longer carries ISTOTA_DB_PATH — that goes to the skill
    # proxy instead, since the model must not hold a path to a database.
    # ``None`` for direct brain callers that build no task env.
    db_path: Path | None = None

    # Which task attempt this request *is*. Daemon-side facts, passed
    # explicitly for the same reason ``db_path`` is: ``env`` is the *sandbox*
    # environment, which carries only what the model may see, and a brain that
    # read its own identity back out of it would be reading a value the task
    # can rewrite. Only NativeBrain uses them today — to name and head the
    # per-attempt session log — and every other brain ignores them.
    #
    # The zero defaults are load-bearing rather than filler: a direct brain
    # call (the sleep cycle, the REPL, a test) has no task behind it, and the
    # session log declines to open a file for one instead of writing a
    # ``task-0-0`` that would collide across every such call. ``_run_fallback``
    # copies the request with ``dataclasses.replace``, which names none of
    # these, so a reroute carries the identity across untouched — which is what
    # files the fallback brain's own log against the same attempt rather than a
    # nameless one, and what leaves ``is_fallback`` below as the only thing in
    # it that names which brain ran.
    task_id: int = 0
    attempt: int = 0
    user_id: str = ""
    source_type: str = ""
    conversation_token: str = ""
    is_group_chat: bool = False

    # Whether this request *is* the fallback run of a rerouted attempt
    # (ISSUE-378). Set by ``executor._run_fallback`` and by nothing else: a
    # reroute deliberately increments neither ``attempt`` nor the task's
    # ``attempt_count``, so nothing a brain writes per attempt says which of
    # the two brains it came from. This is the
    # request-side twin of ``task_usage.is_fallback``, which is what the two
    # spend rows are keyed apart on, and it exists so a native session log can
    # be joined to the right one of them. Every other brain ignores it.
    is_fallback: bool = False

    # ClaudeCodeBrain-specific: optional fallback file the model writes its
    # final result to when stream parsing fails. Other brains may ignore.
    result_file: Path | None = None

    # Optional human-readable session label. TmuxClaudeBrain uses it to name
    # its tmux session (for !stop correlation + log readability); other brains
    # ignore it. Empty = the brain derives a name locally. Additive — the
    # executor may leave it unset.
    session_label: str = ""

    # Prepared image attachments for this task, in sender order
    # (``image_attachments.prepare_image_attachments`` produces them). Empty for
    # every caller that builds a text-only request, which is all of them but the
    # task path.
    #
    # Each brain owns the provider-specific conversion, at the last moment:
    # NativeBrain base64-encodes into initial content blocks, the Claude Code
    # brains name the paths in a mandatory ``Read`` directive. So no image bytes
    # and no base64 ever ride on the request itself — nothing large reaches a
    # task row or a log line, and the executor learns no provider wire format.
    #
    # A fallback copy is ``dataclasses.replace(req, model=…, effort=…,
    # advisor=…)``, which names no other field, so this carries across a reroute
    # untouched and the fallback brain makes its own capability decision.
    images: list[ImageInput] = field(default_factory=list)


@dataclass
class BrainResult:
    """Outputs of one brain.execute() call.

    actions_taken and execution_trace are JSON-encoded strings (or None
    when not applicable) — same shape as the original executor returns,
    so downstream DB writes and post-processing stay unchanged.

    stop_reason is a short tag describing how execution ended; the
    executor uses it to decide retry vs cancel vs fail.
    """

    success: bool
    result_text: str
    actions_taken: str | None = None
    execution_trace: str | None = None
    # completed/cancelled/timeout/oom/transient_api_error/error/not_found,
    # plus "usage_limit" (the brain hit a subscription/quota/billing limit — a
    # persistent "brain unavailable" condition the executor reroutes to the
    # configured fallback brain and which opens the availability breaker) and
    # "fallback" (TmuxClaudeBrain: this attempt couldn't be driven via tmux —
    # the executor reruns it once through the fallback brain within the same
    # attempt).
    stop_reason: str = "completed"

    # Per-attempt token + cost telemetry, normalized across brains
    # (`istota.usage.BrainUsage`). Every brain that can measure sets this on
    # *every* return, success or failure — tokens are spent either way.
    # ClaudeCodeBrain builds it from the CLI's terminal frame plus the
    # per-request `message_delta` frames; NativeBrain converts its `TaskUsage`
    # at the boundary. TmuxClaudeBrain leaves it None: it drives the interactive
    # TUI and reconstructs events from a JSONL transcript, so there is no result
    # frame to read, and a synthetic zero would drag every average.
    usage: "BrainUsage | None" = None

    # Which brain produced this result, for the usage row's `brain_kind`. Set by
    # the brain rather than threaded from the executor's construction site, so
    # it stays correct on the fallback path for free — there the executor's own
    # variable no longer describes the result it holds.
    brain_kind: str = ""

    # The model the brain actually invoked (canonical ID). Each brain sets this
    # to the model it used so the executor can record it on the task row and
    # surface it (e.g. in the web-chat message meta). Empty when the brain can't
    # determine it — the executor then falls back to the requested model.
    model_used: str = ""

    # True when the run reached the model and may have executed tools before
    # failing, so re-running the same prompt would repeat those side effects
    # (a re-sent email, a re-applied edit). Set by the paths that reclassify a
    # *successful* CLI result frame carrying a provider error banner: the CLI
    # ran to completion, so the in-brain retry loop must not re-invoke it —
    # the failure reroutes to the fallback brain instead (ISSUE-212).
    # Default False keeps every existing return site behaving as before.
    work_committed: bool = False

    # What the model had written when a stop that discards the answer fired
    # (ISSUE-372). The `max_turns` / `loop_detected` backstops already deliver
    # the last text-bearing turn under a marker; the two stops a person is most
    # likely to see — a wall-clock timeout and a `!stop` cancel — returned a
    # fixed string and threw 29 minutes of narration away.
    #
    # Deliberately a *separate* field rather than an addition to `result_text`:
    # the executor drops `stop_reason` and the scheduler dispatches on
    # `result_text` by string match, and cancel is matched by exact equality in
    # three places (`scheduler.py`). Appending to `result_text` would send a
    # cancelled task back through the retry ladder. Keeping it separate means
    # every existing match stays byte-identical and the persistence and
    # delivery paths opt in by naming this field.
    #
    # None when the run produced no text at all, which is not the same as "".
    partial_text: str | None = None


class Brain(Protocol):
    """The single boundary every brain implementation satisfies.

    Beyond the actual model call (``execute``), each brain owns its model
    namespace — canonical IDs, provider aliases, and how role aliases like
    ``smart`` map to a real model. Centralizing this on the Brain interface
    means a future OpenRouter / Anthropic-direct backend can ship its own
    naming scheme without any caller changes; consumers always go through
    ``make_brain(config.brain).resolve_*``.
    """

    # The model namespace this brain resolves alias names in. Operators
    # key a per-namespace alias override on this string (``[models.aliases.<name>]``
    # sub-table). ``ClaudeCodeBrain`` / ``TmuxClaudeBrain`` share ``"anthropic"``
    # (same `claude` binary, same Anthropic model IDs); ``NativeBrain`` uses
    # ``"openai_compat"`` (its provider label). Keeping this a namespace *label*
    # rather than the brain *kind* collapses the two CLI brains to one key so an
    # operator writes an ``anthropic`` value once for both.
    model_namespace: str

    def execute(self, req: BrainRequest) -> BrainResult: ...

    @property
    def supports_steering(self) -> bool:
        """Whether this brain can accept a steering message mid-run (`!steer`).

        A steering-capable brain drains ``req.poll_steers`` at its loop
        boundaries and injects each as a user turn without restarting the task.
        The command layer reads this (plus a v1 kind allowlist) to decide whether
        to accept a steer or refuse gracefully. Defaults ``False`` so a brain
        that hasn't opted in is never asked to steer.
        """
        return False

    def resolve_alias(self, alias: str) -> tuple[str | None, str | None] | None:
        """Resolve a ``!model <alias>`` name to ``(model_id, effort)`` or None.

        Accepts an optional orthogonal ``:effort`` modifier on any reference
        (``opus:high``, ``smart:low``, ``claude-opus-5:xhigh``): the base name
        resolves through the brain's alias table (tiers + shortcuts + canonical
        passthrough) and the modifier's effort wins over the entry's own default
        effort. Returns None for an unknown name.
        """
        ...

    def resolve_model_name(self, name: str | None) -> str:
        """Resolve any name (role, provider alias, canonical ID) to a canonical ID.

        Returns ``""`` for empty/None so callers can pass through to the
        brain default. Pass-through for unknown names — keeps backward
        compat for raw model IDs typed directly into config.
        """
        ...

    def list_aliases(self) -> list[tuple[str, str | None, str | None]]:
        """Return the merged alias table (roles + provider aliases) for display."""
        ...

    def validate_alias_override(self, name: str, target: str) -> list[str]:
        """Return human-readable warnings for an operator alias override.

        Called once per ``[models.aliases]`` entry at config-load time so the
        operator sees obvious typos in their logs immediately, rather than
        finding out at runtime when a task fails. Empty list = no warnings.
        Brains that don't care about this can return ``[]``.
        """
        ...


@dataclass
class BrainConfig:
    """Selects which brain to use and shared brain-level knobs.

    Per-brain settings (e.g. OpenRouter API key) live in their own nested
    config blocks added in later phases.
    """

    kind: str = "claude_code"  # "claude_code" (only option in phase 1)
