"""Email thread rooms are hidden from the room list (hidden-email-threads, stage 6).

A thread room stays a room: the private note's `re:` chip, a deep link and the
room stream all open it by finding it in the listing. So the listing keeps
returning it and says two things about it, `email_thread` and `listed`, and the
client files it under a collapsed "Email threads" group unless the viewer chose
to list it. `listed` is per viewer, on their `web_chat_rooms` handle, beside
`color` and `archived`. The PATCH writes it, and refuses it on any room that is
not an email thread, since nothing reads it there.

The aggregate panes (All, Unread, Starred) cover the main list only, so a
hidden thread's rows stay out of them until it is listed.
"""

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db
from istota.config import Config, EmailConfig, SiteConfig, UserConfig, WebConfig
from istota.transport.email.private_room import email_conversation_token

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

ORIGIN = {"origin": "https://example.com"}
web_only = pytest.mark.skipif(not _has_web_deps, reason="web dependencies not installed")


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return path


def _config(db_path, tmp_path):
    return Config(
        db_path=db_path,
        temp_dir=tmp_path / "tmp",
        workspace_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        email=EmailConfig(
            enabled=True, smtp_host="smtp.example.com", bot_email="bot@example.com",
        ),
        users={"alice": UserConfig(display_name="Alice",
                                   email_addresses=["alice@example.com"])},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


def _thread_room(conn, user="alice", name="Book club", ref="<root@example.com>"):
    token = db.register_room(conn, None, user, origin="email", name=name).token
    db.add_room_binding(conn, token, "email", ref)
    return token


def _private_email_room(conn, user="alice"):
    token = db.register_room(conn, None, user, origin="email", name="Email").token
    db.add_room_binding(conn, token, "email", email_conversation_token(user))
    return token


@pytest.fixture
async def client(db_path, tmp_path):
    import istota.webui.app as mod
    config = _config(db_path, tmp_path)
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    transport = ASGITransport(app=mod.app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


async def _login(client, username="alice"):
    import istota.webui.app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


async def _rooms(client, cookies):
    resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
    assert resp.status_code == 200
    return {r["token"]: r for r in resp.json()["rooms"]}


async def _patch(client, cookies, room_id, body):
    return await client.patch(
        f"/istota/api/chat/rooms/{room_id}", json=body, cookies=cookies, headers=ORIGIN,
    )


# ---------------------------------------------------------------------------
# The column
# ---------------------------------------------------------------------------


def _column(path, name="listed"):
    raw = sqlite3.connect(path)
    try:
        return [tuple(r)[1:] for r in raw.execute("PRAGMA table_info(web_chat_rooms)")
                if r[1] == name]
    finally:
        raw.close()


class TestTheColumn:
    def test_an_upgraded_database_matches_a_fresh_one(self, tmp_path):
        fresh = tmp_path / "fresh.db"
        db.init_db(fresh)
        old = tmp_path / "old.db"
        db.init_db(old)
        raw = sqlite3.connect(old)
        raw.execute("ALTER TABLE web_chat_rooms DROP COLUMN listed")
        raw.commit()
        db._run_migrations(raw)
        raw.commit()
        raw.close()
        declared = tmp_path / "declared.db"
        schema = (Path(db.__file__).parent.parent.parent / "schema.sql").read_text()
        raw = sqlite3.connect(declared)
        raw.executescript(schema)
        raw.close()

        expected = [("listed", "INTEGER", 1, "0", 0)]
        assert _column(declared) == _column(old) == _column(fresh) == expected

    def test_an_existing_handle_reads_as_not_listed(self, tmp_path):
        old = tmp_path / "old.db"
        db.init_db(old)
        raw = sqlite3.connect(old)
        raw.execute("ALTER TABLE web_chat_rooms DROP COLUMN listed")
        raw.execute(
            "INSERT INTO web_chat_rooms (user_id, token, name) VALUES ('alice', 't', 'n')"
        )
        raw.commit()
        db._run_migrations(raw)
        raw.commit()
        raw.close()
        with db.get_db(old) as conn:
            (handle,) = db.list_web_chat_rooms(conn, "alice")
        assert handle.listed is False

    def test_the_per_user_rebuild_carries_it(self, tmp_path):
        """The legacy single-token rebuild runs after the ALTER that adds the
        column, so it has to carry it, the way it carries `color`."""
        path = tmp_path / "legacy.db"
        raw = sqlite3.connect(path)
        raw.execute("""
            CREATE TABLE web_chat_rooms (
                id INTEGER PRIMARY KEY, user_id TEXT NOT NULL,
                token TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
                archived INTEGER NOT NULL DEFAULT 0,
                color TEXT NOT NULL DEFAULT '',
                listed INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT (datetime('now')),
                updated_at TEXT NOT NULL DEFAULT (datetime('now'))
            )
        """)
        raw.execute(
            "INSERT INTO web_chat_rooms (user_id, token, name, listed) "
            "VALUES ('alice', 't', 'n', 1)"
        )
        raw.commit()
        db._migrate_web_chat_rooms_peruser(raw)
        raw.commit()
        row = raw.execute("SELECT listed FROM web_chat_rooms WHERE token = 't'").fetchone()
        raw.close()
        assert row == (1,)

    def test_the_store_writes_it_per_handle(self, db_path):
        with db.get_db(db_path) as conn:
            token = _thread_room(conn)
            mine = db.ensure_web_chat_handle(conn, "alice", token, "Book club")
            theirs = db.ensure_web_chat_handle(conn, "bob", token, "Book club")
            updated = db.update_web_chat_room(conn, mine.id, listed=True)
            assert updated.listed is True
            assert db.get_web_chat_room(conn, theirs.id).listed is False
            assert db.update_web_chat_room(conn, mine.id, listed=False).listed is False

    def test_hidden_tokens_are_unlisted_threads_only(self, db_path):
        with db.get_db(db_path) as conn:
            hidden = _thread_room(conn)
            shown = _thread_room(conn, name="Shown", ref="<other@example.com>")
            _private_email_room(conn)
            db.create_web_chat_room(conn, "alice", "general")
            handle = db.ensure_web_chat_handle(conn, "alice", shown, "Shown")
            db.update_web_chat_room(conn, handle.id, listed=True)
            assert db.hidden_room_tokens_for_member(conn, "alice") == {hidden}


# ---------------------------------------------------------------------------
# The listing and the PATCH
# ---------------------------------------------------------------------------


@web_only
class TestThePayload:
    async def test_the_listing_says_which_rooms_are_threads_and_listed(
        self, client, db_path,
    ):
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
            private = _private_email_room(conn)
            web = db.create_web_chat_room(conn, "alice", "general").token
        rooms = await _rooms(client, await _login(client))
        assert rooms[thread]["email_thread"] is True
        assert rooms[thread]["listed"] is False
        for token in (private, web):
            assert rooms[token]["email_thread"] is False
            assert rooms[token]["listed"] is False

    async def test_the_patch_lists_a_thread_and_answers_with_the_listing_keys(
        self, client, db_path,
    ):
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
        cookies = await _login(client)
        listed = await _rooms(client, cookies)
        resp = await _patch(client, cookies, listed[thread]["id"], {"listed": True})
        assert resp.status_code == 200
        body = resp.json()
        assert body["listed"] is True
        assert body["email_thread"] is True
        # The client merges this response over its room record, so it carries
        # every key the listing does (ISSUE-342).
        missing = set(listed[thread]) - set(body) - {"last_activity", "unread_count",
                                                     "has_my_notes"}
        assert missing == set()
        assert (await _rooms(client, cookies))[thread]["listed"] is True

        resp = await _patch(client, cookies, listed[thread]["id"], {"listed": False})
        assert resp.json()["listed"] is False

    async def test_the_patch_refuses_listed_on_any_other_room(self, client, db_path):
        with db.get_db(db_path) as conn:
            private = _private_email_room(conn)
            web = db.create_web_chat_room(conn, "alice", "general").token
        cookies = await _login(client)
        rooms = await _rooms(client, cookies)
        for token in (private, web):
            resp = await _patch(
                client, cookies, rooms[token]["id"], {"listed": True, "color": "teal"},
            )
            assert resp.status_code == 400
            assert resp.json() == {"error": "only an email thread can be listed"}
        # Refused before any write: the colour sent beside it did not land.
        after = await _rooms(client, cookies)
        assert after[web]["color"] is None
        assert after[web]["listed"] is False

    async def test_a_non_boolean_listed_is_refused(self, client, db_path):
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
        cookies = await _login(client)
        rooms = await _rooms(client, cookies)
        resp = await _patch(client, cookies, rooms[thread]["id"], {"listed": "yes"})
        assert resp.status_code == 400
        assert (await _rooms(client, cookies))[thread]["listed"] is False

    async def test_the_stream_snapshot_carries_listed(self, client, db_path):
        import istota.webui.app as mod
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
        cookies = await _login(client)
        rooms = await _rooms(client, cookies)
        snapshot = mod._room_snapshot("alice")
        assert snapshot[thread]["listed"] is False
        await _patch(client, cookies, rooms[thread]["id"], {"listed": True})
        assert mod._room_snapshot("alice")[thread]["listed"] is True


# ---------------------------------------------------------------------------
# The aggregate panes
# ---------------------------------------------------------------------------


@web_only
class TestTheAggregatePanes:
    async def test_a_hidden_thread_is_out_of_all_unread_and_starred(
        self, client, db_path,
    ):
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
            web = db.create_web_chat_room(conn, "alice", "general").token
        cookies = await _login(client)
        # The listing seeds each room's read cursor, so the rows are written
        # after it and read as unread.
        rooms = await _rooms(client, cookies)
        with db.get_db(db_path) as conn:
            for token, body in ((thread, "From the thread"), (web, "From general")):
                msg = db.add_message(
                    conn, token, role="system", body=body, origin_surface="web",
                )
                db.set_message_starred(conn, msg, "alice", True)

        async def bodies(view):
            resp = await client.get(
                f"/istota/api/chat/messages?view={view}", cookies=cookies,
            )
            assert resp.status_code == 200
            return [m["text"] for m in resp.json()["messages"]]

        for view in ("all", "unread", "starred"):
            assert await bodies(view) == ["From general"], view

        await _patch(client, cookies, rooms[thread]["id"], {"listed": True})
        for view in ("all", "unread", "starred"):
            assert "From the thread" in await bodies(view), view
