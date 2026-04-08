"""Tests for the temporal knowledge graph module."""

import sqlite3
from pathlib import Path

import pytest

from istota.knowledge_graph import (
    KnowledgeFact,
    SINGLE_VALUED_PREDICATES,
    TEMPORARY_PREDICATES,
    add_fact,
    delete_fact,
    ensure_table,
    format_facts_for_prompt,
    get_current_facts,
    get_entity_timeline,
    get_fact,
    get_fact_count,
    get_facts_as_of,
    invalidate_fact,
)


@pytest.fixture
def conn(tmp_path):
    """Create a test database with knowledge_facts table."""
    db_path = tmp_path / "test.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    ensure_table(conn)
    return conn


class TestEnsureTable:
    def test_creates_table(self, conn):
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        assert "knowledge_facts" in tables

    def test_idempotent(self, conn):
        """Calling ensure_table twice doesn't error."""
        ensure_table(conn)
        tables = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )]
        assert "knowledge_facts" in tables

    def test_indexes_created(self, conn):
        indexes = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='knowledge_facts'"
        )]
        assert "idx_kf_user_subject" in indexes
        assert "idx_kf_user_predicate" in indexes
        assert "idx_kf_current" in indexes


class TestAddFact:
    def test_basic_insert(self, conn):
        fact_id = add_fact(conn, "user1", "Stefan", "works_at", "Acme")
        assert fact_id is not None
        fact = get_fact(conn, fact_id)
        assert fact.subject == "stefan"
        assert fact.predicate == "works_at"
        assert fact.object == "acme"
        assert fact.user_id == "user1"
        assert fact.valid_until is None
        assert fact.temporary is False

    def test_normalizes_input(self, conn):
        fact_id = add_fact(conn, "user1", "  Stefan  ", "Works_At", "  Acme Corp  ")
        fact = get_fact(conn, fact_id)
        assert fact.subject == "stefan"
        assert fact.predicate == "works_at"
        assert fact.object == "acme corp"

    def test_duplicate_detection(self, conn):
        id1 = add_fact(conn, "user1", "stefan", "knows", "python")
        id2 = add_fact(conn, "user1", "stefan", "knows", "python")
        assert id1 is not None
        assert id2 is None  # Duplicate

    def test_duplicate_only_checks_current(self, conn):
        """A fact that has been invalidated can be re-added."""
        id1 = add_fact(conn, "user1", "stefan", "knows", "python")
        invalidate_fact(conn, id1, ended="2026-01-01")
        id2 = add_fact(conn, "user1", "stefan", "knows", "python")
        assert id2 is not None
        assert id2 != id1

    def test_different_users_not_duplicate(self, conn):
        id1 = add_fact(conn, "user1", "stefan", "knows", "python")
        id2 = add_fact(conn, "user2", "stefan", "knows", "python")
        assert id1 is not None
        assert id2 is not None

    def test_with_valid_from(self, conn):
        fact_id = add_fact(conn, "user1", "stefan", "works_at", "acme",
                           valid_from="2025-06-01")
        fact = get_fact(conn, fact_id)
        assert fact.valid_from == "2025-06-01"

    def test_with_source_tracking(self, conn):
        fact_id = add_fact(conn, "user1", "stefan", "works_at", "acme",
                           source_task_id=123, source_type="user_stated")
        fact = get_fact(conn, fact_id)
        assert fact.source_task_id == 123
        assert fact.source_type == "user_stated"

    def test_with_confidence(self, conn):
        fact_id = add_fact(conn, "user1", "stefan", "knows", "rust", confidence=0.7)
        fact = get_fact(conn, fact_id)
        assert fact.confidence == 0.7


