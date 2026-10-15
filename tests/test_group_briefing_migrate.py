"""Default retirement preserves effective state and never claims personal content."""

from uuid import uuid4

import pytest

from istota import db, user_briefings, user_profiles
from istota.briefings import db as bdb, group_store as store
from istota.config import load_config
from istota.groups.policy import GroupError
from istota.lib import toml_write


def legacy(tmp_path, monkeypatch, *, fresh=False, private=False):
    monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "admins"))
    (tmp_path / "admins").write_text("alice\n")
    database = tmp_path / "framework.db"
    db.init_db(database)
    for user in ("alice", "bob", "carol"):
        user_profiles.provision_user(database, user, {})
    if not fresh:
        with db.get_db(database) as conn:
            conn.execute("DROP TABLE IF EXISTS group_briefing_migrations")
        db.init_db(database)
    default = {"name": "daily", "title": "Daily", "cron": "0 7 * * *", "output": "email",
               "blocks": [{"title": "News", "render_mode": "structured", "sources": [
                   {"kind": "calendar" if private else "shared_block", "config": {} if private else {"name": "news"}}
               ]}]}
    document = {"db_path": str(database), "workspace_path": str(tmp_path / "workspace"),
                "default_briefings": [default], "briefing_shared_blocks": [], "email": {"enabled": True}}
    path = tmp_path / "config.toml"
    path.write_text(toml_write.dumps(document))
    return path, document


def test_upgrade_materializes_once_and_fresh_accounts_never_seed(tmp_path, monkeypatch):
    path, document = legacy(tmp_path, monkeypatch)
    config = load_config(path)
    assert len(user_briefings.list_briefings(config.db_path)) == 3
    with bdb.connect(config.module_db_path("alice", "briefings")) as conn:
        assert bdb.list_blocks(conn, "daily")[0].sources[0].kind == "shared_block"
    user_profiles.provision_user(config.db_path, "dave", {})
    document["default_briefings"][0]["cron"] = "0 9 * * *"
    path.write_text(toml_write.dumps(document))
    config = load_config(path)
    assert config.users["dave"].briefings == []
    assert user_briefings.get_briefing(config.db_path, "alice", "daily").cron == "0 7 * * *"


def test_fresh_install_retains_only_migration_input(tmp_path, monkeypatch):
    path, _ = legacy(tmp_path, monkeypatch, fresh=True)
    config = load_config(path)
    assert config.default_briefings
    assert all(not user.briefings for user in config.users.values())
    assert user_briefings.list_briefings(config.db_path) == []


def imported(tmp_path, monkeypatch, **options):
    from istota.briefings import group_migrate as migration
    path, document = legacy(tmp_path, monkeypatch, **options)
    config = load_config(path)
    with db.get_db(config.db_path) as conn:
        result = migration.import_default(conn, config, "daily", actor_id="alice", timezone="UTC")
    return config, result, path, document


def payload(definition, **changes):
    return {**{key: definition[key] for key in ("name", "title", "cron", "timezone", "enabled", "channel", "generation_mode", "generation")}, **changes}


def test_import_disabled_idempotent_and_common_enable_gate(tmp_path, monkeypatch):
    from istota.briefings import group_migrate as migration
    config, result, _, _ = imported(tmp_path, monkeypatch)
    with db.get_db(config.db_path) as conn:
        definition = store.get_definition(conn, result["definition_id"])
        assert not definition["enabled"]
        with pytest.raises(GroupError, match="migration_cutover_required"):
            store.save_definition(conn, config, definition["group_id"], payload(definition, enabled=True),
                                  actor_id="alice", is_admin=True, expected_revision=1, definition_id=definition["id"])
        updated = store.save_definition(conn, config, definition["group_id"], payload(definition, title="DB edit"),
                                        actor_id="alice", is_admin=True, expected_revision=1, definition_id=definition["id"])
        migration.import_default(conn, config, "daily", actor_id="alice", timezone="UTC")
        assert store.get_definition(conn, definition["id"])["title"] == updated["title"]


