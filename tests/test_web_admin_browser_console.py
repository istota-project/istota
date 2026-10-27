"""The admin browser console: the ticket store, the relay counter and the
ticket route.

The route runs through the FastAPI app with a real database and a password
step-up; the websocket relay's cases are added beside these.
"""

import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

pytest.importorskip("authlib")
pytest.importorskip("fastapi")

from httpx import ASGITransport, AsyncClient

from istota import user_profiles
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.webui import admin_browser_console as console
from istota.webui import auth as web_auth
from tests.test_web_auth_routes import (  # noqa: F401 -- fixtures
    PASSWORD, _cheap_password_hash, session_value,
)

ORIGIN = {"Origin": "https://example.com"}
TICKET = "/istota/api/admin/browsers/console-ticket"
STEP_UP = "/istota/api/settings/step-up"


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


# ---- TicketStore ----


def test_a_ticket_redeems_once_for_its_target():
    store = console.TicketStore()
    ticket = store.mint("alice", "s1", "bob")
    assert store.redeem(ticket, "alice", "s1") == "bob"
    assert store.redeem(ticket, "alice", "s1") is None


def test_a_ticket_is_long_and_url_safe():
    ticket = console.TicketStore().mint("alice", "s1", "bob")
    assert len(ticket) >= 43
    assert set(ticket) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_")


@pytest.mark.parametrize("admin, session_id", [("dave", "s1"), ("alice", "s2")])
def test_a_ticket_from_another_admin_or_session_is_refused_and_deleted(admin, session_id):
    store = console.TicketStore()
    ticket = store.mint("alice", "s1", "bob")
    assert store.redeem(ticket, admin, session_id) is None
    assert store.redeem(ticket, "alice", "s1") is None


def test_an_expired_ticket_is_refused():
    clock = Clock()
    store = console.TicketStore(clock=clock)
    ticket = store.mint("alice", "s1", "bob")
    clock.now += console.TICKET_TTL_SECONDS - 0.5
    fresh = store.mint("alice", "s1", "carol")
    clock.now += 1
    assert store.redeem(ticket, "alice", "s1") is None
    assert store.redeem(fresh, "alice", "s1") == "carol"


def test_an_unknown_ticket_is_refused():
    assert console.TicketStore().redeem("nothing-minted", "alice", "s1") is None


def test_the_oldest_ticket_is_dropped_past_the_cap():
    store = console.TicketStore()
    tickets = [store.mint("alice", "s1", f"user{i}") for i in range(console.MAX_TICKETS + 1)]
    assert store.redeem(tickets[0], "alice", "s1") is None
    assert store.redeem(tickets[1], "alice", "s1") == "user1"
    assert store.redeem(tickets[-1], "alice", "s1") == f"user{console.MAX_TICKETS}"


# ---- RelayCounter ----


def test_the_relay_counter_holds_the_cap():
    relays = console.RelayCounter()
    assert console.MAX_RELAYS == 8
    for _ in range(console.MAX_RELAYS):
        assert not relays.full()
        assert relays.try_acquire()
    assert relays.full()
    assert not relays.try_acquire()
    relays.release()
    assert not relays.full()
    assert relays.try_acquire()


def test_a_release_never_takes_the_count_below_zero():
    relays = console.RelayCounter()
    relays.release()
    for _ in range(console.MAX_RELAYS):
        assert relays.try_acquire()
    assert not relays.try_acquire()


def test_the_close_codes_are_the_spec_table():
    assert {code for code in console.CLOSE_CODES} == {1000, 1011, 4400, 4401, 4403, 4404, 4408, 4429}
    assert [code for code, row in console.CLOSE_CODES.items() if not row.retry] == [4401, 4403, 4404]
    assert console.CLOSE_FORBIDDEN == 4403 and console.CLOSE_TOO_MANY == 4429


# ---- The ticket route ----


