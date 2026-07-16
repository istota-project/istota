"""Tests for scheduler repost suppression (Stage 3 of the user-scoped
Nextcloud OAuth spec): when the web process already posted a web-origin turn
into Talk as the user (external-id stamp on the user row), the completion-time
attributed repost is skipped — the answer post is unaffected."""

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


def _seed_web_mirror_task(db_path, *, stamp_talk_id=None):
    """A web-origin task in a Talk-bound room, with its canonical user turn
    stored (as record_inbound would). When `stamp_talk_id` is set, the user
    turn carries the post-as-user external-id stamp."""
    with db.get_db(db_path) as conn:
        db.register_room(conn, "webroom", "testuser", origin="web")
        db.add_room_binding(conn, "webroom", "web", "webroom")
        db.add_room_binding(conn, "webroom", "talk", "talktok42")
        task_id = db.create_task(
            conn, prompt="what's the weather?", user_id="testuser",
            source_type="web", conversation_token="webroom",
            output_target="room",
        )
        db.add_message(
            conn, "webroom", role="user", body="what's the weather?",
            origin_surface="web", task_id=task_id,
            external_ids=(
                {"talk": str(stamp_talk_id)} if stamp_talk_id else None
            ),
        )
    return task_id


class TestMirrorRepostSuppression:
    @patch("istota.scheduler.post_result_to_talk", return_value=4242)
    @patch("istota.scheduler.run_coro", return_value=4242)
    def test_stamped_turn_suppresses_repost(
        self, mock_run_coro, mock_post_talk, db_path, tmp_path,
    ):
        config = _make_config(db_path, tmp_path)
        _seed_web_mirror_task(db_path, stamp_talk_id=555)

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "It's sunny.", None, None),
        ):
            result = process_one_task(config)
        assert result is not None and result[1] is True

        talk_calls = [
            c for c in mock_post_talk.call_args_list
            if c.kwargs.get("target_token") == "talktok42"
        ]
        # Only the answer was posted — no attributed repost.
        assert len(talk_calls) == 1
        assert talk_calls[0].args[2] == "It's sunny."
        assert "(via web)" not in talk_calls[0].args[2]

    @patch("istota.scheduler.post_result_to_talk", return_value=4242)
    @patch("istota.scheduler.run_coro", return_value=4242)
    def test_unstamped_turn_keeps_legacy_repost(
        self, mock_run_coro, mock_post_talk, db_path, tmp_path,
    ):
        # Regression: no stamp (feature off / post failed) → the attributed
        # repost fires exactly as before, then the answer.
        config = _make_config(db_path, tmp_path)
        _seed_web_mirror_task(db_path, stamp_talk_id=None)

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "It's sunny.", None, None),
        ):
            result = process_one_task(config)
        assert result is not None and result[1] is True

        talk_calls = [
            c for c in mock_post_talk.call_args_list
            if c.kwargs.get("target_token") == "talktok42"
        ]
        assert len(talk_calls) == 2
        assert "Stefan" in talk_calls[0].args[2]
        assert "(via web)" in talk_calls[0].args[2]
        assert "what's the weather?" in talk_calls[0].args[2]
        assert talk_calls[1].args[2] == "It's sunny."

    @patch("istota.scheduler.post_result_to_talk", return_value=4242)
    @patch("istota.scheduler.run_coro", return_value=4242)
    def test_stamp_on_other_surface_does_not_suppress(
        self, mock_run_coro, mock_post_talk, db_path, tmp_path,
    ):
        # An external id on a different surface must not suppress the Talk
        # repost — only a `talk` stamp signals the user post landed in Talk.
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
                external_ids={"matrix": "abc"},
            )

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "a", None, None),
        ):
            process_one_task(config)

        talk_calls = [
            c for c in mock_post_talk.call_args_list
            if c.kwargs.get("target_token") == "talktok42"
        ]
        assert len(talk_calls) == 2  # repost + answer


class TestUserTurnHasExternalId:
    def test_helper(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "r", "u", origin="web")
            task_id = db.create_task(conn, prompt="p", user_id="u", source_type="web")
            db.add_message(
                conn, "r", role="user", body="p", origin_surface="web",
                task_id=task_id, external_ids={"talk": "1"},
            )
            assert db.user_turn_has_external_id(conn, task_id, "talk") is True
            assert db.user_turn_has_external_id(conn, task_id, "matrix") is False
            assert db.user_turn_has_external_id(conn, 424242, "talk") is False

    def test_helper_no_external_ids(self, db_path):
        with db.get_db(db_path) as conn:
            db.register_room(conn, "r", "u", origin="web")
            task_id = db.create_task(conn, prompt="p", user_id="u", source_type="web")
            db.add_message(
                conn, "r", role="user", body="p", origin_surface="web",
                task_id=task_id,
            )
            assert db.user_turn_has_external_id(conn, task_id, "talk") is False
