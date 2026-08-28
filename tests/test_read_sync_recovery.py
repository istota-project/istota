"""Recovery for the web→Talk read push: a 401 retry and a status in the log.

`_post_as_user` handles a 401 by force-refreshing and retrying once;
`_push_read_to_talk`, ten lines away, did not — and could not, because
`mark_conversation_read` swallowed every exception and returned a bool the caller
never read. So a stale-but-present access token produced exactly the reported
shape: mirroring your messages into Talk kept working while read pushes failed
silently on every attempt (ISSUE-333).

There is deliberately **no** web→Talk reconciliation leg here, and no test for
one. The entry asked for it as the durable fix, and the guard it implies —
Talk counts the room unread while the web cursor covers
`room_max_talk_synced_message_id` — is unsound: `cap` never sees the Talk
messages `transport/talk/inbound.py` drops before `record_inbound`, and
`db.initialize_room_read_state` *seeds* a new room's cursor to the newest message
so the cursor is not evidence the user read anything. Either alone makes the
guard true for a room nobody has read, and the push marks the whole conversation
read. See the resolution note on ISSUE-333.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock

import httpx
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

KEY = "r" * 64
ORIGIN = {"origin": "https://example.com"}


def _make_config(tmp_path, sync_interval=60):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    config = Config(
        db_path=db_path,
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice")},
        web=WebConfig(
            enabled=True, port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web", oauth2_client_secret="s",
            session_secret_key="test-session-key",
            token_storage="encrypted",
        ),
        bot_name="Istota",
    )
    config.web.chat.talk_read_sync_interval = sync_interval
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


def _mock_talk_client(monkeypatch, conversations=None, mark=None):
    constructed = []
    instance = MagicMock()
    instance.mark_conversation_read = mark or AsyncMock(return_value=True)
    instance.list_conversations = AsyncMock(return_value=conversations or [])
    instance.aclose = AsyncMock()

    def factory(config, bearer_token=None, timeout=None):
        constructed.append({"bearer_token": bearer_token, "timeout": timeout})
        return instance

    import istota.talk
    monkeypatch.setattr(istota.talk, "TalkClient", factory)
    return constructed, instance


@pytest.fixture(autouse=True)
def _reset_module_state():
    import istota.web_app as mod

    web_tokens._refresh_locks.clear()
    mod._talk_read_pull_state.clear()
    mod._token_degraded_logged.clear()
    mod._bg_tasks.clear()
    yield
    web_tokens._refresh_locks.clear()
    mod._talk_read_pull_state.clear()
    mod._token_degraded_logged.clear()
    mod._bg_tasks.clear()


@pytest.fixture
def keyed(monkeypatch):
    monkeypatch.setenv(web_tokens._KEY_ENV_VAR, KEY)


async def _drain_bg():
    import istota.web_app as mod

    while mod._bg_tasks:
        await asyncio.gather(*list(mod._bg_tasks))


async def _default_room(client, config, cookies, bind_talk=True):
    rooms = (await client.get("/istota/api/chat/rooms", cookies=cookies)).json()["rooms"]
    room = rooms[0]
    if bind_talk:
        with db.get_db(config.db_path) as conn:
            db.add_room_binding(conn, room["token"], "talk", "talkref9")
    return room["id"], room["token"]


def _add_message(config, room_token, role="assistant", external_ids=None):
    with db.get_db(config.db_path) as conn:
        return db.add_message(
            conn, room_token, role=role, body="x",
            origin_surface="web", task_id=None, external_ids=external_ids,
        )


def _http_error(status):
    request = httpx.Request("POST", "https://cloud.example.com/read")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError("boom", request=request, response=response)


@_needs_web_deps
class TestThe401Retry:
    """The reported asymmetry, in one pair of tests. `_post_as_user` recovers from
    a stale-but-present token and `_push_read_to_talk` did not."""

    async def _run(self, tmp_path, monkeypatch, mark):
        config = _make_config(tmp_path, sync_interval=0)  # pull off; push only
        web_tokens.store_tokens(config.db_path, "alice", "stale-at", "rt", 3600)
        constructed, instance = _mock_talk_client(monkeypatch, mark=mark)
        app = _patch_app(config)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="https://example.com",
        ) as client:
            cookies = await _login(client)
            room_id, room_token = await _default_room(client, config, cookies)
            _add_message(config, room_token)
            await client.post(
                f"/istota/api/chat/rooms/{room_id}/read",
                cookies=cookies, headers=ORIGIN,
            )
            await _drain_bg()
        return config, constructed, instance

    async def test_a_401_forces_a_refresh_and_retries_once(
        self, tmp_path, keyed, monkeypatch,
    ):
        mark = AsyncMock(side_effect=[_http_error(401), True])
        refreshed = []

        real = web_tokens.get_access_token

        def _spy(db_path, config, user_id, *, force_refresh=False):
            if force_refresh:
                refreshed.append(user_id)
                return "fresh-at"
            return real(db_path, config, user_id)

        monkeypatch.setattr(web_tokens, "get_access_token", _spy)

        _config, constructed, instance = await self._run(tmp_path, monkeypatch, mark)

        assert refreshed == ["alice"], "a 401 did not force a refresh"
        assert instance.mark_conversation_read.await_count == 2
        # The retry used the freshly minted token, not the stale one.
        assert constructed[-1]["bearer_token"] == "fresh-at"

    async def test_it_retries_only_once(self, tmp_path, keyed, monkeypatch):
        """A token the provider keeps refusing must not become a retry loop."""
        mark = AsyncMock(side_effect=[_http_error(401), _http_error(401)])
        monkeypatch.setattr(
            web_tokens, "get_access_token",
            lambda *a, **k: "any-at",
        )

        _config, _constructed, instance = await self._run(tmp_path, monkeypatch, mark)

        assert instance.mark_conversation_read.await_count == 2

    async def test_a_403_is_not_retried(self, tmp_path, keyed, monkeypatch):
        """Only 401 means "this token is stale". A 403 is an answer about the
        room, and refreshing the token cannot change it."""
        mark = AsyncMock(side_effect=[_http_error(403), True])

        _config, _constructed, instance = await self._run(tmp_path, monkeypatch, mark)

        assert instance.mark_conversation_read.await_count == 1

    async def test_the_failure_log_carries_the_status(
        self, tmp_path, keyed, monkeypatch, caplog,
    ):
        """The entry's diagnosis step 2: the warning carried the exception but not
        the HTTP status, so the log could not tell a dead token from a moved
        endpoint."""
        mark = AsyncMock(side_effect=[_http_error(404), _http_error(404)])

        with caplog.at_level("WARNING", logger="istota.web_app"):
            await self._run(tmp_path, monkeypatch, mark)

        assert any("404" in r.getMessage() for r in caplog.records), (
            "no log line named the HTTP status"
        )


@_needs_web_deps
class TestTheDegradedConsumersSaySo:
    """Item 4. The bot-attributed repost is a legitimate fallback, but it should
    not be indistinguishable from normal operation in the log."""

    async def test_the_mirror_names_the_missing_token(
        self, tmp_path, keyed, monkeypatch, caplog,
    ):
        import istota.web_app as mod

        config = _make_config(tmp_path, sync_interval=0)
        _patch_app(config)
        # Feature on, no stored pair — the state the incident sat in for weeks.
        with caplog.at_level("WARNING", logger="istota.web_app"):
            with db.get_db(config.db_path) as conn:
                db.register_room(conn, "roomtok", "alice", origin="web", name="Room")
                db.add_room_binding(conn, "roomtok", "talk", "talkref9")
                # The turn the mirror would have posted. Without it the lookup
                # bails before it ever asks for a token.
                db.add_message(
                    conn, "roomtok", role="user", body="hi",
                    origin_surface="web", task_id=1,
                )
            await mod._mirror_web_turn_as_user("alice", "roomtok", "hi", 1)

        assert any(
            "token" in r.getMessage().lower() for r in caplog.records
        ), "a silent bail left nothing in the log"

    async def test_the_push_names_the_missing_token(
        self, tmp_path, keyed, monkeypatch, caplog,
    ):
        import istota.web_app as mod

        config = _make_config(tmp_path, sync_interval=0)
        _patch_app(config)
        with caplog.at_level("WARNING", logger="istota.web_app"):
            with db.get_db(config.db_path) as conn:
                db.register_room(conn, "roomtok", "alice", origin="web", name="Room")
                db.add_room_binding(conn, "roomtok", "talk", "talkref9")
            await mod._push_read_to_talk("alice", "roomtok")

        assert any(
            "token" in r.getMessage().lower() for r in caplog.records
        )

    async def test_it_warns_once_per_healthy_period_not_once_per_send(
        self, tmp_path, keyed, monkeypatch, caplog,
    ):
        """`get_access_token` returns None identically for a credential that just
        died and for a user who has never connected, and the row is deleted on
        loss — so nothing downstream can tell the two apart. An unconditional
        warning is therefore a line per send, forever, for every user on a
        deployment that enabled the feature after they last logged in."""
        import istota.web_app as mod

        config = _make_config(tmp_path, sync_interval=0)
        _patch_app(config)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "roomtok", "alice", origin="web", name="Room")
            db.add_room_binding(conn, "roomtok", "talk", "talkref9")

        with caplog.at_level("WARNING", logger="istota.web_app"):
            for _ in range(5):
                await mod._push_read_to_talk("alice", "roomtok")

        assert len(caplog.records) == 1

    async def test_a_healthy_token_rearms_the_warning(
        self, tmp_path, keyed, monkeypatch, caplog,
    ):
        """"Once" must mean once per healthy period, not once per process — a
        credential that comes back and dies again is a second event."""
        import istota.web_app as mod

        config = _make_config(tmp_path, sync_interval=0)
        _mock_talk_client(monkeypatch)
        _patch_app(config)
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "roomtok", "alice", origin="web", name="Room")
            db.add_room_binding(conn, "roomtok", "talk", "talkref9")

        with caplog.at_level("WARNING", logger="istota.web_app"):
            await mod._push_read_to_talk("alice", "roomtok")          # warns
            await mod._push_read_to_talk("alice", "roomtok")          # silent
            web_tokens.store_tokens(config.db_path, "alice", "at", "rt", 3600)
            await mod._push_read_to_talk("alice", "roomtok")          # healthy
            web_tokens.delete_tokens(config.db_path, "alice")
            await mod._push_read_to_talk("alice", "roomtok")          # warns again

        assert len(caplog.records) == 2

    async def test_a_web_only_room_stays_quiet(
        self, tmp_path, keyed, monkeypatch, caplog,
    ):
        """The discriminating negative: a room with no Talk binding is not
        degraded, it is web-only, and warning about it would fire on every read
        in every such room."""
        import istota.web_app as mod

        config = _make_config(tmp_path, sync_interval=0)
        _patch_app(config)
        with caplog.at_level("WARNING", logger="istota.web_app"):
            with db.get_db(config.db_path) as conn:
                db.register_room(conn, "roomtok", "alice", origin="web", name="Room")
            await mod._push_read_to_talk("alice", "roomtok")

        assert caplog.records == []
