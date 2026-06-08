"""NativeBrain — the Brain-protocol adapter over the three-layer stack."""

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

from istota.brain import BrainRequest, make_brain
from istota.brain.native import NativeBrain
from istota.config import BrainConfig, NativeBrainConfig
from istota.llm.provider import StreamDone, StreamError, StreamStart, TextDelta
from istota.llm.types import AssistantMessage, TextContent, ToolCallContent, Usage

from ._mock_provider import MockProvider


def _req(prompt: str, cwd: Path, tools: list[str] | None = None) -> BrainRequest:
    return BrainRequest(
        prompt=prompt,
        allowed_tools=tools if tools is not None else [],
        cwd=cwd,
        env={},
        timeout_seconds=30,
        model="claude-sonnet-4-6",
    )


def _brain(provider, **cfg) -> NativeBrain:
    config = NativeBrainConfig(model="claude-sonnet-4-6", **cfg)
    return NativeBrain(config, provider=provider)


class TestTextCompletion:
    def test_simple_completion(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="Hello there.")],
                    usage=Usage(input_tokens=100, output_tokens=10),
                    stop_reason="end_turn",
                )
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is True
        assert result.result_text == "Hello there."
        assert result.stop_reason == "completed"

    def test_usage_accumulated(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="done")],
                    usage=Usage(input_tokens=200, output_tokens=20),
                    stop_reason="end_turn",
                )
            ]
        )
        result = _brain(provider).execute(_req("go", tmp_path))
        assert result.usage is not None
        assert result.usage.input_tokens == 200
        assert result.usage.output_tokens == 20
        assert result.usage.turns == 1

    def test_system_prompt_from_custom_file(self, tmp_path):
        sysfile = tmp_path / "sys.md"
        sysfile.write_text("You are a test bot.")
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        req = _req("hi", tmp_path)
        req.custom_system_prompt_path = sysfile
        _brain(provider).execute(req)
        assert provider.calls[0]["system_prompt"] == "You are a test bot."


