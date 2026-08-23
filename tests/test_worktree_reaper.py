"""ISSUE-288 — nothing reaps the worktrees under `DEVELOPER_REPOS_DIR`.

Every developer task cuts a worktree from a bare clone and no task removes
another's, so a repos directory accumulates gigabyte checkouts with no owner.
The entry that prompted this was `istota--main`, 1.3 GB, detached at a commit
already on `origin/HEAD`, sitting untouched for a day.

The whole risk here is on the delete side, so most of this file is the *hold
back* cases rather than the reap ones. A reaper that only proved it can remove
a merged worktree passes just as well while deleting one holding the only copy
of a day's work. Each guard below is a separate reason to refuse, and each is
tested against a real repository rather than a parsed string, because the
question "has this landed upstream" is git's to answer and the whole design
rests on getting that one call right.

`git cherry` is the merged test rather than `merge-base --is-ancestor`: a
squash or rebase merge is the normal way an MR lands, and a rebased branch is
not an ancestor of anything while every one of its commits has a patch-id
equivalent upstream. The ancestor test alone would reap almost nothing.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

from istota.worktree_reaper import (
    parse_worktree_list,
    reap_and_report,
    reap_worktrees,
)

GIT_ISOLATION = {
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_TERMINAL_PROMPT": "0",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
}


def _git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=str(cwd), capture_output=True, text=True,
        env={**os.environ, **GIT_ISOLATION},
    )
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{proc.stderr}")
    return proc.stdout


def _upstream(tmp_path: Path) -> Path:
    """A real upstream with one commit on `main`."""
    up = tmp_path / "upstream"
    up.mkdir()
    _git(up, "init", "-q", "-b", "main", ".")
    (up / "README").write_text("base\n")
    _git(up, "add", "README")
    _git(up, "commit", "-q", "-m", "init")
    return up


def _bare_clone(tmp_path: Path, upstream: Path) -> Path:
    """A bare clone in the documented layout, wired the way the skill wires it.

    The refspec and `remote set-head` are what make `refs/remotes/origin/HEAD`
    resolve; `clone --bare` alone creates no remote-tracking refs at all. The
    reaper reads only that ref, so a clone missing this setup is one it must
    refuse to act on — see `test_refuses_when_origin_head_does_not_resolve`.
    """
    bare = tmp_path / "repos" / "namespace" / "project.git"
    bare.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "-q", "--bare", str(upstream), str(bare))
    _git(bare, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
    _git(bare, "fetch", "-q", "origin")
    _git(bare, "remote", "set-head", "origin", "-a")
    return bare


@pytest.fixture
def repos(tmp_path):
    """`(repos_dir, bare, upstream)` — the documented on-disk layout."""
    upstream = _upstream(tmp_path)
    bare = _bare_clone(tmp_path, upstream)
    return tmp_path / "repos", bare, upstream


def _worktree(bare: Path, name: str, start: str, branch: str | None = None) -> Path:
    """A worktree beside the bare clone, on a branch or detached."""
    path = bare.parent / name
    if branch:
        _git(bare, "worktree", "add", "-q", "-b", branch, str(path), start)
    else:
        _git(bare, "worktree", "add", "-q", "--detach", str(path), start)
    return path


def _age(path: Path, hours: float) -> None:
    """Backdate everything the activity probe looks at.

    The retention window is measured from the last *git* activity, which lives
    in the worktree's administrative directory under the bare clone, not in the
    checkout. Backdating the checkout alone leaves a worktree the reaper still
    considers active, which is how the first version of this helper produced a
    suite that passed against a reaper with no age guard at all.

    Directories are backdated too, not just files. `_touched_since` stats every
    directory it walks, and creating a child updates its parent's mtime — so a
    test that builds `web/node_modules` leaves `web/` looking freshly touched
    and the worktree is held on `recent` no matter what the dirty check says.
    Backdating a directory does not disturb its parent, so the order here is
    irrelevant.
    """
    when = time.time() - hours * 3600
    targets = [path]
    dotgit = path / ".git"
    if dotgit.is_file():
        gitdir = Path(dotgit.read_text().split(":", 1)[1].strip())
        targets.append(gitdir)
        targets.extend(gitdir.rglob("*"))
    targets.extend(path.rglob("*"))
    for target in targets:
        os.utime(target, (when, when))


def _names(outcomes) -> set[str]:
    return {o.path.name for o in outcomes if o.removed}


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------

class TestParse:

    def test_reads_a_real_porcelain_listing(self, repos):
        _, bare, _ = repos
        _worktree(bare, "project--topic", "origin/main", branch="topic")
        records = parse_worktree_list(_git(bare, "worktree", "list", "--porcelain", "-z"))

        by_name = {r.path.name: r for r in records}
        assert by_name["project.git"].bare is True
        topic = by_name["project--topic"]
        assert topic.bare is False
        assert topic.branch == "topic"
        assert len(topic.head) == 40

    def test_reads_detached_and_locked(self, repos):
        _, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        _git(bare, "worktree", "lock", str(path))
        record = {
            r.path.name: r
            for r in parse_worktree_list(_git(bare, "worktree", "list", "--porcelain", "-z"))
        }["project--main"]
        assert record.branch == ""
        assert record.locked is True

    def test_a_newline_in_a_path_cannot_forge_a_record(self):
        """The reason the listing is read `-z`. Git does not quote a newline in
        a worktree path in the line-oriented form — verified against real git —
        so a path containing one truncates its own record and the remainder is
        read as a fresh `worktree` line. That buys a record whose *path* is one
        checkout and whose *head* is another: the age and dirty checks run
        against the victim, the merged check against a head chosen to pass.

        Paths under `repos_dir` are chosen by the model, so this is reachable.
        In the NUL form the newline is just a byte inside one field."""
        forged = (
            "worktree /repos/ns/p--evil\nworktree /repos/ns/p--victim\0"
            "HEAD " + "b" * 40 + "\0detached\0\0"
        )
        records = parse_worktree_list(forged)

        assert len(records) == 1
        assert "\n" in str(records[0].path)
        assert records[0].head == "b" * 40

    def test_a_duplicate_path_refuses_the_whole_listing(self):
        """Not a state git produces, so it means something built it. The damage
        is the same as the forged record above, and refusing the repository is
        cheaper than reasoning about which of the two entries is real."""
        duplicated = (
            "worktree /repos/ns/p--one\0HEAD " + "a" * 40 + "\0detached\0\0"
            "worktree /repos/ns/p--one\0HEAD " + "b" * 40 + "\0detached\0\0"
        )
        assert parse_worktree_list(duplicated) is None

    def test_reads_prunable(self):
        records = parse_worktree_list(
            "worktree /a\0HEAD " + "a" * 40 + "\0detached\0prunable gitdir file removed\0\0"
        )
        assert records[0].prunable is True

    def test_garbage_yields_no_records_rather_than_raising(self):
        assert parse_worktree_list("") == []
        assert parse_worktree_list("branch refs/heads/orphan\0HEAD deadbeef\0") == []


# --------------------------------------------------------------------------
# What gets reaped
# --------------------------------------------------------------------------

class TestReaps:

    def test_reaps_a_detached_worktree_already_on_origin_head(self, repos):
        """The `istota--main` case from the entry, exactly: clean, detached at
        a commit on origin/HEAD, carrying no commits of its own."""
        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == {"project--main"}
        assert not path.exists()

    def test_reaps_a_worktree_holding_only_a_node_modules(self, repos):
        """The reported case. An install is the whole reason a worktree gets
        big, and before this the install was also what pinned it forever: the
        `--ignored` half of the dirty check saw `node_modules` and held."""
        repos_dir, bare, upstream = repos
        (upstream / ".gitignore").write_text("node_modules/\n")
        _git(upstream, "add", ".gitignore")
        _git(upstream, "commit", "-q", "-m", "ignore node_modules")
        _git(bare, "fetch", "-q", "origin")

        path = _worktree(bare, "project--main", "origin/main")
        (path / "node_modules" / "pkg" / "nested").mkdir(parents=True)
        (path / "node_modules" / "pkg" / "nested" / "index.js").write_text("x\n")
        (path / "node_modules" / ".package-lock.json").write_text("{}\n")
        assert _git(path, "status", "--porcelain", "-uall").strip() == "", (
            "precondition: only --ignored can see this tree"
        )
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == {"project--main"}
        assert not path.exists()

    def test_reaps_a_worktree_holding_the_caches_production_actually_had(self, repos):
        """Measured, not guessed. The one worktree held on the production host
        was 941 MB, 882 MB of it `.venv`, and its entire ignored listing was
        `.venv/`, `.pytest_cache/`, `.ruff_cache/` and 30-odd `__pycache__/`
        directories — no `.env` and nothing untracked. That worktree is what
        this test is, and before the fix it was unreapable for good."""
        repos_dir, bare, upstream = repos
        (upstream / ".gitignore").write_text(
            ".venv/\n__pycache__/\n.pytest_cache/\n.ruff_cache/\n.mypy_cache/\n"
        )
        (upstream / "src").mkdir()
        (upstream / "src" / "mod.py").write_text("x = 1\n")
        _git(upstream, "add", ".gitignore", "src/mod.py")
        _git(upstream, "commit", "-q", "-m", "ignore caches")
        _git(bare, "fetch", "-q", "origin")

        path = _worktree(bare, "project--main", "origin/main")
        for name in (".venv", ".pytest_cache", ".ruff_cache", ".mypy_cache"):
            (path / name).mkdir()
            (path / name / "content").write_text("x\n")
        (path / "src" / "__pycache__").mkdir()
        (path / "src" / "__pycache__" / "mod.pyc").write_text("x\n")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == {"project--main"}
        assert not path.exists()

    def test_reaps_a_node_modules_nested_below_the_worktree_root(self, repos):
        """This repository keeps its frontend in `web/`, so the install lands
        at `web/node_modules` rather than the root. The entry git reports is
        the whole path, so the check reads its last component."""
        repos_dir, bare, upstream = repos
        (upstream / ".gitignore").write_text("node_modules/\n")
        (upstream / "web").mkdir()
        (upstream / "web" / "package.json").write_text("{}\n")
        _git(upstream, "add", ".gitignore", "web/package.json")
        _git(upstream, "commit", "-q", "-m", "web")
        _git(bare, "fetch", "-q", "origin")

        path = _worktree(bare, "project--main", "origin/main")
        (path / "web" / "node_modules" / "pkg").mkdir(parents=True)
        (path / "web" / "node_modules" / "pkg" / "index.js").write_text("x\n")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == {"project--main"}
        assert not path.exists()

    def test_reaps_a_node_modules_ignored_by_a_glob_pattern(self, repos):
        """`node_modules/**` and `node_modules/*` are both common, and neither
        matches the directory itself — so git collapses at the *children* and
        reports `!! node_modules/pkg/`. A check reading only the last component
        sees `pkg`, holds the worktree, and leaves the reported bug in place for
        every repository whose gitignore is written that way."""
        repos_dir, bare, upstream = repos
        (upstream / ".gitignore").write_text("node_modules/**\n")
        _git(upstream, "add", ".gitignore")
        _git(upstream, "commit", "-q", "-m", "ignore node_modules glob")
        _git(bare, "fetch", "-q", "origin")

        path = _worktree(bare, "project--main", "origin/main")
        (path / "node_modules" / "pkg").mkdir(parents=True)
        (path / "node_modules" / "pkg" / "index.js").write_text("x\n")
        assert _git(path, "status", "--porcelain", "-uall").strip() == "", (
            "precondition: only --ignored can see this tree"
        )
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == {"project--main"}
        assert not path.exists()

    def test_reaps_a_merged_branch_and_deletes_its_ref(self, repos):
        repos_dir, bare, upstream = repos
        path = _worktree(bare, "project--topic", "origin/main", branch="topic")
        (path / "feature").write_text("x\n")
        _git(path, "add", "feature")
        _git(path, "commit", "-q", "-m", "feature")
        _git(upstream, "fetch", "-q", str(path), "topic")
        _git(upstream, "merge", "-q", "--ff-only", "FETCH_HEAD")
        _git(bare, "fetch", "-q", "origin")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == {"project--topic"}
        assert not path.exists()
        assert "topic" not in _git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads/")

    def test_reaps_a_rebased_branch_whose_commits_landed_upstream(self, repos):
        """A rebase or squash merge is how an MR normally lands. The branch is
        not an ancestor of origin/HEAD afterwards, but every commit on it has a
        patch-id equivalent there, so nothing is lost by removing it.

        This is the case `merge-base --is-ancestor` gets wrong, and getting it
        wrong means the reaper never fires on a real repository."""
        repos_dir, bare, upstream = repos
        path = _worktree(bare, "project--topic", "origin/main", branch="topic")
        (path / "feature").write_text("x\n")
        _git(path, "add", "feature")
        _git(path, "commit", "-q", "-m", "feature")

        # Upstream moves, then replays the branch's patch on top of it: same
        # change, different sha, no ancestry between the two.
        (upstream / "other").write_text("y\n")
        _git(upstream, "add", "other")
        _git(upstream, "commit", "-q", "-m", "other")
        _git(upstream, "fetch", "-q", str(path), "topic")
        _git(upstream, "cherry-pick", "FETCH_HEAD")
        _git(bare, "fetch", "-q", "origin")

        branch_head = _git(path, "rev-parse", "HEAD").strip()
        upstream_head = _git(bare, "rev-parse", "refs/remotes/origin/HEAD").strip()
        assert branch_head != upstream_head
        merge_base = _git(bare, "merge-base", branch_head, upstream_head).strip()
        assert merge_base != branch_head, "precondition: not an ancestor"

        _age(path, 48)
        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == {"project--topic"}

    def test_prunes_an_administrative_entry_whose_directory_is_gone(self, repos):
        """A worktree deleted with `rm -rf` leaves a record behind, and every
        later `worktree list` carries it. Pruning those is unconditionally safe
        — there is no checkout left to lose."""
        import shutil

        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--gone", "origin/main", branch="gone")
        shutil.rmtree(path)
        assert "project--gone" in _git(bare, "worktree", "list", "--porcelain")

        reap_worktrees(repos_dir, retention_hours=24)

        assert "project--gone" not in _git(bare, "worktree", "list", "--porcelain")

    def test_reaps_across_several_bare_clones(self, repos, tmp_path):
        repos_dir, bare, upstream = repos
        second = tmp_path / "repos" / "other" / "second.git"
        second.parent.mkdir(parents=True, exist_ok=True)
        _git(tmp_path, "clone", "-q", "--bare", str(upstream), str(second))
        _git(second, "config", "remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        _git(second, "fetch", "-q", "origin")
        _git(second, "remote", "set-head", "origin", "-a")

        first = _worktree(bare, "project--main", "origin/main")
        other = _worktree(second, "second--main", "origin/main")
        _age(first, 48)
        _age(other, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == {"project--main", "second--main"}


# --------------------------------------------------------------------------
# What must survive. Each of these is a separate reason to refuse.
# --------------------------------------------------------------------------

class TestHoldsBack:

    def test_keeps_a_branch_with_commits_not_upstream(self, repos):
        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--topic", "origin/main", branch="topic")
        (path / "feature").write_text("x\n")
        _git(path, "add", "feature")
        _git(path, "commit", "-q", "-m", "unmerged work")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert path.exists()
        held = [o for o in outcomes if o.path == path]
        assert held and held[0].reason == "unmerged"

    def test_keeps_a_worktree_with_an_uncommitted_change(self, repos):
        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        (path / "README").write_text("edited\n")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert (path / "README").read_text() == "edited\n"
        assert [o.reason for o in outcomes if o.path == path] == ["dirty"]

    def test_keeps_a_worktree_holding_only_untracked_files(self, repos):
        """The commonest way a worktree holds unrecoverable work: a scratch
        script, a captured log, a patch never added. `status --porcelain` with
        untracked files included is the whole guard."""
        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        (path / "notes.txt").write_text("findings\n")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert (path / "notes.txt").exists()

    def test_keeps_a_worktree_whose_untracked_file_is_gitignored(self, repos):
        """`--ignored` too: a `.env` or a built `.venv` is exactly what a
        gitignore hides, and losing one is losing work the model cannot redo."""
        repos_dir, bare, upstream = repos
        # Committed upstream, so the worktree stays clean and the only thing
        # in it is the ignored file itself. A `.gitignore` left uncommitted
        # would make this pass on the plain untracked rule instead.
        (upstream / ".gitignore").write_text(".env\n")
        _git(upstream, "add", ".gitignore")
        _git(upstream, "commit", "-q", "-m", "ignore env")
        _git(bare, "fetch", "-q", "origin")

        path = _worktree(bare, "project--main", "origin/main")
        (path / ".env").write_text("SECRET=x\n")
        assert _git(path, "status", "--porcelain", "-uall").strip() == "", (
            "precondition: only --ignored can see this file"
        )
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert (path / ".env").exists()

    def test_keeps_an_ignored_file_beside_a_reconstructible_directory(self, repos):
        """The discount is per-entry, not a mode the worktree enters. A `.env`
        sitting next to a `node_modules` is still the only copy of itself, and
        one discountable entry must not carry the rest of the listing with it.
        """
        repos_dir, bare, upstream = repos
        (upstream / ".gitignore").write_text(".env\nnode_modules/\n")
        _git(upstream, "add", ".gitignore")
        _git(upstream, "commit", "-q", "-m", "ignore env and node_modules")
        _git(bare, "fetch", "-q", "origin")

        path = _worktree(bare, "project--main", "origin/main")
        (path / "node_modules" / "pkg").mkdir(parents=True)
        (path / "node_modules" / "pkg" / "index.js").write_text("x\n")
        (path / ".env").write_text("SECRET=x\n")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert (path / ".env").exists()
        assert [o.reason for o in outcomes if o.path == path] == ["dirty"]

    def test_keeps_a_worktree_with_a_staged_rename_beside_a_node_modules(self, repos):
        """Porcelain v1 `-z` emits a rename as *two* NUL-separated fields —
        `R  <new>\\0<orig>\\0` — so the parse sees a record that is a bare path
        with no status prefix at all. It is safe only because the `R ` record
        settles the answer first, and nothing else in the suite pins that
        ordering; a rewrite that classified records independently would read
        the bare origin path as unrecognized and could regress silently."""
        repos_dir, bare, upstream = repos
        (upstream / ".gitignore").write_text("node_modules/\n")
        (upstream / "original.txt").write_text("content\n")
        _git(upstream, "add", ".gitignore", "original.txt")
        _git(upstream, "commit", "-q", "-m", "seed")
        _git(bare, "fetch", "-q", "origin")

        path = _worktree(bare, "project--main", "origin/main")
        (path / "node_modules").mkdir()
        (path / "node_modules" / "index.js").write_text("x\n")
        _git(path, "mv", "original.txt", "renamed.txt")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert (path / "renamed.txt").exists()
        assert [o.reason for o in outcomes if o.path == path] == ["dirty"]

    def test_keeps_an_ignored_file_whose_name_matches_a_discounted_directory(self, repos):
        """The discount is for directories. A *file* called `node_modules` is
        not a package tree, and git distinguishes the two with the trailing
        slash — so the check must too, rather than matching on the name alone.
        """
        repos_dir, bare, upstream = repos
        (upstream / ".gitignore").write_text("node_modules\n")
        _git(upstream, "add", ".gitignore")
        _git(upstream, "commit", "-q", "-m", "ignore node_modules")
        _git(bare, "fetch", "-q", "origin")

        path = _worktree(bare, "project--main", "origin/main")
        (path / "node_modules").write_text("not a directory\n")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert (path / "node_modules").read_text() == "not a directory\n"
        assert [o.reason for o in outcomes if o.path == path] == ["dirty"]

    def test_keeps_a_worktree_holding_an_ignored_dist_directory(self, repos):
        """`dist`, `build` and `target` are deliberately not on the list. Each
        is an ordinary source directory name in some projects, and the cost of
        a false positive here is deleting real work, not re-running a build."""
        repos_dir, bare, upstream = repos
        (upstream / ".gitignore").write_text("dist/\n")
        _git(upstream, "add", ".gitignore")
        _git(upstream, "commit", "-q", "-m", "ignore dist")
        _git(bare, "fetch", "-q", "origin")

        path = _worktree(bare, "project--main", "origin/main")
        (path / "dist").mkdir()
        (path / "dist" / "index.html").write_text("<html>\n")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert (path / "dist" / "index.html").exists()
        assert [o.reason for o in outcomes if o.path == path] == ["dirty"]

    def test_keeps_a_worktree_whose_ignored_directory_is_unrecognized(self, repos):
        """An explicit list of names, never "ignore every `!!` line". The
        gitignore of this very repository hides `data/` and `lib/`, so a rule
        that discounted ignored directories as a class would delete the real
        thing on the first repository that keeps one."""
        repos_dir, bare, upstream = repos
        (upstream / ".gitignore").write_text("data/\n")
        _git(upstream, "add", ".gitignore")
        _git(upstream, "commit", "-q", "-m", "ignore data")
        _git(bare, "fetch", "-q", "origin")

        path = _worktree(bare, "project--main", "origin/main")
        (path / "data").mkdir()
        (path / "data" / "captured.sqlite").write_text("rows\n")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert (path / "data" / "captured.sqlite").exists()
        assert [o.reason for o in outcomes if o.path == path] == ["dirty"]

    def test_keeps_a_worktree_whose_only_untracked_file_is_named_a_space(self, repos):
        """A filename may legally be a single space, which git renders as the
        four-character `-z` record `"??  "` — a path that is entirely
        whitespace, immediately after the discount made this parse care where
        a record ends. It holds the worktree like any other untracked file."""
        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        (path / " ").write_text("the only copy\n")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert (path / " ").read_text() == "the only copy\n"
        assert [o.reason for o in outcomes if o.path == path] == ["dirty"]

    def test_keeps_a_worktree_whose_only_activity_is_inside_a_discounted_dir(self, repos):
        """The regression the ISSUE-304 discount opens if the two name lists
        are allowed to overlap, and the reason `_WALK_SKIP` subtracts
        `_RECONSTRUCTIBLE_DIRS`.

        `os.walk` prunes by name *before* anything stats the directory, so a
        name on both lists is invisible to both guards at once: the activity
        walk never reads it and the dirty check discounts it. An `npm install`,
        a `uv sync` into an existing `.venv`, or a pytest run writing only
        `__pycache__` touches nothing else in the checkout — so the worktree
        would read as idle and clean, and be deleted with the install still
        running. Before the discount the dirty check held it, which is what
        made pruning it free.
        """
        repos_dir, bare, upstream = repos
        (upstream / ".gitignore").write_text("node_modules/\n")
        _git(upstream, "add", ".gitignore")
        _git(upstream, "commit", "-q", "-m", "ignore node_modules")
        _git(bare, "fetch", "-q", "origin")

        path = _worktree(bare, "project--main", "origin/main")
        (path / "node_modules" / "pkg").mkdir(parents=True)
        (path / "node_modules" / "pkg" / "index.js").write_text("x\n")
        _age(path, 48)

        # The install is in flight: only paths inside `node_modules` are fresh.
        now = time.time()
        for target in (
            path / "node_modules",
            path / "node_modules" / "pkg",
            path / "node_modules" / "pkg" / "index.js",
        ):
            os.utime(target, (now, now))

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert path.exists()
        assert [o.reason for o in outcomes if o.path == path] == ["recent"]

    def test_the_two_name_lists_are_disjoint(self):
        """The invariant behind the test above, asserted directly so a future
        edit to either list cannot quietly reintroduce the overlap. A name the
        dirty check discounts must stay in the activity walk, because the walk
        is then the only guard left."""
        import istota.worktree_reaper as mod

        assert not (mod._WALK_SKIP & mod._RECONSTRUCTIBLE_DIRS)
        assert ".git" in mod._WALK_SKIP

    def test_keeps_a_locked_worktree(self, repos):
        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        _git(bare, "worktree", "lock", str(path), "--reason", "inspection")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert path.exists()
        assert [o.reason for o in outcomes if o.path == path] == ["locked"]

    def test_keeps_a_worktree_inside_the_retention_window(self, repos):
        """The age guard is what protects a sibling task running right now.
        Two developer tasks for one user run concurrently, and neither knows
        the other exists."""
        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert path.exists()
        assert [o.reason for o in outcomes if o.path == path] == ["recent"]

    def test_activity_inside_the_checkout_counts_as_recent(self, repos):
        """A long-running task that has not committed for a day is still
        working. Backdate everything, then touch one file the way an edit
        would, and the worktree must survive."""
        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        _age(path, 48)
        (path / "README").write_text("base\n")  # rewritten, same content: clean

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert path.exists()

    def test_git_activity_alone_counts_as_recent(self, repos):
        """The other half of the activity probe. A task that only runs git —
        a commit, a rebase, a checkout — touches nothing in the working tree,
        so the checkout's own mtimes go on looking a day old while the task is
        very much alive. The administrative directory under the bare clone is
        where that shows up."""
        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        _age(path, 48)

        gitdir = Path((path / ".git").read_text().split(":", 1)[1].strip())
        os.utime(gitdir / "index", None)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert path.exists()

    def test_git_refuses_the_removal_when_this_modules_check_was_wrong(self, repos, monkeypatch):
        """Defence in depth, and the reason `worktree remove` is called without
        `--force`. Every check above runs before the removal, so a worktree can
        be edited in the gap — a sibling task, a human at a shell. Git's own
        refusal is the last thing standing there, and `--force` would remove
        it. Simulated by making the dirty check lie, which is what a race
        looks like from here."""
        import istota.worktree_reaper as mod

        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        (path / "README").write_text("uncommitted work\n")
        _age(path, 48)
        monkeypatch.setattr(mod, "_is_dirty", lambda _wt: False)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert (path / "README").read_text() == "uncommitted work\n"
        assert [o.reason for o in outcomes if o.path == path] == ["failed"]

    def test_keeps_a_protected_path(self, repos):
        """The running task's own worktree, whatever its age or state."""
        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24, protect=[path])

        assert _names(outcomes) == set()
        assert path.exists()
        assert [o.reason for o in outcomes if o.path == path] == ["protected"]

    def test_never_removes_the_bare_clone_itself(self, repos):
        repos_dir, bare, _ = repos
        _age(bare, 48)

        reap_worktrees(repos_dir, retention_hours=24)

        assert bare.exists()
        assert (bare / "HEAD").exists()

    def test_never_removes_an_ordinary_repository_main_worktree(self, tmp_path):
        """A non-bare clone made by hand under repos_dir. Its main worktree is
        the repository; `worktree remove` refuses, and so must this."""
        repos_dir = tmp_path / "repos"
        upstream = _upstream(tmp_path)
        plain = repos_dir / "ns" / "plain"
        plain.parent.mkdir(parents=True)
        _git(tmp_path, "clone", "-q", str(upstream), str(plain))
        _git(plain, "remote", "set-head", "origin", "-a")
        _age(plain, 48)

        reap_worktrees(repos_dir, retention_hours=24)

        assert (plain / "README").exists()

    def test_refuses_when_origin_head_does_not_resolve(self, tmp_path):
        """No upstream reference means no way to prove anything landed. For a
        delete path that is not a reason to guess — it is a reason to stop.

        A bare clone made before the skill's setup ran is exactly this state
        (`clone --bare` creates no remote-tracking refs), and it is also what a
        renamed upstream default branch leaves behind."""
        repos_dir = tmp_path / "repos"
        upstream = _upstream(tmp_path)
        bare = repos_dir / "ns" / "project.git"
        bare.parent.mkdir(parents=True)
        _git(tmp_path, "clone", "-q", "--bare", str(upstream), str(bare))
        path = _worktree(bare, "project--main", "main")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert path.exists()
        assert [o.reason for o in outcomes if o.path == path] == ["no_upstream"]

    def test_ignores_a_worktree_registered_outside_repos_dir(self, repos, tmp_path):
        """`worktree add` takes any path. A record pointing outside the tree
        the sweep was handed is out of scope, and removing it would let a path
        the model chose steer a delete."""
        repos_dir, bare, _ = repos
        outside = tmp_path / "elsewhere" / "checkout"
        outside.parent.mkdir(parents=True)
        _git(bare, "worktree", "add", "-q", "--detach", str(outside), "origin/main")
        _age(outside, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert outside.exists()


# --------------------------------------------------------------------------
# The setup-path contract
# --------------------------------------------------------------------------

class TestNeverRaises:

    def test_a_missing_root_is_an_empty_result(self, tmp_path):
        assert reap_and_report(tmp_path / "nope", retention_hours=24) == []

    def test_a_root_that_is_a_file_is_an_empty_result(self, tmp_path):
        target = tmp_path / "file"
        target.write_text("x")
        assert reap_and_report(target, retention_hours=24) == []

    def test_a_directory_with_no_repositories_is_an_empty_result(self, tmp_path):
        (tmp_path / "junk").mkdir()
        (tmp_path / "junk" / "a.txt").write_text("x")
        assert reap_and_report(tmp_path, retention_hours=24) == []

    def test_the_public_entry_point_swallows_an_exploding_sweep(self, tmp_path, monkeypatch):
        """`reap_worktrees` is the public entry and the one most callers use,
        so the guard lives there rather than only in `reap_and_report`."""
        import istota.worktree_reaper as mod

        monkeypatch.setattr(
            mod, "_reap", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")),
        )
        assert reap_worktrees(tmp_path, retention_hours=24) == []
        assert reap_and_report(tmp_path, retention_hours=24) == []


class TestRetentionFloor:
    """A window short enough to catch a live checkout is not a faster sweep.

    A worktree seconds old is clean, unlocked and carries nothing that is not
    upstream — the exact reapable state — so `0` used to mean "delete the
    checkout of a task that is still setting it up". The knob is clamped
    instead of trusted.
    """

    def test_zero_is_clamped_rather_than_disabling_the_age_guard(self, repos):
        repos_dir, bare, _ = repos
        fresh = _worktree(bare, "project--clean", "origin/main")

        outcomes = reap_worktrees(repos_dir, retention_hours=0)

        assert _names(outcomes) == set()
        assert fresh.exists()
        assert [o.reason for o in outcomes if o.path == fresh] == ["recent"]

    def test_a_negative_window_is_clamped_too(self, repos):
        repos_dir, bare, _ = repos
        fresh = _worktree(bare, "project--clean", "origin/main")

        assert _names(reap_worktrees(repos_dir, retention_hours=-99)) == set()
        assert fresh.exists()

    def test_the_floor_still_reaps_something_genuinely_idle(self, repos):
        """Clamping must not turn the sweep off — a worktree older than the
        floor is still taken."""
        repos_dir, bare, _ = repos
        old = _worktree(bare, "project--old", "origin/main")
        _age(old, 5)

        assert _names(reap_worktrees(repos_dir, retention_hours=0)) == {"project--old"}


class TestHardening:
    """`repos_dir` is bound read-write into the sandbox, so a checkout's own
    `.git/config` is content the model writes. Repository config is covered by
    neither `GIT_CONFIG_NOSYSTEM` nor `GIT_CONFIG_GLOBAL`."""

    def test_a_repo_configured_fsmonitor_is_not_executed(self, repos, tmp_path):
        """`core.fsmonitor` names a program git runs on `status`. Without the
        `-c` overrides this module would run it as the daemon user, with the
        daemon's environment — forge tokens included."""
        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        marker = tmp_path / "pwned.txt"
        hook = tmp_path / "fsmonitor.sh"
        hook.write_text(f"#!/bin/sh\necho pwned > {marker}\n")
        hook.chmod(0o755)
        _git(path, "config", "core.fsmonitor", str(hook))
        _age(path, 48)

        reap_worktrees(repos_dir, retention_hours=24)

        assert not marker.exists(), "core.fsmonitor ran: GIT_HARDENING is not applied"

    def test_a_repo_configured_hookspath_is_not_executed(self, repos, tmp_path):
        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        marker = tmp_path / "hook-ran.txt"
        hooks = tmp_path / "hooks"
        hooks.mkdir()
        post = hooks / "post-index-change"
        post.write_text(f"#!/bin/sh\necho ran > {marker}\n")
        post.chmod(0o755)
        _git(path, "config", "core.hooksPath", str(hooks))
        _age(path, 48)

        reap_worktrees(repos_dir, retention_hours=24)

        assert not marker.exists()

    def test_a_sweep_does_not_reset_the_idle_clock_it_reads(self, repos):
        """`GIT_OPTIONAL_LOCKS=0`, verified rather than asserted. Plain
        `git status` rewrites the worktree's index, which is one of the mtimes
        the retention window reads — so without it every sweep would refresh
        every worktree it examined and nothing would ever be reaped after the
        first pass. A held-back worktree must look exactly as old after a sweep
        as before it."""
        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--topic", "origin/main", branch="topic")
        (path / "feature").write_text("x\n")
        _git(path, "add", "feature")
        _git(path, "commit", "-q", "-m", "unmerged")   # held late, so status runs
        _age(path, 48)

        gitdir = Path((path / ".git").read_text().split(":", 1)[1].strip())
        before = (gitdir / "index").stat().st_mtime

        reap_worktrees(repos_dir, retention_hours=24)

        assert (gitdir / "index").stat().st_mtime == before


class TestFailClosed:
    """Every "git could not answer" path holds the worktree."""

    def test_a_failing_dirty_check_holds(self, repos, monkeypatch):
        import istota.worktree_reaper as mod

        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        _age(path, 48)
        monkeypatch.setattr(mod, "_is_dirty", lambda _wt: None)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert [o.reason for o in outcomes if o.path == path] == ["dirty"]

    def test_a_failing_merged_check_holds(self, repos, monkeypatch):
        import istota.worktree_reaper as mod

        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        _age(path, 48)
        monkeypatch.setattr(mod, "_has_unique_commits", lambda *a: None)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert [o.reason for o in outcomes if o.path == path] == ["unmerged"]

    def test_a_head_with_no_sha_holds(self):
        """A listing carrying no HEAD, or the all-zero sha of an unborn branch.
        Neither used to be compared against anything: an empty head returned
        "no unique commits", which is the reapable answer."""
        import istota.worktree_reaper as mod

        assert mod._has_unique_commits(Path("/nonexistent"), "x", "") is None
        assert mod._has_unique_commits(Path("/nonexistent"), "x", "0" * 40) is None

    def test_a_merge_head_holds(self, repos):
        """`git cherry` reports the commits a merge brought in and never the
        merge's own delta, so content that exists only in a conflict resolution
        is invisible to the merged test. The head is refused instead."""
        repos_dir, bare, upstream = repos
        path = _worktree(bare, "project--topic", "origin/main", branch="topic")
        _git(path, "checkout", "-q", "-b", "side")
        (path / "side").write_text("s\n")
        _git(path, "add", "side")
        _git(path, "commit", "-q", "-m", "side")
        _git(path, "checkout", "-q", "topic")
        _git(path, "merge", "-q", "--no-ff", "side", "-m", "merge side")

        # Land everything upstream so only the merge-head rule can hold it.
        _git(upstream, "fetch", "-q", str(path), "topic")
        _git(upstream, "merge", "-q", "--ff-only", "FETCH_HEAD")
        _git(bare, "fetch", "-q", "origin")
        _age(path, 48)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert [o.reason for o in outcomes if o.path == path] == ["merge_head"]


class TestBranchRef:

    def test_the_ref_delete_is_pinned_to_the_head_that_was_approved(self, repos, monkeypatch):
        """`update-ref -d <ref>` with no old value deletes whatever the ref
        points at now, not the sha `git cherry` approved — so a branch that
        advanced between the listing and the removal would lose the new commits
        to the delete. With the old value, git refuses and says so."""
        import istota.worktree_reaper as mod

        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--topic", "origin/main", branch="topic")
        _age(path, 48)

        def advance_then_report_clean(_wt):
            if not (path / "late").exists():
                (path / "late").write_text("y\n")
                _git(path, "add", "late")
                _git(path, "commit", "-q", "-m", "late work")
            return False

        monkeypatch.setattr(mod, "_is_dirty", advance_then_report_clean)
        reap_worktrees(repos_dir, retention_hours=24)

        refs = _git(bare, "for-each-ref", "--format=%(refname:short)", "refs/heads/")
        assert "topic" in refs, "the late commit was deleted along with the ref"


class TestAdminPointer:
    """`.git` in a linked worktree is a file holding `gitdir: <path>`, and the
    path may be relative — `git worktree add --relative-paths`, or
    `worktree.useRelativePaths` set in a config the model can write. Resolving
    a relative pointer against the daemon's cwd finds nothing, and the failure
    is silent: it drops the git half of the activity signal, so a worktree
    whose task has been committing all day reads as idle."""

    def test_an_absolute_pointer_resolves(self, repos):
        import istota.worktree_reaper as mod

        _, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        assert mod._admin_dir(path) is not None

    def test_a_relative_pointer_resolves_against_the_worktree(self, repos):
        import istota.worktree_reaper as mod

        _, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        admin = Path((path / ".git").read_text().split(":", 1)[1].strip())
        relative = os.path.relpath(admin, path)
        (path / ".git").write_text(f"gitdir: {relative}\n")

        assert not Path(relative).is_dir(), "precondition: bare relative path finds nothing"
        assert mod._admin_dir(path) == path / relative

    def test_a_relative_pointer_still_gates_the_age_check(self, repos):
        """The guard that matters, through the real path: with the pointer
        relative, git activity must still hold a worktree back."""
        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        admin = Path((path / ".git").read_text().split(":", 1)[1].strip())
        _age(path, 48)
        (path / ".git").write_text(f"gitdir: {os.path.relpath(admin, path)}\n")
        os.utime(path / ".git", (time.time() - 48 * 3600,) * 2)
        os.utime(admin / "index", None)   # git touched it a moment ago

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert path.exists()


class TestRecheckBeforeRemoval:
    """`git worktree remove` without `--force` refuses a tracked modification
    and a lock, and removes a gitignored `.env` or `node_modules` without a
    word. So for exactly the class of file the `--ignored` flag protects, this
    module's own check is the only guard, and the gap between checking and
    removing is the whole exposure. The check is repeated immediately before
    the removal to shrink it."""

    def test_a_file_appearing_after_classification_is_not_deleted(self, repos, monkeypatch):
        import istota.worktree_reaper as mod

        repos_dir, bare, upstream = repos
        # Committed upstream so the checkout stays clean and only `--ignored`
        # can see the file that appears.
        (upstream / ".gitignore").write_text(".env\n")
        _git(upstream, "add", ".gitignore")
        _git(upstream, "commit", "-q", "-m", "ignore env")
        _git(bare, "fetch", "-q", "origin")

        path = _worktree(bare, "project--main", "origin/main")
        _age(path, 48)

        # `_is_merge_commit` runs after the dirty check and before the removal,
        # which is the window a sibling process would write into.
        real = mod._is_merge_commit

        def write_then_answer(bare_dir, head):
            (path / ".env").write_text("SECRET=x\n")
            return real(bare_dir, head)

        monkeypatch.setattr(mod, "_is_merge_commit", write_then_answer)

        outcomes = reap_worktrees(repos_dir, retention_hours=24)

        assert _names(outcomes) == set()
        assert (path / ".env").read_text() == "SECRET=x\n"


class TestSchedulerIntegration:
    """The sweep's real call site. It is a scheduler job rather than a task
    setup hook because `dispatch_setup_env_hooks` calls every skill's hook
    whatever the task selected — so on the setup path it ran before every Talk
    reply, every cron job and every heartbeat tick, and the heartbeat builds a
    task with `id=0`."""

    def _config(self, repos_dir, **dev_kwargs):
        from istota.config import Config, DeveloperConfig

        config = Config()
        config.developer = DeveloperConfig(
            enabled=True, repos_dir=str(repos_dir), **dev_kwargs
        )
        return config

    def test_check_worktree_reap_sweeps(self, repos):
        from istota.scheduler import check_worktree_reap

        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        _age(path, 48)

        check_worktree_reap(self._config(repos_dir))

        assert not path.exists()

    def test_check_worktree_reap_honours_the_retention_window(self, repos):
        """The window has to reach the reaper, not stop at the dataclass."""
        from istota.scheduler import check_worktree_reap

        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        _age(path, 48)

        check_worktree_reap(self._config(repos_dir, worktree_retention_hours=72))

        assert path.exists()

    def test_check_worktree_reap_honours_the_off_switch(self, repos):
        from istota.scheduler import check_worktree_reap

        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        _age(path, 48)

        check_worktree_reap(self._config(repos_dir, worktree_reap_enabled=False))

        assert path.exists()

    def test_check_worktree_reap_does_nothing_when_developer_is_off(self, repos):
        """The flag is re-checked inside the function, not left to the loop's
        gate, so it is safe to call on its own."""
        from istota.config import Config, DeveloperConfig
        from istota.scheduler import check_worktree_reap

        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        _age(path, 48)

        config = Config()
        config.developer = DeveloperConfig(enabled=False, repos_dir=str(repos_dir))
        assert check_worktree_reap(config) == []
        assert path.exists()

    def test_check_worktree_reap_never_raises(self, tmp_path):
        from istota.scheduler import check_worktree_reap

        assert check_worktree_reap(self._config(tmp_path / "nope")) == []

    def test_the_developer_setup_hook_no_longer_reaps(self, repos, tmp_path):
        """The hook still writes the credential wiring; it must not delete."""
        from istota.skills.developer import setup_env

        repos_dir, bare, _ = repos
        path = _worktree(bare, "project--main", "origin/main")
        _age(path, 48)

        config = self._config(repos_dir)
        config.security.skill_proxy_enabled = False

        class _Ctx:
            pass

        ctx = _Ctx()
        ctx.config = config
        ctx.task = None
        ctx.user_temp_dir = tmp_path / "temp"
        ctx.user_temp_dir.mkdir(parents=True, exist_ok=True)

        assert setup_env(ctx) is not None
        assert path.exists()
