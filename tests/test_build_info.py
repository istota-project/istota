"""Tests for build_info — the revision a running process actually imported.

Motivated by a live misdiagnosis on 2026-08-10: the Ansible deploy moved the
checkout to the commit under test and fired its restart handlers eight minutes
later, so `git log` on the host reported the fix while the scheduler was still
running the previous commit. An inbound email arrived inside that window and
the fix looked broken. The startup log line these functions back is what turns
that question into a grep.

The git plumbing is read directly, so the fixtures build it by hand rather than
shelling out to `git init` — that is the contract being tested, and it keeps
the suite independent of git being installed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from istota.build_info import build_description, checkout_revision

SHA = "c9603eaa00d818cae51f7a501e5329274b4b0339"
OTHER_SHA = "13dda131771f375861cfccac56465ab2f937f808"


def _checkout(root: Path, *, head: str) -> Path:
    """A repo root with a `.git` directory and the given HEAD contents.

    Returns the package directory nested under it, which is what the real
    caller passes (`__file__` of a module inside `src/istota/`).
    """
    git_dir = root / ".git"
    git_dir.mkdir(parents=True)
    (git_dir / "HEAD").write_text(head, encoding="utf-8")
    package = root / "src" / "istota"
    package.mkdir(parents=True)
    return package


def _loose_ref(root: Path, ref: str, sha: str) -> None:
    path = root / ".git" / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(sha + "\n", encoding="utf-8")


def test_loose_ref_gives_branch_and_sha(tmp_path):
    package = _checkout(tmp_path, head="ref: refs/heads/main\n")
    _loose_ref(tmp_path, "refs/heads/main", SHA)

    assert checkout_revision(str(package / "build_info.py")) == ("main", SHA)


def test_branch_name_keeps_only_its_last_segment(tmp_path):
    package = _checkout(tmp_path, head="ref: refs/heads/job/247-email-room\n")
    _loose_ref(tmp_path, "refs/heads/job/247-email-room", SHA)

    branch, sha = checkout_revision(str(package / "build_info.py"))
    assert (branch, sha) == ("247-email-room", SHA)


def test_packed_refs_when_the_loose_ref_is_absent(tmp_path):
    """What a freshly cloned deployment sees for a branch it has not moved."""
    package = _checkout(tmp_path, head="ref: refs/heads/main\n")
    (tmp_path / ".git" / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        f"{OTHER_SHA} refs/heads/other\n"
        f"{SHA} refs/heads/main\n"
        f"{OTHER_SHA} refs/tags/v0.39.0\n"
        f"^{SHA}\n",
        encoding="utf-8",
    )

    assert checkout_revision(str(package / "build_info.py")) == ("main", SHA)


def test_detached_head_reports_the_sha_with_no_branch(tmp_path):
    package = _checkout(tmp_path, head=SHA + "\n")

    assert checkout_revision(str(package / "build_info.py")) == (None, SHA)


def test_linked_worktree_resolves_through_commondir(tmp_path):
    """The job workflow checks branches out as worktrees, where `.git` is a file.

    The worktree's git dir holds its own HEAD but no branch refs; those live in
    the main checkout's git dir, named by `commondir`.
    """
    main = tmp_path / "istota"
    main_git = main / ".git"
    (main_git / "refs" / "heads").mkdir(parents=True)
    (main_git / "refs" / "heads" / "main").write_text(OTHER_SHA + "\n", encoding="utf-8")
    (main_git / "refs" / "heads" / "job").write_text(SHA + "\n", encoding="utf-8")

    wt_git = main_git / "worktrees" / "job"
    wt_git.mkdir(parents=True)
    (wt_git / "HEAD").write_text("ref: refs/heads/job\n", encoding="utf-8")
    (wt_git / "commondir").write_text("../..\n", encoding="utf-8")

    worktree = tmp_path / "worktree-job"
    package = worktree / "src" / "istota"
    package.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {wt_git}\n", encoding="utf-8")

    branch, sha = checkout_revision(str(package / "build_info.py"))
    assert (branch, sha) == ("job", SHA), "must not report the main checkout's ref"


def test_no_checkout_under_the_import_path(tmp_path):
    """A wheel or `uv tool` install. A normal answer, not an error."""
    package = tmp_path / "site-packages" / "istota"
    package.mkdir(parents=True)

    assert checkout_revision(str(package / "build_info.py")) == (None, None)


def test_unreadable_plumbing_never_raises(tmp_path):
    """A daemon must not fail to start because it cannot name its own version."""
    package = _checkout(tmp_path, head="ref: refs/heads/main\n")
    (tmp_path / ".git" / "HEAD").unlink()

    assert checkout_revision(str(package / "build_info.py")) == (None, None)


def test_description_carries_a_greppable_short_sha(tmp_path):
    package = _checkout(tmp_path, head="ref: refs/heads/main\n")
    _loose_ref(tmp_path, "refs/heads/main", SHA)

    line = build_description(str(package / "build_info.py"))
    assert "main c9603eaa00d8" in line
    assert SHA[:8] in line, "the short sha an operator pastes from `git log`"


def test_description_says_so_when_there_is_no_checkout(tmp_path):
    package = tmp_path / "site-packages" / "istota"
    package.mkdir(parents=True)

    assert "no checkout" in build_description(str(package / "build_info.py"))


@pytest.mark.parametrize("caller", [None])
def test_real_package_resolves_in_this_repo(caller):
    """The default argument path, exercised against the actual checkout.

    Skipped rather than failed off a checkout, since that is a supported shape.
    """
    branch, sha = checkout_revision(caller)
    if sha is None:
        pytest.skip("test run from an install with no checkout under it")
    assert len(sha) == 40 and all(c in "0123456789abcdef" for c in sha)
