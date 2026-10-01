"""Delivery routing refuses a shared room for personal content (multiplayer
Stage 15, the speech-gate draft's SG 10 / B4).

A room more than one human reads is not a destination for a user's personal
deliveries. Four routes reached one before this, and each is pinned here:

- a task's delivery plan naming a shared room it did not run in — which is also
  the reach half: a cron job or briefing aimed at a shared room ran with the
  full reach of its origin, because the disclosure gate keys on the task's own
  conversation;
- the implicit default room (bare ``web``), including a configured
  ``default_room`` pin that became shared after it was set;
- a notification (alert, log, routed notification) whose channel is a room
  that is now shared, ``alerts_channel`` and ``log_channel`` included;
- the email poller's routed notification room.

A conversational reply into the room the task ran in is untouched: that task
was gated on that room.
"""

import logging

import pytest

from istota import db, notifications, user_profiles
from istota.config import Config, NextcloudConfig, TalkConfig, UserConfig
from istota.transport.registry import make_registry
from istota.transport.routing import resolve_delivery_plan

from .support.rooms import plain_talk_room


def _config(tmp_path, **alice):
    path = tmp_path / "state.db"
    db.init_db(path)
    return Config(
        db_path=path,
        temp_dir=tmp_path / "temp",
        nextcloud=NextcloudConfig(url="https://cloud.example.com", username="bot",
                                  app_password="secret"),
        talk=TalkConfig(enabled=True, bot_username="bot"),
        users={"alice": UserConfig(display_name="Alice", **alice),
               "bob": UserConfig(display_name="Bob")},
    )


def _shared_web_room(conn, name="Family"):
    token = db.create_web_chat_room(conn, "alice", name).token
    db.add_web_room_member(conn, token, "bob")
    return token


def _shared_talk_room(conn):
    """A Talk room alice is in with a guest: one member, two humans."""
    shape = plain_talk_room(conn, "alice", name="Team")
    db.add_room_member(conn, shape.canonical, "alice")
    for ref, kind, user in (("alice", "principal", "alice"), ("max", "guest", None)):
        db.upsert_room_participant(
            conn, room_token=shape.canonical, surface="talk", surface_ref=ref,
            kind=kind, user_id=user,
        )
    assert db.list_room_members(conn, shape.canonical) == ["alice"]
    assert db.room_is_shared(conn, shape.canonical)
    return shape.canonical


def _task(conn, **kwargs):
    kwargs.setdefault("user_id", "alice")
    kwargs.setdefault("prompt", "x")
    return db.get_task(conn, db.create_task(conn, **kwargs))


def _legs(plan):
    return [(d.surface, d.channel) for d in plan]


# ---------------------------------------------------------------------------
# The task delivery plan, and the reach half
# ---------------------------------------------------------------------------


