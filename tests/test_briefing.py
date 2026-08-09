"""Tests for istota.skills.briefing module."""

from unittest.mock import patch

import istota.skills.briefing as briefing_mod
from istota.skills.briefing import (
    _strip_html,
    strip_markdown,
    _parse_reminders,
    _fetch_market_data,
    _fetch_finviz_market_data,
    _fetch_calendar_events,
    _briefing_digest_key,
    load_previous_briefing_digest,
    save_briefing_digest,
)
from istota.config import Config, NextcloudConfig


def test_legacy_generator_removed():
    """The legacy component-based generator is gone — blocks are the sole path."""
    assert not hasattr(briefing_mod, "build_briefing_prompt")
    assert not hasattr(briefing_mod, "_component_enabled")
    assert not hasattr(briefing_mod, "_fetch_todo_items")
    assert not hasattr(briefing_mod, "_fetch_newsletter_content")
    # The legacy headlines fetcher moved to the browse briefing source
    # (BROWSE_PRESETS); the duplicate HEADLINE_SOURCES map is gone.
    assert not hasattr(briefing_mod, "_fetch_headlines")
    assert not hasattr(briefing_mod, "HEADLINE_SOURCES")
    # The reminder fetcher outlived the generator by reading the deprecated
    # `reminders_file` resource, but nothing called it — the briefings module
    # picks reminders itself (`sources/builtins._pick_reminder`). `_parse_reminders`
    # is still live; it is what that picker parses with.
    assert not hasattr(briefing_mod, "_fetch_random_reminder")
    assert hasattr(briefing_mod, "_parse_reminders")


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
        lines = [line.strip() for line in result.splitlines() if line.strip()]
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


