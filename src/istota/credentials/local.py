"""User-owned credentials, created in Settings or imported from a file.

Writes return names and grants, never secret values. Imported credentials use
this same source and edit path.
"""

from __future__ import annotations
from istota.credentials import kdbx_import as credential_read

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from istota import db
from istota.credentials import store as secrets_store
from istota.credentials import names as secrets_vault
from istota.credentials.broker import bindings as _bindings
from istota.credentials.broker import grants as _grants
from istota.lib.totp import TotpError, parse_user_input, to_uri

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
    otp: str = ""

    def __repr__(self) -> str:
        return f"LocalCredential(name={self.name!r}, value=<redacted>)"


class LocalCredentialError(ValueError):
    """A refusal about one input. ``field`` names it; the message holds no value."""

    def __init__(self, field: str, message: str, *, code: str | None = None):
        super().__init__(message)
        self.field = field
        self.code = code


def derived_names(name: str) -> tuple[str | None, str | None, str | None]:
    return (
        secrets_vault.slug_name((name, credential_read._USERNAME_SEGMENT)),
        secrets_vault.slug_name((name, credential_read._URL_SEGMENT)),
        secrets_vault.slug_name((name, "totp")),
    )


def _check_name(name: object) -> tuple[str, str, str, str]:
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
    username_name, url_name, otp_name = derived_names(name)
    if username_name is None or url_name is None or otp_name is None:
        raise LocalCredentialError("name", "the name is too long")
    return name, username_name, url_name, otp_name


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


def _check_otp(name: str, otp: str | None) -> str | None:
    if otp is None or otp == "":
        return otp
    try:
        return to_uri(parse_user_input(otp), label=name)
    except TotpError as exc:
        raise LocalCredentialError(
            "otp", f"invalid two-factor secret ({exc.code})", code="invalid_otp",
        ) from None


def _site_shaped(url: str) -> bool:
    """Whether ``url`` is a site and nothing more: ``[scheme://]host[:port][/]``.

    The stored URL is returned by the list payload, so a path, query string,
    fragment or userinfo is refused rather than stored: a token pasted into
    one would otherwise come back on every read.
    """
    if any(mark in url for mark in ("?", "#", "@")):
        return False
    parts = urlsplit(url if "://" in url else "//" + url)
    return parts.path in ("", "/")


def _build_binding(url: object, extra_hosts: object, headers: object, revealable: object) -> dict:
    """The one host parser, ``parse_binding``, with its silent clears made refusals."""
    url = _check_text("url", url, required=False)
    extra_hosts = _check_text("extra_hosts", extra_hosts, required=False)
    headers = _check_text("headers", headers, required=False)
    if type(revealable) is not bool:
        raise LocalCredentialError("revealable", "revealable must be true or false")

    if url and (not _site_shaped(url)
                or not _bindings.parse_binding(url, {}, [], source=SOURCE)["hosts"]):
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


def _foreign_field(conn, user_id: str, owner: str, field_name: str) -> bool:
    """Whether ``field_name`` is stored and belongs to something other than ``owner``.

    A derived name can become taken after ``owner`` was created: a KeePassXC
    entry titled ``foo url`` is a credential named ``foo_url`` of its own.
    Writing or deleting that row from ``owner``'s edit would take it over.
    """
    present = conn.execute(
        "SELECT 1 FROM secrets WHERE user_id=? AND service=? AND key=? "
        "UNION SELECT 1 FROM credential_bindings WHERE user_id=? AND name=?",
        (user_id, _SERVICE, field_name, user_id, field_name),
    ).fetchone()
    if present is None:
        return False
    binding = _bindings.get_binding(conn, user_id, field_name)
    return (_bindings.credential_name(conn, user_id, field_name) != owner
            or binding is None or _bindings.effective_source(binding["source"]) != SOURCE)


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
    groups = _bindings.credential_groups(conn, user_id)
    members = groups.get(name, [])
    return bool(members) and all(_owned_field(conn, user_id, name, member) for member in members)


def stored_fields(conn, user_id: str, name: str) -> dict:
    """What the edit form may know about a local credential: its URL, its extra
    hosts, and whether a username is set.

    The URL is a binding input and not secret; the username and the value are
    never read back. The extra hosts are the bound hosts minus the site's own,
    worked out here with the same parser that bound them: the client cannot
    tell `api.example.com:443` from `api.example.com`, and a site host
    misread as an extra host would stay bound after the site changed.
    """
    username_name, url_name, otp_name = derived_names(name)
    url = ""
    if url_name and _owned_field(conn, user_id, name, url_name):
        stored = secrets_store.get_secret(None, user_id, _SERVICE, url_name, connection=conn)
        url = stored if isinstance(stored, str) else ""
    binding = _bindings.get_entry_binding(conn, user_id, name) or {"hosts": []}
    site_hosts = set(_bindings.parse_binding(url, {}, [], source=SOURCE)["hosts"]) if url else set()
    extra_hosts = [host for host in binding["hosts"] if host not in site_hosts]
    return {
        "url": url,
        "extra_hosts": ", ".join(extra_hosts),
        "username_set": bool(username_name) and _owned_field(conn, user_id, name, username_name),
        "otp_set": bool(otp_name) and _owned_field(conn, user_id, name, otp_name)
        and _bindings.is_otp_seed(conn, user_id, otp_name),
    }


def _owned_field(conn, user_id: str, owner: str, field_name: str) -> bool:
    """Whether ``field_name`` is stored and is ``owner``'s own local field row."""
    stored = conn.execute(
        "SELECT 1 FROM secrets WHERE user_id=? AND service=? AND key=?",
        (user_id, _SERVICE, field_name),
    ).fetchone()
    return stored is not None and not _foreign_field(conn, user_id, owner, field_name)


