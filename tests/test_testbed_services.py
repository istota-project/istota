"""The testbed's own wiring, held down without bringing a stack up.

`testbed/` is code, and code that only runs behind a deselected marker rots.
These are plain unit tests in the default suite, needing no Docker: the shared
HTTP base, the profile table, and the one rule that makes the whole deployment
tier honest — that every key a service or profile writes into the stack's
`config.toml` is one the loader reads.
"""

from __future__ import annotations

import re
import socket
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

from testbed import profiles, services
from testbed import stack as stack_support
from testbed.httpstub import LOOPBACK, HttpStub
from testbed.services import REGISTRY, ServiceCall, gitlab, mail, signaling
from testbed.services.model_endpoint import serve_script

REPO = Path(__file__).resolve().parents[1]
FULL_COMPOSE = REPO / "docker" / "docker-compose.yml"

#: A stand-in for what `StackPool` generates on the full shape. Fixed values, so
#: nothing here invents a password-shaped string that a scan could flag, and
#: obviously fake ones so a reader never wonders whether they are real.
_CREDENTIALS = stack_support.FullCredentials(
    postgres_password="unit-test-postgres",
    admin_password="unit-test-admin",
    bot_password="unit-test-bot",
    user_password="unit-test-user",
    nc_port=18080,
)


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

    def test_a_non_loopback_bind_without_a_credential_is_refused(self, monkeypatch):
        """And refused *before* binding, which is the whole point.

        The socket is what has to not exist: a guard that raised after
        `ThreadingHTTPServer` would have published the listener it was
        complaining about. `stub.port` cannot say so — it starts at 0 and is
        only assigned after the bind, so it reads 0 on both the refusal and
        the leak. The constructor itself is the witness.
        """
        built = []
        monkeypatch.setattr(
            "testbed.httpstub.ThreadingHTTPServer",
            lambda *args, **kwargs: built.append(args) or pytest.fail(
                "a listener was constructed despite the refusal", pytrace=False
            ),
        )
        stub = HttpStub()

        with pytest.raises(ValueError) as caught:
            _serve(stub, host="0.0.0.0")

        assert "credential" in str(caught.value)
        assert built == []

    def test_an_empty_credential_counts_as_none(self):
        """`credential=""` would satisfy an `is None` test and enforce nothing.

        The forge's `_password_accepted` compares against the stored value, so
        an empty one accepts `Basic base64("anyone:")` from the network while
        the guard reports itself satisfied.
        """
        with pytest.raises(ValueError, match="credential"):
            _serve(HttpStub(), host="0.0.0.0", credential="")

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

    def test_every_registered_service_is_classified(self):
        """The rule below applies to the stubs, so the split has to be total.

        `HOST_STUBS` and `ATTACHED` partition `REGISTRY`, and if they stop doing
        so the guard beneath silently covers less than it says. That is the
        exact failure the docstring below anticipated when the registry held
        only stubs: "a future service that is not an HTTP stub at all takes no
        `host` and will fail here, which is the right moment to decide what its
        own rule is." Stage 3 is that moment, and this is the decision — a
        service that binds no socket has no unauthenticated-listener hazard and
        no credential to publish, so demanding one of it would be asserting
        something false.
        """
        assert services.HOST_STUBS | services.ATTACHED == set(REGISTRY)
        assert not services.HOST_STUBS & services.ATTACHED

    def test_the_minimal_argument_table_covers_every_host_stub(self):
        """So the guard below cannot silently stop covering a new stub.

        A row per service rather than reflection over the factory: `REGISTRY`
        holds lazy factories (`services/__init__` is what every stub imports
        `ServiceCall` from, so a top-level import of the stubs would close the
        cycle), and reflecting through one of those inspects the wrapper.
        """
        assert set(_MINIMAL_ARGS) == services.HOST_STUBS

    def test_no_registered_stub_binds_a_public_interface_uncredentialed(
        self, tmp_path
    ):
        """The rule asserted through each factory, not just on the base.

        This is what notices a stub that built its own `ThreadingHTTPServer`
        instead of going through `HttpStub.start` — the failure mode being a
        new service quietly publishing an unauthenticated listener, which
        nothing else in the suite would report.
        """
        for name in sorted(services.HOST_STUBS):
            args, kwargs = _MINIMAL_ARGS[name](tmp_path)
            with pytest.raises(ValueError, match="credential"):
                REGISTRY[name](*args, host="0.0.0.0", **kwargs)


