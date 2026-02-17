"""Tests for invoice_scheduler.py module."""

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota import db
from istota.config import (
    BriefingConfig,
    Config,
    EmailConfig,
    NextcloudConfig,
    ResourceConfig,
    UserConfig,
)
from istota.invoice_scheduler import (
    _already_ran_this_month,
    _check_overdue_invoices,
    _is_due_this_month,
    _resolve_conversation_token,
    _resolve_invoicing_path,
    _resolve_ledger_path,
    _resolve_notification_surface,
    _resolve_overdue_days,
    _send_notification,
    check_scheduled_invoices,
)


SAMPLE_INVOICING_MD = """\
# Invoicing

```toml
accounting_path = "/accounting"
work_log = "/notes/_INVOICES.md"
invoice_output = "invoices/generated"
next_invoice_number = 1
notifications = "email"

[company]
name = "TestCo"
address = "123 Main St"

[clients.acme]
name = "Acme Corp"
address = "456 Oak Ave"
terms = 30

[clients.acme.invoicing]
schedule = "monthly"
day = 15
reminder_days = 3
notifications = "talk"

[clients.beta]
name = "Beta Inc"
terms = 15

[clients.beta.invoicing]
schedule = "monthly"
day = 1
reminder_days = 0

[clients.gamma]
name = "Gamma Ltd"

[services.consulting]
display_name = "Consulting"
rate = 150
type = "hours"
```
"""


class TestResolveNotificationSurface:
    def test_client_override_wins(self):
        assert _resolve_notification_surface("email", "talk", "both") == "email"

    def test_config_level_fallback(self):
        assert _resolve_notification_surface("", "email", "both") == "email"

    def test_user_level_fallback(self):
        assert _resolve_notification_surface("", "", "both") == "both"

    def test_default_is_talk(self):
        assert _resolve_notification_surface("", "", "") == "talk"


class TestResolveInvoicingPath:
    def test_returns_none_without_mount(self, tmp_path):
        config = Config(nextcloud_mount_path=None, users={"alice": UserConfig()})
        assert _resolve_invoicing_path(config, "alice") is None

    def test_returns_none_for_unknown_user(self, tmp_path):
        config = Config(nextcloud_mount_path=tmp_path, users={})
        assert _resolve_invoicing_path(config, "bob") is None

    def test_uses_istota_config_default(self, tmp_path):
        istota_config_file = tmp_path / "Users" / "alice" / "istota" / "config" / "INVOICING.md"
        istota_config_file.parent.mkdir(parents=True)
        istota_config_file.write_text("test")

        user = UserConfig()
        config = Config(nextcloud_mount_path=tmp_path, users={"alice": user})
        assert _resolve_invoicing_path(config, "alice") == istota_config_file

    def test_returns_none_if_file_missing(self, tmp_path):
        user = UserConfig()
        config = Config(nextcloud_mount_path=tmp_path, users={"alice": user})
        assert _resolve_invoicing_path(config, "alice") is None


class TestResolveLedgerPath:
    def test_returns_first_ledger_resource(self, tmp_path):
        ledger = tmp_path / "Users" / "alice" / "ledger.beancount"
        ledger.parent.mkdir(parents=True)
        ledger.write_text("")

        user = UserConfig(resources=[
            ResourceConfig(type="ledger", path="/Users/alice/ledger.beancount"),
        ])
        config = Config(nextcloud_mount_path=tmp_path, users={"alice": user})
        assert _resolve_ledger_path(config, "alice") == str(ledger)

    def test_returns_none_without_mount(self):
        config = Config(nextcloud_mount_path=None, users={"alice": UserConfig()})
        assert _resolve_ledger_path(config, "alice") is None

    def test_returns_none_without_ledger_resource(self, tmp_path):
        user = UserConfig()
        config = Config(nextcloud_mount_path=tmp_path, users={"alice": user})
        assert _resolve_ledger_path(config, "alice") is None


