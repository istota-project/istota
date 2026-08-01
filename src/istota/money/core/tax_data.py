"""Bundled tax rate data, its provenance, and year resolution.

The rate tables used to live as module-level dicts in ``tax.py``. They are data,
they change every January, and getting them wrong produces a number the user
sends to a government — so they live in ``data/tax_rates.json`` instead, where
updating a year is a reviewed one-file change and every year carries a citation.

Two properties this module exists to provide, neither of which needs a network
call:

**Attribution.** Every bundled year names the document it was transcribed from
and the date someone last checked it. The UI shows both.

**Staleness.** The old resolver silently fell back to the newest year present
when the requested year was missing, so in 2027 the page would have computed
from 2026 numbers and reported them as 2027 — the same class of failure as the
2026 rows that were copies of 2025 with nothing able to say so. Resolution now
returns the year it *actually used*, so the caller can say when it differs.

Deliberately not fetched from anywhere. The IRS publishes no machine-readable
rates, and fetching would not have prevented the bug it would appear to solve:
OBBBA changed the *structure* of 2026 federal tax, not only the indexed amounts,
so a bracket API would have returned perfectly current thresholds and still
produced a wrong answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from pathlib import Path

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "tax_rates.json"

# Federal estimated-tax installments: equal quarters, expressed cumulatively.
DEFAULT_INSTALLMENT_SCHEDULE: tuple[float, float, float, float] = (0.25, 0.50, 0.75, 1.00)

# The one conformity knob. See the `starting_points` block in the data file for
# what each means; `tax.compute_state_tax` is what acts on it.
STARTING_POINTS = ("federal_agi", "federal_taxable_income", "gross_compensation")

_EMPTY: dict = {}


@dataclass(frozen=True)
class Jurisdiction:
    """A selectable state, with or without bundled rate data."""

    code: str
    name: str
    taxes_income: bool
    note: str = ""


@dataclass(frozen=True)
class PayrollRates:
    ss_wage_base: float
    ss_rate: float
    medicare_rate: float
    se_taxable_fraction: float
    additional_medicare_rate: float


@dataclass(frozen=True)
class YearRates:
    """One jurisdiction's rates for one year, plus how we got here.

    ``requested_year`` and ``year`` differ exactly when the requested year had
    no bundled data and resolution fell back. ``is_fallback`` is the signal the
    UI turns into a warning naming both years.
    """

    requested_year: int
    year: int
    source: str
    source_url: str
    verified_on: str
    notes: str
    _statuses: dict
    payroll: PayrollRates | None = None

    @property
    def is_fallback(self) -> bool:
        return self.year != self.requested_year

    @property
    def is_stale(self) -> bool:
        """Were these figures last verified before the tax year even began?"""
        return is_stale(self.verified_on, self.requested_year)

    def _status(self, filing_status: str) -> dict:
        return self._statuses.get(filing_status) or _EMPTY

    def brackets(self, filing_status: str) -> list[tuple[float, float]]:
        raw = self._status(filing_status).get("brackets") or []
        return [(b[0], b[1]) for b in raw]

    def standard_deduction(self, filing_status: str) -> float:
        return float(self._status(filing_status).get("standard_deduction") or 0)

    def personal_exemption(self, filing_status: str) -> float:
        """Per-return exemption, subtracted alongside the standard deduction.

        Several flat states (Illinois, Indiana, Michigan) carry one instead of a
        standard deduction, which is why a flat state costs more than one number.
        """
        return float(self._status(filing_status).get("personal_exemption") or 0)

    def qbi_threshold(self, filing_status: str) -> float:
        return float(self._status(filing_status).get("qbi_threshold") or 0)

    def qbi_phaseout_range(self, filing_status: str) -> float:
        return float(self._status(filing_status).get("qbi_phaseout_range") or 0)

    def additional_medicare_threshold(self, filing_status: str) -> float:
        return float(self._status(filing_status).get("additional_medicare_threshold") or 0)

    def has_brackets(self, filing_status: str) -> bool:
        return bool(self._status(filing_status).get("brackets"))


@dataclass(frozen=True)
class StateMeta:
    """A bundled state's structure, which does not vary by year."""

    code: str
    starts_from: str
    installment_schedule: tuple[float, float, float, float]
    installment_note: str = ""


