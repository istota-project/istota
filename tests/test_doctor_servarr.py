"""`servarr.connection`: does each configured arr app answer with its key.

The check reaches the apps through the skill's own `ArrClient` (the same
guards, the same `private_hosts`, the same error mapping), against the fake
stack in `tests/support/fake_arr.py`. What most rows pin is the severity rule
and the order: config problems FAIL with no request, `probe=False` sends
nothing, an app being down is a WARN, a wrong key or an old app is a FAIL, and
the key never reaches a result or the log.
"""

from __future__ import annotations

import json
import logging

import httpx
import pytest

from istota.config import Config, ServarrApp, ServarrConfig
from istota.doctor import (
    CHECK_SCOPES,
    CHECKS,
    DEEP_CHECKS,
    DEPLOYMENT,
    FAIL,
    LIVE_CHECKS,
    OK,
    SKIP,
    WARN,
    check_servarr_connection,
)
from tests.support.fake_arr import APP_BY_PORT, HOST, KEY, FakeArr, api_path, fixture, url

NAME = "servarr.connection"


def _config(apps=("radarr",), *, urls=None, private_hosts=(HOST,), incomplete=(),
            keyless=()) -> Config:
    urls = urls or {}
    return Config(servarr=ServarrConfig(
        apps={app: ServarrApp(url=urls.get(app, url(app)), api_key=KEY) for app in apps},
        private_hosts=list(private_hosts),
        incomplete_apps=list(incomplete),
        keyless_apps=list(keyless),
    ))


def _run(config, fake=None, *, probe=True, resolve=None):
    fake = fake or FakeArr()
    results = check_servarr_connection(config, probe, transport=fake.transport(),
                                       resolve=resolve)
    return results, fake


def _by_app(results) -> dict:
    return {r.name.rsplit(".", 1)[1]: r for r in results}


def _serialized(results) -> str:
    return json.dumps([r.__dict__ for r in results])


class TestTheRegistry:
    def test_registered_at_deployment_scope(self):
        assert NAME in {name for name, _ in CHECKS}
        assert CHECK_SCOPES[NAME] == DEPLOYMENT
        assert NAME not in DEEP_CHECKS
        assert NAME not in LIVE_CHECKS

    def test_registered_beside_the_nextcloud_connection(self):
        names = [name for name, _ in CHECKS]
        assert names.index(NAME) == names.index("nextcloud.connection") + 1


class TestNothingConfigured:
    def test_no_apps_is_one_skip(self):
        results, fake = _run(Config())
        assert len(results) == 1
        assert results[0].name == NAME
        assert results[0].status == SKIP
        assert results[0].scope == DEPLOYMENT
        assert fake.requests == []

    def test_an_unknown_app_alone_is_not_checked(self):
        config = Config(servarr=ServarrConfig(
            apps={"bazarr": ServarrApp(url="http://10.0.0.5:6767", api_key=KEY)},
            private_hosts=[HOST],
        ))
        results, fake = _run(config)
        assert [r.status for r in results] == [SKIP]
        assert fake.requests == []


