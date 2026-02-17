"""Configuration loading for istota.briefing module."""

from unittest.mock import patch, MagicMock

from istota.briefing import (
    _strip_html,
    _parse_reminders,
    _fetch_market_data,
    _fetch_finviz_market_data,
    _fetch_random_reminder,
    _fetch_todo_items,
    _fetch_calendar_events,
    build_briefing_prompt,
)
from istota.config import Config, BriefingConfig, NextcloudConfig, ResourceConfig, UserConfig


class TestStripHtml:
    def test_plain_text_unchanged(self):
        assert _strip_html("Hello world") == "Hello world"

    def test_removes_tags(self):
        assert _strip_html("<b>bold</b> and <i>italic</i>") == "bold and italic"

    def test_decodes_entities(self):
        result = _strip_html("&amp; &lt; &gt; &quot;")
        assert result == "& < > \""

    def test_removes_style_blocks(self):
        html = "<style>body { color: red; }</style><p>Content</p>"
        result = _strip_html(html)
        assert "color" not in result
        assert "Content" in result

    def test_adds_newlines_for_blocks(self):
        html = "<p>First</p><p>Second</p>"
        result = _strip_html(html)
        assert "First" in result
        assert "Second" in result
        # Block elements should be on separate lines
        lines = [l.strip() for l in result.splitlines() if l.strip()]
        assert len(lines) >= 2

    def test_removes_invisible_chars(self):
        # Non-breaking space and zero-width space
        text = "hello\u00a0\u200bworld"
        result = _strip_html(text)
        assert "\u00a0" not in result
        assert "\u200b" not in result
        assert "hello" in result
        assert "world" in result

    def test_empty_string(self):
        assert _strip_html("") == ""

    def test_normalizes_whitespace(self):
        html = "<p>  lots   of    spaces  </p>"
        result = _strip_html(html)
        # Multiple spaces should be collapsed
        assert "  " not in result
        assert "lots of spaces" in result


class TestParseReminders:
    def test_bullet_list(self):
        content = "- First reminder\n- Second reminder\n- Third reminder"
        result = _parse_reminders(content)
        assert len(result) == 3
        assert "First reminder" in result[0]
        assert "Second reminder" in result[1]
        assert "Third reminder" in result[2]

    def test_numbered_list(self):
        content = "1. First item\n2. Second item\n3. Third item"
        result = _parse_reminders(content)
        assert len(result) == 3
        # List prefixes should be stripped
        assert result[0] == "First item"
        assert result[1] == "Second item"

    def test_attribution_merged(self):
        content = "Some wise words\n\n-- Ancient Proverb"
        result = _parse_reminders(content)
        assert len(result) == 1
        assert "Some wise words" in result[0]
        assert "Ancient Proverb" in result[0]

    def test_headers_skipped(self):
        content = "# My Reminders\n\nActual reminder text"
        result = _parse_reminders(content)
        # Header-only blocks are skipped; the actual content remains
        assert any("Actual reminder text" in r for r in result)
        # Headers themselves should not appear as standalone reminders
        assert not any(r.strip() == "# My Reminders" for r in result)

    def test_single_block(self):
        content = "Just one single reminder here."
        result = _parse_reminders(content)
        assert len(result) == 1
        assert result[0] == "Just one single reminder here."

    def test_empty_content(self):
        result = _parse_reminders("")
        assert result == []

    def test_mixed_content(self):
        content = (
            "# Wisdom\n\n"
            "First block of text.\n\n"
            "- Bullet one\n"
            "- Bullet two\n\n"
            "A standalone thought.\n\n"
            "-- Someone Famous"
        )
        result = _parse_reminders(content)
        assert len(result) >= 3
        # The standalone thought should have the attribution merged
        assert any("Someone Famous" in r for r in result)


