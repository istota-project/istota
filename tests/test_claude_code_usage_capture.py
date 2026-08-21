"""Stage 3: ClaudeCodeBrain's usage capture off the stream.

`subprocess.Popen` is patched to replay a fixture, following the fake-process
helper the existing streaming tests use.

The gate in this file is `TestNoSurfaceLeak`. The executor fans progress events
out to live surfaces, so a token-accounting frame reaching `req.on_progress` or
`execution_trace` would put an accounting record in a user's chat.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.brain._types import BrainRequest
from istota.brain.claude_code import ClaudeCodeBrain


def _mock_process(stdout_lines, returncode=0):
    mock = MagicMock()
    mock.stdout = iter(stdout_lines)
    mock.stderr = iter([])
    mock.pid = 4242
    mock.returncode = None

    def _wait(*_a, **_kw):
        mock.returncode = returncode
        return returncode

    mock.wait.side_effect = _wait
    mock.kill = MagicMock()
    mock.stdin = MagicMock()
    return mock


def _init(model="claude-haiku-4-5-20251001", api_key_source="ANTHROPIC_API_KEY"):
    return json.dumps({
        "type": "system",
        "subtype": "init",
        "model": model,
        "apiKeySource": api_key_source,
        "claude_code_version": "2.1.227",
    })


def _message_delta(
    *, input_tokens, cache_read, cache_write, output,
    parent_tool_use_id=None, context_management=None,
):
    frame = {
        "type": "stream_event",
        "parent_tool_use_id": parent_tool_use_id,
        "model": "claude-haiku-4-5-20251001",
        "event": {
            "type": "message_delta",
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_write,
                "output_tokens": output,
            },
        },
    }
    if context_management is not None:
        frame["event"]["context_management"] = context_management
    return json.dumps(frame)


_RESULT = json.dumps({
    "type": "result",
    "subtype": "success",
    "is_error": False,
    "num_turns": 2,
    "duration_ms": 2916,
    "duration_api_ms": 3655,
    "result": "Done.",
    "total_cost_usd": 0.0319275,
    "usage": {"input_tokens": 17, "output_tokens": 147, "service_tier": "standard"},
    "modelUsage": {
        "claude-haiku-4-5-20251001": {
            "inputTokens": 550,
            "outputTokens": 161,
            "cacheReadInputTokens": 14425,
            "cacheCreationInputTokens": 14565,
            "costUSD": 0.0319275,
            "contextWindow": 200000,
            "maxOutputTokens": 32000,
        }
    },
})

# The two main-agent requests of the measured capture.
_REQ_1 = _message_delta(input_tokens=9, cache_read=0, cache_write=14425, output=119)
_REQ_2 = _message_delta(
    input_tokens=8, cache_read=14425, cache_write=140, output=28
)

_TOOL_USE = json.dumps({
    "type": "assistant",
    "message": {
        "id": "msg_1",
        "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Bash",
             "input": {"command": "ls"}}
        ],
    },
})

FULL_STREAM = [_init(), _REQ_1, _TOOL_USE, _REQ_2, _RESULT]


def _run(lines, *, on_progress=None, returncode=0, tmp_path=None):
    req = BrainRequest(
        prompt="hi",
        allowed_tools=["Bash"],
        cwd=tmp_path or Path("/tmp"),
        env={},
        timeout_seconds=60,
        streaming=True,
        on_progress=on_progress,
    )
    with patch(
        "istota.brain.claude_code.subprocess.Popen",
        return_value=_mock_process(lines, returncode=returncode),
    ):
        return ClaudeCodeBrain().execute(req)


class TestFullReplay:
    def test_usage_is_populated_from_model_usage(self, tmp_path):
        result = _run(FULL_STREAM, tmp_path=tmp_path)

        assert result.usage is not None
        # From modelUsage, not result.usage (17/147).
        assert result.usage.billed_input_tokens == 550
        assert result.usage.output_tokens == 161
        assert result.usage.cache_read_tokens == 14425
        assert result.usage.cache_write_tokens == 14565
        assert result.usage.has_totals is True
        assert result.usage.totals_source == "model_usage"

    def test_context_measures_come_from_the_request_frames(self, tmp_path):
        result = _run(FULL_STREAM, tmp_path=tmp_path)

        assert result.usage.initial_context_tokens == 14434
        assert result.usage.peak_context_tokens == 14573
        assert result.usage.model_requests == 2
        assert result.usage.context_window == 200000

    def test_cost_basis_comes_from_the_init_frame(self, tmp_path):
        result = _run(FULL_STREAM, tmp_path=tmp_path)
        assert result.usage.cost_basis == "api"

        subscription = [_init(api_key_source="none")] + FULL_STREAM[1:]
        assert _run(subscription, tmp_path=tmp_path).usage.cost_basis == "subscription"

    def test_brain_kind_is_set(self, tmp_path):
        assert _run(FULL_STREAM, tmp_path=tmp_path).brain_kind == "claude_code"

    def test_the_per_model_split_is_carried(self, tmp_path):
        result = _run(FULL_STREAM, tmp_path=tmp_path)

        assert [m.model for m in result.usage.models] == [
            "claude-haiku-4-5-20251001"
        ]
        assert result.usage.models[0].billed_input_tokens == 550


class TestNoSurfaceLeak:
    def test_accounting_frames_never_reach_on_progress(self, tmp_path):
        """The executor fans these out to live surfaces. An accounting frame in
        a user's chat is a bug."""
        seen = []
        rate_limit = json.dumps({
            "type": "rate_limit_event",
            "rate_limit_info": {"status": "allowed", "rateLimitType": "five_hour"},
        })

        result = _run(
            [_init(), rate_limit, _REQ_1, _TOOL_USE, _REQ_2, _RESULT],
            on_progress=seen.append,
            tmp_path=tmp_path,
        )

        kinds = {type(e).__name__ for e in seen}
        assert "RequestUsageEvent" not in kinds
        assert "RateLimitEvent" not in kinds
        # And the tool event that shares the stream did get through.
        assert "ToolUseEvent" in kinds
        assert result.usage.model_requests == 2

    def test_accounting_frames_never_reach_the_execution_trace(self, tmp_path):
        result = _run(FULL_STREAM, tmp_path=tmp_path)

        trace = json.loads(result.execution_trace or "[]")
        assert all(entry.get("type") != "usage" for entry in trace)
        # The tool entry survives — it is what would have been consumed if usage
        # were emitted from the `assistant` branch.
        assert any(entry.get("type") == "tool" for entry in trace)

    def test_the_rate_limit_posture_is_still_captured(self, tmp_path):
        info = {"status": "allowed", "rateLimitType": "five_hour", "resetsAt": 99}
        rate_limit = json.dumps({"type": "rate_limit_event", "rate_limit_info": info})

        result = _run([_init(), rate_limit, _REQ_1, _RESULT], tmp_path=tmp_path)

        assert result.usage.rate_limit == info


