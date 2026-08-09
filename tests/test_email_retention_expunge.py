"""The two things the ISSUE-230 retention fix left standing.

**Blast radius (targeted expunge).** ``delete_emails_before`` flagged its batch
``\\Deleted`` and then issued a *folder-wide* ``EXPUNGE``, which removes every
message in the folder carrying that flag — including ones another IMAP client
flagged and has not expunged yet. Pre-existing, but the old sweep almost never
reached a message, and this one reaches the whole expired set on every tick. A
server advertising ``UIDPLUS`` can expunge exactly the UIDs we deleted; one that
doesn't gets the folder-wide call and one warning saying so.

**Convergence (bounded sweep).** The sweep took the entire ``BEFORE`` result in
one pass under a single IMAP socket timeout, so a first-run backlog large enough
to trip it logged a failure on every cleanup tick while it slowly converged. It
is bounded per sweep instead: a tick does a completable amount of work and says
what is left, and the next tick (60s later) picks up the rest.
"""

import logging
from datetime import date
from unittest.mock import MagicMock, patch

from istota.config import Config, EmailConfig as AppEmailConfig
from istota.email_support import cleanup_old_emails
from istota.skills.email import (
    _MAX_DELETES_PER_SWEEP,
    EmailConfig,
    delete_emails_before,
)


def _cfg(**kw):
    base = dict(
        imap_host="imap.example.com", imap_port=993,
        imap_user="u", imap_password="p",
        smtp_host="s", smtp_port=587, bot_email="b@example.com",
    )
    base.update(kw)
    return EmailConfig(**base)


def _mailbox(uids, *, uidplus: bool, pre_auth_only: bool = False):
    """A mailbox double.

    ``pre_auth_only`` models the shape that actually ships: the capability
    tuple ``imaplib`` cached from the greeting lacks UIDPLUS, and only a live
    ``CAPABILITY`` command reports it. See ``TestCapabilityAcquisition``.
    """
    mailbox = MagicMock()
    mailbox.__enter__ = MagicMock(return_value=mailbox)
    mailbox.__exit__ = MagicMock(return_value=False)
    mailbox.uids.return_value = list(uids)

    advertised = ("IMAP4REV1", "UIDPLUS") if uidplus else ("IMAP4REV1", "IDLE")
    if pre_auth_only:
        mailbox.client.capabilities = ("IMAP4REV1",)
        mailbox.client.capability.return_value = ("OK", [" ".join(advertised).encode()])
    else:
        mailbox.client.capabilities = advertised
        mailbox.client.capability.return_value = ("NO", [b"not now"])

    mailbox.client.uid.return_value = ("OK", [b""])
    return mailbox


def _uid_calls(mailbox, command):
    """Every ``client.uid(<command>, …)`` call, in order."""
    return [
        call for call in mailbox.client.uid.call_args_list
        if call[0][0].upper() == command
    ]


