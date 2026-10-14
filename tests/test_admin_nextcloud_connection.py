"""The Nextcloud entry on `GET /admin/connections`.

The web process calls no Nextcloud endpoint here: the entry carries the
daemon's last `nextcloud.connection` row from the recorded doctor report,
redacted again with this process's config, and the staleness rule the Health
pane uses. Harness in the pattern of `tests/test_whatsapp_pairing_web.py`.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

from istota import db, doctor
from istota.config import (
    Config,
    NextcloudConfig,
    SiteConfig,
    TalkConfig,
    UserConfig,
    WebConfig,
)

pytestmark = pytest.mark.skipif(
    not _has_web_deps,
    reason="web dependencies not installed (install with: uv sync --extra web)",
)

ADMIN = "alice"
INDEX = "/istota/api/admin/connections"
APP_PASSWORD = "nc-app-password-zz9-plural-z-alpha"


def _make_config(tmp_path, *, url="https://cloud.example.com", username="istota",
                 bot_username="istota") -> Config:
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    config = Config(
        db_path=state / "istota.db",
        workspace_path=tmp_path / "workspace",
        temp_dir=tmp_path / "tmp",
        site=SiteConfig(hostname="example.com"),
        users={ADMIN: UserConfig(display_name="Alice")},
        nextcloud=NextcloudConfig(url=url, username=username, app_password=APP_PASSWORD),
        talk=TalkConfig(enabled=True, bot_username=bot_username),
        web=WebConfig(
            enabled=True,
            port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web",
            oauth2_client_secret="test-secret",
            session_secret_key="test-session-key",
        ),
    )
    config.admin_users = {ADMIN}
    db.init_db(config.db_path)
    return config


def _record(config, checks, *, at=None):
    """A daemon report as `doctor.record_report` writes it, with a chosen time."""
    stamp = at or datetime.now(timezone.utc)
    value = json.dumps({
        "at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "request_id": None,
        "checks": checks,
    })
    with db.get_db(config.db_path) as conn:
        db.shared_kv_set(conn, doctor.REPORT_NAMESPACE, "report", value, "daemon")


def _row(name="nextcloud.connection", status="ok", detail="signed in as istota (Istota) "
         "on Nextcloud 31.0.5", remedy=""):
    return {"name": name, "status": status, "detail": detail, "remedy": remedy,
            "scope": doctor.DEPLOYMENT}


async def _index(config) -> tuple[dict, str]:
    import istota.webui.app as mod

    mod._config = config
    mod.app.state.istota_config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": ADMIN})
    async with AsyncClient(transport=ASGITransport(app=mod.app),
                           base_url="https://example.com") as client:
        login = await client.get("/istota/callback", follow_redirects=False)
        resp = await client.get(INDEX, cookies=login.cookies)
    assert resp.status_code == 200, resp.text
    return resp.json(), resp.text


def _nextcloud(body: dict) -> dict:
    return next(c for c in body["connections"] if c["id"] == "nextcloud")


class TestTheEntry:
    async def test_nextcloud_comes_first_then_whatsapp(self, tmp_path):
        body, _ = await _index(_make_config(tmp_path))
        assert [c["id"] for c in body["connections"]] == ["nextcloud", "whatsapp"]

    async def test_the_configured_facts(self, tmp_path):
        body, _ = await _index(_make_config(tmp_path, bot_username="istota-bot"))
        entry = _nextcloud(body)
        assert entry["label"] == "Nextcloud"
        assert entry["configured"] is True
        assert entry["url"] == "https://cloud.example.com"
        assert entry["username"] == "istota"
        assert entry["talk_enabled"] is True
        assert entry["talk_bot_username"] == "istota-bot"
        assert entry["storage"] == "local"

    async def test_no_url_is_not_configured(self, tmp_path):
        body, _ = await _index(_make_config(tmp_path, url="", username=""))
        entry = _nextcloud(body)
        assert entry["configured"] is False
        assert entry["url"] == ""
        assert entry["check"] is None

    async def test_userinfo_is_removed_from_the_url(self, tmp_path):
        config = _make_config(
            tmp_path, url=f"https://istota:{APP_PASSWORD}@cloud.example.com:8443/nc/",
        )
        body, text = await _index(config)
        assert _nextcloud(body)["url"] == "https://cloud.example.com:8443/nc"
        assert APP_PASSWORD not in text

    async def test_a_username_alone_in_the_url_is_removed_too(self, tmp_path):
        body, _ = await _index(_make_config(tmp_path, url="https://istota@cloud.example.com"))
        assert _nextcloud(body)["url"] == "https://cloud.example.com"

    async def test_the_app_password_is_absent_from_the_body(self, tmp_path):
        config = _make_config(tmp_path)
        _record(config, [_row()])
        _, text = await _index(config)
        assert APP_PASSWORD not in text


class TestTheCheck:
    async def test_no_report_gives_no_check(self, tmp_path):
        body, _ = await _index(_make_config(tmp_path))
        assert _nextcloud(body)["check"] is None

    async def test_a_report_without_the_row_gives_no_check(self, tmp_path):
        config = _make_config(tmp_path)
        _record(config, [_row(name="runtime.mount_liveness")])
        body, _ = await _index(config)
        assert _nextcloud(body)["check"] is None

    async def test_the_recorded_row_is_carried(self, tmp_path):
        config = _make_config(tmp_path)
        at = datetime.now(timezone.utc).replace(microsecond=0)
        _record(config, [
            _row(name="runtime.mount_liveness", status="fail", detail="other"),
            _row(status="fail", detail="cloud.example.com did not accept the credentials "
                 "for istota", remedy="Issue a new app password."),
        ], at=at)
        check = _nextcloud((await _index(config))[0])["check"]
        assert check == {
            "status": "fail",
            "detail": "cloud.example.com did not accept the credentials for istota",
            "remedy": "Issue a new app password.",
            "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "stale": False,
        }

    async def test_the_row_is_redacted_again_with_this_config(self, tmp_path):
        """The daemon redacted with its own config; this process's secret set
        can differ, and it is the one putting the text on the wire."""
        config = _make_config(tmp_path)
        _record(config, [_row(status="warn", detail=f"saw {APP_PASSWORD} somewhere",
                              remedy=f"rotate {APP_PASSWORD}")])
        body, text = await _index(config)
        check = _nextcloud(body)["check"]
        assert APP_PASSWORD not in text
        assert check["detail"].startswith("saw ")
        assert check["remedy"].startswith("rotate ")

    async def test_an_old_report_is_stale(self, tmp_path):
        import istota.webui.app as mod

        config = _make_config(tmp_path)
        interval = config.scheduler.doctor_check_interval
        bound = interval * mod._DOCTOR_STALE_INTERVALS + mod._DOCTOR_STALE_SLACK_SECONDS
        _record(config, [_row()],
                at=datetime.now(timezone.utc) - timedelta(seconds=bound + 120))
        assert _nextcloud((await _index(config))[0])["check"]["stale"] is True

    async def test_an_unreadable_report_still_answers_both_entries(self, tmp_path, monkeypatch,
                                                                   caplog):
        config = _make_config(tmp_path)

        def broken(_config):
            raise RuntimeError(f"db said {APP_PASSWORD}")

        monkeypatch.setattr(doctor, "read_reports", broken)
        with caplog.at_level("WARNING", logger="istota.webui.app"):
            body, _ = await _index(config)
        assert [c["id"] for c in body["connections"]] == ["nextcloud", "whatsapp"]
        assert _nextcloud(body)["check"] is None
        assert "nextcloud_connection_report_unread" in caplog.text
        assert APP_PASSWORD not in caplog.text


class TestTheStalenessRuleIsShared:
    def test_the_health_pane_and_the_card_read_one_bound(self):
        import istota.webui.app as mod

        interval = 3600
        bound = interval * mod._DOCTOR_STALE_INTERVALS + mod._DOCTOR_STALE_SLACK_SECONDS

        def at(seconds_ago):
            stamp = datetime.now(timezone.utc) - timedelta(seconds=seconds_ago)
            return stamp.strftime("%Y-%m-%dT%H:%M:%SZ")

        assert mod._doctor_report_stale_age(at(bound - 60), interval) is None
        assert mod._doctor_report_stale_age(at(bound + 60), interval) is not None
        assert mod._doctor_report_stale_age(at(bound + 60), 0) is None
        assert mod._doctor_report_stale_age("not a stamp", interval) is None
