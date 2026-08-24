"""Another user's package cache, read from inside a live task in the image.

`security.sandbox_cache_dir` belongs under `developer.repos_dir`, and the repos
bind is emitted after the cache bind and is an ancestor of it. bwrap applies
argv in order, so that later bind covers the cache root and hands every user's
subdirectory to every admin developer task, read-write (ISSUE-319).

Removing the covering is not the fix and would cost the thing the placement
exists for. `link(2)` compares mounts rather than devices, so with the cache on
its own mount uv stops hardlinking into the venv and copies every wheel — the
single covering bind is the only shape where it does not. So `build_bwrap_cmd`
keeps the bind and masks the *sibling* directories with empty read-only tmpfs
mounts instead, after both binds.

**Why this file exists rather than more argv assertions.** `tests/test_sandbox.py`
patches `_bwrap_available` and inspects the argv, so it proves the flags are
emitted and nothing about what the kernel does with them. Here the mask is the
whole boundary rather than defence behind the skill CLIs' `ISTOTA_USER_ID`
scoping, which is a higher bar than the database masks have to clear, and the
tier that can actually clear it is the one running a real bwrap in the shipped
image.

Both halves are asserted, because either alone has a false pass. The negative
half — the sibling is empty and unwritable — is equally true of a cache that
was never bound at all, which is what every refusal path in
`resolve_sandbox_cache_dir` produces. The positive half — the task's own cache
is readable and writable, and `ln` into a directory under `repos_dir` succeeds
— is what says the bind is there and still on one mount with the venv.

The in-session control below is not a formality either: it caught this file's
first probe, which asked whether the sibling was a `tmpfs` and could not fail,
because `/data/repos` is on a tmpfs in this container to begin with. What
replaced it is in `CACHE_PROBE`.
"""

from __future__ import annotations

import pytest

from testbed import profiles

pytestmark = pytest.mark.smoke

#: Matches `CACHE.config["ISTOTA_SECURITY_SANDBOX_CACHE_DIR"]`, and
#: `CONTAINER_REPOS_DIR` in `testbed/services/gitlab.py` is its parent. Restated
#: rather than imported so a scenario reads as one thing; held equal by
#: `test_the_probe_paths_match_the_profile` below.
CACHE_ROOT = "/data/repos/.package-caches"
OWN_USER = "testuser"
OTHER_USER = "someone-else"

#: What the other user's cache holds. uv's unpacked-wheel cache is trusted on
#: read and re-verified against no hash, so a file planted here is a file the
#: next `uv sync` would hardlink into a venv and execute — which is why an empty
#: listing inside the sandbox is the assertion and not a stylistic choice.
PLANTED = "sitecustomize.py"

#: Seven facts about three paths, read from inside the task.
#:
#: **The two device numbers are the discriminator, and `stat -f -c %T` is not.**
#: The obvious probe — "is the sibling a tmpfs" — was written first and the
#: control caught it: `/data/repos` is *itself* on a tmpfs in this container, so
#: the answer is `tmpfs` inside the sandbox and outside it alike, and the
#: assertion could not fail. Comparing the sibling's device against the task's
#: own cache is immune to that: a mask is a fresh mount and its own device,
#: while two directories in one bound tree share one. Inside the sandbox they
#: must differ; from the daemon's own view they must match.
#:
#: `other_entries` carries `ls`'s error text into the marked block rather than
#: swallowing it, so a path that is not in the namespace at all reads as
#: `[ls: ...]` and not as `[]`. An empty listing then means present and empty.
#:
#: The `ln` is the positive half and is the property the whole placement is for.
#: It has to target a directory under `repos_dir` but outside the cache, which
#: is what a venv is: on one mount it succeeds, and across a mount boundary it
#: is `EXDEV` however identical the filesystems are.
CACHE_PROBE = f"""
echo CACHE_PROBE_BEGIN
echo "other_dev=$(stat -c %d {CACHE_ROOT}/{OTHER_USER} 2>&1)"
echo "own_dev=$(stat -c %d {CACHE_ROOT}/{OWN_USER} 2>&1)"
echo "other_entries=[$(ls -A {CACHE_ROOT}/{OTHER_USER} 2>&1 | tr '\\n' ' ')]"
if touch {CACHE_ROOT}/{OTHER_USER}/probe 2>/dev/null; then
  echo "other_writable=yes"
  rm -f {CACHE_ROOT}/{OTHER_USER}/probe
else
  echo "other_writable=no"
fi
if touch {CACHE_ROOT}/{OWN_USER}/probe 2>/dev/null; then
  echo "own_writable=yes"
else
  echo "own_writable=no"
fi
mkdir -p /data/repos/fake-venv 2>/dev/null
if ln {CACHE_ROOT}/{OWN_USER}/probe /data/repos/fake-venv/linked 2>/dev/null; then
  echo "hardlink=ok nlink=$(stat -c %h /data/repos/fake-venv/linked 2>&1)"
else
  echo "hardlink=failed"
fi
rm -f {CACHE_ROOT}/{OWN_USER}/probe /data/repos/fake-venv/linked
echo CACHE_PROBE_END
"""

CACHE_SCRIPT = [
    {
        "tool_calls": [
            {
                "id": "call-1",
                "name": "Bash",
                "arguments": {"command": CACHE_PROBE},
            }
        ]
    },
    {"text": "I looked at the package caches"},
]


