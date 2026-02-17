"""Tests for the bookmarks skill (Karakeep API client + CLI)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from istota.skills.bookmarks import (
    KarakeepClient,
    build_parser,
    cmd_add,
    cmd_get,
    cmd_list,
    cmd_list_bookmarks,
    cmd_lists,
    cmd_search,
    cmd_stats,
    cmd_summarize,
    cmd_tag,
    cmd_tags,
    cmd_untag,
    main,
)

# --- Sample API responses ---

SAMPLE_BOOKMARK_LINK = {
    "id": "bk_abc123",
    "createdAt": "2026-02-10T12:00:00Z",
    "modifiedAt": "2026-02-10T12:30:00Z",
    "title": "Example Article",
    "archived": False,
    "favourited": True,
    "taggingStatus": "success",
    "summarizationStatus": "success",
    "note": "Great read",
    "summary": "An article about examples.",
    "source": "api",
    "userId": "user_1",
    "tags": [
        {"id": "tag_1", "name": "tech", "attachedBy": "human"},
        {"id": "tag_2", "name": "reading", "attachedBy": "ai"},
    ],
    "content": {
        "type": "link",
        "url": "https://example.com/article",
        "title": "Example Article",
        "description": "A meta description",
        "imageUrl": None,
        "htmlContent": "<p>Article body</p>",
        "crawledAt": "2026-02-10T12:01:00Z",
        "crawlStatus": "success",
    },
    "assets": [],
}

SAMPLE_BOOKMARK_TEXT = {
    "id": "bk_text456",
    "createdAt": "2026-02-10T13:00:00Z",
    "modifiedAt": None,
    "title": "Quick Note",
    "archived": False,
    "favourited": False,
    "taggingStatus": None,
    "summarizationStatus": None,
    "note": None,
    "summary": None,
    "source": "api",
    "userId": "user_1",
    "tags": [],
    "content": {
        "type": "text",
        "text": "Remember to check this pattern later.",
        "sourceUrl": None,
    },
    "assets": [],
}

SAMPLE_PAGINATED_BOOKMARKS = {
    "bookmarks": [SAMPLE_BOOKMARK_LINK, SAMPLE_BOOKMARK_TEXT],
    "nextCursor": None,
}

SAMPLE_PAGINATED_BOOKMARKS_WITH_CURSOR = {
    "bookmarks": [SAMPLE_BOOKMARK_LINK],
    "nextCursor": "cursor_page2",
}

SAMPLE_PAGINATED_BOOKMARKS_PAGE2 = {
    "bookmarks": [SAMPLE_BOOKMARK_TEXT],
    "nextCursor": None,
}

SAMPLE_TAG = {
    "id": "tag_1",
    "name": "tech",
    "numBookmarks": 42,
    "numBookmarksByAttachedType": {"ai": 10, "human": 32},
}

SAMPLE_TAGS_RESPONSE = {
    "tags": [SAMPLE_TAG, {"id": "tag_2", "name": "reading", "numBookmarks": 15, "numBookmarksByAttachedType": {"ai": 5, "human": 10}}],
    "nextCursor": None,
}

SAMPLE_LIST = {
    "id": "list_1",
    "name": "Reading List",
    "description": "Things to read",
    "icon": "📚",
    "parentId": None,
    "type": "manual",
    "query": None,
    "public": False,
    "hasCollaborators": False,
    "userRole": "owner",
}

SAMPLE_LISTS_RESPONSE = {
    "lists": [SAMPLE_LIST],
}

SAMPLE_STATS_RESPONSE = {
    "numBookmarks": 500,
    "numFavorites": 25,
    "numArchived": 50,
    "numTags": 80,
    "numLists": 5,
    "numHighlights": 12,
    "bookmarksByType": {"link": 450, "text": 40, "asset": 10},
    "topDomains": [
        {"domain": "github.com", "count": 100},
        {"domain": "arxiv.org", "count": 30},
    ],
    "totalAssetSize": 1048576,
    "assetsByType": [{"type": "screenshot", "count": 50, "totalSize": 524288}],
    "bookmarkingActivity": {
        "thisWeek": 10,
        "thisMonth": 35,
        "thisYear": 200,
        "byHour": [{"hour": 10, "count": 50}],
        "byDayOfWeek": [{"day": 1, "count": 80}],
    },
    "tagUsage": [{"name": "tech", "count": 100}],
    "bookmarksBySource": [{"source": "extension", "count": 300}],
}

SAMPLE_TAG_ATTACH_RESPONSE = {
    "attached": ["tag_1", "tag_3"],
}

SAMPLE_TAG_DETACH_RESPONSE = {
    "detached": ["tag_2"],
}


# --- Client Tests ---


class TestKarakeepClientInit:
    def test_init(self):
        client = KarakeepClient("https://keep.example.com/api/v1", "test_key_123")
        assert client.base_url == "https://keep.example.com/api/v1"
        assert client.api_key == "test_key_123"

    def test_init_strips_trailing_slash(self):
        client = KarakeepClient("https://keep.example.com/api/v1/", "key")
        assert client.base_url == "https://keep.example.com/api/v1"


class TestKarakeepClientRequest:
    @patch("istota.skills.bookmarks.httpx.request")
    def test_request_adds_auth_header(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"bookmarks": [], "nextCursor": None}
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "my_api_key")
        client._request("GET", "/bookmarks")

        mock_request.assert_called_once()
        call_kwargs = mock_request.call_args
        headers = call_kwargs[1]["headers"]
        assert headers["Authorization"] == "Bearer my_api_key"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_request_builds_full_url(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        client._request("GET", "/bookmarks/bk_123")

        call_args = mock_request.call_args
        assert call_args[0][0] == "GET"
        assert call_args[0][1] == "https://keep.example.com/api/v1/bookmarks/bk_123"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_request_raises_on_error(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 404
        mock_resp.json.return_value = {"code": "not_found", "message": "Bookmark not found"}
        mock_resp.raise_for_status.side_effect = Exception("404 Not Found")
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        with pytest.raises(Exception, match="404"):
            client._request("GET", "/bookmarks/nonexistent")


class TestKarakeepClientSearch:
    @patch("istota.skills.bookmarks.httpx.request")
    def test_search_basic(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_PAGINATED_BOOKMARKS
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        results = client.search("example")

        assert len(results) == 2
        assert results[0]["id"] == "bk_abc123"
        call_kwargs = mock_request.call_args[1]
        assert call_kwargs["params"]["q"] == "example"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_search_with_options(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_PAGINATED_BOOKMARKS
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        client.search("test", limit=5, sort="desc")

        call_kwargs = mock_request.call_args[1]
        assert call_kwargs["params"]["q"] == "test"
        assert call_kwargs["params"]["limit"] == 5
        assert call_kwargs["params"]["sortOrder"] == "desc"


class TestKarakeepClientBookmarks:
    @patch("istota.skills.bookmarks.httpx.request")
    def test_get_bookmark(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_BOOKMARK_LINK
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        result = client.get_bookmark("bk_abc123")

        assert result["id"] == "bk_abc123"
        assert result["title"] == "Example Article"
        assert result["content"]["type"] == "link"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_list_bookmarks(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_PAGINATED_BOOKMARKS
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        results = client.list_bookmarks(limit=20)

        assert len(results) == 2

    @patch("istota.skills.bookmarks.httpx.request")
    def test_list_bookmarks_favourited(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_PAGINATED_BOOKMARKS
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        client.list_bookmarks(favourited=True)

        call_kwargs = mock_request.call_args[1]
        assert call_kwargs["params"]["favourited"] is True

    @patch("istota.skills.bookmarks.httpx.request")
    def test_create_link_bookmark(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = SAMPLE_BOOKMARK_LINK
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        result = client.create_bookmark(url="https://example.com/article")

        assert result["id"] == "bk_abc123"
        call_kwargs = mock_request.call_args[1]
        body = call_kwargs["json"]
        assert body["type"] == "link"
        assert body["url"] == "https://example.com/article"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_create_text_bookmark(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = SAMPLE_BOOKMARK_TEXT
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        result = client.create_bookmark(text="A note to self")

        call_kwargs = mock_request.call_args[1]
        body = call_kwargs["json"]
        assert body["type"] == "text"
        assert body["text"] == "A note to self"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_create_bookmark_with_title_and_note(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 201
        mock_resp.json.return_value = SAMPLE_BOOKMARK_LINK
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        client.create_bookmark(url="https://example.com", title="My Title", note="My note")

        call_kwargs = mock_request.call_args[1]
        body = call_kwargs["json"]
        assert body["title"] == "My Title"
        assert body["note"] == "My note"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_update_bookmark(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "bk_abc123", "title": "Updated Title",
            "archived": False, "favourited": True,
        }
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        result = client.update_bookmark("bk_abc123", title="Updated Title")

        assert result["title"] == "Updated Title"
        call_kwargs = mock_request.call_args[1]
        assert call_kwargs["json"]["title"] == "Updated Title"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_delete_bookmark(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 204
        mock_resp.text = ""
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        result = client.delete_bookmark("bk_abc123")

        assert result["status"] == "ok"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_summarize(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "id": "bk_abc123",
            "summarizationStatus": "pending",
        }
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        result = client.summarize("bk_abc123")

        assert result["id"] == "bk_abc123"


class TestKarakeepClientTags:
    @patch("istota.skills.bookmarks.httpx.request")
    def test_list_tags(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_TAGS_RESPONSE
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        tags = client.list_tags()

        assert len(tags) == 2
        assert tags[0]["name"] == "tech"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_list_tags_with_filter(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_TAGS_RESPONSE
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        client.list_tags(name_contains="tech")

        call_kwargs = mock_request.call_args[1]
        assert call_kwargs["params"]["nameContains"] == "tech"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_tag_bookmark(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_TAG_ATTACH_RESPONSE
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        result = client.tag_bookmark("bk_abc123", ["tech", "new-tag"])

        assert result["attached"] == ["tag_1", "tag_3"]
        call_kwargs = mock_request.call_args[1]
        body = call_kwargs["json"]
        assert len(body["tags"]) == 2
        assert body["tags"][0]["tagName"] == "tech"
        assert body["tags"][1]["tagName"] == "new-tag"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_untag_bookmark(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_TAG_DETACH_RESPONSE
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        result = client.untag_bookmark("bk_abc123", ["reading"])

        assert result["detached"] == ["tag_2"]

    @patch("istota.skills.bookmarks.httpx.request")
    def test_get_bookmarks_by_tag(self, mock_request):
        # First call: list tags to find tag ID
        # Second call: get bookmarks by tag ID
        tag_resp = MagicMock()
        tag_resp.status_code = 200
        tag_resp.json.return_value = SAMPLE_TAGS_RESPONSE

        bm_resp = MagicMock()
        bm_resp.status_code = 200
        bm_resp.json.return_value = SAMPLE_PAGINATED_BOOKMARKS

        mock_request.side_effect = [tag_resp, bm_resp]

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        results = client.get_bookmarks_by_tag("tech", limit=10)

        assert len(results) == 2
        # Second call should be to /tags/tag_1/bookmarks
        second_call = mock_request.call_args_list[1]
        assert "/tags/tag_1/bookmarks" in second_call[0][1]


class TestKarakeepClientLists:
    @patch("istota.skills.bookmarks.httpx.request")
    def test_list_lists(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_LISTS_RESPONSE
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        lists = client.list_lists()

        assert len(lists) == 1
        assert lists[0]["name"] == "Reading List"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_get_list_bookmarks(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_PAGINATED_BOOKMARKS
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        results = client.get_list_bookmarks("list_1", limit=20)

        assert len(results) == 2

    @patch("istota.skills.bookmarks.httpx.request")
    def test_get_list_by_name_found(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_LISTS_RESPONSE
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        result = client.get_list_by_name("Reading List")

        assert result is not None
        assert result["id"] == "list_1"
        assert result["name"] == "Reading List"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_get_list_by_name_case_insensitive(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_LISTS_RESPONSE
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        result = client.get_list_by_name("reading list")

        assert result is not None
        assert result["id"] == "list_1"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_get_list_by_name_not_found(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_LISTS_RESPONSE
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        result = client.get_list_by_name("Nonexistent List")

        assert result is None


class TestKarakeepClientPagination:
    @patch("istota.skills.bookmarks.httpx.request")
    def test_pagination_follows_cursor(self, mock_request):
        page1_resp = MagicMock()
        page1_resp.status_code = 200
        page1_resp.json.return_value = SAMPLE_PAGINATED_BOOKMARKS_WITH_CURSOR

        page2_resp = MagicMock()
        page2_resp.status_code = 200
        page2_resp.json.return_value = SAMPLE_PAGINATED_BOOKMARKS_PAGE2

        mock_request.side_effect = [page1_resp, page2_resp]

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        results = client.list_bookmarks(limit=50)

        assert len(results) == 2
        assert mock_request.call_count == 2
        # Second call should include cursor
        second_call_params = mock_request.call_args_list[1][1]["params"]
        assert second_call_params["cursor"] == "cursor_page2"

    @patch("istota.skills.bookmarks.httpx.request")
    def test_pagination_respects_limit(self, mock_request):
        """When limit is reached, don't fetch more pages."""
        page1_resp = MagicMock()
        page1_resp.status_code = 200
        page1_resp.json.return_value = SAMPLE_PAGINATED_BOOKMARKS_WITH_CURSOR
        mock_request.return_value = page1_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        results = client.list_bookmarks(limit=1)

        # Should only fetch one page since we got enough items
        assert len(results) == 1
        assert mock_request.call_count == 1


