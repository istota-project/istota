"""Tests for the immunization API routes + parser."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.health import db as health_db
from istota.health._migrate import ensure_initialised
from istota.health.models import HealthContext
from istota.health.parser import parse_paste
from istota.health.routes import (
    get_user_context,
    require_auth,
    router,
)
from istota.health.workspace import synthesize_health_context


@pytest.fixture
def ctx(tmp_path: Path) -> HealthContext:
    c = synthesize_health_context("alice", tmp_path / "workspace")
    ensure_initialised(c)
    return c


@pytest.fixture
def client(ctx: HealthContext) -> TestClient:
    app = FastAPI()
    app.include_router(router, prefix="/istota/api/health")
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    app.dependency_overrides[get_user_context] = lambda: ctx
    return TestClient(app)


class TestParser:
    def _refs(self, ctx):
        with health_db.connect(ctx.db_path) as conn:
            return health_db.list_immunization_refs(conn)

    def test_mychart_shape(self, ctx):
        refs = self._refs(ctx)
        text = (
            "INFS Pres Free 6mos-Adult (Fluzone trivalent) (influenza) (Given 11/28/2025)\n"
            "Tdap (Tetanus, diphtheria, acellular pertussis) (Given 12/1/2016)\n"
            "TYDvi (Typhoid, ViCPs) (Given 10/23/2023)\n"
        )
        rows = parse_paste(text, refs)
        names = [r.name for r in rows]
        assert names == ["Influenza", "Tdap", "Typhoid"]
        assert rows[0].date_given == "2025-11-28"
        assert rows[1].date_given == "2016-12-01"
        assert rows[2].date_given == "2023-10-23"
        assert all(r.confidence == "high" for r in rows)

    def test_iso_shape(self, ctx):
        refs = self._refs(ctx)
        text = "Influenza 2025-11-28\nShingrix dose 1 2024-09-15\n"
        rows = parse_paste(text, refs)
        assert rows[0].name == "Influenza"
        assert rows[0].date_given == "2025-11-28"
        assert rows[1].name == "Shingles"
        assert rows[1].date_given == "2024-09-15"

    def test_unknown_family_keeps_source(self, ctx):
        refs = self._refs(ctx)
        text = "Some Weird Vaccine Brand (Given 1/2/2024)\n"
        rows = parse_paste(text, refs)
        assert rows[0].name == "Unknown"
        assert rows[0].date_given == "2024-01-02"
        assert rows[0].notes == "Some Weird Vaccine Brand (Given 1/2/2024)"

    def test_free_text_fallback(self, ctx):
        refs = self._refs(ctx)
        text = "Got my flu shot at the pharmacy\n"
        rows = parse_paste(text, refs)
        # 'flu' alias matches Influenza; no date → manual.
        assert rows[0].name == "Influenza"
        assert rows[0].date_given is None
        assert rows[0].confidence == "manual"

    def test_empty_input(self, ctx):
        refs = self._refs(ctx)
        assert parse_paste("", refs) == []
        assert parse_paste("\n\n   \n", refs) == []

    def test_two_digit_year_pivot(self, ctx):
        refs = self._refs(ctx)
        rows = parse_paste("Influenza (Given 11/28/68)\n", refs)
        # 68 → 2068 (matches the <70 pivot).
        assert rows[0].date_given == "2068-11-28"
        rows = parse_paste("Influenza (Given 11/28/85)\n", refs)
        # 85 → 1985.
        assert rows[0].date_given == "1985-11-28"

    def test_longest_alias_wins(self, ctx):
        refs = self._refs(ctx)
        # 'COVID-19 PF' should resolve to COVID-19, not whatever else.
        rows = parse_paste(
            "COVID-19 PF (Janssen/J&J), External Administration (Given 3/17/2021)\n",
            refs,
        )
        assert rows[0].name == "COVID-19"
        assert rows[0].date_given == "2021-03-17"


class TestCrudRoutes:
    def test_create_and_get(self, client):
        resp = client.post("/istota/api/health/immunizations", json={
            "name": "Influenza",
            "date_given": "2025-11-28",
            "product_name": "Fluzone trivalent",
            "facility": "CVS Pharmacy",
        })
        assert resp.status_code == 200, resp.text
        iid = resp.json()["id"]

        got = client.get(f"/istota/api/health/immunizations/{iid}").json()
        assert got["immunization"]["name"] == "Influenza"
        assert got["immunization"]["product_name"] == "Fluzone trivalent"

    def test_missing_name_or_date(self, client):
        resp = client.post("/istota/api/health/immunizations", json={
            "date_given": "2025-11-28",
        })
        assert resp.status_code == 400
        resp = client.post("/istota/api/health/immunizations", json={
            "name": "Influenza",
        })
        assert resp.status_code == 400

    def test_list_filters(self, client):
        for d in ("2023-10-23", "2025-11-28"):
            client.post("/istota/api/health/immunizations", json={
                "name": "Influenza", "date_given": d,
            })
        client.post("/istota/api/health/immunizations", json={
            "name": "Tdap", "date_given": "2016-12-01",
        })
        rows = client.get(
            "/istota/api/health/immunizations?name=Influenza",
        ).json()["immunizations"]
        assert all(r["name"] == "Influenza" for r in rows)
        assert len(rows) == 2

    def test_update_and_delete(self, client):
        resp = client.post("/istota/api/health/immunizations", json={
            "name": "Tdap", "date_given": "2016-12-01",
        })
        iid = resp.json()["id"]
        upd = client.put(
            f"/istota/api/health/immunizations/{iid}",
            json={"lot_number": "ABC123"},
        )
        assert upd.status_code == 200
        got = client.get(f"/istota/api/health/immunizations/{iid}").json()
        assert got["immunization"]["lot_number"] == "ABC123"

        d = client.delete(f"/istota/api/health/immunizations/{iid}")
        assert d.status_code == 200
        got = client.get(f"/istota/api/health/immunizations/{iid}")
        assert got.status_code == 404


class TestRefsAndCoverageRoutes:
    def test_refs_endpoint(self, client):
        refs = client.get("/istota/api/health/immunizations/refs").json()
        names = {r["name"] for r in refs["refs"]}
        assert "Influenza" in names
        assert "Tdap" in names

    def test_coverage_no_rows(self, client):
        resp = client.get("/istota/api/health/immunizations/coverage").json()
        # No doses → most refs are never_recorded; series_incomplete for
        # lifetime_after_series; risk_based for risk_based.
        statuses = {c["status"] for c in resp["coverage"]}
        assert "never_recorded" in statuses
        assert resp["other"] == []

    def test_coverage_with_rows(self, client):
        client.post("/istota/api/health/immunizations", json={
            "name": "Influenza", "date_given": "2025-09-01",
        })
        resp = client.get("/istota/api/health/immunizations/coverage").json()
        flu = next(c for c in resp["coverage"] if c["name"] == "Influenza")
        assert flu["last_given"] == "2025-09-01"
        assert flu["dose_count"] == 1

    def test_other_bucket(self, client):
        client.post("/istota/api/health/immunizations", json={
            "name": "Custom Trial Vaccine", "date_given": "2024-01-01",
        })
        resp = client.get("/istota/api/health/immunizations/coverage").json()
        other_names = {o["name"] for o in resp["other"]}
        assert "Custom Trial Vaccine" in other_names


class TestExtractRoute:
    def test_extract_returns_fallback_when_brain_unavailable(self, client):
        # No app.state.istota_config → brain unavailable → fallback empty rows
        # with a warning, but still a 200.
        files = {"file": ("vaccines.png", b"\x89PNG\r\n\x1a\nfake", "image/png")}
        resp = client.post(
            "/istota/api/health/immunizations/extract",
            files=files,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "rows" in body
        assert "warnings" in body
        assert any("unavailable" in w.lower() for w in body["warnings"])

    def test_extract_rejects_empty_upload(self, client):
        files = {"file": ("vaccines.png", b"", "image/png")}
        resp = client.post(
            "/istota/api/health/immunizations/extract",
            files=files,
        )
        assert resp.status_code == 400


class TestExtractParser:
    """LLM response parsing (the brain-call path) — tested in isolation."""

    def test_parse_object_envelope(self):
        from istota.health.immunization_ocr import _parse_llm_response

        raw = (
            '{"immunizations": ['
            '{"name": "Influenza", "product_name": "Fluzone", '
            '"date_given": "2025-11-28", "confidence": "high"}'
            ']}'
        )
        out = _parse_llm_response(raw)
        assert len(out) == 1
        assert out[0]["name"] == "Influenza"
        assert out[0]["date_given"] == "2025-11-28"
        assert out[0]["confidence"] == "high"

    def test_parse_bare_array(self):
        from istota.health.immunization_ocr import _parse_llm_response

        raw = '[{"name": "Tdap", "date_given": "12/1/2016"}]'
        out = _parse_llm_response(raw)
        assert out[0]["name"] == "Tdap"
        # US date normalised to ISO.
        assert out[0]["date_given"] == "2016-12-01"

    def test_parse_strips_code_fences(self):
        from istota.health.immunization_ocr import _parse_llm_response

        raw = (
            "Sure, here you go:\n"
            "```json\n"
            '{"immunizations": [{"name": "MMR", "date_given": "1990-01-01"}]}\n'
            "```"
        )
        out = _parse_llm_response(raw)
        assert out[0]["name"] == "MMR"

    def test_parse_handles_two_digit_year(self):
        from istota.health.immunization_ocr import _parse_llm_response

        raw = '[{"name": "Tdap", "date_given": "12/1/85"}]'
        out = _parse_llm_response(raw)
        assert out[0]["date_given"] == "1985-12-01"

    def test_parse_marks_no_date_as_manual(self):
        from istota.health.immunization_ocr import _parse_llm_response

        raw = '[{"name": "Influenza", "date_given": null}]'
        out = _parse_llm_response(raw)
        assert out[0]["date_given"] is None
        assert out[0]["confidence"] == "manual"

    def test_parse_unknown_name_defaults(self):
        from istota.health.immunization_ocr import _parse_llm_response

        raw = '[{"date_given": "2024-01-01"}]'
        out = _parse_llm_response(raw)
        assert out[0]["name"] == "Unknown"


class TestParseAndBulkRoutes:
    def test_parse_endpoint(self, client):
        text = "Tdap (Given 12/1/2016)\n"
        resp = client.post(
            "/istota/api/health/immunizations/parse",
            json={"text": text},
        )
        assert resp.status_code == 200
        rows = resp.json()["rows"]
        assert rows[0]["name"] == "Tdap"
        assert rows[0]["date_given"] == "2016-12-01"

    def test_bulk_roundtrip(self, client):
        parsed = client.post(
            "/istota/api/health/immunizations/parse",
            json={"text":
                "Influenza (Given 11/28/2025)\n"
                "Tdap (Given 12/1/2016)\n"
            },
        ).json()["rows"]
        resp = client.post(
            "/istota/api/health/immunizations/bulk",
            json={"rows": parsed},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 2
        listing = client.get(
            "/istota/api/health/immunizations",
        ).json()["immunizations"]
        assert {r["name"] for r in listing} == {"Influenza", "Tdap"}

    def test_bulk_rejects_missing_fields(self, client):
        resp = client.post(
            "/istota/api/health/immunizations/bulk",
            json={"rows": [{"name": "Influenza"}]},
        )
        assert resp.status_code == 400


class TestDashboardAndSummary:
    def test_dashboard_has_immunizations(self, client):
        client.post("/istota/api/health/immunizations", json={
            "name": "Influenza", "date_given": "2020-09-01",
        })
        resp = client.get("/istota/api/health/dashboard").json()
        assert "immunizations" in resp
        assert resp["immunizations"]["overdue_count"] >= 1
        assert resp["immunizations"]["last_given"]["name"] == "Influenza"

    def test_history_summary_has_immunizations(self, client):
        client.post("/istota/api/health/immunizations", json={
            "name": "Tdap", "date_given": "2010-01-01",
        })
        resp = client.get("/istota/api/health/history/summary").json()
        assert "immunizations" in resp
        # Tdap from 2010 → overdue (more than 10y).
        action = resp["immunizations"]["action_needed"]
        assert any(c["name"] == "Tdap" for c in action)


class TestExplainerRoute:
    def test_explainer_skipped_for_up_to_date(self, client):
        # No special status → not overdue / series_incomplete / etc.
        # An up_to_date or risk_based vaccine returns source="skipped".
        client.post("/istota/api/health/immunizations", json={
            "name": "Influenza",
            "date_given": "2026-05-01",  # very recent, well within window
        })
        resp = client.get(
            "/istota/api/health/immunizations/Influenza/explainer",
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"] == "skipped"

    def test_explainer_returns_fallback_when_brain_unavailable(self, client):
        # No config wired in the test app → _call_brain returns None → fallback.
        client.post("/istota/api/health/immunizations", json={
            "name": "Tdap", "date_given": "2005-01-01",
        })
        resp = client.get("/istota/api/health/immunizations/Tdap/explainer")
        assert resp.status_code == 200
        body = resp.json()
        # Status is overdue → eligible for explainer → falls back since no brain.
        assert body["source"] == "fallback"
        assert body["status"] == "overdue"
        assert isinstance(body["why_it_matters"], list)
        assert isinstance(body["considerations"], list)
        assert body["disclaimer"]

    def test_explainer_unknown_vaccine_404(self, client):
        resp = client.get(
            "/istota/api/health/immunizations/Notarealvaccine/explainer",
        )
        assert resp.status_code == 404


class TestExplainerParsing:
    def test_parse_strict_json(self):
        from istota.health.immunization_explainer import _parse_response

        raw = (
            '{"summary": "x", "why_it_matters": ["a", "b"], '
            '"considerations": ["c", "d"]}'
        )
        out = _parse_response(raw)
        assert out is not None
        assert out["summary"] == "x"
        assert out["why_it_matters"] == ["a", "b"]
        assert out["considerations"] == ["c", "d"]

    def test_parse_with_code_fences(self):
        from istota.health.immunization_explainer import _parse_response

        raw = (
            "```json\n"
            '{"summary": "x", "why_it_matters": ["a"], "considerations": ["c"]}\n'
            "```"
        )
        assert _parse_response(raw) is not None

    def test_parse_rejects_missing_fields(self):
        from istota.health.immunization_explainer import _parse_response

        assert _parse_response('{"summary": "x"}') is None
        assert _parse_response('{"why_it_matters": ["a"]}') is None
        assert _parse_response("not json at all") is None

    def test_parse_rejects_non_list_arrays(self):
        from istota.health.immunization_explainer import _parse_response

        raw = (
            '{"summary": "x", "why_it_matters": "a single string", '
            '"considerations": ["c"]}'
        )
        assert _parse_response(raw) is None