class TestSupersession:
    def test_single_valued_supersedes(self, conn):
        """New value for single-valued predicate supersedes old one."""
        id1 = add_fact(conn, "user1", "stefan", "works_at", "acme",
                       valid_from="2025-01-01")
        id2 = add_fact(conn, "user1", "stefan", "works_at", "globex",
                       valid_from="2026-04-01")

        old = get_fact(conn, id1)
        new = get_fact(conn, id2)
        assert old.valid_until == "2026-04-01"  # Superseded
        assert new.valid_until is None  # Current

    def test_single_valued_supersession_uses_today_when_no_valid_from(self, conn):
        """When new fact has no valid_from, supersession date is today."""
        from datetime import date as date_cls
        id1 = add_fact(conn, "user1", "stefan", "lives_in", "brooklyn")
        id2 = add_fact(conn, "user1", "stefan", "lives_in", "manhattan")

        old = get_fact(conn, id1)
        assert old.valid_until == date_cls.today().isoformat()

    def test_multi_valued_no_supersession(self, conn):
        """Multi-valued predicates allow concurrent facts."""
        id1 = add_fact(conn, "user1", "stefan", "knows", "python")
        id2 = add_fact(conn, "user1", "stefan", "knows", "go")

        fact1 = get_fact(conn, id1)
        fact2 = get_fact(conn, id2)
        assert fact1.valid_until is None  # Both current
        assert fact2.valid_until is None

    def test_all_single_valued_predicates_supersede(self, conn):
        """Every predicate in SINGLE_VALUED_PREDICATES triggers supersession."""
        for pred in SINGLE_VALUED_PREDICATES:
            id1 = add_fact(conn, "user1", "test", pred, "old_value")
            id2 = add_fact(conn, "user1", "test", pred, "new_value")
            old = get_fact(conn, id1)
            assert old.valid_until is not None, f"{pred} should supersede"

    def test_supersession_scoped_to_user(self, conn):
        """Supersession only affects same user's facts."""
        id1 = add_fact(conn, "user1", "stefan", "works_at", "acme")
        id2 = add_fact(conn, "user2", "stefan", "works_at", "globex")

        fact1 = get_fact(conn, id1)
        assert fact1.valid_until is None  # User1's fact untouched


class TestTemporaryFacts:
    def test_temporary_flag_set(self, conn):
        fact_id = add_fact(conn, "user1", "stefan", "works_at", "acme", temporary=True)
        fact = get_fact(conn, fact_id)
        assert fact.temporary is True

    def test_temporary_predicate_auto_flags(self, conn):
        """Predicates in TEMPORARY_PREDICATES are auto-flagged as temporary."""
        fact_id = add_fact(conn, "user1", "stefan", "staying_in", "warsaw")
        fact = get_fact(conn, fact_id)
        assert fact.temporary is True

    def test_temporary_does_not_supersede_permanent(self, conn):
        """Temporary facts coexist with permanent facts."""
        id_perm = add_fact(conn, "user1", "stefan", "lives_in", "brooklyn")
        id_temp = add_fact(conn, "user1", "stefan", "lives_in", "warsaw",
                           temporary=True, valid_until="2026-07-01")

        perm = get_fact(conn, id_perm)
        temp = get_fact(conn, id_temp)
        assert perm.valid_until is None  # Permanent still current
        assert temp.valid_until == "2026-07-01"
        assert temp.temporary is True

    def test_permanent_supersedes_even_with_temporary_present(self, conn):
        """A new permanent fact supersedes old permanent, ignoring temporaries."""
        id_old = add_fact(conn, "user1", "stefan", "lives_in", "brooklyn")
        add_fact(conn, "user1", "stefan", "staying_in", "warsaw",
                 temporary=True, valid_until="2026-07-01")
        id_new = add_fact(conn, "user1", "stefan", "lives_in", "manhattan",
                          valid_from="2026-08-01")

        old = get_fact(conn, id_old)
        new = get_fact(conn, id_new)
        assert old.valid_until == "2026-08-01"  # Superseded
        assert new.valid_until is None  # Current