class TestKarakeepClientStats:
    @patch("istota.skills.bookmarks.httpx.request")
    def test_stats(self, mock_request):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = SAMPLE_STATS_RESPONSE
        mock_request.return_value = mock_resp

        client = KarakeepClient("https://keep.example.com/api/v1", "key")
        stats = client.stats()

        assert stats["numBookmarks"] == 500
        assert stats["numTags"] == 80


# --- CLI / Parser Tests ---


class TestBuildParser:
    def test_search_command(self):
        parser = build_parser()
        args = parser.parse_args(["search", "machine learning"])
        assert args.command == "search"
        assert args.query == "machine learning"

    def test_search_with_options(self):
        parser = build_parser()
        args = parser.parse_args(["search", "test", "--limit", "5", "--sort", "desc"])
        assert args.limit == 5
        assert args.sort == "desc"

    def test_list_command(self):
        parser = build_parser()
        args = parser.parse_args(["list"])
        assert args.command == "list"
        assert args.limit == 20
        assert args.favourited is False
        assert args.archived is False

    def test_list_favourited(self):
        parser = build_parser()
        args = parser.parse_args(["list", "--favourited"])
        assert args.favourited is True

    def test_list_with_tag(self):
        parser = build_parser()
        args = parser.parse_args(["list", "--tag", "programming"])
        assert args.tag == "programming"

    def test_list_with_list_name(self):
        parser = build_parser()
        args = parser.parse_args(["list", "--in-list", "Read Later"])
        assert args.in_list == "Read Later"

    def test_get_command(self):
        parser = build_parser()
        args = parser.parse_args(["get", "bk_abc123"])
        assert args.command == "get"
        assert args.bookmark_id == "bk_abc123"
        assert args.include_content is False

    def test_get_with_content(self):
        parser = build_parser()
        args = parser.parse_args(["get", "bk_abc123", "--include-content"])
        assert args.include_content is True

    def test_add_url(self):
        parser = build_parser()
        args = parser.parse_args(["add", "https://example.com"])
        assert args.command == "add"
        assert args.url_or_text == "https://example.com"
        assert args.text is False

    def test_add_text(self):
        parser = build_parser()
        args = parser.parse_args(["add", "A quick note", "--text"])
        assert args.text is True
        assert args.url_or_text == "A quick note"

    def test_add_with_tags_and_title(self):
        parser = build_parser()
        args = parser.parse_args([
            "add", "https://example.com",
            "--title", "Great Article",
            "--tags", "tech,reading",
            "--note", "Must read",
        ])
        assert args.title == "Great Article"
        assert args.tags == "tech,reading"
        assert args.note == "Must read"

    def test_tags_command(self):
        parser = build_parser()
        args = parser.parse_args(["tags"])
        assert args.command == "tags"

    def test_tags_with_search(self):
        parser = build_parser()
        args = parser.parse_args(["tags", "--search", "prog"])
        assert args.search == "prog"

    def test_tag_command(self):
        parser = build_parser()
        args = parser.parse_args(["tag", "bk_abc123", "tech,reading"])
        assert args.command == "tag"
        assert args.bookmark_id == "bk_abc123"
        assert args.tag_names == "tech,reading"

    def test_untag_command(self):
        parser = build_parser()
        args = parser.parse_args(["untag", "bk_abc123", "oldtag"])
        assert args.command == "untag"
        assert args.bookmark_id == "bk_abc123"
        assert args.tag_names == "oldtag"

    def test_lists_command(self):
        parser = build_parser()
        args = parser.parse_args(["lists"])
        assert args.command == "lists"

    def test_list_bookmarks_command(self):
        parser = build_parser()
        args = parser.parse_args(["list-bookmarks", "list_1"])
        assert args.command == "list-bookmarks"
        assert args.list_id == "list_1"

    def test_summarize_command(self):
        parser = build_parser()
        args = parser.parse_args(["summarize", "bk_abc123"])
        assert args.command == "summarize"
        assert args.bookmark_id == "bk_abc123"

    def test_stats_command(self):
        parser = build_parser()
        args = parser.parse_args(["stats"])
        assert args.command == "stats"


