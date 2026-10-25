"""The shared heading demotion. The overlay cases live in test_skills_loader.py."""

from istota.lib.markdown_headings import demote_headings


def _shift3(level: int) -> int:
    return min(level + 3, 6)


def test_a_shift_keeps_relative_levels_and_stops_at_six():
    text = "# Title\n## Goal\n#### Deep\n###### Deepest"
    assert demote_headings(text, _shift3) == "#### Title\n##### Goal\n###### Deep\n###### Deepest"


def test_max_level_leaves_deeper_headings_alone():
    assert demote_headings("## a\n### b", lambda _level: 4, max_level=2) == "#### a\n### b"


def test_a_closed_fence_is_a_sample_and_an_unclosed_one_is_not():
    closed = "```\n## sample\n```\n## real"
    assert demote_headings(closed, _shift3) == "```\n## sample\n```\n##### real"
    unclosed = "```\n## not a sample"
    assert demote_headings(unclosed, _shift3) == "```\n##### not a sample"
