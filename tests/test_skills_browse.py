"""Tests for the browse skill CLI client."""

import json
from unittest.mock import MagicMock, patch

import pytest

from istota.skills.browse import (
    _links_from_extract,
    build_parser,
    cmd_close,
    cmd_extract,
    cmd_get,
    cmd_interact,
    cmd_links,
    cmd_screenshot,
    get_api_url,
    main,
)


class TestGetApiUrl:
    def test_default(self):
        with patch.dict("os.environ", {}, clear=True):
            assert get_api_url() == "http://localhost:9223"

    def test_from_env(self):
        with patch.dict("os.environ", {"BROWSER_API_URL": "http://custom:1234"}):
            assert get_api_url() == "http://custom:1234"


class TestBuildParser:
    def test_get_command(self):
        parser = build_parser()
        args = parser.parse_args(["get", "https://example.com"])
        assert args.command == "get"
        assert args.url == "https://example.com"
        assert args.keep_session is False
        assert args.timeout == 30

    def test_get_with_options(self):
        parser = build_parser()
        args = parser.parse_args([
            "get", "https://example.com",
            "--keep-session", "--timeout", "60", "--wait-for", "article",
        ])
        assert args.keep_session is True
        assert args.timeout == 60
        assert args.wait_for == "article"
        assert args.skip_behavior is False

    def test_get_with_skip_behavior(self):
        parser = build_parser()
        args = parser.parse_args(["get", "https://example.com", "--skip-behavior"])
        assert args.skip_behavior is True

    def test_get_with_session(self):
        parser = build_parser()
        args = parser.parse_args(["get", "https://example.com", "--session", "abc123"])
        assert args.session == "abc123"

    def test_screenshot_with_url(self):
        parser = build_parser()
        args = parser.parse_args(["screenshot", "https://example.com", "-o", "/tmp/out.png"])
        assert args.command == "screenshot"
        assert args.url == "https://example.com"
        assert args.output == "/tmp/out.png"

    def test_screenshot_with_session(self):
        parser = build_parser()
        args = parser.parse_args(["screenshot", "--session", "abc123"])
        assert args.session == "abc123"
        assert args.url is None

    def test_extract_command(self):
        parser = build_parser()
        args = parser.parse_args(["extract", "https://example.com", "-s", "article"])
        assert args.command == "extract"
        assert args.selector == "article"

    def test_interact_click(self):
        parser = build_parser()
        args = parser.parse_args(["interact", "sess1", "--click", ".btn", "--click", "#submit"])
        assert args.command == "interact"
        assert args.session_id == "sess1"
        assert args.click == [".btn", "#submit"]

    def test_interact_fill(self):
        parser = build_parser()
        args = parser.parse_args(["interact", "sess1", "--fill", "#name=Alice"])
        assert args.fill == ["#name=Alice"]

    def test_interact_scroll(self):
        parser = build_parser()
        args = parser.parse_args(["interact", "sess1", "--scroll", "down", "--scroll-amount", "1000"])
        assert args.scroll == "down"
        assert args.scroll_amount == 1000

    def test_close_command(self):
        parser = build_parser()
        args = parser.parse_args(["close", "sess1"])
        assert args.command == "close"
        assert args.session_id == "sess1"

    def test_links_command(self):
        parser = build_parser()
        args = parser.parse_args(["links", "https://example.com"])
        assert args.command == "links"
        assert args.url == "https://example.com"
        assert args.selector is None
        assert args.session is None
        assert args.timeout == 30

    def test_links_with_selector(self):
        parser = build_parser()
        args = parser.parse_args(["links", "https://example.com", "-s", "nav a"])
        assert args.selector == "nav a"

    def test_links_with_session(self):
        parser = build_parser()
        args = parser.parse_args(["links", "--session", "abc123", "-s", ".links"])
        assert args.session == "abc123"
        assert args.selector == ".links"
        assert args.url is None


