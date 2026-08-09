"""Tests for the ``tasks`` skill CLI — the out-of-sandbox read surface for task
state (ISSUE-237).

Covers the two DB helpers (``get_task_state_for_user`` /
``list_recent_tasks_for_user``) and the ``istota-skill tasks status|recent``
commands built on them. The invariant under test everywhere is that both the
helpers and the CLI scope every read to one user, so the surface stays narrow
even if the framework DB ever becomes reachable more widely.
"""

import json

import pytest

from istota import db
from istota.skills.tasks import main as tasks_main


def _make_task(conn, **kw):
    """Create a task row and return its id, defaulting the required fields."""
    kw.setdefault("prompt", "do a thing")
    kw.setdefault("source_type", "talk")
    kw.setdefault("user_id", "alice")
    return db.create_task(conn, **kw)


# ============================================================================
# DB helpers
# ============================================================================


class TestGetTaskStateForUser:
    def test_returns_lifecycle_fields(self, db_conn):
        task_id = _make_task(db_conn, prompt="probe the calendar")
        db.update_task_status(db_conn, task_id, "completed", result="four calendars")

        state = db.get_task_state_for_user(db_conn, task_id, "alice")

        assert state is not None
        assert state["id"] == task_id
        assert state["status"] == "completed"
        assert state["result"] == "four calendars"
        assert state["user_id"] == "alice"
        assert state["source_type"] == "talk"
        # started_at / completed_at are not on the Task dataclass, so they have
        # to come from an explicit SELECT rather than _row_to_task.
        assert "started_at" in state
        assert state["completed_at"] is not None
        assert state["created_at"]

    def test_scoped_to_owner(self, db_conn):
        task_id = _make_task(db_conn, user_id="bob")

        assert db.get_task_state_for_user(db_conn, task_id, "alice") is None
        assert db.get_task_state_for_user(db_conn, task_id, "bob") is not None

    def test_missing_task(self, db_conn):
        assert db.get_task_state_for_user(db_conn, 999999, "alice") is None

    def test_carries_lineage_columns(self, db_conn):
        parent = _make_task(db_conn)
        child = _make_task(db_conn, source_type="subtask", parent_task_id=parent)

        state = db.get_task_state_for_user(db_conn, child, "alice")

        assert state["parent_task_id"] == parent
        assert state["scheduled_job_id"] is None

    def test_carries_conversation_token(self, db_conn):
        # The scope is the user, not the room, so a caller has to be able to
        # see that a result came from a different conversation.
        task_id = _make_task(db_conn, conversation_token="room42")

        state = db.get_task_state_for_user(db_conn, task_id, "alice")

        assert state["conversation_token"] == "room42"

    def test_prompt_excerpt_is_bounded(self, db_conn):
        task_id = _make_task(db_conn, prompt="p" * 5000)

        state = db.get_task_state_for_user(db_conn, task_id, "alice")

        # The excerpt identifies a task; it is not a way to page prompts back.
        assert state["prompt_excerpt"] == "p" * db._TASK_PROMPT_EXCERPT_CHARS

    def test_prompt_excerpt_counts_characters_not_bytes(self, db_conn):
        task_id = _make_task(db_conn, prompt="é" * 400)

        state = db.get_task_state_for_user(db_conn, task_id, "alice")

        assert state["prompt_excerpt"] == "é" * db._TASK_PROMPT_EXCERPT_CHARS

    def test_error_field_on_failure(self, db_conn):
        task_id = _make_task(db_conn)
        db.update_task_status(db_conn, task_id, "failed", error="boom")

        state = db.get_task_state_for_user(db_conn, task_id, "alice")

        assert state["status"] == "failed"
        assert state["error"] == "boom"