class TestThePlanRefusesASharedRoom:
    @pytest.mark.parametrize("target", ["web:{room}", "room:{room}", "talk:{room}"])
    def test_a_cron_job_aimed_at_a_shared_room_delivers_nothing_there(
        self, tmp_path, caplog, target,
    ):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            shared = _shared_web_room(conn)
            db.add_room_binding(conn, shared, "talk", shared)
            private = db.create_web_chat_room(conn, "alice", "notes").token
            task = _task(conn, source_type="scheduled", conversation_token=private,
                         output_target=target.format(room=shared) + ",ntfy")
        with caplog.at_level(logging.WARNING, logger="istota.transport.routing"):
            plan = resolve_delivery_plan(config, task, make_registry(config))
        assert all(d.channel != shared for d in plan)
        # The rest of the plan survives.
        assert ("ntfy", None) in _legs(plan)
        refusals = [r.getMessage() for r in caplog.records if shared in r.getMessage()]
        assert refusals and "scheduled" in refusals[0]

    def test_a_briefing_is_refused_even_in_the_room_it_names(self, tmp_path):
        """A briefing's blocks are assembled daemon-side from the user's own
        sources, which no room gate reaches, so there is no gated form of it."""
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            shared = _shared_talk_room(conn)
            task = _task(conn, source_type="briefing", conversation_token=shared,
                         output_target=f"talk:{shared}")
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert all(d.channel != shared for d in plan)

    def test_a_guest_in_a_talk_room_makes_it_shared_for_the_plan(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            shared = _shared_talk_room(conn)
            task = _task(conn, source_type="scheduled", output_target=f"talk:{shared}")
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert all(d.channel != shared for d in plan)

    def test_a_conversational_reply_in_the_shared_room_is_kept(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            shared = _shared_talk_room(conn)
            talk_ref = db.get_room_binding(conn, shared, "talk").surface_ref
            task = _task(conn, source_type="talk", conversation_token=shared)
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert ("talk", talk_ref) in _legs(plan)

    def test_a_cron_job_in_the_shared_room_is_gated_on_it_and_delivered(self, tmp_path):
        """The other answer to the reach half: a job whose conversation is the
        room runs with that room's gate, so its answer may land there."""
        from istota.executor import _task_withheld_scopes

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            shared = _shared_web_room(conn)
            task = _task(conn, source_type="scheduled", conversation_token=shared,
                         output_target=f"web:{shared}")
            assert _task_withheld_scopes(config, conn, task, {}) >= {"files", "memory"}
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert ("web", shared) in _legs(plan)

    def test_an_interactive_task_still_gets_its_reply_when_the_named_room_is_refused(
        self, tmp_path,
    ):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            shared = _shared_talk_room(conn)
            private_room = plain_talk_room(conn, "alice", name="me")
            private = private_room.canonical
            db.add_room_member(conn, private, "alice")
            task = _task(conn, source_type="talk", conversation_token=private,
                         output_target=f"talk:{shared}")
        plan = resolve_delivery_plan(config, task, make_registry(config))
        # Refused, and the reply goes back where the turn was asked.
        assert _legs(plan) == [("talk", private_room.talk_ref)]

    def test_a_side_room_task_still_lands_in_its_side_room(self, tmp_path):
        """Stage 10's pin runs first and keeps its substitution."""
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            shared = _shared_web_room(conn)
            side = db.ensure_side_room(conn, shared, "alice")
            task = _task(conn, source_type="scheduled", conversation_token=side.token,
                         output_target=f"web:{shared}")
        plan = resolve_delivery_plan(config, task, make_registry(config))
        assert _legs(plan) == [("web", side.token)]


# ---------------------------------------------------------------------------
# The implicit default room
# ---------------------------------------------------------------------------


class TestTheDefaultRoomIsNeverShared:
    def test_the_heuristic_skips_a_talk_room_with_a_guest(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            shared = _shared_talk_room(conn)
            db.ensure_web_chat_handle(conn, "alice", shared, "Team")
            handle = db.default_web_room(conn, "alice")
            assert handle is None or handle.token != shared
            provisioned = db.ensure_default_web_chat_room(conn, "alice")
            assert provisioned.token != shared
            assert not db.room_is_shared(conn, provisioned.token)

    def test_a_pinned_default_room_that_becomes_shared_stops_receiving(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            pinned = db.create_web_chat_room(conn, "alice", "notes").token
        user_profiles.ensure_profile(config.db_path, "alice", display_name="alice")
        user_profiles.update_profile(config.db_path, "alice", default_room=pinned)
        with db.get_db(config.db_path) as conn:
            assert db.configured_default_room(conn, "alice") == pinned
            db.add_web_room_member(conn, pinned, "bob")
        with db.get_db(config.db_path) as conn:
            assert db.configured_default_room(conn, "alice") is None
            assert db.ensure_default_web_chat_room(conn, "alice").token != pinned
            handle = db.default_web_room(conn, "alice")
            assert handle is None or handle.token != pinned

    def test_the_bare_web_transcript_fallback_skips_a_shared_room(self, tmp_path):
        from istota.transport.routing import Destination, _room_for_destination

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            shared = _shared_web_room(conn)
            assert _room_for_destination(
                conn, config, "alice", Destination("web", None)) != shared

    def test_a_user_whose_only_room_is_shared_gets_a_private_one(self, tmp_path):
        """The `_web_ok` consequence: delivery provisions a private room, and
        the probe says web is configured because delivery would do that."""
        from istota.transport.web import default_web_room_token

        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            shared = _shared_web_room(conn)
        assert notifications.is_channel_configured(config, "alice", "web")
        token = default_web_room_token(config, "alice")
        assert token and token != shared
        with db.get_db(config.db_path) as conn:
            assert db.list_room_members(conn, token) == ["alice"]


# ---------------------------------------------------------------------------
# Notifications: alerts, the log channel, routed notifications
# ---------------------------------------------------------------------------


@pytest.fixture
def sends(monkeypatch):
    posted = {"talk": [], "web": []}

    async def fake_talk(config, user_id, message, conversation_token=None):
        posted["talk"].append(conversation_token)
        return 1

    def fake_web(config, user_id, message, conversation_token=None, title=None):
        posted["web"].append(conversation_token)
        return True

    monkeypatch.setattr(notifications, "_send_talk", fake_talk)
    monkeypatch.setattr(notifications, "_send_web", fake_web)
    monkeypatch.setattr(notifications, "mirror_talk_to_room", lambda *a, **k: None)
    return posted


class TestNotificationsRefuseASharedRoom:
    def test_an_alerts_channel_that_became_shared_gets_no_alert(
        self, tmp_path, sends, caplog,
    ):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            shared = _shared_talk_room(conn)
        config.users["alice"].alerts_channel = shared
        with caplog.at_level(logging.WARNING):
            sent = notifications.send_notification(
                config, "alice", "disk full", purpose="alert")
        assert shared not in sends["talk"]
        assert sent is False
        assert any(shared in r.getMessage() and "alert" in r.getMessage()
                   for r in caplog.records)

    def test_a_notice_about_a_turn_in_the_room_goes_back_to_it(self, tmp_path, sends):
        """The scheduler's "your task was cancelled" notice answers a turn
        asked in that room, so it is the conversational exemption; the same
        notice with no task room is refused."""
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            shared = _shared_talk_room(conn)
        assert not notifications.send_notification(
            config, "alice", "cancelled", conversation_token=shared)
        assert notifications.send_notification(
            config, "alice", "cancelled", conversation_token=shared, task_room=shared)
        assert sends["talk"] == [shared]

    def test_a_private_alerts_channel_still_gets_it(self, tmp_path, sends):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            own = plain_talk_room(conn, "alice", name="alerts").canonical
            db.add_room_member(conn, own, "alice")
        config.users["alice"].alerts_channel = own
        assert notifications.send_notification(config, "alice", "x", purpose="alert")
        assert sends["talk"] == [own]

    def test_a_routed_web_notification_into_a_shared_room_is_refused(
        self, tmp_path, sends,
    ):
        with db.get_db(_config(tmp_path).db_path) as conn:
            shared = _shared_web_room(conn)
        config = _config(tmp_path, routing={"notification": f"web:{shared},talk"})
        notifications.send_notification(config, "alice", "x", purpose="notification")
        assert shared not in sends["web"]

    def test_a_confirmation_prompt_is_refused_there_too(self, tmp_path, sends):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            shared = _shared_talk_room(conn)
        config.users["alice"].alerts_channel = shared
        delivered, _ = notifications.send_confirmation_prompt(config, "alice", "ok?")
        assert delivered is False and shared not in sends["talk"]

    def test_a_log_channel_that_became_shared_is_dropped(self, tmp_path):
        config = _config(tmp_path)
        with db.get_db(config.db_path) as conn:
            shared = _shared_talk_room(conn)
        config.users["alice"].log_channel = shared
        assert notifications.effective_log_destinations(config, "alice") == []

    def test_the_email_poller_never_routes_mail_into_a_shared_room(self, tmp_path):
        from istota.transport.routing import routed_notification_room

        with db.get_db(_config(tmp_path).db_path) as conn:
            shared = _shared_web_room(conn)
        config = _config(tmp_path, routing={"notification": f"web:{shared}"})
        with db.get_db(config.db_path) as conn:
            assert routed_notification_room(conn, config, "alice") is None

