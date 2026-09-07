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
    cmd_invalidate_fact,
    cmd_reindex,
    cmd_search,
    cmd_stats,
    cmd_timeline,
    main,
)
from tests.support.skill_cli import run_skill_main


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

        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            from istota.memory.search import _insert_chunks
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

        with patch("istota.memory.search._search_vec", return_value=[]):
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

        with patch("istota.memory.search.search") as mock_search:
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

        with patch("istota.memory.search._search_vec", return_value=[]):
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

        with patch("istota.memory.search.ensure_vec_table", return_value=False):
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
    """`index file` is stamped `EGRESS`, so its bound is at the parse.

    Every refusal below drives `main` rather than `cmd_index_file`: the
    resolution moved out of the handler into `parse_and_resolve` (ISSUE-447),
    so calling the handler with a hostile path now exercises nothing at all —
    which is the shape of test this whole spec is written against.

    `EGRESS` and not `READ` because the bytes leave the task: `search` hands
    them back afterwards. So the one root is `{mount}/Users/{user}` — the
    deferred dir, the channel directory and `{mount}/Talk` are all refused,
    where before this stage the first two were admitted.
    """

    @staticmethod
    def _mount(tmp_path, monkeypatch, db_path):
        mount = tmp_path / "mount"
        for sub in ("Users/alice", "Users/bob", "Channels/tok1", "Talk"):
            (mount / sub).mkdir(parents=True)
        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("NEXTCLOUD_MOUNT_PATH", str(mount))
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path / "deferred"))
        (tmp_path / "deferred").mkdir(exist_ok=True)
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "tok1")
        return mount

    def test_index_file(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()
        mount = self._mount(tmp_path, monkeypatch, db_path)
        note = mount / "Users" / "alice" / "memory.md"
        note.write_text("Some memory content about projects")

        with patch("istota.memory.search.ensure_vec_table", return_value=False), \
             patch("istota.memory.search.enable_vec_extension", return_value=False):
            run = run_skill_main(main, ["index", "file", str(note)])

        assert run.exit_code == 0, run.stdout
        assert run.envelope["status"] == "ok"
        assert run.envelope["chunks_inserted"] >= 1

    def test_index_missing_file(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()
        mount = self._mount(tmp_path, monkeypatch, db_path)

        run = run_skill_main(
            main, ["index", "file", str(mount / "Users" / "alice" / "nope.md")],
        )
        assert run.exit_code == 1
        assert run.envelope["status"] == "error"

    def test_refuses_path_outside_the_callers_roots(self, tmp_path, monkeypatch):
        """`index file` + `search` is a file read-back oracle if left unbounded.

        The CLI runs host-side under the skill proxy, so the path argument is
        evaluated with the daemon's filesystem access — indexing the config
        file and then retrieving it through `search` would walk past both the
        sandbox masks and the credential proxy. The other subcommands are
        scoped by their SQL; a filesystem argument needs its own bound.
        """
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()
        self._mount(tmp_path, monkeypatch, db_path)
        secret = tmp_path / "config.toml"
        secret.write_text('app_password = "hunter2"')

        run = run_skill_main(main, ["index", "file", str(secret)])

        assert run.exit_code == 1
        assert run.envelope["status"] == "error"
        assert run.envelope["reason"] == "host_path_refused"
        # The shared allowlist's message since ISSUE-447 put this verb on the
        # shared rule; it deliberately does not enumerate the roots.
        assert "outside allowed roots" in run.envelope["error"]

    def test_refuses_traversal_out_of_a_permitted_root(self, tmp_path, monkeypatch):
        """The bound is on the resolved path, or `../` walks straight out."""
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()
        mount = self._mount(tmp_path, monkeypatch, db_path)
        secret = tmp_path / "config.toml"
        secret.write_text('app_password = "hunter2"')

        run = run_skill_main(main, [
            "index", "file",
            str(mount / "Users" / "alice" / ".." / ".." / ".." / "config.toml"),
        ])

        assert run.exit_code == 1
        assert run.envelope["status"] == "error"

    def test_allows_the_users_own_mount_directory(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()
        mount = self._mount(tmp_path, monkeypatch, db_path)
        note = mount / "Users" / "alice" / "USER.md"
        note.write_text("Some memory content about projects")

        with patch("istota.memory.search.ensure_vec_table", return_value=False), \
             patch("istota.memory.search.enable_vec_extension", return_value=False):
            run = run_skill_main(main, ["index", "file", str(note)])

        assert run.envelope["status"] == "ok", run.stdout

    def test_refuses_another_users_mount_directory(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()
        mount = self._mount(tmp_path, monkeypatch, db_path)
        theirs = mount / "Users" / "bob" / "USER.md"
        theirs.write_text("bob's private notes")

        run = run_skill_main(main, ["index", "file", str(theirs)])

        assert run.exit_code == 1
        assert run.envelope["status"] == "error"

    @pytest.mark.parametrize("where", ["channel", "talk"])
    def test_the_egress_stamp_drops_the_shared_roots(
        self, tmp_path, monkeypatch, where,
    ):
        """The two roots this verb used to have and no longer does.

        Stage 2 of ISSUE-447 moved `index file` onto the shared rule with its
        root set preserved exactly — deferred dir, channel directory, own
        workspace — because a consolidation that changes a boundary is the
        failure the spec exists to prevent. The `EGRESS` stamp is what
        narrows it, and this is the test that says so: indexing puts the
        content in a store `search` reads back after the task is over, so the
        *shared* roots go. The deferred dir stays — it is the user's own temp
        directory, shared with that user's other concurrent tasks and with
        nobody else, the case beside this one asserts it, and dropping it made
        the verb refuse everything on a deployment with no mount.
        """
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()
        mount = self._mount(tmp_path, monkeypatch, db_path)
        source = {
            "channel": mount / "Channels" / "tok1" / "note.md",
            "talk": mount / "Talk" / "note.md",
        }[where]
        source.write_text("someone else's content")

        run = run_skill_main(main, ["index", "file", str(source)])

        assert run.exit_code == 1
        assert run.envelope["reason"] == "host_path_refused"

    def test_the_tasks_own_deferred_directory_is_still_indexable(
        self, tmp_path, monkeypatch,
    ):
        """`EGRESS` drops what is *shared*, and this directory is not.

        It is one task's own scratch space, per user, writable by nothing
        else — and on a deployment with no mount it is the only root there
        is, so excluding it made this verb refuse every path on a shape where
        it used to work.
        """
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()
        self._mount(tmp_path, monkeypatch, db_path)
        source = tmp_path / "deferred" / "note.md"
        source.write_text("my own scratch note")

        run = run_skill_main(main, ["index", "file", str(source)])

        assert run.exit_code == 0, run.stdout
        assert run.envelope["status"] == "ok", run.stdout


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

        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            result = cmd_reindex(args)

        assert result["status"] == "ok"
        assert result["conversations"] >= 1


class TestCmdStats:
    def test_stats(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)

        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            from istota.memory.search import _insert_chunks
            _insert_chunks(conn, "alice", "conversation", "1", ["test chunk"], None)
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()

        with patch("istota.memory.search.enable_vec_extension", return_value=False):
            result = cmd_stats(args)

        assert result["status"] == "ok"
        assert result["total_chunks"] == 1


class TestConversationTokenEnvVar:
    def test_search_includes_channel_when_token_set(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)

        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            from istota.memory.search import _insert_chunks
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

        with patch("istota.memory.search._search_vec", return_value=[]):
            result = cmd_search(args)

        assert result["status"] == "ok"
        contents = [r["content"] for r in result["results"]]
        assert any("GraphQL" in c for c in contents)

    def test_search_no_channel_without_token(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)

        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            from istota.memory.search import _insert_chunks
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

        with patch("istota.memory.search._search_vec", return_value=[]):
            result = cmd_search(args)

        assert result["count"] == 0

    def test_stats_includes_channel_when_token_set(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)

        with patch("istota.memory.search.ensure_vec_table", return_value=False):
            from istota.memory.search import _insert_chunks
            _insert_chunks(conn, "alice", "conversation", "1", ["user chunk"], None)
            _insert_chunks(conn, "channel:room456", "channel_memory", "f1", ["channel chunk"], None)
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_CONVERSATION_TOKEN", "room456")

        args = MagicMock()

        with patch("istota.memory.search.enable_vec_extension", return_value=False):
            result = cmd_stats(args)

        assert result["status"] == "ok"
        assert result["total_chunks"] == 2


class TestMain:
    def test_main_search(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        with patch("istota.memory.search._search_vec", return_value=[]):
            main(["search", "hello"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"

    def test_main_stats(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        with patch("istota.memory.search.enable_vec_extension", return_value=False):
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
        args = parser.parse_args(["facts", "--subject", "bob", "--predicate", "knows"])
        assert args.subject == "bob"
        assert args.predicate == "knows"

    def test_facts_with_as_of(self):
        parser = build_parser()
        args = parser.parse_args(["facts", "--as-of", "2025-06-15"])
        assert args.as_of == "2025-06-15"

    def test_timeline_command(self):
        parser = build_parser()
        args = parser.parse_args(["timeline", "bob"])
        assert args.command == "timeline"
        assert args.subject == "bob"

    def test_add_fact_command(self):
        parser = build_parser()
        args = parser.parse_args(["add-fact", "bob", "works_at", "acme", "--from", "2025-06-01"])
        assert args.command == "add-fact"
        assert args.subject == "bob"
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
        from istota.memory.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        add_fact(conn, "alice", "bob", "knows", "python")
        add_fact(conn, "alice", "bob", "works_at", "acme")
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
        from istota.memory.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        add_fact(conn, "alice", "bob", "knows", "python")
        add_fact(conn, "alice", "istota", "uses_tech", "svelte")
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.subject = "bob"
        args.predicate = None
        args.as_of = None

        result = cmd_facts(args)
        assert result["count"] == 1
        assert result["facts"][0]["subject"] == "bob"

    def test_facts_as_of(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)
        from istota.memory.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        add_fact(conn, "alice", "bob", "works_at", "acme", valid_from="2025-01-01")
        add_fact(conn, "alice", "bob", "works_at", "globex", valid_from="2026-04-01")
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
        from istota.memory.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        add_fact(conn, "alice", "bob", "works_at", "acme", valid_from="2025-01-01")
        add_fact(conn, "alice", "bob", "works_at", "globex", valid_from="2026-04-01")
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()
        args.subject = "bob"

        result = cmd_timeline(args)
        assert result["status"] == "ok"
        assert result["count"] == 2
        assert result["subject"] == "bob"

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
        # Direct path: the deferred dir must be absent, or this test
        # silently exercises the sandbox queue instead.
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)

        args = MagicMock()
        args.subject = "bob"
        args.predicate = "knows"
        args.object = "python"
        args.valid_from = None

        result = cmd_add_fact(args)
        assert result["status"] == "ok"
        assert "fact_id" in result

    def test_add_duplicate(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)
        from istota.memory.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        add_fact(conn, "alice", "bob", "knows", "python")
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        # Direct path: the deferred dir must be absent, or this test
        # silently exercises the sandbox queue instead.
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)

        args = MagicMock()
        args.subject = "bob"
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
        from istota.memory.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        fact_id = add_fact(conn, "alice", "bob", "knows", "python")
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        # Direct path: the deferred dir must be absent, or this test
        # silently exercises the sandbox queue instead.
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)

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
        # Direct path: the deferred dir must be absent, or this test
        # silently exercises the sandbox queue instead.
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)

        args = MagicMock()
        args.fact_id = 9999
        args.ended = None

        result = cmd_invalidate_fact(args)
        assert result["status"] == "error"


class TestCmdDeleteFact:
    def test_delete(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)
        from istota.memory.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        fact_id = add_fact(conn, "alice", "bob", "knows", "python")
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        # Direct path: the deferred dir must be absent, or this test
        # silently exercises the sandbox queue instead.
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)

        args = MagicMock()
        args.fact_id = fact_id

        result = cmd_delete_fact(args)
        assert result["status"] == "ok"

    def test_delete_nonexistent(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        # Direct path: the deferred dir must be absent, or this test
        # silently exercises the sandbox queue instead.
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)

        args = MagicMock()
        args.fact_id = 9999

        result = cmd_delete_fact(args)
        assert result["status"] == "error"


class TestKgSkillDeferred:
    """Sandbox mode: the KG write verbs queue an op instead of hitting the DB.

    This is the only way a sandboxed agent can write a knowledge-graph
    fact at all — the framework DB is not bound into the sandbox, and
    its directory is masked with an empty tmpfs. Every test here deletes
    ISTOTA_DB_PATH, so any fall-through to the direct path would exit(1)
    in `_get_conn` rather than quietly passing.

    The op envelopes are asserted whole because `_process_deferred_kg_ops`
    in scheduler_deferred.py reads these exact keys; a rename on either
    side is a silently dropped write.
    """

    @pytest.fixture
    def sandbox_env(self, tmp_path, monkeypatch):
        monkeypatch.delenv("ISTOTA_DB_PATH", raising=False)
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
        monkeypatch.setenv("ISTOTA_TASK_ID", "42")
        return tmp_path

    def test_add_fact_deferred(self, sandbox_env):
        args = MagicMock()
        args.subject = "bob"
        args.predicate = "knows"
        args.object = "python"
        args.valid_from = None

        result = cmd_add_fact(args)
        assert result == {"status": "ok", "deferred": True}

        ops = json.loads((sandbox_env / "task_42_kg_ops.json").read_text())
        assert ops == [{
            "op": "add_fact",
            "subject": "bob",
            "predicate": "knows",
            "object": "python",
            "valid_from": None,
            "source_type": "user_stated",
        }]

    def test_invalidate_deferred(self, sandbox_env):
        args = MagicMock()
        args.fact_id = 7
        args.ended = "2026-04-08"

        result = cmd_invalidate_fact(args)
        assert result == {"status": "ok", "deferred": True}

        ops = json.loads((sandbox_env / "task_42_kg_ops.json").read_text())
        assert ops == [{"op": "invalidate", "fact_id": 7, "ended": "2026-04-08"}]

    def test_delete_fact_deferred(self, sandbox_env):
        args = MagicMock()
        args.fact_id = 7

        result = cmd_delete_fact(args)
        assert result == {"status": "ok", "deferred": True}

        ops = json.loads((sandbox_env / "task_42_kg_ops.json").read_text())
        assert ops == [{"op": "delete", "fact_id": 7}]

    def test_ops_accumulate_in_one_file(self, sandbox_env):
        add_args = MagicMock()
        add_args.subject = "bob"
        add_args.predicate = "knows"
        add_args.object = "python"
        add_args.valid_from = None
        cmd_add_fact(add_args)

        del_args = MagicMock()
        del_args.fact_id = 7
        cmd_delete_fact(del_args)

        ops = json.loads((sandbox_env / "task_42_kg_ops.json").read_text())
        assert [op["op"] for op in ops] == ["add_fact", "delete"]

    def test_no_task_id_falls_through_to_direct_write(self, tmp_path, monkeypatch):
        """A deferred dir without a task id can't name an op file, so the
        write must go direct rather than being dropped."""
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
        monkeypatch.delenv("ISTOTA_TASK_ID", raising=False)

        args = MagicMock()
        args.subject = "bob"
        args.predicate = "knows"
        args.object = "python"
        args.valid_from = None

        result = cmd_add_fact(args)
        assert "fact_id" in result
        assert not list(tmp_path.glob("*_kg_ops.json"))

    def test_deferred_ops_replay_through_the_scheduler(self, tmp_path, monkeypatch):
        """The file the CLI writes is the file the scheduler reads.

        The envelope assertions above are hand-copied literals, so a
        rename applied to both the producer and those literals still
        leaves the consumer dropping writes — silently, since the
        unknown-op branch of `_process_deferred_kg_ops` only warns. This
        runs the real seam for all three verbs: the CLI writes, the
        scheduler replays, and the effects have to land in the DB.

        The env claims a different user than the task owns, because
        `_process_deferred_kg_ops` takes identity from the task and never
        from the file. Making both "alice" would leave that undefended.
        """
        from istota import db
        from istota.config import Config
        from istota.memory.knowledge_graph import (
            add_fact as kg_add_fact, ensure_table, get_current_facts,
        )
        from istota.scheduler_deferred import _process_deferred_kg_ops

        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="test", user_id="alice")
            task = db.get_task(conn, task_id)
            ensure_table(conn)
            doomed_id = kg_add_fact(conn, "alice", "bob", "uses_tech", "svelte")
            conn.commit()

        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        # The CLI half, exactly as a sandboxed agent reaches it.
        monkeypatch.delenv("ISTOTA_DB_PATH", raising=False)
        monkeypatch.setenv("ISTOTA_USER_ID", "mallory")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(user_temp))
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))

        add_args = MagicMock()
        add_args.subject = "bob"
        add_args.predicate = "knows"
        add_args.object = "python"
        add_args.valid_from = None
        assert cmd_add_fact(add_args) == {"status": "ok", "deferred": True}

        del_args = MagicMock()
        del_args.fact_id = doomed_id
        assert cmd_delete_fact(del_args) == {"status": "ok", "deferred": True}

        assert _process_deferred_kg_ops(Config(db_path=db_path), task, user_temp) == 2

        with db.get_db(db_path) as conn:
            facts = get_current_facts(conn, "alice", subject="bob")
            # Identity comes from the task, never from the CLI's env.
            assert get_current_facts(conn, "mallory", subject="bob") == []
        assert [(f.predicate, f.object) for f in facts] == [("knows", "python")]
        assert facts[0].source_task_id == task_id

        # Replayed ops are consumed, so a retry can't double-apply them.
        assert not (user_temp / f"task_{task_id}_kg_ops.json").exists()

    def test_deferred_invalidate_replays_through_the_scheduler(self, tmp_path, monkeypatch):
        """`invalidate` carries `ended` across the seam, not just `fact_id`."""
        from istota import db
        from istota.config import Config
        from istota.memory.knowledge_graph import (
            add_fact as kg_add_fact, ensure_table, get_fact,
        )
        from istota.scheduler_deferred import _process_deferred_kg_ops

        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="test", user_id="alice")
            task = db.get_task(conn, task_id)
            ensure_table(conn)
            fact_id = kg_add_fact(conn, "alice", "bob", "lives_in", "lisbon")
            conn.commit()

        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        monkeypatch.delenv("ISTOTA_DB_PATH", raising=False)
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(user_temp))
        monkeypatch.setenv("ISTOTA_TASK_ID", str(task_id))

        args = MagicMock()
        args.fact_id = fact_id
        args.ended = "2026-04-08"
        assert cmd_invalidate_fact(args) == {"status": "ok", "deferred": True}

        assert _process_deferred_kg_ops(Config(db_path=db_path), task, user_temp) == 1

        with db.get_db(db_path) as conn:
            fact = get_fact(conn, fact_id)
        assert fact.valid_until == "2026-04-08"


class TestStatsIncludesKG:
    def test_stats_includes_knowledge_facts(self, tmp_path, monkeypatch):
        db_path = tmp_path / "test.db"
        conn = _init_db(db_path)
        from istota.memory.knowledge_graph import ensure_table, add_fact
        ensure_table(conn)
        add_fact(conn, "alice", "bob", "knows", "python")
        add_fact(conn, "alice", "bob", "works_at", "acme")
        conn.commit()
        conn.close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        args = MagicMock()

        with patch("istota.memory.search.enable_vec_extension", return_value=False):
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
        # Direct path: the deferred dir must be absent, or this test
        # silently exercises the sandbox queue instead.
        monkeypatch.delenv("ISTOTA_DEFERRED_DIR", raising=False)

        main(["add-fact", "bob", "knows", "python"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
        assert "fact_id" in output

    def test_main_timeline(self, tmp_path, monkeypatch, capsys):
        db_path = tmp_path / "test.db"
        _init_db(db_path).close()

        monkeypatch.setenv("ISTOTA_DB_PATH", str(db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        main(["timeline", "bob"])

        output = json.loads(capsys.readouterr().out)
        assert output["status"] == "ok"
