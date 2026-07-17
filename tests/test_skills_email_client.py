"""Stage 1 — two-way email client read verbs + read scoping.

Covers the shared ownership resolver (``email_ownership``), the ``--scope``
leak regression, and the new read verbs (list/read/search/thread/attachments/
from-senders/newsletters), including untrusted framing and the IMAP timeout.
"""

from __future__ import annotations

from datetime import date
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import Config
from istota.config import EmailConfig as AppEmailConfig
from istota.config import UserConfig
from istota.email_ownership import (
    extract_user_from_recipient,
    owner_in_scope,
    resolve_email_owner,
)
from istota.skills.email import (
    Email,
    EmailConfig,
    EmailEnvelope,
    _msg_to_email,
    _msg_to_envelope,
    _thread_members,
    cmd_from_senders,
    cmd_list,
    cmd_newsletters,
    cmd_read,
    cmd_search,
    cmd_thread,
    list_emails,
    search_emails,
)

BOT = "bot@x.cynium.com"


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def app_config(tmp_path):
    """Real Config with two users + an initialised temp DB (for ownership)."""
    dbp = tmp_path / "istota.db"
    db.init_db(dbp)
    cfg = Config()
    cfg.db_path = dbp
    cfg.email = AppEmailConfig(enabled=True, bot_email=BOT)
    cfg.users = {
        "stefan": UserConfig(email_addresses=["stefan@personal.com"]),
        "dana": UserConfig(email_addresses=["dana@personal.com"]),
    }
    return cfg


@pytest.fixture
def skill_env(monkeypatch, app_config):
    """Wire load_config + _config_from_env + ISTOTA_USER_ID for the commands."""
    monkeypatch.setattr("istota.config.load_config", lambda *a, **k: app_config)
    monkeypatch.setenv("ISTOTA_USER_ID", "stefan")
    monkeypatch.setenv("SMTP_HOST", "smtp.test")
    monkeypatch.setenv("IMAP_HOST", "imap.test")
    monkeypatch.setenv("IMAP_USER", "u")
    monkeypatch.setenv("IMAP_PASSWORD", "p")
    monkeypatch.setenv("SMTP_FROM", BOT)
    return app_config


def _env(
    uid, sender, *, to=(), cc=(), references=None, subject="Subj",
    snippet="hello world", has_attachments=False, is_read=False,
):
    return EmailEnvelope(
        id=uid, subject=subject, sender=sender, date="Mon, 01 Jun 2026 00:00:00 +0000",
        is_read=is_read, snippet=snippet, has_attachments=has_attachments,
        to=tuple(to), cc=tuple(cc), references=references,
    )


def _mail(uid, sender, *, to=(), cc=(), references=None, in_reply_to=None,
          message_id=None, subject="Subj", body_text="hi", body_html=""):
    return Email(
        id=uid, subject=subject, sender=sender,
        date="Mon, 01 Jun 2026 00:00:00 +0000",
        body=body_text or body_html, attachments=[],
        message_id=message_id, references=references, to=tuple(to), cc=tuple(cc),
        body_text=body_text, body_html=body_html, in_reply_to=in_reply_to,
    )


def _mock_message(uid="1", subject="S", from_="a@x.com", to=None, cc=None,
                  text="body text", html="", flags=None, attachments=None,
                  headers=None):
    msg = MagicMock()
    msg.uid = uid
    msg.subject = subject
    msg.from_ = from_
    msg.to = to or ()
    msg.cc = cc or ()
    msg.date_str = "Mon, 01 Jun 2026 00:00:00 +0000"
    msg.text = text
    msg.html = html
    msg.flags = flags or []
    msg.attachments = attachments or []
    msg.headers = headers or {}
    return msg


def _mock_mailbox(messages):
    mb = MagicMock()
    mb.__enter__ = MagicMock(return_value=mb)
    mb.__exit__ = MagicMock(return_value=False)
    mb.fetch.return_value = messages
    return mb


