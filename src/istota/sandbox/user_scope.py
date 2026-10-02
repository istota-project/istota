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
``skill_host_paths.workspace_roots`` (the one derivation behind every
host-path allowlist since ISSUE-447, which is why ``_indexable_roots`` is no
longer in this list — it was a second copy and is deleted),
``Config.workspace_root``, ``executor._daemon_dirs``,
``repos_relocate._contained``, ``sandbox_cache_sweeper``'s two candidate
scans, the ``developer`` skill's ``_user_repos_dir`` and the ``memory``
skill's ``_user_id``, plus ``db.create_task`` on the lexical half.

**Two hand-rolled copies of the same equality deliberately remain**, and each
keeps a rule this function has no room for.
``skill_host_paths.developer_repos_root`` validates a leaf it was handed
rather than a root plus a component — there is no configured root in a skill
subprocess to check it against.

``executor.get_task_control_dir`` is the one that would be a **widening**, and
it is worth being exact about, because it reads like the most obvious
conversion in the tree. Its root is ``{temp_dir}/.control`` with the last
component deliberately left unresolved, and it compares
``candidate.resolve()`` against ``root / user_id`` *as spelled*. This function
compares against ``root.resolve() / user_id``, so a symlink planted at
``.control`` moves both sides of the equality and the check passes — a control
root pointing anywhere on disk, holding every task's assembled prompt. Read
both docstrings before folding that one in.

**Two checks, because neither catches the other's cases**, and the pair is the
whole containment test — ahead of it sits the absent-root comparison
:func:`scoped_user_dir`'s own docstring covers (ISSUE-462). The lexical one
refuses a component that never became a child — ``.`` is dropped, an absolute one replaces the root, a nested
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
    """``{root}/{user_id}`` when that names a child of ``root``, else ``None``.

    **The absent root is a comparison, not a truthiness test** (ISSUE-462).
    ``Path`` defines neither ``__bool__`` nor ``__len__``, so ``bool(Path(""))``
    is ``True`` while ``Path("")`` *is* ``Path(".")``. Under an ``if not root:``
    guard only ``None`` and ``""`` returned ``None``; ``"."``, ``"./"`` and
    every ``Path`` spelling of the three returned ``Path(user_id)`` — a path
    relative to the daemon's working directory, which passes both containment
    terms and so came back as a scoped answer. A caller that gets one where
    the rule says there is none is a caller whose scoping silently did not
    happen.

    ``Path(root) == Path(".")`` is the whole test, and it covers all five of
    those spellings at once. It is placed *after* ``Path(root)`` rather than
    beside the ``None`` check, because it needs that value and building it is
    what raises on a root of the wrong type — ``Path(7)`` is a ``TypeError``,
    which the truthiness guard happened to swallow for ``0`` and ``False``,
    and which the annotation does not stop arriving since ``root`` comes from
    config and from the environment. The surrounding ``try`` already catches
    it. Refusing rather than raising on a wrong-typed root is defence behind
    the type rather than a widening of it, so the annotation stays
    ``Path | str | None``.

    **Lexical, like the rest of the module**, which means a caller that
    resolves its root before calling has opted out of this test:
    ``Path(".").resolve()`` is an absolute path and scopes normally.
    ``_daemon_dirs``, ``skill_host_paths.workspace_roots`` and both
    ``sandbox_cache_sweeper`` scans resolve first and are in that position.
    That is not a hole left open — the working-directory root is stopped at
    the source, where ``config_mapper.coerce_path`` keeps the declared default
    for a blank setting — but a reader must not take the refusal below as
    covering a root some caller already turned into an absolute path.

    A relative root that is not the working directory keeps working:
    ``Path("workspace")/"alice"`` is a genuine child of ``Path("workspace")``,
    and refusing every relative root would be a second, wider rule than the
    one the callers ask for. ``".."`` and ``"a/.."`` are in that group and are
    answered as written for the same reason.
    """
    if root is None or not is_scopable_user_id(user_id):
        return None
    try:
        root_path = Path(root)
        if root_path == Path("."):
            return None
        candidate = root_path / user_id  # type: ignore[operator]
        contained = (
            candidate.parent == root_path
            and candidate.resolve() == root_path.resolve() / user_id  # type: ignore[operator]
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None
    return candidate if contained else None


def is_within(child: Path, root: Path) -> bool:
    """``child`` is ``root`` itself, or sits beneath it.

    **Lexical, and resolution is the caller's.** ``{root}/..`` is "within"
    ``root`` by this test and names the root's parent on disk, so a caller
    asking about containment has to resolve first, and the ones that decide a
    boundary do — each with the discipline its own boundary needs:
    ``Path.resolve()`` where the paths exist, ``os.path.realpath`` where one of
    them is a rename target that does not yet. Taking a ``resolve=`` keyword
    instead would put that choice in the hands of whoever writes the next call
    site, and the wrong default is a widened boundary rather than a wrong
    answer.

    **Not every caller resolves, and that is deliberate at two of them.**
    ``doctor`` asks about both spellings of a path in turn — as written and
    resolved — because under a symlinked deployment root a mask lands at one
    and not the other, so resolving inside here would collapse the pair it
    exists to compare. ``executor``'s cache-root checks pass one resolved side
    and one config-derived path. Stating this rather than claiming a universal
    discipline: a docstring that overstates its callers is the failure this
    module was consolidated to remove.

    **The root itself counts as within it.** ``Path.is_relative_to`` already
    answers ``True`` for two equal paths, which is why the ``a == b or
    a.is_relative_to(b)`` spelling this replaces at four sites was a redundant
    first term rather than a different rule. Stated here so a reader does not
    add the term back on the assumption that it does something.

    Never raises, for anything: a ``None``, a non-path, an embedded NUL. The
    callers are ``worktree_reaper``, ``repos_relocate``, ``git_remote_scrub``,
    ``skill_host_paths``, ``doctor``, ``executor``, ``image_attachments``,
    ``db_backup`` and ``session/tools/env``. The first four promise never to raise out of their
    own entry points, and a containment predicate that raises fails *open* at
    any caller that wraps it in a truthiness test.

    Catching cannot help with an exception raised *before* the call, which is
    the trap this leaf invites: ``Path.resolve`` and ``os.path.realpath`` both
    raise ``ValueError`` on an embedded NUL, and a caller that resolves in its
    own ``try`` has to catch it there. Two callers had that handler and lost it
    to this consolidation before review put it back.
    """
    try:
        return Path(child).is_relative_to(Path(root))
    except (TypeError, ValueError):
        return False


def paths_overlap(a: Path, b: Path) -> bool:
    """Either path is at or inside the other — the test in both directions.

    The question a bind or a mask asks about a configured path, where the two
    failures are not the same size and are equally unacceptable: above the task
    control tree reaches every user's, inside it reaches one user's or one
    task's. A single-direction test answers one of those and reads as though it
    answered both.

    Lexical and never-raising, for :func:`is_within`'s reasons. Its callers are
    ``config``'s read-only-path warning, ``doctor``'s mirror of it, and
    ``executor``'s workspace blocklist, which asks the same question about the
    source tree and the secret-key directory rather than about the control
    tree. All three resolve before asking.
    """
    return is_within(a, b) or is_within(b, a)
