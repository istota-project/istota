"""Tests for the !search read path (memory-search overhaul).

Exercises `commands._search_memory` + `cmd_search` against a real memory index
(real `index_conversation` / `index_file` / `search`) rather than mocks, so the
scope-classification and retention-fallback behaviour is verified end to end.
"""

import pytest

from istota import db
from istota.commands import CommandContext, _search_memory, cmd_search
from istota.config import Config, NextcloudConfig, TalkConfig, UserConfig
from istota.memory import search as memsearch


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path, tmp_path):
    cfg = Config()
    cfg.db_path = db_path
    cfg.temp_dir = tmp_path / "temp"
    cfg.temp_dir.mkdir(exist_ok=True)
    cfg.talk = TalkConfig(enabled=True, bot_username="istota")
    cfg.nextcloud = NextcloudConfig(url="https://nc.test", username="istota", app_password="pw")
    cfg.users = {"alice": UserConfig()}
    cfg.nextcloud_mount_path = tmp_path / "mount"
    return cfg


def _ctx(config, conn, args, *, user_id="alice", token="room1", surface="web"):
    # web surface: skips the Talk full-text API (no network), still exercises
    # the full memory read path + scope filter.
    return CommandContext(
        config=config, conn=conn, user_id=user_id,
        conversation_token=token, args=args, surface=surface, registry=None,
    )


def _seed_room(conn, token="room1", user_id="alice"):
    db.register_room(conn, token, user_id, origin="web", name=token)


class TestSearchMemoryClassification:
    def test_default_scope_returns_conversation_memory_and_channel(self, config, db_path):
        with db.get_db(db_path) as conn:
            _seed_room(conn)
            # (a) a conversation indexed under BOTH user and channel namespaces
            t1 = db.create_task(
                conn, prompt="the falcon migration timeline", user_id="alice",
                conversation_token="room1", source_type="web",
            )
            db.update_task_status(conn, t1, "completed", result="falcon details here")
            memsearch.index_conversation(conn, "alice", t1, "the falcon migration timeline", "falcon details here")
            memsearch.index_conversation(conn, "channel:room1", t1, "the falcon migration timeline", "falcon details here")
            # (b) a personal memory file
            memsearch.index_file(conn, "alice", "/Users/alice/USER.md", "falcon is my project codename", "memory_file")
            # (c) a channel-memory chunk under the channel namespace
            memsearch.index_file(conn, "channel:room1", "/Channels/room1/CHANNEL.md", "falcon planning notes", "channel_memory")

        with db.get_db(db_path) as conn:
            rows = _search_memory(config, conn, "alice", "falcon", conversation_token="room1")

        source_types = {r["source_type"] for r in rows}
        assert "conversation" in source_types
        assert "memory_file" in source_types
        assert "channel_memory" in source_types
        # conversation row is room-bound; memory rows carry is_memory
        conv = next(r for r in rows if r["source_type"] == "conversation")
        assert conv["conversation_token"] == "room1"
        assert conv["is_memory"] is False
        mem = next(r for r in rows if r["source_type"] == "memory_file")
        assert mem["is_memory"] is True
        assert mem["conversation_token"] is None
        chan = next(r for r in rows if r["source_type"] == "channel_memory")
        assert chan["is_memory"] is True
        assert chan["conversation_token"] == "room1"

    def test_conversation_deduped_by_task_id_across_namespaces(self, config, db_path):
        with db.get_db(db_path) as conn:
            _seed_room(conn)
            t1 = db.create_task(
                conn, prompt="falcon dedup case", user_id="alice",
                conversation_token="room1", source_type="web",
            )
            db.update_task_status(conn, t1, "completed", result="falcon body")
            memsearch.index_conversation(conn, "alice", t1, "falcon dedup case", "falcon body")
            memsearch.index_conversation(conn, "channel:room1", t1, "falcon dedup case", "falcon body")

        with db.get_db(db_path) as conn:
            rows = _search_memory(config, conn, "alice", "falcon", conversation_token="room1")

        conv_rows = [r for r in rows if r["source_type"] == "conversation" and r["task_id"] == t1]
        assert len(conv_rows) == 1

    def test_conversation_scope_survives_task_retention(self, config, db_path):
        """The reproduced regression: task row aged out, conversation chunk still
        indexed — room scope recovered from the messages store."""
        with db.get_db(db_path) as conn:
            _seed_room(conn)
            t1 = db.create_task(
                conn, prompt="falcon retention", user_id="alice",
                conversation_token="room1", source_type="web",
            )
            db.update_task_status(conn, t1, "completed", result="falcon retained body")
            memsearch.index_conversation(conn, "alice", t1, "falcon retention", "falcon retained body")
            # durable messages-store turn for this task
            db.store_turn_message(conn, "room1", role="assistant", body="falcon retained body", task_id=t1, origin_surface="web")
            # simulate retention cleanup of the tasks row
            conn.execute("DELETE FROM tasks WHERE id = ?", (t1,))

        with db.get_db(db_path) as conn:
            assert db.get_task(conn, t1) is None
            rows = _search_memory(config, conn, "alice", "falcon", conversation_token="room1")

        conv = next(r for r in rows if r["source_type"] == "conversation")
        assert conv["task_id"] == t1
        assert conv["conversation_token"] == "room1"


