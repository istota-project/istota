"""Phase 2 — the core agent loop, tool dispatch, hooks, stop conditions."""

import asyncio

import pytest

from istota.agent.events import AgentEvent
from istota.agent.hooks import (
    AfterToolCallResult,
    BeforeToolCallResult,
)
from istota.agent.loop import run_agent_loop, run_agent_loop_continue
from istota.agent.sanitize import sanitize_tool_pairs
from istota.agent.tools import AgentTool, ToolResult
from istota.agent.types import (
    AgentContext,
    AgentLoopConfig,
    PrepareNextTurnResult,
    StopDecision,
)
from istota.llm.types import (
    AssistantMessage,
    Message,
    TextContent,
    ToolCallContent,
    ToolResultMessage,
    ToolParameter,
    ToolSchema,
    UserMessage,
)

from ._mock_provider import MockProvider

pytestmark = pytest.mark.asyncio


# --------------------------------------------------------------------------- #
# Fixtures / helpers
# --------------------------------------------------------------------------- #


def _identity_convert(messages: list) -> list[Message]:
    """convert_to_llm that keeps only real LLM Messages and sanitizes pairs."""
    llm_msgs = [m for m in messages if isinstance(m, (UserMessage, AssistantMessage, ToolResultMessage))]
    return sanitize_tool_pairs(llm_msgs)


def _echo_tool(calls_log: list) -> AgentTool:
    async def _execute(call_id, args, on_update, abort):
        calls_log.append(args)
        if on_update:
            await on_update("partial")
        return ToolResult(content=[TextContent(text=f"echo:{args.get('value', '')}")])

    return AgentTool(
        schema=ToolSchema(
            name="echo",
            description="echo a value",
            parameters=[ToolParameter(name="value", type="string", required=False)],
        ),
        execute=_execute,
        execution_mode="sequential",
    )


def _text_turn(text: str, stop_reason: str = "end_turn") -> AssistantMessage:
    return AssistantMessage(content=[TextContent(text=text)], stop_reason=stop_reason)


def _tool_turn(name: str, args: dict, call_id: str = "c1") -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCallContent(id=call_id, name=name, arguments=args)],
        stop_reason="tool_use",
    )


class _Sink:
    def __init__(self):
        self.events: list[AgentEvent] = []

    async def __call__(self, event: AgentEvent) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e.type for e in self.events]


def _config(provider, **overrides) -> AgentLoopConfig:
    base = dict(provider=provider, model="mock", convert_to_llm=_identity_convert)
    base.update(overrides)
    return AgentLoopConfig(**base)


def _ctx(tools=None, system="sys", messages=None) -> AgentContext:
    return AgentContext(system_prompt=system, messages=messages or [], tools=tools)


# --------------------------------------------------------------------------- #
# Basic flows
# --------------------------------------------------------------------------- #


class TestBasicLoop:
    async def test_text_only_single_turn(self):
        provider = MockProvider([_text_turn("hello there")])
        sink = _Sink()
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="hi")])],
            _ctx(),
            _config(provider),
            sink,
        )
        assistant = [m for m in out if isinstance(m, AssistantMessage)]
        assert len(assistant) == 1
        assert assistant[0].text == "hello there"
        assert sink.types()[0] == "agent_start"
        assert sink.types()[-1] == "agent_end"

    async def test_single_tool_call_then_text(self):
        calls: list = []
        provider = MockProvider(
            [
                _tool_turn("echo", {"value": "x"}),
                _text_turn("done"),
            ]
        )
        sink = _Sink()
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool(calls)]),
            _config(provider),
            sink,
        )
        assert calls == [{"value": "x"}]
        tool_results = [m for m in out if isinstance(m, ToolResultMessage)]
        assert len(tool_results) == 1
        assert tool_results[0].content[0].text == "echo:x"
        assert "tool_execution_start" in sink.types()
        assert "tool_execution_end" in sink.types()

    async def test_tool_update_emits_event(self):
        calls: list = []
        provider = MockProvider([_tool_turn("echo", {"value": "x"}), _text_turn("done")])
        sink = _Sink()
        await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool(calls)]),
            _config(provider),
            sink,
        )
        updates = [e for e in sink.events if e.type == "tool_execution_update"]
        assert len(updates) == 1
        assert updates[0].update_text == "partial"

    async def test_second_turn_sees_tool_result(self):
        calls: list = []
        provider = MockProvider([_tool_turn("echo", {"value": "x"}), _text_turn("done")])
        await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool(calls)]),
            _config(provider),
            _Sink(),
        )
        # Second stream call should include the user msg, assistant tool call,
        # and the tool result.
        second_call_msgs = provider.calls[1]["messages"]
        assert any(isinstance(m, ToolResultMessage) for m in second_call_msgs)

    async def test_tool_execution_end_carries_duration(self):
        calls: list = []
        provider = MockProvider([_tool_turn("echo", {"value": "x"}), _text_turn("done")])
        sink = _Sink()
        await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool(calls)]),
            _config(provider),
            sink,
        )
        ends = [e for e in sink.events if e.type == "tool_execution_end"]
        assert len(ends) == 1
        # Loop-measured, non-negative (sequential path).
        assert ends[0].duration_ms >= 0
        assert isinstance(ends[0].duration_ms, int)

    async def test_parallel_durations_are_per_tool(self):
        # Two parallel tools with different sleeps: each tool_execution_end
        # carries its own wall time, not the batch total.
        async def _slow(call_id, args, on_update, abort):
            await asyncio.sleep(args.get("delay", 0))
            return ToolResult(content=[TextContent(text="ok")])

        slow_tool = AgentTool(
            schema=ToolSchema(
                name="slow", description="sleep",
                parameters=[ToolParameter(name="delay", type="number", required=False)],
            ),
            execute=_slow,
            execution_mode="parallel",
        )
        provider = MockProvider([
            AssistantMessage(
                content=[
                    ToolCallContent(id="a", name="slow", arguments={"delay": 0.0}),
                    ToolCallContent(id="b", name="slow", arguments={"delay": 0.15}),
                ],
                stop_reason="tool_use",
            ),
            _text_turn("done"),
        ])
        sink = _Sink()
        await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[slow_tool]),
            _config(provider, tool_execution="parallel"),
            sink,
        )
        ends = {e.tool_call_id: e.duration_ms for e in sink.events if e.type == "tool_execution_end"}
        assert set(ends) == {"a", "b"}
        # The fast tool's span is well under the slow tool's — proving per-tool
        # timing rather than a shared batch clock.
        assert ends["a"] < ends["b"]


