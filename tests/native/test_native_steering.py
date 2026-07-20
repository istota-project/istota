"""Stage 3 of the !steer spec: NativeBrain steering wiring."""

import asyncio

import pytest

from istota.brain import native
from istota.brain.native import _drain_one_steer, _STEER_FRAME, NativeBrain
from istota.agent.loop import run_agent_loop
from istota.llm.types import AssistantMessage, UserMessage, TextContent

from ._mock_provider import MockProvider
from .test_agent_loop import (
    _config,
    _ctx,
    _echo_tool,
    _text_turn,
    _tool_turn,
    _Sink,
)


class TestDrainOneSteer:
    def test_empty_buffer(self):
        assert _drain_one_steer([]) == []

    def test_frames_and_pops_one(self):
        buffer = ["look at auth", "and the db"]
        out = _drain_one_steer(buffer)
        assert len(out) == 1
        assert isinstance(out[0], UserMessage)
        assert out[0].content[0].text == _STEER_FRAME.format(text="look at auth")
        # Only one popped; the rest remain for later turns.
        assert buffer == ["and the db"]

    def test_framing_marks_it_additive(self):
        out = _drain_one_steer(["x"])
        text = out[0].content[0].text
        assert "while you were working" in text
        assert text.endswith("x")


class TestPollSteers:
    async def test_buffers_texts_from_channel(self, monkeypatch):
        monkeypatch.setattr(native, "_STEER_POLL_INTERVAL_SECONDS", 0.01)
        abort = asyncio.Event()
        buffer: list[str] = []
        calls = {"n": 0}

        def _poll():
            calls["n"] += 1
            if calls["n"] == 1:
                return ["first steer"]
            abort.set()  # stop after the second poll
            return []

        await asyncio.wait_for(
            NativeBrain._poll_steers(_poll, buffer, abort), timeout=3
        )
        assert buffer == ["first steer"]

    async def test_transient_failure_does_not_kill_poller(self, monkeypatch):
        monkeypatch.setattr(native, "_STEER_POLL_INTERVAL_SECONDS", 0.01)
        abort = asyncio.Event()
        buffer: list[str] = []
        calls = {"n": 0}

        def _poll():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("transient DB lock")
            if calls["n"] == 2:
                return ["recovered"]
            abort.set()
            return []

        await asyncio.wait_for(
            NativeBrain._poll_steers(_poll, buffer, abort), timeout=3
        )
        # Survived the raise and still delivered the later steer.
        assert buffer == ["recovered"]


class TestSteeringInjection:
    async def test_pending_steer_reaches_context(self):
        provider = MockProvider([_tool_turn("echo", {"value": "x"}), _text_turn("done")])
        buffer = ["focus on the auth module"]
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool([])]),
            _config(
                provider,
                get_steering_messages=lambda: _drain_one_steer(buffer),
                steering_queue_mode="one_at_a_time",
            ),
            _Sink(),
        )
        injected = [
            m for m in out
            if isinstance(m, UserMessage)
            and "focus on the auth module" in m.content[0].text
        ]
        assert len(injected) == 1
        # It was framed, not raw.
        assert "while you were working" in injected[0].content[0].text

    async def test_one_at_a_time_drains_one_per_turn(self):
        # Two tool turns then text -> three boundaries, enough to inject both.
        provider = MockProvider([
            _tool_turn("echo", {"value": "a"}, call_id="c1"),
            _tool_turn("echo", {"value": "b"}, call_id="c2"),
            _text_turn("done"),
        ])
        buffer = ["steer-one", "steer-two"]
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(tools=[_echo_tool([])]),
            _config(
                provider,
                get_steering_messages=lambda: _drain_one_steer(buffer),
                steering_queue_mode="one_at_a_time",
            ),
            _Sink(),
        )
        injected = [
            m.content[0].text for m in out
            if isinstance(m, UserMessage)
            and ("steer-one" in m.content[0].text or "steer-two" in m.content[0].text)
        ]
        assert len(injected) == 2
        assert "steer-one" in injected[0]
        assert "steer-two" in injected[1]
        assert buffer == []

    async def test_no_steer_is_a_noop(self):
        # Empty channel -> no injected user turns, byte-identical to plain run.
        provider = MockProvider([_text_turn("done")])
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="go")])],
            _ctx(),
            _config(
                provider,
                get_steering_messages=lambda: _drain_one_steer([]),
                steering_queue_mode="one_at_a_time",
            ),
            _Sink(),
        )
        user_turns = [m for m in out if isinstance(m, UserMessage)]
        # Only the original prompt.
        assert len(user_turns) == 1
        assert user_turns[0].content[0].text == "go"
        assert [m.text for m in out if isinstance(m, AssistantMessage)] == ["done"]
