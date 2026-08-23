"""The DMARC canary's inbox row is keyed on the verdict, never on the sender.

``_dmarc_alerted`` — the 24-hour in-process window — keys on
``(user_id, sender, verdict)`` and is safe doing so: it is a dict that clears on
restart, so the worst a hostile sender axis costs there is memory until the next
restart. A ``dedup_key`` is not that. It is durable, unique, and every distinct
value is a row in the user's bell that fires its own push and stays until somebody
clears it by hand. ``inbound.py`` states the rule twenty lines above
``_dmarc_alerted`` for ``_authserv_id_suggested``: an attacker-chosen value is an
unbounded axis.

The sender on this path is a value the forger presented. Today
``sender_claims_to_be_user`` narrows it to an exact lowercase match against the
user's own configured addresses, so the axis is *currently* small — but that
predicate's own docstring records the plain-match choice as deliberate and
reversible, and the whole point of the canary is that the header it reads is
unauthenticated. A durable key must not rest on either fact.

So: one row per verdict, the senders in ``params``, and that list bounded too —
fifty entries in a JSON blob is the same unbounded axis one level down.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from istota import db, notification_sources as sources
from istota.config import Config, EmailConfig as AppEmailConfig, UserConfig
from istota.notification_resolvers import task_alert
from istota.skills.email import Email, EmailEnvelope
from istota.transport.email import inbound as inbound_module
from istota.transport.email.inbound import _DmarcAlert, poll_emails


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


def _rows(config):
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT * FROM notifications WHERE source = 'task_alert' "
            "AND dedup_key LIKE 'dmarc:%' ORDER BY id",
        ).fetchall()


def _params(config, notification_id):
    with db.get_db(config.db_path) as conn:
        raw = conn.execute(
            "SELECT params FROM notifications WHERE id = ?", (notification_id,),
        ).fetchone()[0]
    return json.loads(raw)


FORGED = [f"attacker{n:02d}@evil.example" for n in range(50)]


def _alerts(senders, *, verdict="fail", user_id="alice"):
    """The dict `poll_emails` accumulates, keyed exactly as the poller keys it."""
    out = {}
    for sender in senders:
        key = (user_id, sender.lower(), verdict)
        out[key] = _DmarcAlert(
            key=key, user_id=user_id, verdict=verdict, sender=sender,
            message=(
                "Mail from "
                f"{sender} routed as sender_match on the strength of the From: "
                f"header, but arrived with dmarc={verdict}."
            ),
        )
    return out


# ---------------------------------------------------------------------------
# The bound itself, at the seam that decides the key.
# ---------------------------------------------------------------------------


class TestFiftyForgedSendersProduceOneRow:
    def test_fifty_senders_one_row(self, config):
        alerts = _alerts(FORGED)
        assert len(alerts) == 50

        with patch("istota.notifications.send_notification", return_value=True):
            inbound_module._deliver_dmarc_alerts(config, alerts)

        rows = _rows(config)
        assert len(rows) == 1, [r["dedup_key"] for r in rows]
        assert rows[0]["dedup_key"] == "dmarc:fail"
        assert rows[0]["user_id"] == "alice"
        assert rows[0]["state"] == "open"
        # Fifty alerts, one row, fifty occurrences. This is the deduplication a
        # chat channel structurally cannot do.
        assert rows[0]["occurrences"] == 50

    def test_the_senders_are_in_params_and_bounded(self, config):
        with patch("istota.notifications.send_notification", return_value=True):
            inbound_module._deliver_dmarc_alerts(config, _alerts(FORGED))

        params = _params(config, _rows(config)[0]["id"])
        assert params["verdict"] == "fail"

        senders = params["senders"]
        # The senders are there — dropping them from the key must not cost the
        # operator the one thing that makes the row worth reading.
        assert FORGED[0] in senders
        # And bounded, keeping the *first* seen: the earliest evidence survives
        # and the stored value is stable across occurrences rather than churning
        # on every forged message.
        assert senders == FORGED[: task_alert.MAX_PARAM_ENTRIES]
        assert params["senders_omitted"] == 50 - task_alert.MAX_PARAM_ENTRIES

    def test_a_repeated_sender_does_not_grow_the_list(self, config):
        with patch("istota.notifications.send_notification", return_value=True):
            for _ in range(5):
                inbound_module._reset_dmarc_alert_dedup()
                inbound_module._deliver_dmarc_alerts(config, _alerts(FORGED[:1]))

        params = _params(config, _rows(config)[0]["id"])
        assert params["senders"] == FORGED[:1]
        assert "senders_omitted" not in params

    def test_a_different_verdict_is_a_different_row(self, config):
        with patch("istota.notifications.send_notification", return_value=True):
            inbound_module._deliver_dmarc_alerts(config, _alerts(FORGED[:1]))
            inbound_module._deliver_dmarc_alerts(
                config, _alerts(FORGED[1:2], verdict="none"),
            )

        # The verdict is the bounded axis, and it is the one that carries meaning:
        # `none` says the DMARC record was edited away, `fail` says the mail
        # genuinely failed policy. Collapsing those two would lose the diagnosis.
        assert sorted(r["dedup_key"] for r in _rows(config)) == [
            "dmarc:fail", "dmarc:none",
        ]

    def test_a_hostile_verdict_cannot_forge_a_key(self, config):
        """The verdict is parsed out of a header, so it is slugged like any other."""
        assert task_alert.dmarc_key("fail") == "dmarc:fail"
        assert ":" not in task_alert.dmarc_key("a:b")[len("dmarc:"):]
        assert len(task_alert.dmarc_key("z" * 500)) < 60
        assert task_alert.dmarc_key("") == "dmarc:other"

    def test_the_sender_appears_in_the_delivered_text(self, config):
        with patch("istota.notifications.send_notification", return_value=True) as send:
            inbound_module._deliver_dmarc_alerts(config, _alerts(FORGED[:1]))
        assert FORGED[0] in send.call_args.args[2]

    def test_delivery_still_fires_once_per_sender(self, config):
        """The in-process window is the delivery gate, not the row's branch."""
        with patch("istota.notifications.send_notification", return_value=True) as send:
            inbound_module._deliver_dmarc_alerts(config, _alerts(FORGED[:5]))

        # Five distinct window keys, five pushes — unchanged from before the inbox
        # existed. Were the store's own dedup branch the gate, senders two through
        # five would be silent, because they land on an already-open row.
        assert send.call_count == 5
        assert len(_rows(config)) == 1


