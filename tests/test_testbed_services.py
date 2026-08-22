"""The testbed's own wiring, held down without bringing a stack up.

`testbed/` is code, and code that only runs behind a deselected marker rots.
These are plain unit tests in the default suite, needing no Docker: the shared
HTTP base, the profile table, and the one rule that makes the whole deployment
tier honest — that a service points the daemon at itself only through a variable
the shipped generator already reads.
"""

from __future__ import annotations

import re
import socket
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from testbed import profiles
from testbed.httpstub import LOOPBACK, HttpStub
from testbed.services import REGISTRY, ServiceCall, gitlab
from testbed.services.model_endpoint import serve_script

REPO = Path(__file__).resolve().parents[1]
RENDER_CONFIG = REPO / "docker" / "istota" / "render-config.sh"
FULL_COMPOSE = REPO / "docker" / "docker-compose.yml"


def _serve(stub: HttpStub, **kwargs) -> HttpStub:
    from http.server import BaseHTTPRequestHandler

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 5

        def log_message(self, *args) -> None:
            """Silence, as every stub in this package does."""

        def do_GET(self) -> None:
            stub.record(ServiceCall(method="GET", path=self.path))
            self.send_response(204)
            self.send_header("content-length", "0")
            self.end_headers()

    stub.start(_Handler, **kwargs)
    return stub


class TestTheCredentialRuleIsStructural:
    """The rule that used to live in one docstring.

    Both deployment tiers bind all interfaces so a container can reach the
    stub, which on a laptop on a shared network is a listener anyone can talk
    to — and in the forge stub's case one that runs `git http-backend` with
    `GIT_HTTP_EXPORT_ALL`. A convention in a comment survives two
    implementations, not six.
    """

    def test_a_non_loopback_bind_without_a_credential_is_refused(self):
        stub = HttpStub()
        with pytest.raises(ValueError) as caught:
            _serve(stub, host="0.0.0.0")

        assert "credential" in str(caught.value)
        # And it refused *before* binding, which is the whole point: a stub
        # that raised after `ThreadingHTTPServer` would have published the
        # listener it is complaining about.
        assert stub.port == 0

    def test_a_non_loopback_bind_with_a_credential_is_allowed(self):
        stub = _serve(HttpStub(), host="0.0.0.0", credential="a-value-to-expect")
        try:
            assert stub.host_bound == "0.0.0.0"
            assert stub.credential == "a-value-to-expect"
        finally:
            stub.close()

    def test_loopback_needs_no_credential(self):
        stub = _serve(HttpStub())
        try:
            assert stub.host_bound == LOOPBACK
            assert stub.credential is None
        finally:
            stub.close()

    def test_the_minimal_argument_table_covers_every_registered_service(self):
        """So the guard below cannot silently stop covering a new service.

        A row per service rather than reflection over the factory: `REGISTRY`
        holds lazy factories (`services/__init__` is what every stub imports
        `ServiceCall` from, so a top-level import of the stubs would close the
        cycle), and reflecting through one of those inspects the wrapper.
        """
        assert set(_MINIMAL_ARGS) == set(REGISTRY)

    def test_no_registered_service_binds_a_public_interface_uncredentialed(
        self, tmp_path
    ):
        """The rule asserted through each factory, not just on the base.

        This is what notices a stub that built its own `ThreadingHTTPServer`
        instead of going through `HttpStub.start` — the failure mode being a
        new service quietly publishing an unauthenticated listener, which
        nothing else in the suite would report. A future service that is not an
        HTTP stub at all (a container, an attach to a real server) takes no
        `host` and will fail here, which is the right moment to decide what its
        own rule is.
        """
        for name, factory in REGISTRY.items():
            args, kwargs = _MINIMAL_ARGS[name](tmp_path)
            with pytest.raises(ValueError, match="credential"):
                factory(*args, host="0.0.0.0", **kwargs)


#: Enough arguments to construct each registered service, for the guard above.
_MINIMAL_ARGS = {
    "model": lambda tmp_path: (([{"text": "ok"}],), {}),
    "gitlab": lambda tmp_path: ((tmp_path / "repos",), {}),
}