class TestSubagentAndCompaction:
    def test_a_subagent_frame_is_excluded_from_the_peak(self, tmp_path):
        """Hand-built, not captured: fan-out is denied in production, so a real
        capture cannot produce one. The sub-agent's prompt deliberately exceeds
        every main-agent frame, so a peak that included it would be visibly
        wrong."""
        subagent = _message_delta(
            input_tokens=5, cache_read=0, cache_write=90000, output=10,
            parent_tool_use_id="toolu_1",
        )

        result = _run(
            [_init(), _REQ_1, subagent, _REQ_2, _RESULT], tmp_path=tmp_path
        )

        assert result.usage.peak_context_tokens == 14573
        assert result.usage.peak_context_tokens != 90005
        assert result.usage.subagent_requests == 1
        assert result.usage.model_requests == 2

    def test_a_compacted_frame_is_counted_not_measured(self, tmp_path):
        """A compaction replays the previous response; counting it as a request
        would inflate model_requests with nothing behind it."""
        compacted = _message_delta(
            input_tokens=5, cache_read=0, cache_write=100, output=10,
            context_management={"applied_edits": []},
        )

        result = _run(
            [_init(), _REQ_1, compacted, _REQ_2, _RESULT], tmp_path=tmp_path
        )

        assert result.usage.compacted_requests == 1
        assert result.usage.model_requests == 2

    def test_subagent_requests_reads_zero_on_an_ordinary_run(self, tmp_path):
        """Fan-out is denied today, so a non-zero value is itself a signal."""
        result = _run(FULL_STREAM, tmp_path=tmp_path)
        assert result.usage.subagent_requests == 0


class TestTruncatedStream:
    def test_no_result_frame_yields_context_but_no_totals(self, tmp_path):
        """A timeout or kill still spent tokens. One rule: a row is written
        whenever anything was measured, and has_totals says whether the token
        columns mean anything."""
        result = _run([_init(), _REQ_1, _REQ_2], returncode=1, tmp_path=tmp_path)

        assert result.usage is not None
        assert result.usage.has_totals is False
        assert result.usage.billed_input_tokens == 0
        # Context is measured independently.
        assert result.usage.initial_context_tokens == 14434
        assert result.usage.peak_context_tokens == 14573
        assert result.success is False

    def test_a_stream_with_nothing_measurable_still_returns_a_usage(self, tmp_path):
        result = _run([_init()], returncode=1, tmp_path=tmp_path)

        assert result.usage is not None
        assert result.usage.has_totals is False
        assert result.usage.initial_context_tokens is None

    def test_the_init_model_is_the_fallback_when_model_usage_is_absent(
        self, tmp_path
    ):
        result = _run([_init(model="claude-sonnet-5"), _REQ_1], tmp_path=tmp_path)

        assert result.usage.model == "claude-sonnet-5"


class TestFailurePathsStillMeasure:
    @pytest.mark.parametrize("returncode", [0, 1, -9])
    def test_usage_is_attached_regardless_of_outcome(self, returncode, tmp_path):
        """Tokens are spent whether or not the run succeeded."""
        result = _run(FULL_STREAM, returncode=returncode, tmp_path=tmp_path)

        assert result.usage is not None
        assert result.brain_kind == "claude_code"
