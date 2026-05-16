"""Data types for the health module.

The :class:`HealthContext` is the per-user runtime handle (workspace paths +
db path). Built by :func:`istota.health.workspace.synthesize_health_context`
or :func:`istota.health._loader.resolve_for_user`. Every other function in the
module takes a context (or a connection rooted at its db_path).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


# Canonical stat metric keys. Extensible — new keys can be added without a
# schema migration. Kept centralised so the API, CLI, and dashboard agree on
# the spelling.
STAT_METRICS: dict[str, str] = {
    "weight": "kg",
    "resting_hr": "bpm",
    "blood_pressure_systolic": "mmHg",
    "blood_pressure_diastolic": "mmHg",
    "body_fat_pct": "%",
    "body_temp": "°C",
    "respiratory_rate": "brpm",
    "blood_oxygen": "%",
    # Garmin-sourced daily summaries (source='garmin' on the row).
    # Keys mirror the spec's daily-summary contract; values stay in
    # canonical units so the existing dashboard / display layer renders
    # them without special casing.
    "sleep_duration_min": "min",
    "sleep_score": "score",
    "sleep_deep_min": "min",
    "sleep_light_min": "min",
    "sleep_rem_min": "min",
    "sleep_awake_min": "min",
    "stress_avg": "score",
    "stress_max": "score",
    "body_battery_high": "score",
    "body_battery_low": "score",
    "steps": "steps",
    "active_calories": "kcal",
    "spo2_avg": "%",
    "hrv_status": "ms",
    "vo2_max": "ml/kg/min",
    "respiration_avg": "brpm",
}


@dataclass(frozen=True)
class HealthContext:
    """Per-user runtime handle.

    ``workspace_root`` is the user's bot workspace dir; ``data_dir`` is the
    health module's subdir, and ``uploads_dir`` holds the raw images/PDFs of
    uploaded lab reports. ``framework_db_path`` is the path to istota.db
    (where the encrypted ``secrets`` table holding Garmin tokens lives)
    — defaults to ``None`` so existing callers keep working.
    """
    user_id: str
    workspace_root: Path
    data_dir: Path
    db_path: Path
    uploads_dir: Path
    framework_db_path: Path | None = None

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        (self.data_dir / "data").mkdir(parents=True, exist_ok=True)
        self.uploads_dir.mkdir(parents=True, exist_ok=True)


@dataclass
class Stat:
    id: int
    measured_at: str
    metric: str
    value: float
    unit: str
    source: str
    source_ref: int | None
    notes: str | None
    created_at: str = ""


@dataclass
class Panel:
    id: int
    drawn_at: str
    lab_name: str | None
    panel_type: str | None
    source_file: str | None
    source_mime: str | None
    ocr_text: str | None
    draft: bool
    notes: str | None
    created_at: str = ""
    content_hash: str | None = None


@dataclass
class Biomarker:
    id: int
    panel_id: int
    name: str
    display_name: str | None
    value: float
    unit: str
    ref_range_low: float | None
    ref_range_high: float | None
    flag: str | None
    created_at: str = ""


@dataclass
class BiomarkerRef:
    name: str
    display_name: str
    category: str
    default_unit: str
    ref_range_low: float | None
    ref_range_high: float | None
    ref_range_low_m: float | None
    ref_range_high_m: float | None
    ref_range_low_f: float | None
    ref_range_high_f: float | None
    aliases: list[str]
    description: str | None
