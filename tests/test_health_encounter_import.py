"""Tests for the encounter extract + bulk-import routes and LLM parser."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.health import db as health_db
from istota.health._migrate import ensure_initialised
from istota.health.models import HealthContext
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


class TestExtractParser:
    def test_parse_object_envelope(self):
        from istota.health.encounter_ocr import _parse_llm_response

        raw = (
            '{"encounters": ['
            '{"encounter_date": "2026-04-14", "encounter_type": "visit",'
            ' "provider": "Dr. Smith", "facility": "Kaiser",'
            ' "specialty": "primary care", "reason": "Annual physical",'
            ' "notes": "All normal.", "diagnoses": [], "confidence": "high"}'
            ']}'
        )
        rows, dropped = _parse_llm_response(raw)
        assert dropped == 0
        assert len(rows) == 1
        r = rows[0]
        assert r["encounter_date"] == "2026-04-14"
        assert r["encounter_type"] == "visit"
        assert r["provider"] == "Dr. Smith"
        assert r["diagnoses"] == []

    def test_parse_bare_array(self):
        from istota.health.encounter_ocr import _parse_llm_response

        raw = (
            '[{"encounter_date": "4/14/2026", "encounter_type": "ER",'
            ' "diagnoses": []}]'
        )
        rows, _ = _parse_llm_response(raw)
        assert rows[0]["encounter_date"] == "2026-04-14"
        # "ER" → canonical "er".
        assert rows[0]["encounter_type"] == "er"

    def test_parse_strips_code_fences(self):
        from istota.health.encounter_ocr import _parse_llm_response

        raw = (
            "Sure, here:\n"
            "```json\n"
            '{"encounters": [{"encounter_date": "2020-01-15",'
            ' "encounter_type": "visit", "diagnoses": []}]}\n'
            "```"
        )
        rows, _ = _parse_llm_response(raw)
        assert rows[0]["encounter_date"] == "2020-01-15"

    def test_parse_two_digit_year(self):
        from istota.health.encounter_ocr import _parse_llm_response

        # 85 ≥ 70 → 1985
        raw = (
            '[{"encounter_date": "5/3/85", "encounter_type": "visit"}]'
        )
        rows, _ = _parse_llm_response(raw)
        assert rows[0]["encounter_date"] == "1985-05-03"
        # 24 < 70 → 2024 (not in the future, so the row is kept)
        raw = (
            '[{"encounter_date": "5/3/24", "encounter_type": "visit"}]'
        )
        rows, _ = _parse_llm_response(raw)
        assert rows[0]["encounter_date"] == "2024-05-03"

    def test_parse_drops_future_dated_rows(self):
        from istota.health.encounter_ocr import _parse_llm_response

        raw = (
            '[{"encounter_date": "2099-04-01", "encounter_type": "visit"},'
            ' {"encounter_date": "2020-01-15", "encounter_type": "visit"}]'
        )
        rows, dropped = _parse_llm_response(raw)
        assert dropped == 1
        assert len(rows) == 1
        assert rows[0]["encounter_date"] == "2020-01-15"

    def test_parse_normalises_type_aliases(self):
        from istota.health.encounter_ocr import _parse_llm_response

        cases = [
            ("Emergency room", "er"),
            ("Telehealth video visit", "telehealth"),
            ("Inpatient admission", "hospitalization"),
            ("MRI imaging", "imaging"),
            ("Dental cleaning", "dental"),
            ("Surgery procedure", "procedure"),
            ("Annual screen", "screening"),
            ("nonsense", "visit"),
        ]
        for printed, want in cases:
            raw = (
                f'[{{"encounter_date": "2020-01-15",'
                f' "encounter_type": "{printed}"}}]'
            )
            rows, _ = _parse_llm_response(raw)
            assert rows[0]["encounter_type"] == want, printed

    def test_parse_diagnoses_validates_status_and_severity(self):
        from istota.health.encounter_ocr import _parse_llm_response

        raw = (
            '[{"encounter_date": "2020-01-15", "encounter_type": "visit",'
            ' "diagnoses": ['
            '{"name": "Hypertension", "icd10": "I10", "status": "chronic",'
            ' "severity": "mild"},'
            '{"name": "Bogus", "status": "made-up", "severity": "extreme"},'
            '{"status": "active"}'  # no name → dropped
            ']}]'
        )
        rows, _ = _parse_llm_response(raw)
        dx = rows[0]["diagnoses"]
        assert len(dx) == 2
        assert dx[0]["name"] == "Hypertension"
        assert dx[0]["status"] == "chronic"
        assert dx[0]["severity"] == "mild"
        # Bogus values fall back to defaults.
        assert dx[1]["status"] == "active"
        assert dx[1]["severity"] is None

    def test_parse_missing_date_keeps_row_low_confidence(self):
        from istota.health.encounter_ocr import _parse_llm_response

        raw = (
            '[{"encounter_date": null, "encounter_type": "visit"}]'
        )
        rows, _ = _parse_llm_response(raw)
        assert rows[0]["encounter_date"] is None
        assert rows[0]["confidence"] == "low"


class TestExtractRoute:
    def test_extract_returns_fallback_when_brain_unavailable(self, client):
        files = {"file": ("visit.png", b"\x89PNG\r\n\x1a\nfake", "image/png")}
        resp = client.post(
            "/istota/api/health/encounters/extract",
            files=files,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert "rows" in body
        assert "warnings" in body
        assert any("unavailable" in w.lower() for w in body["warnings"])

    def test_extract_rejects_empty_upload(self, client):
        files = {"file": ("visit.png", b"", "image/png")}
        resp = client.post(
            "/istota/api/health/encounters/extract",
            files=files,
        )
        assert resp.status_code == 400

    def test_extract_invokes_extract_from_file(self, client, monkeypatch):
        captured: dict = {}

        def _fake(path: Path, mime: str, *, config=None) -> dict:
            captured["path"] = path
            captured["mime"] = mime
            return {"rows": [], "mode": "vision", "warnings": []}

        from istota.health import encounter_ocr as enc_mod
        monkeypatch.setattr(enc_mod, "extract_from_file", _fake)

        files = {"file": ("visit.pdf", b"%PDF-1.4 fake", "application/pdf")}
        resp = client.post(
            "/istota/api/health/encounters/extract",
            files=files,
        )
        assert resp.status_code == 200
        assert "path" in captured
        assert captured["mime"] == "application/pdf"


class TestBulkRoute:
    def _row(self, **overrides) -> dict:
        base = {
            "encounter_date": "2020-01-15",
            "encounter_type": "visit",
            "provider": "Dr. Smith",
            "facility": "Kaiser",
            "specialty": "primary care",
            "reason": "checkup",
            "notes": "all good",
            "diagnoses": [],
            "confidence": "high",
        }
        base.update(overrides)
        return base

    def test_bulk_inserts_encounter(self, client):
        resp = client.post(
            "/istota/api/health/encounters/bulk",
            json={"rows": [self._row()]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert len(body["ids"]) == 1
        listing = client.get(
            "/istota/api/health/encounters",
        ).json()["encounters"]
        assert len(listing) == 1
        assert listing[0]["provider"] == "Dr. Smith"

    def test_bulk_inserts_linked_diagnoses(self, client):
        row = self._row(diagnoses=[
            {
                "name": "Hypertension",
                "icd10": "I10",
                "status": "chronic",
                "severity": "mild",
            },
        ])
        resp = client.post(
            "/istota/api/health/encounters/bulk",
            json={"rows": [row]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["count"] == 1
        assert len(body["diagnosis_ids"]) == 1
        # Encounter detail endpoint shows the linked diagnosis.
        eid = body["ids"][0]
        detail = client.get(
            f"/istota/api/health/encounters/{eid}",
        ).json()
        assert len(detail["diagnoses"]) == 1
        d = detail["diagnoses"][0]
        assert d["name"] == "Hypertension"
        assert d["status"] == "chronic"
        assert d["severity"] == "mild"
        assert d["icd10"] == "I10"
        # date_diagnosed defaults to the encounter date.
        assert d["date_diagnosed"] == "2020-01-15"

    def test_bulk_rejects_missing_date(self, client):
        resp = client.post(
            "/istota/api/health/encounters/bulk",
            json={"rows": [{"encounter_type": "visit"}]},
        )
        assert resp.status_code == 400
        assert "encounter_date" in resp.json()["error"]

    def test_bulk_rejects_missing_type(self, client):
        resp = client.post(
            "/istota/api/health/encounters/bulk",
            json={"rows": [{"encounter_date": "2020-01-15"}]},
        )
        assert resp.status_code == 400
        assert "encounter_type" in resp.json()["error"]

    def test_bulk_rejects_malformed_date(self, client):
        resp = client.post(
            "/istota/api/health/encounters/bulk",
            json={"rows": [self._row(encounter_date="yesterday")]},
        )
        assert resp.status_code == 400
        assert "ISO" in resp.json()["error"]

    def test_bulk_rejects_future_date(self, client):
        resp = client.post(
            "/istota/api/health/encounters/bulk",
            json={"rows": [self._row(encounter_date="2099-01-01")]},
        )
        assert resp.status_code == 400
        assert "future" in resp.json()["error"]

    def test_bulk_with_import_id_dedupes_replays(self, client):
        payload = {
            "rows": [self._row(), self._row(encounter_date="2021-02-20")],
            "import_id": "session-xyz",
        }
        first = client.post(
            "/istota/api/health/encounters/bulk", json=payload,
        )
        second = client.post(
            "/istota/api/health/encounters/bulk", json=payload,
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["ids"] == second.json()["ids"]
        listing = client.get(
            "/istota/api/health/encounters",
        ).json()["encounters"]
        assert len(listing) == 2

    def test_bulk_rejects_empty_import_id(self, client):
        resp = client.post(
            "/istota/api/health/encounters/bulk",
            json={"rows": [self._row()], "import_id": ""},
        )
        assert resp.status_code == 400

    def test_bulk_always_writes_dedup_key(self, client, ctx):
        client.post(
            "/istota/api/health/encounters/bulk",
            json={"rows": [self._row()]},
        )
        with health_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT dedup_key FROM encounters",
            ).fetchone()
        assert row is not None
        assert row["dedup_key"] is not None
        assert ":" in row["dedup_key"]
