"""Stage 2 — inbound image attachments on the native brain's first turn.

``BrainRequest.images`` carries prepared attachments (``image_attachments``
produces them) across the brain boundary without their bytes. NativeBrain is
the brain that turns them into provider content: on a vision model the initial
user message is one ``TextContent`` with ``req.prompt`` — the user half of the
composed prompt — followed by one
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

        # Through the real request builder, not just `_message_to_wire`: the
        # prompt-cache breakpoints are applied there, and a message whose last
        # block is an image is a shape that path had never seen.
        body = OpenAICompatibleProvider(
            api_key="k", base_url="https://x/v1"
        )._build_chat_completion_request(
            "", [_first_message(provider)], [], MODEL, 100
        )
        parts = body["messages"][0]["content"]
        assert parts[0]["type"] == "text"
        assert parts[0]["text"] == "describe the attachment"
        assert parts[1]["type"] == "image_url"
        expected = base64.b64encode(PNG_BYTES).decode("ascii")
        assert parts[1]["image_url"]["url"] == f"data:image/png;base64,{expected}"

    def test_an_oversized_file_is_refused_rather_than_encoded(self, tmp_path):
        # The second read of a path under the user temp dir, which is bound
        # read-write into that user's own sandboxes. Preparation's budget bound
        # the file as it stood then; this is a later read of the same name.
        from istota.brain.native import _MAX_IMAGE_BYTES

        big = tmp_path / "swapped.png"
        big.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * (_MAX_IMAGE_BYTES + 1))
        images = [
            ImageInput(path=big.resolve(), media_type="image/png", display_name="swapped.png")
        ]
        provider = _one_turn()
        _brain(provider).execute(_req(tmp_path, images))

        content = _first_message(provider).content
        assert [type(c) for c in content] == [TextContent, TextContent]
        assert "swapped.png" in content[1].text
        assert "could not be read" in content[1].text

    def test_the_read_cap_stays_inside_preparations_own_budget(self):
        from istota.brain.native import _MAX_IMAGE_BYTES
        from istota.image_attachments import MAX_ENCODED_BYTES, encoded_len

        # Restated in the brain rather than imported (a brain module importing
        # image_attachments closes a cycle through brain/__init__), so the two
        # are held equal here instead.
        assert encoded_len(_MAX_IMAGE_BYTES) <= MAX_ENCODED_BYTES

    def test_images_none_is_tolerated(self, tmp_path):
        # Several sibling BrainRequest fields use None meaningfully, so a caller
        # writing `images=None` is a natural mistake — and nothing on this path
        # may fail a task over an attachment.
        provider = _one_turn()
        req = _req(tmp_path, [])
        req.images = None
        result = _brain(provider).execute(req)
        assert result.success is True
        assert _first_message(provider).content == [
            TextContent(text="describe the attachment")
        ]

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

    @staticmethod
    def _tool_turn():
        from istota.llm.types import ToolCallContent

        # A tool call, so the loop runs prepare_next_turn and continues. The
        # reported usage is what trips should_compact against the tiny window.
        return AssistantMessage(
            content=[
                # Bulky narration, so the walk back from the newest reaches the
                # recent budget before index 0 and the cut lands on this turn.
                TextContent(text="z" * 20_000),
                ToolCallContent(
                    id="c1",
                    name="Write",
                    arguments={"file_path": "out.txt", "content": "hi"},
                ),
            ],
            usage=Usage(input_tokens=5000, output_tokens=10),
            stop_reason="tool_use",
        )

    @staticmethod
    def _summary_turn(text="## Goal\nlook at the screenshot"):
        # The compaction summary call draws from the same script.
        return AssistantMessage(
            content=[TextContent(text=text)], stop_reason="end_turn"
        )

    @staticmethod
    def _final_turn():
        return AssistantMessage(
            content=[TextContent(text="done")],
            usage=Usage(input_tokens=20, output_tokens=5),
            stop_reason="end_turn",
        )

    @staticmethod
    def _brain_with_a_tiny_window():
        # A window small enough that any turn trips `should_compact`, and a
        # recent budget the bulky assistant turn above clears on its own while
        # still leaving room for the pin's own ~1200 tokens.
        return NativeBrainConfig(
            model=MODEL, context_window=100, compaction_keep_recent_tokens=3000
        )

    @staticmethod
    def _image_messages(messages):
        return [
            m
            for m in messages
            if any(isinstance(c, ImageContent) for c in getattr(m, "content", []))
        ]

    def test_the_rebuilt_list_still_leads_with_the_image_blocks(self, tmp_path):
        images = [_image(tmp_path, "shot.png", PNG_BYTES, "image/png")]
        provider = MockProvider(
            [self._tool_turn(), self._summary_turn(), self._final_turn()]
        )
        brain = NativeBrain(self._brain_with_a_tiny_window(), provider=provider)
        req = _req(tmp_path, images)
        req.allowed_tools = ["Write"]
        result = brain.execute(req)

        assert result.success is True
        assert len(provider.calls) == 3, "expected turn, summary, post-compaction turn"

        after = provider.calls[2]["messages"]
        assert isinstance(after[0].content[1], ImageContent)
        assert after[0].content[1].display_name == "shot.png"
        assert "[Summary of earlier conversation]" in after[1].content[0].text

    def test_the_pin_carries_the_blocks_and_not_the_user_half(self, tmp_path):
        # The original message is the task's user half — retrieved memory,
        # conversation history, the request. Pinning it whole would make the
        # largest text in the conversation the one piece compaction can never
        # reclaim, and the summary already carries it forward in reduced form.
        # (Istota's standing instructions are not in it: they are the system
        # half, outside `ctx.messages` entirely.)
        images = [_image(tmp_path, "shot.png", PNG_BYTES, "image/png")]
        provider = MockProvider(
            [self._tool_turn(), self._summary_turn(), self._final_turn()]
        )
        brain = NativeBrain(self._brain_with_a_tiny_window(), provider=provider)
        req = _req(tmp_path, images, prompt="USER-HALF-MARKER " + "x" * 400)
        req.allowed_tools = ["Write"]
        brain.execute(req)

        pin = provider.calls[2]["messages"][0]
        assert "USER-HALF-MARKER" not in pin.content[0].text
        assert isinstance(pin.content[1], ImageContent)
        # But the summarizer did see it — that text is what the summary is for.
        assert "USER-HALF-MARKER" in provider.calls[1]["messages"][-1].content[0].text

    def test_the_summary_is_not_told_a_pinned_image_is_gone(self, tmp_path):
        # The two halves must not contradict each other. A summary saying the
        # image is gone is durable — it is updated forward on every later cycle
        # — so next to a live image block it recreates the confidently-blind
        # failure the notice exists to prevent, inverted.
        images = [_image(tmp_path, "shot.png", PNG_BYTES, "image/png")]
        provider = MockProvider(
            [self._tool_turn(), self._summary_turn(), self._final_turn()]
        )
        brain = NativeBrain(self._brain_with_a_tiny_window(), provider=provider)
        req = _req(tmp_path, images)
        req.allowed_tools = ["Write"]
        brain.execute(req)

        summary_prompt = provider.calls[1]["messages"][-1].content[0].text
        assert "no longer in context" not in summary_prompt
        assert isinstance(provider.calls[2]["messages"][0].content[1], ImageContent)

    def test_a_second_compaction_keeps_exactly_one_pin(self, tmp_path):
        images = [_image(tmp_path, "shot.png", PNG_BYTES, "image/png")]
        provider = MockProvider(
            [
                self._tool_turn(),
                self._summary_turn(),
                self._tool_turn(),
                self._summary_turn("## Goal\nstill looking at the screenshot"),
                self._final_turn(),
            ]
        )
        brain = NativeBrain(self._brain_with_a_tiny_window(), provider=provider)
        req = _req(tmp_path, images)
        req.allowed_tools = ["Write"]
        result = brain.execute(req)

        assert result.success is True
        assert len(provider.calls) == 5, "expected two compaction cycles"

        after = provider.calls[4]["messages"]
        carriers = self._image_messages(after)
        assert len(carriers) == 1, "the pin was duplicated or lost on cycle 2"
        assert carriers[0] is after[0]
        assert carriers[0].content[1].display_name == "shot.png"
        # And the second cycle's summarizer was not told the loss either.
        assert "no longer in context" not in provider.calls[3]["messages"][-1].content[0].text


class TestOverflowRecoveryKeepsTheImages:
    """The reactive force-compact needs the pin more than the proactive one, not
    less: it always sheds index 0 (`_aggressive_cut` cuts harder still), and
    once the blocks are gone no later compaction can bring them back."""

    async def test_the_recovery_context_leads_with_the_pin(self, tmp_path):
        from istota.brain.native import _build_recovery_context
        from istota.llm.provider import StreamDone
        from istota.llm.types import ToolResultMessage, UserMessage
        from istota.session.messages import CompactionSummaryMessage

        class _SummaryProvider:
            async def stream(self, system_prompt, messages, tools, **kw):
                self.prompt = messages[-1].content[0].text
                yield StreamDone(
                    message=AssistantMessage(content=[TextContent(text="SUMMARY")])
                )

        def _convert(msgs):
            return [m for m in msgs if hasattr(m, "role")]

        transcript = [
            UserMessage(
                content=[
                    TextContent(text="what is this?"),
                    ImageContent(
                        media_type="image/png", data="AAAA", display_name="shot.png"
                    ),
                ]
            ),
            AssistantMessage(content=[TextContent(text="looking")]),
            UserMessage(content=[TextContent(text="and this one?")]),
            AssistantMessage(content=[TextContent(text="")], stop_reason="error"),
        ]
        provider = _SummaryProvider()
        ctx, _summary, _details = await _build_recovery_context(
            transcript, "sys", None, None, None, provider, "m", _convert
        )

        assert isinstance(ctx.messages[0].content[1], ImageContent)
        assert ctx.messages[0].content[1].display_name == "shot.png"
        assert isinstance(ctx.messages[1], CompactionSummaryMessage)
        assert "no longer in context" not in provider.prompt
        assert not isinstance(ctx.messages[0], ToolResultMessage)

    async def test_an_oversized_block_set_is_dropped_rather_than_recarried(self):
        # This is the one path where the window has already been exceeded, so a
        # pin that cannot fit is the wrong thing to carry into the retry. It
        # falls back to the summary's loss notice, which is the stated floor.
        from istota.brain.native import _build_recovery_context
        from istota.llm.provider import StreamDone
        from istota.llm.types import UserMessage
        from istota.session.messages import CompactionSummaryMessage

        class _SummaryProvider:
            async def stream(self, system_prompt, messages, tools, **kw):
                self.prompt = messages[-1].content[0].text
                yield StreamDone(
                    message=AssistantMessage(content=[TextContent(text="SUMMARY")])
                )

        def _convert(msgs):
            return [m for m in msgs if hasattr(m, "role")]

        transcript = [
            UserMessage(
                content=[
                    TextContent(text="what is this?"),
                    ImageContent(
                        media_type="image/png", data="AAAA", display_name="shot.png"
                    ),
                ]
            ),
            AssistantMessage(content=[TextContent(text="looking")]),
            UserMessage(content=[TextContent(text="and this one?")]),
            AssistantMessage(content=[TextContent(text="")], stop_reason="error"),
        ]
        provider = _SummaryProvider()
        ctx, _summary, _details = await _build_recovery_context(
            transcript, "sys", None, None, None, provider, "m", _convert,
            keep_recent_tokens=4,  # smaller than one image block's estimate
        )

        assert isinstance(ctx.messages[0], CompactionSummaryMessage)
        assert "[image shot.png — no longer in context]" in provider.prompt
