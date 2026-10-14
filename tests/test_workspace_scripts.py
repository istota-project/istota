"""Script descriptions and the user-scoped scripts directory."""

import pytest

from istota import storage
from istota.config import Config


@pytest.fixture
def config(tmp_path):
    config = Config(db_path=tmp_path / "istota.db", workspace_path=tmp_path / "workspace")
    (config.workspace_path / "Users/alice").mkdir(parents=True)
    return config


def test_scripts_dir_resolves_existing_and_missing_directory(config):
    expected = config.workspace_path / "Users/alice/istota/scripts"
    assert storage.resolve_user_scripts_dir(config, "alice") == expected.resolve()
    assert not expected.exists()
    expected.mkdir(parents=True)
    assert storage.resolve_user_scripts_dir(config, "alice") == expected.resolve()


def test_scripts_dir_without_workspace():
    assert storage.resolve_user_scripts_dir(Config(), "alice") is None


@pytest.mark.parametrize("component", ["istota", "istota/scripts"])
@pytest.mark.parametrize("inside", [True, False])
def test_scripts_dir_refuses_symlinks(config, tmp_path, component, inside):
    user_root = config.workspace_path / "Users/alice"
    target = (user_root if inside else tmp_path) / "elsewhere"
    target.mkdir()
    link = user_root / component
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target, target_is_directory=True)
    assert storage.resolve_user_scripts_dir(config, "alice") is None


def test_scripts_dir_refuses_dangling_symlink(config):
    scripts = config.workspace_path / "Users/alice/istota/scripts"
    scripts.parent.mkdir()
    scripts.symlink_to(scripts.parent / "missing", target_is_directory=True)
    assert storage.resolve_user_scripts_dir(config, "alice") is None


def test_scripts_dir_accepts_symlinked_workspace(config, tmp_path):
    link = tmp_path / "workspace-link"
    link.symlink_to(config.workspace_path, target_is_directory=True)
    expected = config.workspace_path / "Users/alice/istota/scripts"
    config.workspace_path = link
    assert storage.resolve_user_scripts_dir(config, "alice") == expected.resolve()


@pytest.mark.parametrize(("head", "expected"), [
    (b"#!/usr/bin/env python3\n# Emails the weekly summary\nprint('ok')", "Emails the weekly summary"),
    (b'"""Emails the weekly summary"""\nprint("ok")', "Emails the weekly summary"),
    (b'"""\n\nEmails the weekly summary\nMore details\n"""', "Emails the weekly summary"),
    (b"r'''Emails the weekly summary'''", "Emails the weekly summary"),
    (b"# -*- coding: utf-8 -*-\n# shellcheck disable=SC2086\n# Does the work", "Does the work"),
    (b"# coding=utf-8\n# noqa\n# type: ignore\n# pyright: strict\n# vim: set ft=python\n# Does the work", "Does the work"),
    (b"#!/bin/bash\nset -euo pipefail\n# Does the work", "Does the work"),
    (b"set -e\n# Does the work", "Does the work"),
    (b"####\n#\n# Does the work", "Does the work"),
    (b"// Does   the\twork", "Does the work"),
    (b"-- Does the work", "Does the work"),
    (b"print('ok')\n# Not a description", None),
    (b"#\nprint('ok')\n# Not a description", None),
    (b'""""""\n# Not a description', None),
    (b"# Not UTF-8: \xff", None),
    (b"\x00binary", None),
    (b"\n\n", None),
])
def test_describe_script(head, expected):
    from istota.workspace.scripts import describe

    assert describe("report.py", head, kind="script") == expected


def test_describe_truncates_after_collapsing_whitespace():
    from istota.workspace.scripts import describe

    assert describe("report.py", b"# " + b"a " * 110, kind="script") == ("a " * 100) + "…"


def test_describe_prompt_heading():
    from istota.workspace.scripts import describe

    assert describe("prompts/report.md", b"\n## Summarize   this week\nDetails", kind="prompt") == "Summarize this week"


