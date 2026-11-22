"""A skill or command task's result is a program's output, not a model's answer.

ISSUE-987. Such a result has no recall value, and the feeds poller's JSON
envelope, indexed every few minutes for every user, had grown to a third of the
memory index. Every writer that turns a completed task into memory leaves these
tasks out, and `purge_program_output` removes what earlier releases indexed.

A prompt task is the control throughout, so an assertion cannot pass because a
writer saw nothing at all.
"""

from __future__ import annotations

import json
from argparse import Namespace
from unittest.mock import patch

import pytest

from istota import db
from istota.config import MemorySearchConfig
from istota.memory import search
from istota.memory.search import index_conversation, reindex_all
from istota.memory.sleep_cycle import gather_channel_data, gather_day_data
from istota.scheduler import process_one_task
from istota.skills.memory_search import main as memory_search_main
from tests import test_shared_room_restriction as harness

config = harness.config
_bwrap_flag_cache = harness._bwrap_flag_cache

ENVELOPE = json.dumps({
    "status": "ok", "polled": 2, "new_entries": 0,
    "feeds": [{"feed_id": 1, "url": "https://feeds.example.com/a.xml", "new_entries": 0}],
})
PROMPT_ANSWER = "promptanswerwombat"


def _no_vec():
    return (
        patch("istota.memory.search.ensure_vec_table", return_value=False),
        patch("istota.memory.search.enable_vec_extension", return_value=False),
    )


def _done(conn, *, prompt="", result=ENVELOPE, token=None, **kwargs) -> int:
    task_id = db.create_task(
        conn, prompt=prompt, user_id="alice", source_type="scheduled",
        conversation_token=token, **kwargs,
    )
    db.update_task_status(conn, task_id, "completed", result=result)
    return task_id


class TestTheAutoIndex:
    def _indexed(self, config, **task) -> list[str]:
        config.memory_search = MemorySearchConfig(enabled=True, auto_index_conversations=True)
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, user_id="alice", source_type="scheduled", **task)
        vec, ext = _no_vec()
        with patch("istota.scheduler._execute_skill_task", return_value=(True, ENVELOPE)), \
                patch("istota.scheduler._execute_command_task", return_value=(True, ENVELOPE)), \
                patch("istota.scheduler.execute_task",
                      return_value=(True, PROMPT_ANSWER, None, None)), \
                patch("istota.scheduler.run_coro", return_value=True), vec, ext:
            process_one_task(config)
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "completed"
            return [row[0] for row in conn.execute("SELECT content FROM memory_chunks")]

    def test_a_skill_task_indexes_nothing(self, config):
        assert self._indexed(config, prompt="", skill="feeds",
                             skill_args='["run-scheduled"]') == []

    def test_a_command_task_indexes_nothing(self, config):
        assert self._indexed(config, prompt="", command="echo hi") == []

    def test_control_a_prompt_job_is_indexed(self, config):
        content = self._indexed(config, prompt="summarise the news")
        assert any(PROMPT_ANSWER in c for c in content)


