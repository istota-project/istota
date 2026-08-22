"""The GitLab stub, driven by the real clients it exists to answer.

`tests/smoke/fake_gitlab.py` re-implements a slice of GitLab's wire protocol,
which is exactly the thing a stub should not be trusted to have got right. This
file is the paydown, and it lives in the **default** suite for the same reason
`tests/test_model_endpoint.py` does: the smoke tier that consumes the stub costs
a compose stack per test, so a framing bug discovered there surfaces as a task
that failed for an unrelated-looking reason, minutes later.

So the assertions here drive the stub with the real `glab` and the real `git` —
no Docker, no daemon, no sandbox. Break the stub's framing and this goes red
first, naming the protocol rather than the symptom.
"""

from __future__ import annotations

import os
import shutil
import subprocess

import pytest

from .smoke.fake_gitlab import LOOPBACK, _auth_shape, serve

REQUIRES_GLAB = pytest.mark.skipif(
    shutil.which("glab") is None, reason="glab not installed"
)


@pytest.fixture
def stub(tmp_path):
    with serve(tmp_path / "repos") as running:
        yield running


def _glab_env(tmp_path, stub, *, token="forge-token-stand-in-value"):
    """The environment `build_invocation` hands the real glab.

    Assembled from the same pieces rather than by calling it, because this file
    is about the stub. `tests/test_forge_cli.py` pins what `build_invocation`
    actually produces; if the two drift, the smoke tier is where it shows.
    """
    from istota.skills.developer import _seed_cli_config_dir

    config_dir = _seed_cli_config_dir(
        tmp_path, "gitlab-config", forge="gitlab", forge_url=stub.url
    )
    return {
        "PATH": os.environ["PATH"],
        "HOME": str(tmp_path),
        "GLAB_CONFIG_DIR": str(config_dir),
        "GITLAB_HOST": stub.url,
        "GITLAB_TOKEN": token,
        "GLAB_CHECK_UPDATE": "false",
        "GLAB_SEND_TELEMETRY": "0",
        "GLAB_NO_PROMPT": "1",
        "NO_COLOR": "1",
        "PAGER": "cat",
    }


class TestAuthShape:
    """The one function that touches a credential."""

    def test_a_private_token_records_its_length_and_not_its_value(self):
        assert _auth_shape({"PRIVATE-TOKEN": "abcdef"}) == "private-token:6"

    def test_a_bearer_token_records_its_length_and_not_its_value(self):
        assert _auth_shape({"Authorization": "Bearer abcdefgh"}) == "bearer:8"

    def test_no_credential_is_the_empty_string(self):
        assert _auth_shape({}) == ""

    def test_an_unrecognised_scheme_keeps_its_name_and_drops_the_value(self):
        shape = _auth_shape({"Authorization": "Basic c2VjcmV0OnZhbHVl"})
        assert shape.startswith("basic:")
        assert "c2VjcmV0" not in shape

    @pytest.mark.parametrize(
        "headers",
        [
            {"PRIVATE-TOKEN": "averyrealsecretvalue"},
            {"Authorization": "Bearer averyrealsecretvalue"},
            {"Authorization": "Basic averyrealsecretvalue"},
        ],
    )
    def test_the_secret_never_survives_into_the_shape(self, headers):
        """The property the whole dataclass exists for, stated once over every
        branch. A failing smoke assertion renders `ForgeCall` into the pytest
        report and the terminal, and under `--live` these carry a real token."""
        assert "averyrealsecretvalue" not in _auth_shape(headers)


class TestForgeCallRendering:
    """What reaches a failing test's output.

    `auth` is lossy by construction; `query` and `body` are not, because
    assertions need them. So the guarantee has to be about rendering, and the
    rendering pytest actually uses is `repr` — assertion rewriting prints the
    repr of whatever a failing comparison touched, and a dataclass's generated
    one carries every field.
    """

    def _call(self, **overrides):
        from .smoke.fake_gitlab import ForgeCall

        fields = {
            "method": "POST",
            "path": "/api/v4/projects/1/merge_requests",
            "query": {"private_token": "a-secret-in-the-query"},
            "body": {"description": "a-secret-in-the-body"},
            "auth": "private-token:20",
        }
        fields.update(overrides)
        return ForgeCall(**fields)

    def test_repr_carries_neither_the_query_nor_the_body(self):
        rendered = repr(self._call())

        assert "a-secret-in-the-query" not in rendered, rendered
        assert "a-secret-in-the-body" not in rendered, rendered

    def test_repr_still_says_enough_to_debug_with(self):
        """Redaction that removes the diagnostic value is its own failure."""
        rendered = repr(self._call())

        assert "POST" in rendered
        assert "/merge_requests" in rendered
        assert "private-token:20" in rendered

    def test_a_list_of_calls_renders_safely_too(self):
        """The shape the assertions actually use.

        `assert not stub.rest_calls(...)` fails on a *list*, and a list's repr
        calls `repr` on each element — so a `__str__`-only override would be
        bypassed by every real failure this is meant to protect.
        """
        rendered = repr([self._call()])

        assert "a-secret-in-the-body" not in rendered, rendered


