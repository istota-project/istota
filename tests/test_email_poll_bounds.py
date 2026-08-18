"""Regression tests for the bounded, lossless inbound email poll (ISSUE-250).

Three properties the old newest-50 window did not have:

1. **No silent loss.** The poll used to fetch the newest 50 messages in the
   folder and dedupe *afterwards*, so anything that fell below the top 50
   between two ticks was never fetched again. These tests drive the poll
   repeatedly and assert every message eventually becomes a task.
2. **A bounded batch.** One tick does a fixed amount of work and leaves the
   rest for the next one, rather than truncating the backlog.
3. **A UID namespace.** IMAP UIDs are only unique within a UIDVALIDITY. The
   dedupe ledger used to key on the bare UID, so a recreated mailbox made
   every new message look already-processed.

Plus the locking property the restructure was for: the framework DB's write
lock is no longer held across the per-message IMAP and WebDAV network I/O.

The fake mailbox below reproduces the IMAP semantics the poll now depends on
— UID-range search, ascending fetch order, the ``limit`` slice applied after
ordering, and the ``<n>:*`` quirk whereby a range whose start is past the
highest assigned UID still returns the last message in the mailbox.
"""

import re
import sqlite3
from unittest.mock import patch

import pytest

from istota import db
from istota.config import Config, EmailConfig as AppEmailConfig, UserConfig
from istota.skills.email import Email, EmailEnvelope
from istota.transport.email import inbound
from istota.transport.email.inbound import poll_emails


@pytest.fixture
def make_config(tmp_path):
    """A Config whose framework DB exists, with email enabled for one user."""
    def _make(**overrides):
        config = Config()
        config.db_path = tmp_path / "test.db"
        db.init_db(config.db_path)
        config.temp_dir = tmp_path / "temp"
        config.temp_dir.mkdir(exist_ok=True)
        config.skills_dir = tmp_path / "skills"
        config.skills_dir.mkdir(exist_ok=True)
        config.email = AppEmailConfig(
            enabled=True,
            imap_host="imap.test",
            imap_port=993,
            imap_user="user",
            imap_password="pass",
            smtp_host="smtp.test",
            smtp_port=587,
            bot_email="bot@test.com",
        )
        config.users = {"alice": UserConfig(email_addresses=["alice@test.com"])}
        # These tests are about the poll *window* — that nothing is buried and
        # a backlog drains in arrival order. The volume budget added later
        # (ISSUE-250 consequence 1) is an orthogonal control on the same path,
        # and leaving it on would silently cap the message counts here, so a
        # cursor regression could hide behind a limiter. Its own coverage is in
        # `test_email_volume_budget.py`.
        config.scheduler.email_rate_limit_messages = 0
        config.scheduler.email_sender_rate_limit_messages = 0
        for key, val in overrides.items():
            setattr(config, key, val)
        return config
    return _make


class FakeMailbox:
    """A stand-in IMAP folder with faithful UID-range + limit semantics."""

    def __init__(self, uids, uidvalidity=1, sender="alice@test.com"):
        self.uids = sorted(uids)
        self.uidvalidity = uidvalidity
        self.sender = sender
        self.read_calls = []

    def _envelope(self, uid):
        return EmailEnvelope(
            id=str(uid),
            subject=f"Message {uid}",
            sender=self.sender,
            date="Mon, 01 Jan 2026 10:00:00 +0000",
            is_read=False,
            to=("bot@test.com",),
            uidvalidity=self.uidvalidity,
        )

    def list_emails(self, folder=None, limit=None, config=None,
                    criteria=None, oldest_first=False):
        selected = list(self.uids)
        if criteria is not None:
            match = re.search(r"UID (\d+):\*", str(criteria))
            assert match, f"unexpected criteria: {criteria!r}"
            start = int(match.group(1))
            in_range = [u for u in selected if u >= start]
            # RFC 3501: a `<n>:*` range always includes the highest assigned
            # UID, even when n is above it. The poll must tolerate getting a
            # message it has already processed back on every caught-up tick.
            selected = in_range if in_range else selected[-1:]
        ordered = selected if oldest_first else list(reversed(selected))
        if limit is not None:
            ordered = ordered[:limit]
        return [self._envelope(u) for u in ordered]

    def read_email(self, email_id, folder=None, config=None, envelope=None):
        self.read_calls.append(email_id)
        return Email(
            id=email_id,
            subject=f"Message {email_id}",
            sender=self.sender,
            date="Mon, 01 Jan 2026 10:00:00 +0000",
            body="Body text",
            attachments=[],
            message_id=f"<msg{email_id}@test.com>",
            references=None,
            to=("bot@test.com",),
            cc=(),
        )


