"""File definitions reconcile through workspace writes and the scheduler loader."""

import pytest

from istota import cron_loader, db, user_profiles
from istota.config import load_config
from istota.lib import toml_write
from istota.settings_plan import engine
from istota.settings_plan.schema import parse_plan
from istota.storage import resolve_user_config_dir


@pytest.fixture
def install(tmp_path, monkeypatch):
    admins = tmp_path / "admins"
    admins.write_text("alice\n")
    monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(admins))
    database = tmp_path / "framework.db"
    db.init_db(database)
    config_path = tmp_path / "config.toml"
    config_path.write_text(toml_write.dumps({
        "db_path": str(database), "workspace_path": str(tmp_path / "workspace"),
        "temp_dir": str(tmp_path / "temp"),
    }))
    user_profiles.provision_user(database, "alice", {})
    config = load_config(config_path, apply_db_overlays=False)
    cron = resolve_user_config_dir(config, "alice") / "CRON.md"
    cron.parent.mkdir(parents=True)
    return config_path, config, cron


def preview(install, jobs, **options):
    context = engine.Context(install[0])
    plan = parse_plan({"users": {"alice": {"scheduled_jobs": jobs}}})
    operations = engine.build_plan(plan, context, engine.Options(**options))
    return context, operations


def seed(install):
    install[2].write_text('Before\n```toml\nlabel = "keep"\n'
                          '[[jobs]]\nname = "daily"\ncron = "0 9 * * *"\nprompt = "Review"\n'
                          '[[jobs]]\nname = "other"\ncron = "0 8 * * *"\nprompt = "Other"\n'
                          '```\nAfter\n')


def test_merge_keeps_prose_and_unrelated_toml_without_syncing(install):
    seed(install)
    before_db = install[1].db_path.read_bytes()
    context, operations = preview(install, [{"name": "daily", "target": "email"}])
    assert [(op.identity, op.action) for op in operations] == [("daily", "update")]
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 2 and not result.restart_required
    assert install[1].db_path.read_bytes() == before_db
    content = install[2].read_text()
    assert content.startswith('Before\n```toml\n') and content.endswith('```\nAfter\n')
    doc = cron_loader.parse_cron_document(content, context.config, "alice")
    import tomllib
    assert tomllib.loads(doc.block)["label"] == "keep"
    assert [(job.name, job.prompt, job.target) for job in doc.jobs] == [
        ("daily", "Review", "email"), ("other", "Other", "")]
    context, operations = preview(install, [{"name": "daily", "target": "email"}])
    assert engine.execute_plan(operations, context).exit_code == 0
    assert install[2].read_text() == content


def test_real_sync_preserves_suspension_and_module_jobs(install):
    seed(install)
    config = install[1]
    with db.get_db(config.db_path) as conn:
        cron_loader.sync_cron_jobs_to_db(conn, "alice", cron_loader.load_cron_jobs(config, "alice"))
        conn.execute("UPDATE scheduled_jobs SET auto_disabled_at = '2026-01-01', consecutive_failures = 5 WHERE name = 'daily'")
        conn.execute("INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt) VALUES (?, ?, ?, ?)",
                     ("alice", "_module.example", "0 8 * * *", "Module"))
    for declaration, suspended in [({"target": "email"}, True), ({"prompt": "Fixed"}, False)]:
        context, operations = preview(install, [{"name": "daily", **declaration}], prune=True)
        assert engine.execute_plan(operations, context).exit_code == 2
        with db.get_db(config.db_path) as conn:
            cron_loader.sync_cron_jobs_to_db(conn, "alice", cron_loader.load_cron_jobs(config, "alice"))
            jobs = {job.name: job for job in db.get_user_scheduled_jobs(conn, "alice")}
        assert set(jobs) == {"daily", "_module.example"}
        assert bool(jobs["daily"].auto_disabled_at) is suspended
        assert jobs["daily"].consecutive_failures == (5 if suspended else 0)


