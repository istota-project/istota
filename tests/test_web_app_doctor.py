"""Tests for the admin doctor endpoints, `GET /api/admin/doctor` and `POST .../run`.

The Health pane shows the daemon's own runs. On the Docker stack the web
process is a separate container without the daemon's sandbox profile, cgroup
or devbox sockets, so a run made here answered for the wrong container: the
sandbox, cgroup and devbox checks failed in the pane on a stack whose tasks
were confined. So the GET reads what the daemon recorded and never runs a
check, and a re-run or a deep run is a request the daemon answers on its next
tick (`tests/test_doctor_daemon_report.py` covers that half).

Redaction still happens here as well as in the daemon: this process is the one
crossing the HTTP boundary, and its config is the one it can vouch for.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

try:
    import authlib  # noqa: F401
    import fastapi  # noqa: F401

    _has_web_deps = True
except ImportError:
    _has_web_deps = False

_needs_web_deps = pytest.mark.skipif(
    not _has_web_deps,
    reason="web dependencies not installed (install with: uv sync --extra web)",
)

if _has_web_deps:
    from httpx import ASGITransport, AsyncClient

    # Imported here rather than lazily, because `istota.webui.app` calls
    # `load_config()` at module scope and that reaches `doctor.run_checks` via
    # `_validate_forge_clis`; deferred, that call would land inside a test that
    # patched `run_checks` to fail on any call.
    import istota.webui.app  # noqa: F401

from istota import doctor
from istota.doctor import DEPLOYMENT, FAIL, IMAGE, OK, SKIP, WARN, CheckResult

from .test_web_app import _make_config, _patch_app


def _results():
    return [
        CheckResult("runtime.platform", OK, "Linux x86_64", scope=IMAGE),
        CheckResult("runtime.bwrap", SKIP, "sandbox is disabled", scope=IMAGE),
        CheckResult(
            "developer.forge_binaries.gh", FAIL, "/usr/local/bin/gh does not exist",
            remedy="Install gh.", scope=IMAGE,
        ),
        CheckResult(
            "developer.forge_config_drift.gh", WARN, "configured path is not the resolved one",
            remedy="Rewrite config.toml.", scope=DEPLOYMENT,
        ),
    ]


@_needs_web_deps
class TestAdminDoctor:
    def _config_with_admin(self, tmp_path):
        from istota import db

        config = _make_config(tmp_path)
        config.db_path = tmp_path / "istota.db"
        config.admin_users = {"alice"}
        db.init_db(config.db_path)
        return config

    async def _login(self, client, username):
        import istota.webui.app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(
            return_value={"user_id": username}
        )
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    async def _call(self, tmp_path, monkeypatch, *, method="GET", path="/istota/api/admin/doctor",
                    user="alice", record=None, deep_record=None, json=None, config=None,
                    headers=None):
        config = config or self._config_with_admin(tmp_path)
        if record is not None:
            doctor.record_report(config, record)
        if deep_record is not None:
            doctor.record_report(config, deep_record, deep=True, request_id="deep1")

        def no_local_run(cfg, **kw):
            raise AssertionError("the web process ran doctor itself")

        monkeypatch.setattr("istota.doctor.run_checks", no_local_run)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, user) if user else None
            if method == "GET":
                resp = await client.get(path, cookies=cookies)
            else:
                resp = await client.post(
                    path, cookies=cookies, json=json,
                    headers={"Origin": "https://example.com"} if headers is None else headers,
                )
        return resp, config

    # ---- the auth gate ----

    async def test_requires_auth(self, tmp_path, monkeypatch):
        resp, _ = await self._call(tmp_path, monkeypatch, user=None)
        assert resp.status_code == 401

    async def test_forbidden_for_non_admin(self, tmp_path, monkeypatch):
        resp, _ = await self._call(tmp_path, monkeypatch, user="bob")
        assert resp.status_code == 403

    async def test_a_run_request_requires_an_admin(self, tmp_path, monkeypatch):
        resp, config = await self._call(
            tmp_path, monkeypatch, method="POST", path="/istota/api/admin/doctor/run",
            user="bob", json={"deep": True},
        )
        assert resp.status_code == 403
        assert doctor.read_reports(config)["request"] is None

    async def test_a_run_request_requires_the_origin(self, tmp_path, monkeypatch):
        resp, config = await self._call(
            tmp_path, monkeypatch, method="POST", path="/istota/api/admin/doctor/run",
            json={"deep": False}, headers={},
        )
        assert resp.status_code == 403
        assert doctor.read_reports(config)["request"] is None

    # ---- what the pane shows ----

    async def test_the_daemons_record_is_the_report(self, tmp_path, monkeypatch):
        resp, _ = await self._call(tmp_path, monkeypatch, record=_results())
        assert resp.status_code == 200
        body = resp.json()
        assert body["checks"] == doctor.check_payload(_results())
        assert body["checked_at"]
        assert body["status"] == FAIL
        assert body["summary"] == {OK: 1, WARN: 1, FAIL: 1, SKIP: 1}

    async def test_overall_status_ignores_skips(self, tmp_path, monkeypatch):
        resp, _ = await self._call(tmp_path, monkeypatch, record=_results()[:2])
        assert resp.json()["status"] == OK

    async def test_nothing_recorded_says_so_rather_than_running_here(self, tmp_path, monkeypatch):
        resp, _ = await self._call(tmp_path, monkeypatch)
        body = resp.json()
        assert body["checked_at"] is None
        assert [(c["name"], c["status"]) for c in body["checks"]] == [("doctor.daemon_report", WARN)]
        assert body["checks"][0]["remedy"]

    async def test_a_stale_record_is_flagged(self, tmp_path, monkeypatch):
        from istota import db

        config = self._config_with_admin(tmp_path)
        config.scheduler.doctor_check_interval = 3600
        doctor.record_report(config, _results()[:1])
        with db.get_db(config.db_path) as conn:
            row = db.shared_kv_get(conn, doctor.REPORT_NAMESPACE, "report")
            stale = row["value"].replace('"at": "', '"at": "2001-01-01T00:00:00Z", "was": "', 1)
            db.shared_kv_set(conn, doctor.REPORT_NAMESPACE, "report", stale, "test")
        resp, _ = await self._call(tmp_path, monkeypatch, config=config)
        names = {c["name"]: c["status"] for c in resp.json()["checks"]}
        assert names["doctor.daemon_report"] == WARN
        assert names["runtime.platform"] == OK

    async def test_a_fresh_record_carries_no_staleness_row(self, tmp_path, monkeypatch):
        resp, _ = await self._call(tmp_path, monkeypatch, record=_results()[:1])
        assert "doctor.daemon_report" not in {c["name"] for c in resp.json()["checks"]}

    async def test_the_last_deep_run_is_shown_with_its_own_time(self, tmp_path, monkeypatch):
        deep = [CheckResult("sandbox.masks", OK, "the databases are masked")]
        resp, _ = await self._call(tmp_path, monkeypatch, record=_results()[:1], deep_record=deep)
        body = resp.json()
        assert [c["name"] for c in body["checks"]] == ["runtime.platform", "sandbox.masks"]
        assert body["deep"] is True
        assert body["deep_checked_at"]
        assert body["deep_request_id"] == "deep1"

    async def test_a_pending_request_is_shown(self, tmp_path, monkeypatch):
        config = self._config_with_admin(tmp_path)
        made = doctor.request_run(config, deep=True)
        resp, _ = await self._call(tmp_path, monkeypatch, config=config, record=_results()[:1])
        assert resp.json()["pending"] == {"id": made["id"], "deep": True}

    async def test_the_last_answered_request_is_shown(self, tmp_path, monkeypatch):
        """What the page waits on: a report's own request_id is overwritten by
        the next sweep."""
        config = self._config_with_admin(tmp_path)
        doctor.record_answered(config, {"id": "abc123", "deep": True})
        resp, _ = await self._call(tmp_path, monkeypatch, config=config, record=_results()[:1])
        assert resp.json()["answered"] == {"id": "abc123", "deep": True}

    async def test_a_credential_in_a_recorded_detail_is_redacted(self, tmp_path, monkeypatch):
        from istota.config import DeveloperConfig

        secret = "NOT-A-REAL-TOKEN-" + "w" * 12
        config = self._config_with_admin(tmp_path)
        config.developer = DeveloperConfig(enabled=True, repos_dir="/tmp/repos", gitlab_token=secret)
        doctor.record_report(config, [CheckResult("a.b", FAIL, f"saw {secret}", remedy=f"rotate {secret}")])
        resp, _ = await self._call(tmp_path, monkeypatch, config=config)
        assert secret not in resp.text

    # ---- asking the daemon ----

    async def test_a_run_request_is_left_for_the_daemon(self, tmp_path, monkeypatch):
        resp, config = await self._call(
            tmp_path, monkeypatch, method="POST", path="/istota/api/admin/doctor/run",
            json={"deep": True},
        )
        assert resp.status_code == 202
        pending = doctor.read_reports(config)["request"]
        assert resp.json() == {"id": pending["id"], "deep": True}

    async def test_a_run_request_defaults_to_shallow(self, tmp_path, monkeypatch):
        resp, config = await self._call(
            tmp_path, monkeypatch, method="POST", path="/istota/api/admin/doctor/run", json={},
        )
        assert resp.status_code == 202
        assert doctor.read_reports(config)["request"]["deep"] is False

    async def test_a_non_boolean_deep_is_refused(self, tmp_path, monkeypatch):
        resp, config = await self._call(
            tmp_path, monkeypatch, method="POST", path="/istota/api/admin/doctor/run",
            json={"deep": "yes"},
        )
        assert resp.status_code == 400
        assert doctor.read_reports(config)["request"] is None