class TestTheOtherWriters:
    def test_a_reindex_adds_only_the_prompt_task(self, config):
        vec, ext = _no_vec()
        with db.get_db(config.db_path) as conn, vec, ext:
            _done(conn, skill="feeds")
            _done(conn, command="echo hi")
            _done(conn, prompt="what is new", result=PROMPT_ANSWER)
            reindex_all(conn, config, "alice", lookback_days=1)
            content = [row[0] for row in conn.execute("SELECT content FROM memory_chunks")]
        assert any(PROMPT_ANSWER in c for c in content)
        assert not any('"polled"' in c for c in content)

    def test_the_sleep_cycles_leave_a_skill_task_out(self, config):
        with db.get_db(config.db_path) as conn:
            token = db.create_web_chat_room(conn, "alice", "Room").token
            _done(conn, skill="feeds", token=token)
            day = gather_day_data(config, conn, "alice", 24, None)
            assert '"polled"' not in day
            assert gather_channel_data(config, conn, token, 24, None) == ""
            assert token not in db.get_active_channel_tokens(conn, "2000-01-01")

            _done(conn, prompt="what is new", result=PROMPT_ANSWER, token=token)
            assert PROMPT_ANSWER in gather_day_data(config, conn, "alice", 24, None)
            assert PROMPT_ANSWER in gather_channel_data(config, conn, token, 24, None)
            assert token in db.get_active_channel_tokens(conn, "2000-01-01")

    def test_the_skill_verb_refuses_a_skill_task(self, config, monkeypatch, capsys):
        with db.get_db(config.db_path) as conn:
            skill_task = _done(conn, skill="feeds")
            prompt_task = _done(conn, prompt="what is new", result=PROMPT_ANSWER)
            running = db.create_task(conn, prompt="p", user_id="alice", source_type="web")
            db.update_task_status(conn, running, "running")
        monkeypatch.setenv("ISTOTA_DB_PATH", str(config.db_path))
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        monkeypatch.setenv("ISTOTA_TASK_ID", str(running))
        vec, ext = _no_vec()
        with vec, ext:
            with pytest.raises(SystemExit):
                memory_search_main(["index", "conversation", str(skill_task)])
            refused = json.loads(capsys.readouterr().out)
            memory_search_main(["index", "conversation", str(prompt_task)])
            accepted = json.loads(capsys.readouterr().out)
        assert refused["reason"] == "program_output"
        assert accepted["status"] == "ok"
        assert accepted["chunks_inserted"] > 0


class _Index:
    """A populated index with a plain table standing in for the vec table."""

    def __init__(self, conn):
        self.conn = conn
        conn.execute("CREATE TABLE memory_chunks_vec (chunk_id INTEGER, embedding BLOB)")

    def add(self, user_id, task_id, prompt, result) -> None:
        with patch("istota.memory.search.ensure_vec_table", return_value=False), \
                patch("istota.memory.search.enable_vec_extension", return_value=False):
            index_conversation(self.conn, user_id, task_id, prompt, result)
        self.conn.execute(
            "INSERT INTO memory_chunks_vec (chunk_id, embedding) "
            "SELECT id, x'00' FROM memory_chunks WHERE source_id = ? AND user_id = ?",
            (str(task_id), user_id),
        )
        self.conn.commit()

    def sources(self) -> set[tuple[str, str]]:
        return {(r[0], r[1]) for r in self.conn.execute(
            "SELECT DISTINCT user_id, source_id FROM memory_chunks")}

    def vec_ids(self) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT chunk_id FROM memory_chunks_vec")}

    def chunk_ids(self) -> set[int]:
        return {r[0] for r in self.conn.execute("SELECT id FROM memory_chunks")}

    def fts_hits(self, term: str) -> int:
        return self.conn.execute(
            "SELECT COUNT(*) FROM memory_chunks_fts WHERE memory_chunks_fts MATCH ?",
            (term,),
        ).fetchone()[0]


def _long_envelope() -> str:
    feeds = [{"feed_id": i, "url": f"https://feeds.example.com/{i}.xml",
              "new_entries": 0, "not_modified": True, "error": None} for i in range(200)]
    return json.dumps({"status": "ok", "polled": 200, "new_entries": 0, "feeds": feeds})


@pytest.fixture
def index(config):
    with db.get_db(config.db_path) as conn:
        skill_task = _done(conn, skill="feeds")
        command_task = _done(conn, command="echo hi")
        prompt_task = _done(conn, prompt="what is new", result=PROMPT_ANSWER)
        json_answer = _done(conn, prompt="", result=ENVELOPE)
        idx = _Index(conn)
        idx.add("alice", skill_task, "", ENVELOPE)
        idx.add("channel:room1", skill_task, "", ENVELOPE)
        idx.add("alice", command_task, "", "hi from a shell")
        idx.add("alice", prompt_task, "what is new", PROMPT_ANSWER)
        # A model answer that happens to look like the envelope: its row says
        # it is a model's, so the content arm must leave it alone.
        idx.add("alice", json_answer, "", ENVELOPE.replace("ok", "okay"))
        # Task rows long gone, found only by content, first chunk and continuations.
        idx.add("alice", 900001, "", _long_envelope())
        idx.add("alice", 900002, "", "an ordinary answer whose row has gone")
        yield {"conn": conn, "idx": idx, "skill": skill_task, "command": command_task,
               "prompt": prompt_task, "json_answer": json_answer}


