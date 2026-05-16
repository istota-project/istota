"""EHR-paste parser for immunization records.

Pure function over a multiline string + a list of canonical refs.
Three shape detectors run in order; the family resolver normalises
each line's product description against the bundled aliases. Lines
that don't match any alias come back tagged ``name = "Unknown"`` with
the source line preserved in ``notes`` — so a paste never silently
drops a row.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from istota.health.models import ImmunizationRef


@dataclass
class ParsedImmunization:
    name: str
    product_name: str | None
    date_given: str | None
    source_line: str
    confidence: str
    notes: str | None = None


# MyChart / Epic shape: "<product> (Given M/D/YYYY)" or "(Given MM/DD/YY)".
# Tolerant of mixed spacing and case.
_MYCHART_RE = re.compile(
    r"^(?P<product>.+?)\s*\(\s*Given\s+(?P<date>\d{1,2}/\d{1,2}/\d{2,4})\s*\)\s*$",
    re.IGNORECASE,
)

# Trailing ISO date.
_ISO_RE = re.compile(
    r"^(?P<product>.+?)\s+(?P<date>\d{4}-\d{2}-\d{2})\s*$",
)


def _normalise_date(raw: str) -> str | None:
    raw = raw.strip()
    # Already ISO.
    iso = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if iso:
        return raw
    # US M/D/YYYY or M/D/YY.
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", raw)
    if not m:
        return None
    month, day, year = m.groups()
    if len(year) == 2:
        # Conventional pivot — 00–69 → 2000–2069, 70–99 → 1970–1999.
        y = int(year)
        year = f"20{year}" if y < 70 else f"19{year}"
    try:
        from datetime import date as _date
        d = _date(int(year), int(month), int(day))
        return d.isoformat()
    except ValueError:
        return None


def _build_alias_index(
    refs: list[ImmunizationRef],
) -> list[tuple[str, ImmunizationRef]]:
    """Sorted list of (alias_lower, ref). Longest aliases first.

    Sorted longest-first so 'Fluzone Quadrivalent' beats 'flu' when the
    product blob contains both substrings.
    """
    items: list[tuple[str, ImmunizationRef]] = []
    for ref in refs:
        items.append((ref.name.lower(), ref))
        for a in ref.aliases:
            a_norm = a.strip().lower()
            if a_norm:
                items.append((a_norm, ref))
    items.sort(key=lambda x: (-len(x[0]), x[0]))
    return items


def _resolve_family(
    product: str, alias_index: list[tuple[str, ImmunizationRef]],
) -> ImmunizationRef | None:
    """Substring match against the alias index — first hit wins (longest first)."""
    p = product.lower()
    for alias, ref in alias_index:
        # Whole-word-ish boundary check so 'd' or 'a' don't match inside
        # words. We accept the alias if it's surrounded by non-alphanumerics
        # or the string ends.
        idx = p.find(alias)
        while idx != -1:
            before = p[idx - 1] if idx > 0 else " "
            after = p[idx + len(alias)] if idx + len(alias) < len(p) else " "
            if not before.isalnum() and not after.isalnum():
                return ref
            idx = p.find(alias, idx + 1)
    return None


def parse_paste(
    text: str, refs: list[ImmunizationRef],
) -> list[ParsedImmunization]:
    """Parse a multi-line EHR / MyChart paste.

    Lines that don't match either shape return ``date_given=None`` and
    ``confidence="manual"`` so the UI prompts the user for a date.
    Family resolution is best-effort; unresolved rows come back with
    ``name="Unknown"`` and the full source line in ``notes``.
    """
    alias_index = _build_alias_index(refs)
    out: list[ParsedImmunization] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        m = _MYCHART_RE.match(line)
        date_iso: str | None = None
        product: str | None = None
        confidence = "low"
        if m:
            product = m.group("product").strip()
            date_iso = _normalise_date(m.group("date"))
            confidence = "high" if date_iso else "medium"
        else:
            m = _ISO_RE.match(line)
            if m:
                product = m.group("product").strip()
                date_iso = _normalise_date(m.group("date"))
                confidence = "high" if date_iso else "medium"
            else:
                # Free-text fallback — full line is product, no date.
                product = line
                date_iso = None
                confidence = "manual"

        ref = _resolve_family(product or "", alias_index)
        if ref is not None:
            name = ref.name
            notes = None
        else:
            name = "Unknown"
            notes = line

        out.append(ParsedImmunization(
            name=name,
            product_name=product or None,
            date_given=date_iso,
            source_line=line,
            confidence=confidence,
            notes=notes,
        ))
    return out