class TestRenderBriefingHtml:
    """render_briefing_html converts the briefing markdown subset to bare HTML.

    The renderer is deliberately not a general markdown engine: anything
    outside the allowed subset is emitted as escaped literal text, and the
    output carries no styling at all (see the spec's "no styling" decision).
    """

    def test_empty_input_returns_empty_string(self):
        from istota.skills.briefing import render_briefing_html
        assert render_briefing_html("") == ""
        assert render_briefing_html("   \n\n ") == ""

    def test_wraps_in_bare_html_shell(self):
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("Hello")
        assert out.startswith("<html><body>")
        assert out.endswith("</body></html>")
        assert "<p>Hello</p>" in out

    def test_no_styling_anywhere(self):
        """Guards the 'email client default typography' decision."""
        from istota.skills.briefing import render_briefing_html
        md = (
            "\U0001f30d World\n"
            "**IRAN:** something happened. [[Semafor](https://semafor.com/a/iran), NYT]\n"
            "\n"
            "---\n"
            "\n"
            "- **10:00 Standup** (30 min)\n"
            "- *soft* item\n"
        )
        out = render_briefing_html(md)
        assert "style=" not in out
        assert "<head" not in out.lower()
        assert "<style" not in out.lower()
        assert "class=" not in out

    def test_renders_link(self):
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("See [Semafor](https://semafor.com/a/iran).")
        assert '<a href="https://semafor.com/a/iran">Semafor</a>' in out

    def test_renders_attribution_bracket_link(self):
        """The news attribution shape `[[Semafor](url), NYT]` keeps its brackets."""
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html(
            "Story text. [[Semafor](https://semafor.com/a/iran), NYT]"
        )
        assert (
            '[<a href="https://semafor.com/a/iran">Semafor</a>, NYT]' in out
        )

    def test_renders_bold_and_italic(self):
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("**bold** and *italic* and _under_")
        assert "<strong>bold</strong>" in out
        assert "<em>italic</em>" in out
        assert "<em>under</em>" in out

    def test_underscore_inside_word_is_not_italic(self):
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("file snake_case_name here")
        assert "<em>" not in out
        assert "snake_case_name" in out

    def test_link_url_with_underscores_survives(self):
        """Emphasis must not run inside an emitted href."""
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("[AP](https://apnews.com/a_b_c)")
        assert '<a href="https://apnews.com/a_b_c">AP</a>' in out
        assert "<em>" not in out

    def test_horizontal_rule(self):
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("before\n\n---\n\nafter")
        assert "<hr>" in out
        assert "<p>before</p>" in out
        assert "<p>after</p>" in out

    def test_bullets_become_unordered_list(self):
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("- one\n- two")
        assert "<ul>" in out and "</ul>" in out
        assert "<li>one</li>" in out
        assert "<li>two</li>" in out

    def test_label_line_then_bullets(self):
        """A plain section label immediately followed by bullets splits cleanly."""
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("\U0001f4c5 Calendar\n- **10:00 Standup**")
        assert "<p>\U0001f4c5 Calendar</p>" in out
        assert "<li><strong>10:00 Standup</strong></li>" in out

    def test_paragraphs_split_on_blank_line(self):
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("first para\n\nsecond para")
        assert out.count("<p>") == 2

    def test_consecutive_lines_keep_their_breaks(self):
        """Markets quotes are one line each; a soft break must survive."""
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("\U0001f7e2 **S&P**: 1.0\n\U0001f534 **Nasdaq**: 2.0")
        assert "<br>" in out
        assert out.count("<p>") == 1

    def test_escapes_html_in_text(self):
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("5 < 6 & 7 > 2 <script>alert(1)</script>")
        assert "&lt;" in out and "&amp;" in out
        assert "<script>" not in out

    def test_javascript_scheme_renders_as_plain_text(self):
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("[click](javascript:alert(1))")
        assert "javascript:" not in out
        assert "click" in out
        assert "<a " not in out

    def test_data_scheme_renders_as_plain_text(self):
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("[x](data:text/html;base64,AAA)")
        assert "<a " not in out
        assert "data:text/html" not in out

    def test_mailto_is_allowed(self):
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("[mail](mailto:a@b.com)")
        assert '<a href="mailto:a@b.com">mail</a>' in out

    def test_url_quotes_cannot_break_the_attribute(self):
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html('[x](https://e.com/a"onmouseover=alert)')
        # The quote must be entity-escaped inside the href, never raw — a raw
        # one would close the attribute and let the rest become markup.
        assert '"onmouseover' not in out
        assert "&quot;onmouseover" in out

    def test_atx_heading_is_not_emitted_as_markup(self):
        from istota.skills.briefing import render_briefing_html
        out = render_briefing_html("## Heading")
        assert "<h2" not in out
        # The marker is dropped (mirroring strip_markdown) so it doesn't leak
        # literally into the mail; the line is an ordinary paragraph.
        assert "<p>Heading</p>" in out
        assert "##" not in out

    def test_news_example_round_trip(self):
        from istota.skills.briefing import render_briefing_html
        md = (
            "\U0001f4f0 World News\n"
            "**IRAN-US TENSIONS ESCALATE:** Iran's foreign minister warned that "
            "Tehran's forces have their \"fingers on the trigger\". "
            "[[Semafor](https://www.semafor.com/article/iran-us-tensions), NYT]\n"
            "\n"
            "\U0001f4c8 Markets\n"
            "\U0001f7e2 **S&P 500 E-mini**: 6,104.75 (+30.25, +0.50%)\n"
            "\n"
            "---\n"
            "\n"
            "*Remember to breathe.*"
        )
        out = render_briefing_html(md)
        assert '<a href="https://www.semafor.com/article/iran-us-tensions">Semafor</a>' in out
        assert "<strong>IRAN-US TENSIONS ESCALATE:</strong>" in out
        assert "<strong>S&amp;P 500 E-mini</strong>" in out
        assert "<hr>" in out
        assert "<em>Remember to breathe.</em>" in out
        assert "style=" not in out

    def test_render_failure_falls_back_to_empty(self, monkeypatch):
        """A renderer exception must never bubble into delivery."""
        import istota.skills.briefing as mod
        monkeypatch.setattr(
            mod, "_render_blocks",
            lambda lines: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert mod.render_briefing_html("some text") == ""
