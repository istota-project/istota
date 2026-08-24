"""The devbox exec client, as a shim runs it: a subprocess, against a real server.

``src/istota/devbox_exec_client.py`` is what every shimmed command becomes, so
the thing under test is the *process* — its exit status, its stdout and stderr
bytes, and what it says when the transport fails. Calling ``main()`` in-process
would test none of that, because the status a shim reports is a wait status and
not a return value.

The server here is the real one, started by ``test_devbox_exec_server``'s own
harness. Two cases the real server cannot be driven into get a scripted socket
instead, and both are the point of the stage: an acknowledgement carrying a
protocol number this client does not know, and an acknowledgement followed by a
close with no terminal frame — the 123 case, which a naive client reports as 0.

Read the ``tests/test_devbox_exec_client.py`` bullet in the spec's Test strategy
alongside this file; each assertion there is a named test here.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from istota.devbox_exec_protocol import (
    PROTOCOL_VERSION,
    SIGPIPE_EXIT,
    STREAM_CONTROL,
    STREAM_STDIN,
    STREAM_STDOUT,
    encode_ack_error,
    encode_ack_ok,
    encode_control,
    encode_line,
    pack_frame,
)

from tests.test_devbox_exec_server import _start_server, _stop_server

CLIENT = Path(__file__).resolve().parents[1] / "src/istota/devbox_exec_client.py"
PROTOCOL = Path(__file__).resolve().parents[1] / "src/istota/devbox_exec_protocol.py"

EXIT_NO_CONNECT = 120
EXIT_UNSUPPORTED_PROTOCOL = 121
EXIT_PROTOCOL_ERROR = 122
EXIT_CONNECTION_LOST = 123

# Long enough that a loaded machine does not fail a status assertion, short
# enough that a hang is a failure rather than a wait for pytest's own timeout.
RUN_TIMEOUT = 60.0


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


def run_client(
    socket_path: str,
    *command: str,
    cwd: str | Path | None = None,
    options: tuple[str, ...] = (),
    stdin_data: bytes | None = None,
    env: dict[str, str] | None = None,
    client: Path | None = None,
    timeout: float = RUN_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run the client the way a shim does, and hand back the wait status."""
    argv = [
        sys.executable,
        str(client or CLIENT),
        "--socket",
        socket_path,
        *options,
        "--",
        *command,
    ]
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd is not None else None,
        input=stdin_data,
        stdin=subprocess.DEVNULL if stdin_data is None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
        timeout=timeout,
    )


class FakeServer:
    """A socket that answers by script, for the shapes the real server cannot make.

    Deliberately narrow: it reads the request line, hands it to a responder and
    closes. Everything a well-behaved server does is tested against the real
    one; this exists for the malformed and the truncated.
    """

    def __init__(self, path: str, responder) -> None:
        self.path = path
        self.requests: list[bytes] = []
        self._responder = responder
        self._sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._sock.bind(path)
        self._sock.listen(8)
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return
            try:
                conn.settimeout(RUN_TIMEOUT)
                line = b""
                while not line.endswith(b"\n"):
                    chunk = conn.recv(4096)
                    if not chunk:
                        break
                    line += chunk
                self.requests.append(line)
                self._responder(conn, line)
            except OSError:
                pass
            finally:
                try:
                    conn.close()
                except OSError:
                    pass

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        try:
            os.unlink(self.path)
        except OSError:
            pass


@pytest.fixture
def server():
    srv = _start_server()
    try:
        yield srv
    finally:
        _stop_server(srv)


@pytest.fixture
def server_factory():
    started = []

    def make(**kwargs):
        srv = _start_server(**kwargs)
        started.append(srv)
        return srv

    try:
        yield make
    finally:
        for srv in started:
            _stop_server(srv)


@pytest.fixture
def fake_server():
    """A scripted socket, and a directory short enough to bind one in."""
    base = Path(tempfile.mkdtemp(dir="/tmp", prefix="istota-exec-fake-")).resolve()
    made: list[FakeServer] = []

    def make(responder) -> FakeServer:
        srv = FakeServer(str(base / f"fake-{len(made)}.sock"), responder)
        made.append(srv)
        return srv

    try:
        yield make
    finally:
        for srv in made:
            srv.close()
        shutil.rmtree(base, ignore_errors=True)


