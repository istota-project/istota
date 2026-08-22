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

**No credential reaches the report.** `ServiceCall.auth` records the *shape* of
the `Authorization` / `PRIVATE-TOKEN` header — scheme and length — never its
value, and everything this tier needs to assert ("a token was injected", "it was
not the ambient one") is satisfiable from that. The query and the body *are*
kept whole, because assertions need them, so the guarantee is about rendering
rather than about storage — see `ServiceCall.__repr__`.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import shutil
import subprocess
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from ..httpstub import LOOPBACK, HttpStub
from . import ServiceCall

# `git http-backend` streams a packfile; a clone of a seeded repo is tiny, but
# the bound keeps a wedged child from holding a handler thread for the session.
GIT_TIMEOUT = 120

# Whatever the stub answers for "who am I". glab asks before most write verbs.
STUB_USER = {"id": 1, "username": "istota-test", "name": "Istota Test"}

# The project the deployment tiers seed and work against, and the token they
# configure. Both live here rather than in a fixture: a service's own credential
# belongs to the service, and the tier that consumes it should not have to know
# how to spell one.
#
# The token is fabricated and deliberately does not wear a real forge prefix.
# The pre-commit scanner objects to `glpat-` on exactly the reasoning that a
# fake value with a real prefix is indistinguishable from a leak to anything
# reading the diff. Its *length* is what the assertions use, via
# `ServiceCall.auth`.
FORGE_TOKEN = "forge-token-for-the-smoke-tier"
FORGE_PROJECT = "istota-test/smoke-project"


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


def _password_accepted(headers, expected: str | None) -> bool:
    """Whether a git request's Basic password is the one we are waiting for.

    `None` accepts anything that carried a credential at all — the shape the
    loopback-bound default-suite tests use, since they have no token to expect.

    Never returns *why* it failed and never logs the value. A mismatch is a
    401, which is indistinguishable to the caller from having sent nothing, and
    that is the correct amount to say.
    """
    if expected is None:
        return True
    authorization = headers.get("Authorization") or ""
    scheme, _, encoded = authorization.partition(" ")
    if scheme.lower() != "basic" or not encoded:
        return False
    try:
        decoded = base64.b64decode(encoded, validate=True).decode("utf-8", "replace")
    except (ValueError, binascii.Error):
        return False
    # `partition`, not `split`: a password may legitimately contain a colon,
    # and only the first one separates it from the username.
    _, _, password = decoded.partition(":")
    return secrets.compare_digest(password, expected)


class GitLabService(HttpStub):
    """A running stub and the record of what it was asked."""

    name = "gitlab"

    def __init__(
        self,
        repo_root: Path,
        *,
        token: str = FORGE_TOKEN,
        project: str = FORGE_PROJECT,
        require_git_auth: bool = True,
    ) -> None:
        super().__init__()
        self.repo_root = repo_root
        self.token = token
        self.project = project
        self.require_git_auth = require_git_auth
        # Git-over-HTTP requests, kept apart from `calls` because they are a
        # different protocol answered by a different program. A scenario
        # asserting "one merge request was opened" must not have to filter out
        # the dozen ref-advertisement requests a clone makes.
        self.git_calls: list[ServiceCall] = []
        # Which repos `seed_repo` created, so `reset` can rebuild exactly those.
        self.seeded: list[str] = []

    @property
    def expect_git_password(self) -> str | None:
        """The password a git request must carry, or `None` to accept any.

        This is `HttpStub.start(credential=...)` under its original name, and
        two things turn on it.

        It is what makes `authenticated_git_calls()` mean "the credential helper
        produced the right token" rather than "something sent a header" — the
        helper shells out to `credential-fetch`, which asks the skill proxy, and
        only comparing the value proves that round trip happened.

        It is also the access control. The deployment tiers bind this listener
        to all interfaces so a container can reach it, and it serves a real
        `git http-backend` with GIT_HTTP_EXPORT_ALL — accepting any header at
        all would let anyone on the same network clone from and push to the
        seeded repos for the length of a run. `None` keeps the permissive
        behaviour for the loopback-bound default-suite tests, which have no
        token to expect, and `HttpStub.start` is what stops a non-loopback bind
        from reaching that state.
        """
        return self.credential

    # -- the `Service` members --------------------------------------------

    def config_env(self) -> dict[str, str]:
        """Turn the `[developer]` block on and point it at this stub.

        All six are read by `docker/istota/render-config.sh` and passed through
        by `docker/docker-compose.yml`. The block is therefore produced by the
        shipped generator rather than written by a fixture, so a change that
        breaks that generation fails in this tier rather than in production.
        """
        return {
            "ISTOTA_DEVELOPER_ENABLED": "true",
            # A tmpfs the compose file already declares. The developer skill
            # binds it read-write into the sandbox, which is where the scenarios
            # clone.
            "ISTOTA_DEVELOPER_REPOS_DIR": "/data/repos",
            "ISTOTA_DEVELOPER_GITLAB_URL": self.container_url,
            "ISTOTA_DEVELOPER_GITLAB_TOKEN": self.token,
            "ISTOTA_DEVELOPER_GITLAB_USERNAME": STUB_USER["username"],
            "ISTOTA_DEVELOPER_GITLAB_DEFAULT_NAMESPACE": self.project.split("/")[0],
        }

    def reset(self) -> None:
        """Forget the calls, and rebuild the seeded repositories.

        Rebuilt rather than merely cleared: the happy path pushes a branch, and
        the next test asserting "the branch landed" would pass on the previous
        test's push. `shutil.rmtree` then `seed_repo` is total in a way that
        deleting refs would not be — a scenario is free to create a tag, a note
        or a second branch, and none of that has to be enumerated here.
        """
        super().reset()
        with self._lock:
            self.git_calls.clear()
            seeded = list(self.seeded)
            self.seeded.clear()
        for path in seeded:
            bare = self.repo_root / f"{path}.git"
            shutil.rmtree(bare, ignore_errors=True)
            shutil.rmtree(bare.parent / f"{bare.stem}-seed", ignore_errors=True)
            self.seed_repo(path)

    def describe(self) -> str:
        """Both call lists, rendered apart.

        The default would fold the git traffic in with the REST traffic, and a
        failing forge scenario needs to tell "glab was never reached" from "git
        was never credentialed" — which are the same length of list and very
        different faults.
        """
        with self._lock:
            rest = list(self.calls)
            git = list(self.git_calls)
        return (
            "  -- rest --\n"
            + ("\n".join(f"    {call}" for call in rest) or "    (none)")
            + "\n  -- git --\n"
            + ("\n".join(f"    {call}" for call in git) or "    (none)")
        )

    # -- repositories -----------------------------------------------------

    def clone_url(self, path: str) -> str:
        """The address a container clones a seeded repo from."""
        return f"{self.container_url}/{path}.git"

    def seed_repo(self, path: str) -> str:
        """Create a bare repo with one commit and return its container clone URL.

        A real repository rather than a fixture directory: the happy path
        clones, branches, commits and pushes, and every one of those is a
        property of git talking to `git http-backend` over the credential
        helper. A stub that only answered REST would asserts nothing about the
        half of the chain that carries the token through git.
        """
        bare = self.repo_root / f"{path}.git"
        bare.parent.mkdir(parents=True, exist_ok=True)
        _git(["init", "--bare", "--initial-branch=main", str(bare)])
        # `http.receivepack` so a push over HTTP is accepted at all, and
        # `receive.denyCurrentBranch=ignore` because the bare repo's HEAD points
        # at main and a push to it is exactly what the happy path does.
        _git(["-C", str(bare), "config", "http.receivepack", "true"])
        _git(["-C", str(bare), "config", "receive.denyCurrentBranch", "ignore"])
        _seed_initial_commit(bare)
        with self._lock:
            if path not in self.seeded:
                self.seeded.append(path)
        return self.clone_url(path)

    def branches(self, path: str) -> list[str]:
        """Branch names in a seeded repo, for asserting a push landed."""
        bare = self.repo_root / f"{path}.git"
        out = _git(
            ["-C", str(bare), "for-each-ref", "--format=%(refname:short)", "refs/heads/"]
        )
        return [line.strip() for line in out.splitlines() if line.strip()]

    def rest_calls(self, method: str = "", contains: str = "") -> list[ServiceCall]:
        """The REST half of `calls_matching`, under the name the scenarios use.

        Kept as its own name rather than folded into the base: on this stub
        "calls" and "git calls" are two protocols on one listener, and a
        scenario reading `rest_calls` is saying which one it means.
        """
        return self.calls_matching(method, contains)

    def authenticated_git_calls(self) -> list[ServiceCall]:
        """Git requests that carried a credential.

        The one that matters is the push: git sends nothing until challenged,
        so a non-empty list here means the challenge was answered — which on
        the deployment path means the credential helper reached the skill proxy
        and got a token back.
        """
        with self._lock:
            return [call for call in self.git_calls if call.auth]


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

    stub: GitLabService = None  # set on the subclass built in `serve`

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

    def _read_body(self) -> bytes:
        length = int(self.headers.get("content-length") or 0)
        return self.rfile.read(length) if length else b""

    def _rest(self, method: str, path: str, query: dict) -> None:
        raw = self._read_body()
        call = ServiceCall(
            method=method,
            path=path,
            auth=_auth_shape(self.headers),
            body=raw,
            # `headers` is left empty deliberately. `ServiceCall` carries the
            # field for a stub whose assertion is about header bytes; here the
            # header block is where the credential is, `auth` already records
            # the only part of it an assertion needs, and storing the rest would
            # put the token back into a list that gets printed.
            query={k: v[0] if len(v) == 1 else v for k, v in query.items()},
        )
        self.stub.record(call)

        # `payload()` rather than a second parse here: a form-encoded body is
        # legal and glab uses one for some verbs, and the routing table and the
        # assertions must not disagree about which encoding arrived.
        status, payload = _route_rest(method, path, call.payload())
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

    def _record_git(self, method: str, path: str, auth: str) -> None:
        """Append to `git_calls`, which is not `HttpStub.calls`.

        The body is deliberately not kept: a `git-receive-pack` POST is a
        packfile, and a scenario asserting on a push reads the repository
        itself (`branches`) rather than the bytes that produced it.
        """
        with self.stub._lock:
            self.stub.git_calls.append(
                ServiceCall(method=method, path=path, auth=auth)
            )

    def _git_http(self, method: str, path: str, query: str) -> None:
        """Hand the request to `git http-backend`, which is a CGI program.

        Running the real backend rather than serving the loose objects: a push
        is a `receive-pack` negotiation, not a series of file reads, and the
        happy path pushes.
        """
        # Challenge first, like a private repo does. This is what makes the git
        # half of the chain assert anything: git sends no credential until it
        # is asked for one, so a stub that never challenges would let a push
        # succeed with the credential helper broken or absent — and the helper
        # is precisely the piece that fetches the token from the skill proxy.
        shape = _auth_shape(self.headers)
        accepted = bool(shape) and _password_accepted(
            self.headers, self.stub.expect_git_password
        )

        # Read the body *before* branching, always. On the 401 path this looks
        # like waste and is not: `protocol_version` is HTTP/1.1, so the socket
        # is reused, and a body left unread stays in the buffer to be parsed as
        # the next request line. Measured — an unauthenticated POST followed by
        # a perfectly valid `GET /api/v4/user` on the same connection answered
        # the second request out of the first one's packfile bytes, as an HTML
        # error page. The live shape is `git push`: whenever libcurl does not
        # pre-emptively re-send Basic auth on `POST /git-receive-pack`, the
        # push fails looking corrupt, which reads as a defect in the forge
        # chain. Intermittent by construction, and the tier exists to find that
        # class of thing rather than to produce it.
        length = int(self.headers.get("content-length") or 0)
        payload = self.rfile.read(length) if length else b""

        if self.stub.require_git_auth and not accepted:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Basic realm="istota-testbed-gitlab"')
            self.send_header("content-length", "0")
            self.end_headers()
            self._record_git(method, path, "")
            return

        self._record_git(method, path, shape)

        # After the body read, for the same keep-alive reason as the 401 above.
        root = self.stub.repo_root
        if root is None:
            self._json(500, {"error": "no repo root"})
            return

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

        # A backend that produced no header block wrote nothing usable, and
        # relaying that verbatim answers 200 with an empty body — git then
        # reports a corrupt response and the real reason, which is on stderr,
        # is gone. `diagnostics()` cannot recover it either.
        if result.returncode != 0 and not result.stdout:
            self._json(
                500,
                {
                    "error": "git http-backend exited "
                    f"{result.returncode}",
                    "stderr": (result.stderr or b"").decode("utf-8", "replace")[-2000:],
                },
            )
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
                # `"".split()` is `[]`, so an unguarded `[0]` raises on a bare
                # `Status:` — inside a handler thread whose `handle_error` is
                # deliberately silent, so the client sees a dropped connection
                # and no diagnostic. A non-numeric token does the same.
                tokens = value.split()
                try:
                    status = int(tokens[0]) if tokens else 200
                except ValueError:
                    status = 200
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
        "hint": "add it to testbed/services/gitlab.py:_route_rest",
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
    such courtesy, and `tests/test_gitlab_service.py` covers `mr create` in the
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
    token: str | None = None,
    project: str = FORGE_PROJECT,
    require_git_auth: bool = True,
) -> GitLabService:
    """Start the stub. Port 0 lets the OS choose, which is what keeps
    concurrent sessions from colliding.

    `host` defaults to loopback and only the deployment tiers override it.
    Binding all interfaces unconditionally would publish an unauthenticated
    listener — one that runs `git http-backend` — on every `uv run pytest`.

    `token` is both the credential the daemon is configured with and the git
    password this stub challenges for; they are one value because the whole
    point of the git assertion is that the credential helper produced *the*
    token rather than something. `None` means "accept any credential on the git
    path", which `HttpStub.start` allows only on a loopback bind.
    """
    repo_root.mkdir(parents=True, exist_ok=True)
    stub = GitLabService(
        repo_root,
        token=token or FORGE_TOKEN,
        project=project,
        require_git_auth=require_git_auth,
    )
    handler = type("_BoundHandler", (_Handler,), {"stub": stub})
    stub.start(handler, host=host, port=port, credential=token)
    return stub
