"""The `assignment` skill: who may call it, and each user-turn and read verb.

Driven through `main` with `run_skill_main`, against a real framework DB and
real task rows, so the caller classification is the one the proxy would see.
"""

from __future__ import annotations

import json
import pytest

from istota import db
from istota.assignments import budget, caller, lifecycle, store
from istota.config import UserConfig
from istota.skills._loader import build_disclosure_index, load_skill_index
from istota.usage.telemetry import BrainUsage
from tests.support.skill_cli import run_skill_main

FLAG = "skill_assignment"


@pytest.fixture
def config(make_config, tmp_path, monkeypatch):
    config = make_config(
        db_path=tmp_path / "istota.db",
        admin_users={"alice", "bob"},
        users={
            "alice": UserConfig(timezone="America/New_York"),
            "bob": UserConfig(),
            "carol": UserConfig(),
        },
    )
    db.init_db(config.db_path)
    monkeypatch.setattr("istota.config.load_config", lambda: config)
    monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
    monkeypatch.setenv("ISTOTA_EXPERIMENTAL_FEATURES", FLAG)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)
    monkeypatch.delenv("ISTOTA_WITHHELD_SCOPES", raising=False)
    return config


def _running(conn, **kwargs) -> int:
    task_id = db.create_task(conn, prompt="hi", **kwargs)
    conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (task_id,))
    return task_id


def _web_turn(config, user_id="alice") -> int:
    with db.get_db(config.db_path) as conn:
        room = db.register_room(conn, None, user_id, origin="web").token
        return _running(conn, user_id=user_id, source_type="web", conversation_token=room)


@pytest.fixture
def turn(config, monkeypatch) -> int:
    task_id = _web_turn(config)
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


def _create(*extra) -> dict:
    code, out = _run(*CREATE, *extra)
    assert code == 0, out
    return out


def _state(config, assignment_id, state, **fields):
    with db.get_db(config.db_path) as conn:
        store.set_state(conn, assignment_id, state, **fields)


def _get(config, assignment_id) -> store.Assignment:
    with db.get_db(config.db_path) as conn:
        return store.get_assignment(conn, assignment_id)


def _inbox(config, assignment_id) -> list[store.InboxEvent]:
    with db.get_db(config.db_path) as conn:
        return store.list_inbox(conn, assignment_id)


# ---------------------------------------------------------------------------
# Who is calling
# ---------------------------------------------------------------------------


class TestCallerFromTaskRows:
    def test_a_private_web_turn_is_the_users(self, config):
        task_id = _web_turn(config)
        with db.get_db(config.db_path) as conn:
            assert caller.classify(conn, task_id=task_id, user_id="alice") == caller.UserTurn(task_id)

    def test_a_private_talk_turn_through_its_binding(self, config):
        with db.get_db(config.db_path) as conn:
            room = db.register_bound_room(
                conn, "alice", origin="talk", name=None, surface="talk", surface_ref="talktok1",
            )
            assert room is not None
            task_id = _running(conn, user_id="alice", source_type="talk", conversation_token="talktok1")
            assert caller.user_turn(conn, task_id=task_id, user_id="alice")

    def test_a_local_cli_task_with_no_room(self, config):
        with db.get_db(config.db_path) as conn:
            task_id = _running(conn, user_id="alice", source_type="cli")
            assert caller.user_turn(conn, task_id=task_id, user_id="alice")

    def test_a_guest_turn_is_not(self, config):
        task_id = _web_turn(config)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE tasks SET guest_participant_id = 1 WHERE id = ?", (task_id,))
            assert not caller.user_turn(conn, task_id=task_id, user_id="alice")

    def test_a_shared_room_is_not(self, config):
        with db.get_db(config.db_path) as conn:
            room = db.register_room(conn, None, "alice", origin="web").token
            db.add_room_member(conn, room, "bob")
            task_id = _running(conn, user_id="alice", source_type="web", conversation_token=room)
            assert not caller.user_turn(conn, task_id=task_id, user_id="alice")

    def test_a_turn_someone_else_wrote_is_not(self, config):
        task_id = _web_turn(config)
        with db.get_db(config.db_path) as conn:
            room = db.get_task(conn, task_id).conversation_token
            conn.execute(
                "INSERT INTO messages (room_token, role, body, task_id, author_user_id, origin_surface)"
                " VALUES (?, 'user', 'x', ?, 'bob', 'web')",
                (room, task_id),
            )
            assert not caller.user_turn(conn, task_id=task_id, user_id="alice")

    @pytest.mark.parametrize("source_type", ["email", "sms", "whatsapp", "scheduled", "briefing", "subtask"])
    def test_other_sources_are_not(self, config, source_type):
        with db.get_db(config.db_path) as conn:
            task_id = _running(conn, user_id="alice", source_type=source_type)
            assert not caller.user_turn(conn, task_id=task_id, user_id="alice")

    def test_someone_elses_task_or_a_finished_one_is_not(self, config):
        task_id = _web_turn(config, user_id="bob")
        with db.get_db(config.db_path) as conn:
            assert isinstance(caller.classify(conn, task_id=task_id, user_id="alice"), caller.Other)
            conn.execute("UPDATE tasks SET status = 'completed' WHERE id = ?", (task_id,))
            assert isinstance(caller.classify(conn, task_id=task_id, user_id="bob"), caller.Other)
            assert isinstance(caller.classify(conn, task_id=None, user_id="bob"), caller.Other)
            assert isinstance(caller.classify(conn, task_id="x", user_id="bob"), caller.Other)

    def test_a_wake_names_its_assignment(self, config, turn):
        a = _create()["assignment"]["id"]
        b = _create()["assignment"]["id"]
        with db.get_db(config.db_path) as conn:
            for assignment_id in (a, b):
                task_id = _running(
                    conn, user_id="alice", source_type="assignment", queue="background",
                    assignment_id=assignment_id, max_attempts=1,
                )
                wake_id = store.insert_wake(conn, assignment_id, fired_by="time", task_id=task_id)
                found = caller.classify(conn, task_id=task_id, user_id="alice")
                assert found == caller.Wake(task_id=task_id, assignment_id=assignment_id, wake_id=wake_id)


