"""Tests for the EmailTransport inbound body (``transport/email/inbound.py``:
``poll_emails`` + routing precedence + confirmation gate) and the shared email
helpers it depends on (``istota.email_support``: subject normalization, thread
id, config adapter, IMAP cleanup)."""

import json
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import Config, EmailConfig as AppEmailConfig, UserConfig
from istota.email_ownership import thread_reply_from_correspondent
from istota.email_support import (
    cleanup_old_emails,
    compute_thread_id,
    get_email_config,
    normalize_subject,
)
from istota.transport.email import inbound as inbound_module
from istota.transport.email.inbound import (
    _dmarc_result,
    _extract_user_from_recipient,
    poll_emails,
)
from istota.skills.email import Email, EmailConfig, EmailEnvelope


@pytest.fixture(autouse=True)
def _clear_volume_state():
    """Reset the poller's in-process volume counters between tests.

    `_prompt_counts` collapses confirmation prompts past a few per
    (user, sender) per window (ISSUE-250). It is module-level and the window is
    an hour, so without this a test that expects a prompt fails purely because
    earlier tests in the same worker process already spent the sender's budget.
    Module-scoped rather than per-class: any test here that drives the gate
    spends it.
    """
    inbound_module._reset_volume_state()
    yield
    inbound_module._reset_volume_state()


@pytest.fixture
def db_path(tmp_path):
    """Create and initialize a temporary SQLite database."""
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def make_config(db_path, tmp_path):
    """Create a Config object with tmp paths and test DB."""
    def _make(**overrides):
        config = Config()
        config.db_path = db_path
        config.temp_dir = tmp_path / "temp"
        config.temp_dir.mkdir(exist_ok=True)
        config.skills_dir = tmp_path / "skills"
        config.skills_dir.mkdir(exist_ok=True)
        for key, val in overrides.items():
            setattr(config, key, val)
        return config
    return _make


def _email_config():
    """Return a standard test AppEmailConfig."""
    return AppEmailConfig(
        enabled=True,
        imap_host="imap.test",
        imap_port=993,
        imap_user="user",
        imap_password="pass",
        smtp_host="smtp.test",
        smtp_port=587,
        bot_email="bot@test.com",
    )


def _envelope(id="1", subject="Hello", sender="alice@test.com", date="Mon, 01 Jan 2026 10:00:00 +0000"):
    return EmailEnvelope(id=id, subject=subject, sender=sender, date=date, is_read=False)


def _email(id="1", subject="Hello", sender="alice@test.com", body="Hi there",
           to=("bot@test.com",), cc=(), authentication_results=None):
    return Email(
        id=id, subject=subject, sender=sender,
        date="Mon, 01 Jan 2026 10:00:00 +0000",
        body=body, attachments=[],
        message_id="<msg1@test.com>", references=None,
        to=to, cc=cc, authentication_results=authentication_results,
    )


# =============================================================================
# TestNormalizeSubject
# =============================================================================


class TestNormalizeSubject:
    def test_basic(self):
        assert normalize_subject("Hello World") == "hello world"

    def test_strip_re_prefix(self):
        assert normalize_subject("Re: Hello") == "hello"

    def test_strip_fwd_prefix(self):
        assert normalize_subject("Fwd: Hello") == "hello"

    def test_strip_multiple_prefixes(self):
        assert normalize_subject("Re: Fwd: Re: Hello") == "hello"

    def test_case_insensitive(self):
        assert normalize_subject("RE: FWD: Hello") == "hello"
        assert normalize_subject("Fw: Hello") == "hello"

    def test_normalize_whitespace(self):
        assert normalize_subject("  Hello   World  ") == "hello world"

    def test_lowercase(self):
        assert normalize_subject("IMPORTANT Meeting") == "important meeting"


# =============================================================================
# TestComputeThreadId
# =============================================================================


class TestComputeThreadId:
    def test_deterministic(self):
        id1 = compute_thread_id("Hello", ["a@test.com", "b@test.com"])
        id2 = compute_thread_id("Hello", ["a@test.com", "b@test.com"])
        assert id1 == id2

    def test_length_16(self):
        result = compute_thread_id("Hello", ["a@test.com"])
        assert len(result) == 16

    def test_sorted_participants(self):
        id1 = compute_thread_id("Hello", ["b@test.com", "a@test.com"])
        id2 = compute_thread_id("Hello", ["a@test.com", "b@test.com"])
        assert id1 == id2

    def test_normalized_subject(self):
        id1 = compute_thread_id("Re: Hello", ["a@test.com"])
        id2 = compute_thread_id("Hello", ["a@test.com"])
        assert id1 == id2

    def test_different_subjects_different_ids(self):
        id1 = compute_thread_id("Hello", ["a@test.com"])
        id2 = compute_thread_id("Goodbye", ["a@test.com"])
        assert id1 != id2


# =============================================================================
# TestPollEmails
# =============================================================================


