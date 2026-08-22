"""A GitLab stand-in: enough REST v4 for glab, plus a real git over HTTP.

The smoke tier's job is to prove the *chain* — executor, sandbox, skill proxy,
the forge wrapper, the deny policy, token injection, the real binary, a server
that answers. Whether the server is GitLab-shaped or GitHub-shaped changes
nothing about what the chain does, and GitLab is the one this deployment uses,
so one forge exercises all of it.

**GitLab and not GitHub, deliberately.** `glab` speaks REST v4 and nothing else,
so a stub is a handful of JSON endpoints. `gh` uses REST v3 *plus* GraphQL for
exactly the paths that matter — `gh pr create` resolves repo metadata through
`/api/graphql` before it posts — and those queries are an unpublished,
version-specific contract whose stub would break on gh upgrades in a way that
reads as a product bug. The gh path is asserted at unit level against
`build_invocation` instead, which is pure and needs no server.

**Plain HTTP, and that is a product property rather than a shortcut.** glab
discards the scheme inside `GITLAB_HOST`, so reaching a plain-HTTP forge needs a
per-host `api_protocol` in glab's own config — which the developer skill writes
(`_plain_http_host_entry`). This stub therefore doubles as the test that the
entry works: if it stops being written, nothing here is reachable and every
scenario fails on a TLS handshake.

**No credential is ever stored.** `ForgeCall.auth` records the *shape* of the
`Authorization` / `PRIVATE-TOKEN` header — scheme and length — never its value.
Under `--live` those headers carry a real token, and a failing assertion renders
the dataclass into the pytest report and the terminal, which is where pasted
credentials end up in a repo's history. Everything this tier needs to assert
("a token was injected", "it was not the ambient one") is satisfiable from the
shape.
"""

from __future__ import annotations

import json
import subprocess
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

# The host side connects over loopback; a container reaches the same listener by
# the Docker alias. Both are offered rather than guessed at the call site,
# because a smoke test needs them at once — it asserts against `calls`
# in-process while the daemon it drives talks to `container_url`.
LOOPBACK = "127.0.0.1"
FROM_CONTAINER = "host.docker.internal"

# `git http-backend` streams a packfile; a clone of a seeded repo is tiny, but
# the bound keeps a wedged child from holding a handler thread for the session.
GIT_TIMEOUT = 120

# Whatever the stub answers for "who am I". glab asks before most write verbs.
STUB_USER = {"id": 1, "username": "istota-test", "name": "Istota Test"}


@dataclass
class ForgeCall:
    """One REST request, with the credential reduced to its shape."""

    method: str
    path: str
    query: dict
    body: dict
    auth: str

    def __str__(self) -> str:  # pragma: no cover - diagnostic
        return f"{self.method} {self.path} auth={self.auth}"


def _auth_shape(headers) -> str:
    """`""`, `"bearer:<len>"` or `"private-token:<len>"` — never the value.

    Length rather than a bare boolean because it is what distinguishes an
    injected token from the ambient one in the negative assertions, and it
    carries no more information about the secret than `len()` does.
    """
    private = headers.get("PRIVATE-TOKEN")
    if private:
        return f"private-token:{len(private)}"
    authorization = headers.get("Authorization") or ""
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() == "bearer" and value:
        return f"bearer:{len(value)}"
    if authorization:
        # An unrecognised scheme is worth distinguishing from none at all, and
        # its *name* is not a secret. The value is still dropped.
        return f"{scheme.lower() or 'unknown'}:{len(value or authorization)}"
    return ""


