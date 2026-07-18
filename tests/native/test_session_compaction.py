"""Context compaction: thresholds, cut points, incremental summaries."""

import pytest

from istota.llm.types import (
    AssistantMessage,
    TextContent,
    ToolCallContent,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from istota.session.compaction import (
    compact_messages,
    derive_keep_recent_tokens,
    derive_reserve_tokens,
    estimate_context_tokens,
    find_cut_point,
    should_compact,
)
from istota.session.messages import CompactionDetails, CompactionSummaryMessage


def _user(text: str) -> UserMessage:
    return UserMessage(content=[TextContent(text=text)])


def _assistant(text: str, usage: Usage | None = None) -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text=text)],
        usage=usage or Usage(),
        stop_reason="end_turn",
    )


class TestShouldCompact:
    def test_below_threshold(self):
        assert should_compact(100_000, 200_000, reserve_tokens=16384) is False

    def test_above_threshold(self):
        assert should_compact(190_000, 200_000, reserve_tokens=16384) is True


class TestEstimateContextTokens:
    def test_uses_last_usage_plus_trailing(self):
        msgs = [
            _user("x" * 4000),
            _assistant("done", usage=Usage(input_tokens=5000, output_tokens=100)),
            _user("y" * 400),  # ~100 tokens trailing
        ]
        total, idx = estimate_context_tokens(msgs)
        assert idx == 1
        assert total == pytest.approx(5200, abs=5)  # 5100 usage + ~100 trailing

    def test_no_usage_falls_back_to_heuristic(self):
        msgs = [_user("a" * 400), _user("b" * 400)]
        total, idx = estimate_context_tokens(msgs)
        assert idx is None
        assert total == pytest.approx(200, abs=10)

    def test_error_message_usage_ignored(self):
        msgs = [
            _user("x" * 400),
            AssistantMessage(
                content=[TextContent(text="boom")],
                usage=Usage(input_tokens=9999),
                stop_reason="error",
            ),
        ]
        _total, idx = estimate_context_tokens(msgs)
        assert idx is None  # error-turn usage is unreliable

    def test_compaction_summary_counted(self):
        # NB-14: a CompactionSummaryMessage renders as a real user message but
        # was invisible to the estimate (no .content), so post-compaction
        # estimates ran low and later compactions fired late.
        summary = CompactionSummaryMessage(summary="s" * 4000)  # ~1000 tokens
        msgs = [summary, _user("y" * 400)]
        total, idx = estimate_context_tokens(msgs)
        assert idx is None  # no usable assistant usage → heuristic path
        assert total == pytest.approx(1100, abs=20)


class TestDerivedCompactionSizing:
    """NB-14: reserve/keep-recent scale with the model window so a small-window
    local model compacts sensibly instead of using the Anthropic-sized
    constants."""

    def test_large_window_matches_legacy_constants(self):
        assert derive_reserve_tokens(200_000) == 16384
        assert derive_keep_recent_tokens(200_000) == 20000

    def test_small_window_scales_down(self):
        assert derive_reserve_tokens(8000) == 2000
        assert derive_keep_recent_tokens(8000) == 4000
        # keep_recent must leave room below the window to actually shrink.
        assert derive_keep_recent_tokens(8000) < 8000 - derive_reserve_tokens(8000)

    def test_summary_counted_in_find_cut_point(self):
        # A big summary at the head must contribute tokens so the cut walk sees
        # it (it doesn't get cut — it's the head — but it must be measured).
        big_summary = CompactionSummaryMessage(summary="s" * 40000)  # ~10k tokens
        msgs = [big_summary, _user("a" * 4000), _assistant("b" * 4000)]
        # With a small keep_recent the tail is kept and the summary would be
        # cut only if measured; assert the walk terminates sensibly (no crash,
        # returns a valid index).
        cut = find_cut_point(msgs, keep_recent_tokens=2000)
        assert 0 <= cut <= len(msgs)


