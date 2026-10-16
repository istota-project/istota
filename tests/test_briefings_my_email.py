"""The personal ``my_email`` briefing source: one connected account per block.

The account is the user's own plus-address in the bot mailbox, or their Gmail
through the Google Workspace connection. The kind is personal-only: group
briefings and shared blocks refuse it on the server.
"""

from __future__ import annotations

import base64
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from istota import db
from istota.briefings import models
from istota.briefings.sources import SourceContext, resolve_source
from istota.config import Config, EmailConfig, GoogleWorkspaceConfig, UserConfig

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=timezone.utc)
GMAIL_READONLY = "https://www.googleapis.com/auth/gmail.readonly"
DRIVE_READONLY = "https://www.googleapis.com/auth/drive.readonly"


def _config(tmp_path: Path, *, google: bool = True) -> Config:
    return Config(
        db_path=tmp_path / "istota.db",
        workspace_path=tmp_path / "mount",
        email=EmailConfig(enabled=True, imap_host="imap.example.com", bot_email="bot@example.com"),
        google_workspace=GoogleWorkspaceConfig(enabled=google, client_id="id", client_secret="secret"),
        users={
            "alice": UserConfig(timezone="UTC", email_addresses=["alice@example.org"]),
            "bob": UserConfig(timezone="UTC"),
        },
    )


@pytest.fixture
def cfg(tmp_path):
    config = _config(tmp_path)
    db.init_db(config.db_path)
    return config


@pytest.fixture
def conn(cfg):
    with db.get_db(cfg.db_path) as connection:
        yield connection


def _grant(conn, user_id: str, scopes: list[str]) -> None:
    conn.execute(
        "INSERT INTO google_oauth_tokens (user_id, access_token, refresh_token, token_expiry, scopes) "
        "VALUES (?, ?, ?, ?, ?)",
        (user_id, b"x", b"x", "2099-01-01T00:00:00", json.dumps(scopes)),
    )
    conn.commit()


def _ctx(cfg, conn, user_id="alice"):
    return SourceContext(app_config=cfg, user_id=user_id, conn=conn, now=NOW)


class _Env:
    def __init__(self, uid, sender, *, to=(), subject="s", date=None):
        self.id = uid
        self.sender = sender
        self.subject = subject
        self.date = date or datetime(2026, 10, 15, 9, 0, tzinfo=timezone.utc)
        self.snippet = "snippet"
        self.to = to
        self.cc = ()
        self.references = None
        self.in_reply_to = None


class _Full:
    def __init__(self, uid, body):
        self.id = uid
        self.body = body


# ---------------------------------------------------------------------------
# The bot mailbox
# ---------------------------------------------------------------------------


class TestBotAccount:
    def test_reads_only_the_users_own_mail_in_the_window(self, cfg, conn, monkeypatch):
        captured = {}
        mine = _Env("1", "carol@example.net", to=("bot+alice@example.com",), subject="Invoice due")
        bobs = _Env("2", "dave@example.net", to=("bot+bob@example.com",), subject="Not alice's")
        shared = _Env("3", "news@example.net", to=("bot@example.com",), subject="Newsletter")
        old = _Env("4", "carol@example.net", to=("bot+alice@example.com",),
                   date=datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc))

        def fake_list(**kw):
            captured["list"] = kw
            return [mine, bobs, shared, old]

        def fake_full(**kw):
            captured["full"] = kw
            return [_Full("1", "Please pay the invoice by Friday.")]

        monkeypatch.setattr("istota.skills.email.list_emails", fake_list)
        monkeypatch.setattr("istota.skills.email.fetch_emails_full", fake_full)

        gs = resolve_source("my_email", {"account": "bot", "lookback_hours": 24}, _ctx(cfg, conn))

        assert gs.ok is True
        assert gs.kind == "my_email"
        assert gs.untrusted is True
        assert [item["subject"] for item in gs.items] == ["Invoice due"]
        assert "pay the invoice" in gs.items[0]["body"]
        criteria = str(captured["list"]["criteria"])
        assert "bot+alice@example.com" in criteria
        assert "SINCE" in criteria
        assert captured["full"]["criteria"] is not None
        assert "4" not in str(captured["full"]["criteria"]).split("UID")[-1].split()

    def test_fails_closed_without_the_framework_db(self, cfg, monkeypatch):
        monkeypatch.setattr(
            "istota.skills.email.list_emails",
            lambda **kw: pytest.fail("no mailbox read without ownership resolution"),
        )
        gs = resolve_source("my_email", {"account": "bot"}, SourceContext(app_config=cfg, user_id="alice", now=NOW))
        assert gs.ok is False

    def test_unknown_account_is_an_empty_source(self, cfg, conn):
        gs = resolve_source("my_email", {"account": "someone-else"}, _ctx(cfg, conn))
        assert gs.ok is False
        assert gs.items == []


