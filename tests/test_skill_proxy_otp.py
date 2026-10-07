"""OTP seeds are daemon-only on every existing credential read seam."""

import base64
import json
import logging
import socket
from pathlib import Path
from types import SimpleNamespace

import h11
import pytest

from istota import db
from istota.config import Config
from istota.credentials import store
from istota.credentials.broker import bindings, intercept
from istota.sandbox import credential_shim
from istota.sandbox.skill_proxy import SkillProxy
from tests import test_vault_credential_fetch as vault_fetch

sock_path = vault_fetch.sock_path
SEED = "fixture-otp-seed-value"
VALUES = {"acme": "fixture-password", "acme_factor": SEED, "acme_totp": "ordinary-field"}


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    config = Config(db_path=tmp_path / "data.db")
    db.init_db(config.db_path)
    for name, value in VALUES.items():
        binding = bindings.parse_binding("https://acme.example", {}, ["istota:reveal"])
        binding.update(credential="acme", kind="totp" if name == "acme_factor" else "value")
        store.upsert_secret(config.db_path, "alice", "vault_entries", name, value, binding=binding)
    return config


def proxy(config, sock_path, **kwargs):
    return SkillProxy(sock_path, {}, {}, config=config, user_id="alice",
                      vault_credentials=VALUES, **kwargs)


def ask(server, sock_path, payload, private):
    if private:
        with server._credential_channel(10) as fd:
            with socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM) as conn:
                conn.sendall(json.dumps(payload).encode() + b"\n")
                with conn.makefile("rb") as reader:
                    return json.loads(reader.readline())
    return vault_fetch.request(sock_path, payload)


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize("bound", [False, True])
def test_seed_refused_before_reveal_and_grant(config, sock_path, private, enforce, bound):
    config.security.credential_broker.enabled = enforce
    config.security.credential_broker.enforce_reveal = enforce
    with proxy(config, sock_path, vault_fetch_limit=1) as server:
        reply = ask(server, sock_path, {"type": "vault_credential", "name": "acme_factor",
                                      "binding": bound, "mode": "read"}, private)
        assert reply["reason"] == "credential_is_otp_seed"
        assert SEED not in json.dumps(reply)
        assert server._vault_fetches == 1
        limited = ask(server, sock_path, {"type": "vault_credential", "name": "acme_factor"}, private)
        absent = ask(server, sock_path, {"type": "vault_credential", "name": "absent"}, private)
        assert limited == absent
        assert limited["reason"] == "vault_credential_limit"


@pytest.mark.parametrize("private", [False, True])
def test_entry_omits_seed_but_keeps_ordinary_totp_named_field(config, sock_path, private):
    config.security.credential_broker.enabled = True
    config.security.credential_broker.enforce_reveal = True
    with proxy(config, sock_path) as server:
        server._created_names.update(VALUES)
        with db.get_db(config.db_path) as conn:
            conn.execute("UPDATE credential_bindings SET revealable=0 WHERE kind='totp'")
        reply = ask(server, sock_path, {"type": "vault_entry", "name": "acme"}, private)
        assert reply["fields"] == {"password": VALUES["acme"], "totp": "ordinary-field"}
        assert reply["otp"] is True
        assert reply["bound_hosts"] == ["acme.example"]
        assert SEED not in json.dumps(reply)
        assert server._vault_fetches == 1


def test_intercept_refuses_seed_placeholder(config):
    broker = SimpleNamespace(config=config, user_id="alice", task_id=1)
    request = h11.Request(method="GET", target="/", headers=[
        ("Host", "acme.example"), ("Authorization", "Bearer {{cred:acme_factor}}"),
    ])
    with pytest.raises(intercept.Refused, match="^credential_is_otp_seed$"):
        intercept._headers(broker, request, "acme.example")


def test_list_kind_and_placeholder_refusal(config, sock_path, monkeypatch, capsys):
    monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", str(sock_path))
    with proxy(config, sock_path):
        reply = vault_fetch.request(sock_path, {"type": "vault_list"})
        assert {item["name"]: item["kind"] for item in reply["credentials"]} == {
            "acme": "value", "acme_factor": "totp", "acme_totp": "value",
        }
        assert SEED not in json.dumps(reply)
        with pytest.raises(credential_shim.ProxyError, match="credential_is_otp_seed"):
            credential_shim._cmd_placeholder(["acme_factor"])
        assert capsys.readouterr().out == ""
        assert credential_shim._cmd_placeholder(["acme_totp"]) == 0
        assert capsys.readouterr().out == "{{cred:acme_totp}}"
        assert credential_shim._cmd_list() == 0
        lines = capsys.readouterr().out.splitlines()
        assert lines[0].endswith("\tOTP")
        assert next(line for line in lines if line.startswith("acme_factor\t")).endswith("\tyes")
        assert next(line for line in lines if line.startswith("acme_totp\t")).endswith("\tno")


