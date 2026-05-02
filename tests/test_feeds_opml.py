"""Tests for OPML import/export including bridger-URL rewriting."""

from istota.feeds import db as feeds_db
from istota.feeds.opml import (
    export_opml,
    import_opml,
    rewrite_bridger_url,
)
from istota.feeds.workspace import synthesize_feeds_context


class TestRewriteBridgerUrl:
    def test_tumblr(self):
        url = "http://127.0.0.1:8900/tumblr/nemfrog/feed.xml"
        assert rewrite_bridger_url(url) == "tumblr:nemfrog"

    def test_arena(self):
        url = "http://127.0.0.1:8900/arena/cats-in-a-channel/feed.xml"
        assert rewrite_bridger_url(url) == "arena:cats-in-a-channel"

    def test_localhost_alias(self):
        url = "http://localhost:8900/tumblr/x/feed.xml"
        assert rewrite_bridger_url(url) == "tumblr:x"

    def test_https_form(self):
        url = "https://localhost:8900/tumblr/x/feed.xml"
        assert rewrite_bridger_url(url) == "tumblr:x"

    def test_default_port_omitted(self):
        url = "http://127.0.0.1/tumblr/x/feed.xml"
        assert rewrite_bridger_url(url) == "tumblr:x"

    def test_passes_real_urls_through(self):
        url = "https://example.com/feed.xml"
        assert rewrite_bridger_url(url) == url

    def test_passes_unknown_local_paths_through(self):
        url = "http://127.0.0.1:8900/something-else/x"
        assert rewrite_bridger_url(url) == url


SAMPLE_OPML = """<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>Stefan's feeds</title></head>
  <body>
    <outline text="Blogs" title="Blogs">
      <outline type="rss" text="Example" title="Example"
               xmlUrl="https://example.com/feed.xml"
               htmlUrl="https://example.com" />
    </outline>
    <outline text="Tumblr" title="Tumblr">
      <outline type="rss" text="nemfrog"
               xmlUrl="http://127.0.0.1:8900/tumblr/nemfrog/feed.xml" />
    </outline>
    <outline type="rss" text="Uncategorized"
             xmlUrl="https://other.example/atom.xml" />
  </body>
</opml>
"""


class TestImportOpml:
    def test_import_creates_categories_and_feeds(self, tmp_path):
        ctx = synthesize_feeds_context("stefan", tmp_path)
        result = import_opml(ctx, SAMPLE_OPML)
        assert result.feeds_added == 3
        assert result.categories_added == 2
        assert result.rewritten_bridger_urls == 1

        with feeds_db.connect(ctx.db_path) as conn:
            cats = {c.slug: c for c in feeds_db.list_categories(conn)}
            feeds = {f.url: f for f in feeds_db.list_feeds(conn)}
        assert "blogs" in cats
        assert "tumblr" in cats
        assert "tumblr:nemfrog" in feeds
        assert feeds["tumblr:nemfrog"].source_type == "tumblr"
        assert feeds["tumblr:nemfrog"].category_id == cats["tumblr"].id
        assert feeds["https://example.com/feed.xml"].source_type == "rss"
        assert feeds["https://other.example/atom.xml"].category_id is None

    def test_re_import_updates_in_place(self, tmp_path):
        ctx = synthesize_feeds_context("stefan", tmp_path)
        first = import_opml(ctx, SAMPLE_OPML)
        second = import_opml(ctx, SAMPLE_OPML)
        assert first.feeds_added == 3
        assert second.feeds_added == 0
        assert second.feeds_updated == 3


class TestExportOpml:
    def test_round_trips_categories_and_feeds(self, tmp_path):
        ctx = synthesize_feeds_context("stefan", tmp_path)
        import_opml(ctx, SAMPLE_OPML)
        xml = export_opml(ctx)
        assert "<opml" in xml
        assert "https://example.com/feed.xml" in xml
        # Re-importing the exported XML should be a no-op (every feed updates)
        ctx2 = synthesize_feeds_context(
            "other", tmp_path / "other_workspace",
        )
        result = import_opml(ctx2, xml)
        assert result.feeds_added == 3
        assert result.categories_added == 2
