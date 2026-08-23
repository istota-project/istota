"""Sender-influenced text is flattened before it is pushed, not only before it is stored.

The notification inbox established that stranger-authored text is stripped of
what a markdown surface would render — Talk renders markdown, so a forged
``From:`` display name reading ``[click here](http://evil)`` becomes a live link
in the user's alerts channel, in a message the bot appears to have written. That
rule reached the durable rows and stopped at the sends beside them: the row was
flattened and the push was not (ISSUE-310).

Three producers in ``transport/email/inbound.py`` compose a message out of
attacker-supplied fields and hand it to the notification ladder:

- ``_deliver_dmarc_alerts`` — the canary's sender, subject and the
  ``Authentication-Results`` detail, all of which arrived on unauthenticated mail
- ``_deliver_throttle_notices`` — the top-senders listing
- ``_deliver_confirmation_prompts`` — the gate's own sender and subject

Each is flattened at the *send*, which is a choke point: a field added to one of
those messages later cannot reintroduce the gap, the way a per-field whitelist
could. The rule is the body rule, not the label rule — see
``test_the_evidence_survives`` for why that distinction is the whole reason this
is not one call to ``confirmations.flatten``.

**Two vectors, two rules, and neither covers the other.** The body rule keeps
newlines, deliberately, because in a free-form alert the line structure is
evidence. But all three of these messages are line-oriented templates, and a
subject can carry a real newline out of ``decode_header`` — so the fields are
additionally flattened to one line each at composition, with
``flatten_prompt_header``. ``TestForgedLines`` covers that half; the
``carries_no_markup`` tests cover the other.

What neither rule does is strip a bare URL: ``http://evil.example/pay`` in a
forged display name still autolinks on a surface that linkifies. That is
``flatten_body``'s standing limit, shared with every durable ``task_alert`` row,
and narrowing it belongs there rather than here.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from istota import db
from istota import notification_sources as sources
from istota.config import Config, EmailConfig as AppEmailConfig, UserConfig
from istota.notification_resolvers import task_alert
from istota.skills.email import Email, EmailEnvelope
from istota.transport.email import inbound as inbound_module
from istota.transport.email.inbound import (
    _DmarcAlert,
    _PendingPrompt,
    _ThrottleNotice,
    poll_emails,
)

# Everything a body may not carry into Talk: inline and reference links, images,
# autolinks and raw HTML, code spans, table cells. Kept here rather than imported
# so the test states the contract instead of restating the implementation.
MARKUP = set("[]()`<>|")

# A display name and a subject a forger controls end to end.
HOSTILE_SENDER = "[click here](http://evil.example) <a@evil.example>"
HOSTILE_SUBJECT = "`rm -rf /` | <img src=x onerror=alert(1)>"


@pytest.fixture(autouse=True)
def _registry():
    sources.reset_registry()
    yield
    sources.reset_registry()


@pytest.fixture(autouse=True)
def _clear_windows():
    inbound_module._reset_dmarc_alert_dedup()
    inbound_module._reset_volume_state()
    yield
    inbound_module._reset_dmarc_alert_dedup()
    inbound_module._reset_volume_state()


@pytest.fixture
def config(tmp_path):
    cfg = Config()
    cfg.db_path = tmp_path / "istota.db"
    cfg.temp_dir = tmp_path / "temp"
    cfg.temp_dir.mkdir()
    cfg.nextcloud_mount_path = tmp_path / "mount"
    cfg.email = AppEmailConfig(
        enabled=True,
        imap_host="imap.test", imap_port=993,
        imap_user="user", imap_password="pass",
        smtp_host="smtp.test", smtp_port=587,
        bot_email="bot@test.com",
    )
    cfg.users = {"alice": UserConfig(
        email_addresses=["alice@test.com"], alerts_channel="alerts_room",
    )}
    db.init_db(cfg.db_path)
    return cfg


def _message_arg(call) -> str:
    """The message text a notification call was actually handed.

    Fails rather than falling back to ``""``. Returning the empty string on a
    signature this no longer recognizes would leave every `_assert_flat` below
    passing vacuously — the one failure mode a sanitization test cannot afford,
    because it looks identical to a clean result.
    """
    args, kwargs = call
    if "message" in kwargs:
        return kwargs["message"]
    if len(args) > 2:
        return args[2]
    pytest.fail(f"no message argument in the notification call: {args!r} {kwargs!r}")


def _pushed(send) -> str:
    assert send.call_count >= 1, "nothing was pushed"
    return _message_arg(send.call_args)


def _assert_flat(text: str) -> None:
    found = sorted(MARKUP & set(text))
    assert not found, f"markup reached the push: {found!r} in {text!r}"


# ---------------------------------------------------------------------------
# The DMARC canary
# ---------------------------------------------------------------------------


def _dmarc_alert(message: str, *, user_id="alice", verdict="fail", sender="a@evil.example"):
    key = (user_id, sender.lower(), verdict)
    return {key: _DmarcAlert(
        key=key, user_id=user_id, verdict=verdict, sender=sender, message=message,
    )}


class TestDmarcAlerts:
    def test_the_push_carries_no_markup(self, config):
        alerts = _dmarc_alert(
            f"Inbound mail authentication check failed.\n\n"
            f"Mail from {HOSTILE_SENDER} routed as sender_match.\n"
            f"Subject: {HOSTILE_SUBJECT}"
        )
        with patch("istota.notifications.send_notification", return_value=True) as send:
            inbound_module._deliver_dmarc_alerts(config, alerts)
        _assert_flat(_pushed(send))

    def test_the_push_and_the_row_say_the_same_thing(self, config):
        """The gap ISSUE-310 names is the row and the push disagreeing.

        Equality holds only under the row's cap: `task_alert.write` truncates to
        `MAX_ALERT_BODY_CHARS` and the push does not, so a long enough alert
        genuinely diverges. That divergence predates this fix and is not what is
        being asserted — the rule applied to both is.
        """
        alerts = _dmarc_alert(f"Mail from {HOSTILE_SENDER}\nSubject: {HOSTILE_SUBJECT}")
        with patch("istota.notifications.send_notification", return_value=True) as send:
            inbound_module._deliver_dmarc_alerts(config, alerts)

        with db.get_db(config.db_path) as conn:
            body = conn.execute(
                "SELECT body FROM notifications WHERE dedup_key = 'dmarc:fail'",
            ).fetchone()[0]
        pushed = _pushed(send)
        assert len(pushed) < task_alert.MAX_ALERT_BODY_CHARS, "the cap would explain a pass"
        assert pushed == body

    def test_through_the_poller(self, config):
        """The real composition path, not a hand-built message."""
        envelope = EmailEnvelope(
            id="1", subject=HOSTILE_SUBJECT, sender="alice@test.com",
            date="Mon, 01 Jan 2026 10:00:00 +0000", is_read=False,
        )
        email = Email(
            id="1", subject=HOSTILE_SUBJECT, sender="alice@test.com",
            date="Mon, 01 Jan 2026 10:00:00 +0000",
            body="pay this invoice", attachments=[],
            message_id="<1@test.com>", references=None,
            to=("bot@test.com",), cc=(),
            authentication_results="mx.test; dmarc=fail header.from=test.com",
            authentication_results_all=(),
        )
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_notification", return_value=True) as send,
        ):
            poll_emails(config)

        pushed = _pushed(send)
        # The subject really did reach the alert — otherwise this asserts nothing.
        assert "rm -rf /" in pushed
        _assert_flat(pushed)


# ---------------------------------------------------------------------------
# The throttle notices
# ---------------------------------------------------------------------------


class TestThrottleNotices:
    @pytest.mark.parametrize("kind", ["throttled", "held"])
    def test_the_push_carries_no_markup(self, config, kind):
        notice = _ThrottleNotice(user_id="alice")
        if kind == "throttled":
            notice.record(HOSTILE_SENDER)
        else:
            notice.record_held(HOSTILE_SENDER)

        with patch("istota.notifications.send_notification", return_value=True) as send:
            inbound_module._deliver_throttle_notices(config, {"alice": notice}, 3600)
        _assert_flat(_pushed(send))

    def test_the_push_and_the_row_say_the_same_thing(self, config):
        """Under the row's cap — see the DMARC pair for why that qualifier is here."""
        notice = _ThrottleNotice(user_id="alice")
        notice.record(HOSTILE_SENDER)

        with patch("istota.notifications.send_notification", return_value=True) as send:
            inbound_module._deliver_throttle_notices(config, {"alice": notice}, 3600)

        with db.get_db(config.db_path) as conn:
            body = conn.execute(
                "SELECT body FROM notifications WHERE dedup_key LIKE 'throttle:%'",
            ).fetchone()[0]
        pushed = _pushed(send)
        assert len(pushed) < task_alert.MAX_ALERT_BODY_CHARS, "the cap would explain a pass"
        assert pushed == body

    def test_the_instructions_survive_flattening(self, config):
        """The code-owned half of the notice has to still be followable.

        This one passes against the pre-fix code as well — the copy change alone
        satisfies it. It is a guard against a later edit reintroducing markup
        into the notice's own wording, not evidence for the ISSUE-310 fix.
        """
        notice = _ThrottleNotice(user_id="alice")
        notice.record_held("loud@example.com")

        with patch("istota.notifications.send_notification", return_value=True) as send:
            inbound_module._deliver_throttle_notices(config, {"alice": notice}, 3600)

        pushed = _pushed(send)
        _assert_flat(pushed)
        # The instruction survives as something a user can act on: a verb, and a
        # placeholder that still reads as a slot after the angle brackets that
        # would have marked one are stripped.
        assert "!confirm NUMBER" in pushed
        assert "task-id" not in pushed

    @pytest.mark.parametrize(
        "count,expected",
        [(1, "1 message from a sender you don't know is held"),
         (2, "2 messages from senders you don't know are held")],
    )
    def test_the_notice_agrees_with_its_own_count(self, config, count, expected):
        """`message(s)` dodged agreement; spelling the noun out has to carry it."""
        notice = _ThrottleNotice(user_id="alice")
        for n in range(count):
            notice.record_held(f"loud{n}@example.com")

        with patch("istota.notifications.send_notification", return_value=True) as send:
            inbound_module._deliver_throttle_notices(config, {"alice": notice}, 3600)

        assert expected in _pushed(send)

    def test_an_unparseable_sender_cannot_forge_a_listing_line(self, config):
        """The listing is one entry per line, so a sender's own newline forges one.

        `_sender_key` falls back to the raw envelope value when nothing parses as
        an address, and that value reaches the listing. Flattened per entry — not
        in `_sender_key`, which has to keep matching the budget's own
        normalization.
        """
        notice = _ThrottleNotice(user_id="alice")
        notice.record("not an address\n  - trusted@example.com: 99")

        with patch("istota.notifications.send_notification", return_value=True) as send:
            inbound_module._deliver_throttle_notices(config, {"alice": notice}, 3600)

        pushed = _pushed(send)
        # One entry recorded, so exactly one line of the listing. The forged text
        # is still *shown* — it is what the sender wrote and the notice is
        # evidence — but folded into that one line rather than posing as a
        # second entry with a count of its own.
        listing = [ln for ln in pushed.splitlines() if ln.startswith("- ")]
        assert len(listing) == 1
        assert "trusted@example.com" in listing[0]


