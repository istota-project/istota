"""Tests for the Garmin auth/token plumbing in the health module.

Tokens now live in the encrypted ``secrets`` table (Fernet via
``ISTOTA_SECRET_KEY``), so every test fixture initialises a framework
``istota.db`` and sets the master key via ``monkeypatch``. The real
``garminconnect`` SDK is never imported — :class:`_FakeAdapter`
provides controllable login / MFA / token outputs.

Test classes guard isolation with ``setup_method`` so xdist-parallel
runs don't share the module-global pending-auth cache.
"""

from __future__ import annotations

from typing import Any

import pytest

from istota import db as framework_db
from istota.health import garmin as gm


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    """Every Garmin test needs a Fernet key for the secrets table."""
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key-test-key-test-key-test-key-test-key")


@pytest.fixture
def fdb(tmp_path):
    """Framework istota.db, schema-initialised."""
    path = tmp_path / "istota.db"
    framework_db.init_db(path)
    return path


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
# Token storage (round-trip through encrypted secrets table)
# ---------------------------------------------------------------------------


class TestTokenStorage:
    def setup_method(self) -> None:
        gm.set_adapter_factory(None)
        gm.clear_pending()

    def teardown_method(self) -> None:
        gm.set_adapter_factory(None)
        gm.clear_pending()

    def test_store_and_load_roundtrip(self, fdb):
        gm.store_tokens(fdb, "alice", {"oauth1_token": "abc"}, email="user@x.com")
        loaded = gm.load_tokens(fdb, "alice")
        assert loaded == {"oauth1_token": "abc"}

    def test_email_is_stored_separately(self, fdb):
        gm.store_tokens(fdb, "alice", {"oauth1_token": "abc"}, email="user@x.com")
        status = gm.get_status(fdb, "alice")
        assert status["email"] == "user@x.com"
        # The token blob in load_tokens must NOT carry email — it's a
        # presentation field, not part of the SDK session state.
        assert "email" not in gm.load_tokens(fdb, "alice")

    def test_clear_removes_blob(self, fdb):
        gm.store_tokens(fdb, "alice", {"oauth1_token": "abc"}, email="user@x.com")
        gm.clear_tokens(fdb, "alice")
        assert gm.load_tokens(fdb, "alice") is None

    def test_mark_token_error_clears_blob(self, fdb):
        """H6 fix: marking token_expired also wipes the blob so the UI
        no longer shows 'Connected' with a red banner forever."""
        gm.store_tokens(fdb, "alice", {"oauth1_token": "abc"}, email="user@x.com")
        gm.mark_token_error(fdb, "alice", "token_expired")
        status = gm.get_status(fdb, "alice")
        assert status["connected"] is False
        assert status["error"] == "token_expired"

    def test_update_last_sync_is_independent_of_blob(self, fdb):
        """H2 fix: last_sync lives in its own secret key, not merged
        into the token blob — so the update doesn't read-modify-write
        the OAuth state."""
        gm.store_tokens(fdb, "alice", {"oauth1_token": "abc"}, email="user@x.com")
        gm.update_last_sync(fdb, "alice")
        status = gm.get_status(fdb, "alice")
        assert status["last_sync"] is not None
        assert status["connected"] is True
        # Blob untouched.
        assert gm.load_tokens(fdb, "alice") == {"oauth1_token": "abc"}


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

    def test_disconnected_when_no_tokens(self, fdb):
        status = gm.get_status(fdb, "alice")
        assert status == {
            "connected": False, "email": None, "last_sync": None, "error": None,
        }

    def test_does_not_leak_token_blob(self, fdb):
        gm.store_tokens(
            fdb, "alice",
            {"oauth1_token": "secret1", "oauth2_token": "secret2"},
            email="user@x.com",
        )
        status = gm.get_status(fdb, "alice")
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

    def test_happy_path_stores_tokens(self, fdb):
        _install(_FakeAdapter())
        result = gm.connect(fdb, user_id="alice", email="user@x.com", password="pw")
        assert result == {"status": "ok"}
        blob = gm.load_tokens(fdb, "alice")
        assert blob["oauth1_token"] == "t1"
        assert gm.get_status(fdb, "alice")["email"] == "user@x.com"

    def test_missing_credentials_rejected(self, fdb):
        _install(_FakeAdapter())
        with pytest.raises(gm.GarminAuthError):
            gm.connect(fdb, user_id="alice", email="", password="pw")

    def test_login_error_propagates(self, fdb):
        _install(_FakeAdapter(login_raises="invalid credentials"))
        with pytest.raises(gm.GarminAuthError, match="invalid credentials"):
            gm.connect(fdb, user_id="alice", email="user@x.com", password="bad")
        assert gm.load_tokens(fdb, "alice") is None

    def test_mfa_required_stashes_pending(self, fdb):
        _install(_FakeAdapter(require_mfa=True))
        result = gm.connect(fdb, user_id="alice", email="user@x.com", password="pw")
        assert gm.load_tokens(fdb, "alice") is None  # no tokens until MFA done
        assert result["status"] == "mfa_required"

    def test_mfa_complete_persists_tokens(self, fdb):
        _install(_FakeAdapter(require_mfa=True, mfa_accepts="999000"))
        gm.connect(fdb, user_id="alice", email="user@x.com", password="pw")
        result = gm.complete_mfa(fdb, user_id="alice", code="999000")
        assert result == {"status": "ok"}
        assert gm.get_status(fdb, "alice")["email"] == "user@x.com"

    def test_mfa_wrong_code_keeps_no_tokens(self, fdb):
        _install(_FakeAdapter(require_mfa=True, mfa_accepts="999000"))
        gm.connect(fdb, user_id="alice", email="user@x.com", password="pw")
        with pytest.raises(gm.GarminAuthError):
            gm.complete_mfa(fdb, user_id="alice", code="000000")
        assert gm.load_tokens(fdb, "alice") is None

    def test_mfa_without_pending_rejected(self, fdb):
        with pytest.raises(gm.GarminAuthError, match="no pending"):
            gm.complete_mfa(fdb, user_id="alice", code="123456")

    def test_pending_is_per_user(self, fdb):
        """alice's pending-auth must not leak into bob's complete_mfa call."""
        _install(_FakeAdapter(require_mfa=True))
        gm.connect(fdb, user_id="alice", email="a@x.com", password="pw")
        with pytest.raises(gm.GarminAuthError, match="no pending"):
            gm.complete_mfa(fdb, user_id="bob", code="123456")

    def test_disconnect_removes_tokens(self, fdb):
        _install(_FakeAdapter())
        gm.connect(fdb, user_id="alice", email="user@x.com", password="pw")
        gm.disconnect(fdb, user_id="alice")
        assert gm.load_tokens(fdb, "alice") is None

    def test_disconnect_clears_error(self, fdb):
        gm.mark_token_error(fdb, "alice", "token_expired")
        gm.disconnect(fdb, user_id="alice")
        assert gm.get_status(fdb, "alice")["error"] is None

    def test_disconnect_clears_pending(self, fdb):
        _install(_FakeAdapter(require_mfa=True))
        gm.connect(fdb, user_id="alice", email="user@x.com", password="pw")
        gm.disconnect(fdb, user_id="alice")
        # MFA must now fail because the pending entry was cleared.
        with pytest.raises(gm.GarminAuthError, match="no pending"):
            gm.complete_mfa(fdb, user_id="alice", code="123456")

    def test_connect_clears_prior_error(self, fdb):
        gm.mark_token_error(fdb, "alice", "token_expired")
        _install(_FakeAdapter())
        gm.connect(fdb, user_id="alice", email="user@x.com", password="pw")
        assert gm.get_status(fdb, "alice")["error"] is None


