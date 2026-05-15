"""Tests for the in-container devbox proxy shim scripts (Stage 4).

The scripts under ``docker/devbox/scripts/`` are intended to run inside
the devbox container, talking to a Unix socket bind-mounted from the
host. These tests run them as subprocesses against a real istota daemon
listening on a tmpdir socket, with ``ISTOTA_CRED_SOCK`` and
``ISTOTA_DEVBOX_LIB`` env vars pointed at the test fixtures.

Each subcommand of the curated ``gh`` / ``glab`` shim set gets one
routing test; one test confirms unrouted subcommands exit 2 with the
expected message; the credential helper and ``gitlab-api``/``github-api``
get happy-path + error-path coverage.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx
import pytest

from istota.devbox_proxy import DevboxProxyContext, handle_connection


REPO = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO / "docker" / "devbox" / "scripts"
LIB_DIR = REPO / "docker" / "devbox" / "lib"


# ---- Fixtures --------------------------------------------------------------


@pytest.fixture()
def sock_path():
    """Short Unix-socket path under /tmp (AF_UNIX 104-char limit on macOS)."""
    dirpath = Path(tempfile.mkdtemp(prefix="dvbx_shim_", dir="/tmp"))
    try:
        yield dirpath / "p.sock"
    finally:
        shutil.rmtree(dirpath, ignore_errors=True)


def _ctx(
    *,
    user_id: str = "alice",
    gitlab_token: str = "GL-TOKEN",
    github_token: str = "GH-TOKEN",
    gitlab_url: str = "https://gitlab.com",
    github_url: str = "https://github.com",
    gitlab_allowlist: tuple[str, ...] = (
        "GET /projects/*",
        "POST /projects/*/merge_requests",
        "GET /projects/*/merge_requests",
        "PUT /projects/*/merge_requests/*",
        "GET /projects/*/issues",
        "POST /projects/*/issues",
        "GET /user",
    ),
    github_allowlist: tuple[str, ...] = (
        "GET /repos/*",
        "POST /repos/*/pulls",
        "GET /repos/*/pulls",
        "PATCH /repos/*/pulls/*",
        "GET /repos/*/issues",
        "POST /repos/*/issues",
        "GET /user",
    ),
    api_timeout: float = 5.0,
    http_handler=None,
) -> DevboxProxyContext:
    if http_handler is None:
        def http_handler(request):
            return httpx.Response(200, text="")
    transport = httpx.MockTransport(http_handler)
    client = httpx.AsyncClient(transport=transport, timeout=api_timeout)
    return DevboxProxyContext(
        user_id=user_id,
        gitlab_token=gitlab_token,
        github_token=github_token,
        gitlab_url=gitlab_url,
        github_url=github_url,
        gitlab_allowlist=gitlab_allowlist,
        github_allowlist=github_allowlist,
        api_timeout=api_timeout,
        http_client=client,
    )


class FakeDaemon:
    """Asyncio-thread daemon recording requests + returning canned responses.

    Started in its own thread so subprocess tests stay simple — no asyncio
    bridging in the test body.
    """

    def __init__(self, sock_path: Path, ctx: DevboxProxyContext):
        self.sock_path = sock_path
        self.ctx = ctx
        self.thread: object | None = None
        self.loop: asyncio.AbstractEventLoop | None = None
        self.server: asyncio.AbstractServer | None = None
        self._ready = None
        self._stop = None

    def start(self):
        import threading

        ready = threading.Event()
        self._ready = ready

        def runner():
            loop = asyncio.new_event_loop()
            self.loop = loop
            asyncio.set_event_loop(loop)
            self._stop = loop.create_future()

            async def cb(reader, writer):
                await handle_connection(reader, writer, self.ctx)

            async def main():
                self.server = await asyncio.start_unix_server(
                    cb, path=str(self.sock_path),
                )
                os.chmod(str(self.sock_path), 0o600)
                ready.set()
                async with self.server:
                    await self._stop

            loop.run_until_complete(main())
            loop.close()

        self.thread = threading.Thread(target=runner, daemon=True)
        self.thread.start()
        ready.wait(timeout=5)

    def stop(self):
        if self.loop and self._stop and not self._stop.done():
            self.loop.call_soon_threadsafe(self._stop.set_result, None)
        if self.thread:
            self.thread.join(timeout=5)


@pytest.fixture()
def daemon_factory(sock_path):
    instances: list[FakeDaemon] = []

    def factory(ctx):
        d = FakeDaemon(sock_path, ctx)
        d.start()
        instances.append(d)
        return d

    yield factory
    for d in instances:
        d.stop()


def _run_script(script: str, args: list[str], *, sock_path: Path, stdin: str = "", env_extra: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke a shim script as a subprocess pointed at our test socket."""
    env = os.environ.copy()
    env["ISTOTA_CRED_SOCK"] = str(sock_path)
    env["ISTOTA_DEVBOX_LIB"] = str(LIB_DIR)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, str(SCRIPTS_DIR / script), *args],
        input=stdin, env=env,
        capture_output=True, text=True,
        timeout=10,
    )


