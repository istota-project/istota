"""The outbound drafts store.

`release` is the interesting half: it is the only path that touches SMTP, it
must be idempotent under a double approve, and it must never mark a row `sent`
that did not go out — a draft wrongly marked sent is a message the user believes
they delivered.
"""

from unittest.mock import patch

import pytest

from istota import db, outbound_drafts as drafts
from istota.config import Config, EmailConfig, UserConfig
from istota.outbound_drafts import (
    DraftError,
    DraftNotFound,
    DraftNotPending,
)


@pytest.fixture
def workspace(tmp_path):
    """`{mount}/Users/alice` — the root attachment confinement is scoped to."""
    root = tmp_path / "mount" / "Users" / "alice"
    root.mkdir(parents=True)
    return root


@pytest.fixture
def config(tmp_path, workspace):
    return Config(
        db_path=tmp_path / "test.db",
        email=EmailConfig(
            enabled=True,
            imap_host="imap.test", imap_user="u", imap_password="p",
            smtp_host="smtp.test", smtp_port=587,
            bot_email="bot@test.invalid",
        ),
        nextcloud_mount_path=tmp_path / "mount",
        users={"alice": UserConfig(display_name="Alice")},
    )


@pytest.fixture
def conn(config):
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as c:
        yield c


def _hold(conn, **overrides):
    kwargs = {
        "user_id": "alice",
        "task_id": None,
        "room_token": "room1",
        "to_addrs": ["stranger@example.invalid"],
        "cc_addrs": [],
        "bcc_addrs": [],
        "subject": "Re: Invite",
        "body": "Tuesday works.",
        "html": False,
        "in_reply_to": "<parent@example.invalid>",
        "references": "<root@example.invalid> <parent@example.invalid>",
        "attachments": [],
        "origin_target": "room:room1",
        "hold_reason": "untrusted_recipient",
    }
    kwargs.update(overrides)
    draft_id = drafts.hold(conn, **kwargs)
    # `release` opens its own connection (it must commit a claim before it
    # sends), so the fixture connection must not sit on an open write
    # transaction or the two deadlock.
    conn.commit()
    return draft_id


# ---------------------------------------------------------------------------
# 1. The row is self-sufficient
# ---------------------------------------------------------------------------


class TestHoldAndRead:
    def test_everything_needed_to_send_round_trips(self, conn):
        draft_id = _hold(conn)
        d = drafts.get(conn, draft_id)

        assert d.status == "pending"
        assert d.to_addrs == ["stranger@example.invalid"]
        assert d.subject == "Re: Invite"
        assert d.body == "Tuesday works."
        # The threading headers are snapshotted, not re-derived at release: a
        # second IMAP fetch can fail or come back different.
        assert d.in_reply_to == "<parent@example.invalid>"
        assert d.references == "<root@example.invalid> <parent@example.invalid>"
        assert d.origin_target == "room:room1"
        assert d.hold_reason == "untrusted_recipient"
        assert d.sent_message_id is None
        assert d.resolved_at is None
        assert d.nagged_at is None

    def test_all_recipients_is_the_envelope_order(self, conn):
        draft_id = _hold(
            conn, to_addrs=["a@x.invalid"], cc_addrs=["b@x.invalid"],
            bcc_addrs=["c@x.invalid"],
        )
        assert drafts.get(conn, draft_id).all_recipients == [
            "a@x.invalid", "b@x.invalid", "c@x.invalid",
        ]

    def test_get_of_an_unknown_id_is_none(self, conn):
        assert drafts.get(conn, 999) is None

    def test_pending_for_user_and_room(self, conn):
        first = _hold(conn)
        second = _hold(conn, subject="Second")
        _hold(conn, user_id="bob", room_token="room2")

        assert [d.id for d in drafts.pending_for_user(conn, "alice")] == [
            first, second,
        ]
        # Creation order — a task holding two drafts renders both, and the
        # order they were composed in is the only one that means anything.
        assert [d.id for d in drafts.pending_for_room(conn, "room1")] == [
            first, second,
        ]

    def test_a_resolved_draft_leaves_the_pending_lists(self, conn):
        draft_id = _hold(conn)
        drafts.discard(conn, draft_id)
        assert drafts.pending_for_user(conn, "alice") == []
        assert drafts.pending_for_room(conn, "room1") == []

    def test_a_draft_with_no_room_is_still_reachable_by_user(self, conn):
        """A cron job mailing an external address under `all` has no room. The
        global list is the only place it appears."""
        draft_id = _hold(conn, room_token=None)
        assert [d.id for d in drafts.pending_for_user(conn, "alice")] == [draft_id]


# ---------------------------------------------------------------------------
# 2. Editing
# ---------------------------------------------------------------------------


