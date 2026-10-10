"""The full tier's own wiring, held down without booting six containers.

`tests/full/` costs a cold boot of the deployment as shipped, so everything
about it that *can* be checked in the default suite is checked here instead —
the same split, and the same reasoning, as `tests/test_smoke_tier.py` one shape
over. The things most worth guarding are pure functions: `full_config` decides
which subsystems a `full` profile turns on and how the daemon reaches the
bundled Nextcloud, and `full_env` decides which credentials the stack gets and
what URL Nextcloud will bake into its `oauth2_clients` row at first install. A
wrong answer costs minutes to discover any other way.

Some tests shell out to `docker compose config`, which is compose's own parser
and the only thing that applies the interpolation and schema rules a real
invocation will. It parses locally and needs no daemon.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import subprocess
from pathlib import Path
from urllib.error import URLError

import pytest

from istota.config import load_config
from testbed import profiles
from testbed import stack as compose_support
from testbed.services import nextcloud as nextcloud_service
from tests.support.monotonic_spy import monotonic_spy
from tests.support.nested_pytest import run_nested_pytest

REPO = Path(__file__).resolve().parents[1]

#: What the nested collect below walks. The guard asserts that selecting the
#: full tier under xdist is refused, which the tier's own directory witnesses —
#: the whole tree cost 26631 items and 12s warm to reach the same verdict
#: (ISSUE-492).
FULL_SCOPE = ["tests/full"]
FULL_COMPOSE = REPO / "docker" / "docker-compose.yml"
TESTBED_OVERLAY = REPO / "testbed" / "compose" / "testbed.yml"
NEXTCLOUD_OVERLAY = REPO / "testbed" / "compose" / "nextcloud.yml"

#: Obviously fake, so nothing here invents a credential-shaped string.
CREDENTIALS = compose_support.FullCredentials(
    postgres_password="unit-test-postgres",
    admin_password="unit-test-admin",
    bot_password="unit-test-bot",
    user_password="unit-test-user",
    nc_port=18080,
    oauth_client_id="unit-test-client",
    oauth_client_secret="unit-test-client-secret",
)

CONFIG_DIR = Path("/nonexistent/config")
SECRETS_DIR = Path("/nonexistent/secrets")


def _env(services=None, credentials=CREDENTIALS) -> dict[str, str]:
    return compose_support.full_env(
        services or {}, credentials, config_dir=CONFIG_DIR, secrets_dir=SECRETS_DIR,
        image="istota-test/lean:unit",
    )


def _config(services=None, profile=profiles.FULL) -> tuple[dict, dict[str, str]]:
    return compose_support.full_config(services or {}, CREDENTIALS, profile)


class _Stub:
    """A `Service` for the parts of the stack that only read `config()` and
    `compose_env()`."""

    def __init__(self, name: str, config: dict | None = None,
                 compose: dict[str, str] | None = None) -> None:
        self.name = name
        self._config = config or {}
        self._compose = compose

    def config(self) -> dict:
        return json.loads(json.dumps(self._config))

    def __getattr__(self, item):
        if item == "compose_env" and self._compose is not None:
            return lambda: dict(self._compose)
        raise AttributeError(item)


def _bare_profile(**changes) -> profiles.Profile:
    return dataclasses.replace(profiles.FULL, config={}, **changes)


class TestTheModuleSwitches:
    """What makes `Profile` mean anything on this shape.

    The dataclass defaults leave Talk and both sleep cycles on. A `full`
    profile declaring two services would otherwise boot a daemon running every
    one of them, which is what the profile mechanism exists to prevent and
    which on a shape with one shared stack couples every test to every
    background loop.
    """

    def test_everything_is_off_unless_a_service_asks_for_it(self):
        document, _ = _config(profile=_bare_profile())

        for path in compose_support.FULL_MODULE_SWITCHES:
            value = document
            for part in path:
                value = value[part]
            assert value is False, path

    def test_nextcloud_in_the_profile_is_what_turns_talk_on(self):
        """And it is the map's job rather than the service's `config()`, which
        is empty by design: a full stack is a Nextcloud install by definition,
        and whether Talk is *enabled* is a profile question."""
        with_nc, _ = _config({"nextcloud": _Stub("nextcloud")}, _bare_profile())
        without, _ = _config({}, _bare_profile())

        assert with_nc["talk"]["enabled"] is True
        assert without["talk"]["enabled"] is False

    def test_every_switch_names_a_real_config_key(self, tmp_path, monkeypatch):
        """A switch on a key the loader does not know is a warning and nothing
        else: the subsystem would stay on. Loaded, so the loader decides."""
        document, _ = _config(profile=_bare_profile())
        path = tmp_path / "config.toml"
        path.write_text(compose_support.toml_dumps(document))
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "admins"))
        config = load_config(path)

        assert config.talk.enabled is False
        assert config.talk.signaling.enabled is False
        assert config.sleep_cycle.enabled is False
        assert config.channel_sleep_cycle.enabled is False
        assert config.browser.enabled is False
        assert config.location.enabled is False

    def test_the_identity_variables_are_passed_through(self):
        """`FULL_IDENTITY` is what this file hands compose for the Nextcloud
        fixture's provisioning. `USER_NAME` is preflighted with
        `${USER_NAME:?}`, so getting its name wrong fails `up` during
        interpolation rather than at boot."""
        compose = NEXTCLOUD_OVERLAY.read_text()
        for variable in compose_support.FULL_IDENTITY:
            assert f"${{{variable}" in compose, variable

    def test_every_owner_is_a_service_that_exists_or_is_planned(self):
        """A typo in an owner name silently leaves its subsystem off forever."""
        from testbed.services import REGISTRY

        known = set(REGISTRY) | compose_support.PLANNED_SERVICES
        for path, owner in compose_support.FULL_MODULE_SWITCHES.items():
            assert owner == "" or owner in known, (path, owner)

    def test_a_planned_service_that_landed_must_be_taken_off_the_list(self):
        """The ratchet: leaving a landed name on the planned list would let a
        typo in a *future* planned one go unnoticed."""
        from testbed.services import REGISTRY

        assert not compose_support.PLANNED_SERVICES & set(REGISTRY)


class TestTheFullConfig:
    """The `config.toml` the full stack boots from, and the secret files beside it."""

    def test_it_loads_and_reaches_the_bundled_nextcloud(self, tmp_path, monkeypatch):
        mail = _Stub("mail", {"email": {"enabled": True, "authserv_id": "mail"}})
        document, secret_values = _config({"nextcloud": _Stub("nextcloud"), "mail": mail})
        path = tmp_path / "config.toml"
        path.write_text(compose_support.toml_dumps(document))
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "admins"))
        monkeypatch.setenv("ISTOTA_NEXTCLOUD_APP_PASSWORD",
                           secret_values["istota_nextcloud_app_password"])
        config = load_config(path)

        assert config.nextcloud.url == "http://nextcloud"
        assert config.nextcloud.username == "istota"
        assert config.nextcloud.app_password == "unit-test-bot"
        assert config.web.auth == ["nextcloud", "email"]
        assert config.web.oauth2_client_id == "unit-test-client"
        assert config.web.oauth2_redirect_uri == "http://localhost:18080/istota/callback"
        assert config.email.confirm_sender_match == "verify"

    def test_the_shared_mount_layout_is_the_moved_install_s(self):
        """`dav_prefix` names the bot's `Shared Files` mount and the boot-time
        OCS share-back is off, since `provision-nc.sh` already mounts the
        directory into the user's tree. Both are `config.toml` keys now, which
        an install moved off the compose Nextcloud keeps."""
        document, _ = _config()

        assert document["nextcloud"]["dav_prefix"] == nextcloud_service.BOT_MOUNT_POINT
        assert document["nextcloud"]["auto_share_bot_dir"] is False

    def test_no_credential_is_in_the_config(self):
        document, secret_values = _config()
        text = compose_support.toml_dumps(document)

        for name, value in secret_values.items():
            if value:
                assert value not in text, name

    def test_the_secret_files_name_only_declared_secrets(self):
        _, secret_values = _config()

        assert set(secret_values) <= set(compose_support.SECRET_NAMES)
        assert secret_values["istota_brain_native_api_key"] == (
            compose_support.SCRIPTED_ENDPOINT_KEY
        )

    def test_the_profile_may_override_a_service(self):
        mail = _Stub("mail", {"email": {"enabled": True, "confirm_sender_match": "off"}})
        document, _ = _config({"mail": mail})

        assert document["email"]["confirm_sender_match"] == "verify"

    def test_two_services_claiming_one_key_is_refused(self):
        """Silent last-wins would boot a stack pointing at the wrong service,
        with dict order deciding which."""
        one = _Stub("one", {"brain": {"native": {"base_url": "http://a"}}})
        two = _Stub("two", {"brain": {"native": {"base_url": "http://b"}}})

        with pytest.raises(compose_support.StackError, match="base_url"):
            _config({"one": one, "two": two})


class TestTheCallbackUrl:
    """The one value `provision-nc.sh` bakes in irreversibly at first install."""

    def test_it_is_written_out_rather_than_left_to_compose_defaults(self):
        assert _env()["ISTOTA_WEB_CALLBACK_URL"] == "http://localhost:18080/istota/callback"

    def test_it_agrees_with_the_port_the_stack_publishes(self):
        environment = _env()

        assert environment["NC_PORT"] in environment["ISTOTA_WEB_CALLBACK_URL"]

    def test_the_config_names_the_url_nextcloud_registers(self):
        """The redirect the web app sends and the one Nextcloud holds must be
        one string, or the OAuth2 login fails in a browser nobody here runs."""
        document, _ = _config()

        assert document["web"]["oauth2_redirect_uri"] == _env()["ISTOTA_WEB_CALLBACK_URL"]


class TestTheCredentials:
    def test_all_four_required_variables_are_present(self):
        """`docker-compose.yml` preflights each with `${…:?}`, so a missing one
        fails `up` during interpolation rather than at boot."""
        environment = _env()

        for key in compose_support.CREDENTIAL_KEYS:
            assert environment[key], key

    def test_the_oauth_pair_reaches_nextcloud_and_the_config(self):
        environment = _env()
        document, secret_values = _config()

        assert environment["ISTOTA_WEB_OAUTH2_CLIENT_ID"] == document["web"]["oauth2_client_id"]
        assert environment["ISTOTA_WEB_OAUTH2_CLIENT_SECRET"] == (
            secret_values["istota_web_oauth2_client_secret"]
        )

    def test_generated_credentials_are_all_different(self):
        generated = compose_support.generate_credentials(1234)

        assert len(set(generated.as_env().values())) == 6

    def test_a_generated_password_survives_an_env_file_round_trip(self, tmp_path):
        """`token_urlsafe` rather than anything with punctuation, because a
        compose env-file is parsed as bare `KEY=VALUE` with no quoting rules to
        hide behind."""
        generated = compose_support.generate_credentials(1234)
        path = compose_support.write_env_file(
            tmp_path / "compose.env", generated.as_env()
        )

        parsed = dict(
            line.split("=", 1) for line in path.read_text().splitlines() if line
        )
        assert parsed == generated.as_env()

    def test_the_repr_carries_no_password(self):
        """pytest renders the repr of whatever a failing comparison touched, and
        this object reaches a `Stack`."""
        generated = compose_support.generate_credentials(1234)

        rendered = repr(generated)
        for value in generated.as_env().values():
            assert value not in rendered
        assert "1234" in rendered

    def test_the_env_a_stack_exposes_has_them_redacted(self):
        environment = _env()
        stack = compose_support.Stack(
            profile=profiles.FULL,
            args=["docker", "compose", "--project-name", "unit"],
            services={},
            env=environment,
        )

        for key in compose_support.CREDENTIAL_KEYS:
            assert stack.env[key] == "<redacted>"
        assert stack.env["ISTOTA_WEB_OAUTH2_CLIENT_SECRET"] == "<redacted>"
        # And the things a scenario actually reads survive.
        assert stack.env["ISTOTA_WEB_CALLBACK_URL"] == environment[
            "ISTOTA_WEB_CALLBACK_URL"
        ]
        assert stack.env["USER_NAME"] == "testuser"

    def test_a_service_published_credential_is_redacted_by_shape(self):
        """Not just the four passwords by name: a service's `compose_env()`
        lands on the same map, and a name allowlist is a thing a future service
        has to remember to extend."""
        signaling = _Stub("signaling", compose={"ISTOTA_TALK_SIGNALING_SECRET": "a-secret"})
        environment = _env({"signaling": signaling})

        assert compose_support.redacted(environment)[
            "ISTOTA_TALK_SIGNALING_SECRET"
        ] == "<redacted>"

    @pytest.mark.parametrize(
        "key",
        [
            "SOMETHING_PASSWORD",
            "SOMETHING_TOKEN",
            "SOMETHING_SECRET",
            "SOMETHING_KEY",
            "ISTOTA_BRAIN_NATIVE_API_KEY",
        ],
    )
    def test_credential_shaped_names_are_recognised(self, key):
        assert compose_support.is_credential_key(key), key

    @pytest.mark.parametrize("key", ["USER_NAME", "NC_PORT", "ISTOTA_TALK_ENABLED"])
    def test_ordinary_names_are_not(self, key):
        """The direction that would make `stack.env` useless."""
        assert not compose_support.is_credential_key(key), key


class TestTheEnvFile:
    def test_a_newline_in_a_value_is_refused_by_name(self, tmp_path):
        """A compose env-file has no escaping, so a newline silently becomes a
        second malformed entry — which reads as *unset*, which on this compose
        file is a `${…:?}` failure blamed on the wrong key."""
        with pytest.raises(compose_support.StackError, match="TOKEN"):
            compose_support.write_env_file(
                tmp_path / "compose.env", {"TOKEN": "one\ntwo"}
            )

    def test_an_inline_comment_marker_is_refused(self, tmp_path):
        """Compose reads the rest of the line as a comment, so the value is
        silently truncated — which downstream reads as a wrong value rather
        than as a parse error."""
        with pytest.raises(compose_support.StackError, match="TOKEN"):
            compose_support.write_env_file(
                tmp_path / "compose.env", {"TOKEN": "one # two"}
            )

    def test_surrounding_whitespace_is_refused(self, tmp_path):
        with pytest.raises(compose_support.StackError, match="TOKEN"):
            compose_support.write_env_file(
                tmp_path / "compose.env", {"TOKEN": " padded "}
            )

    def test_it_is_written_private(self, tmp_path):
        path = compose_support.write_env_file(tmp_path / "compose.env", {"A": "b"})

        assert oct(path.stat().st_mode)[-3:] == "600"

    def test_it_is_never_world_readable_even_for_an_instant(self, tmp_path, monkeypatch):
        """`write_text` then `chmod` leaves four passwords at the process umask
        for the length of the write. Asserted by watching the mode the file is
        *created* with rather than the mode it ends up with, which is what the
        test above already covers and what the bug would have satisfied."""
        seen = []
        real_open = os.open

        def watched(path, flags, mode=0o777, **kwargs):
            seen.append(mode)
            return real_open(path, flags, mode, **kwargs)

        monkeypatch.setattr(os, "open", watched)
        compose_support.write_env_file(tmp_path / "compose.env", {"A": "b"})

        assert 0o600 in seen, seen


class TestTheProcessEnvironmentGuard:
    """Compose interpolates from its own environment before the env-file.

    None of the failures is loud: an exported `ADMIN_PASSWORD` gives 401s that
    read as "Talk is broken", an exported empty `USER_NAME` fails
    `${USER_NAME:?}` on every subcommand — which `_service_state` reports as
    "no container yet" and `down` swallows.
    """

    def test_a_differing_exported_value_is_reported(self, monkeypatch):
        monkeypatch.setenv("USER_NAME", "someone-else")

        assert compose_support.conflicting_process_env(_env()) == {"USER_NAME": "testuser"}

    def test_an_exported_empty_value_counts(self, monkeypatch):
        """The one that breaks *every* compose subcommand rather than one."""
        monkeypatch.setenv("USER_NAME", "")

        assert "USER_NAME" in compose_support.conflicting_process_env(_env())

    def test_an_identical_exported_value_is_not_fought_with(self, monkeypatch):
        monkeypatch.setenv("USER_NAME", "testuser")

        assert compose_support.conflicting_process_env(_env()) == {}

    def test_the_boot_refuses_before_it_builds_anything(self, tmp_path, monkeypatch):
        """Before a socket and before an image.

        `services.build` opens a listener on every interface and the boot then
        builds an image, so a refusal that waited for the full environment map
        would pay for both before saying no. The profile here names no services,
        so nothing can be constructed — if the check had not already run, `up`
        would be the next thing to fail and it would fail differently.
        """
        monkeypatch.setenv("USER_NAME", "someone-else")
        bare = dataclasses.replace(profiles.FULL, services=())
        pool = _pool(tmp_path)

        with pytest.raises(compose_support.StackError, match="USER_NAME"):
            pool.get(bare)

    def test_a_service_claiming_a_variable_twice_is_refused(self):
        """Silent last-wins would boot a stack pointing at the wrong service,
        with dict order deciding which."""
        one = _Stub("one", compose={"SOME_IMAGE_TAG": "a"})
        two = _Stub("two", compose={"SOME_IMAGE_TAG": "b"})

        with pytest.raises(compose_support.StackError, match="SOME_IMAGE_TAG"):
            _env({"one": one, "two": two})

    @pytest.mark.parametrize(
        "key",
        [
            "USER_NAME", "BOT_PASSWORD", "NC_PORT", "ISTOTA_WEB_CALLBACK_URL",
            "ISTOTA_SECRETS_DIR", "ISTOTA_TEST_CONFIG_DIR",
        ],
    )
    def test_a_service_may_not_overwrite_what_the_stack_owns(self, key):
        """A service that quietly renamed `USER_NAME` would leave
        `NextcloudService` authenticating as a user the stack never created; one
        that moved `NC_PORT` would leave the OAuth2 redirect URI baked at a port
        nothing publishes; one that moved the secrets directory would boot a
        daemon with somebody else's credentials."""
        rogue = _Stub("rogue", compose={key: "something-else"})

        with pytest.raises(compose_support.StackError, match=key):
            _env({"rogue": rogue})

    def test_nothing_istota_reads_as_configuration_is_in_the_env_file(self):
        """The config is an input now: compose passes istota none of it, so a
        stray `ISTOTA_EMAIL_*` here would be a value nothing reads."""
        environment = _env()
        allowed = {
            "ISTOTA_BOT_NAME", "ISTOTA_WEB_CALLBACK_URL", "ISTOTA_TALK_SIGNALING_PORT",
            "ISTOTA_TALK_SIGNALING_BACKEND_URLS", "ISTOTA_TEST_CONFIG_DIR",
            "ISTOTA_SECRETS_DIR", "ISTOTA_WEB_OAUTH2_CLIENT_ID",
            "ISTOTA_WEB_OAUTH2_CLIENT_SECRET", "ISTOTA_TEST_IMAGE",
            "ISTOTA_TESTBED_COMPOSE_DIR", "ISTOTA_TEST_BROWSER_IMAGE",
        }
        assert {k for k in environment if k.startswith("ISTOTA_")} == allowed