#: Enough arguments to construct each registered *stub*, for the guard above.
_MINIMAL_ARGS = {
    "model": lambda tmp_path: (([{"text": "ok"}],), {}),
    "gitlab": lambda tmp_path: ((tmp_path / "repos",), {}),
    "ntfy": lambda tmp_path: ((), {}),
    "feeds": lambda tmp_path: ((), {}),
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


def _unknown_keys(tmp_path, document: dict, caplog) -> str:
    """Load `document` as a config and return the loader's unknown-key warning.

    The loader is the reader that decides: an unknown key is a warning and
    nothing else, so a fragment naming one would boot a stack that silently
    ignores the setting the profile or service asked for.
    """
    import logging

    from istota.config import load_config

    path = tmp_path / "config.toml"
    path.write_text(stack_support.toml_dumps(document))
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        load_config(path)
    return " ".join(r.getMessage() for r in caplog.records if "unrecognised key" in r.getMessage())


class TestServiceConfigNamesOnlyRealKeys:
    """Every service's `config()` is a fragment the loader understands.

    The config is an input now, written by the testbed for every profile, so
    the rule this tier holds is no longer "a variable two shipped files agree
    on": it is that every key the testbed writes is a key `load_config` reads,
    the same reader an operator's `istota setup` output goes through.
    """

    @pytest.fixture
    def services(self, tmp_path):
        """One of each service that has a `config()` to check.

        `mail` costs nothing: `serve()` starts no container — the profile's
        compose overlay does that. `signaling` is the same shape. `nextcloud`,
        `ntfy` and `feeds` return nothing by design; their stubs' own tests say
        so (`tests/test_testbed_stubs.py`).
        """
        endpoint = serve_script([{"text": "ok"}])
        forge = gitlab.serve(tmp_path / "repos")
        post = mail.serve(tmp_path / "mail")
        hpb = signaling.serve()
        try:
            yield {
                endpoint.name: endpoint,
                forge.name: forge,
                post.name: post,
                hpb.name: hpb,
            }
        finally:
            hpb.close()
            post.close()
            forge.close()
            endpoint.close()

    def test_the_guard_itself_can_fail(self, tmp_path, caplog, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "admins"))
        warned = _unknown_keys(tmp_path, {"email": {"not_a_real_key": 1}}, caplog)

        assert "not_a_real_key" in warned

    def test_every_key_is_one_the_loader_reads(self, services, tmp_path, caplog, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "admins"))
        document = stack_support.assemble_config(
            services, base=stack_support.LEAN_BASE_CONFIG,
        )

        assert _unknown_keys(tmp_path, document, caplog) == ""

    def test_the_endpoint_points_the_brain_at_its_own_container_url(self, services):
        endpoint = services["model"]
        brain = endpoint.config()["brain"]

        assert brain["kind"] == "native"
        assert brain["native"]["base_url"] == endpoint.container_url
        # `host.docker.internal`, not loopback: a container reaching its own
        # loopback finds nothing, and the symptom is a task that failed to
        # reach the model for no stated reason.
        assert "host.docker.internal" in brain["native"]["base_url"]

    def test_the_signaling_image_tag_matches_the_shipped_compose_default(self):
        """One version, written in two places and held equal by nothing else.

        The harness passes `ISTOTA_TALK_SIGNALING_IMAGE_TAG` explicitly, so a
        compose default is only what an *operator* gets — and a bump in one
        place would leave the tier exercising a version the shipped file does
        not run. `chat-relay` landed in 2.1.0 and a server without it connects
        fine and only ever sends a bare refresh.
        """
        pattern = re.compile(
            r"strukturag/nextcloud-spreed-signaling:\$\{ISTOTA_TALK_SIGNALING_IMAGE_TAG"
            r":-([0-9][^}]*)\}"
        )
        for path in (FULL_COMPOSE,):
            found = pattern.findall(path.read_text())
            assert found == [signaling.IMAGE_TAG], (
                f"{path.name} defaults the signaling image to {found}, and "
                f"testbed.services.signaling.IMAGE_TAG is "
                f"{signaling.IMAGE_TAG!r}"
            )

    def test_the_forge_points_the_developer_skill_at_its_own_container_url(
        self, services
    ):
        forge = services["gitlab"]
        developer = forge.config()["developer"]

        assert developer["enabled"] is True
        assert developer["gitlab_url"] == forge.container_url
        assert developer["gitlab_token"] == forge.token
        assert developer["gitlab_default_namespace"] == "istota-test"


