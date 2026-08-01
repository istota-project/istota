"""Tests for the /money/portfolio/* web routes."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.money import portfolio
from istota.money.cli import UserContext
from istota.money.db import init_db
from istota.money.routes import get_user_config, require_auth, router

FIXTURES = Path(__file__).parent / "fixtures"
CSV_2025 = FIXTURES / "fidelity_positions_2025.csv"
CSV_2026 = FIXTURES / "fidelity_positions_2026.csv"
CSV_FINA = FIXTURES / "fina_history_small.csv"


@pytest.fixture
def client(tmp_path):
    db_path = tmp_path / "data" / "money.db"
    db_path.parent.mkdir(parents=True)
    init_db(db_path)
    ctx = UserContext(data_dir=tmp_path, ledgers=[], db_path=db_path)
    app = FastAPI()
    app.include_router(router, prefix="/api/money")
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    app.dependency_overrides[get_user_config] = lambda: ctx
    tc = TestClient(app)
    tc.db_path = db_path
    return tc


def _upload(client, path: Path, **params):
    with open(path, "rb") as fh:
        return client.post(
            "/api/money/portfolio/import",
            params=params,
            files={"file": (path.name, fh, "text/csv")},
        )


class TestImportRoute:
    def test_happy_path(self, client):
        resp = _upload(client, CSV_2025)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "ok"
        assert body["position_count"] == 45
        assert body["source_file"] == CSV_2025.name

    def test_dry_run_writes_nothing(self, client):
        resp = _upload(client, CSV_2025, dry_run=1)
        assert resp.status_code == 200
        body = resp.json()
        assert body["dry_run"] is True
        assert body["snapshots"][0]["position_count"] == 45
        snaps = client.get("/api/money/portfolio/snapshots").json()
        assert snaps["snapshots"] == []

    def test_duplicate_409(self, client):
        first = _upload(client, CSV_2025).json()
        resp = _upload(client, CSV_2025)
        assert resp.status_code == 409
        assert resp.json()["snapshot_id"] == first["snapshot_id"]

    def test_same_date_collision_and_replace(self, client, tmp_path):
        first = _upload(client, CSV_2025).json()
        # same calendar date, different content
        variant = tmp_path / "variant.csv"
        text = CSV_2025.read_text(encoding="utf-8-sig")
        variant.write_text(text.replace("502", "503"), encoding="utf-8")
        resp = _upload(client, variant)
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "date_collision"
        assert body["existing"]["id"] == first["snapshot_id"]
        # replace resolves it
        resp = _upload(client, variant, replace=first["snapshot_id"])
        assert resp.json()["status"] == "ok"
        snaps = client.get("/api/money/portfolio/snapshots").json()["snapshots"]
        assert len(snaps) == 1

    def test_same_date_force_keeps_both(self, client, tmp_path):
        _upload(client, CSV_2025)
        variant = tmp_path / "variant.csv"
        text = CSV_2025.read_text(encoding="utf-8-sig")
        variant.write_text(text.replace("502", "503"), encoding="utf-8")
        resp = _upload(client, variant, force=1)
        assert resp.json()["status"] == "ok"
        snaps = client.get("/api/money/portfolio/snapshots").json()["snapshots"]
        assert len(snaps) == 2

    def test_fina_history_bulk(self, client):
        resp = _upload(client, CSV_FINA)
        assert resp.status_code == 200
        body = resp.json()
        assert body["imported"] == 3

    def test_bad_file_400(self, client, tmp_path):
        bogus = tmp_path / "bogus.csv"
        bogus.write_text("a,b,c\n1,2,3\n")
        resp = _upload(client, bogus)
        assert resp.status_code == 400
        assert resp.json()["status"] == "error"

    def test_explicit_source(self, client):
        resp = _upload(client, CSV_2025, source="fidelity-positions-csv")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_explicit_source_mismatched_file_400(self, client):
        # A fina file declared as a Fidelity export must error, not fall back
        # to detection — the picker's declared intent wins.
        resp = _upload(client, CSV_FINA, source="fidelity-positions-csv")
        assert resp.status_code == 400
        assert "Fidelity" in resp.json()["error"]

    def test_unknown_source_400(self, client):
        resp = _upload(client, CSV_2025, source="nope")
        assert resp.status_code == 400
        assert "Unknown import source" in resp.json()["error"]


class TestReadRoutes:
    def test_snapshots_and_detail(self, client):
        _upload(client, CSV_2025)
        snaps = client.get("/api/money/portfolio/snapshots").json()["snapshots"]
        assert len(snaps) == 1
        detail = client.get(f"/api/money/portfolio/snapshots/{snaps[0]['id']}")
        assert detail.status_code == 200
        assert detail.json()["summary"]["total_value"] > 0

    def test_snapshot_detail_404(self, client):
        assert client.get("/api/money/portfolio/snapshots/99").status_code == 404

    def test_summary_latest_and_empty(self, client):
        resp = client.get("/api/money/portfolio/summary")
        assert resp.status_code == 200
        assert resp.json()["summary"] is None
        _upload(client, CSV_2025)
        resp = client.get("/api/money/portfolio/summary")
        summary = resp.json()["summary"]
        assert summary["total_value"] > 0
        assert summary["by_asset_class"]
        assert summary["holdings"][0]["symbol"]

    def test_history(self, client):
        _upload(client, CSV_FINA)
        resp = client.get("/api/money/portfolio/history?group_by=asset_class")
        body = resp.json()
        assert len(body["series"]) == 3
        assert "groups" in body["series"][0]

    def test_history_bad_group_by(self, client):
        assert client.get("/api/money/portfolio/history?group_by=zzz").status_code == 400

    def test_diff(self, client):
        _upload(client, CSV_FINA)
        snaps = client.get("/api/money/portfolio/snapshots").json()["snapshots"]
        resp = client.get(
            f"/api/money/portfolio/diff?older={snaps[-1]['id']}&newer={snaps[0]['id']}"
        )
        assert resp.status_code == 200
        assert "changed" in resp.json()["diff"]

    def test_diff_unknown_ids(self, client):
        assert client.get("/api/money/portfolio/diff?older=1&newer=2").status_code == 404

    def test_symbol_history(self, client):
        _upload(client, CSV_FINA)
        resp = client.get("/api/money/portfolio/symbols/vti/history")
        body = resp.json()
        assert body["history"]["symbol"] == "VTI"
        assert len(body["history"]["points"]) == 3


class TestDeleteRoute:
    def test_delete(self, client):
        first = _upload(client, CSV_2025).json()
        resp = client.delete(f"/api/money/portfolio/snapshots/{first['snapshot_id']}")
        assert resp.status_code == 200
        assert client.get("/api/money/portfolio/snapshots").json()["snapshots"] == []

    def test_delete_404(self, client):
        assert client.delete("/api/money/portfolio/snapshots/99").status_code == 404


class TestAccountRoutes:
    def test_list_and_patch(self, client):
        _upload(client, CSV_2026)
        accounts = client.get("/api/money/portfolio/accounts").json()["accounts"]
        acct = next(a for a in accounts if a["account_name"] == "Taxable Brokerage")
        resp = client.patch(
            f"/api/money/portfolio/accounts/{acct['id']}",
            json={"group": "Alice", "account_type": "brokerage", "excluded": True},
        )
        assert resp.status_code == 200
        updated = resp.json()["account"]
        assert updated["group"] == "Alice"
        assert updated["account_type"] == "brokerage"
        assert updated["excluded"] is True

    def test_patch_unknown_key_400(self, client):
        _upload(client, CSV_2026)
        accounts = client.get("/api/money/portfolio/accounts").json()["accounts"]
        resp = client.patch(
            f"/api/money/portfolio/accounts/{accounts[0]['id']}",
            json={"nope": 1},
        )
        assert resp.status_code == 400

    def test_patch_bad_types_400(self, client):
        _upload(client, CSV_2026)
        accounts = client.get("/api/money/portfolio/accounts").json()["accounts"]
        resp = client.patch(
            f"/api/money/portfolio/accounts/{accounts[0]['id']}",
            json={"excluded": "yes"},
        )
        assert resp.status_code == 400

    def test_patch_missing_404(self, client):
        resp = client.patch("/api/money/portfolio/accounts/99", json={"group": "X"})
        assert resp.status_code == 404


class TestClassificationRoutes:
    def test_list_put_delete(self, client):
        resp = client.get("/api/money/portfolio/classifications")
        seeded = resp.json()["classifications"]
        assert any(c["symbol"] == "VTI" for c in seeded)
        resp = client.put(
            "/api/money/portfolio/classifications/goog",
            json={"asset_class": "Stocks", "sub_class": "Technology", "geography": "US"},
        )
        assert resp.status_code == 200
        assert resp.json()["classification"]["symbol"] == "GOOG"
        resp = client.delete("/api/money/portfolio/classifications/GOOG")
        assert resp.status_code == 200
        resp = client.delete("/api/money/portfolio/classifications/GOOG")
        assert resp.status_code == 404

    def test_put_requires_asset_class(self, client):
        resp = client.put(
            "/api/money/portfolio/classifications/goog",
            json={"sub_class": "Technology"},
        )
        assert resp.status_code == 400

    def test_put_rejects_control_chars(self, client):
        resp = client.put(
            "/api/money/portfolio/classifications/goog",
            json={"asset_class": "Sto\x00cks"},
        )
        assert resp.status_code == 400

    def test_excluded_account_changes_summary(self, client):
        _upload(client, CSV_2026)
        before = client.get("/api/money/portfolio/summary").json()["summary"]["total_value"]
        accounts = client.get("/api/money/portfolio/accounts").json()["accounts"]
        acct = next(a for a in accounts if a["account_name"] == "Reserve")
        client.patch(
            f"/api/money/portfolio/accounts/{acct['id']}", json={"excluded": True}
        )
        after = client.get("/api/money/portfolio/summary").json()["summary"]["total_value"]
        assert after == pytest.approx(before - 1000.37)


class TestEnsureInitialisedCreatesPortfolioSchema:
    def test_tables_exist_after_ensure(self, tmp_path):
        from istota.money._migrate import ensure_initialised

        ctx = UserContext(data_dir=tmp_path, ledgers=[])
        ensure_initialised(ctx)
        conn = sqlite3.connect(str(ctx.db_path))
        try:
            rows = conn.execute(
                "SELECT name FROM sqlite_master WHERE name='portfolio_snapshots'"
            ).fetchall()
            assert rows
            # seed ran too
            assert portfolio.list_classifications(conn)
        finally:
            conn.close()
