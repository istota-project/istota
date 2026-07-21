"""Tests for istota.skills.briefing module."""

from unittest.mock import patch, MagicMock

import istota.skills.briefing as briefing_mod
from istota.skills.briefing import (
    _strip_html,
    strip_markdown,
    _parse_reminders,
    _fetch_market_data,
    _fetch_finviz_market_data,
    _fetch_random_reminder,
    _fetch_calendar_events,
    _fetch_headlines,
    _briefing_digest_key,
    load_previous_briefing_digest,
    save_briefing_digest,
    HEADLINE_SOURCES,
)
from istota.config import Config, BriefingConfig, BrowserConfig, NextcloudConfig, ResourceConfig, UserConfig


def test_legacy_generator_removed():
    """The legacy component-based generator is gone — blocks are the sole path."""
    assert not hasattr(briefing_mod, "build_briefing_prompt")
    assert not hasattr(briefing_mod, "_component_enabled")
    assert not hasattr(briefing_mod, "_fetch_todo_items")
    assert not hasattr(briefing_mod, "_fetch_newsletter_content")


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


class TestStripMarkdown:
    """strip_markdown flattens markdown for plain-text (email) delivery."""

    def test_strips_atx_headings(self):
        # Regression: structured briefing sources can emit `## ` verbatim; it
        # must not survive into plain-text email.
        assert strip_markdown("## Market Close:") == "Market Close:"
        assert strip_markdown("### Sub") == "Sub"
        assert strip_markdown("###### Deep") == "Deep"

    def test_strips_heading_only_at_line_start(self):
        # A `#` mid-line (e.g. "issue #42") is not a heading and stays.
        assert strip_markdown("see issue #42") == "see issue #42"

    def test_strips_heading_with_leading_indent(self):
        assert strip_markdown("   ## Indented") == "Indented"

    def test_strips_headings_multiline(self):
        text = "## Market Close:\n  🔴 S&P 500: 7,443.28\n  As of: 06:08"
        result = strip_markdown(text)
        assert "## " not in result
        assert result.startswith("Market Close:")
        assert "🔴 S&P 500: 7,443.28" in result

    def test_strips_bold_italic_and_links(self):
        assert strip_markdown("**bold** and *italic* and _under_") == "bold and italic and under"
        assert strip_markdown("[text](https://x.com)") == "text"

    def test_bold_market_label_flattens(self):
        # The new market source label round-trips to a clean plain label.
        assert strip_markdown("**Market Close:**") == "Market Close:"


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
        # If the markets module is not installed, returns None
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
        # Copied verbatim into the heading-forbidding calendar block — no `## `.
        assert "## " not in result
        # The briefing path uses `with get_caldav_client(...) as client:` so
        # the calendar-event call receives the context-manager-entered client.
        mock_today.assert_called_once_with(
            mock_client.return_value.__enter__.return_value,
            "https://cal/personal",
            tz="America/New_York",
        )

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


class TestFetchFinvizMarketData:
    """Tests for _fetch_finviz_market_data."""

    @patch("istota.skills.markets.finviz.fetch_finviz_data")
    @patch("istota.skills.markets.finviz.format_finviz_briefing")
    def test_returns_formatted_data(self, mock_format, mock_fetch):
        from istota.skills.markets.finviz import FinVizData
        mock_fetch.return_value = FinVizData(headlines=[], major_movers=[])
        mock_format.return_value = "**MARKET HEADLINES**\n- Some headline"

        result = _fetch_finviz_market_data()
        assert result is not None
        assert "FinViz Market Data" in result
        assert "MARKET HEADLINES" in result
        # Copied verbatim into the heading-forbidding markets block — no `## `.
        assert "## " not in result

    @patch("istota.skills.markets.finviz.fetch_finviz_data")
    def test_returns_none_on_fetch_failure(self, mock_fetch):
        mock_fetch.return_value = None
        result = _fetch_finviz_market_data()
        assert result is None

    @patch("istota.skills.markets.finviz.fetch_finviz_data")
    @patch("istota.skills.markets.finviz.format_finviz_briefing")
    def test_returns_none_on_unavailable(self, mock_format, mock_fetch):
        from istota.skills.markets.finviz import FinVizData
        mock_fetch.return_value = FinVizData()
        mock_format.return_value = "FinViz market data unavailable"
        result = _fetch_finviz_market_data()
        assert result is None

    @patch("istota.skills.markets.finviz.fetch_finviz_data", side_effect=Exception("import error"))
    def test_returns_none_on_exception(self, mock_fetch):
        result = _fetch_finviz_market_data()
        assert result is None


