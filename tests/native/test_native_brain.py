"""NativeBrain — the Brain-protocol adapter over the three-layer stack."""

import json
from pathlib import Path

from istota.brain import BrainRequest, make_brain
from istota.brain.native import NativeBrain
from istota.config import BrainConfig, NativeBrainConfig
from istota.llm.types import AssistantMessage, TextContent, ToolCallContent, Usage

from ._mock_provider import MockProvider


def _req(prompt: str, cwd: Path, tools: list[str] | None = None) -> BrainRequest:
    return BrainRequest(
        prompt=prompt,
        allowed_tools=tools if tools is not None else [],
        cwd=cwd,
        env={},
        timeout_seconds=30,
        model="claude-sonnet-4-6",
    )


def _brain(provider, **cfg) -> NativeBrain:
    config = NativeBrainConfig(model="claude-sonnet-4-6", **cfg)
    return NativeBrain(config, provider=provider)


class TestTextCompletion:
    def test_simple_completion(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="Hello there.")],
                    usage=Usage(input_tokens=100, output_tokens=10),
                    stop_reason="end_turn",
                )
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is True
        assert result.result_text == "Hello there."
        assert result.stop_reason == "completed"

    def test_usage_accumulated(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="done")],
                    usage=Usage(input_tokens=200, output_tokens=20),
                    stop_reason="end_turn",
                )
            ]
        )
        result = _brain(provider).execute(_req("go", tmp_path))
        assert result.usage is not None
        assert result.usage.input_tokens == 200
        assert result.usage.output_tokens == 20
        assert result.usage.turns == 1

    def test_system_prompt_from_custom_file(self, tmp_path):
        sysfile = tmp_path / "sys.md"
        sysfile.write_text("You are a test bot.")
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        req = _req("hi", tmp_path)
        req.custom_system_prompt_path = sysfile
        _brain(provider).execute(req)
        assert provider.calls[0]["system_prompt"] == "You are a test bot."


class TestToolUse:
    def test_write_tool_then_completion(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[
                        ToolCallContent(
                            id="c1",
                            name="Write",
                            arguments={"file_path": "out.txt", "content": "hi"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(
                    content=[TextContent(text="Wrote the file.")],
                    stop_reason="end_turn",
                ),
            ]
        )
        req = _req("write a file", tmp_path, tools=["Write", "Read"])
        result = _brain(provider).execute(req)
        assert result.success is True
        assert result.result_text == "Wrote the file."
        assert (tmp_path / "out.txt").read_text() == "hi"

    def test_trace_and_actions(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[
                        ToolCallContent(
                            id="c1",
                            name="Write",
                            arguments={"file_path": "out.txt", "content": "hi"},
                        )
                    ],
                    stop_reason="tool_use",
                ),
                AssistantMessage(
                    content=[TextContent(text="Done.")], stop_reason="end_turn"
                ),
            ]
        )
        req = _req("write", tmp_path, tools=["Write"])
        result = _brain(provider).execute(req)
        trace = json.loads(result.execution_trace)
        assert any(e["type"] == "tool" for e in trace)
        assert any(e["type"] == "text" for e in trace)
        actions = json.loads(result.actions_taken)
        assert any("out.txt" in a for a in actions)

    def test_only_allowed_tools_exposed(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        req = _req("hi", tmp_path, tools=["Read"])
        _brain(provider).execute(req)
        tool_names = {t.name for t in provider.calls[0]["tools"]}
        assert tool_names == {"Read"}


class TestErrorAndStops:
    def test_error_stop(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="")],
                    stop_reason="error",
                    error_message="HTTP 400: bad",
                )
            ]
        )
        result = _brain(provider).execute(_req("hi", tmp_path))
        assert result.success is False
        assert result.stop_reason == "error"

    def test_max_turns_stops(self, tmp_path):
        # Model keeps calling a tool forever; max_turns caps it.
        turns = [
            AssistantMessage(
                content=[
                    ToolCallContent(
                        id=f"c{i}", name="Read", arguments={"file_path": "README"}
                    )
                ],
                stop_reason="tool_use",
            )
            for i in range(20)
        ]
        provider = MockProvider(turns)
        req = _req("loop", tmp_path, tools=["Read"])
        result = _brain(provider, max_turns=3).execute(req)
        assert result.stop_reason == "max_turns"
        # Only ran up to the cap, not all 20 scripted turns.
        assert len(provider.calls) <= 4

    def test_cancellation(self, tmp_path):
        provider = MockProvider(
            [
                AssistantMessage(
                    content=[TextContent(text="first")], stop_reason="end_turn"
                ),
            ]
        )

        cancelled = {"v": False}

        def cancel_check():
            return cancelled["v"]

        req = _req("hi", tmp_path)
        req.cancel_check = cancel_check
        cancelled["v"] = True  # cancelled before the run starts
        result = _brain(provider).execute(req)
        assert result.stop_reason == "cancelled"
        assert result.success is False


class TestFactory:
    def test_make_brain_native(self):
        cfg = BrainConfig(kind="native", native=NativeBrainConfig(model="claude-sonnet-4-6"))
        brain = make_brain(cfg)
        assert isinstance(brain, NativeBrain)

    def test_resolver_methods_delegate(self):
        brain = NativeBrain(NativeBrainConfig(model="claude-sonnet-4-6"), provider=object())
        assert brain.resolve_model_name("sonnet") == "claude-sonnet-4-6"
        assert brain.resolve_alias("smart") == ("claude-opus-4-8", None)
        assert isinstance(brain.list_aliases(), list)
        assert brain.validate_role_override("smart", "claude-sonnet-4-6") == []
