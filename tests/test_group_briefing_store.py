"""Framework storage for one common edition and its audience."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Barrier
from uuid import uuid4

import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.groups.policy import GroupError

NOW = datetime(2026, 10, 15, 7, tzinfo=timezone.utc)
TABLES = ("group_briefing_migrations", "group_briefing_configs", "group_briefing_runs", "group_briefing_recipients",
          "group_briefing_parts", "group_briefing_preferences", "group_briefing_group_preferences")


@pytest.fixture
def env(tmp_path):
    config = Config(db_path=tmp_path / "app.db", users={
        name: UserConfig() for name in ("alice", "bob", "carol", "admin")
    })
    config.email.enabled = True
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        db.create_group(conn, "team", kind="group", display_name="Team", created_by="alice")
        db.add_group_member(conn, "team", "alice", role="owner", added_by="alice")
        db.add_group_member(conn, "team", "bob", added_by="alice")
    return config


def payload(**changes):
    value = {
        "name": "daily", "title": "Daily news", "cron": "0 7 * * *",
        "timezone": "Europe/London", "channel": "email", "enabled": True,
        "generation_mode": "prompt",
        "generation": {"version": 1, "prompt": "Write a short edition.",
                       "research": {"enabled": False}, "inputs": []},
    }
    value.update(changes)
    return value


def define(conn, config, **changes):
    from istota.briefings.group_store import save_definition
    return save_definition(conn, config, "team", payload(**changes),
                           actor_id="alice", expected_revision=0)


def enqueue(conn, config, definition, **kwargs):
    from istota.briefings.group_store import enqueue_run
    return enqueue_run(conn, config, definition["id"], actor_id="alice",
                       request_id=str(uuid4()), now=NOW, **kwargs)


def test_migration_matches_schema_and_repeats_safely(tmp_path):
    old = tmp_path / "old.db"
    db.init_db(old)
    with db.get_db(old) as conn:
        for table in reversed(TABLES):
            conn.execute(f"DROP TABLE {table}")
        conn.commit()
        db._run_migrations(conn)
        conn.commit()
        db._run_migrations(conn)
    declared = sqlite3.connect(":memory:")
    declared.executescript((Path(__file__).resolve().parents[1] / "schema.sql").read_text())
    with db.get_db(old) as upgraded:
        for table in TABLES:
            expected = declared.execute(f"PRAGMA table_info({table})").fetchall()
            actual = [tuple(r) for r in upgraded.execute(f"PRAGMA table_info({table})")]
            assert expected and actual == expected, table
            assert sorted(tuple(r)[1:4] for r in upgraded.execute(f"PRAGMA index_list({table})")) == sorted(
                tuple(r)[1:4] for r in declared.execute(f"PRAGMA index_list({table})")
            )
        assert [tuple(r) for r in upgraded.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'group_briefing_%'"
        )] == declared.execute(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'group_briefing_%'"
        ).fetchall()
    declared.close()


def test_disabled_draft_default_revision_and_immutable_target(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        raw = payload()
        del raw["enabled"]
        first = store.save_definition(conn, env, "team", raw, actor_id="alice", expected_revision=0)
        assert first["enabled"] is False
        assert first["revision"] == 1 and first["generation"]["version"] == 1
        second = store.save_definition(conn, env, "team", payload(title="Changed"),
                                       definition_id=first["id"], actor_id="alice", expected_revision=1)
        assert second["revision"] == 2 and second["created_by"] == "alice"
        with pytest.raises(GroupError, match="reload"):
            store.save_definition(conn, env, "team", payload(), definition_id=first["id"],
                                  actor_id="alice", expected_revision=1)
        db.create_group(conn, "other", kind="group", display_name="Other", created_by="alice")
        db.add_group_member(conn, "other", "alice", role="owner", added_by="alice")
        conn.commit()
        with pytest.raises(GroupError) as exc:
            store.save_definition(conn, env, "other", payload(), definition_id=first["id"],
                                  actor_id="alice", expected_revision=2)
        assert exc.value.code == "immutable_target"


@pytest.mark.parametrize("actor,is_admin,expected", [("bob", False, 403), ("admin", True, 404)])
def test_administration_does_not_grant_private_group_authority(env, actor, is_admin, expected):
    from istota.briefings.group_store import save_definition
    with db.get_db(env.db_path) as conn, pytest.raises(GroupError) as exc:
        save_definition(conn, env, "team", payload(), actor_id=actor,
                        is_admin=is_admin, expected_revision=0)
    assert exc.value.status_code == expected


def test_live_capabilities_ignore_owner_module_toggle_but_gate_delivery(env):
    from istota.briefings.group_store import briefing_capabilities
    env.users["alice"].disabled_modules = ["briefings"]
    with db.get_db(env.db_path) as conn:
        caps = briefing_capabilities(conn, env, "team", "alice")
        assert caps["read_briefings"] and caps["author_briefings"] and caps["generate_briefings"]
        assert not caps["receive_briefings"]
        del env.users["alice"]
        assert not any(briefing_capabilities(conn, env, "team", "alice").values())


@pytest.mark.parametrize("changes", [
    {"timezone": "Not/AZone"}, {"cron": "broken"}, {"channel": "all"},
    {"channel": "group:team"}, {"generation_mode": "other"},
    {"generation": {"version": 2, "prompt": "Text"}},
    {"generation": {"version": 1, "prompt": ""}},
    {"generation": {"version": 1, "prompt": "Text", "research": {"enabled": True}}},
    {"generation": {"version": 1, "prompt": "Text", "research": {
        "enabled": True, "prompt": "Research", "allowed_domains": ["example.com"]}}},
    {"generation": {"version": 1, "prompt": "Text", "inputs": [
        {"id": "private", "kind": "calendar", "config": {}}
    ]}},
    {"generation": {"version": 1, "prompt": "Text", "inputs": [
        {"id": "same", "kind": "markets"}, {"id": "same", "kind": "markets"}
    ]}},
    {"generation": {"version": 1, "prompt": "Text", "contains_personal_information": True}},
])
def test_invalid_definition_cannot_enable(env, changes):
    with db.get_db(env.db_path) as conn, pytest.raises(GroupError) as exc:
        define(conn, env, **changes)
    assert exc.value.status_code == 422


def test_disabled_channel_and_contributions_fail_closed_until_approved(env):
    env.email.enabled = False
    with db.get_db(env.db_path) as conn, pytest.raises(GroupError) as exc:
        define(conn, env)
    assert exc.value.code == "channel_unavailable"
    env.email.enabled = True
    generation = {"version": 1, "prompt": "Text", "inputs": [
        {"id": "contribution", "kind": "personal_file", "config": {"owner_id": "bob", "path": "notes.md"}}
    ]}
    with db.get_db(env.db_path) as conn:
        draft = define(conn, env, enabled=False, generation=generation)
        with pytest.raises(GroupError) as exc:
            enqueue(conn, env, draft, mode="preview")
        assert exc.value.code == "source_approval_required"


def test_block_settings_use_existing_order_and_render_modes(env):
    from istota.briefings.group_store import definition_blocks
    from istota.briefings.models import BriefingBlock, BlockSource
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, generation_mode="blocks", generation={"version": 1, "blocks": [
            {"title": "Quotes", "sources": [{"id": "quotes", "kind": "markets", "config": {}}]},
            {"title": "Notes", "directive": "Summarize", "sources": [
                {"id": "notebook", "kind": "group_memory", "config": {}}
            ]},
        ]})
        blocks = definition_blocks(definition)
        assert all(isinstance(block, BriefingBlock) for block in blocks)
        assert all(isinstance(source, BlockSource) for block in blocks for source in block.sources)
        assert [(b.title, b.position, b.render_mode) for b in blocks] == [
            ("Quotes", 0, "structured"), ("Notes", 1, "synthesis")
        ]
        assert definition["generation"]["blocks"][1]["sources"][0]["id"] == "notebook"


def test_uuid_deduplication_conflicting_payload_and_scheduled_utc_identity(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        request = str(uuid4())
        first = store.enqueue_run(conn, env, definition["id"], actor_id="alice", request_id=request, now=NOW)
        again = store.enqueue_run(conn, env, definition["id"], actor_id="alice", request_id=request,
                                  now=NOW + timedelta(seconds=5))
        assert again["id"] == first["id"]
        with pytest.raises(GroupError) as exc:
            store.enqueue_run(conn, env, definition["id"], actor_id="alice", request_id=request,
                              mode="preview", now=NOW)
        assert exc.value.code == "request_conflict"
        due = NOW.astimezone(timezone(timedelta(hours=1)))
        scheduled = store.enqueue_run(conn, env, definition["id"], due_at=due, now=NOW)
        assert scheduled["id"] == store.enqueue_run(conn, env, definition["id"], due_at=NOW, now=NOW)["id"]
        assert scheduled["snapshot"]["definition"]["revision"] == definition["revision"]
        assert scheduled["snapshot"]["grants"] == []


def test_competing_connections_admit_and_claim_one_occurrence(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)

    barrier = Barrier(2)

    def admit(_):
        with db.get_db(env.db_path) as conn:
            barrier.wait(timeout=5)
            return store.enqueue_run(conn, env, definition["id"], due_at=NOW, now=NOW)["id"]

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = list(pool.map(admit, range(2)))
    assert ids[0] == ids[1]

    def claim(_):
        with db.get_db(env.db_path) as conn:
            barrier.wait(timeout=5)
            return store.claim_run(conn, run_id=ids[0], now=NOW, lease_seconds=10)

    with ThreadPoolExecutor(max_workers=2) as pool:
        claims = list(pool.map(claim, range(2)))
    assert len([claim for claim in claims if claim]) == 1


def test_expired_worker_cannot_publish_or_overwrite_research(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, generation={"version": 1, "prompt": "Text", "research": {
            "enabled": True, "prompt": "Find public news", "allowed_domains": []
        }})
        run = enqueue(conn, env, definition)
        old = store.claim_run(conn, run_id=run["id"], now=NOW, lease_seconds=10)
        assert old["state"] == "researching"
        with pytest.raises(GroupError) as exc:
            store.publish_run(conn, env, run["id"], old["attempt_token"], subject="Old", body_md="Old",
                              now=NOW + timedelta(seconds=11))
        assert exc.value.code == "lease_lost"
        new = store.claim_run(conn, run_id=run["id"], now=NOW + timedelta(seconds=11))
        assert new["attempt_token"] != old["attempt_token"]
        with pytest.raises(GroupError):
            store.save_research(conn, run["id"], old["attempt_token"], {"text": "Old"}, now=NOW)
        researched = store.save_research(conn, run["id"], new["attempt_token"],
                                         {"text": "News", "citations": ["https://example.com/article"]},
                                         now=NOW + timedelta(seconds=12))
        assert researched["state"] == "generating"
        assert researched["research_attempt_token"] == new["attempt_token"]
        taken = store.claim_run(conn, run_id=run["id"], now=NOW + timedelta(minutes=6))
        assert taken["research"]["text"] == "News" and taken["state"] == "generating"


def test_edit_supersedes_active_run_and_preserves_snapshot(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        run = enqueue(conn, env, definition)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        store.save_definition(conn, env, "team", payload(title="Changed"), definition_id=definition["id"],
                              actor_id="alice", expected_revision=definition["revision"])
        saved = store.get_run(conn, run["id"])
        assert saved["state"] == "superseded"
        assert saved["snapshot"]["definition"]["title"] == "Daily news"
        with pytest.raises(GroupError):
            store.publish_run(conn, env, run["id"], claim["attempt_token"], subject="Old", body_md="Old", now=NOW)
        assert conn.execute("SELECT COUNT(*) FROM group_briefing_recipients").fetchone()[0] == 0


def test_atomic_publication_freezes_edition_and_membership_intervals(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        run = enqueue(conn, env, definition)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        edition = store.publish_run(conn, env, run["id"], claim["attempt_token"], subject="News", body_md="Common",
                                    provenance={"sources": ["public"]}, usage={"tokens": 10}, now=NOW)
        assert edition["state"] == "published" and edition["body_md"] == "Common"
        recipients = store.list_recipients(conn, run["id"])
        assert {(r["user_id"], r["channel"]) for r in recipients} == {
            (user, channel) for user in ("alice", "bob") for channel in ("in_app", "email")
        }
        for recipient in recipients:
            member = conn.execute("SELECT * FROM group_members WHERE id = ?", (recipient["membership_id"],)).fetchone()
            assert member["group_id"] == "team" and member["user_id"] == recipient["user_id"]
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE group_briefing_runs SET body_md = 'changed' WHERE id = ?", (run["id"],))
        conn.rollback()
        with pytest.raises(GroupError):
            store.publish_run(conn, env, run["id"], claim["attempt_token"], subject="Again", body_md="Again", now=NOW)
        db.end_group_membership(conn, "team", "bob", ended_by="alice")
        db.add_group_member(conn, "team", "bob", added_by="alice")
        db.add_group_member(conn, "team", "carol", added_by="alice")
        conn.commit()
        assert store.list_recipients(conn, run["id"]) == recipients
        with sqlite3.connect(env.db_path) as reopened:
            assert reopened.execute("SELECT body_md FROM group_briefing_runs WHERE id = ?", (run["id"],)).fetchone() == ("Common",)


def test_recipient_failure_rolls_back_publication(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        run = enqueue(conn, env, definition)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        conn.execute("""CREATE TRIGGER refuse_test_recipient BEFORE INSERT ON group_briefing_recipients
                        BEGIN SELECT RAISE(ABORT, 'test recipient failure'); END""")
        conn.commit()
        with pytest.raises(sqlite3.IntegrityError, match="test recipient failure"):
            store.publish_run(conn, env, run["id"], claim["attempt_token"], subject="News", body_md="Common", now=NOW)
        assert store.get_run(conn, run["id"])["state"] == "generating"
        assert not store.list_recipients(conn, run["id"])


def test_preview_has_no_audience_and_cannot_be_promoted(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, enabled=False)
        run = enqueue(conn, env, definition, mode="preview")
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        preview = store.publish_run(conn, env, run["id"], claim["attempt_token"], subject="Draft", body_md="Private", now=NOW)
        assert preview["state"] == "preview_ready" and not store.list_recipients(conn, run["id"])
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute("UPDATE group_briefing_runs SET mode = 'publish', state = 'published' WHERE id = ?", (run["id"],))


def test_publication_rechecks_archive_and_enabled_accounts(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, channel="in_app")
        run = enqueue(conn, env, definition)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        del env.users["bob"]
        store.publish_run(conn, env, run["id"], claim["attempt_token"], subject="News", body_md="Common", now=NOW)
        assert [r["user_id"] for r in store.list_recipients(conn, run["id"])] == ["alice"]
        another = enqueue(conn, env, definition)
        claim = store.claim_run(conn, run_id=another["id"], now=NOW)
        db.archive_group(conn, "team")
        conn.commit()
        with pytest.raises(GroupError):
            store.publish_run(conn, env, another["id"], claim["attempt_token"], subject="News", body_md="Common", now=NOW)
        assert not store.list_recipients(conn, another["id"])


def test_all_users_publication_reconciles_new_accounts_before_snapshot(env):
    from istota.briefings import group_store as store
    from istota.groups.system import ensure_all_users_group
    with db.get_db(env.db_path) as conn:
        group = ensure_all_users_group(conn, env)
        definition = store.save_definition(conn, env, group["group_id"], payload(channel="in_app"),
                                           actor_id="admin", is_admin=True, expected_revision=0)
        run = store.enqueue_run(conn, env, definition["id"], actor_id="admin", is_admin=True,
                                request_id=str(uuid4()), now=NOW)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        env.users["dave"] = UserConfig()
        store.publish_run(conn, env, run["id"], claim["attempt_token"], subject="News", body_md="Common", now=NOW)
        assert "dave" in {r["user_id"] for r in store.list_recipients(conn, run["id"])}


def test_lease_renewal_extends_ownership_and_terminal_failure_cannot_be_reclaimed(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        run = enqueue(conn, env, definition)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW, lease_seconds=10)
        store.renew_run_lease(conn, run["id"], claim["attempt_token"], now=NOW + timedelta(seconds=9), lease_seconds=10)
        assert store.claim_run(conn, run_id=run["id"], now=NOW + timedelta(seconds=11)) is None
        failed = store.finish_run(conn, run["id"], claim["attempt_token"], error_code="source_unavailable",
                                  now=NOW + timedelta(seconds=12))
        assert failed["state"] == "failed" and failed["attempt_token"] is None
        assert store.claim_run(conn, run_id=run["id"], now=NOW + timedelta(days=1)) is None
        with pytest.raises(GroupError) as exc:
            store.finish_run(conn, run["id"], claim["attempt_token"], error_code="raw provider failure with content")
        assert exc.value.code == "invalid_run_outcome"


def test_current_lease_expires_at_the_boundary(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        run = enqueue(conn, env, define(conn, env))
        claim = store.claim_run(conn, run_id=run["id"], now=NOW, lease_seconds=10)
        with pytest.raises(GroupError) as exc:
            store.renew_run_lease(conn, run["id"], claim["attempt_token"], now=NOW + timedelta(seconds=10))
        assert exc.value.code == "lease_lost"
        assert store.claim_run(conn, run_id=run["id"], now=NOW + timedelta(seconds=10)) is not None


def test_database_constraints_preserve_target_and_recipient_uniqueness(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        with pytest.raises(sqlite3.IntegrityError, match="target immutable"):
            conn.execute("UPDATE group_briefing_configs SET group_id = 'other' WHERE id = ?", (definition["id"],))
        conn.rollback()
        run = enqueue(conn, env, definition)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        store.publish_run(conn, env, run["id"], claim["attempt_token"], subject="News", body_md="Common", now=NOW)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            conn.execute("""INSERT INTO group_briefing_recipients (run_id, user_id, membership_id, channel)
                            SELECT run_id, user_id, membership_id, channel FROM group_briefing_recipients LIMIT 1""")


def test_module_opt_out_keeps_history_and_skips_only_external_leg(env):
    from istota.briefings import group_store as store
    env.users["bob"].disabled_modules = ["briefings"]
    with db.get_db(env.db_path) as conn:
        run = enqueue(conn, env, define(conn, env))
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        store.publish_run(conn, env, run["id"], claim["attempt_token"], subject="News", body_md="Common", now=NOW)
        states = {r["channel"]: r["state"] for r in store.list_recipients(conn, run["id"]) if r["user_id"] == "bob"}
        assert states == {"email": "skipped", "in_app": "pending"}
        assert store.briefing_capabilities(conn, env, "team", "bob")["read_briefings"]


def test_revision_is_rechecked_even_if_edit_bypasses_the_save_helper(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        run = enqueue(conn, env, definition)
        claim = store.claim_run(conn, run_id=run["id"], now=NOW)
        conn.execute("UPDATE group_briefing_configs SET revision = revision + 1 WHERE id = ?", (definition["id"],))
        conn.commit()
        with pytest.raises(GroupError) as exc:
            store.publish_run(conn, env, run["id"], claim["attempt_token"], subject="News", body_md="Common", now=NOW)
        assert exc.value.code == "revision_conflict"
        assert not store.list_recipients(conn, run["id"])


def test_scheduled_claim_waits_for_the_due_instant(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        run = store.enqueue_run(conn, env, definition["id"], due_at=NOW + timedelta(minutes=1), now=NOW)
        assert store.claim_run(conn, run_id=run["id"], now=NOW) is None
        assert store.claim_run(conn, run_id=run["id"], now=NOW + timedelta(minutes=1)) is not None


def test_exclusions_preserve_absent_members_and_require_current_members_for_new_ids(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        first = define(conn, env, excluded_user_ids=["bob", "bob"])
        assert first["excluded_user_ids"] == ["bob"]
        db.end_group_membership(conn, "team", "bob", ended_by="alice")
        conn.commit()
        second = store.save_definition(conn, env, "team", payload(title="Changed"),
            definition_id=first["id"], actor_id="alice", expected_revision=1)
        assert second["excluded_user_ids"] == ["bob"]
        third = store.save_definition(conn, env, "team", payload(excluded_user_ids=["bob"]),
            definition_id=first["id"], actor_id="alice", expected_revision=2)
        assert third["excluded_user_ids"] == ["bob"]
        with pytest.raises(GroupError) as error:
            store.save_definition(conn, env, "team", payload(excluded_user_ids=["carol"]),
                definition_id=first["id"], actor_id="alice", expected_revision=3)
        assert error.value.code == "invalid_excluded_user"
        db.add_group_member(conn, "team", "bob", added_by="alice")
        conn.commit()
        assert store.get_definition(conn, first["id"])["excluded_user_ids"] == ["bob"]
        cleared = store.save_definition(conn, env, "team", payload(excluded_user_ids=[]),
            definition_id=first["id"], actor_id="alice", expected_revision=3)
        assert cleared["excluded_user_ids"] == []


@pytest.mark.parametrize("excluded", [None, "bob", [None], [1], [""], [" bob"]])
def test_exclusions_reject_malformed_values(env, excluded):
    with db.get_db(env.db_path) as conn, pytest.raises(GroupError) as error:
        define(conn, env, excluded_user_ids=excluded)
    assert error.value.code == "invalid_definition"


def test_exclusions_upgrade_existing_definition_to_empty_default(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        first = define(conn, env)
        conn.execute("ALTER TABLE group_briefing_configs DROP COLUMN excluded_user_ids_json")
        conn.commit()
        db._run_migrations(conn)
        conn.commit()
        db._run_migrations(conn)
        assert store.get_definition(conn, first["id"])["excluded_user_ids"] == []
        assert store.get_definition(conn, first["id"])["revision"] == 1


def _published_with_queued(conn, config, **changes):
    """A published edition with pending legs (one mid-send) and a queued later run."""
    from istota.briefings import group_store as store
    definition = define(conn, config, **changes)
    run = enqueue(conn, config, definition)
    claim = store.claim_run(conn, run_id=run["id"], now=NOW)
    store.publish_run(conn, config, run["id"], claim["attempt_token"], subject="News", body_md="Common", now=NOW)
    conn.execute("UPDATE group_briefing_recipients SET state='sending' WHERE run_id=? AND user_id='bob' "
                 "AND channel='email'", (run["id"],))
    conn.commit()
    queued = enqueue(conn, config, store.get_definition(conn, definition["id"]))
    return store.get_definition(conn, definition["id"]), run, queued


def _ledger(conn, run_id):
    from istota.briefings import group_store as store
    return {(r["user_id"], r["channel"]): (r["state"], r["error_code"]) for r in store.list_recipients(conn, run_id)}


def test_hold_stops_the_same_work_as_disabling(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        disabled, disabled_run, disabled_queued = _published_with_queued(conn, env)
        store.save_definition(conn, env, "team", payload(enabled=False), definition_id=disabled["id"],
                              actor_id="alice", expected_revision=disabled["revision"])
        held, held_run, held_queued = _published_with_queued(conn, env, name="weekly")
        after = store.set_hold(conn, env, held["id"], admin_id="admin")
        assert after["admin_hold_at"] and after["admin_hold_by"] == "admin"
        assert after["enabled"] is True and after["revision"] == held["revision"] + 1
        swap = {"definition_disabled": "definition_held"}
        expected = {key: (state, swap.get(code, code)) for key, (state, code) in _ledger(conn, disabled_run["id"]).items()}
        assert _ledger(conn, held_run["id"]) == expected
        assert expected[("alice", "in_app")] == ("skipped", "definition_held")
        assert expected[("bob", "email")] == ("sending", "definition_held")
        for queued in (disabled_queued, held_queued):
            assert store.get_run(conn, queued["id"])["state"] == "superseded"
        sending = next(r for r in store.list_recipients(conn, held_run["id"]) if r["state"] == "sending")
        assert store.delivery_refusal(conn, env, sending) == "definition_held"


def test_hold_and_resume_are_idempotent_and_keep_the_owner_choice(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        assert store.set_hold(conn, env, 9999, admin_id="admin") is None
        assert store.clear_hold(conn, env, 9999) is None
        first = store.set_hold(conn, env, definition["id"], admin_id="admin")
        again = store.set_hold(conn, env, definition["id"], admin_id="other-admin")
        assert (again["admin_hold_at"], again["admin_hold_by"], again["revision"]) == (
            first["admin_hold_at"], "admin", first["revision"])
        resumed = store.clear_hold(conn, env, definition["id"])
        assert resumed["admin_hold_at"] is None and resumed["admin_hold_by"] is None
        assert resumed["enabled"] is True and resumed["revision"] == first["revision"] + 1
        assert store.clear_hold(conn, env, definition["id"])["revision"] == resumed["revision"]
        assert store.enqueue_run(conn, env, definition["id"], due_at=NOW, now=NOW)["state"] == "queued"


def test_held_definition_admits_no_publish_run_but_previews(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        store.set_hold(conn, env, definition["id"], admin_id="admin")
        for kwargs in ({"due_at": NOW}, {"actor_id": "alice", "request_id": str(uuid4())}):
            with pytest.raises(GroupError) as exc:
                store.enqueue_run(conn, env, definition["id"], now=NOW, **kwargs)
            assert (exc.value.status_code, exc.value.code) == (409, "definition_held")
        assert enqueue(conn, env, definition, mode="preview")["mode"] == "preview"


def test_owner_may_edit_or_disable_a_held_definition_but_not_enable_it(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, enabled=False)
        held = store.set_hold(conn, env, definition["id"], admin_id="admin")
        with pytest.raises(GroupError) as exc:
            store.save_definition(conn, env, "team", payload(enabled=True), definition_id=definition["id"],
                                  actor_id="alice", expected_revision=held["revision"])
        assert (exc.value.status_code, exc.value.code) == (409, "definition_held")
        edited = store.save_definition(conn, env, "team", payload(enabled=False, title="Renamed"),
                                       definition_id=definition["id"], actor_id="alice",
                                       expected_revision=held["revision"])
        assert edited["title"] == "Renamed" and edited["admin_hold_at"] == held["admin_hold_at"]
        enabled = define(conn, env, name="weekly")
        held = store.set_hold(conn, env, enabled["id"], admin_id="admin")
        kept = store.save_definition(conn, env, "team", payload(name="weekly", title="Still on"),
                                     definition_id=enabled["id"], actor_id="alice",
                                     expected_revision=held["revision"])
        assert kept["enabled"] is True and kept["admin_hold_at"]
        off = store.save_definition(conn, env, "team", payload(name="weekly", enabled=False),
                                    definition_id=enabled["id"], actor_id="alice",
                                    expected_revision=kept["revision"])
        assert off["enabled"] is False and off["admin_hold_by"] == "admin"


def test_a_stale_editor_cannot_overwrite_a_hold(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        store.set_hold(conn, env, definition["id"], admin_id="admin")
        with pytest.raises(GroupError) as exc:
            store.save_definition(conn, env, "team", payload(title="Stale"), definition_id=definition["id"],
                                  actor_id="alice", expected_revision=definition["revision"])
        assert exc.value.code == "revision_conflict"


def test_delete_stops_work_renames_keeps_history_and_frees_the_name(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition, run, queued = _published_with_queued(conn, env)
        assert store.delete_definition(conn, definition["id"], admin_id="admin") is None
        deleted = store.get_definition(conn, definition["id"])
        assert deleted["deleted_at"] and deleted["deleted_by"] == "admin"
        assert deleted["name"] == f"deleted-{definition['id']}-daily" and deleted["title"] == "Daily news"
        assert deleted["enabled"] is False and deleted["revision"] == definition["revision"] + 1
        assert store.get_run(conn, queued["id"])["state"] == "superseded"
        assert store.get_run(conn, run["id"])["state"] == "published"
        ledger = _ledger(conn, run["id"])
        assert ledger[("alice", "in_app")] == ("skipped", "definition_deleted")
        assert ledger[("bob", "email")] == ("sending", "definition_deleted")
        sending = next(r for r in store.list_recipients(conn, run["id"]) if r["state"] == "sending")
        assert store.delivery_refusal(conn, env, sending) == "definition_deleted"
        assert store.list_definitions(conn, "team") == []
        reused = define(conn, env)
        assert reused["name"] == "daily" and reused["id"] != definition["id"]
        assert [item["id"] for item in store.list_definitions(conn, "team")] == [reused["id"]]


def test_delete_truncates_the_renamed_name_to_the_name_limit(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, name="n" * 100, enabled=False)
        store.delete_definition(conn, definition["id"], admin_id="admin")
        name = store.get_definition(conn, definition["id"])["name"]
        assert len(name) == 100 and name.startswith(f"deleted-{definition['id']}-n")


def test_a_deleted_definition_refuses_authoring_runs_and_admin_actions(env):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env)
        store.delete_definition(conn, definition["id"], admin_id="admin")
        deleted = store.get_definition(conn, definition["id"])
        for action in (
            lambda: store.delete_definition(conn, definition["id"], admin_id="admin"),
            lambda: store.delete_definition(conn, 9999, admin_id="admin"),
            lambda: store.enqueue_run(conn, env, definition["id"], due_at=NOW, now=NOW),
            lambda: enqueue(conn, env, definition, mode="preview"),
            lambda: store.save_definition(conn, env, "team", payload(name=deleted["name"], enabled=False),
                                          definition_id=definition["id"], actor_id="alice",
                                          expected_revision=deleted["revision"]),
        ):
            with pytest.raises(GroupError) as exc:
                action()
            assert (exc.value.status_code, exc.value.code) == (404, "definition_not_found")
        assert store.set_hold(conn, env, definition["id"], admin_id="admin") is None
        assert store.clear_hold(conn, env, definition["id"]) is None
        assert store.get_definition(conn, definition["id"])["revision"] == deleted["revision"]


@pytest.mark.parametrize("state", ["imported", "reviewed", "rolled_back", "cutover"])
def test_delete_refuses_a_definition_under_an_unfinished_migration(env, state):
    from istota.briefings import group_store as store
    with db.get_db(env.db_path) as conn:
        definition = define(conn, env, enabled=False)
        conn.execute("INSERT INTO group_briefing_migrations (legacy_key, definition_id, state, data_json) "
                     "VALUES (?, ?, ?, '{}')", ("default:daily", definition["id"], state))
        conn.commit()
        if state in ("cutover", "rolled_back"):
            store.delete_definition(conn, definition["id"], admin_id="admin")
            assert store.get_definition(conn, definition["id"])["deleted_at"]
            return
        with pytest.raises(GroupError) as exc:
            store.delete_definition(conn, definition["id"], admin_id="admin")
        assert (exc.value.status_code, exc.value.code) == (409, "migration_pending")
        assert store.get_definition(conn, definition["id"]) == definition
