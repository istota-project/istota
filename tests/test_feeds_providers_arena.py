"""Tests for the Are.na v3 API provider.

Network is stubbed at ``httpx.get``. Payload fixtures are trimmed copies of
real ``GET /v3/channels/{slug}/contents`` responses.

The behaviour under test is mostly "no block type renders blank". Before the
v3 switch only Image / Text / Link were mapped, so an ``Embed`` (a YouTube or
Vimeo link — the single most common non-image block) reached the reader with
no title copy, no body and no image, and painted an empty card.
"""

from __future__ import annotations

import pytest

from istota.feeds.providers import arena as arena_provider


class _StubResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def stub_get(monkeypatch):
    """Patch ``httpx.get`` in the provider; return a call-capture dict."""
    captured: dict = {}

    def _install(*blocks: dict):
        def _get(url, params=None, headers=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            captured["headers"] = headers
            return _StubResponse({"data": list(blocks), "meta": {}})

        monkeypatch.setattr(arena_provider.httpx, "get", _get)
        return captured

    return _install


def _rich(text: str) -> dict:
    return {"markdown": text, "html": f"<p>{text}</p>", "plain": text}


def _image(src: str = "https://d2w9rnfcy7mm78.cloudfront.net/1/original.jpg") -> dict:
    return {"src": src, "width": 800, "height": 600, "alt_text": None}


def _block(type_: str, **over) -> dict:
    base = {
        "id": 1,
        "type": type_,
        "base_type": "Block",
        "title": None,
        "description": None,
        "content": None,
        "source": None,
        "image": None,
        "state": "available",
        "created_at": "2020-01-02T03:04:05Z",
        "connection": {"connected_at": "2021-06-07T08:09:10Z", "position": 5},
        "user": {"full_name": "Ada L", "slug": "ada"},
    }
    base.update(over)
    return base


def _renderable(item) -> bool:
    """A card is non-blank when it has any of body copy, or an image."""
    return bool(item.content_text or item.content_html or item.image_urls)


# -- request shape ------------------------------------------------------------


class TestRequest:
    def test_calls_the_v3_contents_endpoint(self, stub_get):
        cap = stub_get(_block("Text", content=_rich("hi")))

        arena_provider.fetch("my-channel")

        assert cap["url"] == "https://api.are.na/v3/channels/my-channel/contents"

    def test_sorts_newest_connected_first_using_the_v3_enum(self, stub_get):
        # v3 rejects v2's split sort/direction pair with a 400; the sort key is
        # a single combined token.
        cap = stub_get(_block("Text", content=_rich("hi")))

        arena_provider.fetch("c")

        assert cap["params"]["sort"] == "position_desc"
        assert "direction" not in cap["params"]

    def test_caps_per_page_at_the_api_maximum(self, stub_get):
        cap = stub_get(_block("Text", content=_rich("hi")))

        arena_provider.fetch("c", limit=500)

        assert cap["params"]["per"] == 100

    def test_sends_a_user_agent(self, stub_get):
        cap = stub_get(_block("Text", content=_rich("hi")))

        arena_provider.fetch("c")

        assert "istota" in cap["headers"]["User-Agent"].lower()


# -- per-type mapping ---------------------------------------------------------


class TestEmbedBlocks:
    """``Embed`` (v2 called it ``Media``) — the blank-card bug."""

    def _youtube(self, **over):
        fields = {
            "id": 76969,
            "title": "The Working Sheepdog",
            "description": _rich("Border Collies in training"),
            "source": {
                "url": "https://www.youtube.com/watch?v=B0sO1wdBhMY",
                "title": "The Working Sheepdog",
                "provider": {"name": "YouTube", "url": "http://www.youtube.com/"},
            },
            "image": _image("https://cdn.are.na/76969/thumb.jpg"),
            "embed": {"type": "video", "html": "<iframe src='...'></iframe>"},
        }
        fields.update(over)
        return _block("Embed", **fields)

    def test_is_not_blank(self, stub_get):
        stub_get(self._youtube())

        (item,) = arena_provider.fetch("c")

        assert _renderable(item)

    def test_uses_the_video_thumbnail_as_the_card_image(self, stub_get):
        stub_get(self._youtube())

        (item,) = arena_provider.fetch("c")

        assert item.image_urls == ["https://cdn.are.na/76969/thumb.jpg"]

    def test_exposes_the_source_url_for_the_inline_player(self, stub_get):
        stub_get(self._youtube())

        (item,) = arena_provider.fetch("c")

        assert item.embed_url == "https://www.youtube.com/watch?v=B0sO1wdBhMY"

    def test_body_carries_the_description_and_a_source_link(self, stub_get):
        stub_get(self._youtube())

        (item,) = arena_provider.fetch("c")

        assert "Border Collies in training" in item.content_html
        assert "https://www.youtube.com/watch?v=B0sO1wdBhMY" in item.content_html

    def test_does_not_store_the_provider_iframe(self, stub_get):
        # Playback is the reader's job (it rebuilds a player from embed_url).
        # Storing a third-party iframe would force a sanitizer loosening that
        # every RSS feed would inherit.
        stub_get(self._youtube())

        (item,) = arena_provider.fetch("c")

        assert "<iframe" not in (item.content_html or "")

    def test_survives_an_embed_with_no_thumbnail(self, stub_get):
        stub_get(self._youtube(image=None))

        (item,) = arena_provider.fetch("c")

        assert item.image_urls == []
        assert _renderable(item)


class TestAttachmentBlocks:
    def _pdf(self, **over):
        fields = {
            "id": 45295848,
            "title": "dewey-art-as-experience.pdf",
            "attachment": {
                "filename": "63d0752a.pdf",
                "content_type": "application/pdf",
                "file_size": 18977966,
                "file_extension": "pdf",
                "url": "https://attachments.are.na/45295848/63d0752a.pdf",
            },
            "image": _image("https://cdn.are.na/45295848/cover.png"),
        }
        fields.update(over)
        return _block("Attachment", **fields)

    def test_is_not_blank(self, stub_get):
        stub_get(self._pdf())

        (item,) = arena_provider.fetch("c")

        assert _renderable(item)

    def test_uses_the_pdf_cover_as_the_card_image(self, stub_get):
        stub_get(self._pdf())

        (item,) = arena_provider.fetch("c")

        assert item.image_urls == ["https://cdn.are.na/45295848/cover.png"]

    def test_links_the_file_with_a_human_readable_size(self, stub_get):
        stub_get(self._pdf())

        (item,) = arena_provider.fetch("c")

        assert "https://attachments.are.na/45295848/63d0752a.pdf" in item.content_html
        assert "18.1 MB" in item.content_html

    def test_survives_an_attachment_with_no_cover(self, stub_get):
        stub_get(self._pdf(image=None))

        (item,) = arena_provider.fetch("c")

        assert _renderable(item)


class TestChannelBlocks:
    def _channel(self, **over):
        b = _block(
            "Channel",
            id=9530,
            title="Adam Curtis",
            slug="adam-curtis",
            counts={"blocks": 32, "channels": 1, "contents": 33},
            **over,
        )
        b.pop("base_type", None)
        return b

    def test_is_not_blank(self, stub_get):
        stub_get(self._channel())

        (item,) = arena_provider.fetch("c")

        assert _renderable(item)

    def test_links_to_the_nested_channel_not_a_block_permalink(self, stub_get):
        stub_get(self._channel())

        (item,) = arena_provider.fetch("c")

        assert item.url == "https://www.are.na/channel/adam-curtis"

    def test_notes_the_channel_size(self, stub_get):
        stub_get(self._channel())

        (item,) = arena_provider.fetch("c")

        assert "33" in item.content_text


class TestTextBlocks:
    def test_uses_the_prerendered_html_not_escaped_markdown(self, stub_get):
        # v2 shipped `content` as HTML-escaped markdown ("&gt; quoted"), which
        # rendered literally. v3 hands us real HTML.
        stub_get(_block("Text", content={
            "markdown": "> quoted *emphasis*",
            "html": "<blockquote><p>quoted <em>emphasis</em></p></blockquote>",
            "plain": "quoted emphasis",
        }))

        (item,) = arena_provider.fetch("c")

        assert item.content_html == "<blockquote><p>quoted <em>emphasis</em></p></blockquote>"
        assert "&gt;" not in item.content_html
        assert item.content_text == "quoted emphasis"

    def test_sanitizes_hostile_markup(self, stub_get):
        stub_get(_block("Text", content={
            "markdown": "x", "plain": "x",
            "html": "<p>ok</p><script>alert(1)</script>",
        }))

        (item,) = arena_provider.fetch("c")

        assert "<script>" not in item.content_html
        assert "ok" in item.content_html

    def test_keeps_the_attribution_alongside_the_quote(self, stub_get):
        # A Text block is the one type where `content` and `description` are
        # both body copy: the quote, and the citation naming where it's from.
        # Reading only `content` drops the attribution, which on a block of
        # quotations is the half that says what you're reading.
        stub_get(_block(
            "Text",
            title="The Man in the Arena",
            content=_rich("It is not the critic who counts"),
            description=_rich("Theodore Roosevelt, Sorbonne, 1910"),
        ))

        (item,) = arena_provider.fetch("c")

        assert "It is not the critic who counts" in item.content_html
        assert "Theodore Roosevelt, Sorbonne, 1910" in item.content_html
        assert "Theodore Roosevelt, Sorbonne, 1910" in item.content_text

    def test_quote_comes_before_its_attribution(self, stub_get):
        stub_get(_block(
            "Text",
            content=_rich("the quote"),
            description=_rich("the citation"),
        ))

        (item,) = arena_provider.fetch("c")

        assert item.content_html.index("the quote") < item.content_html.index(
            "the citation"
        )

    def test_does_not_print_the_note_twice_when_it_repeats_the_body(self, stub_get):
        stub_get(_block(
            "Text",
            content=_rich("same words"),
            description=_rich("same words"),
        ))

        (item,) = arena_provider.fetch("c")

        assert item.content_html.count("same words") == 1

    def test_a_block_carrying_only_a_description_still_renders(self, stub_get):
        stub_get(_block(
            "Text",
            title="A note",
            content={"markdown": "", "html": "", "plain": ""},
            description=_rich("the whole point of the block"),
        ))

        (item,) = arena_provider.fetch("c")

        assert "the whole point of the block" in item.content_html

    def test_an_untitled_quote_is_still_a_full_card(self, stub_get):
        # Most Text blocks have no title — the quote *is* the post. The card
        # renders body-only, which is correct, not a degraded case.
        stub_get(_block("Text", title=None, content=_rich("just a quote")))

        (item,) = arena_provider.fetch("c")

        assert item.title is None
        assert "just a quote" in item.content_html


class TestImageBlocks:
    def test_uses_src_without_a_cache_buster(self, stub_get):
        stub_get(_block("Image", title="a.jpg", image=_image("https://cdn/x.jpg")))

        (item,) = arena_provider.fetch("c")

        assert item.image_urls == ["https://cdn/x.jpg"]

    def test_keeps_the_curator_description(self, stub_get):
        # v2 dropped Image descriptions entirely — the note a curator wrote
        # about why they saved the picture was the actual content.
        stub_get(_block(
            "Image", title="a.jpg", image=_image(),
            description=_rich("Sitterwerk's dynamic library"),
        ))

        (item,) = arena_provider.fetch("c")

        assert "Sitterwerk's dynamic library" in item.content_html


class TestLinkBlocks:
    def test_carries_image_description_and_source(self, stub_get):
        stub_get(_block(
            "Link",
            title="How Do People Get New Ideas?",
            description=_rich("Note from Arthur Obermayer"),
            source={"url": "https://technologyreview.com/s/531911/",
                    "provider": {"name": "MIT Technology Review"}},
            image=_image("https://cdn/link.png"),
        ))

        (item,) = arena_provider.fetch("c")

        assert item.image_urls == ["https://cdn/link.png"]
        assert "Note from Arthur Obermayer" in item.content_html
        assert "https://technologyreview.com/s/531911/" in item.content_html


# -- shared field mapping -----------------------------------------------------


class TestCommonFields:
    def test_guid_is_the_block_id_so_v2_entries_are_not_reinserted(self, stub_get):
        # v2 and v3 return the same block ids; guid must stay the bare id or
        # every existing entry would re-appear as unread on upgrade.
        stub_get(_block("Text", id=8693, content=_rich("hi")))

        (item,) = arena_provider.fetch("c")

        assert item.guid == "8693"

    def test_permalink_is_the_block_page(self, stub_get):
        stub_get(_block("Image", id=314421, image=_image()))

        (item,) = arena_provider.fetch("c")

        assert item.url == "https://www.are.na/block/314421"

    def test_published_at_prefers_connected_at(self, stub_get):
        stub_get(_block("Text", content=_rich("hi")))

        (item,) = arena_provider.fetch("c")

        assert item.published_at == "2021-06-07T08:09:10+00:00"

    def test_published_at_falls_back_to_created_at(self, stub_get):
        stub_get(_block("Text", content=_rich("hi"), connection=None))

        (item,) = arena_provider.fetch("c")

        assert item.published_at == "2020-01-02T03:04:05+00:00"

    def test_author_comes_from_the_block_user(self, stub_get):
        stub_get(_block("Text", content=_rich("hi")))

        (item,) = arena_provider.fetch("c")

        assert item.author == "Ada L"

    def test_reads_the_v3_data_envelope(self, stub_get):
        # v2 returned {"contents": [...]}, v3 returns {"data": [...], "meta": …}
        stub_get(_block("Text", content=_rich("a")), _block("Text", id=2,
                                                            content=_rich("b")))

        items = arena_provider.fetch("c")

        assert [i.guid for i in items] == ["1", "2"]


# -- robustness ---------------------------------------------------------------


class TestNeverBlank:
    def test_an_unknown_future_block_type_still_renders(self, stub_get):
        # The whole class of bug: a type we've never seen must degrade to a
        # readable card, not an empty one.
        stub_get(_block("Hologram", title="Some new thing",
                        description=_rich("what it is")))

        (item,) = arena_provider.fetch("c")

        assert _renderable(item)
        assert "what it is" in item.content_html

    def test_a_block_with_no_content_at_all_gets_a_fallback_body(self, stub_get):
        stub_get(_block("Hologram", id=999))

        (item,) = arena_provider.fetch("c")

        assert _renderable(item)
        assert "https://www.are.na/block/999" in item.content_html

    def test_a_titleless_imageless_text_block_is_dropped_rather_than_blank(
        self, stub_get,
    ):
        stub_get(_block("Text", content={"markdown": "", "html": "", "plain": ""}),
                 _block("Text", id=2, content=_rich("real")))

        items = arena_provider.fetch("c")

        assert all(_renderable(i) or i.title for i in items)

    def test_a_malformed_block_does_not_kill_the_whole_poll(self, stub_get):
        stub_get({"id": 5, "type": "Image", "image": "not-a-dict"},
                 _block("Text", id=6, content=_rich("survivor")))

        items = arena_provider.fetch("c")

        assert "6" in [i.guid for i in items]

    def test_blocks_without_an_id_are_skipped(self, stub_get):
        stub_get(_block("Text", id=None, content=_rich("orphan")),
                 _block("Text", id=7, content=_rich("keeper")))

        items = arena_provider.fetch("c")

        assert [i.guid for i in items] == ["7"]
