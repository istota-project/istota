"""Health tracking skill CLI.

Subcommands cover both body stats and bloodwork:

* ``log <metric> <value>`` — record a stat measurement.
* ``stats [--metric M]`` — list stat entries.
* ``latest`` — latest value per metric.
* ``panels`` / ``panel <id>`` — list/show lab panels.
* ``add-panel`` — create a panel manually.
* ``add-biomarker`` — add a biomarker row to a panel.
* ``trend <name>`` — time-series for a biomarker across confirmed panels.
* ``upload <file>`` — register an uploaded source file (path inside the
  workspace) as a draft panel for later web-UI review.
* ``summary`` — dashboard-style snapshot.
* ``settings`` / ``set KEY VALUE`` — profile + display preferences.

Per-user health DB resolved via ``HEALTH_DB_PATH`` (set by the
``setup_env`` hook below). Writes go through the deferred-op pattern when
``ISTOTA_DEFERRED_DIR`` / ``ISTOTA_TASK_ID`` are set (sandbox mode); the
scheduler applies them post-task. Direct mode is used by the CLI / web
shell when those env vars aren't set.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


_DEFER_FILENAME = "task_{task_id}_health_ops.json"


def setup_env(ctx) -> dict[str, str]:
    """Inject ``HEALTH_DB_PATH`` for the per-user health DB.

    Self-gates on ``Config.is_module_enabled(user_id, "health")`` — when
    the user has opted out the loader raises ``UserNotFoundError`` and we
    return no env contribution, so the CLI can't reach a DB it isn't
    supposed to touch.
    """
    from istota import health as _health  # noqa: PLC0415

    try:
        h = _health.resolve_for_user(ctx.task.user_id, ctx.config)
    except _health.UserNotFoundError:
        return {}
    _health.ensure_initialised(h)
    return {"HEALTH_DB_PATH": str(h.db_path)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _emit(payload: dict, *, error: bool = False) -> None:
    print(json.dumps(payload, default=str))
    if error or payload.get("status") == "error":
        sys.exit(1)


def _fail(msg: str) -> None:
    _emit({"status": "error", "error": msg}, error=True)


def _db_path() -> str:
    db_path = os.environ.get("HEALTH_DB_PATH", "")
    if not db_path:
        _fail("HEALTH_DB_PATH not set — health module disabled or not configured")
    return db_path


def _connect() -> sqlite3.Connection:
    from istota.health import db as health_db

    path = Path(_db_path())
    # In sandbox mode the file is read-only via bwrap, but we still need to
    # *read* it. Writes go through the deferred-op file.
    conn = sqlite3.connect(path, timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def _defer_op(op: dict) -> bool:
    """Append a write op to the per-task deferred file. Returns True when
    deferred; False when no sandbox context is set (caller writes directly).
    """
    deferred_dir = os.environ.get("ISTOTA_DEFERRED_DIR", "")
    task_id = os.environ.get("ISTOTA_TASK_ID", "")
    if not deferred_dir or not task_id:
        return False
    path = Path(deferred_dir) / _DEFER_FILENAME.format(task_id=task_id)
    ops: list[dict] = []
    if path.exists():
        try:
            ops = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            ops = []
    ops.append(op)
    path.write_text(json.dumps(ops))
    return True


# ---------------------------------------------------------------------------
# Stat helpers
# ---------------------------------------------------------------------------


def _coerce_value(metric: str, raw: str, unit: str | None) -> tuple[float, str]:
    """Coerce a value+unit into metric storage units.

    Trivial conversions only — kg/lb, cm/in, °C/°F. Unknown unit pairs
    pass through unchanged so we never silently misrecord a value.
    """
    from istota.health.units import convert_temperature

    try:
        v = float(raw)
    except (TypeError, ValueError):
        _fail(f"value must be numeric, got {raw!r}")

    u = (unit or "").strip()
    if not u:
        # Pick the canonical metric unit when caller didn't say.
        from istota.health.models import STAT_METRICS
        return v, STAT_METRICS.get(metric, "")

    # Imperial → metric for weight.
    if metric == "weight" and u.lower() in ("lb", "lbs", "pound", "pounds"):
        return round(v * 0.45359237, 4), "kg"
    if metric == "weight" and u.lower() in ("kg", "kgs"):
        return v, "kg"
    if metric == "body_temp":
        c = convert_temperature(v, u, "C")
        if c is not None and u.lower() in ("f", "°f"):
            return round(c, 2), "°C"
    return v, u


def _parse_height(raw: str) -> float | None:
    """Parse a height value: ``178``, ``178cm``, ``5ft10in``, ``70in``, ``5'10\"``."""
    import re as _re

    s = raw.strip().lower().replace(" ", "")
    if not s:
        return None
    # plain number → cm
    try:
        return float(s)
    except ValueError:
        pass
    # 5ft10in / 5'10" (try composite before plain ``in`` so feet+inches wins)
    m = _re.match(r"^(\d+)(?:ft|')(\d+(?:\.\d+)?)(?:in|\")?$", s)
    if m:
        feet = float(m.group(1))
        inches = float(m.group(2))
        return round((feet * 12 + inches) * 2.54, 2)
    # NNcm
    if s.endswith("cm"):
        try:
            return float(s[:-2])
        except ValueError:
            return None
    # NNin or NN" → in
    if s.endswith("in"):
        try:
            return round(float(s[:-2]) * 2.54, 2)
        except ValueError:
            return None
    if s.endswith('"'):
        try:
            return round(float(s[:-1]) * 2.54, 2)
        except ValueError:
            return None
    return None


