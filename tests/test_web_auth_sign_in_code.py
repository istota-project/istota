"""Email sign-in by a six-digit code bound to the client that asked (ISSUE-574).

The code alone redeems nothing: it works only with the secret held by the client
that started the request, so a code read in another browser, another app or a
mail scanner cannot strand or hijack the sign-in.
"""

from concurrent.futures import ThreadPoolExecutor
import re
import sqlite3
from unittest.mock import MagicMock

import pytest

from istota import db, user_profiles, web_auth as auth

pytest.importorskip("fastapi")
from tests import test_web_auth_routes as routes

# The route fixtures, shared rather than copied.
configured = routes.configured
client = routes.client
csrf = routes.csrf
session_value = routes.session_value

SECRET = "client-held-secret-0123456789abcdef"


@pytest.fixture
def policy():
    return auth.Policy(12, 900, 10, 30, 604800, 3600, 600, 3)


@pytest.fixture
def identity(db_path):
    user_profiles.ensure_profile(db_path, "alice")
    return auth.upsert_identity(db_path, "alice", "alice@example.com")


def started(db_path, policy, email="alice@example.com", secret=SECRET):
    request_id = auth.start_sign_in(db_path, policy, email, secret)
    return request_id, auth.issue_sign_in_code_if_allowed(db_path, policy, request_id)


def test_code_redeems_only_with_the_starting_clients_secret(db_path, identity, policy):
    request_id, issued = started(db_path, policy)
    code, found = issued
    assert re.fullmatch(r"\d{6}", code)
    assert found == identity
    assert auth.redeem_sign_in_code(db_path, "another-clients-secret", code) == ("dead", None)
    status, result = auth.redeem_sign_in_code(db_path, SECRET, code)
    assert status == "ok"
    assert result == ("alice", "alice@example.com", identity.credential_epoch)
    assert auth.get_identity(db_path, "alice").last_login_at
    assert auth.redeem_sign_in_code(db_path, SECRET, code) == ("dead", None)


def test_wrong_secret_does_not_spend_an_attempt(db_path, identity, policy):
    request_id, (code, _) = started(db_path, policy)
    for _ in range(10):
        assert auth.redeem_sign_in_code(db_path, "wrong", "000000")[0] == "dead"
    assert auth.redeem_sign_in_code(db_path, SECRET, code)[0] == "ok"


def test_five_wrong_codes_kill_the_request(db_path, identity, policy):
    request_id, (code, _) = started(db_path, policy)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(auth.SIGN_IN_CODE_ATTEMPTS - 1):
        assert auth.redeem_sign_in_code(db_path, SECRET, wrong) == ("bad", None)
    assert auth.redeem_sign_in_code(db_path, SECRET, wrong) == ("dead", None)
    assert auth.redeem_sign_in_code(db_path, SECRET, code) == ("dead", None)


def test_spaces_and_hyphens_in_a_typed_code_are_ignored(db_path, identity, policy):
    request_id, (code, _) = started(db_path, policy)
    assert auth.redeem_sign_in_code(db_path, SECRET, f" {code[:3]}-{code[3:]} ")[0] == "ok"


def test_unknown_address_gets_no_code_and_behaves_like_a_wrong_one(db_path, identity, policy):
    request_id, issued = started(db_path, policy, "unknown@example.com")
    assert issued is None
    for _ in range(auth.SIGN_IN_CODE_ATTEMPTS - 1):
        assert auth.redeem_sign_in_code(db_path, SECRET, "123456") == ("bad", None)
    assert auth.redeem_sign_in_code(db_path, SECRET, "123456") == ("dead", None)


def test_expired_request_is_dead(db_path, identity, policy):
    request_id, (code, _) = started(db_path, policy)
    with db.get_db(db_path) as conn:
        conn.execute("UPDATE web_auth_sign_ins SET expires_at = datetime('now', '-1 second')")
    assert auth.redeem_sign_in_code(db_path, SECRET, code) == ("dead", None)


