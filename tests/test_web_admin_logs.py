"""Tests for the admin log + config web endpoints (ISSUE-203)."""

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps,
    reason="web dependencies not installed (install with: uv sync --extra web)",
)

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

from istota.config import Config, LoggingConfig, SiteConfig, UserConfig, WebConfig

pytestmark = _needs_web_deps


def _line(ts: str, level: str, logger: str, message: str) -> str:
    return f"{ts} {level:<5} [{logger:<18}] {message}\n"


def _make_config(tmp_path, *, admins=("alice",), log_output="both") -> Config:
    from istota import db

    logdir = tmp_path / "logs"
    logdir.mkdir(exist_ok=True)
    config = Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice"), "bob": UserConfig()},
        web=WebConfig(
            enabled=True,
            port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web",
            oauth2_client_secret="test-secret",
            session_secret_key="test-session-key",
        ),
        logging=LoggingConfig(output=log_output, file=str(logdir / "istota.log")),
    )
    config.admin_users = set(admins)
    db.init_db(config.db_path)
    return config


def _patch_app(config):
    import istota.web_app as mod

    mod._config = config
    mod.app.state.istota_config = config
    mock_oauth = MagicMock()
    mock_oauth.nextcloud = MagicMock()
    mod._oauth = mock_oauth
    return mod.app


async def _login(client, username):
    import istota.web_app as mod

    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username}
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


@pytest.fixture
async def env(tmp_path):
    """Config + logged-in admin client."""
    config = _make_config(tmp_path)
    app = _patch_app(config)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="https://example.com") as client:
        cookies = await _login(client, "alice")
        yield config, client, cookies


def _log_path(config) -> Path:
    return Path(config.logging.file)


class TestAdminGate:
    async def test_non_admin_is_refused_every_route(self, tmp_path):
        config = _make_config(tmp_path)
        _log_path(config).write_text(_line("2026-07-31 10:00:00", "INFO", "a", "x"))
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await _login(client, "bob")
            for path in (
                "/istota/api/admin/logs/sources",
                "/istota/api/admin/logs/app",
                "/istota/api/admin/config",
            ):
                resp = await client.get(path, cookies=cookies)
                assert resp.status_code == 403, path

    async def test_anonymous_is_refused(self, tmp_path):
        config = _make_config(tmp_path)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            resp = await client.get("/istota/api/admin/logs/sources")
            assert resp.status_code in (401, 403)

    async def test_blank_allowlist_means_no_web_admin(self, tmp_path):
        """Fails closed, unlike Config.is_admin's permissive empty rule."""
        config = _make_config(tmp_path, admins=())
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await _login(client, "alice")
            resp = await client.get("/istota/api/admin/logs/sources", cookies=cookies)
            assert resp.status_code == 403


class TestSources:
    async def test_lists_both_sources(self, env):
        config, client, cookies = env
        _log_path(config).write_text(_line("2026-07-31 10:00:00", "INFO", "a", "x"))
        resp = await client.get("/istota/api/admin/logs/sources", cookies=cookies)
        assert resp.status_code == 200
        by_id = {s["id"]: s for s in resp.json()["sources"]}
        assert by_id["app"]["available"] is True
        assert by_id["tasks"]["available"] is True

    async def test_unavailable_source_is_listed_with_its_reason(self, tmp_path):
        config = _make_config(tmp_path, log_output="console")
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await _login(client, "alice")
            resp = await client.get("/istota/api/admin/logs/sources", cookies=cookies)
            app_source = next(s for s in resp.json()["sources"] if s["id"] == "app")
            assert app_source["available"] is False
            assert app_source["detail"]


