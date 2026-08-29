"""Tests for the memory search core module."""

import json
import os
import sqlite3
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from types import SimpleNamespace

from istota.memory.search import (
    EPHEMERAL_SOURCE_TYPES,
    SearchResult,
    _apply_recency_decay,
    _content_hash,
    _escape_fts5_query,
    _insert_chunks,
    _delete_source_chunks,
    _rrf_fusion,
    _serialize_embedding,
    _VEC_MAX_K,
    chunk_text,
    embed_batch,
    embed_text,
    get_stats,
    index_conversation,
    index_file,
    reindex_all,
    search,
    _search_bm25,
    _search_vec,
    cleanup_old_chunks,
)


def _init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize a test database with the memory_chunks schema."""
    schema_path = Path(__file__).parent.parent / "schema.sql"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.executescript(schema_path.read_text())
    return conn


class TestChunking:
    def test_empty_text(self):
        assert chunk_text("") == []
        assert chunk_text("   ") == []

    def test_short_text_single_chunk(self):
        text = "Hello world, this is a short text."
        chunks = chunk_text(text)
        assert len(chunks) == 1
        assert chunks[0] == text.strip()

    def test_long_text_multiple_chunks(self):
        # Create text with many paragraphs
        paragraphs = [f"Paragraph {i}. " + "word " * 100 for i in range(10)]
        text = "\n\n".join(paragraphs)
        chunks = chunk_text(text, max_tokens=200, overlap_tokens=20)
        assert len(chunks) > 1
        # Each chunk should not exceed approximate max words
        max_words = int(200 * 0.75)
        for chunk in chunks:
            # Allow some slack since paragraph splitting isn't exact
            assert len(chunk.split()) <= max_words + 50

    def test_paragraph_boundaries(self):
        text = "First paragraph here.\n\nSecond paragraph here.\n\nThird paragraph here."
        chunks = chunk_text(text, max_tokens=1000)
        assert len(chunks) == 1  # All fits in one chunk

    def test_sentence_splitting_for_long_paragraphs(self):
        # Single paragraph with many sentences
        sentences = [f"Sentence number {i} with some words." for i in range(50)]
        text = " ".join(sentences)
        chunks = chunk_text(text, max_tokens=100, overlap_tokens=10)
        assert len(chunks) > 1

    def test_overlap_words_present(self):
        # Create text that forces multiple chunks
        words = [f"word{i}" for i in range(200)]
        text = " ".join(words)
        chunks = chunk_text(text, max_tokens=100, overlap_tokens=20)
        assert len(chunks) > 1
        # Some overlap should exist between consecutive chunks
        if len(chunks) >= 2:
            words_1 = set(chunks[0].split()[-15:])
            words_2 = set(chunks[1].split()[:20])
            overlap = words_1 & words_2
            assert len(overlap) > 0


class TestContentHash:
    def test_deterministic(self):
        assert _content_hash("hello") == _content_hash("hello")

    def test_different_texts(self):
        assert _content_hash("hello") != _content_hash("world")


class TestEscapeFTS5Query:
    def test_simple_terms(self):
        assert _escape_fts5_query("hello world") == '"hello" "world"'

    def test_fts5_operators_escaped(self):
        escaped = _escape_fts5_query("NOT AND OR NEAR")
        assert '"NOT"' in escaped
        assert '"AND"' in escaped

    def test_empty_query(self):
        assert _escape_fts5_query("") == '""'

    def test_single_term(self):
        assert _escape_fts5_query("hello") == '"hello"'

    def test_prefix_appends_star(self):
        assert _escape_fts5_query("falcon timeline", prefix=True) == '"falcon"* "timeline"*'

    def test_or_mode_joins_with_or(self):
        assert _escape_fts5_query("falcon timeline", match_mode="or") == '"falcon" OR "timeline"'

    def test_prefix_and_or_combined(self):
        assert _escape_fts5_query("a b", prefix=True, match_mode="or") == '"a"* OR "b"*'

    def test_prefix_empty_query(self):
        assert _escape_fts5_query("", prefix=True) == '""'


class TestSerializeEmbedding:
    def test_roundtrip(self):
        import struct
        embedding = [0.1, 0.2, 0.3]
        serialized = _serialize_embedding(embedding)
        assert len(serialized) == 3 * 4  # 3 floats * 4 bytes
        unpacked = struct.unpack("3f", serialized)
        assert abs(unpacked[0] - 0.1) < 1e-6
        assert abs(unpacked[1] - 0.2) < 1e-6
        assert abs(unpacked[2] - 0.3) < 1e-6


class TestEmbedding:
    @patch("istota.memory.search._get_model")
    def test_embed_text_returns_none_when_no_model(self, mock_model):
        mock_model.return_value = None
        assert embed_text("hello") is None

    @patch("istota.memory.search._get_model")
    def test_embed_text_with_model(self, mock_model):
        np = pytest.importorskip("numpy")
        mock = MagicMock()
        mock.encode.return_value = np.array([0.1, 0.2, 0.3])
        mock_model.return_value = mock
        result = embed_text("hello")
        assert result is not None
        assert len(result) == 3
        mock.encode.assert_called_once_with("hello", normalize_embeddings=True)

    @patch("istota.memory.search._get_model")
    def test_embed_batch_returns_none_when_no_model(self, mock_model):
        mock_model.return_value = None
        assert embed_batch(["hello", "world"]) is None

    @patch("istota.memory.search._get_model")
    def test_embed_batch_empty_list(self, mock_model):
        assert embed_batch([]) == []

    @patch("istota.memory.search._get_model")
    def test_embed_batch_with_model(self, mock_model):
        np = pytest.importorskip("numpy")
        mock = MagicMock()
        mock.encode.return_value = np.array([[0.1, 0.2], [0.3, 0.4]])
        mock_model.return_value = mock
        result = embed_batch(["hello", "world"])
        assert result is not None
        assert len(result) == 2


class TestModelLoadIsSerialized:
    """ISSUE-273.

    `WorkerPool` workers are threads in the daemon, so several tasks can finish
    memory-indexing in the same second. With no lock they all saw `_model is
    None`, all built a `SentenceTransformer`, and the last assignment won — the
    journal showed three `BertModel LOAD REPORT` blocks within two seconds, and
    80 MB resident against 34 MB for a single load, 46 MB of which survived gc
    and `malloc_trim`. The orphaned copies are unreachable, so nothing ever
    frees them.
    """

    @pytest.fixture(autouse=True)
    def _reset_singleton(self):
        from istota.memory import search as search_mod

        before = search_mod._model
        search_mod._model = None
        yield search_mod
        search_mod._model = None if before is None else before

    def _fake_sentence_transformers(self, loads):
        """A stand-in module whose constructor is slow enough to overlap.

        Injected rather than patched so the test never imports torch — the
        280 MB the real import costs is the thing under discussion.
        """
        import types

        module = types.ModuleType("sentence_transformers")

        class SentenceTransformer:
            def __init__(self, name):
                loads.append(name)
                # Every thread arrives here only if the lock let it through.
                # Waiting widens the window a second caller would need.
                time.sleep(0.05)
                self.name = name

        module.SentenceTransformer = SentenceTransformer
        return module

    def test_concurrent_callers_construct_the_model_once(self, _reset_singleton):
        search_mod = _reset_singleton
        loads = []
        fake = self._fake_sentence_transformers(loads)

        start = threading.Barrier(8)
        results = []

        def worker():
            start.wait(timeout=10)
            results.append(search_mod._get_model())

        with patch.dict(sys.modules, {"sentence_transformers": fake}):
            threads = [threading.Thread(target=worker) for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

        assert loads == ["all-MiniLM-L6-v2"], f"model built {len(loads)} times"
        assert len(results) == 8
        assert all(r is results[0] for r in results), "callers got different model objects"

    def test_a_failed_load_does_not_wedge_later_callers(self, _reset_singleton):
        """The lock must not turn a transient import failure into a permanent
        one — `_get_model` has always retried, and it still does."""
        search_mod = _reset_singleton

        broken = MagicMock()
        broken.SentenceTransformer.side_effect = RuntimeError("no weights")
        with patch.dict(sys.modules, {"sentence_transformers": broken}):
            assert search_mod._get_model() is None

        loads = []
        fake = self._fake_sentence_transformers(loads)
        with patch.dict(sys.modules, {"sentence_transformers": fake}):
            assert search_mod._get_model() is not None
        assert loads == ["all-MiniLM-L6-v2"]


class TestInsertAndSearch:
    """Tests using real SQLite with FTS5 (BM25 search)."""

    def test_insert_and_bm25_search(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["Hello world from Alice"], {"task_id": "1"})
            _insert_chunks(conn, "alice", "conversation", "2", ["Python programming is fun"], {"task_id": "2"})

        results = _search_bm25(conn, "alice", "Python programming", 10)
        assert len(results) > 0
        assert "Python" in results[0].content
        conn.close()

    def test_dedup_by_content_hash(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            n1 = _insert_chunks(conn, "alice", "conversation", "1", ["Hello world"], None)
            n2 = _insert_chunks(conn, "alice", "conversation", "2", ["Hello world"], None)  # same content

        assert n1 == 1
        assert n2 == 0  # dedup
        row = conn.execute("SELECT COUNT(*) FROM memory_chunks WHERE user_id = 'alice'").fetchone()
        assert row[0] == 1
        conn.close()

    def test_user_isolation(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["Alice secret data"], None)
            _insert_chunks(conn, "bob", "conversation", "2", ["Bob private info"], None)

        alice_results = _search_bm25(conn, "alice", "secret data", 10)
        bob_results = _search_bm25(conn, "bob", "secret data", 10)

        assert len(alice_results) == 1
        assert len(bob_results) == 0
        conn.close()

    def test_source_type_filter(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["Machine learning topic"], None)
            _insert_chunks(conn, "alice", "memory_file", "/mem.md", ["Machine learning notes"], None)

        conv_only = _search_bm25(conn, "alice", "machine learning", 10, source_types=["conversation"])
        assert len(conv_only) == 1
        assert conv_only[0].source_type == "conversation"
        conn.close()

    def test_delete_source_chunks(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False), \
             patch("istota.memory.search.enable_vec_extension", return_value=False):
            _insert_chunks(conn, "alice", "memory_file", "/f.md", ["Chunk one", "Chunk two"], None)

        count = conn.execute("SELECT COUNT(*) FROM memory_chunks WHERE user_id = 'alice'").fetchone()[0]
        assert count == 2

        with patch("istota.memory.search.enable_vec_extension", return_value=False):
            deleted = _delete_source_chunks(conn, "alice", "memory_file", "/f.md")
        assert deleted == 2

        count = conn.execute("SELECT COUNT(*) FROM memory_chunks WHERE user_id = 'alice'").fetchone()[0]
        assert count == 0
        conn.close()


class TestSearchForgiveness:
    """Prefix matching + gated AND→OR fallback (interactive-search forgiveness)."""

    def _no_vec(self):
        # Force BM25-only so the FTS behaviour is what's under test.
        return patch("istota.memory.search.enable_vec_extension", return_value=False)

    def test_prefix_matches_longer_word(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["the falcons are migrating"], {"task_id": "1"})

        with self._no_vec():
            # Bare term (no prefix) misses the plural.
            strict = search(conn, "alice", "falcon")
            assert strict == []
            # Prefix query matches "falcons".
            loose = search(conn, "alice", "falcon", prefix=True)
        assert len(loose) == 1
        conn.close()

    def test_or_fallback_returns_partial_matches(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["falcon nesting habits"], {"task_id": "1"})

        with self._no_vec():
            # Only "falcon" matches; "helicopter" doesn't. Strict AND → zero.
            strict = search(conn, "alice", "falcon helicopter", allow_or_fallback=False)
            assert strict == []
            # OR fallback recovers the partial match.
            loose = search(conn, "alice", "falcon helicopter", allow_or_fallback=True)
        assert len(loose) == 1
        conn.close()

    def test_or_fallback_off_by_default(self, tmp_path):
        """The recall path default (no fallback) keeps precision — a partial
        multi-term query returns nothing rather than flooding recall."""
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["falcon nesting habits"], {"task_id": "1"})

        with self._no_vec():
            default = search(conn, "alice", "falcon helicopter")
        assert default == []
        conn.close()

    def test_strict_and_still_ands_when_both_present(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["falcon and heron nesting"], {"task_id": "1"})
            _insert_chunks(conn, "alice", "conversation", "2", ["only a falcon here"], {"task_id": "2"})

        with self._no_vec():
            # Strict AND: both terms must appear — only chunk 1 qualifies.
            strict = search(conn, "alice", "falcon heron")
        assert len(strict) == 1
        assert "heron" in strict[0].content
        conn.close()


class TestIndexConversation:
    def test_basic_indexing(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            n = index_conversation(conn, "alice", 42, "What is Python?", "Python is a programming language.")

        assert n > 0
        results = _search_bm25(conn, "alice", "Python programming", 10)
        assert len(results) > 0
        conn.close()

    def test_empty_prompt_and_result(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            n = index_conversation(conn, "alice", 99, "", "")
        assert n == 0
        conn.close()


class TestIndexFile:
    def test_file_indexing_replaces_existing(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False), \
             patch("istota.memory.search.enable_vec_extension", return_value=False):
            index_file(conn, "alice", "/mem.md", "Original content about cats")
            index_file(conn, "alice", "/mem.md", "Replacement content about dogs")

        results = _search_bm25(conn, "alice", "cats", 10)
        assert len(results) == 0  # old content gone

        results = _search_bm25(conn, "alice", "dogs", 10)
        assert len(results) > 0  # new content present
        conn.close()


class TestRRFFusion:
    def test_fusion_basic(self):
        bm25 = [
            SearchResult(chunk_id=1, content="a", score=-1.0, source_type="c", source_id="1"),
            SearchResult(chunk_id=2, content="b", score=-2.0, source_type="c", source_id="2"),
            SearchResult(chunk_id=3, content="c", score=-3.0, source_type="c", source_id="3"),
        ]
        vec = [
            SearchResult(chunk_id=2, content="b", score=0.9, source_type="c", source_id="2"),
            SearchResult(chunk_id=4, content="d", score=0.8, source_type="c", source_id="4"),
            SearchResult(chunk_id=1, content="a", score=0.7, source_type="c", source_id="1"),
        ]

        fused = _rrf_fusion(bm25, vec, k=60)
        # chunk_id 2 appears at rank 2 in bm25 and rank 1 in vec => highest combined
        # chunk_id 1 appears at rank 1 in bm25 and rank 3 in vec
        ids = [r.chunk_id for r in fused]
        assert 1 in ids
        assert 2 in ids
        assert 3 in ids
        assert 4 in ids

    def test_fusion_with_no_overlap(self):
        bm25 = [SearchResult(chunk_id=1, content="a", score=-1.0, source_type="c", source_id="1")]
        vec = [SearchResult(chunk_id=2, content="b", score=0.9, source_type="c", source_id="2")]

        fused = _rrf_fusion(bm25, vec)
        assert len(fused) == 2
        ids = [r.chunk_id for r in fused]
        assert 1 in ids
        assert 2 in ids

    def test_fusion_empty_inputs(self):
        assert _rrf_fusion([], []) == []

    def test_bm25_only_gets_ranks(self):
        bm25 = [
            SearchResult(chunk_id=1, content="a", score=-1.0, source_type="c", source_id="1"),
        ]
        fused = _rrf_fusion(bm25, [])
        assert len(fused) == 1
        assert fused[0].bm25_rank == 1
        assert fused[0].vec_rank is None


class TestSearch:
    def test_bm25_only_fallback(self, tmp_path):
        """When vec search returns empty, falls back to BM25-only."""
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["Quantum computing research"], None)

        with patch("istota.memory.search._search_vec", return_value=[]):
            results = search(conn, "alice", "quantum computing", limit=5)

        assert len(results) > 0
        assert results[0].bm25_rank == 1
        conn.close()

    def test_hybrid_search_with_mock_vec(self, tmp_path):
        """When vec results are available, RRF fusion is used."""
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["Neural network training"], None)

        # Get the chunk_id that was inserted
        row = conn.execute("SELECT id FROM memory_chunks LIMIT 1").fetchone()
        chunk_id = row[0]

        mock_vec_result = SearchResult(
            chunk_id=chunk_id, content="Neural network training",
            score=0.95, source_type="conversation", source_id="1",
        )
        with patch("istota.memory.search._search_vec", return_value=[mock_vec_result]):
            results = search(conn, "alice", "neural network", limit=5)

        assert len(results) > 0
        conn.close()

    def test_exclude_conversation_task_ids_drops_matching(self, tmp_path):
        """Conversation chunks already in context are filtered out of recall."""
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "100", ["Recurrent neural networks"], None)
            _insert_chunks(conn, "alice", "conversation", "101", ["Convolutional neural networks"], None)

        with patch("istota.memory.search._search_vec", return_value=[]):
            results = search(
                conn, "alice", "neural networks", limit=5,
                exclude_conversation_task_ids={100},
            )

        assert all(r.source_id != "100" for r in results)
        assert any(r.source_id == "101" for r in results)
        conn.close()

    def test_exclude_conversation_task_ids_keeps_other_source_types(self, tmp_path):
        """Exclude only applies to conversation chunks, not memory_file."""
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "100", ["Quantum entanglement"], None)
            _insert_chunks(conn, "alice", "memory_file", "100", ["Quantum entanglement notes"], None)

        with patch("istota.memory.search._search_vec", return_value=[]):
            results = search(
                conn, "alice", "quantum entanglement", limit=5,
                exclude_conversation_task_ids={100},
            )

        # The memory_file chunk with source_id "100" should still appear.
        kinds = {(r.source_type, r.source_id) for r in results}
        assert ("memory_file", "100") in kinds
        assert ("conversation", "100") not in kinds
        conn.close()


class TestGetStats:
    def test_stats_with_data(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["chunk one"], None)
            _insert_chunks(conn, "alice", "conversation", "2", ["chunk two"], None)
            _insert_chunks(conn, "alice", "memory_file", "/f.md", ["chunk three"], None)

        with patch("istota.memory.search.enable_vec_extension", return_value=False):
            stats = get_stats(conn, "alice")

        assert stats["total_chunks"] == 3
        assert stats["by_source_type"]["conversation"] == 2
        assert stats["by_source_type"]["memory_file"] == 1
        assert stats["user_id"] == "alice"
        conn.close()

    def test_stats_empty(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.enable_vec_extension", return_value=False):
            stats = get_stats(conn, "alice")
        assert stats["total_chunks"] == 0
        conn.close()


class TestReindexAll:
    def test_reindex_conversations(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")

        # Insert a completed task directly
        conn.execute(
            "INSERT INTO tasks (user_id, source_type, prompt, result, status, created_at) "
            "VALUES (?, ?, ?, ?, 'completed', datetime('now'))",
            ("alice", "talk", "What is AI?", "AI is artificial intelligence."),
        )
        conn.commit()

        config = MagicMock()
        config.nextcloud_mount_path = None

        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            stats = reindex_all(conn, config, "alice", lookback_days=1)

        assert stats["conversations"] >= 1
        assert stats["chunks"] >= 1
        conn.close()

    def test_reindex_memory_files(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")

        # Create mock memory files
        memories_dir = tmp_path / "mount" / "Users" / "alice" / "memories"
        memories_dir.mkdir(parents=True)
        (memories_dir / "2026-02-01.md").write_text("Learned about Python decorators today.")

        config = MagicMock()
        config.nextcloud_mount_path = tmp_path / "mount"

        with patch("istota.memory.search.ensure_vec_table", return_value=False), \
             patch("istota.memory.search.enable_vec_extension", return_value=False):
            stats = reindex_all(conn, config, "alice", lookback_days=1)

        assert stats["memory_files"] >= 1
        conn.close()

    def test_reindex_channel_memory_files(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")

        # Create channel memory files
        channel_memories = tmp_path / "mount" / "Channels" / "room123" / "memories"
        channel_memories.mkdir(parents=True)
        (channel_memories / "2026-02-07.md").write_text("- Decided to use GraphQL (alice)")

        config = MagicMock()
        config.nextcloud_mount_path = tmp_path / "mount"

        with patch("istota.memory.search.ensure_vec_table", return_value=False), \
             patch("istota.memory.search.enable_vec_extension", return_value=False):
            stats = reindex_all(conn, config, "alice", lookback_days=1)

        assert stats.get("channel_memories", 0) >= 1
        conn.close()


class TestIncludeUserIds:
    """Tests for multi-user search support (include_user_ids parameter)."""

    def test_search_bm25_includes_channel(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["user data"], None)
            _insert_chunks(conn, "channel:room123", "channel_memory", "f1", ["channel decision"], None)

        with patch("istota.memory.search._search_vec", return_value=[]):
            results = search(
                conn, "alice", "decision",
                limit=5, include_user_ids=["channel:room123"],
            )

        contents = [r.content for r in results]
        assert "channel decision" in contents
        conn.close()

    def test_search_bm25_without_include_user_ids(self, tmp_path):
        """Without include_user_ids, only user's own chunks are returned."""
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["user data"], None)
            _insert_chunks(conn, "channel:room123", "channel_memory", "f1", ["channel decision"], None)

        with patch("istota.memory.search._search_vec", return_value=[]):
            results = search(conn, "alice", "decision", limit=5)

        contents = [r.content for r in results]
        assert "channel decision" not in contents
        conn.close()

    def test_stats_includes_channel(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["user chunk"], None)
            _insert_chunks(conn, "channel:room123", "channel_memory", "f1", ["channel chunk"], None)

        with patch("istota.memory.search.enable_vec_extension", return_value=False):
            stats = get_stats(conn, "alice", include_user_ids=["channel:room123"])

        assert stats["total_chunks"] == 2
        assert stats["by_source_type"].get("channel_memory") == 1
        conn.close()

    def test_stats_without_include_user_ids(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["user chunk"], None)
            _insert_chunks(conn, "channel:room123", "channel_memory", "f1", ["channel chunk"], None)

        with patch("istota.memory.search.enable_vec_extension", return_value=False):
            stats = get_stats(conn, "alice")

        assert stats["total_chunks"] == 1
        conn.close()