class TestReadiness:
    def test_the_full_shape_waits_on_nextcloud_and_istota(self):
        assert compose_support.READY_SERVICES["full"] == ("nextcloud", "istota")

    def test_it_waits_on_neither_web_nor_nginx(self):
        """`web` starts only once `istota` is healthy and `nginx` depends on
        `web`, so waiting on `istota` covers both, and waiting on either would
        only add their start-up to a budget the cold Nextcloud already spends."""
        assert "web" not in compose_support.READY_SERVICES["full"]
        assert "nginx" not in compose_support.READY_SERVICES["full"]

    def test_the_shared_budget_is_shared(self, monkeypatch):
        """Not `timeout` each. The second wait is only interesting once the
        first has finished, and giving each the whole budget would let a stack
        spend fifty minutes reporting a failure visible in ten."""
        seen = []

        def fake_wait(args, service, timeout, env=None):
            seen.append((service, timeout))

        monkeypatch.setattr(compose_support, "wait_ready", fake_wait)
        compose_support.wait_all_ready([], ("a", "b"), timeout=100)

        assert [service for service, _ in seen] == ["a", "b"]
        assert all(timeout <= 100 for _, timeout in seen)

    def test_an_exhausted_budget_names_what_was_reached(self, monkeypatch):
        """The message has to distinguish "b never got waited on" from "b timed
        out", because they are different faults: the first is a slow `a`."""
        clock = [0.0]
        monotonic_spy(monkeypatch, compose_support, lambda: clock[0])

        def slow(args, service, timeout, env=None):
            clock[0] += 1000

        monkeypatch.setattr(compose_support, "wait_ready", slow)
        with pytest.raises(TimeoutError, match=r"reached: \('a',\)"):
            compose_support.wait_all_ready([], ("a", "b"), timeout=100)


