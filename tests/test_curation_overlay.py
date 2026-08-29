"""Tests for istota.memory.curation.overlay — per-skill overlay documents.

An overlay is a flat markdown file with no `## ` sections, so it cannot go
through `SectionedDoc`. These tests pin the two properties that keeps costing:
the parse must not slice at `## ` (a `## ` in an overlay is content the loader
demotes, not a section boundary), and the op semantics must be the ones
`ops.py` already applies to USER.md rather than a second implementation of
them.
"""

from __future__ import annotations

import copy

import pytest

from istota.memory.curation.overlay import (
    apply_overlay_op,
    parse_overlay_doc,
    serialize_overlay_doc,
)


class TestRoundTrip:
    @pytest.mark.parametrize(
        "text",
        [
            "",
            "- one\n",
            "- one\n- two\n",
            "### Rules\n\n- one\n\n### More\n\n- two\n",
            "A paragraph.\n\n- a bullet\n",
        ],
    )
    def test_newline_terminated_text_round_trips_exactly(self, text):
        assert serialize_overlay_doc(parse_overlay_doc(text)) == text

    def test_missing_trailing_newline_is_added(self):
        # Same contract as serialize_sectioned_doc: a file on disk ends with a
        # newline. The only text that does not round-trip byte-for-byte.
        assert serialize_overlay_doc(parse_overlay_doc("- one")) == "- one\n"

    def test_blank_only_text_serializes_empty(self):
        assert serialize_overlay_doc(parse_overlay_doc("")) == ""

    def test_parse_does_not_slice_at_level_two_headings(self):
        # The whole reason this module exists. `parse_sectioned_doc` would turn
        # this into one Section and drop the `## ` line into `heading`; here it
        # is content, and the loader's `_demote_overlay_headings` is what deals
        # with it at read time.
        text = "- one\n\n## Not a section\n\n- two\n"
        section = parse_overlay_doc(text)
        assert "## Not a section" in section.lines
        assert serialize_overlay_doc(section) == text


class TestAppend:
    def test_append_to_empty_file(self):
        section = parse_overlay_doc("")
        new, outcome = apply_overlay_op(
            section, {"op": "append", "line": "Never run the full suite here"}
        )
        assert outcome == "applied"
        assert serialize_overlay_doc(new) == "- Never run the full suite here\n"

    def test_append_to_existing_bullets_lands_last(self):
        section = parse_overlay_doc("- one\n- two\n")
        new, outcome = apply_overlay_op(section, {"op": "append", "line": "three"})
        assert outcome == "applied"
        assert serialize_overlay_doc(new) == "- one\n- two\n- three\n"

    def test_append_normalizes_an_existing_marker(self):
        section = parse_overlay_doc("- one\n")
        new, _ = apply_overlay_op(section, {"op": "append", "line": "* two"})
        assert serialize_overlay_doc(new) == "- one\n- two\n"

    def test_append_duplicate_is_a_noop(self):
        section = parse_overlay_doc("- one\n- two\n")
        new, outcome = apply_overlay_op(section, {"op": "append", "line": "ONE"})
        assert outcome == "noop_dup"
        assert serialize_overlay_doc(new) == "- one\n- two\n"

    def test_append_leaves_the_input_section_untouched(self):
        section = parse_overlay_doc("- one\n")
        before = copy.deepcopy(section)
        apply_overlay_op(section, {"op": "append", "line": "two"})
        assert section == before

    def test_append_of_a_heading_shaped_line_is_rejected(self):
        section = parse_overlay_doc("- one\n")
        new, reason = apply_overlay_op(section, {"op": "append", "line": "## Rules"})
        assert reason == "line_starts_with_hash"
        assert serialize_overlay_doc(new) == "- one\n"

    @pytest.mark.parametrize(
        "line",
        [
            "note\n### Injected\n- hidden rule",
            "note\n## Escaped",
            "note\rcarriage",
        ],
    )
    def test_an_embedded_newline_is_rejected(self, line):
        # `_HEADING_SHAPED_RE` is not MULTILINE, so it only ever saw the first
        # line: one op inserted three, and the `remove` that undid the append
        # popped one of them and orphaned the rest for good.
        section = parse_overlay_doc("- safe rule\n")
        new, reason = apply_overlay_op(section, {"op": "append", "line": line})
        assert reason == "line_contains_newline"
        assert serialize_overlay_doc(new) == "- safe rule\n"

    def test_append_of_a_blank_line_is_rejected(self):
        section = parse_overlay_doc("- one\n")
        _, reason = apply_overlay_op(section, {"op": "append", "line": "   "})
        assert reason == "empty_line"

    def test_append_under_a_subsection(self):
        section = parse_overlay_doc("- top\n\n### Rules\n\n- existing\n")
        new, outcome = apply_overlay_op(
            section, {"op": "append", "line": "added", "subheading": "Rules"}
        )
        assert outcome == "applied"
        body = serialize_overlay_doc(new)
        assert body.index("- existing") < body.index("- added")
        # Did not land in the top region.
        assert body.index("- top") < body.index("### Rules")

    def test_append_under_a_missing_subsection_is_rejected(self):
        section = parse_overlay_doc("- top\n")
        _, reason = apply_overlay_op(
            section, {"op": "append", "line": "x", "subheading": "Nope"}
        )
        assert reason == "subheading_missing"

    def test_append_with_no_subheading_targets_the_top_region(self):
        section = parse_overlay_doc("- top\n\n### Rules\n\n- existing\n")
        new, outcome = apply_overlay_op(section, {"op": "append", "line": "added"})
        assert outcome == "applied"
        body = serialize_overlay_doc(new)
        assert body.index("- added") < body.index("### Rules")

    def test_append_missing_line_field_is_rejected(self):
        section = parse_overlay_doc("")
        _, reason = apply_overlay_op(section, {"op": "append"})
        assert reason == "missing_field"