@dataclass
class FakeGitLab:
    """A running stub and the record of what it was asked."""

    port: int = 0
    host_bound: str = LOOPBACK
    calls: list[ForgeCall] = field(default_factory=list)
    # Git-over-HTTP requests, kept apart from `calls` because they are a
    # different protocol answered by a different program. A scenario asserting
    # "one merge request was opened" must not have to filter out the dozen
    # ref-advertisement requests a clone makes.
    git_calls: list[ForgeCall] = field(default_factory=list)
    require_git_auth: bool = True
    repo_root: Path | None = None
    _server: ThreadingHTTPServer | None = None
    _thread: threading.Thread | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def url(self) -> str:
        """For a caller in this process."""
        return f"http://{LOOPBACK}:{self.port}"

    @property
    def container_url(self) -> str:
        """For a caller inside a container on this host."""
        return f"http://{FROM_CONTAINER}:{self.port}"

    def seed_repo(self, path: str) -> str:
        """Create a bare repo with one commit and return its container clone URL.

        A real repository rather than a fixture directory: the happy path
        clones, branches, commits and pushes, and every one of those is a
        property of git talking to `git http-backend` over the credential
        helper. A stub that only answered REST would asserts nothing about the
        half of the chain that carries the token through git.
        """
        if self.repo_root is None:
            raise RuntimeError("FakeGitLab was not started with a repo root")
        bare = self.repo_root / f"{path}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        _git(["init", "--bare", "--initial-branch=main", str(bare)])
        # `http.receivepack` so a push over HTTP is accepted at all, and
        # `receive.denyCurrentBranch=ignore` because the bare repo's HEAD points
        # at main and a push to it is exactly what the happy path does.
        _git(["-C", str(bare), "config", "http.receivepack", "true"])
        _git(["-C", str(bare), "config", "receive.denyCurrentBranch", "ignore"])
        _seed_initial_commit(bare)
        return f"{self.container_url}/{path}.git"

    def branches(self, path: str) -> list[str]:
        """Branch names in a seeded repo, for asserting a push landed."""
        if self.repo_root is None:
            return []
        bare = self.repo_root / f"{path}.git"
        out = _git(
            ["-C", str(bare), "for-each-ref", "--format=%(refname:short)", "refs/heads/"]
        )
        return [line.strip() for line in out.splitlines() if line.strip()]

    def rest_calls(self, method: str = "", contains: str = "") -> list[ForgeCall]:
        """Recorded calls, narrowed. `calls` itself stays the whole record."""
        with self._lock:
            snapshot = list(self.calls)
        return [
            call
            for call in snapshot
            if (not method or call.method == method) and contains in call.path
        ]

    def authenticated_git_calls(self) -> list[ForgeCall]:
        """Git requests that carried a credential.

        The one that matters is the push: git sends nothing until challenged,
        so a non-empty list here means the challenge was answered — which on
        the deployment path means the credential helper reached the skill proxy
        and got a token back.
        """
        with self._lock:
            return [call for call in self.git_calls if call.auth]

    def close(self) -> None:
        if self._server is not None:
            # `shutdown` before `server_close`: the former stops the serve loop
            # and blocks until it has, the latter releases the socket.
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    def __enter__(self) -> FakeGitLab:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def _git(argv: list[str], **kwargs) -> str:
    result = subprocess.run(
        ["git", *argv], capture_output=True, text=True, timeout=60, **kwargs
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(argv)} exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
    return result.stdout


def _seed_initial_commit(bare: Path) -> None:
    """One commit on main, made in a scratch clone and pushed into the bare repo.

    Via a clone rather than plumbing (`hash-object` / `mktree` / `commit-tree`)
    because the plumbing route needs an index and an author identity configured
    anyway, and this way the seeded history is one an ordinary git produced.
    """
    scratch = bare.parent / f"{bare.stem}-seed"
    _git(["clone", str(bare), str(scratch)])
    (scratch / "README.md").write_text("# test repo\n")
    _git(["-C", str(scratch), "add", "README.md"])
    _git(
        [
            "-C", str(scratch),
            "-c", "user.email=seed@example.com",
            "-c", "user.name=Seed",
            "commit", "-m", "Initial commit",
        ]
    )
    _git(["-C", str(scratch), "push", "origin", "main"])


