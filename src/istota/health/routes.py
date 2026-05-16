"""FastAPI router for the health module.

Mounted by the host application at ``/istota/api/health``. Reads/writes the
per-user workspace SQLite. Auth, CSRF, and per-user resolution mirror
:mod:`istota.feeds.routes`: the host overrides ``require_auth`` and
``verify_origin`` via ``app.dependency_overrides`` and the istota config is
read off ``request.app.state.istota_config``.
"""

from __future__ import annotations

import asyncio
import mimetypes
import re
import shutil
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi import File as FastAPIFile
from fastapi import Form
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse

from istota.health import db as health_db
from istota.health import garmin as health_garmin
from istota.health import garmin_sync as health_garmin_sync
from istota.health._loader import UserNotFoundError, resolve_for_user
from istota.health._migrate import ensure_initialised
from istota.health.models import HealthContext
from istota.health.units import (
    all_units_agree,
    compute_bmi,
    compute_flag,
    pick_canonical_range,
    widest_canonical_range,
)


# ---------------------------------------------------------------------------
# Auth / CSRF — host app overrides via dependency_overrides
# ---------------------------------------------------------------------------


def require_auth(request: Request) -> dict:
    user = None
    try:
        user = request.session.get("user")
    except (AssertionError, AttributeError):
        pass
    if not user:
        raise HTTPException(401, "unauthorized")
    return user


def verify_origin(request: Request) -> None:
    return None


def get_user_context(
    request: Request,
    user: dict = Depends(require_auth),
) -> HealthContext:
    istota_config = getattr(request.app.state, "istota_config", None)
    try:
        ctx = resolve_for_user(user["username"], istota_config)
    except UserNotFoundError as e:
        raise HTTPException(404, str(e))
    cache: set = getattr(request.app.state, "health_initialised_dbs", None)
    if cache is None:
        cache = set()
        request.app.state.health_initialised_dbs = cache
    if ctx.db_path not in cache:
        ensure_initialised(ctx)
        cache.add(ctx.db_path)
    else:
        ctx.ensure_dirs()
    return ctx


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _stat_to_dict(s) -> dict:
    return {
        "id": s.id,
        "measured_at": s.measured_at,
        "metric": s.metric,
        "value": s.value,
        "unit": s.unit,
        "source": s.source,
        "source_ref": s.source_ref,
        "notes": s.notes or "",
    }


def _panel_to_dict(p, *, biomarker_count: int = 0, flagged_count: int = 0) -> dict:
    return {
        "id": p.id,
        "drawn_at": p.drawn_at,
        "lab_name": p.lab_name,
        "panel_type": p.panel_type,
        "biomarker_count": biomarker_count,
        "flagged_count": flagged_count,
        "draft": p.draft,
        "notes": p.notes,
        "has_source": bool(p.source_file),
        "encounter_id": p.encounter_id,
    }


def _encounter_to_dict(e) -> dict:
    return {
        "id": e.id,
        "encounter_date": e.encounter_date,
        "encounter_type": e.encounter_type,
        "provider": e.provider,
        "facility": e.facility,
        "specialty": e.specialty,
        "reason": e.reason,
        "notes": e.notes,
        "created_at": e.created_at,
    }


def _diagnosis_to_dict(d) -> dict:
    return {
        "id": d.id,
        "name": d.name,
        "icd10": d.icd10,
        "status": d.status,
        "date_diagnosed": d.date_diagnosed,
        "date_resolved": d.date_resolved,
        "encounter_id": d.encounter_id,
        "severity": d.severity,
        "notes": d.notes,
        "created_at": d.created_at,
    }


_ENCOUNTER_TYPES = {
    "visit", "procedure", "screening", "hospitalization", "er",
    "telehealth", "imaging", "dental", "other",
}

_DIAGNOSIS_STATUSES = {"active", "resolved", "chronic"}


def _biomarker_to_dict(b) -> dict:
    return {
        "id": b.id,
        "panel_id": b.panel_id,
        "name": b.name,
        "display_name": b.display_name,
        "value": b.value,
        "unit": b.unit,
        "ref_range_low": b.ref_range_low,
        "ref_range_high": b.ref_range_high,
        "flag": b.flag,
    }


_VALID_METRIC = re.compile(r"^[a-z][a-z0-9_]{0,63}$")


def _validate_metric(m: str) -> str | None:
    if not isinstance(m, str) or not _VALID_METRIC.match(m):
        return "metric must be a lowercase identifier (snake_case)"
    return None


def _settings_with_defaults(stored: dict) -> dict:
    display = stored.get("display_units") or {}
    return {
        "dob": stored.get("dob"),
        "height_cm": stored.get("height_cm"),
        "sex": stored.get("sex"),
        "display_units": {
            "weight": display.get("weight", "kg"),
            "height": display.get("height", "cm"),
            "temp": display.get("temp", "C"),
        },
    }


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


router = APIRouter()


# ---- Stats ----------------------------------------------------------------


