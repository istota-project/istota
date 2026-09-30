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

import zlib
from pathlib import Path

from . import __version__

__all__ = [
    "RUNNING_VERSION", "build_description", "checkout_revision", "version_label",
]


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


def _packed_refs(git_dir: Path) -> list[str]:
    try:
        text = (_common_dir(git_dir) / "packed-refs").read_text(encoding="utf-8")
    except OSError:
        return []
    return text.splitlines()


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

    for line in _packed_refs(git_dir):
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


def _packed_object(objects: Path, sha: str) -> tuple[int, bytes] | None:
    """``(type, body)`` of a non-delta object in a v2 pack, or None.

    Enough of the pack format to read one tag object back: the index's fan-out
    table narrows the sorted sha list, the matching offset (or its entry in the
    large-offset table) locates the entry in the ``.pack``, and the entry is a
    varint header then zlib data. A deltified entry is not reconstructed; git
    does not deltify tag objects in practice, and None is the safe answer.
    """
    try:
        target = bytes.fromhex(sha)
    except ValueError:
        return None
    if len(target) != 20:
        return None
    for idx in sorted((objects / "pack").glob("pack-*.idx")):
        try:
            data = idx.read_bytes()
        except OSError:
            continue
        if data[:8] != b"\377tOc\0\0\0\2":
            continue
        fanout = [int.from_bytes(data[8 + 4 * i:12 + 4 * i], "big") for i in range(256)]
        count = fanout[255]
        lo = fanout[target[0] - 1] if target[0] else 0
        hi = fanout[target[0]]
        names = 8 + 1024
        position = None
        while lo < hi:
            mid = (lo + hi) // 2
            name = data[names + 20 * mid:names + 20 * mid + 20]
            if name == target:
                position = mid
                break
            if name < target:
                lo = mid + 1
            else:
                hi = mid
        if position is None:
            continue
        offsets = names + 24 * count
        offset = int.from_bytes(data[offsets + 4 * position:offsets + 4 * position + 4], "big")
        if offset & 0x80000000:
            large = offsets + 4 * count + 8 * (offset & 0x7FFFFFFF)
            offset = int.from_bytes(data[large:large + 8], "big")
        try:
            with idx.with_suffix(".pack").open("rb") as pack:
                pack.seek(offset)
                entry = pack.read(64 * 1024)
        except OSError:
            return None
        if not entry:
            return None
        kind = (entry[0] >> 4) & 7
        i = 0
        while i < len(entry) and entry[i] & 0x80:
            i += 1
        if kind not in (1, 2, 3, 4):
            return None
        try:
            return kind, zlib.decompressobj().decompress(entry[i + 1:])
        except zlib.error:
            return None
    return None


def _tag_commit(git_dir: Path, tag: str, head: str) -> bool:
    """Whether ``refs/tags/<tag>`` names the commit ``head``.

    A lightweight tag holds the commit sha itself. An annotated tag, which is
    what ``release.sh`` makes, holds a tag object's sha and has to be peeled.
    ``packed-refs`` records the peeled commit on the ``^`` line after the tag;
    a loose ref's tag object is read from ``objects/``, loose or packed, since
    ``git fetch`` writes the ref loose and the object into a pack. An object
    that cannot be read makes the answer False, so the label carries a sha it
    did not need rather than a plain version it should not have.
    """
    ref = f"refs/tags/{tag}"
    common = _common_dir(git_dir)
    loose = None
    for base in (git_dir, common):
        try:
            loose = (base / ref).read_text(encoding="utf-8").strip()
            break
        except OSError:
            continue

    if loose is None:
        lines = _packed_refs(git_dir)
        for i, line in enumerate(lines):
            if line.startswith(("#", "^")) or line.partition(" ")[2].strip() != ref:
                continue
            if line.partition(" ")[0].strip() == head:
                return True
            peeled = lines[i + 1] if i + 1 < len(lines) else ""
            return peeled.startswith("^") and peeled[1:].strip() == head
        return False

    if loose == head:
        return True
    objects = common / "objects"
    body = None
    try:
        raw = zlib.decompress((objects / loose[:2] / loose[2:]).read_bytes())
        header, _, rest = raw.partition(b"\0")
        if header.startswith(b"tag "):
            body = rest
    except (OSError, zlib.error):
        packed = _packed_object(objects, loose)
        if packed is not None and packed[0] == 4:
            body = packed[1]
    if body is None:
        return False
    for line in body.split(b"\n"):
        if line.startswith(b"object "):
            return line[len(b"object "):].strip().decode("ascii", "replace") == head
    return False


def version_label(package_file: str | None = None, *, version: str = __version__) -> str:
    """The version this process runs, with the commit unless it is a release.

    ``version`` is the pyproject version, bumped only when a release is cut, so
    on any commit between two tags it names the earlier release. Unless HEAD is
    exactly ``v<version>`` the short sha is appended in PEP 440's local-version
    form: ``0.42.0+a1b2c3d``. With no checkout under the import path (a wheel,
    the Docker image) the plain version is all there is to say.

    Never raises, for the reason :func:`checkout_revision` gives.
    """
    _, sha = checkout_revision(package_file)
    if not sha:
        return version
    try:
        source = Path(package_file or __file__).resolve().parent
        git_dir = _git_dir(source)
        if git_dir is not None and _tag_commit(git_dir, f"v{version}", sha):
            return version
    except Exception:  # pragma: no cover - defensive; see docstring
        pass
    # PEP 440 allows one local segment, and the uninstalled fallback
    # (`0.0.0+unknown`) already has one.
    sep = "." if "+" in version else "+"
    return f"{version}{sep}{sha[:7]}"


# Read once, at import, like the startup log line: this is the code the process
# is running, and a later read would describe the checkout instead (ISSUE-569).
RUNNING_VERSION = version_label()
