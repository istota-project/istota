"""Regression tests for the inbound email volume budget (ISSUE-250, consequence 1).

The poll became bounded and lossless in an earlier pass, but nothing capped how
much of what it drained turned into paid model invocations. `bot+{user_id}@` is
public by construction — it is the `From:` on every mail the bot sends on a
user's behalf — so any past correspondent can turn one SMTP transaction into a
task on someone else's account, and could do it without limit.

What these tests pin:

1. **A per-user budget**, counted over a sliding window and enforced before the
   task is created.
2. **A per-sender budget under it**, so one loud correspondent throttles alone
   rather than consuming the user's whole allowance.
3. **Over-budget mail is filed, not dropped** — a `throttled` ledger row, the
   message left in INBOX, one alert per window rather than one per message.
   A budget that silently discards recreates the mail loss the first pass fixed,
   with a config knob on it.
4. **The confirmation prompts collapse.** The gate turned a spam flood into a
   notification flood: one prompt per held message, undeduplicated.
5. **Inbound email is off the foreground queue**, so a stranger's mail no longer
   competes with the user's live chat for the same worker slots.
6. **The body and the attachments are capped**, so one large message is not its
   own amplification.

Plus the cross-user half, which is independent of email: `dispatch` scanned
users in an arbitrary order and broke at the instance cap, so a user late in the
scan could get zero workers indefinitely.
"""

import re
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
        # The budget is what these tests are about, so give every one of them a
        # large batch: a backlog that drains a few messages per tick would hide
        # a limiter that never fires.
        config.scheduler.email_poll_batch_size = 200
        for key, val in overrides.items():
            setattr(config, key, val)
        return config
    return _make


class FakeMailbox:
    """A stand-in IMAP folder with faithful UID-range + limit semantics.

    `senders` maps a UID to the address that sent it, so one mailbox can carry
    several correspondents — which is what the per-sender budget is about.
    """

    def __init__(self, uids, uidvalidity=1, sender="stranger@example.com",
                 senders=None, body="Body text", attachments=None):
        self.uids = sorted(uids)
        self.uidvalidity = uidvalidity
        self.sender = sender
        self.senders = senders or {}
        self.body = body
        self.attachments = attachments or []
        self.read_calls = []

    def sender_for(self, uid):
        return self.senders.get(int(uid), self.sender)

    def _envelope(self, uid):
        return EmailEnvelope(
            id=str(uid),
            subject=f"Message {uid}",
            sender=self.sender_for(uid),
            date="Mon, 01 Jan 2026 10:00:00 +0000",
            is_read=False,
            to=("bot+alice@test.com",),
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
            # UID, even when n is above it.
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
            sender=self.sender_for(email_id),
            date="Mon, 01 Jan 2026 10:00:00 +0000",
            body=self.body,
            attachments=list(self.attachments),
            message_id=f"<msg{email_id}@test.com>",
            references=None,
            to=("bot+alice@test.com",),
            cc=(),
        )


def _run_poll(config, mailbox, download=None, notifications=None):
    """Drive one poll against `mailbox`, with the network legs stubbed.

    `notifications` collects `(kind, user_id, message)` for every alert and
    confirmation prompt the poll would have sent, which is how the tests count
    what reaches the user's channel.
    """
    sent = notifications if notifications is not None else []

    def _fake_prompt(config, user_id, message, conversation_token=None):
        sent.append(("prompt", user_id, message))
        return True, 111

    def _fake_notify(config, user_id, message, purpose=None, **kwargs):
        sent.append(("alert", user_id, message))
        return True

    with (
        patch("istota.transport.email.inbound.list_emails", mailbox.list_emails),
        patch("istota.transport.email.inbound.read_email", mailbox.read_email),
        patch("istota.transport.email.inbound.download_attachments",
              download or (lambda *a, **k: [])),
        patch("istota.transport.email.inbound.ensure_user_directories_v2"),
        patch("istota.transport.email.inbound.upload_file_to_inbox_v2"),
        patch("istota.notifications.send_confirmation_prompt", _fake_prompt),
        patch("istota.notifications.send_notification", _fake_notify),
    ):
        return poll_emails(config), sent


