"""The mail accounts a user connected: the registry and its passwords.

One ``mail_accounts`` row per (user, account), holding metadata only. The
passwords are ``secrets`` rows under service ``mail_account``, keys
``{id}.imap_password`` and ``{id}.smtp_password``, keyed on the id so a slug
rename keeps them. ``mail_account`` is deliberately absent from
``CONNECTED_SERVICE_SCHEMA``, so ``istota secret`` and the generic secret routes
refuse it and this module is the only writer of those rows.

Every by-id function is scoped on ``user_id``, so an id from a URL or a channel
request cannot reach another user's row. Functions taking a connection expect
:func:`istota.db.get_db`'s (``sqlite3.Row`` rows) and write inside the caller's
transaction, opening one with ``BEGIN IMMEDIATE`` when none is open; the caller
commits.

No function here returns or logs a password except :func:`resolve_for_daemon`,
which is the one place the daemon decrypts them, and whose result keeps them
out of its ``repr``.
"""

from __future__ import annotations

import logging
import re
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from istota.config import PRIVATE_HOST_PATTERN
from istota.credentials import store
from istota.mail import drafts

if TYPE_CHECKING:
    from istota.config import MailAccountsConfig

logger = logging.getLogger(__name__)

SECRET_SERVICE = "mail_account"
PERMISSIONS = ("read", "read_send")
SECURITY_MODES = ("tls", "starttls")
PURPOSES = ("read", "send")
RESERVED_SLUGS = frozenset({"bot"})
DEFAULT_MAX_ACCOUNTS = 10

_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_MAX_HOST_CHARS = 253
_MAX_FOLDER_CHARS = 254
_MAX_TEXT_CHARS = 200

# What a caller may set, and the default a new row takes for each one absent.
# `imap_host` and `imap_username` have none: a row needs both.
_FIELD_DEFAULTS: dict[str, object] = {
    "label": "",
    "permission": "read",
    "imap_port": 993,
    "imap_security": "tls",
    "default_folder": "INBOX",
    "smtp_host": None,
    "smtp_port": 587,
    "smtp_security": "starttls",
    "smtp_same_login": True,
    "smtp_username": "",
    "from_address": "",
    "from_name": "",
    "sent_folder": "",
    "enabled": True,
}
FIELDS = ("slug", "imap_host", "imap_username", *_FIELD_DEFAULTS)


class MailAccountError(ValueError):
    """A refused write. ``reason`` is a closed set; ``field`` names the input.

    The message never carries a submitted value: a field can be a host or a
    username a caller would not want echoed into a log.
    """

    def __init__(self, reason: str, message: str, *, field: str | None = None):
        super().__init__(message)
        self.reason = reason
        self.field = field


@dataclass(frozen=True)
class MailAccount:
    id: int
    user_id: str
    slug: str
    label: str
    permission: str
    imap_host: str
    imap_port: int
    imap_security: str
    imap_username: str
    default_folder: str
    smtp_host: str | None
    smtp_port: int
    smtp_security: str
    smtp_same_login: bool
    smtp_username: str
    from_address: str
    from_name: str
    sent_folder: str
    enabled: bool
    created_at: str
    updated_at: str
    has_imap_password: bool
    has_smtp_password: bool
    pending_drafts: int

    @property
    def smtp_login(self) -> str:
        """The username SMTP signs in with."""
        return self.imap_username if self.smtp_same_login else self.smtp_username


@dataclass(frozen=True)
class ResolvedAccount:
    """An account and the passwords one purpose needs, decrypted in the daemon.

    ``smtp_password`` is set only for purpose ``send``. Neither password is in
    the ``repr``.
    """
    account: MailAccount
    purpose: str
    imap_password: str = field(repr=False)
    smtp_password: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Unresolved:
    """Why :func:`resolve_for_daemon` would not hand an account over."""
    reason: str


def secret_key(account_id: int, which: Literal["imap", "smtp"]) -> str:
    return f"{account_id}.{which}_password"


def _begin(conn: sqlite3.Connection) -> None:
    if not conn.in_transaction:
        conn.execute("BEGIN IMMEDIATE")


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def validate_slug(slug: object) -> str:
    if not isinstance(slug, str) or not _SLUG_RE.match(slug):
        raise MailAccountError(
            "invalid",
            "Use a slug of 1 to 32 lowercase letters, digits, '-' or '_', "
            "starting with a letter or digit",
            field="slug",
        )
    if slug in RESERVED_SLUGS:
        raise MailAccountError("invalid", f"The slug {slug!r} is reserved", field="slug")
    return slug


