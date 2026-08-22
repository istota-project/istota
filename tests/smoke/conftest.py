"""The lean stack, brought up once per profile and shared for the session.

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

A test declares what it needs and is handed a stack that already has it:

    @pytest.mark.profile("forge")
    @pytest.mark.script([{"text": "done"}])
    def test_something(stack): ...

`profile` defaults to `"base"` and `script` to one plain answer. Both are
optional, and a scenario whose script depends on something only known at run
time — a stub's port, say — calls `stack.script(...)` inside the test instead.

**Stacks are session-scoped, one per profile.** The fixture that used to boot
one per test argued that the endpoint's `base_url` is baked into the rendered
config, so a shared stack would need reconfiguring between tests anyway. That
held only because the endpoint was started immediately before the render. Here
the services start once per profile, before that profile's config is rendered,
and live as long as the stack — so the address stays valid and `rescript`
handles the per-test script, which is what it was written for. `Stack.reset` is
what makes the sharing safe; read its docstring before adding a scenario that
mutates something.

The machinery underneath lives in `testbed/` — `StackPool`, `Stack`, the
`Service` protocol, the compose helpers, the probe. What stays here is the part
that is specific to the *lean shape* and to pytest: which compose file, which
image tag, the xdist guards, and building the negative control's image.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import replace
from pathlib import Path

import pytest

from testbed import probe as probe_support
from testbed import profiles
from testbed import stack as stack_support

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
NO_FORGE_DOCKERFILE = REPO / "docker" / "test" / "Dockerfile.no-forge"
PREBUILT_OVERLAY = REPO / "docker" / "docker-compose.test.prebuilt.yml"

READY_TIMEOUT = 120

_XDIST_MESSAGE = (
    "the smoke tier must run with -n0. Session-scoped fixtures are per-worker, "
    "so N workers would each build the image and bring up their own stacks "
    "under one project prefix, race the same daemon, and sweep each other's "
    "projects."
)

# Every project this tier creates starts with it, which is what makes the
# session-start sweep able to find leftovers without touching anything else.
PROJECT_PREFIX = "istota-smoke-"

# What a test gets when it declares no `script` marker. One plain answer, which
# is enough for any scenario that only needs a task to complete —
# `test_lean_stack.py` asserts on this exact string.
DEFAULT_SCRIPT = [{"text": "the scripted answer"}]


def lean_image_tag() -> str:
    """One image tag per checkout, shared by every stack this tier starts.

    Compose names a built image after the project, and the project is unique
    per stack so an interrupted run's containers are never adopted by the next
    session. Images are not reclaimed by `down --volumes`, so that left one
    permanent tag per stack. A single tag collapses them.

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

    A unique project name per stack stops one run from adopting another's
    containers mid-flight, but it also means nothing ever reclaims them: a
    killed session leaves a container and a named volume behind for good. One
    sweep at session start closes that, and it is scoped by the prefix so it can
    never touch a developer's own stack.
    """
    if stack_support.docker_available():
        stack_support.sweep_projects(PROJECT_PREFIX)
    yield


@pytest.fixture(scope="session", autouse=True)
def _measure_probe_exec(request):
    """Report what the tier spent inside `docker compose exec`.

    Open question 4 in the deployment-testbed spec asks whether a `Probe` query
    per poll is fast enough once one stack serves a whole session, and answers
    it with a measurement rather than a long-lived reader process nobody has
    shown is needed. This is that measurement, and it stays because the answer
    changes as the tier grows — a number printed on every run is what makes a
    regression visible before it is a complaint.

    The span is the tier's, not the session's: it opens at the first smoke test
    and closes at session teardown, so a `-m smoke` run reports a fraction of
    the thing that was actually running.
    """
    probe_support.reset_exec_stats()
    started = time.monotonic()
    yield
    stats = probe_support.exec_stats()
    elapsed = time.monotonic() - started
    if not stats.calls or elapsed <= 0:
        return
    reporter = request.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:  # pragma: no cover - only under a custom -p
        return
    reporter.write_line(
        f"probe: {stats.calls} `docker compose exec` call(s), "
        f"{stats.seconds:.1f}s of {elapsed:.1f}s "
        f"({stats.seconds / elapsed:.0%} of the tier), "
        f"{stats.seconds / stats.calls * 1000:.0f}ms each"
    )


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


@pytest.fixture(scope="session")
def stacks(pytestconfig, tmp_path_factory):
    """Lazily-started, session-scoped stacks, keyed by profile name.

    Nothing is booted here. The pool starts a stack the first time a test
    declares its profile, so a run selecting only the forge scenarios never
    pays for a `base` stack — and `close_all` tears down whatever ended up
    running, volumes included.
    """
    _require_no_xdist(pytestconfig)
    require_docker()

    pool = stack_support.StackPool(
        workdir=tmp_path_factory.mktemp("testbed"),
        lean=stack_support.LeanShape(
            compose_file=COMPOSE_FILE,
            render_script=RENDER_CONFIG,
            image=lean_image_tag(),
            prebuilt_overlay=PREBUILT_OVERLAY,
            ready_timeout=READY_TIMEOUT,
        ),
        platform=resolve_platform(pytestconfig),
        project_prefix=PROJECT_PREFIX,
    )
    try:
        yield pool
    finally:
        pool.close_all()


@pytest.fixture
def stack(request, stacks):
    """The stack for the profile this test declared, reset and quiescent.

    `reset` runs *before* the test rather than after, so a failed test's state
    is still there to inspect and the next test is still clean.

    `no-forge` is the one profile whose image cannot be written down: it is
    derived from whichever image the session actually built. The tag is filled
    in here, and `getfixturevalue` rather than a fixture argument so a run with
    no negative control in it never builds the second image.

    The reset's watermark is stashed as `stack.mark`, because the instant it is
    taken is the one that matters: after this test's reset and before anything
    it does. A scenario taking its own would take it after `submit`, which is
    too late for the row it wants to prove was never written. See
    `Probe.rows_above`.
    """
    marker = request.node.get_closest_marker("profile")
    name = marker.args[0] if marker and marker.args else profiles.BASE.name
    fresh = bool(marker.kwargs.get("fresh")) if marker else False

    profile = profiles.by_name(name)
    if profile.name == profiles.NO_FORGE.name:
        profile = replace(profile, image=request.getfixturevalue("no_forge_image"))

    running = stacks.get(profile, fresh=fresh)
    script_marker = request.node.get_closest_marker("script")
    turns = (
        list(script_marker.args[0])
        if script_marker and script_marker.args
        else list(DEFAULT_SCRIPT)
    )
    try:
        try:
            # `pytrace=False`, because a reset that could not quiesce is a
            # harness condition rather than a code defect, and a traceback
            # through three fixture frames buries the one line that says which
            # task ids were still in flight. `testbed` raises rather than
            # calling `pytest.fail` itself — it is an installable package two
            # repos outside this one consume — so the translation happens here.
            running.mark = running.reset(turns)
        except (TimeoutError, stack_support.StackError) as exc:
            pytest.fail(str(exc), pytrace=False)
        yield running
    finally:
        if fresh:
            stacks.release(running)
