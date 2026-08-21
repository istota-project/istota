"""Stage 3: the stream parser's usage frames.

The lines here follow the shapes `claude` 2.1.227 emits under
`--output-format stream-json --verbose --include-partial-messages`, which is the
exact flag set the daemon passes. The token numbers come from one measured
two-turn run and are asserted verbatim.

The load-bearing test in this file is `test_an_assistant_tool_use_line_still
_yields_the_tool_event`. `parse_stream_line` returns one event per line, so
emitting usage from the `assistant` branch would consume the return slot the
tool event needs. That is the design defect that killed an earlier approach, and
this is the guard that fails if anyone moves capture back there.
"""

import json

import pytest

from istota.brain._events import (
    RateLimitEvent,
    RequestUsageEvent,
    ResultEvent,
    TextDeltaEvent,
    ToolUseEvent,
    make_stream_parser,
    parse_stream_line,
)


def _message_delta(
    *, input_tokens=9, cache_read=0, cache_write=14425, output=119,
    parent_tool_use_id=None, context_management=None, model="claude-haiku-4-5",
):
    frame = {
        "type": "stream_event",
        "parent_tool_use_id": parent_tool_use_id,
        "model": model,
        "event": {
            "type": "message_delta",
            "delta": {"stop_reason": "tool_use"},
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


class TestRequestUsage:
    def test_a_message_delta_yields_one_request_usage_event(self):
        event = parse_stream_line(_message_delta())

        assert isinstance(event, RequestUsageEvent)
        # input + cache_read + cache_write — the whole prompt the model saw.
        assert event.prompt_tokens == 14434
        assert event.output_tokens == 119
        assert event.model == "claude-haiku-4-5"
        assert event.is_subagent is False
        assert event.compacted is False

    def test_the_second_request_of_the_capture(self):
        event = parse_stream_line(
            _message_delta(input_tokens=8, cache_read=14425, cache_write=140, output=28)
        )

        assert event.prompt_tokens == 14573
        assert event.output_tokens == 28

    def test_a_message_start_yields_no_request_usage(self):
        """Its output_tokens is a partial — 3 on the capture, against a real 119.
        Reading it would report a fraction of the run's output."""
        line = json.dumps({
            "type": "stream_event",
            "event": {
                "type": "message_start",
                "message": {
                    "id": "msg_1",
                    "usage": {"input_tokens": 9, "output_tokens": 3},
                },
            },
        })

        assert parse_stream_line(line) is None

    def test_a_subagent_frame_is_marked(self):
        event = parse_stream_line(_message_delta(parent_tool_use_id="toolu_123"))

        assert isinstance(event, RequestUsageEvent)
        assert event.is_subagent is True

    def test_a_compacted_frame_is_marked(self):
        event = parse_stream_line(
            _message_delta(context_management={"applied_edits": []})
        )

        assert isinstance(event, RequestUsageEvent)
        assert event.compacted is True

    def test_a_message_delta_without_usage_yields_nothing(self):
        line = json.dumps({
            "type": "stream_event",
            "event": {"type": "message_delta", "delta": {"stop_reason": "end_turn"}},
        })

        assert parse_stream_line(line) is None

    @pytest.mark.parametrize(
        "usage",
        [
            {"input_tokens": None},
            {"input_tokens": "many"},
            {"input_tokens": True},
            {"input_tokens": -5},
            {},
        ],
    )
    def test_a_malformed_usage_payload_yields_zeros_not_an_exception(self, usage):
        line = json.dumps({
            "type": "stream_event",
            "event": {"type": "message_delta", "usage": usage},
        })

        event = parse_stream_line(line)

        assert isinstance(event, RequestUsageEvent)
        assert event.prompt_tokens >= 0


class TestRateLimit:
    def test_a_rate_limit_line_yields_the_posture(self):
        info = {
            "status": "allowed",
            "resetsAt": 1755680000,
            "rateLimitType": "five_hour",
            "overageStatus": "rejected",
            "isUsingOverage": False,
        }
        line = json.dumps({"type": "rate_limit_event", "rate_limit_info": info})

        event = parse_stream_line(line)

        assert isinstance(event, RateLimitEvent)
        assert event.info == info

    def test_a_missing_info_block_yields_an_empty_dict(self):
        event = parse_stream_line(json.dumps({"type": "rate_limit_event"}))

        assert isinstance(event, RateLimitEvent)
        assert event.info == {}


class TestResultRaw:
    def test_the_result_event_carries_the_whole_frame(self):
        frame = {
            "type": "result",
            "subtype": "success",
            "result": "done",
            "total_cost_usd": 0.03,
            "modelUsage": {"m": {"inputTokens": 5}},
        }

        event = parse_stream_line(json.dumps(frame))

        assert isinstance(event, ResultEvent)
        assert event.raw == frame
        # The existing fields are unchanged.
        assert event.success is True
        assert event.text == "done"


class TestNoRegression:
    def test_an_assistant_tool_use_line_still_yields_the_tool_event(self):
        """The regression guard for the design defect that killed the earlier
        approach. `parse_stream_line` returns one event per line, so emitting
        usage from the `assistant` branch would consume this return slot —
        costing a tool chip on the live surface, an `actions_taken` entry, and
        the `execution_trace` entry the sleep cycle reads for playbooks."""
        line = json.dumps({
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "usage": {"input_tokens": 9, "output_tokens": 4},
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "Bash",
                        "input": {"command": "ls"},
                    }
                ],
            },
        })

        event = parse_stream_line(line)

        assert isinstance(event, ToolUseEvent)
        assert not isinstance(event, RequestUsageEvent)

    def test_content_deltas_still_parse(self):
        line = json.dumps({
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "hi"},
            },
        })

        assert isinstance(parse_stream_line(line), TextDeltaEvent)

    def test_a_full_replay_yields_both_usage_and_content_events(self):
        parse = make_stream_parser()
        lines = [
            json.dumps({"type": "system", "subtype": "init", "model": "m"}),
            _message_delta(),
            json.dumps({
                "type": "assistant",
                "message": {
                    "id": "m1",
                    "content": [
                        {"type": "tool_use", "id": "t1", "name": "Bash",
                         "input": {"command": "ls"}}
                    ],
                },
            }),
            json.dumps({"type": "result", "subtype": "success", "result": "ok"}),
        ]

        events = [parse(line) for line in lines]
        kinds = [type(e).__name__ for e in events if e is not None]

        assert "RequestUsageEvent" in kinds
        assert "ToolUseEvent" in kinds
        assert "ResultEvent" in kinds