class TestPollEmails:
    def test_creates_task_for_known_sender(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope()
        email = _email()

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.transport.email.inbound.ensure_user_directories_v2"),
            patch("istota.transport.email.inbound.upload_file_to_inbox_v2"),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

        # Verify the task was created in the database
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task is not None
            assert task.user_id == "alice"
            assert task.source_type == "email"
            assert "alice@test.com" in task.prompt

    def test_skips_processed_email(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope()

        # Pre-mark the email as processed
        with db.get_db(config.db_path) as conn:
            db.mark_email_processed(conn, email_id="1", sender_email="alice@test.com", subject="Hello")

        with patch("istota.transport.email.inbound.list_emails", return_value=[envelope]):
            task_ids = poll_emails(config)

        assert task_ids == []

    def test_skips_bot_email(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(sender="bot@test.com")

        with patch("istota.transport.email.inbound.list_emails", return_value=[envelope]):
            task_ids = poll_emails(config)

        assert task_ids == []

        # Verify marked as processed
        with db.get_db(config.db_path) as conn:
            assert db.is_email_processed(conn, "1")

    def test_skips_unknown_sender(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(sender="stranger@unknown.com")
        email = _email(sender="stranger@unknown.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
        ):
            task_ids = poll_emails(config)

        assert task_ids == []

        # Verify marked as processed (but no task created)
        with db.get_db(config.db_path) as conn:
            assert db.is_email_processed(conn, "1")

    def test_disabled_returns_empty(self, make_config):
        config = make_config()
        config.email = AppEmailConfig(enabled=False)

        task_ids = poll_emails(config)
        assert task_ids == []

    def test_handles_list_error(self, make_config):
        config = make_config()
        config.email = _email_config()

        with patch("istota.transport.email.inbound.list_emails", side_effect=Exception("IMAP connection failed")):
            task_ids = poll_emails(config)

        assert task_ids == []


# =============================================================================
# TestCleanupOldEmails
# =============================================================================


class TestCleanupOldEmails:
    """Smoke coverage of the shared helper. The retention behaviour itself
    (server-side ``BEFORE`` sweep, the ``processed_emails`` prune, and the
    coupling between the two windows) lives in ``test_email_retention.py``."""

    def test_disabled_returns_zero(self, make_config):
        config = make_config()
        config.email = AppEmailConfig(enabled=False)

        result = cleanup_old_emails(config, days=7)
        assert result == 0

    def test_zero_days_returns_zero(self, make_config):
        config = make_config()
        config.email = _email_config()

        result = cleanup_old_emails(config, days=0)
        assert result == 0

    def test_deletes_expired_emails(self, make_config):
        config = make_config()
        config.email = _email_config()

        with patch(
            "istota.email_support.delete_emails_before", return_value=1,
        ) as mock_delete:
            result = cleanup_old_emails(config, days=7)

        assert result == 1
        mock_delete.assert_called_once()

    def test_handles_imap_error(self, make_config):
        config = make_config()
        config.email = _email_config()

        with patch(
            "istota.email_support.delete_emails_before",
            side_effect=Exception("IMAP error"),
        ):
            result = cleanup_old_emails(config, days=7)

        assert result == 0


# =============================================================================
# TestGetEmailConfig
# =============================================================================


class TestGetEmailConfig:
    def test_converts_config(self, make_config):
        config = make_config()
        config.email = _email_config()

        email_config = get_email_config(config)

        assert isinstance(email_config, EmailConfig)
        assert email_config.imap_host == "imap.test"
        assert email_config.imap_port == 993
        assert email_config.smtp_host == "smtp.test"
        assert email_config.smtp_port == 587
        assert email_config.bot_email == "bot@test.com"


# =============================================================================
# TestSendEmailReturnsMessageId
# =============================================================================


class TestSendEmailReturnsMessageId:
    def test_send_email_returns_message_id(self):
        config = EmailConfig(
            imap_host="imap.test", imap_port=993,
            imap_user="u", imap_password="p",
            smtp_host="smtp.test", smtp_port=587,
            bot_email="bot@test.com",
        )
        from istota.skills.email import send_email
        with patch("istota.skills.email._send_smtp"):
            result = send_email(
                to="alice@test.com",
                subject="Hello",
                body="Hi",
                config=config,
            )
        assert result.startswith("<") and result.endswith(">")
        assert "@test.com>" in result

    def test_reply_to_email_returns_message_id(self):
        config = EmailConfig(
            imap_host="imap.test", imap_port=993,
            imap_user="u", imap_password="p",
            smtp_host="smtp.test", smtp_port=587,
            bot_email="bot@test.com",
        )
        from istota.skills.email import reply_to_email
        with patch("istota.skills.email._send_smtp"):
            result = reply_to_email(
                to_addr="alice@test.com",
                subject="Hello",
                body="Reply",
                config=config,
                in_reply_to="<orig@test.com>",
            )
        assert result.startswith("<") and result.endswith(">")


# =============================================================================
# TestDeferredSentEmail
# =============================================================================


class TestDeferredSentEmail:
    def test_write_deferred_sent_email(self, tmp_path):
        from istota.skills.email import _write_deferred_sent_email

        env = {
            "ISTOTA_TASK_ID": "42",
            "ISTOTA_DEFERRED_DIR": str(tmp_path),
            "ISTOTA_CONVERSATION_TOKEN": "room1",
            "ISTOTA_USER_ID": "carol",
        }
        with patch.dict("os.environ", env, clear=False):
            _write_deferred_sent_email("<msg@test.com>", "bob@x.com", "Hello")

        path = tmp_path / "task_42_sent_emails.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["message_id"] == "<msg@test.com>"
        assert data[0]["to_addr"] == "bob@x.com"
        assert data[0]["subject"] == "Hello"
        assert data[0]["conversation_token"] == "room1"
        assert data[0]["user_id"] == "carol"

    def test_write_deferred_appends_multiple(self, tmp_path):
        from istota.skills.email import _write_deferred_sent_email

        env = {
            "ISTOTA_TASK_ID": "42",
            "ISTOTA_DEFERRED_DIR": str(tmp_path),
            "ISTOTA_USER_ID": "carol",
        }
        with patch.dict("os.environ", env, clear=False):
            _write_deferred_sent_email("<msg1@test.com>", "a@x.com", "First")
            _write_deferred_sent_email("<msg2@test.com>", "b@x.com", "Second")

        data = json.loads((tmp_path / "task_42_sent_emails.json").read_text())
        assert len(data) == 2

    def test_write_deferred_skips_without_env(self, tmp_path):
        from istota.skills.email import _write_deferred_sent_email

        env = {"ISTOTA_TASK_ID": "", "ISTOTA_DEFERRED_DIR": ""}
        with patch.dict("os.environ", env, clear=False):
            _write_deferred_sent_email("<msg@test.com>", "bob@x.com", "Hello")

        # No file should be written
        assert not list(tmp_path.glob("*.json"))

    def test_cmd_send_writes_deferred(self, tmp_path, outbound_gate_off):
        # `outbound_gate_off` supplies the acting user and an `off` policy: this
        # is about the deferred provenance file, and under the default floor the
        # send would be held instead (no row, nothing to record yet).
        from istota.skills.email import cmd_send

        env = {
            "SMTP_HOST": "smtp.test",
            "SMTP_PORT": "587",
            "SMTP_FROM": "bot@test.com",
            "ISTOTA_TASK_ID": "99",
            "ISTOTA_DEFERRED_DIR": str(tmp_path),
        }
        args = MagicMock()
        args.to = "bob@example.com"
        args.subject = "Meeting"
        args.body = "Let's meet"
        args.body_file = None
        args.html = False

        with (
            patch.dict("os.environ", env, clear=False),
            patch("istota.skills.email._send_smtp"),
        ):
            result = cmd_send(args)

        assert result["status"] == "ok"
        path = tmp_path / "task_99_sent_emails.json"
        assert path.exists()
        data = json.loads(path.read_text())
        assert len(data) == 1
        assert data[0]["to_addr"] == "bob@example.com"
        assert data[0]["subject"] == "Meeting"


# =============================================================================
# TestMatchThread
# =============================================================================


class TestMatchThread:
    def test_match_by_references(self, db_path):
        from istota.transport.email.inbound import _match_thread

        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<sent1@bot.com>",
                to_addr="bob@ext.com",
                subject="Meeting",
                conversation_token="room1",
            )

            # Simulate inbound email with References containing our sent message ID
            email = _email(
                sender="bob@ext.com",
                subject="Re: Meeting",
            )
            email.references = "<sent1@bot.com>"

            match = _match_thread(conn, email)
            assert match is not None
            assert match.user_id == "carol"
            assert match.conversation_token == "room1"

    def test_no_match_without_references(self, db_path):
        from istota.transport.email.inbound import _match_thread

        with db.get_db(db_path) as conn:
            email = _email(sender="unknown@ext.com", subject="Random")
            email.references = None

            assert _match_thread(conn, email) is None

    def test_no_match_with_unknown_references(self, db_path):
        from istota.transport.email.inbound import _match_thread

        with db.get_db(db_path) as conn:
            email = _email(sender="bob@ext.com", subject="Re: Something")
            email.references = "<unknown@other.com>"

            assert _match_thread(conn, email) is None

    def test_match_multiple_references(self, db_path):
        """References header with multiple IDs — should match our sent one."""
        from istota.transport.email.inbound import _match_thread

        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<sent2@bot.com>",
                to_addr="alice@ext.com",
                subject="Hello",
            )

            email = _email(sender="alice@ext.com", subject="Re: Hello")
            email.references = "<original@alice.com> <sent2@bot.com>"

            match = _match_thread(conn, email)
            assert match is not None
            assert match.message_id == "<sent2@bot.com>"

    def test_match_by_in_reply_to_when_references_unusable(self, db_path):
        """In-Reply-To alone is enough to thread a reply.

        References does not subsume In-Reply-To in practice: a sender can emit
        one unreadable (encoded-words, a truncated chain) while the other names
        our message exactly. Reading only References dropped such a reply on
        the floor — no owner resolved, so no task and no notification.
        """
        from istota.transport.email.inbound import _match_thread

        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<sent3@bot.com>",
                to_addr="bob@ext.com",
                subject="Invite",
                conversation_token="room3",
            )

            email = _email(sender="bob@ext.com", subject="Re: Invite")
            email.references = "<unrelated@peer.com>"
            email.in_reply_to = "<sent3@bot.com>"

            match = _match_thread(conn, email)
            assert match is not None
            assert match.user_id == "carol"
            assert match.conversation_token == "room3"

    def test_references_still_wins_over_in_reply_to(self, db_path):
        """References is the more complete chain, so it is consulted first."""
        from istota.transport.email.inbound import _match_thread

        with db.get_db(db_path) as conn:
            for mid in ("<refs@bot.com>", "<irt@bot.com>"):
                db.record_sent_email(
                    conn, user_id="carol", message_id=mid,
                    to_addr="bob@ext.com", subject="Invite",
                )

            email = _email(sender="bob@ext.com", subject="Re: Invite")
            email.references = "<refs@bot.com>"
            email.in_reply_to = "<irt@bot.com>"

            match = _match_thread(conn, email)
            assert match is not None
            assert match.message_id == "<refs@bot.com>"

    def test_encoded_references_match_through_the_real_mapper(self, db_path):
        """The seam: `_msg_to_email` decoding + `match_thread`, composed.

        Both halves passing separately is what let the boundary-fold gap
        survive its first review, so this builds the message the way the poller
        really does — from raw wire headers — and deliberately carries **no**
        In-Reply-To, so only the References path can resolve it. Both fold
        placements are exercised, since they fail in opposite directions.
        """
        from unittest.mock import MagicMock

        from istota.skills.email import _msg_to_email
        from istota.transport.email.inbound import _match_thread

        raws = {
            # fold inside an id — the halves must rejoin
            "mid_id": (
                "=?us-ascii?Q?<peer1@ext.com>_<sent-seam@bot.co?=\r\n"
                " =?us-ascii?Q?m>?="
            ),
            # fold at an id boundary — the ids glue together
            "boundary": (
                "=?us-ascii?Q?<peer1@ext.com>?=\r\n"
                " =?us-ascii?Q?<sent-seam@bot.com>?="
            ),
        }

        with db.get_db(db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<sent-seam@bot.com>",
                to_addr="bob@ext.com",
                subject="Invite",
                conversation_token="room_seam",
            )

            for shape, raw in raws.items():
                msg = MagicMock()
                msg.uid = "1"
                msg.subject = "Re: Invite"
                msg.from_ = "bob@ext.com"
                msg.to = ("bot@test.com",)
                msg.cc = ()
                msg.date_str = "Mon, 01 Jan 2026 12:00:00 +0000"
                msg.text = "Sounds good."
                msg.html = ""
                msg.flags = []
                msg.attachments = []
                msg.headers = {"references": (raw,)}

                mail = _msg_to_email(msg)
                assert mail.in_reply_to is None, shape

                match = _match_thread(conn, mail)
                assert match is not None, f"{shape} fold did not thread"
                assert match.user_id == "carol", shape
                assert match.conversation_token == "room_seam", shape

    def test_no_match_with_unknown_in_reply_to(self, db_path):
        from istota.transport.email.inbound import _match_thread

        with db.get_db(db_path) as conn:
            email = _email(sender="bob@ext.com", subject="Re: Something")
            email.references = None
            email.in_reply_to = "<unknown@other.com>"

            assert _match_thread(conn, email) is None


# =============================================================================
# TestPollEmailsThreadMatching
# =============================================================================


class TestPollEmailsThreadMatching:
    """Tests for email poller routing emissary replies via thread matching."""

    def test_unknown_sender_reply_routes_to_originating_user(self, make_config):
        """Reply from unknown sender matching a sent thread routes to originating user."""
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(email_addresses=["carol@test.com"])}

        # Pre-record an outbound email from carol
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<outbound@bot.com>",
                to_addr="external@proton.me",
                subject="Set up a meeting",
                conversation_token="talk_room_42",
            )

        envelope = _envelope(id="2", sender="external@proton.me", subject="Re: Set up a meeting")
        email = Email(
            id="2", subject="Re: Set up a meeting", sender="external@proton.me",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="How about Tuesday?", attachments=[],
            message_id="<reply@proton.me>",
            references="<outbound@bot.com>",
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "carol"
            assert task.output_target == "talk,email"
            assert task.conversation_token == "talk_room_42"
            assert "Emissary email reply" in task.prompt
            assert "external@proton.me" in task.prompt
            assert "How about Tuesday?" in task.prompt

    def test_reply_with_unusable_references_routes_by_in_reply_to(self, make_config):
        """The production shape: References unreadable, In-Reply-To exact.

        The reply was marked `discarded` — no owner, so no task, no
        notification anywhere — while In-Reply-To named our sent message
        exactly. The mail must route, and route to the sender's user.
        """
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(email_addresses=["carol@test.com"])}

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<outbound2@bot.com>",
                to_addr="external@proton.me",
                subject="Invite",
                conversation_token="room_7",
            )

        envelope = _envelope(id="9", sender="external@proton.me", subject="Re: Invite")
        email = Email(
            id="9", subject="Re: Invite", sender="external@proton.me",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Sounds good.", attachments=[],
            message_id="<reply2@proton.me>",
            # A run of encoded-words that no whitespace split can turn back
            # into message ids — exactly what arrived in production.
            references="=?us-ascii?Q?<a1@proton.me>_<outbound2@bot.com>?=",
            in_reply_to="<outbound2@bot.com>",
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "carol"
            assert task.conversation_token == "room_7"
            row = conn.execute(
                "SELECT routing_method, user_id FROM processed_emails WHERE email_id = ?",
                ("9",),
            ).fetchone()
            assert row["routing_method"] == "thread_match"
            assert row["user_id"] == "carol"

    def test_unknown_sender_no_thread_match_discarded(self, make_config):
        """Unknown sender with no thread match is discarded as before."""
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(email_addresses=["carol@test.com"])}

        envelope = _envelope(id="3", sender="stranger@random.com", subject="Buy stuff")
        email = Email(
            id="3", subject="Buy stuff", sender="stranger@random.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Spam", attachments=[],
            message_id="<spam@random.com>",
            references=None,
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert task_ids == []

        # Should still be marked as processed
        with db.get_db(config.db_path) as conn:
            assert db.is_email_processed(conn, "3")

    def test_known_sender_still_works_normally(self, make_config):
        """Known sender emails are routed normally (no output_target override)."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(sender="alice@test.com")
        email = _email(sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "alice"
            assert task.output_target is None  # Normal email routing
            assert "Emissary" not in task.prompt

    def test_emissary_reply_without_conversation_token_uses_thread_id(self, make_config):
        """If original sent email had no conversation_token, fall back to thread_id."""
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(email_addresses=["carol@test.com"])}

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Hello",
                conversation_token=None,  # No Talk context
            )

        envelope = _envelope(id="4", sender="ext@x.com", subject="Re: Hello")
        email = Email(
            id="4", subject="Re: Hello", sender="ext@x.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hi back", attachments=[],
            message_id="<r@x.com>",
            references="<out@bot.com>",
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "carol"
            assert task.output_target == "talk,email"
            # Should use thread_id since no conversation_token on sent email
            assert task.conversation_token is not None

    def test_thread_match_inherits_talk_delivery_token(self, make_config):
        """ISSUE-057: thread_match inherits talk_delivery_token from sent_emails row."""
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(email_addresses=["carol@test.com"])}

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Plan",
                conversation_token="talk_room_99",
                talk_delivery_token="real_talk_room",
            )

        envelope = _envelope(id="9", sender="ext@x.com", subject="Re: Plan")
        email = Email(
            id="9", subject="Re: Plan", sender="ext@x.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Sure", attachments=[],
            message_id="<r9@x.com>", references="<out@bot.com>",
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.talk_delivery_token == "real_talk_room"
            # conversation_token still preserves the email-thread grouping key
            assert task.conversation_token == "talk_room_99"

    def _origin_reply(self, config, *, origin_target, policy=None,
                      sent_conversation_token="rm_web123"):
        """Record a sent_email with origin_target, poll a thread-matched reply,
        and return the created task. Shared by the origin-routing tests."""
        config.email = _email_config()
        user = UserConfig(email_addresses=["carol@test.com"])
        if policy is not None:
            user.email_reply_routing = policy
        config.users = {"carol": user}

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<origin_out@bot.com>",
                to_addr="ext@x.com",
                subject="Question",
                conversation_token=sent_conversation_token,
                origin_target=origin_target,
            )

        envelope = _envelope(id="20", sender="ext@x.com", subject="Re: Question")
        email = Email(
            id="20", subject="Re: Question", sender="ext@x.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="My answer", attachments=[],
            message_id="<r20@x.com>", references="<origin_out@bot.com>",
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)
        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            return db.get_task(conn, task_ids[0])

    def test_web_origin_default_policy_routes_origin_plus_thread(self, make_config):
        task = self._origin_reply(make_config(), origin_target="web:rm_web123")
        assert task.output_target == "web:rm_web123,email"
        assert task.conversation_token == "rm_web123"

    def test_web_origin_policy_origin_only(self, make_config):
        task = self._origin_reply(
            make_config(), origin_target="web:rm_web123", policy="origin",
        )
        assert task.output_target == "web:rm_web123"
        assert task.conversation_token == "rm_web123"

    def test_web_origin_policy_thread_only(self, make_config):
        task = self._origin_reply(
            make_config(), origin_target="web:rm_web123", policy="thread",
        )
        assert task.output_target == "email"

    def test_talk_origin_descriptor_routes_to_token(self, make_config):
        task = self._origin_reply(
            make_config(), origin_target="talk:RealRoomXYZ",
            sent_conversation_token="RealRoomXYZ",
        )
        assert task.output_target == "talk:RealRoomXYZ,email"
        assert task.conversation_token == "RealRoomXYZ"

    # -- Dual-bound origin room: the descriptor must not pick one surface ------
    #
    # A room reachable on both Talk and web is ONE conversation. A stored
    # `web:<tok>` descriptor delivers the reply to the web leg only, so the Talk
    # view of that same room shows nothing — the exact mirror of the ISSUE-242
    # gap, arrived at from the other side. `room` is the primitive that already
    # fans out by live bindings, and bindings are resolved at reply time because
    # a room can be promoted to Talk after the send that stamped the descriptor.

    def _dual_bound(self, config, token="rm_web123"):
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, token, "carol", origin="web")
            db.add_room_binding(conn, token, "web", token)
            db.add_room_binding(conn, token, "talk", token)

    def test_dual_bound_web_origin_fans_out_to_room(self, make_config):
        config = make_config()
        self._dual_bound(config)
        task = self._origin_reply(config, origin_target="web:rm_web123")
        # The stored descriptor names one view of the room; it is upgraded to the
        # room form at reply time so the fan-out reaches every view of it.
        assert task.output_target == "room:rm_web123,email"
        assert task.conversation_token == "rm_web123"

    def test_dual_bound_talk_origin_fans_out_to_room(self, make_config):
        config = make_config()
        self._dual_bound(config, token="RealRoomXYZ")
        task = self._origin_reply(
            config, origin_target="talk:RealRoomXYZ",
            sent_conversation_token="RealRoomXYZ",
        )
        assert task.output_target == "room:RealRoomXYZ,email"

    def test_dual_bound_respects_origin_only_policy(self, make_config):
        config = make_config()
        self._dual_bound(config)
        task = self._origin_reply(
            config, origin_target="web:rm_web123", policy="origin",
        )
        assert task.output_target == "room:rm_web123"

    def test_single_bound_room_keeps_its_surface_descriptor(self, make_config):
        # Nothing to fan out to: one binding means one surface, and `room` would
        # add a DB lookup at every delivery for no change in outcome.
        config = make_config()
        with db.get_db(config.db_path) as conn:
            db.register_room(conn, "rm_web123", "carol", origin="web")
            db.add_room_binding(conn, "rm_web123", "web", "rm_web123")
        task = self._origin_reply(config, origin_target="web:rm_web123")
        assert task.output_target == "web:rm_web123,email"

    def test_unregistered_origin_room_is_unchanged(self, make_config):
        # The pre-existing case every other test in this class exercises: a
        # descriptor naming no room at all must route exactly as before.
        task = self._origin_reply(make_config(), origin_target="web:rm_web123")
        assert task.output_target == "web:rm_web123,email"

    def test_a_stored_room_descriptor_is_used_as_is(self, make_config):
        """The form new sends stamp. No upgrade step — it already names the
        conversation, and expansion reads live bindings at delivery."""
        config = make_config()
        self._dual_bound(config)
        task = self._origin_reply(config, origin_target="room:rm_web123")
        assert task.output_target == "room:rm_web123,email"
        assert task.conversation_token == "rm_web123"

    def test_an_archived_origin_room_falls_back_rather_than_dropping(
        self, make_config,
    ):
        """A reply must never be lost because its room went away.

        The room is gone, so there is nothing to fan out to — but the email leg
        is still a real delivery and the reply reaches the contact who sent it.
        """
        config = make_config()
        self._dual_bound(config)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE rooms SET archived = 1 WHERE token = ?", ("rm_web123",),
            )
        task = self._origin_reply(config, origin_target="web:rm_web123")
        # Not upgraded to the room form: the room is not live to upgrade to.
        # Note this is deliberately *not* the same outcome as the sibling test
        # below, where the descriptor names the room directly and expansion
        # yields email alone. A legacy descriptor names a surface leg, so it
        # keeps delivering to that leg — "existing values keep routing as they
        # do today" — while the room form asks about a room that is gone.
        assert task.output_target == "web:rm_web123,email"

    def test_an_archived_room_named_directly_still_delivers_to_email(
        self, make_config,
    ):
        """Same room, but the descriptor names it directly — the case a send
        stamped before the room was archived. Expansion yields the origin
        delivery alone rather than raising or dropping the reply."""
        from istota.transport.routing import resolve_delivery_plan

        config = make_config()
        self._dual_bound(config)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE rooms SET archived = 1 WHERE token = ?", ("rm_web123",),
            )
        task = self._origin_reply(config, origin_target="room:rm_web123")
        assert task.output_target == "room:rm_web123,email"
        plan = resolve_delivery_plan(config, task, None)
        assert [d.surface for d in plan] == ["email"]

    def _poll_reply(self, config, *, sender, to, references="<origin_out@bot.com>"):
        """Poll a single inbound reply and return the created task (or None)."""
        envelope = _envelope(id="40", sender=sender, subject="Re: Question")
        email = Email(
            id="40", subject="Re: Question", sender=sender,
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="My answer", attachments=[],
            message_id="<r40@x.com>", references=references,
            to=to, cc=(),
        )
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)
        if not task_ids:
            return None
        with db.get_db(config.db_path) as conn:
            return db.get_task(conn, task_ids[0])

    def test_self_reply_via_sender_match_recovers_origin(self, make_config):
        # THE primary bug: the user replies from their OWN configured address, so
        # sender-match resolves them at step 2 and thread-match (which carries the
        # origin descriptor) used to be skipped. The origin must still be
        # recovered, and the prompt stays the plain self-reply template (not the
        # external-emissary one).
        #
        # Recovered for *context*, not for delivery: since ISSUE-254 the reply
        # is mailed back and nothing is written into the origin room, because the
        # user is on the email surface by demonstration. The recovery this test
        # was written for is what `conversation_token` still asserts.
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(email_addresses=["carol@test.com"])}
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn, user_id="carol", message_id="<origin_out@bot.com>",
                to_addr="carol@test.com", subject="Question",
                conversation_token="rm_web123", origin_target="web:rm_web123",
            )
        task = self._poll_reply(config, sender="carol@test.com", to=("bot@test.com",))
        assert task is not None
        assert task.output_target == "email"
        assert task.conversation_token == "rm_web123"
        # Self-reply → plain template, not "an external contact has replied".
        assert "Emissary email reply" not in task.prompt

    def test_self_reply_ignores_the_origin_policy(self, make_config):
        """The suppression is per-message, not per-user, so it overrides the
        policy rather than being expressible through it (ISSUE-254). `origin`
        names the room and nothing else, and an empty plan would lose the reply
        — so it falls back to email, where the user wrote from."""
        config = make_config()
        config.email = _email_config()
        config.users = {
            "carol": UserConfig(
                email_addresses=["carol@test.com"], email_reply_routing="origin",
            ),
        }
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn, user_id="carol", message_id="<origin_out@bot.com>",
                to_addr="carol@test.com", subject="Question",
                conversation_token="rm_web123", origin_target="web:rm_web123",
            )
        task = self._poll_reply(config, sender="carol@test.com", to=("bot@test.com",))
        assert task.output_target == "email"

    def test_plus_address_reply_recovers_origin(self, make_config):
        # Dormant second path: a reply addressed to the bot's plus-address is
        # resolved at step 1, also pre-empting thread-match. The origin must
        # still be recovered.
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(email_addresses=["carol@test.com"])}
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn, user_id="carol", message_id="<origin_out@bot.com>",
                to_addr="ext@x.com", subject="Question",
                conversation_token="rm_web123", origin_target="web:rm_web123",
            )
        task = self._poll_reply(
            config, sender="ext@x.com", to=("bot+carol@test.com",),
        )
        assert task is not None
        assert task.output_target == "web:rm_web123,email"
        assert task.conversation_token == "rm_web123"

    def test_external_thread_reply_keeps_emissary_prompt(self, make_config):
        # An external contact (not a configured email, no plus-address) resolves
        # purely by thread-match → emissary template AND origin routing.
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(email_addresses=["carol@test.com"])}
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn, user_id="carol", message_id="<origin_out@bot.com>",
                to_addr="ext@x.com", subject="Question",
                conversation_token="rm_web123", origin_target="web:rm_web123",
            )
        task = self._poll_reply(config, sender="ext@x.com", to=("bot@test.com",))
        assert task.output_target == "web:rm_web123,email"
        assert "Emissary email reply" in task.prompt

    def test_thread_row_for_other_user_not_applied(self, make_config):
        # Defence-in-depth: a reply sender-matched to user A must not inherit the
        # origin descriptor of a thread row owned by user B (no cross-user
        # surface leak). Identity (sender/plus) wins; the mismatched payload is
        # dropped, so the reply falls back to the default email plan.
        config = make_config()
        config.email = _email_config()
        config.users = {
            "alice": UserConfig(email_addresses=["alice@test.com"]),
            "carol": UserConfig(email_addresses=["carol@test.com"]),
        }
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn, user_id="carol", message_id="<origin_out@bot.com>",
                to_addr="ext@x.com", subject="Question",
                conversation_token="rm_web123", origin_target="web:rm_web123",
            )
        task = self._poll_reply(config, sender="alice@test.com", to=("bot@test.com",))
        assert task is not None
        assert task.user_id == "alice"
        assert task.output_target is None  # mismatched origin dropped → default
        assert task.conversation_token != "rm_web123"

    def test_legacy_null_origin_with_web_token_not_used_as_talk_channel(self, make_config):
        # A legacy (pre-migration) sent_emails row with NULL origin_target whose
        # conversation_token is a web room token must NOT be used as a Talk
        # delivery channel (that would post to a nonexistent Talk room).
        config = make_config()
        config.email = _email_config()
        config.users = {
            "carol": UserConfig(
                email_addresses=["carol@test.com"], alerts_channel="alerts_room",
            ),
        }
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<legacy_web@bot.com>",
                to_addr="ext@x.com",
                subject="Q",
                conversation_token="web-carol-deadbeef",
                origin_target=None,  # legacy row
            )
        envelope = _envelope(id="30", sender="ext@x.com", subject="Re: Q")
        email = Email(
            id="30", subject="Re: Q", sender="ext@x.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="answer", attachments=[],
            message_id="<r30@x.com>", references="<legacy_web@bot.com>",
            to=("bot@test.com",), cc=(),
        )
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)
        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        assert task.output_target == "talk,email"
        # The web token must not leak in as the Talk channel; the ladder falls
        # through to the resolved alerts room instead.
        assert task.talk_delivery_token != "web-carol-deadbeef"
        assert task.talk_delivery_token == "alerts_room"

    def test_known_sender_resolves_talk_delivery_token_from_alerts(self, make_config):
        """plus_address / sender_match routes resolve talk_delivery_token via user config."""
        config = make_config()
        config.email = _email_config()
        config.users = {
            "alice": UserConfig(
                email_addresses=["alice@test.com"],
                alerts_channel="alice_alerts",
            ),
        }

        envelope = _envelope(sender="alice@test.com")
        email = _email(sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            # conversation_token is the synthetic email-thread hash
            assert task.conversation_token is not None
            assert len(task.conversation_token) == 16
            # talk_delivery_token resolves to the user's alerts channel
            assert task.talk_delivery_token == "alice_alerts"


# =============================================================================
# TestExtractUserFromRecipient
# =============================================================================


class TestExtractUserFromRecipient:
    """Tests for plus-address routing via recipient headers."""

    def _config_with_users(self):
        config = Config()
        config.email = _email_config()  # bot_email = "bot@test.com"
        config.users = {
            "carol": UserConfig(email_addresses=["carol@example.com"]),
            "alice": UserConfig(email_addresses=["alice@example.com"]),
        }
        return config

    def test_extracts_user_from_to_header(self):
        config = self._config_with_users()
        email = _email(to=("bot+carol@test.com",))
        assert _extract_user_from_recipient(config, email) == "carol"

    def test_extracts_user_from_cc_header(self):
        config = self._config_with_users()
        email = _email(to=("someone@other.com",), cc=("bot+alice@test.com",))
        assert _extract_user_from_recipient(config, email) == "alice"

    def test_returns_none_for_bare_bot_address(self):
        config = self._config_with_users()
        email = _email(to=("bot@test.com",))
        assert _extract_user_from_recipient(config, email) is None

    def test_returns_none_for_invalid_user(self):
        config = self._config_with_users()
        email = _email(to=("bot+nonexistent@test.com",))
        assert _extract_user_from_recipient(config, email) is None

    def test_case_insensitive_matching(self):
        config = self._config_with_users()
        email = _email(to=("BOT+Carol@Test.Com",))
        assert _extract_user_from_recipient(config, email) == "carol"

    def test_ignores_different_domain(self):
        config = self._config_with_users()
        email = _email(to=("bot+carol@other-domain.com",))
        assert _extract_user_from_recipient(config, email) is None

    def test_returns_none_when_no_recipients(self):
        config = self._config_with_users()
        email = _email(to=(), cc=())
        assert _extract_user_from_recipient(config, email) is None

    def test_first_valid_match_wins(self):
        """If both To and Cc have plus-addresses, To wins."""
        config = self._config_with_users()
        email = _email(to=("bot+carol@test.com",), cc=("bot+alice@test.com",))
        assert _extract_user_from_recipient(config, email) == "carol"


# =============================================================================
# TestPollEmailsPlusAddressRouting
# =============================================================================


class TestPollEmailsPlusAddressRouting:
    """Tests for plus-address routing in the poll loop."""

    def test_plus_address_routes_unknown_sender(self, make_config):
        """Unknown sender emailing bot+carol@ should route to carol."""
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(email_addresses=["carol@test.com"])}

        envelope = _envelope(id="10", sender="stranger@external.com", subject="Hello agent")
        email = Email(
            id="10", subject="Hello agent", sender="stranger@external.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Can you help me?", attachments=[],
            message_id="<ext1@external.com>", references=None,
            to=("bot+carol@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "carol"
            assert task.source_type == "email"
            assert "stranger@external.com" in task.prompt

    def test_plus_address_takes_precedence_over_sender_match(self, make_config):
        """If sender matches alice but To is bot+carol@, route to carol."""
        config = make_config()
        config.email = _email_config()
        config.users = {
            "carol": UserConfig(email_addresses=["carol@test.com"]),
            "alice": UserConfig(email_addresses=["alice@test.com"]),
        }

        envelope = _envelope(id="11", sender="alice@test.com", subject="For carol")
        email = Email(
            id="11", subject="For carol", sender="alice@test.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Route this to carol", attachments=[],
            message_id="<a11@test.com>", references=None,
            to=("bot+carol@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "carol"  # plus-address wins over sender

    def test_invalid_plus_address_falls_through_to_sender(self, make_config):
        """Plus-address with invalid user falls through to sender-based routing."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(id="12", sender="alice@test.com", subject="Test")
        email = Email(
            id="12", subject="Test", sender="alice@test.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hello", attachments=[],
            message_id="<a12@test.com>", references=None,
            to=("bot+nonexistent@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "alice"  # fell through to sender match

    def test_routing_method_stored_for_plus_address(self, make_config):
        """routing_method should be 'plus_address' when routed via plus-addressing."""
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(email_addresses=["carol@test.com"])}

        envelope = _envelope(id="13", sender="stranger@ext.com", subject="Hi")
        email = Email(
            id="13", subject="Hi", sender="stranger@ext.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hello", attachments=[],
            message_id="<s13@ext.com>", references=None,
            to=("bot+carol@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            poll_emails(config)

        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT routing_method FROM processed_emails WHERE email_id = ?", ("13",)
            ).fetchone()
            assert row is not None
            assert row[0] == "plus_address"

    def test_routing_method_stored_for_sender_match(self, make_config):
        """routing_method should be 'sender_match' for known sender routing."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(id="14", sender="alice@test.com", subject="Hi")
        email = _email(id="14", sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            poll_emails(config)

        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT routing_method FROM processed_emails WHERE email_id = ?", ("14",)
            ).fetchone()
            assert row is not None
            assert row[0] == "sender_match"

    def test_routing_method_stored_for_thread_match(self, make_config):
        """routing_method should be 'thread_match' for emissary reply routing."""
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(email_addresses=["carol@test.com"])}

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<out15@bot.com>",
                to_addr="ext@x.com",
                subject="Hello",
            )

        envelope = _envelope(id="15", sender="ext@x.com", subject="Re: Hello")
        email = Email(
            id="15", subject="Re: Hello", sender="ext@x.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Reply", attachments=[],
            message_id="<r15@x.com>",
            references="<out15@bot.com>",
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            poll_emails(config)

        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT routing_method FROM processed_emails WHERE email_id = ?", ("15",)
            ).fetchone()
            assert row is not None
            assert row[0] == "thread_match"

    def test_routing_method_stored_for_discard(self, make_config):
        """routing_method should be 'discarded' for unknown sender with no match."""
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(email_addresses=["carol@test.com"])}

        envelope = _envelope(id="16", sender="spam@nowhere.com", subject="Spam")
        email = Email(
            id="16", subject="Spam", sender="spam@nowhere.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Buy stuff", attachments=[],
            message_id="<spam16@nowhere.com>", references=None,
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            poll_emails(config)

        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT routing_method FROM processed_emails WHERE email_id = ?", ("16",)
            ).fetchone()
            assert row is not None
            assert row[0] == "discarded"


class TestEmailConfirmationGate:
    """Tests for the confirmation gate on plus-addressed emails from untrusted senders."""

    def test_untrusted_sender_held_for_confirmation(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(
            email_addresses=["carol@test.com"],
            alerts_channel="alerts_room",
        )}

        envelope = _envelope(id="20", sender="stranger@evil.com", subject="Hi")
        email = Email(
            id="20", subject="Hi", sender="stranger@evil.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hello", attachments=[],
            message_id="<s20@evil.com>", references=None,
            to=("bot+carol@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_confirmation_prompt", return_value=(True, 77)) as mock_send,
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending_confirmation"
            assert task.talk_response_id == 77
        mock_send.assert_called_once()

    def test_trusted_sender_proceeds_immediately(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(
            email_addresses=["carol@test.com"],
            trusted_email_senders=["*@trusted.com"],
        )}

        envelope = _envelope(id="21", sender="friend@trusted.com", subject="Hi")
        email = Email(
            id="21", subject="Hi", sender="friend@trusted.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hello", attachments=[],
            message_id="<s21@trusted.com>", references=None,
            to=("bot+carol@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending"

    def test_own_email_via_plus_address_not_gated(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(
            email_addresses=["carol@test.com"],
        )}

        envelope = _envelope(id="22", sender="carol@test.com", subject="Hi")
        email = Email(
            id="22", subject="Hi", sender="carol@test.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hello", attachments=[],
            message_id="<s22@test.com>", references=None,
            to=("bot+carol@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending"

    def test_db_trusted_sender_proceeds_immediately(self, make_config):
        """Sender trusted via DB (not config) should bypass the confirmation gate."""
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(
            email_addresses=["carol@test.com"],
            trusted_email_senders=[],  # No config patterns
        )}

        # Add sender to DB trusted list
        with db.get_db(config.db_path) as conn:
            db.add_trusted_sender(conn, "carol", "friend@newcontact.com")

        envelope = _envelope(id="db1", sender="friend@newcontact.com", subject="Hi")
        email = Email(
            id="db1", subject="Hi", sender="friend@newcontact.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hello", attachments=[],
            message_id="<db1@newcontact.com>", references=None,
            to=("bot+carol@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending"  # Not pending_confirmation

    def test_sender_match_own_email_not_gated_by_default(self, make_config):
        """With the gate at its default (off), sender-match mail is processed directly."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(
            email_addresses=["alice@test.com"],
            alerts_channel="alerts_room",
        )}

        assert config.email.confirm_sender_match is False

        envelope = _envelope(id="23", sender="alice@test.com", subject="Hi")
        email = _email(id="23", sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending"

    def test_sender_match_not_gated_when_disabled(self, make_config):
        """Sender-match emails proceed directly when confirm_sender_match is False."""
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = False
        config.users = {"alice": UserConfig(
            email_addresses=["alice@test.com"],
        )}

        envelope = _envelope(id="23b", sender="alice@test.com", subject="Hi")
        email = _email(id="23b", sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending"

    def test_sender_match_trusted_sender_not_gated(self, make_config):
        """A trusted_email_senders pattern exempts an address even with the gate on."""
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = True
        config.users = {"alice": UserConfig(
            email_addresses=["alice@test.com"],
            trusted_email_senders=["alice@test.com"],
        )}

        envelope = _envelope(id="23c", sender="alice@test.com", subject="Hi")
        email = _email(id="23c", sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending"

    def test_gate_no_alerts_channel_still_holds(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(
            email_addresses=["carol@test.com"],
            # No alerts_channel set
        )}

        envelope = _envelope(id="24", sender="stranger@evil.com", subject="Hi")
        email = Email(
            id="24", subject="Hi", sender="stranger@evil.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Hello", attachments=[],
            message_id="<s24@evil.com>", references=None,
            to=("bot+carol@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_confirmation_prompt", return_value=(False, None)),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending_confirmation"
            assert task.talk_response_id is None


class TestSenderMatchConfirmationGate:
    """ISSUE-227 — ``confirm_sender_match`` used to be unreachable: the route is
    *defined* by the sender being one of the user's own addresses, and the trust
    check it consulted returned True for exactly that set. The gate now asks about
    the own-address claim itself, and only an explicit exemption lets mail past."""

    def test_own_address_is_gated_when_enabled(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = True
        config.users = {"alice": UserConfig(
            email_addresses=["alice@test.com"],
            alerts_channel="alerts_room",
        )}

        envelope = _envelope(id="sm1", sender="alice@test.com", subject="Hi")
        email = _email(id="sm1", sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_confirmation_prompt", return_value=(True, 99)) as send,
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending_confirmation"
            assert task.talk_response_id == 99

        prompt = send.call_args.args[2]
        assert "alice@test.com" in prompt
        assert "sender_match" in prompt

    def test_gated_turn_is_not_mirrored_to_the_room(self, make_config):
        """The mirror commits in the task's transaction, so a gated turn must not
        publish before the user has answered (same contract as the plus-address gate)."""
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = True
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(id="sm2", sender="alice@test.com", subject="Hi")
        email = _email(id="sm2", sender="alice@test.com")

        captured = {}

        real_ingest = inbound_module.ingest_message

        def _spy(conn, cfg, msg):
            captured["suppress"] = msg.suppress_transcript_mirror
            return real_ingest(conn, cfg, msg)

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_confirmation_prompt", return_value=(False, None)),
            patch("istota.transport.email.inbound.ingest_message", side_effect=_spy),
        ):
            poll_emails(config)

        assert captured["suppress"] is True

    def test_runtime_trusted_address_bypasses_the_gate(self, make_config):
        """The 'yes trust' escape hatch: once the address is trusted at runtime the
        gate stops asking, so an operator who turns it on is not stuck confirming forever."""
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = True
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        with db.get_db(config.db_path) as conn:
            db.add_trusted_sender(conn, "alice", "alice@test.com")

        envelope = _envelope(id="sm3", sender="alice@test.com", subject="Hi")
        email = _email(id="sm3", sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_ids[0]).status == "pending"

    def test_own_address_claim_is_gated_on_the_plus_address_route_too(self, make_config):
        """The plus-address is public — it is the From: on every mail the bot sends
        on the user's behalf — so a spoofer who knows the address the gate is about
        can also route around it. Same own-address claim, same answer, either route."""
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = True
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(id="sm4", sender="alice@test.com", subject="Hi")
        email = _email(id="sm4", sender="alice@test.com", to=("bot+alice@test.com",))

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_confirmation_prompt", return_value=(False, None)),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending_confirmation"
            assert db.get_email_for_task(conn, task_ids[0]).routing_method == "plus_address"

    def test_plus_address_self_mail_stays_ungated_with_the_gate_off(self, make_config):
        """Default state: the own-address branch still answers for plus-address mail,
        so a user's own plus-addressed self-mail is processed as it always was."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(id="sm4b", sender="alice@test.com", subject="Hi")
        email = _email(id="sm4b", sender="alice@test.com", to=("bot+alice@test.com",))

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_ids[0]).status == "pending"

    def test_external_plus_address_sender_keeps_the_trust_offer(self, make_config):
        """A genuinely external sender is still offered 'yes trust' — trusting them
        is the intended way to stop being asked, and costs nothing this gate protects."""
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = True
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(id="sm4c", sender="stranger@evil.com", subject="Hi")
        email = _email(id="sm4c", sender="stranger@evil.com", to=("bot+alice@test.com",))

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_confirmation_prompt", return_value=(True, 7)) as send,
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        prompt = send.call_args.args[2]
        assert "yes trust" in prompt
        assert "unknown sender" in prompt

    @staticmethod
    def _gate_prompt(config, *, uid="ob1"):
        """Poll one mail from an untrusted stranger, return the gate prompt."""
        envelope = _envelope(id=uid, sender="stranger@evil.com", subject="Hi")
        email = _email(id=uid, sender="stranger@evil.com", to=("bot+alice@test.com",))

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_confirmation_prompt", return_value=(True, 7)) as send,
        ):
            poll_emails(config)
        return send.call_args.args[2].lower()

    def test_trust_offer_discloses_the_outbound_half(self, make_config):
        """`yes trust` grants more than the question appears to ask about.

        One list, two meanings since the outbound approval gate shipped: under
        the `untrusted` policy the answer also stops holding mail *to* this
        address for approval. A user who does not want the outbound half has to
        know to answer plain `yes`, so the prompt has to say so — one of the
        three disclosures `outbound_policy`'s module docstring commits to, and
        the only one on a surface the user reads under time pressure.
        """
        config = make_config()
        config.email = _email_config()
        config.email.outbound_approval_floor = "untrusted"
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        prompt = self._gate_prompt(config)
        assert "without waiting for your approval" in prompt
        # Specifically the *outbound* direction. A sentence about processing
        # incoming mail would pass a looser assertion while saying nothing new.
        assert "mail to this address" in prompt

    @pytest.mark.parametrize("policy", ["off", "all"])
    def test_no_outbound_promise_under_a_policy_that_ignores_the_trust_list(
        self, make_config, policy,
    ):
        """`untrusted` is the only policy that consults the trust list.

        `off` holds nothing to begin with, and `all` clears only the user's own
        addresses — so under either, trusting a correspondent buys no outbound
        permission and promising one would be a lie told at the moment the user
        is deciding whether to trust. `off` is not hypothetical: making it
        reachable from the inventory is half of what this stage is for.
        """
        config = make_config()
        config.email = _email_config()
        config.email.outbound_approval_floor = policy
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        prompt = self._gate_prompt(config, uid=f"ob-{policy}")
        # The inbound half of the offer is unaffected — only the promise about
        # outbound is withheld.
        assert "yes trust" in prompt
        assert "without waiting for your approval" not in prompt

    def test_self_claim_prompt_makes_no_trust_offer_to_disclose(self, make_config):
        """The own-address branch offers a plain yes/no, so there is nothing to
        disclose — and adding the outbound sentence there would advertise a
        shortcut the prompt deliberately withholds from a self-claim."""
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = True
        config.email.outbound_approval_floor = "untrusted"
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(id="ob2", sender="alice@test.com", subject="Hi")
        email = _email(id="ob2", sender="alice@test.com", to=("bot+alice@test.com",))

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_confirmation_prompt", return_value=(True, 7)) as send,
        ):
            poll_emails(config)

        prompt = send.call_args.args[2].lower()
        assert "yes trust" not in prompt
        assert "without waiting for your approval" not in prompt

    def test_trusted_external_sender_is_unaffected_by_the_gate(self, make_config):
        """The flag suppresses only the own-address branch, which cannot match an
        external sender — so their trust answer is arithmetically unchanged."""
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = True
        config.users = {"alice": UserConfig(
            email_addresses=["alice@test.com"],
            trusted_email_senders=["*@partner.com"],
        )}

        envelope = _envelope(id="sm4g", sender="bob@partner.com", subject="Hi")
        email = _email(id="sm4g", sender="bob@partner.com", to=("bot+alice@test.com",))

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_ids[0]).status == "pending"

    def test_self_claim_prompt_omits_the_trust_offer(self, make_config):
        """'yes trust' on a self-claim would exempt the user's own address from the
        gate — for the spoofer too. It must not be offered as one of three equal options."""
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = True
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(id="sm4d", sender="alice@test.com", subject="Hi")
        email = _email(id="sm4d", sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_confirmation_prompt", return_value=(True, 8)) as send,
        ):
            poll_emails(config)

        prompt = send.call_args.args[2]
        assert "yes trust" not in prompt
        assert "unverified sender" in prompt
        assert "Reply 'yes' to process, or 'no' to discard." in prompt

    def test_self_claim_is_judged_against_the_routed_user(self, make_config):
        """An address held by two users routes to whoever the plus-address names.
        The trust offer must follow that user, not whoever `find_user_by_email`
        happens to return first — offering it would trust their own address."""
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = True
        config.users = {
            "bob": UserConfig(email_addresses=["shared@test.com"]),
            "alice": UserConfig(email_addresses=["shared@test.com"]),
        }

        envelope = _envelope(id="sm6", sender="shared@test.com", subject="Hi")
        email = _email(id="sm6", sender="shared@test.com", to=("bot+alice@test.com",))

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_confirmation_prompt", return_value=(True, 11)) as send,
        ):
            task_ids = poll_emails(config)

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.user_id == "alice"
            assert task.status == "pending_confirmation"

        assert "yes trust" not in send.call_args.args[2]

    def test_undeliverable_prompt_warns(self, caplog, make_config):
        """The task is parked and the email already marked processed, so a prompt
        nobody receives is silent mail loss. It must at least be logged."""
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = True
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(id="sm4e", sender="alice@test.com", subject="Hi")
        email = _email(id="sm4e", sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_confirmation_prompt", return_value=(False, None)),
            caplog.at_level("WARNING", logger="istota.transport.email.inbound"),
        ):
            poll_emails(config)

        assert [r for r in caplog.records if "could not be delivered" in r.getMessage()]

    def test_delivered_prompt_does_not_warn(self, caplog, make_config):
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = True
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(id="sm4f", sender="alice@test.com", subject="Hi")
        email = _email(id="sm4f", sender="alice@test.com")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_confirmation_prompt", return_value=(True, 5)),
            caplog.at_level("WARNING", logger="istota.transport.email.inbound"),
        ):
            poll_emails(config)

        assert not [r for r in caplog.records if "could not be delivered" in r.getMessage()]

    def test_confirm_sender_match_does_not_reach_an_emissary_reply(self, make_config):
        """`confirm_sender_match` is about the own-address claim. Turning it on must
        not start holding a correspondent's reply, which makes no such claim.

        Named for the thread route being ungated *by design* until ISSUE-234, which
        narrowed it to the correspondent rather than removing it — this sender is
        the address the bot wrote to, so it stays ungated for the reason the name
        now gives. `TestThreadMatchConfirmationGate` covers the narrowing itself."""
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = True
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn, user_id="alice", message_id="<orig@test.com>",
                to_addr="external@reply.com", subject="Hello",
                conversation_token="room1",
            )

        envelope = _envelope(id="sm5", sender="external@reply.com", subject="Re: Hello")
        email = Email(
            id="sm5", subject="Re: Hello", sender="external@reply.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Thanks", attachments=[],
            message_id="<reply@reply.com>", references="<orig@test.com>",
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_ids[0]).status == "pending"

class TestThreadMatchConfirmationGate:
    """ISSUE-234 — a `Message-ID` the bot issued routes a reply *and* used to
    authorize it. Possession is disclosed to everyone Cc'd, everyone the thread is
    forwarded to, and every archive in the path, so the thread route now also asks
    who sent the mail: the envelope sender must be an address the bot actually
    wrote to on the matched thread, or the message meets the same gate the other
    two routes meet."""

    @staticmethod
    def _seed_thread(config, to_addr="external@reply.com", user_id="alice"):
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn, user_id=user_id, message_id="<orig@test.com>",
                to_addr=to_addr, subject="Hello",
                conversation_token="room1",
            )

    @staticmethod
    def _reply(sender, id="tm1", references="<orig@test.com>"):
        envelope = _envelope(id=id, sender=sender, subject="Re: Hello")
        email = Email(
            id=id, subject="Re: Hello", sender=sender,
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Thanks", attachments=[],
            message_id=f"<{id}@reply.com>", references=references,
            to=("bot@test.com",), cc=(),
        )
        return envelope, email

    def _poll(self, config, envelope, email, prompt_result=(True, 99)):
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_confirmation_prompt", return_value=prompt_result) as send,
        ):
            return poll_emails(config), send

    def test_stranger_holding_the_message_id_is_gated(self, make_config):
        """The reproduction from the entry: the only thing the attacker supplies is
        a References header, and before the fix that alone produced a running task."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(
            email_addresses=["alice@test.com"], alerts_channel="alerts_room",
        )}
        self._seed_thread(config)

        envelope, email = self._reply("attacker@evil.example")
        task_ids, send = self._poll(config, envelope, email)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert task.status == "pending_confirmation"
            # Pinned because it is the one property of a gated thread reply that
            # is inherited rather than chosen: the task keeps the origin room as
            # its token (`inbound.py`, "Continue the originating conversation"),
            # so unlike a gated plus-address message under a synthetic thread
            # hash it parks that room's foreground queue and is cancellable by
            # `cancel_pending_confirmations` on the room's next message. Not new
            # — a gated `sender_match` reply that also matched a thread has
            # always landed here, which is the case `web_app`'s cancel comment
            # describes — but this route widens who reaches it, and a silent
            # change to the token would move the blast radius without a test
            # noticing.
            assert task.conversation_token == "room1"

        prompt = send.call_args.args[2]
        assert "attacker@evil.example" in prompt
        assert "thread_match" in prompt

    def test_the_correspondent_we_wrote_to_is_not_gated(self, make_config):
        """The legitimate emissary reply — the common case, and the reason the route
        was left ungated in the first place. It must stay quiet."""
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = True
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}
        self._seed_thread(config)

        envelope, email = self._reply("external@reply.com")
        task_ids, _ = self._poll(config, envelope, email)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_ids[0]).status == "pending"

    def test_the_match_is_by_address_not_by_domain(self, make_config):
        """A colleague at the correspondent's domain is exactly the population a
        leaked Message-ID reaches first, so a domain match would wave through the
        most likely leak rather than catch it."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}
        self._seed_thread(config)

        envelope, email = self._reply("someone-else@reply.com")
        task_ids, _ = self._poll(config, envelope, email)

        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_ids[0]).status == "pending_confirmation"

    def test_trusting_the_sender_reopens_the_thread(self, make_config):
        """`!trust` had no effect on this route because the trust check was never
        consulted. It is now the way a forwarded thread is let through for good."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}
        self._seed_thread(config)
        with db.get_db(config.db_path) as conn:
            db.add_trusted_sender(conn, "alice", "colleague@reply.com")

        envelope, email = self._reply("colleague@reply.com")
        task_ids, _ = self._poll(config, envelope, email)

        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_ids[0]).status == "pending"

    def test_a_gated_thread_reply_is_not_mirrored_to_the_room(self, make_config):
        """Same contract as the other two routes: the mirror commits in the task's
        transaction, so attacker text must not reach the room before the answer."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}
        self._seed_thread(config)

        captured = {}
        real_ingest = inbound_module.ingest_message

        def _spy(conn, cfg, msg):
            captured["suppress"] = msg.suppress_transcript_mirror
            return real_ingest(conn, cfg, msg)

        envelope, email = self._reply("attacker@evil.example")
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_confirmation_prompt", return_value=(False, None)),
            patch("istota.transport.email.inbound.ingest_message", side_effect=_spy),
        ):
            poll_emails(config)

        assert captured["suppress"] is True

    def test_a_reply_to_a_multi_recipient_send_matches_any_of_them(self, make_config):
        """`to_addr` carries the whole recipient string when the send had several
        (`outbound_drafts` joins them with ', '), so all of them are correspondents."""
        config = make_config()
        config.email = _email_config()
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}
        self._seed_thread(config, to_addr="First <first@reply.com>, second@other.com")

        envelope, email = self._reply("second@other.com")
        task_ids, _ = self._poll(config, envelope, email)

        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_ids[0]).status == "pending"


class TestThreadReplyFromCorrespondent:
    """Unit coverage for the address comparison the thread gate rests on."""

    @staticmethod
    def _sent(to_addr):
        return db.SentEmail(
            id=1, user_id="alice", task_id=None, message_id="<m@test.com>",
            to_addr=to_addr, subject=None, thread_id=None, in_reply_to=None,
            references=None, conversation_token=None, sent_at="2026-01-01",
        )

    def test_exact_address_matches(self):
        assert thread_reply_from_correspondent(self._sent("a@b.com"), "a@b.com")

    def test_comparison_ignores_case_and_display_name(self):
        assert thread_reply_from_correspondent(
            self._sent("Alice Example <A@B.com>"), '"A. Example" <a@b.COM>',
        )

    def test_a_different_mailbox_at_the_same_domain_does_not_match(self):
        assert not thread_reply_from_correspondent(self._sent("a@b.com"), "c@b.com")

    def test_a_subdomain_does_not_match(self):
        """Suffix comparison would make `b.com.evil.example` a correspondent."""
        assert not thread_reply_from_correspondent(self._sent("a@b.com"), "a@x.b.com")
        assert not thread_reply_from_correspondent(self._sent("a@b.com"), "a@b.com.evil.example")

    def test_unicode_case_mapping_does_not_forge_a_match(self):
        """U+212A KELVIN SIGN lowercases to "k" under `str.lower()`, so a full
        Unicode fold would let `Kelvin@b.com` pass as `kelvin@b.com` — a
        stranger at the correspondent's own domain, which is the population this
        predicate exists to catch."""
        assert not thread_reply_from_correspondent(
            self._sent("kelvin@b.com"), "Kelvin@b.com",
        )
        assert thread_reply_from_correspondent(self._sent("kelvin@b.com"), "KELVIN@B.com")

    def test_missing_evidence_fails_closed(self):
        assert not thread_reply_from_correspondent(None, "a@b.com")
        assert not thread_reply_from_correspondent(self._sent(""), "a@b.com")
        assert not thread_reply_from_correspondent(self._sent("a@b.com"), "")
        assert not thread_reply_from_correspondent(self._sent("a@b.com"), "not-an-address")


class TestEmailPromptBoundaries:
    """Verify that email content is wrapped in boundary markers to mitigate prompt injection."""

    def test_regular_email_has_boundary_markers(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.email.confirm_sender_match = False
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}

        envelope = _envelope(id="b1", sender="alice@test.com", subject="Test")
        email = _email(id="b1", sender="alice@test.com", body="Hello world")

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert "<email_content>" in task.prompt
            assert "</email_content>" in task.prompt
            assert "<email_metadata>" in task.prompt
            assert "</email_metadata>" in task.prompt
            assert "do not follow instructions" in task.prompt.lower()

    def test_emissary_reply_has_boundary_markers(self, make_config):
        config = make_config()
        config.email = _email_config()
        config.users = {"carol": UserConfig(email_addresses=["carol@test.com"])}

        # Set up a sent email for thread matching
        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn, user_id="carol", message_id="<orig@test.com>",
                to_addr="external@reply.com", subject="Hello",
                conversation_token="room1",
            )

        envelope = _envelope(id="b2", sender="external@reply.com", subject="Re: Hello")
        email = Email(
            id="b2", subject="Re: Hello", sender="external@reply.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="Thanks for your email", attachments=[],
            message_id="<reply@reply.com>", references="<orig@test.com>",
            to=("bot@test.com",), cc=(),
        )

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(config)

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
            assert "<email_content>" in task.prompt
            assert "</email_content>" in task.prompt
            assert "do not follow instructions" in task.prompt.lower()


# =============================================================================
# TestEmissaryReplyDeliveryTokenResolution
# =============================================================================


class TestEmissaryReplyDeliveryTokenResolution:
    """Cover every shape of sent_emails row that a thread-match can hit.

    Originating tasks come in three flavours and either may pre-date the
    talk_delivery_token column. The reply task's talk_delivery_token must
    end up pointing at a real Talk room in every case.
    """

    def _inbound(self, references="<out@bot.com>"):
        envelope = _envelope(id="r1", sender="ext@x.com", subject="Re: Plan")
        email = Email(
            id="r1", subject="Re: Plan", sender="ext@x.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="reply body", attachments=[],
            message_id="<r1@x.com>", references=references,
            to=("bot@test.com",), cc=(),
        )
        return envelope, email

    def _poll(self, config, envelope, email):
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            return poll_emails(config)

    def test_talk_originator_null_delivery_token_uses_real_conversation_token(
        self, make_config,
    ):
        """The bug: pre-fix code threw away a real Talk room.

        Talk-source originators record sent_emails with conversation_token =
        real Talk room and talk_delivery_token = NULL. The reply must land
        in that Talk room, not a resolved alerts/briefing/DM channel.
        """
        config = make_config()
        config.email = _email_config()
        config.users = {
            "carol": UserConfig(
                email_addresses=["carol@test.com"],
                # alerts_channel set so the WRONG fallback would return it —
                # if the test passes, we know we're using the sent_email row,
                # not the user's resolved channel.
                alerts_channel="WRONG_alerts_channel",
            ),
        }

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Plan",
                conversation_token="original_talk_room",
                talk_delivery_token=None,  # pre-migration / talk-originator
            )

        envelope, email = self._inbound()
        task_ids = self._poll(config, envelope, email)
        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        assert task.user_id == "carol"
        assert task.talk_delivery_token == "original_talk_room"
        # conversation_token also preserved as the original Talk room
        # (transport/email/inbound.py: inherits from sent_email)
        assert task.conversation_token == "original_talk_room"

    def test_email_originator_null_delivery_token_synthetic_falls_back_to_alerts(
        self, make_config,
    ):
        """Email-originator with synthetic thread hash and NULL delivery token.

        The synthetic conversation_token isn't a real Talk room, so we must
        resolve via the user's alerts/briefing/DM rather than misroute.
        """
        synthetic = "deadbeef12345678"  # 16 lowercase hex
        config = make_config()
        config.email = _email_config()
        config.users = {
            "carol": UserConfig(
                email_addresses=["carol@test.com"],
                alerts_channel="carol_alerts",
            ),
        }

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Plan",
                conversation_token=synthetic,
                talk_delivery_token=None,  # pre-migration row
            )

        envelope, email = self._inbound()
        task_ids = self._poll(config, envelope, email)
        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        assert task.talk_delivery_token == "carol_alerts"
        # conversation_token preserved as the synthetic email-thread key
        assert task.conversation_token == synthetic

    def test_briefing_originator_null_delivery_token_uses_briefing_room(
        self, make_config,
    ):
        """Briefing-originated email: conversation_token IS the briefing room."""
        from istota.config import BriefingConfig as BriefConf
        config = make_config()
        config.email = _email_config()
        config.users = {
            "carol": UserConfig(
                email_addresses=["carol@test.com"],
                # alerts_channel deliberately empty so resolve_conversation_token
                # would pick the briefing — same value as the sent_email's
                # conversation_token. To prove we use the sent_email path
                # rather than resolve, set alerts to a different value.
                alerts_channel="other_alerts",
                briefings=[BriefConf(
                    name="morning", cron="0 8 * * *",
                    conversation_token="morning_briefing_room",
                )],
            ),
        }

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Briefing follow-up",
                conversation_token="morning_briefing_room",
                talk_delivery_token=None,
            )

        envelope, email = self._inbound()
        task_ids = self._poll(config, envelope, email)
        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        # Routes to the briefing room (which IS the original conversation_token),
        # not "other_alerts"
        assert task.talk_delivery_token == "morning_briefing_room"

    def test_null_conversation_and_delivery_falls_back_to_resolve(
        self, make_config,
    ):
        """Both NULL on sent_email — resolve via user config."""
        config = make_config()
        config.email = _email_config()
        config.users = {
            "carol": UserConfig(
                email_addresses=["carol@test.com"],
                alerts_channel="carol_alerts",
            ),
        }

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Hello",
                conversation_token=None,
                talk_delivery_token=None,
            )

        envelope, email = self._inbound()
        task_ids = self._poll(config, envelope, email)
        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        assert task.talk_delivery_token == "carol_alerts"

    def test_explicit_delivery_token_wins_over_conversation_token(
        self, make_config,
    ):
        """When sent_email has an explicit delivery token, it beats conversation_token."""
        config = make_config()
        config.email = _email_config()
        config.users = {
            "carol": UserConfig(
                email_addresses=["carol@test.com"],
                alerts_channel="WRONG_alerts",
            ),
        }

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Plan",
                conversation_token="some_other_room",
                talk_delivery_token="explicit_delivery_room",
            )

        envelope, email = self._inbound()
        task_ids = self._poll(config, envelope, email)
        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        assert task.talk_delivery_token == "explicit_delivery_room"

    def test_real_conversation_token_no_user_config_preserves_it(
        self, make_config,
    ):
        """No resolvable channel and a real-looking conversation_token: use it.

        Don't return None just because resolve_conversation_token can't help —
        the originating task already has a perfectly good Talk room recorded.
        """
        config = make_config()
        config.email = _email_config()
        # User exists for routing but has no alerts/briefing/DM
        config.users = {"carol": UserConfig(email_addresses=["carol@test.com"])}

        with db.get_db(config.db_path) as conn:
            db.record_sent_email(
                conn,
                user_id="carol",
                message_id="<out@bot.com>",
                to_addr="ext@x.com",
                subject="Plan",
                conversation_token="orig_room",
                talk_delivery_token=None,
            )

        envelope, email = self._inbound()
        task_ids = self._poll(config, envelope, email)
        assert len(task_ids) == 1

        with db.get_db(config.db_path) as conn:
            task = db.get_task(conn, task_ids[0])
        assert task.talk_delivery_token == "orig_room"


# =============================================================================
# TestEmissaryRecordingShape
# =============================================================================


class TestEmissaryRecordingShape:
    """What gets written to sent_emails when each task type sends an email.

    The thread-match logic above only works if sent_emails rows record the
    right fields for each originator type. These tests pin that contract.
    """

    def _config(self, db_path, tmp_path):
        from istota.config import (
            EmailConfig as AppEmail, NextcloudConfig, SchedulerConfig, TalkConfig,
        )
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=AppEmail(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
        )

    def test_record_sent_email_for_talk_source_task(self, db_path, tmp_path):
        """Talk-source task -> sent_emails.conversation_token = real Talk room."""
        from istota.transport.email.outbound import _record_sent_email
        config = self._config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Send email please", user_id="alice",
                source_type="talk", conversation_token="real_talk_room",
                # talk_delivery_token NULL: talk-source tasks rely on the
                # _talk_target_for_delivery fallback to conversation_token.
                talk_delivery_token=None,
            )
            task = db.get_task(conn, task_id)

        _record_sent_email(
            config, task,
            message_id="<sent@bot.com>",
            to_addr="ext@x.com",
            subject="Hello",
        )

        with db.get_db(db_path) as conn:
            row = db.find_sent_email_by_message_id(conn, "<sent@bot.com>")
        assert row is not None
        assert row.conversation_token == "real_talk_room"
        # The known-NULL talk_delivery_token is the data shape that the
        # the inbound fix has to handle correctly on the read side.
        assert row.talk_delivery_token is None
        assert row.user_id == "alice"

    def test_record_sent_email_for_email_source_task(self, db_path, tmp_path):
        """Email-source task -> sent_emails.talk_delivery_token populated."""
        from istota.transport.email.outbound import _record_sent_email
        config = self._config(db_path, tmp_path)
        synthetic = "abcdef0123456789"

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Reply to that", user_id="alice",
                source_type="email", conversation_token=synthetic,
                talk_delivery_token="alerts_channel_xyz",
            )
            task = db.get_task(conn, task_id)

        _record_sent_email(
            config, task,
            message_id="<sent2@bot.com>",
            to_addr="ext@x.com",
            subject="Re: Plan",
        )

        with db.get_db(db_path) as conn:
            row = db.find_sent_email_by_message_id(conn, "<sent2@bot.com>")
        assert row is not None
        assert row.conversation_token == synthetic
        assert row.talk_delivery_token == "alerts_channel_xyz"

    def test_record_sent_email_for_subtask_inherits_parent_tokens(
        self, db_path, tmp_path,
    ):
        """Subtask sending email -> sent_emails carries parent's tokens."""
        from istota.transport.email.outbound import _record_sent_email
        config = self._config(db_path, tmp_path)

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="parent", user_id="alice",
                source_type="talk", conversation_token="parent_talk_room",
            )
            sub_id = db.create_task(
                conn, prompt="child", user_id="alice",
                source_type="subtask", parent_task_id=parent_id,
                conversation_token="parent_talk_room",
                talk_delivery_token="parent_talk_room",
            )
            sub = db.get_task(conn, sub_id)

        _record_sent_email(
            config, sub,
            message_id="<sub@bot.com>",
            to_addr="ext@x.com",
        )

        with db.get_db(db_path) as conn:
            row = db.find_sent_email_by_message_id(conn, "<sub@bot.com>")
        assert row is not None
        assert row.conversation_token == "parent_talk_room"
        assert row.talk_delivery_token == "parent_talk_room"