@pytest.mark.parametrize("template", [None, "Notes\n", "Notes\n```toml\n# Example\n```\n"])
def test_empty_missing_or_template_is_noop_even_with_prune(install, template):
    if template is not None:
        install[2].write_text(template)
    context, operations = preview(install, [], prune=True)
    assert engine.execute_plan(operations, context).exit_code == 0
    assert install[2].exists() is (template is not None)
    if template is not None:
        assert install[2].read_text() == template


def test_empty_prune_writes_explicit_empty_array_and_sync_deletes_only_file_jobs(install):
    seed(install)
    with db.get_db(install[1].db_path) as conn:
        cron_loader.sync_cron_jobs_to_db(conn, "alice", cron_loader.load_cron_jobs(install[1], "alice"))
    original = install[2].read_bytes()
    context, operations = preview(install, [])
    assert engine.execute_plan(operations, context).exit_code == 0
    assert install[2].read_bytes() == original
    context, operations = preview(install, [], prune=True)
    assert engine.execute_plan(operations, context).exit_code == 2
    assert "jobs = []" in install[2].read_text()
    doc = cron_loader.load_cron_document(install[1], "alice")
    assert not doc.is_template and doc.states_no_jobs
    with db.get_db(install[1].db_path) as conn:
        cron_loader.sync_cron_jobs_to_db(conn, "alice", doc.jobs)
        assert db.get_user_scheduled_jobs(conn, "alice") == []


def test_stale_revision_refuses_write(install):
    seed(install)
    context, operations = preview(install, [{"name": "daily", "prompt": "Planned"}])
    install[2].write_text(install[2].read_text() + "Concurrent notes\n")
    current = install[2].read_bytes()
    result = engine.execute_plan(operations, context)
    assert result.errors == [{"code": "state_changed", "path": ["users", "alice", "scheduled_jobs"]}]
    assert install[2].read_bytes() == current


@pytest.mark.parametrize("block", [
    '[[jobs]]\nname = "daily"\ncron = "0 9 * * *"\nprompt = "Review"\nunknown = "private-placeholder"\n',
    '[[jobs]]\nname = "daily"\ncron = "0 9 * * *"\n',
    '[[jobs]]\nname = "daily"\ncron = "0 9 * * *"\nprompt = "Review"\nenabled = "false"\n',
    '[[jobs]]\nname = "daily"\ncron = "0 9 * * *"\nprompt = "Review"\n' * 2,
    'jobs = [1]\n', 'broken = [\n',
])
def test_uninterpretable_current_definitions_are_never_erased(install, block):
    install[2].write_text("```toml\n" + block + "```\n")
    before = install[2].read_bytes()
    with pytest.raises(engine.ExecutionError, match="cron_file_invalid") as error:
        preview(install, [], prune=True)
    assert "private-placeholder" not in str(error.value)
    assert install[2].read_bytes() == before


@pytest.mark.parametrize("fields", [
    {"cron": "invalid"}, {"target": "unknown"}, {"target": "group:private,email"},
    {"room": "private-bob"}, {"target": "web:private-bob"}, {"brain": "native"},
    {"publish_shared_kv": "namespace/"}, {"command": "echo hello"},
])
def test_invalid_desired_job_refuses_entire_plan(install, fields):
    with db.get_db(install[1].db_path) as conn:
        db.register_room(conn, "private-bob", "bob", origin="web")
    context = engine.Context(install[0])
    plan = parse_plan({"users": {"alice": {"profile": {"display_name": "Changed"},
                       "scheduled_jobs": [{"name": "daily", "cron": "0 9 * * *", "prompt": "Review", **fields}]}}})
    with pytest.raises(engine.ExecutionError):
        engine.build_plan(plan, context, engine.Options())
    assert not install[2].exists()
    assert user_profiles.get_profile(install[1].db_path, "alice").display_name == "alice"


