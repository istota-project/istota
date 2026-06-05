"""NativeBrain — istota's own agent loop behind the Brain protocol.

The executor calls ``brain.execute(req)`` and gets a ``BrainResult`` back, exactly
as it does for ``ClaudeCodeBrain``. It doesn't know which brain is behind the
protocol. Internally ``NativeBrain`` runs the three-layer stack — provider
(``istota.llm``) → generic loop (``istota.agent``) → session management (this
module + ``istota.session``).

What the executor still owns and ``NativeBrain`` consumes: the fully-composed
prompt (``req.prompt`` → the user message), the optional system-prompt file, the
per-task env, the cwd, and the cancel check. What it ignores: ``sandbox_wrap``
(each tool sandboxes its own subprocess per-execution), ``on_pid`` and
``result_file`` (subprocess concerns).

Wired here: compaction via the loop's ``prepare_next_turn`` hook, output-aware
loop detection + a max-turns cap as composable stop conditions, transient-error
retry at the provider boundary, orphan tool-pair repair at the converter
boundary, and per-task cost/token telemetry attached to ``BrainResult.usage``.

Model-namespace resolution (``resolve_alias`` etc.) delegates to a
``ClaudeCodeBrain`` instance: the native brain targets Anthropic models, so it
shares the same alias table rather than maintaining a parallel one.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from istota.agent.events import AgentEvent, _describe_tool_use
from istota.agent.loop import run_agent_loop
from istota.agent.sanitize import sanitize_tool_pairs
from istota.agent.types import (
    AgentContext,
    AgentLoopConfig,
    AgentMessage,
    StopDecision,
)
from istota.llm import make_provider
from istota.llm.catalog import get_model_info
from istota.llm.provider import StreamDone, StreamError, TextDelta, ToolCallDelta
from istota.llm.types import (
    AssistantMessage,
    Message,
    TextContent,
    ToolResultMessage,
    UserMessage,
)
from istota.session.compaction import (
    compact_messages,
    estimate_context_tokens,
    find_cut_point,
    should_compact,
)
from istota.session.loop_detection import detect_repeated_tool_calls
from istota.session.messages import CompactionSummaryMessage
from istota.session.retry import classify_error
from istota.session.usage import TaskUsage

from ._roles import get_role_override, get_role_overrides
from ._types import BrainRequest, BrainResult

logger = logging.getLogger("istota.brain.native")

_API_RETRY_MAX_ATTEMPTS = 3
_API_RETRY_BASE_DELAY = 5.0
_API_RETRY_MAX_DELAY = 120.0


class _RetryingProvider:
    """Wrap a provider with turn-level retry on *immediate* transient errors.

    Transient failures (429 / 5xx / overloaded) surface as a single
    ``StreamError`` before any content delta — the OpenAI-compatible provider
    yields it the moment a non-200 response arrives. We retry only in that
    window: once any text / tool-call delta has been forwarded, the turn is
    committed and a later error passes straight through (a half-streamed turn
    can't be cleanly replayed).

    Putting retry here — at single-completion granularity — keeps it correct for
    multi-turn tasks: re-issuing one request never replays already-executed
    tools. ``abort`` makes the backoff sleep interruptible.
    """

    def __init__(self, inner, abort: asyncio.Event | None):
        self._inner = inner
        self._abort = abort

    async def stream(self, system_prompt, messages, tools, *, model="", max_tokens=16384):
        attempt = 0
        while True:
            committed = False
            pending_error: StreamError | None = None

            async for event in self._inner.stream(
                system_prompt, messages, tools, model=model, max_tokens=max_tokens
            ):
                if isinstance(event, StreamError) and not committed:
                    pending_error = event
                    break
                # Only content-bearing events commit the turn. A StreamStart (or
                # any other zero-payload event a provider might emit) is forwarded
                # without committing, so a StreamError arriving *after* a start —
                # but before any real delta — is still retryable. Committing on
                # StreamStart would silently defeat transient-error retry for any
                # provider that announces the stream before failing.
                if isinstance(event, (TextDelta, ToolCallDelta, StreamDone)):
                    committed = True
                yield event

            if pending_error is None:
                return

            cls = classify_error(pending_error.message.error_message or "")
            if not cls.retryable or attempt >= _API_RETRY_MAX_ATTEMPTS:
                yield pending_error
                return

            attempt += 1
            delay = min(_API_RETRY_BASE_DELAY * (2 ** (attempt - 1)), _API_RETRY_MAX_DELAY)
            logger.warning(
                "native provider transient error (attempt %d/%d), waiting %.1fs: %s",
                attempt,
                _API_RETRY_MAX_ATTEMPTS,
                delay,
                (pending_error.message.error_message or "")[:200],
            )
            if self._abort is not None:
                try:
                    await asyncio.wait_for(self._abort.wait(), timeout=delay)
                    yield pending_error  # aborted during backoff
                    return
                except asyncio.TimeoutError:
                    pass
            else:
                await asyncio.sleep(delay)
            # loop to retry


class NativeBrain:
    """Istota-owned agent loop, provider-agnostic, behind the Brain protocol."""

    def __init__(self, config, provider=None):
        self._config = config
        # ``provider`` injectable for tests; production builds from config.
        self._provider = provider if provider is not None else make_provider(config)

    # --- Model resolution --------------------------------------------------
    #
    # The only provider is ``openai_compat``, which can point at any endpoint
    # (Anthropic, OpenRouter, a local qwen, …). Anthropic aliases must NOT be
    # translated — sending "claude-opus-4-8" to a qwen endpoint would fail — so
    # explicit ids pass through and only operator [models.roles] overrides
    # resolve.

    def resolve_alias(self, alias):
        target = get_role_override(alias) if alias else None
        return (target, None) if target else None

    def resolve_model_name(self, name):
        if not name:
            return ""
        resolved = self.resolve_alias(name)
        if resolved is not None and resolved[0]:
            return resolved[0]
        return name  # explicit id pass-through; no Anthropic translation

    def list_aliases(self):
        return [
            (role, target, None) for role, target in get_role_overrides().items()
        ]

    def validate_role_override(self, role, target):
        # No alias table to validate against for an arbitrary endpoint.
        return []

    # --- Execution ---------------------------------------------------------

    def execute(self, req: BrainRequest) -> BrainResult:
        """Sync wrapper: run the async loop on a fresh event loop.

        The scheduler calls brains from a thread pool, so ``asyncio.run`` here is
        safe — each task gets its own loop.
        """
        try:
            return asyncio.run(self._execute_async(req))
        except Exception as e:  # noqa: BLE001 — never let the brain crash the worker
            logger.exception("NativeBrain.execute raised")
            return BrainResult(success=False, result_text=f"Execution error: {e}", stop_reason="error")

    async def _execute_async(self, req: BrainRequest) -> BrainResult:
        abort = asyncio.Event()
        cancel_task: asyncio.Task | None = None
        if req.cancel_check is not None:
            # Bridge the scheduler's polling cancel_check into the event the
            # loop, tools, and provider backoff all wait on. A cancel_check
            # failure (e.g. transient SQLite lock contention) must not abort the
            # run — treat it as "not cancelled" and let the poller keep trying.
            try:
                already_cancelled = req.cancel_check()
            except Exception:  # noqa: BLE001 — see above
                logger.debug("initial cancel_check raised; ignoring", exc_info=True)
                already_cancelled = False
            if already_cancelled:
                abort.set()
            else:
                cancel_task = asyncio.create_task(self._poll_cancel(req.cancel_check, abort))

        model = req.model or self._config.model
        provider = _RetryingProvider(self._provider, abort)
        usage = TaskUsage()

        # --- event accumulation -------------------------------------------
        trace: list[dict] = []
        actions: list[str] = []
        last_assistant_text = ""
        last_error_message = ""

        async def emit(event: AgentEvent) -> None:
            nonlocal last_assistant_text, last_error_message
            if event.type == "tool_execution_start":
                desc = _describe_tool_use(event.tool_name, event.args)
                trace.append({"type": "tool", "text": desc})
                actions.append(desc)
                await self._emit_progress(req, _tool_use_event(event.tool_name, desc))
            elif event.type == "turn_end":
                msg = event.message
                if isinstance(msg, AssistantMessage):
                    # Capture the provider's error text so _build_result can
                    # surface it; the scheduler only sees result_text, and an
                    # empty error reads as a generic failure (and a policy
                    # refusal would be retried instead of failed-fast).
                    if msg.stop_reason == "error" and msg.error_message:
                        last_error_message = msg.error_message
                    if msg.usage.total_tokens > 0:
                        usage.add(msg.usage, get_model_info(model))
                    text = msg.text.strip()
                    if text:
                        trace.append({"type": "text", "text": text})
                        last_assistant_text = msg.text
                        await self._emit_progress(req, _text_event(msg.text))

        # --- compaction via prepare_next_turn -----------------------------
        compaction_state = {"summary": None, "details": None}

        async def prepare_next_turn(ctx: AgentContext, new_messages):
            info = get_model_info(model)
            window = self._config.context_window or info.context_window
            tokens, _ = estimate_context_tokens(ctx.messages)
            if not should_compact(tokens, window):
                return None
            cut = find_cut_point(ctx.messages)
            if cut == 0:
                return None
            to_compact = ctx.messages[:cut]
            remaining = ctx.messages[cut:]
            summary, details = await compact_messages(
                to_compact,
                compaction_state["summary"],
                compaction_state["details"],
                self._provider,
                model,
                self._convert_to_llm,
            )
            compaction_state["summary"] = summary
            compaction_state["details"] = details
            summary_msg = CompactionSummaryMessage(
                summary=summary, tokens_before=tokens, details=details
            )
            from istota.agent.types import PrepareNextTurnResult

            return PrepareNextTurnResult(messages=[summary_msg, *remaining])

        # --- stop conditions ----------------------------------------------
        max_turns = self._config.max_turns

        async def _max_turns_stop(ctx, new_messages) -> StopDecision:
            turns = sum(1 for m in new_messages if isinstance(m, AssistantMessage))
            if max_turns and turns >= max_turns:
                return StopDecision(stop=True, reason="max_turns")
            return StopDecision(stop=False)

        async def _loop_detect_stop(ctx, new_messages) -> StopDecision:
            if detect_repeated_tool_calls(ctx.messages) is not None:
                return StopDecision(stop=True, reason="loop_detected")
            return StopDecision(stop=False)

        loop_config = AgentLoopConfig(
            provider=provider,
            model=model,
            convert_to_llm=self._convert_to_llm,
            prepare_next_turn=prepare_next_turn,
            stop_conditions=[_max_turns_stop, _loop_detect_stop],
            tool_execution="sequential",
            max_tokens=self._config.max_tokens,
            abort=abort,
        )

        context = AgentContext(
            system_prompt=self._extract_system_prompt(req),
            messages=[],
            tools=self._build_tools(req),
        )
        prompt_msg = UserMessage(content=[TextContent(text=req.prompt)])

        # The loop captures agent_end's stop_reason; we sniff it from the final
        # event by subscribing through a wrapper sink.
        final_stop = {"reason": ""}

        async def emit_wrapped(event: AgentEvent) -> None:
            if event.type == "agent_end":
                final_stop["reason"] = event.stop_reason
            await emit(event)

        # Run the loop under a wall-clock deadline. Without one, a runaway model
        # or a slow provider could run far past the task timeout; the scheduler
        # would then reclaim the "stuck" task and a second worker would execute
        # it concurrently (duplicate output + duplicate deferred-op replay).
        # On timeout we set ``abort`` first so tools/provider unwind cleanly —
        # the bash tool polls abort and kills its subprocess — then give a short
        # grace period before hard-cancelling.
        loop_task = asyncio.create_task(
            run_agent_loop([prompt_msg], context, loop_config, emit_wrapped)
        )
        timed_out = False
        try:
            if req.timeout_seconds and req.timeout_seconds > 0:
                try:
                    await asyncio.wait_for(
                        asyncio.shield(loop_task), timeout=req.timeout_seconds
                    )
                except asyncio.TimeoutError:
                    timed_out = True
                    abort.set()
                    try:
                        await asyncio.wait_for(asyncio.shield(loop_task), timeout=10)
                    except asyncio.TimeoutError:
                        loop_task.cancel()
                        try:
                            await loop_task
                        except (asyncio.CancelledError, Exception):  # noqa: BLE001
                            pass
            else:
                await loop_task
        finally:
            abort.set()
            if cancel_task is not None:
                cancel_task.cancel()
                # Await the cancellation so the loop doesn't log a
                # "Task was destroyed but it is pending" warning on teardown.
                try:
                    await cancel_task
                except asyncio.CancelledError:
                    pass

        if timed_out:
            timeout_min = req.timeout_seconds // 60
            return BrainResult(
                success=False,
                result_text=f"Task execution timed out after {timeout_min} minutes",
                actions_taken=json.dumps(actions) if actions else None,
                execution_trace=json.dumps(trace) if trace else None,
                stop_reason="timeout",
                usage=usage,
            )

        return self._build_result(
            final_stop["reason"], last_assistant_text, last_error_message, trace, actions, usage
        )

    # --- helpers -----------------------------------------------------------

    @staticmethod
    async def _poll_cancel(cancel_check, abort: asyncio.Event) -> None:
        try:
            while not abort.is_set():
                try:
                    if cancel_check():
                        abort.set()
                        return
                except Exception:  # noqa: BLE001
                    # A transient cancel_check failure (e.g. SQLite lock
                    # contention) must not kill the poller — that would
                    # permanently disable !stop for the rest of the run. Log and
                    # keep polling.
                    logger.debug("cancel_check raised; will retry", exc_info=True)
                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

    def _build_tools(self, req: BrainRequest):
        """Default tools bound to a per-task ToolEnv, filtered by allowed_tools.

        Empty ``allowed_tools`` means a text-only invocation (e.g. sleep cycle) —
        no tools are exposed.
        """
        if not req.allowed_tools:
            return []
        from istota.session.tools import ToolEnv, build_default_tools

        env = ToolEnv(
            cwd=Path(req.cwd),
            sandbox_wrap=req.sandbox_wrap,
            subprocess_env=req.env or None,
            bash_timeout_seconds=max(1, req.timeout_seconds),
        )
        allowed = set(req.allowed_tools)
        return [t for t in build_default_tools(env) if t.schema.name in allowed]

    def _convert_to_llm(self, messages: list[AgentMessage]) -> list[Message]:
        """Render AgentMessages to provider wire format, then repair tool pairs.

        Real LLM messages pass through; a ``CompactionSummaryMessage`` becomes a
        user-role note; unknown custom messages are dropped. ``sanitize_tool_pairs``
        then synthesizes results for any orphaned tool_call (and drops stray
        results) so a resumed / compacted context never 400s.
        """
        rendered: list[Message] = []
        for msg in messages:
            if isinstance(msg, (UserMessage, AssistantMessage, ToolResultMessage)):
                rendered.append(msg)
            elif isinstance(msg, CompactionSummaryMessage):
                rendered.append(
                    UserMessage(
                        content=[
                            TextContent(
                                text="[Summary of earlier conversation]\n" + msg.summary
                            )
                        ]
                    )
                )
            # else: unknown custom message — not renderable, skip.
        return sanitize_tool_pairs(rendered)

    @staticmethod
    def _extract_system_prompt(req: BrainRequest) -> str:
        path = req.custom_system_prompt_path
        if path is not None and Path(path).exists():
            return Path(path).read_text()
        return ""

    @staticmethod
    def _build_result(stop_reason, text, error_message, trace, actions, usage) -> BrainResult:
        # Map the loop's agent_end stop_reason to the executor's tag vocabulary.
        # The executor drops stop_reason and the scheduler dispatches purely on
        # result_text string matches (see scheduler.process_one_task), so a
        # cancelled / errored native task MUST carry the same text ClaudeCodeBrain
        # emits — otherwise the scheduler mis-routes it (retries a cancelled task,
        # or retries a policy refusal instead of failing fast with an alert).
        actions_json = json.dumps(actions) if actions else None
        trace_json = json.dumps(trace) if trace else None
        if stop_reason == "aborted":
            return BrainResult(
                success=False,
                result_text="Cancelled by user",
                actions_taken=actions_json,
                execution_trace=trace_json,
                stop_reason="cancelled",
                usage=usage,
            )
        if stop_reason == "error":
            return BrainResult(
                success=False,
                result_text=error_message or text or "Native brain execution error",
                actions_taken=actions_json,
                execution_trace=trace_json,
                stop_reason="error",
                usage=usage,
            )
        # "" (natural), "max_turns", "loop_detected" — all produced output.
        return BrainResult(
            success=True,
            result_text=text,
            actions_taken=actions_json,
            execution_trace=trace_json,
            stop_reason=stop_reason or "completed",
            usage=usage,
        )

    @staticmethod
    async def _emit_progress(req: BrainRequest, event) -> None:
        """Invoke the sync ``on_progress`` callback off the event loop.

        The scheduler's progress callback edits the Talk message by calling
        ``asyncio.run()``. ``emit`` runs inside this brain's own
        ``asyncio.run`` loop, so calling the callback directly would invoke
        ``asyncio.run()`` from a running loop → ``RuntimeError``, silently
        dropping every in-progress update (ISSUE-111). Running it in a
        default-executor thread gives the callback a thread with no running
        loop, so its ``asyncio.run`` works. We ``await`` it so progress edits
        stay ordered with the events that produced them — matching
        ClaudeCodeBrain, which blocks its stream-parse loop on each edit.
        """
        if req.on_progress is None or event is None:
            return
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, req.on_progress, event)
        except Exception:  # noqa: BLE001 — progress is best-effort
            logger.debug("on_progress callback raised", exc_info=True)


def _tool_use_event(tool_name: str, description: str):
    from ._events import ToolUseEvent

    return ToolUseEvent(tool_name=tool_name, description=description)


def _text_event(text: str):
    from ._events import TextEvent

    return TextEvent(text=text)
