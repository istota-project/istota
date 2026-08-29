"""`remove_subheading`, and the delete-on-empty rule for a `### ` subsection.

Two gaps found while migrating a real `USER.md` into per-skill overlays
(ISSUE-341 items 5 and 6):

- Nothing could remove a `### ` subsection. `remove_heading` drops only `## `
  sections, and `remove` targets bullets — so a subsection made of prose,
  numbered items or bold sub-headers could not be removed through the
  sanctioned path at all.
- Removing a subsection's last bullet left the heading behind, orphaned. The
  file-level rule ("a `remove` that empties a file deletes the file, so `ls`
  stays honest") already says what should happen one level up.

Both were fixed on USER.md and on the per-skill overlays at once. The overlay
half is gone with the overlay write verbs (ISSUE-343); what is left here is
the USER.md half, which is where the rule now lives alone.
"""

import pytest

from istota.memory.curation.ops import apply_ops
from istota.memory.curation.parser import parse_sectioned_doc, serialize_sectioned_doc
from istota.memory.curation.types import subsection_region_indices

DOC = """## Notes

- a top-region bullet

### Todo list

- the todo path

### Handwritten journal workflow

All handwritten journal pages get transcribed and routed by their header line.

**Header format:** a date and a category.

1. Uncategorized entries save to Daily/
2. Category = project name searches Projects/

### Keep me

- this subsection is untouched

## Communication style

- unrelated
"""


def _sections(text):
    return parse_sectioned_doc(text)


