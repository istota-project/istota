"""Tests for the health OCR pipeline.

The pipeline is multi-stage:
* text extraction (PDF / image) — exercises the dispatch path.
* LLM extraction — mocked via a fake ``_call_brain``.
* Sanity-check warnings — pure function on extracted dicts.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from istota.health import db as health_db
from istota.health import ocr as health_ocr
from istota.health._migrate import ensure_initialised
from istota.health.workspace import synthesize_health_context


@pytest.fixture
def ctx(tmp_path):
    c = synthesize_health_context("alice", tmp_path / "workspace")
    ensure_initialised(c)
    return c


def _seed_panel_with_source(ctx, *, text: str = "") -> int:
    """Create a panel + a fake source file on disk."""
    with health_db.connect(ctx.db_path) as conn:
        pid = health_db.insert_panel(
            conn,
            drawn_at="2026-05-08",
            lab_name="Quest",
            source_mime="text/plain",
            draft=True,
        )
        panel_dir = ctx.uploads_dir / str(pid)
        panel_dir.mkdir(parents=True, exist_ok=True)
        target = panel_dir / "original.txt"
        target.write_text(text)
        rel = str(target.relative_to(ctx.uploads_dir))
        conn.execute(
            "UPDATE panels SET source_file = ? WHERE id = ?", (rel, pid),
        )
        conn.commit()
    return pid


class TestStripCodeFences:
    def test_plain_passes_through(self):
        assert health_ocr._strip_code_fences('{"a": 1}') == '{"a": 1}'

    def test_strips_json_fence(self):
        raw = '```json\n{"a": 1}\n```'
        assert health_ocr._strip_code_fences(raw) == '{"a": 1}'


class TestParseLlmJson:
    def test_object_with_biomarkers_key(self):
        raw = '{"biomarkers": [{"name": "WBC", "value": 7, "unit": "10^3/uL"}]}'
        out = health_ocr._parse_llm_json(raw)
        assert out == [{"name": "WBC", "value": 7, "unit": "10^3/uL"}]

    def test_bare_array(self):
        raw = '[{"name": "WBC", "value": 7, "unit": "10^3/uL"}]'
        out = health_ocr._parse_llm_json(raw)
        assert out[0]["name"] == "WBC"

    def test_object_in_prose(self):
        raw = (
            'Here is the JSON:\n'
            '{"biomarkers": [{"name": "Hemoglobin", "value": 14, "unit": "g/dL"}]}'
        )
        out = health_ocr._parse_llm_json(raw)
        assert out[0]["name"] == "Hemoglobin"

    def test_garbage_returns_empty(self):
        assert health_ocr._parse_llm_json("not json at all") == []


class TestSanityCheck:
    def test_obviously_wrong_value_warns(self):
        refs_by_name = {
            "WBC": {"ref_range_low": 4.0, "ref_range_high": 11.0},
        }
        # 200 is far above 11 × 10 → warning.
        warnings = health_ocr._sanity_check(
            [{"name": "WBC", "value": 200, "unit": "10^3/uL"}],
            refs_by_name,
        )
        assert warnings and "WBC" in warnings[0]

    def test_in_range_no_warning(self):
        refs_by_name = {
            "WBC": {"ref_range_low": 4.0, "ref_range_high": 11.0},
        }
        assert health_ocr._sanity_check(
            [{"name": "WBC", "value": 7.5, "unit": "10^3/uL"}],
            refs_by_name,
        ) == []

    def test_unknown_biomarker_no_warning(self):
        # Unknown names don't trip the sanity check — the user owns the call.
        assert health_ocr._sanity_check(
            [{"name": "MyUnusualMarker", "value": 1000, "unit": "x"}],
            {},
        ) == []


class TestExtractFromPanel:
    def test_no_source_file_returns_empty_with_warning(self, ctx):
        # Panel exists but has no source_file column set.
        with health_db.connect(ctx.db_path) as conn:
            pid = health_db.insert_panel(
                conn, drawn_at="2026-05-08", draft=True,
            )
            conn.commit()
            panel = health_db.get_panel(conn, pid)
        result = health_ocr.extract_from_panel(ctx, panel)
        assert result["biomarkers"] == []
        assert any("manually" in w.lower() for w in result["warnings"])

    def test_extraction_when_brain_returns_json(self, ctx):
        # Use a plain text file so the image/PDF dispatch falls through.
        # We mock extract_text directly to bypass mime gating.
        with health_db.connect(ctx.db_path) as conn:
            pid = health_db.insert_panel(
                conn,
                drawn_at="2026-05-08",
                source_mime="text/plain",
                draft=True,
            )
            conn.commit()
            panel = health_db.get_panel(conn, pid)

        fake_response = (
            '{"biomarkers": ['
            '{"name": "Hemoglobin", "value": 14.8, "unit": "g/dL", '
            '"ref_range_low": 13.5, "ref_range_high": 17.5}, '
            '{"name": "WBC", "value": 7.2, "unit": "10^3/uL"}'
            ']}'
        )

        with patch.object(
            health_ocr, "extract_text",
            return_value="Hemoglobin 14.8 g/dL\nWBC 7.2 10^3/uL\n" * 20,
        ), patch.object(health_ocr, "_call_brain", return_value=fake_response):
            result = health_ocr.extract_from_panel(ctx, panel)

        names = {b["name"] for b in result["biomarkers"]}
        assert names == {"Hemoglobin", "WBC"}
        # OCR text was persisted on the panel row.
        with health_db.connect(ctx.db_path) as conn:
            panel = health_db.get_panel(conn, pid)
        assert (panel.ocr_text or "").startswith("Hemoglobin")

    def test_extraction_when_brain_unavailable(self, ctx):
        with health_db.connect(ctx.db_path) as conn:
            pid = health_db.insert_panel(
                conn, drawn_at="2026-05-08",
                source_mime="text/plain", draft=True,
            )
            conn.commit()
            panel = health_db.get_panel(conn, pid)
        with patch.object(
            health_ocr, "extract_text", return_value="some lab text " * 50,
        ), patch.object(health_ocr, "_call_brain", return_value=None):
            result = health_ocr.extract_from_panel(ctx, panel)
        assert result["biomarkers"] == []
        assert any("LLM" in w for w in result["warnings"])
        # Raw text still carries through for diagnostics.
        assert "lab text" in result["raw_text"]