class TestEdit:
    def test_edit_replaces_the_body(self, conn):
        draft_id = _hold(conn)
        drafts.edit_body(conn, draft_id, "Actually, Thursday.")
        assert drafts.get(conn, draft_id).body == "Actually, Thursday."

    def test_edit_leaves_recipients_and_threading_alone(self, conn):
        """An editable recipient is a gate the user can be talked through."""
        draft_id = _hold(conn)
        before = drafts.get(conn, draft_id)
        drafts.edit_body(conn, draft_id, "Changed.")
        after = drafts.get(conn, draft_id)

        assert after.to_addrs == before.to_addrs
        assert after.cc_addrs == before.cc_addrs
        assert after.bcc_addrs == before.bcc_addrs
        assert after.subject == before.subject
        assert after.in_reply_to == before.in_reply_to
        assert after.references == before.references

    def test_edit_can_switch_the_body_type(self, conn):
        draft_id = _hold(conn)
        drafts.edit_body(conn, draft_id, "<p>Hi</p>", html=True)
        d = drafts.get(conn, draft_id)
        assert d.html is True
        assert d.body == "<p>Hi</p>"

    def test_edit_of_an_unknown_draft_raises(self, conn):
        with pytest.raises(DraftNotFound):
            drafts.edit_body(conn, 999, "x")

    def test_edit_of_a_discarded_draft_is_refused(self, conn):
        draft_id = _hold(conn)
        drafts.discard(conn, draft_id)
        with pytest.raises(DraftNotPending):
            drafts.edit_body(conn, draft_id, "x")


# ---------------------------------------------------------------------------
# 3. Discard
# ---------------------------------------------------------------------------


class TestDiscard:
    def test_discard_marks_and_stamps(self, conn):
        draft_id = _hold(conn)
        drafts.discard(conn, draft_id)
        d = drafts.get(conn, draft_id)
        assert d.status == "discarded"
        assert d.resolved_at is not None

    def test_discard_is_idempotent(self, conn):
        draft_id = _hold(conn)
        drafts.discard(conn, draft_id)
        drafts.discard(conn, draft_id)
        assert drafts.get(conn, draft_id).status == "discarded"

    def test_discard_of_an_unknown_draft_raises(self, conn):
        with pytest.raises(DraftNotFound):
            drafts.discard(conn, 999)


# ---------------------------------------------------------------------------
# 4. Release — the only path that sends
# ---------------------------------------------------------------------------


