"""CSV import/export for bloodwork panels.

The file format is the one most people already keep offline as a
spreadsheet: rows are dates, columns are biomarkers. Three header rows
precede the data:

1. Category banner (``MORPHOLOGY``, ``CHEMISTRY``, etc.) — optional;
   ignored on import.
2. ``Marker (unit)`` headers (e.g. ``Hgb (g/dL)``). The first two
   columns are always ``Date`` and ``Lab``.
3. Reference range per column (e.g. ``12.7-16.7``) — informational and
   ignored on import; canonical ranges drive flagging.

Each remaining row is one panel: an ISO date, the lab name, then a value
per column (blank = no value for that marker).

Aliases are resolved against ``biomarker_refs`` so that ``Hgb`` becomes
``Hemoglobin``, ``LDL-C`` becomes ``LDL``, and so on. Markers not in the
refs are stored under the printed name and land in the matrix's "Other"
category.

Imported panels are marked confirmed (``draft=0``) since they're a
deliberate user-curated record, not an OCR draft.
"""

from __future__ import annotations

import csv
import io
import re
import sqlite3
from dataclasses import dataclass, field
from typing import Iterable, Iterator

from istota.health import db as health_db
from istota.health.units import (
    compute_flag,
    pick_canonical_range,
    widest_canonical_range,
)


# BP / resting-HR fan-out mirrors routes.api_replace_biomarkers so imported
# rows feed the unified time series the same way as confirmed extractions.
_STAT_FANOUT: dict[str, tuple[str, str]] = {
    "blood_pressure_systolic": ("BP_Systolic", "mmHg"),
    "blood_pressure_diastolic": ("BP_Diastolic", "mmHg"),
    "resting_hr": ("Resting_HR", "bpm"),
}


_HEADER_RE = re.compile(r"^(.*?)(?:\s*\(([^)]+)\))?\s*$")
_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:T.*)?$")


@dataclass
class ParsedPanel:
    drawn_at: str
    lab_name: str | None
    biomarkers: list[dict] = field(default_factory=list)


@dataclass
class ImportSummary:
    panels_created: int = 0
    panels_replaced: int = 0
    panels_skipped: int = 0
    biomarkers_created: int = 0
    rows_processed: int = 0
    warnings: list[str] = field(default_factory=list)


def _parse_header(text: str) -> tuple[str, str]:
    """Split a ``Name (unit)`` column header into (name, unit).

    Returns ``(name, "")`` when no parenthesised unit is present.
    """
    s = (text or "").strip()
    if not s:
        return "", ""
    m = _HEADER_RE.match(s)
    if not m:
        return s, ""
    name = (m.group(1) or "").strip()
    unit = (m.group(2) or "").strip()
    return name, unit


def _looks_like_iso_date(s: str) -> bool:
    return bool(_ISO_DATE_RE.match(s.strip()))


def _coerce_float(raw: str) -> float | None:
    s = (raw or "").strip()
    if not s:
        return None
    # Some CSV exports use the % sign; strip it so the value remains numeric.
    if s.endswith("%"):
        s = s[:-1].strip()
    try:
        return float(s)
    except ValueError:
        return None


def parse_csv_text(text: str) -> tuple[list[ParsedPanel], list[str]]:
    """Pure parser. Returns (panels, warnings). No DB access."""
    warnings: list[str] = []
    if not text or not text.strip():
        warnings.append("CSV is empty.")
        return [], warnings

    reader = csv.reader(io.StringIO(text))
    rows = [r for r in reader if r is not None]

    if len(rows) < 2:
        warnings.append(
            "CSV needs at least two header rows (category banner + "
            "marker headers) before the data.",
        )
        return [], warnings

    # Locate the header row that starts with "Date". The category banner
    # above it is optional and ignored. The reference-range row below it
    # is also ignored (canonical refs drive flagging).
    header_idx: int | None = None
    for i, r in enumerate(rows[:5]):
        if r and r[0].strip().lower() == "date":
            header_idx = i
            break

    if header_idx is None:
        warnings.append(
            "Could not find a 'Date' column header. The marker-headers row "
            "must start with 'Date, Lab, …'.",
        )
        return [], warnings

    headers = rows[header_idx]
    # Skip the optional reference-range row right below the header. It's the
    # next row that does NOT start with an ISO date.
    data_start = header_idx + 1
    if data_start < len(rows) and not _looks_like_iso_date(rows[data_start][0] or ""):
        data_start += 1

    columns: list[tuple[str, str]] = []  # (name, unit) per column, "" for Date/Lab
    for col in headers:
        columns.append(_parse_header(col))

    panels: list[ParsedPanel] = []
    for row_no, row in enumerate(rows[data_start:], start=data_start + 1):
        if not row or not any(cell.strip() for cell in row):
            continue
        date_cell = (row[0] if len(row) > 0 else "").strip()
        if not _looks_like_iso_date(date_cell):
            warnings.append(f"Row {row_no}: skipped — '{date_cell}' is not an ISO date.")
            continue
        lab_cell = (row[1] if len(row) > 1 else "").strip() or None
        panel = ParsedPanel(drawn_at=date_cell, lab_name=lab_cell)
        for idx in range(2, min(len(columns), len(row))):
            name, unit = columns[idx]
            if not name:
                continue
            val = _coerce_float(row[idx])
            if val is None:
                continue
            panel.biomarkers.append({
                "raw_name": name,
                "value": val,
                "unit": unit,
            })
        if panel.biomarkers:
            panels.append(panel)

    return panels, warnings


