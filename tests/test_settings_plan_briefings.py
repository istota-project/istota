"""Schedules and definition pruning over the real briefing stores."""

import json

import pytest

from istota import cli, db, user_briefings, user_profiles
from istota.briefings import db as bdb
from istota.config import Config
from istota.lib import toml_write
from istota.settings_plan import engine
from istota.settings_plan.schema import parse_plan


@pytest.fixture
def install(tmp_path):
    database = tmp_path / "framework.db"
    db.init_db(database)
    config = tmp_path / "config.toml"
    config.write_text(toml_write.dumps({"db_path": str(database)}))
    user_profiles.provision_user(database, "alice", {})
    return config, database


def preview(install, briefings, *, profile=None, **options):
    context = engine.Context(install[0])
    sections = {"briefings": briefings}
    if profile is not None:
        sections["profile"] = profile
    operations = engine.build_plan(parse_plan({"users": {"alice": sections}}), context, engine.Options(**options))
    return context, operations


def schedule(install, name="morning", **fields):
    return user_briefings.ensure_briefing(install[1], user_id="alice", name=name,
                                         **{"cron": "0 7 * * *", "output": "email", **fields})[0]


def module_path(install):
    return Config(db_path=install[1]).module_db_path("alice", "briefings")


def test_cli_dry_run_apply_and_idempotence(install, tmp_path, monkeypatch, capsys):
    config, database = install
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"users": {"alice": {"briefings": [
        {"name": "morning", "cron": "0 7 * * *", "output": "email"},
    ]}}}))
    before = database.read_bytes()
    for flags, expected in [(["--dry-run"], 2), ([], 2), ([], 0)]:
        monkeypatch.setattr("sys.argv", ["istota", "-c", str(config), "apply", "-f", str(plan), "--format", "json", *flags])
        code = 0
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
        assert code == expected
        report = json.loads(capsys.readouterr().out)
        assert report["restart_required"] is (expected == 2)
        if flags:
            assert database.read_bytes() == before
            assert user_briefings.list_briefings(database) == []
    row, = user_briefings.list_briefings(database)
    assert (row.title, row.conversation_token, row.enabled) == ("", "", True)
    assert not module_path(install).exists()


def test_partial_fields_explicit_defaults_and_runtime_preserved(install):
    row = schedule(install, title="Custom", enabled=False, components={"legacy": True})
    with db.get_db(install[1]) as conn:
        conn.execute("UPDATE briefing_configs SET last_run_at = '2026-01-01' WHERE id = ?", (row.id,))
        db.set_briefing_last_run(conn, "alice", "morning")
        runtime = db.get_briefing_last_run(conn, "alice", "morning")
    context, operations = preview(install, [{"name": "morning", "title": ""}])
    assert engine.execute_plan(operations, context).exit_code == 2
    after = user_briefings.get_briefing(install[1], "alice", "morning")
    assert after.id == row.id and after.title == "" and not after.enabled
    assert after.cron == row.cron and after.output == "email" and after.components == row.components
    with db.get_db(install[1]) as conn:
        assert conn.execute("SELECT last_run_at FROM briefing_configs WHERE id = ?", (row.id,)).fetchone()[0] == "2026-01-01"
        assert db.get_briefing_last_run(conn, "alice", "morning") == runtime


