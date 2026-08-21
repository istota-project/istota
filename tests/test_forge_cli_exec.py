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
import tempfile
import threading
from pathlib import Path

import pytest

from istota.forge_cli import (
    ACTION_FORGE_TOKEN,
    EXIT_CREDENTIAL,
    EXIT_DENIED,
    EXIT_EXEC,
    EXIT_MISCONFIGURED,
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
    """A one-shot-per-connection Unix socket speaking the skill-proxy shape.

    ``known_actions`` makes the devbox branch testable against reality: a
    request naming an action the real proxy would not recognise gets the real
    proxy's ``unknown_action`` envelope, not the canned reply. Without that,
    the test asserts the wrapper against the test's own assumption.
    """

    def __init__(self, path, reply, known_actions=None):
        self.path = str(path)
        self.reply = reply
        self.known_actions = known_actions
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
            if self._stop:
                conn.close()   # the waker from close(), not a real client
                return
            with conn:
                data = b""
                while b"\n" not in data:
                    got = conn.recv(4096)
                    if not got:
                        break
                    data += got
                request = None
                if data.strip():
                    try:
                        request = json.loads(data.split(b"\n", 1)[0])
                        self.requests.append(request)
                    except ValueError:
                        pass
                reply = self.reply
                if (
                    self.known_actions is not None
                    and isinstance(request, dict)
                    and request.get("action") not in self.known_actions
                ):
                    reply = {
                        "ok": False, "error": "unknown_action",
                        "message": f"unknown action: {request.get('action')!r}",
                    }
                try:
                    conn.sendall(json.dumps(reply).encode() + b"\n")
                except OSError:
                    pass   # client already gone; nothing to report

    def close(self):
        # accept() is blocking and only reads _stop between connections, so
        # closing the fd is not enough to wake it on Linux. Connect once to
        # push it round the loop, then join.
        self._stop = True
        try:
            waker = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            waker.settimeout(1)
            waker.connect(self.path)
            waker.close()
        except OSError:
            pass
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=5)


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


def _write_policy(bin_dir, real_bin, cfg, *, forge="github", **overrides):
    """The policy file, beside the wrapper — which is where it looks.

    Nothing here travels by environment any more: the wrapper runs as a child
    of the model's own shell, so an env-supplied policy path is a policy the
    model chooses. Tests that want a different setting change the file, the
    same way the deployment does.
    """
    from istota.forge_cli import FORGE_GITHUB, FORGE_GITLAB, build_policy

    policy = {}
    for name in (FORGE_GITHUB, FORGE_GITLAB):
        section = build_policy(name)
        section["real_bin"] = str(real_bin)
        section["config_dir"] = str(cfg)
        section["url"] = "https://github.com" if name == FORGE_GITHUB else "https://gitlab.com"
        if name == forge:
            section.update(overrides)
        policy[name] = section
    path = bin_dir / "forge-policy.json"
    path.write_text(json.dumps(policy))
    return path


def _deploy(tmp_path, **overrides):
    """The wrapper installed as `gh`, next to a fake real binary and a policy.

    ``overrides`` go to the github section, so a test can deploy the devbox
    shape (``url=""``) rather than the sandbox one.
    """
    wrapper = tmp_path / "bin" / "gh"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_MODULE, wrapper)
    wrapper.chmod(0o700)

    real = tmp_path / "real-gh"
    real.write_text(_FAKE_BIN)
    real.chmod(0o700)

    cfg = tmp_path / "gh-config"
    cfg.mkdir(exist_ok=True)
    _write_policy(wrapper.parent, real, cfg, **overrides)
    return wrapper, real, cfg


def _deploy_glab(tmp_path, **overrides):
    """The same, installed as `glab`, so the gitlab branch can be exercised."""
    wrapper = tmp_path / "bin" / "glab"
    wrapper.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(_MODULE, wrapper)
    wrapper.chmod(0o700)

    real = tmp_path / "real-glab"
    real.write_text(_FAKE_BIN)
    real.chmod(0o700)

    cfg = tmp_path / "glab-config"
    cfg.mkdir(exist_ok=True)
    _write_policy(wrapper.parent, real, cfg, forge="gitlab", **overrides)
    return wrapper, real, cfg


