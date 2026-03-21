"""Tests for Miniflux-based feed page generation."""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.feeds import (
    FeedItem,
    _build_feed_page_html,
    _build_filter_css,
    _build_status_text,
    _escape,
    _extract_image_from_enclosures,
    _format_excerpt,
    _map_entries,
    _parse_image_urls,
    _sanitize_html,
    _truncate,
    fetch_miniflux_entries,
    regenerate_feed_page,
    regenerate_feed_pages,
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

    def test_no_enclosures(self):
        entries = [{
            "id": 44,
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


# ============================================================================
# HTML generation (preserved logic)
# ============================================================================


class TestEscape:
    def test_none(self):
        assert _escape(None) == ""

    def test_special_chars(self):
        assert "&amp;" in _escape("a & b")
        assert "&lt;" in _escape("<tag>")


class TestTruncate:
    def test_empty(self):
        assert _truncate("") == ""
        assert _truncate(None) == ""

    def test_short_text(self):
        assert _truncate("hello") == "hello"

    def test_long_text(self):
        result = _truncate("a " * 200, max_len=10)
        assert len(result) < 15
        assert result.endswith("\u2026")


class TestSanitizeHtml:
    def test_empty(self):
        assert _sanitize_html("") == ""

    def test_allowed_tags(self):
        result = _sanitize_html("<p>Hello <b>world</b></p>")
        assert "<p>" in result
        assert "<b>" in result

    def test_strips_script(self):
        result = _sanitize_html("<script>alert(1)</script>Hello")
        assert "<script>" not in result
        assert "Hello" in result


class TestParseImageUrls:
    def test_none(self):
        assert _parse_image_urls(None) == []

    def test_single_url(self):
        assert _parse_image_urls("https://img.example.com/a.jpg") == ["https://img.example.com/a.jpg"]

    def test_json_array(self):
        import json
        urls = ["https://a.jpg", "https://b.jpg"]
        assert _parse_image_urls(json.dumps(urls)) == urls

    def test_invalid_json(self):
        assert _parse_image_urls("[invalid") == ["[invalid"]


class TestFormatExcerpt:
    def test_html_content(self):
        item = FeedItem(0, "", "", "", None, None, None, "<p>Hello</p>", None, None, None, None)
        result = _format_excerpt(item)
        assert "<p>" in result

    def test_text_content(self):
        item = FeedItem(0, "", "", "", None, None, "Line1\nLine2", None, None, None, None, None)
        result = _format_excerpt(item)
        assert "<br>" in result

    def test_empty(self):
        item = FeedItem(0, "", "", "", None, None, None, None, None, None, None, None)
        assert _format_excerpt(item) == ""


class TestBuildStatusText:
    def test_basic(self):
        result = _build_status_text("Jan 15, 12:00", 0, 42)
        assert "42 items" in result
        assert "Jan 15" in result

    def test_with_new(self):
        result = _build_status_text("", 5, 100)
        assert "+5 new" in result


class TestBuildFeedPageHtml:
    def test_produces_valid_html(self):
        items = [
            FeedItem(1, "u", "Blog", "1", "Title", "https://example.com", None, "<p>Body</p>", None, "Author", "2025-01-15T12:00:00Z", "2025-01-15T13:00:00Z"),
        ]
        html = _build_feed_page_html(items, ["Blog"])
        assert "<!doctype html>" in html
        assert "Title" in html
        assert "Blog" in html

    def test_image_card(self):
        items = [
            FeedItem(1, "u", "Photos", "1", "Photo", "https://example.com", None, None, "https://img.example.com/a.jpg", None, "2025-01-15T12:00:00Z", None),
        ]
        html = _build_feed_page_html(items, ["Photos"])
        assert "card-image" in html
        assert "img.example.com/a.jpg" in html

    def test_empty_items(self):
        html = _build_feed_page_html([], [])
        assert "<!doctype html>" in html


class TestBuildFilterCss:
    def test_type_toggles(self):
        css = _build_filter_css(["Blog", "News"])
        assert "image" in css
        assert "text" in css


# ============================================================================
# Page regeneration (mocked API)
# ============================================================================


@dataclass
class _MockConfig:
    """Minimal config stub for regeneration tests."""
    site: MagicMock = None
    nextcloud_mount_path: Path = None
    bot_dir_name: str = "istota"
    users: dict = field(default_factory=dict)
    use_mount: bool = True

    def __post_init__(self):
        if self.site is None:
            self.site = MagicMock(enabled=True)

    def get_user(self, user_id):
        return self.users.get(user_id)


class TestRegenerateFeedPage:
    def test_disabled_site(self, tmp_path):
        config = _MockConfig(site=MagicMock(enabled=False), nextcloud_mount_path=tmp_path)
        assert regenerate_feed_page(config, "stefan", "https://flux.test", "key") is False

    def test_no_mount(self):
        config = _MockConfig(nextcloud_mount_path=None)
        assert regenerate_feed_page(config, "stefan", "https://flux.test", "key") is False

    @patch("istota.feeds.fetch_miniflux_entries")
    def test_generates_page(self, mock_fetch, tmp_path):
        mock_fetch.return_value = [{
            "id": 1,
            "title": "Test Entry",
            "url": "https://example.com/1",
            "content": "<p>Hello</p>",
            "author": "Alice",
            "published_at": "2025-01-15T12:00:00Z",
            "created_at": "2025-01-15T13:00:00Z",
            "feed": {"title": "Example Blog"},
            "enclosures": None,
        }]

        config = _MockConfig(nextcloud_mount_path=tmp_path)
        result = regenerate_feed_page(config, "stefan", "https://flux.test", "key")
        assert result is True

        output_path = tmp_path / "Users" / "stefan" / "istota" / "html" / "feeds" / "index.html"
        assert output_path.exists()
        content = output_path.read_text()
        assert "Test Entry" in content
        assert "Example Blog" in content

    @patch("istota.feeds.fetch_miniflux_entries")
    def test_empty_entries(self, mock_fetch, tmp_path):
        mock_fetch.return_value = []
        config = _MockConfig(nextcloud_mount_path=tmp_path)
        assert regenerate_feed_page(config, "stefan", "https://flux.test", "key") is False

    @patch("istota.feeds.fetch_miniflux_entries")
    def test_api_error(self, mock_fetch, tmp_path):
        mock_fetch.side_effect = Exception("connection refused")
        config = _MockConfig(nextcloud_mount_path=tmp_path)
        assert regenerate_feed_page(config, "stefan", "https://flux.test", "key") is False


class TestRegenerateFeedPages:
    def test_disabled_site(self):
        config = _MockConfig(site=MagicMock(enabled=False))
        assert regenerate_feed_pages(config) == 0

    def test_no_mount(self):
        config = _MockConfig(use_mount=False)
        assert regenerate_feed_pages(config) == 0
