"""Stage 4 — image-bearing tool results on the native brain.

A ``role:"tool"`` message can't portably carry image parts, so an image tool
result renders as the text-only tool message plus a follow-up ``role:"user"``
message holding the image blocks (the pattern Anthropic's compat layer honors).
Gated on the model's vision capability: a no-vision model gets a text note
instead of an image part, so a transcribe-then-reason task degrades cleanly
rather than 400ing.
"""

from pathlib import Path

from istota.brain import BrainRequest
from istota.brain.native import NativeBrain
from istota.config import NativeBrainConfig
from istota.llm.openai_compat import OpenAICompatibleProvider
from istota.llm.types import (
    AssistantMessage,
    ImageContent,
    TextContent,
    ToolResultMessage,
)

from ._mock_provider import MockProvider


def _provider():
    return OpenAICompatibleProvider(api_key="k", base_url="https://x/v1")


def _img_tool_result():
    return ToolResultMessage(
        tool_call_id="c1",
        tool_name="screenshot",
        content=[
            TextContent(text="captured"),
            ImageContent(media_type="image/png", data="BASE64DATA"),
        ],
    )


def _has_image_part(body):
    for m in body["messages"]:
        content = m.get("content")
        if isinstance(content, list):
            if any(isinstance(p, dict) and p.get("type") == "image_url" for p in content):
                return True
    return False


class TestVisionModel:
    def test_followup_user_message_injected_after_tool(self):
        body = _provider()._build_chat_completion_request(
            "", [_img_tool_result()], [], "m", 100, render_tool_images=True
        )
        roles = [m["role"] for m in body["messages"]]
        # tool message, then a follow-up user message
        assert roles == ["tool", "user"]
        tool_msg = body["messages"][0]
        assert "captured" in tool_msg["content"]
        follow = body["messages"][1]
        assert any(
            isinstance(p, dict) and p.get("type") == "image_url"
            for p in follow["content"]
        )
        # the image data round-trips as a data URL
        img_part = next(p for p in follow["content"] if p.get("type") == "image_url")
        assert "BASE64DATA" in img_part["image_url"]["url"]
        assert "image/png" in img_part["image_url"]["url"]

    def test_text_only_tool_result_gets_no_followup(self):
        msg = ToolResultMessage(
            tool_call_id="c1", tool_name="Read", content=[TextContent(text="data")]
        )
        body = _provider()._build_chat_completion_request(
            "", [msg], [], "m", 100, render_tool_images=True
        )
        assert [m["role"] for m in body["messages"]] == ["tool"]


class TestNoVisionModel:
    def test_image_dropped_and_text_note_substituted(self):
        body = _provider()._build_chat_completion_request(
            "", [_img_tool_result()], [], "m", 100, render_tool_images=False
        )
        assert not _has_image_part(body)
        joined = " ".join(str(m.get("content")) for m in body["messages"])
        assert "no vision" in joined

    def test_default_render_tool_images_false(self):
        # Default (no kwarg) is the safe no-vision path.
        body = _provider()._build_chat_completion_request(
            "", [_img_tool_result()], [], "m", 100
        )
        assert not _has_image_part(body)


class TestBrainGatesOnVision:
    def _req(self, cwd, model):
        return BrainRequest(
            prompt="hi", allowed_tools=[], cwd=cwd, env={}, timeout_seconds=30, model=model
        )

    def test_render_tool_images_true_for_vision_model(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        NativeBrain(
            NativeBrainConfig(model="claude-sonnet-4-6"), provider=provider
        ).execute(self._req(tmp_path, "claude-sonnet-4-6"))
        assert provider.calls[0]["render_tool_images"] is True

    def test_render_tool_images_false_for_unknown_model(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        NativeBrain(
            NativeBrainConfig(model="qwen-local"), provider=provider
        ).execute(self._req(tmp_path, "qwen-local"))
        assert provider.calls[0]["render_tool_images"] is False