class TestProfileConfigNamesOnlyRealKeys:
    """The same rule for `Profile.config`, the other half of what is merged.

    A profile is where somebody reaches when a service has no natural claim on
    a setting, so it is checked the same way: every key it writes must be one
    the loader reads.
    """

    def test_the_guard_covers_at_least_one_real_profile(self):
        """Otherwise the assertion below iterates over nothing."""
        assert [p.name for p in profiles.ALL if p.config]

    def test_every_key_is_one_the_loader_reads(self, tmp_path, caplog, monkeypatch):
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "admins"))
        for profile in profiles.ALL:
            if not profile.config:
                continue
            document = stack_support.assemble_config(
                {}, base=stack_support.LEAN_BASE_CONFIG, extra=profile.config,
            )
            # `verify` needs the mail service's `authserv_id`, which this
            # profile-only document does not have.
            document.setdefault("email", {})["authserv_id"] = "mail"
            assert _unknown_keys(tmp_path, document, caplog) == "", profile.name


class TestTheEmailProfiles:
    """The email suite's two lean profiles, which differ only in the floor."""

    def test_both_are_registered(self):
        assert profiles.by_name("email") is profiles.EMAIL
        assert profiles.by_name("email-hold-all") is profiles.EMAIL_HOLD_ALL

    def test_both_run_mail_and_ntfy_with_the_gate_in_verify(self):
        for profile in (profiles.EMAIL, profiles.EMAIL_HOLD_ALL):
            assert profile.shape == "lean"
            assert set(profile.services) == {"model", "mail", "ntfy"}
            assert profile.compose_overlays == (profiles.MAIL_OVERLAY,)
            assert profile.config["email"]["confirm_sender_match"] == "verify"

    def test_only_hold_all_raises_the_outbound_floor(self):
        assert "outbound_approval_floor" not in profiles.EMAIL.config["email"]
        assert profiles.EMAIL_HOLD_ALL.config["email"]["outbound_approval_floor"] == "all"

    def test_both_carry_the_mail_profiles_poll_interval_and_address(self):
        for profile in (profiles.EMAIL, profiles.EMAIL_HOLD_ALL):
            assert profile in profiles.ALL
            assert profile.config["scheduler"]["email_poll_interval"] == 5
            assert profile.config["users"]["testuser"]["email_addresses"] == [
                "testuser@ext.test"
            ]


