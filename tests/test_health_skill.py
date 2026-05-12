"""Tests for ``istota-skill health`` CLI."""

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
        "ISTOTA_EXPERIMENTAL_FEATURES": "module_health",
        # Direct mode — no deferred dir.
        "ISTOTA_DEFERRED_DIR": "",
        "ISTOTA_TASK_ID": "",
    }
    return ctx.db_path, env


class TestExperimentalGate:
    def test_blocks_when_flag_off(self, ready):
        db_path, env = ready
        env = {**env, "ISTOTA_EXPERIMENTAL_FEATURES": ""}
        proc = subprocess.run(
            [sys.executable, "-m", "istota.skills.health", "latest"],
            capture_output=True, text=True, env=env,
        )
        assert proc.returncode == 1
        payload = json.loads(proc.stdout)
        assert payload["status"] == "error"
        assert "module_health" in payload["error"]


class TestStatsCli:
    def test_log_and_latest(self, ready):
        db_path, env = ready
        _run(["log", "weight", "82.5"], env)
        _run(["log", "weight", "182", "--unit", "lb"], env)
        out = _run(["latest"], env)
        # Latest in kg, lb input converted.
        assert "weight" in out["stats"]
        # 182 lb ≈ 82.554 kg
        assert abs(out["stats"]["weight"]["value"] - 82.554) < 0.1
        assert out["stats"]["weight"]["unit"] == "kg"

    def test_stats_list_filter(self, ready):
        db_path, env = ready
        _run(["log", "weight", "82.5"], env)
        _run(["log", "resting_hr", "60"], env)
        out = _run(["stats", "--metric", "weight"], env)
        assert all(s["metric"] == "weight" for s in out["stats"])


class TestPanelsCli:
    def test_add_panel_and_biomarker(self, ready):
        db_path, env = ready
        out = _run(
            ["add-panel", "--drawn-at", "2026-05-08", "--lab", "Quest", "--type", "CBC"],
            env,
        )
        pid = out["id"]
        _run([
            "add-biomarker", str(pid),
            "Hemoglobin", "14.8", "g/dL",
            "--ref-low", "13.5", "--ref-high", "17.5",
        ], env)
        out = _run(["panel", str(pid)], env)
        assert out["panel"]["lab_name"] == "Quest"
        assert out["biomarkers"][0]["name"] == "Hemoglobin"


class TestSettingsCli:
    def test_set_height_parses_imperial(self, ready):
        db_path, env = ready
        _run(["set", "height", "5ft10in"], env)
        out = _run(["settings"], env)
        # 70 in × 2.54 = 177.8 cm
        assert abs(out["settings"]["height_cm"] - 177.8) < 0.1

    def test_display_units_merge(self, ready):
        db_path, env = ready
        _run(["set", "display.weight", "lb"], env)
        _run(["set", "display.temp", "F"], env)
        out = _run(["settings"], env)
        du = out["settings"]["display_units"]
        assert du == {"weight": "lb", "temp": "F"}


class TestDeferredMode:
    def test_writes_to_deferred_file(self, ready, tmp_path):
        db_path, env = ready
        deferred = tmp_path / "deferred"
        deferred.mkdir()
        env = {
            **env,
            "ISTOTA_DEFERRED_DIR": str(deferred),
            "ISTOTA_TASK_ID": "42",
        }
        out = _run(["log", "weight", "82.5"], env)
        assert out["deferred"] is True
        ops_file = deferred / "task_42_health_ops.json"
        assert ops_file.exists()
        ops = json.loads(ops_file.read_text())
        assert ops[0]["op"] == "insert_stat"
        assert ops[0]["metric"] == "weight"
        assert ops[0]["value"] == 82.5
