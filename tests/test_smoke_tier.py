"""The smoke tier's own wiring, held down without bringing a stack up.

`tests/smoke/` needs Docker and is deselected by default, so everything it
depends on that *can* be checked cheaply is checked here instead — the same
split, and for the same reason, as `tests/test_image_tier.py` one tier below.

Two tests in `TestTheComposeFileIsAddressable` do shell out to
`docker compose config`, which is compose's own parser and the only thing that
applies the interpolation and schema rules a real invocation will. It parses
locally and needs no daemon, so it is fast and always runs. Nothing here builds
an image or starts a container.

The parts worth guarding are the ones whose failure mode is silence: a marker
that stops deselecting (so `uv run pytest` starts building images), a
`wait_ready` that never returns for a service with no health check, a compose
`ps` parser that reads every state as "not started yet", and a probe that builds
a WHERE clause ignoring its filters and therefore matches every row.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from testbed import stack as compose_support
from testbed.probe import Probe

REPO = Path(__file__).resolve().parents[1]
PREBUILT_OVERLAY = REPO / "docker" / "docker-compose.test.prebuilt.yml"
COMPOSE_FILE = REPO / "docker" / "docker-compose.test.yml"

# Deliberately not `os.environ`: these checks are about what rides in the
# argument list, and an inherited environment would satisfy them either way.
_MINIMAL_ENV = {"PATH": "/usr/local/bin:/usr/bin:/bin"}


def _require_compose_cli() -> None:
    """Skip only when the compose CLI is genuinely absent.

    Not `docker_available()`, which runs `docker info` — a round trip of up to
    15 seconds that gates on a *daemon*. `docker compose config` is a local
    parse: verified against an unreachable `DOCKER_HOST`, it still resolves and
    exits 0. Gating on the daemon would make these two tests both slower and
    stricter than what they actually need.
    """
    import shutil

    if shutil.which("docker") is None:
        pytest.skip("the docker CLI is not installed")


class TestTheMarkerIsWired:
    def test_the_smoke_marker_is_registered_and_deselected_by_default(self):
        body = (REPO / "pyproject.toml").read_text()

        assert '"smoke:' in body, "the smoke marker is not registered"
        assert "not smoke" in body, (
            "smoke is not in the default deselection, so `uv run pytest` would "
            "try to build and start a compose stack"
        )

    def test_the_default_run_collects_nothing_from_the_smoke_directory(self):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-p",
                "no:cacheprovider",
                "--collect-only",
                "-q",
                "tests/smoke/",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=300,
        )

        assert result.returncode == 5, f"{result.stdout}\n{result.stderr}"
        assert "deselected" in result.stdout, result.stdout

    def test_the_linux_driver_deselects_smoke_too(self):
        """Parity with the tier below, and for the same reason.

        `scripts/test-linux.sh` runs the suite with its own `-m`, which
        *replaces* addopts rather than composing with it. A marker deselected in
        one and not the other runs inside the Linux runner and nowhere else —
        so a stack build would fire in a container with no Docker socket.
        """
        driver = (REPO / "scripts" / "test-linux.sh").read_text()

        assert "not smoke" in driver, (
            "scripts/test-linux.sh does not deselect the smoke marker"
        )

    def test_every_smoke_test_carries_the_marker(self):
        # A file added without `pytestmark` would run in the default suite and
        # hang on a Docker build. The marker is applied at module level, so this
        # is a check that the module-level line is present in each file.
        files = sorted((REPO / "tests" / "smoke").glob("test_*.py"))
        assert files, "no smoke tests found; this guard would pass vacuously"
        for path in files:
            assert "pytestmark = pytest.mark.smoke" in path.read_text(), path


class TestTheXdistGuard:
    """Two halves, because neither covers the other.

    The collection hook catches the `--collect-only` and `--dist` spellings
    before anything is built; `_require_no_xdist` catches a real parallel run,
    which the hook structurally cannot see.
    """

    def test_the_collection_hook_rejects_the_collect_only_spelling(self):
        result = _collect(["-m", "smoke", "-n", "2"])

        assert result.returncode == 4, (
            f"expected a usage error, got {result.returncode}\n"
            f"{result.stdout}\n{result.stderr}"
        )
        assert "must run with -n0" in result.stdout + result.stderr

    def test_the_guard_does_not_fire_on_the_default_run(self):
        # The direction that would break every ordinary `uv run pytest`: the
        # hook is `trylast` so `-m` deselection has already emptied the smoke
        # items by the time it runs.
        result = _collect([])

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"

    def test_a_real_xdist_run_is_refused_before_anything_is_built(self):
        """The scenario the collection hook structurally cannot see.

        Under a real `-n 2` the controller never calls
        `pytest_collection_modifyitems` — it holds no items — and xdist clears
        `numprocesses` in the workers so they do not re-fan-out. Every reading
        available to that hook says "not parallel". The refusal happens at
        fixture setup, before `require_docker()` and before any build, so this
        test needs no Docker daemon and costs a fraction of a second.
        """
        result = subprocess.run(
            [
                sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
                "-q", "--no-header", "-m", "smoke", "-n", "2",
                "tests/smoke/test_lean_stack.py::TestTheStackAnswersATask",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            timeout=300,
        )
        output = result.stdout + result.stderr

        assert result.returncode != 0, f"a real xdist run was not refused\n{output}"
        assert "must run with -n0" in output, output
        assert "xdist worker" in output, output


def _collect(args: list[str]) -> subprocess.CompletedProcess:
    """A nested `--collect-only` pytest, from the repo root.

    `-p no:cacheprovider` because the cacheprovider writes nodeids during
    collection, and these run concurrently with an outer `-n auto` session
    writing the same file.
    """
    return subprocess.run(
        [
            sys.executable, "-m", "pytest", "-p", "no:cacheprovider",
            "--collect-only", "-q", *args,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=300,
    )


class TestTheComposeFileIsAddressable:
    """`docker compose config` is the parser, not a YAML load — it applies the
    interpolation and schema rules the real invocation will."""

    def _config(self, args: list[str], *, env: dict) -> subprocess.CompletedProcess:
        return subprocess.run(
            args + ["config", "--services"],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
        )

    def test_the_compose_file_is_valid_and_names_one_service(self, tmp_path):
        _require_compose_cli()
        env_file = tmp_path / "compose.env"
        env_file.write_text(f"ISTOTA_TEST_CONFIG_DIR={tmp_path}\n")
        args = compose_support.compose_args(
            COMPOSE_FILE, project="cfg-check", env_file=env_file
        )

        result = self._config(args, env=_MINIMAL_ENV)

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert result.stdout.split() == ["istota"], result.stdout

    def test_the_env_file_alone_satisfies_interpolation(self, tmp_path):
        """The bug this tier actually shipped once, pinned.

        Compose interpolates the file on *every* subcommand, not just `up`. The
        first harness exported `ISTOTA_TEST_CONFIG_DIR` into the environment of
        `up` only, so `ps`, `exec`, `logs` and `down` all failed before touching
        a container: `wait_ready` read "no container yet" until it timed out,
        and `down` — which deliberately swallows failures — left the stack and
        its named volume behind. Two leaked stacks before it was noticed.

        Running `config` with a *minimal* environment is what makes this
        non-vacuous. With the ambient environment inherited it would pass
        whether or not the variable rides in the argument list, which is the
        exact distinction that broke.
        """
        _require_compose_cli()
        env_file = tmp_path / "compose.env"
        env_file.write_text(f"ISTOTA_TEST_CONFIG_DIR={tmp_path}\n")

        from_args = compose_support.compose_args(
            COMPOSE_FILE, project="p", env_file=env_file
        )
        without = compose_support.compose_args(COMPOSE_FILE, project="p")

        assert self._config(from_args, env=_MINIMAL_ENV).returncode == 0
        # And the control: no env file, no ambient variable, so it must fail.
        # Without this half the assertion above proves nothing about where the
        # value came from.
        assert self._config(without, env=_MINIMAL_ENV).returncode != 0

    def test_the_config_dir_is_required_rather_than_defaulted(self):
        # `:?` not `:-`. A default would silently mount some other directory and
        # the daemon would boot with no config, which surfaces as a health-check
        # timeout rather than as "the harness forgot to supply this".
        body = COMPOSE_FILE.read_text()

        assert "${ISTOTA_TEST_CONFIG_DIR:?" in body, body[:400]


class TestThePrebuiltOverlay:
    """The overlay that points the stack at an image somebody else built.

    Its only caller is the negative control, and the control is the one thing
    proving the smoke tier can see a broken deployment — so an overlay that
    silently failed to apply would disarm the tier's own falsification while
    every test still passed.
    """

    def test_the_merged_model_runs_the_named_image_and_builds_nothing(self, tmp_path):
        """`build: !reset null` is the whole point, so it is what is asserted.

        A service carrying both `build` and `image` is one compose rebuilds,
        tagging the result over the name we asked it to run — the control would
        then test the *correct* image and pass. `config` renders the merged
        model, which is the only place that outcome is visible before a
        container exists.
        """
        _require_compose_cli()
        env_file = tmp_path / "compose.env"
        env_file.write_text(
            f"ISTOTA_TEST_CONFIG_DIR={tmp_path}\n"
            "ISTOTA_TEST_IMAGE=istota-test/no-forge:pinned\n"
        )
        args = compose_support.compose_args(
            COMPOSE_FILE, project="p", env_file=env_file, overlays=[PREBUILT_OVERLAY]
        )

        result = subprocess.run(
            args + ["config"], capture_output=True, text=True, timeout=60,
            env=_MINIMAL_ENV,
        )

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "istota-test/no-forge:pinned" in result.stdout, result.stdout
        assert "build:" not in result.stdout, (
            "the overlay left a build section in the merged model, so compose "
            "would rebuild and retag over the image the control asked for\n"
            + result.stdout
        )

    def test_the_image_is_required_rather_than_defaulted(self, tmp_path):
        """`:?` not `:-`. An empty default renders a service with no image at
        all, and the failure arrives as a compose error about a malformed
        service rather than as "the harness forgot to say which image"."""
        _require_compose_cli()
        env_file = tmp_path / "compose.env"
        env_file.write_text(f"ISTOTA_TEST_CONFIG_DIR={tmp_path}\n")
        args = compose_support.compose_args(
            COMPOSE_FILE, project="p", env_file=env_file, overlays=[PREBUILT_OVERLAY]
        )

        result = subprocess.run(
            args + ["config"], capture_output=True, text=True, timeout=60,
            env=_MINIMAL_ENV,
        )

        assert result.returncode != 0

    def test_the_base_file_still_builds_when_no_overlay_is_applied(self, tmp_path):
        """The control for the control. Without this, the assertion above
        would pass on a base file that had stopped declaring a build at all."""
        _require_compose_cli()
        env_file = tmp_path / "compose.env"
        env_file.write_text(f"ISTOTA_TEST_CONFIG_DIR={tmp_path}\n")
        args = compose_support.compose_args(
            COMPOSE_FILE, project="p", env_file=env_file
        )

        result = subprocess.run(
            args + ["config"], capture_output=True, text=True, timeout=60,
            env=_MINIMAL_ENV,
        )

        assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
        assert "build:" in result.stdout, result.stdout


class TestTheSeccompGrantStaysInTheTestFile:
    """The lean stack loosens seccomp so bwrap can build a namespace.

    That is a test-harness concession — the supported production shape is bare
    metal via Ansible, where the syscall is not blocked — and the comment in
    the test compose file says so. Nothing enforced it, so a copy across while
    debugging would ship a container-escape-adjacent grant to every operator
    running the Docker stack, and no test would notice.

    Precedent for the shape: `TestVendoredCopy` and
    `tests/test_private_data_scan.py` both guard a property whose violation is
    otherwise silent.
    """

    @pytest.mark.parametrize(
        "setting", ["seccomp", "privileged", "cap_add", "apparmor"]
    )
    def test_the_production_compose_grants_no_extra_privilege(self, setting):
        lines = (REPO / "docker" / "docker-compose.yml").read_text().splitlines()
        offenders = []
        for number, line in enumerate(lines, 1):
            if setting not in line or line.strip().startswith("#"):
                continue
            # The key and the list under it, because a YAML sequence puts the
            # value on the *following* lines — `cap_add:` alone says nothing
            # about what is being added, and a line-at-a-time check reads the
            # devbox's deliberate NET_RAW as an unexplained grant.
            block = " ".join(part.strip() for part in lines[number - 1 : number + 3])
            offenders.append(f"{number}: {block}")

        # `cap_add: NET_RAW` on the devbox is pre-existing and deliberate — it
        # is what lets that container run ping and traceroute, and it is not a
        # sandbox grant. Excluded by name rather than by dropping the setting
        # from the sweep, so a *second* cap_add anywhere still fails this.
        offenders = [line for line in offenders if "NET_RAW" not in line]

        assert not offenders, (
            f"docker/docker-compose.yml grants {setting!r}: {offenders}. The "
            "seccomp relaxation belongs to docker-compose.test.yml alone — see "
            "the comment there."
        )

    def test_the_test_compose_still_carries_it(self):
        """The control. Without this the assertion above would pass just as
        well on a test file that had lost the grant, at which point the whole
        smoke tier fails on bwrap and nothing explains why."""
        body = (REPO / "docker" / "docker-compose.test.yml").read_text()

        assert "seccomp:unconfined" in body, (
            "the lean stack no longer relaxes seccomp; bwrap cannot create a "
            "user namespace under Docker's default profile, so every task "
            "running a Bash tool call will fail"
        )


class TestTheStackStartsTheWayTheDeploymentDoes:
    def test_the_schema_is_created_before_the_scheduler_runs(self):
        """`init` is not optional, and nothing else does it.

        `db.init_db` is reached from the `init` subcommand alone — the scheduler
        opens the DB without creating a schema. The shipped entrypoint runs
        `istota … init` before exec'ing the scheduler; a lean stack that skips
        it comes up and then logs "no such table: tasks" on every tick, forever.
        Measured, before this was fixed.
        """
        body = COMPOSE_FILE.read_text()

        assert "init &&" in body, "the lean stack does not run `istota init`"
        assert "istota-scheduler" in body

    def test_the_health_check_asks_for_the_schema_not_the_file(self):
        """A file check is satisfied before `init` has run.

        Anything that opens the DB creates the file, so `test -f` reports
        healthy on a daemon that cannot dispatch. The health check has to name
        a table.
        """
        body = COMPOSE_FILE.read_text()

        assert "test -f /data/db/istota.db" not in body, (
            "the health check is back to a bare file test, which passes on a "
            "container with no schema"
        )
        assert "sqlite_master" in body and "'tasks'" in body, body


class TestComposeArgs:
    def test_the_project_name_is_always_present(self):
        args = compose_support.compose_args(COMPOSE_FILE, project="p")

        assert "--project-name" in args and "p" in args
        assert str(COMPOSE_FILE) in args

    def test_overlays_follow_the_base_file_in_order(self):
        """Compose merges `-f` files left to right, so the order is the meaning.

        An overlay placed *before* the base would be overridden by it rather
        than overriding it — the same file list, the opposite result, and no
        error either way.
        """
        args = compose_support.compose_args(
            COMPOSE_FILE,
            project="p",
            overlays=[Path("/tmp/first.yml"), Path("/tmp/second.yml")],
        )
        files = [args[i + 1] for i, token in enumerate(args) if token == "-f"]

        assert files == [str(COMPOSE_FILE), "/tmp/first.yml", "/tmp/second.yml"]

    def test_no_overlays_leaves_the_argument_list_as_it_was(self):
        """The default path, so the parameter cannot change existing callers."""
        assert compose_support.compose_args(
            COMPOSE_FILE, project="p"
        ) == compose_support.compose_args(COMPOSE_FILE, project="p", overlays=[])

    def test_the_env_file_is_omitted_when_absent(self):
        # Passing `--env-file` with an empty value makes compose fail with a
        # confusing "no such file" rather than falling back to no env file.
        assert "--env-file" not in compose_support.compose_args(
            COMPOSE_FILE, project="p"
        )
        assert "--env-file" in compose_support.compose_args(
            COMPOSE_FILE, project="p", env_file=Path("/tmp/x.env")
        )


class TestServiceStateParsing:
    """`compose ps --format json` changed shape between compose versions."""

    def _with_ps_output(self, monkeypatch, stdout: str, returncode: int = 0):
        # Patched on the module under test, not on the stdlib `subprocess`
        # module object — the latter replaces `subprocess.run` process-wide for
        # the duration, which is shared mutable state in an `-n auto` suite.
        def fake_run(args, **kwargs):
            if returncode != 0:
                raise compose_support.ComposeError("ps failed")
            return stdout

        monkeypatch.setattr(compose_support, "_run", fake_run)

    def test_a_json_array_is_understood(self, monkeypatch):
        self._with_ps_output(
            monkeypatch,
            json.dumps([{"Service": "istota", "State": "running", "Health": "healthy"}]),
        )

        assert compose_support._service_state([], "istota") == ("running", "healthy")

    def test_one_object_per_line_is_understood(self, monkeypatch):
        """Genuine NDJSON — two objects, one per line.

        The first version of this test fed a *single* JSON object, which
        `json.loads` parses successfully, so it returned through the array
        branch and the newline-delimited fallback it is named for never ran.
        Two objects separated by a newline are not valid JSON as a whole, which
        is what forces the fallback.
        """
        self._with_ps_output(
            monkeypatch,
            json.dumps({"Service": "other", "State": "exited", "Health": ""})
            + "\n"
            + json.dumps({"Service": "istota", "State": "running", "Health": ""}),
        )

        # And the requested service is selected, not merely the first row.
        assert compose_support._service_state([], "istota") == ("running", "")

    def test_an_exited_container_is_reported_as_exited(self, monkeypatch):
        # `wait_ready`'s fast-fail keys on this exact string. It only ever
        # arrives because `ps` is invoked with `--all`; without that flag
        # compose omits stopped containers entirely.
        self._with_ps_output(
            monkeypatch,
            json.dumps([{"Service": "istota", "State": "exited", "Health": ""}]),
        )

        assert compose_support._service_state([], "istota") == ("exited", "")

    def test_ps_is_invoked_with_all_so_stopped_containers_are_visible(self, monkeypatch):
        """The flag whose absence made the dead-container path unreachable.

        Without `--all`, a service that crashed at boot reads as absent rather
        than as `exited`, so `wait_ready` cannot fast-fail and instead spends
        its whole 120s timeout before reporting `state='' health=''` — the least
        informative possible description of "it exited immediately".
        """
        seen = {}

        def fake_run(args, **kwargs):
            seen["args"] = args
            return "[]"

        monkeypatch.setattr(compose_support, "_run", fake_run)
        compose_support._service_state(["docker", "compose"], "istota")

        assert "--all" in seen["args"], seen["args"]

    def test_a_failing_ps_reads_as_empty_rather_than_raising(self, monkeypatch):
        # The branch that turned the env-file bug into a silent timeout: a
        # compose command that fails during interpolation is indistinguishable
        # here from "no container yet". Deliberate — the polling loop must not
        # die on one bad `ps` — but it must not raise either.
        self._with_ps_output(monkeypatch, "", returncode=1)

        assert compose_support._service_state([], "istota") == ("", "")

    def test_unparseable_output_reads_as_empty_rather_than_raising(self, monkeypatch):
        # A deprecation notice on stdout is neither JSON nor JSON-lines. A
        # decode error escaping into `wait_ready`'s polling loop would replace
        # the timeout-with-logs this module works to produce.
        self._with_ps_output(monkeypatch, "WARNING: something on stdout")

        assert compose_support._service_state([], "istota") == ("", "")

    def test_no_container_reads_as_empty_not_as_a_crash(self, monkeypatch):
        self._with_ps_output(monkeypatch, "")

        assert compose_support._service_state([], "istota") == ("", "")


class TestTheChildEnvironmentIsExtendedNotReplaced:
    def test_a_platform_override_keeps_path(self):
        """The bug that made `up(platform=…)` unable to find docker at all.

        `subprocess.run(env={...})` substitutes rather than extends, so building
        the child environment from the override alone leaves no `PATH` — and the
        failure is `FileNotFoundError: docker`, which reads as "Docker is not
        installed" rather than as a harness bug. `HOME` matters too: it is where
        the Docker CLI finds its context and therefore the daemon socket.
        """
        child = compose_support._child_env({"DOCKER_DEFAULT_PLATFORM": "linux/amd64"})

        assert child["DOCKER_DEFAULT_PLATFORM"] == "linux/amd64"
        assert child.get("PATH"), "the override replaced the environment"
        assert child.get("PATH") == os.environ.get("PATH")

    def test_no_override_still_yields_the_real_environment(self):
        assert compose_support._child_env(None).get("PATH") == os.environ.get("PATH")

    def test_up_passes_the_platform_without_dropping_the_environment(self, monkeypatch):
        captured = {}

        def fake_run(args, *, timeout, env=None):
            captured["env"] = compose_support._child_env(env)
            return ""

        monkeypatch.setattr(compose_support, "_run", fake_run)
        compose_support.up(["docker", "compose"], platform="linux/amd64")

        assert captured["env"]["DOCKER_DEFAULT_PLATFORM"] == "linux/amd64"
        assert captured["env"].get("PATH"), "up() would not find docker"


class TestComposeErrorNamesTheSubcommand:
    def test_the_header_says_which_call_failed(self):
        # `args[:4]` is always `docker compose -f <file>`, so every error read
        # identically and none of them said what had actually been run.
        described = compose_support._describe(
            ["docker", "compose", "-f", "/x.yml", "--project-name", "p", "ps", "--all"]
        )

        assert described == "docker compose ps --all", described


class TestWaitReady:
    def test_healthy_is_ready(self, monkeypatch):
        # The only path the lean stack actually takes — its service declares a
        # healthcheck, so the `running`-with-no-health arm never applies to it.
        # Left untested, a change to the literal (or compose renaming the field)
        # would surface only as a 120s timeout in the Docker tier.
        monkeypatch.setattr(
            compose_support,
            "_service_state",
            lambda args, service, env=None: ("running", "healthy"),
        )

        compose_support.wait_ready([], "istota", timeout=5)

    def test_running_without_a_health_check_is_ready(self, monkeypatch):
        """The case that would otherwise hang for the full timeout.

        A service declaring no `healthcheck` never reports a health status, so
        waiting for "healthy" waits forever on a stack that came up correctly.
        """
        monkeypatch.setattr(
            compose_support, "_service_state", lambda args, service, env=None: ("running", "")
        )

        compose_support.wait_ready([], "istota", timeout=5)

    def test_running_but_unhealthy_is_not_ready(self, monkeypatch):
        # The inverse, and the reason the two cases cannot be collapsed into
        # "state == running": a container with a health check that is still
        # starting is `running` and not yet usable.
        monkeypatch.setattr(
            compose_support, "_service_state", lambda args, service, env=None: ("running", "starting")
        )
        monkeypatch.setattr(compose_support, "logs", lambda *a, **k: "(logs)")

        with pytest.raises(TimeoutError):
            compose_support.wait_ready([], "istota", timeout=1)

    def test_the_timeout_message_carries_the_service_logs(self, monkeypatch):
        """A bare timeout says nothing about why the service did not start.

        By the time the caller could look, teardown has removed the container,
        so the logs have to be captured into the exception at the moment it is
        raised.
        """
        monkeypatch.setattr(
            compose_support, "_service_state", lambda args, service, env=None: ("exited", "")
        )
        monkeypatch.setattr(
            compose_support, "logs", lambda *a, **k: "Traceback: config is malformed"
        )

        with pytest.raises(TimeoutError, match="config is malformed"):
            compose_support.wait_ready([], "istota", timeout=5)

    def test_an_exited_service_fails_fast_rather_than_waiting_out_the_timeout(
        self, monkeypatch
    ):
        import time

        monkeypatch.setattr(
            compose_support, "_service_state", lambda args, service, env=None: ("exited", "")
        )
        monkeypatch.setattr(compose_support, "logs", lambda *a, **k: "")

        started = time.monotonic()
        with pytest.raises(TimeoutError):
            compose_support.wait_ready([], "istota", timeout=30)

        assert time.monotonic() - started < 5, "waited out the timeout on a dead service"


@pytest.fixture
def framework_db(tmp_path) -> Path:
    """A real SQLite file with the real schema, for the local probe path."""
    path = tmp_path / "istota.db"
    connection = sqlite3.connect(path)
    connection.executescript((REPO / "schema.sql").read_text())
    connection.executemany(
        "INSERT INTO tasks (source_type, user_id, prompt, status) VALUES (?, ?, ?, ?)",
        [
            ("cli", "alice", "first", "completed"),
            ("cli", "bob", "second", "pending"),
            ("talk", "alice", "third", "failed"),
        ],
    )
    connection.commit()
    connection.close()
    return path


class TestProbe:
    def test_it_refuses_to_be_built_with_neither_access_mode(self):
        # Silently defaulting to one of them would produce a probe that queries
        # the wrong database and reports "no rows" rather than a setup error.
        with pytest.raises(ValueError):
            Probe()

    def test_filters_are_applied_and_not_ignored(self, framework_db):
        probe = Probe(local=framework_db)

        assert len(probe.tasks()) == 3
        assert [t["prompt"] for t in probe.tasks(user_id="alice")] == ["first", "third"]
        assert [t["prompt"] for t in probe.tasks(status="pending")] == ["second"]
        assert [t["prompt"] for t in probe.tasks(source_type="talk")] == ["third"]

    def test_a_task_id_narrows_to_exactly_one_row(self, framework_db):
        """The filter the smoke tier actually needs.

        `user_id` alone is not selective enough against a running daemon: the
        scheduler queues its own work for the same user at startup — a feeds
        poll, a sleep cycle — so a wait filtered on the user returns whichever
        task finished first. The smoke tests came back asserting against a
        `source_type='scheduled'` row before this existed.
        """
        probe = Probe(local=framework_db)

        assert [t["prompt"] for t in probe.tasks(task_id=2)] == ["second"]

    def test_wait_for_task_honours_a_task_id(self, framework_db):
        # Task 1 is completed and task 2 is pending. Without the id filter the
        # wait would return task 1 immediately; with it, task 2 must time out.
        probe = Probe(local=framework_db)

        assert probe.wait_for_task(status="completed", task_id=1, timeout=5)["id"] == 1
        with pytest.raises(TimeoutError):
            probe.wait_for_task(status="completed", task_id=2, timeout=1)

    def test_filters_combine_rather_than_replace_each_other(self, framework_db):
        probe = Probe(local=framework_db)

        assert [
            t["prompt"] for t in probe.tasks(user_id="alice", source_type="cli")
        ] == ["first"]

    def test_the_filter_value_is_a_parameter_not_interpolated(self, framework_db):
        # A quote in a filter value would end the string and change the query.
        # Nothing here is attacker-controlled, but a probe that broke on a
        # value like that would break confusingly and far from the cause.
        probe = Probe(local=framework_db)

        assert probe.tasks(user_id="o'brien") == []

    def test_wait_for_task_returns_a_task_that_already_reached_the_status(
        self, framework_db
    ):
        probe = Probe(local=framework_db)

        task = probe.wait_for_task(status="completed", user_id="alice", timeout=5)

        assert task["prompt"] == "first"

    def test_wait_for_task_returns_a_failure_instead_of_waiting_it_out(
        self, framework_db
    ):
        """Waiting for `completed` on a task that already failed.

        Spending the whole timeout and then reporting "nothing reached
        completed" throws away the one thing worth knowing — that it failed, and
        with what error. The terminal row comes back and the caller's own
        assertion on `status` is what fails.
        """
        probe = Probe(local=framework_db)

        task = probe.wait_for_task(status="completed", user_id="alice", source_type="talk")

        assert task["status"] == "failed"

    def test_a_suspended_task_counts_as_terminal(self, framework_db):
        """`pending_confirmation` parks a task waiting for a human.

        It will not move on its own, so treating it as non-terminal makes the
        wait burn its whole timeout and then report "nothing reached completed"
        — the exact failure mode the terminal set exists to prevent. The status
        list is in AGENTS.md under "Task Status".
        """
        connection = sqlite3.connect(framework_db)
        connection.execute(
            "INSERT INTO tasks (source_type, user_id, prompt, status) VALUES (?,?,?,?)",
            ("cli", "carol", "awaiting a human", "pending_confirmation"),
        )
        connection.commit()
        connection.close()

        task = Probe(local=framework_db).wait_for_task(
            status="completed", user_id="carol", timeout=5
        )

        assert task["status"] == "pending_confirmation"

    def test_wait_for_task_times_out_when_nothing_is_terminal(self, framework_db):
        probe = Probe(local=framework_db)

        with pytest.raises(TimeoutError, match="pending"):
            probe.wait_for_task(status="completed", user_id="bob", timeout=1)

    def test_task_logs_are_scoped_to_one_task(self, framework_db):
        connection = sqlite3.connect(framework_db)
        connection.executemany(
            "INSERT INTO task_logs (task_id, level, message) VALUES (?, ?, ?)",
            [(1, "info", "for one"), (2, "info", "for two")],
        )
        connection.commit()
        connection.close()

        assert [row["message"] for row in Probe(local=framework_db).task_logs(1)] == [
            "for one"
        ]

    def test_the_local_reader_opens_the_database_read_only(self, framework_db):
        """The daemon is writing this file while the probe reads it.

        A probe that took a write lock could stall the thing it is observing,
        and the resulting failure would appear somewhere else entirely.
        """
        probe = Probe(local=framework_db)

        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            probe.query("DELETE FROM tasks")