# =============================================================================
# TestEmissaryLifecycle — end-to-end outbound -> inbound
# =============================================================================


class TestEmissaryLifecycle:
    """Round-trip tests: a task sends an email; the reply comes in and routes."""

    def _inbound_for(self, message_id):
        envelope = _envelope(id="lc1", sender="ext@x.com", subject="Re: Plan")
        email = Email(
            id="lc1", subject="Re: Plan", sender="ext@x.com",
            date="Mon, 01 Jan 2026 12:00:00 +0000",
            body="The reply", attachments=[],
            message_id="<reply_lc1@x.com>", references=f"<{message_id}>",
            to=("bot@test.com",), cc=(),
        )
        return envelope, email

    def _scheduler_config(self, db_path, tmp_path, alerts="alerts_room"):
        from istota.config import (
            EmailConfig as AppEmail, NextcloudConfig, SchedulerConfig, TalkConfig,
        )
        return Config(
            db_path=db_path,
            nextcloud=NextcloudConfig(),
            talk=TalkConfig(),
            email=AppEmail(),
            scheduler=SchedulerConfig(),
            temp_dir=tmp_path / "temp",
            users={"alice": UserConfig(
                email_addresses=["alice@test.com"],
                alerts_channel=alerts,
            )},
        )

    def _poller_config(self, db_path, tmp_path, alerts="alerts_room"):
        config = Config()
        config.db_path = db_path
        config.temp_dir = tmp_path / "temp"
        config.temp_dir.mkdir(exist_ok=True)
        config.skills_dir = tmp_path / "skills"
        config.skills_dir.mkdir(exist_ok=True)
        config.email = _email_config()
        config.users = {"alice": UserConfig(
            email_addresses=["alice@test.com"],
            alerts_channel=alerts,
        )}
        return config

    def test_talk_task_sends_email_reply_routes_to_original_room(
        self, db_path, tmp_path,
    ):
        """Full loop: talk task sends, external replies, routes to original room."""
        from istota.transport.email.outbound import _record_sent_email
        sched_cfg = self._scheduler_config(db_path, tmp_path, alerts="alerts_room")

        with db.get_db(db_path) as conn:
            tid = db.create_task(
                conn, prompt="send email", user_id="alice",
                source_type="talk", conversation_token="talkroom_42",
            )
            task = db.get_task(conn, tid)
        _record_sent_email(
            sched_cfg, task,
            message_id="<m_talk@bot.com>",
            to_addr="ext@x.com", subject="Plan",
        )

        # Inbound reply
        poll_cfg = self._poller_config(db_path, tmp_path, alerts="alerts_room")
        envelope, email = self._inbound_for("m_talk@bot.com")
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(poll_cfg)

        assert len(task_ids) == 1
        with db.get_db(db_path) as conn:
            new_task = db.get_task(conn, task_ids[0])
        assert new_task.user_id == "alice"
        assert new_task.conversation_token == "talkroom_42"
        # The reply routes back to the origin Talk room via the stored origin
        # descriptor (talk:<token>) rather than the talk_delivery_token ladder.
        assert new_task.output_target == "talk:talkroom_42,email"

    def test_email_task_sends_email_reply_routes_via_alerts(
        self, db_path, tmp_path,
    ):
        """Email-source originator: reply routes via the recorded delivery token."""
        from istota.transport.email.outbound import _record_sent_email
        sched_cfg = self._scheduler_config(db_path, tmp_path, alerts="alerts_room")
        synthetic = "0123456789abcdef"

        with db.get_db(db_path) as conn:
            tid = db.create_task(
                conn, prompt="reply", user_id="alice",
                source_type="email", conversation_token=synthetic,
                talk_delivery_token="alerts_room",
            )
            task = db.get_task(conn, tid)
        _record_sent_email(
            sched_cfg, task,
            message_id="<m_email@bot.com>",
            to_addr="ext@x.com", subject="Plan",
        )

        poll_cfg = self._poller_config(db_path, tmp_path, alerts="alerts_room")
        envelope, email = self._inbound_for("m_email@bot.com")
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(poll_cfg)

        assert len(task_ids) == 1
        with db.get_db(db_path) as conn:
            new_task = db.get_task(conn, task_ids[0])
        assert new_task.talk_delivery_token == "alerts_room"
        # conversation_token still preserves the original synthetic email-thread key
        assert new_task.conversation_token == synthetic

    def test_subtask_sends_email_reply_routes_to_parent_room(
        self, db_path, tmp_path,
    ):
        """Subtask of a talk task sends an email — reply must reach parent's room."""
        from istota.transport.email.outbound import _record_sent_email
        sched_cfg = self._scheduler_config(db_path, tmp_path, alerts="alerts_room")

        with db.get_db(db_path) as conn:
            parent_id = db.create_task(
                conn, prompt="parent", user_id="alice",
                source_type="talk", conversation_token="parent_room",
            )
            sub_id = db.create_task(
                conn, prompt="child", user_id="alice",
                source_type="subtask", parent_task_id=parent_id,
                conversation_token="parent_room",
                talk_delivery_token="parent_room",
            )
            sub = db.get_task(conn, sub_id)
        _record_sent_email(
            sched_cfg, sub,
            message_id="<m_sub@bot.com>",
            to_addr="ext@x.com", subject="Plan",
        )

        poll_cfg = self._poller_config(db_path, tmp_path)
        envelope, email = self._inbound_for("m_sub@bot.com")
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        ):
            task_ids = poll_emails(poll_cfg)

        assert len(task_ids) == 1
        with db.get_db(db_path) as conn:
            new_task = db.get_task(conn, task_ids[0])
        # Reply reaches the parent's room via the origin descriptor (talk:<token>).
        assert new_task.conversation_token == "parent_room"
        assert new_task.output_target == "talk:parent_room,email"