# --------------------------------------------------------------------------- #
# The status a shim reports
# --------------------------------------------------------------------------- #


class TestTheExitStatusReachesTheShim:
    """The measurement the transport exists for, carried one step further than
    `test_devbox_exec_server` takes it: the server reporting a status in a
    control frame is worth nothing unless the client exits with it."""

    @pytest.mark.parametrize("script,expected", [("exit 0", 0), ("exit 1", 1), ("exit 7", 7)])
    def test_a_commands_own_status_is_the_clients(self, server, script, expected):
        done = run_client(server.socket_path, "sh", "-c", script, cwd=server.repos)
        assert done.returncode == expected

    def test_a_signalled_child_arrives_as_128_plus_the_signal(self, server):
        done = run_client(
            server.socket_path, "sh", "-c", "kill -TERM $$", cwd=server.repos
        )
        assert done.returncode == 143

    def test_a_pipefail_141_arrives_with_the_note_that_explains_it(self, server):
        """The client passes `note` on rather than swallowing it: 141 is the one
        status with a fixed meaning, and this subsystem has three
        wrong-exit-status bugs behind it."""
        done = run_client(
            server.socket_path,
            "bash",
            "-o",
            "pipefail",
            "-c",
            "yes | head -1",
            cwd=server.repos,
        )
        assert done.returncode == SIGPIPE_EXIT
        assert b"istota-devbox-exec:" in done.stderr
        assert b"SIGPIPE" in done.stderr

    def test_stdout_and_stderr_land_on_their_own_descriptors(self, server):
        done = run_client(
            server.socket_path,
            "sh",
            "-c",
            "echo to-stdout; echo to-stderr >&2",
            cwd=server.repos,
        )
        assert done.stdout == b"to-stdout\n"
        assert done.stderr == b"to-stderr\n"
        assert done.returncode == 0

    def test_two_megabytes_of_stdout_arrive_whole(self, server):
        size = 2 * 1024 * 1024
        done = run_client(
            server.socket_path,
            sys.executable,
            "-c",
            f"import sys; sys.stdout.buffer.write(b'x' * {size})",
            cwd=server.repos,
        )
        assert done.stdout == b"x" * size
        assert done.returncode == 0

    def test_binary_output_survives_the_client(self, server):
        done = run_client(
            server.socket_path,
            sys.executable,
            "-c",
            "import sys; sys.stdout.buffer.write(bytes(range(256)))",
            cwd=server.repos,
        )
        assert done.stdout == bytes(range(256))

    def test_a_missing_binary_is_a_refusal_and_never_a_status(self, server):
        """`spawn_failed` arrives in the acknowledgement, so nothing ran. 120,
        not 127: 127 is the status the model reads most often and the shell's
        own "command not found" already owns it."""
        done = run_client(server.socket_path, "istota-no-such-binary-42", cwd=server.repos)
        assert done.returncode == EXIT_NO_CONNECT
        assert b"spawn_failed" in done.stderr
        assert b"istota-no-such-binary-42" in done.stderr

    def test_a_server_side_timeout_says_why_it_killed_the_command(self, server):
        done = run_client(
            server.socket_path,
            "sleep",
            "30",
            cwd=server.repos,
            options=("--timeout", "1"),
        )
        assert done.returncode == 137
        assert b"reason: timeout" in done.stderr


# --------------------------------------------------------------------------- #
# The working directory
# --------------------------------------------------------------------------- #