def test_prune_definitions_preserves_archives_items_and_runtime(install):
    schedule(install)
    user_briefings.ensure_briefing(install[1], user_id="bob", name="morning", cron="0 7 * * *", output="email")
    path = module_path(install)
    bdb.init_db(path)
    with bdb.connect(path) as conn:
        block = bdb.add_block(conn, briefing_name="morning", title="Calendar")
        bdb.add_source(conn, block_id=block, kind="calendar")
        bdb.insert_archive(conn, briefing_name="morning", subject="Archive", body_md="Saved")
        conn.execute("INSERT INTO briefing_items (source_kind, dedup_key, ingested_at) VALUES ('calendar', 'item', '2026-01-01')")
        conn.execute("INSERT INTO briefing_item_state (item_id, briefing_name) VALUES (1, 'morning')")
        conn.commit()
    with db.get_db(install[1]) as conn:
        db.set_briefing_last_run(conn, "alice", "morning")
        runtime = db.get_briefing_last_run(conn, "alice", "morning")
    context, operations = preview(install, [])
    assert engine.execute_plan(operations, context).exit_code == 0
    context, operations = preview(install, [], prune=True)
    assert engine.execute_plan(operations, context).exit_code == 2
    assert user_briefings.get_briefing(install[1], "alice", "morning") is None
    assert user_briefings.get_briefing(install[1], "bob", "morning") is not None
    with bdb.connect(path) as conn:
        for table in ("briefing_blocks", "briefing_block_sources"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
        for table in ("briefing_archive", "briefing_items", "briefing_item_state"):
            assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 1
    with db.get_db(install[1]) as conn:
        assert db.get_briefing_last_run(conn, "alice", "morning") == runtime


@pytest.mark.parametrize("seed", ["users", "default_briefings"])
def test_prune_refuses_config_seed_and_names_controlling_declaration(install, seed):
    schedule(install)
    definition = {"name": "morning", "cron": "0 7 * * *", "output": "email"}
    raw = {"db_path": str(install[1])}
    raw[seed] = {"alice": {"briefings": [definition]}} if seed == "users" else [definition]
    install[0].write_text(toml_write.dumps(raw))
    with pytest.raises(engine.ExecutionError, match="briefing_seed_conflict") as exc:
        preview(install, [], prune=True)
    assert exc.value.path == (["config", "users", "alice", "briefings", 0] if seed == "users" else ["config", "default_briefings", 0])
    if seed == "default_briefings":
        context, operations = preview(install, [], profile={"default_briefings": False}, prune=True)
        assert engine.execute_plan(operations, context).exit_code == 2


@pytest.mark.parametrize("declaration", [
    {"name": "new"}, {"name": "new", "cron": "bad", "output": "email"},
    {"name": "new", "cron": "0 7 * * *", "output": "invalid"},
    {"name": "new", "cron": "0 7 * * *", "conversation_token": "other-room"},
])
def test_invalid_schedule_refuses_all_writes(install, declaration):
    before = install[1].read_bytes()
    with pytest.raises(engine.ExecutionError, match="validation_error"):
        preview(install, [declaration], profile={"display_name": "Changed"})
    assert install[1].read_bytes() == before
    assert not module_path(install).exists()


def test_stale_schedule_refused_and_runtime_does_not_block(install):
    schedule(install)
    declaration = [{"name": "morning", "title": "Planned"}]
    context, operations = preview(install, declaration)
    schedule(install, title="Concurrent")
    result = engine.execute_plan(operations, context)
    assert result.errors == [{"code": "state_changed", "path": ["users", "alice", "briefings"]}]
    context, operations = preview(install, declaration)
    with db.get_db(install[1]) as conn:
        conn.execute("UPDATE briefing_configs SET last_run_at = '2026-02-01'")
    assert engine.execute_plan(operations, context).exit_code == 2


def test_schedule_section_rolls_back_upserts_when_delete_fails(install):
    schedule(install)
    with db.get_db(install[1]) as conn:
        conn.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON briefing_configs BEGIN SELECT RAISE(ABORT, 'failure'); END")
    context, operations = preview(install, [{"name": "new", "cron": "0 8 * * *", "output": "email"}], prune=True)
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 1 and not result.changed
    assert [row.name for row in user_briefings.list_briefings(install[1])] == ["morning"]


def test_failed_content_prune_reports_partial_and_retry_cleans_orphans(install):
    schedule(install)
    path = module_path(install)
    bdb.init_db(path)
    with bdb.connect(path) as conn:
        bdb.add_block(conn, briefing_name="morning", title="Calendar")
        conn.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON briefing_blocks BEGIN SELECT RAISE(ABORT, 'failure'); END")
        conn.commit()
    context, operations = preview(install, [], prune=True)
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 1 and result.partial and result.restart_required
    assert [(op.section, op.status) for op in result.operations] == [
        ("briefings", "applied"), ("briefing_content", "failed")]
    with bdb.connect(path) as conn:
        assert bdb.list_briefing_names(conn) == ["morning"]
        conn.execute("DROP TRIGGER refuse_delete")
        conn.commit()
    context, operations = preview(install, [], prune=True)
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 2 and not result.restart_required
    with bdb.connect(path) as conn:
        assert bdb.list_briefing_names(conn) == []


def test_stale_module_definition_refused_but_archive_writes_do_not_block(install):
    path = module_path(install)
    bdb.init_db(path)
    with bdb.connect(path) as conn:
        block = bdb.add_block(conn, briefing_name="orphan", title="Before")
        conn.commit()
    context, operations = preview(install, [], prune=True)
    with bdb.connect(path) as conn:
        bdb.update_block(conn, block, title="Concurrent")
        conn.commit()
    result = engine.execute_plan(operations, context)
    assert result.errors == [{"code": "state_changed", "path": ["users", "alice", "briefing_content"]}]
    context, operations = preview(install, [], prune=True)
    with bdb.connect(path) as conn:
        bdb.insert_archive(conn, briefing_name="orphan", subject="Archive", body_md="Saved")
        conn.commit()
    assert engine.execute_plan(operations, context).exit_code == 2


def test_room_reach_is_rechecked_inside_schedule_transaction(install):
    with db.get_db(install[1]) as conn:
        db.register_room(conn, "private-alice", "alice", origin="web")
        db.add_room_binding(conn, "private-alice", "talk", "talk-alice")
    declaration = [{"name": "morning", "cron": "0 7 * * *", "conversation_token": "talk-alice"}]
    context, operations = preview(install, declaration)
    with db.get_db(install[1]) as conn:
        db.add_room_member(conn, "private-alice", "bob")
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 1 and not result.changed
    assert user_briefings.list_briefings(install[1]) == []
    with pytest.raises(engine.ExecutionError, match="validation_error"):
        preview(install, declaration)


def test_omitted_collection_and_unselected_users_are_untouched(install):
    schedule(install)
    context = engine.Context(install[0])
    plan = parse_plan({"users": {"alice": {}, "bob": {"briefings": []}}})
    operations = engine.build_plan(plan, context, engine.Options(prune=True, users=["alice"]))
    assert engine.execute_plan(operations, context).exit_code == 0
    assert user_briefings.get_briefing(install[1], "alice", "morning") is not None
    assert user_profiles.get_profile(install[1], "bob") is None


def test_corrupt_module_store_refuses_before_schedule_prune(install):
    schedule(install)
    path = module_path(install)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"not a database")
    with pytest.raises(engine.ExecutionError, match="briefing_store_unavailable"):
        preview(install, [], prune=True)
    assert user_briefings.get_briefing(install[1], "alice", "morning") is not None