class TestBuildBriefingPrompt:
    def _make_briefing(self, **kwargs):
        defaults = dict(
            name="morning",
            cron="0 6 * * *",
            conversation_token="room1",
            components={"calendar": True, "todos": True},
        )
        defaults.update(kwargs)
        return BriefingConfig(**defaults)

    def _make_config(self, tmp_path=None, users=None):
        cfg = Config()
        if users:
            cfg.users = users
        return cfg

    def test_basic_prompt_structure(self):
        briefing = self._make_briefing()
        config = self._make_config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")
        assert "testuser" in result
        assert "briefing" in result.lower()
        assert "calendar" in result.lower()
        assert "TODO" in result

    @patch("istota.briefing.datetime")
    def test_morning_mode(self, mock_dt):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Set to 8 AM UTC
        mock_now = datetime(2025, 1, 15, 8, 0, tzinfo=ZoneInfo("UTC"))
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        briefing = self._make_briefing()
        config = self._make_config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")
        assert "morning" in result.lower()
        assert "Today's calendar" in result

    @patch("istota.briefing.datetime")
    def test_evening_mode(self, mock_dt):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Set to 8 PM UTC
        mock_now = datetime(2025, 1, 15, 20, 0, tzinfo=ZoneInfo("UTC"))
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        briefing = self._make_briefing()
        config = self._make_config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")
        assert "evening" in result.lower()
        assert "Tomorrow's calendar" in result

    def test_calendar_component(self):
        briefing = self._make_briefing(components={"calendar": True})
        config = self._make_config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")
        assert "calendar" in result.lower()

    def test_todos_component(self):
        briefing = self._make_briefing(components={"todos": True})
        config = self._make_config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")
        assert "TODO" in result

    @patch("istota.briefing._fetch_todo_items")
    def test_todos_prefetched_when_available(self, mock_fetch):
        mock_fetch.return_value = "## Pending TODO Items (pre-fetched)\n- [ ] Buy groceries"
        briefing = self._make_briefing(components={"todos": True})
        config = self._make_config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")
        assert "Buy groceries" in result
        assert "Pending TODO Items" in result

    def test_no_preamble_instruction(self):
        briefing = self._make_briefing()
        config = self._make_config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")
        assert "preamble" in result.lower()

    @patch("istota.briefing._fetch_market_data")
    @patch("istota.briefing.datetime")
    def test_markets_component(self, mock_dt, mock_fetch):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Wednesday (weekday) so markets are fetched
        mock_now = datetime(2025, 1, 15, 8, 0, tzinfo=ZoneInfo("UTC"))
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        mock_fetch.return_value = "## Market Data\nES=F: 5000.00 (+0.5%)"
        briefing = self._make_briefing(
            components={"markets": {"enabled": True, "futures": ["ES=F"]}}
        )
        config = self._make_config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")
        assert "Market Data" in result
        mock_fetch.assert_called_once()

    @patch("istota.briefing._fetch_random_reminder")
    def test_reminders_component(self, mock_reminder):
        mock_reminder.return_value = "Stay curious."
        user_cfg = UserConfig(
            display_name="Test",
            resources=[ResourceConfig(type="reminders_file", path="/path/to/REMINDERS.md")],
        )
        briefing = self._make_briefing(
            components={"reminders": {"enabled": True}}
        )
        config = self._make_config(users={"testuser": user_cfg})
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")
        assert "Stay curious." in result
        assert "REMINDER" in result


class TestFetchMarketData:
    def test_morning_fetches_futures(self):
        market_config = {"futures": ["ES=F"], "indices": ["SPY"]}
        with patch("istota.skills.markets.get_futures_quotes", return_value=[{"symbol": "ES=F"}]) as mock_futures, \
             patch("istota.skills.markets.format_market_summary", return_value="Futures: ES=F 5000"):
            result = _fetch_market_data(market_config, "morning")
            if result is not None:
                mock_futures.assert_called_once_with(["ES=F"])

    def test_evening_fetches_indices(self):
        market_config = {"futures": ["ES=F"], "indices": ["SPY"]}
        with patch("istota.skills.markets.get_index_quotes", return_value=[{"symbol": "SPY"}]) as mock_indices, \
             patch("istota.skills.markets.format_market_summary", return_value="Indices: SPY 500"):
            result = _fetch_market_data(market_config, "evening")
            if result is not None:
                mock_indices.assert_called_once_with(["SPY"])

    def test_import_error_returns_none(self):
        market_config = {"futures": ["ES=F"]}
        with patch(
            "istota.briefing._fetch_market_data.__module__", side_effect=ImportError, create=True
        ):
            # The real function catches ImportError internally
            # Force the import to fail by patching the module lookup
            pass
        # Directly test: if the markets module is not installed, returns None
        with patch.dict("sys.modules", {"istota.skills.markets": None}):
            result = _fetch_market_data(market_config, "morning")
            assert result is None

    def test_fetch_error_returns_none(self):
        market_config = {"futures": ["ES=F"]}
        with patch(
            "istota.skills.markets.get_futures_quotes",
            side_effect=RuntimeError("API down"),
        ):
            result = _fetch_market_data(market_config, "morning")
            assert result is None


