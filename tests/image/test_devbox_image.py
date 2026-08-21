"""The devbox image, which is amd64-only and therefore has no fast local witness.

Its Go toolchain is a hardcoded `linux-amd64` tarball
(`docker/devbox/Dockerfile:31`) and both forge `.deb`s are hardcoded amd64
(`:87`, `:92`), so on an arm64 development machine this file runs only under
`--platform amd64` — an emulated build in the tens of minutes. Stated plainly
rather than papered over: this is the one tier in the spec you will not run
casually, and it is skipped by default on this hardware.

What it asserts is the two properties nothing else can see at image level:

  * the two images agree on the forge versions they ship. `tests/
    test_docker_forge_clis.py` already asserts the two Dockerfiles agree
    *textually*; this asserts the two binaries agree, which is the claim the
    textual test is a proxy for.
  * `docker/devbox/lib/istota_forge_cli.py` in the image is byte-identical to
    `src/istota/forge_cli.py` in the repo. That is the property
    `scripts/sync-devbox-lib.sh` exists to maintain, and it is currently checked
    by nothing at image level — a stale copy means the devbox enforces a
    different deny policy than the sandbox does, silently.
"""

from __future__ import annotations

import hashlib
import platform
import re

import pytest

from .conftest import REPO, assert_ok, sh

pytestmark = pytest.mark.image

FORGE_LIB = "/usr/local/lib/istota_forge"
WRAPPER_IN_IMAGE = f"{FORGE_LIB}/istota_forge_cli.py"
SOURCE_OF_TRUTH = REPO / "src" / "istota" / "forge_cli.py"


def _is_amd64(platform_option: str) -> bool:
    """Whether this session will produce an amd64 image.

    Either the opt-in flag was passed, or the host is already amd64.
    """
    if platform_option:
        return platform_option.endswith("amd64")
    return platform.machine().lower() in ("x86_64", "amd64")


@pytest.fixture(scope="module", autouse=True)
def _amd64_only(platform):
    if not _is_amd64(platform):
        pytest.skip(
            "the devbox image is amd64-only (hardcoded linux-amd64 Go toolchain "
            "and .debs); run with --platform amd64 to build it under emulation"
        )


def _dockerfile_arg(dockerfile, name: str) -> str:
    body = dockerfile.read_text()
    match = re.search(rf"^ARG\s+{name}=(\S+)", body, re.M)
    assert match, f"{name} is not pinned in {dockerfile}"
    return match.group(1)


class TestTheForgeBinariesMatchTheMainImage:
    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_the_binary_is_present_and_runs(self, devbox_image, binary):
        assert_ok(sh(devbox_image, f"{FORGE_LIB}/{binary} --version"), binary)

    @pytest.mark.parametrize(
        "binary,arg", [("gh", "GH_VERSION"), ("glab", "GLAB_VERSION")]
    )
    def test_the_installed_version_matches_this_images_pin(
        self, devbox_image, binary, arg
    ):
        pinned = _dockerfile_arg(devbox_image.dockerfile, arg)
        out = assert_ok(sh(devbox_image, f"{FORGE_LIB}/{binary} --version"), binary)

        assert pinned in out, f"expected {pinned} in {out!r}"

    @pytest.mark.parametrize(
        "binary,arg", [("gh", "GH_VERSION"), ("glab", "GLAB_VERSION")]
    )
    def test_the_two_images_ship_the_same_version(self, devbox_image, binary, arg):
        # A drift here means a task behaves differently depending on which
        # container it lands in, which is the hardest kind of bug to reproduce.
        main = _dockerfile_arg(REPO / "docker" / "istota" / "Dockerfile", arg)
        devbox = _dockerfile_arg(devbox_image.dockerfile, arg)

        assert main == devbox, (
            f"{binary}: the main image pins {main} and the devbox image pins "
            f"{devbox}. scripts/sync-devbox-lib.sh does not cover the ARGs."
        )
        assert main in assert_ok(sh(devbox_image, f"{FORGE_LIB}/{binary} --version"), binary)


class TestTheWrapperCopyIsInSync:
    def test_the_image_copy_is_byte_identical_to_the_source(self, devbox_image):
        # The devbox build context is docker/devbox/, so it cannot COPY from
        # src/ — the copy exists for that reason alone, and a copy with no check
        # is a copy that drifts.
        expected = hashlib.sha256(SOURCE_OF_TRUTH.read_bytes()).hexdigest()
        result = sh(devbox_image, f"sha256sum {WRAPPER_IN_IMAGE}")
        actual = assert_ok(result, f"sha256sum {WRAPPER_IN_IMAGE}").split()[0]

        assert actual == expected, (
            "docker/devbox/lib/istota_forge_cli.py has drifted from "
            "src/istota/forge_cli.py; run scripts/sync-devbox-lib.sh"
        )

    def test_the_repo_copy_is_also_in_sync(self):
        # Cheap and worth having separately: this one distinguishes "the image
        # is stale" from "the repo copy is stale", which the assertion above
        # cannot tell apart.
        vendored = REPO / "docker" / "devbox" / "lib" / "istota_forge_cli.py"

        assert vendored.read_bytes() == SOURCE_OF_TRUTH.read_bytes(), (
            "the vendored copy has drifted; run scripts/sync-devbox-lib.sh"
        )


class TestTheWrapperIsWhatResolvesByName:
    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_the_name_resolves_to_the_wrapper(self, devbox_image, binary):
        result = sh(devbox_image, f"command -v {binary}")
        resolved = assert_ok(result, f"command -v {binary}").strip()

        assert resolved, f"{binary} does not resolve by name at all"
        assert not resolved.startswith(FORGE_LIB), (
            f"{binary} resolves straight to the real binary at {resolved}; "
            "the deny policy and the token injection are both bypassed"
        )

    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_what_resolves_is_the_python_wrapper_not_a_real_binary(
        self, devbox_image, binary
    ):
        result = sh(devbox_image, f"head -c 200 \"$(command -v {binary})\"")
        head = assert_ok(result, f"reading the {binary} on PATH")

        assert "python" in head.lower(), (
            f"the {binary} on PATH is not the python wrapper:\n{head!r}"
        )

    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_the_real_binary_is_off_path(self, devbox_image, binary):
        # The positive half is the test above; without it this passes on an
        # image that installs nothing.
        result = sh(
            devbox_image,
            f"test -x {FORGE_LIB}/{binary} && command -v {binary}",
        )
        resolved = assert_ok(result, f"{binary}").strip()

        assert resolved != f"{FORGE_LIB}/{binary}"
