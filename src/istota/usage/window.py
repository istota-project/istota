"""Date windows shared by the operator and task usage CLIs."""


def usage_window(args):
    """Resolve the CLI's date arguments into one window, in both formats.

    Returns `(since_iso, until_iso, since_sql, until_sql)`. Both formats come
    back together because the window has two readers whose tables store dates
    differently — `task_usage` in ISO-Z, `tasks` in `datetime('now')` — and
    deriving one and forgetting the other is what makes the unmeasured-task
    counter describe a different window than the table above it.

    A bare `--until D` is expanded to D+1 at midnight. Without that,
    `--since 2026-08-01 --until 2026-08-20` silently loses the whole of 20 Aug,
    which is the kind of wrong number nobody notices.

    Raises `ValueError` on an unparseable or inverted window, so the caller can
    refuse rather than print an empty table that looks like an answer.
    """
    from datetime import datetime, timedelta, timezone

    def _iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"

    def _sql(dt):
        return dt.strftime("%Y-%m-%d %H:%M:%S")

    def _parse_day(value):
        return datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    if args.since:
        since_dt = _parse_day(args.since)
    else:
        if args.days < 1:
            # `--days 0` reads as "no limit" and does the opposite: it puts the
            # bound at now and reports nothing. A negative one puts it in the
            # future. Both print a table that looks like an answer.
            raise ValueError("--days must be at least 1")
        since_dt = datetime.now(timezone.utc) - timedelta(days=args.days)

    until_dt = _parse_day(args.until) + timedelta(days=1) if args.until else None
    if until_dt is not None and until_dt <= since_dt:
        raise ValueError("--until must be after --since")

    return (
        _iso(since_dt),
        _iso(until_dt) if until_dt else None,
        _sql(since_dt),
        _sql(until_dt) if until_dt else None,
    )


def unmeasured_window(since_iso, since_sql, until_sql, retention_days, now=None):
    """The part of a window the unmeasured-task counter can actually see.

    Returns `(since_sql, since_iso, covered)`. The counter reads `tasks`, which
    `cleanup_old_tasks` empties after `task_retention_days`, while the usage
    table beside it keeps 180 days; a bound older than the retention floor is
    raised to it (ISSUE-680). `covered` is False when the whole window ends
    before the floor, where no retained task can answer and the count is
    unknown rather than zero.
    """
    from datetime import datetime, timedelta, timezone

    if retention_days < 0:
        # `cleanup_old_tasks` builds `'--N days'`, which SQLite reads as NULL,
        # so a negative value deletes nothing and there is no floor.
        return since_sql, since_iso, True
    now = now or datetime.now(timezone.utc)
    floor = (now - timedelta(days=retention_days)).strftime("%Y-%m-%d %H:%M:%S")
    if since_sql >= floor:
        effective, effective_iso = since_sql, since_iso
    else:
        effective, effective_iso = floor, floor.replace(" ", "T") + ".000Z"
    covered = until_sql is None or effective < until_sql
    return effective, effective_iso, covered