class TestCapabilityAcquisition:
    """UIDPLUS has to be read from the *post-authentication* capability list.

    ``imaplib`` fills ``client.capabilities`` once, from the greeting, and
    nothing refreshes it: ``imaplib.login`` does not, and imap-tools' ``login``
    bypasses ``imaplib.login`` entirely (``_simple_command('LOGIN', …)`` plus a
    hand-set ``client.state``). RFC 3501 §7.2.1 lets the list change on
    authentication, and Dovecot and Gmail both use that — their pre-auth set
    has no UIDPLUS in it. Reading only the cached tuple therefore answers "no
    UIDPLUS" for most servers that have it, silently reverting every delete to
    a folder-wide EXPUNGE.
    """

    def test_uidplus_advertised_only_after_login_is_still_found(self):
        from istota.skills.email import _supports_uid_expunge

        mailbox = _mailbox([], uidplus=True, pre_auth_only=True)
        assert "UIDPLUS" not in mailbox.client.capabilities, "precondition"

        assert _supports_uid_expunge(mailbox, _cfg()) is True
        mailbox.client.capability.assert_called()

    def test_a_post_auth_only_server_gets_the_targeted_sweep(self):
        mailbox = _mailbox(["1", "2"], uidplus=True, pre_auth_only=True)

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            assert delete_emails_before(date(2026, 1, 1), config=_cfg()) == 2

        assert len(_uid_calls(mailbox, "EXPUNGE")) == 1
        mailbox.delete.assert_not_called()

    def test_a_server_advertising_it_only_pre_auth_is_believed(self):
        """The cached tuple is a promise the server already made."""
        from istota.skills.email import _supports_uid_expunge

        mailbox = _mailbox([], uidplus=False, pre_auth_only=False)
        mailbox.client.capabilities = ("IMAP4REV1", "UIDPLUS")

        assert _supports_uid_expunge(mailbox, _cfg()) is True

    def test_neither_list_naming_it_falls_back(self):
        from istota.skills.email import _supports_uid_expunge

        mailbox = _mailbox([], uidplus=False, pre_auth_only=True)
        assert _supports_uid_expunge(mailbox, _cfg()) is False

    def test_an_unreadable_capability_response_is_not_a_crash(self):
        from istota.skills.email import _supports_uid_expunge

        mailbox = _mailbox([], uidplus=False)
        mailbox.client.capabilities = None
        mailbox.client.capability.side_effect = OSError("connection reset")

        assert _supports_uid_expunge(mailbox, _cfg()) is False


class TestTargetedExpunge:
    """A UIDPLUS server expunges the swept UIDs and nothing else."""

    def test_uidplus_expunges_only_the_uids_this_sweep_deleted(self):
        mailbox = _mailbox(["11", "12", "13"], uidplus=True)

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            deleted = delete_emails_before(date(2026, 1, 1), config=_cfg())

        assert deleted == 3

        expunges = _uid_calls(mailbox, "EXPUNGE")
        assert len(expunges) == 1
        assert expunges[0][0][1] == "11,12,13"

        stores = _uid_calls(mailbox, "STORE")
        assert len(stores) == 1
        assert stores[0][0][1] == "11,12,13"
        assert stores[0][0][2] == "+FLAGS"
        assert "\\Deleted" in stores[0][0][3]

        # The two folder-wide calls are what this fix exists to avoid.
        mailbox.expunge.assert_not_called()
        mailbox.delete.assert_not_called()

    def test_each_batch_expunges_only_its_own_uids(self):
        uids = [str(i) for i in range(500)]
        mailbox = _mailbox(uids, uidplus=True)

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            deleted = delete_emails_before(
                date(2026, 1, 1), config=_cfg(), batch_size=200,
            )

        assert deleted == 500
        expunges = _uid_calls(mailbox, "EXPUNGE")
        assert len(expunges) == 3

        expunged = []
        for call in expunges:
            expunged.extend(call[0][1].split(","))
        assert expunged == uids, "batches must partition the search result"
        mailbox.expunge.assert_not_called()

    def test_no_matches_issues_no_commands_at_all(self):
        mailbox = _mailbox([], uidplus=True)

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            assert delete_emails_before(date(2026, 1, 1), config=_cfg()) == 0

        assert _uid_calls(mailbox, "STORE") == []
        assert _uid_calls(mailbox, "EXPUNGE") == []
        mailbox.delete.assert_not_called()


