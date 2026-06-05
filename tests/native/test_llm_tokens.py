"""Phase 1 — istota.llm token estimation (chars/4 heuristic)."""

from istota.llm.tokens import estimate_tokens
from istota.llm.types import (
    AssistantMessage,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCallContent,
    ToolResultMessage,
    UserMessage,
)


class TestEstimateTokens:
    def test_text_content_chars_over_four(self):
        m = UserMessage(content=[TextContent(text="x" * 40)])
        assert estimate_tokens(m) == 10

    def test_thinking_content_counted(self):
        m = AssistantMessage(content=[ThinkingContent(thinking="y" * 40)])
        assert estimate_tokens(m) == 10

    def test_tool_call_counts_name_plus_json_args(self):
        # name "Read" (4) + json.dumps({"file_path": "/x"}) length
        import json

        args = {"file_path": "/x"}
        m = AssistantMessage(content=[ToolCallContent(name="Read", arguments=args)])
        expected = max(1, (len("Read") + len(json.dumps(args))) // 4)
        assert estimate_tokens(m) == expected

    def test_image_uses_fixed_estimate(self):
        m = UserMessage(content=[ImageContent(media_type="image/png", data="zzz")])
        assert estimate_tokens(m) == 4800 // 4

    def test_empty_message_floor_is_one(self):
        assert estimate_tokens(UserMessage(content=[])) == 1

    def test_tool_result_message_text(self):
        m = ToolResultMessage(
            tool_call_id="a",
            tool_name="Bash",
            content=[TextContent(text="z" * 80)],
        )
        assert estimate_tokens(m) == 20

    def test_mixed_content_sums(self):
        m = AssistantMessage(
            content=[
                TextContent(text="a" * 40),  # 40 chars
                ThinkingContent(thinking="b" * 40),  # 40 chars
            ]
        )
        assert estimate_tokens(m) == (40 + 40) // 4