class TestInvalidateFact:
    def test_invalidate(self, conn):
        fact_id = add_fact(conn, "user1", "stefan", "knows", "python")
        result = invalidate_fact(conn, fact_id, ended="2026-04-08")
        assert result is True
        fact = get_fact(conn, fact_id)
        assert fact.valid_until == "2026-04-08"

    def test_invalidate_defaults_to_today(self, conn):
        from datetime import date as date_cls
        fact_id = add_fact(conn, "user1", "stefan", "knows", "python")
        invalidate_fact(conn, fact_id)
        fact = get_fact(conn, fact_id)
        assert fact.valid_until == date_cls.today().isoformat()

    def test_invalidate_already_invalidated(self, conn):
        fact_id = add_fact(conn, "user1", "stefan", "knows", "python")
        invalidate_fact(conn, fact_id, ended="2026-01-01")
        result = invalidate_fact(conn, fact_id, ended="2026-04-08")
        assert result is False  # Already had valid_until

    def test_invalidate_nonexistent(self, conn):
        result = invalidate_fact(conn, 9999)
        assert result is False


class TestDeleteFact:
    def test_delete(self, conn):
        fact_id = add_fact(conn, "user1", "stefan", "knows", "python")
        result = delete_fact(conn, fact_id)
        assert result is True
        assert get_fact(conn, fact_id) is None

    def test_delete_nonexistent(self, conn):
        result = delete_fact(conn, 9999)
        assert result is False


class TestGetCurrentFacts:
    def test_returns_only_current(self, conn):
        add_fact(conn, "user1", "stefan", "works_at", "acme")
        add_fact(conn, "user1", "stefan", "works_at", "globex",
                 valid_from="2026-04-01")

        facts = get_current_facts(conn, "user1")
        assert len(facts) == 1
        assert facts[0].object == "globex"

    def test_filter_by_subject(self, conn):
        add_fact(conn, "user1", "stefan", "knows", "python")
        add_fact(conn, "user1", "istota", "uses_tech", "svelte")

        facts = get_current_facts(conn, "user1", subject="stefan")
        assert len(facts) == 1
        assert facts[0].subject == "stefan"

    def test_filter_by_predicate(self, conn):
        add_fact(conn, "user1", "stefan", "knows", "python")
        add_fact(conn, "user1", "stefan", "works_at", "acme")

        facts = get_current_facts(conn, "user1", predicate="knows")
        assert len(facts) == 1
        assert facts[0].predicate == "knows"

    def test_empty_result(self, conn):
        facts = get_current_facts(conn, "user1")
        assert facts == []

    def test_scoped_to_user(self, conn):
        add_fact(conn, "user1", "stefan", "knows", "python")
        add_fact(conn, "user2", "alice", "knows", "go")

        facts = get_current_facts(conn, "user1")
        assert len(facts) == 1
        assert facts[0].subject == "stefan"


class TestGetFactsAsOf:
    def test_historical_query(self, conn):
        id1 = add_fact(conn, "user1", "stefan", "works_at", "acme",
                       valid_from="2025-01-01")
        add_fact(conn, "user1", "stefan", "works_at", "globex",
                 valid_from="2026-04-01")

        # Query when at Acme
        facts = get_facts_as_of(conn, "user1", "2025-06-15")
        assert len(facts) == 1
        assert facts[0].object == "acme"

    def test_current_date_query(self, conn):
        add_fact(conn, "user1", "stefan", "works_at", "acme",
                 valid_from="2025-01-01")
        add_fact(conn, "user1", "stefan", "works_at", "globex",
                 valid_from="2026-04-01")

        facts = get_facts_as_of(conn, "user1", "2026-06-01")
        assert len(facts) == 1
        assert facts[0].object == "globex"

    def test_before_any_facts(self, conn):
        add_fact(conn, "user1", "stefan", "works_at", "acme",
                 valid_from="2025-01-01")

        facts = get_facts_as_of(conn, "user1", "2024-01-01")
        assert len(facts) == 0

    def test_null_valid_from_always_matches(self, conn):
        """Facts with no valid_from are considered always valid from the start."""
        add_fact(conn, "user1", "stefan", "knows", "python")  # No valid_from

        facts = get_facts_as_of(conn, "user1", "2020-01-01")
        assert len(facts) == 1

    def test_filter_by_subject(self, conn):
        add_fact(conn, "user1", "stefan", "knows", "python")
        add_fact(conn, "user1", "istota", "uses_tech", "svelte")

        facts = get_facts_as_of(conn, "user1", "2026-06-01", subject="stefan")
        assert len(facts) == 1
        assert facts[0].subject == "stefan"