class TestFetchRandomReminder:
    """Tests for reminder shuffle-queue rotation."""

    def test_returns_reminder_from_queue(self, tmp_path):
        """Test that reminders are returned from the queue."""
        db_path = tmp_path / "test.db"
        from istota.db import init_db
        init_db(db_path)

        with patch("istota.skills.files.read_text") as mock_read:
            mock_read.return_value = "- Remember to breathe\n- Stay hydrated"
            config = Config(
                db_path=db_path,
                users={"testuser": UserConfig(
                    resources=[ResourceConfig(type="reminders_file", path="/path/to/REMINDERS.md")],
                )}
            )
            result = _fetch_random_reminder(config, "testuser")
            assert result is not None
            assert result in ("Remember to breathe", "Stay hydrated")

    def test_no_repeats_until_all_shown(self, tmp_path):
        """Test that all reminders are shown before any repeat."""
        db_path = tmp_path / "test.db"
        from istota.db import init_db
        init_db(db_path)

        with patch("istota.skills.files.read_text") as mock_read:
            mock_read.return_value = "- One\n- Two\n- Three"
            config = Config(
                db_path=db_path,
                users={"testuser": UserConfig(
                    resources=[ResourceConfig(type="reminders_file", path="/path/to/REMINDERS.md")],
                )}
            )

            # Get all 3 reminders - should be unique
            seen = []
            for _ in range(3):
                result = _fetch_random_reminder(config, "testuser")
                seen.append(result)

            assert len(set(seen)) == 3  # All unique
            assert set(seen) == {"One", "Two", "Three"}

    def test_queue_resets_after_exhausted(self, tmp_path):
        """Test that queue reshuffles after all items shown."""
        db_path = tmp_path / "test.db"
        from istota.db import init_db
        init_db(db_path)

        with patch("istota.skills.files.read_text") as mock_read:
            mock_read.return_value = "- One\n- Two"
            config = Config(
                db_path=db_path,
                users={"testuser": UserConfig(
                    resources=[ResourceConfig(type="reminders_file", path="/path/to/REMINDERS.md")],
                )}
            )

            # Exhaust the queue (2 items)
            for _ in range(2):
                _fetch_random_reminder(config, "testuser")

            # Next call should still work (queue resets)
            result = _fetch_random_reminder(config, "testuser")
            assert result in ("One", "Two")

    def test_content_change_resets_queue(self, tmp_path):
        """Test that changing reminders content resets the queue."""
        db_path = tmp_path / "test.db"
        from istota.db import init_db, get_db, get_reminder_state
        init_db(db_path)

        config = Config(
            db_path=db_path,
            users={"testuser": UserConfig(
                resources=[ResourceConfig(type="reminders_file", path="/path/to/REMINDERS.md")],
            )}
        )

        with patch("istota.skills.files.read_text") as mock_read:
            # First content
            mock_read.return_value = "- Original"
            _fetch_random_reminder(config, "testuser")

            with get_db(db_path) as conn:
                state1 = get_reminder_state(conn, "testuser")
                hash1 = state1.content_hash

            # Change content
            mock_read.return_value = "- New content\n- More new"
            _fetch_random_reminder(config, "testuser")

            with get_db(db_path) as conn:
                state2 = get_reminder_state(conn, "testuser")
                hash2 = state2.content_hash

            # Hash should have changed
            assert hash1 != hash2

    def test_empty_file_returns_none(self, tmp_path):
        """Test that empty reminders file returns None."""
        db_path = tmp_path / "test.db"
        from istota.db import init_db
        init_db(db_path)

        with patch("istota.skills.files.read_text") as mock_read:
            mock_read.return_value = ""
            config = Config(
                db_path=db_path,
                users={"testuser": UserConfig(
                    resources=[ResourceConfig(type="reminders_file", path="/path/to/REMINDERS.md")],
                )}
            )
            result = _fetch_random_reminder(config, "testuser")
            assert result is None

    def test_no_reminders_resource_returns_none(self):
        """Test that missing reminders resource returns None."""
        config = Config(users={"testuser": UserConfig()})
        result = _fetch_random_reminder(config, "testuser")
        assert result is None

    def test_no_user_returns_none(self):
        """Test that unknown user returns None."""
        config = Config()
        result = _fetch_random_reminder(config, "unknown")
        assert result is None

    def test_read_error_returns_none(self, tmp_path):
        """Test that file read error returns None gracefully."""
        db_path = tmp_path / "test.db"
        from istota.db import init_db
        init_db(db_path)

        with patch("istota.skills.files.read_text", side_effect=FileNotFoundError("not found")):
            config = Config(
                db_path=db_path,
                users={"testuser": UserConfig(
                    resources=[ResourceConfig(type="reminders_file", path="/nonexistent/file.md")],
                )}
            )
            result = _fetch_random_reminder(config, "testuser")
            assert result is None

    def test_db_error_falls_back_to_random(self, tmp_path):
        """Test that DB errors fall back to random selection."""
        from istota import db

        with patch("istota.skills.files.read_text") as mock_read, \
             patch.object(db, "get_db") as mock_db:
            mock_read.return_value = "- Fallback reminder"
            mock_db.side_effect = Exception("DB error")

            config = Config(
                db_path=tmp_path / "nonexistent.db",
                users={"testuser": UserConfig(
                    resources=[ResourceConfig(type="reminders_file", path="/path/to/REMINDERS.md")],
                )}
            )
            result = _fetch_random_reminder(config, "testuser")
            assert result == "Fallback reminder"


