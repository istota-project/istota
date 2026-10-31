"""A connected mail account's password on the skill proxy's private channel.

The property the multi-account design rests on: a mail account's password
reaches only the `email` skill's own invocation, for the purpose it asked for,
for the task's own user, on a turn that user wrote. It is never on the public
socket and never in any environment the model can query.
"""

from __future__ import annotations

import json
import logging
import socket
import subprocess
import threading
from pathlib import Path

import pytest

from istota import db, executor
from istota.config import Config, CredentialBrokerConfig, DevboxConfig, MailAccountsConfig, SecurityConfig, UserConfig
from istota.mail import accounts
from istota.sandbox import credential_shim, skill_proxy as skill_proxy_module, task_env
from istota.sandbox.skill_proxy import SkillProxy

IMAP_SECRET = "imap-pass-7f3c2a"
SMTP_SECRET = "smtp-pass-91be04"
BOB_SECRET = "bob-pass-c0ffee"


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
    path = tmp_path / "istota.db"
    db.init_db(path)
    with db.get_db(path) as conn:
        task_id = db.create_task(conn, user_id="alice", prompt="mail?", source_type="talk")
        reader, _ = accounts.ensure_account(
            conn, "alice", slug="work", imap_host="imap.work.example",
            imap_username="alice@work.example",
        )
        sender, _ = accounts.ensure_account(
            conn, "alice", slug="home", imap_host="imap.home.example",
            imap_username="alice@home.example", permission="read_send",
            smtp_host="smtp.home.example", smtp_port=465, smtp_security="tls",
            smtp_same_login=False, smtp_username="alice-smtp",
            from_address="alice@home.example",
        )
        other, _ = accounts.ensure_account(
            conn, "bob", slug="work", imap_host="imap.bob.example",
            imap_username="bob@bob.example",
        )
        conn.commit()
        accounts.set_password(conn, "alice", reader.id, "imap", IMAP_SECRET)
        accounts.set_password(conn, "alice", sender.id, "imap", IMAP_SECRET)
        accounts.set_password(conn, "alice", sender.id, "smtp", SMTP_SECRET)
        accounts.set_password(conn, "bob", other.id, "imap", BOB_SECRET)
        conn.commit()
    config = Config(db_path=path)
    return {"path": path, "config": config, "task_id": task_id,
            "reader": reader.id, "sender": sender.id, "bob": other.id}


def proxy_for(env, tmp_path):
    return SkillProxy(tmp_path / "proxy.sock", {}, {}, config=env["config"],
                      user_id="alice", task_id=env["task_id"])


def exchange(proxy, payload, *, skill="email", public=False):
    client, server = socket.socketpair()
    if public:
        worker = threading.Thread(target=proxy._handle_connection, args=(server,))
    else:
        worker = threading.Thread(target=proxy._serve_credential_channel, args=(server,),
                                  kwargs={"skill": skill})
    worker.start()
    try:
        client.settimeout(3)
        client.sendall(json.dumps(payload).encode() + b"\n")
        return json.loads(client.makefile("rb").readline())
    finally:
        client.close()
        worker.join(3)
        server.close()
        assert not worker.is_alive()


def ask(account_id, purpose="read"):
    return {"type": "mail_account", "account_id": account_id, "purpose": purpose}


def _turn(env, **author):
    with db.get_db(env["path"]) as conn:
        token = db.create_web_chat_room(conn, "alice", "general").token
        db.add_message(conn, token, role="user", body="read my work mail",
                       origin_surface="web", task_id=env["task_id"], **author)
        conn.commit()