@pytest.fixture(autouse=True)
def _clear_module_state():
    """`poll_emails` keeps per-message failure and alert dedup state in module
    dicts. Tests run under xdist in one process, so each must start clean."""
    inbound._reset_message_failures()
    inbound._reset_volume_state()
    yield
    inbound._reset_message_failures()
    inbound._reset_volume_state()


def _prime_cursor(config, mailbox):
    """Run one poll so the folder has a cursor.

    The first poll of a folder resumes near the top of the mailbox rather than
    walking from UID 1, so a test about a *flood* has to establish a running
    poller first — which is the real scenario.
    """
    original = mailbox.uids
    mailbox.uids = original[:1]
    _run_poll(config, mailbox)
    mailbox.uids = original


def _ledger(config):
    with db.get_db(config.db_path) as conn:
        rows = conn.execute(
            "SELECT email_id, sender_email, routing_method, task_id "
            "FROM processed_emails ORDER BY CAST(email_id AS INTEGER)"
        ).fetchall()
    return [dict(r) for r in rows]


def _tasks(config):
    with db.get_db(config.db_path) as conn:
        rows = conn.execute(
            "SELECT id, user_id, source_type, queue, status FROM tasks ORDER BY id"
        ).fetchall()
    return [dict(r) for r in rows]


# =============================================================================
# The per-user budget
# =============================================================================


class TestPerUserBudget:
    def test_flood_stops_creating_tasks_at_the_budget(self, make_config):
        """The filed symptom: N messages became N tasks, with no ceiling.

        A stranger sends 40 messages against a budget of 10. Ten become tasks;
        the rest are filed. Before the budget this created 40.
        """
        config = make_config()
        config.scheduler.email_rate_limit_messages = 10
        config.scheduler.email_sender_rate_limit_messages = 0  # per-user only
        mailbox = FakeMailbox(range(1, 42))
        _prime_cursor(config, mailbox)

        created, _ = _run_poll(config, mailbox)

        # UID 1 was consumed priming the cursor and counts against the budget.
        assert len(created) == 9
        assert len([t for t in _tasks(config) if t["source_type"] == "email"]) == 10

    def test_over_budget_mail_is_filed_not_dropped(self, make_config):
        """A budget that silently discards recreates the mail loss the first
        pass fixed. Over-budget messages get a `throttled` ledger row with no
        task, and stay in the mailbox where `email from-senders` can reach them.
        """
        config = make_config()
        config.scheduler.email_rate_limit_messages = 5
        config.scheduler.email_sender_rate_limit_messages = 0
        mailbox = FakeMailbox(range(1, 21))
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox)

        ledger = _ledger(config)
        throttled = [r for r in ledger if r["routing_method"] == "throttled"]
        # Every message is accounted for — nothing was skipped.
        assert len(ledger) == 20
        assert len(throttled) == 15
        assert all(r["task_id"] is None for r in throttled)

    def test_budget_recovers_as_the_window_slides(self, make_config):
        """The budget is a sliding window over recent tasks, not a latch: once
        the earlier tasks age out, mail flows again without operator action."""
        config = make_config()
        config.scheduler.email_rate_limit_messages = 3
        config.scheduler.email_sender_rate_limit_messages = 0
        config.scheduler.email_rate_limit_window_seconds = 3600
        mailbox = FakeMailbox(range(1, 11))
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox)
        assert len([t for t in _tasks(config) if t["source_type"] == "email"]) == 3

        # Age every existing task past the window.
        with db.get_db(config.db_path) as conn:
            conn.execute(
                "UPDATE tasks SET created_at = datetime('now', '-2 hours')"
            )

        mailbox.uids = list(range(1, 21))
        created, _ = _run_poll(config, mailbox)
        assert len(created) == 3

    def test_zero_disables_the_budget(self, make_config):
        """An operator who wants the old unbounded behaviour can have it."""
        config = make_config()
        config.scheduler.email_rate_limit_messages = 0
        config.scheduler.email_sender_rate_limit_messages = 0
        mailbox = FakeMailbox(range(1, 31))
        _prime_cursor(config, mailbox)

        created, _ = _run_poll(config, mailbox)

        assert len(created) == 29
        assert not [r for r in _ledger(config) if r["routing_method"] == "throttled"]

    def test_one_alert_per_window_not_one_per_message(self, make_config):
        """Throttling must not become its own notification flood. One alert per
        user per window, naming the count and the senders behind it."""
        config = make_config()
        config.scheduler.email_rate_limit_messages = 2
        config.scheduler.email_sender_rate_limit_messages = 0
        mailbox = FakeMailbox(range(1, 31))
        _prime_cursor(config, mailbox)

        _, sent = _run_poll(config, mailbox)

        alerts = [m for kind, _, m in sent if kind == "alert"]
        assert len(alerts) == 1
        assert "stranger@example.com" in alerts[0]
        assert "28" in alerts[0]

        # A second poll in the same window adds no further alert.
        mailbox.uids = list(range(1, 61))
        _, sent2 = _run_poll(config, mailbox)
        assert [m for kind, _, m in sent2 if kind == "alert"] == []


