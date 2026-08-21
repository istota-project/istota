"""The wrapper's exec path, driven as a real subprocess.

`main` ends in os.execve, which the pure-function tests cannot reach. Here the
wrapper is deployed the way it actually is — a copy of the module named `gh` —
and pointed at a fake binary, so the assertions are on what the real CLI would
have received. Nothing here needs a real gh or glab installed.
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

import pytest

from istota.forge_cli import (
    EXIT_CREDENTIAL,
    EXIT_DENIED,
    EXIT_EXEC,
    EXIT_NO_PROXY,
    EXIT_USAGE,
    FORGE_GITHUB,
    build_policy,
)

SENTINEL = "ghp_sentineltokenvalue0000000000000000"

_MODULE = Path(__file__).resolve().parent.parent / "src/istota/forge_cli.py"

# Prints what the wrapper handed it. Anything not echoed here is, as far as the
# assertions are concerned, not in the child's environment.
_FAKE_BIN = """#!/bin/sh
echo "ARGV:$*"
echo "GH_TOKEN:${GH_TOKEN-<unset>}"
echo "GH_ENTERPRISE_TOKEN:${GH_ENTERPRISE_TOKEN-<unset>}"
echo "GH_HOST:${GH_HOST-<unset>}"
echo "GH_CONFIG_DIR:${GH_CONFIG_DIR-<unset>}"
echo "GH_DEBUG:${GH_DEBUG-<unset>}"
echo "PATH:${PATH-<unset>}"
echo "SOCK:${ISTOTA_SKILL_PROXY_SOCK-<unset>}"
echo "POLICY:${ISTOTA_FORGE_POLICY-<unset>}"
exit 0
"""


class FakeCredentialProxy:
    """A one-shot-per-connection Unix socket speaking the skill-proxy shape."""

    def __init__(self, path, reply):
        self.path = str(path)
        self.reply = reply
        self.requests = []
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(self.path)
        self._sock.listen(8)
        self._stop = False
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        while not self._stop:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            with conn:
                data = b""
                while b"\n" not in data:
                    got = conn.recv(4096)
                    if not got:
                        break
                    data += got
                if data.strip():
                    try:
                        self.requests.append(json.loads(data.split(b"\n", 1)[0]))
                    except ValueError:
                        pass
                conn.sendall(json.dumps(self.reply).encode() + b"\n")

    def close(self):
        self._stop = True
        try:
            self._sock.close()
        except OSError:
            pass


@pytest.fixture
def sock_path():
    """A socket path short enough for AF_UNIX (~104 bytes).

    pytest's tmp_path is far too long on macOS, which is the same reason
    executor.py puts the skill-proxy socket under the system temp root rather
    than beside the task's other files.
    """
    d = tempfile.mkdtemp(prefix="fcli", dir=tempfile.gettempdir())
    path = os.path.join(d, "s")
    yield path
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture
def deployed(tmp_path):
    """The wrapper installed as `gh`, next to a fake real binary."""
    wrapper = tmp_path / "bin" / "gh"
    wrapper.parent.mkdir(parents=True)
    shutil.copy(_MODULE, wrapper)
    wrapper.chmod(0o700)

    real = tmp_path / "real-gh"
    real.write_text(_FAKE_BIN)
    real.chmod(0o700)

    cfg = tmp_path / "gh-config"
    cfg.mkdir()
    return wrapper, real, cfg


def _run(wrapper, args, env):
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    base.update(env)
    return subprocess.run(
        [sys.executable, str(wrapper), *args],
        capture_output=True, text=True, env=base, timeout=30,
    )


def _fields(stdout):
    out = {}
    for line in stdout.splitlines():
        key, _, value = line.partition(":")
        out[key] = value
    return out


class TestExecPath:
    def test_arguments_arrive_unchanged(self, deployed, tmp_path, sock_path):
        wrapper, real, cfg = deployed
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["pr", "list", "--state", "open"], {
                "ISTOTA_GH_REAL": str(real),
                "ISTOTA_GH_CONFIG_DIR": str(cfg),
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == 0, r.stderr
        assert _fields(r.stdout)["ARGV"] == "pr list --state open"

    def test_token_reaches_the_real_binary(self, deployed, tmp_path, sock_path):
        wrapper, real, cfg = deployed
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_GH_REAL": str(real),
                "ISTOTA_GH_CONFIG_DIR": str(cfg),
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
                "ISTOTA_GH_URL": "https://github.com",
            })
        finally:
            proxy.close()
        fields = _fields(r.stdout)
        assert fields["GH_TOKEN"] == SENTINEL
        assert fields["GH_ENTERPRISE_TOKEN"] == "<unset>"
        assert fields["GH_HOST"] == "github.com"
        assert fields["GH_CONFIG_DIR"] == str(cfg)
        assert proxy.requests == [{"type": "credential", "name": "GITHUB_TOKEN"}]

    def test_debug_and_policy_do_not_reach_the_real_binary(self, deployed, tmp_path, sock_path):
        wrapper, real, cfg = deployed
        policy_file = tmp_path / "policy.json"
        policy_file.write_text(json.dumps({FORGE_GITHUB: build_policy(FORGE_GITHUB)}))
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_GH_REAL": str(real),
                "ISTOTA_GH_CONFIG_DIR": str(cfg),
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
                "ISTOTA_FORGE_POLICY": str(policy_file),
                "GH_DEBUG": "api",
            })
        finally:
            proxy.close()
        fields = _fields(r.stdout)
        assert fields["GH_DEBUG"] == "<unset>"
        assert fields["POLICY"] == "<unset>"

    def test_proxy_socket_is_carried_for_gits_credential_helper(self, deployed, tmp_path, sock_path):
        wrapper, real, cfg = deployed
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_GH_REAL": str(real),
                "ISTOTA_GH_CONFIG_DIR": str(cfg),
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert _fields(r.stdout)["SOCK"] == proxy.path

    def test_path_reaches_the_child_unchanged(self, deployed, tmp_path, sock_path):
        wrapper, real, cfg = deployed
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["pr", "list"], {
                "PATH": "/opt/x:/usr/bin:/bin",
                "ISTOTA_GH_REAL": str(real),
                "ISTOTA_GH_CONFIG_DIR": str(cfg),
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert _fields(r.stdout)["PATH"] == "/opt/x:/usr/bin:/bin"


class TestRefusals:
    def test_denied_verb_never_reaches_the_binary(self, deployed, tmp_path, sock_path):
        wrapper, real, cfg = deployed
        marker = tmp_path / "ran"
        real.write_text(f"#!/bin/sh\ntouch {marker}\n")
        real.chmod(0o700)
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["repo", "delete", "someorg/somerepo"], {
                "ISTOTA_GH_REAL": str(real),
                "ISTOTA_GH_CONFIG_DIR": str(cfg),
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == EXIT_DENIED
        assert not marker.exists()
        assert "not permitted" in r.stderr
        # No token was even fetched for a denied verb.
        assert proxy.requests == []

    def test_denied_message_does_not_overclaim(self, deployed, tmp_path):
        wrapper, real, cfg = deployed
        r = _run(wrapper, ["auth", "token"], {
            "ISTOTA_GH_REAL": str(real),
            "ISTOTA_GH_CONFIG_DIR": str(cfg),
            "ISTOTA_SKILL_PROXY_SOCK": str(tmp_path / "absent.sock"),
        })
        assert r.returncode == EXIT_DENIED
        assert "accident guard" in r.stderr
        assert "not a security boundary" in r.stderr

    def test_no_proxy_exits_four(self, deployed):
        wrapper, real, cfg = deployed
        r = _run(wrapper, ["pr", "list"], {
            "ISTOTA_GH_REAL": str(real),
            "ISTOTA_GH_CONFIG_DIR": str(cfg),
        })
        assert r.returncode == EXIT_NO_PROXY
        assert "no credential proxy" in r.stderr

    def test_credential_error_exits_five_without_the_token(self, deployed, tmp_path, sock_path):
        wrapper, real, cfg = deployed
        proxy = FakeCredentialProxy(sock_path, {"error": "not_authorized_credential"},
        )
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_GH_REAL": str(real),
                "ISTOTA_GH_CONFIG_DIR": str(cfg),
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == EXIT_CREDENTIAL
        assert "not_authorized_credential" in r.stderr
        assert SENTINEL not in r.stderr

    def test_missing_real_binary_exits_six_without_the_token(self, deployed, tmp_path, sock_path):
        """Exit 6 is the one error path that formats a message with the token
        already fetched and in scope, so it is the leakage case that matters."""
        wrapper, _, cfg = deployed
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_GH_REAL": str(tmp_path / "does-not-exist"),
                "ISTOTA_GH_CONFIG_DIR": str(cfg),
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == EXIT_EXEC
        assert SENTINEL not in r.stderr
        assert SENTINEL not in r.stdout
        assert "does-not-exist" in r.stderr

    def test_non_executable_real_binary_exits_six(self, deployed, tmp_path, sock_path):
        wrapper, real, cfg = deployed
        real.chmod(0o600)
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_GH_REAL": str(real),
                "ISTOTA_GH_CONFIG_DIR": str(cfg),
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == EXIT_EXEC
        assert SENTINEL not in r.stderr
        assert "Traceback" not in r.stderr


class TestMetaAndRetiredNames:
    def test_version_works_without_any_proxy(self, deployed):
        """A version check is not a forge call. Requiring a credential proxy
        for one breaks every deployment with the proxy switched off."""
        wrapper, real, cfg = deployed
        r = _run(wrapper, ["--version"], {
            "ISTOTA_GH_REAL": str(real),
            "ISTOTA_GH_CONFIG_DIR": str(cfg),
        })
        assert r.returncode == 0, r.stderr
        fields = _fields(r.stdout)
        assert fields["ARGV"] == "--version"
        assert fields["GH_TOKEN"] == "<unset>"

    @pytest.mark.parametrize("name", ["github-api", "gitlab-api"])
    def test_retired_names_exit_two(self, deployed, tmp_path, name):
        wrapper, real, _ = deployed
        retired = wrapper.parent / name
        shutil.copy(_MODULE, retired)
        retired.chmod(0o700)
        r = _run(retired, [], {"ISTOTA_GH_REAL": str(real)})
        assert r.returncode == EXIT_USAGE
        assert "retired" in r.stderr
        assert "gh api" in r.stderr

    def test_unknown_name_exits_two(self, deployed, tmp_path):
        wrapper, real, _ = deployed
        odd = wrapper.parent / "hub"
        shutil.copy(_MODULE, odd)
        odd.chmod(0o700)
        r = _run(odd, ["pr", "list"], {"ISTOTA_GH_REAL": str(real)})
        assert r.returncode == EXIT_USAGE
        assert "not a recognised forge CLI name" in r.stderr


class TestDevboxBackend:
    def test_forge_token_action_used_when_only_cred_sock_present(self, deployed, tmp_path, sock_path):
        wrapper, real, cfg = deployed
        proxy = FakeCredentialProxy(sock_path, {"ok": True, "token": SENTINEL},
        )
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_GH_REAL": str(real),
                "ISTOTA_GH_CONFIG_DIR": str(cfg),
                "ISTOTA_CRED_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == 0, r.stderr
        assert _fields(r.stdout)["GH_TOKEN"] == SENTINEL
        assert proxy.requests == [
            {"action": "forge_token", "provider": "github"},
        ]

    def test_devbox_error_envelope_exits_five(self, deployed, tmp_path, sock_path):
        wrapper, real, cfg = deployed
        proxy = FakeCredentialProxy(sock_path,
            {"ok": False, "error": "no_token", "message": "no token configured for github"},
        )
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_GH_REAL": str(real),
                "ISTOTA_GH_CONFIG_DIR": str(cfg),
                "ISTOTA_CRED_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == EXIT_CREDENTIAL
        assert "no token configured" in r.stderr
