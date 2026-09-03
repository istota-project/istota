"""Direct tests for `TaskStreamAdapter`.

`tests/test_executor_streaming.py` covers the same code by driving the whole of
`execute_task` and reading the `task_events` rows back. These drive the adapter
itself, which is what makes the raising-writer case reachable: there, a writer
that raises fails the surrounding task setup long before a flush.
"""

from unittest.mock import patch

import pytest

from istota import db
from istota.brain._events import (
    ContextManagementEvent,
    TextDeltaEvent,
    TextEvent,
    ThinkingDeltaEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from istota.config import Config
from istota.executor_stream import TaskStreamAdapter


class RecordingWriter:
    """Records `(kind, payload)` instead of writing rows."""

    def __init__(self):
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind, payload=None):
        self.events.append((kind, payload))

    def emit_once(self, kind, payload=None):
        self.events.append((kind, payload))

    def kinds(self) -> list[str]:
        return [kind for kind, _ in self.events]

    def texts(self, kind: str) -> list[str]:
        return [p["text"] for k, p in self.events if k == kind]


class RaisingWriter(RecordingWriter):
    def emit(self, kind, payload=None):
        raise RuntimeError("event log is gone")


@pytest.fixture
def task():
    return db.Task(
        id=42,
        status="running",
        source_type="web",
        user_id="alice",
        prompt="hello",
    )


def make_config(*, gate_chars: int = 280, show_text: bool = False) -> Config:
    config = Config()
    config.scheduler.stream_text_gate_chars = gate_chars
    config.scheduler.progress_show_text = show_text
    return config


def make_adapter(task, writer, *, stream_surface: bool = True, **kwargs):
    config = make_config(**kwargs)
    with patch(
        "istota.transport.registry.task_is_stream_surface",
        return_value=stream_surface,
    ):
        return TaskStreamAdapter(config, task, writer)


class TestTheNarrationGate:
    def test_holds_everything_below_the_gate(self, task):
        writer = RecordingWriter()
        adapter = make_adapter(task, writer, gate_chars=100)

        adapter.on_event(TextDeltaEvent(text="x" * 99))

        assert writer.kinds() == []

    def test_releases_at_the_threshold(self, task):
        writer = RecordingWriter()
        adapter = make_adapter(task, writer, gate_chars=100)

        adapter.on_event(TextDeltaEvent(text="x" * 60))
        assert writer.kinds() == []
        adapter.on_event(TextDeltaEvent(text="y" * 40))

        # The whole held run flushes as one event on unlock, not just the
        # delta that crossed.
        assert writer.texts("text_delta") == ["x" * 60 + "y" * 40]

    def test_a_tool_boundary_re_gates_the_next_run(self, task):
        writer = RecordingWriter()
        adapter = make_adapter(task, writer, gate_chars=100)

        adapter.on_event(TextDeltaEvent(text="x" * 120))  # unlocks, flushes
        adapter.on_event(ToolUseEvent(tool_name="Read", description="Read f"))
        assert adapter._delta_unlocked is False

        adapter.on_event(TextDeltaEvent(text="short lead-in"))
        adapter.settle_at_tool_boundary()

        # The second run never crossed the gate, so it is dropped intact.
        assert writer.texts("text_delta") == ["x" * 120]

    def test_a_substantial_run_keeps_its_tail_at_a_tool_boundary(self, task):
        writer = RecordingWriter()
        adapter = make_adapter(task, writer, gate_chars=100)

        adapter.on_event(TextDeltaEvent(text="x" * 100))  # unlocks + flushes
        adapter.on_event(TextDeltaEvent(text="tail"))     # under the cadence
        adapter.on_event(ToolUseEvent(tool_name="Read", description="Read f"))

        assert writer.texts("text_delta") == ["x" * 100, "tail"]

    def test_a_zero_gate_streams_immediately(self, task):
        writer = RecordingWriter()
        adapter = make_adapter(task, writer, gate_chars=0)

        adapter.on_event(TextDeltaEvent(text="hi"))

        assert writer.texts("text_delta") == ["hi"]


class TestTheWholeTurnDedupe:
    def test_a_text_event_is_dropped_once_deltas_have_flowed(self, task):
        writer = RecordingWriter()
        adapter = make_adapter(task, writer, gate_chars=10)

        adapter.on_event(TextDeltaEvent(text="streamed answer"))
        adapter.on_event(TextEvent(text="streamed answer"))
        adapter.finish()

        assert writer.texts("text_delta") == ["streamed answer"]

    def test_a_text_event_streams_when_no_delta_ever_arrived(self, task):
        writer = RecordingWriter()
        adapter = make_adapter(task, writer, gate_chars=10)

        adapter.on_event(TextEvent(text="whole block"))
        adapter.finish()

        assert writer.texts("text_delta") == ["whole block"]

    def test_a_push_surface_forwards_the_text_event_as_progress_text(self, task):
        writer = RecordingWriter()
        adapter = make_adapter(
            task, writer, stream_surface=False, show_text=True, gate_chars=10,
        )

        adapter.on_event(TextDeltaEvent(text="dropped on push"))
        adapter.on_event(TextEvent(text="narration"))
        adapter.finish()

        assert writer.kinds() == ["progress_text"]
        assert writer.texts("progress_text") == ["narration"]

    def test_a_thinking_event_is_dropped_once_thinking_deltas_have_flowed(self, task):
        writer = RecordingWriter()
        adapter = make_adapter(task, writer)

        adapter.on_event(ThinkingDeltaEvent(thinking="reasoning"))
        adapter.on_event(ThinkingEvent(text="reasoning"))
        adapter.finish()

        assert writer.texts("thinking") == ["reasoning"]

    def test_thinking_has_no_push_fallback(self, task):
        writer = RecordingWriter()
        adapter = make_adapter(
            task, writer, stream_surface=False, show_text=True,
        )

        adapter.on_event(ThinkingDeltaEvent(thinking="reasoning"))
        adapter.on_event(ThinkingEvent(text="reasoning"))
        adapter.finish()

        assert writer.kinds() == []


