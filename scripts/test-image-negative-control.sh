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
#   scripts/test-image-negative-control.sh run-contract
#   scripts/test-image-negative-control.sh secrets
#   scripts/test-image-negative-control.sh rendered-config
#
# The second form runs only the third half, the istota container's run
# contract (the one-deployment-shape spec's parity rows 1, 2, 3, 4, 5, 6, 8,
# 16 and 17).
# Its witnesses are smoke tests, because a run contract is a property of a
# running container rather than of an image, so each control there breaks one
# line of the contract (a compose overlay on the lean stack, or an image with
# one step of the root phase removed) and names the smoke node ids that must
# go red. See the half's own header below.
#
# The third form runs only parity row 12's control: the shipped compose file's
# `istota` service with one credential passed through `environment:`, which
# `docker inspect` then shows (tests/image/test_secrets_contract.py). The
# fourth runs the upgrade case that boots the new entrypoint over a config an
# old release rendered, against an image whose entrypoint appends to that file.
#
# No arrays anywhere: macOS ships bash 3.2, where `"${empty[@]}"` under `set -u`
# is fatal, and this script's whole audience is a developer machine.
set -euo pipefail

cd "$(dirname "$0")/.."

platform="${1:-}"
only_run_contract=""
only_secrets=""
if [ "$platform" = "run-contract" ]; then
    only_run_contract=1
    platform=""
fi
if [ "$platform" = "secrets" ]; then
    only_secrets=1
    platform=""
fi
only_rendered_config=""
if [ "$platform" = "rendered-config" ]; then
    only_rendered_config=1
    platform=""
fi
control_tag="istota-test/no-forge:control"

# --------------------------------------------------------------------------
# The run-contract half. Defined first so the second form can run it alone;
# called at the end of a full run.
#
# Each control breaks one thing and names the node ids that must turn red. A
# control that stops the stack from booting at all (the root phase refusing on
# purpose) reports its witnesses as ERROR rather than FAILED, so for those the
# refusal's own words must also appear in the output: an error for any other
# reason is the wrong red.

RUN_CONTRACT_TESTS="tests/smoke/test_run_contract.py"
RUN_CONTRACT_CONTROLS="docker/test/run-contract-controls"

require_smoke_failures() {
    control_name="$1"
    control_overlays="$2"
    control_expect="$3"
    control_must_say="$4"
    shift 4

    echo
    echo "[control] run-contract/${control_name}: ${control_expect}"
    control_out="$(mktemp)"
    set +e
    ISTOTA_TESTBED_CONTROL_OVERLAYS="$control_overlays" \
        uv run pytest -m smoke -n0 -q --no-header -p no:randomly "$@" 2>&1 | tee "$control_out"
    set -e

    # FAILED only, unless the control is a refusal to boot: a stack that did
    # not come up for some other reason errors every witness, and that red
    # says nothing about the line the control broke.
    control_missing=""
    for node in "$@"; do
        if grep -Fq "FAILED ${node}" "$control_out"; then
            continue
        fi
        if [ -n "$control_must_say" ] && grep -Fq "ERROR ${node}" "$control_out"; then
            continue
        fi
        control_missing="${control_missing} ${node}"
    done
    if [ -n "$control_must_say" ] && ! grep -Fq "$control_must_say" "$control_out"; then
        control_missing="${control_missing} (output never said: ${control_must_say})"
    fi
    rm -f "$control_out"

    if [ -n "$control_missing" ]; then
        echo "[control] FAILED: run-contract/${control_name} did not turn these red:"
        for node in $control_missing; do echo "[control]   ${node}"; done
        echo "[control] Expected: ${control_expect}"
        exit 1
    fi
    echo "[control] OK: run-contract/${control_name} turned every named witness red."
}