# =============================================================================
# TestDmarcCanary — ISSUE-228
# =============================================================================


class TestDmarcResultParsing:
    """The topmost ``Authentication-Results`` is parsed for a ``dmarc=`` methodspec.

    Parsing walks the ``;``-separated methodspecs and anchors ``dmarc=`` to the
    start of one, rather than grepping the whole header. A bare substring search
    matches ``header.from=`` properties and text inside a ``reason="..."`` or a
    parenthesized comment, all of which are attacker-influenced on a header we
    otherwise trust.
    """

    def test_pass(self):
        assert _dmarc_result("mx.test; spf=pass; dmarc=pass header.from=t.com") == "pass"

    def test_fail(self):
        assert _dmarc_result("mx.test; dmarc=fail header.from=t.com") == "fail"

    def test_none_is_a_result_not_an_absence(self):
        """``dmarc=none`` means the domain publishes no policy — the "DMARC record
        was edited away" drift case, not a missing evaluation."""
        assert _dmarc_result("mx.test; dmarc=none header.from=t.com") == "none"

    def test_temperror(self):
        assert _dmarc_result("mx.test; dmarc=temperror") == "temperror"

    def test_case_and_whitespace_insensitive(self):
        assert _dmarc_result("mx.test;   DMARC = PASS header.from=t.com") == "pass"

    def test_no_dmarc_methodspec_is_unevaluated(self):
        assert _dmarc_result("mx.test; spf=pass smtp.mailfrom=t.com; dkim=pass") is None

    def test_absent_header_is_unevaluated(self):
        assert _dmarc_result(None) is None

    def test_empty_header_is_unevaluated(self):
        assert _dmarc_result("") is None

    def test_property_named_dmarc_does_not_match(self):
        """``header.dmarc=pass`` is a property of another method, not a dmarc result."""
        assert _dmarc_result("mx.test; spf=fail header.dmarc=pass") is None

    def test_reason_string_containing_dmarc_is_never_read_as_a_verdict(self):
        """A quoted reason is free text the reporting MTA may echo from the message.
        It is not a verdict — and because the parser cannot tell an MTA that quoted
        the word from a sender who planted it, it refuses to call the header clean
        rather than answering the quiet "no verdict"."""
        assert _dmarc_result('mx.test; spf=fail reason="dmarc=pass claimed"') == "malformed"

    def test_parenthesized_comment_containing_dmarc_is_never_read_as_a_verdict(self):
        assert _dmarc_result("mx.test; spf=fail (dmarc=pass per sender)") == "malformed"

    def test_method_version_form_is_parsed(self):
        """RFC 8601 §2.2 allows `method / method-version`. Reading `dmarc/1=fail`
        as "no verdict" would make it silent under the default config — the exact
        failure mode this canary was filed against."""
        assert _dmarc_result("mx.test; dmarc/1=fail header.from=t.com") == "fail"
        assert _dmarc_result("mx.test; dmarc / 1 = pass") == "pass"

    def test_unregistered_result_token_is_bucketed(self):
        """The token reaches the alert-dedup key, and where this canary matters most
        the sender chose it. Left open it is an unbounded key axis — one alert per
        message, which is the flood the dedup exists to stop."""
        assert _dmarc_result("mx.test; dmarc=aaa") == "other"
        assert _dmarc_result("mx.test; dmarc=zzzz") == "other"

    def test_semicolon_inside_a_quoted_string_cannot_start_a_methodspec(self):
        """A naive split(";") lets quoted free text be promoted to the start of a
        methodspec, where it parses as a real result. Reporting MTAs echo the
        envelope sender into `smtp.mailfrom=`, so that text is attacker-supplied."""
        assert _dmarc_result('mx.test; spf=fail reason="blocked; dmarc=pass"; dmarc=fail') == "fail"
        assert _dmarc_result('mx.test; spf=fail smtp.mailfrom="a; dmarc=pass"@evil.com') == "malformed"

    def test_semicolon_inside_a_nested_comment_cannot_start_a_methodspec(self):
        """RFC 5322 comments nest, so a non-greedy `\\([^)]*\\)` strip stops at the
        first `)` and leaves the tail of the comment exposed."""
        assert _dmarc_result("mx.test; spf=fail (bad sig (rsa); dmarc=pass junk); dmarc=fail") == "fail"
        # Without the trailing genuine verdict, any-non-pass-wins can't carry the
        # assertion, so this is the form that actually discriminates the nesting
        # rule: depth-tracking keeps the injected pass inside the comment, while a
        # non-nesting strip would expose it and answer "pass".
        assert _dmarc_result("mx.test; spf=fail (bad sig (rsa); dmarc=pass junk)") != "pass"

    def test_an_injected_pass_cannot_mask_a_real_fail(self):
        """The end-to-end shape of the above, as a receiving MTA would actually
        write it: the attacker controls only the envelope sender, which lands in the
        SPF comment and in `smtp.mailfrom=`, while the genuine `dmarc=fail` sits at
        the end of the same header. Any non-pass must beat a pass."""
        header = (
            'mx.google.com; spf=softfail (google.com: domain of "x); dmarc=pass ("@evil.com '
            'does not designate 1.2.3.4 as permitted sender) smtp.mailfrom="x); dmarc=pass ("@evil.com; '
            "dmarc=fail (p=REJECT) header.from=victim.com"
        )
        assert _dmarc_result(header) == "fail"

    def test_an_unbalanced_quote_cannot_swallow_the_verdict(self):
        """Dropping quoted text means an *unterminated* quote drops the rest of the
        header, which can include the real verdict. Reporting "no verdict" there
        would be silent under the default config — so the attacker's cheapest move
        would be planting one stray quote."""
        header = 'mx.test; spf=fail smtp.mailfrom="evil; dmarc=fail (p=REJECT) header.from=victim.com'
        assert _dmarc_result(header) == "malformed"

    def test_an_unbalanced_comment_cannot_swallow_the_verdict(self):
        header = "mx.test; spf=fail (note: evil; dmarc=fail header.from=victim.com"
        assert _dmarc_result(header) == "malformed"

    def test_balanced_injected_quotes_cannot_hide_the_verdict(self):
        """The cheaper attack, and the one dropping quoted text does not stop on its
        own: a *balanced* pair straddling the genuine verdict hides it with nothing
        left unbalanced to notice. Two stray quotes echoed into `header.d=` and
        `smtp.mailfrom=` is the whole cost, and the answer would otherwise be "no
        verdict" — silent by default — or a `pass` appended afterwards."""
        hidden = (
            'mx.example.com; dkim=fail header.d="; dmarc=fail (p=reject) '
            'header.from=victim.com; spf=fail smtp.mailfrom="'
        )
        assert _dmarc_result(hidden) == "malformed"
        assert _dmarc_result(hidden + "; dmarc=pass") == "malformed"

    def test_balanced_injected_parens_cannot_hide_the_verdict(self):
        header = (
            "mx.example.com; dkim=fail header.d=(; dmarc=fail (p=reject) "
            "header.from=victim.com; spf=fail smtp.mailfrom=); dmarc=pass"
        )
        assert _dmarc_result(header) == "malformed"

    def test_an_escaped_quote_cannot_hide_the_verdict(self):
        assert _dmarc_result('mx; spf=fail smtp.mailfrom="a\\"; dmarc=fail; x="; dmarc=pass') == "malformed"

    def test_an_escaped_paren_in_a_comment_does_not_end_it(self):
        """RFC 5322 quoted-pairs are legal inside a comment, and a local-part holding
        a paren must be written `\\)`. Without honouring that, the comment ends early
        and the sender's text lands at methodspec position."""
        header = r'mx.test; spf=softfail (mx: domain of "a\); dmarc=pass ("@evil.com not permitted)'
        assert _dmarc_result(header) != "pass"

    def test_an_escaped_paren_does_not_make_a_balanced_header_look_broken(self):
        """The other direction: `\\(` must not deepen the comment, or a conforming
        header reports as unreadable and warns for no reason."""
        assert _dmarc_result(r"mx.test; spf=pass (mx.test: \( escaped) ; dmarc=pass header.from=t.com") == "pass"

    def test_a_real_world_pass_header_is_not_flagged(self):
        """The read-completeness count must not fire on ordinary mail, or the canary
        warns on every healthy message and gets ignored."""
        assert _dmarc_result(
            "mx.google.com; dkim=pass header.i=@t.com header.b=abc; "
            "spf=pass smtp.mailfrom=a@t.com; dmarc=pass header.from=t.com"
        ) == "pass"
        assert _dmarc_result(
            "mx.google.com;\r\n\tdkim=pass header.i=@t.com;\r\n\tdmarc=pass header.from=t.com"
        ) == "pass"

    def test_a_malformed_header_never_reports_pass(self):
        """A pass read out of a header we could not finish reading is not a pass."""
        assert _dmarc_result('mx.test; dmarc=pass; spf=fail smtp.mailfrom="oops') == "malformed"

    def test_an_explicit_fail_outranks_the_malformed_verdict(self):
        """A verdict actually read beats the generic "could not read it all"."""
        assert _dmarc_result('mx.test; dmarc=fail; spf=fail smtp.mailfrom="oops') == "fail"

    def test_a_later_fail_beats_an_earlier_pass(self):
        """Order must not decide it — the parser cannot promise no sender-supplied
        text ever reaches the start of a segment, so preferring the non-pass makes
        an injection at worst noisy, never quiet."""
        assert _dmarc_result("mx.test; dmarc=pass; dmarc=fail") == "fail"
        assert _dmarc_result("mx.test; dmarc=fail; dmarc=pass") == "fail"


