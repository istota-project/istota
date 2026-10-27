"""Agent shared blocks: dispatch queues one research task, capture writes its result.

The task itself runs through the executor's research gate (Stage 3,
``tests/test_shared_block_agent_gate.py``); these tests drive the task rows by
hand and check what dispatch and capture make of them.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from istota import db
from istota.briefings import shared_block_agent as agent
from istota.briefings import shared_block_runs as runs
from istota.browser.owner import BRIEFINGS_IDENTITY
from istota.config import Config, UserConfig, shared_block_from_row

NAME = "world-agent"
NOW = datetime(2026, 7, 21, 6, 5, tzinfo=timezone.utc)
NOTICE = "The turn ended without a final response."


@pytest.fixture(autouse=True)
def _no_alerts(monkeypatch):
    sent: list[str] = []
    monkeypatch.setattr(
        "istota.notifications.delivery.send_operator_alert",
        lambda config, message, **kw: sent.append(message),
    )
    monkeypatch.setattr(
        "istota.brain.primary_brain_unavailable", lambda brain_cfg: (True, None),
    )
    return sent


def _config(tmp_path) -> Config:
    cfg = Config(
        db_path=tmp_path / "istota.db",
        workspace_path=tmp_path / "mount",
        users={"alice": UserConfig(timezone="UTC")},
    )
    db.init_db(cfg.db_path)
    return cfg


def _define(cfg, name=NAME, *, render_mode="agent", directive="Read the front pages."):
    with db.get_db(cfg.db_path) as conn:
        db.upsert_shared_block_config(
            conn, name=name, cron="0 6 * * *", title="World",
            directive=directive, render_mode=render_mode,
            enabled=True, trusted=False, sources=[],
        )
        return shared_block_from_row(db.get_shared_block_config(conn, name))


def _entry(cfg, name=NAME) -> dict | None:
    with db.get_db(cfg.db_path) as conn:
        row = db.shared_kv_get(conn, runs.RUN_NAMESPACE, "agent:" + name)
    return json.loads(row["value"]) if row else None


def _block_value(cfg, name=NAME) -> dict | None:
    with db.get_db(cfg.db_path) as conn:
        row = db.shared_kv_get(conn, "briefing_shared_blocks", name)
    return json.loads(row["value"]) if row else None


def _seed_value(cfg, text="prior", name=NAME):
    with db.get_db(cfg.db_path) as conn:
        db.shared_kv_set(conn, "briefing_shared_blocks", name,
                         json.dumps({"text": text, "trusted": False}), "__system__")


def _finish(cfg, task_id, status, result=None, error=None):
    with db.get_db(cfg.db_path) as conn:
        db.update_task_status(conn, task_id, status, result=result, error=error)


def _dispatch_and_finish(cfg, block, status, result=None, error=None):
    assert agent.dispatch(cfg, block, now=NOW) == runs.QUEUED
    task_id = _entry(cfg)["task_id"]
    _finish(cfg, task_id, status, result=result, error=error)
    return task_id


class TestDispatch:
    def test_queues_one_research_task_and_records_it(self, tmp_path):
        cfg = _config(tmp_path)
        block = _define(cfg)

        assert agent.dispatch(cfg, block, now=NOW) == runs.QUEUED

        entry = _entry(cfg)
        assert entry["captured"] is False
        assert entry["briefing_name"] == f"shared_block:{NAME}:20260721T060500Z"
        with db.get_db(cfg.db_path) as conn:
            task = db.get_task(conn, entry["task_id"])
        assert task.user_id == BRIEFINGS_IDENTITY
        assert task.source_type == "shared_block_research"
        assert task.prompt == "Read the front pages."
        assert task.briefing_name == entry["briefing_name"]
        assert task.queue == "background"
        assert task.skip_log_channel
        assert task.conversation_token is None
        assert not task.attachments

    def test_a_second_dispatch_while_the_task_runs_is_in_flight(self, tmp_path):
        cfg = _config(tmp_path)
        block = _define(cfg)
        assert agent.dispatch(cfg, block, now=NOW) == runs.QUEUED
        first = _entry(cfg)

        assert agent.dispatch(cfg, block, now=NOW) == runs.IN_FLIGHT

        assert _entry(cfg) == first
        with db.get_db(cfg.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE source_type = 'shared_block_research'"
            ).fetchone()[0]
        assert count == 1

    def test_concurrent_dispatches_queue_one_task(self, tmp_path):
        cfg = _config(tmp_path)
        block = _define(cfg)
        outcomes: list[str] = []
        barrier = threading.Barrier(4)

        def _go():
            barrier.wait()
            outcomes.append(agent.dispatch(cfg, block, now=NOW))

        threads = [threading.Thread(target=_go) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sorted(outcomes) == sorted([runs.QUEUED] + [runs.IN_FLIGHT] * 3)

    def test_captures_a_finished_uncaptured_task_before_queueing(self, tmp_path):
        cfg = _config(tmp_path)
        block = _define(cfg)
        first = _dispatch_and_finish(cfg, block, "completed", result="fresh news")

        assert agent.dispatch(cfg, block, now=NOW) == runs.QUEUED

        assert _block_value(cfg) == {"text": "fresh news", "trusted": False}
        assert _entry(cfg)["task_id"] != first

    def test_brain_unavailable_queues_nothing(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        block = _define(cfg)
        monkeypatch.setattr(
            "istota.brain.primary_brain_unavailable", lambda brain_cfg: (False, "open"),
        )
        assert agent.dispatch(cfg, block, now=NOW) == runs.BRAIN_UNAVAILABLE
        assert _entry(cfg) is None
        with db.get_db(cfg.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 0

    def test_a_db_error_is_failed_and_queues_nothing(self, tmp_path, monkeypatch):
        cfg = _config(tmp_path)
        block = _define(cfg)

        def _boom(*a, **k):
            raise RuntimeError("disk")

        monkeypatch.setattr("istota.db.create_task", _boom)
        assert agent.dispatch(cfg, block, now=NOW) == runs.FAILED
        assert _entry(cfg) is None


class TestCapture:
    def test_completed_result_is_written_untrusted_as_system(self, tmp_path):
        cfg = _config(tmp_path)
        block = _define(cfg)
        _seed_value(cfg)
        _dispatch_and_finish(cfg, block, "completed", result="  the digest \n")

        assert agent.capture(cfg) == [NAME]

        assert _block_value(cfg) == {"text": "the digest", "trusted": False}
        with db.get_db(cfg.db_path) as conn:
            row = db.shared_kv_get(conn, "briefing_shared_blocks", NAME)
        assert row["written_by"] == "__system__"
        entry = _entry(cfg)
        assert entry["captured"] is True
        assert entry["outcome"] == runs.REGENERATED
        assert entry["answered_at"]

    def test_a_running_task_is_left_alone(self, tmp_path):
        cfg = _config(tmp_path)
        block = _define(cfg)
        agent.dispatch(cfg, block, now=NOW)
        _finish(cfg, _entry(cfg)["task_id"], "running")

        assert agent.capture(cfg) == []
        assert _entry(cfg)["captured"] is False

    @pytest.mark.parametrize("result", ["", "   ", NOTICE, NOTICE + "\n\nHalf a thought"])
    def test_an_empty_or_no_final_answer_result_keeps_prior(self, tmp_path, result):
        cfg = _config(tmp_path)
        block = _define(cfg)
        _seed_value(cfg)
        _dispatch_and_finish(cfg, block, "completed", result=result)

        agent.capture(cfg)

        assert _block_value(cfg)["text"] == "prior"
        assert _entry(cfg)["outcome"] == runs.KEPT_PRIOR

    @pytest.mark.parametrize("status", ["failed", "cancelled"])
    def test_a_failed_or_cancelled_task_keeps_prior(self, tmp_path, status):
        cfg = _config(tmp_path)
        block = _define(cfg)
        _seed_value(cfg)
        _dispatch_and_finish(cfg, block, status, result="partial", error="briefing_research_browser_off")

        agent.capture(cfg)

        assert _block_value(cfg)["text"] == "prior"
        assert _entry(cfg)["outcome"] == runs.FAILED

    def test_a_missing_task_keeps_prior(self, tmp_path):
        cfg = _config(tmp_path)
        block = _define(cfg)
        _seed_value(cfg)
        task_id = _dispatch_and_finish(cfg, block, "completed", result="news")
        with db.get_db(cfg.db_path) as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

        agent.capture(cfg)

        assert _block_value(cfg)["text"] == "prior"
        assert _entry(cfg)["outcome"] == runs.FAILED

    @pytest.mark.parametrize("column,value", [
        ("briefing_name", "shared_block:other:20260721T060500Z"),
        ("user_id", "alice"),
        ("source_type", "scheduled"),
    ])
    def test_a_task_that_does_not_match_the_entry_is_not_written(self, tmp_path, column, value):
        cfg = _config(tmp_path)
        block = _define(cfg)
        _seed_value(cfg)
        task_id = _dispatch_and_finish(cfg, block, "completed", result="forged")
        with db.get_db(cfg.db_path) as conn:
            conn.execute(f"UPDATE tasks SET {column} = ? WHERE id = ?", (value, task_id))

        agent.capture(cfg)

        assert _block_value(cfg)["text"] == "prior"
        assert _entry(cfg)["outcome"] == runs.FAILED

    def test_a_deleted_block_is_discarded(self, tmp_path):
        # `istota briefings shared remove` deletes the row without `forget`.
        cfg = _config(tmp_path)
        block = _define(cfg)
        _dispatch_and_finish(cfg, block, "completed", result="news")
        with db.get_db(cfg.db_path) as conn:
            db.delete_shared_block_config(conn, NAME)

        agent.capture(cfg)

        assert _block_value(cfg) is None
        assert _entry(cfg)["outcome"] == runs.DISCARDED

    def test_a_block_switched_away_from_agent_is_discarded(self, tmp_path):
        cfg = _config(tmp_path)
        block = _define(cfg)
        _seed_value(cfg)
        _dispatch_and_finish(cfg, block, "completed", result="news")
        _define(cfg, render_mode="synthesis", directive=None)

        agent.capture(cfg)

        assert _block_value(cfg)["text"] == "prior"
        assert _entry(cfg)["outcome"] == runs.DISCARDED

    def test_a_captured_entry_is_not_captured_again(self, tmp_path):
        cfg = _config(tmp_path)
        block = _define(cfg)
        _dispatch_and_finish(cfg, block, "completed", result="news")
        assert agent.capture(cfg) == [NAME]
        _seed_value(cfg, "edited since")
        assert agent.capture(cfg) == []
        assert _block_value(cfg)["text"] == "edited since"

    def test_one_bad_entry_does_not_stop_the_others(self, tmp_path):
        cfg = _config(tmp_path)
        good = _define(cfg, name="good")
        with db.get_db(cfg.db_path) as conn:
            db.shared_kv_set(conn, runs.RUN_NAMESPACE, "agent:bad", "{not json", "daemon")
        assert agent.dispatch(cfg, good, now=NOW) == runs.QUEUED
        task_id = _entry(cfg, "good")["task_id"]
        _finish(cfg, task_id, "completed", result="ok")

        assert agent.capture(cfg) == ["good"]
        assert _block_value(cfg, "good")["text"] == "ok"


class TestFailureAlert:
    def test_the_third_consecutive_failure_alerts_once(self, tmp_path, _no_alerts):
        cfg = _config(tmp_path)
        block = _define(cfg)
        _dispatch_and_finish(cfg, block, "failed", error="x")
        agent.capture(cfg)
        _dispatch_and_finish(cfg, block, "completed", result="")
        agent.capture(cfg)
        assert _no_alerts == []

        _dispatch_and_finish(cfg, block, "failed", error="x")
        agent.capture(cfg)
        assert len(_no_alerts) == 1
        assert NAME in _no_alerts[0]
        assert _entry(cfg)["consecutive_failures"] == 3

        _dispatch_and_finish(cfg, block, "failed", error="x")
        agent.capture(cfg)
        assert len(_no_alerts) == 1

    def test_a_success_resets_the_count(self, tmp_path, _no_alerts):
        cfg = _config(tmp_path)
        block = _define(cfg)
        for _ in range(2):
            _dispatch_and_finish(cfg, block, "failed", error="x")
            agent.capture(cfg)
        _dispatch_and_finish(cfg, block, "completed", result="news")
        agent.capture(cfg)
        assert _entry(cfg)["consecutive_failures"] == 0
        for _ in range(2):
            _dispatch_and_finish(cfg, block, "failed", error="x")
            agent.capture(cfg)
        assert _no_alerts == []


class TestRunState:
    def test_run_state_carries_the_agent_entry(self, tmp_path):
        cfg = _config(tmp_path)
        block = _define(cfg)
        agent.dispatch(cfg, block, now=NOW)
        with db.get_db(cfg.db_path) as conn:
            state = runs.run_state(conn, NAME)
        assert state["agent"]["captured"] is False
        assert state["agent"]["task_id"] == _entry(cfg)["task_id"]

    def test_forget_drops_the_agent_entry(self, tmp_path):
        cfg = _config(tmp_path)
        block = _define(cfg)
        agent.dispatch(cfg, block, now=NOW)
        with db.get_db(cfg.db_path) as conn:
            runs.forget(conn, NAME)
            assert runs.run_state(conn, NAME)["agent"] is None
