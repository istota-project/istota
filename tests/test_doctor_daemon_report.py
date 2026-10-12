"""The daemon's doctor report, and the requests the Health pane leaves for it.

On the Docker stack the daemon and the web process are separate containers.
Only the daemon has the sandbox's security profile, the delegated cgroup and
the devbox sockets, so a doctor run in the web process answered for the wrong
container: `security.sandbox_effective` failed in the Health pane on a stack
whose tasks were sandboxed. The daemon now records every run it makes, the pane
shows that record, and a re-run or a deep run is a request the daemon picks up
on its next tick.
"""

from __future__ import annotations

import pytest

from istota import db, doctor, scheduler
from istota.doctor import FAIL, OK, CheckResult


@pytest.fixture
def config(make_config):
    cfg = make_config()
    db.init_db(cfg.db_path)
    return cfg


def _ok(name="runtime.platform"):
    return CheckResult(name, OK, "Linux x86_64")


class TestTheStore:
    def test_a_recorded_run_reads_back(self, config):
        doctor.record_report(config, [_ok()])
        report = doctor.read_reports(config)["report"]
        assert report["checks"] == doctor.check_payload([_ok()])
        assert report["at"]
        assert report["request_id"] is None

    def test_a_deep_run_is_kept_apart_from_the_shallow_one(self, config):
        doctor.record_report(config, [_ok()])
        doctor.record_report(config, [_ok("sandbox.masks")], deep=True, request_id="r1")
        doctor.record_report(config, [_ok("runtime.bwrap")])
        reports = doctor.read_reports(config)
        assert [c["name"] for c in reports["report"]["checks"]] == ["runtime.bwrap"]
        assert [c["name"] for c in reports["deep_report"]["checks"]] == ["sandbox.masks"]
        assert reports["deep_report"]["request_id"] == "r1"

    def test_nothing_recorded_reads_as_none(self, config):
        assert doctor.read_reports(config) == {"report": None, "deep_report": None, "request": None}

    def test_a_missing_database_reads_as_none_and_records_nothing(self, make_config):
        cfg = make_config()
        doctor.record_report(cfg, [_ok()])
        assert doctor.read_reports(cfg)["report"] is None

    def test_a_second_request_joins_the_pending_one(self, config):
        first = doctor.request_run(config, deep=False)
        second = doctor.request_run(config, deep=True)
        assert second["id"] == first["id"]
        assert second["deep"] is True
        assert doctor.read_reports(config)["request"] == second

    def test_a_deep_request_is_not_downgraded_by_a_shallow_one(self, config):
        doctor.request_run(config, deep=True)
        assert doctor.request_run(config, deep=False)["deep"] is True

    def test_taking_a_request_clears_it(self, config):
        made = doctor.request_run(config, deep=True)
        assert doctor.take_request(config) == made
        assert doctor.take_request(config) is None
        assert doctor.read_reports(config)["request"] is None

    def test_the_namespace_is_out_of_the_models_reach(self):
        from istota.sandbox.kv_namespaces import is_reserved_namespace

        assert is_reserved_namespace(doctor.REPORT_NAMESPACE)


@pytest.fixture
def quiet_alerts(monkeypatch):
    monkeypatch.setattr(scheduler, "_send_operator_alert", lambda *a, **kw: None)


class TestTheDaemonRecords:
    def test_the_boot_run_is_recorded(self, config, monkeypatch, quiet_alerts):
        monkeypatch.setattr("istota.doctor.run_checks", lambda cfg, **kw: [_ok()])
        scheduler.run_startup_checks(config)
        assert doctor.read_reports(config)["report"]["checks"][0]["name"] == "runtime.platform"

    def test_the_sweep_is_recorded(self, config, monkeypatch, quiet_alerts):
        monkeypatch.setattr("istota.doctor.run_checks", lambda cfg, **kw: [_ok("runtime.bwrap")])
        scheduler.check_doctor(config, {})
        assert doctor.read_reports(config)["report"]["checks"][0]["name"] == "runtime.bwrap"

    def test_the_recorded_run_is_redacted(self, make_config, monkeypatch, quiet_alerts):
        from istota.config import DeveloperConfig

        secret = "NOT-A-REAL-TOKEN-" + "r" * 12
        cfg = make_config(developer=DeveloperConfig(enabled=True, repos_dir="/tmp/repos", gitlab_token=secret))
        db.init_db(cfg.db_path)
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda c, **kw: [CheckResult("runtime.x", FAIL, f"rejected {secret}", remedy="r")],
        )
        scheduler.check_doctor(cfg, {})
        assert secret not in doctor.read_reports(cfg)["report"]["checks"][0]["detail"]


