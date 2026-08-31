"""A stop that discards the answer must still hand back the work (ISSUE-372).

``max_turns`` and ``loop_detected`` already deliver the last text-bearing turn
under a marker. Timeout and cancel — the two stops a person is most likely to
see, and the two that fire on the longest runs — returned a fixed string and
threw the model's prose away. ``BrainResult.partial_text`` carries it out
without touching ``result_text``, which the scheduler still dispatches on by
string match.

ISSUE-373's soft deadline lives here too, because it is the same property from
the other end: a run about to be killed by the wall clock stops itself just
before it, on a stop reason that preserves the work.
"""

import asyncio
import threading
from collections.abc import AsyncIterator
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeoutError
from pathlib import Path

from istota.brain import BrainRequest
from istota.brain.native import NativeBrain
from istota.config import NativeBrainConfig
from istota.llm.types import (
    AssistantMessage,
    TextContent,
    ToolCallContent,
    Usage,
)

from ._mock_provider import MockProvider


def _req(prompt: str, cwd: Path, tools: list[str] | None = None) -> BrainRequest:
    return BrainRequest(
        prompt=prompt,
        allowed_tools=tools if tools is not None else ["Write"],
        cwd=cwd,
        env={},
        timeout_seconds=30,
        model="claude-sonnet-4-6",
    )


def _brain(provider, **cfg) -> NativeBrain:
    config = NativeBrainConfig(model="claude-sonnet-4-6", **cfg)
    return NativeBrain(config, provider=provider)


def _narrating_turns(n: int, note: str) -> list[AssistantMessage]:
    """``n`` turns that each say something and then call a tool.

    A tool call is what keeps the loop going: a text-only turn ends it, and a
    run that ends cannot demonstrate anything about a stop that interrupts one.
    """
    return [
        AssistantMessage(
            content=[
                TextContent(text=f"{note} {i}"),
                ToolCallContent(
                    id=f"c{i}",
                    name="Write",
                    arguments={"file_path": f"out{i}.txt", "content": "x"},
                ),
            ],
            stop_reason="tool_use",
            usage=Usage(input_tokens=10, output_tokens=5),
        )
        for i in range(n)
    ]


class _SlowProvider(MockProvider):
    """A scripted provider that takes ``delay`` seconds per turn.

    Enough to move the wall clock past a short test budget while every turn is
    otherwise a perfectly healthy tool-calling turn — which is the failure
    ISSUE-373 describes: not a wedged model, just a slow one.
    """

    def __init__(self, turns, delay: float):
        super().__init__(turns)
        self._delay = delay

    async def stream(self, *args, **kwargs) -> AsyncIterator:
        await asyncio.sleep(self._delay)
        async for event in super().stream(*args, **kwargs):
            yield event


class _HangingProvider(MockProvider):
    """Scripted for ``n`` turns, then never answers again."""

    async def stream(self, *args, **kwargs) -> AsyncIterator:
        if not self._turns:
            await asyncio.sleep(3600)
            return
        async for event in super().stream(*args, **kwargs):
            yield event


class TestTimeoutKeepsTheWork:
    def test_timeout_carries_the_last_narration(self, tmp_path):
        """A run killed by the wall clock hands back what the model wrote."""
        provider = _HangingProvider(_narrating_turns(1, "I found the leak in step"))
        req = _req("investigate", tmp_path)
        req.timeout_seconds = 1
        # The soft deadline off, so the hard clock is what ends this run — the
        # case where a turn hangs past every softer stop.
        result = _brain(provider, soft_deadline_percent=0).execute(req)

        assert result.stop_reason == "timeout"
        # result_text is byte-identical to what the scheduler matches on.
        assert result.result_text == "Task execution timed out after 0 minutes"
        assert result.partial_text is not None
        assert "I found the leak in step 0" in result.partial_text

    def test_timeout_with_no_text_reports_no_partial(self, tmp_path):
        provider = _HangingProvider([])
        req = _req("hang", tmp_path)
        req.timeout_seconds = 1
        result = _brain(provider, soft_deadline_percent=0).execute(req)

        assert result.stop_reason == "timeout"
        assert result.partial_text is None


