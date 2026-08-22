"""The write/deliver split, at the four producer sites that raise a notification.

This is the finding the store's two-call shape exists for. Every one of these
producers raises from *inside* an open write transaction on the framework DB.
`db.get_db` uses `timeout=30.0`, so a second connection opened from in there
waits the full thirty seconds on the write lock the caller is holding, raises,
and is swallowed by the store's never-raises contract — a silent per-notification
stall on the dispatch loop, not a failure. So the assertions are about
*connection opens* and about which side of the `with` block a call lands on,
never about wall time: a regression here is slow, and a timing assertion for it
would be both flaky and thirty seconds long.

Two properties per site:

1. `write_notification` runs on the producer's own connection, inside its open
   transaction, and opens nothing of its own.
2. Delivery happens after that block has closed — proved by taking `BEGIN
   IMMEDIATE` on a second connection from inside the delivery call, which can
   only succeed once the producer's write lock is gone.
"""

from __future__ import annotations

import contextlib
from unittest.mock import patch

import pytest

from istota import db, notification_store as store
from istota.config import Config, EmailConfig, UserConfig
from istota.skills.email import Email, EmailEnvelope
from istota.transport.email.inbound import poll_emails

BOT = "bot@test.invalid"
PLUS = "bot+alice@test.invalid"
OWN = "alice@example.com"
STRANGER = "stranger@example.invalid"


# ---------------------------------------------------------------------------
# probes
# ---------------------------------------------------------------------------


class _WriteProbe:
    """Wraps `write_notification` and records what the caller was holding.

    `opened_during` counts connections opened by the store itself, which must
    stay at zero: the whole point of the split is that the write rides the
    caller's connection.
    """

    def __init__(self, monkeypatch, target: str = "istota.notification_store.write_notification"):
        self.calls: list[dict] = []
        self._depth = _ConnectionDepth(monkeypatch)
        real = store.write_notification

        def _spy(conn, user_id, **kwargs):
            before = self._depth.opens
            result = real(conn, user_id, **kwargs)
            self.calls.append({
                "user_id": user_id,
                "in_transaction": bool(getattr(conn, "in_transaction", False)),
                "opened_during": self._depth.opens - before,
                "source": kwargs.get("source"),
                "dedup_key": kwargs.get("dedup_key"),
                "result": result,
            })
            return result

        monkeypatch.setattr(target, _spy)


class _ConnectionDepth:
    """Counts `db.get_db` opens, so a nested one is visible."""

    def __init__(self, monkeypatch):
        self.opens = 0
        real = db.get_db

        @contextlib.contextmanager
        def _tracked(*args, **kwargs):
            self.opens += 1
            with real(*args, **kwargs) as conn:
                yield conn

        monkeypatch.setattr(db, "get_db", _tracked)


def _count_sends(monkeypatch, *, delivered: bool = False) -> list[tuple]:
    """Record every `send_notification` the inbox delivery makes.

    Patched where `deliver_pending` imports it from, so the real buffering and
    the real `last_delivered_at` stamp still run — a patch on `deliver_pending`
    itself would prove only that something was called.
    """
    calls: list[tuple] = []

    def _send(cfg, user_id, text, **kwargs):
        calls.append((user_id, text, kwargs.get("purpose")))
        return delivered

    monkeypatch.setattr("istota.notifications.send_notification", _send)
    return calls


def _stored(config, source: str):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT * FROM notifications WHERE source = ?", (source,),
        ).fetchone()


def _lock_free_probe(config, record: list[str], label: str):
    """A `deliver_pending` stand-in that proves the producer's lock is gone."""

    def _probe(cfg, results):
        with db.get_db(config.db_path) as probe:
            probe.execute("BEGIN IMMEDIATE")
            probe.rollback()
        record.append(label)

    return _probe


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


def _email_config():
    return EmailConfig(
        enabled=True,
        imap_host="imap.test", imap_port=993,
        imap_user="u", imap_password="p",
        smtp_host="smtp.test", smtp_port=587,
        bot_email=BOT,
        outbound_approval_floor="untrusted",
    )


