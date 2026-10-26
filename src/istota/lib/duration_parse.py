"""A number and one unit letter as a ``timedelta``, in one place (ISSUE-834).

Four skill CLIs each carried a copy: ``skills/tasks.parse_since`` (``30m``,
``2h``, ``7d``), ``skills/location._share_duration`` (``s``, ``m``, ``h``),
``assignments/charter.parse_duration_minutes`` (``m``, ``h``, ``d``) and
``skills/email._parse_since`` (``d`` only). They disagreed on the safety
properties: only the charter capped the digits and only ``parse_since``
bounded the value before building a ``timedelta``, so the email one raised
``OverflowError`` on a huge window, which its command reported as an
out-of-range date or, on a connected account, as a connection failure; three
matched ``\\d``, which takes any Unicode digit. The strict answer wins on each.

What stays with the caller: which units it takes, its own minimum and
maximum, and the error it raises. This only reads the shape.

stdlib-only leaf; imports nothing from the package.
"""

from __future__ import annotations

import re
from datetime import timedelta

_UNIT_LENGTHS = {
    "s": timedelta(seconds=1),
    "m": timedelta(minutes=1),
    "h": timedelta(hours=1),
    "d": timedelta(days=1),
}

# Nine digits of days is within `timedelta`'s own limit (999999999 days), so
# building the answer cannot overflow for any unit in the table.
MAX_DIGITS = 9

_SHAPE = re.compile(r"([0-9]+)([a-z])")


class DurationTooLong(ValueError):
    """The right shape, with more than `MAX_DIGITS` significant digits.

    Separate so a caller with an upper bound can say the value is too far
    rather than misshapen.
    """


def _unit_list(units: str) -> str:
    letters = list(units)
    if len(letters) == 1:
        return letters[0]
    return ", ".join(letters[:-1]) + " or " + letters[-1]


def parse_duration(value: object, units: str) -> timedelta:
    """``value`` as a length of time, using only the unit letters in ``units``.

    Leading and trailing whitespace is ignored and the unit is matched without
    case. Zero is a valid length; a caller with a minimum checks it. Raises
    ``DurationTooLong`` past ``MAX_DIGITS`` significant digits and
    ``ValueError`` with a plain message on anything else.
    """
    unknown = [letter for letter in units if letter not in _UNIT_LENGTHS]
    if unknown or not units:
        raise ValueError(f"unknown duration unit in {units!r}")
    text = "" if value is None else str(value).strip().lower()
    match = _SHAPE.fullmatch(text)
    if match is None or match.group(2) not in units:
        raise ValueError(
            f"a duration is a whole number followed by {_unit_list(units)}"
        )
    digits = match.group(1).lstrip("0") or "0"
    if len(digits) > MAX_DIGITS:
        raise DurationTooLong(f"a duration has at most {MAX_DIGITS} digits")
    return int(digits) * _UNIT_LENGTHS[match.group(2)]
