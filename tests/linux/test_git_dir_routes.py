"""A git directory one user wrote, against the two daemon-side callers (#906).

`developer.repos_dir` holds one subtree per user and each user's tasks write
their own. `worktree_reaper` sweeps every subtree and `repos_relocate` repairs
them, and a links-or-pointer plant needs no race. Each route here runs in the
namespace, where the victim survives, and with its guards off, where the same
plant does the damage. The second run is the control, and what it records is
what the route did before the guards. The reaper's worktree route has a second
guard beside the namespace, the record's scope check, so it is run with each one
alone as well. Two plants against the reaper do no damage even with the guards
off, and `test_two_plants_are_held_by_gits_own_answer` records that.
"""

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from types import MappingProxyType

import pytest

from istota import executor
from istota.maintenance import worktree_reaper
from istota.maintenance.repos_relocate import apply, plan, repair_after_copy
from istota.maintenance.worktree_reaper import reap_worktrees
from istota.sandbox import git_hardening

pytestmark = pytest.mark.linux

in_a_namespace = pytest.mark.skipif(sys.platform != "linux", reason="Linux witness")

GIT_ENV = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, **GIT_ENV},
    )
    assert proc.returncode == 0, f"git {' '.join(args)}: {proc.stderr}"
    return proc.stdout.strip()


def _has_ref(bare: Path, ref: str) -> bool:
    return subprocess.run(
        ["git", "-C", str(bare), "rev-parse", "--verify", "--quiet", ref],
        capture_output=True, env={**os.environ, **GIT_ENV},
    ).returncode == 0


def _age(path: Path) -> None:
    when = time.time() - 48 * 3600
    for target in [path, *path.rglob("*")]:
        try:
            os.utime(target, (when, when), follow_symlinks=False)
        except FileNotFoundError:
            pass


def _upstream(where: Path) -> Path:
    where.mkdir(parents=True)
    _git(where, "init", "-q", "-b", "main", ".")
    (where / "README").write_text("base\n")
    _git(where, "add", "README")
    _git(where, "commit", "-q", "-m", "init")
    return where


def _clone(root: Path, user: str, upstream: Path) -> Path:
    bare = root / user / "ns" / "project.git"
    bare.parent.mkdir(parents=True)
    _git(root, "clone", "-q", "--bare", str(upstream), str(bare))
    _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(bare, "fetch", "-q", "origin")
    _git(bare, "remote", "set-head", "origin", "-a")
    return bare


@pytest.fixture
def users(tmp_path):
    """Alice's clone, Bob's clone and his unmerged worktree, idle for two days."""
    tmp = tmp_path.resolve()
    upstream = _upstream(tmp / "upstream")
    root = tmp / "repos"
    alice = _clone(root, "alice", upstream)
    bob = _clone(root, "bob", upstream)
    worktree = bob.parent / "project--feature"
    _git(bob, "worktree", "add", "-q", "-b", "feature", str(worktree), "origin/main")
    (worktree / "work.txt").write_text("the only copy\n")
    _git(worktree, "add", "work.txt")
    _git(worktree, "commit", "-q", "-m", "not merged anywhere")
    _age(bob.parent)
    return root, alice, bob, worktree


def _links_into_bob(alice: Path, bob: Path) -> None:
    """Git reads Bob's worktrees and branches through Alice's clone, and
    `origin/HEAD` names his branch, so his head reads as merged."""
    (alice / "worktrees").symlink_to(bob / "worktrees", target_is_directory=True)
    shutil.rmtree(alice / "objects")
    (alice / "objects").symlink_to(bob / "objects", target_is_directory=True)
    shutil.rmtree(alice / "refs" / "heads")
    (alice / "refs" / "heads").symlink_to(bob / "refs" / "heads", target_is_directory=True)
    (alice / "refs" / "remotes" / "origin" / "HEAD").write_text("ref: refs/heads/feature\n")


def _pointer_into_bob(alice: Path, bob: Path) -> None:
    (alice / "commondir").write_text(f"{bob}\n")


def _alternates_only(alice: Path, bob: Path) -> None:
    (alice / "objects" / "info").mkdir(exist_ok=True)
    (alice / "objects" / "info" / "alternates").write_text(f"{bob / 'objects'}\n")


