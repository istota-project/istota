"""Generated credentials stored in the encrypted table and retired by the user."""

from __future__ import annotations

import json
import logging

from istota import db
from istota.credentials import store as secrets_store
from istota.credentials.broker import bindings as _bindings
from istota.lib import totp

logger = logging.getLogger(__name__)

SOURCE = "generated"
PREFIX = "generated_"
_SERVICE = "vault_entries"
# Cannot collide with a credential name, which always starts with PREFIX.
# Set once the first complete read after ISSUE-686 has adopted legacy rows.


KIND_RECOVERY = "recovery"
# The KeePass custom field the export writes the codes to, protected.
RECOVERY_FIELD = "Recovery codes"
RECOVERY_MAX_BYTES = 8192
RECOVERY_MAX_LINES = 64
_KINDS = {"otp": "totp", "recovery": KIND_RECOVERY}

_FIELDS = ("password", "username", "url", "otp", "recovery")


class GeneratedCredentialError(ValueError):
    """A refusal with a fixed ``reason`` code; the message names no value."""

    def __init__(self, reason: str, message: str):
        super().__init__(message)
        self.reason = reason


def entry_names(name: str) -> dict[str, str]:
    """Field -> row name for the generated credential ``name``."""
    return {"password": name, "username": name + "_username",
            "url": name + "_url", "otp": name + "_totp", "recovery": name + "_recovery"}


def _begin(conn) -> None:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def is_generated(conn, user_id: str, name: str) -> bool:
    """Whether ``name`` is a generated credential, by its own name (not a field)."""
    if not isinstance(name, str) or not name.startswith(PREFIX):
        return False
    if _bindings.credential_name(conn, user_id, name) != name:
        return False
    binding = _bindings.get_binding(conn, user_id, name)
    return binding is not None and binding["source"] == SOURCE


def generated_names(conn, user_id: str) -> list[str]:
    """Every generated credential this user has, by name."""
    rows = conn.execute(
        "SELECT name FROM credential_bindings WHERE user_id=? AND source=?", (user_id, SOURCE),
    ).fetchall()
    return sorted({row[0] for row in rows
                   if _bindings.credential_name(conn, user_id, row[0]) == row[0]})


def _binding(url: str, *, owner: str, kind: str = "value") -> dict:
    binding = _bindings.parse_binding(url, {}, [], source=SOURCE)
    return {**binding, "credential": owner, "kind": kind}


def _taken(conn, user_id: str, names) -> str | None:
    for candidate in names:
        if conn.execute(
            "SELECT 1 FROM secrets WHERE user_id=? AND service=? AND key=? "
            "UNION SELECT 1 FROM credential_bindings WHERE user_id=? AND name=?",
            (user_id, _SERVICE, candidate, user_id, candidate),
        ).fetchone() is not None:
            return candidate
    return None


def _write_rows(conn, user_id: str, name: str, values: dict, *, actor: str = "system") -> None:
    rows = entry_names(name)
    url = values.get("url") or ""
    for field_name, row in rows.items():
        value = values.get(field_name) or ""
        if not value:
            continue
        kind = _KINDS.get(field_name, "value")
        secrets_store.set_secret(None, user_id, _SERVICE, row, value,
                                 binding=_binding(url, owner=name, kind=kind), connection=conn, actor=actor)


def create(conn, user_id: str, *, name: str, username: str, password: str, url: str,
           actor: str = "system") -> dict[str, str]:
    """Store a new generated credential. The caller's transaction commits it.

    Refuses with ``name_taken`` when any of its row names is held by any
    source: a generated name is never shared with a file entry or a local row.
    """
    _begin(conn)
    rows = entry_names(name)
    taken = _taken(conn, user_id, rows.values())
    if taken is not None:
        raise GeneratedCredentialError("name_taken", f"credential name already exists: {taken}")
    _write_rows(conn, user_id, name, {"password": password, "username": username, "url": url}, actor=actor)
    return rows


def set_otp(conn, user_id: str, name: str, uri: str, *, replace: bool = False,
            actor: str = "system") -> str:
    """Attach a factor; explicit imports may replace an existing owned seed.

    The seed takes the entry's hosts and ``credential: <name>``, so the entry's
    grant covers it (ISSUE-685) and ``--fill-otp`` resolves it by entry name.
    """
    _begin(conn)
    if not is_generated(conn, user_id, name):
        raise GeneratedCredentialError("otp_set_not_generated", "otp_set_not_generated")
    seed_name = entry_names(name)["otp"]
    if _taken(conn, user_id, (seed_name,)) is not None:
        if not replace:
            raise GeneratedCredentialError("otp_already_set", "otp_already_set")
        binding = _bindings.get_binding(conn, user_id, seed_name)
        if (binding is None or binding["source"] != SOURCE
                or _bindings.credential_name(conn, user_id, seed_name) != name):
            raise GeneratedCredentialError("name_taken", "name_taken")
    canonical = totp.to_uri(totp.parse_user_input(uri))
    url = secrets_store.get_secret(None, user_id, _SERVICE, entry_names(name)["url"], connection=conn)
    secrets_store.set_secret(None, user_id, _SERVICE, seed_name, canonical,
                             binding=_binding(url or "", owner=name, kind="totp"), connection=conn, actor=actor)
    return seed_name


