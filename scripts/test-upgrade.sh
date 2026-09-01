#!/usr/bin/env bash
# Boot the shipped image over an older release's config.toml and database.
#
# New code against an old config.toml is how ISSUE-263 reached production: the
# auto-update cron `git reset --hard`s to main without running Ansible, and a
# Docker rebuild over a retained volume used to keep the config the entrypoint
# wrote on that volume's *first* boot (ISSUE-368 made that render on every boot,
# which narrows the Docker case to the window before the container restarts, but
# does not close it). Every other tier in this spec renders a fresh config, and a
# fresh config is current by definition — so none of them can see this.
#
#   scripts/test-upgrade.sh                                   # near anchor, code shape
#   scripts/test-upgrade.sh --from-floor --shape volume       # far anchor, before a release
#   scripts/test-upgrade.sh --shape both
#   scripts/test-upgrade.sh --from v0.38.0 --shape volume     # reproduce a specific report
#   scripts/test-upgrade.sh --platform amd64 --shape both     # before a release
#   scripts/test-upgrade.sh --refresh                         # ignore the cached capture
#   scripts/test-upgrade.sh --shape both -- -x -k drift       # pytest args after `--`
#
# Two anchors, because one is not enough. The near anchor is the merge-base with
# the default branch — about three days at the current release cadence, which on
# its own is close to a no-op as a regression detector, but it is the span the
# auto-update cron actually crosses and it is cheap. The far anchor is the tag in
# `scripts/upgrade-floor`, roughly a month back, and that file *is* the statement
# of how far back an upgrade is supported.
#
# This is a thin driver. Every assertion lives in tests/image/test_upgrade.py so
# that a failure names the property that broke rather than an exit code, and so
# that the tier is runnable directly with pytest. Everything here is argument
# translation.
#
# Run it under `scripts/qtest`, as with any tier that claims a machine.
#
# --- end usage ---
#
# No arrays anywhere: macOS ships bash 3.2, where `"${empty[@]}"` under `set -u`
# is fatal, and this script's whole audience is a developer machine.
set -euo pipefail

cd "$(dirname "$0")/.."

from_ref=""
use_floor=0
shape="code"
platform=""
refresh=0

# The user-facing part of the header, delimited by a sentinel rather than by a
# line number: a hardcoded range silently starts printing implementation notes
# the moment the header grows.
usage() {
    sed -n '2,/^# --- end usage ---$/p' "$0" \
        | grep -v '^# --- end usage ---$' \
        | sed 's/^# \{0,1\}//'
}

# `--from --shape volume` must not take "--shape" as the ref. Every option that
# consumes a value routes through this.
need_value() {
    if [ "$#" -lt 2 ]; then
        echo "$1 needs a value" >&2
        exit 2
    fi
    case "$2" in
        -*) echo "$1 needs a value, got the option '$2'" >&2; exit 2 ;;
    esac
}

# The one ref named by scripts/upgrade-floor.
#
# `tests/support/upgrade.py:read_floor` is the authority — it refuses a file
# naming two refs rather than silently taking the first, and it does not strip
# inline comments. This calls it rather than reimplementing it: an earlier
# version did its own `sed | head -1`, which meant the shell accepted exactly
# what the Python refuses, and a floor written `v1.2.3  # bumped 2026-08` gave
# a clean banner and an unresolvable ref later.
#
# One process, no pipeline: `sed | head -1` under `set -o pipefail` can exit
# 141 when head closes the pipe on a large file, and an assignment from a
# command substitution then aborts the script under `set -e` with no message.
read_floor_ref() {
    uv run python -c 'import sys; sys.path.insert(0, "tests"); from support.upgrade import read_floor; print(read_floor())'
}

while [ $# -gt 0 ]; do
    case "$1" in
        --from)
            need_value "$@"
            from_ref="$2"; shift 2 ;;
        --from-floor)
            use_floor=1; shift ;;
        --shape)
            need_value "$@"
            shape="$2"; shift 2 ;;
        --platform)
            need_value "$@"
            platform="$2"; shift 2 ;;
        --refresh)
            refresh=1; shift ;;
        -h|--help)
            usage; exit 0 ;;
        --)
            # Everything after `--` goes to pytest: -x, -k, --pdb, a node id.
            shift
            break ;;
        *)
            echo "unknown argument: $1" >&2
            echo "try --help" >&2
            exit 2 ;;
    esac
done

case "$shape" in
    code|volume|both) ;;
    *) echo "--shape must be one of: code, volume, both (got '$shape')" >&2; exit 2 ;;
esac

if [ -n "$from_ref" ] && [ "$use_floor" -eq 1 ]; then
    echo "--from and --from-floor name different anchors; pass one." >&2
    exit 2
fi

