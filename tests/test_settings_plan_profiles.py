"""Profile reconciliation through the real framework store and CLI."""

import json

import pytest

from istota import cli, db, user_profiles
from istota.lib import toml_write
from istota.settings_plan import engine
from istota.settings_plan.schema import parse_plan


@pytest.fixture
def install(tmp_path):
    database = tmp_path / "framework.db"
    db.init_db(database)
    config = tmp_path / "config.toml"
    config.write_text(toml_write.dumps({"db_path": str(database)}))
    return config, database


def preview(install, declarations, **options):
    context = engine.Context(install[0])
    operations = engine.build_plan(parse_plan({"users": declarations}), context, engine.Options(**options))
    return context, operations


def test_profile_preview_apply_and_second_apply_are_read_only_when_unchanged(install):
    config, database = install
    before = database.read_bytes()
    users = {"alice": {"profile": {"display_name": "Alice", "timezone": "Europe/Lisbon"}}}
    context, operations = preview(install, users, dry_run=True)
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 2 and result.restart_required
    assert database.read_bytes() == before
    assert context.config.users["alice"].timezone == "Europe/Lisbon"
    context, operations = preview(install, users)
    assert engine.execute_plan(operations, context).exit_code == 2
    before = database.read_bytes()
    context, operations = preview(install, users)
    assert engine.execute_plan(operations, context).exit_code == 0
    assert database.read_bytes() == before
    assert user_profiles.get_profile(database, "alice").display_name == "Alice"


def test_snapshot_defaults_reset_destination_and_preserve_consent_and_locks(install):
    from istota.settings_plan.profiles import snapshot_profile
    from istota.settings_plan.schema import PROFILE_FIELDS

    _, database = install
    user_profiles.provision_user(database, "alice", {"display_name": "Old", "disabled_modules": ["health"],
                                "max_background_workers": 4, "briefing_email_html": False}, mode="managed")
    user_profiles.update_profile(database, "alice", google_scopes={"gmail": "readonly"})
    desired = snapshot_profile(user_profiles.UserProfile("alice"))
    assert set(desired["profile"]) == set(PROFILE_FIELDS)
    assert desired["profile_mode"] == "update"
    assert "google_scopes" not in desired["profile"] and "enrollment" not in desired
    with db.get_db(database) as conn:
        locks = user_profiles.managed_fields(conn, "alice")
    context, operations = preview(install, {"alice": desired})
    assert engine.execute_plan(operations, context).exit_code == 2
    profile = user_profiles.get_profile(database, "alice")
    assert profile.display_name == "" and profile.disabled_modules == []
    assert profile.max_background_workers == 0 and profile.briefing_email_html is True
    assert profile.google_scopes == {"gmail": "readonly"}
    with db.get_db(database) as conn:
        assert user_profiles.managed_fields(conn, "alice") == locks
    context, operations = preview(install, {"alice": snapshot_profile(profile)})
    assert engine.execute_plan(operations, context).exit_code == 0


def test_managed_and_seed_metadata_do_not_report_settings_changes(install):
    _, database = install
    user_profiles.provision_user(database, "alice", {"display_name": "Alice"})
    context, operations = preview(install, {"alice": {"profile_mode": "managed", "profile": {"display_name": "Alice"}}})
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 0 and not result.restart_required
    with db.get_db(database) as conn:
        assert user_profiles.managed_fields(conn, "alice") == {"display_name"}
    context, operations = preview(install, {"alice": {"profile_mode": "seed", "profile": {"display_name": "Ignored"}}})
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 0 and not result.restart_required
    assert context.profiles["alice"].display_name == "Alice"
    with db.get_db(database) as conn:
        assert user_profiles.managed_fields(conn, "alice") == set()