# =============================================================================
# The per-sender budget, underneath the per-user one
# =============================================================================


class TestPerSenderBudget:
    def test_one_loud_sender_does_not_consume_the_users_allowance(self, make_config):
        """The point of the sub-budget: a correspondent who floods throttles
        alone, and mail from everyone else still gets through."""
        config = make_config()
        config.scheduler.email_rate_limit_messages = 100
        config.scheduler.email_sender_rate_limit_messages = 3
        senders = {uid: "loud@example.com" for uid in range(2, 40)}
        senders[40] = "quiet-correspondent@example.com"
        senders[41] = "quiet-correspondent@example.com"
        mailbox = FakeMailbox(range(1, 42), senders=senders)
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox)

        ledger = _ledger(config)
        loud = [r for r in ledger if r["sender_email"] == "loud@example.com"]
        quiet = [r for r in ledger if r["sender_email"] == "quiet-correspondent@example.com"]

        assert len([r for r in loud if r["task_id"] is not None]) == 3
        assert len([r for r in loud if r["routing_method"] == "throttled"]) == 35
        # The second correspondent is unaffected — this is the property a
        # per-user-only budget does not have.
        assert len(quiet) == 2
        assert all(r["task_id"] is not None for r in quiet)

    def test_per_sender_budget_is_scoped_to_the_sender(self, make_config):
        """Two senders each below the sub-budget both get through, even though
        their combined count exceeds it."""
        config = make_config()
        config.scheduler.email_rate_limit_messages = 100
        config.scheduler.email_sender_rate_limit_messages = 2
        senders = {2: "a@example.com", 3: "a@example.com",
                   4: "b@example.com", 5: "b@example.com"}
        mailbox = FakeMailbox(range(2, 6), senders=senders)
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox)

        assert not [r for r in _ledger(config) if r["routing_method"] == "throttled"]


# =============================================================================
# The confirmation prompts
# =============================================================================


