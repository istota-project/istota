"""Tests for the medication API routes."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota.health import db as health_db
from istota.health import garmin_sync
from istota.health._migrate import ensure_initialised
from istota.health.models import HealthContext
from istota.health.routes import (
    get_user_context,
    require_auth,
    router,
)
from istota.health.workspace import synthesize_health_context

BASE = "/istota/api/health"


@pytest.fixture
def ctx(tmp_path: Path) -> HealthContext:
    c = synthesize_health_context("alice", tmp_path / "workspace")
    ensure_initialised(c)
    return c


def _app(ctx: HealthContext, *, user_tz: str | None = None) -> FastAPI:
    app = FastAPI()
    app.include_router(router, prefix=BASE)
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    app.dependency_overrides[get_user_context] = lambda: ctx
    if user_tz is not None:
        user = SimpleNamespace(timezone=user_tz)
        app.state.istota_config = SimpleNamespace(
            get_user=lambda uid: user if uid == "alice" else None,
        )
    return app


@pytest.fixture
def client(ctx: HealthContext) -> TestClient:
    return TestClient(_app(ctx))


def _create(client: TestClient, **body) -> int:
    body.setdefault("name", "Atorvastatin")
    resp = client.post(f"{BASE}/medications", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


def _get(client: TestClient, mid: int) -> dict:
    resp = client.get(f"{BASE}/medications/{mid}")
    assert resp.status_code == 200, resp.text
    return resp.json()["medication"]


def _encounter(ctx: HealthContext) -> int:
    with health_db.connect(ctx.db_path) as conn:
        eid = health_db.insert_encounter(
            conn, encounter_date="2026-01-15", encounter_type="office_visit",
        )
        conn.commit()
    return eid


class TestCreateAndRead:
    def test_create_then_get(self, client, ctx):
        eid = _encounter(ctx)
        mid = _create(
            client, name="  Lipitor ", generic_name="Atorvastatin", dose="20",
            dose_unit="mg", frequency="daily", route="oral", category="Rx",
            prescriber="Dr. Lee", start_date="2026-01-15", encounter_id=eid,
        )
        resp = client.get(f"{BASE}/medications/{mid}")
        assert resp.status_code == 200
        data = resp.json()
        med = data["medication"]
        assert med["name"] == "Lipitor"
        assert med["category"] == "prescription"
        assert med["start_date"] == "2026-01-15"
        assert med["active"] is True
        assert med["encounter_id"] == eid
        assert data["encounter"]["id"] == eid
        assert data["documents"] == []

    def test_category_defaults_to_prescription(self, client):
        mid = _create(client, name="Metformin")
        assert _get(client, mid)["category"] == "prescription"

    def test_source_is_forced_to_manual(self, client):
        mid = _create(client, name="Vitamin D3", category="supplement", source="agent")
        assert _get(client, mid)["source"] == "manual"

    def test_empty_strings_store_null(self, client):
        mid = _create(client, name="Ibuprofen", category="otc", dose="", end_date="")
        med = _get(client, mid)
        assert med["dose"] is None
        assert med["end_date"] is None

    def test_list_default_excludes_stopped(self, client):
        active = _create(client, name="Atorvastatin")
        stopped = _create(client, name="Lisinopril", active=False)
        ids = [m["id"] for m in client.get(f"{BASE}/medications").json()["medications"]]
        assert ids == [active]
        resp = client.get(f"{BASE}/medications", params={"status": "all"})
        ids = {m["id"] for m in resp.json()["medications"]}
        assert ids == {active, stopped}

    def test_list_filters_by_category_and_generic_name(self, client):
        lipitor = _create(client, name="Lipitor", generic_name="Atorvastatin")
        _create(client, name="Fish oil", category="supplement")
        resp = client.get(f"{BASE}/medications", params={"name": "atorvastatin"})
        assert [m["id"] for m in resp.json()["medications"]] == [lipitor]
        resp = client.get(f"{BASE}/medications", params={"category": "Rx"})
        assert [m["id"] for m in resp.json()["medications"]] == [lipitor]

    def test_list_rows_are_the_shared_serialiser(self, client):
        _create(client, name="Atorvastatin")
        row = client.get(f"{BASE}/medications").json()["medications"][0]
        assert list(row)[:3] == ["id", "name", "generic_name"]
        assert list(row)[-2:] == ["created_at", "document_count"]
        assert row["document_count"] == 0

    def test_get_missing_is_404(self, client):
        assert client.get(f"{BASE}/medications/999").status_code == 404


class TestValidation:
    @pytest.mark.parametrize("body, field", [
        ({"name": "X", "category": "vitamin"}, "category"),
        ({"name": "X", "start_date": "2026-02-31"}, "start_date"),
        ({"name": "X", "start_date": "2026-03-01", "end_date": "2026-02-01"}, "end_date"),
        ({"name": "X", "dose": 20}, "dose"),
        ({"name": "X", "active": "yes"}, "active"),
        ({"name": "X", "encounter_id": "abc"}, "encounter_id"),
    ])
    def test_create_400_names_the_field(self, client, body, field):
        resp = client.post(f"{BASE}/medications", json=body)
        assert resp.status_code == 400
        assert field in resp.json()["error"]

    @pytest.mark.parametrize("name", [None, "", "   "])
    def test_create_requires_name(self, client, name):
        body = {} if name is None else {"name": name}
        resp = client.post(f"{BASE}/medications", json=body)
        assert resp.status_code == 400
        assert "name" in resp.json()["error"]

    def test_create_with_unknown_encounter_is_400_and_writes_nothing(self, client):
        resp = client.post(
            f"{BASE}/medications", json={"name": "X", "encounter_id": 999},
        )
        assert resp.status_code == 400
        assert resp.json()["error"] == "encounter not found"
        resp = client.get(f"{BASE}/medications", params={"status": "all"})
        assert resp.json()["medications"] == []

    @pytest.mark.parametrize("params", [{"status": "paused"}, {"category": "vitamin"}])
    def test_list_400_on_unknown_status_or_category(self, client, params):
        resp = client.get(f"{BASE}/medications", params=params)
        assert resp.status_code == 400

    def test_list_limit_is_capped_at_500(self, client):
        assert client.get(f"{BASE}/medications", params={"limit": 501}).status_code == 422
        assert client.get(f"{BASE}/medications", params={"limit": 500}).status_code == 200


class TestUpdate:
    def test_absent_key_is_unchanged_and_null_clears(self, client):
        mid = _create(client, dose="20", dose_unit="mg", notes="with food")
        resp = client.put(
            f"{BASE}/medications/{mid}", json={"dose": "40", "notes": None},
        )
        assert resp.status_code == 200
        med = _get(client, mid)
        assert med["dose"] == "40"
        assert med["dose_unit"] == "mg"
        assert med["notes"] is None

    @pytest.mark.parametrize("field", ["name", "category"])
    def test_null_name_or_category_is_400(self, client, field):
        mid = _create(client, name="Atorvastatin")
        resp = client.put(f"{BASE}/medications/{mid}", json={field: None})
        assert resp.status_code == 400
        assert field in resp.json()["error"]
        med = _get(client, mid)
        assert med["name"] == "Atorvastatin"
        assert med["category"] == "prescription"

    def test_end_before_stored_start_is_400(self, client):
        mid = _create(client, start_date="2026-03-01")
        resp = client.put(f"{BASE}/medications/{mid}", json={"end_date": "2026-02-01"})
        assert resp.status_code == 400
        assert _get(client, mid)["end_date"] is None

    def test_bad_category_is_400(self, client):
        mid = _create(client)
        resp = client.put(f"{BASE}/medications/{mid}", json={"category": "vitamin"})
        assert resp.status_code == 400

    def test_unknown_encounter_is_400(self, client):
        mid = _create(client)
        resp = client.put(f"{BASE}/medications/{mid}", json={"encounter_id": 999})
        assert resp.status_code == 400
        assert _get(client, mid)["encounter_id"] is None

    def test_undo_a_stop(self, client):
        mid = _create(client, active=False, end_date="2026-04-02")
        resp = client.put(
            f"{BASE}/medications/{mid}", json={"active": True, "end_date": None},
        )
        assert resp.status_code == 200
        med = _get(client, mid)
        assert med["active"] is True
        assert med["end_date"] is None

    def test_missing_row_is_404(self, client):
        resp = client.put(f"{BASE}/medications/999", json={"dose": "1"})
        assert resp.status_code == 404


class _FixedDatetime(datetime):
    # 03:00 UTC on 1 March is still 28 February in Los Angeles.
    @classmethod
    def now(cls, tz=None):
        instant = datetime(2026, 3, 1, 3, 0, tzinfo=timezone.utc)
        return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)


class TestDiscontinue:
    def test_with_a_date(self, client):
        mid = _create(client, start_date="2026-01-15")
        resp = client.post(
            f"{BASE}/medications/{mid}/discontinue", json={"end_date": "2026-04-02"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok", "end_date": "2026-04-02"}
        med = _get(client, mid)
        assert med["active"] is False
        assert med["end_date"] == "2026-04-02"

    def test_without_a_body_uses_today_in_the_users_timezone(self, ctx, monkeypatch):
        monkeypatch.setattr(garmin_sync, "datetime", _FixedDatetime)
        client = TestClient(_app(ctx, user_tz="America/Los_Angeles"))
        mid = _create(client)
        resp = client.post(f"{BASE}/medications/{mid}/discontinue")
        assert resp.status_code == 200
        assert resp.json()["end_date"] == "2026-02-28"
        assert _get(client, mid)["end_date"] == "2026-02-28"

    def test_without_a_timezone_uses_utc(self, client, monkeypatch):
        monkeypatch.setattr(garmin_sync, "datetime", _FixedDatetime)
        mid = _create(client)
        resp = client.post(f"{BASE}/medications/{mid}/discontinue", json={})
        assert resp.json()["end_date"] == "2026-03-01"

    @pytest.mark.parametrize("end_date", ["2026-02-31", "2026-01-01"])
    def test_bad_date_or_end_before_start_is_400(self, client, end_date):
        mid = _create(client, start_date="2026-01-15")
        resp = client.post(
            f"{BASE}/medications/{mid}/discontinue", json={"end_date": end_date},
        )
        assert resp.status_code == 400
        assert _get(client, mid)["active"] is True

    def test_missing_row_is_404(self, client):
        resp = client.post(
            f"{BASE}/medications/999/discontinue", json={"end_date": "2026-04-02"},
        )
        assert resp.status_code == 404


class TestDelete:
    def test_delete_then_404(self, client):
        mid = _create(client)
        assert client.delete(f"{BASE}/medications/{mid}").status_code == 200
        assert client.get(f"{BASE}/medications/{mid}").status_code == 404
        assert client.delete(f"{BASE}/medications/{mid}").status_code == 404


class TestEncounterDetail:
    def test_encounter_carries_its_medications(self, client, ctx):
        eid = _encounter(ctx)
        mid = _create(client, encounter_id=eid)
        _create(client, name="Unrelated")
        resp = client.get(f"{BASE}/encounters/{eid}")
        assert resp.status_code == 200
        meds = resp.json()["medications"]
        assert [m["id"] for m in meds] == [mid]


class TestLifecycle:
    def test_create_discontinue_then_list_stopped(self, client):
        kept = _create(client, name="Atorvastatin", dose="40", start_date="2026-04-02")
        old = _create(client, name="Atorvastatin", dose="20", start_date="2026-01-15")
        resp = client.post(
            f"{BASE}/medications/{old}/discontinue", json={"end_date": "2026-04-02"},
        )
        assert resp.status_code == 200
        stopped = client.get(f"{BASE}/medications", params={"status": "stopped"})
        assert [m["id"] for m in stopped.json()["medications"]] == [old]
        active = client.get(f"{BASE}/medications")
        assert [m["id"] for m in active.json()["medications"]] == [kept]
