"""The `shared_source` notification: one fixed-text bell row per user for pending offers.

An OCM offer's folder name, owner and host are chosen by whoever sent it, and
an alert push may be routed into a shared room, so no word of them may reach
a push (#638). The real path is driven: `receive_share` writes the row and
hands the push to the daemon in its own transaction, and the daemon's
`deliver_handed_off` is what calls `send_notification`, which is the one
thing replaced here. The bell's detail view is where the offers are listed.
"""

from __future__ import annotations

import pytest

from istota import db
from istota.notifications import sources as notification_sources
from istota.notifications import store
from istota.notifications.resolvers import shared_source
from istota.sharing import sources
from tests.test_sharing_ocm import (  # noqa: F401 -- shared fixtures
    Sender, _offers, _receive, config, fresh_limits, secret_key_env, sender,
)

FOLDER = "Confidential Merger Plans"
OWNER = "eve-the-sender"


@pytest.fixture
def sends(monkeypatch):
    calls: list[dict] = []

    def fake(_config, user_id, message, **kwargs):
        calls.append({"user_id": user_id, "message": message, **kwargs})
        return True

    monkeypatch.setattr("istota.notifications.delivery.send_notification", fake)
    return calls


def _rows(config):  # noqa: F811
    with db.get_db(config.db_path) as conn:
        return conn.execute(
            "SELECT user_id, dedup_key, title, body, occurrences, state FROM notifications"
            " WHERE source = 'shared_source'"
        ).fetchall()


def _offer(sender, provider_id=5):  # noqa: F811
    host = sender.host
    return sender.offer(providerId=provider_id, name=FOLDER,
                        owner=f"{OWNER}@http://{host}", sharedBy=f"{OWNER}@http://{host}",
                        ownerDisplayName=OWNER, sharedByDisplayName=OWNER)


def test_one_row_per_user_bumped_by_each_offer(config, sender):  # noqa: F811
    assert _receive(config, _offer(sender, 5)).status == 201
    assert _receive(config, _offer(sender, 6)).status == 201
    [row] = _rows(config)
    assert (row["user_id"], row["dedup_key"], row["state"]) == ("alice", "pending-offers", "open")
    assert row["occurrences"] == 2


def test_the_push_is_fixed_text_with_no_sender_word(config, sender, sends):  # noqa: F811
    assert _receive(config, _offer(sender)).status == 201
    assert sends == []  # the web process hands the push over, it does not send
    store.deliver_handed_off(config)
    [sent] = sends
    assert sent["user_id"] == "alice"
    assert sent["message"] == f"{shared_source.TITLE}\n\n{shared_source.BODY}"
    for word in (FOLDER, OWNER, sender.host, "127.0.0.1", "Merger"):
        assert word not in sent["message"]
        assert word not in (sent.get("title") or "")
    [row] = _rows(config)
    assert FOLDER not in row["title"] + row["body"]


def test_a_second_offer_pushes_nothing_more(config, sender, sends):  # noqa: F811
    _receive(config, _offer(sender, 5))
    store.deliver_handed_off(config)
    _receive(config, _offer(sender, 6))
    store.deliver_handed_off(config)
    assert len(sends) == 1


def _panel(config):  # noqa: F811
    notification_sources.reset_registry()
    with db.get_db(config.db_path) as conn:
        rows, total = store.list_open(config, conn, "alice")
        conn.commit()
    return [r for r in rows if r.source == "shared_source"], total


def test_the_detail_lists_each_pending_offer(config, sender):  # noqa: F811
    _receive(config, _offer(sender))
    [row], _ = _panel(config)
    assert row.title == shared_source.TITLE
    assert FOLDER in row.body
    assert OWNER in row.body
    assert sender.host in row.body


def test_a_name_cannot_forge_a_line_in_the_detail(config, sender):  # noqa: F811
    body = _offer(sender)
    body["name"] = "Plans\nA second offer from someone you trust"
    _receive(config, body)
    [row], _ = _panel(config)
    assert len(row.body.splitlines()) == 1


def test_the_row_closes_when_no_offer_is_pending(config, sender):  # noqa: F811
    _receive(config, _offer(sender))
    [offer] = _offers(config)
    with db.get_db(config.db_path) as conn:
        sources.set_state(conn, offer.id, "alice", "declined", reason="test_setup")
        conn.commit()
    rows, _ = _panel(config)
    assert rows == []
    [stored] = _rows(config)
    assert stored["state"] != "open"


def test_a_re_offer_of_an_active_share_keeps_it_open(config, sender):  # noqa: F811
    _receive(config, _offer(sender))
    [offer] = _offers(config)
    with db.get_db(config.db_path) as conn:
        sources.set_state(conn, offer.id, "alice", "active")
        conn.commit()
    assert _panel(config)[0] == []
    _receive(config, _offer(sender))
    [row], _ = _panel(config)
    assert FOLDER in row.body
