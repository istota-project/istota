"""Invoice reviews through sync, the bell, and authenticated money routes."""

import sqlite3
from datetime import date
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota import db as framework_db
from istota.config import Config, UserConfig
from istota.money import db, work
from istota.money.cli import cli
from istota.money.routes import router, require_auth
from istota.notifications import store, sources
from istota.storage import get_user_bot_path
from tests.money.test_cli import TestSyncMonarchInvoiceMatching as SyncFixtures


@pytest.fixture
def scenario(tmp_path):
    config = Config(db_path=tmp_path / "framework.db", workspace_path=tmp_path / "mount",
                    users={u: UserConfig() for u in ("alice", "bob")})
    framework_db.init_db(config.db_path)
    contexts = {}
    for user in config.users:
        workspace = config.workspace_path / get_user_bot_path(user, config.bot_dir_name).lstrip("/") / "money"
        workspace.mkdir(parents=True)
        ctx = SyncFixtures()._ctx(workspace)
        target = config.module_db_path(user, "money")
        target.parent.mkdir(parents=True, exist_ok=True)
        with db.get_db(ctx.db_path) as source, sqlite3.connect(target) as dest:
            source.backup(dest)
        uctx = ctx.users.pop("default")
        uctx.db_path = target
        assert uctx.monarch_config_path is None
        ctx.users[user] = uctx
        ctx.activate_user(user)
        ctx.framework_config = config
        ctx.framework_db_path = config.db_path
        contexts[user] = ctx
        with patch("istota.money.core.invoicing.generate_invoice_pdf"):
            for _ in range(2):
                result = CliRunner().invoke(cli, ["-u", user, "invoice", "create", "acme", "-s", "dev", "-q", "8"], obj=ctx)
                assert result.exit_code == 0, result.output
    sources.reset_registry()
    yield config, contexts
    sources.reset_registry()


def sync(ctx, command="run-scheduled"):
    credit = SyncFixtures()._credit(1200, payee="Private bank memo <script> & payee")
    with patch("istota.money.core.transactions.fetch_monarch_transactions", return_value=[credit]):
        result = CliRunner().invoke(cli, ["-u", ctx.active_user, command], obj=ctx)
    assert result.exit_code == 0, result.output


def rows(config):
    with framework_db.get_db(config.db_path) as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM notifications WHERE source = 'invoice_match'")]


def client(config, user="alice"):
    app = FastAPI()
    app.state.istota_config = config
    app.include_router(router, prefix="/money")
    app.dependency_overrides[require_auth] = lambda: {"username": user}
    return TestClient(app)


def test_scheduled_review_delivers_and_settles_through_route(scenario):
    config, contexts = scenario
    ctx = contexts["alice"]
    with patch("istota.notifications.delivery.send_notification", return_value=True) as send:
        sync(ctx)
    notices = rows(config)
    assert len(notices) == 1
    assert notices[0]["state"] == "open"
    assert notices[0]["last_delivered_at"]
    assert "Private bank memo" in notices[0]["body"]
    assert "Private bank memo" not in str(send.call_args)
    with framework_db.get_db(config.db_path) as conn:
        items, total = store.list_open(config, conn, "alice")
        assert total == 1
        action = items[0].actions[0].to_dict()
        assert action["body"] == {"invoice_number": "INV-000001"}
        assert not sources.invalid_paths(items[0])
        store.mark_seen(conn, "alice", [(notices[0]["id"], notices[0]["updated_at"])])
    assert rows(config)[0]["state"] == "open"
    with client(config) as web:
        response = web.post(action["endpoint"], json=action["body"])
    assert response.status_code == 200, response.text
    assert rows(config)[0]["state"] == "resolved"
    assert work.get_entries_for_invoice(ctx.data_dir, "INV-000001")[0].paid_date == date.today()
    with db.get_db(ctx.db_path) as conn:
        assert db.list_payment_matches(conn)[0]["decided_by"] == "user"


@pytest.mark.parametrize("decision", ["dismiss", "paid"])
def test_resolver_liveness_and_cross_user_isolation(scenario, decision):
    config, contexts = scenario
    for ctx in contexts.values():
        sync(ctx, "sync-monarch")
    notices = rows(config)
    assert len(notices) == 2
    alice_row = next(r for r in notices if r["user_id"] == "alice")
    if decision == "dismiss":
        with db.get_db(contexts["alice"].db_path) as conn:
            db.dismiss_review(conn, alice_row["object_id"])
    else:
        for number in ("INV-000001", "INV-000002"):
            work.record_invoice_payment(contexts["alice"].data_dir, number, date.today())
    with framework_db.get_db(config.db_path) as conn:
        assert store.list_open(config, conn, "alice")[1] == 0
        assert store.list_open(config, conn, "bob")[1] == 1
    assert {r["user_id"]: r["state"] for r in rows(config)} == {"alice": "stale", "bob": "open"}


@pytest.mark.parametrize("command", ["sync-monarch", "run-scheduled"])
def test_settlement_is_bell_only_and_closes_when_seen(scenario, command):
    config, contexts = scenario
    ctx = contexts["alice"]
    work.record_invoice_payment(ctx.data_dir, "INV-000002", date.today())
    with patch("istota.notifications.delivery.send_notification", return_value=True) as send:
        sync(ctx, command)
    send.assert_not_called()
    notice = rows(config)[0]
    assert notice["dedup_key"].startswith("settled:")
    assert notice["last_delivered_at"] is None
    with framework_db.get_db(config.db_path) as conn:
        assert store.list_open(config, conn, "alice")[1] == 1
        store.mark_seen(conn, "alice", [(notice["id"], notice["updated_at"])])
    assert rows(config)[0]["state"] == "resolved"