def test_caller_owned_ensure_and_delete_rollback(install):
    schedule(install)
    with db.get_db(install[1]) as conn:
        conn.execute("BEGIN IMMEDIATE")
        user_briefings.ensure_briefing(install[1], user_id="alice", name="new", cron="0 8 * * *", output="email", conn=conn)
        assert user_briefings.get_briefing(install[1], "alice", "new", conn=conn) is not None
        user_briefings.delete_briefing(install[1], "alice", "morning", conn=conn)
        assert [row.name for row in user_briefings.list_briefings(install[1], "alice", conn=conn)] == ["new"]
        conn.rollback()
    assert [row.name for row in user_briefings.list_briefings(install[1])] == ["morning"]


def test_content_defaults_preview_and_round_trip(install):
    schedule(install)
    definition = [{"name": "morning", "blocks": [{"title": "Calendar", "sources": [{"kind": "calendar"}]}]}]
    context, operations = preview(install, definition, dry_run=True)
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 2 and not result.restart_required
    assert not module_path(install).exists()
    context, operations = preview(install, definition)
    assert engine.execute_plan(operations, context).exit_code == 2
    with bdb.connect(module_path(install)) as conn:
        block, = bdb.list_blocks(conn, "morning")
        assert (block.title, block.directive, block.render_mode, block.options) == ("Calendar", "", "synthesis", {})
        source, = block.sources
        assert (source.kind, source.config, source.enabled) == ("calendar", {}, True)
    context, operations = preview(install, definition)
    assert engine.execute_plan(operations, context).exit_code == 0


@pytest.fixture
def content(install):
    schedule(install)
    path = module_path(install)
    bdb.init_db(path)
    with bdb.connect(path) as conn:
        first = bdb.add_block(conn, briefing_name="morning", title="First", position=4,
                              directive="Old", options={"old": True})
        bdb.add_source(conn, block_id=first, kind="calendar", config={"old": True}, position=5)
        bdb.add_source(conn, block_id=first, kind="markets", position=8)
        bdb.add_block(conn, briefing_name="morning", title="Tail", position=9)
        bdb.insert_archive(conn, briefing_name="morning", subject="Archive", body_md="Saved")
        conn.commit()
        return bdb.list_blocks(conn, "morning")


