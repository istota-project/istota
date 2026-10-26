"""Tests for ``istota-skill health`` medication subcommands and their replay."""

from __future__ import annotations

import contextlib
import io
import json
import os
from datetime import date
from pathlib import Path
from unittest import mock

import pytest

from istota.health import db as health_db
from istota.health._migrate import ensure_initialised
from istota.health.workspace import synthesize_health_context
from istota.skills.health import main as health_main


def _invoke(args, env) -> tuple[int, str]:
    """`istota-skill health <args>` in-process, under exactly `env`."""
    out = io.StringIO()
    code = 0
    with mock.patch.dict(os.environ, env, clear=True), \
            mock.patch("sys.argv", ["health", *args]), \
            contextlib.redirect_stdout(out):
        try:
            health_main()
        except SystemExit as exc:
            code = exc.code if isinstance(exc.code, int) else (0 if exc.code is None else 1)
    return code, out.getvalue()


def _run(args, env, expect_success=True) -> dict:
    code, stdout = _invoke(args, env)
    if expect_success:
        assert code == 0, stdout
    else:
        assert code != 0, stdout
    return json.loads(stdout) if stdout.strip() else {}


@pytest.fixture
def ready(tmp_path) -> tuple[Path, dict]:
    ctx = synthesize_health_context("alice", tmp_path / "workspace")
    ensure_initialised(ctx)
    env = {
        **os.environ,
        "HEALTH_DB_PATH": str(ctx.db_path),
        "ISTOTA_DEFERRED_DIR": "",
        "ISTOTA_TASK_ID": "",
        "ISTOTA_USER_ID": "alice",
    }
    return ctx.db_path, env


@pytest.fixture
def deferred(ready, tmp_path) -> tuple[Path, dict, Path]:
    db_path, base_env = ready
    ddir = tmp_path / "deferred"
    ddir.mkdir()
    env = {**base_env, "ISTOTA_DEFERRED_DIR": str(ddir), "ISTOTA_TASK_ID": "77"}
    return db_path, env, ddir / "task_77_health_ops.json"


def _ops(path: Path) -> list[dict]:
    return json.loads(path.read_text())


def _seed(db_path: Path, **kwargs) -> int:
    with health_db.connect(db_path) as conn:
        mid = health_db.insert_medication(conn, **kwargs)
        conn.commit()
    return mid


class TestDirectMode:
    def test_add_list_show(self, ready):
        db_path, env = ready
        out = _run([
            "add-medication", "Atorvastatin", "--category", "Rx",
            "--dose", "20", "--dose-unit", "mg", "--frequency", "daily",
            "--route", "oral", "--prescriber", "Dr. Lee",
            "--start-date", "2026-01-15",
        ], env)
        assert out["status"] == "ok"
        assert "active_with_same_name" not in out
        mid = out["id"]

        listing = _run(["medications"], env)
        assert [m["id"] for m in listing["medications"]] == [mid]
        row = listing["medications"][0]
        assert row["category"] == "prescription"
        assert row["source"] == "agent"
        assert row["active"] is True

        detail = _run(["medication", str(mid)], env)
        assert detail["medication"]["prescriber"] == "Dr. Lee"
        assert detail["encounter"] is None

    def test_stopped_flag_records_a_past_medication(self, ready):
        _, env = ready
        _run(["add-medication", "Omeprazole", "--category", "otc",
              "--stopped", "--end-date", "2025-03-01"], env)
        assert _run(["medications"], env)["medications"] == []
        stopped = _run(["medications", "--status", "stopped"], env)
        assert [m["name"] for m in stopped["medications"]] == ["Omeprazole"]

    def test_duplicate_warning_still_inserts(self, ready):
        db_path, env = ready
        first = _seed(db_path, name="Atorvastatin", dose="20")
        out = _run(["add-medication", "atorvastatin", "--dose", "40"], env)
        assert out["active_with_same_name"] == [first]
        assert len(_run(["medications", "--name", "Atorvastatin"], env)["medications"]) == 2

    def test_update_and_undo_a_stop(self, ready):
        db_path, env = ready
        mid = _seed(db_path, name="Metformin", start_date="2026-01-01")
        _run(["stop-medication", str(mid), "--date", "2026-03-01"], env)
        with health_db.connect(db_path) as conn:
            m = health_db.get_medication(conn, mid)
        assert (m.active, m.end_date) == (False, "2026-03-01")

        _run(["update-medication", str(mid), "--active", "yes", "--end-date", ""], env)
        with health_db.connect(db_path) as conn:
            m = health_db.get_medication(conn, mid)
        assert (m.active, m.end_date) == (True, None)

    def test_update_with_no_fields_fails(self, ready):
        db_path, env = ready
        mid = _seed(db_path, name="Metformin")
        out = _run(["update-medication", str(mid)], env, expect_success=False)
        assert "no fields" in out["error"]

    def test_stop_before_start_fails(self, ready):
        db_path, env = ready
        mid = _seed(db_path, name="Metformin", start_date="2026-01-01")
        out = _run(["stop-medication", str(mid), "--date", "2025-12-01"], env,
                   expect_success=False)
        assert "before start_date" in out["error"]

    def test_delete_and_missing_row(self, ready):
        db_path, env = ready
        mid = _seed(db_path, name="Metformin")
        _run(["delete-medication", str(mid)], env)
        _run(["medication", str(mid)], env, expect_success=False)
        _run(["delete-medication", str(mid)], env, expect_success=False)

    def test_status_all_with_overlap_window(self, ready):
        db_path, env = ready
        _seed(db_path, name="Old", start_date="2025-01-01", end_date="2025-06-01", active=False)
        _seed(db_path, name="Current", start_date="2026-01-01")
        out = _run(["medications", "--status", "all",
                    "--since", "2026-02-01", "--until", "2026-03-31"], env)
        assert [m["name"] for m in out["medications"]] == ["Current"]


