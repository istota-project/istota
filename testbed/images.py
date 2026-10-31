"""The images a test run built, removed when the run ends.

Every run builds tagged images, and nothing reclaims a tag. Docker's disk is
shared by every worktree, so a few sessions fill it, and a full disk reads as a
broken stack (an empty `/data/.secret_key`). Only what this run built is recorded:
an image named by `ISTOTA_IMAGE_TAG` or a profile belongs to whoever made it.

`ISTOTA_TEST_IMAGE_LEDGER` is for a driver running several pytest sessions over
one image: the tags go to that file, and the driver removes them when it ends
(`scripts/test-image-ledger.sh`). `ISTOTA_KEEP_TEST_IMAGES=1` keeps them, for
someone reaching for one by hand.

A run that built anything also drops build cache nobody has used in
`BUILD_CACHE_MAX_AGE`, since each new tree adds layers and nothing else reclaims the
ones an image no longer holds. By age because a size limit does nothing here: on
Docker Desktop 29, `--keep-storage` and `--max-used-space` both reclaimed 0 B at 3 GB
under a 9.5 GB cache, and `--filter until=` reclaimed all of it. Recent layers stay, so
the next build is warm. The cache is the machine's, so another project's next build
after a long gap can be slower, and none of its images or volumes go.
"""

from __future__ import annotations

import os
import subprocess

KEEP_VAR = "ISTOTA_KEEP_TEST_IMAGES"
LEDGER_VAR = "ISTOTA_TEST_IMAGE_LEDGER"
REMOVE_TIMEOUT = 60
PRUNE_TIMEOUT = 300
#: `scripts/test-image-ledger.sh` holds the same value.
BUILD_CACHE_MAX_AGE = "48h"

_built: set[str] = set()


def record(tag: str) -> None:
    ledger = os.environ.get(LEDGER_VAR)
    if ledger:
        with open(ledger, "a") as handle:
            handle.write(f"{tag}\n")
        return
    _built.add(tag)


def _remove(tag: str) -> bool:
    try:
        result = subprocess.run(
            ["docker", "image", "rm", tag], capture_output=True, text=True, timeout=REMOVE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


def trim_build_cache() -> str | None:
    """Why the trim failed, or None. Said out loud, since nothing else notices a cache
    that has stopped being trimmed until the disk is full.

    The current context's builder, which is the one `docker build` used. Naming
    `BUILDX_BUILDER=default`, as `istota-stack` does on a VM, fails on Docker Desktop.
    A trim that reclaims nothing is still a success: most runs have nothing that old.
    """
    try:
        result = subprocess.run(
            ["docker", "builder", "prune", "--all", "--force", "--filter", f"until={BUILD_CACHE_MAX_AGE}"],
            capture_output=True, text=True, timeout=PRUNE_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return type(exc).__name__
    return None if result.returncode == 0 else (result.stderr or "no output").strip()[-200:]


def remove_built() -> tuple[list[str], list[str]]:
    """``(removed, left)``. Never raises, and no `--force` on an image: Docker refuses
    to remove one a container is running, which is what should happen."""
    tags = sorted(_built)
    _built.clear()
    if os.environ.get(KEEP_VAR, "").strip().lower() in ("1", "true", "yes", "on"):
        return [], tags
    removed: list[str] = []
    left: list[str] = []
    for tag in tags:
        (removed if _remove(tag) else left).append(tag)
    return removed, left
