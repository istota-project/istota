"""Tests for the per-user health SQLite layer."""

from datetime import datetime, timezone

import pytest

from istota.health import db as health_db
from istota.health.workspace import synthesize_health_context
from istota.health._migrate import ensure_initialised


def _ctx(tmp_path):
    return synthesize_health_context("alice", tmp_path / "workspace")


class TestInitDb:
    def test_creates_tables(self, tmp_path):
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
        assert {
            "stats", "panels", "biomarkers", "biomarker_refs",
            "health_settings", "schema_meta",
        } <= tables

    def test_idempotent(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        health_db.init_db(ctx.db_path)
        health_db.init_db(ctx.db_path)

    def test_records_schema_version(self, tmp_path):
        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        health_db.init_db(ctx.db_path)
        with health_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT value FROM schema_meta WHERE key = 'version'"
            ).fetchone()
        assert row["value"] == str(health_db.SCHEMA_VERSION)


class TestStats:
    def test_insert_and_list(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            id_a = health_db.insert_stat(
                conn, metric="weight", value=82.5, unit="kg",
                measured_at="2026-05-01T10:00:00+00:00",
            )
            id_b = health_db.insert_stat(
                conn, metric="weight", value=82.0, unit="kg",
                measured_at="2026-05-08T10:00:00+00:00",
            )
            health_db.insert_stat(
                conn, metric="resting_hr", value=62, unit="bpm",
                measured_at="2026-05-08T10:00:00+00:00",
            )
            conn.commit()
            rows = health_db.list_stats(conn, metric="weight")
        assert [r.id for r in rows] == [id_b, id_a]
        assert rows[0].value == pytest.approx(82.0)

    def test_latest_per_metric(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            health_db.insert_stat(
                conn, metric="weight", value=83.0, unit="kg",
                measured_at="2026-05-01T10:00:00+00:00",
            )
            health_db.insert_stat(
                conn, metric="weight", value=82.0, unit="kg",
                measured_at="2026-05-08T10:00:00+00:00",
            )
            health_db.insert_stat(
                conn, metric="resting_hr", value=60, unit="bpm",
                measured_at="2026-05-08T10:00:00+00:00",
            )
            conn.commit()
            latest = health_db.latest_stats(conn)
        assert set(latest.keys()) == {"weight", "resting_hr"}
        assert latest["weight"].value == pytest.approx(82.0)
        assert latest["resting_hr"].value == pytest.approx(60.0)

    def test_delete_stat(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            sid = health_db.insert_stat(
                conn, metric="weight", value=82.0, unit="kg",
            )
            conn.commit()
            n = health_db.delete_stat(conn, sid)
            conn.commit()
        assert n == 1


class TestPanelsAndBiomarkers:
    def test_panel_lifecycle(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            pid = health_db.insert_panel(
                conn, drawn_at="2026-05-08", lab_name="Quest",
                panel_type="CBC",
            )
            health_db.insert_biomarker(
                conn, panel_id=pid, name="Hemoglobin",
                value=15.0, unit="g/dL",
                ref_range_low=13.5, ref_range_high=17.5,
            )
            health_db.insert_biomarker(
                conn, panel_id=pid, name="WBC",
                value=12.5, unit="10^3/uL",
                ref_range_low=4.0, ref_range_high=11.0,
                flag="H",
            )
            conn.commit()

            panel = health_db.get_panel(conn, pid)
            assert panel is not None
            assert panel.lab_name == "Quest"

            biomarkers = health_db.list_biomarkers_for_panel(conn, pid)
            assert len(biomarkers) == 2

            total, flagged = health_db.panel_counts(conn, pid)
            assert (total, flagged) == (2, 1)

            # CASCADE
            health_db.delete_panel(conn, pid)
            conn.commit()
            assert health_db.list_biomarkers_for_panel(conn, pid) == []

    def test_panel_collision(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            health_db.insert_panel(
                conn, drawn_at="2026-05-08", lab_name="Quest",
            )
            conn.commit()
            hit = health_db.find_panel_collision(
                conn, drawn_at="2026-05-08", lab_name="Quest",
            )
            miss = health_db.find_panel_collision(
                conn, drawn_at="2026-05-08", lab_name="Kaiser",
            )
        assert hit is not None
        assert miss is None

    def test_biomarker_trend_excludes_drafts(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            p1 = health_db.insert_panel(
                conn, drawn_at="2026-01-01", lab_name="Quest", draft=False,
            )
            p2 = health_db.insert_panel(
                conn, drawn_at="2026-05-01", lab_name="Quest", draft=True,
            )
            health_db.insert_biomarker(
                conn, panel_id=p1, name="LDL", value=110, unit="mg/dL",
            )
            health_db.insert_biomarker(
                conn, panel_id=p2, name="LDL", value=88, unit="mg/dL",
            )
            conn.commit()
            trend = health_db.biomarker_trend(conn, name="LDL")
        assert [d for _, d in trend] == ["2026-01-01"]
        assert trend[0][0].value == pytest.approx(110)

    def test_replace_biomarkers(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            pid = health_db.insert_panel(conn, drawn_at="2026-05-08")
            health_db.insert_biomarker(
                conn, panel_id=pid, name="WBC", value=8, unit="10^3/uL",
            )
            n = health_db.replace_biomarkers(conn, pid, [
                {"name": "WBC", "value": 9.0, "unit": "10^3/uL"},
                {"name": "RBC", "value": 5.0, "unit": "10^6/uL"},
            ])
            conn.commit()
        assert n == 2
        with health_db.connect(ctx.db_path) as conn:
            rows = health_db.list_biomarkers_for_panel(conn, pid)
        assert {r.name for r in rows} == {"WBC", "RBC"}


class TestBiomarkerRefs:
    def test_seed_idempotent(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            refs1 = health_db.list_biomarker_refs(conn)
        assert any(r.name == "Hemoglobin" for r in refs1)
        # Second call must not duplicate.
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            refs2 = health_db.list_biomarker_refs(conn)
        assert len(refs1) == len(refs2)

    def test_find_by_alias(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            ref = health_db.find_biomarker_ref_by_alias(conn, "Hgb")
        assert ref is not None
        assert ref.name == "Hemoglobin"

    def test_sex_specific_ranges_present(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            hgb = health_db.get_biomarker_ref(conn, "Hemoglobin")
        assert hgb is not None
        assert hgb.ref_range_low_m is not None
        assert hgb.ref_range_low_f is not None
        assert hgb.ref_range_low_m != hgb.ref_range_low_f


class TestSettings:
    def test_roundtrip(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            health_db.set_setting(conn, "dob", "1985-03-12")
            health_db.set_setting(conn, "height_cm", 178)
            health_db.set_setting(
                conn, "display_units",
                {"weight": "lb", "height": "cm", "temp": "F"},
            )
            conn.commit()
            settings = health_db.get_settings(conn)
        assert settings["dob"] == "1985-03-12"
        assert settings["height_cm"] == 178
        assert settings["display_units"]["weight"] == "lb"