# --------------------------------------------------------------------------
# Ownership resolution
# --------------------------------------------------------------------------


class TestOwnership:
    def test_plus_address_owns(self, app_config):
        env = _env("1", "stranger@out.com", to=["bot+stefan@x.cynium.com"])
        assert extract_user_from_recipient(app_config, env) == "stefan"
        assert resolve_email_owner(app_config, None, env) == "stefan"

    def test_plus_address_unknown_user_is_unowned(self, app_config):
        env = _env("1", "stranger@out.com", to=["bot+ghost@x.cynium.com"])
        assert resolve_email_owner(app_config, None, env) is None

    def test_sender_match_owns(self, app_config):
        env = _env("1", "dana@personal.com", to=[BOT])
        assert resolve_email_owner(app_config, None, env) == "dana"

    def test_bare_box_is_unowned(self, app_config):
        env = _env("1", "stranger@out.com", to=[BOT])
        assert resolve_email_owner(app_config, None, env) is None

    def test_thread_match_owns(self, app_config):
        with db.get_db(app_config.db_path) as conn:
            db.record_sent_email(
                conn, user_id="dana", message_id="<sent-1@x.cynium.com>",
                to_addr="client@out.com",
            )
            env = _env("1", "client@out.com", to=[BOT], references="<sent-1@x.cynium.com>")
            assert resolve_email_owner(app_config, conn, env) == "dana"

    def test_thread_match_skipped_without_conn(self, app_config):
        env = _env("1", "client@out.com", to=[BOT], references="<sent-1@x.cynium.com>")
        # No conn → thread arm skipped → looks unowned.
        assert resolve_email_owner(app_config, None, env) is None

    @pytest.mark.parametrize("scope,owner,expected", [
        ("mine", "stefan", True), ("mine", "dana", False), ("mine", None, False),
        ("shared", None, True), ("shared", "stefan", False), ("shared", "dana", False),
        ("all", "stefan", True), ("all", None, True), ("all", "dana", False),
    ])
    def test_owner_in_scope(self, scope, owner, expected):
        assert owner_in_scope(owner, scope, "stefan") is expected


# --------------------------------------------------------------------------
# Scope filtering — the leak regression
# --------------------------------------------------------------------------


