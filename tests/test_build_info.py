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

import zlib

from istota.build_info import build_description, checkout_revision, version_label

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


# --- version_label (ISSUE-569) --------------------------------------------
#
# `__version__` is the pyproject version, bumped only at a release cut, so a
# host on an untagged commit of main reported the previous release's number.
# The label adds the commit unless HEAD is exactly the matching release tag.


def _label(package: Path, version: str = "0.42.0") -> str:
    return version_label(str(package / "build_info.py"), version=version)


def _loose_object(root: Path, sha: str, kind: str, body: bytes) -> None:
    path = root / ".git" / "objects" / sha[:2] / sha[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(zlib.compress(f"{kind} {len(body)}\0".encode() + body))


def test_an_untagged_commit_carries_its_short_sha(tmp_path):
    """The reported bug: main, several commits past v0.42.0."""
    package = _checkout(tmp_path, head="ref: refs/heads/main\n")
    _loose_ref(tmp_path, "refs/heads/main", SHA)
    _loose_ref(tmp_path, "refs/tags/v0.42.0", OTHER_SHA)

    assert _label(package) == f"0.42.0+{SHA[:7]}"


def test_a_lightweight_release_tag_at_head_is_the_plain_version(tmp_path):
    package = _checkout(tmp_path, head=SHA + "\n")
    _loose_ref(tmp_path, "refs/tags/v0.42.0", SHA)

    assert _label(package) == "0.42.0"


def test_an_annotated_release_tag_is_peeled_from_packed_refs(tmp_path):
    """`release.sh` makes annotated tags; the ref names the tag object."""
    package = _checkout(tmp_path, head=SHA + "\n")
    (tmp_path / ".git" / "packed-refs").write_text(
        "# pack-refs with: peeled fully-peeled sorted\n"
        f"{OTHER_SHA} refs/tags/v0.42.0\n"
        f"^{SHA}\n",
        encoding="utf-8",
    )

    assert _label(package) == "0.42.0"


def test_an_annotated_release_tag_is_peeled_from_a_loose_object(tmp_path):
    package = _checkout(tmp_path, head=SHA + "\n")
    _loose_ref(tmp_path, "refs/tags/v0.42.0", OTHER_SHA)
    _loose_object(
        tmp_path, OTHER_SHA, "tag",
        f"object {SHA}\ntype commit\ntag v0.42.0\n".encode(),
    )

    assert _label(package) == "0.42.0"


def test_a_tag_that_cannot_be_peeled_reads_as_untagged(tmp_path):
    """A tag object found neither loose nor in any pack: showing the sha is safe."""
    package = _checkout(tmp_path, head=SHA + "\n")
    _loose_ref(tmp_path, "refs/tags/v0.42.0", OTHER_SHA)

    assert _label(package) == f"0.42.0+{SHA[:7]}"


def test_a_different_release_tag_at_head_does_not_count(tmp_path):
    """HEAD tagged v0.41.0 while pyproject says 0.42.0 is not the 0.42.0 release."""
    package = _checkout(tmp_path, head=SHA + "\n")
    _loose_ref(tmp_path, "refs/tags/v0.41.0", SHA)

    assert _label(package) == f"0.42.0+{SHA[:7]}"


def test_no_checkout_is_the_plain_version(tmp_path):
    package = tmp_path / "site-packages" / "istota"
    package.mkdir(parents=True)

    assert _label(package) == "0.42.0"


def _pack_one_object(root: Path, sha: str, kind: int, body: bytes) -> None:
    """A v2 `.idx` / `.pack` pair holding one non-delta object, built by hand."""
    size = len(body)
    header = bytearray([(kind << 4) | (size & 0x0F)])
    size >>= 4
    while size:
        header[-1] |= 0x80
        header.append(size & 0x7F)
        size >>= 7
    pack = b"PACK" + (2).to_bytes(4, "big") + (1).to_bytes(4, "big")
    offset = len(pack)
    pack += bytes(header) + zlib.compress(body)

    name = bytes.fromhex(sha)
    fanout = b"".join(
        (1 if i >= name[0] else 0).to_bytes(4, "big") for i in range(256)
    )
    idx = (b"\377tOc" + (2).to_bytes(4, "big") + fanout + name
           + (0).to_bytes(4, "big") + offset.to_bytes(4, "big"))
    packdir = root / ".git" / "objects" / "pack"
    packdir.mkdir(parents=True, exist_ok=True)
    (packdir / "pack-test.idx").write_bytes(idx)
    (packdir / "pack-test.pack").write_bytes(pack)


def test_an_annotated_release_tag_is_peeled_from_a_pack(tmp_path):
    """What `git fetch` leaves: the tag ref loose, its object inside a pack."""
    package = _checkout(tmp_path, head=SHA + "\n")
    _loose_ref(tmp_path, "refs/tags/v0.42.0", OTHER_SHA)
    _pack_one_object(
        tmp_path, OTHER_SHA, 4, f"object {SHA}\ntype commit\ntag v0.42.0\n".encode(),
    )

    assert _label(package) == "0.42.0"


def test_a_packed_tag_naming_another_commit_is_untagged(tmp_path):
    package = _checkout(tmp_path, head=SHA + "\n")
    _loose_ref(tmp_path, "refs/tags/v0.42.0", OTHER_SHA)
    _pack_one_object(
        tmp_path, OTHER_SHA, 4, f"object {'1' * 40}\ntype commit\n".encode() * 3,
    )

    assert _label(package) == f"0.42.0+{SHA[:7]}"


def _pack_objects(root: Path, objects: list[tuple[str, int, bytes]]) -> None:
    """Several non-delta objects in one hand-built v2 pack, index sorted by name."""
    pack = b"PACK" + (2).to_bytes(4, "big") + len(objects).to_bytes(4, "big")
    entries = []
    for sha, kind, body in objects:
        size = len(body)
        header = bytearray([(kind << 4) | (size & 0x0F)])
        size >>= 4
        while size:
            header[-1] |= 0x80
            header.append(size & 0x7F)
            size >>= 7
        entries.append((bytes.fromhex(sha), len(pack)))
        pack += bytes(header) + zlib.compress(body)
    entries.sort()
    fanout = b"".join(
        sum(1 for name, _ in entries if name[0] <= i).to_bytes(4, "big")
        for i in range(256)
    )
    idx = (b"\377tOc" + (2).to_bytes(4, "big") + fanout
           + b"".join(name for name, _ in entries)
           + b"\0\0\0\0" * len(entries)
           + b"".join(offset.to_bytes(4, "big") for _, offset in entries))
    packdir = root / ".git" / "objects" / "pack"
    packdir.mkdir(parents=True, exist_ok=True)
    (packdir / "pack-many.idx").write_bytes(idx)
    (packdir / "pack-many.pack").write_bytes(pack)


@pytest.mark.parametrize("tag_sha", ["00" + "a" * 38, "7f" + "b" * 38, "ff" + "c" * 38])
def test_a_tag_is_found_among_several_packed_objects(tmp_path, tag_sha):
    """The binary search, and both ends of the fan-out table (first byte 0x00, 0xff)."""
    package = _checkout(tmp_path, head=SHA + "\n")
    _loose_ref(tmp_path, "refs/tags/v0.42.0", tag_sha)
    others = ["00" + "1" * 38, "3c" + "2" * 38, "7f" + "3" * 38, "c0" + "4" * 38, "ff" + "5" * 38]
    _pack_objects(tmp_path, [
        *((sha, 3, b"blob contents") for sha in others),
        (tag_sha, 4, f"object {SHA}\ntype commit\n".encode()),
    ])

    assert _label(package) == "0.42.0"


def test_a_version_that_already_has_a_local_segment_stays_valid(tmp_path):
    """The uninstalled fallback `0.0.0+unknown` must not become `+unknown+sha`."""
    package = _checkout(tmp_path, head=SHA + "\n")

    assert _label(package, version="0.0.0+unknown") == f"0.0.0+unknown.{SHA[:7]}"
