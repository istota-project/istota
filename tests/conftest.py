"""Shared test fixtures for istota tests."""

import os
from pathlib import Path

import pytest


def _load_dotenv():
    """Load .env file from project root into os.environ (simple key=value parser)."""
    env_file = Path(__file__).parent.parent / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value:
            os.environ.setdefault(key, value)


_load_dotenv()

# Default-off in tests: most feeds tests expect an empty DB. The seed
# tests in test_feeds_migrate.py monkeypatch this var off explicitly.
os.environ.setdefault("ISTOTA_FEEDS_SKIP_DEFAULT_SEED", "1")
# Same pattern for the money default-ledger seed: most money tests
# expect a clean ledgers/ dir. The seed tests in test_migrate.py
# monkeypatch this var off explicitly.
os.environ.setdefault("ISTOTA_MONEY_SKIP_DEFAULT_SEED", "1")
# web_app's session middleware fails closed without a signing secret
# (ISSUE-124). Tests don't configure one, so opt into the random per-process
# dev secret. Tests that assert the fail-closed behaviour clear this explicitly.
os.environ.setdefault("ISTOTA_WEB_ALLOW_INSECURE_SESSION", "1")

from istota import db
from istota.config import Config, UserConfig


@pytest.fixture(autouse=True)
def _skip_dac_tests_as_root(request):
    """Root bypasses the permission bits these tests are made of.

    A `chmod 0o500` directory is still writable by uid 0, and a `chmod 0o000`
    file is still readable, so a test asserting "this fails" asserts nothing
    and reports a failure that says nothing about the code. The developer host
    runs as a normal user and never notices; `scripts/test-linux.sh` runs as
    root in a container and does.

    `geteuid` via `getattr`: it does not exist on Windows, and an autouse
    fixture that raised `AttributeError` would error every test in the suite
    rather than skip two. -1 is nobody, so nothing skips.
    """
    if request.node.get_closest_marker("requires_dac"):
        if getattr(os, "geteuid", lambda: -1)() == 0:
            pytest.skip("running as root: POSIX permission bits do not constrain this process")


@pytest.fixture(autouse=True)
def _no_network_symbol_lookups(monkeypatch):
    """Portfolio auto-classification's default fetch is a live yfinance
    lookup, and an import triggers it — so any test that reaches a portfolio
    import would otherwise hit the network. Root-level rather than scoped to
    ``tests/money/``, since the import path is reachable from the web-route
    and skill tests too. Tests exercising the lookup path inject their own
    fetch; ``TestFetchSymbolInfo`` captures the real function at import time.
    """
    try:
        from istota.money import portfolio_autoclass
    except Exception:
        # Money extra absent, or its import chain unhappy. Broad on purpose:
        # this fixture is purely defensive and runs before every test in the
        # suite, so anything it raises fails thousands of unrelated tests
        # with a traceback pointing at the wrong place.
        return

    monkeypatch.setattr(
        portfolio_autoclass, "fetch_symbol_info", lambda symbol, **kwargs: None
    )


@pytest.fixture
def db_path(tmp_path):
    """Initialize a real SQLite database using schema.sql and return its path."""
    path = tmp_path / "test.db"
    db.init_db(path)
    return path


@pytest.fixture
def db_conn(db_path):
    """Yield a database connection with row factory set."""
    with db.get_db(db_path) as conn:
        yield conn


@pytest.fixture
def make_task():
    """Factory fixture that creates Task dataclass instances with defaults."""
    def _make_task(**overrides):
        defaults = {
            "id": 1,
            "prompt": "test prompt",
            "user_id": "testuser",
            "source_type": "cli",
            "status": "pending",
        }
        defaults.update(overrides)
        return db.Task(**defaults)
    return _make_task


@pytest.fixture
def make_config(tmp_path):
    """Factory fixture that creates Config instances with tmp paths."""
    def _make_config(**overrides):
        skills_dir = tmp_path / "skills"
        skills_dir.mkdir(exist_ok=True)
        index_file = skills_dir / "_index.toml"
        if not index_file.exists():
            index_file.write_text("")

        mount_path = tmp_path / "mount"
        mount_path.mkdir(exist_ok=True)

        defaults = {
            "db_path": tmp_path / "test.db",
            "temp_dir": tmp_path / "temp",
            "skills_dir": skills_dir,
            "nextcloud_mount_path": mount_path,
        }
        defaults.update(overrides)
        return Config(**defaults)
    return _make_config


@pytest.fixture
def make_user_config():
    """Factory fixture that creates UserConfig instances with defaults."""
    def _make_user_config(**overrides):
        defaults = {
            "display_name": "Test User",
            "email_addresses": [],
            "timezone": "UTC",
            "briefings": [],
        }
        defaults.update(overrides)
        return UserConfig(**defaults)
    return _make_user_config