class TestTheServerItself:
    def test_the_bound_address_is_read_off_the_socket(self):
        stub = _serve(HttpStub())
        try:
            assert stub.port > 0
            assert stub.url == f"http://{LOOPBACK}:{stub.port}"
            assert stub.container_url == f"http://host.docker.internal:{stub.port}"
        finally:
            stub.close()

    def test_it_answers_and_records(self):
        stub = _serve(HttpStub())
        try:
            urllib.request.urlopen(f"{stub.url}/hello", timeout=10)
            assert [call.path for call in stub.calls] == ["/hello"]
        finally:
            stub.close()

    def test_close_is_idempotent_and_actually_releases_the_port(self):
        stub = _serve(HttpStub())
        port = stub.port
        stub.close()
        stub.close()

        probe = socket.socket()
        try:
            with pytest.raises(ConnectionRefusedError):
                probe.connect((LOOPBACK, port))
        finally:
            probe.close()

    def test_starting_twice_is_refused_rather_than_leaking_the_first_listener(self):
        stub = _serve(HttpStub())
        try:
            with pytest.raises(RuntimeError):
                _serve(stub)
        finally:
            stub.close()

    def test_calls_matching_filters_on_both_axes(self):
        stub = HttpStub()
        stub.record(ServiceCall(method="GET", path="/api/v4/user"))
        stub.record(ServiceCall(method="POST", path="/api/v4/projects/1/issues"))
        stub.record(ServiceCall(method="POST", path="/api/v4/projects/1/merge_requests"))

        assert len(stub.calls_matching("POST")) == 2
        assert len(stub.calls_matching(contains="/issues")) == 1
        assert stub.calls_matching("GET", "/issues") == []
        # Unfiltered means everything, which is what `describe` relies on.
        assert len(stub.calls_matching()) == 3

    def test_reset_clears_the_record(self):
        stub = HttpStub()
        stub.record(ServiceCall(method="GET", path="/x"))
        stub.reset()

        assert stub.calls == []


class TestServiceCallPayload:
    def test_a_json_body_parses(self):
        call = ServiceCall(method="POST", path="/x", body=b'{"a": 1}')

        assert call.payload() == {"a": 1}

    def test_a_form_encoded_body_parses_too(self):
        """glab uses one for some verbs, and an assertion must not have to know."""
        call = ServiceCall(method="POST", path="/x", body=b"title=a+title&state=open")

        assert call.payload() == {"title": "a title", "state": "open"}

    def test_an_empty_body_is_an_empty_dict_rather_than_a_raise(self):
        assert ServiceCall(method="GET", path="/x").payload() == {}

    def test_a_json_scalar_does_not_raise(self):
        """A body of `[]` or `"x"` parses fine and then has no `.get`.

        In a handler thread whose `handle_error` is deliberately silent, that
        is a dropped connection and no diagnostic.
        """
        assert ServiceCall(method="POST", path="/x", body=b"[1, 2]").payload() == {
            "_body": [1, 2]
        }


class TestProfiles:
    """Cheap, and it catches a typo that would otherwise surface as a `KeyError`
    deep inside a session-scoped fixture."""

    def test_every_profile_names_services_that_exist(self):
        for profile in profiles.ALL:
            for service in profile.services:
                assert service in REGISTRY, (
                    f"profile {profile.name!r} names service {service!r}, which "
                    f"is not in the registry: {sorted(REGISTRY)}"
                )

    def test_every_profile_runs_a_model(self):
        """A stack with no scripted endpoint has no deterministic task."""
        for profile in profiles.ALL:
            assert "model" in profile.services, profile.name

    def test_every_profile_has_a_known_shape(self):
        for profile in profiles.ALL:
            assert profile.shape in ("lean", "full"), profile

    def test_no_two_profiles_collide_on_name(self):
        """`StackPool` keys by name, so two profiles sharing one would share a
        stack — and the second would silently get the first's services."""
        names = [profile.name for profile in profiles.ALL]

        assert len(names) == len(set(names)), names


def _reads_variable(script: str, name: str) -> bool:
    """Whether a shell script actually *expands* `name`.

    A substring search would be satisfied by a mention in a comment, which is
    exactly the false pass this check exists to prevent — the whole point is
    that the shipped generator does something with the variable.
    """
    return re.search(r"\$\{" + re.escape(name) + r"[:}+-]", script) is not None


def _passed_through(compose: str, name: str) -> bool:
    """Whether `docker-compose.yml` puts `name` in a service's `environment:`.

    The rule has two files in it. On the lean shape the generator runs on the
    host, so anything it reads is reachable; on the full shape it runs *inside
    the container*, from what compose passed it, and compose's explicit
    `environment:` map is not a superset of what the generator reads.
    """
    return re.search(r"^\s+" + re.escape(name) + r":", compose, re.MULTILINE) is not None


