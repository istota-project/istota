"""The devbox image, built natively on whatever architecture you are on.

It used to be amd64-only — a hardcoded `linux-amd64` Go tarball and two
hardcoded amd64 `.deb`s — so on an arm64 machine this file ran only under
`--platform amd64`, an emulated build in the tens of minutes. The practical
result was that the tests for this image were the least-executed in the repo,
run once before a release if at all. ISSUE-280 derived all three assets from
`dpkg --print-architecture` and pinned a checksum per architecture, so the
build is now a few minutes natively and this module no longer skips itself.

What it asserts is the properties nothing else can see at image level:

  * the two images agree on the forge versions they ship. `tests/
    test_docker_forge_clis.py` already asserts the two Dockerfiles agree
    *textually*; this asserts the two binaries agree, which is the claim the
    textual test is a proxy for.
  * `docker/devbox/lib/istota_forge_cli.py` in the image is byte-identical to
    `src/istota/forge_cli.py` in the repo. That is the property
    `scripts/sync-devbox-lib.sh` exists to maintain, and it is currently checked
    by nothing at image level — a stale copy means the devbox enforces a
    different deny policy than the sandbox does, silently. The same claim now
    covers the second file that script syncs, the exec protocol module.
  * the container's uid. The daemon and this container share a filesystem, so a
    uid mismatch means either the container cannot write into a worktree or the
    daemon cannot reap one — and there is no error message anywhere that says
    "uid". `DEV_UID`/`DEV_GID` default to 1000, and a build with no args
    reproducing that exactly is what lets the deploy pass the daemon's own.
  * the exec transport is installed and *starts*. Reading a `COPY` line tells
    you a file is at a path; it does not tell you the supervisor comes up, the
    server binds a socket, or that anything answers on it.

**Every claim above that could pass without the mechanism has a control in
`scripts/test-image-negative-control.sh`**, and each control names the exact
node ids it must turn red. On a tier asserting against a built artifact,
reading the test tells you almost nothing about whether it can fail. The three
that have no control are positive existence checks on a named absolute path —
a `test -x` against the wrong path, a `python3 -c 'import …'` against a
directory that is not there, and a `Cmd` compared to an exact list all fail
closed, so a mistake in them is red rather than vacuously green.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess

import pytest

from .conftest import REPO, assert_ok, is_emulated, require_docker, sh

pytestmark = pytest.mark.image

FORGE_LIB = "/usr/local/lib/istota_forge"
WRAPPER_IN_IMAGE = f"{FORGE_LIB}/istota_forge_cli.py"
SOURCE_OF_TRUTH = REPO / "src" / "istota" / "forge_cli.py"

EXEC_LIB = "/usr/local/lib/istota_devbox_exec"
EXEC_PROTOCOL_IN_IMAGE = f"{EXEC_LIB}/istota_devbox_exec_protocol.py"
EXEC_PROTOCOL_SOURCE = REPO / "src" / "istota" / "devbox_exec_protocol.py"
EXEC_SERVER = "/usr/local/bin/istota-exec-serve"
EXEC_SUPERVISOR = "/usr/local/bin/istota-exec-run"

# The uid a build with no args produces. Named once, because the whole point of
# the default is that it reproduces the image this recipe built before the args
# existed — an image whose /home/dev volume is full of files owned by 1000.
DEFAULT_DEV_UID = "1000"
DEFAULT_DEV_GID = "1000"

# A uid that belongs to nothing in the image, used to stand in for a volume
# left behind by a build with different args.
STRANGER_UID = "4242"


@pytest.fixture(scope="module")
def devbox_image_under_test(devbox_image):
    """The devbox image, built for whatever architecture the session targets.

    This used to be `amd64_devbox_image`, a gate that skipped the whole module
    unless the session produced an amd64 image, with `getfixturevalue` deferring
    the build until after the skip so a native arm64 run never paid for an
    emulated one. Both are gone with ISSUE-280: the recipe builds natively on
    either architecture, so there is nothing left to gate on and no reason to
    defer. A plain fixture parameter is enough.

    Deliberately *not* reintroducing a skip: a tier that skips itself is how
    this file went unexecuted for months, and it is the failure the whole
    deployment-artifact-verification spec exists to end. If the build cannot
    happen the session-scoped fixture in conftest says so on its own terms.
    """
    return devbox_image


def _dockerfile_arg(dockerfile, name: str) -> str:
    body = dockerfile.read_text()
    match = re.search(rf"^ARG\s+{name}=(\S+)", body, re.M)
    assert match, f"{name} is not pinned in {dockerfile}"
    return match.group(1)


class TestTheForgeBinariesMatchTheMainImage:
    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_the_binary_is_present_and_runs(self, devbox_image_under_test, binary):
        assert_ok(sh(devbox_image_under_test, f"{FORGE_LIB}/{binary} --version"), binary)

    @pytest.mark.parametrize(
        "binary,arg", [("gh", "GH_VERSION"), ("glab", "GLAB_VERSION")]
    )
    def test_the_installed_version_matches_this_images_pin(
        self, devbox_image_under_test, binary, arg
    ):
        pinned = _dockerfile_arg(devbox_image_under_test.dockerfile, arg)
        out = assert_ok(sh(devbox_image_under_test, f"{FORGE_LIB}/{binary} --version"), binary)

        assert pinned in out, f"expected {pinned} in {out!r}"

    @pytest.mark.parametrize(
        "binary,arg", [("gh", "GH_VERSION"), ("glab", "GLAB_VERSION")]
    )
    def test_the_two_images_ship_the_same_version(self, devbox_image_under_test, binary, arg):
        # A drift here means a task behaves differently depending on which
        # container it lands in, which is the hardest kind of bug to reproduce.
        main = _dockerfile_arg(REPO / "docker" / "istota" / "Dockerfile", arg)
        devbox = _dockerfile_arg(devbox_image_under_test.dockerfile, arg)

        assert main == devbox, (
            f"{binary}: the main image pins {main} and the devbox image pins "
            f"{devbox}. scripts/sync-devbox-lib.sh does not cover the ARGs."
        )
        assert main in assert_ok(sh(devbox_image_under_test, f"{FORGE_LIB}/{binary} --version"), binary)


class TestTheWrapperCopyIsInSync:
    def test_the_image_copy_is_byte_identical_to_the_source(self, devbox_image_under_test):
        # The devbox build context is docker/devbox/, so it cannot COPY from
        # src/ — the copy exists for that reason alone, and a copy with no check
        # is a copy that drifts.
        expected = hashlib.sha256(SOURCE_OF_TRUTH.read_bytes()).hexdigest()
        result = sh(devbox_image_under_test, f"sha256sum {WRAPPER_IN_IMAGE}")
        actual = assert_ok(result, f"sha256sum {WRAPPER_IN_IMAGE}").split()[0]

        assert actual == expected, (
            "docker/devbox/lib/istota_forge_cli.py has drifted from "
            "src/istota/forge_cli.py; run scripts/sync-devbox-lib.sh"
        )

    # The "is the *repo* copy in sync" half deliberately lives elsewhere:
    # `tests/test_forge_cli.py` already asserts
    # src/istota/forge_cli.py == docker/devbox/lib/istota_forge_cli.py, in the
    # default suite, with no Docker at all. A copy of it here would sit behind
    # the `image` marker and a Docker daemon, so it would run far less often
    # and could only fail in a state that cheaper test had already caught.
    # (This used to say "and an amd64 build", which was the stronger half of
    # the argument until ISSUE-280 made the image build natively. The
    # conclusion is unchanged; the reason is now just the marker.)


class TestTheWrapperIsWhatResolvesByName:
    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_the_name_resolves_to_the_wrapper(self, devbox_image_under_test, binary):
        result = sh(devbox_image_under_test, f"command -v {binary}")
        resolved = assert_ok(result, f"command -v {binary}").strip()

        assert resolved, f"{binary} does not resolve by name at all"
        assert not resolved.startswith(FORGE_LIB), (
            f"{binary} resolves straight to the real binary at {resolved}; "
            "the deny policy and the token injection are both bypassed"
        )

    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_what_resolves_is_the_python_wrapper_not_a_real_binary(
        self, devbox_image_under_test, binary
    ):
        result = sh(devbox_image_under_test, f"head -c 200 \"$(command -v {binary})\"")
        head = assert_ok(result, f"reading the {binary} on PATH")

        assert "python" in head.lower(), (
            f"the {binary} on PATH is not the python wrapper:\n{head!r}"
        )

    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_the_real_binary_is_off_path(self, devbox_image_under_test, binary):
        # The positive half is the test above; without it this passes on an
        # image that installs nothing.
        result = sh(
            devbox_image_under_test,
            f"test -x {FORGE_LIB}/{binary} && command -v {binary}",
        )
        resolved = assert_ok(result, f"{binary}").strip()

        assert resolved != f"{FORGE_LIB}/{binary}"


# ---------------------------------------------------------------------------
# The exec transport: the uid it runs as, the files that carry it, and whether
# it comes up.


def _inspect(image, template: str) -> str:
    """One `docker image inspect` field of the built image.

    Not `run_in`: `Cmd` is image metadata rather than something a container can
    be asked about, and the compose file that overrides it in the deployment is
    a different file with a test of its own.
    """
    require_docker()
    result = subprocess.run(
        ["docker", "image", "inspect", "--format", template, image.tag],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"docker image inspect {template} failed (exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return result.stdout.strip()


# Speak the protocol rather than test for a file, and take the server's pid
# from its own `stat` reply.
#
# Both halves of that were found the expensive way. A socket inode proves an
# inode: the server does not unlink on SIGKILL, so after a kill the path is
# still there and a `test -S` poll returns instantly against a dead stub — the
# same shape as the readiness probe that scanned `/proc/*/cmdline` for a string
# its own command line contained. And the first version of the restart test
# read the pid from `pgrep -f istota-exec-serve`, which matched the pid of the
# `sh -c` running this very script, because the script's own text contains that
# string. It reported `FIRST_PID 1` and killed nothing.
#
# So the wait is a connect-and-ask loop, and identity comes off the wire. That
# also makes the probe prove far more than liveness: the server parsed a
# request, answered with a protocol version, and reported the uid it is running
# as, which is the claim the whole uid design rests on.
_WIRE_PROBE = r"""
probe_wire() {
    if ISTOTA_PROBE_SOCKET="$1" ISTOTA_PROBE_NOT_PID="${3:-}" \
        ISTOTA_PROBE_BUDGET_SECONDS="$PROBE_BUDGET_SECONDS" python3 - <<'PY'
import json
import os
import socket
import sys
import time

sys.path.insert(0, "/usr/local/lib/istota_devbox_exec")
import istota_devbox_exec_protocol as protocol

path = os.environ["ISTOTA_PROBE_SOCKET"]
not_pid = os.environ.get("ISTOTA_PROBE_NOT_PID") or None
# Scaled by the caller, for the same reason `run_in` scales its own: a
# qemu-emulated container is several times slower, and a fixed budget here
# would fail the release run's `--platform amd64` against a working image.
budget = float(os.environ["ISTOTA_PROBE_BUDGET_SECONDS"])


def ask():
    sock = socket.socket(socket.AF_UNIX)
    sock.settimeout(budget)
    try:
        sock.connect(path)
        sock.sendall(protocol.encode_stat_request())

        buffered = b""
        while b"\n" not in buffered:
            chunk = sock.recv(65536)
            if not chunk:
                raise OSError("the server closed before acknowledging")
            buffered += chunk
        line, rest = buffered.split(b"\n", 1)

        decoder = protocol.FrameDecoder()
        frames = []
        pending = rest
        while len(frames) < 2:
            for _stream, payload in decoder.feed(pending):
                frames.append(protocol.decode_control(payload))
            if len(frames) >= 2:
                break
            pending = sock.recv(65536)
            if not pending:
                raise OSError(f"the server closed after {len(frames)} frames")
        return {
            "ack": protocol.decode_ack(line),
            "stat": frames[0],
            "terminal": frames[1],
        }
    finally:
        sock.close()


deadline = time.monotonic() + budget
last = "never attempted"
while time.monotonic() < deadline:
    try:
        answer = ask()
    except OSError as exc:
        last = f"{type(exc).__name__}: {exc}"
    else:
        if not_pid and str(answer["stat"]["pid"]) == not_pid:
            last = f"still the old server, pid {not_pid}"
        else:
            print("PROBE " + json.dumps(answer))
            break
    time.sleep(0.2)
else:
    raise SystemExit(f"nothing answered on {path} within {budget}s ({last})")
PY
    then
        return 0
    fi
    echo "--- supervisor log ($2) ---" >&2
    cat "$2" >&2 || true
    return 1
}
"""

def _script(image, body: str) -> str:
    """The prelude plus a body, with the placeholders filled in.

    A plain replace rather than `str.format` or `%`: the bodies are shell, and
    both of those would make every `${...}` and `%u` in them an escaping
    problem.

    `set -eu` on every one of them, so a setup step that failed takes the
    script down instead of leaving a later assertion to compare two empty
    strings and agree with itself.
    """
    budget = 240 if is_emulated(image) else 30
    prelude = f"set -eu\nPROBE_BUDGET_SECONDS={budget}\n" + _WIRE_PROBE
    return prelude + body.replace("SUPERVISOR", EXEC_SUPERVISOR).replace(
        "STRANGER", STRANGER_UID
    )


def _probe_payload(stdout: str) -> dict:
    for raw in stdout.splitlines():
        if raw.startswith("PROBE "):
            return json.loads(raw[len("PROBE "):])
    raise AssertionError(f"the wire probe printed no PROBE line:\n{stdout}")


def _field(stdout: str, key: str) -> str:
    for raw in stdout.splitlines():
        if raw.startswith(f"{key} "):
            return raw[len(key) + 1:].strip()
    raise AssertionError(f"no {key} line in:\n{stdout}")


class TestTheDevUidBuildArgs:
    """The uid is the invariant of the shared mount, so it is asserted here.

    Not a Dockerfile grep. `ARG DEV_UID=1000` says what the recipe intends;
    `id -u dev` in the built image says what a build with no args produced, and
    the second is the claim — the deploy passes the daemon's own uid, and the
    default exists so a build without one reproduces the image whose /home/dev
    volumes are already full of files owned by 1000.
    """

    def test_the_dev_account_has_the_default_uid_and_gid(self, devbox_image_under_test):
        result = sh(devbox_image_under_test, "id -u dev; id -g dev")
        uid, gid = assert_ok(result, "id dev").split()

        assert (uid, gid) == (DEFAULT_DEV_UID, DEFAULT_DEV_GID), (
            f"a build with no DEV_UID/DEV_GID gave dev {uid}:{gid}, not "
            f"{DEFAULT_DEV_UID}:{DEFAULT_DEV_GID}. Every /home/dev volume in "
            "the estate was written by an image where it was 1000."
        )

    def test_the_home_directory_belongs_to_the_dev_account(self, devbox_image_under_test):
        # The half the supervisor's chown cannot help with: what the *image*
        # ships. A volume masks this at runtime and the chown repairs that; an
        # image whose own /home/dev belongs to somebody else is broken before
        # any volume is involved — `dev` cannot write the directory every
        # `exec` starts in.
        #
        # Both sides are the image's own self-report, which is the shape of an
        # assertion that agrees with itself on any image at all — on one with no
        # `dev` account and no `/home/dev`, both substitutions come back empty
        # and the comparison holds. `set -eu` from `_script` takes the script
        # down first, and the non-empty check below is the belt to that brace.
        result = sh(
            devbox_image_under_test,
            _script(
                devbox_image_under_test,
                'echo "OWNER $(stat -c \'%u %g\' /home/dev)"\n'
                'echo "ACCOUNT $(id -u dev) $(id -g dev)"\n',
            ),
        )
        out = assert_ok(result, "stat /home/dev")

        assert len(_field(out, "OWNER").split()) == 2, _field(out, "OWNER")
        assert len(_field(out, "ACCOUNT").split()) == 2, _field(out, "ACCOUNT")
        assert _field(out, "OWNER") == _field(out, "ACCOUNT"), (
            f"/home/dev is owned by {_field(out, 'OWNER')} and dev is "
            f"{_field(out, 'ACCOUNT')}; dev cannot write its own home"
        )


class TestTheToolchainSurvivesTheHomeVolume:
    """ISSUE-336 — uv is in the image, and not in the directory a volume masks.

    It used to be installed as `dev` into `/home/dev/.local/bin`. Docker seeds
    a named volume from the image only when the volume is empty at
    container-create time, so that survived a first boot and nothing after it:
    `devbox reset` empties the volume and restarts the container, a restart
    copies nothing, and uv was gone for good. `uv` is shimmed, so what that
    actually broke was `uv sync` on a repository task, inside a container the
    reader did not know they were in.

    `tests/test_devbox_deployment_shape.py` asserts the recipe's shape; this
    asserts the built artifact, which is the claim. No negative control: both
    halves fail closed — an image with no uv fails `command -v`, and the path
    it prints is compared against a literal it must not be under.
    """

    def test_uv_and_uvx_run_from_outside_the_home_directory(
        self, devbox_image_under_test
    ):
        result = sh(
            devbox_image_under_test,
            'echo "UV $(command -v uv)"; echo "UVX $(command -v uvx)"; '
            'uv --version > /dev/null; uvx --version > /dev/null',
        )
        out = assert_ok(result, "uv/uvx are not installed and runnable")

        for key in ("UV", "UVX"):
            where = _field(out, key)
            assert where, f"{key} resolved to nothing"
            assert not where.startswith("/home/dev"), (
                f"{key.lower()} is at {where}, inside the directory the "
                f"per-user volume mounts over — a `devbox reset` deletes it "
                f"and nothing puts it back (ISSUE-336)"
            )


class TestTheExecTransportIsInstalled:
    def test_the_exec_server_is_installed_and_executable(self, devbox_image_under_test):
        assert_ok(
            sh(devbox_image_under_test, f"test -x {EXEC_SERVER}"),
            f"{EXEC_SERVER} is not an executable file in the image",
        )

    def test_the_supervisor_is_installed_and_executable(self, devbox_image_under_test):
        assert_ok(
            sh(devbox_image_under_test, f"test -x {EXEC_SUPERVISOR}"),
            f"{EXEC_SUPERVISOR} is not an executable file in the image",
        )

    def test_the_image_command_is_the_supervisor(self, devbox_image_under_test):
        # It used to be ["sleep", "infinity"], which neither restarts the
        # server nor reaps what reparents to it. The per-user service overrides
        # `command` with the same value; setting it in the image is what makes
        # a plain `docker run` of this image the thing the deployment runs.
        raw = _inspect(devbox_image_under_test, "{{json .Config.Cmd}}")

        assert json.loads(raw) == [EXEC_SUPERVISOR], (
            f"the image CMD is {raw}, not [{EXEC_SUPERVISOR!r}]"
        )

    def test_the_protocol_module_imports_where_the_server_looks_for_it(
        self, devbox_image_under_test
    ):
        # The server's module search names this directory rather than reading a
        # path out of its environment, so the directory is part of the
        # interface. A COPY to a different one is a server that cannot start.
        result = sh(
            devbox_image_under_test,
            f"python3 -c \"import sys; sys.path.insert(0, '{EXEC_LIB}'); "
            'import istota_devbox_exec_protocol as p; print(p.PROTOCOL_VERSION)"',
        )

        assert assert_ok(result, "importing the vendored protocol module").strip() == "1"

    def test_the_vendored_protocol_copy_is_byte_identical_to_the_source(
        self, devbox_image_under_test
    ):
        # The second file `scripts/sync-devbox-lib.sh` syncs, and the same
        # argument as the forge wrapper's: the build context is docker/devbox/,
        # so it cannot COPY from src/, and a copy with no check is a copy that
        # drifts. A drift here is a container and a daemon disagreeing about
        # the wire format, which surfaces as a `bad_request` nobody can place.
        expected = hashlib.sha256(EXEC_PROTOCOL_SOURCE.read_bytes()).hexdigest()
        result = sh(devbox_image_under_test, f"sha256sum {EXEC_PROTOCOL_IN_IMAGE}")
        actual = assert_ok(result, f"sha256sum {EXEC_PROTOCOL_IN_IMAGE}").split()[0]

        assert actual == expected, (
            "docker/devbox/lib/istota_devbox_exec_protocol.py has drifted from "
            "src/istota/devbox_exec_protocol.py; run scripts/sync-devbox-lib.sh"
        )


class TestTheSupervisorStartsTheTransport:
    """The assertions that need the supervisor to actually run.

    Everything above is a claim about a path. These are claims about a running
    process, and they are the ones where an assertion can pass without the
    mechanism — which is why each names a control in
    `scripts/test-image-negative-control.sh`.
    """

    def test_the_supervisor_brings_the_transport_up(self, devbox_image_under_test):
        result = sh(
            devbox_image_under_test,
            _script(
                devbox_image_under_test,
                """
