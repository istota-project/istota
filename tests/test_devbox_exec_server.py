"""The devbox exec server, against a real socket, running real subprocesses.

No Docker, which is the point: ``docker/devbox/scripts/istota-exec-serve`` is a
stdlib-only asyncio script, so the exit-status and refusal matrix it exists to
guarantee can be driven on the host with nothing installed. The container is
what the ``integration`` tier proves; correctness is what this file proves.

The roots are directories in a tmpdir rather than ``/srv/repos/<user>`` and
``/home/dev``, because a root is an argument to the server and not a constant in
it — that is Design 3, "the server is the boundary": every containment decision
is made by the process whose view of the filesystem is the one the command gets.

Read the assertion list in the spec's Test strategy alongside this file. Each
one is a named test here, and the two lists are meant to stay in step.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import struct
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from istota.devbox_exec_protocol import (
    ERR_BAD_REQUEST,
    ERR_COMMAND_NOT_FOUND,
    ERR_NO_SUCH_CWD,
    ERR_PATH_REFUSED,
    ERR_SPAWN_FAILED,
    ERR_TOO_LARGE,
    ERR_UNKNOWN_ACTION,
    MAX_READ_FILE_BYTES,
    MAX_REQUEST_BYTES,
    MAX_WRITE_FILE_BYTES,
    PROTOCOL_VERSION,
    SIGPIPE_EXIT,
    STREAM_CONTROL,
    STREAM_STDERR,
    STREAM_STDIN,
    STREAM_STDOUT,
    FrameDecoder,
    decode_ack,
    decode_control,
    encode_exec_request,
    encode_line,
    encode_ping_request,
    encode_read_file_request,
    encode_stat_request,
    encode_stdin_eof,
    encode_write_file_request,
    is_terminal,
    pack_frame,
)

SERVER = Path(__file__).resolve().parents[1] / "docker/devbox/scripts/istota-exec-serve"

# Generous enough that a loaded machine does not fail a status assertion, short
# enough that a hang is a failure rather than a wait for pytest's own timeout.
READ_TIMEOUT = 30.0


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


@dataclass
class Collected:
    """Everything one connection produced after its acknowledgement."""

    stdout: bytes = b""
    stderr: bytes = b""
    controls: list[dict] = field(default_factory=list)
    closed: bool = False

    @property
    def terminal(self) -> dict | None:
        for obj in reversed(self.controls):
            if is_terminal(obj):
                return obj
        return None


class Conn:
    """A blocking client for one connection, speaking the protocol by hand.

    Deliberately not the real client — that is Stage 2. This one exists so the
    server can be driven into shapes a well-behaved client never produces: a
    request with an ``env`` field, an abrupt disconnect, a malformed line.
    """

    def __init__(self, socket_path: str, timeout: float = READ_TIMEOUT) -> None:
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(timeout)
        self.sock.connect(socket_path)
        self._decoder = FrameDecoder()
        self._rest = b""

    def send(self, data: bytes) -> None:
        self.sock.sendall(data)

    def read_ack(self) -> dict:
        buf = b""
        while not buf.endswith(b"\n"):
            chunk = self.sock.recv(1)
            if not chunk:
                raise AssertionError(f"connection closed before an ack: {buf!r}")
            buf += chunk
        return decode_ack(buf)

    def read_nothing(self) -> bytes:
        """Whatever arrives after an error ack — which must be nothing."""
        try:
            return self.sock.recv(65536)
        except socket.timeout:  # pragma: no cover - a hang is the failure
            raise AssertionError("server neither streamed nor closed")

    def collect(self, *, until_terminal: bool = True) -> Collected:
        out = Collected()
        while True:
            try:
                chunk = self.sock.recv(65536)
            except socket.timeout:  # pragma: no cover - a hang is the failure
                raise AssertionError(
                    f"timed out; got stdout={out.stdout[:200]!r} "
                    f"controls={out.controls}"
                )
            if not chunk:
                out.closed = True
                return out
            for stream, payload in self._decoder.feed(chunk):
                if stream == STREAM_STDOUT:
                    out.stdout += payload
                elif stream == STREAM_STDERR:
                    out.stderr += payload
                elif stream == STREAM_CONTROL:
                    obj = decode_control(payload)
                    out.controls.append(obj)
                    if until_terminal and is_terminal(obj):
                        return out
                else:  # pragma: no cover - the server never sends stream 0
                    raise AssertionError(f"server sent stream {stream}")

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:  # pragma: no cover
            pass

    def __enter__(self) -> Conn:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


@dataclass
class Server:
    proc: subprocess.Popen
    socket_path: str
    base: Path
    repos: Path
    home: Path
    staging: Path
    outside: Path
    log: Path

    def connect(self, timeout: float = READ_TIMEOUT) -> Conn:
        return Conn(self.socket_path, timeout=timeout)

    def run(self, **request: object) -> Collected:
        """Send one exec request and collect everything that comes back."""
        with self.connect() as conn:
            conn.send(encode_exec_request(**request))  # type: ignore[arg-type]
            ack = conn.read_ack()
            assert ack["status"] == "ok", ack
            return conn.collect()

    def refusal(self, line: bytes) -> dict:
        """Send one request line and return the error acknowledgement."""
        with self.connect() as conn:
            conn.send(line)
            ack = conn.read_ack()
            assert ack["status"] == "error", ack
            assert conn.read_nothing() == b"", "an error ack streams nothing"
            return ack

    def log_text(self) -> str:
        return self.log.read_text(errors="replace")


def _start_server(
    *,
    idle_timeout: float | None = None,
    kill_grace: float | None = None,
    drain_grace: float | None = None,
    env: dict[str, str] | None = None,
) -> Server:
    # A Unix socket path is capped at ~104 bytes on darwin, and pytest's tmp_path
    # is long enough to blow through that once a class and test name are in it.
    base = Path(tempfile.mkdtemp(dir="/tmp", prefix="istota-exec-")).resolve()
    repos = base / "repos"
    home = base / "home"
    staging = home / ".istota-exec"
    outside = base / "outside"
    sock_dir = base / "sock"
    for d in (repos, home, staging, outside, sock_dir):
        d.mkdir(parents=True, exist_ok=True)
    socket_path = str(sock_dir / "exec.sock")

    argv = [
        sys.executable,
        str(SERVER),
        "--socket",
        socket_path,
        "--repos-root",
        str(repos),
        "--home",
        str(home),
        "--staging",
        str(staging),
    ]
    if idle_timeout is not None:
        argv += ["--idle-timeout-seconds", str(idle_timeout)]
    if kill_grace is not None:
        argv += ["--kill-grace-seconds", str(kill_grace)]
    if drain_grace is not None:
        argv += ["--drain-grace-seconds", str(drain_grace)]

    log = base / "server.log"
    child_env = dict(os.environ)
    child_env.pop("ISTOTA_EXEC_TEST_MARKER", None)
    if env:
        child_env.update(env)
    with open(log, "wb") as handle:
        # An open pipe on the server's own stdin, never written to and never
        # closed until teardown. Without it the server inherits pytest's null
        # fd 0, a child that inherited it would see EOF at once, and every
        # assertion that stdin is off unless requested would pass against a
        # server that handed the child its own descriptor. Verified by mutation:
        # with a null fd 0 those tests stay green against exactly that server.
        proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=handle,
            stderr=subprocess.STDOUT,
            env=child_env,
        )

    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(
                f"server exited {proc.returncode}: {log.read_text(errors='replace')}"
            )
        if os.path.exists(socket_path):
            try:
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(2.0)
                probe.connect(socket_path)
                probe.close()
                break
            except OSError:
                pass
        time.sleep(0.02)
    else:  # pragma: no cover
        raise AssertionError(f"server never listened: {log.read_text(errors='replace')}")

    return Server(
        proc=proc,
        socket_path=socket_path,
        base=base,
        repos=repos,
        home=home,
        staging=staging,
        outside=outside,
        log=log,
    )


def _stop_server(server: Server) -> None:
    if server.proc.poll() is None:
        server.proc.terminate()
        try:
            server.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            server.proc.kill()
            server.proc.wait(timeout=10)


@pytest.fixture
def server():
    srv = _start_server()
    try:
        yield srv
    finally:
        _stop_server(srv)


@pytest.fixture
def server_factory():
    started: list[Server] = []

    def make(**kwargs) -> Server:
        srv = _start_server(**kwargs)
        started.append(srv)
        return srv

    try:
        yield make
    finally:
        for srv in started:
            _stop_server(srv)


def _temp_files(root):
    """The write verb's in-flight temp files under a root, by name."""
    return sorted(q.name for q in root.rglob(".istota-exec-*.tmp"))


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover - same uid in this suite
        return True
    return True


# --------------------------------------------------------------------------- #
# Exit status
# --------------------------------------------------------------------------- #


