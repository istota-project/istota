"""The trigger path reproduces the poll path's filters, including the @mention gate.

**The poll-path version of the first test passes whether or not the bug is
present**, which is why this file exists at all. `poll_talk_conversations`
builds `conv_types` from the room listing, so a group room is known to be a
group room by the time the results block reads it. A drain driven by a
signaling event has no listing in hand: the tempting signature is
`poll_one_conversation(config, token)`, and the results block's
`conv_types.get(token, 1)` then answers **1** — a DM — for every room.
`_get_participants` returns `[]` immediately for type 1, `_is_multi_user` is
False, and the `is_bot_mentioned` gate is skipped entirely. Every message in
every group room the bot sits in becomes a task, from any `config.users`
member, with no @mention required.

So the room context is a required keyword argument with no default anywhere on
the path, and these tests drive the drain rather than the poll to see it.

The second test is the other half: the two paths must produce the *same* task
for the same message, not merely agree about whether to make one. The
`[Room participants: …]` prefix and the stripped mention are both built from
the same context, so a path that carried a wrong `conv_type` and still passed
the gate would produce a task with neither.
"""

import pytest
from unittest.mock import AsyncMock, patch

from istota import db
from istota.config import (
    Config,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.transport.talk import inbound as poller
from istota.transport.talk.inbound import (
    catch_up_conversation,
    poll_one_conversation,
    poll_talk_conversations,
)


@pytest.fixture(autouse=True)
def _reset_poller_caches():
    poller._participant_cache.clear()
    poller._conversation_cache = None
    poller._dm_token_cache.clear()
    poller._last_full_sweep = None
    yield
    poller._participant_cache.clear()
    poller._conversation_cache = None
    poller._dm_token_cache.clear()
    poller._last_full_sweep = None


@pytest.fixture
def make_config(tmp_path):
    def _make():
        path = tmp_path / "test.db"
        if not path.exists():
            db.init_db(path)
        config = Config()
        config.db_path = path
        config.temp_dir = tmp_path / "temp"
        config.temp_dir.mkdir(exist_ok=True)
        config.skills_dir = tmp_path / "skills"
        config.skills_dir.mkdir(exist_ok=True)
        config.talk = TalkConfig(enabled=True, bot_username="istota")
        config.nextcloud = NextcloudConfig(
            url="https://nc.test", username="istota", app_password="pass",
        )
        config.users = {"alice": UserConfig(), "bob": UserConfig()}
        config.scheduler = SchedulerConfig()
        return config
    return _make


_PARTICIPANTS = [
    {"actorId": "alice", "actorType": "users", "displayName": "Alice"},
    {"actorId": "bob", "actorType": "users", "displayName": "Bob"},
    {"actorId": "istota", "actorType": "users", "displayName": "Istota"},
]


def _msg(msg_id=101, message="Just chatting", params=None):
    return {
        "id": msg_id,
        "actorId": "alice",
        "actorType": "users",
        "message": message,
        "messageType": "comment",
        "messageParameters": params if params is not None else {},
        "timestamp": 1700000000,
    }


def _mention_msg(msg_id=102):
    return _msg(
        msg_id=msg_id,
        message="{mention-user0} check my calendar",
        params={
            "mention-user0": {"type": "user", "id": "istota", "name": "Istota"},
        },
    )


def _client(messages):
    client = AsyncMock()
    client.list_conversations = AsyncMock(return_value=[{
        "token": "group1", "type": 2, "displayName": "team",
        "lastMessage": {"id": messages[-1]["id"]},
    }])
    client.poll_messages = AsyncMock(return_value=messages)
    client.get_participants = AsyncMock(return_value=_PARTICIPANTS)
    client.fetch_chat_history = AsyncMock(return_value=[])
    client.get_latest_message_id = AsyncMock(return_value=messages[-1]["id"])
    client.send_message = AsyncMock()
    return client


class TestTheTriggerPathKeepsTheMentionGate:
    """A non-mentioned message in a three-participant room creates no task."""

    @pytest.mark.asyncio
    async def test_the_drain_does_not_ingest_an_unmentioned_message(
        self, make_config,
    ):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "group1", 50)

        with patch(
            "istota.transport.talk.inbound.get_talk_client",
            return_value=_client([_msg()]),
        ):
            created = await poll_one_conversation(
                config, "group1", conv_type=2, display_name="team",
            )

        assert created == []
        with db.get_db(config.db_path) as conn:
            assert db.list_tasks(conn, user_id="alice") == []
            # The cursor still advanced: the message was read and dropped by a
            # filter, exactly as the poll path does it.
            assert db.get_talk_poll_state(conn, "group1") == 101

    @pytest.mark.asyncio
    async def test_the_drain_ingests_a_mentioned_message(self, make_config):
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "group1", 50)

        with patch(
            "istota.transport.talk.inbound.get_talk_client",
            return_value=_client([_mention_msg()]),
        ):
            created = await poll_one_conversation(
                config, "group1", conv_type=2, display_name="team",
            )

        assert len(created) == 1

    @pytest.mark.parametrize(
        "entry_point", [poll_one_conversation, catch_up_conversation],
        ids=["trigger", "catch_up"],
    )
    def test_the_room_context_has_no_default_anywhere_on_the_path(
        self, entry_point,
    ):
        """Both arguments are keyword-only and required, on **both** entry points.

        The defect this file is about is a *default*, so the signature is the
        thing to pin: a `conv_type: int = 1` added later would make every test
        above pass while reopening the hole. Catch-up is the second door onto
        the same `_process_poll_results`, and it runs on every join and every
        reconnect, so a default there is the same hole opened more often.
        """
        import inspect

        sig = inspect.signature(entry_point)
        for name in ("conv_type", "display_name"):
            param = sig.parameters[name]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
            assert param.default is inspect.Parameter.empty, name

    @pytest.mark.asyncio
    async def test_the_trigger_fetch_does_not_hold_a_nextcloud_worker(
        self, make_config,
    ):
        """`timeout=0`, not `scheduler.talk_poll_timeout`.

        The coalescing rule makes the *second* fetch of a burst the normal
        case, and it reads from a cursor the first fetch just advanced past
        every message — so with the poller's 30-second timeout Nextcloud holds
        it for the full 30, on the one drain task every other dirty room queues
        behind. That is a held request per burst, against the design's claim
        that none remains in the steady state.
        """
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "group1", 50)
        client = _client([_msg()])
        client.poll_messages = AsyncMock(return_value=[])

        with patch(
            "istota.transport.talk.inbound.get_talk_client", return_value=client,
        ):
            await poll_one_conversation(
                config, "group1", conv_type=2, display_name="team",
            )

        assert client.poll_messages.await_args.kwargs["timeout"] == 0
        assert client.poll_messages.await_args.kwargs[
            "last_known_message_id"
        ] == 50

    @pytest.mark.asyncio
    async def test_a_failed_fetch_raises_rather_than_reading_as_empty(
        self, make_config,
    ):
        """`_poll_single_conversation` swallows every fetch error and returns
        `(token, [])`, which is right for the poll path and is the drain's
        whole error contract defeated: a read timeout would arrive as a clean
        empty result, the dirty bit would be cleared and the room stranded with
        nothing saying so."""
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "group1", 50)
        client = _client([_msg()])
        client.poll_messages = AsyncMock(side_effect=RuntimeError("read timeout"))

        with patch(
            "istota.transport.talk.inbound.get_talk_client", return_value=client,
        ):
            with pytest.raises(RuntimeError, match="read timeout"):
                await poll_one_conversation(
                    config, "group1", conv_type=2, display_name="team",
                )


