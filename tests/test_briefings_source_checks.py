"""Per-kind source checks on the skill path (`briefings/source_checks.py`)."""

from __future__ import annotations

import json

import pytest

from istota import db
from istota.briefings import source_checks
from istota.briefings.source_checks import SourceRefused, check_source
from istota.briefings.sources.kv import SHARED_BLOCK_NAMESPACE
from istota.briefings.sources.my_email import account_refusal
from istota.config import Config, UserConfig


@pytest.fixture
def cfg(tmp_path):
    config = Config(
        db_path=tmp_path / "istota.db",
        workspace_path=tmp_path / "mount",
        users={"alice": UserConfig(timezone="UTC"), "bob": UserConfig(timezone="UTC")},
    )
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        db.upsert_shared_block_config(conn, name="world-headlines", cron="0 6 * * *")
        db.shared_kv_set(
            conn, SHARED_BLOCK_NAMESPACE, "custom-digest",
            json.dumps({"text": "hi"}), "root",
        )
        conn.commit()
    return config


@pytest.fixture
def bob_account(cfg, monkeypatch):
    from istota.mail import accounts

    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    with db.get_db(cfg.db_path) as conn:
        home, _ = accounts.ensure_account(
            conn, "bob", slug="home", label="Home", imap_host="imap.bob.example",
            imap_username="bob@bob.example",
        )
        conn.commit()
    return home.id


ACCEPTED = [
    ("rss", {"feed_ref": {"kind": "category", "value": 4}, "limit": 10}),
    ("email", {"mode": "senders", "senders": ["news@example.com"], "lookback_hours": 24}),
    ("my_email", {"account": "bot"}),
    ("browse", {"preset": "bbc"}),
    ("browse", {"url": "https://example.com/news"}),
    ("todos", {"path": "TODO.md"}),
    ("reminders", {"path": "shared/reminders.md"}),
    ("notes", {"path": "notes/today.md"}),
    ("shared_block", {"name": "world-headlines", "max_age_hours": 14}),
    ("shared_block", {"name": "custom-digest"}),
    ("markets", {}),
    ("calendar", {}),
]

REFUSED = [
    ("rss", {}, "feed_ref"),
    ("rss", {"feed_ref": {"kind": "url", "value": 4}}, "feed_ref"),
    ("rss", {"feed_ref": {"kind": "category", "value": 0}}, "feed_ref"),
    ("rss", {"feed_ref": {"kind": "category", "value": True}}, "feed_ref"),
    ("rss", {"feed_ref": {"kind": "category", "value": 4}, "limit": 0}, "limit"),
    ("email", {"mode": "everything"}, None),
    ("email", {"surprise": 1}, None),
    ("my_email", {"account": "outlook"}, "account"),
    ("browse", {}, None),
    ("browse", {"preset": "bbc", "url": "https://example.com"}, None),
    ("browse", {"preset": "no-such-preset"}, "preset"),
    ("browse", {"url": "file:///etc/passwd"}, "url"),
    ("todos", {}, "path"),
    ("reminders", {"path": "   "}, "path"),
    ("notes", {"path": 3}, "path"),
    ("shared_block", {}, "name"),
    ("shared_block", {"name": "Not A Slug"}, "name"),
    ("shared_block", {"name": "no-such-block"}, "name"),
    ("shared_block", {"name": "world-headlines", "max_age_hours": -1}, "max_age_hours"),
    ("bogus", {}, "kind"),
]


@pytest.mark.parametrize(("kind", "config"), ACCEPTED)
def test_accepted_configs(cfg, kind, config):
    check_source(cfg, "alice", kind, config)


@pytest.mark.parametrize(("kind", "config", "field"), REFUSED)
def test_refused_configs(cfg, kind, config, field):
    with pytest.raises(SourceRefused) as exc:
        check_source(cfg, "alice", kind, config)
    assert exc.value.reason == "invalid_source"
    assert exc.value.field == field


def test_a_config_that_is_not_an_object_is_refused(cfg):
    with pytest.raises(SourceRefused) as exc:
        check_source(cfg, "alice", "markets", ["not", "an", "object"])
    assert exc.value.reason == "invalid_source"


def test_another_users_mail_account_is_refused_with_the_web_message(cfg, bob_account):
    account = f"imap:{bob_account}"
    with pytest.raises(SourceRefused) as exc:
        check_source(cfg, "alice", "my_email", {"account": account})
    assert exc.value.reason == "mail_account_refused"
    assert exc.value.field == "account"
    assert str(exc.value) == account_refusal(cfg, "alice", account)


def test_an_unchanged_stored_account_passes(cfg, bob_account):
    account = f"imap:{bob_account}"
    check_source(
        cfg, "alice", "my_email", {"account": account, "lookback_hours": 48},
        stored={"account": account, "lookback_hours": 24},
    )


def test_rss_names_one_of_the_users_feeds_when_feeds_answers(cfg, monkeypatch):
    monkeypatch.setattr(source_checks, "_feed_options", lambda app_config, user_id: {
        "available": True,
        "subscriptions": [{"kind": "subscription", "value": 7, "label": "A"}],
        "categories": [{"kind": "category", "value": 4, "label": "News"}],
    })
    check_source(cfg, "alice", "rss", {"feed_ref": {"kind": "subscription", "value": 7}})
    check_source(cfg, "alice", "rss", {"feed_ref": {"kind": "category", "value": 4}})
    with pytest.raises(SourceRefused) as exc:
        check_source(cfg, "alice", "rss", {"feed_ref": {"kind": "subscription", "value": 4}})
    assert exc.value.field == "feed_ref"


def test_rss_shape_only_when_feeds_is_unavailable(cfg):
    # No Feeds module for this user: the route soft-degrades, so does the check.
    check_source(cfg, "alice", "rss", {"feed_ref": {"kind": "subscription", "value": 99}})


def test_shared_block_needs_the_app_config(cfg):
    with pytest.raises(SourceRefused) as exc:
        check_source(None, "alice", "shared_block", {"name": "world-headlines"})
    assert exc.value.reason == "unavailable"
