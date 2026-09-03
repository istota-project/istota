"""Scoping a user id under a root, in one place (ISSUE-402).

``{root}/{user_id}`` is written as a plain join in several places that are each
a boundary, and the join is not the check people read it as. ``PurePath``
discards an empty component and a ``"."``, so ``Path("/mnt/shared") / "Users" /
""`` is ``Path("/mnt/shared/Users")`` — the *parent of every user's directory*
— an absolute component replaces the root outright, and ``".."`` is a child by
name and the parent on disk. Where the result is bound read-write into a
sandbox, or is the allowlist a host-side skill CLI is scoped by, each of those
is one user's task reaching every user's files.

The rule itself is not new: :func:`~istota.executor.get_user_repos_dir` has
applied it to ``developer.repos_dir`` since ISSUE-319 and its docstring already
names the three values truthiness lets through. What was new is that the
codebase held the correct pattern and the reasoning for it, and had applied it
to one of the joins. So it lives here now, and the joins that build a
containment base out of a user id call it: ``sandbox_plan.build_mount_plan``
(the workspace bind, the mount bind and the per-resource skip that compares
against it), ``executor.image_bind_roots``, ``executor.get_user_repos_dir``,
``skill_host_paths.allowed_host_roots`` and the two memory skills'
``_indexable_roots`` / ``_user_id``, plus ``db.create_task`` on the lexical
half. **Three hand-rolled copies of the same equality deliberately remain**,
and each keeps a rule this function has no room for:
``executor.get_task_control_dir`` adds a casefold test against
``.control``, ``executor.daemon_work_dir`` uses the shared root itself as its
refusal signal, and ``skill_host_paths.developer_repos_root`` validates a leaf
it was handed rather than a root plus a component. Converting them is a
separate change; naming them here is what stops this docstring claiming more
than it holds.

**Two checks, because neither catches the other's cases**, and the pair is the
whole content of the function. The lexical one refuses a component that never
became a child — ``.`` is dropped, an absolute one replaces the root, a nested
one goes deeper. The resolved one refuses ``..`` and every symlink *that
leads somewhere else*, both of which are children by name and elsewhere on
disk. ``"."`` is the case that shows why both are needed: it *passes* the
resolved test, since ``root.resolve() / "."`` is ``root.resolve()``, and only
the lexical test sees it.

One symlink case answers differently depending on the interpreter, which is why
that wording is narrow: a *cycle* at the user's own name. ``Path.resolve()``
raises ``RuntimeError`` on a loop through 3.12 and hands the path back as
written from 3.13, so ``root/alice -> root/b -> root/alice`` is refused on the
first and returned on the second. Neither is an exposure — the path names
nothing outside the root, and the cost is ``ELOOP`` at the ``mount`` or the
``open`` rather than a widened boundary — so both are accepted rather than
normalised. ``RuntimeError`` is in the caught tuple for it, and on the version
this repo runs that is load-bearing rather than forward-safety: it is not under
``OSError``, so ``get_user_repos_dir``'s original ``except OSError`` let it
escape into ``build_mount_plan``.

**Validated resolved, returned as written.** ``sandbox_plan._bind`` uses the
string it is handed as the in-namespace destination, so returning the resolved
path would put a symlinked deployment root at a different name inside the
namespace from everything bound under it, hence on another mount. The callers
that want a resolved path resolve it themselves.

Returns ``None`` rather than falling back, in every case. The fallback would be
the shared root, which is the exposure — so it fails closed, and a caller drops
the bind, the root or the allowlist entry instead of widening it.

stdlib-only leaf: imports nothing from the package, so ``skill_host_paths`` can
reach it from a skill subprocess without pulling in the sandbox planner.
Never raises — not for a hostile ``user_id`` and not for one of the wrong type,
which is why the join is inside the ``try`` and ``TypeError`` is caught beside
``OSError`` and ``ValueError``.
"""

from __future__ import annotations

from pathlib import Path


def is_scopable_user_id(user_id: object) -> bool:
    """Whether ``user_id`` can name a directory of its own under any root.

    The lexical half of :func:`scoped_user_dir`, without a root — one plain,
    non-special path component. It exists separately because the producers can
    ask the question before there is a path: ``db.create_task`` used to default
    ``user_id`` to ``""`` and validate nothing, so an unowned task row was one
    omitted argument away and every path derived from it collapsed.

    ``.`` and ``..`` are refused as whole values and not as substrings — a
    Nextcloud username containing a dot (``first.last``) is ordinary and must
    keep working. A backslash is **not** refused, for the same reason: it is an
    ordinary character in a POSIX filename, ``DOMAIN\\user`` is what an
    LDAP-backed Nextcloud hands out, and refusing it here would raise out of
    ``db.create_task`` for every task that user submits — narrowing nothing,
    since the component is genuinely contained.

    **Surrounding whitespace is refused rather than tolerated**, and that is
    about agreement rather than about containment: ``" alice"`` names a real,
    contained directory, but ``skill_host_paths`` reads ``ISTOTA_USER_ID``
    through ``.strip()`` before scoping, so the sandbox would bind
    ``{mount}/Users/ alice`` while the host-side allowlist admitted
    ``{mount}/Users/alice`` — two directories for one task, and the second is
    somebody else's. One of the two spellings has to be refused and this is the
    one nothing legitimately produces.
    """
    if not isinstance(user_id, str):
        return False
    if not user_id or user_id != user_id.strip() or user_id in (".", ".."):
        return False
    return "/" not in user_id and "\0" not in user_id


def scoped_user_dir(root: Path | str | None, user_id: object) -> Path | None:
    """``{root}/{user_id}`` when that names a child of ``root``, else ``None``."""
    if not root or not is_scopable_user_id(user_id):
        return None
    try:
        root_path = Path(root)
        candidate = root_path / user_id  # type: ignore[operator]
        contained = (
            candidate.parent == root_path
            and candidate.resolve() == root_path.resolve() / user_id  # type: ignore[operator]
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return candidate if contained else None
