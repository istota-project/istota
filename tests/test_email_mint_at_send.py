"""Email on rooms, stage 1: a thread the bot starts is a room from the send.

Every point that records a sent mail (`email send` / `email reply` through
the deferred file, a task result, a released draft) puts the mail in its
thread's room, so the first reply finds the room like any later one. A thread
sent before that existed mints at its first reply, with the bot's mail as
the room's first row.
"""

import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, EmailConfig, UserConfig
from istota.mail import drafts
from istota.scheduler_deferred import _process_deferred_sent_emails
from istota.skills.email import Email, EmailEnvelope, cmd_send
from istota.transport.email import threads
from istota.transport.email.inbound import poll_emails

HOST = "carol"
HOST_ADDR = "carol@test.com"
BOT = "bot@test.com"
ANA = "ana@ext.example"
BOB = "bob@ext.example"
SENT = "<sent-1@test.com>"


@pytest.fixture
def config(tmp_path, monkeypatch):
    path = tmp_path / "istota.db"
    db.init_db(path)
    config = Config()
    config.db_path = path
    config.temp_dir = tmp_path / "temp"
    config.temp_dir.mkdir()
    config.skills_dir = tmp_path / "skills"
    config.skills_dir.mkdir()
    config.bot_name = "Zorg"
    config.email = EmailConfig(
        enabled=True, imap_host="imap.test", imap_port=993,
        imap_user="user", imap_password="pass",
        smtp_host="smtp.test", smtp_port=587, bot_email=BOT,
        # The outbound gate has its own files; these are about where the
        # sent mail is recorded.
        outbound_approval_floor="off",
    )
    config.users = {
        HOST: UserConfig(email_addresses=[HOST_ADDR],
                         trusted_email_senders=["*@ext.example"]),
    }
    monkeypatch.setattr("istota.config.load_config", lambda *a, **k: config)
    return config


@pytest.fixture
def task(config):
    """A task in the host's private web room, the usual place a send is asked for."""
    with db.get_db(config.db_path) as conn:
        task_id = db.create_task(conn, prompt="email Ana about Saturday", user_id=HOST,
                                 source_type="web", conversation_token="web-carol")
        return db.get_task(conn, task_id)


@pytest.fixture
def deferred(tmp_path, monkeypatch, task):
    path = tmp_path / "deferred"
    path.mkdir()
    monkeypatch.setenv("ISTOTA_TASK_ID", str(task.id))
    monkeypatch.setenv("ISTOTA_USER_ID", HOST)
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(path))
    for key, value in {"SMTP_HOST": "smtp.test", "IMAP_HOST": "imap.test",
                       "IMAP_USER": "u", "IMAP_PASSWORD": "p", "SMTP_FROM": BOT}.items():
        monkeypatch.setenv(key, value)
    return path


def _send(config, task, deferred, *, to, cc=None, subject="Saturday",
          body="Are you free on Saturday?"):
    """`email send` in the sandbox, then the scheduler's replay of its file."""
    args = SimpleNamespace(to=to, subject=subject, body=body, body_file=None,
                           html=False, cc=cc, bcc=None, attach=None, reply_to=None)
    with patch("istota.skills.email.send_email", return_value=SENT):
        assert cmd_send(args)["status"] == "ok"
    _process_deferred_sent_emails(config, task, deferred)


_UID = [500]


def _poll(config, *, sender, to=(BOT,), cc=(), message_id, references=None,
          subject="Re: Saturday", body="Saturday works."):
    _UID[0] += 1
    uid = str(_UID[0])
    envelope = EmailEnvelope(id=uid, subject=subject, sender=sender,
                             date="Mon, 01 Jan 2026 12:00:00 +0000", is_read=False)
    email = Email(
        id=uid, subject=subject, sender=sender, date="Mon, 01 Jan 2026 12:00:00 +0000",
        body=body, attachments=[], message_id=message_id, references=references,
        to=tuple(to), cc=tuple(cc), authentication_results=None,
    )
    with (
        patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
        patch("istota.transport.email.inbound.read_email", return_value=email),
        patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        patch("istota.transport.email.inbound._deliver_confirmation_prompts"),
        patch("istota.transport.email.inbound._deliver_dmarc_alerts"),
    ):
        return poll_emails(config)