class TestListRecentTasksForUser:
    def test_scoped_to_owner(self, db_conn):
        _make_task(db_conn, user_id="alice")
        _make_task(db_conn, user_id="bob")

        rows = db.list_recent_tasks_for_user(db_conn, "alice")

        assert len(rows) == 1
        assert rows[0]["user_id"] == "alice"

    def test_newest_first(self, db_conn):
        first = _make_task(db_conn)
        second = _make_task(db_conn)

        rows = db.list_recent_tasks_for_user(db_conn, "alice")

        assert [r["id"] for r in rows] == [second, first]

    def test_filter_by_parent(self, db_conn):
        parent = _make_task(db_conn)
        child = _make_task(db_conn, source_type="subtask", parent_task_id=parent)
        _make_task(db_conn)

        rows = db.list_recent_tasks_for_user(db_conn, "alice", parent_task_id=parent)

        assert [r["id"] for r in rows] == [child]

    def test_filter_by_status(self, db_conn):
        done = _make_task(db_conn)
        db.update_task_status(db_conn, done, "completed", result="ok")
        _make_task(db_conn)

        rows = db.list_recent_tasks_for_user(db_conn, "alice", status="completed")

        assert [r["id"] for r in rows] == [done]

    def test_filter_by_source_type(self, db_conn):
        sched = _make_task(db_conn, source_type="scheduled")
        _make_task(db_conn, source_type="talk")

        rows = db.list_recent_tasks_for_user(db_conn, "alice", source_type="scheduled")

        assert [r["id"] for r in rows] == [sched]

    def test_filter_by_since(self, db_conn):
        old = _make_task(db_conn)
        db_conn.execute(
            "UPDATE tasks SET created_at = '2020-01-01 00:00:00' WHERE id = ?", (old,)
        )
        recent = _make_task(db_conn)

        rows = db.list_recent_tasks_for_user(db_conn, "alice", since="2021-01-01 00:00:00")

        assert [r["id"] for r in rows] == [recent]

    def test_limit(self, db_conn):
        for _ in range(5):
            _make_task(db_conn)

        rows = db.list_recent_tasks_for_user(db_conn, "alice", limit=2)

        assert len(rows) == 2

    def test_omits_result_body(self, db_conn):
        task_id = _make_task(db_conn)
        db.update_task_status(db_conn, task_id, "completed", result="x" * 5000)

        rows = db.list_recent_tasks_for_user(db_conn, "alice")

        # The list view is an index, not a bulk result dump — a caller reads one
        # result with `status <id>`. Ten 50 KB results would blow the context of
        # the agent that asked "which of my tasks finished".
        assert "result" not in rows[0]


# ============================================================================
# Skill CLI
# ============================================================================