@pytest.fixture
def deployed(tmp_path):
    """The sandbox shape: a policy that names its own forge URL."""
    return _deploy(tmp_path)


def _run(wrapper, args, env):
    """Exec the wrapper file itself, not `python3 wrapper`.

    The deployed shape is a copy of the module at /usr/local/bin/gh with the
    executable bit set, which the kernel execs directly. Going through
    sys.executable hides a missing shebang: the kernel returns ENOEXEC, the
    shell retries the file as /bin/sh, and sh parses the docstring as commands
    and exits 2 — colliding with EXIT_USAGE.
    """
    base = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    base.update(env)
    return subprocess.run(
        [str(wrapper), *args],
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
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
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
            "ISTOTA_SKILL_PROXY_SOCK": str(tmp_path / "absent.sock"),
        })
        assert r.returncode == EXIT_DENIED
        assert "accident guard" in r.stderr
        assert "not a security boundary" in r.stderr

    def test_no_proxy_exits_four(self, deployed):
        wrapper, real, cfg = deployed
        r = _run(wrapper, ["pr", "list"], {
        })
        assert r.returncode == EXIT_NO_PROXY
        assert "no credential proxy" in r.stderr

    def test_missing_config_dir_refuses_rather_than_falling_back(
        self, deployed, tmp_path, sock_path,
    ):
        """An empty GH_CONFIG_DIR is read as unset, and gh then falls back to
        $HOME/.config/gh - writable, and gh expands `aliases` from it before
        dispatch, so the deny list stops applying. Fail loudly instead."""
        wrapper, real, _ = deployed
        # A policy with no config_dir is the wiring mistake this guards.
        _write_policy(wrapper.parent, real, "", config_dir="")
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == EXIT_MISCONFIGURED
        assert "config directory" in r.stderr
        assert proxy.requests == []   # no token fetched either

    def test_credential_error_exits_five_without_the_token(self, deployed, tmp_path, sock_path):
        wrapper, real, cfg = deployed
        proxy = FakeCredentialProxy(sock_path, {"error": "not_authorized_credential"},
        )
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == EXIT_CREDENTIAL
        assert "not_authorized_credential" in r.stderr
        # Regression net, not a real leakage assertion: this path fails before
        # a token is ever fetched, so the sentinel is not in the wrapper's
        # process to leak. The exit-6 test below is the load-bearing one.
        assert SENTINEL not in r.stderr

    def test_missing_real_binary_exits_six_without_the_token(self, deployed, tmp_path, sock_path):
        """Exit 6 is the one error path that formats a message with the token
        already fetched and in scope, so it is the leakage case that matters."""
        wrapper, real, cfg = deployed
        missing = tmp_path / "does-not-exist"
        _write_policy(wrapper.parent, missing, cfg)
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == EXIT_EXEC
        assert SENTINEL not in r.stderr
        assert SENTINEL not in r.stdout
        assert "does-not-exist" in r.stderr