class TestTheExitStatusIsReal:
    """The measurement this whole transport exists for: `docker exec` through
    the API proxy returns 1 for everything, so every assertion here is one the
    mechanism it replaces could not make."""

    def test_false_is_one(self, server):
        out = server.run(argv=["false"], cwd=str(server.repos))
        assert out.terminal["exit_code"] == 1
        assert out.terminal["signal"] is None

    def test_exit_seven_is_seven(self, server):
        out = server.run(argv=["sh", "-c", "exit 7"], cwd=str(server.repos))
        assert out.terminal["exit_code"] == 7

    def test_true_is_zero(self, server):
        out = server.run(argv=["true"], cwd=str(server.repos))
        assert out.terminal["exit_code"] == 0

    def test_the_shell_form_keeps_pipefail(self, server):
        """ISSUE-307's rule, preserved server-side: without pipefail this is 0,
        because `tail` succeeds."""
        out = server.run(shell="false | tail -1", cwd=str(server.repos))
        assert out.terminal["exit_code"] == 1

    def test_a_duration_is_reported(self, server):
        out = server.run(argv=["true"], cwd=str(server.repos))
        assert isinstance(out.terminal["duration_ms"], int)
        assert out.terminal["duration_ms"] >= 0


class TestTheServerNeverInfersASignal:
    def test_sigpipe_in_a_pipeline_is_141_with_no_signal(self, server):
        """`yes | head -1` makes bash *exit* 141. A server reporting
        signal: "SIGPIPE" here would be fabricating one."""
        out = server.run(shell="yes | head -1", cwd=str(server.repos))
        assert out.terminal["exit_code"] == SIGPIPE_EXIT
        assert out.terminal["signal"] is None
        assert "SIGPIPE" in out.terminal["note"]

    def test_a_genuinely_signalled_child_reports_its_signal(self, server):
        out = server.run(argv=["sh", "-c", "kill -TERM $$"], cwd=str(server.repos))
        assert out.terminal["signal"] == "SIGTERM"
        assert out.terminal["exit_code"] == 143
        assert "note" not in out.terminal

    def test_a_plain_exit_141_carries_the_note_but_no_signal(self, server):
        """A program may legitimately exit 141; it still gets the hint and
        still reports no signal."""
        out = server.run(argv=["sh", "-c", "exit 141"], cwd=str(server.repos))
        assert out.terminal["exit_code"] == SIGPIPE_EXIT
        assert out.terminal["signal"] is None
        assert "note" in out.terminal


# --------------------------------------------------------------------------- #
# Output
# --------------------------------------------------------------------------- #


class TestOutput:
    def test_two_megabytes_of_stdout_arrive_whole(self, server):
        """No transport-level output cap — the JSON envelope's 100 KB ceiling is
        one of the three things Design 2 is written to remove."""
        size = 2 * 1024 * 1024
        out = server.run(
            argv=[
                sys.executable,
                "-c",
                f"import sys; sys.stdout.buffer.write(b'x' * {size})",
            ],
            cwd=str(server.repos),
        )
        assert len(out.stdout) == size
        assert out.stdout == b"x" * size
        assert out.terminal["exit_code"] == 0

    def test_stdout_and_stderr_keep_their_own_frame_ids(self, server):
        out = server.run(
            argv=["sh", "-c", "echo to-stdout; echo to-stderr >&2"],
            cwd=str(server.repos),
        )
        assert out.stdout == b"to-stdout\n"
        assert out.stderr == b"to-stderr\n"

    def test_binary_output_survives(self, server):
        out = server.run(
            argv=[
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(bytes(range(256)))",
            ],
            cwd=str(server.repos),
        )
        assert out.stdout == bytes(range(256))


class TestStdin:
    def test_stdin_is_delivered_when_it_is_requested(self, server):
        with server.connect() as conn:
            conn.send(
                encode_exec_request(argv=["cat"], cwd=str(server.repos), stdin=True)
            )
            assert conn.read_ack()["status"] == "ok"
            conn.send(pack_frame(STREAM_STDIN, b"hello "))
            conn.send(pack_frame(STREAM_STDIN, b"devbox"))
            conn.send(encode_stdin_eof())
            out = conn.collect()
        assert out.stdout == b"hello devbox"
        assert out.terminal["exit_code"] == 0

    def test_the_child_sees_eof_at_once_when_stdin_is_not_requested(self, server):
        """`cat` with an inherited descriptor would block forever. The server
        passes an explicit closed pipe instead of its own stdin."""
        out = server.run(argv=["cat"], cwd=str(server.repos), stdin=False)
        assert out.stdout == b""
        assert out.terminal["exit_code"] == 0

    def test_a_stdin_frame_sent_against_a_closed_pipe_is_dropped(self, server):
        """A client that declared stdin: false and then sends bytes anyway does
        not get a descriptor the request never asked for."""
        with server.connect() as conn:
            conn.send(
                encode_exec_request(
                    argv=["sh", "-c", "cat; echo done"],
                    cwd=str(server.repos),
                    stdin=False,
                )
            )
            assert conn.read_ack()["status"] == "ok"
            conn.send(pack_frame(STREAM_STDIN, b"ignored"))
            out = conn.collect()
        assert out.stdout == b"done\n"
        assert out.terminal["exit_code"] == 0

    def test_a_stdin_frame_after_the_eof_marker_is_dropped(self, server):
        """The other half of the same guard, and the only shape where the
        `want_stdin` test and the closed-pipe test are doing separate work."""
        with server.connect() as conn:
            conn.send(
                encode_exec_request(
                    argv=["sh", "-c", "cat; echo done"],
                    cwd=str(server.repos),
                    stdin=True,
                )
            )
            assert conn.read_ack()["status"] == "ok"
            conn.send(pack_frame(STREAM_STDIN, b"first"))
            conn.send(encode_stdin_eof())
            conn.send(pack_frame(STREAM_STDIN, b"-after-the-end"))
            out = conn.collect()
        assert out.stdout == b"firstdone\n"
        assert out.terminal["exit_code"] == 0

    def test_a_child_that_closes_its_own_stdin_is_not_a_disconnect(self, server):
        """`head -c 5` reads what it wants and closes. The write that follows
        fails with EPIPE, which must stop the pump rather than kill the command
        as a client that went away — and must be retrieved rather than left on
        the loop's floor as an unretrieved-exception warning."""
        with server.connect() as conn:
            conn.send(
                encode_exec_request(
                    argv=["sh", "-c", "head -c 5; sleep 0.3; echo ok"],
                    cwd=str(server.repos),
                    stdin=True,
                )
            )
            assert conn.read_ack()["status"] == "ok"
            for _ in range(16):
                conn.send(pack_frame(STREAM_STDIN, b"x" * 65536))
            conn.send(encode_stdin_eof())
            out = conn.collect()
        assert out.terminal["exit_code"] == 0
        assert out.stdout.endswith(b"ok\n")
        assert "Future exception was never retrieved" not in server.log_text()


# --------------------------------------------------------------------------- #
# The server is the boundary
# --------------------------------------------------------------------------- #


class TestTheWorkingDirectoryIsChecked:
    def test_a_cwd_under_the_repos_root_is_accepted(self, server):
        work = server.repos / "project"
        work.mkdir()
        out = server.run(argv=["pwd"], cwd=str(work))
        assert out.stdout.strip() == str(work).encode()

    def test_a_cwd_outside_the_root_is_refused_before_anything_runs(self, server):
        marker = server.outside / "ran"
        ack = server.refusal(
            encode_exec_request(
                argv=["sh", "-c", f"touch {marker}"], cwd=str(server.outside)
            )
        )
        assert ack["code"] == ERR_PATH_REFUSED
        assert not marker.exists(), "the command ran despite the refusal"

    def test_the_home_root_is_not_a_working_directory(self, server):
        """Design 3 scopes a *named* cwd to the repos root alone. /home/dev is a
        file root so the staging path can be written; it is not somewhere a
        caller may point a command, because a passed-through cwd that exists in
        both namespaces is the case that used to run silently in the container's
        own /tmp.

        This is also the assertion that keeps the `cwd: null` amendment from
        being read as a widening of the roots: the server may choose this
        directory, and a request may not name it."""
        ack = server.refusal(encode_exec_request(argv=["pwd"], cwd=str(server.home)))
        assert ack["code"] == ERR_PATH_REFUSED

    def test_a_null_cwd_runs_in_the_servers_own_home(self, server):
        """`null` means "you choose", and the choice is this process's `--home`
        constant rather than a path that travelled over the wire. It is what the
        devbox skill's ad-hoc verbs send, which have no repository to stand in."""
        out = server.run(argv=["pwd"], cwd=None)
        assert out.stdout.strip() == str(server.home).encode()
        assert out.terminal["exit_code"] == 0

    def test_a_missing_cwd_key_is_a_bad_request_and_not_the_default(self, server):
        """Forgetting a field and declining to name one are different
        statements. A client whose `getcwd` broke must not land in /home/dev."""
        marker = server.home / "should-not-exist"
        ack = server.refusal(
            encode_line(
                {
                    "action": "exec",
                    "argv": ["sh", "-c", f"touch {marker}"],
                    "stdin": False,
                    "timeout": 0,
                }
            )
        )
        assert ack["code"] == ERR_BAD_REQUEST
        assert not marker.exists(), "the command ran despite the refusal"

    def test_a_symlink_out_of_the_root_is_resolved_before_the_test(self, server):
        link = server.repos / "escape"
        link.symlink_to(server.outside)
        ack = server.refusal(encode_exec_request(argv=["pwd"], cwd=str(link)))
        assert ack["code"] == ERR_PATH_REFUSED

    def test_a_missing_directory_under_the_root_is_no_such_cwd(self, server):
        ack = server.refusal(
            encode_exec_request(argv=["pwd"], cwd=str(server.repos / "gone"))
        )
        assert ack["code"] == ERR_NO_SUCH_CWD

    def test_a_relative_cwd_is_refused(self, server):
        ack = server.refusal(encode_exec_request(argv=["pwd"], cwd="project"))
        assert ack["code"] == ERR_PATH_REFUSED