def _run_poll(config, mailbox, read_email=None):
    """Drive one poll against `mailbox`, with the network legs stubbed."""
    with (
        patch("istota.transport.email.inbound.list_emails", mailbox.list_emails),
        patch("istota.transport.email.inbound.read_email",
              read_email or mailbox.read_email),
        patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        patch("istota.transport.email.inbound.ensure_user_directories_v2"),
        patch("istota.transport.email.inbound.upload_file_to_inbox_v2"),
    ):
        return poll_emails(config)


@pytest.fixture(autouse=True)
def _clear_message_failures():
    """`poll_emails` counts per-message failures in a module-level dict."""
    inbound._reset_message_failures()
    yield
    inbound._reset_message_failures()


def _prime_cursor(config, mailbox):
    """Run one poll so the folder has a cursor.

    The first poll of a folder deliberately resumes near the top of the
    mailbox rather than walking it from UID 1, so tests about draining a
    *backlog* have to establish a running poller first — which is the real
    scenario: mail arrives in bulk at an instance that was already polling.
    """
    original = mailbox.uids
    mailbox.uids = original[:1]
    _run_poll(config, mailbox)
    mailbox.uids = original


def _processed_uids(config):
    with db.get_db(config.db_path) as conn:
        rows = conn.execute("SELECT email_id FROM processed_emails").fetchall()
    return {int(r["email_id"]) for r in rows}


# =============================================================================
# Consequence 2 — the window silently buried mail
# =============================================================================


class TestNoSilentLoss:
    def test_backlog_larger_than_one_batch_drains_over_successive_polls(
        self, make_config,
    ):
        """The reported symptom: mail below the newest 50 was never fetched.

        A running poller, then 119 messages arrive at once against a batch
        size of 50. Under the old top-50 window UIDs 2..70 could never be
        reached — the window stayed pinned to the newest 50, all of them
        processed, and nothing walked backwards.
        """
        config = make_config()
        config.scheduler.email_poll_batch_size = 50
        mailbox = FakeMailbox(range(1, 121))
        _prime_cursor(config, mailbox)

        created = []
        for _ in range(10):
            created.extend(_run_poll(config, mailbox))

        assert _processed_uids(config) == set(range(1, 121))
        assert len(created) == 119  # UID 1 was consumed priming the cursor

    def test_oldest_unprocessed_mail_is_taken_first(self, make_config):
        """A batch drains in arrival order, so nothing waits behind newer mail."""
        config = make_config()
        config.scheduler.email_poll_batch_size = 10
        mailbox = FakeMailbox(range(1, 31))
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox)

        assert _processed_uids(config) == set(range(1, 12))

    def test_caught_up_poll_creates_nothing(self, make_config):
        """The `<n>:*` quirk hands back an already-processed message every
        tick once drained; it must not become a duplicate task."""
        config = make_config()
        config.scheduler.email_poll_batch_size = 50
        mailbox = FakeMailbox(range(1, 6))

        first = _run_poll(config, mailbox)
        second = _run_poll(config, mailbox)
        third = _run_poll(config, mailbox)

        assert len(first) == 5
        assert second == []
        assert third == []

    def test_new_mail_after_drain_is_picked_up(self, make_config):
        config = make_config()
        config.scheduler.email_poll_batch_size = 50
        mailbox = FakeMailbox(range(1, 6))
        _run_poll(config, mailbox)

        mailbox.uids = list(range(1, 9))
        created = _run_poll(config, mailbox)

        assert len(created) == 3
        assert _processed_uids(config) == set(range(1, 9))


# =============================================================================
# Bounded work per tick
# =============================================================================


class TestBatchBound:
    def test_one_tick_processes_at_most_the_batch_size(self, make_config):
        config = make_config()
        config.scheduler.email_poll_batch_size = 7
        mailbox = FakeMailbox(range(1, 51))

        created = _run_poll(config, mailbox)

        assert len(created) == 7
        # And the expensive per-message read is bounded too, not just the
        # task creation — the batch is a work budget, not an output filter.
        assert len(mailbox.read_calls) == 7

    def test_remaining_backlog_is_logged(self, make_config, caplog):
        config = make_config()
        config.scheduler.email_poll_batch_size = 5
        mailbox = FakeMailbox(range(1, 21))

        with caplog.at_level("INFO"):
            _run_poll(config, mailbox)

        assert any("backlog" in r.message.lower() for r in caplog.records), (
            "a truncated batch must say that more mail is waiting"
        )