class TestEnvironmentCannotRedirectTheWrapper:
    """The model sets its own environment; the wrapper must not read its
    trust anchors from there.

    Each of these was a one-token bypass while the setting came from an
    ISTOTA_* variable: point the policy at a toothless file, or the config dir
    at one carrying an `aliases:` block, or the real binary at anything at all.
    They now travel in the policy file, whose location the wrapper computes
    from its own path.
    """

    def test_real_binary_env_override_is_ignored(self, deployed, tmp_path, sock_path):
        wrapper, real, cfg = deployed
        impostor = tmp_path / "impostor"
        impostor.write_text("#!/bin/sh\necho IMPOSTOR RAN\n")
        impostor.chmod(0o700)
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_GH_REAL": str(impostor),
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == 0, r.stderr
        assert "IMPOSTOR" not in r.stdout
        assert _fields(r.stdout)["ARGV"] == "pr list"

    def test_policy_env_override_is_ignored(self, deployed, tmp_path, sock_path):
        """A shape-valid policy naming no real rule would disable the lot."""
        wrapper, real, cfg = deployed
        toothless = tmp_path / "toothless.json"
        toothless.write_text(json.dumps({
            FORGE_GITHUB: {
                "path_rules": [["never", "matches"]],
                "flag_value_rules": [], "body_flag_rules": [],
            },
        }))
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["repo", "delete", "someorg/somerepo"], {
                "ISTOTA_FORGE_POLICY": str(toothless),
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == EXIT_DENIED
        assert "repo delete" in r.stderr

    def test_config_dir_env_override_is_ignored(self, deployed, tmp_path, sock_path):
        """gh expands aliases from config.yml before dispatch, so a config dir
        the model picks is a config dir the model writes."""
        wrapper, real, cfg = deployed
        mine = tmp_path / "mine"
        mine.mkdir()
        (mine / "config.yml").write_text("aliases:\n    x: repo delete\n")
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_GH_CONFIG_DIR": str(mine),
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == 0, r.stderr
        assert _fields(r.stdout)["GH_CONFIG_DIR"] == str(cfg)

    def test_non_executable_real_binary_exits_six(self, deployed, tmp_path, sock_path):
        wrapper, real, cfg = deployed
        real.chmod(0o600)
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["pr", "list"], {
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
                "ISTOTA_CRED_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == 0, r.stderr
        assert _fields(r.stdout)["GH_TOKEN"] == SENTINEL
        assert proxy.requests == [
            {"action": ACTION_FORGE_TOKEN, "provider": "github"},
        ]

    def test_forge_token_is_a_real_devbox_action(self, deployed, tmp_path, sock_path):
        """The seam between the two copies of the action name. forge_cli.py
        cannot import istota, so it carries its own ``ACTION_FORGE_TOKEN``;
        this is where a rename on one side would go unnoticed."""
        from istota.devbox_proxy_protocol import ALL_ACTIONS

        assert ACTION_FORGE_TOKEN in ALL_ACTIONS
        wrapper, real, cfg = deployed
        proxy = FakeCredentialProxy(
            sock_path, {"ok": True, "token": SENTINEL}, known_actions=ALL_ACTIONS,
        )
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_CRED_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == 0, r.stderr
        assert _fields(r.stdout)["GH_TOKEN"] == SENTINEL

    def test_proxy_supplied_url_sets_the_host_when_the_policy_has_none(
        self, tmp_path, sock_path,
    ):
        """The devbox shape: one image shared by every user, so its baked
        policy names no per-user URL and the proxy supplies one. Left
        unresolved the wrapper falls back to github.com / gitlab.com and a
        self-hosted token goes to the wrong host, which is a disclosure."""
        wrapper, real, cfg = _deploy(tmp_path, url="")
        proxy = FakeCredentialProxy(
            sock_path,
            {"ok": True, "token": SENTINEL, "url": "https://ghe.example.com"},
        )
        try:
            r = _run(wrapper, ["pr", "list"], {"ISTOTA_CRED_SOCK": proxy.path})
        finally:
            proxy.close()
        assert r.returncode == 0, r.stderr
        fields = _fields(r.stdout)
        assert fields["GH_HOST"] == "ghe.example.com"
        # Not github.com, so the enterprise variable is the one that carries
        # it; GH_TOKEN would leave every call unauthenticated.
        assert fields["GH_TOKEN"] == "<unset>"
        assert fields["GH_ENTERPRISE_TOKEN"] == SENTINEL

    def test_policy_url_beats_the_proxy_supplied_one(
        self, deployed, tmp_path, sock_path,
    ):
        """The sandbox's policy is the trust anchor and must stay supreme: a
        proxy answer cannot redirect a deployment that stated its own URL."""
        wrapper, real, cfg = deployed
        proxy = FakeCredentialProxy(
            sock_path,
            {"ok": True, "token": SENTINEL, "url": "https://evil.example.com"},
        )
        try:
            r = _run(wrapper, ["pr", "list"], {"ISTOTA_CRED_SOCK": proxy.path})
        finally:
            proxy.close()
        assert r.returncode == 0, r.stderr
        assert _fields(r.stdout)["GH_HOST"] == "github.com"

    def test_missing_url_everywhere_refuses_rather_than_guessing(
        self, tmp_path, sock_path,
    ):
        """No URL in the policy and none from the proxy must refuse, not fall
        back to the public host.

        Falling back is a credential disclosure, not a misroute: on a GitHub
        Enterprise Server or self-hosted GitLab deployment the token is scoped
        to that instance, and the CLI's own default sends it to github.com /
        gitlab.com. The first cut of this treated the missing URL as benign
        because the *github.com* case happens to be harmless — which is the
        one deployment where it is.
        """
        wrapper, real, cfg = _deploy(tmp_path, url="")
        proxy = FakeCredentialProxy(sock_path, {"ok": True, "token": SENTINEL})
        try:
            r = _run(wrapper, ["pr", "list"], {"ISTOTA_CRED_SOCK": proxy.path})
        finally:
            proxy.close()
        assert r.returncode == EXIT_MISCONFIGURED, r.stdout
        assert "no forge URL" in r.stderr
        # The real binary must never have run — the fake prints these fields.
        assert "GH_HOST" not in r.stdout

    def test_missing_url_refuses_for_gitlab_too(self, tmp_path, sock_path):
        """The gitlab counterpart, which is the case that actually bites:
        GITLAB_HOST is set only `if forge_url`, so an unset one silently means
        gitlab.com while GITLAB_TOKEN is still populated."""
        wrapper, real, cfg = _deploy_glab(tmp_path, url="")
        proxy = FakeCredentialProxy(sock_path, {"ok": True, "token": SENTINEL})
        try:
            r = _run(wrapper, ["mr", "list"], {"ISTOTA_CRED_SOCK": proxy.path})
        finally:
            proxy.close()
        assert r.returncode == EXIT_MISCONFIGURED, r.stdout
        assert "no forge URL" in r.stderr

    def test_meta_invocation_still_works_without_a_url(self, tmp_path):
        """`gh --version` reaches no forge and needs no token, so the URL
        refusal must not catch it."""
        wrapper, real, cfg = _deploy(tmp_path, url="")
        r = _run(wrapper, ["--version"], {})
        assert r.returncode == 0, r.stderr

    def test_devbox_error_envelope_exits_five(self, deployed, tmp_path, sock_path):
        wrapper, real, cfg = deployed
        proxy = FakeCredentialProxy(sock_path,
            {"ok": False, "error": "no_token", "message": "no token configured for github"},
        )
        try:
            r = _run(wrapper, ["pr", "list"], {
                "ISTOTA_CRED_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == EXIT_CREDENTIAL
        assert "no token configured" in r.stderr


def _seed_config_dir(base, name, file_mode, dir_mode=0o500):
    """A CLI config dir the way the deployment seeds it: empty config.yml,
    then the directory locked down. Mode is a parameter because gh and glab
    disagree about what they will accept."""
    cfg = base / name
    cfg.mkdir()
    (cfg / "config.yml").write_text("")
    (cfg / "config.yml").chmod(file_mode)
    cfg.chmod(dir_mode)
    return cfg


@pytest.mark.integration
class TestAgainstRealBinary:
    """The seam the fakes cannot cover: the real CLIs' own argv, env and
    config handling. Skipped wherever the binary is not installed.

    These pin what Stage 2a measured. The wrapper's whole config-dir design
    rests on the real binaries tolerating a read-only directory, and one of
    them very nearly does not.
    """

    def test_version_through_the_wrapper(self, deployed, tmp_path):
        gh = shutil.which("gh")
        if gh is None:
            pytest.skip("gh not installed")
        wrapper, _, cfg = deployed
        _write_policy(wrapper.parent, gh, cfg)
        r = _run(wrapper, ["--version"], {})
        assert r.returncode == 0, r.stderr
        assert "gh version" in r.stdout

    def test_real_gh_accepts_a_read_only_config_dir(self, deployed, tmp_path):
        """gh expands `aliases` from config.yml before dispatch, so the config
        dir must not be writable by the model. Measured against gh 2.98: a
        0500 directory holding a 0400 config.yml is fine."""
        gh = shutil.which("gh")
        if gh is None:
            pytest.skip("gh not installed")
        wrapper, _, _ = deployed
        cfg = _seed_config_dir(tmp_path, "ro-gh", 0o400)
        try:
            _write_policy(wrapper.parent, gh, cfg)
            r = _run(wrapper, ["--version"], {})
            assert r.returncode == 0, r.stderr
            assert "gh version" in r.stdout
        finally:
            cfg.chmod(0o700)

    def test_real_glab_requires_config_yml_at_0600(self, deployed, tmp_path):
        """glab refuses to start on any other mode — "has the permissions 400,
        but glab requires 600". So the seeded file is 0600 and immutability
        comes from the sandbox's read-only bind, not from the file mode. If
        this ever passes at 0400, the seeding step can be simplified."""
        glab = shutil.which("glab")
        if glab is None:
            pytest.skip("glab not installed")
        wrapper, _, _ = deployed
        glab_wrapper = wrapper.parent / "glab"
        shutil.copy(_MODULE, glab_wrapper)
        glab_wrapper.chmod(0o700)

        strict = _seed_config_dir(tmp_path, "ro-glab-400", 0o400)
        loose = _seed_config_dir(tmp_path, "ro-glab-600", 0o600)
        try:
            _write_policy(glab_wrapper.parent, glab, strict, forge="gitlab")
            bad = _run(glab_wrapper, ["--version"], {})
            assert bad.returncode != 0
            assert "600" in (bad.stdout + bad.stderr)

            _write_policy(glab_wrapper.parent, glab, loose, forge="gitlab")
            good = _run(glab_wrapper, ["--version"], {})
            assert good.returncode == 0, good.stderr
            assert "glab" in good.stdout
        finally:
            strict.chmod(0o700)
            loose.chmod(0o700)

    def test_real_gh_ignores_a_planted_extension(self, deployed, tmp_path, sock_path):
        """gh execs gh-<name> from $XDG_DATA_HOME/gh/extensions for an unknown
        first argument — argv the deny list cannot see. The wrapper pins
        XDG_DATA_HOME at an empty directory to shut that."""
        gh = shutil.which("gh")
        if gh is None:
            pytest.skip("gh not installed")
        wrapper, _, cfg = deployed
        planted = tmp_path / "planted"
        ext = planted / "gh" / "extensions" / "gh-pwned"
        ext.mkdir(parents=True)
        (ext / "gh-pwned").write_text("#!/bin/sh\necho EXTENSION EXECUTED\n")
        (ext / "gh-pwned").chmod(0o755)
        pinned = tmp_path / "pinned-data"
        pinned.mkdir()
        _write_policy(wrapper.parent, gh, cfg, data_dir=str(pinned))

        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["pwned"], {
                "XDG_DATA_HOME": str(planted),
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert "EXTENSION EXECUTED" not in r.stdout
        assert "unknown command" in (r.stdout + r.stderr).lower()

    def test_denied_verb_never_reaches_the_real_binary(self, deployed, tmp_path, sock_path):
        gh = shutil.which("gh")
        if gh is None:
            pytest.skip("gh not installed")
        wrapper, _, cfg = deployed
        proxy = FakeCredentialProxy(sock_path, {"value": SENTINEL})
        try:
            r = _run(wrapper, ["repo", "delete", "someorg/somerepo"], {
                "ISTOTA_SKILL_PROXY_SOCK": proxy.path,
            })
        finally:
            proxy.close()
        assert r.returncode == EXIT_DENIED
        assert proxy.requests == []