class TestTheFileVerbsAreChecked:
    def test_write_file_and_read_file_round_trip_under_a_root(self, server):
        target = server.repos / "note.txt"
        body = b"one\ntwo\n"
        with server.connect() as conn:
            conn.send(
                encode_write_file_request(path=str(target), size=len(body), mode=0o600)
            )
            assert conn.read_ack()["status"] == "ok"
            conn.send(pack_frame(STREAM_STDIN, body))
            out = conn.collect()
        assert out.terminal["exit_code"] == 0
        assert target.read_bytes() == body
        assert target.stat().st_mode & 0o777 == 0o600

        with server.connect() as conn:
            conn.send(encode_read_file_request(path=str(target)))
            assert conn.read_ack()["status"] == "ok"
            back = conn.collect()
        assert back.stdout == body
        assert back.terminal["exit_code"] == 0

    def test_the_staging_dir_is_a_file_root(self, server):
        """`exec-file` writes there, so it has to be writable — and it is a
        root the server knows about rather than a path the caller asserted."""
        target = server.staging / "script.sh"
        body = b"#!/bin/sh\necho hi\n"
        with server.connect() as conn:
            conn.send(encode_write_file_request(path=str(target), size=len(body)))
            assert conn.read_ack()["status"] == "ok"
            conn.send(pack_frame(STREAM_STDIN, body))
            conn.collect()
        assert target.read_bytes() == body

    def test_a_write_outside_the_root_list_is_refused(self, server):
        target = server.outside / "planted"
        ack = server.refusal(encode_write_file_request(path=str(target), size=3))
        assert ack["code"] == ERR_PATH_REFUSED
        assert not target.exists()

    def test_a_read_outside_the_root_list_is_refused(self, server):
        target = server.outside / "secret"
        target.write_bytes(b"no")
        ack = server.refusal(encode_read_file_request(path=str(target)))
        assert ack["code"] == ERR_PATH_REFUSED

    def test_a_symlink_pointing_out_of_the_root_is_refused(self, server):
        secret = server.outside / "secret"
        secret.write_bytes(b"no")
        link = server.repos / "link"
        link.symlink_to(secret)
        ack = server.refusal(encode_read_file_request(path=str(link)))
        assert ack["code"] == ERR_PATH_REFUSED

    def test_a_relative_path_is_refused(self, server):
        ack = server.refusal(encode_read_file_request(path="note.txt"))
        assert ack["code"] == ERR_PATH_REFUSED

    def test_a_directory_is_not_a_readable_file(self, server):
        ack = server.refusal(encode_read_file_request(path=str(server.repos)))
        assert ack["code"] == ERR_BAD_REQUEST

    def test_a_missing_file_says_so_rather_than_refusing_the_path(self, server):
        ack = server.refusal(encode_read_file_request(path=str(server.repos / "nope")))
        assert ack["code"] == ERR_BAD_REQUEST
        assert "refused" not in ack["message"]


class TestTheTwoDirectoriesRefusedByName:
    """Both would fail the root test anyway. They are refused by name because a
    credential directory and the transport's own socket directory each deserve
    their own message — and because the root list is an argument to the server
    while these two are properties of the deployment."""

    @pytest.mark.parametrize("refused", ["/run/istota-cred", "/run/istota-exec"])
    def test_a_read_is_refused_by_name(self, server, refused):
        ack = server.refusal(encode_read_file_request(path=f"{refused}/user/token"))
        assert ack["code"] == ERR_PATH_REFUSED
        assert refused in ack["message"]

    @pytest.mark.parametrize("refused", ["/run/istota-cred", "/run/istota-exec"])
    def test_a_write_is_refused_by_name(self, server, refused):
        ack = server.refusal(
            encode_write_file_request(path=f"{refused}/user/exec.sock", size=1)
        )
        assert ack["code"] == ERR_PATH_REFUSED
        assert refused in ack["message"]

    def test_the_directory_itself_is_refused_not_only_its_children(self, server):
        ack = server.refusal(encode_read_file_request(path="/run/istota-cred"))
        assert ack["code"] == ERR_PATH_REFUSED

    def test_a_lookalike_sibling_is_not_swept_up(self, server):
        """`/run/istota-credentials` is a different directory; it is refused for
        being outside the roots, not for its name."""
        ack = server.refusal(encode_read_file_request(path="/run/istota-credentials"))
        assert ack["code"] == ERR_PATH_REFUSED
        assert "refused by name" not in ack["message"]
        assert "credential socket directory" not in ack["message"]


class TestTheCaps:
    def test_a_read_over_the_cap_is_refused_rather_than_truncated(self, server):
        big = server.repos / "big.bin"
        with open(big, "wb") as handle:
            handle.truncate(MAX_READ_FILE_BYTES + 1)
        with server.connect() as conn:
            conn.send(encode_read_file_request(path=str(big)))
            ack = conn.read_ack()
            assert ack["status"] == "error"
            assert ack["code"] == ERR_TOO_LARGE
            assert conn.read_nothing() == b"", "a refusal must not stream a prefix"

    def test_a_write_over_the_cap_is_refused_before_a_byte_is_sent(self, server):
        target = server.repos / "big.bin"
        ack = server.refusal(
            encode_line(
                {
                    "action": "write_file",
                    "path": str(target),
                    "mode": 0o644,
                    "size": MAX_WRITE_FILE_BYTES + 1,
                }
            )
        )
        assert ack["code"] == ERR_TOO_LARGE
        assert not target.exists()

    def test_a_request_line_over_the_cap_is_refused(self, server):
        line = encode_line(
            {
                "action": "exec",
                "argv": ["true"],
                "cwd": str(server.repos),
                "pad": "p" * (MAX_REQUEST_BYTES + 1024),
            }
        )
        with server.connect() as conn:
            conn.send(line)
            ack = conn.read_ack()
        assert ack["status"] == "error"
        assert ack["code"] == ERR_TOO_LARGE


class TestTheChildsEnvironmentIsTheServersOwn:
    def test_the_child_inherits_the_servers_environment(self, server_factory):
        srv = server_factory(env={"ISTOTA_EXEC_TEST_MARKER": "from-the-container"})
        out = srv.run(
            argv=["sh", "-c", "echo $ISTOTA_EXEC_TEST_MARKER"], cwd=str(srv.repos)
        )
        assert out.stdout.strip() == b"from-the-container"

    def test_no_request_field_can_alter_it(self, server_factory):
        """Design 4 deletes the `env` field. A hand-written client sending one
        anyway must change nothing — which is why the deletion is enforced here
        and not only in the encoder."""
        srv = server_factory(env={"ISTOTA_EXEC_TEST_MARKER": "from-the-container"})
        line = encode_line(
            {
                "action": "exec",
                "argv": ["sh", "-c", "echo $ISTOTA_EXEC_TEST_MARKER"],
                "cwd": str(srv.repos),
                "env": {"ISTOTA_EXEC_TEST_MARKER": "from-the-model"},
            }
        )
        with srv.connect() as conn:
            conn.send(line)
            assert conn.read_ack()["status"] == "ok"
            out = conn.collect()
        assert out.stdout.strip() == b"from-the-container"


# --------------------------------------------------------------------------- #
# Reaping
# --------------------------------------------------------------------------- #


