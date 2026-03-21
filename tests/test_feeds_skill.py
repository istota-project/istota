"""Tests for the feeds skill CLI (Miniflux API client)."""

import json
from unittest.mock import MagicMock, patch

import httpx
import pytest

from istota.skills.feeds import (
    build_parser,
    cmd_add,
    cmd_categories,
    cmd_entries,
    cmd_list,
    cmd_refresh,
    cmd_remove,
)


def _mock_client_response(json_data, status_code=200):
    """Build a mock httpx.Response."""
    return httpx.Response(
        status_code,
        json=json_data,
        request=httpx.Request("GET", "https://flux.test/v1/feeds"),
    )


class TestFeedsList:
    @patch("istota.skills.feeds._client")
    def test_list_feeds(self, mock_client_fn, capsys):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get.return_value = _mock_client_response([
            {
                "id": 1,
                "title": "Example Blog",
                "feed_url": "https://example.com/feed.xml",
                "site_url": "https://example.com",
                "category": {"title": "Tech"},
                "parsing_error_count": 0,
            }
        ])
        mock_client_fn.return_value = client

        parser = build_parser()
        args = parser.parse_args(["list"])
        cmd_list(args)

        output = json.loads(capsys.readouterr().out)
        assert len(output) == 1
        assert output[0]["id"] == 1
        assert output[0]["title"] == "Example Blog"
        assert output[0]["category"] == "Tech"


class TestFeedsAdd:
    @patch("istota.skills.feeds._client")
    def test_add_feed_no_category(self, mock_client_fn, capsys):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.post.return_value = _mock_client_response({"feed_id": 5})
        mock_client_fn.return_value = client

        parser = build_parser()
        args = parser.parse_args(["add", "--url", "https://example.com/feed.xml"])
        cmd_add(args)

        output = json.loads(capsys.readouterr().out)
        assert output["feed_id"] == 5
        client.post.assert_called_once_with("/v1/feeds", json={"feed_url": "https://example.com/feed.xml"})

    @patch("istota.skills.feeds._client")
    def test_add_feed_with_existing_category(self, mock_client_fn, capsys):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get.return_value = _mock_client_response([
            {"id": 10, "title": "Tech"},
        ])
        client.post.return_value = _mock_client_response({"feed_id": 6})
        mock_client_fn.return_value = client

        parser = build_parser()
        args = parser.parse_args(["add", "--url", "https://example.com/feed.xml", "--category", "Tech"])
        cmd_add(args)

        # Should not create a new category
        calls = client.post.call_args_list
        assert len(calls) == 1
        assert calls[0][0] == ("/v1/feeds",)
        assert calls[0][1]["json"]["category_id"] == 10


class TestFeedsRemove:
    @patch("istota.skills.feeds._client")
    def test_remove_feed(self, mock_client_fn, capsys):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.delete.return_value = _mock_client_response(None, 204)
        mock_client_fn.return_value = client

        parser = build_parser()
        args = parser.parse_args(["remove", "--id", "5"])
        cmd_remove(args)

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert output["removed_feed_id"] == 5


class TestFeedsCategories:
    @patch("istota.skills.feeds._client")
    def test_list_categories(self, mock_client_fn, capsys):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get.return_value = _mock_client_response([
            {"id": 1, "title": "Tech"},
            {"id": 2, "title": "Art"},
        ])
        mock_client_fn.return_value = client

        parser = build_parser()
        args = parser.parse_args(["categories"])
        cmd_categories(args)

        output = json.loads(capsys.readouterr().out)
        assert len(output) == 2


class TestFeedsEntries:
    @patch("istota.skills.feeds._client")
    def test_entries_default(self, mock_client_fn, capsys):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get.return_value = _mock_client_response({
            "total": 1,
            "entries": [{
                "id": 100,
                "title": "Article",
                "url": "https://example.com/article",
                "author": "Bob",
                "status": "unread",
                "published_at": "2025-01-15T12:00:00Z",
                "feed": {"title": "Blog"},
            }],
        })
        mock_client_fn.return_value = client

        parser = build_parser()
        args = parser.parse_args(["entries"])
        cmd_entries(args)

        output = json.loads(capsys.readouterr().out)
        assert output["total"] == 1
        assert output["entries"][0]["title"] == "Article"

    @patch("istota.skills.feeds._client")
    def test_entries_with_filters(self, mock_client_fn, capsys):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get.return_value = _mock_client_response({"total": 0, "entries": []})
        mock_client_fn.return_value = client

        parser = build_parser()
        args = parser.parse_args(["entries", "--feed-id", "5", "--status", "unread", "--limit", "10", "--search", "python"])
        cmd_entries(args)

        call_kwargs = client.get.call_args
        params = call_kwargs[1]["params"]
        assert params["feed_id"] == 5
        assert params["status"] == "unread"
        assert params["limit"] == 10
        assert params["search"] == "python"


class TestFeedsRefresh:
    @patch("istota.skills.feeds._client")
    def test_refresh_all(self, mock_client_fn, capsys):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.put.return_value = _mock_client_response(None, 204)
        mock_client_fn.return_value = client

        parser = build_parser()
        args = parser.parse_args(["refresh"])
        cmd_refresh(args)

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        client.put.assert_called_once_with("/v1/feeds/refresh")

    @patch("istota.skills.feeds._client")
    def test_refresh_specific(self, mock_client_fn, capsys):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.put.return_value = _mock_client_response(None, 204)
        mock_client_fn.return_value = client

        parser = build_parser()
        args = parser.parse_args(["refresh", "--feed-id", "3"])
        cmd_refresh(args)

        client.put.assert_called_once_with("/v1/feeds/3/refresh")


class TestParser:
    def test_all_subcommands(self):
        parser = build_parser()
        for cmd in ["list", "categories", "refresh"]:
            args = parser.parse_args([cmd])
            assert args.command == cmd

    def test_add_requires_url(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["add"])

    def test_remove_requires_id(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["remove"])
