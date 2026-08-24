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

**Every path this touches is model-plantable, the destination included.** The
old shared root was bound read-write into every admin task, so an entry under
it may be a symlink, and a ``worktrees/*/gitdir`` record inside a clone is a
file the model can write. Three separate rules follow from that, and the first
two are the ones review found missing:

1. ``{repos_dir}/{user_id}`` is held to the same containment **equality** the
   rest of the layout is (``executor.get_user_repos_dir``,
   ``sandbox_cache_sweeper``): it must resolve to its own name inside the root.
   A symlink there is refused rather than noted, because ``mkdir(exist_ok=True)``
   succeeds on a symlink to a directory, ``chmod`` follows it, and ``rename``
   traverses it — so the whole migration lands at an attacker-chosen path and
   reports success. The mode is then set through an ``O_NOFOLLOW`` descriptor
   rather than by name, the idiom ISSUE-317's sweeper settled on, so the check
   and the change cannot be separated by a swap.
2. **A worktree is only repaired if it currently links back to the clone being
   repaired.** ``git worktree repair <path>`` resolves *which repository to
   write* from ``<path>/.git``, not from the ``-C`` argument — measured, not
   assumed — so handing it a model-chosen path lets a record inside the moving
   clone redirect an unrelated repository's worktree into an attacker-owned
   directory. The precondition is the exact inverse of the verification, run
   against the clone's old path, and it is strictly better than "the directory
   exists".
3. A translated path that escapes the namespace is refused. ``relative_to`` is
   lexical, so ``..`` in a record survives it.

**The move is a rename per namespace, and its repairs run immediately after
it.** Worktrees are siblings of their bare clone inside the namespace
directory, so moving ``{repos_dir}/{namespace}`` wholesale takes the clone and
its worktrees together in one same-filesystem rename, with no window where half
a repository exists at each path. The repairs for that namespace follow at
once, rather than after every rename, so a process death exposes one namespace
instead of all of them.

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

**git records resolved paths, and this module is handed unresolved ones.** A
``repos_dir`` reached through a symlinked ancestor is a shape this codebase
already treats as live (``resolve_sandbox_cache_dir`` reasons about it), and
git canonicalises both records it writes. A lexical comparison then either
classifies every record as outside the namespace — nothing repaired, marker
written, exit 0, which is the dangerous direction — or reports every correctly
repaired worktree as broken. Both comparisons are made on realpaths for that
reason.

**Idempotency by marker, not by inference.** A successful run writes
``{repos_dir}/.istota-layout`` containing ``2``; its presence means migrated.
Inferring from directory names is ambiguous in exactly the way ownership is,
and the marker is unambiguous and cheap. The marker goes down only when every
namespace moved — a half-migrated tree carrying one would never be retried.

**And a progress file, because the marker cannot record a half-repaired tree.**
The namespaces that have moved are recorded in
``{repos_dir}/.istota-layout.in-progress`` as they move. Without it, a run
killed between the last rename and its repairs leaves a tree whose namespaces
are all in place and whose worktrees are all dead — and the next run finds
nothing to move, writes the marker and reports success, permanently. With it,
the next run knows exactly which namespaces moved and therefore what the stale
records still name, and finishes the repairs. Ownership is never taken from
that file: it is inside the model-writable root like everything else, and a
planted one naming another admin would be the cross-user handoff this spec
exists to prevent. Only the namespace list is read from it, and every entry is
re-checked against the tree.

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
import json
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

#: Written before the first rename and removed with the marker. What it buys is
#: in the module docstring: the marker records that the tree moved, and cannot
#: record that its worktrees were repaired.
PROGRESS_NAME = ".istota-layout.in-progress"

#: 0 success (including "nothing to do" and "already migrated"), 1 refusal
#: (nothing was touched), 2 partial (something moved and something did not).
#: Three rather than two because Ansible reads the code: a refusal is an
#: operator decision, a partial is an operator repair, and they are not the
#: same job. The code alone does not say *which* refusal, and the role needs
#: that — a `live_tasks` refusal is transient and the play carries on past it,
#: every other reason needs a person and stops it — so `_print_refusal` also
#: emits `refusal: <reason>` for a caller to match on.
EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_PARTIAL = 2

REFUSE_NO_ADMINS = "no_admins"
REFUSE_MANY_ADMINS = "many_admins"
REFUSE_NOT_A_DIRECTORY = "not_a_directory"
REFUSE_UNREADABLE = "unreadable_root"
REFUSE_COLLISION = "destination_collision"
REFUSE_DESTINATION = "destination_not_contained"
REFUSE_LIVE_TASKS = "live_tasks"
REFUSE_TASK_TABLE = "task_table_unreadable"
REFUSE_NO_CONFIG = "config_unreadable"

_GIT_TIMEOUT = 300

#: How far below a namespace directory to look for repositories. The documented
#: layout puts a bare clone at depth 1 (``{namespace}/{project}.git``); the
#: extra levels cover a forge subgroup. The walk prunes at every repository it
#: recognises, so this bounds a tree of ordinary directories rather than a tree
#: of git objects. Reaching it is reported rather than passed over: a clone the
#: walk never saw is a clone whose worktrees are never repaired, and that must
#: not be silent.
_MAX_SCAN_DEPTH = 4


# ---------------------------------------------------------------------------
# What a plan is
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Repair:
    """One clone's worktrees, as they are recorded and as they will be.

    ``clone_src`` is where the clone was when its records were written, which
    is what those records still name and therefore what the pre-check compares
    against. ``clone_dst`` is where it is now. On a resumed run the rename
    already happened, so ``clone_src`` names a path that no longer exists —
    that is correct and deliberate; it is a record, not a location to open.

    ``worktrees`` holds ``(recorded, translated)`` pairs. ``stale`` holds
    records no translation can follow, which are reported rather than guessed
    at.
    """

    namespace: str
    clone_src: Path
    clone_dst: Path
    worktrees: tuple[tuple[Path, Path], ...] = ()
    stale: tuple[str, ...] = ()


@dataclass(frozen=True)
class Move:
    """One namespace directory, where it is going, and its repairs.

    The repairs ride on the move rather than in a list beside it because they
    run immediately after that rename, which is what bounds a crash to one
    namespace.
    """

    namespace: str
    src: Path
    dst: Path
    repairs: tuple[Repair, ...] = ()


@dataclass(frozen=True)
class RelocatePlan:
    repos_dir: Path
    marker: Path
    user_id: str
    moves: tuple[Move, ...] = ()
    #: Repairs for namespaces a previous run moved and did not finish.
    resume_repairs: tuple[Repair, ...] = ()
    resumed: tuple[str, ...] = ()
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
        """Nothing outstanding. :func:`main`'s exit code is exactly this."""
        return not self.failed and not self.unrepaired


# ---------------------------------------------------------------------------
# Paths, and the two rules about them
# ---------------------------------------------------------------------------


def _real(path: Path) -> Path:
    """``realpath`` without requiring the path to exist.

    Every comparison against something git wrote goes through this. git
    canonicalises the paths it records, and this module is handed the
    configured spelling, so a ``repos_dir`` behind a symlinked ancestor makes
    the two disagree about paths that are in fact the same. ``realpath``
    resolves the components that exist and keeps the rest, which is what lets
    it be asked about a clone that has already been renamed away.
    """
    try:
        return Path(os.path.realpath(path))
    except OSError:  # pragma: no cover - realpath does not stat
        return path


def _contained(root: Path, name: str) -> bool:
    """``{root}/{name}`` really is a child of ``root``, symlinks included.

    The same equality rule ``executor.get_user_repos_dir`` and
    ``sandbox_cache_sweeper`` use, and for the same reason: truthiness alone
    lets through ``.`` (which collapses to the root), ``..`` (its parent) and
    an absolute component (which replaces the root outright), and the entries
    here were model-writable on every deployment running the old shared bind.
    """
    if not name or name in (".", "..") or os.sep in name or "/" in name:
        return False
    candidate = root / name
    try:
        return (
            candidate.parent == root
            and candidate.resolve() == root.resolve() / name
        )
    except OSError:
        return False


def _under(child: Path, parent: Path) -> bool:
    """``child`` is at or below ``parent``, compared on realpaths."""
    try:
        return _real(child).is_relative_to(_real(parent))
    except (OSError, ValueError):  # pragma: no cover - both are pure paths
        return False


def _translate(path: Path, src_root: Path, dst_root: Path) -> Path | None:
    """``path`` with ``src_root`` swapped for ``dst_root``, or None.

    None for a relative path, for one that is not under the namespace being
    moved, and for one whose relative part climbs back out with ``..`` —
    ``relative_to`` is lexical and would hand back an escape as though it were
    a child. A worktree somewhere else on disk is not this rename's to fix, and
    a guess would point a repository at a directory it does not own.

    Both spellings of the source root are tried, because the record was written
    by git and git resolves what it records.
    """
    if not path.is_absolute():
        return None
    for base in (src_root, _real(src_root)):
        try:
            relative = path.relative_to(base)
        except ValueError:
            continue
        if any(part == ".." for part in relative.parts):
            return None
        return dst_root / relative
    return None


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
    :func:`plan` establishes that the root is listable before it asks this, and
    this returns None on anything else so the two cannot disagree.
    """
    try:
        return (repos_dir / MARKER_NAME).read_text(errors="replace")
    except OSError:
        return None


def _read_progress(repos_dir: Path) -> list[str]:
    """Namespaces a previous run recorded as moved.

    Only the namespace list is read. The file sits in the model-writable root,
    so nothing about *ownership* is taken from it — a planted one naming
    another admin would be exactly the cross-user handoff this migration
    exists to prevent — and every name is re-checked against the tree before it
    is acted on. A malformed file is an empty list: the cost is a resumed run
    that repairs nothing, which is the state it was already in.
    """
    try:
        raw = (repos_dir / PROGRESS_NAME).read_text(errors="replace")
    except OSError:
        return []
    try:
        data = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    moved = data.get("moved")
    if not isinstance(moved, list):
        return []
    return [entry for entry in moved if isinstance(entry, str) and entry]


def _write_progress(repos_dir: Path, user_id: str, moved: Iterable[str]) -> None:
    """Record what has moved so far. Best-effort: never raises.

    A failure here costs the resume path and nothing else, and refusing to
    migrate because a breadcrumb could not be written would be the worse trade.
    """
    try:
        (repos_dir / PROGRESS_NAME).write_text(
            json.dumps({"user_id": user_id, "moved": list(moved)}) + "\n"
        )
    except OSError as exc:
        logger.warning("repos_relocate_progress_unwritable path=%s err=%s", repos_dir, exc)


def _clear_progress(repos_dir: Path) -> None:
    try:
        (repos_dir / PROGRESS_NAME).unlink()
    except OSError:
        pass


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


def _git_dirs(
    root: Path, max_depth: int = _MAX_SCAN_DEPTH
) -> tuple[list[Path], list[str]]:
    """Every git directory under ``root``, and what the walk could not see.

    Pruned at each repository found, and at each checkout — a directory whose
    ``.git`` is a *file* holds working-tree content, and descending into it
    would go looking for repositories inside whatever the model checked out.

    The notes matter as much as the list. This walk decides what gets repaired,
    so a subtree dropped for depth or for an unreadable directory is a clone
    whose worktrees nobody will fix, and it is reported rather than passed over.
    """
    found: list[Path] = []
    notes: list[str] = []
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        directory, depth = stack.pop()
        if _is_git_dir(directory):
            found.append(directory)
            continue
        dot_git = directory / ".git"
        try:
            if dot_git.is_file():
                continue
        except OSError as exc:
            notes.append(f"{directory}: could not be examined ({exc})")
            continue
        if _is_git_dir(dot_git):
            found.append(dot_git)
            continue
        if depth >= max_depth:
            notes.append(
                f"{directory}: below the {max_depth}-level scan depth; any "
                "repository under it was not examined and its worktrees were "
                "not repaired"
            )
            continue
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            notes.append(f"{directory}: could not be listed ({exc})")
            continue
        for entry in entries:
            try:
                if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                    continue
            except OSError as exc:
                notes.append(f"{entry.path}: could not be classified ({exc})")
                continue
            stack.append((Path(entry.path), depth + 1))
    return found, notes


def _recorded_worktrees(git_dir: Path) -> tuple[list[Path], list[str]]:
    """Worktree paths as the repository records them, and the malformed records.

    Each ``worktrees/<id>/gitdir`` holds the absolute path of that worktree's
    ``.git`` *file*, so the worktree is its parent. Read rather than inferred
    from sibling directory names, because one namespace can hold several
    projects and a worktree can have been cut anywhere.

    A record not ending in ``.git`` is malformed and reported, never taken as
    naming a directory directly: git writes that suffix on every entry, and
    accepting a record without it lets a written-by-hand file nominate the
    namespace directory itself as a worktree.
    """
    out: list[Path] = []
    notes: list[str] = []
    try:
        entries = sorted(os.scandir(git_dir / "worktrees"), key=lambda e: e.name)
    except OSError:
        return out, notes
    for entry in entries:
        try:
            raw = Path(entry.path, "gitdir").read_bytes()
        except OSError:
            continue
        text = raw.decode("utf-8", "surrogateescape").strip()
        if not text:
            continue
        recorded = Path(text)
        if recorded.name != ".git":
            notes.append(
                f"{entry.path}/gitdir: {text!r} does not name a worktree's .git "
                "file; ignored"
            )
            continue
        out.append(recorded.parent)
    return out, notes


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
        if name in (MARKER_NAME, PROGRESS_NAME):
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
            # at the root, where nothing binds it any more. A symlink named
            # after the admin is a different matter entirely — see the
            # destination check in `plan`, which refuses rather than notes.
            listing.notes.append(f"{name}: a symlink; left in place, not moved")
            continue
        if not is_dir:
            listing.notes.append(f"{name}: not a directory; left in place")
            continue
        if name.startswith("."):
            # `.package-caches` is the cc691d6f cache root. It is a cache
            # rather than a repository and its per-user directories are named
            # *from disk* — the one axis this layout must never trust, since a
            # task could create a directory named for a user who has never run
            # one. So it is reported and left rather than moved into somebody's
            # subtree. It is orphaned by the move (the caches derive per user
            # now) and safe for an operator to delete.
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


def _plan_repairs(
    namespace: str, src: Path, dst: Path, clone_root: Path
) -> tuple[list[Repair], list[str]]:
    """Repairs for one namespace's clones.

    ``clone_root`` is where the clones are *now* — ``src`` before the rename,
    ``dst`` when resuming a run that already renamed. The records inside them
    name paths under ``src`` either way, which is what makes one function serve
    both.
    """
    repairs: list[Repair] = []
    git_dirs, notes = _git_dirs(clone_root)
    for git_dir in git_dirs:
        recorded, malformed = _recorded_worktrees(git_dir)
        notes.extend(malformed)
        if not recorded:
            continue
        relative = git_dir.relative_to(clone_root)
        clone_src = src / relative
        clone_dst = dst / relative
        pairs: list[tuple[Path, Path]] = []
        stale: list[str] = []
        for old in recorded:
            new = _translate(old, src, dst)
            if new is None:
                stale.append(str(old))
            else:
                pairs.append((old, new))
        repairs.append(
            Repair(
                namespace=namespace,
                clone_src=clone_src,
                clone_dst=clone_dst,
                worktrees=tuple(pairs),
                stale=tuple(stale),
            )
        )
    return repairs, notes


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
    refusal. Ownership is asked only when there is something to place or to
    finish, so a fresh install with no admins file — a shape the single-user
    install ships — writes its marker instead of refusing forever.
    """
    root = Path(repos_dir)
    marker = root / MARKER_NAME

    listing = _read_root(root)
    if isinstance(listing, RelocateRefusal):
        return listing

    content = _marker_content(root)
    if content is not None:
        notes = list(listing.notes)
        if content.strip() != LAYOUT_VERSION:
            notes.append(
                f"{marker} holds {content.strip()!r} rather than "
                f"{LAYOUT_VERSION!r}; treated as migrated"
            )
        return RelocatePlan(
            repos_dir=root, marker=marker, user_id="",
            already_migrated=True, notes=tuple(notes),
        )

    recorded_moved = _read_progress(root)
    if not listing.namespaces and not recorded_moved:
        # A fresh install is already in the new layout. No owner is needed
        # because nothing is being placed.
        return RelocatePlan(
            repos_dir=root, marker=marker, user_id="", notes=tuple(listing.notes),
        )

    admin_list = sorted({a for a in admins if a})
    found = ", ".join(admin_list) if admin_list else "none"
    ambiguity = (
        f"namespaces found under {root}: "
        f"{', '.join(listing.namespaces) or 'none'}",
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

    # The destination is held to the containment rule the sources are, and a
    # failure is a refusal rather than a note. `mkdir(exist_ok=True)` succeeds
    # on a symlink to a directory, `chmod` follows it and `rename` traverses
    # it, so a link planted here — plantable from any task on the old shared
    # bind — moves every repository out of the tree and reports success.
    if dst_root.exists() and not _contained(root, user_id):
        return RelocateRefusal(
            REFUSE_DESTINATION,
            f"{dst_root} does not resolve to the subtree named by {user_id!r}.",
            (
                f"{dst_root} exists but is not a plain child directory of "
                f"{root} — a symlink, or something else that resolves "
                "elsewhere.",
                "Every namespace would be renamed through it, out of the tree "
                "the sandbox binds, and the run would report success.",
                "Remove or replace it with a real directory and re-run.",
            ),
        )

    notes = list(listing.notes)

    moves: list[Move] = []
    collisions: list[str] = []
    for name in listing.namespaces:
        if name == user_id:
            # The destination itself: either the subtree `setup_env` created,
            # or a half-migrated tree from a run that died, or a forge
            # namespace that happens to be named after its owner. All three are
            # already inside the right user's root.
            notes.append(f"{name}: already the destination directory; left in place")
            continue
        src = root / name
        dst = dst_root / name
        if dst.exists():
            collisions.append(f"{dst} already exists")
            continue
        repairs, repair_notes = _plan_repairs(name, src, dst, clone_root=src)
        notes.extend(repair_notes)
        moves.append(Move(namespace=name, src=src, dst=dst, repairs=tuple(repairs)))

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

    # Anything a previous run moved and did not finish repairing. Its clones
    # are already at the destination; the records inside them still name the
    # source, which is what makes the translation the same one a fresh move
    # would use.
    resume_repairs: list[Repair] = []
    resumed: list[str] = []
    for name in recorded_moved:
        if not _contained(root, user_id) or not _contained(dst_root, name):
            notes.append(
                f"{name}: recorded as moved by an earlier run, but "
                f"{dst_root / name} is not a plain child directory; not repaired"
            )
            continue
        moved_dst = dst_root / name
        if not moved_dst.is_dir():
            notes.append(
                f"{name}: recorded as moved by an earlier run but not present "
                f"at {moved_dst}; nothing to repair"
            )
            continue
        repairs, repair_notes = _plan_repairs(
            name, root / name, moved_dst, clone_root=moved_dst,
        )
        notes.extend(repair_notes)
        if repairs:
            resume_repairs.extend(repairs)
            resumed.append(name)

    return RelocatePlan(
        repos_dir=root,
        marker=marker,
        user_id=user_id,
        moves=tuple(moves),
        resume_repairs=tuple(resume_repairs),
        resumed=tuple(resumed),
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

    Used twice, and the second use is what makes the repair safe rather than
    merely verified. Afterwards it asks whether the repair worked. *Before*,
    against the clone's old path, it is the precondition on the repair itself:
    ``git worktree repair <path>`` decides which repository to write from
    ``<path>/.git`` and ignores the ``-C`` argument entirely, so a record
    inside the moving clone can otherwise nominate any directory it likes and
    redirect an unrelated repository's worktree into it.

    A file read rather than another subprocess, and compared on realpaths
    because git canonicalises what it writes here.
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
        return _under(Path(line[len(prefix):].strip()), git_dir / "worktrees")
    return False


def _run_repair(repair: Repair) -> tuple[list[str], list[str], list[str]]:
    """Repair one clone's worktrees. Returns ``(repaired, unrepaired, notes)``."""
    repaired: list[str] = []
    unrepaired: list[str] = []
    notes: list[str] = []

    for record in repair.stale:
        notes.append(
            f"{repair.clone_dst}: worktree record {record} is outside the moved "
            "namespace; not repaired"
        )

    targets: list[Path] = []
    for _old, new in repair.worktrees:
        if not new.exists():
            # A checkout removed by hand leaves its record behind until someone
            # runs `git worktree prune`. Ordinary git litter, not a migration
            # failure — there is no worktree left to break.
            notes.append(f"{new}: recorded worktree is not on disk; nothing to repair")
            continue
        # The precondition, not a nicety: see `_links_back`. Either spelling of
        # the clone counts, so a worktree a previous run already repaired is
        # still recognised as this clone's.
        if not (
            _links_back(new, repair.clone_src) or _links_back(new, repair.clone_dst)
        ):
            unrepaired.append(str(new))
            notes.append(
                f"{new}: its .git does not name this clone's administrative "
                f"directory ({repair.clone_src}); not handed to git worktree "
                "repair, which would write to whatever it does name"
            )
            continue
        targets.append(new)

    if not targets:
        return repaired, unrepaired, notes

    status, output = _git(
        repair.clone_dst, "worktree", "repair", *[str(t) for t in targets]
    )
    if status != 0:
        tail = output.strip().splitlines()
        notes.append(
            f"{repair.clone_dst}: git worktree repair exited {status}: "
            f"{tail[-1] if tail else ''}"
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
    return repaired, unrepaired, notes


def _prepare_destination(plan: RelocatePlan) -> str | None:
    """Create ``{repos_dir}/{user_id}`` at 0700. Returns an error string or None.

    Re-checks the containment `plan` refused on, because the two are separated
    by however long a dry run's operator took to read it, and the entry is
    plantable. The mode is then set through an ``O_NOFOLLOW`` descriptor rather
    than by name: ``os.chmod`` follows a final symlink, and
    ``follow_symlinks=False`` is unsupported for ``chmod`` on Linux, so the fd
    is both the refusal and the handle. Same idiom as the cache sweeper's
    identity pin.
    """
    dst_root = plan.repos_dir / plan.user_id
    if dst_root.exists() and not _contained(plan.repos_dir, plan.user_id):
        return (
            f"{dst_root}: does not resolve to the subtree named by "
            f"{plan.user_id!r}; nothing was moved"
        )
    try:
        dst_root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return f"{dst_root}: could not be created ({exc})"
    try:
        fd = os.open(dst_root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as exc:
        return f"{dst_root}: could not be opened without following a symlink ({exc})"
    try:
        os.fchmod(fd, 0o700)
    except OSError as exc:
        # Reported and not fatal, the posture `_user_repos_dir` settled on: a
        # directory another uid owns takes the chmod's EPERM while the move
        # itself is still the right thing to do.
        logger.warning("repos_relocate_chmod_failed path=%s err=%s", dst_root, exc)
    finally:
        os.close(fd)
    return None


def apply(plan: RelocatePlan) -> RelocateReport:
    """Carry out a plan. Never raises.

    One namespace at a time, **and its repairs immediately after its own
    rename** rather than after all of them: a process death then costs the
    worktrees of one namespace instead of every namespace in the tree. What
    moved is recorded in the progress file as it moves, so the run that follows
    a death knows what the stale records still name.

    A namespace that cannot be moved is recorded and the rest continue —
    stopping halfway leaves the same shape as finishing halfway, minus the
    repairs. The marker is written only when every move landed, so a partial
    run is retried rather than declared done; an unrepaired worktree does not
    hold it back, because the layout did move and a re-run has no rename left
    to perform. That is what the progress file is for instead.
    """
    notes = list(plan.notes)
    if plan.already_migrated:
        return RelocateReport(notes=tuple(notes) or ("already migrated",))

    moved: list[str] = []
    failed: list[str] = []
    repaired: list[str] = []
    unrepaired: list[str] = []

    if plan.moves or plan.resume_repairs:
        error = _prepare_destination(plan)
        if error is not None:
            return RelocateReport(failed=(error,), notes=tuple(notes))

    for repair in plan.resume_repairs:
        got, missed, said = _run_repair(repair)
        repaired.extend(got)
        unrepaired.extend(missed)
        notes.extend(said)

    done = list(plan.resumed)
    if plan.moves:
        _write_progress(plan.repos_dir, plan.user_id, done)

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
        done.append(move.namespace)
        _write_progress(plan.repos_dir, plan.user_id, done)
        logger.info("repos_relocated namespace=%s -> %s", move.namespace, move.dst)
        for repair in move.repairs:
            got, missed, said = _run_repair(repair)
            repaired.extend(got)
            unrepaired.extend(missed)
            notes.extend(said)

    marker_written = False
    if not failed:
        try:
            plan.repos_dir.mkdir(parents=True, exist_ok=True)
            plan.marker.write_text(f"{LAYOUT_VERSION}\n")
            marker_written = True
            _clear_progress(plan.repos_dir)
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
    # The machine-readable half of the same refusal, because one caller has to
    # tell them apart and prose is not a contract. Ansible reads this line:
    # `live_tasks` is the one refusal that resolves on its own — the tasks
    # finish — so the play reports it and carries on, while every other reason
    # names something only a person can change and stops the play. Printed last
    # and on a line of its own so the wording above it stays free to change.
    print(f"refusal: {refusal.reason}", file=sys.stderr)


def _print_plan(plan: RelocatePlan) -> None:
    if plan.already_migrated:
        print(f"repos_relocate: already migrated ({plan.marker} is present)")
    elif not plan.moves and not plan.resume_repairs:
        print(f"repos_relocate: nothing to migrate under {plan.repos_dir}")
    else:
        if plan.moves:
            print(f"repos_relocate: {len(plan.moves)} namespace(s) -> {plan.user_id}")
        for move in plan.moves:
            print(f"  {move.src} -> {move.dst}")
            for _old, new in [wt for r in move.repairs for wt in r.worktrees]:
                print(f"  repair {new}")
        if plan.resume_repairs:
            print(
                f"repos_relocate: finishing {len(plan.resumed)} namespace(s) an "
                "earlier run moved but did not repair"
            )
            for _old, new in [wt for r in plan.resume_repairs for wt in r.worktrees]:
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
    unfinished = _read_progress(repos_dir)
    if unfinished:
        print(f"unfinished from an earlier run: {', '.join(unfinished)}")
    listing = _read_root(repos_dir)
    if isinstance(listing, RelocateRefusal):
        print(f"  unreadable: {listing.message}")
        return
    for name in listing.namespaces:
        print(f"  {name}/")
        git_dirs, walk_notes = _git_dirs(repos_dir / name)
        for git_dir in git_dirs:
            worktrees, malformed = _recorded_worktrees(git_dir)
            suffix = f" ({len(worktrees)} worktree(s))" if worktrees else ""
            print(f"    {git_dir}{suffix}")
            for worktree in worktrees:
                print(f"      {worktree}")
            for note in malformed:
                print(f"      note: {note}")
        for note in walk_notes:
            print(f"    note: {note}")
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

    # Paths here are decoded with `surrogateescape` (a repository may hold one
    # non-UTF-8 byte), and stdout is opened strict — so printing a report would
    # raise `UnicodeEncodeError` *after* the moves and the marker, turning a
    # completed migration into a traceback. stderr already uses
    # `backslashreplace`; this gives stdout the same.
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):  # pragma: no cover - depends on the stream
            pass

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
        # Only where something would actually move: a no-op cannot race a task,
        # and neither can a repair, which rewrites two pointer files inside a
        # repository the task table says nobody is using.
        refusal = _live_task_refusal(config)
        if refusal is not None:
            _print_refusal(refusal)
            return EXIT_REFUSED

    if args.dry_run:
        _print_plan(outcome)
        return EXIT_OK

    report = apply(outcome)
    _print_report(report)
    return EXIT_OK if report.ok else EXIT_PARTIAL


if __name__ == "__main__":
    raise SystemExit(main())
