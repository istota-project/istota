"""The admin pairing API: who may reach it, and what it will not hand over.

Driven against `web_app`'s own admin dependencies (`_require_admin`,
`_verify_origin`) in the pattern of `tests/test_web_admin_logs.py`. Not
`web_router_stubs.py`: that is FastAPI-only plumbing for the five *module*
routers, which are mounted separately and have their stubs replaced through
`dependency_overrides`. These routes are declared in `web_app.py` itself, so
there is no stub to override.

**The property the whole design rests on is that the pairing code appears in no
JSON body**, and it is asserted over the state, stream and index endpoints
together rather than one at a time — a per-endpoint test passes while a sibling
leaks, which for a full-account WhatsApp credential is the one failure worth
covering exhaustively.

Two other things here are not about a route at all. The sandbox-bound-relay
refusal is checked against `sandbox_plan.build_mount_plan`'s *own* emitted
mounts rather than against a list this file keeps, so a new bind of a
deployment-owned directory fails the walk instead of quietly escaping the
predicate. And the four new `[whatsapp.baileys]` keys are held out of
`_WHATSAPP_PROVIDER_FIELDS`, which two of them would otherwise break in a way
nothing else here would notice: they carry truthy defaults, so an entry there
would make the Baileys adapter read as configured on every deployment and be
built as a callback-only adapter on a Cloud one.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

from istota import db
from istota.config import Config, SiteConfig, UserConfig, WebConfig
from istota.transport.whatsapp import baileys_bridge, pairing_relay

from .support.drift import source_of
from .support.whatsapp_config import build_whatsapp_config

pytestmark = _needs_web_deps

ADMIN = "alice"
OTHER = "bob"
QR = "2@fake-pairing-payload-not-a-real-credential,AAAA/BBBB="

PAIRING = "/istota/api/admin/connections/whatsapp/pairing"
INDEX = "/istota/api/admin/connections"

#: Every route this stage adds, so the admin gate is enumerated rather than
#: sampled. The five pairing routes plus the section index.
ALL_GET_ROUTES = (
    INDEX,
    PAIRING,
    f"{PAIRING}/stream",
    f"{PAIRING}/qr.svg",
)


#: Every request here is bounded, and that is not belt and braces. httpx's
#: `ASGITransport` collects the whole response body before it returns, so any
#: request that reaches the SSE generator never comes back — a route gate
#: removed by mistake would wedge this file rather than turn a test red, which
#: is the opposite of what the enumerations below are for. Found by control:
#: removing the `pairing_enabled` gate hung the run for thirteen minutes with
#: nothing on stdout.
_HTTP_TIMEOUT = 10.0


async def _get(client, path, cookies=None):
    """A bounded GET. See `_HTTP_TIMEOUT`."""
    return await asyncio.wait_for(
        client.get(path, cookies=cookies), _HTTP_TIMEOUT,
    )


def _make_config(tmp_path, *, admins=(ADMIN,), **whatsapp) -> Config:
    whatsapp.setdefault("enabled", True)
    whatsapp.setdefault("provider", "baileys")
    state = tmp_path / "state"
    state.mkdir(parents=True, exist_ok=True)
    config = Config(
        db_path=state / "istota.db",
        workspace_path=tmp_path / "mount",
        temp_dir=tmp_path / "tmp",
        site=SiteConfig(hostname="example.com"),
        users={ADMIN: UserConfig(display_name="Alice"), OTHER: UserConfig()},
        web=WebConfig(
            enabled=True,
            port=8766,
            oauth2_provider="https://cloud.example.com",
            oauth2_client_id="istota-web",
            oauth2_client_secret="test-secret",
            session_secret_key="test-session-key",
        ),
        whatsapp=build_whatsapp_config(**whatsapp),
    )
    config.admin_users = set(admins)
    db.init_db(config.db_path)
    return config


def _patch_app(config):
    import istota.web_app as mod

    mod._config = config
    mod.app.state.istota_config = config
    oauth = MagicMock()
    oauth.nextcloud = MagicMock()
    mod._oauth = oauth
    return mod.app


async def _login(client, username):
    import istota.web_app as mod

    mod._oauth.nextcloud.authorize_access_token = AsyncMock(
        return_value={"user_id": username}
    )
    resp = await client.get("/istota/callback", follow_redirects=False)
    return resp.cookies


class _Status:
    """The one fact the start route reads off a published bridge.

    A plain object rather than a `MagicMock`, because the route's gate is
    `not status.get("fatal_is_permanent")` and a mock answers every key
    truthily — which would make the live-session refusal untestable in the
    direction that matters.
    """

    def __init__(self, *, fatal_is_permanent: bool):
        self.payload = {
            "listening": True,
            "connected": True,
            "ready": not fatal_is_permanent,
            "fatal_reason": "logged_out" if fatal_is_permanent else None,
            "fatal_is_permanent": fatal_is_permanent,
            "restarts": 3,
        }


@pytest.fixture
def publish_status(monkeypatch):
    """Publish (or withhold) a bridge status the way `read_status` would.

    `None` is the production web unit on the Ansible deployment — the bridge
    lives in the scheduler — so it is the default and several tests depend on
    it being what an unpublished bridge looks like.
    """

    def publish(status: _Status | None) -> None:
        monkeypatch.setattr(
            baileys_bridge,
            "read_status",
            lambda: None if status is None else dict(status.payload),
        )

    publish(None)
    return publish


@pytest.fixture
async def env(tmp_path, publish_status):
    config = _make_config(tmp_path)
    app = _patch_app(config)
    transport = ASGITransport(app=app)
    async with AsyncClient(
        transport=transport, base_url="https://example.com"
    ) as client:
        cookies = await _login(client, ADMIN)
        yield config, client, cookies


def _write_request(config, *, force: bool = False, window_seconds: float = 300.0):
    with db.get_db(config.db_path) as conn:
        return db.request_whatsapp_pairing(
            conn, ADMIN, window_seconds=window_seconds, force=force,
        )


def _row(config) -> dict | None:
    with db.get_db(config.db_path) as conn:
        return db.read_whatsapp_pairing(conn)


def _write_relay(
    config,
    window_id: str,
    *,
    state: str = pairing_relay.STATE_AWAITING_SCAN,
    qr: str | None = QR,
    qr_seq: int = 3,
    ttl: float = 300.0,
) -> Path:
    path = baileys_bridge.default_pairing_relay_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    assert pairing_relay.write_relay(
        path,
        pairing_relay.build_payload(
            window_id=window_id,
            state=state,
            expires_at=time.time() + ttl,
            qr=qr,
            qr_seq=qr_seq,
            message="scan the code",
        ),
    )
    return path


class _FakeRequest:
    """Enough `Request` for an SSE generator, with a scripted disconnect.

    `ASGITransport` collects the whole response body before it returns, so a
    generator that polls for ever cannot be driven through the HTTP client at
    all — it hangs rather than failing, which reports nothing about its
    subject. `tests/test_chat_room_stream.py` drives the room stream the same
    way and for the same reason.
    """

    def __init__(self, *, disconnect_after: int = 1):
        self.headers: dict[str, str] = {}
        self._checks = 0
        self._limit = disconnect_after

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks > self._limit


async def _drain_stream(*, disconnect_after: int = 1) -> str:
    """Run the pairing stream to its disconnect-driven end, with the headers."""
    import istota.web_app as mod

    resp = await mod.admin_whatsapp_pairing_stream(
        _FakeRequest(disconnect_after=disconnect_after), {"username": ADMIN},
    )
    assert resp.media_type == "text/event-stream"
    assert resp.headers["cache-control"] == "no-cache"
    assert resp.headers["x-accel-buffering"] == "no"
    out = ""
    async for chunk in resp.body_iterator:
        out += chunk if isinstance(chunk, str) else chunk.decode()
    return out


def _stream_frame(buf: str) -> dict:
    assert "event: pairing\ndata: " in buf, buf
    return json.loads(buf.split("event: pairing\ndata: ", 1)[1].split("\n\n", 1)[0])


@pytest.fixture(autouse=True)
def _fast_stream_poll(monkeypatch):
    """One second per tick would make every stream test a second long."""
    import istota.web_app as mod

    monkeypatch.setattr(mod, "_PAIRING_STREAM_POLL_SECONDS", 0.01)


# ---------------------------------------------------------------------------
# Authorization
# ---------------------------------------------------------------------------


class TestTheAdminGate:
    async def test_a_non_admin_is_refused_every_route(self, tmp_path, publish_status):
        """Enumerated rather than sampled: six routes, one of which serves a
        credential, so 'the ones I remembered' is not a claim worth making."""
        config = _make_config(tmp_path)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="https://example.com"
        ) as client:
            cookies = await _login(client, OTHER)
            for path in ALL_GET_ROUTES:
                resp = await _get(client, path, cookies=cookies)
                assert resp.status_code == 403, path
            resp = await client.post(
                PAIRING, json={}, cookies=cookies,
                headers={"Origin": "https://example.com"},
            )
            assert resp.status_code == 403
            resp = await client.request(
                "DELETE", PAIRING, cookies=cookies,
                headers={"Origin": "https://example.com"},
            )
            assert resp.status_code == 403

    async def test_an_anonymous_caller_is_refused(self, tmp_path, publish_status):
        config = _make_config(tmp_path)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="https://example.com"
        ) as client:
            for path in ALL_GET_ROUTES:
                resp = await _get(client, path)
                assert resp.status_code in (401, 403), path

    async def test_a_blank_allowlist_means_no_web_admin(
        self, tmp_path, publish_status
    ):
        """Fails closed, unlike `Config.is_admin`'s permissive empty rule."""
        config = _make_config(tmp_path, admins=())
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="https://example.com"
        ) as client:
            cookies = await _login(client, ADMIN)
            resp = await _get(client, PAIRING, cookies=cookies)
            assert resp.status_code == 403


