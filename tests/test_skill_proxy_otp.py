"""OTP seeds are daemon-only on every existing credential read seam."""

import json
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
