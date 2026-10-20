"""A quick note's file name comes from its first line of text."""

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from istota import db, user_profiles
from istota.config import Config
from istota.workspace import quick_notes
from istota.workspace.quick_notes import NAME_MAX_CHARS, derive_name

UTC = ZoneInfo("UTC")
NOW = datetime(2026, 10, 20, 17, 45, tzinfo=timezone.utc)


def name(content, tz=UTC):
    return derive_name(content, tz, NOW)


@pytest.mark.parametrize("content,expected", [
    ("# Buy milk\nand eggs", "Buy milk"),
    ("### Weekly plan", "Weekly plan"),
    ("- Call the plumber", "Call the plumber"),
    ("* item", "item"),
    ("+ item", "item"),
    ("1. First step", "First step"),
    ("2) Second step", "Second step"),
    ("- [ ] Book flights", "Book flights"),
    ("* [x] Paid rent", "Paid rent"),
    ("> # Quoted heading", "Quoted heading"),
    ("[Quarterly report](https://example.com/r) draft", "Quarterly report draft"),
    ("**Bold** and _it_ and `code` ~~gone~~", "Bold and it and code gone"),
    ("-5 degrees tonight", "-5 degrees tonight"),
    ("#hashtag idea", "#hashtag idea"),
])
def test_markdown_markers_are_stripped(content, expected):
    assert name(content) == expected


def test_the_first_non_blank_line_names_the_note():
    assert name("\n   \n\nCall the plumber about the boiler\nsecond line") == (
        "Call the plumber about the boiler"
    )


def test_whitespace_is_collapsed_and_trimmed():
    assert name("  Buy    milk \t now  ") == "Buy milk now"


def test_a_long_line_is_cut_on_a_word_boundary():
    line = "Remember to ask about the boiler service contract renewal before November"
    result = name(line)
    assert len(result) <= NAME_MAX_CHARS
    assert line.startswith(result)
    assert not result.endswith(" ")
    # The cut fell between words, not inside one.
    assert line[len(result)] == " "


def test_a_long_line_without_a_nearby_space_is_cut_hard():
    line = "Short " + "x" * 80
    result = name(line)
    assert result == line[:NAME_MAX_CHARS]


def test_unsafe_characters_go_through_the_upload_sanitiser():
    result = name('Meeting 10/20: plan? "draft" <v2>|*')
    for char in '\\/:*?"<>|':
        assert char not in result
    # A slash is not a path separator here: the text before it survives.
    assert result.startswith("Meeting 10_20")


def test_trailing_dots_and_spaces_are_trimmed():
    assert name("Ideas for the weekend...  ") == "Ideas for the weekend"


@pytest.mark.parametrize("content", ["# ", "---", "***", "> - [ ]", "", "   \n\t\n", "!!! ???"])
def test_nothing_with_a_letter_or_digit_gives_a_timestamp(content):
    assert name(content) == "2026-10-20 17.45"


def test_the_timestamp_is_in_the_users_zone():
    assert name("---", ZoneInfo("America/New_York")) == "2026-10-20 13.45"


def _config_with_zone(tmp_path, zone):
    config = Config(db_path=tmp_path / "istota.db", workspace_path=tmp_path / "workspace")
    db.init_db(config.db_path)
    user_profiles.ensure_profile(config.db_path, "alice", timezone=zone)
    return config


def test_the_users_zone_comes_from_their_profile(tmp_path):
    config = _config_with_zone(tmp_path, "Europe/Berlin")
    assert quick_notes.user_zone(config, "alice") == ZoneInfo("Europe/Berlin")


def test_an_invalid_zone_falls_back_to_utc(tmp_path):
    config = _config_with_zone(tmp_path, "Not/AZone")
    assert quick_notes.user_zone(config, "alice") == UTC
