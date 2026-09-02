"""Goldens over the exact argv ``build_bwrap_cmd`` emits.

The sandbox's whole behaviour is an ordered list of strings handed to
``bwrap``, and ordering is load-bearing in four documented places (the cache
bind before the repos bind, the ``.developer`` re-bind after ``user_temp_dir``,
``extra_ro_binds`` after every bind, the database masks last). Substring
assertions cannot see any of that: they pass just as happily when a bind moves
above the one that was supposed to cover it. Snapshotting the whole argv across
a matrix is what makes a *refactor* of the builder verifiable rather than
merely plausible — the sandbox-mount-plan-as-data spec moves those 600 lines
into a data structure, and these files are the before picture.

An intentional change is a reviewed golden update::

    uv run env ISTOTA_UPDATE_GOLDEN=1 pytest tests/test_sandbox_argv_golden.py -n0

``env`` goes *inside* the ``uv run`` and ``-n0`` is not optional; both are the
convention ``tests/test_prompt_golden.py`` established and its module docstring
explains each. The switch is shared with that file deliberately — one
regeneration verb for every golden in the tree.

**What makes a machine-portable golden out of a host-specific argv.** Four
things reach the argv from outside the config, and each is neutralised rather
than left to differ between a laptop and a Linux runner:

* Every path the config names is derived from ``tmp_path.resolve()`` and
  substituted back out through a table this module owns. Resolving the root
  first is not tidiness: ``_bind`` emits ``src.resolve()`` as the source and the
  path *as written* as the destination, and on macOS an unresolved
  ``/var/folders/...`` root would make those two strings differ where on Linux
  they do not — and ``_mask_dir`` would emit two mask entries where Linux emits
  one, which no string substitution can fix.
* ``$HOME`` is a directory under that root, so the Claude runtime block is
  built rather than discovered.
* ``_source_and_venv_paths`` is patched to two directories under the root. It
  is the seam the product already extracted for its two callers, so patching it
  moves the binds and ``mask_protected_paths`` together, and it removes the one
  case that would otherwise change the *shape* of the argv — a non-venv
  interpreter, where the venv path does not exist and ``_ro_bind`` silently
  emits nothing.
* ``_bwrap_available`` and the three flag probes are patched to fixed answers
  per case, so ``--disable-userns``, ``--unshare-user`` and ``--remount-ro``
  are matrix axes rather than facts about whoever ran the suite.

**One thing is collapsed rather than normalised, and it is a real hole.** The
``/usr`` bind, the merged-usr compat symlinks and the ``/etc`` allowlist are
host state: Debian has ``/bin`` as a symlink and ``/etc/ld.so.cache``, macOS
has neither, so the entries differ in *presence*, not just in spelling.
Everything from after ``bwrap`` up to ``--unshare-pid`` is therefore replaced
by a single token, and ``test_the_host_system_prologue_has_the_documented_shape``
pins its order and membership on whatever host is running instead. That test is
the golden for that slice; the golden proper still pins that the slice comes
first and that nothing moved across its boundary.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

import pytest

from istota import db
from istota.config import (
    Config,
    ContainerConfig,
    DeveloperConfig,
    DevboxConfig,
    SecurityConfig,
)
from istota.executor import SandboxProfile, build_bwrap_cmd

GOLDEN_DIR = Path(__file__).parent / "golden" / "sandbox"
UPDATE_ENV = "ISTOTA_UPDATE_GOLDEN"
UPDATE_CMD = f"uv run env {UPDATE_ENV}=1 pytest tests/test_sandbox_argv_golden.py -n0"

#: What the host-specific prologue collapses to. Not a path, and deliberately
#: not starting with `--`, so the renderer keeps it on `bwrap`'s own line.
HOST_SYSTEM_TOKEN = "<HOST-SYSTEM-BINDS>"

#: The command being wrapped. Fixed, because what is under test is the wrapper.
CMD = ["claude", "-p", "test"]


def updating() -> bool:
    """Whether this run rewrites the goldens instead of comparing.

    Deliberately not ``bool(os.environ.get(...))``: the variable comes from the
    ambient environment, so ``ISTOTA_UPDATE_GOLDEN=0`` left exported in a shell
    would turn every golden here into a rubber stamp. Same affirmative/negative
    sets as ``tests/test_prompt_golden.py`` and ``PRECOMMIT_SCANS_REQUIRED``;
    anything else raises rather than being guessed at.
    """
    raw = os.environ.get(UPDATE_ENV)
    if raw is None or raw == "":
        return False
    value = raw.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise RuntimeError(
        f"{UPDATE_ENV}={raw!r} is neither affirmative nor negative. Run "
        f"`{UPDATE_CMD}` to rewrite the goldens, or unset it to compare."
    )


# ----------------------------------------------------------------- the matrix


@dataclass(frozen=True)
class Case:
    """One (config shape, task shape, profile) the argv is snapshotted for.

    Every field is an axis the Design section names, or a branch inside
    ``build_bwrap_cmd`` that no config axis reaches on its own (the mask
    refusal, the module-root ``ValueError``, the absent ``.developer``).
    """

    name: str
    profile: SandboxProfile = SandboxProfile.CLAUDE
    is_admin: bool = True
    #: Empty means `Config.is_admin` answers True for everyone, which is what
    #: `sandbox_cache_is_derived` reads — the `is_admin` argument above does
    #: not reach it.
    admin_users: tuple[str, ...] = ()
    user_id: str = "alice"
    conversation_token: str = "room123"

    developer_enabled: bool = False
    repos_dir: bool = False
    devbox_enabled: bool = False
    authorized_skills: tuple[str, ...] = ()

    workspace: bool = False
    resources: tuple[tuple[str, str], ...] = ()
    proxy_sock: bool = False
    net_proxy_sock: bool = False
    extra_ro_binds: tuple[str, ...] = ()
    ro_paths: tuple[str, ...] = ()
    custom_system_prompt: bool = False
    cache_dir: bool = False

    #: Relative to the world root, or the literal "mount/modules" to make
    #: `module_db_root()` raise. None keeps the derived `{db_dir}/modules`.
    module_data_dir: str | None = None
    #: Where the framework DB lives, relative to the world root. "temp" puts it
    #: above the task workspace, which is what a mask refusal needs.
    db_dir: str = "data"

    developer_dir: bool = True
    users_config_dir: bool = False
    claude_home: bool = True

    bwrap_available: bool = True
    disable_userns: bool = True
    remount_ro: bool = True
    requires_unshare_user: bool = False

    axes: frozenset[str] = field(default_factory=frozenset)


_RW = "readwrite"
_RO = "read"

CASES: list[Case] = [
    Case("claude_baseline"),
    Case("native_baseline", profile=SandboxProfile.NATIVE),
    Case("claude_without_a_claude_home", claude_home=False),
    Case("no_conversation_token", conversation_token=""),
    Case(
        "non_admin_with_developer_configured",
        is_admin=False,
        admin_users=("bob",),
        developer_enabled=True,
        repos_dir=True,
        cache_dir=True,
    ),
    Case(
        "admin_developer_repos",
        developer_enabled=True,
        repos_dir=True,
    ),
    Case(
        "admin_developer_repos_native",
        profile=SandboxProfile.NATIVE,
        developer_enabled=True,
        repos_dir=True,
    ),
    Case(
        "developer_enabled_without_repos_dir",
        developer_enabled=True,
        cache_dir=True,
    ),
    Case(
        "developer_disabled_with_repos_dir",
        developer_enabled=False,
        repos_dir=True,
        cache_dir=True,
    ),
    Case("configured_cache_only", cache_dir=True),
    Case(
        "devbox_developer_authorized",
        developer_enabled=True,
        repos_dir=True,
        devbox_enabled=True,
        authorized_skills=("developer",),
    ),
    Case(
        "devbox_developer_not_authorized",
        developer_enabled=True,
        repos_dir=True,
        devbox_enabled=True,
        authorized_skills=("email", "browse"),
    ),
    Case(
        "devbox_enabled_developer_off",
        devbox_enabled=True,
        authorized_skills=("developer",),
    ),
    Case("repl_workspace", workspace=True),
    Case(
        "resources_read_and_readwrite",
        resources=(
            ("Docs", _RW),
            ("Notes", _RO),
            ("Users/alice/inside", _RW),
        ),
    ),
    Case("sockets_and_network", proxy_sock=True, net_proxy_sock=True),
    Case("network_without_a_live_socket", net_proxy_sock=True, developer_dir=False),
    Case("extra_ro_binds_present_and_absent", extra_ro_binds=("doc.pdf", "gone.pdf")),
    Case("custom_system_prompt_claude", custom_system_prompt=True),
    Case(
        "custom_system_prompt_native",
        profile=SandboxProfile.NATIVE,
        custom_system_prompt=True,
    ),
    Case("sandbox_ro_paths", ro_paths=("ro",)),
    Case("users_config_dir_masked", users_config_dir=True),
    Case("module_root_outside_the_db_dir", module_data_dir="modules"),
    Case("module_root_under_the_mount", module_data_dir="mount/modules"),
    Case("db_mask_refused_above_the_workspace", db_dir="temp"),
    Case("no_developer_dir", developer_dir=False),
    Case("probes_all_off", disable_userns=False, remount_ro=False),
    Case(
        "probes_unshare_user_only",
        disable_userns=False,
        requires_unshare_user=True,
    ),
    Case("bwrap_unavailable", bwrap_available=False),
    Case(
        "everything_at_once",
        developer_enabled=True,
        repos_dir=True,
        devbox_enabled=True,
        authorized_skills=("developer",),
        workspace=True,
        resources=(("Docs", _RW), ("Notes", _RO)),
        proxy_sock=True,
        net_proxy_sock=True,
        extra_ro_binds=("doc.pdf",),
        ro_paths=("ro",),
        custom_system_prompt=True,
        module_data_dir="modules",
    ),
]

CASES_BY_NAME = {c.name: c for c in CASES}
assert len(CASES_BY_NAME) == len(CASES), "duplicate case name"


# ------------------------------------------------------------------ the world


def _make_world(root: Path, case: Case) -> dict[str, Path]:
    """Everything on disk the argv can name, under one resolved root.

    Built rather than discovered, so what exists is a property of the case and
    not of the machine. ``_ro_bind`` and ``_bind`` skip a source that does not
    exist, so a directory missing here is an entry missing from the argv.
    """
    home = root / "home"
    (home / ".local" / "bin").mkdir(parents=True)
    (home / ".local" / "share" / "claude").mkdir(parents=True)
    (home / ".local" / "state" / "claude").mkdir(parents=True)
    (home / ".cache" / "huggingface").mkdir(parents=True)
    if case.claude_home:
        claude = home / ".claude"
        claude.mkdir()
        (claude / ".credentials.json").write_text("{}")
        (claude / "settings.json").write_text("{}")
        for sub in ("projects", "debug", "todos"):
            (claude / sub).mkdir()

    src = root / "src"
    src.mkdir()
    if case.users_config_dir:
        (src / "config" / "users").mkdir(parents=True)
    venv = root / "venv"
    venv.mkdir()

    mount = root / "mount"
    for rel in ("Users/alice", "Users/bob", "Talk", "Channels/room123", "Docs", "Notes"):
        (mount / rel).mkdir(parents=True)
    (mount / "Users" / "alice" / "inside").mkdir()

    db_dir = root / case.db_dir
    db_dir.mkdir(parents=True, exist_ok=True)

    temp = root / "temp"
    user_temp = temp / case.user_id if case.user_id else temp
    user_temp.mkdir(parents=True, exist_ok=True)
    if case.developer_dir:
        (user_temp / ".developer").mkdir(exist_ok=True)

    repos = root / "repos"
    for user in ("alice", "bob"):
        (repos / user / "acme" / "widget.git").mkdir(parents=True)

    exec_root = root / "run" / "istota-exec"
    for user in ("alice", "bob"):
        (exec_root / user).mkdir(parents=True)

    (root / "workspace").mkdir()
    (root / "cache").mkdir()
    (root / "ro").mkdir()
    (root / "config" / "skills").mkdir(parents=True)
    (root / "config" / "system-prompt.md").write_text("system\n")
    sockets = root / "sockets"
    sockets.mkdir()
    (sockets / "proxy.sock").touch()
    (sockets / "net.sock").touch()
    extra = root / "extra"
    extra.mkdir()
    (extra / "doc.pdf").write_text("doc\n")

    return {
        "home": home,
        "src": src,
        "venv": venv,
        "mount": mount,
        "db_dir": db_dir,
        "user_temp": user_temp,
        "repos": repos,
        "exec_root": exec_root,
    }


def _make_config(root: Path, case: Case, world: dict[str, Path]) -> Config:
    if case.module_data_dir is None:
        module_data_dir = None
    else:
        module_data_dir = root / case.module_data_dir
        module_data_dir.mkdir(parents=True, exist_ok=True)

    return Config(
        db_path=world["db_dir"] / "istota.db",
        temp_dir=root / "temp",
        nextcloud_mount_path=world["mount"],
        skills_dir=root / "config" / "skills",
        module_data_dir=module_data_dir,
        custom_system_prompt=case.custom_system_prompt,
        admin_users=set(case.admin_users),
        security=SecurityConfig(
            sandbox_enabled=True,
            sandbox_cache_dir=str(root / "cache") if case.cache_dir else "",
            sandbox_ro_paths=[str(root / p) for p in case.ro_paths],
        ),
        developer=DeveloperConfig(
            enabled=case.developer_enabled,
            repos_dir=str(world["repos"]) if case.repos_dir else "",
            container=ContainerConfig(exec_socket_dir=str(world["exec_root"])),
        ),
        devbox=DevboxConfig(enabled=case.devbox_enabled),
    )


def build_argv(case: Case, root: Path, monkeypatch) -> list[str]:
    """The argv for one case, normalised. Nothing here reaches the network."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    world = _make_world(root, case)
    config = _make_config(root, case, world)

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

    with (
        patch("istota.executor._bwrap_available", return_value=case.bwrap_available),
        patch(
            "istota.executor._source_and_venv_paths",
            return_value=(world["src"], world["venv"]),
        ),
        patch(
            "istota.executor._bwrap_supports_disable_userns",
            return_value=case.disable_userns,
        ),
        patch(
            "istota.executor._bwrap_supports_remount_ro", return_value=case.remount_ro
        ),
        patch(
            "istota.executor._bwrap_requires_unshare_user",
            return_value=case.requires_unshare_user,
        ),
    ):
        argv = build_bwrap_cmd(
            list(CMD),
            config,
            task,
            case.is_admin,
            resources,
            world["user_temp"],
            proxy_sock=root / "sockets" / "proxy.sock" if case.proxy_sock else None,
            net_proxy_sock=(
                root / "sockets" / "net.sock" if case.net_proxy_sock else None
            ),
            extra_ro_binds=[root / "extra" / name for name in case.extra_ro_binds],
            authorized_skills=frozenset(case.authorized_skills),
            workspace_dir=root / "workspace" if case.workspace else None,
            profile=case.profile,
        )

    return normalize(argv, root, world)


