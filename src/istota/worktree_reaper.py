"""Reap developer worktrees whose work has already landed upstream (ISSUE-288).

The developer skill cuts a worktree from a bare clone for every coding task and
nothing removed one afterwards. Leaving a worktree in place after an MR opens is
right — it is what a follow-up commit and a human inspection both need — but
"leave it for now" was never followed by anything, and on a repos directory full
of gigabyte checkouts that is a slow leak with no owner. The entry that prompted
this found `istota--main`, 1.3 GB, detached at a commit already on
``origin/HEAD``, sitting untouched.

:func:`reap_and_report` runs from the **scheduler**, on its own interval, not
from a task's setup path. That is deliberate and was the review's finding: the
developer skill's ``setup_env`` hook is dispatched for every skill in the index
regardless of what the task selected (``skills/_env.py``,
``dispatch_setup_env_hooks``), so a sweep there ran before every Talk reply,
every cron job and every heartbeat tick — and the heartbeat builds a task with
``id=0``, so it ran with no notion of whose worktree was whose. A delete path
belongs on a stated cadence, with nothing racing it.

**A worktree is removed only when losing it cannot lose anything.** Six things
are true of every one that goes:

- it is inside the ``repos_dir`` the sweep was handed, and is not a
  repository's own main worktree;
- git holds no lock on it;
- nothing has touched it, or any entry in its administrative directory, inside
  the retention window — every entry there rather than the directory's own
  mtime, which any ``.lock`` create-and-remove stamps for the whole window
  without a byte of work having happened, and rather than a list of names,
  which misses the ``FETCH_HEAD`` of the fetch the developer skill runs at the
  start of every task (ISSUE-316);
- ``git status --porcelain -z --untracked-files=all --ignored=matching`` reports
  nothing but reconstructible build directories, so there is no uncommitted
  edit, no scratch file and no gitignored ``.env`` — and that check is repeated
  immediately before the removal, because it is the *only* thing protecting an
  ignored file (``git worktree remove`` without ``--force`` deletes a ``.env``
  or a ``node_modules`` without complaint; its refusal covers tracked
  modifications only). A ``node_modules`` or a ``.venv`` is discounted by name
  (ISSUE-304): those are the largest thing a worktree holds, they hold nothing
  that is not derivable from committed files, and counting them pinned every
  worktree that had ever run an install;
- either its head is reachable from ``refs/remotes/origin/HEAD``, or it is not
  a merge commit — which ``git cherry`` does not examine — and
  ``git cherry refs/remotes/origin/HEAD <head>`` reports no commit unique to
  it.

That last one is the merged test. Two questions in sequence, and the order is
load-bearing. ``merge-base --is-ancestor`` runs first because a head reachable
from upstream needs no patch-id reasoning at all: the commit, its delta and any
conflict resolution it carries are already upstream. Asking the merge-head
refusal first held every worktree cut from a merge tip and never committed to —
the developer skill cuts from ``origin/HEAD``, so that is the state of every
abandoned probe on a branch that lands MRs as merges, and it is the exact set
with nothing in it to lose (ISSUE-316).

The ancestor test does not replace ``git cherry``, and adding it takes nothing
away from why ``git cherry`` is here. A squash or rebase merge is the ordinary
way an MR lands and leaves the branch an ancestor of nothing, while every commit
on it has a patch-id equivalent upstream — so the ancestor test alone would fire
on almost nothing that did real work, and ``git cherry`` is what answers the
question actually being asked there: is there a commit here that exists nowhere
else. An unrelated history, a rebase that dropped a commit from the head, a
branch that never merged: all still report a ``+`` line and are held back.

What ``git cherry`` cannot see is a merge commit's own conflict resolution — it
reports the merged-in commits and never the merge's delta — so a merge head is
refused rather than trusted, in the case where that resolution could exist
nowhere else. Once the merge itself is reachable from upstream there is nothing
left for it to hide, which is why the ancestor test is allowed to answer first
and why the refusal keeps every case it was written for.

**Reading the listing.** ``--porcelain -z``, never plain ``--porcelain``. Git
does not quote a newline in a worktree path in the line-oriented form: a path
containing one truncates its own record and forges a second naming any path and
any head the writer chose, which is a route to removing a *different*, unmerged
worktree. Paths under ``repos_dir`` are model-chosen, so this is reachable. The
NUL form contains it, and a listing with a duplicate path is refused entirely.

**Repository config is hostile input.** ``repos_dir`` is bound read-write into
the sandbox, so a checkout's own ``.git/config`` is model-written. Plain
``git status`` there runs whatever ``core.fsmonitor`` names, as the daemon user,
with the daemon's environment — so every call goes through
:data:`istota.git_hardening.GIT_HARDENING`. ``GIT_CONFIG_NOSYSTEM`` and
``GIT_CONFIG_GLOBAL`` do not cover repository config and are not a substitute.

``GIT_OPTIONAL_LOCKS=0`` is load-bearing rather than tidiness: without it
``git status`` rewrites the worktree's index, which is one of the mtimes the
retention window reads, so every sweep would reset the idle clock of every
worktree it looked at and nothing would ever be reaped after the first pass.

Where the answer cannot be established the worktree stays. A bare clone with no
resolvable ``refs/remotes/origin/HEAD`` has no upstream to compare against, so
its worktrees are all held rather than compared against a guess — this is a
delete path, and refusing is the only safe failure.

Held-back worktrees are reported with a reason rather than passed over in
silence. The accumulation the entry describes is a problem precisely because
nobody could see it; a sweep that removes two and says nothing about the eleven
it kept reproduces that.

stdlib-only leaf. The root is a parameter, no function here raises, and nothing
reads configuration or environment of its own.
"""

