"""Tests for the memory search core module."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.memory_search import (
    SearchResult,
    _content_hash,
    _escape_fts5_query,
    _insert_chunks,
    _delete_source_chunks,
    _rrf_fusion,
    _serialize_embedding,
    chunk_text,
    embed_batch,
    embed_text,
    ensure_vec_table,
    get_stats,
    index_conversation,
    index_file,
    reindex_all,
    search,
    _search_bm25,
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
    @patch("istota.memory_search._get_model")
    def test_embed_text_returns_none_when_no_model(self, mock_model):
        mock_model.return_value = None
        assert embed_text("hello") is None

    @patch("istota.memory_search._get_model")
    def test_embed_text_with_model(self, mock_model):
        import numpy as np
        mock = MagicMock()
        mock.encode.return_value = np.array([0.1, 0.2, 0.3])
        mock_model.return_value = mock
        result = embed_text("hello")
        assert result is not None
        assert len(result) == 3
        mock.encode.assert_called_once_with("hello", normalize_embeddings=True)

    @patch("istota.memory_search._get_model")
    def test_embed_batch_returns_none_when_no_model(self, mock_model):
        mock_model.return_value = None
        assert embed_batch(["hello", "world"]) is None

    @patch("istota.memory_search._get_model")
    def test_embed_batch_empty_list(self, mock_model):
        assert embed_batch([]) == []

    @patch("istota.memory_search._get_model")
    def test_embed_batch_with_model(self, mock_model):
        import numpy as np
        mock = MagicMock()
        mock.encode.return_value = np.array([[0.1, 0.2], [0.3, 0.4]])
        mock_model.return_value = mock
        result = embed_batch(["hello", "world"])
        assert result is not None
        assert len(result) == 2


class TestInsertAndSearch:
    """Tests using real SQLite with FTS5 (BM25 search)."""

    def test_insert_and_bm25_search(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["Hello world from Alice"], {"task_id": "1"})
            _insert_chunks(conn, "alice", "conversation", "2", ["Python programming is fun"], {"task_id": "2"})

        results = _search_bm25(conn, "alice", "Python programming", 10)
        assert len(results) > 0
        assert "Python" in results[0].content
        conn.close()

    def test_dedup_by_content_hash(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            n1 = _insert_chunks(conn, "alice", "conversation", "1", ["Hello world"], None)
            n2 = _insert_chunks(conn, "alice", "conversation", "2", ["Hello world"], None)  # same content

        assert n1 == 1
        assert n2 == 0  # dedup
        row = conn.execute("SELECT COUNT(*) FROM memory_chunks WHERE user_id = 'alice'").fetchone()
        assert row[0] == 1
        conn.close()

    def test_user_isolation(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["Alice secret data"], None)
            _insert_chunks(conn, "bob", "conversation", "2", ["Bob private info"], None)

        alice_results = _search_bm25(conn, "alice", "secret data", 10)
        bob_results = _search_bm25(conn, "bob", "secret data", 10)

        assert len(alice_results) == 1
        assert len(bob_results) == 0
        conn.close()

    def test_source_type_filter(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["Machine learning topic"], None)
            _insert_chunks(conn, "alice", "memory_file", "/mem.md", ["Machine learning notes"], None)

        conv_only = _search_bm25(conn, "alice", "machine learning", 10, source_types=["conversation"])
        assert len(conv_only) == 1
        assert conv_only[0].source_type == "conversation"
        conn.close()

    def test_delete_source_chunks(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory_search.ensure_vec_table", return_value=False), \
             patch("istota.memory_search.enable_vec_extension", return_value=False):
            _insert_chunks(conn, "alice", "memory_file", "/f.md", ["Chunk one", "Chunk two"], None)

        count = conn.execute("SELECT COUNT(*) FROM memory_chunks WHERE user_id = 'alice'").fetchone()[0]
        assert count == 2

        with patch("istota.memory_search.enable_vec_extension", return_value=False):
            deleted = _delete_source_chunks(conn, "alice", "memory_file", "/f.md")
        assert deleted == 2

        count = conn.execute("SELECT COUNT(*) FROM memory_chunks WHERE user_id = 'alice'").fetchone()[0]
        assert count == 0
        conn.close()


class TestIndexConversation:
    def test_basic_indexing(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            n = index_conversation(conn, "alice", 42, "What is Python?", "Python is a programming language.")

        assert n > 0
        results = _search_bm25(conn, "alice", "Python programming", 10)
        assert len(results) > 0
        conn.close()

    def test_empty_prompt_and_result(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            n = index_conversation(conn, "alice", 99, "", "")
        assert n == 0
        conn.close()


class TestIndexFile:
    def test_file_indexing_replaces_existing(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory_search.ensure_vec_table", return_value=False), \
             patch("istota.memory_search.enable_vec_extension", return_value=False):
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
        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["Quantum computing research"], None)

        with patch("istota.memory_search._search_vec", return_value=[]):
            results = search(conn, "alice", "quantum computing", limit=5)

        assert len(results) > 0
        assert results[0].bm25_rank == 1
        conn.close()

    def test_hybrid_search_with_mock_vec(self, tmp_path):
        """When vec results are available, RRF fusion is used."""
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["Neural network training"], None)

        # Get the chunk_id that was inserted
        row = conn.execute("SELECT id FROM memory_chunks LIMIT 1").fetchone()
        chunk_id = row[0]

        mock_vec_result = SearchResult(
            chunk_id=chunk_id, content="Neural network training",
            score=0.95, source_type="conversation", source_id="1",
        )
        with patch("istota.memory_search._search_vec", return_value=[mock_vec_result]):
            results = search(conn, "alice", "neural network", limit=5)

        assert len(results) > 0
        conn.close()


class TestGetStats:
    def test_stats_with_data(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["chunk one"], None)
            _insert_chunks(conn, "alice", "conversation", "2", ["chunk two"], None)
            _insert_chunks(conn, "alice", "memory_file", "/f.md", ["chunk three"], None)

        with patch("istota.memory_search.enable_vec_extension", return_value=False):
            stats = get_stats(conn, "alice")

        assert stats["total_chunks"] == 3
        assert stats["by_source_type"]["conversation"] == 2
        assert stats["by_source_type"]["memory_file"] == 1
        assert stats["user_id"] == "alice"
        conn.close()

    def test_stats_empty(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory_search.enable_vec_extension", return_value=False):
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

        with patch("istota.memory_search.ensure_vec_table", return_value=False):
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

        with patch("istota.memory_search.ensure_vec_table", return_value=False), \
             patch("istota.memory_search.enable_vec_extension", return_value=False):
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

        with patch("istota.memory_search.ensure_vec_table", return_value=False), \
             patch("istota.memory_search.enable_vec_extension", return_value=False):
            stats = reindex_all(conn, config, "alice", lookback_days=1)

        assert stats.get("channel_memories", 0) >= 1
        conn.close()


class TestIncludeUserIds:
    """Tests for multi-user search support (include_user_ids parameter)."""

    def test_search_bm25_includes_channel(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["user data"], None)
            _insert_chunks(conn, "channel:room123", "channel_memory", "f1", ["channel decision"], None)

        with patch("istota.memory_search._search_vec", return_value=[]):
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
        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["user data"], None)
            _insert_chunks(conn, "channel:room123", "channel_memory", "f1", ["channel decision"], None)

        with patch("istota.memory_search._search_vec", return_value=[]):
            results = search(conn, "alice", "decision", limit=5)

        contents = [r.content for r in results]
        assert "channel decision" not in contents
        conn.close()

    def test_stats_includes_channel(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["user chunk"], None)
            _insert_chunks(conn, "channel:room123", "channel_memory", "f1", ["channel chunk"], None)

        with patch("istota.memory_search.enable_vec_extension", return_value=False):
            stats = get_stats(conn, "alice", include_user_ids=["channel:room123"])

        assert stats["total_chunks"] == 2
        assert stats["by_source_type"].get("channel_memory") == 1
        conn.close()

    def test_stats_without_include_user_ids(self, tmp_path):
        conn = _init_db(tmp_path / "test.db")
        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            _insert_chunks(conn, "alice", "conversation", "1", ["user chunk"], None)
            _insert_chunks(conn, "channel:room123", "channel_memory", "f1", ["channel chunk"], None)

        with patch("istota.memory_search.enable_vec_extension", return_value=False):
            stats = get_stats(conn, "alice")

        assert stats["total_chunks"] == 1
        conn.close()