mkdir -p /tmp/exec-dir /tmp/repos-root
ISTOTA_EXEC_SOCKET=/tmp/exec-dir/exec.sock \
ISTOTA_EXEC_REPOS_ROOT=/tmp/repos-root \
    SUPERVISOR > /tmp/supervisor.log 2>&1 &
echo "ACCOUNT $(id -u dev) $(id -g dev)"
probe_wire /tmp/exec-dir/exec.sock /tmp/supervisor.log
"""
            ),
        )
        out = assert_ok(result, "starting the supervisor")
        payload = _probe_payload(out)

        assert payload["ack"] == {"status": "ok", "protocol": 1}, payload["ack"]
        assert payload["stat"]["protocol"] == 1
        assert payload["stat"]["repos_root"] == "/tmp/repos-root"
        assert payload["stat"]["home"] == "/home/dev"
        assert payload["terminal"]["exit_code"] == 0

        # The server runs as `dev`, not as root and not as whoever started the
        # container. That is the whole uid design, observed from inside the
        # process that will run every build.
        uid, gid = _field(out, "ACCOUNT").split()
        assert [payload["stat"]["uid"], payload["stat"]["gid"]] == [int(uid), int(gid)], (
            f"the server answers as {payload['stat']['uid']}:{payload['stat']['gid']} "
            f"and dev is {uid}:{gid}"
        )

    def test_the_supervisor_restarts_the_server_after_it_dies(self, devbox_image_under_test):
        # The reason the container's command is a supervisor at all. Nothing
        # else restarts the server after a container OOM picks it off, and
        # `restart: unless-stopped` is about the container.
        #
        # The second probe is not decoration: a new pid proves something was
        # spawned, and only a reply proves it is serving.
        result = sh(
            devbox_image_under_test,
            _script(
                devbox_image_under_test,
                """