# ----------------------------------------------------------- the substitutions


def normalize(argv: list[str], root: Path, world: dict[str, Path]) -> list[str]:
    """Run-specific paths out, the host-specific prologue collapsed.

    Longest replacement first, because ``$HOME``, the source tree and the venv
    all live under the world root and a shorter match would eat their prefix.
    """
    table = [
        (str(world["home"]), "<HOME>"),
        (str(world["src"]), "<SRC>"),
        (str(world["venv"]), "<VENV>"),
        (str(root), "<TMP>"),
    ]
    table.sort(key=lambda pair: len(pair[0]), reverse=True)

    collapsed = _collapse_host_system(argv)
    out = []
    for token in collapsed:
        for literal, placeholder in table:
            token = token.replace(literal, placeholder)
        out.append(token)
    return out


def _collapse_host_system(argv: list[str]) -> list[str]:
    """Replace ``/usr`` + merged-usr compat + ``/etc`` with one token."""
    if "--unshare-pid" not in argv:
        # bwrap unavailable: the argv is the command, untouched.
        return list(argv)
    index = argv.index("--unshare-pid")
    return argv[:1] + [HOST_SYSTEM_TOKEN] + argv[index:]


def host_system_prologue(argv: list[str]) -> list[str]:
    """The slice ``_collapse_host_system`` throws away, for the shape test."""
    return argv[1 : argv.index("--unshare-pid")]


