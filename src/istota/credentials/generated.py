"""Credentials Istota generates: the one writer of ``source="generated"`` rows.

``istota-credential new`` and ``otp-set`` used to write the user's KeePass file
and reach the ``secrets`` table only through the sync, which treats the file as
authoritative, deletions included. For a credential the user created that is
the right rule; for one Istota created it made the file the only copy, and an
older copy of the file saved over Istota's write deleted the row (ISSUE-686: a
TOTP seed lost that way locked the account). So ownership follows whoever
created the credential: a generated credential lives here, in the table, and
the KDBX ``generated/`` group is an optional one-way mirror of it.

The rows have the shape every other source has (value, ``_username``,
``_url``, ``_totp``, each bound with ``credential: <name>``), so readers do not
branch on the source. ``_recovery`` (ISSUE-688) is the one field only a
generated credential has: the site's single-use recovery codes, bound with
``kind = 'recovery'``, which no task read path returns. What differs:

- **The sync never writes, rebinds or deletes a generated row.** A file copy
  that differs is reported as divergence on the settings card and in a notice,
  never applied. :func:`reconcile` is the sync's whole involvement.
- **The mirror state is per credential**, in the reserved KV namespace
  :data:`NAMESPACE`: ``mirror`` (the toggle), ``state`` (``off``, ``pending``,
  ``mirrored``, ``diverged``) and ``divergence`` (``missing``,
  ``missing_otp``, ``changed``). ``pending`` is a write that has not landed yet
  and is retried each sync cycle; ``diverged`` is a copy that was written and
  later changed, which waits for the user's re-mirror.
- **Retiring is the only deletion**, and it is a user action (settings and
  ``istota secret retire``). Nothing a task can send reaches :func:`retire`.

**No value leaves this module** except to the mirror writer and the sync's
comparison, both in the daemon. Errors carry names and a fixed reason.
"""

from __future__ import annotations

import json
import logging

from istota import db
from istota.credentials import store as secrets_store
from istota.credentials.broker import bindings as _bindings
from istota.lib import totp

logger = logging.getLogger(__name__)

SOURCE = "generated"
NAMESPACE = "_generated_credentials"
PREFIX = "generated_"
_SERVICE = "vault_entries"
# Cannot collide with a credential name, which always starts with PREFIX.
_DEFAULT_KEY = "default_mirror"
# Set once the first complete read after ISSUE-686 has adopted legacy rows.
_LEGACY_KEY = "legacy_adopted"
_RESERVED_KEYS = (_DEFAULT_KEY, _LEGACY_KEY)

STATE_OFF = "off"
STATE_PENDING = "pending"
STATE_MIRRORED = "mirrored"
STATE_DIVERGED = "diverged"
STATE_RETIRED = "retired"

DIVERGED_MISSING = "missing"
DIVERGED_MISSING_OTP = "missing_otp"
DIVERGED_CHANGED = "changed"
DIVERGED_MISSING_RECOVERY = "missing_recovery"

KIND_RECOVERY = "recovery"
# The KeePass custom field the mirror writes the codes to, protected.
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


def _get_meta(conn, user_id: str, name: str) -> dict | None:
    row = db.kv_get(conn, user_id, NAMESPACE, name)
    if row is None:
        return None
    try:
        meta = json.loads(row["value"])
    except (TypeError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _put_meta(conn, user_id: str, name: str, *, mirror: bool, state: str,
              divergence: list[str] | None = None, **extra) -> None:
    db.kv_set(conn, user_id, NAMESPACE, name, json.dumps(
        {"mirror": mirror, "state": state, "divergence": divergence or [], **extra}))


def default_mirror(conn, user_id: str) -> bool:
    """Whether a new generated credential is mirrored to KeePass. On unless set off."""
    row = db.kv_get(conn, user_id, NAMESPACE, _DEFAULT_KEY)
    return row is None or row["value"] != "0"


def set_default_mirror(conn, user_id: str, on: bool) -> None:
    db.kv_set(conn, user_id, NAMESPACE, _DEFAULT_KEY, "1" if on else "0")


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


def mirror_state(conn, user_id: str, name: str) -> dict:
    """``{"mirror", "state", "divergence"}`` for the settings card and tests."""
    meta = _get_meta(conn, user_id, name) or {"mirror": False, "state": STATE_OFF}
    mirror = bool(meta.get("mirror"))
    state = meta.get("state") if mirror else STATE_OFF
    divergence = list(meta.get("divergence") or []) if state == STATE_DIVERGED else []
    return {"mirror": mirror, "state": state or STATE_PENDING, "divergence": divergence}


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
           mirror: bool, actor: str = "system") -> dict[str, str]:
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
    _put_meta(conn, user_id, name, mirror=mirror, state=STATE_PENDING if mirror else STATE_OFF)
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
    meta = mirror_state(conn, user_id, name)
    if meta["mirror"]:
        _put_meta(conn, user_id, name, mirror=True, state=STATE_PENDING)
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
    # KeePass's XML cannot hold these, and the mirror writes the entry whole.
    if any(ord(c) < 32 and c != "\t" or ord(c) == 127 for line in lines for c in line):
        raise GeneratedCredentialError("recovery_unusable", "recovery_unusable")
    normalized = "\n".join(lines)
    if (len(lines) > RECOVERY_MAX_LINES
            or len(normalized.encode("utf-8", "surrogatepass")) > RECOVERY_MAX_BYTES):
        raise GeneratedCredentialError("recovery_too_large", "recovery_too_large")
    return normalized


