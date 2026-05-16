"""Live integration tests against api.garmin.com.

Skipped by default (``@pytest.mark.integration``). Run with::

    uv run pytest tests/test_health_garmin_integration.py -m integration -v

Credentials come from ``tests/.env`` (gitignored). The variables are
``TEST_GARMIN_USERNAME`` and ``TEST_GARMIN_PASSWORD``. We never log
them, and the test deliberately uses a throwaway istota.db so no
production state is touched.

Caveats: real network calls — these tests may legitimately fail when
Garmin's API is down, when the credentials are rotated, or when MFA is
turned on for the account (in which case the test will surface an
``mfa_required`` response and the operator must complete the flow
manually).
"""

from __future__ import annotations

import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from istota import db as framework_db
from istota.health import db as health_db
from istota.health import garmin as gm
from istota.health import garmin_sync
from istota.health._migrate import ensure_initialised
from istota.health.workspace import synthesize_health_context


pytestmark = pytest.mark.integration


def _load_test_env() -> None:
    """Load tests/.env into os.environ if present."""
    env_path = Path(__file__).parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


_load_test_env()


GARMIN_USER = os.environ.get("TEST_GARMIN_USERNAME", "")
GARMIN_PASS = os.environ.get("TEST_GARMIN_PASSWORD", "")


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    monkeypatch.setenv(
        "ISTOTA_SECRET_KEY", "test-key-test-key-test-key-test-key-test-key",
    )


@pytest.fixture
def fdb(tmp_path):
    path = tmp_path / "istota.db"
    framework_db.init_db(path)
    return path


@pytest.fixture
def ctx(tmp_path):
    c = synthesize_health_context("integration_user", tmp_path / "workspace")
    c.ensure_dirs()
    ensure_initialised(c)
    return c


@pytest.fixture(autouse=True)
def _reset_adapter():
    gm.set_adapter_factory(None)
    gm.clear_pending()
    yield
    gm.set_adapter_factory(None)
    gm.clear_pending()


@pytest.mark.skipif(
    not GARMIN_USER or not GARMIN_PASS,
    reason="TEST_GARMIN_USERNAME / TEST_GARMIN_PASSWORD not set in tests/.env",
)
class TestRealGarminAuthAndSync:
    """Single end-to-end flow: connect once, sync once. Garmin's auth
    endpoint rate-limits aggressively (per-IP), so we deliberately avoid
    making more than one ``login()`` call per test session."""

    def test_connect_and_sync(self, fdb, ctx):
        # ---- Connect ------------------------------------------------
        try:
            result = gm.connect(
                fdb,
                user_id=ctx.user_id,
                email=GARMIN_USER,
                password=GARMIN_PASS,
            )
        except gm.GarminNotInstalled:
            pytest.skip("garminconnect not installed; uv sync --extra garmin")
        except gm.GarminAuthError as exc:
            msg = str(exc).lower()
            if "429" in msg or "rate" in msg or "too many" in msg:
                pytest.skip(f"Garmin rate-limited the test connect: {exc}")
            pytest.fail(f"live Garmin auth failed: {exc}")

        if result.get("status") == "mfa_required":
            pytest.skip("account has MFA enabled — manual code entry required")

        assert result == {"status": "ok"}, result

        status = gm.get_status(fdb, ctx.user_id)
        assert status["connected"] is True
        assert status["email"] == GARMIN_USER
        assert status["error"] is None

        # ---- Sync ---------------------------------------------------
        today = datetime.now(timezone.utc).date()
        sync_result = garmin_sync.sync_garmin(
            ctx, fdb, days_back=1, today=today,
        )

        # If the sync raised the "Not authenticated" failure mode (a
        # known Garmin SDK quirk where login() succeeds but session
        # state isn't usable for follow-ups when the mobile path was
        # rate-limited and fell back), surface as a skip — not a test
        # failure. Our auth-error handling still correctly marks the
        # token expired in that case, which the unit tests cover.
        if sync_result.auth_error:
            errs = " ".join(sync_result.errors)
            if "not authenticated" in errs.lower() or "429" in errs:
                pytest.skip(
                    f"Garmin rate-limited or session unusable: {errs}",
                )
            pytest.fail(f"sync surfaced unexpected auth_error: {errs}")

        # We don't insist on specific metrics — the test account may
        # not have worn a watch yesterday. The contract this test
        # exercises is: live OAuth → live HTTP fetch → no auth-error
        # surfaced. Anything inserted is icing.
        assert sync_result.days_processed == 1
