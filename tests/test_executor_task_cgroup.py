"""A6 — the executor's per-task cgroup lifecycle, through the real seam.

``tests/test_task_cgroup.py`` covers the module in isolation. What it cannot
show is that ``execute_task`` actually calls it: creates the cgroup before the
brain runs, places the pid the brain reports, and gives the directory back on
every exit path including the failing ones. That wiring is the whole feature —
a module that works perfectly and is never called contains nothing — and it is
also what the "fail open" rule is most likely to have quietly disabled.

The cgroup root is a directory under ``tmp_path``, reached by pointing
``resolve_root`` at it. Everything below that is real: real directory creation,
real limit files, real removal.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota import task_cgroup
from istota.executor import execute_task

from .test_task_cgroup import _kernelish_rmdir
from .test_executor_streaming import (
    _EXECUTOR_PATCH_RETURNS,
    _EXECUTOR_PATCHES,
    _make_config,
    _make_task,
    contextmanager_chain,
)


@pytest.fixture(autouse=True)
def _fresh_log_state():
    task_cgroup._reset_log_state()
    yield
    task_cgroup._reset_log_state()


@pytest.fixture
def cgroup_root(tmp_path: Path) -> Path:
    root = tmp_path / "cgroup" / "istota-scheduler.service"
    root.mkdir(parents=True)
    return root


def _patches(cgroup_root: Path, *, stdout: str = "done", returncode: int = 0):
    result = MagicMock()
    result.stdout = stdout
    result.stderr = ""
    result.returncode = returncode
    return [
        patch(name, return_value=ret)
        for name, ret in zip(_EXECUTOR_PATCHES, _EXECUTOR_PATCH_RETURNS)
    ] + [
        patch("istota.executor.subprocess.run", return_value=result),
        patch("istota.task_cgroup.resolve_root", return_value=cgroup_root),
        # cgroupfs semantics for `rmdir`, shared with tests/test_task_cgroup.py.
        # Patching `rmdir` rather than `destroy` itself matters here: the real
        # `destroy` now kills a cgroup that still holds processes, and stubbing
        # the whole function would mean the executor path never exercises it.
        patch.object(Path, "rmdir", _kernelish_rmdir),
    ]


def test_creates_the_cgroup_with_the_configured_limits_and_removes_it(
    tmp_path, cgroup_root
):
    config = _make_config(tmp_path)
    config.scheduler.task_memory_max_mb = 1024
    config.scheduler.task_pids_max = 128
    config.scheduler.task_cpu_max_percent = 150
    task = _make_task(id=77)

    seen = {}

    real_create = task_cgroup.create

    def record(task_id, limits, **kwargs):
        path = real_create(task_id, limits, **kwargs)
        # Read the limits back while the cgroup still exists. By the time
        # execute_task returns, the directory is gone — which is the other half
        # of what this test asserts.
        seen["path"] = path
        seen["memory.max"] = (path / "memory.max").read_text().strip()
        seen["pids.max"] = (path / "pids.max").read_text().strip()
        seen["cpu.max"] = (path / "cpu.max").read_text().strip()
        return path

    with contextmanager_chain(
        _patches(cgroup_root) + [patch("istota.task_cgroup.create", side_effect=record)]
    ):
        success, _result, _actions, _trace = execute_task(task, config, [])

    assert success is True
    # The attempt is part of the name: a retry reuses the task row, so the id
    # alone would land attempt 2 in the directory attempt 1 left behind.
    assert seen["path"] == cgroup_root / "task-77-0"
    assert seen["memory.max"] == str(1024 * 1024 * 1024)
    assert seen["pids.max"] == "128"
    assert seen["cpu.max"] == "150000 100000"
    # Given back on the way out, so a long-lived daemon does not accumulate one
    # directory per task it ever ran.
    assert not (cgroup_root / "task-77-0").exists()


def test_the_cgroup_is_removed_when_the_task_fails(tmp_path, cgroup_root):
    # Cleanup hangs off the executor's ExitStack rather than a success branch,
    # so every exit path gives the directory back. A failing task is the one
    # most likely to have been left out.
    config = _make_config(tmp_path)
    task = _make_task(id=78)

    with contextmanager_chain(_patches(cgroup_root, stdout="", returncode=1)):
        success, _result, _actions, _trace = execute_task(task, config, [])

    assert success is False
    assert not (cgroup_root / "task-78-0").exists()
    assert [p.name for p in cgroup_root.iterdir() if p.is_dir()] == []


def test_the_brains_pid_is_placed_in_the_cgroup(tmp_path, cgroup_root):
    # `on_pid` is the placement seam for every brain that spawns one long-lived
    # child. Asserting on the file rather than on a mock call is what makes this
    # fail against a wiring that creates the cgroup and never puts anyone in it.
    config = _make_config(tmp_path)
    task = _make_task(id=79)
    placed = {}

    real_place = task_cgroup.place

    def record(pid, path):
        ok = real_place(pid, path)
        placed["pid"] = (path / "cgroup.procs").read_text().strip()
        return ok

    def call_on_pid(req):
        # Stand in for a brain: report a pid the way ClaudeCodeBrain does.
        req.on_pid(4242)
        return MagicMock(
            success=True, output="ok", stop_reason="ok", actions=[], trace=[],
            usage=None, session_id=None, cost_usd=None,
        )

    with contextmanager_chain(
        _patches(cgroup_root) + [patch("istota.task_cgroup.place", side_effect=record)]
    ):
        with patch("istota.executor.make_brain") as make_brain:
            brain = MagicMock()
            brain.execute.side_effect = call_on_pid
            brain.model_namespace = "anthropic"
            brain.resolve_model_name.side_effect = lambda m: m or "model"
            brain.supports_steering = False
            make_brain.return_value = brain
            execute_task(task, config, [])

    assert placed["pid"] == "4242"


def test_nothing_is_created_when_the_feature_is_off(tmp_path, cgroup_root):
    # The regression proof that the switch is a switch: with it off, the
    # executor must not touch the cgroup tree at all.
    config = _make_config(tmp_path)
    config.scheduler.task_cgroup_enabled = False
    task = _make_task(id=80)

    with contextmanager_chain(_patches(cgroup_root)):
        success, _result, _actions, _trace = execute_task(task, config, [])

    assert success is True
    assert list(cgroup_root.iterdir()) == []


def test_an_oom_kill_is_named_before_the_counters_are_removed(
    tmp_path, cgroup_root, caplog
):
    """``memory.events`` has to be read before ``rmdir`` takes it away.

    Otherwise the feature's first visible effect on a real host is that builds
    and suites which used to pass start failing, reported as an opaque killed
    child with nothing anywhere mentioning a cap — the task is told it died and
    the operator has no way to find out why.
    """
    import logging

    config = _make_config(tmp_path)
    task = _make_task(id=82)

    real_create = task_cgroup.create

    def create_with_an_oom(task_id, limits, **kwargs):
        path = real_create(task_id, limits, **kwargs)
        # What the kernel would have left behind after killing the tree.
        (path / "memory.events").write_text("low 0\nhigh 4\nmax 9\noom 1\noom_kill 3\n")
        return path

    with contextmanager_chain(
        _patches(cgroup_root)
        + [patch("istota.task_cgroup.create", side_effect=create_with_an_oom)]
    ):
        with caplog.at_level(logging.WARNING, logger="istota.executor"):
            execute_task(task, config, [])

    assert "OOM-killed" in caplog.text
    assert "task_memory_max_mb" in caplog.text
    assert not (cgroup_root / "task-82-0").exists()


def test_the_cgroup_is_released_when_the_run_raises_before_the_exit_stack(
    tmp_path, cgroup_root
):
    """The cgroup is created ~200 lines before the ExitStack that cleans it up.

    Everything in between — brain construction, model resolution, the
    BrainRequest itself — is inside the function-wide try, whose handler
    returns without the stack ever being entered. A raise there leaked the
    directory until the next daemon start.
    """
    config = _make_config(tmp_path)
    task = _make_task(id=83)

    with contextmanager_chain(_patches(cgroup_root)):
        with patch("istota.executor.make_brain", side_effect=RuntimeError("boom")):
            success, output, _actions, _trace = execute_task(task, config, [])

    assert success is False
    assert "boom" in output
    assert [p.name for p in cgroup_root.iterdir() if p.is_dir()] == []


def test_a_task_still_runs_when_no_cgroup_can_be_created(tmp_path):
    # The fail-open assertion at the executor level: a deployment that has not
    # applied `Delegate=` resolves no root, and the task must run regardless.
    config = _make_config(tmp_path)
    task = _make_task(id=81)

    patches = [
        patch(name, return_value=ret)
        for name, ret in zip(_EXECUTOR_PATCHES, _EXECUTOR_PATCH_RETURNS)
    ]
    result = MagicMock()
    result.stdout = "ran anyway"
    result.stderr = ""
    result.returncode = 0
    patches += [
        patch("istota.executor.subprocess.run", return_value=result),
        patch("istota.task_cgroup.resolve_root", return_value=None),
    ]

    with contextmanager_chain(patches):
        success, output, _actions, _trace = execute_task(task, config, [])

    assert success is True
    assert output == "ran anyway"