class TestTheSchedulerProbe:
    """`wait_healthy`'s second condition, and the way it was wrong.

    The compose health check looks for the `tasks` table, which is honest at a
    cold boot and nearly useless after a restart: the database is on a named
    volume and survives, so the probe passes within seconds while the entrypoint
    is still re-provisioning. The full shape therefore also waits for pid 1 to
    be the scheduler.

    The first version globbed `/proc/[0-9]*/cmdline` and matched *its own shell*
    — the probe runs as `sh -c '<script>'` and the script text contains the
    literal it greps for — so it returned 0 on the first poll of any container.
    Measured in a bare `alpine`, which contains no istota at all.
    """

    def test_it_reads_pid_one_and_does_not_scan(self):
        assert "/proc/1/cmdline" in compose_support._SCHEDULER_RUNNING
        assert "/proc/[0-9]" not in compose_support._SCHEDULER_RUNNING

    def test_the_probing_shells_own_command_line_cannot_satisfy_it(self):
        """The property, stated so it survives a rewrite.

        Whatever this probe becomes, it must not be satisfiable by the argv of
        the `sh -c` running it — which necessarily contains the string it looks
        for. Reading only a path that is not the probing process is what
        guarantees that; the behavioural half runs in `tests/full/`, where a
        container is already up.
        """
        script = compose_support._SCHEDULER_RUNNING

        assert "istota-scheduler" in script
        # The only file it reads is pid 1's, and a `docker compose exec` shell
        # is never pid 1.
        assert script.count("/proc/") == 1

    def test_wait_healthy_has_a_floor_under_a_spent_budget(self, tmp_path, monkeypatch):
        """`_boot_stack` hands over the remainder of a budget `up` has eaten
        into, and on the full shape `up` blocks on the fixture's health check,
        which allows 300s of start period plus twenty 15s retries. Without a
        floor a slow but correct cold boot arrives here with one second."""
        seen = []
        monkeypatch.setattr(
            compose_support,
            "wait_ready",
            lambda args, service, timeout, env=None: seen.append(timeout),
        )
        stack = compose_support.Stack(
            profile=profiles.BASE,
            args=["docker", "compose", "--project-name", "unit"],
            services={},
        )
        monkeypatch.setattr(
            stack, "exec", lambda argv, **kw: subprocess.CompletedProcess(argv, 0, "", ""),
        )

        stack.wait_healthy(timeout=1)

        assert seen == [compose_support.READY_TIMEOUT]


