"""A read-only copy of the reviewed commit, for a reviewer that reads files.

The reviewer gets `Read`, `Grep` and `Glob`, and the namespace it runs in binds
one directory: the run directory this module builds. So what the reviewer can
see is exactly what is written here, and the rules for writing it are the
engine's rules for reading (see `engine`'s module docstring).

**Content comes out of the object store, never off the filesystem.** The tree
is listed with `git ls-tree` at the resolved head and every blob is read with
`git cat-file --batch`, which applies no textconv, no attributes and no smudge
filter. Not `git archive`: it reads `.gitattributes` from the commit
(`export-ignore`, `export-subst`), and the commit is model-written. Nothing
here opens a path inside the worktree.

**Only regular files are written.** A symlink entry (mode `120000`) is listed
in `meta/skipped.txt` and never written, so nothing under `tree/` points
anywhere; a submodule (`160000`) is listed the same way. Every path goes
through `engine._safe_repo_path` and must come back unchanged, and every leaf
is opened `O_CREAT | O_EXCL | O_NOFOLLOW`, so two entries that collide on disk
(a case-insensitive filesystem, a crafted tree) cost the second one rather
than overwriting the first.

**The run directory is daemon-private.** `{temp_dir}/.review/{user_id}/
run-<hex>/`, a sibling of the per-user temp dirs like `.control` and
`.sandbox-work`, because the shared temp root is bound into no namespace: no
task can plant a link at any level of it. Each level is a real directory, owned
by the daemon, `0700` (`executor._ensure_control_level`, the one
implementation). A user id that casefolds to `.review` is refused for the
reason `executor.get_task_control_dir` gives for `.control`: that user's
model-writable temp dir would be this root.

The caller removes the run directory when the review ends;
`scheduler.cleanup_old_temp_files` removes any `run-*` older than
`RUN_DIR_MAX_AGE_SECONDS` left by a process that died first.
"""

from __future__ import annotations

import errno
import logging
import os
import posixpath
import secrets
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from istota.sandbox.user_scope import scoped_user_dir

from . import engine
from .engine import END_OF_OPTIONS, DiffBundle, ReviewError

logger = logging.getLogger("istota.skills.code_review.snapshot")

REVIEW_DIR_NAME = ".review"
RUN_DIR_PREFIX = "run-"

#: How long a run directory may outlive the review that made it before the
#: scheduler's sweep removes it. A review is bounded by the skill proxy's
#: ceiling, which is minutes, so an hour is only ever a dead process.
RUN_DIR_MAX_AGE_SECONDS = 3600

# Reasons a path is in `meta/skipped.txt`, and the keys of `Snapshot.skipped`.
SKIP_SYMLINK = "symlink"
SKIP_SUBMODULE = "submodule"
SKIP_BAD_PATH = "bad_path"
SKIP_TOO_LARGE = "too_large"
SKIP_OVER_BUDGET = "over_budget"
# A mode git does not write today (`100664` from an old repository, or a crafted
# tree), and a blob the object store does not have.
SKIP_UNSUPPORTED = "unsupported"
SKIP_MISSING = "missing"

_REGULAR_MODES = frozenset({"100644", "100755"})

# `over_budget` paths listed one per line before the rest become one summary
# line: past the cap the list is the size of the repository.
MAX_OVER_BUDGET_LINES = 200

MAX_HISTORY_FILES = 40
MAX_HISTORY_BYTES = 40 * 1024
HISTORY_COMMITS_PER_FILE = 5

# One `cat-file --batch` call stays under `_git`'s output bound with room for
# the per-object header lines; a snapshot larger than that takes several calls.
_BATCH_HEADER_ALLOWANCE = 128
BATCH_MAX_BYTES = engine.MAX_GIT_OUTPUT_BYTES // 2


@dataclass
class Snapshot:
    run_dir: Path
    tree_dir: Path
    files: int = 0
    bytes: int = 0
    skipped: dict[str, int] = field(default_factory=dict)
    truncated: bool = False


