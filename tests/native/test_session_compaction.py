"""Context compaction: thresholds, cut points, incremental summaries.

The last two classes are the ISSUE-375 regression: a forced proactive cut
through ``NativeBrain.execute``, proving Istota's standing instructions are
supplied again on the far side of it because they live in
``AgentContext.system_prompt`` rather than at index 0 of ``ctx.messages``.
"""

import json

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


# --------------------------------------------------------------------------- #
# ISSUE-375: the standing instructions survive a real proactive cut
# --------------------------------------------------------------------------- #

SYSTEM_SENTINEL = "ISTOTA-SYSTEM-SENTINEL-9c41f2"
REQUEST_SENTINEL = "USER-REQUEST-SENTINEL-4ab7e0"


def _bulky_tool_turn() -> AssistantMessage:
    """A turn that both continues the loop and trips the cut.

    The tool call is what makes the loop run ``prepare_next_turn`` at all; the
    reported usage is what trips ``should_compact`` against the tiny window; and
    the bulky narration is what makes the walk back from the newest reach the
    recent budget before index 0, so the cut lands *after* the initial user
    message rather than returning 0.
    """
    return AssistantMessage(
        content=[
            TextContent(text="z" * 20_000),
            ToolCallContent(
                id="c1",
                name="Write",
                arguments={"file_path": "out.txt", "content": "hi"},
            ),
        ],
        usage=Usage(input_tokens=5000, output_tokens=10),
        stop_reason="tool_use",
    )


def _summary_turn() -> AssistantMessage:
    # The compaction summary call draws from the same script. Its text names
    # neither sentinel, which is what lets the post-cut assertions below read an
    # absence as "the initial user message is gone" rather than as "the
    # summarizer happened not to quote it".
    return AssistantMessage(
        content=[TextContent(text="## Goal\nwrite a file")], stop_reason="end_turn"
    )


def _final_turn() -> AssistantMessage:
    return AssistantMessage(
        content=[TextContent(text="done")],
        usage=Usage(input_tokens=20, output_tokens=5),
        stop_reason="end_turn",
    )


