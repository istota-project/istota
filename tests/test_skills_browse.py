"""Tests for the browse skill CLI client."""

import argparse
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import httpx
import pytest

from istota.skills.browse import (
    ACTION_EMITTERS,
    ACTION_ORDER_DEST,
    IMAGE_SPACE_ACTIONS,
    KEY_PRESSES_DEFAULT,
    SCRATCH_NOTE,
    VISUAL_ACTION_TYPES,
    WHEEL_CLICKS_DEFAULT,
    OrderedAppend,
    OrderedFlag,
    _foreground_note_from_response,
    _interact_actions,
    _links_from_extract,
    _note_unreported_actions,
    build_parser,
    cmd_challenge,
    cmd_close,
    cmd_extract,
    cmd_get,
    cmd_interact,
    cmd_links,
    cmd_render,
    cmd_screenshot,
    delivered_size,
    get_api_url,
    main,
)

#: A body that `image_sniff.sniff_raster` admits. `screenshot` refuses to write
#: anything else now, so a `b"fake image data"` stand-in is no longer a
#: screenshot as far as the verb is concerned.
PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"fake image data"


@pytest.fixture
def deferred_dir(tmp_path, monkeypatch):
    """The task's own temp directory, as `build_task_runtime` exports it.

    `ISTOTA_DEFERRED_DIR` is `{temp_dir}/{user_id}`: the first root of the
    write allowlist, and since the derived default moved off the workspace it
    is also where a capture taken with no `-o` goes. Returns the directory.
    """
    own = tmp_path / "temp" / "alice"
    own.mkdir(parents=True)
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(own))
    return own


@pytest.fixture
def workspace(tmp_path, monkeypatch, deferred_dir):
    """A mount with the calling user's workspace, as the executor builds it.

    `screenshot` writes through `skill_host_paths`, whose roots come out of the
    environment, so a test that captures anything has to say who is calling and
    where their workspace is. Returns `{mount}/Users/alice`.

    It takes `deferred_dir` because the two roots arrive together in a real
    task and because a capture with no `-o` now needs the second one. Every
    `-o` case here still names a path under the returned workspace, so the
    extra root widens no assertion in this file.
    """
    mount = tmp_path / "mount"
    own = mount / "Users" / "alice"
    own.mkdir(parents=True)
    monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)
    return own


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
        assert args.max_chars is None
        assert args.limit is None

    def test_extract_with_budgets(self):
        parser = build_parser()
        args = parser.parse_args([
            "extract", "https://example.com", "-s", "article",
            "--max-chars", "80000", "--limit", "50",
        ])
        assert args.max_chars == 80000
        assert args.limit == 50

    def test_render_command(self):
        parser = build_parser()
        args = parser.parse_args(["render", "https://example.com"])
        assert args.command == "render"
        assert args.url == "https://example.com"
        assert args.mode == "full"
        assert args.keep_session is False
        assert args.timeout == 30
        assert args.max_chars is None

    def test_render_with_options(self):
        parser = build_parser()
        args = parser.parse_args([
            "render", "https://example.com", "--mode", "article",
            "--keep-session", "--max-chars", "250000", "--wait-for", "main",
        ])
        assert args.mode == "article"
        assert args.keep_session is True
        assert args.max_chars == 250000
        assert args.wait_for == "main"

    def test_render_session_only(self):
        parser = build_parser()
        args = parser.parse_args(["render", "--session", "abc123"])
        assert args.url is None
        assert args.session == "abc123"

    def test_include_frames_is_opt_in(self):
        parser = build_parser()
        assert parser.parse_args(["render", "https://example.com"]).include_frames is False
        args = parser.parse_args(["render", "https://example.com", "--include-frames"])
        assert args.include_frames is True

    def test_render_rejects_unknown_mode(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["render", "https://example.com", "--mode", "readable"])

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
        args = parser.parse_args([
            "interact", "sess1", "--scroll", "down", "--scroll-clicks", "4",
        ])
        assert args.scroll == "down"
        assert args.scroll_clicks == 4

    def test_interact_scroll_at(self):
        parser = build_parser()
        args = parser.parse_args([
            "interact", "sess1", "--scroll", "up", "--scroll-at", "412,318",
        ])
        assert args.scroll_at == ["412,318"]
        assert args.scroll == "up"

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