class TestLogPage:
    async def test_reads_the_app_log_tail(self, env):
        config, client, cookies = env
        _log_path(config).write_text(
            "".join(
                _line(f"2026-07-31 10:00:{i:02d}", "INFO", "istota.scheduler", f"m{i}")
                for i in range(5)
            )
        )
        resp = await client.get("/istota/api/admin/logs/app?limit=2", cookies=cookies)
        assert resp.status_code == 200
        body = resp.json()
        assert [r["message"] for r in body["records"]] == ["m3", "m4"]
        assert body["tail_cursor"]

    async def test_filters_pass_through(self, env):
        config, client, cookies = env
        _log_path(config).write_text(
            _line("2026-07-31 10:00:00", "INFO", "istota.feeds", "polled")
            + _line("2026-07-31 10:00:01", "ERROR", "istota.scheduler", "boom")
        )
        resp = await client.get(
            "/istota/api/admin/logs/app?level=ERROR", cookies=cookies
        )
        assert [r["message"] for r in resp.json()["records"]] == ["boom"]

        resp = await client.get(
            "/istota/api/admin/logs/app?logger=istota.feeds", cookies=cookies
        )
        assert [r["message"] for r in resp.json()["records"]] == ["polled"]

        resp = await client.get("/istota/api/admin/logs/app?q=BOOM", cookies=cookies)
        assert [r["message"] for r in resp.json()["records"]] == ["boom"]

    async def test_reads_the_task_log_source(self, env):
        from istota import db

        config, client, cookies = env
        with db.get_db(config.db_path) as conn:
            tid = db.create_task(conn, "p", "alice", source_type="talk")
            db.log_task(conn, tid, "info", "claimed")
        resp = await client.get("/istota/api/admin/logs/tasks", cookies=cookies)
        assert resp.status_code == 200
        record = resp.json()["records"][0]
        assert record["message"] == "claimed"
        assert record["task_id"] == tid
        assert record["user_id"] == "alice"

    async def test_unknown_source_is_404(self, env):
        _config, client, cookies = env
        resp = await client.get("/istota/api/admin/logs/nope", cookies=cookies)
        assert resp.status_code == 404

    async def test_a_path_shaped_source_id_is_404_not_a_read(self, env):
        """A client names a source, never a path — traversal is unreachable."""
        _config, client, cookies = env
        for probe in ("..%2f..%2fetc%2fpasswd", "%2Fetc%2Fpasswd", "..", "app%00"):
            resp = await client.get(f"/istota/api/admin/logs/{probe}", cookies=cookies)
            assert resp.status_code in (400, 404), probe

    async def test_unavailable_source_is_409(self, tmp_path):
        config = _make_config(tmp_path, log_output="console")
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await _login(client, "alice")
            resp = await client.get("/istota/api/admin/logs/app", cookies=cookies)
            assert resp.status_code == 409

    async def test_malformed_cursor_is_400(self, env):
        config, client, cookies = env
        _log_path(config).write_text(_line("2026-07-31 10:00:00", "INFO", "a", "x"))
        resp = await client.get(
            "/istota/api/admin/logs/app?before=nonsense", cookies=cookies
        )
        assert resp.status_code == 400

    async def test_limit_is_capped(self, env):
        config, client, cookies = env
        _log_path(config).write_text(_line("2026-07-31 10:00:00", "INFO", "a", "x"))
        resp = await client.get(
            "/istota/api/admin/logs/app?limit=999999", cookies=cookies
        )
        assert resp.status_code == 200

    async def test_paging_reaches_the_start(self, env):
        config, client, cookies = env
        _log_path(config).write_text(
            "".join(
                _line(f"2026-07-31 10:00:{i:02d}", "INFO", "a", f"m{i}")
                for i in range(6)
            )
        )
        first = (await client.get("/istota/api/admin/logs/app?limit=3", cookies=cookies)).json()
        assert first["next_before"]
        second = (
            await client.get(
                f"/istota/api/admin/logs/app?limit=3&before={first['next_before']}",
                cookies=cookies,
            )
        ).json()
        assert [r["message"] for r in second["records"]] == ["m0", "m1", "m2"]
        assert second["next_before"] is None