class TestScopeLeak:
    def _inbox(self):
        return [
            _env("mine-plus", "stranger@out.com", to=["bot+stefan@x.cynium.com"]),
            _env("mine-sender", "stefan@personal.com", to=[BOT]),
            _env("dana-plus", "stranger@out.com", to=["bot+dana@x.cynium.com"]),
            _env("dana-sender", "dana@personal.com", to=[BOT]),
            _env("bare", "stranger@out.com", to=[BOT]),
        ]

    def _ids(self, result):
        return {e["id"] for e in result["emails"]}

    def test_scope_mine(self, skill_env):
        args = MagicMock(scope="mine", limit=20, since=None, from_addr=None, unread=False)
        with patch("istota.skills.email.list_emails", return_value=self._inbox()):
            res = cmd_list(args)
        assert res["status"] == "ok"
        assert self._ids(res) == {"mine-plus", "mine-sender"}

    def test_scope_shared(self, skill_env):
        args = MagicMock(scope="shared", limit=20, since=None, from_addr=None, unread=False)
        with patch("istota.skills.email.list_emails", return_value=self._inbox()):
            res = cmd_list(args)
        assert self._ids(res) == {"bare"}

    def test_scope_all_never_returns_other_users_mail(self, skill_env):
        args = MagicMock(scope="all", limit=20, since=None, from_addr=None, unread=False)
        with patch("istota.skills.email.list_emails", return_value=self._inbox()):
            res = cmd_list(args)
        ids = self._ids(res)
        assert ids == {"mine-plus", "mine-sender", "bare"}
        assert "dana-plus" not in ids
        assert "dana-sender" not in ids

    def test_swapped_users(self, skill_env, monkeypatch):
        monkeypatch.setenv("ISTOTA_USER_ID", "dana")
        args = MagicMock(scope="mine", limit=20, since=None, from_addr=None, unread=False)
        with patch("istota.skills.email.list_emails", return_value=self._inbox()):
            res = cmd_list(args)
        assert self._ids(res) == {"dana-plus", "dana-sender"}

    def test_thread_matched_mail_is_mine_and_invisible_to_others(self, skill_env, app_config):
        with db.get_db(app_config.db_path) as conn:
            db.record_sent_email(
                conn, user_id="stefan", message_id="<s1@x.cynium.com>",
                to_addr="client@out.com",
            )
        inbox = [_env("emissary", "client@out.com", to=[BOT], references="<s1@x.cynium.com>")]
        # As stefan: it's mine.
        args = MagicMock(scope="mine", limit=20, since=None, from_addr=None, unread=False)
        with patch("istota.skills.email.list_emails", return_value=inbox):
            assert self._ids(cmd_list(args)) == {"emissary"}
        # As dana with scope=all: never surfaces (owned by stefan via thread).
        args_all = MagicMock(scope="all", limit=20, since=None, from_addr=None, unread=False)
        with patch("istota.skills.email.list_emails", return_value=inbox):
            with patch.dict("os.environ", {"ISTOTA_USER_ID": "dana"}):
                assert self._ids(cmd_list(args_all)) == set()

    def test_shared_scope_fails_closed_without_db(self, skill_env, monkeypatch):
        # DB open fails → shared/all must refuse rather than risk a leak.
        monkeypatch.setattr(
            "istota.db.get_db",
            MagicMock(side_effect=RuntimeError("db down")),
        )
        args = MagicMock(scope="all", limit=20, since=None, from_addr=None, unread=False)
        with patch("istota.skills.email.list_emails", return_value=self._inbox()):
            res = cmd_list(args)
        assert res["status"] == "error"
        assert "ownership" in res["error"]


# --------------------------------------------------------------------------
# Low-level mapping + criteria
# --------------------------------------------------------------------------


class TestMapping:
    def test_envelope_snippet_and_has_attachments(self):
        att = MagicMock(filename="report.pdf")
        msg = _mock_message(text="  Hello\n  world  ", attachments=[att])
        env = _msg_to_envelope(msg)
        assert env.snippet == "Hello world"
        assert env.has_attachments is True

    def test_envelope_snippet_from_html_only(self):
        msg = _mock_message(text="", html="<p>Hi <b>there</b></p>")
        env = _msg_to_envelope(msg)
        assert "Hi" in env.snippet and "<" not in env.snippet

    def test_read_returns_plain_and_html(self):
        msg = _mock_message(
            text="plain part", html="<p>html part</p>",
            headers={"message-id": ("<m@x>",), "in-reply-to": ("<p@x>",)},
        )
        mail = _msg_to_email(msg)
        assert mail.body_text == "plain part"
        assert mail.body_html == "<p>html part</p>"
        assert mail.in_reply_to == "<p@x>"

    def test_read_attachment_manifest(self):
        att = MagicMock(filename="a.pdf", size=123, content_type="application/pdf")
        msg = _mock_message(attachments=[att])
        mail = _msg_to_email(msg)
        assert mail.attachment_manifest == [
            {"filename": "a.pdf", "size": 123, "content_type": "application/pdf"}
        ]

    def test_list_criteria_pushed_down(self):
        cfg = EmailConfig(imap_host="h", imap_port=993, imap_user="u",
                          imap_password="p", smtp_host="s", smtp_port=587)
        mb = _mock_mailbox([_mock_message()])
        crit = object()
        with patch("istota.skills.email._get_mailbox", return_value=mb):
            list_emails(config=cfg, criteria=crit, limit=7)
        args, kwargs = mb.fetch.call_args
        assert args[0] is crit
        assert kwargs["limit"] == 7

    def test_search_passes_query_verbatim(self):
        cfg = EmailConfig(imap_host="h", imap_port=993, imap_user="u",
                          imap_password="p", smtp_host="s", smtp_port=587)
        mb = _mock_mailbox([_mock_message()])
        with patch("istota.skills.email._get_mailbox", return_value=mb):
            search_emails('FROM "a@x" SUBJECT "invoice"', config=cfg)
        args, _ = mb.fetch.call_args
        assert args[0] == 'FROM "a@x" SUBJECT "invoice"'

    def test_search_empty_query_raises(self):
        cfg = EmailConfig(imap_host="h", imap_port=993, imap_user="u",
                          imap_password="p", smtp_host="s", smtp_port=587)
        with pytest.raises(ValueError):
            search_emails("   ", config=cfg)

    def test_imap_timeout_passed_to_mailbox(self):
        from istota.skills.email import _get_mailbox
        cfg = EmailConfig(imap_host="h", imap_port=993, imap_user="u",
                          imap_password="p", smtp_host="s", smtp_port=587,
                          imap_timeout=17)
        with patch("istota.skills.email.MailBox") as MB:
            _get_mailbox(cfg)
        _, kwargs = MB.call_args
        assert kwargs["timeout"] == 17


