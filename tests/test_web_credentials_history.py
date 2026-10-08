"""History and sensitive actions through the real authenticated web seam."""
import re

# Shared pytest fixtures are intentionally rebound as test parameters.
# ruff: noqa: F811
import pytest
from istota import db, user_profiles
from istota.credentials import generated
from istota.webui import auth
from tests.test_web_app import app, client, config  # noqa: F401
from tests.test_web_credentials_local import BASE, ORIGIN, signed_client  # noqa: F401

@pytest.fixture
async def mail(config, monkeypatch, signed_client):
    import istota.webui.app as mod
    user_profiles.ensure_profile(config.db_path, "alice", display_name="Alice")
    auth.upsert_identity(config.db_path, "alice", "alice@example.com")
    await signed_client.get("/istota/callback", follow_redirects=False)
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE user_profiles SET email_addresses=? WHERE user_id='alice'", ('["different@example.net"]',))
    sent = []
    monkeypatch.setattr(mod.web_auth_mail, "send_auth_email", lambda *args: sent.append(args))
    return sent

async def step_up(client, mail, action):
    response = await client.post("/istota/api/settings/step-up", json={"action": action}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    code = re.search(r"(?<![0-9])[0-9]{6}(?![0-9])", mail[-1][2])[0]
    return {"step_up": {"request_id": response.json()["request_id"], "code": code}}

async def test_history_delete_restore_purge_and_activity(signed_client, config, mail, caplog):
    body = {"name": "example", "value": "SENTINEL-7f3a-history", "username": "alice", "url": "example.com"}
    assert (await signed_client.post(BASE, json=body, headers=ORIGIN)).status_code == 200
    assert (await signed_client.delete(f"{BASE}/example/value", headers=ORIGIN)).status_code == 200
    history = await signed_client.get(f"{BASE}/example/history", headers=ORIGIN)
    assert history.status_code == 200
    assert "SENTINEL" not in history.text
    history_id = history.json()[0]["id"]
    deleted = await signed_client.get(f"{BASE}/deleted", headers=ORIGIN)
    assert deleted.json()[0]["name"] == "example"
    restore = f"{BASE}/history/{history_id}/restore"
    assert (await signed_client.post(restore, json={}, headers=ORIGIN)).status_code == 403
    result = await signed_client.post(restore, json=await step_up(signed_client, mail, "history_restore"), headers=ORIGIN)
    assert result.status_code == 200, result.text
    assert set(result.json()["restored"]) == {"example", "example_username", "example_url"}
    assert (await signed_client.get(f"{BASE}/deleted", headers=ORIGIN)).json() == []
    assert (await signed_client.request("DELETE", f"{BASE}/example/history", json={}, headers=ORIGIN)).status_code == 403
    result = await signed_client.request("DELETE", f"{BASE}/example/history", json=await step_up(signed_client, mail, "history_purge"), headers=ORIGIN)
    assert result.status_code == 200
    activity = await signed_client.get(f"{BASE}/activity", headers=ORIGIN)
    assert {r["action"] for r in activity.json()} == {"delete", "restore", "history_purge"}
    assert "SENTINEL" not in activity.text + caplog.text
    with db.get_db(config.db_path) as conn:
        assert "SENTINEL" not in str([tuple(r) for r in conn.execute("SELECT * FROM credential_audit")])

async def test_reveal_on_nextcloud_needs_identity_and_step_up(signed_client, config, monkeypatch):
    with db.get_db(config.db_path) as conn:
        generated.create(conn, "alice", name="generated_example", username="alice", password="fixture", url="example.com")
        generated.set_recovery(conn, "alice", "generated_example", "SENTINEL-7f3a-code")
    response = await signed_client.post(f"{BASE}/generated_example/recovery", json={"confirm": True}, headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["reason"] == "unavailable"
    response = await signed_client.post("/istota/api/settings/step-up", json={"action": "export"}, headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["detail"] == "step_up_unavailable"

async def test_request_masks_address_and_sends_only_to_identity(signed_client, config, mail):
    for _ in range(config.web.auth_mail_link_max_email + 1):
        response = await signed_client.post("/istota/api/settings/step-up", json={"action": "export"}, headers=ORIGIN)
        assert response.status_code == 200
        assert set(response.json()) == {"request_id", "expires_at", "email_hint"}
        assert response.json()["email_hint"] == "a•••@example.com"
        assert "alice@example.com" not in response.text
    assert len(mail) == config.web.auth_mail_link_max_email
    assert all(args[1] == "alice@example.com" for args in mail)
    assert (await signed_client.post("/istota/api/settings/step-up", json={"action": "export"})).status_code == 403



async def test_generated_retirement_restores_the_whole_batch_from_a_member(signed_client, config, mail):
    from istota.credentials import store
    from istota.credentials.broker.bindings import credential_name, get_binding
    with db.get_db(config.db_path) as conn:
        generated.create(conn, "alice", name="generated_example", username="alice", password="SENTINEL-7f3a-password", url="https://example.com")
        generated.set_otp(conn, "alice", "generated_example", "JBSWY3DPEHPK3PXP")
        generated.set_recovery(conn, "alice", "generated_example", "SENTINEL-7f3a-recovery")
    response = await signed_client.delete(f"{BASE}/generated_example_recovery/value", headers=ORIGIN)
    assert response.status_code == 200
    with db.get_db(config.db_path) as conn:
        history_id = conn.execute("SELECT id FROM secrets_history WHERE key='generated_example_recovery'").fetchone()[0]
    response = await signed_client.post(f"{BASE}/history/{history_id}/restore", json=await step_up(signed_client, mail, "history_restore"), headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert set(response.json()["restored"]) == set(generated.entry_names("generated_example").values())
    with db.get_db(config.db_path) as conn:
        assert credential_name(conn, "alice", "generated_example_recovery") == "generated_example"
        assert get_binding(conn, "alice", "generated_example_recovery")["kind"] == "recovery"
        assert generated.read_recovery(conn, "alice", "generated_example") == "SENTINEL-7f3a-recovery"
        assert "SENTINEL" not in str([tuple(r) for r in conn.execute("SELECT * FROM credential_audit")])
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_example") == "SENTINEL-7f3a-password"


async def test_step_up_mail_work_is_deferred_until_after_response(signed_client, config, mail, monkeypatch):
    import istota.webui.app as mod
    tasks = []
    class Deferred:
        def __init__(self, function, *args):
            tasks.append((function, args))
        async def __call__(self):
            pass
    monkeypatch.setattr(mod, "BackgroundTask", Deferred)
    for budget_used in (False, True):
        if budget_used:
            with db.get_db(config.db_path) as conn:
                for _ in range(config.web.auth_mail_link_max_email):
                    conn.execute("INSERT INTO web_auth_attempts (kind, key) VALUES ('mail_link', 'alice@example.com')")
        response = await signed_client.post("/istota/api/settings/step-up", json={"action": "export"}, headers=ORIGIN)
        assert response.status_code == 200
        assert set(response.json()) == {"request_id", "expires_at", "email_hint"}
        assert not mail
        with db.get_db(config.db_path) as conn:
            assert conn.execute("SELECT count(*) FROM web_auth_step_ups").fetchone()[0] == 0
    assert len(tasks) == 2
