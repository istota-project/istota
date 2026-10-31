"""The worktree reaper's namespace (#906), as the daemon's uid in the image.

`tests/linux/test_git_dir_routes.py` holds the routes as root in the Linux
runner, with a control per route. Here the same sweep runs under the shipped
seccomp profile and run contract, where a namespace that cannot start would read
as every repository failing to list and nothing ever being reaped. So the
ordinary merged worktree going is half of what this asks, and Bob's worktree
staying is the other half.
"""

from __future__ import annotations

import json

import pytest

from testbed import profiles

pytestmark = pytest.mark.smoke

ROOT = "/data/repos/smoke-906"

SWEEP = f"""
import json
import sys
from istota import executor
from istota.executor import _bwrap_available
from istota.maintenance import worktree_reaper
from istota.maintenance.worktree_reaper import reap_worktrees

available = _bwrap_available()
if "guards-off" in sys.argv:
    executor._bwrap_available = lambda: False
    worktree_reaper._is_within = lambda _path, _root: True

outcomes = reap_worktrees({ROOT!r}, retention_hours=1, now=__import__("time").time() + 7200)
print(json.dumps({{
    "bwrap": available,
    "removed": sorted(str(o.path) for o in outcomes if o.removed),
}}))
"""


def _command(*sweep_args: str) -> str:
    git ="git -c user.email=smoke@example.com -c user.name=Smoke"
    return f"""
set -eu
rm -rf {ROOT}
mkdir -p {ROOT}/upstream-holder
git init -q -b main {ROOT}/upstream-holder/up
cd {ROOT}/upstream-holder/up
echo base > f.txt
{git} add f.txt
{git} commit -q -m base
for user in alice bob; do
    bare={ROOT}/$user/ns/project.git
    mkdir -p {ROOT}/$user/ns
    git clone -q --bare {ROOT}/upstream-holder/up "$bare"
    git -C "$bare" config remote.origin.fetch '+refs/heads/*:refs/remotes/origin/*'
    git -C "$bare" fetch -q origin
    git -C "$bare" remote set-head origin -a
done
alice={ROOT}/alice/ns/project.git
bob={ROOT}/bob/ns/project.git
git -C "$alice" worktree add -q --detach {ROOT}/alice/ns/merged origin/main
git -C "$bob" worktree add -q -b feature {ROOT}/bob/ns/unmerged origin/main
cd {ROOT}/bob/ns/unmerged
echo work > work.txt
{git} add work.txt
{git} commit -q -m work

# A second clone of Alice's carries the links, so her first still shows the
# namespace doing its ordinary job on the same sweep.
planted={ROOT}/alice/ns/planted.git
git clone -q --bare {ROOT}/upstream-holder/up "$planted"
rm -rf "$planted/objects" "$planted/refs/heads"
ln -s "$bob/objects" "$planted/objects"
ln -s "$bob/refs/heads" "$planted/refs/heads"
ln -s "$bob/worktrees" "$planted/worktrees"
mkdir -p "$planted/refs/remotes/origin"
echo 'ref: refs/heads/feature' > "$planted/refs/remotes/origin/HEAD"

cd /app
uv run python -c {_quoted(SWEEP)} {" ".join(sweep_args)}
test -f {ROOT}/bob/ns/unmerged/work.txt && echo BOB_WORKTREE_KEPT
git -C "$bob" rev-parse --verify -q refs/heads/feature >/dev/null && echo BOB_BRANCH_KEPT
"""


def _quoted(text: str) -> str:
    return "'" + text.replace("'", "'\\''") + "'"


@pytest.mark.profile(profiles.FORGE.name)
class TestTheSweepRunsInTheNamespace:
    def test_it_reaps_its_own_and_leaves_the_other_users(self, stack):
        try:
            result = stack.exec(["sh", "-c", _command()], timeout=180)

            detail = f"{result.stdout}\n--- stderr ---\n{result.stderr[-4000:]}"
            lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
            assert lines, detail
            answer = json.loads(lines[-1])
            assert answer["bwrap"] is True, detail
            assert answer["removed"] == [f"{ROOT}/alice/ns/merged"], detail
            assert "BOB_WORKTREE_KEPT" in result.stdout, detail
            assert "BOB_BRANCH_KEPT" in result.stdout, detail
        finally:
            stack.exec(["sh", "-c", f"rm -rf {ROOT}"])

    def test_the_same_plant_removes_it_with_the_guards_off(self, stack):
        """The control: without this, the test above passes on a plant that
        does nothing in the image."""
        try:
            result = stack.exec(["sh", "-c", _command("guards-off")], timeout=180)

            detail = f"{result.stdout}\n--- stderr ---\n{result.stderr[-4000:]}"
            lines = [line for line in result.stdout.splitlines() if line.startswith("{")]
            assert lines, detail
            assert f"{ROOT}/bob/ns/unmerged" in json.loads(lines[-1])["removed"], detail
            assert "BOB_WORKTREE_KEPT" not in result.stdout, detail
            assert "BOB_BRANCH_KEPT" not in result.stdout, detail
        finally:
            stack.exec(["sh", "-c", f"rm -rf {ROOT}"])
