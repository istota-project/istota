"""Downloads require a fresh code and announce every successful export."""
from istota.credentials import kdbx_import as credential_read
import base64
import io
import json

from pykeepass import PyKeePass
from istota import db
from istota.credentials import store, names as vault
from tests.test_kdbx_export import cheap, keyfile, PASSWORD  # noqa: F401
from tests.test_web_credentials_local import app, client, config, signed_client, ORIGIN  # noqa: F401
from tests.test_web_credentials_history import mail, step_up  # noqa: F401

# Fixtures are shared with the other credential web tests.
# ruff: noqa: F811
BASE = "/istota/api/settings/credentials/export"


async def test_export_step_up_download_and_notice(signed_client, config, mail, cheap, monkeypatch, caplog):
    store.set_secret(config.db_path, "alice", "vault_entries", "portal", "fixture-value")
    monkeypatch.setattr(cheap, "generate_export_password", lambda: PASSWORD)
    assert (await signed_client.post(BASE, json={}, headers=ORIGIN)).status_code == 403
    proof = await step_up(signed_client, mail, "export")
    response = await signed_client.post(BASE, json={**proof, "keyfile": keyfile().decode()}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    body = response.json()
    kp = PyKeePass(io.BytesIO(base64.b64decode(body["file"])), password=body["password"], keyfile=io.BytesIO(keyfile()))
    assert kp.entries[0].password == "fixture-value"
    assert body["filename"].endswith(".kdbx")
    assert mail[-1][1] == "alice@example.com"
    assert "exported" in mail[-1][2]
    assert PASSWORD not in str(mail) + caplog.text
    with db.get_db(config.db_path) as conn:
        assert PASSWORD not in "\n".join(conn.iterdump())
        assert conn.execute("SELECT count(*) FROM credential_audit WHERE action='export'").fetchone()[0] == 1
        notice = conn.execute("SELECT * FROM notifications WHERE dedup_key LIKE 'credentials-exported:%'").fetchone()
        assert notice is not None
    assert (await signed_client.post(BASE, json=proof, headers=ORIGIN)).status_code == 403


async def test_limit_consumes_code(signed_client, config, mail, cheap):
    config.web.auth_mail_link_max_email = 10
    store.set_secret(config.db_path, "alice", "vault_entries", "portal", "fixture-value")
    for _ in range(3):
        proof = await step_up(signed_client, mail, "export")
        response = await signed_client.post(BASE, json=proof, headers=ORIGIN)
        assert response.status_code == 200, response.text
    proof = await step_up(signed_client, mail, "export")
    response = await signed_client.post(BASE, json=proof, headers=ORIGIN)
    assert response.status_code == 429
    assert response.json()["detail"] == "export_rate_limited"
    assert response.json()["next_allowed_at"]
    assert (await signed_client.post(BASE, json=proof, headers=ORIGIN)).status_code == 403


async def test_empty_body_validation_origin_isolation_and_semaphore(signed_client, config, mail, cheap, monkeypatch):
    assert (await signed_client.post(BASE, json={}, headers={})).status_code == 403
    assert (await signed_client.post(BASE, json={"password": PASSWORD}, headers=ORIGIN)).status_code == 400
    assert (await signed_client.post(BASE, json={"keyfile": "invalid"}, headers=ORIGIN)).status_code == 400
    response = await signed_client.post(BASE, json=await step_up(signed_client, mail, "export"), headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["detail"] == "export_empty"
    class Busy:
        def acquire(self, timeout):
            assert timeout == 30
            return False
    monkeypatch.setattr(cheap, "EXPORT_SLOT", Busy())
    response = await signed_client.post(BASE, json=await step_up(signed_client, mail, "export"), headers=ORIGIN)
    assert response.status_code == 503
    monkeypatch.setattr(vault, "vault_isolation_refusal", lambda *args: "isolation-required")
    assert (await signed_client.post(BASE, json={}, headers=ORIGIN)).status_code == 403
    signed_client.cookies.clear()
    assert (await signed_client.post(BASE, json={}, headers=ORIGIN)).status_code == 401


async def test_invalid_unicode_keyfile_is_safe(signed_client, mail, cheap):
    response = await signed_client.post(BASE, content=json.dumps({"keyfile": "\ud800"}), headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["field"] == "keyfile"


async def test_library_missing_with_keyfile_is_unavailable(signed_client, mail, cheap, monkeypatch):
    def missing(value):
        raise credential_read.VaultLibraryMissing("missing")
    monkeypatch.setattr(cheap, "keyfile_refusal", missing)
    response = await signed_client.post(BASE, json={"keyfile": "fixture"}, headers=ORIGIN)
    assert response.status_code == 503


async def test_notice_failure_rolls_back_audit(signed_client, config, mail, cheap, monkeypatch, caplog):
    from istota.notifications.resolvers import task_alert
    store.set_secret(config.db_path, "alice", "vault_entries", "portal", "fixture")
    monkeypatch.setattr(cheap, "generate_export_password", lambda: PASSWORD)
    def fail(*args, **kwargs):
        raise RuntimeError(PASSWORD)
    monkeypatch.setattr(task_alert, "write", fail)
    response = await signed_client.post(BASE, json=await step_up(signed_client, mail, "export"), headers=ORIGIN)
    assert response.status_code == 500
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM credential_audit WHERE action=\x27export\x27").fetchone()[0] == 0
    assert PASSWORD not in response.text + caplog.text + str(mail)