# --------------------------------------------------------------- the rendering


def render(argv: list[str]) -> str:
    """One line per flag, arguments tab-joined onto it.

    A flag-per-line rendering is what makes a golden diff readable — a token
    per line turns a moved bind into a six-line diff with no shape to it. The
    grouping rule is generic (a token starting with ``--`` opens a line) rather
    than a table of arities, so it cannot silently regroup an argv whose shape
    changed, and ``test_the_rendering_round_trips`` holds it lossless.
    """
    lines: list[list[str]] = []
    for token in argv:
        if not lines or token.startswith("--"):
            lines.append([token])
        else:
            lines[-1].append(token)
    return "".join("\t".join(line) + "\n" for line in lines)


def parse(text: str) -> list[str]:
    argv: list[str] = []
    for line in text.splitlines():
        argv.extend(line.split("\t"))
    return argv


# ------------------------------------------------------------------- the tests


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_sandbox_argv_golden(case, tmp_path, monkeypatch):
    rendered = render(build_argv(case, tmp_path / "world", monkeypatch))
    golden = GOLDEN_DIR / f"{case.name}.txt"

    if updating():
        golden.parent.mkdir(parents=True, exist_ok=True)
        changed = not golden.exists() or golden.read_text(encoding="utf-8") != rendered
        golden.write_text(rendered, encoding="utf-8")
        if changed:
            warnings.warn(
                f"{UPDATE_ENV}: rewrote {golden.name} — review the diff",
                stacklevel=1,
            )
        return

    assert golden.exists(), (
        f"no golden for case {case.name!r}. Generate it with `{UPDATE_CMD}` and "
        "review the result like any other change."
    )
    assert rendered == golden.read_text(encoding="utf-8"), (
        f"the sandbox argv for {case.name!r} differs from "
        f"{golden.relative_to(GOLDEN_DIR.parent.parent)}. This is a pure-refactor "
        f"tripwire: if the change is intended, regenerate with `{UPDATE_CMD}` and "
        "review the diff."
    )


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_the_rendering_round_trips(case, tmp_path, monkeypatch):
    """The golden is the argv, not a summary of it.

    The line grouping is only safe while no argument starts with ``--`` and no
    token carries a tab or a newline. Asserting the round trip states that
    directly instead of trusting it: a future bind whose path breaks either
    rule shows up here rather than as a golden that quietly means something
    else.
    """
    argv = build_argv(case, tmp_path / "world", monkeypatch)
    assert parse(render(argv)) == argv


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
def test_normalization_is_total(case, tmp_path, monkeypatch):
    """Two builds of one case, under two roots, must be the same text.

    The tempting version of this — build once and assert the temp path is
    absent — asserts back exactly the substitutions ``normalize`` has just
    made, so it can only catch a value it already knows about. Building twice
    states the property: a second root moves every path, and anything
    run-specific that the table does not cover comes out as an inequality.
    """
    first = build_argv(case, tmp_path / "first", monkeypatch)
    second = build_argv(case, tmp_path / "second", monkeypatch)

    assert first == second, (
        "the same case built under two roots does not normalize to the same "
        "argv, so something run-specific is reaching the golden"
    )
    # Platform-neutral backstop for a temp path in a form neither the written
    # nor the resolved root matches.
    text = "\n".join(first)
    assert "pytest-of-" not in text
    assert "/var/folders/" not in text
    assert str(tmp_path) not in text


