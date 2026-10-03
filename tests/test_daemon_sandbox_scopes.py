"""``build_daemon_sandbox(withheld_scopes=)``: a task-less namespace that sees less.

The code reviewer reads a diff that may come from an outside contributor, so
its namespace withholds every scope: no ``{mount}/Users/{user_id}``, no
``{repos_dir}/{user_id}`` and no package cache, even for an admin on a
developer deployment. The read-only run directory it does need still lands
last. Each assertion has a control: the same call without ``withheld_scopes``
binds what the withheld call drops, so a wrap that binds nothing for any
reason cannot pass here.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from istota.config import DeveloperConfig
from istota.executor import build_daemon_sandbox

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


def test_withheld_scopes_drop_the_repos_and_workspace_binds(
    dev_config, tmp_path, _bwrap_flag_cache
):
    run_dir = tmp_path / "review-run"
    run_dir.mkdir()
    repos = (tmp_path / "repos" / "alice").resolve()
    user_dir = (dev_config.workspace_path / "Users" / "alice").resolve()

    argv = _argv(build_daemon_sandbox(
        dev_config, "alice", extra_ro_binds=[run_dir], withheld_scopes=WITHHELD,
    ))

    dests = _dests(argv)
    assert str(repos) not in dests
    assert not any(d.startswith(str(repos) + "/") for d in dests), (
        "the derived package cache lives inside the repos subtree"
    )
    assert str(user_dir) not in dests
    binds = _binds(argv)
    run_idx, run_flag, _ = [b for b in binds if b[2] == str(run_dir.resolve())][-1]
    work_idx = [
        i for i, _f, d in binds
        if d == str((dev_config.temp_dir / "alice").resolve())
    ][-1]
    assert run_flag == "--ro-bind"
    assert run_idx > work_idx


def test_control_without_withheld_scopes_binds_both(
    dev_config, tmp_path, _bwrap_flag_cache
):
    run_dir = tmp_path / "review-run"
    run_dir.mkdir()
    repos = (tmp_path / "repos" / "alice").resolve()
    user_dir = (dev_config.workspace_path / "Users" / "alice").resolve()

    argv = _argv(build_daemon_sandbox(
        dev_config, "alice", extra_ro_binds=[run_dir],
    ))

    dests = _dests(argv)
    assert str(repos) in dests
    assert str(user_dir) in dests
    assert str(run_dir.resolve()) in dests


def test_withheld_scopes_keep_the_per_user_daemon_work_dir(
    dev_config, _bwrap_flag_cache
):
    """No ``room-task-0`` / ``emissary-task-0`` directory on this path.

    ``task_temp_dir`` chooses those, and only ``execute_task`` calls it; the
    daemon sandbox hands ``build_bwrap_cmd`` its own work dir, so withholding
    scopes changes the binds and not the directory the CLI runs in.
    """
    sandbox = build_daemon_sandbox(dev_config, "alice", withheld_scopes=WITHHELD)
    argv = _argv(sandbox)

    work = str((dev_config.temp_dir / "alice").resolve())
    assert sandbox.work_dir == Path(work)
    assert work in _dests(argv)
    assert not any("room-task-" in d or "emissary-task-" in d for d in _dests(argv))
    assert not (Path(work) / "room-task-0").exists()
