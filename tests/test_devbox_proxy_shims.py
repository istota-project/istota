"""Tests for the in-container devbox scripts and the image's static content.

``docker/devbox/scripts/git-credential-istota`` runs inside the devbox
container, talking to a Unix socket bind-mounted from the host. It runs
here as a subprocess against a real istota daemon on a tmpdir socket,
with ``ISTOTA_CRED_SOCK`` and ``ISTOTA_DEVBOX_LIB`` pointed at the test
fixtures.

The curated ``gh`` / ``glab`` shims and the ``github-api`` /
``gitlab-api`` REST wrappers are gone — the container runs the real
binaries behind ``forge_cli.py``, whose own tests are in
``test_forge_cli.py`` and ``test_forge_cli_exec.py``. What remains here
is the credential helper, the build-time policy seeding, and the
image-content checks that keep the Dockerfile's COPY paths honest.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from istota.devbox_proxy import DevboxProxyContext, handle_connection


REPO = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO / "docker" / "devbox" / "scripts"
LIB_DIR = REPO / "docker" / "devbox" / "lib"
DOCKERFILE = REPO / "docker" / "devbox" / "Dockerfile"


# ---- Fixtures --------------------------------------------------------------


@pytest.fixture()
def sock_path():
    """Short Unix-socket path under /tmp (AF_UNIX 104-char limit on macOS)."""
    dirpath = Path(tempfile.mkdtemp(prefix="dvbx_shim_", dir="/tmp"))
    try:
        yield dirpath / "p.sock"
    finally:
        shutil.rmtree(dirpath, ignore_errors=True)


def _ctx(
    *,
    user_id: str = "alice",
    gitlab_token: str = "GL-TOKEN",
    github_token: str = "GH-TOKEN",
    gitlab_url: str = "https://gitlab.com",
    github_url: str = "https://github.com",
) -> DevboxProxyContext:
    return DevboxProxyContext(
        user_id=user_id,
        gitlab_token=gitlab_token,
        github_token=github_token,
        gitlab_url=gitlab_url,
        github_url=github_url,
    )


class FakeDaemon:
    """Asyncio-thread daemon recording requests + returning canned responses.

    Started in its own thread so subprocess tests stay simple — no asyncio
    bridging in the test body.
    """

    def __init__(self, sock_path: Path, ctx: DevboxProxyContext):
        self.sock_path = sock_path
        self.ctx = ctx
        self.thread: object | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server: asyncio.AbstractServer | None = None
        self._ready = None
        self._stop = None

    def start(self):
        import threading

        ready = threading.Event()
        self._ready = ready

        def runner():
            loop = asyncio.new_event_loop()
            self.loop = loop
            asyncio.set_event_loop(loop)
            self._stop = loop.create_future()

            async def cb(reader, writer):
                await handle_connection(reader, writer, self.ctx)

            async def main():
                self.server = await asyncio.start_unix_server(
                    cb, path=str(self.sock_path),
                )
                os.chmod(str(self.sock_path), 0o600)
                ready.set()
                async with self.server:
                    await self._stop

            loop.run_until_complete(main())
            loop.close()

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()
        ready.wait(timeout=5)

    def stop(self):
        if self.loop and self._stop and not self._stop.done():
            self.loop.call_soon_threadsafe(self._stop.set_result, None)
        if self.thread:
            self.thread.join(timeout=5)


@pytest.fixture()
def daemon_factory(sock_path):
    instances: list[FakeDaemon] = []

    def factory(ctx):
        d = FakeDaemon(sock_path, ctx)
        d.start()
        instances.append(d)
        return d

    yield factory
    for d in instances:
        d.stop()


def _run_script(script: str, args: list[str], *, sock_path: Path, stdin: str = "", env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke a shim script as a subprocess pointed at our test socket."""
    env = os.environ.copy()
    env["ISTOTA_CRED_SOCK"] = str(sock_path)
    env["ISTOTA_DEVBOX_LIB"] = str(LIB_DIR)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), *args],
        input=stdin, env=env,
        capture_output=True, text=True,
        timeout=10,
    )