def _links_and_alternates_into_bob(alice: Path, bob: Path) -> None:
    """The same forged answer, reaching Bob's objects by an alternates line."""
    _alternates_only(alice, bob)
    (alice / "worktrees").symlink_to(bob / "worktrees", target_is_directory=True)
    shutil.rmtree(alice / "refs" / "heads")
    (alice / "refs" / "heads").symlink_to(bob / "refs" / "heads", target_is_directory=True)
    (alice / "refs" / "remotes" / "origin" / "HEAD").write_text("ref: refs/heads/feature\n")


REMOVING_PLANTS = [_links_into_bob, _links_and_alternates_into_bob]


class TestTheReaperAcrossUsers:
    @in_a_namespace
    @pytest.mark.parametrize("plant", [*REMOVING_PLANTS, _pointer_into_bob, _alternates_only])
    def test_a_plant_leaves_the_other_users_worktree(self, users, plant):
        root, alice, bob, worktree = users
        plant(alice, bob)
        _age(alice.parent)

        reap_worktrees(root, retention_hours=24)

        assert (worktree / "work.txt").is_file()
        assert _has_ref(bob, "refs/heads/feature")

    @pytest.mark.parametrize("plant", REMOVING_PLANTS)
    def test_the_plant_removes_it_with_neither_layer(self, users, plant, monkeypatch):
        root, alice, bob, worktree = users
        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        monkeypatch.setattr(worktree_reaper, "_is_within", lambda _path, _root: True)
        plant(alice, bob)
        _age(alice.parent)

        removed = {o.path for o in reap_worktrees(root, retention_hours=24) if o.removed}

        assert removed == {worktree}
        assert not worktree.exists()
        assert not _has_ref(bob, "refs/heads/feature")

    def test_the_scope_check_holds_without_the_namespace(self, users, monkeypatch):
        root, alice, bob, worktree = users
        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        _links_into_bob(alice, bob)
        _age(alice.parent)

        reap_worktrees(root, retention_hours=24)

        assert (worktree / "work.txt").is_file()
        assert _has_ref(bob, "refs/heads/feature")

    @in_a_namespace
    def test_the_namespace_holds_without_the_scope_check(self, users, monkeypatch):
        root, alice, bob, worktree = users
        monkeypatch.setattr(worktree_reaper, "_is_within", lambda _path, _root: True)
        _links_into_bob(alice, bob)
        _age(alice.parent)

        reap_worktrees(root, retention_hours=24)

        assert (worktree / "work.txt").is_file()
        assert _has_ref(bob, "refs/heads/feature")

    def test_two_plants_are_held_by_gits_own_answer(self, users, monkeypatch):
        """Recorded rather than guarded. A `commondir` makes the sweep ask about
        Bob's clone in Bob's own terms, with his `origin/HEAD`, and alternates
        alone name no worktree of his, so with both guards off his unmerged
        worktree still stays."""
        root, alice, bob, worktree = users
        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        monkeypatch.setattr(worktree_reaper, "_is_within", lambda _path, _root: True)
        _pointer_into_bob(alice, bob)
        _age(alice.parent)

        reap_worktrees(root, retention_hours=24)
        (alice / "commondir").unlink()
        _alternates_only(alice, bob)
        reap_worktrees(root, retention_hours=24)

        assert (worktree / "work.txt").is_file()
        assert _has_ref(bob, "refs/heads/feature")

    @in_a_namespace
    def test_an_ordinary_merged_worktree_is_still_reaped(self, users):
        """The reap goes through the namespace too, so this is what keeps the
        witnesses above from passing on a namespace that cannot do anything."""
        root, alice, _bob, _worktree = users
        merged = alice.parent / "project--merged"
        _git(alice, "worktree", "add", "-q", "--detach", str(merged), "origin/main")
        _age(alice.parent)

        outcomes = reap_worktrees(root, retention_hours=24)

        assert {o.path for o in outcomes if o.removed} == {merged}
        assert not merged.exists()


