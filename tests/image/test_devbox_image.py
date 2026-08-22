"""The devbox image, built natively on whatever architecture you are on.

It used to be amd64-only — a hardcoded `linux-amd64` Go tarball and two
hardcoded amd64 `.deb`s — so on an arm64 machine this file ran only under
`--platform amd64`, an emulated build in the tens of minutes. The practical
result was that the tests for this image were the least-executed in the repo,
run once before a release if at all. ISSUE-280 derived all three assets from
`dpkg --print-architecture` and pinned a checksum per architecture, so the
build is now a few minutes natively and this module no longer skips itself.

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
import re

import pytest

from .conftest import REPO, assert_ok, sh

pytestmark = pytest.mark.image

FORGE_LIB = "/usr/local/lib/istota_forge"
WRAPPER_IN_IMAGE = f"{FORGE_LIB}/istota_forge_cli.py"
SOURCE_OF_TRUTH = REPO / "src" / "istota" / "forge_cli.py"


@pytest.fixture(scope="module")
def devbox_image_under_test(devbox_image):
    """The devbox image, built for whatever architecture the session targets.

    This used to be `amd64_devbox_image`, a gate that skipped the whole module
    unless the session produced an amd64 image, with `getfixturevalue` deferring
    the build until after the skip so a native arm64 run never paid for an
    emulated one. Both are gone with ISSUE-280: the recipe builds natively on
    either architecture, so there is nothing left to gate on and no reason to
    defer. A plain fixture parameter is enough.

    Deliberately *not* reintroducing a skip: a tier that skips itself is how
    this file went unexecuted for months, and it is the failure the whole
    deployment-artifact-verification spec exists to end. If the build cannot
    happen the session-scoped fixture in conftest says so on its own terms.
    """
    return devbox_image


def _dockerfile_arg(dockerfile, name: str) -> str:
    body = dockerfile.read_text()
    match = re.search(rf"^ARG\s+{name}=(\S+)", body, re.M)
    assert match, f"{name} is not pinned in {dockerfile}"
    return match.group(1)


class TestTheForgeBinariesMatchTheMainImage:
    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_the_binary_is_present_and_runs(self, devbox_image_under_test, binary):
        assert_ok(sh(devbox_image_under_test, f"{FORGE_LIB}/{binary} --version"), binary)

    @pytest.mark.parametrize(
        "binary,arg", [("gh", "GH_VERSION"), ("glab", "GLAB_VERSION")]
    )
    def test_the_installed_version_matches_this_images_pin(
        self, devbox_image_under_test, binary, arg
    ):
        pinned = _dockerfile_arg(devbox_image_under_test.dockerfile, arg)
        out = assert_ok(sh(devbox_image_under_test, f"{FORGE_LIB}/{binary} --version"), binary)

        assert pinned in out, f"expected {pinned} in {out!r}"

    @pytest.mark.parametrize(
        "binary,arg", [("gh", "GH_VERSION"), ("glab", "GLAB_VERSION")]
    )
    def test_the_two_images_ship_the_same_version(self, devbox_image_under_test, binary, arg):
        # A drift here means a task behaves differently depending on which
        # container it lands in, which is the hardest kind of bug to reproduce.
        main = _dockerfile_arg(REPO / "docker" / "istota" / "Dockerfile", arg)
        devbox = _dockerfile_arg(devbox_image_under_test.dockerfile, arg)

        assert main == devbox, (
            f"{binary}: the main image pins {main} and the devbox image pins "
            f"{devbox}. scripts/sync-devbox-lib.sh does not cover the ARGs."
        )
        assert main in assert_ok(sh(devbox_image_under_test, f"{FORGE_LIB}/{binary} --version"), binary)


class TestTheWrapperCopyIsInSync:
    def test_the_image_copy_is_byte_identical_to_the_source(self, devbox_image_under_test):
        # The devbox build context is docker/devbox/, so it cannot COPY from
        # src/ — the copy exists for that reason alone, and a copy with no check
        # is a copy that drifts.
        expected = hashlib.sha256(SOURCE_OF_TRUTH.read_bytes()).hexdigest()
        result = sh(devbox_image_under_test, f"sha256sum {WRAPPER_IN_IMAGE}")
        actual = assert_ok(result, f"sha256sum {WRAPPER_IN_IMAGE}").split()[0]

        assert actual == expected, (
            "docker/devbox/lib/istota_forge_cli.py has drifted from "
            "src/istota/forge_cli.py; run scripts/sync-devbox-lib.sh"
        )

    # The "is the *repo* copy in sync" half deliberately lives elsewhere:
    # `tests/test_forge_cli.py` already asserts
    # src/istota/forge_cli.py == docker/devbox/lib/istota_forge_cli.py, in the
    # default suite, with no Docker at all. A copy of it here would sit behind
    # the `image` marker and a Docker daemon, so it would run far less often
    # and could only fail in a state that cheaper test had already caught.
    # (This used to say "and an amd64 build", which was the stronger half of
    # the argument until ISSUE-280 made the image build natively. The
    # conclusion is unchanged; the reason is now just the marker.)


class TestTheWrapperIsWhatResolvesByName:
    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_the_name_resolves_to_the_wrapper(self, devbox_image_under_test, binary):
        result = sh(devbox_image_under_test, f"command -v {binary}")
        resolved = assert_ok(result, f"command -v {binary}").strip()

        assert resolved, f"{binary} does not resolve by name at all"
        assert not resolved.startswith(FORGE_LIB), (
            f"{binary} resolves straight to the real binary at {resolved}; "
            "the deny policy and the token injection are both bypassed"
        )

    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_what_resolves_is_the_python_wrapper_not_a_real_binary(
        self, devbox_image_under_test, binary
    ):
        result = sh(devbox_image_under_test, f"head -c 200 \"$(command -v {binary})\"")
        head = assert_ok(result, f"reading the {binary} on PATH")

        assert "python" in head.lower(), (
            f"the {binary} on PATH is not the python wrapper:\n{head!r}"
        )

    @pytest.mark.parametrize("binary", ["gh", "glab"])
    def test_the_real_binary_is_off_path(self, devbox_image_under_test, binary):
        # The positive half is the test above; without it this passes on an
        # image that installs nothing.
        result = sh(
            devbox_image_under_test,
            f"test -x {FORGE_LIB}/{binary} && command -v {binary}",
        )
        resolved = assert_ok(result, f"{binary}").strip()

        assert resolved != f"{FORGE_LIB}/{binary}"
