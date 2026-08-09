"""Email retention, both halves (ISSUE-230 + ISSUE-231).

**IMAP side (ISSUE-230).** ``cleanup_old_emails`` used to fetch the newest 100
envelopes and delete whichever of *those* were older than the cutoff — a
retention pass working from the wrong end of the mailbox. Above roughly
``100 / retention_days`` messages a day every candidate is younger than the
cutoff on every sweep, so it deletes nothing, forever, and reports a clean run.
The sweep is server-side now: one IMAP ``BEFORE`` search, work proportional to
what has actually expired.

**DB side (ISSUE-231).** ``processed_emails`` gets one row per polled message —
including the ones that produce nothing (bot self-mail, discarded, quiet
senders) — and nothing ever deleted from it. The prune window has to dominate
the IMAP one: a row is the only thing stopping a message still sitting in
``poll_folder`` from being re-ingested as a fresh task.
"""

from datetime import date, datetime, timedelta, timezone
from datetime import timezone as tz_offset
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import Config, EmailConfig as AppEmailConfig, SchedulerConfig
from istota.email_support import cleanup_old_emails
from istota.skills.email import EmailConfig, delete_emails_before


def _app_email_config(**kw):
    base = dict(
        enabled=True,
        imap_host="imap.example.com", imap_port=993,
        imap_user="bot@example.com", imap_password="pw",
        smtp_host="smtp.example.com", smtp_port=587,
        bot_email="bot@example.com", poll_folder="INBOX",
    )
    base.update(kw)
    return AppEmailConfig(**base)


class TestServerSideImapSweep:
    """``delete_emails_before`` is the new primitive: one connection, one
    search, one bulk delete."""

    def _mailbox(self, uids):
        mailbox = MagicMock()
        mailbox.__enter__ = MagicMock(return_value=mailbox)
        mailbox.__exit__ = MagicMock(return_value=False)
        mailbox.uids.return_value = uids
        return mailbox

    def test_searches_server_side_and_deletes_the_whole_result(self):
        mailbox = self._mailbox([str(i) for i in range(500)])
        cfg = EmailConfig(
            imap_host="h", imap_port=993, imap_user="u", imap_password="p",
            smtp_host="s", smtp_port=587, bot_email="b@example.com",
        )

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            deleted = delete_emails_before(date(2026, 1, 1), folder="INBOX", config=cfg)

        assert deleted == 500, "not capped at a fixed head window"
        mailbox.uids.assert_called_once()
        criteria = str(mailbox.uids.call_args[0][0])
        assert "BEFORE" in criteria and "1-Jan-2026" in criteria
        assert mailbox.delete.call_count >= 1
        deleted_uids = [u for call in mailbox.delete.call_args_list for u in call[0][0]]
        assert len(deleted_uids) == 500

    def test_no_matches_deletes_nothing(self):
        mailbox = self._mailbox([])
        cfg = EmailConfig(
            imap_host="h", imap_port=993, imap_user="u", imap_password="p",
            smtp_host="s", smtp_port=587, bot_email="b@example.com",
        )

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            deleted = delete_emails_before(date(2026, 1, 1), folder="INBOX", config=cfg)

        assert deleted == 0
        mailbox.delete.assert_not_called()


