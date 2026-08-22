"""The scripted model endpoint, driven through the real SSE parser.

`tests/support/model_endpoint.py` exists because the lean compose stack runs the
daemon in a container and the test on the host, so the injection point has to be
`base_url` rather than a provider object (see the spec's Stage 6 decision). That
buys a config-level seam and costs a real HTTP surface emitting real SSE bytes —
and *nothing in the smoke tier can tell a correctly framed stream from a subtly
wrong one*. A stream that ends without `[DONE]` or a `finish_reason` is a
`StreamError` by `openai_compat.py:552-560`, which reaches the smoke tier as a
task that failed for a reason unrelated to what that test was asserting.

So the framing is pinned here instead: against `OpenAICompatibleProvider`
itself, over a real socket, in the default suite with no Docker and no daemon.
This is the file that fails when `serve_script` stops speaking the protocol.
"""

from __future__ import annotations

import pytest

from istota.llm.openai_compat import OpenAICompatibleProvider
from istota.llm.provider import StreamDone, StreamError
from istota.llm.types import TextContent, UserMessage

from .support.model_endpoint import serve_script


async def _drive(endpoint, prompt: str = "hello") -> list:
    """Run one turn through the real provider and collect its events."""
    provider = OpenAICompatibleProvider(api_key="unused", base_url=endpoint.base_url)
    try:
        return [
            event
            async for event in provider.stream(
                "you are a test",
                [UserMessage(content=[TextContent(text=prompt)])],
                [],
                model="test-model",
            )
        ]
    finally:
        await provider.aclose()


def _done(events: list) -> StreamDone:
    """The terminal event, insisting it is a clean one.

    Written as a helper because `events[-1]` alone would let a StreamError
    through into assertions about text content, where it fails as a confusing
    AttributeError rather than as "the stream was malformed".
    """
    last = events[-1]
    assert isinstance(last, StreamDone), f"stream did not complete cleanly: {last}"
    return last


class TestATextTurnSurvivesTheRealParser:
    async def test_the_text_arrives_and_the_stream_completes(self):
        with serve_script([{"text": "the answer is 42"}]) as endpoint:
            events = await _drive(endpoint)

        done = _done(events)
        assert done.message.stop_reason == "end_turn"
        assert "the answer is 42" == "".join(
            block.text for block in done.message.content if hasattr(block, "text")
        )

    async def test_the_text_is_streamed_as_deltas_not_one_lump(self):
        # Streaming reassembly is most of what `_parse_sse_lines` does, and a
        # fixture that always sent each turn whole would leave that path
        # unexercised by everything built on this module.
        with serve_script([{"text": "abcdef"}]) as endpoint:
            events = await _drive(endpoint)

        deltas = [e for e in events if type(e).__name__ == "TextDelta"]
        assert len(deltas) > 1, f"served as one chunk: {deltas}"
        assert "".join(d.text for d in deltas) == "abcdef"


class TestAToolCallTurnSurvivesTheRealParser:
    async def test_the_tool_call_is_reassembled_with_its_arguments(self):
        with serve_script(
            [
                {
                    "tool_calls": [
                        {"id": "call_1", "name": "bash", "arguments": {"command": "ls /"}}
                    ]
                }
            ]
        ) as endpoint:
            events = await _drive(endpoint)

        done = _done(events)
        assert done.message.stop_reason == "tool_use"
        calls = [b for b in done.message.content if type(b).__name__ == "ToolCallContent"]
        assert len(calls) == 1, done.message.content
        assert calls[0].name == "bash"
        assert calls[0].arguments == {"command": "ls /"}

    async def test_the_arguments_are_split_across_chunks(self):
        # Argument JSON arriving in fragments is the case `_parse_sse_lines`
        # accumulates for, and a fixture that never splits leaves it untested.
        with serve_script(
            [{"tool_calls": [{"id": "c1", "name": "bash", "arguments": {"command": "x"}}]}]
        ) as endpoint:
            events = await _drive(endpoint)

        fragments = [
            e.arguments_delta
            for e in events
            if type(e).__name__ == "ToolCallDelta" and e.arguments_delta
        ]
        assert len(fragments) > 1, f"arguments served whole: {fragments}"