from __future__ import annotations

import logging
import os
import subprocess
import time
from pathlib import Path
from typing import NamedTuple

from istota.git_hardening import GIT_HARDENING
from istota.git_remote_scrub import find_git_dirs

logger = logging.getLogger("istota.worktree_reaper")

_GIT_TIMEOUT = 60

# `git fetch` reaches the network, so it gets its own, longer budget. Still
# bounded: a hung forge must not hold the sweep open indefinitely.
_FETCH_TIMEOUT = 120

# How long a worktree must have been idle before it is a candidate. This is the
# guard that protects a task *running right now*: the sweep is periodic and
# knows nothing about the worker pool, so the only evidence available that a
# checkout is in use is that something recently touched it. A day is well past
# any single task's lifetime.
DEFAULT_RETENTION_HOURS = 24.0

# The floor the window is clamped to. Below roughly an hour the guard stops
# describing "idle" and starts describing "was created a moment ago" — and a
# freshly cut worktree is by construction clean, unlocked and carrying no
# commit that is not upstream, which is precisely the reapable state. A window
# of 0 would therefore delete the checkout of a task that is still setting it
# up. The knob stays useful; it just cannot be set to something that means
# "delete live work".
MIN_RETENTION_HOURS = 1.0

# Ignored directories a tool rebuilds from files that are already committed.
# Discounted by :func:`_is_dirty`, so a worktree that ran an install is still
# reapable once its work has landed (ISSUE-304).
#
# **An explicit list of names, never "ignore every ``!!`` line".** The whole
# reason the ``--ignored`` half of the check exists is the gitignored ``.env``,
# and a rule that discounted ignored entries as a class would delete it. This
# repository's own gitignore hides ``data/`` and ``lib/``, and the first of
# those is where a module keeps its real rows.
#
# ``dist``, ``build``, ``target`` and ``out`` are deliberately absent. Each is
# a reconstructible artifact directory in some projects and an ordinary source
# directory in others, and the two are indistinguishable from here. The cost of
# being wrong is asymmetric: a name missing from this list keeps a worktree
# nobody needed, a name wrongly on it deletes work that exists nowhere else.
#
# The accepted risk, stated plainly rather than left in the word
# "reconstructible": an *in-place edit* inside one of these directories is lost
# — a patched package under ``node_modules`` is a real debugging workflow, and
# nothing here can tell it from an unmodified install. So is a hand-authored
# directory that happens to carry one of these names and is gitignored;
# ``venv`` is the one where that is imaginable, and it stays because
# ``python -m venv venv`` is what actually produces it.
_RECONSTRUCTIBLE_DIRS = frozenset({
    "node_modules",     # npm, pnpm, yarn
    ".venv", "venv",    # uv, virtualenv
    "__pycache__",      # CPython
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".svelte-kit",      # regenerated by `svelte-kit sync`, and by dev and build
})

# Directories excluded from the activity walk, for cost: a single
# `web/node_modules` is ~18k files and a worktree with both a node and a python
# tree runs to six figures. `.git` is here because in a linked worktree it is a
# file, so this prunes an *embedded* repository's object store.
#
# **Subtracting :data:`_RECONSTRUCTIBLE_DIRS` is load-bearing, not tidiness.**
# The two lists answer different questions, and a name on both is invisible to
# *both* guards: `os.walk` prunes by name before anything stats the directory,
# so nothing under it is ever read, and the dirty check now discounts it by
# design. An `npm install`, a `uv sync` into an existing `.venv`, or a long
# pytest run writing only `__pycache__` touches nothing else in the checkout —
# so the worktree reads as idle *and* clean, and gets deleted with the install
# still running. Before ISSUE-304 the dirty check held it, which is what made
# skipping it free; that premise is exactly what this change removed. Whatever
# `_is_dirty` stops counting, the walk has to start counting.
#
# What is left is the names the dirty check still treats as dirty, where
# skipping remains free for the original reason.
_WALK_SKIP = frozenset({
    ".git", "node_modules", ".venv", "venv", "target", ".tox", ".mypy_cache",
    ".pytest_cache", "__pycache__", ".next", "dist", "build", ".gradle",
}) - _RECONSTRUCTIBLE_DIRS

