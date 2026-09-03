"""Tests for the Talk event subscriber (replaces the old progress callback).

Driven onto `fake_talk` rather than onto a `scheduler.edit_talk_message` mock.
The subscriber's job is to edit *one* message in *one* room over and over, so
mocking the shim away left every assertion here about the body text and none
about the room — and the room is what ISSUE-400 got wrong. The bodies are
asserted exactly as before, off the double's recorded arguments; what is new is
that a call naming a room Nextcloud would not recognise is refused rather than
accepted, and `edit_talk_message` swallowing that refusal into a `False` is
visible in `calls` instead of nowhere.

Every room here is `plain_talk_room` — the subscriber takes its destination
from its caller and does not resolve anything, so the two shapes cannot differ
for it. `TestEditTalkMessage` is the exception and takes both, because that
function *does* choose between `target_token` and `task.conversation_token`.
"""

import asyncio
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, NextcloudConfig
from istota.consumers import TalkEventSubscriber
from istota.events import TaskEvent
from istota.scheduler import edit_talk_message

from .support.rooms import plain_talk_room, promoted_room


@pytest.fixture
def room(db_path):
    with db.get_db(db_path) as conn:
        return plain_talk_room(conn, "testuser")


def _make_task(**kwargs):
    defaults = dict(
        id=99,
        prompt="test",
        user_id="testuser",
        source_type="talk",
        status="running",
        conversation_token="room123",
    )
    defaults.update(kwargs)
    return db.Task(**defaults)


def _make_config(tmp_path, **overrides):
    config = Config(
        db_path=tmp_path / "test.db",
        nextcloud=NextcloudConfig(url="https://nc.test", username="bot", app_password="pw"),
    )
    return config


def _ev(kind, payload, seq=1):
    return TaskEvent(
        task_id=99, seq=seq, kind=kind, payload=payload,
        created_at="2026-06-06T00:00:00.000Z",
    )


def _edits(fake_talk):
    """The bodies of every accepted edit, in order."""
    return [
        c.args["message"] for c in fake_talk.calls
        if c.method == "edit_message" and not c.refused
    ]


# ---------------------------------------------------------------------------
# TalkEventSubscriber
# ---------------------------------------------------------------------------


