"""Tests for stamping the assistant turn's Talk message id into the canonical
`messages.external_ids` ledger (ISSUE-161).

The stamp used to run only for *mirror* Talk legs (web-origin tasks fanned out
to a bound Talk room). Talk-origin exchanges deliver natively, so their replies
went unstamped — and since `room_max_talk_synced_message_id` caps the Talk→web
read-sync cursor at the newest *stamped* row, the reply you just read in Talk
sat above the cap forever and the room never cleared in web.

The stamp now runs for any Talk leg, mirror or native, but only when the id was
posted to the room's *own* bound Talk channel — a cross-channel post id must
never enter the origin room's ledger (it would wrongly advance the read cap).

Driven through the real delivery path onto `fake_talk` rather than off a
`post_result_to_talk` mock's fixed return value, because *which room the id came
from* is the whole claim of the last two cases and a mock returns the same
number whatever room it was handed. The id asserted on is the one the double
minted for the result post, named by its `reference_id`. Rooms come from
`tests/support/rooms.py`: the native case takes both shapes, since the stamp on
a promoted room can only be right if the post resolved the room's `talk`
binding, and against a permissive double it looked right either way.
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
from istota.scheduler import process_one_task

from .support.rooms import plain_talk_room, promoted_room


def _make_config(db_path, tmp_path):
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


@pytest.fixture(params=["plain", "promoted"])
def talk_room(request, db_path):
    """A Talk-origin exchange, on a room whose two tokens agree and on one
    whose two tokens differ.

    Both are Talk-origin as far as the *task* is concerned — a promoted room's
    inbound Talk turn arrives on its `talk` binding — so the same delivery path
    runs over both, and only the promoted one can tell a resolved binding from
    the canonical token.
    """
    with db.get_db(db_path) as conn:
        if request.param == "plain":
            return plain_talk_room(conn, "testuser")
        return promoted_room(conn, "testuser")


def _assistant_external_ids(db_path, task_id):
    import json

    with db.get_db(db_path) as conn:
        row = conn.execute(
            "SELECT external_ids FROM messages WHERE task_id = ? "
            "AND role = 'assistant' LIMIT 1",
            (task_id,),
        ).fetchone()
    assert row is not None, "no assistant row stored for the task"
    return json.loads(row["external_ids"]) if row["external_ids"] else None


class TestTalkOriginAssistantStamp:
    @patch("istota.scheduler.run_coro", side_effect=asyncio.run)
    def test_native_talk_reply_is_stamped(
        self, mock_run, db_path, tmp_path, fake_talk, talk_room,
    ):
        """ISSUE-161: a Talk-origin exchange's reply lands in Talk natively —
        it must carry the Talk id, or the read-sync cap can never reach it."""
        config = _make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="testing reverse", user_id="testuser",
                source_type="talk", conversation_token=talk_room.canonical,
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "reversed.", None, None),
        ):
            result = process_one_task(config)
        assert result is not None and result[1] is True

        posted = fake_talk.sent_id_for(f"istota:task:{task_id}:result")
        assert posted is not None, "the reply never reached the Talk room"
        assert _assistant_external_ids(db_path, task_id) == {"talk": str(posted)}
        # The id is only the right one if the post resolved the room's binding.
        assert fake_talk.refusals == []
        assert [c.token for c in fake_talk.calls] == [talk_room.talk_ref] * len(
            fake_talk.calls
        )

    @patch("istota.scheduler.run_coro", side_effect=asyncio.run)
    def test_native_talk_reply_lifts_read_sync_cap(
        self, mock_run, db_path, tmp_path, fake_talk, talk_room,
    ):
        """The whole point: after a Talk-origin exchange the pull cap must cover
        the assistant reply — not stop at the user's own inbound message."""
        config = _make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="testing reverse", user_id="testuser",
                source_type="talk", conversation_token=talk_room.canonical,
            )
            # The inbound Talk turn, stamped at ingest.
            user_msg_id = db.add_message(
                conn, talk_room.canonical, role="user", body="testing reverse",
                origin_surface="talk", task_id=task_id,
                external_ids={"talk": "93601"},
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "reversed.", None, None),
        ):
            process_one_task(config)

        with db.get_db(db_path) as conn:
            cap = db.room_max_talk_synced_message_id(conn, talk_room.canonical)
            newest = db.room_max_message_id(conn, talk_room.canonical)
        assert cap == newest, "cap must reach the newest message (the reply)"
        assert cap > user_msg_id, "cap must not stall at the inbound user turn"
        assert fake_talk.refusals == []

    @patch("istota.scheduler.run_coro", side_effect=asyncio.run)
    def test_web_mirror_reply_still_stamped(
        self, mock_run, db_path, tmp_path, fake_talk,
    ):
        """Regression: the mirror leg that already worked keeps working."""
        config = _make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            shape = promoted_room(conn, "testuser")
            task_id = db.create_task(
                conn, prompt="q", user_id="testuser", source_type="web",
                conversation_token=shape.canonical, output_target="room",
            )
            db.add_message(
                conn, shape.canonical, role="user", body="q",
                origin_surface="web", task_id=task_id,
                external_ids={"talk": "555"},
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "a", None, None),
        ):
            process_one_task(config)

        posted = fake_talk.sent_id_for(f"istota:task:{task_id}:result")
        assert posted is not None, "the mirror leg never reached the Talk room"
        assert _assistant_external_ids(db_path, task_id) == {"talk": str(posted)}
        assert fake_talk.refusals == []

    @patch("istota.scheduler.run_coro", side_effect=asyncio.run)
    def test_cross_channel_post_is_not_stamped(
        self, mock_run, db_path, tmp_path, fake_talk, talk_room,
    ):
        """A task force-routed to a *different* Talk channel than its room's
        binding must not write that foreign post id into the room's ledger —
        it would wrongly advance the room's read-sync cap.

        `someotherroom` is a real Talk conversation with no room row, which is
        the ordinary shape of a `talk:<token>` override (an operator-configured
        channel), so it goes in `known_channels`. Left out, the post would be
        refused and the case would pass because nothing was delivered at all —
        which is not the claim.
        """
        fake_talk.known_channels.add("someotherroom")
        config = _make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="q", user_id="testuser", source_type="talk",
                conversation_token=talk_room.canonical,
                output_target="talk:someotherroom",
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "a", None, None),
        ):
            process_one_task(config)

        # The post landed — in the other room.
        assert fake_talk.sent_id_for(f"istota:task:{task_id}:result") is not None
        assert fake_talk.calls_to("someotherroom", method="send_message")
        assert _assistant_external_ids(db_path, task_id) is None
        assert fake_talk.refusals == []
