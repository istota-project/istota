"""Running git where only one user's repositories exist.

The `-c` overrides in `git_hardening` stop a repository's config running a
program. They say nothing about where its git directory points: `worktrees`,
`refs` or `objects` as links, a `commondir` file or an `objects/info/alternates`
line each take git to another repository, and git reads and writes there as if
it were this one. Those are files in a tree the model writes, so no check of them
holds until the git process has finished. In a namespace holding the system
directories and one user's subtree, a path out of that subtree names nothing.

The code review engine's reads bind read-only and have no network. The worktree
reaper and the repos migrator write, and the reaper's one fetch needs a network,
which is what the two flags are for.

Where bwrap is unavailable this answers None and the caller runs git unwrapped.
The model's own tools have the daemon's view on those shapes too, so there is no
boundary between users to keep.

Not `build_bwrap_cmd`, which needs a `Config` and a task and binds the venv, the
temp dir and the workspace. Function-scope imports because `executor` imports
this package's neighbours, and because `git_hardening` reaches this module from
code that has to stay a leaf when it passes no scope.
"""

from __future__ import annotations

from pathlib import Path


def namespaced(
    argv: list[str],
    root: Path,
    chdir: Path,
    *,
    writable: bool = False,
    network: bool = False,
) -> list[str] | None:
    """`argv` in a namespace holding the system and `root`, or None.

    None where bwrap cannot make one. `root` is bound read-only unless
    `writable`, and the network namespace is private unless `network`.
    """
    from istota.executor import (
        _bwrap_available,
        _bwrap_requires_unshare_user,
        _bwrap_supports_disable_userns,
    )
    from istota.sandbox.plan import _ETC_BINDS
    from istota.sandbox.shell_exec import on_daemon_path

    bwrap = on_daemon_path("bwrap") if _bwrap_available() else None
    if bwrap is None:
        return None
    wrapped = [bwrap]
    disable_userns = _bwrap_supports_disable_userns()
    if disable_userns or _bwrap_requires_unshare_user():
        wrapped.append("--unshare-user")
    if disable_userns:
        wrapped.append("--disable-userns")
    wrapped += ["--unshare-pid", "--unshare-ipc", "--unshare-uts"]
    if not network:
        wrapped.append("--unshare-net")
    wrapped += [
        "--unshare-cgroup-try", "--die-with-parent",
        "--ro-bind", "/usr", "/usr",
    ]
    for compat in ("/bin", "/lib", "/lib64", "/sbin"):
        path = Path(compat)
        if path.is_symlink():
            wrapped += ["--symlink", str(path.readlink()), compat]
        elif path.exists():
            wrapped += ["--ro-bind", compat, compat]
    for etc in _ETC_BINDS:
        wrapped += ["--ro-bind-try", etc, etc]
    wrapped += ["--dev", "/dev", "--tmpfs", "/tmp"]
    # Both spellings, because a `.git` file holds whichever the task's own
    # namespace showed it, and the variable's may run through a link.
    bind = "--bind" if writable else "--ro-bind"
    for dest in dict.fromkeys((str(root), str(root.resolve()))):
        wrapped += [bind, str(root), dest]
    return [*wrapped, "--chdir", str(chdir), "--", *argv]