def test_shipped_manifests_cannot_inject_vault_entries():
    from istota.skills._loader import load_skill_index
    index = load_skill_index(Path(__file__).resolve().parents[1] / "config" / "skills", bundled_dir=None)
    assert index
    assert not [(name, spec.var) for name, meta in index.items() for spec in meta.env_specs
                if spec.service.casefold() == "vault_entries"]


@pytest.mark.parametrize("private", [False, True])
def test_live_kind_change_cannot_release_snapshot_seed(config, sock_path, private):
    with proxy(config, sock_path) as server:
        binding = bindings.parse_binding("https://acme.example", {}, ["istota:reveal"])
        binding["credential"] = "acme"
        store.upsert_secret(config.db_path, "alice", "vault_entries", "acme_factor",
                            "replacement-plain-value", binding=binding)
        reply = ask(server, sock_path, {"type": "vault_credential", "name": "acme_factor"}, private)
        assert reply == {"value": "replacement-plain-value"}
        store.delete_secret(config.db_path, "alice", "vault_entries", "acme_factor")
        reply = ask(server, sock_path, {"type": "vault_credential", "name": "acme_factor"}, private)
        assert reply["reason"] == "vault_credential_not_present"
        assert SEED not in json.dumps(reply)


@pytest.fixture
def otp(config, monkeypatch):
    from istota.lib import totp
    from istota.sandbox import skill_proxy
    params = totp.parse_user_input(base64.b32encode(b"01234567890123456789").decode())
    uri = totp.to_uri(params)
    binding = bindings.parse_binding("https://acme.example", {}, [])
    binding.update(credential="acme", kind="totp")
    store.upsert_secret(config.db_path, "alice", "vault_entries", "acme_factor", uri,
                        binding=binding)
    monkeypatch.setattr(skill_proxy, "time", lambda: 60.0, raising=False)
    return params, uri


@pytest.mark.parametrize("name", ["acme", "acme_factor"])
def test_otp_live_seed_and_hosts(config, sock_path, otp, name, caplog):
    from istota.lib import totp
    with proxy(config, sock_path) as server, caplog.at_level(logging.INFO):
        reply = ask(server, sock_path, {"type": "vault_otp", "name": name}, True)
        assert reply == {"code": totp.code_at(otp[0], 60), "expires_at": 90,
                         "bound_hosts": ["acme.example"]}
        assert server._vault_fetches == 1
    assert "credential_otp" in caplog.text
    assert reply["code"] not in caplog.text
    assert otp[1] not in caplog.text
    assert SEED not in json.dumps(reply)


@pytest.mark.parametrize("now,wait", [(79.9, 0), (80.0, 0), (80.25, 9.75), (89.9, 0.1)])
def test_otp_waits_for_fresh_window_without_holding_database(config, sock_path, otp, monkeypatch, now, wait):
    from istota.lib import totp
    from istota.sandbox import skill_proxy
    clock = [now]
    sleeps = []

    def sleep(seconds):
        sleeps.append(seconds)
        # The freshness delay must not block other writers.
        with db.get_db(config.db_path) as conn:
            conn.execute("PRAGMA busy_timeout=1")
            conn.execute("BEGIN IMMEDIATE")
        clock[0] += seconds

    monkeypatch.setattr(skill_proxy, "time", lambda: clock[0])
    monkeypatch.setattr(skill_proxy, "sleep", sleep)
    with proxy(config, sock_path) as server:
        reply = ask(server, sock_path, {"type": "vault_otp", "name": "acme"}, True)
    assert sleeps == pytest.approx([wait] if wait else [])
    assert reply["code"] == totp.code_at(otp[0], clock[0])
    assert reply["expires_at"] == (120 if wait else 90)


def test_otp_budget_shared_with_value_reads(config, sock_path, otp):
    with proxy(config, sock_path, vault_fetch_limit=1) as server:
        missing = ask(server, sock_path, {"type": "vault_otp", "name": "absent"}, True)
        assert missing["reason"] == "credential_has_no_otp"
        replies = [ask(server, sock_path, {"type": kind, "name": name}, True)
                   for kind, name in [("vault_otp", "acme"), ("vault_otp", "absent"),
                                      ("vault_credential", "acme")]]
        assert replies[0] == replies[1] == replies[2]
        assert replies[0]["reason"] == "vault_credential_limit"
        assert server._vault_fetches == 4


