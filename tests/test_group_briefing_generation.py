"""One scheduler occurrence uses the ordinary workers and one edition."""

import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from istota import db, scheduler
from istota.briefings import group_store as store
from tests.test_group_briefing_store import NOW, define, enqueue, env as _store_env

env = _store_env


@pytest.fixture
def clock(monkeypatch):
    original = store._timestamp
    monkeypatch.setattr(store, "_timestamp", lambda value=None: original(value or NOW))
    monkeypatch.setattr(scheduler, "_now", lambda tz: NOW.astimezone(tz))


@pytest.fixture
def brain(monkeypatch):
    from istota.brain import BrainResult
    execute = Mock(return_value=BrainResult(success=True, result_text="Common edition"))
    monkeypatch.setattr("istota.brain.make_brain", lambda config: SimpleNamespace(
        execute=execute, resolve_model_name=lambda role: "test-model"))
    monkeypatch.setattr("istota.brain.primary_brain_unavailable", lambda config: (True, None))
    monkeypatch.setattr("istota.brain.report_brain_result", lambda *a, **kw: None)
    return execute


def test_scheduler_generates_once_for_current_members_through_worker(env, clock, brain, monkeypatch):
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, timezone="UTC", channel="in_app")
    generic = Mock(side_effect=AssertionError("final generation must be tool-less"))
    monkeypatch.setattr(scheduler, "execute_task", generic)
    queued = scheduler.check_group_briefings(env)
    assert len(queued) == 1
    assert scheduler.check_group_briefings(env) == []
    assert scheduler.process_one_task(env, user_id="__briefings__", queue="background") == (queued[0], True)
    with db.get_db(env.db_path) as conn:
        run = store.get_run(conn, conn.execute("SELECT id FROM group_briefing_runs").fetchone()[0])
        assert run["definition_id"] == definition["id"] and run["state"] == "published"
        assert run["body_md"] == "Common edition"
        assert run["subject"] == "Daily news — Thursday, 15 October"
        assert [r["user_id"] for r in store.list_recipients(conn, run["id"])] == ["alice", "bob"]
    assert scheduler.check_group_briefings(env) == []
    assert brain.call_count == 1
    assert brain.call_args.args[0].allowed_tools == []
    generic.assert_not_called()


@pytest.mark.parametrize("members", [2, 4])
def test_research_is_saved_before_private_gather_and_reused_on_synthesis_retry(env, clock, brain, monkeypatch, members):
    from istota.briefings.group_runs import advance_runs
    from istota.brain import BrainResult
    env.workspace_path = env.db_path.parent / "workspace"
    root = env.workspace_path / "Groups/team"
    root.mkdir(parents=True)
    (root / "GROUP.md").write_text("PRIVATE GROUP CANARY")
    with db.get_db(env.db_path) as conn:
        for name in ("carol", "admin")[:members - 2]:
            db.add_group_member(conn, "team", name, added_by="alice")
        conn.commit()
        definition = define(conn, env, timezone="UTC", generation={"version": 1,
            "prompt": "PRIVATE FINAL PROMPT", "inputs": [{"id": "notes", "kind": "group_memory", "config": {}}],
            "research": {"enabled": True, "prompt": "Read public article links."}})
        run = enqueue(conn, env, definition)
        research_ids = advance_runs(conn, env, now=NOW)
        assert len(research_ids) == 1
        research = db.get_task(conn, research_ids[0])
        assert research.prompt == "Read public article links."
        db.update_task_status(conn, research.id, "completed", result="[News](https://example.com/article)")
    with db.get_db(env.db_path) as conn:
        generation_ids = advance_runs(conn, env, now=NOW)
        assert len(generation_ids) == 1
        assert store.get_run(conn, run["id"])["research"]["task_id"] == research.id
    brain.side_effect = [BrainResult(success=False, result_text="PRIVATE PROVIDER CANARY", stop_reason="error"),
                         BrainResult(success=True, result_text="Common findings")]
    assert scheduler.process_one_task(env, user_id="__briefings__") == (generation_ids[0], False)
    with db.get_db(env.db_path) as conn:
        assert db.get_task(conn, generation_ids[0]).status == "pending"
        conn.execute("UPDATE tasks SET scheduled_for = NULL WHERE id = ?", (generation_ids[0],))
    # Fresh connections model daemon restart; the next task attempt reuses the
    # committed research rather than creating a fresh task for each member.
    with db.get_db(env.db_path) as conn:
        assert advance_runs(conn, env, now=NOW) == []
    assert scheduler.process_one_task(env, user_id="__briefings__") == (generation_ids[0], True)
    with db.get_db(env.db_path) as conn:
        saved = store.get_run(conn, run["id"])
        assert saved["state"] == "published"
        assert len(store.list_recipients(conn, run["id"])) == members * 2
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE source_type = 'group_briefing_research'").fetchone()[0] == 1
    assert brain.call_count == 2
    assert all("PRIVATE GROUP CANARY" in call.args[0].prompt and "https://example.com/article" in call.args[0].prompt
               and call.args[0].allowed_tools == [] for call in brain.call_args_list)


