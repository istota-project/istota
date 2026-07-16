"""Tests for ingest-time post-as-user Talk mirroring (Stage 3 of the
user-scoped Nextcloud OAuth spec): the web send endpoint posts the prompt to
the bound Talk room as the user and stamps the external id."""

from unittest.mock import AsyncMock, MagicMock

import pytest

from istota import db, web_tokens
from istota.config import Config, SiteConfig, UserConfig, WebConfig

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps, reason="web dependencies not installed",
)

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

KEY = "m" * 64
ORIGIN = {"origin": "https://example.com"}


def _make_config(tmp_path, token_storage="encrypted"):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    config = Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(enabled=True, hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
            token_storage=token_storage,
        ),
        bot_name="Istota",
    )
    # These tests target the send-time post path; keep the Talk→web read pull
    # (which also constructs a bearer TalkClient on the rooms poll) out of the
    # constructed-clients ledger.
    config.web.chat.talk_read_sync_interval = 0
    return config


def _patch_app(config):
    import istota.web_app as mod
    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    return mod.app


async def _login(client, username="alice"):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username},
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


def _mock_talk_client(monkeypatch, posted_id=777, send_error=None):
    """Patch istota.talk.TalkClient with a recording factory."""
    constructed = []
    instance = MagicMock()
    if send_error is not None:
        instance.send_message = AsyncMock(side_effect=send_error)
    else:
        instance.send_message = AsyncMock(
            return_value={"ocs": {"data": {"id": posted_id}}},
        )
    instance.aclose = AsyncMock()

    def factory(config, bearer_token=None, timeout=None):
        constructed.append({"bearer_token": bearer_token, "timeout": timeout})
        return instance

    import istota.talk
    monkeypatch.setattr(istota.talk, "TalkClient", factory)
    return constructed, instance


@pytest.fixture(autouse=True)
def _reset_locks():
    import istota.web_app as mod
    web_tokens._refresh_locks.clear()
    mod._talk_read_pull_state.clear()
    yield
    web_tokens._refresh_locks.clear()
    mod._talk_read_pull_state.clear()


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(web_tokens._KEY_ENV_VAR, KEY)


async def _setup_room(client, config, cookies, bind_talk=True):
    """Surface the default room; optionally bind it to a Talk conversation.
    Returns (room_id, room_token)."""
    rooms = (await client.get("/istota/api/chat/rooms", cookies=cookies)).json()["rooms"]
    room = rooms[0]
    if bind_talk:
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, room["token"], "talk", "talkref9")
    return room["id"], room["token"]


def _user_turn(config, room_token):
    with db.get_db(config.db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM messages WHERE room_token = ? AND role = 'user'",
            (room_token,),
        ).fetchall()
    return rows


@_needs_web_deps
class TestPostAsUser:
    async def test_posts_and_stamps(self, tmp_path, keyed, monkeypatch):
        config = _make_config(tmp_path)
        web_tokens.store_tokens(config.db_path, "alice", "live-at", "rt", 3600)
        constructed, instance = _mock_talk_client(monkeypatch)
        app = _patch_app(config)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            room_id, room_token = await _setup_room(client, config, cookies)
            resp = await client.post(
                f"/istota/api/chat/rooms/{room_id}/messages",
                json={"text": "hello from web"}, cookies=cookies, headers=ORIGIN,
            )
        assert resp.status_code == 200

        # Bearer client constructed with the live access token + tight timeout.
        assert constructed == [{"bearer_token": "live-at", "timeout": 5}]
        # Posted to the bound Talk ref with the webmirror referenceId.
        args, kwargs = instance.send_message.call_args
        assert args[0] == "talkref9"
        assert args[1] == "hello from web"
        rows = _user_turn(config, room_token)
        assert len(rows) == 1
        assert kwargs["reference_id"] == f"istota:webmirror:{rows[0]['id']}"
        # External id stamped on the canonical user row.
        import json
        assert json.loads(rows[0]["external_ids"]) == {"talk": "777"}

    async def test_no_token_no_stamp_still_200(self, tmp_path, keyed, monkeypatch):
        config = _make_config(tmp_path)  # no stored token
        constructed, instance = _mock_talk_client(monkeypatch)
        app = _patch_app(config)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            room_id, room_token = await _setup_room(client, config, cookies)
            resp = await client.post(
                f"/istota/api/chat/rooms/{room_id}/messages",
                json={"text": "hi"}, cookies=cookies, headers=ORIGIN,
            )
        assert resp.status_code == 200
        assert constructed == []
        rows = _user_turn(config, room_token)
        assert rows[0]["external_ids"] is None

    async def test_post_error_no_stamp_still_200(self, tmp_path, keyed, monkeypatch):
        config = _make_config(tmp_path)
        web_tokens.store_tokens(config.db_path, "alice", "live-at", "rt", 3600)
        constructed, instance = _mock_talk_client(
            monkeypatch, send_error=RuntimeError("NC down"),
        )
        app = _patch_app(config)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            room_id, room_token = await _setup_room(client, config, cookies)
            resp = await client.post(
                f"/istota/api/chat/rooms/{room_id}/messages",
                json={"text": "hi"}, cookies=cookies, headers=ORIGIN,
            )
        assert resp.status_code == 200
        rows = _user_turn(config, room_token)
        assert rows[0]["external_ids"] is None  # scheduler fallback will repost

    async def test_unbound_room_no_talk_call(self, tmp_path, keyed, monkeypatch):
        config = _make_config(tmp_path)
        web_tokens.store_tokens(config.db_path, "alice", "live-at", "rt", 3600)
        constructed, instance = _mock_talk_client(monkeypatch)
        app = _patch_app(config)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            room_id, room_token = await _setup_room(
                client, config, cookies, bind_talk=False,
            )
            resp = await client.post(
                f"/istota/api/chat/rooms/{room_id}/messages",
                json={"text": "hi"}, cookies=cookies, headers=ORIGIN,
            )
        assert resp.status_code == 200
        assert constructed == []

    async def test_feature_off_no_talk_call(self, tmp_path, keyed, monkeypatch):
        config = _make_config(tmp_path, token_storage="ephemeral")
        constructed, instance = _mock_talk_client(monkeypatch)
        app = _patch_app(config)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            room_id, room_token = await _setup_room(client, config, cookies)
            resp = await client.post(
                f"/istota/api/chat/rooms/{room_id}/messages",
                json={"text": "hi"}, cookies=cookies, headers=ORIGIN,
            )
        assert resp.status_code == 200
        assert constructed == []