# --- Command Handler Tests ---


class TestCmdSearch:
    @patch("istota.skills.bookmarks.get_client")
    def test_search(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.search.return_value = [SAMPLE_BOOKMARK_LINK]
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["search", "example"])
        result = cmd_search(args)

        assert result["status"] == "ok"
        assert result["count"] == 1
        assert result["bookmarks"][0]["id"] == "bk_abc123"
        assert result["bookmarks"][0]["title"] == "Example Article"
        assert result["bookmarks"][0]["url"] == "https://example.com/article"
        assert result["bookmarks"][0]["tags"] == ["tech", "reading"]


class TestCmdList:
    @patch("istota.skills.bookmarks.get_client")
    def test_list_default(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.list_bookmarks.return_value = [SAMPLE_BOOKMARK_LINK, SAMPLE_BOOKMARK_TEXT]
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["list"])
        result = cmd_list(args)

        assert result["status"] == "ok"
        assert result["count"] == 2

    @patch("istota.skills.bookmarks.get_client")
    def test_list_by_tag(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_bookmarks_by_tag.return_value = [SAMPLE_BOOKMARK_LINK]
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["list", "--tag", "tech"])
        result = cmd_list(args)

        assert result["status"] == "ok"
        mock_client.get_bookmarks_by_tag.assert_called_once_with("tech", limit=20)

    @patch("istota.skills.bookmarks.get_client")
    def test_list_by_list_name(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_list_by_name.return_value = SAMPLE_LIST
        mock_client.get_list_bookmarks.return_value = [SAMPLE_BOOKMARK_LINK]
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["list", "--in-list", "Reading List"])
        result = cmd_list(args)

        assert result["status"] == "ok"
        assert result["count"] == 1
        mock_client.get_list_by_name.assert_called_once_with("Reading List")
        mock_client.get_list_bookmarks.assert_called_once_with("list_1", limit=20)

    @patch("istota.skills.bookmarks.get_client")
    def test_list_by_list_name_not_found(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_list_by_name.return_value = None
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["list", "--in-list", "Nonexistent"])
        result = cmd_list(args)

        assert result["status"] == "error"
        assert "not found" in result["error"]


