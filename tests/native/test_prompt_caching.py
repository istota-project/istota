"""Opt-in prompt caching for the OpenAI-compatible provider.

Anthropic / OpenRouter honor ``cache_control: {"type": "ephemeral"}``
breakpoints on message content blocks. The native brain's large stable prefix
is the system message plus the first user message (the composed prompt), so we
mark those when caching is enabled. Default off — a plain OpenAI / local
endpoint that doesn't understand the field is never sent it.
"""

from istota.llm.openai_compat import OpenAICompatibleProvider
from istota.llm.types import TextContent, UserMessage


def _provider(prompt_caching=False):
    return OpenAICompatibleProvider(
        api_key="k", base_url="https://x/v1", prompt_caching=prompt_caching
    )


def _user(text):
    return UserMessage(content=[TextContent(text=text)])


def _cache_control_blocks(body):
    found = []
    for m in body["messages"]:
        content = m.get("content")
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and "cache_control" in block:
                    found.append((m["role"], block))
    return found


class TestCachingOff:
    def test_no_cache_control_by_default(self):
        body = _provider(False)._build_chat_completion_request(
            "you are helpful", [_user("hello")], [], "m", 100
        )
        assert _cache_control_blocks(body) == []
        # system stays a plain string when caching is off
        assert body["messages"][0]["content"] == "you are helpful"


class TestCachingOn:
    def test_system_message_marked(self):
        body = _provider(True)._build_chat_completion_request(
            "big system prompt", [_user("hi")], [], "m", 100
        )
        sys_msg = body["messages"][0]
        assert sys_msg["role"] == "system"
        assert isinstance(sys_msg["content"], list)
        assert sys_msg["content"][-1]["cache_control"] == {"type": "ephemeral"}
        assert sys_msg["content"][-1]["text"] == "big system prompt"

    def test_first_user_message_marked(self):
        body = _provider(True)._build_chat_completion_request(
            "", [_user("the big composed prompt")], [], "m", 100
        )
        roles = [r for r, _ in _cache_control_blocks(body)]
        assert "user" in roles

    def test_first_and_rolling_user_marked(self):
        # New policy: the first user message (stable prefix) AND the last message
        # (rolling breakpoint) are both marked. With two user messages those are
        # distinct, so there are two user breakpoints.
        body = _provider(True)._build_chat_completion_request(
            "", [_user("first"), _user("second")], [], "m", 100
        )
        marked = _cache_control_blocks(body)
        user_texts = sorted(b["text"] for r, b in marked if r == "user")
        assert user_texts == ["first", "second"]

    def test_no_system_when_empty(self):
        body = _provider(True)._build_chat_completion_request(
            "", [_user("hi")], [], "m", 100
        )
        assert body["messages"][0]["role"] == "user"


def _count_breakpoints(body):
    n = 0
    for m in body["messages"]:
        content = m.get("content")
        if isinstance(content, list):
            n += sum(
                1 for b in content if isinstance(b, dict) and "cache_control" in b
            )
    for t in body.get("tools", []):
        fn = t.get("function") if isinstance(t, dict) else None
        if isinstance(fn, dict) and "cache_control" in fn:
            n += 1
    return n


_CC = {"type": "ephemeral"}


class TestBreakpointPolicy:
    def test_tool_defs_breakpoint_on_last_tool(self):
        wire = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": [{"type": "text", "text": "u"}]},
        ]
        tools_wire = [
            {"type": "function", "function": {"name": "A"}},
            {"type": "function", "function": {"name": "B"}},
        ]
        OpenAICompatibleProvider._apply_cache_breakpoints(wire, tools_wire)
        assert tools_wire[-1]["function"]["cache_control"] == _CC
        assert "cache_control" not in tools_wire[0]["function"]

    def test_system_first_user_and_rolling_marked(self):
        wire = [
            {"role": "system", "content": "s"},
            {"role": "user", "content": [{"type": "text", "text": "first"}]},
            {"role": "assistant", "content": "a"},
            {"role": "user", "content": [{"type": "text", "text": "latest"}]},
        ]
        OpenAICompatibleProvider._apply_cache_breakpoints(wire, None)
        assert wire[0]["content"][-1]["cache_control"] == _CC  # system
        assert wire[1]["content"][-1]["cache_control"] == _CC  # first user
        assert wire[3]["content"][-1]["cache_control"] == _CC  # rolling (last msg)

    def test_rolling_moves_to_latest_message_each_turn(self):
        msgs_turn1 = [_user("first")]
        body1 = _provider(True)._build_chat_completion_request(
            "sys", msgs_turn1, [], "m", 100
        )
        # Turn 2 appends an assistant + a new user message; the rolling
        # breakpoint should now sit on the newest message, not the first user.
        from istota.llm.types import AssistantMessage, TextContent

        msgs_turn2 = [
            _user("first"),
            AssistantMessage(content=[TextContent(text="reply")]),
            _user("second"),
        ]
        body2 = _provider(True)._build_chat_completion_request(
            "sys", msgs_turn2, [], "m", 100
        )
        last2 = body2["messages"][-1]
        assert last2["content"][-1]["text"] == "second"
        assert last2["content"][-1]["cache_control"] == _CC

    def test_never_more_than_four_breakpoints(self):
        from istota.llm.types import AssistantMessage, TextContent, ToolParameter, ToolSchema

        msgs = [_user(f"u{i}") for i in range(8)]
        # interleave assistants so the wire is long
        wired = []
        for i, m in enumerate(msgs):
            wired.append(m)
            wired.append(AssistantMessage(content=[TextContent(text=f"a{i}")]))
        tools = [
            ToolSchema(name=f"T{i}", description="d", parameters=[
                ToolParameter(name="x", type="string")
            ])
            for i in range(5)
        ]
        body = _provider(True)._build_chat_completion_request("sys", wired, tools, "m", 100)
        assert _count_breakpoints(body) <= 4


class TestCacheWriteAccounting:
    async def test_cache_creation_input_tokens_captured(self):
        async def _aiter(lines):
            for line in lines:
                yield line

        p = OpenAICompatibleProvider(api_key="k", base_url="https://x/v1")
        lines = [
            'data: {"choices":[{"delta":{"content":"x"}}]}',
            'data: {"choices":[{"delta":{},"finish_reason":"stop"}],'
            '"usage":{"prompt_tokens":100,"completion_tokens":2,'
            '"prompt_tokens_details":{"cached_tokens":40},'
            '"cache_creation_input_tokens":60}}',
            "data: [DONE]",
        ]
        events = [e async for e in p._parse_sse_lines(_aiter(lines), "m")]
        usage = events[-1].message.usage
        assert usage.cache_read_tokens == 40
        assert usage.cache_write_tokens == 60
