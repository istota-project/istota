#!/bin/bash
# Prove the image tier can see a broken artifact.
#
# Every assertion in tests/image/ is a claim about a container. Nothing inside
# that suite can tell a working assertion from one that matches nothing — and
# that is not hypothetical: the first version filtered `doctor --json` on
# "FAIL" while the JSON emits lowercase, so four Group A tests passed on an
# image with no forge binaries in it. This script is what caught it, and it is
# Stage 5's acceptance criterion in the deployment-artifact-verification spec.
#
# It builds docker/test/Dockerfile.no-forge — the real image with
# /usr/local/lib/istota_forge removed, reproducing ISSUE-263 — and runs the two
# groups that must fail against it. **A clean run here is the failure**: it
# means the tier would pass on the bug it exists to catch.
#
# It covers both halves of the tier:
#
#   * the istota image, via docker/test/Dockerfile.no-forge;
#   * the devbox image, via ten controls, because that file asserts that many
#     separable things and no single broken image reaches all of them. The
#     forge-less image alone left four of the original thirteen assertions
#     green, since /usr/local/bin/gh is a *copy* of the wrapper rather than a
#     symlink into the directory being removed — the fourth control is what
#     closed those. The last six arrived with the exec transport: the uid the
#     container runs as, the ownership of /home/dev, the vendored protocol
#     module, whether the transport comes *up*, the /home/dev repair, and the
#     absence of /workspace. Read each Dockerfile.devbox-* file's note about
#     what it deliberately does *not* break — several turn a neighbour's
#     assertion red for the wrong reason, which is exactly what a second
#     control exists to separate.
#
# Six assertions in the devbox file have no control, deliberately, and each
# fails closed: four positive existence checks (`test -x` against a named
# absolute path, `python3 -c 'import …'` against a named directory, a `Cmd`
# compared to an exact list, and `command -v uv` compared to the directory the
# home volume mounts over), the graceful-stop assertion (a log line only a
# graceful shutdown writes, plus an unlinked socket), and the unconfigured hold
# (a process still alive and a message naming two literal variables).
#
#   scripts/test-image-negative-control.sh [amd64]
#
# No arrays anywhere: macOS ships bash 3.2, where `"${empty[@]}"` under `set -u`
# is fatal, and this script's whole audience is a developer machine.
set -euo pipefail

cd "$(dirname "$0")/.."

platform="${1:-}"
control_tag="istota-test/no-forge:control"

# The tier's own tag scheme is the authority. Reproducing it in shell would be a
# second copy of a rule that already exists, and it would drift.
base_tag="$(
    ISTOTA_TEST_PLATFORM="$platform" uv run python -c '
import sys
# The repo root, not `tests/`: `tests.image.conftest` reads `resolve_platform`
# from the rootdir conftest by relative import, and a flat `image.conftest`
# would be a top-level package with nothing above it to import from.
sys.path.insert(0, ".")
from tests.image.conftest import _tag_for, ISTOTA_DOCKERFILE, resolve_platform


class _Config:
    def getoption(self, name):
        return None


print(_tag_for(ISTOTA_DOCKERFILE, resolve_platform(_Config()), "istota"))
'
)"

echo "[control] base image: $base_tag"
if ! docker image inspect "$base_tag" >/dev/null 2>&1; then
    echo "[control] not built yet — run \`uv run pytest -m image -n0\` first." >&2
    exit 2
fi

echo "[control] building the forge-less control…"
docker build -q -f docker/test/Dockerfile.no-forge \
    --build-arg "BASE=$base_tag" -t "$control_tag" docker/test >/dev/null

echo "[control] running the groups that must fail…"
set +e
if [ -n "$platform" ]; then
    ISTOTA_IMAGE_TAG="$control_tag" uv run pytest -m image -n0 -q --no-header \
        --platform "$platform" \
        tests/image/test_istota_image.py::TestGroupATheDoctorUmbrella \
        tests/image/test_istota_image.py::TestGroupBTheForgeBinaries
else
    ISTOTA_IMAGE_TAG="$control_tag" uv run pytest -m image -n0 -q --no-header \
        tests/image/test_istota_image.py::TestGroupATheDoctorUmbrella \
        tests/image/test_istota_image.py::TestGroupBTheForgeBinaries
fi
status=$?
set -e