class TestConfirmationPromptCollapse:
    def test_prompts_are_capped_per_sender_per_window(self, make_config):
        """The gate turned a spam flood into a notification flood: one prompt
        per held message, undeduplicated, all of them answerable only one at a
        time. Past a few, the user gets one notice instead."""
        config = make_config()
        config.email.confirm_sender_match = "gate"
        config.scheduler.email_rate_limit_messages = 0
        config.scheduler.email_sender_rate_limit_messages = 0
        # UID 1 primes the cursor, and priming sends a prompt of its own — from
        # a different address, so it does not spend the budget under test.
        mailbox = FakeMailbox(range(1, 21), senders={1: "primer@example.com"})
        _prime_cursor(config, mailbox)

        _, sent = _run_poll(config, mailbox)

        prompts = [m for kind, _, m in sent if kind == "prompt"]
        assert len(prompts) == inbound._MAX_PROMPTS_PER_SENDER_WINDOW

        # The held mail is still held and still answerable — suppressing the
        # prompt must not release the gate.
        held = [t for t in _tasks(config) if t["status"] == "pending_confirmation"]
        assert len(held) == 20

    def test_a_single_notice_covers_the_suppressed_prompts(self, make_config):
        """Suppressing silently would leave held mail with nobody told."""
        config = make_config()
        config.email.confirm_sender_match = "gate"
        config.scheduler.email_rate_limit_messages = 0
        config.scheduler.email_sender_rate_limit_messages = 0
        mailbox = FakeMailbox(range(1, 21), senders={1: "primer@example.com"})
        _prime_cursor(config, mailbox)

        _, sent = _run_poll(config, mailbox)

        notices = [m for kind, _, m in sent if kind == "alert"]
        assert len(notices) == 1
        assert "stranger@example.com" in notices[0]
        assert "!confirm" in notices[0]

    def test_a_handful_of_prompts_is_unchanged(self, make_config):
        """The collapse must not fire on ordinary traffic — three held messages
        from a stranger still ask three ordinary questions."""
        config = make_config()
        config.email.confirm_sender_match = "gate"
        config.scheduler.email_rate_limit_messages = 0
        config.scheduler.email_sender_rate_limit_messages = 0
        mailbox = FakeMailbox(range(1, 4), senders={1: "primer@example.com"})
        _prime_cursor(config, mailbox)

        _, sent = _run_poll(config, mailbox)

        assert len([m for kind, _, m in sent if kind == "prompt"]) == 2
        assert [m for kind, _, m in sent if kind == "alert"] == []


# =============================================================================
# The queue
# =============================================================================


class TestInboundEmailQueue:
    def test_email_tasks_land_on_the_background_queue(self, make_config):
        """Mail from a stranger used to compete with the user's live Talk and
        web-chat turns for the same foreground worker slots."""
        config = make_config()
        mailbox = FakeMailbox([1, 2, 3])
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox)

        email_tasks = [t for t in _tasks(config) if t["source_type"] == "email"]
        assert email_tasks
        assert all(t["queue"] == "background" for t in email_tasks)

    def test_the_queue_is_configurable(self, make_config):
        """An operator who wants mail to keep its old latency can have it."""
        config = make_config()
        config.scheduler.email_task_queue = "foreground"
        mailbox = FakeMailbox([1, 2, 3])
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox)

        email_tasks = [t for t in _tasks(config) if t["source_type"] == "email"]
        assert all(t["queue"] == "foreground" for t in email_tasks)


# =============================================================================
# Byte caps
# =============================================================================


