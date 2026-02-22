"""Tests for calendar skill — all-day event timezone filtering + CLI."""

from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from istota.skills.calendar import (
    get_events, get_tomorrow_events, get_today_events,
    cmd_update, cmd_list, build_parser, main, _parse_datetime, _get_date_range,
    update_event,
)


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


# =============================================================================
# CLI Tests
# =============================================================================


class TestParseDatetime:
    def test_parses_space_separated(self):
        result = _parse_datetime("2026-03-15 14:30")
        assert result == datetime(2026, 3, 15, 14, 30)

    def test_parses_t_separated(self):
        result = _parse_datetime("2026-03-15T14:30")
        assert result == datetime(2026, 3, 15, 14, 30)

    def test_parses_with_seconds(self):
        result = _parse_datetime("2026-03-15 14:30:45")
        assert result == datetime(2026, 3, 15, 14, 30, 45)

    def test_raises_on_invalid(self):
        with pytest.raises(ValueError, match="Cannot parse datetime"):
            _parse_datetime("not-a-date")


class TestGetDateRange:
    def test_week_flag(self):
        args = MagicMock(week=True, date="today", tz=None)
        start, end, label = _get_date_range(args)
        assert (end - start).days == 7
        assert label == "week"

    def test_date_flag(self):
        args = MagicMock(week=False, date="2026-03-15", tz=None)
        start, end, label = _get_date_range(args)
        assert start == datetime(2026, 3, 15)
        assert (end - start).days == 1
        assert label == "2026-03-15"


class TestBuildParserUpdateSubcommand:
    def test_update_subcommand_exists(self):
        parser = build_parser()
        args = parser.parse_args([
            "update", "--calendar", "https://cal/url", "--uid", "abc123",
            "--summary", "New Title",
        ])
        assert args.command == "update"
        assert args.calendar == "https://cal/url"
        assert args.uid == "abc123"
        assert args.summary == "New Title"

    def test_update_clear_flags(self):
        parser = build_parser()
        args = parser.parse_args([
            "update", "--calendar", "https://cal/url", "--uid", "abc123",
            "--clear-location", "--clear-description",
        ])
        assert args.clear_location is True
        assert args.clear_description is True

    def test_list_week_flag(self):
        parser = build_parser()
        args = parser.parse_args(["list", "--week", "--tz", "UTC"])
        assert args.week is True

    def test_list_week_and_date_mutually_exclusive(self):
        parser = build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args(["list", "--week", "--date", "tomorrow"])


class TestCmdUpdate:
    @patch("istota.skills.calendar.update_event")
    @patch("istota.skills.calendar._get_client_from_env")
    def test_update_summary(self, mock_client_fn, mock_update):
        mock_update.return_value = True
        parser = build_parser()
        args = parser.parse_args([
            "update", "--calendar", "https://cal", "--uid", "u1",
            "--summary", "New",
        ])
        result = cmd_update(args)
        assert result["status"] == "ok"
        assert "summary" in result["updated_fields"]
        mock_update.assert_called_once()
        call_kwargs = mock_update.call_args
        assert call_kwargs[1]["summary"] == "New"

    @patch("istota.skills.calendar.update_event")
    @patch("istota.skills.calendar._get_client_from_env")
    def test_update_not_found(self, mock_client_fn, mock_update):
        mock_update.return_value = False
        parser = build_parser()
        args = parser.parse_args([
            "update", "--calendar", "https://cal", "--uid", "u1",
            "--summary", "New",
        ])
        result = cmd_update(args)
        assert result["status"] == "error"
        assert "not found" in result["error"]

    @patch("istota.skills.calendar._get_client_from_env")
    def test_update_no_fields_error(self, mock_client_fn):
        parser = build_parser()
        args = parser.parse_args([
            "update", "--calendar", "https://cal", "--uid", "u1",
        ])
        result = cmd_update(args)
        assert result["status"] == "error"
        assert "No fields to update" in result["error"]

    @patch("istota.skills.calendar.update_event")
    @patch("istota.skills.calendar._get_client_from_env")
    def test_clear_location_passes_empty_string(self, mock_client_fn, mock_update):
        mock_update.return_value = True
        parser = build_parser()
        args = parser.parse_args([
            "update", "--calendar", "https://cal", "--uid", "u1",
            "--clear-location",
        ])
        result = cmd_update(args)
        assert result["status"] == "ok"
        call_kwargs = mock_update.call_args[1]
        assert call_kwargs["location"] == ""

    @patch("istota.skills.calendar.update_event")
    @patch("istota.skills.calendar._get_client_from_env")
    def test_update_start_end_parsed(self, mock_client_fn, mock_update):
        mock_update.return_value = True
        parser = build_parser()
        args = parser.parse_args([
            "update", "--calendar", "https://cal", "--uid", "u1",
            "--start", "2026-03-15 14:00", "--end", "2026-03-15 15:00",
        ])
        result = cmd_update(args)
        assert result["status"] == "ok"
        call_kwargs = mock_update.call_args[1]
        assert call_kwargs["start"] == datetime(2026, 3, 15, 14, 0)
        assert call_kwargs["end"] == datetime(2026, 3, 15, 15, 0)


class TestCmdListWeek:
    @patch("istota.skills.calendar.list_calendars")
    @patch("istota.skills.calendar._get_client_from_env")
    def test_list_week_returns_7_day_label(self, mock_client_fn, mock_list_cals):
        mock_list_cals.return_value = []
        parser = build_parser()
        args = parser.parse_args(["list", "--week"])
        result = cmd_list(args)
        assert result["date"] == "week"
        assert result["status"] == "ok"