class TaxRates:
    """The parsed bundle. Build via :func:`load_tax_rates`."""

    def __init__(self, raw: dict) -> None:
        self._raw = raw
        self.jurisdictions: list[Jurisdiction] = [
            Jurisdiction(
                code=j["code"],
                name=j["name"],
                taxes_income=bool(j.get("taxes_income", True)),
                note=j.get("note", ""),
            )
            for j in raw.get("jurisdictions", [])
        ]
        self._by_code = {j.code: j for j in self.jurisdictions}

    # -- registry -----------------------------------------------------------

    def jurisdiction(self, code: str) -> Jurisdiction | None:
        return self._by_code.get((code or "").upper())

    def taxes_income(self, code: str) -> bool:
        j = self.jurisdiction(code)
        return bool(j and j.taxes_income)

    # -- federal ------------------------------------------------------------

    def latest_ss_wage_base(self) -> float:
        """The newest bundled Social Security wage base, or 0 if none.

        The last-resort fallback for a year whose block is missing its payroll
        data. A zero wage base is not a benign default: `min(taxable_se, 0)` is
        zero, so the entire Social Security half of SE tax silently vanishes and
        the result looks normal. Standing in the newest real figure is wrong by
        one year's indexation rather than by the whole component.
        """
        for year in sorted(self._raw.get("federal", {}), reverse=True):
            block = self._raw["federal"][year]
            base = (block.get("payroll") or {}).get("ss_wage_base")
            if base:
                return float(base)
        return 0.0

    def federal_years(self) -> list[int]:
        return sorted(int(y) for y in self._raw.get("federal", {}))

    def federal_year(self, year: int) -> YearRates | None:
        block = self._raw.get("federal", {})
        resolved = _resolve_year(block, year)
        if resolved is None:
            return None
        data = block[str(resolved)]
        payroll_raw = data.get("payroll") or {}
        return YearRates(
            requested_year=year,
            year=resolved,
            source=data.get("source", ""),
            source_url=data.get("source_url", ""),
            verified_on=data.get("verified_on", ""),
            notes=data.get("notes", ""),
            _statuses=data.get("filing_status") or {},
            payroll=PayrollRates(
                ss_wage_base=float(payroll_raw.get("ss_wage_base") or 0),
                ss_rate=float(payroll_raw.get("ss_rate") or 0),
                medicare_rate=float(payroll_raw.get("medicare_rate") or 0),
                se_taxable_fraction=float(payroll_raw.get("se_taxable_fraction") or 0),
                additional_medicare_rate=float(
                    payroll_raw.get("additional_medicare_rate") or 0
                ),
            ),
        )

    # -- states -------------------------------------------------------------

    def bundled_state_codes(self) -> list[str]:
        """States we ship rate data for. Every other state is override-driven."""
        return sorted(self._raw.get("states", {}))

    def omitted_states(self) -> dict[str, str]:
        """States deliberately left unbundled, mapped to why.

        Absent data is fine; absent data that looks like an oversight is not.
        These are states whose rate is known but whose base, deduction or
        exemption could not be verified for the year — shipping the rate alone
        would produce a confident wrong number.
        """
        return {
            code: reason
            for code, reason in (self._raw.get("omitted_states") or {}).items()
            if not code.startswith("_")
        }

    def state_meta(self, code: str) -> StateMeta | None:
        block = self._raw.get("states", {}).get((code or "").upper())
        if block is None:
            return None
        schedule = block.get("installment_schedule") or list(DEFAULT_INSTALLMENT_SCHEDULE)
        return StateMeta(
            code=code.upper(),
            starts_from=block.get("starts_from", "federal_agi"),
            installment_schedule=tuple(schedule),  # type: ignore[arg-type]
            installment_note=block.get("installment_note", ""),
        )

    def state_years(self, code: str) -> list[int]:
        block = self._raw.get("states", {}).get((code or "").upper())
        if block is None:
            return []
        return sorted(int(y) for y in block.get("years", {}))

    def state_year(self, code: str, year: int) -> YearRates | None:
        """Resolve a state's rates, or None.

        None is a real answer, not an error: a state with no bundled data is
        selectable and override-driven, and a no-tax state has nothing to ship.
        The caller must report the state as unavailable rather than computing a
        zero liability from an empty bracket table.
        """
        block = self._raw.get("states", {}).get((code or "").upper())
        if block is None:
            return None
        years = block.get("years", {})
        resolved = _resolve_year(years, year)
        if resolved is None:
            return None
        data = years[str(resolved)]
        return YearRates(
            requested_year=year,
            year=resolved,
            source=data.get("source", ""),
            source_url=data.get("source_url", ""),
            verified_on=data.get("verified_on", ""),
            notes=data.get("notes", ""),
            _statuses=data.get("filing_status") or {},
        )


def _resolve_year(block: dict, year: int) -> int | None:
    """The newest available year at or before ``year``, else the oldest.

    "The law as most recently published at or before that year" is the right
    reading for tax data, and it handles a gap in the bundle (2025 and 2027
    present, 2026 asked for) rather than only the clamp cases. Falling forward
    to the oldest when the request predates the whole bundle keeps the estimate
    computable; it is flagged as a fallback either way, which is the point.
    """
    available = sorted(int(y) for y in block)
    if not available:
        return None
    at_or_before = [y for y in available if y <= year]
    return max(at_or_before) if at_or_before else min(available)


def is_stale(verified_on: str, tax_year: int) -> bool:
    """Do these figures predate the tax year they are being used to compute?

    A blank or unparseable date reads as stale. An unverifiable claim about
    freshness is not a claim to freshness, and the failure mode we are guarding
    against is silence.
    """
    if not verified_on:
        return True
    try:
        checked = date.fromisoformat(verified_on)
    except ValueError:
        return True
    return checked < date(tax_year, 1, 1)


@lru_cache(maxsize=1)
def load_tax_rates() -> TaxRates:
    """Parse and cache the bundled rate data."""
    return TaxRates(json.loads(DATA_PATH.read_text(encoding="utf-8")))