class _Handler(BaseHTTPRequestHandler):
    """Routes `/api/v4/*` to the REST stub and everything else to git."""

    protocol_version = "HTTP/1.1"
    # Bounds a parked keep-alive connection, so `server_close`'s join cannot be
    # held by a client that connected and went quiet.
    timeout = 30

    stub: FakeGitLab = None  # set on the subclass built in `serve`

    # Stdlib hook names, so they are not ours to rename. No `noqa` codes: the
    # project pins ruff to E4/E7/E9/F, so a suppression for N802 would name a
    # rule that is not enabled.
    def log_message(self, *args) -> None:
        """Silence. One line per request to stderr otherwise, which pytest
        attaches to whichever test happens to be running."""

    def handle_error(self, request, client_address) -> None:
        """Silence too. An unhandled exception in a handler thread prints a
        full traceback to stderr that lands in an unrelated test's output."""

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:
        self._dispatch("GET")

    def do_POST(self) -> None:
        self._dispatch("POST")

    def do_PUT(self) -> None:
        self._dispatch("PUT")

    def do_HEAD(self) -> None:
        self._dispatch("HEAD")

    def _dispatch(self, method: str) -> None:
        split = urlsplit(self.path)
        if split.path.startswith("/api/"):
            self._rest(method, split.path, parse_qs(split.query))
        else:
            self._git_http(method, split.path, split.query)

    # -- REST -------------------------------------------------------------

    def _read_body(self) -> tuple[bytes, dict]:
        length = int(self.headers.get("content-length") or 0)
        raw = self.rfile.read(length) if length else b""
        try:
            parsed = json.loads(raw or b"{}")
        except ValueError:
            # A form-encoded body is legal and glab uses one for some verbs.
            # Recorded as a flat dict so an assertion does not have to know
            # which encoding the CLI happened to pick.
            parsed = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        return raw, parsed if isinstance(parsed, dict) else {"_body": parsed}

    def _rest(self, method: str, path: str, query: dict) -> None:
        _, body = self._read_body()
        call = ForgeCall(
            method=method,
            path=path,
            query={k: v[0] if len(v) == 1 else v for k, v in query.items()},
            body=body,
            auth=_auth_shape(self.headers),
        )
        with self.stub._lock:
            self.stub.calls.append(call)

        status, payload = _route_rest(method, path, body)
        self._json(status, payload)

    def _json(self, status: int, payload) -> None:
        encoded = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(encoded)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(encoded)

    # -- git over HTTP ----------------------------------------------------

    def _git_http(self, method: str, path: str, query: str) -> None:
        """Hand the request to `git http-backend`, which is a CGI program.

        Running the real backend rather than serving the loose objects: a push
        is a `receive-pack` negotiation, not a series of file reads, and the
        happy path pushes.
        """
        root = self.stub.repo_root
        if root is None:
            self._json(500, {"error": "no repo root"})
            return

        # Challenge first, like a private repo does. This is what makes the git
        # half of the chain assert anything: git sends no credential until it
        # is asked for one, so a stub that never challenges would let a push
        # succeed with the credential helper broken or absent — and the helper
        # is precisely the piece that fetches the token from the skill proxy.
        shape = _auth_shape(self.headers)
        if self.stub.require_git_auth and not shape:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="fake-gitlab"')
            self.send_header("content-length", "0")
            self.end_headers()
            with self.stub._lock:
                self.stub.git_calls.append(
                    ForgeCall(method=method, path=path, query={}, body={}, auth="")
                )
            return

        with self.stub._lock:
            self.stub.git_calls.append(
                ForgeCall(method=method, path=path, query={}, body={}, auth=shape)
            )

        length = int(self.headers.get("content-length") or 0)
        payload = self.rfile.read(length) if length else b""

        environment = {
            "GIT_PROJECT_ROOT": str(root),
            # Every repo under the root is exported. The root is a per-test
            # temp directory holding only what `seed_repo` put there.
            "GIT_HTTP_EXPORT_ALL": "1",
            "PATH_INFO": path,
            "QUERY_STRING": query,
            "REQUEST_METHOD": method,
            "CONTENT_TYPE": self.headers.get("content-type") or "",
            "CONTENT_LENGTH": str(length),
            "REMOTE_USER": "istota-test",
            "REMOTE_ADDR": self.client_address[0],
            # http-backend reads this to decide whether to advertise
            # receive-pack; the per-repo config says yes as well.
            "GIT_HTTP_MAX_REQUEST_BUFFER": "100M",
        }
        for header, name in (
            ("Content-Encoding", "HTTP_CONTENT_ENCODING"),
            ("Accept", "HTTP_ACCEPT"),
            ("Git-Protocol", "HTTP_GIT_PROTOCOL"),
        ):
            value = self.headers.get(header)
            if value:
                environment[name] = value

        try:
            result = subprocess.run(
                ["git", "http-backend"],
                input=payload,
                capture_output=True,
                timeout=GIT_TIMEOUT,
                env={**_base_env(), **environment},
            )
        except (subprocess.SubprocessError, OSError) as exc:
            self._json(500, {"error": f"git http-backend failed: {exc}"})
            return

        self._write_cgi(result.stdout)

    def _write_cgi(self, raw: bytes) -> None:
        """Split the CGI headers off the body and relay both.

        `http-backend` emits a `Status:` header for anything that is not 200,
        so the status has to come out of the header block rather than being
        assumed — a 404 relayed as 200 makes git report a corrupt response
        instead of a missing repository.
        """
        head, _, body = raw.partition(b"\r\n\r\n")
        if not _:
            head, _, body = raw.partition(b"\n\n")
        status = 200
        headers = []
        for line in head.replace(b"\r\n", b"\n").split(b"\n"):
            if not line.strip():
                continue
            name, _, value = line.decode("latin-1").partition(":")
            value = value.strip()
            if name.lower() == "status":
                status = int(value.split()[0] or 200)
            else:
                headers.append((name, value))

        self.send_response(status)
        for name, value in headers:
            if name.lower() == "content-length":
                continue
            self.send_header(name, value)
        # Our own length, always: http-backend streams chunked responses
        # without one, and relaying no length on HTTP/1.1 makes git wait for a
        # close that keep-alive never delivers.
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)


