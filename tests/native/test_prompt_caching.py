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

    def test_only_first_user_message_marked(self):
        body = _provider(True)._build_chat_completion_request(
            "", [_user("first"), _user("second")], [], "m", 100
        )
        marked = _cache_control_blocks(body)
        # exactly one user breakpoint, on the first user message
        user_marks = [b for r, b in marked if r == "user"]
        assert len(user_marks) == 1
        assert user_marks[0]["text"] == "first"

    def test_no_system_when_empty(self):
        body = _provider(True)._build_chat_completion_request(
            "", [_user("hi")], [], "m", 100
        )
        assert body["messages"][0]["role"] == "user"