def test_cutover_cancels_queued_cached_config_and_rollback(tmp_path, monkeypatch):
    from istota.briefings import group_migrate as migration
    from istota.scheduler import _create_briefing_task
    config, result, path, _ = imported(tmp_path, monkeypatch)
    with bdb.connect(config.module_db_path("alice", "briefings")) as content:
        bdb.insert_archive(content, briefing_name="daily", subject="Personal edition", body_md="Private personal history")
        content.commit()
        archive_before = [tuple(row) for row in content.execute("SELECT * FROM briefing_archive")]
    with db.get_db(config.db_path) as conn:
        task_id = _create_briefing_task(conn, "alice", config.users["alice"].briefings[0])
    with db.get_db(config.db_path) as conn:
        migration.review_default(conn, config, "daily", actor_id="alice", expected_revision=1, timezone="UTC", dispositions={})
        migration.cutover_default(conn, config, "daily", actor_id="alice", expected_revision=1)
        assert db.get_task(conn, task_id).status == "cancelled"
        assert store.get_definition(conn, result["definition_id"])["enabled"]
        with pytest.raises(ValueError, match="briefing_retired"):
            _create_briefing_task(conn, "alice", config.users["alice"].briefings[0])
        assert not user_briefings.get_briefing(config.db_path, "alice", "daily", conn=conn).enabled
    config = load_config(path)
    assert config.users["alice"].briefings == []
    with db.get_db(config.db_path) as conn:
        migration.rollback_default(conn, config, "daily", actor_id="alice", expected_revision=2)
        assert not store.get_definition(conn, result["definition_id"])["enabled"]
        assert user_briefings.get_briefing(config.db_path, "alice", "daily", conn=conn).enabled
        assert db.get_task(conn, task_id).status == "cancelled"
        assert conn.execute("SELECT COUNT(*) FROM group_briefing_runs").fetchone()[0] == 0
    with bdb.connect(config.module_db_path("alice", "briefings")) as content:
        assert [tuple(row) for row in content.execute("SELECT * FROM briefing_archive")] == archive_before


def test_running_legacy_task_refuses_cutover_without_partial_changes(tmp_path, monkeypatch):
    from istota.briefings import group_migrate as migration
    config, result, _, _ = imported(tmp_path, monkeypatch)
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(conn, "legacy", "alice", source_type="briefing", briefing_name="daily")
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
    with db.get_db(config.db_path) as conn:
        migration.review_default(conn, config, "daily", actor_id="alice", expected_revision=1, timezone="UTC", dispositions={})
        with pytest.raises(GroupError, match="legacy_tasks_inflight"):
            migration.cutover_default(conn, config, "daily", actor_id="alice", expected_revision=1)
        assert not store.get_definition(conn, result["definition_id"])["enabled"]
        assert user_briefings.get_briefing(config.db_path, "alice", "daily", conn=conn).enabled


def test_private_import_requires_explicit_source_review(tmp_path, monkeypatch):
    from istota.briefings import group_migrate as migration
    config, result, _, _ = imported(tmp_path, monkeypatch, private=True)
    assert result["source_issues"]
    with db.get_db(config.db_path) as conn:
        with pytest.raises(GroupError, match="migration_sources_unresolved"):
            store.enqueue_run(conn, config, result["definition_id"], actor_id="alice", is_admin=True,
                              request_id=str(uuid4()), mode="preview")
        with pytest.raises(GroupError, match="migration_sources_unresolved"):
            migration.review_default(conn, config, "daily", actor_id="alice", expected_revision=1, timezone="UTC", dispositions={})


def test_legacy_authoring_receives_migration_diagnostics(tmp_path, monkeypatch):
    from istota.config_mapper import collect_problems
    from istota.config_editor.schema import READ_ONLY
    from istota.settings_plan.schema import SchemaError, parse_plan
    path, _ = legacy(tmp_path, monkeypatch, fresh=True)
    with collect_problems() as problems:
        load_config(path, apply_db_overlays=False)
    assert any(problem['kind'] == 'retired' and problem['path'] == ('default_briefings',) for problem in problems)
    assert 'default_briefings' in READ_ONLY[("default_briefings",)]
    with pytest.raises(SchemaError, match='migration'):
        parse_plan({'users': {'alice': {'profile': {'default_briefings': False}}}})


