"""Tests for the Garmin auth/token plumbing in the health module.

The real ``garminconnect`` SDK is never imported here. A
:class:`_FakeAdapter` provides controllable login / MFA / token outputs;
:func:`set_adapter_factory` swaps it in for the duration of each test.

Test classes guard isolation with ``setup_method`` so xdist-parallel runs
don't share the module-global pending-auth cache.
"""

from __future__ import annotations

from typing import Any

import pytest

from istota.health import db as health_db
from istota.health import garmin as gm
from istota.health._migrate import ensure_initialised
from istota.health.workspace import synthesize_health_context


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _ctx(tmp_path, user_id: str = "alice"):
    c = synthesize_health_context(user_id, tmp_path / "workspace")
    c.ensure_dirs()
    ensure_initialised(c)
    return c


class _FakeAdapter:
    """Minimal in-memory adapter for tests.

    Constructor lets each test prescribe whether the login should:
    succeed, require MFA, raise, etc.
    """

    def __init__(
        self,
        *,
        require_mfa: bool = False,
        mfa_accepts: str = "123456",
        login_raises: str | None = None,
        tokens: dict[str, Any] | None = None,
        profile: dict[str, Any] | None = None,
    ) -> None:
        self.require_mfa = require_mfa
        self.mfa_accepts = mfa_accepts
        self.login_raises = login_raises
        self.tokens = tokens or {"oauth1_token": "t1", "oauth2_token": "t2"}
        self.profile = profile
        self._email: str | None = None
        self._authed = False

    def login(self, email: str, password: str) -> gm.ConnectResult:
        if self.login_raises:
            raise gm.GarminAuthError(self.login_raises)
        self._email = email
        if self.require_mfa:
            return gm.ConnectResult(status="mfa_required", prompt="Enter MFA code")
        self._authed = True
        return gm.ConnectResult(status="ok", tokens=self.tokens, email=email)

    def resume_mfa(self, code: str) -> gm.ConnectResult:
        if code != self.mfa_accepts:
            raise gm.GarminAuthError("invalid MFA code")
        self._authed = True
        return gm.ConnectResult(status="ok", tokens=self.tokens)

    def serialize_tokens(self) -> dict[str, Any]:
        return dict(self.tokens)

    def load_tokens(self, tokens: dict[str, Any]) -> None:
        self.tokens = dict(tokens)
        self._authed = True

    def get_user_profile(self) -> dict[str, Any] | None:
        return self.profile


def _install(adapter: _FakeAdapter) -> None:
    gm.set_adapter_factory(lambda: adapter)


# ---------------------------------------------------------------------------
# Token storage
# ---------------------------------------------------------------------------