@pytest.mark.parametrize("profile", [
    {"timezone": "Unknown/Zone"}, {"max_foreground_workers": -1},
    {"disabled_skills": ["nonexistent-skill"]}, {"disabled_modules": ["nonexistent-module"]},
    {"relay_delivery": "unsupported"}, {"routing": {"invalid-purpose": "email"}},
    {"default_destination": "unknown_surface"},
])
def test_invalid_later_user_refuses_the_whole_plan(install, profile):
    _, database = install
    before = database.read_bytes()
    with pytest.raises(engine.ExecutionError):
        preview(install, {"alice": {"profile": {"display_name": "Alice"}}, "bob": {"profile": profile}})
    assert database.read_bytes() == before


@pytest.mark.parametrize("field,value", [("email_addresses", ["alice@example.com"]), ("sms_phone_number", "+12025550123")])
def test_conflicts_between_new_users_are_preflight_errors(install, field, value):
    _, database = install
    before = database.read_bytes()
    with pytest.raises(engine.ExecutionError, match="identity_conflict"):
        preview(install, {"alice": {"profile": {field: value}}, "bob": {"profile": {field: value}}})
    assert database.read_bytes() == before


def test_existing_login_identity_is_not_claimable(install):
    from istota.webui.auth import upsert_identity

    _, database = install
    with db.get_db(database) as conn:
        upsert_identity(database, "bob", "bob@example.com", conn=conn, create_profile=True)
    with pytest.raises(engine.ExecutionError, match="profile_refused"):
        preview(install, {"alice": {"profile": {"email_addresses": ["BOB@example.com"]}}})
    assert user_profiles.get_profile(database, "alice") is None


@pytest.mark.parametrize("field,value", [("default_room", "private-bob"), ("default_destination", "web:private-bob"),
                                         ("log_channel", "talk-bob"), ("routing", {"briefing": "talk:talk-bob"})])
def test_destinations_cannot_use_another_users_room(install, field, value):
    _, database = install
    with db.get_db(database) as conn:
        db.register_room(conn, "private-bob", "bob", origin="web")
        db.add_room_binding(conn, "private-bob", "talk", "talk-bob")
    with pytest.raises(engine.ExecutionError, match="validation_error"):
        preview(install, {"alice": {"profile": {field: value}}})


@pytest.mark.parametrize("surface", ["web", "room"])
def test_private_room_destination_succeeds_but_shared_or_revoked_room_fails(install, surface):
    _, database = install
    with db.get_db(database) as conn:
        db.register_room(conn, "private-alice", "alice", origin="web")
    desired = {"alice": {"profile": {"default_destination": surface + ":private-alice"}}}
    context, operations = preview(install, desired)
    with db.get_db(database) as conn:
        db.add_room_member(conn, "private-alice", "bob")
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 1 and result.errors[0]["code"] == "state_changed"
    assert user_profiles.get_profile(database, "alice") is None
    with pytest.raises(engine.ExecutionError, match="validation_error"):
        preview(install, desired)


def test_stale_profile_refuses_but_runtime_timestamp_does_not(install):
    _, database = install
    user_profiles.provision_user(database, "alice", {"display_name": "Original"})
    desired = {"alice": {"profile": {"display_name": "Planned"}}}
    context, operations = preview(install, desired)
    user_profiles.update_profile(database, "alice", timezone="Europe/Lisbon")
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 1 and result.errors[0]["code"] == "state_changed"
    assert user_profiles.get_profile(database, "alice").display_name == "Original"
    context, operations = preview(install, desired)
    with db.get_db(database) as conn:
        conn.execute("UPDATE user_profiles SET updated_at = '2000-01-01' WHERE user_id = 'alice'")
    assert engine.execute_plan(operations, context).exit_code == 2


def test_enrollment_changes_use_shared_cleanup_and_omit_private_report_values(install):
    from istota.settings_plan.report import result_document

    _, database = install
    user_profiles.provision_user(database, "alice", {}, whatsapp_op="set", whatsapp_number="+12025550123")
    with db.get_db(database) as conn:
        conn.execute("UPDATE whatsapp_user_bindings SET jid = 'old-id', last_user_message_at = '2000-01-01' WHERE user_id = 'alice'")
    desired = {"alice": {"enrollment": {"number": "+12025550124", "bsuid": "new-id"}}}
    context, operations = preview(install, desired)
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 2 and result.restart_required
    assert "new-id" not in json.dumps(result_document(result))
    with db.get_db(database) as conn:
        binding = db.get_whatsapp_binding(conn, "alice")
        assert binding.bootstrap_phone_number == "+12025550124"
        assert not binding.jid and binding.last_user_message_at is None
    context, operations = preview(install, desired)
    assert engine.execute_plan(operations, context).exit_code == 0
    context, operations = preview(install, {"alice": {"enrollment": {"clear": True}}})
    assert engine.execute_plan(operations, context).exit_code == 2
    with db.get_db(database) as conn:
        assert db.get_whatsapp_binding(conn, "alice") is None


