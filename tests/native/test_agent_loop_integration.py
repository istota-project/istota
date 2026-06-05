"""Phase 2 — integration: real tools driven by the agent loop + mock provider.

Verifies the full dispatch path (coercion, prepare/execute/finalize, event
lifecycle) against the actual Read/Write/Edit/Grep/Glob/Bash implementations
rather than stub tools. The provider is still scripted, so behavior stays
deterministic and offline.
"""

import pytest

from istota.agent.loop import run_agent_loop
from istota.agent.sanitize import sanitize_tool_pairs
from istota.agent.types import AgentContext, AgentLoopConfig
from istota.llm.types import (
    AssistantMessage,
    Message,
    TextContent,
    ToolCallContent,
    ToolResultMessage,
    UserMessage,
)
from istota.session.tools import ToolEnv, build_default_tools

from ._mock_provider import MockProvider

pytestmark = pytest.mark.asyncio


def _convert(messages: list) -> list[Message]:
    llm = [m for m in messages if isinstance(m, (UserMessage, AssistantMessage, ToolResultMessage))]
    return sanitize_tool_pairs(llm)


def _config(provider, tmp_path):
    return AgentLoopConfig(provider=provider, model="mock", convert_to_llm=_convert)


class _NullSink:
    async def __call__(self, event):  # noqa: D401
        return None


def _tool_turn(name, args, call_id="c1"):
    return AssistantMessage(
        content=[ToolCallContent(id=call_id, name=name, arguments=args)],
        stop_reason="tool_use",
    )


def _text_turn(text):
    return AssistantMessage(content=[TextContent(text=text)], stop_reason="end_turn")


def _results(out):
    return [m for m in out if isinstance(m, ToolResultMessage)]


class TestRealToolDispatch:
    async def test_write_then_read_roundtrip(self, tmp_path):
        provider = MockProvider(
            [
                _tool_turn("Write", {"file_path": str(tmp_path / "f.txt"), "content": "abc\n"}),
                _tool_turn("Read", {"file_path": str(tmp_path / "f.txt")}, call_id="c2"),
                _text_turn("done"),
            ]
        )
        env = ToolEnv(cwd=tmp_path)
        ctx = AgentContext(system_prompt="sys", messages=[], tools=build_default_tools(env))
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="write and read")])],
            ctx,
            _config(provider, tmp_path),
            _NullSink(),
        )
        assert (tmp_path / "f.txt").read_text() == "abc\n"
        read_result = _results(out)[1]
        assert "abc" in read_result.content[0].text

    async def test_coercion_through_real_read(self, tmp_path):
        f = tmp_path / "f.txt"
        f.write_text("\n".join(f"l{i}" for i in range(1, 21)) + "\n")
        # offset/limit arrive as strings — coercion must turn them into ints.
        provider = MockProvider(
            [
                _tool_turn("Read", {"file_path": str(f), "offset": "5", "limit": "2"}),
                _text_turn("done"),
            ]
        )
        env = ToolEnv(cwd=tmp_path)
        ctx = AgentContext(system_prompt="sys", messages=[], tools=build_default_tools(env))
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="read page")])],
            ctx,
            _config(provider, tmp_path),
            _NullSink(),
        )
        text = _results(out)[0].content[0].text
        assert "5\tl5" in text
        assert "6\tl6" in text
        assert "l7" not in text

    async def test_bash_through_loop(self, tmp_path):
        provider = MockProvider(
            [
                _tool_turn("Bash", {"command": "echo from-bash"}),
                _text_turn("done"),
            ]
        )
        env = ToolEnv(cwd=tmp_path)
        ctx = AgentContext(system_prompt="sys", messages=[], tools=build_default_tools(env))
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="run it")])],
            ctx,
            _config(provider, tmp_path),
            _NullSink(),
        )
        assert "from-bash" in _results(out)[0].content[0].text

    async def test_parallel_reads_no_path_overlap(self, tmp_path):
        (tmp_path / "a.txt").write_text("AAA\n")
        (tmp_path / "b.txt").write_text("BBB\n")
        # Two Read calls in one assistant turn — both parallel-safe, distinct paths.
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[
                        ToolCallContent(id="c1", name="Read", arguments={"file_path": str(tmp_path / "a.txt")}),
                        ToolCallContent(id="c2", name="Read", arguments={"file_path": str(tmp_path / "b.txt")}),
                    ],
                    stop_reason="tool_use",
                ),
                _text_turn("done"),
            ]
        )
        env = ToolEnv(cwd=tmp_path)
        cfg = AgentLoopConfig(
            provider=provider, model="mock", convert_to_llm=_convert, tool_execution="parallel"
        )
        ctx = AgentContext(system_prompt="sys", messages=[], tools=build_default_tools(env))
        out = await run_agent_loop(
            [UserMessage(content=[TextContent(text="read both")])],
            ctx,
            cfg,
            _NullSink(),
        )
        results = _results(out)
        assert len(results) == 2
        joined = " ".join(r.content[0].text for r in results)
        assert "AAA" in joined and "BBB" in joined
