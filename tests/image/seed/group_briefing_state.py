"""Group migration and delivery witnesses across separate image processes."""

import json
import smtplib
import sys
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from istota import db, user_briefings, user_profiles
from istota.briefings import group_delivery, group_migrate, group_runs, group_store as store
from istota.config import load_config
from istota.cron_loader import CronJob, sync_cron_jobs_to_db
from istota.groups.policy import GroupError

ACTOR = "upgradeuser"
JOB = CronJob(name="publisher", cron="0 6 * * *", prompt="Public news", publish_shared_kv="news")
STATE = Path("/data/db/group-witness.json")


def generate(config, definition_id, mode):
    with db.get_db(config.db_path) as conn:
        run = store.enqueue_run(conn, config, definition_id, actor_id=ACTOR,
                                is_admin=True, request_id=str(uuid4()), mode=mode)
        tasks = group_runs.advance_runs(conn, config)
    assert len(tasks) == 1
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, tasks[0])
    with patch("istota.brain.make_brain", side_effect=AssertionError("structured generation called a model")):
        assert group_runs.generate_task(task, config) == (True, "preview_ready" if mode == "preview" else "published")
    with db.get_db(config.db_path) as conn:
        db.update_task_status(conn, task.id, "completed")
        return store.get_run(conn, run["id"])


def dispatch(config, *, partial=False):
    sent = []
    def smtp(message, config, **kwargs):
        sent.append({"to": message["To"], "id": message["Message-ID"],
                     "body": message.get_body(preferencelist=("plain",)).get_content().strip()})
        if partial and message["To"] == "carol@example.com":
            raise smtplib.SMTPRecipientsRefused({"carol@example.com": (450, b"Try later")})
        if partial and message["To"] == "dave@example.com":
            raise TimeoutError("Acceptance unknown")
    with db.get_db(config.db_path) as conn:
        tasks = group_delivery.queue_deliveries(conn, config)
    with patch("istota.skills.email._send_smtp", smtp), patch(
        "istota.brain.make_brain", side_effect=AssertionError("delivery called a model")
    ):
        for task_id in tasks:
            with db.get_db(config.db_path) as conn:
                task = db.get_task(conn, task_id)
            assert group_delivery.deliver_task(task, config)[0]
            with db.get_db(config.db_path) as conn:
                db.update_task_status(conn, task_id, "completed")
    return sent


def prepare(config):
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE prompt LIKE 'a task the previous release completed%' ").fetchone()[0] == 3
        imported = group_migrate.import_default(conn, config, "daily", actor_id=ACTOR, timezone="UTC")
        definition = store.get_definition(conn, imported["definition_id"])
        assert not definition["enabled"] and definition["excluded_user_ids"] == []
        assert store.delivery_preferences(conn, definition["group_id"], "bob")["outbound_muted"]
        assert user_briefings.get_briefing(config.db_path, ACTOR, "daily", conn=conn).cron == "0 9 * * *"
        assert user_briefings.get_briefing(config.db_path, "carol", "daily", conn=conn).cron == "0 7 * * *"
        value = {field: definition[field] for field in group_migrate.PAYLOAD_FIELDS}
        edited = store.save_definition(conn, config, definition["group_id"], value | {"title": "Retained UI edit"},
            actor_id=ACTOR, is_admin=True, expected_revision=1, definition_id=definition["id"])
        group_migrate.import_default(conn, config, "daily", actor_id=ACTOR, timezone="UTC")
        assert store.get_definition(conn, definition["id"])["title"] == edited["title"]
        assert group_migrate.migration_report(conn, config, "daily")[0]["candidates"]
        document = {"group_id": definition["group_id"], "name": "common", "title": "Common news",
                    "cron": "0 7 * * *", "timezone": "UTC", "channel": "email", "dependent_jobs": [],
                    "generation_mode": "blocks", "generation": {"version": 1, "blocks": [{"title": "News",
                    "render_mode": "structured", "sources": [{"id": "news", "kind": "shared_block",
                    "config": {"name": "retained"}}]}]}}
        publisher = group_migrate.import_publisher(conn, config, ACTOR, JOB.name, document, actor_id=ACTOR)
        task_id = db.create_task(conn, "Queued publisher", ACTOR, source_type="scheduled",
                                scheduled_job_id=db.get_scheduled_job_by_name(conn, ACTOR, JOB.name).id)
    generate(config, publisher["definition_id"], "preview")
    with db.get_db(config.db_path) as conn:
        cutover = group_migrate.cutover_publisher(conn, config, ACTOR, JOB.name, actor_id=ACTOR,
                                                expected_revision=1, replacements={}, confirmed=True)
        assert db.get_task(conn, task_id).status == "cancelled"
        kv = [tuple(row) for row in conn.execute("SELECT * FROM shared_kv ORDER BY namespace,key")]
    run = generate(config, publisher["definition_id"], "publish")
    assert "Retained public story" in run["body_md"]
    sends = dispatch(config, partial=True)
    with db.get_db(config.db_path) as conn:
        states = {row["user_id"]: row["state"] for row in store.list_recipients(conn, run["id"]) if row["channel"] == "email"}
        assert states == {ACTOR: "sent", "bob": "skipped", "carol": "failed", "dave": "uncertain"}, states
        parts = [tuple(row) for row in conn.execute("SELECT recipient_id,part_index,send_identity FROM group_briefing_parts ORDER BY recipient_id,part_index")]
    state = {"definition": publisher["definition_id"], "default": definition["id"], "run": run,
             "sends": sends, "parts": parts, "kv": kv, "cutover": cutover}
    STATE.write_text(json.dumps(state))
    return {"partial": states, "default_exclusions": definition["excluded_user_ids"]}