# --------------------------------------------------------------------------- #
# Error / unknown tool / coercion
# --------------------------------------------------------------------------- #


class TestErrorPaths:
    async def test_provider_error_stops_loop(self):
        provider = MockProvider([_text_turn("boom", stop_reason="error")])
        sink = _Sink()
        await run_agent_loop(
            [UserMessage(content=[TextContent(text="hi")])],
            _ctx(),
            _config(provider),
            sink,
        )
        end = [e for e in sink.events if e.type == "agent_end"][0]
        assert end.stop_reason == "error"

    async def test_unknown_tool_returns_error_result(self):
        provider = MockProvider(
            [_tool_turn("nonexistent", {}), _text_turn("recovered")]
        )
        sink = _Sink()
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool([])]),
            _config(provider),
            sink,
        )
        tr = [m for m in out if isinstance(m, ToolResultMessage)][0]
        assert tr.is_error is True
        assert "unknown tool" in tr.content[0].text.lower()

    async def test_tool_exception_captured_as_error(self):
        async def _boom(call_id, args, on_update, abort):
            raise RuntimeError("kaboom")

        tool = AgentTool(
            schema=ToolSchema(name="boom", description=""),
            execute=_boom,
        )
        provider = MockProvider([_tool_turn("boom", {}), _text_turn("ok")])
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[tool]),
            _config(provider),
            _Sink(),
        )
        tr = [m for m in out if isinstance(m, ToolResultMessage)][0]
        assert tr.is_error is True
        assert "kaboom" in tr.content[0].text

    async def test_argument_coercion_applied(self):
        seen: list = []

        async def _exec(call_id, args, on_update, abort):
            seen.append(args)
            return ToolResult(content=[TextContent(text="ok")])

        tool = AgentTool(
            schema=ToolSchema(
                name="count",
                description="",
                parameters=[ToolParameter(name="n", type="integer")],
            ),
            execute=_exec,
        )
        provider = MockProvider([_tool_turn("count", {"n": "5"}), _text_turn("done")])
        await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[tool]),
            _config(provider),
            _Sink(),
        )
        assert seen == [{"n": 5}]

    async def test_missing_required_argument_errors(self):
        async def _exec(call_id, args, on_update, abort):
            return ToolResult(content=[TextContent(text="ok")])

        tool = AgentTool(
            schema=ToolSchema(
                name="need",
                description="",
                parameters=[ToolParameter(name="x", type="string", required=True)],
            ),
            execute=_exec,
        )
        provider = MockProvider([_tool_turn("need", {}), _text_turn("done")])
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[tool]),
            _config(provider),
            _Sink(),
        )
        tr = [m for m in out if isinstance(m, ToolResultMessage)][0]
        assert tr.is_error is True
        assert "missing required" in tr.content[0].text.lower()


# --------------------------------------------------------------------------- #
# Hooks
# --------------------------------------------------------------------------- #