@pytest.mark.parametrize("root", [
    "", "/data/workspace", "$ISTOTA_WORKSPACE_PATH", "${ISTOTA_WORKSPACE_PATH}",
    "$NEXTCLOUD_MOUNT_PATH", "${NEXTCLOUD_MOUNT_PATH}",
])
def test_find_script_paths_absolute_forms(root):
    from pathlib import Path

    from istota.workspace.scripts import find_script_paths

    text = f'python "{root}/Users/alice/istota/scripts/reports/weekly.py"'
    assert find_script_paths(
        text, user_id="alice", bot_dir="istota", workspace_path=Path("/data/workspace"),
    ) == ["reports/weekly.py"]


@pytest.mark.parametrize("terminator", [" ", "\t", "\n", "'", '"', "`", ";", "|", "&", "<", ">", "(", ")"])
def test_find_script_paths_terminators(terminator, config):
    from istota.workspace.scripts import find_script_paths

    assert find_script_paths(
        f"/Users/alice/istota/scripts/x.py{terminator}other",
        user_id="alice", bot_dir="istota", workspace_path=config.workspace_path,
    ) == ["x.py"]


@pytest.mark.parametrize("suffix", [".", ",", ":", ".,:"])
def test_find_script_paths_trailing_punctuation(suffix, config):
    from istota.workspace.scripts import find_script_paths

    assert find_script_paths(
        f"See /Users/alice/istota/scripts/x.py{suffix}",
        user_id="alice", bot_dir="istota", workspace_path=config.workspace_path,
    ) == ["x.py"]


@pytest.mark.parametrize("text", [
    "/backup/Users/alice/istota/scripts/x.py",
    "/backup/data/workspace/Users/alice/istota/scripts/x.py",
    "relative/Users/alice/istota/scripts/x.py",
    "$OTHER/Users/alice/istota/scripts/x.py",
    "${OTHER}/Users/alice/istota/scripts/x.py",
    "/Users/bob/istota/scripts/x.py",
    "/Users/alice/other/scripts/x.py",
    "scripts/x.py",
    "/Users/alice/istota/scripts/../config/x",
    "/Users/alice/istota/scripts/lib/../x.py",
    "/Users/alice/istota/scripts/lib/..",
    "/Users/alice/istota/scripts/lib/..:",
    "/Users/alice/istota/scripts/",
    "/Users/alice/istota/scripts/./",
])
def test_find_script_paths_non_matches(text):
    from pathlib import Path

    from istota.workspace.scripts import find_script_paths

    assert find_script_paths(
        text, user_id="alice", bot_dir="istota", workspace_path=Path("/data/workspace"),
    ) == []


def test_find_script_paths_resolved_workspace(config, tmp_path):
    from istota.workspace.scripts import find_script_paths

    link = tmp_path / "workspace-link"
    link.symlink_to(config.workspace_path, target_is_directory=True)
    text = (
        f"python {link}/Users/alice/istota/scripts/linked.py && "
        f"python {link.resolve()}/Users/alice/istota/scripts/resolved.py"
    )
    assert find_script_paths(
        text, user_id="alice", bot_dir="istota", workspace_path=link,
    ) == ["linked.py", "resolved.py"]


def test_find_script_paths_normalizes_and_deduplicates(config):
    from istota.workspace.scripts import find_script_paths

    text = (
        "python /Users/alice/istota/scripts/lib/./x.py; "
        "python /Users/alice/istota/scripts/lib//x.py"
    )
    assert find_script_paths(
        text, user_id="alice", bot_dir="istota", workspace_path=config.workspace_path,
    ) == ["lib/x.py"]


def test_find_script_paths_literal_identifiers(config):
    from istota.workspace.scripts import find_script_paths

    text = (
        "/Users/alice.test/my.bot/scripts/x.py "
        "/Users/aliceXtest/myXbot/scripts/wrong.py"
    )
    assert find_script_paths(
        text, user_id="alice.test", bot_dir="my.bot", workspace_path=config.workspace_path,
    ) == ["x.py"]


