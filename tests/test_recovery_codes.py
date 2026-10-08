"""Recovery codes for a generated credential: saved by a task, read by the user only (ISSUE-688)."""

import io
import json
import logging
from types import SimpleNamespace

import h11
import pytest

from istota import db
from istota.config import Config
from istota.credentials import generated, store
from istota.credentials.broker import bindings, intercept
from istota.sandbox import credential_shim
from istota.sandbox.skill_proxy import SkillProxy
from tests import test_skill_proxy_vault_create as _vault_create
from tests import test_vault_credential_fetch as vault_fetch
from tests.test_generated_credentials import _config
from tests.test_skill_proxy_otp import ask
from tests.test_skill_proxy_vault_create import _request

sock = _vault_create.sock
sock_path = vault_fetch.sock_path

CODES = "fixture-rc-1111-aaaa\nfixture-rc-2222-bbbb\nfixture-rc-3333-cccc"
VALUES = {"acme": "fixture-password", "acme_recovery": CODES}


@pytest.fixture
def seeded(tmp_path, monkeypatch):
    """An entry with a password and a recovery member, both marked revealable."""
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    config = Config(db_path=tmp_path / "data.db")
    db.init_db(config.db_path)
    for name, value in VALUES.items():
        binding = bindings.parse_binding("https://acme.example", {}, ["istota:reveal"])
        binding.update(credential="acme", kind="recovery" if name == "acme_recovery" else "value")
        store.upsert_secret(config.db_path, "alice", "vault_entries", name, value, binding=binding)
    return config


def _proxy(config, sock_path, **kwargs):
    return SkillProxy(sock_path, {}, {}, config=config, user_id="alice",
                      vault_credentials=dict(VALUES), **kwargs)


@pytest.mark.parametrize("private", [False, True])
@pytest.mark.parametrize("enforce", [False, True])
def test_vault_credential_refuses_recovery_codes(seeded, sock_path, private, enforce):
    seeded.security.credential_broker.enabled = enforce
    seeded.security.credential_broker.enforce_reveal = enforce
    with _proxy(seeded, sock_path) as server:
        server._created_names.update(VALUES)
        reply = ask(server, sock_path, {"type": "vault_credential", "name": "acme_recovery",
                                        "binding": True, "mode": "read"}, private)
    assert reply["reason"] == "credential_is_recovery"
    assert "fixture-rc" not in json.dumps(reply)


@pytest.mark.parametrize("private", [False, True])
def test_vault_entry_omits_recovery_codes_and_claims_no_otp(seeded, sock_path, private):
    with _proxy(seeded, sock_path) as server:
        server._created_names.update(VALUES)
        reply = ask(server, sock_path, {"type": "vault_entry", "name": "acme"}, private)
    assert reply["fields"] == {"password": "fixture-password"}
    assert "otp" not in reply
    assert "fixture-rc" not in json.dumps(reply)


@pytest.mark.parametrize("name", ["acme", "acme_recovery"])
def test_vault_otp_computes_nothing_from_recovery_codes(seeded, sock_path, name):
    with _proxy(seeded, sock_path) as server:
        server._created_names.update(VALUES)
        reply = ask(server, sock_path, {"type": "vault_otp", "name": name}, True)
    assert "code" not in reply
    assert reply["reason"] in ("credential_has_no_otp", "credential_otp_unusable")
    assert "fixture-rc" not in json.dumps(reply)


def test_intercept_refuses_a_recovery_placeholder(seeded):
    broker = SimpleNamespace(config=seeded, user_id="alice", task_id=1)
    request = h11.Request(method="GET", target="/", headers=[
        ("Host", "acme.example"), ("Authorization", "Bearer {{cred:acme_recovery}}"),
    ])
    with pytest.raises(intercept.Refused, match="^credential_is_recovery$"):
        intercept._headers(broker, request, "acme.example")


def test_shim_get_run_list_and_placeholder(seeded, sock_path, monkeypatch, capsys):
    monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", str(sock_path))
    with _proxy(seeded, sock_path):
        reply = vault_fetch.request(sock_path, {"type": "vault_list"})
        assert {item["name"]: item["kind"] for item in reply["credentials"]} == {
            "acme": "value", "acme_recovery": "recovery"}
        for verb in ("read", "inject"):
            with pytest.raises(credential_shim.ProxyError, match="credential_is_recovery"):
                credential_shim.fetch_credential("acme_recovery", verb)
        with pytest.raises(credential_shim.ProxyError, match="credential_is_recovery"):
            credential_shim._cmd_placeholder(["acme_recovery"])
        assert credential_shim._cmd_list() == 0
    lines = capsys.readouterr().out.splitlines()
    assert next(line for line in lines if line.startswith("acme_recovery\t")).endswith("\tno")
    assert "fixture-rc" not in "\n".join(lines)


