"""Attacker-supplied text on the notification render path.

A gated email's sender and subject are chosen by whoever sent the mail, and the
whole point of the gate is that the *body* has not been approved. Three
properties, all of which the confirmation notification has to carry over from
`confirmations.describe`:

- the stored title is flattened, because delivery puts it into Talk, which
  renders markdown — a subject reading `[click me](http://evil)` would become a
  live link in the user's room, in a message the bot appears to have written;
- nothing in the rendered payload carries markup characters, on any field;
- the withheld body appears nowhere at all.

Driven through the real poller rather than a hand-built row: the title is
composed at the gate, and a test that writes its own title proves only that the
test flattens.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from istota import (
    confirmations,
    db,
    notification_sources as sources,
    notification_store as store,
)
from istota.config import Config, EmailConfig, UserConfig
from istota.skills.email import Email, EmailEnvelope
from istota.transport.email.inbound import poll_emails

BOT = "bot@test.invalid"
PLUS = "bot+alice@test.invalid"
OWN = "alice@example.com"

HOSTILE_SUBJECT = "[click me](http://evil) *bold* `code` <b>x</b> _u_ ~s~ |pipe|"
HOSTILE_SENDER = "stranger@example.invalid"
WITHHELD_BODY = "PLEASE-WIRE-THE-MONEY-TO-ACCOUNT-99"

MARKUP_CHARS = "[]()`*_~<>|"


@pytest.fixture(autouse=True)
def _registry():
    sources.reset_registry()
    yield
    sources.reset_registry()


@pytest.fixture
def config(tmp_path, monkeypatch):
    cfg = Config(
        db_path=tmp_path / "istota.db",
        temp_dir=tmp_path / "temp",
        nextcloud_mount_path=tmp_path / "mount",
        email=EmailConfig(
            enabled=True,
            imap_host="imap.test", imap_port=993,
            imap_user="u", imap_password="p",
            smtp_host="smtp.test", smtp_port=587,
            bot_email=BOT,
        ),
        users={"alice": UserConfig(display_name="Alice", email_addresses=[OWN])},
    )
    (tmp_path / "mount" / "Users" / "alice").mkdir(parents=True)
    db.init_db(cfg.db_path)
    monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
    return cfg


def _poll_hostile_mail(config):
    envelope = EmailEnvelope(
        id="71", subject=HOSTILE_SUBJECT, sender=HOSTILE_SENDER,
        date="Mon, 01 Jan 2026 10:00:00 +0000", is_read=False,
    )
    email = Email(
        id="71", subject=HOSTILE_SUBJECT, sender=HOSTILE_SENDER,
        date="Mon, 01 Jan 2026 10:00:00 +0000",
        body=WITHHELD_BODY, attachments=[],
        message_id="<71@example.invalid>", references=None,
        to=(PLUS,), cc=(),
    )
    with (
        patch("istota.transport.email.inbound.list_emails", return_value=[envelope]),
        patch("istota.transport.email.inbound.read_email", return_value=email),
        patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        patch(
            "istota.notifications.send_confirmation_prompt", return_value=(True, None),
        ),
    ):
        return poll_emails(config)


def _all_text(item) -> str:
    parts = [item.title, item.body, item.status_note or "", item.link or ""]
    for action in item.actions:
        parts += [action.label, action.endpoint or "", action.href or ""]
    return "\n".join(parts)


def test_the_stored_title_is_flattened_at_the_gate(config):
    task_ids = _poll_hostile_mail(config)
    assert len(task_ids) == 1

    with db.get_db(config.db_path) as conn:
        row = conn.execute(
            "SELECT title, body FROM notifications WHERE source = 'confirmation'",
        ).fetchone()

    assert row is not None, "the gate wrote no notification row"
    for char in MARKUP_CHARS:
        assert char not in row["title"], f"{char!r} survived into the stored title"
        assert char not in row["body"], f"{char!r} survived into the stored body"
    assert "\n" not in row["title"]
    assert WITHHELD_BODY not in row["title"]
    assert WITHHELD_BODY not in row["body"]


def test_the_rendered_payload_carries_no_markup_and_no_withheld_body(config):
    task_ids = _poll_hostile_mail(config)
    with db.get_db(config.db_path) as conn:
        items, total = store.list_open(config, conn, "alice")

    assert total == 1 and len(items) == 1
    text = _all_text(items[0])
    for char in MARKUP_CHARS:
        assert char not in text, f"{char!r} reached the panel payload"
    assert WITHHELD_BODY not in text
    assert str(task_ids[0]) in text or items[0].object_id == str(task_ids[0])


def test_the_title_comes_from_describe_not_from_the_task_prompt(config):
    """The gate withholds the body; the notification must not hand it back.

    `tasks.prompt` for a gated email *is* the untrusted message, wrapper and
    all. Building the title from it is the one mistake this whole path exists
    to avoid, so the assertion is against `describe`'s output specifically.
    """
    task_ids = _poll_hostile_mail(config)
    with db.get_db(config.db_path) as conn:
        task = db.get_task(conn, task_ids[0])
        expected = confirmations.describe(conn, task)
        row = conn.execute(
            "SELECT title FROM notifications WHERE source = 'confirmation'",
        ).fetchone()
        items, _ = store.list_open(config, conn, "alice")

    assert WITHHELD_BODY in task.prompt, "the fixture no longer exercises the gate"
    assert row["title"] == expected
    assert items[0].title == expected
