"""The lean stack, brought up around one test.

Everything here exists to get from "a checkout" to "a running daemon that will
answer a task" in under thirty seconds, with no Nextcloud and no API key. Three
pieces make that possible, and each replaces something the full stack does
slowly:

- the config is rendered **on the host** by the same `render-config.sh` the
  image ships, so the container never enters the provisioning branch and its
  120-second Nextcloud polling loop;
- the model is a scripted HTTP endpoint in the pytest process, reached through
  `[brain.native] base_url`, so no credential and no network are involved;
- the stack is one service.

The machinery underneath lives in `testbed/` — `Stack`, the `Service` protocol,
the compose helpers, the probe. What stays here is the part that is specific to
the *lean shape*: which compose file, which image tag, and running the render
script on the host.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import uuid
from dataclasses import replace
from pathlib import Path

import pytest

from testbed import stack as stack_support
from testbed.profiles import BASE, FORGE, NO_FORGE, Profile
from testbed.services import Service, gitlab
from testbed.services.model_endpoint import serve_script
from testbed.stack import Stack

# Imported rather than re-derived. `--platform amd64` is a rootdir-level option
# that both Docker tiers honour, and the normalization — a bare `amd64`
# becoming `linux/amd64` — is the part that is easy to get subtly wrong. A
# second copy here would drift, and the symptom of drift is a native build
# wearing an amd64 label.
from ..conftest import resolve_platform
from ..image import conftest as image_support

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

# The credential the endpoint is bound with. It authenticates nothing — the
# daemon sends whatever `docker-compose.test.yml` hardcodes as
# `ISTOTA_BRAIN_NATIVE_API_KEY`, and this endpoint answers regardless — but
# `HttpStub.start` requires one for a non-loopback bind, so that the tier knows
# the name of every value it has published on a shared network.
ENDPOINT_CREDENTIAL = "unused-by-the-scripted-endpoint"


def lean_image_tag() -> str:
    """One image tag per checkout, shared by every stack this tier starts.

    Compose names a built image after the project, and the project is unique
    per test so an interrupted run's stack is never adopted by the next one.
    Images are not reclaimed by `down --volumes`, so that left one permanent
    tag per test. A single tag collapses them.

    Scoped by checkout path rather than fixed, because work in this repo runs
    in parallel git worktrees: two of them sharing a tag means the second
    `up --build` moves it out from under the first run's containers, mid-run.
    Same reasoning as `tests/image/conftest._tag_for`, which carries the same
    component for the same reason.
    """
    digest = hashlib.sha256(str(REPO).encode()).hexdigest()[:8]
    return f"istota-test/lean:{digest}"


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
    if not stack_support.docker_available():
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
    if stack_support.docker_available():
        stack_support.sweep_projects(PROJECT_PREFIX)
    yield


def _render_config(destination: Path, services: dict[str, Service]) -> Path:
    """Run the shipped render script on the host.

    This is the property that makes the shortcut legitimate: the file the lean
    stack boots from is produced by the same script the container would have
    run, not by a fixture that approximates it.

    Each service contributes its own `config_env()` — the variables that point
    the daemon at it — merged over the base. Every one of those is a variable
    `render-config.sh` already reads, so a block the shipped script would not
    have produced cannot be smuggled in here, and this environment is left with
    nothing subsystem-specific in it.
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
    }
    for service in services.values():
        environment.update(service.config_env())
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


def _start_stack(
    pytestconfig,
    tmp_path: Path,
    profile: Profile,
    services: dict[str, Service],
):
    """Render, bring the stack up, hand back a `Stack`, tear it down.

    A generator rather than a fixture so the three fixtures below can each
    decide what to start *before* calling it — the services have to be
    listening before the config that names their ports is rendered.
    """
    config_dir = tmp_path / "config"
    config_dir.mkdir()

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
    lines = [
        f"ISTOTA_TEST_CONFIG_DIR={config_dir}",
        f"ISTOTA_TEST_LEAN_IMAGE={lean_image_tag()}",
    ]
    overlays = []
    if profile.image:
        # Both the overlay and the variable it interpolates, for the same
        # every-subcommand reason as the config dir above.
        lines.append(f"ISTOTA_TEST_IMAGE={profile.image}")
        overlays.append(PREBUILT_OVERLAY)
    env_file.write_text("\n".join(lines) + "\n")
    args = stack_support.compose_args(
        COMPOSE_FILE, project=project, env_file=env_file, overlays=overlays
    )

    try:
        _render_config(config_dir, services)
        stack_support.up(args, platform=resolve_platform(pytestconfig))
        stack_support.wait_ready(args, "istota", timeout=READY_TIMEOUT)
        yield Stack(
            profile=profile, args=args, services=services, config_dir=config_dir
        )
    finally:
        # Volumes too: the DB is a named volume, and leaving it behind would
        # make the next run's assertions depend on this one's rows.
        stack_support.down(args, volumes=True)