def test_the_web_list_does_not_count_recovery_codes_as_otp(seeded):
    from istota.webui import app as web

    seeded.security.allow_unsandboxed_multi_user_vaults = True
    original = web._config
    web._config = seeded
    try:
        payload = web._credential_settings("alice")
    finally:
        web._config = original
    row = next(item for item in payload["credentials"] if item["name"] == "acme")
    assert row["otp"] is False
    assert "fixture-rc" not in json.dumps(payload)


def _create(sock, url="https://acme.example"):
    assert _request(sock, {"type": "vault_create", "slug": "acme", "url": url})["name"] == "generated_acme"


def test_recovery_set_stores_replaces_and_says_which(tmp_path, monkeypatch, sock, caplog):
    config, _ = _config(tmp_path, monkeypatch, with_vault=False)
    caplog.set_level(logging.DEBUG)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=3) as proxy:
        _create(sock)
        first = _request(sock, {"type": "vault_recovery_set", "name": "generated_acme",
                                "text": "  " + CODES.replace("\n", "\n\n  ") + "\n"})
        assert first == {"name": "generated_acme", "count": 3, "replaced": False}
        second = _request(sock, {"type": "vault_recovery_set", "name": "generated_acme",
                                 "text": "fixture-rc-9999-zzzz"})
        assert second == {"name": "generated_acme", "count": 1, "replaced": True}
        assert "generated_acme_recovery" not in proxy.vault_credentials
        assert _request(sock, {"type": "vault_recovery_set", "name": "generated_acme",
                               "text": CODES})["reason"] == "vault_write_limit"
    assert store.get_secret(config.db_path, "alice", "vault_entries",
                            "generated_acme_recovery") == "fixture-rc-9999-zzzz"
    with db.get_db(config.db_path) as conn:
        binding = bindings.get_binding(conn, "alice", "generated_acme_recovery")
        assert binding["kind"] == "recovery" and binding["hosts"] == ["acme.example"]
        assert binding["source"] == generated.SOURCE
        assert bindings.credential_name(conn, "alice", "generated_acme_recovery") == "generated_acme"
        titles = [row["title"] for row in conn.execute(
            "SELECT title FROM notifications WHERE source='task_alert' ORDER BY id")]
    assert "Istota saved recovery codes for generated acme" in titles
    assert "Istota replaced the recovery codes for generated acme" in titles
    assert "fixture-rc" not in caplog.text


@pytest.mark.parametrize("source", ["vault", "local"])
def test_recovery_set_refuses_a_credential_istota_did_not_generate(tmp_path, monkeypatch, sock, source):
    config, _ = _config(tmp_path, monkeypatch, with_vault=False)
    store.upsert_secret(config.db_path, "alice", "vault_entries", "generated_mine", "value",
                        binding=bindings.parse_binding("acme.example", {}, [], source=source))
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1,
                    vault_credentials={"generated_mine": "value"}):
        reply = _request(sock, {"type": "vault_recovery_set", "name": "generated_mine", "text": CODES})
    assert reply["reason"] == "recovery_set_not_generated"
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_mine_recovery") is None


@pytest.mark.parametrize("text", ["", "   \n \n", "x" * 9000], ids=["empty", "blank", "oversized"])
def test_recovery_set_refuses_empty_or_oversized_text(tmp_path, monkeypatch, sock, text):
    config, _ = _config(tmp_path, monkeypatch, with_vault=False)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2):
        _create(sock)
        reply = _request(sock, {"type": "vault_recovery_set", "name": "generated_acme", "text": text})
    assert reply["reason"] in ("recovery_empty", "recovery_too_large")
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme_recovery") is None


