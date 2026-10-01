"""Credentials added in Istota through the settings routes.

The write routes carry a secret in their body, so most of this file is about
where that value must never appear: a response, a log line, an error detail.
"""

import json
import logging
from unittest.mock import AsyncMock

import pytest

from istota import db, secrets_store
from istota.credential_broker import grants
from istota.credential_broker.bindings import get_binding, parse_binding
from tests.test_web_app import app, client, config  # noqa: F401 -- shared web fixtures

BASE = "/istota/api/settings/credentials"
ORIGIN = {"Origin": "https://example.com"}
MARKER = "fixture-marker-value"


@pytest.fixture
async def signed_client(client, monkeypatch, config):  # noqa: F811 -- imported fixtures
    import istota.web_app as mod
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    # The shared fixture is multi-user and unsandboxed, which the store's
    # isolation gate refuses without the operator's opt-in.
    config.security.allow_unsandboxed_multi_user_vaults = True
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": "alice"})
    await client.get("/istota/callback", follow_redirects=False)
    return client


def _body(**overrides):
    body = {"name": "openrouter_key", "value": MARKER, "username": "", "url": "openrouter.ai",
            "extra_hosts": "", "headers": "", "revealable": False}
    body.update(overrides)
    return body


def _update(**overrides):
    body = {"url": "openrouter.ai", "extra_hosts": "", "headers": "", "revealable": False}
    body.update(overrides)
    return body


def _rows(config):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        secrets = conn.execute(
            "SELECT COUNT(*) FROM secrets WHERE user_id='alice' AND service='vault_entries'"
        ).fetchone()[0]
        bound = conn.execute(
            "SELECT COUNT(*) FROM credential_bindings WHERE user_id='alice'"
        ).fetchone()[0]
    return secrets, bound


