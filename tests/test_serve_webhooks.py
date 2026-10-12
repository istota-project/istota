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

        from istota import serve
        from istota.webui import webhook_receiver as wr

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        parent = FastAPI()
        monkeypatch.setattr(
            "istota.webui.app._config", self._config(tmp_path), raising=False,
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

        from istota import serve
        from istota.webui import webhook_receiver as wr

        calls = []
        monkeypatch.setattr(wr, "reload_config", lambda: calls.append(1))
        monkeypatch.setattr(
            "istota.webui.app._config", self._config(tmp_path), raising=False,
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
            "istota.webui.app._config", self._config(tmp_path, location=False),
            raising=False,
        )
        parent = FastAPI()
        serve._maybe_mount_webhooks(parent)
        assert not any(
            getattr(r, "path", "").startswith("/webhooks") for r in parent.routes
        )

    def test_double_attach_is_refused(self, tmp_path, monkeypatch):
        from fastapi import FastAPI

        from istota import serve
        from istota.webui import webhook_receiver as wr

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        monkeypatch.setattr(
            "istota.webui.app._config", self._config(tmp_path), raising=False,
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

        from istota import serve
        from istota.webui import webhook_receiver as wr
        from istota.config import SmsConfig

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        monkeypatch.setattr(
            "istota.webui.app._config",
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

        from istota import serve
        from istota.webui import webhook_receiver as wr
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
            "istota.webui.app._config",
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

        from istota import serve
        from istota.webui import webhook_receiver as wr
        from istota.config import SmsConfig

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        monkeypatch.setattr(
            "istota.webui.app._config",
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

        from istota import serve
        from istota.webui import webhook_receiver as wr
        from .support.whatsapp_config import build_whatsapp_config

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        monkeypatch.setattr(
            "istota.webui.app._config",
            self._config(
                tmp_path, location=False, whatsapp=build_whatsapp_config(enabled=True),
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
        from .support.whatsapp_config import build_whatsapp_config

        monkeypatch.setattr(
            "istota.webui.app._config",
            self._config(
                tmp_path,
                location=False,
                whatsapp=build_whatsapp_config(
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

        from istota import serve
        from istota.webui import webhook_receiver as wr
        from istota.config import SmsConfig

        from .support.whatsapp_config import build_whatsapp_config

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        monkeypatch.setattr(
            "istota.webui.app._config",
            self._config(
                tmp_path,
                sms=SmsConfig(enabled=True),
                whatsapp=build_whatsapp_config(enabled=True),
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

        from istota import serve
        from istota.webui import webhook_receiver as wr
        from .support.whatsapp_config import build_whatsapp_config

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        config = self._config(
            tmp_path,
            location=False,
            whatsapp=build_whatsapp_config(enabled=True, verify_token="verify-placeholder"),
        )
        monkeypatch.setattr("istota.webui.app._config", config, raising=False)
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

        **Live coverage since the per-provider split.** This was a forward pin
        while the Cloud-only structural arms ran unconditionally under
        ``enabled``, which made an enabled ``baileys`` block a config
        ``load_config`` refused and this gate unreachable in production. The
        split landed with the nested blocks, so the state is now an ordinary
        `config.toml`; ``TestTheMountGateOnALoadedConfig`` reaches it that way.
        """
        from fastapi import FastAPI

        from istota import serve
        from .support.whatsapp_config import build_whatsapp_config

        monkeypatch.setattr(
            "istota.webui.app._config",
            self._config(
                tmp_path,
                location=False,
                whatsapp=build_whatsapp_config(enabled=True, provider="baileys"),
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

        from istota.webui import webhook_receiver as wr
        from .support.whatsapp_config import build_whatsapp_config

        config = self._config(
            tmp_path,
            location=False,
            whatsapp=build_whatsapp_config(
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

    def test_the_stack_passes_its_config_in(self, tmp_path, monkeypatch):
        """The stack's web process includes the routes at import, before the
        web app's lifespan has loaded a config; the caller hands one in."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from istota import serve
        from istota.webui import webhook_receiver as wr

        monkeypatch.setattr(wr, "reload_config", lambda: None)
        monkeypatch.setattr("istota.webui.app._config", None, raising=False)
        parent = FastAPI()
        serve._maybe_mount_webhooks(parent, self._config(tmp_path))

        with TestClient(parent) as client:
            assert client.post("/webhooks/location", json={"locations": []}).status_code == 401


@_needs_fastapi
class TestReceiverLoadsUnderAParentLifespan:
    """ISSUE-704: Starlette drops `add_event_handler("startup", ...)` without a
    word when the app was built with `lifespan=`, and the web app is. The
    receiver's token map then stayed empty and every Overland ping was a 403.
    """

    @pytest.fixture
    def stored_token(self, tmp_path, monkeypatch):
        from istota import db
        from istota.config import Config, LocationReceiverConfig, UserConfig
        from istota.credentials import store as secrets_store
        from istota.webui import webhook_receiver as wr

        monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key" * 8)
        db_path = tmp_path / "istota.db"
        db.init_db(db_path)
        secrets_store.set_secret(db_path, "alice", "overland", "ingest_token", "alice-tok")
        config = Config(
            db_path=db_path,
            users={"alice": UserConfig(display_name="Alice")},
            location=LocationReceiverConfig(enabled=True),
            workspace_path=tmp_path,
        )
        monkeypatch.setattr(wr, "load_config", lambda: config)
        for name, value in (
            ("_config", None), ("_sms_providers", None), ("_token_map", {}),
            ("_user_contexts", {}), ("_places_cache", {}), ("_sentinel_stamp", 0.0),
            ("_never_loaded_claimed", False),
        ):
            monkeypatch.setattr(wr, name, value, raising=False)
        return config

    @staticmethod
    def _batch():
        return {"locations": [{
            "type": "Feature",
            "geometry": {"type": "Point", "coordinates": [-122.4, 37.7]},
            "properties": {"timestamp": "2026-10-11T02:10:35Z"},
        }]}

    def test_a_stored_token_is_accepted_under_the_web_apps_lifespan(
        self, stored_token, monkeypatch,
    ):
        """The real web lifespan, not a bare `FastAPI()` that has none."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from istota import serve
        from istota.webui import app as web_app
        from istota.webui import webhook_receiver as wr

        monkeypatch.setattr(web_app, "load_config", lambda: stored_token)
        monkeypatch.setattr(web_app, "_config", web_app._config)
        monkeypatch.setattr(web_app, "_oauth", web_app._oauth)
        monkeypatch.setattr(web_app.signal, "signal", lambda *args: None)
        monkeypatch.setattr(web_app.web_shutdown, "install_signal_hook", lambda: None)
        # The first-use backstop off, so only the lifespan can load the map.
        monkeypatch.setattr(wr, "_never_loaded_claimed", True)
        parent = FastAPI(lifespan=web_app.lifespan)
        serve._maybe_mount_webhooks(parent, stored_token)

        with TestClient(parent) as client:
            resp = client.post(
                "/webhooks/location", params={"token": "alice-tok"}, json=self._batch(),
            )

        assert resp.status_code == 200, resp.text

    def test_one_users_broken_location_db_does_not_stop_the_web_app(
        self, stored_token, monkeypatch,
    ):
        """In the web process a raise from the load fails the whole app's
        startup, so a bad per-user DB skips that user instead."""
        import sqlite3

        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from istota import serve
        from istota.webui import webhook_receiver as wr

        def broken_init_db(path):
            raise sqlite3.OperationalError("unable to open database file")

        monkeypatch.setattr(wr.location, "init_db", broken_init_db)
        parent = FastAPI()
        serve._maybe_mount_webhooks(parent, stored_token)

        with TestClient(parent) as client:
            resp = client.post(
                "/webhooks/location", params={"token": "alice-tok"}, json=self._batch(),
            )

        assert resp.status_code == 403
        assert wr._config is stored_token

    async def test_sighup_reloads_both_the_parent_and_the_receiver(
        self, stored_token, monkeypatch,
    ):
        """The parent's SIGHUP handler is chained, not replaced."""
        import signal
        import threading
        from contextlib import asynccontextmanager

        from fastapi import FastAPI

        from istota import serve
        from istota.webui import webhook_receiver as wr

        parent_reloads = []

        @asynccontextmanager
        async def parent_lifespan(app):
            signal.signal(signal.SIGHUP, lambda *_: parent_reloads.append(1))
            yield

        parent = FastAPI(lifespan=parent_lifespan)
        serve._maybe_mount_webhooks(parent, stored_token)
        saved = signal.getsignal(signal.SIGHUP)
        try:
            async with parent.router.lifespan_context(parent):
                assert wr._token_map == {"alice-tok": "alice"}
                monkeypatch.setattr(wr, "_token_map", {})
                signal.getsignal(signal.SIGHUP)(signal.SIGHUP, None)
                for thread in threading.enumerate():
                    if thread.name == "webhook-sighup-reload":
                        thread.join(timeout=10)
        finally:
            signal.signal(signal.SIGHUP, saved)

        assert parent_reloads == [1]
        assert wr._token_map == {"alice-tok": "alice"}

    def test_a_receiver_that_was_never_loaded_loads_on_first_use(self, stored_token):
        """A missed startup costs one slow request, not a permanent 403."""
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from istota.webui import webhook_receiver as wr

        bare = FastAPI()
        bare.include_router(wr.location_router)
        resp = TestClient(bare).post(
            "/webhooks/location", params={"token": "alice-tok"}, json=self._batch(),
        )

        assert resp.status_code == 200, resp.text

    def test_the_first_use_load_runs_once(self, stored_token, monkeypatch):
        """Claimed under the lock: a config that will not load is not retried
        on every request."""
        from istota.webui import webhook_receiver as wr

        calls = []

        def failing_reload():
            calls.append(1)
            raise RuntimeError("config does not load")

        monkeypatch.setattr(wr, "reload_config", failing_reload)
        wr._load_if_never_loaded()
        wr._load_if_never_loaded()

        assert calls == [1]

    def test_a_never_loaded_sms_route_loads_rather_than_404s(self, stored_token, monkeypatch):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient

        from istota.webui import webhook_receiver as wr

        calls = []
        monkeypatch.setattr(wr, "reload_config", lambda: calls.append(1))
        bare = FastAPI()
        bare.include_router(wr.sms_router)
        TestClient(bare).post("/webhooks/sms/twilio", content=b"")

        assert calls == [1]

    def test_nothing_in_the_tree_registers_a_startup_event(self):
        """The web app has a lifespan, and Starlette drops a startup handler
        on such an app without saying so.

        Reads `src/` off disk, so `scripts/qt` does not select it on a source
        change; name this file in the close.
        """
        import re
        from pathlib import Path

        import istota

        pattern = re.compile(
            r"""(on_event|add_event_handler)\(\s*['"]startup|\bon_startup\b"""
        )
        root = Path(istota.__file__).parent
        offenders = []
        for path in root.rglob("*.py"):
            if pattern.search(path.read_text(encoding="utf-8")):
                offenders.append(str(path.relative_to(root)))
        assert offenders == []

