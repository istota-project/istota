"""`istota-credential request-link`: a secret link issued host-side, delivered privately.

Every gate refuses through the task's real proxy socket, and the happy path
stores one link and one queued `secret_link` request whose body is the link,
while the reply to the model carries neither the token nor the address.
"""
import asyncio
import hashlib
import json
import logging
import re
import socket
import tempfile
from pathlib import Path

import pytest

from istota import db
from istota.config import Config, SiteConfig, UserConfig
from istota.credentials import local
from istota.credentials import names as secrets_vault
from istota.credentials import store as secrets_store
from istota.credentials.broker.bindings import parse_binding
from istota.sandbox import credential_shim
from istota.sandbox.skill_proxy import SkillProxy
from istota.webui import secret_links

LINK_RE = re.compile(r"https://zorg\.example/istota/secret\?token=([A-Za-z0-9_-]+)")


@pytest.fixture
def sock():
    directory = Path(tempfile.mkdtemp(prefix="link-proxy-", dir="/tmp"))
    path = directory / "s.sock"
    yield path
    path.unlink(missing_ok=True)
    directory.rmdir()


def _request(path, payload):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as conn:
        conn.settimeout(10)
        conn.connect(str(path))
        conn.sendall(json.dumps(payload).encode() + b"\n")
        data = b""
        while b"\n" not in data:
            chunk = conn.recv(65536)
            if not chunk:
                raise AssertionError("proxy closed without a reply")
            data += chunk
    return json.loads(data)


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    config = Config(
        db_path=tmp_path / "daemon" / "test.db",
        workspace_path=tmp_path / "workspace",
        users={"alice": UserConfig(display_name="Alice"), "bob": UserConfig()},
        site=SiteConfig(hostname="zorg.example"),
    )
    config.security.allow_unsandboxed_multi_user_vaults = True
    config.db_path.parent.mkdir()
    db.init_db(config.db_path)
    return config


def _running(config, *, room=None, source_type="web", shared=False, **kw):
    with db.get_db(config.db_path) as conn:
        if room is None:
            room = db.create_web_chat_room(conn, "alice", "general").token
            if shared:
                db.add_web_room_member(conn, room, "bob")
        ident = db.create_task(conn, user_id="alice", source_type=source_type,
                               prompt="here is my password", conversation_token=room, **kw)
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (ident,))
    return ident, room


def _proxy(config, sock, task_id, *, limit=1):
    return SkillProxy(sock, {}, {}, config=config, user_id="alice", task_id=task_id,
                      vault_credentials={}, vault_write_limit=limit)


def _ask(config, sock, task_id, *, limit=1, **payload):
    body = {"type": "vault_link_request", "name": "acme", "url": "https://acme.example",
            "username": True, **payload}
    with _proxy(config, sock, task_id, limit=limit):
        return _request(sock, body)


def _rows(config, sql, params=()):
    with db.get_db(config.db_path) as conn:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]


def _nothing_issued(config):
    assert _rows(config, "SELECT id FROM secret_links") == []
    assert _rows(config, "SELECT id FROM whatsapp_skill_requests") == []


class TestTheHappyPath:
    def test_one_link_and_one_queued_request_and_the_reply_carries_neither(
            self, config, sock, caplog):
        caplog.set_level(logging.DEBUG)
        task_id, room = _running(config)
        reply = _ask(config, sock, task_id)

        (link,) = _rows(config, "SELECT * FROM secret_links")
        (request,) = _rows(config, "SELECT * FROM whatsapp_skill_requests")
        stored = link["expires_at"]
        assert reply == {"name": "acme", "host": "acme.example",
                         "expires_at": stored[:10] + "T" + stored[11:19] + "Z",
                         "delivered_to": "web"}
        assert link["user_id"] == "alice" and link["intent"] == "credential"
        assert link["task_id"] == task_id and link["room_token"] == room
        assert json.loads(link["params"]) == {
            "name": "acme", "url": "https://acme.example", "username": True}
        assert request["kind"] == "secret_link" and request["state"] == "queued"
        assert request["requester_user_id"] == request["recipient_user_id"] == "alice"

        (token,) = LINK_RE.findall(request["service_body"])
        assert hashlib.sha256(token.encode()).hexdigest() == link["token_hash"]
        assert "acme" in request["service_body"] and "acme.example" in request["service_body"]
        assert "username and password" in request["service_body"]
        assert secret_links.peek(config.db_path, token).id == link["id"]

        for leaked in (token, "/istota/secret"):
            assert leaked not in json.dumps(reply)
            assert leaked not in caplog.text

    def test_the_drain_delivers_it_to_the_private_room_and_nowhere_else(self, config, sock):
        from istota.relay import requests

        task_id, room = _running(config)
        _ask(config, sock, task_id)
        asyncio.run(requests.drain_requests(config))
        (row,) = _rows(config, "SELECT * FROM messages WHERE body LIKE '%/istota/secret?token=%'")
        assert row["room_token"] == room and row["role"] == "system"
        assert row["delivery_reference"].startswith("private-secret_link:")
        assert _rows(config, "SELECT state FROM whatsapp_skill_requests")[0]["state"] == "sent"

    def test_an_existing_local_credential_may_be_asked_for_again(self, config, sock):
        with db.get_db(config.db_path) as conn:
            local.create(conn, "alice", local.LocalCredential(
                name="acme", value="old", url="acme.example"))
        task_id, _ = _running(config)
        # The save keeps the stored binding, so the message and reply name it.
        reply = _ask(config, sock, task_id, username=False, url="https://other.example")
        assert reply["name"] == "acme" and reply["host"] == "acme.example"
        (request,) = _rows(config, "SELECT text FROM whatsapp_skill_requests")
        assert "(acme.example)" in request["text"] and "other.example" not in request["text"]

    def test_a_second_link_for_the_name_supersedes_the_first(self, config, sock):
        task_id, _ = _running(config)
        _ask(config, sock, task_id, limit=2)
        with _proxy(config, sock, task_id, limit=2):
            _request(sock, {"type": "vault_link_request", "name": "acme",
                            "url": "https://acme.example"})
            assert _request(sock, {"type": "vault_link_request", "name": "acme",
                                   "url": "https://acme.example"})["name"] == "acme"
        outcomes = [r["outcome"] for r in _rows(config, "SELECT outcome FROM secret_links ORDER BY id")]
        assert outcomes == ["superseded", "superseded", None]


