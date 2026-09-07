"""Tests for ``istota-skill health`` immunization subcommands."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from istota.health._migrate import ensure_initialised
from istota.health.workspace import synthesize_health_context


def _run(args, env, expect_success=True) -> dict:
    proc = subprocess.run(
        [sys.executable, "-m", "istota.skills.health", *args],
        capture_output=True, text=True, env=env,
    )
    if expect_success:
        assert proc.returncode == 0, (proc.stdout, proc.stderr)
    return json.loads(proc.stdout) if proc.stdout.strip() else {}


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
        # `--paste-file` is a host path and this CLI runs host-side, so it is
        # scoped to the caller's own workspace (ISSUE-447). This is what the
        # executor would have put in the environment; the `workspace` fixture
        # below is the one directory a paste file may be read from.
        "NEXTCLOUD_MOUNT_PATH": str(tmp_path / "mount"),
    }
    return ctx.db_path, env


@pytest.fixture
def workspace(tmp_path) -> Path:
    """`{mount}/Users/alice`, the root every scoped path here resolves under."""
    path = tmp_path / "mount" / "Users" / "alice"
    path.mkdir(parents=True)
    return path


class TestVaccineRefsAndCoverage:
    def test_vaccine_refs(self, ready):
        _, env = ready
        out = _run(["vaccine-refs"], env)
        names = {r["name"] for r in out["refs"]}
        assert "Influenza" in names
        assert "Tdap" in names

    def test_coverage_filters(self, ready):
        _, env = ready
        out = _run(["coverage"], env)
        assert len(out["coverage"]) > 0
        # Filter to overdue (no rows yet, so should be empty).
        out = _run(["coverage", "--overdue"], env)
        assert out["coverage"] == []


class TestAddImmunizationDirect:
    def test_add_and_list(self, ready):
        _, env = ready
        out = _run([
            "add-immunization",
            "--name", "Influenza",
            "--date", "2025-11-28",
            "--product-name", "Fluzone trivalent",
            "--facility", "CVS Pharmacy",
        ], env)
        assert out["status"] == "ok"
        iid = out["id"]
        listing = _run(["immunizations"], env)
        assert len(listing["immunizations"]) == 1
        assert listing["immunizations"][0]["product_name"] == "Fluzone trivalent"
        detail = _run(["immunization", str(iid)], env)
        assert detail["immunization"]["name"] == "Influenza"

    def test_update_and_delete(self, ready):
        _, env = ready
        out = _run([
            "add-immunization", "--name", "Tdap", "--date", "2016-12-01",
        ], env)
        iid = out["id"]
        _run(["update-immunization", str(iid), "--lot-number", "ABC123"], env)
        detail = _run(["immunization", str(iid)], env)
        assert detail["immunization"]["lot_number"] == "ABC123"
        _run(["delete-immunization", str(iid)], env)
        # After delete, immunization 404 is a CLI failure.
        proc = subprocess.run(
            [sys.executable, "-m", "istota.skills.health",
             "immunization", str(iid)],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode != 0


class TestDeferredOps:
    def test_add_defers(self, ready, tmp_path):
        _, base_env = ready
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {**base_env, "ISTOTA_DEFERRED_DIR": str(deferred),
               "ISTOTA_TASK_ID": "42"}
        out = _run([
            "add-immunization", "--name", "Influenza", "--date", "2025-11-28",
        ], env)
        assert out["deferred"] is True
        ops_file = deferred / "task_42_health_ops.json"
        assert ops_file.exists()
        ops = json.loads(ops_file.read_text())
        assert ops[0]["op"] == "insert_immunization"
        assert ops[0]["name"] == "Influenza"
        assert ops[0]["date_given"] == "2025-11-28"
        assert ops[0]["dedup_key"]

    def test_update_defers(self, ready, tmp_path):
        _, base_env = ready
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {**base_env, "ISTOTA_DEFERRED_DIR": str(deferred),
               "ISTOTA_TASK_ID": "9"}
        out = _run([
            "update-immunization", "5", "--lot-number", "XYZ",
        ], env)
        assert out["deferred"] is True
        ops = json.loads((deferred / "task_9_health_ops.json").read_text())
        assert ops[0]["op"] == "update_immunization"
        assert ops[0]["immunization_id"] == 5
        assert ops[0]["lot_number"] == "XYZ"

    def test_delete_defers(self, ready, tmp_path):
        _, base_env = ready
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {**base_env, "ISTOTA_DEFERRED_DIR": str(deferred),
               "ISTOTA_TASK_ID": "10"}
        _run(["delete-immunization", "7"], env)
        ops = json.loads((deferred / "task_10_health_ops.json").read_text())
        assert ops[0]["op"] == "delete_immunization"
        assert ops[0]["immunization_id"] == 7


class TestImportImmunizations:
    def test_dry_run_inline(self, ready, workspace):
        _, env = ready
        paste_file = workspace / "paste.txt"
        paste_file.write_text(
            "INFS Pres Free 6mos-Adult (Fluzone trivalent) (influenza) "
            "(Given 11/28/2025)\n"
            "Tdap (Given 12/1/2016)\n"
        )
        out = _run([
            "import-immunizations", "--paste-file", str(paste_file), "--dry-run",
        ], env)
        assert out["dry_run"] is True
        names = [r["name"] for r in out["rows"]]
        assert names == ["Influenza", "Tdap"]

    def test_confirm_writes(self, ready, workspace):
        _, env = ready
        paste_file = workspace / "paste.txt"
        paste_file.write_text(
            "Influenza (Given 11/28/2025)\nTdap (Given 12/1/2016)\n"
        )
        out = _run([
            "import-immunizations", "--paste-file", str(paste_file), "--confirm",
        ], env)
        assert out["status"] == "ok"
        assert out["count"] == 2
        listing = _run(["immunizations"], env)
        assert {r["name"] for r in listing["immunizations"]} == {
            "Influenza", "Tdap",
        }

    def test_confirm_requires_dates(self, ready, workspace):
        _, env = ready
        paste_file = workspace / "paste.txt"
        paste_file.write_text("Got my flu shot at the pharmacy\n")
        proc = subprocess.run(
            [sys.executable, "-m", "istota.skills.health",
             "import-immunizations", "--paste-file", str(paste_file),
             "--confirm"],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode != 0
        body = json.loads(proc.stdout)
        assert "no usable date_given" in body["error"]
        # The offending line is named, so --dry-run is not the only way to
        # find out which row is at fault.
        assert "Got my flu shot at the pharmacy" in body["error"]

    def test_confirm_names_the_line_whose_date_is_impossible(self, ready, workspace):
        """The row that used to import, and the message that used to lie.

        ``Influenza 2026-02-31`` parsed to a high-confidence date before
        this stage and went straight to the insert, which never validated
        it. It now refuses the batch — the right direction, since
        ``immunizations._parse_date`` cannot read that value back and the
        dose would have been silently absent from coverage — but the old
        wording, "missing date_given", reads as a lie against a line that
        visibly carries a date.
        """
        _, env = ready
        paste_file = workspace / "paste.txt"
        paste_file.write_text("Influenza 2026-02-31\n")
        proc = subprocess.run(
            [sys.executable, "-m", "istota.skills.health",
             "import-immunizations", "--paste-file", str(paste_file),
             "--confirm"],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode != 0
        body = json.loads(proc.stdout)
        assert "not a real date" in body["error"]
        assert "Influenza 2026-02-31" in body["error"]

    def test_defers_under_sandbox(self, ready, workspace, tmp_path):
        _, base_env = ready
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {**base_env, "ISTOTA_DEFERRED_DIR": str(deferred),
               "ISTOTA_TASK_ID": "55"}
        paste_file = workspace / "paste.txt"
        paste_file.write_text("Influenza (Given 11/28/2025)\n")
        out = _run([
            "import-immunizations", "--paste-file", str(paste_file), "--confirm",
        ], env)
        assert out["deferred"] is True
        ops = json.loads((deferred / "task_55_health_ops.json").read_text())
        assert ops[0]["op"] == "bulk_insert_immunizations"
        assert ops[0]["dedup_key_prefix"]
        assert len(ops[0]["rows"]) == 1


class TestThePasteMigration:
    """`--paste @PATH` was the one genuine inline host read in this skill.

    The other three health file verbs defer on every model-driven path, so
    what they leaked was an existence probe. This one read the file itself,
    on every deployment shape, before any deferral — and `--dry-run` handed
    the parsed content straight back to the caller. `@` in a value argparse
    delivers as a string is also invisible to every enumeration of path
    arguments in the tree, which is why the read moves to a flag of its own
    rather than being scoped where it stood (ISSUE-447).

    The refusal matters as much as the move. Treating `@/etc/passwd` as
    literal text after taking the meaning away would import the string
    `@/etc/passwd` as an immunization record and report success.
    """

    def test_a_leading_at_is_refused_and_names_the_new_flag(self, ready, workspace):
        _, env = ready
        paste_file = workspace / "paste.txt"
        paste_file.write_text("Influenza (Given 11/28/2025)\n")

        out = _run(
            ["import-immunizations", "--paste", f"@{paste_file}", "--dry-run"],
            env, expect_success=False,
        )
        assert out["status"] == "error"
        assert "--paste-file" in out["error"]

    def test_a_refused_at_value_is_not_imported_as_text(self, ready, workspace):
        """The silent reading, asserted against rather than described."""
        _, env = ready
        paste_file = workspace / "paste.txt"
        paste_file.write_text("Influenza (Given 11/28/2025)\n")

        out = _run(
            ["import-immunizations", "--paste", f"@{paste_file}", "--confirm"],
            env, expect_success=False,
        )
        assert out.get("rows") is None
        assert out.get("count") is None
        listing = _run(["immunizations"], env)
        assert listing["immunizations"] == []

    def test_a_paste_file_outside_the_workspace_is_refused(self, ready, tmp_path):
        """The scoping the flag exists to carry."""
        _, env = ready
        outside = tmp_path / "outside.txt"
        outside.write_text("Influenza (Given 11/28/2025)\n")

        out = _run(
            ["import-immunizations", "--paste-file", str(outside), "--dry-run"],
            env, expect_success=False,
        )
        assert out["status"] == "error"
        assert out["reason"] == "host_path_refused"

    def test_literal_text_still_goes_through_paste(self, ready):
        _, env = ready
        out = _run(
            ["import-immunizations", "--paste", "Influenza (Given 11/28/2025)",
             "--dry-run"],
            env,
        )
        assert [r["name"] for r in out["rows"]] == ["Influenza"]

    def test_neither_flag_is_an_error_rather_than_a_usage_dump(self, ready):
        """`--paste` was `required=True` and the pair cannot be.

        argparse would answer a missing required argument with usage text on
        stderr and exit 2; the facade's contract is one JSON envelope on
        stdout, which is what every other refusal in this CLI produces.
        """
        _, env = ready
        out = _run(["import-immunizations", "--dry-run"], env, expect_success=False)
        assert out["status"] == "error"
        assert "--paste" in out["error"]

    def test_both_flags_at_once_is_an_error(self, ready, workspace):
        _, env = ready
        paste_file = workspace / "paste.txt"
        paste_file.write_text("Influenza (Given 11/28/2025)\n")
        out = _run(
            ["import-immunizations", "--paste", "Tdap (Given 12/1/2016)",
             "--paste-file", str(paste_file), "--dry-run"],
            env, expect_success=False,
        )
        assert out["status"] == "error"


class TestExplainImmunization:
    def test_static_for_up_to_date(self, ready):
        _, env = ready
        _run([
            "add-immunization", "--name", "Influenza",
            "--date", "2026-05-01",
        ], env)
        out = _run(["explain-immunization", "Influenza"], env)
        # Curated content shows regardless of coverage status now.
        assert out["source"] == "static"
        assert out["summary"]

    def test_static_payload_for_eligible_vaccine(self, ready):
        _, env = ready
        _run([
            "add-immunization", "--name", "Tdap",
            "--date", "2005-01-01",
        ], env)
        out = _run(["explain-immunization", "Tdap"], env)
        # Overdue Tdap → eligible → served from bundled static JSON.
        assert out["source"] == "static"
        assert out["status"] == "overdue"
        assert out["summary"]
        assert out["why_it_matters"]
        assert "considerations" not in out

    def test_unknown_vaccine_fails(self, ready):
        _, env = ready
        proc = subprocess.run(
            [sys.executable, "-m", "istota.skills.health",
             "explain-immunization", "Notarealvaccine"],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode != 0


class TestSchedulerDeferredReplay:
    """Round-trip: CLI defers → scheduler_deferred replays."""

    def test_replay_insert_update_delete(self, ready, tmp_path):
        """Each new op is recognised by the replayer and writes to DB."""
        from istota.health import db as health_db
        from istota.health.models import HealthContext
        from istota.scheduler_deferred import _process_deferred_health_ops
        from istota import db as core_db

        db_path, _ = ready
        deferred = tmp_path / "deferred"
        deferred.mkdir()

        # Write ops file manually — covers the contract directly.
        ops = [
            {
                "op": "insert_immunization",
                "dedup_key": "task-99:0",
                "name": "Influenza",
                "date_given": "2025-11-28",
                "product_name": "Fluzone",
            },
            {
                "op": "bulk_insert_immunizations",
                "dedup_key_prefix": "task-99:bulk",
                "rows": [
                    {"name": "Tdap", "date_given": "2016-12-01"},
                    {"name": "MMR", "date_given": "1990-01-01"},
                    # Missing fields → skipped.
                    {"name": ""},
                ],
            },
        ]
        (deferred / "task_99_health_ops.json").write_text(json.dumps(ops))

        # Build a fake Config + Task that resolve_for_user can consume.
        class _FakeConfig:
            pass

        config = _FakeConfig()
        # Patch resolve_for_user to return the ctx we already wired.
        import istota.health as _health
        ctx = HealthContext(
            user_id="alice",
            workspace_root=db_path.parent.parent,
            data_dir=db_path.parent,
            db_path=db_path,
            uploads_dir=db_path.parent / "uploads",
        )
        ctx.ensure_dirs()
        original = _health.resolve_for_user
        try:
            _health.resolve_for_user = lambda uid, cfg: ctx
            task = core_db.Task(
                id=99, status="completed", source_type="cli",
                user_id="alice", prompt="",
            )
            count = _process_deferred_health_ops(config, task, deferred)
        finally:
            _health.resolve_for_user = original

        assert count == 3  # 1 insert + 2 valid bulk inserts (third skipped)
        with health_db.connect(db_path) as conn:
            rows = health_db.list_immunizations(conn)
        names = sorted(r.name for r in rows)
        assert names == ["Influenza", "MMR", "Tdap"]