class TestCmdGet:
    @patch("istota.skills.bookmarks.get_client")
    def test_get(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_bookmark.return_value = SAMPLE_BOOKMARK_LINK
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["get", "bk_abc123"])
        result = cmd_get(args)

        assert result["status"] == "ok"
        assert result["bookmark"]["id"] == "bk_abc123"


class TestCmdAdd:
    @patch("istota.skills.bookmarks.get_client")
    def test_add_url(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.create_bookmark.return_value = SAMPLE_BOOKMARK_LINK
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["add", "https://example.com/article"])
        result = cmd_add(args)

        assert result["status"] == "ok"
        mock_client.create_bookmark.assert_called_once()
        call_kwargs = mock_client.create_bookmark.call_args[1]
        assert call_kwargs["url"] == "https://example.com/article"

    @patch("istota.skills.bookmarks.get_client")
    def test_add_text(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.create_bookmark.return_value = SAMPLE_BOOKMARK_TEXT
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["add", "A note", "--text"])
        result = cmd_add(args)

        assert result["status"] == "ok"
        call_kwargs = mock_client.create_bookmark.call_args[1]
        assert call_kwargs["text"] == "A note"
        assert "url" not in call_kwargs

    @patch("istota.skills.bookmarks.get_client")
    def test_add_with_tags(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.create_bookmark.return_value = SAMPLE_BOOKMARK_LINK
        mock_client.tag_bookmark.return_value = SAMPLE_TAG_ATTACH_RESPONSE
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["add", "https://example.com", "--tags", "tech,reading"])
        result = cmd_add(args)

        assert result["status"] == "ok"
        mock_client.tag_bookmark.assert_called_once_with("bk_abc123", ["tech", "reading"])