class TestCancelKeepsTheWork:
    class _BlockedAfterOneTurn(MockProvider):
        """One narrating turn, then a provider waiting for its first byte."""

        def __init__(self, turns):
            super().__init__(turns)
            self.started = threading.Event()
            self._loop = None
            self._release = None

        async def stream(self, *args, **kwargs) -> AsyncIterator:
            if self._turns:
                async for event in super().stream(*args, **kwargs):
                    yield event
                return
            self._loop = asyncio.get_running_loop()
            self._release = asyncio.Event()
            self.started.set()
            await self._release.wait()

        def release(self):
            if self._loop is not None and self._release is not None:
                self._loop.call_soon_threadsafe(self._release.set)

    def test_cancel_carries_the_last_narration(self, tmp_path):
        provider = self._BlockedAfterOneTurn(
            _narrating_turns(1, "so far the DB is fine at step")
        )
        cancelled = threading.Event()
        req = _req("investigate", tmp_path)
        req.cancel_check = cancelled.is_set

        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_brain(provider).execute, req)
            assert provider.started.wait(timeout=10)
            cancelled.set()
            try:
                result = future.result(timeout=10)
            except FutureTimeoutError:
                provider.release()
                future.result(timeout=2)
                raise

        assert result.stop_reason == "cancelled"
        # The three exact-equality matches in the scheduler still hold.
        assert result.result_text == "Cancelled by user"
        assert result.partial_text is not None
        assert "so far the DB is fine at step 0" in result.partial_text


class TestSoftDeadline:
    """ISSUE-373: a run about to be killed by the clock stops itself first."""

    def test_soft_deadline_stops_with_the_work_intact(self, tmp_path):
        # Healthy tool-calling turns at 0.4s each. The soft deadline is 1.0s of
        # a 10s budget, so it is crossed after two or three turns while the hard
        # clock stays nine seconds away — the margins are one-sided on purpose,
        # since the suite runs `-n auto` and a tight window in both directions
        # is a flake. The run must end on the stop that delivers the narration
        # under a marker, not on the one that replaces it with "timed out".
        provider = _SlowProvider(_narrating_turns(40, "still looking at step"), 0.4)
        req = _req("investigate", tmp_path)
        req.timeout_seconds = 10
        result = _brain(provider, soft_deadline_percent=10).execute(req)

        assert result.stop_reason == "soft_timeout"
        assert result.success is True
        assert "still looking at step" in result.result_text
        assert "ran out of time" in result.result_text

    def test_soft_deadline_off_leaves_the_hard_clock_alone(self, tmp_path):
        # 40 scripted turns at 0.4s is 16 seconds of work against a 2s clock, so
        # the hard deadline fires whatever the host is doing — a sleep does not
        # get shorter under load.
        provider = _SlowProvider(_narrating_turns(40, "still looking at step"), 0.4)
        req = _req("investigate", tmp_path)
        req.timeout_seconds = 2
        result = _brain(provider, soft_deadline_percent=0).execute(req)

        assert result.stop_reason == "timeout"
        assert result.success is False

    def test_a_finished_answer_past_the_deadline_is_not_relabelled(self, tmp_path):
        """The stop must not fire on the turn that *is* the answer.

        Stop conditions run after every turn, the final text-only one included,
        with the loop's natural exit one check away. Firing there labels a
        completed run `soft_timeout` and appends a marker saying it ran out of
        time — the opposite of what happened — and there is nothing to rescue,
        because the run already delivered.
        """
        # One turn, which is the answer, taking 0.4s against a 0.1s soft
        # deadline. The deadline is therefore crossed by the time the condition
        # runs, and the only thing stopping it firing is that the turn called no
        # tools. A preceding tool-calling turn would be stopped here, correctly,
        # and would test nothing about this.
        provider = _SlowProvider(
            [
                AssistantMessage(
                    content=[TextContent(text='{"summary": "complete answer"}')],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=10, output_tokens=5),
                )
            ],
            0.4,
        )
        req = _req("investigate", tmp_path)
        req.timeout_seconds = 10
        result = _brain(provider, soft_deadline_percent=1).execute(req)

        assert result.stop_reason == "completed"
        assert result.result_text == '{"summary": "complete answer"}'
        assert "ran out of time" not in result.result_text

    def test_a_text_only_run_never_gets_the_soft_stop(self, tmp_path):
        """The native brain's direct callers parse structured output.

        The sleep cycle, shared briefing blocks, health OCR and conversation
        triage all call with empty `allowed_tools` and read JSON back. Prose
        appended to that breaks them, so the gate matches `budget_nudge_on`'s.
        """
        provider = _SlowProvider(
            [
                AssistantMessage(
                    content=[TextContent(text='{"facts": []}')],
                    stop_reason="end_turn",
                    usage=Usage(input_tokens=10, output_tokens=5),
                )
            ],
            0.4,
        )
        req = _req("extract", tmp_path, tools=[])
        req.timeout_seconds = 10
        result = _brain(provider, soft_deadline_percent=1).execute(req)

        assert result.stop_reason == "completed"
        assert result.result_text == '{"facts": []}'

    def test_no_deadline_means_no_soft_stop(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="done")], stop_reason="end_turn")]
        )
        req = _req("hi", tmp_path)
        req.timeout_seconds = 0
        result = _brain(provider, soft_deadline_percent=90).execute(req)

        assert result.stop_reason == "completed"
        assert result.result_text == "done"

    def test_a_fast_run_finishes_normally_under_a_soft_deadline(self, tmp_path):
        """The stop must not fire on a run with time to spare."""
        provider = MockProvider(
            _narrating_turns(1, "checking step")
            + [
                AssistantMessage(
                    content=[TextContent(text="The answer is 42.")],
                    stop_reason="end_turn",
                )
            ]
        )
        req = _req("investigate", tmp_path)
        req.timeout_seconds = 60
        result = _brain(provider, soft_deadline_percent=90).execute(req)

        assert result.stop_reason == "completed"
        assert result.result_text == "The answer is 42."


