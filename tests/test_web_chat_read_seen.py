"""POST /istota/api/chat/read-seen: rows seen in the All / Unread / Starred
panes advance the web read cursor, room by room, and a room that ends up fully
read gets the Talk read push. Also the `unread` flag the aggregate endpoint
publishes per row, and its absence on the room-event tail."""

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
    from unittest.mock import AsyncMock, MagicMock

ORIGIN = {"origin": "https://example.com"}
URL = "/istota/api/chat/read-seen"


def _make_config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        workspace_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
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


@pytest.fixture
async def client(tmp_path, monkeypatch):
    import istota.webui.app as mod
    config = _make_config(tmp_path)
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    pushes: list[tuple[str, str]] = []

    async def _record_push(username, room_token):
        pushes.append((username, room_token))
        return True

    monkeypatch.setattr(mod, "_push_read_to_talk", _record_push)
    async with AsyncClient(
        transport=ASGITransport(app=mod.app), base_url="https://example.com",
    ) as c:
        c.pushes = pushes
        yield c


async def _login(client, username):
    import istota.webui.app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


async def _default_room(client, cookies):
    # The first listing seeds the cursor, so messages are added after it.
    resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
    return resp.json()["rooms"][0]


def _db_path():
    import istota.webui.app as mod
    return mod._config.db_path


def _add_msg(token, *, role="assistant", body="hello"):
    with db.get_db(_db_path()) as conn:
        return db.add_message(conn, token, role=role, body=body, origin_surface="web")


def _cursor(token, user="alice"):
    with db.get_db(_db_path()) as conn:
        return db.get_room_read_state(conn, token, "web", user)


async def _drain_bg():
    import asyncio

    import istota.webui.app as mod
    while mod._bg_tasks:
        await asyncio.gather(*list(mod._bg_tasks))


class TestValidation:
    @pytest.mark.parametrize("body", [
        [1, 2],
        {},
        {"seen": 5},
        {"seen": "1,2"},
        {"seen": list(range(1, 502))},
        {"seen": ["1"]},
        {"seen": [0]},
        {"seen": [-3]},
        {"seen": [1.5]},
        {"seen": [True]},
    ])
    async def test_malformed_body_422(self, client, body):
        cookies = await _login(client, "alice")
        resp = await client.post(URL, json=body, cookies=cookies, headers=ORIGIN)
        assert resp.status_code == 422

    async def test_not_json_422(self, client):
        cookies = await _login(client, "alice")
        resp = await client.post(
            URL, content=b"not json", cookies=cookies,
            headers={**ORIGIN, "content-type": "application/json"},
        )
        assert resp.status_code == 422

    async def test_500_ids_accepted(self, client):
        cookies = await _login(client, "alice")
        resp = await client.post(
            URL, json={"seen": list(range(1, 501))}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200

    async def test_requires_origin(self, client):
        cookies = await _login(client, "alice")
        resp = await client.post(
            URL, json={"seen": [1]}, cookies=cookies,
            headers={"origin": "https://evil.example.net"},
        )
        assert resp.status_code == 403

    async def test_requires_auth(self, client):
        resp = await client.post(URL, json={"seen": [1]}, headers=ORIGIN)
        assert resp.status_code == 401


class TestAdvance:
    async def test_partial_advance_shape_and_no_push(self, client):
        cookies = await _login(client, "alice")
        room = await _default_room(client, cookies)
        token = room["token"]
        before = _cursor(token)
        a = _add_msg(token, body="one")
        b = _add_msg(token, body="two")
        c = _add_msg(token, body="three")

        resp = await client.post(
            URL, json={"seen": [a, b]}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200
        assert resp.json() == {"rooms": [{
            "token": token, "last_read_message_id": b,
            "unread_count": 1, "advanced": True,
        }]}
        assert before < a
        assert _cursor(token) == b
        await _drain_bg()
        assert client.pushes == []

        # The newest row alone, with an older one unseen, moves nothing.
        d = _add_msg(token, body="four")
        resp = await client.post(
            URL, json={"seen": [d]}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.json()["rooms"] == [{
            "token": token, "last_read_message_id": b,
            "unread_count": 2, "advanced": False,
        }]
        assert c < d

    async def test_full_read_pushes_once(self, client):
        cookies = await _login(client, "alice")
        room = await _default_room(client, cookies)
        token = room["token"]
        with db.get_db(_db_path()) as conn:
            db.add_room_binding(conn, token, "talk", "talkref9")
        a = _add_msg(token, body="one")
        b = _add_msg(token, body="two")
        own = _add_msg(token, role="user", body="my turn")

        resp = await client.post(
            URL, json={"seen": [a, b]}, cookies=cookies, headers=ORIGIN,
        )
        [entry] = resp.json()["rooms"]
        # Fully read lands on the room's newest id, past the trailing user turn.
        assert entry == {
            "token": token, "last_read_message_id": own,
            "unread_count": 0, "advanced": True,
        }
        await _drain_bg()
        assert client.pushes == [("alice", token)]

        # Seen again: nothing moves, so nothing is pushed.
        resp = await client.post(
            URL, json={"seen": [a, b]}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.json()["rooms"][0]["advanced"] is False
        await _drain_bg()
        assert client.pushes == [("alice", token)]

    async def test_foreign_room_untouched_and_unnamed(self, client):
        bob = await _login(client, "bob")
        bob_room = await _default_room(client, bob)
        bob_msg = _add_msg(bob_room["token"], body="bob's")
        bob_cursor = _cursor(bob_room["token"], "bob")

        alice = await _login(client, "alice")
        alice_room = await _default_room(client, alice)
        mine = _add_msg(alice_room["token"], body="alice's")

        resp = await client.post(
            URL, json={"seen": [bob_msg, mine, 999999]}, cookies=alice, headers=ORIGIN,
        )
        tokens = [r["token"] for r in resp.json()["rooms"]]
        assert tokens == [alice_room["token"]]
        assert _cursor(bob_room["token"], "bob") == bob_cursor
        assert _cursor(bob_room["token"], "alice") == 0
        await _drain_bg()
        assert all(t != bob_room["token"] for _, t in client.pushes)


class TestUnreadFlag:
    async def test_aggregate_rows_carry_unread(self, client):
        cookies = await _login(client, "alice")
        room = await _default_room(client, cookies)
        token = room["token"]
        read = _add_msg(token, body="read")
        own = _add_msg(token, role="user", body="mine")
        fresh = _add_msg(token, body="fresh")
        with db.get_db(_db_path()) as conn:
            db.set_room_read_state(conn, token, "web", read, "alice")

        resp = await client.get("/istota/api/chat/messages?view=all", cookies=cookies)
        flags = {m["msg_id"]: m["unread"] for m in resp.json()["messages"]}
        assert flags[read] is False
        assert flags[own] is False
        assert flags[fresh] is True

    async def test_stream_rows_omit_unread(self, client):
        cookies = await _login(client, "alice")
        room = await _default_room(client, cookies)
        mid = _add_msg(room["token"], body="streamed")
        resp = await client.get("/istota/api/chat/events?since_id=0", cookies=cookies)
        [event] = [e for e in resp.json()["events"] if e["msg_id"] == mid]
        assert "unread" not in event