def _rows(config, sql, params=()):
    with db.get_db(config.db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _room(config, ref=SENT):
    with db.get_db(config.db_path) as conn:
        return db.resolve_room_token(conn, "email", ref)


def _people(config, token):
    return {r["surface_ref"]: r["kind"] for r in _rows(
        config, "SELECT surface_ref, kind FROM room_participants "
        "WHERE room_token = ? AND left_at IS NULL", (token,))}


def _messages(config, token):
    rows = _rows(config, "SELECT role, body, task_id, outgoing_mail FROM messages "
                 "WHERE room_token = ? ORDER BY id", (token,))
    for row in rows:
        row["outgoing_mail"] = json.loads(row["outgoing_mail"]) if row["outgoing_mail"] else None
    return rows


class TestASendMintsTheRoom:
    def test_a_send_with_cc_mints_a_room_with_the_mail_first(self, config, task, deferred):
        _send(config, task, deferred, to=ANA, cc=BOB)

        token = _room(config)
        assert token is not None
        with db.get_db(config.db_path) as conn:
            room = db.get_room(conn, token)
        assert room.user_id == HOST and room.origin == "email"
        assert room.name == "Saturday"
        assert _people(config, token) == {ANA: "guest", BOB: "guest"}
        assert _messages(config, token) == [{
            "role": "assistant", "body": "Are you free on Saturday?", "task_id": None,
            "outgoing_mail": {"to": [ANA], "cc": [BOB], "subject": "Saturday",
                              "state": "sent"},
        }]
        # Cc is recorded with To, the form a thread reply stores.
        assert _rows(config, "SELECT to_addr FROM sent_emails") == [
            {"to_addr": f"{ANA}, {BOB}"}]

    def test_a_send_to_the_users_own_address_mints_nothing(self, config, task, deferred):
        _send(config, task, deferred, to=HOST_ADDR)

        assert _rows(config, "SELECT token FROM rooms") == []
        assert len(_rows(config, "SELECT id FROM sent_emails")) == 1

    def test_the_first_reply_is_a_turn_in_that_room(self, config, task, deferred):
        _send(config, task, deferred, to=ANA, cc=BOB)
        token = _room(config)

        task_ids = _poll(config, sender=ANA, to=(BOT,), cc=(BOB,),
                         message_id="<a1@ext.example>", references=SENT)

        assert _rows(config, "SELECT routing_method FROM processed_emails") == [
            {"routing_method": "thread_room"}]
        assert len(_rows(config, "SELECT token FROM rooms")) == 1
        with db.get_db(config.db_path) as conn:
            reply = db.get_task(conn, task_ids[0])
        assert reply.conversation_token == token
        assert reply.output_target == "email"
        assert "Notify the user" not in reply.prompt
        assert [m["role"] for m in _messages(config, token)] == ["assistant", "user"]


class TestAThreadSentBeforeTheChange:
    def test_its_first_reply_mints_the_room_with_the_sent_row_first(self, config):
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(conn, user_id=HOST, message_id=SENT,
                                 to_addr=f"{ANA}, {BOB}", subject="Saturday",
                                 conversation_token="web-carol")

        task_ids = _poll(config, sender=ANA, to=(BOT,), cc=(BOB,),
                         message_id="<a1@ext.example>", references=SENT)

        token = _room(config)
        assert token is not None
        messages = _messages(config, token)
        assert [m["role"] for m in messages] == ["assistant", "user"]
        assert messages[0]["body"] == ""
        assert messages[0]["outgoing_mail"] == {
            "to": [ANA, BOB], "cc": [], "subject": "Saturday", "state": "sent"}
        with db.get_db(config.db_path) as conn:
            reply = db.get_task(conn, task_ids[0])
        assert reply.conversation_token == token
        assert reply.output_target == "email"
        assert reply.talk_delivery_token is None
        assert "Notify the user" not in reply.prompt
        assert "<email_content>" in reply.prompt

    def test_a_held_reply_still_mints_nothing(self, config):
        config.users[HOST].trusted_email_senders = []
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(conn, user_id=HOST, message_id=SENT,
                                 to_addr=ANA, subject="Saturday")

        _poll(config, sender="mallory@elsewhere.example", to=(BOT,), cc=(BOB,),
              message_id="<m1@elsewhere.example>", references=SENT)

        assert _rows(config, "SELECT token FROM rooms") == []


class TestEveryRecordingPoint:
    def test_a_released_draft_mints_the_room(self, config, task):
        with db.get_db(config.db_path) as conn:
            draft_id = drafts.hold(
                conn, user_id=HOST, task_id=task.id, room_token="web-carol",
                to_addrs=[ANA], cc_addrs=[BOB], bcc_addrs=["hidden@ext.example"],
                subject="Saturday", body="Are you free?", html=False,
                in_reply_to=None, references=None, attachments=[],
                origin_target=None, hold_reason="untrusted_recipient",
            )
        with patch("istota.skills.email.send_email", return_value=SENT):
            drafts.release(config, draft_id)

        token = _room(config)
        assert token is not None
        # Bcc is never a person on the thread.
        assert _people(config, token) == {ANA: "guest", BOB: "guest"}
        assert _messages(config, token)[0]["outgoing_mail"]["cc"] == [BOB]
        assert _rows(config, "SELECT to_addr FROM sent_emails") == [
            {"to_addr": f"{ANA}, {BOB}"}]

    @pytest.mark.asyncio
    async def test_a_reply_to_a_lone_correspondent_mints_the_room(self, config):
        from istota.transport.email.outbound import deliver_email_result

        task_ids = _poll(config, sender=ANA, to=("bot+carol@test.com",),
                         message_id="<a0@ext.example>", subject="Question")
        with db.get_db(config.db_path) as conn:
            asked = db.get_task(conn, task_ids[0])
        with patch("istota.transport.email.outbound.reply_to_email",
                   return_value="<re-1@test.com>"):
            assert await deliver_email_result(
                config, asked, json.dumps({"subject": "", "body": "Yes.", "format": "text"}),
            )

        token = _room(config, "<a0@ext.example>")
        assert token is not None
        assert _messages(config, token)[0]["body"] == "Yes."
        assert _people(config, token) == {ANA: "guest"}


class TestRegisteringIsBounded:
    def test_a_room_another_user_hosts_is_left_alone(self, config):
        config.users["dan"] = UserConfig(email_addresses=["dan@test.com"])
        with db.get_db(config.db_path) as conn:
            theirs = threads.register_sent_thread(
                conn, config, user_id="dan", message_id=SENT, to=[ANA])
            mine = threads.register_sent_thread(
                conn, config, user_id=HOST, message_id="<later@test.com>",
                references=SENT, to=["eve@ext.example"])
        assert theirs is not None and mine is None
        assert set(_people(config, theirs.token)) == {ANA}

    def test_a_failure_leaves_the_send_recorded_and_no_half_room(self, config, caplog):
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(conn, user_id=HOST, message_id=SENT, to_addr=ANA)
            with patch.object(threads.db, "mark_audience_baseline",
                              side_effect=RuntimeError("boom")):
                assert threads.register_sent_thread(
                    conn, config, user_id=HOST, message_id=SENT, to=[ANA]) is None

        assert len(_rows(config, "SELECT id FROM sent_emails")) == 1
        assert _rows(config, "SELECT token FROM rooms") == []
        assert _rows(config, "SELECT id FROM room_participants") == []
        assert "Could not register the thread" in caplog.text
