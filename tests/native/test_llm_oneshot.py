"""One-shot text completion helper — lightweight inference, no agent loop.

Used by Pass-2 skill routing (and any caller wanting a quick prompt->text
completion) so the native brain doesn't have to shell out to the `claude` CLI.
"""


from istota.llm.oneshot import complete_text
from istota.llm.types import AssistantMessage, TextContent

from ._mock_provider import MockProvider


def test_collects_final_text():
    provider = MockProvider([AssistantMessage(content=[TextContent(text="hello there")])])
    out = complete_text(provider, "sys", "hi", model="m")
    assert out == "hello there"


def test_passes_model_and_prompt():
    provider = MockProvider([AssistantMessage(content=[TextContent(text="ok")])])
    complete_text(provider, "be terse", "classify this", model="qwen-x", max_tokens=256)
    call = provider.calls[0]
    assert call["model"] == "qwen-x"
    assert call["max_tokens"] == 256
    assert call["system_prompt"] == "be terse"
    assert call["messages"][0].content[0].text == "classify this"


def test_stream_error_returns_none():
    provider = MockProvider(
        [AssistantMessage(content=[TextContent(text="boom")], stop_reason="error")]
    )
    assert complete_text(provider, "", "x", model="m") is None


def test_timeout_returns_none():
    class _SlowProvider:
        async def stream(self, *a, **k):
            import asyncio

            await asyncio.sleep(5)
            yield  # never reached

    assert complete_text(_SlowProvider(), "", "x", model="m", timeout=0.1) is None


def test_error_stop_reason_on_a_normal_done_returns_none():
    """OpenRouter reports an upstream generation failure as an ordinary finished
    turn whose `finish_reason` is `error`, not as a transport error. The text
    there is empty or partial, never an answer.

    This is a deliberate behaviour change (ISSUE-272): before `complete_text`
    was rebased onto `complete_message` it returned that text. Pinned so the
    difference is a decision rather than a refactor artefact.
    """
    provider = MockProvider(
        [AssistantMessage(content=[TextContent(text="partial")], stop_reason="error")]
    )
    assert complete_text(provider, "", "x", model="m") is None


def test_a_stream_error_is_stamped_with_the_error_reason():
    """`StreamError.message` defaults to a bare `AssistantMessage` whose
    `stop_reason` is `end_turn`. `acomplete_message` stamps `error` when it
    collapses the union, so a caller reading the message cannot mistake a
    failure for an empty success — and a site that forgot the field cannot
    make it one."""
    from istota.llm.oneshot import complete_message

    provider = MockProvider(
        [AssistantMessage(content=[TextContent(text="boom")], stop_reason="error")]
    )
    message = complete_message(provider, "", "x", model="m")

    assert message is not None
    assert message.stop_reason == "error"
