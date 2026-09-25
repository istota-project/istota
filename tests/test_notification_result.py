"""Late notifications must not replace the completed answer."""
import json
from types import SimpleNamespace

import pytest

from istota.brain._events import parse_stream_line
from istota.session.result import _compose_full_result
from tests.test_claude_code_usage_capture import _run, _TOOL_USE

ANSWER = ("The completed analysis supports keeping the current plan. " * 12).strip()
NOTE = ("The background lookup finished without usable data. " * 5).strip()


def _text(text):
    return {"type": "text", "text": text}


def _notification(content=None, **extra):
    return json.dumps({"type": "user", "message": {"role": "user", "content": content or [
        _text("<task-notification><task-id>job-1</task-id><status>completed</status></task-notification>")
    ]}, **extra})


@pytest.mark.parametrize("content", [
    "<task-notification>finished</task-notification>",
    "<system-reminder>Background update: <task-notification>finished</task-notification></system-reminder>",
    [_text("<task-notification>finished</task-notification>\nRead the output file.")],
])
def test_parser_records_notification_boundary(content):
    event = parse_stream_line(_notification(content))
    assert event is not None
    assert type(event).__name__ == "TaskNotificationEvent"


@pytest.mark.parametrize("frame", [
    _notification([_text("Please explain <task-notification> markers")]),
    _notification([{"type": "tool_result", "content": "<task-notification>finished"}]),
    _notification(parent_tool_use_id="agent-tool"),
    _notification(isReplay=True),
    json.dumps({"type": "queue-operation", "operation": "enqueue",
                "content": "<task-notification>finished</task-notification>"}),
])
def test_parser_ignores_non_main_notification_frames(frame):
    assert parse_stream_line(frame) is None


def test_answer_and_long_notification_followup_are_delivered():
    trace = [{"type": "tool"}, _text(ANSWER), {"type": "notification"}, _text(NOTE)]
    assert _compose_full_result(NOTE, trace) == ANSWER + "\n\n" + NOTE


def test_without_notification_substantial_final_result_still_wins():
    trace = [{"type": "tool"}, _text(ANSWER), _text(NOTE)]
    assert _compose_full_result(NOTE, trace) == NOTE


def test_notification_does_not_promote_pre_tool_narration():
    trace = [_text(ANSWER), {"type": "tool"}, {"type": "notification"}, _text(NOTE)]
    assert _compose_full_result(NOTE, trace) == NOTE


def test_followup_tools_do_not_discard_completed_original_answer():
    trace = [_text(ANSWER), {"type": "notification"}, _text("Checking output."),
             {"type": "tool"}, _text(NOTE)]
    assert _compose_full_result(NOTE, trace) == ANSWER + "\n\n" + NOTE


def test_multiple_notifications_and_short_answer():
    trace = [_text("Yes."), {"type": "notification"}, _text(NOTE),
             {"type": "notification"}, _text("One more lookup failed.")]
    assert _compose_full_result("One more lookup failed.", trace) == (
        "Yes.\n\n" + NOTE + "\n\nOne more lookup failed."
    )


def test_already_combined_result_is_not_duplicated():
    combined = ANSWER + "\n\n" + NOTE
    trace = [_text(ANSWER), {"type": "notification"}, _text(NOTE)]
    assert _compose_full_result(combined, trace) == combined


def test_automated_output_is_not_glued():
    trace = [_text(ANSWER), {"type": "notification"}, _text(NOTE)]
    assert _compose_full_result(NOTE, trace, SimpleNamespace(source_type="briefing")) == NOTE


def test_stream_to_brain_to_composer(tmp_path):
    def assistant(text, mid):
        return json.dumps({"type": "assistant", "message": {
            "id": mid, "stop_reason": None, "content": [_text(text)],
        }})
    progress = []
    result = _run([
        _TOOL_USE, assistant(ANSWER, "answer"), _notification(), assistant(NOTE, "note"),
        json.dumps({"type": "result", "subtype": "success", "result": NOTE}),
    ], tmp_path=tmp_path, on_progress=progress.append)
    assert result.success
    trace = json.loads(result.execution_trace)
    assert [entry["type"] for entry in trace] == ["tool", "text", "notification", "text"]
    assert _compose_full_result(result.result_text, trace) == ANSWER + "\n\n" + NOTE
    assert all(type(event).__name__ != "TaskNotificationEvent" for event in progress)


def test_dedup_uses_result_after_context_recovery():
    trace = [_text(ANSWER), {"type": "notification"}, {"type": "cm_boundary"}, _text(NOTE)]
    combined = ANSWER + "\n\n" + NOTE
    assert _compose_full_result(combined, trace) == combined


def test_pre_notification_back_reference_recovers_original_answer():
    trace = [_text(ANSWER), {"type": "tool"}, _text("Done."),
             {"type": "notification"}, _text(NOTE)]
    assert _compose_full_result(NOTE, trace) == ANSWER + "\n\n" + NOTE


def test_pre_notification_context_recovery_keeps_complete_answer():
    trace = [_text(ANSWER), {"type": "cm_boundary"}, _text("Tail."),
             {"type": "notification"}, _text(NOTE)]
    assert _compose_full_result(NOTE, trace) == ANSWER + "\n\n" + NOTE
