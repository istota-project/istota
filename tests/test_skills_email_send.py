"""Stage 2 — richer send (cc/bcc/attach/reply-to), reply/reply-all, gated mark/delete."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import Config
from istota.config import EmailConfig as AppEmailConfig
from istota.config import UserConfig
from istota.skills.email import (
    Email,
    EmailConfig,
    _is_bot_address,
    _recipients,
    cmd_delete,
    cmd_mark,
    cmd_reply,
    cmd_send,
    mark_email,
    send_email,
)

BOT = "bot@x.cynium.com"


@pytest.fixture
def econf():
    return EmailConfig(
        imap_host="imap.test", imap_port=993, imap_user="u", imap_password="p",
        smtp_host="smtp.test", smtp_port=587, bot_email=BOT,
    )


@pytest.fixture
def skill_env(monkeypatch, tmp_path):
    dbp = tmp_path / "istota.db"
    db.init_db(dbp)
    cfg = Config()
    cfg.db_path = dbp
    cfg.email = AppEmailConfig(enabled=True, bot_email=BOT)
    cfg.users = {
        "stefan": UserConfig(email_addresses=["stefan@personal.com"]),
        "dana": UserConfig(email_addresses=["dana@personal.com"]),
    }
    monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
    monkeypatch.setenv("ISTOTA_USER_ID", "stefan")
    for k, v in {"SMTP_HOST": "smtp.test", "IMAP_HOST": "imap.test",
                 "IMAP_USER": "u", "IMAP_PASSWORD": "p", "SMTP_FROM": BOT}.items():
        monkeypatch.setenv(k, v)
    return cfg


def _mail(uid, sender, *, to=(), cc=(), message_id=None, references=None, subject="Subj"):
    return Email(
        id=uid, subject=subject, sender=sender, date="Mon, 01 Jun 2026 00:00:00 +0000",
        body="body", attachments=[], message_id=message_id, references=references,
        to=tuple(to), cc=tuple(cc), body_text="body",
    )


# --- send enhancements -----------------------------------------------------


class TestSend:
    def test_bcc_not_transmitted_but_recipient_included(self, econf):
        captured = {}

        def fake_send(server_msg, to_addrs=None):
            captured["msg"] = server_msg
            captured["to_addrs"] = to_addrs

        server = MagicMock()
        server.__enter__ = MagicMock(return_value=server)
        server.__exit__ = MagicMock(return_value=False)
        server.send_message.side_effect = fake_send
        with patch("smtplib.SMTP", return_value=server), \
             patch("istota.skills.email._save_to_sent"):
            send_email(
                to="a@out.com", subject="Hi", body="b", config=econf,
                cc="c@out.com", bcc="secret@out.com",
            )
        # Bcc stripped from headers, but present in envelope recipients.
        assert captured["msg"]["Bcc"] is None
        assert "secret@out.com" in captured["to_addrs"]
        assert "a@out.com" in captured["to_addrs"]
        assert "c@out.com" in captured["to_addrs"]
        assert captured["msg"]["Cc"] == "c@out.com"

    def test_attachments_added(self, econf, tmp_path):
        f = tmp_path / "report.txt"
        f.write_text("data")
        captured = {}

        def fake_send(server_msg, to_addrs=None):
            captured["msg"] = server_msg

        server = MagicMock()
        server.__enter__ = MagicMock(return_value=server)
        server.__exit__ = MagicMock(return_value=False)
        server.send_message.side_effect = fake_send
        with patch("smtplib.SMTP", return_value=server), \
             patch("istota.skills.email._save_to_sent"):
            send_email(to="a@out.com", subject="S", body="b", config=econf,
                       attachments=[str(f)])
        names = [p.get_filename() for p in captured["msg"].iter_attachments()]
        assert "report.txt" in names

    def test_reply_to_header(self, econf):
        captured = {}
        server = MagicMock()
        server.__enter__ = MagicMock(return_value=server)
        server.__exit__ = MagicMock(return_value=False)
        server.send_message.side_effect = lambda m, to_addrs=None: captured.__setitem__("msg", m)
        with patch("smtplib.SMTP", return_value=server), \
             patch("istota.skills.email._save_to_sent"):
            send_email(to="a@out.com", subject="S", body="b", config=econf,
                       reply_to="desk@x.cynium.com")
        assert captured["msg"]["Reply-To"] == "desk@x.cynium.com"

    def test_recipients_dedup(self):
        assert _recipients("a@x.com", ["a@x.com", "b@y.com"], None) == ["a@x.com", "b@y.com"]

    def test_cmd_send_passes_options(self, skill_env):
        args = MagicMock(to="a@out.com", subject="S", body="hi", body_file=None,
                         html=False, cc="c@out.com", bcc="d@out.com",
                         attach=["/tmp/x.txt"], reply_to="r@x.com")
        with patch("istota.skills.email.send_email", return_value="<mid@x>") as se, \
             patch("istota.skills.email._write_deferred_sent_email"):
            res = cmd_send(args)
        assert res["status"] == "ok"
        _, kwargs = se.call_args
        assert kwargs["cc"] == ["c@out.com"]
        assert kwargs["bcc"] == ["d@out.com"]
        assert kwargs["attachments"] == ["/tmp/x.txt"]
        assert kwargs["reply_to"] == "r@x.com"

    def test_cmd_send_echoes_message_id(self, skill_env):
        """The ok envelope carries the sent Message-ID so 'sent' is backed by
        a concrete identifier the agent can quote (ISSUE-175)."""
        args = MagicMock(to="a@out.com", subject="S", body="hi", body_file=None,
                         html=False, cc=None, bcc=None, attach=None, reply_to=None)
        with patch("istota.skills.email.send_email", return_value="<mid@x>"), \
             patch("istota.skills.email._write_deferred_sent_email"):
            res = cmd_send(args)
        assert res["message_id"] == "<mid@x>"


# --- reply / reply-all -----------------------------------------------------


class TestReply:
    def test_reply_threads_from_fetched_message(self, skill_env):
        orig = _mail("5", "client@out.com", to=[BOT], message_id="<orig@x>",
                     references="<older@x>", subject="Project")
        args = MagicMock(id="5", body="thanks", body_file=None, html=False,
                         attach=None, all=False, scope="all", command="reply")
        with patch("istota.skills.email.read_email", return_value=orig), \
             patch("istota.skills.email.send_email", return_value="<new@x>") as se, \
             patch("istota.skills.email._write_deferred_sent_email"):
            res = cmd_reply(args)
        assert res["status"] == "ok"
        assert res["to"] == "client@out.com"
        assert res["message_id"] == "<new@x>"
        _, kwargs = se.call_args
        assert kwargs["in_reply_to"] == "<orig@x>"
        assert "<orig@x>" in kwargs["references"] and "<older@x>" in kwargs["references"]
        assert kwargs["subject"] == "Re: Project"

    def test_reply_all_includes_others_excludes_bot_and_self(self, skill_env):
        orig = _mail(
            "5", "client@out.com",
            to=[BOT, "colleague@out.com", "bot+stefan@x.cynium.com"],
            cc=["cc1@out.com", "client@out.com"], message_id="<orig@x>",
        )
        args = MagicMock(id="5", body="ok", body_file=None, html=False,
                         attach=None, all=True, scope="all", command="reply")
        with patch("istota.skills.email.read_email", return_value=orig), \
             patch("istota.skills.email.send_email", return_value="<new@x>") as se, \
             patch("istota.skills.email._write_deferred_sent_email"):
            res = cmd_reply(args)
        cc = res["cc"]
        assert "colleague@out.com" in cc
        assert "cc1@out.com" in cc
        # bot base + plus-address + the original sender (already the To) excluded
        assert not any("bot@x.cynium.com" in c or "bot+" in c for c in cc)
        assert "client@out.com" not in cc

    def test_reply_to_other_users_mail_is_not_found(self, skill_env):
        orig = _mail("5", "x@out.com", to=["bot+dana@x.cynium.com"], message_id="<o@x>")
        args = MagicMock(id="5", body="ok", body_file=None, html=False,
                         attach=None, all=False, scope="all", command="reply")
        with patch("istota.skills.email.read_email", return_value=orig):
            res = cmd_reply(args)
        assert res["status"] == "not_found"

    def test_is_bot_address(self):
        assert _is_bot_address("bot@x.cynium.com", BOT)
        assert _is_bot_address("bot+stefan@x.cynium.com", BOT)
        assert not _is_bot_address("someone@x.cynium.com", BOT)


# --- gated mark / delete ---------------------------------------------------


class TestGatedOps:
    def test_mark_refuses_without_confirmed(self, skill_env):
        args = MagicMock(id="5", action="read", confirmed=False)
        res = cmd_mark(args)
        assert res["status"] == "error"
        assert res["needs_confirmation"] is True

    def test_delete_refuses_without_confirmed(self, skill_env):
        args = MagicMock(id="5", confirmed=False)
        res = cmd_delete(args)
        assert res["status"] == "error"
        assert res["needs_confirmation"] is True

    def test_mark_confirmed_on_own_mail(self, skill_env):
        orig = _mail("5", "x@out.com", to=["bot+stefan@x.cynium.com"], message_id="<o@x>")
        args = MagicMock(id="5", action="read", confirmed=True, scope="all")
        with patch("istota.skills.email.read_email", return_value=orig), \
             patch("istota.skills.email.mark_email", return_value=True) as me:
            res = cmd_mark(args)
        assert res["status"] == "ok"
        me.assert_called_once()

    def test_delete_confirmed_refuses_other_users_mail(self, skill_env):
        orig = _mail("5", "x@out.com", to=["bot+dana@x.cynium.com"], message_id="<o@x>")
        args = MagicMock(id="5", confirmed=True, scope="all")
        with patch("istota.skills.email.read_email", return_value=orig), \
             patch("istota.skills.email.delete_email", return_value=True) as de:
            res = cmd_delete(args)
        assert res["status"] == "not_found"
        de.assert_not_called()

    def test_mark_email_flag_mapping(self, econf):
        mb = MagicMock()
        mb.__enter__ = MagicMock(return_value=mb)
        mb.__exit__ = MagicMock(return_value=False)
        with patch("istota.skills.email._get_mailbox", return_value=mb):
            mark_email("5", "unread", config=econf)
        args, _ = mb.flag.call_args
        assert args[0] == "5"
        assert args[2] is False  # unread → clear \Seen

    def test_mark_email_invalid_action(self, econf):
        with pytest.raises(ValueError):
            mark_email("5", "bogus", config=econf)


class TestGatedViaMain:
    def test_delete_without_confirmed_exits_nonzero(self, skill_env):
        from istota.skills.email import main
        with pytest.raises(SystemExit) as exc:
            main(["delete", "5"])
        assert exc.value.code == 1