@dataclass
class _Entry:
    path: str
    oid: str
    size: int


def build_snapshot(
    worktree: Path,
    bundle: DiffBundle,
    *,
    root: Path,
    user_id: str,
    max_bytes: int,
    max_file_bytes: int,
) -> Snapshot:
    """Write the tree at `bundle.head` and the range's metadata to a new run dir.

    `root` is the shared temp root (`config.temp_dir`). Raises `ReviewError`
    with reason `snapshot_failed` on any failure, after removing whatever run
    directory it had made.
    """
    try:
        head = engine._require_object_id(bundle.head, "head")
        engine.git_dir(worktree)
        user_level = _ensure_user_level(Path(root), user_id)
    except (OSError, ValueError, ReviewError) as e:
        raise ReviewError(
            f"Could not prepare a snapshot directory: {e}", reason="snapshot_failed"
        ) from e

    run_dir = user_level / f"{RUN_DIR_PREFIX}{secrets.token_hex(8)}"
    try:
        os.mkdir(run_dir, 0o700)
    except OSError as e:
        raise ReviewError(
            f"Could not create {run_dir}: {e}", reason="snapshot_failed"
        ) from e
    try:
        return _fill(worktree, bundle, head, run_dir, max_bytes, max_file_bytes)
    except BaseException as e:
        shutil.rmtree(run_dir, ignore_errors=True)
        if isinstance(e, Exception):
            raise ReviewError(
                f"Snapshot of {bundle.rng} failed: {e}", reason="snapshot_failed"
            ) from e
        raise


def remove_snapshot(snapshot: Snapshot | None) -> None:
    """Remove a run directory. Never raises."""
    if snapshot is None:
        return
    try:
        shutil.rmtree(snapshot.run_dir)
    except FileNotFoundError:
        pass
    except OSError as e:
        logger.warning(
            "code_review_snapshot_remove_failed run_dir=%s error=%s",
            snapshot.run_dir, e,
        )


def _ensure_user_level(root: Path, user_id: str) -> Path:
    # Function scope: `executor` is a large import graph, and only a snapshot
    # build needs it.
    from istota.executor import _ensure_control_level

    if not isinstance(user_id, str) or user_id.casefold() == REVIEW_DIR_NAME.casefold():
        raise ValueError(f"user id {user_id!r} names the review root")
    base = root / REVIEW_DIR_NAME
    user_level = scoped_user_dir(base, user_id)
    if user_level is None:
        raise ValueError(f"user id {user_id!r} is not one path component")
    _ensure_control_level(base, parents=False)
    _ensure_control_level(user_level, parents=False)
    return user_level


def _fill(
    worktree: Path,
    bundle: DiffBundle,
    head: str,
    run_dir: Path,
    max_bytes: int,
    max_file_bytes: int,
) -> Snapshot:
    tree_dir = run_dir / "tree"
    meta_dir = run_dir / "meta"
    os.mkdir(tree_dir, 0o700)
    os.mkdir(meta_dir, 0o700)
    snapshot = Snapshot(run_dir=run_dir, tree_dir=tree_dir)
    skipped: list[tuple[str, str]] = []

    candidates = _list_tree(worktree, head, skipped)
    ordered = _order(candidates, bundle)
    # One blob must fit one batch call, whatever the configuration says.
    max_file_bytes = min(
        max(0, max_file_bytes), BATCH_MAX_BYTES - _BATCH_HEADER_ALLOWANCE
    )
    max_bytes = max(0, max_bytes)

    chosen: list[_Entry] = []
    total = 0
    for entry in ordered:
        if snapshot.truncated:
            skipped.append((SKIP_OVER_BUDGET, entry.path))
        elif entry.size > max_file_bytes:
            skipped.append((SKIP_TOO_LARGE, entry.path))
        elif total + entry.size > max_bytes:
            snapshot.truncated = True
            skipped.append((SKIP_OVER_BUDGET, entry.path))
        else:
            chosen.append(entry)
            total += entry.size

    for batch in _batches(chosen):
        contents = _read_blobs(worktree, batch)
        for entry in batch:
            data = contents.get(entry.oid)
            if data is None:
                skipped.append((SKIP_MISSING, entry.path))
                continue
            if not _write_tree_file(tree_dir, entry.path, data):
                skipped.append((SKIP_BAD_PATH, entry.path))
                continue
            snapshot.files += 1
            snapshot.bytes += len(data)

    _write_meta(meta_dir, "diff.patch", bundle.raw_body.encode("utf-8"))
    _write_meta(meta_dir, "stat.txt", bundle.stat.encode("utf-8"))
    changed = "".join(f"{_line_safe(path)}\n" for path in bundle.files)
    _write_meta(meta_dir, "changed.txt", changed.encode("utf-8"))
    _write_meta(meta_dir, "history.txt", _history(worktree, bundle).encode("utf-8"))
    _write_meta(meta_dir, "skipped.txt", _skipped_text(skipped).encode("utf-8"))

    counts: dict[str, int] = {}
    for reason, _ in skipped:
        counts[reason] = counts.get(reason, 0) + 1
    snapshot.skipped = counts
    return snapshot


