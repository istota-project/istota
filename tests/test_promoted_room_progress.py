"""ISSUE-400 — the progress surface must resolve the Talk room, not assume it.

On a *promoted* room — one that started on web and was later bound to Talk — the
canonical token is the `web-…` token and the Talk room lives only in the room's
`talk` binding. Final delivery already resolves that binding, via
`resolve_delivery_plan`. The three progress-surface sites did not: they handed
`task.conversation_token` straight to the Talk API, which answered 404. The ack
never appeared, so `ack_msg_id` was None and every progress edit that depends on
it no-opped for the rest of the task.

Driven from the sites themselves rather than from `talk_channel_for_task`, which
was never the broken part — it resolves a promoted room correctly and has done
since rooms landed. What was missing was the call to it.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from istota import db
from istota.config import (
    Config,
    EmailConfig,
    NextcloudConfig,
    SchedulerConfig,
    TalkConfig,
    UserConfig,
)
from istota.consumers import TalkEventSubscriber
from istota.events import TaskEvent
from istota.scheduler import edit_talk_message, process_one_task

# The canonical token of a web-origin room, and the Talk ref its binding holds.
# They differ; that divergence is the whole bug.
WEB_TOKEN = "web-testuser-aaaabbbbcccc"
TALK_REF = "RealTalkRoom"


@pytest.fixture
def config(tmp_path):
    db_path = tmp_path / "istota.db"
    db.init_db(db_path)
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    return Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(
            url="https://nc.example.com", username="istota", app_password="s",
        ),
        talk=TalkConfig(enabled=True, bot_username="istota"),
        email=EmailConfig(enabled=False),
        scheduler=SchedulerConfig(),
        nextcloud_mount_path=mount,
        temp_dir=tmp_path / "temp",
        users={"testuser": UserConfig(display_name="Alice")},
    )


def _promoted_room(config):
    """A web room later bound to Talk: canonical token is the web one."""
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, WEB_TOKEN, "testuser", origin="web")
        db.add_room_binding(conn, WEB_TOKEN, "web", WEB_TOKEN)
        db.add_room_binding(conn, WEB_TOKEN, "talk", TALK_REF)


def _plain_talk_room(config, token="plainroom"):
    """An ordinary Talk room: canonical token *is* the Talk ref."""
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, token, "testuser", origin="talk")
        db.add_room_binding(conn, token, "talk", token)
    return token


def _queue_talk_task(config, token):
    with db.get_db(config.db_path) as conn:
        return db.create_task(
            conn, prompt="what is on the list?", user_id="testuser",
            source_type="talk", conversation_token=token,
        )


def _ack_call(mock_post, task_id):
    """The ack post, identified by its reference_id rather than by position."""
    for call in mock_post.call_args_list:
        if call.kwargs.get("reference_id") == f"istota:task:{task_id}:ack":
            return call
    return None


def _talk_task(token):
    return db.Task(
        id=99, prompt="x", user_id="testuser", source_type="talk",
        status="running", conversation_token=token,
    )


def _ev(kind, payload, task_id=99, seq=1):
    return TaskEvent(
        task_id=task_id, seq=seq, kind=kind, payload=payload,
        created_at="2026-09-02T00:00:00.000Z",
    )


# ---------------------------------------------------------------------------
# Site 1 — the ack (scheduler.process_one_task)
# ---------------------------------------------------------------------------


class TestTheAck:
    @patch("istota.scheduler.run_coro", return_value=414)
    @patch("istota.scheduler.post_result_to_talk")
    def test_the_ack_goes_to_the_talk_binding_not_the_canonical_token(
        self, mock_post, mock_run_coro, config,
    ):
        _promoted_room(config)
        task_id = _queue_talk_task(config, WEB_TOKEN)
        with patch(
            "istota.scheduler.execute_task", return_value=(True, "done", None, None),
        ):
            process_one_task(config)

        ack = _ack_call(mock_post, task_id)
        assert ack is not None, "no ack was posted"
        assert ack.kwargs.get("target_token") == TALK_REF

    @patch("istota.scheduler.run_coro", return_value=414)
    @patch("istota.scheduler.post_result_to_talk")
    def test_an_ordinary_talk_room_still_gets_its_own_token(
        self, mock_post, mock_run_coro, config,
    ):
        """The common path, where canonical token and Talk ref are the same
        string. It worked by accident before; it must still work on purpose."""
        token = _plain_talk_room(config)
        task_id = _queue_talk_task(config, token)
        with patch(
            "istota.scheduler.execute_task", return_value=(True, "done", None, None),
        ):
            process_one_task(config)

        ack = _ack_call(mock_post, task_id)
        assert ack is not None, "no ack was posted"
        assert ack.kwargs.get("target_token") == token

    @patch("istota.scheduler.run_coro", return_value=414)
    @patch("istota.scheduler.post_result_to_talk")
    def test_a_room_with_no_registry_entry_falls_back_to_its_token(
        self, mock_post, mock_run_coro, config,
    ):
        """A pre-rooms deployment, or a DM never registered as a room. The
        resolver's last rung returns the token, so the ack is unchanged."""
        task_id = _queue_talk_task(config, "unregistered")
        with patch(
            "istota.scheduler.execute_task", return_value=(True, "done", None, None),
        ):
            process_one_task(config)

        ack = _ack_call(mock_post, task_id)
        assert ack is not None, "no ack was posted"
        assert ack.kwargs.get("target_token") == "unregistered"

    @patch("istota.scheduler.run_coro", return_value=414)
    @patch("istota.scheduler.post_result_to_talk")
    def test_a_talk_task_with_no_token_still_posts_no_ack(
        self, mock_post, mock_run_coro, config,
    ):
        """The half of the guard the resolution did not replace. `task.
        conversation_token` stays in front of it so a resolver rung that answers
        from somewhere else — `tasks.talk_delivery_token` is returned absolutely
        — cannot start posting acks for a task that had nowhere to put one."""
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="x", user_id="testuser", source_type="talk",
                conversation_token=None, talk_delivery_token=TALK_REF,
            )
        with patch(
            "istota.scheduler.execute_task", return_value=(True, "done", None, None),
        ):
            process_one_task(config)

        assert _ack_call(mock_post, task_id) is None

    @patch("istota.scheduler.run_coro", return_value=414)
    @patch("istota.scheduler.post_result_to_talk")
    def test_the_subscriber_carries_the_resolved_token(
        self, mock_post, mock_run_coro, config,
    ):
        """The ack id is useless to the subscriber without the room it lives in
        — every later edit addresses the same message on the same surface."""
        _promoted_room(config)
        _queue_talk_task(config, WEB_TOKEN)
        seen = {}
        real_ctor = TalkEventSubscriber

        def _capture(cfg, task, ack_msg_id, *args, **kwargs):
            seen["target_token"] = kwargs.get("target_token")
            return real_ctor(cfg, task, ack_msg_id, *args, **kwargs)

        with patch(
            "istota.scheduler.execute_task", return_value=(True, "done", None, None),
        ), patch("istota.scheduler.TalkEventSubscriber", side_effect=_capture):
            process_one_task(config)

        assert seen.get("target_token") == TALK_REF