class TestReaping:
    def test_a_client_disconnect_kills_the_whole_process_group(self, server_factory):
        """The one reap signal in the design. A background child of the command
        is in the same session, so the group kill is what reaches it — killing
        the direct child alone would leave it running."""
        srv = server_factory(kill_grace=0.5)
        conn = srv.connect()
        conn.send(
            encode_exec_request(
                argv=["sh", "-c", "sleep 300 & echo $!; echo $$; sleep 300"],
                cwd=str(srv.repos),
            )
        )
        assert conn.read_ack()["status"] == "ok"

        deadline = time.monotonic() + READ_TIMEOUT
        pids: list[int] = []
        decoder = FrameDecoder()
        while len(pids) < 2 and time.monotonic() < deadline:
            for stream, payload in decoder.feed(conn.sock.recv(65536)):
                if stream == STREAM_STDOUT:
                    pids += [int(x) for x in payload.split()]
        assert len(pids) == 2, f"expected two pids, got {pids}"
        assert all(_alive(pid) for pid in pids), "the fixture never started"

        conn.close()

        gone_by = time.monotonic() + 15.0
        while time.monotonic() < gone_by:
            if not any(_alive(pid) for pid in pids):
                break
            time.sleep(0.05)
        assert not any(_alive(pid) for pid in pids), f"{pids} survived the disconnect"
        assert "disconnect" in srv.log_text()

    def test_a_timeout_kills_the_group_and_says_why(self, server):
        out = server.run(
            argv=["sh", "-c", "sleep 300"], cwd=str(server.repos), timeout=1
        )
        assert out.terminal["reason"] == "timeout"
        assert out.terminal["signal"] == "SIGKILL"
        assert out.terminal["exit_code"] == 128 + signal.SIGKILL

    def test_the_idle_timeout_is_the_backstop_and_says_so(self, server_factory):
        srv = server_factory(idle_timeout=1.0)
        out = srv.run(argv=["sh", "-c", "sleep 300"], cwd=str(srv.repos))
        assert out.terminal["reason"] == "idle"
        assert out.terminal["signal"] == "SIGKILL"

    def test_output_keeps_the_idle_timer_alive(self, server_factory):
        """Traffic in either direction counts, so a chatty build is not reaped
        by a backstop meant for a connection whose peer went away."""
        srv = server_factory(idle_timeout=2.0)
        out = srv.run(
            argv=["sh", "-c", "for i in 1 2 3 4 5 6; do echo tick; sleep 0.5; done"],
            cwd=str(srv.repos),
        )
        assert out.terminal["exit_code"] == 0
        assert "reason" not in out.terminal
        assert out.stdout.count(b"tick") == 6


class TestNothingACommandStartedOutlivesIt:
    """Design 11's other half, and the one that is easy to ship broken: the
    disconnect path gets all the attention, while the common case is a command
    that simply finishes."""

    def test_a_completed_command_reports_at_once_and_takes_its_orphan_with_it(
        self, server
    ):
        """asyncio's `Process.wait()` returns when the output pipes disconnect,
        not when the child exits, so a backgrounded process holding stdout kept
        it pending — the terminal frame arrived an hour later, if at all, and
        carried `reason: idle` for a command that had exited cleanly."""
        started = time.monotonic()
        with server.connect() as conn:
            conn.send(
                encode_exec_request(
                    argv=["sh", "-c", "sleep 300 & echo $!; exit 0"],
                    cwd=str(server.repos),
                )
            )
            assert conn.read_ack()["status"] == "ok"
            out = conn.collect()
        elapsed = time.monotonic() - started
        assert out.terminal["exit_code"] == 0
        assert "reason" not in out.terminal, "a finished command was reaped as idle"
        assert elapsed < 10, f"the terminal frame took {elapsed:.1f}s"

        orphan = int(out.stdout.strip())
        gone_by = time.monotonic() + 10.0
        while time.monotonic() < gone_by and _alive(orphan):
            time.sleep(0.05)
        assert not _alive(orphan), f"{orphan} outlived the command that started it"

    def test_a_client_that_closes_before_the_ack_still_gets_its_group_reaped(
        self, server
    ):
        """The narrowest window there is, and the daemon's cancel path produces
        it: a SIGKILL of the client between the request and the answer. The ack
        write then fails, and without a `finally` around the whole exec the
        command runs on with nothing watching it — measured, indefinitely."""
        survived = server.repos / "survived"
        conn = server.connect()
        conn.send(
            encode_exec_request(
                argv=["sh", "-c", f"sleep 3; touch {survived}"],
                cwd=str(server.repos),
            )
        )
        conn.close()

        # The reap is the witness, not the first marker: the group is killed
        # within microseconds of the failed acknowledgement, usually before the
        # shell has run its first command, so `started` is a race. The log line
        # is written only when something was still alive to kill, and without
        # the `finally` there is no line at all and `survived` appears at +3s.
        deadline = time.monotonic() + 20.0
        while time.monotonic() < deadline and "at exit" not in server.log_text():
            time.sleep(0.05)
        assert "reaping process group" in server.log_text()
        time.sleep(5.0)
        assert not survived.exists(), "the command outlived the connection that asked"


class TestShutdown:
    def test_a_stopping_server_kills_the_commands_it_started(self, server_factory):
        """The container outlives this process — the supervisor restarts it a
        second later. A shutdown that forgot its children would accumulate a
        build tree per restart, running against the repos mount with nothing
        watching it."""
        srv = server_factory()
        conn = srv.connect()
        conn.send(
            encode_exec_request(
                argv=["sh", "-c", "echo $$; sleep 300"], cwd=str(srv.repos)
            )
        )
        assert conn.read_ack()["status"] == "ok"
        decoder = FrameDecoder()
        pid = None
        deadline = time.monotonic() + READ_TIMEOUT
        while pid is None and time.monotonic() < deadline:
            for stream, payload in decoder.feed(conn.sock.recv(65536)):
                if stream == STREAM_STDOUT and payload.strip():
                    pid = int(payload.split()[0])
        assert pid and _alive(pid)

        stopping = time.monotonic()
        srv.proc.terminate()
        srv.proc.wait(timeout=20)
        stopped_in = time.monotonic() - stopping
        conn.close()

        # Promptly, not eventually. From 3.12 `wait_closed` waits for live
        # handlers, so a server that leaves its commands running holds the
        # shutdown open — and the supervisor restarts a second later, which
        # makes that stall an outage on a box where the whole point is that a
        # crash costs one second.
        assert stopped_in < 4.0, f"the shutdown took {stopped_in:.1f}s"

        gone_by = time.monotonic() + 10.0
        while time.monotonic() < gone_by and _alive(pid):
            time.sleep(0.05)
        assert not _alive(pid), f"{pid} survived the server that started it"

    def test_a_sigkilled_server_does_not_orphan_the_commands_it_started(
        self, server_factory
    ):
        """The graceful path above runs from a `finally`, and a `finally` does
        not run on SIGKILL — which is the death the supervisor exists for, since
        the reason it was written is the server being picked off when the
        container reaches its `mem_limit`. Measured before the reaper: the
        command survived indefinitely, invisible to the replacement server, to
        `stat` and to every log."""
        srv = server_factory()
        conn = srv.connect()
        pid = None
        try:
            conn.send(
                encode_exec_request(
                    argv=["sh", "-c", "echo $$; sleep 300"], cwd=str(srv.repos)
                )
            )
            assert conn.read_ack()["status"] == "ok"
            decoder = FrameDecoder()
            deadline = time.monotonic() + READ_TIMEOUT
            while pid is None and time.monotonic() < deadline:
                for stream, payload in decoder.feed(conn.sock.recv(65536)):
                    if stream == STREAM_STDOUT and payload.strip():
                        pid = int(payload.split()[0])
            assert pid and _alive(pid), "the fixture never started"

            srv.proc.kill()
            srv.proc.wait(timeout=10)

            gone_by = time.monotonic() + 10.0
            while time.monotonic() < gone_by and _alive(pid):
                time.sleep(0.05)
            assert not _alive(pid), f"{pid} outlived the SIGKILLed server"
        finally:
            conn.close()
            if pid and _alive(pid):
                os.killpg(pid, signal.SIGKILL)

    def test_the_reaper_kills_a_group_whose_leader_has_already_exited(
        self, server_factory
    ):
        """The group, not the leader. A `sh -c 'daemon & exit'` leaves the group
        alive with its leader gone, so nothing keyed on the leader's own
        liveness — a `/proc/<pgid>/stat` read, a start-time comparison — can see
        it. That shape is the ordinary one for a backgrounded dev server."""
        srv = server_factory()
        marker = srv.repos / "child.pid"
        conn = srv.connect()
        pid = None
        try:
            conn.send(
                encode_exec_request(
                    shell=f"( sleep 300 & echo $! > {marker} ) ; exit 0",
                    cwd=str(srv.repos),
                )
            )
            assert conn.read_ack()["status"] == "ok"
            deadline = time.monotonic() + READ_TIMEOUT
            while time.monotonic() < deadline and not marker.exists():
                time.sleep(0.02)
            pid = int(marker.read_text().strip())
            assert _alive(pid), "the backgrounded child never started"
            # Without this the test could pass with the reaper removed: the
            # command exits at once, so `_do_exec`'s own `finally` is racing to
            # kill the same group. Asking the server proves it still holds the
            # group at the moment of the SIGKILL, so whatever killed the child
            # afterwards was not that path.
            with srv.connect() as probe:
                probe.send(encode_stat_request())
                assert probe.read_ack()["status"] == "ok"
                assert probe.collect().controls[0]["process_groups"] == 1

            srv.proc.kill()
            srv.proc.wait(timeout=10)

            gone_by = time.monotonic() + 10.0
            while time.monotonic() < gone_by and _alive(pid):
                time.sleep(0.05)
            assert not _alive(pid), f"{pid} outlived the SIGKILLed server"
        finally:
            conn.close()
            if pid and _alive(pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)