class TestRelease:
    def test_release_sends_the_stored_bytes_and_marks_sent(self, conn, config):
        draft_id = _hold(conn)
        with patch(
            "istota.skills.email.send_email", return_value="<new@test.invalid>",
        ) as send:
            message_id = drafts.release(config, draft_id)

        assert message_id == "<new@test.invalid>"
        kwargs = send.call_args.kwargs
        assert kwargs["to"] == "stranger@example.invalid"
        assert kwargs["subject"] == "Re: Invite"
        assert kwargs["body"] == "Tuesday works."
        assert kwargs["in_reply_to"] == "<parent@example.invalid>"
        assert kwargs["references"] == (
            "<root@example.invalid> <parent@example.invalid>"
        )

        d = drafts.get(conn, draft_id)
        assert d.status == "sent"
        assert d.sent_message_id == "<new@test.invalid>"
        assert d.resolved_at is not None

    def test_an_edited_body_is_what_sends(self, conn, config):
        """Approve sends exactly what the user read."""
        draft_id = _hold(conn)
        drafts.edit_body(conn, draft_id, "Actually, Thursday.")
        conn.commit()
        with patch(
            "istota.skills.email.send_email", return_value="<x@test.invalid>",
        ) as send:
            drafts.release(config, draft_id)
        assert send.call_args.kwargs["body"] == "Actually, Thursday."

    def test_release_writes_the_sent_emails_row_with_the_origin_target(
        self, conn, config,
    ):
        """So a reply to the released mail routes back to the room the
        originating task came from, rather than falling to the alerts ladder."""
        draft_id = _hold(conn)
        with patch(
            "istota.skills.email.send_email", return_value="<new@test.invalid>",
        ):
            drafts.release(config, draft_id)

        row = conn.execute(
            "SELECT * FROM sent_emails WHERE message_id = ?",
            ("<new@test.invalid>",),
        ).fetchone()
        assert row is not None
        assert row["to_addr"] == "stranger@example.invalid"
        assert row["origin_target"] == "room:room1"

    def test_release_is_idempotent_on_a_double_approve(self, conn, config):
        """Two clients, or a duplicate tap. The second call must not send."""
        draft_id = _hold(conn)
        with patch(
            "istota.skills.email.send_email", return_value="<once@test.invalid>",
        ) as send:
            first = drafts.release(config, draft_id)
            second = drafts.release(config, draft_id)

        assert send.call_count == 1
        assert first == second == "<once@test.invalid>"

    def test_an_smtp_failure_leaves_the_row_pending(self, conn, config):
        """Never mark sent optimistically — a draft wrongly marked sent is a
        message the user believes went out and did not."""
        draft_id = _hold(conn)
        with patch(
            "istota.skills.email.send_email", side_effect=OSError("connection refused"),
        ):
            with pytest.raises(OSError):
                drafts.release(config, draft_id)

        d = drafts.get(conn, draft_id)
        assert d.status == "pending"
        assert d.sent_message_id is None
        assert d.resolved_at is None

    def test_a_failed_release_can_be_retried(self, conn, config):
        draft_id = _hold(conn)
        with patch("istota.skills.email.send_email", side_effect=OSError("down")):
            with pytest.raises(OSError):
                drafts.release(config, draft_id)
        with patch(
            "istota.skills.email.send_email", return_value="<retry@test.invalid>",
        ):
            assert drafts.release(config, draft_id) == "<retry@test.invalid>"
        assert drafts.get(conn, draft_id).status == "sent"

    def test_release_of_a_discarded_draft_is_refused(self, conn, config):
        draft_id = _hold(conn)
        drafts.discard(conn, draft_id)
        conn.commit()
        with patch("istota.skills.email.send_email") as send:
            with pytest.raises(DraftNotPending):
                drafts.release(config, draft_id)
        send.assert_not_called()

    def test_release_of_an_unknown_draft_raises(self, conn, config):
        with pytest.raises(DraftNotFound):
            drafts.release(config, 999)

    def test_an_unreadable_attachment_fails_with_the_path_named(
        self, conn, config, workspace,
    ):
        missing = str(workspace / "gone.pdf")
        draft_id = _hold(conn, attachments=[missing])
        with patch("istota.skills.email.send_email") as send:
            with pytest.raises(DraftError) as excinfo:
                drafts.release(config, draft_id)
        send.assert_not_called()
        assert missing in str(excinfo.value)
        assert drafts.get(conn, draft_id).status == "pending"

    def test_a_readable_attachment_is_passed_through(self, conn, config, workspace):
        present = workspace / "note.txt"
        present.write_text("hi")
        draft_id = _hold(conn, attachments=[str(present)])
        with patch(
            "istota.skills.email.send_email", return_value="<a@test.invalid>",
        ) as send:
            drafts.release(config, draft_id)
        assert send.call_args.kwargs["attachments"] == [str(present)]

    def test_an_attachment_swapped_for_a_symlink_is_refused(
        self, conn, config, workspace, tmp_path,
    ):
        """The TOCTOU that matters. A draft sits pending indefinitely while the
        user's workspace stays writable from the sandbox, and `release` runs
        unsandboxed in the daemon — so a path validated at hold time can be
        replaced with a link to anything the daemon can read, and attached to a
        mail the user already approved for an external recipient."""
        secret = tmp_path / "config.toml"
        secret.write_text("smtp_password = hunter2")

        attachment = workspace / "invoice.pdf"
        attachment.write_text("real content")
        draft_id = _hold(conn, attachments=[str(attachment)])

        # ... and after the hold, swapped.
        attachment.unlink()
        attachment.symlink_to(secret)

        with patch("istota.skills.email.send_email") as send:
            with pytest.raises(DraftError) as excinfo:
                drafts.release(config, draft_id)
        send.assert_not_called()
        assert "symlink" in str(excinfo.value)
        assert drafts.get(conn, draft_id).status == "pending"

    def test_an_attachment_outside_the_workspace_is_refused(
        self, conn, config, tmp_path,
    ):
        outside = tmp_path / "elsewhere.txt"
        outside.write_text("not yours")
        draft_id = _hold(conn, attachments=[str(outside)])
        with patch("istota.skills.email.send_email") as send:
            with pytest.raises(DraftError) as excinfo:
                drafts.release(config, draft_id)
        send.assert_not_called()
        assert "outside" in str(excinfo.value)

    def test_another_users_workspace_is_not_reachable(
        self, conn, config, tmp_path,
    ):
        """Confinement is scoped to the draft's own owner, not to the mount."""
        bob = tmp_path / "mount" / "Users" / "bob"
        bob.mkdir(parents=True)
        theirs = bob / "private.txt"
        theirs.write_text("bob's")
        draft_id = _hold(conn, attachments=[str(theirs)])
        with patch("istota.skills.email.send_email") as send:
            with pytest.raises(DraftError):
                drafts.release(config, draft_id)
        send.assert_not_called()

    def test_cc_and_bcc_reach_the_send(self, conn, config):
        draft_id = _hold(
            conn, cc_addrs=["cc@x.invalid"], bcc_addrs=["bcc@x.invalid"],
        )
        with patch(
            "istota.skills.email.send_email", return_value="<a@test.invalid>",
        ) as send:
            drafts.release(config, draft_id)
        assert send.call_args.kwargs["cc"] == ["cc@x.invalid"]
        assert send.call_args.kwargs["bcc"] == ["bcc@x.invalid"]

    @pytest.mark.parametrize(
        "email_config",
        [
            EmailConfig(enabled=True),
            # IMAP configured, SMTP not: an empty smtp_host resolves to
            # loopback, so without this branch the send would relay through
            # whatever local MTA happens to be listening.
            EmailConfig(enabled=True, imap_host="imap.test"),
            # Email switched off entirely.
            EmailConfig(enabled=False, smtp_host="smtp.test"),
        ],
    )
    def test_unconfigured_email_refuses_before_attempting_a_send(
        self, conn, tmp_path, email_config,
    ):
        bare = Config(
            db_path=tmp_path / "test.db",
            email=email_config,
            nextcloud_mount_path=tmp_path / "mount",
            users={"alice": UserConfig()},
        )
        draft_id = _hold(conn)
        with patch("istota.skills.email.send_email") as send:
            with pytest.raises(DraftError):
                drafts.release(bare, draft_id)
        send.assert_not_called()
        assert drafts.get(conn, draft_id).status == "pending"