# ---------------------------------------------------------------------------
# Site 2 — edit_talk_message
# ---------------------------------------------------------------------------


class TestEditTargetToken:
    @pytest.mark.asyncio
    async def test_edit_honours_the_target_token(self, config):
        with patch("istota.transport.talk.get_talk_client") as mock_get:
            mock_get.return_value.edit_message = AsyncMock()
            ok = await edit_talk_message(
                config, _talk_task(WEB_TOKEN), 42, "Updated", target_token=TALK_REF,
            )
        assert ok is True
        mock_get.return_value.edit_message.assert_awaited_once_with(
            TALK_REF, 42, "Updated",
        )

    @pytest.mark.asyncio
    async def test_without_a_target_token_the_conversation_token_stands(self, config):
        """Callers that never had a room to resolve keep the old behaviour."""
        with patch("istota.transport.talk.get_talk_client") as mock_get:
            mock_get.return_value.edit_message = AsyncMock()
            ok = await edit_talk_message(config, _talk_task("plainroom"), 42, "Updated")
        assert ok is True
        mock_get.return_value.edit_message.assert_awaited_once_with(
            "plainroom", 42, "Updated",
        )

    @pytest.mark.asyncio
    async def test_a_target_token_survives_an_empty_conversation_token(self, config):
        """The guard is about where the edit is going, not about what the task
        row happens to store."""
        with patch("istota.transport.talk.get_talk_client") as mock_get:
            mock_get.return_value.edit_message = AsyncMock()
            ok = await edit_talk_message(
                config, _talk_task(""), 42, "Updated", target_token=TALK_REF,
            )
        assert ok is True
        mock_get.return_value.edit_message.assert_awaited_once_with(
            TALK_REF, 42, "Updated",
        )