mkdir -p /tmp/exec-dir /tmp/repos-root
ISTOTA_EXEC_SOCKET=/tmp/exec-dir/exec.sock \
ISTOTA_EXEC_REPOS_ROOT=/tmp/repos-root \
ISTOTA_EXEC_RESPAWN_PAUSE_SECONDS=1 \
    SUPERVISOR > /tmp/supervisor.log 2>&1 &

probe_wire /tmp/exec-dir/exec.sock /tmp/supervisor.log > /tmp/first.probe
first="$(python3 -c "import json,sys; print(json.loads(open('/tmp/first.probe').read().split(' ', 1)[1])['stat']['pid'])")"
echo "FIRST_PID $first"
kill -9 "$first"

probe_wire /tmp/exec-dir/exec.sock /tmp/supervisor.log "$first"
"""
            ),
        )
        out = assert_ok(result, "killing the server under the supervisor")
        payload = _probe_payload(out)

        first = int(_field(out, "FIRST_PID"))
        assert payload["ack"]["status"] == "ok"
        assert payload["stat"]["pid"] != first, (
            f"the socket answered from pid {first} again, which is the process "
            "that was killed — nothing respawned"
        )

    def test_the_supervisor_repairs_a_home_directory_with_the_wrong_owner(
        self, devbox_image_under_test
    ):
        # A pre-existing /home/dev volume was written by an image where dev was
        # 1000. Mounted into a container whose dev is the daemon's uid, it is a
        # home the user cannot write. The repair is a chown guarded by a stat
        # of the directory itself, so the walk that fixes the volume is what
        # stops the next boot from walking it again.
        #
        # The pre-state is asserted in the same script, because a setup step
        # that silently did nothing would leave this passing on an image with
        # no repair in it at all.
        result = sh(
            devbox_image_under_test,
            _script(
                devbox_image_under_test,
                """
