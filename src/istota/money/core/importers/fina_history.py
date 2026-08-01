"""Parser for fina's ``portfolio_history.csv`` (the migration source).

Fina appended every Fidelity import to one flat CSV: the 16 Fidelity columns
plus ``Asset Class``, ``Sub Asset Class``, ``Geography``, ``Import Date`` and
``Owner``. Money values are already numeric (pandas round-trip), percent
columns are already fractions, but ``Average Cost Basis`` and ``Last Price
Change`` were stored as raw ``$``-strings.

Rows group by ``Import Date`` into one snapshot each. The stored
classification columns are ignored — classifications are live data in istota
(a read-time join), and fina's values came from the same hardcoded map the
bundled seed ports. The ``Owner`` column rides along as ``group_hints`` so
the migration can prefill the account registry's group labels.
"""

from __future__ import annotations

import csv
from datetime import datetime
from pathlib import Path

from .positions_base import (
    ParsedSnapshot,
    PositionParseError,
    PositionRow,
    clean_money,
    is_cash_row,
)

SOURCE_NAME = "fina-history-csv"

_EXPECTED_HEADER = [
    "account number",
    "account name",
    "symbol",
    "description",
    "quantity",
    "last price",
    "last price change",
    "current value",
    "today's gain/loss dollar",
    "today's gain/loss percent",
    "total gain/loss dollar",
    "total gain/loss percent",
    "percent of account",
    "cost basis total",
    "average cost basis",
    "type",
    "asset class",
    "sub asset class",
    "geography",
    "import date",
    "owner",
]


def detect_fina_history_csv(file_path: Path) -> bool:
    """True when the first line is fina's 21-column history header."""
    try:
        with open(file_path, encoding="utf-8-sig") as fh:
            first = fh.readline().rstrip("\r\n")
    except OSError:
        return False
    cells = [c.strip().lower() for c in first.split(",")]
    return cells == _EXPECTED_HEADER


def _float_or_none(raw: str) -> float | None:
    s = raw.strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def parse_fina_history_csv(file_path: Path) -> list[ParsedSnapshot]:
    """Parse the fina history file into one snapshot per ``Import Date``."""
    try:
        fh = open(file_path, encoding="utf-8-sig", newline="")
    except OSError as exc:
        raise PositionParseError(f"Cannot read {file_path}: {exc}") from exc

    with fh:
        reader = csv.reader(fh)
        try:
            header = [c.strip().lower() for c in next(reader)]
        except StopIteration:
            raise PositionParseError("File is empty") from None
        if header != _EXPECTED_HEADER:
            raise PositionParseError(
                "Not a fina portfolio_history.csv (unrecognized header)"
            )

        groups: dict[str, list[PositionRow]] = {}
        group_hints: dict[str, dict[str, str]] = {}
        warnings: dict[str, list[str]] = {}
        for fields in reader:
            if not fields or not any(f.strip() for f in fields):
                continue
            if len(fields) < 21:
                fields = fields + [""] * (21 - len(fields))
            import_date = fields[19].strip()
            if not import_date:
                continue
            symbol = fields[2].strip()
            description = fields[3].strip()
            owner = fields[20].strip()
            account_name = fields[1].strip()
            row = PositionRow(
                account_number=fields[0].strip(),
                account_name=account_name,
                symbol=symbol,
                description=description,
                row_type="cash" if is_cash_row(symbol, description) else "position",
                quantity=_float_or_none(fields[4]),
                price=clean_money(fields[5]),
                value=_float_or_none(fields[7]),
                cost_basis=_float_or_none(fields[13]),
                avg_cost_basis=clean_money(fields[14]),
                day_gain=_float_or_none(fields[8]),
                day_gain_pct=_float_or_none(fields[9]),
                total_gain=_float_or_none(fields[10]),
                total_gain_pct=_float_or_none(fields[11]),
                pct_of_account=_float_or_none(fields[12]),
                security_type=fields[15].strip(),
            )
            groups.setdefault(import_date, []).append(row)
            if owner and account_name:
                group_hints.setdefault(import_date, {})[account_name] = owner

    if not groups:
        raise PositionParseError("No position rows found in file")

    snapshots: list[ParsedSnapshot] = []
    for import_date in sorted(groups):
        try:
            exported_at = datetime.fromisoformat(import_date)
        except ValueError:
            warnings.setdefault(import_date, []).append(
                f"Unparseable Import Date {import_date!r}"
            )
            continue
        snapshots.append(ParsedSnapshot(
            exported_at=exported_at,
            exported_at_estimated=False,
            rows=groups[import_date],
            source=SOURCE_NAME,
            warnings=warnings.get(import_date, []),
            group_hints=group_hints.get(import_date, {}),
        ))

    if not snapshots:
        raise PositionParseError("No snapshots with a parseable Import Date")
    return snapshots