class TestRestSurface:
    def test_an_unimplemented_endpoint_says_so_with_the_path(self, stub, tmp_path):
        import urllib.request

        request = urllib.request.Request(f"{stub.url}/api/v4/groups/1/epics")
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request, timeout=10)

        assert caught.value.code == 501
        body = caught.value.read().decode()
        # The path, so a missing endpoint reports itself precisely instead of
        # surfacing as a glab error about a malformed response.
        assert "/api/v4/groups/1/epics" in body
        assert "fake_gitlab" in body

    def test_calls_are_recorded_with_their_query_and_body(self, stub):
        import urllib.request

        urllib.request.urlopen(
            urllib.request.Request(
                f"{stub.url}/api/v4/projects/1/merge_requests",
                data=b'{"title": "a title", "source_branch": "feature"}',
                headers={
                    "content-type": "application/json",
                    "PRIVATE-TOKEN": "sixteencharacter",
                },
                method="POST",
            ),
            timeout=10,
        )

        posted = stub.rest_calls("POST", "merge_requests")
        assert len(posted) == 1, stub.calls
        assert posted[0].body["title"] == "a title"
        assert posted[0].body["source_branch"] == "feature"
        assert posted[0].auth == "private-token:16"


def _authed(stub, path="group/project"):
    """A clone URL carrying userinfo, since the stub challenges for git.

    The deployment answers that challenge through the credential helper, which
    needs a running skill proxy — out of scope for a file that deliberately
    starts no daemon. Userinfo produces the same `Authorization` header by the
    shortest route, so what is exercised here is the stub's own half.
    """
    host = stub.url.split("://", 1)[1]
    return f"http://tester:token-value@{host}/{path}.git"


class TestGitOverHttp:
    """The half a REST-only stub cannot cover.

    The happy path clones, branches, commits and pushes, and each of those is
    git talking to `git http-backend` — a different protocol from the JSON, over
    the same listener, carrying the credential by a different route.
    """

    def test_an_unauthenticated_clone_is_challenged(self, stub, tmp_path):
        """The challenge is the point, not an obstacle to work around.

        git sends no credential until it is asked for one. A stub that never
        asked would let the smoke tier's push succeed with the credential
        helper broken or missing entirely — and that helper is the piece that
        fetches the token from the skill proxy.
        """
        stub.seed_repo("group/project")

        result = subprocess.run(
            ["git", "-c", "credential.helper=", "clone",
             f"{stub.url}/group/project.git", str(tmp_path / "x")],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )

        assert result.returncode != 0
        assert stub.git_calls and not stub.authenticated_git_calls()

    def test_a_challenged_post_leaves_the_connection_usable(self, stub):
        """The 401 must consume the request body before answering.

        `protocol_version` is HTTP/1.1, so the socket is reused. A body left
        unread stays in the buffer and is parsed as the next request line —
        measured before the fix: an unauthenticated POST followed by a valid
        `GET /api/v4/user` on the same connection answered the second request
        out of the first one's bytes, as an HTML error page.

        The live shape is `git push`. Whenever libcurl does not pre-emptively
        re-send Basic auth on `POST /git-receive-pack`, the push fails looking
        corrupt, which reads as a defect in the forge chain rather than in the
        harness — and intermittently, since it depends on connection reuse.
        """
        import http.client

        stub.seed_repo("group/project")
        connection = http.client.HTTPConnection(
            LOOPBACK, stub.port, timeout=15
        )
        try:
            connection.request(
                "POST",
                "/group/project.git/git-receive-pack",
                body=b"x" * 512,
                headers={"content-type": "application/x-git-receive-pack-request"},
            )
            first = connection.getresponse()
            first.read()
            assert first.status == 401, first.status
            assert not first.will_close, (
                "the stub closed the connection, so this test cannot observe "
                "the desync it exists for"
            )

            # The same socket, a well-formed request. Before the fix this came
            # back as an HTML error page parsed out of the packfile bytes.
            connection.request("GET", "/api/v4/user")
            second = connection.getresponse()
            body = second.read()
        finally:
            connection.close()

        assert second.status == 200, (second.status, body[:200])
        assert b"istota-test" in body, body[:200]

    def test_a_seeded_repo_clones(self, stub, tmp_path):
        stub.seed_repo("group/project")
        target = tmp_path / "clone"

        subprocess.run(
            ["git", "clone", _authed(stub), str(target)],
            capture_output=True,
            text=True,
            timeout=60,
            check=True,
        )

        assert (target / "README.md").exists()
        assert stub.authenticated_git_calls()

    def test_a_push_lands_in_the_bare_repo(self, stub, tmp_path):
        """The assertion the happy path's "the branch exists" rests on."""
        stub.seed_repo("group/project")
        target = tmp_path / "clone"
        subprocess.run(
            ["git", "clone", _authed(stub), str(target)],
            capture_output=True, text=True, timeout=60, check=True,
        )
        (target / "new.txt").write_text("content\n")
        for argv in (
            ["add", "new.txt"],
            ["-c", "user.email=t@example.com", "-c", "user.name=T",
             "commit", "-m", "Add a file"],
            ["checkout", "-b", "feature/thing"],
            ["push", "origin", "feature/thing"],
        ):
            subprocess.run(
                ["git", "-C", str(target), *argv],
                capture_output=True, text=True, timeout=60, check=True,
            )

        assert "feature/thing" in stub.branches("group/project")

    def test_a_missing_repo_is_a_failure_rather_than_a_hang(self, stub, tmp_path):
        """`http-backend` answers non-200 through a CGI `Status:` header.

        Relayed as 200 — which is what happens if the header block is not
        parsed — git reports a corrupt response, or waits. Either way the
        failure names the wrong thing.
        """
        result = subprocess.run(
            ["git", "clone", _authed(stub, "nope/missing"), str(tmp_path / "x")],
            capture_output=True, text=True, timeout=60,
        )

        assert result.returncode != 0
        assert "not found" in (result.stdout + result.stderr).lower(), result.stderr


