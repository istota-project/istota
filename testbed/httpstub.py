"""The in-process HTTP server two stubs had each written for themselves.

`gitlab.py` and `services/model_endpoint.py` were written weeks apart and
converged on the same forty lines of `ThreadingHTTPServer` setup, down to the
comments — `daemon_threads = False` so `server_close` joins in-flight handlers,
a handler `timeout` bounding a parked keep-alive, the bound address read back
off the socket rather than trusted from the arguments, `shutdown` before
`server_close`. Two independent implementations arriving at one shape is a
protocol nobody wrote down; this is it, written down.

Every comment below records a bug that was found by one of the two. They moved
here verbatim rather than being reworded, because the reasoning is the thing
worth keeping.
"""

from __future__ import annotations

import threading
from http.server import ThreadingHTTPServer

from .services import ServiceCall

# The host side connects over loopback; a container reaches the same listener by
# the Docker Desktop / Docker Engine alias. Both names are offered rather than
# guessed at the call site, because the two are needed at once: a scenario
# asserts against `calls` in-process while the daemon it is driving talks to
# `container_url`.
LOOPBACK = "127.0.0.1"
FROM_CONTAINER = "host.docker.internal"


class HttpStub:
    """Base for an in-process HTTP stub.

    Subclasses build a handler class and hand it to `start`. What they get in
    return is a bound listener, a recorded call list under a lock, and a `close`
    that actually stops the thing.
    """

    #: Registry key. `Service` requires it; the base leaves it empty so a
    #: subclass that forgot to name itself is visible rather than plausible.
    name: str = ""

    def __init__(self) -> None:
        self.port: int = 0
        self.host_bound: str = ""
        self.credential: str | None = None
        self.calls: list[ServiceCall] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # -- lifecycle --------------------------------------------------------

    def start(
        self,
        handler_cls: type,
        *,
        host: str = LOOPBACK,
        port: int = 0,
        credential: str | None = None,
    ) -> None:
        """Bind, and serve `handler_cls` on a background thread.

        Port 0 lets the OS choose, which is what keeps concurrent test sessions
        from colliding — the chosen port is read back off the socket.

        **A stub bound to anything but loopback must be given a credential to
        expect.** Both deployment tiers bind all interfaces so a container can
        reach the stub, which on a laptop on a shared network means an
        unauthenticated listener — and in the forge stub's case, one that runs
        `git http-backend` with `GIT_HTTP_EXPORT_ALL`. That rule used to live in
        a docstring on `FakeGitLab.expect_git_password`; a convention in a
        docstring survives two implementations, not six, so it is structural
        here. What a subclass *does* with the credential is its own business:
        the forge challenges for it, and a stub with no authenticated verb of
        its own still has to name the value it is exposing, because that is what
        the secret-isolation scenario scans a transcript for.
        """
        if host != LOOPBACK and credential is None:
            raise ValueError(
                f"{type(self).__name__} was asked to bind {host!r}, which is "
                "reachable from outside this machine, without a credential to "
                "expect. Pass `credential=` (see HttpStub.start), or bind "
                f"{LOOPBACK!r}."
            )
        if self._server is not None:
            raise RuntimeError(f"{type(self).__name__} is already serving")

        self.credential = credential
        server = ThreadingHTTPServer((host, port), handler_cls)
        # Set explicitly, and it is `daemon_threads` that matters rather than
        # `block_on_close`: in `socketserver.ThreadingMixIn` the two are
        # independent class attributes, not derived from each other. What joins
        # handler threads on `server_close` is `block_on_close`, left at its
        # default True — and that default only takes effect while
        # `daemon_threads` is False. Leave this True and an in-flight handler
        # could append to `calls` after `close()` returned.
        server.daemon_threads = False
        # Read the bound address back off the socket rather than trusting what
        # we asked for. `host_bound` is asserted against, and an assertion on
        # the argument we passed in would stay green if the bind itself changed.
        self.host_bound = server.server_address[0]
        self.port = server.server_address[1]
        self._server = server
        # A short poll interval: `serve_forever`'s default is 0.5s and
        # `shutdown` waits for the current poll to finish, so every teardown
        # paid up to half a second — which was most of one stub's runtime.
        self._thread = threading.Thread(
            target=lambda: server.serve_forever(poll_interval=0.02), daemon=True
        )
        self._thread.start()

    def close(self) -> None:
        """Stop serving and release the socket. Idempotent."""
        if self._server is not None:
            # `shutdown` before `server_close`: the former stops the serve loop
            # and blocks until it has, the latter releases the socket.
            # Reversed, the loop can be mid-`accept` on a closed fd.
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # -- addresses --------------------------------------------------------

    @property
    def url(self) -> str:
        """For a caller in this process. Always loopback."""
        return f"http://{LOOPBACK}:{self.port}"

    @property
    def container_url(self) -> str:
        """For a caller inside a container on this host."""
        return f"http://{FROM_CONTAINER}:{self.port}"

    # -- recording --------------------------------------------------------

    def record(self, call: ServiceCall) -> None:
        """Append one call, under the lock."""
        with self._lock:
            self.calls.append(call)

    def calls_matching(self, method: str = "", contains: str = "") -> list[ServiceCall]:
        """Recorded calls, narrowed. `calls` itself stays the whole record."""
        with self._lock:
            snapshot = list(self.calls)
        return [
            call
            for call in snapshot
            if (not method or call.method == method) and contains in call.path
        ]

    # -- the `Service` members a stub gets for free -----------------------

    def config_env(self) -> dict[str, str]:
        """No configuration by default.

        A stub the daemon reaches through seeded DB rows or a secrets-store
        entry rather than through `config.toml` returns nothing here, and says
        so in its own override — an empty dict invites the reader to think it
        was forgotten.
        """
        return {}

    def reset(self) -> None:
        """Clear the recorded calls. Subclasses override and call `super()`."""
        with self._lock:
            self.calls.clear()

    def describe(self) -> str:
        """The recorded calls, for `Stack.diagnostics`.

        `ServiceCall.__repr__` is what renders each one, and it is deliberately
        lossy: the body, the query and the headers can all carry a credential,
        and this string is printed into a failing test's output.
        """
        with self._lock:
            snapshot = list(self.calls)
        return "\n".join(f"  {call}" for call in snapshot) or "  (none)"