def _devices(observed: str) -> tuple[str, str]:
    """The sibling's and the task's own device numbers, out of a probe block.

    Both or neither: a missing line means `stat` printed an error the marked
    block carried through, and comparing that error against the other value
    would report "they differ" — the answer the masked case wants — for a probe
    that never ran.
    """
    found: dict[str, str] = {}
    for line in observed.splitlines():
        for key in ("other_dev", "own_dev"):
            if line.startswith(key + "="):
                found[key] = line.split("=", 1)[1].strip()
    missing = [k for k in ("other_dev", "own_dev") if not found.get(k, "").isdigit()]
    if missing:
        raise AssertionError(
            f"{', '.join(missing)} is not a device number, so `stat` failed "
            f"rather than answering\n--- probe ---\n{observed}"
        )
    return found["other_dev"], found["own_dev"]


def probe_output(stack) -> str:
    """The marked block out of the endpoint transcript, or a readable failure."""
    transcript = stack.endpoint.transcript()
    begin = transcript.find("CACHE_PROBE_BEGIN")
    end = transcript.find("CACHE_PROBE_END", begin + 1)
    if begin < 0 or end < 0:
        raise AssertionError(
            "the cache probe's output never reached the model, so the Bash "
            "tool did not run or its result was not sent back — this says "
            "nothing about the masks either way\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
    return transcript[begin:end]


@pytest.fixture
def seeded(stack):
    """Two user caches on the host side, the other one holding a file.

    Created here rather than at boot: the lean shape bypasses `entrypoint.sh`,
    nothing in the image makes the root, and `resolve_sandbox_cache_dir` checks
    that it is an existing writable directory on every task rather than once at
    load — so a directory made after the daemon is up is one the next task
    binds.

    `0700` on each, matching what the resolver creates and what the Ansible role
    sets on the root: every task runs as the same daemon uid, so the mode is
    about other local accounts rather than about this boundary.
    """
    stack.exec([
        "sh", "-c",
        f"mkdir -p {CACHE_ROOT}/{OWN_USER} {CACHE_ROOT}/{OTHER_USER} && "
        f"chmod 700 {CACHE_ROOT} {CACHE_ROOT}/{OWN_USER} {CACHE_ROOT}/{OTHER_USER} && "
        f"echo 'import os' > {CACHE_ROOT}/{OTHER_USER}/{PLANTED}",
    ])
    yield stack
    stack.exec(["sh", "-c", f"rm -rf {CACHE_ROOT} /data/repos/fake-venv"])


@pytest.mark.profile(profiles.CACHE.name)
class TestTheSiblingCacheMasks:
    @pytest.mark.script(CACHE_SCRIPT)
    def test_another_users_cache_is_empty_and_unwritable(self, seeded):
        stack = seeded
        task_id = stack.submit("look at the package caches")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)
        observed = probe_output(stack)

        other_dev, own_dev = _devices(observed)
        assert other_dev != own_dev, (
            f"{CACHE_ROOT}/{OTHER_USER} is on the same device as the task's "
            f"own cache ({other_dev}), so it is the bound directory rather than "
            "a mask — either bwrap was skipped, the cache bind was refused "
            "(check the daemon log for a `sandbox_cache_dir ... not binding "
            "it` warning), or the mask was not emitted.\n"
            f"--- probe ---\n{observed}\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
        assert "other_entries=[]" in observed, (
            f"{CACHE_ROOT}/{OTHER_USER} is a tmpfs but is not empty, so "
            f"something shows through the mask — {PLANTED} is the file the "
            f"next `uv sync` would hardlink out\n--- probe ---\n{observed}"
        )
        assert "other_writable=no" in observed, (
            "the sibling mask is writable, so a task can plant an archive at a "
            "path the mask only pretends to cover. `--remount-ro` was not "
            f"applied.\n--- probe ---\n{observed}"
        )

    @pytest.mark.script(CACHE_SCRIPT)
    def test_the_tasks_own_cache_still_works(self, seeded):
        """The positive half, and it is not decoration.

        Every assertion above is an absence, and each one is equally true of a
        cache that was never bound — which is what `resolve_sandbox_cache_dir`
        produces on any refusal, silently, with one warning in a log the test
        does not read. This is what says the bind is present and the placement
        is doing its job.
        """
        stack = seeded
        task_id = stack.submit("look at the package caches")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)
        observed = probe_output(stack)

        assert "own_writable=yes" in observed, (
            "the task's own cache is not writable, so the cache bind was "
            "refused or masked along with the siblings — every assertion in "
            "the test above then passes for the wrong reason\n"
            f"--- probe ---\n{observed}\n--- daemon logs ---\n{stack.logs(120)}"
        )
        assert "hardlink=ok" in observed, (
            "link(2) from the cache into a directory under repos_dir failed, "
            "so the two are on different mounts and uv will copy every wheel "
            "instead of hardlinking it. That is the entire cost the placement "
            f"exists to avoid.\n--- probe ---\n{observed}"
        )

    def test_the_other_users_cache_is_there_to_be_masked(self, seeded):
        """The control, in the same session rather than by hand.

        Reads the same paths through `docker compose exec`, which is the
        daemon's own view and not inside the sandbox: there the planted file is
        present and the directory is writable. So the test above is the
        difference the masks make, and not a statement about how the image is
        laid out or about a directory that was never populated.
        """
        stack = seeded
        result = stack.exec(["sh", "-c", CACHE_PROBE])

        assert result.returncode == 0, result.stderr
        other_dev, own_dev = _devices(result.stdout)
        assert other_dev == own_dev, (
            f"{CACHE_ROOT}/{OTHER_USER} is already on its own device in the "
            "daemon's own view, so the device comparison above cannot tell a "
            f"mask from the container's ordinary layout\n{result.stdout}"
        )
        assert PLANTED in result.stdout, (
            "the planted file is not in the daemon's own view either, so the "
            f"empty listing above is not about the masks\n{result.stdout}"
        )
        assert "other_writable=yes" in result.stdout, (
            "the other user's cache is not writable from the daemon's own view "
            f"either, so `other_writable=no` above proves nothing\n{result.stdout}"
        )
