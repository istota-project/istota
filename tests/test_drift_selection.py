"""The incremental run must select the guards that read source text.

ISSUE-459: testmon decides which tests a change affects from the source lines
each test *executed*, and a drift guard asserting against
``inspect.getsource(...)`` — reading a function's text to prove a deleted
pattern has not regrown — executes none of the lines it reads. So every such
guard was invisible to ``scripts/qt``: green because it never ran, red only
when invoked by name. Found in the duplicate code consolidation, which added
about a dozen of them.

Measured rather than asserted structurally. A throwaway project holds one
module and three tests over it: one guard reading the module through
``tests.support.drift.source_of``, one reading it through raw
``inspect.getsource``, and one that actually calls the function. Build
testmon's data, change the function's body, and see which of the three the
second run selects.

The raw guard is the negative control. It stays invisible — that is the defect,
unpatched, and it is what makes the helper's result mean anything.
"""

from __future__ import annotations

import ast
import os
import re
import subprocess
from pathlib import Path

import pytest

from tests.support import drift
from tests.support.nested_pytest import run_nested_pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_MODULE_BEFORE = '''\
from contextlib import contextmanager


def target():
    return "before"


@contextmanager
def decorated():
    yield "before"
'''

# Both bodies, so each guard below is answered by a change to the block it
# reads rather than by a neighbour's.
_MODULE_AFTER = _MODULE_BEFORE.replace('"before"', '"after!"')

_CONFTEST = '''\
import sys

sys.path.insert(0, {repo!r})

from tests.support import drift

drift.install()
'''

_GUARDS = '''\
import inspect

import mymod

from tests.support.drift import source_of


def test_guard_through_the_helper():
    assert "def target" in source_of(mymod.target)


def test_guard_on_a_decorated_function():
    assert "yield" in source_of(mymod.decorated)


def test_guard_through_raw_getsource():
    assert "def target" in inspect.getsource(mymod.target)


def test_that_actually_calls_it():
    assert mymod.target().startswith(("before", "after"))
'''


