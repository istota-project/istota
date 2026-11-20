"""`lib.timezones.is_valid_timezone`: the one boolean zone check (#976)."""

import pytest

from istota.lib.timezones import is_valid_timezone


@pytest.mark.parametrize("name", ["UTC", "America/New_York", "America/Los_Angeles", "Europe/Warsaw"])
def test_a_zone_that_loads(name):
    assert is_valid_timezone(name) is True


@pytest.mark.parametrize("name", [
    "", None, 5, ["UTC"], "PST", "PDT", "America/NewYork", "a" * 300, "../../etc/passwd", "UTC\x00",
])
def test_anything_else_is_false_and_never_raises(name):
    assert is_valid_timezone(name) is False