class TestThePurge:
    def test_the_long_envelope_really_spans_chunks(self, index):
        count = index["conn"].execute(
            "SELECT COUNT(*) FROM memory_chunks WHERE source_id = '900001'").fetchone()[0]
        assert count > 1

    def test_a_dry_run_counts_and_deletes_nothing(self, index):
        conn, idx = index["conn"], index["idx"]
        before = (idx.chunk_ids(), idx.vec_ids())
        report = search.purge_program_output(conn, apply=False)
        assert (idx.chunk_ids(), idx.vec_ids()) == before
        assert report["applied"] is False
        assert report["deleted_chunks"] == 0
        assert report["arms"]["program_task"]["sources"] == 3
        assert report["arms"]["feeds_envelope"]["sources"] == 3
        assert report["sources"] == 4
        assert report["chunks"] > report["sources"]
        assert report["arms"]["feeds_envelope"]["samples"]
        for sample in report["arms"]["feeds_envelope"]["samples"]:
            assert sample["head"].startswith("Bot: {")
            assert "\n" not in sample["head"]

    def test_apply_removes_program_output_with_its_vectors_and_fts(self, index):
        conn, idx = index["conn"], index["idx"]
        assert idx.fts_hits("polled") > 0
        with patch("istota.memory.search.enable_vec_extension", return_value=True):
            report = search.purge_program_output(conn, apply=True)
        assert idx.sources() == {
            ("alice", str(index["prompt"])),
            ("alice", str(index["json_answer"])),
            ("alice", "900002"),
        }
        assert idx.vec_ids() == idx.chunk_ids()
        assert report["deleted_chunks"] == report["chunks"]
        assert idx.fts_hits("hi") == 0
        assert idx.fts_hits(PROMPT_ANSWER) == 1

    def test_a_second_run_finds_nothing(self, index):
        search.purge_program_output(index["conn"], apply=True)
        again = search.purge_program_output(index["conn"], apply=True)
        assert again["sources"] == 0
        assert again["deleted_chunks"] == 0

    def test_the_command_defaults_to_a_dry_run(self, config, index, capsys, monkeypatch):
        from istota import cli

        monkeypatch.setattr(cli, "load_config", lambda _path=None: config)
        before = index["idx"].chunk_ids()
        assert cli.cmd_memory_purge_program_output(
            Namespace(config=None, apply=False, samples=2)) == 0
        out = capsys.readouterr().out
        assert "dry run" in out
        assert "feeds_envelope" in out
        assert index["idx"].chunk_ids() == before

        assert cli.cmd_memory_purge_program_output(
            Namespace(config=None, apply=True, samples=2)) == 0
        assert "deleted" in capsys.readouterr().out
        assert len(index["idx"].chunk_ids()) < len(before)


def test_the_delete_helper_batches_past_the_variable_limit(config):
    with db.get_db(config.db_path) as conn:
        conn.execute("CREATE TABLE memory_chunks_vec (chunk_id INTEGER, embedding BLOB)")
        rows = [("alice", "conversation", str(i), 0, f"c{i}", f"h{i}") for i in range(1200)]
        conn.executemany(
            "INSERT INTO memory_chunks (user_id, source_type, source_id, chunk_index, "
            "content, content_hash) VALUES (?, ?, ?, ?, ?, ?)", rows)
        conn.execute("INSERT INTO memory_chunks_vec SELECT id, x'00' FROM memory_chunks")
        ids = [r[0] for r in conn.execute("SELECT id FROM memory_chunks")]
        with patch("istota.memory.search.enable_vec_extension", return_value=True):
            search.delete_chunk_ids(conn, ids)
        assert conn.execute("SELECT COUNT(*) FROM memory_chunks").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM memory_chunks_vec").fetchone()[0] == 0
