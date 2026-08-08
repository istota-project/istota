"""Reply-to-a-message on the web chat surface.

The send POST carries `reply_to_msg_id` — a canonical `messages.id`, the same id
star and delete address. The server validates it against the room being posted
into, derives the parent snapshot itself, and stores the id on both the task and
the user's `messages` row.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from istota import db
from istota.config import Config, NextcloudConfig, SiteConfig, UserConfig, WebConfig

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
        nextcloud=NextcloudConfig(
            url="https://cloud.example.com", username="bot", app_password="p",
        ),
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


def _add_msg(token, *, role="assistant", body="hello", origin="web", task_id=None):
    with db.get_db(_db_path()) as conn:
        return db.add_message(
            conn, token, role=role, body=body, origin_surface=origin,
            task_id=task_id,
        )


def _task(task_id):
    with db.get_db(_db_path()) as conn:
        return db.get_task(conn, task_id)


def _message_row(msg_id):
    with db.get_db(_db_path()) as conn:
        return conn.execute(
            "SELECT * FROM messages WHERE id = ?", (msg_id,)
        ).fetchone()


async def _send(client, cookies, room_id, body):
    return await client.post(
        f"/istota/api/chat/rooms/{room_id}/messages",
        json=body, cookies=cookies, headers=ORIGIN,
    )


class TestSendWithReply:
    async def test_stores_canonical_id_and_snapshot(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        parent = _add_msg(room["token"], role="assistant", body="x" * 1500)

        resp = await _send(chat_client, cookies, room["id"], {
            "text": "no, the second one", "reply_to_msg_id": parent,
        })
        assert resp.status_code == 200
        task_id = resp.json()["task_id"]

        task = _task(task_id)
        assert task.reply_to_message_id == parent
        # Snapshot is the server's own read of the parent body, capped at 1000.
        assert task.reply_to_content == "x" * 1000
        # Talk's own column is untouched — a different namespace.
        assert task.reply_to_talk_id is None

        with db.get_db(_db_path()) as conn:
            row = conn.execute(
                "SELECT id, reply_to_message_id, body FROM messages "
                "WHERE room_token = ? AND task_id = ? AND role = 'user'",
                (room["token"], task_id),
            ).fetchone()
        assert row["reply_to_message_id"] == parent
        # The quote is a prompt-construction concern; the stored body is the
        # user's own text only.
        assert row["body"] == "no, the second one"
        assert task.prompt == "no, the second one"

    async def test_cross_room_parent_is_refused(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        other = await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "other"},
            cookies=cookies, headers=ORIGIN,
        )
        other_token = other.json()["token"]
        foreign = _add_msg(other_token, body="somewhere else")

        before = _task_count()
        resp = await _send(chat_client, cookies, room["id"], {
            "text": "citing across rooms", "reply_to_msg_id": foreign,
        })
        assert resp.status_code == 404
        assert _task_count() == before

    async def test_unknown_parent_is_refused_indistinguishably(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        resp = await _send(chat_client, cookies, room["id"], {
            "text": "citing a ghost", "reply_to_msg_id": 987654,
        })
        assert resp.status_code == 404
        assert resp.json()["error"] == (
            "the message you replied to is no longer available"
        )

    async def test_client_supplied_quote_is_ignored(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        parent = _add_msg(room["token"], body="what the bot really said")

        resp = await _send(chat_client, cookies, room["id"], {
            "text": "sure", "reply_to_msg_id": parent,
            "reply_to_content": "what the client claims it said",
        })
        assert resp.status_code == 200
        task = _task(resp.json()["task_id"])
        assert task.reply_to_content == "what the bot really said"

    async def test_reply_to_system_row_stores_snapshot(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        note = _add_msg(room["token"], role="system", body="disk almost full")

        resp = await _send(chat_client, cookies, room["id"], {
            "text": "what should I delete?", "reply_to_msg_id": note,
        })
        assert resp.status_code == 200
        task = _task(resp.json()["task_id"])
        assert task.reply_to_message_id == note
        assert task.reply_to_content == "disk almost full"
        # A system row has no task, so there is no parent turn to force-include.
        with db.get_db(_db_path()) as conn:
            assert db.get_reply_parent_task_by_message_id(
                conn, room["token"], note,
            ) is None

    async def test_retry_with_same_client_msg_id_makes_no_second_citation(
        self, chat_client,
    ):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        parent = _add_msg(room["token"], body="the original answer")

        first = await _send(chat_client, cookies, room["id"], {
            "text": "yes, do that", "reply_to_msg_id": parent,
            "client_msg_id": "abc-123",
        })
        second = await _send(chat_client, cookies, room["id"], {
            "text": "yes, do that", "reply_to_msg_id": parent,
            "client_msg_id": "abc-123",
        })
        assert first.json()["task_id"] == second.json()["task_id"]
        with db.get_db(_db_path()) as conn:
            rows = conn.execute(
                "SELECT id FROM messages WHERE room_token = ? AND role = 'user' "
                "AND reply_to_message_id = ?",
                (room["token"], parent),
            ).fetchall()
        assert len(rows) == 1

    async def test_plain_send_stores_no_citation(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        resp = await _send(chat_client, cookies, room["id"], {"text": "hello"})
        task = _task(resp.json()["task_id"])
        assert task.reply_to_message_id is None
        assert task.reply_to_content is None


class TestCanonicalParentLookup:
    async def test_resolves_completed_turn_by_message_id(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        with db.get_db(_db_path()) as conn:
            parent_task = db.create_task(
                conn, prompt="q", user_id="alice", source_type="web",
                conversation_token=room["token"],
            )
            db.update_task_status(conn, parent_task, "completed", result="a")
            msg_id = db.add_message(
                conn, room["token"], role="assistant", body="a",
                origin_surface="web", task_id=parent_task,
            )
            found = db.get_reply_parent_task_by_message_id(
                conn, room["token"], msg_id,
            )
        assert found is not None
        assert found.id == parent_task

    async def test_does_not_resolve_a_parent_in_another_room(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        with db.get_db(_db_path()) as conn:
            parent_task = db.create_task(
                conn, prompt="q", user_id="alice", source_type="web",
                conversation_token=room["token"],
            )
            db.update_task_status(conn, parent_task, "completed", result="a")
            msg_id = db.add_message(
                conn, room["token"], role="assistant", body="a",
                origin_surface="web", task_id=parent_task,
            )
            assert db.get_reply_parent_task_by_message_id(
                conn, "some-other-room", msg_id,
            ) is None

    async def test_unfinished_parent_does_not_resolve(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        with db.get_db(_db_path()) as conn:
            parent_task = db.create_task(
                conn, prompt="q", user_id="alice", source_type="web",
                conversation_token=room["token"],
            )
            msg_id = db.add_message(
                conn, room["token"], role="user", body="q",
                origin_surface="web", task_id=parent_task,
            )
            assert db.get_reply_parent_task_by_message_id(
                conn, room["token"], msg_id,
            ) is None


def _task_count():
    with db.get_db(_db_path()) as conn:
        return conn.execute("SELECT COUNT(*) AS c FROM tasks").fetchone()["c"]


class TestTalkInbound:
    """A Talk-origin reply sets the Talk-native column; the canonical one has
    to be filled too or the web transcript renders it as an ordinary message."""

    def _config(self):
        import istota.web_app as mod
        return mod._config

    def _ingest_talk_reply(self, token, *, reply_to_talk_id, parent_talk_id=None):
        from istota.transport import record_inbound

        with db.get_db(_db_path()) as conn:
            db.add_room_binding(conn, token, "talk", token)
            parent = db.add_message(
                conn, token, role="assistant", body="the earlier answer",
                origin_surface="talk",
                external_ids=(
                    {"talk": str(parent_talk_id)} if parent_talk_id else None
                ),
            )
            _room, task_id = record_inbound(
                conn, self._config(), surface="talk", surface_ref=token,
                user_id="alice", text="yes, that one", source_type="talk",
                platform_message_id=555,
                reply_to_message_id=reply_to_talk_id,
                reply_to_content="the earlier answer",
                external_id="555",
            )
            task = db.get_task(conn, task_id)
            row = conn.execute(
                "SELECT reply_to_message_id FROM messages "
                "WHERE room_token = ? AND task_id = ? AND role = 'user'",
                (token, task_id),
            ).fetchone()
        return parent, task, row

    async def test_resolves_the_talk_parent_to_its_canonical_id(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        parent, task, row = self._ingest_talk_reply(
            room["token"], reply_to_talk_id=9001, parent_talk_id=9001,
        )
        assert task.reply_to_talk_id == 9001  # the native column is untouched
        assert task.reply_to_message_id == parent
        assert row["reply_to_message_id"] == parent

    async def test_an_unmirrored_talk_parent_leaves_the_canonical_column_null(
        self, chat_client,
    ):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        _parent, task, row = self._ingest_talk_reply(
            room["token"], reply_to_talk_id=9001, parent_talk_id=None,
        )
        assert task.reply_to_talk_id == 9001
        assert task.reply_to_message_id is None
        assert row["reply_to_message_id"] is None

    async def test_a_talk_parent_in_another_room_does_not_resolve(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        other = await chat_client.post(
            "/istota/api/chat/rooms", json={"name": "other"},
            cookies=cookies, headers=ORIGIN,
        )
        other_token = other.json()["token"]
        with db.get_db(_db_path()) as conn:
            db.add_message(
                conn, other_token, role="assistant", body="elsewhere",
                origin_surface="talk", external_ids={"talk": "9001"},
            )
        _parent, task, _row = self._ingest_talk_reply(
            room["token"], reply_to_talk_id=9001, parent_talk_id=None,
        )
        # Talk ids are per-conversation, so the lookup must be room-scoped.
        assert task.reply_to_message_id is None


class TestTalkMirror:
    """A web reply into a Talk-bound room should land in Talk as a real Talk
    reply — but only when the parent was itself mirrored there."""

    def _bind_talk(self, token):
        with db.get_db(_db_path()) as conn:
            db.add_room_binding(conn, token, "talk", "talkroom1")

    async def test_mirrors_a_reply_with_the_parents_talk_id(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        self._bind_talk(room["token"])
        parent = _add_msg(room["token"], body="the earlier answer")
        with db.get_db(_db_path()) as conn:
            db.set_message_external_id(conn, parent, "talk", "9001")

        sent = await _post_reply_as_user(chat_client, cookies, room, parent)
        assert sent["reply_to"] == 9001

    async def test_degrades_to_a_plain_post_when_the_parent_never_reached_talk(
        self, chat_client,
    ):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        self._bind_talk(room["token"])
        parent = _add_msg(room["token"], body="web-only answer")

        sent = await _post_reply_as_user(chat_client, cookies, room, parent)
        assert sent["reply_to"] is None

    async def test_a_plain_send_carries_no_reply(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        self._bind_talk(room["token"])

        sent = await _post_reply_as_user(chat_client, cookies, room, None)
        assert sent["reply_to"] is None


async def _post_reply_as_user(client, cookies, room, parent_msg_id):
    """Send (optionally citing `parent_msg_id`) with the post-as-user mirror
    armed, and return the arguments it handed to Talk."""
    import istota.web_app as mod

    captured: dict = {}

    class _FakeTalkClient:
        def __init__(self, *a, **kw):
            pass

        async def send_message(self, token, text, reply_to=None, reference_id=None):
            captured["reply_to"] = reply_to
            return {"ocs": {"data": {"id": 4242}}}

        async def aclose(self):
            pass

    body = {"text": "about that"}
    if parent_msg_id is not None:
        body["reply_to_msg_id"] = parent_msg_id
    with patch.object(mod, "_config", mod._config), \
            patch("istota.web_tokens.feature_enabled", return_value=True), \
            patch("istota.web_tokens.get_access_token", return_value="tok"), \
            patch("istota.talk.TalkClient", _FakeTalkClient):
        resp = await _send(client, cookies, room["id"], body)
    assert resp.status_code == 200
    return captured


class TestReadPaths:
    """`reply_to` must be emitted by every producer or a reply renders as a
    reply in one view and as an ordinary message in another."""

    async def _reply_send(self, client, cookies, room, parent_body="the answer"):
        parent = _add_msg(room["token"], role="assistant", body=parent_body)
        resp = await _send(client, cookies, room["id"], {
            "text": "about that", "reply_to_msg_id": parent,
        })
        return parent, resp.json()["task_id"]

    async def test_room_history_carries_the_citation(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        parent, task_id = await self._reply_send(chat_client, cookies, room)

        resp = await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )
        row = next(
            m for m in resp.json()["messages"]
            if m["role"] == "user" and m.get("task_id") == task_id
        )
        assert row["reply_to"] == {
            "msg_id": parent, "role": "assistant",
            "excerpt": "the answer", "deleted": False,
        }

    async def test_aggregate_view_carries_the_citation(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        parent, task_id = await self._reply_send(chat_client, cookies, room)

        resp = await chat_client.get(
            "/istota/api/chat/messages?view=all", cookies=cookies,
        )
        row = next(
            m for m in resp.json()["messages"]
            if m["role"] == "user" and m.get("task_id") == task_id
        )
        assert row["reply_to"]["msg_id"] == parent
        assert row["reply_to"]["excerpt"] == "the answer"

    async def test_excerpt_is_capped_for_display(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        parent, task_id = await self._reply_send(
            chat_client, cookies, room, parent_body="y" * 500,
        )

        resp = await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )
        row = next(
            m for m in resp.json()["messages"]
            if m["role"] == "user" and m.get("task_id") == task_id
        )
        assert row["reply_to"]["excerpt"] == "y" * 200

    async def test_deleted_parent_renders_as_deleted(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        parent, task_id = await self._reply_send(chat_client, cookies, room)
        deleted = await chat_client.delete(
            f"/istota/api/chat/messages/{parent}", cookies=cookies, headers=ORIGIN,
        )
        assert deleted.status_code == 200

        resp = await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )
        row = next(
            m for m in resp.json()["messages"]
            if m["role"] == "user" and m.get("task_id") == task_id
        )
        # The citation survives the parent — erasing it would rewrite the
        # conversation rather than record what happened to it.
        assert row["reply_to"] == {"msg_id": parent, "deleted": True}

    async def test_plain_message_has_no_reply_to_key(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        await _send(chat_client, cookies, room["id"], {"text": "hello"})

        resp = await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )
        rows = [m for m in resp.json()["messages"] if m["role"] == "user"]
        assert rows
        assert all("reply_to" not in m for m in rows)