class TestTheOriginCheck:
    """Only the two mutating routes carry it, which is the existing rule for
    every admin write in this app."""

    async def test_a_start_with_no_origin_is_refused(self, env):
        _, client, cookies = env
        resp = await client.post(PAIRING, json={}, cookies=cookies)
        assert resp.status_code == 403

    async def test_a_cancel_with_no_origin_is_refused(self, env):
        _, client, cookies = env
        resp = await client.request("DELETE", PAIRING, cookies=cookies)
        assert resp.status_code == 403

    async def test_a_mismatched_origin_is_refused(self, env):
        _, client, cookies = env
        resp = await client.post(
            PAIRING, json={}, cookies=cookies,
            headers={"Origin": "https://evil.example"},
        )
        assert resp.status_code == 403


# ---------------------------------------------------------------------------
# The config gates
# ---------------------------------------------------------------------------


class TestTheConfigGates:
    @pytest.mark.parametrize("path", ALL_GET_ROUTES[1:])
    async def test_pairing_disabled_404s_every_pairing_route(
        self, tmp_path, publish_status, path
    ):
        config = _make_config(tmp_path, pairing_enabled=False)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="https://example.com"
        ) as client:
            cookies = await _login(client, ADMIN)
            assert (await _get(client, path, cookies=cookies)).status_code == 404
            resp = await client.post(
                PAIRING, json={}, cookies=cookies,
                headers={"Origin": "https://example.com"},
            )
            assert resp.status_code == 404
            resp = await client.request(
                "DELETE", PAIRING, cookies=cookies,
                headers={"Origin": "https://example.com"},
            )
            assert resp.status_code == 404

    async def test_the_index_survives_pairing_being_disabled(
        self, tmp_path, publish_status
    ):
        """The card has to be able to say re-pairing is switched off here. A
        404 on the index would blank the Connections pane on exactly the
        deployment whose operator made a deliberate choice."""
        config = _make_config(tmp_path, pairing_enabled=False)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="https://example.com"
        ) as client:
            cookies = await _login(client, ADMIN)
            resp = await _get(client, INDEX, cookies=cookies)
            assert resp.status_code == 200
            card = resp.json()["connections"][0]
            assert card["pairing_supported"] is True
            assert card["pairing_enabled"] is False

    async def test_the_cloud_adapter_is_a_409_not_a_404(
        self, tmp_path, publish_status
    ):
        """A fact about the state rather than about the URL: the adapter is
        configured through Meta's business setup and has no QR."""
        config = _make_config(tmp_path, provider="whatsapp_cloud")
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="https://example.com"
        ) as client:
            cookies = await _login(client, ADMIN)
            assert (await _get(client, PAIRING, cookies=cookies)).status_code == 409
            card = (await _get(client, INDEX, cookies=cookies)).json()
            assert card["connections"][0]["pairing_supported"] is False
            assert card["connections"][0]["pairing_enabled"] is False

    async def test_a_disabled_whatsapp_surface_is_a_503(
        self, tmp_path, publish_status
    ):
        config = _make_config(tmp_path, enabled=False)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="https://example.com"
        ) as client:
            cookies = await _login(client, ADMIN)
            assert (await _get(client, PAIRING, cookies=cookies)).status_code == 503