def test_preview_is_draft_only_and_publication_restart_does_not_regenerate(env, clock, brain):
    from istota.briefings.group_runs import advance_runs, generate_task
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, enabled=False)
        run = enqueue(conn, env, definition, mode="preview")
        task_id = advance_runs(conn, env, now=NOW)[0]
        task = db.get_task(conn, task_id)
    assert generate_task(task, env, now=NOW) == (True, "preview_ready")
    assert generate_task(task, env, now=NOW) == (True, "preview_ready")
    with db.get_db(env.db_path) as conn:
        assert store.get_run(conn, run["id"])["body_md"] == "Common edition"
        assert store.list_recipients(conn, run["id"]) == []
    assert brain.call_count == 1


@pytest.mark.parametrize("mixed", [False, True])
def test_blocks_gather_once_keep_structured_verbatim_and_combine_synthesis(env, clock, brain, mixed):
    from istota.briefings.group_runs import advance_runs
    env.workspace_path = env.db_path.parent / "workspace"
    root = env.workspace_path / "Groups/team"
    root.mkdir(parents=True)
    (root / "GROUP.md").write_text("Exact 17.25%")
    blocks = [{"title": "Numbers", "render_mode": "structured", "options": {"presentation": {"width": "wide"}}, "sources": [
        {"id": "numbers", "kind": "group_memory", "config": {}}]}]
    if mixed:
        blocks.extend({"title": title, "sources": [{"id": title.lower(), "kind": "group_memory", "config": {}}]}
                      for title in ("Analysis", "Outlook"))
        brain.return_value.result_text = json.dumps({"1": "Analysis text", "2": "Outlook text"})
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, generation_mode="blocks", generation={"version": 1, "blocks": blocks})
        run = enqueue(conn, env, definition)
        task_id = advance_runs(conn, env, now=NOW)[0]
    assert scheduler.process_one_task(env, user_id="__briefings__") == (task_id, True)
    with db.get_db(env.db_path) as conn:
        saved = store.get_run(conn, run["id"])
        assert saved["state"] == "published"
        assert saved["document"]["blocks"][0]["presentation"] == {"width": "wide"}
        assert saved["document"]["blocks"][0]["block_id"] == 0
        assert saved["body_md"] == ("Numbers\n\nExact 17.25%\n\nAnalysis\n\nAnalysis text\n\nOutlook\n\nOutlook text"
                                    if mixed else "Numbers\n\nExact 17.25%")
    assert brain.call_count == int(mixed)


@pytest.mark.parametrize("change", ["revision", "lease", "archive", "cancel"])
def test_live_changes_during_synthesis_cannot_publish(env, clock, brain, change):
    from istota.briefings.group_runs import advance_runs
    from istota.brain import BrainResult
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        run = enqueue(conn, env, definition)
        task_id = advance_runs(conn, env, now=NOW)[0]
    def changed(request):
        with db.get_db(env.db_path) as conn:
            if change == "revision":
                store.save_definition(conn, env, "team", {**definition, "title": "Edited"},
                    actor_id="alice", expected_revision=1, definition_id=definition["id"])
            elif change == "lease":
                store.claim_run(conn, run_id=run["id"], now=NOW + timedelta(minutes=6))
            elif change == "archive":
                db.archive_group(conn, "team")
            else:
                db.cancel_task(conn, task_id)
        return BrainResult(success=True, result_text="DO NOT PUBLISH")
    brain.side_effect = changed
    scheduler.process_one_task(env, user_id="__briefings__")
    with db.get_db(env.db_path) as conn:
        saved = store.get_run(conn, run["id"])
        assert saved["state"] != "published" and saved["body_md"] is None
        assert store.list_recipients(conn, run["id"]) == []