echo
if [ "$status" -eq 0 ]; then
    echo "[control] FAILED: the tier passed on an image with no forge binaries."
    echo "[control] Some assertion is matching nothing. That is the defect."
    exit 1
fi

echo "[control] OK: the istota tier failed on the broken image, as it must."
echo "[control] Read the failures above and confirm they name the missing path."


# --------------------------------------------------------------------------
# The devbox half.

devbox_base_tag="$(
    ISTOTA_TEST_PLATFORM="$platform" uv run python -c '
import os, sys
# Spelled exactly as the istota half above, and for the reason stated there:
# `tests/image/conftest.py` reaches `resolve_platform` in the rootdir conftest
# by relative import, so a flat `image.conftest` is a top-level package with
# nothing above it to import from. This half carried the flat spelling and
# raised ImportError on every run — and with `set -e` on an assignment from a
# command substitution, that took the script down before a single devbox
# control was built, leaving a run that read as clean and complete.
sys.path.insert(0, ".")
from tests.image.conftest import _tag_for, DEVBOX_DOCKERFILE, resolve_platform


class _Config:
    def getoption(self, name):
        return None


print(_tag_for(DEVBOX_DOCKERFILE, resolve_platform(_Config()), "devbox"))
'
)"

echo
echo "[control] devbox base image: $devbox_base_tag"
if ! docker image inspect "$devbox_base_tag" >/dev/null 2>&1; then
    echo "[control] not built yet — run \`uv run pytest -m image -n0\` first." >&2
    exit 2
fi

# One control per claim the devbox file makes, and each names the exact
# parametrized node ids it must turn red.
#
# Naming the *class* and checking only the exit status is not enough, and that
# is not hypothetical: the first cut did exactly that, and control 3 passed on
# a UnicodeDecodeError raised inside `subprocess` before its assertion ran.
# Red for the right image, for the wrong reason — indistinguishable from a
# working assertion, and precisely what these controls exist to tell apart.
# So the expected FAILED lines have to appear in pytest's own summary.
#
# Tags carry the base image's revision component. `tests/image/conftest.py`
# reasons at length about why a fixed tag is unsafe when work runs in parallel
# git worktrees — a second `docker build -t <same tag>` moves the tag out from
# under a run in progress — and this script holds a tag across a build plus a
# full pytest invocation, four times over.
control_suffix="${devbox_base_tag##*:}"

run_devbox_control() {
    control_name="$1"
    control_dockerfile="$2"
    control_expect="$3"
    shift 3
    # Remaining args are the node ids that must fail. Held in "$@" rather than
    # an array: macOS ships bash 3.2.

    tag="istota-test/${control_name}:${control_suffix}"
    docker build -q -f "docker/test/${control_dockerfile}" \
        --build-arg "BASE=$devbox_base_tag" -t "$tag" docker/test >/dev/null

    require_devbox_failures "$control_name" "$tag" "$control_expect" "$@"
}

# Run the named node ids against a control image and require every one of them
# to appear on a FAILED line.
#
# Split out from `run_devbox_control` because one control is not a perturbation
# of the built image: `devbox-wrong-uid` builds the real recipe with different
# build args, which is the only way to test that the args work at all. A
# `FROM ${BASE}` image with `usermod -u` applied would turn the same assertion
# red while proving nothing about `ARG DEV_UID`.
require_devbox_failures() {
    control_name="$1"
    tag="$2"
    control_expect="$3"
    shift 3

    echo
    echo "[control] devbox/${control_name}: ${control_expect}"

    # The verdict comes from the captured output, not from `$?` — deliberately.
    # A pipeline reports its *last* command's status, so `| tee` would hand
    # back tee's 0 on a failed run. That is fine here and nowhere else: what
    # this needs to know is which node ids failed, which the status cannot say.
    control_out="$(mktemp)"
    set +e
    if [ -n "$platform" ]; then
        ISTOTA_DEVBOX_IMAGE_TAG="$tag" uv run pytest -m image -n0 -q --no-header \
            --platform "$platform" "$@" 2>&1 | tee "$control_out"
    else
        ISTOTA_DEVBOX_IMAGE_TAG="$tag" uv run pytest -m image -n0 -q --no-header \
            "$@" 2>&1 | tee "$control_out"
    fi
    set -e

    # Every named node id must appear on a FAILED line. `grep -F` because a
    # parametrized id contains `[gh]`, which is a bracket expression to a
    # regex engine and a glob to the shell.
    control_missing=""
    for node in "$@"; do
        if ! grep -Fq "FAILED ${node}" "$control_out"; then
            control_missing="${control_missing} ${node}"
        fi
    done
    rm -f "$control_out"

    if [ -n "$control_missing" ]; then
        echo "[control] FAILED: devbox/${control_name} did not turn these red:"
        for node in $control_missing; do echo "[control]   ${node}"; done
        echo "[control] Expected: ${control_expect}"
        echo "[control] Those assertions are matching nothing, or they failed"
        echo "[control] somewhere other than where they were supposed to."
        exit 1
    fi
    echo "[control] OK: devbox/${control_name} turned every named assertion red."
}