# ---------------------------------------------------------------------------
# Gmail
# ---------------------------------------------------------------------------


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")


def _gmail_transport(calls, *, status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if status != 200:
            return httpx.Response(status, json={"error": {"status": "UNAUTHENTICATED"}})
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}, {"id": "m2"}]})
        message_id = request.url.path.rsplit("/", 1)[-1]
        internal = int(NOW.timestamp() * 1000) - 3600_000
        body = {
            "m1": {"mimeType": "multipart/alternative", "parts": [
                {"mimeType": "text/plain", "body": {"data": _b64("Can you sign the lease today?")}},
                {"mimeType": "text/html", "body": {"data": _b64("<p>ignored</p>")}},
            ]},
            "m2": {"mimeType": "text/html", "body": {"data": _b64("<html><body><p>Dentist on Monday</p></body></html>")}},
        }[message_id]
        headers = [
            {"name": "From", "value": f"sender-{message_id}@example.net"},
            {"name": "Subject", "value": f"Subject {message_id}"},
        ]
        return httpx.Response(200, json={
            "id": message_id, "internalDate": str(internal),
            "payload": {**body, "headers": headers},
        })
    return httpx.MockTransport(handler)


class TestGoogleAccount:
    def test_reads_the_inbox_window_with_the_users_token(self, cfg, conn, monkeypatch):
        import istota.briefings.sources.my_email as my_email

        _grant(conn, "alice", [GMAIL_READONLY])
        calls: list[httpx.Request] = []
        monkeypatch.setattr(
            "istota.credentials.google_tokens.access_token_for",
            lambda c, config, uid: f"token-for-{uid}",
        )
        monkeypatch.setattr(my_email, "_gmail_client", lambda: httpx.Client(transport=_gmail_transport(calls)))

        gs = resolve_source("my_email", {"account": "google", "lookback_hours": 24}, _ctx(cfg, conn))

        assert gs.ok is True
        assert gs.untrusted is True
        assert {item["subject"] for item in gs.items} == {"Subject m1", "Subject m2"}
        bodies = {item["subject"]: item["body"] for item in gs.items}
        assert "sign the lease" in bodies["Subject m1"]
        assert "Dentist on Monday" in bodies["Subject m2"]
        assert all(r.headers["Authorization"] == "Bearer token-for-alice" for r in calls)
        query = calls[0].url.params["q"]
        assert "in:inbox" in query
        assert f"after:{int(NOW.timestamp()) - 24 * 3600}" in query
        assert int(calls[0].url.params["maxResults"]) <= my_email.MAX_MESSAGES

    def test_no_request_without_a_gmail_grant(self, cfg, conn, monkeypatch):
        import istota.briefings.sources.my_email as my_email

        _grant(conn, "alice", [DRIVE_READONLY])
        monkeypatch.setattr(my_email, "_gmail_client", lambda: pytest.fail("no Gmail call without the grant"))
        gs = resolve_source("my_email", {"account": "google"}, _ctx(cfg, conn))
        assert gs.ok is False
        assert "gmail" in gs.provenance.lower()

    def test_a_rejected_token_is_an_empty_source(self, cfg, conn, monkeypatch):
        import istota.briefings.sources.my_email as my_email

        _grant(conn, "alice", [GMAIL_READONLY])
        calls: list[httpx.Request] = []
        monkeypatch.setattr("istota.credentials.google_tokens.access_token_for", lambda c, config, uid: "stale")
        monkeypatch.setattr(my_email, "_gmail_client", lambda: httpx.Client(transport=_gmail_transport(calls, status=401)))
        gs = resolve_source("my_email", {"account": "google"}, _ctx(cfg, conn))
        assert gs.ok is False
        assert gs.items == []