class TestByteCaps:
    def test_a_huge_body_is_truncated_before_it_reaches_the_prompt(self, make_config):
        """One large message is otherwise its own amplification: the body is
        interpolated whole into the prompt, and the prompt is what gets paid
        for."""
        config = make_config()
        config.scheduler.email_max_body_chars = 500
        mailbox = FakeMailbox([1, 2], body="x" * 50_000)
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox)

        with db.get_db(config.db_path) as conn:
            prompt = conn.execute(
                "SELECT prompt FROM tasks ORDER BY id DESC LIMIT 1"
            ).fetchone()["prompt"]
        assert len(prompt) < 2000
        assert "truncated" in prompt.lower()

    def test_a_body_within_the_cap_is_untouched(self, make_config):
        config = make_config()
        config.scheduler.email_max_body_chars = 5000
        mailbox = FakeMailbox([1, 2], body="hello there")
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox)

        with db.get_db(config.db_path) as conn:
            prompt = conn.execute(
                "SELECT prompt FROM tasks ORDER BY id DESC LIMIT 1"
            ).fetchone()["prompt"]
        assert "hello there" in prompt
        assert "truncated" not in prompt.lower()

    def test_attachment_download_stops_at_the_per_poll_budget(self, make_config):
        """Attachments are the cheapest way to make the poll slow: unbounded
        bytes fetched over IMAP and pushed to WebDAV, per message."""
        config = make_config()
        config.scheduler.email_max_attachment_bytes_per_poll = 10_000
        seen_budgets = []

        def _download(email_id, target_dir=None, folder=None, config=None,
                      max_total_bytes=None):
            seen_budgets.append(max_total_bytes)
            target_dir.mkdir(parents=True, exist_ok=True)
            path = target_dir / f"{email_id}.bin"
            written = min(4000, max_total_bytes or 4000)
            path.write_bytes(b"x" * written)
            return [path] if written else []

        # Declared attachments matter: a message reporting none skips the
        # download call entirely, so the budget would never be consulted.
        mailbox = FakeMailbox(range(1, 8), attachments=["a.bin"])
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox, download=_download)

        # The budget handed to each call shrinks as the poll spends it, and
        # reaches zero rather than going negative.
        assert seen_budgets[0] == 10_000
        assert seen_budgets == sorted(seen_budgets, reverse=True)
        assert seen_budgets[-1] == 0


class TestDownloadAttachmentsCap:
    """The cap inside `download_attachments` itself.

    The poller tests above stub this function out, so without these the whole
    skip branch is uncovered — the one place the byte budget is actually
    enforced.
    """

    def _mailbox(self, *payloads):
        from unittest.mock import MagicMock

        atts = []
        for name, data in payloads:
            att = MagicMock()
            att.filename = name
            att.payload = data
            atts.append(att)
        msg = MagicMock()
        msg.attachments = atts
        mailbox = MagicMock()
        mailbox.fetch.return_value = [msg]
        mailbox.__enter__ = lambda s: s
        mailbox.__exit__ = lambda s, *a: False
        return mailbox

    def _download(self, mailbox, target, **kwargs):
        from istota.skills.email import EmailConfig as SkillEmailConfig, download_attachments

        cfg = SkillEmailConfig(
            imap_host="imap.test", imap_port=993, imap_user="u",
            imap_password="p", smtp_host="smtp.test", smtp_port=587,
        )
        with patch("istota.skills.email._get_mailbox", return_value=mailbox):
            return download_attachments("1", target_dir=target, config=cfg, **kwargs)

    def test_an_attachment_that_would_cross_the_budget_is_skipped_whole(self, tmp_path):
        """Whole attachments only — a truncated file is worse than an absent
        one, because nothing downstream would know it was cut."""
        mailbox = self._mailbox(("small.txt", b"x" * 100), ("big.bin", b"y" * 5000))

        paths = self._download(mailbox, tmp_path / "att", max_total_bytes=1000)

        assert [p.name for p in paths] == ["small.txt"]
        assert (tmp_path / "att" / "small.txt").read_bytes() == b"x" * 100
        assert not (tmp_path / "att" / "big.bin").exists()

    def test_no_cap_writes_everything(self, tmp_path):
        mailbox = self._mailbox(("a.txt", b"x" * 100), ("b.txt", b"y" * 5000))

        paths = self._download(mailbox, tmp_path / "att", max_total_bytes=None)

        assert sorted(p.name for p in paths) == ["a.txt", "b.txt"]

    def test_a_zero_budget_writes_nothing(self, tmp_path):
        """The poller passes `None` for "unlimited", so a literal 0 reaching
        here means the batch is spent and every attachment must be skipped."""
        mailbox = self._mailbox(("a.txt", b"x"))

        assert self._download(mailbox, tmp_path / "att", max_total_bytes=0) == []


