"""The lean stack, brought up around one test.

Everything here exists to get from "a checkout" to "a running daemon that will
answer a task" in under thirty seconds, with no Nextcloud and no API key. Three
pieces make that possible, and each replaces something the full stack does
slowly:

- the config is rendered **on the host** by the same `render-config.sh` the
  image ships, so the container never enters the provisioning branch and its
  120-second Nextcloud polling loop;
- the model is a scripted HTTP endpoint in the pytest process, reached through
  `base_url`, so no credential and no network are involved;
- the stack is one service.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import uuid
from pathlib import Path

import pytest

from ..support import compose as compose_support
from ..support.model_endpoint import serve_script
from ..support.probe import Probe
from . import fake_gitlab

# Imported rather than re-derived. `--platform amd64` is a rootdir-level option
# (tests/conftest.py) that both Docker tiers honour, and the normalization —
# a bare `amd64` becoming `linux/amd64` — is the part that is easy to get
# subtly wrong. A second copy here would drift, and the symptom of drift is a
# native build wearing an amd64 label.
from ..image import conftest as image_support
from ..image.conftest import resolve_platform

REPO = Path(__file__).resolve().parents[2]
COMPOSE_FILE = REPO / "docker" / "docker-compose.test.yml"
RENDER_CONFIG = REPO / "docker" / "istota" / "render-config.sh"

READY_TIMEOUT = 120

_XDIST_MESSAGE = (
    "the smoke tier must run with -n0. Each test builds and tears down a whole "
    "compose stack, so N workers would race the same daemon, exhaust it, and "
    "sweep each other's projects."
)

# Every project this tier creates starts with it, which is what makes the
# session-start sweep able to find leftovers without touching anything else.
PROJECT_PREFIX = "istota-smoke-"


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Fail early when the tier is selected under xdist.

    Ported from `tests/image/conftest.py`, and load-bearing for the same narrow
    reason: this hook is the only place that can see the `--collect-only` and
    `--dist` spellings, and it turns them into an error before anything is
    built. `trylast` matters because this hook is also where `-m` deselection
    happens — without it the unfiltered item list is what arrives, and an
    ordinary `uv run pytest` fails with a usage error about a tier it had
    already deselected.

    **It cannot see a real parallel run**, which is the actual scenario. Under
    `-n 2` the controller never calls this (it holds no items) and xdist clears
    `numprocesses` in the workers so they do not re-fan-out. `_require_no_xdist`
    is the check that binds.
    """
    if not any(item.get_closest_marker("smoke") for item in items):
        return

    workers = getattr(config.option, "numprocesses", None)
    distribution = config.getoption("dist", "no")
    if workers or distribution not in ("no", None):
        raise pytest.UsageError(
            f"{_XDIST_MESSAGE} (saw -n {workers}, --dist {distribution})"
        )


def _require_no_xdist(config) -> None:
    """Refuse inside an xdist worker.

    `workerinput` is set by xdist on the worker's config and absent in a
    single-process run — the only signal that survives into the place where the
    damage would be done.
    """
    if hasattr(config, "workerinput"):
        worker = config.workerinput.get("workerid", "?")
        pytest.fail(f"{_XDIST_MESSAGE} (running in xdist worker {worker})", pytrace=False)


def require_docker() -> None:
    if not compose_support.docker_available():
        pytest.skip("no Docker daemon available")


@pytest.fixture(scope="session", autouse=True)
def _sweep_leftover_stacks():
    """Reclaim stacks an earlier run was killed before tearing down.

    A unique project name per test stops one run from adopting another's
    containers mid-flight, but it also means nothing ever reclaims them: a
    killed session leaves a container and a named volume behind for good. One
    sweep at session start closes that, and it is scoped by the prefix so it can
    never touch a developer's own stack.
    """
    if compose_support.docker_available():
        compose_support.sweep_projects(PROJECT_PREFIX)
    yield


