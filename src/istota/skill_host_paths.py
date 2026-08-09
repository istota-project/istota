"""Host-path allowlist shared by the skill CLIs that take one.

A skill CLI does not run in the sandbox. The proxy spawns it host-side, with
the daemon's filesystem view, precisely so it can reach the databases the model
cannot. That makes any verb accepting a *host* path an arbitrary-file read or
write unless it is scoped — and the model chooses the path.

Two verbs need the same scoping today: devbox's ``cp-in`` / ``cp-out``, and
``kv set --value-file``. The rule lives here rather than in either skill so the
copies cannot drift; a stdlib-only leaf module, importable from a skill
subprocess without dragging in the framework.

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
contains no symlink components, which is what closes that window.
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