class TestChunkMetadata:
    """Tests for topic and entities metadata on chunks."""

    def test_insert_with_topic(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1",
                          ["Python programming"], None, topic="tech")

        row = conn.execute("SELECT topic FROM memory_chunks WHERE user_id = 'alice'").fetchone()
        assert row[0] == "tech"
        conn.close()

    def test_insert_with_entities(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1",
                          ["Carol uses Python"], None, entities=["carol", "python"])

        row = conn.execute("SELECT entities FROM memory_chunks WHERE user_id = 'alice'").fetchone()
        assert json.loads(row[0]) == ["carol", "python"]
        conn.close()

    def test_insert_without_metadata_defaults_null(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["plain chunk"], None)

        row = conn.execute("SELECT topic, entities FROM memory_chunks WHERE user_id = 'alice'").fetchone()
        assert row[0] is None
        assert row[1] is None
        conn.close()

    def test_index_conversation_with_metadata(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            n = index_conversation(conn, "alice", 1, "What is FastAPI?", "A web framework",
                                  topic="tech", entities=["fastapi"])

        assert n >= 1
        row = conn.execute("SELECT topic, entities FROM memory_chunks WHERE user_id = 'alice'").fetchone()
        assert row[0] == "tech"
        conn.close()

    def test_index_file_with_metadata(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False), \
             patch("istota.memory.search.enable_vec_extension", return_value=False):
            n = index_file(conn, "alice", "/path/mem.md", "Some content",
                          topic="personal", entities=["carol"])

        assert n >= 1
        row = conn.execute("SELECT topic FROM memory_chunks WHERE user_id = 'alice'").fetchone()
        assert row[0] == "personal"
        conn.close()


class TestFilteredSearch:
    """Tests for topic and entity filtering in search."""

    def test_search_filter_by_topic(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1",
                          ["Python web framework discussion"], None, topic="tech")
            _insert_chunks(conn, "alice", "conversation", "2",
                          ["Python for data analysis at work"], None, topic="work")

        with patch("istota.memory.search._search_vec", return_value=[]):
            results = search(conn, "alice", "Python", topics=["tech"])

        # Should get tech chunk + not the work chunk (unless it has NULL topic)
        topics = [r.source_id for r in results]
        assert "1" in topics
        assert "2" not in topics
        conn.close()

    def test_search_topic_filter_includes_null(self, tmp_path):
        """Chunks with NULL topic are always included in filtered searches."""
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1",
                          ["Python programming guide"], None, topic="tech")
            _insert_chunks(conn, "alice", "conversation", "2",
                          ["Python legacy chunk without topic"], None)  # NULL topic

        with patch("istota.memory.search._search_vec", return_value=[]):
            results = search(conn, "alice", "Python", topics=["tech"])

        source_ids = [r.source_id for r in results]
        assert "1" in source_ids  # tech topic matches
        assert "2" in source_ids  # NULL topic included
        conn.close()

    def test_search_filter_by_entity(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1",
                          ["Carol works on Istota project"], None,
                          entities=["carol", "istota"])
            _insert_chunks(conn, "alice", "conversation", "2",
                          ["Alice works on Hermes project"], None,
                          entities=["alice", "hermes"])

        with patch("istota.memory.search._search_vec", return_value=[]):
            results = search(conn, "alice", "works on", entities=["carol"])

        source_ids = [r.source_id for r in results]
        assert "1" in source_ids
        assert "2" not in source_ids
        conn.close()

    def test_search_no_filter_returns_all(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1",
                          ["tech stuff"], None, topic="tech")
            _insert_chunks(conn, "alice", "conversation", "2",
                          ["work stuff"], None, topic="work")

        with patch("istota.memory.search._search_vec", return_value=[]):
            results = search(conn, "alice", "stuff")

        assert len(results) == 2
        conn.close()

    def test_bm25_topic_filter(self, tmp_path):
        """Direct test of _search_bm25 with topic filter."""
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1",
                          ["Machine learning project"], None, topic="tech")
            _insert_chunks(conn, "alice", "conversation", "2",
                          ["Machine learning salary discussion"], None, topic="finance")

        results = _search_bm25(conn, "alice", "machine learning", 10, topics=["tech"])

        source_ids = [r.source_id for r in results]
        assert "1" in source_ids
        assert "2" not in source_ids
        conn.close()


