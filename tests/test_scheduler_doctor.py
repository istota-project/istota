"""Tests for the scheduler's doctor wiring: the boot run and the interval sweep.

Three properties matter more than the mechanics.

**Start-up never aborts.** Both deployment shapes restart automatically
(`restart: unless-stopped` in compose, systemd on bare metal), so a daemon that
exited on a FAIL would not fail loudly — it would crash-loop, and in the
container shape the operator could not exec in to fix the thing that is failing.

**A boot FAIL is alerted, not merely logged.** Nobody reads the log until
something has already broken.

**Recipients are the admin allowlist, failing closed on empty.** `Config.is_admin`
reads an empty allowlist as "everyone", which is the wrong reading for a message
naming install paths, binary locations and remedies.
"""

from __future__ import annotations

import logging

import pytest

from istota import scheduler
from istota.doctor import FAIL, OK, WARN, CheckResult


def _fail(name="developer.forge_binaries.gh"):
    return CheckResult(name, FAIL, "/usr/local/bin/gh does not exist", remedy="Install gh.")


def _ok(name="runtime.platform"):
    return CheckResult(name, OK, "Linux x86_64")


def _warn(name="developer.forge_config_drift.gh"):
    return CheckResult(name, WARN, "configured path is not the resolved one", remedy="Rewrite it.")


@pytest.fixture
def sent(monkeypatch):
    """Capture every operator alert the scheduler dispatches."""
    calls = []
    monkeypatch.setattr(
        scheduler,
        "_send_operator_alert",
        lambda config, user_id, message, **kw: calls.append((user_id, message)),
    )
    return calls


class TestRunStartupChecks:
    def test_a_clean_run_logs_nothing_above_info(self, make_config, monkeypatch, caplog, sent):
        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_ok()])
        with caplog.at_level(logging.WARNING):
            scheduler.run_startup_checks(make_config())
        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_warnings_log_at_warning(self, make_config, monkeypatch, caplog, sent):
        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_warn()])
        with caplog.at_level(logging.WARNING):
            scheduler.run_startup_checks(make_config())
        levels = {r.levelno for r in caplog.records if r.levelno >= logging.WARNING}
        assert levels == {logging.WARNING}

    def test_failures_log_at_error(self, make_config, monkeypatch, caplog, sent):
        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_fail()])
        with caplog.at_level(logging.WARNING):
            scheduler.run_startup_checks(make_config())
        assert any(r.levelno == logging.ERROR for r in caplog.records)

    def test_never_raises_when_the_registry_explodes(self, make_config, monkeypatch, sent):
        """Doctor runs on the start-up path; an exception here is an outage."""

        def _boom(config, **kw):
            raise RuntimeError("registry is broken")

        monkeypatch.setattr("istota.doctor.run_checks", _boom)
        scheduler.run_startup_checks(make_config())

    def test_returns_the_results(self, make_config, monkeypatch, sent):
        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_ok(), _fail()])
        results = scheduler.run_startup_checks(make_config())
        assert [r.name for r in results] == ["runtime.platform", "developer.forge_binaries.gh"]


