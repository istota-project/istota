"""Context compaction: thresholds, cut points, incremental summaries."""

import pytest

from istota.llm.types import (
    AssistantMessage,
    ImageContent,
    TextContent,
    ToolCallContent,
    ToolResultMessage,
    Usage,
    UserMessage,
)
from istota.session.compaction import (
    _serialize_for_summary,
    compact_messages,
    derive_keep_recent_tokens,
    derive_reserve_tokens,
    estimate_context_tokens,
    find_cut_point,
    find_image_message,
    plan_image_pin,
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

    @pytest.mark.asyncio
    async def test_input_truncated_to_max_chars(self):
        # NB-10: the serialized conversation fed to the summarizer is bounded so
        # the summary request can't itself overflow.
        from istota.llm.provider import StreamDone

        class _RecordingProvider:
            def __init__(self):
                self.prompt = ""

            async def stream(self, system_prompt, messages, tools, *, model="", max_tokens=16384):
                self.prompt = messages[-1].content[0].text
                yield StreamDone(message=AssistantMessage(content=[TextContent(text="S")]))

        provider = _RecordingProvider()
        # Ten big messages → a large serialized transcript.
        msgs = [_user("Q" * 20000) for _ in range(10)]
        await compact_messages(
            msgs, None, None, provider, "m", _to_llm, max_input_chars=10000
        )
        assert "elided to fit" in provider.prompt
        # The elided conversation section is bounded (plus fixed scaffolding).
        assert len(provider.prompt) < 30000


class TestImageLossNotice:
    """An ``ImageContent`` block matched neither serializer branch, so the
    summarizer never learned an image had been in the conversation — and the
    model carried on a multi-turn task believing it could still see one. The
    notice is the floor; the pin below is what keeps the capability."""

    @pytest.mark.asyncio
    async def test_the_summary_prompt_names_each_lost_image(self):
        from istota.llm.provider import StreamDone

        class _RecordingProvider:
            def __init__(self):
                self.prompt = ""

            async def stream(self, system_prompt, messages, tools, *, model="", max_tokens=16384):
                self.prompt = messages[-1].content[0].text
                yield StreamDone(message=AssistantMessage(content=[TextContent(text="S")]))

        provider = _RecordingProvider()
        msgs = [
            UserMessage(
                content=[
                    TextContent(text="what is in these?"),
                    ImageContent(media_type="image/png", data="AAAA", display_name="shot.png"),
                    ImageContent(media_type="image/jpeg", data="BBBB", display_name="photo.jpg"),
                ]
            ),
            _assistant("a screenshot and a photo"),
        ]
        await compact_messages(msgs, None, None, provider, "m", _to_llm)

        assert "[image shot.png — no longer in context]" in provider.prompt
        assert "[image photo.jpg — no longer in context]" in provider.prompt
        # The base64 payload is never part of a summary prompt.
        assert "AAAA" not in provider.prompt

    def test_a_tool_result_image_is_named_too(self):
        from istota.session.compaction import _serialize_for_summary

        text = _serialize_for_summary(
            [
                ToolResultMessage(
                    tool_call_id="c1",
                    tool_name="screenshot",
                    content=[
                        TextContent(text="captured"),
                        ImageContent(media_type="image/png", data="CCCC"),
                    ],
                )
            ]
        )
        assert "captured" in text
        assert "no longer in context" in text
        assert "CCCC" not in text


class TestFindImageMessage:
    def test_returns_the_first_image_bearing_user_message(self):
        img = UserMessage(
            content=[
                TextContent(text="look"),
                ImageContent(media_type="image/png", data="AAAA", display_name="a.png"),
            ]
        )
        assert find_image_message([_user("hi"), img, _assistant("ok")]) is img

    def test_none_when_no_message_carries_an_image(self):
        assert find_image_message([_user("hi"), _assistant("ok")]) is None

    def test_a_tool_result_image_is_not_pinned(self):
        # A ``ToolResultMessage`` must follow its ``tool_call``. Hoisting one to
        # the head of the compacted list would strand it and 400 the request.
        result = ToolResultMessage(
            tool_call_id="c1",
            tool_name="screenshot",
            content=[ImageContent(media_type="image/png", data="AAAA")],
        )
        assert find_image_message([result]) is None


class TestPlanImagePin:
    def _prefix(self, *, images=1):
        return [
            UserMessage(
                content=[
                    TextContent(text="PROMPT-MARKER"),
                    *[
                        ImageContent(
                            media_type="image/png",
                            data="AAAA",
                            display_name=f"shot{i}.png",
                        )
                        for i in range(images)
                    ],
                ]
            ),
            _assistant("working"),
        ]

    def test_the_pin_holds_the_blocks_and_a_label_not_the_prompt(self):
        pin, _summary_input = plan_image_pin(self._prefix())
        assert isinstance(pin, UserMessage)
        assert isinstance(pin.content[0], TextContent)
        assert "PROMPT-MARKER" not in pin.content[0].text
        assert [c.display_name for c in pin.content[1:]] == ["shot0.png"]

    def test_the_summary_input_keeps_the_text_and_drops_the_pinned_blocks(self):
        prefix = self._prefix()
        _pin, summary_input = plan_image_pin(prefix)
        rendered = _serialize_for_summary(summary_input)
        assert "PROMPT-MARKER" in rendered
        assert "no longer in context" not in rendered
        # And the caller's own list is untouched.
        assert any(isinstance(c, ImageContent) for c in prefix[0].content)

    def test_a_tool_result_image_still_reads_as_lost(self):
        # It is never pinned, so the notice about it is true.
        prefix = [
            *self._prefix(),
            ToolResultMessage(
                tool_call_id="c1",
                tool_name="screenshot",
                content=[ImageContent(media_type="image/png", data="BBBB")],
            ),
        ]
        _pin, summary_input = plan_image_pin(prefix)
        assert "no longer in context" in _serialize_for_summary(summary_input)

    def test_nothing_to_pin_returns_the_prefix_unchanged(self):
        prefix = [_user("hi"), _assistant("ok")]
        pin, summary_input = plan_image_pin(prefix)
        assert pin is None
        assert summary_input is prefix

    def test_a_pin_too_large_for_the_recent_budget_is_refused(self):
        # `find_cut_point` returns 0 when the walk back does not reach
        # `keep_recent_tokens` before index 0, so a pin that swallows that
        # budget on its own turns compaction into a permanent no-op and the
        # request goes out over-window.
        pin, summary_input = plan_image_pin(self._prefix(images=20), 4)
        assert pin is None
        assert "no longer in context" in _serialize_for_summary(summary_input)

    def test_a_zero_budget_asks_for_no_size_check(self):
        pin, _ = plan_image_pin(self._prefix(images=20), 0)
        assert pin is not None