class TestOutputIsNeverSilentlyLost:
    """The drain grace exists for a pipe nobody will close, not for a client
    that has not got round to reading yet. Bounding it naively dropped 80 KiB of
    a 400 KiB run on Linux and still reported exit 0.

    The rule is unit-tested rather than driven through a socket, deliberately.
    Reaching the state end to end — child exited, pump still pushing bytes at a
    stalled reader — depends on the pipe buffer, the socket buffer and asyncio's
    own high-water mark, and the sizes that hit it on Linux do not on darwin. A
    scenario test for it passes on this machine whatever the rule says, which is
    the one thing a test must not do."""

    def test_the_drain_gives_up_on_a_pipe_nobody_will_close(self):
        module = _load_server_module()

        async def go():
            server = module.ExecServer(
                module.Roots("/tmp", "/tmp", "/tmp"), drain_grace=0.3
            )
            states = (module._PumpState(), module._PumpState())
            pumps = tuple(
                asyncio.ensure_future(asyncio.sleep(30)) for _ in range(len(states))
            )
            begun = time.monotonic()
            drained = await server._drain(pumps, states)
            elapsed = time.monotonic() - begun
            for pump in pumps:
                pump.cancel()
            return drained, elapsed

        drained, elapsed = asyncio.run(go())
        assert drained is False
        assert 0.3 <= elapsed < 5.0

    def test_the_drain_waits_out_a_pump_that_is_writing_to_the_client(self):
        module = _load_server_module()

        async def go():
            server = module.ExecServer(
                module.Roots("/tmp", "/tmp", "/tmp"), drain_grace=0.2
            )
            states = (module._PumpState(), module._PumpState())
            states[0].writing = True
            gates = [asyncio.get_running_loop().create_future() for _ in states]
            pumps = tuple(asyncio.ensure_future(gate) for gate in gates)
            drain = asyncio.ensure_future(server._drain(pumps, states))
            # Five times the grace, and it must still be waiting.
            await asyncio.sleep(1.0)
            still_waiting = not drain.done()
            states[0].writing = False
            for gate in gates:
                gate.set_result(None)
            return still_waiting, await drain

        still_waiting, drained = asyncio.run(go())
        assert still_waiting, "a slow reader was treated as a truncation"
        assert drained is True

    def test_output_that_could_not_be_read_is_reported_as_truncated(
        self, server_factory
    ):
        """A process that `setsid`s out of the group survives the reap and holds
        the descriptor. Nothing can be done about that; what can be done is not
        reporting a clean status over the hole it leaves."""
        srv = server_factory(drain_grace=1.0)
        holder = (
            "import os, sys, time\n"
            "sys.stdout.write('start\\n'); sys.stdout.flush()\n"
            "if os.fork() == 0:\n"
            "    os.setsid()\n"
            "    time.sleep(4)\n"
            "    os._exit(0)\n"
            "sys.exit(0)\n"
        )
        out = srv.run(argv=[sys.executable, "-c", holder], cwd=str(srv.repos))
        assert out.terminal["exit_code"] == 0
        assert out.terminal["truncated"] is True
        assert "truncated" in srv.log_text()


class TestAFramingErrorIsNotADisconnect:
    def test_a_malformed_frame_after_the_ack_gets_an_answer(self, server):
        """The peer is still there. Treating it as a disconnect killed the
        command and closed the connection with nothing said, which a client
        reads as "the container died mid-build"."""
        with server.connect() as conn:
            conn.send(
                encode_exec_request(
                    argv=["sh", "-c", "sleep 30"], cwd=str(server.repos)
                )
            )
            assert conn.read_ack()["status"] == "ok"
            conn.send(struct.pack(">BxxxI", 7, 0))  # no such stream id
            out = conn.collect()
        assert out.terminal["error"] == ERR_BAD_REQUEST
        assert out.terminal["signal"] == "SIGKILL"

    def test_a_post_acknowledgement_failure_never_invents_an_exit_code(self, server):
        """A server fault is not a command that exited 1. `exit_code` is null,
        and the code says what actually happened."""
        target = server.repos / "short.bin"
        with server.connect() as conn:
            conn.send(encode_write_file_request(path=str(target), size=100))
            assert conn.read_ack()["status"] == "ok"
            conn.send(pack_frame(STREAM_STDIN, b"only ten!!"))
            conn.send(encode_stdin_eof())
            out = conn.collect()
        assert out.terminal["exit_code"] is None
        assert out.terminal["error"] == ERR_BAD_REQUEST
        assert not target.exists()
        assert _temp_files(server.repos) == [], "a partial transfer left a temp file"


class TestTheWriteVerbCleansUpAfterItself:
    def test_a_body_longer_than_declared_is_refused(self, server):
        target = server.repos / "over.bin"
        with server.connect() as conn:
            conn.send(encode_write_file_request(path=str(target), size=4))
            assert conn.read_ack()["status"] == "ok"
            conn.send(pack_frame(STREAM_STDIN, b"far too many bytes"))
            out = conn.collect()
        assert out.terminal["error"] == ERR_TOO_LARGE
        assert not target.exists()
        assert _temp_files(server.repos) == []

    def test_a_directory_destination_is_refused_before_the_acknowledgement(
        self, server
    ):
        """Everything that can refuse refuses before the caller is told to send,
        which is the ordering `exec` uses for its spawn."""
        ack = server.refusal(encode_write_file_request(path=str(server.repos), size=4))
        assert ack["code"] == ERR_BAD_REQUEST

    def test_a_stalled_body_times_out_and_leaves_nothing_behind(self, server_factory):
        """The temp file lands under the repos mount, where `worktree_reaper`
        counts an untracked file as dirt and pins the worktree holding it for
        good. A stall must not be able to leave one."""
        srv = server_factory(idle_timeout=1.0)
        target = srv.repos / "stalled.bin"
        with srv.connect() as conn:
            conn.send(encode_write_file_request(path=str(target), size=100))
            assert conn.read_ack()["status"] == "ok"
            out = conn.collect()
        assert out.terminal["error"] == ERR_BAD_REQUEST
        assert not target.exists()
        assert _temp_files(srv.repos) == []


class TestTheIdleTimeoutCoversEveryRead:
    def test_a_connection_that_never_asks_for_anything_is_let_go(self, server_factory):
        srv = server_factory(idle_timeout=1.0)
        with srv.connect() as conn:
            ack = conn.read_ack()
        assert ack["status"] == "error"
        assert ack["code"] == ERR_BAD_REQUEST


class TestConcurrency:
    def test_ten_concurrent_execs_each_get_their_own_status(self, server):
        results: dict[int, int] = {}
        lock = threading.Lock()

        def one(n: int) -> None:
            out = server.run(
                argv=["sh", "-c", f"sleep 0.2; echo {n}; exit {n}"],
                cwd=str(server.repos),
            )
            with lock:
                results[n] = out.terminal["exit_code"]
                assert out.stdout.strip() == str(n).encode()

        threads = [threading.Thread(target=one, args=(n,)) for n in range(1, 11)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=READ_TIMEOUT)
        assert results == {n: n for n in range(1, 11)}


# --------------------------------------------------------------------------- #
# Malformed input, and the two informational actions
# --------------------------------------------------------------------------- #


class TestMalformedRequests:
    def test_an_unknown_action_is_named(self, server):
        ack = server.refusal(encode_line({"action": "rm_rf"}))
        assert ack["code"] == ERR_UNKNOWN_ACTION

    def test_malformed_json_is_a_bad_request(self, server):
        ack = server.refusal(b"{not json\n")
        assert ack["code"] == ERR_BAD_REQUEST

    def test_a_request_that_is_not_an_object_is_a_bad_request(self, server):
        ack = server.refusal(b'["exec"]\n')
        assert ack["code"] == ERR_BAD_REQUEST

    def test_argv_and_shell_together_are_refused(self, server):
        ack = server.refusal(
            encode_line(
                {
                    "action": "exec",
                    "argv": ["true"],
                    "shell": "true",
                    "cwd": str(server.repos),
                }
            )
        )
        assert ack["code"] == ERR_BAD_REQUEST

    def test_a_missing_binary_is_command_not_found_and_streams_nothing(self, server):
        """Its own code rather than `spawn_failed`, because the client can only
        say the useful thing about this one — that the command was routed into
        a container it is not installed in (ISSUE-336)."""
        ack = server.refusal(
            encode_exec_request(
                argv=["istota-no-such-binary-42"], cwd=str(server.repos)
            )
        )
        assert ack["code"] == ERR_COMMAND_NOT_FOUND
        assert "istota-no-such-binary-42" in ack["message"]

    def test_a_spawn_failure_that_is_not_a_missing_binary_stays_generic(
        self, server, tmp_path
    ):
        """The discriminator, and the reason the split is on errno rather than
        on "the spawn raised": a file that exists and cannot be executed is a
        different problem, and telling its caller to install it is wrong."""
        not_executable = tmp_path / "not-executable"
        not_executable.write_text("#!/bin/sh\nexit 0\n")
        not_executable.chmod(0o644)
        ack = server.refusal(
            encode_exec_request(argv=[str(not_executable)], cwd=str(server.repos))
        )
        assert ack["code"] == ERR_SPAWN_FAILED

    def test_one_bad_connection_does_not_take_the_server_down(self, server):
        server.refusal(b"{not json\n")
        out = server.run(argv=["true"], cwd=str(server.repos))
        assert out.terminal["exit_code"] == 0