def _resolve_canonical(
    conn: sqlite3.Connection, raw_name: str,
) -> tuple[str, str | None, "object | None"]:
    """Match raw_name to a canonical ref. Returns (name, display_name, ref)."""
    ref = health_db.find_biomarker_ref_by_alias(conn, raw_name)
    if ref is None:
        return raw_name, None, None
    return ref.name, ref.display_name, ref


def import_csv(
    conn: sqlite3.Connection,
    csv_text: str,
    *,
    on_collision: str = "skip",
) -> ImportSummary:
    """Parse and persist a CSV of bloodwork results.

    ``on_collision`` controls behaviour when a panel with the same
    (drawn_at, lab_name) already exists:

    * ``"skip"`` (default) — leave the existing panel intact.
    * ``"replace"`` — delete the existing panel's biomarkers and derived
      stats, then write the new values into the existing panel id.
    * ``"append"`` — create a new panel anyway (rarely useful; trends
      will see two values for the same date).

    Returns an :class:`ImportSummary`. Caller owns the commit so the
    import can be rolled back as a whole on application-level failure.
    """
    if on_collision not in ("skip", "replace", "append"):
        raise ValueError(f"unknown on_collision: {on_collision!r}")

    parsed, warnings = parse_csv_text(csv_text)
    summary = ImportSummary(warnings=list(warnings))
    if not parsed:
        return summary

    settings = health_db.get_settings(conn)
    sex = settings.get("sex")
    stat_fanout_by_marker = {
        marker_name.lower(): (metric_key, default_unit)
        for metric_key, (marker_name, default_unit) in _STAT_FANOUT.items()
    }

    for parsed_panel in parsed:
        summary.rows_processed += 1

        existing = health_db.find_panel_collision(
            conn,
            drawn_at=parsed_panel.drawn_at,
            lab_name=parsed_panel.lab_name,
        )

        if existing is not None and on_collision == "skip":
            summary.panels_skipped += 1
            continue

        if existing is not None and on_collision == "replace":
            health_db.delete_stats_for_panel(conn, existing.id)
            conn.execute(
                "DELETE FROM biomarkers WHERE panel_id = ?", (existing.id,),
            )
            health_db.update_panel(conn, existing.id, draft=False)
            panel_id = existing.id
            summary.panels_replaced += 1
        else:
            panel_id = health_db.insert_panel(
                conn,
                drawn_at=parsed_panel.drawn_at,
                lab_name=parsed_panel.lab_name,
                draft=False,
            )

        summary.panels_created += 1

        enriched: list[dict] = []
        for b in parsed_panel.biomarkers:
            canonical_name, display_name, ref = _resolve_canonical(
                conn, b["raw_name"],
            )
            canonical_low = canonical_high = None
            if ref is not None:
                canonical_low, canonical_high = pick_canonical_range(ref, sex)
                if canonical_low is None and canonical_high is None:
                    canonical_low, canonical_high = widest_canonical_range(ref)
            flag = compute_flag(
                b["value"], low=canonical_low, high=canonical_high,
            )
            enriched.append({
                "name": canonical_name,
                "display_name": display_name or (
                    b["raw_name"] if canonical_name != b["raw_name"] else None
                ),
                "value": b["value"],
                "unit": b["unit"],
                "ref_range_low": None,
                "ref_range_high": None,
                "flag": flag,
            })

        health_db.replace_biomarkers(conn, panel_id, enriched)
        summary.biomarkers_created += len(enriched)

        # BP / resting-HR fan-out so the unified time series picks them up.
        for b in enriched:
            hit = stat_fanout_by_marker.get(b["name"].lower())
            if not hit:
                continue
            metric_key, default_unit = hit
            health_db.insert_stat(
                conn,
                metric=metric_key,
                value=b["value"],
                unit=b["unit"] or default_unit,
                measured_at=parsed_panel.drawn_at,
                source="lab_panel",
                source_ref=panel_id,
            )

    return summary


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def _category_order(refs: list) -> list[str]:
    """Return distinct categories in the order they appear in refs."""
    seen: list[str] = []
    for r in refs:
        if r.category not in seen:
            seen.append(r.category)
    return seen