# =============================================================================
# UIDVALIDITY — the ledger key's namespace
# =============================================================================


class TestUidValidity:
    def test_recreated_mailbox_does_not_look_already_processed(self, make_config):
        """UIDs restart at 1 when UIDVALIDITY changes. Keyed on the bare UID,
        every message in the new mailbox collided with an old row: skipped as
        a duplicate, and an IntegrityError on insert."""
        config = make_config()
        config.scheduler.email_poll_batch_size = 50

        first = FakeMailbox(range(1, 4), uidvalidity=1)
        assert len(_run_poll(config, first)) == 3

        # Same UIDs, different messages, new validity.
        second = FakeMailbox(range(1, 4), uidvalidity=2)
        created = _run_poll(config, second)

        assert len(created) == 3, "mail in a recreated mailbox was dropped"

    def test_ledger_rows_are_namespaced_by_validity(self, make_config):
        config = make_config()
        config.scheduler.email_poll_batch_size = 50
        _run_poll(config, FakeMailbox(range(1, 4), uidvalidity=1))
        _run_poll(config, FakeMailbox(range(1, 4), uidvalidity=2))

        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT uidvalidity, email_id FROM processed_emails "
                "ORDER BY uidvalidity, CAST(email_id AS INTEGER)"
            ).fetchall()

        assert [(r["uidvalidity"], r["email_id"]) for r in rows] == [
            (1, "1"), (1, "2"), (1, "3"),
            (2, "1"), (2, "2"), (2, "3"),
        ]

    def test_same_uid_same_validity_is_still_deduped(self, make_config):
        """The namespace must not weaken the dedupe it qualifies."""
        config = make_config()
        config.scheduler.email_poll_batch_size = 50
        mailbox = FakeMailbox(range(1, 4), uidvalidity=1)

        assert len(_run_poll(config, mailbox)) == 3
        assert _run_poll(config, mailbox) == []


# =============================================================================
# Consequence 3 — the write lock across network I/O
# =============================================================================


class TestWriteLockNotHeldAcrossNetworkIo:
    def test_another_writer_can_commit_while_the_poll_reads_mail(
        self, make_config,
    ):
        """The whole per-message loop used to run inside one transaction, so
        the first `mark_email_processed` took the framework DB's write lock
        and held it across every remaining IMAP login, attachment download
        and WebDAV upload in the batch.

        Probing on the *second* message is what makes this non-vacuous: at
        the first message's read nothing has been written yet, so no lock is
        held under either design.
        """
        config = make_config()
        config.scheduler.email_poll_batch_size = 50
        mailbox = FakeMailbox(range(1, 4))
        outcomes = []

        def _read_email(email_id, folder=None, config=None, envelope=None):
            if email_id == "2":
                probe = sqlite3.connect(str(config_db_path), timeout=0.5)
                try:
                    probe.execute("PRAGMA busy_timeout = 500")
                    probe.execute(
                        "INSERT INTO processed_emails "
                        "(uidvalidity, email_id, sender_email, subject) "
                        "VALUES (99, 'probe', 'probe@test.com', 'probe')"
                    )
                    probe.commit()
                    outcomes.append("committed")
                except sqlite3.OperationalError as e:
                    outcomes.append(f"blocked: {e}")
                finally:
                    probe.close()
            return mailbox.read_email(email_id, folder, config, envelope)

        config_db_path = config.db_path
        _run_poll(config, mailbox, read_email=_read_email)

        assert outcomes == ["committed"], (
            f"poll held the write lock across message I/O: {outcomes}"
        )


# =============================================================================
# Forward progress past an unreadable message
# =============================================================================


