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
    SandboxProfile,
    build_daemon_sandbox,
    release_daemon_sandbox,
)

NATIVE = SandboxProfile.NATIVE

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
    config.nextcloud.url = "https://cloud.example.com"
    config.workspace_mount_path = config.workspace_path
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
        profile=NATIVE,
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

    sandbox = build_daemon_sandbox(dev_config, "alice", extra_ro_binds=[run_dir], profile=NATIVE)
    argv = _argv(sandbox)

    dests = _dests(argv)
    assert str(repos) in dests
    assert str(user_dir) in dests
    assert str(talk) not in dests  # ISSUE-880: never bound
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
    sandbox = build_daemon_sandbox(dev_config, "alice", withheld_scopes=WITHHELD, profile=NATIVE)
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
    first = build_daemon_sandbox(dev_config, "alice", withheld_scopes=WITHHELD, profile=NATIVE)
    second = build_daemon_sandbox(dev_config, "alice", withheld_scopes=WITHHELD, profile=NATIVE)
    assert first.work_dir != second.work_dir
    (first.work_dir / "left-by-the-model").write_text("x")

    release_daemon_sandbox(first)

    assert not first.work_dir.exists()
    assert second.work_dir.is_dir()


def test_release_leaves_the_shared_work_dir_alone(dev_config):
    sandbox = build_daemon_sandbox(dev_config, "alice", profile=NATIVE)
    release_daemon_sandbox(sandbox)
    assert sandbox.work_dir.is_dir()


def test_a_planted_symlink_at_the_scratch_root_refuses(dev_config, tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    dev_config.temp_dir.mkdir(parents=True, exist_ok=True)
    (dev_config.temp_dir / DAEMON_SCRATCH_DIR_NAME).symlink_to(elsewhere)

    sandbox = build_daemon_sandbox(dev_config, "alice", withheld_scopes=WITHHELD, profile=NATIVE)

    assert sandbox.refused
    assert sandbox.wrap is None
    assert list(elsewhere.iterdir()) == []


def test_a_bare_string_is_refused_rather_than_read_as_characters(dev_config):
    sandbox = build_daemon_sandbox(dev_config, "alice", withheld_scopes="files", profile=NATIVE)
    assert sandbox.refused
    assert sandbox.wrap is None
    assert sandbox.tool_server_wrap is None


# -- the profile -----------------------------------------------------------------


def test_the_profile_is_required(dev_config):
    """As on `build_bwrap_cmd`: a forgotten profile is a TypeError, not CLAUDE."""
    with pytest.raises(TypeError):
        build_daemon_sandbox(dev_config, "alice")


@pytest.mark.parametrize("profile", list(SandboxProfile))
def test_the_profile_reaches_the_bwrap_builder(dev_config, monkeypatch, profile):
    from istota import executor

    seen = {}

    def _fake(cmd, *args, **kwargs):
        seen.update(kwargs)
        return cmd

    monkeypatch.setattr(executor, "build_bwrap_cmd", _fake)
    build_daemon_sandbox(dev_config, "alice", profile=profile).wrap(["x"])
    assert seen["profile"] is profile


def test_the_tool_server_wrap_is_the_wrap_when_one_was_built(dev_config):
    sandbox = build_daemon_sandbox(dev_config, "alice", profile=NATIVE)
    assert sandbox.wrap is not None
    assert sandbox.tool_server_wrap is sandbox.wrap


def test_the_tool_server_wrap_is_the_identity_with_the_sandbox_off(dev_config):
    """Not `None`: the harness refuses roots with no wrap, and a direct caller
    always names its roots. Nothing is sandboxed here, so nothing wraps."""
    dev_config.security.sandbox_enabled = False
    sandbox = build_daemon_sandbox(dev_config, "alice", profile=NATIVE)
    assert sandbox.wrap is None and not sandbox.refused
    with patch("istota.executor._bwrap_available", return_value=True):
        assert sandbox.tool_server_wrap(["tool-server"]) == ["tool-server"]


def test_a_refused_sandbox_has_no_tool_server_wrap(dev_config):
    sandbox = build_daemon_sandbox(dev_config, "", profile=NATIVE)
    assert sandbox.refused
    assert sandbox.tool_server_wrap is None


# ---- The control dir a direct caller's tool server reads its hello from (#862)


def _control_root(config) -> Path:
    return config.temp_dir.resolve() / ".control" / "alice"


@pytest.mark.parametrize("withheld", [frozenset(), WITHHELD], ids=["plain", "withheld"])
def test_a_wrapped_direct_call_gets_a_private_control_dir_bound_read_only(
    dev_config, _bwrap_flag_cache, withheld,
):
    """The CLI-spawned tool server reads its hello and gate from a directory
    the namespace binds read-only and no task can write: a fresh one per call,
    beside the task control dirs, removed on release."""
    sandbox = build_daemon_sandbox(
        dev_config, "alice", withheld_scopes=withheld, profile=NATIVE,
    )
    control = sandbox.control_dir
    try:
        assert control is not None
        assert control.parent == _control_root(dev_config)
        assert control.name.startswith("daemon-")
        for level in (control, control.parent, control.parent.parent):
            assert stat.S_IMODE(level.lstat().st_mode) == 0o700
        binds = _binds(_argv(sandbox))
        flags = [flag for _i, flag, dest in binds if dest == str(control)]
        assert flags == ["--ro-bind"]
        # Bound after the work dir, so nothing writable covers it.
        control_idx = [i for i, _f, d in binds if d == str(control)][-1]
        work_idx = [i for i, _f, d in binds if d == str(sandbox.work_dir)][-1]
        assert control_idx > work_idx
    finally:
        release_daemon_sandbox(sandbox)
    assert not control.exists()
    assert _control_root(dev_config).is_dir(), "release removes only its own dir"


def test_two_direct_calls_get_two_control_dirs(dev_config):
    first = build_daemon_sandbox(dev_config, "alice", profile=NATIVE)
    second = build_daemon_sandbox(dev_config, "alice", profile=NATIVE)
    try:
        assert first.control_dir != second.control_dir
    finally:
        release_daemon_sandbox(first)
        release_daemon_sandbox(second)


def test_no_control_dir_with_the_sandbox_off(dev_config):
    """Nothing is wrapped, so the harness keeps its files in its own dir."""
    dev_config.security.sandbox_enabled = False
    sandbox = build_daemon_sandbox(dev_config, "alice", profile=NATIVE)
    assert sandbox.control_dir is None
    assert not (dev_config.temp_dir / ".control").exists()


def test_a_control_root_that_is_not_the_daemons_refuses_the_namespace(
    dev_config, tmp_path,
):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    dev_config.temp_dir.mkdir(parents=True, exist_ok=True)
    (dev_config.temp_dir / ".control").symlink_to(elsewhere)

    sandbox = build_daemon_sandbox(dev_config, "alice", profile=NATIVE)

    assert sandbox.refused and sandbox.wrap is None
    assert sandbox.control_dir is None
    assert list(elsewhere.iterdir()) == []
