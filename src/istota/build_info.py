"""What code a running process actually loaded.

A long-running daemon imports its modules once and holds them until it is
restarted, and every deploy path here moves the checkout before it restarts
anything. Ansible fires its restart handlers after the frontend build, which on
the production host left an eight-minute window where ``git log`` in the
checkout reported the new commit while the scheduler was still executing the
old one. An inbound email landed inside that window and the fix it was meant to
exercise looked broken (2026-08-10, while verifying ISSUE-247).

So "is the fix live?" has to be answered by the process, not by the checkout.
Each long-running entry point logs :func:`build_description` at startup. It
records the revision as of *import*, which is the code that process is running
however far the checkout moves afterwards — that gap is the whole point, so
this must never be re-read later and cached as if it were current.

The git plumbing is read directly rather than by shelling out to ``git``:
stdlib only, no subprocess in the startup path, an answer when git isn't
installed at all, and immune to ``safe.directory`` refusing a checkout the
daemon user does not own (the daemon runs as its own service account). An
install with no checkout under it — a wheel, ``uv tool install`` — is a normal
answer here rather than an error.
"""

from __future__ import annotations

from pathlib import Path

from . import __version__

__all__ = ["build_description", "checkout_revision"]


def _git_dir(start: Path) -> Path | None:
    """The git directory governing ``start``, or None if it is not in a checkout.

    ``.git`` is a *file* rather than a directory in a linked worktree
    (``gitdir: /path/to/.git/worktrees/<name>``), which is how the job workflow
    checks branches out, so both forms have to resolve.
    """
    for parent in [start, *start.parents]:
        candidate = parent / ".git"
        if candidate.is_dir():
            return candidate
        if not candidate.is_file():
            continue
        text = candidate.read_text(encoding="utf-8").strip()
        if not text.startswith("gitdir:"):
            return None
        linked = Path(text[len("gitdir:"):].strip())
        if not linked.is_absolute():
            linked = (parent / linked).resolve()
        return linked if linked.is_dir() else None
    return None


def _common_dir(git_dir: Path) -> Path:
    """The shared git directory, which is where ``packed-refs`` lives.

    A linked worktree's git directory holds its own HEAD but no packed-refs and
    no branch refs; its ``commondir`` file points at the main one. Absent that
    file, ``git_dir`` already is the common directory.
    """
    try:
        text = (git_dir / "commondir").read_text(encoding="utf-8").strip()
    except OSError:
        return git_dir
    common = Path(text)
    if not common.is_absolute():
        common = (git_dir / common).resolve()
    return common if common.is_dir() else git_dir


def _resolve_ref(git_dir: Path, ref: str) -> str | None:
    """The commit sha a ref name points at, or None.

    Loose ref file first — in both the worktree's git directory and the common
    one, since a linked worktree keeps branch refs only in the latter — then
    ``packed-refs``, which is where a ref lands after ``git gc`` and is the form
    a freshly cloned deployment sees for every branch it has not moved.
    """
    common = _common_dir(git_dir)
    for base in (git_dir, common):
        try:
            sha = (base / ref).read_text(encoding="utf-8").strip()
        except OSError:
            continue
        if sha and not sha.startswith("ref:"):
            return sha

    try:
        lines = (common / "packed-refs").read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        # "^<sha>" peels the tag on the preceding line; "#" is the header.
        if not line or line.startswith(("#", "^")):
            continue
        sha, _, name = line.partition(" ")
        if name.strip() == ref:
            return sha.strip() or None
    return None


def checkout_revision(package_file: str | None = None) -> tuple[str | None, str | None]:
    """``(branch, full sha)`` for the checkout this package was imported from.

    ``(None, None)`` when there is no checkout under the import path or the
    plumbing cannot be read. Branch is None on a detached HEAD, where the sha is
    still the answer that matters.

    Never raises. It backs a log line, and a daemon must not fail to start
    because it could not work out its own version.
    """
    try:
        source = Path(package_file or __file__).resolve().parent
        git_dir = _git_dir(source)
        if git_dir is None:
            return None, None
        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return None, head or None  # detached HEAD holds the sha itself
        ref = head[len("ref:"):].strip()
        return (ref.rpartition("/")[2] or None), _resolve_ref(git_dir, ref)
    except Exception:  # pragma: no cover - defensive; see docstring
        return None, None


def build_description(package_file: str | None = None) -> str:
    """One line naming the code this process is running.

    The grep target for "is the fix live?": compare the sha here against the
    commit you expect, rather than against whatever the checkout reports now.
    """
    branch, sha = checkout_revision(package_file)
    if not sha:
        return f"istota {__version__} (no checkout under the import path)"
    where = f"{branch} {sha[:12]}" if branch else f"detached {sha[:12]}"
    return f"istota {__version__} ({where})"