class TestWhatTheNamespaceCannotSee:
    """In the namespace `prunable` stops meaning the directory is gone: any path
    git cannot see there lists that way, and `git worktree prune` then drops the
    administrative entry of a checkout that is still in use."""

    @in_a_namespace
    def test_a_worktree_recorded_through_a_symlinked_root_is_kept(self, tmp_path):
        """A task's namespace binds the configured spelling, so that is what git
        records, even though the sweep's walk reports the resolved one."""
        tmp = tmp_path.resolve()
        real, link = tmp / "real", tmp / "link"
        real.mkdir()
        link.symlink_to(real, target_is_directory=True)
        bare = _clone(real, "alice", _upstream(tmp / "upstream"))
        worktree = bare.parent / "wt"
        _git(bare, "worktree", "add", "-q", "--detach", str(worktree), "origin/main")
        spelled = link / "alice" / "ns" / "wt"
        (bare / "worktrees" / "wt" / "gitdir").write_text(f"{spelled / '.git'}\n")

        reap_worktrees(link, retention_hours=24)

        assert (bare / "worktrees" / "wt").is_dir()

    @in_a_namespace
    def test_one_gone_worktree_does_not_unregister_a_live_one_outside(self, users, tmp_path):
        root, alice, _bob, _worktree = users
        gone = alice.parent / "gone"
        _git(alice, "worktree", "add", "-q", "--detach", str(gone), "origin/main")
        shutil.rmtree(gone)
        outside = tmp_path.resolve() / "workspace" / "live"
        _git(alice, "worktree", "add", "-q", "--detach", str(outside), "origin/main")

        reap_worktrees(root, retention_hours=24)

        assert (alice / "worktrees" / "live").is_dir()
        assert _git(outside, "rev-parse", "--is-inside-work-tree") == "true"

    @in_a_namespace
    def test_a_gone_worktree_is_still_pruned_when_nothing_is_outside(self, users):
        root, alice, _bob, _worktree = users
        gone = alice.parent / "gone"
        _git(alice, "worktree", "add", "-q", "--detach", str(gone), "origin/main")
        shutil.rmtree(gone)

        reap_worktrees(root, retention_hours=24)

        assert not (alice / "worktrees" / "gone").exists()


@pytest.fixture
def local_fetch(monkeypatch):
    """A fetch from a local path, standing in for an https remote the plant's
    owner runs. What it writes is the question, not how the bytes arrive."""
    monkeypatch.setattr(
        git_hardening, "GIT_SUBPROCESS_ENV",
        MappingProxyType({**git_hardening.GIT_SUBPROCESS_ENV, "GIT_ALLOW_PROTOCOL": "file"}),
    )
    # A local path is no configured forge, and without this no fetch would run at all.
    monkeypatch.setattr(
        "istota.maintenance.worktree_reaper._origin_is_a_forge", lambda *a, **k: True,
    )


