"""Recording ``time.sleep`` without recording the whole process's sleeps.

``monkeypatch.setattr(mod.time, "sleep", recorder)`` is the obvious way to
assert that a retry backed off, and it is wrong in a way that is invisible in
a single-file run. Every module under ``src/`` does ``import time``, so
``mod.time`` **is** the stdlib module: patching ``sleep`` on it replaces the
function process-wide, for every thread in the xdist worker, and a list-based
recorder then collects whatever a scheduler thread, a transcript tailer or a
poller left running by an earlier test happens to do.

Both directions of wrong follow, and the repo has one of each:

- ``assert slept == []`` — a false **red**. Any foreign sleep during the test
  fails it. Observed once in a full run, green on its own, which is the
  signature (`tests/test_tmux_transcript.py::TestParseSettled`, since fixed to
  count the loop's own condition instead).
- ``assert slept`` — a false **green**, and the worse of the two. One foreign
  sleep satisfies it whether or not the code under test slept at all, so the
  assertion cannot fail for the reason it was written.

:func:`sleep_spy` keeps the recording and drops the reach. Sleeps made on the
calling thread are recorded and skipped, so the test stays fast; sleeps made on
any other thread are delegated to the real function, so a background thread
behaves exactly as it would have. Assert on the returned list.

A no-op patch (``lambda *_: None``) has the same reach and no recorder, so it
cannot produce a false assertion of its own — it turns a background thread's
poll into a busy loop instead. Prefer patching the module's own interval
constant where there is one; use ``record=False`` here where there is not.
"""

from __future__ import annotations

import threading
import time as _time_module


def sleep_spy(monkeypatch, module, *, record: bool = True) -> list[float]:
    """Patch ``module.time.sleep`` to a same-thread recorder.

    ``module`` is the module under test (the one that did ``import time``);
    the patch is applied to the ``time`` module it holds, which is why the
    reach has to be bounded here rather than by the attribute path.

    Returns the list that same-thread sleeps are appended to. With
    ``record=False`` the list stays empty and same-thread sleeps are merely
    skipped, for a test that only wants the wait gone.
    """
    calls: list[float] = []
    real_sleep = _time_module.sleep
    owner = threading.get_ident()

    def spy(seconds: float = 0.0) -> None:
        if threading.get_ident() != owner:
            # Somebody else's thread. Leave its timing alone: this patch is
            # process-wide whether we want it or not, so the only thing that
            # keeps it from being a change to unrelated code is delegating.
            real_sleep(seconds)
            return
        if record:
            calls.append(seconds)

    monkeypatch.setattr(module.time, "sleep", spy)
    return calls
