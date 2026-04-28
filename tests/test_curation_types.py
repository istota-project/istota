"""Tests for curation types and line classification helpers."""

from istota.memory.curation.types import (
    Section,
    SectionedDoc,
    classify_line,
    normalize_bullet_text,
    top_region_indices,
)


class TestClassifyLine:
    def test_dash_bullet(self):
        assert classify_line("- foo") == "bullet"
        assert classify_line("  - foo") == "bullet"

    def test_star_bullet(self):
        assert classify_line("* foo") == "bullet"
        assert classify_line("  * foo") == "bullet"

    def test_numbered_bullet(self):
        assert classify_line("1. foo") == "bullet"
        assert classify_line("12. foo") == "bullet"
        assert classify_line("  3. foo") == "bullet"

    def test_subheading(self):
        assert classify_line("### Editor") == "subheading"
        assert classify_line("#### Deeper") == "subheading"

    def test_blank(self):
        assert classify_line("") == "blank"
        assert classify_line("   ") == "blank"
        assert classify_line("\t") == "blank"

    def test_paragraph(self):
        assert classify_line("Just a sentence.") == "paragraph"
        assert classify_line("foo bar baz") == "paragraph"

    def test_dash_without_space_is_paragraph(self):
        # `-foo` without a space after the dash isn't a bullet
        assert classify_line("-foo") == "paragraph"


class TestNormalizeBulletText:
    def test_strips_dash_marker_and_whitespace(self):
        assert normalize_bullet_text("- Prefers Python") == "Prefers Python"
        assert normalize_bullet_text("  - Prefers Python  ") == "Prefers Python"

    def test_strips_star_marker(self):
        assert normalize_bullet_text("* Foo") == "Foo"

    def test_strips_numbered_marker(self):
        assert normalize_bullet_text("1. Foo") == "Foo"
        assert normalize_bullet_text("  12. Foo bar  ") == "Foo bar"

    def test_strips_only_first_marker(self):
        # If text itself starts with `-` it stays
        assert normalize_bullet_text("- - intra-bullet dash") == "- intra-bullet dash"

    def test_non_bullet_returns_stripped(self):
        # Defensive: passing a non-bullet line returns it stripped
        assert normalize_bullet_text("Just text") == "Just text"


class TestTopRegionIndices:
    def test_no_subheading_returns_full_range(self):
        section = Section(heading="X", lines=["", "- a", "- b", ""])
        assert top_region_indices(section) == (0, 4)

    def test_splits_at_first_subheading(self):
        section = Section(
            heading="X",
            lines=["", "- a", "", "### Sub", "- b"],
        )
        assert top_region_indices(section) == (0, 3)

    def test_subheading_at_start(self):
        section = Section(heading="X", lines=["### Sub", "- b"])
        assert top_region_indices(section) == (0, 0)

    def test_empty_lines(self):
        section = Section(heading="X", lines=[])
        assert top_region_indices(section) == (0, 0)


class TestSectionedDoc:
    def test_find_returns_section_by_heading(self):
        doc = SectionedDoc(
            preamble=[],
            sections=[Section(heading="A", lines=[]), Section(heading="B", lines=[])],
        )
        assert doc.find("B").heading == "B"

    def test_find_returns_none_when_missing(self):
        doc = SectionedDoc(preamble=[], sections=[Section(heading="A", lines=[])])
        assert doc.find("X") is None

    def test_has_returns_bool(self):
        doc = SectionedDoc(preamble=[], sections=[Section(heading="A", lines=[])])
        assert doc.has("A") is True
        assert doc.has("X") is False