class TestSkippedAttachmentsAreDeclared:
    def test_the_prompt_names_an_attachment_that_was_not_retrieved(self, make_config):
        """A message saying "see the attached invoice" must not reach the model
        with no invoice and no indication one was withheld — the same rule the
        truncated body follows."""
        config = make_config()
        mailbox = FakeMailbox([1, 2], attachments=["invoice.pdf"])
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox, download=lambda *a, **k: [])

        with db.get_db(config.db_path) as conn:
            prompt = conn.execute(
                "SELECT prompt FROM tasks ORDER BY id DESC LIMIT 1"
            ).fetchone()["prompt"]
        assert "invoice.pdf" in prompt
        assert "not retrieved" in prompt

    def test_no_declared_attachments_means_no_download_call(self, make_config):
        """The second IMAP login per message is pure cost when the message
        already told us it has nothing to fetch."""
        config = make_config()
        calls = []

        def _download(*a, **k):
            calls.append(a)
            return []

        mailbox = FakeMailbox([1, 2, 3], attachments=[])
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox, download=_download)

        assert calls == []


class TestPromptCollapseKnob:
    def test_zero_disables_the_collapse(self, make_config):
        """"0 disables" is documented as the escape hatch for this feature, so
        it has to disable this half too — otherwise an operator who opted out
        still loses gated mail to the confirmation timeout."""
        config = make_config()
        config.email.confirm_sender_match = "gate"
        config.scheduler.email_rate_limit_messages = 0
        config.scheduler.email_sender_rate_limit_messages = 0
        config.scheduler.email_confirmation_prompts_per_window = 0
        mailbox = FakeMailbox(range(1, 12), senders={1: "primer@example.com"})
        _prime_cursor(config, mailbox)

        _, sent = _run_poll(config, mailbox)

        assert len([m for kind, _, m in sent if kind == "prompt"]) == 10
        assert [m for kind, _, m in sent if kind == "alert"] == []

    def test_a_held_notice_survives_an_earlier_throttle_notice(self, make_config):
        """The two notices must not share a dedup slot. Throttled mail is filed
        and recoverable; held mail is cancelled in two hours if nobody answers,
        so a user already told about throttling must still be told about holds.
        """
        config = make_config()
        config.email.confirm_sender_match = "gate"
        # Loose enough that several gated messages get past the collapse
        # threshold (so there is held mail to report), tight enough that the
        # rest is throttled (so there is filed mail too).
        config.scheduler.email_rate_limit_messages = 8
        config.scheduler.email_sender_rate_limit_messages = 0
        mailbox = FakeMailbox(range(1, 31), senders={1: "primer@example.com"})
        _prime_cursor(config, mailbox)

        _, sent = _run_poll(config, mailbox)

        alerts = [m for kind, _, m in sent if kind == "alert"]
        assert len(alerts) == 2
        assert any("over your email budget" in m for m in alerts)
        assert any("!confirm" in m for m in alerts)


class TestSenderKeyNormalization:
    def test_the_prompt_collapse_is_not_evaded_by_a_display_name(self, make_config):
        """The DB budget keys on the addr-spec, so this must too — otherwise
        one sender varying their display name gets an unbounded number of
        prompts while the task budget still counts them as one sender."""
        config = make_config()
        config.email.confirm_sender_match = "gate"
        config.scheduler.email_rate_limit_messages = 0
        config.scheduler.email_sender_rate_limit_messages = 0
        senders = {
            uid: f"Name {uid} <loud@example.com>" for uid in range(2, 15)
        }
        senders[1] = "primer@example.com"
        mailbox = FakeMailbox(range(1, 15), senders=senders)
        _prime_cursor(config, mailbox)

        _, sent = _run_poll(config, mailbox)

        prompts = [m for kind, _, m in sent if kind == "prompt"]
        assert len(prompts) == inbound._MAX_PROMPTS_PER_SENDER_WINDOW

    def test_expired_prompt_windows_are_pruned(self, make_config):
        """The key holds an attacker-supplied address, so a dict that only ever
        grows is a slow leak on a long-running daemon."""
        inbound._prompt_counts[("alice", "old@example.com")] = (0.0, 5)
        config = make_config()
        mailbox = FakeMailbox([1, 2])
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox)

        assert ("alice", "old@example.com") not in inbound._prompt_counts


