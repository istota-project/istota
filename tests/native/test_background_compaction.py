"""Background compaction in the native brain (stage 3 of the pi 1.x round).

A summary starts below the blocking threshold and is placed at a later turn
boundary, so a long task does not stall on the summary call. The provider here
holds each summary call on an `asyncio.Event` the test controls, which is what
lets a test say whether a turn went out while the summary was still running.

Sizing, shared by every run below: a 10,000-token window derives a 2,500-token
reserve, so the blocking threshold is 7,500; with
`compaction_background_tokens = 5000` the background threshold is 2,500. A
tool turn reporting 4,000 input tokens sits between the two, and one reporting
8,000 is past the blocking threshold.
"""

from __future__ import annotations

import asyncio
import json
import logging

import pytest

from istota.llm.provider import (
    StreamDone,
    StreamError,
    StreamStart,
    TextDelta,
    ToolCallDelta,
)
from istota.llm.types import (
    AssistantMessage,
    TextContent,
    ToolCallContent,
    Usage,
)
from istota.session.compaction import derive_background_tokens

from ..live.native_eval import runner as eval_runner
from .test_session_compaction import (
    REQUEST_SENTINEL,
    _final_turn,
    _no_tool_server,  # noqa: F401 — the fixture, used by name below
    _read_session_log,
)


def _tool_turn(n: int, input_tokens: int) -> AssistantMessage:
    # Bulky enough that `find_cut_point` cuts after the initial user message
    # against a 3,000-token recent budget, and a distinct call per turn so
    # loop detection stays out of it.
    return AssistantMessage(
        content=[
            TextContent(text="z" * 20_000),
            ToolCallContent(
                id=f"c{n}",
                name="Write",
                arguments={"file_path": f"out{n}.txt", "content": "hi"},
            ),
        ],
        usage=Usage(input_tokens=input_tokens, output_tokens=10),
        stop_reason="tool_use",
    )


def _summary(text: str = "## Goal\nwrite files", input_tokens: int = 700):
    return AssistantMessage(
        content=[TextContent(text=text)],
        usage=Usage(input_tokens=input_tokens, output_tokens=40),
        stop_reason="end_turn",
    )


def _failed_summary() -> AssistantMessage:
    return AssistantMessage(
        stop_reason="error", error_message="HTTP 400: summary request refused"
    )


_OVERFLOW = AssistantMessage(
    stop_reason="error", error_message="API Error: 400 context length exceeded"
)


class _GatedProvider:
    """Main-model turns from one script, summary calls from another.

    A summary call is the one with no tools. One marked gated waits on
    `self.gate` before answering; an ungated one sets the gate as it answers,
    so a blocking summary can release a background one held behind it.
    `on_main` maps a main-call index to an async action run before that call
    answers. `events` is the order things happened in.
    """

    def __init__(self, main_turns, summaries, on_main=None):
        self._main = list(main_turns)
        self._summaries = list(summaries)
        self._on_main = dict(on_main or {})
        self._main_n = 0
        self._summary_n = 0
        self.gate = asyncio.Event()
        self.events: list[str] = []
        self.calls: list[dict] = []

    @property
    def summary_started(self) -> bool:
        return self._summary_n > 0

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
        prompt_caching=None,
    ):
        self.calls.append(
            {"system_prompt": system_prompt, "messages": list(messages),
             "tools": list(tools)}
        )
        if not tools:
            n = self._summary_n
            self._summary_n += 1
            if not self._summaries:
                raise AssertionError("ran out of scripted summaries")
            turn, gated = self._summaries.pop(0)
            self.events.append(f"summary{n}:start")
            if gated:
                try:
                    await self.gate.wait()
                except asyncio.CancelledError:
                    self.events.append(f"summary{n}:cancelled")
                    raise
            else:
                self.gate.set()
            self.events.append(f"summary{n}:end")
            yield StreamStart()
            for block in turn.content:
                if isinstance(block, TextContent):
                    yield TextDelta(text=block.text)
            if turn.stop_reason == "error":
                yield StreamError(message=turn)
            else:
                yield StreamDone(message=turn)
            return

        n = self._main_n
        self._main_n += 1
        self.events.append(f"main{n}")
        action = self._on_main.get(n)
        if action is not None:
            await action(self)
        if not self._main:
            raise AssertionError("ran out of scripted main turns")
        turn = self._main.pop(0)
        yield StreamStart()
        for block in turn.content:
            if isinstance(block, TextContent):
                yield TextDelta(text=block.text)
            elif isinstance(block, ToolCallContent):
                yield ToolCallDelta(
                    id=block.id,
                    name=block.name,
                    arguments_delta=json.dumps(block.arguments),
                )
        if turn.stop_reason == "error":
            yield StreamError(message=turn)
        else:
            yield StreamDone(message=turn)


