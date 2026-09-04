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

    def test_the_room_context_has_no_default_anywhere_on_the_path(self):
        """Both arguments are keyword-only and required.

        The defect this file is about is a *default*, so the signature is the
        thing to pin: a `conv_type: int = 1` added later would make every test
        above pass while reopening the hole.
        """
        import inspect

        sig = inspect.signature(poll_one_conversation)
        for name in ("conv_type", "display_name"):
            param = sig.parameters[name]
            assert param.kind is inspect.Parameter.KEYWORD_ONLY, name
            assert param.default is inspect.Parameter.empty, name


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