@pytest.mark.parametrize("mutation", ["disabled", "email", "password", "removed", "orphan"])
def test_a_credential_change_after_the_code_was_sent_kills_it(db_path, identity, policy, mutation):
    request_id, (code, _) = started(db_path, policy)
    if mutation == "disabled":
        auth.set_disabled(db_path, "alice", True)
    elif mutation == "email":
        auth.upsert_identity(db_path, "alice", "other@example.com")
    elif mutation == "password":
        auth.set_password(db_path, "alice", "a long example passphrase")
    elif mutation == "removed":
        auth.delete_identity(db_path, "alice")
        auth.upsert_identity(db_path, "alice", "alice@example.com")
    elif mutation == "orphan":
        user_profiles.delete_profile(db_path, "alice")
    assert auth.redeem_sign_in_code(db_path, SECRET, code) == ("dead", None)


def test_success_spends_the_users_other_pending_requests(db_path, identity, policy):
    first, (first_code, _) = started(db_path, policy, secret="first-client-secret-value")
    second, (second_code, _) = started(db_path, policy, secret="second-client-secret-value")
    assert auth.redeem_sign_in_code(db_path, "second-client-secret-value", second_code)[0] == "ok"
    assert auth.redeem_sign_in_code(db_path, "first-client-secret-value", first_code) == ("dead", None)


def test_a_newer_code_for_the_same_client_retires_the_older_one(db_path, identity, policy):
    first, (old_code, _) = started(db_path, policy)
    second, (new_code, _) = started(db_path, policy)
    if old_code != new_code:
        assert auth.redeem_sign_in_code(db_path, SECRET, old_code) == ("bad", None)
    assert auth.redeem_sign_in_code(db_path, SECRET, new_code)[0] == "ok"


def test_asking_again_past_the_mail_budget_keeps_the_code_already_sent(db_path, identity, policy):
    """Review of ISSUE-574: a repeat request with no code to send must not strand the user."""
    for _ in range(2):
        auth.issue_mail_link_if_allowed(db_path, policy, identity.email, "reset")
    _, (code, _) = started(db_path, policy)
    _, issued = started(db_path, policy)
    assert issued is None
    assert auth.redeem_sign_in_code(db_path, SECRET, code)[0] == "ok"


def test_wrong_codes_are_capped_per_address_across_clients(db_path, identity, policy):
    """A guesser opens requests for the victim's address from their own browser.

    Without a per-address count only the mail budget would bound them.
    """
    for index in range(auth.SIGN_IN_CODE_DAILY_FAILURES // auth.SIGN_IN_CODE_ATTEMPTS):
        with db.get_db(db_path) as conn:
            conn.execute("DELETE FROM web_auth_attempts WHERE kind = 'mail_link'")
        guesser = f"guesser-secret-{index:02d}-0123456789"
        _, (code, _) = started(db_path, policy, secret=guesser)
        wrong = "000000" if code != "000000" else "111111"
        for _ in range(auth.SIGN_IN_CODE_ATTEMPTS):
            assert auth.redeem_sign_in_code(db_path, guesser, wrong)[1] is None
    with db.get_db(db_path) as conn:
        conn.execute("DELETE FROM web_auth_attempts WHERE kind = 'mail_link'")
    _, (code, _) = started(db_path, policy)
    assert auth.redeem_sign_in_code(db_path, SECRET, code) == ("dead", None)
    with db.get_db(db_path) as conn:
        conn.execute("UPDATE web_auth_attempts SET at = datetime('now', '-86401 seconds') WHERE kind = 'sign_in_code'")
        conn.execute("DELETE FROM web_auth_attempts WHERE kind = 'mail_link'")
    _, (code, _) = started(db_path, policy)
    assert auth.redeem_sign_in_code(db_path, SECRET, code)[0] == "ok"


def test_a_lifetime_of_zero_is_refused(db_path, policy):
    from dataclasses import replace

    with pytest.raises(ValueError):
        auth.start_sign_in(db_path, replace(policy, sign_in_code_ttl_seconds=0), "alice@example.com", SECRET)


def test_codes_share_the_per_address_mail_budget(db_path, identity, policy):
    assert auth.issue_mail_link_if_allowed(db_path, policy, identity.email, "reset")
    assert started(db_path, policy)[1]
    assert started(db_path, policy)[1]
    request_id, issued = started(db_path, policy)
    assert issued is None
    assert auth.attempts_in_window(db_path, "mail_link", identity.email, 3600) == 3


def test_a_code_is_minted_once_per_request(db_path, identity, policy):
    request_id, issued = started(db_path, policy)
    assert issued
    assert auth.issue_sign_in_code_if_allowed(db_path, policy, request_id) is None


def test_concurrent_redemption_signs_in_once(db_path, identity, policy):
    request_id, (code, _) = started(db_path, policy)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: auth.redeem_sign_in_code(db_path, SECRET, code), range(4)))
    assert sum(status == "ok" for status, _ in results) == 1


