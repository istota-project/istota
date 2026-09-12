"""One nested pytest invocation, for the guards that drive this repo's own conftest.

Six calls across five modules spawn pytest inside pytest, because the thing under
test is this repository's own ``addopts`` and ``conftest`` and ``pytester`` gives
a synthetic project with neither. Each had written its own copy of the call, and
the copies carried the same two defects (ISSUE-492) — two of them with no bound
at all rather than a fixed one.

**A collect with no path argument collects the whole tree**, so its cost is the
size of the repository rather than the size of the thing under test: 26631
items, measured at 12s warm and 73s cold in a fresh worktree. Every tier guard
is asserting about the items of one tier, and every one of them can name its own
directory instead — measured at 0.33s to 0.61s for the same verdicts, a 35x to
200x cut. The scope is a **required** argument rather than a default, because
which directory a guard collects is a statement about what that guard asserts
and the helper cannot pick one for it; what it can do is refuse to build an argv
that names nothing. Narrowing one is not free of judgement either: the wide
collect carried coverage incidentally, and `tests/test_smoke_tier.py`'s
`SERIAL_TIER_SCOPE` records the case where a narrowing dropped it.

**And the bound is fixed while the work under it is load-dependent.** The suite
runs ``-n auto`` and several run at once across worktrees, so the same call that
takes 12s quiet takes far longer under contention — which is how ISSUE-492 was
found, red on a machine running two other full suites. ``subprocess.run``
reports a breach by raising ``TimeoutExpired``, so the test *errors* with a
traceback through ``subprocess`` rather than failing with a message about the
tier it was guarding. ``run_nested_pytest`` turns that into a ``pytest.fail``
naming the bound, the elapsed time and the argv.

The bound itself is deliberately **not** raised. It exists to catch a hang, a
looser one is worse at that job, and raising it buys a fixed amount of headroom
against something that keeps growing — so it postpones rather than fixes, and
the next person meets it at a worse moment. Cutting the work is what gives back
the margin.
"""

from __future__ import annotations

import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

#: Generous, because it is a hang detector rather than a performance budget.
#: With every caller scoped the real cost is under a second, so a breach now
#: means something is stuck rather than that the machine is busy.
DEFAULT_TIMEOUT = 300.0


def run_nested_pytest(
    *,
    scope: Sequence[str],
    args: Sequence[str] = (),
    cwd: Path | str = REPO,
    env: Mapping[str, str] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    cacheprovider: bool = False,
) -> subprocess.CompletedProcess[str]:
    """Run pytest in a subprocess over ``scope``, failing rather than erroring.

    ``scope`` is the paths or node ids to collect, relative to ``cwd``, and it
    must name something. ``args`` is everything else — ``--collect-only``,
    ``-m``, ``-n`` — and goes ahead of the scope in the argv.

    What the check below enforces is that a caller **states** its scope, not
    that the scope is small: ``["."]`` is accepted and is the right answer for a
    child whose rootdir is a throwaway project. What it refuses is the argv that
    names nothing, because that one silently means the whole of ``cwd``.

    Only a breached bound is turned into a failure. Every other outcome is
    returned untouched, because the callers assert on exit 4 (the usage error
    the collection hook raises) and exit 5 (nothing collected), and a helper
    that read a non-zero status as its own failure would make those unwritable.

    ``cacheprovider`` is off by default and exists for one caller. The
    cacheprovider writes ``.pytest_cache/v/cache/`` nodeids during collection,
    and these run concurrently with an outer ``-n auto`` session writing the
    same file — benign today (a clobbered ``--lf`` set, not a failure), but
    shared mutable state across processes is what the order-independence rule in
    AGENTS.md rules out. A child driving **testmon** is the exception: testmon
    reads the ``lf`` option off the config and aborts the session without a
    cacheprovider, so `tests/test_drift_selection.py` turns it back on.
    """
    if isinstance(scope, (str, bytes)):
        # `str` satisfies `Sequence[str]` and would splat into single
        # characters, which pytest answers with exit 4 — the same code three
        # callers assert as the *success* of their guard.
        raise TypeError(
            f"scope must be a sequence of paths, not {type(scope).__name__}; "
            f"pass [{scope!r}] rather than {scope!r}"
        )
    if not scope or not all(str(part).strip() for part in scope):
        # `not scope` alone lets `[""]` through, and an empty argument is how
        # pytest is told to collect everything — measured at 26651 items.
        raise ValueError(
            "run_nested_pytest needs an explicit scope: a nested pytest whose "
            "scope names nothing collects the whole of cwd, which is both the "
            f"cost and the load-dependence ISSUE-492 is about (got {scope!r})"
        )

    argv = [
        sys.executable, "-m", "pytest",
        *(() if cacheprovider else ("-p", "no:cacheprovider")),
        *args, *scope,
    ]

    started = time.monotonic()
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=dict(env) if env is not None else None,
        )
    except subprocess.TimeoutExpired as breach:
        elapsed = time.monotonic() - started
        pytest.fail(
            f"the nested pytest did not finish within {timeout}s "
            f"(gave up after {elapsed:.1f}s).\n"
            f"argv: {argv}\n"
            f"cwd: {cwd}\n"
            f"stdout tail:\n{_tail(breach.stdout)}\n"
            f"stderr tail:\n{_tail(breach.stderr)}",
            pytrace=False,
        )


def _tail(stream: str | bytes | None, limit: int = 2000) -> str:
    """Whatever the killed child had already written, bounded.

    A breach is the one path where the output is all the evidence there is.
    The `bytes` branch is live rather than defensive, and the mechanism is worth
    stating so it is not "cleaned up": on POSIX `subprocess.run` does not
    re-`communicate()` after the kill — `Popen._communicate` raises through
    `_check_timeout(..., output=b"".join(stdout_seq))`, so the payload is bytes
    even under `text=True`. Only the Windows arm re-reads and yields `str`.
    """
    if not stream:
        return ""
    if isinstance(stream, bytes):
        stream = stream.decode("utf-8", "replace")
    return stream[-limit:]
