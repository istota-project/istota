"""The `shared_sources` store: what a user has shared with Istota through an open protocol.

A shared source is an ICS feed (``ics_feed``) or an Open Cloud Mesh folder
share (``ocm_share``). Each has an owner, a lifecycle state, a remote host, an
access level and a bearer secret, which is never in this table:

- an ICS feed's address, and an OCM secret held while an offer is pending or
  re-offered, are `secrets` rows under service `SECRET_SERVICE`, keys
  ``<id>`` and ``<id>:reoffer``. The service is daemon-owned, like
  ``mail_account``, and is not in `credentials/schema.py`;
- an accepted share's secret is the local credential `credential_name(id)`,
  so a skill reads it through the private credential channel under the
  user's grant.

A move into ``declined`` or ``revoked`` deletes all of those in the same
transaction, so a row in either state has no secret. The row itself stays,
identity columns and all, so a repeat of the same remote resource is
recognised rather than duplicated.

**Reach is ISSUE-576's**, with no per-source room policy: an ICS feed is in
the ``calendar`` scope and an OCM share in the ``shares`` scope
(`KIND_SCOPE`), and `sources_for_task` is the one listing every task-side
reader goes through. It answers nothing for a kind whose scope
`rooms.scopes.withheld_for_task` withholds (a guest's turn, a task nobody
asked in a shared room), and nothing for a kind whose skill is not in the
index at all. Leaving the system-half lines out of a member's turn in a
shared room is the prompt's job (`rooms.scopes.ambient_memory_off`), not
this module's.

Every function takes the owner's ``user_id`` and checks it in the statement,
so another user's id answers as a row that does not exist.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from istota.credentials import local as local_credentials
from istota.credentials import store as secrets_store

KINDS = ("ics_feed", "ocm_share")
STATES = ("pending", "active", "declined", "revoked", "error")
ACCESS = ("read", "write")
REACHABLE_STATES = ("active", "error")

SECRET_SERVICE = "shared_source"
KIND_SCOPE = {"ics_feed": "calendar", "ocm_share": "shares"}

_INITIAL_STATE = {"ics_feed": "active", "ocm_share": "pending"}
# Which state may follow which. `pending -> pending` is a repeat offer
# replacing the held secret; `revoked -> pending` a repeat offer of a share
# that was revoked; `revoked -> active` an ICS feed added again. A declined
# offer stays declined: a repeat of it is answered and ignored.
_TRANSITIONS = {
    "pending": frozenset({"pending", "active", "declined", "revoked"}),
    "active": frozenset({"active", "error", "revoked"}),
    "error": frozenset({"active", "error", "revoked"}),
    "declined": frozenset({"declined"}),
    "revoked": frozenset({"revoked", "pending", "active"}),
}
_SECRETLESS_STATES = frozenset({"declined", "revoked"})

_REASON_RE = re.compile(r"\A[a-z0-9_]{1,64}\Z")
_HOST_RE = re.compile(r"\A[^\s/@]{1,255}\Z")
_MAX_KEY_CHARS = 512
_MAX_EXTRAS_BYTES = 16 * 1024

_COLUMNS = (
    "id, user_id, kind, state, remote_host, remote_key, access, extras, error_count, "
    "last_reason, last_ok_at, next_poll_at, created_at, updated_at"
)


class SourceExists(Exception):
    """The user already has a row for this remote resource; ``source_id`` names it."""

    def __init__(self, source_id: int) -> None:
        super().__init__(f"shared source {source_id} already exists")
        self.source_id = source_id


@dataclass(frozen=True)
class SharedSource:
    id: int
    user_id: str
    kind: str
    state: str
    remote_host: str
    remote_key: str
    access: str
    extras: dict = field(default_factory=dict)
    error_count: int = 0
    last_reason: str | None = None
    last_ok_at: str | None = None
    next_poll_at: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


def credential_name(source_id: int) -> str:
    """The local credential an accepted OCM share's secret is stored as."""
    return f"ocm_share_{int(source_id)}"