class TestAuthenticationResultsIsTopmost:
    """Only the topmost ``Authentication-Results`` is stamped by the final receiving
    MTA. Every header below it can be forged by the sender, who simply includes it
    in the message they send."""

    def test_to_full_email_carries_the_topmost_header(self):
        from imap_tools import MailMessage

        from istota.skills.email import _msg_to_email

        raw = (
            b"Authentication-Results: mx.test; dmarc=fail header.from=test.com\r\n"
            b"Authentication-Results: forged.example; dmarc=pass header.from=test.com\r\n"
            b"From: alice@test.com\r\n"
            b"Subject: Hi\r\n"
            b"Message-ID: <m1@test.com>\r\n"
            b"\r\n"
            b"body\r\n"
        )
        email = _msg_to_email(MailMessage.from_bytes(raw))

        assert email.authentication_results is not None
        assert email.authentication_results.startswith("mx.test")
        assert _dmarc_result(email.authentication_results) == "fail"

    def test_a_forged_pass_below_a_genuine_fail_does_not_win(self):
        """The whole point: a spoofer appending their own ``dmarc=pass`` must not
        silence the canary that the real MTA's ``dmarc=fail`` should trip."""
        from imap_tools import MailMessage

        from istota.skills.email import _msg_to_email

        raw = (
            b"Authentication-Results: mx.test; dmarc=fail header.from=test.com\r\n"
            b"Authentication-Results: mx.test; dmarc=pass header.from=test.com\r\n"
            b"From: alice@test.com\r\n"
            b"Subject: Hi\r\n"
            b"\r\n"
            b"body\r\n"
        )
        email = _msg_to_email(MailMessage.from_bytes(raw))

        assert _dmarc_result(email.authentication_results) == "fail"