# ---------------------------------------------------------------------------
# 5. The stale-draft sweep
# ---------------------------------------------------------------------------


def _age(conn, draft_id, hours):
    conn.execute(
        "UPDATE outbound_drafts SET created_at = datetime('now', ?) WHERE id = ?",
        (f"-{hours} hours", draft_id),
    )


class TestStaleSweep:
    def test_selects_only_old_pending_unnagged_drafts(self, conn):
        old = _hold(conn, subject="25h")
        _age(conn, old, 25)

        recent = _hold(conn, subject="23h")
        _age(conn, recent, 23)

        already_sent = _hold(conn, subject="sent")
        _age(conn, already_sent, 30)
        conn.execute(
            "UPDATE outbound_drafts SET status = 'sent' WHERE id = ?",
            (already_sent,),
        )

        already_nagged = _hold(conn, subject="nagged")
        _age(conn, already_nagged, 30)
        drafts.mark_nagged(conn, already_nagged)

        assert [d.id for d in drafts.stale_unnagged(conn)] == [old]

    def test_the_sweep_fires_once_per_draft_not_once_per_tick(self, conn):
        """A draft left pending for a week gets one notification, not 168."""
        draft_id = _hold(conn)
        _age(conn, draft_id, 30)

        first = drafts.stale_unnagged(conn)
        assert [d.id for d in first] == [draft_id]
        drafts.mark_nagged(conn, draft_id)

        assert drafts.stale_unnagged(conn) == []

    def test_a_notification_that_failed_is_retried_next_sweep(self, conn):
        """`mark_nagged` is called only after the notification is delivered, so
        a failure leaves the draft in the next sweep."""
        draft_id = _hold(conn)
        _age(conn, draft_id, 30)

        assert [d.id for d in drafts.stale_unnagged(conn)] == [draft_id]
        # Notification failed — nothing stamped.
        assert [d.id for d in drafts.stale_unnagged(conn)] == [draft_id]

    def test_the_window_is_configurable(self, conn):
        draft_id = _hold(conn)
        _age(conn, draft_id, 5)
        assert drafts.stale_unnagged(conn) == []
        assert [d.id for d in drafts.stale_unnagged(conn, older_than_hours=4)] == [
            draft_id,
        ]

    def test_the_sweep_is_global_across_users(self, conn):
        alice = _hold(conn)
        bob = _hold(conn, user_id="bob")
        _age(conn, alice, 30)
        _age(conn, bob, 30)
        assert {d.id for d in drafts.stale_unnagged(conn)} == {alice, bob}


# ---------------------------------------------------------------------------
# 6. Independence from task lifecycle
# ---------------------------------------------------------------------------


class TestTaskIndependence:
    def test_a_draft_outlives_its_cancelled_task(self, conn, config):
        """The draft is independent of task lifecycle by design — the user is
        approving text they read, not resuming a task. That independence is
        also what keeps the confirmation timeout off this path."""
        task_id = db.create_task(
            conn, prompt="reply", user_id="alice", source_type="email",
        )
        draft_id = _hold(conn, task_id=task_id)
        db.cancel_task(conn, task_id)
        conn.commit()

        assert [d.id for d in drafts.pending_for_user(conn, "alice")] == [draft_id]
        with patch(
            "istota.skills.email.send_email", return_value="<x@test.invalid>",
        ):
            assert drafts.release(config, draft_id) == "<x@test.invalid>"

    def test_expire_stale_confirmations_does_not_touch_drafts(self, conn):
        """The inbound gate's 120-minute auto-cancel is right for its own case
        and wrong here: binning an outbound hold silently loses the user's own
        intended reply."""
        draft_id = _hold(conn)
        _age(conn, draft_id, 500)

        db.expire_stale_confirmations(conn, 120)

        assert drafts.get(conn, draft_id).status == "pending"


# ---------------------------------------------------------------------------
# 7. The interleavings — every one of these was a proven defect
# ---------------------------------------------------------------------------