def _list_tree(worktree: Path, head: str, skipped: list[tuple[str, str]]) -> list[_Entry]:
    """The regular files at `head`, in `ls-tree` order.

    Everything else is appended to `skipped` with its reason. `head` is a
    validated object id, so it cannot be read as an option.
    """
    raw = engine._git(
        worktree, ["ls-tree", "-r", "-z", "--long", "--full-tree", head]
    )
    entries: list[_Entry] = []
    for record in raw.split("\0"):
        if not record:
            continue
        meta, sep, path = record.partition("\t")
        fields = meta.split()
        if not sep or len(fields) != 4:
            continue
        mode, kind, oid, size = fields
        if mode == "120000":
            skipped.append((SKIP_SYMLINK, path))
        elif mode == "160000":
            skipped.append((SKIP_SUBMODULE, path))
        elif mode not in _REGULAR_MODES or kind != "blob" or not size.isdigit():
            skipped.append((SKIP_UNSUPPORTED, path))
        elif engine._safe_repo_path(path) != path:
            skipped.append((SKIP_BAD_PATH, path))
        else:
            entries.append(_Entry(path=path, oid=oid, size=int(size)))
    return entries


def _order(entries: list[_Entry], bundle: DiffBundle) -> list[_Entry]:
    """Changed files, then their directories' other files, then the rest.

    So a cap drops the files least likely to matter to the review.
    """
    excluded = set(bundle.deleted) | set(bundle.binary)
    changed = [path for path in bundle.files if path not in excluded]
    rank = {path: index for index, path in enumerate(changed)}
    directories = {posixpath.dirname(path) for path in changed}
    first: list[_Entry] = []
    second: list[_Entry] = []
    rest: list[_Entry] = []
    for entry in entries:
        if entry.path in rank:
            first.append(entry)
        elif posixpath.dirname(entry.path) in directories:
            second.append(entry)
        else:
            rest.append(entry)
    first.sort(key=lambda entry: rank[entry.path])
    return first + second + rest


def _batches(entries: list[_Entry]) -> list[list[_Entry]]:
    batches: list[list[_Entry]] = []
    current: list[_Entry] = []
    size = 0
    for entry in entries:
        cost = entry.size + _BATCH_HEADER_ALLOWANCE
        if current and size + cost > BATCH_MAX_BYTES:
            batches.append(current)
            current, size = [], 0
        current.append(entry)
        size += cost
    if current:
        batches.append(current)
    return batches


