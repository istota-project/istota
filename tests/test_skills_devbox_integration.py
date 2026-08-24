"""Devbox skill against a real container, over the real exec transport.

Every other devbox test drives the protocol against a server in a tmpdir on the
host. That proves the skill speaks the wire; it does not prove the wire reaches
a *container*. This file is the tier that does, and the assertions that make it
worth running are the ones only a container can satisfy: a hostname that is not
the host's, and a path that exists in the image and nowhere else.

Prerequisites: `ISTOTA_USER_ID`, a config naming an `exec_socket_dir`, and a
running devbox whose exec server is answering. Every test skips without them, so
the file is inert on a laptop and meaningful on the deployment:

    ISTOTA_USER_ID=<user> uv run pytest -m integration tests/test_skills_devbox_integration.py -n0

**What changed, and why the old gate is gone.** This file used to spend a third
of its length probing for a Docker-API allowlist refusal and skipping on it: the
socket a sandboxed task saw was `istota-docker-proxy`, which tracked the exec
ids it issued and denied a raw `docker exec` at the exec-inspect step — *after*
the command had run — so an envelope reading `ok` / `1` said nothing about the
container (ISSUE-313). That proxy is retired and the skill no longer runs
`docker` for anything but `reset`. The status is now in the protocol and comes
from `waitpid`, so there is no refusal to mistake for an answer and nothing to
probe for. The gate is simply whether the transport answers a `ping`.
"""

import os
import shutil
import uuid

import pytest

from istota import devbox_exec_protocol as proto
from istota.skills import devbox

pytestmark = pytest.mark.integration


def _args(**kw):
    return type("A", (), kw)()


def _transport_error() -> str | None:
    """Why the transport is unreachable, or None when it answers."""
    try:
        devbox._converse(proto.encode_ping_request())
    except devbox._Refused as e:
        return e.message
    except Exception as e:  # noqa: BLE001 — any failure is a reason to skip
        return f"{type(e).__name__}: {e}"
    return None


@pytest.fixture(scope="module", autouse=True)
def transport() -> None:
    """The reachability gate for the whole file.

    Autouse on purpose: a guard a new test can forget to request is a guard
    nothing has. Every test here goes through it whether or not it says so.
    """
    if not os.environ.get("ISTOTA_USER_ID", "").strip():
        pytest.skip("ISTOTA_USER_ID is unset, so there is no per-user devbox")
    reason = _transport_error()
    if reason:
        pytest.skip(f"the devbox exec transport did not answer a ping: {reason}")


