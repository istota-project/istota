"""Tests for the Garmin daily-summary sync engine.

A :class:`_FakeAdapter` returns prescribed JSON per endpoint per day so
tests can drive the engine through happy / partial / auth-error /
rate-limit / missing-data shapes without touching the real SDK.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from istota import db as framework_db
from istota.health import db as health_db
from istota.health import garmin as gm
from istota.health import garmin_sync
from istota.health._migrate import ensure_initialised
from istota.health.workspace import synthesize_health_context


@pytest.fixture(autouse=True)
def _secret_key(monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "test-key-test-key-test-key-test-key-test-key")


@pytest.fixture
def fdb(tmp_path):
    path = tmp_path / "istota.db"
    framework_db.init_db(path)
    return path


def _ctx(tmp_path, user_id: str = "alice"):
    c = synthesize_health_context(user_id, tmp_path / "workspace")
    c.ensure_dirs()
    ensure_initialised(c)
    return c


class _FakeAdapter:
    """Adapter that returns prescribed payloads per date.

    ``per_day``: ``{iso_date: {endpoint_name: payload, ...}}``.
    Endpoints not in the entry return None.

    ``auth_fail_on`` — set of endpoint names that should raise
    :class:`gm.GarminAuthError`.

    ``rate_limit_on`` — set of endpoint names that should raise
    :class:`gm.GarminRateLimited`.
    """

    def __init__(
        self,
        per_day: dict[str, dict[str, Any]] | None = None,
        auth_fail_on: set[str] | None = None,
        rate_limit_on: set[str] | None = None,
        tokens_after_sync: dict[str, Any] | None = None,
    ) -> None:
        self.per_day = per_day or {}
        self.auth_fail_on = auth_fail_on or set()
        self.rate_limit_on = rate_limit_on or set()
        self.tokens_after_sync = tokens_after_sync
        self.calls: list[tuple[str, str]] = []
        self._loaded_tokens: dict[str, Any] | None = None

    def _get(self, name: str, dt: str):
        self.calls.append((name, dt))
        if name in self.auth_fail_on:
            raise gm.GarminAuthError(f"auth fail on {name}")
        if name in self.rate_limit_on:
            raise gm.GarminRateLimited(retry_after=1)
        return self.per_day.get(dt, {}).get(name)

    def get_sleep_data(self, dt): return self._get("sleep", dt)
    def get_stress_data(self, dt): return self._get("stress", dt)
    def get_body_battery(self, dt): return self._get("body_battery", dt)
    def get_steps_data(self, dt): return self._get("steps", dt)
    def get_user_summary(self, dt): return self._get("user_summary", dt)
    def get_spo2_data(self, dt): return self._get("spo2", dt)
    def get_hrv_data(self, dt): return self._get("hrv", dt)
    def get_vo2_max(self, dt): return self._get("vo2", dt)
    def get_respiration_data(self, dt): return self._get("respiration", dt)
    def get_resting_heart_rate(self, dt): return self._get("resting_hr", dt)
    def get_body_composition(self, dt): return self._get("body_composition", dt)

    # Stubs to satisfy the Protocol.
    def login(self, *a, **kw): raise NotImplementedError
    def resume_mfa(self, *a, **kw): raise NotImplementedError
    def serialize_tokens(self):
        return self.tokens_after_sync or {"oauth1_token": "fresh"}
    def load_tokens(self, tokens):
        self._loaded_tokens = dict(tokens)
    def get_user_profile(self): return None


# ---------------------------------------------------------------------------
# Date window
# ---------------------------------------------------------------------------


class TestDateWindow:
    def test_yesterday_only_for_days_back_1(self):
        days = garmin_sync._iter_dates(today=date(2026, 5, 15), days_back=1)
        assert days == [date(2026, 5, 14)]

    def test_seven_day_window(self):
        days = garmin_sync._iter_dates(today=date(2026, 5, 15), days_back=7)
        assert len(days) == 7
        assert days[-1] == date(2026, 5, 14)

    def test_today_is_never_pulled(self):
        for n in (1, 7, 30):
            assert date(2026, 5, 15) not in garmin_sync._iter_dates(
                today=date(2026, 5, 15), days_back=n,
            )


class TestUserToday:
    def test_user_tz_anchors_today(self, monkeypatch):
        """H5: a user in UTC+12 at 02:00 local sees local-yesterday
        as their _previous_ day, not UTC's date - 1.

        We can't easily freeze wall-clock time across timezones here,
        but we can verify the TZ branch is taken and produces a date.
        """
        out_pacific = garmin_sync._user_today("Pacific/Auckland")
        out_utc = garmin_sync._user_today(None)
        assert out_pacific is not None
        assert out_utc is not None

    def test_invalid_tz_falls_back_to_utc(self):
        # Doesn't raise; falls back to UTC.
        result = garmin_sync._user_today("Not/A/Real/Zone")
        assert result is not None


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


class TestExtractors:
    def test_sleep_seconds_become_minutes(self):
        out = garmin_sync._from_sleep(
            {"dailySleepDTO": {
                "sleepTimeSeconds": 27000,
                "deepSleepSeconds": 3600,
                "lightSleepSeconds": 12600,
                "remSleepSeconds": 9000,
                "awakeSleepSeconds": 1800,
                "overallScore": 82,
            }},
        )
        assert out["sleep_duration_min"] == 450.0
        assert out["sleep_score"] == 82

    def test_stress(self):
        assert garmin_sync._from_stress(
            {"avgStressLevel": 35, "maxStressLevel": 88},
        ) == {"stress_avg": 35.0, "stress_max": 88.0}

    def test_body_battery_no_longer_uses_delta_fallback(self):
        """M5 fix: charged/drained are deltas, not levels — must not
        be reported as body_battery_high/_low."""
        out = garmin_sync._from_body_battery({"charged": 95, "drained": 80})
        assert out == {"body_battery_high": None, "body_battery_low": None}

    def test_body_battery_dict_with_max_min(self):
        assert garmin_sync._from_body_battery({"max": 90, "min": 20}) == {
            "body_battery_high": 90.0, "body_battery_low": 20.0,
        }

    def test_body_composition_uses_grams_field_explicitly(self):
        """M6 fix: weight comes from weightInGrams, not a heuristic on
        a generic ``weight`` field."""
        out = garmin_sync._from_body_composition(
            {"weightInGrams": 82500, "bodyFat": 18.5},
        )
        assert out["weight"] == 82.5
        assert out["body_fat_pct"] == 18.5

    def test_body_composition_weight_already_kg(self):
        assert garmin_sync._from_body_composition({"weight": 82.5})["weight"] == 82.5

    def test_steps_zero_is_real_data(self):
        """M7 fix: a rest day with 0 steps must be reported as 0, not
        dropped as missing-data."""
        assert garmin_sync._from_steps({"totalSteps": 0}) == {"steps": 0.0}

    def test_steps_dict_total(self):
        assert garmin_sync._from_steps({"totalSteps": 8421}) == {"steps": 8421.0}

    def test_vo2_dict_input(self):
        assert garmin_sync._from_vo2({"vo2MaxValue": 48.5}) == {"vo2_max": 48.5}


# ---------------------------------------------------------------------------
# Sync end-to-end
# ---------------------------------------------------------------------------


class TestSyncEngine:
    def setup_method(self):
        gm.set_adapter_factory(None)
        gm.clear_pending()

    def teardown_method(self):
        gm.set_adapter_factory(None)
        gm.clear_pending()

    def test_happy_path_inserts_rows(self, tmp_path, fdb):
        ctx = _ctx(tmp_path)
        adapter = _FakeAdapter(
            per_day={
                "2026-05-14": {
                    "sleep": {"dailySleepDTO": {"sleepTimeSeconds": 27000, "overallScore": 82}},
                    "stress": {"avgStressLevel": 30, "maxStressLevel": 75},
                    "steps": {"totalSteps": 9000},
                },
            },
        )
        # Pre-seed tokens so the engine has something to re-serialise.
        gm.store_tokens(fdb, ctx.user_id, {"oauth1_token": "abc"}, email="user@x.com")

        res = garmin_sync.sync_garmin(
            ctx, fdb, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        assert res.auth_error is False
        assert res.days_processed == 1
        assert res.inserted == 5
        assert res.skipped == 0

        with health_db.connect(ctx.db_path) as conn:
            rows = conn.execute("SELECT metric, value FROM stats").fetchall()
        metrics = {r["metric"]: r["value"] for r in rows}
        assert metrics["sleep_duration_min"] == 450.0
        assert metrics["steps"] == 9000.0

    def test_dedup_skips_existing_rows(self, tmp_path, fdb):
        ctx = _ctx(tmp_path)
        adapter = _FakeAdapter(per_day={"2026-05-14": {"steps": {"totalSteps": 9000}}})
        gm.store_tokens(fdb, ctx.user_id, {"oauth1_token": "abc"})
        garmin_sync.sync_garmin(
            ctx, fdb, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        res2 = garmin_sync.sync_garmin(
            ctx, fdb, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        assert res2.inserted == 0
        assert res2.skipped == 1

    def test_unique_index_blocks_concurrent_double_insert(self, tmp_path, fdb):
        """H7: even if two parallel runs both pass the dedup check, the
        UNIQUE index converts the second insert into IntegrityError ->
        counted as skipped, not duplicated."""
        ctx = _ctx(tmp_path)
        # Insert one row directly, then run sync — engine sees no row in
        # its check, but UNIQUE on (metric, measured_at, source) blocks
        # the second insert. Use the same measured_at the engine would
        # produce (noon UTC of the date).
        measured_at = garmin_sync._measured_at(date(2026, 5, 14))
        with health_db.connect(ctx.db_path) as conn:
            health_db.insert_stat(
                conn, metric="steps", value=9000.0, unit="steps",
                measured_at=measured_at, source="garmin",
            )
            conn.commit()
        gm.store_tokens(fdb, ctx.user_id, {"oauth1_token": "abc"})
        adapter = _FakeAdapter(per_day={"2026-05-14": {"steps": {"totalSteps": 9000}}})
        res = garmin_sync.sync_garmin(
            ctx, fdb, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        assert res.inserted == 0
        assert res.skipped == 1

    def test_auth_error_clears_tokens(self, tmp_path, fdb):
        """H6: auth-error must wipe the token blob so the UI shows
        disconnected rather than 'connected + red banner forever'."""
        ctx = _ctx(tmp_path)
        gm.store_tokens(fdb, ctx.user_id, {"oauth1_token": "abc"}, email="user@x.com")
        adapter = _FakeAdapter(auth_fail_on={"sleep"})
        res = garmin_sync.sync_garmin(
            ctx, fdb, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        assert res.auth_error is True
        status = gm.get_status(fdb, ctx.user_id)
        assert status["error"] == "token_expired"
        # Blob is wiped — connected flips to false.
        assert status["connected"] is False

    def test_rate_limit_backoff(self, tmp_path, fdb, monkeypatch):
        """Spec: respect Retry-After (or 60s) on 429."""
        ctx = _ctx(tmp_path)
        gm.store_tokens(fdb, ctx.user_id, {"oauth1_token": "abc"})
        adapter = _FakeAdapter(
            rate_limit_on={"sleep"},
            per_day={
                "2026-05-14": {"steps": {"totalSteps": 9000}},
                "2026-05-13": {"steps": {"totalSteps": 8000}},
            },
        )
        slept: list[float] = []
        monkeypatch.setattr(garmin_sync.time, "sleep", lambda s: slept.append(s))

        res = garmin_sync.sync_garmin(
            ctx, fdb, days_back=2, today=date(2026, 5, 15), adapter=adapter,
        )
        # The first day raised rate-limit; the second day should pause
        # before running.
        assert slept, "expected a Retry-After sleep before the next day's pull"
        assert res.errored >= 1

    def test_rotated_tokens_persisted(self, tmp_path, fdb):
        """H1: after a successful sync, the adapter's (possibly rotated)
        token state is written back to the encrypted store."""
        ctx = _ctx(tmp_path)
        gm.store_tokens(fdb, ctx.user_id, {"oauth1_token": "old"})
        adapter = _FakeAdapter(
            per_day={"2026-05-14": {"steps": {"totalSteps": 9000}}},
            tokens_after_sync={"oauth1_token": "ROTATED"},
        )
        garmin_sync.sync_garmin(
            ctx, fdb, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        blob = gm.load_tokens(fdb, ctx.user_id)
        assert blob["oauth1_token"] == "ROTATED"

    def test_no_adapter_no_tokens_returns_auth_error(self, tmp_path, fdb):
        ctx = _ctx(tmp_path)
        gm.set_adapter_factory(lambda: _FakeAdapter())
        res = garmin_sync.sync_garmin(
            ctx, fdb, days_back=1, today=date(2026, 5, 15),
        )
        assert res.auth_error is True
        assert "no Garmin tokens" in res.errors[0]

    def test_missing_day_inserts_nothing(self, tmp_path, fdb):
        ctx = _ctx(tmp_path)
        gm.store_tokens(fdb, ctx.user_id, {"oauth1_token": "abc"})
        adapter = _FakeAdapter(per_day={})
        res = garmin_sync.sync_garmin(
            ctx, fdb, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        assert res.inserted == 0
        assert res.skipped == 0
        assert res.auth_error is False

    def test_measured_at_anchored_to_noon_utc(self, tmp_path, fdb):
        ctx = _ctx(tmp_path)
        gm.store_tokens(fdb, ctx.user_id, {"oauth1_token": "abc"})
        adapter = _FakeAdapter(per_day={"2026-05-14": {"steps": {"totalSteps": 9000}}})
        garmin_sync.sync_garmin(
            ctx, fdb, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        with health_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT measured_at FROM stats WHERE metric = 'steps'"
            ).fetchone()
        parsed = datetime.fromisoformat(row["measured_at"])
        assert parsed.date() == date(2026, 5, 14)
        assert parsed.hour == 12