# ---------------------------------------------------------------------------
# Starting a window
# ---------------------------------------------------------------------------


class TestTheStart:
    async def test_a_start_with_no_bridge_in_this_process_writes_the_row(self, env):
        """`read_status()` answers `None` in the web unit on the split
        deployment, so the live-session gate cannot be applied there and
        `repair_session` is the authority. Refusing outright would 404 the
        shape this whole flow exists for."""
        config, client, cookies = env
        resp = await client.post(
            PAIRING, json={}, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["state"] == "requested"
        assert body["force"] is False
        row = _row(config)
        assert row["state"] == db.WHATSAPP_PAIRING_REQUESTED
        assert row["window_id"] == body["window_id"]
        assert row["requested_by"] == ADMIN
        assert row["force"] is False

    async def test_a_latched_permanent_fatal_needs_no_confirmation(
        self, env, publish_status
    ):
        _, client, cookies = env
        publish_status(_Status(fatal_is_permanent=True))
        resp = await client.post(
            PAIRING, json={}, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 200

    async def test_a_live_session_is_refused_without_force(self, env, publish_status):
        config, client, cookies = env
        publish_status(_Status(fatal_is_permanent=False))
        resp = await client.post(
            PAIRING, json={}, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 409
        assert "working" in resp.json()["detail"]
        # Nothing written: a refusal must not leave a row a poll would service.
        assert _row(config) is None

    async def test_force_alone_is_refused(self, env, publish_status):
        """Enumerated apart from the refusal above, because a single test
        passes while *either* flag alone is sufficient."""
        config, client, cookies = env
        publish_status(_Status(fatal_is_permanent=False))
        resp = await client.post(
            PAIRING, json={"force": True}, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 400
        assert "confirm_disconnect" in resp.json()["detail"]
        assert _row(config) is None

    async def test_force_alone_is_refused_with_no_bridge_visible_either(
        self, env
    ):
        """The confirmation pair is checked before the session-live gate, so it
        holds on the shape where the link cannot be read at all — which is the
        production web unit."""
        config, client, cookies = env
        resp = await client.post(
            PAIRING, json={"force": True}, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 400
        assert _row(config) is None

    async def test_confirm_disconnect_alone_does_not_force(self, env, publish_status):
        """`confirm_disconnect` is a confirmation *of* `force`, never a second
        way to grant it, so on its own it leaves the unforced refusal in
        place."""
        config, client, cookies = env
        publish_status(_Status(fatal_is_permanent=False))
        resp = await client.post(
            PAIRING, json={"confirm_disconnect": True}, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 409
        assert _row(config) is None

    async def test_both_flags_reach_the_row(self, env, publish_status):
        """The column is the whole carrier of the operator's confirmation
        across the two processes: the poll is what calls the bridge, and it is
        not the surface they spoke to."""
        config, client, cookies = env
        publish_status(_Status(fatal_is_permanent=False))
        resp = await client.post(
            PAIRING,
            json={"force": True, "confirm_disconnect": True},
            cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["force"] is True
        assert _row(config)["force"] is True

    async def test_a_second_start_while_one_is_open_is_a_409(self, env):
        _, client, cookies = env
        first = await client.post(
            PAIRING, json={}, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert first.status_code == 200
        second = await client.post(
            PAIRING, json={}, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert second.status_code == 409
        assert "already in progress" in second.json()["detail"]

    async def test_no_route_refuses_on_deployment_shape(
        self, tmp_path, publish_status
    ):
        """`restart_interval_seconds = 0` is what compose renders. An earlier
        draft of the design gated the flow on that value and would have 404'd
        the shape the spec exists to reach."""
        config = _make_config(tmp_path, restart_interval_seconds=0)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="https://example.com"
        ) as client:
            cookies = await _login(client, ADMIN)
            resp = await client.post(
                PAIRING, json={}, cookies=cookies,
                headers={"Origin": "https://example.com"},
            )
            assert resp.status_code == 200

    async def test_the_configured_window_governs_the_request_deadline(
        self, tmp_path, publish_status
    ):
        """Otherwise `pairing_window_seconds` is an operator key that does
        nothing: `request_whatsapp_pairing` defaults to 300 and the route is
        the only caller that can pass the configured value."""
        config = _make_config(tmp_path, pairing_window_seconds=45)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="https://example.com"
        ) as client:
            cookies = await _login(client, ADMIN)
            before = time.time()
            resp = await client.post(
                PAIRING, json={}, cookies=cookies,
                headers={"Origin": "https://example.com"},
            )
            assert resp.status_code == 200
            state = (await _get(client, PAIRING, cookies=cookies)).json()["pairing"]
        deadline = state["expires_at_epoch"]
        assert deadline is not None
        # A window and a half of slack in each direction still separates 45
        # from the 300s default, which is the thing being asserted.
        assert before + 30 <= deadline <= before + 90


# ---------------------------------------------------------------------------
# The sandbox-bound relay refusal
# ---------------------------------------------------------------------------


class TestTheSandboxBoundRelayRefusal:
    """A window is refused where the resolved relay would sit inside a root
    some task's namespace binds. The relay holds a full-account credential for
    the length of the window, and `pairing_relay_path` is operator-settable, so
    this cannot be decided once at design time.
    """

    async def _post(self, tmp_path, publish_status, relay: str):
        config = _make_config(tmp_path, pairing_relay_path=relay)
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="https://example.com"
        ) as client:
            cookies = await _login(client, ADMIN)
            resp = await client.post(
                PAIRING, json={}, cookies=cookies,
                headers={"Origin": "https://example.com"},
            )
            index = (await _get(client, INDEX, cookies=cookies)).json()
        return config, resp, index["connections"][0]

    async def test_a_relay_inside_the_workspace_user_tree_is_refused(
        self, tmp_path, publish_status
    ):
        relay = str(tmp_path / "mount" / "Users" / ADMIN / "qr.json")
        config, resp, card = await self._post(tmp_path, publish_status, relay)
        assert resp.status_code == 409
        assert "sandbox" in resp.json()["detail"]
        assert _row(config) is None
        assert card["pairing_blocked_reason"] == "nextcloud_user_dir"

    async def test_a_relay_inside_a_per_user_temp_dir_is_refused(
        self, tmp_path, publish_status
    ):
        relay = str(tmp_path / "tmp" / ADMIN / "qr.json")
        config, resp, card = await self._post(tmp_path, publish_status, relay)
        assert resp.status_code == 409
        assert _row(config) is None
        assert card["pairing_blocked_reason"] == "user_temp_dir"

    async def test_a_direct_child_of_the_temp_root_is_allowed(
        self, tmp_path, publish_status
    ):
        """The control for the case above, and the one the standalone
        install's own fallback resolves to: `{temp_dir}/whatsapp-pairing.json`
        is a *sibling* of the per-user directories the sandbox binds, not
        inside one."""
        relay = str(tmp_path / "tmp" / "whatsapp-pairing.json")
        config, resp, card = await self._post(tmp_path, publish_status, relay)
        assert resp.status_code == 200
        assert _row(config) is not None
        assert card["pairing_blocked_reason"] is None

    async def test_the_default_state_root_is_allowed(self, env):
        """The shipped path on both server shapes, and the control that says
        the refusal is not refusing everything."""
        config, client, cookies = env
        card = (await _get(client, INDEX, cookies=cookies)).json()["connections"][0]
        assert card["pairing_blocked_reason"] is None
        assert baileys_bridge.default_pairing_relay_path(config).parent == (
            Path(config.db_path).parent
        )

    @pytest.mark.parametrize("developer_enabled", [True, False])
    def test_every_bind_the_planner_emits_is_covered_by_the_predicate(
        self, tmp_path, developer_enabled
    ):
        """Asserted against `build_mount_plan`'s own emitted mounts, not
        against a list kept here.

        A copy of the bind list beside a consumer is how the two come to
        disagree about what is in a namespace, and for a credential relay that
        means a file believed to be outside one being bound read-write into
        every task's. So the walk drives the real planner and requires **every**
        `ro`/`rw` mount it emits to be refused by `sandbox_bound_reason`.

        **It used to filter to deployment-owned sources, and that filter was
        the hole rather than the scope.** `/usr`, the merged-usr compat links,
        the ten `/etc` entries and `python_base` are emitted unconditionally
        for every profile and were skipped structurally — not by an allowlist a
        reader would notice, which is the "success indistinguishable from a
        no-op" class `.claude/rules/testbed.md` catalogues. A socket bind is
        excluded by being a file rather than by name.

        **Both `developer.enabled` values, because that is the axis that
        selects `resolve_sandbox_cache_dir`'s two branches.** With it on, an
        admin's cache is derived *inside* `{repos_dir}/{user_id}`, which the
        repos root already covers; with it off — and on every deployment
        without the developer skill — the cache is
        `{security.sandbox_cache_dir}/{user_id}`, bound **rw** and covered by
        nothing else. Parametrizing on the plan's `is_admin` argument instead
        looks equivalent and is not: `sandbox_cache_is_derived` reads the
        config's own admin list rather than that argument, so both values take
        the derived branch and the read-write bind is never emitted — measured,
        and it is why the control that drops the `package_cache` root used to
        pass.

        The fixture also sets `sandbox_cache_dir` itself, since leaving it
        empty makes `resolve_sandbox_cache_dir` answer `None`, no
        `package_cache` mount enters the walk at all, and the guard is vacuous
        exactly where it is needed.
        """
        from istota import sandbox_plan
        from istota.config import DeveloperConfig, SecurityConfig
        from istota.sandbox_plan import SandboxProfile

        config = _make_config(tmp_path)
        config.security = SecurityConfig(
            sandbox_cache_dir=str(tmp_path / "caches"),
            sandbox_ro_paths=[str(tmp_path / "srv")],
        )
        config.developer = DeveloperConfig(
            enabled=developer_enabled, repos_dir=str(tmp_path / "repos"),
        )
        config.workspace_path.mkdir(parents=True, exist_ok=True)
        (config.workspace_path / "Users" / ADMIN).mkdir(parents=True, exist_ok=True)
        (config.workspace_path / "Talk").mkdir(parents=True, exist_ok=True)
        (tmp_path / "srv").mkdir(parents=True, exist_ok=True)
        # The configured cache branch refuses a root that is not an existing
        # writable directory, so without this the rw bind is never emitted and
        # the walk is vacuous for exactly the mount it most needs to see.
        (tmp_path / "caches").mkdir(parents=True, exist_ok=True)
        (tmp_path / "repos" / ADMIN).mkdir(parents=True, exist_ok=True)
        user_temp = Path(config.temp_dir) / ADMIN
        user_temp.mkdir(parents=True, exist_ok=True)

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, "hi", ADMIN, source_type="cli")
            task = db.get_task(conn, task_id)

        plan = sandbox_plan.build_mount_plan(
            config, task, True, [], user_temp, profile=SandboxProfile.NATIVE,
        )
        checked = 0
        reasons = set()
        for mount in plan.mounts:
            if mount.mode not in ("ro", "rw"):
                continue
            source = Path(mount.source)
            if source.is_file():
                # A bound socket or a single bound file is not a directory a
                # relay could sit inside.
                continue
            checked += 1
            reasons.add(mount.reason)
            probe = source / "whatsapp-pairing.json"
            assert sandbox_plan.sandbox_bound_reason(config, probe) is not None, (
                f"{mount.reason} binds {source}, and a relay written there "
                "would be readable inside a task's sandbox"
            )
        assert checked >= 10, (
            "the walk examined almost nothing, so it cannot have established "
            "anything about the predicate"
        )
        # Named rather than counted, so a planner change that stops emitting
        # the read-write cache bind is a visible failure rather than a quieter
        # walk. This is the bind the earlier filter hid.
        assert "package_cache" in reasons
        assert {"usr", "etc"} <= reasons


# ---------------------------------------------------------------------------
# The payload never crosses as text
# ---------------------------------------------------------------------------


class TestThePayloadReachesNoJsonBody:
    async def test_the_state_stream_and_index_all_withhold_it(self, env):
        """Asserted over the three together, since this is the property the
        whole design rests on and a per-endpoint test passes while a sibling
        leaks."""
        config, client, cookies = env
        window = _write_request(config)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_AWAITING_SCAN,
            )
        _write_relay(config, window)

        state = await _get(client, PAIRING, cookies=cookies)
        index = await _get(client, INDEX, cookies=cookies)
        stream = await _drain_stream()

        for label, text in (
            ("state", state.text),
            ("index", index.text),
            ("stream", stream),
        ):
            assert QR not in text, f"the {label} endpoint carried the payload"
            assert '"qr"' not in text, f"the {label} endpoint carried a qr field"

        # The counter is what a client reads instead, so the absence above is
        # not merely an empty response.
        assert state.json()["pairing"]["qr_seq"] == 3
        assert state.json()["pairing"]["qr_available"] is True
        assert _stream_frame(stream)["qr_seq"] == 3

    async def test_a_stale_window_is_not_rendered(self, env):
        """The reader validates the relay's `window_id` against the row, so a
        file left by an earlier window is ignored rather than drawn."""
        config, client, cookies = env
        window = _write_request(config)
        with db.get_db(config.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_AWAITING_SCAN,
            )
        _write_relay(config, "some-other-window")

        state = (await _get(client, PAIRING, cookies=cookies)).json()["pairing"]
        assert state["qr_available"] is False
        assert state["qr_seq"] == 0
        assert (await _get(client, f"{PAIRING}/qr.svg", cookies=cookies)).status_code == (
            404
        )

    async def test_a_terminal_row_vetoes_a_still_published_relay(self, env):
        """Stage 3's review routed this here as defence in depth behind the
        poll's cancel. The relay carries the *window's* deadline, which is
        later than the request row's, so a window republishing after its row
        closed passes `read_relay`'s own deadline check — only the row can
        settle it, and the poll's cancel is a tick away.
        """
        config, client, cookies = env
        window = _write_request(config)
        _write_relay(config, window)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_EXPIRED, "the window expired",
            )

        state = (await _get(client, PAIRING, cookies=cookies)).json()["pairing"]
        assert state["terminal"] is True
        assert state["qr_available"] is False
        assert (await _get(client, f"{PAIRING}/qr.svg", cookies=cookies)).status_code == (
            404
        )


# ---------------------------------------------------------------------------
# The SVG
# ---------------------------------------------------------------------------


class TestTheSvgRenderer:
    async def test_it_serves_an_svg_that_may_not_be_stored(self, env):
        config, client, cookies = env
        window = _write_request(config)
        with db.get_db(config.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_AWAITING_SCAN,
            )
        _write_relay(config, window)

        resp = await _get(client, f"{PAIRING}/qr.svg?seq=3", cookies=cookies)
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("image/svg+xml")
        assert resp.headers["cache-control"] == "no-store"
        assert resp.headers["x-pairing-qr-seq"] == "3"
        assert resp.text.startswith("<svg")
        # A drawing, not the payload: the credential is encoded as geometry and
        # the string itself appears nowhere in the body.
        assert QR not in resp.text

    async def test_a_seq_one_rotation_behind_still_draws(self, env):
        """A mismatch is not a refusal: the client can legitimately be one
        rotation behind, and a 409 there would blank the code for a person
        mid-scan. The code actually drawn is named in the header."""
        config, client, cookies = env
        window = _write_request(config)
        with db.get_db(config.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_AWAITING_SCAN,
            )
        _write_relay(config, window, qr_seq=7)

        resp = await _get(client, f"{PAIRING}/qr.svg?seq=2", cookies=cookies)
        assert resp.status_code == 200
        assert resp.headers["x-pairing-qr-seq"] == "7"

    async def test_no_request_row_is_a_404(self, env):
        _, client, cookies = env
        resp = await _get(client, f"{PAIRING}/qr.svg", cookies=cookies)
        assert resp.status_code == 404

    async def test_a_window_not_yet_at_awaiting_scan_is_a_404(self, env):
        config, client, cookies = env
        window = _write_request(config)
        _write_relay(
            config, window,
            state=pairing_relay.STATE_AWAITING_SIDECAR, qr=None, qr_seq=0,
        )
        resp = await _get(client, f"{PAIRING}/qr.svg", cookies=cookies)
        assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The stream
# ---------------------------------------------------------------------------


class TestTheStream:
    async def test_it_carries_the_four_small_fields(self, env):
        config, client, cookies = env
        window = _write_request(config)
        with db.get_db(config.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_AWAITING_SCAN,
            )
        _write_relay(config, window)

        frame = _stream_frame(await _drain_stream())
        assert set(frame) == {
            "state", "qr_seq", "qr_available", "expires_at", "message",
        }
        assert frame["state"] == pairing_relay.STATE_AWAITING_SCAN

    async def test_it_streams_with_no_pairing_in_progress(self, env):
        """**The same five keys as the populated frame**, which is the half a
        state-and-counter assertion missed: `qr_available` exists so a client
        can tell "no code yet" from "a code the fetch will miss", and the idle
        frame is the one where that applies — a client keyed on it read
        `undefined` on the first frame of every idle stream.
        """
        _, client, cookies = env
        frame = _stream_frame(await _drain_stream())

        assert set(frame) == {
            "state", "qr_seq", "qr_available", "expires_at", "message",
        }
        assert frame["state"] is None
        assert frame["qr_seq"] == 0
        assert frame["qr_available"] is False

    def test_its_sleep_goes_through_web_shutdown(self):
        """A bare `asyncio.sleep` in a polling SSE generator means every
        Ctrl-C and every deploy restart logs an ASGI traceback and burns the
        whole graceful-shutdown window, because nothing server-side ever ends
        such a stream. Read off the source rather than driven: the property is
        which sleep is called, and a driven stream that merely completes
        proves nothing about that.
        """
        import istota.web_app as mod

        body = source_of(mod.admin_whatsapp_pairing_stream)
        assert "web_shutdown.sleep_unless_shutdown" in body
        # The docstring names the defect, so the assertion has to be about a
        # call rather than about the string appearing anywhere in the source.
        assert "await asyncio.sleep(" not in body
        assert "web_shutdown.is_shutting_down()" in body


# ---------------------------------------------------------------------------
# Cancelling
# ---------------------------------------------------------------------------


class TestTheCancel:
    async def test_it_stamps_the_row_terminal(self, env):
        """It stamps the row and touches neither the relay nor the window: the
        web process holds no bridge on the split deployment, and
        `clear_relay` states the bridge's lock as its precondition. The poll's
        cancel arm is what drops the window."""
        config, client, cookies = env
        window = _write_request(config)
        resp = await client.request(
            "DELETE", PAIRING, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json()["cancelled"] is True
        row = _row(config)
        assert row["state"] == db.WHATSAPP_PAIRING_FAILED
        assert row["window_id"] == window
        assert "cancelled" in row["message"]

    async def test_it_preserves_the_archive_clause(self, env):
        """The row's message is what tells an operator which of two session
        directories holds their credential, so the cancel appends rather than
        replacing it."""
        config, client, cookies = env
        window = _write_request(config)
        archive = "the previous session was moved aside to /srv/x.old-20260101"
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_AWAITING_SIDECAR, archive,
            )
        await client.request(
            "DELETE", PAIRING, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert archive in _row(config)["message"]

    async def test_an_already_closed_row_is_not_cancelled_again(self, env):
        config, client, cookies = env
        window = _write_request(config)
        with db.get_db(config.db_path) as conn:
            db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_PAIRED, "paired",
            )
        resp = await client.request(
            "DELETE", PAIRING, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.json() == {
            "cancelled": False, "reason": "already_closed", "window_id": window,
        }
        assert _row(config)["state"] == db.WHATSAPP_PAIRING_PAIRED

    async def test_no_request_at_all_is_not_an_error(self, env):
        _, client, cookies = env
        resp = await client.request(
            "DELETE", PAIRING, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 200
        assert resp.json() == {"cancelled": False, "reason": "no_pairing"}

    async def test_an_id_less_row_is_cleared_rather_than_stranded(self, env):
        """Every guarded write here refuses an empty `window_id`, so without
        the unguarded clear such a row would block every later request for the
        life of the deployment — the stuck-row class this channel exists to
        avoid."""
        config, client, cookies = env
        _write_request(config)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE whatsapp_runtime SET pairing_window_id = NULL "
                "WHERE singleton = 1"
            )
        resp = await client.request(
            "DELETE", PAIRING, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.json()["cancelled"] is True
        assert _row(config) is None
        # And a fresh request is accepted again, which is the point.
        resp = await client.post(
            PAIRING, json={}, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# The index
# ---------------------------------------------------------------------------


class TestTheConnectionsIndex:
    async def test_it_reports_the_link_only_where_a_bridge_is_visible(
        self, env, publish_status
    ):
        """`link: null` means "this process cannot see the bridge", never "the
        bridge is down" — which on the canonical Ansible deployment is the
        ordinary state of the web unit."""
        _, client, cookies = env
        card = (await _get(client, INDEX, cookies=cookies)).json()["connections"][0]
        assert card["link"] is None

        publish_status(_Status(fatal_is_permanent=True))
        card = (await _get(client, INDEX, cookies=cookies)).json()["connections"][0]
        assert card["link"]["fatal_is_permanent"] is True
        assert card["link"]["fatal_reason"] == "logged_out"
        assert card["link"]["connection_replaced_latched"] is False

    async def test_it_carries_a_latched_replaced_connection(
        self, env, publish_status
    ):
        """ISSUE-553. Without the field the card rendered a sidecar that had
        yielded to another client as one that was reconnecting."""
        _, client, cookies = env
        status = _Status(fatal_is_permanent=False)
        status.payload.update(
            ready=False, fatal_reason="connection_replaced",
            connection_replaced_latched=True,
        )
        publish_status(status)

        card = (await _get(client, INDEX, cookies=cookies)).json()["connections"][0]

        assert card["link"]["connection_replaced_latched"] is True

    async def test_it_carries_the_durable_row_either_way(self, env):
        config, client, cookies = env
        _write_request(config)
        card = (await _get(client, INDEX, cookies=cookies)).json()["connections"][0]
        assert card["pairing"]["state"] == db.WHATSAPP_PAIRING_REQUESTED
        assert card["pairing"]["requested_by"] == ADMIN


# ---------------------------------------------------------------------------
# The config keys themselves
# ---------------------------------------------------------------------------


class TestTheNewKeysAreNotCredentialFields:
    def test_the_baileys_block_declares_no_credential_field(self):
        """Two of the four new keys carry *truthy* defaults, which is how the
        rule in `WhatsAppBaileysConfig`'s docstring could break quietly: an
        always-populated entry in `_WHATSAPP_PROVIDER_FIELDS` would make this
        adapter read as configured on every deployment and be built as a
        callback-only adapter on a Cloud one. The map is an explicit field
        list rather than a truthiness scan, so adding a field changes nothing
        — this is what says so.
        """
        from istota.config import _WHATSAPP_PROVIDER_FIELDS

        assert _WHATSAPP_PROVIDER_FIELDS["baileys"] == ()

    def test_an_enabled_baileys_deployment_reports_no_missing_credential(
        self, tmp_path
    ):
        from istota.config import whatsapp_credential_errors

        config = _make_config(tmp_path)
        assert whatsapp_credential_errors(config) == []

    @pytest.mark.parametrize(
        "value,expected",
        [(45, 45.0), (0, 300.0), (-1, 300.0), ("nonsense", 300.0), (None, 300.0)],
    )
    def test_a_nonsense_window_falls_back_rather_than_failing_the_load(
        self, tmp_path, value, expected
    ):
        """`load_config` runs in the scheduler, the web app, the webhook
        receiver and every host-side skill CLI spawn, so a typo on a knob that
        bounds one admin operation must not stop any of them from starting.
        """
        config = _make_config(tmp_path, pairing_window_seconds=value)
        assert baileys_bridge.configured_pairing_window_seconds(config) == expected


# ---------------------------------------------------------------------------
# What a malformed relay does to a route
# ---------------------------------------------------------------------------


class TestAMalformedRelayIsNotA500:
    """`read_relay` type-checks `window_id`, `state` and `expires_at` and
    passes `qr_seq` through exactly as the file held it — and it coerces
    `expires_at` for its own deadline test without writing the coerced value
    back. Every reader here is a route with no exception wrapper, so an
    unparseable number would be a 500 on three endpoints rather than the "a
    read that fails to parse returns nothing" the design asks for.
    """

    def _plant(self, config, window_id: str, payload: dict) -> None:
        path = baileys_bridge.default_pairing_relay_path(config)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload))

    async def _arm(self, config):
        window = _write_request(config)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_AWAITING_SCAN,
            )
        return window

    async def test_a_non_numeric_qr_seq_does_not_500_any_route(self, env):
        config, client, cookies = env
        window = await self._arm(config)
        self._plant(config, window, {
            "window_id": window,
            "state": pairing_relay.STATE_AWAITING_SCAN,
            "expires_at": time.time() + 300.0,
            "qr": QR,
            "qr_seq": "not-a-number",
            "message": "",
            "updated_at": time.time(),
        })

        state = await _get(client, PAIRING, cookies=cookies)
        index = await _get(client, INDEX, cookies=cookies)
        svg = await _get(client, f"{PAIRING}/qr.svg", cookies=cookies)

        assert state.status_code == 200
        assert index.status_code == 200
        assert svg.status_code == 200
        assert state.json()["pairing"]["qr_seq"] == 0
        assert svg.headers["x-pairing-qr-seq"] == "0"
        assert QR not in state.text and QR not in index.text

    async def test_a_string_expires_at_is_published_as_a_number(self, env):
        """`read_relay` never writes its own coercion back, so the raw value
        would reach a browser drawing a countdown off a field the relay format
        declares a number."""
        config, client, cookies = env
        window = await self._arm(config)
        self._plant(config, window, {
            "window_id": window,
            "state": pairing_relay.STATE_AWAITING_SCAN,
            "expires_at": str(time.time() + 300.0),
            "qr": QR,
            "qr_seq": 2,
            "message": "",
            "updated_at": time.time(),
        })

        state = (await _get(client, PAIRING, cookies=cookies)).json()["pairing"]

        assert isinstance(state["expires_at_epoch"], float)

    async def test_a_nonsense_expires_at_falls_back_to_the_rows_own(self, env):
        config, client, cookies = env
        window = await self._arm(config)
        self._plant(config, window, {
            "window_id": window,
            "state": pairing_relay.STATE_AWAITING_SIDECAR,
            "expires_at": time.time() + 300.0,
            "qr_seq": "x",
            "message": "",
            "updated_at": time.time(),
        })

        state = (await _get(client, PAIRING, cookies=cookies)).json()["pairing"]

        assert state["expires_at_epoch"] is not None
        assert isinstance(state["expires_at_epoch"], float)

    @pytest.mark.parametrize("seq", ["", "abc", "null", "-1"])
    async def test_any_seq_query_value_still_draws(self, env, seq):
        """`seq` is a cache-buster read by nothing. Typed `int`, FastAPI
        answers these 422 — the refusal the route's own docstring says must
        never happen, reached by a different status code."""
        config, client, cookies = env
        window = await self._arm(config)
        _write_relay(config, window, qr_seq=4)

        resp = await _get(client, f"{PAIRING}/qr.svg?seq={seq}", cookies=cookies)

        assert resp.status_code == 200
        assert resp.headers["x-pairing-qr-seq"] == "4"


# ---------------------------------------------------------------------------
# A cancel that races the poll's claim
# ---------------------------------------------------------------------------


class TestTheCancelAgainstAClaimedRow:
    async def test_a_servicing_row_is_refused_rather_than_stamped(self, env):
        """The re-pair is already running and takes no instruction from the
        row, so stamping it could not stop it — and the stamp does active harm:
        the guarded outcome write that follows is then refused by the terminal
        clause, so the timestamped archive path never reaches the row and an
        operator is left with two session directories and nothing saying which
        holds theirs.
        """
        config, client, cookies = env
        window = _write_request(config)
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_SERVICING,
            )

        resp = await client.request(
            "DELETE", PAIRING, cookies=cookies,
            headers={"Origin": "https://example.com"},
        )

        assert resp.status_code == 200
        assert resp.json() == {
            "cancelled": False, "reason": "servicing", "window_id": window,
        }
        # Still writable, which is the point: the outcome can still land.
        row = _row(config)
        assert row["state"] == db.WHATSAPP_PAIRING_SERVICING
        with db.get_db(config.db_path) as conn:
            assert db.record_whatsapp_pairing_state(
                conn, window, db.WHATSAPP_PAIRING_AWAITING_SIDECAR,
                "moved aside to /srv/x.old-20260101",
            )

    async def test_the_read_and_the_write_are_one_write_transaction(self):
        """`BEGIN IMMEDIATE`, matching the poll's own claim. Under a deferred
        `BEGIN` the poll can commit `servicing` between this function's read
        and its write, and since both hold the same `window_id` on a row that
        is still non-terminal the cancel applies and the route reports success
        about a re-pair that is already running.

        Read off the source: the property is which transaction is opened, and a
        driven cancel that merely succeeds proves nothing about it. The
        behavioural half is the test above, which is what the refusal buys.
        """
        import istota.web_app as mod

        body = source_of(mod._cancel_pairing_request)

        assert 'conn.execute("BEGIN IMMEDIATE")' in body
        assert "WHATSAPP_PAIRING_SERVICING" in body


# ---------------------------------------------------------------------------
# A relay path the two processes would resolve differently
# ---------------------------------------------------------------------------


class TestARelativeRelayPathIsRefused:
    async def test_a_relative_path_is_refused_with_its_own_reason(
        self, tmp_path, publish_status
    ):
        """A relative path is resolved against each process's own cwd, so the
        verdict reached in the web process is about a different directory from
        the one the scheduler writes into. An answer about the wrong directory
        is worse than no answer."""
        config = _make_config(tmp_path, pairing_relay_path="relay/qr.json")
        app = _patch_app(config)
        transport = ASGITransport(app=app)
        async with AsyncClient(
            transport=transport, base_url="https://example.com"
        ) as client:
            cookies = await _login(client, ADMIN)
            resp = await client.post(
                PAIRING, json={}, cookies=cookies,
                headers={"Origin": "https://example.com"},
            )
            card = (await _get(client, INDEX, cookies=cookies)).json()

        assert resp.status_code == 409
        assert _row(config) is None
        assert card["connections"][0]["pairing_blocked_reason"] == "relative_path"
