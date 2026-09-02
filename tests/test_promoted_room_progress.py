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

Both room shapes come from `tests/support/rooms.py` and the Talk seam is the
`fake_talk` double, so this file no longer carries its own spelling of either.
The double is what makes the two shapes distinguishable at all: it refuses a
token that is not a live `talk` `surface_ref`, exactly as Nextcloud does, where
the `AsyncMock` this file used to patch in accepted anything. Because the
product swallows that refusal — `edit_talk_message` returns False,
`TalkTransport.deliver` returns None — every assertion below names the room the
API was addressed with, or the return value, and never the absence of a raise.
`TestEditTargetToken`'s first case is the in-file control for that.
"""

import asyncio
from unittest.mock import patch

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

from .support.rooms import plain_talk_room, promoted_room


@pytest.fixture
def config(tmp_path, db_path):
    # `db_path` rather than a database of this file's own, because `fake_talk`
    # resolves bindings against that same fixture — a second path would leave
    # the double reading an empty `room_bindings` and refusing every token here.
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


@pytest.fixture
def promoted(config):
    """A web room later bound to Talk: canonical token is the web one."""
    with db.get_db(config.db_path) as conn:
        shape = promoted_room(conn, "testuser")
    assert shape.diverges, "the promoted shape must not collapse to one token"
    return shape


@pytest.fixture
def plain(config):
    """An ordinary Talk room: the canonical token *is* the Talk ref."""
    with db.get_db(config.db_path) as conn:
        return plain_talk_room(conn, "testuser")


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


def _addressed(fake_talk):
    """(method, token) per attempted call, in order — refused ones included."""
    return [(c.method, c.token) for c in fake_talk.calls]


def _record_sent_ids(fake_talk):
    """The message ids the double hands back, in order.

    `TalkCall` records what went into a call, not what came out of it, and one
    assertion below is that the edit addresses the message the *send* created
    rather than the ack. Wrapping is how that id is read without reaching for
    the double's private counter.
    """
    sent = []
    inner = fake_talk.send_message

    async def _send(*args, **kwargs):
        response = await inner(*args, **kwargs)
        sent.append(response["ocs"]["data"]["id"])
        return response

    fake_talk.send_message = _send
    return sent


# ---------------------------------------------------------------------------
# Site 1 — the ack (scheduler.process_one_task)
# ---------------------------------------------------------------------------


class TestTheAck:
    """Asserted at `post_result_to_talk`'s keyword, which is patched out here.

    Unlike the two classes below, these never reach the Talk seam: `run_coro`
    is patched to a fixed id, so no coroutine runs and the double would record
    nothing at all. What is under test is the scheduler's choice of target
    token, which is the argument it hands down.
    """

    @patch("istota.scheduler.run_coro", return_value=414)
    @patch("istota.scheduler.post_result_to_talk")
    def test_the_ack_goes_to_the_talk_binding_not_the_canonical_token(
        self, mock_post, mock_run_coro, config, promoted,
    ):
        task_id = _queue_talk_task(config, promoted.canonical)
        with patch(
            "istota.scheduler.execute_task", return_value=(True, "done", None, None),
        ):
            process_one_task(config)

        ack = _ack_call(mock_post, task_id)
        assert ack is not None, "no ack was posted"
        assert ack.kwargs.get("target_token") == promoted.talk_ref

    @patch("istota.scheduler.run_coro", return_value=414)
    @patch("istota.scheduler.post_result_to_talk")
    def test_an_ordinary_talk_room_still_gets_its_own_token(
        self, mock_post, mock_run_coro, config, plain,
    ):
        """The common path, where canonical token and Talk ref are the same
        string. It worked by accident before; it must still work on purpose."""
        task_id = _queue_talk_task(config, plain.canonical)
        with patch(
            "istota.scheduler.execute_task", return_value=(True, "done", None, None),
        ):
            process_one_task(config)

        ack = _ack_call(mock_post, task_id)
        assert ack is not None, "no ack was posted"
        assert ack.kwargs.get("target_token") == plain.talk_ref

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
        self, mock_post, mock_run_coro, config, promoted,
    ):
        """The half of the guard the resolution did not replace. `task.
        conversation_token` stays in front of it so a resolver rung that answers
        from somewhere else — `tasks.talk_delivery_token` is returned absolutely
        — cannot start posting acks for a task that had nowhere to put one."""
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="x", user_id="testuser", source_type="talk",
                conversation_token=None, talk_delivery_token=promoted.talk_ref,
            )
        with patch(
            "istota.scheduler.execute_task", return_value=(True, "done", None, None),
        ):
            process_one_task(config)

        assert _ack_call(mock_post, task_id) is None

    @patch("istota.scheduler.run_coro", return_value=414)
    @patch("istota.scheduler.post_result_to_talk")
    def test_the_subscriber_carries_the_resolved_token(
        self, mock_post, mock_run_coro, config, promoted,
    ):
        """The ack id is useless to the subscriber without the room it lives in
        — every later edit addresses the same message on the same surface."""
        _queue_talk_task(config, promoted.canonical)
        seen = {}
        real_ctor = TalkEventSubscriber

        def _capture(cfg, task, ack_msg_id, *args, **kwargs):
            seen["target_token"] = kwargs.get("target_token")
            return real_ctor(cfg, task, ack_msg_id, *args, **kwargs)

        with patch(
            "istota.scheduler.execute_task", return_value=(True, "done", None, None),
        ), patch("istota.scheduler.TalkEventSubscriber", side_effect=_capture):
            process_one_task(config)

        assert seen.get("target_token") == promoted.talk_ref


# ---------------------------------------------------------------------------
# Site 2 — edit_talk_message
# ---------------------------------------------------------------------------


class TestEditTargetToken:
    @pytest.mark.asyncio
    async def test_the_canonical_token_is_refused_at_the_talk_seam(
        self, config, fake_talk, promoted,
    ):
        """The in-file control for everything below it.

        `edit_talk_message` catches every exception and returns False, so a
        converted test asserting only that nothing raised would pass just as
        well with the double unwired. This one makes ISSUE-400's exact mistake
        — the canonical token handed to the Talk API — and requires the double
        to refuse it, so the guard the rest of the file leans on is
        demonstrated here rather than assumed.
        """
        ok = await edit_talk_message(
            config, _talk_task(promoted.canonical), 42, "Updated",
            target_token=promoted.canonical,
        )
        assert ok is False
        assert [(c.method, c.token, c.refused) for c in fake_talk.calls] == [
            ("edit_message", promoted.canonical, True),
        ]

    @pytest.mark.asyncio
    async def test_edit_honours_the_target_token(self, config, fake_talk, promoted):
        ok = await edit_talk_message(
            config, _talk_task(promoted.canonical), 42, "Updated",
            target_token=promoted.talk_ref,
        )
        assert ok is True
        assert _addressed(fake_talk) == [("edit_message", promoted.talk_ref)]
        assert fake_talk.calls[0].args == {"message_id": 42, "message": "Updated"}
        assert fake_talk.refusals == []

    @pytest.mark.asyncio
    async def test_without_a_target_token_the_conversation_token_stands(
        self, config, fake_talk, plain,
    ):
        """Callers that never had a room to resolve keep the old behaviour."""
        ok = await edit_talk_message(
            config, _talk_task(plain.canonical), 42, "Updated",
        )
        assert ok is True
        assert _addressed(fake_talk) == [("edit_message", plain.talk_ref)]
        assert fake_talk.calls[0].args == {"message_id": 42, "message": "Updated"}

    @pytest.mark.asyncio
    async def test_a_target_token_survives_an_empty_conversation_token(
        self, config, fake_talk, promoted,
    ):
        """The guard is about where the edit is going, not about what the task
        row happens to store."""
        ok = await edit_talk_message(
            config, _talk_task(""), 42, "Updated", target_token=promoted.talk_ref,
        )
        assert ok is True
        assert _addressed(fake_talk) == [("edit_message", promoted.talk_ref)]


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
    was actually addressed with — which the double would have refused had it
    been the canonical one.
    """

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_progress_edits_address_the_bound_talk_room(
        self, mock_run, config, fake_talk, promoted,
    ):
        sub = TalkEventSubscriber(
            config, _talk_task(promoted.canonical), ack_msg_id=100,
            target_token=promoted.talk_ref,
        )
        sub.on_event(_ev("tool_start", {"description": "Reading x.txt"}))
        assert _addressed(fake_talk) == [("edit_message", promoted.talk_ref)]
        assert fake_talk.refusals == []

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_the_terminal_summary_edit_too(
        self, mock_run, config, fake_talk, promoted,
    ):
        sub = TalkEventSubscriber(
            config, _talk_task(promoted.canonical), ack_msg_id=100,
            target_token=promoted.talk_ref,
        )
        sub.on_event(_ev("result", {"text": "done"}))
        assert _addressed(fake_talk) == [("edit_message", promoted.talk_ref)]
        assert fake_talk.refusals == []

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_streamed_text_posts_and_edits_on_the_bound_talk_room(
        self, mock_run, config, fake_talk, promoted,
    ):
        sent = _record_sent_ids(fake_talk)
        sub = TalkEventSubscriber(
            config, _talk_task(promoted.canonical), ack_msg_id=100,
            target_token=promoted.talk_ref,
        )
        sub.on_event(_ev("progress_text", {"text": "Working on it"}, seq=1))
        assert _addressed(fake_talk) == [("send_message", promoted.talk_ref)]
        sub.on_event(_ev("progress_text", {"text": "Still working"}, seq=2))
        assert _addressed(fake_talk) == [
            ("send_message", promoted.talk_ref),
            ("edit_message", promoted.talk_ref),
        ]
        # The edit addresses the message the send created, not the ack (100).
        assert len(sent) == 1
        assert fake_talk.calls[1].args["message_id"] == sent[0]

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_a_failed_ack_still_streams_text_to_the_bound_room(
        self, mock_run, config, fake_talk, promoted,
    ):
        """The one path where a wrong room makes a stray message rather than a
        no-op edit: the ack post failed, so there is no message to edit and the
        first `progress_text` posts a new one."""
        sub = TalkEventSubscriber(
            config, _talk_task(promoted.canonical), ack_msg_id=None,
            target_token=promoted.talk_ref,
        )
        sub.on_event(_ev("progress_text", {"text": "Working on it"}, seq=1))
        assert _addressed(fake_talk) == [("send_message", promoted.talk_ref)]
        assert fake_talk.refusals == []

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_no_target_token_still_addresses_the_conversation_token(
        self, mock_run, config, fake_talk, plain,
    ):
        """A caller with no room to resolve keeps the old destination — stated
        as the room the API was addressed with, not as an absent keyword."""
        sub = TalkEventSubscriber(
            config, _talk_task(plain.canonical), ack_msg_id=100,
        )
        sub.on_event(_ev("tool_start", {"description": "Reading x.txt"}))
        assert _addressed(fake_talk) == [("edit_message", plain.talk_ref)]
        assert fake_talk.refusals == []