class TestQueueValidation:
    def test_a_typo_falls_back_to_background_with_a_warning(self, caplog):
        """`queue` goes into `tasks` verbatim with no CHECK, and both claim and
        dispatch filter on the literal values — so a typo produces rows no
        worker ever claims, failing hours later as "your task was cancelled"."""
        from istota.config import _valid_task_queue

        with caplog.at_level("WARNING"):
            assert _valid_task_queue("backgroud") == "background"
        assert "email_task_queue" in caplog.text

    def test_valid_values_pass_through(self):
        from istota.config import _valid_task_queue

        assert _valid_task_queue("foreground") == "foreground"
        assert _valid_task_queue("background") == "background"


# =============================================================================
# Cross-user starvation — independent of email
# =============================================================================


class TestDispatchOrdering:
    def test_users_are_scanned_oldest_pending_task_first(self, tmp_path):
        """`dispatch` iterated a bare `SELECT DISTINCT user_id` with no
        `ORDER BY` and broke at the instance cap, so a user late in an arbitrary
        scan order could get zero workers tick after tick while a flooding user
        reliably held slots. Longest-waiting first is what makes the per-user
        caps mean what they appear to mean.
        """
        db_path = tmp_path / "dispatch.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            for user, age in (("newest", "-1 minutes"),
                              ("oldest", "-90 minutes"),
                              ("middle", "-30 minutes")):
                task_id = db.create_task(
                    conn, prompt="x", user_id=user, source_type="talk",
                    queue="foreground",
                )
                conn.execute(
                    "UPDATE tasks SET created_at = datetime('now', ?) WHERE id = ?",
                    (age, task_id),
                )
            order = db.get_users_with_pending_fg_queue_tasks(conn)

        assert order == ["oldest", "middle", "newest"]

    def test_background_scan_is_ordered_too(self, tmp_path):
        """Names chosen so alphabetical order contradicts age order — an
        unordered `GROUP BY user_id` returns them sorted by name, which would
        pass a test whose oldest user also sorted first."""
        db_path = tmp_path / "dispatch_bg.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            for user, age in (("aaron", "-1 minutes"), ("zoe", "-60 minutes")):
                task_id = db.create_task(
                    conn, prompt="x", user_id=user, source_type="email",
                    queue="background",
                )
                conn.execute(
                    "UPDATE tasks SET created_at = datetime('now', ?) WHERE id = ?",
                    (age, task_id),
                )
            order = db.get_users_with_pending_bg_queue_tasks(conn)

        assert order == ["zoe", "aaron"]


# =============================================================================
# The counting helpers
# =============================================================================


