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


FENCED_PINNED = (
    "## The desk <!-- pinned -->\n"
    "- Answer mail\n"
    "\n"
    "Report format:\n"
    "```\n"
    "## Summary\n"
    "- one line\n"
    "```\n"
    "- Escalate invoices to the owner\n"
    "\n"
    "## Preferences\n"
    "- Likes vim\n"
)


class TestCodeFences:
    def test_a_heading_inside_a_fence_is_body_text(self):
        doc = parse_sectioned_doc(FENCED_PINNED)
        assert [s.heading for s in doc.sections] == [
            "The desk <!-- pinned -->", "Preferences",
        ]
        assert "## Summary" in doc.sections[0].lines
        assert "- Escalate invoices to the owner" in doc.sections[0].lines

    def test_a_fenced_doc_round_trips_byte_for_byte(self):
        assert serialize_sectioned_doc(parse_sectioned_doc(FENCED_PINNED)) == FENCED_PINNED

    def test_tilde_fence_and_longer_closer(self):
        text = "## A\n~~~~ md\n## Not a heading\n~~~~~\n## B\n- b\n"
        doc = parse_sectioned_doc(text)
        assert [s.heading for s in doc.sections] == ["A", "B"]
        assert serialize_sectioned_doc(doc) == text

    def test_a_different_marker_does_not_close_the_fence(self):
        text = "## A\n```\n~~~\n## Inside\n```\n## B\n"
        assert [s.heading for s in parse_sectioned_doc(text).sections] == ["A", "B"]

    def test_an_unclosed_fence_hides_nothing(self):
        # A stray marker must not fold later headings into the section above
        # it; that would put a pinned section under an unpinned heading.
        text = "## A\n```\n- a\n## B <!-- pinned -->\n- b\n"
        doc = parse_sectioned_doc(text)
        assert [s.heading for s in doc.sections] == ["A", "B <!-- pinned -->"]
        assert serialize_sectioned_doc(doc) == text

    def test_the_curator_cannot_reach_pinned_bullets_through_a_fenced_heading(self):
        from istota.memory.curation.ops import apply_ops

        doc = parse_sectioned_doc(FENCED_PINNED)
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "remove_heading", "heading": "Summary"}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "heading_missing"
        assert serialize_sectioned_doc(new_doc) == FENCED_PINNED


class TestDocFind:
    def test_find_returns_section_by_heading_exact_match(self):
        doc = parse_sectioned_doc("## Foo\n- a\n## Bar\n- b\n")
        assert doc.find("Foo").lines[0] == "- a"
        assert doc.find("Bar").lines[0] == "- b"

    def test_find_returns_none_when_heading_missing(self):
        doc = parse_sectioned_doc("## Foo\n- a\n")
        assert doc.find("Missing") is None
