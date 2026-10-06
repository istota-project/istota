"""An email thread room is read-only in web (hidden-email-threads, stage 1).

The thread room is the mail thread itself. A web send there became an ordinary
web task, answered in web and mailed to nobody, so the server refuses it the
way it refuses a phone room, and the listing says the room is read-only. What
stays open is what is not a web turn: approving a held draft. A parked question
on the thread's task is the host's private room's alone (#665): the thread room
shows no card for it and refuses a confirm sent from it.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota import db
from istota.config import Config, EmailConfig, SiteConfig, UserConfig, WebConfig
from istota.mail import drafts as outbound_drafts
from istota.transport.email.private_room import email_conversation_token
from istota.rooms.scopes import is_email_thread_room

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
THREAD_WORDING = "This is an email thread."


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


def _thread_room(conn, user="alice"):
    token = db.register_room(conn, None, user, origin="email", name="Book club").token
    db.add_room_binding(conn, token, "email", "<root@example.com>")
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


async def _send(client, cookies, room_id, payload):
    return await client.post(
        f"/istota/api/chat/rooms/{room_id}/messages",
        json=payload, cookies=cookies, headers=ORIGIN,
    )


class TestThePredicate:
    def test_only_a_thread_room_answers(self, db_path):
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
            private = _private_email_room(conn)
            web = db.create_web_chat_room(conn, "alice", "general").token
            assert is_email_thread_room(conn, thread) is True
            assert is_email_thread_room(conn, private) is False
            assert is_email_thread_room(conn, web) is False
            assert is_email_thread_room(conn, None) is False
            assert is_email_thread_room(conn, "rm_nothing") is False

    def test_the_listing_batch_agrees(self, db_path):
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
            _private_email_room(conn)
            db.create_web_chat_room(conn, "alice", "general")
            assert db.email_thread_tokens_for_member(conn, "alice") == {thread}


@web_only
class TestTheRoomPayload:
    async def test_a_thread_room_is_read_only(self, client, db_path):
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
            private = _private_email_room(conn)
            web = db.create_web_chat_room(conn, "alice", "general").token
        rooms = await _rooms(client, await _login(client))
        assert rooms[thread]["read_only"] is True
        assert rooms[thread]["email_thread"] is True
        assert rooms[thread]["phone_surface"] is None
        assert (rooms[private]["read_only"], rooms[private]["email_thread"]) == (True, False)
        assert rooms[private]["phone_surface"] == "email"
        assert (rooms[web]["read_only"], rooms[web]["email_thread"]) == (False, False)

    async def test_the_patch_response_carries_the_same_keys(self, client, db_path):
        """The client spreads this response over its room record (ISSUE-342),
        so it answers the same read-only fields the listing does."""
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
        cookies = await _login(client)
        room_id = (await _rooms(client, cookies))[thread]["id"]
        resp = await client.patch(
            f"/istota/api/chat/rooms/{room_id}",
            json={"name": "Saturday"}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert (body["read_only"], body["email_thread"]) == (True, True)


@web_only
class TestTheSendRefusal:
    async def test_a_send_a_command_and_a_reply_are_refused(self, client, db_path):
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
            row = db.add_message(
                conn, thread, role="user", body="Come on Saturday?",
                origin_surface="email",
            )
            before = conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
        cookies = await _login(client)
        room_id = (await _rooms(client, cookies))[thread]["id"]
        for payload in (
            {"text": "tell them I'm in"},
            {"text": "!help"},
            {"text": "yes", "reply_to_msg_id": row},
        ):
            resp = await _send(client, cookies, room_id, payload)
            assert resp.status_code == 409, payload
            body = resp.json()
            assert body["read_only"] is True
            assert body["error"].startswith(THREAD_WORDING)
        with db.get_db(db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before
            assert [r["body"] for r in conn.execute(
                "SELECT body FROM messages WHERE room_token = ?", (thread,),
            )] == ["Come on Saturday?"]

    async def test_the_private_email_room_keeps_its_own_wording(self, client, db_path):
        with db.get_db(db_path) as conn:
            private = _private_email_room(conn)
        cookies = await _login(client)
        room_id = (await _rooms(client, cookies))[private]["id"]
        resp = await _send(client, cookies, room_id, {"text": "hello"})
        assert resp.status_code == 409
        assert "transcript of your email conversation" in resp.json()["error"]

    async def test_a_whatsapp_group_keeps_its_own_wording(self, client, db_path):
        with db.get_db(db_path) as conn:
            group = db.register_room(conn, None, "alice", origin="whatsapp",
                                     name="Family").token
            db.add_room_binding(conn, group, "whatsapp", "120363000000000001@g.us")
        cookies = await _login(client)
        room_id = (await _rooms(client, cookies))[group]["id"]
        resp = await _send(client, cookies, room_id, {"text": "hello"})
        assert resp.status_code == 409
        assert "WhatsApp group" in resp.json()["error"]

    async def test_a_web_room_still_takes_a_send(self, client, db_path):
        with db.get_db(db_path) as conn:
            web = db.create_web_chat_room(conn, "alice", "general").token
        cookies = await _login(client)
        room_id = (await _rooms(client, cookies))[web]["id"]
        resp = await _send(client, cookies, room_id, {"text": "hello"})
        assert resp.status_code == 200, resp.text


def _parked_thread_task(conn, thread, user="alice"):
    task_id = db.create_task(
        conn, prompt="reply to the invite", user_id=user,
        source_type="email", conversation_token=thread,
    )
    db.add_message(
        conn, thread, role="user", body="Come on Saturday?",
        origin_surface="email", task_id=task_id,
    )
    db.set_task_confirmation(conn, task_id, "Send the reply?")
    return task_id


@web_only
class TestTheParkedQuestionIsNotInTheThread:
    """#665: the thread room records what took place on the mail; the question
    is the host's private room's (and the bell's, and `!confirm`'s)."""

    def test_the_history_shows_no_card(self, client, db_path):
        import istota.webui.app as mod
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
            task_id = _parked_thread_task(conn, thread)
        hist = mod._chat_room_messages("alice", thread, 50, None)
        assert [m for m in hist["messages"] if m.get("confirmation")] == []
        assert [m["role"] for m in hist["messages"] if m.get("task_id") == task_id] == ["user"]
        assert hist["active_tasks"] == []
        assert hist["active_task"] is None

    def test_a_web_room_still_shows_its_card(self, client, db_path):
        import istota.webui.app as mod
        with db.get_db(db_path) as conn:
            web = db.create_web_chat_room(conn, "alice", "general").token
            task_id = db.create_task(
                conn, prompt="send it", user_id="alice",
                source_type="web", conversation_token=web,
            )
            db.set_task_confirmation(conn, task_id, "Send the reply?")
        hist = mod._chat_room_messages("alice", web, 50, None)
        cards = [m for m in hist["messages"] if m.get("confirmation")]
        assert [c["task_id"] for c in cards] == [task_id]

    def test_the_question_is_not_on_screen_in_the_thread(self, db_path):
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
            task_id = _parked_thread_task(conn, thread)
            assert db.task_shown_in_room(conn, task_id, "alice", thread) is False

    async def test_a_confirm_from_the_thread_room_is_refused(self, client, db_path):
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
            task_id = _parked_thread_task(conn, thread)
        cookies = await _login(client)
        resp = await client.post(
            f"/istota/api/chat/tasks/{task_id}/confirm",
            json={"room": thread}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 409, resp.text
        assert "private chat" in resp.json()["detail"]
        with db.get_db(db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending_confirmation"

    async def test_a_confirm_from_the_private_room_works(self, client, db_path):
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
            private = db.create_web_chat_room(conn, "alice", "general").token
            task_id = _parked_thread_task(conn, thread)
        cookies = await _login(client)
        resp = await client.post(
            f"/istota/api/chat/tasks/{task_id}/confirm",
            json={"room": private}, cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200, resp.text
        with db.get_db(db_path) as conn:
            assert db.get_task(conn, task_id).status != "pending_confirmation"

    async def test_a_confirm_from_the_bell_works(self, client, db_path):
        """The bell's POST names no room."""
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
            task_id = _parked_thread_task(conn, thread)
        cookies = await _login(client)
        resp = await client.post(
            f"/istota/api/chat/tasks/{task_id}/confirm", cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200, resp.text
        with db.get_db(db_path) as conn:
            assert db.get_task(conn, task_id).status != "pending_confirmation"


@web_only
class TestWhatStaysOpen:
    async def test_a_draft_in_the_thread_is_still_approved(self, client, db_path):
        with db.get_db(db_path) as conn:
            thread = _thread_room(conn)
            draft_id = outbound_drafts.hold(
                conn, user_id="alice", task_id=None, room_token=thread,
                to_addrs=["ana@example.invalid"], cc_addrs=[], bcc_addrs=[],
                subject="Re: Book club", body="Alice is in.", html=False,
                in_reply_to="<root@example.com>", references="<root@example.com>",
                attachments=[], origin_target=None,
                hold_reason="untrusted_recipient",
            )
        cookies = await _login(client)
        with patch("istota.skills.email.send_email") as send:
            send.return_value = "<sent@example.com>"
            resp = await client.post(
                f"/istota/api/chat/drafts/{draft_id}/approve",
                cookies=cookies, headers=ORIGIN,
            )
        assert resp.status_code == 200, resp.text
        assert send.call_count == 1
