"""Tests for op application against a SectionedDoc."""

from istota.memory.curation.ops import apply_ops
from istota.memory.curation.parser import parse_sectioned_doc, serialize_sectioned_doc
from istota.memory.curation.types import Section, SectionedDoc


def _doc(text: str) -> SectionedDoc:
    return parse_sectioned_doc(text)


class TestAppend:
    def test_append_to_existing_heading_inserts_at_top_region_end(self):
        doc = _doc("## Pref\n- Foo\n- Bar\n")
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "- Baz"}]
        )
        assert rejected == []
        assert len(applied) == 1
        section = new_doc.find("Pref")
        # Last bullet is now Baz (before any trailing blank line)
        bullets = [l for l in section.lines if l.startswith("- ")]
        assert bullets[-1] == "- Baz"

    def test_append_inserts_before_trailing_blank_lines(self):
        doc = _doc("## Pref\n- Foo\n\n## Other\n- x\n")
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "- Bar"}]
        )
        assert rejected == []
        section = new_doc.find("Pref")
        # The blank line between sections should remain after the new bullet
        idx_bar = section.lines.index("- Bar")
        idx_foo = section.lines.index("- Foo")
        assert idx_bar == idx_foo + 1
        # And there should still be a trailing blank
        assert section.lines[-1] == ""

    def test_append_inserts_before_first_subheading(self):
        doc = _doc("## Pref\n- Foo\n### Editor\n- VS Code\n")
        new_doc, _, _ = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "- Bar"}]
        )
        section = new_doc.find("Pref")
        idx_sub = section.lines.index("### Editor")
        idx_bar = section.lines.index("- Bar")
        assert idx_bar == idx_sub - 1

    def test_append_to_section_with_no_bullets_inserts_after_heading(self):
        doc = _doc("## Pref\n\n")
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "- Bar"}]
        )
        assert applied and not rejected
        assert "- Bar" in new_doc.find("Pref").lines

    def test_append_into_section_with_no_top_region_separates_from_subheading(self):
        # Section starts immediately with a `### subheading` (top region empty).
        # The new bullet must land before the subheading AND a blank line must
        # separate them — otherwise the bullet visually fuses onto the heading.
        doc = _doc("## Pref\n### Sub\n- existing\n")
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "new"}]
        )
        assert applied and applied[0]["outcome"] == "applied"
        assert rejected == []
        section = new_doc.find("Pref")
        idx_bullet = section.lines.index("- new")
        idx_sub = section.lines.index("### Sub")
        # Blank line between the new bullet and the subheading
        assert idx_bullet + 1 < idx_sub
        assert section.lines[idx_bullet + 1] == ""

    def test_append_normalizes_dash_marker(self):
        doc = _doc("## Pref\n- Foo\n")
        new_doc, _, _ = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "- Already dashed"}]
        )
        assert "- Already dashed" in new_doc.find("Pref").lines

    def test_append_normalizes_star_marker(self):
        doc = _doc("## Pref\n- Foo\n")
        new_doc, _, _ = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "* star"}]
        )
        assert "- star" in new_doc.find("Pref").lines

    def test_append_normalizes_numbered_marker(self):
        doc = _doc("## Pref\n- Foo\n")
        new_doc, _, _ = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "1. numbered"}]
        )
        assert "- numbered" in new_doc.find("Pref").lines

    def test_append_normalizes_bare_text(self):
        doc = _doc("## Pref\n- Foo\n")
        new_doc, _, _ = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "bare text"}]
        )
        assert "- bare text" in new_doc.find("Pref").lines

    def test_append_dedup_against_existing_top_region_bullet(self):
        doc = _doc("## Pref\n- Foo bar\n")
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "- Foo bar"}]
        )
        # Op tracked as applied with noop_dup outcome, but no actual line added
        assert len(applied) == 1
        assert applied[0]["outcome"] == "noop_dup"
        assert rejected == []
        # No new bullet
        bullets = [l for l in new_doc.find("Pref").lines if l.startswith("- ")]
        assert bullets == ["- Foo bar"]

    def test_append_dedup_is_case_insensitive(self):
        doc = _doc("## Pref\n- Foo Bar\n")
        new_doc, applied, _ = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "- foo bar"}]
        )
        assert applied[0]["outcome"] == "noop_dup"

    def test_append_does_not_dedup_against_subheading_or_paragraph(self):
        doc = _doc("## Pref\nSome paragraph foo\n### foo\n")
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "- foo"}]
        )
        assert applied and applied[0]["outcome"] != "noop_dup"
        assert "- foo" in new_doc.find("Pref").lines

    def test_append_to_missing_heading_rejected(self):
        doc = _doc("## Pref\n- Foo\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "append", "heading": "Missing", "line": "- x"}]
        )
        assert applied == []
        assert len(rejected) == 1
        assert rejected[0]["reason"] == "heading_missing"

    def test_append_empty_line_rejected(self):
        doc = _doc("## Pref\n- Foo\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "   "}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "empty_line"

    def test_append_hash_prefix_line_rejected(self):
        doc = _doc("## Pref\n- Foo\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "## another heading"}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "line_starts_with_hash"

    def test_append_bullet_with_internal_hash_accepted(self):
        # Hashtags / footnote markers / code-comment-style notes have a `#`
        # but aren't heading-shaped (no whitespace after the hash run). They
        # should NOT be rejected.
        doc = _doc("## Pref\n- Foo\n")
        new_doc, applied, rejected = apply_ops(
            doc,
            [
                {"op": "append", "heading": "Pref", "line": "- #hashtag content"},
                {"op": "append", "heading": "Pref", "line": "#footnote-1 reference"},
                {"op": "append", "heading": "Pref", "line": "- See issue #42"},
            ],
        )
        assert rejected == []
        assert all(a["outcome"] == "applied" for a in applied)
        section = new_doc.find("Pref")
        assert "- #hashtag content" in section.lines
        assert "- #footnote-1 reference" in section.lines
        assert "- See issue #42" in section.lines

    def test_append_heading_shaped_alt_levels_rejected(self):
        # Each `#`-run-followed-by-whitespace shape is heading-like and rejected.
        doc = _doc("## Pref\n- Foo\n")
        for shape in ("# h1", "### h3", "###### h6"):
            _, applied, rejected = apply_ops(
                doc, [{"op": "append", "heading": "Pref", "line": shape}]
            )
            assert rejected and rejected[0]["reason"] == "line_starts_with_hash", shape

    def test_append_after_paragraph_inserts_blank_gap(self):
        # A new bullet appended after a trailing paragraph should not fuse
        # onto it visually. The applier inserts a blank line in between.
        doc = _doc("## Pref\nthis is a free-form paragraph note\n")
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "- new bullet"}]
        )
        assert applied and applied[0]["outcome"] == "applied"
        section = new_doc.find("Pref")
        # Find the paragraph and the bullet, confirm a blank between them.
        para_idx = section.lines.index("this is a free-form paragraph note")
        bullet_idx = section.lines.index("- new bullet")
        assert bullet_idx == para_idx + 2
        assert section.lines[para_idx + 1] == ""

    def test_append_after_bullet_does_not_add_gap(self):
        # Bullet-to-bullet stays adjacent — that's the expected list shape.
        doc = _doc("## Pref\n- existing\n")
        new_doc, _, _ = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "- new"}]
        )
        section = new_doc.find("Pref")
        existing_idx = section.lines.index("- existing")
        new_idx = section.lines.index("- new")
        assert new_idx == existing_idx + 1
        # No blank line between consecutive bullets
        between = section.lines[existing_idx + 1: new_idx]
        assert between == []