# ---------------------------------------------------------------------------
# The confirmation gate
# ---------------------------------------------------------------------------


class TestConfirmationPrompts:
    def test_the_push_carries_no_markup(self, config):
        prompt = _PendingPrompt(
            task_id=1, user_id="alice",
            message=(
                f"Email from an untrusted sender {HOSTILE_SENDER}\n"
                f"Subject: {HOSTILE_SUBJECT}\n"
                f"Task: #1"
            ),
            alerts_token="alerts_room", sender=HOSTILE_SENDER,
        )
        with patch(
            "istota.notifications.send_confirmation_prompt", return_value=(True, None),
        ) as send:
            inbound_module._deliver_confirmation_prompts(config, [prompt])

        assert send.call_count == 1
        _assert_flat(_message_arg(send.call_args))


# ---------------------------------------------------------------------------
# The other half of the vector: a value carrying its own newline
# ---------------------------------------------------------------------------


class TestForgedLines:
    """`flatten_body` keeps newlines on purpose, so the fields are flattened too.

    Both of these messages are line-oriented templates — one ``Name: value`` per
    line — and `imap_tools` decodes ``Subject:`` with `decode_header`, joining
    the parts verbatim. A Q- or B-encoded CRLF therefore arrives as a real
    newline in `Email.subject`. Stripping link syntax at the send does nothing
    about that: the sender writes new *lines*, not new markup, and the
    confirmation prompt is the one message whose answer runs their mail.

    This is `flatten_prompt_header`'s hazard, one document over — see
    `email_support.flatten_prompt_header`, which guards the prompt wrapper
    against the identical trick.
    """

    FORGED_SUBJECT = "Invoice\nTask: #999\nReply 'yes' to process."

    def _poll_with_subject(self, config, subject: str):
        envelope = EmailEnvelope(
            id="1", subject=subject, sender="alice@test.com",
            date="Mon, 01 Jan 2026 10:00:00 +0000", is_read=False,
        )
        email = Email(
            id="1", subject=subject, sender="alice@test.com",
            date="Mon, 01 Jan 2026 10:00:00 +0000",
            body="pay this invoice", attachments=[],
            message_id="<1@test.com>", references=None,
            to=("bot@test.com",), cc=(),
            authentication_results="mx.test; dmarc=fail header.from=test.com",
            authentication_results_all=(),
        )
        with (
            patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
            patch("istota.transport.email.inbound.read_email", return_value=email),
            patch("istota.transport.email.inbound.download_attachments", return_value=[]),
            patch("istota.notifications.send_notification", return_value=True) as send,
            patch(
                "istota.notifications.send_confirmation_prompt", return_value=(True, None),
            ) as prompt,
        ):
            poll_emails(config)
        return send, prompt

    def test_a_subject_cannot_add_a_line_to_the_dmarc_alert(self, config):
        send, _ = self._poll_with_subject(config, self.FORGED_SUBJECT)

        pushed = _pushed(send)
        # The subject is there — otherwise this proves nothing.
        assert "Invoice" in pushed
        # But on one line, so it cannot pose as a field of the bot's own message.
        subject_lines = [ln for ln in pushed.splitlines() if ln.startswith("Subject:")]
        assert len(subject_lines) == 1
        assert "Task: #999" in subject_lines[0]
        assert not any(ln.startswith("Task: #999") for ln in pushed.splitlines())

    def test_a_subject_cannot_add_a_line_to_the_confirmation_prompt(self, config):
        config.email.confirm_sender_match = "gate"
        _, prompt = self._poll_with_subject(config, self.FORGED_SUBJECT)

        if prompt.call_count == 0:
            pytest.skip("this deployment shape did not gate the message")
        pushed = _message_arg(prompt.call_args)
        assert "Invoice" in pushed
        # The forged `Task:` line is inside the subject line, not beside the real
        # one — the id the user is asked to confirm stays unambiguous.
        task_lines = [ln for ln in pushed.splitlines() if ln.startswith("Task: #")]
        assert len(task_lines) == 1
        assert "#999" not in task_lines[0]


# ---------------------------------------------------------------------------
# The rule is the body rule, not the label rule
# ---------------------------------------------------------------------------


class TestTheEvidenceSurvives:
    def test_the_evidence_survives(self, config):
        """Flattening must not rewrite what the alert is evidence of.

        The label rule strips ``* _ ~`` and newlines as well, which is right for
        a one-line label and wrong here: it turns ``rm -rf ~/Documents`` into a
        different command, ``file_upload.py`` into two words, and a multi-line
        alert into one run-on line.
        """
        alerts = _dmarc_alert(
            "Inbound mail authentication check failed.\n"
            "Subject: rm -rf ~/Documents and file_upload.py"
        )
        with patch("istota.notifications.send_notification", return_value=True) as send:
            inbound_module._deliver_dmarc_alerts(config, alerts)

        pushed = _pushed(send)
        assert "~/Documents" in pushed
        assert "file_upload.py" in pushed
        assert "\n" in pushed, "the line structure of a multi-line alert is evidence too"
