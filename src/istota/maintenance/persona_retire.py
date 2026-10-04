"""Retire the per-user ``PERSONA.md`` copies, once.

Every user used to get a seeded copy of the shipped persona at
``{root}/Users/{uid}/{bot_dir}/config/PERSONA.md``, and the prompt preferred it.
The operator's ``{root}/PERSONA.md`` is now the only persona, so those copies
are read by nothing. This removes them:

- a copy whose digest matches **any** shipped version, or the operator's
  current copy, or that is empty, is deleted. "Any" matters: a copy seeded
  from an older release is still unedited, and comparing against the current
  version alone would retire it and tell its owner they had edited it.
- anything else is renamed ``PERSONA.md.retired`` (or
  ``PERSONA.md.retired-<UTC stamp>`` when that name is taken; nothing is ever
  overwritten), and its owner gets one ``task_alert`` notice. Written, never
  delivered: this runs from ``istota init`` with every service stopped, the
  ``room_relocate.record_outcome`` precedent.

Only ``config.users`` are visited, never a directory listing, and a user id
that does not name one plain directory is refused. Each user's
``{bot_dir}/config`` is walked with ``O_NOFOLLOW`` at every component
(``skills._loader.open_overlay_dir``, the ISSUE-344 rule for this tree), so
the read, the delete and the rename all happen relative to one pinned
directory fd. The tree is bound read-write into its owner's sandbox, so a
symlink or FIFO planted at ``PERSONA.md`` or above it is refused and left
alone, never followed.

Idempotent by construction: after a run there is no ``PERSONA.md`` left to
act on. ``python -m istota.maintenance.persona_retire [--dry-run|--list]``;
exit 0 complete (including nothing to do), 1 refusal with nothing touched
(no file root, root unavailable, or a task in flight on a real run, with a
``refusal: live_tasks`` line as ``repos_relocate`` prints), 2 partial (a user
refused). Never raises.
"""

from __future__ import annotations

import argparse
import errno
import logging
import os
import stat
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from istota import db, storage
from istota.prompts import persona
from istota.sandbox.user_scope import is_scopable_user_id

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger("istota.maintenance.persona_retire")

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_PARTIAL = 2

ACTION_ABSENT = "absent"
ACTION_DELETED = "deleted"
ACTION_RETIRED = "retired"
ACTION_REFUSED = "refused"
ACTION_NO_WORKSPACE = "no_workspace"
ACTION_ROOT_UNAVAILABLE = "root_unavailable"

NOTICE_TITLE = "Your persona file was retired"
# No parentheses or brackets: `task_alert.flatten_body` strips them.
NOTICE_BODY = (
    "The bot's character is now set by the operator for everyone. Your edited "
    "copy was kept as {name} in your {bot_dir}/config folder and is no longer "
    "read. If it held a role or standing instructions rather than character, "
    "such as what the bot does for you, how it handles your mail or whom to "
    "escalate to, move those into USER.md. Preferences such as language, "
    "length or tone toward you go there too, or just tell the bot."
)


@dataclass(frozen=True)
class RetireOutcome:
    user_id: str
    action: str
    detail: str = ""
    #: A retirement whose notice could not be written. The rename stands, and
    #: no later run will see the file again, so the caller has to say so.
    notice_failed: bool = False


def _utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")


def _root_is_directory(root: Path) -> bool:
    try:
        return stat.S_ISDIR(os.stat(root).st_mode)
    except OSError:
        return False


def _operator_digest(config: "Config") -> str | None:
    """The operator copy's digest, or None when there is no usable text."""
    path = persona.operator_persona_path(config)
    if path is None:
        return None
    text, _reason, _present = persona.read_operator_file(path)
    if not text or not text.strip():
        return None
    return persona.persona_digest(text)


def _exists_at(name: str, dir_fd: int) -> bool:
    try:
        os.lstat(name, dir_fd=dir_fd)
    except FileNotFoundError:
        return False
    return True


def _retired_name(dir_fd: int) -> str | None:
    """A free ``PERSONA.md.retired[-stamp]`` name in the directory, or None."""
    base = persona.PERSONA_FILENAME + persona.RETIRED_SUFFIX
    for candidate in (base, f"{base}-{_utc_stamp()}"):
        if not _exists_at(candidate, dir_fd):
            return candidate
    return None