run_contract_half() {
    # The lean image the smoke tier builds, by the same rule it uses.
    lean_tag="$(uv run python -c '
import sys
sys.path.insert(0, ".")
from tests.conftest import lean_image_tag
print(lean_image_tag())
')"
    echo
    echo "[control] run-contract: lean image ${lean_tag}"
    if ! docker image inspect "$lean_tag" >/dev/null 2>&1; then
        echo "[control] not built yet — run \`uv run pytest -m smoke -n0 ${RUN_CONTRACT_TESTS}\` first." >&2
        exit 2
    fi
    suffix="${lean_tag##*:}"
    scratch="$(mktemp -d)"

    # An image with one step of the root phase removed, run by the lean stack.
    image_overlay() {
        printf 'services:\n  istota:\n    build: !reset null\n    image: %s\n' "$1" > "$scratch/$2.yml"
        echo "$scratch/$2.yml"
    }

    # Row 5. The daemon started without the drop is uid 0 with the cap_add
    # set and no CAP_DAC_OVERRIDE, so it cannot open the state it does not own
    # and the stack never comes up: the run contract fails closed rather than
    # running a root daemon. The uid and capability assertion itself is turned
    # red by the image half's no-drop control above.
    docker build -q -f docker/test/Dockerfile.no-daemon-drop \
        --build-arg "BASE=$lean_tag" -t "istota-test/no-daemon-drop:$suffix" docker/test >/dev/null
    require_smoke_failures \
        "no-daemon-drop" \
        "$(image_overlay "istota-test/no-daemon-drop:$suffix" no-daemon-drop)" \
        "row 5: the root phase execs the daemon without the drop" \
        "sqlite3.OperationalError: unable to open database file" \
        "${RUN_CONTRACT_TESTS}::TestTheDaemonIsUnprivileged::test_the_daemon_runs_as_10001_with_every_capability_set_empty" \
        "${RUN_CONTRACT_TESTS}::TestTheDaemonIsUnprivileged::test_nothing_the_daemon_wrote_is_owned_by_root"

    docker build -q -f docker/test/Dockerfile.no-cgroup-remount \
        --build-arg "BASE=$lean_tag" -t "istota-test/no-cgroup-remount:$suffix" docker/test >/dev/null
    require_smoke_failures \
        "no-cgroup-remount" \
        "$(image_overlay "istota-test/no-cgroup-remount:$suffix" no-cgroup-remount)" \
        "row 4: the root phase skips the remount, so nothing is delegated" \
        "" \
        "${RUN_CONTRACT_TESTS}::TestATaskIsInItsOwnCgroup::test_doctor_reports_the_delegated_root_ok" \
        "${RUN_CONTRACT_TESTS}::TestATaskIsInItsOwnCgroup::test_a_task_is_placed_limited_and_killed_past_its_limit"

    require_smoke_failures \
        "cgroup-host-bind" \
        "$PWD/${RUN_CONTRACT_CONTROLS}/cgroup-host-bind.yml" \
        "row 4: the host's cgroup tree is bound in, and the root phase refuses to start" \
        "REFUSE: /sys/fs/cgroup is cgroup2 rooted at /../.." \
        "${RUN_CONTRACT_TESTS}::TestATaskIsInItsOwnCgroup::test_doctor_reports_the_delegated_root_ok"

    require_smoke_failures \
        "read-write-root" \
        "$PWD/${RUN_CONTRACT_CONTROLS}/read-write-root.yml" \
        "row 6: no read_only, so a uid-0 write succeeds and the root mount is rw" \
        "" \
        "${RUN_CONTRACT_TESTS}::TestTheRootFilesystemIsReadOnly::test_a_root_write_is_refused_by_the_mount[/app]" \
        "${RUN_CONTRACT_TESTS}::TestTheRootFilesystemIsReadOnly::test_a_root_write_is_refused_by_the_mount[/usr]" \
        "${RUN_CONTRACT_TESTS}::TestTheRootFilesystemIsReadOnly::test_a_root_write_is_refused_by_the_mount[/etc]" \
        "${RUN_CONTRACT_TESTS}::TestTheRootFilesystemIsReadOnly::test_the_daemons_root_mount_is_read_only"

    require_smoke_failures \
        "docker-socket" \
        "$PWD/${RUN_CONTRACT_CONTROLS}/docker-socket.yml" \
        "row 8: the host's Docker socket is bound into the container" \
        "" \
        "${RUN_CONTRACT_TESTS}::TestNoDockerApi::test_no_docker_socket_at_any_path" \
        "${RUN_CONTRACT_TESTS}::TestNoDockerApi::test_nothing_is_mounted_from_a_docker_socket"

    require_smoke_failures \
        "seccomp-unconfined" \
        "$PWD/${RUN_CONTRACT_CONTROLS}/seccomp-unconfined.yml" \
        "row 16: no syscall filter, so the denied calls reach the kernel" \
        "" \
        "${RUN_CONTRACT_TESTS}::TestATasksSyscallSurface::test_the_denied_calls_are_eperm_inside_a_live_sandbox"

    require_smoke_failures \
        "seccomp-docker-default" \
        "$PWD/${RUN_CONTRACT_CONTROLS}/seccomp-docker-default.yml" \
        "row 1: Docker's default profile, so bwrap fails and the root phase refuses to start" \
        "REFUSE: bubblewrap cannot build the sandbox's namespace" \
        "tests/smoke/test_sandbox_in_stack.py::TestTheDatabaseMasks::test_the_database_directory_is_an_empty_read_only_tmpfs" \
        "tests/smoke/test_sandbox_repos_isolation.py::TestAnotherUsersSubtree::test_it_is_not_in_the_namespace_at_all" \
        "tests/smoke/test_sandbox_shared_room.py::TestAGuestsTurn::test_it_reaches_no_workspace_and_no_group"

    # Rows 2 and 3 share a witness: one task's network and its two proxy
    # sockets. Each control must turn it red with its own row's message.
    network_node="${RUN_CONTRACT_TESTS}::TestATasksNetworkAndItsProxies::test_the_proxies_answer_the_task_and_refuse_everyone_else"
    docker build -q -f docker/test/Dockerfile.no-unshare-net \
        --build-arg "BASE=$lean_tag" -t "istota-test/no-unshare-net:$suffix" docker/test >/dev/null
    require_smoke_failures \
        "no-unshare-net" \
        "$(image_overlay "istota-test/no-unshare-net:$suffix" no-unshare-net)" \
        "row 2: the mount plan drops --unshare-net, so a task reaches a host past the proxy" \
        "row 2: the task reached a host without the proxy" \
        "$network_node"

    docker build -q -f docker/test/Dockerfile.every-pid-a-root \
        --build-arg "BASE=$lean_tag" -t "istota-test/every-pid-a-root:$suffix" docker/test >/dev/null
    require_smoke_failures \
        "every-pid-a-root" \
        "$(image_overlay "istota-test/every-pid-a-root:$suffix" every-pid-a-root)" \
        "row 3: every pid counts as a task root, so the proxies serve an outsider" \
        "row 3: the skill proxy served a process outside the task" \
        "$network_node"

    require_smoke_failures \
        "browser-on-default-network" \
        "$PWD/${RUN_CONTRACT_CONTROLS}/browser-on-default-network.yml" \
        "row 17: the browser shares the default network with web, nginx and signaling" \
        "row 17: the browser container reached" \
        "${RUN_CONTRACT_TESTS}::TestTheBrowserIsOnItsOwnNetwork::test_the_browser_reaches_nothing_the_daemon_reaches"

    rm -rf "$scratch"
    echo
    echo "[control] OK: every run-contract control turned its witnesses red."
}