def test_seed_fills_only_empty_phone_identities(install):
    _, database = install
    user_profiles.provision_user(database, "alice", {"timezone": "Europe/Lisbon"}, mode="managed")
    context, operations = preview(install, {"alice": {"profile_mode": "seed", "profile": {
        "timezone": "UTC", "sms_phone_number": "+12025550123"}, "enrollment": {"number": "+12025550124"}}})
    assert context.config.users["alice"].timezone == "Europe/Lisbon"
    assert engine.execute_plan(operations, context).exit_code == 2
    assert user_profiles.get_profile(database, "alice").sms_phone_number == "+12025550123"


def test_cli_profile_apply_reports_change_then_noop(install, tmp_path, monkeypatch, capsys):
    config, database = install
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps({"users": {"alice": {"profile": {"display_name": "Alice"}}}}))
    for expected in (2, 0):
        monkeypatch.setattr("sys.argv", ["istota", "-c", str(config), "apply", "-f", str(plan), "--format", "json"])
        code = 0
        try:
            cli.main()
        except SystemExit as exc:
            code = exc.code
        assert code == expected
        report = json.loads(capsys.readouterr().out)
        assert report["restart_required"] is (expected == 2)


def test_new_user_for_another_section_is_prospective_before_adapter(install):
    context = engine.Context(install[0])
    seen = []
    context.planners["resources"] = lambda plan, user, context, options: seen.append(context.config.users[user].display_name)
    operations = engine.build_plan(parse_plan({"users": {"alice": {"resources": []}}}), context, engine.Options())
    assert seen == ["alice"]
    assert engine.execute_plan(operations, context).exit_code == 2


def test_empty_timezone_uses_readers_default_without_perpetual_drift(install):
    _, database = install
    user_profiles.provision_user(database, "alice", {"timezone": "Europe/Lisbon"})
    desired = {"alice": {"profile": {"timezone": ""}}}
    for expected in (2, 0):
        context, operations = preview(install, desired)
        assert engine.execute_plan(operations, context).exit_code == expected
    assert user_profiles.get_profile(database, "alice").timezone == "UTC"


def test_identity_claimed_after_preview_refuses_without_creating_user(install):
    _, database = install
    context, operations = preview(install, {"alice": {"profile": {"email_addresses": ["shared@example.com"]}}})
    user_profiles.provision_user(database, "bob", {"email_addresses": ["shared@example.com"]})
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 1 and result.errors[0]["code"] == "state_changed"
    assert user_profiles.get_profile(database, "alice") is None


def test_profile_and_managed_fields_rollback_together(install):
    _, database = install
    user_profiles.provision_user(database, "alice", {"display_name": "Original"})
    context, operations = preview(install, {"alice": {"profile_mode": "managed", "profile": {"display_name": "New"}}})
    with db.get_db(database) as conn:
        conn.execute("CREATE TRIGGER refuse_management BEFORE INSERT ON user_profile_managed_fields BEGIN SELECT RAISE(ABORT, 'failure'); END")
    result = engine.execute_plan(operations, context)
    assert result.exit_code == 1 and not result.changed
    assert user_profiles.get_profile(database, "alice").display_name == "Original"


def test_selected_users_do_not_validate_unselected_destinations(install):
    context, operations = preview(install, {"alice": {"profile": {"default_room": "absent"}}, "bob": {}}, users=["bob"])
    assert set(context.profiles) == {"bob"}
    assert engine.execute_plan(operations, context).exit_code == 2
