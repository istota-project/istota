"""Another admin's repos subtree, looked for from inside a live task in the image.

`developer.repos_dir` is a root of per-user subtrees —
`{repos_dir}/{user_id}/{namespace}/{project}.git` — and `build_bwrap_cmd` binds
`{repos_dir}/{user_id}`, never the root. So another user's clones, worktrees,
model-written git configs and package cache are not in the namespace at all.
That is a stronger property than the one this file used to assert, and a simpler
one: there is nothing to mask, because there is nothing there.

**What this replaced, and why the shape of the assertion changed.** ISSUE-319
was the same exposure under the shared root: the cache bind sat inside a repos
bind emitted after it, so bwrap's argv ordering handed every user's cache to
every admin developer task, read-write, and uv trusts its own unpacked wheels on
read. The fix then was an empty read-only tmpfs over every *other* user's cache
directory, and this file asserted that the mask was there and covered. A mask is
a mount, so the old probe compared device numbers. A missing directory is not a
mount, so the probe now asks whether the path resolves at all — and `ls`
reporting `No such file or directory` is the answer, where an empty listing
would mean present and empty, which is what the old shape produced.

**Why this file rather than more argv assertions.** `tests/test_sandbox.py`
patches `_bwrap_available` and inspects the argv, so it proves the flags are
emitted and nothing about what the kernel does with them. The tier that runs a
real bwrap in the shipped image is this one.

Both halves are asserted, because either alone has a false pass. The negative
half — the other user's subtree is not there — is equally true of a task whose
sandbox never ran against `repos_dir` at all, which is what every refusal path
in `resolve_sandbox_cache_dir` and every non-admin task produces. The positive
half — the task's own cache is writable and `ln` from it into a directory beside
it succeeds — is what says the bind is present and still on one mount with the
venv, which is the whole reason the cache is derived inside the subtree.

The in-session control is not a formality either: it caught this file's first
probe under the old layout, which asked whether the sibling was a `tmpfs` and
could not fail, because `/data/repos` is on a tmpfs in this container to begin
with. Here the control reads the same paths through `docker compose exec`, where
the seeded subtree is present, populated and writable.
"""

from __future__ import annotations

import pytest

from testbed import profiles

pytestmark = pytest.mark.smoke

#: `CONTAINER_REPOS_DIR` in `testbed/services/gitlab.py`, which the forge
#: profile renders into `ISTOTA_DEVELOPER_REPOS_DIR`. Restated rather than
#: imported so a scenario reads as one thing; held equal by
#: `tests/test_smoke_tier.py::TestTheReposIsolationPathsAgree`.
REPOS_DIR = "/data/repos"
OWN_USER = "testuser"
OTHER_USER = "someone-else"

OWN_SUBTREE = f"{REPOS_DIR}/{OWN_USER}"
OTHER_SUBTREE = f"{REPOS_DIR}/{OTHER_USER}"

#: Derived by `resolve_sandbox_cache_dir`, not configured. Restated for the same
#: reason as the paths above; `executor.SANDBOX_CACHE_ROOT_NAME` is the source.
CACHE_NAME = ".package-caches"
OWN_CACHE = f"{OWN_SUBTREE}/{CACHE_NAME}"

#: What the other user's subtree holds. Two things, because the subtree carries
#: two different kinds of secret and the exposure argument is different for
#: each. In the cache, uv's unpacked-wheel store is trusted on read and
#: re-verified against no hash, so a file planted there is one the next
#: `uv sync` would hardlink into a venv and execute. In a clone's git config, a
#: credential is printed back by ordinary git commands, which is ISSUE-270's
#: half of the same tree.
PLANTED_NAMESPACE = "acme"
PLANTED_CLONE = "widget.git"
PLANTED_IN_CACHE = "sitecustomize.py"