def test_the_host_system_prologue_has_the_documented_shape(tmp_path, monkeypatch):
    """The slice the golden collapses, pinned on whatever host is running.

    ``/usr`` first, then the merged-usr compat entries, then the ``/etc``
    allowlist — each in the order the source emits them, each optional because
    presence is host state. Order and membership are what the golden cannot
    hold, so they are held here; a bind added to that block without a thought
    about ordering fails this rather than passing everywhere.
    """
    argv = build_argv(CASES_BY_NAME["claude_baseline"], tmp_path / "world", monkeypatch)

    # The golden's first line proves the collapse happened at all.
    assert argv[:2] == ["bwrap", HOST_SYSTEM_TOKEN]

    raw = build_raw_argv(CASES_BY_NAME["claude_baseline"], tmp_path / "raw", monkeypatch)
    prologue = host_system_prologue(raw)

    assert prologue[:3] == ["--ro-bind", "/usr", "/usr"], (
        f"/usr is not the first system bind: {prologue[:6]}"
    )

    compat = ["/bin", "/lib", "/lib64", "/sbin"]
    etc = [
        "/etc/ssl", "/etc/ca-certificates", "/etc/resolv.conf",
        "/etc/hosts", "/etc/nsswitch.conf", "/etc/ld.so.cache",
        "/etc/localtime", "/etc/passwd", "/etc/group",
        "/etc/alternatives",
    ]
    expected_order = compat + etc

    # Every entry after /usr is a three-token bind or symlink whose *last*
    # token is a destination from the list above, and the destinations appear
    # in the list's own order.
    seen: list[str] = []
    index = 3
    while index < len(prologue):
        verb = prologue[index]
        assert verb in ("--ro-bind", "--symlink"), (
            f"unexpected verb {verb!r} in the system prologue: {prologue[index:]}"
        )
        dest = prologue[index + 2]
        assert dest in expected_order, f"unexpected system mount at {dest!r}"
        seen.append(dest)
        index += 3

    assert index == len(prologue), "the prologue does not divide into triples"
    assert seen == [p for p in expected_order if p in seen], (
        f"the system binds are out of order: {seen}"
    )
    assert "/etc/hosts" in seen, "no /etc bind at all — the probe patch is wrong"


