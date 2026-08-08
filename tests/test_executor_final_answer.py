"""ISSUE-211 at the execute_task boundary.

The unit tests in test_executor.py exercise `_compose_full_result` directly.
These pin the executor's own gate around it — which changed from
`if success and trace:` to `if success:`, because the empty-result guard has to
fire for a successful turn that produced no trace at all.
"""

from unittest.mock import patch

from istota.brain._types import BrainResult
from istota.executor import _NO_FINAL_ANSWER_NOTICE, execute_task

from tests.test_executor_streaming import (
    _make_config,
    _make_task,
    _patch_executor,
    contextmanager_chain,
)


class _FakeBrain:
    model_namespace = "anthropic"

    def __init__(self, result):
        self.result = result

    def execute(self, req):
        return self.result

    def resolve_model_name(self, name):
        return (name or "").strip()

    def resolve_alias(self, a):
        return None

    def list_aliases(self):
        return []

    def validate_alias_override(self, r, t):
        return []


def _run(tmp_path, brain_result, *, source_type="talk"):
    config = _make_config(tmp_path)
    config.security.sandbox_enabled = False
    task = _make_task(source_type=source_type)
    patches = _patch_executor() + [
        patch("istota.executor.make_brain", return_value=_FakeBrain(brain_result)),
    ]
    with contextmanager_chain(patches):
        return execute_task(task, config, [])


class TestEmptyResultGuard:
    def test_success_with_no_trace_still_gets_the_notice(self, tmp_path):
        """The old gate skipped composition entirely without a trace, so a
        successful turn that produced nothing delivered a blank reply."""
        success, result, _a, _t = _run(
            tmp_path, BrainResult(True, "", stop_reason="completed"),
        )
        assert success is True
        assert result == _NO_FINAL_ANSWER_NOTICE

    def test_success_with_empty_trace_string_gets_the_notice(self, tmp_path):
        success, result, _a, _t = _run(
            tmp_path, BrainResult(True, "   ", execution_trace="", stop_reason="completed"),
        )
        assert result == _NO_FINAL_ANSWER_NOTICE

    def test_narration_only_trace_is_labelled_not_promoted(self, tmp_path):
        trace = (
            '[{"type": "text", "text": "Let me check the calendar."},'
            ' {"type": "tool", "text": "Read calendar"}]'
        )
        _s, result, _a, _t = _run(
            tmp_path,
            BrainResult(True, "", execution_trace=trace, stop_reason="completed"),
        )
        assert result.startswith(_NO_FINAL_ANSWER_NOTICE)
        assert "Let me check the calendar." in result
        assert result != "Let me check the calendar."

    def test_real_answer_passes_through_untouched(self, tmp_path):
        trace = '[{"type": "tool", "text": "Read calendar"}]'
        _s, result, _a, _t = _run(
            tmp_path,
            BrainResult(
                True, "Your meeting is at 3pm.", execution_trace=trace,
                stop_reason="completed",
            ),
        )
        assert result == "Your meeting is at 3pm."

    def test_automated_task_keeps_its_empty_result(self, tmp_path):
        """A briefing body is parsed, not read — prose here would be archived
        as the digest."""
        _s, result, _a, _t = _run(
            tmp_path, BrainResult(True, "", stop_reason="completed"),
            source_type="briefing",
        )
        assert result == ""

    def test_failed_task_is_not_rewritten(self, tmp_path):
        """Composition is success-only; an error message must reach the
        scheduler's classification intact."""
        _s, result, _a, _t = _run(
            tmp_path, BrainResult(False, "API Error: 529 Overloaded", stop_reason="error"),
        )
        assert result == "API Error: 529 Overloaded"


class TestMalformedTrace:
    def test_non_list_json_trace_does_not_raise(self, tmp_path):
        """Previously an object trace raised AttributeError out to the outer
        handler, turning a completed run into a failure."""
        success, result, _a, _t = _run(
            tmp_path,
            BrainResult(True, "The answer.", execution_trace='{"not": "a list"}',
                        stop_reason="completed"),
        )
        assert success is True
        assert result == "The answer."

    def test_list_of_non_dicts_does_not_raise(self, tmp_path):
        success, result, _a, _t = _run(
            tmp_path,
            BrainResult(True, "The answer.", execution_trace='["a", "b"]',
                        stop_reason="completed"),
        )
        assert success is True
        assert result == "The answer."

    def test_unparseable_trace_does_not_raise(self, tmp_path):
        success, result, _a, _t = _run(
            tmp_path,
            BrainResult(True, "The answer.", execution_trace="{not json",
                        stop_reason="completed"),
        )
        assert success is True
        assert result == "The answer."