class TestTheForgeGuardsItsOwnListener:
    """`HttpStub.start` cannot see whether the subclass enforces the credential.

    The forge is where that gap is real: `require_git_auth=False` satisfies the
    base's guard and then never challenges, on a listener running
    `git http-backend` with `GIT_HTTP_EXPORT_ALL`.
    """

    def test_a_public_bind_without_a_git_challenge_is_refused(self, tmp_path):
        with pytest.raises(ValueError, match="require_git_auth"):
            gitlab.serve(
                tmp_path / "repos",
                host="0.0.0.0",
                token="a-token-to-expect",
                require_git_auth=False,
            )

    def test_a_loopback_bind_without_a_challenge_is_still_allowed(self, tmp_path):
        """A stub answering a public repository is a legitimate shape."""
        forge = gitlab.serve(tmp_path / "repos", require_git_auth=False)
        try:
            assert forge.host_bound == LOOPBACK
        finally:
            forge.close()

    def test_the_advertised_token_is_the_one_the_git_path_challenges_for(
        self, tmp_path
    ):
        """Two names for one value, and they used to be able to disagree.

        `config()` advertises `token` to the daemon while the git path
        compares against `credential`. Resolving the default in one place and
        not the other rejected the daemon's own push while accepting anyone
        else's — a failure that reads as a broken credential helper.
        """
        forge = gitlab.serve(tmp_path / "repos", token="a-token-to-expect")
        try:
            assert forge.token == "a-token-to-expect"
            assert forge.expect_git_password == "a-token-to-expect"
            assert (
                forge.config()["developer"]["gitlab_token"]
                == forge.expect_git_password
            )
        finally:
            forge.close()

    def test_no_token_challenges_for_nothing_rather_than_for_the_default(
        self, tmp_path
    ):
        """The permissive loopback shape the default-suite tests rely on."""
        forge = gitlab.serve(tmp_path / "repos")
        try:
            assert forge.expect_git_password is None
        finally:
            forge.close()


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


    def test_a_failed_rebuild_leaves_the_registry_intact(self, tmp_path, monkeypatch):
        """The bookkeeping bug that made every later reset a silent no-op.

        Clearing `seeded` up front and re-registering as each repo is rebuilt
        loses the whole list the moment one rebuild raises: repos after the
        failure keep the previous scenario's pushes, and `reset()` reports
        success from then on. Under a session-scoped pool that is a stale
        branch a later assertion passes on.
        """
        forge = gitlab.serve(tmp_path / "repos")
        try:
            forge.seed_repo("ns/first")
            forge.seed_repo("ns/second")

            real_git = gitlab._git

            def _fail_on_first(argv, **kwargs):
                if any("ns/first.git" in str(token) for token in argv):
                    raise RuntimeError("simulated git failure")
                return real_git(argv, **kwargs)

            monkeypatch.setattr(gitlab, "_git", _fail_on_first)
            with pytest.raises(RuntimeError):
                forge.reset()

            assert forge.seeded == ["ns/first", "ns/second"]

            # And once the failure clears, a reset rebuilds both rather than
            # reporting success having done nothing.
            monkeypatch.setattr(gitlab, "_git", real_git)
            forge.reset()
            assert forge.branches("ns/first") == ["main"]
            assert forge.branches("ns/second") == ["main"]
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


class TestTheServiceBuilder:
    """`services.build` is what a `StackPool` calls; `REGISTRY` is not.

    The two are kept apart deliberately — see `build`'s docstring — so the pair
    needs a guard that they stay in step.
    """

    def test_it_covers_every_registered_service(self, tmp_path):
        for name in REGISTRY:
            service = services.build(
                name, scratch=tmp_path, host=LOOPBACK, credentials=_CREDENTIALS
            )
            try:
                assert service.name == name
            finally:
                service.close()

    def test_an_unregistered_name_says_what_exists(self, tmp_path):
        # A name nothing will ever register, rather than the next planned
        # service: this test used to name `feeds`, and registering it in Stage
        # 7 turned the guard into an assertion that a working service raises.
        with pytest.raises(KeyError, match="gitlab"):
            services.build("not-a-service", scratch=tmp_path, host=LOOPBACK)

    def test_an_attached_service_refuses_to_be_built_without_a_stack(self, tmp_path):
        """`nextcloud` attaches to a running full stack, and says so.

        The lean shape generates no credentials because it boots no Nextcloud,
        so a `lean` profile naming this service is a mistake with no sensible
        default — and the failure without this guard is a `TypeError` about a
        `None` attribute several frames inside a session fixture.
        """
        with pytest.raises(ValueError, match="full stack"):
            services.build("nextcloud", scratch=tmp_path, host=LOOPBACK)

    def test_the_forge_arrives_with_its_repository_already_seeded(self, tmp_path):
        """A stack's forge has to be clonable before the first scenario runs.

        And it has to be seeded *by* `build`, not by the caller: `reset()`
        rebuilds exactly what `seed_repo` registered, so a repository the test
        seeded itself would vanish at the first reset.
        """
        forge = services.build("gitlab", scratch=tmp_path, host=LOOPBACK)
        try:
            assert forge.seeded == [forge.project]
            assert "main" in forge.branches(forge.project)
        finally:
            forge.close()

    def test_the_model_arrives_with_no_script(self, tmp_path):
        """`Stack.reset` installs the real turns before every test.

        A service constructed with a script would let one of the daemon's own
        pollers consume it in the window before that first reset.
        """
        endpoint = services.build("model", scratch=tmp_path, host=LOOPBACK)
        try:
            assert endpoint.turns == []
        finally:
            endpoint.close()

    def test_every_stub_is_built_with_the_credential_it_publishes(
        self, tmp_path
    ):
        """The pool binds all interfaces, so `build` has to satisfy the rule.

        `HttpStub.start` refuses a non-loopback bind with no credential, and a
        `build` that forgot one would fail at the first `StackPool.get` — after
        an image build, inside a session fixture. Cheaper to find here.

        Asserted on **loopback**, deliberately. `build` passes its credential
        regardless of host, so binding `0.0.0.0` proves nothing extra and does
        real harm: it opens an unauthenticated listener — and for the forge,
        `git http-backend` with `GIT_HTTP_EXPORT_ALL` — on every interface,
        during an ordinary `uv run pytest`. That is the property `HttpStub`'s
        own docstring says the default suite must never break. The refusal
        itself is covered by `TestTheCredentialRuleIsStructural` above.

        Scoped to `HOST_STUBS`: `nextcloud` binds no socket and publishes no
        credential of its own, which is the classification that test asserts is
        total.
        """
        for name in sorted(services.HOST_STUBS):
            service = services.build(name, scratch=tmp_path / name, host=LOOPBACK)
            try:
                assert service.credential, name
            finally:
                service.close()


