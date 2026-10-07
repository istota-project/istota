"""Wallet settings use the real store without returning secret-bearing input."""

import json
import logging
from unittest.mock import AsyncMock

import pytest

from istota import db
from istota.wallet import cards, purchases
from tests.test_web_app import app, client, config  # noqa: F401 -- shared fixtures

BASE = "/istota/api/settings/wallet"
ORIGIN = {"Origin": "https://example.com"}
PAN = "4242" * 4
MARKER = "synthetic-wallet-secret"


@pytest.fixture
async def signed_client(client, config, monkeypatch):  # noqa: F811
    import istota.webui.app as mod
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    config.security.allow_unsandboxed_multi_user_vaults = True
    mod._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": "alice"})
    await client.get("/istota/callback", follow_redirects=False)
    return client


def card_body(**changes):
    return {"label": "Everyday", "number": PAN, "cvc": "123", "exp_month": 12,
            "exp_year": 2099, "name": "Alice", "billing": {"city": "Example City"}, **changes}


async def add(http):
    response = await http.post(BASE + "/cards", json=card_body(), headers=ORIGIN)
    assert response.status_code == 200, response.text
    return response.json()["id"]


async def test_card_and_policy_round_trip(signed_client, config):  # noqa: F811
    assert config.experimental.features == []
    ident = await add(signed_client)
    listing = await signed_client.get(BASE)
    data = listing.json()
    assert data["currency_precision"]["default"] == 2
    assert data["currency_precision"]["exceptions"]["KRW"] == 0
    assert data["currency_precision"]["exceptions"]["OMR"] == 3
    assert data["currency_precision"]["exceptions"]["CLF"] == 4
    assert data["enabled"] is True
    assert not data["refusal"]
    assert data["cards"][0]["last_four"] == "4242"
    assert "number" not in data["cards"][0] and "cvc" not in data["cards"][0]
    assert PAN not in listing.text
    assert data["policy"]["auto_limit_cents"] == 0
    assert data["purchases"] == []
    policy = {"currency": "JPY", "auto_limit_cents": 100, "auto_budget_cents": 1000,
              "ceiling_cents": None, "allow_scheduled": True}
    assert (await signed_client.put(BASE + "/policy", json=policy, headers=ORIGIN)).status_code == 200
    assert (await signed_client.get(BASE)).json()["policy"] == policy
    response = await signed_client.patch(BASE + f"/cards/{ident}", json={"state": "paused", "label": "Travel"}, headers=ORIGIN)
    assert response.status_code == 200
    assert (await signed_client.get(BASE)).json()["cards"][0]["state"] == "paused"
    assert (await signed_client.post(BASE + "/cards", json=card_body(label="Travel"), headers=ORIGIN)).status_code == 409
    response = await signed_client.delete(BASE + f"/cards/{ident}", headers=ORIGIN)
    assert response.json() == {"ok": True, "cancelled": 0}
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM secrets WHERE service='wallet'").fetchone()[0] == 0


@pytest.mark.parametrize("body", [
    [MARKER], {MARKER: MARKER}, {"number": MARKER}, card_body(number=123),
    card_body(billing={MARKER: MARKER}), card_body(exp_month=True),
])
async def test_bad_card_body_never_echoes_input(signed_client, caplog, body):
    caplog.set_level(logging.DEBUG)
    response = await signed_client.post(BASE + "/cards", json=body, headers=ORIGIN)
    assert response.status_code == 400
    assert "field" in response.json()
    assert MARKER not in response.text and PAN not in response.text
    assert MARKER not in caplog.text and PAN not in caplog.text