@pytest.mark.parametrize("fields", [
    {"command": "echo hello"}, {"prompt": "Review", "brain": "native"},
    {"prompt": "Review", "publish_shared_kv": "daily"},
])
def test_user_authority_is_not_the_operator_authority(install, fields, monkeypatch, tmp_path):
    admins = tmp_path / "other-admins"
    admins.write_text("bob\n")
    monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(admins))
    with pytest.raises(engine.ExecutionError):
        preview(install, [{"name": "daily", "cron": "0 9 * * *", **fields}])
    assert not install[2].exists()


def test_prompt_file_snapshot_keeps_reference_and_explicit_defaults(install):
    from istota.settings_plan.files import snapshot_cron
    from istota.settings_plan.schema import JOB_FIELDS
    from istota.workspace import text_files

    prompt = install[2].parent / "review.txt"
    prompt.write_text("Private prompt content")
    reference = "/Users/alice/istota/config/review.txt"
    context, operations = preview(install, [{"name": "daily", "cron": "0 9 * * *", "prompt_file": reference}])
    assert engine.execute_plan(operations, context).exit_code == 2
    with db.get_db(install[1].db_path) as conn:
        definitions, revision = snapshot_cron(conn, context.config, "alice")
    assert set(definitions[0]) == set(JOB_FIELDS)
    assert definitions[0]["prompt_file"] == reference and definitions[0]["prompt"] == ""
    assert "Private prompt content" not in repr(definitions)
    assert revision == text_files.revision(install[2].read_text())
    context, operations = preview(install, definitions)
    assert engine.execute_plan(operations, context).exit_code == 0


def test_snapshot_refuses_pending_migration_but_ignores_module_rows(install):
    from istota.settings_plan.files import snapshot_cron

    with db.get_db(install[1].db_path) as conn:
        conn.execute("INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt) VALUES (?, ?, ?, ?)",
                     ("alice", "_module.example", "0 8 * * *", "Module"))
        assert snapshot_cron(conn, install[1], "alice")[0] == []
        conn.execute("INSERT INTO scheduled_jobs (user_id, name, cron_expression, prompt) VALUES (?, ?, ?, ?)",
                     ("alice", "legacy", "0 8 * * *", "Legacy"))
        with pytest.raises(engine.ExecutionError, match="cron_file_migration_required"):
            snapshot_cron(conn, install[1], "alice")
    with pytest.raises(engine.ExecutionError, match="cron_file_migration_required"):
        preview(install, [], prune=True)
    assert not install[2].exists()


def test_cli_dry_run_and_create_preserve_template_prose(install, tmp_path, monkeypatch, capsys):
    from istota import cli
    import json

    template = "Keep these notes.\n"
    install[2].write_text(template)
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"users": {"alice": {"scheduled_jobs": [
        {"name": "daily", "cron": "0 9 * * *", "command": "istota-skill todos list", "target": "none"},
    ]}}}))
    for flags, expected in [(["--dry-run"], 2), ([], 2), ([], 0)]:
        monkeypatch.setattr("sys.argv", ["istota", "-c", str(install[0]), "apply", "-f", str(plan), "--format", "json", *flags])
        code = 0
        try:
            cli.main()
        except SystemExit as result:
            code = result.code
        assert code == expected
        report = json.loads(capsys.readouterr().out)
        assert report["restart_required"] is False
        if flags:
            assert install[2].read_text() == template
    assert install[2].read_text().startswith(template)


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_non_regular_files_refused(install, tmp_path, kind):
    import os

    if kind == "symlink":
        other = tmp_path / "other.md"
        other.write_text("Keep")
        install[2].symlink_to(other)
    else:
        os.mkfifo(install[2])
    with pytest.raises(engine.ExecutionError, match="file_unavailable"):
        preview(install, [])


