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
"""

import pytest
from unittest.mock import patch

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


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


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
        users={"testuser": UserConfig(display_name="Stefan")},
    )


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
    @patch("istota.scheduler.post_result_to_talk", return_value=93602)
    @patch("istota.scheduler.run_coro", return_value=93602)
    def test_native_talk_reply_is_stamped(
        self, mock_run_coro, mock_post_talk, db_path, tmp_path,
    ):
        """ISSUE-161: a Talk-origin exchange's reply lands in Talk natively —
        it must carry the Talk id, or the read-sync cap can never reach it."""
        config = _make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.register_room(conn, "cpzpcfx2", "testuser", origin="talk")
            db.add_room_binding(conn, "cpzpcfx2", "talk", "cpzpcfx2")
            task_id = db.create_task(
                conn, prompt="testing reverse", user_id="testuser",
                source_type="talk", conversation_token="cpzpcfx2",
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "reversed.", None, None),
        ):
            result = process_one_task(config)
        assert result is not None and result[1] is True

        assert _assistant_external_ids(db_path, task_id) == {"talk": "93602"}

    @patch("istota.scheduler.post_result_to_talk", return_value=93602)
    @patch("istota.scheduler.run_coro", return_value=93602)
    def test_native_talk_reply_lifts_read_sync_cap(
        self, mock_run_coro, mock_post_talk, db_path, tmp_path,
    ):
        """The whole point: after a Talk-origin exchange the pull cap must cover
        the assistant reply — not stop at the user's own inbound message."""
        config = _make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.register_room(conn, "cpzpcfx2", "testuser", origin="talk")
            db.add_room_binding(conn, "cpzpcfx2", "talk", "cpzpcfx2")
            task_id = db.create_task(
                conn, prompt="testing reverse", user_id="testuser",
                source_type="talk", conversation_token="cpzpcfx2",
            )
            # The inbound Talk turn, stamped at ingest.
            user_msg_id = db.add_message(
                conn, "cpzpcfx2", role="user", body="testing reverse",
                origin_surface="talk", task_id=task_id,
                external_ids={"talk": "93601"},
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "reversed.", None, None),
        ):
            process_one_task(config)

        with db.get_db(db_path) as conn:
            cap = db.room_max_talk_synced_message_id(conn, "cpzpcfx2")
            newest = db.room_max_message_id(conn, "cpzpcfx2")
        assert cap == newest, "cap must reach the newest message (the reply)"
        assert cap > user_msg_id, "cap must not stall at the inbound user turn"

    @patch("istota.scheduler.post_result_to_talk", return_value=4242)
    @patch("istota.scheduler.run_coro", return_value=4242)
    def test_web_mirror_reply_still_stamped(
        self, mock_run_coro, mock_post_talk, db_path, tmp_path,
    ):
        """Regression: the mirror leg that already worked keeps working."""
        config = _make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.register_room(conn, "webroom", "testuser", origin="web")
            db.add_room_binding(conn, "webroom", "web", "webroom")
            db.add_room_binding(conn, "webroom", "talk", "talktok42")
            task_id = db.create_task(
                conn, prompt="q", user_id="testuser", source_type="web",
                conversation_token="webroom", output_target="room",
            )
            db.add_message(
                conn, "webroom", role="user", body="q",
                origin_surface="web", task_id=task_id,
                external_ids={"talk": "555"},
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "a", None, None),
        ):
            process_one_task(config)

        assert _assistant_external_ids(db_path, task_id) == {"talk": "4242"}

    @patch("istota.scheduler.post_result_to_talk", return_value=77777)
    @patch("istota.scheduler.run_coro", return_value=77777)
    def test_cross_channel_post_is_not_stamped(
        self, mock_run_coro, mock_post_talk, db_path, tmp_path,
    ):
        """A task force-routed to a *different* Talk channel than its room's
        binding must not write that foreign post id into the room's ledger —
        it would wrongly advance the room's read-sync cap."""
        config = _make_config(db_path, tmp_path)
        with db.get_db(db_path) as conn:
            db.register_room(conn, "cpzpcfx2", "testuser", origin="talk")
            db.add_room_binding(conn, "cpzpcfx2", "talk", "cpzpcfx2")
            task_id = db.create_task(
                conn, prompt="q", user_id="testuser", source_type="talk",
                conversation_token="cpzpcfx2",
                output_target="talk:someotherroom",
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "a", None, None),
        ):
            process_one_task(config)

        assert _assistant_external_ids(db_path, task_id) is None
