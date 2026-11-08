"""Whether anything the pollers read in Nextcloud changed, asked of Nextcloud (#939).

The TASKS.md poll, the CRON.md sync and the shared-file organizer used to find
out by reading the rclone mount on a timer, and every read through an expired
directory cache re-listed the tree from the root down. Nextcloud changes a
folder's ETag whenever anything below it changes (measured against Nextcloud
30 for a user's edit through a share and for a new share to the bot, in
`tests/full/test_tree_watch.py`), so one depth-0 PROPFIND on the bot's root
answers "did anything change" for the whole tree.

**One pass** (`run_pass`, the scheduler's `tree-watch` gate): a depth-0
PROPFIND on the root. If its ETag is the one stored, the pass is over. If not,
a depth-1 PROPFIND on the root, and on each child directory that changed and
lies on a watched path, down to the watched paths themselves, which are never
listed: their own ETag, read from their parent's listing, says whether
anything below them changed. Watched: the root's entries (the organizer),
each user's `{bot_dir}/config/` and `{bot_dir}/scripts/`, and whatever a
consumer registered (`watch`, the directories of a CRON.md `prompt_file`).

**Generations, not a dirty flag.** Each watched path has a counter that moves
when the path changes, and each consumer keeps the snapshot it last ran on
(`should_run` / `record_run`): TASKS.md and CRON.md share `config/`, so a flag
one consumer cleared would hide the change from the other. The root's counter
moves only when its entries (name and file id) change, since its ETag moves on
every write anywhere.

**The mount is told.** A changed watched directory, and any other changed
directory a listing turns up (a user's `inbox/`, `Channels/`), is dropped
from rclone's directory cache (`vfs/forget`, lazy: only what is read again is
listed again), and a listed directory whose entries or files changed is
listed again (`vfs/refresh`), before any consumer reads it. So the mount's
long `--dir-cache-time` serves no listing older than the pass that saw it
change. If the rc cannot be reached, the watched paths it should have
freshened change again once `DIR_CACHE_SECONDS` have passed, when the mount's
own cache has expired, so a consumer that read a stale listing reads again.

**Off means "always changed".** With no pass yet, after a failed pass, on a
local workspace, and for a root on storage that is not Nextcloud's own (an
external storage notices a change on disk only when a folder is listed),
`should_run` answers True, and every consumer reads on its own interval. On
that last shape the whole directory cache is also dropped every pass, which
is what the mount's old 30-second cache did. With `tree_watch_interval = 0`
nothing tells the mount anything, and a change made in Nextcloud takes up to
the mount's ten minutes to show. Each consumer also runs unconditionally once
every `HEAL_SECONDS`, for a change Nextcloud failed to propagate.

State is in memory in the scheduler's process and starts empty, so a restarted
daemon treats everything as changed on its first pass.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from ..config import Config
from . import rclone_rc

logger = logging.getLogger("istota.nextcloud.tree_watch")

#: The mount's `--dir-cache-time` in `host/mount-nextcloud.service`, held equal
#: by `tests/test_host_units.py`.
DIR_CACHE_SECONDS = 600
#: How long a consumer may go without an unconditional pass.
HEAL_SECONDS = 3600
#: Per request. The pass runs on the scheduler's loop thread.
REQUEST_TIMEOUT = 10.0

ROOT = "/"


@dataclass
class _State:
    active: bool = False
    #: Directory path -> the ETag last seen for it (the root's from its own
    #: depth-0 answer, every other one from its parent's listing).
    etags: dict[str, str] = field(default_factory=dict)
    #: Listed directory -> its entries as (name, file id).
    entries: dict[str, frozenset[tuple[str, str]]] = field(default_factory=dict)
    #: Watched path -> generation. Absent means not observed yet.
    generations: dict[str, int] = field(default_factory=dict)
    #: The root's last listing, for the organizer.
    root_listing: list[dict] | None = None
    #: Watched path -> monotonic time at which to move its generation again.
    rebump_at: dict[str, float] = field(default_factory=dict)
    #: Consumer registrations: key -> extra watched directories.
    extra: dict[str, frozenset[str]] = field(default_factory=dict)
    #: Consumer key -> (snapshot it ran on, monotonic time of the run).
    runs: dict[str, tuple[tuple, float]] = field(default_factory=dict)
    failing: bool = False
    untrusted: bool = False
    rc_failing: bool = False


_lock = threading.Lock()
_state = _State()


def reset() -> None:
    """Forget everything (daemon start, tests)."""
    global _state
    with _lock:
        _state = _State()


# --- paths ------------------------------------------------------------------


def _norm(path: str) -> str:
    clean = "/" + (path or "").strip().strip("/")
    return clean if clean != "/" else ROOT


def _parent(path: str) -> str:
    head = path.rsplit("/", 1)[0]
    return head or ROOT


def _ancestors(path: str) -> list[str]:
    """Every directory above `path`, root first, not `path` itself."""
    out = [ROOT]
    parts = [p for p in path.split("/") if p]
    for i in range(1, len(parts)):
        out.append("/" + "/".join(parts[:i]))
    return out if path != ROOT else []


def _is_under(path: str, top: str) -> bool:
    return top == ROOT or path == top or path.startswith(top + "/")


def user_config_dir(config: Config, user_id: str) -> str:
    return f"/Users/{user_id}/{config.bot_dir_name}/config"


def user_scripts_dir(config: Config, user_id: str) -> str:
    return f"/Users/{user_id}/{config.bot_dir_name}/scripts"


def watched_paths(config: Config) -> set[str]:
    """Every path a generation is kept for."""
    out = {ROOT}
    for user_id in config.users:
        out.add(user_config_dir(config, user_id))
        out.add(user_scripts_dir(config, user_id))
    with _lock:
        for paths in _state.extra.values():
            out |= paths
    return out


def watch(key: str, paths: Iterable[str]) -> None:
    """Register extra directories under `key`, replacing what it held before.

    A path below another watched one is needless (the watched one's ETag
    already moves for it) and costs a listing on each change, so callers leave
    those out.
    """
    normalised = {_norm(p) for p in paths if p}
    with _lock:
        _state.extra[key] = frozenset(normalised)


def registered(key: str) -> list[str]:
    """What `key` registered, sorted."""
    with _lock:
        return sorted(_state.extra.get(key, ()))


# --- consumers ----------------------------------------------------------------


def snapshot(paths: Sequence[str]) -> tuple | None:
    """The generations of `paths`, or None while the watcher cannot answer."""
    with _lock:
        if not _state.active:
            return None
        return tuple(_state.generations.get(_norm(p)) for p in paths)


def should_run(key: str, paths: Sequence[str], *, now: float | None = None) -> tuple[bool, tuple | None]:
    """Whether consumer `key` has something to read under `paths`, and the
    snapshot to hand `record_run` once it has run."""
    now = time.monotonic() if now is None else now
    snap = snapshot(paths)
    with _lock:
        last = _state.runs.get(key)
    if snap is None or last is None:
        return True, snap
    if None in snap:
        return True, snap
    if now - last[1] >= HEAL_SECONDS:
        return True, snap
    return snap != last[0], snap


def record_run(key: str, snap: tuple | None, *, now: float | None = None) -> None:
    now = time.monotonic() if now is None else now
    with _lock:
        _state.runs[key] = (snap, now)


def heal_due(key: str, *, now: float | None = None) -> bool:
    """Whether `key`'s next run is its unconditional one (or its first)."""
    now = time.monotonic() if now is None else now
    with _lock:
        last = _state.runs.get(key)
    return last is None or now - last[1] >= HEAL_SECONDS


