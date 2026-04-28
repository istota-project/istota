"""Tests for the memory search core module."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.memory.search import (
    SearchResult,
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
    ensure_vec_table,
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
                          ["Stefan uses Python"], None, entities=["stefan", "python"])

        row = conn.execute("SELECT entities FROM memory_chunks WHERE user_id = 'alice'").fetchone()
        import json
        assert json.loads(row[0]) == ["stefan", "python"]
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
                          topic="personal", entities=["stefan"])

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
                          ["Stefan works on Istota project"], None,
                          entities=["stefan", "istota"])
            _insert_chunks(conn, "alice", "conversation", "2",
                          ["Alice works on Hermes project"], None,
                          entities=["alice", "hermes"])

        with patch("istota.memory.search._search_vec", return_value=[]):
            results = search(conn, "alice", "works on", entities=["stefan"])

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
        # Matches the column order returned by _search_vec's SELECT.
        return (chunk_id, distance, f"content {chunk_id}", "conversation", str(chunk_id), None)

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
        """When any post-filter is active, initial k is limit*10 (vs limit*5 without)."""
        conn_no, ks_no = self._capture_execute([[self._make_row(i) for i in range(10)]])
        conn_filt, ks_filt = self._capture_execute([[self._make_row(i) for i in range(10)]])

        with patch("istota.memory.search.enable_vec_extension", return_value=True), \
             patch("istota.memory.search.embed_text", return_value=[0.0] * 384):
            _search_vec(conn_no, "alice", "q", limit=10)
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