class TestConfigProblems:
    """Each is a FAIL naming the key to fix, and none sends a request."""

    @pytest.mark.parametrize("bad_url,expect", [
        ("ftp://10.0.0.5:7878", "http(s)"),
        ("10.0.0.5:7878", "http(s)"),
        ("http://10.0.0.5:7878?x=1", "query"),
        ("http://arr.example.com:7878", "private_hosts"),
    ])
    def test_a_url_no_request_should_go_to(self, bad_url, expect):
        results, fake = _run(_config(urls={"radarr": bad_url}))
        [result] = results
        assert result.name == f"{NAME}.radarr"
        assert result.status == FAIL
        assert expect in result.detail + result.remedy
        assert "[servarr" in result.detail + result.remedy
        assert fake.requests == []

    def test_userinfo_fails_and_is_never_shown(self):
        secret = "pw-in-url-zz81"
        results, fake = _run(_config(urls={"radarr": f"http://admin:{secret}@{HOST}:7878"}))
        [result] = results
        assert result.status == FAIL
        assert "user name or password" in result.detail
        assert secret not in _serialized(results)
        assert fake.requests == []

    def test_an_incomplete_table_fails(self):
        results, fake = _run(_config(apps=("sonarr",), incomplete=("radarr",)))
        by_app = _by_app(results)
        assert by_app["radarr"].status == FAIL
        assert "[servarr.apps.radarr]" in by_app["radarr"].detail
        assert "url" in by_app["radarr"].remedy and "api_key" in by_app["radarr"].remedy
        assert by_app["sonarr"].status == OK
        assert [doctor_path(r) for r in fake.requests] == ["sonarr system/status"]

    def test_a_missing_key_outside_the_daemon_skips(self, monkeypatch):
        """The key arrives in the daemon's environment; a task or a cron
        command shell never holds it, so its absence there is no verdict
        about the daemon (`_non_daemon_env_markers`, as
        `security.skill_model_credential.value` reads it)."""
        monkeypatch.setenv("ISTOTA_TASK_ID", "7")
        results, _ = _run(_config(apps=(), incomplete=("radarr",), keyless=("radarr",)))
        radarr = _by_app(results)["radarr"]
        assert radarr.status == SKIP
        assert "ISTOTA_TASK_ID" in radarr.detail

    def test_a_missing_key_in_the_daemon_fails(self, monkeypatch):
        for name in ("ISTOTA_TASK_ID", "ISTOTA_SANDBOXED", "PRECOMMIT_SCANS_REQUIRED"):
            monkeypatch.delenv(name, raising=False)
        results, _ = _run(_config(apps=(), incomplete=("radarr",), keyless=("radarr",)))
        assert _by_app(results)["radarr"].status == FAIL

    def test_a_missing_url_fails_anywhere(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_TASK_ID", "7")
        results, _ = _run(_config(apps=(), incomplete=("radarr",)))
        assert _by_app(results)["radarr"].status == FAIL

    def test_a_private_address_not_listed_fails_with_probe_off(self):
        """Guard 2 refuses it before any request, so doctor and the card say so (ISSUE-767)."""
        from istota.skills.servarr.client import config_problem

        assert "private address" in config_problem("radarr", "https://10.1.2.3:7878", [])
        assert config_problem("radarr", "https://10.1.2.3:7878", ["10.1.2.3"]) == ""
        assert config_problem("radarr", "https://arr.example.com", []) == ""

    def test_config_problems_fail_even_with_probe_off(self):
        results, _ = _run(_config(urls={"radarr": "ftp://x"}, apps=("radarr", "sonarr")),
                          probe=False)
        by_app = _by_app(results)
        assert by_app["radarr"].status == FAIL
        assert by_app["sonarr"].status == SKIP


def doctor_path(request: httpx.Request) -> str:
    return f"{APP_BY_PORT[request.url.port]} {api_path(request)}"


class TestProbeDisabled:
    def test_each_app_skips_and_nothing_is_sent(self):
        results, fake = _run(_config(apps=("sonarr", "radarr")), probe=False)
        assert [r.name for r in results] == [f"{NAME}.sonarr", f"{NAME}.radarr"]
        assert all(r.status == SKIP for r in results)
        assert all("probe" in r.detail for r in results)
        assert fake.requests == []


class TestTheProbe:
    def test_ok_names_the_version_and_the_url(self):
        results, fake = _run(_config())
        [result] = results
        assert result.status == OK
        assert fixture("system_status_radarr.json")["version"] in result.detail
        assert url("radarr") in result.detail
        assert result.remedy == ""
        assert [doctor_path(r) for r in fake.requests] == ["radarr system/status"]
        assert fake.requests[0].headers["X-Api-Key"] == KEY

    def test_one_result_per_app_in_the_fixed_order(self):
        results, _ = _run(_config(apps=("prowlarr", "lidarr", "radarr", "sonarr")))
        assert [r.name for r in results] == [
            f"{NAME}.sonarr", f"{NAME}.radarr", f"{NAME}.lidarr", f"{NAME}.prowlarr",
        ]
        assert all(r.status == OK for r in results)
        assert all(r.scope == DEPLOYMENT for r in results)

    def test_a_wrong_key_fails(self):
        fake = FakeArr().json("radarr", "system/status", {"error": "Unauthorized"}, status=401)
        [result], _ = _run(_config(), fake)
        assert result.status == FAIL
        assert "401" in result.detail
        assert "wrong or was rotated" in result.remedy

    def test_an_old_app_fails(self):
        fake = FakeArr().json("sonarr", "system/status", fixture("system_status_sonarr_v3.json"))
        [result], _ = _run(_config(apps=("sonarr",)), fake)
        assert result.status == FAIL
        assert fixture("system_status_sonarr_v3.json")["version"] in result.detail
        assert "4" in result.remedy

    @pytest.mark.parametrize("exc", [
        httpx.ConnectError("refused"),
        httpx.ConnectTimeout("dropped"),
    ])
    def test_an_unreachable_app_warns_once_with_the_firewall_remedy(self, exc):
        fake = FakeArr().on("radarr", "GET", "system/status", exc)
        [result], fake = _run(_config(), fake)
        assert result.status == WARN
        assert "DOCKER-USER" in result.remedy
        assert len(fake.requests) == 1

    def test_a_server_error_warns_and_is_not_retried(self):
        fake = FakeArr().json("radarr", "system/status", {"message": "boom"}, status=502)
        [result], fake = _run(_config(), fake)
        assert result.status == WARN
        assert "502" in result.detail
        assert len(fake.requests) == 1

    def test_a_redirect_fails_without_the_location(self):
        fake = FakeArr().on("radarr", "GET", "system/status", httpx.Response(
            302, headers={"location": "http://elsewhere.example/steal"}))
        [result], fake = _run(_config(), fake)
        assert result.status == FAIL
        assert "elsewhere.example" not in result.detail + result.remedy
        assert len(fake.requests) == 1

    def test_a_name_resolving_private_and_unlisted_warns(self):
        config = _config(urls={"radarr": "https://arr.example.com"}, private_hosts=())
        results, fake = _run(config, resolve=lambda host, port: ["192.168.1.20"])
        [result] = results
        assert result.status == WARN
        assert "private_hosts" in result.detail + result.remedy
        assert fake.requests == []

    def test_one_app_down_leaves_the_others_alone(self):
        fake = FakeArr().on("radarr", "GET", "system/status", httpx.ConnectError("refused"))
        results, _ = _run(_config(apps=("sonarr", "radarr", "lidarr")), fake)
        by_app = _by_app(results)
        assert by_app["sonarr"].status == OK
        assert by_app["radarr"].status == WARN
        assert by_app["lidarr"].status == OK


class TestTheKeyStaysOut:
    CASES = [
        ("unauthorized", lambda f: f.on("radarr", "GET", "system/status", httpx.Response(
            401, text=f"bad key {KEY}"))),
        ("server_error", lambda f: f.on("radarr", "GET", "system/status", httpx.Response(
            500, text=f"echo {KEY}"))),
        ("refused", lambda f: f.on("radarr", "GET", "system/status",
                                   httpx.ConnectError(f"refused {KEY}"))),
        ("not_json", lambda f: f.on("radarr", "GET", "system/status", httpx.Response(
            200, text=f"<html>{KEY}</html>"))),
        ("ok", lambda f: f),
    ]

    @pytest.mark.parametrize("label,script", CASES, ids=[c[0] for c in CASES])
    def test_the_key_is_absent_from_results_and_log(self, label, script, caplog):
        caplog.set_level(logging.DEBUG)
        fake = script(FakeArr())
        results, _ = _run(_config(), fake)
        assert KEY not in _serialized(results)
        assert KEY not in caplog.text

    def test_a_failed_app_logs_one_line_without_the_key(self, caplog):
        caplog.set_level(logging.WARNING, logger="istota.doctor")
        fake = FakeArr().json("radarr", "system/status", {}, status=401)
        _run(_config(), fake)
        lines = [r for r in caplog.records if "servarr_connection_check" in r.getMessage()]
        assert len(lines) == 1
        assert "app=radarr" in lines[0].getMessage()
        assert "reason=unauthorized" in lines[0].getMessage()
