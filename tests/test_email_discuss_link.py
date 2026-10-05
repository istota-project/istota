"""Discuss in private chat with no note (hidden email threads, section 0c).

The second way to link a turn to a shared room: a web send into the user's
private room naming an email thread room as ``about_room``. The route checks
three things before it records anything: the token is an email thread room,
the sender is a current member of it, and the room sent into is the sender's
private room for it (`private_replies.private_room_for`). Any failure is the
same 400 and no task. A reply-to row, when present, decides the link instead.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db
from istota.config import Config, EmailConfig, SiteConfig, UserConfig, WebConfig
from istota.rooms import private_replies

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
REFUSAL = {"error": "cannot link this message to that room"}


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
        users={
            "alice": UserConfig(display_name="Alice", email_addresses=["alice@example.com"]),
            "bob": UserConfig(display_name="Bob", email_addresses=["bob@example.com"]),
        },
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


@pytest.fixture
def config(db_path, tmp_path):
    return _config(db_path, tmp_path)


def _thread_room(conn, user="alice", root="<root@example.com>", name="Book club"):
    token = db.register_room(conn, None, user, origin="email", name=name).token
    db.add_room_binding(conn, token, "email", root)
    return token


def _task_count(conn):
    return conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]


class TestTheRule:
    """`private_replies.about_room_link`, the three checks as one function."""

    def test_a_thread_the_user_is_on_from_their_private_room_links(self, config):
        with db.get_db(config.db_path) as conn:
            general = db.create_web_chat_room(conn, "alice", "general").token
            thread = _thread_room(conn)
            assert private_replies.about_room_link(
                conn, config, user_id="alice", room_token=general, about_token=thread,
            ) == thread

    def test_a_shared_room_that_is_not_a_thread_does_not(self, config):
        with db.get_db(config.db_path) as conn:
            general = db.create_web_chat_room(conn, "alice", "general").token
            shared = db.create_web_chat_room(conn, "alice", "Family").token
            db.add_web_room_member(conn, shared, "bob")
            assert private_replies.about_room_link(
                conn, config, user_id="alice", room_token=general, about_token=shared,
            ) is None

    def test_a_thread_the_user_is_not_on_does_not(self, config):
        with db.get_db(config.db_path) as conn:
            general = db.create_web_chat_room(conn, "alice", "general").token
            theirs = _thread_room(conn, user="bob")
            assert private_replies.about_room_link(
                conn, config, user_id="alice", room_token=general, about_token=theirs,
            ) is None

    def test_a_room_that_is_not_their_private_room_for_it_does_not(self, config):
        with db.get_db(config.db_path) as conn:
            db.create_web_chat_room(conn, "alice", "general")
            other = db.create_web_chat_room(conn, "alice", "other").token
            thread = _thread_room(conn)
            assert private_replies.about_room_link(
                conn, config, user_id="alice", room_token=other, about_token=thread,
            ) is None

    def test_an_archived_thread_does_not(self, config):
        """`linked_room` would drop it at run time, so it is not accepted here."""
        with db.get_db(config.db_path) as conn:
            general = db.create_web_chat_room(conn, "alice", "general").token
            thread = _thread_room(conn)
            db.set_room_archived(conn, thread, True)
            assert private_replies.about_room_link(
                conn, config, user_id="alice", room_token=general, about_token=thread,
            ) is None

    def test_the_thread_itself_and_nonsense_do_not(self, config):
        with db.get_db(config.db_path) as conn:
            db.create_web_chat_room(conn, "alice", "general")
            thread = _thread_room(conn)
            for room, about in ((thread, thread), (thread, "rm_nothing"), (thread, "")):
                assert private_replies.about_room_link(
                    conn, config, user_id="alice", room_token=room, about_token=about,
                ) is None


@pytest.fixture
async def client(config):
    import istota.webui.app as mod
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


async def _send(client, cookies, room_id, payload):
    return await client.post(
        f"/istota/api/chat/rooms/{room_id}/messages",
        json=payload, cookies=cookies, headers=ORIGIN,
    )


@web_only
class TestTheSend:
    async def test_a_send_about_a_thread_links_the_task_and_its_transcript(
        self, client, config,
    ):
        with db.get_db(config.db_path) as conn:
            general = db.create_web_chat_room(conn, "alice", "general")
            thread = _thread_room(conn)
            db.add_message(conn, thread, role="user", origin_surface="email",
                           body="Saturday at the cafe?", author_label="Ana")
        cookies = await _login(client)
        resp = await _send(client, cookies, general.id,
                           {"text": "tell them I'm in", "about_room": thread})
        assert resp.status_code == 200, resp.text
        task_id = resp.json()["task_id"]
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_id)
            assert task.about_room_token == thread
            parent, block = private_replies.linked_context(conn, config, task)
        assert parent == thread
        assert "Saturday at the cafe?" in block

    @pytest.mark.parametrize("case", ["not_a_thread", "not_a_member", "not_private"])
    async def test_each_failed_check_is_the_same_400_and_no_task(
        self, client, config, case,
    ):
        with db.get_db(config.db_path) as conn:
            general = db.create_web_chat_room(conn, "alice", "general")
            other = db.create_web_chat_room(conn, "alice", "other")
            if case == "not_a_thread":
                about = db.create_web_chat_room(conn, "alice", "Family").token
                db.add_web_room_member(conn, about, "bob")
                room = general
            elif case == "not_a_member":
                about = _thread_room(conn, user="bob")
                room = general
            else:
                about = _thread_room(conn)
                room = other
            before = _task_count(conn)
        cookies = await _login(client)
        resp = await _send(client, cookies, room.id, {"text": "hi", "about_room": about})
        assert resp.status_code == 400
        assert resp.json() == REFUSAL
        with db.get_db(config.db_path) as conn:
            assert _task_count(conn) == before
            assert conn.execute(
                "SELECT COUNT(*) FROM messages WHERE room_token = ? AND role = 'user'",
                (room.token,),
            ).fetchone()[0] == 0

    async def test_a_refused_link_leaves_a_parked_question_standing(self, client, config):
        with db.get_db(config.db_path) as conn:
            general = db.create_web_chat_room(conn, "alice", "general")
            shared = db.create_web_chat_room(conn, "alice", "Family").token
            db.add_web_room_member(conn, shared, "bob")
            parked = db.create_task(conn, user_id="alice", source_type="web",
                                    prompt="book it", conversation_token=general.token)
            db.set_task_confirmation(conn, parked, "Book the table?")
            before = _task_count(conn)
        cookies = await _login(client)
        resp = await _send(client, cookies, general.id,
                           {"text": "something else", "about_room": shared})
        assert resp.status_code == 400
        assert resp.json() == REFUSAL
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, parked).status == "pending_confirmation"
            assert _task_count(conn) == before

    @pytest.mark.parametrize("value", [7, ["x"], {"token": "x"}, "x" * 300])
    async def test_a_malformed_about_room_is_refused(self, client, config, value):
        with db.get_db(config.db_path) as conn:
            general = db.create_web_chat_room(conn, "alice", "general")
            before = _task_count(conn)
        cookies = await _login(client)
        resp = await _send(client, cookies, general.id, {"text": "hi", "about_room": value})
        assert resp.status_code == 400
        assert resp.json() == REFUSAL
        with db.get_db(config.db_path) as conn:
            assert _task_count(conn) == before

    async def test_a_reply_to_row_decides_the_link(self, client, config):
        with db.get_db(config.db_path) as conn:
            general = db.create_web_chat_room(conn, "alice", "general")
            thread = _thread_room(conn)
            other_thread = _thread_room(conn, root="<other@example.com>", name="Dinner")
            tagged = db.add_message(
                conn, general.token, role="system", body="Ana wrote on Dinner",
                origin_surface="web", about_room_token=other_thread,
                delivery_reference="private-pass_on:99:pass-on",
            )
            plain = db.add_message(conn, general.token, role="assistant", body="hi",
                                   origin_surface="web")
        cookies = await _login(client)
        linked = await _send(client, cookies, general.id, {
            "text": "and this", "about_room": thread, "reply_to_msg_id": tagged,
        })
        unlinked = await _send(client, cookies, general.id, {
            "text": "and that", "about_room": thread, "reply_to_msg_id": plain,
        })
        assert linked.status_code == 200 and unlinked.status_code == 200
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, linked.json()["task_id"]).about_room_token == other_thread
            assert db.get_task(conn, unlinked.json()["task_id"]).about_room_token is None

    async def test_a_send_with_no_about_room_is_unlinked(self, client, config):
        with db.get_db(config.db_path) as conn:
            general = db.create_web_chat_room(conn, "alice", "general")
            _thread_room(conn)
        cookies = await _login(client)
        resp = await _send(client, cookies, general.id, {"text": "hello"})
        assert resp.status_code == 200, resp.text
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, resp.json()["task_id"]).about_room_token is None
