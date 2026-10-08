"""Federated search policy and its authenticated route."""

import sqlite3
import time
from unittest.mock import Mock

import pytest

from istota.search.core import Provider, ProviderResult, SearchHit, open_with_deadline, run_search


def hit():
    return SearchHit("fake:1", "fact", "Falcon", None, "falcon", [[0, 6]], None, None, [])


def provider(name, run, **kwargs):
    return Provider(name, name.title(), kwargs.pop("order", 0), kwargs.pop("module", None),
                    kwargs.pop("timeout_s", 1.5), kwargs.pop("on_demand", False), run)


async def test_selection_fallback_and_pagination(monkeypatch):
    calls = []
    def run(ctx, terms, mode, limit, offset):
        calls.append((mode, limit, offset))
        return ProviderResult([] if mode == "strict" else [hit()], True)
    disabled = Mock(side_effect=AssertionError("disabled provider ran"))
    demand = Mock(return_value=ProviderResult([hit()], False))
    monkeypatch.setattr("istota.search.registry.providers", lambda: [
        provider("fake", run), provider("disabled", disabled, module="health"),
        provider("money", demand, on_demand=True),
    ])
    config = Mock()
    config.db_path = None
    config.is_module_enabled.return_value = False
    result = await run_search(config, "alice", "falcon invoice", limit=20, offset=0)
    assert calls == [("strict", 5, 0), ("relaxed", 5, 0)]
    assert result["groups"][0]["relaxed"] is True
    assert result["groups"][0]["has_more"] is True
    assert result["on_demand"] == [{"source": "money", "label": "Money"}]
    demand.assert_not_called()
    result = await run_search(config, "alice", "falcon", sources=["money", "unknown"], limit=20, offset=4)
    assert [g["source"] for g in result["groups"]] == ["money"]
    assert demand.call_args.args[3:] == (20, 4)
    assert await run_search(config, "alice", "**") == {"query": "**", "groups": []}


async def test_sql_deadline_and_failure_are_isolated(monkeypatch, caplog):
    closed = []
    def slow(ctx, *args):
        conn = sqlite3.connect(":memory:", check_same_thread=False)
        closed.append(conn)
        with open_with_deadline(lambda **options: conn, ctx.deadline) as c:
            c.execute("WITH RECURSIVE n(x) AS (VALUES(0) UNION ALL SELECT x+1 FROM n WHERE x<100000000) SELECT sum(x) FROM n").fetchone()
        return ProviderResult([], False)
    def broken(*args):
        raise ValueError("private query content")
    monkeypatch.setattr("istota.search.registry.providers", lambda: [
        provider("slow", slow, timeout_s=0.05), provider("broken", broken),
        provider("good", lambda *args: ProviderResult([hit()], False)),
    ])
    start = time.monotonic()
    result = await run_search(Mock(), "alice", "private query content")
    assert time.monotonic() - start < 0.55
    assert [g["error"] for g in result["groups"]] == ["timeout", "failed", None]
    assert "private query content" not in caplog.text
    with pytest.raises(sqlite3.ProgrammingError):
        closed[0].execute("SELECT 1")


async def test_route_scopes_real_framework_sources(tmp_path, monkeypatch):
    import json
    from unittest.mock import AsyncMock
    from httpx import ASGITransport, AsyncClient
    from istota import db
    from istota.search import registry
    from tests.test_web_app import _make_config, _patch_app
    from tests.test_search_framework_providers import room, chunk
    import istota.webui.app as web

    config = _make_config(tmp_path)
    app = _patch_app(config)
    from istota.search.framework import PROVIDERS
    monkeypatch.setattr(registry, "providers", lambda: PROVIDERS)
    with db.get_db(config.db_path) as conn:
        for user in ("alice", "bob"):
            room(conn, user + "room", user, "falcon " + user)
            db.add_message(conn, user + "room", role="user", body="falcon message " + user, origin_surface="web", author_user_id=user)
            chunk(conn, user, "memory_file", "falcon memory " + user)
            conn.execute("INSERT INTO knowledge_facts (user_id,subject,predicate,object) VALUES (?, 'falcon', 'owner', ?)", (user, user))
    web._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": "alice"})
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://example.com") as client:
        assert (await client.get("/istota/api/search?q=falcon")).status_code == 401
        assert (await client.get("/istota/callback", follow_redirects=False)).status_code == 302
        response = await client.get("/istota/api/search?q=falcon&user_id=bob")
        assert response.status_code == 200
        data = response.json()
        assert [g["source"] for g in data["groups"]] == ["chats", "rooms", "memory", "facts"]
        assert all(len(g["results"]) == 1 and g["error"] is None for g in data["groups"])
        assert "bob" not in json.dumps(data)
        for params in ({"q": "x" * 201}, {"limit": 26}, {"offset": 501}, {"limit": 0}):
            assert (await client.get("/istota/api/search", params=params)).status_code == 422
        assert (await client.get("/istota/api/search")).json()["groups"] == []
        for query in ('NEAR(a b)', 'col:x', '"', '*', 'a OR b', '^x', '-x', '(falcon'):
            data = (await client.get("/istota/api/search", params={"q": query})).json()
            assert all(g["error"] is None for g in data["groups"])