def test_revoked_room_is_rechecked_before_write(install):
    with db.get_db(install[1].db_path) as conn:
        db.register_room(conn, "private-alice", "alice", origin="web")
    context, operations = preview(install, [{"name": "daily", "cron": "0 9 * * *", "prompt": "Review",
                                           "room": "private-alice", "target": "web:private-alice"}])
    with db.get_db(install[1].db_path) as conn:
        db.add_room_member(conn, "private-alice", "bob")
    result = engine.execute_plan(operations, context)
    assert result.errors == [{"code": "state_changed", "path": ["users", "alice", "scheduled_jobs"]}]
    assert not install[2].exists()


def test_file_lock_timeout_is_named_and_writes_nothing(install, monkeypatch):
    from functools import partial
    from istota.workspace import text_files

    context, operations = preview(install, [{"name": "daily", "cron": "0 9 * * *", "prompt": "Review"}])
    lock = text_files.file_lock
    with lock(context.config, "alice", install[2]):
        monkeypatch.setattr(text_files, "file_lock", partial(lock, timeout_seconds=0.01))
        result = engine.execute_plan(operations, context)
    assert result.errors == [{"code": "lock_timeout", "path": ["users", "alice", "scheduled_jobs"]}]
    assert not install[2].exists()


def test_nonadmin_skill_command_and_new_user_directory(install, monkeypatch, tmp_path):
    admins = tmp_path / "other-admins"
    admins.write_text("bob\n")
    monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(admins))
    install[2].parent.rmdir()
    context, operations = preview(install, [{"name": "daily", "cron": "0 9 * * *", "command": "istota-skill todos list"}])
    assert not install[2].parent.exists()
    assert engine.execute_plan(operations, context).exit_code == 2
    assert cron_loader.load_cron_jobs(context.config, "alice")[0].command == "istota-skill todos list"


def test_failed_atomic_writer_reports_no_applied_operation(install, monkeypatch):
    from istota import storage

    seed(install)
    before = install[2].read_bytes()
    context, operations = preview(install, [{"name": "daily", "prompt": "Updated"}])
    monkeypatch.setattr(storage, "write_regular_file", lambda path, content: False)
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 1 and not result.changed
    assert result.errors == [{"code": "file_write_failed", "path": ["users", "alice", "scheduled_jobs"]}]
    assert install[2].read_bytes() == before


# Heartbeat definitions share the same file unit as scheduled jobs.
def heartbeat_preview(install, declaration, **options):
    context = engine.Context(install[0])
    plan = parse_plan({"users": {"alice": {"heartbeat": declaration}}})
    return context, engine.build_plan(plan, context, engine.Options(**options))


def heartbeat_seed(install):
    path = install[2].with_name("HEARTBEAT.md")
    path.write_text('Before\n```toml\nlabel = "keep"\n'
                    '[settings]\nquiet_hours = ["22:00-07:00"]\ndefault_cooldown_minutes = 90\n'
                    '[[checks]]\nname = "inbox"\ntype = "file-watch"\npath = "/Users/alice/inbox"\n'
                    'cooldown_minutes = 15\ninterval_minutes = 10\n'
                    '[[checks]]\nname = "other"\ntype = "self-check"\n```\nAfter\n')
    return path


def test_heartbeat_merge_real_loader_and_runtime_preserved(install):
    from istota import heartbeat
    import tomllib

    path = heartbeat_seed(install)
    with db.get_db(install[1].db_path) as conn:
        conn.execute("INSERT INTO heartbeat_state (user_id, check_name, consecutive_errors) VALUES (?, ?, ?)",
                     ("alice", "inbox", 4))
    before_db = install[1].db_path.read_bytes()
    context, operations = heartbeat_preview(install, {
        "settings": {"quiet_hours": []},
        "checks": [{"name": "inbox", "channel": "email", "reset": ["cooldown_minutes"]}],
    })
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 2 and not result.restart_required
    assert install[1].db_path.read_bytes() == before_db
    content = path.read_text()
    assert content.startswith('Before\n```toml\n') and content.endswith('```\nAfter\n')
    assert 'reset' not in content
    assert tomllib.loads(content.split('```toml\n')[1].split('```')[0])["label"] == "keep"
    settings, checks = heartbeat.load_heartbeat_config(context.config, "alice")
    assert settings.quiet_hours == [] and settings.default_cooldown_minutes == 90
    assert [(c.name, c.channel, c.cooldown_minutes, c.interval_minutes) for c in checks] == [
        ("inbox", "email", None, 10), ("other", "", None, None)]
    context, operations = heartbeat_preview(install, {"checks": [{"name": "inbox", "channel": "email"}]})
    assert engine.execute_plan(operations, context).exit_code == 0
    assert path.read_text() == content