class TestToolUse:
    def test_write_tool_then_completion(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[
                        ToolCallContent(
                            id="c1",
                            name="Write",
                            arguments={"file_path": "out.txt", "content": "hi"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(
                    content=[TextContent(text="Wrote the file.")],
                    stop_reason="end_turn",
                ),
            ]
        )
        req = _req("write a file", tmp_path, tools=["Write", "Read"])
        result = _brain(provider).execute(req)
        assert result.success is True
        assert result.result_text == "Wrote the file."
        assert (tmp_path / "out.txt").read_text() == "hi"

    def test_trace_and_actions(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[
                        ToolCallContent(
                            id="c1",
                            name="Write",
                            arguments={"file_path": "out.txt", "content": "hi"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(
                    content=[TextContent(text="Done.")], stop_reason="end_turn"
                ),
            ]
        )
        req = _req("write", tmp_path, tools=["Write"])
        result = _brain(provider).execute(req)
        trace = json.loads(result.execution_trace)
        assert any(e["type"] == "tool" for e in trace)
        assert any(e["type"] == "text" for e in trace)
        actions = json.loads(result.actions_taken)
        assert any("out.txt" in a for a in actions)

    def test_only_allowed_tools_exposed(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        req = _req("hi", tmp_path, tools=["Read"])
        _brain(provider).execute(req)
        tool_names = {t.name for t in provider.calls[0]["tools"]}
        assert tool_names == {"Read"}


class TestErrorAndStops:
    def test_error_stop(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="")],
                    stop_reason="error",
                    error_message="HTTP 400: bad",
                )
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is False
        assert result.stop_reason == "error"

    def test_max_turns_stops(self, tmp_path):
        # Model keeps calling a tool forever; max_turns caps it.
        turns = [
            AssistantMessage(
                content=[
                    ToolCallContent(
                        id=f"c{i}", name="Read", arguments={"file_path": "README"}
                    )
                ],
                stop_reason="tool_use",
            )
            for i in range(20)
        ]
        provider = MockProvider(turns)
        req = _req("loop", tmp_path, tools=["Read"])
        result = _brain(provider, max_turns=3).execute(req)
        assert result.stop_reason == "max_turns"
        # Only ran up to the cap, not all 20 scripted turns.
        assert len(provider.calls) <= 4

    def test_cancellation(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="first")], stop_reason="end_turn"
                ),
            ]
        )

        cancelled = {"v": False}

        def cancel_check():
            return cancelled["v"]

        req = _req("hi", tmp_path)
        req.cancel_check = cancel_check
        cancelled["v"] = True  # cancelled before the run starts
        result = _brain(provider).execute(req)
        assert result.stop_reason == "cancelled"
        assert result.success is False

    def test_cancellation_emits_scheduler_magic_string(self, tmp_path):
        # The executor drops stop_reason; the scheduler routes cancellation by
        # matching result_text == "Cancelled by user" exactly. NativeBrain must
        # emit that string or a cancelled task gets retried.
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="x")], stop_reason="end_turn")]
        )
        req = _req("hi", tmp_path)
        req.cancel_check = lambda: True
        result = _brain(provider).execute(req)
        assert result.result_text == "Cancelled by user"

    def test_error_surfaces_message_into_result_text(self, tmp_path):
        # The scheduler classifies policy refusals / errors from result_text; an
        # empty string reads as a generic failure. The provider's error_message
        # must reach result_text.
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="")],
                    stop_reason="error",
                    error_message="API Error: 400 content policy refused",
                )
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is False
        assert result.stop_reason == "error"
        assert "content policy refused" in result.result_text

    def test_cancel_check_exception_does_not_crash_run(self, tmp_path):
        # A transient cancel_check failure (e.g. SQLite lock) must not abort the
        # run or crash the brain — it's treated as "not cancelled".
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="done")], stop_reason="end_turn")]
        )

        def boom():
            raise RuntimeError("db locked")

        req = _req("hi", tmp_path)
        req.cancel_check = boom
        result = _brain(provider).execute(req)
        assert result.success is True
        assert result.result_text == "done"


