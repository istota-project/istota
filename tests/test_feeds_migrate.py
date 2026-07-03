"""Tests for the legacy ``feeds.toml`` → SQLite importer."""

from __future__ import annotations

import logging

import pytest

from istota.feeds import db as feeds_db
from istota.feeds._migrate import (
    _DEFAULT_INTERVAL_SETTING_KEY,
    _DEFAULTS_SENTINEL_KEY,
    _SENTINEL_KEY,
    ensure_initialised,
    migrate_legacy_toml,
    seed_default_opml,
)
from istota.feeds.workspace import synthesize_feeds_context


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    monkeypatch.delenv("ISTOTA_FEEDS_SKIP_DEFAULT_SEED", raising=False)
    c = synthesize_feeds_context("alice", tmp_path)
    c.ensure_dirs()
    return c


def _legacy_path(ctx):
    """The primary location the migrator searches first."""
    return ctx.data_dir / "config" / "feeds.toml"


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
        _write_toml(_legacy_path(ctx), _SAMPLE)
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
        _write_toml(_legacy_path(ctx), _SAMPLE)
        migrate_legacy_toml(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            assert feeds_db.get_default_poll_interval(conn) == 45

    def test_per_feed_interval_overrides_default(self, ctx):
        _write_toml(_legacy_path(ctx), _SAMPLE)
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
        # Operators confirm via the log line and rm manually. The migrator
        # never deletes user data.
        _write_toml(_legacy_path(ctx), _SAMPLE)
        migrate_legacy_toml(ctx)
        assert _legacy_path(ctx).exists()

    def test_writes_sentinel(self, ctx):
        _write_toml(_legacy_path(ctx), _SAMPLE)
        migrate_legacy_toml(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                (_SENTINEL_KEY,),
            ).fetchone()
            assert row is not None


class TestIdempotence:
    def test_second_run_is_noop(self, ctx):
        _write_toml(_legacy_path(ctx), _SAMPLE)
        first = migrate_legacy_toml(ctx)
        assert first is not None
        # Recreate the file (simulating a stale operator-saved copy).
        _write_toml(_legacy_path(ctx), _SAMPLE)
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
        _write_toml(_legacy_path(ctx), _SAMPLE)

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
        _write_toml(_legacy_path(ctx), _SAMPLE)
        migrate_legacy_toml(ctx)
        # Operator drops a new file.
        _write_toml(_legacy_path(ctx), _SAMPLE)
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

    def test_falls_back_to_workspace_config(self, ctx, tmp_path):
        # No primary; only the workspace-root fallback.
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
        _write_toml(_legacy_path(ctx), "not = valid = toml = ===\n")
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
        _write_toml(_legacy_path(ctx), _SAMPLE)
        ensure_initialised(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM feeds").fetchone()["c"]
        assert count == 2

    def test_idempotent(self, ctx):
        _write_toml(_legacy_path(ctx), _SAMPLE)
        ensure_initialised(ctx)
        ensure_initialised(ctx)  # must not crash, must not re-import
        with feeds_db.connect(ctx.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM feeds").fetchone()["c"]
        assert count == 2


_OVERRIDE_OPML = """\
<?xml version="1.0" encoding="UTF-8"?>
<opml version="2.0">
  <head><title>override</title></head>
  <body>
    <outline text="news" title="news">
      <outline type="rss" text="Override Feed"
        xmlUrl="https://override.example.com/feed.xml"
        htmlUrl="https://override.example.com/" />
    </outline>
  </body>
</opml>
"""


class TestSeedDefaultOpml:
    def test_seeds_bundled_defaults_into_empty_db(self, ctx):
        result = seed_default_opml(ctx)
        assert result is not None
        assert result["feeds_added"] >= 1
        with feeds_db.connect(ctx.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM feeds").fetchone()["c"]
        assert count >= 1

    def test_writes_sentinel_on_success(self, ctx):
        seed_default_opml(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = ?",
                (_DEFAULTS_SENTINEL_KEY,),
            ).fetchone()
            assert row is not None

    def test_second_run_is_noop(self, ctx):
        first = seed_default_opml(ctx)
        assert first is not None
        with feeds_db.connect(ctx.db_path) as conn:
            count1 = conn.execute("SELECT COUNT(*) AS c FROM feeds").fetchone()["c"]
        second = seed_default_opml(ctx)
        assert second is None
        with feeds_db.connect(ctx.db_path) as conn:
            count2 = conn.execute("SELECT COUNT(*) AS c FROM feeds").fetchone()["c"]
        assert count1 == count2

    def test_skips_when_db_already_populated(self, ctx):
        feeds_db.init_db(ctx.db_path)
        with feeds_db.connect(ctx.db_path) as conn:
            feeds_db.upsert_feed(
                conn,
                url="https://added-via-cli.test/feed",
                title=None, site_url=None, source_type="rss",
                category_id=None, poll_interval_minutes=30,
            )
            conn.commit()
        result = seed_default_opml(ctx)
        assert result is None
        # Sentinel still written so we don't re-check on every boot.
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM schema_meta WHERE key = ?",
                (_DEFAULTS_SENTINEL_KEY,),
            ).fetchone()
            assert row is not None
            urls = {r["url"] for r in conn.execute("SELECT url FROM feeds")}
        # User's own feed left intact.
        assert urls == {"https://added-via-cli.test/feed"}

    def test_per_user_override_wins_over_bundled(self, ctx):
        override = ctx.data_dir / "config" / "feeds-defaults.opml"
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(_OVERRIDE_OPML)
        result = seed_default_opml(ctx)
        assert result is not None
        assert result["path"] == str(override)
        with feeds_db.connect(ctx.db_path) as conn:
            urls = {r["url"] for r in conn.execute("SELECT url FROM feeds")}
        assert urls == {"https://override.example.com/feed.xml"}

    def test_workspace_override_beats_bundled(self, ctx, tmp_path):
        # data_dir.parent is the workspace root (synthesize_feeds_context layout).
        override = tmp_path / "config" / "feeds-defaults.opml"
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(_OVERRIDE_OPML)
        result = seed_default_opml(ctx)
        assert result is not None
        assert result["path"] == str(override)
        with feeds_db.connect(ctx.db_path) as conn:
            urls = {r["url"] for r in conn.execute("SELECT url FROM feeds")}
        assert urls == {"https://override.example.com/feed.xml"}

    def test_data_dir_override_wins_over_workspace_override(self, ctx, tmp_path):
        primary = ctx.data_dir / "config" / "feeds-defaults.opml"
        primary.parent.mkdir(parents=True, exist_ok=True)
        primary.write_text(_OVERRIDE_OPML)
        secondary = tmp_path / "config" / "feeds-defaults.opml"
        secondary.parent.mkdir(parents=True, exist_ok=True)
        secondary.write_text(
            '<?xml version="1.0"?><opml version="2.0"><body>'
            '<outline type="rss" xmlUrl="https://wrong.example.com/feed.xml" />'
            '</body></opml>'
        )
        result = seed_default_opml(ctx)
        assert result is not None
        assert result["path"] == str(primary)
        with feeds_db.connect(ctx.db_path) as conn:
            urls = {r["url"] for r in conn.execute("SELECT url FROM feeds")}
        assert urls == {"https://override.example.com/feed.xml"}


class TestSeedDefaultOpmlFootguns:
    """Behaviour around the lockout cases scully flagged: a parse error
    or a missing bundled file must not write the sentinel — fixing the
    underlying issue should unblock seeding without operator surgery on
    ``schema_meta``."""

    def test_parse_error_does_not_lock_out(self, ctx, caplog):
        # Per-user override wins over bundled, so a malformed override
        # actually exercises the import-error branch.
        override = ctx.data_dir / "config" / "feeds-defaults.opml"
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text("not valid xml at all <<<>>>")
        with caplog.at_level(logging.WARNING):
            assert seed_default_opml(ctx) is None
        assert any(
            "feeds_default_opml_import_failed" in rec.message
            for rec in caplog.records
        )
        # No sentinel — fixing the override file should let seeding run.
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM schema_meta WHERE key = ?",
                (_DEFAULTS_SENTINEL_KEY,),
            ).fetchone()
            assert row is None

        # Replace with a valid override and re-run.
        override.write_text(_OVERRIDE_OPML)
        result = seed_default_opml(ctx)
        assert result is not None
        with feeds_db.connect(ctx.db_path) as conn:
            urls = {r["url"] for r in conn.execute("SELECT url FROM feeds")}
        assert urls == {"https://override.example.com/feed.xml"}

    def test_missing_bundled_does_not_lock_out(self, ctx, monkeypatch):
        # Simulate a build that ships without the bundled OPML and no
        # operator override on disk.
        from istota.feeds import _migrate as mig
        monkeypatch.setattr(mig, "_read_bundled_defaults_opml", lambda: None)

        result = seed_default_opml(ctx)
        assert result is None
        with feeds_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT 1 FROM schema_meta WHERE key = ?",
                (_DEFAULTS_SENTINEL_KEY,),
            ).fetchone()
            assert row is None

        # Operator drops a file later — seeding picks it up.
        override = ctx.data_dir / "config" / "feeds-defaults.opml"
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(_OVERRIDE_OPML)
        result = seed_default_opml(ctx)
        assert result is not None
        with feeds_db.connect(ctx.db_path) as conn:
            urls = {r["url"] for r in conn.execute("SELECT url FROM feeds")}
        assert urls == {"https://override.example.com/feed.xml"}


class TestSeedDefaultOpmlWorkspaceRoot:
    """Operator overrides at the workspace level must work even when
    ``data_dir`` is overridden to a non-workspace path."""

    def test_explicit_workspace_root_used_when_data_dir_is_remote(
        self, tmp_path, monkeypatch,
    ):
        from istota.feeds.workspace import synthesize_feeds_context

        monkeypatch.delenv("ISTOTA_FEEDS_SKIP_DEFAULT_SEED", raising=False)

        workspace = tmp_path / "workspace"
        remote_data = tmp_path / "remote-data" / "feeds-store"
        workspace.mkdir()
        ctx = synthesize_feeds_context(
            "alice",
            workspace,
            data_dir=remote_data,
            db_path=remote_data / "feeds.db",
        )
        ctx.ensure_dirs()

        override = workspace / "config" / "feeds-defaults.opml"
        override.parent.mkdir(parents=True, exist_ok=True)
        override.write_text(_OVERRIDE_OPML)

        result = seed_default_opml(ctx)
        assert result is not None
        assert result["path"] == str(override)
        with feeds_db.connect(ctx.db_path) as conn:
            urls = {r["url"] for r in conn.execute("SELECT url FROM feeds")}
        assert urls == {"https://override.example.com/feed.xml"}


class TestEnsureInitialisedSeedsDefaults:
    def test_seeds_when_no_legacy_toml(self, ctx):
        ensure_initialised(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM feeds").fetchone()["c"]
            sentinel = conn.execute(
                "SELECT 1 FROM schema_meta WHERE key = ?",
                (_DEFAULTS_SENTINEL_KEY,),
            ).fetchone()
        assert count >= 1
        assert sentinel is not None

    def test_legacy_toml_takes_precedence_over_defaults(self, ctx):
        # When the operator already has a populated TOML, the migration
        # imports it and the default OPML must not pile on more entries.
        _write_toml(_legacy_path(ctx), _SAMPLE)
        ensure_initialised(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) AS c FROM feeds").fetchone()["c"]
        assert count == 2  # only the TOML rows


class TestBackfillImageDedup:
    """Heal entries stored before the image-dedup poller change.

    Old rows keep the hero image embedded in ``content_html`` (the poller
    used ``INSERT OR IGNORE`` and never rewrote them), so the reader paints
    it twice — once as the hero, once inside the body. The backfill drops
    every ``image_urls`` member from ``content_html``.
    """

    def _seed_entry(self, ctx, *, guid, content_html, image_urls):
        feeds_db.init_db(ctx.db_path)
        with feeds_db.connect(ctx.db_path) as conn:
            feed_id = feeds_db.upsert_feed(
                conn, url="https://xkcd.com/rss.xml", title="xkcd",
                site_url=None, source_type="rss", category_id=None,
                poll_interval_minutes=180,
            )
            conn.execute(
                """
                INSERT INTO feed_entries(
                    feed_id, guid, title, url, author, content_html,
                    content_text, image_urls, published_at, fetched_at, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    feed_id, guid, "Holes", "https://xkcd.com/3266/", None,
                    content_html, "body text",
                    __import__("json").dumps(image_urls),
                    "2026-07-01T04:00:00+00:00", "2026-07-01T05:00:00+00:00",
                    "unread",
                ),
            )
            conn.commit()
        return feed_id

    def test_strips_hero_image_from_stale_body(self, ctx):
        from istota.feeds._migrate import backfill_image_dedup

        img = "https://imgs.xkcd.com/comics/holes.png"
        self._seed_entry(
            ctx,
            guid="https://xkcd.com/3266/",
            content_html=f'<img src="{img}" alt="Holes" />',
            image_urls=[img],
        )
        result = backfill_image_dedup(ctx)
        assert result is not None
        assert result["entries_updated"] == 1
        with feeds_db.connect(ctx.db_path) as conn:
            entries = feeds_db.list_entries(conn)
        assert img not in (entries[0].content_html or "")
        # Hero image is untouched — still available for the reader hero.
        assert entries[0].image_urls == [img]

    def test_collapses_resolution_variant_in_body(self, ctx):
        from istota.feeds._migrate import backfill_image_dedup

        hero = "https://cdn.example.com/photo.jpg?width=700"
        body_variant = "https://cdn.example.com/photo.jpg?width=140"
        self._seed_entry(
            ctx,
            guid="g1",
            content_html=f'<p>Text</p><img src="{body_variant}" />',
            image_urls=[hero],
        )
        result = backfill_image_dedup(ctx)
        assert result["entries_updated"] == 1
        with feeds_db.connect(ctx.db_path) as conn:
            html = feeds_db.list_entries(conn)[0].content_html
        assert "photo.jpg" not in (html or "")
        assert "Text" in html

    def test_leaves_genuine_inline_image_alone(self, ctx):
        from istota.feeds._migrate import backfill_image_dedup

        hero = "https://img/hero.jpg"
        inline = "https://img/midarticle.jpg"
        self._seed_entry(
            ctx,
            guid="g2",
            content_html=f'<img src="{hero}" /><p>body</p><img src="{inline}" />',
            image_urls=[hero],
        )
        backfill_image_dedup(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            html = feeds_db.list_entries(conn)[0].content_html
        assert hero not in html
        assert inline in html

    def test_is_idempotent_via_sentinel(self, ctx):
        from istota.feeds._migrate import backfill_image_dedup

        img = "https://img/x.jpg"
        self._seed_entry(
            ctx, guid="g3",
            content_html=f'<img src="{img}" />', image_urls=[img],
        )
        first = backfill_image_dedup(ctx)
        assert first["entries_updated"] == 1
        second = backfill_image_dedup(ctx)
        assert second is None  # sentinel set → no-op

    def test_no_op_when_nothing_to_strip(self, ctx):
        from istota.feeds._migrate import backfill_image_dedup

        self._seed_entry(
            ctx, guid="g4",
            content_html="<p>just text, no images</p>", image_urls=[],
        )
        result = backfill_image_dedup(ctx)
        # Runs (writes sentinel), but touches zero rows.
        assert result is not None
        assert result["entries_updated"] == 0

    def test_ensure_initialised_runs_backfill(self, ctx):
        from istota.feeds._migrate import _BACKFILL_SENTINEL_KEY

        ensure_initialised(ctx)
        with feeds_db.connect(ctx.db_path) as conn:
            sentinel = conn.execute(
                "SELECT 1 FROM schema_meta WHERE key = ?",
                (_BACKFILL_SENTINEL_KEY,),
            ).fetchone()
        assert sentinel is not None