class TestTheKeptProjectIsNotSwept:
    def test_the_marker_is_in_the_project_name(self, tmp_path):
        pool = _pool(tmp_path, keep=True)
        args, _ = pool._compose_args(profiles.FULL, tmp_path)

        assert compose_support.KEEP_PROJECT_MARKER in _project(args)

    def test_the_sweep_leaves_it_alone(self, monkeypatch):
        """A clean kept teardown removes the containers, so `compose ls` does
        not report the project. A *killed* one leaves them, and the sweep's
        `down --volumes` would destroy the volumes KEEP exists to keep."""
        kept = f"istota-testbed{compose_support.KEEP_PROJECT_MARKER}abc12345"
        listing = json.dumps(
            [{"Name": kept}, {"Name": "istota-testbed-lean-99999999"}]
        )
        torn_down = []

        def fake_run(argv, **kwargs):
            if argv[:3] == ["docker", "compose", "ls"]:
                return subprocess.CompletedProcess(argv, 0, listing, "")
            torn_down.append(argv[argv.index("--project-name") + 1])
            return subprocess.CompletedProcess(argv, 0, "", "")

        monkeypatch.setattr(compose_support.subprocess, "run", fake_run)
        compose_support.sweep_projects("istota-testbed-")

        assert torn_down == ["istota-testbed-lean-99999999"]


class TestTheComposeInvocation:
    def test_the_overlays_go_on_in_their_order(self, tmp_path):
        """Shipped file, profile overlays, concessions, then the Nextcloud
        fixture, whose `/mnt/shared` has to win over the concessions' empty
        one. A profile overlay adds a service; it comes before the
        concessions."""
        pool = _pool(tmp_path)
        profile = profiles.FULL.__class__(
            "x", shape="full", compose_overlays=(Path("/tmp/mail.yml"),)
        )

        args, _ = pool._compose_args(profile, tmp_path)

        files = [args[i + 1] for i, token in enumerate(args) if token == "-f"]
        assert files == [
            str(FULL_COMPOSE), "/tmp/mail.yml", str(TESTBED_OVERLAY), str(NEXTCLOUD_OVERLAY),
        ]

    def test_the_lean_shape_carries_no_fixture(self, tmp_path):
        pool = _pool(tmp_path)

        args, _ = pool._compose_args(profiles.BASE, tmp_path)

        files = [args[i + 1] for i, token in enumerate(args) if token == "-f"]
        assert files == [str(FULL_COMPOSE), str(TESTBED_OVERLAY)]

    def test_the_env_file_rides_in_the_argument_list(self, tmp_path):
        """Compose interpolates on *every* subcommand, so a variable supplied
        only to `up` makes `ps`, `exec`, `logs` and `down` fail during
        interpolation, before they touch a container."""
        pool = _pool(tmp_path)

        args, env_file = pool._compose_args(profiles.FULL, tmp_path)

        assert "--env-file" in args
        assert args[args.index("--env-file") + 1] == str(env_file)

    def test_a_kept_project_name_is_stable_and_an_ephemeral_one_is_not(
        self, tmp_path
    ):
        """Compose scopes a named volume to the project, so a fresh uuid every
        session would leave the kept volumes attached to a project nothing ever
        looks at again — `KEEP` would keep a growing pile of orphans and cache
        nothing."""
        ephemeral = _pool(tmp_path, keep=False)
        first, _ = ephemeral._compose_args(profiles.FULL, tmp_path)
        second, _ = ephemeral._compose_args(profiles.FULL, tmp_path)
        assert _project(first) != _project(second)

        kept = _pool(tmp_path, keep=True)
        one, _ = kept._compose_args(profiles.FULL, tmp_path)
        two, _ = kept._compose_args(profiles.FULL, tmp_path)
        assert _project(one) == _project(two)


