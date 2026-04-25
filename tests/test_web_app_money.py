"""Tests for money web routes mounted under istota's web app."""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401
    _has_web_deps = True
except ImportError:
    _has_web_deps = False

try:
    import money  # noqa: F401
    _has_money = True
except ImportError:
    _has_money = False

_needs = pytest.mark.skipif(
    not (_has_web_deps and _has_money),
    reason="needs web + money extras (uv sync --extra web --extra money)",
)

if _has_web_deps and _has_money:
    from httpx import ASGITransport, AsyncClient

from istota.config import (
    Config,
    ResourceConfig,
    SiteConfig,
    UserConfig,
    WebConfig,
)


def _money_config(tmp_path: Path) -> Path:
    """Build a minimal money config TOML on disk and return its path."""
    data_dir = tmp_path / "money_data"
    (data_dir / "ledgers").mkdir(parents=True)
    (data_dir / "ledgers" / "main.beancount").write_text("")
    config = tmp_path / "money_config.toml"
    config.write_text(
        f'[users.alice]\n'
        f'data_dir = "{data_dir}"\n'
        f'ledgers = ["main"]\n'
    )
    return config


def _istota_config(tmp_path, money_config_path: Path | None = None) -> Config:
    resources = []
    if money_config_path is not None:
        resources.append(ResourceConfig(
            type="money",
            name="Money",
            extra={"config_path": str(money_config_path), "user_key": "alice"},
        ))
    users = {
        "alice": UserConfig(display_name="Alice", resources=resources),
        "bob": UserConfig(display_name="Bob"),  # no money resource
    }
    return Config(
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(enabled=True, hostname="example.com"),
        users=users,
        web=WebConfig(
            enabled=True,
            port=8766,
            oidc_issuer="https://cloud.example.com",
            oidc_client_id="istota-web",
            oidc_client_secret="test-secret",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


def _patch_app(config: Config):
    import istota.web_app as mod
    mod._config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    # Re-install the money loader against the new config
    mod._install_money_loader()
    return mod.app


@pytest.fixture
def app_with_money(tmp_path):
    money_config = _money_config(tmp_path)
    return _patch_app(_istota_config(tmp_path, money_config_path=money_config))


@pytest.fixture
def app_without_money(tmp_path):
    return _patch_app(_istota_config(tmp_path, money_config_path=None))


@pytest.fixture
async def client_with_money(app_with_money):
    transport = ASGITransport(app=app_with_money)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


@pytest.fixture
async def client_without_money(app_without_money):
    transport = ASGITransport(app=app_without_money)
    async with AsyncClient(transport=transport, base_url="https://example.com") as c:
        yield c


async def _login_as(client, username):
    import istota.web_app as mod
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={
        "userinfo": {"preferred_username": username, "name": username.title()},
    })
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


@_needs
class TestMoneyMount:
    async def test_unauthenticated_returns_401(self, client_with_money):
        resp = await client_with_money.get("/istota/money/api/me")
        assert resp.status_code == 401

    async def test_authenticated_with_resource_returns_user(self, client_with_money):
        cookies = await _login_as(client_with_money, "alice")
        resp = await client_with_money.get("/istota/money/api/me", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json()["username"] == "alice"

    async def test_authenticated_without_resource_returns_404(self, client_with_money):
        # bob has no money resource; the per-user config resolver should 404
        cookies = await _login_as(client_with_money, "bob")
        resp = await client_with_money.get("/istota/money/api/ledgers", cookies=cookies)
        assert resp.status_code == 404

    async def test_ledgers_returns_configured_ledgers(self, client_with_money):
        cookies = await _login_as(client_with_money, "alice")
        resp = await client_with_money.get("/istota/money/api/ledgers", cookies=cookies)
        assert resp.status_code == 200
        body = resp.json()
        assert body == {"ledgers": ["main"]}


@_needs
class TestApiMeMoneyFeature:
    async def test_money_feature_true_when_resource_present(self, client_with_money):
        cookies = await _login_as(client_with_money, "alice")
        resp = await client_with_money.get("/istota/api/me", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json()["features"]["money"] is True

    async def test_money_feature_false_when_no_resource(self, client_with_money):
        cookies = await _login_as(client_with_money, "bob")
        resp = await client_with_money.get("/istota/api/me", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json()["features"]["money"] is False
