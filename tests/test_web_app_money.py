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
    import istota.money  # noqa: F401
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
    SiteConfig,
    UserConfig,
    WebConfig,
)


def _seed_money_workspace(tmp_path: Path) -> None:
    """Drop a beancount file inside alice's workspace so ``_discover_ledgers``
    finds it. Workspace is ``{mount}/Users/{user}/{bot_dir}/money/ledgers/``."""
    ledgers_dir = (
        tmp_path / "mount" / "Users" / "alice" / "istota" / "money" / "ledgers"
    )
    ledgers_dir.mkdir(parents=True)
    (ledgers_dir / "main.beancount").write_text("")


def _istota_config(tmp_path, *, with_money: bool = False) -> Config:
    if with_money:
        _seed_money_workspace(tmp_path)
    users = {
        "alice": UserConfig(display_name="Alice"),
        # Money module is on by default after the modules refactor; bob
        # opts out explicitly so the "module disabled" code path stays
        # testable.
        "bob": UserConfig(display_name="Bob", disabled_modules=["money"]),
    }
    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        site=SiteConfig(enabled=True, hostname="example.com"),
        users=users,
        web=WebConfig(
            enabled=True,
            port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web",
            oauth2_client_secret="test-secret",
            session_secret_key="test-session-key",
        ),
        bot_name="Istota",
    )


def _patch_app(config: Config):
    import istota.web_app as mod
    mod._config = config
    mod._oauth = MagicMock()
    mod._oauth.nextcloud = MagicMock()
    # Routes resolve per-user config via request.app.state.istota_config.
    mod.app.state.istota_config = config
    return mod.app


@pytest.fixture
def app_with_money(tmp_path):
    return _patch_app(_istota_config(tmp_path, with_money=True))


@pytest.fixture
def app_without_money(tmp_path):
    return _patch_app(_istota_config(tmp_path, with_money=False))


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
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": username})
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


@_needs
class TestMoneyMount:
    async def test_unauthenticated_returns_401(self, client_with_money):
        resp = await client_with_money.get("/istota/api/money/me")
        assert resp.status_code == 401

    async def test_authenticated_with_resource_returns_user(self, client_with_money):
        cookies = await _login_as(client_with_money, "alice")
        resp = await client_with_money.get("/istota/api/money/me", cookies=cookies)
        assert resp.status_code == 200
        assert resp.json()["username"] == "alice"

    async def test_authenticated_with_module_disabled_returns_404(self, client_with_money):
        # bob has the money module disabled; the per-user config resolver
        # raises UserNotFoundError, which the route surfaces as 404.
        cookies = await _login_as(client_with_money, "bob")
        resp = await client_with_money.get("/istota/api/money/ledgers", cookies=cookies)
        assert resp.status_code == 404

    async def test_ledgers_returns_configured_ledgers(self, client_with_money):
        cookies = await _login_as(client_with_money, "alice")
        resp = await client_with_money.get("/istota/api/money/ledgers", cookies=cookies)
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