def test_the_shim_verb_reads_codes_from_stdin(tmp_path, monkeypatch, sock, capsys):
    config, _ = _config(tmp_path, monkeypatch, with_vault=False)
    monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", str(sock))
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2):
        _create(sock)
        monkeypatch.setattr("sys.stdin", io.StringIO(CODES + "\n"))
        assert credential_shim.main(["recovery-set", "generated_acme"]) == 0
    out = capsys.readouterr().out
    assert "3" in out and "fixture-rc" not in out
    assert store.get_secret(config.db_path, "alice", "vault_entries",
                            "generated_acme_recovery") == CODES


def test_the_private_channel_resolves_a_target_and_saves(tmp_path, monkeypatch, sock):
    config, _ = _config(tmp_path, monkeypatch, with_vault=False)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2) as server:
        _create(sock)
        target = ask(server, sock, {"type": "vault_recovery_target", "name": "generated_acme"}, True)
        assert target == {"name": "generated_acme", "bound_hosts": ["acme.example"]}
        saved = ask(server, sock, {"type": "vault_recovery_set", "name": "generated_acme",
                                   "text": CODES}, True)
        assert saved == {"name": "generated_acme", "count": 3, "replaced": False}
        store.upsert_secret(config.db_path, "alice", "vault_entries", "plain", "value",
                            binding=bindings.parse_binding("acme.example", {}, [], source="local"))
        server.vault_credentials["plain"] = "value"
        refused = ask(server, sock, {"type": "vault_recovery_target", "name": "plain"}, True)
    assert refused["reason"] == "recovery_set_not_generated"


def _stored_with_codes(config):
    with db.get_db(config.db_path) as conn:
        generated.create(conn, "alice", name="generated_acme", username="alice@example.com",
                         password="fixture-password", url="https://acme.example")
        generated.set_recovery(conn, "alice", "generated_acme", CODES)


@pytest.mark.parametrize("request_type", ["vault_recovery_set", "vault_recovery_target"])
def test_a_task_cannot_reach_codes_outside_its_snapshot(tmp_path, monkeypatch, sock, request_type):
    """Review finding: a save replaces, so an unshared credential's codes were destructible."""
    config, _ = _config(tmp_path, monkeypatch, with_vault=False)
    _stored_with_codes(config)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1,
                    vault_credentials={}) as server:
        reply = ask(server, sock, {"type": request_type, "name": "generated_acme", "text": "x"},
                    True)
    assert reply["reason"] == "vault_credential_not_present"
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme_recovery") == CODES


def test_an_ungranted_credential_is_refused_with_the_broker_on(tmp_path, monkeypatch, sock):
    config, _ = _config(tmp_path, monkeypatch, with_vault=False)
    config.security.credential_broker.enabled = True
    _stored_with_codes(config)
    with db.get_db(config.db_path) as conn:
        task = db.create_task(conn, user_id="alice", prompt="x", source_type="talk",
                              conversation_token="room-b")
    snapshot = store.get_service_secrets(config.db_path, "alice", "vault_entries")
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", task_id=str(task),
                    vault_write_limit=1, vault_credentials=snapshot):
        reply = _request(sock, {"type": "vault_recovery_set", "name": "generated_acme", "text": "x"})
    assert reply["reason"] == "credential_not_granted"
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme_recovery") == CODES


def test_control_characters_are_refused_before_they_reach_the_mirror(tmp_path, monkeypatch, sock):
    config, _ = _config(tmp_path, monkeypatch, with_vault=False)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2):
        _create(sock)
        reply = _request(sock, {"type": "vault_recovery_set", "name": "generated_acme",
                                "text": "fixture-rc\x01-1111"})
    assert reply["reason"] == "recovery_unusable"


def test_a_generated_credential_with_no_site_has_no_target(tmp_path, monkeypatch, sock):
    config, _ = _config(tmp_path, monkeypatch, with_vault=False)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=1) as server:
        _create(sock, url="")
        reply = ask(server, sock, {"type": "vault_recovery_target", "name": "generated_acme"}, True)
    assert reply["reason"] == "credential_unbound"


def test_retire_removes_the_codes(tmp_path, monkeypatch, sock):
    config, _ = _config(tmp_path, monkeypatch, with_vault=False)
    with SkillProxy(sock, {}, {}, config=config, user_id="alice", vault_write_limit=2):
        _create(sock)
        _request(sock, {"type": "vault_recovery_set", "name": "generated_acme", "text": CODES})
    assert generated.retire(config, "alice", "generated_acme") is True
    assert store.get_secret(config.db_path, "alice", "vault_entries", "generated_acme_recovery") is None