async def test_parse_and_store_errors_are_safe(signed_client, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    for raw in (MARKER + "{", "[" * 60000):
        response = await signed_client.post(BASE + "/cards", content=raw, headers=ORIGIN)
        assert response.status_code == 400
        assert MARKER not in response.text
    response = await signed_client.post(BASE + "/cards", content="x" * 70000, headers=ORIGIN)
    assert response.status_code == 413
    async def chunks():
        yield json.dumps(card_body()).encode()
    assert (await signed_client.post(BASE + "/cards", content=chunks(), headers=ORIGIN)).status_code == 413
    def broken(*args, **kwargs):
        raise RuntimeError(MARKER)
    monkeypatch.setattr(cards, "add_card", broken)
    response = await signed_client.post(BASE + "/cards", json=card_body(), headers=ORIGIN)
    assert response.status_code == 500
    assert MARKER not in response.text and MARKER not in caplog.text
    assert "RuntimeError" in caplog.text


async def test_writes_gate_origin_and_isolation(signed_client, config, monkeypatch):  # noqa: F811
    from istota.credentials import vault
    writes = [("post", "/cards"), ("patch", "/cards/1"), ("delete", "/cards/1"),
              ("put", "/policy"), ("post", "/purchases/1/cancel")]
    for method, path in writes:
        assert (await signed_client.request(method, BASE + path, json={})).status_code == 403
    monkeypatch.setattr(vault, "vault_isolation_refusal", lambda *a: "isolation refused")
    assert (await signed_client.get(BASE)).json()["refusal"] == "isolation refused"
    for method, path in writes:
        assert (await signed_client.request(method, BASE + path, json={}, headers=ORIGIN)).status_code == 403
    signed_client.cookies.clear()
    assert (await signed_client.get(BASE)).status_code == 401
    for method, path in writes:
        assert (await signed_client.request(method, BASE + path, json={}, headers=ORIGIN)).status_code == 401


async def test_ownership_and_immutable_card_values(signed_client, config):  # noqa: F811
    ident = await add(signed_client)
    assert (await signed_client.patch(BASE + f"/cards/{ident}", json={"number": MARKER}, headers=ORIGIN)).status_code == 400
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE wallet_cards SET user_id='bob' WHERE id=?", (ident,))
    assert (await signed_client.get(BASE)).json()["cards"] == []
    for method in ("patch", "delete"):
        assert (await signed_client.request(method, BASE + f"/cards/{ident}", json={}, headers=ORIGIN)).status_code == 404
    assert (await signed_client.post(BASE + "/purchases/999/cancel", headers=ORIGIN)).status_code == 404


@pytest.mark.parametrize("remove", [False, True])
async def test_cancel_and_remove_use_shared_purchase_close(signed_client, config, remove):  # noqa: F811
    from istota.relay.requests import associate_confirmation, held_question
    ident = await add(signed_client)
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Private")
        task_id = db.create_task(conn, user_id="alice", prompt="Buy filter", conversation_token=room.token)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        result = purchases.request(conn, config, user_id="alice", task_id=task_id, card=ident,
                                   merchant="shop.example", amount_cents=100, currency="USD", description="Filter")
        held = held_question(conn, task_id)
        db.set_task_confirmation(conn, task_id, held["preview"])
        associate_confirmation(conn, actor_user_id="alice", task_id=task_id,
                               request_id=held["id"], preview_digest=held["preview_digest"])
    listing = (await signed_client.get(BASE)).json()["purchases"]
    assert listing[0]["room_token"] == room.token
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET conversation_token='unknown-room' WHERE id=?", (task_id,))
    assert (await signed_client.get(BASE)).json()["purchases"][0]["room_token"] is None
    if remove:
        response = await signed_client.delete(BASE + f"/cards/{ident}", headers=ORIGIN)
        assert response.json() == {"ok": True, "cancelled": 1}
    else:
        response = await signed_client.post(BASE + f"/purchases/{result.purchase_id}/cancel", headers=ORIGIN)
    assert response.status_code == 200, response.text
    with db.get_db(config.db_path) as conn:
        assert purchases.get_purchase(conn, "alice", result.purchase_id)["state"] == "cancelled"
        assert db.get_task(conn, task_id).status == "cancelled"
        assert conn.execute("SELECT state FROM whatsapp_skill_requests WHERE origin_task_id=?", (task_id,)).fetchone()[0] != "held"
    assert (await signed_client.post(BASE + f"/purchases/{result.purchase_id}/cancel", headers=ORIGIN)).status_code == 400


@pytest.mark.parametrize("policy", [{"auto_limit_cents": True}, {"currency": MARKER}, {MARKER: 12}, {"ceiling_cents": -1}])
async def test_invalid_policy(signed_client, policy):
    response = await signed_client.put(BASE + "/policy", json=policy, headers=ORIGIN)
    assert response.status_code == 400
    assert MARKER not in response.text


@pytest.mark.parametrize("currency,text,minor", [("KRW", "25", 25), ("CLP", "25", 25),
                                                ("TND", "25.125", 25125), ("OMR", "25.125", 25125),
                                                ("CLF", "1.2345", 12345)])
async def test_declared_currency_amount_reaches_listing(signed_client, config, currency, text, minor):  # noqa: F811
    from istota.wallet.money import parse_amount
    ident = await add(signed_client)
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "Private")
        task_id = db.create_task(conn, user_id="alice", prompt="Buy filter", conversation_token=room.token)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        purchases.request(conn, config, user_id="alice", task_id=task_id, card=ident,
                          merchant="shop.example", amount_cents=parse_amount(text, currency),
                          currency=currency, description="Filter")
    row = (await signed_client.get(BASE)).json()["purchases"][0]
    assert row["amount_cents"] == minor
    assert row["currency"] == currency
