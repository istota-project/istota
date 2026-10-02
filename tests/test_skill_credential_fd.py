"""Private credential delivery through a real skill subprocess."""

import hashlib
import json
import os
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from istota.sandbox.skill_proxy import SkillProxy
from istota.skills._credref import _resolve_name
from tests import test_vault_credential_fetch as _vault_fetch
from tests.test_vault_credential_fetch import VAULT, request

# Shared fixture, bound by assignment so the parameters using it are not
# read as redefinitions of an unused import.
sock_path = _vault_fetch.sock_path


@pytest.fixture
def skill_program(tmp_path, monkeypatch):
    """Replace only the CLI entry point, keeping the real spawn and parser."""
    def install(body):
        program = tmp_path / "skill.py"
        program.write_text(textwrap.dedent(body))
        launcher = tmp_path / "python-skill"
        launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{program}" "$@"\n')
        launcher.chmod(0o700)
        monkeypatch.setattr("istota.sandbox.skill_proxy.sys.executable", str(launcher))
    return install


def start_proxy(sock_path, **kwargs):
    env = {"PATH": os.environ["PATH"], "PYTHONPATH": str(Path("src").resolve())}
    return SkillProxy(sock_path, {}, env, vault_credentials=VAULT, **kwargs)


def test_spawned_skill_resolves_without_model_socket(sock_path, skill_program):
    skill_program("""
        import argparse, hashlib, json, os, subprocess, sys
        from istota.skills._cli import parse_and_resolve
        from istota.skills._credref import credential_ref
        assert "ISTOTA_SKILL_PROXY_SOCK" not in os.environ
        fd = int(os.environ["ISTOTA_CRED_FD"])
        assert not os.get_inheritable(fd)
        child = subprocess.run([sys.executable, "-c",
            "import os; os.fstat(int(os.environ['ISTOTA_CRED_FD']))"],
            close_fds=False, capture_output=True)
        assert child.returncode != 0
        parser = argparse.ArgumentParser()
        credential_ref(parser, "--secret", action="append")
        args = parse_and_resolve(parser, ["--secret", "github_pat", "--secret", "home_assistant_token"])
        print(json.dumps({"digests": [hashlib.sha256(v.reveal().encode()).hexdigest() for v in args.secret],
                          "boxed": repr(args), "hosts": [v.bound_hosts for v in args.secret]}))
    """)
    with start_proxy(sock_path) as proxy:
        response = request(sock_path, {"skill": "probe", "args": []})
        assert response["returncode"] == 0, response["stderr"]
        assert proxy._vault_fetches == 2
    result = json.loads(response["stdout"])
    assert result["digests"] == [hashlib.sha256(VAULT[n].encode()).hexdigest()
                                 for n in ("github_pat", "home_assistant_token")]
    assert result["hosts"] == [[], []]
    for value in VAULT.values():
        assert value not in json.dumps(response)


@pytest.mark.parametrize("fd", ["invalid", "-1", "999999", ""])
def test_bad_private_fd_never_falls_back_to_model_socket(sock_path, monkeypatch, fd):
    monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", str(sock_path))
    monkeypatch.setenv("ISTOTA_CRED_FD", fd)
    with start_proxy(sock_path) as proxy:
        value, error = _resolve_name("github_pat", "probe")
        assert value is None
        assert "credential" in error.lower()
        assert proxy._vault_fetches == 0


def test_skill_fd_shares_fetch_limit_and_refuses_before_dispatch(sock_path, skill_program):
    skill_program("""
        import argparse
        from istota.skills._cli import parse_and_resolve
        from istota.skills._credref import credential_ref
        parser = argparse.ArgumentParser()
        credential_ref(parser, "--secret", action="append")
        parse_and_resolve(parser, ["--secret", "github_pat", "--secret", "github_pat"])
        print("HANDLER RAN")
    """)
    with start_proxy(sock_path, vault_fetch_limit=2) as proxy:
        assert "value" in request(sock_path, {"type": "vault_credential", "name": "github_pat"})
        response = request(sock_path, {"skill": "probe", "args": []})
        assert proxy._vault_fetches == 3
    assert response["returncode"] == 1
    assert "HANDLER RAN" not in response["stdout"]
    assert "fetch limit" in response["stdout"]