@pytest.fixture
def cli_env(db_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    return db_path


class TestTasksStatusCommand:
    def test_status_of_own_task(self, cli_env, capsys):
        with db.get_db(cli_env) as conn:
            task_id = _make_task(conn)
            db.update_task_status(conn, task_id, "completed", result="the answer")

        tasks_main(["status", str(task_id)])

        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["task"]["id"] == task_id
        assert out["task"]["status"] == "completed"
        assert out["task"]["result"] == "the answer"

    def test_status_of_another_users_task_is_not_found(self, cli_env, capsys):
        with db.get_db(cli_env) as conn:
            task_id = _make_task(conn, user_id="bob")

        with pytest.raises(SystemExit) as exc:
            tasks_main(["status", str(task_id)])

        assert exc.value.code == 0
        out = json.loads(capsys.readouterr().out)
        # Deliberately indistinguishable from a nonexistent id: a caller must
        # not be able to probe which task ids exist on the instance.
        assert out["status"] == "not_found"

    def test_status_of_missing_task(self, cli_env, capsys):
        with pytest.raises(SystemExit) as exc:
            tasks_main(["status", "999999"])

        assert exc.value.code == 0
        assert json.loads(capsys.readouterr().out)["status"] == "not_found"

    def test_result_is_truncated_with_a_flag(self, cli_env, capsys):
        with db.get_db(cli_env) as conn:
            task_id = _make_task(conn)
            db.update_task_status(conn, task_id, "completed", result="y" * 100)

        tasks_main(["status", str(task_id), "--max-chars", "10"])

        out = json.loads(capsys.readouterr().out)
        assert out["task"]["result"] == "y" * 10
        assert out["task"]["result_truncated"] is True
        assert out["task"]["result_chars"] == 100

    def test_untruncated_result_is_not_flagged(self, cli_env, capsys):
        with db.get_db(cli_env) as conn:
            task_id = _make_task(conn)
            db.update_task_status(conn, task_id, "completed", result="short")

        tasks_main(["status", str(task_id)])

        out = json.loads(capsys.readouterr().out)
        assert out["task"]["result_truncated"] is False

    def test_result_cap_floors_at_one(self, cli_env, capsys):
        with db.get_db(cli_env) as conn:
            task_id = _make_task(conn)
            db.update_task_status(conn, task_id, "completed", result="abcdefgh")

        tasks_main(["status", str(task_id), "--max-chars", "-5"])

        out = json.loads(capsys.readouterr().out)
        # A negative cap would slice from the end (result[:-5]) and silently
        # drop the tail while still reporting the full result_chars.
        assert out["task"]["result"] == "a"
        assert out["task"]["result_chars"] == 8

    def test_not_found_says_it_is_permanent(self, cli_env, capsys):
        with pytest.raises(SystemExit):
            tasks_main(["status", "999999"])

        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "not_found"
        # A poll loop must be able to tell this from "not ready yet".
        assert "stop polling" in out["hint"]

    def test_carries_untrusted_notice(self, cli_env, capsys):
        with db.get_db(cli_env) as conn:
            task_id = _make_task(conn)
            db.update_task_status(conn, task_id, "completed", result="ok")

        tasks_main(["status", str(task_id)])

        out = json.loads(capsys.readouterr().out)
        # A result body routinely quotes email / web / feed text.
        assert "instructions" in out["notice"]

    def test_missing_db_path_errors_clearly(self, db_path, monkeypatch, capsys):
        monkeypatch.delenv("ISTOTA_DB_PATH", raising=False)
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        with pytest.raises(SystemExit) as exc:
            tasks_main(["status", "1"])

        assert exc.value.code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        # Names the condition (path unavailable) and the likely cause, rather
        # than asserting a cause that isn't always the true one.
        assert "database path is not available" in out["error"]
        assert "admin" in out["error"].lower()

    def test_missing_user_id_errors(self, db_path, monkeypatch, capsys):
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.delenv("ISTOTA_USER_ID", raising=False)

        with pytest.raises(SystemExit) as exc:
            tasks_main(["status", "1"])

        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out)["status"] == "error"


