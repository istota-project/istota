"""User briefing store (Phase 7b of the Docker onboarding spec).

Per-user briefings (cron-scheduled summaries delivered to a Talk room or
email) live in the ``briefing_configs`` table. This replaces the
``[[briefings]]`` blocks that used to live in per-user TOML files.

Resolution order at config-load time:
    1. ``briefing_configs`` table        (web-UI / ``istota briefing ensure``)
    2. ``[[briefings]]`` in TOML         (legacy ansible-managed; fallback)
    3. Workspace ``BRIEFINGS.md``        (user-editable, takes precedence at
                                          read time in ``get_briefings_for_user``)

Briefings stored in the DB are merged into ``UserConfig.briefings`` by
``_apply_user_briefings`` at the tail of ``load_config``. The runtime
read path (``get_briefings_for_user`` in ``skills/briefing``) keeps its
existing workspace-overrides-admin precedence on top of that.

The DB row, when present, replaces a TOML row of the same name. New
briefings only present in the DB simply add to the user's list.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger(__name__)


@dataclass
class UserBriefing:
    """A briefing config row.

    Mirrors :class:`istota.config.BriefingConfig` plus the DB-only
    ``id``/``user_id``/``enabled`` columns.
    """

    id: int
    user_id: str
    name: str
    cron: str
    conversation_token: str = ""
    output: str = "talk"
    components: dict[str, Any] = field(default_factory=dict)
    enabled: bool = True


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection with 30s timeout, matching db.get_db semantics.

    WAL is persistent in the SQLite file header; re-issuing
    ``PRAGMA journal_mode=WAL`` per connection costs a write-lock
    acquisition and races with sibling readers. The framework
    ``istota.db`` is initialised in WAL mode at ``init_db`` time.
    """
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    finally:
        conn.close()


def _decode_components(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        decoded = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        logger.warning("briefing_configs.components contained invalid JSON; defaulting to {}")
        return {}
    return decoded if isinstance(decoded, dict) else {}


def _row_key(row: sqlite3.Row, key: str) -> Any:
    """Read a column that may be absent on a not-yet-migrated row."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _row_to_briefing(row: sqlite3.Row) -> UserBriefing:
    # The DB column is named ``cron_expression`` for legacy reasons; the
    # in-memory ``BriefingConfig`` uses ``cron`` (matches the TOML key).
    components = _decode_components(row["components"])
    # ``output`` is a real column now. Defensively fall back to a legacy
    # ``__output__`` component key when reading a mid-migration row whose
    # column read yields the default while the key is still present.
    raw_output = _row_key(row, "output")
    output = raw_output if isinstance(raw_output, str) and raw_output.strip() else "talk"
    legacy_output = components.pop("__output__", None)
    if output == "talk" and isinstance(legacy_output, str) and legacy_output.strip():
        output = legacy_output
    return UserBriefing(
        id=int(row["id"]),
        user_id=row["user_id"],
        name=row["name"],
        cron=row["cron_expression"] or "",
        conversation_token=row["conversation_token"] or "",
        output=output,
        components=components,
        enabled=bool(row["enabled"]),
    )


def list_briefings(db_path: Path, user_id: str | None = None) -> list[UserBriefing]:
    """Return briefing rows. When ``user_id`` is set, scope to that user."""
    if not Path(db_path).exists():
        return []
    with _connect(db_path) as conn:
        if user_id is not None:
            rows = conn.execute(
                "SELECT * FROM briefing_configs WHERE user_id = ? ORDER BY name",
                (user_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM briefing_configs ORDER BY user_id, name"
            ).fetchall()
    return [_row_to_briefing(r) for r in rows]


def get_briefing(db_path: Path, user_id: str, name: str) -> UserBriefing | None:
    """Return a single briefing or None."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM briefing_configs WHERE user_id = ? AND name = ?",
            (user_id, name),
        ).fetchone()
    return _row_to_briefing(row) if row else None