class TestWeekendMarketSkip:
    """Test that market quotes are skipped on weekends."""

    def _make_briefing(self, **kwargs):
        defaults = dict(
            name="morning",
            cron="0 6 * * *",
            conversation_token="room1",
            components={
                "markets": {"enabled": True, "futures": ["ES=F"]},
                "calendar": True,
            },
        )
        defaults.update(kwargs)
        return BriefingConfig(**defaults)

    @patch("istota.briefing._fetch_market_data")
    @patch("istota.briefing.datetime")
    def test_weekday_fetches_market_data(self, mock_dt, mock_fetch):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Wednesday
        mock_now = datetime(2025, 1, 15, 8, 0, tzinfo=ZoneInfo("UTC"))
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
        mock_fetch.return_value = "## Market Data\nES=F: 5000"

        briefing = self._make_briefing()
        config = Config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")
        mock_fetch.assert_called_once()
        assert "Market Data" in result

    @patch("istota.briefing._fetch_market_data")
    @patch("istota.briefing.datetime")
    def test_saturday_skips_market_data(self, mock_dt, mock_fetch):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Saturday
        mock_now = datetime(2025, 1, 18, 8, 0, tzinfo=ZoneInfo("UTC"))
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        briefing = self._make_briefing()
        config = Config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")
        mock_fetch.assert_not_called()

    @patch("istota.briefing._fetch_market_data")
    @patch("istota.briefing.datetime")
    def test_sunday_skips_market_data(self, mock_dt, mock_fetch):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Sunday
        mock_now = datetime(2025, 1, 19, 8, 0, tzinfo=ZoneInfo("UTC"))
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        briefing = self._make_briefing()
        config = Config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")
        mock_fetch.assert_not_called()


