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

Model-namespace resolution (``resolve_alias`` etc.) is deliberately minimal: the
sole provider (``openai_compat``) may point at *any* endpoint, so Anthropic
provider aliases (``opus``/``sonnet``/``haiku``) are never translated — an
explicit model id passes through untouched. The three built-in *role* aliases
(``fast``/``general``/``smart``) resolve to the single configured native model
unless the operator remapped them via ``[models.roles]``, so stock config's
``extraction_model``/``curation_model = "general"`` never reaches the wire as a
literal alias string (NB-3).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from pathlib import Path

from istota.agent.events import AgentEvent, _describe_tool_use
from istota.agent.loop import run_agent_loop, run_agent_loop_continue
from istota.agent.sanitize import sanitize_tool_pairs
from istota.agent.types import (
    AgentContext,
    AgentLoopConfig,
    AgentMessage,
    StopDecision,
)
from istota.llm import make_provider
from istota.llm.catalog import get_model_info
from istota.llm.provider import (
    StreamDone,
    StreamError,
    TextDelta,
    ThinkingDelta,
    ToolCallDelta,
)
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

# Built-in role aliases (provider-agnostic convention, mirrors claude_code's
# DEFAULT_ROLE_TARGETS keys). On the native brain these all resolve to the one
# configured endpoint model unless the operator remapped them via [models.roles]
# — so stock config's extraction_model/curation_model="general" never reaches
# the wire as the literal string "general" (NB-3). Ordered for a stable
# list_aliases() table.
_BUILTIN_ROLE_NAMES = ("fast", "general", "smart")

# Reactive overflow recovery: how many force-compact + continue attempts a single
# task may make before giving up and returning the overflow error. Bounded so a
# genuinely too-large single turn can't thrash compaction forever.
_MAX_OVERFLOW_RECOVERIES = 2

_RECOVERY_NUDGE = "[context was compacted; continue]"

