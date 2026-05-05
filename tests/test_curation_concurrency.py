"""Tests for concurrency safety (flock + sha256 re-read)."""

from __future__ import annotations

import threading
import time

import pytest

from istota.memory.curation.file_lock import MemoryMdLocked, memory_md_lock


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
