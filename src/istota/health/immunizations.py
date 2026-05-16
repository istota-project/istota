"""Coverage / status computation for immunizations.

Pure function over ``(refs, rows)``. The web, CLI, and history-summary
surfaces all consume :func:`compute_coverage`; the schedule-to-status rule
table lives here and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from istota.health.models import Immunization, ImmunizationRef


# Window before next_due when status flips from up_to_date to due_soon.
_DUE_SOON_WINDOW_DAYS = 30


# Status enum (kept as plain strings so the JSON contract is obvious).
STATUS_UP_TO_DATE = "up_to_date"
STATUS_DUE_SOON = "due_soon"
STATUS_OVERDUE = "overdue"
STATUS_SERIES_INCOMPLETE = "series_incomplete"
STATUS_NEVER_RECORDED = "never_recorded"
STATUS_EXPIRED = "expired"
STATUS_RISK_BASED = "risk_based"


@dataclass
class CoverageEntry:
    name: str
    display_name: str
    category: str
    status: str
    last_given: str | None
    dose_count: int
    next_due: str | None
    is_overdue: bool
    days_until_due: int | None


def _parse_date(s: str | None) -> date | None:
    if not s:
        return None
    # Accept both bare YYYY-MM-DD and ISO timestamps.
    try:
        return date.fromisoformat(s[:10])
    except ValueError:
        try:
            return datetime.fromisoformat(s).date()
        except ValueError:
            return None


def _windowed_status(
    next_due: date, today: date,
) -> tuple[str, bool, int]:
    """Map next_due against today to (status, is_overdue, days_until_due).

    Used by both ``annual`` and ``every_10y`` schedules.
    """
    delta = (next_due - today).days
    if delta < 0:
        return STATUS_OVERDUE, True, delta
    if delta <= _DUE_SOON_WINDOW_DAYS:
        return STATUS_DUE_SOON, False, delta
    return STATUS_UP_TO_DATE, False, delta


def _coverage_for_ref(
    ref: ImmunizationRef,
    rows: list[Immunization],
    today: date,
) -> CoverageEntry:
    matching = [r for r in rows if r.name == ref.name]
    dose_count = len(matching)
    parsed_dates: list[date] = []
    for r in matching:
        d = _parse_date(r.date_given)
        if d is not None:
            parsed_dates.append(d)
    last_given_date = max(parsed_dates) if parsed_dates else None
    last_given_iso = last_given_date.isoformat() if last_given_date else None

    status = STATUS_NEVER_RECORDED
    next_due: date | None = None
    is_overdue = False
    days_until_due: int | None = None

    schedule = ref.schedule

    if schedule == "risk_based":
        # Don't auto-flag risk-based vaccines as overdue/missing.
        status = STATUS_RISK_BASED if dose_count == 0 else STATUS_UP_TO_DATE
    elif schedule == "annual" or schedule == "every_10y":
        if last_given_date is None:
            status = STATUS_NEVER_RECORDED
        else:
            interval = ref.interval_days or (
                365 if schedule == "annual" else 3650
            )
            next_due = last_given_date + timedelta(days=interval)
            status, is_overdue, days_until_due = _windowed_status(
                next_due, today,
            )
    elif schedule == "lifetime_after_series":
        required = ref.primary_series_doses or 1
        if dose_count == 0:
            status = STATUS_SERIES_INCOMPLETE
        elif dose_count >= required:
            status = STATUS_UP_TO_DATE
        else:
            status = STATUS_SERIES_INCOMPLETE
    elif schedule == "series_then_booster":
        # Same as lifetime_after_series until per-vaccine booster rules land
        # in v2 (see spec open questions).
        required = ref.primary_series_doses or 1
        if dose_count == 0:
            status = STATUS_SERIES_INCOMPLETE
        elif dose_count >= required:
            status = STATUS_UP_TO_DATE
        else:
            status = STATUS_SERIES_INCOMPLETE
    elif schedule == "travel_pre_trip":
        if last_given_date is None:
            status = STATUS_NEVER_RECORDED
        else:
            interval = ref.interval_days or 365
            next_due = last_given_date + timedelta(days=interval)
            delta = (next_due - today).days
            days_until_due = delta
            if delta < 0:
                status = STATUS_EXPIRED
                is_overdue = True
            else:
                status = STATUS_UP_TO_DATE
    elif schedule == "one_time":
        status = STATUS_UP_TO_DATE if dose_count > 0 else STATUS_NEVER_RECORDED
    else:
        # Unknown schedule — surface as never_recorded if no dose, else
        # up_to_date. Defensive default; new schedule kinds should be added
        # to the rule list above.
        status = STATUS_UP_TO_DATE if dose_count > 0 else STATUS_NEVER_RECORDED

    return CoverageEntry(
        name=ref.name,
        display_name=ref.display_name,
        category=ref.category,
        status=status,
        last_given=last_given_iso,
        dose_count=dose_count,
        next_due=next_due.isoformat() if next_due else None,
        is_overdue=is_overdue,
        days_until_due=days_until_due,
    )


def compute_coverage(
    refs: list[ImmunizationRef],
    rows: list[Immunization],
    today: date | None = None,
) -> list[CoverageEntry]:
    """Return one CoverageEntry per ref, in the order the refs were given.

    Pure function. Callers that want sorting (overdue first, etc.) sort
    the result themselves.
    """
    today = today or date.today()
    return [_coverage_for_ref(ref, rows, today) for ref in refs]