@pytest.mark.parametrize("declaration,prune,names", [
    ({"settings": {}}, True, ["inbox", "other"]),
    ({"checks": []}, False, ["inbox", "other"]),
    ({"checks": []}, True, []),
])
def test_heartbeat_presence_and_empty_collections(install, declaration, prune, names):
    from istota.settings_plan.files import snapshot_heartbeat

    path = heartbeat_seed(install)
    context, operations = heartbeat_preview(install, declaration, prune=prune)
    assert engine.execute_plan(operations, context).exit_code == (2 if not names else 0)
    definitions, _ = snapshot_heartbeat(context.config, "alice")
    assert [c["name"] for c in definitions["checks"]] == names
    assert definitions["settings"]["default_cooldown_minutes"] == 90
    if not names:
        assert 'checks = []' in path.read_text()


def test_heartbeat_snapshot_clears_inherited_overrides_and_is_idempotent(install):
    from istota.settings_plan.files import snapshot_heartbeat
    from istota import heartbeat
    from istota.workspace import text_files

    path = heartbeat_seed(install)
    source = path.read_text().replace('cooldown_minutes = 15\ninterval_minutes = 10\n', '')
    path.write_text(source)
    definitions, revision = snapshot_heartbeat(install[1], "alice")
    assert revision == text_files.revision(source)
    assert definitions["checks"][0]["reset"] == ["cooldown_minutes", "interval_minutes", "max_age_hours"]
    assert definitions["checks"][0]["channel"] == ""
    context, operations = heartbeat_preview(install, definitions)
    assert engine.execute_plan(operations, context).exit_code == 0
    heartbeat_seed(install)
    context, operations = heartbeat_preview(install, definitions)
    assert engine.execute_plan(operations, context).exit_code == 2
    _, checks = heartbeat.load_heartbeat_config(context.config, "alice")
    assert checks[0].cooldown_minutes is None and checks[0].interval_minutes is None


@pytest.mark.parametrize("template", [None, "Notes\n", "Notes\n```toml\n# Example\n```\n"])
def test_heartbeat_empty_template_and_settings_only(install, template):
    from istota.settings_plan.files import snapshot_heartbeat

    path = install[2].with_name("HEARTBEAT.md")
    if template is not None:
        path.write_text(template)
    context, operations = heartbeat_preview(install, {"settings": {}, "checks": []}, prune=True)
    assert engine.execute_plan(operations, context).exit_code == 0
    assert path.exists() is (template is not None)
    context, operations = heartbeat_preview(install, {"settings": {"default_cooldown_minutes": 120}})
    assert engine.execute_plan(operations, context).exit_code == 2
    assert snapshot_heartbeat(context.config, "alice")[0]["settings"]["default_cooldown_minutes"] == 120
    if template:
        assert path.read_text().startswith("Notes\n")


def test_heartbeat_stale_revision_and_runtime_change(install):
    path = heartbeat_seed(install)
    context, operations = heartbeat_preview(install, {"checks": [{"name": "inbox", "channel": "email"}]})
    path.write_text(path.read_text() + "Concurrent notes\n")
    before = path.read_bytes()
    result = engine.execute_plan(operations, context)
    assert result.errors == [{"code": "state_changed", "path": ["users", "alice", "heartbeat"]}]
    assert path.read_bytes() == before
    context, operations = heartbeat_preview(install, {"checks": []}, prune=True)
    with db.get_db(install[1].db_path) as conn:
        conn.execute("INSERT INTO heartbeat_state (user_id, check_name, consecutive_errors) VALUES (?, ?, ?)",
                     ("alice", "inbox", 5))
    assert engine.execute_plan(operations, context).exit_code == 2
    with db.get_db(install[1].db_path) as conn:
        assert conn.execute("SELECT consecutive_errors FROM heartbeat_state").fetchone()[0] == 5