class TestACancelOutranksEveryGracefulStop:
    """ISSUE-372/373: a `!stop` must not come back as a completed answer.

    The loop checks `abort` at the top of its inner iteration while stop
    conditions run at the bottom, and the streaming reader catches only an abort
    landing mid-*stream* — so an abort set during **tool execution** reaches the
    stop conditions first. Every one of the three returns `success=True`, and the
    scheduler then marks the task completed, posts the marker to the room as the
    answer after `!stop` already said it stopped, indexes the turn into memory
    and replays the run's deferred ops. Tool execution is where a long run spends
    most of its wall clock, which is where a user reaches for `!stop`.

    Both cases below cancel *during a Bash call*, which is the only window that
    exercises this. Cancelling mid-stream is already handled a layer down and
    proves nothing here.
    """

    @staticmethod
    def _sleeping_turn() -> list[AssistantMessage]:
        return [
            AssistantMessage(
                content=[
                    TextContent(text="running the suite"),
                    ToolCallContent(
                        id="c0", name="Bash",
                        arguments={"command": "sleep 4", "description": "sleep"},
                    ),
                ],
                stop_reason="tool_use",
                usage=Usage(input_tokens=10, output_tokens=5),
            ),
        ] + _narrating_turns(10, "still looking at step")

    def _run_cancelling_mid_tool(self, tmp_path, **cfg):
        cancelled = threading.Event()
        req = _req("investigate", tmp_path, tools=["Bash"])
        req.timeout_seconds = 30
        req.cancel_check = cancelled.is_set

        provider = MockProvider(self._sleeping_turn())
        timer = threading.Timer(1.0, cancelled.set)
        timer.start()
        try:
            return _brain(provider, **cfg).execute(req)
        finally:
            timer.cancel()

    def test_a_cancel_during_a_tool_beats_the_soft_deadline(self, tmp_path):
        # A soft deadline of 0.3s of a 30s budget: crossed long before the tool
        # finishes, so the condition is armed when the cancel lands.
        result = self._run_cancelling_mid_tool(tmp_path, soft_deadline_percent=1)

        assert result.stop_reason == "cancelled"
        assert result.result_text == "Cancelled by user"

    def test_a_cancel_during_a_tool_beats_the_turn_cap(self, tmp_path):
        result = self._run_cancelling_mid_tool(
            tmp_path, max_turns=1, soft_deadline_percent=0,
        )

        assert result.stop_reason == "cancelled"
        assert result.result_text == "Cancelled by user"


class TestASoftStopWithNothingToSave:
    """ISSUE-373: slowness is retryable; a pathology is not.

    `_build_result`'s partial-answer arm makes the marker the whole text and
    returns `success=True`. For `max_turns` and `loop_detected` that is right —
    both name something a retry would only repeat. `soft_timeout` names
    slowness, which a retry on a fresh budget can clear, and before the soft
    deadline existed this exact run reached the hard clock and got its retries.
    """

    def test_no_text_at_all_falls_through_to_the_retryable_timeout(self, tmp_path):
        tool_only = [
            AssistantMessage(
                content=[
                    ToolCallContent(
                        id=f"c{i}",
                        name="Write",
                        arguments={"file_path": f"out{i}.txt", "content": "x"},
                    ),
                ],
                stop_reason="tool_use",
                usage=Usage(input_tokens=10, output_tokens=5),
            )
            for i in range(40)
        ]
        provider = _SlowProvider(tool_only, 0.4)
        req = _req("investigate", tmp_path)
        req.timeout_seconds = 10
        result = _brain(provider, soft_deadline_percent=10).execute(req)

        assert result.success is False
        assert result.stop_reason == "timeout"
        assert result.result_text == "Task execution timed out after 0 minutes"
        # A bare marker delivered as a completed answer is the regression.
        assert "ran out of time" not in result.result_text