class TestFolderWideFallback:
    """No UIDPLUS: the old behaviour, plus one warning naming what it costs."""

    def test_server_without_uidplus_still_deletes(self):
        mailbox = _mailbox(["1", "2"], uidplus=False)

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            deleted = delete_emails_before(date(2026, 1, 1), config=_cfg())

        assert deleted == 2
        mailbox.delete.assert_called_once()
        assert list(mailbox.delete.call_args[0][0]) == ["1", "2"]
        assert _uid_calls(mailbox, "EXPUNGE") == []

    def test_fallback_warns_once_per_host_not_once_per_sweep(self, caplog):
        with caplog.at_level(logging.WARNING, logger="istota.skills.email"):
            for _ in range(3):
                mailbox = _mailbox(["1"], uidplus=False)
                with patch("istota.skills.email._get_mailbox", return_value=mailbox):
                    delete_emails_before(date(2026, 1, 1), config=_cfg())

        warnings = [
            r for r in caplog.records
            if r.levelno == logging.WARNING and "UIDPLUS" in r.getMessage()
        ]
        assert len(warnings) == 1, "a static server fact must not log every tick"
        assert "another client" in warnings[0].getMessage().lower()

    def test_a_refused_uid_expunge_does_not_fall_back_to_folder_wide(self):
        """Falling back would delete exactly what the targeted path protects."""
        mailbox = _mailbox(["1", "2"], uidplus=True)
        mailbox.client.uid.side_effect = lambda cmd, *a: (
            ("NO", [b"denied"]) if cmd.upper() == "EXPUNGE" else ("OK", [b""])
        )

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            deleted = delete_emails_before(date(2026, 1, 1), config=_cfg())

        assert deleted == 0
        mailbox.expunge.assert_not_called()
        mailbox.delete.assert_not_called()

    def test_the_warning_is_not_logged_when_there_is_nothing_to_delete(self, caplog):
        """Every install would otherwise report a fallback it never took."""
        mailbox = _mailbox([], uidplus=False)

        with caplog.at_level(logging.WARNING, logger="istota.skills.email"):
            with patch("istota.skills.email._get_mailbox", return_value=mailbox):
                assert delete_emails_before(date(2026, 1, 1), config=_cfg()) == 0

        assert not [r for r in caplog.records if "UIDPLUS" in r.getMessage()]


class TestDeletedFlagRollback:
    """A refused expunge has to be a genuine no-op, not a hidden mailbox."""

    def _store_ok_expunge_refused(self, mailbox):
        seen = []

        def _uid(cmd, *args):
            seen.append((cmd.upper(), args))
            if cmd.upper() == "EXPUNGE":
                return ("NO", [b"denied"])
            return ("OK", [b""])

        mailbox.client.uid.side_effect = _uid
        return seen

    def test_the_deleted_flag_is_rolled_back_when_the_expunge_is_refused(self):
        """The STORE already landed; leaving it hides the mail in most clients
        and arms it for the next plain EXPUNGE any client sends."""
        mailbox = _mailbox(["7", "8"], uidplus=True)
        seen = self._store_ok_expunge_refused(mailbox)

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            assert delete_emails_before(date(2026, 1, 1), config=_cfg()) == 0

        stores = [call for call in seen if call[0] == "STORE"]
        assert len(stores) == 2, "one to flag, one to roll back"
        assert stores[0][1] == ("7,8", "+FLAGS", r"(\Deleted)")
        assert stores[1][1] == ("7,8", "-FLAGS", r"(\Deleted)")

    def test_rollback_also_runs_for_the_single_message_delete(self):
        from istota.skills.email import delete_email

        mailbox = _mailbox([], uidplus=True)
        seen = self._store_ok_expunge_refused(mailbox)

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            assert delete_email("42", config=_cfg()) is False

        stores = [call for call in seen if call[0] == "STORE"]
        assert [c[1][1] for c in stores] == ["+FLAGS", "-FLAGS"]

    def test_a_failed_rollback_is_logged_and_the_original_error_wins(self, caplog):
        mailbox = _mailbox(["7"], uidplus=True)

        def _uid(cmd, *args):
            if cmd.upper() == "EXPUNGE":
                return ("NO", [b"denied"])
            if args[1] == "-FLAGS":
                raise OSError("connection reset")
            return ("OK", [b""])

        mailbox.client.uid.side_effect = _uid

        with caplog.at_level(logging.DEBUG, logger="istota.skills.email"):
            with patch("istota.skills.email._get_mailbox", return_value=mailbox):
                assert delete_emails_before(date(2026, 1, 1), config=_cfg()) == 0

        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "could not be rolled back" in messages


