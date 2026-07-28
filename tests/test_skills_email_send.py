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

BOT = "bot@example.com"


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
        "alice": UserConfig(email_addresses=["alice@personal.com"]),
        "dana": UserConfig(email_addresses=["dana@personal.com"]),
    }
    monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
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
                       reply_to="desk@example.com")
        assert captured["msg"]["Reply-To"] == "desk@example.com"

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
            to=[BOT, "colleague@out.com", "bot+alice@example.com"],
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
        assert not any("bot@example.com" in c or "bot+" in c for c in cc)
        assert "client@out.com" not in cc

    def test_reply_to_other_users_mail_is_not_found(self, skill_env):
        orig = _mail("5", "x@out.com", to=["bot+dana@example.com"], message_id="<o@x>")
        args = MagicMock(id="5", body="ok", body_file=None, html=False,
                         attach=None, all=False, scope="all", command="reply")
        with patch("istota.skills.email.read_email", return_value=orig):
            res = cmd_reply(args)
        assert res["status"] == "not_found"

    def test_is_bot_address(self):
        assert _is_bot_address("bot@example.com", BOT)
        assert _is_bot_address("bot+alice@example.com", BOT)
        assert not _is_bot_address("someone@example.com", BOT)


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
        orig = _mail("5", "x@out.com", to=["bot+alice@example.com"], message_id="<o@x>")
        args = MagicMock(id="5", action="read", confirmed=True, scope="all")
        with patch("istota.skills.email.read_email", return_value=orig), \
             patch("istota.skills.email.mark_email", return_value=True) as me:
            res = cmd_mark(args)
        assert res["status"] == "ok"
        me.assert_called_once()

    def test_delete_confirmed_refuses_other_users_mail(self, skill_env):
        orig = _mail("5", "x@out.com", to=["bot+dana@example.com"], message_id="<o@x>")
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


# --- multipart/alternative (HTML briefing email) ---------------------------


def _capture_sent(fn):
    """Run ``fn`` with SMTP mocked; return the serialized EmailMessage."""
    captured = {}
    server = MagicMock()
    server.__enter__ = MagicMock(return_value=server)
    server.__exit__ = MagicMock(return_value=False)
    server.send_message.side_effect = (
        lambda m, to_addrs=None: captured.__setitem__("msg", m)
    )
    with patch("smtplib.SMTP", return_value=server), \
         patch("istota.skills.email._save_to_sent"):
        fn()
    return captured["msg"]


class TestMultipartAlternative:
    """``html_body`` turns a send into multipart/alternative (plain + HTML)."""

    def test_send_email_builds_both_parts(self, econf):
        msg = _capture_sent(lambda: send_email(
            to="a@out.com", subject="S", body="plain text", config=econf,
            html_body="<html><body><p>rich</p></body></html>",
        ))
        assert msg.get_content_type() == "multipart/alternative"
        types = [p.get_content_type() for p in msg.iter_parts()]
        assert types == ["text/plain", "text/html"]
        parts = list(msg.iter_parts())
        assert "plain text" in parts[0].get_content()
        assert "<p>rich</p>" in parts[1].get_content()

    def test_send_email_without_html_body_stays_single_part(self, econf):
        msg = _capture_sent(lambda: send_email(
            to="a@out.com", subject="S", body="plain text", config=econf,
        ))
        assert msg.get_content_type() == "text/plain"
        assert not msg.is_multipart()

    def test_blank_html_body_stays_single_part(self, econf):
        """An empty render (the renderer's failure signal) must not go multipart."""
        msg = _capture_sent(lambda: send_email(
            to="a@out.com", subject="S", body="plain", config=econf, html_body="",
        ))
        assert msg.get_content_type() == "text/plain"

    def test_multipart_with_attachment_keeps_both_alternatives(self, econf, tmp_path):
        f = tmp_path / "report.txt"
        f.write_text("data")
        msg = _capture_sent(lambda: send_email(
            to="a@out.com", subject="S", body="plain", config=econf,
            html_body="<html><body><p>rich</p></body></html>",
            attachments=[str(f)],
        ))
        assert msg.get_content_type() == "multipart/mixed"
        assert "report.txt" in [p.get_filename() for p in msg.iter_attachments()]
        body = msg.get_body(preferencelist=("html",))
        assert body is not None and "<p>rich</p>" in body.get_content()

    def test_reply_to_email_builds_both_parts(self, econf):
        from istota.skills.email import reply_to_email
        msg = _capture_sent(lambda: reply_to_email(
            to_addr="a@out.com", subject="Orig", body="plain text", config=econf,
            in_reply_to="<orig@x>",
            html_body="<html><body><p>rich</p></body></html>",
        ))
        assert msg.get_content_type() == "multipart/alternative"
        assert msg["Subject"] == "Re: Orig"
        assert msg["In-Reply-To"] == "<orig@x>"
        types = [p.get_content_type() for p in msg.iter_parts()]
        assert types == ["text/plain", "text/html"]

    def test_reply_to_email_without_html_body_stays_single_part(self, econf):
        from istota.skills.email import reply_to_email
        msg = _capture_sent(lambda: reply_to_email(
            to_addr="a@out.com", subject="Orig", body="plain", config=econf,
        ))
        assert msg.get_content_type() == "text/plain"

    def test_html_content_type_still_sends_single_part_html(self, econf):
        """The existing single-part HTML path (``content_type='html'``) is intact."""
        msg = _capture_sent(lambda: send_email(
            to="a@out.com", subject="S", body="<p>x</p>", config=econf,
            content_type="html",
        ))
        assert msg.get_content_type() == "text/html"
