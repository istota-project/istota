"""The operator persona: one character per installation, kept at the file root.

The operator's copy is ``{root}/PERSONA.md``, beside ``Users/`` and
``Channels/``. Its upstream is the repo's ``config/persona.md``, and the sync
follows Debian's conffile rule: an unedited copy is replaced by each new
shipped version, an edited one is kept and the new shipped text is written
beside it as ``PERSONA.md.shipped`` for the operator to merge.

**"Unedited" means the digest matches any version ever shipped**, not the
current one and not a recorded hash. Two consequences are deliberate: losing
the state row can never turn an edit into "unedited" and overwrite it, and a
copy left at an older shipped version (what most per-user copies on a long-lived
deployment hold) still reads as unedited. The cost is the one dpkg accepts: a
file edited back to exactly some earlier shipped version is upgraded.

State lives in the reserved ``shared_kv`` namespace ``_operator_persona``: the
shipped digest the last sync saw (whether the shipped text moved since, which
decides ``.shipped``) and the last good copy of the operator file, which the
prompt path falls back to while the mount is unreadable so a restart during an
outage does not change the character. Only the sync writes it; the prompt path
reads it.

Nothing here raises. Log lines name the action and reason, never the text.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from istota import db, storage

if TYPE_CHECKING:
    from istota.config import Config

logger = logging.getLogger("istota.prompts.persona")

PERSONA_FILENAME = "PERSONA.md"
SHIPPED_SUFFIX = ".shipped"
RETIRED_SUFFIX = ".retired"
#: The shipped file is about 6 KB; this bounds a runaway edit.
PERSONA_MAX_BYTES = 64 * 1024

KV_NAMESPACE = "_operator_persona"
KV_KEY = "state"
_WRITTEN_BY = "persona:sync"

ACTION_NO_WORKSPACE = "no_workspace"
ACTION_ROOT_UNAVAILABLE = "root_unavailable"
ACTION_WROTE = "wrote"
ACTION_UPGRADED = "upgraded"
ACTION_KEPT_EDITED = "kept_edited"
ACTION_WROTE_SHIPPED_BESIDE = "wrote_shipped_beside"
ACTION_UNCHANGED = "unchanged"
ACTION_EMPTY = "empty"
ACTION_REFUSED = "refused"


def persona_digest(text: str) -> str:
    """sha256 of ``text`` with CRLF folded and surrounding whitespace stripped.

    So an editor's trailing newline or a CRLF save reads as unedited.
    """
    normalised = text.replace("\r\n", "\n").strip()
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


#: Every version of ``config/persona.md`` ever shipped, newest first, from
#: ``git log --follow -- config/persona.md``. Append the new digest whenever
#: the shipped persona changes; ``tests/test_persona.py`` fails until you do.
#: Never remove one: an older version is what an unedited copy may still hold.
SHIPPED_PERSONA_DIGESTS: frozenset[str] = frozenset({
    "3f8635bfd18f8a3294e53dc1907f4195bb4d3688f643cc8e2615a4334b3b1b84",  # 39c8221a
    "7112b68b8a0334fbb06d4cd1d262ea9b1c8f3f361b04041be850ee3327065a5e",  # 322d2187
    "e74d7fb452601886dec062c778eac21e47285d3f2d029f6a9cd2948eab6271ed",  # 71ea941b
    "2ea87837821670c3ffc78c0a68fb6851ab2868956d304539922f40972205cee1",  # c60fe3c3
    "90ad3f56d2967cc7c0b83136056fac38b3c5aa01cd7e0802c6de9e9278eec760",  # 7c956fa1
    "77fe108632a4ca6586e5d869120f235d86ecea9d8f8131c68e5be9b8458f634a",  # e38175ea
    "59f39c84c1664d78581c66b6d6791588ee54ab9cf55cbc829b3bdd0203a3f730",  # cb483960
    "98693ae5dd735d2009d912723d793cbc6851be53b02859aee96f7d2f64daf392",  # 5770a856
    "d8146b0fe6347d1ca165c4b62f3b685c70ab3f523ccf84b461315ae25ff5a35f",  # 259379a3
    "8054c400a69ddde1bd4695ec2cded7ca12ea36d1a7fb3da5ffc3b17e1f3349cd",  # 0679cb18
    "031b274e68bde658005fcd22a8d3d677c972f6ab63df8780a5b9febd6848285c",  # 1cb9b56a
    "d6a984f5c335920ebc39915cd6252f0b6718f9d02ef43064bed32d959c61c182",  # 7bde8f3e
})


def is_shipped(text: str) -> bool:
    """Whether ``text`` is some shipped version, i.e. an unedited copy."""
    return persona_digest(text) in SHIPPED_PERSONA_DIGESTS


def shipped_persona_path(config: "Config") -> Path:
    return config.skills_dir.parent / "persona.md"


def operator_persona_path(config: "Config") -> Path | None:
    """``{root}/PERSONA.md``, or None on a deployment with no file root."""
    if not config.has_workspace:
        return None
    return Path(config.workspace_path) / PERSONA_FILENAME


@dataclass(frozen=True)
class SyncResult:
    action: str
    detail: str = ""


def _read_operator_file(path: Path) -> tuple[str | None, str | None, bool]:
    """``(text, refusal, present)`` for the operator file.

    ``read_regular_file`` answers ``("", None)`` for a missing file and for an
    empty one alike; the sync treats those differently (write the shipped text,
    or leave the operator's choice alone), so a second look tells them apart.
    """
    text, reason = storage.read_regular_file(path, max_bytes=PERSONA_MAX_BYTES)
    if reason is not None:
        return None, reason, True
    if text:
        return text, None, True
    try:
        os.lstat(path)
    except FileNotFoundError:
        return "", None, False
    except OSError:
        return None, "unreadable", True
    return "", None, True


def _db_timeout_ms(config: "Config") -> int | None:
    return config.scheduler.main_loop_read_timeout_ms or None


def _read_state(config: "Config") -> dict | None:
    """The state row, ``{}`` when absent, None when it could not be read."""
    if not db.database_present(config.db_path):
        return {}
    try:
        with db.get_db(config.db_path, busy_timeout_ms=_db_timeout_ms(config)) as conn:
            row = db.shared_kv_get(conn, KV_NAMESPACE, KV_KEY)
    except Exception as exc:  # noqa: BLE001 - never raises
        logger.debug("operator_persona_state_unread err=%s", type(exc).__name__)
        return None
    if row is None:
        return {}
    try:
        value = json.loads(row["value"])
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_state(config: "Config", state: dict) -> None:
    if not db.database_present(config.db_path):
        return
    try:
        with db.get_db(config.db_path, busy_timeout_ms=_db_timeout_ms(config)) as conn:
            db.shared_kv_set(conn, KV_NAMESPACE, KV_KEY, json.dumps(state), _WRITTEN_BY)
    except Exception as exc:  # noqa: BLE001 - a record, not the file step
        logger.warning("operator_persona_state_unwritten err=%s", type(exc).__name__)


def read_last_good(config: "Config") -> str | None:
    """The operator copy as the last sync read it, or None. Never writes."""
    state = _read_state(config)
    if not state:
        return None
    text = state.get("last_good_text")
    if isinstance(text, str) and text.strip():
        return text
    return None


def _root_is_directory(root: Path) -> bool:
    try:
        return stat.S_ISDIR(os.stat(root).st_mode)
    except OSError:
        return False


def sync_operator_persona(config: "Config", *, dry_run: bool = False) -> SyncResult:
    """Apply the conffile rule to ``{root}/PERSONA.md`` and record the last good copy.

    ``dry_run`` decides the action and writes nothing: no file, no
    ``.shipped``, no stale-file removal, no state row.
    """
    path = operator_persona_path(config)
    if path is None:
        return SyncResult(ACTION_NO_WORKSPACE, "no file root")
    root = path.parent
    # Never create the root: on an offline FUSE mount that would write into
    # the mountpoint underneath.
    if not _root_is_directory(root):
        logger.warning("operator_persona_sync action=root_unavailable")
        return SyncResult(ACTION_ROOT_UNAVAILABLE, f"{root} is not a directory")

    try:
        shipped_text = shipped_persona_path(config).read_text(encoding="utf-8")
    except (OSError, ValueError) as exc:
        logger.warning(
            "operator_persona_sync action=refused reason=shipped_unreadable err=%s",
            type(exc).__name__,
        )
        return SyncResult(ACTION_REFUSED, "shipped persona unreadable")
    shipped_digest = persona_digest(shipped_text)
    beside = root / (PERSONA_FILENAME + SHIPPED_SUFFIX)

    state = _read_state(config)
    recorded_shipped = (state or {}).get("shipped_digest")

    text, reason, present = _read_operator_file(path)
    if reason is not None:
        logger.warning("operator_persona_sync action=refused reason=%s", reason)
        return SyncResult(ACTION_REFUSED, f"{PERSONA_FILENAME} refused: {reason}")

    # The text now on disk, when the step below leaves a good copy there.
    good_text: str | None = None
    # A failed write must not advance the recorded shipped digest, or the
    # next sync would read "nothing moved" and never retry it.
    file_step_done = True

    if not present:
        action = ACTION_WROTE
        if not dry_run:
            if storage.write_regular_file(path, shipped_text):
                good_text = shipped_text
            else:
                file_step_done = False
    elif not text.strip():
        action = ACTION_EMPTY
    elif is_shipped(text):
        action = ACTION_UPGRADED if persona_digest(text) != shipped_digest else ACTION_UNCHANGED
        if not dry_run:
            if action == ACTION_UPGRADED:
                if storage.write_regular_file(path, shipped_text):
                    good_text = shipped_text
                else:
                    file_step_done = False
            else:
                good_text = text
            if file_step_done:
                # The gap `.shipped` described has closed.
                storage.remove_regular_file(beside)
    else:
        moved = recorded_shipped is not None and recorded_shipped != shipped_digest
        first = recorded_shipped is None and not os.path.lexists(beside)
        if moved or first:
            action = ACTION_WROTE_SHIPPED_BESIDE
            if not dry_run:
                if storage.write_regular_file(beside, shipped_text):
                    logger.warning(
                        "operator_persona_sync action=wrote_shipped_beside "
                        "reason=edited_copy_and_shipped_%s",
                        "moved" if moved else "unrecorded",
                    )
                else:
                    file_step_done = False
        else:
            action = ACTION_KEPT_EDITED
        good_text = text

    if dry_run:
        return SyncResult(action, "dry run")
    if not file_step_done:
        logger.warning("operator_persona_sync action=refused reason=write_failed step=%s", action)
        return SyncResult(ACTION_REFUSED, f"write failed while {action}")

    if state is not None:
        new_state = dict(state)
        new_state["shipped_digest"] = shipped_digest
        if good_text is not None and good_text.strip():
            digest = persona_digest(good_text)
            if new_state.get("last_good_digest") != digest:
                new_state["last_good_text"] = good_text
                new_state["last_good_digest"] = digest
        if new_state != state:
            new_state["updated_at"] = datetime.now(timezone.utc).isoformat()
            _write_state(config, new_state)

    if action not in (ACTION_UNCHANGED, ACTION_KEPT_EDITED):
        logger.info("operator_persona_sync action=%s", action)
    return SyncResult(action)