@router.get("/stats")
async def api_list_stats(
    ctx: HealthContext = Depends(get_user_context),
    metric: str = Query(default=""),
    since: str = Query(default=""),
    until: str = Query(default=""),
    limit: int = Query(default=200, le=1000, ge=1),
    offset: int = Query(default=0, ge=0),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            return health_db.list_stats(
                conn,
                metric=metric or None,
                since=since or None,
                until=until or None,
                limit=limit,
                offset=offset,
            )

    rows = await asyncio.to_thread(_query)
    return {"stats": [_stat_to_dict(s) for s in rows]}


@router.post("/stats")
async def api_create_stat(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    metric = body.get("metric")
    err = _validate_metric(metric or "")
    if err:
        return JSONResponse({"error": err}, status_code=400)
    try:
        value = float(body["value"])
    except (KeyError, TypeError, ValueError):
        return JSONResponse({"error": "value must be a number"}, status_code=400)
    unit = body.get("unit")
    if not isinstance(unit, str) or not unit.strip():
        return JSONResponse({"error": "unit is required"}, status_code=400)

    measured_at = body.get("measured_at") or _now()
    source = body.get("source") or "manual"
    notes = body.get("notes")

    def _insert():
        with health_db.connect(ctx.db_path) as conn:
            sid = health_db.insert_stat(
                conn,
                metric=metric,
                value=value,
                unit=unit,
                measured_at=measured_at,
                source=source,
                notes=notes,
            )
            conn.commit()
        return sid

    sid = await asyncio.to_thread(_insert)
    return {"status": "ok", "id": sid}


@router.delete("/stats/{stat_id}")
async def api_delete_stat(
    stat_id: int,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    def _delete():
        with health_db.connect(ctx.db_path) as conn:
            n = health_db.delete_stat(conn, stat_id)
            conn.commit()
        return n

    n = await asyncio.to_thread(_delete)
    if not n:
        raise HTTPException(404, "stat not found")
    return {"status": "ok"}


@router.get("/stats/latest")
async def api_stats_latest(ctx: HealthContext = Depends(get_user_context)):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            return health_db.latest_stats(conn)

    latest = await asyncio.to_thread(_query)
    return {
        "stats": {metric: _stat_to_dict(s) for metric, s in latest.items()},
    }


@router.get("/stats/series")
async def api_stats_series(
    ctx: HealthContext = Depends(get_user_context),
    metric: str = Query(...),
    since: str = Query(default=""),
    until: str = Query(default=""),
):
    err = _validate_metric(metric)
    if err:
        return JSONResponse({"error": err}, status_code=400)

    def _query():
        with health_db.connect(ctx.db_path) as conn:
            return health_db.list_stats(
                conn,
                metric=metric,
                since=since or None,
                until=until or None,
                limit=5000,
            )

    rows = await asyncio.to_thread(_query)
    rows_sorted = sorted(rows, key=lambda r: r.measured_at)
    return {
        "metric": metric,
        "points": [
            {"measured_at": r.measured_at, "value": r.value, "unit": r.unit}
            for r in rows_sorted
        ],
    }


# ---- Panels ---------------------------------------------------------------


@router.get("/panels")
async def api_list_panels(
    ctx: HealthContext = Depends(get_user_context),
    since: str = Query(default=""),
    until: str = Query(default=""),
    include_drafts: int = Query(default=1, ge=0, le=1),
    limit: int = Query(default=50, le=500, ge=1),
    offset: int = Query(default=0, ge=0),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            panels = health_db.list_panels(
                conn,
                since=since or None,
                until=until or None,
                include_drafts=bool(include_drafts),
                limit=limit,
                offset=offset,
            )
            out = []
            for p in panels:
                total, flagged = health_db.panel_counts(conn, p.id)
                out.append(_panel_to_dict(
                    p, biomarker_count=total, flagged_count=flagged,
                ))
            return out

    panels = await asyncio.to_thread(_query)
    return {"panels": panels}


@router.post("/panels")
async def api_create_panel(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    drawn_at = body.get("drawn_at")
    if not isinstance(drawn_at, str) or not drawn_at.strip():
        return JSONResponse({"error": "drawn_at is required"}, status_code=400)
    lab_name = body.get("lab_name") or None
    panel_type = body.get("panel_type") or None
    notes = body.get("notes")
    encounter_id = body.get("encounter_id")
    if encounter_id is not None:
        try:
            encounter_id = int(encounter_id)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "encounter_id must be an integer"}, status_code=400,
            )

    def _insert():
        with health_db.connect(ctx.db_path) as conn:
            if encounter_id is not None and health_db.get_encounter(
                conn, encounter_id,
            ) is None:
                return None, "encounter not found"
            collision = health_db.find_panel_collision(
                conn, drawn_at=drawn_at, lab_name=lab_name,
            )
            pid = health_db.insert_panel(
                conn,
                drawn_at=drawn_at,
                lab_name=lab_name,
                panel_type=panel_type,
                notes=notes,
                encounter_id=encounter_id,
            )
            conn.commit()
        return pid, collision

    pid, collision = await asyncio.to_thread(_insert)
    if pid is None and isinstance(collision, str):
        return JSONResponse({"error": collision}, status_code=400)
    payload = {"status": "ok", "id": pid}
    if collision is not None:
        payload["collision"] = {
            "existing_id": collision.id,
            "drawn_at": collision.drawn_at,
            "lab_name": collision.lab_name,
        }
    return payload


@router.get("/panels/{panel_id}")
async def api_get_panel(
    panel_id: int,
    ctx: HealthContext = Depends(get_user_context),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            panel = health_db.get_panel(conn, panel_id)
            if not panel:
                return None
            biomarkers = health_db.list_biomarkers_for_panel(conn, panel_id)
            total, flagged = health_db.panel_counts(conn, panel_id)
            return panel, biomarkers, total, flagged

    result = await asyncio.to_thread(_query)
    if result is None:
        raise HTTPException(404, "panel not found")
    panel, biomarkers, total, flagged = result
    return {
        "panel": _panel_to_dict(
            panel, biomarker_count=total, flagged_count=flagged,
        ),
        "biomarkers": [_biomarker_to_dict(b) for b in biomarkers],
        "source": {
            "available": bool(panel.source_file),
            "mime": panel.source_mime,
        },
    }


@router.put("/panels/{panel_id}")
async def api_update_panel(
    panel_id: int,
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    drawn_at = body.get("drawn_at")
    lab_name = body.get("lab_name")
    panel_type = body.get("panel_type")
    notes = body.get("notes")
    draft = body.get("draft")
    if draft is not None and not isinstance(draft, bool):
        return JSONResponse(
            {"error": "draft must be a boolean"}, status_code=400,
        )
    has_encounter_id = "encounter_id" in body
    encounter_id = body.get("encounter_id")
    if has_encounter_id and encounter_id is not None:
        try:
            encounter_id = int(encounter_id)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "encounter_id must be an integer or null"},
                status_code=400,
            )

    def _update():
        with health_db.connect(ctx.db_path) as conn:
            if (
                has_encounter_id
                and encounter_id is not None
                and health_db.get_encounter(conn, encounter_id) is None
            ):
                return "encounter_not_found"
            kwargs: dict = {
                "drawn_at": drawn_at,
                "lab_name": lab_name,
                "panel_type": panel_type,
                "notes": notes,
                "draft": draft,
            }
            if has_encounter_id:
                kwargs["encounter_id"] = encounter_id
            n = health_db.update_panel(conn, panel_id, **kwargs)
            conn.commit()
        return n

    n = await asyncio.to_thread(_update)
    if n == "encounter_not_found":
        return JSONResponse({"error": "encounter not found"}, status_code=400)
    if not n:
        raise HTTPException(404, "panel not found")
    return {"status": "ok"}


@router.delete("/panels/{panel_id}")
async def api_delete_panel(
    panel_id: int,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    panel_dir = ctx.uploads_dir / str(panel_id)

    def _delete():
        with health_db.connect(ctx.db_path) as conn:
            panel = health_db.get_panel(conn, panel_id)
            if not panel:
                return False
            # Drop derived stats first (source='lab_panel', source_ref=panel_id).
            health_db.delete_stats_for_panel(conn, panel_id)
            health_db.delete_panel(conn, panel_id)  # CASCADE -> biomarkers
            conn.commit()
        # On-disk uploads — best effort.
        if panel_dir.exists():
            try:
                shutil.rmtree(panel_dir)
            except OSError:
                pass
        return True

    ok = await asyncio.to_thread(_delete)
    if not ok:
        raise HTTPException(404, "panel not found")
    return {"status": "ok"}


@router.post("/panels/{panel_id}/biomarkers")
async def api_replace_biomarkers(
    panel_id: int,
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    biomarkers = body.get("biomarkers")
    if not isinstance(biomarkers, list):
        return JSONResponse(
            {"error": "biomarkers must be a list"}, status_code=400,
        )
    for b in biomarkers:
        if not isinstance(b, dict):
            return JSONResponse(
                {"error": "each biomarker must be an object"}, status_code=400,
            )
        if "name" not in b or "value" not in b or "unit" not in b:
            return JSONResponse(
                {"error": "name, value, unit are required"}, status_code=400,
            )

    confirm = bool(body.get("confirm"))

    def _save():
        with health_db.connect(ctx.db_path) as conn:
            panel = health_db.get_panel(conn, panel_id)
            if not panel:
                return None
            # Auto-fill ranges + flags from canonical refs where missing.
            settings = health_db.get_settings(conn)
            sex = settings.get("sex")
            enriched: list[dict] = []
            for b in biomarkers:
                ref = health_db.find_biomarker_ref_by_alias(conn, str(b["name"]))
                low = b.get("ref_range_low")
                high = b.get("ref_range_high")
                canonical_low = canonical_high = None
                if ref is not None:
                    canonical_low, canonical_high = pick_canonical_range(ref, sex)
                # Flag against canonical ranges (preferred) when available,
                # falling back to lab-printed range. ``C`` from the lab is
                # preserved.
                flag_low = canonical_low if canonical_low is not None else low
                flag_high = canonical_high if canonical_high is not None else high
                computed_flag = compute_flag(
                    float(b["value"]),
                    low=flag_low,
                    high=flag_high,
                    lab_flag=b.get("flag"),
                )
                enriched.append({
                    "name": ref.name if ref else str(b["name"]),
                    "display_name": b.get("display_name") or (
                        ref.display_name if ref else None
                    ),
                    "value": float(b["value"]),
                    "unit": str(b["unit"]),
                    "ref_range_low": low,
                    "ref_range_high": high,
                    "flag": computed_flag,
                })
            n = health_db.replace_biomarkers(conn, panel_id, enriched)
            # BP / resting-HR fan-out: also write stats rows so the
            # unified time series picks them up.
            _stat_fanout = {
                "blood_pressure_systolic": ("BP_Systolic", "mmHg"),
                "blood_pressure_diastolic": ("BP_Diastolic", "mmHg"),
                "resting_hr": ("Resting_HR", "bpm"),
            }
            # Clear previous fan-out for this panel before re-creating.
            health_db.delete_stats_for_panel(conn, panel_id)
            name_to_metric = {v[0].lower(): (k, v[1]) for k, v in _stat_fanout.items()}
            for b in enriched:
                hit = name_to_metric.get(b["name"].lower())
                if not hit:
                    continue
                metric_key, default_unit = hit
                health_db.insert_stat(
                    conn,
                    metric=metric_key,
                    value=b["value"],
                    unit=b["unit"] or default_unit,
                    measured_at=panel.drawn_at,
                    source="lab_panel",
                    source_ref=panel_id,
                )
            if confirm:
                health_db.update_panel(conn, panel_id, draft=False)
            conn.commit()
        return n

    n = await asyncio.to_thread(_save)
    if n is None:
        raise HTTPException(404, "panel not found")
    return {"status": "ok", "count": n}


@router.get("/panels/{panel_id}/biomarkers")
async def api_list_panel_biomarkers(
    panel_id: int,
    ctx: HealthContext = Depends(get_user_context),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            panel = health_db.get_panel(conn, panel_id)
            if not panel:
                return None
            return health_db.list_biomarkers_for_panel(conn, panel_id)

    rows = await asyncio.to_thread(_query)
    if rows is None:
        raise HTTPException(404, "panel not found")
    return {"biomarkers": [_biomarker_to_dict(b) for b in rows]}


@router.post("/panels/upload")
async def api_panel_upload(
    request: Request,
    file: UploadFile = FastAPIFile(...),
    drawn_at: str = Form(""),
    lab_name: str = Form(""),
    panel_type: str = Form(""),
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    """Upload a lab result image/PDF.

    Creates a panel row with ``draft=1`` and saves the source file to
    ``{uploads_dir}/{panel_id}/original.{ext}``. The OCR + LLM extraction
    is triggered asynchronously via the ``run_ocr`` flag returned to the
    client; the frontend POSTs to ``/panels/{id}/extract`` next.

    Returns the new panel id and a collision-info object when a panel with
    the same ``(drawn_at, lab_name)`` already exists.
    """
    if not drawn_at:
        drawn_at = datetime.now(timezone.utc).date().isoformat()

    raw = await file.read()
    if not raw:
        return JSONResponse({"error": "empty upload"}, status_code=400)

    mime = file.content_type or mimetypes.guess_type(file.filename or "")[0] or "application/octet-stream"
    suffix = Path(file.filename or "").suffix or mimetypes.guess_extension(mime) or ""

    def _save_and_record():
        with health_db.connect(ctx.db_path) as conn:
            collision = health_db.find_panel_collision(
                conn, drawn_at=drawn_at, lab_name=lab_name or None,
            )
            pid = health_db.insert_panel(
                conn,
                drawn_at=drawn_at,
                lab_name=lab_name or None,
                panel_type=panel_type or None,
                source_mime=mime,
                draft=True,
            )
            conn.commit()
        panel_dir = ctx.uploads_dir / str(pid)
        panel_dir.mkdir(parents=True, exist_ok=True)
        target = panel_dir / f"original{suffix}"
        target.write_bytes(raw)
        rel = str(target.relative_to(ctx.uploads_dir))
        with health_db.connect(ctx.db_path) as conn:
            health_db.update_panel(conn, pid, notes=None)  # placeholder for future
            conn.execute(
                "UPDATE panels SET source_file = ? WHERE id = ?",
                (rel, pid),
            )
            conn.commit()
        return pid, collision

    pid, collision = await asyncio.to_thread(_save_and_record)
    out = {"status": "ok", "id": pid, "draft": True}
    if collision is not None:
        out["collision"] = {
            "existing_id": collision.id,
            "drawn_at": collision.drawn_at,
            "lab_name": collision.lab_name,
        }
    return out


@router.post("/panels/{panel_id}/extract")
async def api_panel_extract(
    panel_id: int,
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    """Run the OCR + LLM extraction pipeline on an uploaded panel source.

    Synchronous; expected to complete in seconds for typical lab PDFs.
    Returns the extracted biomarkers in an editable shape; the client
    POSTs them back to ``/panels/{id}/biomarkers`` with ``confirm: true``.
    """
    from istota.health.ocr import extract_from_panel

    config = getattr(request.app.state, "istota_config", None)

    def _extract():
        with health_db.connect(ctx.db_path) as conn:
            panel = health_db.get_panel(conn, panel_id)
            if not panel:
                return None
        return extract_from_panel(ctx, panel, config=config)

    result = await asyncio.to_thread(_extract)
    if result is None:
        raise HTTPException(404, "panel not found")
    return result


@router.get("/panels/{panel_id}/source")
async def api_panel_source(
    panel_id: int,
    ctx: HealthContext = Depends(get_user_context),
):
    """Stream the original uploaded image/PDF.

    Auth-gated. The path is resolved server-side from the panel row's
    ``source_file`` column — clients never get a raw filesystem path.
    """
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            return health_db.get_panel(conn, panel_id)

    panel = await asyncio.to_thread(_query)
    if not panel:
        raise HTTPException(404, "panel not found")
    if not panel.source_file:
        raise HTTPException(404, "no source file")
    candidate = (ctx.uploads_dir / panel.source_file).resolve()
    uploads_root = ctx.uploads_dir.resolve()
    try:
        candidate.relative_to(uploads_root)
    except ValueError:
        raise HTTPException(400, "invalid source path")
    if not candidate.is_file():
        raise HTTPException(404, "source file missing")
    return FileResponse(
        candidate,
        media_type=panel.source_mime or "application/octet-stream",
    )


# ---- Biomarker trends -----------------------------------------------------


@router.get("/biomarkers/trend")
async def api_biomarker_trend(
    ctx: HealthContext = Depends(get_user_context),
    name: str = Query(...),
    since: str = Query(default=""),
    until: str = Query(default=""),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            ref = health_db.find_biomarker_ref_by_alias(conn, name)
            canonical_name = ref.name if ref else name
            trend = health_db.biomarker_trend(
                conn,
                name=canonical_name,
                since=since or None,
                until=until or None,
            )
            settings = health_db.get_settings(conn)
            sex = settings.get("sex")
        return ref, canonical_name, trend, sex

    ref, canonical_name, trend, sex = await asyncio.to_thread(_query)
    points = [
        {
            "drawn_at": drawn_at,
            "value": b.value,
            "unit": b.unit,
            "flag": b.flag,
        }
        for b, drawn_at in trend
    ]
    units = [p["unit"] for p in points]
    canonical_low = canonical_high = None
    canonical_unit = None
    if ref is not None:
        canonical_low, canonical_high = pick_canonical_range(ref, sex)
        canonical_unit = ref.default_unit
    return {
        "name": canonical_name,
        "display_name": ref.display_name if ref else canonical_name,
        "points": points,
        "unit_mismatch": not all_units_agree(units) if points else False,
        "ref_range_low": canonical_low,
        "ref_range_high": canonical_high,
        "unit": canonical_unit,
    }


@router.get("/biomarkers/summary")
async def api_biomarker_summary(
    ctx: HealthContext = Depends(get_user_context),
):
    """Latest biomarker per name with rudimentary trend direction."""
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            rows = conn.execute(
                """
                SELECT b.*, p.drawn_at AS drawn_at FROM biomarkers b
                JOIN panels p ON p.id = b.panel_id
                WHERE p.draft = 0
                ORDER BY p.drawn_at ASC, b.id ASC
                """
            ).fetchall()
        by_name: dict[str, list[dict]] = {}
        for r in rows:
            by_name.setdefault(r["name"], []).append({
                "drawn_at": r["drawn_at"],
                "value": float(r["value"]),
                "unit": r["unit"],
                "flag": r["flag"],
            })
        out: list[dict] = []
        for name, vs in by_name.items():
            latest = vs[-1]
            prev = vs[-2] if len(vs) >= 2 else None
            direction = "flat"
            if prev:
                if latest["value"] > prev["value"] * 1.01:
                    direction = "up"
                elif latest["value"] < prev["value"] * 0.99:
                    direction = "down"
            out.append({
                "name": name,
                "latest": latest,
                "previous": prev,
                "direction": direction,
                "sample_count": len(vs),
            })
        out.sort(key=lambda x: x["name"].lower())
        return out

    summary = await asyncio.to_thread(_query)
    return {"summary": summary}


@router.get("/bloodwork/matrix")
async def api_bloodwork_matrix(
    ctx: HealthContext = Depends(get_user_context),
):
    """Spreadsheet view of every biomarker × every confirmed panel.

    Returns a structure suitable for a Date-rows / marker-columns table
    grouped by category, with the reference range pinned per column.

    ``panels`` is sorted by ``drawn_at`` ascending (oldest first), matching
    the "lab journal" layout people use offline. ``categories`` preserves
    a stable ordering from the bundled refs; markers not in the refs fall
    into an ``Other`` bucket.
    """
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            panels = health_db.list_panels(
                conn, include_drafts=False, limit=500,
            )
            panels_sorted = sorted(panels, key=lambda p: p.drawn_at)

            marker_meta: dict[str, dict] = {}
            values: dict[int, dict] = {}
            for p in panels_sorted:
                bs = health_db.list_biomarkers_for_panel(conn, p.id)
                values[p.id] = {}
                for b in bs:
                    marker_meta.setdefault(
                        b.name,
                        {"display_name": b.display_name, "unit": b.unit},
                    )
                    values[p.id][b.name] = {
                        "value": b.value,
                        "unit": b.unit,
                        "flag": b.flag,
                    }

            refs = health_db.list_biomarker_refs(conn)
            settings = health_db.get_settings(conn)
        return panels_sorted, marker_meta, values, refs, settings

    panels_sorted, marker_meta, values, refs, settings = await asyncio.to_thread(_query)

    ref_by_name = {r.name: r for r in refs}
    sex = settings.get("sex")

    # Build category buckets in the order categories first appear in refs;
    # anything unknown lands in "Other".
    cat_order: list[str] = []
    cat_markers: dict[str, list[dict]] = {}
    for r in refs:
        if r.category not in cat_markers:
            cat_order.append(r.category)
            cat_markers[r.category] = []

    for name, meta in marker_meta.items():
        ref = ref_by_name.get(name)
        cat = ref.category if ref else "Other"
        if cat not in cat_markers:
            cat_order.append(cat)
            cat_markers[cat] = []
        low = high = None
        if ref is not None:
            if sex:
                low, high = pick_canonical_range(ref, sex)
            else:
                low, high = widest_canonical_range(ref)
        cat_markers[cat].append({
            "name": name,
            "display_name": (
                (ref.display_name if ref else None)
                or meta.get("display_name")
                or name
            ),
            "unit": (
                (ref.default_unit if ref else None) or meta.get("unit") or ""
            ),
            "ref_range_low": low,
            "ref_range_high": high,
            "category": cat,
        })

    # Prune empty categories (refs whose markers nobody has measured).
    cat_order = [c for c in cat_order if cat_markers.get(c)]
    for cat in cat_markers:
        cat_markers[cat].sort(key=lambda m: m["display_name"].lower())

    return {
        "categories": [
            {"name": c, "markers": cat_markers[c]} for c in cat_order
        ],
        "panels": [
            {
                "id": p.id,
                "drawn_at": p.drawn_at,
                "lab_name": p.lab_name,
                "panel_type": p.panel_type,
            }
            for p in panels_sorted
        ],
        "values": {str(pid): vs for pid, vs in values.items()},
    }


@router.get("/biomarkers/{name}/explainer")
async def api_biomarker_explainer(
    name: str,
    request: Request,
    ctx: HealthContext = Depends(get_user_context),
    direction: str = Query(...),
):
    """Cached, brain-generated educational alert for an out-of-range value.

    ``direction`` must be ``"high"`` or ``"low"``. Returns a non-diagnostic
    summary + plausible causes + general considerations + a fixed
    disclaimer. Repeat calls for the same ``(name, direction)`` are served
    from the user's cache.
    """
    if direction not in ("high", "low"):
        return JSONResponse(
            {"error": "direction must be 'high' or 'low'"}, status_code=400,
        )

    config = getattr(request.app.state, "istota_config", None)

    def _resolve():
        from istota.health.explainer import get_or_generate

        with health_db.connect(ctx.db_path) as conn:
            ref = health_db.find_biomarker_ref_by_alias(conn, name)
            settings = health_db.get_settings(conn)
        sex = settings.get("sex")
        canonical = ref.name if ref else name
        display_name = ref.display_name if ref else name
        unit = ref.default_unit if ref else None
        low = high = None
        if ref is not None:
            if sex:
                low, high = pick_canonical_range(ref, sex)
            else:
                low, high = widest_canonical_range(ref)
        return get_or_generate(
            ctx,
            name=canonical,
            display_name=display_name,
            direction=direction,
            unit=unit,
            ref_low=low,
            ref_high=high,
            category=ref.category if ref else None,
            config=config,
        )

    return await asyncio.to_thread(_resolve)


@router.get("/biomarkers/refs")
async def api_biomarker_refs(
    ctx: HealthContext = Depends(get_user_context),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            refs = health_db.list_biomarker_refs(conn)
        out = []
        for r in refs:
            out.append({
                "name": r.name,
                "display_name": r.display_name,
                "category": r.category,
                "default_unit": r.default_unit,
                "ref_range_low": r.ref_range_low,
                "ref_range_high": r.ref_range_high,
                "ref_range_low_m": r.ref_range_low_m,
                "ref_range_high_m": r.ref_range_high_m,
                "ref_range_low_f": r.ref_range_low_f,
                "ref_range_high_f": r.ref_range_high_f,
                "aliases": r.aliases,
                "description": r.description,
            })
        return out

    refs = await asyncio.to_thread(_query)
    return {"refs": refs}


# ---- CSV import / export --------------------------------------------------


@router.post("/csv/import")
async def api_csv_import(
    request: Request,
    file: UploadFile = FastAPIFile(...),
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    """Import a bloodwork CSV.

    Accepts the same shape exported by ``GET /csv/export`` (category
    banner row + ``Marker (unit)`` headers + reference-range row +
    data rows). Aliases are resolved against ``biomarker_refs`` so
    column names like ``Hgb`` / ``LDL-C`` land on canonical markers.

    Dedup is content-based: identical biomarker sets are silently
    skipped; a same-date / same-lab collision with different content
    lands as a draft for user review. No user-facing choice.
    """
    from istota.health.csv_io import import_csv

    raw = await file.read()
    if not raw:
        return JSONResponse({"error": "empty upload"}, status_code=400)
    try:
        csv_text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            csv_text = raw.decode("latin-1")
        except UnicodeDecodeError:
            return JSONResponse(
                {"error": "could not decode file as UTF-8 or latin-1"},
                status_code=400,
            )

    def _import():
        with health_db.connect(ctx.db_path) as conn:
            summary = import_csv(conn, csv_text)
            conn.commit()
        return summary

    summary = await asyncio.to_thread(_import)
    return {
        "status": "ok",
        "panels_created": summary.panels_created,
        "panels_skipped_identical": summary.panels_skipped_identical,
        "panels_needs_review": summary.panels_needs_review,
        "biomarkers_created": summary.biomarkers_created,
        "rows_processed": summary.rows_processed,
        "warnings": summary.warnings,
    }


@router.get("/csv/export")
async def api_csv_export(ctx: HealthContext = Depends(get_user_context)):
    """Stream every confirmed panel as a CSV in the import format."""
    from istota.health.csv_io import export_csv

    def _export():
        with health_db.connect(ctx.db_path) as conn:
            return export_csv(conn)

    text = await asyncio.to_thread(_export)
    return PlainTextResponse(
        text,
        media_type="text/csv",
        headers={
            "Content-Disposition": 'attachment; filename="bloodwork.csv"',
        },
    )


# ---- Settings -------------------------------------------------------------


@router.get("/settings")
async def api_get_settings(ctx: HealthContext = Depends(get_user_context)):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            return health_db.get_settings(conn)

    stored = await asyncio.to_thread(_query)
    return {"settings": _settings_with_defaults(stored)}


@router.put("/settings")
async def api_put_settings(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)

    valid_keys = set(health_db.SETTINGS_KEYS)

    def _save():
        with health_db.connect(ctx.db_path) as conn:
            for k, v in body.items():
                if k not in valid_keys:
                    continue
                if k == "sex" and v not in (None, "M", "F", ""):
                    raise ValueError("sex must be 'M', 'F', or null")
                if k == "height_cm" and v is not None:
                    try:
                        float(v)
                    except (TypeError, ValueError):
                        raise ValueError("height_cm must be a number")
                if v in (None, ""):
                    health_db.delete_setting(conn, k)
                else:
                    health_db.set_setting(conn, k, v)
            conn.commit()
            return health_db.get_settings(conn)

    try:
        stored = await asyncio.to_thread(_save)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"status": "ok", "settings": _settings_with_defaults(stored)}


# ---- Dashboard ------------------------------------------------------------


@router.get("/dashboard")
async def api_dashboard(ctx: HealthContext = Depends(get_user_context)):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            latest = health_db.latest_stats(conn)
            panels = health_db.list_panels(
                conn, include_drafts=False, limit=3,
            )
            panel_dicts = []
            for p in panels:
                total, flagged = health_db.panel_counts(conn, p.id)
                panel_dicts.append(_panel_to_dict(
                    p, biomarker_count=total, flagged_count=flagged,
                ))
            alerts_rows = health_db.flagged_biomarkers_latest(conn, limit=20)
            settings = health_db.get_settings(conn)
            active_diag = health_db.list_diagnoses(
                conn, status="active", limit=500,
            )
            chronic_diag = health_db.list_diagnoses(
                conn, status="chronic", limit=500,
            )
            recent_encounters = health_db.list_encounters(conn, limit=3)
        # BMI is derived from latest weight + settings height.
        bmi: float | None = None
        weight = latest.get("weight")
        height_cm = settings.get("height_cm")
        if weight and height_cm:
            try:
                bmi = compute_bmi(weight.value, float(height_cm))
            except (TypeError, ValueError):
                bmi = None
        alerts = []
        for b, p in alerts_rows:
            d = _biomarker_to_dict(b)
            d["panel_id"] = p.id
            d["drawn_at"] = p.drawn_at
            d["lab_name"] = p.lab_name
            alerts.append(d)
        return {
            "latest_stats": {
                metric: _stat_to_dict(s) for metric, s in latest.items()
            },
            "bmi": bmi,
            "recent_panels": panel_dicts,
            "alerts": alerts,
            "settings": _settings_with_defaults(settings),
            "active_diagnoses_count": len(active_diag) + len(chronic_diag),
            "recent_encounters": [
                _encounter_to_dict(e) for e in recent_encounters
            ],
        }

    return await asyncio.to_thread(_query)


# ---- Encounters -----------------------------------------------------------


@router.get("/encounters")
async def api_list_encounters(
    ctx: HealthContext = Depends(get_user_context),
    since: str = Query(default=""),
    until: str = Query(default=""),
    type: str = Query(default=""),
    limit: int = Query(default=50, le=500, ge=1),
    offset: int = Query(default=0, ge=0),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            return health_db.list_encounters(
                conn,
                since=since or None,
                until=until or None,
                encounter_type=type or None,
                limit=limit,
                offset=offset,
            )

    encounters = await asyncio.to_thread(_query)
    return {"encounters": [_encounter_to_dict(e) for e in encounters]}


@router.post("/encounters")
async def api_create_encounter(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    encounter_date = body.get("encounter_date")
    encounter_type = body.get("encounter_type")
    if not isinstance(encounter_date, str) or not encounter_date.strip():
        return JSONResponse(
            {"error": "encounter_date is required"}, status_code=400,
        )
    if not isinstance(encounter_type, str) or not encounter_type.strip():
        return JSONResponse(
            {"error": "encounter_type is required"}, status_code=400,
        )

    def _insert():
        with health_db.connect(ctx.db_path) as conn:
            eid = health_db.insert_encounter(
                conn,
                encounter_date=encounter_date.strip(),
                encounter_type=encounter_type.strip(),
                provider=body.get("provider") or None,
                facility=body.get("facility") or None,
                specialty=body.get("specialty") or None,
                reason=body.get("reason") or None,
                notes=body.get("notes") or None,
            )
            conn.commit()
        return eid

    eid = await asyncio.to_thread(_insert)
    return {"status": "ok", "id": eid}


@router.get("/encounters/{encounter_id}")
async def api_get_encounter(
    encounter_id: int,
    ctx: HealthContext = Depends(get_user_context),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            enc = health_db.get_encounter(conn, encounter_id)
            if not enc:
                return None
            diagnoses = health_db.diagnoses_for_encounter(conn, encounter_id)
            panels = health_db.panels_for_encounter(conn, encounter_id)
            panel_dicts = []
            for p in panels:
                total, flagged = health_db.panel_counts(conn, p.id)
                panel_dicts.append(_panel_to_dict(
                    p, biomarker_count=total, flagged_count=flagged,
                ))
            return enc, diagnoses, panel_dicts

    result = await asyncio.to_thread(_query)
    if result is None:
        raise HTTPException(404, "encounter not found")
    enc, diagnoses, panel_dicts = result
    return {
        "encounter": _encounter_to_dict(enc),
        "diagnoses": [_diagnosis_to_dict(d) for d in diagnoses],
        "panels": panel_dicts,
    }


@router.put("/encounters/{encounter_id}")
async def api_update_encounter(
    encounter_id: int,
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    allowed = {
        "encounter_date", "encounter_type", "provider", "facility",
        "specialty", "reason", "notes",
    }
    kwargs = {k: v for k, v in body.items() if k in allowed}

    def _update():
        with health_db.connect(ctx.db_path) as conn:
            n = health_db.update_encounter(conn, encounter_id, **kwargs)
            conn.commit()
        return n

    n = await asyncio.to_thread(_update)
    if not n:
        # 0 rows could mean "no fields" or "not found"; distinguish.
        def _check():
            with health_db.connect(ctx.db_path) as conn:
                return health_db.get_encounter(conn, encounter_id)
        existing = await asyncio.to_thread(_check)
        if existing is None:
            raise HTTPException(404, "encounter not found")
    return {"status": "ok"}


@router.delete("/encounters/{encounter_id}")
async def api_delete_encounter(
    encounter_id: int,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    def _delete():
        with health_db.connect(ctx.db_path) as conn:
            n = health_db.delete_encounter(conn, encounter_id)
            conn.commit()
        return n

    n = await asyncio.to_thread(_delete)
    if not n:
        raise HTTPException(404, "encounter not found")
    return {"status": "ok"}


# ---- Diagnoses ------------------------------------------------------------


@router.get("/diagnoses")
async def api_list_diagnoses(
    ctx: HealthContext = Depends(get_user_context),
    status: str = Query(default=""),
    limit: int = Query(default=100, le=500, ge=1),
    offset: int = Query(default=0, ge=0),
):
    if status and status not in _DIAGNOSIS_STATUSES and status != "all":
        return JSONResponse(
            {"error": "unknown status"}, status_code=400,
        )

    def _query():
        with health_db.connect(ctx.db_path) as conn:
            return health_db.list_diagnoses(
                conn,
                status=status or None,
                limit=limit,
                offset=offset,
            )

    diagnoses = await asyncio.to_thread(_query)
    return {"diagnoses": [_diagnosis_to_dict(d) for d in diagnoses]}


@router.post("/diagnoses")
async def api_create_diagnosis(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    name = body.get("name")
    if not isinstance(name, str) or not name.strip():
        return JSONResponse({"error": "name is required"}, status_code=400)
    status = body.get("status", "active")
    if status not in _DIAGNOSIS_STATUSES:
        return JSONResponse({"error": "unknown status"}, status_code=400)
    encounter_id = body.get("encounter_id")
    if encounter_id is not None:
        try:
            encounter_id = int(encounter_id)
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "encounter_id must be an integer"}, status_code=400,
            )

    def _insert():
        with health_db.connect(ctx.db_path) as conn:
            if encounter_id is not None and health_db.get_encounter(
                conn, encounter_id,
            ) is None:
                return None
            did = health_db.insert_diagnosis(
                conn,
                name=name.strip(),
                status=status,
                icd10=body.get("icd10") or None,
                date_diagnosed=body.get("date_diagnosed") or None,
                date_resolved=body.get("date_resolved") or None,
                encounter_id=encounter_id,
                severity=body.get("severity") or None,
                notes=body.get("notes") or None,
            )
            conn.commit()
        return did

    did = await asyncio.to_thread(_insert)
    if did is None:
        return JSONResponse({"error": "encounter not found"}, status_code=400)
    return {"status": "ok", "id": did}


@router.get("/diagnoses/{diagnosis_id}")
async def api_get_diagnosis(
    diagnosis_id: int,
    ctx: HealthContext = Depends(get_user_context),
):
    def _query():
        with health_db.connect(ctx.db_path) as conn:
            d = health_db.get_diagnosis(conn, diagnosis_id)
            if not d:
                return None
            linked_encs = health_db.encounters_for_diagnosis(conn, diagnosis_id)
        return d, linked_encs

    result = await asyncio.to_thread(_query)
    if result is None:
        raise HTTPException(404, "diagnosis not found")
    d, linked_encs = result
    return {
        "diagnosis": _diagnosis_to_dict(d),
        "encounter": _encounter_to_dict(linked_encs[0]) if linked_encs else None,
    }


@router.put("/diagnoses/{diagnosis_id}")
async def api_update_diagnosis(
    diagnosis_id: int,
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    if "status" in body and body["status"] not in _DIAGNOSIS_STATUSES:
        return JSONResponse({"error": "unknown status"}, status_code=400)
    allowed = {
        "name", "icd10", "status", "date_diagnosed", "date_resolved",
        "encounter_id", "severity", "notes",
    }
    kwargs = {k: v for k, v in body.items() if k in allowed}
    if "encounter_id" in kwargs and kwargs["encounter_id"] is not None:
        try:
            kwargs["encounter_id"] = int(kwargs["encounter_id"])
        except (TypeError, ValueError):
            return JSONResponse(
                {"error": "encounter_id must be an integer or null"},
                status_code=400,
            )

    def _update():
        with health_db.connect(ctx.db_path) as conn:
            if (
                "encounter_id" in kwargs
                and kwargs["encounter_id"] is not None
                and health_db.get_encounter(conn, kwargs["encounter_id"]) is None
            ):
                return "encounter_not_found"
            n = health_db.update_diagnosis(conn, diagnosis_id, **kwargs)
            conn.commit()
        return n

    n = await asyncio.to_thread(_update)
    if n == "encounter_not_found":
        return JSONResponse({"error": "encounter not found"}, status_code=400)
    if not n:
        def _check():
            with health_db.connect(ctx.db_path) as conn:
                return health_db.get_diagnosis(conn, diagnosis_id)
        existing = await asyncio.to_thread(_check)
        if existing is None:
            raise HTTPException(404, "diagnosis not found")
    return {"status": "ok"}


@router.delete("/diagnoses/{diagnosis_id}")
async def api_delete_diagnosis(
    diagnosis_id: int,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    def _delete():
        with health_db.connect(ctx.db_path) as conn:
            n = health_db.delete_diagnosis(conn, diagnosis_id)
            conn.commit()
        return n

    n = await asyncio.to_thread(_delete)
    if not n:
        raise HTTPException(404, "diagnosis not found")
    return {"status": "ok"}


# ---- History summary ------------------------------------------------------


@router.get("/history/summary")
async def api_history_summary(
    ctx: HealthContext = Depends(get_user_context),
):
    """New-doctor packet: active conditions, chronic conditions,
    recent encounters (last 12 months), and last 5 procedures in the last
    5 years (older procedures aren't clinically useful for a packet)."""
    from datetime import timedelta

    today = datetime.now(timezone.utc).date()
    one_year_ago = (today - timedelta(days=365)).isoformat()
    five_years_ago = (today - timedelta(days=365 * 5)).isoformat()

    def _query():
        with health_db.connect(ctx.db_path) as conn:
            active = health_db.list_diagnoses(conn, status="active", limit=500)
            chronic = health_db.list_diagnoses(conn, status="chronic", limit=500)
            recent_encounters = health_db.list_encounters(
                conn, since=one_year_ago, limit=500,
            )
            recent_procedures = health_db.list_encounters(
                conn,
                encounter_type="procedure",
                since=five_years_ago,
                limit=5,
            )
        return active, chronic, recent_encounters, recent_procedures

    active, chronic, recent, procedures = await asyncio.to_thread(_query)
    return {
        "active_diagnoses": [_diagnosis_to_dict(d) for d in active],
        "chronic_diagnoses": [_diagnosis_to_dict(d) for d in chronic],
        "recent_encounters": [_encounter_to_dict(e) for e in recent],
        "recent_procedures": [_encounter_to_dict(e) for e in procedures],
    }


# ---- Garmin ---------------------------------------------------------------


def _user_id_from_request(request: Request) -> str:
    user = request.session.get("user") if hasattr(request, "session") else None
    if not isinstance(user, dict) or not user.get("username"):
        raise HTTPException(401, "unauthorized")
    return user["username"]


def _framework_db_path(request: Request) -> Path:
    """Framework istota.db path — where the encrypted ``secrets`` table
    (and therefore Garmin tokens) lives."""
    cfg = getattr(request.app.state, "istota_config", None)
    db_path = getattr(cfg, "db_path", None) if cfg else None
    if not db_path:
        raise HTTPException(503, "framework db_path unavailable")
    return Path(db_path)


def _user_tz(request: Request, user_id: str) -> str | None:
    cfg = getattr(request.app.state, "istota_config", None)
    if cfg is None:
        return None
    uc = cfg.get_user(user_id) if hasattr(cfg, "get_user") else None
    return getattr(uc, "timezone", None) if uc else None


@router.get("/garmin/status")
async def api_garmin_status(
    request: Request, ctx: HealthContext = Depends(get_user_context),
):
    user_id = _user_id_from_request(request)
    db_path = _framework_db_path(request)

    def _query():
        return health_garmin.get_status(db_path, user_id)
    return await asyncio.to_thread(_query)


@router.post("/garmin/connect")
async def api_garmin_connect(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be an object"}, status_code=400)
    email = body.get("email")
    password = body.get("password")
    if not isinstance(email, str) or not isinstance(password, str):
        return JSONResponse(
            {"error": "email and password are required"}, status_code=400,
        )
    user_id = _user_id_from_request(request)
    db_path = _framework_db_path(request)

    def _do():
        return health_garmin.connect(
            db_path, user_id=user_id, email=email, password=password,
        )

    try:
        return await asyncio.to_thread(_do)
    except health_garmin.GarminAuthError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)
    except health_garmin.GarminNotInstalled as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=503)


@router.post("/garmin/mfa")
async def api_garmin_mfa(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("code"), str):
        return JSONResponse({"error": "code is required"}, status_code=400)
    user_id = _user_id_from_request(request)
    db_path = _framework_db_path(request)

    def _do():
        return health_garmin.complete_mfa(
            db_path, user_id=user_id, code=body["code"],
        )

    try:
        return await asyncio.to_thread(_do)
    except health_garmin.GarminAuthError as exc:
        return JSONResponse({"status": "error", "error": str(exc)}, status_code=400)


@router.post("/garmin/disconnect")
async def api_garmin_disconnect(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    user_id = _user_id_from_request(request)
    db_path = _framework_db_path(request)

    def _do():
        return health_garmin.disconnect(db_path, user_id=user_id)

    return await asyncio.to_thread(_do)


@router.post("/garmin/sync")
async def api_garmin_sync(
    request: Request,
    _csrf: None = Depends(verify_origin),
    ctx: HealthContext = Depends(get_user_context),
):
    body: dict = {}
    try:
        body = await request.json()
    except (ValueError, TypeError):
        body = {}
    days_back = body.get("days_back", 7) if isinstance(body, dict) else 7
    try:
        days_back = max(1, min(90, int(days_back)))
    except (TypeError, ValueError):
        days_back = 7
    user_id = _user_id_from_request(request)
    db_path = _framework_db_path(request)
    user_tz = _user_tz(request, user_id)

    def _do():
        return health_garmin_sync.sync_garmin(
            ctx, db_path, days_back=days_back, user_tz=user_tz,
        ).to_dict()

    return await asyncio.to_thread(_do)
