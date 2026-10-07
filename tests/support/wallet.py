from dataclasses import replace

import pytest

from istota import db
from istota.config import Config
from istota.wallet import cards, policy, purchases

# Public test-card pattern, constructed rather than a real account number.
NUMBER = "4242" * 4
CARD = cards.CardInput("Everyday", NUMBER, "123", 12, 2099, "Alice", cards.Billing())


@pytest.fixture(name="wallet_env")
def wallet_fixture(db_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    config = Config()
    with db.get_db(db_path) as conn:
        card_id = cards.add_card(conn, "alice", CARD)
        task_id = db.create_task(conn, user_id="alice", source_type="talk")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
        policy.put_policy(conn, "alice", policy.Policy(auto_limit_cents=5000, auto_budget_cents=20000, ceiling_cents=50000))
    return db_path, config, card_id, task_id


def request(conn, env, **overrides):
    _, config, card_id, task_id = env
    args = dict(user_id="alice", task_id=task_id, card=card_id, merchant="https://shop.example/checkout",
                amount_cents=2499, currency="USD", description="Replacement filter", extra_hosts=[], request_key=None)
    args.update(overrides)
    return purchases.request(conn, config, **args)


def card_input(**changes):
    return replace(CARD, **changes)
