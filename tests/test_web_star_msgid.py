"""ISSUE-172: a web task's terminal `done` event must carry the durable
`messages.id` (the star key), so a live-settled web turn becomes starrable
without a history refetch. Also covers the synthetic terminal backstop.
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


def _seed_web_task(db_path):
    with db.get_db(db_path) as conn:
        db.register_room(conn, "webroom", "testuser", origin="web")
        db.add_room_binding(conn, "webroom", "web", "webroom")
        task_id = db.create_task(
            conn, prompt="what's the weather?", user_id="testuser",
            source_type="web", conversation_token="webroom",
            output_target="web",
        )
    return task_id


def _done_payload(db_path, task_id):
    with db.get_db(db_path) as conn:
        events = db.get_task_events(conn, task_id, 0)
    done = [e for e in events if e["kind"] == "done"]
    assert done, "no terminal done event emitted"
    return done[-1]["payload"]


class TestWebDoneCarriesMsgId:
    def test_done_event_carries_stored_assistant_msg_id(self, db_path, tmp_path):
        config = _make_config(db_path, tmp_path)
        task_id = _seed_web_task(db_path)

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "It's sunny.", None, None),
        ):
            result = process_one_task(config)
        assert result is not None and result[1] is True

        with db.get_db(db_path) as conn:
            stored = db.get_turn_message_id(conn, "webroom", task_id, "assistant")
        assert isinstance(stored, int)

        payload = _done_payload(db_path, task_id)
        assert payload.get("msg_id") == stored

    def test_synthetic_terminal_backstop_carries_msg_id(self, db_path, tmp_path):
        # A completed web task whose durable turn exists but whose client is
        # parked past the real terminal frame gets a synthesized `done` — it too
        # must carry the star key.
        pytest.importorskip("fastapi")
        from istota import web_app

        with db.get_db(db_path) as conn:
            db.register_room(conn, "webroom", "testuser", origin="web")
            task_id = db.create_task(
                conn, prompt="q", user_id="testuser", source_type="web",
                conversation_token="webroom", output_target="web",
            )
            db.update_task_status(conn, task_id, "completed", result="a")
            stored = db.store_turn_message(
                conn, "webroom", role="assistant", body="a", task_id=task_id,
                origin_surface="web",
            )

        config = _make_config(db_path, tmp_path)
        web_app._config = config
        frames = web_app._synthetic_terminal_events(task_id, after_seq=0)
        done = [f for f in frames if f["kind"] == "done"]
        assert done and done[-1]["payload"].get("msg_id") == stored
