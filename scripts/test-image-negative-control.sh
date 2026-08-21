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
import os, sys
sys.path.insert(0, "tests")
from image.conftest import _tag_for, ISTOTA_DOCKERFILE, resolve_platform


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

echo "[control] OK: the tier failed on the broken image, as it must."
echo "[control] Read the failures above and confirm they name the missing path."