def _render_config(
    destination: Path, base_url: str, extra: dict[str, str] | None = None
) -> Path:
    """Run the shipped render script on the host.

    This is the property that makes the shortcut legitimate: the file the lean
    stack boots from is produced by the same script the container would have
    run, not by a fixture that approximates it.

    `extra` adds `ISTOTA_*` variables the script reads — how the forge stack
    turns the `[developer]` block on. It is merged over the base rather than
    replacing it, and it goes through the same script, so a block that the
    render script would not have produced cannot be smuggled in here.
    """
    config_file = destination / "config.toml"
    # An explicit environment, NOT `**os.environ`. render-config.sh reads
    # dozens of `ISTOTA_*` variables, so inheriting the developer's shell would
    # make the config the lean stack boots from depend on whatever happens to be
    # exported in the terminal that started pytest — the same run passing on one
    # machine and failing on another, with nothing in the repo to explain it.
    #
    # This is reproducibility, not test isolation: it does *not* stop the daemon
    # queueing work of its own. The scheduler seeds `_module.feeds.run_scheduled`
    # and polls it at startup regardless of this environment, so the smoke tests
    # filter on the submitted task's id rather than on its user.
    environment = {
        "PATH": os.environ.get("PATH", "/usr/local/bin:/usr/bin:/bin"),
        "CONFIG_FILE": str(config_file),
        "USER_NAME": "testuser",
        "NC_URL": "http://nextcloud",
        "APP_PASSWORD": "app-password-value",
        "BOT_USER": "istota",
        "USER_TIMEZONE": "UTC",
        "ISTOTA_BRAIN_KIND": "native",
        "ISTOTA_BRAIN_NATIVE_BASE_URL": base_url,
        "ISTOTA_BRAIN_NATIVE_MODEL": "scripted-test-model",
        # One turn is all the scripted endpoint has; a loop that asked for more
        # should fail loudly rather than grind through a hundred attempts.
        "ISTOTA_BRAIN_NATIVE_MAX_TURNS": "4",
    }
    environment.update(extra or {})
    result = subprocess.run(
        ["bash", str(RENDER_CONFIG)],
        capture_output=True,
        text=True,
        timeout=60,
        env=environment,
    )
    if result.returncode != 0:
        pytest.fail(
            f"render-config.sh exited {result.returncode}\n"
            f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}",
            pytrace=False,
        )
    assert config_file.exists(), "render-config.sh reported success but wrote nothing"
    return config_file


@pytest.fixture
def lean_stack(pytestconfig, tmp_path, request):
    """A running daemon and the endpoint it talks to.

    Function-scoped on purpose. The scripted turns differ per test, and the
    endpoint's `base_url` is baked into the rendered config, so a shared stack
    would have to be reconfigured and restarted between tests anyway — at which
    point the sharing saves nothing and couples the tests to each other's
    scripts.
    """
    _require_no_xdist(pytestconfig)
    require_docker()

    turns = getattr(request, "param", None) or [{"text": "the scripted answer"}]
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    # All interfaces, explicitly. The default is loopback so an ordinary
    # `uv run pytest` never opens a listener beyond it; this tier is the one
    # caller that genuinely needs the container to reach back in.
    endpoint = serve_script(turns, host="0.0.0.0")
    # A fresh project name per test, so a stack left behind by an interrupted
    # run is never adopted (and then torn down) by the next one. The session
    # sweep above is what reclaims those leftovers.
    project = f"{PROJECT_PREFIX}{uuid.uuid4().hex[:8]}"

    # The config directory travels in an --env-file, not in our environment.
    # Compose interpolates the compose file on *every* subcommand, so a
    # `${VAR:?}` supplied only to `up` makes `ps`, `exec`, `logs` and `down`
    # fail before they touch a container. `down` swallows its failures, so the
    # visible symptom was a stack that survived the run holding a named volume,
    # while `wait_ready` sat out its whole timeout reading "no container yet".
    # An --env-file rides along in the argument list, so every subcommand gets
    # it and no caller has to remember.
    env_file = tmp_path / "compose.env"
    env_file.write_text(f"ISTOTA_TEST_CONFIG_DIR={config_dir}\n")
    args = compose_support.compose_args(
        COMPOSE_FILE, project=project, env_file=env_file
    )

    # Inside the `try` from here on. `_render_config` calls `pytest.fail` on a
    # non-zero render, and an endpoint started outside it would leak a bound
    # port and a live thread for the rest of the session on every failed setup.
    try:
        _render_config(config_dir, endpoint.container_base_url)
        compose_support.up(args, platform=resolve_platform(pytestconfig))
        compose_support.wait_ready(args, "istota", timeout=READY_TIMEOUT)
        yield LeanStack(args=args, endpoint=endpoint, config_dir=config_dir)
    finally:
        # Volumes too: the DB is a named volume, and leaving it behind would
        # make the next run's assertions depend on this one's rows.
        compose_support.down(args, volumes=True)
        endpoint.close()


