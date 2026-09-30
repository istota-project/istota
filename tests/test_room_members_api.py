"""Web room membership (multiplayer Stage 8, speech-gate draft SG 7 / B1–B2).

A web room gains its second human here. The write path is three endpoints and a
users directory; the authorization rules are the draft's D3 — only the room's
creator adds or removes, a member may remove themselves, a Talk-bound room takes
its membership from Talk and refuses both. What these tests pin beyond the
status codes is that a removal is a removal: the removed member keeps no route
back in through their old handle, and a co-member cannot destroy or globally
archive a room they did not create.

Also here: `room_data_grants` (schema and markered migration, no backfill) and
`is_group_chat` on a task being recomputed from the one multi-human predicate.
"""

import sqlite3
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.transport.ingest import record_inbound

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
        workspace_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        users={
            "alice": UserConfig(display_name="Alice", email_addresses=["a@x.test"]),
            "bob": UserConfig(display_name="Bob", email_addresses=["b@x.test"]),
            "carol": UserConfig(),
        },
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


# ---------------------------------------------------------------------------
# room_data_grants: schema and migration
# ---------------------------------------------------------------------------


def _pre_grants_schema() -> str:
    schema = (Path(__file__).parents[1] / "schema.sql").read_text()
    start = schema.index("-- A member's standing consent")
    end = schema.index("-- The speech gate's audit trail")
    return schema[:start] + schema[end:]


class TestRoomDataGrantsMigration:
    def test_the_migration_creates_what_a_fresh_install_gets(self, tmp_path, db_path):
        old = tmp_path / "old.db"
        with sqlite3.connect(old) as conn:
            conn.executescript(_pre_grants_schema())
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'room_data_grants'"
            ).fetchone() is None
        # The migrations alone, not schema.sql: that is what an upgrade runs
        # before the schema file, and what must stand on its own.
        with sqlite3.connect(old) as conn:
            conn.row_factory = sqlite3.Row
            db._run_migrations(conn)
            conn.commit()
        with db.get_db(db_path) as fresh, db.get_db(old) as upgraded:
            a = [tuple(r) for r in fresh.execute("PRAGMA table_info(room_data_grants)")]
            b = [tuple(r) for r in upgraded.execute("PRAGMA table_info(room_data_grants)")]
            assert a and a == b
            assert upgraded.execute(
                "SELECT 1 FROM _migration_state WHERE name = 'room_grants_v1'"
            ).fetchone() is not None

    def test_no_grant_is_backfilled_for_an_existing_shared_room(self, tmp_path):
        old = tmp_path / "old.db"
        with sqlite3.connect(old) as conn:
            conn.executescript(_pre_grants_schema())
            conn.execute(
                "INSERT INTO rooms (token, user_id, origin) VALUES ('grp', 'alice', 'talk')"
            )
            conn.executemany(
                "INSERT INTO room_members (room_token, user_id) VALUES ('grp', ?)",
                [("alice",), ("bob",)],
            )
        db.init_db(old)
        db.init_db(old)
        with db.get_db(old) as conn:
            assert conn.execute("SELECT COUNT(*) FROM room_data_grants").fetchone()[0] == 0

    def test_deleting_a_room_drops_its_grants(self, db_path):
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "mine")
            conn.execute(
                "INSERT INTO room_data_grants (room_token, user_id, scope) "
                "VALUES (?, 'alice', 'calendar')", (room.token,),
            )
            assert db.delete_web_chat_room(conn, room.id, "alice")
            assert conn.execute(
                "SELECT COUNT(*) FROM room_data_grants WHERE room_token = ?",
                (room.token,),
            ).fetchone()[0] == 0


# ---------------------------------------------------------------------------
# is_group_chat recomputed at the choke point
# ---------------------------------------------------------------------------


def _task_group_flag(conn, task_id):
    return bool(conn.execute(
        "SELECT is_group_chat FROM tasks WHERE id = ?", (task_id,),
    ).fetchone()[0])


