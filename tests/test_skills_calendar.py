"""Tests for calendar skill — all-day event timezone filtering."""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from istota.skills.calendar import get_events, get_tomorrow_events, get_today_events


def _make_ical_allday(uid: str, summary: str, start_date: date, end_date: date) -> str:
    """Build a minimal iCalendar string for an all-day event."""
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DTSTART;VALUE=DATE:{start_date.strftime('%Y%m%d')}\r\n"
        f"DTEND;VALUE=DATE:{end_date.strftime('%Y%m%d')}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def _make_ical_timed(uid: str, summary: str, start: datetime, end: datetime) -> str:
    """Build a minimal iCalendar string for a timed event."""
    fmt = "%Y%m%dT%H%M%S"
    return (
        "BEGIN:VCALENDAR\r\n"
        "VERSION:2.0\r\n"
        "BEGIN:VEVENT\r\n"
        f"UID:{uid}\r\n"
        f"SUMMARY:{summary}\r\n"
        f"DTSTART:{start.strftime(fmt)}\r\n"
        f"DTEND:{end.strftime(fmt)}\r\n"
        "END:VEVENT\r\n"
        "END:VCALENDAR\r\n"
    )


def _mock_caldav_event(ical_str: str):
    """Create a mock caldav event object with .data."""
    ev = MagicMock()
    ev.data = ical_str
    return ev


class TestAllDayEventFiltering:
    """Verify that all-day events outside the local date range are filtered out."""

    def _query_events(self, mock_events, start: datetime, end: datetime):
        """Helper: mock CalDAV and call get_events."""
        client = MagicMock()
        calendar = MagicMock()
        client.calendar.return_value = calendar
        calendar.search.return_value = mock_events
        return get_events(client, "https://fake/cal", start, end)

    def test_allday_event_on_queried_date_included(self):
        """An all-day event on Feb 14 should appear in a Feb 14 query."""
        ev = _mock_caldav_event(
            _make_ical_allday("a1", "Saturday Event", date(2026, 2, 14), date(2026, 2, 15))
        )
        la = ZoneInfo("America/Los_Angeles")
        start = datetime(2026, 2, 14, tzinfo=la)
        end = datetime(2026, 2, 15, tzinfo=la)

        result = self._query_events([ev], start, end)
        assert len(result) == 1
        assert result[0].summary == "Saturday Event"
        assert result[0].all_day is True

    def test_allday_event_on_next_day_excluded(self):
        """An all-day event on Feb 15 should NOT appear in a Feb 14 query.

        This is the core bug: CalDAV's UTC range [Feb 14 08:00Z, Feb 15 08:00Z]
        includes a DATE event on Feb 15 (midnight UTC), but locally it's the
        wrong day.
        """
        ev = _mock_caldav_event(
            _make_ical_allday("a2", "Sunday Leak", date(2026, 2, 15), date(2026, 2, 16))
        )
        la = ZoneInfo("America/Los_Angeles")
        start = datetime(2026, 2, 14, tzinfo=la)
        end = datetime(2026, 2, 15, tzinfo=la)

        result = self._query_events([ev], start, end)
        assert len(result) == 0

    def test_mixed_events_only_correct_day_returned(self):
        """Query for Feb 14 with a mix of valid and leaked events."""
        events = [
            # Valid: all-day on Feb 14
            _mock_caldav_event(
                _make_ical_allday("a3", "Walking Tour", date(2026, 2, 14), date(2026, 2, 15))
            ),
            # Valid: timed event on Feb 14
            _mock_caldav_event(
                _make_ical_timed(
                    "t1", "Meeting",
                    datetime(2026, 2, 14, 18, 0), datetime(2026, 2, 14, 18, 30)
                )
            ),
            # Leaked: all-day on Feb 15
            _mock_caldav_event(
                _make_ical_allday("a4", "Sunday Invoice", date(2026, 2, 15), date(2026, 2, 16))
            ),
        ]
        la = ZoneInfo("America/Los_Angeles")
        start = datetime(2026, 2, 14, tzinfo=la)
        end = datetime(2026, 2, 15, tzinfo=la)

        result = self._query_events(events, start, end)
        assert len(result) == 2
        summaries = {e.summary for e in result}
        assert summaries == {"Walking Tour", "Meeting"}

    def test_multiday_allday_event_overlapping_range(self):
        """A multi-day all-day event spanning Feb 13-16 should appear on Feb 14."""
        ev = _mock_caldav_event(
            _make_ical_allday("a5", "Conference", date(2026, 2, 13), date(2026, 2, 16))
        )
        la = ZoneInfo("America/Los_Angeles")
        start = datetime(2026, 2, 14, tzinfo=la)
        end = datetime(2026, 2, 15, tzinfo=la)

        result = self._query_events([ev], start, end)
        assert len(result) == 1
        assert result[0].summary == "Conference"

    def test_allday_event_ending_on_query_start_excluded(self):
        """An all-day event ending on Feb 14 (DTEND=Feb 14) shouldn't appear
        in a Feb 14 query — DTEND is exclusive for all-day events."""
        ev = _mock_caldav_event(
            _make_ical_allday("a6", "Yesterday", date(2026, 2, 13), date(2026, 2, 14))
        )
        la = ZoneInfo("America/Los_Angeles")
        start = datetime(2026, 2, 14, tzinfo=la)
        end = datetime(2026, 2, 15, tzinfo=la)

        result = self._query_events([ev], start, end)
        assert len(result) == 0

    def test_week_query_no_leak(self):
        """A week query should include all-day events within the week but not after."""
        events = [
            _mock_caldav_event(
                _make_ical_allday("w1", "Monday", date(2026, 2, 16), date(2026, 2, 17))
            ),
            _mock_caldav_event(
                _make_ical_allday("w2", "Next Monday", date(2026, 2, 23), date(2026, 2, 24))
            ),
        ]
        la = ZoneInfo("America/Los_Angeles")
        start = datetime(2026, 2, 14, tzinfo=la)
        end = datetime(2026, 2, 21, tzinfo=la)

        result = self._query_events(events, start, end)
        assert len(result) == 1
        assert result[0].summary == "Monday"

    def test_naive_datetime_no_filtering_regression(self):
        """When no timezone is provided (naive datetimes), all-day events
        should still work — no regression from the fix."""
        ev = _mock_caldav_event(
            _make_ical_allday("n1", "Naive Event", date(2026, 2, 14), date(2026, 2, 15))
        )
        start = datetime(2026, 2, 14)
        end = datetime(2026, 2, 15)

        result = self._query_events([ev], start, end)
        assert len(result) == 1
        assert result[0].summary == "Naive Event"
