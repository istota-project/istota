"""Tests for the memory search CLI skill."""

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.skills.memory_search import (
    build_parser,
    cmd_index_conversation,
    cmd_index_file,
    cmd_reindex,
    cmd_search,
    cmd_stats,
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

        with patch("istota.memory_search._search_vec", return_value=[]):
            result = cmd_search(args)

        assert result["status"] == "ok"
        assert result["count"] >= 1
        assert result["results"][0]["content"] == "Python programming guide"

    def test_search_empty_results(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.query = "nonexistent"
        args.limit = 10
        args.source_type = None

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