def _base_env() -> dict:
    """A minimal environment for the git child.

    Explicit rather than `os.environ`: the developer's own `GIT_*` settings
    would otherwise reach the backend, and `GIT_DIR` in particular makes it
    serve whatever repository the terminal happened to be in.
    """
    import os

    return {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": "C",
    }


def _route_rest(method: str, path: str, body: dict) -> tuple[int, object]:
    """The endpoint table, deliberately minimal.

    Anything unimplemented answers 501 with the path in the body, so a missing
    endpoint reports itself precisely rather than surfacing three layers later
    as a glab error about a malformed response. The set below is what `glab mr
    create`, `mr view`, `mr list`, `ci status` and `repo view` were observed to
    need; extend it by reading a 501.
    """
    parts = [segment for segment in path.split("/") if segment]
    # ["api", "v4", ...]
    rest = parts[2:]

    if rest == ["user"]:
        return 200, STUB_USER
    if rest == ["version"]:
        return 200, {"version": "17.0.0-stub", "revision": "stub"}

    if rest and rest[0] == "projects" and len(rest) >= 2:
        project = rest[1]
        tail = rest[2:]
        if not tail:
            return 200, _project(project)
        if tail == ["merge_requests"] and method == "POST":
            return 201, _merge_request(project, body)
        if tail == ["merge_requests"] and method == "GET":
            return 200, []
        if tail[:1] == ["repository"] and tail[1:2] == ["branches"]:
            return 200, []
        if tail == ["pipelines"]:
            return 200, []

    return 501, {
        "error": "not implemented by the stub",
        "method": method,
        "path": path,
        "hint": "add it to tests/smoke/fake_gitlab.py:_route_rest",
    }