def test_find_bare_mentions_existing_only():
    from istota.workspace.scripts import find_bare_mentions

    text = "Use `scripts/lib/./x.py`, then scripts/x.py. Create scripts/missing.py. Repeat scripts/x.py."
    assert find_bare_mentions(text, {"x.py", "lib/x.py", "unused.py"}) == ["lib/x.py", "x.py"]


@pytest.mark.parametrize("text", [
    "scripts/x.py.bak", "other-scripts/x.py", "other/scripts/x.py",
    "/Users/bob/istota/scripts/x.py", "scripts/lib/../x.py", "scripts/",
])
def test_find_bare_mentions_non_matches(text):
    from istota.workspace.scripts import find_bare_mentions

    assert find_bare_mentions(text, {"x.py"}) == []


@pytest.fixture
def index_tree(config):
    from istota import db
    db.init_db(config.db_path)
    root = config.workspace_path / "Users/alice/istota"
    (root / "scripts/prompts").mkdir(parents=True)
    (root / "config").mkdir()
    return root


def _write_jobs(root, jobs):
    from istota.cron_loader import generate_cron_md
    (root / "config/CRON.md").write_text(generate_cron_md(jobs))


def _index(config):
    from istota import db
    from istota.workspace.scripts import scripts_index
    with db.get_db(config.db_path) as conn:
        return scripts_index(config, conn, "alice")


def test_scripts_index_joins_files_documents_and_job_state(config, index_tree):
    from datetime import datetime
    from istota import db
    from istota.cron_loader import CronJob, sync_cron_jobs_to_db
    from istota.workspace.scripts import ScriptRef

    root = index_tree
    (root / "scripts/run.py").write_text("# Runs the report\nprint('ok')")
    (root / "scripts/orphan.sh").write_text("echo ok")
    (root / "scripts/prompts/report.txt").write_text("Use scripts/orphan.sh")
    prefix = "/Users/alice/istota/scripts/"
    jobs = [CronJob(name=name, cron="0 9 * * *", command=f"python {prefix}run.py; python {prefix}run.py")
            for name in ("active", "paused", "disabled")]
    jobs += [
        CronJob(name="file-disabled", cron="0 9 * * *", enabled=False, command=f"python {prefix}run.py"),
        CronJob(name="prompt", cron="0 9 * * *", prompt_file=prefix + "prompts/report.txt"),
        CronJob(name="mention", cron="0 9 * * *", prompt=f"Use scripts/run.py and {prefix}run.py; create {prefix}new.py"),
        CronJob(name="new", cron="0 9 * * *", command=f"python {prefix}run.py"),
    ]
    _write_jobs(root, jobs)
    (root / "config/HEARTBEAT.md").write_text('```toml\n[[checks]]\nname = "missing"\ntype = "shell-command"\ncommand = "bash /Users/alice/istota/scripts/missing.sh"\n```')
    with db.get_db(config.db_path) as conn:
        sync_cron_jobs_to_db(conn, "alice", jobs[:-1])
        rows = {job.name: job for job in db.get_user_scheduled_jobs(conn, "alice")}
        db.suspend_scheduled_job(conn, rows["paused"].id)
        db.disable_scheduled_job(conn, rows["disabled"].id)
        sync_cron_jobs_to_db(conn, "bob", [jobs[-1]])
    before = (root / "config/CRON.md").read_bytes()
    index = _index(config)
    assert index.scripts_view_path == prefix.rstrip("/")
    assert index.sources == {"scripts": "ok", "cron": "ok", "heartbeat": "ok"}
    assert not index.truncated
    assert [file.path for file in index.files] == ["orphan.sh", "prompts/report.txt", "run.py"]
    assert index.files[1].kind == "prompt"
    assert index.files[1].description == "Use scripts/orphan.sh"
    assert index.files[2].description == "Runs the report"
    assert index.files[2].size == (root / "scripts/run.py").stat().st_size
    assert datetime.fromisoformat(index.files[2].modified).utcoffset().total_seconds() == 0
    assert [(ref.name, ref.state) for ref in index.referenced_by["run.py"]] == [
        ("active", "enabled"), ("disabled", "disabled"), ("file-disabled", "disabled"),
        ("new", "not_synced"), ("paused", "suspended"),
    ]
    assert index.referenced_by["prompts/report.txt"] == [ScriptRef("cron", "prompt", "prompt_file", "prompt_file", "enabled")]
    assert index.mentioned_in["run.py"] == [ScriptRef("cron", "mention", "prompt", "mentions", "enabled")]
    assert not index.referenced_by.get("orphan.sh")
    assert not index.mentioned_in.get("orphan.sh")
    assert index.broken == {"missing.sh": [ScriptRef("heartbeat", "missing", "command", "runs", "enabled")]}
    assert (root / "config/CRON.md").read_bytes() == before