# ---------------------------------------------------------------------------
# Subcommands
# ---------------------------------------------------------------------------


def cmd_log(args: argparse.Namespace) -> None:
    metric = args.metric
    value, unit = _coerce_value(metric, args.value, args.unit)
    op = {
        "op": "insert_stat",
        "metric": metric,
        "value": value,
        "unit": unit,
        "measured_at": args.date,
        "notes": args.notes,
        "source": "manual",
    }
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        sid = health_db.insert_stat(
            conn, metric=metric, value=value, unit=unit,
            measured_at=args.date, source="manual", notes=args.notes,
        )
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "id": sid, "metric": metric, "value": value, "unit": unit})


def cmd_stats(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        rows = health_db.list_stats(
            conn,
            metric=args.metric,
            since=args.since,
            until=args.until,
            limit=args.limit,
        )
    finally:
        conn.close()
    _emit({
        "stats": [
            {
                "id": r.id,
                "metric": r.metric,
                "value": r.value,
                "unit": r.unit,
                "measured_at": r.measured_at,
                "source": r.source,
                "notes": r.notes,
            }
            for r in rows
        ],
    })


def cmd_latest(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        latest = health_db.latest_stats(conn)
    finally:
        conn.close()
    _emit({
        "stats": {
            metric: {
                "value": s.value,
                "unit": s.unit,
                "measured_at": s.measured_at,
                "source": s.source,
            }
            for metric, s in latest.items()
        },
    })


def cmd_panels(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        panels = health_db.list_panels(
            conn, since=args.since, limit=args.limit, include_drafts=True,
        )
        out = []
        for p in panels:
            total, flagged = health_db.panel_counts(conn, p.id)
            out.append({
                "id": p.id,
                "drawn_at": p.drawn_at,
                "lab_name": p.lab_name,
                "panel_type": p.panel_type,
                "biomarker_count": total,
                "flagged_count": flagged,
                "draft": p.draft,
            })
    finally:
        conn.close()
    _emit({"panels": out})


def cmd_panel(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        panel = health_db.get_panel(conn, args.id)
        if not panel:
            _fail(f"panel {args.id} not found")
        biomarkers = health_db.list_biomarkers_for_panel(conn, args.id)
    finally:
        conn.close()
    _emit({
        "panel": {
            "id": panel.id,
            "drawn_at": panel.drawn_at,
            "lab_name": panel.lab_name,
            "panel_type": panel.panel_type,
            "draft": panel.draft,
            "notes": panel.notes,
        },
        "biomarkers": [
            {
                "id": b.id, "name": b.name, "display_name": b.display_name,
                "value": b.value, "unit": b.unit,
                "ref_range_low": b.ref_range_low,
                "ref_range_high": b.ref_range_high,
                "flag": b.flag,
            }
            for b in biomarkers
        ],
    })


def cmd_add_panel(args: argparse.Namespace) -> None:
    op = {
        "op": "insert_panel",
        "drawn_at": args.drawn_at,
        "lab_name": args.lab,
        "panel_type": args.type,
        "notes": args.notes,
    }
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        pid = health_db.insert_panel(
            conn,
            drawn_at=args.drawn_at, lab_name=args.lab,
            panel_type=args.type, notes=args.notes,
        )
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "id": pid})


def cmd_add_biomarker(args: argparse.Namespace) -> None:
    op = {
        "op": "insert_biomarker",
        "panel_id": args.panel_id,
        "name": args.name,
        "value": float(args.value),
        "unit": args.unit,
        "ref_range_low": args.ref_low,
        "ref_range_high": args.ref_high,
        "flag": args.flag,
    }
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        bid = health_db.insert_biomarker(
            conn,
            panel_id=args.panel_id,
            name=args.name,
            value=float(args.value),
            unit=args.unit,
            ref_range_low=args.ref_low,
            ref_range_high=args.ref_high,
            flag=args.flag,
        )
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "id": bid})