def test_ambiguous_copies_disabled_preferences_and_variants(tmp_path, monkeypatch):
    from istota.briefings import group_migrate as migration
    path, document = legacy(tmp_path, monkeypatch)
    # A pre-existing row matching by name/content has no trustworthy origin marker.
    database = document['db_path']
    user_briefings.ensure_briefing(database, user_id='bob', name='daily', cron='0 7 * * *', title='Daily', output='email')
    user_briefings.ensure_briefing(database, user_id='carol', name='daily', cron='0 9 * * *', output='email', enabled=False)
    with db.get_db(database) as conn:
        conn.execute("UPDATE user_profiles SET default_briefings=0 WHERE user_id='bob'")
    config = load_config(path)
    with db.get_db(database) as conn:
        imported = migration.import_default(conn, config, 'daily', actor_id='alice', timezone='UTC')
        candidates = {item['user_id']: item for item in imported['candidates']}
        assert candidates['alice']['proposed'] == 'retire'
        assert candidates['bob']['proposed'] == candidates['carol']['proposed'] == 'unresolved'
        with pytest.raises(GroupError, match='migration_candidate_unresolved'):
            migration.review_default(conn, config, 'daily', actor_id='alice', expected_revision=1, timezone='UTC', dispositions={})
        definition = store.get_definition(conn, imported['definition_id'])
        assert store.delivery_preferences(conn, definition['group_id'], 'bob')['outbound_muted']
        assert store.delivery_preferences(conn, definition['group_id'], 'carol', definition_id=definition['id'])['in_app_muted']
        migration.review_default(conn, config, 'daily', actor_id='alice', expected_revision=1, timezone='UTC', dispositions={'bob': 'keep', 'carol': 'mute'})
        migration.cutover_default(conn, config, 'daily', actor_id='alice', expected_revision=1)
        assert user_briefings.get_briefing(database, 'bob', 'daily', conn=conn).enabled
        assert not user_briefings.get_briefing(database, 'carol', 'daily', conn=conn).enabled


def test_review_rechecks_variants_timezone_and_revision(tmp_path, monkeypatch):
    from istota.briefings import group_migrate as migration
    config, result, _, _ = imported(tmp_path, monkeypatch)
    with db.get_db(config.db_path) as conn:
        with pytest.raises(GroupError, match='migration_timezone_review_required'):
            migration.review_default(conn, config, 'daily', actor_id='alice', expected_revision=1, timezone='Europe/London', dispositions={})
        migration.review_default(conn, config, 'daily', actor_id='alice', expected_revision=1, timezone='UTC', dispositions={})
    user_briefings.ensure_briefing(config.db_path, user_id='bob', name='daily', cron='0 9 * * *', output='email')
    with db.get_db(config.db_path) as conn:
        with pytest.raises(GroupError, match='migration_candidates_changed'):
            migration.cutover_default(conn, config, 'daily', actor_id='alice', expected_revision=1)
        definition = store.get_definition(conn, result['definition_id'])
        store.save_definition(conn, config, definition['group_id'], payload(definition, title='Changed'), actor_id='alice', is_admin=True,
                              expected_revision=1, definition_id=definition['id'])
        with pytest.raises(GroupError, match='migration_review_required'):
            migration.cutover_default(conn, config, 'daily', actor_id='alice', expected_revision=2)