@REQUIRES_GLAB
class TestAgainstRealGlab:
    """What the stub is actually for: answering the binary the chain execs."""

    def test_repo_view_is_answered(self, stub, tmp_path):
        env = _glab_env(tmp_path, stub)

        result = subprocess.run(
            ["glab", "repo", "view", "group/project"],
            env=env, capture_output=True, text=True, timeout=60,
        )

        assert result.returncode == 0, (
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
        assert stub.rest_calls(contains="/projects/"), stub.calls

    def test_the_token_reaches_the_stub(self, stub, tmp_path):
        token = "forge-token-" + "z" * 20
        env = _glab_env(tmp_path, stub, token=token)

        subprocess.run(
            ["glab", "api", "/projects/1"],
            env=env, capture_output=True, text=True, timeout=60,
        )

        calls = stub.rest_calls(contains="/projects/1")
        assert calls, stub.calls
        # Shape, not value — and the length is what makes it possible to say a
        # *particular* token arrived without recording one.
        assert calls[0].auth.endswith(f":{len(token)}"), calls[0].auth

    def test_mr_create_reaches_the_post_with_both_branches(self, stub, tmp_path):
        """The happy path's central verb, proven cheap.

        `glab mr create` is what the smoke tier's happy path drives, and it
        costs a compose stack there. It is also the verb most sensitive to the
        project payload: glab 1.114.0 **segfaults** in `glrepo.HeadRepo` when
        the project response omits `http_url_to_repo` — a nil dereference, not
        an error message, so the failure arrives as a signal and names nothing.
        Whatever this asserts, it asserts in two seconds.
        """
        stub.seed_repo("group/project")
        work = tmp_path / "work"
        subprocess.run(
            ["git", "clone", _authed(stub), str(work)],
            capture_output=True, text=True, timeout=60, check=True,
        )
        (work / "f.txt").write_text("content\n")
        for argv in (
            ["checkout", "-b", "feature/thing"],
            ["add", "f.txt"],
            ["-c", "user.email=t@example.com", "-c", "user.name=T",
             "commit", "-m", "Add f"],
            ["push", "-u", "origin", "feature/thing"],
        ):
            subprocess.run(
                ["git", "-C", str(work), *argv],
                capture_output=True, text=True, timeout=60, check=True,
            )

        result = subprocess.run(
            [
                "glab", "mr", "create",
                "--title", "A title",
                "--description", "A body",
                "--source-branch", "feature/thing",
                "--target-branch", "main",
                "--repo", "group/project",
                "--yes",
            ],
            env=_glab_env(tmp_path, stub),
            cwd=str(work),
            capture_output=True, text=True, timeout=120,
        )

        assert result.returncode == 0, (
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
        )
        posted = stub.rest_calls("POST", "merge_requests")
        assert len(posted) == 1, [str(c) for c in stub.calls]
        assert posted[0].body.get("source_branch") == "feature/thing", posted[0].body
        assert posted[0].body.get("target_branch") == "main", posted[0].body

    def test_a_501_is_reported_rather_than_swallowed(self, stub, tmp_path):
        """A stub gap must fail the caller.

        If glab treated a 501 as an empty success, an unimplemented endpoint
        would make a smoke scenario pass while asserting nothing — the exact
        silent non-execution this tier exists to end.
        """
        env = _glab_env(tmp_path, stub)

        result = subprocess.run(
            ["glab", "api", "/groups/1/epics"],
            env=env, capture_output=True, text=True, timeout=60,
        )

        assert result.returncode != 0, result.stdout