def cmd_trend(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        ref = health_db.find_biomarker_ref_by_alias(conn, args.name)
        canonical = ref.name if ref else args.name
        trend = health_db.biomarker_trend(
            conn, name=canonical, since=args.since, until=args.until,
        )
    finally:
        conn.close()
    _emit({
        "name": canonical,
        "points": [
            {
                "drawn_at": d, "value": b.value, "unit": b.unit, "flag": b.flag,
            }
            for b, d in trend
        ],
    })


def cmd_upload(args: argparse.Namespace) -> None:
    """Register an existing file path as a draft panel source.

    The sandboxed Claude process can't move files into the uploads
    directory directly, so callers point at a workspace-relative path and
    the scheduler relocates it during deferred processing.
    """
    path = Path(args.file_path)
    if not path.exists():
        _fail(f"file not found: {path}")
    op = {
        "op": "register_upload",
        "drawn_at": args.drawn_at,
        "lab_name": args.lab,
        "source_path": str(path),
    }
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    # Direct mode is used only by the operator CLI / tests — we record the
    # panel but leave the file in place; the web upload route is the real
    # write path.
    from istota.health import db as health_db

    conn = _connect()
    try:
        pid = health_db.insert_panel(
            conn,
            drawn_at=args.drawn_at,
            lab_name=args.lab,
            source_file=str(path),
            draft=True,
        )
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "id": pid, "draft": True})


def cmd_import_csv(args: argparse.Namespace) -> None:
    """Import a bloodwork CSV from a workspace-accessible file path.

    In sandbox mode the read + parse happens here (CLI is allowed to read
    files), but the writes are deferred so the scheduler applies them
    against the per-user health DB outside the sandbox.
    """
    path = Path(args.file_path)
    if not path.exists():
        _fail(f"file not found: {path}")
    op = {
        "op": "import_csv",
        "source_path": str(path),
    }
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return

    from istota.health import csv_io
    from istota.health import db as health_db

    csv_text = path.read_text(encoding="utf-8-sig", errors="replace")
    conn = _connect()
    try:
        summary = csv_io.import_csv(conn, csv_text)
        conn.commit()
    finally:
        conn.close()
    _emit({
        "status": "ok",
        "panels_created": summary.panels_created,
        "panels_skipped_identical": summary.panels_skipped_identical,
        "panels_needs_review": summary.panels_needs_review,
        "biomarkers_created": summary.biomarkers_created,
        "rows_processed": summary.rows_processed,
        "warnings": summary.warnings,
    })


def cmd_export_csv(args: argparse.Namespace) -> None:
    """Export every confirmed panel as CSV. Read-only — never deferred."""
    from istota.health import csv_io

    conn = _connect()
    try:
        text = csv_io.export_csv(conn)
    finally:
        conn.close()
    if args.output:
        Path(args.output).write_text(text, encoding="utf-8")
        _emit({"status": "ok", "path": args.output, "bytes": len(text)})
        return
    # No path: print the CSV directly so it can be piped.
    sys.stdout.write(text)


