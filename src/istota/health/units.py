"""Unit conversion helpers + reference-range / flag computation.

All storage is metric. The display layer reads ``display_units`` from
``health_settings`` and renders values in the user's preferred system.

The conversion table is intentionally short — it covers the common
cross-lab unit clashes the spec calls out (glucose, cholesterol, LDL, HDL,
triglycerides, creatinine, hemoglobin) plus stat-level unit toggles.
Anything we can't convert safely stays as-is.
"""

from __future__ import annotations

from typing import Iterable


# (from_unit, to_unit) -> multiplier on the source value
_CONVERSIONS: dict[tuple[str, str], float] = {
    # weight
    ("kg", "lb"): 2.2046226218,
    ("lb", "kg"): 1.0 / 2.2046226218,
    # length
    ("cm", "in"): 1.0 / 2.54,
    ("in", "cm"): 2.54,
    # cholesterol / LDL / HDL / triglycerides
    # (the SI-vs-conventional split is sometimes 38.67 for cholesterol and
    # 88.57 for triglycerides; ask once, convert once.)
    ("mg/dL", "mmol/L_chol"): 1.0 / 38.67,
    ("mmol/L_chol", "mg/dL"): 38.67,
    ("mg/dL", "mmol/L_tg"): 1.0 / 88.57,
    ("mmol/L_tg", "mg/dL"): 88.57,
    # glucose
    ("mg/dL", "mmol/L_glucose"): 1.0 / 18.0182,
    ("mmol/L_glucose", "mg/dL"): 18.0182,
    # creatinine
    ("mg/dL", "umol/L_creat"): 88.4,
    ("umol/L_creat", "mg/dL"): 1.0 / 88.4,
    # hemoglobin
    ("g/dL", "g/L"): 10.0,
    ("g/L", "g/dL"): 0.1,
}


# Biomarker -> (mg/dL <-> mmol/L tag). When a unit-pair conversion is
# requested between mg/dL and mmol/L, we need to know which constant applies.
_MGDL_MMOL_TAG: dict[str, str] = {
    "Cholesterol_Total": "mmol/L_chol",
    "LDL": "mmol/L_chol",
    "HDL": "mmol/L_chol",
    "Triglycerides": "mmol/L_tg",
    "Glucose": "mmol/L_glucose",
}


def f_to_c(f: float) -> float:
    return (f - 32.0) * 5.0 / 9.0


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def convert_temperature(value: float, from_unit: str, to_unit: str) -> float | None:
    fu, tu = from_unit.strip(), to_unit.strip()
    if fu == tu:
        return value
    if fu in ("C", "°C") and tu in ("F", "°F"):
        return c_to_f(value)
    if fu in ("F", "°F") and tu in ("C", "°C"):
        return f_to_c(value)
    return None


def convert_biomarker_value(
    biomarker_name: str, value: float, from_unit: str, to_unit: str,
) -> float | None:
    """Convert a biomarker value between commonly-clashing units.

    Returns ``None`` when conversion isn't safe — the caller should skip
    the point and surface a unit-mismatch warning rather than coerce.
    """
    fu, tu = from_unit.strip(), to_unit.strip()
    if fu == tu:
        return value
    # mmol/L is ambiguous without the biomarker context.
    tag = _MGDL_MMOL_TAG.get(biomarker_name)
    if fu == "mg/dL" and tu == "mmol/L":
        if tag is None:
            return None
        return value * _CONVERSIONS[("mg/dL", tag)]
    if fu == "mmol/L" and tu == "mg/dL":
        if tag is None:
            return None
        return value * _CONVERSIONS[(tag, "mg/dL")]
    mult = _CONVERSIONS.get((fu, tu))
    if mult is None:
        return None
    return value * mult


def compute_flag(
    value: float,
    *,
    low: float | None,
    high: float | None,
    lab_flag: str | None = None,
) -> str | None:
    """Compute a flag against a given range.

    ``H`` for above range, ``L`` for below. ``C`` (critical) is only ever
    respected from the lab — we never auto-promote. Missing range bounds
    are treated as open-ended.
    """
    if lab_flag and lab_flag.upper() == "C":
        return "C"
    if high is not None and value > high:
        return "H"
    if low is not None and value < low:
        return "L"
    return None


def widest_canonical_range(ref) -> tuple[float | None, float | None]:
    """Sex-agnostic display range — useful when the user's sex isn't set.

    Returns the widest plausible bounds across both sex-specific ranges and
    the unisex range. Distinct from :func:`pick_canonical_range`, which is
    used for flag computation and stays strict.
    """
    lows = [
        v for v in (
            getattr(ref, "ref_range_low", None),
            getattr(ref, "ref_range_low_m", None),
            getattr(ref, "ref_range_low_f", None),
        )
        if v is not None
    ]
    highs = [
        v for v in (
            getattr(ref, "ref_range_high", None),
            getattr(ref, "ref_range_high_m", None),
            getattr(ref, "ref_range_high_f", None),
        )
        if v is not None
    ]
    return (min(lows) if lows else None, max(highs) if highs else None)


def pick_canonical_range(
    ref,
    sex: str | None,
) -> tuple[float | None, float | None]:
    """Choose the sex-specific range when applicable, fall back to the
    general (unisex) range otherwise.

    ``ref`` is a :class:`BiomarkerRef` (or duck-typed dict-like with the same
    attributes); we read attributes directly so this works for both.
    """
    s = (sex or "").upper()
    if s == "M":
        low = getattr(ref, "ref_range_low_m", None) or ref.ref_range_low
        high = getattr(ref, "ref_range_high_m", None) or ref.ref_range_high
        return low, high
    if s == "F":
        low = getattr(ref, "ref_range_low_f", None) or ref.ref_range_low
        high = getattr(ref, "ref_range_high_f", None) or ref.ref_range_high
        return low, high
    return ref.ref_range_low, ref.ref_range_high


def compute_bmi(weight_kg: float, height_cm: float) -> float | None:
    if height_cm <= 0 or weight_kg <= 0:
        return None
    h = height_cm / 100.0
    return round(weight_kg / (h * h), 2)


def all_units_agree(units: Iterable[str]) -> bool:
    """True when every unit in the iterable is the same non-empty string."""
    seen: set[str] = set()
    for u in units:
        if not u:
            continue
        seen.add(u.strip())
        if len(seen) > 1:
            return False
    return len(seen) == 1