def _project(identifier: str) -> dict:
    """One project, with the fields glab dereferences without checking.

    **`http_url_to_repo` is not optional and its absence is not an error.**
    glab 1.114.0 resolves the head repo by matching the git remote against the
    project's own clone URLs, and with that field missing it panics — a nil
    dereference in `glrepo.(*ResolvedRemotes).HeadRepo`, so `glab mr create`
    dies on SIGSEGV with a Go traceback and no message about what was wrong.
    The same goes for the rest of the padding here: none of it is read by an
    assertion, all of it is read by glab.

    So this is a payload to extend rather than trim. `_route_rest` answers 501
    for a missing *endpoint*, which reports itself; a missing *field* has no
    such courtesy, and `tests/test_fake_gitlab.py` covers `mr create` in the
    default suite precisely so a regression here costs two seconds rather than
    a compose stack.
    """
    from urllib.parse import unquote

    full = unquote(identifier)
    name = full.rsplit("/", 1)[-1]
    return {
        "id": 1,
        "path": name,
        "name": name,
        "path_with_namespace": full if "/" in full else f"istota-test/{full}",
        "default_branch": "main",
        "visibility": "private",
        "web_url": f"http://{LOOPBACK}/{full}",
        "http_url_to_repo": f"http://{LOOPBACK}/{full}.git",
        "ssh_url_to_repo": f"git@{LOOPBACK}:{full}.git",
        "forked_from_project": None,
        "empty_repo": False,
        "archived": False,
        "owner": STUB_USER,
        "namespace": {"id": 1, "path": "istota-test", "full_path": "istota-test", "kind": "group"},
        "permissions": {"project_access": {"access_level": 40}},
        "merge_requests_enabled": True,
    }


def _merge_request(project: str, body: dict) -> dict:
    return {
        "id": 1,
        "iid": 1,
        "project_id": 1,
        "title": body.get("title") or "Untitled",
        "description": body.get("description") or "",
        "source_branch": body.get("source_branch") or "",
        "target_branch": body.get("target_branch") or "main",
        "state": "opened",
        "web_url": f"http://{LOOPBACK}/{project}/-/merge_requests/1",
        "author": STUB_USER,
    }


def serve(
    repo_root: Path,
    *,
    host: str = LOOPBACK,
    port: int = 0,
    require_git_auth: bool = True,
) -> FakeGitLab:
    """Start the stub. Port 0 lets the OS choose, which is what keeps
    concurrent sessions from colliding.

    `host` defaults to loopback and only the smoke tier overrides it. Binding
    all interfaces unconditionally would publish an unauthenticated listener —
    one that runs `git http-backend` — on every `uv run pytest`.
    """
    repo_root.mkdir(parents=True, exist_ok=True)
    stub = FakeGitLab(
        host_bound=host, repo_root=repo_root, require_git_auth=require_git_auth
    )

    handler = type("_BoundHandler", (_Handler,), {"stub": stub})
    server = ThreadingHTTPServer((host, port), handler)
    # `block_on_close` is `not daemon_threads`, so leaving this True means
    # `server_close` does not join handler threads — an in-flight handler could
    # append to `calls` after `close()` returned.
    server.daemon_threads = False
    # Read the bound address back off the socket rather than trusting what we
    # asked for: `host_bound` is asserted against, and an assertion on the
    # argument we passed in would stay green if the bind itself changed.
    stub.host_bound = server.server_address[0]
    stub.port = server.server_address[1]
    stub._server = server
    stub._thread = threading.Thread(
        target=lambda: server.serve_forever(poll_interval=0.02), daemon=True
    )
    stub._thread.start()
    return stub