def mark_changed(path: str) -> None:
    """Move a path's generation without a change seen in Nextcloud: a write
    that failed and is left for the next pass of its consumer to finish."""
    path = _norm(path)
    with _lock:
        if path in _state.generations:
            _state.generations[path] += 1


def root_listing() -> list[dict] | None:
    """The root's entries as the last pass listed them, or None while the
    watcher cannot answer."""
    with _lock:
        if not _state.active or _state.root_listing is None:
            return None
        return list(_state.root_listing)


# --- the pass -----------------------------------------------------------------


@dataclass
class PassResult:
    requests: int = 0
    changed: list[str] = field(default_factory=list)
    forgotten: list[str] = field(default_factory=list)
    refreshed: list[str] = field(default_factory=list)
    error: str = ""


def _bump(generations: dict[str, int], path: str) -> None:
    generations[path] = generations.get(path, 0) + 1


def _propfind(config: Config, path: str, depth: int) -> list[dict]:
    from . import dav

    return dav.propfind(config, path, depth=depth, timeout=REQUEST_TIMEOUT)


def run_pass(config: Config, *, now: float | None = None) -> PassResult:
    """One watch pass. Never raises."""
    now = time.monotonic() if now is None else now
    result = PassResult()
    watched = watched_paths(config)
    on_tree: set[str] = set()
    for path in watched:
        on_tree.update(_ancestors(path))

    with _lock:
        for path, at in list(_state.rebump_at.items()):
            if now >= at:
                _bump(_state.generations, path)
                del _state.rebump_at[path]
        old_etags = dict(_state.etags)
        old_entries = dict(_state.entries)
        generations = dict(_state.generations)
        started_from = dict(generations)
        was_active = _state.active

    try:
        root = _propfind(config, "", 0)
        result.requests += 1
        if not root:
            raise ValueError("empty answer for the root")
        root = root[0]
        if root.get("mount_type"):
            # Nextcloud keeps ETags itself only for its own storage (an
            # external storage notices a change on disk when a folder is
            # listed; a share's are not measured), so the root's ETag says
            # nothing reliable about a deep edit.
            with _lock:
                first = not _state.untrusted
                _state.untrusted = True
                _state.active = False
            if first:
                logger.warning(
                    "tree watch: the bot's root is %s storage, not Nextcloud's own, so "
                    "its ETag does not say what changed below it; every poller reads "
                    "the mount on its own interval and the mount's directory cache is "
                    "dropped each pass", root.get("mount_type"),
                )
            result.error = "untrusted_storage"
            # The mount's cache is long because of the watcher; without one it
            # is dropped every pass, which is what a 30 s cache did before.
            _tell_the_mount(watched, [], [], now, result, everything=True)
            return result

        unobserved = [p for p in watched if p not in generations]
        if was_active and not unobserved and old_etags.get(ROOT) == root.get("etag"):
            return result

        new_etags = dict(old_etags)
        new_entries = dict(old_entries)
        new_etags[ROOT] = root.get("etag", "")
        refresh: list[str] = []
        forget: list[str] = []
        listing = _walk(
            config, ROOT, watched=watched, on_tree=on_tree, old_etags=old_etags,
            new_etags=new_etags, new_entries=new_entries, generations=generations,
            refresh=refresh, forget=forget, result=result,
        )
    except Exception as exc:  # noqa: BLE001 - a pass never raises into the loop
        with _lock:
            first = not _state.failing
            _state.failing = True
            _state.active = False
        if first:
            logger.warning(
                "tree watch: a pass failed (%s); every poller reads the mount on its "
                "own interval until one succeeds", type(exc).__name__,
            )
        result.error = type(exc).__name__
        return result

    with _lock:
        # A `mark_changed` or a rebump that landed while this pass was on the
        # network moved the live counters; keep those moves.
        for path, value in _state.generations.items():
            moved = value - started_from.get(path, 0)
            if moved > 0:
                generations[path] = generations.get(path, 0) + moved
        _state.etags = new_etags
        _state.entries = new_entries
        _state.generations = generations
        _state.root_listing = listing
        _state.active = True
        if _state.failing or _state.untrusted:
            logger.info("tree watch: passes succeed again")
        _state.failing = False
        _state.untrusted = False

    # The first pass knows nothing of what the mount cached before this
    # process started (the mount outlives a daemon restart): drop all of it.
    _tell_the_mount(watched, forget, refresh, now, result, everything=not old_etags)
    return result