class TestGetEntityTimeline:
    def test_chronological_order(self, conn):
        add_fact(conn, "user1", "stefan", "works_at", "acme",
                 valid_from="2025-01-01")
        add_fact(conn, "user1", "stefan", "works_at", "globex",
                 valid_from="2026-04-01")
        add_fact(conn, "user1", "stefan", "knows", "python",
                 valid_from="2020-01-01")

        timeline = get_entity_timeline(conn, "user1", "stefan")
        assert len(timeline) == 3
        # Ordered by valid_from (or created_at if null)
        assert timeline[0].object == "python"
        assert timeline[1].object == "acme"
        assert timeline[2].object == "globex"

    def test_includes_historical_facts(self, conn):
        id1 = add_fact(conn, "user1", "stefan", "works_at", "acme",
                       valid_from="2025-01-01")
        invalidate_fact(conn, id1, ended="2026-01-01")
        add_fact(conn, "user1", "stefan", "works_at", "globex",
                 valid_from="2026-01-01")

        timeline = get_entity_timeline(conn, "user1", "stefan")
        assert len(timeline) == 2

    def test_empty_timeline(self, conn):
        timeline = get_entity_timeline(conn, "user1", "nobody")
        assert timeline == []

    def test_normalizes_subject(self, conn):
        add_fact(conn, "user1", "Stefan", "knows", "python")
        timeline = get_entity_timeline(conn, "user1", "  stefan  ")
        assert len(timeline) == 1


class TestGetFact:
    def test_returns_fact(self, conn):
        fact_id = add_fact(conn, "user1", "stefan", "knows", "python")
        fact = get_fact(conn, fact_id)
        assert fact is not None
        assert fact.id == fact_id

    def test_returns_none_for_nonexistent(self, conn):
        assert get_fact(conn, 9999) is None


class TestGetFactCount:
    def test_counts(self, conn):
        add_fact(conn, "user1", "stefan", "works_at", "acme",
                 valid_from="2025-01-01")
        add_fact(conn, "user1", "stefan", "works_at", "globex",
                 valid_from="2026-04-01")
        add_fact(conn, "user1", "stefan", "knows", "python")

        counts = get_fact_count(conn, "user1")
        assert counts["total"] == 3
        assert counts["current"] == 2  # globex + python
        assert counts["historical"] == 1  # acme (superseded)

    def test_empty(self, conn):
        counts = get_fact_count(conn, "user1")
        assert counts == {"total": 0, "current": 0, "historical": 0}


class TestFormatFactsForPrompt:
    def test_basic_format(self, conn):
        add_fact(conn, "user1", "stefan", "works_at", "acme",
                 valid_from="2025-06-01")
        add_fact(conn, "user1", "stefan", "knows", "python")

        facts = get_current_facts(conn, "user1")
        text = format_facts_for_prompt(facts)
        assert "stefan works_at acme (since 2025-06-01)" in text
        assert "stefan knows python" in text

    def test_temporary_marker(self, conn):
        add_fact(conn, "user1", "stefan", "staying_in", "warsaw",
                 valid_until="2026-07-01")

        facts = get_current_facts(conn, "user1")
        text = format_facts_for_prompt(facts)
        assert "[temporary]" in text

    def test_empty_facts(self):
        assert format_facts_for_prompt([]) == ""