class TestCmdRender:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_render_returns_markdown(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com/world",
            "title": "World",
            "mode": "full",
            "requested_mode": "full",
            "markdown": "## Top stories\n\n* [Headline](https://news.example.com/a)",
            "chars": 54,
            "truncated": False,
            "notes": [],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["render", "https://news.example.com/world"])
        result = cmd_render(args)

        assert result["status"] == "ok"
        assert "[Headline](https://news.example.com/a)" in result["markdown"]
        assert mock_post.call_args[0][0] == "http://test:9223/render"
        payload = mock_post.call_args[1]["json"]
        assert payload["url"] == "https://news.example.com/world"
        assert payload["mode"] == "full"
        assert payload["keep_session"] is False

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_article_mode_payload(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "markdown": "body", "mode": "article"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args([
            "render", "https://news.example.com/story", "--mode", "article",
            "--max-chars", "250000", "--wait-for", "main", "--skip-behavior",
        ])
        cmd_render(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["mode"] == "article"
        assert payload["max_chars"] == 250000
        assert payload["wait_for"] == "main"
        assert payload["skip_behavior"] is True

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_include_frames_reaches_the_payload(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "markdown": "body"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        cmd_render(parser.parse_args([
            "render", "https://news.example.com/world", "--include-frames",
        ]))
        assert mock_post.call_args[1]["json"]["include_frames"] is True

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_default_asks_for_no_frame_content(self, mock_url, mock_post):
        # The census is the container's own business and costs the caller
        # nothing; reading frame content is a round trip per frame, so the
        # flag has to be absent rather than false-by-default-and-sent.
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "markdown": "body"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        cmd_render(parser.parse_args(["render", "https://news.example.com/world"]))
        assert "include_frames" not in mock_post.call_args[1]["json"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_frame_census_is_passed_through_to_the_model(self, mock_url, mock_post):
        # The whole point of ISSUE-516 is that the caller can tell a short page
        # from a page whose content is in a frame, so the count has to survive
        # the CLI rather than being dropped on the way past.
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "markdown": "## Top stories",
            "frames": {"found": 3, "included": 0, "capped": False},
            "notes": ["3 iframes on this page were dropped"],
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        result = cmd_render(parser.parse_args(["render", "https://news.example.com/world"]))
        assert result["frames"] == {"found": 3, "included": 0, "capped": False}

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_render_within_a_session(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "markdown": "x", "session_id": "sess1"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["render", "--session", "sess1"])
        cmd_render(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["session_id"] == "sess1"
        assert "url" not in payload

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_render_without_url_or_session_errors_locally(self, mock_url, mock_post):
        parser = build_parser()
        args = parser.parse_args(["render"])
        result = cmd_render(args)

        assert result["status"] == "error"
        assert "URL or --session" in result["error"]
        mock_post.assert_not_called()

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_render_on_old_container_says_so(self, mock_url, mock_post):
        # Flask's route-miss 404 is HTML, so .json() raises.
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.side_effect = ValueError("not json")
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["render", "https://example.com"])
        result = cmd_render(args)

        assert result["status"] == "error"
        assert "browse get" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_render_expired_session_404_is_not_reported_as_missing_endpoint(
        self, mock_url, mock_post,
    ):
        # The endpoint's own 404 carries a JSON error body. Reporting it as
        # "no render endpoint" would send the agent back to `browse get`.
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {"error": "session sess1 not found or expired"}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["render", "--session", "sess1"])
        result = cmd_render(args)

        assert result["status"] == "error"
        assert "not found or expired" in result["error"]
        assert "browse get" not in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_render_captcha_passthrough(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "captcha",
            "session_id": "sess1",
            "vnc_url": "https://vnc.example.com",
        }
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args(["render", "https://protected.example"])
        result = cmd_render(args)

        assert result["status"] == "captcha"
        assert result["vnc_url"] == "https://vnc.example.com"


class TestCmdScreenshot:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_screenshot_saves_file(self, mock_url, mock_post, workspace):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.headers = {"content-type": "image/png"}
        mock_resp.content = PNG_BYTES
        mock_post.return_value = mock_resp

        output = str(workspace / "shot.png")
        parser = build_parser()
        args = parser.parse_args(["screenshot", "https://example.com", "-o", output])
        result = cmd_screenshot(args)

        assert result["status"] == "ok"
        assert result["path"] == output
        assert (workspace / "shot.png").read_bytes() == PNG_BYTES

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
        assert "max_chars" not in payload
        assert "limit" not in payload

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_extract_budgets_forwarded(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "count": 0, "elements": []}
        mock_post.return_value = mock_resp

        parser = build_parser()
        args = parser.parse_args([
            "extract", "https://example.com", "-s", "article",
            "--max-chars", "80000", "--limit", "50",
        ])
        cmd_extract(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["max_chars"] == 80000
        assert payload["limit"] == 50


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
        args = parser.parse_args(["interact", "sess1", "--scroll", "down", "--scroll-clicks", "2"])
        cmd_interact(args)

        payload = mock_post.call_args[1]["json"]
        assert payload["actions"] == [{"type": "scroll", "direction": "down", "presses": 2}]


def _interact_parser():
    """The `interact` subparser out of the real `build_parser()`."""
    for action in build_parser()._actions:
        choices = getattr(action, "choices", None)
        if isinstance(choices, dict) and "interact" in choices:
            return choices["interact"]
    raise AssertionError("build_parser() declares no interact subcommand")


def _interact_namespace(**values):
    """A hand-built `interact` namespace, the shape `cmd_interact` reads."""
    args = argparse.Namespace(
        session_id="s1",
        click=[],
        fill=[],
        fill_credential=[],
        scroll=None,
        scroll_at=[],
        scroll_clicks=None,
        scroll_zoom=False,
        scroll_amount=None,
    )
    for dest, value in values.items():
        setattr(args, dest, value)
    return args


class TestTheActionOrderRecord:
    """The replay `cmd_interact` runs, at the seam rather than through argv.

    The command-line cases live in `TestFillCredential`, which has the proxy
    the credential stamp needs. These are the paths argv cannot reach: a
    namespace somebody built by hand, and a dest the replay does not know.
    """

    def test_every_argument_declared_with_ordered_append_has_an_emitter(self):
        """The declarations and the replay table, read off the real parser.

        This is the drift that matters: a fourth `--hover` declared with
        `OrderedAppend` and not added to `ACTION_EMITTERS` records a position
        nothing can replay. Asserting `ORDERED_ACTION_DESTS ==
        tuple(ACTION_EMITTERS)` instead would be `x == x`, since one is
        derived from the other — it is the parser that can disagree with them.
        """
        interact = _interact_parser()
        declared = {
            action.dest
            for action in interact._actions
            if isinstance(action, OrderedAppend)
        }
        assert declared, "no argument is declared with OrderedAppend any more"
        assert declared <= set(ACTION_EMITTERS), (
            f"declared with OrderedAppend but not replayable: "
            f"{sorted(declared - set(ACTION_EMITTERS))}"
        )
        # And the table carries nothing the parser stopped declaring, which
        # would leave a dest in the fallback order that can never be populated.
        assert set(ACTION_EMITTERS) <= declared, (
            f"replayable but declared on no argument: "
            f"{sorted(set(ACTION_EMITTERS) - declared)}"
        )

    def test_a_namespace_with_no_order_record_keeps_the_old_shape(self):
        """Clicks, then literal fills, then credentials.

        Reachable only from a namespace built by hand: `OrderedAppend` is the
        argparse action, so it fires under a plain `parse_args` too and an
        argv caller always has a record. Clicks lead here because that is
        what such a caller saw before ISSUE-507, not because it is the right
        order for a login.
        """
        args = _interact_namespace(
            click=[".btn"], fill=["#email=me@example.com"],
        )
        assert _interact_actions(args) == [
            {"type": "click", "selector": ".btn"},
            {"type": "fill", "selector": "#email", "value": "me@example.com"},
        ]

    def test_a_flag_dest_holding_a_bare_true_is_named(self):
        """ISSUE-531: `list(True)` raised a bare TypeError, which reads as a
        browser failure rather than as a malformed call.

        Unreachable from argv -- `OrderedFlag` appends one marker per
        occurrence -- but a hand-built namespace naturally spells a valueless
        option as `True`, which is exactly what the fallback order below is
        for.
        """
        args = _interact_namespace(click_challenge=True)
        with pytest.raises(ValueError, match="must be a list of values"):
            _interact_actions(args)

    def test_an_order_record_naming_an_unknown_dest_is_refused(self):
        """A dest declared nowhere — the only drift the table above allows."""
        args = _interact_namespace()
        setattr(args, ACTION_ORDER_DEST, [("hover", 0)])
        with pytest.raises(ValueError, match="no interact action is defined for hover"):
            _interact_actions(args)

    def test_an_order_record_past_the_end_of_its_values_is_named(self):
        """Named rather than left to `IndexError`, which reads as a browser failure."""
        args = _interact_namespace(click=[".btn"])
        setattr(args, ACTION_ORDER_DEST, [("click", 5)])
        with pytest.raises(ValueError, match="the click order record"):
            _interact_actions(args)

    def test_an_order_record_shorter_than_its_values_is_refused(self):
        """Dropping the unnamed values silently is the reported failure again.

        A record naming the click but not the fills emits the click alone, so
        the form is submitted empty and every action reports `ok` — ISSUE-507
        reached from a hand-built namespace instead of from argv.
        """
        args = _interact_namespace(
            click=[".btn"], fill=["#a=1", "#b=2"],
        )
        setattr(args, ACTION_ORDER_DEST, [("click", 0)])
        with pytest.raises(ValueError, match="action order record"):
            _interact_actions(args)


class TestTheChallengeClick:
    """ISSUE-525: the action the visual path exists for, reachable from argv.

    The container has implemented `click_challenge` and `/challenge` since
    visual mode landed, and the skill declared no flag and no verb for either
    — so the one element the whole path was written to reach could only be
    pressed by reading it off a screenshot by eye and sending `--click-at`,
    which throws away the frame's measured box, the checkbox inset and the
    test that tells the widget from an invisible beacon.
    """

    def test_the_flag_emits_the_action(self):
        args = _interact_parser().parse_args(["s1", "--click-challenge"])
        assert _interact_actions(args) == [{"type": "click_challenge"}]

    def test_it_takes_no_coordinate(self):
        """The container measures the widget, so there is nothing to pass."""
        with pytest.raises(SystemExit):
            _interact_parser().parse_args(["s1", "--click-challenge", "40,50"])

    def test_it_keeps_the_position_it_was_written_in(self):
        """ISSUE-525's one open decision, settled as a position rather than
        inherited as "appends last".

        The flag is deliberately not last here: an implementation that made it
        a `store_true` and appended the action at the end would put the press
        after the type and pass a test that ended with it. Pressing a checkbox
        and then typing into the page behind it is the ordering ISSUE-507
        established is the caller's to choose.
        """
        args = _interact_parser().parse_args([
            "s1", "--fill", "#u=bob", "--click-challenge", "--type", "hi",
        ])
        assert _interact_actions(args) == [
            {"type": "fill", "selector": "#u", "value": "bob"},
            {"type": "click_challenge"},
            {"type": "type", "text": "hi"},
        ]

    def test_two_presses_are_two_actions(self):
        """The marker is appended per occurrence, so the order record's count
        still answers to the values parsed."""
        args = _interact_parser().parse_args([
            "s1", "--click-challenge", "--click-challenge",
        ])
        assert _interact_actions(args) == [{"type": "click_challenge"}] * 2
        assert len(getattr(args, ACTION_ORDER_DEST)) == 2

    def test_absent_it_emits_nothing(self):
        args = _interact_parser().parse_args(["s1", "--click", ".btn"])
        assert _interact_actions(args) == [{"type": "click", "selector": ".btn"}]

    def test_the_ordered_flag_refuses_to_carry_a_value(self):
        """A later argument declared `OrderedFlag` with an nargs would append
        the value itself as the marker, and the emitter ignores its argument —
        so the value would vanish with nothing saying so."""
        parser = argparse.ArgumentParser()
        with pytest.raises(ValueError, match="takes no value"):
            parser.add_argument("--thing", action=OrderedFlag, nargs=1)


class TestTheChallengeVerb:
    """A look before a press: `frames`, `checkbox_css` and `checkbox_screen`.

    It is the only way to tell "no challenge here" from "a challenge this
    cannot locate", which leave `--click-challenge` with nothing to press and
    want opposite things done about them.
    """

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_it_asks_the_endpoint_about_this_session(self, mock_url, mock_post):
        mock_post.return_value = _json_response({
            "status": "ok",
            "frames": [{"url": "https://challenges.cloudflare.com/x",
                        "x": 271.5, "y": 400.0, "width": 300, "height": 65}],
            "checkbox_css": [293.5, 432.5],
            "checkbox_screen": [293, 519],
        })

        result = _run_verb(["challenge", "sess1"])

        mock_post.assert_called_once()
        assert mock_post.call_args[0][0] == "http://test:9223/challenge"
        assert mock_post.call_args[1]["json"] == {"session_id": "sess1"}
        assert result["checkbox_screen"] == [293, 519]
        assert result["frames"][0]["height"] == 65

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_page_with_no_challenge_is_an_ok_answer(self, mock_url, mock_post):
        """Not an error: "there is nothing to press" is the thing being asked."""
        mock_post.return_value = _json_response({
            "status": "ok", "frames": [], "checkbox_css": None,
            "checkbox_screen": None,
        })

        result = _run_verb(["challenge", "sess1"])

        assert result["status"] == "ok"
        assert result["checkbox_css"] is None
        assert "notes" not in result

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_unknown_session_keeps_the_api_s_own_answer(self, mock_url, mock_post):
        """The API's 404 is JSON and carries the diagnosis, so it is not
        rewritten as a missing route."""
        resp = _json_response({"error": "session sess9 not found or expired"})
        resp.status_code = 404
        mock_post.return_value = resp

        result = _run_verb(["challenge", "sess9"])

        assert result["status"] == "error"
        assert "not found" in result["error"]
        assert "notes" not in result

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_container_without_the_route_is_named(self, mock_url, mock_post):
        """Flask's own 404 is HTML, which means there is no such route — on
        this endpoint, an image older than visual mode."""
        mock_post.return_value = _non_json_response(
            404,
            "<!doctype html>\n<title>404 Not Found</title>\n",
            url="http://test:9223/challenge",
        )

        result = _run_verb(["challenge", "sess1"])

        assert result["status"] == "error"
        assert any("predates visual mode" in n for n in result["notes"])

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_main_dispatches_it(self, mock_url, mock_post, capsys):
        """The verb is in `main`'s table and not only in the parser."""
        mock_post.return_value = _json_response({"status": "ok", "frames": []})

        main(["challenge", "sess1"])

        assert json.loads(capsys.readouterr().out)["status"] == "ok"


def _json_response(payload, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"content-type": "application/json"}
    resp.json.return_value = payload
    return resp


class TestAFillWithNoSeparator:
    """`--fill "#email"` — a selector with no value.

    It used to be dropped in silence while the clicks around it ran, which is
    the reported failure by a second route: submit fires against a form that
    was never filled and the envelope is `status: ok` throughout. The sibling
    `--fill-credential` has always refused the same shape before dispatch.
    """

    def test_it_is_refused_rather_than_skipped(self):
        args = _interact_namespace(fill=["#email"])
        with pytest.raises(ValueError, match="expected SELECTOR=VALUE"):
            _interact_actions(args)

    def test_the_click_beside_it_never_runs(self):
        args = _interact_namespace(
            fill=["#email"], click=["button[type=submit]"],
        )
        with pytest.raises(ValueError, match="expected SELECTOR=VALUE"):
            _interact_actions(args)

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_no_request_is_sent(self, mock_url, mock_post):
        """The refusal lands before the browser is asked to do anything."""
        parser = build_parser()
        args = parser.parse_args(
            ["interact", "s1", "--fill", "#email", "--click", "button[type=submit]"],
        )
        with pytest.raises(ValueError, match="expected SELECTOR=VALUE"):
            cmd_interact(args)
        mock_post.assert_not_called()

    def test_a_value_containing_a_separator_is_unaffected(self):
        """The control: an empty value is a value, and `=` may recur."""
        args = _interact_namespace(fill=["#email=", "#q=a=b"])
        assert _interact_actions(args) == [
            {"type": "fill", "selector": "#email", "value": ""},
            {"type": "fill", "selector": "#q", "value": "a=b"},
        ]


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


class TestLinksCarriesTheBudgetVerdict:
    """`links` reshapes the response, so it has to carry the clipping with it.

    The container says when the link budget bound (ISSUE-531), because a full
    array of navigation chrome reads exactly like a complete answer. This
    subcommand rebuilds the envelope from scratch — `status`, `url`, `count`,
    `links` — so a verdict it does not copy over is a verdict the model never
    sees, on the one subcommand whose entire subject is links.
    """

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_clipped_list_keeps_its_flag_through_the_reshape(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com/section/world",
            "text": "Headlines the links array cannot reach",
            "links": [{"text": f"Section {i}", "href": f"/section/{i}"} for i in range(100)],
            "links_truncated": True,
            "anchors_total": 357,
        }
        mock_post.return_value = mock_resp

        args = build_parser().parse_args(["links", "https://news.example.com/section/world"])
        result = cmd_links(args)

        assert result["links_truncated"] is True
        assert result["anchors_total"] == 357
        assert result["count"] == 100

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_limit_that_bound_travels_too(self, mock_url, mock_post):
        """Without it the verb advertises a retry it cannot perform.

        `--max-links` is this subcommand's own remedy for a clipped list, and
        on a page past the container's scan ceiling no value of it reaches
        further (ISSUE-533). The key saying which of the two bound is the
        only thing that separates them, so the reshape has to carry it.
        """
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "url": "https://directory.example.com/all",
            "links": [{"text": f"Entry {i}", "href": f"/e/{i}"} for i in range(2000)],
            "links_truncated": True,
            "links_truncated_by": "scan_ceiling",
            "anchors_total": 5400,
        }
        mock_post.return_value = mock_resp

        args = build_parser().parse_args(
            ["links", "https://directory.example.com/all", "--max-links", "5000"],
        )
        result = cmd_links(args)

        assert result["links_truncated_by"] == "scan_ceiling"
        assert result["anchors_total"] == 5400

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_budget_verdict_travels_as_readily_as_a_ceiling_one(
        self, mock_url, mock_post,
    ):
        """The control: the copy is not keyed on one particular value."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com/section/world",
            "links": [{"text": f"Section {i}", "href": f"/s/{i}"} for i in range(100)],
            "links_truncated": True,
            "links_truncated_by": "max_links",
            "anchors_total": 357,
        }
        mock_post.return_value = mock_resp

        result = cmd_links(
            build_parser().parse_args(["links", "https://news.example.com/section/world"]),
        )

        assert result["links_truncated_by"] == "max_links"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_complete_list_gains_no_flag(self, mock_url, mock_post):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "url": "https://news.example.com",
            "links": [{"text": "Article One", "href": "/article/one"}],
        }
        mock_post.return_value = mock_resp

        result = cmd_links(build_parser().parse_args(["links", "https://news.example.com"]))

        assert "links_truncated" not in result
        assert "links_truncated_by" not in result
        assert "anchors_total" not in result

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_budget_can_be_raised_from_this_subcommand(self, mock_url, mock_post):
        """The flag names a remedy, so the remedy has to be reachable here.

        Without this the model is told the list was clipped by a subcommand
        that gives it no way to ask for more, and its only route is to switch
        to `get` — which returns the page text as well.
        """
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "url": "https://n.example", "links": []}
        mock_post.return_value = mock_resp

        args = build_parser().parse_args(
            ["links", "https://n.example", "--max-links", "400"],
        )
        cmd_links(args)

        assert mock_post.call_args.kwargs["json"]["max_links"] == 400

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_unset_budget_is_not_sent(self, mock_url, mock_post):
        """The container owns the default; sending a null would override it."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"status": "ok", "url": "https://n.example", "links": []}
        mock_post.return_value = mock_resp

        cmd_links(build_parser().parse_args(["links", "https://n.example"]))

        assert "max_links" not in mock_post.call_args.kwargs["json"]


def _non_json_response(status_code, body, url="http://test:9223/browse"):
    """A response whose body is not JSON, the way httpx presents one.

    `resp.json()` raises `ValueError` (json.JSONDecodeError is a subclass) and
    `resp.text` still holds whatever came back.
    """
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = body
    resp.content = body.encode("utf-8") if isinstance(body, str) else body
    resp.encoding = "utf-8"
    resp.url = url
    resp.headers = {"content-type": "text/html"}
    resp.json.side_effect = json.JSONDecodeError("Expecting value", body or "", 0)
    return resp


FLASK_500 = (
    "<!doctype html>\n<html lang=en>\n<title>500 Internal Server Error</title>\n"
    "<h1>Internal Server Error</h1>\n<p>The server encountered an internal error "
    "and was unable to complete your request.</p>\n"
)


def _run_verb(argv):
    """Run a verb through the same command table `main` uses."""
    parser = build_parser()
    args = parser.parse_args(argv)
    commands = {
        "get": cmd_get,
        "render": cmd_render,
        "screenshot": cmd_screenshot,
        "extract": cmd_extract,
        "interact": cmd_interact,
        "links": cmd_links,
        "challenge": cmd_challenge,
        "close": cmd_close,
    }
    return commands[args.command](args)


class TestNonJsonResponsesAreReported:
    """ISSUE-383: a body that will not decode must name the status and the body.

    Before this, every verb called `resp.json()` with no check, the
    `json.JSONDecodeError` reached `main`'s catch-all, and the whole report was
    the string "Expecting value: line 1 column 1 (char 0)" — which names no
    status code, no URL and no part of the body, so a 500 from the container, a
    502 in front of it and an empty response were indistinguishable.
    """

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_flask_html_500_names_the_status_and_the_body(self, mock_url, mock_post):
        mock_post.return_value = _non_json_response(500, FLASK_500)

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert result["status"] == "error"
        assert "500" in result["error"]
        assert "Internal Server Error" in result["error"]
        assert "Expecting value" not in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_request_url_is_named(self, mock_url, mock_post):
        mock_post.return_value = _non_json_response(
            502,
            "<html><body>502 Bad Gateway</body></html>",
            url="http://test:9223/browse",
        )

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert "http://test:9223/browse" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_empty_body_says_so(self, mock_url, mock_post):
        mock_post.return_value = _non_json_response(502, "")

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert result["status"] == "error"
        assert "502" in result["error"]
        assert "empty" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_excerpt_is_capped_and_says_it_was(self, mock_url, mock_post):
        from istota.skills.browse import MAX_BODY_EXCERPT

        mock_post.return_value = _non_json_response(500, "A" * 10000)

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        # The marker is the point: a truncated body that drops it reads as a
        # complete one, and a length assertion alone cannot see that.
        excerpt = result["error"].split("not a JSON object: ", 1)[1]
        assert excerpt == "A" * MAX_BODY_EXCERPT + "…"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_unreadable_body_is_not_called_empty(self, mock_url, mock_post):
        # "empty response" and "40 KB of something unreadable" are different
        # outages; reporting the second as the first is a false statement.
        resp = MagicMock()
        resp.status_code = 500
        resp.url = "http://test:9223/browse"
        resp.json.side_effect = ValueError("no")
        type(resp).content = property(
            lambda self: (_ for _ in ()).throw(RuntimeError("stream consumed")),
        )
        mock_post.return_value = resp

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert result["status"] == "error"
        assert "500" in result["error"]
        assert "(empty)" not in result["error"]
        assert "unreadable" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_bogus_charset_still_produces_an_excerpt(self, mock_url, mock_post):
        resp = _non_json_response(500, "boom")
        resp.encoding = "utf-42"  # no such codec
        mock_post.return_value = resp

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert "boom" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_control_characters_do_not_survive_the_excerpt(self, mock_url, mock_post):
        # The body is whatever the container or an intermediary produced. An
        # ANSI escape reaching a terminal is why it is not passed through as-is.
        mock_post.return_value = _non_json_response(
            500, "boom \x1b[31mRED\x1b[0m \x00 done\r\nnext line",
        )

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert "\x1b" not in result["error"]
        assert "\x00" not in result["error"]
        assert "\n" not in result["error"]
        assert "RED" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_bidi_and_zero_width_characters_do_not_survive(self, mock_url, mock_post):
        # U+202E reverses the display order of everything after it, so a body
        # could otherwise render as a different message inside a line the
        # model reads as this tool's own diagnostic voice. json.dumps with
        # ensure_ascii=False emits these raw, so stripping is the only guard.
        hostile = (
            "start \u202eesrever\u202c \u200bzero \ufeffbom "
            "\u2066iso\u2069 end"
        )
        mock_post.return_value = _non_json_response(500, hostile)

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        for ch in "\u202e\u202c\u200b\ufeff\u2066\u2069":
            assert ch not in result["error"], hex(ord(ch))
        assert "esrever" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_line_separators_are_collapsed(self, mock_url, mock_post):
        # U+2028/U+2029 are handled by the whitespace collapse rather than by
        # the control class, which is why the class does not name them.
        mock_post.return_value = _non_json_response(500, "a\u2028b\u2029c")

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert "\u2028" not in result["error"]
        assert "\u2029" not in result["error"]
        assert "a b c" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_json_body_that_is_not_an_object_is_an_error_too(self, mock_url, mock_post):
        # Well-formed JSON, wrong shape. Every verb calls .get() on what comes
        # back, so a list reaching them is an AttributeError one frame later.
        resp = MagicMock()
        resp.status_code = 200
        resp.text = '["unexpected"]'
        resp.content = b'["unexpected"]'
        resp.encoding = "utf-8"
        resp.url = "http://test:9223/browse"
        resp.json.return_value = ["unexpected"]
        mock_post.return_value = resp

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert result["status"] == "error"
        assert "unexpected" in result["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_non_json_503_carries_the_retry_hint(self, mock_url, mock_post):
        # The message the unreachable `except httpx.HTTPStatusError` branch used
        # to hold. It fires here now, where it can actually be reached.
        mock_post.return_value = _non_json_response(503, "<html>503 Service Unavailable</html>")

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert "503" in result["error"]
        assert "retry" in result["error"].lower()

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_apis_own_json_error_body_still_passes_through(self, mock_url, mock_post):
        # The API reports its own failures as JSON with a non-2xx status, and
        # those bodies carry the diagnosis. A bare raise_for_status() would
        # throw them away, which is why the check is on the body, not the status.
        resp = MagicMock()
        resp.status_code = 503
        resp.json.return_value = {
            "status": "error",
            "error": "Chrome unavailable: CDP connect failed",
        }
        mock_post.return_value = resp

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert result == {
            "status": "error",
            "error": "Chrome unavailable: CDP connect failed",
        }

    @pytest.mark.parametrize(
        "argv",
        [
            ["get", "https://example.com"],
            ["render", "https://example.com"],
            ["extract", "https://example.com", "--selector", "article"],
            ["interact", "sess1", "--click", ".btn"],
            ["links", "https://example.com"],
            ["links", "https://example.com", "--selector", "nav a"],
            ["screenshot", "https://example.com"],
        ],
    )
    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_every_verb_reports_a_non_json_body(
        self, mock_url, mock_post, mock_delete, argv, workspace,
    ):
        # `workspace` is only load-bearing for the screenshot row: with no
        # workspace resolvable that verb refuses before it ever posts, so
        # without the fixture the case would assert against the wrong refusal.
        # The blast radius: the entry named one verb, the decode call was in all
        # of them.
        mock_post.return_value = _non_json_response(500, FLASK_500)

        result = _run_verb(argv)

        assert result["status"] == "error", argv
        assert "500" in result["error"], argv
        assert "Expecting value" not in result["error"], argv

    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_close_reports_a_non_json_body(self, mock_url, mock_delete):
        mock_delete.return_value = _non_json_response(
            500, FLASK_500, url="http://test:9223/sessions/sess1",
        )

        parser = build_parser()
        result = cmd_close(parser.parse_args(["close", "sess1"]))

        assert result["status"] == "error"
        assert "500" in result["error"]
        assert "Expecting value" not in result["error"]


class TestMainExitStatus:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_error_result_exits_one(self, mock_url, mock_post, capsys):
        # Without this the fix would report a 500 on exit 0, where the unhandled
        # decode error at least exited 1.
        mock_post.return_value = _non_json_response(500, FLASK_500)

        with pytest.raises(SystemExit) as exc:
            main(["get", "https://example.com"])
        assert exc.value.code == 1

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "error"
        assert "500" in output["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_api_reported_error_exits_one_too(self, mock_url, mock_post, capsys):
        resp = MagicMock()
        resp.json.return_value = {"status": "error", "error": "navigation timeout"}
        mock_post.return_value = resp

        with pytest.raises(SystemExit) as exc:
            main(["get", "https://example.com"])
        assert exc.value.code == 1

        assert json.loads(capsys.readouterr().out)["error"] == "navigation timeout"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_ok_result_exits_zero(self, mock_url, mock_post, capsys):
        resp = MagicMock()
        resp.json.return_value = {"status": "ok", "title": "Test"}
        mock_post.return_value = resp

        main(["get", "https://example.com"])

        assert json.loads(capsys.readouterr().out)["status"] == "ok"

    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_closed_session_is_not_an_error(self, mock_url, mock_delete, capsys):
        # Only "error" exits 1. A status naming any other outcome is an answer
        # rather than a failure, and a caller branching on it would read a
        # non-zero exit wrong. DELETE /sessions/<id> always answers "closed",
        # whether or not the session was there.
        resp = MagicMock()
        resp.json.return_value = {"status": "closed", "session_id": "sess1"}
        mock_delete.return_value = resp

        main(["close", "sess1"])

        assert json.loads(capsys.readouterr().out)["status"] == "closed"


class TestTheDeadHttpStatusErrorBranchIsGone:
    def test_nothing_raises_or_handles_an_http_status_error(self):
        # The `except httpx.HTTPStatusError` arm in `main` was unreachable
        # because `raise_for_status()` is called nowhere, so its 503 message had
        # never been printed. Dead code that reads as a working feature; the
        # message now lives on the reachable path in `_decode`.
        #
        # Read structurally rather than as text: the module's prose explains
        # why raise_for_status() is the wrong tool here, and a substring scan
        # would fail on the explanation.
        import ast

        import istota.skills.browse as browse

        from tests.support.drift import source_of

        tree = ast.parse(source_of(browse))

        handled = [
            ast.unparse(h.type)
            for h in ast.walk(tree)
            if isinstance(h, ast.ExceptHandler) and h.type is not None
        ]
        assert not any("HTTPStatusError" in name for name in handled), handled

        called = [
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        ]
        assert "raise_for_status" not in called


class TestBareErrorBodiesAreClassified:
    """The API has two spellings for a reported failure; both must classify.

    `browse_api.py` says `{"status": "error", ...}` for its 500s and 503s, but
    its argument and lookup failures — nine paths, two of them 500s — say a
    bare `{"error": ...}` with no `status` key. Every caller here branches on
    `status`, so before ISSUE-383 those read as successes: `main` exited 0 and
    `cmd_links` treated one as a page. `cmd_render` rewrote the shape by hand
    for its own 404, so two verbs disagreed about one server response.
    """

    @staticmethod
    def _bare_error(status_code, message):
        resp = MagicMock()
        resp.status_code = status_code
        resp.url = "http://test:9223/browse"
        resp.json.return_value = {"error": message}
        return resp

    @pytest.mark.parametrize(
        ("argv", "code"),
        [
            (["get", "https://example.com"], 400),
            (["get", "https://example.com", "--session", "gone"], 404),
            (["render", "https://example.com"], 400),
            (["extract", "https://example.com", "--selector", "article"], 400),
            (["interact", "sess1", "--click", ".btn"], 404),
            (["links", "https://example.com"], 400),
            (["links", "https://example.com", "--selector", "nav a"], 400),
        ],
    )
    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_every_verb_classifies_a_bare_error_body(
        self, mock_url, mock_post, mock_delete, argv, code,
    ):
        mock_post.return_value = self._bare_error(code, "url is required")

        result = _run_verb(argv)

        assert result["status"] == "error", argv
        assert result["error"] == "url is required", argv

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_get_and_render_agree_on_an_expired_session(self, mock_url, mock_post):
        # One condition, one server response, two verbs. These used to differ:
        # render rewrote the body and exited 1, get passed it through and
        # exited 0.
        body = "session sess1 not found or expired"
        mock_post.return_value = self._bare_error(404, body)

        parser = build_parser()
        got = cmd_get(parser.parse_args(["get", "https://example.com", "--session", "sess1"]))
        rendered = cmd_render(parser.parse_args(["render", "--session", "sess1"]))

        assert got["status"] == rendered["status"] == "error"
        assert got["error"] == rendered["error"] == body

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_bare_error_exits_one(self, mock_url, mock_post, capsys):
        mock_post.return_value = self._bare_error(400, "url is required")

        with pytest.raises(SystemExit) as exc:
            main(["get", "https://example.com"])
        assert exc.value.code == 1

        assert json.loads(capsys.readouterr().out)["error"] == "url is required"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_body_already_carrying_a_status_is_untouched(self, mock_url, mock_post):
        resp = MagicMock()
        resp.status_code = 404
        resp.json.return_value = {"status": "not_found"}
        mock_post.return_value = resp

        parser = build_parser()
        result = cmd_get(parser.parse_args(["get", "https://example.com"]))

        assert result == {"status": "not_found"}


class TestScreenshotDoesNotTrustTheContentType:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_non_200_labelled_as_an_image_is_not_saved(
        self, mock_url, mock_post, workspace,
    ):
        # An intermediary answering 502 while labelling it image/png used to
        # have its error page written to disk as a .png and reported ok.
        resp = MagicMock()
        resp.status_code = 502
        resp.headers = {"content-type": "image/png"}
        resp.content = b"<html>502 Bad Gateway</html>"
        mock_post.return_value = resp

        output = workspace / "shot.png"
        parser = build_parser()
        result = cmd_screenshot(
            parser.parse_args(["screenshot", "https://example.com", "-o", str(output)]),
        )

        assert result["status"] == "error"
        assert "502" in result["error"]
        assert not output.exists()

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_empty_image_body_is_not_a_screenshot(self, mock_url, mock_post, workspace):
        resp = MagicMock()
        resp.status_code = 200
        resp.headers = {"content-type": "image/png"}
        resp.content = b""
        mock_post.return_value = resp

        output = workspace / "shot.png"
        parser = build_parser()
        result = cmd_screenshot(
            parser.parse_args(["screenshot", "https://example.com", "-o", str(output)]),
        )

        assert result["status"] == "error"
        assert not output.exists()


class TestLinksCleansUpItsSession:
    @patch("istota.skills.browse.httpx.delete")
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_session_is_closed_when_the_extract_leg_fails(
        self, mock_url, mock_post, mock_delete,
    ):
        # The old code raised out of resp.json() on the second leg, skipping
        # the cleanup entirely and stranding one of only two browser tabs for
        # the full session TTL. Returning an error dict runs the delete.
        browse_resp = MagicMock()
        browse_resp.json.return_value = {
            "status": "ok", "url": "https://example.com", "session_id": "sess1", "links": [],
        }
        mock_post.side_effect = [browse_resp, _non_json_response(500, FLASK_500)]

        parser = build_parser()
        result = cmd_links(
            parser.parse_args(["links", "https://example.com", "--selector", "nav a"]),
        )

        mock_delete.assert_called_once()
        assert "sessions/sess1" in mock_delete.call_args[0][0]
        assert result["status"] == "error"
        assert "500" in result["error"]


class TestTheCatchAllNamesTheFailure:
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_read_timeout_is_not_reported_as_two_bare_words(
        self, mock_url, mock_post, capsys,
    ):
        # httpx's ReadTimeout stringifies to "timed out" and several of its
        # siblings to "", naming no verb, no URL and no class — the same
        # defect ISSUE-383 fixed one layer down.
        mock_post.side_effect = httpx.ReadTimeout("timed out")

        with pytest.raises(SystemExit) as exc:
            main(["get", "https://example.com"])
        assert exc.value.code == 1

        error = json.loads(capsys.readouterr().out)["error"]
        assert "ReadTimeout" in error
        assert "get" in error
        assert "http://test:9223" in error

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_exception_with_no_message_still_names_its_class(
        self, mock_url, mock_post, capsys,
    ):
        mock_post.side_effect = httpx.RemoteProtocolError("")

        with pytest.raises(SystemExit):
            main(["get", "https://example.com"])

        error = json.loads(capsys.readouterr().out)["error"]
        assert "RemoteProtocolError" in error
        assert not error.rstrip().endswith(":")


class TestFillCredential:
    """`interact --fill-credential`: the name in the argv, the value in the request.

    Driven through `main` against a **real** `SkillProxy` over a real socket,
    because the whole claim is about where the value comes from and where it
    goes. The browser API is the one thing mocked, since it is on the far side
    of the boundary this stage is about.
    """

    VAULT = {
        "acme_password": "browsevalue-pw-aaaaaaaa",
        "acme_token": "browsevalue-tok-bbbbbbbb",
    }

    @pytest.fixture
    def proxy(self, monkeypatch):
        import tempfile
        from pathlib import Path as _Path

        from istota.skill_proxy import SkillProxy

        directory = tempfile.mkdtemp(prefix="bz_", dir="/tmp")
        sock = _Path(directory) / "s.sock"
        monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", str(sock))
        started = []

        def start(**kwargs):
            kwargs.setdefault("vault_credentials", dict(self.VAULT))
            server = SkillProxy(
                sock, {}, {"PATH": "/usr/bin"}, task_id=3, **kwargs,
            )
            server.__enter__()
            started.append(server)
            return server

        yield start
        for server in started:
            server.__exit__(None, None, None)
        sock.unlink(missing_ok=True)
        _Path(directory).rmdir()

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_value_reaches_the_browser_and_the_name_does_not(
        self, mock_url, mock_post, proxy, capsys,
    ):
        proxy()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok", "session_id": "s1",
            "actions": [{"action": "fill", "selector": "#password", "ok": True}],
        }
        mock_post.return_value = mock_resp

        main(["interact", "s1", "--fill-credential", "#password=acme_password"])

        payload = mock_post.call_args[1]["json"]
        assert payload["actions"] == [
            {
                "type": "fill",
                "selector": "#password",
                "value": self.VAULT["acme_password"],
            },
        ]
        # The name is the model's own label and is not what the browser is
        # told; the request carries the value alone.
        assert "acme_password" not in json.dumps(payload)
        # And the result the model reads back carries neither.
        out = capsys.readouterr().out
        assert self.VAULT["acme_password"] not in out

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_fills_are_sent_in_command_line_order(
        self, mock_url, mock_post, proxy,
    ):
        proxy()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok", "session_id": "s1", "actions": [],
        }
        mock_post.return_value = mock_resp

        main([
            "interact", "s1",
            "--fill", "#email=me@example.com",
            "--fill-credential", "#password=acme_password",
            "--fill", "#note=hello",
            "--fill-credential", "#totp=acme_token",
        ])

        payload = mock_post.call_args[1]["json"]
        assert [a["selector"] for a in payload["actions"]] == [
            "#email", "#password", "#note", "#totp",
        ]
        assert [a["value"] for a in payload["actions"]] == [
            "me@example.com",
            self.VAULT["acme_password"],
            "hello",
            self.VAULT["acme_token"],
        ]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_click_runs_where_it_was_written(self, mock_url, mock_post, proxy):
        """A login written as one call fills the form and then submits it.

        Clicks used to be emitted ahead of every fill whatever the caller
        wrote, so this argv clicked submit against an empty form and reported
        `ok` for all three actions (ISSUE-507). The whole array is asserted
        rather than the relative order, because a click emitted twice — once
        from the replay and once from a leftover loop — keeps the order and
        is still wrong.
        """
        proxy()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok", "session_id": "s1", "actions": [],
        }
        mock_post.return_value = mock_resp

        main([
            "interact", "s1",
            "--fill", "#email=me@example.com",
            "--fill-credential", "#password=acme_password",
            "--click", "button[type=submit]",
        ])
        payload = mock_post.call_args[1]["json"]
        assert payload["actions"] == [
            {"type": "fill", "selector": "#email", "value": "me@example.com"},
            {
                "type": "fill",
                "selector": "#password",
                "value": self.VAULT["acme_password"],
            },
            {"type": "click", "selector": "button[type=submit]"},
        ]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_click_before_a_fill_still_leads(self, mock_url, mock_post, proxy):
        """The other direction, which worked before and has to keep working.

        Positional ordering is strictly more expressive than the click-first
        rule it replaces: a caller who wants the click first — opening a login
        modal, dismissing a cookie banner — writes it first and gets it first.
        """
        proxy()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok", "session_id": "s1", "actions": [],
        }
        mock_post.return_value = mock_resp

        main([
            "interact", "s1",
            "--click", ".show-login",
            "--fill-credential", "#password=acme_password",
        ])
        payload = mock_post.call_args[1]["json"]
        assert payload["actions"] == [
            {"type": "click", "selector": ".show-login"},
            {
                "type": "fill",
                "selector": "#password",
                "value": self.VAULT["acme_password"],
            },
        ]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_several_clicks_keep_their_order_among_the_fills(
        self, mock_url, mock_post, proxy,
    ):
        """Clicks interleave with fills rather than clustering at either end."""
        proxy()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok", "session_id": "s1", "actions": [],
        }
        mock_post.return_value = mock_resp

        main([
            "interact", "s1",
            "--click", ".accept-cookies",
            "--fill", "#email=me@example.com",
            "--click", ".next",
            "--fill-credential", "#password=acme_password",
            "--click", "button[type=submit]",
        ])
        payload = mock_post.call_args[1]["json"]
        assert [
            (a["type"], a["selector"]) for a in payload["actions"]
        ] == [
            ("click", ".accept-cookies"),
            ("fill", "#email"),
            ("click", ".next"),
            ("fill", "#password"),
            ("click", "button[type=submit]"),
        ]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_unknown_name_refuses_before_the_request(
        self, mock_url, mock_post, proxy, capsys,
    ):
        proxy()
        code = None
        try:
            main(["interact", "s1", "--fill-credential", "#password=nope"])
        except SystemExit as exc:
            code = exc.code
        # "No request was sent" is true on BOTH sides of the pre-dispatch
        # refusal, because `cmd_interact` refuses an unresolved reference
        # itself — so this one assertion, useful as it is, discriminates
        # nothing. What separates the two states is whose refusal this is: the
        # two assertions below are what the control turns red, and
        # `tests/test_skill_credential_refs.py::TestTheRefusal` carries the
        # handler-did-not-run form against a recording handler.
        mock_post.assert_not_called()
        assert code == 1
        envelope = json.loads(capsys.readouterr().out.strip())
        assert envelope["reason"] == "vault_credential_refused"
        assert "not resolved" not in envelope["error"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_error_envelope_carries_no_filled_value(
        self, mock_url, mock_post, proxy, capsys,
    ):
        """`on_exception` names the endpoint and the exception, never a value."""
        proxy()
        mock_post.side_effect = httpx.ReadTimeout("timed out")
        with pytest.raises(SystemExit):
            main([
                "interact", "s1",
                "--fill-credential", "#password=acme_password",
            ])
        blob = capsys.readouterr().out
        assert "browse interact" in blob
        for value in self.VAULT.values():
            assert value not in blob

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_unresolved_reference_is_never_typed_into_the_field(
        self, mock_url, mock_post,
    ):
        """A namespace that skipped `parse_and_resolve` must not fill anything.

        Filling the field with the credential's *name* would be a failed login
        whose cause is invisible from the result, so the handler refuses.
        """
        parser = build_parser()
        args = parser.parse_args(
            ["interact", "s1", "--fill-credential", "#password=acme_password"],
        )
        with pytest.raises(ValueError, match="not resolved"):
            cmd_interact(args)
        mock_post.assert_not_called()

    def test_the_parser_accepts_the_flag_repeatably(self):
        parser = build_parser()
        args = parser.parse_args([
            "interact", "s1",
            "--fill-credential", "#a=one",
            "--fill-credential", "#b=two",
        ])
        assert args.fill_credential == ["#a=one", "#b=two"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_attribute_selector_is_not_mis_split(
        self, mock_url, mock_post, proxy,
    ):
        """`input[type=password]=name` splits at the LAST `=`.

        A credential name cannot contain `=` (`secrets_vault.VAULT_NAME_RE`)
        and an attribute selector routinely does, so splitting at the first one
        made the label `input[type` and the name `password]=acme_password` —
        non-empty, so it was sent to the proxy, charged against the attempt's
        fetch budget, and refused with a message naming the vault rather than
        the selector.
        """
        proxy()
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok", "session_id": "s1", "actions": [],
        }
        mock_post.return_value = mock_resp

        main([
            "interact", "s1",
            "--fill-credential", "input[type=password]=acme_password",
        ])

        payload = mock_post.call_args[1]["json"]
        assert payload["actions"] == [
            {
                "type": "fill",
                "selector": "input[type=password]",
                "value": self.VAULT["acme_password"],
            },
        ]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_value_the_page_reflects_back_is_scrubbed(
        self, mock_url, mock_post, proxy, capsys,
    ):
        """The container reports the selector, and the page is not under that rule.

        `/interact` returns the page's own URL and text, so a GET form's
        submission puts the value in the query string and a page that echoes
        what was typed puts it in the body. The one process that knows which
        strings are credentials takes them back out.
        """
        proxy()
        secret = self.VAULT["acme_password"]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok",
            "session_id": "s1",
            "url": f"https://site.example/login?password={secret}",
            "text": f"We could not sign you in as {secret}",
            "actions": [{"action": "fill", "selector": "#password", "ok": True}],
        }
        mock_post.return_value = mock_resp

        main(["interact", "s1", "--fill-credential", "#password=acme_password"])

        out = capsys.readouterr().out
        assert secret not in out
        assert "browsevalue" not in out
        assert out.count("[credential]") == 2

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_container_error_body_is_scrubbed_too(
        self, mock_url, mock_post, proxy, capsys,
    ):
        """The 500 branch stringifies a third-party exception we do not control."""
        proxy()
        secret = self.VAULT["acme_password"]
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "error",
            "error": f'page.fill: Timeout. Call log: fill("{secret}")',
        }
        mock_post.return_value = mock_resp

        with pytest.raises(SystemExit):
            main(["interact", "s1", "--fill-credential", "#password=acme_password"])

        out = capsys.readouterr().out
        assert secret not in out
        assert "[credential]" in out

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_response_with_no_credential_fill_is_untouched(
        self, mock_url, mock_post,
    ):
        """The control for the scrub: nothing is rewritten without a credential."""
        parser = build_parser()
        args = parser.parse_args(["interact", "s1", "--fill", "#a=b"])
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "status": "ok", "session_id": "s1", "text": "[credential] is fine",
        }
        mock_post.return_value = mock_resp
        assert cmd_interact(args)["text"] == "[credential] is fine"


