"""Tests for the health FastAPI router."""

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


class TestStatsRoutes:
    def test_create_and_list(self, client):
        resp = client.post("/istota/api/health/stats", json={
            "metric": "weight", "value": 82.5, "unit": "kg",
        })
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "ok"
        listing = client.get("/istota/api/health/stats").json()
        assert len(listing["stats"]) == 1
        assert listing["stats"][0]["metric"] == "weight"

    def test_metric_validation(self, client):
        resp = client.post("/istota/api/health/stats", json={
            "metric": "Weight Spaces", "value": 82, "unit": "kg",
        })
        assert resp.status_code == 400

    def test_value_validation(self, client):
        resp = client.post("/istota/api/health/stats", json={
            "metric": "weight", "value": "heavy", "unit": "kg",
        })
        assert resp.status_code == 400

    def test_latest(self, client):
        client.post("/istota/api/health/stats", json={
            "metric": "weight", "value": 83.0, "unit": "kg",
            "measured_at": "2026-05-01T10:00:00+00:00",
        })
        client.post("/istota/api/health/stats", json={
            "metric": "weight", "value": 82.0, "unit": "kg",
            "measured_at": "2026-05-08T10:00:00+00:00",
        })
        client.post("/istota/api/health/stats", json={
            "metric": "resting_hr", "value": 60, "unit": "bpm",
        })
        resp = client.get("/istota/api/health/stats/latest").json()
        assert set(resp["stats"].keys()) == {"weight", "resting_hr"}
        assert resp["stats"]["weight"]["value"] == 82.0

    def test_series(self, client):
        client.post("/istota/api/health/stats", json={
            "metric": "weight", "value": 83.0, "unit": "kg",
            "measured_at": "2026-05-01T10:00:00+00:00",
        })
        client.post("/istota/api/health/stats", json={
            "metric": "weight", "value": 82.0, "unit": "kg",
            "measured_at": "2026-05-08T10:00:00+00:00",
        })
        resp = client.get(
            "/istota/api/health/stats/series", params={"metric": "weight"},
        ).json()
        assert [p["value"] for p in resp["points"]] == [83.0, 82.0]

    def test_delete(self, client):
        sid = client.post("/istota/api/health/stats", json={
            "metric": "weight", "value": 82, "unit": "kg",
        }).json()["id"]
        resp = client.delete(f"/istota/api/health/stats/{sid}")
        assert resp.status_code == 200
        assert client.get("/istota/api/health/stats").json()["stats"] == []


class TestPanelsRoutes:
    def test_create_panel_and_replace_biomarkers(self, client):
        resp = client.post("/istota/api/health/panels", json={
            "drawn_at": "2026-05-08", "lab_name": "Quest", "panel_type": "CBC",
        })
        assert resp.status_code == 200
        pid = resp.json()["id"]

        resp = client.post(
            f"/istota/api/health/panels/{pid}/biomarkers",
            json={
                "biomarkers": [
                    {"name": "Hemoglobin", "value": 15.0, "unit": "g/dL"},
                    {"name": "Hgb", "value": 15.0, "unit": "g/dL"},
                ],
                "confirm": True,
            },
        )
        assert resp.status_code == 200, resp.text

        detail = client.get(f"/istota/api/health/panels/{pid}").json()
        assert detail["panel"]["draft"] is False
        names = [b["name"] for b in detail["biomarkers"]]
        # Alias normalisation: "Hgb" → "Hemoglobin"
        assert names.count("Hemoglobin") == 2

    def test_panel_collision(self, client):
        client.post("/istota/api/health/panels", json={
            "drawn_at": "2026-05-08", "lab_name": "Quest",
        })
        resp = client.post("/istota/api/health/panels", json={
            "drawn_at": "2026-05-08", "lab_name": "Quest",
        })
        assert resp.status_code == 200
        body = resp.json()
        assert "collision" in body

    def test_flag_from_canonical_range(self, client, ctx):
        # Set sex so sex-specific Hgb range applies.
        client.put("/istota/api/health/settings", json={"sex": "F"})
        pid = client.post("/istota/api/health/panels", json={
            "drawn_at": "2026-05-08",
        }).json()["id"]
        # Hgb 11 g/dL — below the female canonical low (12.0).
        client.post(
            f"/istota/api/health/panels/{pid}/biomarkers",
            json={
                "biomarkers": [
                    {"name": "Hemoglobin", "value": 11.0, "unit": "g/dL"},
                ],
            },
        )
        bs = client.get(f"/istota/api/health/panels/{pid}").json()["biomarkers"]
        assert bs[0]["flag"] == "L"

    def test_bp_fanout_to_stats(self, client):
        pid = client.post("/istota/api/health/panels", json={
            "drawn_at": "2026-05-08",
        }).json()["id"]
        client.post(
            f"/istota/api/health/panels/{pid}/biomarkers",
            json={
                "biomarkers": [
                    {"name": "BP_Systolic", "value": 130, "unit": "mmHg"},
                    {"name": "BP_Diastolic", "value": 82, "unit": "mmHg"},
                ],
            },
        )
        stats = client.get("/istota/api/health/stats").json()["stats"]
        metrics = {s["metric"] for s in stats}
        assert {"blood_pressure_systolic", "blood_pressure_diastolic"} <= metrics
        for s in stats:
            assert s["source"] == "lab_panel"

    def test_delete_panel_removes_derived_stats(self, client):
        pid = client.post("/istota/api/health/panels", json={
            "drawn_at": "2026-05-08",
        }).json()["id"]
        client.post(
            f"/istota/api/health/panels/{pid}/biomarkers",
            json={"biomarkers": [
                {"name": "BP_Systolic", "value": 120, "unit": "mmHg"},
            ]},
        )
        client.delete(f"/istota/api/health/panels/{pid}")
        assert client.get("/istota/api/health/stats").json()["stats"] == []


