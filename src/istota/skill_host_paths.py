"""Host-path allowlist shared by the skill CLIs that take one.

A skill CLI does not run in the sandbox. The proxy spawns it host-side, with
the daemon's filesystem view, precisely so it can reach the databases the model
cannot. That makes any verb accepting a *host* path an arbitrary-file read or
write unless it is scoped — and the model chooses the path.

There are two allowlists here, for two kinds of path.

``resolve_host_path`` scopes a path inside the caller's own workspace against
the mount roots below. Its consumers are devbox's ``cp-in`` / ``cp-out``, ``kv
set --value-file``, and email's outbound ``--attach``; ``scheduler_deferred``
applies the same rule to deferred health-op paths.

``resolve_under_repos`` scopes a *worktree* against ``DEVELOPER_REPOS_DIR``,
which is somewhere else entirely and is bound into the sandbox for admins only.
Its consumer is the ``code_review`` CLI. The two live in one module so neither
the roots nor the error convention can drift apart, but they are separate
allowlists and a path admitted by one is not admitted by the other.

The rule lives here rather than in any skill: a stdlib-only leaf module,
importable from a skill subprocess without dragging in the framework.

**The roots mirror what the sandbox binds, per user.** `NEXTCLOUD_MOUNT_PATH`
is deliberately the *shared* mount root for everyone — every consumer builds
`$NEXTCLOUD_MOUNT_PATH/Users/<uid>/…` itself, and per-user isolation comes from
`build_bwrap_cmd` binding only the caller's own subtree plus each CLI
self-scoping by `ISTOTA_USER_ID`. A host-side path argument does neither, so
taking the mount root as a root would hand back any other user's workspace
through `kv get`. The roots here are therefore the same three the sandbox
binds: the task's deferred dir, `{mount}/Users/{ISTOTA_USER_ID}`, and the
task's own `{mount}/Channels/{ISTOTA_CONVERSATION_TOKEN}` — plus `{mount}/Talk`
for reads only, matching its read-only bind. With no `ISTOTA_USER_ID` in the
environment the mount contributes nothing; the deferred dir stands alone.

**Callers must use the returned resolved path.** Validating one path and then
opening the original re-walks every symlink in it, so a link swapped in between
lands outside the allowlist with the check already passed. The resolved path
has no symlink components *as of the check*, which removes that re-walk.

It does not make the caller's later open atomic. Both allowlists validate a
path, and the trees they validate are bound read-write into the sandbox, so a
component can in principle be replaced between the return and the use. Path
validation cannot close that on its own. What it does close is the much larger
window of re-resolving an attacker-supplied string, which is why the rule is to
operate on what comes back and never to re-walk the argument.
"""

import os
from pathlib import Path


def allowed_host_roots(*, writable: bool = False) -> list[Path]:
    """Host directories a skill CLI may read from (or write to).

    `writable=True` drops the roots the sandbox binds read-only, so a
    destination path can't be steered into shared, non-user-owned storage.
    """
    roots: list[Path] = []

    deferred = os.environ.get("ISTOTA_DEFERRED_DIR", "").strip()
    if deferred:
        try:
            roots.append(Path(deferred).resolve())
        except OSError:
            pass

    mount_raw = os.environ.get("NEXTCLOUD_MOUNT_PATH", "").strip()
    user_id = os.environ.get("ISTOTA_USER_ID", "").strip()
    if mount_raw and user_id:
        try:
            mount = Path(mount_raw).resolve()
        except OSError:
            return roots
        roots.append(mount / "Users" / user_id)
        token = os.environ.get("ISTOTA_CONVERSATION_TOKEN", "").strip()
        # Guard the token the same way the container name is guarded: it lands
        # in a path, and "../.." would walk straight back out of Channels/.
        if token and "/" not in token and token not in (".", ".."):
            roots.append(mount / "Channels" / token)
        if not writable:
            # Talk attachments are bound read-only in the sandbox; a read may
            # reach them, a destination may not.
            roots.append(mount / "Talk")
    return roots


def resolve_host_path(
    path: Path, *, writable: bool, operation: str,
) -> tuple[Path | None, str | None]:
    """Validate `path` against the allowlist and return the path to actually use.

    Returns `(resolved, None)` on success or `(None, error)` on refusal.
    **Use the returned path**, not the one passed in — see the module docstring.

    `writable=False` means an existing source to read; `writable=True` means a
    destination that need not exist yet.
    """
    roots = allowed_host_roots(writable=writable)
    if not roots:
        # No allowlist resolvable — a CLI smoke test outside the executor, or a
        # misconfigured deployment. Refuse rather than silently widening the
        # boundary to the whole filesystem.
        return None, (
            f"No allowed host roots configured (ISTOTA_DEFERRED_DIR / "
            f"NEXTCLOUD_MOUNT_PATH + ISTOTA_USER_ID unset). {operation} refused."
        )

    try:
        if not writable:
            if path.is_symlink():
                return None, f"Refusing host-side symlink: {path}"
            if not path.exists():
                return None, f"Path not found: {path}"
            resolved = path.resolve(strict=True)
        else:
            # A destination need not exist yet, so anchor on the parent. Resolve
            # and check it *before* creating anything — the old order mkdir'd an
            # out-of-bounds tree as the daemon user and only then refused.
            parent = path.parent
            if parent.is_symlink():
                return None, f"Refusing host-side symlink on dest parent: {parent}"
            resolved_parent = parent.resolve()
            if not _under_a_root(resolved_parent, roots):
                return None, _outside(resolved_parent, roots)
            parent.mkdir(parents=True, exist_ok=True)
            resolved = resolved_parent / path.name
    except OSError as e:
        return None, f"Path resolution failed: {e}"

    if not _under_a_root(resolved, roots):
        return None, _outside(resolved, roots)
    return resolved, None