def test_owner_succession_does_not_change_schedule_or_generation(env, clock, brain):
    from istota.groups.service import change_membership
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, timezone="UTC")
        group = db.get_group(conn, "team")
        change_membership(conn, env, "team", "bob", actor_id="alice", role="owner",
                          expected_revision=group["revision"])
        group = db.get_group(conn, "team")
        change_membership(conn, env, "team", "alice", actor_id="bob", role=None,
                          expected_revision=group["revision"])
    del env.users["alice"]
    queued = scheduler.check_group_briefings(env)
    assert len(queued) == 1
    assert scheduler.process_one_task(env, user_id="__briefings__") == (queued[0], True)
    with db.get_db(env.db_path) as conn:
        current = store.get_definition(conn, definition["id"])
        assert current["revision"] == definition["revision"] and current["cron"] == definition["cron"]
        assert current["created_by"] == "alice"
        assert conn.execute("SELECT COUNT(*) FROM group_briefing_runs WHERE state = 'published'").fetchone()[0] == 1


@pytest.mark.parametrize("mode", ["prompt", "blocks"])
def test_refused_input_with_public_data_blocks_every_generation_mode(env, clock, brain, mode, caplog):
    from istota.briefings.group_runs import advance_runs
    env.workspace_path = env.db_path.parent / "workspace"
    root = env.workspace_path / "Groups/team"
    root.mkdir(parents=True)
    (root / "secret.md").write_text("PRIVATE CANARY")
    (root / "selected.md").symlink_to(root / "secret.md")
    sources = [{"id": "public", "kind": "shared_block", "config": {"name": "news"}},
               {"id": "selected", "kind": "group_file", "config": {"path": "selected.md"}}]
    generation = {"version": 1, "prompt": "Summarize", "inputs": sources}
    if mode == "blocks":
        generation = {"version": 1, "blocks": [{"title": "Verbatim", "render_mode": "structured", "sources": sources}]}
    with db.get_db(env.db_path) as conn:
        db.shared_kv_set(conn, "briefing_shared_blocks", "news", json.dumps({"text": "Public news"}), "__system__")
        conn.commit()
        definition = define(conn, env, generation_mode=mode, generation=generation)
        run = enqueue(conn, env, definition)
        advance_runs(conn, env, now=NOW)
    scheduler.process_one_task(env, user_id="__briefings__")
    with db.get_db(env.db_path) as conn:
        saved = store.get_run(conn, run["id"])
        assert saved["state"] == "failed" and saved["error_code"] == "source_refused"
        assert saved["body_md"] is None and store.list_recipients(conn, run["id"]) == []
    brain.assert_not_called()
    assert "PRIVATE CANARY" not in caplog.text
    assert (root / "secret.md").read_text() == "PRIVATE CANARY"


def test_contribution_revocation_during_synthesis_discards_edition(env, clock, brain, caplog):
    from istota.briefings import source_grants
    from istota.briefings.group_runs import advance_runs
    from istota.brain import BrainResult
    env.workspace_path = env.db_path.parent / "workspace"
    root = env.workspace_root("bob")
    root.mkdir(parents=True)
    (root / "selected.md").write_text("APPROVED CONTRIBUTION CANARY")
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, enabled=False, generation={"version": 1, "prompt": "Summarize", "inputs": [
            {"id": "file", "kind": "personal_file", "config": {"owner_id": "bob", "path": "selected.md"}}]})
        approval = source_grants.source_approval_state(conn, env, definition["id"], "file", actor_id="bob")
        source_grants.approve_source(conn, env, definition["id"], "file", actor_id="bob", acknowledged=True,
            expected_definition_revision=1, expected_grant_revision=0, audience_contract=approval["audience_contract"])
        run = enqueue(conn, env, definition, mode="preview")
        advance_runs(conn, env, now=NOW)
    def revoke(request):
        assert "APPROVED CONTRIBUTION CANARY" in request.prompt and request.allowed_tools == []
        with db.get_db(env.db_path) as conn:
            source_grants.revoke_source(conn, env, definition["id"], "file", actor_id="bob", expected_grant_revision=1)
        return BrainResult(success=True, result_text="PRIVATE RESULT CANARY")
    brain.side_effect = revoke
    scheduler.process_one_task(env, user_id="__briefings__")
    with db.get_db(env.db_path) as conn:
        saved = store.get_run(conn, run["id"])
        assert saved["body_md"] is None and saved["state"] != "preview_ready"
        assert store.list_recipients(conn, run["id"]) == []
    assert "CANARY" not in caplog.text


