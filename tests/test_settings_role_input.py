"""Role settings enter the same planner as a snapshot or local plan."""

import json

from istota import apply, db, user_profiles
from istota.lib import toml_write


def test_private_role_input_applies_profile_and_converges(tmp_path):
    database = tmp_path / "framework.db"
    db.init_db(database)
    config = tmp_path / "config.toml"
    config.write_text(toml_write.dumps({"db_path": str(database)}))
    payload = tmp_path / "role.json"
    payload.write_text(json.dumps({"istota_users": {"alice": {
        "display-name": "Alice", "timezone": "Europe/Lisbon", "managed": True,
        "disabled-module": [""], "no-briefing-email-html": True,
    }}}))
    payload.chmod(0o600)
    out, err = [], []
    assert apply.run(payload, config, dry_run=False, input_format="role-vars",
                     out=out.append, err=err.append) == 2
    profile = user_profiles.get_profile(database, "alice")
    assert profile.display_name == "Alice" and profile.timezone == "Europe/Lisbon"
    assert profile.disabled_modules == [] and profile.briefing_email_html is False
    assert apply.run(payload, config, dry_run=True, input_format="role-vars",
                     out=out.append, err=err.append) == 0


def test_legacy_flags_preserve_presence_aliases_and_defaults():
    from istota.settings_plan.role_input import convert_role_input

    plan, sidecar = convert_role_input({
        "istota_users": {"alice": {
            "seed": True, "managed": False, "timezone": "UTC", "display-name": "",
            "email": ["alice@example.com", "a@example.org"], "disabled-skill": [],
            "default-room": "  ", "default-destination": "", "route": ["reply="],
            "clear-sms-number": True, "max-foreground-workers": 0,
            "no-default-briefings": True, "log-channel": None,
        }},
        "istota_user_resources": {"alice": [
            {"type": "folder", "path": "/data/papers", "extras": ["count=2", "label=Papers"]},
            {"type": "folder", "path": "/data/empty", "extras-json": "{}"},
        ], "bob": []},
        "istota_user_briefings": {"alice": [{"name": "morning", "cron": "0 7 * * *"}]},
    })
    assert plan["users"]["alice"]["profile"] == {
        "timezone": "UTC", "display_name": "", "email_addresses": ["alice@example.com", "a@example.org"],
        "default_room": "", "default_destination": "talk", "routing": {}, "sms_phone_number": "",
        "max_foreground_workers": 0, "default_briefings": False,
    }
    assert plan["users"]["alice"]["profile_mode"] == "seed"
    assert plan["users"]["alice"]["resources"] == [
        {"type": "folder", "path": "/data/papers", "name": "", "permissions": "read", "extra": {"count": 2, "label": "Papers"}},
        {"type": "folder", "path": "/data/empty", "name": "", "permissions": "read", "extra": {}},
    ]
    assert plan["users"]["alice"]["briefings"] == [{
        "name": "morning", "cron": "0 7 * * *", "title": "", "conversation_token": "", "output": "talk", "enabled": True,
    }]
    assert "bob" not in plan["users"] and sidecar == {}


def test_empty_legacy_collections_and_secret_values_declare_nothing():
    from istota.settings_plan.role_input import LEGACY_VARIABLES, convert_role_input

    assert convert_role_input(dict.fromkeys(LEGACY_VARIABLES, {})) == ({}, {})
    assert convert_role_input({"istota_user_resources": {"alice": []},
                               "istota_user_briefings": {"alice": []},
                               "istota_user_secrets": {"alice": [{"value": ""}, {}]}}) == ({}, {})


def test_migration_converter_output_is_accepted_without_translation():
    from istota.maintenance import role_vars
    from istota.settings_plan.role_input import ROLE_VARIABLES, convert_role_input
    from tests.test_management_role import _render

    old = {"istota_user_profile_mode": "seed", "istota_users": {"alice": {
        "display_name": "{{ inventory_display }}", "email_addresses": ["alice@example.com"],
        "disabled_modules": ["feeds"], "timezone_follow_location": False,
        "resources": [{"type": "folder", "path": "/data/papers", "label": "Papers"}],
        "briefings": [{"name": "morning", "cron": "0 7 * * *", "output": "email"}],
    }}, "inventory_display": "Alice", "istota_user_secrets": {
        "alice": [{"service": "karakeep", "key": "api_key", "value": "private-migration-value"}]}}
    converted = role_vars.convert(old, config={"bot_name": "Helper"})
    rendered = _render(converted.vars)
    envelope = json.loads(json.dumps({k: v for k, v in rendered.items() if k in ROLE_VARIABLES}))
    plan, sidecar = convert_role_input(envelope)
    assert plan["meta"] == {"managed": True, "source": "ansible"}
    assert plan["config"] == {"bot_name": "Helper"}
    assert plan["users"]["alice"]["profile_mode"] == "seed"
    assert plan["users"]["alice"]["profile"]["display_name"] == "Alice"
    assert plan["users"]["alice"]["resources"][0]["extra"] == {"label": "Papers"}
    assert plan["users"]["alice"]["briefings"][0]["output"] == "email"
    assert plan["users"]["alice"]["secrets"] == [{"service": "karakeep", "keys": ["api_key"]}]
    assert sidecar["users"]["alice"]["karakeep"]["api_key"] == "private-migration-value"
    assert "private-migration-value" not in json.dumps(plan) + " ".join(converted.notes)


def test_canonical_payload_keeps_schema_and_modes_and_adopts_only_config():
    from copy import deepcopy
    from istota.settings_plan.role_input import convert_role_input

    document = {"istota_plan": {"meta": {"source": "snapshot", "version": 1},
        "config": {"bot_name": "Helper"}, "users": {"alice": {"profile_mode": "update", "resources": []}}},
        "istota_config": {}, "istota_users": {}, "istota_plan_secrets": {}}
    original = deepcopy(document)
    plan, sidecar = convert_role_input(json.loads(json.dumps(document)))
    assert plan == original["istota_plan"] | {"meta": {"source": "ansible", "version": 1, "managed": True}}
    assert document == original and sidecar == {}


