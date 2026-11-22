"""The user's config dir raced by an atomic swapper, on a real kernel (ISSUE-981).

A thread stands in for alice's task: it exchanges her `{bot}/config` with a
link to bob's, with `renameat2(RENAME_EXCHANGE)`, as fast as it can, so the name
always holds one or the other. Meanwhile the daemon's own CRON.md write and
config reads run in a loop. Bob's files must never change and never be read.
The control runs the same race against the old shape, a resolve and then a
read or write by name, and requires bob's file to be reached, so the race can
tell the two apart. `renameat2` is Linux-only, hence the tier.
"""

import ctypes
import os
import sys
import threading

import pytest

from istota import cron_loader, storage
from istota.config import Config, SecurityConfig

pytestmark = pytest.mark.linux

BOB_CANARY = "bob-synthetic-private-canary"
BOB_CRON = "# bob's jobs\n"
ROUNDS = 400
CONTROL_ROUNDS = 4000


@pytest.fixture(autouse=True)
def _requires_renameat2():
    if not sys.platform.startswith("linux"):
        if os.environ.get("ISTOTA_LINUX_TIER") == "1":
            pytest.fail("running under scripts/test-linux.sh, where this must not skip")
        pytest.skip("renameat2 is Linux-only")


@pytest.fixture
def world(tmp_path):
    mount = tmp_path / "mount"
    for user in ("alice", "bob"):
        (mount / "Users" / user / "istota" / "config").mkdir(parents=True)
    alice = mount / "Users" / "alice" / "istota" / "config"
    bob = mount / "Users" / "bob" / "istota" / "config"
    (alice / "USER.md").write_text("alice's own\n")
    (bob / "USER.md").write_text(BOB_CANARY)
    (bob / "CRON.md").write_text(BOB_CRON)
    dbf = tmp_path / "data" / "istota.db"
    dbf.parent.mkdir(parents=True)
    dbf.touch()
    config = Config(
        db_path=dbf, temp_dir=tmp_path / "temp", workspace_path=mount,
        skills_dir=tmp_path / "skills",
        security=SecurityConfig(sandbox_enabled=True),
    )
    return {"config": config, "alice": alice, "bob": bob}


class _Swapper:
    def __init__(self, target, link_to):
        self.target = os.fsencode(target)
        self.link = os.fsencode(str(target) + ".swap")
        os.symlink(link_to, self.link)
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.stop = threading.Event()
        self.swaps = 0
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            # AT_FDCWD, RENAME_EXCHANGE
            if self.libc.renameat2(-100, self.target, -100, self.link, 2) == 0:
                self.swaps += 1

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()
        # Leave `config` as the real directory.
        if os.path.islink(self.target):
            self.libc.renameat2(-100, self.target, -100, self.link, 2)


def test_the_cron_md_write_never_reaches_bob(world):
    config = world["config"]
    with _Swapper(world["alice"], world["bob"]) as swapper:
        for _ in range(ROUNDS):
            cron_loader._write_cron_md(config, "alice", [], None, externalize_prompts=False)
            assert (world["bob"] / "CRON.md").read_text() == BOB_CRON
    print("swaps", swapper.swaps)
    assert swapper.swaps > 0


def test_the_tasks_md_write_never_reaches_bob(world):
    from istota import tasks_file_poller

    config = world["config"]
    with _Swapper(world["alice"], world["bob"]) as swapper:
        for _ in range(ROUNDS):
            try:
                tasks_file_poller._write_tasks_file(
                    config, "/Users/alice/istota/config/TASKS.md",
                    "- [ ] synthetic attacker task\n", user_id="alice")
            except PermissionError:
                pass
            assert not (world["bob"] / "TASKS.md").exists()
    print("swaps", swapper.swaps)
    assert swapper.swaps > 0


def test_a_config_read_never_returns_bobs_file(world):
    config = world["config"]
    with _Swapper(world["alice"], world["bob"]) as swapper:
        for _ in range(ROUNDS):
            text = storage.read_user_config_file(config, "alice", "USER.md")
            assert text != BOB_CANARY
    print("swaps", swapper.swaps)
    assert swapper.swaps > 0


def test_the_same_race_against_a_resolve_then_open_reaches_bob(world):
    """The control: the shape this replaced, on the same race, goes red."""
    config = world["config"]
    reached = 0
    with _Swapper(world["alice"], world["bob"]):
        for _ in range(CONTROL_ROUNDS):
            directory = storage.resolve_user_config_dir(config, "alice")
            if directory is None:
                continue
            text, _reason = storage.read_regular_file(directory / "USER.md")
            if text == BOB_CANARY:
                reached += 1
                break
    print("control reached bob's file:", reached)
    assert reached > 0, "this race cannot tell a descriptor read from a read by name"
