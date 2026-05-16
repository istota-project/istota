"""Tests for the immunizations data layer + coverage rules."""

from datetime import date

import pytest

from istota.health import db as health_db
from istota.health._migrate import (
    _IMM_SEED_HASH_KEY,
    ensure_initialised,
    seed_immunization_refs,
)
from istota.health.immunizations import (
    STATUS_DUE_SOON,
    STATUS_EXPIRED,
    STATUS_NEVER_RECORDED,
    STATUS_OVERDUE,
    STATUS_RISK_BASED,
    STATUS_SERIES_INCOMPLETE,
    STATUS_UP_TO_DATE,
    compute_coverage,
)
from istota.health.models import Immunization, ImmunizationRef
from istota.health.workspace import synthesize_health_context


def _ctx(tmp_path):
    return synthesize_health_context("alice", tmp_path / "workspace")


def _ref(
    name,
    schedule,
    *,
    interval_days=None,
    primary_series_doses=1,
    aliases=None,
    category="routine",
    display_name=None,
):
    return ImmunizationRef(
        name=name,
        display_name=display_name or name,
        category=category,
        schedule=schedule,
        interval_days=interval_days,
        primary_series_doses=primary_series_doses,
        aliases=aliases or [],
        description=None,
        typical_age_range=None,
    )


def _row(name, date_given, _id=1):
    return Immunization(
        id=_id, name=name, product_name=None, date_given=date_given,
        manufacturer=None, dose_label=None, lot_number=None, route=None,
        site=None, administered_by=None, facility=None, encounter_id=None,
        cvx_code=None, notes=None, source="manual", created_at="",
    )


