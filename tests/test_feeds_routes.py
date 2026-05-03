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
from istota.feeds._config_io import read_feeds_config, write_feeds_config
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
    c = synthesize_feeds_context("stefan", tmp_path)
    c.ensure_dirs()
    feeds_db.init_db(c.db_path)
    return c


@pytest.fixture
def client(ctx: FeedsContext) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/istota/api/feeds")
    app.dependency_overrides[require_auth] = lambda: {"username": "stefan"}
    app.dependency_overrides[get_user_context] = lambda: ctx
    return TestClient(app)


# ---------------------------------------------------------------------------
# GET /feeds — response shape parity with the Miniflux proxy
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

    def test_feed_shape_matches_miniflux_proxy(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds").json()
        # Pick the tumblr feed deterministically.
        feed = next(f for f in body["feeds"] if f["title"] == "Nemfrog")
        assert set(feed.keys()) == {"id", "title", "site_url", "category"}
        assert set(feed["category"].keys()) == {"id", "title"}
        assert feed["category"]["title"] == "Tumblr"

    def test_entry_shape_matches_miniflux_proxy(self, ctx, client):
        _seed(ctx)
        body = client.get("/istota/api/feeds").json()
        entry = body["entries"][0]
        expected = {
            "id", "title", "url", "content", "images", "feed",
            "status", "published_at", "created_at",
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
# GET/PUT /feeds/config — round-trip
# ---------------------------------------------------------------------------


class TestConfigEndpoint:
    def test_get_returns_empty_for_fresh_workspace(self, ctx, client):
        resp = client.get("/istota/api/feeds/config")
        assert resp.status_code == 200
        body = resp.json()
        assert body["config"] == {"settings": {}, "categories": [], "feeds": []}
        assert body["diagnostics"]["total_feeds"] == 0

    def test_put_writes_toml_and_syncs_db(self, ctx, client):
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

        # Round-trip: feeds.toml on disk, feeds in DB.
        on_disk = read_feeds_config(ctx.config_path)
        assert on_disk["categories"] == [{"slug": "blogs", "title": "Blogs"}]
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT title FROM feeds WHERE url = ?",
                ("https://example.com/feed.xml",),
            ).fetchone()
            assert row["title"] == "Example"

    def test_put_rejects_malformed_body(self, client):
        resp = client.put("/istota/api/feeds/config", json={"oops": "no"})
        assert resp.status_code == 400

    def test_put_rejects_feed_without_url(self, client):
        resp = client.put(
            "/istota/api/feeds/config",
            json={"config": {"feeds": [{"title": "no url"}]}},
        )
        assert resp.status_code == 400

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

        # feeds.toml was projected from the DB.
        on_disk = read_feeds_config(ctx.config_path)
        urls = {f["url"] for f in on_disk["feeds"]}
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
