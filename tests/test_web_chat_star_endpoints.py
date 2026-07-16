"""Web endpoints for chat cross-room views, per-message starring, and
mark-all-rooms-read.

- PUT  /istota/api/chat/messages/{id}/star
- GET  /istota/api/chat/messages          (view=all|unread|starred + keyset)
- POST /istota/api/chat/rooms/read-all
- per-room history regression: store-sourced turns now carry msg_id/starred
  (system rows keep notif_id).
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db
from istota.config import Config, SiteConfig, UserConfig, WebConfig

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

pytestmark = pytest.mark.skipif(
    not _has_web_deps, reason="web dependencies not installed",
)

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

ORIGIN = {"origin": "https://example.com"}


def _make_config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(enabled=True, hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice"),
               "bob": UserConfig(display_name="Bob")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


def _patch_app(config):
    import istota.web_app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    return mod.app


async def _login(client, username):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


@pytest.fixture
async def chat_client(tmp_path):
    config = _make_config(tmp_path)
    app = _patch_app(config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


def _db_path():
    import istota.web_app as mod
    return mod._config.db_path


async def _default_room(client, cookies):
    resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
    return resp.json()["rooms"][0]


def _add_msg(token, *, role="assistant", body="hello", origin="web",
             task_id=None, title=None, created_at=None):
    with db.get_db(_db_path()) as conn:
        mid = db.add_message(
            conn, token, role=role, body=body, origin_surface=origin,
            task_id=task_id, title=title,
        )
        if created_at is not None:
            conn.execute(
                "UPDATE messages SET created_at = ? WHERE id = ?",
                (created_at, mid),
            )
    return mid


class TestStarEndpoint:
    async def test_star_happy_path_and_toggle_off(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        mid = _add_msg(room["token"])
        resp = await chat_client.put(
            f"/istota/api/chat/messages/{mid}/star",
            json={"starred": True}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "starred": True}
        with db.get_db(_db_path()) as conn:
            assert db.get_starred_message_ids(conn, "alice", [mid]) == {mid}
        resp = await chat_client.put(
            f"/istota/api/chat/messages/{mid}/star",
            json={"starred": False}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "starred": False}
        with db.get_db(_db_path()) as conn:
            assert db.get_starred_message_ids(conn, "alice", [mid]) == set()

    async def test_requires_auth(self, chat_client):
        resp = await chat_client.put(
            "/istota/api/chat/messages/1/star", json={"starred": True},
            headers=ORIGIN,
        )
        assert resp.status_code == 401

    async def test_nonexistent_message_404(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.put(
            "/istota/api/chat/messages/999999/star",
            json={"starred": True}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 404

    async def test_foreign_room_404(self, chat_client):
        bob = await _login(chat_client, "bob")
        bob_room = await _default_room(chat_client, bob)
        mid = _add_msg(bob_room["token"])
        alice = await _login(chat_client, "alice")
        resp = await chat_client.put(
            f"/istota/api/chat/messages/{mid}/star",
            json={"starred": True}, cookies=alice, headers=ORIGIN,
        )
        assert resp.status_code == 404

    async def test_malformed_body_422(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        mid = _add_msg(room["token"])
        resp = await chat_client.put(
            f"/istota/api/chat/messages/{mid}/star",
            json={"starred": "yes"}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 422
        resp = await chat_client.put(
            f"/istota/api/chat/messages/{mid}/star",
            content=b"not json", cookies=cookies,
            headers={**ORIGIN, "content-type": "application/json"},
        )
        assert resp.status_code == 422

    async def test_origin_check(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        mid = _add_msg(room["token"])
        resp = await chat_client.put(
            f"/istota/api/chat/messages/{mid}/star",
            json={"starred": True}, cookies=cookies,
            headers={"origin": "https://evil.example.net"},
        )
        assert resp.status_code == 403


class TestAggregateMessagesEndpoint:
    async def test_requires_auth(self, chat_client):
        resp = await chat_client.get("/istota/api/chat/messages")
        assert resp.status_code == 401

    async def test_all_view_shape(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        u = _add_msg(room["token"], role="user", body="q", task_id=11,
                     created_at="2026-07-01 10:00:00")
        a = _add_msg(room["token"], role="assistant", body="ans", task_id=11,
                     created_at="2026-07-01 10:00:05")
        s = _add_msg(room["token"], role="system", body="alert!", title="Alert",
                     created_at="2026-07-01 11:00:00")
        resp = await chat_client.get(
            "/istota/api/chat/messages", cookies=cookies,
        )
        assert resp.status_code == 200
        data = resp.json()
        msgs = data["messages"]
        assert [m["msg_id"] for m in msgs] == [u, a, s]  # oldest-first
        assert all(m["room_token"] == room["token"] for m in msgs)
        assert all(m["room_name"] for m in msgs)
        assert all(m["starred"] is False for m in msgs)
        # System rows keep notif_id alongside msg_id.
        assert msgs[2]["notif_id"] == s
        assert msgs[2]["role"] == "system"
        assert "**Alert**" in msgs[2]["text"]
        # Assistant rows keep the transcript shape (segments present).
        assert "segments" in msgs[1]
        # Display timestamps are ISO-8601 UTC.
        assert msgs[0]["created_at"].endswith("Z")
        assert data["has_more"] is False

    async def test_unread_view(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        a1 = _add_msg(room["token"], role="assistant", body="seen",
                      created_at="2026-07-01 10:00:00")
        with db.get_db(_db_path()) as conn:
            db.set_room_read_state(conn, room["token"], "web", a1, "alice")
        _add_msg(room["token"], role="user", body="own turn",
                 created_at="2026-07-01 10:01:00")
        a2 = _add_msg(room["token"], role="assistant", body="fresh",
                      created_at="2026-07-01 10:02:00")
        resp = await chat_client.get(
            "/istota/api/chat/messages?view=unread", cookies=cookies,
        )
        msgs = resp.json()["messages"]
        assert [m["msg_id"] for m in msgs] == [a2]

    async def test_starred_view(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        m1 = _add_msg(room["token"], body="starred one",
                      created_at="2026-07-01 10:00:00")
        _add_msg(room["token"], body="not starred",
                 created_at="2026-07-01 10:01:00")
        with db.get_db(_db_path()) as conn:
            db.set_message_starred(conn, m1, "alice", True)
        resp = await chat_client.get(
            "/istota/api/chat/messages?view=starred", cookies=cookies,
        )
        msgs = resp.json()["messages"]
        assert [m["msg_id"] for m in msgs] == [m1]
        assert msgs[0]["starred"] is True

    async def test_keyset_paging(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        mids = [
            _add_msg(room["token"], body=f"m{i}",
                     created_at=f"2026-07-01 10:00:0{i}")
            for i in range(5)
        ]
        resp = await chat_client.get(
            "/istota/api/chat/messages?limit=2", cookies=cookies,
        )
        data = resp.json()
        assert [m["msg_id"] for m in data["messages"]] == mids[3:]
        assert data["has_more"] is True
        cur = data["oldest_cursor"]
        assert cur["id"] == mids[3]
        # The cursor ts is the RAW stored created_at, not the display value.
        assert "T" not in cur["ts"]
        resp = await chat_client.get(
            f"/istota/api/chat/messages?limit=2&before_ts={cur['ts']}&before_id={cur['id']}",
            cookies=cookies,
        )
        data = resp.json()
        assert [m["msg_id"] for m in data["messages"]] == mids[1:3]
        assert data["has_more"] is True

    async def test_bad_view_400(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/messages?view=bogus", cookies=cookies,
        )
        assert resp.status_code == 400

    async def test_half_cursor_400(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get(
            "/istota/api/chat/messages?before_ts=2026-07-01%2010:00:00",
            cookies=cookies,
        )
        assert resp.status_code == 400

    async def test_limit_clamped(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        _add_msg(room["token"], body="x")
        resp = await chat_client.get(
            "/istota/api/chat/messages?limit=100000", cookies=cookies,
        )
        assert resp.status_code == 200
        resp = await chat_client.get(
            "/istota/api/chat/messages?limit=0", cookies=cookies,
        )
        assert resp.status_code == 200

    async def test_membership_scoped(self, chat_client):
        bob = await _login(chat_client, "bob")
        bob_room = await _default_room(chat_client, bob)
        _add_msg(bob_room["token"], body="bob secret")
        alice = await _login(chat_client, "alice")
        await _default_room(chat_client, alice)
        resp = await chat_client.get(
            "/istota/api/chat/messages", cookies=alice,
        )
        texts = [m["text"] for m in resp.json()["messages"]]
        assert "bob secret" not in texts


class TestReadAllEndpoint:
    async def test_read_all(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        _add_msg(room["token"], body="unseen")
        resp = await chat_client.post(
            "/istota/api/chat/rooms/read-all", cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "updated": 1}
        with db.get_db(_db_path()) as conn:
            assert db.count_unread_messages(conn, room["token"], "web", "alice") == 0
        # Second call: nothing left to move.
        resp = await chat_client.post(
            "/istota/api/chat/rooms/read-all", cookies=cookies, headers=ORIGIN,
        )
        assert resp.json() == {"ok": True, "updated": 0}

    async def test_requires_origin(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.post(
            "/istota/api/chat/rooms/read-all", cookies=cookies,
            headers={"origin": "https://evil.example.net"},
        )
        assert resp.status_code == 403


class TestPerRoomHistoryCarriesStarIdentity:
    async def test_store_turns_carry_msg_id_and_starred(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        u = _add_msg(room["token"], role="user", body="q", task_id=21,
                     created_at="2026-07-01 10:00:00")
        a = _add_msg(room["token"], role="assistant", body="ans", task_id=21,
                     created_at="2026-07-01 10:00:05")
        s = _add_msg(room["token"], role="system", body="notice",
                     created_at="2026-07-01 10:01:00")
        with db.get_db(_db_path()) as conn:
            db.set_message_starred(conn, a, "alice", True)
        resp = await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )
        msgs = resp.json()["messages"]
        by_msg_id = {m.get("msg_id"): m for m in msgs}
        assert by_msg_id[u]["role"] == "user"
        assert by_msg_id[u]["starred"] is False
        assert by_msg_id[a]["starred"] is True
        # System rows: notif_id retained (back-compat), msg_id added.
        assert by_msg_id[s]["notif_id"] == s
        assert by_msg_id[s]["starred"] is False

    async def test_aux_only_turn_has_no_msg_id(self, chat_client):
        """An in-flight turn exists only as a tasks row — no msg_id, no star."""
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        await chat_client.post(
            f"/istota/api/chat/rooms/{room['id']}/messages",
            json={"text": "still running"}, cookies=cookies, headers=ORIGIN,
        )
        resp = await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )
        msgs = resp.json()["messages"]
        # The pending assistant placeholder is aux-sourced.
        assistant = next(m for m in msgs if m["role"] == "assistant")
        assert assistant["status"] == "pending"
        assert "msg_id" not in assistant or assistant["msg_id"] is None

    async def test_stars_isolated_per_user_in_shared_room(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        a = _add_msg(room["token"], role="assistant", body="ans", task_id=31)
        with db.get_db(_db_path()) as conn:
            db.add_room_member(conn, room["token"], "bob")
            db.set_message_starred(conn, a, "bob", True)
        resp = await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )
        msgs = resp.json()["messages"]
        target = next(m for m in msgs if m.get("msg_id") == a)
        assert target["starred"] is False
