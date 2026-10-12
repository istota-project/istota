"""Step-up through the web routes: an emailed code where the install can mail,
the account password where it cannot."""

import re

# Shared pytest fixtures are intentionally rebound as test parameters.
# ruff: noqa: F811

import pytest

pytest.importorskip("authlib")
pytest.importorskip("fastapi")

from istota import db
from istota.webui import auth as web_auth
from tests.test_web_auth_routes import (  # noqa: F401 -- fixtures
    PASSWORD, _cheap_password_hash, client, configured, sign_in,
)

ORIGIN = {"Origin": "https://example.com"}
STEP_UP = "/istota/api/settings/step-up"
RECOVERY = "/istota/api/settings/credentials/generated_acme/recovery"


@pytest.fixture
def mail(configured, monkeypatch):
    sent = []
    monkeypatch.setattr(configured.web_auth_mail, "send_auth_email", lambda *args: sent.append(args))
    return sent


@pytest.fixture
def recovery(configured, monkeypatch):
    from istota.credentials import generated

    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    with db.get_db(configured._config.db_path) as conn:
        generated.create(conn, "alice", name="generated_acme", username="alice@example.com",
                         password="fixture-password", url="https://acme.example")
        generated.set_recovery(conn, "alice", "generated_acme", "fixture-rc-1111-aaaa")