class TestTheReapersFetch:
    def _aim_a_pruning_fetch_at_bob(self, root: Path, alice: Path, bob: Path) -> None:
        upstream = _upstream(root / "alice" / "remote")
        _git(alice, "config", "remote.origin.url", str(upstream))
        _git(alice, "config", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
        shutil.rmtree(alice / "refs" / "heads")
        (alice / "refs" / "heads").symlink_to(bob / "refs" / "heads", target_is_directory=True)

    @in_a_namespace
    def test_it_cannot_prune_the_other_users_branch(self, users, local_fetch):
        root, alice, bob, _worktree = users
        self._aim_a_pruning_fetch_at_bob(root, alice, bob)

        reap_worktrees(root, retention_hours=24)

        assert _has_ref(bob, "refs/heads/feature")

    def test_it_prunes_that_branch_without_the_namespace(
        self, users, local_fetch, monkeypatch
    ):
        root, alice, bob, _worktree = users
        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        self._aim_a_pruning_fetch_at_bob(root, alice, bob)

        reap_worktrees(root, retention_hours=24)

        assert not _has_ref(bob, "refs/heads/feature")

    @in_a_namespace
    def test_an_ordinary_fetch_still_lands(self, users, local_fetch):
        root, alice, _bob, _worktree = users
        upstream = _upstream(root / "alice" / "remote")
        _git(alice, "config", "remote.origin.url", str(upstream))
        wanted = _git(upstream, "rev-parse", "HEAD")

        reap_worktrees(root, retention_hours=24)

        assert _git(alice, "rev-parse", "refs/remotes/origin/main") == wanted


@pytest.fixture
def old_layout(tmp_path):
    """One namespace about to move, and a repository outside the tree."""
    tmp = tmp_path.resolve()
    upstream = _upstream(tmp / "upstream")
    root = tmp / "repos"
    root.mkdir()
    clone = root / "acme" / "widget.git"
    clone.parent.mkdir()
    _git(tmp, "clone", "-q", "--bare", str(upstream), str(clone))
    _git(clone, "worktree", "add", "-q", "-b", "wt", str(clone.parent / "widget--wt"), "main")

    victim = tmp / "victim" / "victim.git"
    victim.parent.mkdir()
    _git(tmp, "clone", "-q", "--bare", str(upstream), str(victim))
    victim_worktree = victim.parent / "victim--wt"
    _git(victim, "worktree", "add", "-q", "-b", "vwt", str(victim_worktree), "main")
    return root, clone, victim


def _nominate(clone: Path, admin_name: str) -> None:
    """A directory in the namespace, recorded as a worktree, whose `.git` names
    an entry under the clone's own `worktrees/`. That is what `_links_back`
    asks about, and the name no longer exists once the clone has moved."""
    planted = clone.parent / "planted"
    planted.mkdir()
    (planted / ".git").write_text(f"gitdir: {clone / 'worktrees' / admin_name}\n")
    record = clone / "worktrees" / "planted-record"
    record.mkdir(parents=True)
    (record / "gitdir").write_text(f"{planted / '.git'}\n")


def _admin_entry_links_to_victim(clone: Path, victim: Path) -> None:
    (clone / "worktrees" / "victim--wt").symlink_to(
        victim / "worktrees" / "victim--wt", target_is_directory=True
    )
    _nominate(clone, "victim--wt")


def _commondir_names_victim(clone: Path, victim: Path) -> None:
    (clone / "commondir").write_text(f"{victim}\n")
    _nominate(clone, "victim--wt")


REPAIR_PLANTS = [_admin_entry_links_to_victim, _commondir_names_victim]


class TestTheRepair:
    @in_a_namespace
    @pytest.mark.parametrize("plant", REPAIR_PLANTS)
    def test_a_plant_leaves_the_other_repository(self, old_layout, plant):
        root, clone, victim = old_layout
        record = victim / "worktrees" / "victim--wt" / "gitdir"
        before = record.read_text()
        plant(clone, victim)

        apply(plan(root, {"alice"}))

        assert record.read_text() == before

    @pytest.mark.parametrize("plant", REPAIR_PLANTS)
    def test_the_same_plant_redirects_it_without_the_namespace(
        self, old_layout, plant, monkeypatch
    ):
        root, clone, victim = old_layout
        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        record = victim / "worktrees" / "victim--wt" / "gitdir"
        plant(clone, victim)

        apply(plan(root, {"alice"}))

        assert "planted" in record.read_text()

    @in_a_namespace
    def test_an_ordinary_worktree_is_repaired_in_the_namespace(self, old_layout):
        root, clone, _victim = old_layout

        report = apply(plan(root, {"alice"}))

        moved = root / "alice" / "acme" / "widget--wt"
        assert report.repaired == (str(moved),)
        assert Path(_git(moved, "rev-parse", "--show-toplevel")).resolve() == moved

    def _copied_tree_with_a_plant(self, tmp_path) -> tuple[Path, Path, Path]:
        tmp = tmp_path.resolve()
        upstream = _upstream(tmp / "upstream")
        old, new = tmp / "old", tmp / "new"
        alice = _clone(new, "alice", upstream)
        bob = _clone(new, "bob", upstream)
        worktree = bob.parent / "project--wt"
        _git(bob, "worktree", "add", "-q", "-b", "wt", str(worktree), "origin/main")
        # The records name the old root, as they do after a copy.
        planted = alice.parent / "planted"
        planted.mkdir()
        (planted / ".git").write_text(
            f"gitdir: {old / 'alice' / 'ns' / 'project.git' / 'worktrees' / 'project--wt'}\n"
        )
        entry = alice / "worktrees" / "planted-record"
        entry.mkdir(parents=True)
        (entry / "gitdir").write_text(f"{old / 'alice' / 'ns' / 'planted' / '.git'}\n")
        (alice / "commondir").write_text(f"{bob}\n")
        return old, new, bob / "worktrees" / "project--wt" / "gitdir"

    @in_a_namespace
    def test_a_copied_tree_cannot_reach_from_one_user_into_another(self, tmp_path):
        """`repair_after_copy` runs over every user's subtree at once, so here
        the victim is the next user rather than a repository outside the tree."""
        old, new, record = self._copied_tree_with_a_plant(tmp_path)
        before = record.read_text()

        repair_after_copy(old, new)

        assert record.read_text() == before

    def test_a_copied_tree_reaches_the_next_user_without_the_namespace(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(executor, "_bwrap_available", lambda: False)
        old, new, record = self._copied_tree_with_a_plant(tmp_path)

        repair_after_copy(old, new)

        assert "planted" in record.read_text()
