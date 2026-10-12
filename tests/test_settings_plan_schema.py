"""Plan structure is checked before any live state is read."""

import io
import json
from dataclasses import fields

import pytest

from istota import apply, user_profiles


def test_check_accepts_user_definitions_without_loading_live_state(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("a structural check read live configuration")

    monkeypatch.setattr("istota.config.load_config", forbidden)
    document = {"users": {"alice": {
        "profile": {"display_name": "Alice", "disabled_modules": [], "routing": {}},
        "resources": [{"type": "folder", "path": "/data/papers", "permissions": "readwrite", "extra": {}}],
        "briefings": [{"name": "morning", "blocks": [{"sources": [{"kind": "calendar"}]}]}],
        "scheduled_jobs": [{"name": "weekly", "enabled": False}],
        "heartbeat": {"checks": [{"name": "inbox", "reset": ["interval_minutes"]}]},
        "secrets": [{"service": "karakeep", "keys": ["api_key"]}],
    }}, "shared_briefing_blocks": [{"name": "news", "sources": [{"kind": "browse"}]}]}
    out, err = [], []
    code = apply.check("-", as_json=True, out=out.append, err=err.append,
                       stdin=io.StringIO(json.dumps(document)))
    assert code == 0, out
    assert json.loads(out[0]) == {"problems": [], "error": None}
    assert err == []


def test_presence_defaults_and_scope():
    from istota.settings_plan.schema import parse_plan, select_users

    document = {"config": {}, "shared_briefing_blocks": [], "users": {
        "alice": {"profile": {"display_name": "", "default_briefings": False}, "resources": []},
        "bob": {},
    }}
    plan = parse_plan(document)
    alice = plan.users["alice"]
    assert alice.profile_mode == "update"
    assert "profile_mode" not in alice.sections
    assert alice.sections["resources"] == []
    assert "resources" not in plan.users["bob"].sections
    assert alice.sections["profile"] == {"display_name": "", "default_briefings": False}
    selected = select_users(plan, ["alice"])
    assert set(selected.users) == {"alice"}
    assert selected.config is None and selected.shared_briefing_blocks is None
    assert plan.config == {} and plan.shared_briefing_blocks == []
    document["users"]["alice"]["profile"]["display_name"] = "changed"
    assert alice.sections["profile"]["display_name"] == ""


def test_profile_and_job_fields_are_deliberate_subsets():
    from istota.cron_loader import CronJob
    from istota.settings_plan.schema import JOB_FIELDS, PROFILE_FIELDS

    assert set(PROFILE_FIELDS) == (
        set(user_profiles.SELF_EDITABLE_FIELDS) | set(user_profiles.ADMIN_EDITABLE_FIELDS)
    ) - {"google_scopes"}
    assert set(JOB_FIELDS) == {field.name for field in fields(CronJob)}


@pytest.mark.parametrize("section, value, path", [
    ("profile", {"google_scopes": {}}, ["profile", "google_scopes"]),
    ("profile", {"default_briefings": "false"}, ["profile", "default_briefings"]),
    ("profile", {"max_background_workers": True}, ["profile", "max_background_workers"]),
    ("resources", [{"type": "shared_file", "path": "/data/x"}], ["resources", 0, "type"]),
    ("resources", [{"type": "folder"}], ["resources", 0, "path"]),
    ("briefings", [{"name": "morning", "components": {}}], ["briefings", 0, "components"]),
    ("scheduled_jobs", [{"name": "_module.foo"}], ["scheduled_jobs", 0, "name"]),
    ("scheduled_jobs", [{"name": "weekly", "output_target": "email"}], ["scheduled_jobs", 0, "output_target"]),
    ("heartbeat", {"checks": [{"name": "x", "reset": ["channel"]}]}, ["heartbeat", "checks", 0, "reset", 0]),
    ("heartbeat", {"checks": [{"name": "x", "interval_minutes": 2, "reset": ["interval_minutes"]}]}, ["heartbeat", "checks", 0, "reset"]),
    ("enrollment", {"clear": True, "number": "+12025550123"}, ["enrollment"]),
    ("enrollment", {"reset_identity": True}, ["enrollment", "reset_identity"]),
    ("secrets", [{"service": "karakeep", "keys": ["api_key", "api_key"]}], ["secrets", 0, "keys", 1]),
])
def test_malformed_sections_have_typed_paths_without_values(section, value, path):
    from istota.settings_plan.schema import SchemaError, parse_plan

    with pytest.raises(SchemaError) as caught:
        parse_plan({"users": {"alice": {section: value}}})
    assert caught.value.problem["kind"] == "discarded"
    assert caught.value.problem["path"] == ["users", "alice", *path]
    if section == "enrollment" and "reset_identity" in value:
        assert "user ensure --reset-whatsapp-identity" in str(caught.value)


@pytest.mark.parametrize("document", [
    {"users": {"../alice": {}}},
    {"users": {"Alice": {}, "alice": {}}},
    {"users": {"alice": {"unknown": {}}}},
    {"users": {"alice": {"briefings": [{"name": "same"}, {"name": "same"}]}}},
    {"users": {"alice": {"resources": [{"type": "folder", "path": "/data/x"}] * 2}}},
    {"shared_briefing_blocks": [{"name": "news", "sources": [{"kind": "calendar"}]}]},
    {"config": {"brain": {"native": {"api_key": None}}}},
    {"users": {"alice": {"resources": [{"type": "folder", "path": "/data/x", "extra": {"x": float("nan")}}]}}},
    {"meta": {"version": True}},
    {"meta": {"version": 2}},
])
def test_invalid_document_is_refused(document):
    from istota.settings_plan.schema import SchemaError, parse_plan

    with pytest.raises((SchemaError, apply.PlanError)):
        parse_plan(document)


def test_unknown_selection_is_refused():
    from istota.settings_plan.schema import SchemaError, parse_plan, select_users

    with pytest.raises(SchemaError):
        select_users(parse_plan({"users": {"alice": {}}}), ["bob"])


def test_sidecar_preserves_segment_paths_and_hides_its_repr():
    from istota.settings_plan.schema import parse_sidecar

    sidecar = parse_sidecar({"version": 1, "users": {"alice": {"karakeep": {"api_key": "private-value"}}},
                            "config": [{"path": ["models", "model.with.dots", 0, "api_key"], "value": "private-value"}]})
    assert sidecar.config[0]["path"] == ["models", "model.with.dots", 0, "api_key"]
    assert "private-value" not in repr(sidecar)


@pytest.mark.parametrize("document", [
    {"users": {"alice": {"karakeep": {"api_key": ""}}}},
    {"users": {"alice": {"karakeep": {"api_key": False}}}},
    {"config": [{"path": "brain.native.api_key", "value": "private-value"}]},
    {"config": [{"path": ["models", True], "value": "private-value"}]},
    {"config": [{"path": ["api_key"], "value": "private-value"}] * 2},
    {"version": "1"},
])
def test_bad_sidecar_never_echoes_values(document):
    from istota.settings_plan.schema import SchemaError, parse_sidecar

    with pytest.raises(SchemaError) as caught:
        parse_sidecar(document)
    assert "private-value" not in str(caught.value)


@pytest.mark.parametrize("text", [
    '{"config": {"brain": {"native": {"api_key": "private-value", "api_key": "other"}}}}',
    '{"config": {"x": NaN}}',
    '[config."private-value"\n',
    '[config]\nprivate-value = "x"\nprivate-value = "y"\n',
])
def test_loader_refuses_malformed_input_without_quoting_it(text):
    with pytest.raises(apply.PlanError) as caught:
        apply.load_plan("-", stdin=io.StringIO(text))
    assert "private-value" not in str(caught.value)


def test_check_reports_schema_problems_through_existing_envelope():
    out = []
    code = apply.check("-", as_json=True, out=out.append, err=lambda _: None,
                       stdin=io.StringIO('{"users":{"alice":{"profile":{"default_briefings":"private-value"}}}}'))
    assert code == 1
    reply = json.loads(out[0])
    assert set(reply) == {"problems", "error"}
    assert reply["problems"][0]["path"] == ["users", "alice", "profile", "default_briefings"]
    assert "private-value" not in out[0]


def test_cli_check_accepts_toml_definitions_and_leaves_config_untouched(monkeypatch, capsys, tmp_path):
    from istota import cli

    config = tmp_path / "config.toml"
    config.write_text('bot_name = "Original"\n')
    before = config.stat().st_mtime_ns
    monkeypatch.setattr("sys.stdin", io.StringIO(
        '[users.alice.profile]\ndisplay_name = "Alice"\n'
        '[[users.alice.briefings]]\nname = "morning"\nenabled = false\n'
    ))
    monkeypatch.setattr("sys.argv", ["istota", "-c", str(config), "apply", "--check", "--json", "-f", "-"])
    cli.main()
    assert json.loads(capsys.readouterr().out) == {"problems": [], "error": None}
    assert config.stat().st_mtime_ns == before
    assert sorted(path.name for path in tmp_path.iterdir()) == ["config.toml"]


def test_execution_still_refuses_unimplemented_sections_before_writing(tmp_path):
    target = tmp_path / "config.toml"
    out, err = [], []
    code = apply.run("-", target, dry_run=False, out=out.append, err=err.append,
                     stdin=io.StringIO('{"config": {}, "users": {"alice": {"resources": []}}}'))
    assert code == 1 and not target.exists()
    assert "[users]" in "".join(err)


def test_config_only_run_refuses_null_config(tmp_path):
    target = tmp_path / "config.toml"
    code = apply.run("-", target, dry_run=False, out=lambda _: None, err=lambda _: None,
                     stdin=io.StringIO('{"config": null}'))
    assert code == 1 and not target.exists()


def test_partial_positional_definitions_are_not_filled_with_creation_defaults():
    from istota.settings_plan.schema import parse_plan

    document = {"users": {"alice": {"briefings": [{"name": "morning", "blocks": [
        {"title": "", "sources": [{"enabled": False, "config": {}}]},
        {"sources": []},
    ]}]}}}
    assert parse_plan(document).users["alice"].sections == document["users"]["alice"]


def test_invalid_unselected_user_is_not_hidden_by_scope():
    from istota.settings_plan.schema import SchemaError, parse_plan, select_users

    with pytest.raises(SchemaError):
        select_users(parse_plan({"users": {"alice": {}, "bob": {"profile": {"bad_field": True}}}}), ["alice"])


@pytest.mark.parametrize("raw", [
    '{"config":{"brain":{"native":{"api_key": "private-value",}}}}',
    '[config.brain.native]\napi_key = "private-value" trailing\n',
])
def test_syntax_errors_have_a_location_but_no_input_excerpt(raw):
    with pytest.raises(apply.PlanError) as caught:
        apply.load_plan("-", stdin=io.StringIO(raw))
    assert "line" in str(caught.value) and "column" in str(caught.value)
    assert "private-value" not in str(caught.value)