async def _settle(provider):
    # Enough loop iterations for a woken summary task to run to completion.
    for _ in range(50):
        await asyncio.sleep(0)


async def _release(provider):
    provider.gate.set()
    await _settle(provider)


async def _release_later(provider):
    asyncio.get_running_loop().call_later(0.2, provider.gate.set)


async def _hold(provider):
    # Long enough that only an abort ends this call.
    await asyncio.sleep(10)


def _brain_and_request(tmp_path, provider, *, cancel_check=None, **overrides):
    from istota.brain import BrainRequest
    from istota.brain.native import NativeBrain
    from istota.config import NativeBrainConfig, SessionLogConfig

    fields = dict(
        model="claude-sonnet-4-6",
        context_window=10_000,
        compaction_keep_recent_tokens=3000,
        compaction_background=True,
        compaction_background_tokens=5000,
        session_log=SessionLogConfig(enabled=True, dir=str(tmp_path / "logs")),
    )
    fields.update(overrides)
    brain = NativeBrain(NativeBrainConfig(**fields), provider=provider)
    req = BrainRequest(
        prompt=REQUEST_SENTINEL,
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
        cancel_check=cancel_check,
    )
    return brain, req


def _run(tmp_path, provider, **kw):
    brain, req = _brain_and_request(tmp_path, provider, **kw)
    result = brain.execute(req)
    return result, _read_session_log(tmp_path / "logs")


def _run_direct(tmp_path, provider, **kw):
    """`_execute_async` inside a loop this test owns, so it can see what is left.

    `execute` goes through `asyncio.run`, which cancels whatever is still
    pending on the way out and so would hide a summary task left running.
    """
    brain, req = _brain_and_request(tmp_path, provider, **kw)

    async def _go():
        result = await brain._execute_async(req)
        current = asyncio.current_task()
        # One tick first: asyncio closes an abandoned async generator (the
        # cancelled summary's stream) through a task of its own, which ends on
        # the next iteration. What must not be here is the summary task.
        await asyncio.sleep(0)
        pending = [
            t for t in asyncio.all_tasks() if t is not current and not t.done()
        ]
        return result, pending

    return asyncio.run(_go())


def _compactions(records):
    return [r for r in records if r["type"] == "compaction"]


def _summary_calls(provider):
    return [c for c in provider.calls if not c["tools"]]


def _main_calls(provider):
    return [c for c in provider.calls if c["tools"]]


_SUMMARY_MARKER = "[Summary of earlier conversation]"


def _has_summary(call) -> bool:
    # The provider sees the converted wire messages, where a
    # `CompactionSummaryMessage` is a user turn led by this marker.
    return _SUMMARY_MARKER in str(call["messages"])


class TestDerivedBackgroundTokens:
    def test_an_eighth_of_the_window_capped(self):
        assert derive_background_tokens(10_000) == 1250
        assert derive_background_tokens(200_000) == 25_000
        assert derive_background_tokens(1_000_000) == 32_768
        assert derive_background_tokens(0) == 1