class TestIsGroupChatRecomputed:
    def test_a_web_task_in_a_two_member_room_is_a_group_task(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "shared")
            db.add_room_member(conn, room.token, "bob")
            result = record_inbound(
                conn, config, surface="web", surface_ref=room.token,
                user_id="alice", text="@Istota hello", addressed_to_bot=True,
            )
            assert result.outcome == "created"
            assert _task_group_flag(conn, result.task_id)

    def test_a_private_web_room_stays_a_direct_conversation(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "mine")
            result = record_inbound(
                conn, config, surface="web", surface_ref=room.token,
                user_id="alice", text="hello",
            )
            assert result.outcome == "created"
            assert not _task_group_flag(conn, result.task_id)

    def test_email_keeps_its_own_answer(self, db_path, tmp_path):
        """A guest surface's reply goes back by mail: a shared room it mirrors
        into is not its audience, so the task is not made a group task."""
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "shared")
            db.add_room_member(conn, room.token, "bob")
            result = record_inbound(
                conn, config, surface="email", surface_ref="thread-hash",
                user_id="alice", text="hello", output_target=f"web:{room.token}",
            )
            assert result.task_id is not None
            assert not _task_group_flag(conn, result.task_id)


# ---------------------------------------------------------------------------
# The endpoints
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(db_path, tmp_path):
    import istota.web_app as mod
    config = _config(db_path, tmp_path)
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    transport = ASGITransport(app=mod.app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


async def _login(client, username):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


def _handle(db_path, user_id, token):
    with db.get_db(db_path) as conn:
        row = conn.execute(
            "SELECT id FROM web_chat_rooms WHERE user_id = ? AND token = ?",
            (user_id, token),
        ).fetchone()
    return row["id"] if row else None


def _new_room(db_path, owner="alice", name="plans"):
    with db.get_db(db_path) as conn:
        room = db.create_web_chat_room(conn, owner, name)
        db.add_message(conn, room.token, role="user", body="private note",
                       origin_surface="web", author_user_id=owner)
    return room


async def _add(client, cookies, room_id, user_id, ack=True):
    body = {"user_id": user_id}
    if ack is not None:
        body["acknowledge_history"] = ack
    return await client.post(
        f"/istota/api/chat/rooms/{room_id}/members", json=body,
        cookies=cookies, headers=ORIGIN,
    )


async def _remove(client, cookies, room_id, user_id):
    return await client.delete(
        f"/istota/api/chat/rooms/{room_id}/members/{user_id}",
        cookies=cookies, headers=ORIGIN,
    )


async def _room_ids(client, cookies):
    resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
    return {r["token"]: r["id"] for r in resp.json()["rooms"]}


@web_only
class TestUsersDirectory:
    async def test_lists_every_user_with_a_display_name_and_nothing_else(self, client):
        cookies = await _login(client, "alice")
        resp = await client.get("/istota/api/chat/users", cookies=cookies)
        assert resp.status_code == 200
        users = resp.json()["users"]
        assert users == [
            {"user_id": "alice", "display_name": "Alice"},
            {"user_id": "bob", "display_name": "Bob"},
            {"user_id": "carol", "display_name": "carol"},
        ]
        assert "x.test" not in resp.text

    async def test_requires_a_session(self, client):
        resp = await client.get("/istota/api/chat/users")
        assert resp.status_code == 401


@web_only
class TestAddingAMember:
    async def test_the_creator_adds_a_member_who_then_sees_the_room(self, client, db_path):
        room = _new_room(db_path)
        alice = await _login(client, "alice")
        resp = await _add(client, alice, room.id, "bob")
        assert resp.status_code == 201
        assert resp.json()["member"] == {
            "user_id": "bob", "display_name": "Bob", "is_owner": False,
        }
        bob = await _login(client, "bob")
        assert room.token in await _room_ids(client, bob)
        with db.get_db(db_path) as conn:
            assert db.is_room_member(conn, room.token, "bob")
            assert db.room_is_shared(conn, room.token)
            row = conn.execute(
                "SELECT kind, user_id, left_at FROM room_participants "
                "WHERE room_token = ? AND surface = 'web' AND surface_ref = 'bob'",
                (room.token,),
            ).fetchone()
            assert dict(row) == {"kind": "principal", "user_id": "bob", "left_at": None}

    @pytest.mark.parametrize("ack", [None, False, "true", 1])
    async def test_history_must_be_acknowledged_with_a_real_true(self, client, db_path, ack):
        room = _new_room(db_path)
        alice = await _login(client, "alice")
        resp = await _add(client, alice, room.id, "bob", ack=ack)
        assert resp.status_code == 400
        with db.get_db(db_path) as conn:
            assert not db.is_room_member(conn, room.token, "bob")

    async def test_an_unknown_user_is_refused(self, client, db_path):
        room = _new_room(db_path)
        alice = await _login(client, "alice")
        resp = await _add(client, alice, room.id, "mallory")
        assert resp.status_code == 400
        with db.get_db(db_path) as conn:
            assert not db.is_room_member(conn, room.token, "mallory")

    async def test_a_member_who_is_not_the_creator_cannot_add(self, client, db_path):
        room = _new_room(db_path)
        alice = await _login(client, "alice")
        await _add(client, alice, room.id, "bob")
        bob = await _login(client, "bob")
        bob_room = (await _room_ids(client, bob))[room.token]
        resp = await _add(client, bob, bob_room, "carol")
        assert resp.status_code == 403
        with db.get_db(db_path) as conn:
            assert not db.is_room_member(conn, room.token, "carol")

    async def test_a_non_member_learns_nothing(self, client, db_path):
        room = _new_room(db_path)
        carol = await _login(client, "carol")
        assert (await _add(client, carol, room.id, "carol")).status_code == 404
        resp = await client.get(
            f"/istota/api/chat/rooms/{room.id}/members", cookies=carol,
        )
        assert resp.status_code == 404
        with db.get_db(db_path) as conn:
            assert not db.is_room_member(conn, room.token, "carol")

    async def test_a_talk_bound_room_refuses_membership_writes(self, client, db_path):
        room = _new_room(db_path)
        with db.get_db(db_path) as conn:
            db.add_room_binding(conn, room.token, "talk", "nc-tok")
            db.add_room_member(conn, room.token, "bob")
        alice = await _login(client, "alice")
        assert (await _add(client, alice, room.id, "carol")).status_code == 409
        assert (await _remove(client, alice, room.id, "bob")).status_code == 409
        with db.get_db(db_path) as conn:
            assert sorted(db.list_room_members(conn, room.token)) == ["alice", "bob"]

    async def test_readding_a_member_who_hid_the_room_shows_it_again(self, client, db_path):
        room = _new_room(db_path)
        with db.get_db(db_path) as conn:
            db.dismiss_room(conn, room.token, "bob")
        alice = await _login(client, "alice")
        assert (await _add(client, alice, room.id, "bob")).status_code == 201
        bob = await _login(client, "bob")
        assert room.token in await _room_ids(client, bob)


@web_only
class TestListingMembers:
    async def test_members_and_who_may_manage(self, client, db_path):
        room = _new_room(db_path)
        alice = await _login(client, "alice")
        await _add(client, alice, room.id, "bob")
        resp = await client.get(f"/istota/api/chat/rooms/{room.id}/members", cookies=alice)
        assert resp.status_code == 200
        assert resp.json() == {
            "members": [
                {"user_id": "alice", "display_name": "Alice", "is_owner": True},
                {"user_id": "bob", "display_name": "Bob", "is_owner": False},
            ],
            "can_manage": True,
        }
        bob = await _login(client, "bob")
        bob_room = (await _room_ids(client, bob))[room.token]
        resp = await client.get(f"/istota/api/chat/rooms/{bob_room}/members", cookies=bob)
        assert resp.status_code == 200
        assert resp.json()["can_manage"] is False


@web_only
class TestRemovingAMember:
    async def _shared(self, client, db_path):
        room = _new_room(db_path)
        alice = await _login(client, "alice")
        await _add(client, alice, room.id, "bob")
        bob = await _login(client, "bob")
        bob_room = (await _room_ids(client, bob))[room.token]
        return room, alice, bob, bob_room

    async def test_a_removed_member_has_no_way_back_in(self, client, db_path):
        room, alice, bob, bob_room = await self._shared(client, db_path)
        assert (await _remove(client, alice, room.id, "bob")).status_code == 204
        with db.get_db(db_path) as conn:
            assert not db.is_room_member(conn, room.token, "bob")
            assert not db.room_is_shared(conn, room.token)
        assert _handle(db_path, "bob", room.token) is None
        # Their old handle opens nothing: not a send (which would re-add them
        # through `record_inbound`), not an un-hide, not a read. Asked before
        # listing bob's rooms, which mints him a default room that may reuse
        # the freed handle id — his own room, so harmless, but not this one.
        send = await client.post(
            f"/istota/api/chat/rooms/{bob_room}/messages",
            json={"text": "let me back in"}, cookies=bob, headers=ORIGIN,
        )
        assert send.status_code == 404
        unhide = await client.patch(
            f"/istota/api/chat/rooms/{bob_room}", json={"archived": False},
            cookies=bob, headers=ORIGIN,
        )
        assert unhide.status_code == 404
        read = await client.get(
            f"/istota/api/chat/rooms/{bob_room}/messages", cookies=bob,
        )
        assert read.status_code == 404
        with db.get_db(db_path) as conn:
            assert not db.is_room_member(conn, room.token, "bob")
        assert room.token not in await _room_ids(client, bob)

    async def test_a_member_removes_themselves(self, client, db_path):
        room, alice, bob, bob_room = await self._shared(client, db_path)
        assert (await _remove(client, bob, bob_room, "bob")).status_code == 204
        with db.get_db(db_path) as conn:
            assert db.list_room_members(conn, room.token) == ["alice"]

    async def test_a_member_cannot_remove_someone_else(self, client, db_path):
        room, alice, bob, bob_room = await self._shared(client, db_path)
        assert (await _remove(client, bob, bob_room, "alice")).status_code == 403
        with db.get_db(db_path) as conn:
            assert sorted(db.list_room_members(conn, room.token)) == ["alice", "bob"]

    async def test_the_creator_cannot_leave_their_own_room(self, client, db_path):
        room, alice, bob, bob_room = await self._shared(client, db_path)
        assert (await _remove(client, alice, room.id, "alice")).status_code == 409
        with db.get_db(db_path) as conn:
            assert db.is_room_member(conn, room.token, "alice")

    async def test_removing_someone_who_is_not_a_member_is_404(self, client, db_path):
        room = _new_room(db_path)
        alice = await _login(client, "alice")
        assert (await _remove(client, alice, room.id, "carol")).status_code == 404

    async def test_a_removed_member_can_be_added_back(self, client, db_path):
        room, alice, bob, bob_room = await self._shared(client, db_path)
        await _remove(client, alice, room.id, "bob")
        assert (await _add(client, alice, room.id, "bob")).status_code == 201
        assert room.token in await _room_ids(client, bob)


@web_only
class TestACoMemberCannotDestroyTheRoom:
    async def _shared(self, client, db_path):
        room = _new_room(db_path)
        alice = await _login(client, "alice")
        await _add(client, alice, room.id, "bob")
        bob = await _login(client, "bob")
        bob_room = (await _room_ids(client, bob))[room.token]
        return room, alice, bob, bob_room

    async def test_deleting_a_room_you_did_not_create_leaves_it(self, client, db_path):
        room, alice, bob, bob_room = await self._shared(client, db_path)
        resp = await client.delete(
            f"/istota/api/chat/rooms/{bob_room}", cookies=bob, headers=ORIGIN,
        )
        assert resp.status_code == 200
        with db.get_db(db_path) as conn:
            assert db.get_room(conn, room.token) is not None
            assert db.list_room_members(conn, room.token) == ["alice"]
            assert conn.execute(
                "SELECT COUNT(*) FROM messages WHERE room_token = ?", (room.token,),
            ).fetchone()[0] == 1
        assert room.token in await _room_ids(client, alice)
        assert _handle(db_path, "bob", room.token) is None

    async def test_archiving_a_room_you_did_not_create_hides_it_for_you_only(
        self, client, db_path,
    ):
        room, alice, bob, bob_room = await self._shared(client, db_path)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{bob_room}", json={"archived": True},
            cookies=bob, headers=ORIGIN,
        )
        assert resp.status_code == 200
        with db.get_db(db_path) as conn:
            assert not db.get_room(conn, room.token).archived
            # Hiding is not leaving: bob can still come back, so he is still
            # someone an answer in this room reaches.
            assert db.is_room_member(conn, room.token, "bob")
        assert room.token in await _room_ids(client, alice)
        assert room.token not in await _room_ids(client, bob)
        resp = await client.patch(
            f"/istota/api/chat/rooms/{bob_room}", json={"archived": False},
            cookies=bob, headers=ORIGIN,
        )
        assert resp.status_code == 200
        assert room.token in await _room_ids(client, bob)