class TestProgressStreaming:
    """ISSUE-111: in-progress updates must reach the on_progress sink.

    The scheduler's Talk-edit callback calls ``asyncio.run()`` internally. The
    native brain drives ``on_progress`` from inside its own ``asyncio.run`` event
    loop, so calling the callback directly would invoke ``asyncio.run()`` from a
    running loop → RuntimeError, silently dropping every update. The brain must
    invoke the sync callback off the loop so its ``asyncio.run`` works.
    """

    def _scheduler_like_callback(self, log):
        """Mimic the scheduler progress callback: it calls asyncio.run()."""

        def callback(event):
            log.append(("received", type(event).__name__))

            async def _edit():
                return True

            asyncio.run(_edit())  # exactly what edit_talk_message does
            log.append(("edited", type(event).__name__))

        return callback

    def test_tool_and_text_progress_reach_sink(self, tmp_path):
        provider = MockProvider(
            [
                # Turn 1: intermediate text + a tool call.
                AssistantMessage(
                    content=[
                        TextContent(text="Working on it."),
                        ToolCallContent(
                            id="c1",
                            name="Write",
                            arguments={"file_path": "out.txt", "content": "hi"},
                        ),
                    ],
                    stop_reason="tool_use",
                ),
                # Turn 2: final answer — its text becomes result_text.
                AssistantMessage(
                    content=[TextContent(text="Wrote the file.")],
                    stop_reason="end_turn",
                ),
            ]
        )
        log: list[tuple[str, str]] = []
        req = _req("write a file", tmp_path, tools=["Write"])
        req.on_progress = self._scheduler_like_callback(log)
        result = _brain(provider).execute(req)

        assert result.success is True
        assert result.result_text == "Wrote the file."
        # Each received event must also be fully processed by the callback —
        # i.e. its internal asyncio.run() completed, not swallowed as a
        # RuntimeError ("asyncio.run() cannot be called from a running loop").
        received = [name for kind, name in log if kind == "received"]
        edited = [name for kind, name in log if kind == "edited"]
        assert "ToolUseEvent" in received
        assert "ToolEndEvent" in received  # NativeBrain emits tool completion
        # The intermediate "Working on it." now reaches the sink as a streamed
        # TextDeltaEvent (token-level answer streaming), and the redundant
        # whole-turn TextEvent flush is suppressed (no double-render).
        assert "TextDeltaEvent" in received
        assert "TextEvent" not in received
        assert edited == received  # every callback ran to completion

    def test_streaming_suppresses_whole_turn_text_event(self, tmp_path):
        # With delta streaming the answer arrives incrementally as
        # TextDeltaEvents; the whole-turn TextEvent flush must be suppressed so a
        # stream surface doesn't render the answer twice. result_text is intact.
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="The answer is 42.")],
                    stop_reason="end_turn",
                ),
            ]
        )
        log: list[tuple[str, str]] = []
        req = _req("question", tmp_path)
        req.on_progress = self._scheduler_like_callback(log)
        result = _brain(provider).execute(req)

        assert result.success is True
        assert result.result_text == "The answer is 42."
        received = [name for kind, name in log if kind == "received"]
        assert "TextDeltaEvent" in received  # answer streamed as deltas
        assert "TextEvent" not in received   # whole-turn flush suppressed

    def test_progress_callback_exception_does_not_crash_run(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="done")], stop_reason="end_turn")]
        )

        def boom(event):
            raise RuntimeError("talk server down")

        req = _req("hi", tmp_path)
        req.on_progress = boom
        result = _brain(provider).execute(req)
        assert result.success is True
        assert result.result_text == "done"


class TestAnswerStreaming:
    """Stage 3 — provider TextDeltas are forwarded as ordered TextDeltaEvents."""

    def test_text_deltas_forwarded_in_order(self, tmp_path):
        from istota.brain import TextDeltaEvent, TextEvent

        class _DeltaProvider:
            async def stream(
                self, system_prompt, messages, tools, *, model="", max_tokens=16384, **kw
            ) -> AsyncIterator:
                yield StreamStart()
                for frag in ["Hel", "lo, ", "world"]:
                    yield TextDelta(text=frag)
                yield StreamDone(
                    message=AssistantMessage(
                        content=[TextContent(text="Hello, world")],
                        stop_reason="end_turn",
                    )
                )

        captured: list = []
        req = _req("hi", tmp_path)
        req.on_progress = lambda ev: captured.append(ev)
        result = _brain(_DeltaProvider()).execute(req)

        deltas = [e.text for e in captured if isinstance(e, TextDeltaEvent)]
        assert deltas == ["Hel", "lo, ", "world"]  # forwarded in arrival order
        # No whole-turn TextEvent — the deltas carried it (no double-render).
        assert not any(isinstance(e, TextEvent) for e in captured)
        # The canonical result still equals the assembled final-turn text.
        assert result.result_text == "Hello, world"


class TestTimeout:
    def test_wall_clock_timeout_aborts(self, tmp_path):
        # A provider that streams forever must be stopped at the task deadline,
        # tagged stop_reason="timeout" — not run unbounded (which would let the
        # scheduler reclaim and double-run the task).
        class _ForeverProvider:
            async def stream(
                self, system_prompt, messages, tools, *, model="", max_tokens=16384, **kw
            ) -> AsyncIterator:
                yield StreamStart()
                for _ in range(100000):
                    yield TextDelta(text="x")
                    await asyncio.sleep(0.02)

        req = _req("hang", tmp_path)
        req.timeout_seconds = 1
        result = _brain(_ForeverProvider()).execute(req)
        assert result.success is False
        assert result.stop_reason == "timeout"
        assert "timed out" in result.result_text