class TestCounters:
    def test_email_task_count_is_scoped_to_user_source_and_window(self, tmp_path):
        db_path = tmp_path / "count.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            db.create_task(conn, prompt="x", user_id="alice", source_type="email")
            db.create_task(conn, prompt="x", user_id="alice", source_type="talk")
            db.create_task(conn, prompt="x", user_id="bob", source_type="email")
            old = db.create_task(conn, prompt="x", user_id="alice", source_type="email")
            conn.execute(
                "UPDATE tasks SET created_at = datetime('now', '-2 hours') WHERE id = ?",
                (old,),
            )

            assert db.count_recent_email_tasks(conn, "alice", 3600) == 1
            assert db.count_recent_email_tasks(conn, "bob", 3600) == 1
            assert db.count_recent_email_tasks(conn, "alice", 86400) == 2

    def test_sender_count_reads_the_ledger_and_ignores_taskless_rows(self, tmp_path):
        """`processed_emails` already holds the sender. Rows with no task —
        quiet, discarded, and the throttled rows this change writes — must not
        count against the budget, or throttling would be self-sustaining."""
        db_path = tmp_path / "sender.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            task_id = db.create_task(
                conn, prompt="x", user_id="alice", source_type="email",
            )
            db.mark_email_processed(
                conn, email_id="1", sender_email="loud@example.com",
                subject="s", user_id="alice", task_id=task_id,
                routing_method="plus_address", uidvalidity=1,
            )
            db.mark_email_processed(
                conn, email_id="2", sender_email="loud@example.com",
                subject="s", user_id="alice", task_id=None,
                routing_method="throttled", uidvalidity=1,
            )
            db.mark_email_processed(
                conn, email_id="3", sender_email="other@example.com",
                subject="s", user_id="alice", task_id=task_id,
                routing_method="plus_address", uidvalidity=1,
            )

            assert db.count_recent_email_tasks_from_sender(
                conn, "alice", "loud@example.com", 3600,
            ) == 1
            assert db.count_recent_email_tasks_from_sender(
                conn, "alice", "unseen@example.com", 3600,
            ) == 0

    def test_sender_count_matches_the_address_not_the_display_name(self, tmp_path):
        """The ledger stores the raw envelope sender, so the same person can be
        `Loud <loud@example.com>` on one message and `loud@example.com` on the
        next. A budget that treated those as two senders would not bind."""
        db_path = tmp_path / "sender_addr.db"
        db.init_db(db_path)
        with db.get_db(db_path) as conn:
            for i, raw in enumerate(
                ["loud@example.com", "Loud Person <loud@example.com>"], start=1,
            ):
                task_id = db.create_task(
                    conn, prompt="x", user_id="alice", source_type="email",
                )
                db.mark_email_processed(
                    conn, email_id=str(i), sender_email=raw, subject="s",
                    user_id="alice", task_id=task_id,
                    routing_method="plus_address", uidvalidity=1,
                )

            assert db.count_recent_email_tasks_from_sender(
                conn, "alice", "LOUD@example.com", 3600,
            ) == 2


# =============================================================================
# The budget must not break what the earlier pass fixed
# =============================================================================


class TestThrottlingIsStillLossless:
    def test_the_cursor_advances_past_throttled_mail(self, make_config):
        """A throttled message is resolved, not owed: the cursor must pass it,
        or the poll would refetch the same batch forever."""
        config = make_config()
        config.scheduler.email_rate_limit_messages = 2
        config.scheduler.email_sender_rate_limit_messages = 0
        mailbox = FakeMailbox(range(1, 21))
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox)

        with db.get_db(config.db_path) as conn:
            cursor = db.get_email_poll_cursor(conn, config.email.poll_folder)
        assert cursor is not None
        assert cursor[1] == 20

    def test_a_throttled_message_is_not_re_ingested_later(self, make_config):
        """Filed means handled. Once the budget frees up, the poll must not go
        back and turn the filed backlog into tasks after all — the user asks for
        it explicitly with `email from-senders` instead."""
        config = make_config()
        config.scheduler.email_rate_limit_messages = 2
        config.scheduler.email_sender_rate_limit_messages = 0
        mailbox = FakeMailbox(range(1, 21))
        _prime_cursor(config, mailbox)
        _run_poll(config, mailbox)

        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE tasks SET created_at = datetime('now', '-2 hours')")

        created, _ = _run_poll(config, mailbox)
        assert created == []

    def test_quiet_senders_never_reach_the_budget(self, make_config):
        """A quiet sender creates no task, so it must not spend the allowance
        that real mail needs."""
        config = make_config()
        config.scheduler.email_rate_limit_messages = 3
        config.scheduler.email_sender_rate_limit_messages = 0
        config.users["alice"].quiet_email_senders = ["*@newsletter.example"]
        senders = {uid: "list@newsletter.example" for uid in range(2, 30)}
        senders[30] = "real@example.com"
        mailbox = FakeMailbox(range(1, 31), senders=senders)
        _prime_cursor(config, mailbox)

        _run_poll(config, mailbox)

        ledger = _ledger(config)
        assert len([r for r in ledger if r["routing_method"] == "quiet"]) == 28
        real = [r for r in ledger if r["sender_email"] == "real@example.com"]
        assert len(real) == 1
        assert real[0]["task_id"] is not None
