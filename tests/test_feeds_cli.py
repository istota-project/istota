"""Tests for the native feeds Click CLI."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from istota.feeds import db as feeds_db
from istota.feeds._config_io import read_feeds_config, write_feeds_config
from istota.feeds.cli import cli
from istota.feeds.workspace import synthesize_feeds_context


@pytest.fixture
def ctx(tmp_path):
    fctx = synthesize_feeds_context("alice", tmp_path)
    fctx.ensure_dirs()
    return fctx


def _invoke(ctx, args):
    runner = CliRunner()
    return runner.invoke(cli, args, obj=ctx, standalone_mode=False, catch_exceptions=False)


def _seed_config(ctx, feeds=None, categories=None):
    write_feeds_config(ctx.config_path, {
        "settings": {"default_poll_interval_minutes": 30},
        "categories": categories or [],
        "feeds": feeds or [],
    })


# ---------------------------------------------------------------------------
# list / categories / entries
# ---------------------------------------------------------------------------


class TestList:
    def test_empty(self, ctx):
        _seed_config(ctx)
        r = _invoke(ctx, ["list"])
        assert r.exit_code == 0
        out = json.loads(r.output)
        assert out["status"] == "ok"
        assert out["count"] == 0
        assert out["feeds"] == []

    def test_syncs_config_to_db(self, ctx):
        _seed_config(
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
        _seed_config(ctx, feeds=[{"url": "tumblr:nemfrog"}, {"url": "arena:cats"}])
        r = _invoke(ctx, ["list"])
        out = json.loads(r.output)
        types = {f["url"]: f["source_type"] for f in out["feeds"]}
        assert types["tumblr:nemfrog"] == "tumblr"
        assert types["arena:cats"] == "arena"


class TestCategories:
    def test_lists_categories(self, ctx):
        _seed_config(ctx, categories=[
            {"slug": "blogs", "title": "Blogs"},
            {"slug": "art", "title": "Art"},
        ])
        r = _invoke(ctx, ["categories"])
        out = json.loads(r.output)
        slugs = {c["slug"] for c in out["categories"]}
        assert slugs == {"blogs", "art"}


class TestEntries:
    def test_filters_by_status(self, ctx):
        _seed_config(ctx, feeds=[{"url": "https://x.test/feed"}])
        # populate one read + one unread entry directly
        _invoke(ctx, ["list"])  # syncs the feed into the db
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
    def test_adds_feed_and_writes_config(self, ctx):
        _seed_config(ctx)
        r = _invoke(ctx, [
            "add", "--url", "https://example.com/feed.xml",
            "--title", "Example", "--category", "blogs",
        ])
        out = json.loads(r.output)
        assert out["status"] == "ok"
        cfg = read_feeds_config(ctx.config_path)
        assert cfg["feeds"][0]["url"] == "https://example.com/feed.xml"
        assert cfg["feeds"][0]["category"] == "blogs"
        slugs = [c["slug"] for c in cfg["categories"]]
        assert "blogs" in slugs

    def test_duplicate_returns_error(self, ctx):
        _seed_config(ctx, feeds=[{"url": "https://x/feed"}])
        r = _invoke(ctx, ["add", "--url", "https://x/feed"])
        out = json.loads(r.output)
        assert out["status"] == "error"
        assert "already exists" in out["error"]


class TestRemove:
    def test_removes_by_url(self, ctx):
        _seed_config(ctx, feeds=[
            {"url": "https://a/feed"}, {"url": "https://b/feed"},
        ])
        _invoke(ctx, ["list"])  # sync to db
        r = _invoke(ctx, ["remove", "--url", "https://a/feed"])
        out = json.loads(r.output)
        assert out["status"] == "ok"
        assert out["removed_url"] == "https://a/feed"

        cfg = read_feeds_config(ctx.config_path)
        urls = [f["url"] for f in cfg["feeds"]]
        assert urls == ["https://b/feed"]

    def test_removes_by_id(self, ctx):
        _seed_config(ctx, feeds=[{"url": "https://x/feed"}])
        _invoke(ctx, ["list"])
        with feeds_db.connect(ctx.db_path) as conn:
            feed = feeds_db.get_feed_by_url(conn, "https://x/feed")
        r = _invoke(ctx, ["remove", "--id", str(feed.id)])
        out = json.loads(r.output)
        assert out["removed_url"] == "https://x/feed"

    def test_no_args_errors(self, ctx):
        _seed_config(ctx)
        r = _invoke(ctx, ["remove"])
        out = json.loads(r.output)
        assert out["status"] == "error"


# ---------------------------------------------------------------------------
# refresh / poll
# ---------------------------------------------------------------------------


class TestRefresh:
    def test_clears_next_poll_at(self, ctx):
        _seed_config(ctx, feeds=[{"url": "https://x/feed"}])
        _invoke(ctx, ["list"])
        # Set a future next_poll_at
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
        _seed_config(ctx, feeds=[{"url": "https://x.test/feed"}])
        _invoke(ctx, ["list"])

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

        # Replace httpx.get to make the cli's poll_due_feeds → poll_feed → _poll_rss → http_get default work.
        import httpx
        monkeypatch.setattr(httpx, "get", stub_get)

        r = _invoke(ctx, ["poll"])
        out = json.loads(r.output)
        assert out["status"] == "ok"
        assert out["polled"] == 1
        assert out["new_entries"] == 1

    def test_run_scheduled_polls_due_feeds(self, ctx, monkeypatch):
        _seed_config(ctx, feeds=[{"url": "https://y.test/feed"}])
        _invoke(ctx, ["list"])

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
        _seed_config(ctx, feeds=[
            {"url": "https://ok.test/feed"},
            {"url": "https://bad.test/feed"},
        ])
        _invoke(ctx, ["list"])

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
        # Partial errors don't fail the CLI exit code (only status="error" does).
        assert r.exit_code == 0

    def test_error_when_all_feeds_fail(self, ctx, monkeypatch):
        _seed_config(ctx, feeds=[{"url": "https://bad.test/feed"}])
        _invoke(ctx, ["list"])

        import httpx

        def stub_get(*a, **kw):
            raise httpx.ConnectError("network down")

        monkeypatch.setattr(httpx, "get", stub_get)

        # standalone_mode=False + catch_exceptions=False — _output calls
        # sys.exit(1) on status="error", which surfaces as SystemExit.
        runner = CliRunner()
        r = runner.invoke(cli, ["poll"], obj=ctx, standalone_mode=False)
        assert r.exit_code == 1
        out = json.loads(r.output)
        assert out["status"] == "error"
        assert out["polled"] == 1
        assert out["errors"] == 1
        assert "all 1 feed poll(s) failed" in out["error"]


# ---------------------------------------------------------------------------
# OPML
# ---------------------------------------------------------------------------


class TestStar:
    def _seed_one(self, ctx):
        _seed_config(ctx, feeds=[{"url": "https://x.test/feed"}])
        _invoke(ctx, ["list"])  # syncs into DB
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
        _seed_config(ctx)
        r = _invoke(ctx, ["star"])
        out = json.loads(r.output)
        assert out["status"] == "error"


class TestMarkRead:
    def _seed_two_feeds(self, ctx):
        _seed_config(ctx, feeds=[
            {"url": "https://x.test/feed", "category": "blogs"},
            {"url": "https://y.test/feed"},
        ], categories=[{"slug": "blogs", "title": "Blogs"}])
        _invoke(ctx, ["list"])
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
        _seed_config(ctx)
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
        assert out["wrote_config"] is True

        cfg = read_feeds_config(ctx.config_path)
        urls = sorted(f["url"] for f in cfg["feeds"])
        assert urls == ["https://example.com/feed.xml", "tumblr:nemfrog"]

        out_path = tmp_path / "out.opml"
        r = _invoke(ctx, ["export-opml", "--output", str(out_path)])
        out = json.loads(r.output)
        assert out["status"] == "ok"
        text = out_path.read_text()
        assert "tumblr:nemfrog" in text
