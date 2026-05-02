"""Tests for [feeds] config parsing + the backend-aware briefing dispatcher."""

from __future__ import annotations

import pytest

from istota.config import Config, FeedsConfig, ResourceConfig, UserConfig, load_config
from istota.feeds import fetch_briefing_entries


class TestFeedsConfigParsing:
    def test_default_backend_is_miniflux(self):
        assert FeedsConfig().backend == "miniflux"
        assert Config().feeds.backend == "miniflux"

    def test_load_native_backend(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('[feeds]\nbackend = "native"\n')
        cfg = load_config(p)
        assert cfg.feeds.backend == "native"

    def test_load_miniflux_backend_explicit(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('[feeds]\nbackend = "miniflux"\n')
        cfg = load_config(p)
        assert cfg.feeds.backend == "miniflux"

    def test_unknown_backend_rejected(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('[feeds]\nbackend = "rubbish"\n')
        with pytest.raises(ValueError, match="backend must be"):
            load_config(p)

    def test_no_section_keeps_default(self, tmp_path):
        p = tmp_path / "config.toml"
        p.write_text('db_path = "x.db"\n')
        cfg = load_config(p)
        assert cfg.feeds.backend == "miniflux"


class TestFetchBriefingEntriesDispatch:
    def test_returns_empty_when_no_user(self):
        cfg = Config()
        assert fetch_briefing_entries("nobody", cfg) == []

    def test_miniflux_backend_calls_http_client(self, monkeypatch):
        cfg = Config()
        cfg.feeds = FeedsConfig(backend="miniflux")
        cfg.users = {
            "stefan": UserConfig(resources=[
                ResourceConfig(
                    type="miniflux", base_url="https://m.example.com",
                    api_key="abc",
                ),
            ]),
        }

        captured = {}

        def fake_fetch(base_url, api_key, limit=500):
            captured["base_url"] = base_url
            captured["api_key"] = api_key
            captured["limit"] = limit
            return [{
                "id": 7, "title": "T", "url": "u", "content": "<p>x</p>",
                "feed": {"title": "F"}, "enclosures": None,
                "published_at": "2026-05-01T00:00:00Z",
                "created_at": "2026-05-02T00:00:00Z",
            }]

        # Patch the symbol that fetch_briefing_entries actually calls.
        monkeypatch.setattr("istota.feeds.fetch_miniflux_entries", fake_fetch)

        items = fetch_briefing_entries("stefan", cfg, limit=10)
        assert captured == {
            "base_url": "https://m.example.com", "api_key": "abc", "limit": 10,
        }
        assert len(items) == 1
        assert items[0].title == "T"
        assert items[0].user_id == "stefan"

    def test_native_backend_reads_from_sqlite(self, tmp_path, monkeypatch):
        from istota.feeds import db as feeds_db
        from istota.feeds.models import EntryRecord
        from istota.feeds.workspace import synthesize_feeds_context

        ctx = synthesize_feeds_context("stefan", tmp_path)
        ctx.ensure_dirs()
        feeds_db.init_db(ctx.db_path)
        with feeds_db.connect(ctx.db_path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url="https://example.com/feed.xml", title="Example",
                site_url=None, source_type="rss", category_id=None,
                poll_interval_minutes=30,
            )
            feeds_db.insert_entries(conn, feed_id, [
                EntryRecord(
                    id=0, feed_id=feed_id, guid="g1", title="Hello",
                    url="https://example.com/1", author="Alice",
                    content_html="<p>hi</p>", content_text="hi",
                    image_urls=["https://img/x.jpg"],
                    published_at="2026-05-01T00:00:00+00:00",
                    fetched_at="2026-05-02T00:00:00+00:00",
                    status="unread",
                ),
            ])
            conn.commit()

        cfg = Config()
        cfg.feeds = FeedsConfig(backend="native")
        cfg.users = {"stefan": UserConfig(resources=[ResourceConfig(type="feeds")])}

        # _native_briefing imported the loader symbol at module load — patch the
        # binding it actually uses.
        monkeypatch.setattr(
            "istota.feeds._native_briefing.resolve_for_user",
            lambda user_id, _cfg: ctx,
        )

        items = fetch_briefing_entries("stefan", cfg)
        assert len(items) == 1
        assert items[0].title == "Hello"
        assert items[0].feed_name == "Example"
        assert items[0].image_url == "https://img/x.jpg"

    def test_no_miniflux_resource_returns_empty(self):
        cfg = Config()
        cfg.feeds = FeedsConfig(backend="miniflux")
        cfg.users = {"stefan": UserConfig()}
        assert fetch_briefing_entries("stefan", cfg) == []