def _write_fields(conn, user_id, name, username_name, url_name, otp_name, *,
                  value, username, url, otp, binding, actor):
    """Write or remove fields; an omitted value keeps its row and kind."""
    fields = {name: value, username_name: username, url_name: url, otp_name: otp}
    for member in _bindings.credential_groups(conn, user_id).get(name, []):
        fields.setdefault(member, None)
    for field_name, field_value in fields.items():
        if field_value is None and not _owned_field(conn, user_id, name, field_name):
            continue
        previous = _bindings.get_binding(conn, user_id, field_name)
        kind = previous["kind"] if previous else "value"
        if field_name == otp_name and field_value:
            kind = "totp"
        owned = {**binding, "credential": name, "kind": kind}
        if field_value is None:
            _bindings.put_binding(conn, user_id, field_name, owned)
        elif field_value:
            secrets_store.set_secret(None, user_id, _SERVICE, field_name, field_value,
                                     binding=owned, connection=conn, actor=actor)
        else:
            secrets_store.delete_secret(None, user_id, _SERVICE, field_name, connection=conn, actor=actor)


def create(conn, user_id: str, cred: LocalCredential, *, access: dict | None = None, actor: str = "system") -> dict:
    """Store a new local credential and, with ``access``, its grant, in one transaction.

    ``conn`` is the caller's (``db.get_db``), and is put inside ``BEGIN
    IMMEDIATE`` here if the caller has not already done so; the caller's exit
    commits or rolls back the whole of it.
    """
    _begin(conn)
    name, username_name, url_name, otp_name = _check_name(cred.name)
    taken = _taken_names(conn, user_id)
    for candidate in (name, username_name, url_name, otp_name):
        if candidate in taken:
            if candidate == name:
                raise LocalCredentialError("name", f"a credential named {name} already exists")
            raise LocalCredentialError(
                "name", f"{name} would clash with the existing credential {candidate}"
            )
    for suffix in ("_" + credential_read._USERNAME_SEGMENT, "_" + credential_read._URL_SEGMENT, "_totp"):
        owner = name[: -len(suffix)] if name.endswith(suffix) else ""
        if owner and owner in taken:
            raise LocalCredentialError(
                "name", f"{name} is a field name of the existing credential {owner}"
            )
    value = _check_text("value", cred.value, required=True)
    username = _check_text("username", cred.username, required=False)
    binding = _build_binding(cred.url, cred.extra_hosts, cred.headers, cred.revealable)
    otp = _check_otp(name, cred.otp)
    if otp and not binding["hosts"]:
        raise LocalCredentialError("otp", "two-factor needs a site", code="otp_needs_site")
    if access is not None:
        if not isinstance(access, dict):
            raise LocalCredentialError("access", "access must be an object")
        if not binding["hosts"]:
            raise LocalCredentialError(
                "access", "a credential needs a site before it can be granted"
            )

    _write_fields(conn, user_id, name, username_name, url_name, otp_name,
                  value=value, username=username, url=cred.url, otp=otp, binding=binding, actor=actor)
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


def update(conn, user_id: str, name: str, *, value: str | None, username: str | None, url: str,
           extra_hosts: str, headers: str, revealable: bool, otp: str | None = None, actor: str = "system") -> dict:
    """Replace a local credential's metadata, and its value unless ``value`` is ``None``.

    ``username=None`` keeps the stored username, which the edit form needs
    because it can never read it back. An empty username or URL deletes that
    row. Every field's binding is
    rewritten from the new inputs; a host change takes effect on the next
    request.
    """
    _begin(conn)
    if not isinstance(name, str) or not is_local(conn, user_id, name):
        raise LocalCredentialError(
            "name",
            "no editable credential has this name",
        )
    _, username_name, url_name, otp_name = _check_name(name)
    if value is not None:
        value = _check_text("value", value, required=True)
    if username is not None:
        username = _check_text("username", username, required=False)
    binding = _build_binding(url, extra_hosts, headers, revealable)
    otp = _check_otp(name, otp)
    has_otp = bool(otp) if otp is not None else (
        _owned_field(conn, user_id, name, otp_name)
        and _bindings.is_otp_seed(conn, user_id, otp_name)
    )
    if has_otp and not binding["hosts"]:
        raise LocalCredentialError("otp", "two-factor needs a site", code="otp_needs_site")
    for field, field_name, field_value in (("username", username_name, username),
                                           ("url", url_name, url), ("otp", otp_name, otp)):
        if field_value is not None and _foreign_field(conn, user_id, name, field_name):
            raise LocalCredentialError(
                field, f"{name} would clash with the existing credential {field_name}"
            )
    if not binding["hosts"] and _grants.get_grant(conn, user_id, name) is not None:
        raise LocalCredentialError(
            "url", "this credential has access settings; remove its access first, or keep a site"
        )

    _write_fields(conn, user_id, name, username_name, url_name, otp_name,
                  value=value, username=username, url=url, otp=otp, binding=binding, actor=actor)
    has_username = (bool(username) if username is not None
                    else _owned_field(conn, user_id, name, username_name))
    return {
        "name": name,
        "username_name": username_name if has_username else None,
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
        groups = _bindings.credential_groups(conn, user_id)
        if name not in groups and not any(name in members for members in groups.values()):
            return False
        if not is_local(conn, user_id, name):
            raise LocalCredentialError(
                "name", "this credential was not added in Istota"
            )
        deleted = secrets_store.delete_secret(
            db_path, user_id, _SERVICE, name, all_fields=True, connection=conn,
        )
    return deleted
