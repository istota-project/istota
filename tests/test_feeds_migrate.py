"""Tests for the legacy ``feeds.toml`` → SQLite importer."""

from __future__ import annotations

import logging

import pytest

from istota.feeds import db as feeds_db
from istota.feeds._migrate import (
    _DEFAULT_INTERVAL_SETTING_KEY,
    _SENTINEL_KEY,
    ensure_initialised,
    migrate_legacy_toml,
)
from istota.feeds.workspace import synthesize_feeds_context


@pytest.fixture
def ctx(tmp_path):
    c = synthesize_feeds_context("alice", tmp_path)
    c.ensure_dirs()
    return c


def _write_toml(path, body: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body)


_SAMPLE = """\
[settings]
default_poll_interval_minutes = 45

[[categories]]
slug = "blogs"
title = "Blogs"

[[feeds]]
url = "https://example.com/feed.xml"
title = "Example"
category = "blogs"

[[feeds]]
url = "tumblr:nemfrog"
poll_interval_minutes = 90
"""


class TestNoSourceFile:
    def test_returns_none_when_no_toml(self, ctx):
        result = migrate_legacy_toml(ctx)
        assert result is None
        # Sentinel must NOT be written when there's nothing to import — a
        # later toml drop-in should still be picked up.
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM schema_meta WHERE key = ?",
                (_SENTINEL_KEY,),
            ).fetchone()
            assert row is None


class TestImport:
    def test_imports_categories_and_feeds(self, ctx):
        _write_toml(ctx.config_path, _SAMPLE)
        result = migrate_legacy_toml(ctx)
        assert result is not None
        assert result["categories_added"] == 1
        assert result["feeds_added"] == 2

        with feeds_db.connect(ctx.db_path) as conn:
            urls = {r["url"] for r in conn.execute("SELECT url FROM feeds")}
            slugs = {
                r["slug"] for r in conn.execute("SELECT slug FROM feed_categories")
            }
        assert urls == {"https://example.com/feed.xml", "tumblr:nemfrog"}
        assert slugs == {"blogs"}

    def test_writes_default_interval_to_schema_meta(self, ctx):
        _write_toml(ctx.config_path, _SAMPLE)
        migrate_legacy_toml(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                (_DEFAULT_INTERVAL_SETTING_KEY,),
            ).fetchone()
            assert row is not None
            assert row["value"] == "45"

    def test_per_feed_interval_overrides_default(self, ctx):
        _write_toml(ctx.config_path, _SAMPLE)
        migrate_legacy_toml(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT poll_interval_minutes FROM feeds WHERE url = ?",
                ("tumblr:nemfrog",),
            ).fetchone()
            assert row["poll_interval_minutes"] == 90
            row = conn.execute(
                "SELECT poll_interval_minutes FROM feeds WHERE url = ?",
                ("https://example.com/feed.xml",),
            ).fetchone()
            assert row["poll_interval_minutes"] == 45

    def test_leaves_source_file_in_place(self, ctx):
        # Operators confirm via the log line and `rm` manually. The migrator
        # never deletes user data.
        _write_toml(ctx.config_path, _SAMPLE)
        migrate_legacy_toml(ctx)
        assert ctx.config_path.exists()

    def test_writes_sentinel(self, ctx):
        _write_toml(ctx.config_path, _SAMPLE)
        migrate_legacy_toml(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                (_SENTINEL_KEY,),
            ).fetchone()
            assert row is not None


