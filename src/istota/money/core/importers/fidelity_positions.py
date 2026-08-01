"""Parser for the Fidelity "Portfolio Positions" CSV export.

Handles every format revision seen in the wild:

- **April 2025**: BOM, LF endings, Title-Case header, quoted thousands with
  trailing spaces (``"$29,653.14 "``), ``--`` nulls, ``Pending Activity`` in
  the Symbol column, options rows with blank symbol *and* blank account.
- **December 2025**: CRLF endings, one extra trailing empty column per line,
  ``Pending activity`` (lowercase) moved to the Description column.
- **August 2026**: sentence-case header (``Account number``, ``Last price``),
  no BOM, unquoted money values without thousands separators, explicit
  ``+$``/``-$`` signs, options rows carrying their own account, the footer
  ``Date downloaded``/``Date exported`` line wrapped in quotes, and the
  trailing empty column on data rows only (header stays 16 fields).

Header matching is therefore case-insensitive, and the one-trailing-comma
repair is applied per line (never ``rstrip(',')``, which corrupts rows).
"""

from __future__ import annotations

import csv
import io
import re
from datetime import datetime
from pathlib import Path

from .positions_base import (
    OPTION_DESCRIPTION_RE,
    ParsedSnapshot,
    PositionParseError,
    PositionRow,
    clean_money,
    clean_percent,
    clean_quantity,
    is_cash_row,
)

SOURCE_NAME = "fidelity-positions-csv"

# Canonical header, compared lowercase (Fidelity re-cased it in Aug 2026).
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
]

_FOOTER_DATE_RE = re.compile(
    r"Date (?:downloaded|exported) ([A-Za-z]+-\d+-\d+ \d+:\d+ [ap]\.m)"
)
_PENDING_RE = re.compile(r"pending activity", re.IGNORECASE)


def _header_matches(line: str) -> bool:
    if line.endswith(","):
        line = line[:-1]
    try:
        cells = next(csv.reader(io.StringIO(line)))
    except (csv.Error, StopIteration):
        return False
    return [c.strip().lower() for c in cells] == _EXPECTED_HEADER


def detect_fidelity_positions_csv(file_path: Path) -> bool:
    """True when the first line is the 16-column Fidelity positions header."""
    try:
        with open(file_path, encoding="utf-8-sig") as fh:
            first = fh.readline().rstrip("\r\n")
    except OSError:
        return False
    return _header_matches(first)


def _parse_footer_date(lines: list[str]) -> datetime | None:
    for line in reversed(lines):
        if "Date downloaded" not in line and "Date exported" not in line:
            continue
        match = _FOOTER_DATE_RE.search(line)
        if not match:
            return None
        date_str = match.group(1).replace(".m", "")
        if date_str.endswith(" a") or date_str.endswith(" p"):
            date_str += "m"
        for fmt in ("%b-%d-%Y %I:%M %p", "%b-%d-%Y %I:%M%p", "%b-%d-%Y %I:%M"):
            try:
                return datetime.strptime(date_str, fmt)
            except ValueError:
                continue
        return None
    return None


def _parse_pending_row(fields: list[str], warnings: list[str]) -> PositionRow | None:
    account_number = fields[0].strip() if fields else ""
    account_name = fields[1].strip() if len(fields) > 1 else "Unknown"
    # The amount column moved between format revisions; scan for it. Skip the
    # account fields — a name like "Active Trading (IBKR)" contains "(".
    amount = None
    for part in fields[2:]:
        part = part.strip()
        if part and ("$" in part or "(" in part):
            amount = clean_money(part)
            break
    if amount is None:
        warnings.append(
            f"Pending Activity row for {account_name!r} had no parseable amount; skipped"
        )
        return None
    return PositionRow(
        account_number=account_number,
        account_name=account_name,
        symbol="",
        description="Pending Activity",
        row_type="pending",
        quantity=None,
        price=None,
        value=amount,
        cost_basis=None,
        avg_cost_basis=None,
        day_gain=None,
        day_gain_pct=None,
        total_gain=None,
        total_gain_pct=None,
        pct_of_account=None,
        security_type="",
    )


def parse_fidelity_positions_csv(file_path: Path) -> list[ParsedSnapshot]:
    """Parse one Fidelity positions export. Always returns a single snapshot."""
    try:
        content = file_path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise PositionParseError(f"Cannot read {file_path}: {exc}") from exc

    lines = content.splitlines()
    data_lines: list[str] = []
    for line in lines:
        if line.startswith('"The data and information') or line.strip() == "":
            break
        # Exactly ONE trailing comma (the extra empty column newer formats add).
        if line.endswith(","):
            line = line[:-1]
        data_lines.append(line)

    if not data_lines:
        raise PositionParseError("File is empty or holds no positions data")
    if not _header_matches(data_lines[0]):
        raise PositionParseError(
            "Not a Fidelity Portfolio Positions export (unrecognized header)"
        )

    warnings: list[str] = []
    exported_at = _parse_footer_date(lines)
    exported_at_estimated = exported_at is None
    if exported_at is None:
        exported_at = datetime.now()
        warnings.append(
            "Export date footer missing or unparseable; using current time "
            "(content-hash dedup keeps re-imports safe)"
        )

    rows: list[PositionRow] = []
    last_account: tuple[str, str] | None = None
    reader = csv.reader(io.StringIO("\n".join(data_lines[1:])))
    for fields in reader:
        if not fields or not any(f.strip() for f in fields):
            continue
        if any(_PENDING_RE.search(f) for f in fields):
            pending = _parse_pending_row(fields, warnings)
            if pending is not None:
                rows.append(pending)
            continue
        # Tolerate a short/overlong row rather than misaligning columns.
        if len(fields) < 16:
            fields = fields + [""] * (16 - len(fields))
        elif len(fields) > 16:
            fields = fields[:16]

        account_number = fields[0].strip()
        account_name = fields[1].strip()
        symbol = fields[2].strip()
        description = fields[3].strip()

        if "Total" in account_number:
            continue

        if not symbol and OPTION_DESCRIPTION_RE.search(description):
            # Options row: backfill the symbol from the description's first
            # token; carry the account when the row itself has none.
            symbol = description.split()[0] if " " in description else "OPTION"
            if not account_name and last_account is not None:
                account_number, account_name = last_account
        elif not account_name and not account_number:
            warnings.append(f"Dropped row with no account: {description or symbol!r}")
            continue

        row_type = "cash" if is_cash_row(symbol, description) else "position"
        rows.append(PositionRow(
            account_number=account_number,
            account_name=account_name,
            symbol=symbol,
            description=description,
            row_type=row_type,
            quantity=clean_quantity(fields[4]),
            price=clean_money(fields[5]),
            value=clean_money(fields[7]),
            cost_basis=clean_money(fields[13]),
            avg_cost_basis=clean_money(fields[14]),
            day_gain=clean_money(fields[8]),
            day_gain_pct=clean_percent(fields[9]),
            total_gain=clean_money(fields[10]),
            total_gain_pct=clean_percent(fields[11]),
            pct_of_account=clean_percent(fields[12]),
            security_type=fields[15].strip(),
        ))
        # `Last Price Change` (fields[6]) is deliberately not stored:
        # ephemeral intraday noise.
        if account_name:
            last_account = (account_number, account_name)

    if not rows:
        raise PositionParseError("No position rows found in file")

    return [ParsedSnapshot(
        exported_at=exported_at,
        exported_at_estimated=exported_at_estimated,
        rows=rows,
        source=SOURCE_NAME,
        warnings=warnings,
    )]
