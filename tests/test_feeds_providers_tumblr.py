"""Tests for the Tumblr API v2 provider.

Network is stubbed at ``requests.get``; these exercise NPF block walking
and the within-post image de-duplication (ISSUE-162): a reblog carries the
same photo in both the post's own ``content`` and the reblog ``trail``, so
a naive walk lists it twice.
"""

from __future__ import annotations

import pytest

from istota.feeds.providers import tumblr as tumblr_provider


class _StubResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


@pytest.fixture
def stub_get(monkeypatch):
    """Patch ``requests.get`` in the provider; return a setter for the body."""
    captured: dict = {}

    def _install(payload: dict):
        def _get(url, params=None, timeout=None):
            captured["url"] = url
            captured["params"] = params
            return _StubResponse(payload)

        monkeypatch.setattr(tumblr_provider.requests, "get", _get)
        return captured

    return _install


def _image_block(url: str) -> dict:
    return {"type": "image", "media": [{"url": url, "width": 500}]}


def _posts(*posts: dict) -> dict:
    return {"response": {"posts": list(posts)}}


class TestFetchImageDedup:
    def test_same_url_in_content_and_trail_appears_once(self, stub_get):
        img = "https://64.media.tumblr.com/aaa/bbb-01/s500x750/hash.jpg"
        stub_get(_posts({
            "id": 123,
            "post_url": "https://blog.tumblr.com/post/123",
            "date": "2026-07-16 10:00:00 GMT",
            "content": [_image_block(img), {"type": "text", "text": "nice"}],
            "trail": [{"content": [_image_block(img)]}],
        }))

        items = tumblr_provider.fetch("blog", api_key="k")

        assert len(items) == 1
        assert items[0].image_urls == [img]

    def test_size_variants_of_one_image_collapse_to_the_largest(self, stub_get):
        small = "https://64.media.tumblr.com/aaa/bbb-01/s250x400/hash.jpg"
        large = "https://72.media.tumblr.com/aaa/bbb-01/s1280x1920/hash.jpg"
        stub_get(_posts({
            "id": 1,
            "content": [_image_block(small)],
            "trail": [{"content": [_image_block(large)]}],
        }))

        items = tumblr_provider.fetch("blog", api_key="k")

        assert items[0].image_urls == [large]

    def test_distinct_images_keep_order(self, stub_get):
        first = "https://64.media.tumblr.com/aaa/bbb-01/s500x750/one.jpg"
        second = "https://64.media.tumblr.com/ccc/ddd-01/s500x750/two.jpg"
        third = "https://64.media.tumblr.com/eee/fff-01/s500x750/three.jpg"
        stub_get(_posts({
            "id": 2,
            "content": [_image_block(first), _image_block(second)],
            "trail": [{"content": [_image_block(second), _image_block(third)]}],
        }))

        items = tumblr_provider.fetch("blog", api_key="k")

        assert items[0].image_urls == [first, second, third]

    def test_text_blocks_still_concatenate_across_trail(self, stub_get):
        stub_get(_posts({
            "id": 3,
            "content": [{"type": "text", "text": "my comment"}],
            "trail": [{"content": [{"type": "text", "text": "original"}]}],
        }))

        items = tumblr_provider.fetch("blog", api_key="k")

        assert items[0].content_text == "my comment\noriginal"
        assert items[0].image_urls == []


class TestFetchBasics:
    def test_requires_api_key(self, monkeypatch):
        monkeypatch.delenv("TUMBLR_API_KEY", raising=False)
        with pytest.raises(ValueError):
            tumblr_provider.fetch("blog")

    def test_maps_post_fields(self, stub_get):
        stub_get(_posts({
            "id": 42,
            "post_url": "https://blog.tumblr.com/post/42",
            "summary": "A summary",
            "date": "2026-07-16 10:00:00 GMT",
            "content": [],
        }))

        items = tumblr_provider.fetch("blog", api_key="k")

        assert items[0].guid == "42"
        assert items[0].title == "A summary"
        assert items[0].url == "https://blog.tumblr.com/post/42"
        assert items[0].author == "blog"
        assert items[0].published_at == "2026-07-16T10:00:00+00:00"


class TestPayloadValidation:
    """A response that is not a post collection must not read as "no posts".

    ``.get("response", {}).get("posts", [])`` turned every malformed payload
    into an empty list, which is indistinguishable from a blog that really has
    no posts — so a broken API reported a clean poll, cleared ``last_error``
    and kept the ordinary cadence (ISSUE-388). A valid empty list is a real
    answer and still succeeds; the two lead to the same observation state and
    to different error state.
    """

    def test_a_valid_empty_collection_succeeds(self, stub_get):
        stub_get({"response": {"posts": []}})
        assert tumblr_provider.fetch("blog", api_key="k") == []

    @pytest.mark.parametrize(
        "payload",
        [
            pytest.param({}, id="response-missing"),
            pytest.param({"response": None}, id="response-null"),
            pytest.param({"response": []}, id="response-list"),
            pytest.param({"response": "posts"}, id="response-scalar"),
            pytest.param({"response": {}}, id="posts-missing"),
            pytest.param({"response": {"posts": None}}, id="posts-null"),
            pytest.param({"response": {"posts": {}}}, id="posts-object"),
            pytest.param({"response": {"posts": 3}}, id="posts-scalar"),
            pytest.param({"response": {"posts": "none"}}, id="posts-string"),
            pytest.param([], id="payload-list"),
        ],
    )
    def test_a_malformed_collection_is_rejected(self, stub_get, payload):
        stub_get(payload)
        with pytest.raises(ValueError):
            tumblr_provider.fetch("blog", api_key="k")
