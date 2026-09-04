"""Scripting ``time.monotonic`` without rescripting the whole process's clock.

The companion to :mod:`tests.support.sleep_spy`, and wrong in the same way for
the same reason. Every module under ``src/`` does ``import time``, so
``mod.time`` **is** the stdlib module: ``monkeypatch.setattr(mod.time,
"monotonic", …)`` replaces the function process-wide, for every thread in the
xdist worker, and whatever a scheduler thread, a transcript tailer or a poller
left running by an earlier test then reads the test's clock rather than the
kernel's.

Two consequences, and the first is a false red on the patching test itself:

- **A foreign thread steals a tick.** A test that scripts a finite sequence and
  counts on the loop under test consuming it — ``lambda: next(ticks, 999.0)``
  — reaches its fallback early when somebody else reads the clock first, so
  the loop sees a jump it was never given and the assertion fails for a reason
  unrelated to the code under test.
- **A frozen clock never advances.** A background thread computing a deadline
  from ``monotonic()`` gets a time that does not move, which turns a bounded
  wait into an unbounded one for the duration of the test.

:func:`monotonic_spy` keeps the scripting and drops the reach. The calling
thread gets ``source()``; every other thread gets the real clock, so a
background thread behaves exactly as it would have.

Prefer patching the module's own interval or timeout constant where there is
one — that is module-local and reaches nothing else. Reach for this where the
test needs to drive the clock itself.
"""

from __future__ import annotations

import threading
import time as _time_module
from collections.abc import Callable
from types import ModuleType

from tests.support.sleep_spy import time_holder


def monotonic_spy(
    monkeypatch, module: ModuleType, source: Callable[[], float]
) -> None:
    """Patch ``module.time.monotonic`` to a same-thread scripted clock.

    ``module`` is the module under test; see :func:`tests.support.sleep_spy.
    time_holder` for what the patch is applied to and why the reach has to be
    bounded here rather than by the attribute path.

    ``source`` is a zero-argument callable returning the value the calling
    thread should see — a constant, a mutable cell, or a generator. It is called
    only on the calling thread, so a generator hands out exactly the ticks the
    code under test consumes.
    """
    real_monotonic = _time_module.monotonic
    owner = threading.get_ident()

    def spy() -> float:
        if threading.get_ident() != owner:
            # Somebody else's thread. Leave its clock alone: this patch is
            # process-wide whether we want it or not, so the only thing that
            # keeps it from being a change to unrelated code is delegating.
            return real_monotonic()
        return source()

    monkeypatch.setattr(time_holder(module), "monotonic", spy)
