"""Activating an assignment through the request ledger.

`activate` holds the rendered charter as an `assignment_activation` request;
the task parks on it through the scheduler's authorization branch, and only
an approval of that exact preview moves the draft to `sleeping`. A charter
change while the request is held cancels it, and an approval that finds the
charter or the state moved closes the request as failed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import confirmations, db
from istota.assignments import charter, lifecycle, store
from istota.config import UserConfig
from istota.relay.requests import RequestError, associate_confirmation, text_hash
from tests.support.skill_cli import run_skill_main

KIND = "assignment_activation"


@pytest.fixture
def config(make_config, tmp_path, monkeypatch):
    config = make_config(
        db_path=tmp_path / "istota.db",
        admin_users={"alice", "bob"},
        users={"alice": UserConfig(timezone="UTC"), "bob": UserConfig()},
    )
    db.init_db(config.db_path)
    monkeypatch.setattr("istota.config.load_config", lambda: config)
    monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
    monkeypatch.delenv("ISTOTA_EXPERIMENTAL_FEATURES", raising=False)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
    monkeypatch.delenv("ISTOTA_WITHHELD_SCOPES", raising=False)
    return config


def _web_turn(config, monkeypatch, user_id="alice") -> int:
    with db.get_db(config.db_path) as conn:
        room = db.register_room(conn, None, user_id, origin="web").token
        task_id = db.create_task(conn, prompt="hi", user_id=user_id, source_type="web",
                                 conversation_token=room)
        conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
    monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
    return task_id


def _run(*argv) -> tuple[int, dict]:
    from istota.skills.assignment import main

    result = run_skill_main(main, list(argv))
    return result.exit_code, result.envelope


CREATE = (
    "create", "--title", "Heat pump quotes", "--goal", "Three written quotes.",
    "--autonomy", "May email installers on the approved list.",
    "--budget-usd", "20", "--max-wakes", "40",
)


def _draft(config, monkeypatch, *extra) -> int:
    _web_turn(config, monkeypatch)
    code, out = _run(*CREATE, *extra)
    assert code == 0, out
    return out["assignment"]["id"]


def _park(config, task_id: int) -> str:
    """What the scheduler does with a held request after the task succeeds."""
    with db.get_db(config.db_path) as conn:
        row = conn.execute(
            "SELECT * FROM whatsapp_skill_requests WHERE origin_task_id = ? AND state = 'held'",
            (task_id,),
        ).fetchone()
        db.set_task_confirmation(conn, task_id, row["preview"])
        associate_confirmation(conn, actor_user_id="alice", task_id=task_id,
                               request_id=row["id"], preview_digest=row["preview_digest"])
        return row["id"]


def _held(config, monkeypatch, assignment_id: int) -> tuple[int, str]:
    task_id = _web_turn(config, monkeypatch)
    code, out = _run("activate", str(assignment_id))
    assert code == 0, out
    assert out["status"] == "held"
    return task_id, _park(config, task_id)


def _request(config, request_id: str):
    with db.get_db(config.db_path) as conn:
        return conn.execute("SELECT * FROM whatsapp_skill_requests WHERE id = ?", (request_id,)).fetchone()


def _task(config, task_id: int) -> db.Task:
    with db.get_db(config.db_path) as conn:
        return db.get_task(conn, task_id)


def _assignment(config, assignment_id: int) -> store.Assignment:
    with db.get_db(config.db_path) as conn:
        return store.get_assignment(conn, assignment_id)


def _approve(config, task_id: int) -> None:
    with db.get_db(config.db_path) as conn:
        confirmations.approve(conn, _task(config, task_id), config=config, by="web")


# ---------------------------------------------------------------------------
# Holding the charter
# ---------------------------------------------------------------------------


class TestHold:
    def test_activate_holds_the_rendered_charter(self, config, monkeypatch):
        assignment_id = _draft(config, monkeypatch)
        task_id = _web_turn(config, monkeypatch)
        code, out = _run("activate", str(assignment_id))
        assert code == 0, out
        assert out["status"] == "held" and out["needs_confirmation"] is True
        with db.get_db(config.db_path) as conn:
            rendered = charter.render_current(conn, assignment_id)
            digest = charter.charter_digest(conn, assignment_id)
            row = conn.execute(
                "SELECT * FROM whatsapp_skill_requests WHERE origin_task_id = ?", (task_id,),
            ).fetchone()
        assert row["kind"] == KIND and row["state"] == "held"
        assert row["requester_user_id"] == row["recipient_user_id"] == "alice"
        assert rendered in row["preview"] and out["preview"] == row["preview"]
        assert row["preview_digest"] == text_hash(row["preview"])
        assert json.loads(row["destination"]) == {"assignment_id": assignment_id, "charter_digest": digest}
        assert _assignment(config, assignment_id).state == "draft"

    def test_a_second_call_in_the_same_turn_returns_the_same_request(self, config, monkeypatch):
        assignment_id = _draft(config, monkeypatch)
        _web_turn(config, monkeypatch)
        _, first = _run("activate", str(assignment_id))
        _, second = _run("activate", str(assignment_id))
        assert first["request_id"] == second["request_id"]

    def test_activate_needs_the_users_own_turn(self, config, monkeypatch):
        assignment_id = _draft(config, monkeypatch)
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, prompt="x", user_id="alice", source_type="email")
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
            # The user's own mail: a correspondent's has no such skill at all.
            db.mark_email_processed(conn, "uid-own", "alice@example.com", user_id="alice",
                                    task_id=task_id, routing_method="sender_match")
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
        code, out = _run("activate", str(assignment_id))
        assert code == 1 and out["error"] == "not_a_user_turn"

    def test_only_a_draft_is_activated(self, config, monkeypatch):
        assignment_id = _draft(config, monkeypatch)
        _run("abandon", str(assignment_id))
        code, out = _run("activate", str(assignment_id))
        assert code == 1 and out["error"] == "bad_state" and out["state"] == "abandoned"

    def test_a_past_deadline_is_refused(self, config, monkeypatch):
        assignment_id = _draft(config, monkeypatch)
        with db.get_db(config.db_path) as conn:
            store.update_assignment(conn, assignment_id, deadline="2020-01-01T23:59:59.999Z")
        code, out = _run("activate", str(assignment_id))
        assert code == 1 and out["error"] == "deadline_passed"

    def test_the_active_ceiling_is_checked_at_hold(self, config, monkeypatch):
        config.assignments.max_active_per_user = 1
        running = _draft(config, monkeypatch)
        with db.get_db(config.db_path) as conn:
            store.set_state(conn, running, "sleeping")
        assignment_id = _draft(config, monkeypatch)
        code, out = _run("activate", str(assignment_id))
        assert code == 1 and out["error"] == "too_many_active"

    def test_one_held_activation_per_assignment(self, config, monkeypatch):
        assignment_id = _draft(config, monkeypatch)
        _held(config, monkeypatch, assignment_id)
        _web_turn(config, monkeypatch)
        code, out = _run("activate", str(assignment_id))
        assert code == 1 and out["error"] == "confirmation_pending"

    def test_another_users_assignment_is_not_found(self, config, monkeypatch):
        assignment_id = _draft(config, monkeypatch)
        monkeypatch.setenv("ISTOTA_USER_ID", "bob")
        _web_turn(config, monkeypatch, user_id="bob")
        code, out = _run("activate", str(assignment_id))
        assert code == 1 and out["error"] == "not_found"


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


class TestApprove:
    def test_approval_starts_the_assignment(self, config, monkeypatch):
        assignment_id = _draft(config, monkeypatch)
        task_id, request_id = _held(config, monkeypatch, assignment_id)
        _approve(config, task_id)
        assignment = _assignment(config, assignment_id)
        assert assignment.state == "sleeping"
        assert assignment.next_wake_at is not None and assignment.activated_at is not None
        row = _request(config, request_id)
        assert row["state"] == "approved" and row["approved_digest"] == row["preview_digest"]
        # The parked task re-runs, as every approved authorization does.
        assert _task(config, task_id).status == "pending"
        with db.get_db(config.db_path) as conn:
            kinds = [e.kind for e in store.list_events(conn, assignment_id)]
        assert "activated" in kinds

    def test_charter_md_is_current_after_approval(self, config, monkeypatch):
        """ISSUE-837: approval writes no file, and CHARTER.md said `draft` after it."""
        assignment_id = _draft(config, monkeypatch)
        task_id, _request_id = _held(config, monkeypatch, assignment_id)
        _approve(config, task_id)
        assignment = _assignment(config, assignment_id)
        on_disk = (
            config.workspace_root("alice") / config.bot_dir_name / assignment.dir_relpath / charter.CHARTER_FILE
        ).read_text()
        with db.get_db(config.db_path) as conn:
            current = charter.render_current(conn, assignment_id)

        assert on_disk.endswith(current)
        assert "draft" not in on_disk

    def test_a_charter_change_cancels_the_held_request(self, config, monkeypatch):
        assignment_id = _draft(config, monkeypatch)
        task_id, request_id = _held(config, monkeypatch, assignment_id)
        _web_turn(config, monkeypatch)
        code, out = _run("set", str(assignment_id), "--title", "Heat pump quotes, revised")
        assert code == 0, out
        assert _request(config, request_id)["state"] == "cancelled"
        assert _task(config, task_id).status == "cancelled"
        _approve(config, task_id)
        assert _assignment(config, assignment_id).state == "draft"

    def test_each_charter_verb_cancels_it(self, config, monkeypatch):
        assignment_id = _draft(config, monkeypatch)
        for argv in (
            ("item", "add", str(assignment_id), "--text", "I picked one", "--kind", "confirmed"),
            ("frozen", "add", str(assignment_id), "--name", "ceiling", "--value", "1200 USD"),
            ("topup", str(assignment_id), "--usd", "5"),
        ):
            _, request_id = _held(config, monkeypatch, assignment_id)
            _web_turn(config, monkeypatch)
            code, out = _run(*argv)
            assert code == 0, out
            assert _request(config, request_id)["state"] == "cancelled", argv

    def test_a_stale_digest_fails_the_approval(self, config, monkeypatch):
        assignment_id = _draft(config, monkeypatch)
        task_id, request_id = _held(config, monkeypatch, assignment_id)
        # A charter write that bypassed `edit_charter`: the digest catches it.
        with db.get_db(config.db_path) as conn:
            store.update_assignment(conn, assignment_id, goal="Something else entirely.")
        _approve(config, task_id)
        assert _assignment(config, assignment_id).state == "draft"
        row = _request(config, request_id)
        assert row["state"] == "failed" and row["error_code"] == "charter_changed"
        assert _task(config, task_id).status == "cancelled"

    def test_approval_after_abandon_fails(self, config, monkeypatch):
        assignment_id = _draft(config, monkeypatch)
        task_id, request_id = _held(config, monkeypatch, assignment_id)
        _web_turn(config, monkeypatch)
        _run("abandon", str(assignment_id))
        _approve(config, task_id)
        assert _assignment(config, assignment_id).state == "abandoned"
        row = _request(config, request_id)
        assert row["state"] == "failed" and row["error_code"] == "bad_state"
        assert _task(config, task_id).status == "cancelled"

    def test_the_ceiling_is_checked_again_at_approval(self, config, monkeypatch):
        config.assignments.max_active_per_user = 1
        assignment_id = _draft(config, monkeypatch)
        task_id, request_id = _held(config, monkeypatch, assignment_id)
        other = _draft(config, monkeypatch)
        with db.get_db(config.db_path) as conn:
            store.set_state(conn, other, "sleeping")
        _approve(config, task_id)
        assert _assignment(config, assignment_id).state == "draft"
        assert _request(config, request_id)["error_code"] == "too_many_active"

    def test_a_changed_preview_is_refused_by_the_ledger(self, config, monkeypatch):
        assignment_id = _draft(config, monkeypatch)
        task_id, _ = _held(config, monkeypatch, assignment_id)
        with db.get_db(config.db_path) as conn:
            with pytest.raises(RequestError):
                confirmations.approve(conn, _task(config, task_id), config=config, by="web",
                                      preview_digest=text_hash("something else"))
        assert _assignment(config, assignment_id).state == "draft"

    def test_activate_after_approval_reports_it(self, config, monkeypatch):
        """The confirmed re-run calls `activate` again and is told it is done."""
        assignment_id = _draft(config, monkeypatch)
        task_id, request_id = _held(config, monkeypatch, assignment_id)
        _approve(config, task_id)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
        code, out = _run("activate", str(assignment_id))
        assert code == 0, out
        assert out["status"] == "approved" and out["request_id"] == request_id
        assert out["assignment"]["state"] == "sleeping"

    def test_the_held_task_is_labelled_for_the_bell(self, config, monkeypatch):
        assignment_id = _draft(config, monkeypatch)
        task_id, _ = _held(config, monkeypatch, assignment_id)
        with db.get_db(config.db_path) as conn:
            task = _task(config, task_id)
            assert confirmations.describe(conn, task) == "an assignment awaiting approval"
            assert confirmations.describe_title(conn, task) == "Assignment waiting for approval"


# ---------------------------------------------------------------------------
# Through the scheduler
# ---------------------------------------------------------------------------


def test_the_scheduler_parks_on_the_charter_and_approval_starts_it(config, monkeypatch):
    from istota.scheduler import process_one_task

    monkeypatch.setattr("istota.notifications.delivery.send_notification", lambda *a, **k: True)
    assignment_id = _draft(config, monkeypatch)
    with db.get_db(config.db_path) as conn:
        room = db.register_room(conn, None, "alice", origin="web").token
        task_id = db.create_task(conn, prompt="go", user_id="alice", source_type="web",
                                 conversation_token=room)

    def execute(*args, **kwargs):
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
        code, out = _run("activate", str(assignment_id))
        assert code == 0, out
        return True, json.dumps(out), None, None

    with patch("istota.scheduler.execute_task", side_effect=execute):
        assert process_one_task(config) == (task_id, True)
    task = _task(config, task_id)
    assert task.status == "pending_confirmation"
    with db.get_db(config.db_path) as conn:
        assert charter.render_current(conn, assignment_id) in task.confirmation_prompt
    assert task.whatsapp_confirmation_request_id is not None
    _approve(config, task_id)
    assert _assignment(config, assignment_id).state == "sleeping"


def test_the_scheduler_cancels_a_task_whose_request_was_cancelled(config, monkeypatch):
    """A charter change landing between the hold and the park stops the park."""
    from istota.scheduler import process_one_task

    monkeypatch.setattr("istota.notifications.delivery.send_notification", lambda *a, **k: True)
    assignment_id = _draft(config, monkeypatch)
    with db.get_db(config.db_path) as conn:
        room = db.register_room(conn, None, "alice", origin="web").token
        task_id = db.create_task(conn, prompt="go", user_id="alice", source_type="web",
                                 conversation_token=room)

    def execute(*args, **kwargs):
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
        _, out = _run("activate", str(assignment_id))
        with db.get_db(config.db_path) as conn:
            lifecycle.cancel_held_activation(conn, assignment_id, reason="charter_changed")
        return True, json.dumps(out), None, None

    with patch("istota.scheduler.execute_task", side_effect=execute):
        assert process_one_task(config) == (task_id, False)
    assert _task(config, task_id).status == "cancelled"
    assert _assignment(config, assignment_id).state == "draft"


def test_a_turn_that_activates_then_edits_the_charter_completes_with_its_reply(config, monkeypatch):
    """Its own `set` cancels its own hold; the turn itself is not stopped."""
    from istota.scheduler import process_one_task

    monkeypatch.setattr("istota.notifications.delivery.send_notification", lambda *a, **k: True)
    assignment_id = _draft(config, monkeypatch)
    with db.get_db(config.db_path) as conn:
        room = db.register_room(conn, None, "alice", origin="web").token
        task_id = db.create_task(conn, prompt="go", user_id="alice", source_type="web",
                                 conversation_token=room)

    def execute(*args, **kwargs):
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
        code, out = _run("activate", str(assignment_id))
        assert code == 0 and out["status"] == "held", out
        code, out = _run("set", str(assignment_id), "--goal", "Four written quotes.")
        assert code == 0, out
        return True, "Changed the goal; activate it again when you are ready.", None, None

    with patch("istota.scheduler.execute_task", side_effect=execute):
        assert process_one_task(config) == (task_id, True)
    task = _task(config, task_id)
    assert task.status == "completed"
    assert task.result == "Changed the goal; activate it again when you are ready."
    with db.get_db(config.db_path) as conn:
        (request,) = conn.execute(
            "SELECT state FROM whatsapp_skill_requests WHERE origin_task_id = ?", (task_id,),
        ).fetchall()
    assert request["state"] == "cancelled"
    assert _assignment(config, assignment_id).state == "draft"


def test_another_turns_charter_edit_still_stops_the_held_turn(config, monkeypatch):
    from istota.scheduler import process_one_task

    monkeypatch.setattr("istota.notifications.delivery.send_notification", lambda *a, **k: True)
    assignment_id = _draft(config, monkeypatch)
    with db.get_db(config.db_path) as conn:
        room = db.register_room(conn, None, "alice", origin="web").token
        task_id = db.create_task(conn, prompt="go", user_id="alice", source_type="web",
                                 conversation_token=room)

    def execute(*args, **kwargs):
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
        _, out = _run("activate", str(assignment_id))
        _web_turn(config, monkeypatch)
        code, edited = _run("set", str(assignment_id), "--goal", "Four written quotes.")
        assert code == 0, edited
        return True, json.dumps(out), None, None

    with patch("istota.scheduler.execute_task", side_effect=execute):
        assert process_one_task(config) == (task_id, False)
    assert _task(config, task_id).status == "cancelled"


# ---------------------------------------------------------------------------
# The CHECK rebuild
# ---------------------------------------------------------------------------


def test_an_upgraded_request_table_takes_the_new_kind(tmp_path):
    from tests.test_whatsapp_requests import _request_shape

    fresh = tmp_path / "fresh.db"
    db.init_db(fresh)
    old = tmp_path / "pre-assignments.db"
    schema = (Path(__file__).parents[1] / "schema.sql").read_text()
    assert ", 'assignment_activation'" in schema
    with sqlite3.connect(old) as conn:
        conn.executescript(schema.replace(", 'assignment_activation'", ""))
        conn.execute("INSERT INTO tasks (id,user_id,source_type,prompt) VALUES (1,'alice','web','x')")
        conn.execute(
            "INSERT INTO whatsapp_skill_requests (id,requester_user_id,origin_task_id,request_key,kind,"
            "recipient_user_id,content_hash,service_hash,provider,binding_fingerprint,state) "
            "VALUES ('r','alice',1,'k','location_share','alice','h','s','room','fp','held')")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("UPDATE whatsapp_skill_requests SET kind=? WHERE id='r'", (KIND,))
    db.init_db(old)
    db.init_db(old)
    with db.get_db(fresh) as a, db.get_db(old) as b:
        assert _request_shape(a) == _request_shape(b)
        b.execute("UPDATE whatsapp_skill_requests SET kind=? WHERE id='r'", (KIND,))
        assert b.execute("SELECT state FROM whatsapp_skill_requests WHERE id='r'").fetchone()[0] == "held"
        assert b.execute("PRAGMA foreign_key_check").fetchall() == []
        b.execute("DELETE FROM tasks WHERE id=1")
        assert b.execute("SELECT origin_task_id FROM whatsapp_skill_requests").fetchone()[0] is None