class _FakeRequest:
    """Minimal Request stand-in for driving the SSE generator directly.

    The log stream never terminates on its own, and an ASGI-transport
    `client.stream(...)` cannot close a generator that ignores the client going
    away — so the endpoint is called as a function and the disconnect is what
    ends the loop. Same harness `tests/test_chat_room_stream.py` uses.
    """

    def __init__(self, *, disconnect_after=2, on_check=None):
        self.headers = {}
        self._checks = 0
        self._limit = disconnect_after
        self._on_check = on_check

    async def is_disconnected(self) -> bool:
        self._checks += 1
        if self._on_check is not None:
            self._on_check(self._checks)
        return self._checks > self._limit


async def _drain_stream(source_id: str, cursor: str, request, **kwargs) -> str:
    import istota.web_app as mod

    # Every filter param is passed explicitly: calling the endpoint as a plain
    # function bypasses FastAPI's dependency resolution, so a `Query(...)`
    # default would arrive as the sentinel object rather than a value.
    kwargs.setdefault("level", None)
    kwargs.setdefault("q", None)
    kwargs.setdefault("logger_name", None)
    kwargs.setdefault("user_id", None)
    kwargs.setdefault("task_id", None)
    resp = await mod.admin_log_stream(
        source_id, request, cursor, _={"username": "alice"}, **kwargs
    )
    assert resp.media_type == "text/event-stream"
    assert resp.headers["x-accel-buffering"] == "no"
    out = ""
    async for chunk in resp.body_iterator:
        out += chunk if isinstance(chunk, str) else chunk.decode()
    return out