class TestUnreadableMessage:
    def test_a_failing_message_is_retried_before_it_is_filed(self, make_config):
        """A dropped IMAP socket must not cost the message.

        `read_email` opens its own connection per message, so the likeliest
        failure is transient. Filing on the first error would turn one bad
        moment into permanently lost mail — the exact failure class this issue
        is about.
        """
        config = make_config()
        config.scheduler.email_poll_batch_size = 50
        mailbox = FakeMailbox(range(1, 4))
        _prime_cursor(config, mailbox)

        def _read_email(email_id, folder=None, config=None, envelope=None):
            if email_id == "2":
                raise RuntimeError("IMAP fetch failed")
            return mailbox.read_email(email_id, folder, config, envelope)

        _run_poll(config, mailbox, read_email=_read_email)

        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT routing_method FROM processed_emails WHERE email_id = '2'"
            ).fetchone()
        assert row is None, "filed on the first failure instead of retrying"

        # It recovers if the next attempt succeeds — the cursor was held back
        # rather than stepping over the message it still owed.
        created = _run_poll(config, mailbox)
        assert len(created) == 1
        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT routing_method FROM processed_emails WHERE email_id = '2'"
            ).fetchone()
        assert row["routing_method"] == "sender_match"

    def test_a_permanently_failing_message_is_filed_and_the_poll_moves_on(
        self, make_config,
    ):
        """A message that always fails must not pin the cursor.

        Against a forward cursor an unresolvable message would starve
        everything behind it. After the retry budget it is recorded with
        `read_error` — filed, still in INBOX and reachable by
        `email from-senders`, not silently dropped — and the batch moves past.
        """
        config = make_config()
        config.scheduler.email_poll_batch_size = 50
        mailbox = FakeMailbox(range(1, 4))
        _prime_cursor(config, mailbox)

        def _read_email(email_id, folder=None, config=None, envelope=None):
            if email_id == "2":
                raise RuntimeError("IMAP fetch failed")
            return mailbox.read_email(email_id, folder, config, envelope)

        for _ in range(inbound._MAX_MESSAGE_ATTEMPTS):
            _run_poll(config, mailbox, read_email=_read_email)

        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT routing_method FROM processed_emails WHERE email_id = '2'"
            ).fetchone()
        assert row is not None, "unreadable mail vanished without a ledger row"
        assert row["routing_method"] == "read_error"

        # And the poll is caught up rather than stuck on it.
        assert _run_poll(config, mailbox, read_email=_read_email) == []
        assert _processed_uids(config) == {1, 2, 3}

    def test_one_failing_message_does_not_stop_the_rest_of_the_batch(
        self, make_config,
    ):
        """The batch must keep going. Letting one message abort the poll would
        mean the same batch is refetched every tick and everything behind it
        starves — a coupling the old newest-N window did not have."""
        config = make_config()
        config.scheduler.email_poll_batch_size = 50
        mailbox = FakeMailbox(range(1, 6))
        _prime_cursor(config, mailbox)

        def _read_email(email_id, folder=None, config=None, envelope=None):
            if email_id == "2":
                raise RuntimeError("IMAP fetch failed")
            return mailbox.read_email(email_id, folder, config, envelope)

        created = _run_poll(config, mailbox, read_email=_read_email)

        assert len(created) == 3, "a failing message took the batch down"
        assert _processed_uids(config) == {1, 3, 4, 5}

    def test_a_message_with_no_usable_uid_is_skipped_not_fatal(self, make_config):
        """`imap-tools` yields None when a FETCH response omits the UID. That
        has no ledger key and no cursor position, so reaching
        `mark_email_processed` with it would raise IntegrityError and abort
        the batch."""
        config = make_config()
        config.scheduler.email_poll_batch_size = 50
        mailbox = FakeMailbox(range(1, 4))
        _prime_cursor(config, mailbox)

        real_list = mailbox.list_emails

        def _list_emails(**kwargs):
            envelopes = real_list(**kwargs)
            if envelopes:
                envelopes[0].id = None
            return envelopes

        mailbox.list_emails = _list_emails
        created = _run_poll(config, mailbox)

        # The other messages in the batch still land.
        assert len(created) == 1
        assert _processed_uids(config) == {1, 3}


# =============================================================================
# The upgrade path
# =============================================================================


LEGACY_PROCESSED_EMAILS_DDL = """
CREATE TABLE processed_emails (
    id INTEGER PRIMARY KEY,
    email_id TEXT NOT NULL UNIQUE,
    sender_email TEXT NOT NULL,
    subject TEXT,
    thread_id TEXT,
    message_id TEXT,
    "references" TEXT,
    user_id TEXT,
    task_id INTEGER,
    routing_method TEXT,
    processed_at TEXT DEFAULT (datetime('now'))
)
"""