async def _start(client):
    response = await client.post(STEP_UP, json={"action": "recovery_reveal"}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    return response.json()


async def test_the_method_is_email_where_the_install_can_mail(client, configured, mail):
    configured._config.email.enabled = True
    assert (await sign_in(client)).status_code == 302
    body = await _start(client)
    assert body["method"] == "email"
    assert body["email_hint"] == "a•••@example.com"
    assert len(mail) == 1


async def test_the_method_is_password_without_mail_and_no_mail_is_attempted(client, configured, mail, monkeypatch):
    calls = []
    monkeypatch.setattr(configured, "_send_step_up", lambda *args: calls.append(args))
    assert (await sign_in(client)).status_code == 302
    body = await _start(client)
    assert body["method"] == "password"
    assert set(body) == {"method", "request_id", "expires_at"}
    assert mail == [] and calls == []


async def test_a_right_password_reveals_and_a_wrong_one_is_refused(client, configured, mail, recovery, caplog):
    assert (await sign_in(client)).status_code == 302
    body = await _start(client)
    wrong = await client.post(RECOVERY, json={"step_up": {"request_id": body["request_id"], "password": "not it"}},
                              headers=ORIGIN)
    assert wrong.status_code == 403
    assert wrong.json()["field"] == "password"
    assert wrong.json()["reason"] == "bad"
    assert "not it" not in wrong.text + caplog.text
    right = await client.post(RECOVERY, json={"step_up": {"request_id": body["request_id"], "password": PASSWORD}},
                              headers=ORIGIN)
    assert right.status_code == 200, right.text
    assert right.json()["codes"] == ["fixture-rc-1111-aaaa"]
    assert PASSWORD not in caplog.text


async def test_a_code_is_not_accepted_on_a_password_request(client, configured, recovery):
    assert (await sign_in(client)).status_code == 302
    body = await _start(client)
    with db.get_db(configured._config.db_path) as conn:
        conn.execute("UPDATE web_auth_step_ups SET code_hash=?",
                     (web_auth._code_digest(body["request_id"], "123456"),))
    response = await client.post(RECOVERY, json={"step_up": {"request_id": body["request_id"], "code": "123456"}},
                                 headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["field"] == "password"
    assert "fixture-rc" not in response.text


async def test_a_password_is_not_accepted_on_an_email_request(client, configured, mail, recovery):
    configured._config.email.enabled = True
    assert (await sign_in(client)).status_code == 302
    body = await _start(client)
    response = await client.post(RECOVERY, json={"step_up": {"request_id": body["request_id"], "password": PASSWORD}},
                                 headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["field"] == "code"
    code = re.search(r"[0-9]{6}", mail[-1][2])[0]
    response = await client.post(RECOVERY, json={"step_up": {"request_id": body["request_id"], "code": code}},
                                 headers=ORIGIN)
    assert response.status_code == 200, response.text


async def test_wrong_passwords_are_throttled_with_the_login_form(client, configured, recovery):
    configured._config.web.auth_throttle_max_email = 3
    assert (await sign_in(client)).status_code == 302
    body = await _start(client)
    reasons = []
    for _ in range(3):
        response = await client.post(RECOVERY, json={"step_up": {"request_id": body["request_id"], "password": "not it"}},
                                     headers=ORIGIN)
        reasons.append(response.json()["reason"])
    assert reasons == ["bad", "bad", "throttled"]
    response = await client.post(RECOVERY, json={"step_up": {"request_id": body["request_id"], "password": PASSWORD}},
                                 headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["reason"] == "throttled"


async def test_another_sessions_request_is_refused(client, configured, recovery):
    from httpx import ASGITransport, AsyncClient

    assert (await sign_in(client)).status_code == 302
    body = await _start(client)
    async with AsyncClient(transport=ASGITransport(app=configured.app), base_url="https://example.com") as other:
        assert (await sign_in(other)).status_code == 302
        response = await other.post(RECOVERY, json={"step_up": {"request_id": body["request_id"], "password": PASSWORD}},
                                    headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["reason"] == "dead"
    response = await client.post(RECOVERY, json={"step_up": {"request_id": body["request_id"], "password": PASSWORD}},
                                 headers=ORIGIN)
    assert response.status_code == 200, response.text


async def test_an_expired_request_is_refused(client, configured, recovery):
    assert (await sign_in(client)).status_code == 302
    body = await _start(client)
    with db.get_db(configured._config.db_path) as conn:
        conn.execute("UPDATE web_auth_step_ups SET expires_at=datetime('now', '-1 second')")
    response = await client.post(RECOVERY, json={"step_up": {"request_id": body["request_id"], "password": PASSWORD}},
                                 headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["reason"] == "dead"


async def test_no_password_and_no_mail_is_unavailable_with_a_way_out(client, configured, mail, recovery):
    assert (await sign_in(client)).status_code == 302
    # As after signing in with an emailed code: no password, same session.
    with db.get_db(configured._config.db_path) as conn:
        conn.execute("UPDATE web_auth_identities SET password_hash='' WHERE user_id='alice'")
    response = await client.post(STEP_UP, json={"action": "recovery_reveal"}, headers=ORIGIN)
    assert response.status_code == 403
    assert "password" in response.json()["detail"] and "mail" in response.json()["detail"]
    refused = await client.post(RECOVERY, json={"step_up": {"request_id": "x", "password": "x"}}, headers=ORIGIN)
    assert refused.status_code == 403
    assert refused.json()["reason"] == "unavailable"
    assert "mail" in refused.json()["detail"]
    assert mail == []


async def test_correct_passwords_leave_the_sign_in_budget_alone(client, configured, recovery):
    configured._config.web.auth_throttle_max_email = 3
    assert (await sign_in(client)).status_code == 302
    for _ in range(12):
        body = await _start(client)
        response = await client.post(RECOVERY, json={"step_up": {"request_id": body["request_id"], "password": PASSWORD}},
                                     headers=ORIGIN)
        assert response.status_code == 200, response.text
    assert (await sign_in(client)).status_code == 302


async def test_a_live_password_request_is_handed_back(client, configured):
    assert (await sign_in(client)).status_code == 302
    first, second = await _start(client), await _start(client)
    assert first == second
    with db.get_db(configured._config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM web_auth_step_ups").fetchone()[0] == 1


@pytest.mark.parametrize("email_sign_in", [True, False])
async def test_the_unavailable_advice_matches_the_sign_in_methods(client, configured, email_sign_in):
    if email_sign_in:
        assert (await sign_in(client)).status_code == 302
    else:
        configured._config.web.auth = ["nextcloud"]
        assert (await client.get("/istota/callback", follow_redirects=False)).status_code == 302
    with db.get_db(configured._config.db_path) as conn:
        conn.execute("UPDATE web_auth_identities SET password_hash='' WHERE user_id='alice'")
    detail = (await client.post(STEP_UP, json={"action": "recovery_reveal"}, headers=ORIGIN)).json()["detail"]
    refused = (await client.post(RECOVERY, json={"step_up": {"request_id": "x", "password": "x"}},
                                 headers=ORIGIN)).json()["detail"]
    assert detail == refused
    if email_sign_in:
        assert "istota auth reset alice --print-link" in detail
    else:
        assert "auth reset" not in detail
        assert "email sign-in" in detail and "mail" in detail
