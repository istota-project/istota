from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import pytest

from istota import db
from istota.config import load_config
from istota.wallet import policy
from istota.wallet.hosts import normalize_merchant
from istota.wallet.money import format_amount, parse_amount
from .support.wallet import request, wallet_fixture  # noqa: F401 -- shared fixture


@pytest.mark.parametrize("overrides,state,reason", [({},"authorized",None), ({"amount_cents":50001},"refused","over_ceiling"), ({"amount_cents":6000},"held",None), ({"currency":"EUR","amount_cents":50001},"held",None), ({"extra_hosts":["js.stripe.com"]},"authorized",None), ({"extra_hosts":["pay.google.com"]},"held",None), ({"card":"missing"},"refused","card_not_found")])
def test_decision_order(wallet_env, overrides, state, reason):
    with db.get_db(wallet_env[0]) as conn:
        result = request(conn, wallet_env, **overrides)
        assert (result.status, result.reason) == (state, reason)


def test_no_policy_holds(wallet_env):
    with db.get_db(wallet_env[0]) as conn:
        conn.execute("DELETE FROM wallet_policies")
        assert request(conn, wallet_env, amount_cents=999999).status == "held"


@pytest.mark.parametrize("source", ["scheduled", "briefing", "heartbeat"])
def test_scheduled_ancestor_holds_until_allowed(wallet_env, source):
    with db.get_db(wallet_env[0]) as conn:
        parent = db.create_task(conn, user_id="alice", source_type=source)
        child = db.create_task(conn, user_id="alice", parent_task_id=parent)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (child,))
        assert request(conn, wallet_env, task_id=child).status == "held"
        policy.put_policy(conn, "alice", policy.Policy(auto_limit_cents=5000, auto_budget_cents=20000, allow_scheduled=True))
        assert request(conn, wallet_env, task_id=child).status == "authorized"


def test_member_shared_room_holds_and_unasked_refuses(wallet_env):
    with db.get_db(wallet_env[0]) as conn:
        member = db.create_task(conn, user_id="alice", source_type="talk", conversation_token="room", is_group_chat=True)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (member,))
        assert request(conn, wallet_env, task_id=member).status == "held"
        unasked = db.create_task(conn, user_id="alice", conversation_token="room", is_group_chat=True)
        assert request(conn, wallet_env, task_id=unasked).reason == "wallet_unavailable"


def test_rolling_budget_boundary(wallet_env):
    now = datetime.now(timezone.utc).replace(microsecond=0)
    with db.get_db(wallet_env[0]) as conn:
        first = request(conn, wallet_env)
        conn.execute("UPDATE wallet_purchases SET created_at=? WHERE id=?", ((now-timedelta(days=30)).strftime("%Y-%m-%d %H:%M:%S"), first.purchase_id))
        assert policy.auto_spent_cents(conn, "alice", now) == 2499
        assert policy.auto_spent_cents(conn, "alice", now+timedelta(seconds=1)) == 0
        conn.execute("UPDATE wallet_purchases SET state='unreported'")
        assert policy.auto_spent_cents(conn, "alice", now) == 2499
        conn.execute("UPDATE wallet_purchases SET state='failed'")
        assert policy.auto_spent_cents(conn, "alice", now) == 0


def test_concurrent_requests_reserve_budget(wallet_env):
    with db.get_db(wallet_env[0]) as conn:
        policy.put_policy(conn, "alice", policy.Policy(auto_limit_cents=5000, auto_budget_cents=3000))
    def buy(_):
        with db.get_db(wallet_env[0]) as conn:
            return request(conn, wallet_env).status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(buy, range(2))) == ["authorized", "held"]


@pytest.mark.parametrize("currency,text,minor", [("USD","24.99",2499),("JPY","20",20),("KWD","1.234",1234),("BHD","1.200",1200),("KRW","25",25),("CLP","25",25),("TND","25.125",25125),("OMR","25.125",25125),("CLF","1.2345",12345)])
def test_amounts(currency, text, minor):
    assert parse_amount(text, currency) == minor
    assert parse_amount(format_amount(minor, currency), currency) == minor


@pytest.mark.parametrize("text", ["NaN", "Infinity", "-1", "0", "1.001", "1e2", "1,000", "9" * 100])
def test_bad_amounts(text):
    with pytest.raises(ValueError):
        parse_amount(text, "USD")


def test_host_normalization():
    assert normalize_merchant("https://SHOP.example:443/checkout") == "shop.example"
    assert normalize_merchant("shop.example:8443") == "shop.example:8443"
    for host in ("http://shop.example", "https://alice@shop.example", "*.example", "https://shop.example\\evil"):
        with pytest.raises(ValueError):
            normalize_merchant(host)


def test_security_config_clamps_wallet_window(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text('[experimental]\nfeatures = ["wallet"]\n[security]\nwallet_authorization_minutes = 999\nwallet_requests_per_task = 4\nwallet_fills_per_purchase = 2\n')
    config = load_config(path)
    assert config.security.wallet_authorization_minutes == 240
    assert config.security.wallet_requests_per_task == 4
    assert config.security.wallet_fills_per_purchase == 2


@pytest.mark.parametrize("change,reason", [("paused", "card_paused"), ("expired", "card_expired"), ("feature", "wallet_unavailable"), ("withheld", "wallet_unavailable")])
def test_request_refusal_order(wallet_env, change, reason):
    with db.get_db(wallet_env[0]) as conn:
        if change == "paused":
            conn.execute("UPDATE wallet_cards SET state='paused'")
        if change == "expired":
            conn.execute("UPDATE wallet_cards SET exp_year=2000")
        if change == "feature":
            wallet_env[1].experimental.features = []
        if change == "withheld":
            conn.execute("UPDATE tasks SET guest_participant_id=42 WHERE id=?", (wallet_env[3],))
        result = request(conn, wallet_env, amount_cents=50001)
        assert result.reason == reason
        if reason == "wallet_unavailable":
            row = conn.execute("SELECT reason FROM wallet_purchases WHERE id=?", (result.purchase_id,)).fetchone()
            assert row[0] in ("feature_disabled", "scopes_withheld")


@pytest.mark.parametrize("field,value", [("auto_limit_cents", None), ("auto_budget_cents", -1), ("ceiling_cents", True), ("allow_scheduled", "false")])
def test_policy_rejects_bad_values(wallet_env, field, value):
    with db.get_db(wallet_env[0]) as conn:
        with pytest.raises(ValueError):
            policy.put_policy(conn, "alice", policy.Policy(**{field: value}))


@pytest.mark.parametrize("old_currency,new_currency", [("USD", "JPY"), ("JPY", "KWD")])
def test_currency_change_holds_until_old_spending_leaves_window(wallet_env, old_currency, new_currency):
    with db.get_db(wallet_env[0]) as conn:
        policy.put_policy(conn, "alice", policy.Policy(currency=old_currency, auto_limit_cents=5000, auto_budget_cents=20000))
        first = request(conn, wallet_env, currency=old_currency, amount_cents=1)
        assert first.status == "authorized"
        policy.put_policy(conn, "alice", policy.Policy(currency=new_currency, auto_limit_cents=5000, auto_budget_cents=20000))
        assert request(conn, wallet_env, currency=new_currency).status == "held"
        conn.execute("UPDATE wallet_purchases SET created_at=datetime('now','-31 days') WHERE id=?", (first.purchase_id,))
        assert request(conn, wallet_env, currency=new_currency).status == "authorized"