def _format_range(low: float | None, high: float | None) -> str:
    if low is None and high is None:
        return ""
    if low is None:
        return f"≤{high}"
    if high is None:
        return f"≥{low}"
    return f"{low}-{high}"


def _category_banner(name: str) -> str:
    """Match the spreadsheet's banner casing (uppercase) and special names."""
    overrides = {
        "CBC": "MORPHOLOGY",
        "CMP": "CHEMISTRY",
        "Liver": "LIVER",
        "Lipid": "LIPID PANEL",
        "Thyroid": "THYROID",
        "Iron": "IRON",
        "Vitamins": "VITAMINS",
        "Inflammation": "INFLAMMATION",
        "Hormones": "HORMONES",
        "Diabetes": "DIABETES",
        "Other": "OTHER",
    }
    return overrides.get(name, name.upper())


def export_csv(conn: sqlite3.Connection) -> str:
    """Emit confirmed panels as a CSV in the import format.

    Columns are every (canonical) biomarker name that has at least one
    measurement, grouped by category in the canonical ref ordering, with
    unknown markers landing in a trailing "Other" group.

    Rows are sorted by ``drawn_at`` descending (newest first), matching
    the order most people scan in.
    """
    panels = health_db.list_panels(conn, include_drafts=False, limit=10_000)
    if not panels:
        return ""

    panels_sorted = sorted(panels, key=lambda p: p.drawn_at, reverse=True)
    refs = health_db.list_biomarker_refs(conn)
    ref_by_name = {r.name: r for r in refs}

    # marker_name -> dict of (panel_id -> {value, unit}) for column build
    observed: dict[str, dict[int, dict]] = {}
    marker_unit: dict[str, str] = {}
    for p in panels_sorted:
        for b in health_db.list_biomarkers_for_panel(conn, p.id):
            observed.setdefault(b.name, {})[p.id] = {
                "value": b.value, "unit": b.unit,
            }
            marker_unit.setdefault(b.name, b.unit or "")

    # Group markers by category, preserving ref ordering inside categories.
    cats: dict[str, list[str]] = {}
    for r in refs:
        if r.name in observed:
            cats.setdefault(r.category, []).append(r.name)
    # Unknown markers (no ref) land in "Other".
    for name in observed:
        if name not in ref_by_name:
            cats.setdefault("Other", []).append(name)

    cat_order = [c for c in _category_order(refs) if c in cats]
    for c in cats:
        if c not in cat_order:
            cat_order.append(c)

    columns: list[str] = []
    cat_spans: list[tuple[str, int]] = []  # (category_banner, span)
    for c in cat_order:
        markers = cats[c]
        cat_spans.append((c, len(markers)))
        columns.extend(markers)

    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")

    # Row 1: category banner — two leading empty columns (Date, Lab),
    # then the section name on the first column of its span and blanks
    # under the rest.
    banner = ["", ""]
    for cat, span in cat_spans:
        banner.append(_category_banner(cat))
        banner.extend([""] * (span - 1))
    writer.writerow(banner)

    # Row 2: Marker (unit) headers.
    header = ["Date", "Lab"]
    for name in columns:
        unit = marker_unit.get(name, "")
        if not unit:
            ref = ref_by_name.get(name)
            unit = (ref.default_unit if ref else "")
        header.append(f"{name} ({unit})" if unit else name)
    writer.writerow(header)

    # Row 3: reference range (canonical) per column.
    ref_row = ["", ""]
    for name in columns:
        ref = ref_by_name.get(name)
        if ref is None:
            ref_row.append("")
            continue
        low, high = widest_canonical_range(ref)
        ref_row.append(_format_range(low, high))
    writer.writerow(ref_row)

    # Data rows — one per panel.
    for p in panels_sorted:
        row = [p.drawn_at, p.lab_name or ""]
        for name in columns:
            cell = observed.get(name, {}).get(p.id)
            row.append("" if cell is None else _format_value(cell["value"]))
        writer.writerow(row)

    return buf.getvalue()


def _format_value(v: float) -> str:
    """Format a float for export: drop trailing zeros, keep ints clean."""
    if v == int(v):
        return str(int(v))
    return f"{v:g}"
