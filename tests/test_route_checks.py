"""Save-time route checks outside the web app (`transport/route_checks.py`).

The web profile PUT and the briefing POST reach these through thin wrappers in
`webui.app`, and their own tests are the check that the move changed nothing.
These cover the module directly, as the briefings_config skill calls it: with
`config` passed in rather than read from a module global, and with
`RouteRefused` telling a room or surface refusal from a malformed schedule.
"""

import pytest

from istota import db, user_briefings
from istota.config import Config, UserConfig
from istota.transport import route_checks
from istota.transport.route_checks import RouteRefused, validate_briefing_schedule


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "istota.db"
    db.init_db(path)
    return Config(
        db_path=path,
        users={
            "alice": UserConfig(alerts_channel="alerts-alice"),
            "bob": UserConfig(),
        },
    )


def _talk_room(config, ref, members, *, guest=False):
    with db.get_db(config.db_path) as conn:
        db.register_room(conn, ref, members[0], origin="talk", name=ref)
        db.add_room_binding(conn, ref, "talk", ref)
        for user_id in members:
            db.add_room_member(conn, ref, user_id)
            db.upsert_room_participant(conn, room_token=ref, surface="talk",
                                       surface_ref=user_id, kind="principal",
                                       user_id=user_id)
        if guest:
            db.upsert_room_participant(conn, room_token=ref, surface="talk",
                                       surface_ref="guest/max", kind="guest")


def _payload(**overrides):
    return {"name": "morning", "cron": "0 7 * * *", "output": "talk",
            "conversation_token": "alerts-alice", **overrides}


class TestValidateBriefingSchedule:
    def test_a_valid_schedule_comes_back_cleaned(self, config):
        cleaned = validate_briefing_schedule(
            config, _payload(name=" morning ", title=""), user_id="alice",
            name_required=True,
        )
        assert cleaned == {
            "name": "morning", "cron": "0 7 * * *", "title": "",
            "conversation_token": "alerts-alice", "output": "talk",
            "enabled": True,
        }

    def test_a_member_room_is_accepted(self, config):
        _talk_room(config, "conv-team", ["alice"])
        validate_briefing_schedule(
            config, _payload(conversation_token="conv-team"), user_id="alice",
            name_required=True,
        )

    def test_a_token_the_user_is_not_in_is_refused(self, config):
        _talk_room(config, "conv-bob", ["bob"])
        with pytest.raises(RouteRefused, match="not one of your conversations"):
            validate_briefing_schedule(
                config, _payload(conversation_token="conv-bob"), user_id="alice",
                name_required=True,
            )

    def test_a_talk_leaf_the_user_is_not_in_is_refused(self, config):
        _talk_room(config, "conv-bob", ["bob"])
        with pytest.raises(RouteRefused, match="not one of your conversations"):
            validate_briefing_schedule(
                config, _payload(output="talk:conv-bob"), user_id="alice",
                name_required=True,
            )

    def test_a_shared_room_is_refused(self, config):
        _talk_room(config, "conv-family", ["alice"], guest=True)
        with pytest.raises(RouteRefused, match="shared with other people"):
            validate_briefing_schedule(
                config, _payload(conversation_token="conv-family"), user_id="alice",
                name_required=True,
            )

    def test_an_unknown_surface_is_refused(self, config):
        with pytest.raises(RouteRefused, match="unknown delivery surface: pigeon"):
            validate_briefing_schedule(
                config, _payload(output="pigeon"), user_id="alice",
                name_required=True,
            )

    def test_a_talk_output_with_no_token_is_not_a_route_refusal(self, config):
        with pytest.raises(ValueError, match="conversation_token is required") as exc:
            validate_briefing_schedule(
                config, _payload(conversation_token=""), user_id="alice",
                name_required=True,
            )
        assert not isinstance(exc.value, RouteRefused)

    @pytest.mark.parametrize("overrides, message", [
        ({"name": ""}, "name is required"),
        ({"cron": ""}, "cron is required"),
        ({"title": "x" * 201}, "200 characters"),
        ({"enabled": "yes"}, "enabled must be a boolean"),
    ])
    def test_a_malformed_schedule_is_a_plain_value_error(self, config, overrides, message):
        with pytest.raises(ValueError, match=message) as exc:
            validate_briefing_schedule(
                config, _payload(**overrides), user_id="alice", name_required=True,
            )
        assert not isinstance(exc.value, RouteRefused)

    def test_an_unchanged_stored_token_on_a_now_shared_room_is_accepted(self, config):
        _talk_room(config, "conv-family", ["alice"])
        user_briefings.ensure_briefing(
            config.db_path, user_id="alice", name="morning", cron="0 7 * * *",
            title="", conversation_token="conv-family", output="talk", enabled=True,
        )
        with db.get_db(config.db_path) as conn:
            db.upsert_room_participant(conn, room_token="conv-family", surface="talk",
                                       surface_ref="guest/max", kind="guest")
        stored = user_briefings.get_briefing(config.db_path, "alice", "morning")
        payload = _payload(conversation_token="conv-family", enabled=False)
        cleaned = validate_briefing_schedule(
            config, payload, user_id="alice", stored=stored, name_required=True,
        )
        assert cleaned["enabled"] is False
        with pytest.raises(RouteRefused, match="shared"):
            validate_briefing_schedule(
                config, payload, user_id="alice", stored=None, name_required=True,
            )


class TestTheFamily:
    def test_route_tokens_are_the_channels_plus_member_rooms(self, config):
        _talk_room(config, "conv-team", ["alice"])
        _talk_room(config, "conv-bob", ["bob"])
        assert route_checks.talk_route_tokens(config, "alice") == {
            "alerts-alice", "conv-team",
        }

    def test_no_config_checks_nothing(self):
        route_checks.validate_talk_route_token(None, "conv-anything", "alice")
        route_checks.validate_descriptor_rooms(None, "web:room-anything", "alice")

    def test_a_web_room_the_user_is_not_in_is_refused(self, config):
        with db.get_db(config.db_path) as conn:
            room = db.create_web_chat_room(conn, "bob", "bobs")
        with pytest.raises(ValueError, match="is not one of your rooms"):
            route_checks.validate_descriptor_rooms(config, f"web:{room.token}", "alice")
