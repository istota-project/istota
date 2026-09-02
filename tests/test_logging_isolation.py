"""The `istota` logger must not survive a test.

`logging` is a process global, and one test could poison every later one in
the same xdist worker: `setup_logging` raises the `istota` logger to INFO and
binds a `StreamHandler` to `sys.stderr` as it is at that moment, then sets a
module flag that makes it permanent. Called under `capsys` — as
`tests/test_cli_session.py` does through `cli.main()` — the handler holds the
capture object pytest closes at teardown, so every later `istota.*` record at
INFO or above raises inside `emit` and `logging.Handler.handleError` prints a
traceback to whatever `sys.stderr` is *then*. Inside a `CliRunner` that is the
invocation's own buffer, and click's `Result.output` mixes stdout and stderr,
so the traceback reaches the value the test parses.

Both tests here drive the mechanism rather than asserting on a neighbour's
leftovers: a test that reads global state a *previous* test was supposed to
have cleaned passes vacuously whenever xdist puts the two on different workers.
"""

from __future__ import annotations

import logging

import pytest

from istota import logging_setup
from istota.config import Config

from . import conftest


def _poison() -> None:
    logging_setup.setup_logging(Config())


class TestResetLogging:
    def test_it_puts_back_the_level_as_well_as_the_handlers(self):
        istota = logging.getLogger("istota")
        _poison()
        assert istota.handlers, "setup_logging did not install a handler"
        assert istota.level == logging.INFO

        logging_setup.reset_logging()

        assert istota.handlers == []
        # The half that used to be left behind. It decides whether an INFO
        # record is created at all, so leaving it is enough on its own to
        # change what a later test sees.
        assert istota.level == logging.NOTSET
        assert logging_setup._initialized is False


class TestTheAutouseFixture:
    def test_teardown_restores_what_setup_snapshotted(self):
        """Drive the fixture's own generator, so the assertion is about this
        fixture and not about whichever test ran before this one."""
        gen = conftest._reset_istota_logging.__wrapped__()
        next(gen)  # setup: snapshot

        _poison()
        istota = logging.getLogger("istota")
        assert istota.handlers
        assert istota.level == logging.INFO
        assert logging_setup._initialized is True

        with pytest.raises(StopIteration):
            next(gen)  # teardown

        assert istota.handlers == []
        assert istota.level == logging.NOTSET
        assert logging_setup._initialized is False