@pytest.fixture
def lean_stack(pytestconfig, tmp_path, request):
    """A running daemon and the endpoint it talks to.

    Function-scoped on purpose, until Stage 2 of the deployment-testbed spec
    replaces this with a session-scoped pool. The scripted turns differ per
    test, and the endpoint's base URL is baked into the rendered config, so a
    shared stack would have to be reconfigured and restarted between tests
    anyway — at which point the sharing saves nothing and couples the tests to
    each other's scripts.
    """
    _require_no_xdist(pytestconfig)
    require_docker()

    turns = getattr(request, "param", None) or [{"text": "the scripted answer"}]

    # All interfaces, explicitly. The default is loopback so an ordinary
    # `uv run pytest` never opens a listener beyond it; this tier is the one
    # caller that genuinely needs the container to reach back in.
    endpoint = serve_script(turns, host="0.0.0.0", credential=ENDPOINT_CREDENTIAL)
    try:
        yield from _start_stack(pytestconfig, tmp_path, BASE, {"model": endpoint})
    finally:
        endpoint.close()



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

    # `ISTOTA_IMAGE_TAG` first, exactly as `image_support.istota_image` does.
    # Without it the control is built from the local checkout while the
    # correct-image half of the pair is whatever tag the environment named — so
    # the two differ by more than the forge binaries, and the control measures
    # the difference between two builds rather than the thing it exists for.
    preexisting = os.environ.get("ISTOTA_IMAGE_TAG")
    if preexisting:
        base_tag = preexisting
    else:
        base_tag = image_support.build_image(
            image_support.ISTOTA_DOCKERFILE, REPO, platform=platform, prefix="istota"
        ).tag
    tag = f"istota-test/no-forge:{base_tag.rsplit(':', 1)[-1]}"
    argv = [
        "docker", "build",
        "-f", str(NO_FORGE_DOCKERFILE),
        "--build-arg", f"BASE={base_tag}",
        "-t", tag,
    ]
    if platform:
        argv += ["--platform", platform]
    argv.append(str(NO_FORGE_DOCKERFILE.parent))

    result = subprocess.run(
        argv, capture_output=True, text=True, timeout=image_support.BUILD_TIMEOUT
    )
    if result.returncode != 0:
        # `fail`, not `exit`. A Docker hiccup building the control must not
        # terminate the whole session and take any other tier queued behind it
        # with it; every other failure path in this file uses `fail` too.
        pytest.fail(
            "could not build the no-forge control image:\n"
            + "\n".join((result.stderr or result.stdout or "").splitlines()[-40:]),
            pytrace=False,
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
    container. `gitlab.serve` defaults to loopback for the same reason
    `serve_script` does — this listener runs `git http-backend`, and publishing
    one on every `uv run pytest` is not a thing to do by default.

    The `[developer]` block is produced by `render-config.sh` from the
    `ISTOTA_DEVELOPER_*` variables in `GitLabService.config_env()`, not written
    by this fixture. So the config the scenarios exercise is one the shipped
    script can actually generate, and a change that breaks that generation fails
    here rather than in production.
    """
    yield from _forge_stack(pytestconfig, tmp_path, request, FORGE)


@pytest.fixture
def broken_forge_stack(pytestconfig, tmp_path, request, no_forge_image):
    """The same stack, on an image whose forge binaries are missing.

    The negative control. Everything in `test_forge_e2e.py` is a claim that
    this tier can see a broken deployment; without an artifact that *is*
    broken, the claim is unfalsified and the whole file would pass identically
    if the daemon never ran a forge command. This reproduces ISSUE-263 exactly:
    a config naming `/usr/local/lib/istota_forge/glab`, and nothing at that
    path.

    The tag is filled into the profile here rather than written into
    `profiles.py`, because it is derived from whichever image the session built.
    """
    yield from _forge_stack(
        pytestconfig,
        tmp_path,
        request,
        replace(NO_FORGE, image=no_forge_image),
    )


def _forge_stack(pytestconfig, tmp_path, request, profile: Profile):
    """The body both forge fixtures share.

    A plain generator rather than a third fixture: the two differ only in which
    image they run, and a variant that re-stated the other forty lines would
    drift on the parts that are genuinely the same.
    """
    _require_no_xdist(pytestconfig)
    require_docker()

    turns = getattr(request, "param", None) or [{"text": "nothing scripted"}]

    # Both listeners are started inside the `try`, not before it: `gitlab.serve`
    # does a `mkdir` and a bind, either of which can raise, and an endpoint
    # started on the line above would then never be closed — leaking a bound
    # port and a live thread for the rest of the session. Both bind all
    # interfaces, so the leak is a publicly-bound socket.
    endpoint = None
    stub = None
    try:
        endpoint = serve_script(turns, host="0.0.0.0", credential=ENDPOINT_CREDENTIAL)
        # The token is both what the daemon is configured with and what this
        # listener challenges for, which is what makes
        # `authenticated_git_calls()` mean "the helper produced the right
        # token". It is also the access control: the listener is bound to all
        # interfaces so the container can reach it, and it serves a real
        # `git http-backend`.
        stub = gitlab.serve(
            tmp_path / "forge", host="0.0.0.0", token=gitlab.FORGE_TOKEN
        )
        stub.seed_repo(stub.project)
        yield from _start_stack(
            pytestconfig, tmp_path, profile, {"model": endpoint, "gitlab": stub}
        )
    finally:
        # Guarded, because either may be None if the other's construction raised.
        if stub is not None:
            stub.close()
        if endpoint is not None:
            endpoint.close()
