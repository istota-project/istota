"""Tests for the native feeds Click CLI."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from istota.feeds import db as feeds_db
from istota.feeds.cli import cli
from istota.feeds.workspace import synthesize_feeds_context


@pytest.fixture
def ctx(tmp_path):
    fctx = synthesize_feeds_context("alice", tmp_path)
    fctx.ensure_dirs()
    feeds_db.init_db(fctx.db_path)
    return fctx


def _invoke(ctx, args):
    runner = CliRunner()
    return runner.invoke(cli, args, obj=ctx, standalone_mode=False, catch_exceptions=False)


def _seed_db(ctx, *, feeds=None, categories=None, default_interval=None):
    """Seed the per-user feeds DB directly. Replaces the pre-cut feeds.toml seed."""
    feeds_db.init_db(ctx.db_path)
    with feeds_db.connect(ctx.db_path) as conn:
        slug_to_id: dict[str, int] = {}
        for c in categories or []:
            cat_id = feeds_db.upsert_category(conn, c["slug"], c.get("title", c["slug"]))
            slug_to_id[c["slug"]] = cat_id
        for f in feeds or []:
            url = f["url"]
            from istota.feeds.models import detect_source_type, default_poll_interval_for
            source_type = detect_source_type(url)
            cat_id = slug_to_id.get(f.get("category"))
            if f.get("category") and cat_id is None:
                cat_id = feeds_db.ensure_category(conn, f["category"])
                slug_to_id[f["category"]] = cat_id
            feeds_db.upsert_feed(
                conn,
                url=url,
                title=f.get("title"),
                site_url=f.get("site_url"),
                source_type=source_type,
                category_id=cat_id,
                poll_interval_minutes=f.get(
                    "poll_interval_minutes",
                    default_poll_interval_for(source_type),
                ),
            )
        if default_interval is not None:
            feeds_db.set_default_poll_interval(conn, default_interval)
        conn.commit()


# ---------------------------------------------------------------------------
# list / categories / entries
# ---------------------------------------------------------------------------


class TestList:
    def test_empty(self, ctx):
        r = _invoke(ctx, ["list"])
        assert r.exit_code == 0
        out = json.loads(r.output)
        assert out["status"] == "ok"
        assert out["count"] == 0
        assert out["feeds"] == []

    def test_lists_seeded_feed(self, ctx):
        _seed_db(
            ctx,
            categories=[{"slug": "blogs", "title": "Blogs"}],
            feeds=[{
                "url": "https://example.com/feed.xml",
                "title": "Example",
                "category": "blogs",
            }],
        )
        r = _invoke(ctx, ["list"])
        out = json.loads(r.output)
        assert out["count"] == 1
        assert out["feeds"][0]["url"] == "https://example.com/feed.xml"
        assert out["feeds"][0]["category"] == "Blogs"
        assert out["feeds"][0]["category_slug"] == "blogs"
        assert out["feeds"][0]["source_type"] == "rss"

    def test_provider_url_classified(self, ctx):
        _seed_db(ctx, feeds=[{"url": "tumblr:nemfrog"}, {"url": "arena:cats"}])
        r = _invoke(ctx, ["list"])
        out = json.loads(r.output)
        types = {f["url"]: f["source_type"] for f in out["feeds"]}
        assert types["tumblr:nemfrog"] == "tumblr"
        assert types["arena:cats"] == "arena"


class TestCategories:
    def test_lists_categories(self, ctx):
        _seed_db(ctx, categories=[
            {"slug": "blogs", "title": "Blogs"},
            {"slug": "art", "title": "Art"},
        ])
        r = _invoke(ctx, ["categories"])
        out = json.loads(r.output)
        slugs = {c["slug"] for c in out["categories"]}
        assert slugs == {"blogs", "art"}


class TestEntries:
    def test_filters_by_status(self, ctx):
        _seed_db(ctx, feeds=[{"url": "https://x.test/feed"}])
        with feeds_db.connect(ctx.db_path) as conn:
            feed = feeds_db.get_feed_by_url(conn, "https://x.test/feed")
            from istota.feeds.models import EntryRecord
            feeds_db.insert_entries(conn, feed.id, [
                EntryRecord(id=0, feed_id=feed.id, guid="a", title="A",
                            url=None, author=None, content_html=None,
                            content_text=None, image_urls=[],
                            published_at="2026-01-01T00:00:00+00:00",
                            fetched_at="2026-01-01T00:00:00+00:00",
                            status="unread"),
                EntryRecord(id=0, feed_id=feed.id, guid="b", title="B",
                            url=None, author=None, content_html=None,
                            content_text=None, image_urls=[],
                            published_at="2026-01-02T00:00:00+00:00",
                            fetched_at="2026-01-02T00:00:00+00:00",
                            status="read"),
            ])
            conn.commit()

        r = _invoke(ctx, ["entries", "--status", "unread"])
        out = json.loads(r.output)
        assert out["count"] == 1
        assert out["entries"][0]["title"] == "A"

        r = _invoke(ctx, ["entries", "--status", "read"])
        out = json.loads(r.output)
        assert out["count"] == 1
        assert out["entries"][0]["title"] == "B"


# ---------------------------------------------------------------------------
# add / remove
# ---------------------------------------------------------------------------


class TestAdd:
    def test_adds_feed(self, ctx):
        r = _invoke(ctx, [
            "add", "--url", "https://example.com/feed.xml",
            "--title", "Example", "--category", "blogs",
        ])
        out = json.loads(r.output)
        assert out["status"] == "ok"
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT title, category_id FROM feeds WHERE url = ?",
                ("https://example.com/feed.xml",),
            ).fetchone()
            assert row["title"] == "Example"
            cat = conn.execute(
                "SELECT slug FROM feed_categories WHERE id = ?",
                (row["category_id"],),
            ).fetchone()
            assert cat["slug"] == "blogs"

    def test_duplicate_returns_error(self, ctx):
        _seed_db(ctx, feeds=[{"url": "https://x/feed"}])
        r = _invoke(ctx, ["add", "--url", "https://x/feed"])
        out = json.loads(r.output)
        assert out["status"] == "error"
        assert "already exists" in out["error"]

    def test_add_uses_user_default_interval(self, ctx):
        _seed_db(ctx, default_interval=77)
        r = _invoke(ctx, ["add", "--url", "https://example.com/feed.xml"])
        assert json.loads(r.output)["status"] == "ok"
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT poll_interval_minutes FROM feeds WHERE url = ?",
                ("https://example.com/feed.xml",),
            ).fetchone()
            assert row["poll_interval_minutes"] == 77

    def test_add_does_not_stomp_existing_category_title(self, ctx):
        _seed_db(ctx, categories=[{"slug": "blogs", "title": "Blogs"}])
        # Adding a feed with --category blogs (slug-only) must preserve
        # the title set elsewhere.
        r = _invoke(ctx, [
            "add", "--url", "https://x.test/feed", "--category", "blogs",
        ])
        assert json.loads(r.output)["status"] == "ok"
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT title FROM feed_categories WHERE slug = ?", ("blogs",),
            ).fetchone()
            assert row["title"] == "Blogs"  # not stomped to "blogs"


class TestRemove:
    def test_removes_by_url(self, ctx):
        _seed_db(ctx, feeds=[
            {"url": "https://a/feed"}, {"url": "https://b/feed"},
        ])
        r = _invoke(ctx, ["remove", "--url", "https://a/feed"])
        out = json.loads(r.output)
        assert out["status"] == "ok"
        assert out["removed_url"] == "https://a/feed"

        with feeds_db.connect(ctx.db_path) as conn:
            urls = {row["url"] for row in conn.execute("SELECT url FROM feeds")}
            assert urls == {"https://b/feed"}

    def test_removes_by_id(self, ctx):
        _seed_db(ctx, feeds=[{"url": "https://x/feed"}])
        with feeds_db.connect(ctx.db_path) as conn:
            feed = feeds_db.get_feed_by_url(conn, "https://x/feed")
        r = _invoke(ctx, ["remove", "--id", str(feed.id)])
        out = json.loads(r.output)
        assert out["removed_url"] == "https://x/feed"

    def test_no_args_errors(self, ctx):
        r = _invoke(ctx, ["remove"])
        out = json.loads(r.output)
        assert out["status"] == "error"

    def test_unknown_url_errors(self, ctx):
        r = _invoke(ctx, ["remove", "--url", "https://nope.test/feed"])
        out = json.loads(r.output)
        assert out["status"] == "error"


# ---------------------------------------------------------------------------
# refresh / poll
# ---------------------------------------------------------------------------


class TestRefresh:
    def test_clears_next_poll_at(self, ctx):
        _seed_db(ctx, feeds=[{"url": "https://x/feed"}])
        with feeds_db.connect(ctx.db_path) as conn:
            conn.execute("UPDATE feeds SET next_poll_at = '9999-01-01T00:00:00+00:00'")
            conn.commit()
        r = _invoke(ctx, ["refresh"])
        out = json.loads(r.output)
        assert out["status"] == "ok"
        assert out["reset_count"] >= 1
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute("SELECT next_poll_at FROM feeds").fetchone()
            assert row["next_poll_at"] is None


class TestPoll:
    def test_poll_uses_stub_http(self, ctx, monkeypatch):
        _seed_db(ctx, feeds=[{"url": "https://x.test/feed"}])

        sample_rss = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>X</title>
<link>https://x.test</link>
<item><guid>g1</guid><title>A</title><link>https://x.test/a</link></item>
</channel></rss>"""

        class StubResp:
            status_code = 200
            content = sample_rss
            text = sample_rss.decode()
            headers = {"ETag": "abc"}

        def stub_get(*a, **kw):
            return StubResp()

        import httpx
        monkeypatch.setattr(httpx, "get", stub_get)

        r = _invoke(ctx, ["poll"])
        out = json.loads(r.output)
        assert out["status"] == "ok"
        assert out["polled"] == 1
        assert out["new_entries"] == 1

    def test_run_scheduled_polls_due_feeds(self, ctx, monkeypatch):
        _seed_db(ctx, feeds=[{"url": "https://y.test/feed"}])

        sample_rss = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>Y</title>
