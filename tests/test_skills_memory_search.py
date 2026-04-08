"""Tests for the memory search CLI skill."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.skills.memory_search import (
    build_parser,
    cmd_add_fact,
    cmd_delete_fact,
    cmd_facts,
    cmd_index_conversation,
    cmd_index_file,
    cmd_invalidate_fact,
    cmd_reindex,
    cmd_search,
    cmd_stats,
    cmd_timeline,
    main,
)


def _init_db(db_path: Path) -> sqlite3.Connection:
    """Initialize a test database with the memory_chunks schema."""
    schema_path = Path(__file__).parent.parent / "schema.sql"
    conn = sqlite3.connect(str(db_path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    conn.executescript(schema_path.read_text())
    return conn


class TestBuildParser:
    def test_search_command(self):
        parser = build_parser()
        args = parser.parse_args(["search", "hello world"])
        assert args.command == "search"
        assert args.query == "hello world"
        assert args.limit == 10

    def test_search_with_options(self):
        parser = build_parser()
        args = parser.parse_args(["search", "test", "--limit", "5", "--source-type", "conversation"])
        assert args.limit == 5
        assert args.source_type == "conversation"

    def test_search_with_since(self):
        parser = build_parser()
        args = parser.parse_args(["search", "test", "--since", "2026-03-25"])
        assert args.since == "2026-03-25"

    def test_search_without_since_defaults_none(self):
        parser = build_parser()
        args = parser.parse_args(["search", "test"])
        assert args.since is None

    def test_index_conversation_command(self):
        parser = build_parser()
        args = parser.parse_args(["index", "conversation", "42"])
        assert args.command == "index"
        assert args.index_command == "conversation"
        assert args.task_id == 42

    def test_index_file_command(self):
        parser = build_parser()
        args = parser.parse_args(["index", "file", "/path/to/file.md"])
        assert args.command == "index"
        assert args.index_command == "file"
        assert args.path == "/path/to/file.md"

    def test_reindex_command(self):
        parser = build_parser()
        args = parser.parse_args(["reindex", "--lookback-days", "30"])
        assert args.command == "reindex"
        assert args.lookback_days == 30

    def test_stats_command(self):
        parser = build_parser()
        args = parser.parse_args(["stats"])
        assert args.command == "stats"


class TestCmdSearch:
    def test_search_returns_results(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)

        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            from istota.memory_search import _insert_chunks
            _insert_chunks(conn, "alice", "conversation", "1", ["Python programming guide"], None)
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.query = "Python"
        args.limit = 10
        args.source_type = None
        args.topic = None
        args.entity = None

        with patch("istota.memory_search._search_vec", return_value=[]):
            result = cmd_search(args)

        assert result["status"] == "ok"
        assert result["count"] >= 1
        assert result["results"][0]["content"] == "Python programming guide"

    def test_search_passes_since_to_search(self, tmp_path, monkeypatch):
        """--since flag should be forwarded to memory_search.search()."""
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.query = "test"
        args.limit = 10
        args.source_type = None
        args.since = "2026-03-25"
        args.topic = None
        args.entity = None

        with patch("istota.memory_search.search") as mock_search:
            mock_search.return_value = []
            result = cmd_search(args)

        mock_search.assert_called_once()
        assert mock_search.call_args.kwargs.get("since") == "2026-03-25"
        assert result["status"] == "ok"

    def test_search_empty_results(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.query = "nonexistent"
        args.limit = 10
        args.source_type = None
        args.topic = None
        args.entity = None

        with patch("istota.memory_search._search_vec", return_value=[]):
            result = cmd_search(args)

        assert result["status"] == "ok"
        assert result["count"] == 0


class TestCmdIndexConversation:
    def test_index_existing_task(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)
        conn.execute(
            "INSERT INTO tasks (id, user_id, source_type, prompt, result, status) "
            "VALUES (1, 'alice', 'talk', 'What is AI?', 'AI is cool.', 'completed')"
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.task_id = 1

        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            result = cmd_index_conversation(args)

        assert result["status"] == "ok"
        assert result["chunks_inserted"] >= 1

    def test_index_nonexistent_task(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.task_id = 999

        result = cmd_index_conversation(args)
        assert result["status"] == "error"


class TestCmdIndexFile:
    def test_index_file(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        file_path = tmp_path / "memory.md"
        file_path.write_text("Some memory content about projects")

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.path = str(file_path)
        args.source_type = None

        with patch("istota.memory_search.ensure_vec_table", return_value=False), \
             patch("istota.memory_search.enable_vec_extension", return_value=False):
            result = cmd_index_file(args)

        assert result["status"] == "ok"
        assert result["chunks_inserted"] >= 1

    def test_index_missing_file(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.path = "/nonexistent/file.md"
        args.source_type = None

        result = cmd_index_file(args)
        assert result["status"] == "error"


class TestCmdReindex:
    def test_reindex(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)
        conn.execute(
            "INSERT INTO tasks (user_id, source_type, prompt, result, status, created_at) "
            "VALUES ('alice', 'talk', 'Hello', 'Hi there', 'completed', datetime('now'))"
        )
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", "")

        args = MagicMock()
        args.lookback_days = 90

        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            result = cmd_reindex(args)

        assert result["status"] == "ok"
        assert result["conversations"] >= 1


class TestCmdStats:
    def test_stats(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)

        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            from istota.memory_search import _insert_chunks
            _insert_chunks(conn, "alice", "conversation", "1", ["test chunk"], None)
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()

        with patch("istota.memory_search.enable_vec_extension", return_value=False):
            result = cmd_stats(args)

        assert result["status"] == "ok"
        assert result["total_chunks"] == 1


class TestConversationTokenEnvVar:
    def test_search_includes_channel_when_token_set(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)

        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            from istota.memory_search import _insert_chunks
            _insert_chunks(conn, "alice", "conversation", "1", ["user data"], None)
            _insert_chunks(conn, "channel:room123", "channel_memory", "f1", ["channel decision about GraphQL"], None)
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "room123")

        args = MagicMock()
        args.query = "GraphQL"
        args.limit = 10
        args.source_type = None
        args.topic = None
        args.entity = None

        with patch("istota.memory_search._search_vec", return_value=[]):
            result = cmd_search(args)

        assert result["status"] == "ok"
        contents = [r["content"] for r in result["results"]]
        assert any("GraphQL" in c for c in contents)

    def test_search_no_channel_without_token(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)

        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            from istota.memory_search import _insert_chunks
            _insert_chunks(conn, "channel:room123", "channel_memory", "f1", ["channel only content"], None)
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        # No ISTOTA_CONVERSATION_TOKEN set

        args = MagicMock()
        args.query = "channel"
        args.limit = 10
        args.source_type = None
        args.topic = None
        args.entity = None

        with patch("istota.memory_search._search_vec", return_value=[]):
            result = cmd_search(args)

        assert result["count"] == 0

    def test_stats_includes_channel_when_token_set(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)

        with patch("istota.memory_search.ensure_vec_table", return_value=False):
            from istota.memory_search import _insert_chunks
            _insert_chunks(conn, "alice", "conversation", "1", ["user chunk"], None)
            _insert_chunks(conn, "channel:room456", "channel_memory", "f1", ["channel chunk"], None)
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "room456")

        args = MagicMock()

        with patch("istota.memory_search.enable_vec_extension", return_value=False):
            result = cmd_stats(args)

        assert result["status"] == "ok"
        assert result["total_chunks"] == 2


class TestMain:
    def test_main_search(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        with patch("istota.memory_search._search_vec", return_value=[]):
            main(["search", "hello"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"

    def test_main_stats(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        with patch("istota.memory_search.enable_vec_extension", return_value=False):
            main(["stats"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"


class TestBuildParserKG:
    def test_facts_command(self):
        parser = build_parser()
        args = parser.parse_args(["facts"])
        assert args.command == "facts"
        assert args.subject is None
        assert args.predicate is None
        assert args.as_of is None

    def test_facts_with_filters(self):
        parser = build_parser()
        args = parser.parse_args(["facts", "--subject", "stefan", "--predicate", "knows"])
        assert args.subject == "stefan"
        assert args.predicate == "knows"

    def test_facts_with_as_of(self):
        parser = build_parser()
        args = parser.parse_args(["facts", "--as-of", "2025-06-15"])
        assert args.as_of == "2025-06-15"

    def test_timeline_command(self):
        parser = build_parser()
        args = parser.parse_args(["timeline", "stefan"])
        assert args.command == "timeline"
        assert args.subject == "stefan"

    def test_add_fact_command(self):
        parser = build_parser()
        args = parser.parse_args(["add-fact", "stefan", "works_at", "acme", "--from", "2025-06-01"])
        assert args.command == "add-fact"
        assert args.subject == "stefan"
        assert args.predicate == "works_at"
        assert args.object == "acme"
        assert args.valid_from == "2025-06-01"

    def test_invalidate_command(self):
        parser = build_parser()
        args = parser.parse_args(["invalidate", "42", "--ended", "2026-04-08"])
        assert args.command == "invalidate"
        assert args.fact_id == 42
        assert args.ended == "2026-04-08"

    def test_delete_fact_command(self):
        parser = build_parser()
        args = parser.parse_args(["delete-fact", "42"])
        assert args.command == "delete-fact"
        assert args.fact_id == 42


class TestCmdFacts:
    def test_empty_facts(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.subject = None
        args.predicate = None
        args.as_of = None

        result = cmd_facts(args)
        assert result["status"] == "ok"
        assert result["count"] == 0

    def test_facts_with_data(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)
        from istota.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        add_fact(conn, "alice", "stefan", "knows", "python")
        add_fact(conn, "alice", "stefan", "works_at", "acme")
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.subject = None
        args.predicate = None
        args.as_of = None

        result = cmd_facts(args)
        assert result["count"] == 2

    def test_facts_filter_by_subject(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)
        from istota.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        add_fact(conn, "alice", "stefan", "knows", "python")
        add_fact(conn, "alice", "istota", "uses_tech", "svelte")
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.subject = "stefan"
        args.predicate = None
        args.as_of = None

        result = cmd_facts(args)
        assert result["count"] == 1
        assert result["facts"][0]["subject"] == "stefan"

    def test_facts_as_of(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)
        from istota.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        add_fact(conn, "alice", "stefan", "works_at", "acme", valid_from="2025-01-01")
        add_fact(conn, "alice", "stefan", "works_at", "globex", valid_from="2026-04-01")
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.subject = None
        args.predicate = None
        args.as_of = "2025-06-15"

        result = cmd_facts(args)
        assert result["count"] == 1
        assert result["facts"][0]["object"] == "acme"


class TestCmdTimeline:
    def test_timeline(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)
        from istota.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        add_fact(conn, "alice", "stefan", "works_at", "acme", valid_from="2025-01-01")
        add_fact(conn, "alice", "stefan", "works_at", "globex", valid_from="2026-04-01")
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.subject = "stefan"

        result = cmd_timeline(args)
        assert result["status"] == "ok"
        assert result["count"] == 2
        assert result["subject"] == "stefan"

    def test_timeline_empty(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.subject = "nobody"

        result = cmd_timeline(args)
        assert result["count"] == 0


class TestCmdAddFact:
    def test_add_fact(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.subject = "stefan"
        args.predicate = "knows"
        args.object = "python"
        args.valid_from = None

        result = cmd_add_fact(args)
        assert result["status"] == "ok"
        assert "fact_id" in result

    def test_add_duplicate(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)
        from istota.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        add_fact(conn, "alice", "stefan", "knows", "python")
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.subject = "stefan"
        args.predicate = "knows"
        args.object = "python"
        args.valid_from = None

        result = cmd_add_fact(args)
        assert result["status"] == "ok"
        assert "Duplicate" in result["message"]


class TestCmdInvalidateFact:
    def test_invalidate(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)
        from istota.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        fact_id = add_fact(conn, "alice", "stefan", "knows", "python")
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.fact_id = fact_id
        args.ended = "2026-04-08"

        result = cmd_invalidate_fact(args)
        assert result["status"] == "ok"

    def test_invalidate_nonexistent(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.fact_id = 9999
        args.ended = None

        result = cmd_invalidate_fact(args)
        assert result["status"] == "error"


class TestCmdDeleteFact:
    def test_delete(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)
        from istota.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        fact_id = add_fact(conn, "alice", "stefan", "knows", "python")
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.fact_id = fact_id

        result = cmd_delete_fact(args)
        assert result["status"] == "ok"

    def test_delete_nonexistent(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.fact_id = 9999

        result = cmd_delete_fact(args)
        assert result["status"] == "error"


class TestStatsIncludesKG:
    def test_stats_includes_knowledge_facts(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)
        from istota.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        add_fact(conn, "alice", "stefan", "knows", "python")
        add_fact(conn, "alice", "stefan", "works_at", "acme")
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()

        with patch("istota.memory_search.enable_vec_extension", return_value=False):
            result = cmd_stats(args)

        assert result["status"] == "ok"
        assert "knowledge_facts" in result
        assert result["knowledge_facts"]["current"] == 2


class TestMainKG:
    def test_main_facts(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        main(["facts"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"

    def test_main_add_fact(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        main(["add-fact", "stefan", "knows", "python"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert "fact_id" in output

    def test_main_timeline(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        main(["timeline", "stefan"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
