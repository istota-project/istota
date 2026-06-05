"""Native-brain usage persistence — token/cost telemetry to task_logs.

The native brain computes per-task token usage + cost; the executor persists it
to ``task_logs`` (no schema migration) so it's observable in production.
ClaudeCodeBrain leaves usage None, so this is a no-op for it.
"""

import json

from istota import db
from istota.executor import _persist_task_usage
from istota.session.usage import TaskUsage


def _setup(tmp_path):
    dbp = tmp_path / "istota.db"
    db.init_db(dbp)
    with db.get_db(dbp) as conn:
        tid = db.create_task(conn, prompt="x", user_id="alice", source_type="cli")
    return dbp, tid


class _Cfg:
    def __init__(self, dbp):
        self.db_path = dbp


def test_writes_usage_log_row(tmp_path):
    dbp, tid = _setup(tmp_path)
    usage = TaskUsage(input_tokens=1200, output_tokens=340, cache_read_tokens=50, cost_usd=0.0123)
    with db.get_db(dbp) as conn:
        _persist_task_usage(_Cfg(dbp), conn, tid, usage)
    with db.get_db(dbp) as conn:
        logs = db.get_task_logs(conn, tid)
    usage_lines = [l for l in logs if "usage " in l["message"]]
    assert len(usage_lines) == 1
    payload = json.loads(usage_lines[0]["message"].split("usage ", 1)[1])
    assert payload["input_tokens"] == 1200
    assert payload["output_tokens"] == 340
    assert payload["cache_read_tokens"] == 50
    assert payload["cost_usd"] == 0.0123


def test_none_usage_is_noop(tmp_path):
    dbp, tid = _setup(tmp_path)
    with db.get_db(dbp) as conn:
        _persist_task_usage(_Cfg(dbp), conn, tid, None)
        logs = db.get_task_logs(conn, tid)
    assert not [l for l in logs if "usage " in l["message"]]


def test_opens_own_conn_when_none(tmp_path):
    dbp, tid = _setup(tmp_path)
    usage = TaskUsage(input_tokens=10, output_tokens=2, cost_usd=0.0)
    _persist_task_usage(_Cfg(dbp), None, tid, usage)
    with db.get_db(dbp) as conn:
        logs = db.get_task_logs(conn, tid)
    assert [l for l in logs if "usage " in l["message"]]
