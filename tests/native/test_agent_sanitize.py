"""Phase 2 — orphaned tool-pair sanitization at the converter boundary."""

from istota.agent.sanitize import sanitize_tool_pairs
from istota.llm.types import (
    AssistantMessage,
    TextContent,
    ToolCallContent,
    ToolResultMessage,
    UserMessage,
)


def _assistant_with_call(call_id: str, name: str = "Read") -> AssistantMessage:
    return AssistantMessage(
        content=[ToolCallContent(id=call_id, name=name, arguments={})],
        stop_reason="tool_use",
    )


class TestSanitizeToolPairs:
    def test_well_formed_pairs_unchanged(self):
        msgs = [
            UserMessage(content=[TextContent(text="hi")]),
            _assistant_with_call("c1"),
            ToolResultMessage(tool_call_id="c1", tool_name="Read", content=[]),
        ]
        out = sanitize_tool_pairs(msgs)
        assert out == msgs

    def test_orphaned_call_gets_synthesized_result(self):
        msgs = [
            _assistant_with_call("c1"),
        ]
        out = sanitize_tool_pairs(msgs)
        assert len(out) == 2
        synthesized = out[1]
        assert isinstance(synthesized, ToolResultMessage)
        assert synthesized.tool_call_id == "c1"
        assert synthesized.is_error is True
        assert "interrupted" in synthesized.content[0].text.lower()

    def test_synthesized_result_is_adjacent_to_its_call(self):
        msgs = [
            _assistant_with_call("c1"),
            UserMessage(content=[TextContent(text="next")]),
        ]
        out = sanitize_tool_pairs(msgs)
        # call, synthesized result, then the user message
        assert isinstance(out[0], AssistantMessage)
        assert isinstance(out[1], ToolResultMessage)
        assert out[1].tool_call_id == "c1"
        assert isinstance(out[2], UserMessage)

    def test_orphaned_result_dropped(self):
        msgs = [
            UserMessage(content=[TextContent(text="hi")]),
            ToolResultMessage(tool_call_id="ghost", tool_name="Read", content=[]),
        ]
        out = sanitize_tool_pairs(msgs)
        assert len(out) == 1
        assert isinstance(out[0], UserMessage)

    def test_multiple_calls_one_missing(self):
        msgs = [
            AssistantMessage(
                content=[
                    ToolCallContent(id="c1", name="Read", arguments={}),
                    ToolCallContent(id="c2", name="Grep", arguments={}),
                ],
                stop_reason="tool_use",
            ),
            ToolResultMessage(tool_call_id="c1", tool_name="Read", content=[]),
        ]
        out = sanitize_tool_pairs(msgs)
        synth = [m for m in out if isinstance(m, ToolResultMessage) and m.is_error]
        assert len(synth) == 1
        assert synth[0].tool_call_id == "c2"

    def test_does_not_mutate_input(self):
        msgs = [_assistant_with_call("c1")]
        original_len = len(msgs)
        sanitize_tool_pairs(msgs)
        assert len(msgs) == original_len

    def test_result_before_call_is_repaired(self):
        # A tool_result that precedes its owning call (a context-reordering
        # hazard) must not leave the call orphaned: the stray leading result is
        # dropped and a synthetic result is added after the call.
        msgs = [
            ToolResultMessage(tool_call_id="c1", tool_name="Read", content=[]),
            _assistant_with_call("c1"),
        ]
        out = sanitize_tool_pairs(msgs)
        assert isinstance(out[0], AssistantMessage)
        synth = [m for m in out if isinstance(m, ToolResultMessage)]
        assert len(synth) == 1
        assert synth[0].tool_call_id == "c1"
        assert synth[0].is_error is True
        # The call is no longer orphaned — it is followed by exactly one result.
        assert out.index(synth[0]) > 0
