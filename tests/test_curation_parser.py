"""Tests for SectionedDoc parser/serializer."""

from istota.memory.curation.parser import parse_sectioned_doc, serialize_sectioned_doc


class TestParse:
    def test_empty_document(self):
        doc = parse_sectioned_doc("")
        assert doc.preamble == []
        assert doc.sections == []

    def test_preamble_only_no_headings(self):
        doc = parse_sectioned_doc("Some intro text.\nMore.\n")
        assert doc.preamble == ["Some intro text.", "More.", ""]
        assert doc.sections == []

    def test_single_section_with_bullets(self):
        text = "## Preferences\n- Foo\n- Bar\n"
        doc = parse_sectioned_doc(text)
        assert doc.preamble == []
        assert len(doc.sections) == 1
        assert doc.sections[0].heading == "Preferences"
        assert doc.sections[0].lines == ["- Foo", "- Bar", ""]

    def test_multiple_sections(self):
        text = "## A\n- a1\n\n## B\n- b1\n- b2\n"
        doc = parse_sectioned_doc(text)
        assert [s.heading for s in doc.sections] == ["A", "B"]
        assert doc.sections[0].lines == ["- a1", ""]
        assert doc.sections[1].lines == ["- b1", "- b2", ""]

    def test_subheading_preserved_inside_section_lines(self):
        text = "## Pref\n- x\n### Editor\n- VS Code\n"
        doc = parse_sectioned_doc(text)
        assert doc.sections[0].lines == ["- x", "### Editor", "- VS Code", ""]

    def test_blank_lines_within_section_preserved(self):
        text = "## A\n- a\n\n- b\n"
        doc = parse_sectioned_doc(text)
        assert doc.sections[0].lines == ["- a", "", "- b", ""]

    def test_heading_with_trailing_whitespace_stripped(self):
        text = "## Foo  \n- a\n"
        doc = parse_sectioned_doc(text)
        assert doc.sections[0].heading == "Foo"

    def test_preamble_then_sections(self):
        text = "Intro\n\n## A\n- a\n"
        doc = parse_sectioned_doc(text)
        assert doc.preamble == ["Intro", ""]
        assert doc.sections[0].heading == "A"


class TestSerialize:
    def test_round_trip_well_formed_input(self):
        text = "## A\n- a1\n- a2\n\n## B\n- b1\n"
        assert serialize_sectioned_doc(parse_sectioned_doc(text)) == text

    def test_serialize_adds_single_trailing_newline_when_missing(self):
        text = "## A\n- a"
        out = serialize_sectioned_doc(parse_sectioned_doc(text))
        assert out.endswith("\n")
        assert not out.endswith("\n\n")

    def test_serialize_preserves_existing_single_trailing_newline(self):
        text = "## A\n- a\n"
        assert serialize_sectioned_doc(parse_sectioned_doc(text)) == text

    def test_round_trip_preamble_only(self):
        text = "Just intro.\nMore intro.\n"
        assert serialize_sectioned_doc(parse_sectioned_doc(text)) == text

    def test_round_trip_with_subheading(self):
        text = "## A\n- a\n### Sub\n- s\n"
        assert serialize_sectioned_doc(parse_sectioned_doc(text)) == text

    def test_serialize_empty_doc(self):
        from istota.memory.curation.types import SectionedDoc
        out = serialize_sectioned_doc(SectionedDoc(preamble=[], sections=[]))
        assert out == "\n" or out == ""  # acceptable: either empty or single newline


class TestDocFind:
    def test_find_returns_section_by_heading_exact_match(self):
        doc = parse_sectioned_doc("## Foo\n- a\n## Bar\n- b\n")
        assert doc.find("Foo").lines[0] == "- a"
        assert doc.find("Bar").lines[0] == "- b"

    def test_find_returns_none_when_heading_missing(self):
        doc = parse_sectioned_doc("## Foo\n- a\n")
        assert doc.find("Missing") is None
