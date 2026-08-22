#!/usr/bin/env bash
# Run the test suite on a real Linux kernel, with a real bubblewrap.
#
# Everything istota knows about its own runtime is otherwise asserted on the
# one platform that cannot run it: `tests/test_sandbox.py` patches
# `_bwrap_available` and checks argv, so on darwin the sandbox code path has
# never executed. This driver builds a small Debian image, binds the checkout
# into it read-only, and runs the suite there — including the `linux`-marked
# tests, which are deselected everywhere else.
#
# It is a discretionary command. Nothing runs it automatically, `uv run pytest`
# on the host is unchanged by its existence, and the `linux` marker is
# deselected by pyproject's addopts so a developer on a box without Docker can
# run and develop against the whole suite as before.
#
#   scripts/test-linux.sh                      # ruff + the suite + the linux tests
#   scripts/test-linux.sh -m linux             # just the sandbox tests
#   scripts/test-linux.sh tests/test_sandbox.py -x
#
# Any arguments are passed through to pytest.
#
# Run it under `scripts/qtest`, as with any full suite: the container sizes its
# worker pool from the host's cores and knows nothing about the semaphore, so
# an unwrapped run competes with whatever else is testing on the machine.
#
# One observed quirk, so a phantom failure is not chased: Docker Desktop's
# file sharing has served a *stale* copy of a just-edited file through the
# read-only bind, which surfaced as a test asserting against a file that no
# longer looked like that. A second run picked up the current one. If a result
# contradicts what you can read on disk, run it again before believing it.
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
IMAGE_TAG="${ISTOTA_TEST_IMAGE:-istota-test:local}"
CACHE_VOLUME="istota-test-cache"

# Refuse inside the sandbox, and refuse *before* asking about the daemon.
#
# A task's Docker access is the devbox allowlist proxy, which permits ping,
# version, container list, and inspect/archive/restart/exec on the task's own
# container — and nothing that creates or starts one. This tier needs to run a
# container with CAP_SYS_ADMIN, CAP_NET_ADMIN and unconfined seccomp, which is
# the exact capability the sandbox exists to deny. The collision is structural:
# it is not a misconfiguration to be fixed by widening the allowlist, and
# widening it would hand every task a host escape.
#
# The ordering is the point. `docker version` is *on* the proxy's allowlist, so
# the precheck below passes inside a task and the run then died minutes later
# inside `docker build`, reporting a buildx driver error that describes nothing
# about the real boundary (ISSUE-293). The pytest tiers never had this failure
# mode because they precheck with `docker info`, which the proxy denies.
if [ -n "${ISTOTA_SANDBOXED:-}" ]; then
    echo "scripts/test-linux.sh cannot run inside the sandbox." >&2
    echo "" >&2
    echo "This tier runs a container with CAP_SYS_ADMIN and CAP_NET_ADMIN so that" >&2
    echo "bwrap can create namespaces. A task reaches Docker through the devbox" >&2
    echo "allowlist proxy, which does not permit creating or starting a container" >&2
    echo "and should not — that grant would be a host escape." >&2
    echo "" >&2
    echo "This is not a test failure. Nothing is broken and nothing is red." >&2
    echo "Say in the merge request that the change touches the sandbox and that" >&2
    echo "the linux tier is out of reach from a task, and ask for the run before" >&2
    echo "merge. See docs/development/testing.md, 'Deployment tiers'." >&2
    # 75, not 1: the tier did not run, which is a different thing from the tier
    # running and going red. 1 is what a real failure exits with here — the
    # daemon precheck below, the bwrap probe, a failing suite — so reusing it
    # would leave a caller unable to tell "out of reach" from "broken", which
    # is the confusion this whole change exists to remove. `scripts/qtest`
    # already uses 75 for "no slot came free and the command did not run".
    exit 75
fi

if ! docker version >/dev/null 2>&1; then
    echo "scripts/test-linux.sh needs a running Docker daemon." >&2
    echo "This is a discretionary tier — 'uv run pytest' on the host does not need it." >&2
    exit 1