@pytest.fixture
def allowlisted_dir(tmp_path, monkeypatch):
    """cp-in / cp-out refuse a host path outside the skill's allowed roots."""
    monkeypatch.setenv("ISTOTA_DEFERRED_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def remote_scratch():
    """A per-run directory on the persistent volume, removed afterwards.

    Made and removed over the transport, which is the only channel this file
    has left — and the right one, because a directory the transport cannot
    create is a directory the verbs under test could not have used either.
    """
    path = f"/home/dev/.istota-it-{uuid.uuid4().hex[:8]}"
    made = _exec(f"mkdir -p {path}")
    if made["exit_code"] != 0:
        pytest.fail(f"could not create {path} in the devbox: {made['stderr']}")
    yield path
    _exec(f"rm -rf {path}")


def _exec(command: str, timeout: int = 60) -> dict:
    return devbox.cmd_exec(_args(command=command, timeout=timeout))


def _exec_file(path, interpreter: str | None = None, timeout: int = 60) -> dict:
    return devbox.cmd_exec_file(
        _args(path=str(path), interpreter=interpreter, timeout=timeout),
    )


def _cp_in(src, dest: str) -> dict:
    return devbox.cmd_cp_in(_args(src=str(src), dest=dest))


def _cp_out(src: str, dest) -> dict:
    return devbox.cmd_cp_out(_args(src=src, dest=str(dest)))


class TestItIsActuallyTheContainer:
    """The assertions a client that ran the command on the host cannot pass.

    "A file written into the shared path appears on the other side" is path
    identity, which is the design — so it passes byte for byte against a host
    that never had a container. That is the shape of the three sandbox
    scenarios which stayed green through the whole period every task ran
    unconfined. These name something only the image can produce.
    """

    def test_the_hostname_differs_from_this_one(self):
        result = _exec("cat /etc/hostname")
        assert result["exit_code"] == 0, result
        assert result["stdout"].strip(), result
        assert result["stdout"].strip() != os.uname().nodename, (
            "the command reported this machine's hostname, so it ran here "
            "rather than in the container"
        )

    def test_stat_reports_the_container_side_view(self):
        """`stat` is answered by the server, so its facts are the container's.

        The hostname is the discriminating one — the roots would be identical
        on a host that happened to be configured the same way, which is the
        design (Design 15 wants one spelling on both sides).
        """
        info = devbox.cmd_status(_args())
        assert info["status"] == "ok", info
        transport = info["transport"]
        assert transport["reachable"] is True, transport
        assert transport["home"] == "/home/dev", transport
        assert transport["hostname"] != os.uname().nodename, transport

    def test_a_path_that_exists_only_in_the_image_resolves(self):
        """The exec server itself: installed by the image, absent on a host
        that merely has the repository checked out."""
        result = _exec("test -x /usr/local/bin/istota-exec-serve")
        assert result["exit_code"] == 0, result
        assert not os.path.exists("/usr/local/bin/istota-exec-serve"), (
            "this host has the server installed too, so the assertion above "
            "cannot distinguish the container — pick another image-only path"
        )


class TestCopyRoundTrip:
    def test_a_file_copied_in_is_visible_inside_the_container(
        self, allowlisted_dir, remote_scratch,
    ):
        src = allowlisted_dir / "probe.txt"
        src.write_text("round-trip marker\n")
        dest = f"{remote_scratch}/probe.txt"

        result = _cp_in(src, dest)
        assert result["status"] == "ok", result

        # The whole point: ask the container, not the daemon.
        seen = _exec(f"cat {dest}")
        assert seen["exit_code"] == 0, seen
        assert seen["stdout"] == "round-trip marker\n"

    def test_a_file_written_inside_the_container_copies_out(
        self, allowlisted_dir, remote_scratch,
    ):
        remote = f"{remote_scratch}/out.txt"
        written = _exec(f"printf 'from inside\\n' > {remote}")
        assert written["exit_code"] == 0, written

        dest = allowlisted_dir / "out.txt"
        result = _cp_out(remote, dest)
        assert result["status"] == "ok", result
        assert dest.read_text() == "from inside\n"

    def test_a_destination_outside_the_servers_roots_is_refused(
        self, allowlisted_dir,
    ):
        """The refusal is the *server's*, decided inside the container.

        This used to be a daemon-side list of container paths — a guess about
        the container's mount table, which is what ISSUE-306 and ISSUE-312 both
        were. `/etc` exists in both namespaces and means different things,
        which is exactly the class the root test closes.
        """
        src = allowlisted_dir / "probe.txt"
        src.write_text("hello\n")

        result = _cp_in(src, "/etc/probe.txt")

        assert result["status"] == "error", result
        assert result.get("code") == proto.ERR_PATH_REFUSED, result
        assert _exec("test -e /etc/probe.txt")["exit_code"] != 0


class TestExecPipelineStatus:
    """ISSUE-307, in the shell that actually runs the command.

    The unit tier runs a server on the host, so it proves this build's `bash`
    honours the flag; these prove the *image's* does.
    """

    def test_a_failing_pipeline_is_not_reported_as_success(self):
        result = _exec("false | tail -1")
        assert result["status"] == "ok", result
        assert result["exit_code"] != 0, (
            "the container's shell reported success for a failing pipeline — "
            "`exit_code` in this envelope does not mean what it says"
        )

    def test_a_succeeding_pipeline_is_still_success(self):
        """Control: the option must not turn every pipeline red."""
        result = _exec("echo hi | tail -1")
        assert result["exit_code"] == 0, result
        assert result["stdout"] == "hi\n"

    def test_the_option_is_on_inside_the_container(self):
        result = _exec("set -o | grep pipefail")
        assert result["exit_code"] == 0, result
        assert result["stdout"].split() == ["pipefail", "on"], result["stdout"]


class TestExecFile:
    @pytest.mark.parametrize("name,body,expected", [
        ("probe.sh", "#!/bin/bash\necho shell-ok\n", "shell-ok\n"),
        ("probe.py", "print('python-ok')\n", "python-ok\n"),
    ])
    def test_runs_a_script_through_its_interpreter(
        self, allowlisted_dir, name, body, expected,
    ):
        script = allowlisted_dir / name
        script.write_text(body)
        result = _exec_file(script)
        assert result["status"] == "ok", result
        assert result["exit_code"] == 0, result
        assert result["stdout"] == expected

    def test_runs_a_script_with_no_extension_via_its_shebang(self, allowlisted_dir):
        """The staging directory used to be a `noexec` tmpfs, so this path
        could not work even once the file was reachable."""
        script = allowlisted_dir / "probe"
        script.write_text("#!/bin/bash\necho shebang-ok\n")
        result = _exec_file(script)
        assert result["status"] == "ok", result
        assert result["exit_code"] == 0, result
        assert result["stdout"] == "shebang-ok\n"

    def test_leaves_no_staged_copy_behind(self, allowlisted_dir):
        """The cleanup used to run against a path the file was never written
        to, so it removed nothing and exited 0.

        Scoped to this process's own staged name: the staging directory is
        shared by every caller and keyed on pid, so asserting it is globally
        empty would fail on any concurrent `exec-file` — another test, or a
        real task on the deployment.
        """
        script = allowlisted_dir / "probe.sh"
        script.write_text("#!/bin/bash\necho ok\n")
        assert _exec_file(script)["exit_code"] == 0

        staged = f"{devbox._EXEC_STAGING_DIR}/exec_{os.getpid()}_probe.sh"
        assert _exec(f"test -e {staged}")["exit_code"] != 0, staged


class TestStatus:
    def test_it_reports_the_container_and_the_transport_separately(self):
        """Two halves, and neither substitutes for the other: Docker says the
        container is running, the transport says the server inside it answers.
        """
        info = devbox.cmd_status(_args())

        assert info["status"] == "ok", info
        assert info["transport"]["reachable"] is True, info
        if shutil.which(devbox._docker_cli()) and devbox._container_name():
            # Only where this process can reach Docker at all — on the
            # deployment the CLI runs host-side and can, but the transport
            # half must not depend on it.
            assert "container" in info, info
