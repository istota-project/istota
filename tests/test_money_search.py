"""Money search reuses the transaction route and runs only on request."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient

from istota.money.cli import UserContext
from istota.money.routes import get_user_config, require_auth, router
from tests.money.test_routes_edit import _seed_ledger


@pytest.fixture
def ledger(tmp_path):
    path = _seed_ledger(tmp_path)
    with path.open("a") as file:
        file.write(
            '\n2024-03-01 * "Acme" "Coffee refill" #work\n'
            '  id: "txn-refill"\n'
            '  Expenses:Food:Coffee   6.00 USD\n'
            '  Expenses:Food:Restaurants   4.00 USD\n'
            '  Assets:Bank:Checking\n'
        )
    return path


@pytest.mark.parametrize("params,ids", [
    ({}, ["txn-refill", "txn-refill", "txn-pay", "txn-coffee"]),
    ({"filter": "Coffee"}, ["txn-refill", "txn-refill", "txn-coffee"]),
    ({"filter": "#work"}, ["txn-refill", "txn-refill"]),
    ({"filter": "Coffee|Payment", "year": 2024}, ["txn-refill", "txn-refill", "txn-pay", "txn-coffee"]),
    ({"account": "^Assets:", "filter": "Acme"}, ["txn-refill", "txn-coffee"]),
    ({"year": 2023}, []),
    ({"filter": "O'Reilly"}, []),
])
def test_extracted_query_preserves_route_rows(ledger, params, ids):
    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    app.dependency_overrides[get_user_config] = lambda: UserContext(
        data_dir=ledger.parent, ledgers=[{"name": "main", "path": ledger}],
    )
    with TestClient(app) as client:
        before = client.get("/transactions", params=params).json()
        assert [row["id"] for row in before["transactions"]] == ids
        from istota.money.core.ledger import search_transactions
        rows = search_transactions(ledger, params.get("filter"), account=params.get("account"), year=params.get("year"))
        assert rows == before["transactions"]
        after = client.get("/transactions", params={**params, "page": 2, "per_page": 1}).json()
        assert after == {"status": "ok", "transactions": rows[1:2], "total": len(rows), "page": 2, "per_page": 1}
        assert search_transactions(ledger, params.get("filter"), account=params.get("account"), year=params.get("year"), limit=1, offset=1) == rows[1:2]
        assert client.get("/transactions?ledger=missing").status_code == 404
        assert client.get("/transactions?filter=[").status_code == 500


@pytest.fixture
def config(tmp_path, ledger):
    from tests.test_web_app import _make_config
    from istota.money._loader import resolve_for_user
    cfg = _make_config(tmp_path)
    cfg.users["alice"].disabled_modules = ["health", "location", "briefings", "feeds"]
    for user in ("alice", "bob"):
        cfg.users[user].disabled_modules = ["health", "location", "briefings", "feeds"]
        ctx = resolve_for_user(user, cfg)
        target = ctx.ledgers[0]["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(ledger.read_text().replace("Acme", "Other user") if user == "bob" else ledger.read_text())
    return cfg


async def test_authenticated_on_demand_response(config, monkeypatch):
    from tests.test_web_app import _patch_app
    import istota.webui.app as web
    from istota.money.core import ledger as queries
    app = _patch_app(config)
    web._oauth.nextcloud.authorize_access_token = AsyncMock(return_value={"user_id": "alice"})
    actual = queries.run_bean_query
    calls = []
    def track(*args, **kwargs):
        calls.append((args, kwargs))
        return actual(*args, **kwargs)
    monkeypatch.setattr(queries, "run_bean_query", track)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://example.com") as client:
        assert (await client.get("/istota/api/search?q=Coffee&sources=money")).status_code == 401
        await client.get("/istota/callback", follow_redirects=False)
        all_sources = (await client.get("/istota/api/search?q=Coffee")).json()
        assert all_sources["on_demand"] == [{"source": "money", "label": "Transactions"}]
        assert calls == []
        response = await client.get("/istota/api/search?q=Coffee&sources=money&limit=20&user_id=bob")
        assert response.status_code == 200
        data = response.json()
        group = data["groups"][0]
        assert group["source"] == "money" and group["error"] is None
        assert not group["relaxed"] and not group["has_more"]
        assert len(group["results"]) == 3
        assert [hit["date"] for hit in group["results"]] == ["2024-03-01", "2024-03-01", "2024-02-01"]
        assert len({hit["id"] for hit in group["results"]}) == 3
        assert {hit["title"] for hit in group["results"]} == {"Acme"}
        assert group["results"][0]["link"] == {"type": "route", "path": "/money/transactions/", "params": {"account": "Expenses:Food:Coffee", "year": "2024"}}
        assert all(hit["kind"] == "transaction" and hit["highlights"] for hit in group["results"])
        assert 0 < calls[0][1]["timeout"] <= 8
        first = (await client.get("/istota/api/search?q=Coffee&sources=money&limit=2")).json()["groups"][0]
        second = (await client.get("/istota/api/search?q=Coffee&sources=money&limit=2&offset=2")).json()["groups"][0]
        assert first["has_more"] and not second["has_more"]
        assert first["results"] + second["results"] == group["results"]
        empty = (await client.get("/istota/api/search?q=Coffee%20absent&sources=money")).json()["groups"][0]
        assert empty["results"] == [] and not empty["relaxed"]
        # The dialog fixture comes from the authenticated route and a real ledger.
        for payload in (all_sources, data):
            for item in payload["groups"]:
                item["elapsed_ms"] = 0
        fixture = Path(__file__).parents[1] / "web/src/lib/test/fixtures/money-search.json"
        expected = {"all": all_sources, "money": data}
        if os.environ.get("UPDATE_MONEY_SEARCH_FIXTURE"):
            fixture.parent.mkdir(parents=True, exist_ok=True)
            fixture.write_text(json.dumps(expected, indent=2) + "\n")
        assert json.loads(fixture.read_text()) == expected
        from istota import db
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE user_profiles SET disabled_modules = ? WHERE user_id = ?", (json.dumps(["money"]), "alice"))
        disabled = (await client.get("/istota/api/search?q=Coffee&sources=money")).json()
        assert disabled["groups"] == [] and disabled["on_demand"] == []


async def test_missing_ledger_and_expired_deadline(config):
    from istota.money._loader import resolve_for_user
    from istota.money.search import PROVIDER
    from istota.search.core import SearchContext, run_search
    from istota.lib.text_match import parse_query
    resolve_for_user("alice", config).ledgers[0]["path"].unlink()
    group = (await run_search(config, "alice", "Coffee", sources=["money"]))["groups"][0]
    assert group["results"] == [] and group["error"] is None
    with pytest.raises(TimeoutError):
        PROVIDER.run(SearchContext(config, "alice", time.monotonic() - 1), parse_query("Coffee"), "strict", 5, 0)


def test_query_timeout_kills_and_reaps_subprocess(tmp_path, monkeypatch):
    from istota.money.core import ledger as queries
    spawned = []
    popen = subprocess.Popen
    def track(*args, **kwargs):
        process = popen(*args, **kwargs)
        spawned.append(process)
        return process
    monkeypatch.setattr(queries.subprocess, "Popen", track)
    monkeypatch.setattr(queries, "_bean_cmd", lambda _: sys.executable)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        queries.run_bean_query(Path("-c"), "import time; time.sleep(30)", timeout=0.5)
    assert time.monotonic() - start < 3
    assert len(spawned) == 1 and spawned[0].returncode is not None
    pid = spawned[0].pid
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)


def test_default_query_timeout_keeps_existing_error(monkeypatch, tmp_path):
    from istota.money.core import ledger as queries
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] == 120
        raise subprocess.TimeoutExpired(args[0], 120)
    monkeypatch.setattr(queries.subprocess, "run", timeout)
    with pytest.raises(ValueError, match="bean-query timed out"):
        queries.run_bean_query(tmp_path / "main.beancount", "SELECT date")