def test_scripts_index_non_admin_references(config, index_tree):
    from istota.cron_loader import CronJob
    config.admin_users = {"bob"}
    prefix = "/Users/alice/istota/scripts/"
    (index_tree / "scripts/run.sh").write_text("# Run")
    _write_jobs(index_tree, [CronJob(name="command", cron="0 9 * * *", command=f"bash {prefix}run.sh; bash {prefix}missing.sh")])
    (index_tree / "config/HEARTBEAT.md").write_text('```toml\n[[checks]]\nname = "check"\ntype = "shell-command"\ncommand = "bash /Users/alice/istota/scripts/missing.sh"\n```')
    index = _index(config)
    assert index.referenced_by["run.sh"][0].state == "admin_only"
    assert [(ref.source, ref.state) for ref in index.broken["missing.sh"]] == [("cron", "admin_only"), ("heartbeat", "admin_only")]


def test_scan_scripts_bounds_and_descriptions(index_tree, monkeypatch):
    from istota.workspace import scripts
    root = index_tree / "scripts"
    for name in (".hidden.py", "__pycache__/x.py", "node_modules/x.js", "venv/x.py", ".venv/x.py", "deep/beyond/x.py"):
        path = root / name
        path.parent.mkdir(exist_ok=True, parents=True)
        path.write_text("# Hidden")
    (root / "large.py").write_bytes(b"# Large script\n" + b"x" * scripts.DESCRIPTION_HEAD_BYTES)
    (root / "binary").write_bytes(b"\x00\xff")
    (root / "prompts/guide.md").write_text("# A prompt")
    (root / "prompts/helper.py").write_text("# A script")
    (root / "linked").symlink_to(root / "large.py")
    (root / "linked-dir").symlink_to(root / "prompts", target_is_directory=True)
    monkeypatch.setattr(scripts, "SCRIPTS_MAX_DEPTH", 1)
    files, truncated = scripts.scan_scripts(root)
    assert truncated
    assert [(f.path, f.kind, f.description) for f in files] == [
        ("binary", "script", None), ("large.py", "script", "Large script"),
        ("prompts/guide.md", "prompt", "A prompt"), ("prompts/helper.py", "script", "A script"),
    ]


def test_scripts_index_truncation_checks_unlisted_targets(config, index_tree, monkeypatch):
    from istota.cron_loader import CronJob
    from istota.workspace import scripts
    root = index_tree / "scripts"
    (root / "a.py").write_text("# First")
    (root / "z.py").write_text("# Last")
    (root / "lib").mkdir()
    (root / "link.py").symlink_to(root / "z.py")
    (root / "linked-dir").symlink_to(root / "lib", target_is_directory=True)
    prefix = "/Users/alice/istota/scripts/"
    targets = ["z.py", "lib", "link.py", "linked-dir/x.py", "missing.py"]
    _write_jobs(index_tree, [CronJob(name="check", cron="0 9 * * *", command="; ".join(prefix + p for p in targets))])
    monkeypatch.setattr(scripts, "SCRIPTS_MAX_FILES", 1)
    index = _index(config)
    assert index.truncated
    assert [f.path for f in index.files] == ["a.py"]
    assert set(index.broken) == {"link.py", "linked-dir/x.py", "missing.py"}
    assert "lib" not in index.referenced_by
    assert "z.py" in index.referenced_by


