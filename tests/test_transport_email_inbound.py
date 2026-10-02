"""Tests for the EmailTransport inbound body (``transport/email/inbound.py``:
``poll_emails`` + routing precedence + confirmation gate) and the shared email
helpers it depends on (``istota.mail.support``: subject normalization, thread
id, config adapter, IMAP cleanup)."""

import json
from contextlib import ExitStack, contextmanager
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import Config, EmailConfig as AppEmailConfig, UserConfig
from istota.mail.ownership import thread_reply_from_correspondent
from istota.mail.support import (
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

_INBOUND = "istota.transport.email.inbound"
_PROMPT = "istota.notifications.delivery.send_confirmation_prompt"
_ALERT = "istota.notifications.delivery.send_notification"


@pytest.fixture(autouse=True)
def _clear_volume_state():
    """Reset the poller's in-process counters between tests.

    `_prompt_counts` collapses confirmation prompts past a few per
    (user, sender) per window (ISSUE-250). It is module-level and the window is
    an hour, so without this a test that expects a prompt fails purely because
    earlier tests in the same worker process already spent the sender's budget.
    Module-scoped rather than per-class: any test here that drives the gate
    spends it. The DMARC alert dedup is module-level for the same reason.
    """
    inbound_module._reset_volume_state()
    inbound_module._reset_dmarc_alert_dedup()
    yield
    inbound_module._reset_volume_state()
    inbound_module._reset_dmarc_alert_dedup()


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


@pytest.fixture
def mail_config(make_config):
    """A Config with email enabled, the given users, and `[email]` overrides."""
    def _make(users=None, **email_settings):
        config = make_config()
        config.email = _email_config()
        for key, val in email_settings.items():
            setattr(config.email, key, val)
        if users is not None:
            config.users = users
        return config
    return _make


@pytest.fixture
def alice_config(mail_config):
    """The DMARC-canary shape: one user, alice, with an alerts room."""
    def _make(**email_settings):
        return mail_config(_users("alice", alerts_channel="alerts_room"), **email_settings)
    return _make


def _users(*names, **settings):
    """`{name: UserConfig(email_addresses=["<name>@test.com"], **settings)}`."""
    return {
        name: UserConfig(email_addresses=[f"{name}@test.com"], **settings)
        for name in names
    }


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
           to=("bot@test.com",), cc=(), message_id="<msg1@test.com>",
           references=None, in_reply_to=None, authentication_results=None,
           authentication_results_all=()):
    return Email(
        id=id, subject=subject, sender=sender,
        date="Mon, 01 Jan 2026 10:00:00 +0000",
        body=body, attachments=[],
        message_id=message_id, references=references, in_reply_to=in_reply_to,
        to=to, cc=cc, authentication_results=authentication_results,
        authentication_results_all=authentication_results_all,
    )


def _self_claim(id, *, header=None, headers=(), sender="alice@test.com"):
    """A mail from alice's own address carrying the given Authentication-Results."""
    return _email(id=id, sender=sender, authentication_results=header,
                  authentication_results_all=tuple(headers))


@contextmanager
def _inbox(*emails):
    """Serve `emails` from a mocked IMAP folder, one envelope per message."""
    envelopes = [_envelope(id=e.id, subject=e.subject, sender=e.sender) for e in emails]
    if len(emails) == 1:
        read = patch(f"{_INBOUND}.read_email", return_value=emails[0])
    else:
        read = patch(f"{_INBOUND}.read_email", side_effect=list(emails))
    with (
        patch(f"{_INBOUND}.list_emails", return_value=envelopes),
        read,
        patch(f"{_INBOUND}.download_attachments", return_value=[]),
    ):
        yield


def _run_poll(config, *emails, prompt=None, alert=None):
    """Poll `emails`, optionally stubbing the confirmation prompt's return value
    and the alert sender's. Returns (task_ids, prompt_mock, alert_mock); a
    mock that was not stubbed is None."""
    prompt_mock = alert_mock = None
    with ExitStack() as stack:
        stack.enter_context(_inbox(*emails))
        if prompt is not None:
            prompt_mock = stack.enter_context(patch(_PROMPT, return_value=prompt))
        if alert is not None:
            alert_mock = stack.enter_context(patch(_ALERT, return_value=alert))
        task_ids = poll_emails(config)
    return task_ids, prompt_mock, alert_mock


def _poll(config, *emails):
    return _run_poll(config, *emails)[0]


def _poll_prompted(config, *emails, result=(True, 7)):
    task_ids, send, _ = _run_poll(config, *emails, prompt=result)
    return task_ids, send


def _poll_alerted(config, *emails, delivered=True):
    task_ids, _, alert = _run_poll(config, *emails, alert=delivered)
    return task_ids, alert


def _ingested(config, email, *, prompt=None):
    """Poll one mail and return the message the poller handed to ingest."""
    seen = []
    real_ingest = inbound_module.ingest_message

    def _spy(conn, cfg, msg):
        seen.append(msg)
        return real_ingest(conn, cfg, msg)

    with ExitStack() as stack:
        stack.enter_context(_inbox(email))
        if prompt is not None:
            stack.enter_context(patch(_PROMPT, return_value=prompt))
        stack.enter_context(patch(f"{_INBOUND}.ingest_message", side_effect=_spy))
        poll_emails(config)
    return seen[0]


def _task(config, task_id):
    with db.get_db(config.db_path) as conn:
        return db.get_task(conn, task_id)


def _single_task(config, *emails):
    """Poll, require exactly one task, and return it."""
    task_ids = _poll(config, *emails)
    assert len(task_ids) == 1
    return _task(config, task_ids[0])


def _processed(config, email_id):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT routing_method, user_id FROM processed_emails WHERE email_id = ?",
            (email_id,),
        ).fetchone()


def _sent(config, message_id, *, user_id="carol", to_addr="ext@x.com",
          subject="Hello", **fields):
    with db.get_db(config.db_path) as conn:
        db.record_sent_email(
            conn, user_id=user_id, message_id=message_id,
            to_addr=to_addr, subject=subject, **fields,
        )


def _scheduler_config(db_path, tmp_path, users=None):
    from istota.config import NextcloudConfig, SchedulerConfig, TalkConfig
    return Config(
        db_path=db_path,
        nextcloud=NextcloudConfig(),
        talk=TalkConfig(),
        email=AppEmailConfig(),
        scheduler=SchedulerConfig(),
        temp_dir=tmp_path / "temp",
        users=users or {},
    )


# =============================================================================
# TestNormalizeSubject
# =============================================================================


@pytest.mark.parametrize("subject, expected", [
    ("Hello World", "hello world"),
    ("Re: Hello", "hello"),
    ("Fwd: Hello", "hello"),
    ("Re: Fwd: Re: Hello", "hello"),
    ("RE: FWD: Hello", "hello"),
    ("Fw: Hello", "hello"),
    ("  Hello   World  ", "hello world"),
    ("IMPORTANT Meeting", "important meeting"),
], ids=[
    "basic", "re", "fwd", "multiple", "case_insensitive", "fw",
    "whitespace", "lowercase",
])
def test_normalize_subject(subject, expected):
    assert normalize_subject(subject) == expected


# =============================================================================
# TestComputeThreadId
# =============================================================================


class TestComputeThreadId:
    @pytest.mark.parametrize("a, b", [
        (("Hello", ["a@test.com", "b@test.com"]), ("Hello", ["a@test.com", "b@test.com"])),
        (("Hello", ["b@test.com", "a@test.com"]), ("Hello", ["a@test.com", "b@test.com"])),
        (("Re: Hello", ["a@test.com"]), ("Hello", ["a@test.com"])),
    ], ids=["deterministic", "sorted_participants", "normalized_subject"])
    def test_same_thread(self, a, b):
        assert compute_thread_id(*a) == compute_thread_id(*b)

    def test_length_16(self):
        assert len(compute_thread_id("Hello", ["a@test.com"])) == 16

    def test_different_subjects_different_ids(self):
        id1 = compute_thread_id("Hello", ["a@test.com"])
        id2 = compute_thread_id("Goodbye", ["a@test.com"])
        assert id1 != id2


# =============================================================================
# TestPollEmails
# =============================================================================