def cmd_summary(args: argparse.Namespace) -> None:
    from istota.health import db as health_db
    from istota.health.units import compute_bmi

    conn = _connect()
    try:
        latest = health_db.latest_stats(conn)
        panels = health_db.list_panels(conn, include_drafts=False, limit=3)
        alerts_rows = health_db.flagged_biomarkers_latest(conn, limit=20)
        settings = health_db.get_settings(conn)
    finally:
        conn.close()
    bmi = None
    weight = latest.get("weight")
    height_cm = settings.get("height_cm")
    if weight and height_cm:
        try:
            bmi = compute_bmi(weight.value, float(height_cm))
        except (TypeError, ValueError):
            bmi = None
    _emit({
        "latest_stats": {
            m: {"value": s.value, "unit": s.unit, "measured_at": s.measured_at}
            for m, s in latest.items()
        },
        "bmi": bmi,
        "recent_panels": [
            {"id": p.id, "drawn_at": p.drawn_at, "lab_name": p.lab_name}
            for p in panels
        ],
        "alerts": [
            {
                "name": b.name, "value": b.value, "unit": b.unit, "flag": b.flag,
                "panel_id": p.id, "drawn_at": p.drawn_at, "lab_name": p.lab_name,
            }
            for b, p in alerts_rows
        ],
    })


def cmd_garmin_status(args: argparse.Namespace) -> None:
    from istota.health import garmin as gm

    conn = _connect()
    try:
        status = gm.get_status(conn)
    finally:
        conn.close()
    _emit({"status": "ok", "garmin": status})


def cmd_garmin_sync(args: argparse.Namespace) -> None:
    """Run a Garmin sync. Direct mode only — sync writes to multiple stats
    rows and is not part of the deferred-op set; the scheduled job and
    web `/garmin/sync` endpoint cover the unsandboxed surfaces."""
    from istota.health import garmin_sync
    from istota.health._loader import resolve_for_user

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    if not user_id:
        _fail("ISTOTA_USER_ID not set")

    # Resolve the per-user context. The CLI may be invoked outside the
    # sandbox (operator / scheduled job), so we can call the host-side
    # loader here without config wiring.
    try:
        # Lazy: try to import the config from a known path; if not
        # available, fall back to assembling a minimal context from
        # HEALTH_DB_PATH (used by the in-sandbox CLI surface).
        from istota.health.models import HealthContext
        from pathlib import Path as _P
        db = _P(_db_path())
        ctx = HealthContext(
            user_id=user_id,
            workspace_root=db.parent.parent,
            data_dir=db.parent.parent,
            db_path=db,
            uploads_dir=db.parent.parent / "uploads",
        )
    except Exception as exc:  # noqa: BLE001
        _fail(f"failed to build health context: {exc}")

    res = garmin_sync.sync_garmin(ctx, days_back=args.days_back)
    payload = res.to_dict()
    payload["status"] = "ok" if not res.auth_error else "error"
    if res.auth_error:
        payload["error"] = "token_expired"
    _emit(payload, error=res.auth_error)


def cmd_garmin_disconnect(args: argparse.Namespace) -> None:
    from istota.health import garmin as gm

    user_id = os.environ.get("ISTOTA_USER_ID", "")
    conn = _connect()
    try:
        gm.disconnect(conn, user_id=user_id)
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok"})


def cmd_settings(args: argparse.Namespace) -> None:
    from istota.health import db as health_db

    conn = _connect()
    try:
        s = health_db.get_settings(conn)
    finally:
        conn.close()
    _emit({"settings": s})


def cmd_set(args: argparse.Namespace) -> None:
    """``health set <key> <value>``.

    Whitelisted keys: ``dob``, ``height``, ``sex``, plus dotted
    ``display.weight``, ``display.height``, ``display.temp``.
    """
    key = args.key
    raw = args.value

    if key == "dob":
        op_val: Any = raw
    elif key == "height":
        h = _parse_height(raw)
        if h is None:
            _fail(f"could not parse height: {raw!r}")
        key = "height_cm"
        op_val = h
    elif key == "sex":
        if raw.upper() not in ("M", "F"):
            _fail("sex must be 'M' or 'F'")
        op_val = raw.upper()
    elif key.startswith("display."):
        dim = key.split(".", 1)[1]
        if dim not in ("weight", "height", "temp"):
            _fail(f"unknown display dimension {dim!r}")
        op_val = {dim: raw}
        key = "display_units_merge"
    else:
        _fail(f"unknown settings key {key!r}")

    op = {"op": "set_setting", "key": key, "value": op_val}
    if _defer_op(op):
        _emit({"status": "ok", "deferred": True, "op": op})
        return
    from istota.health import db as health_db

    conn = _connect()
    try:
        if key == "display_units_merge":
            existing = health_db.get_settings(conn).get("display_units") or {}
            existing.update(op_val)
            health_db.set_setting(conn, "display_units", existing)
        else:
            health_db.set_setting(conn, key, op_val)
        conn.commit()
    finally:
        conn.close()
    _emit({"status": "ok", "key": key, "value": op_val})


