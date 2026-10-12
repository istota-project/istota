"""Shared-block Run now is answered by the daemon, not the web process (ISSUE-700).

The web route leaves a request in `shared_kv`; the daemon's
`shared-block-request` gate takes it and runs the generation where the browser
network and the daemon's brain breaker are.
"""

from __future__ import annotations

import json
import threading

from istota import db, scheduler
from istota.briefings import shared_block_runs
from istota.config import Config, UserConfig


def _config(tmp_path) -> Config:
    cfg = Config(
        db_path=tmp_path / "istota.db",
        workspace_path=tmp_path / "mount",
        users={"alice": UserConfig(timezone="UTC")},
    )
    db.init_db(cfg.db_path)
    return cfg


def _define(cfg, name="mk", **fields):
    with db.get_db(cfg.db_path) as conn:
        db.upsert_shared_block_config(
            conn, name=name, cron="0 6 * * *", title="Markets",
            directive=None, render_mode=fields.get("render_mode", "structured"),
            enabled=True, trusted=True,
            sources=fields.get("sources", [{"kind": "markets", "config": {}}]),
        )


class TestRequest:
    def test_a_second_press_joins_the_pending_request(self, tmp_path):
        cfg = _config(tmp_path)
        first = shared_block_runs.request_run(cfg, "mk")
        second = shared_block_runs.request_run(cfg, "mk")
        assert first["id"] == second["id"]
        assert shared_block_runs.requests_pending(cfg)

    def test_a_request_stays_pending_until_it_is_answered(self, tmp_path):
        # A daemon restarted mid-run must find the request again.
        cfg = _config(tmp_path)
        asked = shared_block_runs.request_run(cfg, "a")
        assert shared_block_runs.next_request(cfg)["id"] == asked["id"]
        assert shared_block_runs.next_request(cfg)["id"] == asked["id"]
        shared_block_runs.record_answered(cfg, asked, shared_block_runs.REGENERATED)
        assert shared_block_runs.next_request(cfg) is None
        assert not shared_block_runs.requests_pending(cfg)

    def test_no_database_is_nothing_pending(self, tmp_path):
        cfg = Config(db_path=tmp_path / "absent.db")
        assert shared_block_runs.requests_pending(cfg) is False
        assert shared_block_runs.next_request(cfg) is None
        assert not (tmp_path / "absent.db").exists()

    def test_the_namespace_is_out_of_a_task_s_reach(self):
        from istota.sandbox.kv_namespaces import is_reserved_namespace
        assert is_reserved_namespace(shared_block_runs.RUN_NAMESPACE)


class TestDaemonAnswers:
    def test_the_daemon_generates_the_db_definition_and_records_the_answer(
        self, tmp_path, monkeypatch,
    ):
        cfg = _config(tmp_path)
        _define(cfg)
        seen = {}

        def _fake_run(block, config, now=None):
            seen["block"] = block
            return {"text": "fresh", "trusted": True}

        monkeypatch.setattr("istota.briefings.shared_blocks.run_shared_block", _fake_run)
        asked = shared_block_runs.request_run(cfg, "mk")

        scheduler.check_shared_block_requests(cfg)

        assert seen["block"].name == "mk"
        assert seen["block"].render_mode == "structured"
        with db.get_db(cfg.db_path) as conn:
            stored = db.shared_kv_get(conn, "briefing_shared_blocks", "mk")
            state = shared_block_runs.run_state(conn, "mk")
        assert json.loads(stored["value"])["text"] == "fresh"
        assert state["pending"] is None
        assert state["answered"]["id"] == asked["id"]
        assert state["answered"]["outcome"] == shared_block_runs.REGENERATED

    def test_an_empty_gather_answers_kept_prior(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        _define(cfg)
        monkeypatch.setattr(
            "istota.briefings.shared_blocks.run_shared_block",
            lambda block, config, now=None: None,
        )
        shared_block_runs.request_run(cfg, "mk")
        scheduler.check_shared_block_requests(cfg)
        with db.get_db(cfg.db_path) as conn:
            state = shared_block_runs.run_state(conn, "mk")
        assert state["answered"]["outcome"] == shared_block_runs.KEPT_PRIOR

    def test_the_daemon_s_breaker_decides_a_synthesis_block(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        _define(cfg, render_mode="synthesis")
        monkeypatch.setattr(
            "istota.brain.primary_brain_unavailable",
            lambda brain_cfg: (False, "breaker open"),
        )

        def _boom(*a, **k):
            raise AssertionError("generated while the primary brain is unavailable")

        monkeypatch.setattr("istota.briefings.shared_blocks.run_shared_block", _boom)
        shared_block_runs.request_run(cfg, "mk")
        scheduler.check_shared_block_requests(cfg)
        with db.get_db(cfg.db_path) as conn:
            state = shared_block_runs.run_state(conn, "mk")
        assert state["answered"]["outcome"] == shared_block_runs.BRAIN_UNAVAILABLE

    def test_one_pass_answers_every_pending_block(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        _define(cfg, "a")
        _define(cfg, "b")
        monkeypatch.setattr(
            "istota.briefings.shared_blocks.run_shared_block",
            lambda block, config, now=None: {"text": block.name},
        )
        asked = {n: shared_block_runs.request_run(cfg, n)["id"] for n in ("a", "b")}
        scheduler.check_shared_block_requests(cfg)
        with db.get_db(cfg.db_path) as conn:
            for name in ("a", "b"):
                state = shared_block_runs.run_state(conn, name)
                assert state["pending"] is None
                assert state["answered"]["id"] == asked[name]

    def test_an_unwritable_answer_does_not_spin(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        _define(cfg)
        calls = []

        def _fake_run(block, config, now=None):
            calls.append(block.name)
            return {"text": "x"}

        monkeypatch.setattr("istota.briefings.shared_blocks.run_shared_block", _fake_run)
        monkeypatch.setattr(shared_block_runs, "record_answered", lambda *a, **k: None)
        shared_block_runs.request_run(cfg, "mk")
        scheduler.check_shared_block_requests(cfg)
        assert calls == ["mk"]
        assert shared_block_runs.requests_pending(cfg)

    def test_a_block_deleted_after_the_press_is_answered_not_found(self, tmp_path):
        cfg = _config(tmp_path)
        shared_block_runs.request_run(cfg, "gone")
        scheduler.check_shared_block_requests(cfg)
        with db.get_db(cfg.db_path) as conn:
            state = shared_block_runs.run_state(conn, "gone")
        assert state["answered"]["outcome"] == shared_block_runs.NOT_FOUND


class TestPoll:
    def test_nothing_pending_spawns_nothing(self, tmp_path):
        cfg = _config(tmp_path)
        inflight: dict[str, threading.Thread] = {}
        scheduler.poll_shared_block_requests(cfg, inflight)
        assert inflight == {}

    def test_a_pending_request_is_answered_on_a_thread(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        _define(cfg)
        monkeypatch.setattr(
            "istota.briefings.shared_blocks.run_shared_block",
            lambda block, config, now=None: {"text": "x"},
        )
        shared_block_runs.request_run(cfg, "mk")
        inflight: dict[str, threading.Thread] = {}
        scheduler.poll_shared_block_requests(cfg, inflight)
        thread = inflight["shared-block-request"]
        thread.join(timeout=10)
        with db.get_db(cfg.db_path) as conn:
            state = shared_block_runs.run_state(conn, "mk")
        assert state["answered"]["outcome"] == shared_block_runs.REGENERATED
