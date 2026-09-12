"""`istota serve` runs enabled webhook routes inside the web app's process.

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
    def _config(self, tmp_path, *, location=True, sms=None, whatsapp=None):
        from istota.config import (
            Config,
            LocationReceiverConfig,
            SmsConfig,
            WhatsAppConfig,
        )

        return Config(
            db_path=tmp_path / "istota.db",
            location=LocationReceiverConfig(enabled=location),
            sms=sms or SmsConfig(),
            whatsapp=whatsapp or WhatsAppConfig(),
            workspace_path=tmp_path / "workspace",
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
            "istota.web_app._config", self._config(tmp_path, location=False),
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

    def test_sms_only_attaches_both_fixed_provider_routes(self, tmp_path, monkeypatch):
        from fastapi import FastAPI

        from istota import serve, webhook_receiver as wr
        from istota.config import SmsConfig

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        monkeypatch.setattr(
            "istota.web_app._config",
            self._config(tmp_path, location=False, sms=SmsConfig(enabled=True)),
            raising=False,
        )
        parent = FastAPI()
        serve._maybe_mount_webhooks(parent)

        paths = {getattr(route, "path", "") for route in parent.routes}
        assert "/webhooks/location" not in paths
        assert "/webhooks/sms/twilio" in paths
        assert "/webhooks/sms/telnyx" in paths

    def test_callback_only_provider_attaches_sms_routes(self, tmp_path, monkeypatch):
        from fastapi import FastAPI

        from istota import serve, webhook_receiver as wr
        from istota.config import SmsConfig, TelnyxSmsConfig

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        sms = SmsConfig(
            telnyx=TelnyxSmsConfig(
                api_key="api-placeholder",
                public_key="public-placeholder",
                messaging_profile_id="profile-placeholder",
            )
        )
        monkeypatch.setattr(
            "istota.web_app._config",
            self._config(tmp_path, location=False, sms=sms),
            raising=False,
        )
        parent = FastAPI()
        serve._maybe_mount_webhooks(parent)

        paths = {getattr(route, "path", "") for route in parent.routes}
        assert "/webhooks/sms/twilio" in paths
        assert "/webhooks/sms/telnyx" in paths

    def test_location_and_sms_routes_each_attach_once(self, tmp_path, monkeypatch):
        from fastapi import FastAPI

        from istota import serve, webhook_receiver as wr
        from istota.config import SmsConfig

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        monkeypatch.setattr(
            "istota.web_app._config",
            self._config(tmp_path, sms=SmsConfig(enabled=True)),
            raising=False,
        )
        parent = FastAPI()
        serve._maybe_mount_webhooks(parent)
        serve._maybe_mount_webhooks(parent)

        paths = [
            getattr(route, "path", "")
            for route in parent.routes
            if getattr(route, "path", "").startswith("/webhooks")
        ]
        assert paths.count("/webhooks/location") == 1
        assert paths.count("/webhooks/sms/twilio") == 1
        assert paths.count("/webhooks/sms/telnyx") == 1

    def test_whatsapp_only_attaches_the_one_fixed_route(self, tmp_path, monkeypatch):
        """The combined process is the standalone install's whole webhook host.

        ``webhook_receiver`` includes this router unconditionally, so
        production behind nginx has served the path since stage 2; ``istota
        serve`` answered 404 at it until the gate below existed.
        """
        from fastapi import FastAPI

        from istota import serve, webhook_receiver as wr
        from istota.config import WhatsAppConfig

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        monkeypatch.setattr(
            "istota.web_app._config",
            self._config(
                tmp_path, location=False, whatsapp=WhatsAppConfig(enabled=True),
            ),
            raising=False,
        )
        parent = FastAPI()
        serve._maybe_mount_webhooks(parent)

        paths = {getattr(route, "path", "") for route in parent.routes}
        assert "/webhooks/whatsapp" in paths
        assert "/webhooks/location" not in paths
        assert not any(path.startswith("/webhooks/sms/") for path in paths)

    def test_a_disabled_whatsapp_block_attaches_nothing(self, tmp_path, monkeypatch):
        """No callback-only shape, unlike SMS.

        A complete but inactive SMS provider block keeps its routes so late
        delivery callbacks still authenticate during a switch. WhatsApp has
        one account and the route refuses every request while ``enabled`` is
        false, so mounting it would answer 403 where the absence answers 404
        and would buy nothing.
        """
        from fastapi import FastAPI

        from istota import serve
        from istota.config import WhatsAppConfig

        monkeypatch.setattr(
            "istota.web_app._config",
            self._config(
                tmp_path,
                location=False,
                whatsapp=WhatsAppConfig(
                    waba_id="100000000000001",
                    phone_number_id="100000000000002",
                    business_phone_number="+15551230000",
                    access_token="token-placeholder",
                    app_secret="app-secret-placeholder",
                    verify_token="verify-placeholder",
                ),
            ),
            raising=False,
        )
        parent = FastAPI()
        serve._maybe_mount_webhooks(parent)

        assert not [
            r for r in parent.routes
            if getattr(r, "path", "").startswith("/webhooks")
        ]

    def test_all_three_surfaces_share_one_mount_and_attach_once(
        self, tmp_path, monkeypatch,
    ):
        from fastapi import FastAPI

        from istota import serve, webhook_receiver as wr
        from istota.config import SmsConfig, WhatsAppConfig

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        monkeypatch.setattr(
            "istota.web_app._config",
            self._config(
                tmp_path,
                sms=SmsConfig(enabled=True),
                whatsapp=WhatsAppConfig(enabled=True),
            ),
            raising=False,
        )
        parent = FastAPI()
        serve._maybe_mount_webhooks(parent)
        serve._maybe_mount_webhooks(parent)

        paths = [
            getattr(route, "path", "")
            for route in parent.routes
            if getattr(route, "path", "").startswith("/webhooks")
        ]
        assert paths.count("/webhooks/location") == 1
        assert paths.count("/webhooks/sms/twilio") == 1
        # Two routes at one path: the GET handshake and the POST.
        assert paths.count("/webhooks/whatsapp") == 2

    def test_the_mounted_whatsapp_route_is_the_real_handler(
        self, tmp_path, monkeypatch,
    ):
        """A route at the right path is not the same as a route that answers.

        The GET handshake refuses a wrong verify token with 403, which only
        the receiver's own handler produces. A 404 would mean the path landed
        somewhere else and a 405 would mean only the POST attached.
        """
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from istota import serve, webhook_receiver as wr
        from istota.config import WhatsAppConfig

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        config = self._config(
            tmp_path,
            location=False,
            whatsapp=WhatsAppConfig(enabled=True, verify_token="verify-placeholder"),
        )
        monkeypatch.setattr("istota.web_app._config", config, raising=False)
        monkeypatch.setattr(wr, "_config", config, raising=False)
        parent = FastAPI()
        serve._maybe_mount_webhooks(parent)

        with TestClient(parent) as client:
            resp = client.get(
                "/webhooks/whatsapp",
                params={
                    "hub.mode": "subscribe",
                    "hub.verify_token": "wrong",
                    "hub.challenge": "1234",
                },
            )

        assert resp.status_code == 403

    def test_a_provider_receiving_over_its_own_transport_mounts_nothing(
        self, tmp_path, monkeypatch,
    ):
        """``/webhooks/whatsapp`` is Meta's signed callback and nothing else
        answers there.

        A deployment that selected an adapter with no HTTP callback of its own
        would publish a route nothing can authenticate — so the mount gate is
        `enabled` *and* an active provider that has a webhook at all. It is
        deliberately not "some built adapter has one", which is
        ``callback_only_names`` and would keep Meta's route alive on a
        deployment that switched away from Cloud; that is the SMS rule, and
        this surface's is that turning the block off turns the account off.

        **A forward pin rather than live coverage.** The config it builds is
        one ``load_config`` currently refuses — the Cloud-only structural arms
        (``waba_id`` and its neighbours) still run unconditionally under
        ``enabled``, so an enabled ``baileys`` block fails the load, and the
        per-provider split is Stage 3's. Constructing ``WhatsAppConfig``
        directly is what reaches the state at all. So this gate is inert in
        production until that lands, and the test exists to keep it correct
        when it stops being.
        """
        from fastapi import FastAPI

        from istota import serve
        from istota.config import WhatsAppConfig

        monkeypatch.setattr(
            "istota.web_app._config",
            self._config(
                tmp_path,
                location=False,
                whatsapp=WhatsAppConfig(enabled=True, provider="baileys"),
            ),
            raising=False,
        )
        parent = FastAPI()
        serve._maybe_mount_webhooks(parent)

        assert not [
            r for r in parent.routes
            if getattr(r, "path", "").startswith("/webhooks")
        ]

    def test_the_handlers_refuse_on_the_predicate_the_mount_reads(
        self, tmp_path, monkeypatch,
    ):
        """Asked again at the route rather than left to the mount.

        ``webhook_receiver`` includes its own router unconditionally, so a
        deployment running the standalone receiver has both handlers attached
        whatever the provider is — and a combined process can have its config
        reloaded underneath an already-included router. Both answer 404, which
        is what the absence of the route answers and what "turning the block
        off is turning the account off" has always meant.
        """
        from fastapi.testclient import TestClient

        from istota import webhook_receiver as wr
        from istota.config import WhatsAppConfig

        config = self._config(
            tmp_path,
            location=False,
            whatsapp=WhatsAppConfig(
                enabled=True,
                provider="baileys",
                verify_token="verify-placeholder",
                app_secret="app-secret-placeholder",
            ),
        )
        monkeypatch.setattr(wr, "_config", config, raising=False)
        client = TestClient(wr.app)

        handshake = client.get("/webhooks/whatsapp", params={
            "hub.mode": "subscribe",
            "hub.verify_token": "verify-placeholder",
            "hub.challenge": "1234",
        })
        batch = client.post(
            "/webhooks/whatsapp",
            content=b"{}",
            headers={"content-type": "application/json"},
        )

        assert handshake.status_code == 404
        assert batch.status_code == 404