# --------------------------------------------------------------------------
# Row 12. The witness boots the shipped compose file's `istota` service from
# the image the tier built; the control adds one overlay passing a credential
# through `environment:`, and the inspect assertion must go red. The other
# three assertions are about files and stay green, which is what tells this
# red from a stack that did not come up.

SECRETS_TESTS="tests/image/test_secrets_contract.py"

secrets_half() {
    echo
    echo "[control] secrets: a credential in compose's environment"
    secrets_out="$(mktemp)"
    set +e
    ISTOTA_SECRETS_CONTROL_OVERLAYS="$PWD/${RUN_CONTRACT_CONTROLS}/secret-in-environment.yml" \
        uv run pytest -m image -n0 -q --no-header -p no:randomly "$SECRETS_TESTS" 2>&1 | tee "$secrets_out"
    set -e
    secrets_node="${SECRETS_TESTS}::TestTheCredentialsAreFilesNotEnvironment::test_docker_inspect_carries_no_credential"
    secrets_read="${SECRETS_TESTS}::TestTheCredentialsAreFilesNotEnvironment::test_the_daemon_read_them"
    if ! grep -Fq "FAILED ${secrets_node}" "$secrets_out"; then
        rm -f "$secrets_out"
        echo "[control] FAILED: secrets did not turn ${secrets_node} red."
        exit 1
    fi
    if grep -Fq "FAILED ${secrets_read}" "$secrets_out" || grep -Fq "ERROR ${secrets_read}" "$secrets_out"; then
        rm -f "$secrets_out"
        echo "[control] FAILED: secrets broke the stack rather than the one line;"
        echo "[control]   ${secrets_read} should still pass."
        exit 1
    fi
    rm -f "$secrets_out"
    echo "[control] OK: secrets turned the inspect witness red and left the files' witnesses green."
}