class TestCleanupOldEmails:
    def _config(self, **kw):
        return Config(email=_app_email_config(**kw))

    def test_disabled_returns_zero(self):
        config = Config(email=AppEmailConfig(enabled=False))
        with patch("istota.email_support.delete_emails_before") as mock_delete:
            assert cleanup_old_emails(config, days=7) == 0
        mock_delete.assert_not_called()

    def test_zero_days_returns_zero(self):
        config = self._config()
        with patch("istota.email_support.delete_emails_before") as mock_delete:
            assert cleanup_old_emails(config, days=0) == 0
        mock_delete.assert_not_called()

    def test_passes_a_cutoff_date_derived_from_the_retention_window(self):
        """Exact, against a frozen clock. A tolerance here would admit the
        one-day error the DB-side floor is sized around."""
        config = self._config()
        frozen = datetime(2026, 8, 8, 3, 0, tzinfo=timezone.utc)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen if tz else frozen.replace(tzinfo=None)

        with (
            patch("istota.email_support.datetime", _FrozenDatetime),
            patch(
                "istota.email_support.delete_emails_before", return_value=3,
            ) as mock_delete,
        ):
            assert cleanup_old_emails(config, days=7) == 3

        cutoff = mock_delete.call_args[0][0]
        assert isinstance(cutoff, date)
        assert cutoff == date(2026, 8, 1)
        assert mock_delete.call_args[1]["folder"] == "INBOX"

    def test_cutoff_is_utc_not_the_daemon_host_local_date(self):
        """IMAP ``BEFORE`` is evaluated in the mail server's zone. A daemon east
        of the server computing a local date would delete a day early."""
        config = self._config()
        # 2026-08-08 22:00 UTC is already 2026-08-09 in UTC+13.
        frozen = datetime(2026, 8, 8, 22, 0, tzinfo=timezone.utc)
        local = frozen.astimezone(tz_offset(timedelta(hours=13))).replace(tzinfo=None)

        class _FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                return frozen if tz else local

        with (
            patch("istota.email_support.datetime", _FrozenDatetime),
            patch(
                "istota.email_support.delete_emails_before", return_value=0,
            ) as mock_delete,
        ):
            cleanup_old_emails(config, days=7)

        assert mock_delete.call_args[0][0] == date(2026, 8, 1)

    def test_deletes_beyond_the_first_hundred_messages(self):
        """The regression this issue is about: a busy mailbox whose expired
        mail sits far below the newest-100 window."""
        config = self._config()
        mailbox = MagicMock()
        mailbox.__enter__ = MagicMock(return_value=mailbox)
        mailbox.__exit__ = MagicMock(return_value=False)
        mailbox.uids.return_value = [str(i) for i in range(1200)]

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            deleted = cleanup_old_emails(config, days=7)

        assert deleted == 1200

    def test_handles_imap_error(self):
        config = self._config()
        with patch(
            "istota.email_support.delete_emails_before",
            side_effect=Exception("IMAP error"),
        ):
            assert cleanup_old_emails(config, days=7) == 0


class TestProcessedEmailPrune:
    def _mark(self, conn, email_id, *, age_days):
        row_id = db.mark_email_processed(
            conn, email_id=email_id, sender_email="a@example.com",
        )
        conn.execute(
            "UPDATE processed_emails SET processed_at = datetime('now', ? || ' days') "
            "WHERE id = ?",
            (f"-{age_days}", row_id),
        )
        return row_id

    def _remaining(self, conn):
        return {
            r["email_id"]
            for r in conn.execute("SELECT email_id FROM processed_emails")
        }

    def test_prunes_only_rows_past_the_window(self, db_path):
        with db.get_db(db_path) as conn:
            self._mark(conn, "ancient", age_days=200)
            self._mark(conn, "old", age_days=91)
            self._mark(conn, "recent", age_days=5)

            deleted = db.cleanup_old_processed_emails(conn, 90)

            assert deleted == 2
            assert self._remaining(conn) == {"recent"}

    def test_zero_disables_the_prune(self, db_path):
        with db.get_db(db_path) as conn:
            self._mark(conn, "ancient", age_days=5000)

            assert db.cleanup_old_processed_emails(conn, 0) == 0
            assert self._remaining(conn) == {"ancient"}

    def test_prune_is_independent_of_task_retention(self, db_path):
        """The rows outlive their tasks by design — the FK is unenforced and
        ``cleanup_old_tasks`` runs at a much shorter window, so a dangling
        ``task_id`` must not make a row eligible."""
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="p", user_id="alice", source_type="email",
            )
            row_id = db.mark_email_processed(
                conn, email_id="e1", sender_email="a@example.com", task_id=task_id,
            )
            conn.execute(
                "UPDATE processed_emails SET processed_at = datetime('now', '-30 days') "
                "WHERE id = ?", (row_id,),
            )
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

            assert db.cleanup_old_processed_emails(conn, 90) == 0
            assert self._remaining(conn) == {"e1"}

    def test_row_referenced_by_the_transcript_is_kept(self, db_path):
        """ISSUE-226 attribution outlives the task row. ``messages`` is not
        age-pruned — the transcript deliberately survives the ``tasks`` row it
        came from — and ``_EMAIL_SENDER_SUBQUERY`` recovers an email turn's
        envelope sender from this table by ``task_id``. Delete the row and an
        external contact's mail renders in the prompt as the principal's own
        words, which the sleep cycle then writes durably to USER.md and the
        knowledge graph.
        """
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="p", user_id="alice", source_type="email",
                conversation_token="room1",
            )
            row_id = db.mark_email_processed(
                conn, email_id="threaded", sender_email="stranger@example.com",
                task_id=task_id, routing_method="thread_match",
            )
            conn.execute(
                "UPDATE processed_emails SET processed_at = datetime('now', '-400 days') "
                "WHERE id = ?", (row_id,),
            )
            conn.execute(
                "INSERT INTO rooms (token, origin, user_id) VALUES ('room1', 'web', 'alice')"
            )
            conn.execute(
                "INSERT INTO messages (room_token, role, body, task_id, origin_surface) "
                "VALUES ('room1', 'user', 'hello', ?, 'email')",
                (task_id,),
            )
            # The task itself is long gone — retention GCs it at 7 days.
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))

            assert db.cleanup_old_processed_emails(conn, 90) == 0
            assert self._remaining(conn) == {"threaded"}

    def test_row_with_no_transcript_reference_is_still_pruned(self, db_path):
        """The rows that actually pile up produced no task at all — bot
        self-mail, discarded mail, quiet senders — so the exclusion above costs
        the prune almost nothing."""
        with db.get_db(db_path) as conn:
            self._mark(conn, "discarded", age_days=400)
            task_id = db.create_task(
                conn, prompt="p", user_id="alice", source_type="email",
            )
            row_id = db.mark_email_processed(
                conn, email_id="answered", sender_email="a@example.com",
                task_id=task_id,
            )
            conn.execute(
                "UPDATE processed_emails SET processed_at = datetime('now', '-400 days') "
                "WHERE id = ?", (row_id,),
            )

            assert db.cleanup_old_processed_emails(conn, 90) == 2
            assert self._remaining(conn) == set()