class TestPollEmails:
    def test_creates_task_for_known_sender(self, mail_config):
        config = mail_config(_users("alice"))

        with (
            _inbox(_email()),
            patch(f"{_INBOUND}.ensure_user_directories_v2"),
            patch(f"{_INBOUND}.upload_file_to_inbox_v2"),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1
        task = _task(config, task_ids[0])
        assert task is not None
        assert task.user_id == "alice"
        assert task.source_type == "email"
        assert "alice@test.com" in task.prompt

    def test_skips_processed_email(self, mail_config):
        config = mail_config(_users("alice"))

        with db.get_db(config.db_path) as conn:
            db.mark_email_processed(conn, email_id="1", sender_email="alice@test.com", subject="Hello")

        with patch(f"{_INBOUND}.list_emails", return_value=[_envelope()]):
            assert poll_emails(config) == []

    def test_skips_bot_email(self, mail_config):
        config = mail_config(_users("alice"))

        with patch(f"{_INBOUND}.list_emails", return_value=[_envelope(sender="bot@test.com")]):
            assert poll_emails(config) == []

        with db.get_db(config.db_path) as conn:
            assert db.is_email_processed(conn, "1")

    def test_skips_unknown_sender(self, mail_config):
        config = mail_config(_users("alice"))

        assert _poll(config, _email(sender="stranger@unknown.com")) == []

        # Marked as processed, but no task created
        with db.get_db(config.db_path) as conn:
            assert db.is_email_processed(conn, "1")

    def test_disabled_returns_empty(self, make_config):
        config = make_config()
        config.email = AppEmailConfig(enabled=False)

        assert poll_emails(config) == []

    def test_handles_list_error(self, mail_config):
        config = mail_config()

        with patch(f"{_INBOUND}.list_emails", side_effect=Exception("IMAP connection failed")):
            assert poll_emails(config) == []


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

        assert cleanup_old_emails(config, days=7) == 0

    def test_zero_days_returns_zero(self, mail_config):
        assert cleanup_old_emails(mail_config(), days=0) == 0

    def test_deletes_expired_emails(self, mail_config):
        with patch(
            "istota.mail.support.delete_emails_before", return_value=1,
        ) as mock_delete:
            result = cleanup_old_emails(mail_config(), days=7)

        assert result == 1
        mock_delete.assert_called_once()

    def test_handles_imap_error(self, mail_config):
        with patch(
            "istota.mail.support.delete_emails_before",
            side_effect=Exception("IMAP error"),
        ):
            assert cleanup_old_emails(mail_config(), days=7) == 0


# =============================================================================
# TestGetEmailConfig
# =============================================================================


class TestGetEmailConfig:
    def test_converts_config(self, mail_config):
        email_config = get_email_config(mail_config())

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
    _config = EmailConfig(
        imap_host="imap.test", imap_port=993,
        imap_user="u", imap_password="p",
        smtp_host="smtp.test", smtp_port=587,
        bot_email="bot@test.com",
    )

    def test_send_email_returns_message_id(self):
        from istota.skills.email import send_email
        with patch("istota.skills.email._send_smtp"):
            result = send_email(
                to="alice@test.com", subject="Hello", body="Hi", config=self._config,
            )
        assert result.startswith("<") and result.endswith(">")
        assert "@test.com>" in result

    def test_reply_to_email_returns_message_id(self):
        from istota.skills.email import reply_to_email
        with patch("istota.skills.email._send_smtp"):
            result = reply_to_email(
                to_addr="alice@test.com", subject="Hello", body="Reply",
                config=self._config, in_reply_to="<orig@test.com>",
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
    @staticmethod
    def _record(conn, message_id, **fields):
        fields.setdefault("to_addr", "bob@ext.com")
        fields.setdefault("subject", "Invite")
        db.record_sent_email(conn, user_id="carol", message_id=message_id, **fields)

    def test_match_by_references(self, db_path):
        from istota.transport.email.inbound import _match_thread

        with db.get_db(db_path) as conn:
            self._record(conn, "<sent1@bot.com>", subject="Meeting",
                         conversation_token="room1")

            email = _email(sender="bob@ext.com", subject="Re: Meeting",
                           references="<sent1@bot.com>")

            match = _match_thread(conn, email)
            assert match is not None
            assert match.user_id == "carol"
            assert match.conversation_token == "room1"

    @pytest.mark.parametrize("references, in_reply_to", [
        (None, None),
        ("<unknown@other.com>", None),
        (None, "<unknown@other.com>"),
    ], ids=["no_references", "unknown_references", "unknown_in_reply_to"])
    def test_no_match(self, db_path, references, in_reply_to):
        from istota.transport.email.inbound import _match_thread

        with db.get_db(db_path) as conn:
            email = _email(sender="bob@ext.com", subject="Re: Something",
                           references=references, in_reply_to=in_reply_to)

            assert _match_thread(conn, email) is None

    def test_match_multiple_references(self, db_path):
        """References header with multiple IDs — should match our sent one."""
        from istota.transport.email.inbound import _match_thread

        with db.get_db(db_path) as conn:
            self._record(conn, "<sent2@bot.com>", to_addr="alice@ext.com", subject="Hello")

            email = _email(sender="alice@ext.com", subject="Re: Hello",
                           references="<original@alice.com> <sent2@bot.com>")

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
            self._record(conn, "<sent3@bot.com>", conversation_token="room3")

            email = _email(sender="bob@ext.com", subject="Re: Invite",
                           references="<unrelated@peer.com>",
                           in_reply_to="<sent3@bot.com>")

            match = _match_thread(conn, email)
            assert match is not None
            assert match.user_id == "carol"
            assert match.conversation_token == "room3"

    def test_references_still_wins_over_in_reply_to(self, db_path):
        """References is the more complete chain, so it is consulted first."""
        from istota.transport.email.inbound import _match_thread

        with db.get_db(db_path) as conn:
            for mid in ("<refs@bot.com>", "<irt@bot.com>"):
                self._record(conn, mid)

            email = _email(sender="bob@ext.com", subject="Re: Invite",
                           references="<refs@bot.com>", in_reply_to="<irt@bot.com>")

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
            self._record(conn, "<sent-seam@bot.com>", conversation_token="room_seam")

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


# =============================================================================
# TestPollEmailsThreadMatching
# =============================================================================


class TestPollEmailsThreadMatching:
    """Tests for email poller routing emissary replies via thread matching."""

    def test_unknown_sender_reply_routes_to_originating_user(self, mail_config):
        """Reply from unknown sender matching a sent thread routes to originating user."""
        config = mail_config(_users("carol"))
        _sent(config, "<outbound@bot.com>", to_addr="external@proton.me",
              subject="Set up a meeting", conversation_token="talk_room_42")

        task = _single_task(config, _email(
            id="2", subject="Re: Set up a meeting", sender="external@proton.me",
            body="How about Tuesday?", references="<outbound@bot.com>",
        ))

        assert task.user_id == "carol"
        assert task.output_target == "talk,email"
        assert task.conversation_token == "talk_room_42"
        assert "Emissary email reply" in task.prompt
        assert "external@proton.me" in task.prompt
        assert "How about Tuesday?" in task.prompt

    def test_reply_with_unusable_references_routes_by_in_reply_to(self, mail_config):
        """The production shape: References unreadable, In-Reply-To exact.

        The reply was marked `discarded` — no owner, so no task, no
        notification anywhere — while In-Reply-To named our sent message
        exactly. The mail must route, and route to the sender's user.
        """
        config = mail_config(_users("carol"))
        _sent(config, "<outbound2@bot.com>", to_addr="external@proton.me",
              subject="Invite", conversation_token="room_7")

        task = _single_task(config, _email(
            id="9", subject="Re: Invite", sender="external@proton.me",
            # A run of encoded-words that no whitespace split can turn back
            # into message ids — exactly what arrived in production.
            references="=?us-ascii?Q?<a1@proton.me>_<outbound2@bot.com>?=",
            in_reply_to="<outbound2@bot.com>",
        ))

        assert task.user_id == "carol"
        assert task.conversation_token == "room_7"
        row = _processed(config, "9")
        assert row["routing_method"] == "thread_match"
        assert row["user_id"] == "carol"

    def test_unknown_sender_no_thread_match_discarded(self, mail_config):
        """Unknown sender with no thread match is discarded, recorded as such."""
        config = mail_config(_users("carol"))

        assert _poll(config, _email(id="3", subject="Buy stuff",
                                    sender="stranger@random.com")) == []

        with db.get_db(config.db_path) as conn:
            assert db.is_email_processed(conn, "3")
        assert _processed(config, "3")["routing_method"] == "discarded"

    def test_known_sender_still_works_normally(self, mail_config):
        """Known sender emails are routed normally (no output_target override)."""
        config = mail_config(_users("alice"))

        task = _single_task(config, _email(sender="alice@test.com"))

        assert task.user_id == "alice"
        assert task.output_target is None  # Normal email routing
        assert "Emissary" not in task.prompt

    def test_emissary_reply_without_conversation_token_uses_thread_id(self, mail_config):
        """If original sent email had no conversation_token, fall back to thread_id."""
        config = mail_config(_users("carol"))
        _sent(config, "<out@bot.com>", conversation_token=None)

        task = _single_task(config, _email(
            id="4", subject="Re: Hello", sender="ext@x.com", references="<out@bot.com>",
        ))

        assert task.user_id == "carol"
        assert task.output_target == "talk,email"
        # Should use thread_id since no conversation_token on sent email
        assert task.conversation_token is not None

    def test_thread_match_inherits_talk_delivery_token(self, mail_config):
        """ISSUE-057: thread_match inherits talk_delivery_token from sent_emails row."""
        config = mail_config(_users("carol"))
        _sent(config, "<out@bot.com>", subject="Plan",
              conversation_token="talk_room_99", talk_delivery_token="real_talk_room")

        task = _single_task(config, _email(
            id="9", subject="Re: Plan", sender="ext@x.com", references="<out@bot.com>",
        ))

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
        _sent(config, "<origin_out@bot.com>", subject="Question",
              conversation_token=sent_conversation_token, origin_target=origin_target)

        return _single_task(config, _email(
            id="20", subject="Re: Question", sender="ext@x.com",
            references="<origin_out@bot.com>",
        ))

    @pytest.mark.parametrize("origin_target, policy, token, expected", [
        ("web:rm_web123", None, "rm_web123", "web:rm_web123,email"),
        ("web:rm_web123", "origin", "rm_web123", "web:rm_web123"),
        ("web:rm_web123", "thread", None, "email"),
        ("talk:RealRoomXYZ", None, "RealRoomXYZ", "talk:RealRoomXYZ,email"),
    ], ids=[
        "web_default_policy_origin_plus_thread", "web_origin_only",
        "web_thread_only", "talk_descriptor_routes_to_token",
    ])
    def test_origin_policy(self, make_config, origin_target, policy, token, expected):
        # The pre-existing case: a descriptor naming no registered room at all
        # routes by the policy alone.
        task = self._origin_reply(
            make_config(), origin_target=origin_target, policy=policy,
            sent_conversation_token=token or "rm_web123",
        )
        assert task.output_target == expected
        if token is not None:
            assert task.conversation_token == token

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

    def test_a_stored_room_descriptor_is_used_as_is(self, make_config):
        """The form new sends stamp. No upgrade step — it already names the
        conversation, and expansion reads live bindings at delivery."""
        config = make_config()
        self._dual_bound(config)
        task = self._origin_reply(config, origin_target="room:rm_web123")
        assert task.output_target == "room:rm_web123,email"
        assert task.conversation_token == "rm_web123"

    def _archive(self, config):
        self._dual_bound(config)
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE rooms SET archived = 1 WHERE token = ?", ("rm_web123",),
            )

    def test_an_archived_origin_room_falls_back_rather_than_dropping(
        self, make_config,
    ):
        """A reply must never be lost because its room went away.

        The room is gone, so there is nothing to fan out to — but the email leg
        is still a real delivery and the reply reaches the contact who sent it.
        """
        config = make_config()
        self._archive(config)
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
        self._archive(config)
        task = self._origin_reply(config, origin_target="room:rm_web123")
        assert task.output_target == "room:rm_web123,email"
        plan = resolve_delivery_plan(config, task, None)
        assert [d.surface for d in plan] == ["email"]

    def _origin_sent_and_reply(self, config, *, sender, to, sent_to="ext@x.com"):
        """Record carol's origin-stamped send, poll one reply to it, and return
        the created task (or None)."""
        _sent(config, "<origin_out@bot.com>", to_addr=sent_to, subject="Question",
              conversation_token="rm_web123", origin_target="web:rm_web123")
        task_ids = _poll(config, _email(
            id="40", subject="Re: Question", sender=sender, to=to,
            references="<origin_out@bot.com>",
        ))
        if not task_ids:
            return None
        return _task(config, task_ids[0])

    def test_self_reply_via_sender_match_recovers_origin(self, mail_config):
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
        config = mail_config(_users("carol"))
        task = self._origin_sent_and_reply(
            config, sender="carol@test.com", to=("bot@test.com",), sent_to="carol@test.com",
        )
        assert task is not None
        assert task.output_target == "email"
        assert task.conversation_token == "rm_web123"
        # Self-reply → plain template, not "an external contact has replied".
        assert "Emissary email reply" not in task.prompt

    def test_self_reply_ignores_the_origin_policy(self, mail_config):
        """The suppression is per-message, not per-user, so it overrides the
        policy rather than being expressible through it (ISSUE-254). `origin`
        names the room and nothing else, and an empty plan would lose the reply
        — so it falls back to email, where the user wrote from."""
        config = mail_config(_users("carol", email_reply_routing="origin"))
        task = self._origin_sent_and_reply(
            config, sender="carol@test.com", to=("bot@test.com",), sent_to="carol@test.com",
        )
        assert task.output_target == "email"

    def test_plus_address_reply_recovers_origin(self, mail_config):
        # Dormant second path: a reply addressed to the bot's plus-address is
        # resolved at step 1, also pre-empting thread-match. The origin must
        # still be recovered.
        config = mail_config(_users("carol"))
        task = self._origin_sent_and_reply(
            config, sender="ext@x.com", to=("bot+carol@test.com",),
        )
        assert task is not None
        assert task.output_target == "web:rm_web123,email"
        assert task.conversation_token == "rm_web123"

    def test_external_thread_reply_keeps_emissary_prompt(self, mail_config):
        # An external contact (not a configured email, no plus-address) resolves
        # purely by thread-match → emissary template AND origin routing.
        config = mail_config(_users("carol"))
        task = self._origin_sent_and_reply(config, sender="ext@x.com", to=("bot@test.com",))
        assert task.output_target == "web:rm_web123,email"
        assert "Emissary email reply" in task.prompt

    def test_thread_row_for_other_user_not_applied(self, mail_config):
        # Defence-in-depth: a reply sender-matched to user A must not inherit the
        # origin descriptor of a thread row owned by user B (no cross-user
        # surface leak). Identity (sender/plus) wins; the mismatched payload is
        # dropped, so the reply falls back to the default email plan.
        config = mail_config(_users("alice", "carol"))
        task = self._origin_sent_and_reply(
            config, sender="alice@test.com", to=("bot@test.com",),
        )
        assert task is not None
        assert task.user_id == "alice"
        assert task.output_target is None  # mismatched origin dropped → default
        assert task.conversation_token != "rm_web123"

    @pytest.mark.parametrize("token", ["web-carol-deadbeef", "rm_example"])
    def test_legacy_null_origin_with_web_token_not_used_as_talk_channel(self, mail_config, token):
        # A legacy (pre-migration) sent_emails row with NULL origin_target whose
        # conversation_token is a web room token must NOT be used as a Talk
        # delivery channel (that would post to a nonexistent Talk room).
        config = mail_config(_users("carol", alerts_channel="alerts_room"))
        _sent(config, "<legacy_web@bot.com>", subject="Q",
              conversation_token=token, origin_target=None)  # legacy row

        task = _single_task(config, _email(
            id="30", subject="Re: Q", sender="ext@x.com", references="<legacy_web@bot.com>",
        ))
        assert task.output_target == "talk,email"
        # The web token must not leak in as the Talk channel; the ladder falls
        # through to the resolved alerts room instead.
        assert task.talk_delivery_token != token
        assert task.talk_delivery_token == "alerts_room"

    def test_known_sender_resolves_talk_delivery_token_from_alerts(self, mail_config):
        """plus_address / sender_match routes resolve talk_delivery_token via user config."""
        config = mail_config(_users("alice", alerts_channel="alice_alerts"))

        task = _single_task(config, _email(sender="alice@test.com"))

        # conversation_token is the synthetic email-thread hash
        assert task.conversation_token is not None
        assert len(task.conversation_token) == 16
        # talk_delivery_token resolves to the user's alerts channel
        assert task.talk_delivery_token == "alice_alerts"


# =============================================================================
# TestExtractUserFromRecipient
# =============================================================================


@pytest.mark.parametrize("to, cc, expected", [
    (("bot+carol@test.com",), (), "carol"),
    (("someone@other.com",), ("bot+alice@test.com",), "alice"),
    (("bot@test.com",), (), None),
    (("bot+nonexistent@test.com",), (), None),
    (("BOT+Carol@Test.Com",), (), "carol"),
    (("bot+carol@other-domain.com",), (), None),
    ((), (), None),
    # If both To and Cc have plus-addresses, To wins.
    (("bot+carol@test.com",), ("bot+alice@test.com",), "carol"),
], ids=[
    "to_header", "cc_header", "bare_bot_address", "invalid_user",
    "case_insensitive", "different_domain", "no_recipients", "first_valid_match_wins",
])
def test_extract_user_from_recipient(to, cc, expected):
    """Plus-address routing via recipient headers."""
    config = Config()
    config.email = _email_config()  # bot_email = "bot@test.com"
    config.users = {
        "carol": UserConfig(email_addresses=["carol@example.com"]),
        "alice": UserConfig(email_addresses=["alice@example.com"]),
    }
    assert _extract_user_from_recipient(config, _email(to=to, cc=cc)) == expected


# =============================================================================
# TestBotAddressedInTo
# =============================================================================


class TestBotAddressedInTo:
    """SG 5: email's explicit address is the bot in To, not only in Cc."""

    @pytest.mark.parametrize("to, cc, expected", [
        (("bot@test.com",), (), True),
        (("BOT@Test.com",), (), True),
        (("bot+carol@test.com",), (), True),
        (('"Istota" <bot+carol@test.com>',), (), True),
        (("carol@test.com",), ("bot@test.com",), False),
        (("carol@test.com",), ("bot+carol@test.com",), False),
        (("robot@test.com",), (), False),
        (("bot@elsewhere.com",), (), False),
        ((), (), False),
    ])
    def test_to_versus_cc(self, to, cc, expected):
        from istota.mail.ownership import bot_addressed_in_to

        config = Config()
        config.email = _email_config()
        assert bot_addressed_in_to(config, _email(to=to, cc=cc)) is expected

    def test_no_bot_address_configured(self):
        from istota.mail.ownership import bot_addressed_in_to

        assert bot_addressed_in_to(Config(), _email()) is False

    @pytest.mark.parametrize("to, cc, expected", [
        (("bot+carol@test.com",), (), True),
        (("dave@test.com",), ("bot+carol@test.com",), False),
    ])
    def test_the_poller_passes_it_to_ingest(self, mail_config, to, cc, expected):
        config = mail_config(_users("carol"))
        msg = _ingested(config, _email(id="adr", sender="carol@test.com", to=to, cc=cc))
        assert msg.addressed_to_bot is expected


# =============================================================================
# TestPollEmailsPlusAddressRouting
# =============================================================================


class TestPollEmailsPlusAddressRouting:
    """Tests for plus-address routing in the poll loop."""

    def test_plus_address_routes_unknown_sender(self, mail_config):
        """Unknown sender emailing bot+carol@ routes to carol, recorded as such."""
        config = mail_config(_users("carol"))

        task = _single_task(config, _email(
            id="10", subject="Hello agent", sender="stranger@external.com",
            body="Can you help me?", to=("bot+carol@test.com",),
        ))

        assert task.user_id == "carol"
        assert task.source_type == "email"
        assert "stranger@external.com" in task.prompt
        assert _processed(config, "10")["routing_method"] == "plus_address"

    def test_plus_address_takes_precedence_over_sender_match(self, mail_config):
        """If sender matches alice but To is bot+carol@, route to carol."""
        config = mail_config(_users("carol", "alice"))

        task = _single_task(config, _email(
            id="11", subject="For carol", sender="alice@test.com",
            to=("bot+carol@test.com",),
        ))

        assert task.user_id == "carol"  # plus-address wins over sender

    def test_invalid_plus_address_falls_through_to_sender(self, mail_config):
        """Plus-address with invalid user falls through to sender-based routing."""
        config = mail_config(_users("alice"))

        task = _single_task(config, _email(
            id="12", subject="Test", sender="alice@test.com",
            to=("bot+nonexistent@test.com",),
        ))

        assert task.user_id == "alice"  # fell through to sender match

    def test_routing_method_stored_for_sender_match(self, mail_config):
        config = mail_config(_users("alice"))

        _poll(config, _email(id="14", sender="alice@test.com"))

        assert _processed(config, "14")["routing_method"] == "sender_match"

    def test_routing_method_stored_for_thread_match(self, mail_config):
        config = mail_config(_users("carol"))
        _sent(config, "<out15@bot.com>")

        _poll(config, _email(id="15", subject="Re: Hello", sender="ext@x.com",
                             references="<out15@bot.com>"))

        assert _processed(config, "15")["routing_method"] == "thread_match"


class TestEmailConfirmationGate:
    """Tests for the confirmation gate on plus-addressed emails from untrusted senders."""

    def test_untrusted_sender_held_for_confirmation(self, mail_config):
        config = mail_config(_users("carol", alerts_channel="alerts_room"))

        task_ids, send = _poll_prompted(config, _email(
            id="20", sender="stranger@evil.com", to=("bot+carol@test.com",),
        ), result=(True, 77))

        assert len(task_ids) == 1
        task = _task(config, task_ids[0])
        assert task.status == "pending_confirmation"
        assert task.talk_response_id == 77
        send.assert_called_once()

    def test_trusted_sender_proceeds_immediately(self, mail_config):
        config = mail_config(_users("carol", trusted_email_senders=["*@trusted.com"]))

        task = _single_task(config, _email(
            id="21", sender="friend@trusted.com", to=("bot+carol@test.com",),
        ))
        assert task.status == "pending"

    def test_own_email_via_plus_address_not_gated(self, mail_config):
        config = mail_config(_users("carol"))

        task = _single_task(config, _email(
            id="22", sender="carol@test.com", to=("bot+carol@test.com",),
        ))
        assert task.status == "pending"

    def test_db_trusted_sender_proceeds_immediately(self, mail_config):
        """Sender trusted via DB (not config) should bypass the confirmation gate."""
        config = mail_config(_users("carol", trusted_email_senders=[]))
        with db.get_db(config.db_path) as conn:
            db.add_trusted_sender(conn, "carol", "friend@newcontact.com")

        task = _single_task(config, _email(
            id="db1", sender="friend@newcontact.com", to=("bot+carol@test.com",),
        ))
        assert task.status == "pending"  # Not pending_confirmation

    def test_sender_match_own_email_not_gated_by_default(self, mail_config):
        """With the gate at its default (off), sender-match mail is processed directly."""
        config = mail_config(_users("alice", alerts_channel="alerts_room"))
        assert config.email.confirm_sender_match == "off"

        task = _single_task(config, _email(id="23", sender="alice@test.com"))
        assert task.status == "pending"

    def test_sender_match_trusted_sender_not_gated(self, mail_config):
        """A trusted_email_senders pattern exempts an address even with the gate on."""
        config = mail_config(
            _users("alice", trusted_email_senders=["alice@test.com"]),
            confirm_sender_match="gate",
        )

        task = _single_task(config, _email(id="23c", sender="alice@test.com"))
        assert task.status == "pending"

    def test_gate_no_alerts_channel_still_holds(self, mail_config):
        config = mail_config(_users("carol"))  # No alerts_channel set

        task_ids, _ = _poll_prompted(config, _email(
            id="24", sender="stranger@evil.com", to=("bot+carol@test.com",),
        ), result=(False, None))

        assert len(task_ids) == 1
        task = _task(config, task_ids[0])
        assert task.status == "pending_confirmation"
        assert task.talk_response_id is None


class TestSenderMatchConfirmationGate:
    """ISSUE-227 — ``confirm_sender_match`` used to be unreachable: the route is
    *defined* by the sender being one of the user's own addresses, and the trust
    check it consulted returned True for exactly that set. The gate now asks about
    the own-address claim itself, and only an explicit exemption lets mail past."""

    def test_own_address_is_gated_when_enabled(self, mail_config):
        config = mail_config(
            _users("alice", alerts_channel="alerts_room"), confirm_sender_match="gate",
        )

        task_ids, send = _poll_prompted(
            config, _email(id="sm1", sender="alice@test.com"), result=(True, 99),
        )

        assert len(task_ids) == 1
        task = _task(config, task_ids[0])
        assert task.status == "pending_confirmation"
        assert task.talk_response_id == 99

        prompt = send.call_args.args[2]
        assert "alice@test.com" in prompt
        assert "sender_match" in prompt

    def test_gated_turn_is_not_mirrored_to_the_room(self, mail_config):
        """The mirror commits in the task's transaction, so a gated turn must not
        publish before the user has answered (same contract as the plus-address gate)."""
        config = mail_config(_users("alice"), confirm_sender_match="gate")

        msg = _ingested(config, _email(id="sm2", sender="alice@test.com"),
                        prompt=(False, None))

        assert msg.suppress_transcript_mirror is True

    def test_runtime_trusted_address_bypasses_the_gate(self, mail_config):
        """The 'yes trust' escape hatch: once the address is trusted at runtime the
        gate stops asking, so an operator who turns it on is not stuck confirming forever."""
        config = mail_config(_users("alice"), confirm_sender_match="gate")
        with db.get_db(config.db_path) as conn:
            db.add_trusted_sender(conn, "alice", "alice@test.com")

        task = _single_task(config, _email(id="sm3", sender="alice@test.com"))
        assert task.status == "pending"

    def test_own_address_claim_is_gated_on_the_plus_address_route_too(self, mail_config):
        """The plus-address is public — it is the From: on every mail the bot sends
        on the user's behalf — so a spoofer who knows the address the gate is about
        can also route around it. Same own-address claim, same answer, either route."""
        config = mail_config(_users("alice"), confirm_sender_match="gate")

        task_ids, _ = _poll_prompted(config, _email(
            id="sm4", sender="alice@test.com", to=("bot+alice@test.com",),
        ), result=(False, None))

        assert len(task_ids) == 1
        assert _task(config, task_ids[0]).status == "pending_confirmation"
        with db.get_db(config.db_path) as conn:
            assert db.get_email_for_task(conn, task_ids[0]).routing_method == "plus_address"

    def test_plus_address_self_mail_stays_ungated_with_the_gate_off(self, mail_config):
        """Default state: the own-address branch still answers for plus-address mail,
        so a user's own plus-addressed self-mail is processed as it always was."""
        config = mail_config(_users("alice"))

        task = _single_task(config, _email(
            id="sm4b", sender="alice@test.com", to=("bot+alice@test.com",),
        ))
        assert task.status == "pending"

    def test_external_plus_address_sender_keeps_the_trust_offer(self, mail_config):
        """A genuinely external sender is still offered 'yes trust' — trusting them
        is the intended way to stop being asked, and costs nothing this gate protects."""
        config = mail_config(_users("alice"), confirm_sender_match="gate")

        task_ids, send = _poll_prompted(config, _email(
            id="sm4c", sender="stranger@evil.com", to=("bot+alice@test.com",),
        ))

        assert len(task_ids) == 1
        prompt = send.call_args.args[2]
        assert "yes trust" in prompt
        assert "unknown sender" in prompt

    @staticmethod
    def _gate_prompt(config, *, uid="ob1", sender="stranger@evil.com"):
        """Poll one plus-addressed mail, return the gate prompt, lowercased."""
        _, send = _poll_prompted(config, _email(
            id=uid, sender=sender, to=("bot+alice@test.com",),
        ))
        return send.call_args.args[2].lower()

    def test_trust_offer_discloses_the_outbound_half(self, mail_config):
        """`yes trust` grants more than the question appears to ask about.

        One list, two meanings since the outbound approval gate shipped: under
        the `untrusted` policy the answer also stops holding mail *to* this
        address for approval. A user who does not want the outbound half has to
        know to answer plain `yes`, so the prompt has to say so — one of the
        three disclosures `outbound_policy`'s module docstring commits to, and
        the only one on a surface the user reads under time pressure.
        """
        config = mail_config(_users("alice"), outbound_approval_floor="untrusted")

        prompt = self._gate_prompt(config)
        assert "without waiting for your approval" in prompt
        # Specifically the *outbound* direction. A sentence about processing
        # incoming mail would pass a looser assertion while saying nothing new.
        assert "mail to this address" in prompt

    @pytest.mark.parametrize("policy", ["off", "all"])
    def test_no_outbound_promise_under_a_policy_that_ignores_the_trust_list(
        self, mail_config, policy,
    ):
        """`untrusted` is the only policy that consults the trust list.

        `off` holds nothing to begin with, and `all` clears only the user's own
        addresses — so under either, trusting a correspondent buys no outbound
        permission and promising one would be a lie told at the moment the user
        is deciding whether to trust. `off` is not hypothetical: making it
        reachable from the inventory is half of what this stage is for.
        """
        config = mail_config(_users("alice"), outbound_approval_floor=policy)

        prompt = self._gate_prompt(config, uid=f"ob-{policy}")
        # The inbound half of the offer is unaffected — only the promise about
        # outbound is withheld.
        assert "yes trust" in prompt
        assert "without waiting for your approval" not in prompt

    def test_self_claim_prompt_makes_no_trust_offer_to_disclose(self, mail_config):
        """The own-address branch offers a plain yes/no, so there is nothing to
        disclose — and adding the outbound sentence there would advertise a
        shortcut the prompt deliberately withholds from a self-claim."""
        config = mail_config(
            _users("alice"),
            confirm_sender_match="gate", outbound_approval_floor="untrusted",
        )

        prompt = self._gate_prompt(config, uid="ob2", sender="alice@test.com")
        assert "yes trust" not in prompt
        assert "without waiting for your approval" not in prompt

    def test_trusted_external_sender_is_unaffected_by_the_gate(self, mail_config):
        """The flag suppresses only the own-address branch, which cannot match an
        external sender — so their trust answer is arithmetically unchanged."""
        config = mail_config(
            _users("alice", trusted_email_senders=["*@partner.com"]),
            confirm_sender_match="gate",
        )

        task = _single_task(config, _email(
            id="sm4g", sender="bob@partner.com", to=("bot+alice@test.com",),
        ))
        assert task.status == "pending"

    def test_self_claim_prompt_omits_the_trust_offer(self, mail_config):
        """'yes trust' on a self-claim would exempt the user's own address from the
        gate — for the spoofer too. It must not be offered as one of three equal options."""
        config = mail_config(_users("alice"), confirm_sender_match="gate")

        _, send = _poll_prompted(config, _email(id="sm4d", sender="alice@test.com"),
                                 result=(True, 8))

        prompt = send.call_args.args[2]
        assert "yes trust" not in prompt
        assert "unverified sender" in prompt
        assert "Reply 'yes' to process, or 'no' to discard." in prompt

    def test_self_claim_is_judged_against_the_routed_user(self, mail_config):
        """An address held by two users routes to whoever the plus-address names.
        The trust offer must follow that user, not whoever `find_user_by_email`
        happens to return first — offering it would trust their own address."""
        config = mail_config({
            "bob": UserConfig(email_addresses=["shared@test.com"]),
            "alice": UserConfig(email_addresses=["shared@test.com"]),
        }, confirm_sender_match="gate")

        task_ids, send = _poll_prompted(config, _email(
            id="sm6", sender="shared@test.com", to=("bot+alice@test.com",),
        ), result=(True, 11))

        task = _task(config, task_ids[0])
        assert task.user_id == "alice"
        assert task.status == "pending_confirmation"
        assert "yes trust" not in send.call_args.args[2]

    @pytest.mark.parametrize("result, warns", [
        ((False, None), True),
        ((True, 5), False),
    ], ids=["undeliverable_prompt_warns", "delivered_prompt_does_not_warn"])
    def test_undeliverable_prompt_warns(self, caplog, mail_config, result, warns):
        """The task is parked and the email already marked processed, so a prompt
        nobody receives is silent mail loss. It must at least be logged."""
        config = mail_config(_users("alice"), confirm_sender_match="gate")

        with caplog.at_level("WARNING", logger="istota.transport.email.inbound"):
            _poll_prompted(config, _email(id="sm4e", sender="alice@test.com"), result=result)

        warned = [r for r in caplog.records if "could not be delivered" in r.getMessage()]
        assert bool(warned) is warns

    def test_confirm_sender_match_does_not_reach_an_emissary_reply(self, mail_config):
        """`confirm_sender_match` is about the own-address claim. Turning it on must
        not start holding a correspondent's reply, which makes no such claim.

        Named for the thread route being ungated *by design* until ISSUE-234, which
        narrowed it to the correspondent rather than removing it — this sender is
        the address the bot wrote to, so it stays ungated for the reason the name
        now gives. `TestThreadMatchConfirmationGate` covers the narrowing itself."""
        config = mail_config(_users("alice"), confirm_sender_match="gate")
        _sent(config, "<orig@test.com>", user_id="alice", to_addr="external@reply.com",
              conversation_token="room1")

        task = _single_task(config, _email(
            id="sm5", subject="Re: Hello", sender="external@reply.com",
            references="<orig@test.com>",
        ))
        assert task.status == "pending"


class TestThreadMatchConfirmationGate:
    """ISSUE-234 — a `Message-ID` the bot issued routes a reply *and* used to
    authorize it. Possession is disclosed to everyone Cc'd, everyone the thread is
    forwarded to, and every archive in the path, so the thread route now also asks
    who sent the mail: the envelope sender must be an address the bot actually
    wrote to on the matched thread, or the message meets the same gate the other
    two routes meet."""

    @staticmethod
    def _seed_thread(config, to_addr="external@reply.com"):
        _sent(config, "<orig@test.com>", user_id="alice", to_addr=to_addr,
              conversation_token="room1")

    @staticmethod
    def _reply(sender, id="tm1"):
        return _email(id=id, subject="Re: Hello", sender=sender,
                      message_id=f"<{id}@reply.com>", references="<orig@test.com>")

    def _status(self, config, sender):
        """Poll a reply from `sender` with the prompt stubbed; return the task status."""
        task_ids, _ = _poll_prompted(config, self._reply(sender), result=(True, 99))
        return _task(config, task_ids[0]).status

    def test_stranger_holding_the_message_id_is_gated(self, mail_config):
        """The reproduction from the entry: the only thing the attacker supplies is
        a References header, and before the fix that alone produced a running task."""
        config = mail_config(_users("alice", alerts_channel="alerts_room"))
        self._seed_thread(config)

        task_ids, send = _poll_prompted(
            config, self._reply("attacker@evil.example"), result=(True, 99),
        )

        assert len(task_ids) == 1
        task = _task(config, task_ids[0])
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

    def test_the_correspondent_we_wrote_to_is_not_gated(self, mail_config):
        """The legitimate emissary reply — the common case, and the reason the route
        was left ungated in the first place. It must stay quiet."""
        config = mail_config(_users("alice"), confirm_sender_match="gate")
        self._seed_thread(config)

        task_ids, _ = _poll_prompted(config, self._reply("external@reply.com"))

        assert len(task_ids) == 1
        assert _task(config, task_ids[0]).status == "pending"

    def test_the_match_is_by_address_not_by_domain(self, mail_config):
        """A colleague at the correspondent's domain is exactly the population a
        leaked Message-ID reaches first, so a domain match would wave through the
        most likely leak rather than catch it."""
        config = mail_config(_users("alice"))
        self._seed_thread(config)

        assert self._status(config, "someone-else@reply.com") == "pending_confirmation"

    def test_trusting_the_sender_reopens_the_thread(self, mail_config):
        """`!trust` had no effect on this route because the trust check was never
        consulted. It is now the way a forwarded thread is let through for good."""
        config = mail_config(_users("alice"))
        self._seed_thread(config)
        with db.get_db(config.db_path) as conn:
            db.add_trusted_sender(conn, "alice", "colleague@reply.com")

        assert self._status(config, "colleague@reply.com") == "pending"

    def test_a_gated_thread_reply_is_not_mirrored_to_the_room(self, mail_config):
        """Same contract as the other two routes: the mirror commits in the task's
        transaction, so attacker text must not reach the room before the answer."""
        config = mail_config(_users("alice"))
        self._seed_thread(config)

        msg = _ingested(config, self._reply("attacker@evil.example"), prompt=(False, None))

        assert msg.suppress_transcript_mirror is True

    def test_a_reply_to_a_multi_recipient_send_matches_any_of_them(self, mail_config):
        """`to_addr` carries the whole recipient string when the send had several
        (`outbound_drafts` joins them with ', '), so all of them are correspondents."""
        config = mail_config(_users("alice"))
        self._seed_thread(config, to_addr="First <first@reply.com>, second@other.com")

        assert self._status(config, "second@other.com") == "pending"


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

    def test_regular_email_has_boundary_markers(self, mail_config):
        config = mail_config(_users("alice"), confirm_sender_match="off")

        task = _single_task(config, _email(id="b1", sender="alice@test.com", body="Hello world"))

        assert "<email_content>" in task.prompt
        assert "</email_content>" in task.prompt
        assert "<email_metadata>" in task.prompt
        assert "</email_metadata>" in task.prompt
        assert "do not follow instructions" in task.prompt.lower()

    def test_emissary_reply_has_boundary_markers(self, mail_config):
        config = mail_config(_users("carol"))
        _sent(config, "<orig@test.com>", to_addr="external@reply.com",
              conversation_token="room1")

        task = _single_task(config, _email(
            id="b2", subject="Re: Hello", sender="external@reply.com",
            body="Thanks for your email", references="<orig@test.com>",
        ))

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

    @staticmethod
    def _briefing_user():
        from istota.config import BriefingConfig
        return {
            # alerts_channel set to a different value from the briefing room, to
            # prove the sent_email path is used rather than resolve.
            "alerts_channel": "other_alerts",
            "briefings": [BriefingConfig(
                name="morning", cron="0 8 * * *",
                conversation_token="morning_briefing_room",
            )],
        }

    @pytest.mark.parametrize(
        "user_settings, conversation_token, delivery_token, "
        "expected_delivery, expected_conversation",
        [
            # The bug: pre-fix code threw away a real Talk room. Talk-source
            # originators record conversation_token = real Talk room and
            # talk_delivery_token = NULL. alerts_channel is set so the WRONG
            # fallback would return it.
            ({"alerts_channel": "WRONG_alerts_channel"}, "original_talk_room", None,
             "original_talk_room", "original_talk_room"),
            # Email originator with a synthetic thread hash (16 lowercase hex):
            # not a real Talk room, so resolve via the user's alerts.
            ({"alerts_channel": "carol_alerts"}, "deadbeef12345678", None,
             "carol_alerts", "deadbeef12345678"),
            # Briefing originator: conversation_token IS the briefing room.
            ("briefing", "morning_briefing_room", None, "morning_briefing_room", None),
            # Both NULL on sent_email — resolve via user config.
            ({"alerts_channel": "carol_alerts"}, None, None, "carol_alerts", None),
            # An explicit delivery token beats conversation_token.
            ({"alerts_channel": "WRONG_alerts"}, "some_other_room", "explicit_delivery_room",
             "explicit_delivery_room", None),
            # No resolvable channel and a real-looking conversation_token: use it,
            # rather than returning None because resolve cannot help.
            ({}, "orig_room", None, "orig_room", None),
        ],
        ids=[
            "talk_originator", "email_originator_synthetic", "briefing_originator",
            "both_null_resolves", "explicit_delivery_wins", "no_user_channel_keeps_room",
        ],
    )
    def test_reply_delivery_token(
        self, mail_config, user_settings, conversation_token, delivery_token,
        expected_delivery, expected_conversation,
    ):
        if user_settings == "briefing":
            user_settings = self._briefing_user()
        config = mail_config(_users("carol", **user_settings))
        _sent(config, "<out@bot.com>", subject="Plan",
              conversation_token=conversation_token, talk_delivery_token=delivery_token)

        task = _single_task(config, _email(
            id="r1", subject="Re: Plan", sender="ext@x.com", body="reply body",
            references="<out@bot.com>",
        ))

        assert task.user_id == "carol"
        assert task.talk_delivery_token == expected_delivery
        if expected_conversation is not None:
            assert task.conversation_token == expected_conversation


# =============================================================================
# TestEmissaryRecordingShape
# =============================================================================


class TestEmissaryRecordingShape:
    """What gets written to sent_emails when each task type sends an email.

    The thread-match logic above only works if sent_emails rows record the
    right fields for each originator type. These tests pin that contract.
    """

    @staticmethod
    def _record_and_read(db_path, tmp_path, task, message_id, **fields):
        from istota.transport.email.outbound import _record_sent_email
        _record_sent_email(
            _scheduler_config(db_path, tmp_path), task,
            message_id=message_id, to_addr="ext@x.com", **fields,
        )
        with db.get_db(db_path) as conn:
            row = db.find_sent_email_by_message_id(conn, message_id)
        assert row is not None
        return row

    def test_record_sent_email_for_talk_source_task(self, db_path, tmp_path):
        """Talk-source task -> sent_emails.conversation_token = real Talk room."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Send email please", user_id="alice",
                source_type="talk", conversation_token="real_talk_room",
                # talk_delivery_token NULL: talk-source tasks rely on the
                # _talk_target_for_delivery fallback to conversation_token.
                talk_delivery_token=None,
            )
            task = db.get_task(conn, task_id)

        row = self._record_and_read(db_path, tmp_path, task, "<sent@bot.com>", subject="Hello")
        assert row.conversation_token == "real_talk_room"
        # The known-NULL talk_delivery_token is the data shape that the
        # the inbound fix has to handle correctly on the read side.
        assert row.talk_delivery_token is None
        assert row.user_id == "alice"

    def test_record_sent_email_for_email_source_task(self, db_path, tmp_path):
        """Email-source task -> sent_emails.talk_delivery_token populated."""
        synthetic = "abcdef0123456789"

        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="Reply to that", user_id="alice",
                source_type="email", conversation_token=synthetic,
                talk_delivery_token="alerts_channel_xyz",
            )
            task = db.get_task(conn, task_id)

        row = self._record_and_read(db_path, tmp_path, task, "<sent2@bot.com>", subject="Re: Plan")
        assert row.conversation_token == synthetic
        assert row.talk_delivery_token == "alerts_channel_xyz"

    def test_record_sent_email_for_subtask_inherits_parent_tokens(
        self, db_path, tmp_path,
    ):
        """Subtask sending email -> sent_emails carries parent's tokens."""
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

        row = self._record_and_read(db_path, tmp_path, sub, "<sub@bot.com>")
        assert row.conversation_token == "parent_talk_room"
        assert row.talk_delivery_token == "parent_talk_room"


# =============================================================================
# TestEmissaryLifecycle — end-to-end outbound -> inbound
# =============================================================================


class TestEmissaryLifecycle:
    """Round-trip tests: a task sends an email; the reply comes in and routes."""

    @staticmethod
    def _round_trip(db_path, tmp_path, mail_config, task, message_id):
        """Record `task`'s send of `message_id`, poll the reply, return its task."""
        from istota.transport.email.outbound import _record_sent_email

        users = _users("alice", alerts_channel="alerts_room")
        _record_sent_email(
            _scheduler_config(db_path, tmp_path, users), task,
            message_id=f"<{message_id}>", to_addr="ext@x.com", subject="Plan",
        )
        return _single_task(mail_config(users), _email(
            id="lc1", subject="Re: Plan", sender="ext@x.com", body="The reply",
            references=f"<{message_id}>",
        ))

    def test_talk_task_sends_email_reply_routes_to_original_room(
        self, db_path, tmp_path, mail_config,
    ):
        """Full loop: talk task sends, external replies, routes to original room."""
        with db.get_db(db_path) as conn:
            tid = db.create_task(
                conn, prompt="send email", user_id="alice",
                source_type="talk", conversation_token="talkroom_42",
            )
            task = db.get_task(conn, tid)

        new_task = self._round_trip(db_path, tmp_path, mail_config, task, "m_talk@bot.com")

        assert new_task.user_id == "alice"
        assert new_task.conversation_token == "talkroom_42"
        # The reply routes back to the origin Talk room via the stored origin
        # descriptor (talk:<token>) rather than the talk_delivery_token ladder.
        assert new_task.output_target == "talk:talkroom_42,email"

    def test_email_task_sends_email_reply_routes_via_alerts(
        self, db_path, tmp_path, mail_config,
    ):
        """Email-source originator: reply routes via the recorded delivery token."""
        synthetic = "0123456789abcdef"

        with db.get_db(db_path) as conn:
            tid = db.create_task(
                conn, prompt="reply", user_id="alice",
                source_type="email", conversation_token=synthetic,
                talk_delivery_token="alerts_room",
            )
            task = db.get_task(conn, tid)

        new_task = self._round_trip(db_path, tmp_path, mail_config, task, "m_email@bot.com")

        assert new_task.talk_delivery_token == "alerts_room"
        # conversation_token still preserves the original synthetic email-thread key
        assert new_task.conversation_token == synthetic

    def test_subtask_sends_email_reply_routes_to_parent_room(
        self, db_path, tmp_path, mail_config,
    ):
        """Subtask of a talk task sends an email — reply must reach parent's room."""
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

        new_task = self._round_trip(db_path, tmp_path, mail_config, sub, "m_sub@bot.com")

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

    @pytest.mark.parametrize("header, expected", [
        ("mx.test; spf=pass; dmarc=pass header.from=t.com", "pass"),
        ("mx.test; dmarc=fail header.from=t.com", "fail"),
        # `dmarc=none` means the domain publishes no policy — the "DMARC record
        # was edited away" drift case, not a missing evaluation.
        ("mx.test; dmarc=none header.from=t.com", "none"),
        ("mx.test; dmarc=temperror", "temperror"),
        ("mx.test;   DMARC = PASS header.from=t.com", "pass"),
        ("mx.test; spf=pass smtp.mailfrom=t.com; dkim=pass", None),
        (None, None),
        ("", None),
        # `header.dmarc=pass` is a property of another method, not a dmarc result.
        ("mx.test; spf=fail header.dmarc=pass", None),
        ("mx.test; spf=fail (dmarc=pass per sender)", "malformed"),
        # Unbalanced comment: dropping it would drop the real verdict.
        ("mx.test; spf=fail (note: evil; dmarc=fail header.from=victim.com", "malformed"),
        ('mx; spf=fail smtp.mailfrom="a\\"; dmarc=fail; x="; dmarc=pass', "malformed"),
        # A pass read out of a header we could not finish reading is not a pass.
        ('mx.test; dmarc=pass; spf=fail smtp.mailfrom="oops', "malformed"),
        # A verdict actually read beats the generic "could not read it all".
        ('mx.test; dmarc=fail; spf=fail smtp.mailfrom="oops', "fail"),
    ], ids=[
        "pass", "fail", "none_is_a_result", "temperror", "case_and_whitespace",
        "no_dmarc_methodspec", "absent_header", "empty_header",
        "property_named_dmarc", "parenthesized_comment", "unbalanced_comment",
        "escaped_quote", "malformed_never_pass", "explicit_fail_outranks_malformed",
    ])
    def test_result(self, header, expected):
        assert _dmarc_result(header) == expected

    def test_reason_string_containing_dmarc_is_never_read_as_a_verdict(self):
        """A quoted reason is free text the reporting MTA may echo from the message.
        It is not a verdict — and because the parser cannot tell an MTA that quoted
        the word from a sender who planted it, it refuses to call the header clean
        rather than answering the quiet "no verdict"."""
        assert _dmarc_result('mx.test; spf=fail reason="dmarc=pass claimed"') == "malformed"

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

    @staticmethod
    def _parse(second_stamp, extra=b""):
        from imap_tools import MailMessage

        from istota.skills.email import _msg_to_email

        raw = (
            b"Authentication-Results: mx.test; dmarc=fail header.from=test.com\r\n"
            b"Authentication-Results: " + second_stamp + b"\r\n"
            b"From: alice@test.com\r\n"
            b"Subject: Hi\r\n" + extra +
            b"\r\n"
            b"body\r\n"
        )
        return _msg_to_email(MailMessage.from_bytes(raw))

    def test_to_full_email_carries_the_topmost_header(self):
        email = self._parse(b"forged.example; dmarc=pass header.from=test.com",
                            extra=b"Message-ID: <m1@test.com>\r\n")

        assert email.authentication_results is not None
        assert email.authentication_results.startswith("mx.test")
        assert _dmarc_result(email.authentication_results) == "fail"

    def test_a_forged_pass_below_a_genuine_fail_does_not_win(self):
        """The whole point: a spoofer appending their own ``dmarc=pass`` must not
        silence the canary that the real MTA's ``dmarc=fail`` should trip."""
        email = self._parse(b"mx.test; dmarc=pass header.from=test.com")

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

    _FAIL = "mx.test; dmarc=fail header.from=test.com"

    def test_dmarc_pass_is_silent(self, alice_config, caplog):
        with caplog.at_level("WARNING"):
            task_ids, alert = _poll_alerted(alice_config(), _self_claim(
                "d1", header="mx.test; dmarc=pass header.from=test.com"))

        assert len(task_ids) == 1
        assert alert.call_count == 0
        assert "dmarc" not in caplog.text.lower()

    def test_dmarc_fail_warns_and_alerts(self, alice_config, caplog):
        with caplog.at_level("WARNING"):
            _, alert = _poll_alerted(alice_config(), _self_claim("d2", header=self._FAIL))

        assert alert.call_count == 1
        assert alert.call_args.kwargs["purpose"] == "alert"
        assert "alice@test.com" in alert.call_args.args[2]
        assert "alice@test.com" in caplog.text
        assert "dmarc=fail" in caplog.text.lower()

    def test_dmarc_none_warns(self, alice_config):
        """``dmarc=none`` is the "DMARC record was edited away" drift case — the
        exact silent degradation this canary exists to catch."""
        _, alert = _poll_alerted(alice_config(), _self_claim(
            "d3", header="mx.test; dmarc=none header.from=test.com"))

        assert alert.call_count == 1

    def test_missing_header_is_silent_by_default(self, alice_config, caplog):
        """A mail path that stamps nothing would otherwise warn on every message."""
        with caplog.at_level("WARNING"):
            _, alert = _poll_alerted(alice_config(), _self_claim("d4"))

        assert alert.call_count == 0
        assert "dmarc" not in caplog.text.lower()

    def test_missing_header_warns_when_opted_in(self, alice_config, caplog):
        """The 'mailbox moved to a provider that does not stamp' drift case is only
        reachable for an operator who knows their MTA is supposed to stamp."""
        config = alice_config(dmarc_canary_warn_on_missing=True)

        with caplog.at_level("WARNING"):
            _, alert = _poll_alerted(config, _self_claim("d5"))

        assert alert.call_count == 1
        assert "no dmarc result" in caplog.text.lower()

    def test_header_without_a_dmarc_methodspec_follows_the_missing_rule(self, alice_config):
        """A header that authenticated SPF but never evaluated DMARC is absence of
        evidence, not evidence of failure — same class as no header at all."""
        _, alert = _poll_alerted(alice_config(), _self_claim(
            "d6", header="mx.test; spf=pass smtp.mailfrom=test.com"))

        assert alert.call_count == 0

    def test_plus_address_route_with_a_self_claim_is_watched_too(self, alice_config, caplog):
        """ISSUE-227 found that scoping this to ``sender_match`` is bypassable: the
        plus-address is public, so ``From: <user>`` + ``To: bot+<user>@…`` carries the
        identical own-address claim on a route a sender-match-only check never sees.
        The gate collapsed both routes; the canary that watches the gate's assumption
        has to cover the same set or it has the same hole."""
        email = _email(id="d7", sender="alice@test.com", to=("bot+alice@test.com",),
                       authentication_results=self._FAIL)

        with caplog.at_level("WARNING"):
            task_ids, alert = _poll_alerted(alice_config(), email)

        assert len(task_ids) == 1
        assert alert.call_count == 1
        assert "plus_address" in caplog.text

    def test_external_sender_is_not_watched(self, alice_config):
        """The canary guards one assumption: that a ``From:`` naming the user's own
        address proves the user sent it. Mail from a genuinely external sender never
        leans on that claim — it is gated or trusted on its own terms."""
        config = alice_config()
        config.users["alice"].trusted_email_senders = ["carol@vendor.com"]
        email = _email(id="d8", sender="carol@vendor.com", to=("bot+alice@test.com",),
                       authentication_results="mx.test; dmarc=fail header.from=vendor.com")

        _, alert = _poll_alerted(config, email)

        assert alert.call_count == 0

    def test_disabled_canary_is_silent_on_an_outright_fail(self, alice_config, caplog):
        with caplog.at_level("WARNING"):
            _, alert = _poll_alerted(alice_config(dmarc_canary=False),
                                     _self_claim("d9", header=self._FAIL))

        assert alert.call_count == 0
        assert "dmarc" not in caplog.text.lower()

    def test_the_canary_never_blocks_the_mail(self, alice_config):
        """It is a detector, not a gate. A failing check must not change what happens
        to the message — that call belongs to ``confirm_sender_match``."""
        config = alice_config()

        task_ids, _ = _poll_alerted(config, _self_claim("d10", header=self._FAIL))

        assert len(task_ids) == 1
        assert _task(config, task_ids[0]).status == "pending"

    def test_alert_is_deduped_but_the_log_is_not(self, alice_config, caplog):
        """A persistently broken mail path must not flood the alert channel, but the
        log has to keep a per-message record."""
        emails = [_self_claim(f"d11-{i}", header=self._FAIL) for i in range(3)]

        with caplog.at_level("WARNING"):
            task_ids, alert = _poll_alerted(alice_config(), *emails)

        assert len(task_ids) == 3
        assert alert.call_count == 1
        assert caplog.text.lower().count("dmarc=fail") == 3

    def test_a_different_verdict_alerts_again(self, alice_config):
        """Dedup is per verdict, so a path degrading from ``fail`` to ``none`` is not
        swallowed by the earlier alert."""
        _, alert = _poll_alerted(
            alice_config(),
            _self_claim("d12-a", header="mx.test; dmarc=fail"),
            _self_claim("d12-b", header="mx.test; dmarc=none"),
        )

        assert alert.call_count == 2

    def test_a_failing_alert_does_not_break_the_poll(self, alice_config):
        """The canary is best-effort monitoring; an unreachable alert surface must
        not cost the user their mail."""
        config = alice_config()

        with (
            _inbox(_self_claim("d13", header="mx.test; dmarc=fail")),
            patch(_ALERT, side_effect=RuntimeError("talk down")),
        ):
            task_ids = poll_emails(config)

        assert len(task_ids) == 1

    def test_an_unreadable_header_alerts_under_the_default_config(self, alice_config, caplog):
        """The end-to-end half of the parser's `malformed` rule. Checking only
        `_dmarc_result`'s return value leaves the behaviour that matters untested —
        that an unreadable header alerts rather than falling into the silent
        no-verdict class, which is what would let a planted delimiter silence it."""
        with caplog.at_level("WARNING"):
            _, alert = _poll_alerted(alice_config(), _self_claim(
                "d17", header='mx.test; spf=fail smtp.mailfrom="oops'))

        assert alert.call_count == 1
        assert "unreadable" in caplog.text.lower()

    def test_quiet_sender_mail_still_reports_on_the_mail_path(self, alice_config, caplog):
        """A quiet sender's mail is filed with no task, by a branch that skips to the
        next message before the gate. The canary sits above it deliberately: quieting
        a sender says nothing about whether the mail path still authenticates From:,
        and that branch skipping first would blind the canary for every quieted
        self-address."""
        config = alice_config()
        config.users["alice"].quiet_email_senders = ["alice@test.com"]

        with caplog.at_level("WARNING"):
            task_ids, alert = _poll_alerted(config, _self_claim("d14", header=self._FAIL))

        assert task_ids == []
        assert alert.call_count == 1
        assert "dmarc=fail" in caplog.text.lower()

    def test_a_failed_delivery_does_not_consume_the_dedup_window(self, alice_config):
        """The window opens on a delivered alert, not a decided one. `send_notification`
        reports "no destination configured" by returning False rather than raising, and
        stamping the dedup at decision time would swallow the next 24 hours of alerts
        after a single silent failure."""
        config = alice_config()

        def _poll_once(email_id, delivered):
            email = _self_claim(email_id, header="mx.test; dmarc=fail")
            return _poll_alerted(config, email, delivered=delivered)[1]

        assert _poll_once("d15-a", False).call_count == 1
        # Undelivered, so the next occurrence must try again rather than be throttled.
        assert _poll_once("d15-b", True).call_count == 1
        # Delivered now, so this one is throttled.
        assert _poll_once("d15-c", True).call_count == 0

    def test_the_alert_is_sent_after_the_poll_transaction_closes(self, alice_config):
        """`poll_emails` holds one write transaction across the whole envelope loop, and
        an alert can route to a surface that writes to the same DB — the web surface
        does. Sending in-loop makes that second connection block on the poller's own
        lock until the busy timeout, stalling the scheduler's dispatch loop."""
        config = alice_config()
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
        emails = [
            _self_claim("d16-a", header="mx.test; dmarc=fail"),
            _self_claim("d16-b", header="mx.test; dmarc=fail", sender="alice2@test.com"),
        ]

        with _inbox(*emails), patch(_ALERT, side_effect=_fake_send):
            task_ids = poll_emails(config)

        assert len(task_ids) == 2
        assert len(observed) == 2


# =============================================================================
# TestAuthservIdScoping (ISSUE-249)
# =============================================================================


class TestAuthservIdScoping:
    """ISSUE-249 Gap 1 — "topmost" is only a proxy for "ours", and it inverts in
    exactly the case the canary exists to catch. While the MTA stamps, element 0
    is its stamp. The moment it stops, element 0 is whatever the sender wrote, so
    a forged ``dmarc=pass`` reads as a healthy path and the one config change that
    would have made the drift visible is defeated by the drift itself.

    ``[email] authserv_id`` names the receiving host's own identity — the first
    field of the RFC 8601 header — so our stamp can be told from one the sender
    wrote.
    """

    @staticmethod
    def _poll(config, headers, id="a1"):
        return _poll_alerted(config, _self_claim(id, headers=headers))[1]

    def test_a_forged_stamp_no_longer_reads_as_a_healthy_path(self, alice_config, caplog):
        """The whole point of the issue. The MTA has stopped stamping, so the only
        Authentication-Results on the message is the sender's own, claiming a pass.
        Unscoped, that is silent — element 0 is taken as ours."""
        config = alice_config(authserv_id="mx.test")

        with caplog.at_level("WARNING"):
            alert = self._poll(config, ["forged.example; dmarc=pass header.from=test.com"])

        assert alert.call_count == 1
        assert "mx.test" in caplog.text

    def test_no_header_at_all_is_loud_once_an_authserv_id_is_configured(self, alice_config):
        """Configuring the id *is* the operator's statement that their MTA stamps,
        so a message with no stamp of ours contradicts it. This does not wait for
        `dmarc_canary_warn_on_missing` — that flag exists for the unscoped case,
        where absence only means "this path does not stamp"."""
        config = alice_config(authserv_id="mx.test")

        alert = self._poll(config, [])

        assert alert.call_count == 1
        assert config.email.dmarc_canary_warn_on_missing is False

    @pytest.mark.parametrize("headers", [
        # A sender who supplies their own header cannot stop the MTA prepending
        # its stamp above it. The verdict read is ours either way.
        (["forged.example; dmarc=pass header.from=test.com",
          "mx.test; dmarc=fail header.from=test.com"]),
        # Nothing stops an MTA emitting one header per method, so several stamps
        # carrying our id is a legitimate shape. Non-pass-wins has to hold across
        # them, not only within one.
        (["mx.test; dmarc=pass header.from=test.com",
          "mx.test; dmarc=fail header.from=test.com"]),
    ], ids=["our_stamp_below_a_forged_one", "a_second_stamp_of_ours"])
    def test_our_failing_stamp_is_read(self, alice_config, caplog, headers):
        config = alice_config(authserv_id="mx.test")

        with caplog.at_level("WARNING"):
            alert = self._poll(config, headers)

        assert alert.call_count == 1
        assert "dmarc=fail" in caplog.text.lower()

    @pytest.mark.parametrize("authserv_id, headers", [
        # Headers that are not ours are discarded, not merged: a sender cannot make
        # the canary noisy by planting a `dmarc=fail` under someone else's
        # authserv-id any more than they can make it quiet with a `dmarc=pass`.
        ("mx.test", ["mx.test; dmarc=pass header.from=test.com",
                     "forged.example; dmarc=fail header.from=test.com"]),
        ("MX.Test.", ["mx.test; dmarc=pass header.from=test.com"]),
        # RFC 8601 allows a version number between the authserv-id and the first
        # methodspec: `Authentication-Results: mx.test 1; dmarc=pass`.
        ("mx.test", ["mx.test 1; dmarc=pass header.from=test.com"]),
        # The default, and it governs header *selection* only — the alignment
        # check runs either way, so this is deliberately not named "nothing
        # changes". The silent case here is the one the feature exists to close,
        # which is why the docs push operators to set an id.
        ("", ["forged.example; dmarc=pass header.from=test.com",
              "mx.test; dmarc=fail header.from=test.com"]),
    ], ids=[
        "a_forged_fail_beside_our_pass_cannot_add_a_verdict",
        "the_match_ignores_case_and_a_trailing_dot",
        "an_rfc_8601_version_after_the_id_still_matches",
        "blank_authserv_id_still_selects_the_topmost_header_only",
    ])
    def test_silent(self, alice_config, authserv_id, headers):
        assert self._poll(alice_config(authserv_id=authserv_id), headers).call_count == 0

    def test_a_matching_stamp_with_no_dmarc_verdict_follows_warn_on_missing(self, alice_config):
        """Distinct from "no stamp of ours": our MTA did stamp, it just did not
        evaluate DMARC. That is the pre-existing absence-of-evidence class and it
        keeps its existing knob."""
        config = alice_config(authserv_id="mx.test")

        assert self._poll(config, ["mx.test; spf=pass smtp.mailfrom=test.com"]).call_count == 0

        loud = alice_config(authserv_id="mx.test", dmarc_canary_warn_on_missing=True)
        assert self._poll(loud, ["mx.test; spf=pass smtp.mailfrom=test.com"],
                          id="a2").call_count == 1

    def test_an_unreadable_stamp_of_ours_outranks_a_readable_failure(self, alice_config, caplog):
        """`malformed` is the least trustworthy state, so it decides the wording
        and the dedup bucket rather than taking its turn in wire order. Both are
        loud either way; what would be wrong is reporting a specific verdict while
        another of our own stamps was unreadable."""
        config = alice_config(authserv_id="mx.test")

        with caplog.at_level("WARNING"):
            alert = self._poll(config, [
                "mx.test; dmarc=fail header.from=test.com",
                'mx.test; dmarc="x',
            ])

        assert alert.call_count == 1
        assert "unreadable" in caplog.text.lower()

    def test_the_rejected_headers_are_counted_never_quoted(self, alice_config, caplog):
        """In the unstamped branch every header present is one we rejected, so its
        content is whatever the sender wrote. It must not reach the log or the
        operator alert."""
        config = alice_config(authserv_id="mx.test")

        with caplog.at_level("WARNING"):
            alert = self._poll(config, [
                "forged.example; dmarc=pass header.from=attacker-controlled.invalid",
            ])

        assert alert.call_count == 1
        assert "forged.example" not in caplog.text
        assert "attacker-controlled.invalid" not in caplog.text
        assert "forged.example" not in alert.call_args.args[2]
        assert "attacker-controlled.invalid" not in alert.call_args.args[2]
        assert "1 present" in caplog.text

    def test_a_quoted_authserv_id_is_not_taken_as_ours(self, alice_config):
        """`_split_methodspecs` blanks quoted strings, so a quoted authserv-id reads
        as empty and matches nothing. Deliberately the loud direction: the parser's
        standing rule is that an ambiguous read resolves to the warning, never to
        the silence."""
        config = alice_config(authserv_id="mx.test")

        alert = self._poll(config, ['"mx.test"; dmarc=pass header.from=test.com'])

        assert alert.call_count == 1

    def test_the_canary_switch_still_turns_the_whole_thing_off(self, alice_config):
        config = alice_config(authserv_id="mx.test", dmarc_canary=False)

        assert self._poll(config, ["forged.example; dmarc=pass"]).call_count == 0


# =============================================================================
# TestDmarcAlignment (ISSUE-249)
# =============================================================================


class TestDmarcAlignment:
    """A `dmarc=pass` says the MTA authenticated *some* address. Checking its
    `header.from` against the `From:` we actually routed on is what makes it a
    statement about this sender rather than one we assumed was about this sender.
    """

    @staticmethod
    def _poll(config, header, id="g1", sender="alice@test.com"):
        return _poll_alerted(config, _self_claim(id, header=header, sender=sender))[1]

    @pytest.mark.parametrize("header, logged", [
        ("mx.test; dmarc=pass header.from=vendor.example", "vendor.example"),
        # The alignment check only refines a pass. A non-pass is already the more
        # specific answer and must not be relabelled.
        ("mx.test; dmarc=fail header.from=vendor.example", "dmarc=fail"),
        # First-match-wins is the rule `_dmarc_result` was fixed to stop using,
        # and it must not come back one property down: a sender who appends a
        # second `header.from` naming the right domain would otherwise silence the
        # wrong one ahead of it. Every property on every stamp we read is checked.
        ("mx.test; dmarc=pass header.from=test.com; dmarc=pass header.from=evil.example",
         "evil.example"),
        # Absent and unreadable are different answers. `_split_methodspecs` blanks
        # a quoted value, and resolving that to silence would hand a sender a
        # one-token way to turn the alignment check off.
        ('mx.test; dmarc=pass header.from="evil.example"', "could not read"),
        # `header.from=` truncated to nothing by the next methodspec's semicolon
        # is the same unreadable class, and reads as absent under a `+` capture.
        ("mx.test; dmarc=pass header.from=; x=evil.example", None),
        # Normalising a trailing dot must not turn a present property into an
        # absent one.
        ("mx.test; dmarc=pass header.from=.", None),
        # The relaxation is a label-boundary relationship, not a suffix match —
        # `nottest.com` must not pass as `test.com`.
        ("mx.test; dmarc=pass header.from=nottest.com", None),
    ], ids=[
        "a_pass_about_a_different_domain_warns",
        "a_fail_is_still_reported_as_a_fail",
        "an_appended_aligned_property_cannot_mask_a_misaligned_one",
        "a_property_present_but_unreadable_warns",
        "an_empty_property_value_warns",
        "a_bare_root_dot_warns",
        "a_lookalike_domain_is_not_aligned",
    ])
    def test_warns(self, alice_config, caplog, header, logged):
        with caplog.at_level("WARNING"):
            alert = self._poll(alice_config(), header)

        assert alert.call_count == 1
        if logged in ("dmarc=fail", "could not read"):
            assert logged in caplog.text.lower()
        elif logged is not None:
            assert logged in caplog.text

    @pytest.mark.parametrize("header", [
        "mx.test; dmarc=pass header.from=test.com",
        "mx.test; dmarc=pass header.from=Alice@TEST.com.",
        # Not every MTA emits the property. Absence means we cannot check, which
        # is not the same as a mismatch — warning here would fire on every message
        # from such a path and train the operator to ignore the canary.
        "mx.test; dmarc=pass",
    ], ids=[
        "an_aligned_pass_stays_silent",
        "alignment_ignores_case_a_local_part_and_a_trailing_dot",
        "a_pass_with_no_header_from_property_is_silent",
    ])
    def test_silent(self, alice_config, header):
        assert self._poll(alice_config(), header).call_count == 0

    def test_a_subdomain_of_the_from_domain_counts_as_aligned(self, alice_config):
        """DMARC's own relaxed mode aligns on the organizational domain, and an MTA
        may record the domain it evaluated rather than the literal From: domain.
        Warning on every message from such a path is the noise that trains an
        operator to ignore the canary."""
        config = alice_config()
        # The canary only runs on a self-claim, so the subdomain sender has to be
        # one of this user's own addresses — otherwise the route is `discarded`
        # and the silence proves nothing about alignment.
        config.users["alice"].email_addresses = ["alice@test.com", "alice@mail.test.com"]

        assert self._poll(config, "mx.test; dmarc=pass header.from=mail.test.com",
                          id="g10").call_count == 0
        assert self._poll(config, "mx.test; dmarc=pass header.from=test.com",
                          id="g11", sender="alice@mail.test.com").call_count == 0


# =============================================================================
# TestDkimSpfDetail (ISSUE-249)
# =============================================================================


class TestDkimSpfDetail:
    """`dkim=` and `spf=` never change the verdict — they say *how* a failing path
    is failing, which is what a partial misconfiguration looks like."""

    def test_the_log_and_the_alert_name_the_dkim_and_spf_verdicts(self, alice_config, caplog):
        email = _self_claim("k1", header=(
            "mx.test; dmarc=fail header.from=test.com; "
            "dkim=pass header.d=test.com; spf=softfail smtp.mailfrom=test.com"
        ))

        with caplog.at_level("WARNING"):
            _, alert = _poll_alerted(alice_config(), email)

        assert alert.call_count == 1
        assert "dkim=pass" in caplog.text
        assert "spf=softfail" in caplog.text
        assert "dkim=pass" in alert.call_args.args[2]

    def test_a_failing_dkim_beside_an_aligned_pass_stays_silent(self, alice_config):
        """The rule this class exists to state: DMARC is the verdict, and dkim/spf
        are detail. Without this the class asserts detail text on a message that
        was already failing, which would still pass if dkim promoted a pass."""
        email = _self_claim("k2", header=(
            "mx.test; dmarc=pass header.from=test.com; "
            "dkim=fail header.d=test.com; spf=fail smtp.mailfrom=test.com"
        ))

        _, alert = _poll_alerted(alice_config(), email)

        assert alert.call_count == 0

    def test_an_oversized_method_token_is_truncated(self, alice_config, caplog):
        """`[a-z]+` bounds the alphabet these tokens are drawn from, not their
        length, and in the unscoped case the whole header is sender-written. The
        WARNING is emitted per message and never deduped, so an unbounded value
        here is an unbounded write to the log and to the alert body."""
        email = _self_claim("k3", header=(
            "mx.test; dmarc=fail header.from=test.com; "
            "dkim=" + "a" * 5000
        ))

        with caplog.at_level("WARNING"):
            _, alert = _poll_alerted(alice_config(), email)

        assert alert.call_count == 1
        assert "a" * 200 not in caplog.text
        assert "a" * 200 not in alert.call_args.args[2]


# =============================================================================
# TestVerifyPolicy (ISSUE-249 Gap 3)
# =============================================================================


def _verify_config(alice_config, policy="verify", **email_settings):
    return alice_config(authserv_id="mx.test", confirm_sender_match=policy, **email_settings)


def _poll_verify(config, header, id="v1"):
    """Returns (task_ids, confirmation-prompt spy, alert spy). A held message
    still creates a task — it is parked awaiting an answer, not dropped — so the
    prompt is what distinguishes gated from ungated, never the task count."""
    return _run_poll(
        config, _self_claim(id, headers=[header] if header else []),
        prompt=(True, 99), alert=True,
    )


class TestVerifyPolicy:
    """ISSUE-249 Gap 3 — the verdict finally decides something.

    Before this, the gate consulted `is_trusted_email_sender` and nothing else,
    so a self-claim carrying a verified aligned pass and one carrying a header
    the sender wrote got the identical answer whichever way the flag was set. The
    canary computed the answer and threw it away.

    `verify` is what makes the gate usable: `gate` asks about every self-sent
    message because nothing in a plain SMTP message separates the user from
    someone claiming to be them, and this is the signal that does.
    """

    def test_a_verified_aligned_pass_is_let_through(self, alice_config):
        """The point of the setting. This is the message `gate` would have held
        and interrupted the user about, for no gain."""
        task_ids, prompt, _ = _poll_verify(
            _verify_config(alice_config), "mx.test; dmarc=pass header.from=test.com")

        assert len(task_ids) == 1
        assert prompt.call_count == 0

    @pytest.mark.parametrize("policy, header, held", [
        ("verify", "mx.test; dmarc=fail header.from=test.com", 1),
        # The forgery case, and the reason `verify` refuses to run without an
        # authserv-id: unscoped this header would have been read as a pass.
        ("verify", "forged.example; dmarc=pass header.from=test.com", 1),
        # A pass about a different address is not a statement about this sender,
        # which is exactly what the gate is asking about.
        ("verify", "mx.test; dmarc=pass header.from=vendor.example", 1),
        # Fail closed. Holding costs one confirmation and the mail is still
        # there; the other direction runs an unauthenticated message on the
        # strength of a check that never happened.
        ("verify", None, 1),
        # Unchanged behaviour for every deployment that has not opted in: the
        # header is proof, and a failing verdict warns without holding anything.
        ("off", "mx.test; dmarc=fail header.from=test.com", 0),
        # Also unchanged: `gate` holds a self-claim however well it
        # authenticates. That is the noisiness `verify` exists to fix, not a bug.
        ("gate", "mx.test; dmarc=pass header.from=test.com", 1),
    ], ids=[
        "a_failing_verdict_is_held",
        "a_pass_from_someone_elses_authserv_id_is_held",
        "a_misaligned_pass_is_held",
        "no_verdict_at_all_is_held",
        "off_ignores_the_verdict_entirely",
        "gate_ignores_the_verdict_entirely",
    ])
    def test_hold(self, alice_config, policy, header, held):
        _, prompt, _ = _poll_verify(_verify_config(alice_config, policy), header)

        assert prompt.call_count == held

    def test_verify_works_with_the_canary_switched_off(self, alice_config):
        """`dmarc_canary` governs the warnings. An operator who does not want the
        log noise has not thereby said unauthenticated mail should run, so the
        verdict has to be computed independently of that switch."""
        config = _verify_config(alice_config, dmarc_canary=False)

        _, held, _ = _poll_verify(config, "mx.test; dmarc=fail header.from=test.com")
        assert held.call_count == 1

        _, passed, _ = _poll_verify(config, "mx.test; dmarc=pass header.from=test.com",
                                    id="v7")
        assert passed.call_count == 0

    def test_an_explicitly_trusted_sender_still_bypasses_verify(self, alice_config):
        """`verify` narrows what the own-address claim buys. It does not withdraw
        a grant the operator made out of band."""
        config = _verify_config(alice_config)
        config.users["alice"].trusted_email_senders = ["alice@test.com"]

        _, prompt, _ = _poll_verify(config, "mx.test; dmarc=fail header.from=test.com")

        assert prompt.call_count == 0

    def test_verify_without_an_authserv_id_holds_a_forged_pass(self, alice_config):
        """`_validate_confirm_sender_match` refuses this combination at load, so
        this is the defence behind it: the guarantee must not rest on nothing ever
        setting the policy outside `load_config`. Unscoped, the verdict is read off
        the topmost header, so a sender writing `dmarc=pass` would otherwise walk
        straight through the gate."""
        config = _verify_config(alice_config)
        config.email.authserv_id = ""

        _, prompt, _ = _poll_verify(
            config, "attacker.example; dmarc=pass header.from=test.com")

        assert prompt.call_count == 1

    def test_the_policy_reader_normalises_what_the_loader_would_have(self, alice_config):
        """Anything building an `EmailConfig` directly bypasses the validator. A
        stray `False` meaning `off` that silently became `gate` would hold every
        self-sent message and expire it into cancellation — safe in the security
        direction, mail loss in the availability one."""
        config = _verify_config(alice_config)
        header = "mx.test; dmarc=fail header.from=test.com"

        for value, expect_held in (
            (False, 0), ("Off", 0), (" off ", 0),
            (True, 1), ("GATE", 1),
        ):
            config.email.confirm_sender_match = value
            _, prompt, _ = _poll_verify(config, header, id=f"vn-{value}")
            assert prompt.call_count == expect_held, value

    def test_an_unrecognised_policy_fails_closed_and_says_so(self, alice_config, caplog):
        """Loud, not silent: an unreachable value reaching here is a bug, and
        `gate` is the safe direction to be wrong in."""
        config = _verify_config(alice_config, policy="verifty")

        with caplog.at_level("WARNING"):
            _, prompt, _ = _poll_verify(config, "mx.test; dmarc=pass header.from=test.com")

        assert prompt.call_count == 1
        assert "not one of" in caplog.text


# =============================================================================
# TestAuthservIdDiscovery (ISSUE-249)
# =============================================================================


class TestAuthservIdDiscovery:
    """The setting does nothing until it is set, and the value lives in a raw
    header most people never open. The poller reads it off and produces the exact
    line to paste — once per observed id, and only while the setting is blank.

    It raises no notification of its own. A healthy mail path is silent on the
    alert channel today, and an advisory arriving there would both devalue a
    channel that otherwise means "your mail authentication is failing" and land
    unsolicited on every deployment that upgrades. It logs, and it rides along
    with a canary alert when one is already firing.
    """

    @staticmethod
    def _poll(config, header, id="s1"):
        return _poll_alerted(config, _self_claim(id, headers=[header] if header else []))[1]

    def test_a_clean_verdict_logs_the_id_and_raises_nothing(self, alice_config, caplog):
        """Two properties at once. The id is named only where the verdict passed,
        because that is the evidence the stamp came from a real MTA rather than
        from the sender. And mail that authenticates cleanly stays silent on the
        alert channel, advisory or not — that is what decided the shape."""
        with caplog.at_level("INFO"):
            alert = self._poll(alice_config(), "mx.test; dmarc=pass header.from=test.com")

        assert alert.call_count == 0
        assert "'mx.test'" in caplog.text
        assert "[email] authserv_id" in caplog.text

    def test_a_failing_verdict_never_names_an_id(self, alice_config, caplog):
        """The attack this closes: a spoofer can raise a canary alert on demand by
        sending a forged self-claim that fails. If the alert then recommended the
        authserv-id off that same forged header, an operator who pasted it would
        scope the check to an id the attacker stamps — silencing the canary, and
        under `verify` turning every forged message into a pass. So a failing
        verdict gets generic advice naming nothing."""
        with caplog.at_level("INFO"):
            alert = self._poll(
                alice_config(), "forged.example; dmarc=fail header.from=test.com")

        assert alert.call_count == 1
        body = alert.call_args.args[2]
        assert "forged.example" not in body
        assert "forged.example" not in caplog.text
        assert "authserv_id" in body

    def test_the_advice_still_rides_along_with_an_alert(self, alice_config):
        """Where an operator is already being interrupted, tell them the check can
        be scoped — just without a value read off the header under suspicion.

        Spelled ``email.authserv_id``, not ``[email] authserv_id``: the alert is
        flattened before it is pushed (ISSUE-310) and the square brackets do not
        survive that. The advice has to name something an operator can act on
        after the flattening, not before it. The log lines asserted elsewhere in
        this class keep the TOML section form — they are not flattened.
        """
        alert = self._poll(alice_config(), "mx.test; dmarc=fail header.from=test.com")

        assert "email.authserv_id" in alert.call_args.args[2]

    def test_setting_the_id_silences_both_halves_for_good(self, alice_config, caplog):
        """The nag terminates on the one action it asks for, which is why it needs
        no off switch of its own."""
        with caplog.at_level("INFO"):
            alert = self._poll(alice_config(authserv_id="mx.test"),
                               "mx.test; dmarc=fail header.from=test.com")

        assert "authserv_id" not in caplog.text
        assert "authserv_id" not in alert.call_args.args[2]

    def test_distinct_forged_ids_cannot_make_it_a_per_message_log(self, alice_config, caplog):
        """The dedup key is the user, not the observed id. Keyed on the id it would
        be an unbounded axis — the observed value is attacker-chosen in exactly the
        state this runs in, so every message could carry a fresh one, giving one
        log line and one permanent set entry per message."""
        config = alice_config()

        with caplog.at_level("INFO"):
            for n in range(5):
                self._poll(config, f"mx{n}.test; dmarc=pass header.from=test.com",
                           id=f"s5-{n}")

        assert caplog.text.count("[email] authserv_id") == 1
        assert len(inbound_module._authserv_id_suggested) == 1

    def test_mail_with_no_stamp_suggests_nothing(self, alice_config, caplog):
        """Nothing to name, and inventing a value would be worse than silence."""
        with caplog.at_level("INFO"):
            alert = self._poll(alice_config(dmarc_canary_warn_on_missing=True), None)

        assert "'mx" not in caplog.text
        assert "authserv_id" in alert.call_args.args[2]


# =============================================================================
# TestCanaryAlertOutcomeWording (ISSUE-249 Gap 3)
# =============================================================================


class TestCanaryAlertOutcomeWording:
    """The alert describes the *policy*, never this message's fate.

    An earlier draft inferred the outcome from the policy string and was wrong in
    both directions: it told a `gate` deployment nothing was blocked when
    everything is, and told a `verify` deployment a trusted sender's message was
    held when it ran. What actually happened is not knowable where the alert is
    composed — the hold is decided much later and also turns on the trust list,
    and the quiet-sender and rate-limit branches can drop the message before a
    task exists at all.
    """

    @staticmethod
    def _poll(alice_config, policy, *, trusted=()):
        config = _verify_config(alice_config, policy)
        config.users["alice"].trusted_email_senders = list(trusted)
        _, prompt, alert = _run_poll(
            config, _self_claim("w1", header="mx.test; dmarc=fail header.from=test.com"),
            prompt=(True, 99), alert=True,
        )
        return alert.call_args.args[2], prompt.call_count

    def test_gate_does_not_claim_nothing_was_blocked(self, alice_config):
        """Under `gate` every self-claim is held, so the old flat sentence was the
        exact opposite of what happened."""
        body, held = self._poll(alice_config, "gate")

        assert held == 1
        assert "Nothing was blocked" not in body
        assert "gate" in body

    def test_verify_does_not_claim_a_hold_that_did_not_happen(self, alice_config):
        """A trusted sender goes through on a failing verdict, and the alert still
        fires. Claiming the message was held would be false."""
        body, held = self._poll(alice_config, "verify", trusted=["alice@test.com"])

        assert held == 0
        assert "The message is held" not in body
        assert "unless its sender is explicitly trusted" in body

    def test_off_says_nothing_was_blocked(self, alice_config):
        body, held = self._poll(alice_config, "off")

        assert held == 0
        assert "Nothing was blocked" in body


# =============================================================================
# TestVerifyHoldIsDiagnosable (ISSUE-249 Gap 3)
# =============================================================================


class TestVerifyHoldIsDiagnosable:
    """A `verify` hold always says why, even where the canary is silent.

    The canary's WARNING is the usual explanation, but it sits behind two switches
    the gate is deliberately independent of. In either state every self-addressed
    message would be held with nothing in the log saying why, and an unanswered
    hold is cancelled at `confirmation_timeout_minutes` — so the failure mode is
    mail quietly going missing.
    """

    @staticmethod
    def _poll(alice_config, header, **email_settings):
        config = _verify_config(alice_config, **email_settings)
        return _poll_verify(config, header, id="h1")[1].call_count

    def test_a_hold_with_the_canary_off_is_still_logged(self, alice_config, caplog):
        with caplog.at_level("WARNING"):
            held = self._poll(alice_config, "mx.test; dmarc=fail header.from=test.com",
                              dmarc_canary=False)

        assert held == 1
        assert "confirm_sender_match is 'verify'" in caplog.text
        assert "alice@test.com" in caplog.text

    def test_a_hold_on_an_unevaluated_verdict_is_logged(self, alice_config, caplog):
        """The default flags leave `unevaluated` silent on the canary, so without
        this the most likely `verify` hold of all would have no diagnostic."""
        with caplog.at_level("WARNING"):
            held = self._poll(alice_config, "mx.test; spf=pass smtp.mailfrom=test.com")

        assert held == 1
        assert "confirm_sender_match is 'verify'" in caplog.text

    def test_a_message_that_passes_logs_no_hold(self, alice_config, caplog):
        with caplog.at_level("WARNING"):
            held = self._poll(alice_config, "mx.test; dmarc=pass header.from=test.com")

        assert held == 0
        assert "confirm_sender_match is 'verify'" not in caplog.text
