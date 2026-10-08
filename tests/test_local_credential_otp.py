"""Local OTP writes through the settings routes and encrypted store."""

import base64

import pytest

from istota import db
from istota.credentials import local, store
from istota.credentials.broker import bindings
from istota.lib.totp import parse_otpauth, parse_user_input
from tests.test_web_credentials_local import (  # noqa: F401 -- shared web fixtures
    BASE, ORIGIN, app, client, config, signed_client,
)

SEED = base64.b32encode(b"local-otp-fixture").decode()
URI = "otpauth://totp/example?secret=" + SEED + "&algorithm=SHA256&digits=8"


def body(**overrides):
    return {"name": "acme", "value": "fixture-value", "url": "acme.example", **overrides}


def edit(**overrides):
    return {"url": "acme.example", "extra_hosts": "", "headers": "",
            "revealable": False, **overrides}


def seed_value(config):  # noqa: F811 -- shared web fixture
    return store.get_secret(config.db_path, "alice", "vault_entries", "acme_totp")


@pytest.mark.parametrize("otp", [SEED, URI])
async def test_create_and_list_never_return_seed(signed_client, config, otp):  # noqa: F811
    response = await signed_client.post(BASE, json=body(otp=otp), headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert parse_otpauth(seed_value(config)) == parse_user_input(otp)
    with db.get_db(config.db_path) as conn:
        binding = bindings.get_binding(conn, "alice", "acme_totp")
        assert binding["kind"] == "totp"
        assert binding["hosts"] == ["acme.example"]
        assert bindings.credential_name(conn, "alice", "acme_totp") == "acme"
    listing = await signed_client.get(BASE)
    row = listing.json()["credentials"][0]
    assert row["otp"] is True
    assert row["otp_set"] is True
    assert SEED not in response.text + listing.text
    assert "otpauth://" not in response.text + listing.text


async def test_three_state_update_rebinds_and_delete_cleans_seed(signed_client, config):  # noqa: F811
    assert (await signed_client.post(BASE, json=body(otp=SEED), headers=ORIGIN)).status_code == 200
    before = seed_value(config)
    for keep in ({}, {"otp": None}):
        response = await signed_client.patch(BASE + "/acme/local",
            json=edit(url="other.example", revealable=True, **keep), headers=ORIGIN)
        assert response.status_code == 200, response.text
        assert seed_value(config) == before
        with db.get_db(config.db_path) as conn:
            binding = bindings.get_binding(conn, "alice", "acme_totp")
            assert binding["kind"] == "totp"
            assert binding["hosts"] == ["other.example"]
    response = await signed_client.patch(BASE + "/acme/local", json=edit(otp=URI), headers=ORIGIN)
    assert response.status_code == 200
    assert parse_otpauth(seed_value(config)).digits == 8
    response = await signed_client.patch(BASE + "/acme/local", json=edit(otp="", url=""), headers=ORIGIN)
    assert response.status_code == 200
    assert seed_value(config) is None
    with db.get_db(config.db_path) as conn:
        assert bindings.get_binding(conn, "alice", "acme_totp") is None
    row = (await signed_client.get(BASE)).json()["credentials"][0]
    assert row["otp"] is False and row["otp_set"] is False
    assert (await signed_client.patch(BASE + "/acme/local", json=edit(otp=SEED), headers=ORIGIN)).status_code == 200
    assert (await signed_client.delete(BASE + "/acme/value", headers=ORIGIN)).status_code == 200
    assert seed_value(config) is None
    with db.get_db(config.db_path) as conn:
        assert bindings.get_binding(conn, "alice", "acme_totp") is None


async def test_seed_requires_bound_site_on_create_and_keep(signed_client, config):  # noqa: F811
    response = await signed_client.post(BASE, json=body(otp=SEED, url=""), headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["code"] == "otp_needs_site"
    assert seed_value(config) is None
    assert (await signed_client.post(BASE, json=body(otp=SEED), headers=ORIGIN)).status_code == 200
    response = await signed_client.patch(BASE + "/acme/local", json=edit(url=""), headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["code"] == "otp_needs_site"
    assert seed_value(config) is not None


@pytest.mark.parametrize("otp", ["invalid-otp-marker!", "otpauth://hotp/example?secret=" + SEED])
async def test_invalid_seed_response_and_logs_are_input_free(signed_client, config, caplog, otp):  # noqa: F811
    response = await signed_client.post(BASE, json=body(otp=otp), headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["field"] == "otp"
    assert response.json()["code"] == "invalid_otp"
    assert otp not in response.text + caplog.text
    assert seed_value(config) is None


async def test_totp_names_are_reserved_and_foreign_rows_survive(signed_client, config):  # noqa: F811
    assert (await signed_client.post(BASE, json=body(), headers=ORIGIN)).status_code == 200
    response = await signed_client.post(BASE, json=body(name="acme_totp"), headers=ORIGIN)
    assert response.status_code == 400
    binding = bindings.parse_binding("other.example", {}, [])
    store.set_secret(config.db_path, "alice", "vault_entries", "acme_totp", "foreign-value", binding=binding)
    response = await signed_client.patch(BASE + "/acme/local", json=edit(otp=SEED), headers=ORIGIN)
    assert response.status_code == 400
    assert seed_value(config) == "foreign-value"
    # Untouched foreign rows must neither block an edit nor show local two-factor.
    response = await signed_client.patch(BASE + "/acme/local", json=edit(), headers=ORIGIN)
    assert response.status_code == 200
    row = next(r for r in (await signed_client.get(BASE)).json()["credentials"] if r["name"] == "acme")
    assert row["otp_set"] is False


async def test_imported_group_label_uses_kind_not_name(signed_client, config):  # noqa: F811
    binding = {**bindings.parse_binding("acme.example", {}, []), "credential": "vault_entry", "kind": "totp"}
    store.set_secret(config.db_path, "alice", "vault_entries", "arbitrary_field", URI, binding=binding)
    row = (await signed_client.get(BASE)).json()["credentials"][0]
    assert row["name"] == "vault_entry" and row["otp"] is True
    # Imported entries are editable; this field is not the form's canonical OTP row.
    assert row["otp_set"] is False


def test_local_repr_hides_seed():
    assert SEED not in repr(local.LocalCredential(name="acme", value="fixture", otp=SEED))
