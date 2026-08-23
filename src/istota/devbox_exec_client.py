#!/usr/bin/env python3
"""The devbox exec client: one command, over the transport, with its real status.

What a shim execs. It connects to the per-user socket, sends one ``exec``
request, streams the command's output through, and exits with the status the
server reported — which is the whole reason this transport exists, because
``docker exec`` through the Docker-API proxy runs the command and loses it.

**stdlib only, and the protocol module is the only import.** ``setup_env``
copies this file and ``devbox_exec_protocol.py`` side by side into the task's
shim directory, so the framing lives in one file rather than being written out
a third time here: a module, a vendored container copy and an inlined client
copy would be three places for one wire format, and ``sync-devbox-lib.sh``
covers one of them. Run as a script the module is the sibling file; imported as
``istota.devbox_exec_client`` it is the package's own.

Exit codes
----------

The client never raises to the shim. Every failure prints one line to stderr
prefixed ``istota-devbox-exec:`` and exits with a status outside the range a
command produces:

=====  ========================================================================
 120   nothing ran: the socket could not be reached, the working directory
       could not be determined, or the server refused the request in its
       acknowledgement
 121   the server's ``protocol`` is one this client does not know
 122   protocol error — a malformed frame or acknowledgement
 123   the connection ended after the acknowledgement and before the terminal
       frame; the command's fate is unknown
=====  ========================================================================

125, 126 and 127 are deliberately not used: they collide with ``docker``'s own
status and with the shell's "not executable" and "command not found", and 127
is the status the model reads most often. The stderr line is the discriminator
of record; the code carries only what a caller can act on.

**123 is the point of the file.** A connection that ends after the ack and
before the terminal frame is the one case with no exit status at all — a
container restart, or the container itself being OOM-killed mid-build — and it
is what a naive implementation reports as 0. It is a different code from 120 so
an operator can tell a devbox that never answered from one that died mid-build.

The working directory
---------------------

``os.getcwd()`` and never ``$PWD``. ``$PWD`` is the *logical* path the parent
shell recorded, so a ``cd`` through a symlink yields a string whose meaning
differs from the directory the process is actually in — and the server's whole
containment test is a ``realpath``. ``os.getcwd()`` returns the physical path,
which is what that test wants, and it fails loudly on a deleted directory
instead of sending a dead string for the server to have a rule about. ``--cwd``
overrides it for the devbox skill, which passes a directory it was told about
rather than one it is standing in.

No signal handlers
------------------

Nothing sends the client a signal it could usefully catch. The daemon's kill
path is SIGKILL on the process group, which no handler observes, and an
operator's Ctrl-C gets the default disposition: the client dies, the connection
closes, and the server reaps the process group — which is the behaviour a
handler would have been written to produce. The connection close is the one
reap signal in the design and there is nothing competing with it.
"""

from __future__ import annotations

import argparse
import os
import socket
import sys
import threading
from typing import Any

if __package__ in (None, ""):
    # Executed as a script: the protocol module is the file beside this one,
    # which is how it arrives in the task's shim directory. Preferred over the
    # installed package deliberately — the copy that travelled with this file
    # is the one whose framing it was tested against.
    import devbox_exec_protocol as proto  # type: ignore[import-not-found]
else:
    from . import devbox_exec_protocol as proto


PREFIX = "istota-devbox-exec"

EXIT_NO_CONNECT = 120
EXIT_UNSUPPORTED_PROTOCOL = 121
EXIT_PROTOCOL_ERROR = 122
EXIT_CONNECTION_LOST = 123

# The connect budget, and the only timeout on the connect path. Config carries
# it as `developer.container.connect_timeout_seconds`; a shim bakes it in
# alongside the socket path, so nothing about it is read from the environment.
DEFAULT_CONNECT_TIMEOUT_SECONDS = 5.0


class _Usage(Exception):
    """A bad command line. Handled like every other pre-connect failure."""


class _ConnectionLost(Exception):
    """The connection ended after the acknowledgement, with no terminal frame.

    Raised rather than returned so that the one place holding the socket path
    is the one that reports it, and so no path can reach the end of ``_pump``
    with a status it made up.
    """


def _say(message: str) -> None:
    """One line on stderr, prefixed. Never raises — stderr may be gone too."""
    try:
        sys.stderr.write(f"{PREFIX}: {message}\n")
        sys.stderr.flush()
    except Exception:
        pass