def test_completed_transition_occurrence_is_never_regenerated(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from istota.briefings import group_migrate as migration
    config, result, path, _ = imported(tmp_path, monkeypatch)
    now = datetime(2026, 1, 2, 7, 1, tzinfo=timezone.utc)
    with db.get_db(config.db_path) as conn:
        task = db.create_task(conn, 'legacy', 'alice', source_type='briefing', briefing_name='daily')
        conn.execute("UPDATE tasks SET status='completed' WHERE id=?", (task,))
    with db.get_db(config.db_path) as conn:
        migration.review_default(conn, config, 'daily', actor_id='alice', expected_revision=1, timezone='UTC', dispositions={})
        result = migration.cutover_default(conn, config, 'daily', actor_id='alice', expected_revision=1, now=now)
        assert result['outcomes'] == [{'task_id': task, 'before': 'completed', 'outcome': 'completed'}]
    db.init_db(config.db_path)
    config = load_config(path)
    with db.get_db(config.db_path) as conn:
        with pytest.raises(GroupError, match='migration_transition_occurrence'):
            store.enqueue_run(conn, config, result['definition_id'], due_at=now - timedelta(minutes=1), now=now)
        run = store.enqueue_run(conn, config, result['definition_id'], due_at=now + timedelta(days=1), now=now)
        assert run['state'] == 'queued'


def test_preservation_restarts_after_module_commit_without_resetting_content(tmp_path, monkeypatch):
    from istota.briefings import group_migrate as migration
    path, _ = legacy(tmp_path, monkeypatch)
    real = migration._write
    def fail_checkpoint(conn, key, state, data, definition_id=None):
        if key == 'default:daily' and data.get('users', {}).get('bob', {}).get('preserved'):
            raise OSError('simulated interruption')
        return real(conn, key, state, data, definition_id)
    monkeypatch.setattr(migration, '_write', fail_checkpoint)
    with pytest.raises(OSError, match='simulated interruption'):
        load_config(path)
    monkeypatch.setattr(migration, '_write', real)
    config = load_config(path)
    assert len(user_briefings.list_briefings(config.db_path)) == 3
    for user in ('alice', 'bob', 'carol'):
        with bdb.connect(config.module_db_path(user, 'briefings')) as conn:
            assert len(bdb.list_blocks(conn, 'daily')) == 1
    with bdb.connect(config.module_db_path('alice', 'briefings')) as conn:
        conn.execute("UPDATE briefing_blocks SET title='Personal edit'")
        conn.commit()
    config = load_config(path)
    with db.get_db(config.db_path) as conn:
        report = migration.migration_report(conn, config, 'daily')[0]
        assert report['candidates'][0]['proposed'] == 'unresolved'
    with bdb.connect(config.module_db_path('alice', 'briefings')) as conn:
        assert bdb.list_blocks(conn, 'daily')[0].title == 'Personal edit'


def test_legacy_optout_can_be_unmuted_and_reload_does_not_reassert_it(tmp_path, monkeypatch):
    from istota.briefings import group_migrate as migration
    path, document = legacy(tmp_path, monkeypatch)
    with db.get_db(document['db_path']) as conn:
        conn.execute("UPDATE user_profiles SET default_briefings=0 WHERE user_id='bob'")
    config = load_config(path)
    with db.get_db(config.db_path) as conn:
        result = migration.import_default(conn, config, 'daily', actor_id='alice', timezone='UTC')
        definition = store.get_definition(conn, result['definition_id'])
        assert store.delivery_preferences(conn, definition['group_id'], 'bob')['outbound_muted']
        store.set_delivery_preferences(conn, config, definition['group_id'], actor_id='bob', outbound_muted=False, in_app_muted=False)
        migration.review_default(conn, config, 'daily', actor_id='alice', expected_revision=1, timezone='UTC', dispositions={'bob': 'retire'})
        migration.cutover_default(conn, config, 'daily', actor_id='alice', expected_revision=1)
    config = load_config(path)
    with db.get_db(config.db_path) as conn:
        migration.import_default(conn, config, 'daily', actor_id='alice', timezone='UTC')
        assert not store.delivery_preferences(conn, definition['group_id'], 'bob')['outbound_muted']
        run = store.enqueue_run(conn, config, definition['id'], actor_id='alice', is_admin=True, request_id=str(uuid4()))
        claim = store.claim_run(conn, run_id=run['id'])
        store.publish_run(conn, config, run['id'], claim['attempt_token'], subject='Common', body_md='Edition')
        recipient = next(row for row in store.list_recipients(conn, run['id']) if row['user_id'] == 'bob' and row['channel'] == 'email')
        assert store.delivery_refusal(conn, config, recipient) is None


def test_cli_import_review_cutover_and_apply_cannot_bypass(tmp_path, monkeypatch, capsys):
    import json
    from istota import cli_briefings
    from istota.settings_plan import engine
    from istota.settings_plan.schema import parse_plan
    path, _ = legacy(tmp_path, monkeypatch)
    config = load_config(path)
    args = ['migrate', 'import', '--name', 'daily', '--actor', 'alice', '--timezone', 'UTC']
    assert cli_briefings.dispatch(args, config) == 0
    result = json.loads(capsys.readouterr().out)
    with db.get_db(config.db_path) as conn:
        definition = store.get_definition(conn, result['definition_id'])
    plan = parse_plan({'group_briefings': [{'group_id': definition['group_id'], 'name': 'daily', 'enabled': True}]})
    with pytest.raises(engine.ExecutionError, match='migration_cutover_required'):
        engine.build_plan(plan, engine.Context(path), engine.Options(actor='alice'))
    assert cli_briefings.dispatch(['migrate', 'review', '--name', 'daily', '--actor', 'alice', '--timezone', 'UTC', '--revision', '1'], config) == 0
    capsys.readouterr()
    assert cli_briefings.dispatch(['migrate', 'cutover', '--name', 'daily', '--actor', 'alice', '--revision', '1'], config) == 0
    capsys.readouterr()
    assert cli_briefings.dispatch(['migrate', 'rollback', '--name', 'daily', '--actor', 'bob', '--revision', '2'], config) == 1
    assert 'admin' in capsys.readouterr().err


def test_imported_contribution_still_requires_owners_grant(tmp_path, monkeypatch):
    from istota.briefings import group_migrate as migration
    config, result, _, _ = imported(tmp_path, monkeypatch, private=True)
    with db.get_db(config.db_path) as conn:
        definition = store.get_definition(conn, result['definition_id'])
        generation = {'version': 1, 'prompt': 'Summarize the selected contribution.', 'research': {'enabled': False},
                      'inputs': [{'id': 'notes', 'kind': 'personal_file', 'config': {'owner_id': 'bob', 'path': 'notes.md'}}]}
        store.save_definition(conn, config, definition['group_id'], payload(definition, generation_mode='prompt', generation=generation),
                              actor_id='alice', is_admin=True, expected_revision=1, definition_id=definition['id'])
        with pytest.raises(GroupError, match='approval'):
            migration.review_default(conn, config, 'daily', actor_id='alice', expected_revision=2, timezone='UTC',
                                     dispositions={}, accept_source_changes=True)
        assert conn.execute('SELECT COUNT(*) FROM group_briefing_source_grants').fetchone()[0] == 0
        assert migration.migration_report(conn, config, 'daily')[0]['state'] == 'imported'


def test_explicit_identical_override_is_not_auto_retired(tmp_path, monkeypatch):
    from istota.briefings import group_migrate as migration
    path, document = legacy(tmp_path, monkeypatch)
    document['users'] = {'alice': {'briefings': document['default_briefings']}}
    path.write_text(toml_write.dumps(document))
    config = load_config(path)
    with db.get_db(config.db_path) as conn:
        report = migration.migration_report(conn, config, 'daily')[0]
        alice = report['candidates'][0]
        assert alice['unchanged'] and alice['explicit_override']
        assert alice['proposed'] == 'unresolved'


def test_cutover_reenable_of_personal_schedule_is_refused(tmp_path, monkeypatch):
    from istota.briefings import group_migrate as migration
    config, _, _, _ = imported(tmp_path, monkeypatch)
    with db.get_db(config.db_path) as conn:
        migration.review_default(conn, config, 'daily', actor_id='alice', expected_revision=1, timezone='UTC', dispositions={})
        migration.cutover_default(conn, config, 'daily', actor_id='alice', expected_revision=1)
    with pytest.raises(ValueError, match='briefing_retired'):
        user_briefings.ensure_briefing(config.db_path, user_id='alice', name='daily', cron='0 7 * * *', output='email')