class TestDmarcCanary:
    """ISSUE-228 — with ``confirm_sender_match`` off (the default), a ``From:``
    matching the user's own address is taken as proof that user sent the mail.
    That is only sound because the receiving MTA rejected forgeries before the
    poller ever saw the folder. Nothing in the code could see whether that was
    still true. The canary reads the MTA's own stamp and says so when it isn't.

    It detects misconfiguration and drift, not attack: an attacker who forges the
    topmost header silences it. That is acceptable because the MTA is the
    boundary, not this check.
    """

    @pytest.fixture(autouse=True)
    def _clear_dedup(self):
        inbound_module._reset_dmarc_alert_dedup()
        yield
        inbound_module._reset_dmarc_alert_dedup()

    def _config(self, make_config, **email_overrides):
        config = make_config()
        config.email = _email_config()
        for key, val in email_overrides.items():
            setattr(config.email, key, val)
        config.users = {"alice": UserConfig(
            email_addresses=["alice@test.com"],
            alerts_channel="alerts_room",
        )}
        return config

    def _poll(self, config, envelope, email):
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_notification", return_value=True) as alert,
        ):
            task_ids = poll_emails(config)
        return task_ids, alert

    def test_dmarc_pass_is_silent(self, make_config, caplog):
        config = self._config(make_config)
        email = _email(id="d1", sender="alice@test.com",
                       authentication_results="mx.test; dmarc=pass header.from=test.com")

        with caplog.at_level("WARNING"):
            task_ids, alert = self._poll(config, _envelope(id="d1", sender="alice@test.com"), email)

        assert len(task_ids) == 1
        assert alert.call_count == 0
        assert "dmarc" not in caplog.text.lower()

    def test_dmarc_fail_warns_and_alerts(self, make_config, caplog):
        config = self._config(make_config)
        email = _email(id="d2", sender="alice@test.com",
                       authentication_results="mx.test; dmarc=fail header.from=test.com")

        with caplog.at_level("WARNING"):
            task_ids, alert = self._poll(config, _envelope(id="d2", sender="alice@test.com"), email)

        assert alert.call_count == 1
        assert alert.call_args.kwargs["purpose"] == "alert"
        assert "alice@test.com" in alert.call_args.args[2]
        assert "alice@test.com" in caplog.text
        assert "dmarc=fail" in caplog.text.lower()

    def test_dmarc_none_warns(self, make_config, caplog):
        """``dmarc=none`` is the "DMARC record was edited away" drift case — the
        exact silent degradation this canary exists to catch."""
        config = self._config(make_config)
        email = _email(id="d3", sender="alice@test.com",
                       authentication_results="mx.test; dmarc=none header.from=test.com")

        with caplog.at_level("WARNING"):
            _, alert = self._poll(config, _envelope(id="d3", sender="alice@test.com"), email)

        assert alert.call_count == 1

    def test_missing_header_is_silent_by_default(self, make_config, caplog):
        """A mail path that stamps nothing would otherwise warn on every message."""
        config = self._config(make_config)
        email = _email(id="d4", sender="alice@test.com", authentication_results=None)

        with caplog.at_level("WARNING"):
            _, alert = self._poll(config, _envelope(id="d4", sender="alice@test.com"), email)

        assert alert.call_count == 0
        assert "dmarc" not in caplog.text.lower()

    def test_missing_header_warns_when_opted_in(self, make_config, caplog):
        """The 'mailbox moved to a provider that does not stamp' drift case is only
        reachable for an operator who knows their MTA is supposed to stamp."""
        config = self._config(make_config, dmarc_canary_warn_on_missing=True)
        email = _email(id="d5", sender="alice@test.com", authentication_results=None)

        with caplog.at_level("WARNING"):
            _, alert = self._poll(config, _envelope(id="d5", sender="alice@test.com"), email)

        assert alert.call_count == 1
        assert "no dmarc result" in caplog.text.lower()

    def test_header_without_a_dmarc_methodspec_follows_the_missing_rule(self, make_config, caplog):
        """A header that authenticated SPF but never evaluated DMARC is absence of
        evidence, not evidence of failure — same class as no header at all."""
        config = self._config(make_config)
        email = _email(id="d6", sender="alice@test.com",
                       authentication_results="mx.test; spf=pass smtp.mailfrom=test.com")

        with caplog.at_level("WARNING"):
            _, alert = self._poll(config, _envelope(id="d6", sender="alice@test.com"), email)

        assert alert.call_count == 0

    def test_plus_address_route_with_a_self_claim_is_watched_too(self, make_config, caplog):
        """ISSUE-227 found that scoping this to ``sender_match`` is bypassable: the
        plus-address is public, so ``From: <user>`` + ``To: bot+<user>@…`` carries the
        identical own-address claim on a route a sender-match-only check never sees.
        The gate collapsed both routes; the canary that watches the gate's assumption
        has to cover the same set or it has the same hole."""
        config = self._config(make_config)
        envelope = _envelope(id="d7", sender="alice@test.com")
        email = _email(id="d7", sender="alice@test.com", to=("bot+alice@test.com",),
                       authentication_results="mx.test; dmarc=fail header.from=test.com")

        with caplog.at_level("WARNING"):
            task_ids, alert = self._poll(config, envelope, email)

        assert len(task_ids) == 1
        assert alert.call_count == 1
        assert "plus_address" in caplog.text

    def test_external_sender_is_not_watched(self, make_config, caplog):
        """The canary guards one assumption: that a ``From:`` naming the user's own
        address proves the user sent it. Mail from a genuinely external sender never
        leans on that claim — it is gated or trusted on its own terms."""
        config = self._config(make_config)
        config.users["alice"].trusted_email_senders = ["carol@vendor.com"]
        envelope = _envelope(id="d8", sender="carol@vendor.com")
        email = _email(id="d8", sender="carol@vendor.com", to=("bot+alice@test.com",),
                       authentication_results="mx.test; dmarc=fail header.from=vendor.com")

        with caplog.at_level("WARNING"):
            _, alert = self._poll(config, envelope, email)

        assert alert.call_count == 0

    def test_disabled_canary_is_silent_on_an_outright_fail(self, make_config, caplog):
        config = self._config(make_config, dmarc_canary=False)
        email = _email(id="d9", sender="alice@test.com",
                       authentication_results="mx.test; dmarc=fail header.from=test.com")

        with caplog.at_level("WARNING"):
            _, alert = self._poll(config, _envelope(id="d9", sender="alice@test.com"), email)

        assert alert.call_count == 0
        assert "dmarc" not in caplog.text.lower()

    def test_the_canary_never_blocks_the_mail(self, make_config):
        """It is a detector, not a gate. A failing check must not change what happens
        to the message — that call belongs to ``confirm_sender_match``."""
        config = self._config(make_config)
        email = _email(id="d10", sender="alice@test.com",
                       authentication_results="mx.test; dmarc=fail header.from=test.com")

        task_ids, _ = self._poll(config, _envelope(id="d10", sender="alice@test.com"), email)

        assert len(task_ids) == 1
        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_ids[0]).status == "pending"

    def test_alert_is_deduped_but_the_log_is_not(self, make_config, caplog):
        """A persistently broken mail path must not flood the alert channel, but the
        log has to keep a per-message record."""
        config = self._config(make_config)
        envelopes = [_envelope(id=f"d11-{i}", sender="alice@test.com") for i in range(3)]
        emails = [_email(id=f"d11-{i}", sender="alice@test.com",
                         authentication_results="mx.test; dmarc=fail header.from=test.com")
                  for i in range(3)]

        with caplog.at_level("WARNING"):
            with (
                patch("istota.transport.email.inbound.list_emails", return_value=envelopes),
                patch("istota.transport.email.inbound.read_email", side_effect=emails),
                patch("istota.transport.email.inbound.download_attachments", return_value=[]),
                patch("istota.notifications.send_notification", return_value=True) as alert,
            ):
                task_ids = poll_emails(config)

        assert len(task_ids) == 3
        assert alert.call_count == 1
        assert caplog.text.lower().count("dmarc=fail") == 3

    def test_a_different_verdict_alerts_again(self, make_config):
        """Dedup is per verdict, so a path degrading from ``fail`` to ``none`` is not
        swallowed by the earlier alert."""
        config = self._config(make_config)
        envelopes = [_envelope(id="d12-a", sender="alice@test.com"),
                     _envelope(id="d12-b", sender="alice@test.com")]
        emails = [_email(id="d12-a", sender="alice@test.com",
                         authentication_results="mx.test; dmarc=fail"),
                  _email(id="d12-b", sender="alice@test.com",
                         authentication_results="mx.test; dmarc=none")]

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=envelopes),
            patch("istota.transport.email.inbound.read_email", side_effect=emails),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_notification", return_value=True) as alert,
        ):
            poll_emails(config)

        assert alert.call_count == 2

    def test_a_failing_alert_does_not_break_the_poll(self, make_config):
        """The canary is best-effort monitoring; an unreachable alert surface must
        not cost the user their mail."""
        config = self._config(make_config)
        email = _email(id="d13", sender="alice@test.com",
                       authentication_results="mx.test; dmarc=fail")

        with (
            patch("istota.transport.email.inbound.list_emails",
                  return_value=[_envelope(id="d13", sender="alice@test.com")]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_notification",
                  side_effect=RuntimeError("talk down")),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

    def test_an_unreadable_header_alerts_under_the_default_config(self, make_config, caplog):
        """The end-to-end half of the parser's `malformed` rule. Checking only
        `_dmarc_result`'s return value leaves the behaviour that matters untested —
        that an unreadable header alerts rather than falling into the silent
        no-verdict class, which is what would let a planted delimiter silence it."""
        config = self._config(make_config)
        email = _email(id="d17", sender="alice@test.com",
                       authentication_results='mx.test; spf=fail smtp.mailfrom="oops')

        with caplog.at_level("WARNING"):
            _, alert = self._poll(config, _envelope(id="d17", sender="alice@test.com"), email)

        assert alert.call_count == 1
        assert "unreadable" in caplog.text.lower()

    def test_quiet_sender_mail_still_reports_on_the_mail_path(self, make_config, caplog):
        """A quiet sender's mail is filed with no task, by a branch that skips to the
        next message before the gate. The canary sits above it deliberately: quieting
        a sender says nothing about whether the mail path still authenticates From:,
        and that branch skipping first would blind the canary for every quieted
        self-address."""
        config = self._config(make_config)
        config.users["alice"].quiet_email_senders = ["alice@test.com"]
        email = _email(id="d14", sender="alice@test.com",
                       authentication_results="mx.test; dmarc=fail header.from=test.com")

        with caplog.at_level("WARNING"):
            task_ids, alert = self._poll(config, _envelope(id="d14", sender="alice@test.com"), email)

        assert task_ids == []
        assert alert.call_count == 1
        assert "dmarc=fail" in caplog.text.lower()

    def test_a_failed_delivery_does_not_consume_the_dedup_window(self, make_config):
        """The window opens on a delivered alert, not a decided one. `send_notification`
        reports "no destination configured" by returning False rather than raising, and
        stamping the dedup at decision time would swallow the next 24 hours of alerts
        after a single silent failure."""
        config = self._config(make_config)

        def _poll_once(email_id, delivered):
            with (
                patch("istota.transport.email.inbound.list_emails",
                      return_value=[_envelope(id=email_id, sender="alice@test.com")]),
                patch("istota.transport.email.inbound.read_email",
                      return_value=_email(id=email_id, sender="alice@test.com",
                                          authentication_results="mx.test; dmarc=fail")),
                patch("istota.transport.email.inbound.download_attachments", return_value=[]),
                patch("istota.notifications.send_notification", return_value=delivered) as alert,
            ):
                poll_emails(config)
            return alert

        assert _poll_once("d15-a", False).call_count == 1
        # Undelivered, so the next occurrence must try again rather than be throttled.
        assert _poll_once("d15-b", True).call_count == 1
        # Delivered now, so this one is throttled.
        assert _poll_once("d15-c", True).call_count == 0

    def test_the_alert_is_sent_after_the_poll_transaction_closes(self, make_config):
        """`poll_emails` holds one write transaction across the whole envelope loop, and
        an alert can route to a surface that writes to the same DB — the web surface
        does. Sending in-loop makes that second connection block on the poller's own
        lock until the busy timeout, stalling the scheduler's dispatch loop."""
        config = self._config(make_config)
        config.users["alice"].email_addresses = ["alice@test.com", "alice2@test.com"]
        observed = {}

        def _fake_send(cfg, user_id, message, **kwargs):
            # A second connection writing the same DB: this raises "database is
            # locked" if the poller's transaction is still open.
            with db.get_db(cfg.db_path) as conn:
                conn.execute(
                    "INSERT INTO processed_emails (email_id, sender_email, subject) "
                    "VALUES (?, ?, ?)",
                    (f"canary-probe-{len(observed)}", "probe@test.com", "probe"),
                )
            observed[len(observed)] = True
            return True

        # Two envelopes from *different* senders, so each decides its own alert and
        # the first iteration's writes are already pending on the poller's
        # connection by the time the second one is decided. With a single envelope
        # nothing has been written yet when the canary runs, so there is no lock to
        # contend for and an in-loop send would pass.
        envelopes = [_envelope(id="d16-a", sender="alice@test.com"),
                     _envelope(id="d16-b", sender="alice2@test.com")]
        emails = [_email(id="d16-a", sender="alice@test.com",
                         authentication_results="mx.test; dmarc=fail"),
                  _email(id="d16-b", sender="alice2@test.com",
                         authentication_results="mx.test; dmarc=fail")]

        with (
            patch("istota.transport.email.inbound.list_emails", return_value=envelopes),
            patch("istota.transport.email.inbound.read_email", side_effect=emails),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_notification", side_effect=_fake_send),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 2
        assert len(observed) == 2