def _host(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise MailAccountError("invalid", "Use a host name or IP address", field=name)
    host = value.strip()
    if (not host or len(host) > _MAX_HOST_CHARS
            or not re.match(PRIVATE_HOST_PATTERN, host)):
        raise MailAccountError(
            "invalid",
            "Use a host name or IP address alone, with no scheme, port or path",
            field=name,
        )
    return host


def _port(value: object, name: str) -> int:
    if type(value) is not int or not 1 <= value <= 65535:
        raise MailAccountError("invalid", "Use a port between 1 and 65535", field=name)
    return value


def _choice(value: object, allowed: tuple[str, ...], name: str) -> str:
    if value not in allowed:
        raise MailAccountError(
            "invalid", f"Use one of: {', '.join(allowed)}", field=name,
        )
    return value  # type: ignore[return-value]


def _text(value: object, name: str, *, required: bool = False) -> str:
    if not isinstance(value, str) or len(value) > _MAX_TEXT_CHARS or _has_control(value):
        raise MailAccountError(
            "invalid", f"Use text of at most {_MAX_TEXT_CHARS} characters on one line",
            field=name,
        )
    if required and not value.strip():
        raise MailAccountError("invalid", "This field is required", field=name)
    return value.strip()


def _folder(value: object, name: str, *, allow_empty: bool) -> str:
    if (not isinstance(value, str) or len(value) > _MAX_FOLDER_CHARS
            or _has_control(value) or (not allow_empty and not value.strip())):
        raise MailAccountError(
            "invalid",
            f"Use a folder name under {_MAX_FOLDER_CHARS + 1} characters with no control characters",
            field=name,
        )
    return value.strip()


def _has_control(value: str) -> bool:
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def _from_address(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        return ""
    try:
        parsed = drafts.normalize_addresses([value], field="from_address")
    except drafts.DraftError:
        parsed = []
    if len(parsed) != 1:
        raise MailAccountError("invalid", "Use one email address", field="from_address")
    return parsed[0]


def _flag(value: object, name: str) -> bool:
    if type(value) is bool:
        return value
    if value in (0, 1):
        return bool(value)
    raise MailAccountError("invalid", "Use true or false", field=name)


def validate_fields(values: dict) -> dict:
    """Check and normalize a complete set of account fields, returning them.

    Hosts are checked for shape only. Whether one resolves, and to a public
    address, is decided at connect time, where DNS is actually resolved.
    """
    out = {
        "slug": validate_slug(values.get("slug")),
        "label": _text(values.get("label", ""), "label"),
        "permission": _choice(values.get("permission"), PERMISSIONS, "permission"),
        "imap_host": _host(values.get("imap_host"), "imap_host"),
        "imap_port": _port(values.get("imap_port"), "imap_port"),
        "imap_security": _choice(values.get("imap_security"), SECURITY_MODES, "imap_security"),
        "imap_username": _text(values.get("imap_username"), "imap_username", required=True),
        "default_folder": _folder(values.get("default_folder"), "default_folder", allow_empty=False),
        "smtp_host": None,
        "smtp_port": _port(values.get("smtp_port"), "smtp_port"),
        "smtp_security": _choice(values.get("smtp_security"), SECURITY_MODES, "smtp_security"),
        "smtp_same_login": _flag(values.get("smtp_same_login"), "smtp_same_login"),
        "smtp_username": _text(values.get("smtp_username", ""), "smtp_username"),
        "from_address": _from_address(values.get("from_address")),
        "from_name": _text(values.get("from_name", ""), "from_name"),
        "sent_folder": _folder(values.get("sent_folder", ""), "sent_folder", allow_empty=True),
        "enabled": _flag(values.get("enabled"), "enabled"),
    }
    smtp_host = values.get("smtp_host")
    if smtp_host is not None and not (isinstance(smtp_host, str) and not smtp_host.strip()):
        out["smtp_host"] = _host(smtp_host, "smtp_host")
    if out["smtp_host"] and not out["smtp_same_login"] and not out["smtp_username"]:
        raise MailAccountError(
            "invalid", "Give an SMTP username, or sign in with the IMAP login",
            field="smtp_username",
        )
    if out["permission"] == "read_send":
        if not out["smtp_host"]:
            raise MailAccountError(
                "invalid", "Sending needs an SMTP server", field="smtp_host",
            )
        if not out["from_address"]:
            raise MailAccountError(
                "invalid", "Sending needs a From address", field="from_address",
            )
    return out


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


_SELECT = """
    SELECT a.*,
        EXISTS (SELECT 1 FROM secrets s
                WHERE s.user_id = a.user_id AND s.service = 'mail_account'
                  AND s.key = a.id || '.imap_password') AS has_imap_password,
        EXISTS (SELECT 1 FROM secrets s
                WHERE s.user_id = a.user_id AND s.service = 'mail_account'
                  AND s.key = a.id || '.smtp_password') AS has_smtp_password,
        (SELECT count(*) FROM outbound_drafts d
         WHERE d.user_id = a.user_id AND d.account_id = a.id
           AND d.status = 'pending') AS pending_drafts
    FROM mail_accounts a
"""


def _account(row: sqlite3.Row) -> MailAccount:
    return MailAccount(
        id=row["id"],
        user_id=row["user_id"],
        slug=row["slug"],
        label=row["label"],
        permission=row["permission"],
        imap_host=row["imap_host"],
        imap_port=row["imap_port"],
        imap_security=row["imap_security"],
        imap_username=row["imap_username"],
        default_folder=row["default_folder"],
        smtp_host=row["smtp_host"],
        smtp_port=row["smtp_port"],
        smtp_security=row["smtp_security"],
        smtp_same_login=bool(row["smtp_same_login"]),
        smtp_username=row["smtp_username"],
        from_address=row["from_address"],
        from_name=row["from_name"],
        sent_folder=row["sent_folder"],
        enabled=bool(row["enabled"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        has_imap_password=bool(row["has_imap_password"]),
        has_smtp_password=bool(row["has_smtp_password"]),
        pending_drafts=row["pending_drafts"],
    )


def list_accounts(conn: sqlite3.Connection, user_id: str) -> list[MailAccount]:
    rows = conn.execute(
        _SELECT + " WHERE a.user_id = ? ORDER BY a.slug", (user_id,),
    ).fetchall()
    return [_account(row) for row in rows]


def get_account(conn: sqlite3.Connection, user_id: str, slug: str) -> MailAccount | None:
    row = conn.execute(
        _SELECT + " WHERE a.user_id = ? AND a.slug = ?", (user_id, slug),
    ).fetchone()
    return _account(row) if row else None


def get_account_by_id(
    conn: sqlite3.Connection, user_id: str, account_id: int,
) -> MailAccount | None:
    row = conn.execute(
        _SELECT + " WHERE a.user_id = ? AND a.id = ?", (user_id, account_id),
    ).fetchone()
    return _account(row) if row else None


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------


def _current_values(account: MailAccount) -> dict:
    return {name: getattr(account, name) for name in FIELDS}


def _check_names(fields: dict) -> None:
    unknown = sorted(set(fields) - set(FIELDS))
    if unknown:
        raise TypeError(f"unknown mail account field(s): {', '.join(unknown)}")


def _merged(base: dict, changes: dict) -> dict:
    merged = {**base, **changes}
    # Removing SMTP takes sending with it, unless the caller asked for
    # `read_send` in the same change, which validation then refuses.
    if "smtp_host" in changes and not changes["smtp_host"] and "permission" not in changes:
        merged["permission"] = "read"
    return merged


def _write(conn: sqlite3.Connection, user_id: str, account_id: int | None, values: dict) -> int:
    columns = [name for name in FIELDS]
    params = [values[name] for name in columns]
    params = [int(v) if type(v) is bool else v for v in params]
    try:
        if account_id is None:
            cursor = conn.execute(
                f"INSERT INTO mail_accounts (user_id, {', '.join(columns)}) "
                f"VALUES (?, {', '.join('?' for _ in columns)}) RETURNING id",
                (user_id, *params),
            )
            return cursor.fetchone()[0]
        conn.execute(
            f"UPDATE mail_accounts SET {', '.join(f'{c} = ?' for c in columns)}, "
            "updated_at = datetime('now') WHERE id = ? AND user_id = ?",
            (*params, account_id, user_id),
        )
        return account_id
    except sqlite3.IntegrityError as e:
        if "UNIQUE" in str(e):
            raise MailAccountError(
                "slug_taken", "You already have an account with that slug", field="slug",
            ) from None
        raise


def _after_write(conn: sqlite3.Connection, user_id: str, account: MailAccount) -> None:
    # SMTP removed: its password has nothing left to sign in to.
    if account.smtp_host is None and account.has_smtp_password:
        store.delete_secret(None, user_id, SECRET_SERVICE, secret_key(account.id, "smtp"),
                            connection=conn)


def ensure_account(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    slug: str,
    rename_to: str | None = None,
    max_accounts: int = DEFAULT_MAX_ACCOUNTS,
    **fields,
) -> tuple[MailAccount, str]:
    """Create or update the account ``slug``. Returns ``(account, state)``.

    ``state`` is ``"created"``, ``"updated"`` or ``"noop"``, the contract of
    ``istota user ensure`` and ``user_briefings.ensure_briefing``. Fields not
    passed keep their current value, or take the default on a new row.
    ``rename_to`` changes the slug of an existing account, keeping its id and
    its passwords. A new row past ``max_accounts`` is refused.
    """
    _check_names(fields)
    validate_slug(slug)
    _begin(conn)
    existing = get_account(conn, user_id, slug)
    if existing is None:
        if rename_to is not None:
            raise MailAccountError("not_found", "No account with that slug to rename", field="slug")
        count = conn.execute(
            "SELECT count(*) FROM mail_accounts WHERE user_id = ?", (user_id,),
        ).fetchone()[0]
        if count >= max_accounts:
            raise MailAccountError(
                "too_many_accounts",
                f"You can connect at most {max_accounts} mail accounts",
            )
        values = validate_fields({**_FIELD_DEFAULTS, **_merged({}, fields), "slug": slug})
        account_id = _write(conn, user_id, None, values)
        return get_account_by_id(conn, user_id, account_id), "created"  # type: ignore[return-value]

    changes = dict(fields)
    if rename_to is not None:
        changes["slug"] = rename_to
    values = validate_fields(_merged(_current_values(existing), changes))
    if values == _current_values(existing):
        return existing, "noop"
    _write(conn, user_id, existing.id, values)
    account = get_account_by_id(conn, user_id, existing.id)
    _after_write(conn, user_id, account)  # type: ignore[arg-type]
    return get_account_by_id(conn, user_id, existing.id), "updated"  # type: ignore[return-value]


def update_account(
    conn: sqlite3.Connection, user_id: str, account_id: int, **fields,
) -> MailAccount:
    """Change an account by id, slug included; the id and the passwords stay.

    Setting ``smtp_host`` to ``None`` removes sending: the permission drops to
    ``read`` and the SMTP password is deleted.
    """
    _check_names(fields)
    _begin(conn)
    existing = get_account_by_id(conn, user_id, account_id)
    if existing is None:
        raise MailAccountError("not_found", "No such mail account")
    values = validate_fields(_merged(_current_values(existing), fields))
    _write(conn, user_id, account_id, values)
    account = get_account_by_id(conn, user_id, account_id)
    _after_write(conn, user_id, account)  # type: ignore[arg-type]
    return get_account_by_id(conn, user_id, account_id)  # type: ignore[return-value]


def set_password(
    conn: sqlite3.Connection,
    user_id: str,
    account_id: int,
    which: Literal["imap", "smtp"],
    value: str,
) -> str:
    """Store one of an account's passwords. Returns ``created|updated|noop``.

    Raises ``credentials.store.SecretKeyMissingError`` without a master key.
    """
    if which not in ("imap", "smtp"):
        raise ValueError(f"unknown password {which!r}")
    if not isinstance(value, str) or not value:
        raise MailAccountError("invalid", "The password is empty", field=f"{which}_password")
    _begin(conn)
    account = get_account_by_id(conn, user_id, account_id)
    if account is None:
        raise MailAccountError("not_found", "No such mail account")
    if which == "smtp" and account.smtp_host is None:
        raise MailAccountError(
            "invalid", "This account has no SMTP server", field="smtp_password",
        )
    key = secret_key(account_id, which)
    current = store.get_secret(None, user_id, SECRET_SERVICE, key, connection=conn, touch=False)
    if current == value:
        return "noop"
    store.set_secret(None, user_id, SECRET_SERVICE, key, value, connection=conn)
    return "created" if current is None else "updated"


def clear_password(
    conn: sqlite3.Connection,
    user_id: str,
    account_id: int,
    which: Literal["imap", "smtp"],
) -> bool:
    """Delete one of an account's passwords. True if one was stored."""
    if which not in ("imap", "smtp"):
        raise ValueError(f"unknown password {which!r}")
    _begin(conn)
    if get_account_by_id(conn, user_id, account_id) is None:
        raise MailAccountError("not_found", "No such mail account")
    return store.delete_secret(
        None, user_id, SECRET_SERVICE, secret_key(account_id, which), connection=conn,
    )


def delete_account(
    conn: sqlite3.Connection, user_id: str, account_id: int, *, by: str = "system",
) -> bool:
    """Remove an account, its passwords and its pending drafts, in one transaction.

    Pending drafts go through ``drafts.discard``, which closes their
    notifications. A draft already ``sending`` is terminal and left alone.
    ``by`` names the surface the decision came from, for the closed
    notification rows. False when the user has no such account.
    """
    _begin(conn)
    if get_account_by_id(conn, user_id, account_id) is None:
        return False
    pending = conn.execute(
        "SELECT id FROM outbound_drafts WHERE user_id = ? AND account_id = ? AND status = ?",
        (user_id, account_id, drafts.STATUS_PENDING),
    ).fetchall()
    for row in pending:
        drafts.discard(conn, row["id"], by=by)
    for which in ("imap", "smtp"):
        store.delete_secret(
            None, user_id, SECRET_SERVICE, secret_key(account_id, which), connection=conn,
        )
    conn.execute(
        "DELETE FROM mail_accounts WHERE id = ? AND user_id = ?", (account_id, user_id),
    )
    return True


# ---------------------------------------------------------------------------
# The daemon's read, with passwords
# ---------------------------------------------------------------------------


def resolve_for_daemon(
    db_path: Path,
    user_id: str,
    account_id: int,
    *,
    purpose: str,
    settings: "MailAccountsConfig",
) -> ResolvedAccount | Unresolved:
    """The account and the passwords ``purpose`` needs, or why not.

    The one function the private channel, ``drafts.release``, the briefing
    source and the connection test call. Refuses as the channel does, except
    for the turn-author check, which only the channel can make:

    - ``mail_accounts_disabled``: ``[mail_accounts] enabled`` is off;
    - ``account_not_found``: no such account for this user, or it is
      disabled; one answer for both so ids cannot be probed;
    - ``permission_denied``: ``send`` on a ``read`` account, or with
      ``[mail_accounts] allow_send`` off;
    - ``password_missing``: a password the purpose needs is not stored, or
      cannot be decrypted (no master key, or a rotated one).

    Logs the account id, purpose and reason, never a host, username or
    password.
    """
    if purpose not in PURPOSES:
        raise ValueError(f"unknown purpose {purpose!r}")
    if not settings.enabled:
        return _refuse(user_id, account_id, purpose, "mail_accounts_disabled")

    from istota import db

    with db.get_db(db_path) as conn:
        account = get_account_by_id(conn, user_id, account_id)
    if account is None or not account.enabled:
        return _refuse(user_id, account_id, purpose, "account_not_found")
    if purpose == "send" and (
        account.permission != "read_send" or not settings.allow_send
        or account.smtp_host is None
    ):
        return _refuse(user_id, account_id, purpose, "permission_denied")

    imap_password = store.get_secret(
        db_path, user_id, SECRET_SERVICE, secret_key(account_id, "imap"),
    )
    if not isinstance(imap_password, str) or not imap_password:
        return _refuse(user_id, account_id, purpose, "password_missing")
    smtp_password = None
    if purpose == "send":
        if account.smtp_same_login:
            smtp_password = imap_password
        else:
            smtp_password = store.get_secret(
                db_path, user_id, SECRET_SERVICE, secret_key(account_id, "smtp"),
            )
            if not isinstance(smtp_password, str) or not smtp_password:
                return _refuse(user_id, account_id, purpose, "password_missing")
    return ResolvedAccount(
        account=account, purpose=purpose,
        imap_password=imap_password, smtp_password=smtp_password,
    )


def _refuse(user_id: str, account_id: int, purpose: str, reason: str) -> Unresolved:
    logger.info(
        "mail account not resolved user=%s account_id=%s purpose=%s reason=%s",
        user_id, account_id, purpose, reason,
    )
    return Unresolved(reason)
