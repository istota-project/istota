"""Authentication configuration and the local-only no-auth launch boundary."""

import pytest
from fastapi import FastAPI

from istota.config import Config, WebConfig


@pytest.fixture(autouse=True)
def restore_web_state(monkeypatch):
    from istota.webui import app as web_app

    monkeypatch.setattr(web_app, "_config", web_app._config)
    monkeypatch.setattr(web_app, "_oauth", web_app._oauth)


@pytest.mark.asyncio
async def test_direct_uvicorn_lifespan_refuses_no_auth(monkeypatch):
    from istota.webui import app as web_app

    cfg = Config(web=WebConfig(auth="none"))
    monkeypatch.setattr(web_app, "load_config", lambda: cfg)
    monkeypatch.setattr(web_app.signal, "signal", lambda *args: None)
    monkeypatch.setattr(web_app.web_shutdown, "install_signal_hook", lambda: None)
    app = FastAPI()
    app.state.host = "127.0.0.1"
    with pytest.raises(RuntimeError, match="istota serve"):
        async with web_app.lifespan(app):
            pass


@pytest.mark.asyncio
@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
async def test_validated_local_launcher_lifespan_accepts_no_auth(monkeypatch, host):
    from istota.webui import app as web_app

    cfg = Config(web=WebConfig(auth="none"))
    monkeypatch.setattr(web_app, "load_config", lambda: cfg)
    monkeypatch.setattr(web_app.signal, "signal", lambda *args: None)
    monkeypatch.setattr(web_app.web_shutdown, "install_signal_hook", lambda: None)
    app = FastAPI()
    app.state.local_no_auth_bind = host
    async with web_app.lifespan(app):
        assert app.state.istota_config is cfg


@pytest.mark.parametrize("host", [None, "0.0.0.0", "192.0.2.1"])
def test_reload_refuses_no_auth_and_preserves_config(monkeypatch, host, caplog):
    from istota.webui import app as web_app

    old = Config()
    old_oauth = object()
    monkeypatch.setattr(web_app, "_config", old)
    monkeypatch.setattr(web_app, "_oauth", old_oauth)
    monkeypatch.setattr(web_app, "load_config", lambda: Config(web=WebConfig(auth="none")))
    app = FastAPI()
    app.state.istota_config = old
    if host is not None:
        app.state.local_no_auth_bind = host
    web_app._reload_config_on_signal(app)
    assert web_app._config is old
    assert web_app._oauth is old_oauth
    assert app.state.istota_config is old
    assert "keeping the previously loaded config" in caplog.text


def test_email_requires_a_session_secret(monkeypatch):
    from istota.webui import app as web_app

    monkeypatch.delenv("ISTOTA_WEB_SESSION_SECRET_KEY", raising=False)
    monkeypatch.delenv("ISTOTA_WEB_ALLOW_INSECURE_SESSION", raising=False)
    monkeypatch.setattr(web_app, "load_config", lambda: Config(web=WebConfig(auth="email")))
    with pytest.raises(RuntimeError, match="signing secret"):
        web_app._resolve_session_secret()


def test_leaf_secret_resolution():
    from istota.webui.session_secret import resolve

    cfg = Config(web=WebConfig(auth="email", session_secret_key=" config-test-key "))
    assert resolve(cfg, {"ISTOTA_WEB_SESSION_SECRET_KEY": " env-test-key "}) == "env-test-key"
    assert resolve(cfg, {}) == "config-test-key"
    cfg.web.session_secret_key = ""
    assert resolve(cfg, {}) is None
    assert resolve(None, {}) is None
    assert resolve(cfg, {"ISTOTA_WEB_ALLOW_INSECURE_SESSION": "1"})
    cfg.web = WebConfig(auth="none")
    assert resolve(cfg, {})
