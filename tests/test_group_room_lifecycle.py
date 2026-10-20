"""A group-owned room's life, end to end (group-owned rooms, Stage 5).

One scenario through the routes a person uses, on real SQLite and signed
sessions: an owner creates a room from Groups and members talk in it; the
creator leaves the group and another owner takes the host role and keeps
managing the room; a member is removed and loses every read and send; a new
member joins and reads the whole history. Each step's behaviour has its own
focused test in `test_group_rooms.py`, `test_group_room_revocation.py` and
`test_group_room_routes.py`; this file holds them in sequence, so a change
that breaks the hand-off between two steps shows here.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.groups import service
from istota.rooms import policy as room_policy

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

pytestmark = pytest.mark.skipif(not _has_web_deps, reason="web dependencies not installed")

ORIGIN = {"origin": "https://example.com"}
API = "/istota/api"


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    return Config(
        db_path=db_path,
        site=SiteConfig(hostname="example.com"),
        users={name: UserConfig(display_name=f"{name.title()} Example")
               for name in ("alice", "bob", "carol", "dave")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


@pytest.fixture
def team(config):
    """`team`, which alice owns. Everyone else joins through the routes."""
    with db.get_db(config.db_path) as conn:
        service.create_group(conn, config, "team", actor_id="admin", is_admin=True,
                             expected_revision=0, owners=["alice"],
                             display_name="Team", kind="team")
    return "team"


@pytest.fixture
async def client(config):
    import istota.webui.app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    async with AsyncClient(transport=ASGITransport(app=mod.app),
                           base_url="https://example.com") as c:
        yield c


async def _login(client, username):
    import istota.webui.app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


def _revision(config):
    with db.get_db(config.db_path) as conn:
        return db.get_group(conn, "team", include_archived=True)["revision"]


async def _put_member(client, cookies, config, user_id, role):
    resp = await client.put(
        f"{API}/groups/team/members/{user_id}", cookies=cookies, headers=ORIGIN,
        json={"revision": _revision(config), "role": role, "acknowledge_history": True},
    )
    assert resp.status_code == 200, resp.text
    return resp


async def _delete_member(client, cookies, config, user_id):
    resp = await client.request(
        "DELETE", f"{API}/groups/team/members/{user_id}", cookies=cookies,
        headers=ORIGIN, json={"revision": _revision(config)},
    )
    assert resp.status_code == 200, resp.text
    return resp


def _handle_id(config, token, user):
    with db.get_db(config.db_path) as conn:
        row = conn.execute(
            "SELECT id FROM web_chat_rooms WHERE token = ? AND user_id = ?",
            (token, user),
        ).fetchone()
    return row["id"] if row else None


async def _listed(client, cookies):
    resp = await client.get(f"{API}/chat/rooms", cookies=cookies)
    assert resp.status_code == 200
    return {r["token"]: r for r in resp.json()["rooms"]}


async def _post(client, cookies, room_id, text):
    return await client.post(f"{API}/chat/rooms/{room_id}/messages", cookies=cookies,
                             headers=ORIGIN, json={"text": text})


async def _transcript(client, cookies, room_id):
    resp = await client.get(f"{API}/chat/rooms/{room_id}/messages", cookies=cookies)
    if resp.status_code != 200:
        return resp.status_code, None
    return 200, " ".join(str(m) for m in resp.json()["messages"])


async def test_a_group_room_outlives_its_creator_and_follows_the_group(client, config, team):
    alice = await _login(client, "alice")
    await _put_member(client, alice, config, "bob", "owner")
    await _put_member(client, alice, config, "carol", "member")

    # alice creates the room from Groups; every member has it in Chat.
    resp = await client.post(
        f"{API}/groups/team/rooms", cookies=alice, headers=ORIGIN,
        json={"name": "Planning", "request_id": "req-lifecycle-1",
              "revision": _revision(config)},
    )
    assert resp.status_code == 201, resp.text
    token = resp.json()["room"]["token"]
    bob = await _login(client, "bob")
    carol = await _login(client, "carol")
    for cookies in (alice, bob, carol):
        assert token in await _listed(client, cookies)

    assert (await _post(client, alice, _handle_id(config, token, "alice"),
                        "kickoff-canary from the creator")).status_code == 200
    carol_room = _handle_id(config, token, "carol")
    assert (await _post(client, carol, carol_room,
                        "carol-canary before she leaves")).status_code == 200

    # The creator leaves the group. The other owner becomes the host, and the
    # creator field stays history.
    alice_room = _handle_id(config, token, "alice")
    await _delete_member(client, alice, config, "alice")
    assert token not in await _listed(client, alice)
    assert (await _transcript(client, alice, alice_room))[0] == 404
    with db.get_db(config.db_path) as conn:
        assert room_policy.current_host(conn, room_policy.get_policy(conn, token)) == "bob"
        assert db.get_room(conn, token).user_id == "alice"
        assert sorted(db.list_room_members(conn, token)) == ["bob", "carol"]

    # Management continues with the new host: bob, an owner, changes a room
    # setting; carol, a member, cannot.
    bob_room = _handle_id(config, token, "bob")
    resp = await client.patch(f"{API}/chat/rooms/{bob_room}", cookies=bob,
                              headers=ORIGIN, json={"speech_mode": "mention"})
    assert resp.status_code == 200, resp.text
    assert resp.json()["policy"]["host"] == "bob"
    assert resp.json()["policy"]["speech_mode"] == "mention"
    resp = await client.patch(f"{API}/chat/rooms/{carol_room}", cookies=carol,
                              headers=ORIGIN, json={"speech_mode": "classifier"})
    assert resp.status_code == 403

    # bob removes carol from the group. Her stale handle reads and sends
    # nothing, the post does not put her back, and her messages stay.
    await _delete_member(client, bob, config, "carol")
    assert token not in await _listed(client, carol)
    assert (await _transcript(client, carol, carol_room))[0] == 404
    assert (await _post(client, carol, carol_room, "carol-canary after removal")).status_code == 404
    with db.get_db(config.db_path) as conn:
        assert db.list_room_members(conn, token) == ["bob"]
        assert db.group_room_admits(conn, token, "carol") is False

    # dave joins, acknowledging the history, and reads all of it.
    await _put_member(client, bob, config, "dave", "member")
    dave = await _login(client, "dave")
    assert token in await _listed(client, dave)
    status, transcript = await _transcript(client, dave, _handle_id(config, token, "dave"))
    assert status == 200
    assert "kickoff-canary from the creator" in transcript
    assert "carol-canary before she leaves" in transcript
    assert "carol-canary after removal" not in transcript
    with db.get_db(config.db_path) as conn:
        assert sorted(db.list_room_members(conn, token)) == ["bob", "dave"]
        assert room_policy.current_host(conn, room_policy.get_policy(conn, token)) == "bob"