def _legacy_db(path, rows):
    """A framework DB carrying the pre-ISSUE-250 ledger shape and some rows."""
    conn = sqlite3.connect(str(path))
    conn.execute(LEGACY_PROCESSED_EMAILS_DDL)
    conn.executemany(
        "INSERT INTO processed_emails (email_id, sender_email, subject, "
        "routing_method) VALUES (?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    db.init_db(path)  # runs the migration


class TestUidValidityMigration:
    def test_rebuild_preserves_rows_and_widens_the_key(self, tmp_path):
        path = tmp_path / "legacy.db"
        _legacy_db(path, [
            ("1", "alice@test.com", "One", "plus_address"),
            ("2", "bob@test.com", "Two", "discarded"),
        ])

        with db.get_db(path) as conn:
            ddl = conn.execute(
                "SELECT sql FROM sqlite_master WHERE name = 'processed_emails'"
            ).fetchone()[0]
            rows = conn.execute(
                "SELECT uidvalidity, email_id, sender_email, subject, "
                "routing_method FROM processed_emails ORDER BY id"
            ).fetchall()

        assert "UNIQUE (uidvalidity, email_id)" in ddl
        assert [tuple(r) for r in rows] == [
            (0, "1", "alice@test.com", "One", "plus_address"),
            (0, "2", "bob@test.com", "Two", "discarded"),
        ]

    def test_rebuild_is_idempotent(self, tmp_path):
        path = tmp_path / "legacy.db"
        _legacy_db(path, [("1", "alice@test.com", "One", "plus_address")])
        db.init_db(path)
        db.init_db(path)

        with db.get_db(path) as conn:
            count = conn.execute(
                "SELECT COUNT(*) FROM processed_emails"
            ).fetchone()[0]
        assert count == 1

    def test_same_uid_in_two_validities_can_coexist(self, tmp_path):
        path = tmp_path / "legacy.db"
        _legacy_db(path, [("7", "alice@test.com", "One", "plus_address")])

        with db.get_db(path) as conn:
            db.mark_email_processed(
                conn, email_id="7", sender_email="bob@test.com",
                subject="Different message", uidvalidity=99,
            )
            count = conn.execute(
                "SELECT COUNT(*) FROM processed_emails WHERE email_id = '7'"
            ).fetchone()[0]
        assert count == 2

    def test_first_poll_after_upgrade_does_not_reingest_the_mailbox(
        self, make_config, tmp_path,
    ):
        """The migration leaves old rows in namespace 0. Without adoption, the
        first poll would find no match for any real UID and turn every message
        still in the mailbox into a fresh task — a task storm on deploy."""
        config = make_config()
        config.scheduler.email_poll_batch_size = 50

        # Simulate the pre-upgrade ledger: UIDs 1..5 handled, namespace 0.
        with db.get_db(config.db_path) as conn:
            for uid in range(1, 6):
                db.mark_email_processed(
                    conn, email_id=str(uid), sender_email="alice@test.com",
                    subject=f"Message {uid}", routing_method="plus_address",
                    uidvalidity=0,
                )

        # Same mailbox, now reporting its real UIDVALIDITY, with one new message.
        mailbox = FakeMailbox(range(1, 7), uidvalidity=12345)
        created = _run_poll(config, mailbox)

        assert len(created) == 1, (
            f"upgrade re-ingested already-processed mail: {len(created)} tasks"
        )
        with db.get_db(config.db_path) as conn:
            stale = conn.execute(
                "SELECT COUNT(*) FROM processed_emails WHERE uidvalidity = 0"
            ).fetchone()[0]
        assert stale == 0, "legacy rows were left in the unknown namespace"

    def test_a_real_recreation_after_the_first_poll_is_not_adopted(self, make_config):
        """Once a folder has a cursor, a validity change is a real mailbox
        recreation: the new UIDs are new mail and must not be swallowed as
        already-seen, nor adopted into the old namespace."""
        config = make_config()
        config.scheduler.email_poll_batch_size = 50

        assert len(_run_poll(config, FakeMailbox(range(1, 4), uidvalidity=1))) == 3
        created = _run_poll(config, FakeMailbox(range(1, 4), uidvalidity=2))
        assert len(created) == 3

        with db.get_db(config.db_path) as conn:
            validities = [
                r[0] for r in conn.execute(
                    "SELECT DISTINCT uidvalidity FROM processed_emails "
                    "ORDER BY uidvalidity"
                ).fetchall()
            ]
        assert validities == [1, 2]


class TestUnknownUidValidity:
    """UIDVALIDITY 0 means "the server did not tell us", not an observation.

    Reading it as a namespace makes one unanswered IMAP command look like a
    recreated mailbox — which resets the cursor and re-ingests everything as
    fresh tasks. That is a mail storm produced by a transient failure, so the
    unknown value must change nothing.
    """

    def test_a_transient_uidvalidity_failure_does_not_reingest(self, make_config):
        config = make_config()
        config.scheduler.email_poll_batch_size = 50

        assert len(_run_poll(config, FakeMailbox(range(1, 21), uidvalidity=7))) == 20

        # Same mailbox, same messages, but STATUS/SELECT gave nothing this tick.
        created = _run_poll(config, FakeMailbox(range(1, 21), uidvalidity=0))

        assert created == [], (
            f"an unreadable UIDVALIDITY re-ingested the mailbox: {len(created)} tasks"
        )

    def test_the_cursor_is_not_rewritten_to_the_unknown_namespace(self, make_config):
        config = make_config()
        config.scheduler.email_poll_batch_size = 50
        _run_poll(config, FakeMailbox(range(1, 6), uidvalidity=7))
        _run_poll(config, FakeMailbox(range(1, 6), uidvalidity=0))

        with db.get_db(config.db_path) as conn:
            cursor = db.get_email_poll_cursor(conn, config.email.poll_folder)
        assert cursor is not None
        assert cursor[0] == 7, "the stored namespace was clobbered with 0"

    def test_a_server_that_never_reports_it_still_works(self, make_config):
        """The pre-namespace behaviour, unchanged: everything lives in 0."""
        config = make_config()
        config.scheduler.email_poll_batch_size = 50
        mailbox = FakeMailbox(range(1, 6), uidvalidity=0)

        assert len(_run_poll(config, mailbox)) == 5
        assert _run_poll(config, mailbox) == []

    def test_a_server_that_starts_reporting_it_adopts_rather_than_resets(
        self, make_config,
    ):
        """0 -> real is the ledger gaining a name, not the mailbox changing."""
        config = make_config()
        config.scheduler.email_poll_batch_size = 50
        assert len(_run_poll(config, FakeMailbox(range(1, 21), uidvalidity=0))) == 20

        created = _run_poll(config, FakeMailbox(range(1, 21), uidvalidity=7))

        assert created == [], (
            f"a newly reported UIDVALIDITY re-ingested the mailbox: {len(created)}"
        )


class TestFirstPollDoesNotAnswerOldMail:
    """The first poll of a folder resumes near the top rather than walking it.

    Mail the old window buried has no ledger row — that is the bug — so a walk
    from UID 1 would ingest it, and ingesting a months-old message means
    answering it: a reply mailed to the original sender, or a confirmation
    prompt per message. This fix stops mail being lost from here on; it does
    not reach back and answer mail that was already lost.
    """

    def test_upgrade_resumes_from_the_highest_ledger_uid(self, make_config):
        config = make_config()
        config.scheduler.email_poll_batch_size = 50

        # An upgrading instance: the old poller handled UIDs 40..45, and 1..39
        # are mail it buried and never recorded.
        with db.get_db(config.db_path) as conn:
            for uid in range(40, 46):
                db.mark_email_processed(
                    conn, email_id=str(uid), sender_email="alice@test.com",
                    subject=f"Message {uid}", routing_method="plus_address",
                    uidvalidity=0,
                )

        mailbox = FakeMailbox(range(1, 51), uidvalidity=3)
        created = _run_poll(config, mailbox)

        # Only 46..50 — the genuinely new mail. Not the buried 1..39.
        assert _processed_uids(config) == set(range(40, 51))
        assert len(created) == 5

    def test_fresh_install_takes_at_most_one_batch_from_the_top(self, make_config):
        """No ledger at all — the same bounded first touch the old window gave."""
        config = make_config()
        config.scheduler.email_poll_batch_size = 10
        mailbox = FakeMailbox(range(1, 101), uidvalidity=3)

        created = _run_poll(config, mailbox)

        assert len(created) == 10
        assert _processed_uids(config) == set(range(91, 101))