def build_raw_argv(case: Case, root: Path, monkeypatch) -> list[str]:
    """``build_argv`` without the normalisation, for the prologue test alone."""
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    world = _make_world(root, case)
    config = _make_config(root, case, world)
    monkeypatch.setenv("HOME", str(world["home"]))
    task = db.Task(
        id=1, prompt="test", user_id=case.user_id, source_type="talk",
        status="running", conversation_token=case.conversation_token,
    )
    with (
        patch("istota.executor._bwrap_available", return_value=True),
        patch(
            "istota.executor._source_and_venv_paths",
            return_value=(world["src"], world["venv"]),
        ),
        patch("istota.executor._bwrap_supports_disable_userns", return_value=True),
        patch("istota.executor._bwrap_supports_remount_ro", return_value=True),
        patch("istota.executor._bwrap_requires_unshare_user", return_value=False),
    ):
        return build_bwrap_cmd(
            list(CMD), config, task, case.is_admin, [], world["user_temp"],
            profile=case.profile,
        )


def test_every_golden_file_belongs_to_a_case():
    """A renamed case must not leave a stale file behind.

    A golden nothing reads is worse than no golden: it looks like coverage in a
    directory listing and asserts nothing.
    """
    if updating():
        pytest.skip(
            "mid-regeneration: under xdist this test has no ordering "
            "relationship with the writers, so it would race them"
        )
    if not GOLDEN_DIR.exists():
        pytest.skip("no goldens generated yet")
    on_disk = {p.stem for p in GOLDEN_DIR.glob("*.txt")}
    assert on_disk == set(CASES_BY_NAME), (
        f"orphaned goldens: {sorted(on_disk - set(CASES_BY_NAME))}; "
        f"missing goldens: {sorted(set(CASES_BY_NAME) - on_disk)}"
    )


