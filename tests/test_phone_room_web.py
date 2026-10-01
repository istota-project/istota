"""The web half of a phone room (room-surface-model Stage 24).

A private SMS or WhatsApp conversation is a room web can read and cannot write
into (decided 2026-10-01). What this file holds the server to:

- the listing badges a phone-bound room and says which ones are read-only;
- a web send into a private phone room is refused by the server, since a
  hidden composer is not a gate, while a WhatsApp group room keeps its composer;
- a phone task's parked question is answered by text, not from web;
- a texted turn carries its surface on the history payload;
- the implicit default web room is never a phone room.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.transport.ingest import record_phone_turn
from istota.transport.routing import phone_transcript_surface
from istota.transport.sms import sms_conversation_token
from istota.transport.whatsapp import whatsapp_conversation_token

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
        users={"alice": UserConfig(display_name="Alice"), "bob": UserConfig()},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


def _mint(conn, config, surface="sms", user="alice", text="first text"):
    ref = (sms_conversation_token if surface == "sms" else whatsapp_conversation_token)(user)
    return record_phone_turn(
        conn, config, surface=surface, surface_ref=ref, user_id=user,
        text=text, channel_name="SMS" if surface == "sms" else "WhatsApp",
    )


def _group_room(conn, host="alice", jid="120363000000000001@g.us"):
    token = db.register_room(conn, None, host, origin="whatsapp", name="Family").token
    db.add_room_binding(conn, token, "whatsapp", jid)
    return token


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


async def _login(client, username="alice"):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


async def _rooms(client, cookies):
    resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
    assert resp.status_code == 200
    return {r["token"]: r for r in resp.json()["rooms"]}


class TestThePredicate:
    def test_private_threads_are_read_only_and_a_group_is_not(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            sms = _mint(conn, config, "sms").room_token
            wa = _mint(conn, config, "whatsapp").room_token
            group = _group_room(conn)
            web = db.create_web_chat_room(conn, "alice", "general").token
            assert phone_transcript_surface(conn, sms) == "sms"
            assert phone_transcript_surface(conn, wa) == "whatsapp"
            assert phone_transcript_surface(conn, group) is None
            assert phone_transcript_surface(conn, web) is None
            assert phone_transcript_surface(conn, "rm_nothing") is None

    def test_another_member_does_not_make_the_thread_writable(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            sms = _mint(conn, config, "sms").room_token
            db.add_room_member(conn, sms, "bob")
            assert phone_transcript_surface(conn, sms) == "sms"


class TestTheImplicitDefaultRoom:
    def test_a_phone_room_is_never_the_default(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            sms = _mint(conn, config, "sms").room_token
            db.ensure_web_chat_handle(conn, "alice", sms, "SMS")
            assert db.default_web_room(conn, "alice") is None
            # The writer mints a private room rather than delivering into the
            # read-only transcript.
            made = db.ensure_default_web_chat_room(conn, "alice")
            assert made.token != sms
            assert made.name == "general"

    def test_an_older_phone_room_does_not_outrank_a_web_room(self, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            wa = _mint(conn, config, "whatsapp").room_token
            db.ensure_web_chat_handle(conn, "alice", wa, "WhatsApp")
            web = db.create_web_chat_room(conn, "alice", "general")
            assert db.default_web_room(conn, "alice").token == web.token
            assert [r.token for r in db._default_room_candidates(conn, "alice")] == [
                web.token,
            ]


@web_only
class TestTheListing:
    async def test_badges_names_and_read_only(self, client, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            sms = _mint(conn, config, "sms").room_token
            wa = _mint(conn, config, "whatsapp").room_token
            group = _group_room(conn)
            web = db.create_web_chat_room(conn, "alice", "general").token
        rooms = await _rooms(client, await _login(client))
        assert (rooms[sms]["phone_surface"], rooms[sms]["read_only"]) == ("sms", True)
        assert rooms[sms]["name"] == "SMS"
        assert (rooms[wa]["phone_surface"], rooms[wa]["read_only"]) == ("whatsapp", True)
        assert rooms[wa]["name"] == "WhatsApp"
        assert (rooms[group]["phone_surface"], rooms[group]["read_only"]) == (
            "whatsapp", False,
        )
        assert (rooms[web]["phone_surface"], rooms[web]["read_only"]) == (None, False)


@web_only
class TestTheSendRefusal:
    async def test_a_web_send_into_a_phone_room_is_refused(self, client, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            sms = _mint(conn, config, "sms").room_token
        cookies = await _login(client)
        room_id = (await _rooms(client, cookies))[sms]["id"]
        with db.get_db(db_path) as conn:
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        for text in ("hello from web", "!help", "yes"):
            resp = await client.post(
                f"/istota/api/chat/rooms/{room_id}/messages",
                json={"text": text}, cookies=cookies, headers=ORIGIN,
            )
            assert resp.status_code == 409
            assert resp.json()["read_only"] is True
        with db.get_db(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
            assert [r["body"] for r in conn.execute(
                "SELECT body FROM messages WHERE room_token = ?", (sms,),
            )] == ["first text"]

    async def test_a_group_room_and_a_web_room_still_take_a_send(
        self, client, db_path, tmp_path,
    ):
        with db.get_db(db_path) as conn:
            group = _group_room(conn)
            web = db.create_web_chat_room(conn, "alice", "general").token
        cookies = await _login(client)
        rooms = await _rooms(client, cookies)
        for token in (group, web):
            resp = await client.post(
                f"/istota/api/chat/rooms/{rooms[token]['id']}/messages",
                json={"text": "hello"}, cookies=cookies, headers=ORIGIN,
            )
            assert resp.status_code == 200, resp.text


@web_only
class TestTheParkedQuestion:
    def _park(self, db_path, tmp_path, status="pending_confirmation"):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            result = _mint(conn, config, "sms", text="delete my calendar")
            conn.execute(
                "UPDATE tasks SET status = ?, confirmation_prompt = 'Sure?' "
                "WHERE id = ?", (status, result.task_id),
            )
        return result.task_id

    async def test_confirm_and_decline_are_refused(self, client, db_path, tmp_path):
        task_id = self._park(db_path, tmp_path)
        cookies = await _login(client)
        confirm = await client.post(
            f"/istota/api/chat/tasks/{task_id}/confirm", cookies=cookies, headers=ORIGIN,
        )
        assert confirm.status_code == 409
        decline = await client.post(
            f"/istota/api/chat/tasks/{task_id}/cancel", cookies=cookies, headers=ORIGIN,
        )
        assert decline.status_code == 409
        with db.get_db(db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending_confirmation"

    async def test_stopping_a_running_phone_task_is_still_allowed(
        self, client, db_path, tmp_path,
    ):
        task_id = self._park(db_path, tmp_path, status="running")
        cookies = await _login(client)
        resp = await client.post(
            f"/istota/api/chat/tasks/{task_id}/cancel", cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200
        with db.get_db(db_path) as conn:
            assert conn.execute(
                "SELECT cancel_requested FROM tasks WHERE id = ?", (task_id,),
            ).fetchone()[0] == 1


@web_only
class TestTheTextedTurn:
    async def test_history_marks_the_surface(self, client, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            sms = _mint(conn, config, "sms", text="texted in").room_token
        cookies = await _login(client)
        room_id = (await _rooms(client, cookies))[sms]["id"]
        resp = await client.get(
            f"/istota/api/chat/rooms/{room_id}/messages", cookies=cookies,
        )
        assert resp.status_code == 200
        rows = [m for m in resp.json()["messages"] if m["role"] == "user"]
        assert [(m["text"], m.get("via"), m.get("origin")) for m in rows] == [
            ("texted in", "sms", None),
        ]


class TestThePinnedDefaultAndRelays:
    def test_a_relay_never_lands_in_a_pinned_phone_room(self, db_path, tmp_path):
        from istota import relay_destinations, user_profiles
        from istota.whatsapp_requests import RequestError

        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            sms = _mint(conn, config, "sms").room_token
            db.ensure_web_chat_handle(conn, "alice", sms, "SMS")
        user_profiles.ensure_profile(db_path, "alice", display_name="Alice")
        user_profiles.update_profile(db_path, "alice", default_room=sms)
        with db.get_db(db_path) as conn:
            # The pin is the user's choice and still answers the lookup...
            assert db.default_web_room(conn, "alice").token == sms
            # ...but a question delivered there could never be answered.
            with pytest.raises(RequestError) as e:
                relay_destinations._room(conn, config, "alice")
            assert str(e.value) == "recipient_has_no_private_room"


class TestTheBellItem:
    def test_a_phone_question_has_no_buttons(self, db_path, tmp_path):
        from istota.notification_resolvers import confirmation
        from istota.notification_sources import NotificationRow

        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = _mint(conn, config, "sms", text="delete it").task_id
            db.set_task_confirmation(conn, task_id, "Sure?")
            row = NotificationRow(
                id=1, user_id="alice", source=confirmation.SOURCE,
                dedup_key=confirmation.dedup_key(task_id), object_type="task",
                object_id=str(task_id), severity="action_needed", actionable=True,
                title="Sure?", body="",
            )
            view = confirmation.RESOLVER.resolve(config, conn, row)
        assert view is not None
        assert view.actions == ()
        assert "Reply by SMS" in view.body


@web_only
class TestAPreMintTask:
    async def test_a_task_on_the_hash_token_is_refused_too(self, client, db_path, tmp_path):
        config = _config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="old question", user_id="alice", source_type="sms",
                conversation_token=sms_conversation_token("alice"),
            )
            db.set_task_confirmation(conn, task_id, "Sure?")
            _mint(conn, config, "sms")
        cookies = await _login(client)
        resp = await client.post(
            f"/istota/api/chat/tasks/{task_id}/confirm", cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 409
        decline = await client.post(
            f"/istota/api/chat/tasks/{task_id}/cancel", cookies=cookies, headers=ORIGIN,
        )
        assert decline.status_code == 409
        assert decline.json()["read_only"] is True
