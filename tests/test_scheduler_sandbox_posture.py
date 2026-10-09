"""The daemon's start-up decision about a sandbox it was asked for and cannot build.

On Linux, `sandbox_enabled` with no usable bubblewrap used to be a warning, and
every task then ran with the daemon's own filesystem access (ISSUE-381 is the
shape that shipped). In the one deployment shape that is a missing grant or a
profile that is too narrow, so it is now a start-up failure: compose restarts
the container and the operator sees the loop and the reason in the log, rather
than a quiet unsandboxed run.

macOS development checkouts stay as they were: bubblewrap does not exist there,
and the warning says so.
"""

from __future__ import annotations

import logging

import pytest

from istota import scheduler as sched
from istota.config import Config, SecurityConfig, UserConfig


def _config(*, sandbox: bool = True, users: int = 1) -> Config:
    return Config(
        security=SecurityConfig(sandbox_enabled=sandbox),
        users={f"user{i}": UserConfig() for i in range(users)},
    )


@pytest.fixture
def bwrap(monkeypatch):
    """Set what `_bwrap_available` answers, without spawning anything."""
    from istota import executor

    def _set(available: bool) -> None:
        monkeypatch.setattr(executor, "_bwrap_available", lambda: available)

    return _set


class TestLinux:
    @pytest.fixture(autouse=True)
    def _linux(self, monkeypatch):
        monkeypatch.setattr(sched.sys, "platform", "linux")

    def test_a_sandbox_that_cannot_be_built_stops_the_daemon(self, bwrap):
        bwrap(False)

        with pytest.raises(sched.SandboxUnavailable) as raised:
            sched._report_sandbox_posture(_config())

        assert "bubblewrap" in str(raised.value)

    def test_single_user_is_no_exception(self, bwrap):
        # The old code downgraded single-user to a dev-only warning. On Linux
        # there is no single-user shape that is meant to run unconfined with
        # the sandbox switched on.
        bwrap(False)

        with pytest.raises(sched.SandboxUnavailable):
            sched._report_sandbox_posture(_config(users=1))

    def test_a_working_sandbox_starts(self, bwrap, caplog):
        bwrap(True)

        with caplog.at_level(logging.INFO, logger="istota.scheduler"):
            sched._report_sandbox_posture(_config())

        assert "SECURITY Sandbox enabled with bubblewrap" in caplog.text

    def test_a_deliberately_disabled_sandbox_still_starts(self, bwrap):
        # Not the case this guards: the operator turned it off and knows.
        bwrap(False)

        sched._report_sandbox_posture(_config(sandbox=False))


class TestDevelopmentOffLinux:
    def test_macos_warns_and_starts(self, monkeypatch, bwrap, caplog):
        monkeypatch.setattr(sched.sys, "platform", "darwin")
        bwrap(False)

        with caplog.at_level(logging.WARNING, logger="istota.scheduler"):
            sched._report_sandbox_posture(_config())

        assert "bubblewrap unavailable" in caplog.text


def test_main_exits_non_zero_when_the_sandbox_is_unavailable(monkeypatch, tmp_path):
    def _refuse(config, **kwargs):
        raise sched.SandboxUnavailable("bubblewrap cannot create a namespace here")

    monkeypatch.setattr(sched, "run_daemon", _refuse)
    monkeypatch.setattr(sched, "load_config", lambda path: Config())
    monkeypatch.setattr("istota.logging_setup.setup_logging", lambda *a, **k: None)
    monkeypatch.setattr("sys.argv", ["istota-scheduler", "--daemon"])

    with pytest.raises(SystemExit) as exited:
        sched.main()

    assert exited.value.code == 1