@pytest.mark.parametrize("other_source", [False, True])
def test_research_outage_is_visible_but_all_empty_fails(env, clock, brain, other_source):
    from istota.briefings.group_runs import advance_runs
    with db.get_db(env.db_path) as conn:
        db.shared_kv_set(conn, "briefing_shared_blocks", "news", json.dumps({"text": "Public news"}), "__system__")
        conn.commit()
        definition = define(conn, env, generation={"version": 1, "prompt": "Summarize",
            "inputs": [{"id": "news", "kind": "shared_block", "config": {"name": "news"}}] if other_source else [],
            "research": {"enabled": True, "prompt": "Find public news"}})
        run = enqueue(conn, env, definition)
        task_id = advance_runs(conn, env, now=NOW)[0]
        db.update_task_status(conn, task_id, "failed", error="Provider outage")
    with db.get_db(env.db_path) as conn:
        advance_runs(conn, env, now=NOW)
    scheduler.process_one_task(env, user_id="__briefings__")
    with db.get_db(env.db_path) as conn:
        saved = store.get_run(conn, run["id"])
        assert saved["state"] == ("published" if other_source else "failed")
        if other_source:
            assert saved["body_md"].startswith("Incomplete sources:")
            assert saved["provenance"]["incomplete_sources"]
            assert saved["provenance"]["sources"][-1]["error_code"] == "research_unavailable"
        else:
            assert saved["error_code"] == "sources_empty" and saved["body_md"] is None
    assert brain.call_count == int(other_source)