class TestKeepSemantics:
    """`ISTOTA_TESTBED_KEEP`, and the two corrections the boot path forces.

    The spec's version wiped `shared_files` along with `istota_data`. That
    removes `/mnt/shared/.istota-provisioned`, which `provision-nc.sh` never
    rewrites — it is a `post-installation` hook and the Nextcloud image runs
    those only when it performs the install — so the fixture's health check
    waits for a flag nothing will write, and `istota`, which depends on it,
    never starts.
    """

    def test_only_the_daemons_own_volumes_are_wiped(self):
        assert set(compose_support.StackPool.KEEP_WIPES) == {
            "istota_data",
            "redis_data",
        }

    def test_the_nextcloud_volumes_are_not_wiped(self):
        """They hold the whole cost: the install, and the two app-store
        downloads `provision-nc.sh` triggers."""
        for volume in ("nextcloud_html", "nextcloud_data", "postgres_data"):
            assert volume not in compose_support.StackPool.KEEP_WIPES

    def test_shared_files_is_not_wiped(self):
        assert "shared_files" not in compose_support.StackPool.KEEP_WIPES

    def test_an_ephemeral_session_gets_a_fresh_port_per_stack(self, tmp_path):
        """The full shape publishes nginx on a *reserved* host port, and the
        pool can hold two full stacks at once — a `fresh=True` one alongside a
        cached one, or two `fresh=True` ones from different modules. One
        memoized port makes the second `up` fail on a bind, naming a port rather
        than the reason. Each stack is its own Nextcloud, so there is nothing to
        share."""
        pool = _pool(tmp_path, keep=False)

        first = pool._full_credentials()
        second = pool._full_credentials()

        assert first.nc_port != second.nc_port

    def test_a_kept_session_reuses_the_port_and_the_passwords(self, tmp_path):
        """The kept volumes' Nextcloud users already have these passwords and
        its OAuth2 client already names this port — `provision-nc.sh` bakes the
        redirect URI at first install and does not revisit it."""
        pool = _pool(tmp_path, keep=True)

        first = pool._full_credentials()
        second = _pool(tmp_path, keep=True)._full_credentials()

        assert first == second

    def test_the_kept_credentials_file_is_private(self, tmp_path):
        pool = _pool(tmp_path, keep=True)
        pool._full_credentials()

        assert oct((tmp_path / "keep" / "credentials.json").stat().st_mode)[-3:] == "600"

    def test_a_lean_stack_loses_its_volumes_even_in_a_kept_session(
        self, tmp_path, monkeypatch
    ):
        """One pool serves both shapes. A lean stack's named volume is the
        framework DB every assertion is read out of, so keeping it would make
        the next session's assertions depend on this one's rows."""
        seen = []
        monkeypatch.setattr(
            compose_support,
            "down",
            lambda args, volumes=False, env=None: seen.append(volumes),
        )
        pool = _pool(tmp_path, keep=True)

        pool._down(["docker", "compose", "--project-name", "p"], shape="lean")

        assert seen == [True]


class TestTheOverlayIsAddressable:
    """Parsed by compose itself, which is the only thing that decides."""

    def test_the_merged_model_parses(self, tmp_path):
        config = _compose_config(tmp_path)

        assert "istota" in config["services"]

    def test_the_seccomp_grant_reaches_the_istota_service(self, tmp_path):
        """Without it Docker's default profile blocks the
        `unshare(CLONE_NEWUSER)` bubblewrap needs, and every task that runs a
        Bash tool call fails. Measured on the shipped image: `bwrap
        --unshare-user --ro-bind / / -- /bin/true` exits 1 without the grant and
        0 with it. The grant is the shipped profile now, from the shipped file,
        and the overlay must not widen it back to `seccomp:unconfined`."""
        config = _compose_config(tmp_path)
        opts = config["services"]["istota"]["security_opt"]

        assert "seccomp=./istota/seccomp-istota.json" in opts
        assert not [o for o in opts if "unconfined" in o and o.startswith("seccomp")]

    def test_the_unmasked_system_paths_reach_the_istota_service(self, tmp_path):
        """The second half of the same grant, and the quiet one.

        seccomp lets bwrap create the user namespace; it does not let it mount
        a procfs inside one, because Docker's masked `/proc` entries and
        read-only `/proc/sys` make the container's procfs not "fully visible"
        to the kernel. `build_bwrap_cmd` emits `--proc /proc`, so without this
        no sandbox can start — and `_bwrap_available` performs the same mounts,
        so the daemon answers by disabling the sandbox and carrying on. The
        loss is therefore silent rather than red, which is what makes a guard
        on a compose line worth having.
        """
        config = _compose_config(tmp_path)

        assert "systempaths=unconfined" in config["services"]["istota"]["security_opt"]

    def test_the_host_gateway_alias_reaches_the_istota_service(self, tmp_path):
        """`host.docker.internal` is built in on Docker Desktop and absent on
        Docker Engine, and every full-shape task reaches the scripted endpoint
        through it."""
        config = _compose_config(tmp_path)
        hosts = config["services"]["istota"]["extra_hosts"]

        assert any("host-gateway" in str(entry) for entry in _entries(hosts))

    def test_the_istota_service_gains_a_health_check(self, tmp_path):
        """The shipped service has none, and `restart: unless-stopped` makes a
        wedged boot look alive — so without this `wait_ready` has nothing
        honest to wait on."""
        config = _compose_config(tmp_path)
        check = config["services"]["istota"]["healthcheck"]

        assert "tasks" in " ".join(str(part) for part in check["test"])

    def test_no_brain_credential_can_come_from_the_shell(self, tmp_path):
        """Compose lets the *process* environment outrank an `--env-file`, so an
        interpolated `${ANTHROPIC_API_KEY}` would hand a developer's exported
        key to a test container posting to a listener on their machine. The
        shipped file interpolates none any more: credentials are secret files,
        and this stack's are the scratch directory's. Asserted with all three
        exported to a value nothing should adopt."""
        poisoned = {
            "ANTHROPIC_API_KEY": "not-a-real-key-but-exported",
            "CLAUDE_CODE_OAUTH_TOKEN": "not-a-real-token-but-exported",
            "ISTOTA_BRAIN_NATIVE_API_KEY": "not-a-real-key-but-exported",
        }
        config = _compose_config(tmp_path, extra_env=poisoned)

        for service in ("istota", "web"):
            environment = config["services"][service].get("environment") or {}
            assert not set(poisoned) & set(environment), service
        for name, spec in config["secrets"].items():
            assert spec["file"] == str(SECRETS_DIR / name), name

    @pytest.mark.parametrize("service", ["istota", "web"])
    def test_the_config_directory_is_bound_read_only(self, tmp_path, service):
        """Where `istota setup` would have written it, from the session's
        scratch directory, for both services that read it."""
        config = _compose_config(tmp_path)
        binds = [
            v for v in config["services"][service]["volumes"]
            if v.get("target") == "/data/config"
        ]

        assert binds and binds[0]["source"] == str(CONFIG_DIR), binds
        assert binds[0].get("read_only") is True