# The token the forge stack configures. Fabricated, and deliberately not
# wearing a real forge prefix: the pre-commit scanner objects to `glpat-` on
# exactly the reasoning that a fake value with a real prefix is
# indistinguishable from a leak to anything reading the diff. Its *length* is
# what the assertions use, via `ForgeCall.auth`.
FORGE_TOKEN = "forge-token-for-the-smoke-tier"

# The project the stub seeds and the scenarios work against.
FORGE_PROJECT = "istota-test/smoke-project"


NO_FORGE_DOCKERFILE = REPO / "docker" / "test" / "Dockerfile.no-forge"
PREBUILT_OVERLAY = REPO / "docker" / "docker-compose.test.prebuilt.yml"


@pytest.fixture(scope="session")
def no_forge_image(pytestconfig) -> str:
    """The shipped image with the forge binaries removed.

    Built here rather than imported as a fixture from `tests/image/conftest.py`,
    because a fixture defined in a sibling package's conftest is not visible to
    this one — the *functions* are, and those are what this uses.

    Two builds: the real image (usually a cache hit, since the compose stack in
    this same session just built it from the same context) and then the control
    on top of it. `Dockerfile.no-forge` takes the real tag as `BASE` precisely
    so the second is one `rm -rf` layer.
    """
    _require_no_xdist(pytestconfig)
    require_docker()
    platform = resolve_platform(pytestconfig)
    base = image_support.build_image(
        image_support.ISTOTA_DOCKERFILE, REPO, platform=platform, prefix="istota"
    )
    tag = f"istota-test/no-forge:{base.tag.rsplit(':', 1)[1]}"
    argv = [
        "docker", "build",
        "-f", str(NO_FORGE_DOCKERFILE),
        "--build-arg", f"BASE={base.tag}",
        "-t", tag,
    ]
    if platform:
        argv += ["--platform", platform]
    argv.append(str(NO_FORGE_DOCKERFILE.parent))

    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=image_support.BUILD_TIMEOUT
    )
    if result.returncode != 0:
        pytest.exit(
            "could not build the no-forge control image:\n"
            + "\n".join((result.stderr or result.stdout or "").splitlines()[-40:]),
            returncode=1,
        )
    return tag


@pytest.fixture
def forge_stack(pytestconfig, tmp_path, request):
    """The lean stack with the developer skill wired to a fake GitLab.

    Function-scoped like `lean_stack`, and for the same reason plus one: the
    stub's port is baked into the rendered config, and its recorded calls are
    per-scenario state that a shared stack would smear across tests.

    Two things here are worth not rediscovering.

    The stub binds all interfaces, because the daemon reaching it lives in a
    container. `serve` defaults to loopback for the same reason
    `model_endpoint.serve_script` does — this listener runs `git http-backend`,
    and publishing one on every `uv run pytest` is not a thing to do by
    default.

    The `[developer]` block is produced by `render-config.sh` from
    `ISTOTA_DEVELOPER_*` variables, not written by this fixture. So the config
    the scenarios exercise is one the shipped script can actually generate, and
    a change that breaks that generation fails here rather than in production.
    """
    yield from _forge_stack(pytestconfig, tmp_path, request)