mkdir -p /tmp/exec-dir /tmp/repos-root
sudo -n chown -R STRANGER:STRANGER /home/dev
echo "BEFORE $(stat -c '%u %g' /home/dev)"

ISTOTA_EXEC_SOCKET=/tmp/exec-dir/first.sock \
ISTOTA_EXEC_REPOS_ROOT=/tmp/repos-root \
    SUPERVISOR > /tmp/first.log 2>&1 &
first_supervisor=$!
probe_wire /tmp/exec-dir/first.sock /tmp/first.log > /dev/null
echo "AFTER $(stat -c '%u %g' /home/dev)"
echo "NESTED $(stat -c '%u %g' /home/dev/.bashrc)"
echo "ACCOUNT $(id -u dev) $(id -g dev)"

# By pid, not by `pkill -f`: this script's own command line contains both
# names, so a pattern kill would take the shell running it.
kill -9 "$first_supervisor" || true
sleep 2

ISTOTA_EXEC_SOCKET=/tmp/exec-dir/second.sock \
ISTOTA_EXEC_REPOS_ROOT=/tmp/repos-root \
    SUPERVISOR > /tmp/second.log 2>&1 &
probe_wire /tmp/exec-dir/second.sock /tmp/second.log > /dev/null
echo "FIRST_CHOWN_LINES $(grep -c 'chown /home/dev' /tmp/first.log || true)"
echo "FIRST_FAILED_LINES $(grep -c 'chown /home/dev: FAILED' /tmp/first.log || true)"
echo "SECOND_CHOWN_LINES $(grep -c 'chown /home/dev' /tmp/second.log || true)"
"""
            ),
        )
        out = assert_ok(result, "the /home/dev repair")

        assert _field(out, "BEFORE") == f"{STRANGER_UID} {STRANGER_UID}", (
            "the setup chown did not take, so this test proves nothing"
        )
        assert _field(out, "AFTER") == _field(out, "ACCOUNT"), (
            f"/home/dev is still owned by {_field(out, 'AFTER')} after the "
            f"supervisor ran; dev is {_field(out, 'ACCOUNT')}"
        )
        assert _field(out, "NESTED") == _field(out, "ACCOUNT"), (
            "the top directory was repaired but its contents were not, so the "
            "guard reports the volume fixed and never walks it again"
        )
        assert int(_field(out, "FIRST_CHOWN_LINES")) > 0, (
            "nothing was logged about the repair; an operator cannot see it happen"
        )
        # Without this, the two assertions above are equally satisfied by a
        # repair that failed part-way: `chown -R` sets the top directory even
        # when it errors on a descendant, and the guard would then read the
        # repaired top directory on the next boot and never retry. The
        # supervisor puts the top directory back on a failure for that reason,
        # and this is what notices if it stops.
        assert _field(out, "FIRST_FAILED_LINES") == "0", (
            "the repair reported a failure; the volume is half-chowned and the "
            "guard is what decides whether the next start tries again"
        )
        assert _field(out, "SECOND_CHOWN_LINES") == "0", (
            "the second start walked /home/dev again — the stat guard is not "
            "holding, and every boot pays a recursive chown of the whole volume"
        )


    def test_a_stop_signal_lets_the_server_shut_down(self, devbox_image_under_test):
        # Without a trap the server's whole shutdown path is unreachable, and
        # the failure differs by shape: under `init: true` tini signals its
        # direct child only, so an untrapped `sh` dies and the server goes with
        # the pid namespace; as PID 1 the kernel delivers no default-disposition
        # signal at all, so `docker stop` waits out its grace and SIGKILLs. Both
        # skip the drain, the per-connection reap and the socket unlink.
        #
        # `stopped` is the server's own last log line, written after
        # `wait_closed` and the unlink. It is the one thing only a graceful
        # shutdown produces.
        result = sh(
            devbox_image_under_test,
            _script(
                devbox_image_under_test,
                """