# --------------------------------------------------------------------------
# list filters build server-side criteria
# --------------------------------------------------------------------------


class TestListFilters:
    def test_since_from_unread_build_criteria(self, skill_env):
        captured = {}

        def fake_list(*, folder, limit, config, criteria):
            captured["criteria"] = criteria
            return []

        args = MagicMock(scope="all", limit=5, since="2026-01-01",
                         from_addr="alice@x.com", unread=True)
        with patch("istota.skills.email.list_emails", side_effect=fake_list):
            cmd_list(args)
        crit_str = str(captured["criteria"])
        assert "SINCE 1-Jan-2026" in crit_str
        assert 'FROM "alice@x.com"' in crit_str
        assert "UNSEEN" in crit_str

    def test_scope_mine_pushes_ownership_server_side(self, skill_env):
        # --scope mine must not fetch the whole INBOX window then filter — it
        # pushes TO bot+<user>@ OR FROM <user addrs> down to the server so a busy
        # shared box can't truncate the caller's mail out of the window.
        captured = {}

        def fake_list(*, folder, limit, config, criteria):
            captured["criteria"] = str(criteria)
            return []

        args = MagicMock(scope="mine", limit=5, since=None, from_addr=None, unread=False)
        with patch("istota.skills.email.list_emails", side_effect=fake_list):
            cmd_list(args)
        assert 'TO "bot+stefan@x.cynium.com"' in captured["criteria"]
        assert 'FROM "stefan@personal.com"' in captured["criteria"]
        assert "OR" in captured["criteria"]


class TestParseSince:
    def test_iso_date(self):
        from istota.skills.email import _parse_since
        assert _parse_since("2026-01-15") == date(2026, 1, 15)

    def test_relative_days_requires_d_suffix(self):
        from istota.skills.email import _parse_since
        assert _parse_since("7d") is not None
        # A bare year must NOT be read as a day-count — it errors.
        with pytest.raises(ValueError):
            _parse_since("2026")


# --------------------------------------------------------------------------
# read / search / from-senders / newsletters / thread commands
# --------------------------------------------------------------------------


class TestReadVerb:
    def test_read_scoped_and_framed(self, skill_env):
        mail = _mail("9", "stranger@out.com", to=["bot+stefan@x.cynium.com"],
                     body_text="secret plan", body_html="<p>secret plan</p>")
        args = MagicMock(scope="all", id="9")
        with patch("istota.skills.email.read_email", return_value=mail):
            res = cmd_read(args)
        assert res["status"] == "ok"
        assert res["untrusted"] is True
        assert "UNTRUSTED EMAIL CONTENT" in res["email"]["body"]
        assert "secret plan" in res["email"]["body"]

    def test_read_other_users_mail_is_not_found(self, skill_env):
        mail = _mail("9", "stranger@out.com", to=["bot+dana@x.cynium.com"])
        args = MagicMock(scope="all", id="9")
        with patch("istota.skills.email.read_email", return_value=mail):
            res = cmd_read(args)
        assert res["status"] == "not_found"

    def test_read_missing_is_not_found(self, skill_env):
        args = MagicMock(scope="all", id="404")
        with patch("istota.skills.email.read_email", side_effect=RuntimeError("nope")):
            res = cmd_read(args)
        assert res["status"] == "not_found"