class TestCmdSearchScope:
    @pytest.mark.asyncio
    async def test_default_scope_includes_memory_and_conversation(self, config, db_path):
        with db.get_db(db_path) as conn:
            _seed_room(conn)
            t1 = db.create_task(conn, prompt="falcon convo", user_id="alice",
                                conversation_token="room1", source_type="web")
            db.update_task_status(conn, t1, "completed", result="falcon convo body")
            memsearch.index_conversation(conn, "channel:room1", t1, "falcon convo", "falcon convo body")
            memsearch.index_file(conn, "alice", "/Users/alice/USER.md", "falcon personal note", "memory_file")

        with db.get_db(db_path) as conn:
            out = await cmd_search(_ctx(config, conn, "falcon"))

        assert "No results" not in out
        assert "falcon" in out.lower()

    @pytest.mark.asyncio
    async def test_memories_flag_returns_memory_rows_in_room(self, config, db_path):
        with db.get_db(db_path) as conn:
            _seed_room(conn)
            memsearch.index_file(conn, "alice", "/Users/alice/USER.md", "falcon memory only", "memory_file")

        with db.get_db(db_path) as conn:
            out = await cmd_search(_ctx(config, conn, "--memories falcon"))

        assert "No results" not in out
        assert "falcon" in out.lower()

    @pytest.mark.asyncio
    async def test_or_fallback_fires_on_empty_scoped_result(self, config, db_path):
        """Forgiveness must key on the *scoped* result being empty, not the global
        strict pass. A strict AND match in another room (reachable via the user
        namespace) must not suppress a loose in-room match (Mulder MEDIUM)."""
        with db.get_db(db_path) as conn:
            _seed_room(conn)
            _seed_room(conn, token="room2")
            # room1: a loose match only (matches "falcon", not "helicopter").
            t1 = db.create_task(conn, prompt="falcons are nesting", user_id="alice",
                                conversation_token="room1", source_type="web")
            db.update_task_status(conn, t1, "completed", result="the falcons are nesting on the ridge")
            memsearch.index_conversation(conn, "channel:room1", t1, "falcons are nesting", "the falcons are nesting on the ridge")
            memsearch.index_conversation(conn, "alice", t1, "falcons are nesting", "the falcons are nesting on the ridge")
            # room2: a STRICT match for both terms, indexed under the user namespace
            # too (so the strict pass finds it and would suppress the fallback).
            t2 = db.create_task(conn, prompt="falcon helicopter drill", user_id="alice",
                                conversation_token="room2", source_type="web")
            db.update_task_status(conn, t2, "completed", result="the falcon helicopter drill went well")
            memsearch.index_conversation(conn, "alice", t2, "falcon helicopter drill", "the falcon helicopter drill went well")
            memsearch.index_conversation(conn, "channel:room2", t2, "falcon helicopter drill", "the falcon helicopter drill went well")

        with db.get_db(db_path) as conn:
            out = await cmd_search(_ctx(config, conn, "falcon helicopter"))

        # The in-room loose match surfaces despite the out-of-room strict match.
        assert "No results" not in out
        assert "falcons are nesting" in out.lower()

    @pytest.mark.asyncio
    async def test_specific_room_excludes_memory_rows(self, config, db_path):
        with db.get_db(db_path) as conn:
            _seed_room(conn)
            _seed_room(conn, token="other")
            # a conversation in the OTHER room
            t1 = db.create_task(conn, prompt="falcon in other", user_id="alice",
                                conversation_token="other", source_type="web")
            db.update_task_status(conn, t1, "completed", result="falcon other body")
            memsearch.index_conversation(conn, "channel:room1", t1, "falcon in other", "falcon other body")
            # a personal memory hit (should be excluded under --room)
            memsearch.index_file(conn, "alice", "/Users/alice/USER.md", "falcon personal secret", "memory_file")

        with db.get_db(db_path) as conn:
            out = await cmd_search(_ctx(config, conn, "--room other falcon"))

        # personal memory excluded from a specific-room search
        assert "personal secret" not in out


