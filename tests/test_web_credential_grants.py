"""Settings grant writes use the signed-in user and the normal CSRF gate."""

from unittest.mock import AsyncMock
import pytest
from istota import db, secrets_store
from istota.credential_broker.bindings import parse_binding
from tests.test_web_app import app, client, config  # noqa: F401 -- shared web fixtures


@pytest.fixture
async def signed_client(client, monkeypatch, config):  # noqa: F811 -- imported fixtures
    import istota.web_app as mod
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": "alice"})
    await client.get("/istota/callback", follow_redirects=False)
    secrets_store.upsert_secret(config.db_path, "alice", "vault_entries", "portal", "fixture-password",
                               binding=parse_binding("https://portal.example", {}, []))
    return client


async def test_settings_list_and_save_are_user_scoped(signed_client, config):  # noqa: F811 -- imported fixture
    base = "/istota/api/settings/credentials"
    response = await signed_client.get(base)
    assert response.status_code == 200
    body = response.json()
    assert body["credentials"][0]["name"] == "portal"
    assert body["credentials"][0]["grant"] is None
    assert "fixture-password" not in response.text
    assert body["grant_existing_available"] is True
    response = await signed_client.put(base + "/portal", json={}, headers={"Origin": "https://example.com"})
    assert response.status_code == 200
    assert response.json()["grant"]["allow_scheduled"] is False
    assert "methods" not in response.json()["grant"]
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT user_id FROM credential_grants").fetchone()[0] == "alice"
    response = await signed_client.put(base + "/portal", json={"scope_mode": "rooms", "rooms": ["foreign"]},
                                      headers={"Origin": "https://example.com"})
    assert response.status_code == 400
    response = await signed_client.delete(base + "/portal", headers={"Origin": "https://example.com"})
    assert response.status_code == 200


async def test_grant_mutations_require_csrf(signed_client):
    base = "/istota/api/settings/credentials"
    for method, suffix in [("put", "/portal"), ("delete", "/portal"), ("post", "/grant-existing")]:
        kwargs = {"json": {}} if method == "put" else {}
        response = await getattr(signed_client, method)(base + suffix, **kwargs)
        assert response.status_code == 403
    response = await signed_client.post(base + "/grant-existing", headers={"Origin": "https://example.com"})
    assert response.json()["count"] == 1
    assert (await signed_client.get(base)).json()["grant_existing_available"] is False


async def test_credentials_require_login(client):  # noqa: F811 -- imported fixture
    for method, suffix in [("get", ""), ("put", "/portal"), ("delete", "/portal"),
                           ("delete", "/portal/value"), ("post", "/grant-existing")]:
        kwargs = {"json": {}} if method == "put" else {}
        response = await getattr(client, method)("/istota/api/settings/credentials" + suffix,
                                                headers={"Origin": "https://example.com"}, **kwargs)
        assert response.status_code == 401


async def test_vault_bare_domain_sync_delete_and_resync(signed_client, config, tmp_path):  # noqa: F811
    from istota import secrets_vault
    from istota.credential_broker.bindings import get_binding
    from istota.credential_broker.grants import get_grant
    from tests.test_secrets_vault import _new_db, _read

    kp, path = _new_db(tmp_path)
    kp.add_entry(kp.root_group, "portal", "alice", "fixture-password", url="portal.example.com")
    kp.save()
    read, _ = _read(path)
    secrets_vault.apply_vault(config.db_path, "alice", read)
    secrets_vault.apply_vault(config.db_path, "bob", read)
    base = "/istota/api/settings/credentials"
    origin = {"Origin": "https://example.com"}
    entries = (await signed_client.get(base)).json()["credentials"]
    assert {entry["name"] for entry in entries} == {"portal"}
    assert all(entry["hosts"] == ["portal.example.com"] for entry in entries)
    assert secrets_store.get_secret(config.db_path, "alice", "vault_entries", "portal_url") == "portal.example.com"
    assert (await signed_client.put(base + "/portal", json={}, headers=origin)).status_code == 200

    response = await signed_client.delete(base + "/portal/value", headers=origin)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": True}
    assert secrets_store.get_secret(config.db_path, "alice", "vault_entries", "portal") is None
    assert secrets_store.get_secret(config.db_path, "bob", "vault_entries", "portal") == "fixture-password"
    with db.get_db(config.db_path) as conn:
        assert get_binding(conn, "alice", "portal") is None
        assert get_grant(conn, "alice", "portal") is None
    entries = (await signed_client.get(base)).json()["credentials"]
    assert {entry["name"] for entry in entries} == set()

    secrets_vault.apply_vault(config.db_path, "alice", read)
    entries = (await signed_client.get(base)).json()["credentials"]
    restored = next(entry for entry in entries if entry["name"] == "portal")
    assert restored["hosts"] == ["portal.example.com"]
    assert restored["grant"] is None