class TestSearchVerb:
    def test_search_error_propagates_not_silent(self, skill_env):
        from istota.skills.email import main
        # A malformed IMAP criteria raises inside search_emails → error envelope.
        with patch("istota.skills.email.search_emails", side_effect=RuntimeError("BAD SEARCH")):
            with pytest.raises(SystemExit) as exc:
                main(["search", "not-a-real-criteria"])
        assert exc.value.code == 1


class TestFromSenders:
    def test_server_side_no_truncation(self, skill_env):
        captured = {}

        def fake_list(*, folder, limit, config, criteria):
            captured["limit"] = limit
            captured["criteria"] = str(criteria)
            return [_env("1", "news@stratechery.com", to=[BOT])]

        args = MagicMock(scope="shared", senders="news@stratechery.com,bloomberg.com",
                         since="7d", limit=0)
        with patch("istota.skills.email.list_emails", side_effect=fake_list):
            res = cmd_from_senders(args)
        # limit 0 → None (all matching), and it's a server-side FROM criteria.
        assert captured["limit"] is None
        assert "FROM" in captured["criteria"]
        assert res["status"] == "ok"

    def test_requires_senders(self, skill_env):
        args = MagicMock(scope="all", senders="", since=None, limit=0)
        res = cmd_from_senders(args)
        assert res["status"] == "error"


class TestNewsletters:
    def test_requires_sources(self, skill_env):
        args = MagicMock(scope="all", sources="", since=None, limit=0)
        res = cmd_newsletters(args)
        assert res["status"] == "error"

    def test_delegates_to_from_senders(self, skill_env):
        args = MagicMock(scope="shared", sources="a@x.com", since=None, limit=0)
        with patch("istota.skills.email.list_emails", return_value=[]):
            res = cmd_newsletters(args)
        assert res["status"] == "ok"


class TestThreadWalk:
    def test_walks_references_not_subject(self, skill_env):
        # Root + one real reply (shares message-id chain) + an unrelated
        # same-subject message that must NOT be merged.
        root = _mail("1", "client@out.com", to=[BOT], subject="Project",
                     message_id="<root@x>")
        reply = _mail("2", "client@out.com", to=[BOT], subject="Re: Project",
                      message_id="<reply@x>", references="<root@x>")
        unrelated = _mail("3", "other@out.com", to=[BOT], subject="Project",
                          message_id="<other@x>")
        members = _thread_members(root, [reply, unrelated])
        ids = [m.id for m in members]
        assert ids == ["1", "2"]  # ordered, unrelated excluded

    def test_thread_command_scoped(self, skill_env):
        root = _mail("1", "stranger@out.com", to=["bot+stefan@x.cynium.com"],
                     message_id="<root@x>", body_text="root body")
        reply = _mail("2", "stranger@out.com", to=["bot+stefan@x.cynium.com"],
                      message_id="<reply@x>", references="<root@x>", body_text="reply body")
        args = MagicMock(scope="all", id="1", window=200)
        with patch("istota.skills.email.read_email", return_value=root), \
             patch("istota.skills.email.fetch_emails_full", return_value=[root, reply]):
            res = cmd_thread(args)
        assert res["status"] == "ok"
        assert [m["id"] for m in res["messages"]] == ["1", "2"]
        assert "UNTRUSTED EMAIL CONTENT" in res["messages"][0]["body"]