class TestBriefingDigest:
    def _make_config(self, db_path):
        cfg = Config()
        cfg.db_path = db_path
        return cfg

    def test_digest_key_with_channel(self):
        key = _briefing_digest_key("room1")
        assert key == "digest:room1"

    def test_digest_key_without_channel(self):
        key = _briefing_digest_key()
        assert key == "digest:default"

    def test_load_returns_none_when_no_entry(self, db_path):
        cfg = self._make_config(db_path)
        result = load_previous_briefing_digest("alice", cfg, conversation_token="room1")
        assert result is None

    def test_save_and_load_roundtrip(self, db_path):
        cfg = self._make_config(db_path)

        save_briefing_digest("alice", cfg, "📰 NEWS\n- Story A\n- Story B", conversation_token="room1")

        result = load_previous_briefing_digest("alice", cfg, conversation_token="room1")
        assert result is not None
        assert "Story A" in result
        assert "Story B" in result
        assert "Generated:" in result

    def test_save_overwrites_previous(self, db_path):
        cfg = self._make_config(db_path)

        save_briefing_digest("alice", cfg, "📰 First briefing", conversation_token="room1")
        save_briefing_digest("alice", cfg, "📰 Second briefing", conversation_token="room1")

        result = load_previous_briefing_digest("alice", cfg, conversation_token="room1")
        assert "Second briefing" in result
        assert "First briefing" not in result

class TestHeadlineSources:
    """Test the HEADLINE_SOURCES registry."""

    def test_all_expected_sources_present(self):
        expected = {"ap", "reuters", "guardian", "ft", "aljazeera", "lemonde", "spiegel"}
        assert expected <= set(HEADLINE_SOURCES.keys())

    def test_sources_have_required_fields(self):
        for key, source in HEADLINE_SOURCES.items():
            assert "url" in source, f"{key} missing url"
            assert "name" in source, f"{key} missing name"
            assert source["url"].startswith("http"), f"{key} url invalid"


class TestFetchHeadlines:
    """Tests for _fetch_headlines."""

    def _make_config(self, browser_enabled=True):
        return Config(
            browser=BrowserConfig(
                enabled=browser_enabled,
                api_url="http://localhost:9223",
            ),
        )

    def test_browser_disabled_returns_none(self):
        config = self._make_config(browser_enabled=False)
        result = _fetch_headlines({"sources": ["ap"]}, config)
        assert result is None

    def test_empty_sources_returns_none(self):
        config = self._make_config()
        result = _fetch_headlines({"sources": []}, config)
        assert result is None

    def test_no_sources_key_returns_none(self):
        config = self._make_config()
        result = _fetch_headlines({}, config)
        assert result is None

    def test_unknown_source_skipped(self):
        config = self._make_config()
        with patch("istota.skills.briefing.httpx") as mock_httpx:
            result = _fetch_headlines({"sources": ["nonexistent"]}, config)
        assert result is None

    @patch("istota.skills.briefing.httpx")
    def test_successful_fetch(self, mock_httpx):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "ok",
            "text": "Breaking: Major event happens. More details follow.",
        }
        mock_httpx.post.return_value = mock_response

        config = self._make_config()
        result = _fetch_headlines({"sources": ["ap"]}, config)

        assert result is not None
        assert "AP News" in result
        assert "Major event happens" in result
        assert "pre-fetched" in result.lower()

    @patch("istota.skills.briefing.httpx")
    def test_multiple_sources(self, mock_httpx):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "status": "ok",
            "text": "Some headlines here.",
        }
        mock_httpx.post.return_value = mock_response

        config = self._make_config()
        result = _fetch_headlines({"sources": ["ap", "reuters"]}, config)

        assert result is not None
        assert "AP News" in result
        assert "Reuters" in result

    @patch("istota.skills.briefing.httpx")
    def test_truncates_long_pages(self, mock_httpx):
        long_text = "x" * 10000
        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "ok", "text": long_text}
        mock_httpx.post.return_value = mock_response

        config = self._make_config()
        result = _fetch_headlines({"sources": ["ap"]}, config)

        assert result is not None
        assert "[truncated]" in result

    @patch("istota.skills.briefing.httpx")
    def test_fetch_error_skips_source(self, mock_httpx):
        mock_httpx.post.side_effect = Exception("connection refused")

        config = self._make_config()
        result = _fetch_headlines({"sources": ["ap"]}, config)
        assert result is None

    @patch("istota.skills.briefing.httpx")
    def test_non_ok_status_skips_source(self, mock_httpx):
        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "error", "error": "captcha"}
        mock_httpx.post.return_value = mock_response

        config = self._make_config()
        result = _fetch_headlines({"sources": ["ap"]}, config)
        assert result is None

    @patch("istota.skills.briefing.httpx")
    def test_empty_text_skips_source(self, mock_httpx):
        mock_response = MagicMock()
        mock_response.json.return_value = {"status": "ok", "text": ""}
        mock_httpx.post.return_value = mock_response

        config = self._make_config()
        result = _fetch_headlines({"sources": ["ap"]}, config)
        assert result is None

    @patch("istota.skills.briefing.httpx")
    def test_partial_failure_still_returns_successful(self, mock_httpx):
        """If one source fails but another succeeds, return the successful one."""
        call_count = [0]

        def side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("timeout")
            mock_resp = MagicMock()
            mock_resp.json.return_value = {"status": "ok", "text": "Reuters headlines"}
            return mock_resp

        mock_httpx.post.side_effect = side_effect

        config = self._make_config()
        result = _fetch_headlines({"sources": ["ap", "reuters"]}, config)

        assert result is not None
        assert "Reuters" in result
        assert "AP" not in result