#: Each axis the spec's Design section names, as a predicate over a case. A
#: matrix is a claim about coverage, and a claim nothing checks decays: this
#: turns "covers sandbox on and off" into something that fails when the case
#: carrying one side is deleted or edited away.
AXES = {
    "bwrap available": lambda c: c.bwrap_available,
    "admin": lambda c: c.is_admin,
    "developer.enabled": lambda c: c.developer_enabled,
    "developer.repos_dir": lambda c: c.repos_dir,
    "developer enabled with a repos_dir": lambda c: c.developer_enabled and c.repos_dir,
    "devbox configured": lambda c: c.devbox_enabled,
    "devbox reachable by the task": lambda c: (
        c.devbox_enabled and c.developer_enabled and c.repos_dir
        and "developer" in c.authorized_skills
    ),
    "REPL workspace": lambda c: c.workspace,
    "conversation token": lambda c: bool(c.conversation_token),
    "read-only resource": lambda c: any(p == _RO for _, p in c.resources),
    "read-write resource": lambda c: any(p == _RW for _, p in c.resources),
    "NATIVE profile": lambda c: c.profile is SandboxProfile.NATIVE,
    "network isolation": lambda c: c.net_proxy_sock,
    "extra read-only binds": lambda c: bool(c.extra_ro_binds),
    "custom system prompt": lambda c: c.custom_system_prompt,
    "configured cache root": lambda c: c.cache_dir,
    "--disable-userns": lambda c: c.disable_userns,
    "--remount-ro": lambda c: c.remount_ro,
    ".developer directory": lambda c: c.developer_dir,
}


@pytest.mark.parametrize("axis", sorted(AXES), ids=lambda a: a.replace(" ", "_"))
def test_the_matrix_covers_both_sides_of_every_axis(axis):
    predicate = AXES[axis]
    on = [c.name for c in CASES if predicate(c)]
    off = [c.name for c in CASES if not predicate(c)]
    assert on, f"no case exercises {axis!r}"
    assert off, f"no case exercises the absence of {axis!r}"