# Refuse inside the sandbox, ahead of the daemon precheck. `docker version` is
# on the devbox proxy's allowlist and container creation is not, so without
# this the tier passes its own precheck inside a task and fails later on a
# call the proxy forbids. See the longer note in scripts/test-linux.sh.
if [ -n "${ISTOTA_SANDBOXED:-}" ]; then
    echo "scripts/test-upgrade.sh cannot run inside the sandbox." >&2
    echo "" >&2
    echo "This tier boots the current image over an older release's state, which" >&2
    echo "means creating and running containers. A task reaches Docker through the" >&2
    echo "devbox allowlist proxy, which permits neither and should not." >&2
    echo "" >&2
    echo "This is not a test failure. Nothing is broken and nothing is red." >&2
    echo "Say in the merge request that the change touches a migration, a config" >&2
    echo "key or config.toml generation and that the upgrade tier is out of reach" >&2
    echo "from a task, and ask for the run before merge." >&2
    echo "See docs/development/testing.md, 'Deployment tiers'." >&2
    # 75 — the tier did not run. See the note in scripts/test-linux.sh; this
    # script already reserves 2 for a usage error and 1 for a real failure.
    exit 75
fi

if ! docker version >/dev/null 2>&1; then
    echo "scripts/test-upgrade.sh needs a running Docker daemon." >&2
    echo "This is a discretionary tier — 'uv run pytest' on the host does not need it." >&2
    exit 1
fi

# --from-floor is resolved here rather than passed through as a flag, so that a
# floor a shallow clone cannot see is reported as its own thing — the usual
# cause, and its fix is `git fetch --tags --unshallow` rather than anything to
# do with upgrades.
if [ "$use_floor" -eq 1 ]; then
    if ! from_ref="$(read_floor_ref)"; then
        echo "scripts/upgrade-floor could not be read; see the error above." >&2
        exit 1
    fi
    if [ -z "$from_ref" ]; then
        echo "scripts/upgrade-floor names no ref." >&2
        exit 1
    fi
    if ! git rev-parse --verify --quiet "${from_ref}^{commit}" >/dev/null; then
        echo "the floor '${from_ref}' does not resolve in this clone." >&2
        echo "A shallow clone that predates the tag is the usual cause:" >&2
        echo "    git fetch --tags --unshallow" >&2
        exit 1
    fi
fi

if [ "$shape" = "both" ]; then
    shapes="code,volume"
else
    shapes="$shape"
fi

# The banner has to be per-shape, because without an explicit --from each shape
# uses its *own* default anchor: code from the merge-base, volume from the
# floor. A single "anchor: …" line was wrong for exactly the run most worth
# reading it on — `--shape both`, where it named the near anchor while the
# volume half was quietly using the floor.
echo "[upgrade] shapes:   ${shapes}"
if [ -n "$from_ref" ]; then
    echo "[upgrade] anchor:   ${from_ref} (both shapes)"
else
    case "$shapes" in
        *code*)   echo "[upgrade] anchor:   code   <- merge-base with the default branch" ;;
    esac
    case "$shapes" in
        *volume*) echo "[upgrade] anchor:   volume <- $(read_floor_ref) (scripts/upgrade-floor)" ;;
    esac
fi
if [ -n "$platform" ]; then
    echo "[upgrade] platform: ${platform} (an emulated build is minutes, not seconds)"
fi

export ISTOTA_UPGRADE_SHAPES="$shapes"
if [ -n "$from_ref" ]; then
    export ISTOTA_UPGRADE_FROM="$from_ref"
fi
if [ "$refresh" -eq 1 ]; then
    export ISTOTA_UPGRADE_REFRESH=1
fi

receipt="$(mktemp -t istota-upgrade-receipt)"
trap 'rm -f "$receipt"' EXIT INT TERM
export ISTOTA_UPGRADE_RECEIPT="$receipt"

# -n0 because the image fixtures are session-scoped and N xdist workers would
# each race to build one tag. tests/image/conftest.py fails the session with
# that reason rather than letting it happen, so this is belt and braces.
#
# `-rs` so skip reasons are always printed. Skipping is a normal answer here —
# a shape that was not selected, a near anchor that is HEAD — and a bare count
# of skips is not something anyone can act on.
#
# Not `exec`, because this driver has a post-condition. Every shape can skip,
# and a session where they all did exits 0 having asserted nothing about an
# upgrade. That is the silent non-execution `scripts/test-linux.sh` grew
# `ISTOTA_LINUX_TIER=1` for, and it matters more here, where one shape skipping
# is routine. The tier writes a line to $ISTOTA_UPGRADE_RECEIPT for each shape
# it actually carried through; an empty receipt means no upgrade was performed.
set +e
if [ -n "$platform" ]; then
    uv run pytest -m image -n0 --no-header -rs --platform "$platform" \
        tests/image/test_upgrade.py "$@"
else
    uv run pytest -m image -n0 --no-header -rs \
        tests/image/test_upgrade.py "$@"
fi
status=$?
set -e

if [ "$status" -ne 0 ]; then
    exit "$status"
fi

if [ ! -s "$receipt" ]; then
    echo >&2
    echo "[upgrade] FAILED: no shape performed an upgrade — this run asserted" >&2
    echo "[upgrade] nothing about upgrading. Read the skip reasons above; a" >&2
    echo "[upgrade] clean exit here would be a lie." >&2
    exit 1
fi

echo "[upgrade] upgrades performed:"
sed 's/^/[upgrade]   /' "$receipt"