class TestCmdTags:
    @patch("istota.skills.bookmarks.get_client")
    def test_list_tags(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.list_tags.return_value = [SAMPLE_TAG]
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["tags"])
        result = cmd_tags(args)

        assert result["status"] == "ok"
        assert result["tags"][0]["name"] == "tech"
        assert result["tags"][0]["count"] == 42


class TestCmdTag:
    @patch("istota.skills.bookmarks.get_client")
    def test_tag(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.tag_bookmark.return_value = SAMPLE_TAG_ATTACH_RESPONSE
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["tag", "bk_abc123", "tech,new-tag"])
        result = cmd_tag(args)

        assert result["status"] == "ok"
        mock_client.tag_bookmark.assert_called_once_with("bk_abc123", ["tech", "new-tag"])


class TestCmdUntag:
    @patch("istota.skills.bookmarks.get_client")
    def test_untag(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.untag_bookmark.return_value = SAMPLE_TAG_DETACH_RESPONSE
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["untag", "bk_abc123", "reading"])
        result = cmd_untag(args)

        assert result["status"] == "ok"
        mock_client.untag_bookmark.assert_called_once_with("bk_abc123", ["reading"])


class TestCmdLists:
    @patch("istota.skills.bookmarks.get_client")
    def test_lists(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.list_lists.return_value = [SAMPLE_LIST]
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["lists"])
        result = cmd_lists(args)

        assert result["status"] == "ok"
        assert result["lists"][0]["name"] == "Reading List"