REASON_MERGED = "merged"
REASON_PROTECTED = "protected"
REASON_LOCKED = "locked"
REASON_RECENT = "recent"
REASON_DIRTY = "dirty"
REASON_UNMERGED = "unmerged"
REASON_MERGE_HEAD = "merge_head"
REASON_NO_UPSTREAM = "no_upstream"
REASON_OUTSIDE = "outside_root"
REASON_MAIN = "main_worktree"
REASON_FAILED = "failed"

# Reasons that describe "not a candidate" rather than "kept something".
# Excluded from the held-back summary so a count of eleven means eleven real
# checkouts, not eleven bare clones.
_NOT_CANDIDATE = frozenset({REASON_MAIN, REASON_OUTSIDE})


class WorktreeRecord(NamedTuple):
    """One entry of ``git worktree list --porcelain -z``."""

    path: Path
    head: str      # 40-hex sha, or "" when the listing carried none
    branch: str    # short branch name, or "" when detached
    bare: bool
    locked: bool
    prunable: bool


class ReapOutcome(NamedTuple):
    """What happened to one worktree, and why.

    A held-back worktree is reported too: the whole point of the entry was that
    accumulation nobody could see, and a sweep that only reports its deletions
    is the same blind spot with a timer attached.
    """

    path: Path
    branch: str
    removed: bool
    reason: str


# --------------------------------------------------------------------------
# git
# --------------------------------------------------------------------------

def _git(cwd: Path, *args: str, timeout: int = _GIT_TIMEOUT) -> tuple[int, str]:
    """``(exit_status, stdout)``. Bytes are decoded, never rejected.

    ``GIT_HARDENING`` first, before ``-C``: the repository this runs against is
    model-writable, and a plain ``git`` there executes ``core.fsmonitor``,
    ``diff.external`` or a ``gpg.*`` program of the writer's choosing, as the
    daemon user, inheriting the daemon's environment. The two ``GIT_CONFIG_*``
    variables below cover the system and user config and do nothing about the
    repository's own, which is the writable one.

    ``GIT_OPTIONAL_LOCKS=0`` keeps ``git status`` from rewriting the worktree's
    index. That file is one of the mtimes :func:`_last_activity` reads, so
    without this every sweep would reset the idle clock of every worktree it
    examined and nothing would ever be reaped after the first pass.

    ``text=True`` would raise ``UnicodeDecodeError`` — a ``ValueError``, caught
    by neither ``OSError`` nor ``SubprocessError`` — on a repository holding a
    path or a commit message with one non-UTF-8 byte, and would abort the sweep
    from inside a helper every caller treats as total.
    """
    try:
        proc = subprocess.run(
            ["git", *GIT_HARDENING, "-C", str(cwd), *args],
            capture_output=True, timeout=timeout,
            env={
                **os.environ,
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_OPTIONAL_LOCKS": "0",
            },
        )
    except (OSError, subprocess.SubprocessError):
        return 1, ""
    return proc.returncode, proc.stdout.decode("utf-8", "surrogateescape")


def parse_worktree_list(data: str) -> list[WorktreeRecord] | None:
    """Records from ``git worktree list --porcelain -z``.

    ``None`` — refuse the whole listing — when two records name the same path.
    That is not a state git produces, so it means something constructed it, and
    the damage it buys is a record whose *path* is one worktree and whose
    *head* is another: the age and dirty checks would run against the victim
    while the merged check ran against a head chosen to pass.

    NUL-separated, never line-separated. Git does not quote a newline in a
    worktree path in the line-oriented form, so a path containing one truncates
    its record and forges the next — and paths under ``repos_dir`` are chosen
    by the model. Attributes are NUL-terminated and records are separated by an
    empty attribute (a second NUL). Unrecognised attributes are ignored, so
    this keeps working against a git older or newer than the one it was
    written for.
    """
    records: list[WorktreeRecord] = []
    path: Path | None = None
    head = branch = ""
    bare = locked = prunable = False

    def flush() -> None:
        nonlocal path, head, branch, bare, locked, prunable
        if path is not None:
            records.append(WorktreeRecord(path, head, branch, bare, locked, prunable))
        path, head, branch = None, "", ""
        bare, locked, prunable = False, False, False

    for field in data.split("\0"):
        if not field:
            continue
        if field.startswith("worktree "):
            flush()
            path = Path(field[len("worktree "):])
        elif path is None:
            continue
        elif field.startswith("HEAD "):
            head = field[len("HEAD "):].strip()
        elif field.startswith("branch "):
            branch = field[len("branch "):].strip()
            if branch.startswith("refs/heads/"):
                branch = branch[len("refs/heads/"):]
        elif field == "bare":
            bare = True
        elif field == "locked" or field.startswith("locked "):
            locked = True
        elif field == "prunable" or field.startswith("prunable "):
            prunable = True
    flush()

    seen = {record.path for record in records}
    if len(seen) != len(records):
        logger.warning(
            "worktree_reaper: refusing a listing with a duplicate worktree path; "
            "nothing swept in this repository.",
        )
        return None
    return records