class TestTalkEventSubscriber:
    """`run_coro` runs the coroutine inline, so the edits reach the double.

    Each case asserts the body the room was actually edited with — a mocked
    `edit_talk_message` would have shown the same body for a room that does not
    exist, which is the whole failure mode the double removes.
    """

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_tool_start_edits_ack(self, mock_run, tmp_path, fake_talk, room):
        sub = TalkEventSubscriber(
            _make_config(tmp_path), _make_task(conversation_token=room.canonical),
            ack_msg_id=100,
        )
        sub.on_event(_ev("tool_start", {
            "tool_name": "Read", "description": "📄 Reading x.txt", "tool_call_id": "t1",
        }))
        assert [(c.method, c.token) for c in fake_talk.calls] == [
            ("edit_message", room.talk_ref),
        ]
        assert fake_talk.calls[0].args["message_id"] == 100
        body = fake_talk.calls[0].args["message"]
        assert "📄 Reading x.txt" in body
        assert "s)" in body  # elapsed seconds annotation
        assert sub.descriptions == ["📄 Reading x.txt"]

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_tool_end_annotates_with_duration(self, mock_run, tmp_path, fake_talk, room):
        sub = TalkEventSubscriber(
            _make_config(tmp_path), _make_task(conversation_token=room.canonical),
            ack_msg_id=100,
        )
        sub.on_event(_ev("tool_start", {"description": "📄 Reading x.txt"}, seq=1))
        sub.on_event(_ev("tool_end", {
            "tool_name": "Read", "tool_call_id": "t1", "success": True, "duration_ms": 180,
        }, seq=2))
        body = _edits(fake_talk)[-1]
        assert "✓" in body
        assert "180ms" in body

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_tool_end_failure_marks_cross(self, mock_run, tmp_path, fake_talk, room):
        sub = TalkEventSubscriber(
            _make_config(tmp_path), _make_task(conversation_token=room.canonical),
            ack_msg_id=100,
        )
        sub.on_event(_ev("tool_start", {"description": "⚙️ build"}, seq=1))
        sub.on_event(_ev("tool_end", {
            "tool_name": "Bash", "tool_call_id": "t1", "success": False, "duration_ms": 5,
        }, seq=2))
        assert "✗" in _edits(fake_talk)[-1]

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_no_ack_msg_id_means_no_edits(self, mock_run, tmp_path, fake_talk, room):
        sub = TalkEventSubscriber(
            _make_config(tmp_path), _make_task(conversation_token=room.canonical),
            ack_msg_id=None,
        )
        sub.on_event(_ev("tool_start", {"description": "📄 Reading x.txt"}))
        sub.on_event(_ev("error", {"message": "boom"}, seq=2))
        # Nothing reached Talk at all — not even a refused attempt, which is
        # what "no edits" has to mean now that a refusal is also silent.
        assert fake_talk.calls == []
        # descriptions are still accumulated.
        assert sub.descriptions == ["📄 Reading x.txt"]

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_progress_text_posts_then_edits(self, mock_run, tmp_path, fake_talk, room):
        sub = TalkEventSubscriber(
            _make_config(tmp_path), _make_task(conversation_token=room.canonical),
            ack_msg_id=100,
        )
        sub.on_event(_ev("progress_text", {"text": "Working on it"}, seq=1))
        assert [(c.method, c.token) for c in fake_talk.calls] == [
            ("send_message", room.talk_ref),
        ]
        assert sub._text_msg_id == fake_talk.sent_ids[0]
        # Second text event edits the existing message rather than posting.
        sub.on_event(_ev("progress_text", {"text": "Still working"}, seq=2))
        assert [(c.method, c.token) for c in fake_talk.calls] == [
            ("send_message", room.talk_ref),
            ("edit_message", room.talk_ref),
        ]
        # The edit addresses the posted message, not the ack (100).
        assert fake_talk.calls[1].args["message_id"] == fake_talk.sent_ids[0]
        body = fake_talk.calls[1].args["message"]
        assert "Working on it" in body and "Still working" in body

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_result_edits_ack_with_done_summary(self, mock_run, tmp_path, fake_talk, room):
        sub = TalkEventSubscriber(
            _make_config(tmp_path), _make_task(conversation_token=room.canonical),
            ack_msg_id=100,
        )
        sub.on_event(_ev("tool_start", {"description": "📄 Reading a"}, seq=1))
        sub.on_event(_ev("tool_start", {"description": "📄 Reading b"}, seq=2))
        sub.on_event(_ev("result", {"text": "done", "truncated": False}, seq=3))
        body = _edits(fake_talk)[-1]
        assert "✅ Done" in body
        assert "2 actions" in body  # N-actions summary retained
        assert "#99" in body

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_result_summary_singular_and_zero(self, mock_run, tmp_path, fake_talk, room):
        sub = TalkEventSubscriber(
            _make_config(tmp_path), _make_task(conversation_token=room.canonical),
            ack_msg_id=100,
        )
        sub.on_event(_ev("result", {"text": "done"}, seq=1))
        body = _edits(fake_talk)[-1]
        assert "✅ Done" in body
        assert "action" not in body  # zero actions → no "— N actions" clause

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_error_edits_ack_with_failed_summary(self, mock_run, tmp_path, fake_talk, room):
        sub = TalkEventSubscriber(
            _make_config(tmp_path), _make_task(conversation_token=room.canonical),
            ack_msg_id=100,
        )
        sub.on_event(_ev("error", {"message": "Something broke"}))
        assert "❌ Failed" in _edits(fake_talk)[-1]

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_cancelled_edits_ack(self, mock_run, tmp_path, fake_talk, room):
        sub = TalkEventSubscriber(
            _make_config(tmp_path), _make_task(conversation_token=room.canonical),
            ack_msg_id=100,
        )
        sub.on_event(_ev("cancelled", {}))
        assert "Cancelled" in _edits(fake_talk)[-1]

    @patch("istota.consumers.talk.run_coro", side_effect=Exception("network"))
    def test_edit_exception_swallowed(self, mock_run, tmp_path, fake_talk, room):
        sub = TalkEventSubscriber(
            _make_config(tmp_path), _make_task(conversation_token=room.canonical),
            ack_msg_id=100,
        )
        # Must not raise.
        sub.on_event(_ev("tool_start", {"description": "📄 Reading x.txt"}))
        assert fake_talk.calls == []

    @patch("istota.consumers.talk.run_coro", side_effect=asyncio.run)
    def test_on_finish_is_noop(self, mock_run, tmp_path, fake_talk, room):
        sub = TalkEventSubscriber(
            _make_config(tmp_path), _make_task(conversation_token=room.canonical),
            ack_msg_id=100,
        )
        sub.on_finish()  # no result delivery here — scheduler owns that
        assert fake_talk.calls == []