@pytest.fixture
def setup(db_path, monkeypatch):
    import istota.webui.app as mod

    config = Config(
        db_path=db_path,
        site=SiteConfig(hostname="example.com"),
        users={"alice": UserConfig(display_name="Alice"), "bob": UserConfig(), "dave": UserConfig()},
        web=WebConfig(auth=["email", "nextcloud"]),
    )
    config.admin_users = {"alice", "dave"}
    config.browser.enabled = True
    for user_id in ("alice", "bob"):
        user_profiles.ensure_profile(db_path, user_id, display_name=user_id.title())
        web_auth.upsert_identity(db_path, user_id, f"{user_id}@example.com")
    web_auth.set_password(db_path, "alice", PASSWORD)
    monkeypatch.setattr(mod, "_config", config)
    monkeypatch.setattr(mod.app.state, "istota_config", config, raising=False)
    monkeypatch.setattr(mod, "_oauth", MagicMock())
    monkeypatch.setattr(console, "TICKETS", console.TicketStore())
    monkeypatch.setattr(console, "RELAYS", console.RelayCounter())
    return config


async def _client(user_id):
    import istota.webui.app as mod

    client = AsyncClient(transport=ASGITransport(app=mod.app), base_url="https://example.com")
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": user_id})
    await client.get("/istota/callback", follow_redirects=False)
    return client


@pytest.fixture
async def alice(setup):
    client = await _client("alice")
    yield client
    await client.aclose()