# ---------------------------------------------------------------------------
# Listing the connected accounts
# ---------------------------------------------------------------------------


class TestListAccounts:
    def test_bot_address_always_and_gmail_only_with_the_grant(self, cfg, conn):
        from istota.briefings.sources.my_email import list_accounts

        accounts = {a["id"]: a for a in list_accounts(cfg, conn, "alice")}
        assert accounts["bot"]["available"] is True
        assert accounts["bot"]["address"] == "bot+alice@example.com"
        assert accounts["google"]["available"] is False
        assert accounts["google"]["reason"]

        _grant(conn, "alice", [GMAIL_READONLY])
        accounts = {a["id"]: a for a in list_accounts(cfg, conn, "alice")}
        assert accounts["google"]["available"] is True

    def test_gmail_is_absent_when_google_workspace_is_off(self, tmp_path):
        from istota.briefings.sources.my_email import list_accounts

        cfg = _config(tmp_path, google=False)
        db.init_db(cfg.db_path)
        with db.get_db(cfg.db_path) as connection:
            ids = [a["id"] for a in list_accounts(cfg, connection, "alice")]
        assert ids == ["bot"]

    def test_route_lists_the_signed_in_users_accounts(self, cfg, conn):
        from istota.briefings.routes import require_auth, router, verify_origin

        _grant(conn, "bob", [GMAIL_READONLY])
        app = FastAPI()
        app.include_router(router, prefix="/istota/api/briefings")
        app.state.istota_config = cfg
        app.dependency_overrides[require_auth] = lambda: {"username": "alice"}
        app.dependency_overrides[verify_origin] = lambda: None
        body = TestClient(app).get("/istota/api/briefings/email-accounts").json()
        accounts = {a["id"]: a for a in body["accounts"]}
        assert accounts["bot"]["address"] == "bot+alice@example.com"
        assert accounts["google"]["available"] is False  # bob's grant is not alice's


# ---------------------------------------------------------------------------
# Personal only
# ---------------------------------------------------------------------------


class TestPersonalOnly:
    def test_personal_blocks_accept_it(self):
        block = models.normalize_block_payload({
            "briefing_name": "morning", "title": "Inbox",
            "sources": [{"kind": "my_email", "config": {"account": "bot"}}],
        })
        assert block["sources"][0]["kind"] == "my_email"

    def test_shared_blocks_refuse_it(self):
        with pytest.raises(ValueError):
            models.validate_shared_block_payload({
                "name": "inbox", "cron": "0 7 * * *",
                "sources": [{"kind": "my_email", "config": {"account": "bot"}}],
            })

    def test_group_definitions_refuse_it(self):
        assert "my_email" not in models.GROUP_SOURCE_KINDS
        with pytest.raises(ValueError, match="not allowed for group briefings"):
            models._group_sources([{"id": "inbox", "kind": "my_email", "config": {"account": "bot"}}], set())

    def test_the_group_resolver_refuses_it(self, cfg, conn, monkeypatch):
        from istota.briefings.sources.group import GroupSourceContext, _resolve_stored

        monkeypatch.setattr(
            "istota.skills.email.list_emails",
            lambda **kw: pytest.fail("a group read reached the mailbox"),
        )
        ctx = GroupSourceContext(group_id="team", definition_id=1, config=cfg, now=NOW)
        source = {"id": "inbox", "kind": "my_email", "config": {"account": "bot"}}
        gs = _resolve_stored(source, ctx, conn, {"timezone": "UTC"})
        assert gs.ok is False
        assert gs.failure_kind == "refused"

    def test_the_preset_asks_for_action_items(self):
        template = next(t for t in models.BLOCK_TEMPLATES if t["id"] == "my_email")
        assert template["block"]["sources"] == [{"kind": "my_email", "config": {}, "enabled": True}]
        assert "action items" in template["block"]["directive"]

    def test_an_empty_config_reads_the_bot_account(self, cfg, conn, monkeypatch):
        captured = {}
        monkeypatch.setattr("istota.skills.email.list_emails", lambda **kw: captured.update(kw) or [])
        resolve_source("my_email", {}, _ctx(cfg, conn))
        assert "bot+alice@example.com" in str(captured["criteria"])