def test_positional_prefix_preserves_ids_tails_and_replaces_mappings(install, content):
    definition = [{"name": "morning", "blocks": [{"title": "Changed", "directive": "", "options": {},
                    "sources": [{"config": {}, "enabled": False}]}]}]
    context, operations = preview(install, definition)
    assert len(context.warnings) == 2
    assert len(operations) == 1 and operations[0].section == "briefing_content"
    assert engine.execute_plan(operations, context).exit_code == 2
    with bdb.connect(module_path(install)) as conn:
        first, tail = bdb.list_blocks(conn, "morning")
        assert (first.id, tail.id) == (content[0].id, content[1].id)
        assert (first.title, first.directive, first.options) == ("Changed", "", {})
        assert [source.id for source in first.sources] == [source.id for source in content[0].sources]
        assert first.sources[0].config == {} and not first.sources[0].enabled
        assert first.sources[1] == content[0].sources[1]
        assert tail == content[1]
        assert bdb.count_archive(conn) == 1
    context, operations = preview(install, definition)
    assert engine.execute_plan(operations, context).exit_code == 0
    assert len(context.warnings) == 2


def test_kind_change_append_and_prune_keep_order_and_archives(install, content):
    definition = [{"name": "morning", "blocks": [{"sources": [{"kind": "notes", "config": {"path": "notes.md"}}]},
                    {"title": "Second"}, {"title": "Third", "sources": [{"kind": "todos"}]}]}]
    context, operations = preview(install, definition, prune=True)
    assert engine.execute_plan(operations, context).exit_code == 2
    with bdb.connect(module_path(install)) as conn:
        blocks = bdb.list_blocks(conn, "morning")
        assert [block.title for block in blocks] == ["First", "Second", "Third"]
        assert [block.id for block in blocks[:2]] == [block.id for block in content]
        source, = blocks[0].sources
        assert (source.kind, source.config) == ("notes", {"path": "notes.md"})
        assert blocks[2].sources[0].kind == "todos"
        assert bdb.count_archive(conn) == 1
    context, operations = preview(install, definition, prune=True)
    assert engine.execute_plan(operations, context).exit_code == 0


@pytest.mark.parametrize("blocks,prune,counts,warnings", [
    (None, True, (2, 2), 0), ([], False, (2, 2), 1), ([], True, (0, 0), 0),
    ([{"sources": []}], False, (2, 2), 2), ([{"sources": []}], True, (1, 0), 0),
    ([{"title": "First"}], True, (1, 2), 0),
])
def test_absent_empty_and_pruned_arrays(install, content, blocks, prune, counts, warnings):
    definition = {"name": "morning"}
    if blocks is not None:
        definition["blocks"] = blocks
    context, operations = preview(install, [definition], prune=prune)
    assert len(context.warnings) == warnings
    assert engine.execute_plan(operations, context).exit_code in (0, 2)
    with bdb.connect(module_path(install)) as conn:
        rows = bdb.list_blocks(conn, "morning")
        assert (len(rows), sum(len(block.sources) for block in rows)) == counts
        assert bdb.count_archive(conn) == 1


def test_nullable_directive_is_semantic_noop_and_snapshot_has_defaults(install, content):
    from istota.settings_plan.briefings import snapshot_blocks

    with bdb.connect(module_path(install)) as conn:
        before = snapshot_blocks(conn, "morning", "alice")
        assert before[1] == {"title": "Tail", "directive": "", "render_mode": "synthesis", "options": {}, "sources": []}
    context, operations = preview(install, [{"name": "morning", "blocks": before}], prune=True)
    assert engine.execute_plan(operations, context).exit_code == 0
    with bdb.connect(module_path(install)) as conn:
        assert bdb.list_blocks(conn, "morning") == content


@pytest.mark.parametrize("blocks", [[{}], [{"title": "   "}], [{"title": "New", "sources": [{}]}]])
def test_missing_required_content_fields_refuse_before_schedule_write(install, blocks):
    with pytest.raises(engine.ExecutionError, match="validation_error"):
        preview(install, [{"name": "new", "cron": "0 7 * * *", "output": "email", "blocks": blocks}])
    assert user_briefings.list_briefings(install[1]) == []
    assert not module_path(install).exists()


@pytest.mark.parametrize("column,value", [("options", "broken"), ("options", "[]"), ("render_mode", "unknown")])
def test_invalid_current_content_is_not_replaced_with_empty(install, content, column, value):
    with bdb.connect(module_path(install)) as conn:
        conn.execute(f"UPDATE briefing_blocks SET {column} = ? WHERE id = ?", (value, content[0].id))
        conn.commit()
    with pytest.raises(engine.ExecutionError, match="briefing_store_invalid"):
        preview(install, [{"name": "morning", "blocks": []}], prune=True)


def test_content_transaction_rolls_back_update_and_tail_delete(install, content):
    with bdb.connect(module_path(install)) as conn:
        conn.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON briefing_blocks BEGIN SELECT RAISE(ABORT, 'failure'); END")
        conn.commit()
    context, operations = preview(install, [{"name": "morning", "blocks": [{"title": "Changed"}]}], prune=True)
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 1 and not result.changed
    with bdb.connect(module_path(install)) as conn:
        assert bdb.list_blocks(conn, "morning") == content


