"""Admin identity controls through the authenticated HTTP seam."""

import base64
from concurrent.futures import ThreadPoolExecutor
import json
import re
import threading
from urllib.parse import parse_qs, urlparse

from httpx import ASGITransport, AsyncClient
from itsdangerous import TimestampSigner
import pytest

from istota import db, user_profiles, web_auth
from istota.config import Config, SiteConfig, WebConfig

pytest.importorskip("authlib")
pytest.importorskip("fastapi")


@pytest.fixture
def configured(db_path, monkeypatch):
    from istota import web_app as mod
    config = Config(db_path=db_path, site=SiteConfig(hostname="example.com"),
                    web=WebConfig(auth=["email", "nextcloud"]), admin_users={"alice"})
    monkeypatch.setattr(mod, "_config", config)
    monkeypatch.setattr(mod.app.state, "istota_config", config, raising=False)
    for name in ("alice", "bob"):
        user_profiles.ensure_profile(db_path, name)
        web_auth.upsert_identity(db_path, name, f"{name}@example.com")
    monkeypatch.setattr(mod.web_auth_mail, "send_auth_email", lambda *args: True)
    return mod


def session(client, mod, username, method="nextcloud", epoch=None):
    identity = web_auth.get_identity(mod._config.db_path, username)
    data = {"user": {"username": username, "display_name": username},
            "auth": {"method": method, "epoch": epoch if epoch is not None else identity.credential_epoch if identity else 0}}
    secret = next(m.kwargs["secret_key"] for m in mod.app.user_middleware if m.cls.__name__ == "SessionMiddleware")
    value = TimestampSigner(str(secret)).sign(base64.b64encode(json.dumps(data).encode())).decode()
    client.cookies.set("istota_session", value, domain="example.com", path="/istota/")


@pytest.fixture
async def client(configured):
    async with AsyncClient(transport=ASGITransport(app=configured.app), base_url="https://example.com",
                           headers={"origin": "https://example.com"}) as client:
        session(client, configured, "alice")
        yield client


ROUTES = [("GET", ""), ("POST", ""), ("DELETE", "/bob")] + [
    ("POST", f"/bob/{action}") for action in ("invite", "reset", "disable", "logout-all")]


@pytest.mark.parametrize("method,path", ROUTES)
async def test_non_admin_refused(client, configured, method, path):
    session(client, configured, "bob")
    response = await client.request(method, "/istota/api/admin/users" + path, json={})
    assert response.status_code == 403


@pytest.mark.parametrize("method,path", [r for r in ROUTES if r[0] != "GET"])
async def test_mutations_require_origin(client, method, path):
    response = await client.request(method, "/istota/api/admin/users" + path, json={}, headers={"origin": "https://other.example.com"})
    assert response.status_code == 403


@pytest.mark.parametrize("user_id", ["../bad", "/bad", ".", "..", "a/b", "a\\b", "a b", "Upper", "a" * 33])
async def test_new_id_validation(client, configured, user_id):
    response = await client.post("/istota/api/admin/users", json={"user_id": user_id, "email": "new@example.com"})
    assert response.status_code == 400
    assert user_profiles.get_profile(configured._config.db_path, user_id) is None


async def test_create_lists_states_and_sends_invite(client, configured, monkeypatch):
    mail = []
    loop_thread = threading.get_ident()
    def send(*args):
        assert threading.get_ident() != loop_thread
        mail.append(args)
        return True
    monkeypatch.setattr(configured.web_auth_mail, "send_auth_email", send)
    response = await client.post("/istota/api/admin/users", json={"user_id": "carol", "email": " Carol@EXAMPLE.com ", "display_name": "Carol"})
    assert response.status_code == 200
    assert user_profiles.get_profile(configured._config.db_path, "carol").display_name == "Carol"
    assert mail[0][1] == "carol@example.com"
    link = re.search(r'https://[^\s]+', mail[0][3])[0]
    assert web_auth.peek_token(configured._config.db_path, parse_qs(urlparse(link).query)["token"][0], "enrol")
    user_profiles.ensure_profile(configured._config.db_path, "legacy")
    web_auth.set_password(configured._config.db_path, "bob", "example long passphrase")
    with db.get_db(configured._config.db_path) as conn:
        conn.execute("DELETE FROM user_profiles WHERE user_id = 'bob'")
    data = (await client.get("/istota/api/admin/users")).json()
    rows = {row["user_id"]: row for row in data["users"]}
    assert rows["legacy"]["state"] == "nextcloud_only"
    assert rows["carol"]["state"] == "passwordless"
    assert data["orphans"][0]["state"] == "password_set"
    assert "password_hash" not in json.dumps(data)
    assert "credential_epoch" not in json.dumps(data)


async def test_attach_preserves_case_profile_and_revokes(client, configured):
    path = configured._config.db_path
    user_profiles.ensure_profile(path, "Legacy.User", display_name="Original", timezone="Europe/Paris")
    before = user_profiles.get_profile(path, "Legacy.User")
    async with AsyncClient(transport=ASGITransport(app=configured.app), base_url="https://example.com") as other:
        session(other, configured, "Legacy.User")
        assert (await other.get("/istota/api/me")).status_code == 200
        response = await client.post("/istota/api/admin/users", json={"user_id": "Legacy.User", "email": "legacy@example.com", "display_name": "Replacement"})
        assert response.status_code == 200
        assert user_profiles.get_profile(path, "Legacy.User") == before
        assert (await other.get("/istota/api/me")).status_code == 401
    response = await client.post("/istota/api/admin/users", json={"user_id": "legacy.user", "email": "new@example.com"})
    assert response.status_code == 400
    assert "Legacy.User" in response.text