class TestVecAdaptiveK:
    """Tests for ISSUE-041 — adaptive KNN k in _search_vec."""

    def _make_row(self, chunk_id, distance=0.1):
        # Matches the column order returned by _search_vec's SELECT
        # (..., mc.metadata_json, mc.created_at).
        return (chunk_id, distance, f"content {chunk_id}", "conversation",
                str(chunk_id), None, "2026-01-01")

    def _capture_execute(self, batches):
        """Build a fake conn.execute that yields one batch per call and records k."""
        call_ks: list[int] = []
        idx = {"i": 0}

        def fake_execute(sql, params):
            # k is the 2nd positional param (after serialized embedding).
            call_ks.append(params[1])
            i = idx["i"]
            idx["i"] = i + 1
            return iter(batches[i] if i < len(batches) else [])

        conn = MagicMock()
        conn.execute.side_effect = fake_execute
        return conn, call_ks

    def test_starts_wider_with_filters(self):
        """When any post-filter is active, initial k is limit*10 (vs limit*5 without).

        The episode-window filter (ISSUE-109 #2) is itself a post-filter and is
        on by default, so the no-filter baseline opts out via include_expired."""
        conn_no, ks_no = self._capture_execute([[self._make_row(i) for i in range(10)]])
        conn_filt, ks_filt = self._capture_execute([[self._make_row(i) for i in range(10)]])

        with patch("istota.memory.search.enable_vec_extension", return_value=True), \
             patch("istota.memory.search.embed_text", return_value=[0.0] * 384):
            _search_vec(conn_no, "alice", "q", limit=10, include_expired=True)
            _search_vec(conn_filt, "alice", "q", limit=10, topics=["tech"])

        assert ks_no[0] == 50   # limit * 5
        assert ks_filt[0] == 100  # limit * 10

    def test_grows_k_when_filter_starves_results(self):
        """If post-filter leaves fewer than `limit` results, k doubles and re-runs."""
        # First call: 2 rows survive the filter (too few). Second: 6 more rows.
        first = [self._make_row(i) for i in (1, 2)]
        second = [self._make_row(i) for i in (1, 2, 3, 4, 5, 6, 7, 8)]
        conn, ks = self._capture_execute([first, second])

        with patch("istota.memory.search.enable_vec_extension", return_value=True), \
             patch("istota.memory.search.embed_text", return_value=[0.0] * 384):
            results = _search_vec(conn, "alice", "q", limit=5, topics=["tech"])

        assert len(ks) == 2
        assert ks[1] == ks[0] * 2
        # Dedup by chunk_id: total distinct chunks across both batches = 8.
        assert len(results) == 5
        assert [r.chunk_id for r in results] == [1, 2, 3, 4, 5]

    def test_stops_when_no_new_rows(self):
        """If a larger k returns no new chunk ids, stop (pool exhausted)."""
        first = [self._make_row(i) for i in (1, 2)]
        # Same rows on second pass — post-filter admits nothing new.
        second = [self._make_row(i) for i in (1, 2)]
        conn, ks = self._capture_execute([first, second, [self._make_row(99)]])

        with patch("istota.memory.search.enable_vec_extension", return_value=True), \
             patch("istota.memory.search.embed_text", return_value=[0.0] * 384):
            results = _search_vec(conn, "alice", "q", limit=10, topics=["tech"])

        # Should have stopped after the 2nd call (no new rows), not tried 3rd.
        assert len(ks) == 2
        assert len(results) == 2

    def test_caps_at_max_k(self):
        """Growth stops once k reaches _VEC_MAX_K, even if still short of limit."""
        # Every iteration returns the same 1 row — never satisfies limit=10.
        batches = [[self._make_row(i)] for i in range(20)]
        conn, ks = self._capture_execute(batches)

        with patch("istota.memory.search.enable_vec_extension", return_value=True), \
             patch("istota.memory.search.embed_text", return_value=[0.0] * 384):
            _search_vec(conn, "alice", "q", limit=10, topics=["tech"])

        assert ks[-1] == _VEC_MAX_K
        # Sequence should be non-decreasing and monotonically doubling up to cap.
        assert ks == sorted(ks)
        assert ks[0] == 100  # limit*10 with filters

    def test_single_pass_when_first_batch_satisfies_limit(self):
        """If first KNN pass yields >= limit rows, no second call."""
        rows = [self._make_row(i) for i in range(50)]
        conn, ks = self._capture_execute([rows])

        with patch("istota.memory.search.enable_vec_extension", return_value=True), \
             patch("istota.memory.search.embed_text", return_value=[0.0] * 384):
            results = _search_vec(conn, "alice", "q", limit=10)

        assert len(ks) == 1
        assert len(results) == 10


