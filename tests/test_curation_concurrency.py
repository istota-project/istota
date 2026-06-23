"""Tests for concurrency safety (flock + sha256 re-read)."""

from __future__ import annotations

import threading
import time

import pytest

from istota.memory.curation.file_lock import (
    MemoryMdLocked,
    deferred_lock_dir,
    lock_path_for,
    memory_md_lock,
)


class TestLockAnchorLocation:
    """The flock anchor must NOT be a sibling of USER.md.

    USER.md lives on the rclone/FUSE Nextcloud mount where flock is
    unreliable and stray `.lock` files clutter the user's config dir. The
    anchor lives in a local directory instead, deterministically keyed on
    the target's absolute path so two processes (nightly curator + runtime
    CLI) agree on the same anchor while different users never collide.
    """

    def test_anchor_is_not_a_sibling_of_target(self, tmp_path):
        target = tmp_path / "USER.md"
        anchor = lock_path_for(target)
        assert anchor.parent != target.parent
        assert anchor.suffix == ".lock"
        assert target.name in anchor.name

    def test_anchor_is_deterministic_for_a_target(self, tmp_path):
        target = tmp_path / "u1" / "USER.md"
        assert lock_path_for(target) == lock_path_for(target)

    def test_distinct_targets_get_distinct_anchors(self, tmp_path):
        a = tmp_path / "u1" / "USER.md"
        b = tmp_path / "u2" / "USER.md"
        assert lock_path_for(a) != lock_path_for(b)

    def test_lock_dir_override_is_honored(self, tmp_path):
        target = tmp_path / "USER.md"
        ld = tmp_path / "locks"
        anchor = lock_path_for(target, lock_dir=ld)
        assert anchor.parent == ld

    def test_lock_does_not_create_a_sibling_lock_file(self, tmp_path):
        target = tmp_path / "USER.md"
        target.write_text("seed\n")
        ld = tmp_path / "locks"
        with memory_md_lock(target, lock_dir=ld):
            pass
        # No `.lock` littered next to USER.md (i.e. on the mount).
        assert not (tmp_path / "USER.md.lock").exists()
        # The anchor lives in the local lock dir instead.
        assert list(ld.glob("USER.md.*.lock"))

    def test_deferred_lock_dir_is_under_the_task_deferred_dir(self, tmp_path):
        # The daemon writers anchor under the per-user deferred dir so the
        # curator (host) and the runtime CLI (host or sandboxed — the dir is
        # bind-mounted in) land on the same inode. Both compute the same path.
        deferred = tmp_path / "tmp-istota" / "alice"
        ld = deferred_lock_dir(deferred)
        assert ld.parent == deferred
        # Two writers given the same deferred dir + target agree on the anchor.
        target = tmp_path / "mount" / "Users" / "alice" / "istota" / "config" / "USER.md"
        assert lock_path_for(target, lock_dir=ld) == lock_path_for(target, lock_dir=ld)
        # And the anchor sits inside the deferred subtree (so it's bind-shared).
        anchor = lock_path_for(target, lock_dir=ld)
        assert deferred in anchor.parents


class TestMemoryMdLock:
    def test_serializes_two_writers(self, tmp_path):
        target = tmp_path / "USER.md"
        target.write_text("seed\n")
        order: list[str] = []

        def writer(name: str, hold_seconds: float):
            with memory_md_lock(target, timeout_seconds=5.0):
                order.append(f"{name}:enter")
                time.sleep(hold_seconds)
                order.append(f"{name}:exit")

        t1 = threading.Thread(target=writer, args=("a", 0.3))
        t2 = threading.Thread(target=writer, args=("b", 0.0))
        t1.start()
        time.sleep(0.05)  # let t1 grab the lock first
        t2.start()
        t1.join()
        t2.join()

        # The first writer's enter+exit must bracket the second writer's enter.
        assert order[0] == "a:enter"
        assert order[1] == "a:exit"
        assert order[2] == "b:enter"
        assert order[3] == "b:exit"

    def test_timeout_raises(self, tmp_path):
        target = tmp_path / "USER.md"
        target.write_text("seed\n")

        # Hold the lock in a background thread, try to acquire with short
        # timeout in the foreground.
        holder_started = threading.Event()
        release = threading.Event()

        def hold():
            with memory_md_lock(target, timeout_seconds=2.0):
                holder_started.set()
                release.wait(timeout=2.0)

        t = threading.Thread(target=hold)
        t.start()
        try:
            assert holder_started.wait(timeout=1.0)
            with pytest.raises(MemoryMdLocked):
                with memory_md_lock(target, timeout_seconds=0.3):
                    pass
        finally:
            release.set()
            t.join()