def ensure_briefing(
    db_path: Path,
    *,
    user_id: str,
    name: str,
    cron: str,
    conversation_token: str = "",
    output: str = "talk",
    components: dict[str, Any] | None = None,
    enabled: bool = True,
) -> tuple[UserBriefing, str]:
    """Idempotent upsert. Returns ``(briefing, state)``.

    ``state`` is one of ``"created"``, ``"updated"``, ``"noop"`` — same
    contract as ``istota user ensure`` / ``istota resource ensure``.
    """
    if not name:
        raise ValueError("briefing name cannot be empty")
    if not cron:
        raise ValueError("briefing cron cannot be empty")
    # Accept any output_target descriptor (talk/email/both/all/ntfy/talk:<tok>/
    # comma lists). Unknown surfaces are warn-and-dropped at delivery, not here.
    from .transport import parse_output_target
    if not parse_output_target(output):
        raise ValueError(
            f"briefing output must be a valid delivery descriptor, got {output!r}"
        )

    components = dict(components or {})
    components_json = json.dumps(components, sort_keys=True)
    enabled_int = 1 if enabled else 0

    existing = get_briefing(db_path, user_id, name)
    if existing is None:
        state = "created"
    else:
        same = (
            existing.cron == cron
            and existing.conversation_token == (conversation_token or "")
            and existing.output == output
            and existing.components == components
            and existing.enabled == enabled
        )
        state = "noop" if same else "updated"

    if state == "noop":
        return existing, state  # type: ignore[return-value]

    with _connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO briefing_configs
                (user_id, name, cron_expression, conversation_token, components, output, enabled)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (user_id, name) DO UPDATE SET
                cron_expression = excluded.cron_expression,
                conversation_token = excluded.conversation_token,
                components = excluded.components,
                output = excluded.output,
                enabled = excluded.enabled
            """,
            (user_id, name, cron, conversation_token or "", components_json, output, enabled_int),
        )

    fresh = get_briefing(db_path, user_id, name)
    assert fresh is not None
    return fresh, state


def delete_briefing(db_path: Path, user_id: str, name: str) -> bool:
    """Remove a briefing by (user_id, name). Returns True if a row was removed."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM briefing_configs WHERE user_id = ? AND name = ?",
            (user_id, name),
        )
        return cur.rowcount > 0


def delete_briefing_by_id(db_path: Path, user_id: str, briefing_id: int) -> bool:
    """Delete a briefing by id, scoped to user_id (web UI safety).

    The user_id scope prevents one user from deleting another user's
    briefing by guessing IDs from the URL. Returns True if a row was
    removed.
    """
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM briefing_configs WHERE id = ? AND user_id = ?",
            (briefing_id, user_id),
        )
        return cur.rowcount > 0


# --- Migration: TOML → DB --------------------------------------------------

def import_from_user_configs(
    db_path: Path,
    user_configs: "dict[str, object]",
) -> int:
    """Seed ``briefing_configs`` rows from loaded TOML user configs.

    Walks ``user_config.briefings`` for each user; inserts any
    ``(user_id, name)`` pair that doesn't already have a DB row.
    Idempotent across restarts.
    """
    if not Path(db_path).exists():
        return 0

    written = 0
    with _connect(db_path) as conn:
        existing_keys = {
            (r["user_id"], r["name"])
            for r in conn.execute(
                "SELECT user_id, name FROM briefing_configs"
            ).fetchall()
        }

        for user_id, user_config in user_configs.items():
            briefings = getattr(user_config, "briefings", None) or []
            for b in briefings:
                key = (user_id, getattr(b, "name", ""))
                if not key[1] or key in existing_keys:
                    continue

                cron = getattr(b, "cron", "") or ""
                if not cron:
                    continue

                output = getattr(b, "output", "talk") or "talk"
                token = getattr(b, "conversation_token", "") or ""
                comps = dict(getattr(b, "components", {}) or {})

                try:
                    conn.execute(
                        """
                        INSERT INTO briefing_configs
                            (user_id, name, cron_expression, conversation_token, components, output, enabled)
                        VALUES (?, ?, ?, ?, ?, ?, 1)
                        """,
                        (
                            user_id,
                            key[1],
                            cron,
                            token,
                            json.dumps(comps, sort_keys=True),
                            output,
                        ),
                    )
                    written += 1
                    logger.info(
                        "briefing imported from TOML user=%s name=%s",
                        user_id, key[1],
                    )
                except Exception as e:  # pragma: no cover - defensive
                    logger.warning(
                        "briefing import failed user=%s name=%s: %s",
                        user_id, key[1], e,
                    )

    if written:
        logger.info("user_briefings migration: wrote %d new row(s) from TOML", written)
    return written