# ---- Arguments -------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _Usage(message)


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(
        prog="devbox-exec",
        description="Run one command in the devbox and exit with its status.",
    )
    parser.add_argument("--socket", required=True, help="the exec socket to connect to")
    parser.add_argument(
        "--cwd",
        default=None,
        help="run in this directory instead of the client's own (devbox skill)",
    )
    parser.add_argument(
        "--stdin",
        action="store_true",
        help="forward this process's stdin to the command",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=0.0,
        help="seconds before the server kills the process group; 0 means none",
    )
    parser.add_argument(
        "--connect-timeout",
        type=float,
        default=DEFAULT_CONNECT_TIMEOUT_SECONDS,
        help="seconds to wait for the socket",
    )
    return parser


def split_argv(argv: list[str]) -> tuple[list[str], list[str]]:
    """Split the client's own options from the command, at the first ``--``.

    Done by hand rather than left to argparse: the command routinely carries
    its own options (``npm run test -- --watch``), and only the *first* ``--``
    is the separator. Everything after it travels verbatim.
    """
    if "--" not in argv:
        raise _Usage("the command must follow a '--' separator")
    cut = argv.index("--")
    return argv[:cut], argv[cut + 1 :]


# ---- The conversation ------------------------------------------------------


def _read_ack(sock: socket.socket) -> tuple[dict[str, Any], bytes]:
    """Read the acknowledgement line, returning it and whatever followed it.

    The server may write the ack and the first output frames into one segment,
    so the bytes past the newline are handed back rather than dropped.
    """
    buf = b""
    while b"\n" not in buf:
        chunk = sock.recv(proto.CHUNK_BYTES)
        if not chunk:
            raise proto.ProtocolError(
                proto.ERR_BAD_REQUEST,
                "the server closed the connection without acknowledging",
            )
        buf += chunk
        if len(buf) > proto.MAX_REQUEST_BYTES:
            raise proto.ProtocolError(
                proto.ERR_TOO_LARGE, "acknowledgement line exceeds the request cap"
            )
    line, _, rest = buf.partition(b"\n")
    return proto.decode_ack(line), rest


def _forward_stdin(sock: socket.socket) -> None:
    """Feed this process's stdin to the command, then send the EOF marker.

    A thread rather than a selector loop: the main thread's job is to get
    output onto the terminal the moment it arrives, and a blocking read on
    fd 0 is the simplest thing that does not interfere with that. It is a
    daemon thread, so a command that exits while its stdin is still open —
    ``head -c 5`` — does not hold the process up. No lock: this is the only
    thing that ever writes to the socket once the request has gone, and the
    main thread only ever reads.

    Swallows its own errors on purpose. The socket dying is the main thread's
    story to tell, and a traceback from a daemon thread would land in the
    middle of the command's own output.
    """
    try:
        while True:
            chunk = os.read(0, proto.CHUNK_BYTES)
            if not chunk:
                break
            sock.sendall(proto.pack_frame(proto.STREAM_STDIN, chunk))
        sock.sendall(proto.encode_stdin_eof())
    except Exception:
        return


def _write_out(fileno: int, payload: bytes) -> None:
    """Write one output frame straight through to a descriptor.

    ``os.write`` rather than ``sys.stdout.buffer``: no buffering to reason
    about, and a partial write is visible here rather than left to a flush
    that happens somewhere else.
    """
    view = memoryview(payload)
    while view:
        written = os.write(fileno, view)
        view = view[written:]


def _notes(body: dict[str, Any]) -> list[str]:
    """The lines a terminal frame is worth saying something about."""
    lines: list[str] = []
    reason = body.get("reason")
    if reason:
        lines.append(f"the command was killed by the server (reason: {reason})")
    if body.get("truncated"):
        lines.append(
            "some of the command's output could not be read back and is missing"
        )
    note = body.get("note")
    if isinstance(note, str) and note:
        lines.append(note)
    error = body.get("error")
    if error:
        lines.append(f"{error}: {body.get('message', '')}".strip())
    return lines


def _finish(body: dict[str, Any]) -> int:
    """Turn the terminal control frame into this process's exit status."""
    for line in _notes(body):
        _say(line)
    code = body.get("exit_code")
    if code is None:
        # The server has no status to report and says so rather than inventing
        # one. Same answer as a connection that ended early, for the same
        # reason: the command's fate is unknown, and it is never 0.
        _say("the server reported no exit status; the command's fate is unknown")
        return EXIT_CONNECTION_LOST
    if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 255:
        _say(f"the server reported an exit status this client cannot use: {code!r}")
        return EXIT_PROTOCOL_ERROR
    return code