# ---- git-credential-istota -------------------------------------------------


class TestGitCredentialHelper:
    def test_get_github_returns_credentials_on_stdout(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        stdin = "protocol=https\nhost=github.com\n"
        result = _run_script(
            "git-credential-istota", ["get"],
            sock_path=sock_path, stdin=stdin,
        )
        assert result.returncode == 0, result.stderr
        assert "username=x-access-token" in result.stdout
        assert "password=GH-TOKEN" in result.stdout
        assert "host=github.com" in result.stdout

    def test_get_gitlab_returns_credentials(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        stdin = "protocol=https\nhost=gitlab.com\n"
        result = _run_script(
            "git-credential-istota", ["get"],
            sock_path=sock_path, stdin=stdin,
        )
        assert result.returncode == 0
        assert "password=GL-TOKEN" in result.stdout

    def test_get_unknown_host_exits_zero_with_empty_stdout(self, sock_path, daemon_factory):
        """Git treats missing password= lines as 'no credential' — that's
        the right outcome for hosts the daemon doesn't have a token for."""
        daemon_factory(_ctx())
        stdin = "protocol=https\nhost=bitbucket.org\n"
        result = _run_script(
            "git-credential-istota", ["get"],
            sock_path=sock_path, stdin=stdin,
        )
        assert result.returncode == 0
        assert "password=" not in result.stdout

    def test_store_is_quiet_noop(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        stdin = "protocol=https\nhost=github.com\npassword=anything\n"
        result = _run_script(
            "git-credential-istota", ["store"],
            sock_path=sock_path, stdin=stdin,
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_erase_is_quiet_noop(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        result = _run_script(
            "git-credential-istota", ["erase"],
            sock_path=sock_path, stdin="",
        )
        assert result.returncode == 0
        assert result.stdout == ""

    def test_unknown_op_exits_2(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        result = _run_script(
            "git-credential-istota", ["approve"],
            sock_path=sock_path, stdin="",
        )
        assert result.returncode == 2
        assert "unknown" in result.stderr.lower() or "approve" in result.stderr

    def test_proxy_unreachable_exits_1(self, sock_path):
        """Without a daemon running, the helper should fail with a clear
        operator-targeted message."""
        # No daemon — socket file doesn't exist.
        result = _run_script(
            "git-credential-istota", ["get"],
            sock_path=sock_path, stdin="protocol=https\nhost=github.com\n",
        )
        assert result.returncode == 1
        assert "unreachable" in result.stderr.lower()


# ---- gitlab-api / github-api ----------------------------------------------


class TestApiWrappers:
    def test_gitlab_api_happy_get_prints_body(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text='{"id":42}')

        daemon_factory(_ctx(http_handler=handler))
        result = _run_script(
            "gitlab-api",
            ["--method", "GET", "--endpoint", "/projects/42"],
            sock_path=sock_path,
        )
        assert result.returncode == 0, result.stderr
        assert '{"id":42}' in result.stdout
        # Sanity check upstream was actually hit.
        assert len(seen) == 1
        assert str(seen[0].url) == "https://gitlab.com/api/v4/projects/42"

    def test_gitlab_api_post_with_body_inline(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(201, text='{"iid":1}')

        daemon_factory(_ctx(http_handler=handler))
        result = _run_script(
            "gitlab-api",
            [
                "--method", "POST",
                "--endpoint", "/projects/42/merge_requests",
                "--body", '{"title":"x","source_branch":"f","target_branch":"main"}',
            ],
            sock_path=sock_path,
        )
        assert result.returncode == 0, result.stderr
        assert '"iid":1' in result.stdout
        assert seen[0].content.decode("utf-8") == '{"title":"x","source_branch":"f","target_branch":"main"}'

    def test_gitlab_api_post_with_body_stdin(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(201, text='{}')

        daemon_factory(_ctx(http_handler=handler))
        body = '{"title":"from stdin"}'
        result = _run_script(
            "gitlab-api",
            [
                "--method", "POST",
                "--endpoint", "/projects/42/merge_requests",
                "--body-stdin",
            ],
            sock_path=sock_path, stdin=body,
        )
        assert result.returncode == 0, result.stderr
        assert seen[0].content.decode("utf-8") == body

    def test_gitlab_api_repeatable_header(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="{}")

        daemon_factory(_ctx(http_handler=handler))
        result = _run_script(
            "gitlab-api",
            [
                "--method", "GET", "--endpoint", "/projects/42",
                "--header", "X-Trace=abc",
                "--header", "X-Foo=bar",
            ],
            sock_path=sock_path,
        )
        assert result.returncode == 0
        assert seen[0].headers["X-Trace"] == "abc"
        assert seen[0].headers["X-Foo"] == "bar"

    def test_gitlab_api_upstream_4xx_exits_1_and_prints_body(self, sock_path, daemon_factory):
        def handler(request):
            return httpx.Response(422, text='{"error":"invalid"}')

        daemon_factory(_ctx(http_handler=handler))
        result = _run_script(
            "gitlab-api",
            ["--method", "POST", "--endpoint", "/projects/42/merge_requests",
             "--body", '{"title":""}'],
            sock_path=sock_path,
        )
        assert result.returncode == 1
        # Body still printed on stdout so callers can inspect.
        assert '"error":"invalid"' in result.stdout
        # Human message goes to stderr.
        assert "upstream" in result.stderr.lower() or "422" in result.stderr

    def test_gitlab_api_not_allowed_endpoint_exits_1(self, sock_path, daemon_factory):
        called = []

        def handler(request):
            called.append(request)
            return httpx.Response(200, text="{}")

        daemon_factory(
            _ctx(
                gitlab_allowlist=("GET /projects/*",),
                http_handler=handler,
            )
        )
        result = _run_script(
            "gitlab-api",
            ["--method", "DELETE", "--endpoint", "/projects/42"],
            sock_path=sock_path,
        )
        assert result.returncode == 1
        assert called == []
        assert "not in allowlist" in result.stderr or "not_allowed" in result.stderr.lower() or "allowlist" in result.stderr

    def test_github_api_happy_post(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(201, text='{"number":99,"html_url":"https://x"}')

        daemon_factory(_ctx(http_handler=handler))
        result = _run_script(
            "github-api",
            [
                "--method", "POST",
                "--endpoint", "/repos/foo/bar/pulls",
                "--body", '{"title":"x","head":"f","base":"main"}',
            ],
            sock_path=sock_path,
        )
        assert result.returncode == 0, result.stderr
        assert '"number":99' in result.stdout
        assert str(seen[0].url) == "https://api.github.com/repos/foo/bar/pulls"
        assert seen[0].headers["Authorization"] == "token GH-TOKEN"

    def test_github_api_missing_endpoint_arg_exits_nonzero(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        result = _run_script(
            "github-api", ["--method", "GET"],
            sock_path=sock_path,
        )
        assert result.returncode != 0
        assert "endpoint" in result.stderr.lower()


# ---- gh shim --------------------------------------------------------------


def _run_gh(args, *, sock_path, slug="foo/bar", **kwargs):
    return _run_script(
        "gh", args, sock_path=sock_path,
        env_extra={"ISTOTA_DEVBOX_REPO_SLUG": slug, **kwargs.pop("env_extra", {})},
        **kwargs,
    )


class TestGhShim:
    def test_auth_status_routes_to_user_endpoint(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text='{"login":"alice"}')

        daemon_factory(_ctx(http_handler=handler))
        result = _run_gh(["auth", "status"], sock_path=sock_path)
        assert result.returncode == 0, result.stderr
        assert '"login":"alice"' in result.stdout
        assert str(seen[0].url) == "https://api.github.com/user"

    def test_repo_view_routes_to_repos_owner_repo(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text='{"name":"bar"}')

        daemon_factory(_ctx(http_handler=handler))
        result = _run_gh(["repo", "view"], sock_path=sock_path, slug="foo/bar")
        assert result.returncode == 0, result.stderr
        assert str(seen[0].url) == "https://api.github.com/repos/foo/bar"

    def test_pr_create_posts_to_pulls_with_body(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(201, text='{"number":42,"html_url":"https://x"}')

        daemon_factory(_ctx(http_handler=handler))
        result = _run_gh(
            [
                "pr", "create",
                "--title", "Add foo",
                "--body", "see desc",
                "--base", "main",
                "--head", "feature/x",
            ],
            sock_path=sock_path,
        )
        assert result.returncode == 0, result.stderr
        assert '"number":42' in result.stdout
        sent = seen[0]
        assert sent.method == "POST"
        assert str(sent.url) == "https://api.github.com/repos/foo/bar/pulls"
        body = json.loads(sent.content.decode("utf-8"))
        assert body == {
            "title": "Add foo",
            "body": "see desc",
            "base": "main",
            "head": "feature/x",
        }

    def test_pr_view_routes_with_number(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="{}")

        daemon_factory(_ctx(http_handler=handler))
        result = _run_gh(["pr", "view", "7"], sock_path=sock_path)
        assert result.returncode == 0, result.stderr
        assert str(seen[0].url) == "https://api.github.com/repos/foo/bar/pulls/7"

    def test_pr_list_carries_state_query(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="[]")

        daemon_factory(_ctx(http_handler=handler))
        result = _run_gh(
            ["pr", "list", "--state", "all"], sock_path=sock_path,
        )
        assert result.returncode == 0, result.stderr
        assert str(seen[0].url) == "https://api.github.com/repos/foo/bar/pulls?state=all"

    def test_pr_close_patches_with_closed_state(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="{}")

        daemon_factory(_ctx(http_handler=handler))
        result = _run_gh(["pr", "close", "9"], sock_path=sock_path)
        assert result.returncode == 0, result.stderr
        assert seen[0].method == "PATCH"
        assert str(seen[0].url) == "https://api.github.com/repos/foo/bar/pulls/9"
        assert json.loads(seen[0].content.decode("utf-8")) == {"state": "closed"}

    def test_issue_create_routes_to_issues(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(201, text='{"number":3}')

        daemon_factory(_ctx(http_handler=handler))
        result = _run_gh(
            ["issue", "create", "--title", "Bug X", "--body", "details"],
            sock_path=sock_path,
        )
        assert result.returncode == 0, result.stderr
        assert seen[0].method == "POST"
        assert str(seen[0].url) == "https://api.github.com/repos/foo/bar/issues"
        assert json.loads(seen[0].content.decode("utf-8")) == {
            "title": "Bug X", "body": "details",
        }

    def test_issue_view_routes_with_number(self, sock_path, daemon_factory):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="{}")

        daemon_factory(_ctx(http_handler=handler))
        result = _run_gh(["issue", "view", "12"], sock_path=sock_path)
        assert result.returncode == 0
        assert str(seen[0].url) == "https://api.github.com/repos/foo/bar/issues/12"

    def test_issue_list_carries_state(self, sock_path, daemon_factory):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="[]")

        daemon_factory(_ctx(http_handler=handler))
        result = _run_gh(["issue", "list"], sock_path=sock_path)
        assert result.returncode == 0
        assert "state=open" in str(seen[0].url)

    def test_unrouted_subcommand_exits_2(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        result = _run_gh(["release", "create"], sock_path=sock_path)
        assert result.returncode == 2
        assert "not yet routed" in result.stderr
        assert "github-api" in result.stderr

    def test_unrouted_pr_subcommand_exits_2(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        result = _run_gh(["pr", "merge"], sock_path=sock_path)
        assert result.returncode == 2
        assert "not yet routed" in result.stderr

    def test_missing_repo_slug_exits_1_with_clear_message(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        # No ISTOTA_DEVBOX_REPO_SLUG, and pytest's cwd is a real git repo
        # whose origin doesn't match foo/bar — call repo view explicitly
        # in a tmp dir so git remote get-url fails.
        with tempfile.TemporaryDirectory(prefix="dvbx_norepo_", dir="/tmp") as td:
            env = os.environ.copy()
            env["ISTOTA_CRED_SOCK"] = str(sock_path)
            env["ISTOTA_DEVBOX_LIB"] = str(LIB_DIR)
            result = subprocess.run(
                [sys.executable, str(SCRIPTS_DIR / "gh"), "repo", "view"],
                cwd=td, env=env,
                capture_output=True, text=True, timeout=10,
            )
        assert result.returncode == 1
        assert "remote" in result.stderr.lower() or "repo" in result.stderr.lower()


# ---- glab shim ------------------------------------------------------------


def _run_glab(args, *, sock_path, slug="ns/path", **kwargs):
    return _run_script(
        "glab", args, sock_path=sock_path,
        env_extra={"ISTOTA_DEVBOX_REPO_SLUG": slug, **kwargs.pop("env_extra", {})},
        **kwargs,
    )


class TestGlabShim:
    def test_auth_status_routes_to_user(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text='{"username":"alice"}')

        daemon_factory(_ctx(http_handler=handler))
        result = _run_glab(["auth", "status"], sock_path=sock_path)
        assert result.returncode == 0, result.stderr
        assert str(seen[0].url) == "https://gitlab.com/api/v4/user"

    def test_repo_view_routes_with_urlencoded_slug(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text='{"id":42}')

        daemon_factory(_ctx(http_handler=handler))
        result = _run_glab(["repo", "view"], sock_path=sock_path, slug="myns/myrepo")
        assert result.returncode == 0, result.stderr
        # GitLab API takes URL-encoded namespace/path.
        assert str(seen[0].url) == "https://gitlab.com/api/v4/projects/myns%2Fmyrepo"

    def test_mr_create_posts_with_body(self, sock_path, daemon_factory):
        seen: list[httpx.Request] = []

        def handler(request):
            seen.append(request)
            return httpx.Response(201, text='{"iid":3}')

        daemon_factory(_ctx(http_handler=handler))
        result = _run_glab(
            [
                "mr", "create",
                "--title", "Add foo",
                "--description", "desc",
                "--source-branch", "feature/x",
                "--target-branch", "main",
            ],
            sock_path=sock_path,
        )
        assert result.returncode == 0, result.stderr
        assert '"iid":3' in result.stdout
        sent = seen[0]
        assert sent.method == "POST"
        assert str(sent.url) == "https://gitlab.com/api/v4/projects/ns%2Fpath/merge_requests"
        body = json.loads(sent.content.decode("utf-8"))
        assert body == {
            "title": "Add foo",
            "description": "desc",
            "source_branch": "feature/x",
            "target_branch": "main",
        }

    def test_mr_view_routes_with_iid(self, sock_path, daemon_factory):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="{}")

        daemon_factory(_ctx(http_handler=handler))
        result = _run_glab(["mr", "view", "9"], sock_path=sock_path)
        assert result.returncode == 0
        assert str(seen[0].url) == "https://gitlab.com/api/v4/projects/ns%2Fpath/merge_requests/9"

    def test_mr_list_default_state(self, sock_path, daemon_factory):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="[]")

        daemon_factory(_ctx(http_handler=handler))
        result = _run_glab(["mr", "list"], sock_path=sock_path)
        assert result.returncode == 0
        assert "state=opened" in str(seen[0].url)

    def test_mr_close_puts_with_state_event(self, sock_path, daemon_factory):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="{}")

        daemon_factory(_ctx(http_handler=handler))
        result = _run_glab(["mr", "close", "9"], sock_path=sock_path)
        assert result.returncode == 0, result.stderr
        assert seen[0].method == "PUT"
        assert str(seen[0].url) == "https://gitlab.com/api/v4/projects/ns%2Fpath/merge_requests/9"
        assert json.loads(seen[0].content.decode("utf-8")) == {"state_event": "close"}

    def test_issue_create_posts_with_description(self, sock_path, daemon_factory):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(201, text='{"iid":7}')

        daemon_factory(_ctx(http_handler=handler))
        result = _run_glab(
            ["issue", "create", "--title", "Bug", "--description", "details"],
            sock_path=sock_path,
        )
        assert result.returncode == 0, result.stderr
        assert json.loads(seen[0].content.decode("utf-8")) == {
            "title": "Bug", "description": "details",
        }

    def test_issue_list_default_state(self, sock_path, daemon_factory):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, text="[]")

        daemon_factory(_ctx(http_handler=handler))
        result = _run_glab(["issue", "list"], sock_path=sock_path)
        assert result.returncode == 0
        assert "state=opened" in str(seen[0].url)

    def test_unrouted_command_exits_2(self, sock_path, daemon_factory):
        daemon_factory(_ctx())
        result = _run_glab(["release", "create"], sock_path=sock_path)
        assert result.returncode == 2
        assert "not yet routed" in result.stderr
        assert "gitlab-api" in result.stderr


# ---- Image content smoke checks -------------------------------------------


class TestImageStaticContent:
    """No `docker build` here — that's a CI/integration concern. Instead we
    verify the COPY paths in the Dockerfile point at files that actually
    exist, and that the static gitconfig wires the helper correctly."""

    def test_all_script_paths_exist_and_are_executable(self):
        for name in (
            "git-credential-istota", "gitlab-api", "github-api", "gh", "glab",
        ):
            path = SCRIPTS_DIR / name
            assert path.exists(), f"missing shim script: {path}"
            assert os.access(path, os.X_OK), f"shim not executable: {path}"

    def test_lib_module_path_exists(self):
        assert (LIB_DIR / "istota_devbox_client.py").exists()

    def test_gitconfig_wires_credential_helper(self):
        gc = (REPO / "docker" / "devbox" / "etc" / "gitconfig").read_text()
        assert "[credential]" in gc
        assert "helper = istota" in gc
        # Placeholder identity so `git commit` doesn't choke.
        assert "[user]" in gc

    def test_dockerfile_copies_lib_scripts_and_gitconfig(self):
        dockerfile = (REPO / "docker" / "devbox" / "Dockerfile").read_text()
        # Each shim and the lib are COPIed.
        for line in (
            "COPY lib/istota_devbox_client.py /usr/local/lib/istota_devbox/istota_devbox_client.py",
            "COPY scripts/git-credential-istota /usr/local/bin/git-credential-istota",
            "COPY scripts/gitlab-api /usr/local/bin/gitlab-api",
            "COPY scripts/github-api /usr/local/bin/github-api",
            "COPY scripts/gh /usr/local/bin/gh",
            "COPY scripts/glab /usr/local/bin/glab",
            "COPY etc/gitconfig /etc/gitconfig",
        ):
            assert line in dockerfile, f"missing Dockerfile line: {line}"
        # ENV defaults that the developer skill's prompt language depends on.
        assert "GITLAB_API_CMD=/usr/local/bin/gitlab-api" in dockerfile
        assert "GITHUB_API_CMD=/usr/local/bin/github-api" in dockerfile
        assert "ISTOTA_CRED_SOCK=/run/istota-cred/sock" in dockerfile
