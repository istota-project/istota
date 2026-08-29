"""Tests for the Phase-A USER.md lint pass."""

from __future__ import annotations

from datetime import date, timedelta

from istota.memory.curation.lint import (
    LINT_SEEN_TTL_DAYS,
    TemporalBulletCandidate,
    filter_unseen_candidates,
    find_temporal_bullets,
    prepend_agents_header_if_missing,
)
from istota.memory.curation.parser import parse_sectioned_doc


def _doc(text: str):
    return parse_sectioned_doc(text)


class TestFindTemporalBullets:
    def test_acquired_on_date_is_a_match(self):
        doc = _doc(
            "## Notes\n\n- alice ordered a Lamy 2000 on 2026-04-15\n\n"
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
            "## Communication style\n\n- started signing emails as Istota on 2026-04-12\n\n"
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
            "## Notes\n\n- alice ordered a Lamy 2000 on 2026-04-15\n\n"
        )
        kg = "alice acquired lamy 2000 (since 2026-04-15)"
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
            "- alice ordered a arc desk lamp (2026-05-03)\n\n"
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


def _cand(heading: str, bullet: str) -> TemporalBulletCandidate:
    return TemporalBulletCandidate(
        heading=heading,
        bullet_text=bullet,
        suggested_predicate="acquired",
        suggested_object=bullet,
        suggested_valid_from="2026-04-15",
    )


class TestFilterUnseenCandidates:
    """The seen set is a plain dict in and a plain dict out; the caller stores
    it (in the KV store, via `curation.audit`). These used to round-trip it
    through a `USER.md.lint_seen.json` sidecar the function wrote itself."""

    def test_first_run_keeps_all_and_records_them(self):
        cands = [_cand("Notes", "ordered a Lamy 2000 on 2026-04-15")]
        out, seen = filter_unseen_candidates(cands, {}, today=date(2026, 5, 5))
        assert len(out) == 1
        assert len(seen) == 1
        assert next(iter(seen.values())) == "2026-05-05"

    def test_second_run_within_ttl_drops_seen(self):
        cands = [_cand("Notes", "ordered a Lamy 2000 on 2026-04-15")]
        _, seen = filter_unseen_candidates(cands, {}, today=date(2026, 5, 5))
        out, _ = filter_unseen_candidates(cands, seen, today=date(2026, 5, 6))
        assert out == []

    def test_after_ttl_resurfaces(self):
        cands = [_cand("Notes", "ordered a Lamy 2000 on 2026-04-15")]
        _, seen = filter_unseen_candidates(cands, {}, today=date(2026, 5, 5))
        future = date(2026, 5, 5) + timedelta(days=LINT_SEEN_TTL_DAYS + 1)
        out, _ = filter_unseen_candidates(cands, seen, today=future)
        assert len(out) == 1

    def test_unseen_candidate_passes_through(self):
        first = [_cand("Notes", "ordered pen A on 2026-04-15")]
        _, seen = filter_unseen_candidates(first, {}, today=date(2026, 5, 5))
        mixed = [
            _cand("Notes", "ordered pen A on 2026-04-15"),
            _cand("Notes", "ordered pen B on 2026-04-20"),
        ]
        out, _ = filter_unseen_candidates(mixed, seen, today=date(2026, 5, 6))
        assert len(out) == 1
        assert out[0].bullet_text.endswith("pen B on 2026-04-20")

    def test_a_non_dict_seen_set_is_treated_as_empty(self):
        """`read_lint_seen` already coerces, but a corrupt stored value must
        not reach this function's `.items()` and raise inside a nightly pass."""
        cands = [_cand("Notes", "ordered a Lamy 2000 on 2026-04-15")]
        out, seen = filter_unseen_candidates(cands, None, today=date(2026, 5, 5))
        assert len(out) == 1
        assert len(seen) == 1

    def test_pruning_drops_stale_entries(self):
        seen_in = {
            "0" * 16: "2025-01-01",  # well past TTL
            "1" * 16: "2026-05-01",  # within TTL
        }
        cands = [_cand("Notes", "ordered a Lamy 2000 on 2026-04-15")]
        _, seen = filter_unseen_candidates(cands, seen_in, today=date(2026, 5, 5))
        assert "0" * 16 not in seen
        assert "1" * 16 in seen
        assert len(seen) == 2  # the one from `cands` plus the within-TTL one

    def test_the_input_dict_is_not_mutated(self):
        """The caller stores the returned set; mutating the argument would
        make a failed write look like a successful one on the next read."""
        seen_in = {"0" * 16: "2026-05-01"}
        cands = [_cand("Notes", "ordered a Lamy 2000 on 2026-04-15")]
        filter_unseen_candidates(cands, seen_in, today=date(2026, 5, 5))
        assert seen_in == {"0" * 16: "2026-05-01"}