def normalize_recovery(text: str) -> str:
    """The stored form of a block of recovery codes: one trimmed line each.

    Kept as text rather than parsed into codes, since sites format them every
    way there is; blank lines and surrounding space are all that is dropped.
    """
    if not isinstance(text, str):
        raise GeneratedCredentialError("recovery_empty", "recovery_empty")
    lines = [line.strip() for line in text.splitlines()]
    lines = [line for line in lines if line]
    if not lines:
        raise GeneratedCredentialError("recovery_empty", "recovery_empty")
    # KeePass's XML cannot hold these, and the export writes the entry whole.
    if any(ord(c) < 32 and c != "\t" or ord(c) == 127 for line in lines for c in line):
        raise GeneratedCredentialError("recovery_unusable", "recovery_unusable")
    normalized = "\n".join(lines)
    if (len(lines) > RECOVERY_MAX_LINES
            or len(normalized.encode("utf-8", "surrogatepass")) > RECOVERY_MAX_BYTES):
        raise GeneratedCredentialError("recovery_too_large", "recovery_too_large")
    return normalized


def set_recovery(conn, user_id: str, name: str, text: str, *, fmt: str = "block", actor: str = "system") -> tuple[str, int, bool]:
    """Store, or replace, a generated credential's recovery codes (ISSUE-688).

    Returns ``(row name, line count, replaced)``. Replacing rather than
    refusing like :func:`set_otp`, because a site that regenerates its codes
    invalidates the old set. The row takes the entry's hosts and
    ``credential: <name>`` like the seed, with ``kind = 'recovery'``.
    """
    from istota.credentials.names import VAULT_NAME_RE

    _begin(conn)
    if not is_generated(conn, user_id, name):
        raise GeneratedCredentialError("recovery_set_not_generated", "recovery_set_not_generated")
    row_name = entry_names(name)["recovery"]
    if not VAULT_NAME_RE.match(row_name):
        raise GeneratedCredentialError("recovery_name_too_long", "recovery_name_too_long")
    if fmt not in ("codes", "phrase", "block"):
        raise GeneratedCredentialError("recovery_format", "Unsupported recovery format")
    normalized = normalize_recovery(text)
    existing = _bindings.get_binding(conn, user_id, row_name)
    if existing is not None and (existing["source"] != SOURCE
                                 or _bindings.credential_name(conn, user_id, row_name) != name):
        raise GeneratedCredentialError("name_taken", f"credential name already exists: {row_name}")
    replaced = conn.execute(
        "SELECT 1 FROM secrets WHERE user_id=? AND service=? AND key=?",
        (user_id, _SERVICE, row_name),
    ).fetchone() is not None
    url = secrets_store.get_secret(None, user_id, _SERVICE, entry_names(name)["url"], connection=conn)
    secrets_store.set_secret(None, user_id, _SERVICE, row_name, normalized,
                             binding=_binding(url or "", owner=name, kind=KIND_RECOVERY),
                             connection=conn, actor=actor)
    total = len(normalized.splitlines())
    conn.execute("""INSERT INTO recovery_code_state (user_id, name, format, total, captured_at)
                    VALUES (?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(user_id, name) DO UPDATE SET format=excluded.format,
                    total=excluded.total, spent='[]', captured_at=excluded.captured_at,
                    updated_at=datetime('now')""", (user_id, name, fmt, total))
    return row_name, total, replaced


def recovery_state(conn, user_id: str, name: str) -> dict | None:
    value = read_recovery(conn, user_id, name)
    if value is None:
        return None
    row = conn.execute("SELECT format, total, spent FROM recovery_code_state WHERE user_id=? AND name=?",
                       (user_id, name)).fetchone()
    fmt, total, spent = (row[0], row[1], json.loads(row[2])) if row else ("block", len(value.splitlines()), [])
    return {"format": fmt, "total": total, "spent": spent,
            "remaining": total - len(spent) if fmt == "codes" else None}


def read_recovery(conn, user_id: str, name: str) -> str | None:
    """The stored codes, for the user's own view in Settings. Never a task's."""
    if not is_generated(conn, user_id, name):
        return None
    value = secrets_store.get_secret(None, user_id, _SERVICE, entry_names(name)["recovery"],
                                     connection=conn)
    return value if isinstance(value, str) and value else None


def has_recovery(conn, user_id: str, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM credential_bindings WHERE user_id=? AND name=? AND kind=?",
        (user_id, entry_names(name)["recovery"], KIND_RECOVERY),
    ).fetchone() is not None


def stored_values(conn, user_id: str, name: str) -> dict[str, str]:
    """The table's copy, field -> value ("" for an absent row). Daemon-only."""
    values = {}
    for field_name, row in entry_names(name).items():
        value = secrets_store.get_secret(None, user_id, _SERVICE, row, connection=conn)
        values[field_name] = value if isinstance(value, str) else ""
    return values


def retire(config, user_id: str, name: str, *, actor: str = "system") -> bool:
    """Delete a generated credential and its grant, leaving exported files alone."""
    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM secrets WHERE user_id=? AND service=? AND key=?",
                        (user_id, _SERVICE, name)).fetchone() is None:
            return False
        name = _bindings.credential_name(conn, user_id, name)
        if not is_generated(conn, user_id, name):
            raise GeneratedCredentialError("not_generated", f"{name} was not generated by Istota")
        secrets_store.delete_secret(None, user_id, _SERVICE, name, all_fields=True, connection=conn, actor=actor)
        conn.execute("DELETE FROM recovery_code_state WHERE user_id=? AND name=?", (user_id, name))
        db.close_signup_tag(conn, user_id, name[len(PREFIX):])
        from istota.credentials import audit
        audit.record(conn, user_id, action="retire", actor=actor, name=name)
    logger.info("generated: %s: retired %s", _label(user_id), _label(name))
    return True


def _label(value: str) -> str:
    from istota.credentials.names import _label as label
    return label(value)
