"""Portable paths use the migration walker, with explicit settings selectors."""

import copy
import io
import json
import tomllib

import pytest

from istota import cli


def test_apply_patch_runs_before_config_validation(monkeypatch, capsys, tmp_path):
    target = tmp_path / "config.toml"
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"config": {
        "workspace_path": "${WORKSPACE}/files",
        "caldav": {"password": "/old/credential"},
    }})))
    monkeypatch.setattr("sys.argv", ["istota", "-c", str(target), "apply", "-f", "-",
                                   "--patch", "${WORKSPACE}=/new", "--format", "json"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2
    captured = capsys.readouterr()
    assert not captured.err
    result = json.loads(captured.out)
    assert result["changed"]
    written = tomllib.loads(target.read_text())
    assert written["workspace_path"] == "/new/files"
    assert written["caldav"]["password"] == "/old/credential"


def test_patch_boundaries_longest_match_once_and_placeholders():
    from istota.settings_plan.paths import patch_document

    document = {"users": {"alice": {"resources": [
        {"type": "folder", "path": path} for path in
        ("/old", "/old/child/file", "/old-sibling", "relative/file", "${ROOT}/file")
    ]}}}
    changed, warnings = patch_document(document, ["/old=/next", "/old/child=/specific",
                                                   "/next=/cascaded", "${ROOT}=/literal"])
    assert [entry["path"] for entry in changed["users"]["alice"]["resources"]] == [
        "/next", "/specific/file", "/old-sibling", "relative/file", "/literal/file"]
    assert warnings == ["Path patch 3 matched no selected paths."]
    assert document["users"]["alice"]["resources"][0]["path"] == "/old"


@pytest.mark.parametrize("patches", [
    ["/old"], ["=/new"], ["/old="], ["relative=/new"], ["/old=relative"],
    ["${BAD=/new"], ["${9BAD}=/new"], ["${ROOT}tail=/new"],
    ["/old=${ROOT"], ["/old=/new", "/old/=/other"],
])
def test_invalid_patch_is_safe(patches):
    from istota.settings_plan.paths import patch_document
    from istota.settings_plan.schema import SchemaError

    with pytest.raises(SchemaError) as refused:
        patch_document({}, patches)
    assert refused.value.problem["path"][0] == "patch"
    assert all(patch not in str(refused.value) for patch in patches)


def test_root_and_equal_patches():
    from istota.settings_plan.paths import patch_document

    document = {"config": {"workspace_path": "/", "db_path": "/db/data", "temp_dir": "relative"}}
    changed, warnings = patch_document(document, ["/=/root/"])
    assert changed["config"] == {"workspace_path": "/root", "db_path": "/root/db/data", "temp_dir": "relative"}
    assert warnings == []
    restored, _ = patch_document(changed, ["/root=/"])
    assert restored == document
    assert patch_document(document, ["/=/"]) == (document, [])


def test_selectors_include_seeds_and_leave_other_strings_alone():
    from istota.settings_plan.paths import patch_document

    source = {"kind": "notes", "config": {"path": "/old/notes", "url": "/old/url"}}
    briefing = {"name": "morning", "blocks": [{"directive": "/old/directive", "sources": [source,
        {"kind": "browse", "config": {"path": "/old/not-a-file"}}]}]}
    user = {"resources": [{"type": "folder", "path": "/old/folder", "extra": {"path": "/old/extra"}}],
            "briefings": [briefing], "scheduled_jobs": [{"prompt_file": "/old/prompt", "command": "/old/cmd"}],
            "heartbeat": {"checks": [{"type": "file-watch", "path": "/old/watch"},
                                      {"type": "shell-command", "command": "/old/shell", "path": "/old/arbitrary"}]}}
    document = {"meta": {"source": "/old/meta"}, "users": {"alice": user}, "config": {
        "users": {"alice": user}, "default_briefings": [briefing],
        "briefing_shared_blocks": [{"sources": [source]}],
        "workspace_mount_path": "/old/mount", "nextcloud_mount_path": "/old/alias",
        "scheduler": {"db_backup_dir": "/old/backups"},
        "brain": {"native": {"session_log": {"dir": "/old/logs"}, "extra_headers": {"path": "/old/private"}}},
        "caldav": {"password": "/old/private"}, "custom_system_prompt": "/old/prompt text",
    }}
    original = copy.deepcopy(document)
    changed, warnings = patch_document(document, ["/old=/new"])
    assert not warnings and document == original
    for node in (changed["users"]["alice"], changed["config"]["users"]["alice"]):
        assert node["resources"][0]["path"] == "/new/folder"
        assert node["resources"][0]["extra"]["path"] == "/old/extra"
        assert node["briefings"][0]["blocks"][0]["sources"][0]["config"] == {"path": "/new/notes", "url": "/old/url"}
        assert node["briefings"][0]["blocks"][0]["sources"][1]["config"]["path"] == "/old/not-a-file"
        assert node["scheduled_jobs"][0] == {"prompt_file": "/new/prompt", "command": "/old/cmd"}
        assert node["heartbeat"]["checks"][0]["path"] == "/new/watch"
        assert node["heartbeat"]["checks"][1] == user["heartbeat"]["checks"][1]
    assert changed["config"]["default_briefings"][0]["blocks"][0]["sources"][0]["config"]["path"] == "/new/notes"
    assert changed["config"]["briefing_shared_blocks"][0]["sources"][0]["config"]["path"] == "/new/notes"
    assert changed["config"]["brain"]["native"]["session_log"]["dir"] == "/new/logs"
    assert changed["config"]["brain"]["native"]["extra_headers"] == original["config"]["brain"]["native"]["extra_headers"]
    assert changed["config"]["workspace_mount_path"] == "/new/mount"
    assert changed["config"]["nextcloud_mount_path"] == "/new/alias"
    assert changed["config"]["scheduler"]["db_backup_dir"] == "/new/backups"
    assert changed["config"]["caldav"] == original["config"]["caldav"]
    assert changed["config"]["custom_system_prompt"] == "/old/prompt text"
    assert changed["meta"] == original["meta"]


def test_migration_reexports_the_shared_all_string_walker():
    from istota.lib import path_rebase
    from istota.maintenance import bare_metal_import

    assert bare_metal_import.rebase_json is path_rebase.rebase_json
    assert bare_metal_import.rebase_path is path_rebase.rebase_path
    value = {"/old/key": ["/old/value", {"arbitrary": "/old/nested"}, 1]}
    rebased, count = path_rebase.rebase_json(value, [("/old", "/new")])
    assert rebased == {"/old/key": ["/new/value", {"arbitrary": "/new/nested"}, 1]}
    assert count == 2


def test_collapsing_resource_identity_is_refused_by_check(monkeypatch, capsys):
    document = {"users": {"alice": {"resources": [
        {"type": "folder", "path": "/old/data"}, {"type": "folder", "path": "/new/data"}]}}}
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(document)))
    monkeypatch.setattr("sys.argv", ["istota", "apply", "--check", "--json", "-f", "-", "--patch", "/old=/new"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    result = json.loads(capsys.readouterr().out)
    assert "duplicate" in result["problems"][0]["message"]


def test_config_path_selectors_cover_declared_types_and_migration_tables():
    import dataclasses
    import typing
    from pathlib import Path

    from istota.config import Config, _NOT_CONFIGURATION
    from istota.maintenance.bare_metal_import import REBASED_KEYS
    from istota.settings_plan.paths import CONFIG_PATH_FIELDS

    def path_fields(owner, prefix=()):
        found = set()
        for name, declared in typing.get_type_hints(owner).items():
            if not prefix and name in _NOT_CONFIGURATION:
                continue
            path = (*prefix, name)
            if declared is Path or Path in typing.get_args(declared):
                found.add(path)
            elif dataclasses.is_dataclass(declared):
                found.update(path_fields(declared, path))
        return found

    assert path_fields(Config) <= CONFIG_PATH_FIELDS
    assert set(REBASED_KEYS) <= CONFIG_PATH_FIELDS


@pytest.mark.parametrize("kind", ["todos", "reminders", "notes"])
def test_snapshot_path_redaction_round_trip_is_literal(kind, monkeypatch):
    from istota.settings_plan.paths import patch_document

    monkeypatch.setenv("WORKSPACE_ROOT", "/environment-must-not-be-read")
    document = {"users": {"alice": {"briefings": [{"blocks": [{"sources": [
        {"kind": kind, "config": {"path": "/data/workspace/file"}},
        {"kind": kind, "config": {"path": "relative/file"}},
    ]}]}]}}}
    redacted, warnings = patch_document(document, ["/data/workspace=${WORKSPACE_ROOT}"])
    assert not warnings
    sources = redacted["users"]["alice"]["briefings"][0]["blocks"][0]["sources"]
    assert sources[0]["config"]["path"] == "${WORKSPACE_ROOT}/file"
    assert sources[1]["config"]["path"] == "relative/file"
    restored, warnings = patch_document(redacted, ["${WORKSPACE_ROOT}=/data/workspace"])
    assert restored == document and not warnings


def test_cli_repeated_patches_report_unmatched_sources(monkeypatch, capsys, tmp_path):
    target = tmp_path / "config.toml"
    monkeypatch.setattr("sys.stdin", io.StringIO('{"config": {"workspace_path": "/old/child"}}'))
    monkeypatch.setattr("sys.argv", ["istota", "-c", str(target), "apply", "-f", "-", "--dry-run",
        "--patch", "/old=/new", "--patch", "/new=/must-not-cascade", "--format", "json"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 2
    report = json.loads(capsys.readouterr().out)
    assert report["warnings"] == ["Path patch 2 matched no selected paths."]
    assert not target.exists()


def test_bad_patch_uses_the_check_envelope(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO('{"config": {}}'))
    monkeypatch.setattr("sys.argv", ["istota", "apply", "--check", "--json", "-f", "-",
                                   "--patch", "private-relative-input=/new"])
    with pytest.raises(SystemExit) as stopped:
        cli.main()
    assert stopped.value.code == 1
    captured = capsys.readouterr()
    result = json.loads(captured.out)
    assert result["problems"][0]["path"] == ["patch", 0]
    assert "private-relative-input" not in captured.out + captured.err