class TestPruneWindowFloor:
    """The two windows are coupled: pruning a row for a message still
    physically present in ``poll_folder`` lets that message be re-ingested as a
    brand-new task. The DB window is therefore never allowed below the mail's
    own lifetime, and says so in the log when the configured value is."""

    def _effective(self, **kw):
        from istota.scheduler import _effective_processed_email_retention, _warned_keys

        _warned_keys.clear()  # the warnings latch; each case wants a fresh one
        return _effective_processed_email_retention(SchedulerConfig(**kw))

    def test_floor_is_one_day_above_imap_retention(self):
        """Not *equal* to it. The IMAP sweep searches ``BEFORE <date>``, which
        is date-granular and evaluated in the mail server's zone, while
        ``processed_at`` is an exact UTC timestamp — so a boundary message
        outlives an exactly-equal window by up to a day."""
        assert self._effective(
            email_retention_days=30, processed_email_retention_days=7,
        ) == 31

    def test_configured_window_wins_when_it_already_dominates(self):
        assert self._effective(
            email_retention_days=7, processed_email_retention_days=90,
        ) == 90

    def test_disabled_imap_retention_disables_the_prune(self):
        """``email_retention_days = 0`` means mail is *never* deleted from
        IMAP, so the message's lifetime is unbounded and the row's has to be
        too. Pruning at 90 days there is precisely the re-ingest hazard the
        floor exists to prevent, with no finite window to floor against."""
        assert self._effective(
            email_retention_days=0, processed_email_retention_days=90,
        ) == 0

    def test_zero_stays_zero(self):
        assert self._effective(
            email_retention_days=7, processed_email_retention_days=0,
        ) == 0

    def test_floor_warning_is_logged_once_not_every_tick(self, caplog):
        """``run_cleanup_checks`` runs every 60s; a static misconfiguration
        must not produce ~1440 identical lines a day."""
        from istota.scheduler import _effective_processed_email_retention, _warned_keys

        _warned_keys.clear()
        sched = SchedulerConfig(
            email_retention_days=30, processed_email_retention_days=7,
        )
        with caplog.at_level("WARNING", logger="istota.scheduler"):
            for _ in range(5):
                _effective_processed_email_retention(sched)

        hits = [r for r in caplog.records if "processed_email_retention_days" in r.message]
        assert len(hits) == 1


class TestCleanupChecksWiring:
    def test_run_cleanup_checks_prunes_processed_emails(self, db_path, tmp_path):
        from istota.scheduler import run_cleanup_checks

        config = Config(
            db_path=db_path,
            email=AppEmailConfig(enabled=False),
            scheduler=SchedulerConfig(processed_email_retention_days=90),
            temp_dir=tmp_path / "temp",
        )
        with db.get_db(db_path) as conn:
            row_id = db.mark_email_processed(
                conn, email_id="stale", sender_email="a@example.com",
            )
            conn.execute(
                "UPDATE processed_emails SET processed_at = datetime('now', '-400 days') "
                "WHERE id = ?", (row_id,),
            )

        run_cleanup_checks(config)

        with db.get_db(db_path) as conn:
            rows = conn.execute("SELECT COUNT(*) FROM processed_emails").fetchone()[0]
        assert rows == 0


@pytest.mark.parametrize("field,default", [
    ("email_retention_days", 7),
    ("processed_email_retention_days", 90),
])
def test_retention_defaults(field, default):
    assert getattr(SchedulerConfig(), field) == default
