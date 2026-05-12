"""Tests for the health CSV import/export module.

The CSV format mirrors the spreadsheet people keep offline:

* row 1: category banner (mostly empty + section names)
* row 2: ``Marker (unit)`` headers
* row 3: reference ranges per column (informational, ignored on import)
* rows 4+: ``date, lab, value, value, …``
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from istota.health import csv_io
from istota.health import db as health_db
from istota.health._migrate import ensure_initialised
from istota.health.workspace import synthesize_health_context


# A trimmed-down fixture mirroring the user's real CSV shape.
SAMPLE_CSV = (
    ",,MORPHOLOGY,,,,CHEMISTRY,,LIPID PANEL,,,\n"
    "Date,Lab,WBC (th/mm3),Hgb (g/dL),Hct (%),PLT (th/mm3),"
    "Na (mEq/L),Glucose (mg/dL),Cholesterol (mg/dL),Triglyceride (mg/dL),"
    "HDL (mg/dL),LDL-C (mg/dL)\n"
    ",,4.8-10.5,12.7-16.7,36-50,140-440,134-144,70-118,120-200,30-150,40-60,20-100\n"
    "2024-07-27,\"Kaiser Permanente\",6.4,14.5,43.5,278,139,,229,165,51,148\n"
    "2025-11-28,\"Kaiser Permanente\",6.1,14.6,42.9,272,135,,219,90,55,148\n"
)


@pytest.fixture
def ctx(tmp_path):
    c = synthesize_health_context("alice", tmp_path / "workspace")
    ensure_initialised(c)
    return c


class TestParseColumnHeader:
    def test_simple(self):
        name, unit = csv_io._parse_header("WBC (th/mm3)")
        assert name == "WBC"
        assert unit == "th/mm3"

    def test_no_unit(self):
        name, unit = csv_io._parse_header("Globulin")
        assert name == "Globulin"
        assert unit == ""

    def test_unit_with_slash(self):
        name, unit = csv_io._parse_header("Cholesterol/HDL (ratio)")
        assert name == "Cholesterol/HDL"
        assert unit == "ratio"

    def test_compound_name(self):
        name, unit = csv_io._parse_header("Bili, Ttl (mg/dL)")
        assert name == "Bili, Ttl"
        assert unit == "mg/dL"


class TestParseCsv:
    def test_extracts_panels_and_biomarkers(self):
        parsed, warnings = csv_io.parse_csv_text(SAMPLE_CSV)
        assert warnings == []
        assert len(parsed) == 2

        first = parsed[0]
        assert first.drawn_at == "2024-07-27"
        assert first.lab_name == "Kaiser Permanente"
        marker_names = [b["raw_name"] for b in first.biomarkers]
        # Glucose was empty in row 1 -> skipped.
        assert "Glucose" not in marker_names
        assert "WBC" in marker_names
        assert "LDL-C" in marker_names

    def test_skips_blank_rows(self):
        text = SAMPLE_CSV + "\n,\n"
        parsed, _ = csv_io.parse_csv_text(text)
        assert len(parsed) == 2

    def test_invalid_date_collected_as_warning(self):
        text = SAMPLE_CSV.replace("2024-07-27", "not-a-date")
        parsed, warnings = csv_io.parse_csv_text(text)
        assert len(parsed) == 1  # second row OK
        assert any("not-a-date" in w for w in warnings)

    def test_lenient_two_row_input(self):
        # Single header line + data row is acceptable — category banner and
        # reference-range rows are optional decorations.
        parsed, warnings = csv_io.parse_csv_text("Date,Lab,WBC\n2024-07-27,X,5\n")
        assert len(parsed) == 1
        assert parsed[0].biomarkers[0]["raw_name"] == "WBC"

    def test_missing_date_column(self):
        # No "Date" column header anywhere — bail with a useful warning.
        parsed, warnings = csv_io.parse_csv_text("Foo,Bar\n1,2\n")
        assert parsed == []
        assert any("date" in w.lower() for w in warnings)


class TestImportCsv:
    def test_creates_panels_with_canonical_names(self, ctx):
        with health_db.connect(ctx.db_path) as conn:
            summary = csv_io.import_csv(conn, SAMPLE_CSV)
            conn.commit()

        assert summary.panels_created == 2
        assert summary.panels_skipped_identical == 0
        assert summary.panels_needs_review == 0
        assert summary.biomarkers_created > 0

        with health_db.connect(ctx.db_path) as conn:
            panels = health_db.list_panels(conn, include_drafts=False)
            assert [p.drawn_at for p in panels] == ["2025-11-28", "2024-07-27"]
            bs = health_db.list_biomarkers_for_panel(conn, panels[-1].id)
        names = {b.name for b in bs}
        # Aliases resolved.
        assert "Hemoglobin" in names  # Hgb -> Hemoglobin
        assert "Hematocrit" in names  # Hct -> Hematocrit
        assert "Platelets" in names  # PLT -> Platelets
        assert "Sodium" in names      # Na -> Sodium
        assert "LDL" in names         # LDL-C -> LDL

    def test_imports_set_confirmed_not_draft(self, ctx):
        with health_db.connect(ctx.db_path) as conn:
            csv_io.import_csv(conn, SAMPLE_CSV)
            conn.commit()
            panels = health_db.list_panels(conn, include_drafts=True)
        assert all(p.draft is False for p in panels)

    def test_reimport_is_noop_via_content_hash(self, ctx):
        # Re-importing the exact same CSV is silently skipped — content
        # hash matches and no second panel row is created.
        with health_db.connect(ctx.db_path) as conn:
            csv_io.import_csv(conn, SAMPLE_CSV)
            conn.commit()
            summary = csv_io.import_csv(conn, SAMPLE_CSV)
            conn.commit()
            panels = health_db.list_panels(conn, include_drafts=True)
        assert summary.panels_skipped_identical == 2
        assert summary.panels_created == 0
        assert summary.panels_needs_review == 0
        assert len(panels) == 2  # still just the original two

    def test_same_date_lab_different_content_lands_as_draft(self, ctx):
        # Same (date, lab) as an existing confirmed panel but with one
        # marker value changed — the new row lands as a draft for review,
        # the confirmed original is untouched.
        with health_db.connect(ctx.db_path) as conn:
            csv_io.import_csv(conn, SAMPLE_CSV)
            conn.commit()
            tweaked = SAMPLE_CSV.replace("6.4,14.5,43.5", "6.4,14.5,44.0")
            summary = csv_io.import_csv(conn, tweaked)
            conn.commit()
            panels = health_db.list_panels(conn, include_drafts=True)
        assert summary.panels_needs_review == 1
        # The other row matches exactly → silent skip.
        assert summary.panels_skipped_identical == 1
        assert summary.panels_created == 0
        # 2 confirmed originals + 1 draft.
        assert len(panels) == 3
        drafts = [p for p in panels if p.draft]
        assert len(drafts) == 1

    def test_content_hash_is_order_invariant(self, ctx):
        # Reordering biomarker rows under the hood doesn't bust dedup —
        # the hash is computed on a sorted canonical tuple set.
        biomarkers_a = [
            {"name": "Hemoglobin", "value": 14.0, "unit": "g/dL"},
            {"name": "Hematocrit", "value": 42.0, "unit": "%"},
        ]
        biomarkers_b = list(reversed(biomarkers_a))
        assert (
            health_db.compute_content_hash(biomarkers_a)
            == health_db.compute_content_hash(biomarkers_b)
        )

    def test_flags_computed_against_canonical(self, ctx):
        # Hgb 11.0 below the unisex Hemoglobin low (12.0 for F, 13.5 for M).
        text = (
            ",,MORPHOLOGY\n"
            "Date,Lab,Hgb (g/dL)\n"
            ",,12.7-16.7\n"
            "2024-07-27,Kaiser,11.0\n"
        )
        with health_db.connect(ctx.db_path) as conn:
            # Sex not set -> falls back to general range (unset),
            # but male/female ranges still come into play via the widest
            # canonical range used for "no sex" — same logic as routes.
            health_db.set_setting(conn, "sex", "M")
            csv_io.import_csv(conn, text)
            conn.commit()
            panels = health_db.list_panels(conn, include_drafts=False)
            bs = health_db.list_biomarkers_for_panel(conn, panels[0].id)
        # Male canonical low is 13.5; 11.0 should flag L.
        assert bs[0].flag == "L"

    def test_unknown_marker_stored_as_printed(self, ctx):
        text = (
            ",,OTHER\n"
            "Date,Lab,MyCustomThing (units)\n"
            ",,\n"
            "2024-07-27,Kaiser,42\n"
        )
        with health_db.connect(ctx.db_path) as conn:
            csv_io.import_csv(conn, text)
            conn.commit()
            panels = health_db.list_panels(conn, include_drafts=False)
            bs = health_db.list_biomarkers_for_panel(conn, panels[0].id)
        assert bs[0].name == "MyCustomThing"

    def test_bp_fanout_on_import(self, ctx):
        # BP_Systolic / BP_Diastolic columns fan out to stats.
        text = (
            ",,VITALS\n"
            "Date,Lab,BP_Systolic (mmHg),BP_Diastolic (mmHg)\n"
            ",,90-120,60-80\n"
            "2024-07-27,Kaiser,128,82\n"
        )
        with health_db.connect(ctx.db_path) as conn:
            csv_io.import_csv(conn, text)
            conn.commit()
            stats = health_db.list_stats(conn)
        metrics = {s.metric for s in stats}
        assert "blood_pressure_systolic" in metrics
        assert "blood_pressure_diastolic" in metrics


class TestExportCsv:
    def test_roundtrip(self, ctx):
        with health_db.connect(ctx.db_path) as conn:
            csv_io.import_csv(conn, SAMPLE_CSV)
            conn.commit()
            exported = csv_io.export_csv(conn)
        # Two data rows + 3 header rows.
        lines = exported.strip().splitlines()
        assert len(lines) == 5
        # Date column header on row 2.
        assert lines[1].split(",")[0] == "Date"
        # Re-import the export — content-hash dedup makes it a true noop.
        with health_db.connect(ctx.db_path) as conn:
            summary = csv_io.import_csv(conn, exported)
            conn.commit()
        assert summary.panels_created == 0
        assert summary.panels_skipped_identical == 2

    def test_empty_db_emits_header_only(self, ctx):
        with health_db.connect(ctx.db_path) as conn:
            out = csv_io.export_csv(conn)
        # Header rows + no data lines.
        assert out.strip().splitlines() == []  # nothing to export

    def test_deferred_replay(self, ctx, tmp_path):
        """The deferred ``import_csv`` op (from sandbox CLI) replays via the
        scheduler and ends up in the same state as a direct in-process import.
        """
        import json
        from unittest.mock import MagicMock

        from istota import health as health_pkg
        from istota import scheduler_deferred
        from istota.health import _loader as health_loader

        src = tmp_path / "bw.csv"
        src.write_text(SAMPLE_CSV)

        # Per-task deferred-ops file as the CLI would have written it.
        user_temp = tmp_path / "user_temp"
        user_temp.mkdir()
        task_id = 999
        ops_file = user_temp / f"task_{task_id}_health_ops.json"
        ops_file.write_text(json.dumps([
            {"op": "import_csv", "source_path": str(src)},
        ]))

        # Stub the loader on the package namespace the deferred handler imports.
        fake_resolve = MagicMock(return_value=ctx)
        original_resolve = health_pkg.resolve_for_user
        health_pkg.resolve_for_user = fake_resolve
        try:
            fake_task = MagicMock(id=task_id, user_id="alice")
            fake_config = MagicMock()
            count = scheduler_deferred._process_deferred_health_ops(
                fake_config, fake_task, user_temp,
            )
        finally:
            health_pkg.resolve_for_user = original_resolve

        assert count == 1
        assert not ops_file.exists()

        with health_db.connect(ctx.db_path) as conn:
            panels = health_db.list_panels(conn, include_drafts=False)
        assert len(panels) == 2

    def test_excludes_drafts(self, ctx):
        # One confirmed + one draft panel.
        with health_db.connect(ctx.db_path) as conn:
            confirmed = health_db.insert_panel(
                conn, drawn_at="2024-01-01", lab_name="X", draft=False,
            )
            health_db.insert_biomarker(
                conn, panel_id=confirmed, name="Hemoglobin",
                value=14.0, unit="g/dL",
            )
            draft = health_db.insert_panel(
                conn, drawn_at="2024-02-01", lab_name="Y", draft=True,
            )
            health_db.insert_biomarker(
                conn, panel_id=draft, name="Hemoglobin",
                value=15.0, unit="g/dL",
            )
            conn.commit()
            out = csv_io.export_csv(conn)
        # Only the confirmed panel row should be present.
        rows = out.strip().splitlines()[3:]
        assert len(rows) == 1
        assert "2024-01-01" in rows[0]
