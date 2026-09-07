"""Give a drift guard the source dependency testmon cannot see for itself.

testmon decides which tests a change affects from the source lines each test
*executed*. A guard that asserts against ``inspect.getsource(...)`` — reading a
function's text to prove a deleted pattern has not regrown — executes none of
the lines it reads, so testmon records no dependency on them and
``scripts/qt`` never selects it. It comes back green because it did not run
(ISSUE-459).

``source_of`` is ``inspect.getsource`` plus that missing dependency: it records
the line range it read against the running test, and ``install()`` patches
testmon's collector to merge those ranges into the coverage it is about to
store. testmon turns covered lines into per-block checksums, so a guard over
one function ends up depending on that function's block alone, and a guard over
a whole module on every block in it.

Without testmon there is nothing to record against, so ``source_of`` records
nothing and is exactly ``inspect.getsource``.
"""

from __future__ import annotations

import inspect
import os
import warnings
from typing import Any

#: nodeid -> absolute filename -> lines read. Emptied by the patched collector
#: every time testmon stores a batch, so it holds one batch at most.
_pending: dict[str, dict[str, set[int]]] = {}

_installed = False


def _current_nodeid() -> str | None:
    """The running test, in the spelling testmon keys its coverage contexts by.

    pytest exports ``<nodeid> (setup|call|teardown)`` for the duration of each
    phase; testmon's context is the bare nodeid (`item.nodeid`).
    """
    raw = os.environ.get("PYTEST_CURRENT_TEST")
    if not raw:
        return None
    return raw.rsplit(" (", 1)[0]


def source_of(obj: Any) -> str:
    """``inspect.getsource(obj)``, with the dependency on it recorded."""
    # `getsourcelines` unwraps a decorated function and `getsourcefile` does
    # not, so unwrap once here and ask both about the same object. Reading a
    # `@contextmanager` function otherwise records a dependency on contextlib —
    # measured, and it is the shape half this repo's drift guards read.
    target = inspect.unwrap(obj)
    lines, start = inspect.getsourcelines(target)
    text = "".join(lines)
    filename = inspect.getsourcefile(target)
    if not _installed or not filename:
        return text

    nodeid = _current_nodeid()
    if nodeid is None:
        # Read at import or collection time, which no drift guard has reason to
        # do. Said out loud because the alternative is the ISSUE-459 behaviour
        # back with nothing reporting it.
        warnings.warn(
            f"source_of({getattr(obj, '__name__', obj)!r}) ran outside a test "
            f"phase, so testmon records no dependency on what it read",
            stacklevel=2,
        )
        return text

    # `realpath`, because the filename has to survive a relpath against
    # testmon's rootdir and testmon canonicalizes its own side that way
    # (coverage's `abs_file`). Resolving only one of the two turns a worktree
    # reached through a symlink into a `../…` name that `_merge` then drops —
    # silently, which is ISSUE-459 over again.
    # A module comes back starting at line 0; every other tool counts from 1.
    first = max(start, 1)
    read = _pending.setdefault(nodeid, {}).setdefault(os.path.realpath(filename), set())
    read.update(range(first, first + len(lines)))
    return text


def _merge(nodes_files_lines: dict, rootdir: str, pending: dict) -> None:
    """Fold what was read into the coverage testmon is about to fingerprint.

    Takes `pending` rather than reaching for the module global, so a test can
    exercise it without clearing what other tests in the same batch recorded.
    """
    # A flush carries every test in the batch, so anything recorded and absent
    # from it is a test testmon dropped — an interrupted one. Discarded with the
    # rest rather than held, since nothing will ever ask for it again.
    for nodeid, files in pending.items():
        if nodeid not in nodes_files_lines:
            continue
        for filename, lines in files.items():
            # The spelling testmon stores and reads back (`cached_relpath`).
            relname = os.path.relpath(filename, rootdir).replace(os.sep, "/")
            # Anything outside the rootdir it cannot store, so drop it rather
            # than write a `../…` name into the data file. The venv is normally
            # *inside* the tree, so a guard over an installed package — the one
            # over xdist's plugin — does get a dependency recorded, on a file
            # testmon otherwise omits from coverage. That is the wanted answer:
            # the guard exists to fail when xdist stops reading the variable.
            if relname.startswith(".."):
                continue
            nodes_files_lines[nodeid].setdefault(relname, set()).update(lines)
    pending.clear()


def install() -> bool:
    """Patch testmon's collector to store what `source_of` read.

    `get_batch_coverage_data` is the one seam: both of testmon's call sites feed
    its return value straight into `get_tests_fingerprints`, which is where a
    set of covered lines becomes the block checksums a later run compares
    against. Returns whether testmon is installed at all.

    A testmon that renamed the method fails loudly here, at conftest import. One
    that changed the shape underneath raises inside a `pytest_runtest_makereport`
    hookwrapper, which is an INTERNALERROR — the case `scripts/qt` already
    catches and answers by rerunning the suite without testmon, so it cannot
    read as a green run. Nothing is swallowed here: a merge that silently did
    nothing is the defect this closes.
    """
    global _installed

    try:
        from testmon import testmon_core
    except ImportError:
        return False

    if _installed:
        return True

    original = testmon_core.TestmonCollector.get_batch_coverage_data

    def get_batch_coverage_data(self):
        nodes_files_lines = original(self)
        if nodes_files_lines:
            _merge(nodes_files_lines, self.rootdir, _pending)
        return nodes_files_lines

    testmon_core.TestmonCollector.get_batch_coverage_data = get_batch_coverage_data
    _installed = True
    return True
