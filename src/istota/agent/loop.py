"""Core agent loop.

Prior art: Pi's runLoop() (agent/src/agent-loop.ts:155). Double-loop structure:
the inner loop handles tool-call iterations + steering messages, the outer loop
handles follow-up messages.

This module knows nothing about istota's application layer — no skills, no
sandboxing, no Nextcloud. It drives a tool-use loop against an ``LLMProvider``
and emits ``AgentEvent``s. Compaction, model switching, token budgets, and loop
detection all plug in through hooks (``prepare_next_turn``) and composable
``stop_conditions`` — the loop applies their results without understanding them.
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from dataclasses import dataclass, field
from typing import Any

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
    ToolCallContent,
    ToolResultMessage,
    ToolSchema,
)

from .coercion import coerce_arguments
from .events import AgentEvent, AgentEventSink
from .hooks import (
    AfterToolCallContext,
    BeforeToolCallContext,
)
from .tools import AgentTool, ToolResult
from .types import (
    AgentContext,
    AgentLoopConfig,
    AgentMessage,
    QueueMode,
    StopDecision,
)

logger = logging.getLogger("istota.agent.loop")


# --------------------------------------------------------------------------- #
# Public entry points
# --------------------------------------------------------------------------- #


async def run_agent_loop(
    prompts: list[AgentMessage],
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
) -> list[AgentMessage]:
    """Start an agent loop with new prompt messages.

    Returns all new messages produced during the run (the prompts plus every
    assistant / tool-result / injected message the run appended).
    """
    new_messages: list[AgentMessage] = list(prompts)
    ctx = AgentContext(
        system_prompt=context.system_prompt,
        messages=[*context.messages, *prompts],
        tools=context.tools,
    )

    await emit(AgentEvent(type="agent_start"))

    for prompt in prompts:
        await emit(AgentEvent(type="message_start", message=prompt))
        await emit(AgentEvent(type="message_end", message=prompt))

    await _run_loop(ctx, new_messages, config, emit)
    return new_messages


async def run_agent_loop_continue(
    context: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
) -> list[AgentMessage]:
    """Continue from the current context (for retry after compaction).

    Prior art: Pi's agentLoopContinue (agent/src/agent-loop.ts:64). The context
    must not end on an assistant message — there'd be nothing to respond to.
    """
    if not context.messages:
        raise ValueError("Cannot continue: no messages in context")

    last = context.messages[-1]
    if getattr(last, "role", None) == "assistant":
        raise ValueError("Cannot continue from assistant message")

    new_messages: list[AgentMessage] = []
    await emit(AgentEvent(type="agent_start"))
    await _run_loop(context, new_messages, config, emit)
    return new_messages


# --------------------------------------------------------------------------- #
# Main loop
# --------------------------------------------------------------------------- #


async def _run_loop(
    ctx: AgentContext,
    new_messages: list[AgentMessage],
    config: AgentLoopConfig,
    emit: AgentEventSink,
) -> None:
    """Main loop logic shared by run and continue.

    Outer loop drains follow-up messages; inner loop iterates tool calls and
    steering messages. After each ``turn_end`` the loop runs ``prepare_next_turn``
    (compaction / model swap), then evaluates the composable ``stop_conditions``
    (token budget, loop detection, max turns). The ``abort`` event is checked at
    every loop boundary and threaded into tool execution.
    """
    stop_conditions = _resolve_stop_conditions(config)

    pending: list[AgentMessage] = []
    if config.get_steering_messages:
        pending = _drain_queue(config.get_steering_messages(), config.steering_queue_mode)

    while True:  # outer: follow-up loop
        has_more_tool_calls = True

        while has_more_tool_calls or pending:  # inner: tool-call loop
            if config.abort and config.abort.is_set():
                await emit(AgentEvent(type="agent_end", messages=new_messages, stop_reason="aborted"))
                return

            await emit(AgentEvent(type="turn_start"))

            # Inject any pending steering / follow-up messages.
            if pending:
                for msg in pending:
                    await emit(AgentEvent(type="message_start", message=msg))
                    await emit(AgentEvent(type="message_end", message=msg))
                    ctx.messages.append(msg)
                    new_messages.append(msg)
                pending = []

            message = await _stream_assistant_response(ctx, config, emit)
            ctx.messages.append(message)
            new_messages.append(message)

            if message.stop_reason in ("error", "aborted"):
                await emit(AgentEvent(type="turn_end", message=message, tool_results=[]))
                await emit(
                    AgentEvent(
                        type="agent_end",
                        messages=new_messages,
                        stop_reason=message.stop_reason,
                    )
                )
                return

            tool_calls = message.tool_calls
            tool_results: list[ToolResultMessage] = []
            has_more_tool_calls = False

            if tool_calls:
                batch = await _execute_tool_batch(ctx, message, config, emit)
                tool_results = batch.messages
                has_more_tool_calls = not batch.terminate
                for result in tool_results:
                    ctx.messages.append(result)
                    new_messages.append(result)

            await emit(AgentEvent(type="turn_end", message=message, tool_results=tool_results))

            # prepare_next_turn: compaction, model switch, system-prompt swap.
            if config.prepare_next_turn:
                prep = await config.prepare_next_turn(ctx, new_messages)
                if prep:
                    if prep.messages is not None:
                        ctx.messages = prep.messages
                    if prep.model is not None:
                        config.model = prep.model
                    if prep.system_prompt is not None:
                        ctx.system_prompt = prep.system_prompt

            # Composable stop conditions — graceful stop at a clean boundary.
            for cond in stop_conditions:
                decision = await cond(ctx, new_messages)
                if decision.stop:
                    await emit(
                        AgentEvent(
                            type="agent_end",
                            messages=new_messages,
                            stop_reason=decision.reason,
                        )
                    )
                    return

            # Poll for steering messages (respects queue mode).
            if config.get_steering_messages:
                pending = _drain_queue(config.get_steering_messages(), config.steering_queue_mode)

        # Agent would stop — check for follow-ups (respects queue mode).
        if config.get_follow_up_messages:
            follow_ups = _drain_queue(config.get_follow_up_messages(), config.follow_up_queue_mode)
            if follow_ups:
                pending = follow_ups
                continue

        break

    await emit(AgentEvent(type="agent_end", messages=new_messages, stop_reason=""))


def _resolve_stop_conditions(config: AgentLoopConfig):
    """Merge ``should_stop_after_turn`` (back-compat) into ``stop_conditions``."""
    conditions = list(config.stop_conditions)
    hook = config.should_stop_after_turn
    if hook is not None:

        async def _adapter(ctx: AgentContext, msgs: list[AgentMessage]) -> StopDecision:
            stop = await hook(ctx, msgs)
            return StopDecision(stop=bool(stop), reason="should_stop_after_turn")

        conditions.append(_adapter)
    return conditions


def _drain_queue(messages: list[AgentMessage], mode: QueueMode) -> list[AgentMessage]:
    """Drain a message queue according to the queue mode.

    ``"all"`` drains everything; ``"one_at_a_time"`` takes the first message and
    leaves the rest for subsequent turns (prevents queue flooding).
    """
    if not messages:
        return []
    if mode == "one_at_a_time":
        return [messages[0]]
    return messages


# --------------------------------------------------------------------------- #
# Streaming one assistant response
# --------------------------------------------------------------------------- #


async def _stream_assistant_response(
    ctx: AgentContext,
    config: AgentLoopConfig,
    emit: AgentEventSink,
) -> AssistantMessage:
    """Stream one assistant completion, accumulating deltas into a message.

    Converts the running ``AgentMessage`` list to provider wire format via
    ``convert_to_llm`` (which sanitizes tool pairs), drives the provider stream,
    and folds ``StreamDone`` / ``StreamError`` into the final message. Honors the
    ``abort`` event between stream events. The provider never raises — failures
    arrive as ``StreamError``.
    """
    snapshot = list(ctx.messages)
    if config.transform_context:
        snapshot = config.transform_context(snapshot)
    wire_messages: list[Message] = config.convert_to_llm(snapshot)
    tool_schemas: list[ToolSchema] = [t.schema for t in (ctx.tools or [])]

    text_parts: list[str] = []
    # tool-call accumulation keyed by index of arrival
    tool_acc: list[dict] = []

    await emit(AgentEvent(type="message_start", message=None))

    final: AssistantMessage | None = None
    stream = config.provider.stream(
        ctx.system_prompt,
        wire_messages,
        tool_schemas,
        model=config.model,
        max_tokens=config.max_tokens,
        reasoning_effort=config.reasoning_effort,
        render_tool_images=config.render_tool_images,
    )

    async for event in stream:
        if config.abort and config.abort.is_set():
            return AssistantMessage(
                content=[TextContent(text="".join(text_parts))],
                stop_reason="aborted",
                model=config.model,
            )

        if isinstance(event, TextDelta):
            text_parts.append(event.text)
            await emit(AgentEvent(type="message_update", assistant_event=event))
        elif isinstance(event, ToolCallDelta):
            _accumulate_tool_delta(tool_acc, event)
            await emit(AgentEvent(type="message_update", assistant_event=event))
        elif isinstance(event, ThinkingDelta):
            # Reasoning fragment — forwarded so the brain can surface it on stream
            # surfaces. Not folded into text_parts (it must not land in the
            # message content / result_text); the provider assembles the canonical
            # ThinkingContent block from its own accumulation at StreamDone.
            await emit(AgentEvent(type="message_update", assistant_event=event))
        elif isinstance(event, StreamDone):
            final = event.message
            break
        elif isinstance(event, StreamError):
            final = event.message
            break
        # StreamStart and any unknown event types carry no payload to fold.

    if final is None:
        # Stream ended without an explicit done/error — assemble from deltas.
        final = _assemble_message(text_parts, tool_acc, config.model)

    await emit(AgentEvent(type="message_end", message=final))
    return final


def _accumulate_tool_delta(acc: list[dict], event: ToolCallDelta) -> None:
    """Fold a ToolCallDelta into the accumulator.

    Deltas with an ``id`` start a new call; deltas without one append their
    ``arguments_delta`` to the most recent call's argument buffer.
    """
    if event.id:
        acc.append({"id": event.id, "name": event.name, "args": event.arguments_delta or ""})
    elif acc:
        acc[-1]["args"] += event.arguments_delta or ""


def _assemble_message(text_parts: list[str], tool_acc: list[dict], model: str) -> AssistantMessage:
    """Build an AssistantMessage from accumulated deltas (fallback path)."""
    import json

    content: list[Any] = []
    text = "".join(text_parts)
    if text:
        content.append(TextContent(text=text))
    for entry in tool_acc:
        try:
            args = json.loads(entry["args"]) if entry["args"] else {}
        except json.JSONDecodeError:
            args = {}
        content.append(ToolCallContent(id=entry["id"], name=entry["name"], arguments=args))
    stop_reason = "tool_use" if tool_acc else "end_turn"
    return AssistantMessage(content=content, stop_reason=stop_reason, model=model)


# --------------------------------------------------------------------------- #
# Tool execution: prepare → execute → finalize
# --------------------------------------------------------------------------- #


@dataclass
class ToolBatchResult:
    messages: list[ToolResultMessage]
    terminate: bool


@dataclass
class _Prepared:
    """Outcome of preparing one tool call.

    ``immediate=True`` short-circuits execution with a ready-made result
    (unknown tool, blocked by hook, validation failure). Otherwise ``tool`` and
    ``args`` carry the coerced call to execute.
    """

    tool_call: ToolCallContent
    immediate: bool
    tool: AgentTool | None = None
    args: dict = field(default_factory=dict)
    result: ToolResult | None = None  # set when immediate
    is_error: bool = False


@dataclass
class _Executed:
    result: ToolResult
    is_error: bool


@dataclass
class _Finalized:
    result: ToolResult
    is_error: bool


async def _execute_tool_batch(
    ctx: AgentContext,
    assistant_message: AssistantMessage,
    config: AgentLoopConfig,
    emit: AgentEventSink,
) -> ToolBatchResult:
    """Execute a batch of tool calls from one assistant message.

    Runs sequentially when the loop default is sequential, when any tool in the
    batch declares ``execution_mode="sequential"``, or when the batch targets
    overlapping file paths (write-write hazard). Otherwise parallel.
    """
    tool_calls = assistant_message.tool_calls
    tools_map = {t.schema.name: t for t in (ctx.tools or [])}

    has_sequential = any(
        (tools_map.get(tc.name).execution_mode if tools_map.get(tc.name) else "sequential")
        == "sequential"
        for tc in tool_calls
    )

    if (
        config.tool_execution == "sequential"
        or has_sequential
        or _has_path_overlap(tool_calls)
    ):
        return await _execute_sequential(ctx, assistant_message, tool_calls, config, emit)
    return await _execute_parallel(ctx, assistant_message, tool_calls, config, emit)


async def _execute_sequential(
    ctx: AgentContext,
    assistant_message: AssistantMessage,
    tool_calls: list[ToolCallContent],
    config: AgentLoopConfig,
    emit: AgentEventSink,
) -> ToolBatchResult:
    """Sequential execution: prepare, execute, finalize one at a time."""
    messages: list[ToolResultMessage] = []
    finalized: list[_Finalized] = []

    for tc in tool_calls:
        if config.abort and config.abort.is_set():
            break

        await emit(
            AgentEvent(
                type="tool_execution_start",
                tool_call_id=tc.id,
                tool_name=tc.name,
                args=tc.arguments,
            )
        )

        async def _on_update(text: str, _tc=tc) -> None:
            await emit(
                AgentEvent(
                    type="tool_execution_update",
                    tool_call_id=_tc.id,
                    tool_name=_tc.name,
                    update_text=text,
                )
            )

        _start = time.monotonic()
        prepared = await _prepare_tool_call(ctx, assistant_message, tc, config)

        if prepared.immediate:
            result = prepared.result or ToolResult(content=[])
            result_msg = _make_tool_result_message(tc, result, prepared.is_error)
            finalized.append(_Finalized(result=result, is_error=prepared.is_error))
        else:
            executed = await _execute_prepared(prepared, on_update=_on_update, abort=config.abort)
            final = await _finalize(ctx, assistant_message, prepared, executed, config)
            result_msg = _make_tool_result_message(tc, final.result, final.is_error)
            finalized.append(final)

        duration_ms = int((time.monotonic() - _start) * 1000)
        await emit(
            AgentEvent(
                type="tool_execution_end",
                tool_call_id=tc.id,
                tool_name=tc.name,
                result=result_msg,
                is_error=result_msg.is_error,
                duration_ms=duration_ms,
            )
        )
        await emit(AgentEvent(type="message_start", message=result_msg))
        await emit(AgentEvent(type="message_end", message=result_msg))
        messages.append(result_msg)

    terminate = bool(finalized) and all(f.result.terminate for f in finalized)
    return ToolBatchResult(messages=messages, terminate=terminate)


async def _execute_parallel(
    ctx: AgentContext,
    assistant_message: AssistantMessage,
    tool_calls: list[ToolCallContent],
    config: AgentLoopConfig,
    emit: AgentEventSink,
) -> ToolBatchResult:
    """Parallel execution: prepare sequentially, execute concurrently.

    Preparation may depend on context, so it stays sequential. Execution runs
    via ``asyncio.gather``; a tool that throws is captured as an error result
    (``_execute_prepared`` never raises) and the rest continue. Results are
    emitted in the original call order.
    """
    prepared_list: list[tuple[int, ToolCallContent, _Prepared]] = []
    immediate: dict[int, tuple[ToolCallContent, ToolResult, bool]] = {}

    for i, tc in enumerate(tool_calls):
        await emit(
            AgentEvent(
                type="tool_execution_start",
                tool_call_id=tc.id,
                tool_name=tc.name,
                args=tc.arguments,
            )
        )
        prepared = await _prepare_tool_call(ctx, assistant_message, tc, config)
        if prepared.immediate:
            immediate[i] = (tc, prepared.result or ToolResult(content=[]), prepared.is_error)
        else:
            prepared_list.append((i, tc, prepared))

    async def _run(idx: int, tc: ToolCallContent, prepared: _Prepared):
        # Each tool's span lives inside its own gather coroutine, so
        # duration_ms is per-tool wall time, not batch time.
        _start = time.monotonic()
        executed = await _execute_prepared(prepared, on_update=None, abort=config.abort)
        final = await _finalize(ctx, assistant_message, prepared, executed, config)
        duration_ms = int((time.monotonic() - _start) * 1000)
        return idx, tc, final, duration_ms

    executed_results = await asyncio.gather(
        *[_run(i, tc, p) for i, tc, p in prepared_list],
        return_exceptions=True,
    )

    all_results: dict[int, tuple[ToolCallContent, ToolResult, bool, int]] = {}
    for idx, (tc, result, is_error) in immediate.items():
        all_results[idx] = (tc, result, is_error, 0)
    for k, item in enumerate(executed_results):
        if isinstance(item, Exception):
            # _run's callees (_execute_prepared / _finalize) contain their own
            # errors, so this is defensive — but if anything unexpected raises,
            # synthesize an error result rather than dropping the call, which
            # would orphan the tool_call (no result message, no
            # tool_execution_end) and force the wire sanitizer to paper over it
            # (NB-8). gather preserves order, so prepared_list[k] is this call.
            idx, tc, _prep = prepared_list[k]
            logger.warning("parallel tool run raised idx=%s tool=%s: %s", idx, tc.name, item)
            all_results[idx] = (tc, _error_result(f"Tool run error: {item}"), True, 0)
            continue
        idx, tc, final, duration_ms = item
        all_results[idx] = (tc, final.result, final.is_error, duration_ms)

    messages: list[ToolResultMessage] = []
    results_for_terminate: list[ToolResult] = []
    for i in sorted(all_results.keys()):
        tc, result, is_error, duration_ms = all_results[i]
        result_msg = _make_tool_result_message(tc, result, is_error)
        await emit(
            AgentEvent(
                type="tool_execution_end",
                tool_call_id=tc.id,
                tool_name=tc.name,
                result=result_msg,
                is_error=is_error,
                duration_ms=duration_ms,
            )
        )
        await emit(AgentEvent(type="message_start", message=result_msg))
        await emit(AgentEvent(type="message_end", message=result_msg))
        messages.append(result_msg)
        results_for_terminate.append(result)

    terminate = bool(results_for_terminate) and all(r.terminate for r in results_for_terminate)
    return ToolBatchResult(messages=messages, terminate=terminate)


async def _prepare_tool_call(
    ctx: AgentContext,
    assistant_message: AssistantMessage,
    tc: ToolCallContent,
    config: AgentLoopConfig,
) -> _Prepared:
    """Resolve the tool, coerce arguments, run the before-hook, validate.

    Returns an immediate error outcome for an unknown tool, a hook block, or a
    missing required argument; otherwise a ready-to-execute prepared call.
    """
    tools_map = {t.schema.name: t for t in (ctx.tools or [])}
    tool = tools_map.get(tc.name)
    if tool is None:
        return _Prepared(
            tool_call=tc,
            immediate=True,
            result=_error_result(f"Unknown tool: {tc.name}"),
            is_error=True,
        )

    # Argument coercion / the app's prepare_arguments hook must never crash the
    # loop or orphan the call — a raise becomes an immediate error result the
    # model sees, and the batch keeps going (NB-8).
    try:
        if tool.prepare_arguments is not None:
            args = tool.prepare_arguments(tc.arguments)
        else:
            args = coerce_arguments(tc.arguments, tool.schema)
    except Exception as exc:  # noqa: BLE001 — contain into an error result
        logger.warning("tool_prepare_error tool=%s id=%s: %s", tc.name, tc.id, exc)
        return _Prepared(
            tool_call=tc,
            immediate=True,
            result=_error_result(f"Argument preparation failed: {exc}"),
            is_error=True,
        )

    missing = _missing_required(args, tool.schema)
    if missing:
        return _Prepared(
            tool_call=tc,
            immediate=True,
            result=_error_result(f"Missing required argument(s): {', '.join(missing)}"),
            is_error=True,
        )

    if config.before_tool_call is not None:
        hook_ctx = BeforeToolCallContext(
            assistant_message=assistant_message,
            tool_call=tc,
            args=args,
            context=ctx,
        )
        try:
            decision = await _maybe_await(config.before_tool_call(hook_ctx))
        except Exception as exc:  # noqa: BLE001 — app-hook bug, don't crash/orphan
            logger.warning("before_tool_call hook raised tool=%s id=%s: %s", tc.name, tc.id, exc)
            return _Prepared(
                tool_call=tc,
                immediate=True,
                result=_error_result(f"before_tool_call hook error: {exc}"),
                is_error=True,
            )
        if decision and decision.block:
            reason = decision.reason or "blocked by policy"
            return _Prepared(
                tool_call=tc,
                immediate=True,
                result=_error_result(f"Tool call blocked: {reason}"),
                is_error=True,
            )

    return _Prepared(tool_call=tc, immediate=False, tool=tool, args=args)


async def _execute_prepared(
    prepared: _Prepared,
    on_update,
    abort: asyncio.Event | None,
) -> _Executed:
    """Run the prepared tool's execute function, capturing any exception."""
    assert prepared.tool is not None
    try:
        result = await prepared.tool.execute(
            prepared.tool_call.id, prepared.args, on_update, abort
        )
        # A tool can self-report a non-fatal failure via result.is_error without
        # raising (NB-19), so ToolEndEvent.success reflects it.
        return _Executed(result=result, is_error=bool(getattr(result, "is_error", False)))
    except Exception as exc:  # noqa: BLE001 — tools must not crash the loop
        logger.warning(
            "tool_execute_error tool=%s id=%s: %s",
            prepared.tool_call.name,
            prepared.tool_call.id,
            exc,
        )
        return _Executed(result=_error_result(f"Tool error: {exc}"), is_error=True)