class TestRemoveSubheading:
    def test_removes_a_prose_subsection_whole(self):
        """The case nothing could do: a subsection with no bullets in it."""
        doc = _sections(DOC)
        doc, applied, rejected = apply_ops(
            doc,
            [{"op": "remove_subheading", "heading": "Notes",
              "subheading": "Handwritten journal workflow"}],
        )
        assert rejected == []
        assert applied[0]["outcome"] == "applied"

        out = serialize_sectioned_doc(doc)
        assert "Handwritten journal workflow" not in out
        assert "All handwritten journal pages" not in out
        assert "**Header format:**" not in out
        assert "1. Uncategorized entries save to Daily/" not in out
        # Its neighbours survive, on both sides.
        assert "### Todo list" in out
        assert "- the todo path" in out
        assert "### Keep me" in out
        assert "- this subsection is untouched" in out

    def test_removes_the_heading_line_itself_not_just_the_body(self):
        doc = _sections(DOC)
        doc, _, _ = apply_ops(
            doc, [{"op": "remove_subheading", "heading": "Notes", "subheading": "Todo list"}]
        )
        out = serialize_sectioned_doc(doc)
        assert "### Todo list" not in out
        assert "- the todo path" not in out

    def test_the_top_region_is_never_touched(self):
        doc = _sections(DOC)
        doc, _, _ = apply_ops(
            doc, [{"op": "remove_subheading", "heading": "Notes", "subheading": "Todo list"}]
        )
        assert "- a top-region bullet" in serialize_sectioned_doc(doc)

    def test_other_sections_are_never_touched(self):
        doc = _sections(DOC)
        doc, _, _ = apply_ops(
            doc, [{"op": "remove_subheading", "heading": "Notes", "subheading": "Todo list"}]
        )
        out = serialize_sectioned_doc(doc)
        assert "## Communication style" in out
        assert "- unrelated" in out

    def test_missing_subheading_is_rejected_not_applied(self):
        doc = _sections(DOC)
        doc, applied, rejected = apply_ops(
            doc, [{"op": "remove_subheading", "heading": "Notes", "subheading": "Nope"}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "subheading_missing"
        assert "### Todo list" in serialize_sectioned_doc(doc)

    def test_missing_heading_is_rejected(self):
        doc = _sections(DOC)
        _, applied, rejected = apply_ops(
            doc, [{"op": "remove_subheading", "heading": "Nope", "subheading": "Todo list"}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "heading_missing"

    def test_missing_field_is_rejected(self):
        doc = _sections(DOC)
        _, applied, rejected = apply_ops(
            doc, [{"op": "remove_subheading", "heading": "Notes"}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "missing_field"

    def test_an_empty_subheading_name_does_not_match_the_first_subsection(self):
        """`subsection_region_indices` returns None for an empty target; the op
        must reject rather than deleting whichever subsection came first."""
        doc = _sections(DOC)
        _, applied, rejected = apply_ops(
            doc, [{"op": "remove_subheading", "heading": "Notes", "subheading": "   "}]
        )
        assert applied == []
        assert rejected[0]["reason"] == "subheading_missing"


class TestDeleteOnEmptySubsection:
    def test_removing_the_last_bullet_takes_the_heading_with_it(self):
        doc = _sections(DOC)
        doc, applied, rejected = apply_ops(
            doc, [{"op": "remove", "heading": "Notes", "match": "the todo path"}]
        )
        assert rejected == []
        assert applied[0]["outcome"] == "applied"
        out = serialize_sectioned_doc(doc)
        assert "- the todo path" not in out
        assert "### Todo list" not in out, "an emptied subsection must not be left orphaned"

    def test_a_subsection_with_bullets_left_keeps_its_heading(self):
        text = DOC.replace("- the todo path", "- one\n- two")
        doc = _sections(text)
        doc, _, _ = apply_ops(doc, [{"op": "remove", "heading": "Notes", "match": "one"}])
        out = serialize_sectioned_doc(doc)
        assert "### Todo list" in out
        assert "- two" in out

    def test_a_subsection_still_holding_prose_keeps_its_heading(self):
        """Only a subsection that is *empty* goes. Prose is content, not blank."""
        text = DOC.replace(
            "### Todo list\n\n- the todo path",
            "### Todo list\n\nSome prose that is not a bullet.\n\n- the todo path",
        )
        doc = _sections(text)
        doc, _, _ = apply_ops(doc, [{"op": "remove", "heading": "Notes", "match": "the todo path"}])
        out = serialize_sectioned_doc(doc)
        assert "### Todo list" in out
        assert "Some prose that is not a bullet." in out

    def test_removing_a_top_region_bullet_removes_no_heading(self):
        doc = _sections(DOC)
        doc, _, _ = apply_ops(
            doc, [{"op": "remove", "heading": "Notes", "match": "a top-region bullet"}]
        )
        out = serialize_sectioned_doc(doc)
        assert "### Todo list" in out
        assert "### Keep me" in out


class TestRegionHelperUnchanged:
    """`subsection_region_indices` keeps excluding the heading line — every
    existing caller wants the bullet region, and the removal path is what
    needed the heading index."""

    @pytest.mark.parametrize("name", ["Todo list", "todo LIST", "### Todo list"])
    def test_start_is_the_line_after_the_heading(self, name):
        section = _sections(DOC).find("Notes")
        region = subsection_region_indices(section, name)
        assert region is not None
        start, _ = region
        assert section.lines[start - 1].strip().lstrip("#").strip() == "Todo list"


class TestRemoveSubheadingCli:
    """The verb end to end, through the skill CLI."""

    def test_drops_a_prose_subsection_from_user_md(self, tmp_path, monkeypatch, capsys):
        import json

        from istota.skills.memory import main as memory_main

        mount = tmp_path / "mount"
        cfg = mount / "Users" / "alice" / "istota" / "config"
        cfg.mkdir(parents=True)
        (cfg / "USER.md").write_text(DOC)
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")
        monkeypatch.delenv("ISTOTA_CONVERSATION_TOKEN", raising=False)

        memory_main([
            "remove-subheading", "--heading", "Notes",
            "--subheading", "Handwritten journal workflow",
        ])
        out = json.loads(capsys.readouterr().out)
        assert out["outcome"] == "applied"

        body = (cfg / "USER.md").read_text()
        assert "Handwritten journal workflow" not in body
        assert "All handwritten journal pages" not in body
        assert "### Todo list" in body
        assert "## Communication style" in body

    def test_user_md_requires_a_subheading(self, tmp_path, monkeypatch, capsys):
        import json

        import pytest as _pytest

        from istota.skills.memory import main as memory_main

        mount = tmp_path / "mount"
        cfg = mount / "Users" / "alice" / "istota" / "config"
        cfg.mkdir(parents=True)
        (cfg / "USER.md").write_text(DOC)
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_BOT_DIR_NAME", "istota")

        with _pytest.raises(SystemExit):
            memory_main(["remove-subheading", "--heading", "Notes"])
        assert json.loads(capsys.readouterr().out)["error"] == "subheading_required"
        assert "### Todo list" in (cfg / "USER.md").read_text()