def test_scripts_index_missing_sources_and_directory(config, index_tree):
    from istota.cron_loader import CronJob
    (index_tree / "scripts/prompts").rmdir()
    (index_tree / "scripts").rmdir()
    index = _index(config)
    assert index.files == []
    assert index.sources == {"scripts": "ok", "cron": "missing", "heartbeat": "missing"}
    _write_jobs(index_tree, [CronJob(name="missing", cron="0 9 * * *", command="bash /Users/alice/istota/scripts/gone.sh")])
    assert set(_index(config).broken) == {"gone.sh"}


def test_scripts_index_refuses_scripts_symlink(config, index_tree, tmp_path):
    (index_tree / "scripts/prompts").rmdir()
    (index_tree / "scripts").rmdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.py").write_text("# Private")
    (index_tree / "scripts").symlink_to(outside, target_is_directory=True)
    index = _index(config)
    assert index.files == []
    assert index.sources["scripts"] == "refused"


@pytest.mark.parametrize("source", ["cron", "heartbeat"])
def test_scripts_index_source_failure_is_partial(config, index_tree, monkeypatch, source, caplog):
    from istota import cron_loader, heartbeat
    (index_tree / "scripts/run.py").write_text("# Run")
    (index_tree / f"config/{source.upper()}.md").write_text("present")
    module, name = (cron_loader, "load_cron_document") if source == "cron" else (heartbeat, "load_heartbeat_config")
    def fail(*args):
        raise OSError("private host path")
    monkeypatch.setattr(module, name, fail)
    index = _index(config)
    assert [f.path for f in index.files] == ["run.py"]
    assert index.sources[source] == "error"
    assert "OSError" in caplog.text
    assert "private host path" not in caplog.text


def test_scripts_index_unreadable_cron_is_error(config, index_tree):
    (index_tree / "config/CRON.md").write_bytes(b"\xff")
    assert _index(config).sources["cron"] == "error"


def test_scripts_index_without_workspace_or_valid_user(config):
    from istota.workspace.scripts import scripts_index
    assert scripts_index(Config(), None, "alice") is None
    assert scripts_index(config, None, "../bob") is None


@pytest.mark.parametrize("component", ["istota", "istota/scripts"])
def test_scripts_index_refuses_directory_swap_after_resolution(config, index_tree, tmp_path, monkeypatch, component):
    real_resolve = storage.resolve_user_scripts_dir
    user_root = index_tree.parent
    victim = user_root / component
    outside = tmp_path / "outside"
    (outside / "scripts").mkdir(parents=True)
    (outside / "scripts/private.py").write_text("# Private")
    (outside / "private.py").write_text("# Private")

    def swap(config, user_id):
        resolved = real_resolve(config, user_id)
        victim.rename(victim.with_name(victim.name + "-old"))
        victim.symlink_to(outside, target_is_directory=True)
        return resolved

    monkeypatch.setattr(storage, "resolve_user_scripts_dir", swap)
    index = _index(config)
    assert index.files == []
    assert index.sources["scripts"] == "refused"


@pytest.mark.parametrize("at_root", [True, False])
def test_scripts_index_directory_listing_failures(config, index_tree, monkeypatch, at_root):
    from istota.workspace import scripts
    from istota.workspace.listing import ListingError

    (index_tree / "scripts/run.py").write_text("# Run")
    (index_tree / "scripts/prompts/hidden.txt").write_text("Not reachable")
    real_list = scripts.list_dir
    def fail(root, parts):
        if (len(parts) == 2) == at_root:
            raise ListingError(403, "Folder refused")
        return real_list(root, parts)
    monkeypatch.setattr(scripts, "list_dir", fail)
    index = _index(config)
    if at_root:
        assert index.files == []
        assert index.sources["scripts"] == "refused"
    else:
        assert [file.path for file in index.files] == ["run.py"]
        assert index.sources["scripts"] == "ok"


