"""Tests for the admin doctor endpoint, `GET /api/admin/doctor`.

Read-only, in the shape `admin_config_view` and `admin_logs` already
establish — and those two modules are *defined* by the property this endpoint
has to inherit. `admin_config_view` exists so credentials never leave the
process; `admin_logs` so a caller names a source id, never a path. Doctor's
`detail` carries observed paths and raw exception text, so redaction is
asserted here rather than assumed from the renderer.

The deep run is the other half. It spawns a bubblewrap subprocess, so it gets a
single-flight lock and a bounded timeout: a second concurrent request is told
so rather than queued behind a namespace spawn, and a probe that hangs is
reported rather than holding the request open.
"""

from __future__ import annotations

import asyncio
import json
import threading
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

    # Imported here rather than lazily inside `_patch_app`, because
    # `istota.web_app` calls `load_config()` at module scope and that
    # reaches `doctor.run_checks` via `_validate_forge_clis`. Deferred,
    # the import lands inside whichever test patched `run_checks` first
    # and its call is counted as the endpoint's.
    import istota.web_app  # noqa: F401

from istota.doctor import DEPLOYMENT, FAIL, IMAGE, OK, SKIP, WARN, CheckResult

from .test_web_app import _make_config, _patch_app


def _results():
    return [
        CheckResult("runtime.platform", OK, "Linux x86_64", scope=IMAGE),
        CheckResult("runtime.bwrap", SKIP, "sandbox is disabled", scope=IMAGE),
        CheckResult(
            "developer.forge_binaries.gh",
            FAIL,
            "/usr/local/bin/gh does not exist",
            remedy="Install gh.",
            scope=IMAGE,
        ),
        CheckResult(
            "developer.forge_config_drift.gh",
            WARN,
            "configured path is not the resolved one",
            remedy="Rewrite config.toml.",
            scope=DEPLOYMENT,
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
        import istota.web_app as mod

        mod._oauth.nextcloud.authorize_access_token = AsyncMock(
            return_value={"user_id": username}
        )
        resp = await client.get("/istota/callback", follow_redirects=False)
        return resp.cookies

    async def _get(self, tmp_path, monkeypatch, path="/istota/api/admin/doctor",
                   user="alice", results=None, run_checks=None):
        config = self._config_with_admin(tmp_path)
        if run_checks is None:
            def run_checks(cfg, **kw):
                return _results() if results is None else results
        monkeypatch.setattr("istota.doctor.run_checks", run_checks)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, user) if user else None
            return await client.get(path, cookies=cookies)

    # ---- the auth gate ----

    async def test_requires_auth(self, tmp_path, monkeypatch):
        resp = await self._get(tmp_path, monkeypatch, user=None)
        assert resp.status_code == 401

    async def test_forbidden_for_non_admin(self, tmp_path, monkeypatch):
        resp = await self._get(tmp_path, monkeypatch, user="bob")
        assert resp.status_code == 403

    async def test_admin_gets_the_report(self, tmp_path, monkeypatch):
        resp = await self._get(tmp_path, monkeypatch)
        assert resp.status_code == 200

    # ---- the payload ----

    async def test_shape_matches_render_json(self, tmp_path, monkeypatch):
        """The image tier asserts over `istota doctor --json`; the dashboard
        must not need a second shape to learn.

        Asserted against `render_json`'s actual output, not against a hardcoded
        dict — two surfaces each checked against their own literal can diverge
        with both suites green, which is what the previous version of this did.
        """
        from istota.doctor import render_json

        resp = await self._get(tmp_path, monkeypatch)
        body = resp.json()
        assert body["checks"] == json.loads(render_json(_results(), secrets=()))

    async def test_carries_a_summary_and_an_overall_status(self, tmp_path, monkeypatch):
        resp = await self._get(tmp_path, monkeypatch)
        body = resp.json()
        assert body["summary"] == {"ok": 1, "warn": 1, "fail": 1, "skip": 1}
        assert body["status"] == FAIL

    async def test_overall_status_is_warn_when_nothing_failed(self, tmp_path, monkeypatch):
        results = [
            CheckResult("a.b", OK, "fine"),
            CheckResult("c.d", WARN, "iffy", remedy="look"),
        ]
        resp = await self._get(tmp_path, monkeypatch, results=results)
        assert resp.json()["status"] == WARN

    async def test_overall_status_is_ok_when_only_ok_and_skip(self, tmp_path, monkeypatch):
        results = [CheckResult("a.b", OK, "fine"), CheckResult("c.d", SKIP, "n/a")]
        resp = await self._get(tmp_path, monkeypatch, results=results)
        assert resp.json()["status"] == OK

    async def test_remedies_survive_to_the_client(self, tmp_path, monkeypatch):
        """A finding an operator cannot act on is a log line, not a check."""
        resp = await self._get(tmp_path, monkeypatch)
        failing = [c for c in resp.json()["checks"] if c["status"] == FAIL]
        assert failing[0]["remedy"] == "Install gh."

    # ---- redaction ----

    async def test_a_credential_in_a_detail_is_redacted(self, tmp_path, monkeypatch):
        """Check authors are forbidden from putting a credential in `detail`.
        The endpoint does not take their word for it: `detail` carries raw
        exception text, and this crosses an HTTP boundary."""
        from istota.config import DeveloperConfig

        secret = "NOT-A-REAL-TOKEN-" + "w" * 12
        config = self._config_with_admin(tmp_path)
        config.developer = DeveloperConfig(
            enabled=True, repos_dir="/tmp/repos", gitlab_token=secret
        )
        monkeypatch.setattr(
            "istota.doctor.run_checks",
            lambda cfg, **kw: [
                CheckResult("x.y", FAIL, f"rejected {secret}", remedy=f"rotate {secret}")
            ],
        )
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/doctor", cookies=cookies)
        assert secret not in resp.text
        assert "[redacted]" in resp.text

    # ---- the deep control ----

    async def test_shallow_by_default(self, tmp_path, monkeypatch):
        seen = {}

        def _capture(cfg, **kw):
            seen.update(kw)
            return _results()

        await self._get(tmp_path, monkeypatch, run_checks=_capture)
        assert seen.get("deep") is False

    async def test_deep_is_opt_in(self, tmp_path, monkeypatch):
        seen = {}

        def _capture(cfg, **kw):
            seen.update(kw)
            return _results()

        await self._get(
            tmp_path, monkeypatch, path="/istota/api/admin/doctor?deep=1", run_checks=_capture
        )
        assert seen.get("deep") is True

    async def test_the_deep_phase_runs_only_the_deep_checks(self, tmp_path, monkeypatch):
        """Two phases, not one. The registry's own bounds cover the shallow
        phase; the outer timer then covers only what it can actually bound."""
        calls = []

        def _capture(cfg, **kw):
            calls.append(kw)
            return _results() if not kw.get("deep") else []

        await self._get(
            tmp_path, monkeypatch, path="/istota/api/admin/doctor?deep=1", run_checks=_capture
        )
        assert len(calls) == 2
        assert calls[0]["deep"] is False
        assert calls[1]["deep"] is True
        from istota.doctor import DEEP_CHECKS

        assert set(calls[1]["only"]) == set(DEEP_CHECKS)

    async def test_a_second_deep_run_gets_409(self, tmp_path, monkeypatch):
        """The deep probe spawns a namespace. A second concurrent request is
        told so rather than queued behind one."""
        import istota.web_app as mod

        started = threading.Event()
        release = threading.Event()

        def _gated(cfg, **kw):
            if not kw.get("deep"):
                return _results()
            started.set()
            assert release.wait(timeout=10), "deep run was never released"
            return []

        monkeypatch.setattr("istota.doctor.run_checks", _gated)
        config = self._config_with_admin(tmp_path)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            first = asyncio.create_task(
                client.get("/istota/api/admin/doctor?deep=1", cookies=cookies, timeout=30)
            )
            await asyncio.to_thread(started.wait, 10)
            second = await client.get("/istota/api/admin/doctor?deep=1", cookies=cookies)
            release.set()
            first_resp = await first

        assert second.status_code == 409
        assert "in flight" in second.json()["detail"]
        assert first_resp.status_code == 200
        assert mod._doctor_deep_slot.acquire(blocking=False)
        mod._doctor_deep_slot.release()

    async def test_a_shallow_run_is_not_blocked_by_a_deep_run(self, tmp_path, monkeypatch):
        """Only the namespace-spawning path is single-flight; the cheap read
        stays available while a deep run is going."""
        import istota.web_app as mod

        assert mod._doctor_deep_slot.acquire(blocking=False)
        try:
            resp = await self._get(tmp_path, monkeypatch)
            assert resp.status_code == 200
        finally:
            mod._doctor_deep_slot.release()

    async def test_the_slot_is_released_when_the_run_raises(self, tmp_path, monkeypatch):
        """A wedged slot would make the control dead until the next restart.

        `raise_app_exceptions=False` so the transport reports what a real server
        would return instead of re-raising into the test — the assertion is
        about the slot, and a propagated exception proves nothing about it.
        """
        import istota.web_app as mod

        def _boom(cfg, **kw):
            if kw.get("deep"):
                raise RuntimeError("probe exploded")
            return _results()

        monkeypatch.setattr("istota.doctor.run_checks", _boom)
        config = self._config_with_admin(tmp_path)
        app = _patch_app(config)
        transport = ASGITransport(app=app, raise_app_exceptions=False)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")
            resp = await client.get("/istota/api/admin/doctor?deep=1", cookies=cookies)
        assert resp.status_code == 500
        assert mod._doctor_deep_slot.acquire(blocking=False)
        mod._doctor_deep_slot.release()

    async def test_the_slot_is_released_after_a_normal_deep_run(self, tmp_path, monkeypatch):
        import istota.web_app as mod

        resp = await self._get(tmp_path, monkeypatch, path="/istota/api/admin/doctor?deep=1")
        assert resp.status_code == 200
        assert mod._doctor_deep_slot.acquire(blocking=False)
        mod._doctor_deep_slot.release()

    async def test_a_deep_run_that_overruns_is_reported_not_hung(self, tmp_path, monkeypatch):
        """`check_sandbox_masks` bounds its own subprocess, but a check that
        hangs some other way must not hold the request open forever."""
        import istota.web_app as mod

        monkeypatch.setattr(mod, "_doctor_deep_timeout", lambda: 0.1)
        finish = threading.Event()

        def _hang(cfg, **kw):
            if not kw.get("deep"):
                return _results()
            finish.wait(timeout=10)
            return []

        try:
            resp = await self._get(
                tmp_path, monkeypatch, path="/istota/api/admin/doctor?deep=1", run_checks=_hang
            )
            assert resp.status_code == 200
            body = resp.json()
            assert body["status"] == FAIL

            # The timeout is reported under a name that is not a registry entry:
            # the run failed, and blaming `sandbox.masks` would assert a verdict
            # on a check that may never have been reached.
            names = [c["name"] for c in body["checks"]]
            assert "doctor.deep_run" in names
            assert "sandbox.masks" not in names

            # And the shallow findings survive — an operator who opened the page
            # to read a real failure still sees it.
            assert "developer.forge_binaries.gh" in names
        finally:
            finish.set()

    async def test_a_timed_out_deep_run_still_holds_the_slot(self, tmp_path, monkeypatch):
        """The regression that made the lock decorative.

        `asyncio.wait_for` cancels the await, not the OS thread. Releasing on
        the timeout says "nothing is running" while bwrap is still spawning, so
        the next request starts a second concurrent namespace. The slot must
        stay taken until the thread itself finishes.
        """
        import istota.web_app as mod

        monkeypatch.setattr(mod, "_doctor_deep_timeout", lambda: 0.1)
        finish = threading.Event()

        def _hang(cfg, **kw):
            if not kw.get("deep"):
                return _results()
            finish.wait(timeout=10)
            return []

        monkeypatch.setattr("istota.doctor.run_checks", _hang)
        config = self._config_with_admin(tmp_path)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        try:
            async with AsyncClient(transport=transport, base_url="https://example.com") as client:
                cookies = await self._login(client, "alice")
                first = await client.get("/istota/api/admin/doctor?deep=1", cookies=cookies)
                assert first.status_code == 200
                assert first.json()["status"] == FAIL

                # The worker is still inside `_hang`. A second deep request must
                # be refused, not admitted alongside it.
                second = await client.get("/istota/api/admin/doctor?deep=1", cookies=cookies)
                assert second.status_code == 409
        finally:
            finish.set()

        # And once the thread does finish, the slot comes back.
        for _ in range(100):
            if mod._doctor_deep_slot.acquire(blocking=False):
                mod._doctor_deep_slot.release()
                break
            await asyncio.sleep(0.05)
        else:
            raise AssertionError("the slot was never released by the worker thread")

    async def test_the_endpoint_does_not_block_the_event_loop(self, tmp_path, monkeypatch):
        """`run_checks` is synchronous and spawns subprocesses; run inline it
        would stall every other request for its duration.

        The tick count is read *while the request is still in flight*. Read
        after awaiting the ticker it would be 10 either way — blocking only
        delays the ticks, it cannot lose them — which is a test that cannot
        fail.
        """
        ticks = []
        ticking = threading.Event()

        def _slowish(cfg, **kw):
            ticking.wait(timeout=5)
            return _results()

        config = self._config_with_admin(tmp_path)
        monkeypatch.setattr("istota.doctor.run_checks", _slowish)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="https://example.com") as client:
            cookies = await self._login(client, "alice")

            async def _tick():
                for _ in range(10):
                    ticks.append(1)
                    await asyncio.sleep(0.01)
                # Only now let the (threaded) check return.
                ticking.set()

            ticker = asyncio.create_task(_tick())
            resp = await client.get("/istota/api/admin/doctor", cookies=cookies)
            await ticker

        # Run inline on the loop, `_slowish` would block until `ticking` was set
        # — which only the ticker sets — and the request would deadlock rather
        # than return. Reaching here at all is the assertion.
        assert resp.status_code == 200
        assert len(ticks) == 10
