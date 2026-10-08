"""Feed search through its index, loader and user-scoped route."""

import sqlite3
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.config import Config, UserConfig
from istota.feeds import db
from istota.feeds.routes import router, require_auth
from istota.lib.text_match import fts5_match, parse_query
from istota.modules import module_loader
from istota.search.core import SearchContext, run_search, open_with_deadline


def seed(conn, title="Falcon", body="Quiet body", author="Alice"):
    feed = db.upsert_feed(conn, url="https://example.com/feed", title="Example feed", source_type="rss",
                          site_url=None, category_id=None, poll_interval_minutes=30)
    return conn.execute(
        "INSERT INTO feed_entries(feed_id, guid, title, author, content_text, content_html, fetched_at) "
        "VALUES (?, hex(randomblob(8)), ?, ?, ?, '<p>Article</p>', '2026-01-01T00:00:00Z')",
        (feed, title, author, body),
    ).lastrowid


@pytest.fixture
def store(tmp_path):
    path = tmp_path / "feeds.db"
    db.init_db(path)
    with db.connect(path) as conn:
        yield conn


def matches(conn, query):
    return [row[0] for row in conn.execute(
        "SELECT rowid FROM feed_entries_fts WHERE feed_entries_fts MATCH ?",
        (fts5_match(parse_query(query), "strict"),),
    )]


def test_triggers_refresh_delete_and_feed_cascade(store):
    entry = seed(store)
    assert matches(store, "falc") == [entry]
    store.execute("UPDATE feed_entries SET title='Kestrel', author='Bob', content_text='New text' WHERE id=?", (entry,))
    assert matches(store, "falcon") == []
    assert matches(store, "kestrel") == [entry]
    assert matches(store, "alice") == []
    store.execute("DELETE FROM feed_entries WHERE id=?", (entry,))
    assert matches(store, "kestrel") == []
    seed(store)
    store.execute("DELETE FROM feeds")
    assert matches(store, "falcon") == []
    store.execute("INSERT INTO feed_entries_fts(feed_entries_fts, rank) VALUES ('integrity-check', 1)")


def test_upgrade_rebuilds_once_and_matches_fresh_schema(tmp_path):
    old = tmp_path / "old.db"
    db.init_db(old)
    with db.connect(old) as conn:
        for suffix in ('ai', 'ad', 'au'):
            conn.execute(f"DROP TRIGGER IF EXISTS feed_entries_fts_{suffix}")
        conn.execute("DROP TABLE IF EXISTS feed_entries_fts")
        conn.execute("UPDATE schema_meta SET value='8' WHERE key='version'")
        entry = seed(conn)
        conn.commit()
    db.init_db(old)
    with db.connect(old) as conn:
        assert matches(conn, "falcon") == [entry]
        before = conn.total_changes
        db._migrate_v8_to_v9(conn)
        assert conn.total_changes == before
        upgraded = conn.execute("SELECT name, sql FROM sqlite_master WHERE name LIKE 'feed_entries_fts%' ORDER BY name").fetchall()
    fresh = tmp_path / "fresh.db"
    db.init_db(fresh)
    with db.connect(fresh) as conn:
        assert [tuple(r) for r in upgraded] == [tuple(r) for r in conn.execute(
            "SELECT name, sql FROM sqlite_master WHERE name LIKE 'feed_entries_fts%' ORDER BY name")]


def test_ranking_snippets_and_paging(store):
    from istota.feeds.search import search_entries
    title_hit = seed(store)
    body_hit = seed(store, title="Other", body="A falcon flies")
    rows = search_entries(store, '"falcon"*', 1, 0)
    assert rows[0]["id"] == title_hit
    assert "\ue000Falcon\ue001" in rows[0]["snippet"]
    rows = search_entries(store, '"falcon"*', 1, 1)
    assert rows[0]["id"] == body_hit
    assert "\ue000falcon\ue001" in rows[0]["snippet"]


@pytest.fixture
def config(tmp_path):
    cfg = Config(db_path=tmp_path / "framework.db", nextcloud_mount_path=tmp_path / "mount",
                 users={"alice": UserConfig(), "bob": UserConfig()})
    cfg.nextcloud_mount_path.mkdir()
    return cfg


def populate(config, user):
    _, resolve, connect, _ = module_loader("feeds")
    ctx = resolve(user, config)
    db.init_db(ctx.db_path)
    with connect(ctx.db_path) as conn:
        entry = seed(conn, title=f"{user} Falcon", body="Café flight plan")
        conn.execute("UPDATE feed_entries SET starred=1")
        conn.commit()
    return ctx, entry