class TestStartupAlert:
    def test_one_alert_per_admin_not_one_per_check(self, make_config, monkeypatch, sent):
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [_fail("a.b"), _fail("c.d"), _fail("e.f")],
        )
        config = make_config(admin_users={"alice"})
        scheduler.run_startup_checks(config)
        assert len(sent) == 1
        assert sent[0][0] == "alice"

    def test_the_alert_names_the_failing_checks_and_remedies(self, make_config, monkeypatch, sent):
        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_fail()])
        scheduler.run_startup_checks(make_config(admin_users={"alice"}))
        message = sent[0][1]
        assert "developer.forge_binaries.gh" in message
        assert "Install gh." in message

    def test_every_admin_gets_it(self, make_config, monkeypatch, sent):
        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_fail()])
        scheduler.run_startup_checks(make_config(admin_users={"alice", "bob"}))
        assert {user for user, _ in sent} == {"alice", "bob"}

    def test_no_alert_without_a_failure(self, make_config, monkeypatch, sent):
        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_ok(), _warn()])
        scheduler.run_startup_checks(make_config(admin_users={"alice"}))
        assert sent == []

    def test_empty_allowlist_sends_nothing_and_says_so(
        self, make_config, monkeypatch, caplog, sent
    ):
        """`Config.is_admin` treats an empty allowlist as "everyone", which is
        the wrong reading for a message naming install paths and remedies."""
        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_fail()])
        with caplog.at_level(logging.WARNING):
            scheduler.run_startup_checks(make_config(admin_users=set()))
        assert sent == []
        assert any("no admin" in r.getMessage().lower() for r in caplog.records)

    def test_a_failed_send_does_not_propagate(self, make_config, monkeypatch):
        """The alert path must not be able to take down the daemon."""
        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_fail()])

        def _explode(*args, **kwargs):
            raise RuntimeError("talk is down")

        monkeypatch.setattr(scheduler, "_send_operator_alert", _explode)
        scheduler.run_startup_checks(make_config(admin_users={"alice"}))


class TestRedaction:
    """The renderers are not the only boundary. A log file and a Talk room are
    boundaries too, and several checks interpolate raw exception text into
    `detail`."""

    @staticmethod
    def _config_with_token(make_config, secret):
        from istota.config import DeveloperConfig

        return make_config(
            admin_users={"alice"},
            developer=DeveloperConfig(
                enabled=True, repos_dir="/tmp/repos", gitlab_token=secret
            ),
        )

    def test_the_startup_alert_is_redacted(self, make_config, monkeypatch, sent):
        secret = "NOT-A-REAL-TOKEN-" + "r" * 12
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [
                CheckResult("a.b", FAIL, f"rejected {secret}", remedy=f"rotate {secret}")
            ],
        )
        scheduler.run_startup_checks(self._config_with_token(make_config, secret))
        assert secret not in sent[0][1]
        assert "[redacted]" in sent[0][1]

    def test_the_startup_log_is_redacted(self, make_config, monkeypatch, caplog, sent):
        secret = "NOT-A-REAL-TOKEN-" + "l" * 12
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [CheckResult("a.b", FAIL, f"saw {secret}", remedy="rotate")],
        )
        with caplog.at_level(logging.WARNING):
            scheduler.run_startup_checks(self._config_with_token(make_config, secret))
        assert secret not in " ".join(r.getMessage() for r in caplog.records)

    def test_the_sweep_alert_is_redacted(self, make_config, monkeypatch, sent):
        secret = "NOT-A-REAL-TOKEN-" + "s" * 12
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: [CheckResult("a.b", FAIL, f"saw {secret}", remedy="rotate")],
        )
        scheduler.check_doctor(self._config_with_token(make_config, secret), {})
        assert secret not in sent[0][1]


class TestSweepSkips:
    def test_the_sweep_leaves_the_framework_db_alone(self, make_config, monkeypatch, sent):
        """`PRAGMA quick_check` reads the whole database and `check_db_health`
        already does it daily. Hourly here would be the same scan 24 times."""
        seen = {}

        def _capture(config, **kw):
            seen.update(kw)
            return [_ok()]

        monkeypatch.setattr("istota.doctor.run_checks", _capture)
        scheduler.check_doctor(make_config(), {})
        assert "runtime.framework_db" in seen.get("skip", ())

    def test_the_boot_run_still_checks_it(self, make_config, monkeypatch, sent):
        seen = {}

        def _capture(config, **kw):
            seen.update(kw)
            return [_ok()]

        monkeypatch.setattr("istota.doctor.run_checks", _capture)
        scheduler.run_startup_checks(make_config())
        assert "runtime.framework_db" not in seen.get("skip", ())

    def test_the_skipped_set_names_real_checks(self):
        from istota.doctor import CHECKS

        names = {name for name, _ in CHECKS}
        assert set(scheduler.SWEEP_SKIPPED_CHECKS) <= names