def _read_blobs(worktree: Path, batch: list[_Entry]) -> dict[str, bytes]:
    """Blob content by object id, from one `cat-file --batch` call."""
    request = "".join(f"{entry.oid}\n" for entry in batch).encode("ascii")
    raw = engine._git_batch(worktree, ["cat-file", "--batch"], request)
    contents: dict[str, bytes] = {}
    position = 0
    while position < len(raw):
        newline = raw.find(b"\n", position)
        if newline < 0:
            break
        header = raw[position:newline].decode("ascii", "replace").split()
        position = newline + 1
        if len(header) == 2 and header[1] == "missing":
            continue
        if len(header) != 3 or not header[2].isdigit():
            raise ReviewError(
                "cat-file --batch returned an unreadable header",
                reason="snapshot_failed",
            )
        oid, kind, size = header[0], header[1], int(header[2])
        body = raw[position:position + size]
        if len(body) != size:
            raise ReviewError(
                "cat-file --batch returned a short object", reason="snapshot_failed"
            )
        position += size + 1  # the newline after each object
        if kind == "blob":
            contents[oid] = body
    return contents


# The errors that mean "this path cannot exist here beside the others" rather
# than "the disk is broken": a collision on a case-insensitive filesystem, a
# path that is both a file and a directory in a crafted tree, a name too long.
_PATH_CONFLICT_ERRNOS = frozenset(
    {errno.EEXIST, errno.ENOTDIR, errno.EISDIR, errno.ENAMETOOLONG, errno.ELOOP}
)


def _write_tree_file(tree_dir: Path, path: str, data: bytes) -> bool:
    """Write one blob under `tree_dir`. False when the path conflicts."""
    parts = path.split("/")
    directory = tree_dir
    try:
        for part in parts[:-1]:
            directory = directory / part
            try:
                os.mkdir(directory, 0o700)
            except FileExistsError:
                pass
        _write_leaf(directory / parts[-1], data)
    except OSError as e:
        if e.errno in _PATH_CONFLICT_ERRNOS:
            return False
        raise
    return True


def _write_leaf(path: Path, data: bytes) -> None:
    fd = os.open(
        path,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
        0o600,
    )
    try:
        view = memoryview(data)
        while view:
            written = os.write(fd, view)
            view = view[written:]
    finally:
        os.close(fd)


def _write_meta(meta_dir: Path, name: str, data: bytes) -> None:
    _write_leaf(meta_dir / name, data)


def _line_safe(text: str) -> str:
    """One line, whatever the path holds: control characters as `\\xNN`."""
    return "".join(
        f"\\x{ord(char):02x}" if ord(char) < 0x20 or ord(char) == 0x7F else char
        for char in text
    )


def _skipped_text(skipped: list[tuple[str, str]]) -> str:
    lines: list[str] = []
    over_budget = 0
    for reason, path in skipped:
        if reason == SKIP_OVER_BUDGET:
            over_budget += 1
            if over_budget > MAX_OVER_BUDGET_LINES:
                continue
        lines.append(f"{reason}\t{_line_safe(path)}\n")
    if over_budget > MAX_OVER_BUDGET_LINES:
        lines.append(
            f"{SKIP_OVER_BUDGET}\t... and {over_budget - MAX_OVER_BUDGET_LINES} "
            "more paths not written\n"
        )
    return "".join(lines)


def _range_base(worktree: Path, rng: str) -> str | None:
    """The commit the range's diff is taken against, as an object id.

    The merge base for `a...b`, which is what `git diff` compares against;
    the left side for `a..b`; the revision itself for a single one. None when
    there is no merge base.
    """
    def resolve(rev: str) -> str:
        return engine._git(
            worktree,
            ["rev-parse", "--verify", END_OF_OPTIONS, f"{rev or 'HEAD'}^{{commit}}"],
            reason="bad_range",
        ).strip()

    if "..." in rng:
        left, _, right = rng.partition("...")
        left_id = engine._require_object_id(resolve(left), "base")
        right_id = engine._require_object_id(resolve(right), "head")
        try:
            base = engine._git(worktree, ["merge-base", left_id, right_id]).strip()
        except ReviewError:
            return None
        return engine._require_object_id(base, "merge base")
    if ".." in rng:
        left, _, _ = rng.partition("..")
        return engine._require_object_id(resolve(left), "base")
    return engine._require_object_id(resolve(rng), "base")