class TestPingAndStat:
    def test_ping_answers_with_the_protocol_version(self, server):
        with server.connect() as conn:
            conn.send(encode_ping_request())
            ack = conn.read_ack()
            assert ack == {"status": "ok", "protocol": PROTOCOL_VERSION}
            out = conn.collect()
        reply = out.controls[0]
        assert reply["pong"] is True
        assert reply["protocol"] == PROTOCOL_VERSION
        assert out.terminal["exit_code"] == 0

    def test_stat_reports_the_roots_it_enforces(self, server):
        with server.connect() as conn:
            conn.send(encode_stat_request())
            assert conn.read_ack()["status"] == "ok"
            out = conn.collect()
        reply = out.controls[0]
        assert reply["repos_root"] == str(server.repos)
        assert reply["home"] == str(server.home)
        assert reply["staging"] == str(server.staging)
        assert reply["uid"] == os.getuid()
        assert reply["protocol"] == PROTOCOL_VERSION
        assert out.terminal["exit_code"] == 0

    def test_stat_reports_running_work_and_whether_the_backstop_is_there(
        self, server_factory
    ):
        """doctor's transport check is a ping and a stat. Both answered happily
        from a server that had lost track of a live build and from one running
        with no reaper behind it, which is what made the orphans invisible from
        every side."""
        srv = server_factory()
        with srv.connect() as conn:
            conn.send(encode_stat_request())
            assert conn.read_ack()["status"] == "ok"
            idle = conn.collect().controls[0]
        assert idle["process_groups"] == 0
        assert idle["reaper"] is True

        busy = srv.connect()
        try:
            busy.send(
                encode_exec_request(shell="echo up; sleep 300", cwd=str(srv.repos))
            )
            assert busy.read_ack()["status"] == "ok"
            decoder = FrameDecoder()
            deadline = time.monotonic() + READ_TIMEOUT
            started = False
            while not started and time.monotonic() < deadline:
                for stream, payload in decoder.feed(busy.sock.recv(65536)):
                    if stream == STREAM_STDOUT and payload.strip():
                        started = True
            assert started, "the fixture never started"
            with srv.connect() as conn:
                conn.send(encode_stat_request())
                assert conn.read_ack()["status"] == "ok"
                assert conn.collect().controls[0]["process_groups"] == 1
        finally:
            busy.close()


class TestTheReaperRecordStream:
    """The reaper's own logic, without a server around it.

    Every one of these is about a record whose group is *not* the one the
    record names any more — which is the only way a `killpg` from here can
    reach something it should not.
    """

    def test_a_record_is_added_and_removed_by_the_stream(self):
        module = _load_server_module()
        live: set[int] = set()
        for record in (b"+41", b"+42", b"-41"):
            module._apply_reaper_record(live, record)
        assert live == {42}

    def test_an_unreadable_record_is_dropped_rather_than_guessed_at(self):
        module = _load_server_module()
        live: set[int] = set()
        for record in (b"", b"41", b"+", b"+0", b"-0", b"+-1", b"+4 1", b"?41", b"+x"):
            module._apply_reaper_record(live, record)
        assert live == set()

    def test_a_group_that_is_already_gone_is_never_signalled(self, monkeypatch):
        """The whole anti-reuse story. The server sends `-<pgid>` just after it
        kills a group, so a record can outlive its group by a few instructions —
        and by then the pid is free for anything else to claim."""
        module = _load_server_module()
        killed = []
        monkeypatch.setattr(module, "group_alive", lambda pgid: pgid == 42)
        monkeypatch.setattr(
            module, "kill_group", lambda pgid, sig: killed.append((pgid, sig))
        )
        assert module._reap_abandoned_groups({41, 42}) == 0
        assert killed == [(42, signal.SIGKILL)]

    def test_a_survivor_of_sigkill_is_reported_rather_than_retried(
        self, monkeypatch, caplog
    ):
        module = _load_server_module()
        monkeypatch.setattr(module, "REAPER_VERIFY_SECONDS", 0.1)
        monkeypatch.setattr(module, "group_alive", lambda pgid: True)
        monkeypatch.setattr(module, "kill_group", lambda pgid, sig: None)
        with caplog.at_level("ERROR", logger=module.logger.name):
            module._reap_abandoned_groups({41})
        assert "still present after SIGKILL" in caplog.text

    def test_a_read_failure_exits_without_killing_anything(self, monkeypatch):
        """EOF is the server's death; a read *failure* is the reaper's own.

        Both used to `break` into the same reap, so a failing read SIGKILLed
        every live build under a perfectly healthy server — the thing this file
        exists to prevent, done by the thing meant to prevent it.
        """
        module = _load_server_module()
        killed = []
        monkeypatch.setattr(module, "group_alive", lambda pgid: True)
        monkeypatch.setattr(
            module, "kill_group", lambda pgid, sig: killed.append(pgid)
        )

        reads = iter([b"+41\n", OSError(5, "Input/output error")])

        def failing_read(fd, size):
            value = next(reads)
            if isinstance(value, OSError):
                raise value
            return value

        monkeypatch.setattr(module.os, "read", failing_read)
        assert module.run_reaper() == 0
        assert killed == [], "a read failure killed the groups it was tracking"

    def test_a_backlog_larger_than_one_read_loses_no_record(self, monkeypatch):
        """The cap used to be applied before the split, so it truncated the
        *head* of the buffer — where the partial record from the previous read
        lives. Measured, a legitimate backlog of 2000 records lost two of them,
        which is two builds the reaper would never kill."""
        module = _load_server_module()
        expected = list(range(9000, 11000))
        stream = b"".join(b"+%d\n" % pgid for pgid in expected)
        assert len(stream) > module.REAPER_MAX_BUFFER_BYTES, "not a real backlog"

        killed = []
        monkeypatch.setattr(module, "group_alive", lambda pgid: True)
        monkeypatch.setattr(
            module, "kill_group", lambda pgid, sig: killed.append(pgid)
        )
        chunks = iter(
            [stream[i : i + 4096] for i in range(0, len(stream), 4096)] + [b""]
        )
        monkeypatch.setattr(module.os, "read", lambda fd, size: next(chunks))

        module.run_reaper()

        assert sorted(killed) == expected

    def test_the_stream_ends_at_eof_and_reaps_what_it_was_told(
        self, monkeypatch, tmp_path
    ):
        """End to end through `run_reaper`'s own read loop, on a real pipe,
        with the write end closed the way the kernel closes it on a SIGKILL."""
        module = _load_server_module()
        killed = []
        monkeypatch.setattr(module, "group_alive", lambda pgid: pgid in (42, 43))
        monkeypatch.setattr(
            module, "kill_group", lambda pgid, sig: killed.append(pgid)
        )
        read_fd, write_fd = os.pipe()
        os.write(write_fd, b"+41\n+42\n-41\n+4")
        os.write(write_fd, b"3\n")
        saved = os.dup(0)
        try:
            os.dup2(read_fd, 0)
            os.close(read_fd)
            os.close(write_fd)
            assert module.run_reaper() == 0
        finally:
            os.dup2(saved, 0)
            os.close(saved)
        assert sorted(killed) == [42, 43]


class TestARecordIsReleasedWhenItsGroupGoes:
    def test_a_stalled_client_does_not_hold_a_record_past_its_group(
        self, server_factory
    ):
        """The record has to be released when the *group* empties, not when the
        connection ends.

        `_do_exec`'s `finally` runs after `_drain` — whose grace restarts while
        any pump is writing — and after the terminal frame's `writer.drain()`,
        which has no timeout. So a client that stops reading held the record for
        as long as it liked while the pgid was already free to be reused, and a
        SIGKILL in that state aimed the reaper's `killpg` at whatever now held
        the number. Measured at 15 seconds and still counting.
        """
        # A drain grace far longer than the assertion window, so the two cases
        # separate cleanly: with the record released on the group's exit this
        # reads 0 within a second, and without it the server holds the record
        # for the whole grace — which restarts while any pump is writing, so it
        # was measured still holding at 15s against the 5s default.
        srv = server_factory(drain_grace=30.0)
        stalled = srv.connect()
        try:
            # Sized so the command itself finishes — small enough to fit in the
            # pipe and socket buffers — while the server stays blocked writing
            # to a client that has stopped reading. Too much more and the child
            # blocks instead, which is a live group and a different test.
            stalled.send(
                encode_exec_request(
                    shell="head -c 100000 /dev/zero | tr '\\0' x", cwd=str(srv.repos)
                )
            )
            assert stalled.read_ack()["status"] == "ok"
            stalled.sock.recv(4096)

            deadline = time.monotonic() + 8.0
            groups = None
            while time.monotonic() < deadline:
                with srv.connect() as probe:
                    probe.send(encode_stat_request())
                    assert probe.read_ack()["status"] == "ok"
                    groups = probe.collect().controls[0]["process_groups"]
                if groups == 0:
                    break
                time.sleep(0.1)

            assert groups == 0, (
                "the server still holds a record for a group that has exited, "
                "so its pgid is free for reuse while the reaper would still "
                "signal it"
            )
        finally:
            stalled.close()


