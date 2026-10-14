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