fi

# In a linked worktree, `.git` is a *file* pointing at a gitdir outside the
# checkout, so binding the checkout alone leaves every git command run from the
# container's working directory failing with "fatal: not a git repository" and
# exit 128 — including the ones tests inherit their cwd for
# (`git_remote_scrub`, the private-data scanner). Bind the common gitdir at the
# same absolute path so the pointer resolves. In an ordinary clone the common
# dir is inside the checkout and this adds nothing.
git_common_dir=""
if git_common_dir="$(git -C "$REPO_ROOT" rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"; then
    case "$git_common_dir" in
        # Already inside the checkout — an ordinary clone, nothing to add.
        "$REPO_ROOT"/*) git_common_dir="" ;;
        # A colon would be read as the separator in `src:dst:ro` and bind
        # something else entirely. Rare enough to decline rather than escape.
        *:*) git_common_dir="" ;;
    esac
    # `rev-parse` reports what the gitfile *says*, not what exists, so a stale
    # `commondir` yields a path it returns with exit 0 — and `docker run -v`
    # creates a missing source as a root-owned empty directory rather than
    # failing. The result would be an empty bind, the same "not a git
    # repository" as before, and a stray directory on the host.
    if [ -n "$git_common_dir" ] && [ ! -d "$git_common_dir" ]; then
        echo "warning: git reports a common dir that does not exist: $git_common_dir" >&2
        echo "         not binding it; git commands inside the runner may fail." >&2
        git_common_dir=""
    fi
fi

# The capability grants below are what let bwrap create namespaces inside the
# container. --cap-add=SYS_ADMIN with an unconfined seccomp profile is close to
# host-equivalent on a Linux Docker host, and bounded by the VM on Docker
# Desktop. They exist for this local test runner and nowhere else: they must
# never appear in a compose file that could be pointed at a real deployment.
#
# NET_ADMIN is the one that is not obvious. On the deployment the daemon runs
# unprivileged, so bwrap creates a user namespace and holds CAP_NET_ADMIN
# inside it — enough to bring up the loopback interface `--unshare-net`
# requires. In this container bwrap runs as real root and therefore skips the
# user namespace, so the capability has to come from Docker instead. Without
# it every network-isolated sandbox dies at startup with
# "bwrap: loopback: Failed RTM_NEWADDR: No child processes".
echo "note: this runner grants CAP_SYS_ADMIN + CAP_NET_ADMIN and unconfined" >&2
echo "      seccomp/apparmor so bwrap can create namespaces. Local test runner" >&2
echo "      only — never a deployment." >&2

# Quiet by default: a cached build is fourteen CACHED lines ahead of the test
# output every run. ISTOTA_TEST_BUILD_PROGRESS=plain when a build is what you
# are debugging. A failing build still prints its error either way.
#
# `--progress` is a BuildKit flag, and the legacy builder refuses it outright
# ("unknown flag: --progress") before it reads the Dockerfile. That is not a
# hypothetical path: `DOCKER_BUILDKIT=0` is how a host whose default buildx
# builder is a `docker-container` driver that cannot reach the daemon gets a
# build at all, so the one configuration that needs the legacy builder was the
# one this script refused to build on (ISSUE-293).
#
# Ask the CLI which build it is about to run rather than inferring it from a
# daemon version: `docker build --help` is the same switch the CLI itself makes
# on DOCKER_BUILDKIT, so it answers the actual question. Confirmed against
# Docker 29.6.2 (build dfc4efb): the help lists `--progress` under the default
# builder and does not list it under `DOCKER_BUILDKIT=0`. Captured rather than
# piped into grep, because `grep -q` exits on the first match and SIGPIPEs the
# producer — under `set -o pipefail` that reads as a failed probe and would
# silently drop the flag on a BuildKit host.
#
# NUL-delimited, not newline: `ISTOTA_TEST_BUILD_PROGRESS` is read from the
# environment, and a value containing a newline would otherwise arrive as two
# array elements — `docker build --progress pl ain -f …` names a second build
# context and fails obscurely. A value with a space is already safe by way of
# `IFS=`; this covers the other one. `read -r -d ''` works on bash 3.2.
build_progress_args() {
    local help_text
    help_text="$(docker build --help 2>/dev/null || true)"
    case "$help_text" in
        *--progress*) printf '%s\0%s\0' --progress "${ISTOTA_TEST_BUILD_PROGRESS:-quiet}" ;;
    esac
}

progress_args=()
while IFS= read -r -d '' progress_arg; do
    progress_args+=("$progress_arg")
done < <(build_progress_args)

docker build ${progress_args[@]+"${progress_args[@]}"} \
    -f "$REPO_ROOT/docker/test/Dockerfile" -t "$IMAGE_TAG" "$REPO_ROOT"

docker volume create "$CACHE_VOLUME" >/dev/null

run_in_container() {
    # --tmpfs /tmp because the source bind is read-only and pytest's tmp_path,
    # the sandbox probes and the uv cache all need somewhere to write.
    #
    # PYTHONPATH because the image installs dependencies but not the project
    # (`--no-install-project`), so `istota` is importable only through pytest's
    # `pythonpath = ["src"]` — and that ini setting applies to the pytest
    # process, not to the dozens of tests that spawn `python -m istota.skills.X`
    # as a subprocess. Without it those exit with ModuleNotFoundError and read
    # as ~170 unrelated failures.
    #
    # A git identity as environment variables rather than a global config: a
    # dozen tests build throwaway repositories and commit into them, and they
    # pass on a developer host only because that host has a `user.email` set.
    # `GIT_AUTHOR_*` survives the tests that repoint HOME, which a
    # `git config --global` in the image would not.
    local gitdir_bind=()
    if [ -n "$git_common_dir" ]; then
        gitdir_bind=(-v "$git_common_dir:$git_common_dir:ro")
    fi
    # --init puts a real reaper at PID 1. Without it PID 1 is pytest, which
    # reaps only its own children, so an orphaned grandchild stays a zombie —
    # and `os.kill(pid, 0)` on a zombie succeeds. The process-group and qtest
    # tests then report "the grandchild survived the group kill" when what
    # survived is a defunct entry nobody collected. On a real host systemd
    # does this job.
    # `${a[@]+"${a[@]}"}` rather than `"${a[@]}"`: on bash 3.2 — which is what
    # /bin/bash is on macOS, the platform this script exists for — expanding an
    # empty array under `set -u` is a fatal "unbound variable". The array is
    # empty in an ordinary clone, so the plain form worked in a linked worktree
    # and died in the normal checkout. It dies inside a `$( )` too, so the
    # first thing it breaks is the bwrap probe, which then reports a namespace
    # failure that never happened.
    docker run --rm --init \
        ${gitdir_bind[@]+"${gitdir_bind[@]}"} \
        --cap-add=SYS_ADMIN \
        --cap-add=NET_ADMIN \
        --security-opt seccomp=unconfined \
        --security-opt apparmor=unconfined \
        -v "$REPO_ROOT:/src:ro" \
        -e PYTHONPATH=/src/src \
        -e ISTOTA_LINUX_TIER=1 \
        -e GIT_AUTHOR_NAME=istota-test -e GIT_AUTHOR_EMAIL=test@istota.invalid \
        -e GIT_COMMITTER_NAME=istota-test -e GIT_COMMITTER_EMAIL=test@istota.invalid \
        -v "$CACHE_VOLUME:/uv-cache" \
        --tmpfs /tmp:exec \
        -w /src \
        "$IMAGE_TAG" "$@"
}

# Fail loudly, not silently, when the namespace cannot be created. A skip on the
# one layer built to end silent non-execution would repeat the original defect
# exactly. A developer who knowingly cannot run bwrap here sets
# ISTOTA_ALLOW_NO_BWRAP=1 and gets the rest of the suite.
#
# Individual `linux`-marked tests still skip when collected outside this driver
# (a bare `pytest -m linux` on darwin) — that is a different situation from the
# driver claiming success. Inside the driver they cannot skip at all:
# ISTOTA_LINUX_TIER=1, set on every container above, turns their skip guard
# into a hard failure. Without that the two questions could drift apart — these
# probes ask about `--unshare-user` and `--unshare-net`, while the tests guard
# on `_bwrap_available()`, which probes neither — and every linux test could
# skip itself while the driver reported a clean run.
#
# Two probes, not one. `--unshare-user` and `--unshare-net` fail for different
# reasons and only the first is fixed by CAP_SYS_ADMIN, so folding them into a
# single bwrap invocation would let a passing user-namespace probe vouch for a
# network namespace that cannot come up. They must also stay separate in the
# other direction: adding `--unshare-user` to the network probe makes bwrap
# create a user namespace where CAP_NET_ADMIN comes free, so the probe would
# pass on a host where the real sandbox — which does not pass `--unshare-user`
# here, because `--disable-userns` is unsupported in a container — still fails.
probe_failure=""
if ! probe_output="$(run_in_container bwrap --unshare-user --ro-bind / / -- true 2>&1)"; then
    probe_failure="bwrap cannot create a user namespace: ${probe_output}"
elif ! probe_output="$(run_in_container bwrap --unshare-net --ro-bind / / -- true 2>&1)"; then
    probe_failure="bwrap cannot bring up a network namespace: ${probe_output}"
fi

if [ -n "$probe_failure" ]; then
    if [ "${ISTOTA_ALLOW_NO_BWRAP:-}" = "1" ]; then
        echo "warning: ${probe_failure}" >&2
        echo "         ISTOTA_ALLOW_NO_BWRAP=1 is set, so the linux-marked tests will" >&2
        echo "         skip themselves and the rest of the suite runs." >&2
    else
        echo "error: ${probe_failure}" >&2
        echo "       The linux tier exists to execute the sandbox path, so this is a" >&2
        echo "       failure, not a skip." >&2
        echo "       Set ISTOTA_ALLOW_NO_BWRAP=1 to run the rest of the suite anyway." >&2
        exit 1
    fi
fi

# Run the suite *including* the linux tests. Without this the driver would
# inherit pyproject's addopts, which deselects `linux` — the exact tests the
# driver exists to run.
#
# Prepended rather than conditional: pytest's `-m` is last-wins, so a user's
# own `-m` still overrides this in every spelling (`-m x`, `-m=x`, `-mx`). The
# version of this that tried to detect an incoming `-m` matched two spellings
# pytest does not accept and missed one it does, and could not have mattered
# either way.
#
# ISTOTA_LINUX_TIER_MARKERS is the same deselection set as pyproject's addopts,
# restated because there is no way to say "the default expression, plus linux".
# `image` and `smoke` are forward references to Stages 5 and 7 of the
# deployment-artifact-verification spec and do not exist yet; naming a marker
# no test carries is harmless. tests/test_linux_runner.py fails if this set
# ever falls behind addopts — which is the direction that would silently start
# running a marker meant to be off by default.
default_markers='linux or (not integration and not live and not image and not smoke and not ml)'
pytest_args=(-m "$default_markers" "$@")

# pytest writes its cache beside the rootdir, and the rootdir is the read-only
# bind. Redirect it rather than disabling the cache plugin, so --lf and --ff
# still work inside the runner.
pytest_args=(-o cache_dir=/tmp/pytest_cache "${pytest_args[@]}")

# ruff first: a lint failure is cheaper to read before a suite's worth of output,
# and this is the run that sees the tree as Linux sees it.
# The cgroup setup is sourced, not run: it exports ISTOTA_TEST_CGROUP_ROOT into
# the pytest process, and it has to happen inside the container because what it
# builds is that container's own cgroup subtree. It never fails the run — a
# Docker that cannot delegate cgroups is a limitation of the machine, and the
# tests skip themselves there rather than taking the whole tier down with them.
run_in_container sh -c '. /src/scripts/dev/linux-tier-cgroup.sh
ruff check --output-format concise src tests && exec pytest "$@"' \
    -- "${pytest_args[@]}"
