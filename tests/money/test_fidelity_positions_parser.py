"""Tests for the Fidelity Portfolio Positions CSV parser.

Three real export formats are covered:
- April 2025: BOM, LF, Title-Case header, quoted thousands, trailing spaces
  inside values, ``--`` nulls, ``Pending Activity`` in the Symbol column,
  options rows with blank symbol+account.
- Dec 2025 (via the Aug 2026 fixture's shared traits): CRLF, 17th trailing
  empty column on data rows, ``Pending activity`` in the Description column.
- Aug 2026: sentence-case header, no BOM, unquoted no-separator money values,
  signed ``+$``/``-$`` amounts, options rows carrying their own account,
  quoted ``"Date exported …"`` footer line.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from istota.money.core.importers.fidelity_positions import (
    detect_fidelity_positions_csv,
    parse_fidelity_positions_csv,
)
from istota.money.core.importers.positions_base import PositionParseError

FIXTURES = Path(__file__).parent / "fixtures"
CSV_2025 = FIXTURES / "fidelity_positions_2025.csv"
CSV_2026 = FIXTURES / "fidelity_positions_2026.csv"
CSV_FINA = FIXTURES / "fina_history_small.csv"


def _rows_by(snapshot, **filters):
    rows = snapshot.rows
    for attr, want in filters.items():
        rows = [r for r in rows if getattr(r, attr) == want]
    return rows


class TestDetect:
    def test_accepts_2025_format(self):
        assert detect_fidelity_positions_csv(CSV_2025) is True

    def test_accepts_2026_sentence_case_format(self):
        assert detect_fidelity_positions_csv(CSV_2026) is True

    def test_accepts_trailing_comma_header(self, tmp_path):
        header = CSV_2025.read_text(encoding="utf-8-sig").splitlines()[0]
        p = tmp_path / "t.csv"
        p.write_text(header + ",\nX1,Acct,VTI,DESC,1,$1.00 ,,$1.00 ,,,,,,,,Cash,\n")
        assert detect_fidelity_positions_csv(p) is True

    def test_rejects_fina_history(self):
        assert detect_fidelity_positions_csv(CSV_FINA) is False

    def test_rejects_arbitrary_csv(self, tmp_path):
        p = tmp_path / "other.csv"
        p.write_text("Date,Merchant,Amount\n2025-01-01,Store,5.00\n")
        assert detect_fidelity_positions_csv(p) is False

    def test_rejects_missing_file(self, tmp_path):
        assert detect_fidelity_positions_csv(tmp_path / "nope.csv") is False


class TestParse2025:
    @pytest.fixture(scope="class")
    def snap(self):
        snapshots = parse_fidelity_positions_csv(CSV_2025)
        assert len(snapshots) == 1
        return snapshots[0]

    def test_exported_at_from_footer(self, snap):
        assert snap.exported_at == datetime(2025, 4, 9, 18, 30)
        assert snap.exported_at_estimated is False

    def test_row_and_account_counts(self, snap):
        assert len(snap.rows) == 45
        assert len({r.account_name for r in snap.rows}) == 12

    def test_total_row_dropped(self, snap):
        assert not any("Total" in r.account_number for r in snap.rows)
        # the injected Total row's value never lands on any row
        assert not any(r.value == 104600.00 for r in snap.rows)

    def test_currency_and_percent_cleaning(self, snap):
        (vgit,) = _rows_by(snap, symbol="VGIT")
        assert vgit.quantity == 502
        assert vgit.price == 59.07
        assert vgit.value == 30000.00
        assert vgit.day_gain == -130.52
        assert vgit.day_gain_pct == pytest.approx(-0.0044)
        assert vgit.total_gain == -150.60
        assert vgit.total_gain_pct == pytest.approx(-0.0051)
        assert vgit.pct_of_account == pytest.approx(0.2708)
        assert vgit.cost_basis == 30200.00
        assert vgit.avg_cost_basis == 59.37
        assert vgit.security_type == "Margin"
        assert vgit.row_type == "position"

    def test_double_dash_becomes_none(self, snap):
        (sgov,) = _rows_by(snap, symbol="SGOV")
        assert sgov.total_gain is None
        assert sgov.total_gain_pct is None
        assert sgov.avg_cost_basis is None
        assert sgov.quantity == 120.441

    def test_cash_rows_typed(self, snap):
        cash = _rows_by(snap, row_type="cash")
        assert len(cash) == 12
        symbols = {r.symbol for r in cash}
        assert "SPAXX**" in symbols
        assert "USD***" in symbols
        assert "CORE**" in symbols
        assert "FZDXX" in symbols
        assert "**" in symbols

    def test_fzdxx_sparse_row(self, snap):
        (fzdxx,) = _rows_by(snap, symbol="FZDXX")
        assert fzdxx.row_type == "cash"
        assert fzdxx.quantity == 150000.00
        assert fzdxx.value == 150000.00
        assert fzdxx.cost_basis is None

    def test_pending_rows_extracted_both_signs(self, snap):
        pending = _rows_by(snap, row_type="pending")
        by_account = {r.account_name: r for r in pending}
        assert by_account["Roth IRA A"].value == 45.72
        assert by_account["Joint Brokerage"].value == -40000.00
        for r in pending:
            assert r.symbol == ""
            assert r.description == "Pending Activity"

    def test_options_rescued_with_account_carry(self, snap):
        opts = [r for r in snap.rows if r.symbol in ("MU", "FXY", "HON", "XYZ")]
        assert len(opts) == 4
        for r in opts:
            assert r.account_name == "Active Trading (IBKR)"
            assert r.row_type == "position"
        (mu,) = [r for r in opts if r.symbol == "MU"]
        assert mu.description == "MU 18JUL25 120 C"
        assert mu.quantity == 1
        assert mu.value == 34.16
        assert mu.cost_basis == 527.80

    def test_bare_star_star_rows(self, snap):
        stars = _rows_by(snap, symbol="**")
        assert len(stars) == 2
        for r in stars:
            assert r.row_type == "cash"
            assert r.quantity is None
        values = {r.account_name: r.value for r in stars}
        assert values["Active Trading (IBKR)"] == 4084.00
        assert values["Charting (Schwab)"] == 504.00

    def test_zero_value_cash(self, snap):
        (usd,) = _rows_by(snap, symbol="USD***")
        assert usd.value == 0.0
        assert usd.row_type == "cash"


class TestParse2026:
    @pytest.fixture(scope="class")
    def snap(self):
        snapshots = parse_fidelity_positions_csv(CSV_2026)
        assert len(snapshots) == 1
        return snapshots[0]

    def test_exported_at_from_quoted_date_exported_footer(self, snap):
        assert snap.exported_at == datetime(2026, 8, 1, 14, 4)
        assert snap.exported_at_estimated is False

    def test_trailing_column_stripped_columns_aligned(self, snap):
        (vti,) = _rows_by(snap, symbol="VTI")
        assert vti.description == "VANGUARD TOTAL STK MKT ETF"
        assert vti.quantity == 10
        assert vti.value == 3000.00
        assert vti.security_type == "Margin"

    def test_signed_plus_values_cleaned(self, snap):
        (vti,) = _rows_by(snap, symbol="VTI")
        assert vti.day_gain == 20.00
        assert vti.day_gain_pct == pytest.approx(0.0067)
        assert vti.total_gain == 500.00
        assert vti.total_gain_pct == pytest.approx(0.20)

    def test_negative_values_cleaned(self, snap):
        (vxus,) = _rows_by(snap, symbol="VXUS")
        assert vxus.day_gain == -3.60
        assert vxus.total_gain == -110.80

    def test_lowercase_pending_in_description_with_parens_account(self, snap):
        pending = _rows_by(snap, row_type="pending")
        assert len(pending) == 1
        assert pending[0].account_name == "Active Trading (IBKR)"
        assert pending[0].value == -120.50

    def test_option_with_own_account_backfills_symbol(self, snap):
        (baba,) = _rows_by(snap, description="BABA 18SEP26 120 C")
        assert baba.symbol == "BABA"
        assert baba.account_name == "Active Trading (IBKR)"
        assert baba.quantity == 2
        assert baba.value == 537.00

    def test_option_with_blank_account_carries_previous(self, snap):
        (corn,) = _rows_by(snap, description="CORN 21AUG26 22 C")
        assert corn.symbol == "CORN"
        assert corn.account_name == "Active Trading (IBKR)"

    def test_row_count(self, snap):
        assert len(snap.rows) == 9


class TestFooterDate:
    def _write(self, tmp_path, footer_lines):
        header = CSV_2025.read_text(encoding="utf-8-sig").splitlines()[0]
        p = tmp_path / "t.csv"
        body = [header, "X1,Acct,VTI,DESC,1,$1.00 ,,$1.00 ,,,,,,,,Cash", ""]
        p.write_text("\n".join(body + footer_lines) + "\n")
        return p

    def test_missing_footer_estimates(self, tmp_path):
        p = self._write(tmp_path, ['"Some disclaimer"'])
        (snap,) = parse_fidelity_positions_csv(p)
        assert snap.exported_at_estimated is True
        assert snap.warnings

    def test_unparseable_date_estimates(self, tmp_path):
        p = self._write(tmp_path, ["Date downloaded blorp ET"])
        (snap,) = parse_fidelity_positions_csv(p)
        assert snap.exported_at_estimated is True

    def test_am_variant(self, tmp_path):
        p = self._write(tmp_path, ["Date downloaded Dec-19-2025 10:05 a.m ET"])
        (snap,) = parse_fidelity_positions_csv(p)
        assert snap.exported_at == datetime(2025, 12, 19, 10, 5)
        assert snap.exported_at_estimated is False


class TestParseErrors:
    def test_wrong_header_raises(self, tmp_path):
        p = tmp_path / "bad.csv"
        p.write_text("Date,Merchant,Amount\n2025-01-01,Store,5.00\n")
        with pytest.raises(PositionParseError):
            parse_fidelity_positions_csv(p)

    def test_empty_file_raises(self, tmp_path):
        p = tmp_path / "empty.csv"
        p.write_text("")
        with pytest.raises(PositionParseError):
            parse_fidelity_positions_csv(p)

    def test_header_only_raises(self, tmp_path):
        header = CSV_2025.read_text(encoding="utf-8-sig").splitlines()[0]
        p = tmp_path / "headeronly.csv"
        p.write_text(header + "\n")
        with pytest.raises(PositionParseError):
            parse_fidelity_positions_csv(p)

    def test_blank_account_non_option_dropped_with_warning(self, tmp_path):
        header = CSV_2025.read_text(encoding="utf-8-sig").splitlines()[0]
        p = tmp_path / "t.csv"
        p.write_text(
            "\n".join([
                header,
                "X1,Acct,VTI,DESC,1,$1.00 ,,$1.00 ,,,,,,,,Cash",
                ",,,NOT AN OPTION ROW,1,$1.00 ,,$1.00 ,,,,,,,,Cash",
            ]) + "\n"
        )
        (snap,) = parse_fidelity_positions_csv(p)
        assert len(snap.rows) == 1
        assert any("NOT AN OPTION ROW" in w or "account" in w.lower() for w in snap.warnings)