@pytest.fixture(autouse=True)
def _clean_prompt_budget():
    """The gate's prompt throttle is a module dict that outlives one test.

    `_prompt_counts` is keyed `(user, sender)` and never cleared between tests
    in a reused process, so a second gated mail from the same sender reaches
    `_prompt_budget_spent` and has its prompt collapsed — the case one of the
    tests below covers deliberately, and silent pollution everywhere else.
    """
    from istota.transport.email import inbound

    inbound._prompt_counts.clear()
    inbound._throttle_alerted.clear()
    yield
    inbound._prompt_counts.clear()
    inbound._throttle_alerted.clear()


@pytest.fixture
def config(tmp_path, monkeypatch):
    cfg = Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "temp",
        nextcloud_mount_path=tmp_path / "mount",
        email=_email_config(),
        users={"alice": UserConfig(display_name="Alice", email_addresses=[OWN])},
    )
    (tmp_path / "mount" / "Users" / "alice").mkdir(parents=True)
    db.init_db(cfg.db_path)
    monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
    return cfg


def _gated_mail(uid="41", sender=STRANGER, subject="Invite"):
    envelope = EmailEnvelope(
        id=uid, subject=subject, sender=sender,
        date="Mon, 01 Jan 2026 10:00:00 +0000", is_read=False,
    )
    email = Email(
        id=uid, subject=subject, sender=sender,
        date="Mon, 01 Jan 2026 10:00:00 +0000",
        body="Are you free next week?", attachments=[],
        message_id=f"<{uid}@example.invalid>", references=None,
        to=(PLUS,), cc=(),
    )
    return envelope, email


def _poll(config, envelope, email):
    with (
        patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
        patch("istota.transport.email.inbound.read_email", return_value=email),
        patch("istota.transport.email.inbound.download_attachments", return_value=[]),
    ):
        return poll_emails(config)


# ---------------------------------------------------------------------------
# 1. the inbound email gate
# ---------------------------------------------------------------------------


class TestEmailGate:
    def test_the_row_is_written_on_the_pollers_own_connection(
        self, config, monkeypatch,
    ):
        probe = _WriteProbe(monkeypatch)
        envelope, email = _gated_mail()
        with patch(
            "istota.notifications.send_confirmation_prompt", return_value=(True, None),
        ):
            task_ids = _poll(config, envelope, email)

        assert len(task_ids) == 1
        assert len(probe.calls) == 1, "the gate raised no notification"
        call = probe.calls[0]
        assert call["in_transaction"], (
            "the gate must write inside the transaction that parked the task"
        )
        assert call["opened_during"] == 0, (
            "the store opened its own connection while the poller held the lock"
        )
        assert call["dedup_key"] == f"task:{task_ids[0]}"

    def test_delivery_runs_after_the_poll_transaction_closes(
        self, config, monkeypatch,
    ):
        record: list[str] = []
        monkeypatch.setattr(
            "istota.transport.email.inbound.deliver_pending",
            _lock_free_probe(config, record, "delivered"),
        )
        envelope, email = _gated_mail(uid="42")
        # The prompt fails, which is the arm that actually delivers the row —
        # stubbing a *successful* prompt would leave the buffer empty and the
        # probe would pass on a call that sent nothing.
        with patch(
            "istota.notifications.send_confirmation_prompt", return_value=(False, None),
        ):
            _poll(config, envelope, email)

        assert record == ["delivered"], (
            "deliver_pending never ran, or ran while the write lock was held"
        )

    def test_a_delivered_prompt_is_the_rows_delivery_and_the_row_sends_nothing(
        self, config, monkeypatch,
    ):
        """One question, one message.

        The prompt already carries this question to whatever the user's routing
        table resolves; sending the row as well puts two messages in the same
        alerts channel for one gated email.
        """
        sent = _count_sends(monkeypatch)
        envelope, email = _gated_mail(uid="43")
        with patch(
            "istota.notifications.send_confirmation_prompt", return_value=(True, None),
        ):
            _poll(config, envelope, email)

        assert sent == [], "the row pushed a second copy of the question"
        assert _stored(config, "confirmation")["last_delivered_at"] is None

    def test_a_prompt_that_reached_nobody_falls_back_to_the_row(
        self, config, monkeypatch,
    ):
        """The one arm where the row is the only voice left.

        `send_confirmation_prompt` returning False used to mean a WARNING and a
        task that died unanswered two hours later. The row is durable either
        way; this is the second attempt on top of it.
        """
        sent = _count_sends(monkeypatch, delivered=True)
        envelope, email = _gated_mail(uid="44")
        with patch(
            "istota.notifications.send_confirmation_prompt", return_value=(False, None),
        ):
            _poll(config, envelope, email)

        assert len(sent) == 1
        assert _stored(config, "confirmation")["last_delivered_at"] is not None

    def test_a_throttled_prompt_writes_a_row_and_pushes_nothing(
        self, config, monkeypatch,
    ):
        """The arm that would undo ISSUE-250 if the row delivered.

        Past the per-sender budget the prompt is suppressed and the sender's
        held mail is collapsed into one summary notice. A row that delivered
        here would fan fifty collapsed prompts straight back out as fifty
        notification pushes.
        """
        from istota.transport.email import inbound

        sent = _count_sends(monkeypatch)
        with patch(
            "istota.notifications.send_confirmation_prompt", return_value=(True, None),
        ):
            for n in range(inbound._MAX_PROMPTS_PER_SENDER_WINDOW + 1):
                envelope, email = _gated_mail(uid=str(50 + n))
                _poll(config, envelope, email)

        with db.get_db(config.db_path) as conn:
            rows = conn.execute(
                "SELECT last_delivered_at FROM notifications "
                "WHERE source = 'confirmation'",
            ).fetchall()
        assert len(rows) == inbound._MAX_PROMPTS_PER_SENDER_WINDOW + 1, (
            "every held task must get a row, throttled or not"
        )
        assert all(r["last_delivered_at"] is None for r in rows)
        # Exactly one push for the whole burst, and it is the collapse's own
        # summary — not one per row, which is what the collapse exists to stop.
        assert len(sent) == 1, [text for _, text, _ in sent]
        assert "held waiting for your confirmation" in sent[0][1]