# NB-15: markers appended to a final answer whose last turn ended on a
# non-clean finish reason, so a truncated/filtered response is visibly flagged
# rather than delivered as a complete one. Keyed on the provider's mapped
# stop_reason (see openai_compat._FINISH_REASON_MAP).
_TRUNCATION_MARKERS = {
    "max_tokens": "[truncated: the response hit the output token limit]",
    "content_filter": "[note: the response was cut short by the model provider's content filter]",
}


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

    async def stream(
        self,
        system_prompt,
        messages,
        tools,
        *,
        model="",
        max_tokens=16384,
        reasoning_effort=None,
        render_tool_images=False,
    ):
        attempt = 0
        while True:
            committed = False
            pending_error: StreamError | None = None

            async for event in self._inner.stream(
                system_prompt,
                messages,
                tools,
                model=model,
                max_tokens=max_tokens,
                reasoning_effort=reasoning_effort,
                render_tool_images=render_tool_images,
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
        if not alias:
            return None
        target = get_role_override(alias)
        if target:
            return (target, None)
        # A built-in role alias with no operator override resolves to the single
        # model this endpoint is configured for (NB-3). The native brain speaks
        # to one endpoint with one model, so fast/general/smart all mean "the
        # configured model" unless the operator remapped them via [models.roles].
        # Provider aliases (opus/sonnet/haiku) are NOT roles and still pass
        # through untranslated.
        if alias.lower() in _BUILTIN_ROLE_NAMES and self._config.model:
            return (self._config.model, None)
        return None

    def resolve_model_name(self, name):
        if not name:
            return ""
        resolved = self.resolve_alias(name)
        if resolved is not None and resolved[0]:
            return resolved[0]
        # An unoverridden role with an empty native model must still not reach
        # the wire as the literal "general"/"fast"/"smart" — collapse to the
        # (empty) configured model, which downstream treats as "brain default".
        if name.lower() in _BUILTIN_ROLE_NAMES:
            return self._config.model
        return name  # explicit id pass-through; no Anthropic translation

    def list_aliases(self):
        overrides = get_role_overrides()
        listed: list[tuple[str, str | None, str | None]] = []
        seen: set[str] = set()
        # Built-in roles first: operator override if present, else the native
        # model. So `!models` shows the truthful resolved table on native.
        for role in _BUILTIN_ROLE_NAMES:
            listed.append((role, overrides.get(role) or self._config.model, None))
            seen.add(role)
        # Any custom operator role names beyond the three defaults.
        for role, target in overrides.items():
            if role not in seen:
                listed.append((role, target, None))
        return listed

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
            return BrainResult(
                success=False,
                result_text=f"Execution error: {e}",
                stop_reason="error",
                model_used=req.model or self._config.model,
            )

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

        # Resolve the effort tier and capability-gate it. The compat field
        # (``reasoning_effort``) only makes sense for a reasoning model; sending
        # it to a non-thinking endpoint (a local qwen) would 400. The raw tier
        # passes through — the provider folds xhigh/max → high at the wire.
        effort = (req.effort or self._config.effort or "").strip()
        reasoning_effort: str | None = None
        if effort:
            if get_model_info(model).supports_thinking:
                reasoning_effort = effort
            else:
                logger.debug(
                    "native effort ignored: model=%s does not support thinking", model
                )

        # --- event accumulation -------------------------------------------
        trace: list[dict] = []
        actions: list[str] = []
        last_assistant_text = ""
        last_error_message = ""
        # The final assistant turn's stop_reason, so a truncated (max_tokens) or
        # filtered (content_filter) answer can be marked visible rather than
        # delivered as a clean completion (NB-15).
        last_assistant_stop = {"value": ""}

        # Final-turn suppression (see spec "NativeBrain integration"): every
        # turn_end carries assistant text, and the *last* text-bearing turn's
        # text is exactly what becomes BrainResult.result_text. Emitting it as
        # a TextEvent too would double-render (progress_text + result) on a
        # single-turn task. We hold each turn's text and only emit it once a
        # later text-bearing turn proves it wasn't the final one — so the
        # final turn's text is never forwarded as progress.
        pending_text: dict[str, str | None] = {"value": None}

        # Token-level answer streaming (streaming-web-chat-responses spec): the
        # loop already fans provider TextDeltas out as ``message_update`` events.
        # We forward each as a ``TextDeltaEvent`` so the executor can stream it
        # to a stream surface (web chat / repl). The brain stays surface-agnostic
        # — it always emits both the per-token deltas *and* the intermediate-turn
        # whole-text ``TextEvent``s below. The executor, which alone knows the
        # surface, decides what to keep: on a stream surface it drops the
        # redundant whole-turn TextEvent once deltas have flowed; on a push
        # surface (Talk) it drops the deltas and forwards the TextEvents as
        # progress_text. Final-turn suppression (below) is unconditional either
        # way — the last turn's text becomes the result.

        async def emit(event: AgentEvent) -> None:
            nonlocal last_assistant_text, last_error_message
            if event.type == "message_update":
                ae = event.assistant_event
                if isinstance(ae, TextDelta) and ae.text:
                    await self._emit_progress(req, _text_delta_event(ae.text))
                elif isinstance(ae, ThinkingDelta) and ae.thinking:
                    # Stream reasoning fragments as ThinkingDeltaEvents. The
                    # assembled ThinkingContent in StreamDone stays as-is (message
                    # fidelity, excluded from result_text) — no double count since
                    # the loop breaks on StreamDone and never emits it as progress.
                    await self._emit_progress(
                        req, _thinking_delta_event(ae.thinking)
                    )
            elif event.type == "tool_execution_start":
                desc = _describe_tool_use(event.tool_name, event.args)
                trace.append({"type": "tool", "text": desc})
                actions.append(desc)
                await self._emit_progress(
                    req, _tool_use_event(event.tool_name, desc, event.tool_call_id)
                )
            elif event.type == "tool_execution_end":
                await self._emit_progress(
                    req,
                    _tool_end_event(
                        event.tool_name,
                        event.tool_call_id,
                        not event.is_error,
                        event.duration_ms,
                    ),
                )
            elif event.type == "tool_execution_update":
                if event.update_text:
                    await self._emit_progress(
                        req,
                        _tool_progress_event(
                            event.tool_name, event.tool_call_id, event.update_text
                        ),
                    )
            elif event.type == "turn_end":
                msg = event.message
                if isinstance(msg, AssistantMessage):
                    last_assistant_stop["value"] = msg.stop_reason or ""
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
                        # Flush the previously-held text (now known not to be
                        # final); hold this one. The last text-bearing turn
                        # stays held → suppressed (its text is the result). The
                        # executor dedupes these against streamed deltas
                        # per-surface, so emitting them unconditionally is safe.
                        if pending_text["value"] is not None:
                            await self._emit_progress(
                                req, _text_event(pending_text["value"])
                            )
                        pending_text["value"] = msg.text

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
            reasoning_effort=reasoning_effort,
            render_tool_images=get_model_info(model).supports_vision,
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

        # Run the loop under a wall-clock deadline shared across the initial run
        # and every overflow-recovery continue. Without one, a runaway model or a
        # slow provider could run far past the task timeout; the scheduler would
        # then reclaim the "stuck" task and a second worker would execute it
        # concurrently (duplicate output + duplicate deferred-op replay). On
        # timeout we set ``abort`` first so tools/provider unwind cleanly — the
        # bash tool polls abort and kills its subprocess — then give a short grace
        # period before hard-cancelling.
        deadline = (
            time.monotonic() + req.timeout_seconds
            if req.timeout_seconds and req.timeout_seconds > 0
            else None
        )

        async def _run_loop_once(prompts, ctx) -> tuple[list, bool]:
            """Run one loop pass under the *remaining* shared deadline.

            ``prompts`` non-None → ``run_agent_loop``; None → continue. Returns
            ``(new_messages, timed_out)``. The deadline spans all attempts, so a
            recovery continue gets only the time left, never a fresh budget.
            """
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return [], True
            if prompts is not None:
                coro = run_agent_loop(prompts, ctx, loop_config, emit_wrapped)
            else:
                coro = run_agent_loop_continue(ctx, loop_config, emit_wrapped)
            task = asyncio.create_task(coro)
            if remaining is None:
                return await task, False
            try:
                msgs = await asyncio.wait_for(asyncio.shield(task), timeout=remaining)
                return msgs, False
            except asyncio.TimeoutError:
                abort.set()
                try:
                    msgs = await asyncio.wait_for(asyncio.shield(task), timeout=10)
                    return msgs, True
                except asyncio.TimeoutError:
                    task.cancel()
                    try:
                        return await task, True
                    except (asyncio.CancelledError, Exception):  # noqa: BLE001
                        return [], True

        timed_out = False
        try:
            transcript, timed_out = await _run_loop_once([prompt_msg], context)

            # Reactive overflow recovery: a mid-task context-length error is
            # recoverable — force-compact the accumulated transcript and continue
            # from the summary. The proactive ``prepare_next_turn`` path is the
            # first line of defense; this is the safety net beneath it. Bounded
            # (≤_MAX_OVERFLOW_RECOVERIES) and time-bounded (shares the deadline)
            # so a genuinely too-large turn can't thrash forever.
            recoveries = 0
            while (
                not timed_out
                and final_stop["reason"] == "error"
                and classify_error(last_error_message).is_context_overflow
                and recoveries < _MAX_OVERFLOW_RECOVERIES
            ):
                recoveries += 1
                logger.info(
                    "native overflow recovery %d/%d: compacting and continuing",
                    recoveries,
                    _MAX_OVERFLOW_RECOVERIES,
                )
                recovery_ctx, summary, details = await _build_recovery_context(
                    transcript,
                    context.system_prompt,
                    context.tools,
                    compaction_state["summary"],
                    compaction_state["details"],
                    self._provider,
                    model,
                    self._convert_to_llm,
                )
                compaction_state["summary"] = summary
                compaction_state["details"] = details
                # Clear the error markers so the post-continue re-check sees the
                # continue's own outcome, not the prior overflow.
                final_stop["reason"] = ""
                last_error_message = ""
                _cont, timed_out = await _run_loop_once(None, recovery_ctx)
                # continue mutates recovery_ctx.messages to the full transcript.
                transcript = recovery_ctx.messages
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
            # In ``finally`` so the per-task cache footer is logged at task end
            # even if the recovery body raises a non-overflow exception.
            _log_cache_telemetry(usage)

        if timed_out:
            timeout_min = req.timeout_seconds // 60
            return BrainResult(
                success=False,
                result_text=f"Task execution timed out after {timeout_min} minutes",
                actions_taken=json.dumps(actions) if actions else None,
                execution_trace=json.dumps(trace) if trace else None,
                stop_reason="timeout",
                usage=usage,
                model_used=model,
            )

        # NB-15: a final answer the model was forced to cut short (output token
        # cap) or that the endpoint's content filter clipped is delivered with a
        # visible marker instead of masquerading as a complete response.
        result_text = last_assistant_text
        if not final_stop["reason"] and result_text:
            marker = _TRUNCATION_MARKERS.get(last_assistant_stop["value"])
            if marker:
                result_text = f"{result_text}\n\n{marker}"

        return self._build_result(
            final_stop["reason"], result_text, last_error_message,
            trace, actions, usage, model,
        )

    # --- helpers -----------------------------------------------------------

    @staticmethod
    async def _poll_cancel(cancel_check, abort: asyncio.Event) -> None:
        try:
            while not abort.is_set():
                try:
                    # cancel_check is a *synchronous* DB read (open + query with a
                    # 30s busy timeout). Run it off the event loop so SQLite lock
                    # contention can't freeze the whole loop — streaming, tool
                    # execution, progress emission, and the wall-clock deadline
                    # timer all share it (NB-9; same off-loop discipline as
                    # _emit_progress's run_in_executor hop).
                    cancelled = await asyncio.to_thread(cancel_check)
                    if cancelled:
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

        # Filesystem confinement (NB-1): when the executor supplies file-access
        # roots, the in-process file tools are confined to them (the native
        # stand-in for bwrap's filesystem isolation). Relative paths then resolve
        # under the user's own writable dir rather than the shared temp root.
        read_roots = tuple(req.fs_read_roots) if req.fs_read_roots else None
        write_roots = tuple(req.fs_write_roots) if req.fs_write_roots else None
        cwd = write_roots[0] if write_roots else Path(req.cwd)

        env = ToolEnv(
            cwd=cwd,
            sandbox_wrap=req.sandbox_wrap,
            subprocess_env=req.env or None,
            bash_timeout_seconds=max(1, req.timeout_seconds),
            read_roots=read_roots,
            write_roots=write_roots,
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
    def _build_result(
        stop_reason, text, error_message, trace, actions, usage, model="",
    ) -> BrainResult:
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
                model_used=model,
            )
        if stop_reason == "error":
            return BrainResult(
                success=False,
                result_text=error_message or text or "Native brain execution error",
                actions_taken=actions_json,
                execution_trace=trace_json,
                stop_reason="error",
                usage=usage,
                model_used=model,
            )
        # "" (natural), "max_turns", "loop_detected" — all produced output.
        return BrainResult(
            success=True,
            result_text=text,
            actions_taken=actions_json,
            execution_trace=trace_json,
            stop_reason=stop_reason or "completed",
            usage=usage,
            model_used=model,
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


def _aggressive_cut(transcript: list) -> int:
    """Cut index for the cut==0 overflow fallback: keep only the last
    user/tool-result tail.

    Walks back to the most recent ``UserMessage`` / ``ToolResultMessage`` and
    compacts everything before it, guaranteeing forward progress when
    ``find_cut_point`` declined to cut. With no user/tool-result anchor, the
    whole transcript is compacted (``len(transcript)``) so only the summary
    survives.

    The kept tail never starts on a ``ToolResultMessage`` (that would strand the
    result from its owning tool_call, which ``sanitize_tool_pairs`` then drops
    silently — losing the very output recovery meant to preserve). Mirrors
    ``find_cut_point``: advance forward past leading tool_results so the orphan
    lands in the compacted prefix with its call; if that runs off the end (the
    anchor was a trailing result with no newer message), back up so the owning
    assistant message is kept instead.
    """
    anchor = len(transcript)
    for i in range(len(transcript) - 1, -1, -1):
        if isinstance(transcript[i], (UserMessage, ToolResultMessage)):
            anchor = i
            break
    if anchor == len(transcript):
        return anchor  # no anchor — compact everything, only the summary survives

    advanced = anchor
    while advanced < len(transcript) and isinstance(transcript[advanced], ToolResultMessage):
        advanced += 1
    if advanced < len(transcript):
        return advanced
    back = anchor
    while back > 0 and isinstance(transcript[back], ToolResultMessage):
        back -= 1
    return back


async def _build_recovery_context(
    transcript: list,
    system_prompt: str,
    tools,
    prev_summary: str | None,
    prev_details,
    provider,
    model: str,
    convert_to_llm,
) -> tuple[AgentContext, str, object]:
    """Force-compact ``transcript`` and return a context ready for continue.

    Ignores ``should_compact`` (this is the reactive safety net — the window was
    already exceeded). Falls back to ``_aggressive_cut`` when ``find_cut_point``
    returns 0 so a turn is always reclaimed. Appends a synthetic user nudge when
    the compacted tail ends on an assistant message, because
    ``run_agent_loop_continue`` refuses to continue from one.
    """
    cut = find_cut_point(transcript)
    if cut == 0:
        cut = _aggressive_cut(transcript)
    to_compact = transcript[:cut]
    remaining = transcript[cut:]
    summary, details = await compact_messages(
        to_compact, prev_summary, prev_details, provider, model, convert_to_llm
    )
    messages: list = [
        CompactionSummaryMessage(summary=summary, tokens_before=0, details=details),
        *remaining,
    ]
    if messages and getattr(messages[-1], "role", None) == "assistant":
        messages.append(UserMessage(content=[TextContent(text=_RECOVERY_NUDGE)]))
    ctx = AgentContext(system_prompt=system_prompt, messages=messages, tools=tools)
    return ctx, summary, details


def _log_cache_telemetry(usage: TaskUsage) -> None:
    """Log the cumulative cross-turn cache-hit rate at task end (Stage 5b).

    ``hit_rate`` is ``cache_read_tokens / input_tokens`` as a percentage. Under
    OpenAI-compat semantics (the sole transport) ``prompt_tokens`` already
    includes ``cached_tokens``, so the read count is a subset of the input and
    the ratio is bounded in [0, 100]. A non-conforming provider that reports
    cache reads *outside* ``prompt_tokens`` could push it past 100%, so the
    value is clamped defensively. With no input recorded the ratio is reported
    as 0% (no divide-by-zero). Mirrors pi's per-task cache footer so Stage 2's
    caching can be validated against production data.
    """
    read = usage.cache_read_tokens
    inp = usage.input_tokens
    rate = min(100.0, read / inp * 100.0) if inp else 0.0
    logger.info(
        "native cache hit_rate=%.1f%% read=%d input=%d write=%d",
        rate,
        read,
        inp,
        usage.cache_write_tokens,
    )


def _tool_use_event(tool_name: str, description: str, tool_call_id: str = ""):
    from ._events import ToolUseEvent

    return ToolUseEvent(
        tool_name=tool_name, description=description, tool_call_id=tool_call_id
    )


def _tool_end_event(tool_name: str, tool_call_id: str, success: bool, duration_ms: int):
    from ._events import ToolEndEvent

    return ToolEndEvent(
        tool_name=tool_name,
        tool_call_id=tool_call_id,
        success=success,
        duration_ms=duration_ms,
    )


def _tool_progress_event(tool_name: str, tool_call_id: str, text: str):
    from ._events import ToolProgressEvent

    return ToolProgressEvent(
        tool_name=tool_name, tool_call_id=tool_call_id, text=text
    )


def _text_event(text: str):
    from ._events import TextEvent

    return TextEvent(text=text)


def _text_delta_event(text: str):
    from ._events import TextDeltaEvent

    return TextDeltaEvent(text=text)


def _thinking_delta_event(thinking: str):
    from ._events import ThinkingDeltaEvent

    return ThinkingDeltaEvent(thinking=thinking)