class TestSchema:
    def test_creates_immunization_tables(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        health_db.init_db(ctx.db_path)
        with health_db.connect(ctx.db_path) as conn:
            tables = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
        assert {"immunizations", "immunization_refs"} <= tables


class TestImmunizationCrud:
    def test_insert_and_get(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            iid = health_db.insert_immunization(
                conn,
                name="Influenza",
                date_given="2025-11-28",
                product_name="Fluzone trivalent",
                manufacturer="Sanofi",
                route="IM",
                site="left deltoid",
                facility="CVS Pharmacy",
                notes="Annual 2025-26",
            )
            conn.commit()
            row = health_db.get_immunization(conn, iid)
        assert row is not None
        assert row.name == "Influenza"
        assert row.product_name == "Fluzone trivalent"
        assert row.manufacturer == "Sanofi"
        assert row.source == "manual"

    def test_list_filters(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            a = health_db.insert_immunization(
                conn, name="Influenza", date_given="2023-10-23",
            )
            b = health_db.insert_immunization(
                conn, name="Influenza", date_given="2025-11-28",
            )
            health_db.insert_immunization(
                conn, name="Tdap", date_given="2016-12-01",
            )
            conn.commit()
            flu = health_db.list_immunizations(conn, name="Influenza")
            recent = health_db.list_immunizations(conn, since="2020-01-01")
        # Most recent first.
        assert [r.id for r in flu] == [b, a]
        assert all(r.name == "Influenza" for r in flu)
        # The 2016 Tdap is excluded by since=.
        assert all(r.date_given >= "2020-01-01" for r in recent)

    def test_update_and_delete(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            iid = health_db.insert_immunization(
                conn, name="Tdap", date_given="2016-12-01",
            )
            n = health_db.update_immunization(
                conn, iid, lot_number="ABC123", notes="boost",
            )
            conn.commit()
        assert n == 1
        with health_db.connect(ctx.db_path) as conn:
            row = health_db.get_immunization(conn, iid)
        assert row.lot_number == "ABC123"
        assert row.notes == "boost"

        with health_db.connect(ctx.db_path) as conn:
            d = health_db.delete_immunization(conn, iid)
            conn.commit()
            row = health_db.get_immunization(conn, iid)
        assert d == 1
        assert row is None

    def test_update_rejects_none_required(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            iid = health_db.insert_immunization(
                conn, name="Influenza", date_given="2025-11-28",
            )
            n = health_db.update_immunization(
                conn, iid, name=None, date_given=None,
            )
            conn.commit()
        assert n == 0
        with health_db.connect(ctx.db_path) as conn:
            row = health_db.get_immunization(conn, iid)
        assert row.name == "Influenza"
        assert row.date_given == "2025-11-28"

    def test_dedup_key_idempotent(self, tmp_path):
        # Same dedup_key on second insert → returns the first row id.
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            a = health_db.insert_immunization(
                conn, name="Influenza", date_given="2025-11-28",
                dedup_key="task-1:row-0",
            )
            b = health_db.insert_immunization(
                conn, name="Influenza", date_given="2025-11-28",
                dedup_key="task-1:row-0",
            )
            conn.commit()
            rows = health_db.list_immunizations(conn)
        assert a == b
        assert len(rows) == 1

    def test_encounter_set_null_on_delete(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2025-11-28", encounter_type="visit",
            )
            iid = health_db.insert_immunization(
                conn, name="Influenza", date_given="2025-11-28",
                encounter_id=eid,
            )
            conn.commit()
            health_db.delete_encounter(conn, eid)
            conn.commit()
            row = health_db.get_immunization(conn, iid)
        assert row.encounter_id is None

    def test_immunizations_for_encounter(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2025-11-28", encounter_type="visit",
            )
            a = health_db.insert_immunization(
                conn, name="Influenza", date_given="2025-11-28",
                encounter_id=eid,
            )
            b = health_db.insert_immunization(
                conn, name="COVID-19", date_given="2025-11-28",
                encounter_id=eid,
            )
            health_db.insert_immunization(
                conn, name="Tdap", date_given="2016-12-01",
            )
            conn.commit()
            rows = health_db.immunizations_for_encounter(conn, eid)
        assert sorted(r.id for r in rows) == sorted([a, b])


class TestImmunizationRefs:
    def test_upsert_and_list(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            health_db.upsert_immunization_ref(conn, {
                "name": "Custom",
                "display_name": "Custom",
                "category": "routine",
                "schedule": "annual",
                "interval_days": 365,
                "primary_series_doses": 1,
                "aliases": ["custom-alias"],
                "description": "x",
            })
            conn.commit()
            ref = health_db.get_immunization_ref(conn, "Custom")
        assert ref is not None
        assert ref.aliases == ["custom-alias"]
        assert ref.schedule == "annual"

    def test_alias_lookup(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            # The bundled refs should already be seeded.
            by_alias = health_db.find_immunization_ref_by_alias(
                conn, "flu",
            )
            by_brand = health_db.find_immunization_ref_by_alias(
                conn, "Fluzone Quadrivalent",
            )
            by_canonical = health_db.find_immunization_ref_by_alias(
                conn, "Influenza",
            )
            missing = health_db.find_immunization_ref_by_alias(
                conn, "not-a-real-vaccine",
            )
        assert by_alias is not None and by_alias.name == "Influenza"
        assert by_brand is not None and by_brand.name == "Influenza"
        assert by_canonical is not None and by_canonical.name == "Influenza"
        assert missing is None


class TestSeedImmunizationRefs:
    def test_seeds_on_first_call(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        health_db.init_db(ctx.db_path)
        # Bundled refs not seeded yet.
        n = seed_immunization_refs(ctx)
        assert n is not None and n > 0
        with health_db.connect(ctx.db_path) as conn:
            refs = health_db.list_immunization_refs(conn)
        names = {r.name for r in refs}
        assert "Influenza" in names
        assert "Tdap" in names
        assert "Shingles" in names

    def test_idempotent_when_hash_matches(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        health_db.init_db(ctx.db_path)
        first = seed_immunization_refs(ctx)
        second = seed_immunization_refs(ctx)
        assert first is not None and first > 0
        assert second is None

    def test_reseeds_when_hash_changes(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        health_db.init_db(ctx.db_path)
        seed_immunization_refs(ctx)
        # Mutate the recorded hash so the next call re-runs.
        with health_db.connect(ctx.db_path) as conn:
            conn.execute(
                "UPDATE schema_meta SET value = ? WHERE key = ?",
                ("oldhash", _IMM_SEED_HASH_KEY),
            )
            conn.commit()
        n = seed_immunization_refs(ctx)
        assert n is not None and n > 0


class TestCoverageAnnual:
    def test_overdue_when_past_due(self):
        ref = _ref("Influenza", "annual", interval_days=365)
        rows = [_row("Influenza", "2023-10-23")]
        today = date(2025, 11, 1)
        result = compute_coverage([ref], rows, today=today)[0]
        assert result.status == STATUS_OVERDUE
        assert result.is_overdue is True
        assert result.days_until_due is not None and result.days_until_due < 0
        assert result.last_given == "2023-10-23"
        assert result.dose_count == 1

    def test_due_soon_within_30_days(self):
        ref = _ref("Influenza", "annual", interval_days=365)
        rows = [_row("Influenza", "2024-11-01")]
        # next_due = 2025-11-01; today is 2025-10-20 → 12 days out.
        today = date(2025, 10, 20)
        result = compute_coverage([ref], rows, today=today)[0]
        assert result.status == STATUS_DUE_SOON
        assert result.is_overdue is False
        assert result.days_until_due == 12

    def test_up_to_date_well_within_window(self):
        ref = _ref("Influenza", "annual", interval_days=365)
        rows = [_row("Influenza", "2025-09-01")]
        today = date(2025, 10, 20)
        result = compute_coverage([ref], rows, today=today)[0]
        assert result.status == STATUS_UP_TO_DATE
        assert result.is_overdue is False

    def test_never_recorded_when_no_dose(self):
        ref = _ref("Influenza", "annual", interval_days=365)
        result = compute_coverage([ref], [], today=date(2025, 10, 20))[0]
        assert result.status == STATUS_NEVER_RECORDED
        assert result.dose_count == 0
        assert result.last_given is None

    def test_uses_most_recent_dose(self):
        # Two doses; coverage should anchor on the latest.
        ref = _ref("Influenza", "annual", interval_days=365)
        rows = [
            _row("Influenza", "2022-11-01", _id=1),
            _row("Influenza", "2025-09-01", _id=2),
        ]
        result = compute_coverage([ref], rows, today=date(2025, 10, 20))[0]
        assert result.last_given == "2025-09-01"
        assert result.dose_count == 2
        assert result.status == STATUS_UP_TO_DATE


class TestCoverageEveryTenY:
    def test_overdue_past_decade(self):
        ref = _ref("Tdap", "every_10y", interval_days=3650, category="booster")
        rows = [_row("Tdap", "2010-01-01")]
        result = compute_coverage([ref], rows, today=date(2026, 5, 16))[0]
        assert result.status == STATUS_OVERDUE
        assert result.is_overdue is True

    def test_up_to_date_recent(self):
        ref = _ref("Tdap", "every_10y", interval_days=3650, category="booster")
        rows = [_row("Tdap", "2023-01-01")]
        result = compute_coverage([ref], rows, today=date(2026, 5, 16))[0]
        assert result.status == STATUS_UP_TO_DATE


class TestCoverageLifetimeAfterSeries:
    def test_up_to_date_after_full_series(self):
        ref = _ref("Hepatitis B", "lifetime_after_series",
                   primary_series_doses=3)
        rows = [
            _row("Hepatitis B", "2020-01-01", _id=1),
            _row("Hepatitis B", "2020-02-01", _id=2),
            _row("Hepatitis B", "2020-07-01", _id=3),
        ]
        result = compute_coverage([ref], rows, today=date(2026, 5, 16))[0]
        assert result.status == STATUS_UP_TO_DATE
        assert result.dose_count == 3

    def test_series_incomplete_with_no_doses(self):
        ref = _ref("Hepatitis B", "lifetime_after_series",
                   primary_series_doses=3)
        result = compute_coverage([ref], [], today=date(2026, 5, 16))[0]
        assert result.status == STATUS_SERIES_INCOMPLETE

    def test_series_incomplete_with_partial_doses(self):
        ref = _ref("Hepatitis B", "lifetime_after_series",
                   primary_series_doses=3)
        rows = [_row("Hepatitis B", "2020-01-01")]
        result = compute_coverage([ref], rows, today=date(2026, 5, 16))[0]
        assert result.status == STATUS_SERIES_INCOMPLETE
        assert result.dose_count == 1


class TestCoverageTravel:
    def test_up_to_date_within_interval(self):
        ref = _ref("Typhoid", "travel_pre_trip", interval_days=730,
                   category="travel")
        rows = [_row("Typhoid", "2025-01-01")]
        result = compute_coverage([ref], rows, today=date(2026, 5, 16))[0]
        assert result.status == STATUS_UP_TO_DATE
        assert result.is_overdue is False

    def test_expired_past_interval_no_due_soon(self):
        ref = _ref("Typhoid", "travel_pre_trip", interval_days=730,
                   category="travel")
        rows = [_row("Typhoid", "2023-01-01")]
        result = compute_coverage([ref], rows, today=date(2026, 5, 16))[0]
        # Travel pre_trip never enters due_soon — straight to expired.
        assert result.status == STATUS_EXPIRED
        assert result.is_overdue is True

    def test_never_recorded_when_no_dose(self):
        ref = _ref("Typhoid", "travel_pre_trip", interval_days=730,
                   category="travel")
        result = compute_coverage([ref], [], today=date(2026, 5, 16))[0]
        assert result.status == STATUS_NEVER_RECORDED


class TestCoverageRiskBased:
    def test_risk_based_when_no_dose(self):
        ref = _ref("Pneumococcal", "risk_based", category="risk_based")
        result = compute_coverage([ref], [], today=date(2026, 5, 16))[0]
        assert result.status == STATUS_RISK_BASED
        assert result.is_overdue is False

    def test_up_to_date_when_recorded(self):
        ref = _ref("Pneumococcal", "risk_based", category="risk_based")
        rows = [_row("Pneumococcal", "2024-09-01")]
        result = compute_coverage([ref], rows, today=date(2026, 5, 16))[0]
        assert result.status == STATUS_UP_TO_DATE


class TestCoverageDefaultsToToday:
    def test_today_defaults_to_real_today(self):
        # Just ensure today=None doesn't blow up.
        ref = _ref("Influenza", "annual", interval_days=365)
        result = compute_coverage([ref], [])
        assert result[0].status == STATUS_NEVER_RECORDED