def _upstream_head(bare: Path) -> str:
    """The sha ``refs/remotes/origin/HEAD`` resolves to, or ``""``.

    Fetches first, best-effort. Without any fetch the comparison runs against
    whatever the last task left behind: a branch merged upstream an hour ago is
    still ahead of a stale ``origin/HEAD`` and would be held.

    **The fetch usually fails on a private repository, by design.** The git
    credential helper is registered per task, through the ``GIT_CONFIG_KEY_*``
    variables :mod:`istota.skills.developer` returns from ``setup_env``; the
    scheduler process has none of them, and giving it a credential of its own
    is a separate decision about where forge tokens live, not something to
    settle inside a cleanup sweep. ``GIT_TERMINAL_PROMPT=0`` means it fails
    fast rather than hanging on a prompt.

    That is safe in the only direction that matters: a stale ``origin/HEAD``
    can only hold more back, never reap more. What it costs is timeliness — on
    a private repo, freshness comes from the developer skill's own
    ``git fetch origin`` at the start of the next task on that repository, so a
    merged worktree is reaped by the first sweep after that task rather than by
    the first sweep after the merge. A repository nobody works on again keeps
    its merged worktrees; that residue is the one part of the accumulation this
    module does not clear.

    ``rev-parse --verify``, not ``symbolic-ref``: a dangling origin/HEAD
    survives the upstream default branch being renamed and reads as present to
    anything that does not resolve it. The developer skill's setup repairs that
    state (ISSUE-269); this module only has to notice it.
    """
    code, _ = _git(bare, "fetch", "--quiet", "--prune", "origin", timeout=_FETCH_TIMEOUT)
    if code != 0:
        logger.info(
            "worktree_reaper: could not fetch %s (expected on a private repo — "
            "the git credential helper is registered per task, not for the "
            "scheduler). Comparing against the local origin/HEAD, which can only "
            "hold more back; the next developer task on this repository will "
            "refresh it.", bare,
        )
    code, out = _git(
        bare, "rev-parse", "--verify", "--quiet", "refs/remotes/origin/HEAD^{commit}",
    )
    return out.strip() if code == 0 else ""


def _is_ancestor(bare: Path, head: str, upstream: str) -> bool:
    """Whether ``head`` is reachable from ``upstream``. False when unanswerable.

    The cheapest containment answer there is, and the only one that needs no
    patch-id reasoning at all: if the head commit is reachable from upstream
    then the commit itself, its delta and any conflict resolution it carries
    are already there, and losing the worktree cannot lose them. That is what
    lets this run *before* the merge-head refusal — the hazard that refusal
    exists for is content living only in a merge that has **not** landed
    (ISSUE-316).

    It does not replace :func:`_has_unique_commits`. A squash or rebase merge
    is the ordinary way an MR lands and leaves the branch an ancestor of
    nothing, so this answers False on most branches that really did land.

    ``--is-ancestor`` exits 0 for contained and 1 for not; anything else is an
    error — an unresolvable sha, a missing object, git not running at all —
    and reads as "not contained" so the remaining checks decide. Returning
    True there would turn a broken repository into a licence to delete.
    """
    if not head or not upstream:
        return False
    code, _ = _git(bare, "merge-base", "--is-ancestor", head, upstream)
    return code == 0


def _is_merge_commit(bare: Path, head: str) -> bool | None:
    """Whether ``head`` has more than one parent. ``None`` when git could not say.

    ``git cherry`` reports the commits a merge brought in and never the merge's
    own delta, so anything that exists only in a conflict resolution is
    invisible to the merged test. Refusing a merge head outright is cheaper
    than reasoning about when that matters.
    """
    code, out = _git(bare, "rev-list", "--parents", "-n", "1", head)
    if code != 0:
        return None
    # `<sha> <parent> [<parent> ...]` — more than two fields is a merge.
    return len(out.split()) > 2


def _has_unique_commits(bare: Path, upstream: str, head: str) -> bool | None:
    """Whether ``head`` carries a commit that exists nowhere upstream.

    ``None`` when git could not answer, which the caller treats as "held back"
    — an unanswerable question is not a licence to delete.

    ``git cherry`` prints ``+ <sha>`` for a commit with no patch-id equivalent
    in ``upstream`` and ``- <sha>`` for one applied there under another sha.
    Only the ``+`` lines matter: a rebased or cherry-picked branch is all
    ``-``, and an ancestor produces no output at all.
    """
    if not head or set(head) == {"0"}:
        # No head, or the all-zero sha an unborn branch reports. Nothing to
        # compare, so nothing that can be established — hold.
        return None
    code, out = _git(bare, "cherry", upstream, head)
    if code != 0:
        return None
    return any(line.startswith("+") for line in out.splitlines())