class TestTasksRecentCommand:
    def test_lists_own_tasks_newest_first(self, cli_env, capsys):
        with db.get_db(cli_env) as conn:
            first = _make_task(conn)
            second = _make_task(conn)
            _make_task(conn, user_id="bob")

        tasks_main(["recent"])

        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["count"] == 2
        assert [t["id"] for t in out["tasks"]] == [second, first]

    def test_parent_filter(self, cli_env, capsys):
        with db.get_db(cli_env) as conn:
            parent = _make_task(conn)
            child = _make_task(conn, source_type="subtask", parent_task_id=parent)
            _make_task(conn)

        tasks_main(["recent", "--parent", str(parent)])

        out = json.loads(capsys.readouterr().out)
        assert [t["id"] for t in out["tasks"]] == [child]

    def test_status_filter(self, cli_env, capsys):
        with db.get_db(cli_env) as conn:
            done = _make_task(conn)
            db.update_task_status(conn, done, "completed", result="ok")
            _make_task(conn)

        tasks_main(["recent", "--status", "completed"])

        out = json.loads(capsys.readouterr().out)
        assert [t["id"] for t in out["tasks"]] == [done]

    def test_since_accepts_a_relative_window(self, cli_env, capsys):
        with db.get_db(cli_env) as conn:
            old = _make_task(conn)
            conn.execute(
                "UPDATE tasks SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
                (old,),
            )
            recent = _make_task(conn)

        tasks_main(["recent", "--since", "30m"])

        out = json.loads(capsys.readouterr().out)
        assert [t["id"] for t in out["tasks"]] == [recent]

    def test_since_accepts_an_absolute_timestamp(self, cli_env, capsys):
        with db.get_db(cli_env) as conn:
            old = _make_task(conn)
            conn.execute(
                "UPDATE tasks SET created_at = '2020-01-01 00:00:00' WHERE id = ?",
                (old,),
            )
            recent = _make_task(conn)

        tasks_main(["recent", "--since", "2021-01-01T00:00:00"])

        out = json.loads(capsys.readouterr().out)
        assert [t["id"] for t in out["tasks"]] == [recent]
        assert out["filters"]["since"] == "2021-01-01 00:00:00"

    def test_since_rejects_garbage(self, cli_env, capsys):
        with pytest.raises(SystemExit) as exc:
            tasks_main(["recent", "--since", "yesterday-ish"])

        assert exc.value.code == 1
        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "error"
        assert "since" in out["error"].lower()

    @pytest.mark.parametrize("bad", ["2026-13-45", "2026-02-30", "2026-08-08 12:30:99"])
    def test_since_rejects_impossible_dates(self, cli_env, capsys, bad):
        # Shape-only validation let these through, after which they compared
        # as plain strings against created_at and matched nothing at all —
        # reported as "no tasks yet", which is the silent no-op the whole
        # command exists to remove.
        with pytest.raises(SystemExit) as exc:
            tasks_main(["recent", "--since", bad])

        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_since_rejects_an_enormous_window(self, cli_env, capsys):
        # timedelta(days=999999999999) raises OverflowError, not ValueError, so
        # this escaped the handler as a traceback with no JSON envelope.
        with pytest.raises(SystemExit) as exc:
            tasks_main(["recent", "--since", "999999999999d"])

        assert exc.value.code == 1
        assert json.loads(capsys.readouterr().out)["status"] == "error"

    def test_unknown_status_is_rejected_not_silently_empty(self, cli_env, capsys):
        # "complete" (not "completed") would otherwise return an empty list
        # indistinguishable from "nothing has finished yet".
        with pytest.raises(SystemExit) as exc:
            tasks_main(["recent", "--status", "complete"])

        assert exc.value.code != 0

    def test_source_type_filter_is_echoed_back(self, cli_env, capsys):
        # --source-type stays free-form (the set grows), so the response has to
        # say what it filtered on for a zero-count answer to be readable.
        tasks_main(["recent", "--source-type", "typo"])

        out = json.loads(capsys.readouterr().out)
        assert out["count"] == 0
        assert out["filters"]["source_type"] == "typo"

    def test_limit_is_capped(self, cli_env, capsys):
        with db.get_db(cli_env) as conn:
            for _ in range(3):
                _make_task(conn)

        tasks_main(["recent", "--limit", "10000"])

        out = json.loads(capsys.readouterr().out)
        # A runaway --limit would page the whole task table back through the
        # proxy's single-line JSON response.
        assert out["count"] == 3
        assert out["limit"] == 50

    def test_empty_result_set(self, cli_env, capsys):
        tasks_main(["recent"])

        out = json.loads(capsys.readouterr().out)
        assert out["status"] == "ok"
        assert out["count"] == 0
        assert out["tasks"] == []


class TestTasksCliSurface:
    def test_unknown_subcommand_exits_nonzero(self, cli_env, capsys):
        with pytest.raises(SystemExit) as exc:
            tasks_main(["frobnicate"])

        assert exc.value.code != 0

    def test_no_subcommand_exits_nonzero(self, cli_env, capsys):
        with pytest.raises(SystemExit) as exc:
            tasks_main([])

        assert exc.value.code != 0
