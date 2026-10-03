"""``build_daemon_sandbox(withheld_scopes=)``: a task-less namespace that sees less.

The code reviewer reads a diff that may come from an outside contributor, so
its namespace withholds every scope: no ``{mount}/Users/{user_id}``, no
``{repos_dir}/{user_id}``, no package cache and no ``{mount}/Talk``, even for
an admin on a developer deployment, and not the shared per-user temp dir
either, since every task of the user leaves its files and deferred ops there.
The read-only run directory it does need still lands after the work dir. Each
assertion has a control: the same call without ``withheld_scopes`` binds what
the withheld call drops, so a wrap that binds nothing for any reason cannot
pass here.
"""

from __future__ import annotations

import stat
from pathlib import Path
from unittest.mock import patch

import pytest

from istota.config import DeveloperConfig
from istota.executor import (
    DAEMON_SCRATCH_DIR_NAME,
    build_daemon_sandbox,
    release_daemon_sandbox,
)

WITHHELD = frozenset({"files", "memory", "developer"})


@pytest.fixture
def _bwrap_flag_cache():
    from istota import executor

    saved = dict(executor._bwrap_flag_support)
    executor._bwrap_flag_support.clear()
    yield
    executor._bwrap_flag_support.clear()
    executor._bwrap_flag_support.update(saved)


@pytest.fixture
def dev_config(make_config, tmp_path):
    repos = tmp_path / "repos"
    config = make_config(
        developer=DeveloperConfig(enabled=True, repos_dir=str(repos)),
    )
    (repos / "alice").mkdir(parents=True)
    (config.workspace_path / "Users" / "alice").mkdir(parents=True)
    (config.workspace_path / "Talk").mkdir()
    assert config.is_admin("alice")
    return config


def _argv(sandbox) -> list[str]:
    with patch("istota.executor._bwrap_available", return_value=True):
        return sandbox.wrap(["claude"])


def _binds(argv: list[str]) -> list[tuple[int, str, str]]:
    return [
        (i, tok, argv[i + 2])
        for i, tok in enumerate(argv[:-2])
        if tok in ("--bind", "--ro-bind")
    ]


def _dests(argv: list[str]) -> set[str]:
    return {dest for _i, _flag, dest in _binds(argv)}


def _shared_user_temp(config) -> str:
    return str((config.temp_dir / "alice").resolve())


def test_withheld_scopes_drop_the_repos_workspace_and_talk_binds(
    dev_config, tmp_path, _bwrap_flag_cache
):
    run_dir = tmp_path / "review-run"
    run_dir.mkdir()
    repos = (tmp_path / "repos" / "alice").resolve()
    user_dir = (dev_config.workspace_path / "Users" / "alice").resolve()
    talk = (dev_config.workspace_path / "Talk").resolve()

    sandbox = build_daemon_sandbox(
        dev_config, "alice", extra_ro_binds=[run_dir], withheld_scopes=WITHHELD,
    )
    argv = _argv(sandbox)

    dests = _dests(argv)
    assert str(repos) not in dests
    assert not any(d.startswith(str(repos) + "/") for d in dests), (
        "the derived package cache lives inside the repos subtree"
    )
    assert str(user_dir) not in dests
    assert str(talk) not in dests
    binds = _binds(argv)
    run_idx, run_flag, _ = [b for b in binds if b[2] == str(run_dir.resolve())][-1]
    work_idx = [i for i, _f, d in binds if d == str(sandbox.work_dir)][-1]
    assert run_flag == "--ro-bind"
    assert run_idx > work_idx


def test_control_without_withheld_scopes_binds_them_all(
    dev_config, tmp_path, _bwrap_flag_cache
):
    run_dir = tmp_path / "review-run"
    run_dir.mkdir()
    repos = (tmp_path / "repos" / "alice").resolve()
    user_dir = (dev_config.workspace_path / "Users" / "alice").resolve()
    talk = (dev_config.workspace_path / "Talk").resolve()

    sandbox = build_daemon_sandbox(dev_config, "alice", extra_ro_binds=[run_dir])
    argv = _argv(sandbox)

    dests = _dests(argv)
    assert str(repos) in dests
    assert str(user_dir) in dests
    assert str(talk) in dests
    assert str(run_dir.resolve()) in dests
    assert _shared_user_temp(dev_config) in dests
    assert sandbox.work_dir == Path(_shared_user_temp(dev_config))
    assert not sandbox.scratch


def test_withheld_scopes_bind_a_private_work_dir_not_the_shared_one(
    dev_config, _bwrap_flag_cache
):
    """The per-user temp dir is every task's, deferred-op files included.

    Withholding scopes also does not move the namespace to a ``room-task-0``
    or ``emissary-task-0`` directory: ``task_temp_dir`` makes that choice and
    only ``execute_task`` calls it.
    """
    sandbox = build_daemon_sandbox(dev_config, "alice", withheld_scopes=WITHHELD)
    argv = _argv(sandbox)

    dests = _dests(argv)
    shared = _shared_user_temp(dev_config)
    assert shared not in dests
    assert not any(d.startswith(shared + "/") for d in dests)
    assert sandbox.scratch
    work = sandbox.work_dir
    assert str(work) in dests
    assert work.parent == (
        dev_config.temp_dir.resolve() / DAEMON_SCRATCH_DIR_NAME / "alice"
    )
    for level in (work, work.parent, work.parent.parent):
        assert stat.S_IMODE(level.stat().st_mode) == 0o700
    assert argv[argv.index("--chdir") + 1] == str(work)
    assert not any("room-task-" in d or "emissary-task-" in d for d in dests)


def test_each_call_gets_its_own_work_dir_and_release_removes_it(dev_config):
    first = build_daemon_sandbox(dev_config, "alice", withheld_scopes=WITHHELD)
    second = build_daemon_sandbox(dev_config, "alice", withheld_scopes=WITHHELD)
    assert first.work_dir != second.work_dir
    (first.work_dir / "left-by-the-model").write_text("x")

    release_daemon_sandbox(first)

    assert not first.work_dir.exists()
    assert second.work_dir.is_dir()


def test_release_leaves_the_shared_work_dir_alone(dev_config):
    sandbox = build_daemon_sandbox(dev_config, "alice")
    release_daemon_sandbox(sandbox)
    assert sandbox.work_dir.is_dir()


def test_a_planted_symlink_at_the_scratch_root_refuses(dev_config, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    dev_config.temp_dir.mkdir(parents=True, exist_ok=True)
    (dev_config.temp_dir / DAEMON_SCRATCH_DIR_NAME).symlink_to(elsewhere)

    sandbox = build_daemon_sandbox(dev_config, "alice", withheld_scopes=WITHHELD)

    assert sandbox.refused
    assert sandbox.wrap is None
    assert list(elsewhere.iterdir()) == []


def test_a_bare_string_is_refused_rather_than_read_as_characters(dev_config):
    sandbox = build_daemon_sandbox(dev_config, "alice", withheld_scopes="files")
    assert sandbox.refused
    assert sandbox.wrap is None