def resume(config):
    state = json.loads(STATE.read_text())
    with db.get_db(config.db_path) as conn:
        sync_cron_jobs_to_db(conn, ACTOR, [JOB])
        conn.commit()
        assert not db.get_scheduled_job_by_name(conn, ACTOR, JOB.name).enabled
        try:
            db.create_task(conn, "Cached publisher", ACTOR, source_type="scheduled",
                           scheduled_job_id=db.get_scheduled_job_by_name(conn, ACTOR, JOB.name).id)
        except ValueError as error:
            assert "publisher_retired" in str(error)
            conn.rollback()
        else:
            raise AssertionError("restart admitted the retired publisher")
        assert store.get_definition(conn, state["default"])["title"] == "Retained UI edit"
        assert group_runs.advance_runs(conn, config) == []
        assert store.get_run(conn, state["run"]["id"]) == state["run"]
        assert store.retry_deliveries(conn, config, state["run"]["id"], actor_id=ACTOR, is_admin=True) == 1
    sends = dispatch(config)
    assert [item["to"] for item in sends] == ["carol@example.com"]
    assert sends[0] == next(item for item in state["sends"] if item["to"] == "carol@example.com")
    assert dispatch(config) == []
    with db.get_db(config.db_path) as conn:
        try:
            group_migrate.rollback_publisher(conn, config, ACTOR, JOB.name, actor_id=ACTOR, expected_revision=2)
        except GroupError as error:
            assert error.code == "group_deliveries_inflight"
        else:
            raise AssertionError("rollback accepted uncertain delivery")
        assert store.retry_deliveries(conn, config, state["run"]["id"], actor_id=ACTOR, is_admin=True,
                                     acknowledge_duplicates=True) == 1
    acknowledged = dispatch(config)
    assert acknowledged == [next(item for item in state["sends"] if item["to"] == "dave@example.com")]
    user_profiles.provision_user(config.db_path, "newuser", {})
    config = load_config(Path("/data/config/config.toml"))
    with db.get_db(config.db_path) as conn:
        assert not user_briefings.get_briefing(config.db_path, "newuser", "daily", conn=conn)
        assert group_runs.advance_runs(conn, config) == []
        assert store.get_run(conn, state["run"]["id"]) == state["run"]
        assert not any(row["user_id"] == "newuser" for row in store.list_recipients(conn, state["run"]["id"]))
        assert [list(row) for row in conn.execute("SELECT recipient_id,part_index,send_identity FROM group_briefing_parts ORDER BY recipient_id,part_index")] == state["parts"]
        assert [list(row) for row in conn.execute("SELECT * FROM shared_kv ORDER BY namespace,key")] == state["kv"]
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE source_type='group_briefing_generation'").fetchone()[0] == 2
        assert conn.execute("SELECT COUNT(*) FROM tasks WHERE source_type='group_briefing_research'").fetchone()[0] == 0
        group_migrate.rollback_publisher(conn, config, ACTOR, JOB.name, actor_id=ACTOR, expected_revision=2)
        sync_cron_jobs_to_db(conn, ACTOR, [JOB])
        assert db.get_scheduled_job_by_name(conn, ACTOR, JOB.name).enabled
        assert not store.get_definition(conn, state["definition"])["enabled"]
        assert store.get_run(conn, state["run"]["id"]) == state["run"]
    return {"retried": [item["to"] for item in sends], "acknowledged": [item["to"] for item in acknowledged],
            "same_edition": True, "retirement_survived_restart": True, "rollback": True}


if __name__ == "__main__":
    config = load_config(Path("/data/config/config.toml"))
    print(json.dumps((prepare if sys.argv[1] == "prepare" else resume)(config)))