class TestTheWorkingDirectoryIsPhysical:
    """Design 5: `os.getcwd()` and never `$PWD`. `$PWD` is the *logical* path
    the parent shell recorded, and the server's containment test is a
    `realpath`, so the two can name different directories."""

    def _marked(self, server) -> tuple[Path, Path]:
        real = server.repos / "real" / "sub"
        real.mkdir(parents=True)
        (real / "marker.txt").write_text("physical\n")
        decoy = server.repos / "decoy"
        decoy.mkdir()
        (decoy / "marker.txt").write_text("logical\n")
        return real, decoy

    def test_pwd_is_not_consulted(self, server):
        """The one assertion that discriminates: stand in one directory and
        claim another in `$PWD`. A client reading the environment runs in the
        decoy, and both directories pass the server's root test, so nothing
        else about the request would look wrong."""
        real, decoy = self._marked(server)
        env = dict(os.environ)
        env["PWD"] = str(decoy)
        done = run_client(
            server.socket_path, "cat", "marker.txt", cwd=real, env=env
        )
        assert done.stdout == b"physical\n"
        assert done.returncode == 0

    def test_a_directory_reached_through_a_symlink_is_sent_resolved(
        self, server, fake_server
    ):
        """`cd` through a symlink is where the two spellings diverge without
        anybody doing anything strange.

        Asserted against the *request line*, because the real server is not a
        witness for this one: `check_cwd` resolves the path before it logs or
        runs anything, so both spellings produce identical output and an
        identical log line. What differs is only what went on the wire.
        """
        real, _ = self._marked(server)
        link = server.repos / "link"
        link.symlink_to(real.parent)
        srv = fake_server(_ack_then_exit_zero)
        env = dict(os.environ)
        env["PWD"] = str(link / "sub")
        done = run_client(srv.path, "cat", "marker.txt", cwd=link / "sub", env=env)
        assert done.returncode == 0
        sent = json.loads(srv.requests[0])
        assert sent["cwd"] == str(real)
        assert str(link) not in sent["cwd"]

    def test_cwd_overrides_it_for_the_devbox_skill(self, server):
        """`--cwd` stays on the client for the caller that was *told* about a
        directory rather than standing in one."""
        real, decoy = self._marked(server)
        done = run_client(
            server.socket_path,
            "cat",
            "marker.txt",
            cwd=real,
            options=("--cwd", str(decoy)),
        )
        assert done.stdout == b"logical\n"

    def test_a_deleted_working_directory_is_loud_and_never_reaches_the_wire(
        self, server
    ):
        """`os.getcwd()` raises here where `$PWD` would have sent a dead string
        for the server to have a rule about."""
        gone = server.repos / "gone"
        gone.mkdir()
        script = (
            f'cd "{gone}" && rmdir "{gone}" && '
            f'exec "{sys.executable}" "{CLIENT}" --socket "{server.socket_path}" -- true'
        )
        done = subprocess.run(
            ["sh", "-c", script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=RUN_TIMEOUT,
        )
        assert done.returncode == EXIT_NO_CONNECT
        assert b"istota-devbox-exec:" in done.stderr
        assert b"working directory" in done.stderr

    def test_a_directory_outside_the_repos_root_is_refused_before_anything_runs(
        self, server
    ):
        done = run_client(server.socket_path, "true", cwd=server.outside)
        assert done.returncode == EXIT_NO_CONNECT
        assert b"path_refused" in done.stderr


# --------------------------------------------------------------------------- #
# Stdin
# --------------------------------------------------------------------------- #


class TestStdin:
    def test_it_is_forwarded_when_asked_for(self, server):
        done = run_client(
            server.socket_path,
            "cat",
            cwd=server.repos,
            options=("--stdin",),
            stdin_data=b"hello devbox\n",
        )
        assert done.stdout == b"hello devbox\n"
        assert done.returncode == 0

    def test_more_than_one_frame_of_it_survives(self, server):
        payload = bytes(range(256)) * 1024  # 256 KiB, four chunks
        done = run_client(
            server.socket_path,
            "cat",
            cwd=server.repos,
            options=("--stdin",),
            stdin_data=payload,
        )
        assert done.stdout == payload

    def test_the_command_sees_eof_when_it_was_not_asked_for(self, server):
        """`cat` with an inherited descriptor blocks forever; the request never
        asked for stdin, so the child gets a closed pipe."""
        done = run_client(
            server.socket_path, "cat", cwd=server.repos, stdin_data=b"ignored"
        )
        assert done.stdout == b""
        assert done.returncode == 0

    def test_a_command_that_closes_its_stdin_early_still_reports_its_status(
        self, server
    ):
        """`head -c 5` stops reading while the client is still sending. The
        broken pipe is the child's business, not a transport failure."""
        done = run_client(
            server.socket_path,
            "head",
            "-c",
            "5",
            cwd=server.repos,
            options=("--stdin",),
            stdin_data=b"y" * (512 * 1024),
        )
        assert done.returncode == 0
        assert done.stdout == b"yyyyy"


# --------------------------------------------------------------------------- #
# The four transport codes
# --------------------------------------------------------------------------- #


def _ack_then_exit_zero(conn: socket.socket, line: bytes) -> None:
    conn.sendall(encode_ack_ok())
    conn.sendall(encode_control({"exit_code": 0, "signal": None}))


def _ack_then_close(conn: socket.socket, line: bytes) -> None:
    conn.sendall(encode_ack_ok())


def _ack_then_partial_output_then_close(conn: socket.socket, line: bytes) -> None:
    conn.sendall(encode_ack_ok())
    conn.sendall(pack_frame(STREAM_STDOUT, b"half a build\n"))


class TestTheFourTransportCodes:
    """Each is a status outside the range a command produces, and each says one
    line prefixed `istota-devbox-exec:`. 125/126/127 are deliberately unused:
    they collide with docker's own status and with the shell's."""

    def test_120_when_there_is_no_socket_to_connect_to(self, tmp_path):
        missing = str(tmp_path / "nothing.sock")
        done = run_client(missing, "true")
        assert done.returncode == EXIT_NO_CONNECT
        assert done.stderr.startswith(b"istota-devbox-exec: ")
        assert missing.encode() in done.stderr
        assert len(done.stderr.strip().splitlines()) == 1

    def test_120_when_the_server_refuses_in_the_acknowledgement(self, fake_server):
        srv = fake_server(
            lambda conn, line: conn.sendall(
                encode_ack_error("path_refused", "/etc is not under the repos root")
            )
        )
        done = run_client(srv.path, "true")
        assert done.returncode == EXIT_NO_CONNECT
        assert b"path_refused" in done.stderr

    def test_121_on_a_protocol_number_this_client_does_not_know(self, fake_server):
        srv = fake_server(
            lambda conn, line: conn.sendall(
                encode_line({"status": "ok", "protocol": PROTOCOL_VERSION + 98})
            )
        )
        done = run_client(srv.path, "true")
        assert done.returncode == EXIT_UNSUPPORTED_PROTOCOL
        assert b"protocol" in done.stderr

    def test_121_and_not_1_when_the_number_is_a_bool(self, fake_server):
        """`True == 1` in Python. An acknowledgement carrying `"protocol": true`
        is a malformed one, not protocol 1."""
        srv = fake_server(
            lambda conn, line: conn.sendall(
                encode_line({"status": "ok", "protocol": True})
            )
        )
        done = run_client(srv.path, "true")
        assert done.returncode == EXIT_UNSUPPORTED_PROTOCOL

    def test_122_on_an_acknowledgement_that_is_not_json(self, fake_server):
        srv = fake_server(lambda conn, line: conn.sendall(b"not json at all\n"))
        done = run_client(srv.path, "true")
        assert done.returncode == EXIT_PROTOCOL_ERROR

    def test_122_when_the_connection_closes_before_any_acknowledgement(
        self, fake_server
    ):
        srv = fake_server(lambda conn, line: None)
        done = run_client(srv.path, "true")
        assert done.returncode == EXIT_PROTOCOL_ERROR
        assert b"acknowledg" in done.stderr

    def test_122_on_a_frame_header_naming_a_stream_that_does_not_exist(
        self, fake_server
    ):
        def respond(conn, line):
            conn.sendall(encode_ack_ok())
            conn.sendall(b"\x07\x00\x00\x00\x00\x00\x00\x01x")

        srv = fake_server(respond)
        done = run_client(srv.path, "true")
        assert done.returncode == EXIT_PROTOCOL_ERROR

    def test_122_when_the_server_sends_a_stream_that_travels_the_other_way(
        self, fake_server
    ):
        def respond(conn, line):
            conn.sendall(encode_ack_ok())
            conn.sendall(pack_frame(STREAM_STDIN, b"stdin comes from us"))

        srv = fake_server(respond)
        done = run_client(srv.path, "true")
        assert done.returncode == EXIT_PROTOCOL_ERROR

    def test_122_on_an_exit_status_no_process_could_have_produced(self, fake_server):
        def respond(conn, line):
            conn.sendall(encode_ack_ok())
            conn.sendall(encode_control({"exit_code": 4096, "signal": None}))

        srv = fake_server(respond)
        done = run_client(srv.path, "true")
        assert done.returncode == EXIT_PROTOCOL_ERROR

    def test_123_when_the_server_acknowledges_and_then_closes(self, fake_server):
        """The stage's whole point. An ack means the command is running, so a
        close with no terminal frame is the one case with no exit status at all
        — a container restart, or the container itself OOM-killed mid-build.
        A client that treats "the connection ended" as "nothing more to read"
        reports 0 here, which is the silent green this transport exists against.
        """
        srv = fake_server(_ack_then_close)
        done = run_client(srv.path, "true")
        assert done.returncode == EXIT_CONNECTION_LOST
        assert done.returncode != 0
        assert done.stderr.startswith(b"istota-devbox-exec: ")

    def test_123_even_when_output_arrived_first(self, fake_server):
        """Output is not evidence of completion. A build that printed 400 lines
        and then lost its container has no status either."""
        srv = fake_server(_ack_then_partial_output_then_close)
        done = run_client(srv.path, "true")
        assert done.stdout == b"half a build\n"
        assert done.returncode == EXIT_CONNECTION_LOST

    def test_123_when_the_server_reports_no_exit_status_at_all(self, fake_server):
        """The server sends a null `exit_code` rather than inventing 1 for its
        own faults. The client must not read the null as success either, and it
        folds the reason into the one line the contract promises."""

        def respond(conn, line):
            conn.sendall(encode_ack_ok())
            conn.sendall(
                encode_control(
                    {
                        "exit_code": None,
                        "signal": None,
                        "error": "internal",
                        "message": "process group 41 did not exit after SIGKILL",
                    }
                )
            )

        srv = fake_server(respond)
        done = run_client(srv.path, "true")
        assert done.returncode == EXIT_CONNECTION_LOST
        assert b"did not exit after SIGKILL" in done.stderr
        assert len(done.stderr.strip().splitlines()) == 1

    def test_123_when_the_server_accepts_and_never_acknowledges(self, fake_server):
        """A socket that answers nothing is reachable: the socket directory is
        writable by a container root, which may unlink the server's inode and
        bind its own. Unbounded, that hangs the shim for the whole task."""
        srv = fake_server(lambda conn, line: time.sleep(30))
        started = time.monotonic()
        done = run_client(
            srv.path, "true", options=("--connect-timeout", "1"), timeout=25
        )
        assert done.returncode == EXIT_CONNECTION_LOST
        assert b"did not acknowledge" in done.stderr
        assert time.monotonic() - started < 20

    def test_123_against_a_real_server_that_dies_mid_command(self, server_factory):
        """What a container restart looks like from the client's side, with a
        real server and a real command that outlives it."""
        srv = server_factory()
        argv = [
            sys.executable,
            str(CLIENT),
            "--socket",
            srv.socket_path,
            "--",
            "sleep",
            "20",
        ]
        client = subprocess.Popen(
            argv,
            cwd=str(srv.repos),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        try:
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline:
                if "exec sleep" in srv.log_text():
                    break
                time.sleep(0.05)
            else:  # pragma: no cover - the command never started
                raise AssertionError(f"the server never ran it: {srv.log_text()}")
            srv.proc.send_signal(signal.SIGKILL)
            _, stderr = client.communicate(timeout=RUN_TIMEOUT)
        finally:
            if client.poll() is None:  # pragma: no cover
                client.kill()
                client.communicate()
        assert client.returncode == EXIT_CONNECTION_LOST
        assert b"istota-devbox-exec:" in stderr


# --------------------------------------------------------------------------- #
# What the terminal frame carries besides a number
# --------------------------------------------------------------------------- #


class TestTheTerminalFrameIsPassedOn:
    def test_truncated_output_is_reported_rather_than_swallowed(self, server_factory):
        """Stage 1 could assert the server *sets* `truncated`; only a client can
        assert somebody is told. A process that `setsid`s out of the group holds
        the descriptor past the drain, and reporting a clean status over the
        hole it leaves is this subsystem's whole failure class."""
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
        done = run_client(
            srv.socket_path, sys.executable, "-c", holder, cwd=srv.repos
        )
        assert done.returncode == 0
        assert b"missing" in done.stderr
        assert b"istota-devbox-exec:" in done.stderr

    def test_a_clean_run_says_nothing_on_stderr_of_its_own(self, server):
        """The prefix is the discriminator of record, so it must not appear on
        a run with nothing to discriminate."""
        done = run_client(server.socket_path, "echo", "hello", cwd=server.repos)
        assert done.returncode == 0
        assert done.stderr == b""


# --------------------------------------------------------------------------- #
# The file itself
# --------------------------------------------------------------------------- #


class TestTheClientIsACopyableLeaf:
    """`setup_env` copies this file and the protocol module side by side into
    the task's shim directory, so it has to work with nothing else present."""

    def test_it_has_a_shebang(self):
        assert CLIENT.read_bytes().startswith(b"#!/usr/bin/env python3\n")

    def test_it_does_not_reimplement_the_framing(self):
        """Two files, not one (Design 5). A standalone client would put the
        wire format in three places with `sync-devbox-lib.sh` covering one."""
        source = CLIENT.read_text()
        assert "devbox_exec_protocol" in source
        assert "import struct" not in source
        assert "BxxxI" not in source

    def test_it_installs_no_signal_handlers(self):
        """Design 4: nothing sends them. The daemon's kill is a SIGKILL of the
        process group, which no handler observes, and Ctrl-C's default
        disposition already closes the connection and makes the server reap."""
        source = CLIENT.read_text()
        assert "signal.signal" not in source
        assert "import signal" not in source

    def test_it_runs_from_a_copy_with_only_the_protocol_module_beside_it(
        self, server, tmp_path
    ):
        """What `{dev_bin}` looks like: two files in a directory, and no istota
        package to import from."""
        dev_bin = tmp_path / "dev_bin"
        dev_bin.mkdir()
        shutil.copy2(CLIENT, dev_bin / CLIENT.name)
        shutil.copy2(PROTOCOL, dev_bin / PROTOCOL.name)
        env = {
            k: v for k, v in os.environ.items() if k not in ("PYTHONPATH", "PYTHONHOME")
        }
        done = run_client(
            server.socket_path,
            "sh",
            "-c",
            "exit 3",
            cwd=server.repos,
            env=env,
            client=dev_bin / CLIENT.name,
        )
        assert done.returncode == 3
        assert done.stderr == b""

    def test_the_sibling_module_is_what_it_imports_and_not_the_package(
        self, server, tmp_path
    ):
        """The control for the test above, which on its own proves nothing: this
        checkout is an editable install, so `import istota…` succeeds from any
        directory and a client written that way passes it. Copying the client
        *without* its sibling is what discriminates — it must fail, and name the
        module it could not find."""
        dev_bin = tmp_path / "lonely"
        dev_bin.mkdir()
        shutil.copy2(CLIENT, dev_bin / CLIENT.name)
        done = run_client(
            server.socket_path,
            "sh",
            "-c",
            "exit 3",
            cwd=server.repos,
            client=dev_bin / CLIENT.name,
        )
        assert done.returncode != 3
        assert done.returncode != 0
        assert b"devbox_exec_protocol" in done.stderr


# --------------------------------------------------------------------------- #
# The descriptors the client is handed
# --------------------------------------------------------------------------- #


def run_client_with_redirection(
    redirect: str,
    socket_path: str,
    *command: str,
    cwd: str | Path | None = None,
    options: tuple[str, ...] = (),
    timeout: float = RUN_TIMEOUT,
) -> subprocess.CompletedProcess:
    """Run the client under a shell that has closed one of its std descriptors."""
    inner = " ".join(
        shlex.quote(a)
        for a in [
            sys.executable,
            str(CLIENT),
            "--socket",
            socket_path,
            *options,
            "--",
            *command,
        ]
    )
    return subprocess.run(
        ["sh", "-c", f"exec {inner} {redirect}"],
        cwd=str(cwd) if cwd is not None else None,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )


class TestAClosedStandardDescriptorCannotBecomeTheSocket:
    """`socket.socket()` takes the lowest free descriptor. A shim invoked as
    `npm ci >&-` — which the model can type — hands this process a closed fd 1,
    and without the reservation the socket lands on it: the command's output
    goes into the wire and the client exits 0, which is a clean status over
    missing output. With more output the server parses the command's own stdout
    as a client frame instead, kills the process group, and the status is wrong
    in the other direction."""

    def test_a_closed_stdout_neither_swallows_the_output_nor_the_status(self, server):
        done = run_client_with_redirection(
            ">&-",
            server.socket_path,
            "sh",
            "-c",
            "echo hello-from-command; exit 5",
            cwd=server.repos,
        )
        assert done.returncode == 5
        assert b"unknown stream" not in done.stderr

    def test_a_closed_stderr_leaves_the_output_where_it_belongs(self, server):
        """The command has to write to *stderr* for this to discriminate: with
        the socket on fd 2, it is the command's own stderr that goes into the
        wire, the server reads it as a client frame and kills the group, and
        the status that comes back is the transport's rather than the
        command's.

        And it has to still be running when that happens, which is what the
        sleep is for: a command that has already exited has had its terminal
        frame sent, so the client reads its status before it injects anything
        and the corruption goes unobserved."""
        done = run_client_with_redirection(
            "2>&-",
            server.socket_path,
            "sh",
            "-c",
            "echo to-stderr >&2; sleep 2; echo to-stdout; exit 6",
            cwd=server.repos,
        )
        assert done.returncode == 6
        assert done.stdout == b"to-stdout\n"

    def test_a_write_only_stdin_still_sends_the_eof_marker(self, server_factory):
        """A dead fd 0 with a live connection is the case the swallowed
        exception got wrong: the command waits on stdin that will never come,
        and the wait ends at the server's idle backstop — an hour, on the
        deployment default — reported as a command killed for idleness."""
        srv = server_factory(idle_timeout=8.0)
        done = run_client_with_redirection(
            "0>/dev/null",
            srv.socket_path,
            "cat",
            cwd=srv.repos,
            options=("--stdin",),
            timeout=30,
        )
        assert done.returncode == 0
        assert done.stdout == b""

    def test_a_closed_stdin_is_not_read_as_the_servers_own_frames(self, server):
        """The worst of the three: the stdin thread would read the server's
        frames off the socket and send them back as the command's input, which
        put raw 8-byte frame headers on the command's stdout."""
        done = run_client_with_redirection(
            "0<&-",
            server.socket_path,
            "cat",
            cwd=server.repos,
            options=("--stdin",),
        )
        assert done.returncode == 0
        assert done.stdout == b""


class TestTheClientsOwnStdoutGoingAway:
    def test_a_closed_reader_is_reported_as_141_with_a_line_that_says_why(
        self, server
    ):
        """`shimmed | head -1`. A real command takes SIGPIPE and reports 141, so
        the shim does too — and says so, because a shim reporting 141 and a
        command reporting 141 are different facts."""
        inner = " ".join(
            shlex.quote(a)
            for a in [
                sys.executable,
                str(CLIENT),
                "--socket",
                server.socket_path,
                "--",
                "yes",
            ]
        )
        done = subprocess.run(
            ["bash", "-o", "pipefail", "-c", f"{inner} | head -1"],
            cwd=str(server.repos),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=RUN_TIMEOUT,
        )
        assert done.returncode == SIGPIPE_EXIT
        assert done.stdout == b"y\n"
        assert b"istota-devbox-exec:" in done.stderr
        assert b"closed the pipe" in done.stderr


# --------------------------------------------------------------------------- #
# Reading the wire
# --------------------------------------------------------------------------- #


class TestTheAcknowledgementAndTheFramesMayShareASegment:
    def test_bytes_after_the_newline_are_not_dropped(self, fake_server):
        """The real server writes the ack and the first frames as separate
        transport writes, so this path is unreachable through it — measured,
        every `leftover` against a real server is empty. A client that read the
        ack with `recv` and threw the rest away passes the whole suite."""

        def respond(conn, line):
            conn.sendall(
                encode_ack_ok()
                + pack_frame(STREAM_STDOUT, b"in the same segment")
                + encode_control({"exit_code": 3, "signal": None})
            )

        srv = fake_server(respond)
        done = run_client(srv.path, "true")
        assert done.stdout == b"in the same segment"
        assert done.returncode == 3


class TestTheTerminalFrameMustBeUsable:
    @pytest.mark.parametrize("code", [4096, -1, True, "0", 0.0])
    def test_a_status_no_process_could_have_produced_is_a_protocol_error(
        self, fake_server, code
    ):
        """Each reaches 122 by a different clause of the same check, and these
        are the clauses a later edit is most likely to simplify away."""

        def respond(conn, line):
            conn.sendall(encode_ack_ok())
            conn.sendall(encode_control({"exit_code": code, "signal": None}))

        srv = fake_server(respond)
        done = run_client(srv.path, "true")
        assert done.returncode == EXIT_PROTOCOL_ERROR

    def test_a_control_frame_that_is_not_json_is_a_protocol_error(self, fake_server):
        def respond(conn, line):
            conn.sendall(encode_ack_ok())
            conn.sendall(pack_frame(STREAM_CONTROL, b"not json"))

        srv = fake_server(respond)
        done = run_client(srv.path, "true")
        assert done.returncode == EXIT_PROTOCOL_ERROR


# --------------------------------------------------------------------------- #
# The command line
# --------------------------------------------------------------------------- #


class TestTheCommandIsTakenVerbatim:
    def test_only_the_first_separator_is_the_separator(self, fake_server):
        """`npm run test -- --watch` is an ordinary thing to type, and argparse's
        own `--` handling is what this splits by hand to avoid."""
        srv = fake_server(_ack_then_exit_zero)
        done = run_client(
            srv.path, "npm", "run", "test", "--", "--watch", "--reporter=dot"
        )
        assert done.returncode == 0
        assert json.loads(srv.requests[0])["argv"] == [
            "npm",
            "run",
            "test",
            "--",
            "--watch",
            "--reporter=dot",
        ]

    def test_an_argument_with_spaces_and_globs_travels_unchanged(self, fake_server):
        srv = fake_server(_ack_then_exit_zero)
        done = run_client(srv.path, "sh", "-c", "echo 'a b' *.py")
        assert done.returncode == 0
        assert json.loads(srv.requests[0])["argv"] == ["sh", "-c", "echo 'a b' *.py"]

    def test_no_separator_at_all_is_a_usage_failure_and_not_a_status(self, tmp_path):
        done = subprocess.run(
            [sys.executable, str(CLIENT), "--socket", str(tmp_path / "s.sock"), "true"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=RUN_TIMEOUT,
        )
        assert done.returncode == EXIT_NO_CONNECT
        assert b"separator" in done.stderr

    def test_a_separator_with_nothing_after_it_is_a_usage_failure(self, tmp_path):
        done = run_client(str(tmp_path / "s.sock"))
        assert done.returncode == EXIT_NO_CONNECT
        assert b"istota-devbox-exec:" in done.stderr

    def test_a_help_flag_in_the_command_belongs_to_the_command(self, fake_server):
        """`-- npm --help` is a request to run `npm --help` in the container,
        not a request for this client's usage."""
        srv = fake_server(_ack_then_exit_zero)
        done = run_client(srv.path, "npm", "--help")
        assert done.returncode == 0
        assert done.stdout == b""
        assert json.loads(srv.requests[0])["argv"] == ["npm", "--help"]

    def test_help_prints_usage_and_still_does_not_exit_zero(self):
        """argparse exits 0 from inside `print_help`. A shim that got a zero
        status from a client that ran nothing is the one answer this file
        exists never to give."""
        done = subprocess.run(
            [sys.executable, str(CLIENT), "--help"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=RUN_TIMEOUT,
        )
        assert done.returncode != 0
        assert b"--socket" in done.stdout


class TestTheTimeouts:
    def test_a_zero_connect_budget_means_no_budget_rather_than_non_blocking(
        self, server
    ):
        """`settimeout(0)` puts the socket in non-blocking mode, where `connect`
        raises at once — so a `0` in config would fail every command on the
        deployment rather than removing a limit."""
        done = run_client(
            server.socket_path,
            "sh",
            "-c",
            "exit 4",
            cwd=server.repos,
            options=("--connect-timeout", "0"),
        )
        assert done.returncode == 4

    def test_a_non_finite_timeout_never_reaches_the_wire(self, server):
        """`nan` is neither less than nor greater than zero, so the protocol's
        range check passes it, and `json.dumps` writes the non-standard token
        `NaN`."""
        done = run_client(
            server.socket_path,
            "true",
            cwd=server.repos,
            options=("--timeout", "nan"),
        )
        assert done.returncode == EXIT_NO_CONNECT
        assert b"finite" in done.stderr