class TestCmdGet:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_basic_get(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "title": "Example",
            "text": "Hello world",
            "url": "https://example.com",
            "links": [],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["get", "https://example.com"])
        result = cmd_get(args)

        assert result["status"] == "ok"
        assert result["title"] == "Example"
        mock_post.assert_called_once()
        call_args = mock_post.call_args
        assert call_args[1]["json"]["url"] == "https://example.com"
        assert call_args[1]["json"]["keep_session"] is False

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_get_with_session(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "session_id": "abc123"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["get", "https://example.com", "--session", "abc123"])
        cmd_get(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["session_id"] == "abc123"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_get_with_skip_behavior(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["get", "https://example.com", "--skip-behavior"])
        cmd_get(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["skip_behavior"] is True

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_get_without_skip_behavior(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["get", "https://example.com"])
        cmd_get(args)

        payload = mock_post.call_args[1]["json"]
        assert "skip_behavior" not in payload

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_captcha_response(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "captcha",
            "session_id": "xyz789",
            "vnc_url": "https://vnc.example.com",
            "message": "Captcha detected.",
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["get", "https://protected.com", "--keep-session"])
        result = cmd_get(args)

        assert result["status"] == "captcha"
        assert result["session_id"] == "xyz789"
        assert result["vnc_url"] == "https://vnc.example.com"


class TestCmdScreenshot:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_screenshot_saves_file(self, mock_url, mock_post, tmp_path):
        mock_resp = MagicMock()
        mock_resp.headers = {"content-type": "image/png"}
        mock_resp.content = b"\x89PNG fake image data"
        mock_post.return_value = mock_resp

        output = str(tmp_path / "shot.png")
        parser = build_parser()
        args = parser.parse_args(["screenshot", "https://example.com", "-o", output])
        result = cmd_screenshot(args)

        assert result["status"] == "ok"
        assert result["path"] == output
        assert (tmp_path / "shot.png").read_bytes() == b"\x89PNG fake image data"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_screenshot_error(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.headers = {"content-type": "application/json"}
        mock_resp.json.return_value = {"status": "error", "error": "timeout"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["screenshot", "https://example.com"])
        result = cmd_screenshot(args)

        assert result["status"] == "error"


class TestCmdExtract:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_extract(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "url": "https://example.com",
            "selector": "article",
            "count": 1,
            "elements": [{"text": "Article content", "html": "<p>Article content</p>"}],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["extract", "https://example.com", "-s", "article"])
        result = cmd_extract(args)

        assert result["status"] == "ok"
        assert result["count"] == 1
        payload = mock_post.call_args[1]["json"]
        assert payload["selector"] == "article"


class TestCmdInteract:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_click_actions(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "session_id": "sess1",
            "actions": [{"action": "click", "ok": True}],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["interact", "sess1", "--click", ".btn"])
        result = cmd_interact(args)

        assert result["status"] == "ok"
        payload = mock_post.call_args[1]["json"]
        assert payload["session_id"] == "sess1"
        assert payload["actions"] == [{"type": "click", "selector": ".btn"}]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_fill_actions(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "session_id": "sess1", "actions": []}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["interact", "sess1", "--fill", "#email=test@example.com"])
        cmd_interact(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["actions"] == [{"type": "fill", "selector": "#email", "value": "test@example.com"}]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_scroll_action(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "session_id": "sess1", "actions": []}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["interact", "sess1", "--scroll", "down", "--scroll-amount", "1000"])
        cmd_interact(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["actions"] == [{"type": "scroll", "direction": "down", "amount": 1000}]


class TestCmdClose:
    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_close(self, mock_url, mock_delete):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "closed", "session_id": "sess1"}
        mock_delete.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["close", "sess1"])
        result = cmd_close(args)

        assert result["status"] == "closed"
        mock_delete.assert_called_once_with(
            "http://test:9223/sessions/sess1", timeout=30.0
        )


class TestMain:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_main_outputs_json(self, mock_url, mock_post, capsys):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "title": "Test"}
        mock_post.return_value = mock_resp

        main(["get", "https://example.com"])

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "ok"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_main_connection_error(self, mock_url, mock_post, capsys):
        import httpx
        mock_post.side_effect = httpx.ConnectError("Connection refused")

        with pytest.raises(SystemExit) as exc_info:
            main(["get", "https://example.com"])
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "error"
        assert "Cannot connect" in output["error"]


