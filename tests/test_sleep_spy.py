"""The recorder's own negative control.

`sleep_spy` exists because the obvious `monkeypatch.setattr(mod.time, "sleep",
recorder)` reaches every thread in the process, which is how a test asserting
`slept == []` goes red on somebody else's thread and a test asserting `slept`
goes green without the code under test having slept at all. The helper's whole
claim is that the reach is bounded, so that claim needs a test of its own —
otherwise it is a comment, and the two call sites are back to trusting one.
"""

from __future__ import annotations

import threading
import time
import types

from tests.support.sleep_spy import sleep_spy


def _module_that_imported_time() -> types.ModuleType:
    """A stand-in for any module under `src/` doing `import time`.

    Deliberately not a real product module: the property under test is about
    the stdlib module every one of them holds, not about any of their code.
    """
    mod = types.ModuleType("fake_product_module")
    mod.time = time
    return mod


def test_a_sleep_on_the_calling_thread_is_recorded_and_skipped(monkeypatch):
    mod = _module_that_imported_time()
    slept = sleep_spy(monkeypatch, mod)

    started = time.monotonic()
    mod.time.sleep(5.0)
    elapsed = time.monotonic() - started

    assert slept == [5.0]
    assert elapsed < 1.0, "a recorded sleep must not actually wait"


def test_another_threads_sleep_is_neither_recorded_nor_skipped(monkeypatch):
    """The half that the plain recorder gets wrong, in both directions.

    Not recorded, so a foreign sleep cannot satisfy an `assert slept` or break
    an `assert slept == []`. Not skipped either, because this patch lands on
    the stdlib module whether the test wants it to or not — turning a
    background poller's wait into a no-op is a change to unrelated code, and a
    busy loop rather than a bounded one.
    """
    mod = _module_that_imported_time()
    slept = sleep_spy(monkeypatch, mod)
    observed: list[float] = []

    def other_thread():
        started = time.monotonic()
        mod.time.sleep(0.05)
        observed.append(time.monotonic() - started)

    t = threading.Thread(target=other_thread)
    t.start()
    t.join(timeout=5)

    assert not t.is_alive()
    assert slept == [], "a foreign thread's sleep must not reach the recorder"
    assert observed and observed[0] >= 0.04, "it must still really have slept"


def test_record_false_skips_without_recording(monkeypatch):
    mod = _module_that_imported_time()
    slept = sleep_spy(monkeypatch, mod, record=False)

    started = time.monotonic()
    mod.time.sleep(5.0)

    assert slept == []
    assert time.monotonic() - started < 1.0


def test_the_patch_is_undone(monkeypatch):
    """`monkeypatch` restores the stdlib function, not a copy of the module.

    Worth pinning because the reach is what makes a leak here expensive: a spy
    left installed would follow every later test in the worker.
    """
    real = time.sleep
    mod = _module_that_imported_time()
    sleep_spy(monkeypatch, mod)
    assert time.sleep is not real
    monkeypatch.undo()
    assert time.sleep is real