class TestDeferredMode:
    def test_add_defers_with_dedup_key_and_agent_source(self, deferred):
        _, env, ops_file = deferred
        out = _run(["add-medication", "Vitamin D3", "--category", "supplement",
                    "--dose", "5000", "--dose-unit", "IU",
                    "--start-date", "2026-05-01"], env)
        assert out["deferred"] is True
        op = _ops(ops_file)[0]
        assert op["op"] == "insert_medication"
        assert op["name"] == "Vitamin D3"
        assert op["category"] == "supplement"
        assert op["source"] == "agent"
        assert op["dedup_key"]
        assert op["active"] is True

    def test_bad_category_fails_before_deferring(self, deferred):
        _, env, ops_file = deferred
        out = _run(["add-medication", "Fish oil", "--category", "vitamin"], env,
                   expect_success=False)
        assert "prescription" in out["error"]
        assert not ops_file.exists()

    def test_impossible_date_and_reversed_range_fail_before_deferring(self, deferred):
        _, env, ops_file = deferred
        _run(["add-medication", "X", "--start-date", "2026-02-31"], env,
             expect_success=False)
        _run(["add-medication", "X", "--start-date", "2026-03-01",
              "--end-date", "2026-02-01"], env, expect_success=False)
        assert not ops_file.exists()

    def test_duplicate_warning_reads_the_db_while_deferring(self, deferred):
        db_path, env, _ = deferred
        first = _seed(db_path, name="Lipitor", generic_name="Atorvastatin")
        out = _run(["add-medication", "Atorvastatin"], env)
        assert out["deferred"] is True
        assert out["active_with_same_name"] == [first]

    def test_update_defers_and_maps_empty_to_null(self, deferred):
        _, env, ops_file = deferred
        _run(["update-medication", "5", "--dose", "40", "--end-date", "",
              "--active", "yes"], env)
        op = _ops(ops_file)[0]
        assert op == {
            "op": "update_medication", "medication_id": 5,
            "dose": "40", "end_date": None, "active": True,
        }

    def test_stop_without_date_writes_today(self, deferred):
        _, env, ops_file = deferred
        before = date.today().isoformat()
        _run(["stop-medication", "3"], env)
        after = date.today().isoformat()
        op = _ops(ops_file)[0]
        assert op["op"] == "discontinue_medication"
        assert op["medication_id"] == 3
        assert op["end_date"] in {before, after}

    def test_delete_defers(self, deferred):
        _, env, ops_file = deferred
        _run(["delete-medication", "4"], env)
        assert _ops(ops_file) == [{"op": "delete_medication", "medication_id": 4}]


def _replay(db_path: Path, deferred_dir: Path, ops: list[dict], task_id: int = 99) -> int:
    import istota.health as _health
    from istota import db as core_db
    from istota.health.models import HealthContext
    from istota.scheduler_deferred import _process_deferred_health_ops

    (deferred_dir / f"task_{task_id}_health_ops.json").write_text(json.dumps(ops))
    ctx = HealthContext(
        user_id="alice",
        workspace_root=db_path.parent.parent,
        data_dir=db_path.parent,
        db_path=db_path,
        uploads_dir=db_path.parent / "uploads",
    )
    ctx.ensure_dirs()
    task = core_db.Task(
        id=task_id, status="completed", source_type="cli",
        user_id="alice", prompt="",
    )
    with mock.patch.object(_health, "resolve_for_user", lambda uid, cfg: ctx):
        return _process_deferred_health_ops(object(), task, deferred_dir)