def test_sign_in_rolls_back_if_login_stamp_fails(db_path, identity, policy):
    request_id, (code, _) = started(db_path, policy)
    with db.get_db(db_path) as conn:
        conn.execute("CREATE TRIGGER reject_login BEFORE UPDATE OF last_login_at ON web_auth_identities "
                     "BEGIN SELECT RAISE(ABORT, 'test failure'); END")
    with pytest.raises(sqlite3.Error):
        auth.redeem_sign_in_code(db_path, SECRET, code)
    with db.get_db(db_path) as conn:
        conn.execute("DROP TRIGGER reject_login")
    assert auth.redeem_sign_in_code(db_path, SECRET, code)[0] == "ok"


def test_the_database_holds_neither_the_code_nor_the_secret(db_path, identity, policy):
    request_id, (code, _) = started(db_path, policy)
    with db.get_db(db_path) as conn:
        row = dict(conn.execute("SELECT * FROM web_auth_sign_ins").fetchone())
    assert code not in row.values() and SECRET not in row.values()
    assert row["code_hash"] and row["secret_hash"]


def test_operator_code_replaces_the_emailed_one_for_the_newest_request(db_path, identity, policy):
    request_id, (emailed, _) = started(db_path, policy)
    minted = auth.mint_sign_in_code(db_path, "alice")
    code = minted.code
    assert re.fullmatch(r"\d{6}", code) and minted.expires_at and minted.requested_at
    assert minted.pending == 1
    if code != emailed:
        assert auth.redeem_sign_in_code(db_path, SECRET, emailed) == ("bad", None)
    assert auth.redeem_sign_in_code(db_path, SECRET, code)[0] == "ok"


def test_operator_code_works_when_mail_never_went_out(db_path, identity, policy):
    request_id = auth.start_sign_in(db_path, policy, "alice@example.com", SECRET)
    code = auth.mint_sign_in_code(db_path, "alice").code
    assert auth.redeem_sign_in_code(db_path, SECRET, code)[0] == "ok"


def test_operator_code_needs_a_pending_request(db_path, identity, policy):
    with pytest.raises(ValueError, match="No pending sign-in"):
        auth.mint_sign_in_code(db_path, "alice")
    started(db_path, policy, "unknown@example.com")
    with pytest.raises(ValueError, match="No pending sign-in"):
        auth.mint_sign_in_code(db_path, "alice")


# --- routes -----------------------------------------------------------------

REQUEST = "/istota/auth/sign-in-code/request"
REDEEM = "/istota/auth/sign-in-code"


async def request_code(client, email="alice@example.com"):
    page = await client.get("/istota/login")
    return await client.post(REQUEST, data={"email": email, "csrf_token": csrf(page, REQUEST)})


@pytest.fixture
def mailbox(configured, monkeypatch):
    from istota.skills import email as mail

    configured._config.email.enabled = True
    sent = MagicMock()
    monkeypatch.setattr(mail, "send_email", sent)
    return sent


def emailed_code(mailbox):
    return re.search(r"^(\d{6})$", mailbox.call_args.kwargs["body"], re.M)[1]


async def test_login_page_offers_a_code_rather_than_a_link(client):
    page = await client.get("/istota/login")
    assert f'action="{REQUEST}"' in page.text
    assert "login-link" not in page.text


async def test_code_sign_in_end_to_end(client, configured, mailbox):
    response = await request_code(client)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'autocomplete="one-time-code"' in response.text
    code = emailed_code(mailbox)
    assert code in mailbox.call_args.kwargs["subject"]
    assert "http" not in mailbox.call_args.kwargs["body"]
    signed_in = await client.post(REDEEM, data={"code": code, "csrf_token": csrf(response, REDEEM)})
    assert signed_in.status_code == 302 and signed_in.headers["location"] == "/istota/"
    session = session_value(client)
    assert session["user"]["username"] == "alice"
    assert "sign_in" not in session
    assert (await client.get("/istota/api/me")).status_code == 200