# ---------------------------------------------------------------------------
# Refusals
# ---------------------------------------------------------------------------


class TestRefusals:
    def test_the_flag_off(self, config, turn, monkeypatch):
        monkeypatch.setenv("ISTOTA_EXPERIMENTAL_FEATURES", "")
        code, out = _run("list")
        assert code == 1
        assert out["error"] == "feature_disabled"

    def test_not_an_admin(self, config, monkeypatch):
        monkeypatch.setenv("ISTOTA_USER_ID", "carol")
        code, out = _run("list")
        assert (code, out["error"]) == (1, "not_admin")

    def test_withheld_scopes(self, config, turn, monkeypatch):
        monkeypatch.setenv("ISTOTA_WITHHELD_SCOPES", "files,memory")
        code, out = _run("list")
        assert (code, out["error"]) == (1, "not_a_user_turn")

    def test_a_user_turn_verb_from_no_task(self, config):
        code, out = _run(*CREATE)
        assert (code, out["error"]) == (1, "not_a_user_turn")
        assert "message" in out

    def test_a_user_turn_verb_from_an_email_task(self, config, monkeypatch):
        with db.get_db(config.db_path) as conn:
            task_id = _running(conn, user_id="alice", source_type="email")
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
        code, out = _run(*CREATE)
        assert (code, out["error"]) == (1, "not_a_user_turn")

    def test_a_wake_cannot_use_a_user_turn_verb(self, config, turn, monkeypatch):
        assignment_id = _create()["assignment"]["id"]
        with db.get_db(config.db_path) as conn:
            task_id = _running(conn, user_id="alice", source_type="assignment",
                               assignment_id=assignment_id, max_attempts=1)
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
        for argv in (("set", str(assignment_id), "--cadence", "1d"),
                     ("topup", str(assignment_id), "--usd", "5"),
                     ("note", str(assignment_id), "hi"),
                     ("close", str(assignment_id), "--completed")):
            code, out = _run(*argv)
            assert (code, out["error"]) == (1, "not_a_user_turn"), argv
        # The read verbs answer a wake.
        assert _run("show", str(assignment_id))[0] == 0

    def test_another_users_assignment_is_not_found(self, config, turn, monkeypatch):
        bobs_turn = _web_turn(config, user_id="bob")
        monkeypatch.setenv("ISTOTA_USER_ID", "bob")
        monkeypatch.setenv("ISTOTA_TASK_ID", str(bobs_turn))
        bobs = _create()["assignment"]
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_TASK_ID", str(turn))
        for ident in (str(bobs["id"]), bobs["slug"]):
            for argv in (("show", ident), ("pause", ident), ("note", ident, "x")):
                code, out = _run(*argv)
                assert (code, out["error"]) == (1, "not_found"), argv
        assert _run("list")[1]["assignments"] == []


# ---------------------------------------------------------------------------
# Drafting
# ---------------------------------------------------------------------------


