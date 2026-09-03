"""Per-message delete on the web chat surface (ISSUE-210).

- DELETE /istota/api/chat/messages/{id}   — hard delete, 404/409 semantics
- GET    /istota/api/chat/events          — deletion tail on the polling fallback
- Talk propagation                        — best-effort, never fails the delete
"""

import asyncio
import contextlib
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
        # Talk propagation is gated on a configured Nextcloud — a standalone
        # install has no Talk to mirror a delete into.
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


@pytest.fixture(autouse=True)
def _reset_bg_tasks():
    import istota.web_app as mod
    mod._bg_tasks.clear()
    yield
    mod._bg_tasks.clear()


async def _drain_bg():
    """Talk propagation is fire-and-forget, so it is still pending when the
    response lands. Every assertion about it has to wait for it."""
    import istota.web_app as mod
    while mod._bg_tasks:
        await asyncio.gather(*list(mod._bg_tasks))


def _db_path():
    import istota.web_app as mod
    return mod._config.db_path


async def _default_room(client, cookies):
    resp = await client.get("/istota/api/chat/rooms", cookies=cookies)
    return resp.json()["rooms"][0]


def _add_msg(token, *, role="assistant", body="hello", origin="web",
             task_id=None, external_ids=None):
    with db.get_db(_db_path()) as conn:
        return db.add_message(
            conn, token, role=role, body=body, origin_surface=origin,
            task_id=task_id, external_ids=external_ids,
        )