def test_cli_canonical_sidecar_and_check_stdin(tmp_path, monkeypatch, capsys):
    import io
    import tomllib
    import pytest
    from istota import cli

    envelope = {"istota_plan": {"config": {"brain": {"native": {"extra_headers": {"x.tenant": "<unchanged>"}}}}},
                "istota_plan_secrets": {"config": [{"path": ["brain", "native", "extra_headers", "x.tenant"],
                                                   "value": "private-role-value"}]}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(envelope)))
    monkeypatch.setattr("sys.argv", ["istota", "apply", "--input-format", "role-vars", "-f", "-", "--check", "--json"])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"problems": [], "error": None}
    payload = tmp_path / "role.json"
    payload.write_text(json.dumps(envelope))
    payload.chmod(0o600)
    target = tmp_path / "config.toml"
    monkeypatch.setattr("sys.argv", ["istota", "-c", str(target), "apply", "--input-format", "role-vars",
                                    "-f", str(payload), "--format", "json"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert "private-role-value" not in captured.out + captured.err
    assert tomllib.loads(target.read_text())["brain"]["native"]["extra_headers"]["x.tenant"] == "private-role-value"


def test_role_errors_are_safe_and_preflight(tmp_path):
    import pytest
    from istota.settings_plan.role_input import convert_role_input
    from istota.settings_plan.schema import SchemaError

    invalid = [
        {"istota_plan": {"config": {}}, "istota_users": {"alice": {}}},
        {"istota_plan_secrets": {"users": {"alice": {"karakeep": {"api_key": "private-bad-value"}}}}},
        {"istota_users": {"alice": {"reset-whatsapp-identity": True}}},
        {"istota_users": {"alice": {"managed": True, "seed": True}}},
        {"istota_users": {"alice": {"max-background-workers": "private-bad-value"}}},
        {"istota_users": {"alice": {"route": "private-bad-value"}}},
        {"istota_user_briefings": {"alice": [{"components": "private-bad-value"}]}},
        {"istota_user_resources": {"alice": [{"type": "folder", "path": "/data/papers", "extras-json": "private-bad-value"}]}},
        {"istota_users": {"alice": {"unknown": False}}},
    ]
    for envelope in invalid:
        with pytest.raises(SchemaError) as exc:
            convert_role_input(envelope)
        assert "private-bad-value" not in str(exc.value)
        source = tmp_path / "role.json"
        source.write_text(json.dumps(envelope))
        source.chmod(0o600)
        out, err = [], []
        target = tmp_path / "config.toml"
        assert apply.run(source, target, input_format="role-vars", dry_run=False,
                         out=out.append, err=err.append) == 1
        assert not target.exists() and "private-bad-value" not in "".join(out + err)
    with pytest.raises(SchemaError, match="user ensure --reset-whatsapp-identity"):
        convert_role_input(invalid[2])


def test_role_input_requires_private_regular_file_and_safe_parse_errors(tmp_path):
    import os
    import pytest

    source = tmp_path / "role.json"
    source.write_text('{"private-malformed-value":')
    source.chmod(0o640)
    with pytest.raises(apply.PlanError, match="mode 0600"):
        apply.load_input(source, input_format="role-vars")
    source.chmod(0o600)
    with pytest.raises(apply.PlanError) as exc:
        apply.load_input(source, input_format="role-vars")
    assert "private-malformed-value" not in str(exc.value)
    for kind in ("symlink", "fifo", "directory"):
        path = tmp_path / kind
        if kind == "symlink":
            path.symlink_to(source)
        elif kind == "fifo":
            os.mkfifo(path, 0o600)
        else:
            path.mkdir()
        with pytest.raises(apply.PlanError):
            apply.load_input(path, input_format="role-vars")


def test_legacy_secret_write_and_empty_skip_use_the_real_store(tmp_path, monkeypatch, caplog):
    from istota.credentials import store

    monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-role-key-" * 4)
    database = tmp_path / "framework.db"
    db.init_db(database)
    user_profiles.provision_user(database, "alice", {})
    store.set_secret(database, "alice", "karakeep", "api_key", "private-old-value")
    config = tmp_path / "config.toml"
    config.write_text(toml_write.dumps({"db_path": str(database)}))
    payload = tmp_path / "role.json"
    payload.write_text(json.dumps({"istota_user_secrets": {
        "alice": [{"service": "karakeep", "key": "api_key", "value": "private-new-value"}]}}))
    payload.chmod(0o600)
    before = database.read_bytes()
    for dry_run, expected in [(True, 2), (False, 2), (True, 0)]:
        out, err = [], []
        assert apply.run(payload, config, dry_run=dry_run, input_format="role-vars", output_format="json",
                         out=out.append, err=err.append) == expected
        assert not json.loads(out[0])["restart_required"]
        assert "private-new-value" not in "".join(out + err) + caplog.text
        if dry_run and expected == 2:
            assert database.read_bytes() == before
    assert store.list_user_services(database, "alice")["karakeep"][0]["last_accessed_at"] is None
    payload.write_text(json.dumps({"istota_user_secrets": {
        "alice": [{"service": "karakeep", "key": "api_key", "value": ""}]}}))
    assert apply.run(payload, config, dry_run=False, input_format="role-vars", prune=True, prune_secrets=True,
                     out=lambda _: None, err=lambda _: None) == 0
    assert store.get_secret(database, "alice", "karakeep", "api_key") == "private-new-value"
