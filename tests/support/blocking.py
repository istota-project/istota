"""A wall-clock bound for the tests that plant a FIFO (ISSUE-339).

The failure these tests exist to catch is a blocking ``open(2)``, so a
regression does not make them fail — it makes them *hang*, and the suite runs
under ``-n auto``, where one wedged worker stalls the whole run and reports
nothing about why. ``SIGALRM`` converts that back into an ordinary failure with
the test's own name on it.

Unix only, which is every platform this project supports, and single-threaded:
``signal.alarm`` fires on the main thread, and pytest-xdist workers run their
tests there. Nested use would clobber the outer alarm, so this is deliberately
not reentrant — one guard per test, around the call under test and nothing else.
"""

import signal
from contextlib import contextmanager


class DidNotReturn(AssertionError):
    """The call under test blocked instead of refusing."""


@contextmanager
def fails_if_it_blocks(seconds: int = 5, what: str = "the call under test"):
    fired = []

    def _fire(signum, frame):
        fired.append(True)
        raise DidNotReturn(
            f"{what} blocked for {seconds}s — a planted FIFO is being opened "
            "without O_NONBLOCK, or read before the S_ISREG check"
        )

    previous = signal.signal(signal.SIGALRM, _fire)
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous)
        # The alarm fires exactly once. If anything on the path between here
        # and the blocking `open(2)` swallows the exception — a broad `except
        # Exception`, which several readers here legitimately use — the guard
        # would be consumed, the call would resume blocking, and the worker
        # would hang silently: the precise failure this helper exists to turn
        # into a test failure. Asserting the handler did *not* fire converts
        # that back into a visible one.
        if fired:
            raise DidNotReturn(
                f"{what} blocked for {seconds}s and something on the path "
                "swallowed the alarm — the guard cannot fire twice"
            )
