"""Admin browser discovery at the HTTP boundary."""
from unittest.mock import AsyncMock

import httpx
import pytest

from tests.test_web_admin_logs import _make_config, _patch_app, _login


@pytest.mark.parametrize("username,admins,status", [
    (None, {"alice"}, 401), ("bob", {"alice"}, 403), ("alice", set(), 403),
])
async def test_admin_gate(tmp_path, monkeypatch, username, admins, status):
    from istota.webui import admin_browsers
    fetch = AsyncMock()
    monkeypatch.setattr(admin_browsers, "snapshot", fetch)
    app = _patch_app(_make_config(tmp_path, admins=admins))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="https://example.com") as client:
        if username:
            await _login(client, username)
        response = await client.get("/istota/api/admin/browsers")
    assert response.status_code == status
    fetch.assert_not_called()


async def test_discovery_through_admin_endpoint(tmp_path, monkeypatch):
    from istota.webui import admin_browsers
    config = _make_config(tmp_path)
    config.browser.enabled = True
    config.browser.api_url = "http://browser:9223"
    config.browser.vnc_url = "https://console.example.com/vnc.html?resize=scale"
    app = _patch_app(config)
    real_client = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.path == "/instances"
        assert request.url.params["vnc_url"] == config.browser.vnc_url
        return httpx.Response(200, json={"instances": [{
            "user": "alice", "slot": 0, "idle_seconds": 42,
            "url": "https://console.example.com/vnc.html?path=websockify%2F%3Ftoken%3Dalice",
        }]})

    monkeypatch.setattr(admin_browsers.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(respond), **kw))
    async with real_client(transport=httpx.ASGITransport(app=app), base_url="https://example.com") as client:
        await _login(client, "alice")
        response = await client.get("/istota/api/admin/browsers")
    assert response.status_code == 200
    assert response.json()["instances"][0]["idle_seconds"] == 42
    assert response.json()["status"] == "ok"
    assert response.headers["cache-control"] == "no-store"
    assert len(requests) == 1


@pytest.mark.parametrize("mode", ["disabled", "unavailable", "missing_url", "bad_payload"])
async def test_discovery_states(monkeypatch, mode):
    from istota.webui import admin_browsers
    from istota.config import BrowserConfig
    config = BrowserConfig(enabled=mode != "disabled", vnc_url="")
    real_client = httpx.AsyncClient
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.params["vnc_url"] == ""
        if mode == "unavailable":
            raise httpx.ConnectError("private service detail")
        if mode == "bad_payload":
            return httpx.Response(200, json={"instances": [{"user": "alice"}]})
        return httpx.Response(200, json={"instances": [{
            "user": "alice", "slot": 0, "idle_seconds": 1, "url": "",
        }]})

    monkeypatch.setattr(admin_browsers.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(respond), **kw))
    result = await admin_browsers.snapshot(config)
    expected = "unavailable" if mode in {"unavailable", "bad_payload"} else "disabled" if mode == "disabled" else "ok"
    assert result["status"] == expected
    assert "private" not in str(result)
    if mode == "disabled":
        assert not requests
    if mode == "missing_url":
        assert result["console_configured"] is False
        assert result["instances"][0]["url"] == ""


@pytest.mark.parametrize("url", [
    "", "/vnc.html", "javascript:alert(1)", "https://alice:placeholder@console.example.com/vnc.html",
    "https://console.example.com/vnc.html?password=placeholder",
    "https://console.example.com/vnc.html#password=placeholder",
])
def test_console_rejects_relative_urls_and_credentials(url):
    from istota.webui.admin_browsers import _console_url
    assert _console_url(url) == ""


async def test_timeout_is_unavailable(monkeypatch):
    from istota.webui import admin_browsers
    from istota.config import BrowserConfig
    real_client = httpx.AsyncClient

    def respond(request):
        raise httpx.ReadTimeout("private service detail")

    monkeypatch.setattr(admin_browsers.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(respond), **kw))
    result = await admin_browsers.snapshot(BrowserConfig(enabled=True))
    assert result["status"] == "unavailable"


# ISSUE-699: on the Docker stack the web container is not on the browser
# network, so the daemon records what it sees and the page reads that.

def _browser_config(tmp_path):
    config = _make_config(tmp_path)
    config.browser.enabled = True
    config.browser.api_url = "http://browser:9223"
    return config


_REAL_ASYNC_CLIENT = httpx.AsyncClient


def _instances_transport(monkeypatch, requests):
    """Patch the browser API's answer. Call after `_patch_app`: the patch is
    httpx-wide, and the app's import subclasses the real client."""
    from istota.webui import admin_browsers

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"instances": [{
            "user": "alice", "slot": 0, "idle_seconds": 7, "url": "",
        }]})

    monkeypatch.setattr(admin_browsers.httpx, "AsyncClient", lambda **kw: _REAL_ASYNC_CLIENT(
        transport=httpx.MockTransport(respond), **kw))


async def _get_browsers(app):
    async with _REAL_ASYNC_CLIENT(transport=httpx.ASGITransport(app=app), base_url="https://example.com") as client:
        await _login(client, "alice")
        return await client.get("/istota/api/admin/browsers")


async def test_the_page_reads_what_the_daemon_recorded_without_probing(tmp_path, monkeypatch):
    import asyncio
    from istota import scheduler
    config = _browser_config(tmp_path)
    app = _patch_app(config)
    requests = []
    _instances_transport(monkeypatch, requests)

    # The daemon's gate body, on a thread as `_spawn_background_check` runs it.
    await asyncio.to_thread(scheduler.check_browser_instances, config)
    assert len(requests) == 1

    response = await _get_browsers(app)
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["instances"] == [{"user": "alice", "slot": 0, "idle_seconds": 7, "url": ""}]
    assert body["as_of"]
    assert len(requests) == 1, "the web process probed the browser itself"


async def test_a_stale_record_reads_as_unavailable(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from istota.webui import admin_browsers
    config = _browser_config(tmp_path)
    app = _patch_app(config)
    requests = []
    _instances_transport(monkeypatch, requests)
    old =datetime.now(timezone.utc) - timedelta(seconds=admin_browsers.STALE_AFTER_SECONDS + 5)
    admin_browsers.record_snapshot(
        config, {"status": "ok", "console_configured": False,
                 "instances": [{"user": "alice", "slot": 0, "idle_seconds": 1, "url": ""}]},
        at=old,
    )

    body = (await _get_browsers(app)).json()
    assert body["status"] == "unavailable"
    assert body["instances"] == []
    assert not requests


async def test_a_disabled_browser_ignores_an_old_record(tmp_path):
    from istota.webui import admin_browsers
    config = _browser_config(tmp_path)
    admin_browsers.record_snapshot(config, {"status": "ok", "console_configured": False, "instances": []})
    config.browser.enabled = False

    body = (await _get_browsers(_patch_app(config))).json()
    assert body["status"] == "disabled"


def test_the_daemon_records_only_while_the_browser_is_enabled(tmp_path):
    from istota.scheduler import build_interval_gates
    config = _make_config(tmp_path)
    gate = next(g for g in build_interval_gates(config) if g.name == "browser-instances")
    assert gate.background
    assert gate.enabled(config) is False
    config.browser.enabled = True
    assert gate.enabled(config) is True
