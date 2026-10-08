"""Backup recipient changes use the shared step-up seam."""
import pyrage
from istota import db
from tests.test_web_app import app, client, config  # noqa: F401
from tests.test_web_credentials_local import signed_client, ORIGIN  # noqa: F401
from tests.test_web_credentials_history import mail, step_up  # noqa: F401
# ruff: noqa: F811
BASE = "/istota/api/settings/credentials/backup"

async def test_recipient_set_clear_and_step_up(signed_client, config, mail):
    recipient = str(pyrage.x25519.Identity.generate().to_public())
    response = await signed_client.get(BASE, headers=ORIGIN)
    assert response.status_code == 200
    assert response.json()["recipient_suffix"] is None
    assert response.json()["available"] is True
    assert (await signed_client.put(BASE, json={"recipient": recipient}, headers=ORIGIN)).status_code == 403
    proof = await step_up(signed_client, mail, "backup_recipient")
    response = await signed_client.put(BASE, json={"recipient": recipient, **proof}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.json() == {"recipient_suffix": recipient[-8:]}
    assert response.headers["cache-control"] == "no-store"
    assert (await signed_client.put(BASE, json={"recipient": None, **proof}, headers=ORIGIN)).status_code == 403
    proof = await step_up(signed_client, mail, "backup_recipient")
    assert (await signed_client.put(BASE, json={"recipient": None, **proof}, headers=ORIGIN)).status_code == 200
    with db.get_db(config.db_path) as conn:
        assert {r[0] for r in conn.execute("SELECT action FROM credential_audit")} == {"backup_recipient_set", "backup_recipient_cleared"}
    assert (await signed_client.put(BASE, json={"recipient": recipient})).status_code == 403

async def test_invalid_recipient_and_isolation(signed_client, config, mail, monkeypatch):
    from istota.credentials import names as vault
    response = await signed_client.put(BASE, json={"recipient": "ssh-key", **await step_up(signed_client, mail, "backup_recipient")}, headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["field"] == "recipient"
    monkeypatch.setattr(vault, "vault_isolation_refusal", lambda *args: "isolation-required")
    assert (await signed_client.get(BASE, headers=ORIGIN)).status_code == 403