def validate_host_path(
    path: Path, *, must_exist: bool, operation: str,
) -> str | None:
    """Error-only wrapper for callers that don't need the resolved path.

    Prefer `resolve_host_path`: reading the unresolved path reopens the symlink
    window this check exists to close.
    """
    _, err = resolve_host_path(path, writable=not must_exist, operation=operation)
    return err


def developer_repos_root() -> Path | None:
    """The resolved `DEVELOPER_REPOS_DIR`, or None when it is unset or blank.

    Separate from `allowed_host_roots` on purpose. The mount roots scope a path
    the model names inside its own workspace; this scopes a *worktree* the model
    names, which lives somewhere else entirely. Mixing the two would let a
    review read the user's workspace and a `kv --value-file` read a checkout.

    **This resolves the root; it does not decide who may use it.** The env spec
    that sets `DEVELOPER_REPOS_DIR` gates on `developer.enabled` and
    `developer.repos_dir` with no admin condition, while `build_bwrap_cmd` binds
    the directory only when the caller is an admin. So a non-admin task has the
    variable set and no bind behind it, and a non-`None` return here says
    nothing about authorization. The `is_admin` check belongs to the calling CLI.
    """
    raw = os.environ.get("DEVELOPER_REPOS_DIR", "").strip()
    if not raw:
        return None
    try:
        root = Path(raw).resolve()
    except (OSError, ValueError):
        return None

    # A relative value would anchor on wherever the CLI happened to be started,
    # which is not a boundary anyone chose.
    if not Path(raw).is_absolute():
        return None
    # `/` passes every containment check there is. Refusing an unset variable
    # "rather than widening to the whole filesystem" and then accepting the one
    # value that widens to the whole filesystem is not a boundary. Two
    # components is the shallowest plausible real root (`/srv/repos`).
    if len(root.parts) < 3:
        return None
    return root


def resolve_under_repos(path: str | Path) -> tuple[Path | None, str | None]:
    """Validate a worktree directory against `DEVELOPER_REPOS_DIR`.

    Returns `(resolved, None)` on success or `(None, error)` on refusal, the
    same shape as `resolve_host_path` — one module, one error convention, and
    the caller has to turn the failure into a JSON envelope either way.

    **The symlink rule differs from `resolve_host_path`, deliberately.** That
    one refuses a symlinked argument outright. This one follows links and then
    checks containment, because a worktree is legitimately reached through one
    and refusing would break ordinary layouts. A link that stays inside the root
    is therefore accepted; one that leaves it resolves outside and is refused.
    Following is what catches the escape, so it has to happen before the check.

    **Use the returned path**, and do not re-walk the argument — see the module
    docstring for what that does and does not guarantee.

    **Containment is not sufficient to make a git invocation on the result
    safe.** A path fully inside the root can still be a repository that runs
    code or reads outside it, because `repos_dir` is bound read-write into the
    sandbox and a repository's behaviour lives in files the model can write:

    - `.git/config` in a contained worktree can set `diff.external`,
      `core.fsmonitor` or a textconv filter, each of which makes `git diff`
      execute an arbitrary command as the daemon user.
    - A plain-directory argument makes git search *upward* for a repository, so
      it can operate on one above the root.
    - A `.git` file containing `gitdir: <outside>` redirects the repository out
      of the root while `rev-parse --show-toplevel` still reports the contained
      path.

    Callers must therefore neutralise repository-supplied configuration and
    confirm the resolved git directory, not just the worktree path. See the
    `code_review` CLI for the invocation that does this; do not call git on the
    result of this function without it.
    """
    root = developer_repos_root()
    if root is None:
        return None, (
            "DEVELOPER_REPOS_DIR is unset, so no worktree path can be validated. "
            "Refusing rather than widening to the whole filesystem."
        )

    # `Path("")` is `.`, which would resolve to wherever the daemon happens to
    # have been started. Refuse explicitly rather than letting the outcome
    # depend on the process CWD.
    if not isinstance(path, (str, os.PathLike)):
        # The contract is to return an error, never to raise. `Path(None)` is a
        # TypeError, and `args.worktree` is None whenever the flag is omitted.
        return None, f"Invalid worktree path of type {type(path).__name__}"

    raw = str(path).strip()
    if not raw:
        return None, "Empty worktree path"

    try:
        candidate = Path(raw)
        if not candidate.exists():
            return None, f"Path not found: {candidate}"
        resolved = candidate.resolve(strict=True)
        if not resolved.is_dir():
            return None, f"Not a directory: {resolved}"
    except (OSError, ValueError) as e:
        # ValueError covers an embedded NUL, which `exists()` raises rather than
        # returning False on some versions.
        return None, f"Path resolution failed: {e}"

    if not _under_a_root(resolved, [root]):
        return None, (
            f"Path {resolved} is outside the developer repos root ({root})"
        )
    return resolved, None


def _under_a_root(resolved: Path, roots: list[Path]) -> bool:
    for root in roots:
        try:
            resolved.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _outside(resolved: Path, roots: list[Path]) -> str:
    return (
        f"Path {resolved} is outside allowed roots "
        f"({', '.join(str(r) for r in roots)})"
    )