class TestTheFullProfile:
    def test_it_declares_the_full_shape(self):
        assert profiles.FULL.shape == "full"

    def test_it_is_the_only_one(self):
        """Every other axis in this tier gets its own profile, because on the
        lean shape a profile costs thirty seconds. On the full shape it costs
        minutes, and `StackPool` keys by name — so `full` plus `full-mail` is
        two cold boots of the same six containers to run four scenarios."""
        full_profiles = [p for p in profiles.ALL if p.shape == "full"]

        assert [p.name for p in full_profiles] == ["full"]

    def test_the_user_ids_the_service_defaults_to_are_the_ones_compose_gets(self):
        """`testbed.stack` imports `testbed.services`, so the two cannot share a
        constant without a cycle. Pinned here instead: a service authenticating
        as a user the stack never created reads as "Talk is broken"."""
        assert (
            nextcloud_service.DEFAULT_BOT_USER
            == compose_support.FULL_IDENTITY["BOT_USER"]
        )
        assert (
            nextcloud_service.DEFAULT_TEST_USER
            == compose_support.FULL_IDENTITY["USER_NAME"]
        )


class TestTheNextcloudReset:
    """What `reset` touches, and — more importantly — what it does not.

    The scope was settled by experiment against a booted stack (spec Open
    question 3): a room can be deleted cleanly through the API the daemon's own
    client exposes, and it takes its messages and its invite notification with
    it. So the scope is exactly the rooms this object created, and everything
    the boot made is baseline. These tests hold that boundary without a server,
    by recording the calls the reset would make.
    """

    @staticmethod
    def _service(monkeypatch, *, fail: set[str] = frozenset()):
        service = nextcloud_service.attach(
            base_url="http://localhost:1",
            admin_password="unit-admin",
            bot_password="unit-bot",
            test_password="unit-user",
        )
        calls: list[tuple[str, str, str]] = []

        def fake_ocs(path, *, user="", method="GET", body=None, tolerate=()):
            calls.append((method, path, user))
            token = path.rsplit("/", 1)[-1]
            if method == "POST" and path.endswith("/room"):
                return nextcloud_service.OcsResponse(
                    200, "OK", {"token": f"token-{len(calls)}"}
                )
            if token in fail:
                # An OCS v2 refusal *raises* — the HTTP status carries it, so
                # `_ocs` never returns a 403. A fake that returned one would
                # verify the guard against a shape the real client cannot
                # produce, which is how the first version of this test passed
                # while the code aborted the loop.
                raise nextcloud_service.NextcloudError(f"DELETE {path} HTTP 403")
            return nextcloud_service.OcsResponse(200, "OK", {})

        monkeypatch.setattr(service, "_ocs", fake_ocs)
        return service, calls

    def test_it_deletes_the_rooms_it_created_and_nothing_else(self, monkeypatch):
        service, calls = self._service(monkeypatch)
        first = service.create_room(name="one")
        second = service.create_room(name="two")
        calls.clear()

        service.reset()

        assert [(method, path) for method, path, _ in calls] == [
            ("DELETE", f"/ocs/v2.php/apps/spreed/api/v4/room/{first}"),
            ("DELETE", f"/ocs/v2.php/apps/spreed/api/v4/room/{second}"),
        ]

    def test_a_room_is_deleted_by_the_actor_that_created_it(self, monkeypatch):
        """Talk lets a moderator delete, and the creator is one. Deleting as the
        bot would work for a bot-created room and 403 for a user-created one —
        which is every room a scenario makes, since rooms are user-created."""
        service, calls = self._service(monkeypatch)
        service.create_room(name="one")
        calls.clear()

        service.reset()

        assert [user for _, _, user in calls] == [service.test_user]

    def test_a_second_reset_deletes_nothing(self, monkeypatch):
        service, calls = self._service(monkeypatch)
        service.create_room(name="one")
        service.reset()
        calls.clear()

        service.reset()

        assert calls == []

    def test_a_room_that_survives_is_reported_rather_than_swallowed(
        self, monkeypatch
    ):
        """A leaked room is a cross-test dependency, and those get diagnosed as
        flake in whichever later scenario happens to trip over it."""
        service, _ = self._service(monkeypatch, fail={"token-1"})
        service.create_room(name="one")

        with pytest.raises(nextcloud_service.NextcloudError, match="survived"):
            service.reset()

    def test_one_bad_room_does_not_hide_the_others(self, monkeypatch):
        """Every room is attempted, and the list is cleared whatever happened.

        Both halves matter and both were wrong first time. Aborting at the
        first bad room leaves the rest of the session's rooms behind it; not
        clearing the list re-attempts the same delete before *every* remaining
        test and reports the first test's problem against all of them.
        """
        service, calls = self._service(monkeypatch, fail={"token-1"})
        service.create_room(name="one")
        service.create_room(name="two")
        calls.clear()

        with pytest.raises(nextcloud_service.NextcloudError):
            service.reset()

        assert [path.rsplit("/", 1)[-1] for _, path, _ in calls] == [
            "token-1", "token-2",
        ]
        assert service._created_rooms == []

    def test_a_room_already_gone_is_not_a_failure(self, monkeypatch):
        """A scenario may delete its own room to assert on the deletion."""
        service, _ = self._service(monkeypatch)
        service.create_room(name="one")
        monkeypatch.setattr(
            service, "_ocs",
            lambda *a, **k: nextcloud_service.OcsResponse(404, "not found", None),
        )
        monkeypatch.setattr(service, "rooms", lambda **k: [])

        service.reset()

    def test_a_404_the_bot_can_still_see_is_a_failure(self, monkeypatch):
        """Talk answers 404 for a room the *actor* cannot see, too.

        Believing it would write off a room that is still there, still polled,
        and still in the next test's way — the one outcome this reset promises
        to be loud about.
        """
        service, _ = self._service(monkeypatch)
        token = service.create_room(name="one")
        monkeypatch.setattr(
            service, "_ocs",
            lambda *a, **k: nextcloud_service.OcsResponse(404, "not found", None),
        )
        monkeypatch.setattr(service, "rooms", lambda **k: [{"token": token}])

        with pytest.raises(nextcloud_service.NextcloudError, match="still sees it"):
            service.reset()

    def test_the_baseline_the_boot_creates_is_named_in_the_docstring(self):
        """The scope is only usable if it is written where a reader will find it.

        Enumerated rather than gestured at, because the failure it prevents is
        somebody reading `rooms()` returning six entries as leakage and
        "fixing" the reset to delete them.
        """
        body = nextcloud_service.NextcloudService.reset.__doc__ or ""

        for baseline in ("provision-rooms", "#alerts", "/mnt/shared", "baseline"):
            assert baseline in body, baseline


