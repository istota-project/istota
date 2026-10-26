"""The shared `N` plus unit duration rule (ISSUE-834).

Four skill CLIs carried a copy. The tests here are the rule itself; each
caller's own file covers its limits and its error envelope.
"""

from datetime import timedelta

import pytest

from istota.lib.duration_parse import MAX_DIGITS, DurationTooLong, parse_duration


@pytest.mark.parametrize("raw, units, expected", [
    ("30m", "mhd", timedelta(minutes=30)),
    ("6h", "mhd", timedelta(hours=6)),
    ("2d", "mhd", timedelta(days=2)),
    ("45s", "smh", timedelta(seconds=45)),
    (" 2D ", "mhd", timedelta(days=2)),
    ("0m", "mhd", timedelta(0)),
])
def test_parses_a_number_and_an_allowed_unit(raw, units, expected):
    assert parse_duration(raw, units) == expected


@pytest.mark.parametrize("raw", [
    "", "   ", "6", "h", "-1h", "+1h", "1.5h", "1 h", "1hh", "h1", "NaN",
    "٣d",  # an Arabic-Indic three: `\d` matches it and `int()` reads it
])
def test_refuses_anything_but_ascii_digits_and_one_unit(raw):
    with pytest.raises(ValueError):
        parse_duration(raw, "mhd")


def test_refuses_a_unit_the_caller_did_not_allow():
    with pytest.raises(ValueError, match="m, h or d"):
        parse_duration("30s", "mhd")
    with pytest.raises(ValueError):
        parse_duration("2d", "smh")


def test_a_number_too_long_is_a_value_error_not_an_overflow():
    # timedelta(days=10**12) raises OverflowError, which a caller catching
    # ValueError lets out as a traceback.
    assert parse_duration("9" * MAX_DIGITS + "d", "d") == timedelta(days=int("9" * MAX_DIGITS))
    with pytest.raises(DurationTooLong):
        parse_duration("9" * (MAX_DIGITS + 1) + "d", "d")
    with pytest.raises(DurationTooLong):
        parse_duration("999999999999d", "mhd")


def test_leading_zeros_do_not_count_toward_the_cap():
    assert parse_duration("0000000001d", "d") == timedelta(days=1)
    assert parse_duration("00m", "m") == timedelta(0)


def test_a_misshapen_value_is_not_reported_as_too_long():
    with pytest.raises(ValueError) as exc:
        parse_duration("9999999999x", "d")
    assert not isinstance(exc.value, DurationTooLong)


@pytest.mark.parametrize("raw", [None, 30, ["30m"]])
def test_a_non_string_is_a_value_error_not_a_type_error(raw):
    # The charter passes values straight off argparse and JSON.
    with pytest.raises(ValueError):
        parse_duration(raw, "mhd")


def test_an_unknown_unit_letter_is_a_caller_error():
    with pytest.raises(ValueError, match="unknown"):
        parse_duration("1w", "w")
