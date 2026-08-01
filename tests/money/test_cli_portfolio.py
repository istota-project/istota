"""Tests for the `money portfolio` Click group."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from istota.money import config_store, portfolio
from istota.money.cli import Context, UserContext, cli

FIXTURES = Path(__file__).parent / "fixtures"
CSV_2025 = FIXTURES / "fidelity_positions_2025.csv"
CSV_2026 = FIXTURES / "fidelity_positions_2026.csv"
CSV_FINA = FIXTURES / "fina_history_small.csv"


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def obj(tmp_path):
    dbp = tmp_path / "data" / "money.db"
    dbp.parent.mkdir(parents=True, exist_ok=True)
    config_store.init_db(dbp)
    uctx = UserContext(data_dir=tmp_path, ledgers=[], db_path=dbp)
    ctx = Context()
    ctx.users["default"] = uctx
    ctx.activate_user("default")
    return ctx


def _invoke(runner, obj, args):
    result = runner.invoke(cli, args, obj=obj, catch_exceptions=False)
    payload = json.loads(result.output) if result.output.strip() else {}
    return result, payload


class TestImport:
    def test_import_fidelity_auto_detect(self, runner, obj):
        result, payload = _invoke(runner, obj, ["portfolio", "import", str(CSV_2025)])
        assert result.exit_code == 0
        assert payload["status"] == "ok"
        assert payload["position_count"] == 45
        assert payload["snapshot_id"] >= 1

    def test_reimport_duplicate_exits_zero(self, runner, obj):
        _invoke(runner, obj, ["portfolio", "import", str(CSV_2025)])
        result, payload = _invoke(runner, obj, ["portfolio", "import", str(CSV_2025)])
        assert result.exit_code == 0
        assert payload["status"] == "duplicate"

    def test_dry_run_writes_nothing(self, runner, obj):
        result, payload = _invoke(
            runner, obj, ["portfolio", "import", str(CSV_2025), "--dry-run"]
        )
        assert result.exit_code == 0
        assert payload["dry_run"] is True
        assert payload["snapshots"][0]["position_count"] == 45
        _, snaps = _invoke(runner, obj, ["portfolio", "snapshots"])
        assert snaps["snapshots"] == []

    def test_fina_history_imports_all_snapshots(self, runner, obj):
        result, payload = _invoke(runner, obj, ["portfolio", "import", str(CSV_FINA)])
        assert result.exit_code == 0
        assert payload["status"] == "ok"
        assert payload["imported"] == 3

    def test_transactions_source_is_rejected(self, runner, obj):
        result, payload = _invoke(
            runner, obj,
            ["portfolio", "import", str(CSV_2025), "--source", "monarch-csv"],
        )
        assert result.exit_code == 1
        assert payload["status"] == "error"
        assert "import-csv" in payload["error"]

    def test_undetectable_file_errors(self, runner, obj, tmp_path):
        bogus = tmp_path / "bogus.csv"
        bogus.write_text("a,b,c\n1,2,3\n")
        result, payload = _invoke(runner, obj, ["portfolio", "import", str(bogus)])
        assert result.exit_code == 1
        assert payload["status"] == "error"

    def test_replace_deletes_old_snapshot(self, runner, obj):
        _, first = _invoke(runner, obj, ["portfolio", "import", str(CSV_2025)])
        result, payload = _invoke(
            runner, obj,
            ["portfolio", "import", str(CSV_2026), "--replace", str(first["snapshot_id"])],
        )
        assert payload["status"] == "ok"
        _, snaps = _invoke(runner, obj, ["portfolio", "snapshots"])
        assert len(snaps["snapshots"]) == 1
        assert snaps["snapshots"][0]["id"] == payload["snapshot_id"]


class TestReads:
    @pytest.fixture
    def seeded(self, runner, obj):
        _invoke(runner, obj, ["portfolio", "import", str(CSV_FINA)])
        return obj

    def test_snapshots(self, runner, seeded):
        result, payload = _invoke(runner, seeded, ["portfolio", "snapshots"])
        assert result.exit_code == 0
        assert len(payload["snapshots"]) == 3

    def test_summary_defaults_to_latest(self, runner, seeded):
        _, payload = _invoke(runner, seeded, ["portfolio", "summary"])
        assert payload["status"] == "ok"
        assert payload["summary"]["exported_at"].startswith("2025-05-01")

    def test_summary_group_filter(self, runner, seeded):
        # Bob's account appears only in the oldest fixture snapshot.
        _, snaps = _invoke(runner, seeded, ["portfolio", "snapshots"])
        oldest = snaps["snapshots"][-1]["id"]
        _, payload = _invoke(
            runner, seeded,
            ["portfolio", "summary", "--snapshot", str(oldest), "--group", "Bob"],
        )
        assert payload["summary"]["total_value"] == pytest.approx(52000.0)

    def test_summary_no_snapshots_errors(self, runner, obj):
        result, payload = _invoke(runner, obj, ["portfolio", "summary"])
        assert result.exit_code == 1
        assert payload["status"] == "error"

    def test_history_grouped(self, runner, seeded):
        _, payload = _invoke(
            runner, seeded, ["portfolio", "history", "--group-by", "asset_class"]
        )
        assert len(payload["series"]) == 3
        assert "Stocks" in payload["series"][0]["groups"]

    def test_diff(self, runner, seeded):
        _, snaps = _invoke(runner, seeded, ["portfolio", "snapshots"])
        newest, middle = snaps["snapshots"][0]["id"], snaps["snapshots"][1]["id"]
        _, payload = _invoke(
            runner, seeded, ["portfolio", "diff", str(middle), str(newest)]
        )
        assert payload["status"] == "ok"
        assert "changed" in payload["diff"]

    def test_symbol_history(self, runner, seeded):
        _, payload = _invoke(runner, seeded, ["portfolio", "symbol", "vti"])
        assert payload["history"]["symbol"] == "VTI"
        assert len(payload["history"]["points"]) == 3


class TestDeleteSnapshot:
    def test_requires_confirmed(self, runner, obj):
        _, first = _invoke(runner, obj, ["portfolio", "import", str(CSV_2025)])
        result, payload = _invoke(
            runner, obj, ["portfolio", "delete-snapshot", str(first["snapshot_id"])]
        )
        assert result.exit_code == 1
        assert "--confirmed" in payload["error"]

    def test_deletes_with_confirmed(self, runner, obj):
        _, first = _invoke(runner, obj, ["portfolio", "import", str(CSV_2025)])
        result, payload = _invoke(
            runner, obj,
            ["portfolio", "delete-snapshot", str(first["snapshot_id"]), "--confirmed"],
        )
        assert payload["status"] == "ok"
        _, snaps = _invoke(runner, obj, ["portfolio", "snapshots"])
        assert snaps["snapshots"] == []

    def test_missing_id_errors(self, runner, obj):
        result, payload = _invoke(
            runner, obj, ["portfolio", "delete-snapshot", "99", "--confirmed"]
        )
        assert result.exit_code == 1


class TestAccountsAndClassify:
    @pytest.fixture
    def seeded(self, runner, obj):
        _invoke(runner, obj, ["portfolio", "import", str(CSV_2026)])
        return obj

    def test_accounts_list(self, runner, seeded):
        _, payload = _invoke(runner, seeded, ["portfolio", "accounts"])
        names = {a["account_name"] for a in payload["accounts"]}
        assert "Taxable Brokerage" in names
        assert "Active Trading (IBKR)" in names

    def test_set_group_and_type(self, runner, seeded):
        _, payload = _invoke(runner, seeded, ["portfolio", "accounts"])
        acct = next(a for a in payload["accounts"] if a["account_name"] == "Taxable Brokerage")
        _, updated = _invoke(
            runner, seeded,
            ["portfolio", "accounts", "--set-group", str(acct["id"]), "Alice"],
        )
        assert updated["status"] == "ok"
        _, updated = _invoke(
            runner, seeded,
            ["portfolio", "accounts", "--set-type", str(acct["id"]), "brokerage"],
        )
        _, payload = _invoke(runner, seeded, ["portfolio", "accounts"])
        acct = next(a for a in payload["accounts"] if a["account_name"] == "Taxable Brokerage")
        assert acct["group"] == "Alice"
        assert acct["account_type"] == "brokerage"

    def test_exclude_include(self, runner, seeded):
        _, payload = _invoke(runner, seeded, ["portfolio", "accounts"])
        acct = next(a for a in payload["accounts"] if a["account_name"] == "Reserve")
        _invoke(runner, seeded, ["portfolio", "accounts", "--exclude", str(acct["id"])])
        _, payload = _invoke(runner, seeded, ["portfolio", "accounts"])
        acct = next(a for a in payload["accounts"] if a["account_name"] == "Reserve")
        assert acct["excluded"] is True
        _invoke(runner, seeded, ["portfolio", "accounts", "--include", str(acct["id"])])
        _, payload = _invoke(runner, seeded, ["portfolio", "accounts"])
        acct = next(a for a in payload["accounts"] if a["account_name"] == "Reserve")
        assert acct["excluded"] is False

    def test_classify_and_unclassify(self, runner, seeded):
        result, payload = _invoke(
            runner, seeded,
            ["portfolio", "classify", "GOOG", "--asset-class", "Stocks",
             "--sub-class", "Technology", "--geography", "US"],
        )
        assert payload["status"] == "ok"
        conn = None
        import sqlite3

        conn = sqlite3.connect(str(seeded.db_path))
        try:
            cls = {c.symbol_norm: c for c in portfolio.list_classifications(conn)}
            assert cls["GOOG"].sub_class == "Technology"
        finally:
            conn.close()
        _, removed = _invoke(runner, seeded, ["portfolio", "unclassify", "GOOG"])
        assert removed["status"] == "ok"

    def test_unclassify_missing_errors(self, runner, seeded):
        result, payload = _invoke(runner, seeded, ["portfolio", "unclassify", "NOPE"])
        assert result.exit_code == 1


class TestAutoClassification:
    def test_import_auto_classifies_via_heuristic(self, runner, obj, tmp_path):
        variant = tmp_path / "variant.csv"
        text = CSV_2025.read_text(encoding="utf-8-sig")
        variant.write_text(text.replace("VGIT", "ZZZQ"), encoding="utf-8")
        result, payload = _invoke(runner, obj, ["portfolio", "import", str(variant)])
        assert result.exit_code == 0
        classified = {c["symbol"]: c for c in payload["auto_classified"]}
        assert classified["ZZZQ"]["asset_class"] == "Fixed Income"
        assert "ZZZQ" not in payload["unclassified_symbols"]

    def test_autoclass_backfills(self, runner, obj, tmp_path, monkeypatch):
        from istota.money import portfolio_autoclass

        variant = tmp_path / "variant.csv"
        text = CSV_2025.read_text(encoding="utf-8-sig")
        text = text.replace("VGIT", "ZZZQ").replace(
            "VANGUARD SCOTTSDALE FDS INTER TERM TREAS", "OPAQUE HOLDINGS CO"
        )
        variant.write_text(text, encoding="utf-8")
        _, payload = _invoke(runner, obj, ["portfolio", "import", str(variant)])
        assert "ZZZQ" in payload["unclassified_symbols"]

        monkeypatch.setattr(
            portfolio_autoclass, "fetch_symbol_info",
            lambda s: {"quoteType": "EQUITY", "sector": "Energy",
                       "country": "United States"},
        )
        result, payload = _invoke(runner, obj, ["portfolio", "autoclass"])
        assert result.exit_code == 0
        assert payload["status"] == "ok"
        classified = {c["symbol"]: c for c in payload["classified"]}
        assert classified["ZZZQ"]["method"] == "lookup"

    def test_autoclass_nothing_to_do(self, runner, obj):
        result, payload = _invoke(runner, obj, ["portfolio", "autoclass"])
        assert result.exit_code == 0
        assert payload["classified"] == []
        assert payload["unresolved"] == []

    def test_import_survives_a_classification_failure(
        self, runner, obj, tmp_path, monkeypatch,
    ):
        """The route wraps classification fail-soft; the CLI's equivalent
        block was bare, so the same outage failed an import that had already
        committed."""
        from istota.money import portfolio_autoclass

        def boom(conn, snapshots, **kwargs):
            raise RuntimeError("classification exploded")

        monkeypatch.setattr(
            portfolio_autoclass, "auto_classify_snapshots", boom,
        )
        variant = tmp_path / "variant.csv"
        text = CSV_2025.read_text(encoding="utf-8-sig")
        variant.write_text(text.replace("VGIT", "ZZZQ"), encoding="utf-8")
        result, payload = _invoke(runner, obj, ["portfolio", "import", str(variant)])
        assert result.exit_code == 0
        assert payload["status"] == "ok"
        assert "ZZZQ" in payload["unclassified_symbols"]
        assert payload["auto_classified"] == []

    def test_operator_gate_skips_the_third_party_lookup(
        self, runner, obj, tmp_path, monkeypatch,
    ):
        from istota.money import portfolio_autoclass

        calls = []
        monkeypatch.setattr(
            portfolio_autoclass, "fetch_symbol_info",
            lambda s, **kw: calls.append(s) or {"quoteType": "EQUITY"},
        )
        obj.autoclass_lookup = False
        variant = tmp_path / "variant.csv"
        text = CSV_2025.read_text(encoding="utf-8-sig")
        variant.write_text(text.replace("VGIT", "ZZZQ"), encoding="utf-8")
        _invoke(runner, obj, ["portfolio", "import", str(variant)])
        assert calls == []
        result, payload = _invoke(runner, obj, ["portfolio", "autoclass"])
        assert result.exit_code == 0
        assert calls == []
        assert payload["lookups_available"] is False