def _is_reconstructible(entry: str) -> bool:
    """Whether a ``!!`` path is a directory this module agrees to discard.

    The trailing slash is required, and carries the actual meaning: git writes
    it only when a *directory* was collapsed, which happens only where the whole
    subtree is ignored. A file called ``node_modules`` is not a package tree and
    is not covered.

    **Any** component may match, not just the first or the last, and both ends
    of that matter. Not the first: this repository keeps its frontend in
    ``web/``, so the install lands at ``web/node_modules`` and a check anchored
    at the worktree root would miss every nested one. Not only the last either:
    the pattern shape decides where git collapses, and ``node_modules/**`` or
    ``node_modules/*`` — both common — never match the directory itself, so git
    reports the matched *children* (``!! node_modules/pkg/``) and a last-
    component test sees ``pkg``. That reads as dirty, which is the safe
    direction, but it leaves the worktree pinned forever, which is the entire
    bug ISSUE-304 is about.

    Widening to any component cannot reach anything the narrow form protected.
    A path is only ever discounted if some ancestor on it is a directory this
    module already agrees to discard whole, and the entry is a subtree of that
    ancestor by construction. ``data/`` still holds (no component matches), and
    ``data/node_modules/`` is discounted correctly — git collapsed at
    ``data/node_modules``, so ``data/`` itself is not ignored and only the
    package tree inside it is in question.
    """
    if not entry.endswith("/"):
        return False
    return any(
        part in _RECONSTRUCTIBLE_DIRS
        for part in entry.rstrip("/").split("/")
    )


def _is_dirty(worktree: Path) -> bool | None:
    """Whether the checkout holds anything not committed. ``None`` on failure.

    ``--untracked-files=all`` and ``--ignored`` both, because the files most
    worth not deleting are the ones git is not tracking: a scratch script, a
    captured log, a ``.env``. Untracked work is the commonest thing a worktree
    holds that exists nowhere else, and for ignored files this check is the
    *only* protection — ``worktree remove`` without ``--force`` removes them
    without a word.

    Ignored entries naming a :data:`_RECONSTRUCTIBLE_DIRS` directory are the
    one exception (ISSUE-304). They are the largest thing a worktree ever holds
    and they contain nothing that is not derivable from files already
    committed, so counting them as dirty pinned every worktree that had ever
    run an install — permanently, since nothing later cleans them up. The
    production host had one such worktree at 941 MB, 882 MB of it ``.venv``,
    whose entire ignored listing was ``.venv/``, ``.pytest_cache/``,
    ``.ruff_cache/`` and thirty-odd ``__pycache__/`` directories.

    ``--ignored=matching`` rather than the default ``traditional``, which is
    what makes the classification possible at all: with ``--untracked-files=all``
    in force, ``traditional`` expands an ignored directory into one line per
    file inside it, and the production worktree above reported 31,074 of them
    where ``matching`` reports 69. ``matching`` collapses a directory only when
    the directory *itself* matched a pattern — a ``logs/`` holding files hidden
    by ``*.log`` is still listed file by file — so nothing an ignore rule
    covers can disappear behind a directory line that this function discounts.

    ``-z`` for the same reason the worktree listing uses it: the line-oriented
    forms quote unusual paths, and a path here is model-written. NUL-separated
    records cannot be forged by a newline in a filename.
    """
    code, out = _git(
        worktree, "status", "--porcelain", "-z",
        "--untracked-files=all", "--ignored=matching",
    )
    if code != 0:
        return None
    for record in out.split("\0"):
        # Emptiness, not ``strip()``. The only empty record is the tail after
        # the trailing NUL, and that is precisely what this skips. Stripping
        # would happen to work — every record opens with two non-whitespace
        # status characters, so a record for a file named ``" "`` still leaves
        # ``"??"`` behind — but that makes the parse depend on the alphabet of
        # the status codes rather than on the framing, which is the kind of
        # thing that is true until someone adds a code.
        if not record:
            continue
        # Anything that is not an ignored entry — a tracked modification, an
        # untracked file, the second path of a rename — settles it at once.
        if not record.startswith("!! "):
            return True
        if not _is_reconstructible(record[3:]):
            return True
    return False


# --------------------------------------------------------------------------
# Activity
# --------------------------------------------------------------------------