@pytest.fixture
def broken_forge_stack(pytestconfig, tmp_path, request, no_forge_image):
    """The same stack, on an image whose forge binaries are missing.

    The negative control. Everything in `test_forge_e2e.py` is a claim that
    this tier can see a broken deployment; without an artifact that *is*
    broken, the claim is unfalsified and the whole file would pass identically
    if the daemon never ran a forge command. This reproduces ISSUE-263 exactly:
    a config naming `/usr/local/lib/istota_forge/glab`, and nothing at that
    path.
    """
    yield from _forge_stack(
        pytestconfig, tmp_path, request, image=no_forge_image
    )


def _forge_stack(pytestconfig, tmp_path, request, *, image: str = ""):
    """The body both forge fixtures share.

    A plain generator rather than a third fixture: the two differ only in which
    image they run, and a variant that re-stated the other forty lines would
    drift on the parts that are genuinely the same.
    """
    _require_no_xdist(pytestconfig)
    require_docker()

    turns = getattr(request, "param", None) or [{"text": "nothing scripted"}]
    config_dir = tmp_path / "config"
    config_dir.mkdir()

    endpoint = serve_script(turns, host="0.0.0.0")
    stub = fake_gitlab.serve(tmp_path / "forge", host="0.0.0.0")
    project = f"{PROJECT_PREFIX}{uuid.uuid4().hex[:8]}"

    env_file = tmp_path / "compose.env"
    lines = [f"ISTOTA_TEST_CONFIG_DIR={config_dir}"]
    overlays = []
    if image:
        # Both the overlay and the variable it interpolates. Compose reads the
        # env-file when it interpolates *every* subcommand, so a value supplied
        # only to `up` makes `ps`, `logs` and `down` fail before they reach a
        # container — the failure mode that once left two stacks running.
        lines.append(f"ISTOTA_TEST_IMAGE={image}")
        overlays.append(PREBUILT_OVERLAY)
    env_file.write_text("\n".join(lines) + "\n")
    args = compose_support.compose_args(
        COMPOSE_FILE, project=project, env_file=env_file, overlays=overlays
    )

    try:
        clone_url = stub.seed_repo(FORGE_PROJECT)
        _render_config(
            config_dir,
            endpoint.container_base_url,
            {
                "ISTOTA_DEVELOPER_ENABLED": "true",
                # A tmpfs the compose file already declares. The developer
                # skill binds it read-write into the sandbox, which is where
                # the scenarios clone.
                "ISTOTA_DEVELOPER_REPOS_DIR": "/data/repos",
                "ISTOTA_DEVELOPER_GITLAB_URL": stub.container_url,
                "ISTOTA_DEVELOPER_GITLAB_TOKEN": FORGE_TOKEN,
                "ISTOTA_DEVELOPER_GITLAB_USERNAME": "istota-test",
                "ISTOTA_DEVELOPER_GITLAB_DEFAULT_NAMESPACE": FORGE_PROJECT.split("/")[0],
            },
        )
        compose_support.up(args, platform=resolve_platform(pytestconfig))
        compose_support.wait_ready(args, "istota", timeout=READY_TIMEOUT)
        yield ForgeStack(
            args=args,
            endpoint=endpoint,
            config_dir=config_dir,
            stub=stub,
            clone_url=clone_url,
        )
    finally:
        compose_support.down(args, volumes=True)
        stub.close()
        endpoint.close()