# --------------------------------------------------------------------------- #
# Visual mode: the picture's coordinate frame, and a point read off it
# --------------------------------------------------------------------------- #


def _real_png(width, height):
    """A PNG whose IHDR really says `width` x `height`.

    The stand-in at the top of this file is eight signature bytes and some
    text, which is enough for the sniff and not for a resize: the resize path
    opens the bytes, and a test whose fixture Pillow cannot read would pass for
    the wrong reason.
    """
    from io import BytesIO

    from PIL import Image

    buffer = BytesIO()
    Image.new("RGB", (width, height), (20, 30, 40)).save(buffer, format="PNG")
    return buffer.getvalue()


def _capture_record(width, height, **overrides):
    """What the container puts on `X-Browse-Capture` for a viewport capture."""
    record = {
        "image": [width, height],
        "window": {"x": 0, "y": 0, "width": width, "height": height + 87},
        "offset": [0, 87],
        "page": {
            "viewport": [width, height],
            "dpr": 1,
            "scroll": [0, 0],
            "url": "https://example.com/booking",
        },
        "full_page": False,
        "at": 1_758_000_000.0,
    }
    record.update(overrides)
    return record


def _screenshot_response(content, headers=None):
    resp = MagicMock()
    resp.status_code = 200
    resp.content = content
    resp.headers = {"content-type": "image/png", **(headers or {})}
    return resp