@pytest.mark.parametrize("block", [
    '[[checks]]\nname = "bad"\n',
    '[[checks]]\nname = "bad"\ntype = "unknown"\n',
    '[[checks]]\nname = "bad"\ntype = "self-check"\nunknown = "private-placeholder"\n',
    '[[checks]]\nname = "bad"\ntype = "self-check"\n' * 2,
    'checks = [1]\n', 'broken = [\n',
    '[settings]\nunknown = "private-placeholder"\n',
])
def test_heartbeat_invalid_current_file_is_not_erased(install, block, caplog):
    path = install[2].with_name("HEARTBEAT.md")
    path.write_text("```toml\n" + block + "```\n")
    before = path.read_bytes()
    with pytest.raises(engine.ExecutionError, match="heartbeat_file_invalid") as error:
        heartbeat_preview(install, {"checks": []}, prune=True)
    assert path.read_bytes() == before
    assert "private-placeholder" not in str(error.value) + caplog.text


def test_heartbeat_new_checks_validate_without_running(install, monkeypatch):
    from istota import heartbeat

    def unexpected(*args, **kwargs):
        pytest.fail("preview must not execute checks")
    monkeypatch.setattr(heartbeat, "run_check", unexpected)
    monkeypatch.setattr(heartbeat.httpx, "get", unexpected)
    context, operations = heartbeat_preview(install, {"checks": [
        {"name": "web", "type": "url-health", "url": "https://example.com"},
        {"name": "shell", "type": "shell-command", "command": "echo ok"},
    ]})
    assert not install[2].with_name("HEARTBEAT.md").exists()
    assert engine.execute_plan(operations, context).exit_code == 2


@pytest.mark.parametrize("check", [
    {"name": "bad"}, {"name": "bad", "type": "file-watch"},
    {"name": "bad", "type": "self-check", "interval_minutes": -1},
])
def test_heartbeat_invalid_merged_definition_refuses_plan(install, check):
    with pytest.raises(engine.ExecutionError):
        heartbeat_preview(install, {"checks": [check]})
    assert not install[2].with_name("HEARTBEAT.md").exists()


@pytest.mark.parametrize("check", [
    {"name": "inbox", "max_age_hours": 2, "reset": ["max_age_hours"]},
    {"name": "other", "reset": ["max_age_hours"]},
])
def test_heartbeat_age_reset_rejects_conflict_and_wrong_merged_type(install, check):
    from istota.settings_plan.schema import SchemaError

    path = heartbeat_seed(install)
    before = path.read_bytes()
    with pytest.raises((engine.ExecutionError, SchemaError)):
        heartbeat_preview(install, {"checks": [check]})
    assert path.read_bytes() == before


def test_heartbeat_age_reset_clears_destination_limit(install):
    from istota import heartbeat
    from istota.settings_plan.files import snapshot_heartbeat

    path = heartbeat_seed(install)
    snapshot, _ = snapshot_heartbeat(install[1], "alice")
    path.write_text(path.read_text().replace('cooldown_minutes = 15', 'max_age_hours = 1\ncooldown_minutes = 15'))
    context, operations = heartbeat_preview(install, snapshot)
    assert engine.execute_plan(operations, context).exit_code == 2
    assert "max_age_hours" not in path.read_text()
    assert "max_age_hours" not in heartbeat.load_heartbeat_config(context.config, "alice")[1][0].config
    context, operations = heartbeat_preview(install, snapshot)
    assert engine.execute_plan(operations, context).exit_code == 0