class TestCreateAndSet:
    def test_create_writes_a_draft_and_its_directory(self, config, turn):
        out = _create("--deadline", "2099-12-01", "--cadence", "12h")
        a = out["assignment"]
        assert a["state"] == "draft"
        assert a["slug"] == "heat-pump-quotes"
        assert a["deadline"] == "2099-12-01T23:59:59.999Z"
        assert a["cadence_minutes"] == 720
        assert a["budget"] == {"max_cost_usd": 20.0, "max_wakes": 40, "per_wake_minutes": 20, "per_wake_turns": 60}
        assert "Default check-in every 12h." in out["charter"]
        assert out["directory_written"] is True
        folder = config.workspace_root("alice") / config.bot_dir_name / "assignments" / "heat-pump-quotes"
        assert (folder / "SCRATCHPAD.md").is_file()
        assert "# Heat pump quotes" in (folder / "CHARTER.md").read_text()
        with db.get_db(config.db_path) as conn:
            assert [e.kind for e in store.list_events(conn, a["id"])] == ["created"]
        # A second one with the same title takes the next slug.
        assert _create()["assignment"]["slug"] == "heat-pump-quotes-2"

    def test_a_deadline_time_with_no_offset_is_in_the_users_timezone(self, config, turn):
        out = _create("--deadline", "2099-06-01T09:00")
        assert out["assignment"]["deadline"] == "2099-06-01T13:00:00.000Z"

    @pytest.mark.parametrize("extra, code", [
        (("--budget-usd", "500"), "over_ceiling"),
        (("--max-wakes", "100000"), "over_ceiling"),
        (("--deadline", "2001-01-01"), "deadline_passed"),
        (("--cadence", "soon"), "invalid_duration"),
        (("--cadence", "5m"), "invalid_charter"),
        (("--effort", "extreme"), "invalid_charter"),
    ])
    def test_create_refusals(self, config, turn, extra, code):
        status, out = _run(*CREATE, *extra)
        assert (status, out["error"]) == (1, code)
        assert _run("list")[1]["assignments"] == []

    def test_set_on_a_draft_changes_the_charter_and_writes_no_inbox_event(self, config, turn):
        a = _create()["assignment"]
        code, out = _run("set", a["slug"], "--budget-usd", "30", "--goal", "Four quotes.", "--no-auto-complete")
        assert code == 0, out
        assert out["changed"] == ["auto_complete", "budget", "goal"]
        assert out["assignment"]["budget"]["max_cost_usd"] == 30.0
        assert out["assignment"]["budget"]["max_wakes"] == 40
        assert "Four quotes." in out["charter"]
        assert _inbox(config, a["id"]) == []

    def test_set_after_activation_tells_the_next_wake(self, config, turn):
        a = _create()["assignment"]
        _state(config, a["id"], "sleeping")
        code, out = _run("set", str(a["id"]), "--cadence", "1d")
        assert code == 0, out
        [event] = _inbox(config, a["id"])
        assert event.kind == "charter_change" and event.trusted
        assert "every 6h" in event.payload["before"] and "every 1d" in event.payload["after"]

    def test_set_with_nothing_and_on_a_closed_assignment(self, config, turn):
        a = _create()["assignment"]
        assert _run("set", str(a["id"]))[1]["error"] == "invalid_input"
        _state(config, a["id"], "abandoned")
        code, out = _run("set", str(a["id"]), "--title", "New")
        assert (code, out["error"], out["state"]) == (1, "bad_state", "abandoned")
        assert _get(config, a["id"]).title == "Heat pump quotes"


class TestItemsAndFrozen:
    def test_item_add_remove_and_the_checks(self, config, turn):
        a = _create()["assignment"]["id"]
        code, out = _run(
            "item", "add", str(a), "--text", "Three quotes", "--kind", "mechanical",
            "--check", "csv_columns", "--file", "quotes.csv", "--columns", "source, date", "--min-rows", "3",
        )
        assert code == 0, out
        assert "(check: csv_columns quotes.csv source,date; min 3 rows)" in out["charter"]
        mechanical = out["item_id"]
        code, out = _run("item", "add", str(a), "--text", "Picked one", "--kind", "confirmed")
        assert code == 0
        assert "2. [confirmed] Picked one" in out["charter"]
        assert _run("item", "remove", str(a), str(mechanical))[0] == 0
        with db.get_db(config.db_path) as conn:
            assert [i.text for i in store.list_items(conn, a)] == ["Picked one"]
        assert _run("item", "remove", str(a), str(mechanical))[1]["error"] == "not_found"

    @pytest.mark.parametrize("argv", [
        ("--kind", "mechanical"),
        ("--kind", "confirmed", "--check", "contains", "--file", "a.md", "--text-match", "x"),
        ("--kind", "mechanical", "--check", "file_parses", "--file", "../x.csv", "--format", "csv"),
        ("--kind", "mechanical", "--check", "csv_columns", "--file", "a.csv", "--columns", "a", "--format", "csv"),
        ("--kind", "confirmed", "--file", "a.csv"),
    ])
    def test_bad_validator_specs(self, config, turn, argv):
        a = _create()["assignment"]["id"]
        code, out = _run("item", "add", str(a), "--text", "x", *argv)
        assert (code, out["error"]) == (1, "invalid_validator")

    def test_item_confirm(self, config, turn):
        a = _create()["assignment"]["id"]
        mechanical = _run("item", "add", str(a), "--text", "m", "--kind", "mechanical",
                          "--check", "contains", "--file", "a.md", "--text-match", "done")[1]["item_id"]
        confirmed = _run("item", "add", str(a), "--text", "c", "--kind", "confirmed")[1]["item_id"]
        assert _run("item", "confirm", str(a), str(mechanical))[1]["error"] == "bad_state"
        _state(config, a, "sleeping")
        code, out = _run("item", "confirm", str(a), str(confirmed))
        assert code == 0
        assert out["item"] == {"id": confirmed, "status": "confirmed"}
        assert [(e.kind, e.trusted) for e in _inbox(config, a)] == [("item_confirmed", True)]

    def test_frozen_add_and_remove(self, config, turn):
        a = _create()["assignment"]["id"]
        code, out = _run("frozen", "add", str(a), "--name", "price_ceiling", "--value", "1200 USD")
        assert code == 0
        assert "- price_ceiling: 1200 USD" in out["charter"]
        dup = _run("frozen", "add", str(a), "--name", "price_ceiling", "--value", "1300 USD")[1]
        assert dup["error"] == "invalid_charter"
        assert _run("frozen", "remove", str(a), str(out["frozen_id"]))[0] == 0
        assert "None." in _run("show", str(a))[1]["charter"]