class TestTheScratchDefault:
    """Where a capture goes with no `-o`, and what the answer says about it.

    None of this had a test before: every screenshot case in this file passed
    `-o`, so `screenshot_dir`, `_write_derived_capture` and
    `_workspace_relative` were reached by nothing. The destination moving off
    the user's workspace is what made the gap worth closing rather than
    noting.
    """

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_capture_with_no_output_lands_in_the_tasks_temp_dir(
        self, mock_url, mock_post, workspace, deferred_dir,
    ):
        mock_post.return_value = _screenshot_response(PNG_BYTES)

        result = cmd_screenshot(
            build_parser().parse_args(["screenshot", "https://example.com"]),
        )

        assert result["status"] == "ok", result
        written = Path(result["path"])
        assert written.parent == deferred_dir / "screenshots"
        assert written.read_bytes() == PNG_BYTES
        # The workspace is resolvable here and is still not where this went:
        # the fixture sets both roots, so a pass is the destination rule and
        # not an unavailable alternative.
        assert workspace not in written.parents

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_scratch_capture_says_so_and_carries_no_chat_url(
        self, mock_url, mock_post, workspace, deferred_dir,
    ):
        """The absence and the note are asserted together.

        `workspace_path` missing is the whole functional difference and it
        teaches a model nothing on its own, so the note is what has to be
        there — and the note alone would be satisfied by a capture that had
        gone to the workspace anyway.
        """
        mock_post.return_value = _screenshot_response(PNG_BYTES)

        result = cmd_screenshot(
            build_parser().parse_args(["screenshot", "https://example.com"]),
        )

        assert "workspace_path" not in result, result
        assert SCRATCH_NOTE in result.get("notes", []), result

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_output_in_the_workspace_still_earns_the_chat_url(
        self, mock_url, mock_post, workspace,
    ):
        """The control. Embedding still works when a caller asks for it, which
        is what makes the move a change of default rather than a removal."""
        mock_post.return_value = _screenshot_response(PNG_BYTES)

        result = cmd_screenshot(
            build_parser().parse_args([
                "screenshot", "https://example.com",
                "-o", str(workspace / "radar.png"),
            ]),
        )

        assert result["workspace_path"] == "/Users/alice/radar.png"
        assert SCRATCH_NOTE not in result.get("notes", []), result

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_two_captures_in_one_second_do_not_overwrite_each_other(
        self, mock_url, mock_post, workspace, deferred_dir,
    ):
        """`O_EXCL` and the suffix ladder, on the path that derives names.

        Two tasks of one user share this directory and the stem is a UTC
        second, so a check-then-write would have the second report `ok` over
        bytes that are no longer there.
        """
        argv = ["screenshot", "https://example.com"]
        mock_post.return_value = _screenshot_response(PNG_BYTES)
        first = cmd_screenshot(build_parser().parse_args(argv))
        mock_post.return_value = _screenshot_response(PNG_BYTES + b"second")
        second = cmd_screenshot(build_parser().parse_args(argv))

        assert first["path"] != second["path"]
        assert Path(first["path"]).read_bytes() == PNG_BYTES
        assert Path(second["path"]).read_bytes() == PNG_BYTES + b"second"

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_no_temp_dir_refuses_by_name_before_the_browser_is_asked(
        self, mock_url, mock_post, workspace, monkeypatch,
    ):
        """The one remaining failure reason, and it costs no browser time.

        The refusal names `--output` because the workspace root survives the
        deferred dir's absence — the explicit form really would have worked,
        so the remedy is not a stock sentence.
        """
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR")

        result = cmd_screenshot(
            build_parser().parse_args(["screenshot", "https://example.com"]),
        )

        assert result["status"] == "error"
        assert "ISTOTA_DEFERRED_DIR" in result["error"]
        assert "--output" in result["error"]
        assert not mock_post.called


