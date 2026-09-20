"""Stage 4 — image-bearing tool results on the native brain.

A ``role:"tool"`` message can't portably carry image parts, so an image tool
result renders as the text-only tool message plus a follow-up ``role:"user"``
message holding the image blocks (the pattern Anthropic's compat layer honors).
Gated on the model's vision capability: a no-vision model gets a text note
instead of an image part, so a transcribe-then-reason task degrades cleanly
rather than 400ing.
"""


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
        # Vision capability now comes from config/fetched, not a bundled catalog.
        from istota.llm.catalog import set_model_overrides

        overrides = {"claude-sonnet-4-6": {"supports_vision": True}}
        set_model_overrides(overrides)
        try:
            provider = MockProvider(
                [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
            )
            NativeBrain(
                NativeBrainConfig(model="claude-sonnet-4-6", model_overrides=overrides),
                provider=provider,
            ).execute(self._req(tmp_path, "claude-sonnet-4-6"))
            assert provider.calls[0]["render_tool_images"] is True
        finally:
            set_model_overrides({})

    def test_render_tool_images_false_for_unknown_model(self, tmp_path):
        provider = MockProvider(
            [AssistantMessage(content=[TextContent(text="ok")], stop_reason="end_turn")]
        )
        NativeBrain(
            NativeBrainConfig(model="qwen-local"), provider=provider
        ).execute(self._req(tmp_path, "qwen-local"))
        assert provider.calls[0]["render_tool_images"] is False


class TestTheProducerTheProductActuallyHas:
    """Driven by `make_read_tool` rather than by `_img_tool_result()`.

    Everything above was written before anything in the tree produced an
    `ImageContent`, so its subject was a fixture shaped like a tool result. The
    `Read` image arm is the first real producer, and a fixture that agrees with
    the delivery path says nothing about whether the producer does.
    """

    @staticmethod
    def _read_a_png(tmp_path):
        import asyncio
        from io import BytesIO

        from PIL import Image

        from istota.session.tools import ToolEnv, make_read_tool

        buffer = BytesIO()
        Image.new("RGB", (48, 32), (7, 8, 9)).save(buffer, format="PNG")
        path = tmp_path / "shot.png"
        path.write_bytes(buffer.getvalue())

        tool = make_read_tool(ToolEnv(cwd=tmp_path))
        return asyncio.run(tool.execute("c1", {"file_path": str(path)}, None, None))

    @staticmethod
    def _as_tool_result(result):
        return ToolResultMessage(
            tool_call_id="c1", tool_name="Read", content=result.content,
        )

    def test_a_read_result_reaches_a_vision_model_as_an_image(self, tmp_path):
        message = self._as_tool_result(self._read_a_png(tmp_path))

        body = _provider()._build_chat_completion_request(
            "", [message], [], "m", 100, render_tool_images=True,
        )

        assert [m["role"] for m in body["messages"]] == ["tool", "user"]
        # The text block the arm emits first is what the tool message carries.
        assert "image/png" in body["messages"][0]["content"]
        assert "48x32 pixels" in body["messages"][0]["content"]
        part = next(
            p for p in body["messages"][1]["content"]
            if p.get("type") == "image_url"
        )
        assert part["image_url"]["url"].startswith("data:image/png;base64,")

    def test_a_no_vision_model_is_told_what_it_was_handed(self, tmp_path):
        from istota.untrusted import IMAGE_NOTICE

        message = self._as_tool_result(self._read_a_png(tmp_path))

        body = _provider()._build_chat_completion_request(
            "", [message], [], "m", 100, render_tool_images=False,
        )

        assert not _has_image_part(body)
        tool_text = body["messages"][0]["content"]
        # The model reads what it was given rather than an unexplained
        # omission, so it can say so and stop instead of looping blind.
        assert "shot.png" in tool_text
        assert "48x32 pixels" in tool_text
        assert IMAGE_NOTICE in tool_text

    def test_it_survives_the_tool_server_wire(self, tmp_path):
        from istota.session.tools.remote import content_from_wire, content_to_wire

        result = self._read_a_png(tmp_path)

        # The tools run in their own bwrap namespace, so every block the arm
        # produces crosses a socket before it reaches the loop.
        round_tripped = content_from_wire(content_to_wire(result.content))

        assert [type(b) for b in round_tripped] == [type(b) for b in result.content]
        assert round_tripped[1].data == result.content[1].data
        assert round_tripped[1].display_name == "shot.png"
