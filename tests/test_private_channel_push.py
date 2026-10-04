"""One private channel and the push rule (email on rooms, stage 5; #625, #628).

A private note about an email thread is never mailed; a parked question's bell
push goes to ntfy and email only, never into a room; and an answer is parked
only when its final paragraph asks.
"""

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from istota import db
from istota.config import (
    Config, EmailConfig, NextcloudConfig, SchedulerConfig, TalkConfig, UserConfig,
)
from istota.notifications.resolvers import confirmation as confirmation_source
from istota.notifications.store import deliver_pending
from istota.rooms import private_replies

from .support.rooms import plain_talk_room

PARTICIPANTS_ALICE = [{"actorType": "users", "actorId": "alice"},
                      {"actorType": "users", "actorId": "bot"}]
QUESTION = "I need your confirmation before sending the invite. Reply yes or no."


@pytest.fixture
def config(tmp_path):
    path = tmp_path / "state.db"
    db.init_db(path)
    workspace = tmp_path / "mount"
    workspace.mkdir()
    return Config(
        db_path=path,
        temp_dir=tmp_path / "temp",
        workspace_path=workspace,
        nextcloud=NextcloudConfig(url="https://cloud.example.com", username="bot",
                                  app_password="secret"),
        talk=TalkConfig(enabled=True, bot_username="bot"),
        email=EmailConfig(enabled=True, bot_email="bot@example.com"),
        scheduler=SchedulerConfig(),
        users={"alice": UserConfig(display_name="Alice",
                                   email_addresses=["alice@example.com"]),
               "bob": UserConfig(display_name="Bob")},
    )


@pytest.fixture
def ntfy(monkeypatch):
    """Every ntfy push, as (user_id, message)."""
    seen = []

    def send(config, user_id, message, **kwargs):
        seen.append((user_id, message))
        return True

    monkeypatch.setattr("istota.notifications.delivery._send_ntfy", send)
    return seen


@pytest.fixture
def smtp():
    """The SMTP seam every mail the bot sends goes through."""
    with patch("istota.skills.email.send_email", return_value="<x@example.com>") as send:
        yield send


def _email_thread(conn, name="Thread"):
    token = db.register_room(conn, None, "alice", origin="email", name=name).token
    db.add_room_binding(conn, token, "email", "<root@example.com>")
    db.add_room_member(conn, token, "bob")
    return token


def _shared_talk(conn, name="Family"):
    shape = plain_talk_room(conn, "alice", token="groupref", name=name)
    db.add_room_member(conn, shape.canonical, "bob")
    return shape


