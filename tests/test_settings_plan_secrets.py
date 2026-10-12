"""Private config replacements through the real apply boundary."""

import json
import os
import tomllib

import pytest

from istota import apply, cli
from istota.config_editor.secrets import SENTINEL
from istota.lib import toml_write


def invoke(tmp_path, document, *, sidecar=None, dry_run=False, output_format="json"):
    plan = tmp_path / "plan.json"
    plan.write_text(json.dumps(document))
    kwargs = {}
    if sidecar is not None:
        path = tmp_path / "private.json"
        path.write_text(json.dumps(sidecar))
        path.chmod(0o600)
        kwargs["secrets_file"] = path
    out, err = [], []
    code = apply.run(plan, tmp_path / "config.toml", dry_run=dry_run,
                     output_format=output_format, out=out.append, err=err.append, **kwargs)
    return code, "\n".join(out), "\n".join(err)


def test_sentinel_preserves_current_config_and_snapshot_marker(tmp_path):
    current = {"brain": {"native": {"extra_headers": {"x.tenant": "old-private-value"}}}}
    config = tmp_path / "config.toml"
    config.write_text(toml_write.dumps(current))
    marker = tmp_path / "managed.json"
    marker.write_text('{"config": {"managed": true, "source": "ansible"}}')
    before = config.read_bytes(), marker.read_bytes()
    document = {"meta": {"source": "snapshot"}, "config": {
        "brain": {"native": {"extra_headers": {"x.tenant": SENTINEL}}}}}
    code, out, err = invoke(tmp_path, document)
    assert code == 0 and json.loads(out)["operations"] == []
    assert (config.read_bytes(), marker.read_bytes()) == before
    assert "old-private-value" not in out + err


@pytest.mark.parametrize("output_format", ["json", "text"])
def test_sidecar_nested_array_and_dotted_header_apply_and_dry_run(tmp_path, output_format, caplog):
    config = {"brain": {"native": {"extra_headers": {"x.tenant": SENTINEL}}},
              "extensions": {"models": {"model.v1": [{"name": "first", "api_key": SENTINEL}]}}}
    values = ["replacement-header", "replacement-array-key"]
    sidecar = {"config": [
        {"path": ["brain", "native", "extra_headers", "x.tenant"], "value": values[0]},
        {"path": ["extensions", "models", "model.v1", 0, "api_key"], "value": values[1]},
    ]}
    code, out, err = invoke(tmp_path, {"config": config}, sidecar=sidecar,
                            dry_run=True, output_format=output_format)
    assert code == 2 and not (tmp_path / "config.toml").exists()
    assert all(value not in out + err + caplog.text for value in values)
    code, out, err = invoke(tmp_path, {"config": config}, sidecar=sidecar, output_format=output_format)
    assert code == 2
    stored = tomllib.loads((tmp_path / "config.toml").read_text())
    assert stored["brain"]["native"]["extra_headers"]["x.tenant"] == values[0]
    assert stored["extensions"]["models"]["model.v1"][0]["api_key"] == values[1]
    assert all(value not in out + err + caplog.text for value in values)
    assert (tmp_path / "config.toml").stat().st_mode & 0o777 == 0o600
    assert invoke(tmp_path, {"config": config}, sidecar=sidecar)[0] == 0


@pytest.mark.parametrize("path", [
    ["bot_name"], ["missing", "api_key"], ["brain", "native", "extra_headers"],
    ["extensions", "models", "0", "api_key"], ["nextcloud", "app_password"],
])
def test_invalid_or_file_routed_targets_refuse_without_values(tmp_path, path):
    config = {"bot_name": "Bot", "brain": {"native": {"extra_headers": {}}},
              "extensions": {"models": [{"api_key": SENTINEL}]},
              "nextcloud": {"app_password": SENTINEL}}
    code, out, err = invoke(tmp_path, {"config": config}, sidecar={
        "config": [{"path": path, "value": "private-refused-value"}]})
    assert code == 1 and not (tmp_path / "config.toml").exists()
    assert "private-refused-value" not in out + err


def test_missing_sentinel_names_path_and_sidecar_remedy(tmp_path):
    code, out, err = invoke(tmp_path, {"config": {"caldav": {"password": SENTINEL}}}, output_format="text")
    assert code == 1 and "caldav" in err and "--secrets-file" in err
    assert not (tmp_path / "config.toml").exists()


@pytest.mark.parametrize("kind", ["public", "symlink", "directory", "fifo", "stdin", "malformed"])
def test_private_reader_refuses_unsafe_files(tmp_path, kind):
    from istota.settings_plan.secrets import read_sidecar

    path = tmp_path / "private.json"
    path.write_text('{"config": [{"value": "private-malformed-value"')
    path.chmod(0o600)
    if kind == "public":
        path.chmod(0o640)
    elif kind == "symlink":
        link = tmp_path / "link.json"
        link.symlink_to(path)
        path = link
    elif kind == "directory":
        path = tmp_path
    elif kind == "fifo":
        path.unlink()
        os.mkfifo(path, 0o600)
    elif kind == "stdin":
        path = "-"
    with pytest.raises(apply.PlanError) as exc:
        read_sidecar(path)
    assert "private-malformed-value" not in str(exc.value)