async def test_the_code_does_not_work_in_another_browser(client, configured, mailbox):
    from httpx import ASGITransport, AsyncClient

    response = await request_code(client)
    code = emailed_code(mailbox)
    async with AsyncClient(transport=ASGITransport(app=configured.app),
                           base_url="https://example.com") as other:
        page = await other.get("/istota/login")
        other_form = await other.post(REQUEST, data={"email": "bob@example.com", "csrf_token": csrf(page, REQUEST)})
        stolen = await other.post(REDEEM, data={"code": code, "csrf_token": csrf(other_form, REDEEM)})
        assert stolen.status_code == 400
        assert (await other.get("/istota/api/me")).status_code == 401
    signed_in = await client.post(REDEEM, data={"code": code, "csrf_token": csrf(response, REDEEM)})
    assert signed_in.status_code == 302


async def test_wrong_code_reprompts_and_the_fifth_ends_the_request(client, configured, mailbox):
    response = await request_code(client)
    code = emailed_code(mailbox)
    wrong = "000000" if code != "000000" else "111111"
    token = csrf(response, REDEEM)
    for _ in range(auth.SIGN_IN_CODE_ATTEMPTS - 1):
        retry = await client.post(REDEEM, data={"code": wrong, "csrf_token": token})
        assert retry.status_code == 400 and "not accepted" in retry.text
        assert f'action="{REDEEM}"' in retry.text
    final = await client.post(REDEEM, data={"code": wrong, "csrf_token": token})
    assert final.status_code == 400 and "expired" in final.text
    assert "sign_in" not in session_value(client)
    assert (await client.post(REDEEM, data={"code": code, "csrf_token": token})).status_code == 400


async def test_request_responses_do_not_disclose_identity(client, configured, mailbox):
    known = await request_code(client)
    unknown = await request_code(client, "unknown@example.com")
    auth.set_disabled(configured._config.db_path, "alice", True)
    disabled = await request_code(client)
    strip = lambda response: re.sub(r'value="[^"]*"', "", response.text)  # noqa: E731
    assert strip(known) == strip(unknown) == strip(disabled)
    assert mailbox.call_count == 1


async def test_redeem_without_a_pending_request_is_refused(client):
    page = await client.get(REDEEM)
    assert page.status_code == 302 and page.headers["location"] == "/istota/login"


async def test_redeem_requires_csrf(client, configured, mailbox):
    await request_code(client)
    response = await client.post(REDEEM, data={"code": emailed_code(mailbox)})
    assert response.status_code == 403 and "form expired" in response.text
    assert (await client.get("/istota/api/me")).status_code == 401


@pytest.mark.parametrize("method,route", [("POST", REQUEST), ("GET", REDEEM), ("POST", REDEEM)])
async def test_code_routes_need_the_email_method(client, configured, method, route):
    configured._config.web.auth = ["nextcloud"]
    assert (await client.request(method, route)).status_code == 404


@pytest.mark.parametrize("method,route", [
    ("GET", "/istota/auth/login-link"), ("POST", "/istota/auth/login-link"),
    ("POST", "/istota/auth/login-link/request"), ("POST", "/istota/api/admin/users/alice/login-link"),
])
async def test_the_login_link_routes_are_gone(client, method, route):
    assert (await client.request(method, route)).status_code in (404, 405)


def test_operator_code_gets_a_full_set_of_tries_and_clears_the_daily_cap(db_path, identity, policy):
    _, (code, _) = started(db_path, policy)
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(auth.SIGN_IN_CODE_ATTEMPTS - 1):
        auth.redeem_sign_in_code(db_path, SECRET, wrong)
    with db.get_db(db_path) as conn:
        for _ in range(auth.SIGN_IN_CODE_DAILY_FAILURES):
            conn.execute("INSERT INTO web_auth_attempts (kind, key) VALUES ('sign_in_code', 'alice@example.com')")
    code = auth.mint_sign_in_code(db_path, "alice").code
    wrong = "000000" if code != "000000" else "111111"
    for _ in range(auth.SIGN_IN_CODE_ATTEMPTS - 1):
        assert auth.redeem_sign_in_code(db_path, SECRET, wrong) == ("bad", None)
    assert auth.redeem_sign_in_code(db_path, SECRET, code)[0] == "ok"