class TestRemove:
    def test_removes_the_unique_match(self):
        section = parse_overlay_doc("- alpha rule\n- beta rule\n")
        new, outcome = apply_overlay_op(section, {"op": "remove", "match": "alpha"})
        assert outcome == "applied"
        assert serialize_overlay_doc(new) == "- beta rule\n"

    def test_removes_the_last_bullet_leaving_an_empty_document(self):
        section = parse_overlay_doc("- only one\n")
        new, outcome = apply_overlay_op(section, {"op": "remove", "match": "only"})
        assert outcome == "applied"
        assert serialize_overlay_doc(new).strip() == ""

    def test_zero_matches_is_a_noop(self):
        section = parse_overlay_doc("- alpha\n")
        new, outcome = apply_overlay_op(section, {"op": "remove", "match": "gamma"})
        assert outcome == "noop_no_match"
        assert serialize_overlay_doc(new) == "- alpha\n"

    def test_multiple_matches_is_rejected(self):
        section = parse_overlay_doc("- alpha rule\n- alpha other\n")
        new, reason = apply_overlay_op(section, {"op": "remove", "match": "alpha"})
        assert reason == "multiple_matches"
        assert serialize_overlay_doc(new) == "- alpha rule\n- alpha other\n"

    def test_reaches_into_a_subsection(self):
        # A second bullet keeps the subsection non-empty, so this stays a test
        # about *reaching in*; the emptied case belongs to the delete-on-empty
        # rule in tests/test_curation_subheading_removal.py.
        section = parse_overlay_doc("- top\n\n### Rules\n\n- nested rule\n- other rule\n")
        new, outcome = apply_overlay_op(section, {"op": "remove", "match": "nested"})
        assert outcome == "applied"
        body = serialize_overlay_doc(new)
        assert "nested rule" not in body
        assert "### Rules" in body
        assert "- other rule" in body

    def test_a_subheading_line_is_never_a_match(self):
        section = parse_overlay_doc("### Rules\n\n- a bullet\n")
        _, outcome = apply_overlay_op(section, {"op": "remove", "match": "Rules"})
        assert outcome == "noop_no_match"

    def test_empty_match_is_rejected(self):
        section = parse_overlay_doc("- alpha\n")
        _, reason = apply_overlay_op(section, {"op": "remove", "match": "  "})
        assert reason == "empty_match"

    def test_a_named_subsection_scopes_the_search(self):
        # `--heading` used to be accepted, validated against nothing and
        # dropped, so this removed the top-region bullet and said `applied`.
        section = parse_overlay_doc(
            "- top rule\n\n### Rules\n\n- nested rule\n"
        )
        new, outcome = apply_overlay_op(
            section, {"op": "remove", "match": "top", "subheading": "Rules"}
        )
        assert outcome == "noop_no_match"
        assert serialize_overlay_doc(new) == (
            "- top rule\n\n### Rules\n\n- nested rule\n"
        )

    def test_a_missing_subsection_is_rejected_rather_than_ignored(self):
        section = parse_overlay_doc("- top rule\n\n### Rules\n\n- nested\n")
        new, reason = apply_overlay_op(
            section, {"op": "remove", "match": "top", "subheading": "NoSuch"}
        )
        assert reason == "subheading_missing"
        assert serialize_overlay_doc(new) == (
            "- top rule\n\n### Rules\n\n- nested\n"
        )

    def test_a_named_subsection_disambiguates_a_multiple_match(self):
        section = parse_overlay_doc(
            "- alpha top\n\n### Rules\n\n- alpha nested\n- keep me\n"
        )
        new, outcome = apply_overlay_op(
            section, {"op": "remove", "match": "alpha", "subheading": "Rules"}
        )
        assert outcome == "applied"
        # `- alpha top` is untouched: the named subsection is what disambiguated.
        assert serialize_overlay_doc(new) == "- alpha top\n\n### Rules\n\n- keep me\n"