async def _proof(client) -> dict:
    response = await client.post(STEP_UP, json={"action": "admin_browser_console"}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["method"] == "password"
    return {"request_id": body["request_id"], "password": PASSWORD}


async def _assert_proof_still_redeems(client, proof):
    response = await client.post(TICKET, json={"user": "bob", "step_up": proof}, headers=ORIGIN)
    assert response.status_code == 200, response.text


async def test_a_valid_request_returns_a_ticket_for_this_session(alice, caplog):
    proof = await _proof(alice)
    with caplog.at_level(logging.INFO, logger="istota.webui.admin_browser_console"):
        response = await alice.post(TICKET, json={"user": "bob", "step_up": proof}, headers=ORIGIN)
    assert response.status_code == 200, response.text
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    assert body["expires_in"] == 60
    assert set(body) == {"ticket", "expires_in"}
    session_id = session_value(alice)["session_id"]
    assert console.TICKETS.redeem(body["ticket"], "alice", session_id) == "bob"
    assert "browser_console_ticket admin='alice' user='bob'" in caplog.text
    assert body["ticket"] not in caplog.text


async def test_a_proof_is_spent_by_a_valid_request(alice):
    proof = await _proof(alice)
    await _assert_proof_still_redeems(alice, proof)
    again = await alice.post(TICKET, json={"user": "bob", "step_up": proof}, headers=ORIGIN)
    assert again.status_code == 403
    assert "ticket" not in again.json()


async def test_a_non_admin_is_refused(setup):
    client = await _client("bob")
    try:
        response = await client.post(TICKET, json={"user": "alice"}, headers=ORIGIN)
        assert response.status_code == 403
        assert "ticket" not in response.text
    finally:
        await client.aclose()


async def test_no_session_is_refused(setup):
    import istota.webui.app as mod

    async with AsyncClient(transport=ASGITransport(app=mod.app), base_url="https://example.com") as client:
        response = await client.post(TICKET, json={"user": "bob"}, headers=ORIGIN)
    assert response.status_code in (401, 403)


@pytest.mark.parametrize("headers", [{}, {"Origin": "https://evil.example"}])
async def test_a_bad_origin_is_refused_and_spends_no_proof(alice, headers):
    proof = await _proof(alice)
    response = await alice.post(TICKET, json={"user": "bob", "step_up": proof}, headers=headers)
    assert response.status_code == 403
    assert "ticket" not in response.text
    await _assert_proof_still_redeems(alice, proof)


@pytest.mark.parametrize("user", ["", None, 7, ["bob"], "x" * 257, "bob\nadmin", "bob\x00", "bob\x7f"])
async def test_a_malformed_user_is_400_and_spends_no_proof(alice, user):
    proof = await _proof(alice)
    response = await alice.post(TICKET, json={"user": user, "step_up": proof}, headers=ORIGIN)
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_user"
    if isinstance(user, str) and user:
        assert user not in response.text
    await _assert_proof_still_redeems(alice, proof)


async def test_a_long_user_id_at_the_limit_and_a_space_are_accepted(alice):
    for user in ("x" * 256, "a user"):
        proof = await _proof(alice)
        response = await alice.post(TICKET, json={"user": user, "step_up": proof}, headers=ORIGIN)
        assert response.status_code == 200, response.text


@pytest.mark.parametrize("content", [b"not json", b"[1, 2]", b"\xff\xfe"])
async def test_a_body_that_is_not_a_json_object_is_400(alice, content):
    response = await alice.post(TICKET, content=content,
                                headers={**ORIGIN, "Content-Type": "application/json"})
    assert response.status_code == 400
    assert response.json()["error"] == "invalid_body"


async def test_an_oversized_body_is_413_and_spends_no_proof(alice):
    proof = await _proof(alice)
    response = await alice.post(TICKET, json={"user": "bob", "step_up": proof, "pad": "x" * 5000},
                                headers=ORIGIN)
    assert response.status_code == 413
    assert response.json()["error"] == "too_large"
    await _assert_proof_still_redeems(alice, proof)


async def test_a_disabled_browser_is_404_and_spends_no_proof(alice, setup):
    proof = await _proof(alice)
    setup.browser.enabled = False
    response = await alice.post(TICKET, json={"user": "bob", "step_up": proof}, headers=ORIGIN)
    assert response.status_code == 404
    assert response.json()["error"] == "browser_disabled"
    setup.browser.enabled = True
    await _assert_proof_still_redeems(alice, proof)


async def test_an_admin_without_a_native_identity_is_refused(setup):
    client = await _client("dave")
    try:
        response = await client.post(TICKET, json={"user": "bob"}, headers=ORIGIN)
        assert response.status_code == 403
        assert response.json() == {
            "error": "admin_identity_required",
            "detail": "Opening a browser console needs an admin with a live email sign-in identity.",
        }
    finally:
        await client.aclose()


async def test_a_disabled_identity_is_refused(alice, setup):
    web_auth.set_disabled(setup.db_path, "alice", True)
    response = await alice.post(TICKET, json={"user": "bob"}, headers=ORIGIN)
    assert response.status_code in (401, 403)
    assert "ticket" not in response.text


async def test_a_full_relay_counter_is_429_and_spends_no_proof(alice):
    proof = await _proof(alice)
    for _ in range(console.MAX_RELAYS):
        assert console.RELAYS.try_acquire()
    response = await alice.post(TICKET, json={"user": "bob", "step_up": proof}, headers=ORIGIN)
    assert response.status_code == 429
    assert response.json()["error"] == "console_limit"
    assert len(console.TICKETS._tickets) == 0
    console.RELAYS.release()
    await _assert_proof_still_redeems(alice, proof)


async def test_a_missing_step_up_is_the_step_up_refusal(alice, setup):
    await _proof(alice)
    response = await alice.post(TICKET, json={"user": "bob"}, headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["field"] == "password"
    assert "reason" in response.json()
    assert len(console.TICKETS._tickets) == 0


async def test_a_wrong_password_is_the_step_up_refusal(alice):
    proof = await _proof(alice)
    response = await alice.post(TICKET, json={"user": "bob", "step_up": {**proof, "password": "not it"}},
                                headers=ORIGIN)
    assert response.status_code == 403
    assert response.json()["reason"] == "bad"
    assert len(console.TICKETS._tickets) == 0