@pytest.fixture(autouse=True)
def _reset_async_runtime_singletons():
    """Isolate the process-global persistent asyncio runtime + TalkClient.

    These singletons (``istota.async_runtime._RUNTIME`` / ``_TALK_CLIENT``)
    persist across tests within an xdist worker. A test that lazily starts the
    runtime or opens the shared client and doesn't reset it would leak that
    state into the next test on the same worker (e.g. a returned-singleton whose
    httpx pool is already open). Reset before and after every test so isolation
    doesn't depend on each Talk-touching test remembering to clean up. Cheap for
    the vast majority of tests that never touch the runtime: the reset helpers
    early-return when the globals are still ``None``.
    """
    from istota.async_runtime import reset_async_runtime, reset_talk_client

    reset_talk_client()
    reset_async_runtime()
    yield
    reset_talk_client()
    reset_async_runtime()


@pytest.fixture
def outbound_gate_off(monkeypatch, tmp_path):
    """Put the outbound approval gate in ``off`` mode for a send-mechanics test.

    The gate runs for real and answers "send" — it is not patched out. Tests
    about how `send` / `reply` build a message have nothing to say about the
    policy, and the default floor (`untrusted`) would hold every one of their
    fixture recipients. The gate's own behaviour is covered in
    ``test_outbound_gate.py`` and ``test_outbound_gate_fires.py``.

    Also isolates the catch-all-pattern warning latch, a process-global set that
    would otherwise carry across tests in an xdist worker.
    """
    from istota import outbound_policy
    from istota.config import Config, EmailConfig

    db_path = tmp_path / "gate-off.db"
    db.init_db(db_path)
    cfg = Config(
        db_path=db_path,
        email=EmailConfig(enabled=True, outbound_approval_floor="off"),
        users={"alice": UserConfig(display_name="Alice")},
    )
    monkeypatch.setattr("istota.config.load_config", lambda *a, **k: cfg)
    monkeypatch.setenv("ISTOTA_USER_ID", "alice")
    outbound_policy._warned_catch_all.clear()
    yield cfg
    outbound_policy._warned_catch_all.clear()


@pytest.fixture(autouse=True)
def _reset_expunge_warning_latch():
    """Isolate ``skills.email._expunge_warned_hosts``.

    A process-global "warned about this host already" set, so any test that
    drives a mailbox without UIDPLUS seeds it for the rest of that xdist
    worker. Tests must be order-independent, and a test asserting on that
    warning would otherwise pass or fail on who ran first.
    """
    from istota.skills import email as email_skill

    email_skill._expunge_warned_hosts.clear()
    yield
    email_skill._expunge_warned_hosts.clear()


def pytest_addoption(parser):
    """`--platform` for the image tier.

    Lives here rather than in ``tests/image/conftest.py`` because pytest only
    honours ``pytest_addoption`` in an *initial* conftest — the rootdir's and
    the testpaths' — and a subdirectory conftest is loaded after argument
    parsing has already happened.

    The development machine is arm64 and production is amd64. A native build is
    fast and an emulated one is not, so native is the default and amd64 is an
    explicit opt-in taken before a release. ``ISTOTA_TEST_PLATFORM`` is the
    environment-variable form, for the shell drivers.
    """
    parser.addoption(
        "--platform",
        action="store",
        default=None,
        metavar="PLATFORM",
        help=(
            "Docker platform for the image tier, e.g. amd64 or linux/amd64. "
            "Defaults to native, or to $ISTOTA_TEST_PLATFORM."
        ),
    )


def resolve_platform(config) -> str:
    """`--platform`, else `$ISTOTA_TEST_PLATFORM`, else native.

    A bare architecture is accepted and normalized — `amd64` is what a person
    types and `linux/amd64` is what Docker wants, and getting that wrong builds
    natively while the tag claims otherwise.

    Here rather than in ``tests/image/conftest.py`` because three Docker tiers
    now read it. The smoke tier used to import it across package boundaries
    (``from ..image.conftest import resolve_platform``), which meant one tier's
    fixtures depended on another's conftest for a five-line pure function; the
    option it reads is declared just above, so this is where it belongs.
    """
    raw = config.getoption("--platform") or os.environ.get("ISTOTA_TEST_PLATFORM") or ""
    raw = raw.strip()
    if not raw:
        return ""
    return raw if "/" in raw else f"linux/{raw}"


# --- Deployment tiers: the stack fixtures both shapes share ----------------
#
# Hoisted here from `tests/smoke/conftest.py` in Stage 3 of the
# deployment-testbed spec, at the point the reason for hoisting appeared:
# `tests/full/` needs the same `stacks` / `stack` pair, and a fixture defined in
# a sibling package's conftest is invisible to another. What stays down in
# `tests/smoke/conftest.py` is what is specific to the *lean* shape — its image
# tag and the negative control's image.
#
# **Nothing here is autouse**, and that is the constraint that shaped it. The
# sweep and the exec measurement were autouse session fixtures while they lived
# under `tests/smoke/`, where they only ever applied to that directory. At the
# rootdir an autouse session fixture runs on *every* `uv run pytest`, and the
# sweep shells out to `docker info`. They are requested by `stacks` instead, so
# they still run exactly once and only when a stack is actually asked for.

