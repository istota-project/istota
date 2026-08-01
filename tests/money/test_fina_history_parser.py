"""Tests for the fina portfolio_history.csv parser (migration source)."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from istota.money.core.importers.fina_history import (
    detect_fina_history_csv,
    parse_fina_history_csv,
)
from istota.money.core.importers.positions_base import PositionParseError

FIXTURES = Path(__file__).parent / "fixtures"
CSV_FINA = FIXTURES / "fina_history_small.csv"
CSV_2025 = FIXTURES / "fidelity_positions_2025.csv"


class TestDetect:
    def test_accepts_fina_history(self):
        assert detect_fina_history_csv(CSV_FINA) is True

    def test_rejects_fidelity_export(self):
        assert detect_fina_history_csv(CSV_2025) is False

    def test_rejects_missing_file(self, tmp_path):
        assert detect_fina_history_csv(tmp_path / "nope.csv") is False


class TestParse:
    @pytest.fixture(scope="class")
    def snapshots(self):
        return parse_fina_history_csv(CSV_FINA)

    def test_split_by_import_date_ascending(self, snapshots):
        assert [s.exported_at for s in snapshots] == [
            datetime(2025, 4, 10, 19, 24),
            datetime(2025, 4, 11, 12, 30),
            datetime(2025, 5, 1, 9, 0),
        ]
        assert all(s.exported_at_estimated is False for s in snapshots)
        assert [len(s.rows) for s in snapshots] == [3, 2, 2]

    def test_numeric_passthrough(self, snapshots):
        vti = next(r for r in snapshots[0].rows if r.symbol == "VTI")
        assert vti.quantity == 258.0
        assert vti.price == 257.43
        assert vti.value == 65000.0
        assert vti.day_gain == 945.6
        # fractions stored as-is, never re-divided
        assert vti.day_gain_pct == pytest.approx(0.0144)
        assert vti.pct_of_account == pytest.approx(0.6169)
        assert vti.security_type == "Margin"

    def test_avg_cost_basis_cleaned_to_float(self, snapshots):
        vti = next(r for r in snapshots[0].rows if r.symbol == "VTI")
        assert vti.avg_cost_basis == 253.76

    def test_cash_rows_typed(self, snapshots):
        spaxx = next(r for r in snapshots[0].rows if r.symbol == "SPAXX**")
        assert spaxx.row_type == "cash"
        assert spaxx.value == 7000.0

    def test_group_hints(self, snapshots):
        assert snapshots[0].group_hints == {
            "Taxable Brokerage": "Alice",
            "Joint Brokerage": "Bob",
        }
        assert snapshots[2].group_hints == {"Taxable Brokerage": "Alice"}

    def test_classification_columns_ignored(self, snapshots):
        # PositionRow carries no classification fields; stored values must not
        # leak into raw-independent fields either.
        vti = next(r for r in snapshots[0].rows if r.symbol == "VTI")
        assert not hasattr(vti, "asset_class")

    def test_source_name(self, snapshots):
        assert all(s.source == "fina-history-csv" for s in snapshots)


class TestParseErrors:
    def test_wrong_header_raises(self, tmp_path):
        p = tmp_path / "bad.csv"
        p.write_text("Date,Merchant,Amount\n2025-01-01,Store,5.00\n")
        with pytest.raises(PositionParseError):
            parse_fina_history_csv(p)
