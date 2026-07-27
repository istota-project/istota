"""Tests for the native feeds FastAPI router.

Uses ``fastapi.testclient.TestClient`` against a minimal app that mounts
``istota.feeds.routes.router`` and overrides the auth + context
dependencies to inject a tmp-path-backed FeedsContext. This mirrors how
``web_app.py`` mounts the router under the native backend.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.feeds import db as feeds_db
from istota.feeds.models import EntryRecord, FeedsContext
from istota.feeds.routes import (
    get_user_context,
    require_auth,
    router,
)
from istota.feeds.workspace import synthesize_feeds_context


def _seed(ctx: FeedsContext) -> dict:
    """Seed a minimal feeds DB; return ids for assertions."""
    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        cat_id = feeds_db.upsert_category(conn, "tumblr", "Tumblr")
        feed_id = feeds_db.upsert_feed(
            conn,
            url="tumblr:nemfrog",
            title="Nemfrog",
            site_url="https://nemfrog.tumblr.com",
            source_type="tumblr",
            category_id=cat_id,
            poll_interval_minutes=30,
        )
        rss_feed_id = feeds_db.upsert_feed(
            conn,
            url="https://example.com/feed.xml",
            title="Example Blog",
            site_url="https://example.com",
            source_type="rss",
            category_id=None,
            poll_interval_minutes=30,
        )
        feeds_db.insert_entries(conn, feed_id, [
            EntryRecord(
                id=0, feed_id=feed_id, guid="post-1", title="Post One",
                url="https://nemfrog.tumblr.com/post/1", author=None,
                content_html="<p>hello world</p>", content_text="hello world",
                image_urls=["https://img.example.com/a.jpg"],
                published_at="2026-05-01T10:00:00+00:00",
                fetched_at="2026-05-02T00:00:00+00:00",
                status="unread",
            ),
            EntryRecord(
                id=0, feed_id=feed_id, guid="post-2", title="Post Two",
                url="https://nemfrog.tumblr.com/post/2", author=None,
                content_html="<p>second</p>", content_text="second",
                image_urls=[], published_at="2026-04-30T10:00:00+00:00",
                fetched_at="2026-05-02T00:00:00+00:00",
                status="read",
            ),
        ])
        feeds_db.insert_entries(conn, rss_feed_id, [
            EntryRecord(
                id=0, feed_id=rss_feed_id, guid="rss-1", title="RSS One",
                url="https://example.com/post/1", author="Alice",
                content_html="<p>rss</p>", content_text="rss", image_urls=[],
                published_at="2026-05-02T08:00:00+00:00",
                fetched_at="2026-05-02T09:00:00+00:00",
                status="unread",
            ),
        ])
        conn.commit()
    return {"cat_id": cat_id, "tumblr_feed_id": feed_id, "rss_feed_id": rss_feed_id}


@pytest.fixture
def ctx(tmp_path: Path) -> FeedsContext:
    c = synthesize_feeds_context("alice", tmp_path)
    c.ensure_dirs()
    feeds_db.init_db(c.db_path)
    return c


@pytest.fixture
def client(ctx: FeedsContext) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/istota/api/feeds")
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    app.dependency_overrides[get_user_context] = lambda: ctx
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /feeds — response shape consumed by the SvelteKit reader
# ---------------------------------------------------------------------------


class TestGetFeeds:
    def test_returns_feeds_entries_total(self, ctx, client):
        _seed(ctx)
        resp = client.get("/istota/api/feeds")
        assert resp.status_code == 200
        body = resp.json()
        assert set(body.keys()) == {"feeds", "entries", "total"}
        assert isinstance(body["feeds"], list)
        assert isinstance(body["entries"], list)

    def test_feed_shape(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds").json()
        # Pick the tumblr feed deterministically.
        feed = next(f for f in body["feeds"] if f["title"] == "Nemfrog")
        assert set(feed.keys()) == {"id", "title", "site_url", "category"}
        assert set(feed["category"].keys()) == {"id", "title"}
        assert feed["category"]["title"] == "Tumblr"

    def test_entry_shape(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds").json()
        entry = body["entries"][0]
        expected = {
            "id", "title", "url", "content", "images", "duplicate_image_count",
            "embed_url", "feed", "status", "starred", "starred_at",
            "published_at", "created_at",
        }
        assert set(entry.keys()) == expected
        assert set(entry["feed"].keys()) == {"id", "title", "site_url", "category"}

    def test_status_filter(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds?status=unread").json()
        assert {e["status"] for e in body["entries"]} == {"unread"}
        assert body["total"] == 2  # post-1 + rss-1

    def test_feed_id_filter(self, ctx, client):
        ids = _seed(ctx)
        body = client.get(f"/istota/api/feeds?feed_id={ids['tumblr_feed_id']}").json()
        assert all(e["feed"]["id"] == ids["tumblr_feed_id"] for e in body["entries"])
        assert body["total"] == 2  # both tumblr posts

    def test_category_id_filter(self, ctx, client):
        ids = _seed(ctx)
        body = client.get(f"/istota/api/feeds?category_id={ids['cat_id']}").json()
        # Only the tumblr feed sits under the tumblr category.
        for e in body["entries"]:
            assert e["feed"]["category"]["id"] == ids["cat_id"]

    def test_before_filter(self, ctx, client):
        _seed(ctx)
        cutoff_ts = int(
            datetime(2026, 5, 1, tzinfo=timezone.utc).timestamp()
        )
        body = client.get(f"/istota/api/feeds?before={cutoff_ts}").json()
        # Only post-2 (2026-04-30) is strictly before the 2026-05-01 cutoff.
        titles = [e["title"] for e in body["entries"]]
        assert "Post Two" in titles
        assert "Post One" not in titles
        assert "RSS One" not in titles


# ---------------------------------------------------------------------------
# PUT /feeds/entries/{id} + batch — writes hit SQLite
# ---------------------------------------------------------------------------


class TestUpdateEntries:
    def test_single_entry_marks_read(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds?status=unread").json()
        entry = next(e for e in body["entries"] if e["title"] == "Post One")

        resp = client.put(
            f"/istota/api/feeds/entries/{entry['id']}",
            json={"status": "read"},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1

        # Verify SQLite was actually mutated.
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT status FROM feed_entries WHERE id = ?", (entry["id"],),
            ).fetchone()
            assert row["status"] == "read"

    def test_batch_marks_read(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds?status=unread").json()
        ids = [e["id"] for e in body["entries"]]
        assert len(ids) == 2

        resp = client.put(
            "/istota/api/feeds/entries/batch",
            json={"entry_ids": ids, "status": "read"},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 2

        # All previously-unread entries are now read.
        body2 = client.get("/istota/api/feeds?status=unread").json()
        assert body2["total"] == 0

    def test_batch_rejects_empty_list(self, client):
        resp = client.put(
            "/istota/api/feeds/entries/batch",
            json={"entry_ids": [], "status": "read"},
        )
        assert resp.status_code == 400

    def test_rejects_invalid_status(self, ctx, client):
        _seed(ctx)
        resp = client.put(
            "/istota/api/feeds/entries/1",
            json={"status": "archived"},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Starring + GET ?starred=
# ---------------------------------------------------------------------------


class TestStarring:
    def test_entry_response_includes_starred_fields(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds").json()
        for e in body["entries"]:
            assert "starred" in e
            assert "starred_at" in e
            assert e["starred"] is False

    def test_put_single_toggles_starred(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds").json()
        entry_id = body["entries"][0]["id"]

        resp = client.put(
            f"/istota/api/feeds/entries/{entry_id}",
            json={"starred": True},
        )
        assert resp.status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT starred, starred_at FROM feed_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
            assert row["starred"] == 1
            assert row["starred_at"] is not None

    def test_put_combined_status_and_starred(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds?status=unread").json()
        entry_id = body["entries"][0]["id"]

        resp = client.put(
            f"/istota/api/feeds/entries/{entry_id}",
            json={"status": "read", "starred": True},
        )
        assert resp.status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT status, starred FROM feed_entries WHERE id = ?",
                (entry_id,),
            ).fetchone()
            assert row["status"] == "read"
            assert row["starred"] == 1

    def test_batch_combined_status_and_starred(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds").json()
        ids = [e["id"] for e in body["entries"]]

        resp = client.put(
            "/istota/api/feeds/entries/batch",
            json={"entry_ids": ids, "starred": True},
        )
        assert resp.status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM feed_entries WHERE starred = 1"
            ).fetchone()["c"]
            assert count == len(ids)

    def test_put_rejects_non_bool_starred(self, ctx, client):
        _seed(ctx)
        resp = client.put(
            "/istota/api/feeds/entries/1", json={"starred": "yes"},
        )
        assert resp.status_code == 400

    def test_get_with_starred_filter(self, ctx, client):
        ids = _seed(ctx)
        # Star one tumblr entry.
        with feeds_db.connect(ctx.db_path) as conn:
            target = conn.execute(
                "SELECT id FROM feed_entries WHERE feed_id = ? LIMIT 1",
                (ids["tumblr_feed_id"],),
            ).fetchone()["id"]
            feeds_db.update_entry_starred(conn, [target], True)
            conn.commit()

        body = client.get("/istota/api/feeds?starred=1").json()
        assert body["total"] == 1
        assert body["entries"][0]["id"] == target
        assert body["entries"][0]["starred"] is True

        # starred=0 returns the unstarred remainder.
        body0 = client.get("/istota/api/feeds?starred=0").json()
        assert body0["total"] == 2

        # Default (no starred param) returns everything.
        body_all = client.get("/istota/api/feeds").json()
        assert body_all["total"] == 3


# ---------------------------------------------------------------------------
# POST /feeds/mark-as-read
# ---------------------------------------------------------------------------


class TestMarkAsReadRoute:
    def test_scope_all(self, ctx, client):
        _seed(ctx)
        resp = client.post("/istota/api/feeds/mark-as-read", json={"scope": "all"})
        assert resp.status_code == 200
        # Two unread entries pre-existed.
        assert resp.json()["updated"] == 2
        body = client.get("/istota/api/feeds?status=unread").json()
        assert body["total"] == 0

    def test_scope_feed(self, ctx, client):
        ids = _seed(ctx)
        resp = client.post(
            "/istota/api/feeds/mark-as-read",
            json={"scope": "feed", "id": ids["tumblr_feed_id"]},
        )
        assert resp.status_code == 200
        # Only the unread tumblr entry (post-1) flipped; rss-1 still unread.
        assert resp.json()["updated"] == 1
        body = client.get("/istota/api/feeds?status=unread").json()
        assert body["total"] == 1

    def test_scope_category(self, ctx, client):
        ids = _seed(ctx)
        resp = client.post(
            "/istota/api/feeds/mark-as-read",
            json={"scope": "category", "id": ids["cat_id"]},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1  # post-1 only

    def test_before_id_caps(self, ctx, client):
        _seed(ctx)
        # Find current unread max id.
        body = client.get("/istota/api/feeds?status=unread").json()
        sorted_ids = sorted(e["id"] for e in body["entries"])
        cap = sorted_ids[0]  # only the first
        resp = client.post(
            "/istota/api/feeds/mark-as-read",
            json={"scope": "all", "before_id": cap},
        )
        assert resp.status_code == 200
        assert resp.json()["updated"] == 1

    def test_rejects_unknown_scope(self, client):
        resp = client.post(
            "/istota/api/feeds/mark-as-read", json={"scope": "global"},
        )
        assert resp.status_code == 400

    def test_feed_scope_requires_id(self, client):
        resp = client.post(
            "/istota/api/feeds/mark-as-read", json={"scope": "feed"},
        )
        assert resp.status_code == 400

    def test_negative_before_id_rejected(self, client):
        resp = client.post(
            "/istota/api/feeds/mark-as-read",
            json={"scope": "all", "before_id": -1},
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# GET/PUT /feeds/config — round-trip
# ---------------------------------------------------------------------------


class TestConfigEndpoint:
    def test_get_returns_empty_for_fresh_workspace(self, ctx, client):
        resp = client.get("/istota/api/feeds/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["config"] == {"settings": {}, "categories": [], "feeds": []}
        assert body["diagnostics"]["total_feeds"] == 0

    def test_put_persists_to_db(self, ctx, client):
        payload = {
            "config": {
                "settings": {"default_poll_interval_minutes": 45},
                "categories": [{"slug": "blogs", "title": "Blogs"}],
                "feeds": [
                    {
                        "url": "https://example.com/feed.xml",
                        "title": "Example",
                        "category": "blogs",
                    },
                ],
            }
        }
        resp = client.put("/istota/api/feeds/config", json=payload)
        assert resp.status_code == 200
        assert resp.json()["sync"]["feeds_added"] == 1

        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT title FROM feeds WHERE url = ?",
                ("https://example.com/feed.xml",),
            ).fetchone()
            assert row["title"] == "Example"
            cat = conn.execute(
                "SELECT title FROM feed_categories WHERE slug = ?",
                ("blogs",),
            ).fetchone()
            assert cat["title"] == "Blogs"
            assert feeds_db.get_default_poll_interval(conn) == 45

        # GET round-trip: the wire shape coming back matches what was sent.
        body = client.get("/istota/api/feeds/config").json()
        assert body["config"]["settings"] == {"default_poll_interval_minutes": 45}
        assert body["config"]["categories"] == [
            {"slug": "blogs", "title": "Blogs"},
        ]
        urls = [f["url"] for f in body["config"]["feeds"]]
        assert urls == ["https://example.com/feed.xml"]

    def test_put_rejects_malformed_body(self, client):
        resp = client.put("/istota/api/feeds/config", json={"oops": "no"})
        assert resp.status_code == 400

    def test_put_rejects_feed_without_url(self, client):
        resp = client.put(
            "/istota/api/feeds/config",
            json={"config": {"feeds": [{"title": "no url"}]}},
        )
        assert resp.status_code == 400

    def test_put_removes_feeds_and_categories_dropped_from_payload(self, ctx, client):
        """Wholesale-replace: feeds removed in the UI must not linger in the DB.

        Regression: previously the sidebar still showed the old RSS row after
        re-subscribing as ``tumblr:`` because ``_sync_config_to_db`` was
        upsert-only.
        """
        client.put(
            "/istota/api/feeds/config",
            json={
                "config": {
                    "categories": [
                        {"slug": "rss", "title": "RSS"},
                        {"slug": "tumblr", "title": "Tumblr"},
                    ],
                    "feeds": [
                        {
                            "url": "https://nemfrog.tumblr.com/rss",
                            "title": "Nemfrog RSS",
                            "category": "rss",
                        },
                    ],
                }
            },
        )
        resp = client.put(
            "/istota/api/feeds/config",
            json={
                "config": {
                    "categories": [{"slug": "tumblr", "title": "Tumblr"}],
                    "feeds": [
                        {
                            "url": "tumblr:nemfrog",
                            "title": "Nemfrog",
                            "category": "tumblr",
                        },
                    ],
                }
            },
        )
        assert resp.status_code == 200
        sync = resp.json()["sync"]
        assert sync["feeds_removed"] == 1
        assert sync["categories_removed"] == 1

        with feeds_db.connect(ctx.db_path) as conn:
            urls = {row["url"] for row in conn.execute("SELECT url FROM feeds")}
            slugs = {
                row["slug"] for row in conn.execute("SELECT slug FROM feed_categories")
            }
        assert urls == {"tumblr:nemfrog"}
        assert slugs == {"tumblr"}

    def test_diagnostics_reflect_seeded_state(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds/config").json()
        diag = body["diagnostics"]
        assert diag["total_feeds"] == 2
        assert diag["total_entries"] == 3
        assert diag["unread_entries"] == 2  # post-1, rss-1


# ---------------------------------------------------------------------------
# OPML import/export
# ---------------------------------------------------------------------------


_SAMPLE_OPML = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Test export</title></head>
  <body>
    <outline text="Tumblr" title="Tumblr">
      <outline type="rss" text="Nemfrog"
               xmlUrl="http://127.0.0.1:8900/tumblr/nemfrog/feed.xml"
               htmlUrl="https://nemfrog.tumblr.com" />
    </outline>
    <outline type="rss" text="Example"
             xmlUrl="https://example.com/feed.xml"
             htmlUrl="https://example.com" />
  </body>
</opml>
"""


