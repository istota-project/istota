"""A restricted task's temp dir on a real kernel, raced by the user's other task.

The closure test for ISSUE-980. Alice's ordinary task runs in its real
namespace and swaps her restricted task's temp dir for a link to bob's temp dir
as fast as it can, while the restricted task's namespace is built and started
round after round with the real mount builder. Bob's directory must never
appear. The control runs the same race against the old layout, where the
directory sat inside `{temp}/alice` and was bound by name, and requires bob's
directory to appear, so the race can tell the two apart.
"""

import os
import signal
import subprocess
import sys
import time

import pytest

from istota import db
from istota.config import SecurityConfig
from istota.executor import (
    SandboxProfile,
    _bwrap_available,
    build_bwrap_cmd,
    ensure_task_temp_dir,
    get_user_temp_dir,
)

pytestmark = pytest.mark.linux

CANARY = "bob-synthetic-temp-canary"
APPROVED = "restricted-own-contents"
WITHHELD = frozenset({"files", "memory"})
ROUNDS = 120


@pytest.fixture(autouse=True)
def _requires_real_bwrap():
    if not _bwrap_available():
        if os.environ.get("ISTOTA_LINUX_TIER") == "1":
            pytest.fail("running under scripts/test-linux.sh, where this must not skip")
        pytest.skip("bwrap is not available")


@pytest.fixture
def world(tmp_path, make_config):
    db_dir = tmp_path / "app" / "data"
    db_dir.mkdir(parents=True)
    mount = tmp_path / "mount"
    (mount / "Users" / "alice").mkdir(parents=True)
    config = make_config(
        db_path=db_dir / "istota.db",
        temp_dir=tmp_path / "temp",
        workspace_path=mount,
        security=SecurityConfig(sandbox_enabled=True),
    )
    bob = get_user_temp_dir(config, "bob")
    bob.mkdir(parents=True)
    (bob / "canary.txt").write_text(CANARY)
    alice = get_user_temp_dir(config, "alice")
    alice.mkdir(parents=True)
    restricted = db.Task(
        id=42, prompt="probe", user_id="alice", source_type="email",
        status="running", conversation_token=None,
    )
    ordinary = db.Task(
        id=41, prompt="probe", user_id="alice", source_type="talk",
        status="running", conversation_token=None,
    )
    return {
        "config": config, "bob": bob, "alice": alice,
        "restricted": restricted, "ordinary": ordinary,
        "legacy": alice / "room-task-42",
    }


#: Alice's other task's swapper: an atomic exchange of each target with a link
#: to bob's temp dir, as fast as it can, so the name always holds one or the
#: other. A target whose link cannot be made beside it is out of its reach.
SWAPPER = """
import ctypes, os, sys
libc = ctypes.CDLL(None, use_errno=True)
bob, targets = sys.argv[1], sys.argv[2:]
pairs = []
for target in targets:
    link = target + ".swap"
    try:
        os.symlink(bob, link)
    except OSError:
        continue
    pairs.append((target.encode(), link.encode()))
while True:
    for a, b in pairs:
        libc.renameat2(-100, a, -100, b, 2)  # AT_FDCWD, RENAME_EXCHANGE
"""


def _swapper(world, targets):
    """Alice's ordinary task, in its real namespace, swapping each target."""
    argv = build_bwrap_cmd(
        [sys.executable, "-I", "-c", SWAPPER, str(world["bob"]), *map(str, targets)],
        world["config"], world["ordinary"], False, [], world["alice"],
        profile=SandboxProfile.NATIVE,
    )
    proc = subprocess.Popen(
        argv, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )
    time.sleep(0.5)
    assert proc.poll() is None, proc.stderr.read().decode(errors="replace")
    return proc


def _stop(proc):
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=10)


def _race(world, make_temp_dir, targets):
    """How many rounds of the restricted task read each answer, and how many
    found the old name swapped for a link, so a swapper that did nothing fails."""
    proc = _swapper(world, targets)
    seen = {"approved": 0, "canary": 0, "refused": 0, "swapped": 0}
    try:
        for _ in range(ROUNDS):
            seen["swapped"] += world["legacy"].is_symlink()
            try:
                own = make_temp_dir()
                argv = build_bwrap_cmd(
                    ["/bin/sh", "-c", "cat canary.txt approved.txt 2>/dev/null"],
                    world["config"], world["restricted"], False, [], own,
                    profile=SandboxProfile.NATIVE, withheld_scopes=WITHHELD,
                )
            except (OSError, RuntimeError, ValueError):
                seen["refused"] += 1
                continue
            result = subprocess.run(argv, capture_output=True, text=True, timeout=60)
            seen["approved"] += APPROVED in result.stdout
            seen["canary"] += CANARY in result.stdout
    finally:
        _stop(proc)
    print("race", seen)
    return seen


def test_the_ordinary_namespace_cannot_see_the_restricted_dir(world):
    own = ensure_task_temp_dir(world["config"], world["restricted"], restricted=True)
    argv = build_bwrap_cmd(
        ["/bin/sh", "-c", f'test -e "{own}" && echo visible; '
                          f'ln -s /tmp "{own}" 2>/dev/null && echo planted; echo done'],
        world["config"], world["ordinary"], False, [], world["alice"],
        profile=SandboxProfile.NATIVE,
    )

    result = subprocess.run(argv, capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert "done" in result.stdout
    assert "visible" not in result.stdout
    assert not own.is_symlink() and own.is_dir()


def test_bobs_temp_dir_never_appears_in_the_restricted_namespace(world):
    config, task = world["config"], world["restricted"]
    own = ensure_task_temp_dir(config, task, restricted=True)
    (own / "approved.txt").write_text(APPROVED)
    # Alice's other task can still make a directory at the old name and swap it.
    world["legacy"].mkdir()

    seen = _race(
        world,
        lambda: ensure_task_temp_dir(config, task, restricted=True),
        [own, world["legacy"]],
    )

    assert seen["swapped"] > 0, f"the swapper never swapped anything: {seen}"
    assert seen["canary"] == 0, f"bob's temp dir was bound: {seen}"
    assert seen["approved"] > 0, f"no round got as far as the bind: {seen}"


def test_the_same_race_against_the_old_layout_shows_bobs_dir(world):
    """The control: the directory inside `{temp}/alice`, checked, made and
    bound by name, as before ISSUE-980."""
    legacy = world["legacy"]
    legacy.mkdir()
    (legacy / "approved.txt").write_text(APPROVED)

    def old_way():
        if legacy.is_symlink():
            raise OSError("is a symlink")
        legacy.mkdir(parents=True, exist_ok=True)
        return legacy

    seen = _race(world, old_way, [legacy])

    assert seen["canary"] > 0, f"this race cannot tell a safe layout from an unsafe one: {seen}"
