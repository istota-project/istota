"""The scripted clock's own negative control.

Same claim as `tests/test_sleep_spy.py`, and the same reason for pinning it:
`monkeypatch.setattr(mod.time, "monotonic", …)` reaches every thread in the
worker, so a scripted clock is handed to whatever background thread an earlier
test left running. For `monotonic` the consequence is the sharper of the two —
a test that scripts a finite tick sequence and counts on the loop under test
consuming it goes red when a foreign thread takes a tick instead, and a frozen
clock hands any background deadline computation a time that never moves.
"""

from __future__ import annotations

import threading
import time
import types

from tests.support.monotonic_spy import monotonic_spy


def _module_that_imported_time() -> types.ModuleType:
    """A stand-in for any module under `src/` doing `import time`.

    Deliberately not a real product module: the property under test is about
    the stdlib module every one of them holds, not about any of their code.
    """
    mod = types.ModuleType("fake_product_module")
    mod.time = time
    return mod


def test_the_calling_thread_gets_the_scripted_clock(monkeypatch):
    mod = _module_that_imported_time()
    monotonic_spy(monkeypatch, mod, lambda: 42.0)

    assert mod.time.monotonic() == 42.0


def test_another_thread_gets_the_real_clock(monkeypatch):
    """A frozen clock off-thread is a deadline that never arrives."""
    mod = _module_that_imported_time()
    monotonic_spy(monkeypatch, mod, lambda: 42.0)
    observed: list[float] = []

    def other_thread():
        observed.append(mod.time.monotonic())

    t = threading.Thread(target=other_thread)
    t.start()
    t.join(timeout=5)

    assert not t.is_alive()
    assert observed and observed[0] != 42.0, "a foreign thread must see the real clock"


def test_another_thread_cannot_steal_a_tick(monkeypatch):
    """The failure the two generator call sites were exposed to.

    A test scripting `[0.0, 6.0, 11.0]` is counting the reads its own loop
    makes. A foreign thread reading the clock once consumes a tick the loop was
    going to see, so the loop reaches the fallback early and the test goes red
    for a reason that has nothing to do with the code under test.
    """
    mod = _module_that_imported_time()
    ticks = iter([1.0, 2.0, 3.0])
    monotonic_spy(monkeypatch, mod, lambda: next(ticks, 999.0))

    def other_thread():
        for _ in range(10):
            mod.time.monotonic()

    t = threading.Thread(target=other_thread)
    t.start()
    t.join(timeout=5)

    assert not t.is_alive()
    assert [mod.time.monotonic() for _ in range(3)] == [1.0, 2.0, 3.0]


def test_the_patch_is_undone(monkeypatch):
    """`monkeypatch` restores the stdlib function, not a copy of the module.

    Worth pinning because the reach is what makes a leak here expensive: a spy
    left installed would follow every later test in the worker, and a clock
    that does not move wedges any bounded wait rather than failing it.
    """
    real = time.monotonic
    mod = _module_that_imported_time()
    monotonic_spy(monkeypatch, mod, lambda: 0.0)
    assert time.monotonic is not real
    monkeypatch.undo()
    assert time.monotonic is real