class TestTheProfileLookup:
    def test_a_declared_name_resolves(self):
        assert profiles.by_name("forge") is profiles.FORGE

    def test_a_typo_names_the_profiles_that_exist(self):
        """The message is the point: this is raised inside a session fixture,
        where pytest reports it as an error on every test in the profile."""
        with pytest.raises(KeyError, match="no-forge"):
            profiles.by_name("noforge")


class TestTheScriptedEndpointsBarrier:
    """The barrier between quiescing and rescripting.

    The daemon's pollers run on their own threads all session, so one can
    create a task in the window between "the task table read quiescent" and
    "the next test's script is installed" — and that task then consumes turn 0.
    Refusing loudly is what turns a stolen turn into a task that failed saying
    why.
    """

    def test_a_request_during_the_swap_is_refused_and_counted(self):
        endpoint = serve_script([{"text": "hello"}])
        try:
            with endpoint.barrier():
                with pytest.raises(urllib.error.HTTPError) as raised:
                    _post_completion(endpoint)
                assert raised.value.code == 403
            assert endpoint.refused == 1
            # And nothing was served, so the next request still gets turn 0.
            assert endpoint.served == 0
            assert endpoint.requests == []
        finally:
            endpoint.close()

    def test_the_barrier_drops_again_afterwards(self):
        endpoint = serve_script([{"text": "hello"}])
        try:
            with endpoint.barrier():
                pass
            _post_completion(endpoint)
            # `served`, not the bytes: `TEXT_CHUNK` is 4, so "hello" arrives as
            # two deltas and never appears contiguously in the stream.
            assert endpoint.served == 1
            assert endpoint.refused == 0
        finally:
            endpoint.close()

    def test_it_drops_even_when_the_body_raises(self):
        """`rescript` can raise; a barrier left up serves nothing for the rest
        of the session, and every later test times out with no explanation."""
        endpoint = serve_script([{"text": "hello"}])
        try:
            with pytest.raises(RuntimeError):
                with endpoint.barrier():
                    raise RuntimeError("the swap failed")
            _post_completion(endpoint)
            assert endpoint.served == 1
        finally:
            endpoint.close()