class LeanStack:
    """What a smoke test is handed."""

    def __init__(self, *, args: list[str], endpoint, config_dir: Path):
        self.args = args
        self.endpoint = endpoint
        self.config_dir = config_dir
        self.probe = Probe(compose_args=args, service="istota")

    def submit(self, prompt: str, *, user_id: str = "testuser") -> int:
        """Enqueue a task through the shipped CLI and return its id.

        Through `istota task` rather than by writing a row directly: inserting
        into `tasks` would assert nothing about the image, and the point of this
        tier is that the artifact works.

        The id is parsed out and returned because the caller needs it: the
        daemon queues tasks of its own for the same user at startup, so an
        assertion filtered on `user_id` alone can land on the wrong row.
        """
        result = subprocess.run(
            self.args
            + [
                "exec",
                "-T",
                "istota",
                "uv",
                "run",
                "istota",
                "-c",
                "/data/config/config.toml",
                "task",
                prompt,
                "-u",
                user_id,
                "--source-type",
                "cli",
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if result.returncode != 0:
            pytest.fail(
                f"submitting a task exited {result.returncode}\n"
                f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}",
                pytrace=False,
            )
        match = re.search(r"Task created:\s*(\d+)", result.stdout)
        if not match:
            pytest.fail(
                "could not read a task id out of `istota task` output; the CLI "
                f"prints 'Task created: N'\n--- stdout ---\n{result.stdout}",
                pytrace=False,
            )
        return int(match.group(1))

    def logs(self, tail: int = 60) -> str:
        return compose_support.logs(self.args, "istota", tail=tail)


class ForgeStack(LeanStack):
    """A `LeanStack` plus the forge it was pointed at.

    A subclass rather than a second class: everything a forge scenario does to
    the daemon — submit a task, read the row back, pull the logs on failure — is
    what `LeanStack` already does, and duplicating it would let the two drift on
    the parts that are genuinely shared.
    """

    def __init__(self, *, stub, clone_url: str, **kwargs):
        super().__init__(**kwargs)
        self.stub = stub
        self.clone_url = clone_url

    def doctor(self, *, scope: str = "") -> list[dict]:
        """`istota doctor --json` inside the running container.

        Through the shipped CLI in the shipped image, which is the whole point:
        a doctor run on the host would be asking about the developer's laptop.

        The exit code is deliberately ignored — `doctor.exit_code` is non-zero
        when a check FAILs, and the negative control exists to produce exactly
        that. What matters is the payload, and it is valid JSON either way by
        construction (`render_json`).
        """
        argv = self.args + [
            "exec", "-T", "istota",
            "uv", "run", "istota", "-c", "/data/config/config.toml",
            "doctor", "--json",
        ]
        if scope:
            argv += ["--scope", scope]
        result = subprocess.run(
            argv, capture_output=True, text=True, timeout=180
        )
        try:
            return json.loads(result.stdout or "[]")
        except ValueError:
            pytest.fail(
                f"`istota doctor --json` did not print JSON (exit "
                f"{result.returncode})\n--- stdout ---\n{result.stdout}\n"
                f"--- stderr ---\n{result.stderr}",
                pytrace=False,
            )

    def merge_requests(self) -> list:
        """The MRs opened against the stub, which is the happy path's assertion."""
        return self.stub.rest_calls("POST", "/merge_requests")

    def diagnostics(self, task: dict) -> str:
        """One string carrying everything a failed forge scenario needs.

        Assembled in one place because the useful context is spread over four
        sources — the task row, the daemon log, the REST calls and the git
        calls — and a scenario that printed only the first reports "the task
        failed" for a wrapper that was denied, a token that never arrived and a
        stub endpoint that answered 501, all identically.
        """
        rest = "\n".join(f"  {call}" for call in self.stub.calls) or "  (none)"
        git = "\n".join(f"  {call}" for call in self.stub.git_calls) or "  (none)"
        return (
            f"task {task.get('id')} ended {task.get('status')!r}: "
            f"{task.get('error')!r}\n"
            f"--- result ---\n{task.get('result')}\n"
            f"--- rest calls ---\n{rest}\n"
            f"--- git calls ---\n{git}\n"
            f"--- daemon logs ---\n{self.logs(150)}"
        )