class TestConfigEnvNamesOnlyShippedVariables:
    """The design constraint from the spec, enforced.

    A service may only be wired in through a variable `render-config.sh` reads
    *and* `docker-compose.yml` passes through. The alternative — letting the
    fixture write config directly — is faster and destroys the property that
    makes the tier honest, because the file a stack boots from would no longer
    be one the shipped generator can produce. If a variable is missing, the fix
    is to add it to both as a reviewed product change.

    Enforced here rather than discovered at stack level: a service pointing the
    daemon at itself through a variable the generator ignores otherwise
    surfaces as a mysteriously misconfigured stack, minutes later.
    """

    @pytest.fixture(scope="class")
    def script(self) -> str:
        return RENDER_CONFIG.read_text()

    @pytest.fixture(scope="class")
    def compose(self) -> str:
        return FULL_COMPOSE.read_text()

    @pytest.fixture
    def services(self, tmp_path):
        """One of each conforming service, on loopback, started and stopped."""
        endpoint = serve_script([{"text": "ok"}])
        forge = gitlab.serve(tmp_path / "repos")
        try:
            yield {endpoint.name: endpoint, forge.name: forge}
        finally:
            forge.close()
            endpoint.close()

    def test_the_guard_itself_can_fail(self, script, compose):
        """A check whose regex quietly stopped matching would report a clean
        tree, so both halves get a negative control."""
        assert not _reads_variable(script, "ISTOTA_NOT_A_REAL_VARIABLE")
        assert not _passed_through(compose, "ISTOTA_NOT_A_REAL_VARIABLE")

    def test_every_variable_is_read_by_the_shipped_generator(self, services, script):
        for name, service in services.items():
            for variable in service.config_env():
                assert _reads_variable(script, variable), (
                    f"{name}.config_env() names {variable}, which "
                    f"{RENDER_CONFIG.name} does not read"
                )

    def test_every_variable_is_passed_through_by_the_full_compose_file(
        self, services, compose
    ):
        for name, service in services.items():
            for variable in service.config_env():
                assert _passed_through(compose, variable), (
                    f"{name}.config_env() names {variable}, which "
                    f"docker-compose.yml does not pass into the container — so "
                    f"the full shape's generator would never see it"
                )

    def test_the_endpoint_points_the_brain_at_its_own_container_url(self, services):
        endpoint = services["model"]
        rendered = endpoint.config_env()

        assert rendered["ISTOTA_BRAIN_KIND"] == "native"
        assert rendered["ISTOTA_BRAIN_NATIVE_BASE_URL"] == endpoint.container_url
        # `host.docker.internal`, not loopback: a container reaching its own
        # loopback finds nothing, and the symptom is a task that failed to
        # reach the model for no stated reason.
        assert "host.docker.internal" in rendered["ISTOTA_BRAIN_NATIVE_BASE_URL"]

    def test_the_forge_points_the_developer_skill_at_its_own_container_url(
        self, services
    ):
        forge = services["gitlab"]
        rendered = forge.config_env()

        assert rendered["ISTOTA_DEVELOPER_ENABLED"] == "true"
        assert rendered["ISTOTA_DEVELOPER_GITLAB_URL"] == forge.container_url
        assert rendered["ISTOTA_DEVELOPER_GITLAB_TOKEN"] == forge.token
        assert rendered["ISTOTA_DEVELOPER_GITLAB_DEFAULT_NAMESPACE"] == "istota-test"


class TestTheForgeResets:
    def test_reset_forgets_the_calls_and_rebuilds_the_repository(self, tmp_path):
        """A pushed branch must not survive into the next scenario.

        Under session scope the same stub serves every forge test, so a reset
        that only cleared the call lists would leave `branches()` reporting the
        previous test's push — and an assertion that a branch landed would pass
        without anything having pushed it.
        """
        forge = gitlab.serve(tmp_path / "repos")
        try:
            forge.seed_repo(forge.project)
            _push_a_branch(forge, tmp_path)
            assert "extra" in forge.branches(forge.project)
            forge.record(ServiceCall(method="GET", path="/api/v4/user"))

            forge.reset()

            assert forge.calls == []
            assert forge.git_calls == []
            assert "extra" not in forge.branches(forge.project)
            # And the repository still exists, rather than being deleted: the
            # next scenario clones it.
            assert forge.branches(forge.project) == ["main"]
        finally:
            forge.close()


def _push_a_branch(forge, tmp_path: Path) -> None:
    """Clone over the loopback listener and push a branch, as a scenario does."""
    import subprocess

    work = tmp_path / "clone"
    url = f"http://tester:token@{forge.url.split('://', 1)[1]}/{forge.project}.git"
    env = {"PATH": __import__("os").environ["PATH"], "GIT_TERMINAL_PROMPT": "0"}
    subprocess.run(["git", "clone", url, str(work)], check=True, capture_output=True,
                   timeout=60, env=env)
    subprocess.run(["git", "-C", str(work), "checkout", "-b", "extra"], check=True,
                   capture_output=True, timeout=60, env=env)
    subprocess.run(["git", "-C", str(work), "push", "origin", "extra"], check=True,
                   capture_output=True, timeout=60, env=env)
