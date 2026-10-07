from dataclasses import asdict

import pytest

from istota import db
from istota.credentials.schema import all_known_services
from istota.wallet import cards
from .support.wallet import NUMBER, card_input, request, wallet_fixture  # noqa: F401 -- shared fixture


def test_card_metadata_and_encrypted_storage(wallet_env):
    path, _, card_id, _ = wallet_env
    with db.get_db(path) as conn:
        view = cards.get_card(conn, "alice", card_id)
        assert view.brand == "visa" and view.last_four == "4242"
        assert "number" not in asdict(view) and "cvc" not in asdict(view)
        assert NUMBER not in repr(view) + repr(card_input())
        assert NUMBER not in str(list(conn.execute("SELECT * FROM secrets")))
        assert cards.read_card_secrets(conn, "alice", card_id)["number"] == NUMBER
        assert cards.get_card(conn, "bob", card_id) is None
        assert cards.list_cards(conn, "bob") == []
    assert "wallet" not in all_known_services()


@pytest.mark.parametrize("prefix,length,brand", [("4",16,"visa"), ("51",16,"mastercard"), ("2221",16,"mastercard"), ("37",15,"amex"), ("6011",16,"discover"), ("9",16,"other")])
def test_luhn_and_brand(wallet_env, prefix, length, brand):
    base = prefix + "0" * (length - len(prefix) - 1)
    number = next(base + str(i) for i in range(10) if cards.valid_number(base + str(i)))
    with db.get_db(wallet_env[0]) as conn:
        ident = cards.add_card(conn, "alice", card_input(label=brand, number=number))
        assert cards.get_card(conn, "alice", ident).brand == brand


@pytest.mark.parametrize("changes,field", [({"number":"1234"},"number"), ({"number":"4242" * 3 + "4243"},"number"), ({"number":"０" * 16},"number"), ({"cvc":"12"},"cvc"), ({"exp_month":13},"exp_month"), ({"exp_year":2000},"exp_year"), ({"label":""},"label"), ({"label":"Everyday"},"label")])
def test_invalid_card_never_echoes_number(wallet_env, changes, field):
    with db.get_db(wallet_env[0]) as conn:
        with pytest.raises(cards.CardError) as error:
            cards.add_card(conn, "alice", card_input(**changes))
        assert error.value.field == field
        assert NUMBER not in str(error.value)


def test_update_and_remove_cancel_open_purchases(wallet_env):
    path, _, card_id, _ = wallet_env
    with db.get_db(path) as conn:
        purchase = request(conn, wallet_env)
        cards.update_card(conn, "alice", card_id, label="Travel", name="A", billing=cards.Billing(city="Example"))
        assert cards.get_card(conn, "alice", "Travel").billing.city == "Example"
        with pytest.raises(TypeError):
            cards.update_card(conn, "alice", card_id, number=NUMBER)
        assert cards.remove_card(conn, "bob", card_id) == 0
        assert cards.remove_card(conn, "alice", card_id) == 1
        row = conn.execute("SELECT * FROM wallet_purchases WHERE id=?", (purchase.purchase_id,)).fetchone()
        assert row["state"] == "cancelled" and row["card_id"] is None
        assert conn.execute("SELECT count(*) FROM secrets WHERE service='wallet'").fetchone()[0] == 0


def test_card_and_secrets_rollback_together(wallet_env):
    path = wallet_env[0]
    with pytest.raises(RuntimeError):
        with db.get_db(path) as conn:
            cards.add_card(conn, "alice", card_input(label="Rollback", number="-".join(["4242"] * 2) + " " + "-".join(["4242"] * 2)))
            raise RuntimeError()
    with db.get_db(path) as conn:
        assert len(cards.list_cards(conn, "alice")) == 1
        assert conn.execute("SELECT count(*) FROM secrets WHERE service='wallet'").fetchone()[0] == 2