class TestTokenStorage:
    def setup_method(self) -> None:
        gm.set_adapter_factory(None)
        gm.clear_pending()

    def teardown_method(self) -> None:
        gm.set_adapter_factory(None)
        gm.clear_pending()

    def test_store_and_load_roundtrip(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            gm.store_tokens(conn, {"oauth1_token": "abc"}, email="user@x.com")
            conn.commit()
            loaded = gm.load_tokens(conn)
        assert loaded["oauth1_token"] == "abc"
        assert loaded["email"] == "user@x.com"

    def test_clear_removes_blob(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            gm.store_tokens(conn, {"oauth1_token": "abc"}, email="user@x.com")
            conn.commit()
            gm.clear_tokens(conn)
            conn.commit()
            assert gm.load_tokens(conn) is None

    def test_mark_token_error_visible_in_status(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            gm.mark_token_error(conn, "token_expired")
            conn.commit()
            status = gm.get_status(conn)
        assert status["connected"] is False
        assert status["error"] == "token_expired"

    def test_update_last_sync_preserves_other_fields(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            gm.store_tokens(conn, {"oauth1_token": "abc"}, email="user@x.com")
            conn.commit()
            gm.update_last_sync(conn)
            conn.commit()
            blob = gm.load_tokens(conn)
        assert blob["last_sync"] is not None
        assert blob["email"] == "user@x.com"
        assert blob["oauth1_token"] == "abc"


# ---------------------------------------------------------------------------
# get_status
# ---------------------------------------------------------------------------


class TestStatus:
    def setup_method(self) -> None:
        gm.set_adapter_factory(None)
        gm.clear_pending()

    def teardown_method(self) -> None:
        gm.set_adapter_factory(None)
        gm.clear_pending()

    def test_disconnected_when_no_tokens(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            status = gm.get_status(conn)
        assert status == {
            "connected": False, "email": None, "last_sync": None, "error": None,
        }

    def test_does_not_leak_token_blob(self, tmp_path):
        """The status endpoint must never return the raw token blob."""
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            gm.store_tokens(
                conn,
                {"oauth1_token": "secret1", "oauth2_token": "secret2"},
                email="user@x.com",
            )
            conn.commit()
            status = gm.get_status(conn)
        # Whitelisted shape only; no token field at any key.
        assert set(status.keys()) == {"connected", "email", "last_sync", "error"}
        assert "secret1" not in str(status)
        assert "secret2" not in str(status)
        assert status["connected"] is True
        assert status["email"] == "user@x.com"


# ---------------------------------------------------------------------------
# connect / complete_mfa / disconnect
# ---------------------------------------------------------------------------


class TestConnect:
    def setup_method(self) -> None:
        gm.set_adapter_factory(None)
        gm.clear_pending()

    def teardown_method(self) -> None:
        gm.set_adapter_factory(None)
        gm.clear_pending()

    def test_happy_path_stores_tokens(self, tmp_path):
        adapter = _FakeAdapter()
        _install(adapter)
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            result = gm.connect(
                conn, user_id="alice", email="user@x.com", password="pw",
            )
            blob = gm.load_tokens(conn)
        assert result == {"status": "ok"}
        assert blob["oauth1_token"] == "t1"
        assert blob["email"] == "user@x.com"

    def test_missing_credentials_rejected(self, tmp_path):
        _install(_FakeAdapter())
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            with pytest.raises(gm.GarminAuthError):
                gm.connect(conn, user_id="alice", email="", password="pw")

    def test_login_error_propagates(self, tmp_path):
        _install(_FakeAdapter(login_raises="invalid credentials"))
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            with pytest.raises(gm.GarminAuthError, match="invalid credentials"):
                gm.connect(conn, user_id="alice", email="user@x.com", password="bad")
            assert gm.load_tokens(conn) is None

    def test_mfa_required_stashes_pending(self, tmp_path):
        _install(_FakeAdapter(require_mfa=True))
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            result = gm.connect(
                conn, user_id="alice", email="user@x.com", password="pw",
            )
            # No tokens yet — only stashed.
            assert gm.load_tokens(conn) is None
        assert result["status"] == "mfa_required"
        assert "MFA" in result["prompt"] or "code" in result["prompt"].lower()

    def test_mfa_complete_persists_tokens(self, tmp_path):
        _install(_FakeAdapter(require_mfa=True, mfa_accepts="999000"))
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            gm.connect(conn, user_id="alice", email="user@x.com", password="pw")
            result = gm.complete_mfa(conn, user_id="alice", code="999000")
            blob = gm.load_tokens(conn)
        assert result == {"status": "ok"}
        assert blob["email"] == "user@x.com"

    def test_mfa_wrong_code_keeps_no_tokens(self, tmp_path):
        _install(_FakeAdapter(require_mfa=True, mfa_accepts="999000"))
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            gm.connect(conn, user_id="alice", email="user@x.com", password="pw")
            with pytest.raises(gm.GarminAuthError):
                gm.complete_mfa(conn, user_id="alice", code="000000")
            assert gm.load_tokens(conn) is None

    def test_mfa_without_pending_rejected(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            with pytest.raises(gm.GarminAuthError, match="no pending"):
                gm.complete_mfa(conn, user_id="alice", code="123456")

    def test_pending_is_per_user(self, tmp_path):
        """alice's pending-auth must not leak into bob's complete_mfa call."""
        _install(_FakeAdapter(require_mfa=True))
        ctx_a = _ctx(tmp_path, "alice")
        with health_db.connect(ctx_a.db_path) as conn:
            gm.connect(conn, user_id="alice", email="a@x.com", password="pw")
        ctx_b = _ctx(tmp_path, "bob")
        with health_db.connect(ctx_b.db_path) as conn:
            with pytest.raises(gm.GarminAuthError, match="no pending"):
                gm.complete_mfa(conn, user_id="bob", code="123456")

    def test_disconnect_removes_tokens(self, tmp_path):
        _install(_FakeAdapter())
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            gm.connect(conn, user_id="alice", email="user@x.com", password="pw")
            gm.disconnect(conn, user_id="alice")
            assert gm.load_tokens(conn) is None

    def test_disconnect_clears_error(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            gm.mark_token_error(conn, "token_expired")
            conn.commit()
            gm.disconnect(conn, user_id="alice")
            assert gm.get_status(conn)["error"] is None

    def test_disconnect_clears_pending(self, tmp_path):
        _install(_FakeAdapter(require_mfa=True))
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            gm.connect(conn, user_id="alice", email="user@x.com", password="pw")
            gm.disconnect(conn, user_id="alice")
            # MFA must now fail because the pending entry was cleared.
            with pytest.raises(gm.GarminAuthError, match="no pending"):
                gm.complete_mfa(conn, user_id="alice", code="123456")

    def test_connect_clears_prior_error(self, tmp_path):
        """A fresh successful connect must clear any previous token_expired flag."""
        _install(_FakeAdapter())
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            gm.mark_token_error(conn, "token_expired")
            conn.commit()
            gm.connect(conn, user_id="alice", email="user@x.com", password="pw")
            assert gm.get_status(conn)["error"] is None


# ---------------------------------------------------------------------------
# Pending-auth TTL
# ---------------------------------------------------------------------------


class TestPendingTTL:
    def setup_method(self) -> None:
        gm.set_adapter_factory(None)
        gm.clear_pending()

    def teardown_method(self) -> None:
        gm.set_adapter_factory(None)
        gm.clear_pending()

    def test_expired_pending_is_dropped(self, tmp_path, monkeypatch):
        _install(_FakeAdapter(require_mfa=True))
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            gm.connect(conn, user_id="alice", email="user@x.com", password="pw")

        # Fast-forward past the TTL by patching monotonic.
        import time as _time
        base = _time.monotonic() + gm.PENDING_AUTH_TTL_SEC + 60
        monkeypatch.setattr(gm.time, "monotonic", lambda: base)

        with health_db.connect(ctx.db_path) as conn:
            with pytest.raises(gm.GarminAuthError, match="no pending"):
                gm.complete_mfa(conn, user_id="alice", code="123456")