def _walk(
    config: Config,
    directory: str,
    *,
    watched: set[str],
    on_tree: set[str],
    old_etags: dict[str, str],
    new_etags: dict[str, str],
    new_entries: dict[str, frozenset[tuple[str, str]]],
    generations: dict[str, int],
    refresh: list[str],
    forget: list[str],
    result: PassResult,
) -> list[dict]:
    """List `directory`, tell the mount about whatever in it changed, and go
    down the children on a watched path. Returns the listing (without the
    directory itself) for the root, and nothing for the others.

    Every changed directory below the root is either on a watched path, and
    listed in turn, or off it, and dropped from the mount's cache whole
    (forget is lazy: only what is read again is listed again). So no
    directory on the mount serves a listing older than the pass that saw it
    change, watched or not.
    """
    answer = _propfind(config, directory, 1)
    result.requests += 1
    children = [e for e in answer if _norm(e.get("path", "")) != directory]
    names = frozenset((e.get("name", ""), e.get("fileid", "")) for e in children)
    if new_entries.get(directory) != names:
        refresh.append(directory)
        new_entries[directory] = names
        if directory == ROOT:
            _bump(generations, ROOT)
            result.changed.append(ROOT)
    elif directory == ROOT and ROOT not in generations:
        _bump(generations, ROOT)

    present = {}
    listed = set()
    for entry in children:
        path = _norm(entry.get("path", ""))
        listed.add(path)
        etag = entry.get("etag", "")
        before = old_etags.get(path)
        new_etags[path] = etag
        if entry.get("is_dir"):
            present[path] = entry
            off_tree = path not in watched and path not in on_tree
            if off_tree and before is not None and before != etag:
                forget.append(path)
        elif before is not None and before != etag:
            # An edited file in a listed directory: its size and time in the
            # mount's listing are the old ones.
            refresh.append(directory)

    for path in [p for p in old_etags if p != ROOT and _parent(p) == directory and p not in listed]:
        for stale in [p for p in new_etags if _is_under(p, path)]:
            del new_etags[stale]

    for path in sorted(p for p in watched | on_tree if _parent(p) == directory and p != ROOT):
        entry = present.get(path)
        etag = entry.get("etag", "") if entry else None
        before = old_etags.get(path)
        changed = etag != before
        below_unobserved = any(_is_under(w, path) and w not in generations for w in watched)
        if path in watched and (changed or path not in generations):
            _bump(generations, path)
            result.changed.append(path)
            if etag is not None:
                forget.append(path)
        if path in on_tree and etag is not None and (changed or below_unobserved):
            _walk(
                config, path, watched=watched, on_tree=on_tree, old_etags=old_etags,
                new_etags=new_etags, new_entries=new_entries, generations=generations,
                refresh=refresh, forget=forget, result=result,
            )
        elif path in on_tree and etag is None:
            # Gone, or never there: whatever is watched below it is observed
            # as absent, and moves again when it appears.
            for w in watched:
                if _is_under(w, path) and w != path and (w not in generations or before is not None):
                    _bump(generations, w)
                    result.changed.append(w)
    return children if directory == ROOT else []