class TestUidValidation:
    """The raw path lost the shape check `mailbox.delete` gave it for free."""

    def test_a_uid_carrying_crlf_is_refused_before_it_reaches_the_wire(self):
        """`imaplib` concatenates str args into the command with no escaping,
        so an embedded CRLF would be a second IMAP command."""
        from istota.skills.email import delete_email

        mailbox = _mailbox([], uidplus=True)

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            assert delete_email("1\r\nX LOGOUT", config=_cfg()) is False

        assert _uid_calls(mailbox, "STORE") == []
        assert _uid_calls(mailbox, "EXPUNGE") == []

    def test_a_non_numeric_uid_is_refused(self):
        from istota.skills.email import delete_email

        mailbox = _mailbox([], uidplus=True)

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            assert delete_email("../../etc/passwd", config=_cfg()) is False

        assert _uid_calls(mailbox, "STORE") == []

    def test_an_empty_batch_sends_no_command(self):
        """`UID STORE  +FLAGS (\\Deleted)` with an empty set is a BAD reply."""
        from istota.skills.email import _delete_uid_batch

        mailbox = _mailbox([], uidplus=True)
        _delete_uid_batch(mailbox, [], targeted=True)
        _delete_uid_batch(mailbox, [], targeted=False)

        assert mailbox.client.uid.call_args_list == []
        mailbox.delete.assert_not_called()


class TestSingleMessageDelete:
    """``delete_email`` is the agent-reachable verb and shares the hazard."""

    def test_deleting_one_message_expunges_only_that_uid(self):
        from istota.skills.email import delete_email

        mailbox = _mailbox([], uidplus=True)

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            assert delete_email("42", config=_cfg()) is True

        expunges = _uid_calls(mailbox, "EXPUNGE")
        assert len(expunges) == 1
        assert expunges[0][0][1] == "42"
        mailbox.expunge.assert_not_called()
        mailbox.delete.assert_not_called()

    def test_falls_back_on_a_server_without_uidplus(self):
        from istota.skills.email import delete_email

        mailbox = _mailbox([], uidplus=False)

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            assert delete_email("42", config=_cfg()) is True

        mailbox.delete.assert_called_once_with(["42"])

    def test_a_refused_expunge_reports_failure(self):
        from istota.skills.email import delete_email

        mailbox = _mailbox([], uidplus=True)
        mailbox.client.uid.side_effect = lambda cmd, *a: (
            ("NO", [b"denied"]) if cmd.upper() == "EXPUNGE" else ("OK", [b""])
        )

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            assert delete_email("42", config=_cfg()) is False


class TestBoundedSweep:
    """A backlog drains over several ticks instead of failing on each one."""

    def test_sweep_stops_at_the_bound_and_takes_the_oldest_first(self):
        uids = [str(i) for i in range(5000)]
        mailbox = _mailbox(uids, uidplus=True)

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            deleted = delete_emails_before(
                date(2026, 1, 1), config=_cfg(), batch_size=200, max_deletes=2000,
            )

        assert deleted == 2000
        touched = []
        for call in _uid_calls(mailbox, "EXPUNGE"):
            touched.extend(call[0][1].split(","))
        assert touched == uids[:2000], "SEARCH returns ascending UIDs; oldest first"

    def test_the_bound_takes_the_oldest_even_if_search_returns_unsorted(self):
        """RFC 3501 does not specify SEARCH result ordering, and the UIDs are
        strings — so a plain `sorted()` would order "10" before "9"."""
        mailbox = _mailbox(["30", "4", "100", "9", "21"], uidplus=True)

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            deleted = delete_emails_before(
                date(2026, 1, 1), config=_cfg(), max_deletes=3,
            )

        assert deleted == 3
        touched = []
        for call in _uid_calls(mailbox, "EXPUNGE"):
            touched.extend(call[0][1].split(","))
        assert touched == ["4", "9", "21"]

    def test_bound_of_zero_sweeps_everything(self):
        uids = [str(i) for i in range(5000)]
        mailbox = _mailbox(uids, uidplus=True)

        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            deleted = delete_emails_before(
                date(2026, 1, 1), config=_cfg(), batch_size=200, max_deletes=0,
            )

        assert deleted == 5000

    def test_a_bounded_sweep_reports_the_remainder_without_an_error(self, caplog):
        mailbox = _mailbox([str(i) for i in range(5000)], uidplus=True)

        with caplog.at_level(logging.INFO, logger="istota.skills.email"):
            with patch("istota.skills.email._get_mailbox", return_value=mailbox):
                delete_emails_before(
                    date(2026, 1, 1), config=_cfg(), batch_size=200, max_deletes=2000,
                )

        assert not [r for r in caplog.records if r.levelno >= logging.ERROR], (
            "a converging drain is not a failure"
        )
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "3000" in messages and "remain" in messages.lower()

    def test_a_completed_sweep_reports_no_remainder(self, caplog):
        mailbox = _mailbox(["1", "2", "3"], uidplus=True)

        with caplog.at_level(logging.INFO, logger="istota.skills.email"):
            with patch("istota.skills.email._get_mailbox", return_value=mailbox):
                delete_emails_before(date(2026, 1, 1), config=_cfg())

        messages = " ".join(r.getMessage() for r in caplog.records).lower()
        assert "remain" not in messages


