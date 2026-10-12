"""The `config_change_requests` table: a change waiting for the host applier.

The web inserts a row (`create`) and touches a trigger file; the host runs
`istota config-request take` in a one-shot container, which claims the oldest
`pending` row (`take`), checks everything, writes, and records the outcome
(`mark`, `finish`). The lifecycle:

    pending -> taken [-> building] -> applied | unchanged | refused | failed
    pending -> cancelled
    pending -> refused          (config.toml changed since the request was made)

At most one row is open (`pending`, `taken`, `building`); a partial unique
index holds that, so a second `create` raises `RequestOpen`.

No credential value is stored in the clear. The document is TOML with a
sentinel at every secret leaf, and every new credential value is in
`secrets_sealed`, sealed with the master key (`credentials.store.seal`), which
`take` nulls in the transaction that claims the row.

A request reaching `applied` or `failed` raises `admin_config_change` for
every admin before or after it but the actor, handed to the daemon in the same
transaction.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)

OPEN_STATUSES = ("pending", "taken", "building")
FINISH_STATUSES = ("applied", "unchanged", "refused", "failed")
RETENTION_DAYS = 180

STALE_OUTPUT = "the host did not record an outcome"
DIGEST_OUTPUT = "config.toml changed since this request was made"
UNSEAL_OUTPUT = "the request's credentials could not be unsealed with this install's key"

_PUBLIC_COLUMNS = (
    "id", "status", "admin_id", "base_digest", "document_toml", "admins_json",
    "changed_sections_json", "secret_names_json", "diff_text", "output_text",
    "created_at", "taken_at", "finished_at",
)


class RequestOpen(Exception):
    """A request is already open; `id` names it."""

    def __init__(self, request_id: int | None):
        super().__init__(f"request {request_id} is still open")
        self.id = request_id


class RequestStateError(Exception):
    """The row is not in a state that allows this transition."""


@dataclass(frozen=True)
class TakenRequest:
    """A claimed request, with its credentials unsealed for the host's files."""

    id: int
    admin_id: str
    base_digest: str
    document_toml: str
    secrets: dict[str, str]
    admins: list[str] | None
    changed_sections: list[str]
    secret_names: list[str]


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _begin(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _open_id(conn: sqlite3.Connection) -> int | None:
    row = conn.execute(
        "SELECT id FROM config_change_requests WHERE status IN ('pending','taken','building')"
    ).fetchone()
    return row[0] if row else None


def create(conn: sqlite3.Connection, preview: Any, *, admin_id: str, base_digest: str) -> int:
    """Store a previewed change as `pending` and return its id.

    `preview` is an `edits.Preview`. Its credential values are sealed here;
    without the master key this raises `SecretKeyMissingError` and stores
    nothing. Raises `RequestOpen` when a request is open.
    """
    from istota.credentials import store

    values = preview.secrets
    sealed = store.seal(json.dumps(values, sort_keys=True)) if values else None
    _begin(conn)
    open_id = _open_id(conn)
    if open_id is not None:
        raise RequestOpen(open_id)
    try:
        cursor = conn.execute(
            """INSERT INTO config_change_requests (
                   status, admin_id, base_digest, document_toml, secrets_sealed,
                   admins_json, changed_sections_json, secret_names_json,
                   diff_text, created_at)
               VALUES ('pending', ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                admin_id, base_digest, preview.document_toml, sealed,
                json.dumps(preview.admins) if preview.admins is not None else None,
                json.dumps(preview.changed_sections),
                json.dumps(preview.secret_names),
                "\n".join(preview.diff), _now(),
            ),
        )
    except sqlite3.IntegrityError:
        raise RequestOpen(_open_id(conn)) from None
    return int(cursor.lastrowid)


def _row(conn: sqlite3.Connection, request_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM config_change_requests WHERE id = ?", (int(request_id),)
    ).fetchone()


def _public(row: sqlite3.Row) -> dict:
    found = {name: row[name] for name in _PUBLIC_COLUMNS}
    found["admins"] = json.loads(row["admins_json"]) if row["admins_json"] else None
    found["admins_changed"] = row["admins_json"] is not None
    found["changed_sections"] = json.loads(row["changed_sections_json"])
    found["secret_names"] = json.loads(row["secret_names_json"])
    for name in ("admins_json", "changed_sections_json", "secret_names_json"):
        del found[name]
    return found


def get(conn: sqlite3.Connection, request_id: int) -> dict | None:
    """One request, without its sealed credentials."""
    row = _row(conn, request_id)
    return _public(row) if row is not None else None


def list_recent(conn: sqlite3.Connection, limit: int = 50) -> list[dict]:
    """The newest requests first, without their sealed credentials."""
    limit = max(1, min(int(limit), 500))
    rows = conn.execute(
        "SELECT * FROM config_change_requests ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    return [_public(row) for row in rows]


def _notify(
    conn: sqlite3.Connection, row: sqlite3.Row, status: str, current_admins: Iterable[str],
) -> None:
    """`admin_config_change` for every admin before or after but the actor."""
    from istota.config_editor.edits import loggable_secret_name
    from istota.notifications.resolvers import admin_config_change
    from istota.notifications.store import hand_to_daemon

    recipients = set(current_admins)
    for column in ("admins_json", "admins_before_json"):
        if row[column]:
            recipients.update(json.loads(row[column]))
    recipients.discard(row["admin_id"])
    names = sorted({loggable_secret_name(name) for name in json.loads(row["secret_names_json"])})
    results = [
        admin_config_change.write(
            conn, user_id, request_id=row["id"], admin_name=row["admin_id"], status=status,
            sections=json.loads(row["changed_sections_json"]), credentials=names,
            admins_changed=row["admins_json"] is not None,
        )
        for user_id in sorted(recipients)
    ]
    hand_to_daemon(conn, results)


def _close(
    conn: sqlite3.Connection, row: sqlite3.Row, status: str, output: str,
    current_admins: Iterable[str],
) -> None:
    conn.execute(
        """UPDATE config_change_requests
           SET status = ?, output_text = ?, secrets_sealed = NULL, finished_at = ?
           WHERE id = ?""",
        (status, output, _now(), row["id"]),
    )
    logger.info("admin_config_result request=%d status=%s", row["id"], status)
    if status in ("applied", "failed"):
        _notify(conn, row, status, current_admins)


def take(
    conn: sqlite3.Connection, live_digest: str, *, current_admins: Iterable[str] = (),
) -> TakenRequest | None:
    """Claim the oldest `pending` request, in one `BEGIN IMMEDIATE`.

    First every row still `taken` or `building` is failed: the caller holds the
    stack lock, which proves no other run owns one. A request whose
    `base_digest` is not `live_digest` (the sha256 of `config.toml` now) is
    `refused`, so a write made on the host after the request is never reverted.
    The claimed row's sealed credentials are unsealed for the caller and nulled
    on the row. `current_admins` is the admin list now, recorded as the "before"
    list a notification goes to.
    """
    from istota.credentials import store

    current_admins = sorted(set(current_admins))
    _begin(conn)
    for row in conn.execute(
        "SELECT * FROM config_change_requests WHERE status IN ('taken','building') ORDER BY id"
    ).fetchall():
        _close(conn, row, "failed", STALE_OUTPUT, current_admins)
    while True:
        row = conn.execute(
            "SELECT * FROM config_change_requests WHERE status = 'pending' ORDER BY id LIMIT 1"
        ).fetchone()
        if row is None:
            return None
        if row["base_digest"] != live_digest:
            _close(conn, row, "refused", DIGEST_OUTPUT, current_admins)
            continue
        try:
            values = json.loads(store.unseal(row["secrets_sealed"])) if row["secrets_sealed"] else {}
        except Exception as exc:  # noqa: BLE001 - a missing key or another key's token
            logger.warning("config request %d: unseal failed: %s", row["id"], type(exc).__name__)
            _close(conn, row, "failed", UNSEAL_OUTPUT, current_admins)
            continue
        conn.execute(
            """UPDATE config_change_requests
               SET status = 'taken', taken_at = ?, secrets_sealed = NULL,
                   admins_before_json = ?
               WHERE id = ?""",
            (_now(), json.dumps(current_admins), row["id"]),
        )
        return TakenRequest(
            id=row["id"],
            admin_id=row["admin_id"],
            base_digest=row["base_digest"],
            document_toml=row["document_toml"],
            secrets=values,
            admins=json.loads(row["admins_json"]) if row["admins_json"] else None,
            changed_sections=json.loads(row["changed_sections_json"]),
            secret_names=json.loads(row["secret_names_json"]),
        )


def mark(conn: sqlite3.Connection, request_id: int, status: str = "building") -> None:
    """`taken` -> `building`, while the host builds an image the change needs."""
    if status != "building":
        raise RequestStateError("a request is marked building, and nothing else")
    _begin(conn)
    updated = conn.execute(
        "UPDATE config_change_requests SET status = 'building' WHERE id = ? AND status = 'taken'",
        (int(request_id),),
    ).rowcount
    if not updated:
        raise RequestStateError(f"request {int(request_id)} is not taken")


def finish(
    conn: sqlite3.Connection, request_id: int, status: str, output: str, *,
    current_admins: Iterable[str] = (),
) -> None:
    """Record the host's outcome on a `taken` or `building` request."""
    if status not in FINISH_STATUSES:
        raise RequestStateError(f"{status!r} is not an outcome")
    _begin(conn)
    row = _row(conn, request_id)
    if row is None or row["status"] not in ("taken", "building"):
        raise RequestStateError(f"request {int(request_id)} is not taken")
    _close(conn, row, status, output, current_admins)


def cancel(conn: sqlite3.Connection, request_id: int) -> bool:
    """`pending` -> `cancelled`. False when the row is not pending."""
    _begin(conn)
    updated = conn.execute(
        """UPDATE config_change_requests
           SET status = 'cancelled', secrets_sealed = NULL, finished_at = ?
           WHERE id = ? AND status = 'pending'""",
        (_now(), int(request_id)),
    ).rowcount
    return bool(updated)


def prune(conn: sqlite3.Connection, *, older_than_days: int = RETENTION_DAYS) -> int:
    """Delete closed requests created more than `older_than_days` ago."""
    if older_than_days <= 0:
        raise ValueError("retention must be positive")
    return conn.execute(
        """DELETE FROM config_change_requests
           WHERE status NOT IN ('pending','taken','building')
             AND julianday(created_at) < julianday('now', ?)""",
        (f"-{int(older_than_days)} days",),
    ).rowcount