class TestOpml:
    def test_import_rewrites_bridger_urls(self, ctx, client):
        resp = client.post(
            "/istota/api/feeds/import-opml",
            files={"file": ("export.opml", _SAMPLE_OPML, "text/x-opml")},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["feeds_added"] == 2
        assert body["rewritten_bridger_urls"] == 1

        with feeds_db.connect(ctx.db_path) as conn:
            urls = {row["url"] for row in conn.execute("SELECT url FROM feeds")}
        assert "tumblr:nemfrog" in urls
        assert "https://example.com/feed.xml" in urls

    def test_import_rejects_empty(self, client):
        resp = client.post(
            "/istota/api/feeds/import-opml",
            files={"file": ("empty.opml", b"", "text/x-opml")},
        )
        assert resp.status_code == 400

    def test_import_rejects_too_large(self, client):
        big = b"<opml>" + b"x" * (5 * 1024 * 1024 + 1) + b"</opml>"
        resp = client.post(
            "/istota/api/feeds/import-opml",
            files={"file": ("big.opml", big, "text/x-opml")},
        )
        assert resp.status_code == 413

    def test_import_rejects_malformed_xml(self, client):
        resp = client.post(
            "/istota/api/feeds/import-opml",
            files={"file": ("bad.opml", b"<not-xml", "text/x-opml")},
        )
        assert resp.status_code == 400

    def test_export_returns_opml(self, ctx, client):
        # Seed via PUT config, then export.
        client.put(
            "/istota/api/feeds/config",
            json={
                "config": {
                    "feeds": [{"url": "tumblr:nemfrog", "title": "Nemfrog"}],
                }
            },
        )
        resp = client.get("/istota/api/feeds/export-opml")
        assert resp.status_code == 200
        assert "opml" in resp.text.lower()
        assert "tumblr:nemfrog" in resp.text
        assert "attachment" in resp.headers.get("content-disposition", "")


# ---------------------------------------------------------------------------
# GET /feeds — cross-entry image suppression (ISSUE-162)
# ---------------------------------------------------------------------------


IMG = "https://64.media.tumblr.com/aaa/bbb-01/s500x750/hash.jpg"
IMG_BIG = "https://72.media.tumblr.com/aaa/bbb-01/s1280x1920/hash.jpg"
OTHER_IMG = "https://64.media.tumblr.com/ccc/ddd-01/s500x750/other.jpg"


def _seed_reblog_pair(ctx, *, older_published: str, newer_published: str,
                      same_feed: bool = True, window_days: int | None = None):
    """Two entries carrying the same picture, newest first by publication."""
    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        cat_id = feeds_db.upsert_category(conn, "art", "Art")
        feed_a = feeds_db.upsert_feed(
            conn, url="tumblr:a", title="A", site_url=None,
            source_type="tumblr", category_id=cat_id, poll_interval_minutes=60,
        )
        feed_b = feed_a if same_feed else feeds_db.upsert_feed(
            conn, url="tumblr:b", title="B", site_url=None,
            source_type="tumblr", category_id=None, poll_interval_minutes=60,
        )
        feeds_db.insert_entries(conn, feed_a, [
            EntryRecord(
                id=0, feed_id=feed_a, guid="newer", title="Newer", url=None,
                author=None, content_html=None, content_text=None,
                image_urls=[IMG_BIG], published_at=newer_published,
                fetched_at=newer_published, status="unread",
            ),
        ])
        feeds_db.insert_entries(conn, feed_b, [
            EntryRecord(
                id=0, feed_id=feed_b, guid="older", title="Older", url=None,
                author=None, content_html=None, content_text=None,
                image_urls=[IMG, OTHER_IMG], published_at=older_published,
                fetched_at=older_published, status="unread",
            ),
        ])
        if window_days is not None:
            feeds_db.set_image_dedupe_window_days(conn, window_days)
        conn.commit()
    return {"feed_a": feed_a, "feed_b": feed_b, "cat_id": cat_id}


def _by_title(body) -> dict:
    return {e["title"]: e for e in body["entries"]}


class TestImageSuppression:
    def test_repeat_inside_window_is_hidden_on_the_older_entry(self, ctx, client):
        _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-07-14T10:00:00+00:00",
        )
        entries = _by_title(client.get("/istota/api/feeds").json())

        # Both entries still render — only the repeated tile goes.
        assert set(entries) == {"Newer", "Older"}
        assert entries["Newer"]["images"] == [IMG_BIG]
        assert entries["Newer"]["duplicate_image_count"] == 0
        assert entries["Older"]["images"] == [OTHER_IMG]
        assert entries["Older"]["duplicate_image_count"] == 1

    def test_repeat_outside_window_renders(self, ctx, client):
        _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-05-01T10:00:00+00:00",
        )
        entries = _by_title(client.get("/istota/api/feeds").json())

        assert entries["Older"]["images"] == [IMG, OTHER_IMG]
        assert entries["Older"]["duplicate_image_count"] == 0

    def test_window_zero_disables_suppression(self, ctx, client):
        _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-07-14T10:00:00+00:00",
            window_days=0,
        )
        entries = _by_title(client.get("/istota/api/feeds").json())

        assert entries["Older"]["images"] == [IMG, OTHER_IMG]
        assert entries["Older"]["duplicate_image_count"] == 0

    def test_configured_window_is_honoured(self, ctx, client):
        _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-06-20T10:00:00+00:00",  # 26 days
            window_days=30,
        )
        entries = _by_title(client.get("/istota/api/feeds").json())

        assert entries["Older"]["images"] == [OTHER_IMG]

    def test_feed_filter_scopes_the_lookup(self, ctx, client):
        """Viewing one blog must not hide tiles because of another blog."""
        ids = _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-07-14T10:00:00+00:00",
            same_feed=False,
        )
        entries = _by_title(
            client.get(f"/istota/api/feeds?feed_id={ids['feed_b']}").json()
        )

        assert set(entries) == {"Older"}
        assert entries["Older"]["images"] == [IMG, OTHER_IMG]
        assert entries["Older"]["duplicate_image_count"] == 0

    def test_category_filter_scopes_the_lookup(self, ctx, client):
        ids = _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-07-14T10:00:00+00:00",
            same_feed=False,
        )
        # Only feed A is in the category, so within that view nothing repeats.
        entries = _by_title(
            client.get(f"/istota/api/feeds?category_id={ids['cat_id']}").json()
        )

        assert set(entries) == {"Newer"}
        assert entries["Newer"]["images"] == [IMG_BIG]

    def test_owner_outside_the_page_still_suppresses(self, ctx, client):
        """Paging must not resurrect a tile the previous page already showed."""
        _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-07-14T10:00:00+00:00",
        )
        body = client.get("/istota/api/feeds?limit=1&offset=1").json()

        assert [e["title"] for e in body["entries"]] == ["Older"]
        assert body["entries"][0]["images"] == [OTHER_IMG]

    def test_read_state_does_not_change_suppression(self, ctx, client):
        """Marking the newer entry read (as the reader does while scrolling)
        must not make the hidden tile pop back into view."""
        _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-07-14T10:00:00+00:00",
        )
        with feeds_db.connect(ctx.db_path) as conn:
            newer = next(
                e for e in feeds_db.list_entries(conn) if e.guid == "newer"
            )
            feeds_db.update_entry_status(conn, [newer.id], "read")
            conn.commit()

        entries = _by_title(client.get("/istota/api/feeds?status=unread").json())

        assert set(entries) == {"Older"}
        assert entries["Older"]["images"] == [OTHER_IMG]

    def test_starred_view_keeps_the_image_you_starred(self, ctx, client):
        """A starred post must not lose its picture to an unstarred repeat."""
        _seed_reblog_pair(
            ctx,
            newer_published="2026-07-16T10:00:00+00:00",
            older_published="2026-07-14T10:00:00+00:00",
        )
        with feeds_db.connect(ctx.db_path) as conn:
            older = next(
                e for e in feeds_db.list_entries(conn) if e.guid == "older"
            )
            feeds_db.update_entry_starred(conn, [older.id], True)
            conn.commit()

        entries = _by_title(client.get("/istota/api/feeds?starred=1").json())

        assert set(entries) == {"Older"}
        assert entries["Older"]["images"] == [IMG, OTHER_IMG]
        assert entries["Older"]["duplicate_image_count"] == 0