class TestTheDeliveredCaptureFrame:
    """`screenshot` reports the frame it delivered, and resizes to fit it."""

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_capture_inside_the_envelope_is_written_untouched(
        self, mock_url, mock_post, workspace,
    ):
        content = _real_png(1280, 800)
        record = _capture_record(1280, 800)
        mock_post.return_value = _screenshot_response(
            content, {"X-Browse-Capture": json.dumps(record)},
        )

        output = str(workspace / "shot.png")
        args = build_parser().parse_args(
            ["screenshot", "--session", "s1", "-o", output],
        )
        result = cmd_screenshot(args)

        assert result["status"] == "ok"
        assert result["capture"] == {
            "image": [1280, 800],
            "viewport": [1280, 800],
            "dpr": 1,
            "scale": 1.0,
            "full_page": False,
        }
        # Byte-identical, so `scale: 1` really is the identity rather than a
        # re-encode that happens to come out the same size.
        assert (workspace / "shot.png").read_bytes() == content
        assert result["size"] == len(content)

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_oversize_capture_is_resized_before_it_is_written(
        self, mock_url, mock_post, workspace,
    ):
        mock_post.return_value = _screenshot_response(
            _real_png(1920, 1080),
            {"X-Browse-Capture": json.dumps(_capture_record(1920, 1080))},
        )

        output = str(workspace / "shot.png")
        args = build_parser().parse_args(
            ["screenshot", "--session", "s1", "-o", output],
        )
        result = cmd_screenshot(args)

        from istota.image_sniff import image_dimensions

        written = (workspace / "shot.png").read_bytes()
        assert result["capture"]["image"] == [1429, 804]
        assert result["capture"]["viewport"] == [1920, 1080]
        assert result["capture"]["scale"] < 1
        # The file on disk, the reported size and the reported frame are one
        # picture. A resize that did not reach the write would leave `size`
        # describing bytes that are no longer there.
        assert image_dimensions(written) == (1429, 804)
        assert result["size"] == len(written)

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_pillow_failure_is_an_error_rather_than_unresized_bytes(
        self, mock_url, mock_post, workspace,
    ):
        # A PNG signature the sniff admits over bytes Pillow cannot open, which
        # is what a truncated or rewritten body looks like.
        mock_post.return_value = _screenshot_response(
            PNG_BYTES,
            {"X-Browse-Capture": json.dumps(_capture_record(1920, 1080))},
        )

        output = workspace / "shot.png"
        args = build_parser().parse_args(
            ["screenshot", "--session", "s1", "-o", str(output)],
        )
        result = cmd_screenshot(args)

        assert result["status"] == "error"
        assert "1920x1080" in result["error"]
        assert "1429x804" in result["error"]
        # Nothing written: an oversize picture delivered as if it were in frame
        # is a click that lands somewhere else.
        assert not output.exists()

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_old_container_sends_no_header_and_is_said_so(
        self, mock_url, mock_post, workspace,
    ):
        mock_post.return_value = _screenshot_response(_real_png(1280, 800))

        args = build_parser().parse_args(
            ["screenshot", "--session", "s1", "-o", str(workspace / "shot.png")],
        )
        result = cmd_screenshot(args)

        assert result["status"] == "ok"
        assert result["capture"] is None
        assert "predates visual mode" in result["notes"][0]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_container_that_could_not_measure_says_why(
        self, mock_url, mock_post, workspace,
    ):
        mock_post.return_value = _screenshot_response(
            _real_png(1280, 800),
            {"X-Browse-Capture-Error": "Chrome window not found on the X11 display"},
        )

        args = build_parser().parse_args(
            ["screenshot", "--session", "s1", "-o", str(workspace / "shot.png")],
        )
        result = cmd_screenshot(args)

        assert result["capture"] is None
        assert "Chrome window not found" in result["notes"][0]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_measureless_capture_still_reports_a_frame(
        self, mock_url, mock_post, workspace,
    ):
        # `/screenshot` takes `measure: false`, which skips the one CDP
        # evaluate and leaves `page` null. The X11 half of the frame is intact,
        # so the picture is still clickable and only the viewport is unknown.
        record = _capture_record(1280, 800, page=None)
        mock_post.return_value = _screenshot_response(
            _real_png(1280, 800), {"X-Browse-Capture": json.dumps(record)},
        )

        args = build_parser().parse_args(
            ["screenshot", "--session", "s1", "-o", str(workspace / "shot.png")],
        )
        result = cmd_screenshot(args)

        assert result["capture"] == {
            "image": [1280, 800],
            "viewport": None,
            "dpr": 1,
            "scale": 1.0,
            "full_page": False,
        }


