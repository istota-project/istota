"""`istota serve` runs the GPS webhook receiver inside the web app's process.

Two bugs lived here, neither reachable in production — which runs the
receiver as its own uvicorn behind nginx, so this code path only executes
in the local single-user install.
"""

from __future__ import annotations

import pytest


_needs_fastapi = pytest.mark.skipif(
    pytest.importorskip("fastapi", reason="fastapi not installed") is None,
    reason="fastapi not installed",
)


@_needs_fastapi
class TestServeWebhookMount:
    def _config(self, tmp_path, *, enabled=True):
        from istota.config import Config, LocationReceiverConfig

        return Config(
            db_path=tmp_path / "istota.db",
            location=LocationReceiverConfig(enabled=enabled),
            nextcloud_mount_path=tmp_path / "workspace",
        )

    def test_receiver_answers_at_the_documented_path(self, tmp_path, monkeypatch):
        """Not /webhooks/webhooks/location — the router carries its own prefix."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from istota import serve, webhook_receiver as wr

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        parent = FastAPI()
        monkeypatch.setattr(
            "istota.web_app._config", self._config(tmp_path), raising=False,
        )
        serve._maybe_mount_webhooks(parent)

        with TestClient(parent) as client:
            resp = client.post("/webhooks/location", json={"locations": []})
        # 401 is the receiver answering (no token supplied). A 404 would mean
        # the path landed somewhere else.
        assert resp.status_code == 401

    def test_mount_loads_the_token_map(self, tmp_path, monkeypatch):
        """Starlette does not run a mounted sub-app's lifespan, so the parent
        has to call reload_config itself or every request 403s forever."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from istota import serve, webhook_receiver as wr

        calls = []
        monkeypatch.setattr(wr, "reload_config", lambda: calls.append(1))
        monkeypatch.setattr(
            "istota.web_app._config", self._config(tmp_path), raising=False,
        )
        parent = FastAPI()
        serve._maybe_mount_webhooks(parent)

        with TestClient(parent):
            pass

        assert calls, "reload_config never ran, so the token map stayed empty"

    def test_disabled_location_attaches_nothing(self, tmp_path, monkeypatch):
        from fastapi import FastAPI

        from istota import serve

        monkeypatch.setattr(
            "istota.web_app._config", self._config(tmp_path, enabled=False),
            raising=False,
        )
        parent = FastAPI()
        serve._maybe_mount_webhooks(parent)
        assert not any(
            getattr(r, "path", "").startswith("/webhooks") for r in parent.routes
        )

    def test_double_attach_is_refused(self, tmp_path, monkeypatch):
        from fastapi import FastAPI

        from istota import serve, webhook_receiver as wr

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        monkeypatch.setattr(
            "istota.web_app._config", self._config(tmp_path), raising=False,
        )
        parent = FastAPI()
        serve._maybe_mount_webhooks(parent)
        serve._maybe_mount_webhooks(parent)
        routes = [
            r for r in parent.routes
            if getattr(r, "path", "").startswith("/webhooks")
        ]
        assert len(routes) == 1