class TestSchedulerDeferredReplay:
    def test_all_four_ops(self, ready, tmp_path):
        db_path, _ = ready
        ddir = tmp_path / "deferred"
        ddir.mkdir()
        keep = _seed(db_path, name="Metformin", start_date="2026-01-01")
        gone = _seed(db_path, name="Typo")
        count = _replay(db_path, ddir, [
            {"op": "insert_medication", "dedup_key": "k1", "name": "Atorvastatin",
             "category": "rx", "dose": "20", "dose_unit": "mg",
             "start_date": "2026-01-15", "source": "agent", "active": True},
            {"op": "update_medication", "medication_id": keep, "dose": "1000"},
            {"op": "discontinue_medication", "medication_id": keep,
             "end_date": "2026-04-02"},
            {"op": "delete_medication", "medication_id": gone},
        ])
        assert count == 4
        with health_db.connect(db_path) as conn:
            rows = health_db.list_medications(conn, status="all")
        by_name = {m.name: m for m in rows}
        assert set(by_name) == {"Atorvastatin", "Metformin"}
        assert by_name["Atorvastatin"].category == "prescription"
        assert by_name["Atorvastatin"].source == "agent"
        assert by_name["Metformin"].dose == "1000"
        assert (by_name["Metformin"].active, by_name["Metformin"].end_date) == (
            False, "2026-04-02",
        )

    def test_a_replayed_insert_writes_one_row(self, ready, tmp_path):
        db_path, _ = ready
        ddir = tmp_path / "deferred"
        ddir.mkdir()
        op = {"op": "insert_medication", "dedup_key": "same", "name": "Aspirin",
              "category": "otc"}
        _replay(db_path, ddir, [op])
        _replay(db_path, ddir, [op])
        with health_db.connect(db_path) as conn:
            assert len(health_db.list_medications(conn, status="all")) == 1

    def test_a_bad_op_between_two_good_ones(self, ready, tmp_path):
        db_path, _ = ready
        ddir = tmp_path / "deferred"
        ddir.mkdir()
        count = _replay(db_path, ddir, [
            {"op": "insert_medication", "dedup_key": "a", "name": "First"},
            {"op": "insert_medication", "dedup_key": "b", "name": "Bad",
             "category": "vitamin"},
            {"op": "insert_medication", "dedup_key": "c", "name": "Third",
             "start_date": "2026-02-31"},
            {"op": "insert_medication", "dedup_key": "d", "name": "Last"},
        ])
        assert count == 2
        with health_db.connect(db_path) as conn:
            names = {m.name for m in health_db.list_medications(conn, status="all")}
        assert names == {"First", "Last"}

    def test_an_unknown_source_becomes_agent(self, ready, tmp_path):
        db_path, _ = ready
        ddir = tmp_path / "deferred"
        ddir.mkdir()
        _replay(db_path, ddir, [
            {"op": "insert_medication", "dedup_key": "s", "name": "Zinc",
             "category": "supplement", "source": "import"},
        ])
        with health_db.connect(db_path) as conn:
            (m,) = health_db.list_medications(conn)
        assert m.source == "agent"


class TestMedicationDocuments:
    @pytest.fixture
    def scoped(self, ready, tmp_path) -> tuple[Path, dict, Path]:
        """Direct mode with a workspace a source file may sit in."""
        db_path, env = ready
        workspace = tmp_path / "mount" / "Users" / "alice"
        workspace.mkdir(parents=True)
        env = {**env, "NEXTCLOUD_MOUNT_PATH": str(tmp_path / "mount")}
        return db_path, env, workspace

    def test_attach_direct_and_show(self, scoped):
        db_path, env, workspace = scoped
        mid = _seed(db_path, name="Atorvastatin")
        src = workspace / "label.pdf"
        src.write_bytes(b"%PDF-1.4 prescription label")

        out = _run(
            ["attach-document", "--path", str(src), "--to", f"medication:{mid}"],
            env,
        )
        assert out["entity_type"] == "medication"

        detail = _run(["medication", str(mid)], env)
        assert [d["id"] for d in detail["documents"]] == [out["id"]]
        assert detail["documents"][0]["filename"] == "label.pdf"

    def test_ref_defers_as_a_medication_ref(self, scoped, tmp_path):
        db_path, env, workspace = scoped
        ddir = tmp_path / "deferred"
        ddir.mkdir()
        env = {**env, "ISTOTA_DEFERRED_DIR": str(ddir), "ISTOTA_TASK_ID": "80"}
        src = workspace / "label.pdf"
        src.write_bytes(b"%PDF-1.4 prescription label")

        _run(["add-medication", "Atorvastatin", "--ref", "statin"], env)
        _run(
            ["attach-document", "--path", str(src), "--to", "medication:@statin"],
            env,
        )
        ops = _ops(ddir / "task_80_health_ops.json")
        assert ops[0]["op"] == "insert_medication"
        assert ops[0]["ref"] == "statin"
        assert ops[1]["medication_ref"] == "statin"
        assert "entity_id" not in ops[1]
        assert "encounter_ref" not in ops[1]

    def test_ref_in_direct_mode_is_not_passed_to_the_insert(self, ready):
        """`ref` is a replay-only key; the direct insert must not receive it."""
        _, env = ready
        out = _run(["add-medication", "Atorvastatin", "--ref", "statin"], env)
        assert out["status"] == "ok"

    def test_a_medication_ref_needs_a_sandboxed_task(self, scoped):
        _, env, workspace = scoped
        src = workspace / "label.pdf"
        src.write_bytes(b"%PDF-1.4 prescription label")
        out = _run(
            ["attach-document", "--path", str(src), "--to", "medication:@x"],
            env, expect_success=False,
        )
        assert "medication:@ref only resolves inside a sandboxed task" in out["error"]