def _move_without_replacing(src: str, dst: str, dir_fd: int) -> str | None:
    """Move ``src`` to ``dst`` in one directory, never replacing ``dst``.

    ``rename(2)`` replaces an existing target, so the move is ``link(2)``,
    which fails with ``EEXIST``, then ``unlink`` of the old name. A filesystem
    with no hard links (an rclone FUSE mount among them) falls back to a check
    and a ``rename``; the gap between the two is closed only by every service
    being stopped, which ``init`` and ``main``'s live-task guard provide.
    Returns a refusal reason, or None when the move happened.
    """
    try:
        os.link(src, dst, src_dir_fd=dir_fd, dst_dir_fd=dir_fd, follow_symlinks=False)
    except FileExistsError:
        return f"{dst} appeared"
    except FileNotFoundError:
        return f"{src} vanished"
    except OSError:
        if _exists_at(dst, dir_fd):
            return f"{dst} appeared"
        os.rename(src, dst, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        return None
    try:
        os.unlink(src, dir_fd=dir_fd)
    except OSError as exc:
        # Two names on one inode: drop the new one so a later run sees the
        # state this one started from.
        try:
            os.unlink(dst, dir_fd=dir_fd)
        except OSError:
            pass
        return f"could not remove {src} ({errno.errorcode.get(exc.errno or 0, 'OSError')})"
    return None


def _write_notice(config: "Config", user_id: str, name: str) -> bool:
    """One fire-and-forget row for ``user_id``, written and never delivered."""
    from istota.notifications.resolvers import task_alert  # noqa: PLC0415

    if not db.database_present(config.db_path):
        logger.warning("persona_retire_notice_unwritten user=%s reason=no_database", user_id)
        return False
    try:
        with db.get_db(config.db_path) as conn:
            # The store never raises; a refused or failed write is None.
            written = task_alert.write(
                conn, user_id,
                dedup_key=task_alert.persona_retired_key(),
                title=NOTICE_TITLE,
                body=NOTICE_BODY.format(name=name, bot_dir=config.bot_dir_name),
                severity="info",
                params={"alert_type": task_alert.ALERT_TYPE_NOTE},
            )
    except Exception as exc:  # noqa: BLE001 - the rename stands either way
        logger.warning(
            "persona_retire_notice_unwritten user=%s err=%s", user_id, type(exc).__name__,
        )
        return False
    if written is None:
        logger.warning("persona_retire_notice_unwritten user=%s reason=store_refused", user_id)
        return False
    return True


def _decide(data: bytes, operator_digest: str | None) -> str:
    """``deleted`` for a copy nothing would miss, ``retired`` for an edit."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return ACTION_RETIRED
    if not text.strip():
        return ACTION_DELETED
    digest = persona.persona_digest(text)
    if digest in persona.SHIPPED_PERSONA_DIGESTS or digest == operator_digest:
        return ACTION_DELETED
    return ACTION_RETIRED


def _retire_one(
    config: "Config", user_id: str, operator_digest: str | None, *, dry_run: bool,
) -> RetireOutcome:
    from istota.skills._loader import (  # noqa: PLC0415 - import cycle
        OVERLAY_UNREADABLY_LARGE,
        open_overlay_dir,
        read_overlay_bytes,
    )

    if not is_scopable_user_id(user_id):
        logger.warning("persona_retire user=%r action=refused reason=unscopable_user_id", user_id)
        return RetireOutcome(user_id, ACTION_REFUSED, "user id does not name one directory")
    user_root = config.workspace_root(user_id)
    if user_root is None:
        return RetireOutcome(user_id, ACTION_REFUSED, "user id does not name one directory")
    parts = (config.bot_dir_name, "config")
    # Walked with O_NOFOLLOW at every component (ISSUE-344): the tree is bound
    # read-write into its owner's sandbox, so a resolve-then-open check could
    # be redirected by a component swapped in between.
    dir_fd = open_overlay_dir(user_root, *parts)
    if dir_fd is None:
        reason, _definitive = storage._classify_dir_refusal(user_root, list(parts))
        if reason == storage.OWNER_FILE_MISSING:
            return RetireOutcome(user_id, ACTION_ABSENT)
        logger.warning("persona_retire user=%s action=refused reason=%s", user_id, reason)
        return RetireOutcome(user_id, ACTION_REFUSED, f"config directory refused: {reason}")
    try:
        leaf = persona.PERSONA_FILENAME
        data, reason, size = read_overlay_bytes(
            Path(leaf), max_bytes=persona.PERSONA_MAX_BYTES, dir_fd=dir_fd,
        )
        if reason == OVERLAY_UNREADABLY_LARGE:
            # Larger than any shipped version can be, so it is an edit.
            action = ACTION_RETIRED
        elif reason is not None:
            logger.warning("persona_retire user=%s action=refused reason=%s", user_id, reason)
            return RetireOutcome(user_id, ACTION_REFUSED, reason)
        elif size is None:
            return RetireOutcome(user_id, ACTION_ABSENT)
        else:
            action = _decide(data or b"", operator_digest)

        if action == ACTION_DELETED:
            if not dry_run:
                os.unlink(leaf, dir_fd=dir_fd)
                logger.info("persona_retire user=%s action=deleted", user_id)
            return RetireOutcome(user_id, ACTION_DELETED, "dry run" if dry_run else "")

        name = _retired_name(dir_fd)
        if name is None:
            logger.warning("persona_retire user=%s action=refused reason=retired_names_taken", user_id)
            return RetireOutcome(user_id, ACTION_REFUSED, "both retired names are taken")
        if dry_run:
            return RetireOutcome(user_id, ACTION_RETIRED, f"dry run: would be {name}")
        refusal = _move_without_replacing(leaf, name, dir_fd)
        if refusal is not None:
            logger.warning("persona_retire user=%s action=refused reason=%s", user_id, refusal)
            return RetireOutcome(user_id, ACTION_REFUSED, refusal)
        logger.info("persona_retire user=%s action=retired", user_id)
        if not _write_notice(config, user_id, name):
            return RetireOutcome(
                user_id, ACTION_RETIRED, f"{name}; the notice could not be written",
                notice_failed=True,
            )
        return RetireOutcome(user_id, ACTION_RETIRED, name)
    except OSError as exc:
        logger.warning("persona_retire user=%s action=refused errno=%s", user_id, exc.errno)
        return RetireOutcome(
            user_id, ACTION_REFUSED, errno.errorcode.get(exc.errno or 0, "OSError"),
        )
    finally:
        os.close(dir_fd)


def retire_user_personas(config: "Config", *, dry_run: bool = False) -> list[RetireOutcome]:
    """Delete or retire each configured user's ``PERSONA.md``. Never raises.

    ``dry_run`` decides each action and writes nothing: no delete, no rename,
    no notice.
    """
    users = sorted(config.users)
    if not config.has_workspace:
        return [RetireOutcome(uid, ACTION_NO_WORKSPACE) for uid in users]
    # Never create the root: on an offline FUSE mount that would write into
    # the mountpoint underneath.
    if not _root_is_directory(Path(config.workspace_path)):
        return [RetireOutcome(uid, ACTION_ROOT_UNAVAILABLE) for uid in users]
    operator_digest = _operator_digest(config)
    outcomes = []
    for user_id in users:
        try:
            outcomes.append(_retire_one(config, user_id, operator_digest, dry_run=dry_run))
        except Exception as exc:  # noqa: BLE001 - one user must not stop the rest
            logger.warning(
                "persona_retire user=%s action=refused err=%s", user_id, type(exc).__name__,
            )
            outcomes.append(RetireOutcome(user_id, ACTION_REFUSED, type(exc).__name__))
    return outcomes


@dataclass(frozen=True)
class PersonaCensus:
    """What is left in the configured users' config folders. Counts only."""

    #: Users whose folder still holds a regular ``PERSONA.md``, which the next
    #: ``istota init`` deletes or retires.
    remaining: int = 0
    #: Users whose ``PERSONA.md`` is not a regular file (a link, a pipe, a
    #: directory). ``_retire_one`` refuses those and leaves them, so only a
    #: person can clear them.
    irregular: int = 0
    #: ``PERSONA.md.retired*`` files across all configured users.
    retired: int = 0
    #: Users whose folder exists and could not be opened (a symlink, a file);
    #: ``istota init`` refuses these users too.
    unreadable: int = 0


def census_user_personas(config: "Config") -> PersonaCensus:
    """Count per-user copies, retired copies and unopenable folders. Never raises.

    The same walk ``_retire_one`` takes (configured users only, ``O_NOFOLLOW``
    at every component), listing the pinned directory and opening no file, so
    a FIFO planted there cannot block ``doctor`` on the start-up path. A
    ``PERSONA.md`` is classified with ``lstat`` the way the retirement's read
    would answer it, so the two agree on what ``init`` can clear. Names no
    user: ``doctor`` renders this to every admin.
    """
    remaining = irregular = retired = unreadable = 0
    try:
        if not config.has_workspace:
            return PersonaCensus()
        from istota.skills._loader import open_overlay_dir  # noqa: PLC0415 - import cycle
    except Exception:  # noqa: BLE001 - a census never raises
        return PersonaCensus()
    retired_prefix = persona.PERSONA_FILENAME + persona.RETIRED_SUFFIX
    parts = (config.bot_dir_name, "config")
    for user_id in sorted(config.users):
        try:
            if not is_scopable_user_id(user_id):
                unreadable += 1
                continue
            user_root = config.workspace_root(user_id)
            if user_root is None:
                unreadable += 1
                continue
            dir_fd = open_overlay_dir(user_root, *parts)
            if dir_fd is None:
                reason, _definitive = storage._classify_dir_refusal(user_root, list(parts))
                if reason != storage.OWNER_FILE_MISSING:
                    unreadable += 1
                continue
            try:
                names = os.listdir(dir_fd)
                leaf_is_regular = None
                if persona.PERSONA_FILENAME in names:
                    try:
                        st = os.lstat(persona.PERSONA_FILENAME, dir_fd=dir_fd)
                        leaf_is_regular = stat.S_ISREG(st.st_mode)
                    except FileNotFoundError:
                        pass
                    except OSError:
                        leaf_is_regular = False
            finally:
                os.close(dir_fd)
        except Exception:  # noqa: BLE001 - a census never raises
            unreadable += 1
            continue
        if leaf_is_regular is True:
            remaining += 1
        elif leaf_is_regular is False:
            irregular += 1
        for name in names:
            if name.startswith(retired_prefix):
                retired += 1
    return PersonaCensus(
        remaining=remaining, irregular=irregular, retired=retired, unreadable=unreadable,
    )


def exit_code(outcomes: list[RetireOutcome]) -> int:
    actions = {o.action for o in outcomes}
    if actions & {ACTION_NO_WORKSPACE, ACTION_ROOT_UNAVAILABLE}:
        return EXIT_REFUSED
    if ACTION_REFUSED in actions:
        return EXIT_PARTIAL
    return EXIT_OK


_WOULD = {ACTION_DELETED: "would delete", ACTION_RETIRED: "would retire"}


def _render(outcome: RetireOutcome, *, planned: bool) -> str:
    label = outcome.action
    detail = outcome.detail
    if planned:
        label = _WOULD.get(outcome.action, outcome.action)
        if outcome.action != ACTION_REFUSED:
            detail = ""
    return f"{outcome.user_id}: {label}" + (f" ({detail})" if detail else "")


REFUSE_LIVE_TASKS = "live_tasks"


def _live_task_refusal(config: "Config") -> str | None:
    """Why a real run must not start now, or None.

    ``init`` runs with every service stopped; a hand run of this module may
    not, and a task writing the directory between the check and the rename is
    the gap ``_move_without_replacing`` cannot close everywhere. An unreadable
    task table refuses too, since "unknown" is not "nobody is working". No
    database at all is not a refusal: nothing can be running against it.
    """
    if not db.database_present(config.db_path):
        return None
    try:
        with db.get_db(config.db_path) as conn:
            busy = db.get_users_with_live_tasks(conn)
    except Exception as exc:  # noqa: BLE001 - never raises out of main
        return f"the task table could not be read ({type(exc).__name__}); stop all units first"
    if busy:
        return "a task is in flight; stop all units first"
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="istota.maintenance.persona_retire",
        description="Delete or retire the per-user PERSONA.md copies. Stop all service units first.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true",
        help="Print the operator persona sync and the retirement plan; write nothing.",
    )
    mode.add_argument(
        "--list", action="store_true", dest="list_only",
        help="Print each configured user's current state; write nothing.",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return int(exc.code or 0)

    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(errors="backslashreplace")
        except (OSError, ValueError):  # pragma: no cover - depends on the stream
            pass

    try:
        from istota.config import load_config  # noqa: PLC0415

        config = load_config()
    except Exception as exc:  # noqa: BLE001 - never raises out of main
        print(f"persona_retire: the configuration could not be loaded ({exc})", file=sys.stderr)
        return EXIT_REFUSED

    if not config.has_workspace:
        print("persona_retire: no file root; nothing to retire", file=sys.stderr)
        return EXIT_REFUSED
    if not _root_is_directory(Path(config.workspace_path)):
        print(f"persona_retire: {config.workspace_path} is not a directory", file=sys.stderr)
        return EXIT_REFUSED

    planned = args.dry_run or args.list_only
    if not planned:
        refusal = _live_task_refusal(config)
        if refusal is not None:
            print(f"persona_retire: refused — {refusal}", file=sys.stderr)
            print(f"refusal: {REFUSE_LIVE_TASKS}", file=sys.stderr)
            return EXIT_REFUSED
    try:
        if args.dry_run:
            sync = persona.sync_operator_persona(config, dry_run=True)
            print(f"operator persona: {sync.action}")
        outcomes = retire_user_personas(config, dry_run=planned)
    except Exception as exc:  # noqa: BLE001 - never raises out of main
        print(f"persona_retire: failed ({type(exc).__name__})", file=sys.stderr)
        return EXIT_REFUSED
    for outcome in outcomes:
        print(_render(outcome, planned=planned))
    return exit_code(outcomes)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
