"""Bound the on-disk package caches ``security.sandbox_cache_dir`` creates (ISSUE-317).

ISSUE-305 moved a task's uv and npm caches off bubblewrap's root tmpfs and onto
disk, because a cache in RAM is unattributable, capped at half the box's memory,
and thrown away at task exit so every task downloads again. The cost of fixing
that is that the caches now *persist*, and nothing removed them: on the
reference deployment one ``uv sync --all-extras`` is about 1.8 GB of wheels, and
the volume the caches share with ``developer.repos_dir`` was already at 79%.
Turning the key on without this module trades a bounded RAM burn for an
unbounded disk leak on the fuller resource.

:func:`sweep_and_report` runs from the **scheduler**, on
``scheduler.sandbox_cache_sweep_interval``, for the same reason
:mod:`istota.worktree_reaper` does: ``dispatch_setup_env_hooks`` calls every
skill's ``setup_env`` whatever the task selected, so a sweep there would fire
before every Talk reply, every cron job and every heartbeat tick. A delete path
belongs on a stated cadence.

**A size ceiling, not an age rule.** An age window is the obvious policy and it
does not work here. A single dependency resolution writes more than any sane
window's worth of bytes at once — 456 MB for one ``uv sync --extra test`` plus
``npm ci``, roughly 1.8 GB for ``--all-extras`` — so a rule phrased in days
either keeps everything or throws away a cache that is minutes old and about to
be reused. What the operator actually has is a fixed volume, so the budget is
stated in bytes. Every visited cache gets the cheap reclaim first (``uv cache
prune``, ``npm cache verify``), which removes unreachable entries and keeps the
warm ones; only a cache still over its ceiling afterwards is wiped.

**The sweeper never deletes a file itself.** Not the root, not a per-user
directory, not a cache entry. It runs the package managers' own reclaim verbs
and measures the result. A tool that is missing, that fails, or that times out
is reported and the cache is left alone — there is no ``rm -rf`` fallback for a
directory we could not get a tool to reclaim properly, because the difference
between "uv's cache" and "everything the model put in this directory" is exactly
what uv knows and this module does not.

**The concurrency hazard, and the three guards that answer it.** A wipe that
lands while a developer task is mid-``uv sync`` against the same cache breaks
that task: the sync has resolved a wheel to a cache path and unlinking it under
the process turns the next ``link(2)`` into ``ENOENT``. Hoping uv tolerates that
is not a plan, so:

1. **The caller's in-flight set.** ``busy_users`` names every user with a task
   holding a live worker, read from the task table by the scheduler wrapper.
   A user in that set is skipped *entirely* — not even the cheap reclaim, since
   ``uv cache prune`` unlinks as surely as ``clean`` does. This is the only
   guard that sees a sync against a fully warm cache, which writes nothing at
   all and merely hardlinks out. A caller that cannot answer the question must
   pass no sweep at all rather than an empty set; the wrapper does that.

   **The set is a snapshot and it ages across the sweep.** It is read once, so
   the caches visited last are judged against a reading that can be tens of
   minutes old by then — every cache costs a tree walk and up to four
   subprocesses, each bounded at :data:`_TOOL_TIMEOUT`. Re-reading it per user
   would mean this module holding a database handle, which is the one thing a
   leaf here must not do. Stated as a cost rather than left for a reader to
   work out from the call site.
2. **An idle window on the tree's newest mtime**, :data:`DEFAULT_MIN_IDLE_SECONDS`.
   This covers a writer the task table never knew about — an operator shell, a
   devbox, a task that started after the busy set was read — and it is
   deliberately short, because it is a backstop and not the policy. A cache
   being written *right now* is the case it catches, which is also the case
   guard 1's staleness is most likely to have missed.
3. **uv's own in-use check**, preserved by never passing ``--force`` to either
   ``cache prune`` or ``cache clean``. uv takes an exclusive lock on the cache
   and holds it for the whole of an install, and it is the last thing standing
   when guards 1 and 2 both lose a race with the kernel. **It blocks rather
   than refusing**, which is worth knowing because it changes what happens
   next: the call waits, :data:`_TOOL_TIMEOUT` eventually kills it, and the
   outcome is reported as a tool failure. Safe, and slow — a reclaim that has
   to wait is one that should have been skipped. It also runs the other way:
   this module's own ``clean`` holds that lock for the whole delete, so a
   task's ``uv sync`` starting mid-sweep queues behind it. ``npm``'s
   ``--force`` on ``cache clean`` is *not* the same flag: npm has no in-use
   check to bypass, it is refusing an operation it considers unnecessary, so
   the npm half rests on guards 1 and 2 alone.

The accepted cost, stated rather than implied: a deployment busy enough that
some user always has a task in flight will keep skipping that user, and the
ceiling becomes advisory for them. The skip is logged with its reason so that
shows up as a growing number rather than as silence. Skipping is the right way
round — a cache one interval too large costs disk, a wipe one second too early
costs a task.

**Containment.** ``resolve_sandbox_cache_dir`` creates ``{root}/{user_id}`` and
nothing deeper, so a candidate is a directory whose *resolved* parent is the
resolved root — one rule, which a symlink fails by construction. Anything else
is refused by name. That matters because the root's parent is normally
``developer.repos_dir``, which is bound read-write into an admin developer
task's sandbox — so the entries under the root are model-adjacent, and a
symlink planted there would otherwise aim a package manager's reclaim verb at a
directory of the model's choosing.

For the same reason the tools are run with their cwd in a fresh temporary
directory rather than in the cache, with ``uv --no-config``, with npm's user
and global config files pointed at ``os.devnull``, and with an environment
built from an allowlist rather than inherited whole — the daemon's own
environment carries the secret key and every module credential, and a process
whose job is to unlink files needs none of it. The per-user cache is
model-written by construction; a host-side tool started with its cwd inside it
would pick up a ``uv.toml`` or an ``.npmrc`` the model wrote, as the daemon
user. Same shape as the reasoning in :mod:`istota.git_hardening`, for a
different pair of programs.

A tool is run only where its own subdirectory exists, which also covers the one
asymmetry between the two: ``uv cache clean`` removes the cache *directory*
along with its contents, while ``npm cache clean`` empties one and leaves it.
Both are right — uv recreates it on the next sync, and the sweep after a wipe
simply has no uv half to reclaim.

**Measurement is du-style.** ``st_blocks * 512`` rather than ``st_size``,
because the ceiling exists to protect a volume and blocks are what fill one; an
inode is counted once, because uv's cache is full of hardlinks and counting a
shared inode per link would report an overage that no amount of reclaiming can
clear. ``os.walk`` with ``followlinks=False``, and a symlink is never followed
out of the tree.

**The ceiling counts the whole per-user directory, and only two tools can act
on it.** ``XDG_CACHE_HOME`` points at the user root, so a third tool's cache —
``huggingface/`` is the one shipped today — lands beside ``uv/`` and ``npm/``
and counts toward the budget while neither reclaim verb can touch it. That case
reports ``still-over`` and names the largest subdirectory rather than looping or
reaching for the filesystem, which is the honest answer and the one an operator
can act on.

**The subdirectory names are restated here, not imported.** They belong to
``executor.SANDBOX_CACHE_UV`` / ``SANDBOX_CACHE_NPM``, and importing
:mod:`istota.executor` would drag the whole task path into a maintenance
thread. ``tests/test_sandbox_cache_sweeper.py`` holds the two pairs equal, the
same way the forge-CLI version literals are held across the role and the two
Dockerfiles.

stdlib-only leaf: imports nothing from the package, takes its root and its
policy as parameters rather than reading a ``Config``, and never raises.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
from collections.abc import Collection, Iterator
from pathlib import Path
from typing import NamedTuple

logger = logging.getLogger("istota.sandbox_cache_sweeper")

# Mirrors executor.SANDBOX_CACHE_UV / SANDBOX_CACHE_NPM — see the module
# docstring for why these are a copy and what holds them equal.
CACHE_UV = "uv"
CACHE_NPM = "npm"

# Bytes per block in the unit `st_blocks` is defined in. POSIX fixes it at 512
# regardless of the filesystem's own block size.
_BLOCK = 512

# The default budget, per user. Two full `uv sync --extra test` sets plus their
# npm counterparts, with room for the wheels a second Python version pulls in —
# large enough that a working deployment never trips it, small enough that a
# 40 GB volume shared with `developer.repos_dir` survives a handful of users.
DEFAULT_MAX_BYTES = 10 * 1024 ** 3

# The floor the ceiling is clamped to. Below roughly a gigabyte the ceiling is
# under the working set of a *single* dependency resolution, so every sweep
# would wipe a cache that is doing its job and the next task would re-download
# the same bytes — the exact behaviour ISSUE-305 removed, restored by a config
# typo. The knob stays useful; it just cannot be set to "never keep anything".
MIN_MAX_BYTES = 1024 ** 3

# How long the cache tree must have been unwritten before the sweep will act.
# Guard 2 in the module docstring: a backstop for a writer the caller's
# in-flight set never knew about, not the policy.
DEFAULT_MIN_IDLE_SECONDS = 900.0

# Per invocation. `npm cache verify` walks every entry in the cache index and a
# cold, large cache is genuinely slow, so this is generous — it bounds a wedged
# binary rather than a busy one.
_TOOL_TIMEOUT = 900

ACTION_OUTSIDE = "outside"          # not a direct child of the root; nothing run
ACTION_BUSY = "busy"                # the user has a task in flight
ACTION_RECENT = "recent"            # something wrote into the cache too recently
ACTION_FUTURE_MTIME = "future-mtime"  # stamped ahead of the clock; a pin or a clock fault
ACTION_NO_TOOLS = "no-tools"        # over the ceiling with no reclaim verb available
ACTION_RECLAIMED = "reclaimed"      # swept, and inside the ceiling afterwards
ACTION_WIPED = "wiped"              # escalated to a full clean, and now inside it
ACTION_STILL_OVER = "still-over"    # everything available was run and it is still over


class CacheSize(NamedTuple):
    """Disk usage of a tree, and the newest mtime anywhere in it."""

    bytes: int
    newest_mtime: float


class SweepOutcome(NamedTuple):
    user_id: str
    path: Path
    action: str
    before_bytes: int
    after_bytes: int
    detail: str = ""


# --------------------------------------------------------------------------
# Measurement
# --------------------------------------------------------------------------

def measure_cache(path: Path) -> CacheSize:
    """Disk usage and newest mtime of ``path``, du-style. Never raises.

    An inode is counted once: uv's cache hardlinks aggressively, and counting a
    shared inode per link reports an overage that reclaiming cannot clear.
    Symlinks are stat'd but never followed, so nothing outside the tree is
    counted and nothing outside it can be reached.
    """
    total = 0
    newest = 0.0
    seen: set[tuple[int, int]] = set()

    def _on_error(exc: OSError) -> None:
        logger.debug("sandbox_cache_sweeper: skipping %s (%s)", getattr(exc, "filename", "?"), exc)

    try:
        if not path.is_dir():
            return CacheSize(0, 0.0)
        root_stat = path.lstat()
        newest = root_stat.st_mtime
    except OSError:
        return CacheSize(0, 0.0)

    for dirpath, dirnames, filenames in os.walk(path, onerror=_on_error, followlinks=False):
        for name in (*dirnames, *filenames):
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if info.st_mtime > newest:
                newest = info.st_mtime
            key = (info.st_dev, info.st_ino)
            if key in seen:
                continue
            seen.add(key)
            total += info.st_blocks * _BLOCK
    return CacheSize(total, newest)


def _largest_child(path: Path) -> tuple[str, int]:
    """The biggest immediate subdirectory of ``path``, for the ``still-over`` note."""
    biggest = ("", 0)
    try:
        entries = sorted(path.iterdir())
    except OSError:
        return biggest
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        size = measure_cache(entry).bytes
        if size > biggest[1]:
            biggest = (entry.name, size)
    return biggest


# --------------------------------------------------------------------------
# Containment
# --------------------------------------------------------------------------

def _candidates(root: Path) -> Iterator[tuple[Path, bool]]:
    """Each entry in ``root`` with whether it is a per-user cache we may act on.

    ``resolve_sandbox_cache_dir`` creates ``{root}/{user_id}`` and nothing
    deeper, so a candidate is a directory that **resolves to its own name
    inside the root** — ``entry.resolve() == root.resolve() / entry.name``. A
    directory that fails that yields ``False`` and is reported rather than
    silently skipped: a planted symlink is the interesting case, and a sweep
    that quietly ignored one would look identical to a sweep that found
    nothing.

    **Equality, not "the resolved parent is the root".** The weaker test reads
    as though it excludes every symlink and does not: ``{root}/zzz`` pointing at
    ``{root}/bob`` resolves to a path whose parent *is* the root, so it passes
    — and then ``user_id`` is taken from the entry, so the busy check asks
    whether ``zzz`` has a task in flight while the reclaim verb runs against
    bob's real cache. That is guard 1 defeated by a name, and it was found by
    review after the weaker rule had been written down as sufficient. Requiring
    the resolved path to carry the same name closes both that and the
    target-outside-the-root case in one comparison. It matters
    because the root's parent can be a directory bound read-write into a task's
    sandbox: an entry here can then be model-planted, and following one would
    aim a package manager's reclaim verb at a directory of the model's
    choosing.

    **What is yielded is the resolved path, not the entry as read**, and that is
    the half that makes the rule worth anything. The check and the use are
    separated by a full tree walk and up to four subprocesses, so a validated
    ``{root}/alice`` that is handed on unresolved can be renamed away and
    replaced with a symlink inside the window. A resolved path has no symlink
    component left in it, so there is nothing to swap.
    """
    try:
        resolved_root = root.resolve()
        entries = sorted(root.iterdir())
    except OSError as exc:
        logger.warning("sandbox_cache_sweeper: %s is unreadable (%s); nothing swept.", root, exc)
        return

    for entry in entries:
        try:
            # A plain file in the root is not a cache and nothing here would
            # remove one; it needs no outcome row.
            if not entry.is_dir():
                continue
            resolved = entry.resolve()
            if resolved != resolved_root / entry.name:
                yield entry, False
                continue
        except OSError:
            continue
        # The *resolved* path is what goes on, never the entry as read. The
        # check and the use are separated by a full tree walk and two
        # subprocesses, and the model can rename its own cache directory and
        # drop a symlink in its place inside that window — so validating one
        # path and handing a different one to `uv cache clean` would leave the
        # containment rule describing a check nothing acted on. A resolved path
        # contains no symlink component, so there is nothing left to swap.
        yield resolved, True


# --------------------------------------------------------------------------
# Running a package manager's own reclaim verb
# --------------------------------------------------------------------------

# What a reclaim verb is allowed to inherit. An allowlist, not the daemon's
# environment minus a few names: on a deployment the daemon is started from a
# systemd `EnvironmentFile` and carries the secret key, the Nextcloud app
# password and every module credential, and none of that is any business of a
# subprocess whose whole job is to unlink files. It is also the only way to be
# sure of the two variables that would quietly break the sweep — an inherited
# `npm_config_cache` redirects the reclaim, and `UV_NO_CACHE` makes uv work out
# of a temporary directory so the prune reclaims nothing — since npm reads its
# entire configuration out of that namespace and a deny-list has to guess at
# every spelling of it.
_INHERITED_ENV = ("PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE", "TZ")


def _tool_env(uv_dir: Path, npm_dir: Path) -> dict[str, str]:
    """A minimal environment with the cache locations pinned and config disarmed."""
    env = {
        key: os.environ[key] for key in _INHERITED_ENV if key in os.environ
    }
    env["UV_CACHE_DIR"] = str(uv_dir)
    env["npm_config_cache"] = str(npm_dir)
    # ~/.npmrc and /usr/etc/npmrc are outside this module's control and inside
    # the model's reach on some deployments; `--no-config` is uv's equivalent.
    env["npm_config_userconfig"] = os.devnull
    env["npm_config_globalconfig"] = os.devnull
    return env


def _run(argv: list[str], cwd: str, env: dict[str, str]) -> tuple[bool, str]:
    """Run one reclaim verb. Returns (succeeded, detail). Never raises."""
    try:
        proc = subprocess.run(
            argv, cwd=cwd, env=env,
            capture_output=True, text=True, timeout=_TOOL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        return False, f"{Path(argv[0]).name} timed out after {_TOOL_TIMEOUT}s"
    except OSError as exc:
        return False, f"{Path(argv[0]).name} could not be run ({exc})"
    if proc.returncode != 0:
        tail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, f"{Path(argv[0]).name} exited {proc.returncode}: {tail[-1] if tail else ''}"
    return True, ""


def _uv_argv(binary: str, uv_dir: Path, verb: str) -> list[str]:
    # `--cache-dir` on the argv as well as `UV_CACHE_DIR` in the environment, so
    # an inherited variable cannot redirect the removal. Never `--force`: that
    # bypasses uv's in-use check, which is guard 3.
    return [binary, "--no-config", "--cache-dir", str(uv_dir), "cache", verb]


def _npm_argv(binary: str, npm_dir: Path, verb: str) -> list[str]:
    argv = [binary, "cache", verb, "--cache", str(npm_dir)]
    if verb == "clean":
        # npm's `--force` is not uv's: there is no in-use check behind it, it is
        # npm declining an operation it considers unnecessary. The npm half is
        # protected by guards 1 and 2 alone, which is why it is safe to pass.
        argv.append("--force")
    return argv


def _reclaim(
    user_dir: Path, verbs: tuple[str, str], uv_bin: str | None, npm_bin: str | None,
) -> tuple[int, int, list[str]]:
    """Run one round (``verbs`` = the uv verb and the npm verb).

    Returns ``(ran, missing, notes)``. ``missing`` counts a tool whose cache
    subdirectory is *there* and whose binary is not — which is a different
    outcome from a cache that simply holds nothing of that tool's, and the
    caller reports the two differently.
    """
    uv_dir = user_dir / CACHE_UV
    npm_dir = user_dir / CACHE_NPM
    env = _tool_env(uv_dir, npm_dir)
    ran = 0
    missing = 0
    notes: list[str] = []

    with tempfile.TemporaryDirectory(prefix="istota-cache-sweep-") as cwd:
        for binary, name, directory, argv in (
            (uv_bin, "uv", uv_dir, _uv_argv(uv_bin or "", uv_dir, verbs[0])),
            (npm_bin, "npm", npm_dir, _npm_argv(npm_bin or "", npm_dir, verbs[1])),
        ):
            if not directory.is_dir():
                continue
            if not binary:
                missing += 1
                notes.append(f"{name} is not installed, so its cache was not reclaimed")
                continue
            ok, detail = _run(argv, cwd, env)
            ran += 1
            if not ok:
                notes.append(detail)
    return ran, missing, notes


# --------------------------------------------------------------------------
# The sweep
# --------------------------------------------------------------------------

def sweep_caches(
    root: Path | str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    busy_users: Collection[str] = (),
    min_idle_seconds: float = DEFAULT_MIN_IDLE_SECONDS,
    floor_bytes: int = MIN_MAX_BYTES,
    now: float | None = None,
) -> list[SweepOutcome]:
    """Bring every per-user cache under ``root`` inside ``max_bytes``. Never raises.

    ``floor_bytes`` is the clamp :data:`MIN_MAX_BYTES` describes. It is a
    parameter only so the tests can exercise the ceiling on kilobytes instead of
    on gigabytes; no caller passes anything but the default.

    ``now`` is likewise injected for the tests. Everything else comes from the
    caller because this module reads no configuration of its own.
    """
    root_path = Path(root)
    if max_bytes < floor_bytes:
        logger.warning(
            "sandbox_cache_sweeper: a ceiling of %d bytes is below the %d-byte floor "
            "and would wipe a cache after every single dependency resolution; "
            "using the floor.", max_bytes, floor_bytes,
        )
        max_bytes = floor_bytes

    uv_bin = shutil.which("uv")
    npm_bin = shutil.which("npm")
    stamp = time.time() if now is None else now
    busy = set(busy_users)
    outcomes: list[SweepOutcome] = []

    for entry, usable in _candidates(root_path):
        if not usable:
            outcomes.append(SweepOutcome(
                entry.name, entry, ACTION_OUTSIDE, 0, 0,
                "not a directory directly inside the configured root",
            ))
            continue
        # Per user, so one cache that blows up in an unforeseen way cannot end
        # the sweep and leave every later one unswept while the caller reads the
        # result as complete.
        try:
            outcomes.append(_sweep_one(
                entry, max_bytes, busy, min_idle_seconds, stamp, uv_bin, npm_bin,
            ))
        except Exception:  # noqa: BLE001 — see above
            logger.exception("sandbox_cache_sweeper: sweeping %s failed", entry)
    return outcomes


def _sweep_one(
    user_dir: Path,
    max_bytes: int,
    busy: set[str],
    min_idle_seconds: float,
    stamp: float,
    uv_bin: str | None,
    npm_bin: str | None,
) -> SweepOutcome:
    user_id = user_dir.name
    before = measure_cache(user_dir)

    if user_id in busy:
        return SweepOutcome(
            user_id, user_dir, ACTION_BUSY, before.bytes, before.bytes,
            "a task for this user is in flight",
        )
    # **Clamped, because this mtime is model-controlled.** The tree is bound
    # read-write into that user's own sandbox, so one `touch -d '+10 years'`
    # inside it makes `idle` negative, which is below any window — and the cache
    # is then pinned for good, which is the unbounded disk leak this module
    # exists to prevent, restored by a single command. Without the clamp it is
    # also invisible: `recent` is not warned, so an operator sees a count and
    # not a growing cache, and the negative duration in the detail reads as an
    # arithmetic bug rather than as a boundary being pushed on.
    #
    # A future stamp gets its own outcome rather than being quietly clamped into
    # `recent`. It is either a clock problem or a deliberate pin, both of which
    # need somebody to look, and the guard it defeats is the one protecting a
    # running task — so the sweep still declines to act on this pass and says
    # loudly why.
    if before.newest_mtime > stamp:
        return SweepOutcome(
            user_id, user_dir, ACTION_FUTURE_MTIME, before.bytes, before.bytes,
            f"newest mtime is {before.newest_mtime - stamp:.0f}s in the future; "
            "not sweeping on a timestamp this cache's own writer controls",
        )
    idle = stamp - before.newest_mtime
    if idle < min_idle_seconds:
        return SweepOutcome(
            user_id, user_dir, ACTION_RECENT, before.bytes, before.bytes,
            f"written {idle:.0f}s ago, inside the {min_idle_seconds:.0f}s idle window",
        )

    ran, missing, notes = _reclaim(user_dir, ("prune", "verify"), uv_bin, npm_bin)
    after = measure_cache(user_dir)
    if after.bytes <= max_bytes:
        return SweepOutcome(
            user_id, user_dir, ACTION_RECLAIMED, before.bytes, after.bytes,
            "; ".join(notes),
        )

    # **Escalate only against an overage a reclaim verb can actually reach.**
    # `XDG_CACHE_HOME` points at the user root and that root is bound
    # read-write into the user's own sandbox, so bytes can sit beside `uv/` and
    # `npm/` in a directory neither `clean` verb touches. Measuring the whole
    # directory and escalating on it means a single large file in a third
    # subdirectory wipes both real caches on every sweep, for good, while the
    # total never comes under the ceiling — every task re-downloading every
    # time, which is precisely the pre-ISSUE-305 behaviour the `MIN_MAX_BYTES`
    # floor exists to prevent, arriving by another road. So the wipe is decided
    # on the reclaimable portion, and an overage outside it is reported instead.
    reclaimable = sum(
        measure_cache(user_dir / name).bytes for name in (CACHE_UV, CACHE_NPM)
    )
    # Wiping can only ever remove `reclaimable`, so it can only ever get the
    # total down to `unreclaimable`. If that alone is already over the ceiling,
    # the wipe cannot succeed and its only effect is to throw away two working
    # caches.
    unreclaimable = after.bytes - reclaimable
    if unreclaimable > max_bytes:
        name, size = _largest_child(user_dir)
        note = (
            f"{_human(unreclaimable)} of this cache is outside {CACHE_UV}/ and "
            f"{CACHE_NPM}/, which neither reclaim verb can touch"
        )
        if name:
            note += f"; largest subdirectory is {name} ({_human(size)})"
        notes.append(note)
        return SweepOutcome(user_id, user_dir, ACTION_STILL_OVER,
                            before.bytes, after.bytes, "; ".join(notes))

    # **The liveness decision is re-taken before the wipe, not carried over.**
    # The prune round can take a long time, and the delay correlates with the
    # hazard rather than being independent of it: uv holds an exclusive lock on
    # the cache for the whole of an install, and `uv cache prune` *blocks* on a
    # held lock rather than refusing, so the round stalls for exactly as long as
    # a task is syncing. Carrying the earlier reading into the escalation would
    # fire the wipe on evidence gathered before that task existed. Same idea as
    # `worktree_reaper` repeating its dirty check immediately before the delete.
    #
    # The busy set is not re-read here; that would mean holding a database
    # handle, which this module does not do. The mtime is what is available, and
    # a sync that stalled the prune round has written into the cache to do it.
    fresh = measure_cache(user_dir)
    if fresh.newest_mtime > before.newest_mtime:
        notes.append(
            "something wrote into this cache during the reclaim; not escalating"
        )
        return SweepOutcome(user_id, user_dir, ACTION_RECENT,
                            before.bytes, fresh.bytes, "; ".join(notes))

    wiped, wipe_missing, wipe_notes = _reclaim(user_dir, ("clean", "clean"), uv_bin, npm_bin)
    notes.extend(n for n in wipe_notes if n not in notes)
    after = measure_cache(user_dir)

    # Nothing ran and something should have: the caches are there and the tool
    # that owns them is not installed. Report that rather than deleting by hand.
    if ran == 0 and wiped == 0 and (missing or wipe_missing):
        return SweepOutcome(
            user_id, user_dir, ACTION_NO_TOOLS, before.bytes, after.bytes,
            "; ".join(notes) or "no package manager available to reclaim this cache",
        )
    if after.bytes <= max_bytes:
        return SweepOutcome(user_id, user_dir, ACTION_WIPED, before.bytes, after.bytes,
                            "; ".join(notes))

    name, size = _largest_child(user_dir)
    if name:
        notes.append(f"largest remaining subdirectory is {name} ({_human(size)})")
    return SweepOutcome(user_id, user_dir, ACTION_STILL_OVER, before.bytes, after.bytes,
                        "; ".join(notes))


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TiB"


def sweep_and_report(
    root: Path | str,
    *,
    max_bytes: int = DEFAULT_MAX_BYTES,
    busy_users: Collection[str] = (),
    min_idle_seconds: float = DEFAULT_MIN_IDLE_SECONDS,
) -> list[SweepOutcome]:
    """:func:`sweep_caches`, logged.

    A line for each cache that was acted on or could not be, and one summary
    line counting the rest. The skipped set is the number an operator needs to
    see growing — a deployment where every sweep skips the same user is one
    where the ceiling has quietly stopped applying.
    """
    try:
        outcomes = sweep_caches(
            root, max_bytes=max_bytes, busy_users=busy_users,
            min_idle_seconds=min_idle_seconds,
        )
    except Exception:  # noqa: BLE001 — a periodic sweep must not kill its thread
        logger.exception("sandbox_cache_sweeper: sweep of %s failed", root)
        return []

    skipped: dict[str, int] = {}
    for outcome in outcomes:
        if outcome.action in (
            ACTION_WIPED, ACTION_STILL_OVER, ACTION_NO_TOOLS, ACTION_FUTURE_MTIME,
        ):
            level = logger.info if outcome.action == ACTION_WIPED else logger.warning
            level(
                "sandbox_cache_sweeper: %s cache for %s — %s to %s%s",
                outcome.action, outcome.user_id,
                _human(outcome.before_bytes), _human(outcome.after_bytes),
                f" ({outcome.detail})" if outcome.detail else "",
            )
        elif outcome.action == ACTION_RECLAIMED and (
            outcome.after_bytes < outcome.before_bytes or outcome.detail
        ):
            # The `detail` arm matters on its own: a cache under its ceiling
            # with npm missing reclaims nothing and says so, and dropping that
            # line means the operator first hears about it months later when the
            # cache crosses the line.
            logger.info(
                "sandbox_cache_sweeper: reclaimed the cache for %s — %s to %s%s",
                outcome.user_id,
                _human(outcome.before_bytes), _human(outcome.after_bytes),
                f" ({outcome.detail})" if outcome.detail else "",
            )
        else:
            skipped[outcome.action] = skipped.get(outcome.action, 0) + 1

    if skipped:
        logger.info(
            "sandbox_cache_sweeper: took no bytes from %d cache(s): %s.",
            sum(skipped.values()),
            ", ".join(f"{count} {action}" for action, count in sorted(skipped.items())),
        )
    return outcomes
