"""Tests for the health module's scheduled-jobs auto-seed."""

from __future__ import annotations

import pytest

from istota.health import db as health_db
from istota.health import garmin as gm
from istota.health._migrate import ensure_initialised
from istota.health.jobs import GARMIN_SYNC_JOB, MODULE_PREFIX, jobs_for_user
from istota.health.workspace import synthesize_health_context


def _ctx(tmp_path, user_id: str = "alice"):
    c = synthesize_health_context(user_id, tmp_path / "workspace")
    c.ensure_dirs()
    ensure_initialised(c)
    return c


class TestJobsForUser:
    def test_no_tokens_no_jobs(self, tmp_path):
        ctx = _ctx(tmp_path)
        assert jobs_for_user(ctx, "alice") == []

    def test_seeds_garmin_sync_when_tokens_present(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            gm.store_tokens(conn, {"oauth1_token": "abc"}, email="user@x.com")
            conn.commit()

        jobs = jobs_for_user(ctx, "alice")
        assert len(jobs) == 1
        job = jobs[0]
        assert job["name"] == f"{MODULE_PREFIX}garmin_sync"
        assert job["skill"] == "health"
        # skill_args is JSON-encoded list
        import json
        args = json.loads(job["skill_args"])
        assert args == ["garmin-sync", "--days-back", "2"]
        assert job["cron"] == GARMIN_SYNC_JOB.cron

    def test_no_context_no_jobs(self):
        assert jobs_for_user(None, "alice") == []

    def test_module_prefix_is_namespaced(self):
        """Defends against accidental collisions with user CRON.md rows
        — the orphan-deletion logic spares ``_module.*`` rows only when
        the prefix matches."""
        assert MODULE_PREFIX == "_module.health."
        assert GARMIN_SYNC_JOB.name.startswith(MODULE_PREFIX)