async def test_delete_unbound_credential_requires_csrf(signed_client, config):  # noqa: F811
    secrets_store.upsert_secret(config.db_path, "alice", "vault_entries", "stale", "fixture-password")
    url = "/istota/api/settings/credentials/stale/value"
    assert (await signed_client.delete(url)).status_code == 403
    assert secrets_store.secret_exists(config.db_path, "alice", "vault_entries", "stale")
    response = await signed_client.delete(url, headers={"Origin": "https://example.com"})
    assert response.status_code == 200
    assert response.json()["deleted"] is True
    assert not secrets_store.secret_exists(config.db_path, "alice", "vault_entries", "stale")


async def test_delete_cannot_remove_deployment_credentials(signed_client, config):  # noqa: F811
    from istota.credential_broker.bindings import get_binding
    config.developer.enabled = True
    config.developer.github_token = "fixture-token"
    base = "/istota/api/settings/credentials"
    await signed_client.get(base)
    response = await signed_client.delete(base + "/forge.github/value",
                                          headers={"Origin": "https://example.com"})
    assert response.status_code == 400
    assert config.developer.github_token == "fixture-token"
    with db.get_db(config.db_path) as conn:
        assert get_binding(conn, "alice", "forge.github") is not None


async def test_rooms_and_forge_identity_are_checked(signed_client, config):  # noqa: F811 -- imported fixture
    base = "/istota/api/settings/credentials"
    config.admin_users = {"bob"}
    config.developer.enabled = True
    config.developer.github_token = "fixture-token"
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Personal")
    data = (await signed_client.get(base)).json()
    assert [c["name"] for c in data["credentials"]] == ["portal"]
    assert data["rooms"] == [{"token": room.token, "name": "Personal"}]
    response = await signed_client.put(base + "/portal", json={"scope_mode": "rooms", "rooms": [room.token]},
                                      headers={"Origin": "https://example.com"})
    assert response.status_code == 200
    for payload in [{"methods": ["GET"]}, {"allow_scheduled": "false"}, {"user_id": "bob"}]:
        response = await signed_client.put(base + "/portal", json=payload, headers={"Origin": "https://example.com"})
        assert response.status_code == 400


async def test_deleted_room_can_be_removed_from_grant(signed_client, config):  # noqa: F811 -- imported fixture
    base = "/istota/api/settings/credentials"
    origin = {"Origin": "https://example.com"}
    with db.get_db(config.db_path) as conn:
        kept = db.create_web_chat_room(conn, "alice", "Personal")
        removed = db.create_web_chat_room(conn, "alice", "Old room")
    response = await signed_client.put(base + "/portal", headers=origin,
                                      json={"scope_mode": "rooms", "rooms": [kept.token, removed.token]})
    assert response.status_code == 200
    with db.get_db(config.db_path) as conn:
        db.delete_web_chat_room(conn, removed.id, "alice")
    data = (await signed_client.get(base)).json()
    assert data["rooms"] == [{"token": kept.token, "name": "Personal"}]
    assert removed.token in data["credentials"][0]["grant"]["rooms"]
    response = await signed_client.put(base + "/portal", headers=origin,
                                      json={"scope_mode": "rooms", "rooms": [kept.token]})
    assert response.status_code == 200
    assert response.json()["grant"]["rooms"] == [kept.token]


