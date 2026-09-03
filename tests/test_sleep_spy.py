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

import pytest

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


def test_a_one_shot_hook_is_not_spent_by_another_thread(monkeypatch):
    """`on_sleep` exists for a test that changes state between two polls.

    `tests/test_cli_session.py` appends to the file the follow loop is reading,
    the first time that loop sleeps. Such a hook is one-shot, so an unpatched
    reach is the sharper hazard: a foreign thread's sleep spends the one shot,
    the loop never sees the change, and the test goes red about the loop.
    """
    mod = _module_that_imported_time()
    fired: list[str] = []

    def hook(_seconds):
        if not fired:
            fired.append("owner")

    sleep_spy(monkeypatch, mod, record=False, on_sleep=hook)

    t = threading.Thread(target=lambda: mod.time.sleep(0.01))
    t.start()
    t.join(timeout=5)

    assert not t.is_alive()
    assert fired == [], "a foreign thread's sleep must not run the hook"
    mod.time.sleep(0.0)
    assert fired == ["owner"]


def test_the_stdlib_module_may_be_passed_directly(monkeypatch):
    """`time.time` is the wall clock, so a bare `getattr` picks the wrong object.

    `istota.cli` imports `time` inside the function under test, so the stdlib
    module is the only handle a test has. Passing it must patch `sleep` on it,
    not walk into its `time` attribute.
    """
    slept = sleep_spy(monkeypatch, time)
    time.sleep(3.0)
    assert slept == [3.0]


def test_a_foreign_module_bound_to_the_name_time_is_refused(monkeypatch):
    """Silence is the one outcome a spy must not produce.

    Patching some other module's `sleep` sets an attribute nothing calls, so the
    recorder stays empty and every assertion about it passes for no reason —
    the failure this helper exists to remove, wearing its own name.
    """
    mod = types.ModuleType("module_with_a_confusing_attribute")
    mod.time = threading  # any module that is not the stdlib clock

    with pytest.raises(TypeError, match="not the stdlib time module"):
        sleep_spy(monkeypatch, mod)


def test_a_from_import_is_patched_where_the_name_actually_lives(monkeypatch):
    """`from time import sleep` binds the function on the module itself."""
    mod = types.ModuleType("module_that_did_a_from_import")
    mod.sleep = time.sleep

    slept = sleep_spy(monkeypatch, mod)
    mod.sleep(7.0)

    assert slept == [7.0]


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
