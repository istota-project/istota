"""Stage 2 — inbound image attachments on the native brain's first turn.

``BrainRequest.images`` carries prepared attachments (``image_attachments``
produces them) across the brain boundary without their bytes. NativeBrain is
the brain that turns them into provider content: on a vision model the initial
user message is one ``TextContent`` with the composed prompt followed by one
``ImageContent`` per image, which is the order OpenRouter's image-understanding
guide documents. On a model that declares no vision support no image block is
sent at all and each image gets a named text omission instead, so the request
stays valid and the model is never told it saw something it did not.
"""

import base64

from istota.brain import BrainRequest, ImageInput
from istota.brain.native import NativeBrain
from istota.config import NativeBrainConfig
from istota.llm.catalog import set_model_overrides
from istota.llm.openai_compat import OpenAICompatibleProvider
from istota.llm.types import (
    AssistantMessage,
    ImageContent,
    TextContent,
    Usage,
)

from ._mock_provider import MockProvider

MODEL = "claude-sonnet-4-6"

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake-png-payload"
JPEG_BYTES = b"\xff\xd8\xff\xe0" + b"fake-jpeg-payload"


def _image(tmp_path, name: str, data: bytes, media_type: str) -> ImageInput:
    path = tmp_path / name
    path.write_bytes(data)
    return ImageInput(
        path=path.resolve(), media_type=media_type, display_name=name
    )


def _req(cwd, images, prompt="describe the attachment") -> BrainRequest:
    return BrainRequest(
        prompt=prompt,
        allowed_tools=[],
        cwd=cwd,
        env={},
        timeout_seconds=30,
        model=MODEL,
        images=images,
    )


def _brain(provider) -> NativeBrain:
    return NativeBrain(NativeBrainConfig(model=MODEL), provider=provider)


def _one_turn() -> MockProvider:
    return MockProvider(
        [
            AssistantMessage(
                content=[TextContent(text="ok")],
                usage=Usage(input_tokens=10, output_tokens=2),
                stop_reason="end_turn",
            )
        ]
    )


def _first_message(provider):
    return provider.calls[0]["messages"][0]


def _wire(msg) -> dict:
    return OpenAICompatibleProvider._message_to_wire(msg)


class TestVisionModel:
    """A model that declares vision gets real image blocks."""

    def setup_method(self):
        set_model_overrides({MODEL: {"supports_vision": True}})

    def teardown_method(self):
        set_model_overrides({})

    def test_text_first_then_one_block_per_image(self, tmp_path):
        images = [
            _image(tmp_path, "shot.png", PNG_BYTES, "image/png"),
            _image(tmp_path, "photo.jpg", JPEG_BYTES, "image/jpeg"),
        ]
        provider = _one_turn()
        _brain(provider).execute(_req(tmp_path, images))

        content = _first_message(provider).content
        assert isinstance(content[0], TextContent)
        assert content[0].text == "describe the attachment"
        assert [type(c) for c in content[1:]] == [ImageContent, ImageContent]
        assert [c.media_type for c in content[1:]] == ["image/png", "image/jpeg"]
        assert content[1].data == base64.b64encode(PNG_BYTES).decode("ascii")
        assert content[2].data == base64.b64encode(JPEG_BYTES).decode("ascii")

    def test_display_name_travels_on_the_block(self, tmp_path):
        # The compaction loss notice names the image; the block is the only
        # thing still in the message list by then, so the name rides on it.
        images = [_image(tmp_path, "shot.png", PNG_BYTES, "image/png")]
        provider = _one_turn()
        _brain(provider).execute(_req(tmp_path, images))

        block = _first_message(provider).content[1]
        assert block.display_name == "shot.png"

    def test_the_wire_carries_a_data_url_with_the_decoded_media_type(self, tmp_path):
        images = [_image(tmp_path, "shot.png", PNG_BYTES, "image/png")]
        provider = _one_turn()
        _brain(provider).execute(_req(tmp_path, images))

        parts = _wire(_first_message(provider))["content"]
        assert parts[0] == {"type": "text", "text": "describe the attachment"}
        assert parts[1]["type"] == "image_url"
        expected = base64.b64encode(PNG_BYTES).decode("ascii")
        assert parts[1]["image_url"]["url"] == f"data:image/png;base64,{expected}"

    def test_an_unreadable_image_becomes_a_notice_and_the_rest_still_send(
        self, tmp_path
    ):
        good = _image(tmp_path, "shot.png", PNG_BYTES, "image/png")
        gone = _image(tmp_path, "vanished.jpg", JPEG_BYTES, "image/jpeg")
        gone.path.unlink()

        provider = _one_turn()
        _brain(provider).execute(_req(tmp_path, [good, gone]))

        content = _first_message(provider).content
        assert [type(c) for c in content] == [TextContent, ImageContent, TextContent]
        assert content[1].display_name == "shot.png"
        notice = content[2].text
        assert "vanished.jpg" in notice
        assert "could not be read" in notice
        # A read failure must never be reported as sight.
        assert str(tmp_path) not in notice

    def test_no_images_leaves_the_message_exactly_as_before(self, tmp_path):
        provider = _one_turn()
        _brain(provider).execute(_req(tmp_path, []))
        assert _first_message(provider).content == [
            TextContent(text="describe the attachment")
        ]