# ---------------------------------------------------------------------------
# State verbs
# ---------------------------------------------------------------------------


class TestStateVerbs:
    def test_pause_resume_abandon(self, config, turn):
        a = _create()["assignment"]["id"]
        assert _run("pause", str(a))[1]["state"] == "draft"  # bad_state carries the state
        _state(config, a, "sleeping")
        code, out = _run("pause", str(a))
        assert (code, out["assignment"]["state"], out["assignment"]["state_reason"]) == (0, "paused", "user")
        code, out = _run("resume", str(a))
        assert code == 0
        assert out["assignment"]["state"] == "sleeping"
        assert out["assignment"]["next_wake_at"] is not None
        code, out = _run("abandon", str(a), "--reason", "bought one")
        assert (out["assignment"]["state"], out["assignment"]["state_reason"]) == ("abandoned", "bought one")
        assert out["assignment"]["closed_at"] is not None
        assert _run("abandon", str(a))[1]["error"] == "bad_state"
        with db.get_db(config.db_path) as conn:
            kinds = [e.kind for e in store.list_events(conn, a)]
        assert kinds == ["created", "paused", "resumed", "abandoned"]

    def test_a_draft_can_be_abandoned(self, config, turn):
        a = _create()["assignment"]["id"]
        assert _run("abandon", str(a))[1]["assignment"]["state"] == "abandoned"

    def test_resume_past_the_active_limit(self, config, turn):
        config.assignments.max_active_per_user = 1
        a = _create()["assignment"]["id"]
        b = _create()["assignment"]["id"]
        _state(config, a, "sleeping")
        _state(config, b, "paused", reason="user")
        code, out = _run("resume", str(b))
        assert (code, out["error"]) == (1, "too_many_active")

    def test_resume_refused_while_the_budget_is_spent(self, config, turn):
        a = _create("--max-wakes", "1")["assignment"]["id"]
        _state(config, a, "paused", reason="budget")
        with db.get_db(config.db_path) as conn:
            store.insert_wake(conn, a, fired_by="time")
        assert _run("resume", str(a))[1]["error"] == "bad_state"

    def test_wake_requests_a_wake_without_creating_one(self, config, turn):
        a = _create()["assignment"]["id"]
        assert _run("wake", str(a))[1]["error"] == "bad_state"
        _state(config, a, "sleeping")
        code, out = _run("wake", str(a))
        assert code == 0
        assert out["assignment"]["next_wake_at"] is not None
        [event] = _inbox(config, a)
        assert (event.kind, event.trusted, event.payload["text"]) == ("user_note", True, "wake requested by the user")
        with db.get_db(config.db_path) as conn:
            assert store.count_wakes(conn, a) == 0

    def test_wake_on_a_proposed_completion_is_refused(self, config, turn):
        a = _create()["assignment"]["id"]
        _state(config, a, "waiting", waiting_on=["completion"])
        assert _run("wake", str(a))[1]["error"] == "bad_state"

    def test_topup_raises_the_cap_and_lifts_a_budget_pause(self, config, turn):
        a = _create("--max-wakes", "1")["assignment"]["id"]
        with db.get_db(config.db_path) as conn:
            store.insert_wake(conn, a, fired_by="time")
            store.update_assignment(conn, a, budget_low_sent=True)
            store.set_state(conn, a, "paused", reason="budget")
        assert _run("topup", str(a), "--usd", "100")[1]["error"] == "over_ceiling"
        code, out = _run("topup", str(a), "--wakes", "5")
        assert code == 0, out
        assert out["assignment"]["budget"]["max_wakes"] == 6
        assert out["assignment"]["state"] == "sleeping"
        assert "6 wakes" in out["charter"]
        assigned = _get(config, a)
        assert assigned.budget_low_sent is False and assigned.next_wake_at is not None
        assert [e.kind for e in _inbox(config, a)] == ["charter_change"]

    def test_note_and_handoff_done(self, config, turn):
        a = _create()["assignment"]["id"]
        _state(config, a, "waiting", waiting_on=["user"])
        with db.get_db(config.db_path) as conn:
            action = store.insert_action(conn, a, text="Photograph the label", report_spec="the photo")
        code, out = _run("note", str(a), "Acme called, visit Tuesday")
        assert code == 0
        code, out = _run("handoff", "done", str(a), str(action), "--report", "photo is in the inbox")
        assert code == 0
        assert out["handoff"] == {"id": action, "status": "done"}
        assert _run("handoff", "done", str(a), str(action), "--report", "again")[1]["error"] == "bad_state"
        assert _run("handoff", "done", str(a), "999", "--report", "x")[1]["error"] == "not_found"
        events = _inbox(config, a)
        assert [(e.kind, e.trusted) for e in events] == [("user_note", True), ("handoff_done", True)]
        assert events[1].payload == {"action_id": action, "step": "Photograph the label",
                                     "report": "photo is in the inbox"}
        assert _run("note", str(a), "  ")[1]["error"] == "invalid_input"

    def test_close_confirms_or_rejects_a_proposed_completion(self, config, turn):
        a = _create()["assignment"]["id"]
        _run("item", "add", str(a), "--text", "picked", "--kind", "confirmed")
        assert _run("close", str(a), "--completed")[1]["error"] == "bad_state"
        _state(config, a, "waiting", waiting_on=["completion"])
        code, out = _run("close", str(a), "--reject", "the third quote has no capacity")
        assert code == 0
        assert out["assignment"]["state"] == "sleeping"
        assert [e.kind for e in _inbox(config, a)] == ["completion_rejected"]
        _state(config, a, "waiting", waiting_on=["completion"])
        code, out = _run("close", str(a), "--completed")
        assert out["assignment"]["state"] == "completed"
        with db.get_db(config.db_path) as conn:
            assert [i.status for i in store.list_items(conn, a)] == ["confirmed"]


