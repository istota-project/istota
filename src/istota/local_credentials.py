"""Credentials added in Istota: the one writer of ``source="local"`` rows.

A local credential has the shape a KeePassXC entry produces in the
``vault_entries`` namespace — a value row, optional ``<name>_username`` and
``<name>_url`` rows, one binding per row carrying ``credential: <name>`` — so a
task cannot tell the two apart and nothing that reads the store changes. What
differs is the binding's ``source``, which is what keeps the KeePassXC sync's
sweep off these rows (``secrets_vault.apply_vault``).

Nothing here touches a file, pykeepass or a passphrase, which is why it sits
beside ``secrets_vault`` rather than inside it.

**No value leaves this module.** Refusal messages are fixed text plus the
credential *name*; return values carry names and the grant, never a value.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import db, secrets_store, secrets_vault
from .credential_broker import bindings as _bindings
from .credential_broker import grants as _grants

SOURCE = "local"

_SERVICE = secrets_vault.VAULT_ENTRY_SERVICE
_RESERVED_PREFIXES = ("forge.", "generated_")


@dataclass(frozen=True)
class LocalCredential:
    name: str
    value: str
    username: str = ""
    url: str = ""
    extra_hosts: str = ""
    headers: str = ""
    revealable: bool = False

    def __repr__(self) -> str:
        return f"LocalCredential(name={self.name!r}, value=<redacted>)"


class LocalCredentialError(ValueError):
    """A refusal about one input. ``field`` names it; the message holds no value."""

    def __init__(self, field: str, message: str):
        super().__init__(message)
        self.field = field


def _derived_names(name: str) -> tuple[str | None, str | None]:
    return (
        secrets_vault.slug_name((name, secrets_vault._USERNAME_SEGMENT)),
        secrets_vault.slug_name((name, secrets_vault._URL_SEGMENT)),
    )


def _check_name(name: object) -> tuple[str, str, str]:
    if not isinstance(name, str) or not name:
        raise LocalCredentialError("name", "a name is required")
    if name.startswith(_RESERVED_PREFIXES):
        raise LocalCredentialError(
            "name", "names starting with generated_ or forge. are reserved"
        )
    if secrets_vault.slug_name((name,)) != name:
        raise LocalCredentialError(
            "name",
            "use lowercase letters, digits and single underscores, starting with a letter",
        )
    username_name, url_name = _derived_names(name)
    if username_name is None or url_name is None:
        raise LocalCredentialError("name", "the name is too long")
    return name, username_name, url_name


def _check_text(field: str, value: object, *, required: bool) -> str:
    if not isinstance(value, str):
        raise LocalCredentialError(field, f"{field} must be text")
    if required and not value.strip():
        raise LocalCredentialError(field, f"{field} is required")
    if value != value.strip():
        raise LocalCredentialError(field, f"{field} cannot start or end with whitespace")
    try:
        size = len(value.encode("utf-8"))
    except UnicodeError:
        raise LocalCredentialError(field, f"{field} is not valid text") from None
    if size > secrets_vault.VAULT_MAX_VALUE_BYTES:
        raise LocalCredentialError(
            field, f"{field} is larger than {secrets_vault.VAULT_MAX_VALUE_BYTES} bytes"
        )
    return value


def _build_binding(url: object, extra_hosts: object, headers: object, revealable: object) -> dict:
    """The one host parser, ``parse_binding``, with its silent clears made refusals."""
    url = _check_text("url", url, required=False)
    extra_hosts = _check_text("extra_hosts", extra_hosts, required=False)
    headers = _check_text("headers", headers, required=False)
    if type(revealable) is not bool:
        raise LocalCredentialError("revealable", "revealable must be true or false")

    if url and not _bindings.parse_binding(url, {}, [], source=SOURCE)["hosts"]:
        raise LocalCredentialError("url", "not a hostname or https URL")
    if extra_hosts and not _bindings.parse_binding(
        "", {"istota_hosts": extra_hosts}, [], source=SOURCE
    )["hosts"]:
        raise LocalCredentialError("extra_hosts", "not a list of hostnames")
    binding = _bindings.parse_binding(
        url,
        {"istota_hosts": extra_hosts, "istota_headers": headers or None},
        ["istota:reveal"] if revealable else [],
        source=SOURCE,
    )
    if headers:
        asked = {h.strip().lower() for h in headers.split(",") if h.strip()}
        if not asked or asked != set(binding["headers"]):
            raise LocalCredentialError("headers", "not a list of header names")
    return binding


def _taken_names(conn, user_id: str) -> set[str]:
    taken = {row[0] for row in conn.execute(
        "SELECT key FROM secrets WHERE user_id=? AND service=?", (user_id, _SERVICE),
    )}
    taken |= {row[0] for row in conn.execute(
        "SELECT name FROM credential_bindings WHERE user_id=?", (user_id,),
    )}
    taken |= set(_bindings.credential_groups(conn, user_id))
    return taken


def _begin(conn) -> None:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


def is_local(conn, user_id: str, name: str) -> bool:
    """Whether ``name`` is a stored credential added in Istota, by its own name.

    A field row (``<name>_url``) is not a credential of its own, so it answers
    False even though its binding says ``local``.
    """
    if _bindings.credential_name(conn, user_id, name) != name:
        return False
    row = conn.execute(
        "SELECT b.source FROM secrets s JOIN credential_bindings b "
        "ON b.user_id = s.user_id AND b.name = s.key "
        "WHERE s.user_id=? AND s.service=? AND s.key=?",
        (user_id, _SERVICE, name),
    ).fetchone()
    return row is not None and row[0] == SOURCE


def _write_fields(conn, user_id, name, username_name, url_name, *, value, username, url, binding):
    """Write or remove each field row with the shared binding. ``value=None`` keeps it."""
    owned = {**binding, "credential": name}
    if value is not None:
        secrets_store.set_secret(None, user_id, _SERVICE, name, value,
                                 binding=owned, connection=conn)
    else:
        _bindings.put_binding(conn, user_id, name, owned)
    for field_name, field_value in ((username_name, username), (url_name, url)):
        if field_value:
            secrets_store.set_secret(None, user_id, _SERVICE, field_name, field_value,
                                     binding=owned, connection=conn)
        else:
            secrets_store.delete_secret(None, user_id, _SERVICE, field_name, connection=conn)


def create(conn, user_id: str, cred: LocalCredential, *, access: dict | None = None) -> dict:
    """Store a new local credential and, with ``access``, its grant, in one transaction.

    ``conn`` is the caller's (``db.get_db``), and is put inside ``BEGIN
    IMMEDIATE`` here if the caller has not already done so; the caller's exit
    commits or rolls back the whole of it.
    """
    _begin(conn)
    name, username_name, url_name = _check_name(cred.name)
    taken = _taken_names(conn, user_id)
    for candidate in (name, username_name, url_name):
        if candidate in taken:
            if candidate == name:
                raise LocalCredentialError("name", f"a credential named {name} already exists")
            raise LocalCredentialError(
                "name", f"{name} would clash with the existing credential {candidate}"
            )
    value = _check_text("value", cred.value, required=True)
    username = _check_text("username", cred.username, required=False)
    binding = _build_binding(cred.url, cred.extra_hosts, cred.headers, cred.revealable)
    if access is not None:
        if not isinstance(access, dict):
            raise LocalCredentialError("access", "access must be an object")
        if not binding["hosts"]:
            raise LocalCredentialError(
                "access", "a credential needs a site before it can be granted"
            )

    _write_fields(conn, user_id, name, username_name, url_name,
                  value=value, username=username, url=cred.url, binding=binding)
    grant = None
    if access is not None:
        try:
            grant = _grants.put_grant(conn, user_id, name, **access)
        except (TypeError, ValueError):
            raise LocalCredentialError("access", "invalid access settings") from None
    return {
        "name": name,
        "username_name": username_name if username else None,
        "url_name": url_name if cred.url else None,
        "grant": grant,
    }


def update(conn, user_id: str, name: str, *, value: str | None, username: str, url: str,
           extra_hosts: str, headers: str, revealable: bool) -> dict:
    """Replace a local credential's metadata, and its value unless ``value`` is ``None``.

    An empty username or URL deletes that row. Every field's binding is
    rewritten from the new inputs; a host change takes effect on the next
    request, as a KeePassXC edit does.
    """
    _begin(conn)
    if not isinstance(name, str) or not is_local(conn, user_id, name):
        raise LocalCredentialError(
            "name",
            "no credential added in Istota has this name; one from KeePassXC "
            "or the deployment is edited there",
        )
    _, username_name, url_name = _check_name(name)
    if value is not None:
        value = _check_text("value", value, required=True)
    username = _check_text("username", username, required=False)
    binding = _build_binding(url, extra_hosts, headers, revealable)
    if not binding["hosts"] and _grants.get_grant(conn, user_id, name) is not None:
        raise LocalCredentialError(
            "url", "this credential has access settings; remove its access first, or keep a site"
        )

    _write_fields(conn, user_id, name, username_name, url_name,
                  value=value, username=username, url=url, binding=binding)
    return {
        "name": name,
        "username_name": username_name if username else None,
        "url_name": url_name if url else None,
        "grant": _grants.get_grant(conn, user_id, name),
    }


def delete(db_path: Path, user_id: str, name: str) -> bool:
    """Delete a local credential: every field row, its bindings and its grant.

    A real deletion; nothing brings it back. Refuses a name some other source
    owns. ``False`` when nothing by that name is stored.
    """
    with db.get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        exists = conn.execute(
            "SELECT 1 FROM secrets WHERE user_id=? AND service=? AND key=?",
            (user_id, _SERVICE, name),
        ).fetchone()
        if exists is None:
            return False
        if not is_local(conn, user_id, name):
            raise LocalCredentialError(
                "name", "this credential was not added in Istota"
            )
        deleted = secrets_store.delete_secret(
            db_path, user_id, _SERVICE, name, all_fields=True, connection=conn,
        )
    return deleted