class TestConfig:
    def test_off_by_default(self):
        from istota.config import NativeBrainConfig

        cfg = NativeBrainConfig()
        assert cfg.compaction_background is False
        assert cfg.compaction_background_tokens == 0

    def test_read_from_toml(self, tmp_path):
        from istota.config import load_config

        path = tmp_path / "config.toml"
        path.write_text(
            "[brain.native]\n"
            "compaction_background = true\n"
            "compaction_background_tokens = 12000\n"
        )
        native = load_config(path).brain.native
        assert native.compaction_background is True
        assert native.compaction_background_tokens == 12000


@pytest.mark.usefixtures("_no_tool_server")
class TestStartAndPlace:
    def test_the_next_turn_goes_out_while_the_summary_runs(self, tmp_path):
        provider = _GatedProvider(
            [_tool_turn(1, 4000), _tool_turn(2, 4000), _final_turn()],
            [(_summary(), True)],
            on_main={1: _release},
        )
        result, records = _run(tmp_path, provider)
        assert result.success is True, result.result_text

        # The second turn was requested before the summary answered, and
        # carried the uncut context.
        assert provider.events.index("main1") < provider.events.index("summary0:end")
        first, second, third = _main_calls(provider)
        assert REQUEST_SENTINEL in str(second["messages"])
        assert not _has_summary(second)
        # Placed at the next boundary: the third turn runs on the summary.
        assert _has_summary(third)
        assert REQUEST_SENTINEL not in str(third["messages"])

        (record,) = _compactions(records)
        assert record["trigger"] == "background"
        assert record["ok"] is True
        assert record["placed"] is True
        assert record["cut_index"] == 1
        assert record["messages_dropped"] == 1
        assert record["summary"] == "## Goal\nwrite files"
        assert record["usage"]["input_tokens"] == 700
        # The summary's spend is in the task usage.
        assert result.usage.output_tokens == 10 + 10 + 5 + 40

    def test_off_means_no_summary_below_the_blocking_threshold(self, tmp_path):
        provider = _GatedProvider(
            [_tool_turn(1, 4000), _tool_turn(2, 4000), _final_turn()], []
        )
        result, records = _run(tmp_path, provider, compaction_background=False)
        assert result.success is True, result.result_text
        assert _summary_calls(provider) == []
        assert _compactions(records) == []

    def test_a_failed_summary_cuts_nothing_and_counts_toward_the_limit(
        self, tmp_path
    ):
        # Each failed summary is retried at the next boundary; after the second
        # failure no third is started.
        provider = _GatedProvider(
            [_tool_turn(1, 4000), _tool_turn(2, 4000), _tool_turn(3, 4000),
             _final_turn()],
            [(_failed_summary(), False), (_failed_summary(), False)],
            on_main={1: _settle, 2: _settle},
        )
        result, records = _run(tmp_path, provider)
        assert result.success is True, result.result_text
        assert len(_summary_calls(provider)) == 2
        assert not any(_has_summary(c) for c in _main_calls(provider))
        cuts = _compactions(records)
        assert [(c["trigger"], c["ok"], c["placed"]) for c in cuts] == [
            ("background", False, False),
            ("background", False, False),
        ]
        assert "summary request refused" in cuts[0]["error"]