class TestSearchStructuredData:
    """The structured `search_results` payload for rich stream surfaces."""

    @pytest.mark.asyncio
    async def test_result_data_shape(self, config, db_path):
        with db.get_db(db_path) as conn:
            _seed_room(conn)
            t1 = db.create_task(conn, prompt="falcon payload", user_id="alice",
                                conversation_token="room1", source_type="web")
            db.update_task_status(conn, t1, "completed", result="falcon payload body")
            memsearch.index_conversation(conn, "channel:room1", t1, "falcon payload", "falcon payload body")
            memsearch.index_file(conn, "alice", "/Users/alice/USER.md", "falcon note", "memory_file")

        with db.get_db(db_path) as conn:
            ctx = _ctx(config, conn, "falcon")
            await cmd_search(ctx)

        data = ctx.result_data
        assert data is not None
        assert data["kind"] == "search_results"
        assert data["query"] == "falcon"
        assert isinstance(data["results"], list) and data["results"]
        for r in data["results"]:
            assert set(r) >= {
                "source_type", "summary", "date", "room_token", "room_name",
                "task_id", "talk_message_id", "talk_link",
            }
        conv = next(r for r in data["results"] if r["source_type"] == "conversation")
        assert conv["task_id"] == t1
        assert conv["room_token"] == "room1"
        # No talk_message_id (web task) → no talk_link.
        assert conv["talk_message_id"] is None
        assert conv["talk_link"] is None

    @pytest.mark.asyncio
    async def test_talk_link_only_when_message_id_present(self, config, db_path):
        with db.get_db(db_path) as conn:
            _seed_room(conn)
            t1 = db.create_task(conn, prompt="falcon linked", user_id="alice",
                                conversation_token="room1", source_type="talk",
                                talk_message_id=555)
            db.update_task_status(conn, t1, "completed", result="falcon linked body")
            memsearch.index_conversation(conn, "channel:room1", t1, "falcon linked", "falcon linked body")

        with db.get_db(db_path) as conn:
            ctx = _ctx(config, conn, "falcon")
            await cmd_search(ctx)

        conv = next(r for r in ctx.result_data["results"] if r["source_type"] == "conversation")
        assert conv["talk_message_id"] == 555
        assert conv["talk_link"] == "https://nc.test/call/room1#message_555"

    @pytest.mark.asyncio
    async def test_no_results_returns_empty_card(self, config, db_path):
        with db.get_db(db_path) as conn:
            _seed_room(conn)
            ctx = _ctx(config, conn, "nonexistenttermxyz")
            out = await cmd_search(ctx)

        assert "No results" in out
        assert ctx.result_data is not None
        assert ctx.result_data["kind"] == "search_results"
        assert ctx.result_data["results"] == []

    @pytest.mark.asyncio
    async def test_dispatch_threads_data(self, config, db_path):
        from istota import commands
        with db.get_db(db_path) as conn:
            _seed_room(conn)
            memsearch.index_file(conn, "alice", "/Users/alice/USER.md", "falcon dispatched", "memory_file")

        with db.get_db(db_path) as conn:
            result = await commands.dispatch(
                config, "alice", "room1", "!search falcon",
                surface="web", conn=conn,
            )
        assert result.handled
        assert result.data is not None
        assert result.data["kind"] == "search_results"


class TestGetMessageRoomForTask:
    def test_returns_room_for_stored_message(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web", name="room1")
            t1 = db.create_task(conn, prompt="hi", user_id="alice",
                                conversation_token="room1", source_type="web")
            db.store_turn_message(conn, "room1", role="assistant", body="hey", task_id=t1, origin_surface="web")
            assert db.get_message_room_for_task(conn, t1) == "room1"

    def test_returns_none_when_absent(self, db_path):
        with db.get_db(db_path) as conn:
            assert db.get_message_room_for_task(conn, 999999) is None