def secret_keys(source_id: int) -> tuple[str, str]:
    """The `SECRET_SERVICE` keys a source may hold: its own and a re-offer's."""
    return str(int(source_id)), f"{int(source_id)}:reoffer"


def _begin(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def _from_row(row) -> SharedSource:
    try:
        extras = json.loads(row["extras"]) if row["extras"] else {}
    except ValueError:
        extras = {}
    return SharedSource(
        id=row["id"], user_id=row["user_id"], kind=row["kind"], state=row["state"],
        remote_host=row["remote_host"], remote_key=row["remote_key"], access=row["access"],
        extras=extras if isinstance(extras, dict) else {},
        error_count=row["error_count"], last_reason=row["last_reason"],
        last_ok_at=row["last_ok_at"], next_poll_at=row["next_poll_at"],
        created_at=row["created_at"], updated_at=row["updated_at"],
    )


def _check_kind(kind: object) -> str:
    if kind not in KINDS:
        raise ValueError(f"unknown shared source kind {kind!r}")
    return kind


def create_source(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    kind: str,
    state: str,
    remote_host: str,
    remote_key: str,
    access: str = "read",
    extras: Mapping | None = None,
) -> int:
    """Insert a new source and return its id.

    An ICS feed starts ``active`` and an OCM share ``pending``; any other
    starting state is refused. `SourceExists` when the user already has a row
    for ``(kind, remote_key)``, whatever its state, so the caller can
    reactivate it rather than add a second one.
    """
    _check_kind(kind)
    if state != _INITIAL_STATE[kind]:
        raise ValueError(f"a {kind} starts {_INITIAL_STATE[kind]}, not {state!r}")
    if access not in ACCESS:
        raise ValueError(f"unknown access {access!r}")
    if not isinstance(user_id, str) or not user_id:
        raise ValueError("a source needs an owner")
    if not isinstance(remote_host, str) or not _HOST_RE.fullmatch(remote_host):
        raise ValueError("remote_host must be a host and optional port")
    if not isinstance(remote_key, str) or not remote_key or len(remote_key) > _MAX_KEY_CHARS:
        raise ValueError("remote_key must be a non-empty string")
    if extras is None:
        extras = {}
    if not isinstance(extras, Mapping):
        raise ValueError("extras must be a mapping")
    encoded = json.dumps(dict(extras), sort_keys=True)
    if len(encoded.encode()) > _MAX_EXTRAS_BYTES:
        raise ValueError("extras are too large")

    _begin(conn)
    existing = conn.execute(
        "SELECT id FROM shared_sources WHERE user_id = ? AND kind = ? AND remote_key = ?",
        (user_id, kind, remote_key),
    ).fetchone()
    if existing is not None:
        raise SourceExists(existing[0])
    cur = conn.execute(
        "INSERT INTO shared_sources (user_id, kind, state, remote_host, remote_key, access, extras)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_id, kind, state, remote_host, remote_key, access, encoded),
    )
    return cur.lastrowid


def get_source(conn: sqlite3.Connection, source_id: int, user_id: str) -> SharedSource | None:
    """The user's source by id, or None for a missing id or another user's."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM shared_sources WHERE id = ? AND user_id = ?",
        (source_id, user_id),
    ).fetchone()
    return _from_row(row) if row is not None else None


def find_source(conn: sqlite3.Connection, user_id: str, kind: str,
                remote_key: str) -> SharedSource | None:
    """The user's row for one remote resource, in any state."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM shared_sources WHERE user_id = ? AND kind = ? AND remote_key = ?",
        (user_id, _check_kind(kind), remote_key),
    ).fetchone()
    return _from_row(row) if row is not None else None


