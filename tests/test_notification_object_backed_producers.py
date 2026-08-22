"""The two producers that raise on their own connection, and why that is safe.

The cron auto-disable sites are in `test_notification_transactions.py` with the
other buffered writers. These two are the exceptions the store's
`raise_notification` docstring demands be justified:

- `sync_garmin` reaches the framework DB only through `secrets_store`, whose
  helpers each open and close a connection around a single statement. There is
  no open write lock for a second connection to wait thirty seconds on.
- the health panel upload handler's only open connection is to the *health
  module* DB — a different file, a different lock.

Both are asserted here rather than argued: each test opens the framework DB
independently while the producer runs and takes `BEGIN IMMEDIATE`, which can
only succeed if the caller is holding nothing.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from istota import db, notification_sources as sources, notification_store as store
from istota.config import Config, UserConfig
from istota.health import garmin as gm
from istota.health import garmin_sync
from istota.health._migrate import ensure_initialised
from istota.health.workspace import synthesize_health_context
from istota.notification_resolvers import connected_service as service_source

try:
    import fastapi  # noqa: F401
    from fastapi.testclient import TestClient  # noqa: F401
    _has_web_deps = True
except ImportError:  # pragma: no cover - exercised only on a lean install
    _has_web_deps = False


@pytest.fixture(autouse=True)
def _registry():
    sources.reset_registry()
    yield
    sources.reset_registry()


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    monkeypatch.setenv(
        "ISTOTA_SECRET_KEY", "test-key-test-key-test-key-test-key-test-key",
    )


@pytest.fixture
def config(tmp_path):
    cfg = Config(
        db_path=tmp_path / "istota.db",
        nextcloud_mount_path=tmp_path / "mount",
        users={"alice": UserConfig(display_name="Alice")},
    )
    db.init_db(cfg.db_path)
    return cfg


def _rows(config, source):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT * FROM notifications WHERE source = ?", (source,),
        ).fetchall()


def _sends(monkeypatch, *, delivered=True):
    calls: list[tuple] = []

    def _send(cfg, user_id, text, **kwargs):
        calls.append((user_id, text, kwargs.get("purpose")))
        return delivered

    monkeypatch.setattr("istota.notifications.send_notification", _send)
    return calls


class _LockProbe:
    """Records whether the framework DB was write-lockable during the call."""

    def __init__(self, config):
        self.config = config
        self.free: list[bool] = []

    def check(self):
        try:
            with db.get_db(self.config.db_path, busy_timeout_ms=200) as probe:
                probe.execute("BEGIN IMMEDIATE")
                probe.rollback()
            self.free.append(True)
        except Exception:
            self.free.append(False)


# ---------------------------------------------------------------------------
# Garmin token expiry
# ---------------------------------------------------------------------------


class _AuthFailAdapter:
    """Rehydrates fine and then fails auth on the first endpoint of the day."""

    def load_tokens(self, tokens):
        return None

    def serialize_tokens(self):
        return {"oauth1_token": "abc"}

    def __getattr__(self, name):
        def _boom(*_a, **_k):
            raise gm.GarminAuthError("token expired")
        return _boom


def _ctx(config, user_id="alice"):
    ctx = synthesize_health_context(
        user_id, Path(config.nextcloud_mount_path) / "workspace",
    )
    ensure_initialised(ctx)
    return ctx


class TestGarminTokenExpiry:
    def test_a_missing_token_blob_raises_a_reconnect_row(self, config, monkeypatch):
        """The first of the two sites: `acquire_client` finds nothing to load."""
        ctx = _ctx(config)
        gm.set_adapter_factory(_AuthFailAdapter)
        sent = _sends(monkeypatch)

        res = garmin_sync.sync_garmin(
            ctx, config.db_path, days_back=1, today=date(2026, 5, 15),
            config=config,
        )

        assert res.auth_error is True
        rows = _rows(config, "connected_service")
        assert len(rows) == 1
        assert rows[0]["user_id"] == "alice"
        assert rows[0]["dedup_key"] == "service:garmin"
        assert rows[0]["object_id"] == "garmin"
        assert rows[0]["state"] == "open"
        assert rows[0]["last_delivered_at"] is not None
        assert len(sent) == 1 and sent[0][0] == "alice"

    def test_a_mid_sync_auth_failure_raises_the_same_row(self, config, monkeypatch):
        """The second site: the tokens loaded and then stopped working."""
        ctx = _ctx(config)
        gm.store_tokens(config.db_path, "alice", {"oauth1_token": "abc"})
        _sends(monkeypatch)

        res = garmin_sync.sync_garmin(
            ctx, config.db_path, days_back=1, today=date(2026, 5, 15),
            adapter=_AuthFailAdapter(), config=config,
        )

        assert res.auth_error is True
        rows = _rows(config, "connected_service")
        assert len(rows) == 1 and rows[0]["state"] == "open"

    def test_the_raise_holds_no_write_lock_on_the_framework_db(
        self, config, monkeypatch,
    ):
        """`raise_notification` is only safe for a caller that holds no lock.

        Probed at the moment the store is *entered*, with only `sync_garmin` and
        its callers on the stack — so a `False` here means the producer is
        holding the write lock the store is about to want, which in production
        is a silent thirty-second stall that the never-raises contract then
        swallows. Not probed inside `write_notification`: by then the store's own
        connection has the lock, and the answer would be `False` for the right
        reason and prove nothing about the caller.
        """
        ctx = _ctx(config)
        gm.set_adapter_factory(_AuthFailAdapter)
        probe = _LockProbe(config)
        real_raise = store.raise_notification

        def _spy(cfg, user_id, **kwargs):
            probe.check()
            return real_raise(cfg, user_id, **kwargs)

        monkeypatch.setattr("istota.notification_store.raise_notification", _spy)
        _sends(monkeypatch)

        garmin_sync.sync_garmin(
            ctx, config.db_path, days_back=1, today=date(2026, 5, 15),
            config=config,
        )

        assert probe.free == [True], (
            "sync_garmin was holding the framework write lock at the raise"
        )
        assert len(_rows(config, "connected_service")) == 1

    def test_the_skill_cli_leg_writes_nothing_and_delivers_nothing(
        self, config, monkeypatch,
    ):
        """No `config`, no row — the same rule the email skill CLI follows.

        `sync_garmin` runs in a short-lived host-side process there, and
        `send_notification`'s Talk and ntfy fan-out does not belong in it.
        """
        ctx = _ctx(config)
        gm.set_adapter_factory(_AuthFailAdapter)
        sent = _sends(monkeypatch)

        res = garmin_sync.sync_garmin(
            ctx, config.db_path, days_back=1, today=date(2026, 5, 15),
        )

        assert res.auth_error is True
        assert _rows(config, "connected_service") == []
        assert sent == []

    def test_the_token_error_flag_is_still_set(self, config, monkeypatch):
        """The notification is additive; the existing signal must survive."""
        ctx = _ctx(config)
        gm.store_tokens(config.db_path, "alice", {"oauth1_token": "abc"})
        _sends(monkeypatch)

        garmin_sync.sync_garmin(
            ctx, config.db_path, days_back=1, today=date(2026, 5, 15),
            adapter=_AuthFailAdapter(), config=config,
        )

        status = gm.get_status(config.db_path, "alice")
        assert status["connected"] is False
        assert status["error"] == "token_expired"

    def test_a_failed_raise_never_fails_the_sync(self, config, monkeypatch):
        """A sync must not break because the inbox did."""
        ctx = _ctx(config)
        gm.set_adapter_factory(_AuthFailAdapter)
        monkeypatch.setattr(
            service_source, "raise_for_service",
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("inbox down")),
        )

        res = garmin_sync.sync_garmin(
            ctx, config.db_path, days_back=1, today=date(2026, 5, 15),
            config=config,
        )
        assert res.auth_error is True
        assert gm.get_status(config.db_path, "alice")["error"] == "token_expired"


# ---------------------------------------------------------------------------
# the draft health panel
# ---------------------------------------------------------------------------


@pytest.fixture
def health_client(config):
    """The real router, mounted with an `istota_config` on `app.state`.

    `tests/test_health_routes.py` mounts it *without* one, which is why every
    case there still passes unchanged: with no config the handler cannot resolve
    a framework DB and writes nothing.
    """
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from istota.health._loader import resolve_for_user
    from istota.health.routes import get_user_context, require_auth, router

    ctx = resolve_for_user("alice", config)
    ensure_initialised(ctx)

    app = FastAPI()
    app.state.istota_config = config
    app.include_router(router, prefix="/istota/api/health")
    app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
    app.dependency_overrides[get_user_context] = lambda: ctx
    return TestClient(app), ctx


def _upload(client) -> int:
    resp = client.post(
        "/istota/api/health/panels/upload",
        files={"file": ("report.pdf", b"%PDF-1.4 fake", "application/pdf")},
        data={"drawn_at": "2026-05-08", "lab_name": "Quest"},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["id"]


@pytest.mark.skipif(not _has_web_deps, reason="web dependencies not installed")
class TestDraftHealthPanel:
    def test_an_upload_raises_a_row_in_the_framework_db(
        self, config, health_client, monkeypatch,
    ):
        client, ctx = health_client
        sent = _sends(monkeypatch)

        panel_id = _upload(client)

        rows = _rows(config, "health_panel")
        assert len(rows) == 1
        assert rows[0]["user_id"] == "alice"
        assert rows[0]["dedup_key"] == f"panel:{panel_id}"
        assert rows[0]["object_id"] == str(panel_id)
        assert rows[0]["state"] == "open"
        assert "Quest" in rows[0]["title"]
        assert len(sent) == 1

        # And nowhere near the module DB, which is the trap the whole source
        # exists to avoid: panel ids are per-user, so a row there would be
        # invisible to a store scoped by framework `user_id`.
        assert ctx.db_path != config.db_path
        from istota.health import db as health_db
        with health_db.connect(ctx.db_path) as c:
            tables = {
                r[0] for r in c.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'",
                ).fetchall()
            }
        assert "notifications" not in tables

    def test_confirming_the_biomarkers_closes_the_row(self, config, health_client):
        client, _ctx = health_client
        panel_id = _upload(client)

        resp = client.post(
            f"/istota/api/health/panels/{panel_id}/biomarkers",
            json={
                "biomarkers": [
                    {"name": "Hgb", "value": 14.5, "unit": "g/dL"},
                ],
                "confirm": True,
            },
        )
        assert resp.status_code == 200, resp.text

        rows = _rows(config, "health_panel")
        assert len(rows) == 1
        assert rows[0]["state"] == "resolved"
        assert rows[0]["resolved_by"] == "web"

    def test_saving_without_confirming_leaves_the_row_open(
        self, config, health_client,
    ):
        """A save mid-review is not a confirmation, and the panel is still draft."""
        client, _ctx = health_client
        panel_id = _upload(client)

        resp = client.post(
            f"/istota/api/health/panels/{panel_id}/biomarkers",
            json={"biomarkers": [{"name": "Hgb", "value": 14.5, "unit": "g/dL"}]},
        )
        assert resp.status_code == 200, resp.text
        assert _rows(config, "health_panel")[0]["state"] == "open"

    def test_a_row_left_open_over_a_confirmed_panel_goes_stale(
        self, config, health_client,
    ):
        """The backstop, for the confirm paths this stage did not wire."""
        client, ctx = health_client
        panel_id = _upload(client)

        from istota.health import db as health_db
        with health_db.connect(ctx.db_path) as c:
            health_db.update_panel(c, panel_id, draft=False)
            c.commit()

        with db.get_db(config.db_path) as conn:
            items, total = store.list_open(config, conn, "alice")
        assert items == [] and total == 0
        assert _rows(config, "health_panel")[0]["state"] == "stale"

    def test_the_open_row_renders_a_review_link(self, config, health_client):
        client, _ctx = health_client
        _upload(client)

        with db.get_db(config.db_path) as conn:
            items, total = store.list_open(config, conn, "alice")
        assert total == 1 and len(items) == 1
        (action,) = items[0].actions
        assert (action.id, action.method, action.href) == (
            "review", "LINK", "/health/bloodwork",
        )
        assert sources.is_safe_path(action.href)