def _admin_dir(worktree: Path) -> Path | None:
    """The worktree's administrative directory under the bare clone.

    A linked worktree's ``.git`` is a file holding ``gitdir: <path>``. That
    directory is where git writes ``index``, ``HEAD`` and ``logs/HEAD``, so it
    carries the timestamp of the last git operation — which the checkout itself
    does not: a ``git commit`` touches no file in the working tree at all.

    The pointer may be **relative** (``git worktree add --relative-paths``, or
    ``worktree.useRelativePaths`` in a config the model can write), in which
    case it is relative to the worktree. Resolving it against the daemon's cwd
    instead finds nothing, and the failure is silent — it would drop the git
    half of the activity signal and leave a worktree whose task has been
    committing all day looking idle.
    """
    dot_git = worktree / ".git"
    try:
        if not dot_git.is_file():
            return None
        raw = dot_git.read_text("utf-8", "surrogateescape")
    except OSError:
        return None
    if not raw.startswith("gitdir:"):
        return None
    candidate = Path(raw.split(":", 1)[1].strip())
    if not candidate.is_absolute():
        candidate = worktree / candidate
    try:
        return candidate if candidate.is_dir() else None
    except OSError:
        return None


def _touched_since(worktree: Path, cutoff: float) -> bool:
    """Whether anything in or behind ``worktree`` has an mtime after ``cutoff``.

    A predicate, not a maximum, so it can stop at the first file that settles
    it. That matters: the common case on a busy repos directory is a live
    worktree, and the answer is usually available from the administrative
    directory alone. Computing a true maximum meant walking every file of every
    candidate — a checkout with a `node_modules` and a `.venv` is six figures —
    before the two cheap git checks even ran.

    The administrative directory goes first because it is the half that answers
    soonest for work in progress: a task that only runs git touches nothing in
    the working tree, and a task that only edits files touches nothing in the
    admin directory, so both halves are needed.

    There it reads every **entry**, and deliberately not the directory's own
    mtime (ISSUE-316). A directory is stamped whenever an entry is created or
    removed in it, and a `.lock` does both even for an operation that writes
    nothing — so the stamp outlives the lock by the whole retention window and
    a worktree got a fresh day of exemption on evidence that no work had
    happened. Measured on the live clone: an administrative directory 5h26m
    newer than the newest file in it, every one of which was from the previous
    day.

    An entry's own mtime does not have that problem, in either direction. A
    lock that has been created and removed leaves nothing behind to find, and
    one that is still there carries the time it was taken — which is a git
    process running *now*, and exactly the thing the window exists to protect.

    Every entry rather than a list of names, and that is not thoroughness for
    its own sake. `git fetch` in a linked worktree writes `FETCH_HEAD` and
    touches neither `index`, nor `HEAD`, nor the reflog — and the developer
    skill fetches at the start of every task, so a list of the names anyone
    thought to write down read a live checkout as idle and deleted it while
    its task was still reading code. The same gap covered `rebase-merge/`,
    `sequencer/`, `BISECT_LOG`, `MERGE_MSG` and `config.worktree`. Reading the
    directory costs one `readdir` over a handful of entries and needs no
    revision the next time git writes somewhere new.

    `logs/HEAD` is stated on top of that because it sits one level down and is
    rewritten in place, which does not restamp `logs/`.

    Unreadable is treated as touched: a missing file is an answer (a worktree
    that has never had an `ORIG_HEAD` simply has none), while a permission or
    I/O error is a question that could not be answered, and on a delete path
    that holds the worktree.
    """

    def newer(path: Path) -> bool:
        try:
            return path.stat().st_mtime > cutoff
        except FileNotFoundError:
            return False
        except OSError:
            return True

    admin = _admin_dir(worktree)
    if admin is not None:
        try:
            entries = list(os.scandir(admin))
        except OSError:
            return True
        for entry in entries:
            if newer(Path(entry.path)):
                return True
        if newer(admin / "logs" / "HEAD"):
            return True

    try:
        if newer(worktree):
            return True
    except OSError:
        return True

    walked = False
    for root, dirs, files in os.walk(worktree, onerror=lambda _e: None):
        walked = True
        dirs[:] = [d for d in dirs if d not in _WALK_SKIP]
        base = Path(root)
        if newer(base):
            return True
        for name in files:
            if newer(base / name):
                return True
    if not walked:
        # `os.walk` swallows the error and yields nothing for a directory it
        # cannot read. That is not evidence of idleness.
        return True
    return False


def _is_within(path: Path, root: Path) -> bool:
    """Whether ``path`` is ``root`` or sits beneath it, resolving symlinks.

    ``worktree add`` takes any path and the records live in a directory the
    model can write, so a record naming somewhere outside the sweep's own root
    is out of scope — acting on it would let a chosen path steer a delete.
    """
    try:
        resolved = path.resolve()
        base = root.resolve()
    except OSError:
        return False
    return resolved == base or base in resolved.parents


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------