class TestConcurrency:
    """`release` claims the row in a committed transaction before it sends.

    A plain SELECT takes no lock in SQLite, so the earlier read-then-check
    guard prevented nothing: two callers both saw `pending`, both reached SMTP,
    and only the loser's UPDATE matched zero rows — unchecked. Every test in
    this class fails against that version.
    """

    def test_two_concurrent_approvals_send_once(self, conn, config):
        import threading

        draft_id = _hold(conn)
        sends: list[str] = []
        in_smtp = threading.Event()
        release_second = threading.Event()

        def slow_send(**kwargs):
            sends.append(kwargs["body"])
            in_smtp.set()
            # Hold the SMTP window open so the second approval lands inside it.
            release_second.wait(timeout=5)
            return f"<msg{len(sends)}@test.invalid>"

        def first():
            with patch("istota.skills.email.send_email", side_effect=slow_send):
                try:
                    drafts.release(config, draft_id)
                except Exception:
                    pass

        t = threading.Thread(target=first)
        t.start()
        assert in_smtp.wait(timeout=5)

        with patch("istota.skills.email.send_email", side_effect=slow_send):
            with pytest.raises(DraftNotPending):
                drafts.release(config, draft_id)

        release_second.set()
        t.join(timeout=5)

        assert len(sends) == 1
        with db.get_db(config.db_path) as c:
            assert drafts.get(c, draft_id).status == "sent"

    def test_a_discard_arriving_mid_send_is_refused(self, conn, config):
        """Previously the mail went out and the row read `discarded` — the
        inverse of the invariant: a message the user believes they stopped."""
        import threading

        draft_id = _hold(conn)
        in_smtp = threading.Event()
        finish = threading.Event()
        sends: list[object] = []

        def slow_send(**kwargs):
            sends.append(kwargs)
            in_smtp.set()
            finish.wait(timeout=5)
            return "<sent@test.invalid>"

        def releaser():
            with patch("istota.skills.email.send_email", side_effect=slow_send):
                drafts.release(config, draft_id)

        t = threading.Thread(target=releaser)
        t.start()
        assert in_smtp.wait(timeout=5)

        with db.get_db(config.db_path) as c:
            with pytest.raises(DraftNotPending):
                drafts.discard(c, draft_id)

        finish.set()
        t.join(timeout=5)

        with db.get_db(config.db_path) as c:
            d = drafts.get(c, draft_id)
        assert d.status == "sent"
        assert len(sends) == 1

    def test_an_edit_arriving_mid_send_is_refused(self, conn, config):
        """Previously the edit reported success while the *pre-edit* body went
        out, and the row then showed the new body marked sent — nothing
        recorded that the stored and sent bytes differed."""
        import threading

        draft_id = _hold(conn)
        in_smtp = threading.Event()
        finish = threading.Event()
        sent_bodies: list[str] = []

        def slow_send(**kwargs):
            sent_bodies.append(kwargs["body"])
            in_smtp.set()
            finish.wait(timeout=5)
            return "<sent@test.invalid>"

        def releaser():
            with patch("istota.skills.email.send_email", side_effect=slow_send):
                drafts.release(config, draft_id)

        t = threading.Thread(target=releaser)
        t.start()
        assert in_smtp.wait(timeout=5)

        with db.get_db(config.db_path) as c:
            with pytest.raises(DraftNotPending):
                drafts.edit_body(c, draft_id, "EDITED")

        finish.set()
        t.join(timeout=5)

        with db.get_db(config.db_path) as c:
            d = drafts.get(c, draft_id)
        # The stored body is what was sent. They cannot diverge.
        assert sent_bodies == ["Tuesday works."]
        assert d.body == "Tuesday works."
        assert d.status == "sent"

    def test_an_edit_landing_before_the_claim_is_what_sends(self, conn, config):
        """The window the mid-send guard does not cover: an edit that commits
        between `release`'s read and its claim.

        Both statements succeed, because both require `status='pending'` and the
        edit leaves it there. The edit's own guard is satisfied and the PATCH
        route reports 200, so the user is told their new text is what will go
        out. If `release` sends from the row it read *before* the claim, the
        recipient gets the pre-edit body instead — irreversibly, and with
        nothing anywhere recording that the sent bytes were not the stored ones.

        A plain SELECT takes no lock under deferred isolation, and the claim
        UPDATE then waits up to the full busy timeout for the write lock, so the
        window is wide rather than theoretical and the competing writer is
        exactly `edit_body`. Patching `get` to edit on its first call reproduces
        the interleaving deterministically.
        """
        draft_id = _hold(conn)
        real_get = drafts.get
        calls = {"n": 0}

        def edit_then_read(c, did):
            calls["n"] += 1
            result = real_get(c, did)
            if calls["n"] == 1:
                # Commits on its own connection, exactly as the PATCH route does.
                with db.get_db(config.db_path) as other:
                    drafts.edit_body(other, did, "Wednesday, actually.")
                    other.commit()
            return result

        with patch("istota.skills.email.send_email") as send:
            send.return_value = "<sent@test.invalid>"
            with patch.object(drafts, "get", side_effect=edit_then_read):
                drafts.release(config, draft_id)

        assert send.call_args.kwargs["body"] == "Wednesday, actually."
        with db.get_db(config.db_path) as c:
            d = drafts.get(c, draft_id)
        # The stored body and the sent body cannot diverge.
        assert d.body == "Wednesday, actually."
        assert d.status == "sent"

    def test_the_sent_marker_survives_a_caller_rollback(self, conn, config):
        """`release` owns its own transaction, so a caller who rolls back after
        it returns cannot resurrect the draft and send the mail twice."""
        draft_id = _hold(conn)
        with patch(
            "istota.skills.email.send_email", return_value="<once@test.invalid>",
        ) as send:
            drafts.release(config, draft_id)

            # A caller doing more work on their own connection, then failing.
            with db.get_db(config.db_path) as c:
                c.execute(
                    "INSERT INTO outbound_drafts (user_id, to_addrs) VALUES (?, ?)",
                    ("alice", '["x@y.invalid"]'),
                )
                c.rollback()

            # The draft is still sent, and a re-approve does not re-send.
            assert drafts.release(config, draft_id) == "<once@test.invalid>"

        assert send.call_count == 1

    def test_a_row_stuck_in_sending_is_not_silently_resent(self, conn, config):
        """The process died between the claim and the result. We cannot know
        whether the mail went out, so the row is terminal rather than reset —
        resetting it to pending would risk sending twice."""
        draft_id = _hold(conn)
        conn.execute(
            "UPDATE outbound_drafts SET status = 'sending' WHERE id = ?",
            (draft_id,),
        )
        conn.commit()

        with patch("istota.skills.email.send_email") as send:
            with pytest.raises(DraftNotPending):
                drafts.release(config, draft_id)
        send.assert_not_called()