def test_cli_accepts_sidecar_path(monkeypatch, tmp_path, capsys):
    plan = tmp_path / "plan.json"
    sidecar = tmp_path / "private.toml"
    plan.write_text(json.dumps({"config": {"brain": {"native": {"extra_headers": {"x.tenant": SENTINEL}}}}}))
    sidecar.write_text('[[config]]\npath = ["brain", "native", "extra_headers", "x.tenant"]\nvalue = "private-cli-value"\n')
    sidecar.chmod(0o600)
    monkeypatch.setattr("sys.argv", ["istota", "-c", str(tmp_path / "config.toml"), "apply",
                                   "-f", str(plan), "--secrets-file", str(sidecar), "--format", "json"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 2
    captured = capsys.readouterr()
    assert json.loads(captured.out)["changed"]
    assert "private-cli-value" not in captured.out + captured.err


def test_sidecar_is_read_once_even_if_replaced_before_execution(tmp_path, monkeypatch):
    from istota.settings_plan import engine

    original = engine.execute_plan

    def replace_sidecar(operations, context):
        (tmp_path / "private.json").write_text('invalid replacement document')
        return original(operations, context)

    monkeypatch.setattr(engine, "execute_plan", replace_sidecar)
    document = {"config": {"brain": {"native": {"extra_headers": {"x.tenant": "literal-old"}}}}}
    sidecar = {"config": [{"path": ["brain", "native", "extra_headers", "x.tenant"], "value": "private-once"}]}
    code, out, err = invoke(tmp_path, document, sidecar=sidecar)
    assert code == 2 and "private-once" not in out + err
    assert tomllib.loads((tmp_path / "config.toml").read_text())["brain"]["native"]["extra_headers"]["x.tenant"] == "private-once"


def test_config_edit_during_resolution_refuses_stale_secret(tmp_path, monkeypatch):
    from istota.settings_plan import secrets

    original = secrets.resolve_config
    config = tmp_path / "config.toml"
    config.write_text('[brain.native.extra_headers]\nx = "old-private-value"\n')

    def interleave(document, config_path, sidecar):
        resolved = original(document, config_path, sidecar)
        config_path.write_text('[brain.native.extra_headers]\nx = "new-private-value"\n')
        return resolved

    monkeypatch.setattr(secrets, "resolve_config", interleave)
    code, out, err = invoke(tmp_path, {"config": {"brain": {"native": {"extra_headers": {"x": SENTINEL}}}}})
    assert code == 1 and json.loads(out)["errors"][0]["code"] == "state_changed"
    assert "new-private-value" in config.read_text()
    assert "old-private-value" not in out + err and "new-private-value" not in out + err


def test_loader_exception_does_not_disclose_private_replacement(tmp_path, monkeypatch, caplog):
    def refuse(*args, **kwargs):
        raise ValueError("private-loader-value")

    monkeypatch.setattr("istota.config.load_config", refuse)
    code, out, err = invoke(tmp_path, {"config": {"brain": {"native": {"extra_headers": {"x": SENTINEL}}}}},
                            sidecar={"config": [{"path": ["brain", "native", "extra_headers", "x"], "value": "private-loader-value"}]},
                            output_format="text")
    assert code == 1 and "private-loader-value" not in out + err + caplog.text
    assert not (tmp_path / "config.toml").exists()


def test_sidecar_scope_and_unmatched_user_slots():
    from istota.settings_plan.schema import SchemaError, parse_plan, parse_sidecar
    from istota.settings_plan.secrets import select_sidecar

    plan = parse_plan({"users": {"alice": {"secrets": [{"service": "karakeep", "keys": ["api_key"]}]},
                                 "bob": {"secrets": []}}})
    sidecar = parse_sidecar({"users": {"alice": {"karakeep": {"api_key": "private-user-value"}},
                                      "bob": {"other": {"api_key": "private-ignored-value"}}},
                             "config": [{"path": ["missing", "api_key"], "value": "private-ignored-config"}]})
    scoped = select_sidecar(plan, sidecar, ["alice"])
    assert set(scoped.users) == {"alice"} and scoped.config == []
    assert "private" not in repr(scoped)
    with pytest.raises(SchemaError, match="service is not declared"):
        select_sidecar(plan, sidecar, None)
    with pytest.raises(SchemaError, match="key is not declared"):
        select_sidecar(plan, parse_sidecar({"users": {"alice": {"karakeep": {"base_url": "private-unmatched"}}}}), None)
    with pytest.raises(SchemaError, match="user is not declared"):
        select_sidecar(plan, parse_sidecar({"users": {"unknown": {}}}), None)
    with pytest.raises(SchemaError, match="require a submitted config"):
        select_sidecar(plan, parse_sidecar({"config": sidecar.config}), None)


def test_reordered_array_requires_replacement_not_positional_credential(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text(toml_write.dumps({"extensions": {"models": [{"name": "first", "api_key": "private-first"}]}}))
    before = config.read_bytes()
    code, out, err = invoke(tmp_path, {"config": {"extensions": {"models": [{"name": "other", "api_key": SENTINEL}]}}})
    assert code == 1 and config.read_bytes() == before
    assert "private-first" not in out + err


def test_missing_array_sentinel_reports_typed_path(tmp_path):
    code, out, err = invoke(tmp_path, {"config": {"extensions": {"models": [{"api_key": SENTINEL}]}}})
    assert code == 1
    assert json.loads(out)["errors"][0]["path"] == ["config", "extensions", "models", 0, "api_key"]


def test_stored_sentinel_is_not_a_credential_to_preserve(tmp_path):
    config = tmp_path / "config.toml"
    config.write_text('[brain.native.extra_headers]\nx = "<unchanged>"\n')
    before = config.read_bytes()
    code, out, err = invoke(tmp_path, {"config": {"bot_name": "Changed", "brain": {"native": {"extra_headers": {"x": SENTINEL}}}}})
    assert code == 1 and config.read_bytes() == before


def test_non_utf8_current_config_is_a_safe_refusal(tmp_path):
    (tmp_path / "config.toml").write_bytes(b'private-current-value\xff')
    code, out, err = invoke(tmp_path, {"config": {}}, output_format="text")
    assert code == 1 and "private-current-value" not in out + err
