"""Tests for scheduler repost suppression (Stage 3 of the user-scoped
Nextcloud OAuth spec): when the web process already posted a web-origin turn
into Talk as the user (external-id stamp on the user row), the completion-time
attributed repost is skipped — the answer post is unaffected.

Asserted at the Talk seam rather than at a `post_result_to_talk` mock. The
mirror leg only exists on a room bound to *two* surfaces, so every room here is
the promoted shape from `tests/support/rooms.py` and the destination is a
`talk` `surface_ref` that is not the room's canonical token. Against the old
permissive double a repost sent to the canonical `web-…` token was
indistinguishable from one sent to the Talk ref; `fake_talk` refuses the first,
exactly as Nextcloud does. `refusals == []` is asserted alongside every count,
because `TalkTransport.deliver` swallows the refusal and returns None — a
suppression test that only counted *successful* posts would read a 404 as a
suppression.

Not parametrized over `plain_talk_room`: a Talk-origin room has no `web`
binding, so there is no web-origin task in it to mirror. The non-diverging
mirror shape — a Talk room a user later also opens in web chat — is a third
shape neither builder makes, noted here rather than faked.
"""

import asyncio
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

from .support.rooms import promoted_room


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


def _seed_web_mirror_task(db_path, *, stamp_talk_id=None, external_ids=None):
    """A web-origin task in a Talk-bound room, with its canonical user turn
    stored (as record_inbound would). When `stamp_talk_id` is set, the user
    turn carries the post-as-user external-id stamp."""
    with db.get_db(db_path) as conn:
        shape = promoted_room(conn, "testuser")
        task_id = db.create_task(
            conn, prompt="what's the weather?", user_id="testuser",
            source_type="web", conversation_token=shape.canonical,
            output_target="room",
        )
        if external_ids is None and stamp_talk_id:
            external_ids = {"talk": str(stamp_talk_id)}
        db.add_message(
            conn, shape.canonical, role="user", body="what's the weather?",
            origin_surface="web", task_id=task_id, external_ids=external_ids,
        )
    return shape, task_id


def _bodies(fake_talk, shape):
    """Every message posted to the room's Talk ref, in order."""
    return [
        c.args["message"]
        for c in fake_talk.calls_to(shape.talk_ref, method="send_message")
    ]


class TestMirrorRepostSuppression:
    @patch("istota.scheduler.run_coro", side_effect=asyncio.run)
    def test_stamped_turn_suppresses_repost(
        self, mock_run, db_path, tmp_path, fake_talk,
    ):
        config = _make_config(db_path, tmp_path)
        shape, _ = _seed_web_mirror_task(db_path, stamp_talk_id=555)

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "It's sunny.", None, None),
        ):
            result = process_one_task(config)
        assert result is not None and result[1] is True

        # Only the answer was posted — no attributed repost.
        assert _bodies(fake_talk, shape) == ["It's sunny."]
        # Without this, a repost refused for naming the canonical token would
        # read exactly like a repost that was suppressed.
        assert fake_talk.refusals == []
        assert shape.canonical not in [c.token for c in fake_talk.calls]

    @patch("istota.scheduler.run_coro", side_effect=asyncio.run)
    def test_unstamped_turn_keeps_legacy_repost(
        self, mock_run, db_path, tmp_path, fake_talk,
    ):
        # Regression: no stamp (feature off / post failed) → the attributed
        # repost fires exactly as before, then the answer.
        config = _make_config(db_path, tmp_path)
        shape, _ = _seed_web_mirror_task(db_path, stamp_talk_id=None)

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "It's sunny.", None, None),
        ):
            result = process_one_task(config)
        assert result is not None and result[1] is True

        bodies = _bodies(fake_talk, shape)
        assert len(bodies) == 2
        assert "Alice" in bodies[0]
        assert "(via web)" in bodies[0]
        assert "what's the weather?" in bodies[0]
        assert bodies[1] == "It's sunny."
        assert fake_talk.refusals == []

    @patch("istota.scheduler.run_coro", side_effect=asyncio.run)
    def test_stamp_on_other_surface_does_not_suppress(
        self, mock_run, db_path, tmp_path, fake_talk,
    ):
        # An external id on a different surface must not suppress the Talk
        # repost — only a `talk` stamp signals the user post landed in Talk.
        config = _make_config(db_path, tmp_path)
        shape, _ = _seed_web_mirror_task(db_path, external_ids={"matrix": "abc"})

        with patch(
            "istota.scheduler.execute_task",
            return_value=(True, "a", None, None),
        ):
            process_one_task(config)

        bodies = _bodies(fake_talk, shape)
        assert len(bodies) == 2  # repost + answer
        assert "(via web)" in bodies[0]
        assert bodies[1] == "a"
        assert fake_talk.refusals == []


class TestUserTurnHasExternalId:
    """The database helper alone — no delivery, so no Talk seam to reach."""

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