class TestTrend:
    def test_excludes_drafts(self, client):
        p1 = client.post("/istota/api/health/panels", json={
            "drawn_at": "2026-01-01",
        }).json()["id"]
        # First panel — confirm so trend includes it.
        client.post(
            f"/istota/api/health/panels/{p1}/biomarkers",
            json={"biomarkers": [
                {"name": "LDL", "value": 110, "unit": "mg/dL"},
            ], "confirm": True},
        )
        p2 = client.post("/istota/api/health/panels", json={
            "drawn_at": "2026-05-01",
        }).json()["id"]
        # Second panel — mark as draft so it stays out of trend.
        client.put(f"/istota/api/health/panels/{p2}", json={"draft": True})
        client.post(
            f"/istota/api/health/panels/{p2}/biomarkers",
            json={"biomarkers": [
                {"name": "LDL", "value": 80, "unit": "mg/dL"},
            ]},
        )
        trend = client.get(
            "/istota/api/health/biomarkers/trend", params={"name": "LDL"},
        ).json()
        assert len(trend["points"]) == 1
        assert trend["points"][0]["value"] == 110

    def test_unit_mismatch_flag(self, client):
        p1 = client.post("/istota/api/health/panels", json={
            "drawn_at": "2026-01-01",
        }).json()["id"]
        client.post(
            f"/istota/api/health/panels/{p1}/biomarkers",
            json={"biomarkers": [
                {"name": "Glucose", "value": 95, "unit": "mg/dL"},
            ], "confirm": True},
        )
        p2 = client.post("/istota/api/health/panels", json={
            "drawn_at": "2026-05-01",
        }).json()["id"]
        client.post(
            f"/istota/api/health/panels/{p2}/biomarkers",
            json={"biomarkers": [
                {"name": "Glucose", "value": 5.2, "unit": "mmol/L"},
            ], "confirm": True},
        )
        trend = client.get(
            "/istota/api/health/biomarkers/trend", params={"name": "Glucose"},
        ).json()
        assert trend["unit_mismatch"] is True


class TestSettings:
    def test_get_defaults(self, client):
        resp = client.get("/istota/api/health/settings").json()
        assert resp["settings"]["display_units"] == {
            "weight": "kg", "height": "cm", "temp": "C",
        }

    def test_partial_update(self, client):
        client.put("/istota/api/health/settings", json={
            "height_cm": 178, "sex": "M",
        })
        s = client.get("/istota/api/health/settings").json()["settings"]
        assert s["height_cm"] == 178
        assert s["sex"] == "M"
        assert s["dob"] is None

    def test_sex_rejection(self, client):
        resp = client.put("/istota/api/health/settings", json={"sex": "X"})
        assert resp.status_code == 400

    def test_display_units_roundtrip(self, client):
        client.put("/istota/api/health/settings", json={
            "display_units": {"weight": "lb", "height": "ft_in", "temp": "F"},
        })
        s = client.get("/istota/api/health/settings").json()["settings"]
        assert s["display_units"]["weight"] == "lb"