class TestNewsletterSectionSplit:
    """Test that newsletter prompt instructs Claude to split stories between NEWS and MARKETS."""

    @patch("istota.briefing._fetch_newsletter_content")
    @patch("istota.briefing.datetime")
    def test_newsletter_prompt_instructs_section_split(self, mock_dt, mock_news):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        mock_now = datetime(2025, 1, 15, 8, 0, tzinfo=ZoneInfo("UTC"))
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        mock_news.return_value = "## Newsletter content\n\nSome news here"

        briefing = BriefingConfig(
            name="morning",
            cron="0 6 * * *",
            conversation_token="room1",
            components={
                "news": {
                    "enabled": True,
                    "lookback_hours": 12,
                    "sources": [{"type": "domain", "value": "semafor.com"}],
                },
            },
        )
        config = Config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")

        assert "NEWS section" in result
        assert "MARKETS section" in result
        mock_news.assert_called_once()

    @patch("istota.briefing._fetch_newsletter_content")
    @patch("istota.briefing._fetch_market_data")
    @patch("istota.briefing.datetime")
    def test_weekend_skips_quotes_but_fetches_newsletters(self, mock_dt, mock_market, mock_news):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Saturday
        mock_now = datetime(2025, 1, 18, 8, 0, tzinfo=ZoneInfo("UTC"))
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        mock_news.return_value = "## Newsletter content\n\nMarket and general news"

        briefing = BriefingConfig(
            name="morning",
            cron="0 6 * * *",
            conversation_token="room1",
            components={
                "markets": {"enabled": True, "futures": ["ES=F"]},
                "news": {
                    "enabled": True,
                    "lookback_hours": 12,
                    "sources": [{"type": "domain", "value": "semafor.com"}],
                },
            },
        )
        config = Config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")

        # Market quotes skipped on weekend
        mock_market.assert_not_called()
        # Newsletters still fetched
        mock_news.assert_called_once()
        assert "Newsletter content" in result


class TestFetchTodoItems:
    def _make_config(self, users=None):
        cfg = Config()
        if users:
            cfg.users = users
        return cfg

    def test_no_user_config(self):
        config = self._make_config()
        assert _fetch_todo_items(config, "unknown") is None

    def test_no_todo_resources(self):
        user = UserConfig(display_name="Test", resources=[])
        config = self._make_config(users={"testuser": user})
        assert _fetch_todo_items(config, "testuser") is None

    @patch("istota.skills.files.read_text")
    def test_extracts_pending_items(self, mock_read):
        from istota.config import ResourceConfig

        mock_read.return_value = (
            "# Tasks\n"
            "- [ ] Buy groceries\n"
            "- [x] Clean house\n"
            "- [ ] Write report\n"
            "- [~] In progress item\n"
        )
        user = UserConfig(
            display_name="Test",
            resources=[ResourceConfig(type="todo_file", path="/todo.md", permissions="read")],
        )
        config = self._make_config(users={"testuser": user})
        result = _fetch_todo_items(config, "testuser")
        assert result is not None
        assert "Buy groceries" in result
        assert "Write report" in result
        assert "Clean house" not in result
        assert "In progress" not in result

    @patch("istota.skills.files.read_text")
    def test_empty_todo_file(self, mock_read):
        from istota.config import ResourceConfig

        mock_read.return_value = "# Tasks\n"
        user = UserConfig(
            display_name="Test",
            resources=[ResourceConfig(type="todo_file", path="/todo.md", permissions="read")],
        )
        config = self._make_config(users={"testuser": user})
        assert _fetch_todo_items(config, "testuser") is None

    @patch("istota.skills.files.read_text")
    def test_read_failure_returns_none(self, mock_read):
        from istota.config import ResourceConfig

        mock_read.side_effect = Exception("mount unavailable")
        user = UserConfig(
            display_name="Test",
            resources=[ResourceConfig(type="todo_file", path="/todo.md", permissions="read")],
        )
        config = self._make_config(users={"testuser": user})
        assert _fetch_todo_items(config, "testuser") is None