class TestServed:
    def test_a_read_returns_the_imap_block_and_no_smtp(self, env, tmp_path):
        reply = exchange(proxy_for(env, tmp_path), ask(env["reader"]))
        assert reply == {
            "imap": {"host": "imap.work.example", "port": 993, "security": "tls",
                     "username": "alice@work.example", "password": IMAP_SECRET},
            "smtp": None,
        }

    def test_a_read_on_a_sending_account_still_carries_no_smtp(self, env, tmp_path):
        reply = exchange(proxy_for(env, tmp_path), ask(env["sender"]))
        assert reply["smtp"] is None
        assert SMTP_SECRET not in json.dumps(reply)

    def test_a_send_returns_both_blocks(self, env, tmp_path):
        reply = exchange(proxy_for(env, tmp_path), ask(env["sender"], "send"))
        assert reply["smtp"] == {"host": "smtp.home.example", "port": 465, "security": "tls",
                                 "username": "alice-smtp", "password": SMTP_SECRET}
        assert reply["imap"]["password"] == IMAP_SECRET

    def test_the_users_own_turn_is_served(self, env, tmp_path):
        _turn(env, author_user_id="alice")
        assert "imap" in exchange(proxy_for(env, tmp_path), ask(env["reader"]))

    def test_nothing_secret_is_logged(self, env, tmp_path, caplog):
        with caplog.at_level(logging.DEBUG):
            exchange(proxy_for(env, tmp_path), ask(env["sender"], "send"))
            exchange(proxy_for(env, tmp_path), ask(env["reader"], "send"))
        for value in (IMAP_SECRET, SMTP_SECRET, "imap.home.example", "smtp.home.example",
                      "alice-smtp", "alice@home.example", "imap.work.example"):
            assert value not in caplog.text
        assert f"account_id={env['sender']}" in caplog.text
        assert "reason=permission_denied" in caplog.text


class TestRefused:
    def test_another_skill_is_refused(self, env, tmp_path):
        reply = exchange(proxy_for(env, tmp_path), ask(env["reader"]), skill="browse")
        assert reply == {"ok": False, "reason": "skill_not_permitted"}

    def test_a_channel_with_no_skill_named_is_refused(self, env, tmp_path):
        reply = exchange(proxy_for(env, tmp_path), ask(env["reader"]), skill=None)
        assert reply == {"ok": False, "reason": "skill_not_permitted"}

    def test_the_public_socket_refuses_before_opening_the_database(self, env, tmp_path, monkeypatch):
        proxy = proxy_for(env, tmp_path)
        monkeypatch.setattr(proxy, "_peer_in_task", lambda peer: True)

        def no_db(*args, **kwargs):
            raise AssertionError("public mail_account request opened the database")
        monkeypatch.setattr(db, "get_db", no_db)
        reply = exchange(proxy, {**ask(env["reader"]), "skill": "email"}, public=True)
        assert reply == {"ok": False, "reason": "account_channel_unavailable"}

    def test_another_users_id_is_not_found(self, env, tmp_path):
        reply = exchange(proxy_for(env, tmp_path), ask(env["bob"]))
        assert reply == {"ok": False, "reason": "account_not_found"}
        assert BOB_SECRET not in json.dumps(reply)

    def test_a_disabled_account_reads_as_not_found(self, env, tmp_path):
        with db.get_db(env["path"]) as conn:
            accounts.update_account(conn, "alice", env["reader"], enabled=False)
            conn.commit()
        assert exchange(proxy_for(env, tmp_path), ask(env["reader"]))["reason"] == "account_not_found"

    @pytest.mark.parametrize("account_id", [None, True, "1", -1, 0, 1.0, 2**64])
    def test_a_malformed_id_is_not_found(self, env, tmp_path, account_id):
        reply = exchange(proxy_for(env, tmp_path), ask(account_id))
        assert reply == {"ok": False, "reason": "account_not_found"}

    @pytest.mark.parametrize("purpose", [None, "write", "SEND", ""])
    def test_an_unknown_purpose_is_refused(self, env, tmp_path, purpose):
        reply = exchange(proxy_for(env, tmp_path), ask(env["sender"], purpose))
        assert reply == {"ok": False, "reason": "invalid_credential_request"}

    def test_send_on_a_read_account_is_denied(self, env, tmp_path):
        reply = exchange(proxy_for(env, tmp_path), ask(env["reader"], "send"))
        assert reply == {"ok": False, "reason": "permission_denied"}

    def test_send_with_sending_off_is_denied(self, env, tmp_path):
        env["config"].mail_accounts = MailAccountsConfig(allow_send=False)
        reply = exchange(proxy_for(env, tmp_path), ask(env["sender"], "send"))
        assert reply == {"ok": False, "reason": "permission_denied"}
        assert SMTP_SECRET not in json.dumps(reply)

    def test_the_feature_off_refuses(self, env, tmp_path):
        env["config"].mail_accounts = MailAccountsConfig(enabled=False)
        reply = exchange(proxy_for(env, tmp_path), ask(env["reader"]))
        assert reply == {"ok": False, "reason": "mail_accounts_disabled"}

    def test_a_missing_password_is_named(self, env, tmp_path):
        with db.get_db(env["path"]) as conn:
            accounts.clear_password(conn, "alice", env["reader"], "imap")
            conn.commit()
        reply = exchange(proxy_for(env, tmp_path), ask(env["reader"]))
        assert reply == {"ok": False, "reason": "password_missing"}

    def test_a_turn_another_user_wrote_is_refused(self, env, tmp_path):
        _turn(env, author_user_id="bob")
        reply = exchange(proxy_for(env, tmp_path), ask(env["reader"]))
        assert reply == {"ok": False, "reason": "not_owner_turn"}

    def test_a_correspondents_mail_is_refused(self, env, tmp_path):
        # An email thread room runs a correspondent's admitted mail as the
        # host at full reach; that mail must not read the host's other boxes.
        _turn(env, author_label="mallory@elsewhere.example")
        reply = exchange(proxy_for(env, tmp_path), ask(env["reader"]))
        assert reply == {"ok": False, "reason": "not_owner_turn"}

    def test_a_guests_turn_is_refused(self, env, tmp_path):
        with db.get_db(env["path"]) as conn:
            conn.execute("UPDATE tasks SET guest_participant_id = 7 WHERE id = ?", (env["task_id"],))
            conn.commit()
        reply = exchange(proxy_for(env, tmp_path), ask(env["reader"]))
        assert reply == {"ok": False, "reason": "not_owner_turn"}

    def test_a_task_of_another_user_is_refused(self, env, tmp_path):
        proxy = proxy_for(env, tmp_path)
        proxy.user_id = "bob"
        reply = exchange(proxy, ask(env["bob"]))
        assert reply == {"ok": False, "reason": "not_owner_turn"}

    def test_a_proxy_with_no_task_is_refused(self, env, tmp_path):
        proxy = proxy_for(env, tmp_path)
        proxy.task_id = None
        reply = exchange(proxy, ask(env["reader"]))
        assert reply == {"ok": False, "reason": "account_channel_unavailable"}

    def test_an_unexpected_error_is_redacted(self, env, tmp_path, monkeypatch, caplog):
        def broken(*args, **kwargs):
            raise RuntimeError(IMAP_SECRET)
        monkeypatch.setattr(accounts, "resolve_for_daemon", broken)
        with caplog.at_level(logging.DEBUG):
            reply = exchange(proxy_for(env, tmp_path), ask(env["reader"]))
        assert reply == {"ok": False, "reason": "mail_account_error"}
        assert IMAP_SECRET not in caplog.text
        assert "RuntimeError" in caplog.text