@pytest.mark.parametrize("content", ['```toml\n[[checks]]\n', '```TOML\n[[checks]]\n```\n'])
def test_heartbeat_malformed_fence_is_refused(install, content):
    path = install[2].with_name("HEARTBEAT.md")
    path.write_text(content)
    with pytest.raises(engine.ExecutionError, match="heartbeat_file_invalid"):
        heartbeat_preview(install, {"checks": []}, prune=True)
    assert path.read_text() == content


@pytest.mark.parametrize("check", [
    {"name": "web", "type": "url-health", "url": "https://example.com"},
    {"name": "shell", "type": "shell-command", "command": "echo ok"},
    {"name": "watch", "type": "file-watch", "path": "/Users/bob/private"},
])
def test_heartbeat_rejects_other_users_paths_and_admin_checks(install, check, monkeypatch, tmp_path):
    admins = tmp_path / "other-admins"
    admins.write_text("bob\n")
    monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(admins))
    with pytest.raises(engine.ExecutionError, match="validation_error"):
        heartbeat_preview(install, {"checks": [check]})
    assert not install[2].with_name("HEARTBEAT.md").exists()


def test_heartbeat_defaults_match_handler_configuration(install):
    from istota.settings_plan.files import snapshot_heartbeat

    context, operations = heartbeat_preview(install, {"checks": [
        {"name": "self", "type": "self-check"},
        {"name": "web", "type": "url-health", "url": "https://example.com"},
        {"name": "shell", "type": "shell-command", "command": "echo ok"},
    ]})
    assert engine.execute_plan(operations, context).exit_code == 2
    definitions, _ = snapshot_heartbeat(context.config, "alice")
    checks = {check["name"]: check for check in definitions["checks"]}
    assert checks["self"]["execution_test"] is True
    assert checks["web"]["expected_status"] == 200 and checks["web"]["timeout"] == 10
    assert checks["shell"]["timeout"] == 30 and checks["shell"]["condition"] == ""
    assert checks["shell"]["message"] == "Check failed: {value}"


def test_heartbeat_revoked_admin_is_rechecked_before_write(install, tmp_path, monkeypatch):
    context, operations = heartbeat_preview(install, {"checks": [
        {"name": "web", "type": "url-health", "url": "https://example.com"},
    ]})
    admins = tmp_path / "other-admins"
    admins.write_text("bob\n")
    monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(admins))
    result = engine.execute_plan(operations, context)
    assert result.errors == [{"code": "state_changed", "path": ["users", "alice", "heartbeat"]}]
    assert not install[2].with_name("HEARTBEAT.md").exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_heartbeat_nonregular_file_refused(install, tmp_path, kind):
    import os

    path = install[2].with_name("HEARTBEAT.md")
    if kind == "symlink":
        target = tmp_path / "target.md"
        target.write_text("Notes")
        path.symlink_to(target)
    else:
        os.mkfifo(path)
    with pytest.raises(engine.ExecutionError, match="file_unavailable"):
        heartbeat_preview(install, {"checks": []}, prune=True)


def test_heartbeat_cli_dry_run_write_and_repeat(install, tmp_path, monkeypatch, capsys):
    from istota import cli
    import json

    path = heartbeat_seed(install)
    before = path.read_bytes()
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"users": {"alice": {"heartbeat": {"checks": [
        {"name": "inbox", "reset": ["cooldown_minutes", "interval_minutes", "max_age_hours"]},
    ]}}}}))
    for flags, expected in [(["--dry-run"], 2), ([], 2), ([], 0)]:
        monkeypatch.setattr("sys.argv", ["istota", "-c", str(install[0]), "apply", "-f", str(plan), "--format", "json", *flags])
        code = 0
        try:
            cli.main()
        except SystemExit as result:
            code = result.code
        assert code == expected
        report = json.loads(capsys.readouterr().out)
        assert report["restart_required"] is False
        if flags:
            assert path.read_bytes() == before
