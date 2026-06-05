"""Phase 1 — istota.llm message and tool types."""

import dataclasses

import pytest

from istota.llm.types import (
    AssistantMessage,
    ImageContent,
    TextContent,
    ThinkingContent,
    ToolCallContent,
    ToolParameter,
    ToolResultMessage,
    ToolSchema,
    Usage,
    UserMessage,
)


class TestContentTypes:
    def test_text_content_defaults_and_tag(self):
        c = TextContent(text="hello")
        assert c.type == "text"
        assert c.text == "hello"

    def test_image_content_fields(self):
        c = ImageContent(media_type="image/png", data="abc123")
        assert c.type == "image"
        assert c.media_type == "image/png"
        assert c.data == "abc123"

    def test_tool_call_content_fields(self):
        c = ToolCallContent(id="tc_1", name="Read", arguments={"file_path": "/x"})
        assert c.type == "tool_call"
        assert c.id == "tc_1"
        assert c.name == "Read"
        assert c.arguments == {"file_path": "/x"}

    def test_thinking_content(self):
        c = ThinkingContent(thinking="reasoning")
        assert c.type == "thinking"
        assert c.thinking == "reasoning"

    def test_content_types_are_frozen(self):
        # frozen dataclasses prevent mutation during parallel tool execution
        for c in (
            TextContent(text="x"),
            ImageContent(),
            ToolCallContent(),
            ThinkingContent(),
        ):
            with pytest.raises(dataclasses.FrozenInstanceError):
                c.type = "mutated"

    def test_tool_call_arguments_independent_per_instance(self):
        a = ToolCallContent()
        b = ToolCallContent()
        # default_factory — each instance gets its own dict
        assert a.arguments is not b.arguments


class TestUsage:
    def test_total_tokens_sums_all_buckets(self):
        u = Usage(
            input_tokens=10,
            output_tokens=5,
            cache_read_tokens=3,
            cache_write_tokens=2,
        )
        assert u.total_tokens == 20

    def test_usage_zero_default(self):
        assert Usage().total_tokens == 0


class TestUserMessage:
    def test_role_and_content(self):
        m = UserMessage(content=[TextContent(text="hi")])
        assert m.role == "user"
        assert m.content[0].text == "hi"

    def test_user_message_is_mutable(self):
        # messages are mutable — the loop appends to content during streaming
        m = UserMessage()
        m.content.append(TextContent(text="added"))
        assert m.content[0].text == "added"


class TestAssistantMessage:
    def test_defaults(self):
        m = AssistantMessage()
        assert m.role == "assistant"
        assert m.stop_reason == "end_turn"
        assert m.error_message is None
        assert isinstance(m.usage, Usage)

    def test_tool_calls_property_filters_tool_call_content(self):
        m = AssistantMessage(
            content=[
                TextContent(text="let me look"),
                ToolCallContent(id="a", name="Read"),
                ThinkingContent(thinking="hmm"),
                ToolCallContent(id="b", name="Grep"),
            ]
        )
        calls = m.tool_calls
        assert [c.id for c in calls] == ["a", "b"]

    def test_tool_calls_empty_when_none(self):
        assert AssistantMessage(content=[TextContent(text="done")]).tool_calls == []

    def test_text_property_joins_text_blocks(self):
        m = AssistantMessage(
            content=[
                TextContent(text="line one"),
                ToolCallContent(id="a", name="Read"),
                TextContent(text="line two"),
            ]
        )
        assert m.text == "line one\nline two"

    def test_text_property_ignores_non_text(self):
        m = AssistantMessage(content=[ToolCallContent(id="a", name="Read")])
        assert m.text == ""


class TestToolResultMessage:
    def test_defaults(self):
        m = ToolResultMessage(tool_call_id="tc_1", tool_name="Read")
        assert m.role == "tool_result"
        assert m.tool_call_id == "tc_1"
        assert m.tool_name == "Read"
        assert m.is_error is False
        assert m.content == []

    def test_error_result(self):
        m = ToolResultMessage(
            tool_call_id="tc_1",
            tool_name="Bash",
            content=[TextContent(text="boom")],
            is_error=True,
        )
        assert m.is_error is True
        assert m.content[0].text == "boom"


class TestToolSchema:
    def test_tool_parameter_defaults(self):
        p = ToolParameter(name="path", type="string")
        assert p.required is True
        assert p.enum is None
        assert p.properties is None

    def test_tool_schema_holds_parameters(self):
        s = ToolSchema(
            name="Read",
            description="Read a file",
            parameters=[ToolParameter(name="file_path", type="string")],
        )
        assert s.name == "Read"
        assert s.parameters[0].name == "file_path"

    def test_tool_schema_default_empty_parameters(self):
        s = ToolSchema(name="Glob", description="glob")
        assert s.parameters == []
