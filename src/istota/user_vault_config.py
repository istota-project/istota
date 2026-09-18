"""The per-user credential vault's two selecting fields, when a surface sets them.

``vault_path`` and ``vault_services`` were TOML-only, and the reason was never
that TOML is where configuration lives. It was that ``user_profiles`` is the
general settings overlay and these two are a security control: ``vault_path``
selects which KDBX file the daemon decrypts with a key it holds, and
``vault_services`` selects which of that user's stored credentials the file may
overwrite and delete. Neither may be settable by anything downstream of a task,
so neither could go on the table every other per-user scalar lives on.

This is that table's replacement for the surface case: one row per user, written
by the web settings endpoint and by ``istota user ensure`` and by nothing else.
``tests/test_user_vault_config.py`` sweeps the tree for a third writer rather
than naming the two, on the rule ``.claude/rules/devbox.md`` records for the
vendored-lib guards — a guard naming its own subjects covers only the ones its
author thought of.

**A row present is the authority; no row falls back to ``[users.<id>]``.** That
is the repo's existing "DB rows win at config-load time" rule, with one
difference that matters: the read is *live* rather than load-time.
:meth:`Config.vault_path_for` asks this module on every call, because the
scheduler holds one ``Config`` for its whole life and syncs on a five-minute
interval — a load-time overlay would mean a change made in the browser did
nothing until somebody restarted the daemon, which is the opposite of what a
settings page promises.

**Clearing deletes the row rather than blanking it.** An empty ``vault_path``
and no row are the same state — the feature is off for this user — and two
spellings of one state is how a precedence rule starts lying: a blank row would
*win* over a TOML line, so "I cleared the web field" would silently make an
operator's ``config.toml`` inert with nothing saying so.

**Relative paths only, and that is enforced by the endpoint rather than here.**
A relative ``vault_path`` resolves under the user's own workspace, which is the
edit-it-from-a-phone case the feature exists for. The absolute form is the
escape hatch that puts the file where no sandbox binds it, is checked against
``storage._sandbox_writable_roots`` rather than against one user's tree, and in
a user's hands would be an arbitrary read as the daemon user. The containment
question is ``storage.resolve_user_vault_path``'s, so the refusal is stated
where that resolver can be *asked* rather than copied.

The passphrase is not here. It is a ``vault`` / ``passphrase`` row in the
encrypted ``secrets`` table, like every other credential.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from . import sqlite_util
from .user_scope import is_scopable_user_id

logger = logging.getLogger(__name__)

#: Who wrote the row. Recorded rather than enforced — it is for an operator
#: reading the table, and both values are ordinary writers.
SOURCE_WEB = "web"
SOURCE_CLI = "cli"


@dataclass(frozen=True)
class VaultConfigRow:
    """One user's stored vault selection.

    ``vault_services`` is a list here and JSON in the column; a value that will
    not parse reads as the empty list, which is the dry-run state — the file is
    read and nothing is applied — rather than as everything.
    """

    user_id: str
    vault_path: str = ""
    vault_services: list[str] = field(default_factory=list)
    updated_at: str = ""
    updated_by: str = ""


def _parse_services(raw: object) -> list[str]:
    """The JSON column as a list of non-empty strings, whatever it holds.

    Degrades to ``[]`` rather than raising. The caller is a config accessor on
    the path of every vault sync and every settings render, and the safe
    direction is unambiguous: an unreadable list means the vault owns nothing,
    which reads the file and writes no credential.
    """
    if not isinstance(raw, str) or not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except (ValueError, TypeError):
        logger.warning("user_vault_config.vault_services is not JSON; reading as []")
        return []
    if not isinstance(parsed, list):
        logger.warning("user_vault_config.vault_services is not a list; reading as []")
        return []
    return [s.strip() for s in parsed if isinstance(s, str) and s.strip()]


def get_vault_config(
    db_path: Path, user_id: str, conn: sqlite3.Connection | None = None
) -> VaultConfigRow | None:
    """This user's row, or ``None`` when there is not one.

    ``None`` is the answer for every user by default and is what makes the TOML
    fallback reachable, so it is distinct from a row holding an empty path —
    which :func:`set_vault_config` refuses to create.

    Never raises. A missing table is the case worth naming: ``schema.sql`` is
    applied by ``init_db`` and a process reading a database an older release
    wrote before its own ``init_db`` ran would otherwise take an
    ``OperationalError`` out of a config accessor.
    """
    if not isinstance(user_id, str) or not user_id:
        return None
    sql = (
        "SELECT user_id, vault_path, vault_services, updated_at, updated_by "
        "FROM user_vault_config WHERE user_id = ?"
    )
    try:
        if conn is not None:
            row = conn.execute(sql, (user_id,)).fetchone()
        else:
            with sqlite_util.open_db(db_path, row_factory=True) as own:
                row = own.execute(sql, (user_id,)).fetchone()
    except sqlite3.OperationalError as e:
        logger.debug("user_vault_config read failed for %r: %s", user_id, e)
        return None
    if row is None:
        return None
    return VaultConfigRow(
        user_id=row["user_id"],
        vault_path=row["vault_path"] or "",
        vault_services=_parse_services(row["vault_services"]),
        updated_at=row["updated_at"] or "",
        updated_by=row["updated_by"] or "",
    )


def set_vault_config(
    db_path: Path,
    user_id: str,
    *,
    vault_path: str,
    vault_services: list[str],
    source: str = SOURCE_WEB,
) -> None:
    """Write this user's selection, replacing whatever stood.

    Raises ``ValueError`` for a ``user_id`` that is not a single path component
    and for an empty ``vault_path``. Both are refusals rather than degradations
    because both have a caller that can act on them: the id ends up joined under
    the workspace root by the resolver, where ``user_scope`` already owns the
    rule and an empty or traversing value is the exposure it exists to refuse;
    and an empty path is :func:`clear_vault_config`'s job, spelled a second way.

    It does **not** validate that the path resolves. That answer depends on the
    filesystem and on the whole ``Config``, it is ``storage`` that owns it, and a
    store that could only hold a path resolving *right now* would refuse a
    perfectly good row for a mount that happened to be down.
    """
    if not is_scopable_user_id(user_id):
        raise ValueError("user_id must be a single path component")
    path = (vault_path or "").strip()
    if not path:
        raise ValueError("vault_path is empty; use clear_vault_config to switch it off")
    services = [s.strip() for s in vault_services if isinstance(s, str) and s.strip()]
    with sqlite_util.open_db(db_path, commit=True) as conn:
        conn.execute(
            "INSERT INTO user_vault_config "
            "  (user_id, vault_path, vault_services, updated_at, updated_by) "
            "VALUES (?, ?, ?, datetime('now'), ?) "
            "ON CONFLICT(user_id) DO UPDATE SET "
            "  vault_path = excluded.vault_path, "
            "  vault_services = excluded.vault_services, "
            "  updated_at = excluded.updated_at, "
            "  updated_by = excluded.updated_by",
            (user_id, path, json.dumps(services), source),
        )
    # The path is the user's own filename under their own workspace, so it is
    # not a credential — but it is still their data, and a boot log is read by
    # every admin. The service list is the part an operator needs.
    logger.info(
        "vault config set user=%s services=%s source=%s",
        user_id, sorted(services), source,
    )


def clear_vault_config(db_path: Path, user_id: str) -> bool:
    """Remove this user's row. True when there was one.

    The return value is what lets a caller tell "switched off" from "was never
    on", which the endpoint reports and the CLI prints.
    """
    if not isinstance(user_id, str) or not user_id:
        return False
    with sqlite_util.open_db(db_path, commit=True) as conn:
        cur = conn.execute(
            "DELETE FROM user_vault_config WHERE user_id = ?", (user_id,)
        )
        removed = cur.rowcount > 0
    if removed:
        logger.info("vault config cleared user=%s", user_id)
    return removed


def list_vault_configs(db_path: Path) -> dict[str, VaultConfigRow]:
    """Every stored row, keyed by user id.

    For ``doctor`` and the CLI, which ask about the deployment rather than about
    one user. Never raises, for :func:`get_vault_config`'s reason.
    """
    try:
        with sqlite_util.open_db(db_path, row_factory=True) as conn:
            rows = conn.execute(
                "SELECT user_id, vault_path, vault_services, updated_at, updated_by "
                "FROM user_vault_config"
            ).fetchall()
    except sqlite3.OperationalError as e:
        logger.debug("user_vault_config listing failed: %s", e)
        return {}
    return {
        row["user_id"]: VaultConfigRow(
            user_id=row["user_id"],
            vault_path=row["vault_path"] or "",
            vault_services=_parse_services(row["vault_services"]),
            updated_at=row["updated_at"] or "",
            updated_by=row["updated_by"] or "",
        )
        for row in rows
    }
