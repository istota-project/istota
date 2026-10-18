"""Navigation counts through the authenticated host and real per-user stores."""

import sqlite3
import threading
from unittest.mock import AsyncMock

import pytest

from istota import db, user_profiles
from istota.briefings import db as bdb, _loader as bloader, _migrate as bmigrate
from istota.feeds import db as fdb, _loader as floader, _migrate as fmigrate
from istota.feeds.models import EntryRecord
from tests import test_web_app as host_fixtures

# Reuse the existing host auth fixtures.
app = host_fixtures.app
client = host_fixtures.client
config = host_fixtures.config

URL = "/istota/api/nav/unread"


async def _login(client, username="alice"):
    import istota.webui.app as host

    host._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": username})
    assert (await client.get("/istota/callback", follow_redirects=False)).status_code == 302


def _seed(config, username, *, chat=4, briefings=125, feeds=150):
    user_profiles.ensure_profile(config.db_path, username)
    user_profiles.update_profile(config.db_path, username, show_nav_unread_badges=True, disabled_modules=[])
    with db.get_db(config.db_path) as conn:
        room = db.register_room(conn, None, username, origin="web", name="Room")
        db.set_room_read_state(conn, room.token, "web", 0, username)
        for _ in range(chat):
            db.add_message(conn, room.token, role="assistant", body="Reply", origin_surface="web")
    bctx = bloader.resolve_for_user(username, config)
    bmigrate.ensure_initialised(bctx, app_config=config)
    with bdb.connect(bctx.db_path) as conn:
        for _ in range(briefings):
            bdb.insert_archive(conn, briefing_name="Morning", subject="Daily", body_md="Body")
        read_id = bdb.insert_archive(conn, briefing_name="Evening", subject="Read", body_md="Body")
        bdb.mark_archived_read(conn, read_id)
        conn.commit()
    fctx = floader.resolve_for_user(username, config)
    fmigrate.ensure_initialised(fctx)
    with fdb.connect(fctx.db_path) as conn:
        fid = fdb.upsert_feed(conn, url="https://example.com/feed", title="Feed", site_url=None,
                              source_type="rss", category_id=None, poll_interval_minutes=30)
        entries = [
            EntryRecord(id=0, feed_id=fid, guid=str(i), title="Entry", url=None,
                        author=None, content_html=None, content_text=None,
                        status="unread" if i < feeds else "read" if i == feeds else "removed")
            for i in range(feeds + 2)
        ]
        fdb.insert_entries(conn, fid, entries)
        conn.execute("UPDATE feed_entries SET starred = 1 WHERE guid = '0' AND feed_id = ?", (fid,))
        conn.commit()
    return bctx, fctx


async def test_auth_and_preference_off_avoid_content_stores(client, config, monkeypatch):
    assert (await client.get(URL)).status_code == 401
    await _login(client)

    def forbidden(*args, **kwargs):
        pytest.fail("disabled preference accessed content")

    monkeypatch.setattr(bloader, "resolve_for_user", forbidden)
    monkeypatch.setattr(floader, "resolve_for_user", forbidden)
    monkeypatch.setattr(db, "count_user_web_unread_messages", forbidden)
    response = await client.get(URL)
    assert response.status_code == 200
    assert response.json() == {"enabled": False, "chat": None, "briefings": None, "feeds": None}
    assert response.headers["cache-control"] == "no-store"


async def test_exact_totals_isolation_and_live_preference(client, config):
    _seed(config, "alice")
    _seed(config, "bob", chat=2, briefings=3, feeds=5)
    await _login(client)
    response = await client.get(URL, params={"user_id": "bob", "limit": 1})
    assert response.status_code == 200
    assert response.json() == {"enabled": True, "chat": 4, "briefings": 125, "feeds": 150}
    assert response.headers["cache-control"] == "no-store"
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM web_chat_rooms").fetchone()[0] == 0
    user_profiles.update_profile(config.db_path, "alice", show_nav_unread_badges=False)
    assert (await client.get(URL)).json() == {"enabled": False, "chat": None, "briefings": None, "feeds": None}
    await _login(client, "bob")
    assert (await client.get(URL)).json() == {"enabled": True, "chat": 2, "briefings": 3, "feeds": 5}


async def test_disabled_modules_are_not_resolved(client, config, monkeypatch, caplog):
    user_profiles.ensure_profile(config.db_path, "alice")
    user_profiles.update_profile(config.db_path, "alice", show_nav_unread_badges=True,
                                 disabled_modules=["briefings", "feeds"])
    await _login(client)

    def forbidden(*args, **kwargs):
        pytest.fail("disabled module was resolved")

    monkeypatch.setattr(bloader, "resolve_for_user", forbidden)
    monkeypatch.setattr(floader, "resolve_for_user", forbidden)
    assert (await client.get(URL)).json() == {"enabled": True, "chat": 0, "briefings": None, "feeds": None}
    assert not [r for r in caplog.records if r.name == "istota.webui.nav_unread"]
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM rooms").fetchone()[0] == 0


@pytest.mark.parametrize("section", ["chat", "briefings", "feeds"])
async def test_section_failure_is_isolated(client, config, monkeypatch, caplog, section):
    _seed(config, "alice", chat=2, briefings=3, feeds=5)
    await _login(client)
    target, name = {
        "chat": (db, "total_unread_messages"),
        "briefings": (bdb, "count_unread_archive"),
        "feeds": (fdb, "count_entries"),
    }[section]

    def broken(*args, **kwargs):
        raise sqlite3.OperationalError("database unavailable")

    monkeypatch.setattr(target, name, broken)
    expected = {"enabled": True, "chat": 2, "briefings": 3, "feeds": 5}
    expected[section] = None
    assert (await client.get(URL)).json() == expected
    warnings = [r for r in caplog.records if r.name == "istota.webui.nav_unread"]
    assert len(warnings) == 1
    assert warnings[0].unread_section == section
    assert warnings[0].error_type == "OperationalError"


async def test_unavailable_workspace_does_not_initialize_modules(client, config, monkeypatch):
    user_profiles.ensure_profile(config.db_path, "alice")
    user_profiles.update_profile(config.db_path, "alice", show_nav_unread_badges=True)
    config.workspace_path = None
    await _login(client)

    def forbidden(*args, **kwargs):
        pytest.fail("unavailable module initialized")

    monkeypatch.setattr(bmigrate, "ensure_initialised", forbidden)
    monkeypatch.setattr(fmigrate, "ensure_initialised", forbidden)
    assert (await client.get(URL)).json() == {"enabled": True, "chat": 0, "briefings": None, "feeds": None}


async def test_connections_open_in_worker_and_first_use_initializes(client, config, monkeypatch):
    # Config fallback also works before a stored profile exists.
    config.users["alice"].show_nav_unread_badges = True
    await _login(client)
    with db.get_db(config.db_path) as conn:
        conn.execute("DELETE FROM user_profiles WHERE user_id = ?", ("alice",))
    event_thread = threading.get_ident()
    original = sqlite3.connect
    opened = []

    def connect(*args, **kwargs):
        assert threading.get_ident() != event_thread
        opened.append(str(args[0]))
        return original(*args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", connect)
    response = await client.get(URL)
    assert response.json() == {"enabled": True, "chat": 0, "briefings": 0, "feeds": 0}
    assert str(config.module_db_path("alice", "briefings")) in opened
    assert str(config.module_db_path("alice", "feeds")) in opened