class TestTheTwoPathsProduceTheSameTask:
    """The prefix and the stripped mention are identical either way."""

    async def _via_poll(self, config, message):
        with patch(
            "istota.transport.talk.inbound.get_talk_client",
            return_value=_client([message]),
        ):
            return await poll_talk_conversations(config)

    async def _via_trigger(self, config, message):
        with patch(
            "istota.transport.talk.inbound.get_talk_client",
            return_value=_client([message]),
        ):
            return await poll_one_conversation(
                config, "group1", conv_type=2, display_name="team",
            )

    @pytest.mark.asyncio
    async def test_the_prompt_and_the_room_name_match(self, make_config, tmp_path):
        poll_config = make_config()
        with db.get_db(poll_config.db_path) as conn:
            db.set_talk_poll_state(conn, "group1", 50)
        poll_ids = await self._via_poll(poll_config, _mention_msg())

        # A second database, so the two runs cannot see each other's rows.
        poller._participant_cache.clear()
        poller._conversation_cache = None
        trigger_path = tmp_path / "trigger.db"
        db.init_db(trigger_path)
        trigger_config = make_config()
        trigger_config.db_path = trigger_path
        with db.get_db(trigger_path) as conn:
            db.set_talk_poll_state(conn, "group1", 50)
        trigger_ids = await self._via_trigger(trigger_config, _mention_msg())

        assert len(poll_ids) == 1 and len(trigger_ids) == 1
        with db.get_db(poll_config.db_path) as conn:
            polled = db.get_task(conn, poll_ids[0])
            polled_room = db.get_room(conn, "group1")
        with db.get_db(trigger_path) as conn:
            triggered = db.get_task(conn, trigger_ids[0])
            triggered_room = db.get_room(conn, "group1")

        assert polled.prompt.startswith("[Room participants: ")
        assert "istota" not in polled.prompt.lower()
        assert triggered.prompt == polled.prompt
        assert triggered.is_group_chat == polled.is_group_chat is True
        assert triggered.user_id == polled.user_id == "alice"
        # `channel_name` is what names the room in the registry, so that is
        # where a context field dropped on the trigger path shows up.
        assert triggered_room.name == polled_room.name == "team"