class TestParseBriefingJson:
    """Tests for parse_briefing_json() — extracts structured output from briefing results."""

    def test_valid_json(self):
        from istota.skills.briefing import parse_briefing_json
        msg = '{"subject": "Morning Briefing", "body": "📰 NEWS\\nStuff happened"}'
        result = parse_briefing_json(msg)
        assert result is not None
        assert result["subject"] == "Morning Briefing"
        assert "NEWS" in result["body"]

    def test_json_in_code_fence(self):
        from istota.skills.briefing import parse_briefing_json
        msg = 'Here is the briefing:\n```json\n{"subject": "Evening Briefing", "body": "📈 MARKETS\\nS&P up"}\n```'
        result = parse_briefing_json(msg)
        assert result is not None
        assert result["subject"] == "Evening Briefing"
        assert "MARKETS" in result["body"]

    def test_json_with_preamble(self):
        from istota.skills.briefing import parse_briefing_json
        msg = 'I composed the briefing:\n{"subject": "Morning Briefing", "body": "Content here"}'
        result = parse_briefing_json(msg)
        assert result is not None
        assert result["body"] == "Content here"

    def test_plain_text_returns_none(self):
        from istota.skills.briefing import parse_briefing_json
        msg = "📰 NEWS\nJust plain briefing text with no JSON"
        result = parse_briefing_json(msg)
        assert result is None

    def test_missing_body_returns_none(self):
        from istota.skills.briefing import parse_briefing_json
        msg = '{"subject": "Briefing"}'
        result = parse_briefing_json(msg)
        assert result is None

    def test_subject_defaults_when_missing(self):
        from istota.skills.briefing import parse_briefing_json
        msg = '{"body": "Content here"}'
        result = parse_briefing_json(msg)
        assert result is not None
        assert result["subject"] is None
        assert result["body"] == "Content here"

    def test_smart_quotes_normalized(self):
        from istota.skills.briefing import parse_briefing_json
        msg = '{"subject": "Morning Briefing", "body": "He said \u201chello\u201d today"}'
        result = parse_briefing_json(msg)
        assert result is not None
        assert "hello" in result["body"]

    def test_invalid_json_returns_none(self):
        from istota.skills.briefing import parse_briefing_json
        msg = '{"broken json'
        result = parse_briefing_json(msg)
        assert result is None

    def test_duplicate_json_objects_returns_first(self):
        """When _compose_full_result prepends a near-duplicate block, parse the first JSON."""
        from istota.skills.briefing import parse_briefing_json
        msg = (
            'Now let me compose the briefing.\n\n'
            '{"subject": "Evening Briefing", "body": "First version content"}\n\n'
            'Now let me compose the briefing.\n\n'
            '{"subject": "Evening Briefing", "body": "Second version content"}'
        )
        result = parse_briefing_json(msg)
        assert result is not None
        assert result["subject"] == "Evening Briefing"
        assert result["body"] == "First version content"