class TestLogStream:
    @pytest.fixture(autouse=True)
    def _fast_poll(self, monkeypatch):
        import istota.web_app as mod

        monkeypatch.setattr(mod, "_LOG_STREAM_POLL_SECONDS", 0.001)

    async def test_streams_appended_records(self, env):
        config, client, cookies = env
        path = _log_path(config)
        path.write_text(_line("2026-07-31 10:00:00", "INFO", "a", "first"))
        page = (await client.get("/istota/api/admin/logs/app", cookies=cookies)).json()
        cursor = page["tail_cursor"]

        def _append(check: int) -> None:
            if check == 1:
                with path.open("a") as fh:
                    fh.write(_line("2026-07-31 10:00:01", "INFO", "a", "second"))

        body = await _drain_stream(
            "app", cursor, _FakeRequest(disconnect_after=3, on_check=_append)
        )
        assert "event: records" in body
        assert "second" in body
        # The cursor already covered "first"; a tail must not replay it.
        assert "first" not in body

    async def test_a_rotated_file_reports_a_reset(self, env):
        config, client, cookies = env
        path = _log_path(config)
        path.write_text(
            "".join(
                _line(f"2026-07-31 10:00:{i:02d}", "INFO", "a", f"old{i}")
                for i in range(20)
            )
        )
        page = (await client.get("/istota/api/admin/logs/app", cookies=cookies)).json()
        cursor = page["tail_cursor"]

        def _rotate(check: int) -> None:
            if check == 1:
                path.write_text(_line("2026-07-31 11:00:00", "INFO", "a", "fresh"))

        body = await _drain_stream(
            "app", cursor, _FakeRequest(disconnect_after=3, on_check=_rotate)
        )
        frame = json.loads(body.split("event: records\ndata: ", 1)[1].split("\n\n", 1)[0])
        assert frame["reset"] is True
        assert [r["message"] for r in frame["records"]] == ["fresh"]

    async def test_idle_stream_emits_a_keepalive(self, env, monkeypatch):
        import istota.web_app as mod

        config, client, cookies = env
        _log_path(config).write_text(_line("2026-07-31 10:00:00", "INFO", "a", "only"))
        page = (await client.get("/istota/api/admin/logs/app", cookies=cookies)).json()
        monkeypatch.setattr(mod, "_LOG_STREAM_KEEPALIVE_TICKS", 1)
        body = await _drain_stream(
            "app", page["tail_cursor"], _FakeRequest(disconnect_after=2)
        )
        assert ": ping" in body

    async def test_streams_the_task_log_source(self, env):
        from istota import db

        config, client, cookies = env
        with db.get_db(config.db_path) as conn:
            tid = db.create_task(conn, "p", "alice", source_type="talk")
            db.log_task(conn, tid, "info", "claimed")
        page = (await client.get("/istota/api/admin/logs/tasks", cookies=cookies)).json()

        def _write(check: int) -> None:
            if check == 1:
                with db.get_db(config.db_path) as c:
                    db.log_task(c, tid, "error", "exploded")

        body = await _drain_stream(
            "tasks", page["tail_cursor"], _FakeRequest(disconnect_after=3, on_check=_write)
        )
        assert "exploded" in body
        assert "claimed" not in body

    async def test_malformed_cursor_fails_before_the_stream_starts(self, env):
        """A StreamingResponse cannot turn a later error into a 400."""
        config, client, cookies = env
        _log_path(config).write_text(_line("2026-07-31 10:00:00", "INFO", "a", "x"))
        resp = await client.get(
            "/istota/api/admin/logs/app/stream?cursor=bogus", cookies=cookies
        )
        assert resp.status_code == 400

    async def test_stream_requires_admin(self, tmp_path):
        config = _make_config(tmp_path)
        _log_path(config).write_text(_line("2026-07-31 10:00:00", "INFO", "a", "x"))
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await _login(client, "bob")
            resp = await client.get(
                "/istota/api/admin/logs/app/stream?cursor=istota.log:0", cookies=cookies
            )
            assert resp.status_code == 403

    async def test_a_stop_signal_ends_the_stream(self, env):
        """The tail polls until the client goes away, so on a restart it was
        cancelled at the graceful-shutdown timeout and the `CancelledError`
        reached uvicorn's log as a traceback. See `istota.web_shutdown`."""
        import asyncio

        from istota import web_shutdown

        config, client, cookies = env
        _log_path(config).write_text(_line("2026-07-31 10:00:00", "INFO", "a", "x"))
        page = (await client.get("/istota/api/admin/logs/app", cookies=cookies)).json()

        def _stop(check: int) -> None:
            if check == 2:
                web_shutdown.begin_shutdown()

        try:
            # The client never disconnects: only the stop notice can end this.
            await asyncio.wait_for(
                _drain_stream(
                    "app", page["tail_cursor"],
                    _FakeRequest(disconnect_after=100_000, on_check=_stop),
                ),
                timeout=10,
            )
        finally:
            web_shutdown.reset_for_tests()


class TestAdminConfig:
    async def test_returns_sectioned_config(self, env):
        _config, client, cookies = env
        resp = await client.get("/istota/api/admin/config", cookies=cookies)
        assert resp.status_code == 200
        body = resp.json()
        assert body["editable"] is False
        keys = {s["key"] for s in body["sections"]}
        assert "general" in keys
        assert "scheduler" in keys

    async def test_credentials_are_redacted_over_the_wire(self, tmp_path):
        config = _make_config(tmp_path)
        config.web.oauth2_client_secret = "TOP-SECRET-OAUTH"
        config.web.session_secret_key = "TOP-SECRET-SESSION"
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await _login(client, "alice")
            resp = await client.get("/istota/api/admin/config", cookies=cookies)
            assert "TOP-SECRET-OAUTH" not in resp.text
            assert "TOP-SECRET-SESSION" not in resp.text
            fields = {
                f["key"]: f
                for s in resp.json()["sections"]
                for f in s["fields"]
            }
            assert fields["web.oauth2_client_secret"]["secret"] is True
            assert fields["web.oauth2_client_secret"]["set"] is True