def set_recovery(conn, user_id: str, name: str, text: str, *, actor: str = "system") -> tuple[str, int, bool]:
    """Store, or replace, a generated credential's recovery codes (ISSUE-688).

    Returns ``(row name, line count, replaced)``. Replacing rather than
    refusing like :func:`set_otp`, because a site that regenerates its codes
    invalidates the old set. The row takes the entry's hosts and
    ``credential: <name>`` like the seed, with ``kind = 'recovery'``.
    """
    from istota.credentials.vault import VAULT_NAME_RE

    _begin(conn)
    if not is_generated(conn, user_id, name):
        raise GeneratedCredentialError("recovery_set_not_generated", "recovery_set_not_generated")
    row_name = entry_names(name)["recovery"]
    if not VAULT_NAME_RE.match(row_name):
        raise GeneratedCredentialError("recovery_name_too_long", "recovery_name_too_long")
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
    if mirror_state(conn, user_id, name)["mirror"]:
        _put_meta(conn, user_id, name, mirror=True, state=STATE_PENDING)
    return row_name, len(normalized.splitlines()), replaced


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


def _same_otp(left: str, right: str) -> bool:
    if not left or not right:
        return left == right
    try:
        return totp.parse_otpauth(left) == totp.parse_otpauth(right)
    except totp.TotpError:
        return False


def divergence(stored: dict[str, str], copy: dict[str, str] | None) -> list[str]:
    """How the file's copy differs from the table. Empty means it matches."""
    if copy is None:
        return [DIVERGED_MISSING]
    found = []
    if stored.get("otp") and not copy.get("otp"):
        found.append(DIVERGED_MISSING_OTP)
    elif not _same_otp(stored.get("otp", ""), copy.get("otp", "")):
        found.append(DIVERGED_CHANGED)
    if stored.get("recovery") and not copy.get("recovery"):
        found.append(DIVERGED_MISSING_RECOVERY)
    elif DIVERGED_CHANGED not in found and _recovery_lines(stored) != _recovery_lines(copy):
        found.append(DIVERGED_CHANGED)
    if DIVERGED_CHANGED not in found and any(
        (stored.get(field_name) or "") != (copy.get(field_name) or "")
        for field_name in ("password", "username", "url")
    ):
        found.append(DIVERGED_CHANGED)
    return found


def _recovery_lines(values: dict[str, str]) -> list[str]:
    return [line.strip() for line in (values.get("recovery") or "").splitlines() if line.strip()]


def record_mirror_result(conn, user_id: str, name: str, written: dict[str, str],
                         landed: bool) -> str:
    """Record a mirror write against the state as it is now, not as it was read.

    The write ran outside any transaction, so the credential may have been
    retired, its mirror switched off, or its values changed (an ``otp-set``)
    meanwhile. Only a write of the current values to a mirror still on counts
    as ``mirrored``; a copy written after a retire queues its removal again.
    """
    _begin(conn)
    meta = _get_meta(conn, user_id, name)
    if meta is None:
        return STATE_OFF
    if meta.get("state") == STATE_RETIRED:
        if landed:
            _put_meta(conn, user_id, name, mirror=bool(meta.get("mirror")), state=STATE_RETIRED,
                      copy_removed=False)
        return STATE_RETIRED
    if not meta.get("mirror") or meta.get("state") == STATE_OFF:
        return STATE_OFF
    if landed and stored_values(conn, user_id, name) == written:
        _put_meta(conn, user_id, name, mirror=True, state=STATE_MIRRORED)
        return STATE_MIRRORED
    _put_meta(conn, user_id, name, mirror=True, state=STATE_PENDING)
    return STATE_PENDING