def test_interactive_review_does_not_push_and_dismiss_route_closes(scenario):
    config, contexts = scenario
    with patch("istota.notifications.delivery.send_notification", return_value=True) as send:
        sync(contexts["alice"], "sync-monarch")
    send.assert_not_called()
    notice = rows(config)[0]
    endpoint = f"/money/invoices/review/{notice['object_id']}/dismiss"
    with client(config, "bob") as web:
        assert web.post(endpoint).status_code == 400
    assert rows(config)[0]["state"] == "open"
    with client(config) as web:
        assert web.post(endpoint).status_code == 200
    assert rows(config)[0]["state"] == "resolved"


def test_reverted_settlement_is_stale(scenario):
    config, contexts = scenario
    ctx = contexts["alice"]
    work.record_invoice_payment(ctx.data_dir, "INV-000002", date.today())
    sync(ctx, "sync-monarch")
    with db.get_db(ctx.db_path) as conn:
        db.revert_settled(conn, "INV-000001")
    with framework_db.get_db(config.db_path) as conn:
        assert store.list_open(config, conn, "alice")[1] == 0
    assert rows(config)[0]["state"] == "stale"


def test_candidate_cap_and_route_refusals(scenario):
    from fastapi import HTTPException
    from istota.money.routes import verify_origin

    config, contexts = scenario
    ctx = contexts["alice"]
    with patch("istota.money.core.invoicing.generate_invoice_pdf"):
        for _ in range(2):
            result = CliRunner().invoke(cli, ["-u", "alice", "invoice", "create", "acme", "-s", "dev", "-q", "8"], obj=ctx)
            assert result.exit_code == 0, result.output
    sync(ctx, "sync-monarch")
    with framework_db.get_db(config.db_path) as conn:
        item = store.list_open(config, conn, "alice")[0][0]
    assert len([a for a in item.actions if a.id.startswith("settle-")]) == 3
    assert next(a for a in item.actions if a.id == "open").href == "/money/invoices"
    assert "INV-000004" in item.body
    action = item.actions[0]
    with client(config) as web:
        assert web.post(action.endpoint, json={"invoice_number": "INV-999999"}).status_code == 400
        assert web.post(action.endpoint, json=[]).status_code == 400

        def refuse_origin():
            raise HTTPException(403, "origin mismatch")

        web.app.dependency_overrides[verify_origin] = refuse_origin
        assert web.post(action.endpoint, json=action.body).status_code == 403
        assert web.post(item.actions[-1].endpoint).status_code == 403
        web.app.dependency_overrides.clear()
        assert web.post(action.endpoint, json=action.body).status_code == 401
    assert rows(config)[0]["state"] == "open"
    assert all(e.paid_date is None for e in work.load_work_entries(ctx.data_dir))


def test_dismiss_closes_only_the_owners_same_id(scenario):
    config, contexts = scenario
    for ctx in contexts.values():
        sync(ctx, "sync-monarch")
    notice = next(r for r in rows(config) if r["user_id"] == "alice")
    ident = notice["object_id"]
    with db.get_db(contexts["bob"].db_path) as conn:
        conn.execute("UPDATE invoice_payment_matches SET ledger_txn_id = ?", (ident,))
    with framework_db.get_db(config.db_path) as conn:
        conn.execute("UPDATE notifications SET object_id = ?, dedup_key = ? WHERE user_id = 'bob'",
                     (ident, f"review:{ident}"))
    with client(config) as web:
        assert web.post(f"/money/invoices/review/{ident}/dismiss").status_code == 200
    assert {r["user_id"]: r["state"] for r in rows(config)} == {"alice": "resolved", "bob": "open"}
    with framework_db.get_db(config.db_path) as conn:
        assert store.list_open(config, conn, "bob")[1] == 1


@pytest.mark.parametrize("boundary", ["operator", "skill"])
def test_framework_context_from_invocation_boundaries(scenario, monkeypatch, boundary):
    from istota import cli_money
    from istota.skills import money as skill

    config, contexts = scenario
    user_ctx = contexts["alice"].users["alice"]
    credit = SyncFixtures()._credit(1200)
    with patch("istota.money.core.transactions.fetch_monarch_transactions", return_value=[credit]), \
         patch("istota.money.load_user_secrets", return_value={}):
        if boundary == "operator":
            with patch.object(cli_money, "_load_user_ctx", return_value=user_ctx):
                assert cli_money._invoke_money_cli(config, "alice", ["sync-monarch"]) == 0
        else:
            # The scheduler's explicit DB path wins over the loaded config.
            from copy import copy
            loaded = copy(config)
            loaded.db_path = config.db_path.with_name("unused.db")
            monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
            with patch.object(skill, "_resolve_context", return_value=("alice", loaded, user_ctx, None)):
                assert skill._run(["sync-monarch"])["status"] == "ok"
            assert not loaded.db_path.exists()
    assert len(rows(config)) == 1
    assert rows(config)[0]["user_id"] == "alice"


def test_missing_framework_path_keeps_money_review(scenario, caplog):
    config, contexts = scenario
    ctx = contexts["alice"]
    ctx.framework_db_path = None
    sync(ctx)
    with db.get_db(ctx.db_path) as conn:
        assert len(db.list_payment_matches(conn, status="review")) == 1
    assert rows(config) == []
    assert "bell write skipped" in caplog.text