class TestASubtaskAnswersForItsParentsTurn:
    """A subtask has no stored turn of its own; the turn it serves is its root's.

    A correspondent's mail on an email thread room runs as the host, and a
    subtask it spawns gets the host's user id, the room's token and no
    `messages` row, so the task's own row says nothing about who asked.
    """

    def _child_of(self, env, parent_id, **columns):
        with db.get_db(env["path"]) as conn:
            child = db.create_task(conn, user_id="alice", prompt="go on",
                                   source_type="subtask", parent_task_id=parent_id)
            for name, value in columns.items():
                conn.execute(f"UPDATE tasks SET {name} = ? WHERE id = ?", (value, child))
            conn.commit()
        return child

    def _ask_as(self, env, tmp_path, task_id):
        proxy = proxy_for(env, tmp_path)
        proxy.task_id = task_id
        return exchange(proxy, ask(env["reader"]))

    def test_a_correspondents_subtask_is_refused(self, env, tmp_path):
        _turn(env, author_user_id="stranger")
        child = self._child_of(env, env["task_id"])
        assert self._ask_as(env, tmp_path, child) == {"ok": False, "reason": "not_owner_turn"}

    def test_an_outside_senders_grandchild_is_refused(self, env, tmp_path):
        _turn(env, author_label="mallory@elsewhere.example")
        grandchild = self._child_of(env, self._child_of(env, env["task_id"]))
        assert self._ask_as(env, tmp_path, grandchild) == {
            "ok": False, "reason": "not_owner_turn",
        }

    def test_a_guest_ancestor_is_refused(self, env, tmp_path):
        with db.get_db(env["path"]) as conn:
            conn.execute("UPDATE tasks SET guest_participant_id = 7 WHERE id = ?",
                         (env["task_id"],))
            conn.commit()
        child = self._child_of(env, env["task_id"])
        assert self._ask_as(env, tmp_path, child)["reason"] == "not_owner_turn"

    def test_the_owners_own_turn_serves_its_subtask(self, env, tmp_path):
        _turn(env, author_user_id="alice")
        child = self._child_of(env, env["task_id"])
        assert "imap" in self._ask_as(env, tmp_path, child)

    def _email_task(self, env, sender, *, stored_row):
        with db.get_db(env["path"]) as conn:
            task_id = db.create_task(conn, user_id="alice", prompt="mail?", source_type="email")
            db.mark_email_processed(conn, f"uid-{task_id}", sender, user_id="alice",
                                    task_id=task_id, routing_method="plus_address")
            if stored_row:
                token = db.create_web_chat_room(conn, "alice", "thread").token
                db.add_message(conn, token, role="user", body="b", origin_surface="email",
                               task_id=task_id)
            conn.commit()
        return task_id

    def test_a_strangers_mail_with_no_stored_row_is_refused(self, env, tmp_path):
        task_id = self._email_task(env, "mallory@elsewhere.example", stored_row=False)
        assert self._ask_as(env, tmp_path, task_id)["reason"] == "not_owner_turn"

    def test_a_strangers_mail_with_an_unattributed_row_is_refused(self, env, tmp_path):
        task_id = self._email_task(env, "mallory@elsewhere.example", stored_row=True)
        assert self._ask_as(env, tmp_path, task_id)["reason"] == "not_owner_turn"

    def test_the_owners_own_mail_with_an_unattributed_row_is_served(self, env, tmp_path):
        env["config"].users["alice"] = UserConfig(email_addresses=["alice@home.example"])
        task_id = self._email_task(env, "alice@home.example", stored_row=True)
        assert "imap" in self._ask_as(env, tmp_path, task_id)

    def test_a_missing_parent_is_refused(self, env, tmp_path):
        child = self._child_of(env, 999_999)
        assert self._ask_as(env, tmp_path, child)["reason"] == "not_owner_turn"

    def test_a_cycle_is_refused_and_ends(self, env, tmp_path):
        child = self._child_of(env, env["task_id"])
        with db.get_db(env["path"]) as conn:
            conn.execute("UPDATE tasks SET parent_task_id = ? WHERE id = ?",
                         (child, env["task_id"]))
            conn.commit()
        assert self._ask_as(env, tmp_path, child)["reason"] == "not_owner_turn"