def test_expired_generation_claim_reuses_saved_research_and_rejects_old_task(env, clock, brain):
    from istota.briefings.group_runs import advance_runs, generate_task
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, generation={"version": 1, "prompt": "Summarize",
            "research": {"enabled": True, "prompt": "Research"}})
        run = enqueue(conn, env, definition)
        research_id = advance_runs(conn, env, now=NOW)[0]
        db.update_task_status(conn, research_id, "completed", result="Saved research")
    with db.get_db(env.db_path) as conn:
        old_id = advance_runs(conn, env, now=NOW)[0]
        old_task = db.get_task(conn, old_id)
    later = NOW + timedelta(minutes=6)
    with db.get_db(env.db_path) as conn:
        new_id = advance_runs(conn, env, now=later)[0]
        new_task = db.get_task(conn, new_id)
    assert generate_task(old_task, env, now=later) == (True, "lease_lost")
    assert generate_task(new_task, env, now=later) == (True, "published")
    assert brain.call_count == 1 and "Saved research" in brain.call_args.args[0].prompt
    with db.get_db(env.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE source_type = 'group_briefing_research'").fetchone()[0] == 1
        assert store.get_run(conn, run["id"])["state"] == "published"


def test_competing_pollers_queue_one_task_for_one_occurrence(env, clock):
    from concurrent.futures import ThreadPoolExecutor
    with db.get_db(env.db_path) as conn:
        define(conn, env, timezone="UTC")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda _: scheduler.check_group_briefings(env), range(2)))
    with db.get_db(env.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM group_briefing_runs").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == 1


def test_scheduled_research_runs_through_existing_task_completion(env, clock, brain, monkeypatch):
    with db.get_db(env.db_path) as conn:
        define(conn, env, timezone="UTC", generation={"version": 1, "prompt": "FINAL CANARY",
            "research": {"enabled": True, "prompt": "Follow public article links"}})
    research = Mock(return_value=(True, "Findings [article](https://example.com/article)", None, None))
    monkeypatch.setattr(scheduler, "execute_task", research)
    first = scheduler.check_group_briefings(env)
    assert len(first) == 1
    assert scheduler.process_one_task(env, user_id="__briefings__") == (first[0], True)
    assert research.call_args.args[0].prompt == "Follow public article links"
    second = scheduler.check_group_briefings(env)
    assert len(second) == 1 and second != first
    assert scheduler.process_one_task(env, user_id="__briefings__") == (second[0], True)
    assert research.call_count == 1 and brain.call_count == 1
    assert "https://example.com/article" in brain.call_args.args[0].prompt
    deliveries = scheduler.check_group_briefings(env)
    assert len(deliveries) == 2
    with db.get_db(env.db_path) as conn:
        assert all(db.get_task(conn, task).source_type == "group_briefing_delivery" for task in deliveries)
    for task in deliveries:
        assert scheduler.process_one_task(env, user_id="__briefings__") == (task, True)
    assert scheduler.check_group_briefings(env) == []
    assert research.call_count == 1 and brain.call_count == 1


def test_group_generation_ignores_owner_personal_module_toggle(env, clock, brain):
    env.users["alice"].disabled_modules = ["briefings"]
    with db.get_db(env.db_path) as conn:
        define(conn, env, timezone="UTC")
    task_id = scheduler.check_group_briefings(env)[0]
    assert scheduler.process_one_task(env, user_id="__briefings__") == (task_id, True)
    with db.get_db(env.db_path) as conn:
        run = conn.execute("SELECT id FROM group_briefing_runs").fetchone()
        recipients = store.list_recipients(conn, run["id"])
        assert next(r for r in recipients if r["user_id"] == "alice" and r["channel"] == "email")["state"] == "skipped"
        assert next(r for r in recipients if r["user_id"] == "bob" and r["channel"] == "email")["state"] == "pending"


def test_generation_usage_is_attributed_to_existing_task(env, clock, brain):
    from istota.usage.telemetry import BrainUsage
    brain.return_value.usage = BrainUsage(has_totals=True, output_tokens=12, cost_usd=0.01)
    with db.get_db(env.db_path) as conn:
        define(conn, env, timezone="UTC")
    task_id = scheduler.check_group_briefings(env)[0]
    scheduler.process_one_task(env, user_id="__briefings__")
    with db.get_db(env.db_path) as conn:
        saved = store.get_run(conn, conn.execute("SELECT id FROM group_briefing_runs").fetchone()[0])
        assert saved["usage"]["output_tokens"] == 12
        usage = conn.execute("SELECT * FROM task_usage WHERE task_id = ?", (task_id,)).fetchone()
        assert usage["origin"] == "task" and usage["source_type"] == "group_briefing_generation"


def test_scheduler_skips_stale_and_nonexistent_wall_times(env, clock, monkeypatch):
    from datetime import datetime, timezone
    with db.get_db(env.db_path) as conn:
        define(conn, env, timezone="America/Los_Angeles", cron="30 2 * * *")
    # 03:00 PDT on spring-forward day: 02:30 never occurred.
    now = datetime(2026, 3, 8, 10, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler, "_now", lambda tz: now.astimezone(tz))
    assert scheduler.check_group_briefings(env) == []
    now = datetime(2026, 3, 9, 20, tzinfo=timezone.utc)
    assert scheduler.check_group_briefings(env) == []


def test_repeated_fall_back_hour_has_one_scheduled_occurrence(env, clock, monkeypatch):
    from datetime import datetime, timezone
    with db.get_db(env.db_path) as conn:
        define(conn, env, timezone="America/Los_Angeles", cron="30 1 * * *")
    now = datetime(2026, 11, 1, 8, 30, tzinfo=timezone.utc)
    monkeypatch.setattr(scheduler, "_now", lambda tz: now.astimezone(tz))
    first = scheduler.check_group_briefings(env)
    assert len(first) == 1
    now = datetime(2026, 11, 1, 9, 30, tzinfo=timezone.utc)
    scheduler.check_group_briefings(env)
    with db.get_db(env.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM group_briefing_runs").fetchone()[0] == 1


def test_scheduler_admits_no_run_for_a_held_definition(env, clock):
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, timezone="UTC", channel="in_app")
        store.set_hold(conn, env, definition["id"], admin_id="admin")
    assert scheduler.check_group_briefings(env) == []
    with db.get_db(env.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM group_briefing_runs").fetchone()[0] == 0
        store.clear_hold(conn, env, definition["id"])
    assert len(scheduler.check_group_briefings(env)) == 1