def test_content_concurrent_edit_refused_archive_change_allowed(install, content):
    definition = [{"name": "morning", "blocks": [{"title": "Planned"}]}]
    context, operations = preview(install, definition)
    with bdb.connect(module_path(install)) as conn:
        bdb.update_source(conn, content[0].sources[0].id, enabled=False)
        conn.commit()
    result = engine.execute_plan(operations, context)
    assert result.errors == [{"code": "state_changed", "path": ["users", "alice", "briefing_content"]}]
    context, operations = preview(install, definition)
    with bdb.connect(module_path(install)) as conn:
        bdb.insert_archive(conn, briefing_name="morning", subject="New archive", body_md="Saved")
        conn.commit()
    assert engine.execute_plan(operations, context).exit_code == 2


def test_content_report_redacts_structured_credentials(install):
    schedule(install)
    definition = [{"name": "morning", "blocks": [{"title": "Browse", "sources": [
        {"kind": "browse", "config": {"headers": {"Authorization": "private-example"}}}]}]}]
    context, operations = preview(install, definition, dry_run=True)
    from istota.settings_plan.report import result_document
    report = result_document(engine.execute_plan(operations, context))
    assert "private-example" not in json.dumps(report)


def test_missing_content_empty_array_does_not_create_database(install):
    schedule(install)
    context, operations = preview(install, [{"name": "morning", "blocks": []}], prune=True)
    assert engine.execute_plan(operations, context).exit_code == 0
    assert not module_path(install).exists()


def test_content_upsert_and_orphan_prune_share_transaction(install, content):
    with bdb.connect(module_path(install)) as conn:
        bdb.add_block(conn, briefing_name="orphan", title="Orphan")
        conn.execute("CREATE TRIGGER refuse_delete BEFORE DELETE ON briefing_blocks "
                     "WHEN OLD.briefing_name = 'orphan' BEGIN SELECT RAISE(ABORT, 'failure'); END")
        conn.commit()
    context, operations = preview(install, [{"name": "morning", "blocks": [{"title": "Changed"}]}], prune=True)
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 1 and not result.changed
    with bdb.connect(module_path(install)) as conn:
        assert bdb.list_blocks(conn, "morning") == content
        assert bdb.list_briefing_names(conn) == ["morning", "orphan"]


def test_cli_creates_schedule_and_content_then_reapplies_unchanged(install, tmp_path, monkeypatch, capsys):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"users": {"alice": {"briefings": [
        {"name": "morning", "cron": "0 7 * * *", "output": "email", "blocks": [
            {"title": "Calendar", "sources": [{"kind": "calendar"}]}]},
    ]}}}))
    for flags, expected in [(["--dry-run"], 2), ([], 2), ([], 0)]:
        monkeypatch.setattr("sys.argv", ["istota", "-c", str(install[0]), "apply", "-f", str(plan), "--format", "json", *flags])
        code = 0
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
        assert code == expected
        report = json.loads(capsys.readouterr().out)
        assert report["restart_required"] is (expected == 2)
        if flags:
            assert user_briefings.list_briefings(install[1]) == []
            assert not module_path(install).exists()
    assert user_briefings.get_briefing(install[1], "alice", "morning") is not None
    with bdb.connect(module_path(install)) as conn:
        block, = bdb.list_blocks(conn, "morning")
        assert block.sources[0].kind == "calendar"


@pytest.mark.parametrize("output", ["talk:talk-alice", "email,talk:talk-alice"])
def test_explicit_talk_destination_updates_without_redundant_token(install, output):
    with db.get_db(install[1]) as conn:
        db.register_room(conn, "private-alice", "alice", origin="web")
        db.add_room_binding(conn, "private-alice", "talk", "talk-alice")
    row = schedule(install, output=output, conversation_token="")
    declaration = [{"name": "morning", "title": "Updated"}]
    context, operations = preview(install, declaration)
    assert engine.execute_plan(operations, context).exit_code == 2
    stored = user_briefings.get_briefing(install[1], "alice", "morning")
    assert stored.id == row.id and stored.title == "Updated"
    assert stored.output == output and stored.conversation_token == ""
    context, operations = preview(install, declaration)
    assert engine.execute_plan(operations, context).exit_code == 0
    with db.get_db(install[1]) as conn:
        db.add_room_member(conn, "private-alice", "bob")
    with pytest.raises(engine.ExecutionError, match="validation_error"):
        preview(install, declaration)
