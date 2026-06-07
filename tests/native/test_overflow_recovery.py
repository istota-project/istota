"""Stage 3 — reactive overflow-recovery compaction in the native brain.

When a turn exceeds the context window mid-task, the provider returns a
context-length error classified ``is_context_overflow``. ``_RetryingProvider``
does not retry it, the loop ends ``stop_reason="error"``, and today the task
fails. This stage wires a bounded (≤2) recovery: force-compact the accumulated
transcript and ``run_agent_loop_continue`` from the summary, sharing the task's
wall-clock deadline.
"""

from collections.abc import AsyncIterator
from pathlib import Path

from istota.brain import BrainRequest
from istota.brain import native as native_mod
from istota.brain.native import (
    NativeBrain,
    _aggressive_cut,
    _build_recovery_context,
)
from istota.config import NativeBrainConfig
from istota.llm.provider import StreamDone, StreamError, StreamStart, TextDelta
from istota.llm.types import (
    AssistantMessage,
    TextContent,
    ToolCallContent,
    ToolResultMessage,
    UserMessage,
    Usage,
)


def _req(prompt, cwd, model="claude-sonnet-4-6", timeout=30):
    return BrainRequest(
        prompt=prompt,
        allowed_tools=[],
        cwd=cwd,
        env={},
        timeout_seconds=timeout,
        model=model,
    )


def _brain(provider, **cfg):
    return NativeBrain(NativeBrainConfig(model="claude-sonnet-4-6", **cfg), provider=provider)


class _ScriptedProvider:
    """Pops one scripted behavior per ``stream`` call.

    Each behavior is ``("overflow", msg)`` → a context-length StreamError, or
    ``("done", text)`` → a successful completion. Compaction's summary call and
    the loop / continue turns all draw from the same script in call order.
    """

    def __init__(self, behaviors):
        self.behaviors = list(behaviors)
        self.calls = 0

    async def stream(
        self, system_prompt, messages, tools, *, model="", max_tokens=16384, **kw
    ) -> AsyncIterator:
        self.calls += 1
        kind, payload = self.behaviors.pop(0) if self.behaviors else ("done", "")
        yield StreamStart()
        if kind == "overflow":
            yield StreamError(
                message=AssistantMessage(stop_reason="error", error_message=payload)
            )
        else:
            yield TextDelta(text=payload)
            yield StreamDone(
                message=AssistantMessage(
                    content=[TextContent(text=payload)],
                    usage=Usage(input_tokens=10, output_tokens=5),
                    stop_reason="end_turn",
                )
            )


_OVERFLOW = ("overflow", "HTTP 400: context length exceeded")


class _SummaryProvider:
    """Always yields a fixed summary completion (for compaction unit tests)."""

    async def stream(self, system_prompt, messages, tools, *, model="", max_tokens=16384, **kw):
        yield StreamDone(
            message=AssistantMessage(
                content=[TextContent(text="SUMMARY")], stop_reason="end_turn"
            )
        )


def _convert(msgs):
    # minimal identity convert for the helper unit tests
    from istota.session.messages import CompactionSummaryMessage

    out = []
    for m in msgs:
        if isinstance(m, (UserMessage, AssistantMessage, ToolResultMessage)):
            out.append(m)
        elif isinstance(m, CompactionSummaryMessage):
            out.append(UserMessage(content=[TextContent(text=m.summary)]))
    return out