async def test_entry_fields_share_grant_and_http_override(signed_client, config, tmp_path):  # noqa: F811
    from istota import secrets_vault
    from istota.credential_broker import grants
    from tests.test_secrets_vault import _new_db, _read
    kp, path = _new_db(tmp_path)
    kp.add_entry(kp.root_group, "portal", "alice", "fixture-password",
                 url="http://192.0.2.10:8080/login")
    kp.save()
    read, _ = _read(path)
    secrets_vault.apply_vault(config.db_path, "alice", read)
    base = "/istota/api/settings/credentials"
    origin = {"Origin": "https://example.com"}
    entries = (await signed_client.get(base)).json()["credentials"]
    assert [entry["name"] for entry in entries] == ["portal"]
    assert entries[0]["hosts"] == ["http://192.0.2.10:8080"]
    response = await signed_client.put(base + "/portal", json={}, headers=origin)
    assert response.status_code == 200
    assert response.json()["grant"]["allow_http"] is False
    with db.get_db(config.db_path) as conn:
        task = db.create_task(conn, user_id="alice", prompt="test", source_type="talk",
                              conversation_token="room-a")
        grants.ensure_credential_grants(conn, task, "alice")
        assert grants.check_credential_grant(conn, task, "alice", "portal_username",
            "http://192.0.2.10:8080", "authorization") == "credential_https_required"
    response = await signed_client.put(base + "/portal", json={"allow_http": True}, headers=origin)
    assert response.status_code == 200
    with db.get_db(config.db_path) as conn:
        assert grants.check_credential_grant(conn, task, "alice", "portal_username",
            "http://192.0.2.10:8080", "authorization") == "credential_changed"
        task = db.create_task(conn, user_id="alice", prompt="test", source_type="talk",
                              conversation_token="room-a")
        assert set(grants.ensure_credential_grants(conn, task, "alice")) == {"portal"}
        assert grants.check_credential_grant(conn, task, "alice", "portal_username",
            "http://192.0.2.10:8080", "authorization") is None
    await signed_client.delete(base + "/portal", headers=origin)
    with db.get_db(config.db_path) as conn:
        assert grants.check_credential_grant(conn, task, "alice", "portal_username",
            "http://192.0.2.10:8080", "authorization") == "credential_not_granted"


async def test_grouping_uses_entry_identity_with_custom_fields_and_no_password(signed_client, config, tmp_path):  # noqa: F811
    from istota import secrets_vault
    from tests.test_secrets_vault import _new_db, _read
    kp, path = _new_db(tmp_path)
    entry = kp.add_entry(kp.root_group, "service", "alice", "", url="https://service.example")
    entry.set_custom_property("api_token", "fixture-token")
    kp.add_entry(kp.root_group, "other_username", "", "fixture-other", url="https://other.example")
    kp.save()
    read, _ = _read(path)
    secrets_vault.apply_vault(config.db_path, "alice", read)
    base = "/istota/api/settings/credentials"
    entries = (await signed_client.get(base)).json()["credentials"]
    assert {entry["name"] for entry in entries} == {"service", "other_username"}
    response = await signed_client.put(base + "/service", json={}, headers={"Origin": "https://example.com"})
    assert response.status_code == 200
    response = await signed_client.delete(base + "/service/value", headers={"Origin": "https://example.com"})
    assert response.json()["deleted"] is True
    assert not secrets_store.secret_exists(config.db_path, "alice", "vault_entries", "service_api_token")
    assert secrets_store.secret_exists(config.db_path, "alice", "vault_entries", "other_username")


async def test_vault_status_counts_entries_instead_of_fields(signed_client, config, tmp_path):  # noqa: F811
    from istota import secrets_vault
    from istota.config import UserConfig
    from tests.test_secrets_vault import _new_db, _read

    config.users["alice"] = UserConfig(vault_path="config/vault.kdbx")
    secrets_store.set_secret(config.db_path, "alice", "vault", "passphrase", "fixture-passphrase")
    kp, path = _new_db(tmp_path)
    kp.add_entry(kp.root_group, "portal", "alice", "fixture-password", url="https://portal.example")
    entry = kp.add_entry(kp.root_group, "service", "alice", "", url="https://service.example")
    entry.set_custom_property("api_token", "fixture-token")
    kp.add_entry(kp.root_group, "other_username", "", "fixture-other")
    kp.save()
    read, _ = _read(path)
    secrets_vault.apply_vault(config.db_path, "alice", read)
    secrets_store.set_secret(config.db_path, "bob", "vault_entries", "private", "fixture-private")

    response = await signed_client.get("/istota/api/settings/vault")
    assert response.status_code == 200
    body = response.json()
    assert body["entry_count"] == 3
    assert body["entry_names"] == ["other_username", "portal", "service"]
    assert body["entry_names_truncated"] is False
    assert "fixture-password" not in response.text
