"""Shared definitions and retained references across real briefing stores."""

from contextlib import closing
import sqlite3

import pytest

from istota import db, user_profiles
from istota.briefings import db as bdb
from istota.config import Config
from istota.lib import toml_write
from istota.lib.sqlite_util import connect_read_only
from istota.settings_plan import engine
from istota.settings_plan.schema import SchemaError, parse_plan


@pytest.fixture
def install(tmp_path):
    database = tmp_path / "framework.db"
    db.init_db(database)
    config = tmp_path / "config.toml"
    config.write_text(toml_write.dumps({"db_path": str(database), "briefing_shared_blocks": [], "default_briefings": []}))
    for user in ("alice", "bob"):
        user_profiles.provision_user(database, user, {})
    return config, database


def preview(install, document, **options):
    context = engine.Context(install[0])
    operations = engine.build_plan(parse_plan(document), context, engine.Options(**options))
    return context, operations


def shared(install, name="news", **fields):
    with db.get_db(install[1]) as conn:
        return db.upsert_shared_block_config(conn, **{"name": name, "cron": "0 7 * * *", **fields})


def reference(install, user="bob", name="news"):
    path = Config(db_path=install[1]).module_db_path(user, "briefings")
    bdb.init_db(path)
    with bdb.connect(path) as conn:
        block = bdb.add_block(conn, briefing_name="morning", title="News")
        bdb.add_source(conn, block_id=block, kind="shared_block", config={"name": name})
        conn.commit()
    return path


def test_shared_create_partial_update_snapshot_and_no_drift(install):
    from istota.settings_plan.shared_briefings import snapshot_shared_blocks

    document = {"shared_briefing_blocks": [{"name": "news", "cron": "0 7 * * *", "sources": [{"kind": "browse"}]}]}
    before = install[1].read_bytes()
    context, operations = preview(install, document, dry_run=True)
    assert engine.execute_plan(operations, context).exit_code == 2
    assert install[1].read_bytes() == before
    context, operations = preview(install, document)
    assert engine.execute_plan(operations, context).restart_required
    with db.get_db(install[1]) as conn:
        db.set_briefing_shared_block_last_run(conn, "news", "2026-01-01")
        db.shared_kv_set(conn, "briefing_shared_blocks", "news", '{"text":"Saved"}', "daemon")
    context, operations = preview(install, {"shared_briefing_blocks": [{"name": "news", "trusted": True}]})
    assert engine.execute_plan(operations, context).exit_code == 2
    with closing(connect_read_only(install[1])) as conn:
        conn.row_factory = sqlite3.Row
        definitions = snapshot_shared_blocks(conn)
        assert definitions == [{"name": "news", "cron": "0 7 * * *", "title": "", "directive": "", "render_mode": "synthesis", "enabled": True, "trusted": True, "sources": [{"kind": "browse", "config": {}}]}]
        assert db.get_briefing_shared_block_last_run(conn, "news") == "2026-01-01"
    context, operations = preview(install, {"shared_briefing_blocks": definitions})
    assert engine.execute_plan(operations, context).exit_code == 0
    context, operations = preview(install, {"shared_briefing_blocks": []}, prune=True)
    assert engine.execute_plan(operations, context).exit_code == 2
    with db.get_db(install[1]) as conn:
        assert db.shared_kv_get(conn, "briefing_shared_blocks", "news") is not None
        assert db.get_briefing_shared_block_last_run(conn, "news") == "2026-01-01"


@pytest.mark.parametrize("kind", ["calendar", "rss", "shared_block"])
def test_shared_sources_refuse_personal_or_chained_content(install, kind):
    with pytest.raises(SchemaError):
        preview(install, {"shared_briefing_blocks": [{"name": "news", "sources": [{"kind": kind}]}]})


@pytest.mark.parametrize("definition", [{"name": "news"}, {"name": "news", "cron": "bad"}, {"name": "Bad Name", "cron": "0 7 * * *"}])
def test_shared_domain_validation(install, definition):
    with pytest.raises(engine.ExecutionError, match="validation_error"):
        preview(install, {"shared_briefing_blocks": [definition]})


def test_prune_refuses_reference_from_unselected_user_or_preserved_tail(install):
    shared(install)
    reference(install)
    document = {"shared_briefing_blocks": [], "users": {"alice": {"briefings": []}}}
    with pytest.raises(engine.ExecutionError, match="shared_block_reference"):
        preview(install, document, prune=True)
    document["users"]["bob"] = {"briefings": [{"name": "morning", "cron": "0 7 * * *", "output": "email"}]}
    with pytest.raises(engine.ExecutionError, match="shared_block_reference"):
        preview(install, document, prune=True)


