"""The sandbox mount plan, as data.

`build_bwrap_cmd` used to decide every bind, mask and namespace flag by
appending strings to an argv list in statement order, which meant the plan
existed only as the argv it had already become. Four other consumers need to
ask questions of that plan — the native brain's file-tool roots, the cache
bind's ancestor check, the mask's protected paths, and `ToolEnv` behind them —
and none of those questions can be put to a list of strings, so each consumer
restated a slice of the policy in its own shape. ISSUE-319 and ISSUE-320 were
both one copy disagreeing with another.

So the decision and the formatting are two functions now.
:func:`build_mount_plan` answers *what is mounted, where, and in which order*;
:func:`render_bwrap_argv` turns that answer into bwrap's argv and nothing else.
The argv is byte-for-byte what the single function produced —
``tests/test_sandbox_argv_golden.py`` snapshots it across a 30-case matrix and
is the tripwire for that claim.

**Ordering is list order, deliberately.** It is load-bearing in four places for
four unrelated reasons (the cache bind before the repos bind so `link(2)` sees
one mount, the ``.developer`` read-only re-bind after ``user_temp_dir``,
``extra_ro_binds`` after every bind so nothing buries them, the masks last so
nothing shows through them). A priority number would encode none of those, so
the plan is read top to bottom exactly as the argv is.

**The import direction is one-way.** ``executor`` imports this module at module
scope, to re-export :class:`SandboxProfile` for its five import sites. This
module therefore imports ``executor`` inside functions only. A module-scope
``from .executor import ...`` here is an import cycle.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from . import db
    from .config import Config

logger = logging.getLogger(__name__)

Mode = Literal["ro", "rw", "tmpfs", "symlink", "flag"]

#: ``Mount.reason`` for a caller-supplied ``extra_ro_binds`` entry. The render
#: logs an absent one rather than skipping it silently, because that entry is a
#: boundary and its two callers lose different things by it — see the comment
#: at the emission site in :func:`build_mount_plan`.
EXTRA_RO_BIND = "extra_ro_bind"


@dataclass(frozen=True)
class Mount:
    """One entry in the plan: a bind, a tmpfs, a symlink or a raw flag.

    ``source`` is the path *as written*, never pre-resolved, because the render
    emits ``source.resolve()`` as bwrap's source and the written string as
    bwrap's destination. Under a symlinked deployment root those are two
    different strings, and three consumers depend on the difference — see
    ``tests/test_sandbox_argv_golden.py``'s ``symlinked_deployment_root`` case.

    A source that does not exist is carried on the plan and skipped by the
    render rather than omitted here, because a projection needs to see an entry
    whose source is momentarily absent. The cost is that the check and the
    emission are no longer adjacent: everything the builder does — including
    ``resolve_sandbox_cache_dir``'s ``mkdir`` — now happens before the first
    ``exists()`` runs. One reachable spelling changes because of it, a REPL
    ``--workspace`` naming a directory that ``mkdir`` is about to create, which
    used to be skipped and is now bound. It is the calling user's own subtree
    and the cache or repos bind covers the same path read-write a few entries
    later, so the namespace is unchanged; the old argv merely reached that path
    through the covering bind instead of its own.

    The flags below default to the *permissive* answer, which is worth knowing
    before setting one: an entry that forgets ``protected`` may be shadowed by
    a late mask, and one that forgets ``always_deny`` is not carried as a
    write-deny root. Both fail open. ``user_data`` fails closed — a missing
    root is a missing capability. ``require_dir`` is not a policy flag at all;
    it narrows the render's own existence test and is documented with it.
    """

    mode: Mode
    #: None for ``tmpfs`` (which uses ``source`` for the path) and ``flag``.
    source: Path | None
    #: None means "same as source, as written". For ``symlink`` this is the
    #: in-sandbox path and ``source`` is the link target.
    dest: Path | None
    #: A short stable id — ``user_temp_dir``, ``claude_credentials``. It
    #: appears in no argv; it is what makes the plan dumpable and what the
    #: parity test names its divergences by.
    reason: str
    #: Raw argv for a ``flag`` entry (``--unshare-net``, ``--unshare-pid``, …).
    argv: tuple[str, ...] = ()
    #: Whether a late tmpfs mask may not shadow this entry. Read by
    #: ``mask_protected_paths``' plan branch; set by a later stage of the
    #: sandbox-mount-plan-as-data spec, not by this one.
    protected: bool = False
    #: Whether this is user data the native brain's file tools should see as a
    #: root. False for /usr, the venv, the source tree, the Claude runtime
    #: block and the sockets. Read by :func:`project_fs_roots`.
    user_data: bool = False
    #: Whether the projection must carry this entry as a write-deny root even
    #: when the source is absent. Read by :func:`project_fs_roots`. One entry
    #: sets it — see the ``.developer`` emission in :func:`build_mount_plan`
    #: for the window that costs. **Inert on its own**: the projection walks
    #: ``user_data`` entries, so an entry setting only this one is never
    #: reached. Set both.
    always_deny: bool = False
    #: Whether the *render* requires the source to be a directory rather than
    #: merely to exist. Only ``.developer`` sets it, and only because
    #: ``always_deny`` forced that entry to be emitted unconditionally: the
    #: single function this module replaced gated the bind on ``is_dir()``, and
    #: ``user_temp_dir`` is per user rather than per task, so a model in one
    #: task can leave a regular file named ``.developer`` there for the next.
    #: Without this the render's plain ``exists()`` would bind that file and
    #: the argv would differ from the argv this module promises not to change
    #: — and, on a symlink loop at that name, would raise out of ``resolve()``
    #: rather than producing an argv at all. The render therefore applies it
    #: to the *unresolved* source, before resolving.
    require_dir: bool = False


@dataclass(frozen=True)
class MountPlan:
    """What a sandbox mounts, in emission order, plus its tail state.

    ``masks`` is held apart from ``mounts`` rather than as more entries in it,
    because the masks must be the last mount operations bwrap performs and a
    single list makes that a convention rather than a structure.
    """

    mounts: tuple[Mount, ...]
    chdir: Path
    masks: tuple[Path, ...] = ()
    #: Masks refused because they would have shadowed a path the task needs.
    #: Logged at build time; carried here so a caller can report on them.
    refused_masks: tuple[Path, ...] = ()
    #: The validated REPL workspace, if one was supplied. Also the chdir target
    #: when set, and the extra entry ``mask_protected_paths`` was given.
    workspace_resolved: Path | None = None


class SandboxProfile(str, Enum):
    """Which *outer process* a sandbox is being built for.

    The mount plan is otherwise generic: every bind, mask and namespace flag
    `build_mount_plan` emits is decided by the config, the task and the user,
    and is identical under both profiles. Two things are not, and both exist
    only because the process bwrap execs is the `claude` CLI:

    - the Claude runtime block — ``~/.local/bin``, ``~/.local/share/claude``,
      ``~/.local/state/claude``, and the ``~/.claude`` tmpfs base with
      ``.credentials.json``, ``settings.json`` and the ``projects``/``debug``/
      ``todos`` directories through it;
    - the ``custom_system_prompt_path`` bind, which is there because the CLI
      opens that file itself, inside the namespace.

    ``NATIVE`` is the profile for a sandbox around istota's own code —
    NativeBrain's Bash tool today, its tool server next. That process makes no
    model call from inside the namespace, so it needs neither: it reads the
    system prompt in the daemon, and handing it the credential file means a
    `cat` of `~/.claude/.credentials.json` comes back as a tool result to
    whatever provider native is pointed at (ISSUE-389). Read-only stops the
    token being *rewritten*, never read.

    There is deliberately no default. A forgotten profile is a ``TypeError``
    at the call site rather than a silent grant of the Claude mounts, which is
    the failure this split exists to make impossible.
    """

    CLAUDE = "claude"
    NATIVE = "native"


def plan_masks(config: Config, protected: list[Path]) -> tuple[list[Path], list[Path]]:
    """The database masks for this config, as ``(masks, refused)``.

    No SQLite file the daemon owns is readable from inside the sandbox, for
    admins or anyone else. Reads and writes go through skill CLIs, which the
    proxy runs host-side scoped by ISTOTA_USER_ID.

    These are masks rather than "just don't bind it" because not binding was
    never sufficient and the gap was invisible: ``module_data_dir`` defaults
    under ``{db_path.parent}``, the reference deployment puts that under
    ``istota_home``, and ``sandbox_ro_paths`` defaulted to the ``/srv/app`` that
    contains it — so one RO bind that mentions no database exposed the
    framework DB, its live -wal/-shm, every user's health/money/location/feeds
    DB, the local DB backups and the browser profile. An empty tmpfs over the
    directories shadows whatever earlier binds put there, because bwrap applies
    operations in argv order and the render emits these last. Keep them last.
    ``--remount-ro`` on each mask is part of the same operation — see below for
    why an empty *writable* tmpfs makes the dead end look like a corrupt
    database — and is the one thing that may follow a mask, since it can only
    take permissions away. The render adds it; whether the host's bwrap
    supports it is a property of the binary rather than of the plan.

    It is a mask, not a revocation: with ``kernel.unprivileged_userns_clone``
    on (bwrap needs it) a process can ``unshare -Urm`` and umount a tmpfs to
    reveal what was underneath, which is why ``--disable-userns`` is passed
    where bwrap supports it and why ``sandbox_ro_paths`` should stay narrow.
    With nothing bound underneath — the shipped default — there is nothing to
    reveal either way.

    ``protected`` is what a mask may not shadow: a mask at or above any of
    those would take away something the task needs (its own workspace, the
    source tree it runs from), turning a security measure into an outage. The
    standalone layout puts db_path beside the workspace, so this is reachable
    by configuration rather than only by mistake.
    """
    from . import executor

    masks: list[Path] = []
    refused: list[Path] = []

    def _mask_dir(target: Path) -> bool:
        """Plan to cover ``target`` with an empty, read-only tmpfs, at every
        name it answers to. False if any name went uncovered.

        Both the resolved path and the path as written: a bind uses the
        *unresolved* string as its sandbox destination, so under a symlinked
        deployment root (`/srv` -> `/realstore`) a bind lands at `/srv/app`
        while a resolved-only mask lands at `/realstore/app/...` — a path not
        in the namespace at all, leaving the databases readable at the name the
        model would actually use.

        Read-only because a writable mask makes the dead end lie. `sqlite3
        {db_dir}/istota.db "select …"` on a writable tmpfs *creates* the file
        and then reports `no such table` — which reads as a missing schema or a
        corrupt database, sends the model hunting, and leaves a zero-byte
        `istota.db` sitting in the directory for the rest of the task. On a
        read-only mask the same command fails at open, which is the truth: the
        file is not in this namespace. It also means nothing a task writes
        under a database directory can survive to be mistaken for a database.

        Read-only makes a mask *under* an existing mask fatal rather than
        merely redundant: bwrap has to `mkdir` the second mountpoint on the
        first mask's tmpfs, gets EROFS, and exits before running anything — so
        a second mask nested in the first would fail every task rather than
        weakening one directory. Already-covered candidates are therefore
        skipped here, where every mask can see the others, rather than by each
        caller checking one path against one other.
        """
        covered = True
        candidates: list[Path] = []
        for candidate in (target, target.resolve()):
            if candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            shadowed = executor.mask_shadowed_by(candidate, protected)
            if shadowed:
                logger.error(
                    "Not masking %s: it contains paths the sandbox needs (%s). "
                    "Move db_path/module_data_dir out from above the workspace "
                    "and the source tree — the databases are exposed until you do.",
                    candidate, ", ".join(str(p) for p in shadowed),
                )
                refused.append(candidate)
                covered = False
                continue
            if any(candidate.is_relative_to(m) for m in masks):
                # Covered by an earlier mask, which is the same guarantee by a
                # different mount — not a skip the caller needs to hear about.
                continue
            masks.append(candidate)
        return covered

    if config.db_path:
        db_dir = Path(config.db_path).parent
        _mask_dir(db_dir)
        try:
            module_root = config.module_db_root()
        except ValueError:
            # module_data_dir is under the Nextcloud mount — a misconfiguration
            # module resolution fails loudly on. Refusing to build the sandbox
            # would turn it into "every task fails", so mask what we can and
            # let the module path own the error. The mount root is bound only
            # per-user, so the misplaced root isn't broadly reachable anyway.
            logger.warning(
                "module_data_dir is under the Nextcloud mount; skipping its "
                "sandbox mask (module resolution will raise on use)",
            )
        else:
            # No "is it already under db_dir?" test here: `_mask_dir` skips a
            # candidate any earlier mask already covers, and it does it against
            # every name each mask answers to. The check that used to live here
            # compared one resolved path against one other, and it also skipped
            # the module root when the db_dir mask had been *refused* — leaving
            # it unmasked for want of a cover that was never mounted.
            _mask_dir(module_root)

    return masks, refused


def build_mount_plan(
    config: Config,
    task: db.Task,
    is_admin: bool,
    user_resources: list[db.UserResource],
    user_temp_dir: Path,
    *,
    profile: SandboxProfile,
    proxy_sock: Path | None = None,
    net_proxy_sock: Path | None = None,
    extra_ro_binds: list[Path] | None = None,
    authorized_skills: "frozenset[str] | set[str] | list[str] | None" = None,
    workspace_dir: Path | None = None,
) -> MountPlan:
    """Every bind, mask and namespace flag this task's sandbox gets, in order.

    ``profile`` is required and keyword-only — see :class:`SandboxProfile` for
    what it decides and why it has no default. Everything else about the plan is
    generic, including the ordering, which is load-bearing in four places
    (cache bind before repos bind, ``.developer`` re-bind after
    ``user_temp_dir``, ``extra_ro_binds`` after *every* bind — its two entries
    are a document a daemon-side OCR call names, which lives inside a
    read-write bind that would otherwise bury it, and the task's own control
    directory — and masks last with ``--remount-ro`` after each).
    Omitting the Claude block under ``NATIVE`` moves none of them.

    ``authorized_skills`` is the union of selected skills and skills
    auto-authorized by credential presence — the same set
    ``_build_network_allowlist`` keys on. It used to be ``selected_skills``, was
    read by nothing, and is now the predicate behind the exec-socket bind.
    *Authorized*, not *selected*, deliberately: `developer` is a menu skill with
    no `always_include` and no `source_types`, so it reaches `selected_skills`
    only via sticky skills, which is to say on the second turn of a conversation
    and not the first.

    ``workspace_dir`` (REPL ``--workspace cwd``) is bound RW and becomes the
    sandbox ``--chdir`` target instead of ``user_temp_dir``. It is bounds-checked
    against the protected-path blocklist (see ``_validate_workspace_dir``) — an
    arbitrary RW bind would otherwise let a workspace shadow the RO ``.developer``
    protections or reach another user's mount.

    Raises ``ValueError`` from ``_validate_workspace_dir`` and nothing else.
    That propagates here and is caught by ``native_fs_roots``, which is a
    deliberate asymmetry rather than an oversight: a REPL workspace the sandbox
    refuses must fail the task, while the native brain's roots degrade to the
    unvalidated shape and log.

    **This is not a free function to call.** It is deliberately outside the
    ``_bwrap_available()`` gate, because ``native_fs_roots`` has to build a
    plan on the shapes with no bwrap — there the roots are the only confinement
    there is — but it has two side effects a caller inherits.
    ``resolve_sandbox_cache_dir`` creates the user's cache directory, and
    ``plan_masks`` logs a refused mask at ERROR and a misplaced
    ``module_data_dir`` at WARNING. Both were previously reachable only from a
    host that was going to build a sandbox; a second consumer calling this per
    task on macOS or the standalone install turns the first into a directory
    nothing will mount and the second into the same two lines forever.
    """
    from . import config as istota_config
    from . import executor

    mounts: list[Mount] = []

    def _ro(
        src: Path,
        reason: str,
        *,
        dest: Path | None = None,
        user_data: bool = False,
        always_deny: bool = False,
        require_dir: bool = False,
    ) -> None:
        mounts.append(Mount(
            mode="ro", source=src, dest=dest, reason=reason, user_data=user_data,
            always_deny=always_deny, require_dir=require_dir,
        ))

    def _rw(
        src: Path, reason: str, *, dest: Path | None = None, user_data: bool = False,
    ) -> None:
        mounts.append(Mount(
            mode="rw", source=src, dest=dest, reason=reason, user_data=user_data,
        ))

    def _tmpfs(path: Path, reason: str) -> None:
        mounts.append(Mount(mode="tmpfs", source=path, dest=None, reason=reason))

    def _symlink(target: Path, path: Path, reason: str) -> None:
        mounts.append(Mount(mode="symlink", source=target, dest=path, reason=reason))

    def _flag(reason: str, *argv: str) -> None:
        mounts.append(
            Mount(mode="flag", source=None, dest=None, reason=reason, argv=argv)
        )

    # --- System (RO) ---
    _ro(Path("/usr"), "usr")
    # Merged-usr compatibility: /bin, /lib, /sbin, /lib64 are symlinks to /usr/*
    # on Debian 13+. Create symlinks inside sandbox so both paths work.
    for compat in ["/bin", "/lib", "/lib64", "/sbin"]:
        p = Path(compat)
        if p.is_symlink():
            _symlink(p.readlink(), p, "merged_usr_compat")
        elif p.exists():
            _ro(p, "merged_usr_compat")

    # Selective /etc binds — only what's needed for DNS, TLS, user lookup,
    # timezone, and for the binaries in the /usr bind above to resolve.
    #
    # /etc/alternatives is the last of those and the least obvious: Debian ships
    # awk, cc, vi, editor, pager, which and nc as /usr/bin symlinks into it, so
    # binding /usr alone carries the links in and leaves every one of them
    # dangling. The command then fails with "No such file or directory" for a
    # binary ls shows sitting right there, inside the sandbox only. It holds
    # nothing but symlinks back into /usr, which is already bound.
    etc_files = [
        "/etc/ssl", "/etc/ca-certificates", "/etc/resolv.conf",
        "/etc/hosts", "/etc/nsswitch.conf", "/etc/ld.so.cache",
        "/etc/localtime", "/etc/passwd", "/etc/group",
        "/etc/alternatives",
    ]
    for ef in etc_files:
        _ro(Path(ef), "etc")

    # --- Namespaces ---
    _flag("namespaces", "--unshare-pid", "--proc", "/proc", "--dev", "/dev",
          "--tmpfs", "/tmp")

    # --- Application installs (RO) ---
    # Bind extra RO paths from config (e.g. /srv/app for co-located services)
    for ro_path in config.security.sandbox_ro_paths:
        _ro(Path(ro_path), "sandbox_ro_paths")

    # --- Python venv + source tree (RO) ---
    # Resolve istota_home from the source tree (src/istota/ -> parent -> parent).
    # Shared with `mask_protected_paths`, which has to name the same two paths.
    istota_src, venv_path = executor._source_and_venv_paths()
    _ro(venv_path, "venv")
    _ro(istota_src, "istota_src")

    # --- Custom system prompt (RO, this one file) --- CLAUDE only.
    # The config directory is not in the sandbox and should not be: it holds
    # config.toml. Everything else in it — emissaries, persona, guidelines,
    # skill bodies — reaches the model as content the daemon read and put in
    # the prompt. `system-prompt.md` is the exception, because the CLI opens
    # the path itself, inside the namespace. Binding the file rather than its
    # directory keeps config.toml out; bwrap creates the parent as a mount
    # point and nothing else in it is visible.
    #
    # This dependency was met until now only by `sandbox_ro_paths =
    # ["/srv/app"]`, the same default that exposed the databases; narrowing it
    # to [] made every task on a custom_system_prompt install exit with
    # "System prompt file not found".
    #
    # `NATIVE` skips it because nothing inside that namespace opens the path:
    # NativeBrain reads the system prompt in the daemon and puts it on the
    # wire itself, so the file is already prompt text by the time the sandbox
    # is built. Which is also why `NATIVE` needs no config-directory bind at
    # all — this was the only one.
    if profile is SandboxProfile.CLAUDE:
        sp_path = executor.custom_system_prompt_path(config)
        if sp_path is not None:
            _ro(sp_path, "custom_system_prompt")

    # Mask other users' config files
    users_config_dir = istota_src / "config" / "users"
    if users_config_dir.exists():
        _tmpfs(users_config_dir, "users_config_dir")

    # --- Claude CLI runtime (selective .local binds) --- CLAUDE only.
    #
    # These exist because the process bwrap execs *is* the `claude` CLI: it
    # resolves its own binary, reads its installed versions, takes a lock in
    # state/, authenticates with the credential and writes its session JSONL
    # back out. Under `NATIVE` the outer process is istota's own code, which
    # does none of that — and binding the block anyway is ISSUE-389's filed
    # bug, because `cat "$HOME/.claude/.credentials.json"` inside a native
    # Bash call returns the subscription token as a tool result. Read-only
    # stops it being rewritten, not read.
    #
    # `~/.local/bin` goes with them. On the reference deployment it holds
    # `claude` and `gws`, and neither is reachable from a native namespace by
    # design: `gws` is spawned host-side by the skill proxy, never in-sandbox.
    #
    # The `~/.claude` tmpfs goes too, and that is the one line worth pausing
    # on, because it is a mask as well as a mount base. With the shipped
    # `sandbox_ro_paths = []` nothing puts `$HOME` in the namespace, so under
    # `NATIVE` the directory is absent rather than shadowed and the mask has
    # nothing to do. Under a broadened `sandbox_ro_paths` covering `$HOME` it
    # would have — that is the same default-deny-plus-hardening question the
    # spec parks for `/etc/{namespace}` and the config directory, and it is
    # deferred here for the same reason rather than overlooked.
    home = Path(os.environ.get("HOME", "/tmp"))
    if profile is SandboxProfile.CLAUDE:
        # bin/ and share/claude/ are RO (binary + versions)
        _ro(home / ".local" / "bin", "claude_local_bin")
        _ro(home / ".local" / "share" / "claude", "claude_share")
        # state/claude/ is RW (lock files created at runtime)
        _rw(home / ".local" / "state" / "claude", "claude_state")

        # --- Claude auth (tmpfs base + RW credentials for OAuth refresh) ---
        claude_dir = home / ".claude"
        if claude_dir.exists():
            _tmpfs(claude_dir, "claude_home")
            creds = claude_dir / ".credentials.json"
            if creds.exists():
                # RO: prevents token persistence attacks
                _ro(creds, "claude_credentials")
            settings = claude_dir / "settings.json"
            if settings.exists():
                _ro(settings, "claude_settings")
            # Persist session JSONL logs and debug output across sandbox exits
            for subdir in ["projects", "debug", "todos"]:
                d = claude_dir / subdir
                if d.exists():
                    _rw(d, f"claude_{subdir}")

    # --- User workspace (RW) ---
    _rw(user_temp_dir.resolve(), "user_temp_dir", user_data=True)

    # --- REPL workspace (RW) — validated, bound, and used as the chdir target.
    workspace_resolved: Path | None = None
    if workspace_dir is not None:
        workspace_resolved = executor._validate_workspace_dir(config, workspace_dir)
        _rw(workspace_resolved, "repl_workspace", user_data=True)

    # .developer/ scripts (credential-fetch, git helpers) must be read-only
    # to prevent a compromised subprocess from replacing them to intercept
    # credentials.  A later --ro-bind on a subdir overrides the parent --bind.
    #
    # **Emitted whether or not the directory is there**, which is the one
    # entry where that matters. `build_bwrap_cmd` is called per Bash
    # invocation and re-reads the filesystem each time, while the roots
    # `project_fs_roots` derives are built once per task — so an entry that
    # appeared only when the directory did would leave a window in which a
    # `.developer` created mid-run is read-only for Bash and writable for the
    # native file tools. `always_deny` is what carries it through the
    # projection; `require_dir` is what keeps the argv unchanged, since the
    # render's own skip is `exists()` and this site's used to be `is_dir()`.
    dev_dir = user_temp_dir.resolve() / ".developer"
    _ro(dev_dir, "developer_dir", user_data=True, always_deny=True, require_dir=True)

    # --- Skill proxy socket (RO inside sandbox) ---
    if proxy_sock and proxy_sock.exists():
        _ro(proxy_sock, "skill_proxy_socket")

    # --- Network isolation ---
    if net_proxy_sock:
        _flag("network_isolation", "--unshare-net")
        if net_proxy_sock.exists():
            _ro(net_proxy_sock, "net_proxy_socket")

    # --- No Docker API reaches a task, and no `docker` binary either ---
    # This used to bind the Docker CLI read-only and, at the conventional
    # in-sandbox path /var/run/docker.sock, a per-user allowlist proxy in front
    # of the root-equivalent socket. Both are gone, and the proxy with them.
    #
    # The bind was safe on its own terms — the proxy refused create, run, build,
    # privileged and host-mount, so a task reaching it with `curl --unix-socket`
    # could not escalate — but it was also *unconditional*, which is what makes
    # its removal worth stating: `cp`, `restart`, `inspect` and the raw HTTP
    # surface were reachable from every task of every user on a devbox
    # deployment, including tasks built from email, feeds and fetched pages.
    #
    # Nothing in a build needs it now. Project code reaches the container over
    # the exec transport, whose socket is bound below and gated on
    # `"developer" in authorized_skills` — an arbitrary-command channel into a
    # permissive-egress container is not an allowlist, so unlike the proxy's it
    # cannot be ungated. The devbox skill's one remaining Docker verb, `reset`,
    # runs host-side in the skill CLI process and never wanted a bind.
    #
    # **The socket is what makes this true, not the missing CLI bind.** `/usr`
    # is `--ro-bind`ed unconditionally a little above, so `/usr/bin/docker` —
    # which is exactly `devbox.docker_cli`'s default — is still in the namespace
    # on any host that installs the client. The explicit bind was redundant
    # there and is gone with the rest; what a task cannot do is reach a daemon,
    # because no socket is bound at any path and no `DOCKER_HOST` is exported.

    # --- Nextcloud mounts (scoped per-user for both admin and non-admin) ---
    mount = config.nextcloud_mount_path
    if mount:
        mount = mount.resolve()
        user_dir = mount / "Users" / task.user_id
        if user_dir.exists():
            _rw(user_dir, "nextcloud_user_dir", user_data=True)
        # Talk attachments directory (flat, shared across conversations)
        talk_dir = mount / "Talk"
        if talk_dir.exists():
            _ro(talk_dir, "nextcloud_talk_dir", user_data=True)
        if task.conversation_token:
            channel_dir = mount / "Channels" / task.conversation_token
            if channel_dir.exists():
                _rw(channel_dir, "nextcloud_channel_dir", user_data=True)

    # --- Huggingface model cache (RO) ---
    hf_cache = home / ".cache" / "huggingface"
    if hf_cache.exists():
        _ro(hf_cache, "huggingface_cache")

    # --- Package-manager cache (RW) ---
    # Not gated on admin or on the developer skill: any task that runs a
    # package manager writes a cache, and without this the write lands on
    # bwrap's root tmpfs. `execute_task` points UV_CACHE_DIR and XDG_CACHE_HOME
    # at whatever this returns, so the bind and the environment cannot disagree.
    #
    # One user's own directory, never a shared root — see
    # `resolve_sandbox_cache_dir` for why a shared cache is a cross-user code
    # path. This is emitted late, after the `.developer` read-only re-bind and
    # the huggingface bind, so a destination *above* either would cover it;
    # `_sandbox_bind_targets` is what refuses that. Still before the masks,
    # which stay last.
    #
    # The bind stays *before* the developer repos bind, deliberately. With
    # `developer.repos_dir` set the cache is `{repos_dir}/{user_id}/`
    # `.package-caches`, so that later bind is an ancestor and covers this one,
    # which is what puts the cache and a worktree's venv on a single mount and
    # is the only shape where uv can hardlink rather than copy — moving this
    # bind after it makes the cache its own mount again and costs the full byte
    # copy. What that covering exposes is the rest of *this* user's subtree,
    # which is already bound RW for exactly these tasks; there is no other
    # user's cache in the namespace to mask, which is what retired the ISSUE-319
    # machinery rather than merely satisfying it.
    #
    # **The `--disable-userns` precondition this bind used to carry is gone,
    # and ISSUE-320 asked whether a symlink swap came back with it.** Measured
    # on a real kernel
    # (`tests/linux/test_sandbox_cache_dir.py::TestTheCacheBindSymlinkRace`).
    # The window is real, it was reachable, and what closed it is the gate on
    # `sandbox_cache_is_derived` rather than anything about the flag.
    #
    # **The window is real.** The render emits `src.resolve()` as the source and
    # the path *as written* as the destination. On a real directory `resolve()`
    # returns the same string, so the argv source is the written name — and
    # bwrap's `mount` walks that name again, in the kernel, after Python is
    # done with it. A symlink planted in between is followed: measured binding
    # another user's subtree read-write, on the shape with no covering bind.
    # (There is a second, earlier window between `resolve_sandbox_cache_dir`
    # returning and the render's own `resolve()`; that one is closed by Python
    # resolving, not by the kernel, and it is much narrower.)
    #
    # **What makes it unreachable is that the derived cache and the covering
    # bind now have one gate.** `sandbox_cache_is_derived` is `is_admin and
    # developer.enabled and developer.repos_dir` — exactly the condition the
    # repos bind below is emitted on. So wherever this bind's source sits in a
    # model-writable parent, the repos bind is emitted after it, is an ancestor
    # of it, and lands on top: the swapped mount is buried and what the sandbox
    # sees at the cache path is the host symlink, dangling. Before that gate
    # matched, a *non-admin* on a devbox deployment derived a cache inside
    # `{repos_dir}/{user_id}` — which their own container mounts read-write —
    # and took this bind with nothing above it. That was ISSUE-320 holding.
    #
    # **The entry's account of what the flag was doing is wrong**, and it is
    # worth not re-deriving: passing `--disable-userns` does not pin this
    # directory as a mountpoint. bwrap mounts inside its own mount namespace,
    # so the host directory never becomes one, and `rename` on it succeeds
    # while the bind is live — measured on a bwrap without the flag, which is
    # what the linux tier runs; the flag-on arm of that measurement skips there
    # and runs where bwrap has it. The flag never closed this window, so its
    # removal did not open it.
    #
    # The ordering below is therefore load-bearing for a second reason beyond
    # `link(2)`: moving this bind after the repos bind costs both. The linux
    # tier is what detects that; `tests/test_sandbox.py`'s bind-order assertion
    # names this as its second reason.
    #
    # **`user_data` is False on the derived branch, and that asymmetry with the
    # bind is deliberate (ISSUE-320).** The projection has no mounts and so no
    # ordering to lean on: nothing lands on top of anything, and `ToolEnv`
    # realpaths every root it is handed, which is later than
    # `resolve_sandbox_cache_dir`'s `O_NOFOLLOW` check. A symlink planted at
    # `.package-caches` in between would therefore make the link's *target* a
    # write root of its own. On the derived branch the cache is inside the
    # repos subtree, which is already a write root, so the entry buys nothing
    # and costs that; the cache stays writable through the root that contains
    # it. On the fallback branch the root is operator-owned, outside
    # `repos_dir` and bound into no sandbox, so there is no writer to race and
    # the entry is the only thing making the cache writable at all.
    #
    # `sandbox_cache_is_derived` is the gate for both halves, which is why this
    # is one condition rather than two that could drift apart.
    cache_dir = executor.resolve_sandbox_cache_dir(config, task.user_id)
    if cache_dir is not None:
        _rw(
            cache_dir, "package_cache",
            user_data=not executor.sandbox_cache_is_derived(config, task.user_id),
        )

    # --- Developer repos (RW) ---
    #
    # The task's own subtree, never the shared root — see `get_user_repos_dir`.
    # Created by the developer skill's `setup_env`, which runs before this.
    #
    # The `exists()` below is the guard that stops a user who has never run a
    # developer task from having the root stand in, and on this branch it is
    # again not catching anything: `resolve_sandbox_cache_dir` runs a dozen
    # lines above and creates `{repos_dir}/{user_id}` with `parents=True`
    # whenever `sandbox_cache_is_derived` holds — which is this same condition,
    # since ISSUE-320 made the two gates one. So wherever this bind is emitted
    # the directory has just been created, and `setup_env` had already created
    # it for exactly these tasks besides. Kept because the two call sites are
    # far apart and the coupling is not local; the next person to move that
    # `mkdir` should find this note rather than a comment claiming a check that
    # does something.
    if is_admin and config.developer.enabled:
        repos = executor.get_user_repos_dir(config, task.user_id)
        if repos is not None and repos.exists():
            # `user_data`, and safe to realpath in a way the derived cache is
            # not: its parent is `developer.repos_dir`, which no task and no
            # devbox can write, so there is no planting a symlink at this name.
            _rw(repos, "developer_repos", user_data=True)

    # --- The devbox exec socket (RW) ---
    #
    # Gated on `"developer" in authorized_skills`, byte for byte the predicate
    # at `_build_network_allowlist` that already decides whether this task gets
    # the package registries and the forge. The exec socket is bound exactly
    # where the package registries are allowed.
    #
    # **The gate is about the mechanism, not caution.** The retired docker
    # socket proxy this replaced was bound into every sandbox ungated, and that
    # was defensible because it was an allowlist: it refused create, run, build,
    # privileged and host-mount, so even an untrusted-content task reaching it
    # with `curl --unix-socket` could not escalate. This is the opposite shape —
    # an unauthenticated arbitrary-command channel into a container with
    # permissive egress — so binding it into every task's sandbox would hand an
    # email, feed or browse task a route straight around
    # `_build_network_allowlist`, which is per task and skill-scoped. Nothing
    # binds a Docker socket at any path now; the contrast is why this one is
    # gated, not a description of a bind that still exists.
    #
    # The *directory*, not the socket file: a server restart unlinks and
    # recreates the inode, and a bind of the file itself strands this side
    # against a dead target. Only the per-user subdirectory — the parent holds
    # every user's socket, and that is arbitrary command execution against
    # another user's repositories.
    if (
        istota_config.devbox_container_backend(config)
        and "developer" in (authorized_skills or ())
    ):
        exec_dir = istota_config.exec_socket_dir(config, task.user_id)
        if exec_dir is not None and exec_dir.is_dir():
            _rw(exec_dir, "devbox_exec_socket")

    # --- Per-resource mounts ---
    if mount:
        for r in user_resources:
            if not r.resource_path:
                continue
            rpath = (mount / r.resource_path.lstrip("/")).resolve()
            if not rpath.exists():
                continue
            # Skip if already covered by user dir bind
            user_dir = mount / "Users" / task.user_id
            try:
                rpath.relative_to(user_dir.resolve())
                continue  # Already inside user dir
            except ValueError:
                pass
            if r.permissions == "readwrite":
                _rw(rpath, "user_resource", user_data=True)
            else:
                _ro(rpath, "user_resource", user_data=True)

    # --- Extra RO binds (e.g. service sockets for same-host APIs, and the
    # document a task-less OCR call reads) ---
    #
    # **Last of the binds, and that is the point.** bwrap applies operations in
    # argv order and the later mount wins, so a caller asking for read-only
    # gets read-only whatever else covers the same path. Emitted higher up —
    # where this block used to sit, between the `user_temp_dir` bind and the
    # Nextcloud mount — it was buried by any later bind of an ancestor, and a
    # bloodwork panel's upload lives under `{mount}/Users/{user_id}`, which is
    # bound read-write. So the one caller that names a document
    # (`build_daemon_sandbox`, ISSUE-397) got read-write on its main path while
    # every comment said otherwise, and a test asserting only that the
    # `--ro-bind` was emitted passed throughout. The assertion has to be about
    # argv order: `tests/test_brain_request_confinement.py::
    # test_the_document_stays_read_only_under_a_later_bind`.
    #
    # It has one task-path entry now, and being last is what that entry wants
    # too. `execute_task` appends the task's own control directory,
    # `{temp_dir}/.control/{user_id}/task_{id}` — both prompt halves, the
    # briefing metadata and the prepared image renditions, everything the
    # daemon authors for this task. It is a sibling of `user_temp_dir` rather
    # than a child, so nothing binds over it today; being after every bind is
    # what keeps that true of any bind added later. (The entry used to be one
    # file *inside* the read-write `user_temp_dir` bind, where a later
    # read-only bind was the only thing that made it read-only at all. That
    # ordering requirement is gone with the file, and the loop stays where it
    # is because the property is worth having unconditionally. This comment
    # used to say the loop was free to move because the list was always empty
    # on the task path. It was true when written and stopped being true in the
    # same release.)
    #
    # A missing entry is skipped rather than raising, because bwrap fails the
    # whole namespace on a bind whose source is absent — one cleanup race would
    # otherwise fail every task instead of one. It is logged by the render,
    # because the entry is a boundary and the two callers lose different things
    # by it. The OCR document sits inside a read-write bind, so a skipped entry
    # leaves the writable copy in the namespace and nothing else says so. The
    # control directory is bound by nothing else, so a skipped entry leaves the
    # path *absent*: the CLI then exits at `--append-system-prompt-file` and a
    # `Read` of a prepared attachment gets ENOENT. Fail-closed either way,
    # which is why the message names both rather than picking one.
    for path in (extra_ro_binds or []):
        _ro(path, EXTRA_RO_BIND)

    # --- Database masks (must be the LAST mount operations) ---
    # Held apart from the binds so the render cannot put anything after them.
    # See `plan_masks` for why they are masks rather than absent binds.
    protected = executor.mask_protected_paths(
        config, user_temp_dir=user_temp_dir, workspace_dir=workspace_resolved,
    )
    masks, refused = plan_masks(config, protected)

    return MountPlan(
        mounts=tuple(mounts),
        chdir=workspace_resolved or user_temp_dir.resolve(),
        masks=tuple(masks),
        refused_masks=tuple(refused),
        workspace_resolved=workspace_resolved,
    )


def render_bwrap_argv(
    plan: MountPlan,
    cmd: list[str],
    *,
    net_proxy_sock: Path | None = None,
    user_temp_dir: Path,
) -> list[str]:
    """The plan as bwrap's argv, plus the lifecycle tail.

    Mechanical, with one decision left in it: a bind whose source does not
    exist is skipped, because bwrap fails the whole namespace on a missing
    source and one cleanup race would otherwise fail every task instead of one.
    ``Mount.require_dir`` narrows that test to ``is_dir()`` for the one entry
    the builder emits unconditionally.

    Raises nothing for a plan :func:`build_mount_plan` produced — which is the
    only kind there is on any product path, and the precondition the ``assert``
    below states rather than assumes. A hand-built plan whose ``ro``/``rw``/
    ``tmpfs`` entry carries no source is a programming error and is not made to
    render as a silently dropped bind, since a dropped bind is exactly the
    failure this module is here to make visible.

    The three things after the mounts are not plan data. The masks' companion
    ``--remount-ro`` and the ``--unshare-user`` / ``--disable-userns`` pair are
    properties of the host's bwrap binary rather than of this task, and the
    ``--die-with-parent`` / ``--chdir`` / ``--`` tail plus the network bridge
    wrapper are the process's lifecycle rather than its filesystem.
    """
    from . import executor

    args: list[str] = ["bwrap"]

    for entry in plan.mounts:
        if entry.mode == "flag":
            args.extend(entry.argv)
            continue
        if entry.mode == "symlink":
            args.extend(["--symlink", str(entry.source), str(entry.dest)])
            continue
        if entry.source is None:
            # Not an assert: those vanish under `python -O`, and the next line
            # would then be an AttributeError from inside a sandbox spawn. A
            # sourceless bind is a programming error in whatever produced the
            # plan, and it is raised rather than skipped because a dropped bind
            # is the failure this module exists to make visible.
            raise ValueError(
                f"mount {entry.reason!r} has mode {entry.mode!r} and no source"
            )
        if entry.mode == "tmpfs":
            args.extend(["--tmpfs", str(entry.source.resolve())])
            continue
        if entry.reason == EXTRA_RO_BIND and not entry.source.exists():
            logger.warning(
                "sandbox: extra read-only bind %s does not exist; the path is "
                "left as its parent bind has it, or absent from the namespace "
                "entirely where nothing else binds it", entry.source,
            )
            continue
        if entry.require_dir and not entry.source.is_dir():
            # Before the `resolve()` below, never after, and that ordering is
            # the whole point of the flag. The gate this reproduces ran in the
            # builder, so the path was never resolved when it failed; a
            # symlink loop at that name makes `is_dir()` answer False and
            # `resolve()` raise `RuntimeError`, and `user_temp_dir` is bound
            # read-write into every sandbox of that user — so checking after
            # resolving would let one task plant a loop and make every later
            # sandbox build for that user raise instead of returning an argv.
            continue
        original = str(entry.source)
        src = entry.source.resolve()
        if not src.exists():
            continue
        dest = str(entry.dest.resolve()) if entry.dest else original
        args.extend(["--ro-bind" if entry.mode == "ro" else "--bind", str(src), dest])

    # --- Database masks (must be the LAST mount operations) ---
    for candidate in plan.masks:
        args.extend(["--tmpfs", str(candidate)])
        if executor._bwrap_supports_remount_ro():
            # After the tmpfs, never before: --remount-ro acts on whatever
            # is mounted at that path at the time bwrap reaches it, and
            # before the tmpfs that is the host directory.
            args.extend(["--remount-ro", str(candidate)])

    if executor._bwrap_supports_disable_userns():
        # Both, or neither: bwrap exits 1 on `--disable-userns` without
        # `--unshare-user` ("--disable-userns requires --unshare-user"), which
        # is why this flag never once reached a real sandbox — the probe had
        # the same gap and answered "unsupported" on every host. Unprivileged
        # bwrap unshares the user namespace regardless, so on the supported
        # deployment the companion flag only makes the request explicit.
        args.extend(["--unshare-user", "--disable-userns"])
    elif executor._bwrap_requires_unshare_user():
        # Not hardening, unlike the branch above: on this host it is what makes
        # bwrap run at all. `_bwrap_available`'s plain probe failed and its
        # `--unshare-user` probe succeeded, which happens as uid 0 with a
        # non-setuid bwrap — bwrap only forces the user namespace on itself
        # when it is neither. The real argv has to carry the flag the probe was
        # answered with, or the daemon would report a working sandbox and then
        # build one that cannot start.
        args.append("--unshare-user")

    # --- Lifecycle ---
    args.extend(["--die-with-parent", "--chdir", str(plan.chdir)])
    args.append("--")

    if net_proxy_sock:
        # Wrap the command in a shell that starts the TCP-to-Unix bridge as a
        # background process, then execs the original command with HTTPS_PROXY
        # pointed at the bridge. "$@" preserves the original argv from cmd.
        #
        # The bridge's stdin is redirected from /dev/null so it cannot share
        # (and accidentally consume) the prompt that the brain pipes to the
        # exec'd command's stdin — the read end is otherwise inherited by both.
        #
        # No `sleep` before exec: the bridge only needs to be listening before
        # the command opens a *network* connection, which happens well after
        # the command starts and reads its stdin prompt; the bridge's bind()
        # /listen() completes within a few ms of Python startup. On the rare
        # cold-start race the command's own connection retry recovers.
        from .network_proxy import BRIDGE_PORT
        bridge_path = str(user_temp_dir.resolve() / ".developer" / "net-bridge")
        sock_path = str(net_proxy_sock)
        shell_cmd = (
            f"python3 {bridge_path} {sock_path} {BRIDGE_PORT} </dev/null & "
            f"exec env "
            f"HTTPS_PROXY=http://127.0.0.1:{BRIDGE_PORT} "
            f"HTTP_PROXY=http://127.0.0.1:{BRIDGE_PORT} "
            f'NO_PROXY= "$@"'
        )
        args.extend(["/bin/sh", "-c", shell_cmd, "sh"] + cmd)
    else:
        args.extend(cmd)

    return args


def project_fs_roots(
    plan: MountPlan, control_dir: Path | None = None,
) -> tuple[list[Path], list[Path], list[Path]]:
    """The plan's user-data binds as ``(read_roots, write_roots, write_denied)``.

    The native brain's file tools take path roots rather than a namespace, so
    they need the same answer :func:`render_bwrap_argv` renders, in a different
    shape. This is that projection, and it is the reason the plan exists as
    data: the two used to be written twice and ISSUE-319 and ISSUE-320 were
    both one copy disagreeing with the other. ``native_fs_roots``' docstring is
    where the *purpose* of these roots is recorded; this function is only the
    derivation.

    Only ``user_data`` entries. ``/usr``, the venv, the source tree, the Claude
    runtime block and the three sockets are in the namespace because a process
    needs them, not because the model's files live there, and the file tools
    have never listed them.

    Four rules, none of which falls out of a plain walk:

    1. **An ``always_deny`` entry is carried whether or not its source
       exists**, and is carried *as written* rather than resolved. That is
       ``.developer``. The existence half is what closes a real window:
       ``build_bwrap_cmd`` re-reads the filesystem on every invocation while
       these roots are built once per task, so a gate here leaves a
       ``.developer`` created mid-run read-only for Bash and writable for the
       file tools. The as-written half is behaviour preservation and nothing
       more — it is the value this function has always returned and the value
       the bind names — and it is worth being explicit that it does **not**
       make the denial symlink-proof: ``ToolEnv`` realpaths every deny root
       before comparing, so a symlink planted at that name still relocates the
       denial. Closing that means changing the enforcer, which is a decision
       about ``ToolEnv`` rather than about this projection.
    2. **A read-only entry nested inside an earlier read-write one is a
       write-deny root, not a read root.** That is what bwrap's ordering does —
       the later ``--ro-bind`` lands on top of the earlier ``--bind`` — and
       containment is how it is expressed where there are no mounts. The rule
       is deliberately one-directional and the other direction is a known gap
       rather than a case it handles: a read-only entry *containing* an earlier
       read-write one also wins in the namespace, because the later mount
       covers the earlier mountpoint, and the inner path stays a write root
       here. Reachable only by two per-resource rows arranged that way, outside
       ``Users/{user_id}``; it predates the projection, and expressing it means
       dropping a root rather than adding one, which is the direction that
       breaks a working deployment.
    3. **The derived package cache is not a write root**, because it is inside
       the repos subtree which already is one, and adding it would make a
       symlink planted at ``.package-caches`` a write root of its own once
       ``ToolEnv`` realpaths it. The plan carries that as ``user_data=False``
       on the derived branch, so this function needs no branch of its own.
    4. **No database root of any kind.** ``plan.masks`` are not mounts, are
       held apart from them, and are not projected. The file tools have no
       masks; what keeps a database out of these roots is that none is under a
       ``user_data`` bind.

    ``control_dir`` is not a plan entry. ``execute_task`` passes it through
    ``extra_ro_binds``, which is a caller-supplied list rather than policy, and
    it is seeded onto the deny list outside the confinement branch besides — so
    it is an argument here for the same reason. It goes on **both**
    ``write_denied`` and ``read_only``: the deny list is enforced ahead of
    ``ToolEnv``'s unconfined early return while the root lists are inert there,
    and under confinement the directory is inside no write root, so without the
    read entry a task could not open its own prepared attachment.

    Raises nothing for a plan :func:`build_mount_plan` produced. That is a
    precondition rather than a guarantee about any input: ``Path.resolve()``
    raises on a symlink loop, and what keeps it out of reach is that every
    entry reaching it was either existence-gated by the builder or resolved
    there already. A hand-built plan can break that.
    """
    plan_write: list[Path] = []
    plan_read_only: list[Path] = []
    plan_denied: list[Path] = []
    #: Read-write roots seen so far, for rule 2. Order, not membership.
    rw_seen: list[Path] = []

    def _add(target: list[Path], path: Path) -> None:
        if path not in target:
            target.append(path)

    for entry in plan.mounts:
        if not entry.user_data or entry.mode not in ("ro", "rw") or entry.source is None:
            continue
        if entry.always_deny:
            _add(plan_denied, entry.source)
            continue
        root = entry.source.resolve()
        if not root.exists():
            continue
        if entry.mode == "rw":
            _add(plan_write, root)
            rw_seen.append(root)
        elif any(root != seen and root.is_relative_to(seen) for seen in rw_seen):
            _add(plan_denied, root)
        else:
            _add(plan_read_only, root)

    write = plan_write
    read_only: list[Path] = []
    write_denied = plan_denied

    if control_dir is not None:
        control = control_dir.resolve()
        _add(write_denied, control)
        _add(read_only, control)
    for path in plan_read_only:
        _add(read_only, path)

    read_roots = list(dict.fromkeys(write + read_only))
    return read_roots, write, write_denied