# ---------------------------------------------------------------------------
# edit_talk_message (unchanged scheduler helper)
# ---------------------------------------------------------------------------


class TestEditTalkMessage:
    """The one place in this file that chooses a destination, so the one place
    the two room shapes can disagree.

    With no `target_token` the helper edits `task.conversation_token`, which on
    a promoted room is the unpostable canonical one — the ISSUE-400 mistake,
    here made through the *default* rather than through an argument. The
    resolved-caller cases live in `tests/test_promoted_room_progress.py`.
    """

    @pytest.mark.asyncio
    @pytest.mark.parametrize("shape", ["plain", "promoted"])
    async def test_edit_addresses_the_tasks_own_token(
        self, shape, db_path, fake_talk,
    ):
        config = Config(nextcloud=NextcloudConfig(
            url="https://nc.test", username="bot", app_password="pass",
        ))
        with db.get_db(db_path) as conn:
            room = (
                plain_talk_room(conn, "testuser") if shape == "plain"
                else promoted_room(conn, "testuser")
            )
        task = _make_task(conversation_token=room.canonical)
        result = await edit_talk_message(config, task, 42, "Updated")

        assert [(c.method, c.token, c.refused) for c in fake_talk.calls] == [
            ("edit_message", room.canonical, room.diverges),
        ]
        if room.diverges:
            # The `False` is the swallowed 404, not "nothing was attempted" —
            # which is exactly why the refusal above is asserted alongside it.
            assert result is False
        else:
            assert result is True
            assert fake_talk.calls[0].args == {
                "message_id": 42, "message": "Updated",
            }

    @pytest.mark.asyncio
    async def test_edit_returns_false_on_a_404(self, db_path, fake_talk):
        """A token naming no conversation — the real failure this used to fake
        with an `Exception("404")` side effect."""
        config = Config(nextcloud=NextcloudConfig(
            url="https://nc.test", username="bot", app_password="pass",
        ))
        task = _make_task(conversation_token="nosuchroom")
        result = await edit_talk_message(config, task, 42, "Updated")
        assert result is False
        assert [(c.token, c.refused) for c in fake_talk.calls] == [
            ("nosuchroom", True),
        ]

    @pytest.mark.asyncio
    async def test_edit_returns_false_no_url(self, fake_talk):
        config = Config()  # no nextcloud URL
        task = _make_task()
        result = await edit_talk_message(config, task, 42, "msg")
        assert result is False
        # The guard is in front of the client, not behind it.
        assert fake_talk.calls == []

    @pytest.mark.asyncio
    async def test_edit_returns_false_no_conversation_token(self, fake_talk):
        config = Config(nextcloud=NextcloudConfig(
            url="https://nc.test", username="bot", app_password="pass",
        ))
        task = _make_task(conversation_token="")
        result = await edit_talk_message(config, task, 42, "msg")
        assert result is False
        assert fake_talk.calls == []