class TestRetryProvider:
    def test_streamstart_before_error_is_still_retried(self, tmp_path, monkeypatch):
        # _RetryingProvider must not treat StreamStart as a committed turn: a
        # transient error after StreamStart (but before any real delta) is
        # retryable. Regression for the StreamStart-commits-the-turn bug.
        from istota.brain import native as native_mod

        monkeypatch.setattr(native_mod, "_API_RETRY_BASE_DELAY", 0.0)

        calls = {"n": 0}

        class _Flaky:
            async def stream(
                self, system_prompt, messages, tools, *, model="", max_tokens=16384, **kw
            ) -> AsyncIterator:
                calls["n"] += 1
                yield StreamStart()
                if calls["n"] == 1:
                    yield StreamError(
                        message=AssistantMessage(
                            stop_reason="error", error_message="HTTP 503: overloaded"
                        )
                    )
                else:
                    yield StreamDone(
                        message=AssistantMessage(
                            content=[TextContent(text="recovered")],
                            stop_reason="end_turn",
                        )
                    )

        result = _brain(_Flaky()).execute(_req("hi", tmp_path))
        assert calls["n"] == 2  # retried despite the leading StreamStart
        assert result.success is True
        assert result.result_text == "recovered"


class TestCacheTelemetry:
    def test_hit_rate_logged_at_task_end(self, tmp_path, caplog):
        import logging

        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="done")],
                    usage=Usage(
                        input_tokens=100,
                        output_tokens=10,
                        cache_read_tokens=40,
                        cache_write_tokens=25,
                    ),
                    stop_reason="end_turn",
                )
            ]
        )
        with caplog.at_level(logging.INFO, logger="istota.brain.native"):
            _brain(provider).execute(_req("hi", tmp_path))
        line = next((r.message for r in caplog.records if "cache" in r.message), None)
        assert line is not None
        assert "hit_rate=" in line
        assert "read=40" in line
        assert "input=100" in line
        # The documented cache_creation → cache_write_tokens mapping reaches the
        # task-end footer (closes the end-to-end loop, not just the SSE parse).
        assert "write=25" in line

    def test_hit_rate_clamped_at_100(self, tmp_path, caplog):
        # A non-conforming provider can report cache reads outside prompt_tokens;
        # the footer must not show a >100% rate.
        import logging

        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="done")],
                    usage=Usage(
                        input_tokens=10, output_tokens=5, cache_read_tokens=40
                    ),
                    stop_reason="end_turn",
                )
            ]
        )
        with caplog.at_level(logging.INFO, logger="istota.brain.native"):
            _brain(provider).execute(_req("hi", tmp_path))
        line = next((r.message for r in caplog.records if "cache" in r.message), None)
        assert line is not None
        assert "hit_rate=100.0%" in line

    def test_zero_input_no_divide_by_zero(self, tmp_path, caplog):
        import logging

        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="done")],
                    usage=Usage(input_tokens=0, output_tokens=0),
                    stop_reason="end_turn",
                )
            ]
        )
        with caplog.at_level(logging.INFO, logger="istota.brain.native"):
            result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is True  # did not crash on divide-by-zero
        line = next((r.message for r in caplog.records if "cache" in r.message), None)
        assert line is not None
        assert "hit_rate=0" in line


class TestFactory:
    def test_make_brain_native(self):
        cfg = BrainConfig(kind="native", native=NativeBrainConfig(model="claude-sonnet-4-6"))
        brain = make_brain(cfg)
        assert isinstance(brain, NativeBrain)

    def test_openai_compat_provider_does_not_translate_aliases(self):
        # A non-Anthropic endpoint gets explicit ids, not translated Anthropic
        # aliases (see test_native_resolution.py).
        brain = NativeBrain(NativeBrainConfig(model="qwen-x"), provider=object())
        assert brain.resolve_model_name("opus") == "opus"
