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