# ---------------------------------------------------------------------------
# Read verbs and the ledger
# ---------------------------------------------------------------------------


def _usage(conn, *, task_id, assignment_id, cost, has_totals=True):
    db.insert_task_usage(
        conn,
        usage=BrainUsage(billed_input_tokens=100, output_tokens=10, cost_usd=cost,
                         cost_basis="subscription" if has_totals else "unknown", has_totals=has_totals),
        user_id="alice", brain_kind="native", origin="task", success=True, model="m",
        source_type="assignment", task_id=task_id, assignment_id=assignment_id,
    )


class TestReadVerbs:
    def test_list_by_state(self, config, turn):
        a = _create()["assignment"]["id"]
        _create()
        _state(config, a, "sleeping")
        code, out = _run("list", "--state", "sleeping")
        assert code == 0
        assert [x["id"] for x in out["assignments"]] == [a]
        assert len(_run("list")[1]["assignments"]) == 2

    def test_show_reports_the_ledger_board_handoffs_and_wakes(self, config, turn):
        a = _create()["assignment"]["id"]
        with db.get_db(config.db_path) as conn:
            for n, (cost, measured) in enumerate(((1.25, True), (0.0, False), (0.5, True)), start=1):
                task_id = db.create_task(conn, prompt="[assignment wake]", user_id="alice",
                                         source_type="assignment", assignment_id=a, max_attempts=1)
                wake = store.insert_wake(conn, a, fired_by="time", task_id=task_id)
                _usage(conn, task_id=task_id, assignment_id=a, cost=cost, has_totals=measured)
                store.update_wake(conn, wake, ended_at=db.iso_utc_now(), outcome="next",
                                  summary=f"wake {n} summary")
            # A wake the brain never wrote a row for, and one that never reached a brain.
            store.update_wake(conn, store.insert_wake(conn, a, fired_by="time", task_id=9999),
                              ended_at=db.iso_utc_now(), outcome="failed")
            store.update_wake(conn, store.insert_wake(conn, a, fired_by="time", task_id=9998),
                              ended_at=db.iso_utc_now(), outcome="not_active")
            store.insert_action(conn, a, text="Call Acme", report_spec="the date")
        code, out = _run("show", "heat-pump-quotes", "--wakes", "2")
        assert code == 0, out
        ledger = out["ledger"]
        assert ledger["cost_usd"] == 1.75
        assert ledger["wakes"] == 5
        assert ledger["unmeasured_wakes"] == 2
        assert ledger["billed_input_tokens"] == 200
        assert ledger["remaining"] == {"cost_usd": 18.25, "wakes": 35}
        assert [w["seq"] for w in out["wakes"]] == [5, 4]
        assert out["open_handoffs"][0]["text"] == "Call Acme"
        assert out["charter"].startswith("# Heat pump quotes")
        assert "data" in out["notice"]

    def test_totals_count_only_this_assignment(self, config, turn):
        a = _create()["assignment"]["id"]
        b = _create()["assignment"]["id"]
        with db.get_db(config.db_path) as conn:
            _usage(conn, task_id=None, assignment_id=b, cost=3.0)
            assert budget.totals(conn, a).cost_usd == 0.0
            assert budget.totals(conn, b).cost_usd == 3.0


class TestLifecycleConcurrency:
    def test_a_stale_writer_sees_the_first_writers_state(self, config, turn):
        a = _create()["assignment"]["id"]
        _state(config, a, "sleeping")
        with db.get_db(config.db_path) as conn:
            lifecycle.pause(conn, a, user_id="alice")
            with pytest.raises(lifecycle.TransitionError) as exc:
                lifecycle.pause(conn, a, user_id="alice")
        assert exc.value.code == "bad_state" and exc.value.state == "paused"


