"""The servarr entry on `GET /admin/connections`.

Built from loaded config plus the daemon's stored `servarr.connection.<app>`
rows; the web process calls no arr. It never carries a key: `api_key_set` says
whether one is there, and the URL is shown with any userinfo removed.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("authlib")
pytest.importorskip("fastapi")

from httpx import ASGITransport, AsyncClient, AsyncHTTPTransport, HTTPTransport

from istota import db, doctor
from istota.config import (
    Config,
    ServarrApp,
    ServarrConfig,
    SiteConfig,
    UserConfig,
    WebConfig,
)

INDEX = "/istota/api/admin/connections"
KEY = "never-shown-arr-sentinel"
HOST = "10.0.0.5"


@pytest.fixture
def config(tmp_path):
    config = Config(
        db_path=tmp_path / "istota.db",
        workspace_path=tmp_path / "workspace",
        site=SiteConfig(hostname="example.com"),
        users={"alice": UserConfig(), "bob": UserConfig()},
        web=WebConfig(
            enabled=True,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="example-web-client",
            oauth2_client_secret="test-secret",
            session_secret_key="test-session-key",
        ),
        servarr=ServarrConfig(
            apps={
                "radarr": ServarrApp(url=f"http://{HOST}:7878", api_key=KEY),
                "sonarr": ServarrApp(url=f"http://{HOST}:8989", api_key=KEY),
            },
            requesters=["bob"],
            private_hosts=[HOST],
            defaults={
                "radarr": {"quality_profile": "HD - 720p/1080p", "root_folder": "/films"},
                "bazarr": {"x": "y"},
            },
        ),
    )
    config.admin_users = {"alice"}
    db.init_db(config.db_path)
    return config


@pytest.fixture(autouse=True)
def no_outbound_http(monkeypatch):
    sync = MagicMock(side_effect=AssertionError("Connections must not probe HTTP"))
    async_ = AsyncMock(side_effect=AssertionError("Connections must not probe HTTP"))
    monkeypatch.setattr(HTTPTransport, "handle_request", sync)
    monkeypatch.setattr(AsyncHTTPTransport, "handle_async_request", async_)
    yield
    sync.assert_not_called()
    async_.assert_not_called()


@pytest.fixture
def index(config, monkeypatch):
    import istota.webui.app as mod

    monkeypatch.setattr(mod, "_config", config)
    monkeypatch.setattr(mod.app.state, "istota_config", config, raising=False)
    oauth = MagicMock()
    monkeypatch.setattr(mod, "_oauth", oauth)

    async def get(username="alice"):
        async with AsyncClient(transport=ASGITransport(app=mod.app),
                               base_url="https://example.com") as client:
            if username is not None:
                oauth.nextcloud.authorize_access_token = AsyncMock(
                    return_value={"user_id": username},
                )
                login = await client.get("/istota/callback", follow_redirects=False)
                assert login.status_code == 302
            return await client.get(INDEX)

    return get


def _record(config, checks, *, at=None):
    stamp = at or datetime.now(timezone.utc)
    value = json.dumps({
        "at": stamp.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "request_id": None,
        "checks": checks,
    })
    with db.get_db(config.db_path) as conn:
        db.shared_kv_set(conn, doctor.REPORT_NAMESPACE, "report", value, "daemon")


def _row(app, status="ok", detail=None, remedy=""):
    return {"name": f"servarr.connection.{app}", "status": status,
            "detail": detail or f"{app} 5.9.1.9070 at http://{HOST}", "remedy": remedy,
            "scope": doctor.DEPLOYMENT}


def servarr_entry(response) -> dict:
    assert response.status_code == 200, response.text
    entries = response.json()["connections"]
    assert [e["id"] for e in entries] == ["nextcloud", "whatsapp", "google_workspace", "servarr"]
    return entries[3]


def _apps(entry) -> dict:
    return {row["app"]: row for row in entry["apps"]}


async def test_the_configured_facts(index):
    response = await index()
    entry = servarr_entry(response)
    assert entry["label"] == "Servarr"
    assert entry["status"] == "configured"
    assert [row["app"] for row in entry["apps"]] == ["sonarr", "radarr"]
    radarr = _apps(entry)["radarr"]
    assert radarr == {
        "app": "radarr",
        "url": f"http://{HOST}:7878",
        "api_key_set": True,
        "incomplete": False,
        "problem": None,
        "check": None,
    }
    assert entry["missing"] == ["lidarr", "prowlarr"]
    assert entry["requesters"] == ["bob"]
    assert entry["defaults"] == {
        "radarr": {"quality_profile": "HD - 720p/1080p", "root_folder": "/films"},
    }
    assert KEY not in response.text


async def test_no_apps_is_disabled(config, index):
    config.servarr = ServarrConfig()
    entry = servarr_entry(await index())
    assert entry["status"] == "disabled"
    assert entry["apps"] == []
    assert entry["missing"] == ["sonarr", "radarr", "lidarr", "prowlarr"]


async def test_a_dropped_table_is_incomplete(config, index):
    config.servarr.incomplete_apps = ["lidarr"]
    entry = servarr_entry(await index())
    assert entry["status"] == "incomplete"
    lidarr = _apps(entry)["lidarr"]
    assert lidarr["incomplete"] is True
    assert lidarr["api_key_set"] is None
    assert lidarr["url"] == ""
    assert "[servarr.apps.lidarr]" in lidarr["problem"]
    assert "lidarr" not in entry["missing"]


async def test_a_config_problem_the_doctor_reports_is_incomplete(config, index):
    config.servarr.apps["radarr"] = ServarrApp(url="http://arr.example.com:7878", api_key=KEY)
    entry = servarr_entry(await index())
    assert entry["status"] == "incomplete"
    assert '[private_hosts] "arr.example.com" = ["servarr"]' in _apps(entry)["radarr"]["problem"]
    assert _apps(entry)["sonarr"]["problem"] is None


async def test_userinfo_is_stripped_and_the_key_never_appears(config, index):
    secret = "pw-in-url-zz81"
    config.servarr.apps["radarr"] = ServarrApp(
        url=f"http://admin:{secret}@{HOST}:7878", api_key=KEY)
    response = await index()
    entry = servarr_entry(response)
    assert _apps(entry)["radarr"]["url"] == f"http://{HOST}:7878"
    assert secret not in response.text
    assert KEY not in response.text
    assert entry["status"] == "incomplete"


async def test_doctor_rows_attach_per_app(config, index):
    at = datetime.now(timezone.utc).replace(microsecond=0)
    _record(config, [
        _row("radarr", status="warn", detail="radarr at http://10.0.0.5:7878: connection_failed",
             remedy="The app may be down."),
        _row("sonarr"),
        {"name": "nextcloud.connection", "status": "ok", "detail": "x", "remedy": "",
         "scope": doctor.DEPLOYMENT},
    ], at=at)
    entry = servarr_entry(await index())
    apps = _apps(entry)
    assert apps["radarr"]["check"] == {
        "status": "warn",
        "detail": "radarr at http://10.0.0.5:7878: connection_failed",
        "remedy": "The app may be down.",
        "at": at.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "stale": False,
    }
    assert apps["sonarr"]["check"]["status"] == "ok"


async def test_a_row_echoing_the_key_is_redacted(config, index):
    _record(config, [_row("radarr", status="fail", detail=f"saw {KEY}")])
    response = await index()
    assert KEY not in response.text


async def test_an_old_report_is_stale(config, index):
    import istota.webui.app as mod

    interval = config.scheduler.doctor_check_interval
    bound = interval * mod._DOCTOR_STALE_INTERVALS + mod._DOCTOR_STALE_SLACK_SECONDS
    _record(config, [_row("radarr")],
            at=datetime.now(timezone.utc) - timedelta(seconds=bound + 120))
    entry = servarr_entry(await index())
    assert _apps(entry)["radarr"]["check"]["stale"] is True


@pytest.mark.parametrize("username,status", [("alice", 200), ("bob", 403), (None, 401)])
async def test_admin_gate(index, username, status):
    response = await index(username)
    assert response.status_code == status
    if status == 200:
        servarr_entry(response)
    else:
        assert KEY not in response.text
        assert HOST not in response.text