DEVBOX_TESTS="tests/image/test_devbox_image.py"

run_devbox_control \
    "devbox-no-forge" \
    "Dockerfile.devbox-no-forge" \
    "the forge binaries are gone, so the version assertions cannot pass" \
    "${DEVBOX_TESTS}::TestTheForgeBinariesMatchTheMainImage::test_the_binary_is_present_and_runs[gh]" \
    "${DEVBOX_TESTS}::TestTheForgeBinariesMatchTheMainImage::test_the_binary_is_present_and_runs[glab]" \
    "${DEVBOX_TESTS}::TestTheForgeBinariesMatchTheMainImage::test_the_installed_version_matches_this_images_pin[gh-GH_VERSION]" \
    "${DEVBOX_TESTS}::TestTheForgeBinariesMatchTheMainImage::test_the_installed_version_matches_this_images_pin[glab-GLAB_VERSION]" \
    "${DEVBOX_TESTS}::TestTheForgeBinariesMatchTheMainImage::test_the_two_images_ship_the_same_version[gh-GH_VERSION]" \
    "${DEVBOX_TESTS}::TestTheForgeBinariesMatchTheMainImage::test_the_two_images_ship_the_same_version[glab-GLAB_VERSION]"

run_devbox_control \
    "devbox-stale-wrapper" \
    "Dockerfile.devbox-stale-wrapper" \
    "the wrapper is present and readable but its bytes differ from src/" \
    "${DEVBOX_TESTS}::TestTheWrapperCopyIsInSync::test_the_image_copy_is_byte_identical_to_the_source"

run_devbox_control \
    "devbox-real-binary-on-path" \
    "Dockerfile.devbox-real-binary-on-path" \
    "gh and glab on PATH are the real CLIs, so what resolves is not the wrapper" \
    "${DEVBOX_TESTS}::TestTheWrapperIsWhatResolvesByName::test_what_resolves_is_the_python_wrapper_not_a_real_binary[gh]" \
    "${DEVBOX_TESTS}::TestTheWrapperIsWhatResolvesByName::test_what_resolves_is_the_python_wrapper_not_a_real_binary[glab]"

# The fourth exists because the first three left four of the thirteen
# assertions untouched — two with no control at all, and two that only ever
# went red through a guard raising rather than through their own comparison.
run_devbox_control \
    "devbox-forge-dir-on-path" \
    "Dockerfile.devbox-forge-dir-on-path" \
    "the forge dir is on PATH, so the name resolves ahead of the wrapper" \
    "${DEVBOX_TESTS}::TestTheWrapperIsWhatResolvesByName::test_the_name_resolves_to_the_wrapper[gh]" \
    "${DEVBOX_TESTS}::TestTheWrapperIsWhatResolvesByName::test_the_name_resolves_to_the_wrapper[glab]" \
    "${DEVBOX_TESTS}::TestTheWrapperIsWhatResolvesByName::test_the_real_binary_is_off_path[gh]" \
    "${DEVBOX_TESTS}::TestTheWrapperIsWhatResolvesByName::test_the_real_binary_is_off_path[glab]"


# --------------------------------------------------------------------------
# The exec transport and the uid, added with the devbox-as-the-development-
# container work.