# ---------------------------------------------------------------------------
# The skill itself
# ---------------------------------------------------------------------------


class TestTheSkill:
    def test_the_menu_line_carries_quest(self, config):
        index = load_skill_index(config.skills_dir, bundled_dir=config.bundled_skills_dir)
        meta = index["assignment"]
        assert meta.admin_only and meta.experimental and meta.cli
        assert meta.source_types == ["assignment"]
        menu = build_disclosure_index(["assignment"], index)
        line = next(line for line in menu.splitlines() if "- assignment:" in line)
        assert "quest" in line

    def test_selection_needs_the_flag(self, config):
        from istota.skills._loader import eligible_skill_names

        index = load_skill_index(config.skills_dir, bundled_dir=config.bundled_skills_dir)
        assert "assignment" not in eligible_skill_names(index, set(), is_admin=True)
        assert "assignment" in eligible_skill_names(
            index, set(), is_admin=True, enabled_experimental_features=frozenset({FLAG}),
        )
        assert "assignment" not in eligible_skill_names(
            index, set(), is_admin=False, enabled_experimental_features=frozenset({FLAG}),
        )

    def test_the_cli_answers_json_through_the_facade(self, config, turn):
        out = _run("list")
        assert out[0] == 0 and json.dumps(out[1])


def test_durations_parse_both_ways():
    from istota.assignments import charter

    assert [charter.parse_duration_minutes(v) for v in ("30m", "6h", "2D", " 1d ")] == [30, 360, 2880, 1440]
    for bad in ("", "6", "h", "-1h", "0m", "1w", "9999999d"):
        with pytest.raises(charter.CharterError) as exc:
            charter.parse_duration_minutes(bad)
        assert exc.value.code == "invalid_duration"


# ---------------------------------------------------------------------------
# The wake's verbs
# ---------------------------------------------------------------------------


def _open_wake(config, assignment_id, user_id="alice") -> tuple[int, int]:
    """A running wake task of the assignment and its open wake row."""
    _state(config, assignment_id, "active")
    with db.get_db(config.db_path) as conn:
        task_id = _running(conn, user_id=user_id, source_type="assignment", queue="assignment",
                           assignment_id=assignment_id, max_attempts=1)
        wake_id = store.insert_wake(conn, assignment_id, fired_by="time", task_id=task_id)
    return task_id, wake_id


@pytest.fixture
def wake(config, turn, monkeypatch):
    """Alice's assignment A, mid-wake, with the wake's environment set."""
    a = _create("--deadline", "2099-12-01")["assignment"]["id"]
    task_id, wake_id = _open_wake(config, a)
    monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
    monkeypatch.setenv(caller.ASSIGNMENT_ID_VAR, str(a))
    return {"assignment_id": a, "task_id": task_id, "wake_id": wake_id}


def _wake_row(config, wake_id) -> store.Wake:
    with db.get_db(config.db_path) as conn:
        return store.get_wake(conn, wake_id)


def _events(config, assignment_id, kind) -> list[store.Event]:
    with db.get_db(config.db_path) as conn:
        return [e for e in store.list_events(conn, assignment_id) if e.kind == kind]


def _minutes_from_now(iso: str) -> float:
    from datetime import datetime, timezone

    moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    return (moment - datetime.now(timezone.utc)).total_seconds() / 60


WAKE_VERBS = (
    ("next", "--in", "6h"),
    ("handoff", "photograph the furnace label", "--report", "the photo"),
    ("report", "first quote in"),
    ("attempt", "--target", "acme.example/quote", "--tried", "the web form", "--outcome", "failed"),
    ("complete", "--evidence", "quotes.csv has three rows"),
)