class TestResolveConversationToken:
    def test_invoicing_token_takes_precedence(self, tmp_path):
        user = UserConfig(
            invoicing_conversation_token="invoice-room",
            briefings=[BriefingConfig(name="morning", cron="0 7 * * *", conversation_token="brief-room")],
        )
        config = Config(nextcloud_mount_path=tmp_path, users={"alice": user})
        assert _resolve_conversation_token(config, "alice") == "invoice-room"

    def test_falls_back_to_briefing_token(self, tmp_path):
        user = UserConfig(
            briefings=[BriefingConfig(name="morning", cron="0 7 * * *", conversation_token="brief-room")],
        )
        config = Config(nextcloud_mount_path=tmp_path, users={"alice": user})
        assert _resolve_conversation_token(config, "alice") == "brief-room"

    def test_returns_none_without_any_token(self, tmp_path):
        user = UserConfig()
        config = Config(nextcloud_mount_path=tmp_path, users={"alice": user})
        assert _resolve_conversation_token(config, "alice") is None


class TestIsDueThisMonth:
    def test_due_when_day_passed_and_never_run(self):
        now = datetime(2026, 2, 15, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
        assert _is_due_this_month(None, 10, now) is True

    def test_not_due_when_day_not_reached(self):
        now = datetime(2026, 2, 5, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
        assert _is_due_this_month(None, 10, now) is False

    def test_not_due_when_already_ran_this_month(self):
        now = datetime(2026, 2, 15, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
        last_run = "2026-02-10T08:00:00"
        assert _is_due_this_month(last_run, 10, now) is False

    def test_due_when_ran_last_month(self):
        now = datetime(2026, 2, 15, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
        last_run = "2026-01-15T08:00:00"
        assert _is_due_this_month(last_run, 10, now) is True

    def test_due_on_exact_day(self):
        now = datetime(2026, 2, 10, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
        assert _is_due_this_month(None, 10, now) is True

    def test_timezone_aware_last_run(self):
        now = datetime(2026, 2, 15, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("US/Eastern"))
        # Last run was Feb 10 UTC — still Feb in Eastern
        last_run = "2026-02-10T08:00:00"
        assert _is_due_this_month(last_run, 10, now) is False

    def test_not_due_before_target_day(self):
        # Day 5, target is 10 → not due yet
        tz = __import__("zoneinfo").ZoneInfo("UTC")
        now = datetime(2026, 2, 5, 10, 0, tzinfo=tz)
        assert _is_due_this_month(None, 10, now) is False

    def test_day_1_schedule_works(self):
        # Day 1 target, and it's the 1st → due
        tz = __import__("zoneinfo").ZoneInfo("UTC")
        now = datetime(2026, 2, 1, 10, 0, tzinfo=tz)
        assert _is_due_this_month(None, 1, now) is True


class TestSendNotification:
    @patch("istota.notifications._send_talk")
    def test_talk_surface(self, mock_talk, tmp_path):
        mock_talk.return_value = True
        config = Config(nextcloud_mount_path=tmp_path, users={"alice": UserConfig()})
        result = _send_notification(config, "alice", "test msg", "subject", "talk")
        assert result is True
        mock_talk.assert_called_once()

    @patch("istota.notifications._send_email")
    def test_email_surface(self, mock_email, tmp_path):
        mock_email.return_value = True
        config = Config(nextcloud_mount_path=tmp_path, users={"alice": UserConfig()})
        result = _send_notification(config, "alice", "test msg", "subject", "email")
        assert result is True
        mock_email.assert_called_once()

    @patch("istota.notifications._send_email")
    @patch("istota.notifications._send_talk")
    def test_both_surface(self, mock_talk, mock_email, tmp_path):
        mock_talk.return_value = True
        mock_email.return_value = True
        config = Config(nextcloud_mount_path=tmp_path, users={"alice": UserConfig()})
        result = _send_notification(config, "alice", "test msg", "subject", "both")
        assert result is True
        mock_talk.assert_called_once()
        mock_email.assert_called_once()


class TestCheckScheduledInvoices:
    def _make_config(self, tmp_path, invoicing_content=None):
        """Helper to create a Config with invoicing set up."""
        mount = tmp_path / "mount"
        mount.mkdir()

        invoicing_path = mount / "Users" / "alice" / "istota" / "config" / "INVOICING.md"
        invoicing_path.parent.mkdir(parents=True)
        invoicing_path.write_text(invoicing_content or SAMPLE_INVOICING_MD)

        user = UserConfig(
            timezone="UTC",
            invoicing_notifications="talk",
            invoicing_conversation_token="room123",
        )
        db_path = tmp_path / "test.db"
        db.init_db(db_path)

        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            users={"alice": user},
        )
        return config

    @patch("istota.invoice_scheduler._send_notification")
    def test_sends_reminder_when_due(self, mock_notify, tmp_path):
        config = self._make_config(tmp_path)
        mock_notify.return_value = True

        # Set "now" to Feb 12 — reminder_days=3, day=15 → reminder on 12th
        with patch("istota.invoice_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 12, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            with db.get_db(config.db_path) as conn:
                results = check_scheduled_invoices(conn, config)

        assert results["reminders_sent"] >= 1
        # acme has reminder_days=3, day=15 → reminder due on day 12
        assert mock_notify.called

    @patch("istota.invoice_scheduler._send_notification")
    @patch("istota.skills.invoicing.generate_invoices_for_period")
    def test_generates_invoices_when_due(self, mock_gen, mock_notify, tmp_path):
        config = self._make_config(tmp_path)
        mock_notify.return_value = True
        mock_gen.return_value = [
            {"invoice_number": "INV-000001", "client": "Acme Corp", "total": 1500.00},
        ]

        # Set "now" to Feb 15 — day=15 for acme, day=1 for beta (also due)
        with patch("istota.invoice_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 15, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            with db.get_db(config.db_path) as conn:
                results = check_scheduled_invoices(conn, config)

        # Both acme (day=15) and beta (day=1) are due on Feb 15
        assert results["invoices_generated"] == 2
        assert mock_gen.call_count == 2

    @patch("istota.invoice_scheduler._send_notification")
    def test_skips_non_monthly_clients(self, mock_notify, tmp_path):
        config = self._make_config(tmp_path)

        # gamma has no schedule set (defaults to on-demand)
        with patch("istota.invoice_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 15, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            with db.get_db(config.db_path) as conn:
                results = check_scheduled_invoices(conn, config)

        # gamma should not trigger anything (on-demand)
        # The notification calls should only be for acme and beta
        for call in mock_notify.call_args_list:
            msg = call[0][2]  # message argument
            assert "Gamma" not in msg

    @patch("istota.invoice_scheduler._send_notification")
    @patch("istota.skills.invoicing.generate_invoices_for_period")
    def test_skips_already_generated_this_month(self, mock_gen, mock_notify, tmp_path):
        config = self._make_config(tmp_path)
        mock_notify.return_value = True

        with db.get_db(config.db_path) as conn:
            # Pre-set generation state for acme this month
            db.set_invoice_schedule_generation(conn, "alice", "acme")

        with patch("istota.invoice_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 15, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            with db.get_db(config.db_path) as conn:
                results = check_scheduled_invoices(conn, config)

        # acme should have been skipped (already generated)
        # but generation should still record for beta (day=1, already past)
        mock_gen.assert_called()
        # The call should be for beta, not acme
        call_kwargs = mock_gen.call_args
        assert call_kwargs[1].get("client_filter") == "beta" or call_kwargs.kwargs.get("client_filter") == "beta"

    @patch("istota.invoice_scheduler._send_notification")
    def test_no_reminder_when_reminder_days_zero(self, mock_notify, tmp_path):
        """Beta has reminder_days=0, so it should never get reminders."""
        config = self._make_config(tmp_path)
        mock_notify.return_value = True

        # Set to Feb 1 — beta's schedule_day. No reminder for beta (reminder_days=0).
        # Acme's reminder_day=12 is not yet reached.
        with patch("istota.invoice_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 1, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            with db.get_db(config.db_path) as conn:
                results = check_scheduled_invoices(conn, config)

        # No reminders: beta has reminder_days=0, acme's reminder_day=12 not yet
        assert results["reminders_sent"] == 0
        # Verify no "Reminder" notifications were sent for beta
        for call in mock_notify.call_args_list:
            msg = call[0][2]
            if "Beta" in msg:
                assert "Reminder" not in msg

    @patch("istota.invoice_scheduler._send_notification")
    @patch("istota.skills.invoicing.generate_invoices_for_period")
    def test_generation_with_no_entries_still_records_state(self, mock_gen, mock_notify, tmp_path):
        config = self._make_config(tmp_path)
        mock_gen.return_value = []  # No uninvoiced entries

        with patch("istota.invoice_scheduler.datetime") as mock_dt:
            mock_dt.now.return_value = datetime(2026, 2, 15, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            with db.get_db(config.db_path) as conn:
                results = check_scheduled_invoices(conn, config)

                # State should be recorded even with 0 invoices (to prevent re-running)
                state = db.get_invoice_schedule_state(conn, "alice", "acme")
                assert state is not None
                assert state.last_generation_at is not None

        assert results["invoices_generated"] == 0

    @patch("istota.invoice_scheduler._send_notification")
    def test_notification_surface_resolution(self, mock_notify, tmp_path):
        """Test that client-level notification override is used."""
        config = self._make_config(tmp_path)
        mock_notify.return_value = True

        with patch("istota.invoice_scheduler.datetime") as mock_dt:
            # Set to day 12 — acme reminder day
            mock_dt.now.return_value = datetime(2026, 2, 12, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            with db.get_db(config.db_path) as conn:
                check_scheduled_invoices(conn, config)

        # Check that the acme notification used "talk" (client override)
        for call in mock_notify.call_args_list:
            msg = call[0][2]
            surface = call[0][4]
            if "Acme" in msg:
                assert surface == "talk"  # client override

    @patch("istota.invoice_scheduler._send_notification")
    @patch("istota.skills.invoicing.generate_invoices_for_period")
    def test_no_reminder_after_generation(self, mock_gen, mock_notify, tmp_path):
        """Once generation has run, don't send reminder for same month."""
        config = self._make_config(tmp_path)
        mock_notify.return_value = True
        mock_gen.return_value = [{"invoice_number": "INV-000001", "client": "Acme Corp", "total": 100.0}]

        with db.get_db(config.db_path) as conn:
            # Simulate generation already happened on the 15th
            db.set_invoice_schedule_generation(conn, "alice", "acme")

        with patch("istota.invoice_scheduler.datetime") as mock_dt:
            # Now it's the 16th — past both reminder and generation day
            mock_dt.now.return_value = datetime(2026, 2, 16, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))
            mock_dt.fromisoformat = datetime.fromisoformat
            mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)

            with db.get_db(config.db_path) as conn:
                results = check_scheduled_invoices(conn, config)

        # No reminder for acme since generation already happened
        for call in mock_notify.call_args_list:
            msg = call[0][2]
            if "Acme" in msg:
                assert "Reminder" not in msg


class TestDBInvoiceScheduleState:
    def test_get_nonexistent_returns_none(self, db_conn):
        state = db.get_invoice_schedule_state(db_conn, "alice", "acme")
        assert state is None

    def test_set_and_get_reminder(self, db_conn):
        db.set_invoice_schedule_reminder(db_conn, "alice", "acme")
        state = db.get_invoice_schedule_state(db_conn, "alice", "acme")
        assert state is not None
        assert state.last_reminder_at is not None
        assert state.last_generation_at is None

    def test_set_and_get_generation(self, db_conn):
        db.set_invoice_schedule_generation(db_conn, "alice", "acme")
        state = db.get_invoice_schedule_state(db_conn, "alice", "acme")
        assert state is not None
        assert state.last_generation_at is not None
        assert state.last_reminder_at is None

    def test_both_timestamps(self, db_conn):
        db.set_invoice_schedule_reminder(db_conn, "alice", "acme")
        db.set_invoice_schedule_generation(db_conn, "alice", "acme")
        state = db.get_invoice_schedule_state(db_conn, "alice", "acme")
        assert state.last_reminder_at is not None
        assert state.last_generation_at is not None

    def test_multiple_clients(self, db_conn):
        db.set_invoice_schedule_reminder(db_conn, "alice", "acme")
        db.set_invoice_schedule_generation(db_conn, "alice", "beta")
        acme = db.get_invoice_schedule_state(db_conn, "alice", "acme")
        beta = db.get_invoice_schedule_state(db_conn, "alice", "beta")
        assert acme.last_reminder_at is not None
        assert acme.last_generation_at is None
        assert beta.last_generation_at is not None
        assert beta.last_reminder_at is None


class TestOverdueDBFunctions:
    def test_get_empty_returns_empty_set(self, db_conn):
        result = db.get_notified_overdue_invoices(db_conn, "alice")
        assert result == set()

    def test_mark_and_get(self, db_conn):
        db.mark_invoice_overdue_notified(db_conn, "alice", "INV-000001")
        db.mark_invoice_overdue_notified(db_conn, "alice", "INV-000002")
        result = db.get_notified_overdue_invoices(db_conn, "alice")
        assert result == {"INV-000001", "INV-000002"}

    def test_mark_duplicate_ignores(self, db_conn):
        db.mark_invoice_overdue_notified(db_conn, "alice", "INV-000001")
        db.mark_invoice_overdue_notified(db_conn, "alice", "INV-000001")
        result = db.get_notified_overdue_invoices(db_conn, "alice")
        assert result == {"INV-000001"}

    def test_clear_removes_record(self, db_conn):
        db.mark_invoice_overdue_notified(db_conn, "alice", "INV-000001")
        db.clear_overdue_notification(db_conn, "alice", "INV-000001")
        result = db.get_notified_overdue_invoices(db_conn, "alice")
        assert result == set()

    def test_user_isolation(self, db_conn):
        db.mark_invoice_overdue_notified(db_conn, "alice", "INV-000001")
        db.mark_invoice_overdue_notified(db_conn, "bob", "INV-000002")
        assert db.get_notified_overdue_invoices(db_conn, "alice") == {"INV-000001"}
        assert db.get_notified_overdue_invoices(db_conn, "bob") == {"INV-000002"}


OVERDUE_INVOICING_MD = """\
# Invoicing

```toml
accounting_path = "/accounting"
work_log = "/notes/_INVOICES.md"
invoice_output = "invoices/generated"
next_invoice_number = 1
days_until_overdue = 30

[company]
name = "TestCo"
address = "123 Main St"

[clients.acme]
name = "Acme Corp"
address = "456 Oak Ave"
terms = 30

[clients.acme.invoicing]
schedule = "on-demand"

[clients.beta]
name = "Beta Inc"
terms = 15

[clients.beta.invoicing]
schedule = "on-demand"

[services.consulting]
display_name = "Consulting"
rate = 150
type = "hours"
```
"""

OVERDUE_WORK_LOG = """\
# Work Log

```toml
[[entries]]
date = 2025-12-15
client = "acme"
service = "consulting"
qty = 10
invoice = "INV-000001"

[[entries]]
date = 2025-12-20
client = "beta"
service = "consulting"
qty = 5
invoice = "INV-000002"

[[entries]]
date = 2026-01-25
client = "acme"
service = "consulting"
qty = 8
invoice = "INV-000003"
```
"""


class TestOverdueInvoiceDetection:
    def _make_config(self, tmp_path, invoicing_content=None, work_log_content=None):
        """Helper to create Config with overdue detection set up."""
        mount = tmp_path / "mount"
        mount.mkdir()

        invoicing_path = mount / "Users" / "alice" / "istota" / "config" / "INVOICING.md"
        invoicing_path.parent.mkdir(parents=True)
        invoicing_path.write_text(invoicing_content or OVERDUE_INVOICING_MD)

        work_log_path = mount / "notes" / "_INVOICES.md"
        work_log_path.parent.mkdir(parents=True)
        work_log_path.write_text(work_log_content or OVERDUE_WORK_LOG)

        user = UserConfig(
            timezone="UTC",
            invoicing_notifications="talk",
            invoicing_conversation_token="room123",
        )
        db_path = tmp_path / "test.db"
        db.init_db(db_path)

        config = Config(
            db_path=db_path,
            nextcloud_mount_path=mount,
            users={"alice": user},
        )
        return config

    @patch("istota.invoice_scheduler._send_notification")
    def test_detects_overdue_invoice(self, mock_notify, tmp_path):
        """Invoice 40 days old with 30-day threshold should be detected."""
        config = self._make_config(tmp_path)
        mock_notify.return_value = True

        from istota.skills.invoicing import parse_invoicing_config, parse_work_log

        invoicing_path = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "INVOICING.md"
        invoicing_config = parse_invoicing_config(invoicing_path)

        work_log_path = config.nextcloud_mount_path / "notes" / "_INVOICES.md"
        work_entries = parse_work_log(work_log_path)

        # now = Feb 5, 2026. INV-000001 invoice_date=Dec 15 → 52 days, overdue by 22
        # INV-000002 invoice_date=Dec 20 → 47 days, overdue by 17
        # INV-000003 invoice_date=Jan 25 → 11 days, not overdue
        now = datetime(2026, 2, 5, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))

        with db.get_db(config.db_path) as conn:
            count = _check_overdue_invoices(
                conn, config, "alice", config.users["alice"],
                invoicing_config, work_entries, now,
            )

        assert count == 2
        mock_notify.assert_called_once()
        msg = mock_notify.call_args[0][2]
        assert "INV-000001" in msg
        assert "INV-000002" in msg
        assert "INV-000003" not in msg

    @patch("istota.invoice_scheduler._send_notification")
    def test_no_notification_when_not_overdue(self, mock_notify, tmp_path):
        """Invoice 20 days old with 30-day threshold should not be detected."""
        config = self._make_config(tmp_path)

        from istota.skills.invoicing import parse_invoicing_config, parse_work_log

        invoicing_path = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "INVOICING.md"
        invoicing_config = parse_invoicing_config(invoicing_path)

        work_log_path = config.nextcloud_mount_path / "notes" / "_INVOICES.md"
        work_entries = parse_work_log(work_log_path)

        # now = Jan 5, 2026. INV-000001 invoice_date=Dec 15 → 21 days, not overdue (30-day threshold)
        now = datetime(2026, 1, 5, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))

        with db.get_db(config.db_path) as conn:
            count = _check_overdue_invoices(
                conn, config, "alice", config.users["alice"],
                invoicing_config, work_entries, now,
            )

        assert count == 0
        mock_notify.assert_not_called()

    @patch("istota.invoice_scheduler._send_notification")
    def test_no_duplicate_notifications(self, mock_notify, tmp_path):
        """Second check should not re-notify for same invoice."""
        config = self._make_config(tmp_path)
        mock_notify.return_value = True

        from istota.skills.invoicing import parse_invoicing_config, parse_work_log

        invoicing_path = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "INVOICING.md"
        invoicing_config = parse_invoicing_config(invoicing_path)

        work_log_path = config.nextcloud_mount_path / "notes" / "_INVOICES.md"
        work_entries = parse_work_log(work_log_path)

        now = datetime(2026, 2, 5, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))

        with db.get_db(config.db_path) as conn:
            count1 = _check_overdue_invoices(
                conn, config, "alice", config.users["alice"],
                invoicing_config, work_entries, now,
            )
            count2 = _check_overdue_invoices(
                conn, config, "alice", config.users["alice"],
                invoicing_config, work_entries, now,
            )

        assert count1 == 2
        assert count2 == 0
        assert mock_notify.call_count == 1

    @patch("istota.invoice_scheduler._send_notification")
    def test_client_level_override(self, mock_notify, tmp_path):
        """Client with 15-day override should detect sooner than 30-day global."""
        content = """\
# Invoicing

```toml
accounting_path = "/accounting"
work_log = "/notes/_INVOICES.md"
invoice_output = "invoices/generated"
next_invoice_number = 1
days_until_overdue = 30

[company]
name = "TestCo"

[clients.acme]
name = "Acme Corp"

[clients.acme.invoicing]
days_until_overdue = 15

[services.consulting]
display_name = "Consulting"
rate = 150
type = "hours"
```
"""
        work_log = """\
# Work Log

```toml
[[entries]]
date = 2026-01-10
client = "acme"
service = "consulting"
qty = 5
invoice = "INV-000001"
```
"""
        config = self._make_config(tmp_path, invoicing_content=content, work_log_content=work_log)
        mock_notify.return_value = True

        from istota.skills.invoicing import parse_invoicing_config, parse_work_log

        invoicing_path = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "INVOICING.md"
        invoicing_config = parse_invoicing_config(invoicing_path)

        work_log_path = config.nextcloud_mount_path / "notes" / "_INVOICES.md"
        work_entries = parse_work_log(work_log_path)

        # now = Jan 30. Invoice date = Jan 10. 20 days old.
        # Global = 30 (not overdue), but client override = 15 → overdue by 5 days
        now = datetime(2026, 1, 30, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))

        with db.get_db(config.db_path) as conn:
            count = _check_overdue_invoices(
                conn, config, "alice", config.users["alice"],
                invoicing_config, work_entries, now,
            )

        assert count == 1
        msg = mock_notify.call_args[0][2]
        assert "5 days overdue" in msg

    @patch("istota.invoice_scheduler._send_notification")
    def test_ignores_paid_invoices(self, mock_notify, tmp_path):
        """Entries with paid_date should not trigger overdue detection."""
        work_log = """\
# Work Log

```toml
[[entries]]
date = 2025-12-01
client = "acme"
service = "consulting"
qty = 10
invoice = "INV-000001"
paid_date = 2026-01-15
```
"""
        config = self._make_config(tmp_path, work_log_content=work_log)

        from istota.skills.invoicing import parse_invoicing_config, parse_work_log

        invoicing_path = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "INVOICING.md"
        invoicing_config = parse_invoicing_config(invoicing_path)

        work_log_path = config.nextcloud_mount_path / "notes" / "_INVOICES.md"
        work_entries = parse_work_log(work_log_path)

        now = datetime(2026, 2, 5, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))

        with db.get_db(config.db_path) as conn:
            count = _check_overdue_invoices(
                conn, config, "alice", config.users["alice"],
                invoicing_config, work_entries, now,
            )

        assert count == 0
        mock_notify.assert_not_called()

    @patch("istota.invoice_scheduler._send_notification")
    def test_disabled_when_zero(self, mock_notify, tmp_path):
        """days_until_overdue=0 at both levels should disable detection."""
        content = """\
# Invoicing

```toml
accounting_path = "/accounting"
work_log = "/notes/_INVOICES.md"
invoice_output = "invoices/generated"
next_invoice_number = 1
days_until_overdue = 0

[company]
name = "TestCo"

[clients.acme]
name = "Acme Corp"

[services.consulting]
display_name = "Consulting"
rate = 150
type = "hours"
```
"""
        work_log = """\
# Work Log

```toml
[[entries]]
date = 2025-11-01
client = "acme"
service = "consulting"
qty = 10
invoice = "INV-000001"
```
"""
        config = self._make_config(tmp_path, invoicing_content=content, work_log_content=work_log)

        from istota.skills.invoicing import parse_invoicing_config, parse_work_log

        invoicing_path = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "INVOICING.md"
        invoicing_config = parse_invoicing_config(invoicing_path)

        work_log_path = config.nextcloud_mount_path / "notes" / "_INVOICES.md"
        work_entries = parse_work_log(work_log_path)

        now = datetime(2026, 2, 5, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))

        with db.get_db(config.db_path) as conn:
            count = _check_overdue_invoices(
                conn, config, "alice", config.users["alice"],
                invoicing_config, work_entries, now,
            )

        assert count == 0
        mock_notify.assert_not_called()

    @patch("istota.invoice_scheduler._send_notification")
    def test_multiple_overdue_invoices_consolidated(self, mock_notify, tmp_path):
        """Multiple overdue invoices should produce a single notification."""
        config = self._make_config(tmp_path)
        mock_notify.return_value = True

        from istota.skills.invoicing import parse_invoicing_config, parse_work_log

        invoicing_path = config.nextcloud_mount_path / "Users" / "alice" / "istota" / "config" / "INVOICING.md"
        invoicing_config = parse_invoicing_config(invoicing_path)

        work_log_path = config.nextcloud_mount_path / "notes" / "_INVOICES.md"
        work_entries = parse_work_log(work_log_path)

        # Both INV-000001 and INV-000002 should be overdue
        now = datetime(2026, 2, 5, 10, 0, tzinfo=__import__("zoneinfo").ZoneInfo("UTC"))

        with db.get_db(config.db_path) as conn:
            count = _check_overdue_invoices(
                conn, config, "alice", config.users["alice"],
                invoicing_config, work_entries, now,
            )

        assert count == 2
        # Single consolidated notification
        assert mock_notify.call_count == 1
        msg = mock_notify.call_args[0][2]
        assert "**Overdue Invoices**" in msg
        assert "INV-000001" in msg
        assert "INV-000002" in msg


class TestResolveOverdueDays:
    def test_client_override_wins(self):
        client = MagicMock(days_until_overdue=15)
        config = MagicMock(days_until_overdue=30)
        assert _resolve_overdue_days(client, config) == 15

    def test_falls_back_to_global(self):
        client = MagicMock(days_until_overdue=0)
        config = MagicMock(days_until_overdue=30)
        assert _resolve_overdue_days(client, config) == 30

    def test_both_zero_returns_zero(self):
        client = MagicMock(days_until_overdue=0)
        config = MagicMock(days_until_overdue=0)
        assert _resolve_overdue_days(client, config) == 0