def _rows(config, sql, params=()):
    with db.get_db(config.db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _run(config, task_id, result):
    from istota.scheduler import process_one_task

    with patch("istota.scheduler.execute_task", return_value=(True, result, None, None)):
        process_one_task(config)
    with db.get_db(config.db_path) as conn:
        return db.get_task(conn, task_id)


# ---------------------------------------------------------------------------
# The heads-up mail is gone
# ---------------------------------------------------------------------------


class TestNoHeadsUpMail:
    def test_a_private_note_about_an_email_thread_sends_no_mail(self, config, smtp):
        with db.get_db(config.db_path) as conn:
            parent = _email_thread(conn)
            web = db.create_web_chat_room(conn, "alice", "general").token
            delivery = private_replies.deliver_private(
                conn, config, user_id="alice", about_token=parent,
                kind="confirmation", reference="7:abc", body="Shall I?")
        assert delivery.dest.room_token == web
        delivered = asyncio.run(private_replies.send_private(config, delivery, body="Shall I?"))
        # A web room is read from the transcript: nothing was pushed, so the
        # park's own bell row is still owed.
        assert delivered is False
        smtp.assert_not_called()
        (row,) = _rows(config, "SELECT body FROM messages WHERE room_token=?", (web,))
        assert row["body"] == "Shall I?"

    def test_a_whisper_about_an_email_thread_with_no_private_room_goes_to_the_bell(
        self, config, smtp,
    ):
        with db.get_db(config.db_path) as conn:
            parent = _email_thread(conn)
            delivery = private_replies.deliver_private(
                conn, config, user_id="alice", about_token=parent,
                kind="whisper", reference="room-whisper:w1", body="Only for you.")
        assert delivery.dest is None and delivery.notice is not None
        with patch("istota.notifications.store.deliver_pending"):
            assert asyncio.run(private_replies.send_private(
                config, delivery, body="Only for you.")) is False
        smtp.assert_not_called()
        assert _rows(config, "SELECT title FROM notifications WHERE source='task_alert'") == [
            {"title": "Private note about Thread"}]


# ---------------------------------------------------------------------------
# A room-free push
# ---------------------------------------------------------------------------


class TestTheRoomFreePush:
    def test_a_confirmation_row_is_room_free(self, config):
        with db.get_db(config.db_path) as conn:
            result = confirmation_source.write(conn, "alice", task_id=1, title="t", body="b")
        assert result is not None and result.room_free is True

    def test_it_reaches_ntfy_and_never_the_alerts_room(self, config, ntfy, fake_talk):
        fake_talk.db_path = config.db_path
        with db.get_db(config.db_path) as conn:
            alerts = plain_talk_room(conn, "alice", name="alerts")
        config.users["alice"].routing = {"alert": f"talk:{alerts.talk_ref},ntfy"}
        with db.get_db(config.db_path) as conn:
            result = confirmation_source.write(conn, "alice", task_id=1,
                                               title="Approve?", body="b")
        deliver_pending(config, [result])
        assert [user for user, _ in ntfy] == ["alice"]
        assert fake_talk.calls == []
        assert _rows(config, "SELECT id FROM messages WHERE room_token=?",
                     (alerts.canonical,)) == []
        (row,) = _rows(config, "SELECT last_delivered_at FROM notifications")
        assert row["last_delivered_at"] is not None

    def test_the_control_an_ordinary_alert_still_reaches_talk(self, config, ntfy, fake_talk):
        from istota.notifications.resolvers import task_alert

        fake_talk.db_path = config.db_path
        with db.get_db(config.db_path) as conn:
            alerts = plain_talk_room(conn, "alice", name="alerts")
        config.users["alice"].routing = {"alert": f"talk:{alerts.talk_ref},ntfy"}
        with db.get_db(config.db_path) as conn:
            result = task_alert.write(conn, "alice", dedup_key="x", title="Heads up",
                                      body="b", severity="info", actionable=False)
        assert result.room_free is False
        deliver_pending(config, [result])
        assert fake_talk.calls_to(alerts.talk_ref, method="send_message")
        assert len(ntfy) == 1

    def test_with_neither_ntfy_nor_email_nothing_is_pushed_and_the_row_stands(
        self, config, ntfy, fake_talk,
    ):
        fake_talk.db_path = config.db_path
        with db.get_db(config.db_path) as conn:
            alerts = plain_talk_room(conn, "alice", name="alerts")
        config.users["alice"].routing = {"alert": f"talk:{alerts.talk_ref}"}
        with db.get_db(config.db_path) as conn:
            result = confirmation_source.write(conn, "alice", task_id=1, title="t", body="b")
        deliver_pending(config, [result])
        assert ntfy == [] and fake_talk.calls == []
        (row,) = _rows(config, "SELECT state, last_delivered_at FROM notifications")
        assert row == {"state": "open", "last_delivered_at": None}


# ---------------------------------------------------------------------------
# Through the scheduler's park
# ---------------------------------------------------------------------------


class TestThePark:
    def test_a_web_origin_park_reaches_ntfy_and_not_talk(self, config, ntfy, fake_talk):
        fake_talk.db_path = config.db_path
        with db.get_db(config.db_path) as conn:
            alerts = plain_talk_room(conn, "alice", name="alerts")
            web = db.create_web_chat_room(conn, "alice", "general").token
            ident = db.create_task(conn, prompt="send the invite", user_id="alice",
                                   source_type="web", conversation_token=web,
                                   output_target="room")
        config.users["alice"].routing = {"alert": f"talk:{alerts.talk_ref},ntfy"}
        task = _run(config, ident, QUESTION)
        assert task.status == "pending_confirmation"
        assert len(ntfy) == 1
        assert fake_talk.calls_to(alerts.talk_ref) == []

    def test_a_web_only_private_room_pushes_the_bell(self, config, monkeypatch, fake_talk):
        fake_talk.db_path = config.db_path
        delivered = []
        monkeypatch.setattr("istota.scheduler.deliver_pending",
                            lambda config, results: delivered.extend(r for r in results if r))
        with db.get_db(config.db_path) as conn:
            group = _shared_talk(conn)
            db.create_web_chat_room(conn, "alice", "general")
            ident = db.create_task(conn, prompt="invite them", user_id="alice",
                                   source_type="talk", conversation_token=group.canonical,
                                   is_group_chat=True)
        task = _run(config, ident, QUESTION)
        assert task.status == "pending_confirmation"
        (pushed,) = [r for r in delivered if r.notification_id]
        assert pushed.room_free is True

    def test_a_talk_private_room_that_delivers_pushes_nothing_more(
        self, config, monkeypatch, fake_talk,
    ):
        fake_talk.db_path = config.db_path
        monkeypatch.setattr("istota.nextcloud.talk.TalkClient.get_participants",
                            AsyncMock(return_value=PARTICIPANTS_ALICE))
        delivered = []
        monkeypatch.setattr("istota.scheduler.deliver_pending",
                            lambda config, results: delivered.extend(r for r in results if r))
        with db.get_db(config.db_path) as conn:
            group = _shared_talk(conn)
            private = plain_talk_room(conn, "alice", name="talk")
            ident = db.create_task(conn, prompt="invite them", user_id="alice",
                                   source_type="talk", conversation_token=group.canonical,
                                   is_group_chat=True)
        task = _run(config, ident, QUESTION)
        assert task.status == "pending_confirmation"
        assert fake_talk.calls_to(private.talk_ref, method="send_message")
        assert [r for r in delivered if r.notification_id] == []

    def test_an_answer_explaining_the_confirmation_card_completes(self, config, fake_talk):
        """#625: an answer about confirmations is not one."""
        fake_talk.db_path = config.db_path
        explanation = (
            "The card under that message is a confirmation. When a task needs "
            "your approval it asks \"Should I proceed?\" and shows Confirm and "
            "Discard buttons.\n\n"
            "> Please confirm the invite to Ana\n\n"
            "Clicking Confirm runs the action; Discard drops it. Nothing is "
            "waiting on you right now."
        )
        with db.get_db(config.db_path) as conn:
            web = db.create_web_chat_room(conn, "alice", "general").token
            ident = db.create_task(conn, prompt="what is this card?", user_id="alice",
                                   source_type="web", conversation_token=web,
                                   output_target="room")
        assert _run(config, ident, explanation).status == "completed"
