"""One approved recovery fill spends one code through the private proxy."""
import json

import pytest

from istota import confirmations, db
from istota.config import Config, UserConfig
from istota.credentials import generated
from istota.sandbox.skill_proxy import SkillProxy
from tests.test_skill_proxy_otp import ask
from tests import test_skill_proxy_vault_create as _vault_create

sock = _vault_create.sock

NAME = "generated_acme"
CODES = ["sentinel-rc-1111", "sentinel-rc-2222", "sentinel-rc-3333"]


@pytest.fixture
def recovery_env(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    config = Config(db_path=tmp_path / "data.db", users={"alice": UserConfig()})
    db.init_db(config.db_path)
    with db.get_db(config.db_path) as conn:
        task = db.create_task(conn, user_id="alice", prompt="Sign in", source_type="web")
        db.update_task_status(conn, task, "running")
        generated.create(conn, "alice", name=NAME, username="alice", password="fixture-password",
                         url="https://acme.example")
        generated.set_recovery(conn, "alice", NAME, "\n".join(CODES), fmt="codes")
    monkeypatch.setattr("istota.notifications.store.deliver_pending", lambda *_: None)
    return config, task


def fill(proxy, sock, host="acme.example"):
    return ask(proxy, sock, {"type": "vault_recovery_fill", "name": NAME, "host": host}, True)


def proxy_for(env, sock):
    config, task = env
    return SkillProxy(sock, {}, {}, config=config, user_id="alice", task_id=task,
                      vault_credentials={NAME: "fixture-password"}, vault_write_limit=10)


def park(conn, task):
    from istota.relay.requests import associate_confirmation, held_question
    held = held_question(conn, task)
    assert held["kind"] == "recovery_fill"
    db.set_task_confirmation(conn, task, held["preview"])
    associate_confirmation(conn, actor_user_id="alice", task_id=task,
                           request_id=held["id"], preview_digest=held["preview_digest"])
    return db.get_task(conn, task)


def test_approve_claim_and_hold_again(recovery_env, sock, caplog):
    config, task = recovery_env
    with proxy_for(recovery_env, sock) as proxy:
        assert fill(proxy, sock) == {"held": True}
        assert fill(proxy, sock) == {"held": True}
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT count(*) FROM recovery_fill_authorizations").fetchone()[0] == 1
        pending = park(conn, task)
        assert confirmations.describe_title(conn, pending) == "Recovery code waiting for approval"
        confirmations.approve(conn, pending, config=config, by="web")
        db.update_task_status(conn, task, "running")
    with proxy_for(recovery_env, sock) as proxy:
        assert fill(proxy, sock) == {"code": CODES[0], "bound_hosts": ["acme.example"], "remaining": 2}
        assert fill(proxy, sock) == {"held": True}
    with db.get_db(config.db_path) as conn:
        assert generated.recovery_state(conn, "alice", NAME)["spent"] == [0]
        assert conn.execute("SELECT state FROM recovery_fill_authorizations ORDER BY id").fetchone()[0] == "used"
        audit = [dict(r) for r in conn.execute("SELECT * FROM credential_audit")]
        assert [r["action"] for r in audit] == ["recovery_fill_approved", "recovery_fill"]
        assert audit[0]["actor"] == "web:alice"
        assert conn.execute("SELECT 1 FROM notifications WHERE dedup_key='vault-recovery-low:generated_acme'").fetchone()
    for code in CODES:
        assert code not in json.dumps(audit) + caplog.text


@pytest.mark.parametrize("source", ["scheduled", "briefing", "heartbeat", "subtask", "cli", "cron", "relay", "unknown"])
def test_noninteractive_refused_even_after_capture(recovery_env, sock, source):
    config, task = recovery_env
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE tasks SET source_type=? WHERE id=?", (source, task))
    with proxy_for(recovery_env, sock) as proxy:
        proxy._captured_codes_this_attempt[NAME] = "acme.example"
        assert fill(proxy, sock)["reason"] == "recovery_fill_not_interactive"
    with db.get_db(config.db_path) as conn:
        assert generated.recovery_state(conn, "alice", NAME)["spent"] == []


@pytest.mark.parametrize("case,reason", [("format", "recovery_fill_format"), ("empty", "recovery_none_left"),
                                         ("host", "credential_origin_mismatch"), ("grant", "credential_not_granted"),
                                         ("source", "recovery_set_not_generated"), ("reach", "vault_credential_not_present")])
def test_refusals(recovery_env, sock, case, reason):
    config, task = recovery_env
    with db.get_db(config.db_path) as conn:
        if case == "format":
            generated.set_recovery(conn, "alice", NAME, "some older block", fmt="block")
        elif case == "empty":
            conn.execute("UPDATE recovery_code_state SET spent='[0,1,2]'")
        elif case == "source":
            conn.execute("UPDATE credential_bindings SET source='local'")
    if case == "grant":
        config.security.credential_broker.enabled = True
    with proxy_for(recovery_env, sock) as proxy:
        if case == "reach":
            proxy.vault_credentials.clear()
        assert fill(proxy, sock, "other.example" if case == "host" else "acme.example")["reason"] == reason


@pytest.mark.parametrize("source,host,exempt", [("page", "acme.example", True), ("download", "acme.example", True),
                                               ("stdin", "acme.example", False), ("page", "other.example", False),
                                               ("page", None, False)])
def test_enrollment_once_with_checked_host(recovery_env, sock, source, host, exempt):
    config, task = recovery_env
    capture = {"type": "vault_secret_capture", "name": NAME, "kind": "codes", "text": "\n".join(CODES),
               "source": source, "host": host}
    with proxy_for(recovery_env, sock) as proxy:
        assert "shape" in ask(proxy, sock, capture, True)
        first = fill(proxy, sock)
        assert (first.get("code") == CODES[0]) is exempt
        # Capturing again does not renew the exemption.
        assert "shape" in ask(proxy, sock, capture, True)
        assert fill(proxy, sock) == {"held": True}
    with db.get_db(config.db_path) as conn:
        uses = [json.loads(r[0]) for r in conn.execute("SELECT detail_json FROM credential_audit WHERE action='recovery_fill'")]
        assert len(uses) == int(exempt)
        if exempt:
            assert uses[0]["exemption"] == "enrollment"


def test_capture_in_previous_attempt_needs_approval(recovery_env, sock):
    with proxy_for(recovery_env, sock) as proxy:
        assert "shape" in ask(proxy, sock, {"type": "vault_secret_capture", "name": NAME, "kind": "codes",
                                             "text": "\n".join(CODES), "source": "page", "host": "acme.example"}, True)
    with proxy_for(recovery_env, sock) as proxy:
        assert fill(proxy, sock) == {"held": True}


@pytest.mark.parametrize("action,state", [("decline", "declined"), ("cancel", "cancelled"), ("expire", "expired")])
def test_lifecycle_closes_without_spending(recovery_env, sock, action, state):
    config, task = recovery_env
    with proxy_for(recovery_env, sock) as proxy:
        assert fill(proxy, sock) == {"held": True}
    with db.get_db(config.db_path) as conn:
        pending = park(conn, task)
        if action == "decline":
            confirmations.decline(conn, pending)
        elif action == "cancel":
            db.cancel_task(conn, task)
        else:
            conn.execute("UPDATE tasks SET updated_at=datetime('now','-3 hours') WHERE id=?", (task,))
            db.expire_stale_confirmations(conn, 120)
        assert conn.execute("SELECT state FROM recovery_fill_authorizations").fetchone()[0] == state
        assert generated.recovery_state(conn, "alice", NAME)["spent"] == []
        assert db.get_task(conn, task).status == "cancelled"


def test_authorized_expiry_does_not_spend(recovery_env, sock):
    config, task = recovery_env
    with proxy_for(recovery_env, sock) as proxy:
        assert fill(proxy, sock) == {"held": True}
    with db.get_db(config.db_path) as conn:
        confirmations.approve(conn, park(conn, task), config=config, by="talk")
        conn.execute("UPDATE recovery_fill_authorizations SET expires_at='2000-01-01 00:00:00'")
        db.update_task_status(conn, task, "running")
    with proxy_for(recovery_env, sock) as proxy:
        assert fill(proxy, sock) == {"held": True}
    with db.get_db(config.db_path) as conn:
        assert [r[0] for r in conn.execute("SELECT state FROM recovery_fill_authorizations ORDER BY id")] == ["expired", "held"]
        assert generated.recovery_state(conn, "alice", NAME)["spent"] == []


@pytest.mark.parametrize("shared", [False, True])
def test_scheduler_parks_and_bell_respects_private_preview(recovery_env, monkeypatch, shared):
    from unittest.mock import patch
    from istota.credentials import recovery_fill
    from istota.notifications import store
    from istota.scheduler import process_one_task
    config, task = recovery_env
    with db.get_db(config.db_path) as conn:
        room = db.create_web_chat_room(conn, "alice", "general").token
        if shared:
            db.add_room_member(conn, room, "bob")
        conn.execute("UPDATE tasks SET status='pending',conversation_token=?,output_target='room' WHERE id=?", (room, task))

    def execute(*args, **kwargs):
        with db.get_db(config.db_path) as conn:
            recovery_fill.request(conn, user_id="alice", task_id=task, name=NAME, host="acme.example")
        return True, "Waiting for approval.", None, None

    with patch("istota.scheduler.execute_task", side_effect=execute), patch("istota.notifications.delivery.send_notification", return_value=True):
        process_one_task(config)
    with db.get_db(config.db_path) as conn:
        pending = db.get_task(conn, task)
        assert pending.status == "pending_confirmation"
        assert NAME in pending.confirmation_prompt
        views, _ = store.list_open(config, conn, "alice")
        assert views[0].title == "Recovery code waiting for approval"
        from istota.rooms.private_replies import preview_rooms
        rooms = preview_rooms(conn, pending)
        assert (not rooms) == any(a.id == "confirm" for a in views[0].actions)
        if shared:
            assert room not in rooms
            assert all(NAME not in r[0] for r in conn.execute("SELECT body FROM messages WHERE room_token=?", (room,)))
        else:
            assert room in rooms


@pytest.mark.parametrize("approved", [False, True])
def test_browse_fill_private_channel_and_scrub(recovery_env, sock, monkeypatch, capsys, approved):
    import httpx
    from istota.skills import browse
    config, task = recovery_env
    if approved:
        with proxy_for(recovery_env, sock) as proxy:
            assert fill(proxy, sock) == {"held": True}
        with db.get_db(config.db_path) as conn:
            confirmations.approve(conn, park(conn, task), config=config, by="web")
            db.update_task_status(conn, task, "running")
    posts = []

    def browser_request(method, url, **kwargs):
        if url.endswith("/health"):
            return httpx.Response(200, json={"per_user_profiles": True, "credential_origin_check": True})
        if method == "get":
            return httpx.Response(200, json={"session_id": "s1", "url": "https://acme.example/recover"})
        posts.append(kwargs["json"])
        return httpx.Response(200, json={"status": "ok", "session_id": "s1", "user_scope": "alice",
                                         "actions": [{"ok": True}], "text": CODES[0]})

    monkeypatch.setattr(browse, "browser_request", browser_request)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    with proxy_for(recovery_env, sock) as proxy, proxy._credential_channel(10) as fd:
        monkeypatch.setenv("ISTOTA_CRED_FD", str(fd))
        args = browse.parse_and_resolve(browse.build_parser(), ["interact", "s1", "--fill-recovery", "#code=" + NAME])
        result = browse.cmd_interact(args)
    if approved:
        assert result["status"] == "ok"
        assert posts[0]["actions"] == [{"type": "fill", "selector": "#code", "value": CODES[0],
                                         "credential": True, "bound_hosts": ["acme.example"]}]
        assert CODES[0] not in json.dumps(result)
    else:
        assert result == {"status": "held", "held": True}
        assert posts == []
    assert CODES[0] not in capsys.readouterr().out


def test_recovery_kind_upgrade_preserves_references(tmp_path):
    import sqlite3
    from pathlib import Path
    from tests.test_whatsapp_requests import _request_shape
    old, fresh = tmp_path / "old.db", tmp_path / "fresh.db"
    schema = (Path(__file__).parents[1] / "schema.sql").read_text().replace(", 'recovery_fill'", "")
    with sqlite3.connect(old) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO tasks (id,user_id,source_type,prompt) VALUES (1,'alice','web','x')")
        conn.execute("INSERT INTO whatsapp_skill_requests (id,requester_user_id,origin_task_id,request_key,kind,recipient_user_id,content_hash,service_hash,provider,binding_fingerprint,state) VALUES ('r','alice',1,'k','purchase','alice','h','s','wallet','fp','held')")
        conn.execute("INSERT INTO message_relays (id,asker_user_id,recipient_user_id,request_id,provider,binding_fingerprint,state) VALUES ('relay','alice','bob','r','room','fp','held')")
    db.init_db(old)
    db.init_db(old)
    db.init_db(fresh)
    with db.get_db(old) as conn, db.get_db(fresh) as new:
        assert _request_shape(conn) == _request_shape(new)
        conn.execute("UPDATE whatsapp_skill_requests SET kind='recovery_fill' WHERE id='r'")
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT request_id FROM message_relays").fetchone()[0] == "r"
        conn.execute("DELETE FROM tasks WHERE id=1")
        assert conn.execute("SELECT origin_task_id FROM whatsapp_skill_requests").fetchone()[0] is None


def test_stdin_replacement_cannot_inherit_page_exemption(recovery_env, sock):
    with proxy_for(recovery_env, sock) as proxy:
        capture = {"type": "vault_secret_capture", "name": NAME, "kind": "codes", "text": "\n".join(CODES),
                   "source": "page", "host": "acme.example"}
        assert "shape" in ask(proxy, sock, capture, True)
        assert "shape" in ask(proxy, sock, {**capture, "source": "stdin"}, True)
        assert fill(proxy, sock) == {"held": True}


def test_concurrent_claims_consume_authorization_once(recovery_env, sock):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from istota.credentials import recovery_fill
    config, task = recovery_env
    with proxy_for(recovery_env, sock) as proxy:
        assert fill(proxy, sock) == {"held": True}
    with db.get_db(config.db_path) as conn:
        confirmations.approve(conn, park(conn, task), config=config, by="web")
        db.update_task_status(conn, task, "running")
    barrier = Barrier(2)

    def claim():
        with db.get_db(config.db_path) as conn:
            barrier.wait(timeout=5)
            return recovery_fill.claim(conn, user_id="alice", task_id=task, name=NAME, host="acme.example")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: claim(), range(2)))
    assert results.count(CODES[0]) == 1 and results.count(None) == 1
    with db.get_db(config.db_path) as conn:
        assert generated.recovery_state(conn, "alice", NAME)["spent"] == [0]