async def _finalize(
    ctx: AgentContext,
    assistant_message: AssistantMessage,
    prepared: _Prepared,
    executed: _Executed,
    config: AgentLoopConfig,
) -> _Finalized:
    """Apply the after-hook's partial overrides to the executed result."""
    result = executed.result
    is_error = executed.is_error

    if config.after_tool_call is not None:
        hook_ctx = AfterToolCallContext(
            assistant_message=assistant_message,
            tool_call=prepared.tool_call,
            args=prepared.args,
            result=result,
            is_error=is_error,
            context=ctx,
        )
        # A raising after-hook must not crash the loop (sequential mode) or be
        # swallowed by gather into a dropped, orphaned call (parallel mode). Fail
        # the call closed with an error result — an after-hook is often a policy
        # gate, so falling open to the raw result would be the wrong default
        # (NB-8).
        try:
            override = await _maybe_await(config.after_tool_call(hook_ctx))
        except Exception as exc:  # noqa: BLE001 — contain into an error result
            logger.warning(
                "after_tool_call hook raised tool=%s id=%s: %s",
                prepared.tool_call.name,
                prepared.tool_call.id,
                exc,
            )
            return _Finalized(
                result=_error_result(f"after_tool_call hook error: {exc}"),
                is_error=True,
            )
        if override:
            content = override.content if override.content is not None else result.content
            details = override.details if override.details is not None else result.details
            terminate = (
                override.terminate if override.terminate is not None else result.terminate
            )
            result = ToolResult(content=content, details=details, terminate=terminate)
            if override.is_error is not None:
                is_error = override.is_error

    return _Finalized(result=result, is_error=is_error)