class TestRecoveryEndToEnd:
    def test_overflow_then_success_completes(self, tmp_path, monkeypatch):
        spy = {"continue": 0}
        real_continue = native_mod.run_agent_loop_continue

        async def _spy_continue(*a, **k):
            spy["continue"] += 1
            return await real_continue(*a, **k)

        monkeypatch.setattr(native_mod, "run_agent_loop_continue", _spy_continue)

        provider = _ScriptedProvider(
            [_OVERFLOW, ("done", "SUMMARY"), ("done", "Recovered answer.")]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is True
        assert result.result_text == "Recovered answer."
        assert spy["continue"] == 1  # recovery entered exactly once
        assert result.usage.input_tokens > 0  # usage spans the recovered segment

    def test_budget_exhausted_returns_error(self, tmp_path, monkeypatch):
        spy = {"continue": 0}
        real_continue = native_mod.run_agent_loop_continue

        async def _spy_continue(*a, **k):
            spy["continue"] += 1
            return await real_continue(*a, **k)

        monkeypatch.setattr(native_mod, "run_agent_loop_continue", _spy_continue)

        # Overflows forever; compaction summaries interleave. After 2 recovery
        # attempts the brain gives up.
        provider = _ScriptedProvider(
            [
                _OVERFLOW,
                ("done", "SUMMARY"),
                _OVERFLOW,
                ("done", "SUMMARY"),
                _OVERFLOW,
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is False
        assert result.stop_reason == "error"
        assert "context length" in result.result_text
        assert spy["continue"] == 2  # exactly two recovery attempts

    def test_non_overflow_error_not_recovered(self, tmp_path, monkeypatch):
        spy = {"continue": 0}
        real_continue = native_mod.run_agent_loop_continue

        async def _spy_continue(*a, **k):
            spy["continue"] += 1
            return await real_continue(*a, **k)

        monkeypatch.setattr(native_mod, "run_agent_loop_continue", _spy_continue)

        provider = _ScriptedProvider([("overflow", "HTTP 400: content policy refused")])
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is False
        assert spy["continue"] == 0  # a non-overflow error is not recovered


class TestAggressiveCut:
    def test_returns_user_anchor_directly(self):
        # A UserMessage is a safe tail start — return it as-is.
        transcript = [
            AssistantMessage(content=[TextContent(text="a0")]),
            UserMessage(content=[TextContent(text="u")]),
            AssistantMessage(content=[TextContent(text="a2")], stop_reason="error"),
        ]
        assert _aggressive_cut(transcript) == 1  # the user message

    def test_advances_past_trailing_tool_result_to_avoid_orphan(self):
        # The most recent anchor is a tool_result; the cut advances forward past
        # it so the kept tail never begins with an orphaned result — the owning
        # assistant + result both land in the compacted prefix.
        transcript = [
            UserMessage(content=[TextContent(text="u")]),
            AssistantMessage(content=[ToolCallContent(id="c", name="Bash")]),
            ToolResultMessage(tool_call_id="c", content=[TextContent(text="r")]),
            AssistantMessage(content=[TextContent(text="a2")], stop_reason="error"),
        ]
        assert _aggressive_cut(transcript) == 3  # advanced past the tool_result

    def test_backs_up_when_tool_result_is_last(self):
        # Anchor is a trailing tool_result with no newer message: forward-advance
        # runs off the end, so back up to keep the owning assistant — the tail
        # stays [assistant(+call), result], never an orphan.
        transcript = [
            UserMessage(content=[TextContent(text="u")]),
            AssistantMessage(content=[ToolCallContent(id="c", name="Bash")]),
            ToolResultMessage(tool_call_id="c", content=[TextContent(text="r")]),
        ]
        cut = _aggressive_cut(transcript)
        assert cut == 1  # backs up to the owning assistant
        assert not isinstance(transcript[cut], ToolResultMessage)

    def test_no_user_or_tool_result_compacts_everything(self):
        transcript = [
            AssistantMessage(content=[TextContent(text="a")]),
            AssistantMessage(content=[TextContent(text="b")]),
        ]
        assert _aggressive_cut(transcript) == len(transcript)


class TestBuildRecoveryContext:
    async def test_nudge_appended_when_tail_ends_on_assistant(self, tmp_path):
        transcript = [
            UserMessage(content=[TextContent(text="hi")]),
            AssistantMessage(content=[TextContent(text="")], stop_reason="error"),
        ]
        ctx, summary, details = await _build_recovery_context(
            transcript, "sys", None, None, None, _SummaryProvider(), "m", _convert
        )
        last = ctx.messages[-1]
        assert isinstance(last, UserMessage)
        assert "compacted" in last.content[0].text
        # continue must not raise on this context
        from istota.agent.loop import run_agent_loop_continue

        async def _sink(_e):
            pass

        # the continue would call the provider; _SummaryProvider answers cleanly
        await run_agent_loop_continue(
            ctx,
            _loop_config(_SummaryProvider(), _convert),
            _sink,
        )

    async def test_cut_zero_uses_aggressive_fallback(self, tmp_path):
        # Small messages → find_cut_point returns 0 → aggressive fallback. The
        # tool exchange (call + result) is folded into the summary, so the kept
        # tail never begins with an orphaned tool_result that conversion would
        # silently drop.
        transcript = [
            UserMessage(content=[TextContent(text="u")]),
            AssistantMessage(content=[ToolCallContent(id="c", name="Bash")]),
            ToolResultMessage(tool_call_id="c", content=[TextContent(text="r")]),
            AssistantMessage(content=[TextContent(text="")], stop_reason="error"),
        ]
        ctx, _s, _d = await _build_recovery_context(
            transcript, "sys", None, None, None, _SummaryProvider(), "m", _convert
        )
        from istota.session.messages import CompactionSummaryMessage

        assert isinstance(ctx.messages[0], CompactionSummaryMessage)
        # The kept tail does not start on a tool_result (no orphan).
        tail = ctx.messages[1:]
        assert not (tail and isinstance(tail[0], ToolResultMessage))

        # And it survives the brain's real conversion: sanitize_tool_pairs has no
        # orphaned result to drop, so every ToolResultMessage that reaches the
        # wire still has its owning tool_call.
        brain = _brain(_SummaryProvider())
        wire = brain._convert_to_llm(ctx.messages)
        seen_calls = {
            c.id
            for m in wire
            if isinstance(m, AssistantMessage)
            for c in m.tool_calls
        }
        for m in wire:
            if isinstance(m, ToolResultMessage):
                assert m.tool_call_id in seen_calls


def _loop_config(provider, convert):
    from istota.agent.types import AgentLoopConfig

    return AgentLoopConfig(provider=provider, model="m", convert_to_llm=convert)