class TestTheGates:
    def test_the_write_budget_is_spent_first_and_refusals_count(self, config, sock):
        task_id, _ = _running(config)
        assert _ask(config, sock, task_id, limit=0)["reason"] == "vault_write_limit"
        with _proxy(config, sock, task_id, limit=1):
            assert _request(sock, {"type": "vault_link_request", "name": "acme",
                                   "url": "not a url/x?y"})["reason"] == "bad_url"
            assert _request(sock, {"type": "vault_link_request", "name": "acme",
                                   "url": "https://acme.example"})["reason"] == "vault_write_limit"
        _nothing_issued(config)

    def test_a_correspondents_mail_cannot_cause_a_link(self, config, sock):
        task_id, _ = _running(config, source_type="email")
        with db.get_db(config.db_path) as conn:
            db.mark_email_processed(conn, f"uid-{task_id}", "mallory@elsewhere.example",
                                    user_id="alice", task_id=task_id,
                                    routing_method="plus_address")
        assert _ask(config, sock, task_id)["reason"] == "not_owner_turn"
        _nothing_issued(config)

    def test_another_users_task_is_not_the_owners_turn(self, config, sock):
        with db.get_db(config.db_path) as conn:
            ident = db.create_task(conn, user_id="bob", source_type="web", prompt="x")
        assert _ask(config, sock, ident)["reason"] == "not_owner_turn"
        _nothing_issued(config)

    @pytest.mark.parametrize("source_type", ["scheduled", "briefing", "heartbeat"])
    def test_a_scheduled_turn_never_issues_a_link(self, config, sock, source_type):
        task_id, _ = _running(config, source_type=source_type)
        assert _ask(config, sock, task_id)["reason"] == "scheduled_turn"
        _nothing_issued(config)

    def test_an_incognito_room_never_issues_a_link(self, config, sock):
        task_id, _ = _running(config)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE tasks SET incognito = 1 WHERE id = ?", (task_id,))
        assert _ask(config, sock, task_id)["reason"] == "incognito"
        _nothing_issued(config)

    def test_vault_isolation_refuses_with_its_own_text(self, config, sock, monkeypatch):
        config.security.allow_unsandboxed_multi_user_vaults = False
        monkeypatch.setattr("istota.executor._bwrap_available", lambda: False)
        task_id, _ = _running(config)
        reply = _ask(config, sock, task_id)
        assert reply["reason"] == "vault_isolation"
        assert reply["error"] == secrets_vault.VAULT_ISOLATION_REASON
        _nothing_issued(config)

    def test_no_web_origin_means_no_link(self, config, sock):
        config.site.hostname = ""
        task_id, _ = _running(config)
        assert _ask(config, sock, task_id)["reason"] == "no_external_origin"
        _nothing_issued(config)

    @pytest.mark.parametrize("name", ["", "Acme", "generated_acme", "a" * 300, 7])
    def test_a_name_the_store_would_refuse(self, config, sock, name):
        task_id, _ = _running(config)
        assert _ask(config, sock, task_id, name=name)["reason"] == "bad_name"
        _nothing_issued(config)

    # `vault` rows read as local (`bindings.effective_source`): an imported entry is
    # the user's own and Settings edits it the same way, so only these refuse.
    @pytest.mark.parametrize("source", ["generated", "config"])
    def test_an_existing_name_that_is_not_local(self, config, sock, source):
        secrets_store.set_secret(
            config.db_path, "alice", secrets_vault.VAULT_ENTRY_SERVICE, "acme", "kept",
            binding=parse_binding("acme.example", {}, [], source=source),
        )
        task_id, _ = _running(config)
        assert _ask(config, sock, task_id)["reason"] == "not_local"
        _nothing_issued(config)

    def test_a_field_of_another_credential_is_not_local(self, config, sock):
        with db.get_db(config.db_path) as conn:
            local.create(conn, "alice", local.LocalCredential(
                name="acme", value="v", username="me", url="acme.example"))
        task_id, _ = _running(config)
        assert _ask(config, sock, task_id, name="acme_username")["reason"] == "not_local"

    @pytest.mark.parametrize("url", [
        "", None, "https://acme.example/login", "https://acme.example?next=x",
        "https://me@acme.example", " https://acme.example", "ftp://", 5,
    ])
    def test_a_url_that_is_not_a_site(self, config, sock, url):
        task_id, _ = _running(config)
        assert _ask(config, sock, task_id, url=url)["reason"] == "bad_url"
        _nothing_issued(config)

    def test_username_must_be_a_bool(self, config, sock):
        task_id, _ = _running(config)
        assert _ask(config, sock, task_id, username="yes")["reason"] == "bad_request"
        _nothing_issued(config)

    def test_a_shared_room_task_with_no_private_room(self, config, sock):
        task_id, _ = _running(config, shared=True)
        assert _ask(config, sock, task_id)["reason"] == "no_private_room"
        _nothing_issued(config)

    def test_a_shared_room_task_links_into_the_private_room(self, config, sock):
        task_id, shared = _running(config, shared=True)
        with db.get_db(config.db_path) as conn:
            mine = db.create_web_chat_room(conn, "alice", "mine").token
        assert _ask(config, sock, task_id)["delivered_to"] == "web"
        (request,) = _rows(config, "SELECT destination FROM whatsapp_skill_requests")
        assert json.loads(request["destination"])["about"] == shared
        from istota.relay import requests
        asyncio.run(requests.drain_requests(config))
        (row,) = _rows(config, "SELECT room_token FROM messages WHERE body LIKE '%token=%'")
        assert row["room_token"] == mine

    def test_the_live_link_cap(self, config, sock):
        task_id, room = _running(config)
        with db.get_db(config.db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            for index in range(secret_links.MAX_LIVE_PER_USER):
                secret_links.issue(conn, user_id="alice", intent="credential",
                                   params={"name": f"other{index}"}, task_id=task_id,
                                   room_token=room, ttl_seconds=900)
        assert _ask(config, sock, task_id)["reason"] == "too_many_links"
        assert _rows(config, "SELECT id FROM whatsapp_skill_requests") == []
        assert len(_rows(config, "SELECT id FROM secret_links")) == secret_links.MAX_LIVE_PER_USER

    def test_a_store_failure_rolls_back_and_names_no_value(self, config, sock, monkeypatch, caplog):
        from istota.rooms import private_replies

        def broken(*args, **kwargs):
            raise RuntimeError("disk said token=abc")

        monkeypatch.setattr(private_replies, "enqueue_secret_link", broken)
        task_id, _ = _running(config)
        reply = _ask(config, sock, task_id)
        assert reply == {"error": "Could not create the link", "reason": "store_failed"}
        assert "token=abc" not in caplog.text
        _nothing_issued(config)


class TestTheShim:
    def test_request_link_sends_the_request_and_prints_only_metadata(self, monkeypatch, capsys):
        seen = []

        def answer(payload, **kwargs):
            seen.append(payload)
            assert kwargs["timeout"] == credential_shim.CREATE_TIMEOUT_SECONDS
            return {"name": "acme", "host": "acme.example", "expires_at": "2026-11-27 14:15:00",
                    "delivered_to": "whatsapp", "extra": "dropped"}

        monkeypatch.setattr(credential_shim, "_request", answer)
        assert credential_shim.main(
            ["request-link", "acme", "--url", "https://acme.example", "--username"]) == 0
        assert seen == [{"type": "vault_link_request", "name": "acme",
                         "url": "https://acme.example", "username": True}]
        assert json.loads(capsys.readouterr().out) == {
            "name": "acme", "host": "acme.example", "expires_at": "2026-11-27 14:15:00",
            "delivered_to": "whatsapp"}

    @pytest.mark.parametrize("argv", [
        ["request-link"], ["request-link", "acme"], ["request-link", "acme", "--url"],
        ["request-link", "--url", "x"], ["request-link", "acme", "--url", "x", "--bogus"],
    ])
    def test_usage_errors_send_nothing(self, monkeypatch, argv):
        monkeypatch.setattr(credential_shim, "_request", lambda *a, **k: pytest.fail("sent"))
        assert credential_shim.main(argv) == credential_shim.EXIT_REFUSED

    def test_a_refusal_prints_and_exits_one(self, monkeypatch, capsys):
        def refuse(payload, **kwargs):
            raise credential_shim.ProxyError("no private room", "no_private_room")

        monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", "/nonexistent")
        monkeypatch.setattr(credential_shim, "_request", refuse)
        assert credential_shim.main(["request-link", "acme", "--url", "acme.example"]) == 1
        assert "istota-credential: no private room" in capsys.readouterr().err

    def test_the_verb_is_documented(self):
        assert "request-link NAME --url URL [--username]" in credential_shim.USAGE
        assert "Ten verbs" in credential_shim.__doc__