class TestTheTurnsAreServedInOrder:
    async def test_each_request_advances_the_script(self):
        with serve_script([{"text": "first"}, {"text": "second"}]) as endpoint:
            first = _done(await _drive(endpoint))
            second = _done(await _drive(endpoint))

        assert "first" in first.message.content[0].text
        assert "second" in second.message.content[0].text

    async def test_running_off_the_end_is_an_error_not_a_repeat(self):
        """An unplanned extra turn must be loud.

        Replaying the last turn forever is the tempting default and it is the
        wrong one: an agent loop that called the model once more than the test
        scripted has done something the test did not describe, and quietly
        feeding it the previous answer turns that into a pass. It surfaces as a
        StreamError, which the daemon records as a failed task.
        """
        with serve_script([{"text": "only one"}]) as endpoint:
            _done(await _drive(endpoint))
            events = await _drive(endpoint)

        assert isinstance(events[-1], StreamError), events[-1]
        assert "script" in (events[-1].message.error_message or "").lower()


class TestTheEndpointRecordsWhatItWasAsked:
    async def test_the_request_body_is_captured(self):
        with serve_script([{"text": "ok"}]) as endpoint:
            await _drive(endpoint, prompt="what is the capital of France")

            assert len(endpoint.requests) == 1
            body = endpoint.requests[0]
            assert body["model"] == "test-model"
            assert body["stream"] is True
            assert any(
                "capital of France" in str(message.get("content"))
                for message in body["messages"]
            )

    async def test_the_system_prompt_is_captured(self):
        # The lean stack's real question is whether the daemon assembled the
        # prompt it was supposed to, and this is where that becomes visible.
        with serve_script([{"text": "ok"}]) as endpoint:
            await _drive(endpoint)

            body = endpoint.requests[0]
            assert any(
                message.get("role") == "system"
                and "you are a test" in str(message.get("content"))
                for message in body["messages"]
            )


class TestTheEndpointIsAddressableFromAnotherProcess:
    def test_the_default_bind_is_loopback_only(self):
        """The default must not publish a listener beyond this machine.

        Only the smoke tier needs a container to reach back in. Binding all
        interfaces unconditionally would open an unauthenticated POST endpoint
        on every `uv run pytest`, and on macOS raise the incoming-connections
        prompt — a run that appears to hang on a dialog nobody is looking at.
        """
        with serve_script([{"text": "ok"}]) as endpoint:
            assert endpoint.port > 0
            assert endpoint.base_url.endswith(f":{endpoint.port}/v1")
            assert endpoint.host_bound == "127.0.0.1"

    def test_the_bind_address_is_read_off_the_socket_not_the_argument(self):
        """Asserted against the live socket, which is the whole point.

        `host_bound` used to be the value passed in, so an assertion on it was
        satisfied by the *request* to bind rather than the bind — change the
        `ThreadingHTTPServer` call to hardcode loopback and the smoke tier would
        break while this file stayed green. That is the wrong-reason pass this
        class exists to prevent, so the field is now populated from
        `server_address` and cross-checked here.
        """
        with serve_script([{"text": "ok"}], host="0.0.0.0") as endpoint:
            assert endpoint.host_bound == "0.0.0.0"
            assert endpoint._server.server_address[0] == "0.0.0.0"
            assert endpoint._server.server_address[1] == endpoint.port

    def test_the_server_stops_when_the_context_exits(self):
        with serve_script([{"text": "ok"}]) as endpoint:
            port = endpoint.port

        with pytest.raises(OSError):
            # Rebinding the same port proves the listener is gone. Without the
            # shutdown the thread outlives the test and the next one inherits
            # a server serving the wrong script.
            import socket

            probe = socket.socket()
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                probe.connect(("127.0.0.1", port))
                probe.close()
                raise AssertionError(f"port {port} still accepts connections")
            except ConnectionRefusedError:
                raise OSError("refused, as it should be")