def _existing_at(worktree: Path, base: str, paths: list[str]) -> set[str]:
    """Which of `paths` name an object at `base`.

    `cat-file --batch-check` with `<rev>:<path>` requests on stdin, so no path
    is ever an argv element. A path holding a newline cannot be asked about
    this way and is treated as absent.
    """
    asked = [path for path in paths if "\n" not in path]
    if not asked:
        return set()
    request = "".join(f"{base}:{path}\n" for path in asked).encode("utf-8")
    raw = engine._git_batch(worktree, ["cat-file", "--batch-check"], request)
    answers = raw.decode("utf-8", "replace").split("\n")
    present: set[str] = set()
    for path, answer in zip(asked, answers):
        if not answer.endswith(" missing") and not answer.endswith(" ambiguous"):
            present.add(path)
    return present


def _history(worktree: Path, bundle: DiffBundle) -> str:
    """Recent commits touching each changed file, as of the range's base.

    The reviewer has no git; this is what stands in for "check what moved
    recently". Only files that exist at the base: an added file has no
    history before the range.
    """
    base = _range_base(worktree, bundle.rng)
    if base is None:
        return "[no merge base for the range, so no history]\n"
    paths = bundle.files[:MAX_HISTORY_FILES]
    present = _existing_at(worktree, base, paths)
    out: list[str] = []
    size = 0
    for path in paths:
        if path not in present:
            continue
        log = engine._git(
            worktree,
            [
                "--literal-pathspecs",
                "log",
                "-n",
                str(HISTORY_COMMITS_PER_FILE),
                "--format=%h %ad %s",
                "--date=short",
                "--no-show-signature",
                END_OF_OPTIONS,
                base,
                "--",
                path,
            ],
        )
        block = f"== {_line_safe(path)}\n{log.rstrip()}\n\n"
        if size + len(block) > MAX_HISTORY_BYTES:
            out.append("[history truncated]\n")
            break
        out.append(block)
        size += len(block)
    return "".join(out)


def sweep_stale_run_dirs(
    temp_dir: Path,
    *,
    max_age_seconds: int = RUN_DIR_MAX_AGE_SECONDS,
    now: float | None = None,
) -> int:
    """Remove `{temp_dir}/.review/*/run-*` and `{temp_dir}/.sandbox-work/*/work-*`
    directories older than `max_age_seconds`. Returns how many went.

    The backstop for a review whose process died before its own cleanup. The
    private work dir `executor.build_daemon_sandbox` makes for the same review
    is covered by the same rule, since it has the same lifetime. Never follows
    a link at any level, and never raises.
    """
    import time

    from istota.executor import DAEMON_SCRATCH_DIR_NAME

    cutoff = (time.time() if now is None else now) - max_age_seconds
    removed = 0
    for name, prefix in (
        (REVIEW_DIR_NAME, RUN_DIR_PREFIX),
        (DAEMON_SCRATCH_DIR_NAME, "work-"),
    ):
        base = Path(temp_dir) / name
        for user_level in _real_subdirs(base):
            for run in _real_subdirs(user_level):
                if not run.name.startswith(prefix):
                    continue
                try:
                    if run.stat(follow_symlinks=False).st_mtime >= cutoff:
                        continue
                    shutil.rmtree(run)
                    removed += 1
                except OSError as e:
                    logger.warning(
                        "code_review_run_dir_sweep_failed path=%s error=%s", run, e
                    )
    return removed


def _real_subdirs(directory: Path) -> list[Path]:
    """Directories directly under `directory`, links excluded. Never raises."""
    try:
        if not directory.is_dir() or directory.is_symlink():
            return []
        with os.scandir(directory) as it:
            return [
                Path(entry.path)
                for entry in it
                if entry.is_dir(follow_symlinks=False)
            ]
    except OSError:
        return []