class TestCheckDoctor:
    def test_seeded_state_suppresses_a_repeat_of_the_boot_alert(
        self, make_config, monkeypatch, sent
    ):
        """The daemon seeds `doctor_state` from the boot run. Without it every
        failure the boot alert already named counts as newly failing an hour
        later and alerts a second time."""
        import istota.doctor as doctor_mod

        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_fail()])
        config = make_config(admin_users={"alice"})
        boot_results = scheduler.run_startup_checks(config)
        assert len(sent) == 1
        state = {"failing": {r.name for r in doctor_mod.failing(boot_results)}}
        scheduler.check_doctor(config, state)
        assert len(sent) == 1

    def test_alerts_on_the_transition_into_fail(self, make_config, monkeypatch, sent):
        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_fail()])
        config = make_config(admin_users={"alice"})
        state = {}
        scheduler.check_doctor(config, state)
        assert len(sent) == 1

    def test_does_not_re_alert_while_still_failing(self, make_config, monkeypatch, sent):
        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_fail()])
        config = make_config(admin_users={"alice"})
        state = {}
        scheduler.check_doctor(config, state)
        scheduler.check_doctor(config, state)
        scheduler.check_doctor(config, state)
        assert len(sent) == 1

    def test_a_newly_failing_check_alerts_again(self, make_config, monkeypatch, sent):
        config = make_config(admin_users={"alice"})
        state = {}
        monkeypatch.setattr("istota.doctor.run_checks", lambda c, **kw: [_fail("a.b")])
        scheduler.check_doctor(config, state)
        monkeypatch.setattr("istota.doctor.run_checks", lambda c, **kw: [_fail("a.b"), _fail("c.d")])
        scheduler.check_doctor(config, state)
        assert len(sent) == 2
        assert "c.d" in sent[1][1]

    def test_recovering_then_failing_again_alerts(self, make_config, monkeypatch, sent):
        config = make_config(admin_users={"alice"})
        state = {}
        monkeypatch.setattr("istota.doctor.run_checks", lambda c, **kw: [_fail("a.b")])
        scheduler.check_doctor(config, state)
        monkeypatch.setattr("istota.doctor.run_checks", lambda c, **kw: [_ok()])
        scheduler.check_doctor(config, state)
        monkeypatch.setattr("istota.doctor.run_checks", lambda c, **kw: [_fail("a.b")])
        scheduler.check_doctor(config, state)
        assert len(sent) == 2

    def test_no_alert_while_clean(self, make_config, monkeypatch, sent):
        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_ok()])
        state = {}
        scheduler.check_doctor(make_config(admin_users={"alice"}), state)
        assert sent == []

    def test_empty_allowlist_sends_nothing(self, make_config, monkeypatch, sent):
        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_fail()])
        scheduler.check_doctor(make_config(admin_users=set()), {})
        assert sent == []

    def test_returns_the_results(self, make_config, monkeypatch, sent):
        monkeypatch.setattr("istota.doctor.run_checks", lambda config, **kw: [_fail()])
        results = scheduler.check_doctor(make_config(), {})
        assert [r.name for r in results] == ["developer.forge_binaries.gh"]

    def test_never_raises(self, make_config, monkeypatch, sent):
        def _boom(config, **kw):
            raise RuntimeError("registry is broken")

        monkeypatch.setattr("istota.doctor.run_checks", _boom)
        assert scheduler.check_doctor(make_config(), {}) == []

    def test_does_not_run_deep_checks(self, make_config, monkeypatch, sent):
        """The sweep is periodic and unattended; spawning a namespace on a timer
        is not what `--deep` is for."""
        seen = {}
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda config, **kw: (seen.update(kw), [_ok()])[1],
        )
        scheduler.check_doctor(make_config(), {})
        assert seen.get("deep") is not True


class TestDoctorInterval:
    def test_config_key_defaults_to_an_hour(self, make_config):
        assert make_config().scheduler.doctor_check_interval == 3600

    def test_config_key_is_read_from_toml(self, tmp_path):
        from istota.config import load_config

        path = tmp_path / "config.toml"
        path.write_text("[scheduler]\ndoctor_check_interval = 900\n")
        assert load_config(path).scheduler.doctor_check_interval == 900
