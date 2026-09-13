"""The nested-pytest helper, and the guard that keeps a fifth copy from growing.

`tests/support/nested_pytest.py` exists because six calls across five modules
spawned pytest inside pytest to drive this repository's own addopts and
conftest, and the copies carried the same two defects (ISSUE-492): a collect
with no path argument, whose cost is the size of the repository rather than the
size of the thing under test, and a bound over it that reports a breach by
raising `TimeoutExpired` — an error about a subprocess rather than a failure
about what the test was checking. Two of the six had no bound at all.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from tests.support import nested_pytest
from tests.support.drift import source_of

TESTS_DIR = nested_pytest.REPO / "tests"

#: Modules allowed to spawn pytest without the helper, and why. Empty, and
#: that is the finding rather than the starting point: the one candidate,
#: `tests/test_drift_selection.py`, needs a cacheprovider its child's testmon
#: session reads the `lf` option from — which is now a parameter rather than a
#: reason to stand outside. An entry here is a decision on the record; it is
#: not a place to park a module somebody did not want to convert.
EXEMPT: set[str] = set()

#: The spelling every nested pytest in this tree uses.
#:
#: Assembled from parts rather than written out, so this module does not match
#: its own marker. Exempting this file instead would have been the easy fix and
#: is a hole: the guard would then be the one place in `tests/` free to spawn a
#: nested pytest unnoticed.
SPAWN = ", ".join(["sys.executable", '"-m"', '"pytest"'])


def nested_pytest_modules() -> list[str]:
    """Every test module that spawns pytest inside pytest, found not listed.

    A hand-written list can only cover the modules its author thought of, which
    is the failure `.claude/rules/devbox.md` records against the per-file
    vendored-lib guards: two files were pinned by name and a third was pinned by
    nothing, with both guards reporting green. Scully found the same shape here
    on the first draft of this file — `tests/test_env_isolation.py` carried the
    bound defect verbatim and was in nobody's list.

    The directory walk is itself invisible to testmon, which is the residual
    AGENTS.md already names for a guard that reads files off disk. It is
    narrowed rather than removed: what the walk produces is module *names*, and
    the assertion then reads each one through `source_of`, so every module that
    exists on the day it runs does get a recorded dependency.
    """
    helper = Path(nested_pytest.__file__).resolve()
    found = []
    for path in sorted(TESTS_DIR.rglob("*.py")):
        if path.resolve() == helper or path.name in EXEMPT:
            continue
        if SPAWN in path.read_text(encoding="utf-8"):
            found.append(path)
    return [
        ".".join(["tests", *p.relative_to(TESTS_DIR).with_suffix("").parts])
        for p in found
    ]


class TestABreachedBoundIsAFailure:
    """The reported symptom: the test errors instead of saying what it checked.

    `subprocess.run` raises `TimeoutExpired` on a breach, which pytest reports
    as an *error* with a traceback through `subprocess`. Nothing in that report
    names the tier being guarded, so the reader's first move is to work out
    which nested pytest it even was.
    """

    def test_a_breach_fails_rather_than_raising_timeoutexpired(self, tmp_path):
        # A bound no process start can meet, so the breach is the only outcome
        # and the test costs one interpreter launch.
        with pytest.raises(pytest.fail.Exception):
            nested_pytest.run_nested_pytest(
                scope=[str(tmp_path)],
                args=["--collect-only", "-q"],
                cwd=tmp_path,
                timeout=0.001,
            )

    def test_the_breach_is_not_reported_as_a_subprocess_error(self, tmp_path):
        """`TimeoutExpired` is not a `Failed`, so this is the discriminating
        half: catching the base class would pass either way."""
        with pytest.raises(BaseException) as caught:
            nested_pytest.run_nested_pytest(
                scope=[str(tmp_path)],
                args=["--collect-only", "-q"],
                cwd=tmp_path,
                timeout=0.001,
            )

        assert not isinstance(caught.value, subprocess.TimeoutExpired)

    def test_the_failure_names_the_bound_and_the_scope(self, tmp_path):
        with pytest.raises(pytest.fail.Exception) as caught:
            nested_pytest.run_nested_pytest(
                scope=[str(tmp_path)],
                args=["--collect-only", "-q"],
                cwd=tmp_path,
                timeout=0.001,
            )

        message = str(caught.value)
        assert "0.001" in message, message
        assert str(tmp_path) in message, message

    def test_it_does_not_swallow_an_ordinary_nonzero_exit(self, tmp_path):
        """A breach is a failure; every other outcome is still the caller's.

        The tier guards assert on exit 4 and exit 5, so a helper that turned a
        non-zero status into a failure of its own would make all of them
        unwritable.
        """
        result = nested_pytest.run_nested_pytest(
            scope=["tests/test_nested_pytest.py::TestNoSuchClass"],
            args=["--collect-only", "-q"],
            cwd=nested_pytest.REPO,
        )

        assert result.returncode != 0
        assert isinstance(result, subprocess.CompletedProcess)


class TestTheScopeIsRequired:
    """The other half, and the reason it is a parameter rather than a default.

    Which directory a guard collects is a statement about what that guard
    asserts, so the helper cannot pick one. What it can do is refuse to run
    without one, since the argv it would otherwise build collects everything.
    """

    def test_an_empty_scope_is_refused(self):
        with pytest.raises(ValueError, match="scope"):
            nested_pytest.run_nested_pytest(
                scope=[], args=["--collect-only", "-q"], cwd=nested_pytest.REPO
            )

    @pytest.mark.parametrize("blank", ([""], ["   "], ["tests/image", ""]))
    def test_a_blank_member_is_refused_too(self, blank):
        """`not scope` tests the sequence, not its members.

        An empty string is how pytest is told to collect everything, so
        `scope=[""]` reproduced the whole-tree collect exactly — measured at
        26651 items and 26.7s, through a check whose stated job is to refuse
        that argv. Found in review.
        """
        with pytest.raises(ValueError, match="scope"):
            nested_pytest.run_nested_pytest(
                scope=blank, args=["--collect-only", "-q"], cwd=nested_pytest.REPO
            )

    def test_a_bare_string_scope_is_refused(self):
        """`str` satisfies `Sequence[str]` and splats into single characters.

        pytest answers that with exit 4 — which is the code three tier guards
        assert as the *success* of their own check — so a typo'd scope could
        have made one of them green for the wrong reason. Found in review.
        """
        with pytest.raises(TypeError, match="sequence"):
            nested_pytest.run_nested_pytest(
                scope="tests/image",
                args=["--collect-only", "-q"],
                cwd=nested_pytest.REPO,
            )

    def test_the_cacheprovider_can_be_turned_back_on(self):
        """One caller needs it: testmon reads the `lf` option off the config and
        aborts the session without a cacheprovider."""
        off = nested_pytest.run_nested_pytest(
            scope=["tests/test_tier_deselection.py"],
            args=["--collect-only", "-q"],
            cwd=nested_pytest.REPO,
        )
        on = nested_pytest.run_nested_pytest(
            scope=["tests/test_tier_deselection.py"],
            args=["--collect-only", "-q"],
            cwd=nested_pytest.REPO,
            cacheprovider=True,
        )

        assert off.returncode == 0, off.stdout
        assert on.returncode == 0, on.stdout
        assert "no:cacheprovider" in source_of(nested_pytest.run_nested_pytest)

    def test_the_scope_bounds_what_is_collected(self):
        """The property the whole fix rests on: a scoped collect sees only the
        scope. Asserted on the nodeids rather than on a count, since a count
        would go stale with the file."""
        result = nested_pytest.run_nested_pytest(
            scope=["tests/test_tier_deselection.py"],
            args=["--collect-only", "-q"],
            cwd=nested_pytest.REPO,
        )

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        nodeids = [
            line for line in result.stdout.splitlines() if "::" in line
        ]
        assert nodeids, result.stdout
        for nodeid in nodeids:
            assert nodeid.startswith("tests/test_tier_deselection.py::"), nodeid

    def test_the_cacheprovider_is_off(self):
        """These run concurrently with an outer `-n auto` session writing the
        same `.pytest_cache` nodeids file."""
        assert "no:cacheprovider" in source_of(nested_pytest.run_nested_pytest)


class TestTheChildsRenderingIsPinned:
    """The child's output is data the caller parses, so the shell must not style it.

    Every caller reads the child's stdout, and pytest colours that stdout when
    `PY_COLORS=1` or `FORCE_COLOR=1` is exported — which puts an escape between
    the space and the word in `test_x \\x1b[32mPASSED\\x1b[0m` and in
    `\\x1b[31mFAILED\\x1b[0m tests/…`. Both spellings are scraped in this tree,
    and both come back empty rather than wrong, so the guard above them reports
    the subject broken instead of the reader.

    **Found twice.** ISSUE-365 met the `FAILED` spelling in
    `tests/test_env_isolation.py` and answered it there, with a `PY_COLORS=0`
    that reached one caller of six; ISSUE-493 then met the `PASSED` spelling in
    `tests/test_drift_selection.py`, which had no such defence and spent three
    failing assertions blaming `source_of`. The pin is in the argv because
    `--color` outranks every spelling of the variable wherever it came from and
    a caller passing an `env` of its own cannot undo it — the route it arrived
    by, an unexamined `dict(os.environ)`. That makes this the authoritative copy
    and the local setting is gone; `git_hardening` states the same rule for
    `git`'s `color.ui`.

    Negative control, run and reverted: with `--color=no` out of the argv,
    `PY_COLORS=1` renders `test_x.py::test_a \\x1b[32mPASSED\\x1b[0m` and nine
    nodes go red — the four below, the four in `tests/test_drift_selection.py`
    (on the fixture's liveness assertion, which is the half that now names the
    harness rather than blaming `source_of`), and
    `tests/test_env_isolation.py::TestTheScrubHoldsUnderARealRun::test_the_control_goes_red_without_the_scrub`,
    which is a witness only because ISSUE-493 dropped the local `PY_COLORS=0`
    that used to make it immune.
    """

    #: Both spellings, since pytest consults them in that order and stopping at
    #: the first would leave the other live.
    FORCING = ({"PY_COLORS": "1"}, {"FORCE_COLOR": "1"})

    @pytest.fixture(scope="class")
    def project(self, tmp_path_factory) -> Path:
        project = tmp_path_factory.mktemp("colour")
        (project / "pytest.ini").write_text("[pytest]\n")
        (project / "test_x.py").write_text("def test_a():\n    assert True\n")
        return project

    @pytest.mark.parametrize("forcing", FORCING, ids=lambda f: next(iter(f)))
    def test_a_forced_colour_does_not_reach_the_output(self, project, forcing):
        env = dict(os.environ) | {"PYTEST_ADDOPTS": ""} | forcing

        result = nested_pytest.run_nested_pytest(
            scope=["."], args=["-v"], cwd=project, env=env
        )

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "\x1b[" not in result.stdout, repr(result.stdout[-1500:])

    @pytest.mark.parametrize("forcing", FORCING, ids=lambda f: next(iter(f)))
    def test_the_scrape_every_caller_writes_still_matches(self, project, forcing):
        """The discriminating half: absent escapes is not the same claim as a
        parseable line, and the callers assert on the second one."""
        env = dict(os.environ) | {"PYTEST_ADDOPTS": ""} | forcing

        result = nested_pytest.run_nested_pytest(
            scope=["."], args=["-v"], cwd=project, env=env
        )

        verbose = [line for line in result.stdout.splitlines() if "::test_a" in line]
        assert verbose, result.stdout
        assert all(" PASSED" in line for line in verbose), repr(verbose)


class TestNoTestSpawnsPytestOnItsOwn:
    """The drift guard: a sixth copy is what the consolidation is protecting.

    Stated over what the walk finds rather than over a list, so a module added
    later is covered on the day it is added.
    """

    def test_nothing_outside_the_exemption_spawns_its_own(self):
        offenders = nested_pytest_modules()

        assert not offenders, (
            f"{', '.join(offenders)} spawns a nested pytest without going "
            "through tests.support.nested_pytest, so it carries its own bound "
            "and its own collection scope (ISSUE-492). Convert it, or add it "
            "to EXEMPT above with the reason."
        )

    def test_the_marker_still_matches_a_file_that_spawns(self):
        """The positive control, and it reads a *module* because that is the
        branch the guard uses.

        `tests/support/drift.py` treats the two differently — a module's source
        comes back starting at line 0 where a function's starts at 1 — so a
        control over `run_nested_pytest` the function would witness the wrong
        branch. The helper module is the only honest subject left now that every
        caller is converted, and it is the one the walk skips, so this is what
        says the guard above can still fail.
        """
        assert SPAWN in source_of(nested_pytest), (
            "the spawn spelling changed, so the walk matches nothing and the "
            "guard above passes vacuously"
        )

    def test_the_walk_actually_reads_the_tree(self):
        """The other half: a walk that found no files at all would also report
        no offenders."""
        seen = list(TESTS_DIR.rglob("*.py"))

        assert len(seen) > 100, f"the walk only found {len(seen)} files"

    def test_an_exemption_must_name_a_file_that_really_spawns(self):
        """A ratchet for entries added later. `EXEMPT` is empty today, so this
        passes vacuously on purpose — what it stops is an exemption naming a
        file that has since been converted or deleted, which reads as a live
        carve-out while covering nothing."""
        for name in EXEMPT:
            path = TESTS_DIR / name
            assert path.exists(), f"{name} is exempted but does not exist"
            assert SPAWN in path.read_text(encoding="utf-8"), (
                f"{name} is exempted but no longer spawns a nested pytest; "
                "drop the exemption"
            )
