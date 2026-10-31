"""ISSUE-909 — a `talk:` target naming a minted room token never reached Talk.

A Talk-origin room carries an `rm_` identity and a separate Talk binding, and
`room_target_descriptor` still answered `talk:<rm_…>`, so `rooms list` and the
prompt header taught a target naming no conversation. The answer reached the
web view through the canonical row, and the Talk post went to a conversation
Nextcloud does not have.

Two halves: jobs already written with that target are repaired at delivery, and
new jobs are handed a target that names the conversation.
"""

from __future__ import annotations

import asyncio
import logging
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
from istota.notifications.delivery import send_notification
from istota.scheduler import process_one_task
from istota.transport.routing import resolve_delivery_plan, room_target_descriptor

from .support.rooms import plain_talk_room

USER = "testuser"
ANSWER = "The reminder fired."


@pytest.fixture
def config(tmp_path, db_path):
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    cfg = Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(
            url="https://nc.example.com", username="istota", app_password="s",
        ),
        talk=TalkConfig(enabled=True, bot_username="istota"),
        email=EmailConfig(enabled=False),
        scheduler=SchedulerConfig(),
        workspace_path=mount,
        temp_dir=tmp_path / "temp",
        users={USER: UserConfig(display_name="Alice")},
    )
    cfg.temp_dir.mkdir(exist_ok=True)
    return cfg


@pytest.fixture
def room(config):
    with db.get_db(config.db_path) as conn:
        shape = plain_talk_room(conn, USER)
    assert db.is_canonical_room_token(shape.canonical)
    assert shape.canonical != shape.talk_ref
    return shape


def _job_task(token, output_target, **kwargs):
    return db.Task(
        id=1, status="pending", source_type="scheduled", user_id=USER,
        prompt="remind me", conversation_token=token,
        output_target=output_target, priority=5, attempt_count=0, max_attempts=3,
        **kwargs,
    )


def _queue_job(config, token, output_target):
    with db.get_db(config.db_path) as conn:
        return db.create_task(
            conn, prompt="remind me", user_id=USER, source_type="scheduled",
            conversation_token=token, output_target=output_target,
        )


def _run(config):
    with patch(
        "istota.scheduler.execute_task", return_value=(True, ANSWER, None, None),
    ):
        process_one_task(config)


def _landed(fake_talk, token):
    return [
        c for c in fake_talk.calls_to(token, method="send_message")
        if c.sent_id is not None
    ]


@patch("istota.scheduler.run_coro", side_effect=asyncio.run)
class TestAJobReachesTheBoundConversation:
    """Through the real seam: the row a job makes, the plan, the Talk post."""

    def test_a_target_already_written_with_the_room_token_is_repaired(
        self, mock_run, config, fake_talk, room,
    ):
        """The reported failure, and the `CRON.md` rows it left behind."""
        _queue_job(config, room.canonical, f"talk:{room.canonical}")
        _run(config)

        assert len(_landed(fake_talk, room.talk_ref)) == 1
        assert fake_talk.refusals == []

    def test_the_target_the_room_hands_out_reaches_it(
        self, mock_run, config, fake_talk, room,
    ):
        target = room_target_descriptor(room.canonical, "talk", room.talk_ref)
        _queue_job(config, room.canonical, target)
        _run(config)

        assert len(_landed(fake_talk, room.talk_ref)) == 1
        assert fake_talk.refusals == []

    def test_the_answer_lands_in_the_rooms_transcript_once(
        self, mock_run, config, fake_talk, room,
    ):
        task_id = _queue_job(config, room.canonical, f"talk:{room.canonical}")
        _run(config)

        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT body FROM messages WHERE room_token = ? AND task_id = ? "
                "AND role = 'assistant'",
                (room.canonical, task_id),
            ).fetchall()
        assert [r["body"] for r in rows] == [ANSWER]

    def test_a_room_with_no_talk_binding_keeps_its_answer_in_the_room(
        self, mock_run, config, fake_talk,
    ):
        """The drop the issue suggested would have emptied a scheduled job's
        plan, and with it the only copy of the answer."""
        with db.get_db(config.db_path) as conn:
            token = db.register_room(conn, None, USER, origin="web", name="web").token
        task_id = _queue_job(config, token, f"talk:{token}")
        _run(config)

        assert [c for c in fake_talk.calls if c.method == "send_message"] == []
        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT body FROM messages WHERE room_token = ? AND task_id = ? "
                "AND role = 'assistant'",
                (token, task_id),
            ).fetchall()
        assert [r["body"] for r in rows] == [ANSWER]

    def test_a_token_naming_no_room_keeps_the_answer_in_the_alert(
        self, mock_run, config, fake_talk,
    ):
        _queue_job(config, None, "talk:rm_gone")
        with patch("istota.scheduler.send_notification", return_value=True):
            _run(config)

        with db.get_db(config.db_path) as conn:
            (row,) = conn.execute(
                "SELECT body FROM notifications WHERE source = 'task_alert'",
            ).fetchall()
        assert ANSWER in row["body"]

    def test_another_users_room_token_does_not_reach_their_conversation(
        self, mock_run, config, fake_talk,
    ):
        with db.get_db(config.db_path) as conn:
            theirs = plain_talk_room(conn, "someone-else")
        _queue_job(config, None, f"talk:{theirs.canonical}")
        with patch("istota.scheduler.send_notification", return_value=True):
            _run(config)

        assert _landed(fake_talk, theirs.talk_ref) == []