class TestPartialFailureReporting:
    """A stopped sweep is only an error when it achieved nothing."""

    def _failing_after(self, mailbox, ok_expunges: int):
        calls = {"expunge": 0}

        def _uid(cmd, *args):
            if cmd.upper() != "EXPUNGE":
                return ("OK", [b""])
            calls["expunge"] += 1
            if calls["expunge"] > ok_expunges:
                raise TimeoutError("socket timeout")
            return ("OK", [b""])

        mailbox.client.uid.side_effect = _uid

    def test_progress_then_failure_warns_because_the_next_tick_resumes(self, caplog):
        mailbox = _mailbox([str(i) for i in range(600)], uidplus=True)
        self._failing_after(mailbox, ok_expunges=1)

        with caplog.at_level(logging.DEBUG, logger="istota.skills.email"):
            with patch("istota.skills.email._get_mailbox", return_value=mailbox):
                deleted = delete_emails_before(
                    date(2026, 1, 1), config=_cfg(), batch_size=200,
                )

        assert deleted == 200
        assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings, "a stopped sweep still has to be visible"
        assert "200" in warnings[0].getMessage()

    def test_failing_on_the_first_batch_is_an_error(self, caplog):
        mailbox = _mailbox([str(i) for i in range(600)], uidplus=True)
        self._failing_after(mailbox, ok_expunges=0)

        with caplog.at_level(logging.DEBUG, logger="istota.skills.email"):
            with patch("istota.skills.email._get_mailbox", return_value=mailbox):
                deleted = delete_emails_before(
                    date(2026, 1, 1), config=_cfg(), batch_size=200,
                )

        assert deleted == 0
        assert [r for r in caplog.records if r.levelno >= logging.ERROR], (
            "a sweep that removed nothing is not converging"
        )


class TestCleanupWiring:
    """The scheduler's entry point bounds its sweep."""

    def test_cleanup_old_emails_passes_the_sweep_bound(self):
        config = Config(email=AppEmailConfig(
            enabled=True,
            imap_host="imap.example.com", imap_port=993,
            imap_user="bot@example.com", imap_password="pw",
            smtp_host="smtp.example.com", smtp_port=587,
            bot_email="bot@example.com", poll_folder="INBOX",
        ))

        with patch(
            "istota.email_support.delete_emails_before", return_value=7,
        ) as mock_delete:
            assert cleanup_old_emails(config, days=7) == 7

        assert mock_delete.call_args[1]["max_deletes"] == _MAX_DELETES_PER_SWEEP
        assert _MAX_DELETES_PER_SWEEP > 0, "an unbounded default defeats the fix"