class TestDashboard:
    def test_dashboard_includes_bmi_and_alerts(self, client):
        client.put("/istota/api/health/settings", json={"height_cm": 178})
        client.post("/istota/api/health/stats", json={
            "metric": "weight", "value": 82.0, "unit": "kg",
        })
        pid = client.post("/istota/api/health/panels", json={
            "drawn_at": "2026-05-08",
        }).json()["id"]
        client.post(
            f"/istota/api/health/panels/{pid}/biomarkers",
            json={"biomarkers": [
                # 15 WBC is above canonical high (11) — should flag H.
                {"name": "WBC", "value": 15.0, "unit": "10^3/uL"},
            ], "confirm": True},
        )
        dash = client.get("/istota/api/health/dashboard").json()
        assert dash["bmi"] is not None
        assert dash["bmi"] > 0
        assert len(dash["alerts"]) == 1
        assert dash["alerts"][0]["flag"] == "H"


class TestBiomarkerRefs:
    def test_lists_seeded_refs(self, client):
        refs = client.get("/istota/api/health/biomarkers/refs").json()["refs"]
        assert any(r["name"] == "Hemoglobin" for r in refs)


class TestPanelUpload:
    def test_creates_draft_with_source_file(self, client, ctx):
        # Upload a tiny text blob — the route doesn't care about file
        # content here, only that the panel + file get persisted.
        resp = client.post(
            "/istota/api/health/panels/upload",
            files={"file": ("report.pdf", b"%PDF-1.4 fake pdf", "application/pdf")},
            data={"drawn_at": "2026-05-08", "lab_name": "Quest"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok"
        assert body["draft"] is True
        pid = body["id"]

        detail = client.get(f"/istota/api/health/panels/{pid}").json()
        assert detail["panel"]["draft"] is True
        assert detail["source"]["available"] is True
        assert detail["source"]["mime"] == "application/pdf"

        # The source file is reachable via the auth-gated route.
        src = client.get(f"/istota/api/health/panels/{pid}/source")
        assert src.status_code == 200
        assert src.content.startswith(b"%PDF")

    def test_empty_upload_rejected(self, client):
        resp = client.post(
            "/istota/api/health/panels/upload",
            files={"file": ("empty.pdf", b"", "application/pdf")},
            data={"drawn_at": "2026-05-08"},
        )
        assert resp.status_code == 400


class TestCsvImportExport:
    SAMPLE = (
        ",,MORPHOLOGY,,LIPID PANEL,\n"
        "Date,Lab,Hgb (g/dL),WBC (th/mm3),LDL-C (mg/dL),HDL (mg/dL)\n"
        ",,12.7-16.7,4.8-10.5,20-100,40-60\n"
        "2024-07-27,Kaiser,14.5,6.4,148,51\n"
        "2025-11-28,Kaiser,14.6,6.1,148,55\n"
    )

    def test_import_then_list(self, client):
        resp = client.post(
            "/istota/api/health/csv/import",
            files={"file": ("bloodwork.csv", self.SAMPLE.encode(), "text/csv")},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["panels_created"] == 2
        assert body["panels_skipped_identical"] == 0
        assert body["panels_needs_review"] == 0
        assert body["biomarkers_created"] == 8

        panels = client.get("/istota/api/health/panels").json()["panels"]
        assert [p["drawn_at"] for p in panels] == ["2025-11-28", "2024-07-27"]
        assert all(p["draft"] is False for p in panels)

    def test_reimport_silently_skips_identical(self, client):
        for _ in range(2):
            resp = client.post(
                "/istota/api/health/csv/import",
                files={"file": ("b.csv", self.SAMPLE.encode(), "text/csv")},
            )
            assert resp.status_code == 200
        last = resp.json()
        # Second pass matches on content hash for every row → silent noop.
        assert last["panels_skipped_identical"] == 2
        assert last["panels_created"] == 0
        assert last["panels_needs_review"] == 0

    def test_export_then_reimport(self, client):
        client.post(
            "/istota/api/health/csv/import",
            files={"file": ("b.csv", self.SAMPLE.encode(), "text/csv")},
        )
        resp = client.get("/istota/api/health/csv/export")
        assert resp.status_code == 200
        text = resp.text
        # CSV has 3 header rows + 2 data rows.
        assert len(text.strip().splitlines()) == 5
        # Round-trip: re-importing the export hits content-hash dedup.
        again = client.post(
            "/istota/api/health/csv/import",
            files={"file": ("re.csv", text.encode(), "text/csv")},
        )
        assert again.json()["panels_skipped_identical"] == 2


class TestBloodworkMatrix:
    def _seed(self, client, drawn_at: str, lab: str, items: list[tuple[str, float, str]]):
        pid = client.post("/istota/api/health/panels", json={
            "drawn_at": drawn_at, "lab_name": lab,
        }).json()["id"]
        client.post(
            f"/istota/api/health/panels/{pid}/biomarkers",
            json={
                "biomarkers": [
                    {"name": n, "value": v, "unit": u}
                    for (n, v, u) in items
                ],
                "confirm": True,
            },
        )
        return pid

    def test_empty(self, client):
        resp = client.get("/istota/api/health/bloodwork/matrix").json()
        assert resp["panels"] == []
        assert resp["categories"] == []
        assert resp["values"] == {}

    def test_matrix_shape(self, client):
        p1 = self._seed(client, "2026-01-15", "Quest", [
            ("Hemoglobin", 14.8, "g/dL"),
            ("WBC", 7.0, "10^3/uL"),
            ("LDL", 95, "mg/dL"),
        ])
        p2 = self._seed(client, "2026-05-08", "Kaiser", [
            ("Hemoglobin", 15.0, "g/dL"),
            ("LDL", 112, "mg/dL"),
        ])
        m = client.get("/istota/api/health/bloodwork/matrix").json()
        # Panels sorted ascending.
        assert [p["id"] for p in m["panels"]] == [p1, p2]
        # CBC category contains Hemoglobin and WBC; Lipid contains LDL.
        cats = {c["name"]: [mk["name"] for mk in c["markers"]] for c in m["categories"]}
        assert "Hemoglobin" in cats["CBC"]
        assert "WBC" in cats["CBC"]
        assert "LDL" in cats["Lipid"]
        # Markers carry the canonical reference range.
        cbc = next(c for c in m["categories"] if c["name"] == "CBC")
        hgb = next(m for m in cbc["markers"] if m["name"] == "Hemoglobin")
        assert hgb["unit"] == "g/dL"
        assert hgb["ref_range_low"] is not None
        # Values are keyed by panel id then marker name.
        assert m["values"][str(p2)]["LDL"]["value"] == 112

    def test_drafts_excluded(self, client):
        p1 = self._seed(client, "2026-01-15", "Quest", [
            ("Hemoglobin", 14.8, "g/dL"),
        ])
        # Second panel — left as draft via PUT.
        p2 = client.post("/istota/api/health/panels", json={
            "drawn_at": "2026-05-08", "lab_name": "Kaiser",
        }).json()["id"]
        client.put(f"/istota/api/health/panels/{p2}", json={"draft": True})
        client.post(
            f"/istota/api/health/panels/{p2}/biomarkers",
            json={"biomarkers": [
                {"name": "Hemoglobin", "value": 15.0, "unit": "g/dL"},
            ]},
        )
        m = client.get("/istota/api/health/bloodwork/matrix").json()
        assert [p["id"] for p in m["panels"]] == [p1]
        assert str(p2) not in m["values"]

    def test_unknown_marker_lands_in_other(self, client):
        self._seed(client, "2026-05-08", "Custom Lab", [
            ("MyUnusualMarker", 42, "x/y"),
        ])
        m = client.get("/istota/api/health/bloodwork/matrix").json()
        other = next((c for c in m["categories"] if c["name"] == "Other"), None)
        assert other is not None
        assert other["markers"][0]["name"] == "MyUnusualMarker"


class TestEncountersRoutes:
    def test_create_list_and_get(self, client):
        resp = client.post(
            "/istota/api/health/encounters",
            json={
                "encounter_date": "2026-05-13",
                "encounter_type": "procedure",
                "provider": "Dr. Smith",
                "facility": "Kaiser",
                "specialty": "gastroenterology",
                "reason": "Screening colonoscopy",
                "notes": "Grade I-II hemorrhoids",
            },
        )
        assert resp.status_code == 200, resp.text
        eid = resp.json()["id"]

        listing = client.get("/istota/api/health/encounters").json()
        assert len(listing["encounters"]) == 1
        assert listing["encounters"][0]["provider"] == "Dr. Smith"

        detail = client.get(f"/istota/api/health/encounters/{eid}").json()
        assert detail["encounter"]["specialty"] == "gastroenterology"
        assert detail["diagnoses"] == []
        assert detail["panels"] == []

    def test_filter_by_type(self, client):
        client.post(
            "/istota/api/health/encounters",
            json={"encounter_date": "2026-01-01", "encounter_type": "visit"},
        )
        client.post(
            "/istota/api/health/encounters",
            json={"encounter_date": "2026-05-13", "encounter_type": "procedure"},
        )
        listing = client.get(
            "/istota/api/health/encounters", params={"type": "procedure"},
        ).json()
        assert [e["encounter_type"] for e in listing["encounters"]] == ["procedure"]

    def test_missing_fields(self, client):
        resp = client.post(
            "/istota/api/health/encounters",
            json={"encounter_type": "visit"},
        )
        assert resp.status_code == 400

    def test_update(self, client):
        eid = client.post(
            "/istota/api/health/encounters",
            json={"encounter_date": "2026-05-13", "encounter_type": "visit"},
        ).json()["id"]
        resp = client.put(
            f"/istota/api/health/encounters/{eid}",
            json={"notes": "Follow-up in 3 years"},
        )
        assert resp.status_code == 200
        detail = client.get(f"/istota/api/health/encounters/{eid}").json()
        assert detail["encounter"]["notes"] == "Follow-up in 3 years"

    def test_delete(self, client):
        eid = client.post(
            "/istota/api/health/encounters",
            json={"encounter_date": "2026-05-13", "encounter_type": "visit"},
        ).json()["id"]
        resp = client.delete(f"/istota/api/health/encounters/{eid}")
        assert resp.status_code == 200
        miss = client.get(f"/istota/api/health/encounters/{eid}")
        assert miss.status_code == 404

    def test_get_includes_linked_panels_and_diagnoses(self, client):
        eid = client.post(
            "/istota/api/health/encounters",
            json={"encounter_date": "2026-05-13", "encounter_type": "procedure"},
        ).json()["id"]
        pid = client.post(
            "/istota/api/health/panels",
            json={
                "drawn_at": "2026-05-13", "lab_name": "Quest",
                "encounter_id": eid,
            },
        ).json()["id"]
        did = client.post(
            "/istota/api/health/diagnoses",
            json={"name": "Hemorrhoids", "encounter_id": eid},
        ).json()["id"]
        detail = client.get(f"/istota/api/health/encounters/{eid}").json()
        assert [d["id"] for d in detail["diagnoses"]] == [did]
        assert [p["id"] for p in detail["panels"]] == [pid]


class TestDiagnosesRoutes:
    def test_create_and_filter(self, client):
        client.post(
            "/istota/api/health/diagnoses",
            json={"name": "Internal hemorrhoids", "date_diagnosed": "2026-05-13"},
        )
        client.post(
            "/istota/api/health/diagnoses",
            json={"name": "Hypertension", "status": "chronic"},
        )
        client.post(
            "/istota/api/health/diagnoses",
            json={"name": "Strep", "status": "resolved",
                  "date_resolved": "2024-12-15"},
        )
        actives = client.get(
            "/istota/api/health/diagnoses", params={"status": "active"},
        ).json()
        assert [d["name"] for d in actives["diagnoses"]] == ["Internal hemorrhoids"]
        all_d = client.get(
            "/istota/api/health/diagnoses", params={"status": "all"},
        ).json()
        # default ordering: active → chronic → resolved
        names = [d["name"] for d in all_d["diagnoses"]]
        assert names == ["Internal hemorrhoids", "Hypertension", "Strep"]

    def test_unknown_status_rejected(self, client):
        resp = client.post(
            "/istota/api/health/diagnoses",
            json={"name": "X", "status": "bogus"},
        )
        assert resp.status_code == 400

    def test_resolve(self, client):
        did = client.post(
            "/istota/api/health/diagnoses",
            json={"name": "Hemorrhoids", "date_diagnosed": "2026-05-13"},
        ).json()["id"]
        resp = client.put(
            f"/istota/api/health/diagnoses/{did}",
            json={"status": "resolved", "date_resolved": "2026-06-15"},
        )
        assert resp.status_code == 200
        detail = client.get(f"/istota/api/health/diagnoses/{did}").json()
        assert detail["diagnosis"]["status"] == "resolved"
        assert detail["diagnosis"]["date_resolved"] == "2026-06-15"

    def test_linked_encounter(self, client):
        eid = client.post(
            "/istota/api/health/encounters",
            json={"encounter_date": "2026-05-13", "encounter_type": "procedure"},
        ).json()["id"]
        did = client.post(
            "/istota/api/health/diagnoses",
            json={"name": "Hemorrhoids", "encounter_id": eid},
        ).json()["id"]
        detail = client.get(f"/istota/api/health/diagnoses/{did}").json()
        assert detail["encounter"]["id"] == eid

    def test_create_rejects_missing_encounter(self, client):
        resp = client.post(
            "/istota/api/health/diagnoses",
            json={"name": "Hemorrhoids", "encounter_id": 9999},
        )
        assert resp.status_code == 400

    def test_delete(self, client):
        did = client.post(
            "/istota/api/health/diagnoses",
            json={"name": "Hemorrhoids"},
        ).json()["id"]
        resp = client.delete(f"/istota/api/health/diagnoses/{did}")
        assert resp.status_code == 200


class TestHistorySummary:
    def test_summary(self, client):
        eid = client.post(
            "/istota/api/health/encounters",
            json={"encounter_date": "2026-05-13", "encounter_type": "procedure"},
        ).json()["id"]
        client.post(
            "/istota/api/health/encounters",
            json={"encounter_date": "2026-05-01", "encounter_type": "visit"},
        )
        client.post(
            "/istota/api/health/diagnoses",
            json={"name": "Hypertension", "status": "chronic"},
        )
        client.post(
            "/istota/api/health/diagnoses",
            json={"name": "Hemorrhoids", "encounter_id": eid},
        )
        client.post(
            "/istota/api/health/diagnoses",
            json={"name": "Old thing", "status": "resolved"},
        )
        summary = client.get("/istota/api/health/history/summary").json()
        assert [d["name"] for d in summary["active_diagnoses"]] == ["Hemorrhoids"]
        assert [d["name"] for d in summary["chronic_diagnoses"]] == ["Hypertension"]
        assert len(summary["recent_encounters"]) == 2
        assert [e["encounter_type"] for e in summary["recent_procedures"]] == [
            "procedure",
        ]


class TestPanelEncounterLink:
    def test_create_panel_with_encounter(self, client):
        eid = client.post(
            "/istota/api/health/encounters",
            json={"encounter_date": "2026-05-13", "encounter_type": "visit"},
        ).json()["id"]
        pid = client.post(
            "/istota/api/health/panels",
            json={"drawn_at": "2026-05-13", "lab_name": "Quest",
                  "encounter_id": eid},
        ).json()["id"]
        panel = client.get(f"/istota/api/health/panels/{pid}").json()
        assert panel["panel"]["encounter_id"] == eid

    def test_create_panel_rejects_missing_encounter(self, client):
        resp = client.post(
            "/istota/api/health/panels",
            json={"drawn_at": "2026-05-13", "encounter_id": 9999},
        )
        assert resp.status_code == 400

    def test_put_panel_clears_encounter(self, client):
        eid = client.post(
            "/istota/api/health/encounters",
            json={"encounter_date": "2026-05-13", "encounter_type": "visit"},
        ).json()["id"]
        pid = client.post(
            "/istota/api/health/panels",
            json={"drawn_at": "2026-05-13", "encounter_id": eid},
        ).json()["id"]
        resp = client.put(
            f"/istota/api/health/panels/{pid}", json={"encounter_id": None},
        )
        assert resp.status_code == 200
        panel = client.get(f"/istota/api/health/panels/{pid}").json()
        assert panel["panel"]["encounter_id"] is None


class TestDashboardHistory:
    def test_dashboard_includes_history(self, client):
        client.post(
            "/istota/api/health/diagnoses",
            json={"name": "Hypertension", "status": "chronic"},
        )
        client.post(
            "/istota/api/health/diagnoses",
            json={"name": "Active thing", "status": "active"},
        )
        client.post(
            "/istota/api/health/diagnoses",
            json={"name": "Old", "status": "resolved"},
        )
        client.post(
            "/istota/api/health/encounters",
            json={"encounter_date": "2026-05-13", "encounter_type": "visit",
                  "provider": "Dr. Smith"},
        )
        d = client.get("/istota/api/health/dashboard").json()
        assert d["active_diagnoses_count"] == 2  # active + chronic, not resolved
        assert d["recent_encounters"][0]["provider"] == "Dr. Smith"
