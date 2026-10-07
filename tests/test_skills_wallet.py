import json

import pytest

from istota import db
from istota.agent.events import _private_relay_tool
from istota.skills import wallet
from istota.wallet import purchases
from tests.support.wallet import NUMBER, wallet_fixture  # noqa: F401


@pytest.fixture
def cli(wallet_env, monkeypatch, capsys):
    path, config, _, task_id = wallet_env
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
    monkeypatch.setenv("ISTOTA_DB_PATH", str(path))
    monkeypatch.setattr("istota.config.load_config", lambda: config)
    def run(*args, code=0):
        try:
            wallet.main(list(args))
        except SystemExit as exc:
            assert exc.code == code
        else:
            assert code == 0
        captured = capsys.readouterr()
        assert NUMBER not in captured.out + captured.err
        return json.loads(captured.out)
    return run


def buy(cli, **kw):
    return cli("request", "--card", "Everyday", "--merchant", "https://shop.example/checkout",
               "--amount", "24.99", "--currency", "USD", "--description", "Replacement filter",
               "--request-key", "filter", **kw)


def test_cli_cards_request_status_complete(cli, wallet_env, monkeypatch):
    path, config, _, task_id = wallet_env
    listed = cli("cards")
    assert listed["cards"][0]["last_four"] == "4242"
    assert listed["remaining_auto_budget_cents"] == 20000
    def deliver(config, results):
        with db.get_db(path) as conn:
            assert conn.execute("SELECT count(*) FROM wallet_purchases").fetchone()[0] == 1
    monkeypatch.setattr("istota.notifications.store.deliver_pending", deliver)
    result = buy(cli)
    assert result["status"] == "authorized"
    assert "notification" not in result
    assert buy(cli) == result
    pid = str(result["purchase_id"])
    assert cli("status", pid)["purchase"]["state"] == "authorized"
    with db.get_db(path) as conn:
        purchases.claim_fill(conn, config, user_id="alice", task_id=task_id, purchase_id=int(pid))
    assert cli("complete", pid, "--order-ref", "A-123", "--amount", "24.99")["status"] == "ok"
    assert cli("status", pid)["purchase"]["state"] == "completed"


@pytest.mark.parametrize("verb", ["fail", "cancel"])
def test_terminal_verbs(cli, verb):
    pid = str(buy(cli)["purchase_id"])
    assert cli(verb, pid)["status"] == "ok"
    assert cli("status", pid)["purchase"]["state"] == {"fail": "failed", "cancel": "cancelled"}[verb]


def test_held_and_pending_refusal(cli, wallet_env):
    path, _, _, _ = wallet_env
    with db.get_db(path) as conn:
        conn.execute("DELETE FROM wallet_policies")
    assert buy(cli)["status"] == "held"
    result = cli("request", "--card", "Everyday", "--merchant", "shop.example", "--amount", "25",
                 "--currency", "USD", "--description", "Another item", code=1)
    assert result["reason"] == "confirmation_pending"


def test_identity_and_live_availability(cli, wallet_env, monkeypatch):
    _, config, _, _ = wallet_env
    config.security.sandbox_enabled = False
    from istota.config import UserConfig
    config.users = {"alice": UserConfig(), "bob": UserConfig()}
    assert cli("cards", code=1)["reason"] == "wallet_unavailable"
    monkeypatch.setenv("ISTOTA_USER_ID", "bob")
    assert cli("cards", code=1)["reason"] == "task_unavailable"


def test_exception_never_echoes_values(cli, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError(NUMBER)
    monkeypatch.setattr(purchases, "request", broken)
    assert buy(cli, code=1)["reason"] == "wallet_error"


@pytest.mark.parametrize("verb", ["cards", "request", "status", "complete", "fail", "cancel"])
def test_wallet_progress_is_private(verb):
    assert _private_relay_tool("Bash", {"command": f"istota-skill wallet {verb}"})


def test_numeric_card_id(cli, wallet_env):
    _, _, card_id, _ = wallet_env
    result = cli("request", "--card", str(card_id), "--merchant", "shop.example", "--amount", "1.00",
                 "--currency", "USD", "--description", "Item")
    assert result["status"] == "authorized"


@pytest.mark.parametrize("verb", ["complete", "fail", "cancel"])
def test_other_task_cannot_change_purchase(cli, wallet_env, monkeypatch, verb):
    path, _, _, _ = wallet_env
    purchase_id = str(buy(cli)["purchase_id"])
    with db.get_db(path) as conn:
        other = db.create_task(conn, user_id="alice", source_type="talk")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (other,))
    monkeypatch.setenv("ISTOTA_TASK_ID", str(other))
    assert cli(verb, purchase_id, code=1)["reason"] == "purchase_not_found"


def test_invalid_preview_is_safe(cli, monkeypatch):
    from istota.relay.requests import RequestError
    def invalid(*args, **kwargs):
        raise RequestError("invalid_preview")
    monkeypatch.setattr(purchases, "request", invalid)
    assert buy(cli, code=1)["reason"] == "invalid_preview"


def test_stopped_task_cannot_request(cli, wallet_env):
    path, _, _, task_id = wallet_env
    with db.get_db(path) as conn:
        conn.execute("UPDATE tasks SET status='cancelled' WHERE id=?", (task_id,))
    assert buy(cli, code=1)["reason"] == "task_unavailable"


def test_wallet_separator_hides_description_and_invocation():
    from istota.agent.events import _describe_tool_use, _tool_invocation, PRIVATE_RELAY_TOOL_DESCRIPTION
    args = ["--", "request", "--card", "Everyday", "--merchant", "shop.example",
            "--amount", "250", "--currency", "USD", "--description", "Treatment"]
    assert wallet.build_parser().parse_args(args).command == "request"
    data = {"command": "istota-skill wallet " + " ".join(args), "description": "Buying Treatment for 250 USD"}
    assert _describe_tool_use("Bash", data) == PRIVATE_RELAY_TOOL_DESCRIPTION
    assert _tool_invocation("Bash", data) is None


def test_cancel_held_allows_a_new_purchase(cli, wallet_env):
    from istota.relay.requests import held_question
    path, _, _, task_id = wallet_env
    with db.get_db(path) as conn:
        conn.execute("DELETE FROM wallet_policies")
    pid = str(buy(cli)["purchase_id"])
    cli("cancel", pid)
    with db.get_db(path) as conn:
        assert held_question(conn, task_id) is None
        assert db.get_task(conn, task_id).status == "running"
    result = cli("request", "--card", "Everyday", "--merchant", "shop.example", "--amount", "25",
                 "--currency", "USD", "--description", "Replacement item", "--request-key", "replacement")
    assert result["status"] == "held"