mkdir -p /tmp/exec-dir /tmp/repos-root
ISTOTA_EXEC_SOCKET=/tmp/exec-dir/exec.sock \
ISTOTA_EXEC_REPOS_ROOT=/tmp/repos-root \
    SUPERVISOR > /tmp/supervisor.log 2>&1 &
supervisor=$!
probe_wire /tmp/exec-dir/exec.sock /tmp/supervisor.log > /dev/null

kill -TERM "$supervisor"
i=0
while kill -0 "$supervisor" 2>/dev/null; do
    i=$((i + 1))
    if [ "$i" -gt 150 ]; then
        echo "the supervisor was still running 30s after SIGTERM" >&2
        cat /tmp/supervisor.log >&2 || true
        exit 1
    fi
    sleep 0.2
done
echo "STOPPED_LINES $(grep -c 'istota-exec-serve INFO stopped' /tmp/supervisor.log || true)"
echo "RESPAWNED_LINES $(grep -c 'restarting in' /tmp/supervisor.log || true)"
echo "SOCKET_LEFT $(test -e /tmp/exec-dir/exec.sock && echo yes || echo no)"
"""
            ),
        )
        out = assert_ok(result, "stopping the supervisor")

        assert _field(out, "STOPPED_LINES") != "0", (
            "the server never reached its own shutdown path, so the drain, the "
            "per-connection reap and the socket unlink were all skipped"
        )
        assert _field(out, "RESPAWNED_LINES") == "0", (
            "the supervisor respawned the server during a deliberate stop"
        )
        assert _field(out, "SOCKET_LEFT") == "no", (
            "the socket inode outlived the server, so the next client to reach "
            "it connects to nothing and cannot tell that from a dead container"
        )

    def test_a_supervisor_with_no_settings_holds_rather_than_exiting(
        self, devbox_image_under_test
    ):
        # The branch that keeps a misconfiguration from becoming a crash loop.
        # Exiting would take the container down under `restart: unless-stopped`,
        # which also removes the way in to diagnose it — so the transport is
        # down, the container is up, and `doctor`'s transport check is what
        # reports it. The log has to name both variables, because that message
        # is the only thing an operator has to go on.
        #
        # No control: an assertion that the process is alive and that a log line
        # contains two literal names fails closed.
        result = sh(
            devbox_image_under_test,
            _script(
                devbox_image_under_test,
                """