class TestARequestOnTheNextTick:
    def _calls(self, monkeypatch):
        calls = []

        def run_checks(cfg, **kw):
            calls.append(kw)
            return [_ok("sandbox.masks")] if kw.get("deep") else [_ok()]

        monkeypatch.setattr("istota.doctor.run_checks", run_checks)
        return calls

    def test_no_request_runs_nothing(self, config, monkeypatch, quiet_alerts):
        calls = self._calls(monkeypatch)
        scheduler.check_doctor_request(config, {})
        assert calls == []

    def test_a_shallow_request_runs_the_sweep_and_names_itself(self, config, monkeypatch, quiet_alerts):
        calls = self._calls(monkeypatch)
        made = doctor.request_run(config, deep=False)
        scheduler.check_doctor_request(config, {})
        assert [kw.get("deep", False) for kw in calls] == [False]
        reports = doctor.read_reports(config)
        assert reports["report"]["request_id"] == made["id"]
        assert reports["deep_report"] is None
        assert reports["request"] is None

    def test_a_deep_request_also_runs_the_deep_checks(self, config, monkeypatch, quiet_alerts):
        calls = self._calls(monkeypatch)
        made = doctor.request_run(config, deep=True)
        scheduler.check_doctor_request(config, {})
        deep_calls = [kw for kw in calls if kw.get("deep")]
        assert deep_calls == [{"only": tuple(sorted(doctor.DEEP_CHECKS)), "deep": True}]
        deep_report = doctor.read_reports(config)["deep_report"]
        assert deep_report["request_id"] == made["id"]
        assert [c["name"] for c in deep_report["checks"]] == ["sandbox.masks"]

    def test_the_gate_runs_every_tick_whatever_the_sweep_interval(self, make_config):
        from istota.scheduler import build_interval_gates

        cfg = make_config()
        cfg.scheduler.doctor_check_interval = 0
        gate = next(g for g in build_interval_gates(cfg) if g.name == "doctor-request")
        assert gate.background
        assert gate.enabled(cfg)
        assert gate.interval(cfg) <= cfg.scheduler.poll_interval


class TestTheCredentialGateAsksTheDaemon:
    """`vault_isolation_refusal` gates every credential and wallet route on
    whether tasks run sandboxed. Asked in the web container it probed bwrap
    there, where the stack grants no sandbox, and refused every route on a
    multi-user stack whose tasks were sandboxed in the daemon."""

    @pytest.fixture
    def multi_user(self, make_config):
        from istota.config import UserConfig

        cfg = make_config(users={"alice": UserConfig(), "bob": UserConfig()})
        db.init_db(cfg.db_path)
        return cfg

    def _local(self, monkeypatch, value):
        monkeypatch.setattr("istota.executor.effective_sandboxing", lambda config: value)

    def test_the_daemons_verdict_opens_the_gate_where_the_local_probe_fails(self, multi_user, monkeypatch):
        from istota.credentials.names import vault_isolation_refusal

        self._local(monkeypatch, False)
        doctor.record_sandbox_verdict(multi_user, True)
        assert vault_isolation_refusal(multi_user, "alice") is None

    def test_the_daemons_verdict_closes_it_too(self, multi_user, monkeypatch):
        from istota.credentials.names import VAULT_ISOLATION_REASON, vault_isolation_refusal

        self._local(monkeypatch, True)
        doctor.record_sandbox_verdict(multi_user, False)
        assert vault_isolation_refusal(multi_user, "alice") == VAULT_ISOLATION_REASON

    def test_with_no_daemon_verdict_the_local_probe_answers(self, multi_user, monkeypatch):
        from istota.credentials.names import VAULT_ISOLATION_REASON, vault_isolation_refusal

        self._local(monkeypatch, False)
        assert vault_isolation_refusal(multi_user, "alice") == VAULT_ISOLATION_REASON
        self._local(monkeypatch, True)
        assert vault_isolation_refusal(multi_user, "alice") is None

    def test_the_boot_run_publishes_the_verdict(self, config, monkeypatch, quiet_alerts):
        monkeypatch.setattr("istota.doctor.run_checks", lambda cfg, **kw: [_ok()])
        monkeypatch.setattr("istota.executor.effective_sandboxing", lambda c: True)
        scheduler.run_startup_checks(config)
        assert doctor.daemon_sandbox_verdict(config) is True