class TestNoVisionModel:
    """No override installed, so the conservative catalog default applies."""

    def test_no_image_block_and_one_named_omission_per_image(self, tmp_path):
        images = [
            _image(tmp_path, "shot.png", PNG_BYTES, "image/png"),
            _image(tmp_path, "photo.jpg", JPEG_BYTES, "image/jpeg"),
        ]
        provider = _one_turn()
        _brain(provider).execute(_req(tmp_path, images))

        content = _first_message(provider).content
        assert all(isinstance(c, TextContent) for c in content)
        assert content[0].text == "describe the attachment"
        assert content[1].text == (
            "[image shot.png omitted: selected model does not declare vision "
            "support; OCR context may still be available]"
        )
        assert content[2].text == (
            "[image photo.jpg omitted: selected model does not declare vision "
            "support; OCR context may still be available]"
        )

    def test_the_wire_has_no_image_url_part(self, tmp_path):
        images = [_image(tmp_path, "shot.png", PNG_BYTES, "image/png")]
        provider = _one_turn()
        _brain(provider).execute(_req(tmp_path, images))

        parts = _wire(_first_message(provider))["content"]
        assert all(p["type"] == "text" for p in parts)

    def test_the_file_is_never_read(self, tmp_path):
        # No vision means no base64 pass at all: the bytes must not be loaded
        # only to be thrown away.
        images = [_image(tmp_path, "shot.png", PNG_BYTES, "image/png")]
        images[0].path.unlink()

        provider = _one_turn()
        result = _brain(provider).execute(_req(tmp_path, images))

        assert result.success is True
        content = _first_message(provider).content
        assert "does not declare vision support" in content[1].text


class TestCompactionKeepsTheImages:
    """The image-bearing message sits at index 0 and ``find_cut_point`` walks
    back from the newest, so the first compaction of a long task is exactly the
    cut that drops it. Pinning it ahead of the summary keeps a bounded, known
    number of blocks — the task's own attachments — rather than the unbounded
    history the cut exists to shed."""

    def setup_method(self):
        set_model_overrides({MODEL: {"supports_vision": True}})

    def teardown_method(self):
        set_model_overrides({})

    def test_the_rebuilt_list_still_leads_with_the_image_blocks(self, tmp_path):
        from istota.llm.types import ToolCallContent

        images = [_image(tmp_path, "shot.png", PNG_BYTES, "image/png")]
        provider = MockProvider(
            [
                # Turn 1: a tool call, so the loop runs prepare_next_turn and
                # continues. Its reported usage is what trips should_compact.
                AssistantMessage(
                    content=[
                        ToolCallContent(
                            id="c1",
                            name="Write",
                            arguments={"file_path": "out.txt", "content": "hi"},
                        )
                    ],
                    usage=Usage(input_tokens=5000, output_tokens=10),
                    stop_reason="tool_use",
                ),
                # The compaction summary call draws from the same script.
                AssistantMessage(
                    content=[TextContent(text="## Goal\nlook at the screenshot")],
                    stop_reason="end_turn",
                ),
                AssistantMessage(
                    content=[TextContent(text="done")],
                    usage=Usage(input_tokens=20, output_tokens=5),
                    stop_reason="end_turn",
                ),
            ]
        )
        brain = NativeBrain(
            NativeBrainConfig(
                model=MODEL,
                context_window=100,
                compaction_keep_recent_tokens=1,
            ),
            provider=provider,
        )
        req = _req(tmp_path, images)
        req.allowed_tools = ["Write"]
        result = brain.execute(req)

        assert result.success is True
        assert len(provider.calls) == 3, "expected turn, summary, post-compaction turn"

        # The summary prompt records the loss, for the case where the pin is
        # ever unavailable.
        summary_prompt = provider.calls[1]["messages"][-1].content[0].text
        assert "[image shot.png — no longer in context]" in summary_prompt

        # And the post-compaction turn still leads with the real blocks.
        after = provider.calls[2]["messages"]
        assert isinstance(after[0].content[1], ImageContent)
        assert after[0].content[1].display_name == "shot.png"
        assert "[Summary of earlier conversation]" in after[1].content[0].text