# ---------------------------------------------------------------------------
# Pending-auth TTL and cap
# ---------------------------------------------------------------------------


class TestPending:
    def setup_method(self) -> None:
        gm.set_adapter_factory(None)
        gm.clear_pending()

    def teardown_method(self) -> None:
        gm.set_adapter_factory(None)
        gm.clear_pending()

    def test_expired_pending_is_dropped(self, fdb, monkeypatch):
        _install(_FakeAdapter(require_mfa=True))
        gm.connect(fdb, user_id="alice", email="user@x.com", password="pw")

        # Fast-forward past the TTL by patching monotonic.
        import time as _time
        base = _time.monotonic() + gm.PENDING_AUTH_TTL_SEC + 60
        monkeypatch.setattr(gm.time, "monotonic", lambda: base)

        with pytest.raises(gm.GarminAuthError, match="no pending"):
            gm.complete_mfa(fdb, user_id="alice", code="123456")

    def test_cap_evicts_oldest(self, fdb, monkeypatch):
        """L2: a runaway caller can't grow the pending cache unboundedly."""
        monkeypatch.setattr(gm, "PENDING_MAX_ENTRIES", 4)
        _install(_FakeAdapter(require_mfa=True))
        for i in range(6):
            gm.connect(
                fdb, user_id=f"u{i}", email=f"u{i}@x.com", password="pw",
            )
        # Earliest two should have been evicted, latest four kept.
        assert gm._take_pending("u0") is None
        assert gm._take_pending("u1") is None
        for i in range(2, 6):
            assert gm._take_pending(f"u{i}") is not None


# ---------------------------------------------------------------------------
# Auth-error / rate-limit / transient detection
# ---------------------------------------------------------------------------


class TestErrorDetection:
    def test_class_name_match(self):
        class GarminConnectAuthenticationError(Exception):
            pass
        assert gm._looks_like_auth_error(GarminConnectAuthenticationError("bad"))

    def test_login_class_match(self):
        class LoginError(Exception):
            pass
        assert gm._looks_like_auth_error(LoginError("bad"))

    def test_403_alone_does_not_match(self):
        """H4 regression: a bare WAF 403 must not be classified as auth."""
        assert not gm._looks_like_auth_error(Exception("403 Forbidden"))

    def test_401_with_keyword_matches(self):
        assert gm._looks_like_auth_error(Exception("401 token expired"))

    def test_401_alone_does_not_match(self):
        assert not gm._looks_like_auth_error(Exception("401 retries exhausted"))

    def test_rate_limit_detection(self):
        ok, retry_after = gm._looks_like_rate_limited(
            Exception("429 Too Many Requests, Retry-After: 90"),
        )
        assert ok is True
        assert retry_after == 90

    def test_rate_limit_without_retry_after(self):
        ok, retry_after = gm._looks_like_rate_limited(Exception("429 throttled"))
        assert ok is True
        assert retry_after is None

    def test_non_rate_limit(self):
        ok, _ = gm._looks_like_rate_limited(Exception("500 server error"))
        assert ok is False

    def test_transient_detection(self):
        assert gm._looks_like_transient_network_error(Exception("502 bad gateway"))
        assert gm._looks_like_transient_network_error(Exception("connection reset"))
        # Not transient:
        assert not gm._looks_like_transient_network_error(Exception("400 bad request"))
