"""Tests for ingest_message — IncomingMessage → task."""

import pytest

from istota import db
from istota.config import Config
from istota.transport import IncomingMessage, ingest_message


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def config(db_path):
    cfg = Config()
    cfg.db_path = db_path
    return cfg


class TestIngestMessage:
    def test_maps_all_fields_to_task(self, config, db_path):
        msg = IncomingMessage(
            user_id="alice",
            text="do the thing",
            source_type="talk",
            surface="talk",
            channel_token="room42",
            delivery_token="deliver42",
            platform_message_id=1001,
            reply_to_message_id=999,
            reply_to_content="parent text",
            attachments=["Talk/a.png"],
            is_group_chat=True,
            output_target="both",
            model="claude-opus-4-8",
            effort="high",
        )
        with db.get_db(db_path) as conn:
            task_id = ingest_message(conn, config, msg)

        with db.get_db(db_path) as conn:
            task = db.get_task(conn, task_id)

        assert task.prompt == "do the thing"
        assert task.user_id == "alice"
        assert task.source_type == "talk"
        assert task.conversation_token == "room42"
        assert task.talk_delivery_token == "deliver42"
        assert task.talk_message_id == 1001
        assert task.reply_to_talk_id == 999
        assert task.reply_to_content == "parent text"
        assert task.attachments == ["Talk/a.png"]
        assert task.is_group_chat is True
        assert task.output_target == "both"
        assert task.model == "claude-opus-4-8"
        assert task.effort == "high"

    def test_minimal_message(self, config, db_path):
        msg = IncomingMessage(
            user_id="bob",
            text="hello",
            source_type="email",
            surface="email",
            channel_token="thread1",
        )
        with db.get_db(db_path) as conn:
            task_id = ingest_message(conn, config, msg)
            task = db.get_task(conn, task_id)
        assert task.source_type == "email"
        assert task.conversation_token == "thread1"
        assert task.attachments is None

    def test_empty_attachments_become_none(self, config, db_path):
        msg = IncomingMessage(
            user_id="bob", text="x", source_type="talk",
            surface="talk", channel_token="c", attachments=[],
        )
        with db.get_db(db_path) as conn:
            task_id = ingest_message(conn, config, msg)
            task = db.get_task(conn, task_id)
        assert task.attachments is None

    def test_duplicate_talk_message_returns_existing_id(self, config, db_path):
        msg = IncomingMessage(
            user_id="alice", text="hi", source_type="talk",
            surface="talk", channel_token="room1", platform_message_id=555,
        )
        with db.get_db(db_path) as conn:
            first = ingest_message(conn, config, msg)
            second = ingest_message(conn, config, msg)
        assert first == second