class TestTheConnectionRetry:
    """Reads are retried through a cold nginx. Writes are not, and must not be."""

    @staticmethod
    def _service():
        return nextcloud_service.attach(
            base_url="http://localhost:1",
            admin_password="unit-admin",
            bot_password="unit-bot",
            test_password="unit-user",
        )

    def test_a_write_that_cannot_connect_fails_at_once(self, monkeypatch):
        """A `URLError` can be raised after the request reached the server.

        Replaying a POST is how you get a second Talk room whose token nothing
        recorded — which `reset` then cannot delete — or a duplicate inbound
        message, which turns "exactly one reply" into a flake.
        """
        service = self._service()
        attempts = []

        def refuse(*args, **kwargs):
            attempts.append(1)
            raise URLError("connection refused")

        monkeypatch.setattr(nextcloud_service, "urlopen", refuse)
        monkeypatch.setattr(nextcloud_service, "CONNECT_RETRY_POLL_SECONDS", 0.0)

        with pytest.raises(nextcloud_service.NextcloudError, match="not retried"):
            service._ocs("/ocs/v2.php/x", method="POST", body={"a": 1})

        assert len(attempts) == 1

    def test_a_read_is_retried_until_the_window_closes(self, monkeypatch):
        service = self._service()
        attempts = []

        def refuse(*args, **kwargs):
            attempts.append(1)
            raise URLError("connection refused")

        monkeypatch.setattr(nextcloud_service, "urlopen", refuse)
        monkeypatch.setattr(nextcloud_service, "CONNECT_RETRY_POLL_SECONDS", 0.0)
        monkeypatch.setattr(nextcloud_service, "CONNECT_RETRY_SECONDS", 3)
        # The window is wall-clock, so zeroing the poll alone would spin a core
        # for the whole three seconds — in the default suite, under `-n auto`.
        # Drive the clock instead: the deadline is start + 3, so the third
        # failure is the one that gives up.
        ticks = iter([0.0, 1.0, 2.0, 3.0])
        monotonic_spy(monkeypatch, nextcloud_service, lambda: next(ticks, 99.0))

        with pytest.raises(nextcloud_service.NextcloudError, match="within 3s"):
            service._ocs("/ocs/v2.php/x")

        assert len(attempts) > 1


#: One depth-1 PROPFIND answer: the collection, then one file whose name needs
#: decoding. Shared by the tests below so the two claims are made against the
#: same bytes.
_PROPFIND = (
    "<d:multistatus xmlns:d='DAV:'>"
    "<d:response><d:href>/remote.php/dav/files/istota/Shared%20Files/"
    "</d:href></d:response>"
    "<d:response><d:href>/remote.php/dav/files/istota/Shared%20Files/"
    "Users/testuser/inbox/a%20note.txt</d:href></d:response>"
    "</d:multistatus>"
).encode()


class TestTheWebdavPathTranslation:
    """`files()` reports paths relative to the account's own DAV root."""

    @staticmethod
    def _service():
        return nextcloud_service.attach(
            base_url="http://localhost:1",
            admin_password="unit-admin",
            bot_password="unit-bot",
            test_password="unit-user",
        )

    def test_the_dav_prefix_is_dropped_and_the_path_is_decoded(
        self, monkeypatch
    ):
        service = self._service()
        monkeypatch.setattr(service, "_dav", lambda *a, **k: (_PROPFIND, 207))

        assert service.files("Shared Files") == [
            "Shared Files/Users/testuser/inbox/a note.txt",
        ]

    def test_the_requested_collection_is_not_in_its_own_listing(
        self, monkeypatch
    ):
        """Otherwise `assert files(d)` is non-empty for an *empty* directory.

        A depth-1 PROPFIND answers with the collection first, so a caller
        checking "the mount resolves to something" would be checking that it
        asked for something — an assertion that cannot fail, which this repo
        has shipped twice.
        """
        service = self._service()
        only_itself = (
            "<d:multistatus xmlns:d='DAV:'>"
            "<d:response><d:href>/remote.php/dav/files/istota/Shared%20Files/"
            "</d:href></d:response></d:multistatus>"
        ).encode()
        monkeypatch.setattr(service, "_dav", lambda *a, **k: (only_itself, 207))

        assert service.files("Shared Files") == []
        assert service.files("/Shared Files/") == []

    def test_a_non_multistatus_answer_names_the_status(self, monkeypatch):
        """A 404 here means the path does not exist in that account's tree,
        which on this shape is the ordinary way of getting the mount point
        wrong — and an empty list would read as an empty directory."""
        service = self._service()
        monkeypatch.setattr(service, "_dav", lambda *a, **k: (b"nope", 404))

        with pytest.raises(nextcloud_service.NextcloudError, match="404"):
            service.files("Users/testuser")


class TestTheMarker:
    def test_the_full_tier_is_deselected_by_default(self):
        """Otherwise `uv run pytest` boots six containers."""
        body = (REPO / "pyproject.toml").read_text()

        addopts = next(
            line for line in body.splitlines() if line.startswith("addopts = ")
        )
        # Not `and not full'` — that spelling asserted `full` was the *last*
        # marker in the expression, so adding any marker after it broke a test
        # about a different tier. Assert membership, not position.
        assert "not full" in addopts, addopts

    def test_every_full_test_carries_the_marker(self):
        files = sorted((REPO / "tests" / "full").glob("test_*.py"))
        assert files, "no full tests found; this guard would pass vacuously"
        for path in files:
            assert "pytestmark = pytest.mark.full" in path.read_text(), path

    def test_the_xdist_guard_covers_it(self):
        """Session-scoped fixtures are per-worker, so N workers would each bring
        up their own six-container stack under one project prefix."""
        result = run_nested_pytest(
            scope=FULL_SCOPE,
            args=["-m", "full", "-n", "2", "--collect-only", "-q"],
            cwd=REPO,
        )

        assert result.returncode == 4, result.stdout + result.stderr
        assert "must run with -n0" in result.stdout + result.stderr


class TestTheProvisioningSuiteRefusesAKeptVolumeSet:
    def test_it_skips_rather_than_asserting_against_a_previous_session(self):
        """Everything in `tests/full/test_provisioning.py` asserts on what
        first-install provisioning wrote, and `KEEP` persists the volumes whose
        first install happened in a previous session — where the
        `post-installation` hook that runs `provision-nc.sh` will not run
        again."""
        result = run_nested_pytest(
            scope=["tests/full/test_provisioning.py"],
            args=["-m", "full", "--collect-only", "-q", "-n0"],
            cwd=REPO,
            env={**os.environ, "ISTOTA_TESTBED_KEEP": "1"},
        )

        # Collection succeeds; the skip is at session-fixture setup. What this
        # asserts is that the guard is wired to the tier at all — the message
        # itself is checked by reading it, since running the fixture means
        # booting the stack the guard exists to avoid.
        assert result.returncode == 0, result.stdout + result.stderr
        body = (REPO / "tests" / "full" / "conftest.py").read_text()
        assert "ISTOTA_TESTBED_KEEP" in body
        assert "pytest.skip" in body