class TestTheRealSendPath:
    """Patching `send_email` skips the layer that builds the actual message.

    These patch `_send_smtp` instead and assert on the `EmailMessage` and the
    envelope recipient list, so the mapping from a stored draft to the wire is
    pinned rather than assumed.
    """

    def _sent(self, config, draft_id):
        captured = {}

        def capture(msg, cfg, recipients=None):
            captured["msg"] = msg
            captured["recipients"] = recipients

        with patch("istota.skills.email._send_smtp", side_effect=capture):
            captured["message_id"] = drafts.release(config, draft_id)
        return captured

    def test_every_recipient_reaches_the_envelope_and_bcc_stays_off_the_headers(
        self, conn, config,
    ):
        draft_id = _hold(
            conn,
            to_addrs=["a@x.invalid", "b@x.invalid"],
            cc_addrs=["c@x.invalid"],
            bcc_addrs=["d@x.invalid"],
        )
        sent = self._sent(config, draft_id)

        assert sent["recipients"] == [
            "a@x.invalid", "b@x.invalid", "c@x.invalid", "d@x.invalid",
        ]
        assert sent["msg"]["To"] == "a@x.invalid, b@x.invalid"
        assert sent["msg"]["Cc"] == "c@x.invalid"
        # Bcc recipients get the mail; the header is never transmitted.
        assert sent["msg"]["Bcc"] is None

    def test_the_threading_headers_and_subject_are_carried(self, conn, config):
        draft_id = _hold(conn)
        sent = self._sent(config, draft_id)
        assert sent["msg"]["Subject"] == "Re: Invite"
        assert sent["msg"]["In-Reply-To"] == "<parent@example.invalid>"
        assert sent["msg"]["References"] == (
            "<root@example.invalid> <parent@example.invalid>"
        )

    def test_a_real_message_id_is_generated_and_recorded(self, conn, config):
        draft_id = _hold(conn)
        sent = self._sent(config, draft_id)
        assert sent["message_id"].startswith("<")
        assert sent["message_id"] == sent["msg"]["Message-ID"]
        with db.get_db(config.db_path) as c:
            assert drafts.get(c, draft_id).sent_message_id == sent["message_id"]

    def test_the_html_flag_produces_an_html_part(self, conn, config):
        draft_id = _hold(conn, body="<p>Hi</p>", html=True)
        sent = self._sent(config, draft_id)
        assert sent["msg"].get_content_type() == "text/html"

    def test_a_plain_draft_stays_plain(self, conn, config):
        draft_id = _hold(conn)
        sent = self._sent(config, draft_id)
        assert sent["msg"].get_content_type() == "text/plain"