@pytest.mark.parametrize("case,reason", [
    ("no_seed", "credential_has_no_otp"), ("withheld", "credential_has_no_otp"),
    ("multiple", "credential_has_no_otp"), ("unbound", "credential_unbound"),
    ("malformed", "credential_otp_unusable"),
])
def test_otp_refusals(config, sock_path, otp, case, reason, caplog):
    with proxy(config, sock_path) as server:
        if case == "no_seed":
            store.delete_secret(config.db_path, "alice", "vault_entries", "acme_factor")
        elif case == "withheld":
            server.vault_credentials = {}
        elif case == "multiple":
            with db.get_db(config.db_path) as conn:
                conn.execute("UPDATE credential_bindings SET kind='totp' WHERE name='acme_totp'")
        elif case == "unbound":
            with db.get_db(config.db_path) as conn:
                conn.execute("UPDATE credential_bindings SET hosts='[]' WHERE kind='totp'")
        elif case == "malformed":
            store.upsert_secret(config.db_path, "alice", "vault_entries", "acme_factor", SEED)
        reply = ask(server, sock_path, {"type": "vault_otp", "name": "acme"}, True)
        assert reply["reason"] == reason
        assert server._vault_fetches == 1
        assert SEED not in json.dumps(reply) + caplog.text
        assert otp[1] not in json.dumps(reply) + caplog.text


def test_otp_owner_grant_and_live_revocation(config, sock_path, otp):
    from istota.credentials.broker import grants
    config.security.credential_broker.enabled = True
    with db.get_db(config.db_path) as conn:
        grants.put_grant(conn, "alice", "acme")
        task_id = db.create_task(conn, user_id="alice", prompt="test", source_type="talk",
                                 conversation_token="room-a")
    with db.get_db(config.db_path) as conn:
        grants.ensure_credential_grants(conn, task_id, "alice")
    with proxy(config, sock_path, task_id=task_id) as server:
        assert "code" in ask(server, sock_path, {"type": "vault_otp", "name": "acme_factor"}, True)
        with db.get_db(config.db_path) as conn:
            grants.put_grant(conn, "alice", "acme", allow_scheduled=True)
        reply = ask(server, sock_path, {"type": "vault_otp", "name": "acme"}, True)
        assert reply["reason"] == "credential_changed"


def test_otp_missing_grant_and_created_name_exemption(config, sock_path, otp):
    config.security.credential_broker.enabled = True
    with proxy(config, sock_path) as server:
        reply = ask(server, sock_path, {"type": "vault_otp", "name": "acme"}, True)
        assert reply["reason"] == "credential_not_granted"
        server._created_names.add("acme_factor")
        assert "code" in ask(server, sock_path, {"type": "vault_otp", "name": "acme"}, True)


def test_otp_public_socket_cannot_claim_private_authority(config, sock_path, otp):
    with proxy(config, sock_path) as server:
        reply = ask(server, sock_path, {"type": "vault_otp", "name": "acme", "mode": "skill",
                                      "trusted_skill": True}, False)
        assert "error" in reply
        assert "code" not in reply
        assert server._vault_fetches == 0


def test_fetch_otp_reuses_private_fd(config, sock_path, otp, monkeypatch):
    from istota.lib import totp
    monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", str(sock_path))
    with proxy(config, sock_path) as server:
        with server._credential_channel(10) as fd:
            for name in ("acme", "acme_factor"):
                assert credential_shim.fetch_otp(name, "skill", credential_fd=str(fd)) == (
                    totp.code_at(otp[0], 60), 90, ("acme.example",))
        for fd in (None, "bad-fd", "-1"):
            with pytest.raises(credential_shim.ProxyError):
                credential_shim.fetch_otp("acme", "skill", credential_fd=fd)
        assert server._vault_fetches == 2


@pytest.mark.parametrize("field,value", [("code", None), ("expires_at", True),
                                         ("expires_at", "90"), ("bound_hosts", "acme.example")])
def test_fetch_otp_validates_reply(monkeypatch, field, value):
    reply = {"code": "123456", "expires_at": 90, "bound_hosts": ["acme.example"]}
    reply[field] = value
    monkeypatch.setattr(credential_shim, "_request", lambda *a, **kw: reply)
    with pytest.raises(credential_shim.ProxyError, match="unparseably"):
        credential_shim.fetch_otp("acme", "skill", credential_fd="3")