# The one control that is a real build rather than a perturbation. `DEV_UID`
# and `DEV_GID` exist so the deploy can pass the daemon's own uid, and the only
# way to know the args work is to use them; a `FROM ${BASE}` image with
# `usermod -u` applied would turn the assertion red while telling you nothing
# about `ARG DEV_UID`.
#
# It costs a build of the real recipe from the `useradd` layer down, which is
# uv, rustup and the two forge CLIs — about half a minute on a warm cache, and
# it needs the network on a cold one. The layers above it (apt, Node, Go) are
# shared with the base build that already happened.
#
# **It is also the one that needs `--platform`.** Every other control is
# `FROM ${BASE}` and inherits the base image's architecture. This one starts
# from `debian:trixie-slim`, which is multi-arch, so without the flag an
# `amd64` run builds a native image and hands it to a pytest run that then adds
# `--platform linux/amd64` to `docker run`. The control still goes red — with
# `exec format error`, which is the "red for the right image, for the wrong
# reason" failure this whole file exists to tell apart.
echo
echo "[control] devbox/devbox-wrong-uid: building the real recipe with DEV_UID=1234…"
wrong_uid_tag="istota-test/devbox-wrong-uid:${control_suffix}"
if [ -n "$platform" ]; then
    # `amd64` is what a person types at this script and `linux/amd64` is what
    # Docker wants; `resolve_platform` normalizes for the pytest side and this
    # is the same rule for the build side. Getting it wrong builds natively
    # while the flag claims otherwise, which is the failure being avoided.
    case "$platform" in
        */*) docker_platform="$platform" ;;
        *) docker_platform="linux/${platform}" ;;
    esac
    docker build -q -f docker/devbox/Dockerfile \
        --platform "$docker_platform" \
        --build-arg DEV_UID=1234 --build-arg DEV_GID=1234 \
        -t "$wrong_uid_tag" docker/devbox >/dev/null
else
    docker build -q -f docker/devbox/Dockerfile \
        --build-arg DEV_UID=1234 --build-arg DEV_GID=1234 \
        -t "$wrong_uid_tag" docker/devbox >/dev/null
fi

require_devbox_failures \
    "devbox-wrong-uid" \
    "$wrong_uid_tag" \
    "dev is 1234, so a build with no args did not reproduce uid 1000" \
    "${DEVBOX_TESTS}::TestTheDevUidBuildArgs::test_the_dev_account_has_the_default_uid_and_gid"

run_devbox_control \
    "devbox-home-owned-by-a-stranger" \
    "Dockerfile.devbox-home-owned-by-a-stranger" \
    "/home/dev belongs to an account that does not exist in the image" \
    "${DEVBOX_TESTS}::TestTheDevUidBuildArgs::test_the_home_directory_belongs_to_the_dev_account"

run_devbox_control \
    "devbox-stale-exec-protocol" \
    "Dockerfile.devbox-stale-exec-protocol" \
    "the vendored protocol module is present and imports, but its bytes differ" \
    "${DEVBOX_TESTS}::TestTheExecTransportIsInstalled::test_the_vendored_protocol_copy_is_byte_identical_to_the_source"

# The transport tests are the ones where an assertion can pass without the
# mechanism, so this is the control that matters most of the five. Note what it
# is *not* asked to prove: the /home/dev repair test also probes the wire and
# goes red here, for the wrong reason, which is why it is not named and has a
# control of its own below.
run_devbox_control \
    "devbox-no-exec-server" \
    "Dockerfile.devbox-no-exec-server" \
    "the supervisor runs but the server is gone, so nothing ever binds" \
    "${DEVBOX_TESTS}::TestTheExecTransportIsInstalled::test_the_exec_server_is_installed_and_executable" \
    "${DEVBOX_TESTS}::TestTheSupervisorStartsTheTransport::test_the_supervisor_brings_the_transport_up" \
    "${DEVBOX_TESTS}::TestTheSupervisorStartsTheTransport::test_the_supervisor_restarts_the_server_after_it_dies"

run_devbox_control \
    "devbox-no-home-repair" \
    "Dockerfile.devbox-no-home-repair" \
    "the transport comes up normally and never chowns /home/dev" \
    "${DEVBOX_TESTS}::TestTheSupervisorStartsTheTransport::test_the_supervisor_repairs_a_home_directory_with_the_wrong_owner"

run_devbox_control \
    "devbox-workspace-present" \
    "Dockerfile.devbox-workspace-present" \
    "/workspace is back, which an absence assertion can only see if it works" \
    "${DEVBOX_TESTS}::TestTheWorkspaceTmpfsIsGone::test_the_image_has_no_workspace_directory"

echo
echo "[control] OK: both halves of the image tier can see a broken artifact,"
echo "[control] and every assertion in the devbox file that could pass"
echo "[control] vacuously has one that reaches it."
echo "[control] The five without one all fail closed; the header says which."