class TestTheSkillNameComesFromTheDispatch:
    """The skill name the channel checks is the one the proxy spawned, never a
    claim in the request: driven through `_handle_connection` with the
    subprocess stood in by a reader of the real channel fd."""

    def _run(self, env, tmp_path, monkeypatch, skill):
        proxy = proxy_for(env, tmp_path)
        monkeypatch.setattr(proxy, "_peer_in_task", lambda peer: True)

        def fake_run(cmd, *, env, **kwargs):
            try:
                answer = credential_shim.fetch_mail_account(
                    proxy_env_account, "read", credential_fd=env["ISTOTA_CRED_FD"])
                out = answer["imap"]["host"]
            except credential_shim.ProxyError as exc:
                out = f"refused:{exc}"
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        proxy_env_account = env["reader"]
        monkeypatch.setattr(skill_proxy_module, "run_capture", fake_run)
        monkeypatch.setattr(skill_proxy_module.peer_process, "reporting_pid",
                            _no_report)
        return exchange(proxy, {"skill": skill, "args": [], "trusted_skill": "email"},
                        public=True)

    def test_email_is_served(self, env, tmp_path, monkeypatch):
        assert self._run(env, tmp_path, monkeypatch, "email")["stdout"] == "imap.work.example"

    def test_another_skill_is_refused_whatever_it_claims(self, env, tmp_path, monkeypatch):
        assert self._run(env, tmp_path, monkeypatch, "kv")["stdout"] == "refused:skill_not_permitted"


class _no_report:
    def __init__(self, register, preexec=None):
        pass

    def __enter__(self):
        return None

    def __exit__(self, *exc):
        return False