class TestCmdListBookmarks:
    @patch("istota.skills.bookmarks.get_client")
    def test_list_bookmarks(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.get_list_bookmarks.return_value = [SAMPLE_BOOKMARK_LINK]
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["list-bookmarks", "list_1"])
        result = cmd_list_bookmarks(args)

        assert result["status"] == "ok"
        assert result["count"] == 1


class TestCmdSummarize:
    @patch("istota.skills.bookmarks.get_client")
    def test_summarize(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.summarize.return_value = {"id": "bk_abc123", "summarizationStatus": "pending"}
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["summarize", "bk_abc123"])
        result = cmd_summarize(args)

        assert result["status"] == "ok"


class TestCmdStats:
    @patch("istota.skills.bookmarks.get_client")
    def test_stats(self, mock_get_client):
        mock_client = MagicMock()
        mock_client.stats.return_value = SAMPLE_STATS_RESPONSE
        mock_get_client.return_value = mock_client

        parser = build_parser()
        args = parser.parse_args(["stats"])
        result = cmd_stats(args)

        assert result["status"] == "ok"
        assert result["stats"]["numBookmarks"] == 500


# --- Main / Integration Tests ---


class TestMain:
    @patch("istota.skills.bookmarks.get_client")
    def test_main_outputs_json(self, mock_get_client, capsys):
        mock_client = MagicMock()
        mock_client.stats.return_value = SAMPLE_STATS_RESPONSE
        mock_get_client.return_value = mock_client

        main(["stats"])

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "ok"

    @patch("istota.skills.bookmarks.get_client")
    def test_main_error_output(self, mock_get_client, capsys):
        mock_client = MagicMock()
        mock_client.search.side_effect = Exception("Connection refused")
        mock_get_client.return_value = mock_client

        with pytest.raises(SystemExit) as exc_info:
            main(["search", "test"])
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "error"
        assert "Connection refused" in output["error"]

    def test_main_missing_env(self, capsys, monkeypatch):
        monkeypatch.delenv("KARAKEEP_API_KEY", raising=False)
        monkeypatch.delenv("KARAKEEP_BASE_URL", raising=False)

        with pytest.raises(SystemExit) as exc_info:
            main(["stats"])
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        output = json.loads(captured.out)
        assert output["status"] == "error"


# --- Bookmark formatting helper ---


class TestFormatBookmark:
    def test_format_link_bookmark(self):
        from istota.skills.bookmarks import format_bookmark
        result = format_bookmark(SAMPLE_BOOKMARK_LINK)

        assert result["id"] == "bk_abc123"
        assert result["title"] == "Example Article"
        assert result["url"] == "https://example.com/article"
        assert result["tags"] == ["tech", "reading"]
        assert result["favourited"] is True
        assert result["summary"] == "An article about examples."

    def test_format_text_bookmark(self):
        from istota.skills.bookmarks import format_bookmark
        result = format_bookmark(SAMPLE_BOOKMARK_TEXT)

        assert result["id"] == "bk_text456"
        assert result["title"] == "Quick Note"
        assert "url" not in result or result.get("url") is None
        assert result["tags"] == []

    def test_format_bookmark_no_content(self):
        """Bookmarks fetched without includeContent still format."""
        from istota.skills.bookmarks import format_bookmark
        bm = {**SAMPLE_BOOKMARK_LINK, "content": {"type": "unknown"}}
        result = format_bookmark(bm)
        assert result["id"] == "bk_abc123"