def test_scripts_index_keeps_valid_jobs_from_partial_cron(config, index_tree):
    (index_tree / "scripts/run.py").write_text("# Run")
    (index_tree / "config/CRON.md").write_text(
        '```toml\n[[jobs]]\nname = "valid"\ncron = "0 9 * * *"\n'
        'command = "python /Users/alice/istota/scripts/run.py"\n'
        '[[jobs]]\nname = "refused"\ncron = "0 9 * * *"\n'
        'prompt_file = "/Users/alice/istota/scripts/missing.txt"\n```'
    )
    index = _index(config)
    assert index.sources["cron"] == "ok"
    assert [ref.name for ref in index.referenced_by["run.py"]] == ["valid"]
    assert not index.broken



def test_scan_scripts_stops_at_file_limit(index_tree, monkeypatch):
    from istota.workspace import scripts
    (index_tree / "scripts/run.py").write_text("# Run")
    monkeypatch.setattr(scripts, "SCRIPTS_MAX_FILES", 1)
    files, truncated = scripts.scan_scripts(index_tree / "scripts")
    assert [file.path for file in files] == ["run.py"]
    assert truncated


def test_scripts_index_invalid_reference_leaf(config, index_tree):
    from istota.cron_loader import CronJob
    _write_jobs(index_tree, [CronJob(name="invalid", cron="0 9 * * *", command="python /Users/alice/istota/scripts/bad\x00.py")])
    index = _index(config)
    assert index.files == []
    assert list(index.broken) == ["bad\x00.py"]


def test_scripts_index_never_trusts_a_resolved_descendant(config, index_tree, monkeypatch):
    from istota.cron_loader import CronJob

    staging = index_tree.parent / "staging"
    (staging / "istota/scripts").mkdir(parents=True)
    other_user = config.workspace_path / "Users/bob"
    (other_user / "istota/scripts").mkdir(parents=True)
    (other_user / "istota/scripts/private.py").write_text("# Private description")
    _write_jobs(index_tree, [CronJob(
        name="check", cron="0 9 * * *",
        command="python /Users/alice/istota/scripts/private.py",
    )])
    real_contained = storage._contained_under_user_root

    def swap(config, user_id, path):
        if path == index_tree / "scripts":
            path.rename(index_tree / "scripts-original")
            path.symlink_to(staging / "istota/scripts", target_is_directory=True)
            resolved = real_contained(config, user_id, path)
            staging.rename(staging.with_name("staging-original"))
            staging.symlink_to(other_user, target_is_directory=True)
            return resolved
        return real_contained(config, user_id, path)

    monkeypatch.setattr(storage, "_contained_under_user_root", swap)
    index = _index(config)
    assert index.files == []
    assert index.sources["scripts"] == "refused"
    assert not index.referenced_by


@pytest.mark.parametrize("content", [
    storage.HEARTBEAT_TEMPLATE.format(conversation_token="", user_id="alice"),
    "",
    '```toml\n[settings]\ndefault_cooldown_minutes = 60\n```',
])
def test_scripts_index_valid_empty_heartbeat(config, index_tree, content):
    (index_tree / "config/HEARTBEAT.md").write_text(content)
    assert _index(config).sources["heartbeat"] == "ok"


@pytest.mark.parametrize("content", [b"\xff", b'```toml\nchecks = [\n```'])
def test_scripts_index_unreadable_heartbeat(config, index_tree, content):
    (index_tree / "config/HEARTBEAT.md").write_bytes(content)
    assert _index(config).sources["heartbeat"] == "error"


@pytest.mark.parametrize("name", ["daily report.txt", "report;draft(1).txt", "report."])
def test_scripts_index_prompt_file_uses_complete_path(config, index_tree, name):
    from istota.cron_loader import CronJob

    (index_tree / "scripts/prompts" / name).write_text("Summarize the report")
    _write_jobs(index_tree, [CronJob(
        name="report", cron="0 9 * * *",
        prompt_file=f"/Users/alice/istota/scripts/prompts/{name}",
    )])
    index = _index(config)
    assert not index.broken
    assert index.referenced_by[f"prompts/{name}"][0].kind == "prompt_file"