#: Seven facts about four paths, read from inside the task.
#:
#: **`other_present` is the discriminator and the listing is the diagnosis.**
#: `ls -A` on a path that is not in the namespace exits non-zero and prints to
#: stderr; on a present-but-empty directory it exits 0 and prints nothing. The
#: old layout produced the second, so a test written against an empty listing
#: would pass under both and say nothing about which one it got. `other_entries`
#: carries `ls`'s error text into the marked block rather than swallowing it, so
#: a failure reads as `[ls: ...]` and can be told from `[]`. `other_cache_entries`
#: asks the same of the cache inside it, which is the directory ISSUE-319 was
#: actually about and the one a listing of the subtree does not descend into.
#:
#: `root_entries` is the same question asked from above: the shared root is a
#: mountpoint's parent that bwrap created on its own root tmpfs, so it exists
#: and holds exactly what was bound under it.
#:
#: The `ln` is the positive half and is the property the derivation is for. It
#: has to target a directory inside the user's own subtree but outside the
#: cache, which is what a worktree's venv is: on one mount it succeeds, and
#: across a mount boundary it is `EXDEV` however identical the filesystems are.
REPOS_PROBE = f"""
echo REPOS_PROBE_BEGIN
if ls -A {OTHER_SUBTREE} >/dev/null 2>&1; then
  echo "other_present=yes"
else
  echo "other_present=no"
fi
echo "other_entries=[$(ls -A {OTHER_SUBTREE} 2>&1 | tr '\\n' ' ')]"
echo "other_cache_entries=[$(ls -A {OTHER_SUBTREE}/{CACHE_NAME} 2>&1 | tr '\\n' ' ')]"
echo "root_entries=[$(ls -A {REPOS_DIR} 2>&1 | tr '\\n' ' ')]"
if touch {OTHER_SUBTREE}/probe 2>/dev/null; then
  echo "other_writable=yes"
  rm -f {OTHER_SUBTREE}/probe
else
  echo "other_writable=no"
fi
if touch {OWN_CACHE}/probe 2>/dev/null; then
  echo "own_writable=yes"
else
  echo "own_writable=no"
fi
mkdir -p {OWN_SUBTREE}/fake-venv 2>/dev/null
if ln {OWN_CACHE}/probe {OWN_SUBTREE}/fake-venv/linked 2>/dev/null; then
  echo "hardlink=ok nlink=$(stat -c %h {OWN_SUBTREE}/fake-venv/linked 2>&1)"
else
  echo "hardlink=failed"
fi
rm -f {OWN_CACHE}/probe {OWN_SUBTREE}/fake-venv/linked
echo REPOS_PROBE_END
"""

REPOS_SCRIPT = [
    {
        "tool_calls": [
            {
                "id": "call-1",
                "name": "Bash",
                "arguments": {"command": REPOS_PROBE},
            }
        ]
    },
    {"text": "I looked at the repos directory"},
]