def _read_session_log(root) -> list[dict]:
    files = sorted((root / "alice").glob("*.jsonl"))
    assert files, f"no session log was written under {root / 'alice'}"
    assert len(files) == 1, f"expected one session log, found {files}"
    return [
        json.loads(line)
        for line in files[0].read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


class TestTheSystemHalfSurvivesAProactiveCut:
    """ISSUE-375, end to end, against a cut that really happens.

    ``executor.build_prompt()`` used to return one string holding Istota's
    identity, emissaries, persona, tool descriptions, skill inventory, rules and
    the user's request, and ``NativeBrain`` put that string at index 0 of
    ``ctx.messages``. ``find_cut_point`` walks back from the newest, so index 0
    is the first thing any compaction drops — and the model then ran on for the
    rest of a long task with a model-written summary where its standing
    instructions had been.

    A unit test over ``_system_prompt_parts`` proves the file is read. It cannot
    prove this, because the fault was never in the reading: it was in *where the
    text lives*, and only a real cut moves that boundary. So this forces a
    proactive compaction through ``NativeBrain.execute``.

    ``TestThePreSplitShapeLosesIt`` below is the control: the same run with the
    same text carried on ``req.prompt`` instead, which is the shape ISSUE-375
    describes, and it loses the sentinel at the same cut.
    """

    def _composed(self, tmp_path):
        # The sentinel leads the file, so the session-log assertion below cannot
        # start passing or failing on `max_content_chars` head-and-tail
        # truncation — a policy value this test never names. The `truncated`
        # assertion is the second half of that.
        path = tmp_path / "task_4471_system_prompt.txt"
        path.write_text(
            f"{SYSTEM_SENTINEL}\nYou are Istota, a helpful assistant bot.\n"
            "## Important rules\n1. Only access resources that belong to alice.\n",
            encoding="utf-8",
        )
        return path

    def _run(self, tmp_path, *, composed, prompt):
        from istota.brain import BrainRequest
        from istota.brain.native import NativeBrain
        from istota.config import NativeBrainConfig, SessionLogConfig

        from ._mock_provider import MockProvider

        root = tmp_path / "logs"
        provider = MockProvider([_bulky_tool_turn(), _summary_turn(), _final_turn()])
        brain = NativeBrain(
            NativeBrainConfig(
                model="claude-sonnet-4-6",
                # A window small enough that the first turn's reported usage
                # trips `should_compact`, and a recent budget the bulky turn
                # clears on its own.
                context_window=100,
                compaction_keep_recent_tokens=3000,
                session_log=SessionLogConfig(enabled=True, dir=str(root)),
            ),
            provider=provider,
        )
        req = BrainRequest(
            prompt=prompt,
            allowed_tools=["Write"],
            cwd=tmp_path,
            env={},
            timeout_seconds=30,
            model="claude-sonnet-4-6",
            task_id=4471,
            attempt=1,
            user_id="alice",
            source_type="talk",
            conversation_token="a1b2c3d4",
            composed_system_prompt_path=composed,
        )
        result = brain.execute(req)
        return result, provider, _read_session_log(root)

    def _calls(self, provider):
        """The three provider calls, named.

        The summary call is the one compaction makes: no system prompt and no
        tools. Splitting on that rather than on an index means a fourth call
        appearing later fails the count assertion instead of silently shifting
        which call is being asserted about.
        """
        assert len(provider.calls) == 3, provider.calls
        main = [c for c in provider.calls if c["tools"]]
        summary = [c for c in provider.calls if not c["tools"]]
        assert len(main) == 2, "expected a first turn and a post-compaction turn"
        assert len(summary) == 1, "expected exactly one compaction summary call"
        assert summary[0]["system_prompt"] == ""
        return main[0], summary[0], main[1]

    def _compaction_record(self, records):
        cuts = [r for r in records if r["type"] == "compaction"]
        assert len(cuts) == 1, f"expected exactly one compaction, got {len(cuts)}"
        assert cuts[0]["trigger"] == "proactive"
        # The cut really dropped the initial user message, which is the whole
        # premise: index 0 gone, the assistant turn and its result kept.
        assert cuts[0]["cut_index"] == 1
        assert cuts[0]["messages_dropped"] == 1
        return cuts[0]

    def test_the_system_sentinel_is_supplied_again_after_the_cut(self, tmp_path):
        result, provider, records = self._run(
            tmp_path, composed=self._composed(tmp_path), prompt=REQUEST_SENTINEL
        )
        assert result.success is True
        first, _summary, after = self._calls(provider)
        self._compaction_record(records)

        assert SYSTEM_SENTINEL in first["system_prompt"]
        assert SYSTEM_SENTINEL in after["system_prompt"]
        # Verbatim, not merely present: the fix is that compaction never reaches
        # `AgentContext.system_prompt` at all, so the two are the same bytes.
        assert after["system_prompt"] == first["system_prompt"]

    def test_the_system_sentinel_never_enters_the_compactable_history(self, tmp_path):
        _result, provider, records = self._run(
            tmp_path, composed=self._composed(tmp_path), prompt=REQUEST_SENTINEL
        )
        first, summary, after = self._calls(provider)
        cut = self._compaction_record(records)

        # Asserted as a pairing. Every line below is an absence, and an absence
        # alone is vacuous here: with the composed part removed from
        # `_system_prompt_parts` the file is never read at all, so all four
        # hold for the wrong reason and this test stays green through the exact
        # regression it names. Measured — that removal leaves the four absences
        # passing and only this line red.
        assert SYSTEM_SENTINEL in first["system_prompt"]
        # Not in the initial user message …
        assert SYSTEM_SENTINEL not in str(first["messages"])
        # … so it cannot be in what the summarizer was handed …
        assert SYSTEM_SENTINEL not in str(summary["messages"])
        # … nor in the summary that replaced the cut prefix …
        assert SYSTEM_SENTINEL not in cut["summary"]
        # … nor in the `CompactionSummaryMessage` the next turn carries.
        assert SYSTEM_SENTINEL not in str(after["messages"])
        assert "[Summary of earlier conversation]" in str(after["messages"])

    def test_the_request_sentinel_is_the_half_the_cut_reclaims(self, tmp_path):
        """The user half behaves as before: present on turn one, summarized away.

        This is also what stops the assertions above being vacuous. A run whose
        compaction never fired would show the system sentinel on both turns too
        — because nothing was cut. The request sentinel leaving is the evidence
        that the cut this test forces actually happened.
        """
        _result, provider, records = self._run(
            tmp_path, composed=self._composed(tmp_path), prompt=REQUEST_SENTINEL
        )
        first, summary, after = self._calls(provider)
        self._compaction_record(records)

        assert REQUEST_SENTINEL in str(first["messages"])
        assert REQUEST_SENTINEL not in first["system_prompt"]
        # The summarizer saw it — carrying it forward in reduced form is what
        # the summary is for.
        assert REQUEST_SENTINEL in str(summary["messages"])
        # And the scripted summary does not repeat it, so its absence here is
        # the cut rather than the summarizer's wording.
        assert REQUEST_SENTINEL not in str(after["messages"])

    def test_the_session_log_records_the_surviving_prompt_and_its_source(
        self, tmp_path
    ):
        _result, _provider, records = self._run(
            tmp_path, composed=self._composed(tmp_path), prompt=REQUEST_SENTINEL
        )
        context = records[1]
        assert context["type"] == "context"
        assert SYSTEM_SENTINEL in context["system_prompt"]
        assert context["system_prompt_source"] == "builtin+composed"
        # The sentinel leads the file and the record is nowhere near
        # `max_content_chars`, so nothing here rests on that policy value.
        assert context.get("truncated") in (None, False)


class TestThePreSplitShapeLosesIt:
    """The control for the class above, and the shape ISSUE-375 reports.

    Same window, same script, same cut — but the standing instructions ride on
    ``req.prompt`` the way they did before the split, so they are the text at
    index 0 of ``ctx.messages`` and the cut takes them. Without this, every
    assertion above could be read as "compaction did nothing", and a test
    asserting against an artifact has to be shown able to fail.
    """

    def test_instructions_on_the_user_turn_do_not_survive_the_cut(self, tmp_path):
        runner = TestTheSystemHalfSurvivesAProactiveCut()
        _result, provider, records = runner._run(
            tmp_path,
            composed=None,
            prompt=f"{SYSTEM_SENTINEL}\nYou are Istota.\n\n{REQUEST_SENTINEL}",
        )
        first, _summary, after = runner._calls(provider)
        runner._compaction_record(records)

        # It was there on turn one, in the message compaction is allowed to eat.
        assert SYSTEM_SENTINEL in str(first["messages"])
        assert SYSTEM_SENTINEL not in first["system_prompt"]
        # And after one cut the model runs on without it. That is the defect.
        assert SYSTEM_SENTINEL not in str(after["messages"])
        assert SYSTEM_SENTINEL not in after["system_prompt"]
