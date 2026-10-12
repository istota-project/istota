"""IMAP and SMTP servers that speak just enough to sign in, over STARTTLS.

For the first-run setup's email check, which signs in and out and sends no
message. The real clients (`imap_tools` through `skills.email.imap_session`,
`smtplib` through `skills.email._open_smtp`) talk to these over a socket, TLS
included, so nothing of istota's is patched. The wire-level mail suite runs a
real server in a container; these are for the default suite.

Each server accepts one username and password and records what it was asked,
the password never among it.
"""

from __future__ import annotations

import base64
import socketserver
import ssl
import threading
from pathlib import Path

LOOPBACK = "127.0.0.1"


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _LineStub:
    """A line-oriented server on an ephemeral loopback port."""

    def __init__(self, *, cert: Path, key: Path, username: str, password: str,
                 starttls: bool = True) -> None:
        self.username = username
        self.password = password
        self.starttls = starttls
        self.commands: list[str] = []
        self.logins: list[tuple[str, bool]] = []
        self._lock = threading.Lock()
        self._tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        self._tls.load_cert_chain(str(cert), str(key))
        stub = self

        class Handler(socketserver.StreamRequestHandler):
            timeout = 10

            def handle(self) -> None:
                try:
                    stub._serve(self)
                except (OSError, ssl.SSLError, ValueError):
                    pass

        self._server = _Server((LOOPBACK, 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(
            target=lambda: self._server.serve_forever(poll_interval=0.02), daemon=True,
        )
        self._thread.start()

    def _note(self, verb: str) -> None:
        with self._lock:
            self.commands.append(verb)

    def _note_login(self, user: str, accepted: bool) -> None:
        with self._lock:
            self.logins.append((user, accepted))

    def _wrap(self, handler) -> None:
        tls = self._tls.wrap_socket(handler.connection, server_side=True)
        handler.connection = handler.request = tls
        handler.rfile = tls.makefile("rb")
        handler.wfile = tls.makefile("wb")

    @staticmethod
    def _send(handler, *lines: str) -> None:
        handler.wfile.write("".join(line + "\r\n" for line in lines).encode())
        handler.wfile.flush()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()


class ImapStub(_LineStub):
    """CAPABILITY, STARTTLS, LOGIN, SELECT and LOGOUT."""

    def _serve(self, h) -> None:
        self._send(h, "* OK stub IMAP4rev1 ready")
        secure = False
        while True:
            raw = h.rfile.readline()
            if not raw:
                return
            line = raw.decode().rstrip("\r\n")
            tag, _, rest = line.partition(" ")
            verb, _, args = rest.partition(" ")
            verb = verb.upper()
            self._note(verb)
            if verb == "CAPABILITY":
                caps = "IMAP4rev1 AUTH=PLAIN" + ("" if secure or not self.starttls else " STARTTLS")
                self._send(h, f"* CAPABILITY {caps}", f"{tag} OK CAPABILITY completed")
            elif verb == "STARTTLS" and self.starttls and not secure:
                self._send(h, f"{tag} OK begin TLS")
                self._wrap(h)
                secure = True
            elif verb == "LOGIN":
                user, password = _imap_strings(args)
                accepted = user == self.username and password == self.password
                self._note_login(user, accepted)
                if accepted:
                    self._send(h, f"{tag} OK LOGIN completed")
                else:
                    self._send(h, f"{tag} NO [AUTHENTICATIONFAILED] invalid credentials")
            elif verb in ("SELECT", "EXAMINE"):
                self._send(
                    h, "* FLAGS (\\Seen)", "* 0 EXISTS", "* 0 RECENT",
                    "* OK [UIDVALIDITY 1] UIDs valid", f"{tag} OK [READ-WRITE] SELECT completed",
                )
            elif verb == "LOGOUT":
                self._send(h, "* BYE logging out", f"{tag} OK LOGOUT completed")
                return
            else:
                self._send(h, f"{tag} BAD unsupported")


def _imap_strings(args: str) -> tuple[str, str]:
    """The two arguments of a LOGIN, quoted or atoms."""
    out: list[str] = []
    i = 0
    while i < len(args) and len(out) < 2:
        if args[i] == " ":
            i += 1
            continue
        if args[i] == '"':
            i += 1
            chars: list[str] = []
            while i < len(args) and args[i] != '"':
                if args[i] == "\\" and i + 1 < len(args):
                    i += 1
                chars.append(args[i])
                i += 1
            out.append("".join(chars))
            i += 1
        else:
            end = args.find(" ", i)
            end = len(args) if end == -1 else end
            out.append(args[i:end])
            i = end
    while len(out) < 2:
        out.append("")
    return out[0], out[1]


class SmtpStub(_LineStub):
    """EHLO, STARTTLS, AUTH PLAIN and QUIT; never accepts a message."""

    def _serve(self, h) -> None:
        self._send(h, "220 stub ESMTP ready")
        secure = False
        while True:
            raw = h.rfile.readline()
            if not raw:
                return
            line = raw.decode().rstrip("\r\n")
            verb, _, args = line.partition(" ")
            verb = verb.upper()
            self._note(verb)
            if verb in ("EHLO", "HELO"):
                lines = ["250-stub"]
                if self.starttls and not secure:
                    lines.append("250-STARTTLS")
                lines.append("250 AUTH PLAIN")
                self._send(h, *lines)
            elif verb == "STARTTLS" and self.starttls and not secure:
                self._send(h, "220 ready to start TLS")
                self._wrap(h)
                secure = True
            elif verb == "AUTH":
                mechanism, _, initial = args.partition(" ")
                if mechanism.upper() != "PLAIN" or not initial:
                    self._send(h, "504 only AUTH PLAIN with an initial response")
                    continue
                _authz, user, password = base64.b64decode(initial).decode().split("\0")
                accepted = user == self.username and password == self.password
                self._note_login(user, accepted)
                self._send(h, "235 authenticated" if accepted else "535 authentication failed")
            elif verb == "QUIT":
                self._send(h, "221 bye")
                return
            else:
                self._send(h, "502 not implemented")