# ---------------------------------------------------------------------------
# 2. the scheduler's confirmation park
# ---------------------------------------------------------------------------


class TestSchedulerConfirmation:
    def test_the_park_writes_inside_its_own_transaction_and_delivers_after(
        self, config, monkeypatch,
    ):
        from istota.scheduler import process_one_task

        with db.get_db(config.db_path) as conn:
            db.create_task(
                conn, prompt="Delete the file", user_id="alice",
                source_type="web", conversation_token="web-alice-abc",
                output_target="web",
            )

        probe = _WriteProbe(monkeypatch)
        record: list[str] = []
        monkeypatch.setattr(
            "istota.scheduler.deliver_pending",
            _lock_free_probe(config, record, "delivered"),
        )
        monkeypatch.setattr("istota.scheduler.run_coro", lambda *a, **k: None)
        monkeypatch.setattr(
            "istota.scheduler.execute_task",
            lambda *a, **k: (
                True,
                "I need your confirmation before deleting the file. Reply yes or no.",
                None, None,
            ),
        )

        result = process_one_task(config)
        assert result is not None
        task_id, _ = result

        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending_confirmation"
        assert len(probe.calls) == 1
        assert probe.calls[0]["in_transaction"]
        assert probe.calls[0]["opened_during"] == 0
        assert probe.calls[0]["dedup_key"] == f"task:{task_id}"
        assert record == ["delivered"]

    def test_a_talk_carried_question_writes_a_row_and_pushes_nothing(
        self, config, monkeypatch,
    ):
        """`post_talk_message` posts the question into the room itself.

        Delivering the row as well puts the same question in that room twice.
        The row is still written — it is what makes the question answerable
        from the bell after the message has scrolled away.
        """
        from istota.scheduler import process_one_task

        config.users["alice"] = UserConfig(
            display_name="Alice", email_addresses=[OWN], alerts_channel="alerts",
        )
        with db.get_db(config.db_path) as conn:
            db.create_task(
                conn, prompt="Delete the file", user_id="alice",
                source_type="talk", conversation_token="talk-room-1",
            )

        sent = _count_sends(monkeypatch)
        posted: list = []
        monkeypatch.setattr(
            "istota.scheduler.run_coro", lambda *a, **k: posted.append(a) or 1,
        )
        monkeypatch.setattr(
            "istota.scheduler.execute_task",
            lambda *a, **k: (
                True,
                "I need your confirmation before deleting the file. Reply yes or no.",
                None, None,
            ),
        )

        result = process_one_task(config)
        assert result is not None
        task_id, _ = result

        with db.get_db(config.db_path) as conn:
            assert db.get_task(conn, task_id).status == "pending_confirmation"
            row = conn.execute(
                "SELECT last_delivered_at FROM notifications WHERE dedup_key = ?",
                (f"task:{task_id}",),
            ).fetchone()
        assert row is not None, "the park wrote no row"
        assert row["last_delivered_at"] is None
        assert sent == [], "the row pushed a second copy of the question"
        # Without this the test passes for the wrong reason: a plan with no
        # Talk leg at all also sends nothing, and would prove nothing about
        # the branch being asserted.
        assert posted, "the Talk leg never carried the question"