<link>https://y.test</link>
<item><guid>g1</guid><title>A</title><link>https://y.test/a</link></item>
</channel></rss>"""

        class StubResp:
            status_code = 200
            content = sample_rss
            text = sample_rss.decode()
            headers = {"ETag": "abc"}

        import httpx
        monkeypatch.setattr(httpx, "get", lambda *a, **kw: StubResp())

        r = _invoke(ctx, ["run-scheduled"])
        out = json.loads(r.output)
        assert out["status"] == "ok"
        assert out["polled"] == 1
        assert out["new_entries"] == 1

    def test_partial_error_when_some_feeds_fail(self, ctx, monkeypatch):
        _seed_db(ctx, feeds=[
            {"url": "https://ok.test/feed"},
            {"url": "https://bad.test/feed"},
        ])

        sample_rss = b"""<?xml version="1.0"?>
<rss version="2.0"><channel>
<title>OK</title><link>https://ok.test</link>
<item><guid>g1</guid><title>A</title><link>https://ok.test/a</link></item>
</channel></rss>"""

        class OkResp:
            status_code = 200
            content = sample_rss
            text = sample_rss.decode()
            headers = {"ETag": "abc"}

        import httpx

        def stub_get(url, *a, **kw):
            if "bad.test" in url:
                raise httpx.ConnectError("boom")
            return OkResp()

        monkeypatch.setattr(httpx, "get", stub_get)

        r = _invoke(ctx, ["poll"])
        out = json.loads(r.output)
        assert out["status"] == "partial_error"
        assert out["polled"] == 2
        assert out["errors"] == 1
        assert r.exit_code == 0

    def test_error_when_all_feeds_fail(self, ctx, monkeypatch):
        _seed_db(ctx, feeds=[{"url": "https://bad.test/feed"}])

        import httpx

        def stub_get(*a, **kw):
            raise httpx.ConnectError("network down")

        monkeypatch.setattr(httpx, "get", stub_get)

        runner = CliRunner()
        r = runner.invoke(cli, ["poll"], obj=ctx, standalone_mode=False)
        assert r.exit_code == 1
        out = json.loads(r.output)
        assert out["status"] == "error"
        assert out["polled"] == 1
        assert out["errors"] == 1
        assert "all 1 feed poll(s) failed" in out["error"]


class TestStar:
    def _seed_one(self, ctx):
        _seed_db(ctx, feeds=[{"url": "https://x.test/feed"}])
        with feeds_db.connect(ctx.db_path) as conn:
            from istota.feeds.models import EntryRecord
            feed = feeds_db.get_feed_by_url(conn, "https://x.test/feed")
            feeds_db.insert_entries(conn, feed.id, [
                EntryRecord(
                    id=0, feed_id=feed.id, guid="a", title="A",
                    url=None, author=None, content_html=None,
                    content_text=None, image_urls=[],
                    published_at="2026-01-01T00:00:00+00:00",
                    fetched_at="2026-01-01T00:00:00+00:00",
                ),
                EntryRecord(
                    id=0, feed_id=feed.id, guid="b", title="B",
                    url=None, author=None, content_html=None,
                    content_text=None, image_urls=[],
                    published_at="2026-01-02T00:00:00+00:00",
                    fetched_at="2026-01-02T00:00:00+00:00",
                ),
            ])
            conn.commit()
            ids = [e.id for e in feeds_db.list_entries(conn)]
        return ids

    def test_star_single(self, ctx):
        ids = self._seed_one(ctx)
        r = _invoke(ctx, ["star", "--id", str(ids[0])])
        out = json.loads(r.output)
        assert out["status"] == "ok"
        assert out["updated"] == 1
        assert out["starred"] is True
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT starred FROM feed_entries WHERE id = ?", (ids[0],),
            ).fetchone()
            assert row["starred"] == 1

    def test_star_batch(self, ctx):
        ids = self._seed_one(ctx)
        r = _invoke(ctx, ["star", "--ids", ",".join(str(i) for i in ids)])
        out = json.loads(r.output)
        assert out["updated"] == 2

    def test_unstar(self, ctx):
        ids = self._seed_one(ctx)
        _invoke(ctx, ["star", "--id", str(ids[0])])
        r = _invoke(ctx, ["star", "--id", str(ids[0]), "--unstar"])
        out = json.loads(r.output)
        assert out["status"] == "ok"
        assert out["starred"] is False
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT starred, starred_at FROM feed_entries WHERE id = ?",
                (ids[0],),
            ).fetchone()
            assert row["starred"] == 0
            assert row["starred_at"] is None

    def test_starred_lists_only_starred(self, ctx):
        ids = self._seed_one(ctx)
        _invoke(ctx, ["star", "--id", str(ids[0])])
        r = _invoke(ctx, ["starred"])
        out = json.loads(r.output)
        assert out["count"] == 1
        assert out["entries"][0]["id"] == ids[0]

    def test_star_no_args_errors(self, ctx):
        r = _invoke(ctx, ["star"])
        out = json.loads(r.output)
        assert out["status"] == "error"


class TestMarkRead:
    def _seed_two_feeds(self, ctx):
        _seed_db(ctx, feeds=[
            {"url": "https://x.test/feed", "category": "blogs"},
            {"url": "https://y.test/feed"},
        ], categories=[{"slug": "blogs", "title": "Blogs"}])
        with feeds_db.connect(ctx.db_path) as conn:
            from istota.feeds.models import EntryRecord
            feed_x = feeds_db.get_feed_by_url(conn, "https://x.test/feed")
            feed_y = feeds_db.get_feed_by_url(conn, "https://y.test/feed")
            for fid, guid in [
                (feed_x.id, "x1"), (feed_x.id, "x2"),
                (feed_y.id, "y1"),
            ]:
                feeds_db.insert_entries(conn, fid, [
                    EntryRecord(
                        id=0, feed_id=fid, guid=guid, title=guid,
                        url=None, author=None, content_html=None,
                        content_text=None, image_urls=[],
                        published_at="2026-05-01T00:00:00+00:00",
                        fetched_at="2026-05-01T00:00:00+00:00",
                    ),
                ])
            conn.commit()
        return feed_x.id, feed_y.id

    def test_mark_all(self, ctx):
        self._seed_two_feeds(ctx)
        r = _invoke(ctx, ["mark-read", "--all"])
        out = json.loads(r.output)
        assert out["status"] == "ok"
        assert out["updated"] == 3
        assert out["scope"] == "all"

    def test_mark_feed(self, ctx):
        feed_x, _ = self._seed_two_feeds(ctx)
        r = _invoke(ctx, ["mark-read", "--feed", str(feed_x)])
        out = json.loads(r.output)
        assert out["updated"] == 2
        assert out["scope"] == "feed"

    def test_mark_category_by_slug(self, ctx):
        self._seed_two_feeds(ctx)
        r = _invoke(ctx, ["mark-read", "--category", "blogs"])
        out = json.loads(r.output)
        assert out["updated"] == 2
        assert out["scope"] == "category"

    def test_mark_unknown_category_errors(self, ctx):
        self._seed_two_feeds(ctx)
        r = _invoke(ctx, ["mark-read", "--category", "nope"])
        out = json.loads(r.output)
        assert out["status"] == "error"

    def test_no_scope_errors(self, ctx):
        r = _invoke(ctx, ["mark-read"])
        out = json.loads(r.output)
        assert out["status"] == "error"

    def test_mutually_exclusive(self, ctx):
        feed_x, _ = self._seed_two_feeds(ctx)
        r = _invoke(ctx, ["mark-read", "--all", "--feed", str(feed_x)])
        out = json.loads(r.output)
        assert out["status"] == "error"


class TestOpml:
    def test_import_and_round_trip(self, ctx, tmp_path):
        opml = """<?xml version="1.0"?>
<opml version="2.0">
  <head><title>x</title></head>
  <body>
    <outline title="Tumblr">
      <outline type="rss" text="nemfrog" xmlUrl="http://127.0.0.1:8900/tumblr/nemfrog/feed.xml"/>
    </outline>
    <outline title="Blogs">
      <outline type="rss" text="Example" title="Example" xmlUrl="https://example.com/feed.xml"/>
    </outline>
  </body>
</opml>
"""
        p = tmp_path / "in.opml"
        p.write_text(opml)
        r = _invoke(ctx, ["import-opml", str(p)])
        out = json.loads(r.output)
        assert out["status"] == "ok"
        assert out["feeds_added"] == 2
        assert out["rewritten_bridger_urls"] == 1

        with feeds_db.connect(ctx.db_path) as conn:
            urls = sorted(row["url"] for row in conn.execute("SELECT url FROM feeds"))
        assert urls == ["https://example.com/feed.xml", "tumblr:nemfrog"]

        out_path = tmp_path / "out.opml"
        r = _invoke(ctx, ["export-opml", "--output", str(out_path)])
        out = json.loads(r.output)
        assert out["status"] == "ok"
        text = out_path.read_text()
        assert "tumblr:nemfrog" in text