# ---------------------------------------------------------------------------
# argparse wiring
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Health tracking CLI")
    sub = parser.add_subparsers(dest="command")

    log = sub.add_parser("log", help="Record a stat measurement")
    log.add_argument("metric", help="Metric key (weight, resting_hr, body_fat_pct, …)")
    log.add_argument("value", help="Numeric value")
    log.add_argument("--unit", default=None, help="Source unit; default = canonical metric unit")
    log.add_argument("--date", default=None, help="ISO 8601 timestamp (default: now)")
    log.add_argument("--notes", default=None)

    stats = sub.add_parser("stats", help="List stat entries")
    stats.add_argument("--metric")
    stats.add_argument("--since")
    stats.add_argument("--until")
    stats.add_argument("--limit", type=int, default=100)

    sub.add_parser("latest", help="Latest value per metric")

    panels = sub.add_parser("panels", help="List bloodwork panels")
    panels.add_argument("--since")
    panels.add_argument("--limit", type=int, default=20)

    panel = sub.add_parser("panel", help="Show a single panel with biomarkers")
    panel.add_argument("id", type=int)

    add_panel = sub.add_parser("add-panel", help="Create a panel manually")
    add_panel.add_argument("--drawn-at", dest="drawn_at", required=True)
    add_panel.add_argument("--lab")
    add_panel.add_argument("--type", dest="type")
    add_panel.add_argument("--notes")

    add_bio = sub.add_parser("add-biomarker", help="Add a biomarker to a panel")
    add_bio.add_argument("panel_id", type=int)
    add_bio.add_argument("name")
    add_bio.add_argument("value")
    add_bio.add_argument("unit")
    add_bio.add_argument("--ref-low", dest="ref_low", type=float)
    add_bio.add_argument("--ref-high", dest="ref_high", type=float)
    add_bio.add_argument("--flag", choices=["H", "L", "C"])

    trend = sub.add_parser("trend", help="Time series for a biomarker")
    trend.add_argument("name")
    trend.add_argument("--since")
    trend.add_argument("--until")

    upload = sub.add_parser("upload", help="Register a file as a draft panel source")
    upload.add_argument("file_path")
    upload.add_argument("--drawn-at", dest="drawn_at", required=True)
    upload.add_argument("--lab")

    import_csv = sub.add_parser(
        "import-csv",
        help="Import a bloodwork CSV (Date,Lab,Marker (unit) layout)",
    )
    import_csv.add_argument("file_path")

    export_csv = sub.add_parser(
        "export-csv",
        help="Export confirmed panels as a CSV (prints to stdout if no path)",
    )
    export_csv.add_argument("--output", "-o", default=None)

    sub.add_parser("summary", help="Dashboard-style snapshot")

    sub.add_parser("settings", help="Show current settings")

    set_p = sub.add_parser("set", help="Update a setting")
    set_p.add_argument("key", help="dob | height | sex | display.weight | display.height | display.temp")
    set_p.add_argument("value")

    sub.add_parser("garmin-status", help="Show Garmin Connect link status")
    g_sync = sub.add_parser("garmin-sync", help="Manually trigger a Garmin daily-summary sync")
    g_sync.add_argument("--days-back", dest="days_back", type=int, default=7)
    sub.add_parser("garmin-disconnect", help="Remove stored Garmin tokens")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    commands = {
        "log": cmd_log,
        "stats": cmd_stats,
        "latest": cmd_latest,
        "panels": cmd_panels,
        "panel": cmd_panel,
        "add-panel": cmd_add_panel,
        "add-biomarker": cmd_add_biomarker,
        "trend": cmd_trend,
        "upload": cmd_upload,
        "import-csv": cmd_import_csv,
        "export-csv": cmd_export_csv,
        "summary": cmd_summary,
        "settings": cmd_settings,
        "set": cmd_set,
        "garmin-status": cmd_garmin_status,
        "garmin-sync": cmd_garmin_sync,
        "garmin-disconnect": cmd_garmin_disconnect,
    }

    fn = commands.get(args.command)
    if fn is None:
        parser.print_help()
        sys.exit(1)
    fn(args)