# ---- git-credential-istota -------------------------------------------------


class TestGitCredentialHelper:
    def test_get_github_returns_credentials_on_stdout(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        stdin = "protocol=https\nhost=github.com\n"
        result = _run_script(
            "git-credential-istota", ["get"],
            sock_path=sock_path, stdin=stdin,
        )
        assert result.returncode == 0, result.stderr
        assert "username=x-access-token" in result.stdout
        assert "password=GH-TOKEN" in result.stdout
        assert "host=github.com" in result.stdout

    def test_get_gitlab_returns_credentials(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        stdin = "protocol=https\nhost=gitlab.com\n"
        result = _run_script(
            "git-credential-istota", ["get"],
            sock_path=sock_path, stdin=stdin,
        )
        assert result.returncode == 0
        assert "password=GL-TOKEN" in result.stdout

    def test_get_unknown_host_exits_zero_with_empty_stdout(self, sock_path, daemon_factory):
        """Git treats missing password= lines as 'no credential' — that's
        the right outcome for hosts the daemon doesn't have a token for."""
        daemon_factory(_ctx())
        stdin = "protocol=https\nhost=bitbucket.org\n"
        result = _run_script(
            "git-credential-istota", ["get"],
            sock_path=sock_path, stdin=stdin,
        )
        assert result.returncode == 0
        assert "password=" not in result.stdout

    def test_store_is_quiet_noop(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        stdin = "protocol=https\nhost=github.com\npassword=anything\n"
        result = _run_script(
            "git-credential-istota", ["store"],
            sock_path=sock_path, stdin=stdin,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_erase_is_quiet_noop(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        result = _run_script(
            "git-credential-istota", ["erase"],
            sock_path=sock_path, stdin="",
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_unknown_op_exits_2(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        result = _run_script(
            "git-credential-istota", ["approve"],
            sock_path=sock_path, stdin="",
        )
        assert result.returncode == 2
        assert "unknown" in result.stderr.lower() or "approve" in result.stderr

    def test_proxy_unreachable_exits_1(self, sock_path):
        """Without a daemon running, the helper should fail with a clear
        operator-targeted message."""
        # No daemon — socket file doesn't exist.
        result = _run_script(
            "git-credential-istota", ["get"],
            sock_path=sock_path, stdin="protocol=https\nhost=github.com\n",
        )
        assert result.returncode == 1
        assert "unreachable" in result.stderr.lower()


# ---- The wrapper against the real daemon -----------------------------------


class TestWrapperAgainstRealDaemon:
    """The one place both ends of `forge_token` run for real.

    Everywhere else the daemon's reply and the wrapper's expectation are two
    independently hand-written literals, so a rename on either side passes its
    own tests. Here the vendored wrapper talks to the actual
    `handle_connection` over a socket.
    """

    def _install_wrapper(self, tmp_path, name, real_stub, url):
        import json as _json

        from istota.forge_cli import FORGE_GITHUB, FORGE_GITLAB, build_policy

        bin_dir = tmp_path / "bin"
        bin_dir.mkdir(parents=True, exist_ok=True)
        wrapper = bin_dir / name
        shutil.copy(LIB_DIR / "istota_forge_cli.py", wrapper)
        wrapper.chmod(0o700)

        real = tmp_path / f"real-{name}"
        real.write_text(real_stub)
        real.chmod(0o700)

        cfg = tmp_path / f"{name}-config"
        cfg.mkdir(exist_ok=True)

        # The devbox shape: no `url` in the policy, so the daemon's answer is
        # the only source for it.
        policy = {}
        for forge in (FORGE_GITHUB, FORGE_GITLAB):
            section = build_policy(forge)
            section["real_bin"] = str(real)
            section["config_dir"] = str(cfg)
            policy[forge] = section
        (bin_dir / "forge-policy.json").write_text(_json.dumps(policy))
        return wrapper

    def test_forge_token_round_trips_through_the_real_daemon(
        self, tmp_path, sock_path, daemon_factory,
    ):
        daemon_factory(_ctx(github_url="https://ghe.example.com"))
        stub = (
            "#!/bin/sh\n"
            'echo "GH_TOKEN:${GH_TOKEN-<unset>}"\n'
            'echo "GH_ENTERPRISE_TOKEN:${GH_ENTERPRISE_TOKEN-<unset>}"\n'
            'echo "GH_HOST:${GH_HOST-<unset>}"\n'
        )
        wrapper = self._install_wrapper(tmp_path, "gh", stub, None)
        result = subprocess.run(
            [str(wrapper), "pr", "list"],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "ISTOTA_CRED_SOCK": str(sock_path)},
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        fields = dict(
            line.split(":", 1) for line in result.stdout.splitlines() if ":" in line
        )
        # The token the daemon actually holds, not one the test wrote.
        assert fields["GH_ENTERPRISE_TOKEN"] == "GH-TOKEN"
        # And the URL the daemon actually holds, which is the field that only
        # exists because a shared image cannot bake a per-user one.
        assert fields["GH_HOST"] == "ghe.example.com"

    def test_glab_gets_the_gitlab_url_from_the_daemon(
        self, tmp_path, sock_path, daemon_factory,
    ):
        daemon_factory(_ctx(gitlab_url="https://git.example.com:8443/gitlab"))
        stub = (
            "#!/bin/sh\n"
            'echo "GITLAB_TOKEN:${GITLAB_TOKEN-<unset>}"\n'
            'echo "GITLAB_HOST:${GITLAB_HOST-<unset>}"\n'
        )
        wrapper = self._install_wrapper(tmp_path, "glab", stub, None)
        result = subprocess.run(
            [str(wrapper), "mr", "list"],
            env={"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                 "ISTOTA_CRED_SOCK": str(sock_path)},
            capture_output=True, text=True, timeout=30,
        )
        assert result.returncode == 0, result.stderr
        fields = dict(
            line.split(":", 1) for line in result.stdout.splitlines() if ":" in line
        )
        assert fields["GITLAB_TOKEN"] == "GL-TOKEN"
        # Port and subpath both survive — a hostname-only value loses them.
        assert fields["GITLAB_HOST"] == "https://git.example.com:8443/gitlab"


# ---- Image content smoke checks -------------------------------------------


class TestImageStaticContent:
    """No `docker build` here — that's a CI/integration concern. Instead we
    verify the COPY paths in the Dockerfile point at files that actually
    exist, that the static gitconfig wires the helper correctly, and that
    the retired shims are really gone."""

    def test_remaining_script_paths_exist_and_are_executable(self):
        for name in ("git-credential-istota", "seed-forge-policy"):
            path = SCRIPTS_DIR / name
            assert path.exists(), f"missing script: {path}"
            assert os.access(path, os.X_OK), f"not executable: {path}"

    def test_retired_shims_are_gone(self):
        """The curated gh/glab shims and the REST wrappers were deleted with
        the proxy's API actions. A file reappearing here would shadow the
        wrapper at /usr/local/bin and route around the policy entirely."""
        for name in ("gh", "glab", "github-api", "gitlab-api"):
            assert not (SCRIPTS_DIR / name).exists(), (
                f"docker/devbox/scripts/{name} is back — the container gets "
                f"the real binary behind forge_cli.py, not a shim"
            )

    def test_lib_module_paths_exist(self):
        assert (LIB_DIR / "istota_devbox_client.py").exists()
        assert (LIB_DIR / "istota_forge_cli.py").exists()

    def test_client_lib_no_longer_carries_the_api_wrapper_plumbing(self):
        src = (LIB_DIR / "istota_devbox_client.py").read_text()
        for gone in ("api_wrapper_main", "get_repo_slug", "emit_response"):
            assert gone not in src, f"{gone} should have gone with the shims"
        # What git-credential-istota imports must survive.
        for kept in ("def call(", "def die(", "class ProxyUnreachable"):
            assert kept in src

    def test_gitconfig_wires_credential_helper(self):
        gc = (REPO / "docker" / "devbox" / "etc" / "gitconfig").read_text()
        assert "[credential]" in gc
        assert "helper = istota" in gc
        # Placeholder identity so `git commit` doesn't choke.
        assert "[user]" in gc

    def test_dockerfile_copies_lib_scripts_and_gitconfig(self):
        dockerfile = DOCKERFILE.read_text()
        for line in (
            "COPY lib/istota_devbox_client.py /usr/local/lib/istota_devbox/istota_devbox_client.py",
            "COPY lib/istota_forge_cli.py /usr/local/lib/istota_forge/istota_forge_cli.py",
            "COPY scripts/git-credential-istota /usr/local/bin/git-credential-istota",
            "COPY scripts/seed-forge-policy /usr/local/lib/istota_forge/seed-forge-policy",
            "COPY etc/gitconfig /etc/gitconfig",
        ):
            assert line in dockerfile, f"missing Dockerfile line: {line}"

    def test_dockerfile_installs_the_wrapper_under_all_four_names(self):
        dockerfile = DOCKERFILE.read_text()
        assert "for name in gh glab github-api gitlab-api" in dockerfile, (
            "the retired names carry the one-line explanation; without them a "
            "cached habit gets 'command not found' and reaches for something else"
        )

    def test_dockerfile_keeps_the_real_binaries_off_path(self):
        """The real gh/glab live under /usr/local/lib/istota_forge/. Installing
        the .deb instead would put a second, real gh at /usr/bin/gh."""
        dockerfile = DOCKERFILE.read_text()
        assert "/usr/local/lib/istota_forge/gh" in dockerfile
        assert "/usr/local/lib/istota_forge/glab" in dockerfile
        assert "dpkg-deb --fsys-tarfile" in dockerfile
        assert "dpkg -i" not in dockerfile

    def test_dockerfile_pins_and_verifies_every_download(self):
        """A pinned version with no checksum is a version pin, not a supply
        chain control — the vendor can re-cut a tag.

        Stated as "every download into /tmp is verified" rather than as a count
        of `sha256sum` calls. The count was 2, and it failed the moment the Go
        toolchain gained the verification it had always lacked (ISSUE-280) —
        a passing test going red on a strict improvement, and a number nobody
        would think to raise if a *fourth* download arrived unverified.
        """
        dockerfile = DOCKERFILE.read_text()
        assert "ARG GH_VERSION=" in dockerfile
        assert "ARG GLAB_VERSION=" in dockerfile

        # Fold shell line continuations first: the Go layer puts its `-o` on the
        # next line, so a line-scoped regex sees two downloads where there are
        # three — and would have reported "all verified" while missing one.
        folded = re.sub(r"\\\n\s*", " ", dockerfile)
        # `[^\s;"]+` rather than `\S+`: a shell `;` or a closing quote directly
        # after the path is punctuation, not part of the filename.
        downloads = re.findall(r"curl [^\n]*?-o \"?(/tmp/[^\s;\"]+)", folded)
        assert len(downloads) >= 3, (
            f"expected the go, gh and glab downloads, found {downloads}"
        )
        for target in downloads:
            assert f'{target}" | sha256sum -c -' in folded, (
                f"{target} is downloaded and never checksum-verified"
            )

        # Every pinned digest is a real sha256, whatever it is called. The names
        # carry an architecture suffix now, so matching them exactly is what
        # went stale last time.
        digests = re.findall(r"^ARG\s+(\w*SHA256\w*)=(\S+)", dockerfile, re.M)
        assert len(digests) >= 6, (
            f"expected two digests per artifact for three artifacts, found "
            f"{[name for name, _ in digests]}"
        )
        for name, digest in digests:
            assert len(digest) == 64 and all(c in "0123456789abcdef" for c in digest), (
                f"{name} is not a sha256 hex digest: {digest!r}"
            )

    def test_dockerfile_drops_the_retired_env_vars(self):
        dockerfile = DOCKERFILE.read_text()
        for gone in ("GITLAB_API_CMD", "GITHUB_API_CMD", "GH_PATH=", "GLAB_PATH="):
            assert gone not in dockerfile, f"{gone} should be gone from the image"
        assert "ISTOTA_CRED_SOCK=/run/istota-cred/sock" in dockerfile


# ---- Build-time policy seeding ---------------------------------------------


class TestSeedForgePolicy:
    """The build-time script that generates /etc/istota-forge/policy.json.

    It runs against a tmpdir here rather than the image paths — that is what
    the two argv overrides are for."""

    def _seed(self, tmp_path):
        lib = tmp_path / "lib"
        etc = tmp_path / "etc"
        lib.mkdir()
        etc.mkdir()
        shutil.copy(LIB_DIR / "istota_forge_cli.py", lib / "istota_forge_cli.py")
        result = subprocess.run(
            [sys.executable, str(SCRIPTS_DIR / "seed-forge-policy"),
             "--forge-lib", str(lib), "--etc", str(etc)],
            capture_output=True, text=True, timeout=30,
        )
        return result, lib, etc

    def test_writes_a_policy_the_wrapper_loads(self, tmp_path):
        result, lib, etc = self._seed(tmp_path)
        assert result.returncode == 0, result.stderr

        from istota.forge_cli import FORGE_GITHUB, FORGE_GITLAB, load_policy

        out = str(etc / "policy.json")
        for forge, binary in ((FORGE_GITHUB, "gh"), (FORGE_GITLAB, "glab")):
            loaded = load_policy(out, forge)
            # real_bin is absent from the baseline, so its presence proves the
            # file was read rather than fallen back from.
            assert loaded["real_bin"] == str(lib / binary)
            assert loaded["config_dir"] == str(etc / binary)
            assert loaded["path_rules"]

    def test_seeded_policy_denies_the_baseline(self, tmp_path):
        """The container and the sandbox must not disagree about what is
        denied, which is why this is generated from build_policy rather than
        written out by hand."""
        result, lib, etc = self._seed(tmp_path)
        assert result.returncode == 0, result.stderr

        from istota.forge_cli import FORGE_GITHUB, denied_reason, load_policy

        policy = load_policy(str(etc / "policy.json"), FORGE_GITHUB)
        for args in (["repo", "delete", "o/r"], ["auth", "status"],
                     ["api", "graphql"], ["gist", "create", "-"]):
            assert denied_reason(FORGE_GITHUB, args, policy) is not None, args
        assert denied_reason(FORGE_GITHUB, ["pr", "list"], policy) is None

    def test_container_policy_never_grants_a_direct_token(self, tmp_path):
        """direct_token lets the wrapper read an ambient GITHUB_TOKEN. The
        container always has the credential socket, so an ambient token there
        would mean something upstream failed to strip it."""
        result, lib, etc = self._seed(tmp_path)
        assert result.returncode == 0, result.stderr
        policy = json.loads((etc / "policy.json").read_text())
        for section in policy.values():
            assert section["direct_token"] is False

    def test_no_url_is_baked_in(self, tmp_path):
        """One image serves every user. A baked gitlab.com would make a
        self-hosted deployment look configured while sending its token to the
        wrong host; the per-user URL rides in with the token instead."""
        result, lib, etc = self._seed(tmp_path)
        assert result.returncode == 0, result.stderr
        policy = json.loads((etc / "policy.json").read_text())
        for name, section in policy.items():
            assert "url" not in section, (
                f"{name} baked a URL into a shared image; the per-user value "
                f"arrives with the token from the devbox proxy"
            )
