"""Stage 3 — quiet senders: filed silently (no task), after owner resolution,
before the confirmation gate."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from istota import db, user_profiles
from istota.config import Config
from istota.config import EmailConfig as AppEmailConfig
from istota.config import UserConfig
from istota.skills.email import Email, EmailEnvelope
from istota.transport.email.inbound import poll_emails

BOT = "bot@test.com"


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


def _config(db_path, tmp_path, **user_kw):
    cfg = Config()
    cfg.db_path = db_path
    cfg.temp_dir = tmp_path / "temp"
    cfg.temp_dir.mkdir(exist_ok=True)
    cfg.email = AppEmailConfig(
        enabled=True, imap_host="imap.test", imap_port=993, imap_user="u",
        imap_password="p", smtp_host="smtp.test", smtp_port=587, bot_email=BOT,
    )
    cfg.users = {"alice": UserConfig(email_addresses=["alice@personal.com"], **user_kw)}
    return cfg


def _env(sender, id="1", subject="Hi"):
    return EmailEnvelope(id=id, subject=subject, sender=sender,
                         date="Mon, 01 Jun 2026 00:00:00 +0000", is_read=False)


def _mail(sender, id="1", subject="Hi", to=("bot+alice@test.com",)):
    return Email(id=id, subject=subject, sender=sender,
                 date="Mon, 01 Jun 2026 00:00:00 +0000", body="body", attachments=[],
                 message_id="<m1@test.com>", references=None, to=to, cc=())


# --- is_quiet_email_sender -------------------------------------------------


class TestIsQuietEmailSender:
    def test_matches_pattern(self, db_path, tmp_path):
        cfg = _config(db_path, tmp_path, quiet_email_senders=["*@stratechery.com"])
        assert cfg.is_quiet_email_sender("alice", "ben@stratechery.com") is True
        assert cfg.is_quiet_email_sender("alice", "ben@other.com") is False

    def test_does_not_implicitly_match_own_address(self, db_path, tmp_path):
        # Unlike trusted senders, your own address is never implicitly quiet.
        cfg = _config(db_path, tmp_path, quiet_email_senders=[])
        assert cfg.is_quiet_email_sender("alice", "alice@personal.com") is False

    def test_unknown_user_is_false(self, db_path, tmp_path):
        cfg = _config(db_path, tmp_path, quiet_email_senders=["*@x.com"])
        assert cfg.is_quiet_email_sender("ghost", "a@x.com") is False


# --- poll_emails quiet branch ----------------------------------------------


def _run_poll(cfg, envelope, email):
    with (
        patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
        patch("istota.transport.email.inbound.read_email", return_value=email),
        patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        patch("istota.transport.email.inbound.ensure_user_directories_v2"),
        patch("istota.transport.email.inbound.upload_file_to_inbox_v2"),
    ):
        return poll_emails(cfg)


class TestQuietBranch:
    def test_quiet_sender_creates_no_task(self, db_path, tmp_path):
        cfg = _config(db_path, tmp_path, quiet_email_senders=["*@stratechery.com"])
        env = _env("ben@stratechery.com")
        mail = _mail("ben@stratechery.com", to=("bot+alice@test.com",))
        task_ids = _run_poll(cfg, env, mail)
        assert task_ids == []
        # Marked processed with routing_method="quiet", no task link.
        with db.get_db(db_path) as conn:
            assert db.is_email_processed(conn, "1") is True
            row = conn.execute(
                "SELECT routing_method, task_id FROM processed_emails WHERE email_id=?",
                ("1",),
            ).fetchone()
            assert row["routing_method"] == "quiet"
            assert row["task_id"] is None

    def test_quiet_mail_left_in_inbox(self, db_path, tmp_path):
        cfg = _config(db_path, tmp_path, quiet_email_senders=["*@stratechery.com"])
        env = _env("ben@stratechery.com")
        mail = _mail("ben@stratechery.com")
        with patch("istota.transport.email.inbound.list_emails", return_value=[env]), \
             patch("istota.transport.email.inbound.read_email", return_value=mail), \
             patch("istota.skills.email.delete_email") as del_mock:
            poll_emails(cfg)
        del_mock.assert_not_called()

    def test_non_quiet_sender_still_creates_task(self, db_path, tmp_path):
        cfg = _config(db_path, tmp_path, quiet_email_senders=["*@stratechery.com"])
        env = _env("someone@work.com")
        mail = _mail("someone@work.com", to=("bot+alice@test.com",))
        task_ids = _run_poll(cfg, env, mail)
        assert len(task_ids) == 1

    def test_unowned_quiet_matching_mail_is_discarded_not_filed(self, db_path, tmp_path):
        # A quiet pattern that also matches an *unowned* stranger: the discard
        # branch wins (owner resolution first), so it's "discarded", not "quiet".
        cfg = _config(db_path, tmp_path, quiet_email_senders=["*@stranger.com"])
        env = _env("nobody@stranger.com")
        mail = _mail("nobody@stranger.com", to=(BOT,))  # bare box, no plus, unowned
        task_ids = _run_poll(cfg, env, mail)
        assert task_ids == []
        with db.get_db(db_path) as conn:
            row = conn.execute(
                "SELECT routing_method FROM processed_emails WHERE email_id=?", ("1",),
            ).fetchone()
            assert row["routing_method"] == "discarded"

    def test_quiet_untrusted_sender_produces_no_gate(self, db_path, tmp_path):
        # A quiet plus-addressed (untrusted) sender must not raise a confirmation
        # gate for a task that will never exist.
        cfg = _config(db_path, tmp_path, quiet_email_senders=["*@stratechery.com"])
        env = _env("ben@stratechery.com")
        mail = _mail("ben@stratechery.com", to=("bot+alice@test.com",))
        with patch("istota.notifications.send_talk_confirmation") as conf, \
             patch("istota.transport.email.inbound.list_emails", return_value=[env]), \
             patch("istota.transport.email.inbound.read_email", return_value=mail), \
             patch("istota.transport.email.inbound.download_attachments", return_value=[]):
            task_ids = poll_emails(cfg)
        assert task_ids == []
        conf.assert_not_called()

    def test_own_address_not_quiet_still_processed_normally(self, db_path, tmp_path):
        cfg = _config(db_path, tmp_path, quiet_email_senders=[])
        env = _env("alice@personal.com")
        mail = _mail("alice@personal.com", to=(BOT,))
        task_ids = _run_poll(cfg, env, mail)
        assert len(task_ids) == 1  # sender-match → normal task, not filed


# --- profile persistence ---------------------------------------------------


class TestProfilePersistence:
    def test_round_trip(self, db_path):
        prof = user_profiles.UserProfile(
            user_id="alice", quiet_email_senders=["*@stratechery.com", "news@x.com"],
        )
        user_profiles.upsert_profile(db_path, prof)
        got = user_profiles.get_profile(db_path, "alice")
        assert got.quiet_email_senders == ["*@stratechery.com", "news@x.com"]

    def test_merge_into_user_config(self, db_path):
        prof = user_profiles.UserProfile(user_id="alice", quiet_email_senders=["*@x.com"])
        uc = UserConfig()
        user_profiles.merge_into_user_config(prof, uc)
        assert uc.quiet_email_senders == ["*@x.com"]