class TestAddHeading:
    def test_add_heading_appends_at_end_of_doc(self):
        doc = _doc("## A\n- a\n")
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "add_heading", "heading": "B", "lines": ["- b1", "- b2"]}]
        )
        assert applied and not rejected
        assert [s.heading for s in new_doc.sections] == ["A", "B"]
        b = new_doc.find("B")
        assert "- b1" in b.lines
        assert "- b2" in b.lines

    def test_add_heading_duplicate_rejected(self):
        doc = _doc("## A\n- a\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "add_heading", "heading": "A", "lines": ["- x"]}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "heading_exists"

    def test_add_heading_empty_lines_list_rejected(self):
        doc = _doc("## A\n- a\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "add_heading", "heading": "B", "lines": []}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "empty_lines"

    def test_add_heading_hash_prefix_heading_rejected(self):
        doc = _doc("## A\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "add_heading", "heading": "## B", "lines": ["- x"]}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "heading_starts_with_hash"

    def test_add_heading_empty_heading_rejected(self):
        doc = _doc("## A\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "add_heading", "heading": "  ", "lines": ["- x"]}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "empty_heading"

    def test_add_heading_normalizes_each_bullet(self):
        doc = _doc("## A\n- a\n")
        new_doc, applied, _ = apply_ops(
            doc,
            [{"op": "add_heading", "heading": "B", "lines": ["* star", "1. one", "bare"]}],
        )
        b = new_doc.find("B")
        assert "- star" in b.lines
        assert "- one" in b.lines
        assert "- bare" in b.lines


class TestRemove:
    def test_remove_matches_top_region_bullet_only(self):
        doc = _doc("## Pref\n- Foo\n- Bar\n")
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "remove", "heading": "Pref", "match": "Foo"}]
        )
        assert applied and not rejected
        section = new_doc.find("Pref")
        assert "- Foo" not in section.lines
        assert "- Bar" in section.lines

    def test_remove_no_match_is_noop(self):
        doc = _doc("## Pref\n- Foo\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "remove", "heading": "Pref", "match": "missing"}]
        )
        assert len(applied) == 1
        assert applied[0]["outcome"] == "noop_no_match"
        assert rejected == []

    def test_remove_multiple_matches_rejected(self):
        doc = _doc("## Pref\n- Foo apple\n- Foo banana\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "remove", "heading": "Pref", "match": "foo"}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "multiple_matches"

    def test_remove_missing_heading_rejected(self):
        doc = _doc("## Pref\n- Foo\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "remove", "heading": "Other", "match": "Foo"}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "heading_missing"

    def test_remove_empty_match_rejected(self):
        doc = _doc("## Pref\n- Foo\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "remove", "heading": "Pref", "match": "  "}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "empty_match"

    def test_remove_match_is_case_insensitive(self):
        doc = _doc("## Pref\n- Banana\n")
        new_doc, applied, _ = apply_ops(
            doc, [{"op": "remove", "heading": "Pref", "match": "BANANA"}]
        )
        assert applied
        assert "- Banana" not in new_doc.find("Pref").lines

    def test_remove_ignores_paragraph_lines(self):
        # Only bullets in the top region are match candidates. A paragraph
        # whose text contains the match substring should NOT be removed; the
        # bullet is.
        doc = _doc("## Pref\nfoo as paragraph text\n- the foo bullet\n")
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "remove", "heading": "Pref", "match": "foo"}]
        )
        assert applied and applied[0]["outcome"] == "applied"
        section = new_doc.find("Pref")
        # Paragraph preserved, bullet removed
        assert "foo as paragraph text" in section.lines
        assert "- the foo bullet" not in section.lines

    def test_remove_descends_into_subsections(self):
        # Removing stale bullets is the whole point — a bullet under a
        # `### subheading` must be removable, not rejected.
        doc = _doc("## Pref\n- top bullet\n### Sub\n- sub foo bullet\n")
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "remove", "heading": "Pref", "match": "foo"}]
        )
        assert rejected == []
        assert applied and applied[0]["outcome"] == "applied"
        section = new_doc.find("Pref")
        assert "- sub foo bullet" not in section.lines
        # The subheading line and the other bullet survive.
        assert "### Sub" in section.lines
        assert "- top bullet" in section.lines

    def test_remove_true_miss_still_noop_when_subsections_present(self):
        # If the match is absent from BOTH top region and subsections, the
        # remove is the usual quiet no-op (not a reject).
        doc = _doc("## Pref\n- top bullet\n### Sub\n- sub bullet\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "remove", "heading": "Pref", "match": "missing"}]
        )
        assert rejected == []
        assert applied and applied[0]["outcome"] == "noop_no_match"

    def test_remove_in_section_with_no_top_region_descends(self):
        # Section starts immediately with a `### subheading` — top region
        # is empty. A bullet under that subheading is still removable.
        doc = _doc("## Pref\n### Sub\n- sub foo bullet\n")
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "remove", "heading": "Pref", "match": "foo"}]
        )
        assert rejected == []
        assert applied and applied[0]["outcome"] == "applied"
        assert "- sub foo bullet" not in new_doc.find("Pref").lines

    def test_remove_uniqueness_spans_whole_section(self):
        # A needle that matches one top-region bullet AND one subsection
        # bullet is ambiguous now that remove spans the whole section.
        doc = _doc("## Pref\n- foo top\n### Sub\n- foo sub\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "remove", "heading": "Pref", "match": "foo"}]
        )
        assert applied == []
        assert rejected and rejected[0]["reason"] == "multiple_matches"

    def test_remove_never_removes_a_subheading_line(self):
        # `### …` lines are subheadings, not bullets — remove must ignore them
        # even when the match substring appears in the heading text.
        doc = _doc("## Pref\n### Editor settings\n- vs code\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "remove", "heading": "Pref", "match": "Editor"}]
        )
        assert applied and applied[0]["outcome"] == "noop_no_match"
        assert rejected == []