class TestPayloadDirectIngestion:
    """Stage 5: the relayed comment goes in without a fetch.

    The claim this has to earn is that ingestion is byte-for-byte the fetch
    path's — `ingest_relayed_comments` hands the comments to the same
    `_process_poll_results`, so the filter chain, the confirmation handling,
    the cursor advance and the atomicity guarantee are not reimplemented. What
    it adds is the two things a fetch got for free: the cursor as a filter, and
    ordering.
    """

    @pytest.mark.asyncio
    async def test_the_ingested_row_matches_the_one_a_fetch_produces(
        self, make_config, tmp_path,
    ):
        """The spec's stated verification for this stage.

        Same comment, two paths, two databases — compared on the task row and
        on the cursor, which is everything the results block writes.
        """
        from istota.transport.talk.inbound import ingest_relayed_comments

        comment = _mention_msg(msg_id=102)

        def _run_path(db_name, coro_factory):
            config = make_config()
            config.db_path = tmp_path / db_name
            db.init_db(config.db_path)
            with db.get_db(config.db_path) as conn:
                db.set_talk_poll_state(conn, "group1", 50)
            return config, coro_factory(config)

        fetched_config, fetch_coro = _run_path(
            "fetched.db",
            lambda c: poll_one_conversation(
                c, "group1", conv_type=2, display_name="team",
            ),
        )
        with patch(
            "istota.transport.talk.inbound.get_talk_client",
            return_value=_client([comment]),
        ):
            fetched = await fetch_coro

        direct_config, direct_coro = _run_path(
            "direct.db",
            lambda c: ingest_relayed_comments(
                c, "group1", [comment], conv_type=2, display_name="team",
            ),
        )
        with patch(
            "istota.transport.talk.inbound.get_talk_client",
            return_value=_client([comment]),
        ):
            direct = await direct_coro

        assert len(fetched) == 1 and len(direct) == 1

        def _row(config, task_id):
            with db.get_db(config.db_path) as conn:
                row = dict(conn.execute(
                    "SELECT * FROM tasks WHERE id = ?", (task_id,),
                ).fetchone())
            # Identity and wall-clock columns differ between any two runs and
            # say nothing about the path that produced them.
            for column in ("id", "created_at", "updated_at"):
                row.pop(column, None)
            return row

        assert _row(direct_config, direct[0]) == _row(fetched_config, fetched[0])

        with db.get_db(direct_config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "group1") == 102
        with db.get_db(fetched_config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "group1") == 102

    @pytest.mark.asyncio
    async def test_a_comment_at_or_below_the_cursor_is_dropped(
        self, make_config,
    ):
        """The reconnect race, which the fetch path cannot have.

        A fetch passes the cursor to the *server*, so a message already
        processed never comes back. A relayed comment arrived unasked, and one
        queued moments before a reconnect has already been ingested by
        `catch_up_conversation` reading forward from the same cursor.
        `ingest_message` dedups on the Talk id, but `dispatch_command`,
        `handle_confirmation_reply` with its ack post and
        `confirmations.cancel_for_conversation` do not.
        """
        from istota.transport.talk.inbound import ingest_relayed_comments

        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "group1", 102)

        with patch(
            "istota.transport.talk.inbound.get_talk_client",
            return_value=_client([_mention_msg(msg_id=102)]),
        ):
            created = await ingest_relayed_comments(
                config, "group1", [_mention_msg(msg_id=102)],
                conv_type=2, display_name="team",
            )

        assert created == []
        with db.get_db(config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "group1") == 102

    @pytest.mark.asyncio
    async def test_a_batch_is_ingested_oldest_first(self, make_config):
        """A batch is accumulated across events, so nothing else orders it.

        **The cursor is not what this is about, and asserting on it is how the
        first version of this test came back green with the sort deleted.**
        `set_talk_poll_state` is MAX-guarded, so an out-of-order walk still
        leaves the highest id behind and every count still adds up. What
        reverses is the *tasks*: `_process_poll_results` creates one per
        message in the order it walks them, so the model is handed a
        conversation running backwards. That is what the assertion has to name.
        """
        from istota.transport.talk.inbound import ingest_relayed_comments

        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "group1", 50)

        older = _mention_msg(msg_id=101)
        older["message"] = "{mention-user0} first thing"
        newer = _mention_msg(msg_id=103)
        newer["message"] = "{mention-user0} second thing"

        with patch(
            "istota.transport.talk.inbound.get_talk_client",
            return_value=_client([_mention_msg(msg_id=103)]),
        ):
            created = await ingest_relayed_comments(
                config, "group1", [newer, older],
                conv_type=2, display_name="team",
            )

        assert len(created) == 2
        with db.get_db(config.db_path) as conn:
            prompts = [
                conn.execute(
                    "SELECT prompt FROM tasks WHERE id = ?", (task_id,),
                ).fetchone()[0]
                for task_id in created
            ]
            assert db.get_talk_poll_state(conn, "group1") == 103

        assert "first thing" in prompts[0], prompts
        assert "second thing" in prompts[1], prompts

    @pytest.mark.asyncio
    async def test_the_mention_gate_still_applies(self, make_config):
        """The whole point of reusing `_process_poll_results`.

        A payload path that skipped the gate would ingest every message in
        every group room the bot sits in — the same hole this file's first test
        is about, reopened one layer up.
        """
        from istota.transport.talk.inbound import ingest_relayed_comments

        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.set_talk_poll_state(conn, "group1", 50)

        with patch(
            "istota.transport.talk.inbound.get_talk_client",
            return_value=_client([_msg(msg_id=101)]),
        ):
            created = await ingest_relayed_comments(
                config, "group1", [_msg(msg_id=101)],
                conv_type=2, display_name="team",
            )

        assert created == []
        # Read and dropped by a filter, exactly as the fetch path does it.
        with db.get_db(config.db_path) as conn:
            assert db.get_talk_poll_state(conn, "group1") == 101

    def test_the_room_context_has_no_default_here_either(self):
        """The third door onto `_process_poll_results`, held to the same rule."""
        import inspect

        from istota.transport.talk.inbound import ingest_relayed_comments

        sig = inspect.signature(ingest_relayed_comments)
        for name in ("conv_type", "display_name"):
            param = sig.parameters[name]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
            assert param.default is inspect.Parameter.empty, name