class TestHooks:
    async def test_before_hook_blocks_tool(self):
        calls: list = []

        def _before(hook_ctx):
            return BeforeToolCallResult(block=True, reason="not allowed")

        provider = MockProvider([_tool_turn("echo", {"value": "x"}), _text_turn("ok")])
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool(calls)]),
            _config(provider, before_tool_call=_before),
            _Sink(),
        )
        assert calls == []  # tool never executed
        tr = [m for m in out if isinstance(m, ToolResultMessage)][0]
        assert tr.is_error is True
        assert "not allowed" in tr.content[0].text

    async def test_after_hook_rewrites_result(self):
        def _after(hook_ctx):
            return AfterToolCallResult(content=[TextContent(text="REWRITTEN")])

        provider = MockProvider([_tool_turn("echo", {"value": "x"}), _text_turn("ok")])
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool([])]),
            _config(provider, after_tool_call=_after),
            _Sink(),
        )
        tr = [m for m in out if isinstance(m, ToolResultMessage)][0]
        assert tr.content[0].text == "REWRITTEN"

    async def test_after_hook_terminate(self):
        def _after(hook_ctx):
            return AfterToolCallResult(terminate=True)

        # Only one assistant turn scripted: if terminate works, the loop won't
        # request a second completion (which would exhaust the mock).
        provider = MockProvider([_tool_turn("echo", {"value": "x"})])
        await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool([])]),
            _config(provider, after_tool_call=_after),
            _Sink(),
        )
        assert len(provider.calls) == 1

    async def test_before_hook_exception_becomes_error_result(self):
        # NB-8: a before-hook that raises must not crash the loop or orphan the
        # tool call — it becomes an error result and the loop continues.
        def _before(hook_ctx):
            raise RuntimeError("hook boom")

        provider = MockProvider([_tool_turn("echo", {"value": "x"}), _text_turn("ok")])
        sink = _Sink()
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool([])]),
            _config(provider, before_tool_call=_before),
            sink,
        )
        tr = [m for m in out if isinstance(m, ToolResultMessage)]
        assert len(tr) == 1
        assert tr[0].is_error is True
        assert "hook" in tr[0].content[0].text.lower()
        # The call was paired with a tool_execution_end (not orphaned).
        assert "tool_execution_end" in sink.types()

    async def test_after_hook_exception_becomes_error_result(self):
        def _after(hook_ctx):
            raise RuntimeError("after boom")

        provider = MockProvider([_tool_turn("echo", {"value": "x"}), _text_turn("ok")])
        sink = _Sink()
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool([])]),
            _config(provider, after_tool_call=_after),
            sink,
        )
        tr = [m for m in out if isinstance(m, ToolResultMessage)]
        assert len(tr) == 1
        assert tr[0].is_error is True
        assert "tool_execution_end" in sink.types()

    async def test_prepare_arguments_exception_becomes_error_result(self):
        def _boom(_args):
            raise ValueError("bad args")

        async def _never(call_id, args, ou, ab):
            return ToolResult(content=[TextContent(text="x")])

        tool = AgentTool(
            schema=ToolSchema(name="boom", description="d", parameters=[]),
            execute=_never,
            execution_mode="sequential",
            prepare_arguments=_boom,
        )
        provider = MockProvider([_tool_turn("boom", {}), _text_turn("ok")])
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[tool]),
            _config(provider),
            _Sink(),
        )
        tr = [m for m in out if isinstance(m, ToolResultMessage)]
        assert len(tr) == 1
        assert tr[0].is_error is True

    async def test_after_hook_exception_parallel_mode_no_orphan(self):
        # Parallel mode: gather(return_exceptions=True) previously swallowed a
        # raising after-hook and `continue`d, leaving the call with no result
        # message and no tool_execution_end. Both parallel calls must yield an
        # error result.
        def _after(hook_ctx):
            raise RuntimeError("after boom")

        async def _par_exec(call_id, args, ou, ab):
            return ToolResult(content=[TextContent(text="ok")])

        par_tool = AgentTool(
            schema=ToolSchema(
                name="par",
                description="d",
                parameters=[ToolParameter(name="value", type="string", required=False)],
            ),
            execute=_par_exec,
            execution_mode="parallel",
        )
        two_calls = AssistantMessage(
            content=[
                ToolCallContent(id="c1", name="par", arguments={"value": "a"}),
                ToolCallContent(id="c2", name="par", arguments={"value": "b"}),
            ],
            stop_reason="tool_use",
        )
        provider = MockProvider([two_calls, _text_turn("ok")])
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[par_tool]),
            _config(provider, after_tool_call=_after),
            _Sink(),
        )
        tr = [m for m in out if isinstance(m, ToolResultMessage)]
        assert len(tr) == 2  # neither call orphaned
        assert all(m.is_error for m in tr)


# --------------------------------------------------------------------------- #
# prepare_next_turn + stop conditions
# --------------------------------------------------------------------------- #