def _classify(
    record: WorktreeRecord,
    bare: Path,
    upstream: str,
    cutoff: float,
    protect: set[Path],
) -> str:
    """The reason this worktree is or is not reapable. ``REASON_MERGED`` means go.

    Ordered cheapest and most absolute first: a protected or locked worktree is
    never touched whatever else is true of it, and the subprocess calls at the
    end run only for a worktree that has already passed everything else.
    """
    try:
        resolved = record.path.resolve()
    except OSError:
        return REASON_FAILED
    if resolved in protect or record.path in protect:
        return REASON_PROTECTED
    if record.locked:
        return REASON_LOCKED
    if not upstream:
        return REASON_NO_UPSTREAM
    if _touched_since(record.path, cutoff):
        return REASON_RECENT

    dirty = _is_dirty(record.path)
    if dirty is None or dirty:
        return REASON_DIRTY

    # Asked before the merge-head refusal, and that order is the whole of
    # ISSUE-316. A worktree is cut from `origin/HEAD`, so before its task's
    # first commit its head *is* the upstream tip — and on a branch that lands
    # MRs as merge commits, that tip is usually a merge. Refusing first held
    # every never-committed worktree forever, which is exactly the set with
    # nothing in it to lose.
    if _is_ancestor(bare, record.head, upstream):
        return REASON_MERGED

    merge_head = _is_merge_commit(bare, record.head)
    if merge_head is None or merge_head:
        return REASON_MERGE_HEAD

    unique = _has_unique_commits(bare, upstream, record.head)
    if unique is None or unique:
        return REASON_UNMERGED
    return REASON_MERGED


def _remove(bare: Path, record: WorktreeRecord) -> bool:
    """Remove the worktree and, if it had one, its branch ref.

    The dirty check runs **again**, immediately before the removal. Everything
    in :func:`_classify` happened at least a couple of subprocess calls ago and
    the checkout is writable by anyone with the directory — and git's own
    refusal, the thing standing behind this, does not cover an ignored file:
    ``git worktree remove`` without ``--force`` deletes a ``.env`` and a
    ``node_modules`` without complaint — verified directly rather than taken
    from the documentation, since the ``node_modules`` half of that is now the
    behaviour :data:`_RECONSTRUCTIBLE_DIRS` relies on. So for exactly the class
    of file the ``--ignored`` flag was added to protect, this module's check is
    the only guard, and the window between checking and removing is the whole
    exposure.
    Re-checking does not close it, but it shrinks it from seconds to
    microseconds.

    No ``--force``, so git's refusal on a tracked modification is still the
    last thing standing behind all of it. That refusal is narrower than it
    sounds and the margin is this module's to cover: measured, ``worktree
    remove`` without ``--force`` returns 0 and deletes the checkout with an
    ``index.lock`` present and with a rebase in progress. The lock it does
    honour is the administrative one ``git worktree lock`` sets, which is a
    different thing and is checked separately in :func:`_classify`. A git
    process running *now* is caught by the retention window instead, which is
    why that window reads a still-held ``.lock`` as activity.

    The branch ref goes with ``update-ref -d`` and its **old value**, which is
    the head the containment checks actually approved — whichever of them
    answered, since ``_classify`` reaches ``REASON_MERGED`` by the ancestor
    test or by ``git cherry`` and both are about ``record.head``. Without the
    old value the delete
    takes whatever the ref points at now, so a branch that advanced between the
    listing and here would lose the new commits. ``update-ref`` rather than
    ``branch -d`` because a bare clone's HEAD deliberately points at a deleted
    ref (ISSUE-125) and ``branch -d`` consults HEAD, so it fails on every
    branch here including the merged ones — and the ``git cherry`` proof is
    stronger than the check ``branch -d`` performs anyway.
    """
    if _is_dirty(record.path) is not False:
        logger.info(
            "worktree_reaper: %s changed between the check and the removal; "
            "keeping it.", record.path,
        )
        return False

    code, _ = _git(bare, "worktree", "remove", str(record.path))
    if code != 0:
        return False

    if record.branch and record.head:
        code, _ = _git(
            bare, "update-ref", "-d", f"refs/heads/{record.branch}", record.head,
        )
        if code != 0:
            logger.warning(
                "worktree_reaper: removed the worktree at %s but could not delete "
                "refs/heads/%s (it may have moved since the sweep began). The ref "
                "is still there; nothing is lost.",
                record.path, record.branch,
            )
    return True


def reap_worktrees(
    root: Path | str,
    *,
    retention_hours: float = DEFAULT_RETENTION_HOURS,
    protect: list[Path] | None = None,
    now: float | None = None,
) -> list[ReapOutcome]:
    """Sweep every repository under ``root``, removing what has already landed.

    Returns an outcome per worktree considered — removed and held back alike.
    An empty list means the sweep ran and found no worktrees, not that it
    declined to run. Never raises; a repository that fails is logged and the
    sweep continues, because a caller must not read a partial sweep as a
    complete one.

    ``retention_hours`` is clamped to :data:`MIN_RETENTION_HOURS`. A shorter
    window does not mean "reap sooner", it means "reap the checkout a task is
    still setting up" — a worktree seconds old is clean, unlocked and carries
    nothing that is not upstream.
    """
    try:
        return _reap(root, retention_hours, protect, now)
    except Exception:  # noqa: BLE001 — the public entry point of a delete path
        logger.exception("worktree_reaper: sweep of %s failed", root)
        return []