class TestDeleteEndpoint:
    async def test_deletes_the_message(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        mid = _add_msg(room["token"])

        resp = await chat_client.delete(
            f"/istota/api/chat/messages/{mid}", cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200
        assert resp.json() == {"ok": True, "message_id": mid}
        with db.get_db(_db_path()) as conn:
            assert db.get_message_room(conn, mid) is None

    async def test_requires_auth(self, chat_client):
        resp = await chat_client.delete(
            "/istota/api/chat/messages/1", headers=ORIGIN,
        )
        assert resp.status_code == 401

    async def test_rejects_a_foreign_origin(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        mid = _add_msg(room["token"])
        resp = await chat_client.delete(
            f"/istota/api/chat/messages/{mid}", cookies=cookies,
            headers={"origin": "https://evil.example"},
        )
        assert resp.status_code == 403
        with db.get_db(_db_path()) as conn:
            assert db.get_message_room(conn, mid) is not None

    async def test_unknown_message_404(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.delete(
            "/istota/api/chat/messages/999999", cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 404

    async def test_foreign_room_is_indistinguishable_from_missing(self, chat_client):
        # A 403 here would confirm the id exists; the star endpoint takes the
        # same line, and both must, or one becomes an id oracle for the other.
        alice = await _login(chat_client, "alice")
        alice_room = await _default_room(chat_client, alice)
        mid = _add_msg(alice_room["token"])

        bob = await _login(chat_client, "bob")
        resp = await chat_client.delete(
            f"/istota/api/chat/messages/{mid}", cookies=bob, headers=ORIGIN,
        )
        assert resp.status_code == 404
        assert resp.json()["error"] == "message not found"
        with db.get_db(_db_path()) as conn:
            assert db.get_message_room(conn, mid) is not None

    async def test_in_flight_turn_is_busy(self, chat_client):
        # The scheduler writes the assistant row at completion, so deleting a
        # running turn's row would have the delete silently undone.
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        with db.get_db(_db_path()) as conn:
            task_id = db.create_task(
                conn, "q", "alice", source_type="web",
                conversation_token=room["token"],
            )
            db.update_task_status(conn, task_id, "running")
        mid = _add_msg(room["token"], role="user", body="q", task_id=task_id)

        resp = await chat_client.delete(
            f"/istota/api/chat/messages/{mid}", cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 409
        with db.get_db(_db_path()) as conn:
            assert db.get_message_room(conn, mid) is not None

    async def test_settled_turn_is_deletable(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        with db.get_db(_db_path()) as conn:
            task_id = db.create_task(
                conn, "q", "alice", source_type="web",
                conversation_token=room["token"],
            )
            db.update_task_status(conn, task_id, "completed", result="a")
        mid = _add_msg(room["token"], task_id=task_id)

        resp = await chat_client.delete(
            f"/istota/api/chat/messages/{mid}", cookies=cookies, headers=ORIGIN,
        )
        assert resp.status_code == 200

    async def test_deleted_message_leaves_the_transcript(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        gone = _add_msg(room["token"], body="regrettable")
        kept = _add_msg(room["token"], body="fine")

        await chat_client.delete(
            f"/istota/api/chat/messages/{gone}", cookies=cookies, headers=ORIGIN,
        )
        resp = await chat_client.get(
            f"/istota/api/chat/rooms/{room['id']}/messages", cookies=cookies,
        )
        bodies = [m.get("text") for m in resp.json()["messages"]]
        assert "regrettable" not in bodies
        assert "fine" in bodies
        assert kept  # the other row is untouched


class TestDeletionTailOnEvents:
    async def test_events_carries_the_deletion(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        mid = _add_msg(room["token"])
        await chat_client.delete(
            f"/istota/api/chat/messages/{mid}", cookies=cookies, headers=ORIGIN,
        )

        resp = await chat_client.get("/istota/api/chat/events", cookies=cookies)
        body = resp.json()
        assert [d["msg_id"] for d in body["deletions"]] == [mid]
        assert body["deletion_cursor"] > 0

    async def test_cursor_suppresses_a_replay(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        mid = _add_msg(room["token"])
        await chat_client.delete(
            f"/istota/api/chat/messages/{mid}", cookies=cookies, headers=ORIGIN,
        )
        first = await chat_client.get("/istota/api/chat/events", cookies=cookies)
        cursor = first.json()["deletion_cursor"]

        again = await chat_client.get(
            f"/istota/api/chat/events?since_deletion_id={cursor}", cookies=cookies,
        )
        assert again.json()["deletions"] == []

    async def test_no_deletions_is_an_empty_tail(self, chat_client):
        cookies = await _login(chat_client, "alice")
        resp = await chat_client.get("/istota/api/chat/events", cookies=cookies)
        assert resp.json()["deletions"] == []
        assert resp.json()["deletion_cursor"] == 0


@contextlib.contextmanager
def _fake_talk_class(client):
    """Stand `client` in at `istota.talk.TalkClient`, the seam this path uses.

    `_delete_from_talk` builds both of its clients itself as of ISSUE-407 —
    patching `async_runtime.get_talk_client`, which is what these tests used to
    do, now stands in front of nothing. Three of the four were `assert_not_
    awaited` and so stayed green against a product reaching the real Nextcloud;
    the fourth is what caught it.
    """
    client.aclose = AsyncMock()
    with patch("istota.talk.TalkClient", return_value=client):
        yield client


class TestTalkPropagation:
    async def _bound_room(self, chat_client, cookies, talk_ref="TALKROOM"):
        room = await _default_room(chat_client, cookies)
        with db.get_db(_db_path()) as conn:
            db.add_room_binding(conn, room["token"], "talk", talk_ref)
        return room

    async def test_deletes_the_mirrored_talk_message(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await self._bound_room(chat_client, cookies)
        mid = _add_msg(room["token"], external_ids={"talk": "4242"})

        client = MagicMock()
        client.delete_message = AsyncMock(return_value={})
        with _fake_talk_class(client):
            resp = await chat_client.delete(
                f"/istota/api/chat/messages/{mid}", cookies=cookies, headers=ORIGIN,
            )
            await _drain_bg()
        assert resp.status_code == 200
        client.delete_message.assert_awaited_once_with("TALKROOM", 4242)

    async def test_web_only_room_never_calls_talk(self, chat_client):
        cookies = await _login(chat_client, "alice")
        room = await _default_room(chat_client, cookies)
        mid = _add_msg(room["token"])

        client = MagicMock()
        client.delete_message = AsyncMock(return_value={})
        with _fake_talk_class(client):
            await chat_client.delete(
                f"/istota/api/chat/messages/{mid}", cookies=cookies, headers=ORIGIN,
            )
            await _drain_bg()
        client.delete_message.assert_not_awaited()

    async def test_unmirrored_message_in_a_bound_room_never_calls_talk(
        self, chat_client,
    ):
        # No `talk` external id — the message never reached Talk, so there is
        # nothing there to delete and guessing an id would delete someone
        # else's message.
        cookies = await _login(chat_client, "alice")
        room = await self._bound_room(chat_client, cookies)
        mid = _add_msg(room["token"])

        client = MagicMock()
        client.delete_message = AsyncMock(return_value={})
        with _fake_talk_class(client):
            await chat_client.delete(
                f"/istota/api/chat/messages/{mid}", cookies=cookies, headers=ORIGIN,
            )
            await _drain_bg()
        client.delete_message.assert_not_awaited()

    async def test_talk_failure_does_not_fail_the_delete(self, chat_client):
        # The web-side delete has already committed; reporting it as failed
        # would leave the user re-clicking a button that already worked.
        cookies = await _login(chat_client, "alice")
        room = await self._bound_room(chat_client, cookies)
        mid = _add_msg(room["token"], external_ids={"talk": "7"})

        client = MagicMock()
        client.delete_message = AsyncMock(side_effect=RuntimeError("talk down"))
        with _fake_talk_class(client):
            resp = await chat_client.delete(
                f"/istota/api/chat/messages/{mid}", cookies=cookies, headers=ORIGIN,
            )
            await _drain_bg()
        assert resp.status_code == 200
        with db.get_db(_db_path()) as conn:
            assert db.get_message_room(conn, mid) is None