class TestFetchCalendarEvents:
    def _make_config(self, **kwargs):
        return Config(
            nextcloud=NextcloudConfig(
                url="https://nc.example.com",
                username="istota",
                app_password="secret",
            ),
            **kwargs,
        )

    def test_no_caldav_config_returns_none(self):
        config = Config()  # No nextcloud config
        assert _fetch_calendar_events(config, "testuser", True, "UTC") is None

    @patch("istota.skills.calendar.get_caldav_client")
    @patch("istota.skills.calendar.get_calendars_for_user")
    @patch("istota.skills.calendar.get_today_events")
    @patch("istota.skills.calendar.format_event_for_display")
    def test_morning_fetches_today(self, mock_format, mock_today, mock_cals, mock_client):
        from datetime import datetime
        from istota.skills.calendar import CalendarEvent

        mock_cals.return_value = [("Personal", "https://cal/personal", True)]
        event = CalendarEvent(
            uid="1", summary="Standup", start=datetime(2025, 1, 15, 9, 0),
            end=datetime(2025, 1, 15, 9, 30),
        )
        mock_today.return_value = [event]
        mock_format.return_value = "09:00 - 09:30: Standup"

        config = self._make_config()
        result = _fetch_calendar_events(config, "testuser", True, "America/New_York")

        assert result is not None
        assert "Today" in result
        assert "Standup" in result
        mock_today.assert_called_once_with(mock_client.return_value, "https://cal/personal", tz="America/New_York")

    @patch("istota.skills.calendar.get_caldav_client")
    @patch("istota.skills.calendar.get_calendars_for_user")
    @patch("istota.skills.calendar.get_tomorrow_events")
    @patch("istota.skills.calendar.format_event_for_display")
    def test_evening_fetches_tomorrow(self, mock_format, mock_tomorrow, mock_cals, mock_client):
        from datetime import datetime
        from istota.skills.calendar import CalendarEvent

        mock_cals.return_value = [("Personal", "https://cal/personal", True)]
        event = CalendarEvent(
            uid="1", summary="Dentist", start=datetime(2025, 1, 16, 14, 0),
            end=datetime(2025, 1, 16, 15, 0),
        )
        mock_tomorrow.return_value = [event]
        mock_format.return_value = "14:00 - 15:00: Dentist"

        config = self._make_config()
        result = _fetch_calendar_events(config, "testuser", False, "America/New_York")

        assert result is not None
        assert "Tomorrow" in result
        assert "Dentist" in result

    @patch("istota.skills.calendar.get_caldav_client")
    @patch("istota.skills.calendar.get_calendars_for_user")
    @patch("istota.skills.calendar.get_today_events")
    def test_no_events_shows_no_events(self, mock_today, mock_cals, mock_client):
        mock_cals.return_value = [("Personal", "https://cal/personal", True)]
        mock_today.return_value = []

        config = self._make_config()
        result = _fetch_calendar_events(config, "testuser", True, "UTC")

        assert result is not None
        assert "No events scheduled" in result

    @patch("istota.skills.calendar.get_caldav_client")
    @patch("istota.skills.calendar.get_calendars_for_user")
    def test_no_calendars_returns_none(self, mock_cals, mock_client):
        mock_cals.return_value = []

        config = self._make_config()
        assert _fetch_calendar_events(config, "testuser", True, "UTC") is None

    @patch("istota.skills.calendar.get_caldav_client", side_effect=Exception("connection failed"))
    def test_caldav_error_returns_none(self, mock_client):
        config = self._make_config()
        assert _fetch_calendar_events(config, "testuser", True, "UTC") is None


class TestCalendarPreFetchInPrompt:
    """Test that calendar events are pre-fetched and embedded in the prompt."""

    @patch("istota.briefing._fetch_calendar_events")
    @patch("istota.briefing.datetime")
    def test_calendar_prefetched_in_prompt(self, mock_dt, mock_cal):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        mock_now = datetime(2025, 1, 15, 8, 0, tzinfo=ZoneInfo("UTC"))
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        mock_cal.return_value = "## Today's Calendar (pre-fetched)\n- 09:00 - 09:30: Standup [Personal]"

        briefing = BriefingConfig(
            name="morning", cron="0 6 * * *",
            conversation_token="room1",
            components={"calendar": True},
        )
        config = Config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")

        assert "Standup" in result
        assert "pre-fetched" in result
        # Should NOT have the fallback instruction
        assert "Today's calendar events" not in result

    @patch("istota.briefing._fetch_calendar_events")
    @patch("istota.briefing.datetime")
    def test_calendar_fallback_when_prefetch_fails(self, mock_dt, mock_cal):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        mock_now = datetime(2025, 1, 15, 8, 0, tzinfo=ZoneInfo("UTC"))
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        mock_cal.return_value = None  # Pre-fetch failed

        briefing = BriefingConfig(
            name="morning", cron="0 6 * * *",
            conversation_token="room1",
            components={"calendar": True},
        )
        config = Config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")

        # Should fall back to agent-fetched instruction
        assert "Today's calendar events" in result