class TestCmdLinks:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_links_basic(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "title": "Hub Page",
            "url": "https://news.example.com",
            "text": "Lots of text...",
            "links": [
                {"text": "Article One", "href": "/article/one"},
                {"text": "Article Two", "href": "/article/two"},
            ],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["links", "https://news.example.com"])
        result = cmd_links(args)

        assert result["status"] == "ok"
        assert result["url"] == "https://news.example.com"
        assert result["count"] == 2
        assert result["links"] == [
            {"text": "Article One", "href": "/article/one"},
            {"text": "Article Two", "href": "/article/two"},
        ]
        # Should not contain text field
        assert "text" not in result or result.get("text") is None
        assert "title" not in result

    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_links_with_selector_href_attr(self, mock_url, mock_post, mock_delete):
        """When extract returns href on elements directly (Guardian-style)."""
        browse_resp = MagicMock()
        browse_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com",
            "session_id": "sess1",
            "text": "...",
            "links": [],
        }
        extract_resp = MagicMock()
        extract_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com",
            "selector": "a[data-link-name='article']",
            "count": 2,
            "elements": [
                {
                    "text": "Russia can keep fighting",
                    "html": '<span class="dcr-n509ks">Russia can keep fighting</span>',
                    "href": "/world/2026/feb/24/russia-fighting",
                },
                {
                    "text": "Louvre president resigns",
                    "html": '<span>Louvre president resigns</span>',
                    "href": "/world/2026/feb/24/louvre-president",
                },
            ],
        }
        mock_post.side_effect = [browse_resp, extract_resp]
        mock_delete_resp = MagicMock()
        mock_delete_resp.json.return_value = {"status": "closed"}
        mock_delete.return_value = mock_delete_resp

        parser = build_parser()
        args = parser.parse_args(["links", "https://news.example.com", "-s", "a[data-link-name='article']"])
        result = cmd_links(args)

        assert result["status"] == "ok"
        assert result["count"] == 2
        assert result["links"] == [
            {"text": "Russia can keep fighting", "href": "/world/2026/feb/24/russia-fighting"},
            {"text": "Louvre president resigns", "href": "/world/2026/feb/24/louvre-president"},
        ]
        mock_delete.assert_called_once()

    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_links_with_selector_nested_anchors(self, mock_url, mock_post, mock_delete):
        """When extract returns elements containing nested <a> tags (fallback)."""
        browse_resp = MagicMock()
        browse_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com",
            "session_id": "sess1",
            "text": "...",
            "links": [],
        }
        extract_resp = MagicMock()
        extract_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com",
            "selector": "nav",
            "count": 1,
            "elements": [
                {
                    "text": "World News Sports",
                    "html": '<a href="/world" class="nav-link">World News</a> <a href="/sports"><span>Sports</span></a>',
                },
            ],
        }
        mock_post.side_effect = [browse_resp, extract_resp]
        mock_delete_resp = MagicMock()
        mock_delete_resp.json.return_value = {"status": "closed"}
        mock_delete.return_value = mock_delete_resp

        parser = build_parser()
        args = parser.parse_args(["links", "https://news.example.com", "-s", "nav"])
        result = cmd_links(args)

        assert result["status"] == "ok"
        assert result["count"] == 2
        assert result["links"] == [
            {"text": "World News", "href": "/world"},
            {"text": "Sports", "href": "/sports"},
        ]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_links_with_session_and_selector(self, mock_url, mock_post):
        """Session + selector uses extract with href attribute."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com",
            "selector": ".headlines a",
            "count": 1,
            "elements": [
                {
                    "text": "Breaking News",
                    "html": '<span>Breaking News</span>',
                    "href": "/breaking/123",
                },
            ],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["links", "--session", "sess1", "-s", ".headlines a"])
        result = cmd_links(args)

        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["links"] == [{"text": "Breaking News", "href": "/breaking/123"}]
        payload = mock_post.call_args[1]["json"]
        assert payload["session_id"] == "sess1"
        assert payload["selector"] == ".headlines a"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_links_empty(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "title": "Empty Page",
            "url": "https://example.com",
            "text": "No links here",
            "links": [],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["links", "https://example.com"])
        result = cmd_links(args)

        assert result["status"] == "ok"
        assert result["count"] == 0
        assert result["links"] == []

    def test_links_from_extract_prefers_href_attr(self):
        """_links_from_extract uses href attr when present."""
        data = {
            "elements": [
                {"text": "Article A", "html": "<span>Article A</span>", "href": "/a"},
                {"text": "Article B", "html": "<span>Article B</span>", "href": "/b"},
            ]
        }
        links = _links_from_extract(data)
        assert links == [
            {"text": "Article A", "href": "/a"},
            {"text": "Article B", "href": "/b"},
        ]

    def test_links_from_extract_falls_back_to_inner_html(self):
        """_links_from_extract parses <a> tags when no href attr."""
        data = {
            "elements": [
                {
                    "text": "Nav section",
                    "html": '<a href="/x">Link X</a> and <a href="/y"><b>Link Y</b></a>',
                },
            ]
        }
        links = _links_from_extract(data)
        assert links == [
            {"text": "Link X", "href": "/x"},
            {"text": "Link Y", "href": "/y"},
        ]

    def test_links_from_extract_mixed(self):
        """Mix of elements with and without href attr."""
        data = {
            "elements": [
                {"text": "Direct link", "html": "<span>Direct</span>", "href": "/direct"},
                {"text": "Container", "html": '<a href="/nested">Nested</a>'},
            ]
        }
        links = _links_from_extract(data)
        assert len(links) == 2
        assert links[0] == {"text": "Direct link", "href": "/direct"}
        assert links[1] == {"text": "Nested", "href": "/nested"}

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_links_error_passthrough(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "error",
            "error": "timeout",
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["links", "https://example.com"])
        result = cmd_links(args)

        assert result["status"] == "error"
        assert result["error"] == "timeout"