def _pump(sock: socket.socket, leftover: bytes) -> int:
    """Stream frames until the terminal one, and return the exit status."""
    decoder = proto.FrameDecoder()
    pending = leftover
    while True:
        for stream, payload in decoder.feed(pending):
            if stream == proto.STREAM_STDOUT:
                _write_out(1, payload)
            elif stream == proto.STREAM_STDERR:
                _write_out(2, payload)
            elif stream == proto.STREAM_CONTROL:
                body = proto.decode_control(payload)
                if proto.is_terminal(body):
                    return _finish(body)
            else:
                raise proto.ProtocolError(
                    proto.ERR_BAD_REQUEST,
                    f"the server sent stream {stream}, which travels the other way",
                )
        pending = sock.recv(proto.CHUNK_BYTES)
        if not pending:
            raise _ConnectionLost()


# ---- Entry point -----------------------------------------------------------


def main(argv: list[str]) -> int:
    try:
        options, command = split_argv(list(argv))
        args = build_parser().parse_args(options)
    except _Usage as e:
        _say(str(e))
        return EXIT_NO_CONNECT
    if not command:
        _say("no command given after '--'")
        return EXIT_NO_CONNECT

    if args.cwd is not None:
        cwd = args.cwd
    else:
        try:
            cwd = os.getcwd()
        except OSError as e:
            # A deleted working directory. Loud here rather than a dead string
            # for the server to have a rule about.
            _say(f"cannot determine the working directory: {e}")
            return EXIT_NO_CONNECT

    try:
        request = proto.encode_exec_request(
            argv=command, cwd=cwd, stdin=args.stdin, timeout=args.timeout
        )
    except proto.ProtocolError as e:
        _say(str(e))
        return EXIT_NO_CONNECT

    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    except OSError as e:  # pragma: no cover - AF_UNIX is always available here
        _say(f"cannot create a socket: {e}")
        return EXIT_NO_CONNECT

    with sock:
        try:
            # A zero or negative budget would put the socket in non-blocking
            # mode, where `connect` raises immediately and every command on the
            # deployment fails to connect. Not a timeout anybody meant to ask
            # for, so it is read as "no timeout".
            sock.settimeout(args.connect_timeout if args.connect_timeout > 0 else None)
            sock.connect(args.socket)
            # Off for everything after the connect: a build can go minutes
            # without a byte, and the server's idle timeout is the backstop.
            sock.settimeout(None)
            sock.sendall(request)
        except OSError as e:
            _say(f"could not connect to {args.socket}: {e}")
            return EXIT_NO_CONNECT

        try:
            ack, leftover = _read_ack(sock)
        except proto.ProtocolError as e:
            _say(f"{args.socket}: {e}")
            return EXIT_PROTOCOL_ERROR
        except OSError as e:
            _say(f"{args.socket}: {e}")
            return EXIT_PROTOCOL_ERROR

        if ack.get("status") != "ok":
            _say(
                f"the devbox refused the command: "
                f"{ack.get('code')}: {ack.get('message')}"
            )
            return EXIT_NO_CONNECT

        # Only past this point can the command have run, which is what makes
        # the codes below mean different things from the ones above.
        if not proto.supported_protocol(ack.get("protocol")):
            _say(
                f"the devbox speaks protocol {ack.get('protocol')!r}, "
                f"which this client does not know "
                f"(it speaks {sorted(proto.SUPPORTED_PROTOCOLS)})"
            )
            return EXIT_UNSUPPORTED_PROTOCOL

        if args.stdin:
            threading.Thread(target=_forward_stdin, args=(sock,), daemon=True).start()

        try:
            return _pump(sock, leftover)
        except _ConnectionLost:
            _say(
                f"{args.socket}: the connection ended before the command reported "
                f"a status; the command's fate is unknown and it may have completed"
            )
            return EXIT_CONNECTION_LOST
        except proto.ProtocolError as e:
            _say(f"{args.socket}: {e}")
            return EXIT_PROTOCOL_ERROR
        except BrokenPipeError:
            # Our own stdout went away — the shim is in a pipeline and the
            # reader has gone. A real command takes SIGPIPE here, so report
            # what that looks like and let the connection close, which is what
            # makes the server reap the process group.
            return proto.SIGPIPE_EXIT
        except OSError as e:
            _say(f"{args.socket}: {e}")
            return EXIT_CONNECTION_LOST


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
