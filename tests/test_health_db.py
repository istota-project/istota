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
            "encounters", "diagnoses",
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

    def test_migrates_pre_content_hash_db(self, tmp_path):
        """A DB created before the content_hash column must migrate cleanly.

        Regression for prod 500s where executescript hit
        ``CREATE INDEX … ON panels(content_hash)`` before the migration's
        ALTER on the existing panels table.
        """
        import sqlite3

        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        # Materialise an older panels table without the content_hash column.
        conn = sqlite3.connect(ctx.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE panels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drawn_at TEXT NOT NULL,
                    lab_name TEXT,
                    panel_type TEXT,
                    source_file TEXT,
                    source_mime TEXT,
                    ocr_text TEXT,
                    draft INTEGER NOT NULL DEFAULT 0,
                    notes TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO panels (drawn_at) VALUES ('2026-05-01T00:00:00+00:00');
                """,
            )
            conn.commit()
        finally:
            conn.close()
        # This used to raise sqlite3.OperationalError: no such column.
        health_db.init_db(ctx.db_path)
        with health_db.connect(ctx.db_path) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(panels)")}
            assert "content_hash" in cols
            indices = {
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'index'"
                ).fetchall()
            }
            assert "idx_panels_content_hash" in indices
            # Pre-existing rows survive with a NULL hash.
            row = conn.execute("SELECT content_hash FROM panels").fetchone()
            assert row["content_hash"] is None


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


class TestRecanonicalize:
    def test_rewrites_alias_to_canonical(self, tmp_path):
        # ensure_initialised must rewrite biomarker rows that match an
        # alias of a canonical ref. Regression for the CSV-import path
        # that stored raw column names before the alias table caught up.
        from istota.health._migrate import recanonicalize_biomarker_names

        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            pid = health_db.insert_panel(
                conn, drawn_at="2026-05-08", lab_name="Quest", draft=False,
            )
            # Insert a row under a raw alias name (NOT canonical).
            bid = health_db.insert_biomarker(
                conn, panel_id=pid, name="Cholesterol",
                value=180, unit="mg/dL",
            )
            conn.commit()
            # Force a re-run: clear the recanon sentinel.
            conn.execute(
                "DELETE FROM schema_meta WHERE key = 'biomarker_recanonicalize_hash'",
            )
            conn.commit()

        fixed = recanonicalize_biomarker_names(ctx)
        assert fixed == 1
        with health_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT name FROM biomarkers WHERE id = ?", (bid,),
            ).fetchone()
        assert row["name"] == "Cholesterol_Total"

        # Idempotent — second call is a no-op.
        assert recanonicalize_biomarker_names(ctx) == 0


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


class TestEncounters:
    def test_insert_and_get(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn,
                encounter_date="2026-05-13",
                encounter_type="procedure",
                provider="Dr. Smith",
                facility="Kaiser Sunset",
                specialty="gastroenterology",
                reason="Screening colonoscopy",
                notes="Grade I-II hemorrhoids found.",
            )
            conn.commit()
            enc = health_db.get_encounter(conn, eid)
        assert enc is not None
        assert enc.encounter_type == "procedure"
        assert enc.provider == "Dr. Smith"
        assert enc.facility == "Kaiser Sunset"

    def test_list_filters(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            a = health_db.insert_encounter(
                conn, encounter_date="2026-01-15", encounter_type="visit",
            )
            b = health_db.insert_encounter(
                conn, encounter_date="2026-05-13", encounter_type="procedure",
            )
            health_db.insert_encounter(
                conn, encounter_date="2025-09-01", encounter_type="screening",
            )
            conn.commit()
            recent = health_db.list_encounters(conn, since="2026-01-01")
            procs = health_db.list_encounters(conn, encounter_type="procedure")
        assert [e.id for e in recent] == [b, a]
        assert [e.id for e in procs] == [b]

    def test_update_encounter(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="procedure", notes="initial",
            )
            n = health_db.update_encounter(
                conn, eid, notes="Follow-up in 3 years",
                facility="Kaiser",
            )
            conn.commit()
        assert n == 1
        with health_db.connect(ctx.db_path) as conn:
            enc = health_db.get_encounter(conn, eid)
        assert enc.notes == "Follow-up in 3 years"
        assert enc.facility == "Kaiser"

    def test_update_encounter_clears_nullable_fields(self, tmp_path):
        # Explicit None on nullable fields must actually clear them.
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="procedure",
                provider="Dr Smith", facility="Kaiser",
                specialty="GI", reason="screening", notes="initial",
            )
            n = health_db.update_encounter(
                conn, eid, provider=None, facility=None,
                specialty=None, reason=None, notes=None,
            )
            conn.commit()
        assert n == 1
        with health_db.connect(ctx.db_path) as conn:
            enc = health_db.get_encounter(conn, eid)
        assert enc.provider is None
        assert enc.facility is None
        assert enc.specialty is None
        assert enc.reason is None
        assert enc.notes is None

    def test_update_encounter_rejects_none_required(self, tmp_path):
        # encounter_date / encounter_type are NOT NULL; explicit None is a no-op.
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="procedure",
            )
            # Only None for required fields → 0 rows changed.
            n = health_db.update_encounter(
                conn, eid, encounter_date=None, encounter_type=None,
            )
            conn.commit()
        assert n == 0
        with health_db.connect(ctx.db_path) as conn:
            enc = health_db.get_encounter(conn, eid)
        assert enc.encounter_date == "2026-05-13"
        assert enc.encounter_type == "procedure"

    def test_delete_clears_panel_fk(self, tmp_path):
        # SET NULL on encounter delete must propagate to panels.encounter_id.
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="procedure",
            )
            pid = health_db.insert_panel(
                conn, drawn_at="2026-05-13", lab_name="Quest",
                encounter_id=eid,
            )
            conn.commit()
            health_db.delete_encounter(conn, eid)
            conn.commit()
            panel = health_db.get_panel(conn, pid)
        assert panel is not None
        assert panel.encounter_id is None

    def test_panels_for_encounter(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="visit",
            )
            p1 = health_db.insert_panel(
                conn, drawn_at="2026-05-13",
                lab_name="Quest", encounter_id=eid,
            )
            health_db.insert_panel(
                conn, drawn_at="2026-05-13", lab_name="Kaiser",
            )
            conn.commit()
            linked = health_db.panels_for_encounter(conn, eid)
        assert [p.id for p in linked] == [p1]


class TestDiagnoses:
    def test_insert_and_status_filter(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            active = health_db.insert_diagnosis(
                conn, name="Internal hemorrhoids",
                date_diagnosed="2026-05-13", severity="mild",
                icd10="K64.0",
            )
            chronic = health_db.insert_diagnosis(
                conn, name="Hypertension", status="chronic",
                date_diagnosed="2020-01-15",
            )
            resolved = health_db.insert_diagnosis(
                conn, name="Strep throat", status="resolved",
                date_diagnosed="2024-12-01",
                date_resolved="2024-12-15",
            )
            conn.commit()
            actives = health_db.list_diagnoses(conn, status="active")
            chronics = health_db.list_diagnoses(conn, status="chronic")
            all_d = health_db.list_diagnoses(conn, status="all")
        assert [d.id for d in actives] == [active]
        assert [d.id for d in chronics] == [chronic]
        # default ordering: active → chronic → resolved
        assert [d.id for d in all_d] == [active, chronic, resolved]

    def test_unknown_status_rejected(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            with pytest.raises(ValueError):
                health_db.insert_diagnosis(conn, name="X", status="bogus")

    def test_update_marks_resolved(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            did = health_db.insert_diagnosis(
                conn, name="Hemorrhoids", date_diagnosed="2026-05-13",
            )
            health_db.update_diagnosis(
                conn, did, status="resolved", date_resolved="2026-06-15",
            )
            conn.commit()
            d = health_db.get_diagnosis(conn, did)
        assert d.status == "resolved"
        assert d.date_resolved == "2026-06-15"

    def test_diagnoses_for_encounter(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="procedure",
            )
            d_linked = health_db.insert_diagnosis(
                conn, name="Hemorrhoids", encounter_id=eid,
                date_diagnosed="2026-05-13",
            )
            health_db.insert_diagnosis(
                conn, name="Unrelated",
                date_diagnosed="2024-01-01",
            )
            conn.commit()
            linked = health_db.diagnoses_for_encounter(conn, eid)
            up = health_db.encounters_for_diagnosis(conn, d_linked)
        assert [d.id for d in linked] == [d_linked]
        assert [e.id for e in up] == [eid]

    def test_delete_encounter_sets_diagnosis_fk_null(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="procedure",
            )
            did = health_db.insert_diagnosis(
                conn, name="Hemorrhoids", encounter_id=eid,
            )
            conn.commit()
            health_db.delete_encounter(conn, eid)
            conn.commit()
            d = health_db.get_diagnosis(conn, did)
        assert d.encounter_id is None


class TestDeferredEncounterReplay:
    def test_replay_inserts(self, tmp_path):
        """The scheduler replays deferred encounter/diagnosis ops."""
        import json
        from unittest.mock import MagicMock

        from istota import health as health_pkg
        from istota import scheduler_deferred

        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        user_temp = tmp_path / "user_temp"
        user_temp.mkdir()
        ops_file = user_temp / "task_55_health_ops.json"
        ops_file.write_text(json.dumps([
            {
                "op": "insert_encounter",
                "encounter_date": "2026-05-13",
                "encounter_type": "procedure",
                "provider": "Dr. Smith",
            },
            {
                "op": "insert_diagnosis",
                "name": "Hemorrhoids",
                "encounter_id": 1,
                "date_diagnosed": "2026-05-13",
            },
        ]))
        fake_resolve = MagicMock(return_value=ctx)
        original = health_pkg.resolve_for_user
        health_pkg.resolve_for_user = fake_resolve
        try:
            count = scheduler_deferred._process_deferred_health_ops(
                MagicMock(), MagicMock(id=55, user_id="alice"), user_temp,
            )
        finally:
            health_pkg.resolve_for_user = original
        assert count == 2
        with health_db.connect(ctx.db_path) as conn:
            encs = health_db.list_encounters(conn)
            diags = health_db.list_diagnoses(conn)
        assert [e.provider for e in encs] == ["Dr. Smith"]
        assert [d.name for d in diags] == ["Hemorrhoids"]
        assert diags[0].encounter_id == encs[0].id

    def test_replay_update_and_delete(self, tmp_path):
        import json
        from unittest.mock import MagicMock

        from istota import health as health_pkg
        from istota import scheduler_deferred

        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="procedure",
            )
            did = health_db.insert_diagnosis(
                conn, name="Hemorrhoids",
                date_diagnosed="2026-05-13",
            )
            conn.commit()
        user_temp = tmp_path / "user_temp"
        user_temp.mkdir()
        ops_file = user_temp / "task_99_health_ops.json"
        ops_file.write_text(json.dumps([
            {
                "op": "update_diagnosis",
                "diagnosis_id": did,
                "status": "resolved",
                "date_resolved": "2026-06-15",
            },
            {
                "op": "update_encounter",
                "encounter_id": eid,
                "notes": "Follow-up in 3 years",
            },
            {"op": "delete_encounter", "encounter_id": eid},
        ]))
        fake_resolve = MagicMock(return_value=ctx)
        original = health_pkg.resolve_for_user
        health_pkg.resolve_for_user = fake_resolve
        try:
            count = scheduler_deferred._process_deferred_health_ops(
                MagicMock(), MagicMock(id=99, user_id="alice"), user_temp,
            )
        finally:
            health_pkg.resolve_for_user = original
        assert count == 3
        with health_db.connect(ctx.db_path) as conn:
            assert health_db.get_encounter(conn, eid) is None
            d = health_db.get_diagnosis(conn, did)
        assert d.status == "resolved"
        assert d.date_resolved == "2026-06-15"
        # encounter_id should have been NULLed by ON DELETE SET NULL
        assert d.encounter_id is None

    def test_replay_failure_writes_sidecar(self, tmp_path):
        """A bad op must surface as an ERROR + a failure sidecar file."""
        import json
        from unittest.mock import MagicMock

        from istota import health as health_pkg
        from istota import scheduler_deferred

        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        user_temp = tmp_path / "user_temp"
        user_temp.mkdir()
        ops_file = user_temp / "task_88_health_ops.json"
        # Bad op: missing required encounter_date.
        ops_file.write_text(json.dumps([
            {"op": "insert_encounter", "encounter_type": "procedure"},
        ]))
        fake_resolve = MagicMock(return_value=ctx)
        original = health_pkg.resolve_for_user
        health_pkg.resolve_for_user = fake_resolve
        try:
            scheduler_deferred._process_deferred_health_ops(
                MagicMock(), MagicMock(id=88, user_id="alice"), user_temp,
            )
        finally:
            health_pkg.resolve_for_user = original
        sidecar = user_temp / "task_88_health_op_failures.json"
        assert sidecar.exists()
        payload = json.loads(sidecar.read_text())
        assert len(payload) == 1
        assert payload[0]["op"]["op"] == "insert_encounter"
        assert "encounter_date" in payload[0]["error"] or "KeyError" in payload[0]["error"]

    def test_replay_is_idempotent_on_dedup_key(self, tmp_path):
        """Replaying the same insert op twice must not duplicate the row."""
        import json
        from unittest.mock import MagicMock

        from istota import health as health_pkg
        from istota import scheduler_deferred

        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        user_temp = tmp_path / "user_temp"
        user_temp.mkdir()
        ops_payload = [
            {
                "op": "insert_encounter",
                "dedup_key": "deadbeef",
                "encounter_date": "2026-05-13",
                "encounter_type": "procedure",
                "provider": "Dr. Smith",
            },
            {
                "op": "insert_diagnosis",
                "dedup_key": "cafef00d",
                "name": "Hemorrhoids",
                "date_diagnosed": "2026-05-13",
            },
        ]
        fake_resolve = MagicMock(return_value=ctx)
        original = health_pkg.resolve_for_user
        health_pkg.resolve_for_user = fake_resolve
        try:
            for _ in range(2):
                ops_file = user_temp / "task_77_health_ops.json"
                ops_file.write_text(json.dumps(ops_payload))
                scheduler_deferred._process_deferred_health_ops(
                    MagicMock(), MagicMock(id=77, user_id="alice"), user_temp,
                )
        finally:
            health_pkg.resolve_for_user = original
        with health_db.connect(ctx.db_path) as conn:
            encs = health_db.list_encounters(conn)
            diags = health_db.list_diagnoses(conn)
        assert len(encs) == 1
        assert len(diags) == 1


class TestPanelEncounterMigration:
    def test_migrates_pre_encounter_db(self, tmp_path):
        """A panels table created before encounter_id must migrate cleanly."""
        import sqlite3

        ctx = _ctx(tmp_path)
        ctx.ensure_dirs()
        conn = sqlite3.connect(ctx.db_path)
        try:
            conn.executescript(
                """
                CREATE TABLE panels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    drawn_at TEXT NOT NULL,
                    lab_name TEXT,
                    panel_type TEXT,
                    source_file TEXT,
                    source_mime TEXT,
                    ocr_text TEXT,
                    draft INTEGER NOT NULL DEFAULT 0,
                    notes TEXT,
                    content_hash TEXT,
                    created_at TEXT NOT NULL DEFAULT (datetime('now'))
                );
                INSERT INTO panels (drawn_at) VALUES ('2026-05-01T00:00:00+00:00');
                """,
            )
            conn.commit()
        finally:
            conn.close()
        health_db.init_db(ctx.db_path)
        with health_db.connect(ctx.db_path) as conn:
            cols = {r[1] for r in conn.execute("PRAGMA table_info(panels)")}
            assert "encounter_id" in cols
            row = conn.execute("SELECT encounter_id FROM panels").fetchone()
            assert row["encounter_id"] is None

    def test_panel_insert_with_encounter_id(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="visit",
            )
            pid = health_db.insert_panel(
                conn, drawn_at="2026-05-13", lab_name="Quest",
                encounter_id=eid,
            )
            conn.commit()
            panel = health_db.get_panel(conn, pid)
        assert panel.encounter_id == eid

    def test_update_panel_clears_encounter_id(self, tmp_path):
        ctx = _ctx(tmp_path)
        ensure_initialised(ctx)
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn, encounter_date="2026-05-13",
                encounter_type="visit",
            )
            pid = health_db.insert_panel(
                conn, drawn_at="2026-05-13", encounter_id=eid,
            )
            conn.commit()
            health_db.update_panel(conn, pid, encounter_id=None)
            conn.commit()
            panel = health_db.get_panel(conn, pid)
        assert panel.encounter_id is None