def _post_completion(endpoint) -> bytes:
    request = urllib.request.Request(
        f"{endpoint.url}/chat/completions",
        data=b'{"model": "m", "messages": []}',
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.read()


class TestTheForgeWaitsForGitBeforeRebuilding:
    """`reset()` rmtrees the bare repositories and re-seeds them.

    Under a session-scoped pool that runs against a *live* listener.
    `Stack.reset` quiesces the daemon first and the daemon is this stub's only
    client, so in practice nothing is cloning — but a `git` subprocess can
    outlive the task that spawned it, and rmtree under a running `http-backend`
    answers a clone with a truncated packfile. The wait is what makes that a
    bounded refusal rather than a corrupt repository in whichever scenario runs
    next.
    """

    def test_a_reset_waits_for_an_in_flight_git_request(self, tmp_path):
        forge = gitlab.serve(tmp_path / "repos")
        try:
            forge.seed_repo("group/project")
            released = threading.Event()
            entered = threading.Event()

            def hold():
                with forge.serving_git():
                    entered.set()
                    released.wait(timeout=10)

            holder = threading.Thread(target=hold)
            holder.start()
            entered.wait(timeout=10)

            assert forge.await_git_idle(timeout=0.2) is False
            released.set()
            holder.join(timeout=10)
            assert forge.await_git_idle(timeout=5) is True
        finally:
            forge.close()

    def test_a_reset_refuses_rather_than_rebuilding_underneath_a_clone(
        self, tmp_path
    ):
        """The failure the wait exists to prevent, made to happen.

        Asserted on the refusal rather than on a corrupt repository, because a
        corrupt repository is precisely the outcome that would be diagnosed as
        something else.

        `timeout=` is passed rather than the module global being patched. The
        patch does not reach a default bound at definition time, so the test
        waited the real ten seconds, passed, and reported a message quoting the
        value it thought it had set. This version is bounded by the argument
        it hands in, which is also what the message has to quote.
        """
        forge = gitlab.serve(tmp_path / "repos")
        try:
            forge.seed_repo("group/project")
            with forge.serving_git():
                with pytest.raises(RuntimeError, match="waited 0.1s"):
                    forge.reset(timeout=0.1)
            # And once it is idle again, the rebuild happens as usual.
            forge.reset()
            assert forge.branches("group/project") == ["main"]
        finally:
            forge.close()

    def test_the_refusal_is_fast_rather_than_waiting_out_the_default(
        self, tmp_path
    ):
        """A default-suite test must not sit for ten seconds.

        The bug this pins is not hypothetical: with `GIT_IDLE_TIMEOUT` bound as
        a parameter default, the timeout above could not be shortened at all
        and cost ten seconds on every full run.
        """
        forge = gitlab.serve(tmp_path / "repos")
        try:
            forge.seed_repo("group/project")
            with forge.serving_git():
                started = time.monotonic()
                with pytest.raises(RuntimeError):
                    forge.reset(timeout=0.1)
                elapsed = time.monotonic() - started
            assert elapsed < 2, f"the refusal took {elapsed:.1f}s, not ~0.1s"
        finally:
            forge.close()

    def test_a_request_arriving_mid_rebuild_waits_instead_of_reading_a_deletion(
        self, tmp_path
    ):
        """Waiting for idle is check-then-act on its own.

        `await_git_idle` drops the lock before the rmtree, so a request landing
        one instruction later has `git http-backend` reading a repository as it
        is deleted — a truncated packfile, reported by whichever scenario runs
        next as a corrupt repository. The gate is what closes it: while
        `reset` is rebuilding, a new `serving_git` blocks.
        """
        forge = gitlab.serve(tmp_path / "repos")
        try:
            forge.seed_repo("group/project")
            admitted = threading.Event()

            with forge._rebuild_gate() as idle:
                assert idle is True

                def enter():
                    with forge.serving_git(timeout=5):
                        admitted.set()

                waiter = threading.Thread(target=enter)
                waiter.start()
                # Held off for as long as the gate is held. A generous margin,
                # because asserting "did not happen" needs the scheduler to
                # have had a real chance to let it happen.
                assert not admitted.wait(timeout=0.5), (
                    "a git request was admitted while the repositories were "
                    "being rebuilt underneath it"
                )

            assert admitted.wait(timeout=5), "the gate never released the waiter"
            waiter.join(timeout=5)
        finally:
            forge.close()