class TestTheReaperIsReportedLive:
    """`stat` answers whether the reaper is there now, not whether one started.

    It used to be a boolean set when the pipe was created and falsified only
    when the *next* command spawned. A reaper killed on its own therefore read
    as healthy for the whole idle window — which is the window doctor probes in,
    and this is not a hypothetical death: the reaper shares the server's cgroup,
    so the OOM killer the feature exists for can take it instead.
    """

    def _reaper_pid(self, srv) -> int:
        match = re.search(r"reaper started, pid (\d+)", srv.log_text())
        assert match, f"the server never logged a reaper pid: {srv.log_text()}"
        return int(match.group(1))

    def test_a_reaper_killed_on_its_own_is_reported_before_the_next_command(
        self, server_factory
    ):
        srv = server_factory()
        reaper = self._reaper_pid(srv)
        with srv.connect() as conn:
            conn.send(encode_stat_request())
            assert conn.read_ack()["status"] == "ok"
            assert conn.collect().controls[0]["reaper"] is True

        os.kill(reaper, signal.SIGKILL)
        gone_by = time.monotonic() + 10.0
        while time.monotonic() < gone_by and _alive(reaper):
            time.sleep(0.05)

        # No command in between: that is the whole point. The old field only
        # moved when `_send` failed, so this same sequence answered True.
        with srv.connect() as conn:
            conn.send(encode_stat_request())
            assert conn.read_ack()["status"] == "ok"
            assert conn.collect().controls[0]["reaper"] is False

    def test_the_server_still_runs_commands_with_no_reaper(self, server_factory):
        """Losing the backstop must not become an outage. A server that refused
        work without a reaper would turn a leak into a dead transport, which is
        strictly worse than the thing it protects against."""
        srv = server_factory()
        os.kill(self._reaper_pid(srv), signal.SIGKILL)
        gone_by = time.monotonic() + 10.0
        while time.monotonic() < gone_by and _alive(self._reaper_pid(srv)):
            time.sleep(0.05)

        out = srv.run(shell="echo still-serving", cwd=str(srv.repos))

        assert out.terminal["exit_code"] == 0
        assert b"still-serving" in out.stdout