import hashlib  # noqa: E402 - this file's imports are split by section, above
import time  # noqa: E402
from dataclasses import replace  # noqa: E402

from testbed import probe as probe_support  # noqa: E402
from testbed import profiles  # noqa: E402
from testbed import stack as stack_support  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
LEAN_COMPOSE_FILE = REPO / "docker" / "docker-compose.test.yml"
RENDER_CONFIG = REPO / "docker" / "istota" / "render-config.sh"
LEAN_PREBUILT_OVERLAY = REPO / "docker" / "docker-compose.test.prebuilt.yml"
FULL_COMPOSE_FILE = REPO / "docker" / "docker-compose.yml"
TESTBED_OVERLAY = REPO / "testbed" / "compose" / "testbed.yml"

LEAN_READY_TIMEOUT = 120

#: The tiers that must run `-n0`, and therefore the ones the guard below covers.
SERIAL_TIER_MARKERS = ("smoke", "full", "testbed")

#: Every compose project these tiers create starts with it, which is what makes
#: the session-start sweep able to find leftovers without touching anything else
#: — a developer's own demo or red-team stack is never named this.
PROJECT_PREFIX = "istota-testbed-"

#: The prefix the smoke tier used before Stage 3 gave both shapes one pool.
#: Swept as well as the current one, so a stack left behind by a run from before
#: the rename is still reclaimed rather than surviving forever.
LEGACY_PROJECT_PREFIXES = ("istota-smoke-",)

_XDIST_MESSAGE = (
    "the smoke, full and testbed tiers must run with -n0. Session-scoped "
    "fixtures are per-worker, so N workers would each build the image and bring "
    "up their own stacks under one project prefix, race the same daemon, and "
    "sweep each other's projects. The wire tier is milder and still wrong: N "
    "workers would each start a mail container, and the assertions there are "
    "about what is in a mailbox."
)

# What a test gets when it declares no `script` marker. One plain answer, which
# is enough for any scenario that only needs a task to complete —
# `test_lean_stack.py` asserts on this exact string.
DEFAULT_SCRIPT = [{"text": "the scripted answer"}]


def lean_image_tag() -> str:
    """One image tag per checkout, shared by every lean stack in the session.

    Compose names a built image after the project, and the project is unique per
    stack so an interrupted run's containers are never adopted by the next
    session. Images are not reclaimed by `down --volumes`, so that left one
    permanent tag per stack. A single tag collapses them.

    Scoped by checkout path rather than fixed, because work in this repo runs in
    parallel git worktrees: two of them sharing a tag means the second
    `up --build` moves it out from under the first run's containers, mid-run.
    Same reasoning as `tests/image/conftest._tag_for`.

    The full shape needs no equivalent. Its `build:` blocks name no `image:`, so
    compose tags them `<project>-<service>` and each stack gets its own.
    """
    digest = hashlib.sha256(str(REPO).encode()).hexdigest()[:8]
    return f"istota-test/lean:{digest}"


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Fail early when a serial tier is selected under xdist.

    `trylast` matters because this hook is also where `-m` deselection happens —
    without it the unfiltered item list is what arrives, and an ordinary
    `uv run pytest` fails with a usage error about a tier it had already
    deselected.

    **It cannot see a real parallel run**, which is the actual scenario. Under
    `-n 2` the controller never calls this (it holds no items) and xdist clears
    `numprocesses` in the workers so they do not re-fan-out. `_require_no_xdist`
    is the check that binds.
    """
    if not any(
        item.get_closest_marker(marker)
        for item in items
        for marker in SERIAL_TIER_MARKERS
    ):
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
        pytest.fail(
            f"{_XDIST_MESSAGE} (running in xdist worker {worker})", pytrace=False
        )


def require_docker() -> None:
    if not stack_support.docker_available():
        pytest.skip("no Docker daemon available")


@pytest.fixture(scope="session")
def _sweep_leftover_stacks():
    """Reclaim stacks an earlier run was killed before tearing down.

    A unique project name per stack stops one run from adopting another's
    containers mid-flight, but it also means nothing ever reclaims them: a killed
    session leaves a container and a named volume behind for good. One sweep at
    the first stack request closes that, scoped by prefix so it can never touch a
    developer's own stack.
    """
    if stack_support.docker_available():
        for prefix in (PROJECT_PREFIX, *LEGACY_PROJECT_PREFIXES):
            stack_support.sweep_projects(prefix)
    yield


@pytest.fixture(scope="session")
def _measure_probe_exec(request):
    """Report what the tier spent inside `docker compose exec`.

    Open question 4 in the deployment-testbed spec asks whether a `Probe` query
    per poll is fast enough once one stack serves a whole session, and answers it
    with a measurement rather than a long-lived reader process nobody has shown
    is needed. This is that measurement, and it stays because the answer changes
    as the tier grows — a number printed on every run is what makes a regression
    visible before it is a complaint. Stage 2 measured 31% for the lean shape;
    the full shape has a longer session and the same counters.

    The span opens at the first stack request and closes at session teardown, so
    a `-m smoke` or `-m full` run reports a fraction of the thing that was
    actually running.
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


