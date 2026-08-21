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
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
ISTOTA_DOCKERFILE = REPO / "docker" / "istota" / "Dockerfile"
DEVBOX_DOCKERFILE = REPO / "docker" / "devbox" / "Dockerfile"

BUILD_TIMEOUT = 3600  # a cold amd64 build under qemu is tens of minutes
RUN_TIMEOUT = 120


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    """Fail the session if xdist is active and any image test was selected.

    Not a skip: a silent skip on a tier built to end silent non-execution
    repeats the original defect.

    ``trylast`` is load-bearing. pytest's own ``-m`` deselection happens in this
    same hook, so without it this runs against the *unfiltered* list and fires
    on an ordinary ``uv run pytest`` — which deselects the marker and was never
    going to build anything. Verified by
    ``tests/test_image_tier.py::TestTheGuardDoesNotFireOnTheDefaultRun``.
    """
    if not any(item.get_closest_marker("image") for item in items):
        return
    workers = getattr(config.option, "numprocesses", None)
    if workers:
        raise pytest.UsageError(
            f"the image tier must run with -n0, not -n {workers}. Session-scoped "
            "image fixtures are per-xdist-worker, so N workers would race to "
            "`docker build` the same tag and then run against whichever won."
        )


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
    """
    head = subprocess.run(
        ["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    revision = (head.stdout or "").strip() or "nogit"
    digest = hashlib.sha256(dockerfile.read_bytes()).hexdigest()[:12]
    arch = (platform or "native").replace("/", "-")
    return f"istota-test/{prefix}:{revision}-{digest}-{arch}"


def build_image(dockerfile: Path, context: Path, *, platform: str = "", prefix: str) -> BuiltImage:
    """`docker build`, streaming output only on failure.

    Fails the session rather than the individual test: every test in the file
    depends on the image, so reporting the build failure thirty times says
    nothing the first one did not.
    """
    require_docker()
    tag = _tag_for(dockerfile, platform, prefix)
    argv = ["docker", "build", "-f", str(dockerfile), "-t", tag]
    if platform:
        argv += ["--platform", platform]
    argv.append(str(context))

    result = subprocess.run(argv, capture_output=True, text=True, timeout=BUILD_TIMEOUT)
    if result.returncode != 0:
        tail = "\n".join((result.stderr or result.stdout or "").splitlines()[-50:])
        pytest.fail(f"docker build failed for {dockerfile}:\n{tail}", pytrace=False)
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
    """
    if not env:
        return text
    for name, value in env.items():
        if value and _CREDENTIAL_NAME.search(name):
            text = text.replace(value, f"<{name}>")
    return text


def run_in(
    image: BuiltImage,
    argv: list[str],
    *,
    env: dict[str, str] | None = None,
    entrypoint: str = "/bin/sh",
    timeout: int = RUN_TIMEOUT,
) -> subprocess.CompletedProcess:
    """`docker run --rm --entrypoint …`, with credential-scrubbed output.

    The entrypoint override is the default and not a convenience: the image's
    own ENTRYPOINT waits ten minutes for a provisioning flag that a volume-less
    container will never have.
    """
    require_docker()
    cmd = ["docker", "run", "--rm", "--entrypoint", entrypoint]
    if image.platform:
        cmd += ["--platform", image.platform]
    for name, value in (env or {}).items():
        cmd += ["-e", f"{name}={value}"]
    cmd.append(image.tag)
    cmd += argv

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    return subprocess.CompletedProcess(
        args=result.args,
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
def istota_image(platform) -> BuiltImage:
    """The shipped image, built once per session.

    ``ISTOTA_IMAGE_TAG`` skips the build and tests a pre-built tag instead —
    that is how the upgrade driver reuses an image it already built, and how you
    would smoke a published one.
    """
    preexisting = os.environ.get("ISTOTA_IMAGE_TAG")
    if preexisting:
        require_docker()
        return BuiltImage(tag=preexisting, dockerfile=ISTOTA_DOCKERFILE, platform=platform)
    return build_image(ISTOTA_DOCKERFILE, REPO, platform=platform, prefix="istota")


@pytest.fixture(scope="session")
def devbox_image(platform) -> BuiltImage:
    preexisting = os.environ.get("ISTOTA_DEVBOX_IMAGE_TAG")
    if preexisting:
        require_docker()
        return BuiltImage(tag=preexisting, dockerfile=DEVBOX_DOCKERFILE, platform=platform)
    return build_image(
        DEVBOX_DOCKERFILE, REPO / "docker" / "devbox", platform=platform, prefix="devbox"
    )