class TestWakeScoping:
    @pytest.mark.parametrize("argv", WAKE_VERBS, ids=[v[0] for v in WAKE_VERBS])
    def test_a_user_turn_is_not_a_wake(self, config, turn, argv):
        a = _create()["assignment"]["id"]
        code, out = _run(*argv, "--assignment", str(a))
        assert (code, out["error"]) == (1, "not_a_wake")

    def test_another_task_of_the_user_is_not_a_wake(self, config, monkeypatch):
        with db.get_db(config.db_path) as conn:
            task_id = _running(conn, user_id="alice", source_type="scheduled")
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
        assert _run("report", "x", "--assignment", "1")[1]["error"] == "not_a_wake"

    @pytest.mark.parametrize("argv", WAKE_VERBS, ids=[v[0] for v in WAKE_VERBS])
    def test_a_wake_of_a_cannot_write_to_b(self, config, turn, wake, monkeypatch, argv):
        monkeypatch.setenv("ISTOTA_TASK_ID", str(turn))
        b = _create()["assignment"]["id"]
        monkeypatch.setenv("ISTOTA_TASK_ID", str(wake["task_id"]))
        code, out = _run(*argv, "--assignment", str(b))
        assert (code, out["error"]) == (1, "wrong_assignment")
        # The environment variable is a default, not authority.
        monkeypatch.setenv(caller.ASSIGNMENT_ID_VAR, str(b))
        code, out = _run(*argv)
        assert (code, out["error"]) == (1, "wrong_assignment")
        with db.get_db(config.db_path) as conn:
            assert store.list_actions(conn, b) == []
            assert store.list_attempts(conn, b) == []
        assert _events(config, b, "report") == []

    def test_the_assignment_defaults_to_the_env_then_the_task_row(self, config, wake, monkeypatch):
        assert _run("report", "one")[0] == 0
        monkeypatch.delenv(caller.ASSIGNMENT_ID_VAR)
        assert _run("report", "two")[0] == 0
        slug = _get(config, wake["assignment_id"]).slug
        assert _run("report", "three", "--assignment", slug)[0] == 0
        texts = [e.payload["text"] for e in _events(config, wake["assignment_id"], "report")]
        assert texts == ["one", "two", "three"]

    def test_another_users_assignment_is_not_found(self, config, wake, monkeypatch):
        bobs_turn = _web_turn(config, user_id="bob")
        monkeypatch.setenv("ISTOTA_USER_ID", "bob")
        monkeypatch.setenv("ISTOTA_TASK_ID", str(bobs_turn))
        bobs = _create()["assignment"]["id"]
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_TASK_ID", str(wake["task_id"]))
        assert _run("report", "x", "--assignment", str(bobs))[1]["error"] == "not_found"

    def test_an_ended_wake_writes_nothing(self, config, wake):
        with db.get_db(config.db_path) as conn:
            store.update_wake(conn, wake["wake_id"], ended_at=db.iso_utc_now(), outcome="failed")
        for argv in WAKE_VERBS:
            code, out = _run(*argv)
            assert (code, out["error"]) == (1, "not_a_wake"), argv
        assert _wake_row(config, wake["wake_id"]).next_request is None

    def test_a_wake_task_with_no_wake_row_is_not_a_wake(self, config, turn, monkeypatch):
        a = _create()["assignment"]["id"]
        with db.get_db(config.db_path) as conn:
            task_id = _running(conn, user_id="alice", source_type="assignment",
                               assignment_id=a, max_attempts=1)
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))
        assert _run("report", "x")[1]["error"] == "not_a_wake"


class TestNext:
    def test_in_records_the_request_on_the_wake_row(self, config, wake):
        code, out = _run("next", "--in", "6h", "--progress", "two quotes in")
        assert code == 0, out
        row = _wake_row(config, wake["wake_id"])
        request = row.next_request
        assert (request["mode"], request["requested"], request["clamped"]) == ("in", "6h", None)
        assert 355 < _minutes_from_now(request["wake_at"]) <= 360
        assert row.progress_note == "two quotes in"
        assert out["next"] == request
        # A request, not a decision: the state and the wake's end are finish_wake's.
        assert _get(config, wake["assignment_id"]).state == "active"
        assert row.ended_at is None and row.outcome is None

    def test_in_below_the_floor_is_clamped(self, config, wake):
        _run("next", "--in", "1m")
        request = _wake_row(config, wake["wake_id"]).next_request
        floor = config.assignments.min_wake_interval_minutes
        assert request["clamped"] == "floor"
        assert floor - 1 < _minutes_from_now(request["wake_at"]) <= floor

    def test_at_past_the_deadline_is_clamped_to_it(self, config, wake):
        _run("next", "--at", "2100-06-01T09:00:00Z")
        request = _wake_row(config, wake["wake_id"]).next_request
        assert request["clamped"] == "deadline"
        assert request["wake_at"] == _get(config, wake["assignment_id"]).deadline

    def test_at_with_no_offset_is_the_users_timezone(self, config, wake):
        _run("next", "--at", "2099-01-15T09:00")
        request = _wake_row(config, wake["wake_id"]).next_request
        assert (request["mode"], request["wake_at"]) == ("at", "2099-01-15T14:00:00.000Z")

    def test_wait_user_and_the_last_call_wins(self, config, wake):
        _run("next", "--in", "2h", "--progress", "first")
        _run("next", "--wait", "user")
        row = _wake_row(config, wake["wake_id"])
        assert row.next_request["mode"] == "wait" and row.next_request["wait"] == "user"
        assert row.next_request["wake_at"] is None
        assert row.progress_note is None

    @pytest.mark.parametrize("argv,code", [
        (("next", "--in", "soon"), "invalid_duration"),
        (("next", "--at", "tomorrow"), "invalid_input"),
        (("next", "--at", "2099-01-15"), "invalid_input"),
    ])
    def test_bad_times(self, config, wake, argv, code):
        assert _run(*argv)[1]["error"] == code
        assert _wake_row(config, wake["wake_id"]).next_request is None

    def test_one_of_in_at_or_wait_is_required(self, config, wake):
        assert _run("next")[0] != 0
        assert _run("next", "--in", "1h", "--wait", "user")[0] != 0


