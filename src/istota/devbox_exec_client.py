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
 122   protocol error — a malformed frame or acknowledgement, or a fault in
       this client
 123   no exit status: the connection ended after the acknowledgement and
       before the terminal frame, or the server reported no status of its own.
       The command's fate is unknown
=====  ========================================================================

125, 126 and 127 are deliberately not used: they collide with ``docker``'s own
status and with the shell's "not executable" and "command not found", and 127
is the status the model reads most often. The stderr line is the discriminator
of record; the code carries only what a caller can act on. (120 is also what
CPython exits with when it cannot flush ``sys.stdout`` at shutdown, which is
why nothing here writes the command's output through ``sys.stdout``.)

Two statuses inside a command's own range are reported deliberately, because in
both cases the shim is standing in for a command that would have reported them
itself: **141** when this client's own stdout is a closed pipe, which is what a
real command takes SIGPIPE for in ``npm ls | head -1``, and **130** on Ctrl-C.
Each still prints its line, so the prefix distinguishes them from a command
that produced the same number.

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

**This client always sends a string, and has no spelling for ``cwd: null``.**
The wire carries ``string | null`` — ``null`` meaning "the server chooses",
which resolves to its own ``/home/dev`` — and the caller that needs it is the
devbox skill's ad-hoc verbs, which have no repository to stand in. They do not
get it from here. That skill's CLI runs host-side in a Python process and has
to import ``devbox_exec_protocol`` regardless, because four of its five verbs
(``cp-in``, ``cp-out``, ``exec-file``'s write half and ``status``) are actions
this client does not implement at all and never should — it runs one command
and exits with its status. Giving it a ``--cwd=-`` or a ``--server-cwd`` would
be a second mechanism for one verb of a caller that is already speaking the
wire for the other four, and it would put a flag on the surface a shim bakes
in, where every added spelling is one more thing a shim can get wrong. So the
flag surface stays: ``--socket``, ``--cwd``, ``--stdin``, ``--timeout``,
``--connect-timeout``, ``--``.

No signal handlers
------------------

Nothing sends the client a signal it could usefully catch. The daemon's kill
path is SIGKILL on the process group, which no handler observes, and an
operator's Ctrl-C closes the connection and makes the server reap the process
group — which is the behaviour a handler would have been written to produce.
The connection close is the one reap signal in the design and there is nothing
competing with it. ``KeyboardInterrupt`` is caught, but only to say one line
and return 130 instead of printing a traceback into the middle of the command's
own stderr; no disposition is changed and ``signal`` is not imported.
"""

from __future__ import annotations

import argparse
import math
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

# The two a command could have produced itself, reported where this process is
# standing in for one that would have. See the module docstring.
EXIT_SIGPIPE = proto.SIGPIPE_EXIT
EXIT_INTERRUPTED = 130

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


class _OutputFailed(Exception):
    """This process could not write the command's output to its own descriptor.

    Distinct from a lost connection, which is what an ``OSError`` from a write
    to fd 1 used to be reported as: a full disk or a non-blocking stdout is a
    fact about this end, and naming the socket there sends whoever reads it to
    look at the container.
    """


def _say(message: str) -> None:
    """One line on stderr, prefixed. Never raises — stderr may be gone too.

    Control characters are flattened first. Part of what lands here came from
    the server and is model-chosen — an argv[0] in a ``spawn_failed`` message,
    a refused path — and a newline in it would forge a second line carrying
    this prefix, which the design calls the discriminator of record.
    """
    flat = "".join(c if c.isprintable() or c == " " else " " for c in str(message))
    try:
        sys.stderr.write(f"{PREFIX}: {flat}\n")
        sys.stderr.flush()
    except Exception:
        pass


def _reserve_std_descriptors() -> None:
    """Open fds 0, 1 and 2 on /dev/null if the caller left any of them closed.

    Not hygiene — the alternative is a silent green. ``socket.socket()`` takes
    the lowest free descriptor, so a shim invoked as ``npm ci >&-`` (which the
    model can type) hands this process a closed fd 1, the socket lands *on* it,
    and ``_write_out(1, …)`` then writes the command's output into the wire.
    Measured: the command's stdout disappears, the server never parses those
    bytes, and the client exits 0 — a clean status over missing output, which
    is the failure class this whole transport exists to remove. With more
    output the same setup corrupts the status instead, because the server reads
    the command's own stdout as a client frame and kills the process group. The
    fd 0 variant is worse again: the stdin thread reads the server's frames off
    the socket and sends them back as the command's input.

    Done before anything else opens a descriptor, which is why it is the first
    statement in ``main``.
    """
    for fd in (0, 1, 2):
        try:
            os.fstat(fd)
        except OSError:
            opened = os.open(os.devnull, os.O_RDWR)
            if opened != fd:
                os.dup2(opened, fd)
                os.close(opened)


# ---- Arguments -------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:  # type: ignore[override]
        raise _Usage(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
        """Never `sys.exit` from inside argparse.

        `--help` calls this with status 0 after printing, which would hand a
        shim a zero status for a run that executed nothing — the one answer
        this file exists to never give. The help text is already on stdout by
        then; what changes is only what the caller is told about it.
        """
        raise _Usage(message.strip() if message else "printed usage and ran nothing")


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


def wants_help(argv: list[str]) -> bool:
    """True when the *client* was asked for help, rather than the command.

    Scanned before the split and stopping at the separator, because ``-- npm
    --help`` is a request to run ``npm --help`` in the container and must not
    print this file's usage instead.
    """
    for token in argv:
        if token == "--":
            return False
        if token in ("-h", "--help"):
            return True
    return False


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

    The EOF marker goes in a ``finally``, which is the difference between two
    failures that look alike from here. A dead socket has nobody to tell. A
    dead **fd 0** — a write-only descriptor, a revoked tty — leaves a perfectly
    good connection whose command is waiting on stdin that will never arrive,
    and without this the wait ends at the server's idle backstop, an hour later,
    reported as a command killed for idleness.
    """
    try:
        while True:
            chunk = os.read(0, proto.CHUNK_BYTES)
            if not chunk:
                break
            sock.sendall(proto.pack_frame(proto.STREAM_STDIN, chunk))
    except Exception:
        pass
    finally:
        try:
            sock.sendall(proto.encode_stdin_eof())
        except Exception:
            pass


def _write_out(fileno: int, payload: bytes) -> None:
    """Write one output frame straight through to a descriptor.

    ``os.write`` rather than ``sys.stdout.buffer``: no buffering to reason
    about, and a partial write is visible here rather than left to a flush
    that happens somewhere else.

    A broken pipe travels on unchanged, because that one is not a fault — it is
    ``| head`` and it has its own answer. Every other descriptor failure gets
    named as what it is rather than being reported as a lost connection.
    """
    view = memoryview(payload)
    while view:
        try:
            written = os.write(fileno, view)
        except BrokenPipeError:
            raise
        except OSError as e:
            raise _OutputFailed(
                f"could not write the command's output to fd {fileno}: {e}"
            ) from e
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
    code = body.get("exit_code")
    if code is None:
        # The server has no status to report and says so rather than inventing
        # one. Same answer as a connection that ended early, for the same
        # reason: the command's fate is unknown, and it is never 0. Its reason
        # is folded into this line rather than printed as a second one.
        error = body.get("error")
        message = body.get("message")
        detail = f" ({error}: {message})" if error else ""
        _say(
            f"the server reported no exit status{detail}; the command's fate "
            f"is unknown"
        )
        return EXIT_CONNECTION_LOST
    if isinstance(code, bool) or not isinstance(code, int) or not 0 <= code <= 255:
        _say(f"the server reported an exit status this client cannot use: {code!r}")
        return EXIT_PROTOCOL_ERROR
    for line in _notes(body):
        _say(line)
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


def _run(argv: list[str]) -> int:
    if wants_help(argv):
        build_parser().print_help()
        _say("printed usage and ran nothing")
        return EXIT_NO_CONNECT
    try:
        options, command = split_argv(list(argv))
        args = build_parser().parse_args(options)
    except _Usage as e:
        _say(str(e))
        return EXIT_NO_CONNECT
    if not command:
        _say("no command given after '--'")
        return EXIT_NO_CONNECT
    if not math.isfinite(args.timeout):
        # `float("nan")` is neither < 0 nor > 0, so it passes the protocol's
        # range check, and `json.dumps` writes it as the non-standard token
        # `NaN`. Both ends are this one module today; the day anything else
        # parses a request, that is an interop failure with no error message.
        _say(f"--timeout must be a finite number of seconds, got {args.timeout}")
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

    # A zero or negative budget would put the socket in non-blocking mode,
    # where `connect` raises immediately and every command on the deployment
    # fails to connect. Not a timeout anybody meant to ask for, so it is read
    # as "no timeout".
    budget = args.connect_timeout if args.connect_timeout > 0 else None

    with sock:
        try:
            sock.settimeout(budget)
            sock.connect(args.socket)
            sock.sendall(request)
        except OSError as e:
            _say(f"could not connect to {args.socket}: {e}")
            return EXIT_NO_CONNECT

        try:
            # Still under the connect budget, deliberately. This is the one
            # read in the file that can be bounded without risking a wrong
            # answer about a running command, and leaving it unbounded means a
            # socket that accepts and never speaks hangs the shim for the whole
            # task — which a hostile process inside the container can arrange,
            # since it may unlink the socket and bind its own in its place.
            ack, leftover = _read_ack(sock)
        except TimeoutError:
            # Not 120: a spawn slow enough to miss the budget has still
            # happened, and this client cannot tell that from a server that
            # never read the request. The one true statement is the 123 one.
            _say(
                f"{args.socket}: the server accepted the connection and did not "
                f"acknowledge within {budget}s; the command's fate is unknown"
            )
            return EXIT_CONNECTION_LOST
        except (proto.ProtocolError, OSError) as e:
            _say(f"{args.socket}: {e}")
            return EXIT_PROTOCOL_ERROR

        # Off for everything after the acknowledgement: a build can go minutes
        # without a byte, and the server's idle timeout is the backstop.
        sock.settimeout(None)

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
                f"(it speaks {sorted(proto.SUPPORTED_PROTOCOLS)}); the command "
                f"was already started and has been abandoned"
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
            # makes the server reap the process group. Said out loud, because
            # a shim reporting 141 and a command reporting 141 are different
            # facts and only the prefix separates them.
            _say(
                "the reader of this command's output closed the pipe; the "
                "command was abandoned and is reported as 141, the status a "
                "command taking SIGPIPE would have produced"
            )
            return EXIT_SIGPIPE
        except _OutputFailed as e:
            _say(str(e))
            return EXIT_CONNECTION_LOST
        except OSError as e:
            _say(f"{args.socket}: {e}")
            return EXIT_CONNECTION_LOST


def main(argv: list[str]) -> int:
    """Never raise to the shim, on any path.

    A traceback out of here is an exit 1, which is a status a command produces,
    so a client fault would arrive as a failed build. Every deliberate failure
    already has a code; this is for the ones nobody thought of.
    """
    _reserve_std_descriptors()
    try:
        return _run(list(argv))
    except KeyboardInterrupt:
        _say("interrupted")
        return EXIT_INTERRUPTED
    except BaseException as e:
        _say(f"internal client error: {e.__class__.__name__}: {e}")
        return EXIT_PROTOCOL_ERROR


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