class TestTheShim:
    def test_fetch_over_the_real_channel(self, env, tmp_path):
        proxy = proxy_for(env, tmp_path)
        with proxy._credential_channel(3, "email") as fd:
            answer = credential_shim.fetch_mail_account(env["sender"], "send", credential_fd=str(fd))
        assert answer["imap"]["username"] == "alice@home.example"
        assert answer["smtp"]["password"] == SMTP_SECRET

    def test_a_refusal_is_raised_with_its_reason(self, env, tmp_path):
        proxy = proxy_for(env, tmp_path)
        with proxy._credential_channel(3, "email") as fd:
            with pytest.raises(credential_shim.ProxyError, match="^permission_denied$"):
                credential_shim.fetch_mail_account(env["reader"], "send", credential_fd=str(fd))

    def test_no_channel_has_no_public_fallback(self, monkeypatch):
        monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", "/never-use-public.sock")
        for fd in (None, "not a descriptor", "-1"):
            with pytest.raises(credential_shim.ProxyError, match="^account_channel_unavailable$"):
                credential_shim.fetch_mail_account(1, "read", credential_fd=fd)

    @pytest.mark.parametrize("reply", [
        {"ok": False, "reason": IMAP_SECRET},
        {"imap": {"host": "h", "port": "993", "security": "tls", "username": "u", "password": IMAP_SECRET},
         "smtp": None},
        {"imap": {"host": "h", "port": 993, "security": "tls", "username": "u"}, "smtp": None},
        {"imap": {"host": "h", "port": 993, "security": "tls", "username": "u", "password": "p"},
         "smtp": "nope"},
        [IMAP_SECRET],
    ])
    def test_a_malformed_reply_never_echoes_data(self, reply, monkeypatch):
        monkeypatch.setattr(credential_shim, "_request", lambda *a, **kw: reply)
        with pytest.raises(credential_shim.ProxyError, match="^mail_account_error$"):
            credential_shim.fetch_mail_account(1, "read", credential_fd="3")


class TestNoEnvironmentPath:
    """No mail password reaches any environment, by any name: not the model's,
    not the proxy's base or credential env, not the vault namespace, and not
    the `istota-credential env` lookup allowlist."""

    def test_a_runtime_for_a_user_with_two_accounts_carries_no_password(
        self, tmp_path, make_task, monkeypatch,
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: True)
        monkeypatch.setenv("ISTOTA_SECRET_KEY", "deadbeef" * 8)
        (tmp_path / "db").mkdir()
        config = Config(
            db_path=tmp_path / "db" / "test.db",
            temp_dir=tmp_path / "temp",
            workspace_path=tmp_path / "mount",
            security=SecurityConfig(
                sandbox_enabled=True, skill_proxy_enabled=True,
                credential_broker=CredentialBrokerConfig(enabled=False, enforce_reveal=False),
            ),
            devbox=DevboxConfig(enabled=False),
        )
        db.init_db(config.db_path)
        with db.get_db(config.db_path) as conn:
            task_id = db.create_task(conn, user_id="testuser", prompt="x", source_type="talk")
            for slug, value in (("work", IMAP_SECRET), ("home", SMTP_SECRET)):
                account, _ = accounts.ensure_account(
                    conn, "testuser", slug=slug, imap_host=f"imap.{slug}.example",
                    imap_username=f"testuser@{slug}.example",
                )
                conn.commit()
                accounts.set_password(conn, "testuser", account.id, "imap", value)
                conn.commit()

        from istota.skills._loader import load_skill_index
        index = load_skill_index(Path("/nonexistent-operator-skills"))
        assert "email" in index
        user_temp_dir = tmp_path / "temp" / "testuser"
        user_temp_dir.mkdir(parents=True)
        control_dir = tmp_path / "temp" / ".control" / "testuser" / f"task_{task_id}"
        control_dir.mkdir(parents=True)
        runtime = task_env.build_task_runtime(
            config, task=make_task(id=task_id, user_id="testuser", source_type="talk"),
            user_temp_dir=user_temp_dir, control_dir=control_dir, task_attempt=1,
            selected_skills=["email"], skill_index=index, is_admin=True,
            user_resources=[], user_config=None, discovered_calendars=[],
        )
        proxy = runtime.proxy_ctx
        assert proxy is not None
        haystacks = {
            "model env": runtime.env,
            "proxy base env": proxy.base_env,
            "proxy credential env": proxy.credential_env,
            "vault namespace": proxy.vault_credentials,
            "sandbox env": runtime.sandbox_env,
        }
        for label, mapping in haystacks.items():
            blob = "\n".join(f"{k}={v}" for k, v in mapping.items())
            for value in (IMAP_SECRET, SMTP_SECRET):
                assert value not in blob, f"a mail password leaked into the {label}"
        allowed = proxy.allowed_credentials or set()
        for name in allowed:
            assert IMAP_SECRET not in proxy.credential_env.get(name, "")
            assert SMTP_SECRET not in proxy.credential_env.get(name, "")
            assert "mail_account" not in name.lower()