async def test_provider_user_scope_fallback_badges_and_hostile_queries(config):
    from istota.feeds.search import PROVIDER
    populate(config, "alice")
    populate(config, "bob")
    result = PROVIDER.run(SearchContext(config, "alice", time.monotonic()+2), parse_query("cafe"), "strict", 5, 0)
    hit = result.hits[0]
    assert hit.title == "alice Falcon"
    assert hit.subtitle == "Example feed"
    assert hit.badges == ["starred", "unread"]
    assert hit.date == "2026-01-01T00:00:00Z"
    assert hit.link == {"type": "route", "path": "/feeds/", "params": {"entry": "1"}}
    assert [hit.snippet[a:b] for a, b in hit.highlights] == ["Café"]
    data = await run_search(config, "alice", "falcon absent", sources=["feeds"])
    assert data["groups"][0]["relaxed"] is True
    assert len(data["groups"][0]["results"]) == 1
    populate(config, "alice")
    page = PROVIDER.run(SearchContext(config, "alice", time.monotonic()+2), parse_query("falcon"), "strict", 1, 0)
    assert page.has_more is True
    assert len(page.hits) == 1
    next_page = PROVIDER.run(SearchContext(config, "alice", time.monotonic()+2), parse_query("falcon"), "strict", 1, 1)
    assert next_page.has_more is False
    assert next_page.hits[0].id != page.hits[0].id
    for query in ('NEAR(a b)', 'col:x', '"', '*', 'a OR b', '^x', '-x', '(falcon'):
        data = await run_search(config, "alice", query, sources=["feeds"])
        assert all(g["error"] is None for g in data["groups"])


async def test_missing_module_db_is_empty_without_creating_it(config):
    _, resolve, _, _ = module_loader("feeds")
    path = resolve("alice", config).db_path
    data = await run_search(config, "alice", "falcon", sources=["feeds"])
    assert data["groups"][0]["results"] == []
    assert data["groups"][0]["error"] is None
    assert not path.exists()


def test_single_entry_route_is_scoped_and_uses_existing_wire_shape(config):
    _, entry = populate(config, "alice")
    bob, _ = populate(config, "bob")
    with db.connect(bob.db_path) as conn:
        other = seed(conn, title="Private article")
        conn.commit()
    app = FastAPI()
    app.state.istota_config = config
    app.include_router(router, prefix="/feeds")
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    with TestClient(app) as client:
        response = client.get(f"/feeds/entries/{entry}")
        assert response.status_code == 200
        found = response.json()
        assert found["title"] == "alice Falcon"
        assert found["content"] == "<p>Article</p>"
        assert found["feed"]["title"] == "Example feed"
        listed = client.get("/feeds").json()["entries"][0]
        assert found == listed
        assert client.get(f"/feeds/entries/{other}").status_code == 404


def test_interrupted_migration_leaves_no_partial_index(tmp_path, monkeypatch):
    path = tmp_path / "feeds.db"
    db.init_db(path)
    with db.connect(path) as conn:
        for suffix in ("ai", "ad", "au"):
            conn.execute(f"DROP TRIGGER feed_entries_fts_{suffix}")
        conn.execute("DROP TABLE feed_entries_fts")
        conn.commit()
    monkeypatch.setattr(db, "_FTS_STATEMENTS", db._FTS_STATEMENTS + (
        "WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n",
    ))
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        with open_with_deadline(lambda **options: db.connect(path, **options), time.monotonic()+0.02) as conn:
            db._migrate_v8_to_v9(conn)
    with db.connect(path) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'feed_entries_fts%'").fetchall() == []



def test_interrupted_rebuild_preserves_the_interrupt_and_can_retry(tmp_path):
    path = tmp_path / "feeds.db"
    db.init_db(path)
    with db.connect(path) as conn:
        for suffix in ("ai", "ad", "au"):
            conn.execute(f"DROP TRIGGER feed_entries_fts_{suffix}")
        conn.execute("DROP TABLE feed_entries_fts")
        for _ in range(100):
            seed(conn, body="Falcon " * 100)
        conn.commit()
        rebuilding = False
        interrupted = False

        def trace(sql):
            nonlocal rebuilding
            if "VALUES ('rebuild')" in sql:
                rebuilding = True

        def interrupt():
            nonlocal interrupted
            if rebuilding and not interrupted:
                interrupted = True
                return 1
            return 0

        conn.set_trace_callback(trace)
        conn.set_progress_handler(interrupt, 100)
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            db._migrate_v8_to_v9(conn)
        conn.set_trace_callback(None)
        conn.set_progress_handler(None, 0)
        assert conn.execute("SELECT name FROM sqlite_master WHERE name LIKE 'feed_entries_fts%'").fetchall() == []
        db._migrate_v8_to_v9(conn)
        assert len(matches(conn, "falcon")) == 100