def test_public_proxy_cannot_claim(recovery_env, sock):
    with proxy_for(recovery_env, sock) as proxy:
        proxy._captured_codes_this_attempt[NAME] = "acme.example"
        result = ask(proxy, sock, {"type": "vault_recovery_fill", "name": NAME, "host": "acme.example"}, False)
        assert "error" in result and "code" not in result


def test_scheduler_expires_approved_authorization(recovery_env, sock):
    from istota.scheduler import run_cleanup_checks
    config, task = recovery_env
    with proxy_for(recovery_env, sock) as proxy:
        assert fill(proxy, sock) == {"held": True}
    with db.get_db(config.db_path) as conn:
        confirmations.approve(conn, park(conn, task), config=config, by="web")
        conn.execute("UPDATE recovery_fill_authorizations SET expires_at='2000-01-01 00:00:00'")
    run_cleanup_checks(config)
    with db.get_db(config.db_path) as conn:
        assert conn.execute("SELECT state FROM recovery_fill_authorizations").fetchone()[0] == "expired"
        assert generated.recovery_state(conn, "alice", NAME)["spent"] == []


def test_fill_is_bound_to_approved_host_even_with_other_bindings(recovery_env, sock):
    config, task = recovery_env
    with db.get_db(config.db_path) as conn:
        conn.execute("UPDATE credential_bindings SET hosts=? WHERE name=?", (json.dumps(["acme.example", "other.example"]), NAME))
    with proxy_for(recovery_env, sock) as proxy:
        assert fill(proxy, sock) == {"held": True}
    with db.get_db(config.db_path) as conn:
        confirmations.approve(conn, park(conn, task), config=config, by="web")
        db.update_task_status(conn, task, "running")
    with proxy_for(recovery_env, sock) as proxy:
        assert fill(proxy, sock)["bound_hosts"] == ["acme.example"]