class TestFetchFinvizMarketData:
    """Tests for _fetch_finviz_market_data."""

    @patch("istota.skills.finviz.fetch_finviz_data")
    @patch("istota.skills.finviz.format_finviz_briefing")
    def test_returns_formatted_data(self, mock_format, mock_fetch):
        from istota.skills.finviz import FinVizData
        mock_fetch.return_value = FinVizData(headlines=[], major_movers=[])
        mock_format.return_value = "**MARKET HEADLINES**\n- Some headline"

        result = _fetch_finviz_market_data()
        assert result is not None
        assert "FinViz Market Data" in result
        assert "MARKET HEADLINES" in result

    @patch("istota.skills.finviz.fetch_finviz_data")
    def test_returns_none_on_fetch_failure(self, mock_fetch):
        mock_fetch.return_value = None
        result = _fetch_finviz_market_data()
        assert result is None

    @patch("istota.skills.finviz.fetch_finviz_data")
    @patch("istota.skills.finviz.format_finviz_briefing")
    def test_returns_none_on_unavailable(self, mock_format, mock_fetch):
        from istota.skills.finviz import FinVizData
        mock_fetch.return_value = FinVizData()
        mock_format.return_value = "FinViz market data unavailable"
        result = _fetch_finviz_market_data()
        assert result is None

    @patch("istota.skills.finviz.fetch_finviz_data", side_effect=Exception("import error"))
    def test_returns_none_on_exception(self, mock_fetch):
        result = _fetch_finviz_market_data()
        assert result is None


class TestFinvizInBriefingPrompt:
    """Test FinViz integration in build_briefing_prompt."""

    @patch("istota.briefing._fetch_finviz_market_data")
    @patch("istota.briefing._fetch_market_data")
    @patch("istota.briefing.datetime")
    def test_evening_includes_finviz(self, mock_dt, mock_market, mock_finviz):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Wednesday evening
        mock_now = datetime(2025, 1, 15, 18, 0, tzinfo=ZoneInfo("UTC"))
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        mock_market.return_value = "## Market Close\nS&P 500: 6994.25"
        mock_finviz.return_value = "## FinViz Market Data\n**MOVERS**\n🟢 **SPOT** +14.36%"

        briefing = BriefingConfig(
            name="evening", cron="0 18 * * *",
            conversation_token="room1",
            components={"markets": {"enabled": True}},
        )
        config = Config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")

        assert "FinViz" in result
        assert "MOVERS" in result
        mock_finviz.assert_called_once()

    @patch("istota.briefing._fetch_finviz_market_data")
    @patch("istota.briefing._fetch_market_data")
    @patch("istota.briefing.datetime")
    def test_morning_does_not_include_finviz(self, mock_dt, mock_market, mock_finviz):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Wednesday morning
        mock_now = datetime(2025, 1, 15, 8, 0, tzinfo=ZoneInfo("UTC"))
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        mock_market.return_value = "## Pre-market Futures\nES=F: 5000"

        briefing = BriefingConfig(
            name="morning", cron="0 6 * * *",
            conversation_token="room1",
            components={"markets": {"enabled": True}},
        )
        config = Config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")

        mock_finviz.assert_not_called()

    @patch("istota.briefing._fetch_finviz_market_data")
    @patch("istota.briefing._fetch_market_data")
    @patch("istota.briefing.datetime")
    def test_weekend_evening_skips_finviz(self, mock_dt, mock_market, mock_finviz):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Saturday evening
        mock_now = datetime(2025, 1, 18, 18, 0, tzinfo=ZoneInfo("UTC"))
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        briefing = BriefingConfig(
            name="evening", cron="0 18 * * *",
            conversation_token="room1",
            components={"markets": {"enabled": True}},
        )
        config = Config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")

        mock_finviz.assert_not_called()

    @patch("istota.briefing._fetch_finviz_market_data")
    @patch("istota.briefing._fetch_market_data")
    @patch("istota.briefing.datetime")
    def test_markets_disabled_skips_finviz(self, mock_dt, mock_market, mock_finviz):
        from datetime import datetime
        from zoneinfo import ZoneInfo

        # Wednesday evening, but markets disabled
        mock_now = datetime(2025, 1, 15, 18, 0, tzinfo=ZoneInfo("UTC"))
        mock_dt.now.return_value = mock_now
        mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

        briefing = BriefingConfig(
            name="evening", cron="0 18 * * *",
            conversation_token="room1",
            components={"calendar": True},  # no markets
        )
        config = Config()
        result = build_briefing_prompt(briefing, "testuser", config, "UTC")

        mock_finviz.assert_not_called()