def _keep_scope() -> str:
    """One kept credential set per checkout, matching the kept project name.

    `StackPool._compose_args_full` derives the project from the compose file's
    resolved path for the same reason: two worktrees sharing a kept volume set
    would each boot the other's half-provisioned Nextcloud.
    """
    return hashlib.sha256(str(FULL_COMPOSE_FILE.resolve()).encode()).hexdigest()[:8]


def _report_boot_times(config, pool) -> None:
    """Print where a cold boot went, once, at session end.

    Open question 2 asks whether the provisioned volume set needs snapshotting,
    and says it should be settled against Stage 3's measurement rather than
    against the "roughly ten minutes" a comment remembers. A number nobody has to
    instrument for is what makes that possible later.
    """
    if not pool.boot_times:
        return
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:  # pragma: no cover - only under a custom -p
        return
    for profile, service, seconds in pool.boot_times:
        reporter.write_line(f"boot: {profile} waited {seconds:.0f}s on {service}")


@pytest.fixture(scope="session")
def stacks(pytestconfig, tmp_path_factory, _sweep_leftover_stacks, _measure_probe_exec):
    """Lazily-started, session-scoped stacks, keyed by profile name.

    Nothing is booted here. The pool starts a stack the first time a test
    declares its profile, so a run selecting only the forge scenarios never pays
    for a `base` stack, and one selecting only lean scenarios never pays the full
    shape's cold boot — and `close_all` tears down whatever ended up running.

    One pool for both shapes rather than one per tier, so a session that happened
    to select from both sweeps once and tears down once.
    """
    _require_no_xdist(pytestconfig)
    require_docker()

    keep = bool(os.environ.get("ISTOTA_TESTBED_KEEP"))
    pool = stack_support.StackPool(
        workdir=tmp_path_factory.mktemp("testbed"),
        lean=stack_support.LeanShape(
            compose_file=LEAN_COMPOSE_FILE,
            render_script=RENDER_CONFIG,
            image=lean_image_tag(),
            prebuilt_overlay=LEAN_PREBUILT_OVERLAY,
            ready_timeout=LEAN_READY_TIMEOUT,
        ),
        full=stack_support.FullShape(
            compose_file=FULL_COMPOSE_FILE,
            overlay=TESTBED_OVERLAY,
            keep=keep,
            # Outside the checkout, with the other machine-wide test state:
            # these are real generated passwords, and the repo's pre-commit hook
            # exists because credentials end up in trees.
            keep_dir=Path.home() / ".cache" / "istota-testbed" / _keep_scope(),
        ),
        platform=resolve_platform(pytestconfig),
        project_prefix=PROJECT_PREFIX,
    )
    try:
        yield pool
    finally:
        pool.close_all()
        _report_boot_times(pytestconfig, pool)


@pytest.fixture
def stack(request, stacks):
    """The stack for the profile this test declared, reset and quiescent.

    `reset` runs *before* the test rather than after, so a failed test's state is
    still there to inspect and the next test is still clean.

    `no-forge` is the one profile whose image cannot be written down: it is
    derived from whichever image the session actually built. The tag is filled in
    here, and `getfixturevalue` rather than a fixture argument so a run with no
    negative control in it never builds the second image — and so this fixture,
    which now lives at the rootdir, does not have to see a lean-only fixture that
    still lives under `tests/smoke/`.

    The reset's watermark is stashed as `stack.mark`, because the instant it is
    taken is the one that matters: after this test's reset and before anything it
    does. A scenario taking its own would take it after `submit`, which is too
    late for the row it wants to prove was never written.
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
            # harness condition rather than a code defect, and a traceback through
            # three fixture frames buries the one line that says which task ids
            # were still in flight. `testbed` raises rather than calling
            # `pytest.fail` itself — it is an installable package two repos
            # outside this one consume — so the translation happens here.
            running.mark = running.reset(turns)
        except (TimeoutError, stack_support.StackError) as exc:
            pytest.fail(str(exc), pytrace=False)
        yield running
    finally:
        if fresh:
            stacks.release(running)