class TestCorruptColumns:
    """A malformed column must refuse, never degrade to an empty list.

    Degrading meant sending to a *different* recipient set than the row records
    and then marking it sent.
    """

    @pytest.mark.parametrize(
        "stored",
        ['"a@b.invalid"', "{}", "not json at all", '[{"a": "x@y.invalid"}]', "[1, 2]"],
    )
    def test_a_corrupt_recipient_column_refuses(self, conn, config, stored):
        draft_id = _hold(conn)
        conn.execute(
            "UPDATE outbound_drafts SET to_addrs = ? WHERE id = ?",
            (stored, draft_id),
        )
        conn.commit()

        with patch("istota.skills.email.send_email") as send:
            with pytest.raises(drafts.DraftCorrupt):
                drafts.release(config, draft_id)
        send.assert_not_called()


class TestOpenListing:
    """The listing the web surface reads, which has the opposite strictness
    requirement to `release`.

    Refusing to send a row we cannot read is right. Refusing to *list* nine
    readable rows because a tenth is malformed is not — it empties the approval
    surface for mail that is perfectly answerable.
    """

    def test_it_carries_sending_rows_as_well_as_pending(self, conn):
        """A row left `sending` — the process died between the claim and the
        finalize — is invisible to every `status='pending'` producer, so the one
        state that needs a human to check the mailbox was the one state the web
        surface could not show."""
        pending = _hold(conn)
        stuck = _hold(conn, subject="Stuck")
        conn.execute(
            "UPDATE outbound_drafts SET status = ? WHERE id = ?",
            (drafts.STATUS_SENDING, stuck),
        )
        conn.commit()

        listing = drafts.open_for_user(conn, "alice")

        assert [d.id for d in listing.drafts] == [pending, stuck]
        assert [d.status for d in listing.drafts] == ["pending", "sending"]

    def test_a_resolved_row_is_not_open(self, conn):
        draft_id = _hold(conn)
        drafts.discard(conn, draft_id)
        assert drafts.open_for_user(conn, "alice").drafts == []

    def test_one_corrupt_row_does_not_take_down_the_others(self, conn):
        good = _hold(conn, subject="Readable")
        bad = _hold(conn, subject="Corrupt")
        conn.execute(
            "UPDATE outbound_drafts SET to_addrs = ? WHERE id = ?",
            ("not json at all", bad),
        )
        conn.commit()

        listing = drafts.open_for_user(conn, "alice")

        assert [d.id for d in listing.drafts] == [good]
        # Reported rather than dropped: the user still has held mail, and a row
        # that silently vanishes is mail they never learn about.
        assert listing.unreadable == [bad]

    def test_a_clean_listing_reports_nothing_unreadable(self, conn):
        _hold(conn)
        assert drafts.open_for_user(conn, "alice").unreadable == []

    def test_it_is_scoped_to_the_owner(self, conn):
        mine = _hold(conn)
        _hold(conn, user_id="bob")
        assert [d.id for d in drafts.open_for_user(conn, "alice").drafts] == [mine]


class TestStaleSweepResilience:
    """One corrupt row must not silence the nag for everyone.

    The sweep is a single global read across every user, so a row that raises
    here stopped the stale-draft notification for the whole instance, on every
    tick, indefinitely — a draft nobody can be told about, which is the exact
    outcome the notification exists to prevent.
    """

    def _aged(self, conn, **overrides):
        draft_id = _hold(conn, **overrides)
        conn.execute(
            "UPDATE outbound_drafts SET created_at = datetime('now', '-48 hours') "
            "WHERE id = ?", (draft_id,),
        )
        conn.commit()
        return draft_id

    def test_a_corrupt_row_is_skipped_rather_than_raising(self, conn):
        good = self._aged(conn, subject="Readable")
        bad = self._aged(conn, subject="Corrupt")
        conn.execute(
            "UPDATE outbound_drafts SET to_addrs = ? WHERE id = ?",
            ("not json", bad),
        )
        conn.commit()

        stale = drafts.stale_unnagged(conn)

        assert [d.id for d in stale] == [good]

    def test_a_clean_sweep_is_unaffected(self, conn):
        first = self._aged(conn)
        second = self._aged(conn, subject="Second")
        assert [d.id for d in drafts.stale_unnagged(conn)] == [first, second]