def record_copy_removed(conn, user_id: str, name: str) -> None:
    """A retired credential's KeePass copy is gone; the tombstone itself stays."""
    _begin(conn)
    meta = _get_meta(conn, user_id, name)
    if meta is not None and meta.get("state") == STATE_RETIRED:
        _put_meta(conn, user_id, name, mirror=bool(meta.get("mirror")), state=STATE_RETIRED,
                  copy_removed=True)


def set_mirror(conn, user_id: str, name: str, on: bool) -> None:
    """The per-credential toggle. Turning it on queues a write; off leaves the copy."""
    _begin(conn)
    if not is_generated(conn, user_id, name):
        raise GeneratedCredentialError("not_generated", f"{name} was not generated by Istota")
    current = mirror_state(conn, user_id, name)
    if on and current["mirror"]:
        return
    _put_meta(conn, user_id, name, mirror=on, state=STATE_PENDING if on else STATE_OFF)


def pending_names(conn, user_id: str) -> tuple[list[str], list[str]]:
    """``(to_write, to_remove)``: mirror writes not yet landed, and retired copies."""
    to_write, to_remove = [], []
    for row in db.kv_list(conn, user_id, NAMESPACE):
        key = row["key"]
        if key in _RESERVED_KEYS:
            continue
        meta = _get_meta(conn, user_id, key)
        if meta is None:
            continue
        if meta.get("state") == STATE_RETIRED:
            if meta.get("mirror") and not meta.get("copy_removed"):
                to_remove.append(key)
        elif meta.get("mirror") and meta.get("state") == STATE_PENDING:
            to_write.append(key)
    return sorted(to_write), sorted(to_remove)


def _adopt_legacy(conn, user_id: str, read) -> list[str]:
    """Take over ``vault`` rows the sync imported from ``generated/`` before ISSUE-686.

    Once, on the first complete read after the upgrade (:data:`_LEGACY_KEY`):
    a name rule applied every pass would also take over a user's own entry
    under a group like ``Generated Passwords`` the moment they deleted it.
    On that one pass a ``generated_`` owner the read no longer produces as an
    ordinary name is a former ``generated/`` entry; a user's own entry with
    such a name is still in ``read.services`` and stays theirs.
    """
    if db.kv_get(conn, user_id, NAMESPACE, _LEGACY_KEY) is not None:
        return []
    db.kv_set(conn, user_id, NAMESPACE, _LEGACY_KEY, "1")
    produced = set(read.services) | set(read.held)
    adopted: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT s.key, b.source FROM secrets s LEFT JOIN credential_bindings b "
        "ON b.user_id = s.user_id AND b.name = s.key WHERE s.user_id=? AND s.service=?",
        (user_id, _SERVICE),
    ).fetchall()
    for key, source in rows:
        if (source or "vault") != "vault" or key in produced:
            continue
        owner = _bindings.credential_name(conn, user_id, key)
        if owner.startswith(PREFIX) and owner not in produced:
            adopted.setdefault(owner, []).append(key)
    for owner, members in adopted.items():
        for member in members:
            binding = _bindings.get_binding(conn, user_id, member) or _binding("", owner=owner)
            _bindings.put_binding(conn, user_id, member, {
                **binding, "source": SOURCE, "credential": owner,
                "kind": binding.get("kind") or "value"})
        if _get_meta(conn, user_id, owner) is None:
            _put_meta(conn, user_id, owner, mirror=True, state=STATE_MIRRORED)
        logger.warning("generated: %s: adopted %s from the vault's generated group",
                       _label(user_id), _label(owner))
    return sorted(adopted)


