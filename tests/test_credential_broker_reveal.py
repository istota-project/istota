"""Reveal policy at the real public socket and private skill boundary."""

import hashlib
import json
import logging
import os
import sys

import pytest

from istota import db, secrets_store
from istota.config import Config
from istota.credential_broker.bindings import parse_binding
from istota.skill_proxy import SkillProxy
from tests.test_skill_credential_fd import skill_program as skill_program, start_proxy
from tests.test_vault_credential_fetch import (
    VAULT, proxy, request, run_shim, sock_path as sock_path,
)


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    config = Config(db_path=tmp_path / "data.db")
    db.init_db(config.db_path)
    config.security.credential_broker.enforce_reveal = True
    for name, value in VAULT.items():
        secrets_store.upsert_secret(
            config.db_path, "alice", "vault_entries", name, value,
            binding=parse_binding("https://portal.example", {}, []),
        )
    return config


def set_reveal(config, revealable):
    secrets_store.upsert_secret(
        config.db_path, "alice", "vault_entries", "github_pat", VAULT["github_pat"],
        binding=parse_binding("https://portal.example", {}, ["istota:reveal"] if revealable else []),
    )


@pytest.mark.parametrize("mode", ["read", "inject", "skill", "unknown", {"skill": True}])
@pytest.mark.parametrize("binding", [False, True])
def test_public_claims_cannot_reveal(config, sock_path, mode, binding):
    with proxy(sock_path, config=config, user_id="alice"):
        reply = request(sock_path, {"type": "vault_credential", "name": "github_pat",
                                   "mode": mode, "trusted_skill": True, "binding": binding})
    assert reply["reason"] == "credential_brokered"
    assert "value" not in reply
    assert VAULT["github_pat"] not in json.dumps(reply)


def test_live_reveal_revocation_and_missing_metadata(config, sock_path):
    set_reveal(config, True)
    with proxy(sock_path, config=config, user_id="alice"):
        payload = {"type": "vault_credential", "name": "github_pat"}
        assert request(sock_path, payload)["value"] == VAULT["github_pat"]
        set_reveal(config, False)
        assert request(sock_path, payload)["reason"] == "credential_brokered"
        set_reveal(config, True)
        assert request(sock_path, payload)["value"] == VAULT["github_pat"]
        with db.get_db(config.db_path) as conn:
            conn.execute("DELETE FROM credential_bindings WHERE user_id=?", ("alice",))
        assert request(sock_path, payload)["reason"] == "credential_brokered"


@pytest.mark.parametrize("mode", ["read", "inject", "skill"])
def test_audit_default_is_value_free_and_records_all_public_modes(config, sock_path, caplog, mode):
    config.security.credential_broker.enforce_reveal = False
    with caplog.at_level(logging.INFO, logger="istota.skill_proxy"):
        with proxy(sock_path, config=config, user_id="alice"):
            reply = request(sock_path, {"type": "vault_credential", "name": "github_pat", "mode": mode})
    assert reply["value"] == VAULT["github_pat"]
    assert "reason=credential_brokered" in caplog.text
    assert "action=would_refuse" in caplog.text
    assert f"mode={mode}" in caplog.text
    for value in VAULT.values():
        assert value not in caplog.text


def test_refusal_consumes_shared_budget_before_presence(config, sock_path):
    with proxy(sock_path, config=config, user_id="alice", vault_fetch_limit=1) as server:
        payload = {"type": "vault_credential", "name": "github_pat"}
        assert request(sock_path, payload)["reason"] == "credential_brokered"
        for name in ("github_pat", "absent"):
            reply = request(sock_path, {**payload, "name": name})
            assert reply["reason"] == "vault_credential_limit"
            assert "name" not in reply
        assert server._vault_fetches == 3