@pytest.mark.parametrize("failure", [None, "timeout", "spawn"])
def test_channel_closed_after_invocation(sock_path, monkeypatch, failure):
    held = []
    def run(*args, **kwargs):
        fd = int(kwargs["env"]["ISTOTA_CRED_FD"])
        assert fd in kwargs["pass_fds"]
        endpoint = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)
        endpoint.settimeout(2)
        held.append(endpoint)
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args[0], 1)
        if failure == "spawn":
            raise OSError("spawn failed")
        return subprocess.CompletedProcess(args[0], 0, "ok", "")
    monkeypatch.setattr("istota.sandbox.skill_proxy.subprocess.run", run)
    try:
        with start_proxy(sock_path) as proxy:
            response = request(sock_path, {"skill": "probe", "args": []})
            assert response["returncode"] == {None: 0, "timeout": 124, "spawn": 1}[failure]
            assert held[0].recv(1) == b""
            assert proxy._vault_fetches == 0
    finally:
        for endpoint in held:
            endpoint.close()


def test_private_channel_preserves_live_binding_and_value(tmp_path, sock_path, monkeypatch):
    from istota import db
    from istota.credentials import store as secrets_store
    from istota.sandbox import credential_shim
    from istota.config import Config
    from istota.credentials.broker.bindings import parse_binding

    monkeypatch.setenv("ISTOTA_SECRET_KEY", "a" * 64)
    config = Config(db_path=tmp_path / "data.db")
    db.init_db(config.db_path)
    with start_proxy(sock_path, config=config, user_id="alice") as proxy:
        with proxy._credential_channel(2) as fd:
            for host in ("portal.example", "other.example"):
                secrets_store.upsert_secret(
                    config.db_path, "alice", "vault_entries", "github_pat", host,
                    binding=parse_binding("https://" + host, {}, []),
                )
                assert credential_shim.fetch_credential(
                    "github_pat", "skill", binding=True, credential_fd=str(fd),
                ) == (host, [host])
            secrets_store.delete_secret(config.db_path, "alice", "vault_entries", "github_pat")
            with pytest.raises(credential_shim.ProxyError, match="no longer available"):
                credential_shim.fetch_credential("github_pat", "skill", binding=True, credential_fd=str(fd))


def test_private_provenance_is_server_owned(sock_path, monkeypatch):
    from istota.sandbox import credential_shim

    with start_proxy(sock_path) as proxy:
        calls = []
        serve = proxy._serve_vault_credential
        def record(conn, payload, *, trusted_skill=False):
            calls.append(trusted_skill)
            return serve(conn, payload, trusted_skill=trusted_skill)
        monkeypatch.setattr(proxy, "_serve_vault_credential", record)
        request(sock_path, {"type": "vault_credential", "name": "github_pat",
                            "mode": "skill", "trusted_skill": True})
        with proxy._credential_channel(2) as first:
            with proxy._credential_channel(2) as second:
                assert first != second
                credential_shim.fetch_credential("github_pat", "read", credential_fd=str(first))
            # Closing the second invocation leaves the first one usable.
            credential_shim.fetch_credential("github_pat", "read", credential_fd=str(first))
        assert calls == [False, True, True]


@pytest.mark.parametrize("payload", [b"not json\n", b"[]\n", b"x" * 65537,
                                     b'{"type":"vault_create"}\n'])
def test_private_channel_rejects_malformed_or_other_operations(sock_path, payload):
    with start_proxy(sock_path) as proxy:
        with proxy._credential_channel(2) as fd:
            with socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM) as endpoint:
                endpoint.settimeout(2)
                endpoint.sendall(payload)
                with endpoint.makefile("rb") as reader:
                    answer = reader.read()
                assert b"value" not in answer
                assert proxy._vault_fetches == 0


def test_disconnected_private_fd_does_not_fall_back(sock_path, monkeypatch):
    monkeypatch.setenv("ISTOTA_SKILL_PROXY_SOCK", str(sock_path))
    server, child = socket.socketpair()
    server.close()
    with child, start_proxy(sock_path) as proxy:
        monkeypatch.setenv("ISTOTA_CRED_FD", str(child.fileno()))
        value, error = _resolve_name("github_pat", "probe")
        assert value is None
        assert "credential" in error.lower()
        assert proxy._vault_fetches == 0