class TestIdentityRead:
    """Owner and status without parsing anything that can be corrupt.

    The ownership check and the discard guard both need these two columns and
    nothing else, so making them go through `_row` meant a malformed recipient
    list denied the user the one action that would clear it.
    """

    def test_it_reports_owner_and_status(self, conn):
        draft_id = _hold(conn)
        assert drafts.identity(conn, draft_id) == ("alice", "pending")

    def test_an_unknown_id_is_none(self, conn):
        assert drafts.identity(conn, 999) is None

    def test_it_reads_a_row_whose_json_columns_are_corrupt(self, conn):
        draft_id = _hold(conn)
        conn.execute(
            "UPDATE outbound_drafts SET to_addrs = ? WHERE id = ?",
            ("{}", draft_id),
        )
        conn.commit()

        assert drafts.identity(conn, draft_id) == ("alice", "pending")

    def test_a_corrupt_row_can_still_be_discarded(self, conn):
        """Discarding sends nothing, so nothing about it depends on being able
        to read the recipient list. Leaving it refused would strand the card
        with no action that works."""
        draft_id = _hold(conn)
        conn.execute(
            "UPDATE outbound_drafts SET attachments = ? WHERE id = ?",
            ("[1, 2]", draft_id),
        )
        conn.commit()

        drafts.discard(conn, draft_id)

        assert drafts.identity(conn, draft_id) == ("alice", "discarded")

    def test_a_corrupt_row_still_refuses_to_send(self, conn, config):
        """The other half of the same rule: resilience is for listing and
        discarding, never for the send."""
        draft_id = _hold(conn)
        conn.execute(
            "UPDATE outbound_drafts SET to_addrs = ? WHERE id = ?",
            ("not json", draft_id),
        )
        conn.commit()

        with pytest.raises(drafts.DraftCorrupt):
            drafts.release(config, draft_id)


class TestAddressNormalization:
    """Stored addresses, the card and the envelope are one list.

    `send_email` re-parses with `getaddresses`, so a stored entry holding two
    addresses would become two envelope recipients while the row reported one.
    """

    def test_a_multi_address_entry_is_split_at_hold_time(self, conn):
        draft_id = _hold(conn, to_addrs=["a@x.invalid, b@x.invalid"])
        assert drafts.get(conn, draft_id).to_addrs == [
            "a@x.invalid", "b@x.invalid",
        ]

    def test_a_display_name_is_reduced_to_the_address(self, conn):
        draft_id = _hold(conn, to_addrs=["Alice <alice@x.invalid>"])
        assert drafts.get(conn, draft_id).to_addrs == ["alice@x.invalid"]

    def test_addresses_are_lowercased(self, conn):
        draft_id = _hold(conn, to_addrs=["Alice@X.Invalid"])
        assert drafts.get(conn, draft_id).to_addrs == ["alice@x.invalid"]

    def test_a_newline_bearing_address_is_refused_at_hold(self, conn):
        """`send_email` sets To/Cc without sanitizing, so this would raise deep
        inside the email package on the user's approval instead of here."""
        with pytest.raises(DraftError):
            _hold(conn, to_addrs=["a@x.invalid\nBcc: victim@x.invalid"])

    @pytest.mark.parametrize("bad", ["garbage", "", "   ", "@x.invalid"])
    def test_an_unusable_address_is_refused_at_hold(self, conn, bad):
        with pytest.raises(DraftError):
            _hold(conn, to_addrs=[bad])

    def test_a_draft_with_no_recipients_is_refused(self, conn):
        with pytest.raises(DraftError):
            _hold(conn, to_addrs=[])


class TestPolicyIndependence:
    """Policy is evaluated at hold time and never re-read."""

    def test_release_ignores_a_policy_tightened_since_the_hold(
        self, conn, config,
    ):
        draft_id = _hold(conn)
        config.email.outbound_approval_floor = "all"
        with patch(
            "istota.skills.email.send_email", return_value="<x@test.invalid>",
        ):
            assert drafts.release(config, draft_id) == "<x@test.invalid>"

    def test_turning_the_gate_off_does_not_auto_send_held_mail(
        self, conn, config,
    ):
        draft_id = _hold(conn)
        config.email.outbound_approval_floor = "off"
        assert [d.id for d in drafts.pending_for_user(conn, "alice")] == [draft_id]
        assert drafts.get(conn, draft_id).status == "pending"


class TestTwoDraftsFromOneTask:
    def test_each_is_independently_answerable(self, conn, config):
        task_id = db.create_task(
            conn, prompt="reply to both", user_id="alice", source_type="email",
        )
        first = _hold(conn, task_id=task_id, subject="To Ann")
        second = _hold(conn, task_id=task_id, subject="To Bo")

        assert [d.id for d in drafts.pending_for_room(conn, "room1")] == [
            first, second,
        ]

        drafts.discard(conn, first)
        conn.commit()
        with patch(
            "istota.skills.email.send_email", return_value="<b@test.invalid>",
        ):
            drafts.release(config, second)

        with db.get_db(config.db_path) as c:
            assert drafts.get(c, first).status == "discarded"
            assert drafts.get(c, second).status == "sent"


class TestSweepArgumentValidation:
    @pytest.mark.parametrize("hours", [0, -24])
    def test_a_non_positive_window_returns_nothing(self, conn, hours):
        """`datetime('now', '--24 hours')` is not a legal modifier — it
        evaluates to NULL and the whole predicate goes NULL, so the sweep
        silently returns nothing; `0` would mean *now* and nag every pending
        draft at once."""
        draft_id = _hold(conn)
        _age(conn, draft_id, 200)
        assert drafts.stale_unnagged(conn, older_than_hours=hours) == []