class TestOrdering:
    def test_thinking_settles_before_the_answer_at_a_tool_boundary(self, task):
        writer = RecordingWriter()
        adapter = make_adapter(task, writer, gate_chars=10)

        # Both buffers have to be non-empty when the boundary arrives, or the
        # two flushes are no-ops on their empty-buffer guards and the assertion
        # holds whichever order they run in.
        adapter._delta_unlocked = True
        adapter._thinking_buf.append("thought")
        adapter._delta_buf.append("substantial answer")
        adapter.on_event(ToolUseEvent(tool_name="Read", description="Read f"))

        # The reasoning chip settles first, so its rows keep a lower seq than
        # any trailing answer text.
        assert writer.kinds() == ["thinking", "text_delta", "tool_start"]

    def test_finish_flushes_thinking_before_deltas(self, task):
        writer = RecordingWriter()
        adapter = make_adapter(task, writer, gate_chars=0)

        # Buffer both without tripping either flush cadence.
        adapter._thinking_buf.append("thought")
        adapter._delta_buf.append("answer")
        adapter.finish()

        assert writer.kinds() == ["thinking", "text_delta"]

    def test_a_context_management_boundary_flushes_both(self, task):
        writer = RecordingWriter()
        adapter = make_adapter(task, writer, gate_chars=0)

        adapter.on_event(ThinkingDeltaEvent(thinking="thought"))
        adapter.on_event(TextDeltaEvent(text="answer"))
        writer.events.clear()
        adapter._thinking_buf.append("more thought")
        adapter._delta_buf.append("more answer")
        adapter.on_event(ContextManagementEvent())

        assert writer.kinds() == ["thinking", "text_delta", "context_management"]


class TestFailuresDoNotPropagate:
    def test_a_raising_writer_does_not_break_a_delta_flush(self, task):
        adapter = make_adapter(task, RaisingWriter(), gate_chars=0)

        adapter.on_event(TextDeltaEvent(text="answer"))
        adapter.finish()

        # The buffer is cleared before the emit, so a failed flush loses the
        # text rather than re-raising it at every later boundary.
        assert adapter._delta_buf == []

    def test_a_raising_writer_does_not_break_a_thinking_flush(self, task):
        adapter = make_adapter(task, RaisingWriter())

        adapter.on_event(ThinkingDeltaEvent(thinking="reasoning"))
        adapter.flush_thinking()

        assert adapter._thinking_buf == []


class TestNoEventWriter:
    def test_every_method_is_a_no_op(self, task):
        adapter = make_adapter(task, None, gate_chars=0)
        # Seeded, so the assertions below are about `on_event` never buffering
        # rather than about the constructor's initial state.
        adapter._delta_buf.append("seed")
        adapter._thinking_buf.append("seed")

        adapter.on_event(TextDeltaEvent(text="answer"))
        adapter.on_event(ThinkingDeltaEvent(thinking="reasoning"))
        adapter.on_event(ToolUseEvent(tool_name="Read", description="Read f"))
        adapter.flush_thinking()
        adapter.finish()

        # Nothing was appended and nothing raised. The seeds survive because
        # every flush returns on the missing writer.
        assert adapter._delta_buf == ["seed"]
        assert adapter._thinking_buf == ["seed"]

    def test_the_settle_still_runs_without_a_writer(self, task):
        """The reroute settle is about the daemon's own buffers, not the notice.

        `_failover_notice` is skipped when there is no writer; the settle is
        not, so it must reset the gate rather than refuse on a missing writer.
        `on_event` returns early with no writer, so nothing ever reaches these
        buffers in that state — they are seeded here to make the reset visible.

        Held-narration branch only, and deliberately so: the unlocked branch
        delegates to `flush_deltas`, which returns on the missing writer before
        it clears anything, so there the gate resets and the buffer does not.
        That asymmetry is in the pre-extraction code and is unreachable — a
        writer-less task buffers nothing to begin with.
        """
        adapter = make_adapter(task, None, gate_chars=10)
        adapter._delta_buf.append("held narration")
        adapter._delta_chars = 14
        adapter._delta_unlocked = False

        adapter.settle_at_tool_boundary()

        assert adapter._delta_buf == []
        assert adapter._delta_chars == 0
        assert adapter._delta_unlocked is False