class TestAPointReadOffThePicture:
    """`interact --click-at` sends the picture's own size, and refuses when
    there is no frame to interpret the point against."""

    @staticmethod
    def _session_response(capture):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {
            "status": "ok", "session_id": "s1", "alive": True, "capture": capture,
        }
        return resp

    @staticmethod
    def _interact_response():
        resp = MagicMock()
        resp.json.return_value = {
            "status": "ok",
            "session_id": "s1",
            "actions": [{"action": "click_at", "ok": True, "screen": [412, 405]}],
        }
        return resp

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_delivered_size_rides_the_action(self, mock_url, mock_get, mock_post):
        mock_get.return_value = self._session_response(_capture_record(1920, 1080))
        mock_post.return_value = self._interact_response()

        args = build_parser().parse_args(
            ["interact", "s1", "--click-at", "412,318"],
        )
        result = cmd_interact(args)

        assert result["status"] == "ok"
        assert mock_get.call_args[0][0] == "http://test:9223/sessions/s1"
        # The same number `cmd_screenshot` resized to, recomputed here in a
        # process that remembers nothing. This is the whole contract.
        assert mock_post.call_args[1]["json"]["actions"] == [
            {"type": "click_at", "x": 412.0, "y": 318.0, "image_size": [1429, 804]},
        ]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_in_envelope_capture_sends_its_own_size(
        self, mock_url, mock_get, mock_post,
    ):
        mock_get.return_value = self._session_response(_capture_record(1280, 800))
        mock_post.return_value = self._interact_response()

        cmd_interact(build_parser().parse_args(
            ["interact", "s1", "--hover-at", "10,20"],
        ))

        assert mock_post.call_args[1]["json"]["actions"] == [
            {"type": "hover_at", "x": 10.0, "y": 20.0, "image_size": [1280, 800]},
        ]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_session_with_no_capture_refuses_before_any_post(
        self, mock_url, mock_get, mock_post,
    ):
        mock_get.return_value = self._session_response(None)

        result = cmd_interact(build_parser().parse_args(
            ["interact", "s1", "--click-at", "1,2"],
        ))

        assert result["status"] == "error"
        assert "no screenshot on record" in result["error"]
        # Local refusal: nothing is clicked, and no browser time is spent.
        mock_post.assert_not_called()

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_old_container_has_no_capture_key_at_all(
        self, mock_url, mock_get, mock_post,
    ):
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"status": "ok", "session_id": "s1", "alive": True}
        mock_get.return_value = resp

        result = cmd_interact(build_parser().parse_args(
            ["interact", "s1", "--click-at", "1,2"],
        ))

        assert result["status"] == "error"
        assert "predates visual mode" in result["error"]
        mock_post.assert_not_called()

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_keyboard_actions_need_no_frame(self, mock_url, mock_get, mock_post):
        mock_post.return_value = self._interact_response()

        cmd_interact(build_parser().parse_args(
            ["interact", "s1", "--type", "Ada Lovelace", "--press", "Tab"],
        ))

        # They act on whatever has focus, which is a property of the page
        # rather than of a picture, so no session read happens at all.
        mock_get.assert_not_called()
        assert mock_post.call_args[1]["json"]["actions"] == [
            {"type": "type", "text": "Ada Lovelace"},
            {"type": "key", "key": "Tab"},
        ]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_unknown_action_becomes_a_named_old_container_sentence(
        self, mock_url, mock_get, mock_post,
    ):
        resp = MagicMock()
        resp.json.return_value = {
            "status": "ok",
            "session_id": "s1",
            "actions": [{"action": "type", "ok": False, "error": "unknown"}],
        }
        mock_post.return_value = resp

        result = cmd_interact(build_parser().parse_args(
            ["interact", "s1", "--type", "hello"],
        ))

        assert "older than this skill" in result["notes"][0]
        assert "type" in result["notes"][0]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_unknown_selector_action_is_left_alone(
        self, mock_url, mock_get, mock_post,
    ):
        # The container has always answered this way for an action it does not
        # know. Only the visual types mean "the image is behind this code".
        resp = MagicMock()
        resp.json.return_value = {
            "status": "ok",
            "session_id": "s1",
            "actions": [{"action": "teleport", "ok": False, "error": "unknown"}],
        }
        mock_post.return_value = resp

        result = cmd_interact(build_parser().parse_args(
            ["interact", "s1", "--click", ".btn"],
        ))

        assert "notes" not in result

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_coordinate_interleaves_with_a_fill_in_written_order(
        self, mock_url, mock_get, mock_post,
    ):
        mock_get.return_value = self._session_response(_capture_record(1280, 800))
        mock_post.return_value = self._interact_response()

        cmd_interact(build_parser().parse_args([
            "interact", "s1",
            "--click-at", "10,20",
            "--fill", "#q=hello",
            "--press", "Enter",
            "--scroll", "down",
        ]))

        actions = mock_post.call_args[1]["json"]["actions"]
        assert [a["type"] for a in actions] == ["click_at", "fill", "key", "scroll"]
        # `--scroll` is still last whatever position it was written in.
        assert actions[-1]["direction"] == "down"


class TestAMalformedPoint:
    @pytest.mark.parametrize("value", ["412", "a,b", "412,", ",318", "nan,1", "1,inf"])
    def test_it_is_refused_rather_than_coerced(self, value):
        # Refused, not skipped: the actions around a dropped click would still
        # run and still report `ok`, which is ISSUE-507's symptom by another
        # route.
        args = build_parser().parse_args(["interact", "s1", "--click-at", value])
        with pytest.raises(ValueError, match="--click-at"):
            _interact_actions(args)


