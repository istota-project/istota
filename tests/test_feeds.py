"""Tests for Miniflux API client (istota.feeds)."""

from istota.feeds import (
    FeedItem,
    _extract_image_from_enclosures,
    _extract_image_from_content,
    _map_entries,
)


# ============================================================================
# Miniflux entry mapping
# ============================================================================


class TestMapEntries:
    def test_basic_entry(self):
        entries = [{
            "id": 42,
            "title": "Test Article",
            "url": "https://example.com/article",
            "content": "<p>Hello</p>",
            "author": "Alice",
            "published_at": "2025-01-15T12:00:00Z",
            "created_at": "2025-01-15T13:00:00Z",
            "feed": {"title": "Example Blog"},
            "enclosures": None,
        }]
        items = _map_entries(entries, "stefan")
        assert len(items) == 1
        assert items[0].id == 42
        assert items[0].feed_name == "Example Blog"
        assert items[0].title == "Test Article"
        assert items[0].content_html == "<p>Hello</p>"
        assert items[0].user_id == "stefan"

    def test_image_from_enclosure(self):
        entries = [{
            "id": 43,
            "title": "Photo Post",
            "url": "https://example.com/photo",
            "content": "",
            "author": "",
            "published_at": "2025-01-15T12:00:00Z",
            "created_at": "2025-01-15T13:00:00Z",
            "feed": {"title": "Photos"},
            "enclosures": [
                {"mime_type": "image/jpeg", "url": "https://example.com/img.jpg"},
            ],
        }]
        items = _map_entries(entries, "stefan")
        assert items[0].image_url == "https://example.com/img.jpg"

    def test_image_from_content_fallback(self):
        entries = [{
            "id": 44,
            "title": "Inline Image",
            "url": "https://example.com/post",
            "content": '<p>Text</p><img src="https://example.com/inline.jpg">',
            "author": "",
            "published_at": "2025-01-15T12:00:00Z",
            "created_at": "2025-01-15T13:00:00Z",
            "feed": {"title": "Blog"},
            "enclosures": [],
        }]
        items = _map_entries(entries, "stefan")
        assert items[0].image_url == "https://example.com/inline.jpg"

    def test_no_enclosures(self):
        entries = [{
            "id": 45,
            "title": "Text Only",
            "url": "https://example.com/text",
            "content": "Text",
            "author": "",
            "published_at": "2025-01-15T12:00:00Z",
            "created_at": "2025-01-15T13:00:00Z",
            "feed": {"title": "Blog"},
        }]
        items = _map_entries(entries, "stefan")
        assert items[0].image_url is None

    def test_empty_entries(self):
        assert _map_entries([], "stefan") == []


class TestExtractImageFromEnclosures:
    def test_none(self):
        assert _extract_image_from_enclosures(None) is None

    def test_empty_list(self):
        assert _extract_image_from_enclosures([]) is None

    def test_image_enclosure(self):
        encs = [{"mime_type": "image/png", "url": "https://img.example.com/pic.png"}]
        assert _extract_image_from_enclosures(encs) == "https://img.example.com/pic.png"

    def test_non_image_skipped(self):
        encs = [
            {"mime_type": "audio/mpeg", "url": "https://example.com/audio.mp3"},
            {"mime_type": "image/jpeg", "url": "https://example.com/img.jpg"},
        ]
        assert _extract_image_from_enclosures(encs) == "https://example.com/img.jpg"


class TestExtractImageFromContent:
    def test_none(self):
        assert _extract_image_from_content(None) is None

    def test_empty(self):
        assert _extract_image_from_content("") is None

    def test_img_tag(self):
        assert _extract_image_from_content('<img src="https://example.com/a.jpg">') == "https://example.com/a.jpg"

    def test_no_img(self):
        assert _extract_image_from_content("<p>No images here</p>") is None