async def test_duplicate_and_failed_identity_write_leave_no_profile(client, configured):
    path = configured._config.db_path
    response = await client.post("/istota/api/admin/users", json={"user_id": "carol", "email": "BOB@example.com"})
    assert response.status_code == 400
    assert "bob" in response.text
    assert user_profiles.get_profile(path, "carol") is None
    with db.get_db(path) as conn:
        conn.execute("CREATE TRIGGER reject_identity BEFORE INSERT ON web_auth_identities BEGIN SELECT RAISE(ABORT, 'private failure'); END")
    response = await client.post("/istota/api/admin/users", json={"user_id": "carol", "email": "carol@example.com"})
    assert response.status_code == 503
    assert "private failure" not in response.text
    assert user_profiles.get_profile(path, "carol") is None


@pytest.mark.parametrize("action", ["disable", "logout-all", "remove"])
async def test_revokes_nextcloud_and_preserves_profile(client, configured, action):
    path = configured._config.db_path
    async with AsyncClient(transport=ASGITransport(app=configured.app), base_url="https://example.com") as other:
        session(other, configured, "bob")
        if action == "remove":
            response = await client.delete("/istota/api/admin/users/bob")
        else:
            response = await client.post(f"/istota/api/admin/users/bob/{action}", json={"disabled": True})
        assert response.status_code == 200
        assert (await other.get("/istota/api/me")).status_code == 401
    assert user_profiles.get_profile(path, "bob") is not None


@pytest.mark.parametrize("action", ["disable", "logout-all", "invite", "reset", "remove"])
async def test_nextcloud_only_controls_refuse(client, configured, action):
    web_auth.delete_identity(configured._config.db_path, "bob")
    response = await client.request("DELETE" if action == "remove" else "POST", "/istota/api/admin/users/bob" + ("" if action == "remove" else f"/{action}"), json={"disabled": True})
    assert response.status_code == 400


@pytest.mark.parametrize("action", ["disable", "remove"])
async def test_last_admin_refusal(client, configured, action):
    response = await client.request("DELETE" if action == "remove" else "POST", "/istota/api/admin/users/alice" + ("" if action == "remove" else "/disable"), json={"disabled": True})
    assert response.status_code == 400
    assert "last enabled admin" in response.text.lower()
    assert not web_auth.get_identity(configured._config.db_path, "alice").disabled


def test_last_admin_guard_is_atomic(configured):
    path = configured._config.db_path
    def remove(name):
        try:
            web_auth.delete_identity(path, name, protected_admins={"alice", "bob"})
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(remove, ["alice", "bob"])) == [False, True]
    assert len(web_auth.list_identities(path)) == 1


@pytest.mark.parametrize("action,purpose", [("invite", "enrol"), ("reset", "reset")])
async def test_admin_links_and_visible_sanitized_mail_errors(client, configured, monkeypatch, action, purpose):
    mail = []
    monkeypatch.setattr(configured.web_auth_mail, "send_auth_email", lambda *args: mail.append(args) or True)
    response = await client.post(f"/istota/api/admin/users/bob/{action}")
    assert response.status_code == 200
    link = re.search(r'https://[^\s]+', mail[0][3])[0]
    assert web_auth.peek_token(configured._config.db_path, parse_qs(urlparse(link).query)["token"][0], purpose)
    monkeypatch.setattr(configured.web_auth_mail, "send_auth_email", lambda *args: False)
    response = await client.post(f"/istota/api/admin/users/bob/{action}")
    assert response.status_code == 502
    assert "could not be sent" in response.text
    configured._config.site.hostname = ""
    response = await client.post(f"/istota/api/admin/users/bob/{action}")
    assert response.status_code == 403
    assert response.json() == {"error": "forbidden"}


async def test_mail_never_sends_new_address_token_to_previous_address(client, configured, monkeypatch):
    original = web_auth.issue_token
    def changed(*args, **kwargs):
        web_auth.upsert_identity(configured._config.db_path, "bob", "changed@example.com")
        return original(*args, **kwargs)
    sent = []
    monkeypatch.setattr(web_auth, "issue_token", changed)
    monkeypatch.setattr(configured.web_auth_mail, "send_auth_email", lambda *args: sent.append(args) or True)
    response = await client.post("/istota/api/admin/users/bob/reset")
    assert response.status_code == 400
    assert not sent


@pytest.mark.parametrize("actions", [("disable", "disable"), ("disable", "remove")])
def test_concurrent_last_admin_changes(configured, actions):
    path = configured._config.db_path
    def change(item):
        name, action = item
        try:
            if action == "disable":
                web_auth.set_disabled(path, name, True, protected_admins={"alice", "bob"})
            else:
                web_auth.delete_identity(path, name, protected_admins={"alice", "bob"})
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(change, zip(("alice", "bob"), actions))) == [False, True]
    assert sum(not i.disabled for i in web_auth.list_identities(path)) == 1


async def test_disable_enable_and_invalid_boolean(client, configured):
    for value in ("false", 1, None):
        response = await client.post("/istota/api/admin/users/bob/disable", json={"disabled": value})
        assert response.status_code == 400
    for disabled in (True, False):
        response = await client.post("/istota/api/admin/users/bob/disable", json={"disabled": disabled})
        assert response.status_code == 200
        assert web_auth.get_identity(configured._config.db_path, "bob").disabled is disabled