ISTOTA_EXEC_UNCONFIGURED_PAUSE_SECONDS=1 SUPERVISOR > /tmp/supervisor.log 2>&1 &
supervisor=$!
sleep 4
echo "ALIVE $(kill -0 "$supervisor" 2>/dev/null && echo yes || echo no)"
echo "NAMED_SOCKET $(grep -c 'ISTOTA_EXEC_SOCKET' /tmp/supervisor.log || true)"
echo "NAMED_REPOS $(grep -c 'ISTOTA_EXEC_REPOS_ROOT' /tmp/supervisor.log || true)"
kill -TERM "$supervisor" 2>/dev/null || true
"""
            ),
        )
        out = assert_ok(result, "the unconfigured supervisor")

        assert _field(out, "ALIVE") == "yes", (
            "the supervisor exited on a missing setting; under "
            "`restart: unless-stopped` that is a crash loop"
        )
        assert _field(out, "NAMED_SOCKET") != "0"
        assert _field(out, "NAMED_REPOS") != "0"


class TestTheWorkspaceTmpfsIsGone:
    def test_the_image_has_no_workspace_directory(self, devbox_image_under_test):
        # /workspace was a 1 GiB tmpfs charged against the container's memory
        # limit, and it caused ISSUE-306 and ISSUE-312 — `docker cp` cannot
        # traverse a tmpfs mount, so a file copied there arrived nowhere.
        # Nothing needs it once the work root is a bind mount of the repos tree
        # and scratch is /home/dev. The mount comes out of the Ansible service
        # definition; this is the image's half, and it is what stops the path
        # coming back here.
        result = sh(devbox_image_under_test, "test ! -e /workspace")

        assert result.returncode == 0, (
            "/workspace exists in the devbox image. It is deleted: the tmpfs is "
            "gone from the service definition and nothing should recreate the "
            f"path.\n--- stdout ---\n{result.stdout}\n"
            f"--- stderr ---\n{result.stderr}"
        )
