"""User profile store (Phase 6 of the Docker onboarding spec).

Per-user profile fields (display_name, email_addresses, timezone, log_channel,
alerts_channel, ntfy_topic, worker overrides, disabled_skills,
trusted_email_senders) live in the ``user_profiles`` table. This replaces
the per-user TOML files at ``config/users/{user}.toml``.

Resource entries still live in ``config.toml`` under
``[[users.X.resources]]`` — those are deployment-level topology and stay
ansible-managed. Only the user *profile* moves here.

Resolution order at config-load time:
    1. ``user_profiles`` table       (web-UI / Docker-seeded / ``istota-admin user ensure``)
    2. ``config/users/{user}.toml``  (legacy ansible-managed; fallback)
    3. ``[users.X]`` in main config  (legacy single-file ansible)

A user is "first seen" by the system in any of these places:
- Ansible templates ``config/users/{user}.toml`` and runs
  ``istota-admin user ensure`` (which writes the DB row).
- Docker entrypoint emits ``[[users.X.resources]]`` blocks; the scheduler
  startup auto-seeds an empty profile row from the bare key.
- Web login: the OAuth2 callback calls ``ensure_profile`` to create the
  row from the NC username + display_name.

The DB row, when present, wins. Migration from existing TOML happens once
on scheduler startup; legacy TOML files are left in place (read-only) so
operators can roll back without losing data.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

logger = logging.getLogger(__name__)


@dataclass
class UserProfile:
    """Profile fields that live in the ``user_profiles`` table.

    Mirrors the subset of ``UserConfig`` that Phase 6 moves out of TOML.
    Briefings and resources stay in TOML and are not represented here.
    """

    user_id: str
    display_name: str = ""
    email_addresses: list[str] = field(default_factory=list)
    timezone: str = "UTC"
    log_channel: str = ""
    alerts_channel: str = ""
    ntfy_topic: str = ""
    site_enabled: bool = False
    max_foreground_workers: int = 0
    max_background_workers: int = 0
    disabled_skills: list[str] = field(default_factory=list)
    trusted_email_senders: list[str] = field(default_factory=list)


_PROFILE_COLUMNS = (
    "display_name", "email_addresses", "timezone",
    "log_channel", "alerts_channel", "ntfy_topic",
    "site_enabled", "max_foreground_workers", "max_background_workers",
    "disabled_skills", "trusted_email_senders",
)


@contextmanager
def _connect(db_path: Path) -> Iterator[sqlite3.Connection]:
    """Open a connection with WAL + 30s timeout, matching db.get_db semantics."""
    conn = sqlite3.connect(db_path, timeout=30.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        yield conn
        conn.commit()
    finally:
        conn.close()


def _row_to_profile(row: sqlite3.Row) -> UserProfile:
    return UserProfile(
        user_id=row["user_id"],
        display_name=row["display_name"] or "",
        email_addresses=_parse_json_list(row["email_addresses"]),
        timezone=row["timezone"] or "UTC",
        log_channel=row["log_channel"] or "",
        alerts_channel=row["alerts_channel"] or "",
        ntfy_topic=row["ntfy_topic"] or "",
        site_enabled=bool(row["site_enabled"]),
        max_foreground_workers=int(row["max_foreground_workers"] or 0),
        max_background_workers=int(row["max_background_workers"] or 0),
        disabled_skills=_parse_json_list(row["disabled_skills"]),
        trusted_email_senders=_parse_json_list(row["trusted_email_senders"]),
    )


def _parse_json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as e:
        logger.warning(
            "user_profiles: failed to decode JSON list column (%s); falling back to []",
            e,
        )
        return []
    if not isinstance(parsed, list):
        logger.warning(
            "user_profiles: JSON list column has non-list type %s; falling back to []",
            type(parsed).__name__,
        )
        return []
    return [str(x) for x in parsed]


def get_profile(db_path: Path, user_id: str) -> UserProfile | None:
    """Return the stored profile for ``user_id`` or None if no row exists."""
    with _connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return _row_to_profile(row) if row else None


def list_profiles(db_path: Path) -> dict[str, UserProfile]:
    """Return all stored profiles keyed by user_id."""
    out: dict[str, UserProfile] = {}
    with _connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM user_profiles ORDER BY user_id"
        ).fetchall()
    for row in rows:
        profile = _row_to_profile(row)
        out[profile.user_id] = profile
    return out


def ensure_profile(
    db_path: Path,
    user_id: str,
    *,
    display_name: str = "",
    timezone: str = "",
    seed_from: "object | None" = None,
) -> UserProfile:
    """Insert a row for ``user_id`` if missing, return the resulting profile.

    Existing rows are NOT overwritten — this is the "first-login auto-seed"
    path. Use ``update_profile`` to change values explicitly.

    ``display_name`` and ``timezone`` are accepted as initial values for new
    rows; they are ignored when the row already exists. Caller should pass
    derived defaults (e.g. NC display_name).

    If ``seed_from`` is a ``UserConfig`` (or any object with the matching
    attributes), its list fields and channel/ntfy/worker scalars are copied
    into the new row so the DB row carries the full TOML payload from the
    moment it exists. This eliminates the "DB has display_name but TOML
    has email_addresses" split-brain that would otherwise happen when the
    web callback auto-seeds before the scheduler's TOML import runs.
    """
    existing = get_profile(db_path, user_id)
    if existing is not None:
        return existing

    profile = UserProfile(
        user_id=user_id,
        display_name=display_name or _attr(seed_from, "display_name") or user_id,
        timezone=timezone or _attr(seed_from, "timezone") or "UTC",
        log_channel=_attr(seed_from, "log_channel") or "",
        alerts_channel=_attr(seed_from, "alerts_channel") or "",
        ntfy_topic=_attr(seed_from, "ntfy_topic") or "",
        site_enabled=bool(_attr(seed_from, "site_enabled") or False),
        max_foreground_workers=int(_attr(seed_from, "max_foreground_workers") or 0),
        max_background_workers=int(_attr(seed_from, "max_background_workers") or 0),
        email_addresses=list(_attr(seed_from, "email_addresses") or []),
        trusted_email_senders=list(_attr(seed_from, "trusted_email_senders") or []),
        disabled_skills=list(_attr(seed_from, "disabled_skills") or []),
    )
    _insert(db_path, profile)
    logger.info("ensured user_profile user=%s (new row)", user_id)
    return profile


def ensure_profile_with_status(
    db_path: Path,
    user_id: str,
    *,
    display_name: str = "",
    timezone: str = "",
    seed_from: "object | None" = None,
) -> tuple[UserProfile, bool]:
    """Same as ``ensure_profile`` but returns ``(profile, created)``.

    ``created`` is True iff this call inserted a new row. Web UI auto-seed
    in the OAuth callback uses this to decide whether to refresh
    ``display_name`` from NC: the spec is "first-login auto-seed", not
    "every login overwrites." Without this signal, a user whose NC
    display_name happens to equal their user_id triggers the
    placeholder-detection heuristic on every login.
    """
    existing = get_profile(db_path, user_id)
    if existing is not None:
        return existing, False
    profile = ensure_profile(
        db_path, user_id,
        display_name=display_name, timezone=timezone, seed_from=seed_from,
    )
    return profile, True


def _attr(obj: "object | None", name: str) -> "object | None":
    """Best-effort attribute read; returns None if obj is falsy or attr missing."""
    if obj is None:
        return None
    return getattr(obj, name, None)


def upsert_profile(db_path: Path, profile: UserProfile) -> None:
    """Replace the entire row for ``profile.user_id``.

    Use this for ``istota-admin user ensure`` and one-time TOML migration.
    Web UI writes go through :func:`update_profile` (partial update).
    """
    _insert(db_path, profile, replace=True)


def update_profile(
    db_path: Path,
    user_id: str,
    **fields: object,
) -> UserProfile:
    """Partial update — only specified columns change. Returns the new profile.

    Raises ValueError if the user has no row yet (caller should ensure first)
    or if an unknown field is passed (defends against schema drift).
    """
    if not fields:
        existing = get_profile(db_path, user_id)
        if existing is None:
            raise ValueError(f"no user_profile row for {user_id!r}")
        return existing

    unknown = set(fields) - set(_PROFILE_COLUMNS)
    if unknown:
        raise ValueError(f"unknown profile field(s): {sorted(unknown)}")

    sets: list[str] = []
    params: list[object] = []
    for col, value in fields.items():
        if col in {"email_addresses", "disabled_skills", "trusted_email_senders"}:
            value = json.dumps(list(value or []))
        elif col == "site_enabled":
            value = 1 if value else 0
        elif col in {"max_foreground_workers", "max_background_workers"}:
            value = int(value or 0)
        else:
            value = str(value or "")
        sets.append(f"{col} = ?")
        params.append(value)

    sets.append("updated_at = datetime('now')")
    params.append(user_id)

    with _connect(db_path) as conn:
        cur = conn.execute(
            f"UPDATE user_profiles SET {', '.join(sets)} WHERE user_id = ?",
            params,
        )
        if cur.rowcount == 0:
            raise ValueError(f"no user_profile row for {user_id!r}")

    updated = get_profile(db_path, user_id)
    assert updated is not None  # row was just updated
    return updated


def delete_profile(db_path: Path, user_id: str) -> bool:
    """Remove a profile row. Returns True if a row was removed."""
    with _connect(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM user_profiles WHERE user_id = ?", (user_id,),
        )
        return cur.rowcount > 0


def _insert(db_path: Path, profile: UserProfile, *, replace: bool = False) -> None:
    """Insert (replace=False) or upsert (replace=True) a profile row.

    ``created_at`` survives an upsert because we use ON CONFLICT instead of
    INSERT OR REPLACE. ``updated_at`` is always set to ``datetime('now')``.
    """
    insert_cols = ("user_id", *_PROFILE_COLUMNS)
    placeholders = ", ".join(["?"] * len(insert_cols))
    values = (
        profile.user_id,
        profile.display_name,
        json.dumps(list(profile.email_addresses)),
        profile.timezone,
        profile.log_channel,
        profile.alerts_channel,
        profile.ntfy_topic,
        1 if profile.site_enabled else 0,
        int(profile.max_foreground_workers or 0),
        int(profile.max_background_workers or 0),
        json.dumps(list(profile.disabled_skills)),
        json.dumps(list(profile.trusted_email_senders)),
    )
    cols_sql = ", ".join(insert_cols)
    if replace:
        update_clauses = ",\n                ".join(
            f"{c} = excluded.{c}" for c in _PROFILE_COLUMNS
        )
        sql = f"""
            INSERT INTO user_profiles ({cols_sql}, updated_at)
            VALUES ({placeholders}, datetime('now'))
            ON CONFLICT(user_id) DO UPDATE SET
                {update_clauses},
                updated_at = datetime('now')
        """
    else:
        sql = f"""
            INSERT OR IGNORE INTO user_profiles ({cols_sql}, updated_at)
            VALUES ({placeholders}, datetime('now'))
        """

    with _connect(db_path) as conn:
        conn.execute(sql, values)


# --- Migration: TOML → DB --------------------------------------------------
#
# One-time import on every scheduler startup. Walks the loaded TOML
# UserConfig values; copies profile fields into the user_profiles table —
# but only for users that don't already have a row. Idempotent across
# restarts; safe to call before any TOML files exist.

def import_from_user_configs(
    db_path: Path,
    user_configs: dict[str, "object"],
) -> int:
    """Seed user_profiles rows from loaded TOML user configs.

    Skips users that already have a DB row (DB wins, never overwritten).
    Returns the number of rows written. Logs each new row at INFO level so
    operators can tell when migration runs vs. no-op restarts.
    """
    written = 0
    for user_id, user_config in user_configs.items():
        existing = get_profile(db_path, user_id)
        if existing is not None:
            continue

        profile = UserProfile(
            user_id=user_id,
            display_name=getattr(user_config, "display_name", "") or user_id,
            email_addresses=list(getattr(user_config, "email_addresses", []) or []),
            timezone=getattr(user_config, "timezone", "") or "UTC",
            log_channel=getattr(user_config, "log_channel", "") or "",
            alerts_channel=getattr(user_config, "alerts_channel", "") or "",
            ntfy_topic=getattr(user_config, "ntfy_topic", "") or "",
            site_enabled=bool(getattr(user_config, "site_enabled", False)),
            max_foreground_workers=int(getattr(user_config, "max_foreground_workers", 0) or 0),
            max_background_workers=int(getattr(user_config, "max_background_workers", 0) or 0),
            disabled_skills=list(getattr(user_config, "disabled_skills", []) or []),
            trusted_email_senders=list(getattr(user_config, "trusted_email_senders", []) or []),
        )
        try:
            _insert(db_path, profile, replace=False)
            written += 1
            logger.info("user_profile imported from TOML user=%s", user_id)
        except Exception as e:
            logger.warning("user_profile import failed user=%s: %s", user_id, e)

    if written:
        logger.info("user_profiles migration: wrote %d new row(s) from TOML", written)
    return written


def merge_into_user_config(profile: UserProfile, user_config: "object") -> "object":
    """Apply DB profile fields onto a TOML-loaded ``UserConfig`` in place.

    The DB row, when it exists, is authoritative for every field it owns.
    Briefings and resources stay TOML-only.

    Why this rule rather than "DB wins only when non-empty":
    A user clearing their email addresses via the web UI must not have the
    TOML resurrect them on the next config reload. Because ``ensure_profile``
    seeds list fields from the full TOML ``UserConfig`` at row-creation time
    (see :func:`ensure_profile`), an "empty" list in the DB unambiguously
    means "the user explicitly emptied it" — not "row not yet populated."
    """
    if user_config is None:
        return user_config

    setattr(user_config, "display_name", profile.display_name or getattr(user_config, "display_name", "") or profile.user_id)
    setattr(user_config, "timezone", profile.timezone or "UTC")
    for attr in (
        "log_channel", "alerts_channel", "ntfy_topic",
        "site_enabled", "max_foreground_workers", "max_background_workers",
    ):
        setattr(user_config, attr, getattr(profile, attr))

    # List fields: DB row owns them once it exists. The auto-seed path
    # carries TOML lists into the row, so an empty DB list is a deliberate
    # "user cleared this" signal.
    for attr in ("email_addresses", "disabled_skills", "trusted_email_senders"):
        setattr(user_config, attr, list(getattr(profile, attr) or []))

    return user_config