class TestNoCredentialAtAPoint:
    """`--fill-credential` stays selector-only, and the point is the absence.

    A source assertion because what is being checked is that a capability is
    not there: `page.fill` refuses a selector that is not fillable, and a
    coordinate fill is a click and then a type, where the type succeeds
    whatever the click did.
    """

    def test_no_coordinate_emitter_can_carry_a_credential_pair(self):
        from istota.skills import browse
        from tests.support.drift import source_of

        for dest in ("click_at", "hover_at", "press", "type"):
            source = source_of(ACTION_EMITTERS[dest])
            assert "CredentialPair" not in source
            assert "reveal" not in source
        # And the one emitter that does unwrap a credential is still reached
        # only by the selector-shaped flag.
        assert "reveal" in source_of(browse._fill_credential_action)

    def test_the_credential_flag_is_declared_with_a_selector_metavar(self):
        parser = _interact_parser()
        flags = {
            option: action
            for action in parser._actions
            for option in action.option_strings
        }
        assert flags["--fill-credential"].metavar == "SELECTOR=NAME"
        assert flags["--click-at"].metavar == "X,Y"


class TestAnActionWithNoResult:
    """A 500 with a short `actions` list, which is what a click that raised
    after the pointer already moved looks like from here.

    Measured, not hypothetical: `xdotool mousemove --sync` blocks on a
    zero-distance move and times out at five seconds, and the Bezier path's
    final landing move is within a pixel of its predecessor often enough that
    roughly three in ten coordinate clicks raised on a live container. The
    timeout can fire after the press has been delivered.
    """

    @staticmethod
    def _failed_after(n_results):
        resp = MagicMock()
        resp.status_code = 500
        resp.json.return_value = {
            "status": "error",
            "session_id": "s1",
            "actions": [
                {"action": "click_at", "ok": True, "screen": [1, 2]}
            ] * n_results,
            "error": (
                "Command '['xdotool', 'mousemove', '--sync', '--screen', "
                "'0', '819', '396']' timed out after 5 seconds"
            ),
        }
        return resp

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_actions_with_no_result_are_named(self, mock_url, mock_get, mock_post):
        mock_get.return_value = TestAPointReadOffThePicture._session_response(
            _capture_record(1280, 800),
        )
        mock_post.return_value = self._failed_after(0)

        result = cmd_interact(build_parser().parse_args([
            "interact", "s1", "--click-at", "10,20", "--press", "Enter",
        ]))

        assert result["status"] == "error"
        assert result["unreported_actions"] == ["click_at", "key"]
        note = result["notes"][0]
        assert "may still have happened" in note
        assert "do not simply retry" in note

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_partial_list_names_only_the_tail(self, mock_url, mock_get, mock_post):
        mock_get.return_value = TestAPointReadOffThePicture._session_response(
            _capture_record(1280, 800),
        )
        mock_post.return_value = self._failed_after(1)

        result = cmd_interact(build_parser().parse_args([
            "interact", "s1", "--click-at", "10,20", "--click-at", "30,40",
        ]))

        # The container appends one result per action in order, so the first
        # action with no result is the one that raised.
        assert result["unreported_actions"] == ["click_at"]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_complete_failure_list_gets_no_note(self, mock_url, mock_get, mock_post):
        # Every action reported a result and the call still failed — an
        # ordinary refusal, where nothing is unknown and the note would be a
        # false alarm.
        mock_get.return_value = TestAPointReadOffThePicture._session_response(
            _capture_record(1280, 800),
        )
        resp = MagicMock()
        resp.status_code = 500
        resp.json.return_value = {
            "status": "error", "session_id": "s1", "error": "something else",
            "actions": [{"action": "click_at", "ok": False, "error": "stale_capture"}],
        }
        mock_post.return_value = resp

        result = cmd_interact(build_parser().parse_args([
            "interact", "s1", "--click-at", "10,20",
        ]))

        assert "unreported_actions" not in result
        assert "notes" not in result

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_successful_call_gets_no_note(self, mock_url, mock_get, mock_post):
        mock_get.return_value = TestAPointReadOffThePicture._session_response(
            _capture_record(1280, 800),
        )
        mock_post.return_value = TestAPointReadOffThePicture._interact_response()

        result = cmd_interact(build_parser().parse_args([
            "interact", "s1", "--click-at", "10,20",
        ]))

        assert result["status"] == "ok"
        assert "unreported_actions" not in result


