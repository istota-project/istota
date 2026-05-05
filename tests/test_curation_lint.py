"""Tests for the Phase-A USER.md lint pass."""

from __future__ import annotations

from istota.memory.curation.lint import (
    find_temporal_bullets,
    prepend_agents_header_if_missing,
)
from istota.memory.curation.parser import parse_sectioned_doc


def _doc(text: str):
    return parse_sectioned_doc(text)


class TestFindTemporalBullets:
    def test_acquired_on_date_is_a_match(self):
        doc = _doc(
            "## Notes\n\n- stefan ordered a Lamy 2000 on 2026-04-15\n\n"
        )
        out = find_temporal_bullets(doc, kg_facts_text="")
        assert len(out) == 1
        assert out[0].suggested_predicate == "acquired"
        assert "lamy 2000" in (out[0].suggested_object or "").lower()
        assert out[0].suggested_valid_from == "2026-04-15"

    def test_noted_suffix_alone_is_not_a_match(self):
        doc = _doc(
            "## Preferences\n\n- Prefers morning meetings (noted 2026-04-12)\n\n"
        )
        out = find_temporal_bullets(doc, kg_facts_text="")
        assert out == []

    def test_decided_with_noted_suffix_is_not_a_match(self):
        doc = _doc(
            "## Notes\n\n- decided to default to short replies (noted 2026-04-12)\n\n"
        )
        out = find_temporal_bullets(doc, kg_facts_text="")
        assert out == []

    def test_temporal_under_behavior_heading_is_skipped(self):
        doc = _doc(
            "## Communication style\n\n- started signing emails as Zorg on 2026-04-12\n\n"
        )
        out = find_temporal_bullets(doc, kg_facts_text="")
        assert out == []

    def test_subsection_bullet_not_matched(self):
        doc = _doc(
            "## Notes\n\n### Email\n\n- bought a Lamy 2000 on 2026-04-15\n\n"
        )
        out = find_temporal_bullets(doc, kg_facts_text="")
        assert out == []

    def test_kg_dedup_pre_check_filters(self):
        doc = _doc(
            "## Notes\n\n- stefan ordered a Lamy 2000 on 2026-04-15\n\n"
        )
        kg = "stefan acquired lamy 2000 (since 2026-04-15)"
        out = find_temporal_bullets(doc, kg_facts_text=kg)
        assert out == []

    def test_max_candidates_capped(self):
        bullets = "\n".join(
            f"- ordered a Pen{i} on 2026-04-{i:02d}" for i in range(1, 10)
        )
        doc = _doc(f"## Notes\n\n{bullets}\n\n")
        out = find_temporal_bullets(doc, kg_facts_text="", max_candidates=3)
        assert len(out) == 3

    def test_lead_date_form(self):
        doc = _doc(
            "## Notes\n\n- 2026-05-04: visited a fountain pen shop\n\n"
        )
        out = find_temporal_bullets(doc, kg_facts_text="")
        assert len(out) == 1
        assert out[0].suggested_valid_from == "2026-05-04"
        assert out[0].suggested_predicate is None

    def test_pen_failure_case_in_paren_form_not_caught(self):
        # The original failure-case bullets used `(2026-05-03)`, not
        # `on 2026-05-03`. The lint pass intentionally does NOT catch
        # them — the forward fix is the classification gate at write
        # time, not retroactive lint.
        doc = _doc(
            "## Notes\n\n"
            "- stefan ordered a pilot prera fountain pen (2026-05-03)\n\n"
        )
        out = find_temporal_bullets(doc, kg_facts_text="")
        assert out == []


class TestAgentsHeaderMigration:
    def test_idempotent(self):
        text = "# User Memory\n\n## Notes\n\n- foo\n"
        new1, changed1 = prepend_agents_header_if_missing(text)
        assert changed1
        assert "<!-- agents:" in new1
        new2, changed2 = prepend_agents_header_if_missing(new1)
        assert not changed2
        assert new2 == new1

    def test_already_present_substring_match(self):
        text = "<!-- agents: custom -->\n# User Memory\n"
        new, changed = prepend_agents_header_if_missing(text)
        assert not changed
        assert new == text