def _reap(
    root: Path | str,
    retention_hours: float,
    protect: list[Path] | None,
    now: float | None,
) -> list[ReapOutcome]:
    root_path = Path(root)
    try:
        resolved_root = root_path.resolve()
    except OSError:
        return []
    if not resolved_root.is_dir():
        return []

    if retention_hours < MIN_RETENTION_HOURS:
        logger.warning(
            "worktree_reaper: worktree_retention_hours=%s is below the %s-hour "
            "floor and would delete a checkout a task is still setting up; "
            "using the floor.", retention_hours, MIN_RETENTION_HOURS,
        )
        retention_hours = MIN_RETENTION_HOURS

    protected = set()
    for entry in protect or []:
        protected.add(Path(entry))
        try:
            protected.add(Path(entry).resolve())
        except OSError:
            pass

    stamp = time.time() if now is None else now
    cutoff = stamp - retention_hours * 3600
    outcomes: list[ReapOutcome] = []

    for git_dir in find_git_dirs(root_path):
        # Per repository, so one that blows up in an unforeseen way cannot end
        # the sweep and leave every later repository unswept while the caller
        # reads the result as complete.
        try:
            outcomes.extend(_sweep_repo(git_dir, resolved_root, cutoff, protected))
        except Exception:  # noqa: BLE001 — see above
            logger.exception("worktree_reaper: sweeping %s failed", git_dir)
    return outcomes


def _list_worktrees(git_dir: Path) -> list[WorktreeRecord] | None:
    code, listing = _git(git_dir, "worktree", "list", "--porcelain", "-z")
    if code != 0:
        return None
    return parse_worktree_list(listing)


def _sweep_repo(
    git_dir: Path,
    root: Path,
    cutoff: float,
    protect: set[Path],
) -> list[ReapOutcome]:
    records = _list_worktrees(git_dir)
    if records is None:
        return []

    # Prune administrative entries whose directory is already gone — there is
    # no checkout left to lose, and one left in place is carried by every later
    # listing forever. Only when the listing actually shows one, and only when
    # it is a worktree this sweep owns: `git worktree prune` also unregisters a
    # worktree whose directory is merely *unreadable* right now (an unmounted
    # volume, a permissions blip), and doing that to a path outside `root` is
    # not this sweep's business.
    if any(r.prunable and not r.bare and _is_within(r.path, root) for r in records):
        _git(git_dir, "worktree", "prune")
        records = _list_worktrees(git_dir)
        if records is None:
            return []

    upstream = _upstream_head(git_dir)
    outcomes: list[ReapOutcome] = []

    for index, record in enumerate(records):
        # The first record is the repository's own main worktree — the bare
        # directory for a bare clone, the checkout for an ordinary one. Neither
        # is a task's worktree and `worktree remove` refuses both anyway.
        if record.bare or index == 0:
            outcomes.append(ReapOutcome(record.path, record.branch, False, REASON_MAIN))
            continue
        if not _is_within(record.path, root):
            outcomes.append(
                ReapOutcome(record.path, record.branch, False, REASON_OUTSIDE)
            )
            continue

        reason = _classify(record, git_dir, upstream, cutoff, protect)
        if reason != REASON_MERGED:
            outcomes.append(ReapOutcome(record.path, record.branch, False, reason))
            continue

        removed = _remove(git_dir, record)
        outcomes.append(ReapOutcome(
            record.path, record.branch, removed,
            REASON_MERGED if removed else REASON_FAILED,
        ))
    return outcomes


def reap_and_report(
    root: Path | str,
    *,
    retention_hours: float = DEFAULT_RETENTION_HOURS,
    protect: list[Path] | None = None,
) -> list[ReapOutcome]:
    """:func:`reap_worktrees`, logged.

    One line per removal, and one summary line for what was held back with a
    count per reason — the held-back set is the number an operator needs to see
    growing, and a line each would bury it on a busy repos directory.
    """
    outcomes = reap_worktrees(root, retention_hours=retention_hours, protect=protect)

    held: dict[str, int] = {}
    for outcome in outcomes:
        if outcome.removed:
            logger.info(
                "Reaped worktree %s (branch %s): every commit on it is already "
                "upstream, the checkout was clean and nothing had touched it in "
                "%.0fh.",
                outcome.path, outcome.branch or "detached",
                max(retention_hours, MIN_RETENTION_HOURS),
            )
        elif outcome.reason == REASON_FAILED:
            logger.warning(
                "Worktree %s is reapable but could not be removed; it is still "
                "on disk. Remove it by hand with `git worktree remove`.",
                outcome.path,
            )
        elif outcome.reason not in _NOT_CANDIDATE:
            held[outcome.reason] = held.get(outcome.reason, 0) + 1

    if held:
        logger.info(
            "Kept %d developer worktree(s): %s.",
            sum(held.values()),
            ", ".join(f"{count} {reason}" for reason, count in sorted(held.items())),
        )
    return outcomes