def probe_output(stack) -> str:
    """The marked block out of the endpoint transcript, or a readable failure."""
    transcript = stack.endpoint.transcript()
    begin = transcript.find("REPOS_PROBE_BEGIN")
    end = transcript.find("REPOS_PROBE_END", begin + 1)
    if begin < 0 or end < 0:
        raise AssertionError(
            "the probe's output never reached the model, so the Bash tool did "
            "not run or its result was not sent back — this says nothing about "
            "the binds either way\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
    return transcript[begin:end]


@pytest.fixture
def seeded(stack):
    """A second user's repos subtree on the host side, with something in it.

    Created here rather than at boot: the lean shape bypasses `entrypoint.sh`
    and nothing in the image makes a per-user subtree for a user who has never
    run a task. The task's *own* subtree and its cache are deliberately not
    seeded — `resolve_sandbox_cache_dir` creates both on every task, and having
    the fixture make them would hide a daemon that had stopped doing so.

    `0700` on each, matching what the daemon creates: every task runs as the
    same daemon uid, so the mode is about other local accounts rather than about
    this boundary.
    """
    stack.exec([
        "sh", "-c",
        f"mkdir -p {OTHER_SUBTREE}/{CACHE_NAME} "
        f"{OTHER_SUBTREE}/{PLANTED_NAMESPACE}/{PLANTED_CLONE} && "
        f"chmod 700 {OTHER_SUBTREE} {OTHER_SUBTREE}/{CACHE_NAME} && "
        f"echo 'import os' > {OTHER_SUBTREE}/{CACHE_NAME}/{PLANTED_IN_CACHE} && "
        f"echo '[remote \"origin\"]' > "
        f"{OTHER_SUBTREE}/{PLANTED_NAMESPACE}/{PLANTED_CLONE}/config",
    ])
    yield stack
    stack.exec(["sh", "-c", f"rm -rf {OTHER_SUBTREE} {OWN_SUBTREE}/fake-venv"])


@pytest.mark.profile(profiles.FORGE.name)
class TestAnotherUsersSubtree:
    @pytest.mark.script(REPOS_SCRIPT)
    def test_it_is_not_in_the_namespace_at_all(self, seeded):
        stack = seeded
        task_id = stack.submit("look at the repos directory")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)
        observed = probe_output(stack)

        assert "other_present=no" in observed, (
            f"{OTHER_SUBTREE} resolves inside the sandbox. Either the bind is "
            "the shared root again, or bwrap was skipped — check the daemon log "
            f"for both.\n--- probe ---\n{observed}\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
        assert "other_entries=[]" not in observed, (
            f"{OTHER_SUBTREE} listed as present and empty, which is what a mask "
            "over it would look like rather than an absence. The property this "
            "layout claims is that the path is not there at all.\n"
            f"--- probe ---\n{observed}"
        )
        assert "other_cache_entries=[]" not in observed, (
            f"{OTHER_SUBTREE}/{CACHE_NAME} listed as present and empty. That is "
            "the ISSUE-319 shape exactly — a mask over another user's package "
            "cache rather than a cache that is not in the namespace.\n"
            f"--- probe ---\n{observed}"
        )
        assert f"{OTHER_USER}" not in _root_entries(observed), (
            f"{OTHER_USER} is an entry of {REPOS_DIR} inside the sandbox, so "
            "the shared root was bound and the per-user split bought nothing\n"
            f"--- probe ---\n{observed}"
        )
        assert "other_writable=no" in observed, (
            f"a file was created under {OTHER_SUBTREE} from inside the task, "
            f"which is the planting half of ISSUE-319\n--- probe ---\n{observed}"
        )

    @pytest.mark.script(REPOS_SCRIPT)
    def test_the_tasks_own_subtree_still_works(self, seeded):
        """The positive half, and it is not decoration.

        Every assertion above is an absence, and each one is equally true of a
        task whose sandbox bound nothing under `repos_dir` — a non-admin, a
        refused cache, a bwrap that was skipped. This is what says the bind is
        present, the cache is inside it, and the two are on one mount.
        """
        stack = seeded
        task_id = stack.submit("look at the repos directory")
        stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=180)
        observed = probe_output(stack)

        assert OWN_USER in _root_entries(observed), (
            f"{OWN_SUBTREE} is not in the namespace either, so the test above "
            "passes because nothing under repos_dir was bound at all\n"
            f"--- probe ---\n{observed}\n--- daemon logs ---\n{stack.logs(120)}"
        )
        assert "own_writable=yes" in observed, (
            "the task's own package cache is not writable, so the cache bind "
            "was refused — every assertion in the test above then passes for "
            f"the wrong reason\n--- probe ---\n{observed}\n"
            f"--- daemon logs ---\n{stack.logs(120)}"
        )
        assert "hardlink=ok" in observed, (
            "link(2) from the cache into a directory beside it failed, so the "
            "two are on different mounts and uv will copy every wheel instead "
            "of hardlinking it. That is the entire cost the derivation exists "
            f"to avoid.\n--- probe ---\n{observed}"
        )

    def test_the_other_users_subtree_is_there_to_be_missing(self, seeded):
        """The control, in the same session rather than by hand.

        Reads the same paths through `docker compose exec`, which is the
        daemon's own view and not inside the sandbox: there the seeded subtree
        is present, holds both planted files and is writable. So the test above
        is the difference the per-user bind makes, and not a statement about a
        directory that was never created.
        """
        stack = seeded
        result = stack.exec(["sh", "-c", REPOS_PROBE])

        assert result.returncode == 0, result.stderr
        assert "other_present=yes" in result.stdout, (
            f"{OTHER_SUBTREE} is missing from the daemon's own view too, so "
            f"`other_present=no` above proves nothing\n{result.stdout}"
        )
        assert PLANTED_IN_CACHE in result.stdout, (
            f"{PLANTED_IN_CACHE} is not in {OTHER_SUBTREE}/{CACHE_NAME} in the "
            "daemon's own view either, so the empty-cache assertion above is "
            f"not about the binds\n{result.stdout}"
        )
        assert PLANTED_NAMESPACE in result.stdout, (
            f"the seeded {PLANTED_NAMESPACE}/ namespace is not in the daemon's "
            f"own view either\n{result.stdout}"
        )
        assert "other_writable=yes" in result.stdout, (
            "the other user's subtree is not writable from the daemon's own "
            f"view either, so `other_writable=no` above proves nothing\n"
            f"{result.stdout}"
        )
        assert OTHER_USER in _root_entries(result.stdout), (
            f"{OTHER_USER} is not an entry of {REPOS_DIR} in the daemon's own "
            "view, so the root listing above cannot tell a per-user bind from "
            f"an empty tree\n{result.stdout}"
        )


def _root_entries(observed: str) -> str:
    """The `root_entries=[...]` payload, or a readable failure.

    Returned as the raw string rather than a list: the assertions ask whether a
    user id appears, and a missing line has to be an error rather than an empty
    answer that reads as "the other user was not there".
    """
    for line in observed.splitlines():
        if line.startswith("root_entries="):
            return line.split("=", 1)[1]
    raise AssertionError(
        "the probe printed no root_entries line, so `ls` did not run\n"
        f"--- probe ---\n{observed}"
    )