def test_remove_reference_then_delete_shared_and_skip_on_user_failure(install):
    shared(install)
    reference(install)
    document = {"shared_briefing_blocks": [{"name": "new", "cron": "0 8 * * *"}], "users": {"bob": {"briefings": []}}}
    context, operations = preview(install, document, prune=True, continue_on_error=True)
    content = next(unit for unit in context.units if unit.section == "briefing_content")
    original = content.write
    def fail(conn, operations):
        raise RuntimeError("unavailable")
    content.write = fail
    result = engine.execute_plan(operations, context)
    assert result.partial and result.exit_code == 1
    assert next(op for op in result.operations if op.action == "delete" and op.user is None).status == "skipped"
    content.write = original
    context, operations = preview(install, document, prune=True)
    assert engine.execute_plan(operations, context).exit_code == 2
    with db.get_db(install[1]) as conn:
        assert [row.name for row in db.list_shared_block_configs(conn)] == ["new"]


def test_user_scope_ignores_shared_updates_but_checks_references(install):
    document = {"shared_briefing_blocks": [{"name": "news", "cron": "0 7 * * *"}], "users": {"alice": {"briefings": [{"name": "morning", "cron": "0 7 * * *", "output": "email", "blocks": [{"title": "News", "sources": [{"kind": "shared_block", "config": {"name": "news"}}]}]}]}}}
    with pytest.raises(engine.ExecutionError, match="shared_block_reference"):
        preview(install, document, users=["alice"])
    assert not Config(db_path=install[1]).module_db_path("alice", "briefings").exists()
    context, operations = preview(install, document)
    assert engine.execute_plan(operations, context).exit_code == 2
    context, operations = preview(install, document, users=["alice"])
    assert engine.execute_plan(operations, context).exit_code == 0


def test_seed_conflict_and_config_seed_reference(install):
    shared(install)
    raw = {"db_path": str(install[1]), "briefing_shared_blocks": [{"name": "news", "cron": "0 7 * * *"}], "default_briefings": []}
    install[0].write_text(toml_write.dumps(raw))
    with pytest.raises(engine.ExecutionError, match="shared_block_seed_conflict") as error:
        preview(install, {"shared_briefing_blocks": []}, prune=True)
    assert error.value.path == ["config", "briefing_shared_blocks", 0]
    raw["briefing_shared_blocks"] = []
    raw["default_briefings"] = [{"name": "morning", "cron": "0 7 * * *", "blocks": [{"title": "News", "sources": [{"kind": "shared_block", "config": {"name": "news"}}]}]}]
    install[0].write_text(toml_write.dumps(raw))
    with pytest.raises(engine.ExecutionError, match="shared_block_reference"):
        preview(install, {"shared_briefing_blocks": []}, prune=True)


def test_stale_definition_and_new_reference_refuse_delete(install):
    shared(install)
    context, operations = preview(install, {"shared_briefing_blocks": [{"name": "news", "title": "Changed"}]})
    shared(install, title="Concurrent")
    assert engine.execute_plan(operations, context).errors[0]["code"] == "state_changed"
    context, operations = preview(install, {"shared_briefing_blocks": []}, prune=True)
    reference(install)
    assert engine.execute_plan(operations, context).errors[0]["code"] == "state_changed"


def test_corrupt_shared_json_refused_without_writes(install):
    shared(install)
    with db.get_db(install[1]) as conn:
        conn.execute("UPDATE shared_block_configs SET sources = 'broken'")
    with pytest.raises(engine.ExecutionError, match="shared_block_store_invalid"):
        preview(install, {"shared_briefing_blocks": []}, prune=True)


def test_legacy_source_defaults_snapshot_without_drift(install):
    from istota.settings_plan.shared_briefings import snapshot_shared_blocks

    shared(install, sources=[{"kind": "browse"}])
    with db.get_db(install[1]) as conn:
        definitions = snapshot_shared_blocks(conn)
    assert definitions[0]["sources"] == [{"kind": "browse", "config": {}}]
    context, operations = preview(install, {"shared_briefing_blocks": definitions})
    assert engine.execute_plan(operations, context).exit_code == 0


def test_custom_published_reference_survives_unrelated_global_prune(install):
    shared(install)
    reference(install, "alice", "custom")
    with db.get_db(install[1]) as conn:
        db.shared_kv_set(conn, "briefing_shared_blocks", "custom", '{"text":"Saved"}', "publisher")
    document = {"shared_briefing_blocks": [], "users": {"alice": {"briefings": [{"name": "morning", "cron": "0 7 * * *", "output": "email"}]}}}
    context, operations = preview(install, document, prune=True)
    assert engine.execute_plan(operations, context).exit_code == 2


def test_user_content_rechecks_shared_reference_before_write(install):
    shared(install)
    document = {"users": {"alice": {"briefings": [{"name": "morning", "cron": "0 7 * * *", "output": "email", "blocks": [{"title": "News", "sources": [{"kind": "shared_block", "config": {"name": "news"}}]}]}]}}}
    context, operations = preview(install, document)
    with db.get_db(install[1]) as conn:
        db.delete_shared_block_config(conn, "news")
    result = engine.execute_plan(operations, context)
    assert result.errors[0]["code"] == "state_changed"
    assert result.partial