@pytest.mark.usefixtures("_no_tool_server")
class TestTheBlockingThreshold:
    def test_waits_for_the_summary_in_flight_instead_of_starting_another(
        self, tmp_path, caplog
    ):
        provider = _GatedProvider(
            [_tool_turn(1, 4000), _tool_turn(2, 8000), _final_turn()],
            [(_summary(), True)],
            on_main={1: _release_later},
        )
        with caplog.at_level(logging.INFO, logger="istota.brain.native"):
            result, records = _run(tmp_path, provider)
        assert result.success is True, result.result_text
        assert "background summary in flight; awaiting it" in caplog.text

        assert len(_summary_calls(provider)) == 1
        (record,) = _compactions(records)
        assert record["trigger"] == "background"
        assert record["placed"] is True
        assert _has_summary(_main_calls(provider)[-1])

    def test_a_summary_outliving_the_wait_is_discarded_as_stale(
        self, tmp_path, monkeypatch
    ):
        # The wait is bounded by the attempt's remaining time; shortened here so
        # the blocking compaction runs while the background one is still held.
        # The blocking summary releases the gate as it answers, so the
        # background summary finishes after the context was already replaced.
        import istota.brain.native as native

        monkeypatch.setattr(native, "_background_await_budget", lambda _d: 0.05)
        provider = _GatedProvider(
            [_tool_turn(1, 4000), _tool_turn(2, 8000), _final_turn()],
            [
                (_summary("## Goal\nstale", input_tokens=600), True),
                (_summary("## Goal\nblocking", input_tokens=900), False),
            ],
            on_main={2: _settle},
        )
        result, records = _run(tmp_path, provider)
        assert result.success is True, result.result_text

        cuts = _compactions(records)
        assert [(c["trigger"], c["ok"], c["placed"]) for c in cuts] == [
            ("proactive", True, True),
            ("background", True, False),
        ]
        stale = cuts[1]
        assert stale["summary"] is None
        assert stale["messages_dropped"] == 0
        assert stale["usage"]["input_tokens"] == 600
        # Both summaries are paid for, the discarded one included.
        assert result.usage.billed_input_tokens == 4000 + 8000 + 20 + 600 + 900
        # The final turn runs on the blocking summary, not the stale one.
        final = str(_main_calls(provider)[-1]["messages"])
        assert final.count(_SUMMARY_MARKER) == 1
        assert "blocking" in final
        assert "stale" not in final
        # The eval runner counts only summaries that went into context.
        log_file = next((tmp_path / "logs" / "alice").glob("*.jsonl"))
        assert eval_runner.metrics_from_log(log_file, None)["compactions"] == 1


@pytest.mark.usefixtures("_no_tool_server")
class TestCancellation:
    def test_a_run_that_completes_leaves_no_summary_running(self, tmp_path):
        provider = _GatedProvider(
            [_tool_turn(1, 4000), _final_turn()], [(_summary(), True)]
        )
        result, pending = _run_direct(tmp_path, provider)
        assert result.success is True, result.result_text
        assert pending == []
        assert "summary0:cancelled" in provider.events
        # A cancelled summary reported nothing, so it adds nothing.
        assert result.usage.output_tokens == 10 + 5

    def test_a_completed_run_records_no_background_compaction(self, tmp_path):
        provider = _GatedProvider(
            [_tool_turn(1, 4000), _final_turn()], [(_summary(), True)]
        )
        result, records = _run(tmp_path, provider)
        assert result.success is True, result.result_text
        assert _compactions(records) == []

    def test_an_abort_cancels_the_summary_in_flight(self, tmp_path):
        provider = _GatedProvider(
            [_tool_turn(1, 4000), _final_turn()],
            [(_summary(), True)],
            on_main={1: _hold},
        )
        result, pending = _run_direct(
            tmp_path, provider, cancel_check=lambda: provider.summary_started
        )
        assert result.stop_reason == "cancelled"
        assert pending == []
        assert "summary0:cancelled" in provider.events

    def test_overflow_recovery_cancels_it_before_summarizing(self, tmp_path):
        provider = _GatedProvider(
            [_tool_turn(1, 4000), _OVERFLOW, _final_turn()],
            [(_summary(), True), (_summary("## Goal\nrecovered"), False)],
        )
        result, records = _run(tmp_path, provider)
        assert result.success is True, result.result_text
        assert provider.events.index("summary0:cancelled") < provider.events.index(
            "summary1:start"
        )
        cuts = _compactions(records)
        assert [(c["trigger"], c["placed"]) for c in cuts] == [("overflow", True)]