async def test_create_list_update_and_delete(signed_client, config):  # noqa: F811
    response = await signed_client.post(
        BASE, json=_body(username="fixture-user-name",
                         access={"scope_mode": "all", "rooms": [], "allow_scheduled": False,
                                 "allow_http": False}),
        headers=ORIGIN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["ok"] is True
    assert body["name"] == "openrouter_key"
    assert body["username_name"] == "openrouter_key_username"
    assert body["url_name"] == "openrouter_key_url"
    assert body["grant"]["scope_mode"] == "all"
    assert MARKER not in response.text

    listing = await signed_client.get(BASE)
    data = listing.json()
    row = next(c for c in data["credentials"] if c["name"] == "openrouter_key")
    assert row["source"] == "local"
    assert row["hosts"] == ["openrouter.ai"]
    assert row["url"] == "openrouter.ai"
    assert row["username_set"] is True
    assert row["grant"]["scope_mode"] == "all"
    assert data["can_add"] is True
    assert data["add_blocked_reason"] == ""
    assert MARKER not in listing.text
    assert "fixture-user-name" not in listing.text

    # Value and username omitted: both kept, metadata replaced.
    response = await signed_client.patch(
        BASE + "/openrouter_key/local", json=_update(url="api.openrouter.ai", revealable=True),
        headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json()["username_name"] == "openrouter_key_username"
    get = secrets_store.get_secret
    assert get(config.db_path, "alice", "vault_entries", "openrouter_key") == MARKER
    assert get(config.db_path, "alice", "vault_entries", "openrouter_key_username") == "fixture-user-name"
    with db.get_db(config.db_path) as conn:
        assert get_binding(conn, "alice", "openrouter_key_username")["hosts"] == ["api.openrouter.ai"]
        assert get_binding(conn, "alice", "openrouter_key")["revealable"] is True

    # A new value replaces it; an empty username removes that row.
    response = await signed_client.patch(
        BASE + "/openrouter_key/local",
        json=_update(value="fixture-replacement", username="", url="api.openrouter.ai"),
        headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert "fixture-replacement" not in response.text
    assert get(config.db_path, "alice", "vault_entries", "openrouter_key") == "fixture-replacement"
    assert get(config.db_path, "alice", "vault_entries", "openrouter_key_username") is None
    row = next(c for c in (await signed_client.get(BASE)).json()["credentials"]
               if c["name"] == "openrouter_key")
    assert row["username_set"] is False

    response = await signed_client.delete(BASE + "/openrouter_key/value", headers=ORIGIN)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "deleted": True}
    assert _rows(config) == (0, 0)
    with db.get_db(config.db_path) as conn:
        assert grants.get_grant(conn, "alice", "openrouter_key") is None
    assert (await signed_client.get(BASE)).json()["credentials"] == []


async def test_create_without_access_has_no_grant(signed_client, config):  # noqa: F811
    response = await signed_client.post(BASE, json={"name": "plain", "value": MARKER},
                                        headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json() == {"ok": True, "name": "plain", "username_name": None,
                               "url_name": None, "grant": None}
    row = (await signed_client.get(BASE)).json()["credentials"][0]
    assert row["source"] == "local"
    assert row["hosts"] == []
    assert row["url"] == ""
    assert row["username_set"] is False


@pytest.mark.parametrize("body, field", [
    (_body(name="OpenRouter Key"), "name"),
    (_body(name="generated_key"), "name"),
    (_body(value=" padded "), "value"),
    (_body(url="ftp://openrouter.ai"), "url"),
    (_body(url="", access={"scope_mode": "all"}), "access"),
    (_body(access={"scope_mode": "rooms", "rooms": ["foreign"]}), "access"),
    (_body(access={"scope_mode": "all", "allow_scheduled": "yes"}), "access"),
])
async def test_refusals_name_their_field_and_write_nothing(signed_client, config, body, field):  # noqa: F811
    response = await signed_client.post(BASE, json=body, headers=ORIGIN)
    assert response.status_code == 400, response.text
    assert response.json()["field"] == field
    assert _rows(config) == (0, 0)
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM credential_grants").fetchone()[0] == 0


async def test_access_to_own_room_is_accepted(signed_client, config):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Personal")
    response = await signed_client.post(
        BASE, json=_body(access={"scope_mode": "rooms", "rooms": [room.token]}), headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json()["grant"]["rooms"] == [room.token]


def _secret_cases():
    over_cap = MARKER + "x" * 9000
    return [
        ("success", json.dumps(_body())),
        ("non-object", json.dumps([MARKER])),
        ("number value", json.dumps({"name": "numeric", "value": 918273645546372})),
        ("unknown key", json.dumps({**_body(name="other"), "extra": MARKER})),
        ("secret as key", json.dumps({**_body(name="other"), MARKER: "x"})),
        ("over cap", json.dumps(_body(name="big", value=over_cap))),
        ("not json", MARKER + "{"),
    ]


async def test_the_secret_never_appears_in_a_response_or_a_log(signed_client, caplog):
    caplog.set_level(logging.DEBUG)
    texts = []
    for label, raw in _secret_cases():
        response = await signed_client.post(
            BASE, content=raw, headers={**ORIGIN, "Content-Type": "application/json"})
        assert response.status_code in (200, 400), (label, response.status_code)
        texts.append((label, response.text))
    # A collision on the name the success case created.
    response = await signed_client.post(BASE, json=_body(), headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["field"] == "name"
    texts.append(("collision", response.text))
    response = await signed_client.patch(
        BASE + "/openrouter_key/local", json=_update(value=MARKER + " "), headers=ORIGIN)
    assert response.status_code == 400
    texts.append(("update refusal", response.text))
    for label, text in texts:
        assert MARKER not in text, label
        assert "918273645546372" not in text, label
    assert MARKER not in caplog.text
    assert "918273645546372" not in caplog.text


async def test_unexpected_errors_are_logged_by_type_only(signed_client, monkeypatch, caplog):
    import istota.local_credentials as local

    def boom(*args, **kwargs):
        raise RuntimeError(MARKER)

    monkeypatch.setattr(local, "create", boom)
    caplog.set_level(logging.DEBUG)
    response = await signed_client.post(BASE, json=_body(), headers=ORIGIN)
    assert response.status_code == 500
    assert response.json()["detail"] == "the credential could not be stored; see the daemon log"
    assert "RuntimeError" in caplog.text
    assert MARKER not in caplog.text
    assert MARKER not in response.text


async def test_writes_need_origin_login_and_a_bounded_body(signed_client, client):  # noqa: F811
    assert (await signed_client.post(BASE, json=_body())).status_code == 403
    assert (await signed_client.patch(BASE + "/x/local", json=_update())).status_code == 403
    big = json.dumps(_body(value="x" * (70 * 1024)))
    response = await signed_client.post(
        BASE, content=big, headers={**ORIGIN, "Content-Type": "application/json"})
    assert response.status_code == 413
    signed_client.cookies.clear()
    assert (await signed_client.post(BASE, json=_body(), headers=ORIGIN)).status_code == 401
    assert (await signed_client.patch(BASE + "/x/local", json=_update(),
                                      headers=ORIGIN)).status_code == 401


async def test_isolation_refusal_closes_every_write(signed_client, config, monkeypatch):  # noqa: F811
    from istota import secrets_vault
    secrets_store.set_secret(config.db_path, "alice", "vault_entries", "portal", MARKER,
                             binding={**parse_binding("https://portal.example", {}, [],
                                                      source="local"), "credential": "portal"})
    monkeypatch.setattr(secrets_vault, "vault_isolation_refusal",
                        lambda cfg, user: secrets_vault.VAULT_ISOLATION_REASON)
    for method, path, kwargs in [("post", BASE, {"json": _body()}),
                                 ("patch", BASE + "/portal/local", {"json": _update()}),
                                 ("delete", BASE + "/portal/value", {})]:
        response = await getattr(signed_client, method)(path, headers=ORIGIN, **kwargs)
        assert response.status_code == 403, method
        assert response.json()["detail"] == secrets_vault.VAULT_ISOLATION_REASON
    assert secrets_store.get_secret(config.db_path, "alice", "vault_entries", "portal") == MARKER
    data = (await signed_client.get(BASE)).json()
    assert data["can_add"] is False
    assert data["add_blocked_reason"] == secrets_vault.VAULT_ISOLATION_REASON


async def test_patch_refuses_a_keepassxc_credential(signed_client, config):  # noqa: F811
    secrets_store.upsert_secret(config.db_path, "alice", "vault_entries", "portal", MARKER,
                                binding=parse_binding("https://portal.example", {}, []))
    response = await signed_client.patch(BASE + "/portal/local", json=_update(), headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["field"] == "name"
    assert secrets_store.get_secret(config.db_path, "alice", "vault_entries", "portal") == MARKER
    row = (await signed_client.get(BASE)).json()["credentials"][0]
    assert row["source"] == "vault"
    assert "url" not in row
    assert "username_set" not in row


async def test_patch_requires_the_metadata_fields(signed_client):
    await signed_client.post(BASE, json=_body(), headers=ORIGIN)
    response = await signed_client.patch(BASE + "/openrouter_key/local", json={"value": None},
                                         headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["field"] == "url"