class TestTheRowSurvivesAFailedDelivery:
    def test_no_destination_leaves_the_row_open_and_undelivered(self, config):
        """`send_notification` returning False is the case the inbox exists for."""
        with patch("istota.notifications.send_notification", return_value=False) as send:
            inbound_module._deliver_dmarc_alerts(config, _alerts(FORGED[:1]))

        assert send.call_count == 1
        row = _rows(config)[0]
        assert row["state"] == "open"
        assert row["last_delivered_at"] is None

    def test_a_failed_send_does_not_open_the_window(self, config):
        """The row is durable; the window still retries on the next occurrence."""
        with patch("istota.notifications.send_notification", return_value=False):
            inbound_module._deliver_dmarc_alerts(config, _alerts(FORGED[:1]))
        assert inbound_module._dmarc_alerted == {}

    def test_a_successful_delivery_stamps_the_row(self, config):
        with patch("istota.notifications.send_notification", return_value=True):
            inbound_module._deliver_dmarc_alerts(config, _alerts(FORGED[:1]))
        assert _rows(config)[0]["last_delivered_at"] is not None


# ---------------------------------------------------------------------------
# And once through the real poller, so the wiring is not assumed.
# ---------------------------------------------------------------------------


def _envelope(uid: str) -> EmailEnvelope:
    return EmailEnvelope(
        id=uid, subject="Urgent", sender="alice@test.com",
        date="Mon, 01 Jan 2026 10:00:00 +0000", is_read=False,
    )


def _email(uid: str, verdict: str = "fail") -> Email:
    return Email(
        id=uid, subject="Urgent", sender="alice@test.com",
        date="Mon, 01 Jan 2026 10:00:00 +0000",
        body="pay this invoice", attachments=[],
        message_id=f"<{uid}@test.com>", references=None,
        to=("bot@test.com",), cc=(),
        authentication_results=f"mx.test; dmarc={verdict} header.from=test.com",
        authentication_results_all=(),
    )


def _poll_one(config, uid: str, *, delivered=True):
    with (
        patch("istota.transport.email.inbound.list_emails",
              return_value=[_envelope(uid)]),
        patch("istota.transport.email.inbound.read_email",
              return_value=_email(uid)),
        patch("istota.transport.email.inbound.download_attachments", return_value=[]),
        patch("istota.notifications.send_notification", return_value=delivered) as send,
    ):
        poll_emails(config)
    return send


class TestThroughThePoller:
    def test_a_failing_message_leaves_a_row(self, config):
        send = _poll_one(config, "1")
        assert send.call_count == 1

        rows = _rows(config)
        assert len(rows) == 1
        assert rows[0]["dedup_key"] == "dmarc:fail"
        assert rows[0]["last_delivered_at"] is not None
        # No link, ever — this whole source is unlinkable by construction.
        assert rows[0]["link"] is None
        assert rows[0]["object_id"] is None

    def test_a_second_message_from_the_same_sender_is_window_suppressed(self, config):
        first = _poll_one(config, "1")
        second = _poll_one(config, "2")
        assert first.call_count == 1
        assert second.call_count == 0
        # And the window suppressing the *send* means no second occurrence to
        # record: the row counts alerts raised, not messages seen.
        assert _rows(config)[0]["occurrences"] == 1