# ---------------------------------------------------------------------------
# TestCleanupOldChunks (Item 4: unified memory retention)
# ---------------------------------------------------------------------------


from datetime import datetime, timedelta, timezone


def _insert_chunk_with_age(
    conn,
    user_id: str,
    source_type: str,
    source_id: str,
    content: str,
    age_days: int,
) -> int:
    """Insert a memory_chunk with a backdated created_at."""
    import hashlib
    h = hashlib.sha256(content.encode()).hexdigest()
    when = (
        (datetime.now(timezone.utc) - timedelta(days=age_days))
        .replace(tzinfo=None)
        .isoformat()
    )
    cur = conn.execute(
        "INSERT INTO memory_chunks (user_id, source_type, source_id, chunk_index, content, content_hash, created_at) "
        "VALUES (?, ?, ?, 0, ?, ?, ?)",
        (user_id, source_type, source_id, content, h, when),
    )
    conn.commit()
    return cur.lastrowid


class TestCleanupOldChunks:
    def test_deletes_ephemeral_chunks_older_than_retention(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        old = _insert_chunk_with_age(conn, "alice", "conversation", "task1", "old conv", age_days=120)
        recent = _insert_chunk_with_age(conn, "alice", "conversation", "task2", "recent", age_days=10)

        deleted = cleanup_old_chunks(conn, "alice", retention_days=90)
        assert deleted == 1
        rows = conn.execute("SELECT id FROM memory_chunks").fetchall()
        ids = {r[0] for r in rows}
        assert old not in ids
        assert recent in ids

    def test_preserves_chunks_newer_than_retention(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunk_with_age(conn, "alice", "memory_file", "f1", "young", age_days=30)
        deleted = cleanup_old_chunks(conn, "alice", retention_days=90)
        assert deleted == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0] == 1

    def test_preserves_user_memory_chunks_regardless_of_age(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunk_with_age(conn, "alice", "user_memory", "USER.md", "durable", age_days=999)
        deleted = cleanup_old_chunks(conn, "alice", retention_days=90)
        assert deleted == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0] == 1

    def test_retention_zero_is_noop(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunk_with_age(conn, "alice", "conversation", "t", "ancient", age_days=10000)
        deleted = cleanup_old_chunks(conn, "alice", retention_days=0)
        assert deleted == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0] == 1

    def test_retention_negative_is_noop(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunk_with_age(conn, "alice", "conversation", "t", "ancient", age_days=10000)
        deleted = cleanup_old_chunks(conn, "alice", retention_days=-5)
        assert deleted == 0

    def test_user_id_scoped(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunk_with_age(conn, "alice", "conversation", "t1", "alice old", age_days=200)
        _insert_chunk_with_age(conn, "bob", "conversation", "t2", "bob old", age_days=200)
        deleted = cleanup_old_chunks(conn, "alice", retention_days=90)
        assert deleted == 1
        rows = conn.execute("SELECT user_id FROM memory_chunks").fetchall()
        assert {r[0] for r in rows} == {"bob"}

    def test_default_source_types_include_channel_memory(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunk_with_age(conn, "channel:abc", "channel_memory", "/p/2025-01-01.md", "old chan", age_days=200)
        deleted = cleanup_old_chunks(conn, "channel:abc", retention_days=90)
        assert deleted == 1

    def test_fts_rows_cleared_via_trigger(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        cid = _insert_chunk_with_age(conn, "alice", "conversation", "t", "needle in haystack", age_days=200)
        # Confirm FTS row exists pre-cleanup
        fts_before = conn.execute(
            "SELECT COUNT(*) FROM memory_chunks_fts WHERE rowid = ?", (cid,)
        ).fetchone()[0]
        assert fts_before == 1
        cleanup_old_chunks(conn, "alice", retention_days=90)
        fts_after = conn.execute(
            "SELECT COUNT(*) FROM memory_chunks_fts WHERE rowid = ?", (cid,)
        ).fetchone()[0]
        assert fts_after == 0

    def test_custom_source_types_filter(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunk_with_age(conn, "alice", "conversation", "t1", "conv", age_days=200)
        _insert_chunk_with_age(conn, "alice", "memory_file", "f1", "memfile", age_days=200)
        # Only sweep "conversation"
        deleted = cleanup_old_chunks(
            conn, "alice", retention_days=90, source_types=("conversation",)
        )
        assert deleted == 1
        rows = conn.execute("SELECT source_type FROM memory_chunks").fetchall()
        assert {r[0] for r in rows} == {"memory_file"}

    def test_production_write_path_timestamp_format(self, tmp_path):
        """Regression for the SQLite/Python ISO format mismatch.

        Production INSERTs rely on the `created_at` column default
        (`datetime('now')`), which writes ``'YYYY-MM-DD HH:MM:SS'`` — SPACE
        separator. A cutoff using Python's ``isoformat()`` would use ``'T'``
        and lex-compare as GREATER than the space form on the same date,
        deleting rows up to 24h newer than the retention boundary.

        This test exercises the real write path (no explicit created_at) +
        ages rows via SQLite's own ``datetime('now', '-N days')``, then asserts
        that a row aged inside the retention window is preserved.
        """
        import hashlib
        conn = _init_db(tmp_path / "test.db")
        # Insert via the column default (mirrors production INSERTs in
        # _insert_chunks). No created_at supplied.
        h = hashlib.sha256(b"young content").hexdigest()
        cur = conn.execute(
            "INSERT INTO memory_chunks (user_id, source_type, source_id, chunk_index, content, content_hash) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            ("alice", "conversation", "t1", "young content", h),
        )
        young_id = cur.lastrowid
        # Backdate to 30 days ago, again using SQLite's datetime() so the
        # stored format matches the column default exactly.
        conn.execute(
            "UPDATE memory_chunks SET created_at = datetime('now', '-30 days') WHERE id = ?",
            (young_id,),
        )
        h2 = hashlib.sha256(b"old content").hexdigest()
        cur = conn.execute(
            "INSERT INTO memory_chunks (user_id, source_type, source_id, chunk_index, content, content_hash) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            ("alice", "conversation", "t2", "old content", h2),
        )
        old_id = cur.lastrowid
        conn.execute(
            "UPDATE memory_chunks SET created_at = datetime('now', '-200 days') WHERE id = ?",
            (old_id,),
        )
        conn.commit()

        # Retention 90 days: young (30d) survives, old (200d) deleted.
        deleted = cleanup_old_chunks(conn, "alice", retention_days=90)
        assert deleted == 1
        surviving = {r[0] for r in conn.execute("SELECT id FROM memory_chunks").fetchall()}
        assert young_id in surviving
        assert old_id not in surviving

    def test_same_date_boundary_not_overdeleted(self, tmp_path):
        """A row aged just under the retention boundary stays — even when
        its date prefix matches the cutoff date. This is the exact failure
        mode of the prior ISO-format mismatch (space < 'T' on same date)."""
        import hashlib
        conn = _init_db(tmp_path / "test.db")
        # Row aged 89 days (1 day inside a 90-day window).
        h = hashlib.sha256(b"borderline").hexdigest()
        cur = conn.execute(
            "INSERT INTO memory_chunks (user_id, source_type, source_id, chunk_index, content, content_hash) "
            "VALUES (?, ?, ?, 0, ?, ?)",
            ("alice", "conversation", "t", "borderline", h),
        )
        rid = cur.lastrowid
        conn.execute(
            "UPDATE memory_chunks SET created_at = datetime('now', '-89 days') WHERE id = ?",
            (rid,),
        )
        conn.commit()
        deleted = cleanup_old_chunks(conn, "alice", retention_days=90)
        assert deleted == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0] == 1


class TestRecencyDecayUnit:
    """ISSUE-109 #1 — time-decay weighting counters the 'frequency != relevance'
    gravity well: a dense old cluster shouldn't outrank current context on mass."""

    def _r(self, cid, score, created_at):
        return SearchResult(
            chunk_id=cid, content="c", score=score,
            source_type="conversation", source_id=str(cid),
            created_at=created_at,
        )

    def test_half_life_zero_is_noop(self):
        results = [self._r(1, 0.5, "2024-01-01"), self._r(2, 0.4, "2026-06-04")]
        out = _apply_recency_decay(list(results), half_life_days=0, now="2026-06-04")
        # Order unchanged, scores untouched.
        assert [r.chunk_id for r in out] == [1, 2]
        assert out[0].score == 0.5

    def test_older_chunk_penalized_more(self):
        now = "2026-06-04"
        old = (datetime.fromisoformat(now) - timedelta(days=365)).date().isoformat()
        results = [self._r(1, 0.5, old), self._r(2, 0.5, now)]
        out = _apply_recency_decay(results, half_life_days=180, now=now)
        assert out[0].chunk_id == 2  # equal raw score → newer wins

    def test_order_flips_when_ancient_chunk_scores_higher(self):
        now = "2026-06-04"
        ancient = (datetime.fromisoformat(now) - timedelta(days=730)).date().isoformat()
        results = [self._r(1, 0.55, ancient), self._r(2, 0.50, now)]
        out = _apply_recency_decay(results, half_life_days=180, now=now)
        assert out[0].chunk_id == 2  # decay overcomes the small raw-score edge

    def test_half_life_halves_score(self):
        now = "2026-06-04"
        old = (datetime.fromisoformat(now) - timedelta(days=180)).date().isoformat()
        out = _apply_recency_decay([self._r(1, 1.0, old)], half_life_days=180, now=now)
        assert out[0].score == pytest.approx(0.5, abs=1e-6)

    def test_missing_created_at_no_penalty(self):
        out = _apply_recency_decay([self._r(1, 1.0, "")], half_life_days=180, now="2026-06-04")
        assert out[0].score == pytest.approx(1.0)

    def test_future_created_at_no_boost(self):
        """A clock-skewed future timestamp must not inflate score above raw."""
        out = _apply_recency_decay([self._r(1, 1.0, "2099-01-01")], half_life_days=180, now="2026-06-04")
        assert out[0].score == pytest.approx(1.0)


class TestSearchRecencyDecay:
    def test_decay_reorders_search_results(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunk_with_age(
            conn, "alice", "conversation", "t_old",
            "traveling with cats on the flight to lisbon", age_days=300,
        )
        _insert_chunk_with_age(
            conn, "alice", "conversation", "t_new",
            "cats flight question for next week", age_days=2,
        )
        now = datetime.now(timezone.utc).date().isoformat()
        res = search(conn, "alice", "cats flight", recency_half_life_days=30, now=now)
        assert res, "expected BM25 matches"
        assert res[0].source_id == "t_new"  # recent dominates after decay

    def test_default_no_decay_preserves_behavior(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunk_with_age(conn, "alice", "conversation", "t1", "cats flight one", age_days=300)
        _insert_chunk_with_age(conn, "alice", "conversation", "t2", "cats flight two", age_days=2)
        # No recency_half_life_days passed → no decay; both returned.
        res = search(conn, "alice", "cats flight")
        assert {r.source_id for r in res} == {"t1", "t2"}


class TestEpisodeWindowFiltering:
    """ISSUE-109 #2 — a chunk whose episode window has closed is suppressed on
    the retrieval path (and therefore the always-loaded recall path too)."""

    def test_expired_chunk_excluded(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunks(
            conn, "alice", "memory_file", "f_move",
            ["cat transport logistics during the lisbon move"],
            valid_until="2026-01-01",
        )
        _insert_chunks(
            conn, "alice", "memory_file", "f_vet",
            ["cat vet appointment notes"],
        )
        res = search(conn, "alice", "cat", now="2026-06-04")
        contents = " ".join(r.content for r in res)
        assert "vet" in contents
        assert "transport" not in contents

    def test_include_expired_returns_closed_windows(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunks(
            conn, "alice", "memory_file", "f_move",
            ["cat transport logistics during the lisbon move"],
            valid_until="2026-01-01",
        )
        res = search(conn, "alice", "cat", now="2026-06-04", include_expired=True)
        assert any("transport" in r.content for r in res)

    def test_future_window_kept(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunks(
            conn, "alice", "memory_file", "f_trip",
            ["cat sitter booked for the upcoming trip"],
            valid_until="2026-12-31",
        )
        res = search(conn, "alice", "cat", now="2026-06-04")
        assert any("sitter" in r.content for r in res)

    def test_null_window_always_kept(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunks(conn, "alice", "memory_file", "f", ["cat allergy is lifelong"])
        res = search(conn, "alice", "cat", now="2026-06-04")
        assert any("allergy" in r.content for r in res)


class TestInsertChunksWindows:
    def test_scalar_window_persisted(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunks(
            conn, "alice", "memory_file", "f", ["a chunk"],
            valid_from="2026-01-01", valid_until="2026-03-01",
        )
        row = conn.execute(
            "SELECT valid_from, valid_until FROM memory_chunks WHERE source_id = 'f'"
        ).fetchone()
        assert row["valid_from"] == "2026-01-01"
        assert row["valid_until"] == "2026-03-01"

    def test_per_chunk_valid_until_overrides_scalar(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        _insert_chunks(
            conn, "alice", "memory_file", "f", ["first", "second"],
            valid_from="2026-01-01",
            valid_until_per_chunk=["2026-02-01", None],
        )
        rows = conn.execute(
            "SELECT content, valid_from, valid_until FROM memory_chunks "
            "WHERE source_id = 'f' ORDER BY chunk_index"
        ).fetchall()
        assert rows[0]["content"] == "first"
        assert rows[0]["valid_from"] == "2026-01-01"
        assert rows[0]["valid_until"] == "2026-02-01"
        assert rows[1]["valid_until"] is None


class TestMemoryChunkWindowMigration:
    def test_run_migrations_adds_window_columns(self, tmp_path):
        from istota import db
        conn = sqlite3.connect(str(tmp_path / "old.db"))
        # Old-schema memory_chunks lacking the window columns.
        conn.execute(
            "CREATE TABLE memory_chunks ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, user_id TEXT, source_type TEXT, "
            "source_id TEXT, chunk_index INTEGER, content TEXT, content_hash TEXT, "
            "metadata_json TEXT, created_at TEXT)"
        )
        db._run_migrations(conn)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(memory_chunks)")}
        assert {"valid_from", "valid_until"} <= cols



class TestReindexSkillOverlays:
    """A rule that moved out of USER.md into one skill's overlay reaches a
    prompt only on a task that selected that skill. Search is then the only
    surface that can answer "why does the bot always do X" for it — without
    these rows the rule is findable only by reading the file."""

    SKILLS = ("developer", "notes", "sensitive_actions")

    @classmethod
    def _config(cls, tmp_path, **overrides):
        """A real Config, deliberately not a MagicMock.

        A MagicMock answers every attribute, so it hides exactly the defect
        review found here: `reindex_all` reads `bot_dir_name`, `skills_dir` and
        `bundled_skills_dir`, and its only production caller was handing it a
        stand-in carrying the mount alone.
        """
        from istota.config import Config, UserConfig

        bundled = tmp_path / "bundled"
        for skill in cls.SKILLS:
            d = bundled / skill
            d.mkdir(parents=True, exist_ok=True)
            (d / "skill.md").write_text(
                f"---\nname: {skill}\ndescription: the {skill} skill\n---\n\n# {skill}\n"
            )
        ops = tmp_path / "ops_skills"
        ops.mkdir(exist_ok=True)
        return Config(
            db_path=tmp_path / "istota.db",
            temp_dir=tmp_path / "tmp",
            nextcloud_mount_path=tmp_path / "mount",
            bundled_skills_dir=bundled,
            skills_dir=ops,
            users={"alice": UserConfig()},
            **overrides,
        )

    @staticmethod
    def _overlays(config):
        d = (
            config.nextcloud_mount_path
            / "Users" / "alice" / config.bot_dir_name / "config" / "skills"
        )
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def _reindex(conn, config):
        with patch("istota.memory.search.ensure_vec_table", return_value=False), \
             patch("istota.memory.search.enable_vec_extension", return_value=False):
            return reindex_all(conn, config, "alice", lookback_days=1)

    def test_an_overlay_is_indexed_and_findable(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        config = self._config(tmp_path)
        (self._overlays(config) / "developer.md").write_text(
            "- Never run the full test suite in a foreground task here.\n"
        )

        stats = self._reindex(conn, config)
        assert stats["skill_overlays"] == 1

        rows = conn.execute(
            "SELECT source_id FROM memory_chunks WHERE source_type = 'skill_overlay'"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0].endswith("config/skills/developer.md")
        conn.close()

    def test_it_is_durable_not_ephemeral(self):
        """Like user_memory: refreshed on edit, never aged out. An overlay for
        a rarely-used skill is exactly where a swept index would bite."""
        assert "skill_overlay" not in EPHEMERAL_SOURCE_TYPES

    def test_a_missing_directory_indexes_nothing_and_does_not_raise(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        config = self._config(tmp_path)
        (config.nextcloud_mount_path).mkdir(exist_ok=True)
        assert self._reindex(conn, config)["skill_overlays"] == 0
        conn.close()

    def test_non_markdown_entries_are_skipped(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        config = self._config(tmp_path)
        d = self._overlays(config)
        (d / "notes.md.bak").write_text("- stale copy\n")
        (d / "README.txt").write_text("hi\n")

        assert self._reindex(conn, config)["skill_overlays"] == 0
        conn.close()

    def test_an_empty_overlay_indexes_nothing(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        config = self._config(tmp_path)
        (self._overlays(config) / "developer.md").write_text("\n   \n")

        assert self._reindex(conn, config)["skill_overlays"] == 0
        conn.close()

    # ------------------------------------------- only what binds is indexed

    def test_an_unknown_skill_name_is_not_indexed(self, tmp_path):
        """It reaches no prompt, so indexing it would have search return a rule
        that is in no prompt — the failure delete-on-empty exists to prevent,
        arrived at from the other side. `doctor` is what reports the file."""
        conn = _init_db(tmp_path / "test.db")
        config = self._config(tmp_path)
        (self._overlays(config) / "develper.md").write_text("- a rule\n")

        assert self._reindex(conn, config)["skill_overlays"] == 0
        conn.close()

    def test_a_denylisted_skill_is_not_indexed(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        config = self._config(tmp_path)
        (self._overlays(config) / "sensitive_actions.md").write_text("- planted\n")

        assert self._reindex(conn, config)["skill_overlays"] == 0
        conn.close()

    def test_an_over_cap_overlay_is_not_indexed(self, tmp_path):
        from istota.skills._loader import OVERLAY_MAX_BYTES

        conn = _init_db(tmp_path / "test.db")
        config = self._config(tmp_path)
        (self._overlays(config) / "developer.md").write_text(
            "- x\n" * (OVERLAY_MAX_BYTES // 4 + 4)
        )

        assert self._reindex(conn, config)["skill_overlays"] == 0
        conn.close()

    # ----------------------------------------------------- the plantable tree

    def test_a_planted_symlink_file_is_not_indexed(self, tmp_path):
        """`{mount}/Users/{user_id}` is bound read-write into that user's own
        sandbox, so every entry here is model-plantable. A followed symlink
        would put a daemon-readable file into a store `!search` reads back."""
        conn = _init_db(tmp_path / "test.db")
        config = self._config(tmp_path)
        secret = tmp_path / "credentials.json"
        secret.write_text("- TOP SECRET TOKEN value\n")
        (self._overlays(config) / "developer.md").symlink_to(secret)

        assert self._reindex(conn, config)["skill_overlays"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE content LIKE '%TOP SECRET%'"
        ).fetchone()[0] == 0
        conn.close()

    def test_a_redirected_overlay_directory_is_not_walked(self, tmp_path):
        """`O_NOFOLLOW` on the file covers the last component only. The files at
        the far end of a redirected `config/` or `skills/` are ordinary regular
        files that pass every leaf-level guard, so containment is the gate."""
        conn = _init_db(tmp_path / "test.db")
        config = self._config(tmp_path)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / "developer.md").write_text("- CONSTITUTIONAL SECRET TEXT\n")
        user_config = (
            config.nextcloud_mount_path
            / "Users" / "alice" / config.bot_dir_name / "config"
        )
        user_config.mkdir(parents=True)
        (user_config / "skills").symlink_to(elsewhere, target_is_directory=True)

        assert self._reindex(conn, config)["skill_overlays"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE content LIKE '%CONSTITUTIONAL%'"
        ).fetchone()[0] == 0
        conn.close()

    @pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="no mkfifo on this platform")
    def test_a_fifo_does_not_hang_the_reindex(self, tmp_path):
        """A FIFO with no writer blocks `open(2)`."""
        conn = _init_db(tmp_path / "test.db")
        config = self._config(tmp_path)
        os.mkfifo(self._overlays(config) / "developer.md")

        assert self._reindex(conn, config)["skill_overlays"] == 0
        conn.close()

    # --------------------------------------------------------------- staleness

    def test_re_running_replaces_rather_than_duplicates(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        config = self._config(tmp_path)
        path = self._overlays(config) / "developer.md"
        path.write_text("- first rule about deployment hosts\n")
        self._reindex(conn, config)
        path.write_text("- a completely different rule about branches\n")
        self._reindex(conn, config)

        rows = conn.execute(
            "SELECT content FROM memory_chunks WHERE source_type = 'skill_overlay'"
        ).fetchall()
        assert len(rows) == 1
        assert "branches" in rows[0][0]
        conn.close()

    def test_rows_for_a_file_deleted_outside_the_cli_are_reaped(self, tmp_path):
        """Unlike USER.md, an overlay is a file the workflow deletes — and a
        deletion over Nextcloud calls no CLI. `skill_overlay` is outside
        EPHEMERAL_SOURCE_TYPES, so nothing else would ever reclaim the rows."""
        conn = _init_db(tmp_path / "test.db")
        config = self._config(tmp_path)
        path = self._overlays(config) / "developer.md"
        path.write_text("- a rule that is about to be deleted\n")
        assert self._reindex(conn, config)["skill_overlays"] == 1

        path.unlink()
        assert self._reindex(conn, config)["skill_overlays"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE source_type = 'skill_overlay'"
        ).fetchone()[0] == 0
        conn.close()

    def test_a_pass_that_walked_nothing_reaps_nothing(self, tmp_path):
        """An empty `live` set must never read as "every overlay was deleted".
        Every path that walks no directory returns before the reap."""
        conn = _init_db(tmp_path / "test.db")
        config = self._config(tmp_path)
        overlays = self._overlays(config)
        (overlays / "developer.md").write_text("- a rule\n")
        assert self._reindex(conn, config)["skill_overlays"] == 1

        # The directory is now redirected out of the user tree, so the walk
        # refuses. The existing rows must survive it.
        import shutil

        shutil.rmtree(overlays)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        overlays.symlink_to(elsewhere, target_is_directory=True)

        assert self._reindex(conn, config)["skill_overlays"] == 0
        assert conn.execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE source_type = 'skill_overlay'"
        ).fetchone()[0] == 1
        conn.close()

    def test_the_only_production_caller_supplies_everything_reindex_reads(
        self, tmp_path, monkeypatch
    ):
        """`istota-skill memory_search reindex` used to hand `reindex_all` a
        `SimpleNamespace` carrying the mount alone, so with a mount configured
        the whole verb died on `config.bot_dir_name` before any block ran."""
        from istota.skills.memory_search import cmd_reindex

        conn = _init_db(tmp_path / "test.db")
        conn.close()
        config = self._config(tmp_path)
        (self._overlays(config) / "developer.md").write_text("- a rule\n")
        monkeypatch.setenv("ISTOTA_DB_PATH", str(tmp_path / "test.db"))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setattr("istota.config.load_config", lambda *a, **kw: config)

        with patch("istota.memory.search.ensure_vec_table", return_value=False), \
             patch("istota.memory.search.enable_vec_extension", return_value=False):
            out = cmd_reindex(SimpleNamespace(lookback_days=1))

        assert out["status"] == "ok"
        assert out["skill_overlays"] == 1
