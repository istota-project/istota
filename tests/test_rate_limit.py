"""`lib/rate_limit.SlidingWindow`: per-key hit counts over a window, bounded memory."""

from __future__ import annotations

import pytest

from istota.lib.rate_limit import SlidingWindow


class TestTheWindow:
    def test_hits_up_to_the_limit_are_admitted(self):
        window = SlidingWindow(60, 3)
        assert [window.admit("a", 0.0 + i) for i in range(4)] == [True, True, True, False]

    def test_a_refusal_does_not_extend_the_window(self):
        window = SlidingWindow(60, 1)
        assert window.admit("a", 0.0)
        assert not window.admit("a", 30.0)
        assert not window.admit("a", 59.0)
        assert window.admit("a", 60.0)

    def test_hits_leave_the_window_as_it_slides(self):
        window = SlidingWindow(10, 2)
        assert window.admit("a", 0.0)
        assert window.admit("a", 5.0)
        assert not window.admit("a", 9.0)
        assert window.admit("a", 10.5)
        assert not window.admit("a", 11.0)
        assert window.admit("a", 15.5)

    def test_keys_are_counted_apart(self):
        window = SlidingWindow(60, 1)
        assert window.admit("a", 0.0)
        assert window.admit("b", 0.0)
        assert not window.admit("a", 1.0)

    def test_a_zero_limit_admits_nothing(self):
        assert not SlidingWindow(60, 0).admit("a", 0.0)

    @pytest.mark.parametrize("window_seconds, max_hits", [(0, 1), (-1, 1), (60, -1)])
    def test_nonsense_bounds_are_refused(self, window_seconds, max_hits):
        with pytest.raises(ValueError):
            SlidingWindow(window_seconds, max_hits)


class TestBoundedMemory:
    def test_the_least_recently_used_key_is_evicted_at_the_cap(self):
        window = SlidingWindow(60, 1, max_keys=2)
        assert window.admit("a", 0.0)
        assert window.admit("b", 1.0)
        assert window.admit("c", 2.0)
        assert len(window) == 2
        # "a" was evicted, so its spent hit is forgotten.
        assert window.admit("a", 3.0)
        # "b" was evicted by "a"; "c" is still counted.
        assert not window.admit("c", 4.0)

    def test_a_used_key_is_refreshed_and_kept(self):
        window = SlidingWindow(60, 5, max_keys=2)
        window.admit("a", 0.0)
        window.admit("b", 1.0)
        window.admit("a", 2.0)
        window.admit("c", 3.0)
        assert "a" in window and "c" in window and "b" not in window

    def test_a_refused_key_does_not_take_a_slot(self):
        window = SlidingWindow(60, 0, max_keys=2)
        for key in ("a", "b", "c", "d"):
            window.admit(key, 0.0)
        assert len(window) == 0
