"""Tests for the Garmin daily-summary sync engine.

A :class:`_FakeAdapter` returns prescribed JSON per endpoint per day so
tests can drive the engine through happy / partial / auth-error /
missing-data shapes without touching the real SDK.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

import pytest

from istota.health import db as health_db
from istota.health import garmin as gm
from istota.health import garmin_sync
from istota.health._migrate import ensure_initialised
from istota.health.workspace import synthesize_health_context


def _ctx(tmp_path, user_id: str = "alice"):
    c = synthesize_health_context(user_id, tmp_path / "workspace")
    c.ensure_dirs()
    ensure_initialised(c)
    return c


class _FakeAdapter:
    """Adapter that returns prescribed payloads per date.

    Each ``per_day`` entry: ``{iso_date: {endpoint_name: payload, ...}}``.
    Endpoints not in the entry return ``None``.

    ``auth_fail_on`` — set of endpoint names that should raise
    :class:`gm.GarminAuthError`.
    """

    def __init__(
        self,
        per_day: dict[str, dict[str, Any]] | None = None,
        auth_fail_on: set[str] | None = None,
    ) -> None:
        self.per_day = per_day or {}
        self.auth_fail_on = auth_fail_on or set()
        self.calls: list[tuple[str, str]] = []

    def _get(self, name: str, dt: str):
        self.calls.append((name, dt))
        if name in self.auth_fail_on:
            raise gm.GarminAuthError(f"auth fail on {name}")
        return self.per_day.get(dt, {}).get(name)

    def get_sleep_data(self, dt: str):
        return self._get("sleep", dt)

    def get_stress_data(self, dt: str):
        return self._get("stress", dt)

    def get_body_battery(self, dt: str):
        return self._get("body_battery", dt)

    def get_steps_data(self, dt: str):
        return self._get("steps", dt)

    def get_user_summary(self, dt: str):
        return self._get("user_summary", dt)

    def get_spo2_data(self, dt: str):
        return self._get("spo2", dt)

    def get_hrv_data(self, dt: str):
        return self._get("hrv", dt)

    def get_vo2_max(self, dt: str):
        return self._get("vo2", dt)

    def get_respiration_data(self, dt: str):
        return self._get("respiration", dt)

    def get_resting_heart_rate(self, dt: str):
        return self._get("resting_hr", dt)

    def get_body_composition(self, dt: str):
        return self._get("body_composition", dt)

    # Stubs to satisfy the Protocol.
    def login(self, *a, **kw): raise NotImplementedError
    def resume_mfa(self, *a, **kw): raise NotImplementedError
    def serialize_tokens(self): return {}
    def load_tokens(self, tokens): pass
    def get_user_profile(self): return None


# ---------------------------------------------------------------------------
# Date window
# ---------------------------------------------------------------------------


class TestDateWindow:
    def test_yesterday_only_for_days_back_1(self):
        today = date(2026, 5, 15)
        days = garmin_sync._iter_dates(today=today, days_back=1)
        assert days == [date(2026, 5, 14)]

    def test_seven_day_window(self):
        today = date(2026, 5, 15)
        days = garmin_sync._iter_dates(today=today, days_back=7)
        assert days[0] == date(2026, 5, 8)
        assert days[-1] == date(2026, 5, 14)
        assert len(days) == 7

    def test_today_is_never_pulled(self):
        today = date(2026, 5, 15)
        for n in (1, 7, 30):
            days = garmin_sync._iter_dates(today=today, days_back=n)
            assert today not in days

    def test_clamps_zero_to_one(self):
        today = date(2026, 5, 15)
        assert garmin_sync._iter_dates(today=today, days_back=0) == [date(2026, 5, 14)]


# ---------------------------------------------------------------------------
# Extractors
# ---------------------------------------------------------------------------


class TestExtractors:
    def test_sleep_seconds_become_minutes(self):
        out = garmin_sync._from_sleep(
            {"dailySleepDTO": {
                "sleepTimeSeconds": 27000,  # 7.5h
                "deepSleepSeconds": 3600,
                "lightSleepSeconds": 12600,
                "remSleepSeconds": 9000,
                "awakeSleepSeconds": 1800,
                "overallScore": 82,
            }},
        )
        assert out["sleep_duration_min"] == 450.0
        assert out["sleep_deep_min"] == 60.0
        assert out["sleep_score"] == 82

    def test_sleep_empty_payload(self):
        assert garmin_sync._from_sleep({}) == {
            "sleep_duration_min": None,
            "sleep_deep_min": None,
            "sleep_light_min": None,
            "sleep_rem_min": None,
            "sleep_awake_min": None,
            "sleep_score": None,
        }

    def test_stress(self):
        assert garmin_sync._from_stress(
            {"avgStressLevel": 35, "maxStressLevel": 88},
        ) == {"stress_avg": 35.0, "stress_max": 88.0}

    def test_body_battery_dict_shape(self):
        assert garmin_sync._from_body_battery(
            {"max": 90, "min": 20},
        ) == {"body_battery_high": 90.0, "body_battery_low": 20.0}

    def test_body_composition_weight_grams_to_kg(self):
        out = garmin_sync._from_body_composition(
            {"weight": 82500, "bodyFat": 18.5},
        )
        assert out["weight"] == 82.5
        assert out["body_fat_pct"] == 18.5

    def test_body_composition_weight_already_kg(self):
        out = garmin_sync._from_body_composition({"weight": 82.5})
        assert out["weight"] == 82.5

    def test_vo2_scalar_input(self):
        assert garmin_sync._from_vo2(48.5) == {"vo2_max": 48.5}

    def test_vo2_dict_input(self):
        assert garmin_sync._from_vo2({"vo2MaxValue": 48.5}) == {"vo2_max": 48.5}

    def test_steps_dict_total(self):
        assert garmin_sync._from_steps({"totalSteps": 8421}) == {"steps": 8421.0}


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

    def test_happy_path_inserts_rows(self, tmp_path):
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
        res = garmin_sync.sync_garmin(
            ctx, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        assert res.auth_error is False
        assert res.days_processed == 1
        # sleep_duration_min, sleep_score, stress_avg, stress_max, steps = 5 rows
        assert res.inserted == 5
        assert res.skipped == 0

        with health_db.connect(ctx.db_path) as conn:
            rows = conn.execute(
                "SELECT metric, value, source FROM stats ORDER BY metric"
            ).fetchall()
        metrics = {r["metric"]: r["value"] for r in rows}
        assert metrics["sleep_duration_min"] == 450.0
        assert metrics["stress_max"] == 75.0
        assert metrics["steps"] == 9000.0
        assert all(r["source"] == "garmin" for r in rows)

    def test_dedup_skips_existing_rows(self, tmp_path):
        ctx = _ctx(tmp_path)
        adapter = _FakeAdapter(
            per_day={
                "2026-05-14": {"steps": {"totalSteps": 9000}},
            },
        )
        garmin_sync.sync_garmin(
            ctx, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        res2 = garmin_sync.sync_garmin(
            ctx, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        assert res2.inserted == 0
        assert res2.skipped == 1

    def test_missing_day_inserts_nothing(self, tmp_path):
        ctx = _ctx(tmp_path)
        adapter = _FakeAdapter(per_day={})
        res = garmin_sync.sync_garmin(
            ctx, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        assert res.inserted == 0
        assert res.skipped == 0
        assert res.auth_error is False

    def test_auth_error_marks_token_expired(self, tmp_path):
        ctx = _ctx(tmp_path)
        # Pre-seed tokens so we can verify the error flag goes up.
        with health_db.connect(ctx.db_path) as conn:
            gm.store_tokens(conn, {"oauth1_token": "abc"}, email="user@x.com")
            conn.commit()
        adapter = _FakeAdapter(auth_fail_on={"sleep"})
        res = garmin_sync.sync_garmin(
            ctx, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        assert res.auth_error is True
        with health_db.connect(ctx.db_path) as conn:
            status = gm.get_status(conn)
        assert status["error"] == "token_expired"

    def test_no_adapter_no_tokens_returns_auth_error(self, tmp_path):
        ctx = _ctx(tmp_path)
        # Force the real-adapter path to a fake one to avoid garminconnect.
        gm.set_adapter_factory(lambda: _FakeAdapter())
        res = garmin_sync.sync_garmin(
            ctx, days_back=1, today=date(2026, 5, 15),
        )
        assert res.auth_error is True
        assert "no Garmin tokens" in res.errors[0]

    def test_last_sync_stamped_after_insert(self, tmp_path):
        ctx = _ctx(tmp_path)
        with health_db.connect(ctx.db_path) as conn:
            gm.store_tokens(conn, {"oauth1_token": "abc"}, email="user@x.com")
            conn.commit()
        adapter = _FakeAdapter(
            per_day={"2026-05-14": {"steps": {"totalSteps": 9000}}},
        )
        garmin_sync.sync_garmin(
            ctx, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        with health_db.connect(ctx.db_path) as conn:
            blob = gm.load_tokens(conn)
        assert blob["last_sync"] is not None

    def test_measured_at_anchored_to_noon_utc(self, tmp_path):
        ctx = _ctx(tmp_path)
        adapter = _FakeAdapter(
            per_day={"2026-05-14": {"steps": {"totalSteps": 9000}}},
        )
        garmin_sync.sync_garmin(
            ctx, days_back=1, today=date(2026, 5, 15), adapter=adapter,
        )
        with health_db.connect(ctx.db_path) as conn:
            row = conn.execute(
                "SELECT measured_at FROM stats WHERE metric = 'steps'"
            ).fetchone()
        # Parsed as datetime — must be 12:00:00 UTC on 2026-05-14.
        parsed = datetime.fromisoformat(row["measured_at"])
        assert parsed.date() == date(2026, 5, 14)
        assert parsed.hour == 12
        assert parsed.tzinfo is not None

    def test_multi_day_window(self, tmp_path):
        ctx = _ctx(tmp_path)
        adapter = _FakeAdapter(
            per_day={
                "2026-05-12": {"steps": {"totalSteps": 1000}},
                "2026-05-13": {"steps": {"totalSteps": 2000}},
                "2026-05-14": {"steps": {"totalSteps": 3000}},
            },
        )
        res = garmin_sync.sync_garmin(
            ctx, days_back=3, today=date(2026, 5, 15), adapter=adapter,
        )
        assert res.days_processed == 3
        assert res.inserted == 3
        with health_db.connect(ctx.db_path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) AS c FROM stats WHERE metric = 'steps'"
            ).fetchone()["c"]
        assert count == 3
