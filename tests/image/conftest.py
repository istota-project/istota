"""Build the shipped images and run assertions against the result.

Layer 2 of the deployment-artifact-verification spec, and the layer that catches
ISSUE-263 directly. Everything else in the suite asserts against Python objects
on a developer's macOS host; this asserts against the thing that actually ships.

Two rules here are structural rather than stylistic, and both exist because the
obvious implementation is wrong:

**`-n0` is not optional.** ``addopts`` pins ``-n auto``, and a session-scoped
fixture under xdist is per-*worker* — N workers would each race to ``docker
build`` the same tag. The guard below fails the session with the reason rather
than leaving that to be diagnosed from interleaved output.

**The image's own ENTRYPOINT is unusable here.** It waits up to 600s for
``/mnt/shared/.istota-provisioned`` and then exits 1, so every ``docker run`` in
this tier overrides it. That is also why Group C runs ``render-config.sh``
directly: in a volume-less container there is nothing to provision against.

Skips happen at *setup*, never at import. Collection must not require a Docker
daemon, so a developer without one running still collects a clean session.
"""

from __future__ import annotations

import hashlib
import os
import platform as _platform  # `platform` is also a parameter name below
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ISTOTA_DOCKERFILE = REPO / "docker" / "istota" / "Dockerfile"
DEVBOX_DOCKERFILE = REPO / "docker" / "devbox" / "Dockerfile"

BUILD_TIMEOUT = 3600  # a cold amd64 build under qemu is tens of minutes

# Group A's doctor run probes half a dozen binaries with `--version` inside one
# container, and under qemu that is the slowest call in the tier. 120s is
# comfortable natively; the emulated multiplier keeps a slow run from surfacing
# as a TimeoutExpired, which reads as a broken image rather than a slow one.
RUN_TIMEOUT = 120
EMULATED_RUN_TIMEOUT = 600