class TestANotification:
    def test_a_route_naming_a_room_token_reaches_its_conversation(
        self, config, fake_talk, room,
    ):
        """A check's `channel` in HEARTBEAT.md goes this way, with no save-time
        validator in front of it."""
        with patch("istota.async_runtime.run_coro", side_effect=asyncio.run):
            sent = send_notification(
                config, USER, "disk is full", surface=f"talk:{room.canonical}",
            )

        assert sent is True
        assert len(_landed(fake_talk, room.talk_ref)) == 1
        assert fake_talk.refusals == []


class TestThePlan:
    def test_a_room_token_resolves_to_its_talk_binding(self, config, room):
        plan = resolve_delivery_plan(
            config, _job_task(room.canonical, f"talk:{room.canonical}"), None,
        )
        assert [(d.surface, d.channel) for d in plan] == [("talk", room.talk_ref)]

    def test_a_room_token_with_no_talk_binding_is_the_rooms_web_leg(self, config):
        with db.get_db(config.db_path) as conn:
            token = db.register_room(conn, None, USER, origin="web", name="web").token
        plan = resolve_delivery_plan(
            config, _job_task(token, f"talk:{token}"), None,
        )
        assert [(d.surface, d.channel, d.kind) for d in plan] == [
            ("web", token, "push"),
        ]

    def test_a_token_that_is_not_the_users_room_stays_as_written_and_warns(
        self, config, caplog,
    ):
        with db.get_db(config.db_path) as conn:
            theirs = plain_talk_room(conn, "someone-else")
        with caplog.at_level(logging.WARNING, logger="istota.transport.routing"):
            plan = resolve_delivery_plan(
                config, _job_task(None, f"talk:{theirs.canonical}"), None,
            )
        assert [(d.surface, d.channel) for d in plan] == [("talk", theirs.canonical)]
        assert "names none of their rooms" in caplog.text

    def test_a_native_conversation_ref_passes_through_unread(self, config):
        """A Talk ref is the address itself, registered or not. A provisioned
        channel the registry has never heard of keeps working."""
        plan = resolve_delivery_plan(
            config, _job_task(None, "talk:rawconv"), None,
        )
        assert [(d.surface, d.channel) for d in plan] == [("talk", "rawconv")]

    def test_a_shared_room_named_by_token_is_still_refused(self, config, room):
        """Resolving the token must not walk a job past the shared-room rule
        from some other room."""
        with db.get_db(config.db_path) as conn:
            db.add_room_member(conn, room.canonical, "someone-else")
            assert db.room_is_shared(conn, room.canonical)
        plan = resolve_delivery_plan(
            config, _job_task(None, f"talk:{room.canonical}"), None,
        )
        assert plan == []