@pytest.mark.parametrize("verb", ["get", "run", "stdin"])
@pytest.mark.parametrize("revealable", [False, True])
def test_shim_fetch_and_exec_obey_reveal(config, sock_path, verb, revealable):
    set_reveal(config, revealable)
    args = ["get", "github_pat"]
    if verb != "get":
        source = "sys.stdin.read()" if verb == "stdin" else "os.environ['TOKEN']"
        assignment = ["--stdin", "github_pat"] if verb == "stdin" else ["TOKEN=github_pat"]
        args = ["run", *assignment, "--", sys.executable, "-c",
                f"import os, sys; print({source}, end='')"]
    with proxy(sock_path, config=config, user_id="alice"):
        result = run_shim(sock_path, args)
    assert result.returncode == (0 if revealable else 1)
    assert result.stdout == (VAULT["github_pat"] if revealable else "")
    if not revealable:
        assert "credential_brokered" in result.stderr
    assert VAULT["github_pat"] not in result.stderr


@pytest.mark.parametrize("enforce", [False, True])
@pytest.mark.parametrize("name", ["GITLAB_TOKEN", "NC_PASS"])
def test_manifest_env_and_raw_client_cannot_bypass(config, sock_path, caplog, enforce, name):
    config.security.credential_broker.enforce_reveal = enforce
    with SkillProxy(sock_path, {name: "fixture-manifest-value"}, {"PATH": os.environ["PATH"]},
                    config=config, user_id="alice", allowed_credentials={name}) as server:
        result = run_shim(sock_path, ["env", name])
        reply = request(sock_path, {"type": "credential", "name": name, "mode": "skill",
                                   "trusted_skill": True})
        assert server._vault_fetches == 0
    assert result.returncode == (1 if enforce else 0)
    assert result.stdout == ("" if enforce else "fixture-manifest-value")
    if enforce:
        assert reply["reason"] == "credential_brokered"
        assert "value" not in reply
    else:
        assert reply == {"value": "fixture-manifest-value"}
    assert f"action={'refused' if enforce else 'would_refuse'}" in caplog.text
    assert "fixture-manifest-value" not in caplog.text


def test_real_private_skill_resolves_under_enforcement(config, sock_path, skill_program, caplog):
    skill_program("""
        import argparse, hashlib, json
        from istota.skills._cli import parse_and_resolve
        from istota.skills._credref import credential_ref
        parser = argparse.ArgumentParser()
        credential_ref(parser, "--secret")
        args = parse_and_resolve(parser, ["--secret", "github_pat"])
        import os
        assert os.environ["NC_PASS"] == "fixture-manifest-value"
        print(json.dumps({"digest": hashlib.sha256(args.secret.reveal().encode()).hexdigest(),
                          "hosts": args.secret.bound_hosts}))
    """)
    with start_proxy(sock_path, config=config, user_id="alice") as server:
        server.credential_env["NC_PASS"] = "fixture-manifest-value"
        response = request(sock_path, {"skill": "probe", "args": []})
        assert response["returncode"] == 0, response["stderr"]
        assert server._vault_fetches == 1
    assert json.loads(response["stdout"]) == {
        "digest": hashlib.sha256(VAULT["github_pat"].encode()).hexdigest(),
        "hosts": ["portal.example"],
    }
    assert "credential_brokered" not in caplog.text
    for value in VAULT.values():
        assert value not in json.dumps(response) + caplog.text


def test_discovery_stays_available_and_free(config, sock_path):
    with proxy(sock_path, config=config, user_id="alice", vault_fetch_limit=1) as server:
        assert request(sock_path, {"type": "vault_credential", "name": "github_pat"})["reason"] == "credential_brokered"
        result = run_shim(sock_path, ["placeholder", "github_pat"])
        assert result.returncode == 0
        assert result.stdout == "{{cred:github_pat}}"
        listed = request(sock_path, {"type": "vault_list"})
        assert listed["names"] == sorted(VAULT)
        assert server._vault_fetches == 1


def test_reveal_audit_flattens_names(config, sock_path, caplog):
    name = "forged\nrecord" + "x" * 200
    with proxy(sock_path, config=config, user_id="alice", vault_credentials={name: "fixture-value"}):
        reply = request(sock_path, {"type": "vault_credential", "name": name})
    assert reply["reason"] == "credential_brokered"
    assert "\n" not in reply["name"]
    for record in caplog.records:
        assert "\n" not in record.getMessage()
        assert "fixture-value" not in record.getMessage()
        assert len(record.getMessage()) < 300
