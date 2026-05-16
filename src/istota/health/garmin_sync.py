"""Garmin daily-summary sync engine.

Pulls per-day summaries (sleep, stress, body battery, steps, SpO2, HRV,
VO2 max, respiration, resting HR, body composition) and writes them into
the existing ``stats`` table tagged ``source='garmin'``. Dedup is
application-layer via :func:`health_db.stat_exists_for_source` —
duplicate ``(metric, measured_at, source)`` tuples are skipped.

Errors are partitioned at endpoint granularity. A missing-data day or
500 from one endpoint doesn't stop the rest. Auth errors trip a
``garmin_error`` flag in ``health_settings`` so the settings UI can
prompt a reconnect.
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass, field
from datetime import date as _date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from istota.health import db as health_db
from istota.health import garmin as gm
from istota.health.models import HealthContext


logger = logging.getLogger(__name__)


# Each extractor: (metric_key, unit, fn(payload) -> float | None).
# ``fn`` is called with the raw endpoint response and returns the
# canonical value (or None if the response shape doesn't carry it).
@dataclass(frozen=True)
class _Extract:
    metric: str
    unit: str
    fn: Callable[[Any], float | None]


@dataclass
class SyncResult:
    inserted: int = 0
    skipped: int = 0
    errored: int = 0
    days_processed: int = 0
    errors: list[str] = field(default_factory=list)
    auth_error: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "inserted": self.inserted,
            "skipped": self.skipped,
            "errored": self.errored,
            "days_processed": self.days_processed,
            "errors": list(self.errors),
            "auth_error": self.auth_error,
        }


# ---------------------------------------------------------------------------
# Extractors — defensive .get() chains; unknown shapes return None.
# ---------------------------------------------------------------------------


def _num(x: Any) -> float | None:
    try:
        if x is None:
            return None
        f = float(x)
    except (TypeError, ValueError):
        return None
    # Garmin uses -1 / 0 / Sentinel for "no data" on several endpoints.
    # We can't distinguish "0 was measured" from "-1 means missing" in
    # general; use sentinels only where we know the metric can't be 0
    # in practice (sleep durations, VO2 max).
    return f


def _sec_to_min(x: Any) -> float | None:
    s = _num(x)
    if s is None:
        return None
    return round(s / 60.0, 2)


def _from_sleep(payload: Any) -> dict[str, float | None]:
    """Extract sleep-stage minutes + score from getDailySleep response."""
    if not isinstance(payload, dict):
        return {}
    dto = payload.get("dailySleepDTO") or payload
    if not isinstance(dto, dict):
        dto = {}
    return {
        "sleep_duration_min": _sec_to_min(
            dto.get("sleepTimeSeconds") or dto.get("sleepTime"),
        ),
        "sleep_deep_min": _sec_to_min(dto.get("deepSleepSeconds")),
        "sleep_light_min": _sec_to_min(dto.get("lightSleepSeconds")),
        "sleep_rem_min": _sec_to_min(dto.get("remSleepSeconds")),
        "sleep_awake_min": _sec_to_min(dto.get("awakeSleepSeconds")),
        "sleep_score": _num(
            (dto.get("sleepScores") or {}).get("overall", {}).get("value")
            if isinstance(dto.get("sleepScores"), dict)
            else dto.get("overallScore"),
        ),
    }


def _from_stress(payload: Any) -> dict[str, float | None]:
    if not isinstance(payload, dict):
        return {}
    return {
        "stress_avg": _num(payload.get("avgStressLevel") or payload.get("overallStressLevel")),
        "stress_max": _num(payload.get("maxStressLevel")),
    }


def _from_body_battery(payload: Any) -> dict[str, float | None]:
    """Body battery is sometimes a list of (timestamp, status, level) tuples,
    sometimes a dict with ``charged``/``drained``. Handle both."""
    high: float | None = None
    low: float | None = None
    if isinstance(payload, list):
        levels: list[float] = []
        for entry in payload:
            if isinstance(entry, dict):
                lv = entry.get("bodyBatteryValuesArray") or entry.get("bodyBatteryLevel")
                if isinstance(lv, list):
                    for row in lv:
                        if isinstance(row, list) and len(row) >= 3 and isinstance(row[2], (int, float)):
                            levels.append(float(row[2]))
                elif isinstance(lv, (int, float)):
                    levels.append(float(lv))
        if levels:
            high = max(levels)
            low = min(levels)
    elif isinstance(payload, dict):
        high = _num(payload.get("max") or payload.get("charged"))
        low = _num(payload.get("min") or payload.get("drained"))
    return {"body_battery_high": high, "body_battery_low": low}


def _from_steps(payload: Any) -> dict[str, float | None]:
    if isinstance(payload, dict):
        return {"steps": _num(payload.get("totalSteps") or payload.get("steps"))}
    if isinstance(payload, list):
        total = 0.0
        for row in payload:
            if isinstance(row, dict):
                s = _num(row.get("steps"))
                if s is not None:
                    total += s
        return {"steps": total if total > 0 else None}
    return {}


def _from_user_summary(payload: Any) -> dict[str, float | None]:
    if not isinstance(payload, dict):
        return {}
    return {
        "active_calories": _num(
            payload.get("activeKilocalories") or payload.get("activeCalories"),
        ),
    }


def _from_spo2(payload: Any) -> dict[str, float | None]:
    if not isinstance(payload, dict):
        return {}
    avg = (
        payload.get("averageSpO2")
        or payload.get("avgSleepSpO2")
        or payload.get("avgSpO2")
    )
    return {"spo2_avg": _num(avg)}


def _from_hrv(payload: Any) -> dict[str, float | None]:
    if not isinstance(payload, dict):
        return {}
    summary = payload.get("hrvSummary") or payload.get("hrvSummaryDTO") or payload
    if not isinstance(summary, dict):
        return {}
    return {
        "hrv_status": _num(
            summary.get("lastNightAvg")
            or summary.get("weeklyAvg")
            or summary.get("avg"),
        ),
    }


def _from_vo2(payload: Any) -> dict[str, float | None]:
    if isinstance(payload, dict):
        v = payload.get("vo2MaxValue") or payload.get("vo2Max")
        if v is None and isinstance(payload.get("generic"), dict):
            v = payload["generic"].get("vo2MaxValue")
        return {"vo2_max": _num(v)}
    if isinstance(payload, (int, float)):
        return {"vo2_max": _num(payload)}
    return {}


def _from_respiration(payload: Any) -> dict[str, float | None]:
    if not isinstance(payload, dict):
        return {}
    v = (
        payload.get("avgWakingRespirationValue")
        or payload.get("avgRespirationValue")
        or payload.get("respirationValue")
    )
    return {"respiration_avg": _num(v)}


def _from_resting_hr(payload: Any) -> dict[str, float | None]:
    if not isinstance(payload, dict):
        return {}
    v = payload.get("restingHeartRate") or payload.get("restingHr")
    if v is None and isinstance(payload.get("allMetrics"), dict):
        try:
            v = payload["allMetrics"]["metricsMap"][
                "WELLNESS_RESTING_HEART_RATE"
            ][0]["value"]
        except (KeyError, IndexError, TypeError):
            v = None
    return {"resting_hr": _num(v)}


def _from_body_composition(payload: Any) -> dict[str, float | None]:
    if not isinstance(payload, dict):
        return {}
    # Garmin Index scale: weight is in grams.
    raw_weight = payload.get("weight") or payload.get("weightInGrams")
    weight = _num(raw_weight)
    if weight is not None and weight > 500:
        # heuristic: anything > 500 must be grams (a 500kg human is impossible)
        weight = round(weight / 1000.0, 4)
    return {
        "weight": weight,
        "body_fat_pct": _num(payload.get("bodyFat") or payload.get("bodyFatPct")),
    }


# ---------------------------------------------------------------------------
# Sync pipeline
# ---------------------------------------------------------------------------


def _measured_at(day: _date) -> str:
    """Canonical timestamp for a daily summary: noon UTC of the date.

    Anchoring at noon (rather than midnight) keeps the row inside the
    intended day in every reasonable timezone, so the dashboard's
    ``measured_at >= today_start`` filters don't shift Garmin rows into
    the wrong day.
    """
    return datetime.combine(day, datetime.min.time(), tzinfo=timezone.utc) \
        .replace(hour=12).isoformat()


def _iter_dates(*, today: _date, days_back: int) -> list[_date]:
    """Returns yesterday going back ``days_back`` days. Never includes today —
    Garmin data is incomplete until the day ends."""
    days_back = max(1, int(days_back))
    end = today - timedelta(days=1)
    start = today - timedelta(days=days_back)
    out: list[_date] = []
    d = start
    while d <= end:
        out.append(d)
        d += timedelta(days=1)
    return out


def _gather_for_day(adapter: gm.GarminAdapter, day: _date) -> dict[str, float]:
    """Pull every endpoint for a single day and merge into a metric→value map.

    Auth errors propagate. Anything else short-circuits to skipping that
    endpoint (already logged by the adapter).
    """
    iso = day.isoformat()
    out: dict[str, float] = {}
    for fetcher, extractor in (
        (adapter.get_sleep_data, _from_sleep),
        (adapter.get_stress_data, _from_stress),
        (adapter.get_body_battery, _from_body_battery),
        (adapter.get_steps_data, _from_steps),
        (adapter.get_user_summary, _from_user_summary),
        (adapter.get_spo2_data, _from_spo2),
        (adapter.get_hrv_data, _from_hrv),
        (adapter.get_vo2_max, _from_vo2),
        (adapter.get_respiration_data, _from_respiration),
        (adapter.get_resting_heart_rate, _from_resting_hr),
        (adapter.get_body_composition, _from_body_composition),
    ):
        payload = fetcher(iso)
        if payload is None:
            continue
        for k, v in extractor(payload).items():
            if v is None:
                continue
            out[k] = float(v)
    return out


def sync_garmin(
    ctx: HealthContext,
    *,
    days_back: int = 1,
    today: _date | None = None,
    adapter: gm.GarminAdapter | None = None,
) -> SyncResult:
    """Pull Garmin daily summaries and insert into the stats table.

    Parameters
    ----------
    ctx:
        Per-user health context (resolved upstream).
    days_back:
        How many days of history to pull, ending yesterday. ``1`` is the
        default daily sync; first-time sync passes ``30``.
    today:
        Override the "today" anchor for tests; defaults to UTC today.
    adapter:
        Inject an already-authenticated adapter (e.g. from a manual
        sync triggered right after a fresh connect). When ``None`` the
        engine loads the persisted tokens and builds a new adapter.
    """
    result = SyncResult()
    today = today or datetime.now(timezone.utc).date()

    if adapter is None:
        try:
            adapter = _load_authenticated_adapter(ctx.db_path)
        except gm.GarminAuthError as exc:
            result.auth_error = True
            result.errors.append(str(exc))
            with health_db.connect(ctx.db_path) as conn:
                gm.mark_token_error(conn, "token_expired")
                conn.commit()
            return result
        except gm.GarminNotInstalled as exc:
            result.errors.append(str(exc))
            return result

    dates = _iter_dates(today=today, days_back=days_back)
    for day in dates:
        result.days_processed += 1
        try:
            metrics = _gather_for_day(adapter, day)
        except gm.GarminAuthError as exc:
            result.auth_error = True
            result.errors.append(f"{day.isoformat()}: {exc}")
            with health_db.connect(ctx.db_path) as conn:
                gm.mark_token_error(conn, "token_expired")
                conn.commit()
            break
        except Exception as exc:  # noqa: BLE001
            result.errored += 1
            result.errors.append(f"{day.isoformat()}: {exc}")
            continue

        if not metrics:
            continue

        measured_at = _measured_at(day)
        with health_db.connect(ctx.db_path) as conn:
            for metric, value in metrics.items():
                unit = _unit_for(metric)
                if health_db.stat_exists_for_source(
                    conn, metric=metric, measured_at=measured_at, source="garmin",
                ):
                    result.skipped += 1
                    continue
                try:
                    health_db.insert_stat(
                        conn,
                        metric=metric, value=value, unit=unit,
                        measured_at=measured_at, source="garmin",
                    )
                    result.inserted += 1
                except sqlite3.Error as exc:
                    result.errored += 1
                    result.errors.append(
                        f"{measured_at} {metric}: {exc}"
                    )
            conn.commit()

    # Stamp last_sync only when at least one endpoint succeeded.
    if result.inserted or result.skipped:
        with health_db.connect(ctx.db_path) as conn:
            gm.update_last_sync(conn)
            conn.commit()

    logger.info(
        "garmin sync: user=%s inserted=%d skipped=%d errored=%d days=%d auth_error=%s",
        ctx.user_id,
        result.inserted, result.skipped, result.errored,
        result.days_processed, result.auth_error,
    )
    return result


def _unit_for(metric: str) -> str:
    from istota.health.models import STAT_METRICS
    return STAT_METRICS.get(metric, "")


def _load_authenticated_adapter(db_path: Path) -> gm.GarminAdapter:
    """Build an adapter and rehydrate it from stored tokens.

    Raises :class:`gm.GarminAuthError` when no tokens are stored — caller
    surfaces this as a "needs reconnect" signal.
    """
    with health_db.connect(db_path) as conn:
        blob = gm.load_tokens(conn)
    if not blob:
        raise gm.GarminAuthError("no Garmin tokens — connect via /garmin/connect")
    adapter = gm._build_adapter()
    # Strip presentation-only fields before handing to the SDK.
    tokens = {
        k: v for k, v in blob.items()
        if k not in ("email", "last_sync")
    }
    adapter.load_tokens(tokens)
    return adapter
