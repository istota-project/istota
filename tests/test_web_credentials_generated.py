"""Generated credentials on the settings card (ISSUE-686): mirror state, toggles, retire."""

import pytest

from istota import db
from istota.credentials import generated
from istota.credentials import store as secrets_store
from tests.test_web_app import app, client, config  # noqa: F401 -- shared web fixtures
from tests.test_web_credentials_local import BASE, ORIGIN, signed_client  # noqa: F401
from tests.test_web_credentials_history import mail, step_up  # noqa: F401

PASSWORD = "fixture-generated-password"


@pytest.fixture
def stored(config):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        generated.create(conn, "alice", name="generated_acme", username="alice@example.com",
                         password=PASSWORD, url="https://acme.example")


async def test_the_list_has_no_mirror_state_or_value(signed_client, stored):  # noqa: F811
    response = await signed_client.get(BASE)
    data = response.json()
    row = next(c for c in data["credentials"] if c["name"] == "generated_acme")
    assert row["source"] == "generated"
    assert "generated" not in row
    assert "generated_default_mirror" not in data
    assert "vault_enabled" not in data
    assert PASSWORD not in response.text


@pytest.mark.parametrize("method,path", [("put", "generated/default"),
    ("put", "generated_acme/mirror"), ("post", "generated_acme/remirror")])
async def test_mirror_routes_are_removed(signed_client, stored, method, path):  # noqa: F811
    response = await getattr(signed_client, method)(f"{BASE}/{path}", json={"mirror": True}, headers=ORIGIN)
    assert response.status_code in (404, 405)


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


async def test_showing_the_codes_needs_step_up_and_is_logged_by_name(
    signed_client, with_codes, caplog, mail,  # noqa: F811
):
    url = f"{BASE}/generated_acme/recovery"
    for body in ({}, {"confirm": "yes"}, {"confirm": True, "extra": 1}):
        refused = await signed_client.post(url, json=body, headers=ORIGIN)
        assert refused.status_code == 403
        assert "fixture-rc" not in refused.text
    assert (await signed_client.post(url, json={"confirm": True})).status_code == 403
    with caplog.at_level("INFO", logger="istota.webui.app"):
        response = await signed_client.post(url, json=await step_up(signed_client, mail, "recovery_reveal"), headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json() == {"codes": CODES.splitlines(), "spent": [], "format": "block"}
    assert response.headers["cache-control"] == "no-store"
    assert "recovery codes viewed via settings" in caplog.text
    assert "fixture-rc" not in caplog.text


async def test_no_codes_or_not_generated_is_a_404(signed_client, stored, config, mail):  # noqa: F811
    from istota.credentials.broker.bindings import parse_binding
    secrets_store.set_secret(config.db_path, "alice", "vault_entries", "github", "token",
                             binding=parse_binding("github.com", {}, [], source="local"))
    for name in ("generated_acme", "github", "absent"):
        response = await signed_client.post(f"{BASE}/{name}/recovery", json=await step_up(signed_client, mail, "recovery_reveal"),
                                            headers=ORIGIN)
        assert response.status_code == 404
        assert "token" not in response.text


async def test_reveal_reports_spent_codes(signed_client, with_codes, config, mail):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        generated.set_recovery(conn, "alice", "generated_acme", CODES, fmt="codes")
        conn.execute("UPDATE recovery_code_state SET spent='[0]'")
    response = await signed_client.post(f"{BASE}/generated_acme/recovery",
        json=await step_up(signed_client, mail, "recovery_reveal"), headers=ORIGIN)
    assert response.status_code == 200
    assert response.json() == {"codes": CODES.splitlines(), "spent": [0], "format": "codes"}
    listing = (await signed_client.get(BASE)).json()
    row = next(row for row in listing["credentials"] if row["name"] == "generated_acme")
    assert row["recovery_remaining"] == len(CODES.splitlines()) - 1