# ---------------------------------------------------------------------------
# Site 3 — the subscriber's own posts and edits
# ---------------------------------------------------------------------------


class TestSubscriberRouting:
    """Asserted at the Talk client, not at the keyword.

    Checking that `target_token=…` was forwarded proves nothing on its own: the
    absence-of-a-kwarg version passes identically against the pre-fix code, and
    would keep passing if the plumbing below it were deleted. These run the
    coroutines for real, down through `edit_talk_message` /
    `post_result_to_talk` and the transport, and assert the room the Talk API
    was actually addressed with.
    """

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_progress_edits_address_the_bound_talk_room(self, mock_run, config):
        with patch("istota.transport.talk.get_talk_client") as mock_get:
            mock_get.return_value.edit_message = AsyncMock()
            sub = TalkEventSubscriber(
                config, _talk_task(WEB_TOKEN), ack_msg_id=100, target_token=TALK_REF,
            )
            sub.on_event(_ev("tool_start", {"description": "Reading x.txt"}))
        assert mock_get.return_value.edit_message.await_args.args[0] == TALK_REF

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_the_terminal_summary_edit_too(self, mock_run, config):
        with patch("istota.transport.talk.get_talk_client") as mock_get:
            mock_get.return_value.edit_message = AsyncMock()
            sub = TalkEventSubscriber(
                config, _talk_task(WEB_TOKEN), ack_msg_id=100, target_token=TALK_REF,
            )
            sub.on_event(_ev("result", {"text": "done"}))
        assert mock_get.return_value.edit_message.await_args.args[0] == TALK_REF

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_streamed_text_posts_and_edits_on_the_bound_talk_room(
        self, mock_run, config,
    ):
        with patch("istota.transport.talk.get_talk_client") as mock_get:
            client = mock_get.return_value
            client.send_message = AsyncMock(
                return_value={"ocs": {"data": {"id": 555}}},
            )
            client.edit_message = AsyncMock()
            sub = TalkEventSubscriber(
                config, _talk_task(WEB_TOKEN), ack_msg_id=100, target_token=TALK_REF,
            )
            sub.on_event(_ev("progress_text", {"text": "Working on it"}, seq=1))
            assert client.send_message.await_args.args[0] == TALK_REF
            sub.on_event(_ev("progress_text", {"text": "Still working"}, seq=2))
            assert client.edit_message.await_args.args[0] == TALK_REF
            assert client.edit_message.await_args.args[1] == 555

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_a_failed_ack_still_streams_text_to_the_bound_room(
        self, mock_run, config,
    ):
        """The one path where a wrong room makes a stray message rather than a
        no-op edit: the ack post failed, so there is no message to edit and the
        first `progress_text` posts a new one."""
        with patch("istota.transport.talk.get_talk_client") as mock_get:
            client = mock_get.return_value
            client.send_message = AsyncMock(
                return_value={"ocs": {"data": {"id": 777}}},
            )
            sub = TalkEventSubscriber(
                config, _talk_task(WEB_TOKEN), ack_msg_id=None, target_token=TALK_REF,
            )
            sub.on_event(_ev("progress_text", {"text": "Working on it"}, seq=1))
        assert client.send_message.await_args.args[0] == TALK_REF

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_no_target_token_still_addresses_the_conversation_token(
        self, mock_run, config,
    ):
        """A caller with no room to resolve keeps the old destination — stated
        as the room the API was addressed with, not as an absent keyword."""
        with patch("istota.transport.talk.get_talk_client") as mock_get:
            mock_get.return_value.edit_message = AsyncMock()
            sub = TalkEventSubscriber(config, _talk_task("plainroom"), ack_msg_id=100)
            sub.on_event(_ev("tool_start", {"description": "Reading x.txt"}))
        assert mock_get.return_value.edit_message.await_args.args[0] == "plainroom"
