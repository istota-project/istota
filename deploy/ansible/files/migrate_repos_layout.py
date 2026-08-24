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

**It needs to be told the user.** The old layout recorded no owner — which is
half the reason it is being replaced — so nothing here can derive one. Without
`--user` the script reports what it would do and changes nothing.

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


def _looks_like_a_repository(entry: Path) -> bool:
    """A bare clone, an ordinary clone, or neither."""
    if (entry / "HEAD").exists() and (entry / "objects").is_dir():
        return True
    return (entry / ".git").exists()


def _blocking_reason(entry: Path) -> str:
    """Why this directory must not move, or "" when it may.

    Checks the repository *and* every worktree registered against it: moving a
    bare clone out from under a checkout that is somewhere else entirely leaves
    a worktree pointing at a path that no longer exists.
    """
    code, listing = _git(entry, "worktree", "list", "--porcelain")
    if code != 0:
        return "git could not list its worktrees"

    checkouts: list[Path] = []
    for line in listing.splitlines():
        if line.startswith("worktree "):
            checkouts.append(Path(line[len("worktree ") :].strip()))

    for checkout in checkouts:
        if not checkout.is_dir():
            return f"a registered worktree is missing: {checkout}"
        try:
            checkout.relative_to(entry)
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
            state, _, path = line.partition(" ")
            path = path.strip().strip('"')
            if line.startswith("!!") and _is_reconstructible(path):
                continue
            if line.startswith("??") and _is_reconstructible(path):
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
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"SKIP {root} does not exist; nothing to migrate")
        return 0

    known = {u.strip() for u in args.known_user.split(",") if u.strip()}

    candidates: list[Path] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir() or entry.is_symlink():
            continue
        if entry.name in known:
            continue
        if not _looks_like_a_repository(entry):
            continue
        candidates.append(entry)

    if not candidates:
        print(f"OK {root} is already in the per-user layout")
        return 0

    if not args.user:
        for entry in candidates:
            print(f"WOULD-MOVE {entry} (set istota_developer_repos_migrate_to to move it)")
        return 0

    destination_root = root / args.user
    if args.dry_run:
        for entry in candidates:
            print(f"WOULD-MOVE {entry} -> {destination_root / entry.name}")
        return 0

    destination_root.mkdir(parents=True, exist_ok=True)

    failures = 0
    for entry in candidates:
        destination = destination_root / entry.name
        if destination.exists():
            print(f"HELD {entry}: {destination} already exists; not merging")
            continue
        reason = _blocking_reason(entry)
        if reason:
            print(f"HELD {entry}: {reason}")
            continue
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
        code, _ = _git(destination, "worktree", "repair")
        if code != 0:
            print(f"MOVED {entry} -> {destination} (worktree repair reported a problem)")
        else:
            print(f"MOVED {entry} -> {destination}")

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