# --------------------------------------------------------------------------
# The config is an input: the upgrade case boots the new entrypoint over a
# config.toml an old release rendered and requires the file unchanged. The
# control image appends a line to it at boot; only that assertion may go red,
# and the daemon must still start.

RENDERED_TESTS="tests/image/test_upgrade.py::TestTheNewEntrypointKeepsARenderedConfig"

rendered_config_half() {
    # The newest image the tier built; `docker images` lists newest first.
    rendered_base="$(docker images --format '{{.Repository}}:{{.Tag}}' istota-test/istota | head -1)"
    if [ -z "$rendered_base" ]; then
        echo "[control] no istota-test/istota image yet — run \`uv run pytest -m image -n0\` first." >&2
        exit 2
    fi
    echo
    echo "[control] rendered-config: an entrypoint that rewrites the operator's config (base ${rendered_base})"
    docker build -q -f docker/test/Dockerfile.rewrites-config \
        --build-arg "BASE=$rendered_base" -t istota-test/rewrites-config:control docker/test >/dev/null
    rendered_out="$(mktemp)"
    set +e
    ISTOTA_IMAGE_TAG=istota-test/rewrites-config:control ISTOTA_UPGRADE_SHAPES=volume \
        ISTOTA_UPGRADE_FROM="$(grep -v '^#' scripts/upgrade-floor | grep -v '^$' | head -1)" \
        uv run pytest -m image -n0 -q --no-header -p no:randomly "$RENDERED_TESTS" 2>&1 | tee "$rendered_out"
    set -e
    if ! grep -Fq "FAILED ${RENDERED_TESTS}::test_the_file_is_not_rewritten" "$rendered_out" \
        || grep -Fq "FAILED ${RENDERED_TESTS}::test_the_daemon_starts_from_it" "$rendered_out"; then
        rm -f "$rendered_out"
        echo "[control] FAILED: rendered-config did not turn exactly the rewrite witness red."
        exit 1
    fi
    rm -f "$rendered_out"
    echo "[control] OK: rendered-config turned the rewrite witness red and the daemon still started."
}

if [ -n "$only_run_contract" ]; then
    run_contract_half
    exit 0
fi
if [ -n "$only_rendered_config" ]; then
    rendered_config_half
    exit 0
fi
if [ -n "$only_secrets" ]; then
    secrets_half
    exit 0
fi

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

# The image half of parity row 5: an image whose `istota-drop` skips setpriv.
# The same control image the run-contract half below runs as a stack.
echo
echo "[control] building the no-drop control (row 5, image half)…"
no_drop_tag="istota-test/no-drop:${base_tag##*:}"
docker build -q -f docker/test/Dockerfile.no-drop \
    --build-arg "BASE=$base_tag" -t "$no_drop_tag" docker/test >/dev/null
no_drop_out="$(mktemp)"
set +e
if [ -n "$platform" ]; then
    ISTOTA_IMAGE_TAG="$no_drop_tag" uv run pytest -m image -n0 -q --no-header \
        --platform "$platform" tests/image/test_istota_image.py::TestTheDropToTheDaemonsUser 2>&1 | tee "$no_drop_out"
else
    ISTOTA_IMAGE_TAG="$no_drop_tag" uv run pytest -m image -n0 -q --no-header \
        tests/image/test_istota_image.py::TestTheDropToTheDaemonsUser 2>&1 | tee "$no_drop_out"
fi
set -e
no_drop_node="tests/image/test_istota_image.py::TestTheDropToTheDaemonsUser::test_the_drop_leaves_uid_10001_and_no_capabilities"
if ! grep -Fq "FAILED ${no_drop_node}" "$no_drop_out"; then
    rm -f "$no_drop_out"
    echo "[control] FAILED: the drop witness passed on an image whose drop does nothing."
    exit 1
fi
rm -f "$no_drop_out"
echo "[control] OK: the drop witness failed on the no-drop image."


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

# The run-contract half runs the lean smoke stack, which builds natively; an
# amd64 run of this script leaves it to the native one. So does row 12's.
if [ -z "$platform" ]; then
    run_contract_half
    secrets_half
    rendered_config_half
fi