# ---------------------------------------------------------------------------
# 3. the outbound approval gate (daemon leg)
# ---------------------------------------------------------------------------


class TestOutboundGate:
    def test_the_hold_writes_inside_the_gates_transaction(self, config, monkeypatch):
        from istota.transport.email import outbound

        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(
                conn, prompt="reply", user_id="alice", source_type="email",
            )
            task = db.get_task(conn, task_id)

        probe = _WriteProbe(monkeypatch)
        record: list[str] = []
        monkeypatch.setattr(
            "istota.transport.email.outbound.deliver_pending",
            _lock_free_probe(config, record, "delivered"),
        )

        may_send, draft_id = outbound._hold_if_unapproved(
            config, task, to_addr=STRANGER, subject="Re: hello",
            body="the reply", html=False,
        )

        assert may_send is False and draft_id is not None
        assert len(probe.calls) == 1
        assert probe.calls[0]["in_transaction"]
        assert probe.calls[0]["opened_during"] == 0
        assert probe.calls[0]["dedup_key"] == f"draft:{draft_id}"
        assert record == ["delivered"]


# ---------------------------------------------------------------------------
# 4. the email skill CLI leg — a row, and deliberately no delivery
# ---------------------------------------------------------------------------


class TestSkillCliGate:
    def test_the_skill_writes_a_row_and_never_delivers(self, config, monkeypatch):
        """`hold`'s second caller is a short-lived host-side subprocess.

        Delivering from there would put `send_notification`'s Talk and ntfy
        fan-out in the skill proxy's child process. The user learns about the
        draft from the bell, and the skill's own return value already tells the
        model what happened.
        """
        from istota.skills import email as email_skill

        monkeypatch.setenv("ISTOTA_USER_ID", "alice")
        probe = _WriteProbe(monkeypatch)
        sent: list = []
        monkeypatch.setattr(
            "istota.notifications.send_notification",
            lambda *a, **k: sent.append(a) or True,
        )

        refusal, _paths = email_skill._outbound_gate(
            to=[STRANGER], cc=[], bcc=[],
            subject="hello", body="hi", html=False,
            in_reply_to=None, references=None, reply_to=None, attachments=[],
        )

        assert refusal is not None and refusal["status"] == "held"
        assert len(probe.calls) == 1
        assert probe.calls[0]["in_transaction"]
        assert probe.calls[0]["opened_during"] == 0
        assert probe.calls[0]["dedup_key"] == f"draft:{refusal['draft_id']}"
        assert sent == [], "the skill CLI must not deliver a notification"

        with db.get_db(config.db_path) as conn:
            row = conn.execute(
                "SELECT last_delivered_at FROM notifications WHERE source = ?",
                ("outbound_draft",),
            ).fetchone()
        assert row is not None
        assert row["last_delivered_at"] is None


# ---------------------------------------------------------------------------
# 5. the never-a-second-connection property, stated directly
# ---------------------------------------------------------------------------


def test_write_notification_opens_no_connection_of_its_own(config, monkeypatch):
    depth = _ConnectionDepth(monkeypatch)
    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        before = depth.opens
        result = store.write_notification(
            conn, "alice", source="confirmation", dedup_key="task:1",
            title="held", object_type="task", object_id="1",
        )
    assert result is not None
    assert depth.opens == before