class TestReplace:
    def test_replaces_the_unique_match(self):
        section = parse_overlay_doc("- alpha rule\n- beta rule\n")
        new, outcome = apply_overlay_op(
            section, {"op": "replace", "match": "alpha", "line": "rewritten"}
        )
        assert outcome == "applied"
        assert serialize_overlay_doc(new) == "- rewritten\n- beta rule\n"

    def test_zero_matches_is_a_noop(self):
        section = parse_overlay_doc("- alpha\n")
        new, outcome = apply_overlay_op(
            section, {"op": "replace", "match": "gamma", "line": "x"}
        )
        assert outcome == "noop_no_match"
        assert serialize_overlay_doc(new) == "- alpha\n"

    def test_multiple_matches_is_rejected(self):
        section = parse_overlay_doc("- alpha one\n- alpha two\n")
        _, reason = apply_overlay_op(
            section, {"op": "replace", "match": "alpha", "line": "x"}
        )
        assert reason == "multiple_matches"

    def test_rewriting_to_an_existing_bullet_is_a_noop(self):
        section = parse_overlay_doc("- alpha\n- beta\n")
        new, outcome = apply_overlay_op(
            section, {"op": "replace", "match": "alpha", "line": "beta"}
        )
        assert outcome == "noop_dup"
        assert serialize_overlay_doc(new) == "- alpha\n- beta\n"

    def test_a_heading_shaped_replacement_is_rejected(self):
        section = parse_overlay_doc("- alpha\n")
        _, reason = apply_overlay_op(
            section, {"op": "replace", "match": "alpha", "line": "### Rules"}
        )
        assert reason == "line_starts_with_hash"

    def test_a_named_subsection_scopes_the_search(self):
        section = parse_overlay_doc("- top rule\n\n### Rules\n\n- nested rule\n")
        new, outcome = apply_overlay_op(
            section,
            {"op": "replace", "match": "top", "line": "x", "subheading": "Rules"},
        )
        assert outcome == "noop_no_match"
        assert "- top rule" in serialize_overlay_doc(new)

    def test_a_missing_subsection_is_rejected_rather_than_ignored(self):
        section = parse_overlay_doc("- top rule\n")
        _, reason = apply_overlay_op(
            section,
            {"op": "replace", "match": "top", "line": "x", "subheading": "NoSuch"},
        )
        assert reason == "subheading_missing"

    def test_indentation_of_the_matched_bullet_survives(self):
        section = parse_overlay_doc("- top\n  - nested one\n")
        new, outcome = apply_overlay_op(
            section, {"op": "replace", "match": "nested", "line": "nested two"}
        )
        assert outcome == "applied"
        assert serialize_overlay_doc(new) == "- top\n  - nested two\n"


class TestUnknownOps:
    def test_unknown_op_is_rejected(self):
        section = parse_overlay_doc("- one\n")
        _, reason = apply_overlay_op(section, {"op": "add_heading", "heading": "X"})
        assert reason == "unknown_op"

    def test_remove_heading_is_not_an_overlay_op(self):
        # Overlays have no `## ` sections, so the two heading ops have nothing
        # to act on. Refused here as well as at the CLI.
        section = parse_overlay_doc("- one\n")
        _, reason = apply_overlay_op(section, {"op": "remove_heading", "heading": "X"})
        assert reason == "unknown_op"
