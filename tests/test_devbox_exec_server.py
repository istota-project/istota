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

import json
import os
import re
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

    log = base / "server.log"
    child_env = dict(os.environ)
    child_env.pop("ISTOTA_EXEC_TEST_MARKER", None)
    if env:
        child_env.update(env)
    with open(log, "wb") as handle:
        proc = subprocess.Popen(argv, stdout=handle, stderr=subprocess.STDOUT, env=child_env)

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
        """Design 3 scopes cwd to the repos root alone. /home/dev is a file root
        so the staging path can be written; it is not somewhere a command runs,
        because a passed-through cwd that exists in both namespaces is the case
        that used to run silently in the container's own /tmp."""
        ack = server.refusal(encode_exec_request(argv=["pwd"], cwd=str(server.home)))
        assert ack["code"] == ERR_PATH_REFUSED

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
        assert ERR_PATH_REFUSED not in ack["code"]


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

    def test_a_missing_binary_is_spawn_failed_and_streams_nothing(self, server):
        ack = server.refusal(
            encode_exec_request(
                argv=["istota-no-such-binary-42"], cwd=str(server.repos)
            )
        )
        assert ack["code"] == ERR_SPAWN_FAILED
        assert "istota-no-such-binary-42" in ack["message"]

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


def test_the_json_the_server_writes_is_the_json_the_protocol_decodes(server):
    """A last guard against a server that hand-rolls its own framing: every
    control frame it sent parses as JSON and nothing arrived on stream 0."""
    out = server.run(argv=["sh", "-c", "echo hi"], cwd=str(server.repos))
    assert all(isinstance(json.dumps(c), str) for c in out.controls)
    assert out.terminal is out.controls[-1]
