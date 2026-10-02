"""Authority is not shared (multiplayer Stage 16, speech-gate draft B5 / SG 11).

The room is shared; the authority is not. A member acts on their own tasks and
their own messages, and a room's standing settings (name, model, effort,
brain, and turning it into a Talk conversation) are its host's once more than
one human reads it. These pin each guard against a two-member room, where a
co-member is the person the guard exists for, with the private room as the
control: every rule here is a no-op for a room one human reads.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import commands, confirmations, db
from istota.rooms import policy as room_policy
from istota.config import (
    Config,
    NextcloudConfig,
    SiteConfig,
    TalkConfig,
    UserConfig,
    WebConfig,
)

from .support.rooms import plain_talk_room

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


def _config(tmp_path, *, admins=("carol",)):
    path = tmp_path / "state.db"
    if not path.exists():
        db.init_db(path)
    return Config(
        db_path=path,
        temp_dir=tmp_path / "temp",
        workspace_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        nextcloud=NextcloudConfig(url="https://cloud.example.com", username="bot",
                                  app_password="secret"),
        talk=TalkConfig(enabled=True, bot_username="bot"),
        users={"alice": UserConfig(display_name="Alice"),
               "bob": UserConfig(display_name="Bob"),
               "carol": UserConfig(display_name="Carol")},
        # An explicit list: an empty one reads as "everyone is an admin", which
        # would let every command below through its admin exemption.
        admin_users=set(admins),
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


@pytest.fixture
def config(tmp_path):
    return _config(tmp_path)


def _group(conn, token="grp"):
    """A Talk group room Alice created and hosts, with Bob as a second member."""
    shape = plain_talk_room(conn, "alice", token=token, name="Family")
    db.add_room_member(conn, shape.canonical, "bob")
    return shape


def _task(conn, user="alice", *, token="grp", status="running", source_type="talk", **kw):
    task_id = db.create_task(
        conn, prompt="what is on today", user_id=user, source_type=source_type,
        conversation_token=token, is_group_chat=True, **kw,
    )
    conn.execute("UPDATE tasks SET status = ? WHERE id = ?", (status, task_id))
    return task_id


def _cancel_requested(conn, task_id):
    return bool(conn.execute(
        "SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,)).fetchone()[0])


def _run(config, conn, user, text, token="grp", surface="web"):
    return asyncio.run(commands.dispatch(
        config, user, token, text, surface=surface, conn=conn)).text or ""


# ---------------------------------------------------------------------------
# Confirmations: already guarded, pinned against a shared room
# ---------------------------------------------------------------------------


class TestConfirmationsInASharedRoom:
    def test_a_co_member_cannot_answer_anothers_confirmation(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            task_id = _task(conn, status="pending_confirmation")
            conn.execute("UPDATE tasks SET talk_response_id = 77 WHERE id = ?", (task_id,))
            # Path A (a reply to the prompt) and path B (the room) both find
            # Alice's task; the answerer check is what stops Bob.
            assert confirmations.resolve(
                conn, "bob", conversation_token="grp", talk_response_id=77,
            ).task is None
            assert confirmations.resolve(conn, "bob", conversation_token="grp").task is None
            reply = _run(config, conn, "bob", f"!confirm {task_id}")
            assert db.get_task(conn, task_id).status == "pending_confirmation"
            assert "alice" not in reply.lower() or "yours" in reply.lower()
            # The owner still can.
            assert confirmations.resolve(
                conn, "alice", conversation_token="grp", talk_response_id=77,
            ).task.id == task_id

    def test_the_docstring_names_the_shared_room(self):
        assert "shared room" in confirmations.resolve.__doc__


# ---------------------------------------------------------------------------
# !stop, !steer, !retry: the acting user must own the task
# ---------------------------------------------------------------------------


class TestTaskCommandsInASharedRoom:
    def test_a_co_members_bare_stop_leaves_anothers_task_running(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            task_id = _task(conn)
            reply = _run(config, conn, "bob", "!stop")
            assert "no active task" in reply.lower()
            assert not _cancel_requested(conn, task_id)

    def test_a_co_member_cannot_stop_a_task_by_id(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            task_id = _task(conn)
            reply = _run(config, conn, "bob", f"!stop {task_id}")
            assert "isn't yours" in reply
            assert not _cancel_requested(conn, task_id)
            assert "Cancelling" in _run(config, conn, "alice", "!stop")

    def test_a_co_member_cannot_steer_anothers_task(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            task_id = _task(conn)
            reply = _run(config, conn, "bob", "!steer skip the calendar")
            assert "no running task" in reply.lower()
            assert db.count_pending_steers(conn, task_id) == 0

    def test_a_co_member_cannot_retry_anothers_task(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            task_id = _task(conn, status="failed")
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
            assert "belongs to another user" in _run(config, conn, "bob", f"!retry #{task_id}")
            assert "no failed" in _run(config, conn, "bob", "!retry").lower()
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before

    @pytest.mark.parametrize("verb", ["!retry", "!resume"])
    def test_a_retried_guest_turn_is_still_a_guest_turn(self, config, verb):
        """A retry re-runs the guest's words; it must not re-run them as the
        host's own turn, with the host's grants and the host named as author."""
        with db.get_db(config.db_path) as conn:
            _group(conn)
            conn.execute(
                "INSERT INTO room_participants (room_token, surface, surface_ref, kind, "
                "display_name) VALUES ('grp', 'talk', 'guests/max', 'guest', 'Max')"
            )
            guest = conn.execute("SELECT id FROM room_participants").fetchone()[0]
            task_id = _task(conn, status="failed", guest_participant_id=guest,
                            audience="mixed")
            _run(config, conn, "alice", f"{verb} #{task_id}")
            new = db.get_task(conn, conn.execute(
                "SELECT MAX(id) FROM tasks").fetchone()[0])
            assert new.id != task_id and new.parent_task_id == task_id
            assert new.guest_participant_id == guest
            assert new.audience == "mixed"
            row = conn.execute(
                "SELECT author_user_id, author_participant_id FROM messages "
                "WHERE task_id = ? AND role = 'user'", (new.id,),
            ).fetchone()
            assert row is not None
            assert (row[0], row[1]) == (None, guest)


# ---------------------------------------------------------------------------
# Room settings: the host's, once the room is shared
# ---------------------------------------------------------------------------


class TestRoomSettingsCommands:
    def test_a_co_member_cannot_set_the_rooms_model_or_effort(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            for text in ("!room model default", "!room effort high"):
                reply = _run(config, conn, "bob", text)
                assert "host" in reply.lower()
            room = db.get_room(conn, "grp")
            assert room.effort is None

    def test_the_host_sets_them(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            assert "high" in _run(config, conn, "alice", "!room effort high")
            assert db.get_room(conn, "grp").effort == "high"

    def test_reading_the_settings_is_anyones(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            assert "host" not in _run(config, conn, "bob", "!room").lower()

    def test_a_private_rooms_one_member_sets_them_freely(self, config):
        with db.get_db(config.db_path) as conn:
            plain_talk_room(conn, "bob", token="solo", name="Mine")
            assert "high" in _run(config, conn, "bob", "!room effort high", token="solo")
            assert db.get_room(conn, "solo").effort == "high"

    def test_a_room_with_no_host_says_how_to_claim_it(self, config):
        with db.get_db(config.db_path) as conn:
            _group(conn)
            room_policy.ensure_policy(conn, "grp")
            conn.execute("UPDATE room_policy SET host_user_id = NULL")
            reply = _run(config, conn, "bob", "!room effort high")
            assert "!room host" in reply
            assert db.get_room(conn, "grp").effort is None

    def test_an_admin_who_is_not_the_host_cannot_set_the_brain(self, tmp_path):
        config = _config(tmp_path, admins=("bob",))
        config.brain.room_selectable = ["native"]
        with db.get_db(config.db_path) as conn:
            _group(conn)
            reply = _run(config, conn, "bob", "!brain native")
            assert "host" in reply.lower()
            assert not db.get_room(conn, "grp").brain


# ---------------------------------------------------------------------------
# The web half
# ---------------------------------------------------------------------------


@pytest.fixture
async def client(tmp_path):
    import istota.web_app as mod
    config = _config(tmp_path, admins=("bob",))
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    transport = ASGITransport(app=mod.app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        c.istota_config = config
        yield c


async def _login(client, username):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


def _shared_web_room(config):
    """Alice's web room with Bob added: ``(token, alice's handle id)``. Bob's
    handle is minted by his first room listing, as it is for a real member."""
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "plans")
        db.add_room_member(conn, room.token, "bob")
    return room.token, room.id


async def _handle(client, cookies, token):
    resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
    return {r["token"]: r["id"] for r in resp.json()["rooms"]}[token]


def _message(config, token, author, body="hi", **kw):
    with db.get_db(config.db_path) as conn:
        return db.add_message(conn, token, role="user", body=body,
                              origin_surface="web", author_user_id=author, **kw)


@web_only
class TestRoomSettingsOnTheWeb:
    @pytest.mark.parametrize("body", [
        {"name": "renamed"}, {"effort": "high"}, {"model": ""}, {"brain": ""},
    ])
    async def test_a_co_member_cannot_change_a_room_global_setting(self, client, body):
        config = client.istota_config
        token, _alice_id = _shared_web_room(config)
        bob = await _login(client, "bob")
        room_id = await _handle(client, bob, token)
        resp = await client.patch(f"/istota/api/chat/rooms/{room_id}", json=body,
                                  cookies=bob, headers=ORIGIN)
        assert resp.status_code == 403
        with db.get_db(config.db_path) as conn:
            room = db.get_room(conn, token)
            assert room.name == "plans" and room.effort is None

    async def test_a_co_member_cannot_open_the_room_in_talk(self, client):
        config = client.istota_config
        token, _alice_id = _shared_web_room(config)
        bob = await _login(client, "bob")
        room_id = await _handle(client, bob, token)
        resp = await client.post(f"/istota/api/chat/rooms/{room_id}/promote",
                                 cookies=bob, headers=ORIGIN)
        assert resp.status_code == 403
        with db.get_db(config.db_path) as conn:
            assert db.get_room_binding(conn, token, "talk") is None

    async def test_a_co_members_own_colour_and_hide_are_theirs(self, client):
        config = client.istota_config
        token, _alice_id = _shared_web_room(config)
        bob = await _login(client, "bob")
        room_id = await _handle(client, bob, token)
        resp = await client.patch(f"/istota/api/chat/rooms/{room_id}",
                                  json={"color": ""}, cookies=bob, headers=ORIGIN)
        assert resp.status_code == 200

    async def test_the_host_renames(self, client):
        config = client.istota_config
        token, alice_id = _shared_web_room(config)
        alice = await _login(client, "alice")
        resp = await client.patch(f"/istota/api/chat/rooms/{alice_id}",
                                  json={"name": "renamed", "effort": "high"},
                                  cookies=alice, headers=ORIGIN)
        assert resp.status_code == 200
        with db.get_db(config.db_path) as conn:
            assert db.get_room(conn, token).effort == "high"

    async def test_a_co_member_writes_the_rooms_notes(self, client, tmp_path):
        """Decided, not overlooked: `CHANNEL.md` is the room's front-stage memory,
        read into the user half of every member's task, and editing it is how a
        member keeps something from a newcomer (D19). Any member writes it."""
        config = client.istota_config
        token, _alice_id = _shared_web_room(config)
        bob = await _login(client, "bob")
        room_id = await _handle(client, bob, token)
        resp = await client.get(f"/istota/api/chat/rooms/{room_id}/memory", cookies=bob)
        assert resp.status_code == 200
        body = {"content": "# notes\n", "revision": resp.json().get("revision")}
        resp = await client.put(f"/istota/api/chat/rooms/{room_id}/memory", json=body,
                                cookies=bob, headers=ORIGIN)
        assert resp.status_code == 200


@web_only
class TestMessageDeletionOnTheWeb:
    async def test_a_co_member_cannot_delete_anothers_message(self, client):
        config = client.istota_config
        token, _a = _shared_web_room(config)
        mine = _message(config, token, "alice")
        bob = await _login(client, "bob")
        await _handle(client, bob, token)
        resp = await client.delete(f"/istota/api/chat/messages/{mine}",
                                   cookies=bob, headers=ORIGIN)
        assert resp.status_code == 403
        with db.get_db(config.db_path) as conn:
            assert db.get_message_room(conn, mine) == token

    async def test_nor_the_bots_answer_to_another_member(self, client):
        config = client.istota_config
        token, _a = _shared_web_room(config)
        with db.get_db(config.db_path) as conn:
            task_id = _task(conn, "alice", token=token, status="completed",
                            source_type="web")
            answer = db.add_message(conn, token, role="assistant", body="done",
                                    origin_surface="web", task_id=task_id)
        bob = await _login(client, "bob")
        await _handle(client, bob, token)
        resp = await client.delete(f"/istota/api/chat/messages/{answer}",
                                   cookies=bob, headers=ORIGIN)
        assert resp.status_code == 403
        alice = await _login(client, "alice")
        resp = await client.delete(f"/istota/api/chat/messages/{answer}",
                                   cookies=alice, headers=ORIGIN)
        assert resp.status_code == 200

    async def test_a_member_deletes_their_own(self, client):
        config = client.istota_config
        token, _a = _shared_web_room(config)
        own = _message(config, token, "bob")
        bob = await _login(client, "bob")
        await _handle(client, bob, token)
        resp = await client.delete(f"/istota/api/chat/messages/{own}",
                                   cookies=bob, headers=ORIGIN)
        assert resp.status_code == 200

    async def test_the_host_is_no_moderator(self, client):
        config = client.istota_config
        token, _a = _shared_web_room(config)
        bobs = _message(config, token, "bob")
        alice = await _login(client, "alice")
        resp = await client.delete(f"/istota/api/chat/messages/{bobs}",
                                   cookies=alice, headers=ORIGIN)
        assert resp.status_code == 403

    async def test_a_private_rooms_member_deletes_anything(self, client):
        config = client.istota_config
        with db.get_db(config.db_path) as conn:
            room = db.create_web_chat_room(conn, "alice", "solo")
            note = db.add_message(conn, room.token, role="system", body="notice",
                                  origin_surface="web")
        alice = await _login(client, "alice")
        resp = await client.delete(f"/istota/api/chat/messages/{note}",
                                   cookies=alice, headers=ORIGIN)
        assert resp.status_code == 200


@web_only
class TestTaskEndpointsOnTheWeb:
    async def test_a_co_member_cannot_watch_or_cancel_anothers_task(self, client):
        config = client.istota_config
        token, _a = _shared_web_room(config)
        with db.get_db(config.db_path) as conn:
            task_id = _task(conn, "alice", token=token, source_type="web")
        carol = await _login(client, "carol")  # a non-admin co-member
        with db.get_db(config.db_path) as conn:
            db.add_room_member(conn, token, "carol")
        for path in (f"/istota/api/chat/tasks/{task_id}/events",):
            assert (await client.get(path, cookies=carol)).status_code == 403
        resp = await client.post(f"/istota/api/chat/tasks/{task_id}/cancel",
                                 cookies=carol, headers=ORIGIN)
        assert resp.status_code == 403
        with db.get_db(config.db_path) as conn:
            assert not _cancel_requested(conn, task_id)

    async def test_an_admin_cannot_answer_anothers_held_task(self, client):
        """`!confirm` has no admin exemption and `!stop` refuses an admin on a
        held task; the web routes were the back door onto both."""
        config = client.istota_config
        token, _a = _shared_web_room(config)
        with db.get_db(config.db_path) as conn:
            task_id = _task(conn, "alice", token=token, source_type="web",
                            status="pending_confirmation")
        bob = await _login(client, "bob")  # an admin
        for verb in ("confirm", "cancel"):
            resp = await client.post(f"/istota/api/chat/tasks/{task_id}/{verb}",
                                     cookies=bob, headers=ORIGIN)
            assert resp.status_code == 403
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending_confirmation"

    async def test_an_admin_still_cancels_a_running_task(self, client):
        config = client.istota_config
        token, _a = _shared_web_room(config)
        with db.get_db(config.db_path) as conn:
            task_id = _task(conn, "alice", token=token, source_type="web")
        bob = await _login(client, "bob")
        resp = await client.post(f"/istota/api/chat/tasks/{task_id}/cancel",
                                 cookies=bob, headers=ORIGIN)
        assert resp.status_code == 200
        with db.get_db(config.db_path) as conn:
            assert _cancel_requested(conn, task_id)