async def test_empty_later_strict_page_does_not_relax(monkeypatch):
    calls = []
    def run(ctx, terms, mode, limit, offset):
        calls.append((mode, offset))
        return ProviderResult([hit()] if offset == 0 else [], False)
    monkeypatch.setattr("istota.search.registry.providers", lambda: [provider("fake", run)])
    result = await run_search(Mock(), "alice", "falcon invoice", sources=["fake"], offset=20)
    assert calls == [("strict", 20), ("strict", 0)]
    assert result["groups"][0]["results"] == []
    assert result["groups"][0]["relaxed"] is False


async def test_one_term_and_strict_hits_never_relax(monkeypatch):
    run = Mock(return_value=ProviderResult([], False))
    monkeypatch.setattr("istota.search.registry.providers", lambda: [provider("fake", run)])
    await run_search(Mock(), "alice", "falcon")
    assert run.call_count == 1
    run.return_value = ProviderResult([hit()], False)
    await run_search(Mock(), "alice", "falcon invoice")
    assert run.call_count == 2


async def test_concurrent_requests_share_provider_bound(monkeypatch):
    import asyncio
    import threading
    lock = threading.Lock()
    active = 0
    maximum = 0
    def run(*args):
        nonlocal active, maximum
        with lock:
            active += 1
            maximum = max(active, maximum)
        time.sleep(0.02)
        with lock:
            active -= 1
        return ProviderResult([hit()], False)
    monkeypatch.setattr("istota.search.registry.providers", lambda: [provider(str(i), run) for i in range(10)])
    await asyncio.gather(*(run_search(Mock(), "alice", "falcon") for _ in range(3)))
    assert maximum == 8


def test_shared_helpers_keep_web_aliases():
    from istota.lib.date_parse import iso_utc
    from istota.modules import module_loader
    from istota.webui.app import _iso_utc, _module_loader
    from istota import briefings
    from istota.feeds.db import connect as feeds_connect
    assert _iso_utc is iso_utc
    assert _module_loader is module_loader
    assert iso_utc("2026-01-02 12:34:56") == "2026-01-02T12:34:56Z"
    assert iso_utc("2026-01-02T12:34:56+02:00") == "2026-01-02T10:34:56Z"
    assert module_loader("feeds")[2] is feeds_connect
    from istota.briefings.db import connect
    assert module_loader("briefings")[2] is connect
    assert module_loader("briefings")[1] is briefings.resolve_for_user


@pytest.mark.parametrize("store", ["feeds", "framework"])
@pytest.mark.parametrize("release_after", [0.02, 0.6])
async def test_lock_waits_respect_deadlines_and_short_contention_retries(tmp_path, monkeypatch, store, release_after):
    import threading
    from istota import db
    from istota.feeds import db as feeds_db

    path = tmp_path / "locked.db"
    if store == "feeds":
        feeds_db.init_db(path)
        with feeds_db.connect(path) as conn:
            for suffix in ("ai", "ad", "au"):
                conn.execute(f"DROP TRIGGER feed_entries_fts_{suffix}")
            conn.execute("DROP TABLE feed_entries_fts")
            conn.execute("UPDATE schema_meta SET value='8' WHERE key='version'")
            conn.commit()
    else:
        db.init_db(path)
    blocker = sqlite3.connect(path, check_same_thread=False)
    if store == "framework":
        blocker.execute("PRAGMA journal_mode=DELETE")
        blocker.execute("BEGIN EXCLUSIVE")
    else:
        blocker.execute("BEGIN IMMEDIATE")
    timer = threading.Timer(release_after, blocker.rollback)
    timer.start()
    def locked(ctx, *args):
        opener = feeds_db.connect if store == "feeds" else db.get_db
        with open_with_deadline(lambda **options: opener(path, **options), ctx.deadline) as conn:
            if store == "feeds":
                feeds_db._migrate_v8_to_v9(conn)
            conn.execute("SELECT 1").fetchone()
        return ProviderResult([hit()], False)
    monkeypatch.setattr("istota.search.registry.providers", lambda: [
        provider("locked", locked, timeout_s=0.1),
        provider("good", lambda *args: ProviderResult([hit()], False)),
    ])
    try:
        start = time.monotonic()
        groups = (await run_search(Mock(), "alice", "falcon"))["groups"]
        elapsed = time.monotonic() - start
        assert elapsed < 0.4
        assert groups[0]["error"] == ("timeout" if release_after > 0.1 else None)
        assert groups[1]["results"] and groups[1]["error"] is None
    finally:
        timer.cancel()
        timer.join()
        blocker.close()


async def test_locked_module_profile_does_not_block_search_or_use_stale_access(tmp_path, monkeypatch):
    import threading
    from istota import db
    from istota.config import Config, UserConfig

    path = tmp_path / "profiles.db"
    db.init_db(path)
    config = Config(db_path=path, users={"alice": UserConfig()})
    run = Mock(return_value=ProviderResult([hit()], False))
    monkeypatch.setattr("istota.search.registry.providers", lambda: [provider("health", run, module="health")])
    blocker = sqlite3.connect(path, check_same_thread=False)
    blocker.execute("PRAGMA journal_mode=DELETE")
    blocker.execute("BEGIN EXCLUSIVE")
    timer = threading.Timer(0.6, blocker.rollback)
    timer.start()
    try:
        start = time.monotonic()
        result = await run_search(config, "alice", "falcon")
        assert time.monotonic() - start < 0.4
        assert result["groups"] == []
        run.assert_not_called()
    finally:
        timer.cancel()
        timer.join()
        blocker.close()
