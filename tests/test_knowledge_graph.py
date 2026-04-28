"""Tests for the temporal knowledge graph module."""

import sqlite3
from pathlib import Path

import pytest

from istota.memory.knowledge_graph import (
    KnowledgeFact,
    SINGLE_VALUED_PREDICATES,
    TEMPORARY_PREDICATES,
    _normalize,
    _fact_similarity,
    _tokenize,
    add_fact,
    cleanup_old_audit_rows,
    delete_fact,
    ensure_table,
    format_facts_for_prompt,
    get_current_facts,
    get_entity_timeline,
    get_fact,
    get_fact_count,
    get_facts_as_of,
    get_fact_history,
    invalidate_fact,
    select_relevant_facts,
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
        assert "idx_kf_unique_current" in indexes

    def test_unique_index_blocks_duplicate_current_facts(self, conn):
        """Raw INSERT of duplicate current triple is blocked by unique index."""
        conn.execute(
            "INSERT INTO knowledge_facts (user_id, subject, predicate, object) "
            "VALUES (?, ?, ?, ?)",
            ("user1", "stefan", "knows", "python"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO knowledge_facts (user_id, subject, predicate, object) "
                "VALUES (?, ?, ?, ?)",
                ("user1", "stefan", "knows", "python"),
            )

    def test_unique_index_allows_historical_duplicates(self, conn):
        """Same triple can appear multiple times in historical record."""
        conn.execute(
            "INSERT INTO knowledge_facts (user_id, subject, predicate, object, valid_until) "
            "VALUES (?, ?, ?, ?, ?)",
            ("user1", "stefan", "knows", "python", "2026-01-01"),
        )
        conn.execute(
            "INSERT INTO knowledge_facts (user_id, subject, predicate, object, valid_until) "
            "VALUES (?, ?, ?, ?, ?)",
            ("user1", "stefan", "knows", "python", "2026-02-01"),
        )
        # One current, two historical — no error
        conn.execute(
            "INSERT INTO knowledge_facts (user_id, subject, predicate, object) "
            "VALUES (?, ?, ?, ?)",
            ("user1", "stefan", "knows", "python"),
        )

    def test_ensure_table_survives_existing_duplicates(self, tmp_path, caplog):
        """If current-fact duplicates exist, ensure_table logs warning but does not crash."""
        import logging
        db_path = tmp_path / "dup.db"
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        # Create table without unique index, insert dupes
        conn.execute("""
            CREATE TABLE knowledge_facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                subject TEXT NOT NULL,
                predicate TEXT NOT NULL,
                object TEXT NOT NULL,
                valid_from TEXT,
                valid_until TEXT,
                temporary INTEGER DEFAULT 0,
                confidence REAL DEFAULT 1.0,
                source_task_id INTEGER,
                source_type TEXT DEFAULT 'extracted',
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            )
        """)
        for _ in range(2):
            conn.execute(
                "INSERT INTO knowledge_facts (user_id, subject, predicate, object) "
                "VALUES (?, ?, ?, ?)",
                ("user1", "stefan", "knows", "python"),
            )
        with caplog.at_level(logging.WARNING, logger="istota.knowledge_graph"):
            ensure_table(conn)
        assert any("duplicate" in r.message.lower() for r in caplog.records)


class _ConnProxy:
    """Wraps a sqlite3 connection so a test can intercept execute() calls."""

    def __init__(self, real, on_execute=None):
        self._real = real
        self._on_execute = on_execute

    def execute(self, sql, params=()):
        if self._on_execute is not None:
            self._on_execute(sql, params, self._real)
        return self._real.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._real, name)


class TestAddFactRaceCondition:
    def test_add_fact_returns_none_on_constraint_race(self, conn):
        """If the SELECT dedup passes but the INSERT hits the unique constraint
        (because another writer inserted the same triple in between),
        add_fact must return None rather than raising IntegrityError.
        """
        import istota.memory.knowledge_graph as kg

        inserted = {"done": False}

        def interceptor(sql, params, real):
            if (
                not inserted["done"]
                and sql.strip().upper().startswith("INSERT OR IGNORE INTO KNOWLEDGE_FACTS")
            ):
                real.execute(
                    "INSERT INTO knowledge_facts "
                    "(user_id, subject, predicate, object) VALUES (?, ?, ?, ?)",
                    ("user1", "stefan", "likes", "coffee"),
                )
                inserted["done"] = True

        proxy = _ConnProxy(conn, on_execute=interceptor)
        result = kg.add_fact(proxy, "user1", "stefan", "likes", "coffee")
        assert result is None
        count = conn.execute(
            "SELECT COUNT(*) FROM knowledge_facts WHERE subject='stefan' "
            "AND predicate='likes' AND object='coffee'"
        ).fetchone()[0]
        assert count == 1

    def test_add_fact_still_dedups_via_select_path(self, conn):
        """Normal dedup (no race) still returns None via the SELECT check,
        without relying on the constraint.
        """
        id1 = add_fact(conn, "user1", "stefan", "enjoys", "hiking")
        id2 = add_fact(conn, "user1", "stefan", "enjoys", "hiking")
        assert id1 is not None
        assert id2 is None


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


class TestNormalize:
    def test_lowercase_and_strip(self):
        assert _normalize("  Stefan  ") == "stefan"

    def test_preserves_known_predicates(self):
        """Known predicates pass through unchanged (lowercase only)."""
        for pred in SINGLE_VALUED_PREDICATES:
            assert _normalize(pred) == pred
        for pred in TEMPORARY_PREDICATES:
            assert _normalize(pred) == pred

    def test_freeform_predicates_normalized(self):
        assert _normalize("Allergic_To") == "allergic_to"
        assert _normalize("  ENJOYS  ") == "enjoys"


class TestFactSimilarity:
    def test_identical_strings(self):
        assert _fact_similarity("allergic_to tree nuts", "allergic_to tree nuts") == 1.0

    def test_completely_different(self):
        assert _fact_similarity("allergic_to tree nuts", "lives_in warsaw") == 0.0

    def test_partial_overlap(self):
        sim = _fact_similarity("allergic_to tree_nuts", "allergic_to tree nuts")
        # Words: {allergic_to, tree_nuts} vs {allergic_to, tree, nuts}
        # Intersection: {allergic_to} = 1, Union: 4 → 0.25
        assert 0.0 < sim < 0.5

    def test_empty_string(self):
        assert _fact_similarity("", "something") == 0.0
        assert _fact_similarity("something", "") == 0.0

    def test_high_similarity(self):
        sim = _fact_similarity("works_at acme corp", "works_at acme corporation")
        # 2 of 3 vs 2 of 3 shared words → moderate
        assert sim > 0.4


class TestFuzzyDedup:
    def test_exact_duplicate_still_skipped(self, conn):
        """Existing exact-dedup behavior is preserved."""
        id1 = add_fact(conn, "user1", "stefan", "allergic_to", "tree nuts")
        id2 = add_fact(conn, "user1", "stefan", "allergic_to", "tree nuts")
        assert id1 is not None
        assert id2 is None

    def test_near_duplicate_with_word_variant_inserts(self, conn):
        """`tree nut` and `tree nuts` are different tokens — not deduped."""
        id1 = add_fact(conn, "user1", "stefan", "allergic_to", "tree nuts")
        id2 = add_fact(conn, "user1", "stefan", "allergic_to", "tree nut")
        assert id1 is not None
        # Tokens {tree, nut} vs {tree, nuts} — neither is a subset, Jaccard
        # 1/3 < 0.6 → both insert. Word-boundary precision is the trade-off
        # for the tighter token-subset rule.
        assert id2 is not None

    def test_high_overlap_deduped(self, conn):
        """High word overlap triggers dedup."""
        id1 = add_fact(conn, "user1", "stefan", "allergic_to", "tree nuts and peanuts")
        # Same words, slightly different phrasing
        id2 = add_fact(conn, "user1", "stefan", "allergic_to", "peanuts and tree nuts")
        assert id1 is not None
        assert id2 is None  # Same words, Jaccard=1.0

    def test_different_predicate_object_inserted(self, conn):
        """Completely different predicate+object is inserted normally."""
        id1 = add_fact(conn, "user1", "stefan", "allergic_to", "tree nuts")
        id2 = add_fact(conn, "user1", "stefan", "lives_in", "warsaw")
        assert id1 is not None
        assert id2 is not None

    def test_same_fact_different_subject_inserted(self, conn):
        """Same predicate+object but different subject → not deduped."""
        id1 = add_fact(conn, "user1", "stefan", "allergic_to", "tree nuts")
        id2 = add_fact(conn, "user1", "felix", "allergic_to", "tree nuts")
        assert id1 is not None
        assert id2 is not None

    def test_same_fact_different_user_inserted(self, conn):
        """Same everything but different user_id → not deduped."""
        id1 = add_fact(conn, "user1", "stefan", "allergic_to", "tree nuts")
        id2 = add_fact(conn, "user2", "stefan", "allergic_to", "tree nuts")
        assert id1 is not None
        assert id2 is not None

    def test_predicate_variant_fuzzy_dedup(self, conn):
        """Predicate variants caught by fuzzy dedup (word overlap)."""
        id1 = add_fact(conn, "user1", "stefan", "allergic_to", "tree nuts")
        # "is_allergic_to tree nuts" vs "allergic_to tree nuts"
        # Words: {is_allergic_to, tree, nuts} vs {allergic_to, tree, nuts}
        # Intersection: {tree, nuts} = 2, Union: {is_allergic_to, allergic_to, tree, nuts} = 4
        # Jaccard = 0.5 — below 0.7, so this is NOT caught by fuzzy dedup
        # This is acceptable — the extraction prompt guides consistent naming
        id2 = add_fact(conn, "user1", "stefan", "is_allergic_to", "tree nuts")
        assert id1 is not None
        assert id2 is not None  # Different predicate, low Jaccard

    def test_freeform_predicate_inserted(self, conn):
        """Freeform predicates not in any known set are accepted."""
        fact_id = add_fact(conn, "user1", "stefan", "enjoys", "hiking")
        assert fact_id is not None
        fact = get_fact(conn, fact_id)
        assert fact.predicate == "enjoys"
        assert fact.object == "hiking"

    def test_freeform_predicate_is_multi_valued(self, conn):
        """Unknown predicates don't supersede — they're multi-valued by default."""
        id1 = add_fact(conn, "user1", "stefan", "enjoys", "hiking")
        id2 = add_fact(conn, "user1", "stefan", "enjoys", "cooking")
        fact1 = get_fact(conn, id1)
        fact2 = get_fact(conn, id2)
        assert fact1.valid_until is None  # Both current
        assert fact2.valid_until is None

    def test_fuzzy_dedup_only_checks_current_facts(self, conn):
        """Invalidated facts should not trigger fuzzy dedup."""
        id1 = add_fact(conn, "user1", "stefan", "allergic_to", "tree nuts")
        invalidate_fact(conn, id1, ended="2026-01-01")
        # Re-add same fact — should succeed since the old one is invalidated
        id2 = add_fact(conn, "user1", "stefan", "allergic_to", "tree nuts")
        assert id2 is not None

    def test_substring_object_collapses_refined_value(self, conn):
        """Same predicate, refined object — substring fast-path catches it."""
        id1 = add_fact(conn, "user1", "stefan", "uses_tech", "python")
        id2 = add_fact(conn, "user1", "stefan", "uses_tech", "python 3")
        assert id1 is not None
        assert id2 is None  # "python" ⊂ "python 3" (same predicate)

    def test_substring_object_collapses_added_qualifier(self, conn):
        # Use a multi-valued predicate so substring fires before supersession.
        id1 = add_fact(conn, "user1", "stefan", "owns", "acme")
        id2 = add_fact(conn, "user1", "stefan", "owns", "acme corp")
        assert id1 is not None
        assert id2 is None  # "acme" ⊂ "acme corp" (same predicate)

    def test_morning_vs_weekly_briefings_does_not_merge(self, conn):
        """Spec example: ("morning briefings", "weekly briefings") — must NOT merge.

        Tokens (incl. predicate): {prefers, morning, briefings} vs
        {prefers, weekly, briefings}. Jaccard = 2/4 = 0.5 < 0.6.
        """
        id1 = add_fact(conn, "user1", "stefan", "prefers", "morning briefings")
        id2 = add_fact(conn, "user1", "stefan", "prefers", "weekly briefings")
        assert id1 is not None
        assert id2 is not None

    def test_substring_does_not_merge_unrelated(self, conn):
        """Genuinely different objects don't merge."""
        id1 = add_fact(conn, "user1", "stefan", "prefers", "morning briefings")
        id2 = add_fact(conn, "user1", "stefan", "prefers", "evening summaries")
        assert id1 is not None
        # No shared content tokens; Jaccard 1/4 = 0.25 < 0.6 → inserted.
        assert id2 is not None

    def test_substring_scoped_to_predicate(self, conn):
        """Different predicates must not substring-merge even if objects share text."""
        id1 = add_fact(conn, "user1", "stefan", "allergic_to", "tree nuts")
        # Different predicate ("is_allergic_to" was the spec's example): the
        # substring path is scoped to identical predicates, so this is
        # judged by Jaccard alone.
        id2 = add_fact(conn, "user1", "stefan", "is_allergic_to", "tree nuts")
        assert id1 is not None
        assert id2 is not None


class TestTokenize:
    def test_basic_words(self):
        assert _tokenize("Hello World") == {"hello", "world"}

    def test_underscored_words(self):
        assert _tokenize("project_alpha is active") == {"project_alpha", "is", "active"}

    def test_punctuation_stripped(self):
        tokens = _tokenize("What's happening with project_alpha?")
        assert "project_alpha" in tokens
        assert "what" in tokens
        assert "s" in tokens

    def test_mixed_case(self):
        assert _tokenize("Stefan WORKS at Acme") == {"stefan", "works", "at", "acme"}

    def test_empty_string(self):
        assert _tokenize("") == set()


class TestSelectRelevantFacts:
    def _make_fact(self, subject, predicate, obj, **kwargs):
        defaults = dict(
            id=0, user_id="user1", valid_from=None, valid_until=None,
            temporary=False, confidence=1.0, source_task_id=None,
            source_type="extracted", created_at="2026-04-01T00:00:00",
            updated_at="2026-04-01T00:00:00",
        )
        defaults.update(kwargs)
        return KnowledgeFact(
            subject=subject, predicate=predicate, object=obj, **defaults,
        )

    def test_identity_facts_always_included(self):
        facts = [
            self._make_fact("stefan", "works_at", "acme"),
            self._make_fact("stefan", "lives_in", "warsaw"),
            self._make_fact("project_alpha", "has_status", "active"),
        ]
        result = select_relevant_facts(facts, "unrelated prompt", "stefan")
        assert len(result) == 2
        assert all(f.subject == "stefan" for f in result)

    def test_subject_match_in_prompt(self):
        facts = [
            self._make_fact("project_alpha", "has_status", "active"),
            self._make_fact("project_beta", "has_status", "paused"),
        ]
        result = select_relevant_facts(
            facts, "How is project_alpha going?", "stefan",
        )
        assert len(result) == 1
        assert result[0].subject == "project_alpha"

    def test_object_match_in_prompt(self):
        facts = [
            self._make_fact("project_alpha", "uses_tech", "svelte"),
            self._make_fact("project_beta", "uses_tech", "react"),
        ]
        result = select_relevant_facts(
            facts, "Tell me about our svelte projects", "stefan",
        )
        assert len(result) == 1
        assert result[0].object == "svelte"

    def test_no_match_excluded(self):
        facts = [
            self._make_fact("project_gamma", "has_status", "archived"),
        ]
        result = select_relevant_facts(
            facts, "What's for lunch?", "stefan",
        )
        assert len(result) == 0

    def test_identity_plus_matched(self):
        facts = [
            self._make_fact("stefan", "works_at", "acme"),
            self._make_fact("acme", "has_status", "growing"),
            self._make_fact("project_beta", "has_status", "paused"),
        ]
        result = select_relevant_facts(
            facts, "Tell me about acme", "stefan",
        )
        # stefan (identity) + acme (subject match)
        assert len(result) == 2
        subjects = {f.subject for f in result}
        assert subjects == {"stefan", "acme"}

    def test_empty_facts(self):
        result = select_relevant_facts([], "some prompt", "stefan")
        assert result == []

    def test_max_facts_does_not_truncate_identity(self):
        """Identity facts are anchors — never truncated, even when over cap.
        Matched facts (subject/object in prompt) get cut instead.
        """
        facts = [
            self._make_fact("stefan", "knows", "python", created_at="2026-01-01"),
            self._make_fact("stefan", "knows", "go", created_at="2026-02-01"),
            self._make_fact("stefan", "works_at", "acme", created_at="2026-03-01"),
        ]
        result = select_relevant_facts(
            facts, "anything", "stefan", max_facts=2,
        )
        # All 3 identity facts kept — cap is a soft target when identity exceeds it.
        assert len(result) == 3
        assert all(f.subject == "stefan" for f in result)

    def test_max_facts_prioritizes_identity_over_matched(self):
        facts = [
            self._make_fact("stefan", "works_at", "acme"),
            self._make_fact("stefan", "knows", "python"),
            self._make_fact("project_alpha", "has_status", "active"),
        ]
        result = select_relevant_facts(
            facts, "project_alpha update", "stefan", max_facts=2,
        )
        # 2 identity facts saturate the cap; project_alpha is dropped.
        assert len(result) == 2
        assert all(f.subject == "stefan" for f in result)

    def test_max_facts_zero_means_unlimited(self):
        facts = [
            self._make_fact("stefan", "knows", f"lang{i}")
            for i in range(20)
        ]
        result = select_relevant_facts(
            facts, "anything", "stefan", max_facts=0,
        )
        assert len(result) == 20

    def test_case_insensitive_matching(self):
        facts = [
            self._make_fact("project_alpha", "has_status", "active"),
        ]
        result = select_relevant_facts(
            facts, "What about Project_Alpha?", "stefan",
        )
        assert len(result) == 1

    def test_multi_word_entity_partial_match(self):
        """A multi-word subject matches if any token appears in prompt."""
        facts = [
            self._make_fact("alice smith", "works_at", "acme"),
        ]
        result = select_relevant_facts(
            facts, "Send alice the report", "stefan",
        )
        assert len(result) == 1

    def test_user_id_match_is_case_insensitive(self):
        facts = [
            self._make_fact("stefan", "works_at", "acme"),
        ]
        result = select_relevant_facts(
            facts, "unrelated", "Stefan",
        )
        assert len(result) == 1

    def test_max_facts_matched_sorted_by_recency(self):
        facts = [
            self._make_fact("stefan", "works_at", "acme"),  # identity
            self._make_fact("project_old", "uses_tech", "python",
                           created_at="2025-01-01"),
            self._make_fact("project_new", "uses_tech", "python",
                           created_at="2026-04-01"),
        ]
        result = select_relevant_facts(
            facts, "python projects", "stefan", max_facts=2,
        )
        # 1 identity + 1 matched (most recent)
        assert len(result) == 2
        non_identity = [f for f in result if f.subject != "stefan"]
        assert len(non_identity) == 1
        assert non_identity[0].subject == "project_new"


class TestKnowledgeFactsAudit:
    def test_insert_writes_audit_row(self, conn):
        fact_id = add_fact(conn, "alice", "stefan", "uses_tech", "python")
        rows = get_fact_history(conn, "alice")
        assert len(rows) == 1
        assert rows[0].op == "insert"
        assert rows[0].fact_id == fact_id
        assert rows[0].before_json is None
        assert rows[0].after_json is not None
        assert "python" in rows[0].after_json

    def test_supersede_writes_audit_row(self, conn):
        a = add_fact(conn, "alice", "stefan", "works_at", "acme")
        b = add_fact(conn, "alice", "stefan", "works_at", "globex")
        ops = [r.op for r in get_fact_history(conn, "alice")]
        # Two inserts + one supersede on the prior row.
        assert ops.count("insert") == 2
        assert ops.count("supersede") == 1
        # Supersede row points at the old fact.
        sup = next(r for r in get_fact_history(conn, "alice") if r.op == "supersede")
        assert sup.fact_id == a
        assert sup.before_json is not None
        assert "acme" in sup.before_json

    def test_fuzzy_dedup_skip_writes_audit_row(self, conn):
        # 3-of-4 token overlap → Jaccard 0.75 ≥ 0.7 threshold. Use a
        # multi-valued predicate so fuzzy fires before supersession.
        first = add_fact(conn, "alice", "stefan", "uses_tech", "python java")
        second = add_fact(conn, "alice", "stefan", "uses_tech", "python java rust")
        # First inserted, second skipped via fuzzy dedup.
        assert first is not None
        assert second is None
        ops = [r.op for r in get_fact_history(conn, "alice")]
        assert "fuzzy_dedup_skip" in ops
        skip = next(r for r in get_fact_history(conn, "alice") if r.op == "fuzzy_dedup_skip")
        # Audit row points at the existing fact it collided with.
        assert skip.fact_id == first

    def test_invalidate_writes_audit_row(self, conn):
        fact_id = add_fact(conn, "alice", "stefan", "uses_tech", "python")
        invalidate_fact(conn, fact_id, ended="2026-04-01")
        ops = [r.op for r in get_fact_history(conn, "alice")]
        assert "invalidate" in ops

    def test_delete_writes_audit_row(self, conn):
        fact_id = add_fact(conn, "alice", "stefan", "uses_tech", "python")
        delete_fact(conn, fact_id)
        ops = [r.op for r in get_fact_history(conn, "alice")]
        assert "delete" in ops

    def test_history_filter_by_entity(self, conn):
        add_fact(conn, "alice", "stefan", "uses_tech", "python")
        add_fact(conn, "alice", "felix", "lives_in", "warsaw")
        rows = get_fact_history(conn, "alice", entity="felix")
        assert len(rows) == 1
        assert "felix" in (rows[0].after_json or "")

    def test_cleanup_old_audit_rows_skips_when_zero(self, conn):
        add_fact(conn, "alice", "stefan", "uses_tech", "python")
        n = cleanup_old_audit_rows(conn, "alice", 0)
        assert n == 0
        # Row still there.
        assert len(get_fact_history(conn, "alice")) == 1

    def test_cleanup_old_audit_rows_deletes_old(self, conn):
        # Insert audit row directly with a synthetic ts in the far past.
        ensure_table(conn)
        conn.execute(
            "INSERT INTO knowledge_facts_audit "
            "(user_id, fact_id, op, before_json, after_json, ts) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            ("alice", 999, "insert", None, "{}", "2020-01-01 00:00:00"),
        )
        conn.commit()
        n = cleanup_old_audit_rows(conn, "alice", retention_days=1)
        assert n == 1
