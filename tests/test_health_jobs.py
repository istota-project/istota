"""Tests for the health module's scheduled-jobs auto-seed."""

from __future__ import annotations

import json

import pytest

from istota import db as framework_db
from istota.health import garmin as gm
from istota.health._migrate import ensure_initialised
from istota.health.jobs import GARMIN_SYNC_JOB, MODULE_PREFIX, jobs_for_user
from istota.health.workspace import synthesize_health_context


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key-test-key-test-key-test-key-test-key")


@pytest.fixture
def fdb(tmp_path):
    path = tmp_path / "istota.db"
    framework_db.init_db(path)
    return path


def _ctx(tmp_path, fdb, user_id: str = "alice"):
    c = synthesize_health_context(user_id, tmp_path / "workspace")
    c.ensure_dirs()
    ensure_initialised(c)
    # Health context needs to know the framework DB path so jobs.py can
    # probe the encrypted secrets table.
    from dataclasses import replace
    return replace(c, framework_db_path=fdb)


class TestJobsForUser:
    def test_no_tokens_no_jobs(self, tmp_path, fdb):
        ctx = _ctx(tmp_path, fdb)
        assert jobs_for_user(ctx, "alice") == []

    def test_seeds_garmin_sync_when_tokens_present(self, tmp_path, fdb):
        ctx = _ctx(tmp_path, fdb)
        gm.store_tokens(fdb, "alice", {"oauth1_token": "abc"}, email="user@x.com")

        jobs = jobs_for_user(ctx, "alice")
        assert len(jobs) == 1
        job = jobs[0]
        assert job["name"] == f"{MODULE_PREFIX}garmin_sync"
        assert job["skill"] == "health"
        args = json.loads(job["skill_args"])
        assert args == ["garmin-sync", "--days-back", "2"]
        assert job["cron"] == GARMIN_SYNC_JOB.cron

    def test_no_context_no_jobs(self):
        assert jobs_for_user(None, "alice") == []

    def test_no_framework_db_no_jobs(self, tmp_path, fdb):
        """A HealthContext without a framework_db_path can't probe the
        secrets table; we must not emit jobs we know will fail."""
        ctx = _ctx(tmp_path, fdb)
        from dataclasses import replace
        ctx_no_fdb = replace(ctx, framework_db_path=None)
        assert jobs_for_user(ctx_no_fdb, "alice") == []

    def test_module_prefix_is_namespaced(self):
        assert MODULE_PREFIX == "_module.health."
        assert GARMIN_SYNC_JOB.name.startswith(MODULE_PREFIX)