def _make_tool_result_message(
    tc: ToolCallContent, result: ToolResult, is_error: bool
) -> ToolResultMessage:
    return ToolResultMessage(
        tool_call_id=tc.id,
        tool_name=tc.name,
        content=result.content,
        is_error=is_error,
    )


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #


async def _maybe_await(value):
    """Await ``value`` if it's awaitable, else return it. Hooks may be sync."""
    if inspect.isawaitable(value):
        return await value
    return value


def _error_result(text: str) -> ToolResult:
    return ToolResult(content=[TextContent(text=text)])


def _missing_required(args: dict, schema: ToolSchema) -> list[str]:
    """Required schema parameters absent from (or null in) ``args``."""
    missing = []
    for param in schema.parameters:
        if param.required and (param.name not in args or args[param.name] is None):
            missing.append(param.name)
    return missing


def _has_path_overlap(tool_calls: list[ToolCallContent]) -> bool:
    """Whether any two calls in the batch target the same file path.

    Falls back to sequential execution to avoid concurrent writes to one path.
    Prior art: Hermes's _should_parallelize_tool_batch() path check.
    """
    paths: set[str] = set()
    for tc in tool_calls:
        path = tc.arguments.get("file_path") or tc.arguments.get("path")
        if path:
            if path in paths:
                return True
            paths.add(path)
    return False