class TestTheSharedMountName:
    """One value, and the places that have to agree on it.

    `provision-nc.sh` creates the bot's `files_external` mount, and the mount
    point it chooses *is* the prefix the daemon has to put in front of every
    logical path before it becomes a DAV or OCS path — `/Users/alice` on the
    volume is `/Shared Files/Users/alice` in the bot's Nextcloud tree. The
    fixture hands the name to the Nextcloud container; the daemon reads it from
    its config, `[nextcloud] dav_prefix`, which the testbed writes as an
    install moved off the old bundled Nextcloud keeps it.
    """

    def test_the_nextcloud_container_and_the_config_agree(self, tmp_path):
        model = _compose_config(tmp_path)
        mount_name = model["services"]["nextcloud"]["environment"]["ISTOTA_NC_SHARED_MOUNT_NAME"]
        document, _ = _config()

        assert mount_name == nextcloud_service.BOT_MOUNT_POINT
        assert document["nextcloud"]["dav_prefix"] == mount_name

    def test_the_scripts_own_fallback_agrees_with_it(self, tmp_path):
        """`provision-nc.sh` carries a `:-` default so it still provisions if
        run outside compose, and that default is the only other copy."""
        model = _compose_config(tmp_path)
        mount_name = model["services"]["nextcloud"]["environment"][
            "ISTOTA_NC_SHARED_MOUNT_NAME"
        ]
        script = (REPO / "testbed" / "compose" / "provision-nc.sh").read_text()

        fallback = re.search(r"\$\{ISTOTA_NC_SHARED_MOUNT_NAME:-([^}]*)\}", script)

        assert fallback is not None, "the script no longer reads the compose value"
        assert fallback.group(1) == mount_name

    def test_the_istota_service_is_passed_no_dav_prefix(self, tmp_path):
        """The prefix is a config key now. An `ISTOTA_NEXTCLOUD_DAV_PREFIX`
        passed to the container would be read by nothing."""
        model = _compose_config(tmp_path)

        assert "ISTOTA_NEXTCLOUD_DAV_PREFIX" not in (
            model["services"]["istota"].get("environment") or {}
        )


def _pool(tmp_path, *, keep: bool = False) -> compose_support.StackPool:
    return compose_support.StackPool(
        workdir=tmp_path,
        shape=compose_support.Shape(
            compose_file=FULL_COMPOSE,
            image="istota-test/lean:unit",
            keep=keep,
            keep_dir=tmp_path / "keep",
        ),
    )


def _project(args: list[str]) -> str:
    return args[args.index("--project-name") + 1]


def _entries(value):
    """`extra_hosts` is a list in the file and may be a dict after merging."""
    if isinstance(value, dict):
        return [f"{key}:{item}" for key, item in value.items()]
    return list(value)


def _compose_config(tmp_path, *, extra_env: dict[str, str] | None = None) -> dict:
    """`docker compose config --format json` over the merged model.

    Compose's own parser, which is the only thing that applies the
    interpolation and schema rules a real invocation will. Local: no daemon.
    """
    import shutil

    if shutil.which("docker") is None:
        pytest.skip("the docker CLI is not installed")

    env_file = compose_support.write_env_file(tmp_path / "compose.env", _env())
    result = subprocess.run(
        [
            "docker", "compose",
            "-f", str(FULL_COMPOSE),
            "-f", str(TESTBED_OVERLAY),
            "-f", str(NEXTCLOUD_OVERLAY),
            "--project-name", "istota-testbed-unit",
            "--env-file", str(env_file),
            "config", "--format", "json",
        ],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **(extra_env or {})},
    )
    if result.returncode != 0:
        pytest.fail(
            f"`docker compose config` exited {result.returncode}\n{result.stderr}",
            pytrace=False,
        )
    return json.loads(result.stdout)


class TestTheInContainerProbes:
    """`tests/full/test_talk_poll_pressure.py` ships Python as a string.

    Its two probes are sent to `python -c` inside the istota container, so
    nothing on the host ever parses them: a defect in either is invisible to
    ruff and to the whole default suite, and surfaces only as a non-zero exit
    three minutes into a cold six-container boot, reported as "the probe itself
    failed" with a traceback from `<string>`.

    Both were written with `{{` / `}}` escaped for a `str.format` the templates
    do not use — they interpolate with `.replace` — so every brace survived
    doubled and `{{"a": 1}}` reached the interpreter as a set containing a dict.

    **A `compile()` check does not catch that, and the first version of this
    guard was written and its negative control run before that was noticed.**
    `{{"a": 1}}` is perfectly valid Python: a set literal holding a dict. It
    raises `TypeError: unhashable type: 'dict'` when the line *executes*, so
    compiling the source proves only that the braces balance. Reintroducing the
    exact production defect left the compile-only guard green — which is the
    failure mode `.claude/rules/testbed.md` describes as a probe whose success
    is indistinguishable from a no-op, arrived at here by the same route it
    describes: reading the test instead of running the control.

    So the check is on the doubled brace itself, which is unambiguous (no probe
    here has any reason to carry one) and catches it before execution, plus an
    AST sweep for set literals, since a doubled brace can only ever produce a
    set literal or a set comprehension and every `{...}` in these templates is
    meant to be a dict. `compile()` stays for the ordinary syntax error.
    """

    PROBES = ["PROBE_CURSORS", "PROBE_POLL", "PROBE_LAST_MESSAGE"]

    @staticmethod
    def _source(name: str) -> str:
        from tests.full import test_talk_poll_pressure as pressure

        return getattr(pressure, name)

    @pytest.mark.parametrize("name", PROBES)
    def test_the_probe_source_is_valid_python(self, name):
        try:
            compile(self._source(name), f"<{name}>", "exec")
        except SyntaxError as exc:
            pytest.fail(
                f"{name} is not valid Python: {exc}\n"
                f"line {exc.lineno}: {(exc.text or '').rstrip()}",
                pytrace=False,
            )

    @pytest.mark.parametrize("name", PROBES)
    def test_the_probe_carries_no_doubled_brace(self, name):
        source = self._source(name)
        for lineno, line in enumerate(source.splitlines(), 1):
            assert "{{" not in line and "}}" not in line, (
                f"{name} line {lineno} carries a doubled brace, which is the "
                f"str.format escaping these templates do not use — it reaches "
                f"the interpreter as a set literal:\n  {line.strip()}"
            )

    @pytest.mark.parametrize("name", PROBES)
    def test_the_probe_builds_no_set_literals(self, name):
        import ast

        tree = ast.parse(self._source(name))
        offenders = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, (ast.Set, ast.SetComp))
        ]
        assert offenders == [], (
            f"{name} builds a set at line(s) {offenders}; every brace in these "
            f"probes is meant to be a dict, so a set is a doubled brace that "
            f"will raise at run time inside the container"
        )

    @pytest.mark.parametrize("name", PROBES)
    def test_the_probe_resolved_its_config_path(self, name):
        """A `.replace` placeholder that is never substituted fails the same way.

        The templates carry `{config}` and swap it for `CONTAINER_CONFIG`; a
        rename on one side leaves the literal in the source, which compiles fine
        and then opens a file called `{config}`.
        """
        from tests.full import test_talk_poll_pressure as pressure

        source = self._source(name)
        assert "{config}" not in source, (
            f"{name} still carries an unsubstituted {{config}} placeholder"
        )
        assert pressure.CONTAINER_CONFIG in source, (
            f"{name} does not name {pressure.CONTAINER_CONFIG}"
        )