class TestTurnControl:
    async def test_prepare_next_turn_switches_model(self):
        async def _prep(ctx, msgs):
            return PrepareNextTurnResult(model="model-2")

        provider = MockProvider(
            [_tool_turn("echo", {"value": "x"}), _text_turn("done")]
        )
        await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool([])]),
            _config(provider, prepare_next_turn=_prep),
            _Sink(),
        )
        # First call used "mock", second used the swapped model.
        assert provider.calls[0]["model"] == "mock"
        assert provider.calls[1]["model"] == "model-2"

    async def test_stop_condition_halts_with_reason(self):
        async def _stop(ctx, msgs):
            return StopDecision(stop=True, reason="max_turns")

        # Two tool turns scripted; stop after the first turn means the second
        # is never requested.
        provider = MockProvider(
            [_tool_turn("echo", {"value": "x"}), _tool_turn("echo", {"value": "y"})]
        )
        sink = _Sink()
        await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool([])]),
            _config(provider, stop_conditions=[_stop]),
            sink,
        )
        assert len(provider.calls) == 1
        end = [e for e in sink.events if e.type == "agent_end"][0]
        assert end.stop_reason == "max_turns"

    async def test_should_stop_after_turn_back_compat(self):
        async def _stop(ctx, msgs):
            return True

        provider = MockProvider(
            [_tool_turn("echo", {"value": "x"}), _tool_turn("echo", {"value": "y"})]
        )
        sink = _Sink()
        await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool([])]),
            _config(provider, should_stop_after_turn=_stop),
            sink,
        )
        assert len(provider.calls) == 1
        end = [e for e in sink.events if e.type == "agent_end"][0]
        assert end.stop_reason == "should_stop_after_turn"


# --------------------------------------------------------------------------- #
# Steering / follow-up queues
# --------------------------------------------------------------------------- #


class TestQueues:
    async def test_follow_up_messages_resume_loop(self):
        provider = MockProvider([_text_turn("first"), _text_turn("second")])
        sent = {"done": False}

        def _follow():
            if sent["done"]:
                return []
            sent["done"] = True
            return [UserMessage(content=[TextContent(text="more")])]

        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="hi")])],
            _ctx(),
            _config(provider, get_follow_up_messages=_follow),
            _Sink(),
        )
        assert len(provider.calls) == 2
        texts = [m.text for m in out if isinstance(m, AssistantMessage)]
        assert texts == ["first", "second"]

    async def test_steering_message_injected_into_context(self):
        # Single-shot source: returns one steering message, then nothing.
        # (The application is responsible for clearing consumed messages; a
        # source that re-returns the same message would loop forever.)
        provider = MockProvider([_tool_turn("echo", {"value": "x"}), _text_turn("done")])
        pending = [UserMessage(content=[TextContent(text="steer-me")])]

        def _steer():
            out = list(pending)
            pending.clear()
            return out

        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool([])]),
            _config(provider, get_steering_messages=_steer, steering_queue_mode="all"),
            _Sink(),
        )
        injected = [
            m for m in out if isinstance(m, UserMessage) and m.content[0].text == "steer-me"
        ]
        assert len(injected) == 1


# --------------------------------------------------------------------------- #
# Abort
# --------------------------------------------------------------------------- #


class TestAbort:
    async def test_abort_before_turn_stops(self):
        provider = MockProvider([_text_turn("never")])
        abort = asyncio.Event()
        abort.set()
        sink = _Sink()
        await run_agent_loop(
            [UserMessage(content=[TextContent(text="hi")])],
            _ctx(),
            _config(provider, abort=abort),
            sink,
        )
        # No provider call: aborted at the first loop boundary.
        assert provider.calls == []
        end = [e for e in sink.events if e.type == "agent_end"][0]
        assert end.stop_reason == "aborted"


# --------------------------------------------------------------------------- #
# continue entry point
# --------------------------------------------------------------------------- #


class TestContinue:
    async def test_continue_requires_messages(self):
        provider = MockProvider([])
        with pytest.raises(ValueError):
            await run_agent_loop_continue(_ctx(messages=[]), _config(provider), _Sink())

    async def test_continue_rejects_trailing_assistant(self):
        provider = MockProvider([])
        ctx = _ctx(messages=[AssistantMessage(content=[TextContent(text="x")])])
        with pytest.raises(ValueError):
            await run_agent_loop_continue(ctx, _config(provider), _Sink())

    async def test_continue_runs_from_context(self):
        provider = MockProvider([_text_turn("continued")])
        ctx = _ctx(messages=[UserMessage(content=[TextContent(text="resume")])])
        out = await run_agent_loop_continue(ctx, _config(provider), _Sink())
        assert any(isinstance(m, AssistantMessage) and m.text == "continued" for m in out)