class TestAnInteractionThatNeverAnswered:
    """A transport failure with no response, where the container carries on.

    The response-based caution can only fire on a body. A `ReadTimeout`, a
    reset connection or a socket dropped mid-POST produces no body at all,
    so the whole action list went out to a container that never learned the
    client had gone away — and the model was handed a bare failure. It then
    retries from a page it believes is unchanged, which is exactly what the
    caution exists to stop.

    Reachable without anything going wrong at the network layer: the request
    timeout is 120s and a long type action is paced in the tens of
    milliseconds per character, so an ordinary list can outlast it.
    """

    @staticmethod
    def _args(*extra):
        return build_parser().parse_args(
            ["interact", "s1", "--click", "#one", "--fill", "#q=hello", *extra],
        )

    @pytest.mark.parametrize("exc", [
        httpx.ReadTimeout("timed out"),
        httpx.WriteTimeout("timed out"),
        httpx.ReadError("reset"),
        httpx.WriteError("broken pipe"),
        httpx.RemoteProtocolError("peer closed"),
    ])
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_an_ambiguous_failure_cautions_about_the_whole_list(
        self, mock_url, mock_post, exc,
    ):
        mock_post.side_effect = exc

        result = cmd_interact(self._args())

        assert result["status"] == "error"
        assert result["unreported_actions"] == ["click", "fill"]
        note = result["notes"][0]
        assert "any of them may have happened" in note
        assert "do not simply retry" in note

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_error_names_the_exception_class(self, mock_url, mock_post):
        """`str(ReadTimeout)` is often empty, so the class has to carry it.

        The same lesson `describe` records one layer up: a message built from
        `str(exc)` alone names no verb, no URL and no class.
        """
        mock_post.side_effect = httpx.ReadTimeout("")

        result = cmd_interact(self._args())

        assert "ReadTimeout" in result["error"]
        assert "http://test:9223" in result["error"]

    @pytest.mark.parametrize("exc", [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("timed out"),
        httpx.PoolTimeout("no connection"),
    ])
    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_failure_before_the_request_left_is_not_cautioned(
        self, mock_url, mock_post, exc,
    ):
        """Nothing reached the container, so nothing can have happened.

        Cautioning here would tell the model its actions might have run
        against a container it never opened a connection to — and it would
        replace `describe`'s "is the container running?" with a sentence
        about page state, which is the wrong remedy entirely.
        """
        mock_post.side_effect = exc

        with pytest.raises(type(exc)):
            cmd_interact(self._args())

    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_failure_fetching_the_capture_frame_is_not_cautioned(
        self, mock_url, mock_get,
    ):
        """The frame is read before the actions are sent, so it is pre-send.

        This is the one caution-worthy-looking failure inside `cmd_interact`
        that genuinely is not: `/interact` has not been called yet.
        """
        mock_get.side_effect = httpx.ReadTimeout("timed out")

        with pytest.raises(httpx.ReadTimeout):
            cmd_interact(build_parser().parse_args(
                ["interact", "s1", "--click-at", "10,20"],
            ))

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_two_cautions_do_not_read_alike(self, mock_url, mock_post):
        """"Some of these may have happened" and "any of these may have"
        call for different recovery, so the notes must not be one sentence.

        The response arm knows which actions are in doubt and names the first
        of them; this arm has no results list and can only name the whole
        list. Both notes are built here and compared, because the property is
        that they differ — asserting a phrase in one of them would pass just
        as happily if the other were changed to match it.
        """
        mock_post.side_effect = httpx.ReadTimeout("timed out")
        transport_note = cmd_interact(self._args())["notes"][0]

        actions = [{"type": "click"}, {"type": "fill"}]
        response_note = _note_unreported_actions(
            {"status": "error", "actions": []}, actions,
        )["notes"][0]

        assert transport_note != response_note
        assert "The first of them may still have happened" in response_note
        assert "The first of them" not in transport_note
        assert "any of them may have happened" in transport_note
        assert "any of them may have happened" not in response_note
        # Both still tell the model to look before repeating, which is the
        # one instruction they share.
        assert "do not simply retry" in transport_note
        assert "do not simply retry" in response_note

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_caution_still_fails_the_command(self, mock_url, mock_post, capsys):
        """The caution is a returned envelope where it used to be a raise.

        `run_skill_cli` exits on a returned error envelope as it does on an
        exception, so the task still fails — a note the model reads on a
        command that reported success would be worse than no note.
        """
        mock_post.side_effect = httpx.ReadTimeout("timed out")

        with pytest.raises(SystemExit) as exc_info:
            main(["interact", "s1", "--click", "#one"])
        assert exc_info.value.code == 1

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "error"
        assert output["unreported_actions"] == ["click"]
        assert "any of them may have happened" in output["notes"][0]

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_a_transport_failure_on_another_verb_gains_no_caution(
        self, mock_url, mock_post, capsys,
    ):
        """`on_exception` is registered for the whole CLI, so the caution had
        to be scoped to the interact path rather than attached there.

        `get` sends no actions, so a page-state caution on it would be noise
        pointing at a list that does not exist.
        """
        mock_post.side_effect = httpx.ReadTimeout("timed out")

        with pytest.raises(SystemExit):
            main(["get", "https://example.com"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "error"
        assert "notes" not in output
        assert "unreported_actions" not in output


def _alive_response(capture):
    """What `GET /sessions/<id>` answers, for the framed-action capture read."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "status": "ok", "session_id": "s1", "alive": True, "capture": capture,
    }
    return resp


def _ok_interact_response():
    resp = MagicMock()
    resp.json.return_value = {"status": "ok", "session_id": "s1", "actions": []}
    return resp


class TestTheScrollFlags:
    """`--scroll-at` has a position; the keyless `--scroll` still does not.

    ISSUE-528. A scroll used to be one thing — a CDP `window.scrollBy` on the
    document — so it was not repeatable, had no position, and `cmd_interact`
    appended it last whatever the caller wrote. A wheel *at a point* is none of
    those: two of them at two points are two different actions, and one before
    a click is a different outcome from one after it. So `--scroll-at` joins
    `ACTION_EMITTERS` with the rest and the keyless form keeps its old slot.
    """

    def _actions(self, argv):
        return _interact_actions(build_parser().parse_args(
            ["interact", "s1"] + argv,
        ))

    def test_a_point_scroll_is_a_wheel_at_that_point(self):
        assert self._actions(["--scroll-at", "412,318"]) == [{
            "type": "scroll_at", "x": 412.0, "y": 318.0,
            "direction": "down", "clicks": WHEEL_CLICKS_DEFAULT,
        }]

    def test_the_direction_flag_supplies_the_point_scroll_s_direction(self):
        actions = self._actions(["--scroll", "up", "--scroll-at", "10,20"])
        assert [a["type"] for a in actions] == ["scroll_at"]
        assert actions[0]["direction"] == "up"

    def test_a_directed_point_scroll_appends_no_keyless_scroll(self):
        """`--scroll` contributes direction there and nothing else. Emitting
        both would scroll the widget and then the document behind it, which is
        the failure this issue is about, performed twice."""
        actions = self._actions(["--scroll", "down", "--scroll-at", "10,20"])
        assert [a["type"] for a in actions] == ["scroll_at"]

    def test_the_keyless_scroll_is_presses_rather_than_pixels(self):
        assert self._actions(["--scroll", "down"]) == [{
            "type": "scroll", "direction": "down",
            "presses": KEY_PRESSES_DEFAULT,
        }]

    def test_the_keyless_scroll_is_still_last_whatever_position_it_was_written_in(self):
        actions = self._actions([
            "--scroll", "down", "--click-at", "10,20", "--press", "Enter",
        ])
        assert [a["type"] for a in actions] == ["click_at", "key", "scroll"]

    def test_a_point_scroll_interleaves_in_the_order_written(self):
        actions = self._actions([
            "--click-at", "10,20",
            "--scroll-at", "30,40",
            "--fill", "#q=hello",
        ])
        assert [a["type"] for a in actions] == ["click_at", "scroll_at", "fill"]

    def test_two_point_scrolls_are_two_actions(self):
        actions = self._actions(["--scroll-at", "10,20", "--scroll-at", "30,40"])
        assert [(a["x"], a["y"]) for a in actions] == [(10.0, 20.0), (30.0, 40.0)]

    def test_the_click_count_reaches_both_paths_under_its_own_name(self):
        assert self._actions(
            ["--scroll-at", "10,20", "--scroll-clicks", "7"],
        )[0]["clicks"] == 7
        assert self._actions(
            ["--scroll", "down", "--scroll-clicks", "4"],
        )[0]["presses"] == 4

    def test_zoom_is_the_modifier_on_a_point_scroll(self):
        action = self._actions(
            ["--scroll", "up", "--scroll-at", "10,20", "--scroll-zoom"],
        )[0]
        assert action["modifier"] == "ctrl"

    def test_an_ordinary_point_scroll_carries_no_modifier(self):
        """The control for the test above: a key on the action means a
        gesture was asked for, so the ordinary case must not carry one."""
        assert "modifier" not in self._actions(["--scroll-at", "10,20"])[0]

    def test_zoom_without_a_point_is_refused(self):
        """There is no keyless zoom: Page_Down cannot carry the gesture, so
        the flag would be accepted and do nothing."""
        with pytest.raises(ValueError, match="--scroll-zoom"):
            self._actions(["--scroll", "down", "--scroll-zoom"])

    def test_a_click_count_with_no_scroll_at_all_is_refused(self):
        with pytest.raises(ValueError, match="--scroll-clicks"):
            self._actions(["--scroll-clicks", "3"])

    @pytest.mark.parametrize("count", ["0", "-1", "999"])
    def test_a_click_count_outside_the_bound_is_refused(self, count):
        with pytest.raises(ValueError, match="--scroll-clicks"):
            self._actions(["--scroll-at", "10,20", "--scroll-clicks", count])

    @pytest.mark.parametrize("value", ["412", "a,b", "412,", "nan,1"])
    def test_a_malformed_point_is_refused_rather_than_coerced(self, value):
        with pytest.raises(ValueError, match="--scroll-at"):
            self._actions(["--scroll-at", value])


class TestTheRetiredPixelArgument:
    """`--scroll-amount` is refused by name, not dropped and not reinterpreted.

    A wheel tick is a distance the browser picks and a Page_Down is a viewport,
    so neither path can honour a pixel figure — and `skill.md` documented
    `--scroll-amount 2000` as the infinite-scroll recipe, which a task holds in
    its prompt from the moment it started. So the flag stays declared and
    answers with the name that replaced it: argparse's own `unrecognized
    arguments` would be usage text with no remedy in it.
    """

    def test_it_says_what_replaced_it(self):
        args = build_parser().parse_args(
            ["interact", "s1", "--scroll", "down", "--scroll-amount", "2000"],
        )
        with pytest.raises(ValueError, match="--scroll-clicks"):
            _interact_actions(args)

    def test_it_is_not_advertised(self):
        """Suppressed rather than deleted, so the help does not offer a flag
        every call refuses."""
        for action in _interact_parser()._actions:
            if "--scroll-amount" in action.option_strings:
                assert action.help == argparse.SUPPRESS
                return
        raise AssertionError("--scroll-amount is no longer declared at all")


class TestThePointScrollIsFramed:
    """It is read off a picture, so it needs the delivered size like a click."""

    def test_scroll_at_is_an_image_space_action(self):
        assert "scroll_at" in IMAGE_SPACE_ACTIONS
        assert "scroll_at" in VISUAL_ACTION_TYPES

    def test_the_keyless_scroll_is_neither(self):
        """It converts nothing, so it must not drag a capture fetch in front
        of itself — and an old container still scrolls the document for it, so
        an `unknown` naming it would be wrong about what happened."""
        assert "scroll" not in IMAGE_SPACE_ACTIONS
        assert "scroll" not in VISUAL_ACTION_TYPES

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_delivered_size_is_patched_onto_it(
        self, mock_url, mock_get, mock_post,
    ):
        mock_post.return_value = _ok_interact_response()
        mock_get.return_value = _alive_response(_capture_record(1280, 800))

        cmd_interact(build_parser().parse_args(
            ["interact", "s1", "--scroll-at", "10,20"],
        ))

        action = mock_post.call_args[1]["json"]["actions"][0]
        assert action["image_size"] == list(delivered_size(1280, 800))

    @patch("istota.skills.browse.httpx.post")
    @patch("istota.skills.browse.httpx.get")
    @patch("istota.skills.browse.get_api_url", return_value="http://test:9223")
    def test_the_keyless_scroll_fetches_no_capture(
        self, mock_url, mock_get, mock_post,
    ):
        mock_post.return_value = _ok_interact_response()

        cmd_interact(build_parser().parse_args(
            ["interact", "s1", "--scroll", "down"],
        ))

        mock_get.assert_not_called()


class TestTheScrollRefusalsAreNotDefaults:
    """Where a guess is cheap to get wrong, the flag is required instead.

    A plain `--scroll-at` defaults to `down`, because scrolling a pane the
    wrong way costs one more call. Zoom does not: zooming a map out when the
    caller meant in changes what the next screenshot shows, and reads as the
    page having moved rather than as a wrong flag.
    """

    def _actions(self, argv):
        return _interact_actions(build_parser().parse_args(
            ["interact", "s1"] + argv,
        ))

    def test_zoom_with_no_direction_is_refused_rather_than_zooming_out(self):
        with pytest.raises(ValueError, match="--scroll up or --scroll down"):
            self._actions(["--scroll-at", "10,20", "--scroll-zoom"])

    @pytest.mark.parametrize("direction,expected", [("up", "up"), ("down", "down")])
    def test_a_named_direction_is_carried(self, direction, expected):
        """The control: both directions work, so the refusal above is about
        the absent one rather than about zoom being broken."""
        action = self._actions(
            ["--scroll", direction, "--scroll-at", "10,20", "--scroll-zoom"],
        )[0]
        assert action["direction"] == expected
        assert action["modifier"] == "ctrl"

    def test_a_plain_point_scroll_still_defaults_to_down(self):
        """The other control, and the asymmetry stated: only zoom requires it."""
        assert self._actions(["--scroll-at", "10,20"])[0]["direction"] == "down"


class TestAHandBuiltClickCount:
    """argv cannot produce these; `_interact_actions` is called directly too.

    The skill's count check exists to save a round trip, so a value the
    container would refuse has to be refused here — and as a `ValueError`,
    which is what the CLI's envelope machinery turns into an error answer. A
    `TypeError` out of the comparison would escape as an unhandled exception.
    """

    @pytest.mark.parametrize("value", [True, False, "3", 3.5, None.__class__])
    def test_a_non_integer_count_raises_the_envelope_s_own_error(self, value):
        args = _interact_namespace(scroll_at=["10,20"], scroll_clicks=value)
        args.action_order = [("scroll_at", 0)]
        with pytest.raises(ValueError, match="--scroll-clicks"):
            _interact_actions(args)

    def test_a_whole_number_in_range_is_carried(self):
        """The control: the type guard must not refuse the ordinary value."""
        args = _interact_namespace(scroll_at=["10,20"], scroll_clicks=7)
        args.action_order = [("scroll_at", 0)]
        assert _interact_actions(args)[0]["clicks"] == 7


class TestTheCaptureRequirementIsStatedWhereItIsMet:
    """ISSUE-537: `--click-challenge`'s help said the opposite of the rule.

    "Takes no coordinate -- the container measures the widget itself" reads as
    "no capture needed", and the refusal that follows (`no_capture`) is the
    first thing on the whole path that mentions one. `skill.md` had it right
    and the flag's own help did not, which is the copy a caller reaching for
    `--help` actually sees.

    Asserting on the help *string* rather than on behaviour, deliberately:
    there is no behaviour here to assert on. The requirement lives in the
    container, and what was wrong was the sentence.
    """

    def _help(self, option):
        parser = build_parser()
        interact = parser._subparsers._group_actions[0].choices["interact"]
        action = next(
            a for a in interact._actions if option in a.option_strings
        )
        return " ".join((action.help or "").split())

    def test_click_challenge_names_the_screenshot_it_needs(self):
        help_text = self._help("--click-challenge")
        assert "screenshot of this session on record" in help_text

    def test_click_at_still_names_it_too(self):
        """The control. `--click-at` is the flag whose help was already right,
        and the phrase asserted above is the one it uses -- so a change that
        reworded both would turn this red rather than passing quietly."""
        assert "screenshot of this session on record" in self._help("--click-at")

    def test_the_challenge_verb_says_what_a_null_screen_point_means(self):
        """The other half: `challenge` is the verb `--click-challenge`'s help
        sends you to first, and it said nothing about the capture at all."""
        parser = build_parser()
        challenge = parser._subparsers._group_actions[0].choices["challenge"]
        description = " ".join((challenge.description or "").split())
        assert "no_capture" in description
        assert "browse screenshot --session" in description


class TestTheForegroundNoteOnACapture:
    """ISSUE-536: a capture now brings its own tab forward and says so.

    The verdict cannot be a field — the body of that response is a PNG — so it
    rides on a header, and the skill turns it into a note beside the capture
    note. Confirmed says nothing, which is why the absent case is asserted as
    carefully as the present one: a container predating the fix also sends no
    header, and the two are deliberately indistinguishable from here.
    """

    def test_a_confirmed_switch_adds_no_note(self):
        assert _foreground_note_from_response({}) is None

    def test_an_empty_header_is_not_a_verdict(self):
        """httpx returns "" for a header the container set to nothing, and an
        empty code in a note reads as a fault with no name."""
        assert _foreground_note_from_response({"X-Browse-Foreground": "  "}) is None

    def test_an_unconfirmed_switch_names_the_code(self):
        note = _foreground_note_from_response(
            {"X-Browse-Foreground": "foreground_unconfirmed"}
        )
        assert "foreground_unconfirmed" in note
        assert "may show another tab" in note

    def test_the_detail_rides_along_when_there_is_one(self):
        note = _foreground_note_from_response({
            "X-Browse-Foreground": "foreground_ambiguous",
            "X-Browse-Foreground-Detail": "another open tab carries the same title",
        })
        assert "another open tab carries the same title" in note

    def test_a_detail_with_no_code_is_not_a_note_on_its_own(self):
        """The code is the verdict. A detail without one is a container
        sending half an answer, and inventing a verdict for it would report an
        unconfirmed switch that was never reported."""
        assert _foreground_note_from_response(
            {"X-Browse-Foreground-Detail": "something"}
        ) is None