class TestFindCutPoint:
    def test_everything_fits_returns_zero(self):
        msgs = [_user("short"), _assistant("ok")]
        assert find_cut_point(msgs, keep_recent_tokens=20000) == 0

    def test_cuts_older_keeps_recent(self):
        # Each message ~2500 tokens; keep_recent=5000 keeps roughly the last 2-3.
        msgs = [_assistant("z" * 10000) for _ in range(10)]
        cut = find_cut_point(msgs, keep_recent_tokens=5000)
        assert 0 < cut < len(msgs)

    def test_never_cuts_at_tool_result(self):
        msgs = [
            _assistant("a" * 10000),
            AssistantMessage(
                content=[ToolCallContent(id="t1", name="Read", arguments={"file_path": "f"})],
                stop_reason="tool_use",
            ),
            ToolResultMessage(tool_call_id="t1", tool_name="Read", content=[TextContent(text="x" * 10000)]),
            _assistant("b" * 10000),
        ]
        cut = find_cut_point(msgs, keep_recent_tokens=4000)
        assert not isinstance(msgs[cut], ToolResultMessage) if cut < len(msgs) else True

    def test_keeps_at_least_newest_message(self):
        huge = _assistant("q" * 400_000)  # one message alone blows the budget
        msgs = [_assistant("a" * 1000), huge]
        cut = find_cut_point(msgs, keep_recent_tokens=20000)
        assert cut < len(msgs)  # the newest message survives

    def test_trailing_tool_result_only_backs_up_to_owning_call(self):
        # The newest turn is Assistant(tool_call) + ToolResult, and the budget
        # alone grabs only the result. The cut must back up to the owning
        # assistant message so the kept tail never begins with an orphaned
        # result (the old clamp-forward lost the result entirely).
        msgs = [
            _user("older"),
            _assistant("a" * 40_000),
            AssistantMessage(
                content=[ToolCallContent(id="t1", name="Read", arguments={"file_path": "f"})],
                stop_reason="tool_use",
            ),
            ToolResultMessage(
                tool_call_id="t1", tool_name="Read", content=[TextContent(text="x" * 200_000)]
            ),
        ]
        cut = find_cut_point(msgs, keep_recent_tokens=4000)
        assert 0 < cut < len(msgs)
        # Tail starts on the assistant message that owns the result, not the
        # result itself.
        assert isinstance(msgs[cut], AssistantMessage)
        assert msgs[cut].tool_calls and msgs[cut].tool_calls[0].id == "t1"
        # The result is preserved in the kept tail.
        assert any(isinstance(m, ToolResultMessage) for m in msgs[cut:])


def _to_llm(msgs):
    out = []
    for m in msgs:
        if hasattr(m, "role"):
            out.append(m)
    return out


class _SummaryProvider:
    """Yields a single text completion for the compaction summary call."""

    def __init__(self, text: str):
        self._text = text
        self.calls = 0

    async def stream(self, system_prompt, messages, tools, *, model="", max_tokens=16384):
        from istota.llm.provider import StreamDone

        self.calls += 1
        yield StreamDone(message=AssistantMessage(content=[TextContent(text=self._text)]))


class TestCompactMessages:
    @pytest.mark.asyncio
    async def test_generates_summary_text(self):
        provider = _SummaryProvider("## Goal\nDo the thing")
        msgs = [_user("please do the thing"), _assistant("working")]
        summary, details = await compact_messages(
            msgs, None, None, provider, "m", _to_llm
        )
        assert "Goal" in summary
        assert provider.calls == 1
        assert isinstance(details, CompactionDetails)

    @pytest.mark.asyncio
    async def test_tracks_file_operations_across_cycles(self):
        provider = _SummaryProvider("summary")
        msgs = [
            AssistantMessage(
                content=[
                    ToolCallContent(id="r", name="Read", arguments={"file_path": "/a.py"}),
                    ToolCallContent(id="w", name="Write", arguments={"file_path": "/b.py"}),
                ],
                stop_reason="tool_use",
            ),
        ]
        prev = CompactionDetails(read_files=["/old.py"], modified_files=[])
        _summary, details = await compact_messages(
            msgs, "prev summary", prev, provider, "m", _to_llm
        )
        assert "/a.py" in details.read_files
        assert "/old.py" in details.read_files  # carried forward
        assert "/b.py" in details.modified_files

    @pytest.mark.asyncio
    async def test_failure_returns_previous_summary(self):
        class _BoomProvider:
            async def stream(self, *a, **k):
                raise RuntimeError("network down")
                yield  # pragma: no cover

        summary, _details = await compact_messages(
            [_user("hi")], "earlier summary", None, _BoomProvider(), "m", _to_llm
        )
        assert summary == "earlier summary"
