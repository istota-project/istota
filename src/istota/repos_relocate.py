"""One-time relocation of ``developer.repos_dir`` into per-user subtrees.

The old layout was one shared tree, ``{repos_dir}/{namespace}/{project}.git``,
bound read-write into every admin developer task. Every admin could therefore
read and write every other admin's clones, worktrees, model-written git configs
and package caches — the root cause behind ISSUE-319 and behind
``skill_host_paths.resolve_under_repos`` accepting another admin's checkout.
The layout is now ``{repos_dir}/{user_id}/{namespace}/{project}.git`` and a
task binds only its own subtree, so the reach is closed structurally rather
than by a mask.

This module moves an existing deployment across, mirroring
:mod:`istota.db_relocate`: a one-shot migrator invoked by the Ansible role, so
the judgement lives in Python where it can be tested rather than in untested
YAML run once, during a real migration, on the only copy of the data.

    python -m istota.repos_relocate             # migrate
    python -m istota.repos_relocate --dry-run   # print the plan, touch nothing
    python -m istota.repos_relocate --list      # report the tree as it stands

**Ownership is the whole problem.** Nothing on disk says which user owns a
clone — a forge namespace is not a user id, and the two can legitimately share
a name. So the destination is inferred from exactly one configured admin and
from nothing else. Zero admins (a missing or empty admins file, which
``Config.is_admin`` reads as "everyone is an admin") or more than one is a
**refusal**: the namespaces found and the destination that cannot be inferred
are printed and the exit code is non-zero. Guessing wrong hands one admin's
clones to another, which is the exact exposure the per-user layout exists to
remove, so a migrator that refuses loudly is worth more than one that guesses.

**The move is a rename per namespace.** Worktrees are siblings of their bare
clone inside the namespace directory, so moving ``{repos_dir}/{namespace}``
wholesale takes the clone and its worktrees together in one same-filesystem
rename, with no window where half a repository exists at each path.

**Then ``git worktree repair``, with the new paths.** git stores absolute paths
in both directions — the worktree's ``.git`` file names the repository's
administrative directory, and ``worktrees/<id>/gitdir`` names the worktree —
and a wholesale rename breaks both at once. Measured before this module was
written: ``git worktree repair`` **with no arguments** then succeeds, prints
nothing and repairs nothing, because the stale record is git's only clue about
where to look. The new paths have to be passed explicitly, and they are read
from each clone's own ``worktrees/*/gitdir`` records rather than guessed from
the sibling directory names, so a worktree cut anywhere under the namespace is
found and one cut outside it is reported instead of being silently mismapped.

**Idempotency by marker, not by inference.** A successful run writes
``{repos_dir}/.istota-layout`` containing ``2``; its presence means migrated.
Inferring from directory names is ambiguous in exactly the way ownership is,
and the marker is unambiguous and cheap. The marker goes down only when every
namespace moved — a half-migrated tree carrying a marker would never be
retried.

**Two guards.** A task in ``locked`` or ``running`` refuses the whole run:
moving a clone out from under a live task destroys it. And the second run is a
no-op, so Ansible can call this unconditionally.

**The destination name is never a move candidate.** ``{repos_dir}/{user_id}``
is where everything is going, and it may already exist — the developer skill's
``setup_env`` creates it at 0700, and a crashed earlier run leaves it holding
the namespaces that did move. Treating it as a namespace would rename it into
itself (``EINVAL``) on a fresh tree and nest the tree one level deeper on a
re-run. The cost is that a forge namespace legitimately named after the admin
stays where it is, one level shallower than the rest — inside that user's own
root, which is where it belongs, and reported rather than passed over.

Every ``git`` invocation carries :data:`istota.git_hardening.GIT_HARDENING`,
for the reason it always does under ``repos_dir``: repository config is
model-written and a plain ``git`` command there runs whatever
``core.fsmonitor`` names, as the daemon user, with the daemon's environment.

Error posture follows :mod:`istota.worktree_reaper` and
:mod:`istota.sandbox_cache_sweeper`, the other two delete-adjacent paths:
nothing raises out of :func:`main`, what could not be done is reported rather
than passed over in silence, and a refusal is distinguishable from a success by
the exit code as well as by the text. ``--dry-run`` touches nothing.

The planning and applying halves are a stdlib-only leaf — the root and the
admin set are parameters. Only :func:`main` reads configuration, the admins
file and the task table.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from .git_hardening import GIT_HARDENING

logger = logging.getLogger(__name__)

#: Written into ``{repos_dir}`` by a successful run; its presence is the whole
#: idempotency story.
MARKER_NAME = ".istota-layout"
LAYOUT_VERSION = "2"

#: 0 success (including "nothing to do" and "already migrated"), 1 refusal
#: (nothing was touched), 2 partial (something moved and something did not).
#: Three rather than two because Ansible reads the code: a refusal is an
#: operator decision, a partial is an operator repair, and they are not the
#: same job.
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_PARTIAL = 2

REFUSE_NO_ADMINS = "no_admins"
REFUSE_MANY_ADMINS = "many_admins"
REFUSE_NOT_A_DIRECTORY = "not_a_directory"
REFUSE_UNREADABLE = "unreadable_root"
REFUSE_COLLISION = "destination_collision"
REFUSE_LIVE_TASKS = "live_tasks"
REFUSE_TASK_TABLE = "task_table_unreadable"
REFUSE_NO_CONFIG = "config_unreadable"

_GIT_TIMEOUT = 300

#: How far below a namespace directory to look for repositories. The documented
#: layout puts a bare clone at depth 1 (``{namespace}/{project}.git``); the
#: extra levels cover a forge subgroup. The walk prunes at every repository it
#: recognises, so this bounds a tree of ordinary directories rather than a
#: tree of git objects.
_MAX_SCAN_DEPTH = 4


# ---------------------------------------------------------------------------
# What a plan is
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Move:
    """One namespace directory and where it is going."""

    namespace: str
    src: Path
    dst: Path


@dataclass(frozen=True)
class Repair:
    """One bare clone's worktrees, as they are recorded and as they will be.

    ``worktrees`` holds ``(recorded, translated)`` pairs read from the clone's
    own ``worktrees/*/gitdir`` files. ``stale`` holds records that name a path
    outside the namespace being moved, which no translation can follow.
    """

    clone_src: Path
    clone_dst: Path
    worktrees: tuple[tuple[Path, Path], ...] = ()
    stale: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelocatePlan:
    repos_dir: Path
    marker: Path
    user_id: str
    moves: tuple[Move, ...] = ()
    repairs: tuple[Repair, ...] = ()
    already_migrated: bool = False
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelocateRefusal:
    """Nothing was touched and nothing will be until an operator decides."""

    reason: str
    message: str
    details: tuple[str, ...] = ()


@dataclass(frozen=True)
class RelocateReport:
    moved: tuple[str, ...] = ()
    repaired: tuple[str, ...] = ()
    unrepaired: tuple[str, ...] = ()
    failed: tuple[str, ...] = ()
    marker_written: bool = False
    notes: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not self.failed and not self.unrepaired


# ---------------------------------------------------------------------------
# Reading the tree
# ---------------------------------------------------------------------------


def _marker_content(repos_dir: Path) -> str | None:
    """The marker's text, or None when there is no marker.

    Presence is what counts, not the value: a marker naming some other version
    still means a migration ran, and re-running this one over a tree somebody
    else already rearranged is the more dangerous reading. The content is
    carried into the report so an unexpected value is visible.

    **An error is never read as presence.** A root that is unreadable, or that
    is not a directory at all, raises here — and answering "migrated" would
    turn both into a silent exit 0 that reports a migration nobody performed.
    :func:`plan` establishes that the root is listable before it asks this,
    and this returns None on anything else so the two cannot disagree.
    """
    try:
        return (repos_dir / MARKER_NAME).read_text(errors="replace")
    except OSError:
        return None


def _is_git_dir(path: Path) -> bool:
    """The standard shape of a git directory, bare or otherwise."""
    try:
        return (
            (path / "HEAD").is_file()
            and (path / "objects").is_dir()
            and (path / "refs").is_dir()
        )
    except OSError:
        return False


def _git_dirs(root: Path, max_depth: int = _MAX_SCAN_DEPTH) -> list[Path]:
    """Every git directory under ``root``, pruning at each one found.

    A checkout (a directory whose ``.git`` is a *file*) is pruned too: its
    contents are working-tree files, and descending into them would look for
    repositories inside whatever the model checked out.
    """
    found: list[Path] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if _is_git_dir(directory):
            found.append(directory)
            continue
        dot_git = directory / ".git"
        try:
            if dot_git.is_file():
                # A linked or ordinary worktree. Its repository is elsewhere.
                continue
        except OSError:
            continue
        if _is_git_dir(dot_git):
            found.append(dot_git)
            continue
        if depth >= max_depth:
            continue
        try:
            entries = list(os.scandir(directory))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError:
                continue
            stack.append((Path(entry.path), depth + 1))
    return found


def _recorded_worktrees(git_dir: Path) -> list[Path]:
    """Worktree paths as the repository records them.

    Each ``worktrees/<id>/gitdir`` holds the absolute path of that worktree's
    ``.git`` *file*; the worktree itself is its parent. Read rather than
    inferred from sibling directory names, because one namespace can hold
    several projects and a worktree can have been cut anywhere.
    """
    out: list[Path] = []
    try:
        entries = sorted(os.scandir(git_dir / "worktrees"), key=lambda e: e.name)
    except OSError:
        return out
    for entry in entries:
        try:
            raw = Path(entry.path, "gitdir").read_bytes()
        except OSError:
            continue
        text = raw.decode("utf-8", "surrogateescape").strip()
        if not text:
            continue
        recorded = Path(text)
        out.append(recorded.parent if recorded.name == ".git" else recorded)
    return out


def _translate(path: Path, src_root: Path, dst_root: Path) -> Path | None:
    """``path`` with ``src_root`` swapped for ``dst_root``, or None.

    None for a relative path or one that is not under the namespace being
    moved — a worktree somewhere else on disk is not this rename's to fix, and
    a guess would point a repository at a directory it does not own.
    """
    if not path.is_absolute():
        return None
    try:
        relative = path.relative_to(src_root)
    except ValueError:
        return None
    return dst_root / relative


def _contained(root: Path, name: str) -> bool:
    """``{root}/{name}`` really is a child of ``root``, symlinks included.

    The same equality rule ``executor.get_user_repos_dir`` and
    ``sandbox_cache_sweeper`` use. The entries here were model-writable on
    every deployment running the old shared bind, so an entry that resolves
    somewhere else is a thing that can actually be there.
    """
    candidate = root / name
    try:
        return (
            candidate.parent == root
            and candidate.resolve() == root.resolve() / name
        )
    except OSError:
        return False


@dataclass
class _Listing:
    """What is directly under ``repos_dir``, classified."""

    namespaces: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _read_root(repos_dir: Path) -> _Listing | RelocateRefusal:
    """Classify the root's entries into movable namespaces and everything else."""
    listing = _Listing()
    try:
        entries = sorted(os.scandir(repos_dir), key=lambda e: e.name)
    except FileNotFoundError:
        return listing
    except NotADirectoryError:
        return RelocateRefusal(
            REFUSE_NOT_A_DIRECTORY,
            f"{repos_dir} is not a directory.",
            (f"developer.repos_dir names {repos_dir}, which is not a directory.",),
        )
    except OSError as exc:
        return RelocateRefusal(
            REFUSE_UNREADABLE,
            f"{repos_dir} could not be listed ({exc}).",
            (
                "Nothing was touched. The migrator cannot tell an empty tree "
                "from an unreadable one, and the two want opposite actions.",
            ),
        )
    for entry in entries:
        name = entry.name
        if name == MARKER_NAME:
            continue
        try:
            is_symlink = entry.is_symlink()
            is_dir = entry.is_dir(follow_symlinks=False)
        except OSError as exc:
            listing.notes.append(f"{name}: could not be classified ({exc}); left in place")
            continue
        if is_symlink:
            # Moving a symlink moves the link, and the link was plantable from
            # inside a task on every deployment running the shared bind. Left
            # at the root, where nothing binds it any more.
            listing.notes.append(f"{name}: a symlink; left in place, not moved")
            continue
        if not is_dir:
            listing.notes.append(f"{name}: not a directory; left in place")
            continue
        if name.startswith("."):
            # `.package-caches` is the cc691d6f cache root. It is a cache, not
            # a repository, and its per-user directories are named *from disk* —
            # the one axis this layout must never trust, since a task could
            # create a directory named for a user who has never run one. So it
            # is reported and left rather than moved into somebody's subtree.
            # It is orphaned by the move (the caches derive per user now) and
            # safe for an operator to delete.
            listing.notes.append(f"{name}: not a namespace; left in place")
            continue
        if not _contained(repos_dir, name):
            listing.notes.append(
                f"{name}: does not resolve to a child of {repos_dir}; left in place"
            )
            continue
        listing.namespaces.append(name)
    return listing


# ---------------------------------------------------------------------------
# plan
# ---------------------------------------------------------------------------


def plan(
    repos_dir: str | Path, admins: Iterable[str]
) -> RelocatePlan | RelocateRefusal:
    """What the migration would do, or why it will not do it.

    Filesystem-only and side-effect free: it reads the tree and the admin set
    it is handed, and writes nothing. The live-task guard is :func:`main`'s,
    because it needs the task table and this stays a leaf.

    The order of the questions is deliberate. Whether the root can be read at
    all comes first, because every later answer is a guess otherwise and the
    cheerful guess — "no marker, no namespaces, nothing to do" — writes a
    marker over a tree nobody has looked at. The marker is next, so a migrated
    deployment that later gains a second admin is still a no-op rather than a
    refusal. Ownership is asked only when there is something to place, so a
    fresh install with no admins file — a shape the single-user install ships —
    writes its marker instead of refusing forever.
    """
    root = Path(repos_dir)
    marker = root / MARKER_NAME

    listing = _read_root(root)
    if isinstance(listing, RelocateRefusal):
        return listing

    content = _marker_content(root)
    if content is not None:
        notes = ()
        if content.strip() != LAYOUT_VERSION:
            notes = (
                f"{marker} holds {content.strip()!r} rather than {LAYOUT_VERSION!r}; "
                "treated as migrated",
            )
        return RelocatePlan(
            repos_dir=root, marker=marker, user_id="", already_migrated=True, notes=notes,
        )

    if not listing.namespaces:
        # A fresh install is already in the new layout. No owner is needed
        # because nothing is being placed.
        return RelocatePlan(
            repos_dir=root, marker=marker, user_id="", notes=tuple(listing.notes),
        )

    admin_list = sorted({a for a in admins if a})
    found = ", ".join(admin_list) if admin_list else "none"
    ambiguity = (
        f"namespaces found under {root}: {', '.join(listing.namespaces)}",
        f"admins configured: {found}",
        "Each namespace has to become {repos_dir}/{user_id}/{namespace} and "
        "nothing on disk says which user owns one — a forge namespace is not a "
        "user id.",
        "Fix the admins file so it names exactly one user and re-run, or move "
        f"the namespaces by hand and write {LAYOUT_VERSION!r} into {marker}.",
    )
    if not admin_list:
        return RelocateRefusal(
            REFUSE_NO_ADMINS,
            "no admin is configured, so the destination cannot be inferred.",
            ambiguity,
        )
    if len(admin_list) > 1:
        return RelocateRefusal(
            REFUSE_MANY_ADMINS,
            f"{len(admin_list)} admins are configured, so the destination "
            "cannot be inferred.",
            ambiguity,
        )

    user_id = admin_list[0]
    dst_root = root / user_id
    notes = list(listing.notes)

    moves: list[Move] = []
    collisions: list[str] = []
    for name in listing.namespaces:
        if name == user_id:
            # The destination itself: either the subtree `setup_env` created,
            # or a half-migrated tree from a run that died, or a forge
            # namespace that happens to be named after its owner. All three
            # are already inside the right user's root.
            notes.append(
                f"{name}: already the destination directory; left in place"
            )
            continue
        src = root / name
        dst = dst_root / name
        if dst.exists():
            collisions.append(f"{dst} already exists")
            continue
        moves.append(Move(namespace=name, src=src, dst=dst))

    if collisions:
        return RelocateRefusal(
            REFUSE_COLLISION,
            "a namespace already exists at its destination.",
            (
                *collisions,
                "The two trees are not merged: a collision here means the "
                "assumption behind the migration is wrong about this tree.",
                "Resolve it by hand and re-run.",
            ),
        )

    repairs: list[Repair] = []
    for move in moves:
        for git_dir in _git_dirs(move.src):
            recorded = _recorded_worktrees(git_dir)
            if not recorded:
                continue
            pairs: list[tuple[Path, Path]] = []
            stale: list[str] = []
            for old in recorded:
                new = _translate(old, move.src, move.dst)
                if new is None:
                    stale.append(str(old))
                else:
                    pairs.append((old, new))
            clone_dst = _translate(git_dir, move.src, move.dst)
            if clone_dst is None:  # pragma: no cover - git_dir is under move.src
                continue
            repairs.append(
                Repair(
                    clone_src=git_dir,
                    clone_dst=clone_dst,
                    worktrees=tuple(pairs),
                    stale=tuple(stale),
                )
            )

    return RelocatePlan(
        repos_dir=root,
        marker=marker,
        user_id=user_id,
        moves=tuple(moves),
        repairs=tuple(repairs),
        notes=tuple(notes),
    )


# ---------------------------------------------------------------------------
# apply
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> tuple[int, str]:
    """``(exit_status, output)``. Never raises.

    ``GIT_HARDENING`` first, before ``-C``: this runs against repositories
    whose own config the model writes, and a later ``-c`` beats the
    repository's value. The two ``GIT_CONFIG_*`` variables cover the system and
    user files and do nothing about the repository's own, which is the writable
    one. Bytes are decoded rather than rejected — ``text=True`` would raise
    ``UnicodeDecodeError`` on a repository holding one non-UTF-8 path.
    """
    try:
        proc = subprocess.run(
            ["git", *GIT_HARDENING, "-C", str(cwd), *args],
            capture_output=True, timeout=_GIT_TIMEOUT,
            env={
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
            },
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    out = proc.stdout + proc.stderr
    return proc.returncode, out.decode("utf-8", "surrogateescape")


def _links_back(worktree: Path, git_dir: Path) -> bool:
    """The worktree's ``.git`` file names an administrative directory in ``git_dir``.

    A file read rather than another subprocess, and it asks the question the
    repair was for directly: after a wholesale rename both halves of the link
    are stale, and this is the half that lives in the worktree.
    """
    try:
        text = (worktree / ".git").read_bytes().decode("utf-8", "surrogateescape")
    except OSError:
        return False
    prefix = "gitdir:"
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith(prefix):
            continue
        target = Path(line[len(prefix):].strip())
        try:
            return target.is_relative_to(git_dir / "worktrees")
        except (OSError, ValueError):
            return False
    return False


def apply(plan: RelocatePlan) -> RelocateReport:
    """Carry out a plan. Never raises.

    One namespace at a time, and a namespace that cannot be moved is recorded
    and the rest continue — stopping halfway through leaves a tree in the same
    shape as finishing halfway through, minus the repairs. The marker is only
    written when every move landed, so a partial run is retried rather than
    declared done.
    """
    notes = list(plan.notes)
    if plan.already_migrated:
        return RelocateReport(notes=tuple(notes) or ("already migrated",))

    moved: list[str] = []
    failed: list[str] = []
    repaired: list[str] = []
    unrepaired: list[str] = []

    if plan.moves:
        dst_root = plan.repos_dir / plan.user_id
        try:
            dst_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return RelocateReport(
                failed=(f"{dst_root}: could not be created ({exc})",),
                notes=tuple(notes),
            )
        try:
            # `mkdir`'s mode is umask-dependent, so chmod explicitly — the same
            # idiom, and the same 0700, the developer skill's `setup_env` uses
            # on this exact directory.
            os.chmod(dst_root, 0o700)
        except OSError as exc:
            notes.append(f"{dst_root}: could not be set to 0700 ({exc})")

    for move in plan.moves:
        try:
            os.rename(move.src, move.dst)
        except OSError as exc:
            failed.append(f"{move.namespace}: could not be moved ({exc})")
            logger.error(
                "repos_relocate_move_failed namespace=%s src=%s dst=%s err=%s",
                move.namespace, move.src, move.dst, exc,
            )
            continue
        moved.append(move.namespace)
        logger.info("repos_relocated namespace=%s -> %s", move.namespace, move.dst)

    moved_set = set(moved)
    for repair in plan.repairs:
        if not _repair_is_live(repair, plan, moved_set):
            # Its namespace did not move, so both halves of the link still
            # point where they always did and there is nothing to repair.
            continue
        for record in repair.stale:
            notes.append(
                f"{repair.clone_dst}: worktree record {record} is outside the "
                "moved namespace; not repaired"
            )
        targets = [new for _old, new in repair.worktrees if new.exists()]
        for _old, new in repair.worktrees:
            if not new.exists():
                # A checkout removed by hand leaves its record behind until
                # someone runs `git worktree prune`. Ordinary git litter, not a
                # migration failure — there is no worktree left to break.
                notes.append(f"{new}: recorded worktree is not on disk; nothing to repair")
        if not targets:
            continue
        status, output = _git(
            repair.clone_dst, "worktree", "repair", *[str(t) for t in targets]
        )
        if status != 0:
            notes.append(
                f"{repair.clone_dst}: git worktree repair exited {status}: "
                f"{output.strip().splitlines()[-1] if output.strip() else ''}"
            )
        for target in targets:
            if _links_back(target, repair.clone_dst):
                repaired.append(str(target))
            else:
                unrepaired.append(str(target))
                logger.error(
                    "repos_relocate_repair_failed worktree=%s clone=%s",
                    target, repair.clone_dst,
                )

    marker_written = False
    if not failed:
        try:
            plan.repos_dir.mkdir(parents=True, exist_ok=True)
            plan.marker.write_text(f"{LAYOUT_VERSION}\n")
            marker_written = True
        except OSError as exc:
            failed.append(f"{plan.marker}: could not be written ({exc})")

    return RelocateReport(
        moved=tuple(moved),
        repaired=tuple(repaired),
        unrepaired=tuple(unrepaired),
        failed=tuple(failed),
        marker_written=marker_written,
        notes=tuple(notes),
    )


def _repair_is_live(repair: Repair, plan: RelocatePlan, moved: set[str]) -> bool:
    """Only repair inside a namespace that actually moved."""
    for move in plan.moves:
        try:
            if repair.clone_src.is_relative_to(move.src):
                return move.namespace in moved
        except (OSError, ValueError):  # pragma: no cover - both are pure paths
            continue
    return False


# ---------------------------------------------------------------------------
# The CLI
# ---------------------------------------------------------------------------


def _live_task_refusal(config) -> RelocateRefusal | None:
    """Refuse while any task holds a live worker.

    Fail-closed on an unreadable task table, matching the cache sweeper: an
    empty set there reads as "nobody is working", which is the one wrong answer
    that costs a running task its clone. A framework database that does not
    exist is a different fact and not fail-closed — nothing can be running
    against a database that is not there, and refusing would fail the very
    first deploy.
    """
    from . import db

    db_path = Path(config.db_path) if config.db_path else None
    if db_path is None or not db_path.exists():
        return None
    try:
        with db.get_db(db_path) as conn:
            busy = db.get_users_with_live_tasks(conn)
    except Exception as exc:  # noqa: BLE001 - never raises out of main
        return RelocateRefusal(
            REFUSE_TASK_TABLE,
            f"the task table could not be read ({exc}).",
            (
                "Nothing was touched. Whether a task is running is unknown, and "
                "moving a clone out from under one destroys it.",
            ),
        )
    if busy:
        return RelocateRefusal(
            REFUSE_LIVE_TASKS,
            "a task is in flight.",
            (
                f"users with work in flight: {', '.join(sorted(busy))}",
                "Stop the scheduler (or wait for the tasks to finish) and "
                "re-run. Moving a clone out from under a live task destroys it.",
            ),
        )
    return None


def _print_refusal(refusal: RelocateRefusal) -> None:
    print(f"repos_relocate: refused — {refusal.message}", file=sys.stderr)
    for line in refusal.details:
        print(f"  {line}", file=sys.stderr)


def _print_plan(plan: RelocatePlan) -> None:
    if plan.already_migrated:
        print(f"repos_relocate: already migrated ({plan.marker} is present)")
    elif not plan.moves:
        print(f"repos_relocate: nothing to migrate under {plan.repos_dir}")
    else:
        print(f"repos_relocate: {len(plan.moves)} namespace(s) -> {plan.user_id}")
        for move in plan.moves:
            print(f"  {move.src} -> {move.dst}")
        for repair in plan.repairs:
            for _old, new in repair.worktrees:
                print(f"  repair {new}")
    for note in plan.notes:
        print(f"  note: {note}")


def _print_report(report: RelocateReport) -> None:
    for namespace in report.moved:
        print(f"moved: {namespace}")
    for worktree in report.repaired:
        print(f"repaired: {worktree}")
    for note in report.notes:
        print(f"note: {note}")
    for worktree in report.unrepaired:
        print(f"NOT repaired: {worktree}", file=sys.stderr)
    for failure in report.failed:
        print(f"FAILED: {failure}", file=sys.stderr)
    print(
        f"done: {len(report.moved)} moved, {len(report.repaired)} repaired, "
        f"{len(report.unrepaired)} unrepaired, {len(report.failed)} failed, "
        f"marker {'written' if report.marker_written else 'not written'}",
        file=sys.stderr,
    )


def _print_listing(repos_dir: Path) -> None:
    """Report the tree as it stands. Inspection only: never a verdict."""
    print(f"repos_dir: {repos_dir}")
    content = _marker_content(repos_dir)
    print(f"marker: {(content or '').strip() or 'absent'}")
    listing = _read_root(repos_dir)
    if isinstance(listing, RelocateRefusal):
        print(f"  unreadable: {listing.message}")
        return
    for name in listing.namespaces:
        print(f"  {name}/")
        for git_dir in _git_dirs(repos_dir / name):
            worktrees = _recorded_worktrees(git_dir)
            suffix = f" ({len(worktrees)} worktree(s))" if worktrees else ""
            print(f"    {git_dir}{suffix}")
            for worktree in worktrees:
                print(f"      {worktree}")
    for note in listing.notes:
        print(f"  note: {note}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="istota.repos_relocate",
        description=(
            "Move developer.repos_dir into per-user subtrees "
            "({repos_dir}/{user_id}/{namespace}/{project}.git)."
        ),
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print the plan and touch nothing.",
    )
    parser.add_argument(
        "--list", action="store_true", dest="list_only",
        help="Report the tree as it stands and exit; never migrates.",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    try:
        from .config import load_admin_users, load_config

        config = load_config()
    except Exception as exc:  # noqa: BLE001 - never raises out of main
        _print_refusal(
            RelocateRefusal(
                REFUSE_NO_CONFIG, f"the configuration could not be loaded ({exc}).",
            )
        )
        return EXIT_REFUSED

    configured = (getattr(config.developer, "repos_dir", "") or "").strip()
    if not configured:
        print("repos_relocate: developer.repos_dir is not configured; nothing to migrate")
        return EXIT_OK
    root = Path(configured)

    if args.list_only:
        _print_listing(root)
        return EXIT_OK

    outcome = plan(root, load_admin_users())
    if isinstance(outcome, RelocateRefusal):
        _print_refusal(outcome)
        return EXIT_REFUSED

    if outcome.already_migrated:
        _print_plan(outcome)
        return EXIT_OK

    if outcome.moves:
        # Only where something would actually move: a no-op cannot race a task.
        refusal = _live_task_refusal(config)
        if refusal is not None:
            _print_refusal(refusal)
            return EXIT_REFUSED

    if args.dry_run:
        _print_plan(outcome)
        return EXIT_OK

    report = apply(outcome)
    _print_report(report)
    if report.failed or report.unrepaired:
        return EXIT_PARTIAL
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
