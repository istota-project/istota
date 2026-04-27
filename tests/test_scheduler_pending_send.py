"""Tests for the scheduler's deferred pending-send handler (Layer A).

When the email skill's recipient gate writes task_{id}_pending_send.json, the
scheduler holds the task in pending_confirmation, posts to the alerts channel,
and lets the existing confirmation-reply machinery resume.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

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
from istota.scheduler import _process_deferred_pending_sends


def _make_config(db_path, tmp_path, alerts_channel: str = ""):
    mount = tmp_path / "mount"
    mount.mkdir(exist_ok=True)
    return Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(
            url="https://nc.example.com", username="istota", app_password="x",
        ),
        talk=TalkConfig(enabled=True, bot_username="istota"),
        email=EmailConfig(enabled=False),
        scheduler=SchedulerConfig(),
        nextcloud_mount_path=mount,
        temp_dir=tmp_path / "temp",
        users={"alice": UserConfig(alerts_channel=alerts_channel)},
    )


class TestProcessDeferredPendingSends:
    @patch("istota.notifications.send_talk_confirmation")
    def test_no_file_returns_false(self, mock_send, db_path, tmp_path):
        config = _make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="x", user_id="alice", source_type="talk")
            task = db.get_task(conn, task_id)

        result = _process_deferred_pending_sends(config, task, user_temp)
        assert result is False
        mock_send.assert_not_called()

    @patch("istota.notifications.send_talk_confirmation")
    def test_queues_confirmation(self, mock_send, db_path, tmp_path):
        mock_send.return_value = 12345  # talk message id
        config = _make_config(db_path, tmp_path, alerts_channel="alerts-room")
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="x", user_id="alice", source_type="talk",
                conversation_token="main-room",
            )
            task = db.get_task(conn, task_id)

        # Skill wrote the gate file
        (user_temp / f"task_{task_id}_pending_send.json").write_text(json.dumps([
            {
                "to": "stranger@elsewhere.com",
                "subject": "About the project",
                "body": "Hi, I'm writing on behalf of the user about...",
                "content_type": "plain",
            },
        ]))

        result = _process_deferred_pending_sends(config, task, user_temp)
        assert result is True

        # Talk confirmation was posted to the alerts channel
        mock_send.assert_called_once()
        args = mock_send.call_args
        token_arg = args[0][3] if len(args[0]) >= 4 else args.kwargs.get("conversation_token")
        assert token_arg == "alerts-room"

        prompt_arg = args[0][2]
        assert "stranger@elsewhere.com" in prompt_arg
        assert "About the project" in prompt_arg
        assert "yes" in prompt_arg.lower()

        # Task is pending_confirmation with the prompt stored
        with db.get_db(db_path) as conn:
            updated = db.get_task(conn, task_id)
        assert updated.status == "pending_confirmation"
        assert updated.confirmation_prompt is not None
        assert "stranger@elsewhere.com" in updated.confirmation_prompt
        assert updated.talk_response_id == 12345

        # File is intentionally NOT deleted — needed on re-run
        assert (user_temp / f"task_{task_id}_pending_send.json").exists()

    @patch("istota.notifications.send_talk_confirmation")
    def test_confirmation_rerun_cleans_up_file(self, mock_send, db_path, tmp_path):
        config = _make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="x", user_id="alice", source_type="talk")
            db.set_task_confirmation(conn, task_id, "previous draft")
            db.confirm_task(conn, task_id)
            task = db.get_task(conn, task_id)

        # Leftover file from the original gate trigger
        path = user_temp / f"task_{task_id}_pending_send.json"
        path.write_text(json.dumps([{"to": "approved@x.com", "subject": "s", "body": "b", "content_type": "plain"}]))

        result = _process_deferred_pending_sends(config, task, user_temp)

        assert result is False  # not a fresh queue, just cleanup
        assert not path.exists()
        mock_send.assert_not_called()

    @patch("istota.notifications.send_talk_confirmation")
    def test_malformed_file_cleans_up(self, mock_send, db_path, tmp_path):
        config = _make_config(db_path, tmp_path)
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="x", user_id="alice", source_type="talk")
            task = db.get_task(conn, task_id)

        path = user_temp / f"task_{task_id}_pending_send.json"
        path.write_text("not valid json {{{")

        result = _process_deferred_pending_sends(config, task, user_temp)
        assert result is False
        assert not path.exists()
        mock_send.assert_not_called()

    @patch("istota.notifications.send_talk_confirmation")
    def test_multiple_recipients_in_prompt(self, mock_send, db_path, tmp_path):
        mock_send.return_value = 99
        config = _make_config(db_path, tmp_path, alerts_channel="alerts")
        user_temp = tmp_path / "temp" / "alice"
        user_temp.mkdir(parents=True)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(conn, prompt="x", user_id="alice", source_type="talk")
            task = db.get_task(conn, task_id)

        (user_temp / f"task_{task_id}_pending_send.json").write_text(json.dumps([
            {"to": "a@x.com", "subject": "S1", "body": "B1", "content_type": "plain"},
            {"to": "b@y.com", "subject": "S2", "body": "B2", "content_type": "plain"},
        ]))

        result = _process_deferred_pending_sends(config, task, user_temp)
        assert result is True

        prompt = mock_send.call_args[0][2]
        assert "a@x.com" in prompt
        assert "b@y.com" in prompt
