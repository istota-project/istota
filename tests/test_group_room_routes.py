"""The managed-room API (group-owned rooms, Stage 4).

`GET` and `POST /groups/{id}/rooms` over `groups.rooms.create_group_room`, the
Groups destination every `group_managed` refusal carries, and the refusals
that keep a group-owned room's audience the group's: no member add or remove,
no promote to Talk, no external surface binding and no guest. Real SQLite, the
host app and signed sessions throughout.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.groups import service
from istota.groups.rooms import create_group_room

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
MANAGE_URL = "/istota/groups/?group=team"


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


def _revision(conn, group_id="team"):
    return db.get_group(conn, group_id, include_archived=True)["revision"]


@pytest.fixture
def team(config):
    """`team`: alice owns it, bob is a member. carol and dave are outside it."""
    with db.get_db(config.db_path) as conn:
        service.create_group(conn, config, "team", actor_id="admin", is_admin=True,
                             expected_revision=0, owners=["alice"],
                             display_name="Team", kind="team")
        service.change_membership(conn, config, "team", "bob", actor_id="alice",
                                  expected_revision=_revision(conn), role="member",
                                  acknowledge_history=True)
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


def _current_revision(config):
    with db.get_db(config.db_path) as conn:
        return _revision(conn)


async def _create(client, cookies, config, *, name="Planning", request_id="req-00000001",
                  revision=None):
    body = {"name": name, "request_id": request_id,
            "revision": _current_revision(config) if revision is None else revision}
    return await client.post(f"{API}/groups/team/rooms", cookies=cookies,
                             headers=ORIGIN, json=body)


def _handle_id(config, token, user):
    with db.get_db(config.db_path) as conn:
        row = conn.execute(
            "SELECT id FROM web_chat_rooms WHERE token = ? AND user_id = ?",
            (token, user),
        ).fetchone()
    return row["id"] if row else None


@pytest.fixture
def managed(config, team):
    with db.get_db(config.db_path) as conn:
        return create_group_room(conn, "alice", "team", "Planning",
                                 "req-fixture-01")["room"].token


def _assert_group_managed(resp):
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason"] == "group_managed"
    assert body["group"] == {"group_id": "team", "display_name": "Team"}
    assert body["manage_url"] == MANAGE_URL
    assert body["error"]


# ---------------------------------------------------------------------------
# POST /groups/{id}/rooms
# ---------------------------------------------------------------------------


class TestCreatingFromGroups:
    async def test_an_owner_creates_a_room_every_member_sees(self, client, config, team):
        alice = await _login(client, "alice")
        before = _current_revision(config)
        resp = await _create(client, alice, config)
        assert resp.status_code == 201
        body = resp.json()
        assert body["created"] is True
        room = body["room"]
        assert room["name"] == "Planning"
        assert room["membership_source"] == "group"
        assert room["room_id"] == _handle_id(config, room["token"], "alice")
        assert body["revision"] == before + 1
        with db.get_db(config.db_path) as conn:
            event = conn.execute(
                "SELECT event_type, details_json FROM group_events "
                "WHERE group_id = 'team' ORDER BY id DESC LIMIT 1").fetchone()
        assert event["event_type"] == "room_created"
        assert room["token"] in event["details_json"]
        bob = await _login(client, "bob")
        listed = await client.get(f"{API}/chat/rooms", cookies=bob)
        bobs = {r["token"]: r for r in listed.json()["rooms"]}
        assert bobs[room["token"]]["managed_by"] == {
            "group_id": "team", "display_name": "Team",
            "manage_url": MANAGE_URL, "is_owner": False,
        }

    async def test_a_replay_returns_the_same_room_and_another_name_conflicts(
            self, client, config, team):
        alice = await _login(client, "alice")
        first = (await _create(client, alice, config)).json()
        # The revision moved with the creation; a replay is answered anyway.
        again = await _create(client, alice, config, revision=first["revision"] - 1)
        assert again.status_code == 200
        assert again.json()["created"] is False
        assert again.json()["room"]["token"] == first["room"]["token"]
        assert again.json()["room"]["room_id"] == first["room"]["room_id"]
        other = await _create(client, alice, config, name="Elsewhere")
        assert other.status_code == 409
        assert other.json()["error"] == "request_conflict"
        with db.get_db(config.db_path) as conn:
            assert conn.execute(
                "SELECT COUNT(*) FROM rooms WHERE group_id = 'team'").fetchone()[0] == 1

    async def test_a_stale_revision_creates_nothing(self, client, config, team):
        alice = await _login(client, "alice")
        resp = await _create(client, alice, config, revision=_current_revision(config) - 1)
        assert resp.status_code == 409
        assert resp.json()["error"] == "revision_conflict"
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM group_room_requests").fetchone()[0] == 0

    async def test_a_member_is_refused_and_an_outsider_learns_nothing(self, client, config, team):
        bob = await _login(client, "bob")
        assert (await _create(client, bob, config)).status_code == 403
        carol = await _login(client, "carol")
        outsider = await _create(client, carol, config)
        unknown = await client.post(f"{API}/groups/nowhere/rooms", cookies=carol,
                                    headers=ORIGIN, json={"name": "x", "request_id": "req-00000009",
                                                          "revision": 0})
        assert outsider.status_code == unknown.status_code == 404
        assert outsider.json() == unknown.json()
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM group_room_requests").fetchone()[0] == 0

    async def test_the_origin_guard_and_the_body_shape(self, client, config, team):
        alice = await _login(client, "alice")
        resp = await client.post(f"{API}/groups/team/rooms", cookies=alice,
                                 headers={"origin": "https://other.example.com"},
                                 json={"name": "x", "request_id": "req-00000001", "revision": 0})
        assert resp.status_code == 403
        for body in ({"name": "x", "request_id": "req-00000001"},
                     {"name": "x", "request_id": "req-00000001", "revision": 0, "extra": 1},
                     {"name": 5, "request_id": "req-00000001", "revision": 0}):
            resp = await client.post(f"{API}/groups/team/rooms", cookies=alice,
                                     headers=ORIGIN, json=body)
            assert resp.status_code == 422

    async def test_members_are_recorded_with_their_names(self, client, config, team):
        alice = await _login(client, "alice")
        token = (await _create(client, alice, config)).json()["room"]["token"]
        with db.get_db(config.db_path) as conn:
            service.change_membership(conn, config, "team", "carol", actor_id="alice",
                                      expected_revision=_revision(conn), role="member",
                                      acknowledge_history=True)
            names = {row["user_id"]: row["display_name"] for row in conn.execute(
                "SELECT user_id, display_name FROM room_participants "
                "WHERE room_token = ? AND left_at IS NULL", (token,))}
        # Signing in seeds alice's profile, whose name the config now carries.
        assert names == {"alice": config.users["alice"].display_name,
                         "bob": "Bob Example", "carol": "Carol Example"}
        assert names["alice"]


# ---------------------------------------------------------------------------
# GET /groups/{id}/rooms and the detail's rooms
# ---------------------------------------------------------------------------


class TestListingFromGroups:
    async def test_managed_and_accessible_linked_rooms_are_labelled(
            self, client, config, team, managed):
        with db.get_db(config.db_path) as conn:
            shared = db.create_web_chat_room(conn, "alice", "Linked").token
            db.add_web_room_member(conn, shared, "bob")
            db.set_room_group(conn, shared, "team")
            private = db.create_web_chat_room(conn, "alice", "Alice only").token
            db.set_room_group(conn, private, "team")
        bob = await _login(client, "bob")
        resp = await client.get(f"{API}/groups/team/rooms", cookies=bob)
        assert resp.status_code == 200
        rooms = {r["token"]: r for r in resp.json()["rooms"]}
        assert set(rooms) == {managed, shared}
        assert rooms[managed]["membership_source"] == "group"
        assert rooms[shared]["membership_source"] == "room"
        assert rooms[managed]["room_id"] == _handle_id(config, managed, "bob")
        assert "Alice only" not in resp.text
        assert resp.json()["can_create"] is False
        detail = (await client.get(f"{API}/groups/team", cookies=bob)).json()
        assert {r["token"]: r["membership_source"] for r in detail["rooms"]} == {
            managed: "group", shared: "room"}

    async def test_a_hidden_managed_room_is_still_listed(self, client, config, team, managed):
        bob = await _login(client, "bob")
        room_id = _handle_id(config, managed, "bob")
        await client.patch(f"{API}/chat/rooms/{room_id}", cookies=bob, headers=ORIGIN,
                           json={"archived": True})
        resp = await client.get(f"{API}/groups/team/rooms", cookies=bob)
        assert [r["token"] for r in resp.json()["rooms"]] == [managed]

    async def test_an_owner_may_create_and_an_outsider_reads_nothing(
            self, client, config, team, managed):
        alice = await _login(client, "alice")
        assert (await client.get(f"{API}/groups/team/rooms", cookies=alice)).json()["can_create"]
        carol = await _login(client, "carol")
        resp = await client.get(f"{API}/groups/team/rooms", cookies=carol)
        assert resp.status_code == 404
        assert "Planning" not in resp.text


# ---------------------------------------------------------------------------
# The Chat side: labels and refusals with the Groups destination
# ---------------------------------------------------------------------------


class TestChatRefusals:
    async def test_membership_mutations_point_to_groups(self, client, config, team, managed):
        alice = await _login(client, "alice")
        room_id = _handle_id(config, managed, "alice")
        _assert_group_managed(await client.post(
            f"{API}/chat/rooms/{room_id}/members", cookies=alice, headers=ORIGIN,
            json={"user_id": "carol", "acknowledge_history": True}))
        _assert_group_managed(await client.delete(
            f"{API}/chat/rooms/{room_id}/members/bob", cookies=alice, headers=ORIGIN))
        _assert_group_managed(await client.delete(
            f"{API}/chat/rooms/{room_id}", cookies=alice, headers=ORIGIN))
        _assert_group_managed(await client.put(
            f"{API}/chat/rooms/{room_id}/group", cookies=alice, headers=ORIGIN,
            json={"group_id": None}))
        with db.get_db(config.db_path) as conn:
            assert db.list_room_members(conn, managed) == ["alice", "bob"]
            assert db.get_room(conn, managed).group_id == "team"

    async def test_promote_to_talk_is_refused(self, client, config, team, managed):
        config.nextcloud.url = "https://cloud.example.com"
        alice = await _login(client, "alice")
        room_id = _handle_id(config, managed, "alice")
        resp = await client.post(f"{API}/chat/rooms/{room_id}/promote",
                                 cookies=alice, headers=ORIGIN)
        _assert_group_managed(resp)
        with db.get_db(config.db_path) as conn:
            assert db.get_room_binding(conn, managed, "talk") is None

    async def test_the_people_and_group_panes_say_who_decides(
            self, client, config, team, managed):
        bob = await _login(client, "bob")
        room_id = _handle_id(config, managed, "bob")
        members = (await client.get(f"{API}/chat/rooms/{room_id}/members", cookies=bob)).json()
        assert members["can_manage"] is False
        assert members["managed_by"] == {"group_id": "team", "display_name": "Team",
                                         "manage_url": MANAGE_URL, "is_owner": False}
        link = (await client.get(f"{API}/chat/rooms/{room_id}/group", cookies=bob)).json()
        assert link["managed"] is True
        assert link["manage_url"] == MANAGE_URL
        assert link["can_set"] is False

    async def test_an_ordinary_room_is_unchanged(self, client, config, team):
        with db.get_db(config.db_path) as conn:
            token = db.create_web_chat_room(conn, "alice", "Ordinary").token
            db.set_room_group(conn, token, "team")
        alice = await _login(client, "alice")
        room_id = _handle_id(config, token, "alice")
        members = (await client.get(f"{API}/chat/rooms/{room_id}/members", cookies=alice)).json()
        assert members["can_manage"] is True and members["managed_by"] is None
        link = (await client.get(f"{API}/chat/rooms/{room_id}/group", cookies=alice)).json()
        assert link["managed"] is False and link["manage_url"] is None
        resp = await client.post(f"{API}/chat/rooms/{room_id}/members", cookies=alice,
                                 headers=ORIGIN, json={"user_id": "carol",
                                                       "acknowledge_history": True})
        assert resp.status_code == 201
        listed = await client.get(f"{API}/chat/rooms", cookies=alice)
        assert {r["token"]: r for r in listed.json()["rooms"]}[token]["managed_by"] is None


# ---------------------------------------------------------------------------
# No external audience at the db seams
# ---------------------------------------------------------------------------


class TestNoExternalAudience:
    def test_no_external_surface_binds_a_group_room(self, config, team, managed):
        with db.get_db(config.db_path) as conn:
            for surface, ref in (("talk", "abcd1234"), ("whatsapp", "group-jid"),
                                 ("email", "email-thread-x")):
                assert db.add_room_binding(conn, managed, surface, ref) is False
                assert db.replace_room_binding(conn, managed, surface, ref,
                                               expected_ref=None) is False
                assert db.get_room_binding(conn, managed, surface) is None
            # Its own web binding is not an external audience.
            assert db.add_room_binding(conn, managed, "web", managed) is True

    def test_an_ordinary_room_still_binds(self, config, team):
        with db.get_db(config.db_path) as conn:
            token = db.create_web_chat_room(conn, "alice", "Ordinary").token
            db.set_room_group(conn, token, "team")
            assert db.add_room_binding(conn, token, "talk", "abcd1234") is True

    def test_a_guest_is_never_enrolled(self, config, team, managed):
        with db.get_db(config.db_path) as conn:
            with pytest.raises(ValueError):
                db.upsert_room_participant(
                    conn, room_token=managed, surface="web", surface_ref="stranger",
                    kind="guest", display_name="Stranger")
            assert conn.execute(
                "SELECT COUNT(*) FROM room_participants WHERE room_token = ? AND kind = 'guest'",
                (managed,)).fetchone()[0] == 0
