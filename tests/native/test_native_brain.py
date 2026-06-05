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


class TestTimeout:
    def test_wall_clock_timeout_aborts(self, tmp_path):
        # A provider that streams forever must be stopped at the task deadline,
        # tagged stop_reason="timeout" — not run unbounded (which would let the
        # scheduler reclaim and double-run the task).
        class _ForeverProvider:
            async def stream(
                self, system_prompt, messages, tools, *, model="", max_tokens=16384
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
                self, system_prompt, messages, tools, *, model="", max_tokens=16384
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