def _rc_path(path: str) -> str:
    return "" if path == ROOT else path.lstrip("/")


def _tell_the_mount(
    watched: set[str], forget: list[str], refresh: list[str], now: float, result: PassResult,
    *, everything: bool = False,
) -> None:
    failed: list[str] = []
    reason = ""
    if everything:
        answer = rclone_rc.forget_all()
        if answer.ok:
            result.forgotten = [ROOT]
        else:
            failed.extend(watched)
            reason = answer.reason
        forget, refresh = [], []
    # A forgotten directory's subtree is re-read on the next access, so a
    # forget below another forget is redundant.
    forget = sorted(set(forget))
    forget = [p for p in forget if not any(o != p and _is_under(p, o) for o in forget)]
    # Parents first: a child is found through its parent's listing.
    refresh = sorted(set(refresh), key=lambda p: (0 if p == ROOT else p.count("/"), p))
    if forget:
        answer = rclone_rc.forget_dirs([_rc_path(p) for p in forget])
        if answer.ok:
            result.forgotten = forget
        else:
            failed.extend(w for w in watched if any(_is_under(w, f) for f in forget))
            reason = answer.reason
    if refresh:
        answer = rclone_rc.refresh_dirs([_rc_path(p) for p in refresh])
        if answer.ok:
            result.refreshed = refresh
        else:
            for top in refresh:
                failed.extend(w for w in watched if _is_under(w, top))
            reason = reason or answer.reason
    with _lock:
        for path in set(failed):
            _state.rebump_at.setdefault(path, now + DIR_CACHE_SECONDS)
        first = bool(failed) and not _state.rc_failing
        recovered = not failed and bool(forget or refresh or everything) and _state.rc_failing
        if failed:
            _state.rc_failing = True
        elif recovered:
            _state.rc_failing = False
    if first:
        logger.warning(
            "tree watch: could not freshen the mount's directory cache (%s); a change "
            "in Nextcloud may take up to %d minutes to reach the pollers",
            reason, DIR_CACHE_SECONDS // 60,
        )
    elif recovered:
        logger.info("tree watch: the mount's rc answers again")