class TestHandoffReportAttemptComplete:
    def test_handoff_writes_an_open_action_and_an_event(self, config, wake):
        code, out = _run("handoff", "photograph the furnace label", "--report", "the photo",
                         "--due", "2099-02-01")
        assert code == 0, out
        with db.get_db(config.db_path) as conn:
            (action,) = store.list_actions(conn, wake["assignment_id"])
        assert (action.kind, action.status, action.wake_id) == ("handoff", "open", wake["wake_id"])
        assert (action.text, action.report_spec) == ("photograph the furnace label", "the photo")
        assert action.due_at == "2099-02-01T23:59:59.999Z"
        assert out["handoff"]["id"] == action.id
        (event,) = _events(config, wake["assignment_id"], "handoff_created")
        assert (event.actor, event.wake_id, event.payload["action_id"]) == (
            "assignment", wake["wake_id"], action.id,
        )

    def test_the_user_reports_a_wakes_handoff_done(self, config, turn, wake, monkeypatch):
        action_id = _run("handoff", "call the insurer", "--report", "the reference")[1]["handoff"]["id"]
        monkeypatch.setenv("ISTOTA_TASK_ID", str(turn))
        code, out = _run("handoff", "done", str(wake["assignment_id"]), str(action_id),
                         "--report", "ref 88412")
        assert code == 0, out
        assert out["handoff"] == {"id": action_id, "status": "done"}
        # The user's spelling from a wake is still a user-turn verb.
        monkeypatch.setenv("ISTOTA_TASK_ID", str(wake["task_id"]))
        code, out = _run("handoff", "done", str(wake["assignment_id"]), str(action_id), "--report", "x")
        assert out["error"] == "not_a_user_turn"

    def test_a_quoted_step_starting_with_done_is_a_handoff(self, config, wake):
        code, out = _run("handoff", "done 4 7", "--report", "x")
        assert code == 0, out
        with db.get_db(config.db_path) as conn:
            assert [a.text for a in store.list_actions(conn, wake["assignment_id"])] == ["done 4 7"]

    @pytest.mark.parametrize("argv,code", [
        (("handoff", "  ", "--report", "x"), "invalid_input"),
        (("handoff", "step", "--report", " "), "invalid_input"),
        (("handoff", "step", "--report", "x", "--due", "2000-01-01"), "invalid_input"),
        (("handoff", "step", "--report", "x", "--due", "soon"), "invalid_input"),
        (("report", ""), "invalid_input"),
        (("report", "x" * 5000), "invalid_input"),
        (("attempt", "--target", "", "--tried", "x", "--outcome", "ok"), "invalid_input"),
        (("complete", "--evidence", " "), "invalid_input"),
    ])
    def test_bad_input(self, config, wake, argv, code):
        assert _run(*argv)[1]["error"] == code

    def test_handoff_and_report_refused_once_the_assignment_is_closed(self, config, wake):
        _state(config, wake["assignment_id"], "abandoned")
        assert _run("handoff", "x", "--report", "y")[1]["error"] == "bad_state"
        assert _run("report", "x")[1]["error"] == "bad_state"
        # The wake's own record still lands, for finish_wake to read.
        assert _run("next", "--in", "6h")[0] == 0
        assert _run("attempt", "--target", "t", "--tried", "x", "--outcome", "ok")[0] == 0

    def test_report_is_an_event_for_the_user(self, config, wake):
        code, out = _run("report", "first written quote received")
        assert code == 0, out
        (event,) = _events(config, wake["assignment_id"], "report")
        assert (event.actor, event.wake_id, event.payload) == (
            "assignment", wake["wake_id"], {"text": "first written quote received"},
        )
        assert out["event_id"] == event.id

    def test_attempt_rows_carry_the_wake(self, config, wake):
        for outcome in ("failed", "partial"):
            code, out = _run("attempt", "--target", "acme.example/quote", "--tried", "web form",
                             "--outcome", outcome)
            assert code == 0, out
        with db.get_db(config.db_path) as conn:
            rows = store.list_attempts(conn, wake["assignment_id"])
        assert [(r.outcome, r.wake_id, r.target) for r in rows] == [
            ("partial", wake["wake_id"], "acme.example/quote"),
            ("failed", wake["wake_id"], "acme.example/quote"),
        ]
        assert _run("attempt", "--target", "t", "--tried", "x", "--outcome", "maybe")[0] == 2

    def test_complete_records_the_evidence_and_the_last_call_wins(self, config, wake):
        _run("complete", "--evidence", "first")
        code, out = _run("complete", "--evidence", "quotes.csv has three rows")
        assert code == 0, out
        row = _wake_row(config, wake["wake_id"])
        assert row.complete_request == "quotes.csv has three rows"
        assert row.outcome is None
        assert _get(config, wake["assignment_id"]).state == "active"
        assert len(_events(config, wake["assignment_id"], "complete_requested")) == 2

    def test_a_failing_wakes_writes_are_already_in_the_db(self, config, wake):
        """Written directly, not deferred: nothing waits for the task to succeed."""
        _run("next", "--wait", "user")
        _run("handoff", "sign the form", "--report", "a photo of the signed page")
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE tasks SET status = 'failed' WHERE id = ?", (wake["task_id"],))
            assert store.get_wake(conn, wake["wake_id"]).next_request["mode"] == "wait"
            assert len(store.list_actions(conn, wake["assignment_id"], status="open")) == 1