class TestIdempotence:
    def test_second_run_is_noop(self, ctx):
        _write_toml(ctx.config_path, _SAMPLE)
        first = migrate_legacy_toml(ctx)
        assert first is not None
        # Recreate the file (simulating a stale operator-saved copy).
        _write_toml(ctx.config_path, _SAMPLE)
        second = migrate_legacy_toml(ctx)
        assert second is None
        with feeds_db.connect(ctx.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM feeds").fetchone()["c"]
            assert count == 2  # not duplicated

    def test_skips_when_db_already_populated_without_sentinel(self, ctx, caplog):
        """If a user has rows from `feeds add` (no migration ran yet) and
        a stray toml is present, we must NOT overwrite — log a warning and bail.
        """
        feeds_db.init_db(ctx.db_path)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.upsert_feed(
                conn,
                url="https://added-via-cli.test/feed",
                title=None, site_url=None, source_type="rss",
                category_id=None, poll_interval_minutes=30,
            )
            conn.commit()
        _write_toml(ctx.config_path, _SAMPLE)

        with caplog.at_level(logging.WARNING):
            result = migrate_legacy_toml(ctx)
        assert result is None
        assert any(
            "feeds_legacy_toml_present_but_db_populated" in rec.message
            for rec in caplog.records
        )
        # Sentinel NOT written — later cleanup or merge can still re-attempt.
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM schema_meta WHERE key = ?",
                (_SENTINEL_KEY,),
            ).fetchone()
            assert row is None

    def test_warns_when_stale_toml_after_successful_import(self, ctx, caplog):
        """After import, an operator may drop a fresh feeds.toml back. The
        sentinel should suppress re-import; we should log so the operator
        notices the file is dead."""
        _write_toml(ctx.config_path, _SAMPLE)
        migrate_legacy_toml(ctx)
        # Operator drops a new file.
        _write_toml(ctx.config_path, _SAMPLE)
        with caplog.at_level(logging.WARNING):
            assert migrate_legacy_toml(ctx) is None
        assert any(
            "feeds_legacy_toml_present_but_already_imported" in rec.message
            for rec in caplog.records
        )


class TestSearchPaths:
    def test_picks_data_config_first(self, ctx, tmp_path):
        # data_dir/config/feeds.toml — the primary location.
        primary = ctx.data_dir / "config" / "feeds.toml"
        _write_toml(primary, _SAMPLE)
        # workspace_root/config/feeds.toml — secondary.
        secondary = tmp_path / "config" / "feeds.toml"
        _write_toml(secondary, "[[feeds]]\nurl = \"https://other/feed\"\n")

        result = migrate_legacy_toml(ctx)
        assert result is not None
        assert result["path"] == str(primary)
        with feeds_db.connect(ctx.db_path) as conn:
            urls = {r["url"] for r in conn.execute("SELECT url FROM feeds")}
        assert urls == {"https://example.com/feed.xml", "tumblr:nemfrog"}

    def test_falls_back_to_workspace_config(self, tmp_path):
        # Skip the default config_path resolution by pointing config_path at
        # a non-existent file, then drop the toml at workspace_root/config.
        ctx = synthesize_feeds_context(
            "alice", tmp_path,
            config_path=tmp_path / "noexist" / "feeds.toml",
        )
        ctx.ensure_dirs()
        secondary = tmp_path / "config" / "feeds.toml"
        _write_toml(
            secondary,
            "[[feeds]]\nurl = \"https://only-here/feed\"\n",
        )
        result = migrate_legacy_toml(ctx)
        assert result is not None
        assert result["path"] == str(secondary)


class TestParseErrors:
    def test_unparseable_toml_does_not_raise(self, ctx, caplog):
        _write_toml(ctx.config_path, "not = valid = toml = ===\n")
        with caplog.at_level(logging.WARNING):
            assert migrate_legacy_toml(ctx) is None
        # No sentinel — a fix-and-retry should succeed later.
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM schema_meta WHERE key = ?", (_SENTINEL_KEY,),
            ).fetchone()
            assert row is None


class TestEnsureInitialised:
    def test_runs_init_and_migration(self, ctx):
        _write_toml(ctx.config_path, _SAMPLE)
        ensure_initialised(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM feeds").fetchone()["c"]
        assert count == 2

    def test_idempotent(self, ctx):
        _write_toml(ctx.config_path, _SAMPLE)
        ensure_initialised(ctx)
        ensure_initialised(ctx)  # must not crash, must not re-import
        with feeds_db.connect(ctx.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM feeds").fetchone()["c"]
        assert count == 2
