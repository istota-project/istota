"""Stage 4 + 5 of the !steer spec: the `!steer` command + transcript/event surfacing."""

import pytest

from istota import db
from istota.commands import (
    CommandContext,
    cmd_help,
    cmd_steer,
    dispatch,
    parse_command,
)
from istota.config import (
    BrainConfig,
    Config,
    NativeBrainConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def make_config(db_path):
    def _make(brain_kind="native"):
        config = Config()
        config.db_path = db_path
        config.talk = TalkConfig(enabled=True, bot_username="istota")
        config.users = {"alice": UserConfig(), "bob": UserConfig()}
        config.scheduler = SchedulerConfig()
        if brain_kind == "native":
            config.brain = BrainConfig(kind="native", native=NativeBrainConfig())
        else:
            config.brain = BrainConfig(kind=brain_kind)
        return config

    return _make


def _ctx(config, conn, *, user_id="alice", token="room1", args="", surface="talk"):
    return CommandContext(
        config=config, conn=conn, user_id=user_id,
        conversation_token=token, args=args, surface=surface,
    )


def _running_task(conn, *, user_id="alice", token="room1", source_type="talk",
                  status="running"):
    tid = db.create_task(
        conn, prompt="original prompt", user_id=user_id,
        source_type=source_type, conversation_token=token,
    )
    db.update_task_status(conn, tid, status)
    return tid


class TestTargetResolution:
    async def test_empty_args_returns_usage(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _running_task(conn)
            out = await cmd_steer(_ctx(config, conn, args=""))
        assert "Usage" in out

    async def test_no_running_task(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            out = await cmd_steer(_ctx(config, conn, args="do the thing"))
        assert "No running task" in out

    async def test_room_scoped(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _running_task(conn, token="room2")  # running, but different room
            out = await cmd_steer(_ctx(config, conn, token="room1", args="hey"))
        assert "No running task" in out

    async def test_own_task_only(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _running_task(conn, user_id="bob")  # bob's task in room1
            out = await cmd_steer(_ctx(config, conn, user_id="alice", args="hey"))
        assert "No running task" in out

    async def test_pending_confirmation_excluded(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _running_task(conn, status="pending_confirmation")
            out = await cmd_steer(_ctx(config, conn, args="hey"))
        assert "No running task" in out


class TestSteerability:
    async def test_claude_code_refuses_and_writes_nothing(self, make_config):
        config = make_config(brain_kind="claude_code")
        with db.get_db(config.db_path) as conn:
            tid = _running_task(conn)
            out = await cmd_steer(_ctx(config, conn, args="focus on auth"))
            assert "headless" in out.lower()
            assert db.count_pending_steers(conn, tid) == 0

    async def test_tmux_refuses_not_yet(self, make_config):
        config = make_config(brain_kind="tmux_claude")
        with db.get_db(config.db_path) as conn:
            tid = _running_task(conn)
            out = await cmd_steer(_ctx(config, conn, args="focus on auth"))
            assert "isn't steerable yet" in out or "not steerable" in out.lower()
            assert db.count_pending_steers(conn, tid) == 0

    async def test_native_accepts(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            tid = _running_task(conn)
            out = await cmd_steer(_ctx(config, conn, args="focus on auth"))
            assert f"#{tid}" in out
            assert "Steering" in out
            assert db.count_pending_steers(conn, tid) == 1
            steers = db.claim_pending_steers(conn, tid)
            assert steers[0].text == "focus on auth"
            assert steers[0].source == "talk"
            assert steers[0].user_id == "alice"


class TestDepthCap:
    async def test_refuses_over_cap(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            tid = _running_task(conn)
            for i in range(10):
                db.add_task_steer(conn, tid, f"steer {i}", "alice", "talk")
            out = await cmd_steer(_ctx(config, conn, args="one too many"))
            assert "Too many pending steers" in out
            # No 11th written.
            assert db.count_pending_steers(conn, tid) == 10


class TestTranscriptAndEvents:
    async def test_writes_transcript_user_row(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk", name="Room 1")
            _running_task(conn)
            await cmd_steer(_ctx(config, conn, args="check the db layer"))
            rows = conn.execute(
                "SELECT role, body, task_id, origin_surface FROM messages "
                "WHERE room_token = ?",
                ("room1",),
            ).fetchall()
            steer_rows = [r for r in rows if r["body"] == "check the db layer"]
            assert len(steer_rows) == 1
            assert steer_rows[0]["role"] == "user"
            assert steer_rows[0]["task_id"] is None  # display-only, not re-paired
            assert steer_rows[0]["origin_surface"] == "talk"

    async def test_transcript_row_names_who_steered(self, make_config):
        """A `task_id IS NULL` row has no task to recover an identity from, so
        the author column is the only record of who typed it.

        Steering is own-task-only, but a room is shared: bob steers bob's task
        in a room alice also reads, and without the author alice sees it as her
        own words.
        """
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="talk", name="Room 1")
            _running_task(conn, user_id="bob")
            await cmd_steer(
                _ctx(config, conn, user_id="bob", args="check the db layer"),
            )
            row = conn.execute(
                "SELECT author_user_id, author_label FROM messages "
                "WHERE room_token = ? AND body = ?",
                ("room1", "check the db layer"),
            ).fetchone()
            assert (row["author_user_id"], row["author_label"]) == ("bob", None)

    async def test_returns_the_recorded_row_for_the_web_client_to_stamp(self, make_config):
        """The stored row's id rides back on `result_data` (ISSUE-300).

        A steer row carries no task id, so `msg_id` is the only key the web
        client's `appendStreamedRow` can dedup on. Without the stamp the room
        stream's echo of this row draws a second bubble for one steer — live
        only, since the inline result is never recorded, so a reload disagrees
        with what the user just watched. `body` goes with it because the stored
        row holds the note alone while the client drew the whole `!steer …`
        line it was given.
        """
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "room1", "alice", origin="web", name="Room 1")
            _running_task(conn, source_type="web")
            ctx = _ctx(config, conn, args="check the db layer", surface="web")
            await cmd_steer(ctx)
            row = conn.execute(
                "SELECT id FROM messages WHERE room_token = ? AND body = ?",
                ("room1", "check the db layer"),
            ).fetchone()
            assert ctx.result_data == {
                "kind": "steer_recorded",
                "user_msg_id": row["id"],
                "body": "check the db layer",
            }

    async def test_no_recorded_row_means_nothing_to_stamp(self, make_config):
        """No room, no transcript row, so no id — and the client must not be
        handed one to stamp onto a row that never echoes."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            _running_task(conn)  # no room registered
            ctx = _ctx(config, conn, args="hey")
            await cmd_steer(ctx)
            assert ctx.result_data is None

    async def test_no_room_skips_transcript_but_still_steers(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            tid = _running_task(conn)  # no room registered
            out = await cmd_steer(_ctx(config, conn, args="hey"))
            assert "Steering" in out
            assert db.count_pending_steers(conn, tid) == 1

    async def test_emits_progress_event_frame(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            tid = _running_task(conn)
            await cmd_steer(_ctx(config, conn, args="pivot to the api"))
            events = db.get_task_events(conn, tid)
            steer_events = [
                e for e in events
                if e["kind"] == "progress_text" and "Steering" in e["payload"].get("text", "")
            ]
            assert len(steer_events) == 1
            assert "pivot to the api" in steer_events[0]["payload"]["text"]


class TestHiddenAlias:
    def test_inject_parses(self):
        assert parse_command("!inject go left") == ("inject", "go left")

    async def test_inject_routes_to_steer(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            tid = _running_task(conn)
        # dispatch opens its own conn; no registry -> stream-ish return.
        result = await dispatch(
            config, "alice", "room1", "!inject focus here", surface="web",
        )
        assert result.handled is True
        assert "Steering" in (result.text or "")
        with db.get_db(config.db_path) as conn:
            assert db.count_pending_steers(conn, tid) == 1

    async def test_inject_hidden_from_help(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            out = await cmd_help(_ctx(config, conn))
        assert "!steer" in out
        assert "!inject" not in out