class TestReplace:
    def test_replace_unique_top_region_bullet(self):
        doc = _doc("## Pref\n- old wording\n- keep me\n")
        new_doc, applied, rejected = apply_ops(
            doc,
            [{"op": "replace", "heading": "Pref", "match": "old wording",
              "line": "new wording"}],
        )
        assert rejected == []
        assert applied and applied[0]["outcome"] == "applied"
        section = new_doc.find("Pref")
        assert "- new wording" in section.lines
        assert "- old wording" not in section.lines
        assert "- keep me" in section.lines

    def test_replace_descends_into_subsection_and_preserves_indent(self):
        doc = _doc("## Pref\n- top\n### Sub\n  - nested old\n")
        new_doc, applied, rejected = apply_ops(
            doc,
            [{"op": "replace", "heading": "Pref", "match": "nested old",
              "line": "nested new"}],
        )
        assert rejected == []
        assert applied and applied[0]["outcome"] == "applied"
        # Indentation of the original nested bullet is preserved.
        assert "  - nested new" in new_doc.find("Pref").lines

    def test_replace_normalizes_marker(self):
        doc = _doc("## Pref\n- old\n")
        new_doc, _, _ = apply_ops(
            doc, [{"op": "replace", "heading": "Pref", "match": "old", "line": "* starred"}]
        )
        assert "- starred" in new_doc.find("Pref").lines

    def test_replace_no_match_is_noop(self):
        doc = _doc("## Pref\n- foo\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "replace", "heading": "Pref", "match": "missing", "line": "x"}]
        )
        assert rejected == []
        assert applied and applied[0]["outcome"] == "noop_no_match"

    def test_replace_multiple_matches_rejected(self):
        doc = _doc("## Pref\n- foo apple\n- foo banana\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "replace", "heading": "Pref", "match": "foo", "line": "x"}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "multiple_matches"

    def test_replace_identical_text_is_noop_dup(self):
        doc = _doc("## Pref\n- same\n")
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "replace", "heading": "Pref", "match": "same", "line": "same"}]
        )
        assert rejected == []
        assert applied and applied[0]["outcome"] == "noop_dup"

    def test_replace_into_existing_other_bullet_is_noop_dup(self):
        # Rewriting one bullet to text another bullet already has must not
        # manufacture a duplicate — leave the doc unchanged.
        doc = _doc("## Pref\n- apple\n- banana\n")
        new_doc, applied, rejected = apply_ops(
            doc,
            [{"op": "replace", "heading": "Pref", "match": "apple", "line": "banana"}],
        )
        assert rejected == []
        assert applied and applied[0]["outcome"] == "noop_dup"
        bullets = [l for l in new_doc.find("Pref").lines if l.startswith("- ")]
        assert bullets == ["- apple", "- banana"]

    def test_replace_missing_heading_rejected(self):
        doc = _doc("## Pref\n- foo\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "replace", "heading": "Nope", "match": "foo", "line": "x"}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "heading_missing"

    def test_replace_empty_match_rejected(self):
        doc = _doc("## Pref\n- foo\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "replace", "heading": "Pref", "match": "  ", "line": "x"}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "empty_match"

    def test_replace_hash_prefixed_new_line_rejected(self):
        doc = _doc("## Pref\n- foo\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "replace", "heading": "Pref", "match": "foo", "line": "## h"}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "line_starts_with_hash"


class TestRemoveHeading:
    def test_remove_heading_drops_whole_section(self):
        doc = _doc("## A\n- a\n\n## B\n- b\n")
        new_doc, applied, rejected = apply_ops(
            doc, [{"op": "remove_heading", "heading": "A"}]
        )
        assert rejected == []
        assert applied and applied[0]["outcome"] == "applied"
        assert [s.heading for s in new_doc.sections] == ["B"]

    def test_remove_heading_missing_rejected(self):
        doc = _doc("## A\n- a\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "remove_heading", "heading": "Nope"}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "heading_missing"

    def test_remove_heading_with_subsections(self):
        doc = _doc("## A\n- a\n### Sub\n- s\n\n## B\n- b\n")
        new_doc, applied, _ = apply_ops(
            doc, [{"op": "remove_heading", "heading": "A"}]
        )
        assert applied[0]["outcome"] == "applied"
        assert new_doc.find("A") is None
        assert new_doc.find("B") is not None


class TestAppendSubheading:
    def test_append_under_existing_subheading(self):
        doc = _doc("## Pref\n- top\n### Editor\n- vs code\n")
        new_doc, applied, rejected = apply_ops(
            doc,
            [{"op": "append", "heading": "Pref", "subheading": "Editor",
              "line": "vim too"}],
        )
        assert rejected == []
        assert applied and applied[0]["outcome"] == "applied"
        section = new_doc.find("Pref")
        idx_sub = section.lines.index("### Editor")
        idx_new = section.lines.index("- vim too")
        idx_existing = section.lines.index("- vs code")
        # New bullet lands after the subheading and after the existing one.
        assert idx_new > idx_existing > idx_sub
        # Top region untouched.
        assert "- top" in section.lines

    def test_append_under_subheading_stops_at_next_subheading(self):
        doc = _doc("## Pref\n### A\n- a1\n### B\n- b1\n")
        new_doc, applied, rejected = apply_ops(
            doc,
            [{"op": "append", "heading": "Pref", "subheading": "A", "line": "a2"}],
        )
        assert rejected == []
        section = new_doc.find("Pref")
        idx_a2 = section.lines.index("- a2")
        idx_b = section.lines.index("### B")
        # The new bullet belongs to subsection A, before subheading B.
        assert idx_a2 < idx_b

    def test_append_under_subheading_dedups(self):
        doc = _doc("## Pref\n### Editor\n- vs code\n")
        new_doc, applied, rejected = apply_ops(
            doc,
            [{"op": "append", "heading": "Pref", "subheading": "Editor",
              "line": "vs code"}],
        )
        assert rejected == []
        assert applied and applied[0]["outcome"] == "noop_dup"

    def test_append_missing_subheading_rejected(self):
        doc = _doc("## Pref\n- top\n### Editor\n- vs code\n")
        _, applied, rejected = apply_ops(
            doc,
            [{"op": "append", "heading": "Pref", "subheading": "Nope", "line": "x"}],
        )
        assert applied == []
        assert rejected[0]["reason"] == "subheading_missing"

    def test_append_without_subheading_unchanged_top_region_behavior(self):
        # Omitting subheading keeps the established top-region append.
        doc = _doc("## Pref\n- top\n### Editor\n- vs code\n")
        new_doc, applied, _ = apply_ops(
            doc, [{"op": "append", "heading": "Pref", "line": "another top"}]
        )
        section = new_doc.find("Pref")
        idx_new = section.lines.index("- another top")
        idx_sub = section.lines.index("### Editor")
        assert idx_new < idx_sub


class TestBatch:
    def test_apply_batch_partial_success_continues_after_rejection(self):
        doc = _doc("## A\n- a\n")
        ops = [
            {"op": "append", "heading": "Missing", "line": "- x"},  # rejected
            {"op": "append", "heading": "A", "line": "- new"},  # applied
        ]
        new_doc, applied, rejected = apply_ops(doc, ops)
        assert len(applied) == 1
        assert len(rejected) == 1
        assert "- new" in new_doc.find("A").lines

    def test_apply_batch_returns_applied_and_rejected_separately(self):
        doc = _doc("## A\n- a\n")
        ops = [
            {"op": "remove", "heading": "A", "match": "a"},
            {"op": "append", "heading": "Missing", "line": "- x"},
        ]
        _, applied, rejected = apply_ops(doc, ops)
        assert len(applied) == 1
        assert len(rejected) == 1

    def test_apply_batch_does_not_mutate_input_doc(self):
        text = "## A\n- a\n"
        doc = _doc(text)
        original_serialized = serialize_sectioned_doc(doc)
        apply_ops(doc, [{"op": "append", "heading": "A", "line": "- new"}])
        assert serialize_sectioned_doc(doc) == original_serialized

    def test_apply_batch_op_with_unknown_field_accepted(self):
        # Forward-compat: extra fields on a valid op are tolerated.
        doc = _doc("## A\n- a\n")
        ops = [
            {
                "op": "append",
                "heading": "A",
                "line": "- new",
                "reason": "explanation",
            }
        ]
        new_doc, applied, rejected = apply_ops(doc, ops)
        assert applied and not rejected
        assert "- new" in new_doc.find("A").lines

    def test_unknown_op_rejected(self):
        doc = _doc("## A\n- a\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "delete_section", "heading": "A"}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "unknown_op"

    def test_op_missing_required_field_rejected(self):
        doc = _doc("## A\n- a\n")
        _, applied, rejected = apply_ops(
            doc, [{"op": "append", "heading": "A"}]  # missing 'line'
        )
        assert applied == []
        assert rejected[0]["reason"] == "missing_field"