def reconcile(conn, user_id: str, read) -> list[tuple[str, list[str]]]:
    """The sync's whole involvement with generated credentials. Caller commits.

    1. Adopt legacy ``vault`` rows from ``generated/`` (complete reads only).
    2. Import a ``generated/`` copy the table has no row for, as generated and
       mirrored. Never over an existing row of any source, and never one the
       user retired.
    3. Compare every mirrored credential with its copy and record divergence.

    Returns ``(name, divergence)`` for each credential that newly diverged, for
    the notice. Nothing here deletes or overwrites a row.
    """
    _begin(conn)
    complete = not read.truncated
    if complete:
        _adopt_legacy(conn, user_id, read)
    copies = getattr(read, "generated", {}) or {}
    for name, copy in sorted(copies.items()):
        meta = _get_meta(conn, user_id, name)
        if meta is not None and meta.get("state") == STATE_RETIRED:
            continue
        if _taken(conn, user_id, entry_names(name).values()) is not None:
            continue
        if not copy.get("password"):
            continue
        _write_rows(conn, user_id, name, copy)
        _put_meta(conn, user_id, name, mirror=True, state=STATE_MIRRORED)
        logger.info("generated: %s: imported %s from the vault's generated group",
                    _label(user_id), _label(name))

    newly: list[tuple[str, list[str]]] = []
    if not complete:
        return newly
    for name in generated_names(conn, user_id):
        meta = _get_meta(conn, user_id, name) or {"mirror": True, "state": STATE_MIRRORED}
        if not meta.get("mirror") or meta.get("state") in (STATE_OFF, STATE_RETIRED):
            continue
        found = divergence(stored_values(conn, user_id, name), copies.get(name))
        if meta.get("state") == STATE_PENDING:
            if not found:
                _put_meta(conn, user_id, name, mirror=True, state=STATE_MIRRORED)
            continue
        if not found:
            _put_meta(conn, user_id, name, mirror=True, state=STATE_MIRRORED)
            continue
        if meta.get("state") != STATE_DIVERGED or meta.get("divergence") != found:
            newly.append((name, found))
        _put_meta(conn, user_id, name, mirror=True, state=STATE_DIVERGED, divergence=found)
    return newly


_DIVERGENCE_TEXT = {
    DIVERGED_MISSING: "The KeePass copy of {name} is missing.",
    DIVERGED_MISSING_OTP: "The KeePass copy of {name} is missing its two-factor seed.",
    DIVERGED_CHANGED: "The KeePass copy of {name} was changed outside Istota.",
    DIVERGED_MISSING_RECOVERY: "The KeePass copy of {name} is missing its recovery codes.",
}


def raise_divergence_notice(conn, user_id: str, name: str, found: list[str]):
    """One actionable notice per credential and divergence. Returns the raise result."""
    from istota.notifications.resolvers import task_alert

    sentences = " ".join(_DIVERGENCE_TEXT[code].format(name=name) for code in found)
    return task_alert.write(
        conn, user_id,
        dedup_key=f"generated-diverged:{task_alert._slug(name, limit=64)}",
        title=f"KeePass copy of {name} differs",
        body=(f"{sentences} Istota still holds the credential and keeps using its own copy; "
              "nothing was changed or deleted. Re-mirror it from Settings, Credentials to "
              "write Istota's copy back to the file."),
        severity="warning", actionable=True,
        params={"status": "generated_diverged", "name": name},
    )


def retire(config, user_id: str, name: str, *, actor: str = "system") -> bool:
    """Delete a generated credential: every row, its grant, and its KeePass copy.

    A user action only: the settings page and ``istota secret retire``. No
    proxy request reaches it, so a task cannot retire a credential. ``name``
    may be the credential or one of its fields. The mirror copy is removed in
    the same call when the file can be written, and otherwise on a later sync
    cycle. The ``retired`` tombstone is permanent, mirrored or not, so neither
    a copy left in the file nor an older file saved over the removal is ever
    imported back; a later ``new`` of the same name replaces it. ``False``
    when nothing by that name is stored.
    """
    from istota.credentials import vault

    with db.get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM secrets WHERE user_id=? AND service=? AND key=?",
                        (user_id, _SERVICE, name)).fetchone() is None:
            return False
        name = _bindings.credential_name(conn, user_id, name)
        if not is_generated(conn, user_id, name):
            raise GeneratedCredentialError("not_generated", f"{name} was not generated by Istota")
        mirrored = mirror_state(conn, user_id, name)["mirror"]
        secrets_store.delete_secret(None, user_id, _SERVICE, name, all_fields=True, connection=conn, actor=actor)
        _put_meta(conn, user_id, name, mirror=mirrored, state=STATE_RETIRED,
                  copy_removed=not mirrored)
        db.close_signup_tag(conn, user_id, name[len(PREFIX):])
        from istota.credentials import audit
        audit.record(conn, user_id, action="retire", actor=actor, name=name)
    logger.info("generated: %s: retired %s", _label(user_id), _label(name))
    if mirrored:
        vault.unmirror_generated(config, user_id, name)
    return True


def _label(value: str) -> str:
    from istota.credentials.vault import _label as label
    return label(value)
