"""The binds and the roots, asserted against each other.

This is the assertion the sandbox-mount-plan-as-data spec exists to make
possible. `build_bwrap_cmd` and `native_fs_roots` used to decide the same nine
rules twice, in two shapes, and nothing anywhere said the two answers had to
agree — all twenty test files touching them asserted against one side or the
other. ISSUE-319 and ISSUE-320 were each one copy disagreeing with the other,
and each cost a filed bug to discover.

Both are projections of one `MountPlan` now, so the agreement is statable:
every `user_data` bind reaches the roots in the list its mode earns, every root
traces back to a `user_data` bind, and the places the two deliberately differ
are named here by `Mount.reason`. That last part is what stops this file
decaying: a third divergence appearing without an edit to `DIVERGENCES` fails,
and the entries are a literal dict rather than a predicate for exactly that
reason. A predicate is how a parity test gets quietly widened into vacuity.

`tests/test_sandbox_plan_projection.py` is the other half and is not a subset
of this one: it covers the four rules `project_fs_roots` applies *instead of* a
mechanical walk, which are precisely the cases this file has to exempt. Neither
file can state the other's property.

The matrix is `tests/test_sandbox_argv_golden.py`'s, imported rather than
restated. It is thirty cases over the axes the spec's Design section names, it
already has a test asserting both sides of each axis are exercised, and a
second copy of it would agree with the first until a `Case` default changed.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from unittest.mock import patch

import pytest

from istota import db
from istota.executor import (
    _sandbox_bind_targets,
    custom_system_prompt_path,
    native_fs_roots,
    sandbox_cache_is_derived,
)
from istota.sandbox_plan import (
    Mount,
    MountPlan,
    SandboxProfile,
    build_mount_plan,
    project_fs_roots,
)
from tests.test_sandbox_argv_golden import (
    CASES,
    CASES_BY_NAME,
    Case,
    _make_config,
    _make_world,
    _RO,
    _RW,
)

# --------------------------------------------------------- the two divergences

#: Where the roots deliberately do not follow the plain reading of the plan,
#: keyed by ``Mount.reason``. Both entries are behaviours ``native_fs_roots``
#: had before the projection existed and that a mechanical walk loses.
#:
#: A literal dict, never a predicate. The whole value of naming them is that a
#: *third* divergence fails this file rather than being absorbed, and a
#: predicate broad enough to describe these two is broad enough to describe the
#: next one nobody looked at.
DIVERGENCES: dict[str, str] = {
    "developer_dir": (
        "the .developer carve-out is a write-deny root whether or not the "
        "directory exists, and at the path as written rather than resolved. "
        "build_bwrap_cmd re-reads the filesystem on every invocation while "
        "these roots are built once per task, so an existence gate here leaves "
        "a .developer created mid-run read-only for Bash and writable for the "
        "file tools"
    ),
    "package_cache": (
        "the package cache is user data on the fallback branch and not on the "
        "derived one (sandbox_cache_is_derived), which is the one reason whose "
        "user_data answer is not constant. Derived, it sits inside the repos "
        "subtree that is already a write root, and a root of its own would "
        "make a symlink planted at .package-caches a write root once ToolEnv "
        "realpaths it (ISSUE-320)"
    ),
}


# ------------------------------------------------------------ building a plan


@dataclass(frozen=True)
class Built:
    """One case, built: the plan and everything needed to re-derive it."""

    case: Case
    plan: MountPlan
    config: object
    task: db.Task
    resources: list[db.UserResource]
    world: dict[str, Path]
    roots: tuple[list[Path], list[Path], list[Path]]


def build_plan(
    case: Case,
    root: Path,
    monkeypatch,
    *,
    profile: SandboxProfile | None = None,
    prepare: Callable[[dict[str, Path]], None] | None = None,
) -> Built:
    """The plan for one golden case, plus its projection.

    Deliberately not routed through ``build_bwrap_cmd``: ``bwrap_unavailable``
    is a matrix case, and there the argv is the bare command while
    ``native_fs_roots`` still builds a plan — it must, since on a host with no
    bwrap the roots are the only confinement there is. Parity has to hold on
    that shape too, so this builds the plan directly and patches nothing about
    bwrap.

    ``prepare`` runs after the world is on disk and before the config is read,
    for the one case below that needs a directory the golden matrix's world
    does not create.
    """
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    world = _make_world(root, case)
    if prepare is not None:
        prepare(world)
    config = _make_config(case, world)
    monkeypatch.setenv("HOME", str(world["home"]))

    task = db.Task(
        id=1,
        prompt="test",
        user_id=case.user_id,
        source_type="talk",
        status="running",
        conversation_token=case.conversation_token,
    )
    resources = [
        db.UserResource(
            id=index + 1,
            user_id=case.user_id,
            resource_type="folder",
            resource_path=path,
            display_name=None,
            permissions=permissions,
        )
        for index, (path, permissions) in enumerate(case.resources)
    ]

    plan = _plan(case, config, task, resources, world, profile or case.profile)
    return Built(
        case=case,
        plan=plan,
        config=config,
        task=task,
        resources=resources,
        world=world,
        roots=project_fs_roots(plan, None),
    )


def _plan(case, config, task, resources, world, profile: SandboxProfile) -> MountPlan:
    with patch(
        "istota.executor._source_and_venv_paths",
        return_value=(world["src"], world["venv"]),
    ):
        return build_mount_plan(
            config,
            task,
            case.is_admin,
            resources,
            world["user_temp"],
            profile=profile,
            proxy_sock=world["sockets"] / "proxy.sock" if case.proxy_sock else None,
            net_proxy_sock=(
                world["sockets"] / "net.sock" if case.net_proxy_sock else None
            ),
            extra_ro_binds=[world["extra"] / name for name in case.extra_ro_binds],
            authorized_skills=frozenset(case.authorized_skills),
            workspace_dir=world["workspace"] if case.workspace else None,
        )


def other_profile_plan(built: "Built", profile: SandboxProfile) -> MountPlan:
    """The same case's plan under the other profile, from the *same* world.

    Two `build_plan` calls would each set ``HOME`` to their own world, so
    anything reading the environment afterwards — `_sandbox_bind_targets` does
    — would be answered about whichever ran last while being compared against
    the other one's binds. One world, two plans.
    """
    return _plan(
        built.case, built.config, built.task, built.resources, built.world, profile,
    )


#: One case the golden matrix does not carry, because it needs a directory
#: `_make_world` does not create and adding it would rewrite a golden.
#:
#: Rule 2 — a read-only user-data bind nested inside an earlier read-write one
#: is a write-deny root rather than a read root — fires in the matrix only for
#: `.developer`, which is also the entry `DIVERGENCES` exempts. So without this
#: case the rule is asserted nowhere on the *non*-exempt path, and a projection
#: that applied it to `.developer` alone would pass every case above.
NESTED_RESOURCE = Case(
    "nested_read_only_resource",
    resources=((("Docs"), _RW), ("Docs/nested", _RO)),
)


def _make_nested(world: dict[str, Path]) -> None:
    (world["mount"] / "Docs" / "nested").mkdir(parents=True, exist_ok=True)


PARITY_CASES = [*CASES, NESTED_RESOURCE]


def _prepare_for(case: Case) -> Callable[[dict[str, Path]], None] | None:
    return _make_nested if case is NESTED_RESOURCE else None


@pytest.fixture
def built(request, tmp_path, monkeypatch) -> Built:
    case = request.param
    return build_plan(
        case, tmp_path / "world", monkeypatch, prepare=_prepare_for(case),
    )


def _parametrize(fn):
    return pytest.mark.parametrize(
        "built", PARITY_CASES, ids=lambda c: c.name, indirect=True,
    )(fn)


# ------------------------------------------------- the plain reading of a plan


def _resolved(path: Path) -> Path | None:
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return None


def _mechanical(plan: MountPlan) -> list[tuple[Mount, set[tuple[str, Path]]]]:
    """What each mount contributes under the plainest possible rule.

    ``user_data`` binds only; resolve the source, skip it if it is not there,
    read-write to ``write``, read-only to ``read_only`` unless an earlier
    read-write root contains it, in which case ``write_denied`` — which is what
    bwrap's own ordering does to a ``--ro-bind`` landing under an earlier
    ``--bind``.

    Written from the *bind* side on purpose. It restates a rule
    ``project_fs_roots`` also states, and that is the shape of every parity
    assertion: the value is not that the rule is written once, it is that a
    bind reaching one consumer and not the other cannot pass.
    """
    out: list[tuple[Mount, set[tuple[str, Path]]]] = []
    rw_seen: list[Path] = []
    for mount in plan.mounts:
        if mount.mode not in ("ro", "rw") or not mount.user_data:
            out.append((mount, set()))
            continue
        assert mount.source is not None, mount.reason
        root = _resolved(mount.source)
        if root is None or not root.exists():
            out.append((mount, set()))
            continue
        if mount.mode == "rw":
            rw_seen.append(root)
            out.append((mount, {("write", root)}))
        elif any(root != seen and root.is_relative_to(seen) for seen in rw_seen):
            out.append((mount, {("write_denied", root)}))
        else:
            out.append((mount, {("read_only", root)}))
    return out


def _as_lists(roots: tuple[list[Path], list[Path], list[Path]]) -> dict[str, list[Path]]:
    """The projection's answer keyed the way ``_mechanical`` names its lists.

    ``read_roots`` is ``write + read_only``, so the read-only half is what is
    left after the write roots are taken out of it.
    """
    read_roots, write, denied = roots
    return {
        "write": write,
        "read_only": [p for p in read_roots if p not in write],
        "write_denied": denied,
    }


# ------------------------------------------------------------- direction one


@_parametrize
def test_every_user_data_bind_reaches_the_root_list_its_mode_earns(built: Built):
    """A bind in the plan is a root, in the right list. The assertion no test
    could express before the plan was data."""
    lists = _as_lists(built.roots)

    for mount, expected in _mechanical(built.plan):
        if mount.reason in DIVERGENCES:
            continue
        for name, path in expected:
            assert path in lists[name], (
                f"{mount.reason!r} is bound {mount.mode} at {path} and is user "
                f"data, but no {name} root covers it. A bind and its root are "
                f"one answer now; this is the drift ISSUE-319 and ISSUE-320 "
                f"each cost a filed bug to find."
            )


@_parametrize
def test_the_developer_carve_out_is_denied_whatever_the_directory_is(built: Built):
    """The first divergence, asserted rather than merely exempted."""
    dev = [m for m in built.plan.mounts if m.reason == "developer_dir"]
    assert len(dev) == 1, "the .developer entry is emitted unconditionally"
    entry = dev[0]
    _, write, denied = built.roots

    assert entry.source in denied, DIVERGENCES["developer_dir"]
    assert entry.source not in write, DIVERGENCES["developer_dir"]


@_parametrize
def test_the_package_cache_is_a_root_only_on_the_fallback_branch(built: Built):
    """The second divergence, asserted rather than merely exempted.

    Derived, the cache must still be *writable* — through the repos root that
    contains it, which is the whole argument for not giving it one of its own.
    """
    cache = [m for m in built.plan.mounts if m.reason == "package_cache"]
    if not cache:
        return
    _, write, _denied = built.roots
    root = _resolved(cache[0].source)
    assert root is not None

    if sandbox_cache_is_derived(built.config, built.task.user_id):
        assert root not in write, DIVERGENCES["package_cache"]
        assert any(root.is_relative_to(w) for w in write), (
            "the derived cache has no write root of its own and must therefore "
            "be inside one; if it is not, the bind is writable and the file "
            "tools cannot write it"
        )
    else:
        assert root in write, (
            "on the fallback branch the cache root is outside repos_dir and is "
            "bound into no other sandbox, so this entry is the only thing "
            "making the cache writable at all"
        )


# ------------------------------------------------------------- direction two


@_parametrize
def test_every_root_comes_from_a_user_data_bind(built: Built):
    """The reverse direction, so a root the projection invented fails too.

    A one-directional parity test is half a test: it catches a bind that lost
    its root and passes a root that answers to no bind, which is the shape a
    hand-written list decays into.
    """
    read_roots, write, denied = built.roots
    plan = built.plan

    rw_sources: dict[Path, str] = {}
    ro_sources: dict[Path, str] = {}
    rw_seen: list[Path] = []
    nested: dict[Path, str] = {}
    for mount in plan.mounts:
        if mount.mode not in ("ro", "rw") or not mount.user_data:
            continue
        root = _resolved(mount.source)
        if root is None or not root.exists():
            continue
        if mount.mode == "rw":
            rw_sources.setdefault(root, mount.reason)
            rw_seen.append(root)
        elif any(root != seen and root.is_relative_to(seen) for seen in rw_seen):
            nested.setdefault(root, mount.reason)
        else:
            ro_sources.setdefault(root, mount.reason)

    for path in write:
        assert path in rw_sources, (
            f"{path} is a write root and no read-write user-data bind in the "
            f"plan names it. Either the plan lost a bind or the projection "
            f"invented a root; both are the failure this file exists for."
        )
    for path in read_roots:
        assert path in rw_sources or path in ro_sources, (
            f"{path} is a read root and no user-data bind in the plan names it"
        )
    for path in denied:
        assert path in nested or any(
            m.always_deny and m.source == path for m in plan.mounts
        ), (
            f"{path} is a write-deny root and is neither an always_deny entry "
            f"nor a read-only bind nested inside an earlier read-write one. "
            f"Deny roots are derived by containment now; a hand-added one is "
            f"the thing that used to drift."
        )


@_parametrize
def test_no_mask_reaches_any_root(built: Built):
    """Rule 4: masks are not mounts and are not projected.

    A database directory arriving as a root would be the file tools reading
    what the namespace masks — the two consumers disagreeing about a boundary
    rather than about a convenience.
    """
    read_roots, write, denied = built.roots
    for mask in built.plan.masks:
        for path in [*read_roots, *write, *denied]:
            assert path != mask and not path.is_relative_to(mask), (
                f"{path} is a root and {mask} is a database mask above it"
            )


# ----------------------------------------------- the mask's protected paths


@_parametrize
def test_the_two_protected_derivations_agree(built: Built):
    """`mask_protected_paths` has two bodies and they must answer the same.

    The per-task one projects `Mount.protected` — `build_mount_plan` hands it
    the mounts it has just accumulated — and the taskless one, which `doctor`
    calls, names the paths itself. That is a second copy of a policy again, and
    the fact that these are the paths a *database mask* is refused against is
    what makes it worth an assertion rather than a comment: a `protected` flag
    dropped from a bind does not change one byte of argv on any golden case,
    because the refusal only fires where a db_path sits above that particular
    path. It goes wrong later, on a deployment nobody ran the suite on, by
    masking away the venv or the source tree and failing every task.

    Sets, not lists: the plan yields them in bind order (venv, source tree,
    workspace, REPL workspace) and the standing body in its own. `_mask_dir`
    reads the list for containment and for a log message, so only membership
    is load-bearing.
    """
    from istota.executor import mask_protected_paths

    with patch(
        "istota.executor._source_and_venv_paths",
        return_value=(built.world["src"], built.world["venv"]),
    ):
        from_plan = mask_protected_paths(
            built.config, plan_mounts=built.plan.mounts,
        )
        standing = mask_protected_paths(
            built.config,
            user_temp_dir=built.world["user_temp"],
            workspace_dir=built.plan.workspace_resolved,
        )

    assert set(from_plan) == set(standing), (
        "the plan-projected protected list and the standing derivation "
        "disagree. Either a bind that must not be masked away lost its "
        "`protected=True`, or the standing body names a path that is no "
        "longer bound."
    )
    assert len(from_plan) == len(set(from_plan)), (
        f"the projected list has a duplicate: {from_plan}"
    )


# ---------------------------------------------------- the divergences, exactly


def _divergent_reasons(built: Built) -> set[str]:
    """Reasons whose projected answer is not the plain reading of the plan."""
    lists = _as_lists(built.roots)
    predicted: dict[tuple[str, Path], set[str]] = {}
    for mount, expected in _mechanical(built.plan):
        for key in expected:
            predicted.setdefault(key, set()).add(mount.reason)

    divergent: set[str] = set()
    for (name, path), reasons in predicted.items():
        if path not in lists[name]:
            divergent |= reasons
    for name, values in lists.items():
        for path in values:
            if (name, path) in predicted:
                continue
            divergent |= {
                m.reason
                for m in built.plan.mounts
                if m.source is not None
                and (m.source == path or _resolved(m.source) == path)
            }
    return divergent


def test_the_divergence_list_is_exactly_what_diverges(tmp_path, monkeypatch):
    """Two detectors over the whole matrix, and their union must be the dict.

    They find different things and neither subsumes the other. The first is
    per-root: a reason whose contribution to the three lists is not what the
    plain reading predicts, which is how `.developer` shows up — carried when
    its directory is absent, where the plain rule contributes nothing. The
    second is per-flag: a reason whose `user_data` answer is not the same in
    every plan, which is how `package_cache` shows up and which no root-level
    comparison can see, since the derived branch marks it `user_data=False` and
    the plain reading then agrees with the projection about contributing
    nothing.

    Failing in both directions is the point. A third divergence of either kind
    lands here rather than being absorbed, and a named divergence that stopped
    diverging fails too, so the dict cannot rot into a list of names that once
    meant something.
    """
    by_root: set[str] = set()
    user_data_answers: dict[str, set[bool]] = {}

    for index, case in enumerate(PARITY_CASES):
        for profile in (SandboxProfile.CLAUDE, SandboxProfile.NATIVE):
            built = build_plan(
                case,
                tmp_path / f"w{index}-{profile.value}",
                monkeypatch,
                profile=profile,
                prepare=_prepare_for(case),
            )
            by_root |= _divergent_reasons(built)
            for mount in built.plan.mounts:
                if mount.mode in ("ro", "rw"):
                    user_data_answers.setdefault(mount.reason, set()).add(
                        mount.user_data
                    )

    by_flag = {r for r, answers in user_data_answers.items() if len(answers) > 1}

    assert by_root | by_flag == set(DIVERGENCES), (
        "the roots diverge from the plan somewhere DIVERGENCES does not name, "
        "or name a divergence that no longer happens.\n"
        f"  found by root comparison: {sorted(by_root)}\n"
        f"  found by user_data flag:  {sorted(by_flag)}\n"
        f"  documented:               {sorted(DIVERGENCES)}\n"
        + "\n".join(f"  {r}: {why}" for r, why in sorted(DIVERGENCES.items()))
    )
    # Each detector must still be the one that finds its own entry, so a
    # change that made both fire on one reason could not hide the loss of the
    # other.
    assert by_root == {"developer_dir"}, DIVERGENCES["developer_dir"]
    assert by_flag == {"package_cache"}, DIVERGENCES["package_cache"]


# --------------------------------------------- the public entry point agrees


@_parametrize
def test_native_fs_roots_is_this_projection(built: Built):
    """The delegation, pinned.

    Everything above asserts against `build_mount_plan` + `project_fs_roots`
    directly. That is only worth anything while the function `execute_task`
    actually calls returns the same triple, so this asserts the two are one
    answer rather than two that happen to agree today.
    """
    with patch(
        "istota.executor._source_and_venv_paths",
        return_value=(built.world["src"], built.world["venv"]),
    ):
        native = native_fs_roots(
            built.config,
            built.task,
            built.case.is_admin,
            built.resources,
            built.world["user_temp"],
            built.world["workspace"] if built.case.workspace else None,
        )
    plan = build_mount_plan(
        built.config,
        built.task,
        built.case.is_admin,
        built.resources,
        built.world["user_temp"],
        profile=SandboxProfile.NATIVE,
        workspace_dir=built.world["workspace"] if built.case.workspace else None,
    )
    assert native == project_fs_roots(plan, None)


def test_a_control_directory_adds_itself_and_nothing_else(tmp_path, monkeypatch):
    """`control_dir` is an argument rather than a plan entry, so direction two
    exempts it — which is only safe while it adds exactly itself."""
    control = tmp_path / "control" / "alice" / "task_1"
    control.mkdir(parents=True)
    built = build_plan(
        CASES_BY_NAME["claude_baseline"], tmp_path / "world", monkeypatch,
    )
    read_before, write_before, denied_before = built.roots
    read_after, write_after, denied_after = project_fs_roots(built.plan, control)

    assert write_after == write_before
    assert denied_after == [*denied_before, control.resolve()]
    assert set(read_after) - set(read_before) == {control.resolve()}


# ------------------------------------------- the cache's ancestor check, too


#: Every ``_sandbox_bind_targets`` entry that is deliberately *broader* than
#: any single bind, with the reason. Keyed by a label rather than by a path,
#: because four of the seven are config-derived and one is per-user.
#:
#: A literal again, and for the same reason as ``DIVERGENCES``: a target that
#: covers no bind at all is the ISSUE-319 shape — a list entry that reads like
#: a boundary and refuses nothing — and the only thing that catches it is a
#: test that refuses to accept a new name silently.
DELIBERATELY_BROADER: dict[str, str] = {
    "/": (
        "the filesystem root. The rule is equal-or-ancestor, so this refuses a "
        "cache at / and nothing else could"
    ),
    "/etc": (
        "the /etc allowlist is bound file by file (ssl, resolv.conf, passwd, "
        "alternatives, …) and the directory itself is bound at no path"
    ),
    "/tmp": (
        "the namespace's own tmpfs, emitted inside the --unshare-pid flag "
        "group rather than as a bind"
    ),
    "temp_dir": (
        "every user's task workspace. The plan binds one user's "
        "{temp_dir}/{user_id}; a cache above the parent would cover all of "
        "them, and the .developer credential helpers with them"
    ),
    "home/.local": (
        "the Claude runtime block. bin/, share/claude and state/claude are "
        "bound; the directory holding them is not, and a cache over it would "
        "give the model write access to the claude binary the daemon spawns"
    ),
    "developer.repos_dir": (
        "the *global* repos root. The plan binds one user's "
        "{repos_dir}/{user_id}, and the root is the stricter test on purpose: "
        "a cache at or above it would cover every user's subtree at once"
    ),
    "nextcloud_mount_path": (
        "the mount root. The plan binds Users/{user_id}, Talk and "
        "Channels/{token} beneath it, never the root"
    ),
}

#: The one target that can name a path with nothing on disk behind it, so no
#: bind could name it either. Bounded here so the escape hatch cannot widen.
MAY_BE_ABSENT = {"home/.claude"}


def _labelled_targets(config, home: Path) -> dict[Path, str]:
    labels = {
        Path("/"): "/",
        Path("/usr"): "/usr",
        Path("/etc"): "/etc",
        Path("/tmp"): "/tmp",
        Path(config.temp_dir): "temp_dir",
        home / ".local": "home/.local",
        home / ".claude": "home/.claude",
        home / ".cache" / "huggingface": "home/.cache/huggingface",
    }
    if config.developer.repos_dir:
        labels[Path(config.developer.repos_dir)] = "developer.repos_dir"
    if config.nextcloud_mount_path:
        labels[Path(config.nextcloud_mount_path)] = "nextcloud_mount_path"
    for ro_path in config.security.sandbox_ro_paths:
        labels[Path(ro_path)] = "sandbox_ro_paths"
    sp_path = custom_system_prompt_path(config)
    if sp_path is not None:
        labels[sp_path] = "custom_system_prompt"
    return labels


def _destinations(plan: MountPlan) -> set[Path]:
    """Every path the render names as a destination inside the namespace.

    The same expressions ``render_bwrap_argv`` uses, and the existence gate
    left off: a bind whose source is momentarily absent still describes a path
    the plan is willing to mount, which is the question this list answers.
    """
    out: set[Path] = set()
    for mount in plan.mounts:
        if mount.mode == "flag":
            continue
        if mount.mode == "symlink":
            out.add(Path(str(mount.dest)))
            continue
        assert mount.source is not None, mount.reason
        if mount.mode == "tmpfs":
            resolved = _resolved(mount.source)
            if resolved is not None:
                out.add(resolved)
            continue
        if mount.dest is not None:
            resolved = _resolved(mount.dest)
            if resolved is not None:
                out.add(resolved)
        else:
            out.add(mount.source)
    return out


class TestTheCacheAncestorList:
    """`_sandbox_bind_targets` stays hand-written; this is its coverage test.

    It cannot become a projection — it is called from inside
    `resolve_sandbox_cache_dir`, which `build_mount_plan` itself calls, so
    projecting it is an import cycle (the spec's Decisions section settles
    this). What is available is the half that matters: every path it names is a
    path the plan actually mounts, or is deliberately broader than one and says
    so here. ISSUE-319 was an entry on this list that was *also* the documented
    home for the cache; an entry naming a path nothing mounts is the same
    failure with nothing to detect it.
    """

    @pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
    def test_every_target_covers_something_the_plan_mounts(
        self, case, tmp_path, monkeypatch,
    ):
        built = build_plan(case, tmp_path / "world", monkeypatch,
                           profile=SandboxProfile.CLAUDE)
        native = other_profile_plan(built, SandboxProfile.NATIVE)
        # Both profiles, because this function has no profile argument: the
        # custom system prompt is a CLAUDE-only bind and would read as an
        # uncovered target under NATIVE alone.
        destinations = _destinations(built.plan) | _destinations(native)
        home = Path(str(built.world["home"]))
        labels = _labelled_targets(built.config, home)

        for target in _sandbox_bind_targets(built.config):
            label = labels.get(target)
            assert label is not None, (
                f"{target} is a cache-ancestor target this test does not "
                f"recognise. Give it a label in `_labelled_targets`, and if it "
                f"is broader than any single bind, a reason in "
                f"`DELIBERATELY_BROADER`."
            )
            if label in DELIBERATELY_BROADER:
                continue
            if label in MAY_BE_ABSENT and not target.exists():
                continue
            assert target in destinations, (
                f"{target} ({label}) is on the cache-ancestor list and the "
                f"plan mounts nothing at it. Either it is dead — the ISSUE-319 "
                f"shape, a list entry that reads like a boundary and refuses "
                f"nothing — or it is deliberately broader than a bind and "
                f"belongs in DELIBERATELY_BROADER with its reason."
            )

    def test_every_documented_broader_entry_is_actually_reached(
        self, tmp_path, monkeypatch,
    ):
        """The exemption list must not rot into names that mean nothing.

        Each entry has to be produced by some case in the matrix and has to be
        genuinely broader there — a strict ancestor of a destination, never an
        exact one, since an exact match would mean the exemption is now hiding
        a covered target rather than explaining an uncovered one.
        """
        reached: set[str] = set()
        for index, case in enumerate(CASES):
            built = build_plan(case, tmp_path / f"c{index}", monkeypatch,
                               profile=SandboxProfile.CLAUDE)
            destinations = _destinations(built.plan) | _destinations(
                other_profile_plan(built, SandboxProfile.NATIVE)
            )
            labels = _labelled_targets(built.config, Path(str(built.world["home"])))
            for target in _sandbox_bind_targets(built.config):
                label = labels.get(target)
                if label not in DELIBERATELY_BROADER:
                    continue
                assert target not in destinations, (
                    f"{target} ({label}) is exempted as broader than any bind "
                    f"and is an exact mount destination in {case.name!r}. The "
                    f"exemption is hiding a covered target now; drop it."
                )
                reached.add(label)

        assert reached == set(DELIBERATELY_BROADER), (
            "a documented broader entry is produced by no case in the matrix: "
            f"{sorted(set(DELIBERATELY_BROADER) - reached)}"
        )


def test_the_extra_parity_case_is_not_a_golden(tmp_path, monkeypatch):
    """`NESTED_RESOURCE` exists only here, and must stay that way.

    `tests/test_sandbox_argv_golden.py::test_every_golden_file_belongs_to_a_case`
    requires a golden per case in `CASES`, so a case that leaked into that list
    would need a golden generated for it — and a golden written to satisfy a
    test is not a before-picture of anything.
    """
    assert NESTED_RESOURCE not in CASES
    assert NESTED_RESOURCE.name not in CASES_BY_NAME

    built = build_plan(
        NESTED_RESOURCE, tmp_path / "world", monkeypatch, prepare=_make_nested,
    )
    _, write, denied = built.roots
    nested = (built.world["mount"] / "Docs" / "nested").resolve()
    assert (built.world["mount"] / "Docs").resolve() in write
    assert nested in denied, (
        "a read-only resource nested inside an earlier read-write one is a "
        "write-deny root, which is what bwrap's ordering already makes it"
    )
    assert nested not in write
