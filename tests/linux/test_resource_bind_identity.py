"""What a linked resource's bind holds on a real kernel when its name is swapped.

The closure test for ISSUE-881: build the argv with the real mount builder, swap
the resource's last component or a parent, run it, and look from inside. The
private directory must never become visible, at any point in setup.
"""

import json
import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from istota import db
from istota.config import SecurityConfig
from istota.executor import SandboxProfile, _bwrap_available, build_bwrap_cmd

pytestmark = pytest.mark.linux

CANARY = "private-canary-contents"
APPROVED = "approved-contents"


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
    parent = mount / "Shared"
    linked = parent / "docs"
    linked.mkdir(parents=True)
    (linked / "note.txt").write_text(APPROVED)
    (parent / "single.txt").write_text(APPROVED)

    private = tmp_path / "private"
    private.mkdir()
    (private / "note.txt").write_text(CANARY)
    (private / "single.txt").write_text(CANARY)
    (private / "docs").mkdir()
    (private / "docs" / "note.txt").write_text(CANARY)

    config = make_config(
        db_path=db_dir / "istota.db",
        temp_dir=tmp_path / "temp",
        workspace_path=mount,
        security=SecurityConfig(sandbox_enabled=True),
    )
    user_temp = config.temp_dir / "alice"
    user_temp.mkdir(parents=True)
    return {
        "config": config, "user_temp": user_temp, "tmp": tmp_path,
        "parent": parent, "linked": linked, "private": private,
    }


def _argv(world, script, path="Shared/docs", permissions="read", resource_type="folder"):
    task = db.Task(
        id=1, prompt="probe", user_id="alice", source_type="talk",
        status="running", conversation_token=None,
    )
    resource = db.UserResource(
        id=1, user_id="alice", resource_type=resource_type, resource_path=path,
        display_name=None, permissions=permissions,
    )
    argv = build_bwrap_cmd(
        ["/bin/sh", "-c", script], world["config"], task, False, [resource],
        world["user_temp"], profile=SandboxProfile.NATIVE,
    )
    assert "--ro-bind-fd" in argv or "--bind-fd" in argv
    return argv


def _run(argv):
    return subprocess.run(argv, capture_output=True, text=True, timeout=60)


def _swap_leaf(world):
    world["linked"].rename(world["tmp"] / "moved-leaf")
    world["linked"].symlink_to(world["private"], target_is_directory=True)


def _swap_parent(world):
    world["parent"].rename(world["tmp"] / "moved-parent")
    world["parent"].symlink_to(world["private"], target_is_directory=True)


def _dest(world):
    return world["config"].workspace_path.resolve() / "Shared" / "docs"


class TestTheBind:
    def test_a_read_only_resource_is_visible_and_not_writable(self, world):
        dest = _dest(world)
        result = _run(_argv(world, f"cat {dest}/note.txt; touch {dest}/new; echo rc=$?"))

        assert result.returncode == 0, result.stderr
        assert APPROVED in result.stdout
        assert "rc=0" not in result.stdout

    def test_a_readwrite_resource_writes_through_to_the_source(self, world):
        dest = _dest(world)
        result = _run(_argv(world, f"echo w > {dest}/new", permissions="readwrite"))

        assert result.returncode == 0, result.stderr
        assert (world["linked"] / "new").read_text() == "w\n"

    def test_a_shared_file_binds_as_a_file(self, world):
        dest = _dest(world).parent / "single.txt"
        result = _run(_argv(
            world, f"cat {dest}", path="Shared/single.txt", resource_type="shared_file",
        ))

        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == APPROVED

    def test_no_descriptor_reaches_the_sandboxed_command(self, world):
        result = _run(_argv(world, "ls -l /proc/self/fd"))

        assert result.returncode == 0, result.stderr
        assert "/Shared/docs" not in result.stdout


class TestASwapBeforeTheWalk:
    def test_a_swapped_leaf_loses_the_bind_and_shows_nothing_private(self, world):
        dest = _dest(world)
        argv = _argv(world, f"cat {dest}/note.txt; echo after")
        _swap_leaf(world)

        result = _run(argv)

        assert result.returncode == 0, result.stderr
        assert CANARY not in result.stdout
        assert "after" in result.stdout

    def test_a_swapped_parent_loses_the_bind_and_shows_nothing_private(self, world):
        dest = _dest(world)
        argv = _argv(world, f"cat {dest}/note.txt; echo after")
        _swap_parent(world)

        result = _run(argv)

        assert result.returncode == 0, result.stderr
        assert CANARY not in result.stdout
        assert "after" in result.stdout


class TestASwapDuringSetup:
    """Swap as fast as possible while the launcher and bwrap run.

    Three outcomes are allowed on any round: the approved object, no bind, or
    bwrap refusing to start. The one that is not is the canary.
    """

    ROUNDS = 150

    def _race(self, world, which, argv):
        """How many rounds of ``argv`` read each answer while ``which`` swaps."""
        target = world["linked"] if which == "leaf" else world["parent"]
        aside = world["tmp"] / f"aside-{which}"
        stop = threading.Event()

        def hammer():
            while not stop.is_set():
                try:
                    target.rename(aside)
                    target.symlink_to(world["private"], target_is_directory=True)
                    target.unlink()
                    aside.rename(target)
                except OSError:
                    pass

        worker = threading.Thread(target=hammer)
        worker.start()
        seen = {"approved": 0, "canary": 0}
        try:
            for _ in range(self.ROUNDS):
                result = _run(argv)
                seen["approved"] += APPROVED in result.stdout
                seen["canary"] += CANARY in result.stdout
        finally:
            stop.set()
            worker.join()
        return seen

    @pytest.mark.parametrize("which", ["leaf", "parent"])
    def test_the_private_object_never_becomes_visible(self, world, which):
        dest = _dest(world)
        argv = _argv(world, f"cat {dest}/note.txt 2>/dev/null")

        seen = self._race(world, which, argv)

        assert seen["canary"] == 0, "the private object was bound"
        assert seen["approved"] > 0, "no round got as far as the fd bind"

    def test_the_same_race_against_a_bind_by_name_shows_the_private_object(self, world):
        """The control: the shape this replaced, on the same race, goes red."""
        dest = _dest(world)
        argv = _argv(world, f"cat {dest}/note.txt 2>/dev/null")
        bwrap = argv[argv.index("--") + 1:]
        flag = bwrap.index("--ro-bind-fd")
        bwrap[flag:flag + 3] = ["--ro-bind", str(dest), str(dest)]

        seen = self._race(world, "leaf", bwrap)

        assert seen["canary"] > 0, "this race cannot tell a safe bind from an unsafe one"


def test_the_launcher_survives_the_harness_launcher_clearing_the_environment(world, tmp_path):
    """The CLI path: `tool_launcher` clears the env and execs what the wrap built."""
    dest = _dest(world)
    argv = _argv(world, f"/usr/bin/cat {dest}/note.txt")
    fifo = tmp_path / "pid.fifo"
    os.mkfifo(fifo)
    reader = os.open(fifo, os.O_RDONLY | os.O_NONBLOCK)
    env_file = tmp_path / "env.json"
    env_file.write_text(json.dumps({"PATH": "/nonexistent"}))
    env_file.chmod(0o600)
    try:
        result = subprocess.run(
            [
                sys.executable, "-I", "-m", "istota.sandbox.tool_launcher",
                "--report", str(Path(fifo)), "--env-file", str(env_file), "--", *argv,
            ],
            capture_output=True, text=True, timeout=60,
        )
    finally:
        os.close(reader)

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == APPROVED