def _run(project: Path, *extra: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    # The child builds its own data file in its own rootdir; nothing about this
    # repo's incremental run may leak into it. `PYTEST_ADDOPTS` matters most:
    # one carrying `-m` switches testmon's selection off entirely, which would
    # run all four tests and turn the negative control below red for a reason
    # that has nothing to do with the code.
    # `TESTMON_DATAFILE` is the other one: an absolute value inherited from the
    # parent has the throwaway project writing into the developer's data file.
    env.pop("ISTOTA_DESELECT_TIERS", None)
    env.pop("TESTMON_DATAFILE", None)
    env["PYTEST_ADDOPTS"] = ""
    # Forced rather than scrubbed, and it is the regression test for ISSUE-493.
    # `_reported` scrapes the child's *terminal rendering*, so a shell exporting
    # `PY_COLORS=1` or `FORCE_COLOR=1` put an escape between the space and the
    # word — `test_x \x1b[32mPASSED\x1b[0m` — and the scrape came back empty
    # while the child had selected and passed exactly the right three tests.
    # The first run's `4 passed` check survived it (colour wraps that phrase
    # whole), so the fixture went on and the three positive assertions below
    # failed against an empty set while the negative control passed vacuously:
    # the guard reported the helper broken when the harness was. Setting it here
    # keeps `run_nested_pytest`'s `--color=no` under measurement instead of
    # leaving the guard at the mercy of whichever shell ran the suite.
    env["PY_COLORS"] = "1"
    return run_nested_pytest(
        # `cacheprovider=True`: testmon reads the `lf` option off the config and
        # aborts the session without it, so this is the one caller that keeps it.
        #
        # `["."]` is the scope because the child's rootdir is the throwaway
        # project rather than this repo, so "everything" is four tests and is
        # the right answer. The helper asks a caller to *state* its scope, not
        # to make it small, and this had no bound at all before — the worst
        # instance of ISSUE-492's second half, since a hang here ran until the
        # outer session was killed.
        scope=list(extra) or ["."],
        args=["--testmon", "-v", "-p", "no:randomly"],
        cwd=project,
        env=env,
        cacheprovider=True,
    )


def _reported(output: str) -> set[str]:
    """The test names the run actually executed."""
    names = set()
    for line in output.splitlines():
        if "::" in line and " PASSED" in line:
            names.add(line.split("::", 1)[1].split(" ", 1)[0])
    return names


#: The child's closing summary line, and the count in it. Anchored on the rule
#: rather than on the phrase, which appears in a `-v` run's captured output and
#: assertion messages as readily as in the line this is about — `re.search`
#: takes the first match, so an unanchored pattern answers with whichever
#: occurrence came first.
_SUMMARY_RE = re.compile(r"^=+ (.*?) =+$", re.M)
_PASSED_RE = re.compile(r"(\d+) passed")


def _passed(output: str) -> int | None:
    """How many tests the child's closing summary reports as **passed**.

    The second reading of the same run, and the two have to agree. `_reported`
    scrapes per-test lines and answers the empty set for a rendering it cannot
    parse; the summary is a different line in a different format, so a scrape
    that has stopped working disagrees with it rather than looking like a run
    that selected nothing (ISSUE-493). Passes rather than tests *run*, since
    `_reported` counts only the ones that passed.

    `None` means the child said `no tests ran` — testmon selected nothing,
    which is a legitimate outcome here and the real ISSUE-459 regression
    signal. It is deliberately not the same value as nought passed and not the
    same as an unreadable summary: the caller has to answer those three
    differently, and collapsing any pair of them is how this check goes
    vacuous. A summary that is neither raises, because there is no honest
    answer to give.
    """
    summaries = _SUMMARY_RE.findall(output)
    if not summaries:
        raise AssertionError(f"the child printed no summary line:\n{output[-3000:]}")
    # The last one: `test session starts` is a summary line too.
    final = summaries[-1]
    if "no tests ran" in final:
        return None
    found = _PASSED_RE.search(final)
    if not found:
        raise AssertionError(f"unreadable summary line {final!r}:\n{output[-3000:]}")
    return int(found.group(1))


@pytest.fixture(scope="module")
def selection(tmp_path_factory) -> set[str]:
    """Names selected by a second run, after one function's body changed."""
    project = tmp_path_factory.mktemp("drift")
    (project / "pytest.ini").write_text("[pytest]\n")
    (project / "conftest.py").write_text(_CONFTEST.format(repo=str(REPO_ROOT)))
    (project / "mymod.py").write_text(_MODULE_BEFORE)
    (project / "test_guards.py").write_text(_GUARDS)

    first = _run(project)
    assert "4 passed" in first.stdout, first.stdout[-3000:] + first.stderr[-2000:]

    (project / "mymod.py").write_text(_MODULE_AFTER)

    second = _run(project)
    assert second.returncode in (0, 5), second.stdout[-3000:] + second.stderr[-2000:]

    names = _reported(second.stdout)
    # The harness saying it is alive, before any test reads the set. Without
    # it the three assertions below fail one by one against an empty set, which
    # reads as `source_of` having stopped recording — the diagnosis ISSUE-493
    # was filed under, and it took a hand-built probe to rule out.
    #
    # Two outcomes, and collapsing them is what makes this check vacuous.
    # `no tests ran` is testmon selecting nothing — a genuine result, and the
    # ISSUE-459 regression this file exists to report, so it belongs to the
    # assertions below rather than here. Anything else must agree with the
    # scrape. Read the exit status for neither: measured, a run that selects
    # nothing exits *0* rather than the 5 the obvious version of this asserted,
    # which would have turned the real regression into a harness error.
    passed = _passed(second.stdout)
    if passed is not None:
        assert len(names) == passed, (
            "the per-test scrape and the child's own summary disagree, so the "
            "harness cannot read this run and nothing below it means "
            f"anything:\nscraped {sorted(names)}\n"
            + second.stdout[-3000:]
            + second.stderr[-2000:]
        )
    return names


def test_the_executing_test_is_selected(selection):
    """The control in the other direction: testmon works as advertised."""
    assert "test_that_actually_calls_it" in selection


def test_the_helper_makes_the_guard_selectable(selection):
    assert "test_guard_through_the_helper" in selection, (
        "a guard reading source through drift.source_of was not selected by a "
        "change to the function it reads — qt would report it green unrun"
    )


def test_a_decorated_function_is_read_through_its_own_file(selection):
    """`getsourcelines` unwraps a decorator and `getsourcefile` does not.

    Taking the filename off the wrapper recorded a dependency on `contextlib`
    instead — green here, and wrong against every `@contextmanager` guard in
    the suite, which is most of them.
    """
    assert "test_guard_on_a_decorated_function" in selection


def test_raw_getsource_is_still_invisible(selection):
    """The negative control, and the defect as filed.

    Kept as a live measurement rather than a comment: if testmon ever learns to
    trace this on its own, this test goes red and the helper can be deleted.
    """
    assert "test_guard_through_raw_getsource" not in selection


class TestTheHelperInProcess:
    """The two blocks the subprocess measurement above cannot depend on.

    testmon sees nothing a child process does, so everything proved by the
    fixture is proved outside its view: a regression in `source_of` or `_merge`
    would leave the tests that measure them unselected, which is ISSUE-459
    reproduced one level up on the fix for it. These call both directly, in
    this process, so testmon records the dependency.
    """

    def test_source_of_records_the_lines_it_read(self):
        text = drift.source_of(drift._merge)
        assert "def _merge" in text

        nodeid = drift._current_nodeid()
        read = drift._pending[nodeid][os.path.realpath(drift.__file__)]
        assert drift._merge.__code__.co_firstlineno in read

    def test_merge_stores_the_range_under_a_rootdir_relative_path(self):
        # Its own `pending`, never the module's: clearing that one mid-batch
        # would drop what a sibling test had recorded and not yet stored.
        node = "tests/test_drift_selection.py::whatever"
        pending = {node: {str(REPO_ROOT / "src" / "istota" / "db.py"): {3, 4}}}
        nodes_files_lines = {node: {}}

        drift._merge(nodes_files_lines, str(REPO_ROOT), pending)

        assert nodes_files_lines[node] == {"src/istota/db.py": {3, 4}}
        assert pending == {}

    def test_merge_drops_what_lies_outside_the_rootdir(self):
        """testmon stores a dependency as a path under its rootdir."""
        node = "tests/test_drift_selection.py::whatever"
        pending = {node: {"/usr/lib/python3.12/contextlib.py": {1}}}
        nodes_files_lines = {node: {}}

        drift._merge(nodes_files_lines, str(REPO_ROOT), pending)

        assert nodes_files_lines[node] == {}


# `getsourcelines` and `getsourcefile` are here because they are the same guard
# written two characters differently, and just as invisible.
_RAW_READERS = frozenset({"getsource", "getsourcelines", "getsourcefile"})


def _calls_inspect_getsource(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in _RAW_READERS:
            return True
        if isinstance(func, ast.Name) and func.id in _RAW_READERS:
            return True
    return False


def test_no_test_reads_source_through_raw_getsource():
    """The idiom guard, and the reason the fix does not need repeating.

    `source_of` only helps where it is called, and a guard written the obvious
    way is invisible in exactly the way ISSUE-459 describes — silently, and
    only under `qt`.
    """
    # The helper itself reads source for a living; it is the one place these
    # names are the right answer.
    allowed = {Path("tests/support/drift.py")}
    offenders = []
    for path in sorted(REPO_ROOT.joinpath("tests").rglob("*.py")):
        rel = path.relative_to(REPO_ROOT)
        if rel in allowed:
            continue
        if _calls_inspect_getsource(ast.parse(path.read_text(encoding="utf-8"))):
            offenders.append(str(rel))
    assert not offenders, (
        f"{offenders} call inspect.getsource directly; use "
        f"tests.support.drift.source_of so scripts/qt can select them"
    )