class TestTheSocket:
    def test_the_socket_is_private_to_the_uid_that_owns_it(self, server):
        """Both sides run as the same uid (Design 6), so 0600 needs no group
        ceremony — and 0666 would open the transport to anything in the
        container."""
        assert os.stat(server.socket_path).st_mode & 0o777 == 0o600

    def test_a_stale_socket_file_is_replaced_rather_than_fatal(self, server_factory):
        """The supervisor restarts the server every second on a crash loop; a
        leftover inode must not turn that into a permanent outage. A clean
        SIGTERM unlinks the socket on the way out, so the case worth testing is
        the one where it never got the chance."""
        srv = server_factory()
        srv.proc.kill()
        srv.proc.wait(timeout=10)
        assert os.path.exists(srv.socket_path), "nothing stale was left to clear"
        second = subprocess.Popen(
            [
                sys.executable,
                str(SERVER),
                "--socket",
                srv.socket_path,
                "--repos-root",
                str(srv.repos),
                "--home",
                str(srv.home),
                "--staging",
                str(srv.staging),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            deadline = time.monotonic() + 20.0
            while time.monotonic() < deadline:
                try:
                    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    probe.settimeout(2.0)
                    probe.connect(srv.socket_path)
                    probe.close()
                    break
                except OSError:
                    time.sleep(0.02)
            else:  # pragma: no cover
                raise AssertionError("the restarted server never listened")
        finally:
            second.terminate()
            second.wait(timeout=10)


class TestTheScriptItself:
    def test_it_has_a_shebang_and_is_executable(self):
        """It is installed as a container `command` and run by the kernel under
        its own name; without a shebang that is ENOEXEC."""
        assert SERVER.read_bytes().startswith(b"#!/usr/bin/env python3\n")
        assert os.access(SERVER, os.X_OK)

    def test_it_imports_nothing_from_istota(self):
        """It runs in a container that has no istota package. The one module it
        needs travels as the vendored copy."""
        offenders = [
            line
            for line in SERVER.read_text().splitlines()
            if re.match(r"\s*(from|import)\s+istota(\.|\s|$)", line)
        ]
        assert offenders == []

    def test_the_defaults_are_the_ones_the_deployment_relies_on(self):
        module = _load_server_module()
        assert module.DEFAULT_IDLE_TIMEOUT_SECONDS == 3600.0
        assert module.KILL_GRACE_SECONDS == 5.0
        assert module.DEFAULT_HOME == "/home/dev"
        assert module.DEFAULT_STAGING == "/home/dev/.istota-exec"
        assert module.REFUSED_BY_NAME == ("/run/istota-cred", "/run/istota-exec")

    def test_a_root_that_is_not_an_absolute_path_is_refused_rather_than_answered(
        self,
    ):
        """`os.path.realpath("")` is the process's own working directory, and
        `is_under(path, "")` is True for every absolute path. An unset variable
        in the supervisor's command line would otherwise scope every exec to
        wherever the server happened to be started, silently."""
        module = _load_server_module()
        for bad in ("", "repos", "./repos"):
            with pytest.raises(ValueError):
                module.is_under("/etc/shadow", bad)
            with pytest.raises(ValueError):
                module.Roots(bad, "/home/dev", "/home/dev/.istota-exec")

    def test_the_server_refuses_to_start_on_a_relative_root(self, tmp_path):
        done = subprocess.run(
            [
                sys.executable,
                str(SERVER),
                "--socket",
                str(tmp_path / "exec.sock"),
                "--repos-root",
                "repos",
            ],
            capture_output=True,
            timeout=30,
            text=True,
        )
        assert done.returncode != 0
        assert "absolute path" in (done.stderr + done.stdout)

    def test_the_default_working_directory_must_exist_and_must_not_have_moved(
        self, tmp_path
    ):
        """`serve()` only warns when --home is missing, so the branch is live on
        a deployment whose home was never created or whose bind mount failed.
        And the default is admitted on the strength of being this process's own
        constant, so a symlink dropped over it afterwards has to be refused
        rather than followed — otherwise that claim stops being true."""
        module = _load_server_module()
        home = tmp_path / "home"
        elsewhere = tmp_path / "elsewhere"
        home.mkdir()
        elsewhere.mkdir()
        roots = module.Roots(str(tmp_path), str(home), str(home / ".istota-exec"))
        assert roots.default_cwd() == str(home.resolve())

        home.rmdir()
        with pytest.raises(module.ProtocolError) as gone:
            roots.default_cwd()
        assert gone.value.code == ERR_NO_SUCH_CWD

        home.symlink_to(elsewhere)
        with pytest.raises(module.ProtocolError) as moved:
            roots.default_cwd()
        assert moved.value.code == ERR_PATH_REFUSED

    def test_the_root_test_is_a_prefix_test_on_components(self):
        """`/srv/repos/alice-evil` is not under `/srv/repos/alice`, and a naive
        startswith says it is."""
        module = _load_server_module()
        assert module.is_under("/srv/repos/alice/x", "/srv/repos/alice")
        assert module.is_under("/srv/repos/alice", "/srv/repos/alice")
        assert not module.is_under("/srv/repos/alice-evil", "/srv/repos/alice")
        assert not module.is_under("/srv/repos", "/srv/repos/alice")


def _load_server_module():
    """Import the server as a module.

    It ships without a `.py` suffix because it is installed as a command, so
    `spec_from_file_location` needs to be told which loader to use.
    """
    import importlib.machinery
    import importlib.util

    loader = importlib.machinery.SourceFileLoader("istota_exec_serve", str(SERVER))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


def test_the_terminal_frame_is_the_last_thing_on_the_connection(server):
    """A client stops reading at the terminal frame, so anything the server
    sends after one is invisible."""
    out = server.run(argv=["sh", "-c", "echo hi"], cwd=str(server.repos))
    assert out.terminal is out.controls[-1]
    assert out.stdout == b"hi\n"


class TestTheDoctorProbeSpeaksToTheRealServer:
    """`doctor`'s transport probe, against this server rather than a stub.

    The four `developer.container` checks are otherwise exercised against a
    monkeypatched `_exec_transport_request`, which proves the reporting and
    nothing about the wire. That is the shape of vacuous assertion this
    subsystem keeps producing — a probe whose success is indistinguishable from
    a no-op — so the framing, the acknowledgement parse and the control-frame
    walk are held against the real server here, where the harness already is.
    """

    def test_a_ping_comes_back_with_a_pong(self, server):
        from istota import doctor

        frames, error = doctor._exec_transport_request(
            Path(server.socket_path), encode_ping_request(), 10.0
        )

        assert error == ""
        assert any(frame.get("pong") is True for frame in frames)
        assert is_terminal(frames[-1])

    def test_a_stat_carries_the_uid_and_the_repos_root(self, server):
        """What the identity check compares. A server answering with neither
        would make that check pass by producing no findings."""
        from istota import doctor

        frames, error = doctor._exec_transport_request(
            Path(server.socket_path), encode_stat_request(), 10.0
        )

        assert error == ""
        stat = next(f for f in frames if "uid" in f)
        assert stat["uid"] == os.getuid()
        assert stat["repos_root"] == str(server.repos)

    def test_an_exec_status_comes_back(self, server):
        """The uv-cache check reads a `test -d` exit code off the terminal
        frame, so a probe that could not distinguish 0 from 1 would report every
        deployment's cache mount as present."""
        from istota import doctor

        for command, expected in ((["true"], 0), (["false"], 1)):
            frames, error = doctor._exec_transport_request(
                Path(server.socket_path),
                encode_exec_request(argv=command, cwd=None, stdin=False, timeout=5),
                10.0,
            )
            assert error == ""
            assert frames[-1]["exit_code"] == expected

    def test_a_refusal_is_reported_rather_than_read_as_silence(self, server):
        """An error acknowledgement closes the connection with nothing streamed
        behind it. Reading that as "no frames" would make a refused probe
        indistinguishable from a healthy one that said nothing."""
        from istota import doctor

        frames, error = doctor._exec_transport_request(
            Path(server.socket_path),
            encode_exec_request(
                argv=["true"], cwd=str(server.outside), stdin=False, timeout=5
            ),
            10.0,
        )

        assert frames == []
        assert ERR_PATH_REFUSED in error

    def test_a_socket_nobody_is_serving_is_reported_not_raised(self, tmp_path):
        """Doctor runs on the daemon's start-up path; an exception there turns a
        diagnostic into an outage."""
        from istota import doctor

        frames, error = doctor._exec_transport_request(
            tmp_path / "absent.sock", encode_ping_request(), 1.0
        )

        assert frames == []
        assert "could not connect" in error

    def test_the_checks_come_back_green_against_it(self, server, tmp_path):
        """End to end: the four checks, over the real transport, with no
        monkeypatching anywhere. `repos_root` has to agree, which is what makes
        the identity check's OK mean something."""
        from istota import doctor
        from istota.config import (
            Config,
            ContainerConfig,
            DeveloperConfig,
            DevboxConfig,
            UserConfig,
        )

        # The server's own roots, spelled as the daemon would derive them:
        # `{repos_dir}/{user}` and `{exec_socket_dir}/{user}/exec.sock`.
        user = server.repos.name
        config = Config(
            db_path=tmp_path / "test.db",
            developer=DeveloperConfig(
                enabled=True,
                repos_dir=str(server.repos.parent),
                container=ContainerConfig(
                    exec_socket_dir=str(Path(server.socket_path).parent.parent),
                ),
            ),
            devbox=DevboxConfig(enabled=True),
            users={user: UserConfig(display_name=user)},
        )
        # `_start_server` names the socket directory `sock`, not the user, so
        # the derived path has to be made to match rather than assumed.
        derived = Path(server.socket_path).parent.parent / user
        derived.mkdir(exist_ok=True)
        link = derived / "exec.sock"
        if not link.exists():
            os.symlink(server.socket_path, link)

        results = {r.name: r for r in doctor.check_developer_container(config, probe=True)}

        assert results["developer.container.transport"].status == "ok", (
            results["developer.container.transport"].detail
        )
        assert results["developer.container.identity"].status == "ok", (
            results["developer.container.identity"].detail
        )


class TestAShimReachesThisServerEndToEnd:
    """The whole chain minus Docker: shim → client → server → real exit status.

    Every other test of this feature holds one link. `test_developer_shims.py`
    asserts on the text of a shim, `test_devbox_exec_client.py` drives the
    client directly, and this file drives the wire — so the thing nobody
    exercised was the join, which is where a quoting mistake or a wrong flag
    name lives. The staging hand-verification is what covers the Docker half;
    this covers everything under it, on the host, with no container.

    `sh` stands in for `npm` because a shim's name is only a filename and `sh`
    exists on every machine the suite runs on. Nothing in the shim, the client
    or the server treats one command differently from another.
    """

    @pytest.fixture
    def shimmed(self, server, tmp_path):
        """`setup_env`'s output, wired to this server's socket."""
        from istota import db
        from istota.config import (
            Config,
            ContainerConfig,
            DeveloperConfig,
            DevboxConfig,
            SecurityConfig,
        )
        from istota.skills.developer import setup_env

        user = server.repos.name
        socket_parent = Path(server.socket_path).parent.parent
        # The daemon derives `{exec_socket_dir}/{user}/exec.sock`; the harness
        # names the directory `sock`. Make the derived path real rather than
        # assuming the two agree.
        derived = socket_parent / user
        derived.mkdir(exist_ok=True)
        link = derived / "exec.sock"
        if not link.exists():
            os.symlink(server.socket_path, link)

        config = Config(
            db_path=tmp_path / "test.db",
            developer=DeveloperConfig(
                enabled=True,
                repos_dir=str(server.repos.parent),
                container=ContainerConfig(
                    exec_socket_dir=str(socket_parent),
                    shim_commands=["sh"],
                ),
            ),
            devbox=DevboxConfig(enabled=True),
            security=SecurityConfig(skill_proxy_enabled=False),
        )

        class _Ctx:
            pass

        user_temp = server.base / "task-temp"
        user_temp.mkdir(exist_ok=True)
        ctx = _Ctx()
        ctx.config = config
        ctx.user_temp_dir = str(user_temp)
        ctx.task = db.Task(
            id=1, prompt="p", user_id=user, source_type="talk", status="running",
        )
        setup_env(ctx)
        return user_temp / ".developer" / "exec-shims" / "sh"

    def _run(self, shim, *args, cwd, stdin=None):
        return subprocess.run(
            [str(shim), *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            input=stdin,
            timeout=READ_TIMEOUT,
            check=False,
        )

    def test_the_command_runs_and_its_status_comes_back(self, shimmed, server):
        """The measurement this whole transport exists for: `docker exec`
        through the API proxy runs the command and loses its status."""
        work = server.repos / "project"
        work.mkdir()

        assert self._run(shimmed, "-c", "exit 0", cwd=work).returncode == 0
        assert self._run(shimmed, "-c", "exit 7", cwd=work).returncode == 7
        assert self._run(shimmed, "-c", "false", cwd=work).returncode == 1

    def test_output_comes_back_on_the_right_stream(self, shimmed, server):
        work = server.repos / "project"
        work.mkdir()

        result = self._run(shimmed, "-c", "echo out; echo err >&2", cwd=work)

        assert result.stdout == "out\n"
        assert "err" in result.stderr

    def test_the_working_directory_travels(self, shimmed, server):
        """The shim sends no `--cwd`; the client takes the *physical* directory
        from `os.getcwd()`, which is what the server's realpath check wants."""
        work = server.repos / "project" / "web"
        work.mkdir(parents=True)

        result = self._run(shimmed, "-c", "pwd", cwd=work)

        assert result.stdout.strip() == str(work)

    def test_a_pipeline_into_a_shimmed_command_works(self, shimmed, server):
        """The case an earlier draft broke silently: with `stdin` never set, the
        producer takes SIGPIPE and, under the Bash tool's pipefail, the whole
        pipeline comes back 141."""
        work = server.repos / "project"
        work.mkdir()

        result = self._run(shimmed, "-c", "cat", cwd=work, stdin="hello\n")

        assert result.returncode == 0
        assert result.stdout == "hello\n"

    def test_an_argument_with_a_space_and_a_glob_arrives_whole(self, shimmed, server):
        """`argv`, never `shell`, so no quoting bug can be introduced between
        the model's shell and the container."""
        work = server.repos / "project"
        work.mkdir()

        result = self._run(
            shimmed, "-c", 'printf "%s\\n" "$@"', "sh", "a b", "*.js", cwd=work
        )

        assert result.stdout == "a b\n*.js\n"

    def test_a_working_directory_outside_the_root_is_refused_loudly(
        self, shimmed, server
    ):
        """The case that used to run silently in the container's own `/tmp`:
        `/tmp`, `/home/…` and `/usr/src` all exist in both namespaces and mean
        different things, and a shim's cwd is *inherited* rather than chosen."""
        result = self._run(shimmed, "-c", "exit 0", cwd=server.outside)

        assert result.returncode != 0
        assert "istota-devbox-exec" in result.stderr

    def test_a_dead_socket_exits_120_and_names_it(self, shimmed, server):
        """The refusal a task gets when the container is down, and the one a
        task that is not authorized for the developer skill gets on every
        shimmed command — the same class of loud, immediate failure a host-side
        `npm ci` gets from the CONNECT proxy today."""
        work = server.repos / "project"
        work.mkdir()
        # The server unlinks its own socket on a clean stop, which is what a
        # stopped container leaves behind too.
        _stop_server(server)
        assert not os.path.exists(server.socket_path)

        result = self._run(shimmed, "-c", "exit 0", cwd=work)

        assert result.returncode == 120
        assert "exec.sock" in result.stderr
