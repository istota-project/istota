"""Generated credentials on the settings card (ISSUE-686): mirror state, toggles, retire."""

import pytest

from istota import db
from istota.credentials import generated
from istota.credentials import store as secrets_store
from tests.test_web_app import app, client, config  # noqa: F401 -- shared web fixtures
from tests.test_web_credentials_local import BASE, ORIGIN, signed_client  # noqa: F401

PASSWORD = "fixture-generated-password"


@pytest.fixture
def stored(config):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        generated.create(conn, "alice", name="generated_acme", username="alice@example.com",
                         password=PASSWORD, url="https://acme.example", mirror=False)


async def test_the_list_carries_mirror_state_and_never_the_value(signed_client, stored):  # noqa: F811
    response = await signed_client.get(BASE)
    data = response.json()
    row = next(c for c in data["credentials"] if c["name"] == "generated_acme")
    assert row["source"] == "generated"
    assert row["generated"] == {"mirror": False, "state": "off", "divergence": []}
    assert data["generated_default_mirror"] is True
    assert data["vault_enabled"] is False
    assert PASSWORD not in response.text


async def test_the_default_and_per_credential_toggles(signed_client, stored, config):  # noqa: F811
    response = await signed_client.put(f"{BASE}/generated/default", json={"mirror": False},
                                       headers=ORIGIN)
    assert response.status_code == 200, response.text
    with db.get_db(config.db_path) as conn:
        assert generated.default_mirror(conn, "alice") is False

    response = await signed_client.put(f"{BASE}/generated_acme/mirror", json={"mirror": True},
                                       headers=ORIGIN)
    assert response.status_code == 200, response.text
    with db.get_db(config.db_path) as conn:
        assert generated.mirror_state(conn, "alice", "generated_acme")["mirror"] is True

    assert (await signed_client.put(f"{BASE}/generated_acme/mirror", json={"mirror": "yes"},
                                    headers=ORIGIN)).status_code == 400
    assert (await signed_client.put(f"{BASE}/generated_acme/mirror", json={"mirror": True})
            ).status_code == 403


async def test_a_toggle_on_a_credential_istota_did_not_generate_is_refused(signed_client, config):  # noqa: F811
    from istota.credentials.broker.bindings import parse_binding
    secrets_store.set_secret(config.db_path, "alice", "vault_entries", "github", "token",
                             binding=parse_binding("github.com", {}, [], source="local"))
    response = await signed_client.put(f"{BASE}/github/mirror", json={"mirror": True}, headers=ORIGIN)
    assert response.status_code == 400
    response = await signed_client.post(f"{BASE}/github/remirror", headers=ORIGIN)
    assert response.status_code == 400


async def test_delete_retires_a_generated_credential(signed_client, stored, config):  # noqa: F811
    response = await signed_client.delete(f"{BASE}/generated_acme/value", headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json()["deleted"] is True
    remaining = secrets_store.get_service_secrets(config.db_path, "alice", "vault_entries")
    assert not [name for name in remaining if name.startswith("generated_acme")]


CODES = "fixture-rc-1111-aaaa\nfixture-rc-2222-bbbb"


@pytest.fixture
def with_codes(stored, config):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        generated.set_recovery(conn, "alice", "generated_acme", CODES)


async def test_the_list_says_codes_exist_and_never_carries_them(signed_client, with_codes):  # noqa: F811
    response = await signed_client.get(BASE)
    row = next(c for c in response.json()["credentials"] if c["name"] == "generated_acme")
    assert row["recovery"] is True
    assert row["otp"] is False
    assert "fixture-rc" not in response.text


async def test_showing_the_codes_needs_a_confirm_and_is_logged_by_name(
    signed_client, with_codes, caplog,  # noqa: F811
):
    url = f"{BASE}/generated_acme/recovery"
    for body in ({}, {"confirm": "yes"}, {"confirm": True, "extra": 1}):
        refused = await signed_client.post(url, json=body, headers=ORIGIN)
        assert refused.status_code == 400
        assert "fixture-rc" not in refused.text
    assert (await signed_client.post(url, json={"confirm": True})).status_code == 403
    with caplog.at_level("INFO", logger="istota.webui.app"):
        response = await signed_client.post(url, json={"confirm": True}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json() == {"codes": CODES}
    assert response.headers["cache-control"] == "no-store"
    assert "recovery codes viewed via settings" in caplog.text
    assert "fixture-rc" not in caplog.text


async def test_no_codes_or_not_generated_is_a_404(signed_client, stored, config):  # noqa: F811
    from istota.credentials.broker.bindings import parse_binding
    secrets_store.set_secret(config.db_path, "alice", "vault_entries", "github", "token",
                             binding=parse_binding("github.com", {}, [], source="local"))
    for name in ("generated_acme", "github", "absent"):
        response = await signed_client.post(f"{BASE}/{name}/recovery", json={"confirm": True},
                                            headers=ORIGIN)
        assert response.status_code == 404
        assert "token" not in response.text