class TestImageDedupeWindowConfig:
    def test_get_config_reports_the_window(self, ctx, client):
        _seed(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.set_image_dedupe_window_days(conn, 21)
            conn.commit()

        settings = client.get("/istota/api/feeds/config").json()["config"]["settings"]
        assert settings["image_dedupe_window_days"] == 21

    def test_put_config_round_trips_the_window(self, ctx, client):
        _seed(ctx)
        resp = client.put(
            "/istota/api/feeds/config",
            json={"config": {
                "settings": {"image_dedupe_window_days": 7},
                "categories": [],
                "feeds": [],
            }},
        )
        assert resp.status_code == 200
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_image_dedupe_window_days(conn) == 7

    def test_put_config_accepts_zero_as_off(self, ctx, client):
        _seed(ctx)
        client.put(
            "/istota/api/feeds/config",
            json={"config": {
                "settings": {"image_dedupe_window_days": 0},
                "categories": [], "feeds": [],
            }},
        )
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_image_dedupe_window_days(conn) == 0

    def test_put_config_rejects_a_non_int_window(self, ctx, client):
        _seed(ctx)
        resp = client.put(
            "/istota/api/feeds/config",
            json={"config": {
                "settings": {"image_dedupe_window_days": "soon"},
                "categories": [], "feeds": [],
            }},
        )
        assert resp.status_code == 400

    def test_put_config_rejects_a_negative_window(self, ctx, client):
        _seed(ctx)
        resp = client.put(
            "/istota/api/feeds/config",
            json={"config": {
                "settings": {"image_dedupe_window_days": -1},
                "categories": [], "feeds": [],
            }},
        )
        assert resp.status_code == 400
