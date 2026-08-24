#!/usr/bin/env python3
"""Move a flat `developer.repos_dir` down one level, into `{repos_dir}/{user}`.

`repos_dir` used to be a single global directory holding every user's bare
clones and worktrees. It is now a per-user root: the daemon derives
`{repos_dir}/{user_id}` and hands that to the bwrap bind, the devbox mount, the
`DEVELOPER_REPOS_DIR` manifest variable and the credential scrub. This script is
the one-time move that gets an existing host into that shape.

Three rules, and each of them is what keeps a delete-adjacent operation on
somebody's only copy of their work honest.

**It never moves a directory holding uncommitted work.** A repository under here
is routinely a bare clone with live worktrees beside it, and a task may be
running in one right now. Anything with a modified tracked file, a staged
change, an untracked file that is not reconstructible build output, or a
worktree registered outside this root is named and left exactly where it is.

**It never merges into an existing destination.** If `{repos_dir}/{user}/x`
already exists, `x` stays at the top level and is reported. A merge would be
the one operation here with no way back.

**It needs to know whose the clones are, and it exits non-zero when it cannot
work that out.** The old layout recorded no owner, which is half the reason it
is being replaced. With exactly one configured user there is nothing to derive
between and the move happens; with several, the script reports what it would do,
changes nothing and exits 2 — because a green play here is followed by a daemon
restart into a state where the repos bind names an empty directory and the
developer skill is silently unusable.

Idempotent: a root already in the per-user shape has nothing at the top level
that is a repository, so a rerun moves nothing and exits 0.

Output is line-oriented and the Ansible task keys `changed` on the `MOVED `
prefix, so keep it.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

#: Directories a tool rebuilds from files that are already committed. Same list
#: and the same reasoning as `worktree_reaper._RECONSTRUCTIBLE_DIRS`: an
#: untracked `node_modules` must not pin a repository in the old layout for
#: ever, and a name that is *sometimes* build output (`dist`, `build`, `target`)
#: is deliberately absent, because the cost of being wrong is asymmetric.
RECONSTRUCTIBLE = frozenset({
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".cargo",
    ".gradle",
    ".tox",
})

GIT_TIMEOUT = 60

#: How quiet a directory has to have been before it may move.
#:
#: **The clean check is not enough on its own, and the reason is specific.** A
#: task running `npm ci` or `uv sync` right now has produced exactly
#: `?? node_modules/…` and `?? .venv/…`, which `_is_reconstructible` discounts
#: by design — so a checkout with an install in flight reads as *clean* and
#: would be moved out from under the process writing to it. The daemon is up and
#: dispatching while this runs; the play does not stop it.
#:
#: The same guard `worktree_reaper` uses, at a much shorter window, because the
#: two protect against different lengths of thing: the reaper is a periodic
#: sweep that must not delete a checkout a task might return to, and this is a
#: one-shot move that must not land in the middle of a write. Measured across
#: the checkout *and* the git directory, because a `git commit` touches no
#: working-tree file and an edit touches no administrative one.
DEFAULT_IDLE_MINUTES = 15


def _git(cwd: Path, *args: str) -> tuple[int, str]:
    """Run git in `cwd`, returning `(returncode, stdout)`.

    `-c` overrides rather than the repository's own config: a repository under
    `repos_dir` is model-writable, and `core.fsmonitor` or `diff.external` in
    one makes a plain `git status` run a program as this user. Mirrors
    `istota.git_hardening`, restated here because this script ships as a
    standalone file the role copies to the host.
    """
    hardening = [
        "-c", "core.fsmonitor=",
        "-c", "diff.external=",
        "-c", "gpg.program=/bin/false",
        "-c", "gpg.openpgp.program=/bin/false",
        "-c", "gpg.x509.program=/bin/false",
        "-c", "gpg.ssh.program=/bin/false",
        "-c", "core.pager=cat",
        "-c", "color.ui=false",
    ]
    env = dict(os.environ)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_OPTIONAL_LOCKS"] = "0"
    try:
        proc = subprocess.run(
            ["git", *hardening, *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, f"git failed: {exc}"
    return proc.returncode, proc.stdout


def _is_reconstructible(path: str) -> bool:
    """True when *any* component of `path` is a rebuildable directory.

    Any component, not the last: git collapses an ignored directory at the
    point the pattern matched, so `node_modules/pkg/` is a line whose last
    component is `pkg`.
    """
    return any(part in RECONSTRUCTIBLE for part in Path(path).parts)


#: How far below a top-level directory to look for a repository. The documented
#: layout is `{repos_dir}/<namespace>/<project>.git`, so the thing that moves is
#: the *namespace* directory and the repository is one level inside it; the
#: extra level covers a repository filed one deeper by hand. Same reasoning and
#: the same figure as `git_remote_scrub._MAX_DEPTH`, which this cannot import
#: because the role copies this file to the host on its own.
_MAX_DEPTH = 3


def _is_git_dir(path: Path) -> bool:
    """A bare clone or the `.git` of an ordinary one."""
    return all((path / marker).exists() for marker in ("HEAD", "config", "objects"))


def _repositories_under(entry: Path) -> list[Path]:
    """Every git directory at or below `entry`, pruning at each one.

    Returns the *git directory* — the bare clone itself, or the `.git` of an
    ordinary one — because that is where `git worktree list` has to run.
    """
    if _is_git_dir(entry):
        return [entry]

    found: list[Path] = []
    for dirpath, dirnames, _filenames in os.walk(entry, onerror=lambda _e: None):
        here = Path(dirpath)
        depth = len(here.relative_to(entry).parts)
        if _is_git_dir(here):
            found.append(here)
            dirnames[:] = []
            continue
        if ".git" in dirnames and _is_git_dir(here / ".git"):
            found.append(here / ".git")
            dirnames[:] = []
            continue
        if depth >= _MAX_DEPTH:
            dirnames[:] = []
    return found


def _checkouts(git_dir: Path) -> list[Path] | None:
    """Every non-bare worktree registered against `git_dir`, or None on failure.

    A record is a run of lines ending at a blank one. The `bare` line marks the
    bare clone's own entry, which has no work tree at all — `git status` there
    exits non-zero with "this operation must be run in a work tree", so counting
    it would hold every bare clone in the tree for a reason that has nothing to
    do with its contents. That is the documented layout, so getting this wrong
    means the migration never moves anything.
    """
    code, listing = _git(git_dir, "worktree", "list", "--porcelain", "-z")
    if code != 0:
        return None

    # `-z`, not the line-oriented form, and for the reason `worktree_reaper`
    # already pays for: git does not quote a newline in a worktree path in the
    # line-oriented output, so a path whose second physical line is exactly
    # `bare` would drop that worktree from this list — and a worktree nobody
    # `git status`es cannot hold the move. Paths under `repos_dir` are chosen by
    # the model (`git worktree add <path>`), so this is reachable rather than
    # theoretical. In `-z` form each attribute is NUL-terminated and a record
    # ends at an empty attribute.
    found: list[Path] = []
    current: Path | None = None
    is_bare = False
    for attribute in listing.split("\0"):
        if attribute.startswith("worktree "):
            current = Path(attribute[len("worktree ") :])
            is_bare = False
            continue
        if attribute == "bare":
            is_bare = True
            continue
        if attribute:
            continue
        if current is not None and not is_bare:
            found.append(current)
        current, is_bare = None, False
    if current is not None and not is_bare:
        found.append(current)
    return found


def _resolve(path: Path) -> Path:
    """`path.resolve()`, falling back to the path itself.

    Never raises: a broken link or a permission error must not take the
    whole migration with it, and the unresolved path is still the honest
    answer for a comparison that will then simply not match.
    """
    try:
        return path.resolve()
    except OSError:
        return path


def _newest_mtime(path: Path) -> float:
    """The most recent mtime anywhere under `path`, or 0.0.

    Bounded by pruning at the reconstructible directories: walking a
    `node_modules` is tens of thousands of stats for an answer the directory's
    own mtime already gives.
    """
    newest = 0.0
    try:
        newest = path.stat().st_mtime
    except OSError:
        return 0.0
    for dirpath, dirnames, filenames in os.walk(path, onerror=lambda _e: None):
        dirnames[:] = [d for d in dirnames if d not in RECONSTRUCTIBLE]
        here = Path(dirpath)
        for name in list(dirnames) + filenames:
            try:
                newest = max(newest, (here / name).lstat().st_mtime)
            except OSError:
                continue
    return newest


def _busy_reason(paths: list[Path], idle_seconds: float, now: float) -> str:
    """Why these directories look like live work, or "" when they are quiet."""
    if idle_seconds <= 0:
        return ""
    for path in paths:
        newest = _newest_mtime(path)
        if newest and now - newest < idle_seconds:
            age = int(now - newest)
            return f"{path} was written {age}s ago; something may be using it"
    return ""


def _blocking_reason(git_dir: Path, entry: Path) -> str:
    """Why this repository must not move, or "" when it may.

    Checks every worktree registered against it: moving a bare clone out from
    under a checkout that is somewhere else entirely leaves a worktree pointing
    at a path that no longer exists, and `worktree repair` run from the new
    location cannot reach it.
    """
    checkouts = _checkouts(git_dir)
    if checkouts is None:
        return "git could not list its worktrees"

    # Compared resolved, on both sides. `git worktree list` reports git's own
    # record of the path, and a `repos_dir` that is or sits under a symlink — a
    # common shape for a data volume — spells it differently from the one this
    # walk produced. Unresolved, every repository on such a host is held with
    # "a worktree lives outside this directory", which is false and points an
    # operator at a problem that does not exist.
    resolved_entry = _resolve(entry)
    for checkout in checkouts:
        if not checkout.is_dir():
            return f"a registered worktree is missing: {checkout}"
        try:
            _resolve(checkout).relative_to(resolved_entry)
        except ValueError:
            return f"a worktree lives outside this directory: {checkout}"
        code, status = _git(
            checkout,
            "status",
            "--porcelain",
            "--untracked-files=all",
            "--ignored=matching",
        )
        if code != 0:
            return f"git status failed in {checkout}"
        for line in status.splitlines():
            if not line.strip():
                continue
            # `XY path`, with `!!` for ignored and `??` for untracked. Anything
            # else is a tracked modification or a staged change and holds the
            # move outright.
            path = line[3:].strip().strip('"')
            if line.startswith(("!!", "??")) and _is_reconstructible(path):
                continue
            return f"uncommitted work in {checkout}: {line.strip()}"
    return ""


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="migrate_repos_layout")
    parser.add_argument("--root", required=True)
    parser.add_argument("--user", default="", help="the user to move repositories under")
    parser.add_argument(
        "--known-user",
        default="",
        help="comma-separated user ids; a top-level directory with one of these "
             "names is already a per-user root and is left alone",
    )
    parser.add_argument(
        "--idle-minutes",
        type=float,
        default=DEFAULT_IDLE_MINUTES,
        help="hold a directory written to more recently than this; 0 disables",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"SKIP {root} does not exist; nothing to migrate")
        return 0

    known = {u.strip() for u in args.known_user.split(",") if u.strip()}
    idle_seconds = max(args.idle_minutes, 0.0) * 60.0
    now = time.time()

    # A candidate is a top-level directory holding at least one repository —
    # not one that *is* one. The documented layout puts the bare clone at
    # `{root}/<namespace>/<project>.git`, so the thing that moves is the
    # namespace directory, and a rule that looked for a repository at the top
    # level would find nothing on every host in that shape and report the tree
    # as already migrated.
    candidates: list[tuple[Path, list[Path]]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        repositories = _repositories_under(entry)
        if not repositories:
            continue
        # **A name is not enough to say "already migrated".** `--known-user`
        # holds user ids, and a top-level entry in the old layout is a *forge
        # namespace* — one named after some other istota user is entirely
        # ordinary, and skipping it on the name alone would leave it behind
        # while the script printed OK. The depth of its repositories is the real
        # discriminator: a per-user root holds them at depth 2 or more
        # (`{user}/{namespace}/{project}.git`), a namespace at depth 1.
        if entry.name in known:
            depths = [len(g.relative_to(entry).parts) for g in repositories]
            if min(depths) >= 2:
                continue
            print(
                f"NOTE {entry} is named for a configured user but holds a "
                f"repository at depth {min(depths)}; treating it as a namespace"
            )
        candidates.append((entry, repositories))

    if not candidates:
        print(f"OK {root} is already in the per-user layout")
        return 0

    user = args.user
    if not user and len(known) == 1:
        # **Derived only where there is nothing to derive between.** One
        # configured user means every clone under here is theirs; there is no
        # judgement to make and no way to file one person's work under
        # another's name. This is the reference deployment, and leaving it to a
        # hand-set variable is what turns "the role performs the move" into "the
        # role reports and does nothing" on the host that most needs it.
        user = next(iter(known))
        print(f"NOTE one configured user ({user}); moving repositories under them")

    if not user:
        for entry, _ in candidates:
            print(
                f"WOULD-MOVE {entry} (set istota_developer_repos_migrate_to to "
                f"the user these belong to)"
            )
        # Non-zero, because a green play here is followed by a daemon restart
        # into a state where the repos bind names an empty directory and the
        # developer skill is silently unusable. With more than one configured
        # user nothing can work out the owner, so the operator has to.
        return 2

    destination_root = root / user
    if args.dry_run:
        for entry, _ in candidates:
            print(f"WOULD-MOVE {entry} -> {destination_root / entry.name}")
        return 0

    destination_root.mkdir(parents=True, exist_ok=True)

    failures = 0
    for entry, repositories in candidates:
        destination = destination_root / entry.name
        if destination.exists():
            print(f"HELD {entry}: {destination} already exists; not merging")
            continue
        # Every repository under it, not just the first: the whole directory
        # moves, so one dirty checkout anywhere inside holds all of them.
        reason = next(
            (r for r in (_blocking_reason(g, entry) for g in repositories) if r), ""
        )
        # The idle window, after the clean check and before the move. The clean
        # check cannot see a build in flight — an install produces exactly the
        # untracked paths `_is_reconstructible` discounts — so without this a
        # directory a task is writing to reads as clean and moves.
        reason = reason or _busy_reason([entry], idle_seconds, now)
        if reason:
            print(f"HELD {entry}: {reason}")
            continue
        # Captured *before* the move, because afterwards every path in the
        # listing names a directory that no longer exists.
        registered = {g: (_checkouts(g) or []) for g in repositories}
        try:
            shutil.move(str(entry), str(destination))
        except OSError as exc:
            print(f"FAILED {entry}: {exc}")
            failures += 1
            continue
        # A move changes every worktree's absolute path, and both halves of the
        # link record one. `git worktree repair` rewrites both from where the
        # repository now is; without it every worktree is registered against a
        # path that no longer exists.
        problems = 0
        for git_dir, checkouts in registered.items():
            moved_git_dir = destination / git_dir.relative_to(entry)
            # **The new worktree paths are arguments**, and that is the whole
            # difference between a repaired worktree and a broken one. A bare
            # `repair` fixes worktrees whose administrative entry it can still
            # find — but here the *repository and its worktrees moved together*,
            # so every path in that entry is gone and git has nothing to look
            # at. Git's own instruction for this case is to name the new
            # locations.
            moved_checkouts = [
                str(destination / checkout.relative_to(entry)) for checkout in checkouts
            ]
            code, _ = _git(moved_git_dir, "worktree", "repair", *moved_checkouts)
            if code != 0:
                problems += 1
        if problems:
            print(f"MOVED {entry} -> {destination} (worktree repair reported a problem)")
        else:
            print(f"MOVED {entry} -> {destination}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