_XDIST_MESSAGE = (
    "the image tier must run with -n0. Session-scoped image fixtures are "
    "per-xdist-worker, so N workers would race to `docker build` the same tag "
    "and then run against whichever won."
)


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Fail early when the tier is selected under xdist.

    Not a skip: a silent skip on a tier built to end silent non-execution
    repeats the original defect.

    ``trylast`` is load-bearing. pytest's own ``-m`` deselection happens in this
    same hook, so without it this runs against the *unfiltered* list and fires
    on an ordinary ``uv run pytest`` — which deselects the marker and was never
    going to build anything.

    **This hook alone does not catch a real xdist run**, which is the whole
    scenario. Under `-n 2` the controller never calls this hook (it holds no
    items), and xdist clears `numprocesses` and `dist` in the workers so they do
    not re-fan-out — so every reading here says "not parallel". Measured: a real
    `-m image -n 2` ran the entire tier ungated. The check that actually binds
    is `_require_no_xdist`, called from the image fixtures, where
    `config.workerinput` gives the worker away. This one stays because it turns
    the `--collect-only` and `--dist` spellings into an error before anything is
    built.
    """
    if not any(item.get_closest_marker("image") for item in items):
        return

    workers = getattr(config.option, "numprocesses", None)
    distribution = config.getoption("dist", "no")
    if workers or distribution not in ("no", None):
        raise pytest.UsageError(
            f"{_XDIST_MESSAGE} (saw -n {workers}, --dist {distribution})"
        )


def _require_no_xdist(config) -> None:
    """Refuse to build inside an xdist worker.

    `workerinput` is set by xdist on the worker's config and is absent in a
    single-process run — it is the only signal that survives into the place
    where the damage would be done. Called from both image fixtures rather than
    from a hook because the hook cannot see a real parallel run at all.
    """
    if hasattr(config, "workerinput"):
        worker = config.workerinput.get("workerid", "?")
        pytest.fail(f"{_XDIST_MESSAGE} (running in xdist worker {worker})", pytrace=False)


@dataclass(frozen=True)
class BuiltImage:
    tag: str
    dockerfile: Path
    platform: str


def resolve_platform(config) -> str:
    """`--platform`, else `$ISTOTA_TEST_PLATFORM`, else native.

    A bare architecture is accepted and normalized — `amd64` is what a person
    types and `linux/amd64` is what Docker wants, and getting that wrong builds
    natively while the tag claims otherwise.
    """
    raw = config.getoption("--platform") or os.environ.get("ISTOTA_TEST_PLATFORM") or ""
    raw = raw.strip()
    if not raw:
        return ""
    return raw if "/" in raw else f"linux/{raw}"


def is_emulated(image: BuiltImage) -> bool:
    """Whether this image runs under qemu on this host.

    Shared by `run_in`'s timeout and by the devbox file's amd64 gate, so the two
    cannot disagree about what "amd64" means.
    """
    if not image.platform:
        return False
    host = _platform.machine().lower()
    native = "amd64" if host in ("x86_64", "amd64") else "arm64"
    return not image.platform.endswith(native)


def docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def require_docker() -> None:
    if not docker_available():
        pytest.skip("no reachable Docker daemon")


def _tag_for(dockerfile: Path, platform: str, prefix: str) -> str:
    """A tag that cannot collide across HEAD, Dockerfile edits, or architecture.

    The platform is in the tag because a cached arm64 image and an amd64 one are
    different artifacts that would otherwise share a name — and the whole point
    of the amd64 opt-in is that it tests something the native build does not.

    The Dockerfile hash covers the uncommitted case: a dirty working tree at the
    same HEAD would otherwise reuse an image built from the previous text.

    The checkout path covers the concurrent case, which is the one that bites in
    this repo: work runs in parallel git worktrees, and two of them at the same
    HEAD with the same Dockerfile but different `src/` would otherwise share a
    tag — so a second `docker build -t <same tag>` moves the tag out from under
    the first run's `docker run`s, mid-session. Docker's layer cache is keyed on
    content rather than on the tag, so distinguishing them costs nothing on a
    warm cache.
    """
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    revision = (head.stdout or "").strip() or "nogit"
    digest = hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:12]
    checkout = hashlib.sha256(str(REPO).encode()).hexdigest()[:6]
    arch = (platform or "native").replace("/", "-")
    return f"istota-test/{prefix}:{revision}-{digest}-{checkout}-{arch}"


def build_image(dockerfile: Path, context: Path, *, platform: str = "", prefix: str) -> BuiltImage:
    """`docker build`, surfacing output only on failure.

    Ends the *session* rather than failing a test. `pytest.fail` inside a
    session-scoped fixture caches the exception and re-raises it for every
    dependent test, so a failed build printed its 50-line tail once per test —
    about 2600 lines of build log across this tier, which is the opposite of
    what the first version's comment claimed. `pytest.exit` stops after one.

    The tail goes to a file as well, because 50 lines is often not the line that
    matters and the report is not where you want to scroll for it.
    """
    require_docker()
    tag = _tag_for(dockerfile, platform, prefix)
    argv = ["docker", "build", "-f", str(dockerfile), "-t", tag]
    if platform:
        argv += ["--platform", platform]
    argv.append(str(context))

    result = subprocess.run(argv, capture_output=True, text=True, timeout=BUILD_TIMEOUT)
    if result.returncode != 0:
        output = result.stderr or result.stdout or ""
        log = Path(tempfile.gettempdir()) / f"istota-image-build-{prefix}.log"
        try:
            log.write_text(output)
            where = f"\nFull build output: {log}"
        except OSError:  # pragma: no cover - diagnostic path
            where = ""
        tail = "\n".join(output.splitlines()[-50:])
        pytest.exit(
            f"docker build failed for {dockerfile}:\n{tail}{where}",
            returncode=1,
        )
    return BuiltImage(tag=tag, dockerfile=dockerfile, platform=platform)


# Anything shaped like a credential in a value we passed in. A failing container
# assertion renders its output into the pytest report, and under a live run that
# output can carry a real token.
_CREDENTIAL_NAME = re.compile(
    r"(TOKEN|PASSWORD|SECRET|KEY|CREDENTIAL|PASSWD|API)", re.IGNORECASE
)


def scrub(text: str, env: dict[str, str] | None) -> str:
    """Replace every credential-shaped value we supplied with its variable name.

    Keyed on the *name* of the variable rather than on the shape of the value:
    a token has no universal shape, and the one thing we do reliably know here
    is which variables we ourselves put in the environment.

    Both the raw value and its TOML-escaped form, because `render-config.sh`
    escapes `"` and `\\` before writing a credential into config.toml — so a
    token containing either appears in the rendered file, and therefore in a
    failing Group C assertion, in a form a literal replace would walk straight
    past.
    """
    if not env:
        return text
    for name, value in env.items():
        if not value or not _CREDENTIAL_NAME.search(name):
            continue
        placeholder = f"<{name}>"
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        # Longest first: if the escaped form contains the raw one as a
        # substring, replacing the raw one first would leave a fragment behind.
        for form in sorted({value, escaped}, key=len, reverse=True):
            text = text.replace(form, placeholder)
    return text


def run_in(
    image: BuiltImage,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    entrypoint: str = "/bin/sh",
    timeout: int | None = None,
) -> subprocess.CompletedProcess:
    """`docker run --rm --entrypoint …`, with credential-scrubbed output.

    The entrypoint override is the default and not a convenience: the image's
    own ENTRYPOINT waits ten minutes for a provisioning flag that a volume-less
    container will never have.
    """
    require_docker()
    if timeout is None:
        timeout = EMULATED_RUN_TIMEOUT if is_emulated(image) else RUN_TIMEOUT

    cmd = ["docker", "run", "--rm", "--entrypoint", entrypoint]
    if image.platform:
        cmd += ["--platform", image.platform]

    # A credential-shaped value is passed as a bare `-e NAME` and handed to
    # docker through *our* environment, never as `-e NAME=value` in argv, which
    # any other user on the host can read out of `ps`. Today's values are
    # fabricated; the spec's `--live` variant reuses this helper with a real
    # token, and the repo already fixed this exact shape once for the deploy
    # credential (see .claude/rules/deployment.md).
    child_env = dict(os.environ)
    for name, value in (env or {}).items():
        if _CREDENTIAL_NAME.search(name):
            child_env[name] = value
            cmd += ["-e", name]
        else:
            cmd += ["-e", f"{name}={value}"]

    cmd.append(image.tag)
    cmd += argv

    # `args` is scrubbed too. pytest renders the whole CompletedProcess into an
    # assertion message, so a token in argv reaches the report by a path that
    # scrubbing stdout alone does not cover.
    safe_args = [scrub(part, env) for part in cmd]
    try:
        # `errors="replace"` rather than the default `strict`: several
        # assertions read the first bytes of a file to decide what it is, and a
        # binary there raises UnicodeDecodeError inside `Popen` before `sh()`
        # returns. The test then goes red without its assertion ever running or
        # its message ever rendering — red for the right image but the wrong
        # reason, which is indistinguishable from an assertion that works and
        # is exactly what the negative controls exist to tell apart. Found by
        # the ISSUE-281 control that puts a real ELF binary on PATH.
        result = subprocess.run(
            cmd, capture_output=True, text=True, errors="replace",
            timeout=timeout, env=child_env,
        )
    except subprocess.TimeoutExpired:
        # TimeoutExpired.__str__ is "Command '<full argv>' timed out after N
        # seconds" — the raw argv, straight into the traceback of every test
        # that passed a credential. Converted to a scrubbed failure instead.
        pytest.fail(
            f"`docker run` timed out after {timeout}s: {' '.join(safe_args)}",
            pytrace=False,
        )

    return subprocess.CompletedProcess(
        args=safe_args,
        returncode=result.returncode,
        stdout=scrub(result.stdout or "", env),
        stderr=scrub(result.stderr or "", env),
    )


def sh(image: BuiltImage, script: str, **kwargs) -> subprocess.CompletedProcess:
    """`run_in` for a shell snippet, which is most of what this tier does."""
    return run_in(image, ["-c", script], **kwargs)


def assert_ok(result: subprocess.CompletedProcess, what: str) -> str:
    assert result.returncode == 0, (
        f"{what} failed (exit {result.returncode})\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    return result.stdout


@pytest.fixture(scope="session")
def platform(pytestconfig) -> str:
    return resolve_platform(pytestconfig)


@pytest.fixture(scope="session")
def istota_image(platform, pytestconfig) -> BuiltImage:
    """The shipped image, built once per session.

    ``ISTOTA_IMAGE_TAG`` skips the build and tests a pre-built tag instead —
    that is how the upgrade driver reuses an image it already built, and how you
    would smoke a published one.
    """
    _require_no_xdist(pytestconfig)
    preexisting = os.environ.get("ISTOTA_IMAGE_TAG")
    if preexisting:
        require_docker()
        return BuiltImage(tag=preexisting, dockerfile=ISTOTA_DOCKERFILE, platform=platform)
    return build_image(ISTOTA_DOCKERFILE, REPO, platform=platform, prefix="istota")


@pytest.fixture(scope="session")
def devbox_image(platform, pytestconfig) -> BuiltImage:
    _require_no_xdist(pytestconfig)
    preexisting = os.environ.get("ISTOTA_DEVBOX_IMAGE_TAG")
    if preexisting:
        require_docker()
        return BuiltImage(tag=preexisting, dockerfile=DEVBOX_DOCKERFILE, platform=platform)
    return build_image(
        DEVBOX_DOCKERFILE, REPO / "docker" / "devbox", platform=platform, prefix="devbox"
    )