def sources_for_user(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    kinds: Sequence[str] | None = None,
    states: Sequence[str] = REACHABLE_STATES,
) -> list[SharedSource]:
    """The user's sources of ``kinds`` (all when None) in ``states``, oldest first."""
    kinds = tuple(_check_kind(kind) for kind in (kinds if kinds is not None else KINDS))
    states = tuple(states)
    if not kinds or not states or any(state not in STATES for state in states):
        raise ValueError("kinds and states must be non-empty and known")
    kind_marks = ", ".join("?" for _ in kinds)
    state_marks = ", ".join("?" for _ in states)
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM shared_sources WHERE user_id = ?"
        f" AND kind IN ({kind_marks}) AND state IN ({state_marks}) ORDER BY id",
        (user_id, *kinds, *states),
    ).fetchall()
    return [_from_row(row) for row in rows]


def sources_for_task(
    conn: sqlite3.Connection,
    task,
    *,
    kind: str,
    skill_index: Mapping[str, object],
) -> list[SharedSource]:
    """The reachable sources of ``kind`` a task may read: the one listing for a task.

    Nothing when the kind's scope is withheld from the task, or when the
    skill that is its scope is not in ``skill_index`` at all; otherwise the
    task user's ``active`` and ``error`` sources.
    """
    from istota.rooms import scopes as room_scopes

    scope = KIND_SCOPE[_check_kind(kind)]
    if scope not in skill_index:
        return []
    if scope in room_scopes.withheld_for_task(conn, task, skill_index=skill_index):
        return []
    return sources_for_user(conn, task.user_id, kinds=(kind,))


def _remove_secrets(conn: sqlite3.Connection, source: SharedSource) -> None:
    for key in secret_keys(source.id):
        secrets_store.delete_secret(None, source.user_id, SECRET_SERVICE, key, connection=conn)
    if source.kind == "ocm_share":
        local_credentials.delete_in(conn, source.user_id, credential_name(source.id))


def set_state(
    conn: sqlite3.Connection,
    source_id: int,
    user_id: str,
    state: str,
    *,
    reason: str | None = None,
) -> bool:
    """Move the user's source to ``state``; False when there is no such source.

    ``reason`` is a fixed code (lowercase, digits, underscores), recorded as
    ``last_reason``, never response text. ``error`` counts up
    ``error_count``; ``active`` resets it and clears the reason. A move the
    lifecycle does not allow raises ``ValueError`` and changes nothing. A move
    into ``declined`` or ``revoked`` deletes the source's secrets in the same
    transaction.
    """
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    if reason is not None and not (isinstance(reason, str) and _REASON_RE.fullmatch(reason)):
        raise ValueError("reason must be a code")
    _begin(conn)
    source = get_source(conn, source_id, user_id)
    if source is None:
        return False
    if state not in _TRANSITIONS[source.state]:
        raise ValueError(f"a {source.state} source cannot become {state}")
    if state == "error":
        conn.execute(
            "UPDATE shared_sources SET state = ?, error_count = error_count + 1, last_reason = ?,"
            " updated_at = datetime('now') WHERE id = ? AND user_id = ?",
            (state, reason, source_id, user_id),
        )
    elif state == "active":
        conn.execute(
            "UPDATE shared_sources SET state = ?, error_count = 0, last_reason = ?,"
            " updated_at = datetime('now') WHERE id = ? AND user_id = ?",
            (state, reason, source_id, user_id),
        )
    else:
        conn.execute(
            "UPDATE shared_sources SET state = ?, last_reason = ?, updated_at = datetime('now')"
            " WHERE id = ? AND user_id = ?",
            (state, reason, source_id, user_id),
        )
    if state in _SECRETLESS_STATES:
        _remove_secrets(conn, source)
    return True


def revoke(conn: sqlite3.Connection, source_id: int, user_id: str, *, reason: str) -> bool:
    """Move the user's source to ``revoked``, taking its secret and any local credential."""
    return set_state(conn, source_id, user_id, "revoked", reason=reason)


def delete_for_user(conn: sqlite3.Connection, user_id: str) -> int:
    """Delete every source the user has, with its secrets and credentials; the count."""
    _begin(conn)
    owned = sources_for_user(conn, user_id, states=STATES)
    for source in owned:
        _remove_secrets(conn, source)
    conn.execute("DELETE FROM shared_sources WHERE user_id = ?", (user_id,))
    return len(owned)
