"""Secret links: short-lived, single-use capability tokens for one action.

A link lets a user enter a secret or connect Google from a chat without a web
session: opening it is the authentication. This module is the token store and
the one issuing path, with no HTTP. It follows `webui/auth.py`'s token
discipline: a SHA-256 digest is the only thing stored, a token is spent by an
``UPDATE ... WHERE used_at IS NULL`` whose rowcount decides who won, and a peek
writes nothing, because link previews fetch the page.

``issue``, ``live_for_update``, ``spend`` and ``record_failure`` take the
caller's connection, and the caller owns the transaction. ``peek`` and
``prune`` open their own.

Issuing from a task (``turn_refusal``, ``origin_refusal``, ``issue_and_queue``)
is shared by the skill proxy's ``vault_link_request`` and the host-side
``google_workspace connect-link`` verb, so both links pass the same gates and
reach the user by the same private-room delivery.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from istota.db import get_db
from istota.webui.auth import _timestamp

INTENTS = ("credential", "google_connect")
OUTCOMES = ("stored", "cancelled", "failed", "superseded", "connected")
MAX_LIVE_PER_USER = 5
MAX_FAILURES = 5
PRUNE_AFTER_DAYS = 7


class SecretLinkError(ValueError):
    """A refusal with a fixed ``reason``: ``invalid`` or ``too_many_links``."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class SecretLink:
    id: int
    user_id: str
    intent: str
    params: dict
    task_id: int
    room_token: str
    expires_at: str


def link_host(url) -> str | None:
    """The one host a credential link's credential is bound to, or None for a URL that is not a site.

    The rule the credential store applies on create (`local._site_shaped` and
    `parse_binding`), so a link is never issued for a URL its form could not save.
    """
    from istota.credentials import local
    from istota.credentials.broker.bindings import parse_binding

    if not isinstance(url, str) or not url or url != url.strip() or not local._site_shaped(url):
        return None
    hosts = parse_binding(url, {}, [], source=local.SOURCE)["hosts"]
    return hosts[0] if len(hosts) == 1 else None


def expiry(stored: str) -> datetime:
    """A link's ``expires_at``, which is UTC, as an aware datetime."""
    return datetime.strptime(stored, "%Y-%m-%d %H:%M:%S.%f").replace(tzinfo=timezone.utc)


def local_clock(config, user_id: str, when: datetime) -> str:
    """``when`` as the user's wall-clock time, or UTC said as such when they chose no zone.

    The delivered message and the form both say the expiry this way, so the
    two never disagree.
    """
    from zoneinfo import ZoneInfo

    from istota.lib.timezones import is_valid_timezone

    name = config.resolve_user_timezone(user_id, default="")
    if not name or not is_valid_timezone(name):
        return when.strftime("%H:%M UTC")
    return when.astimezone(ZoneInfo(name)).strftime("%H:%M")


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def _plausible(token: object) -> bool:
    return isinstance(token, str) and 0 < len(token) <= 256 and token.isascii()


def _link(row: sqlite3.Row) -> SecretLink:
    return SecretLink(
        id=row["id"],
        user_id=row["user_id"],
        intent=row["intent"],
        params=json.loads(row["params"]),
        task_id=row["task_id"],
        room_token=row["room_token"],
        expires_at=row["expires_at"],
    )


def _live_row(conn: sqlite3.Connection, token: object) -> sqlite3.Row | None:
    if not _plausible(token):
        return None
    return conn.execute(
        "SELECT * FROM secret_links WHERE token_hash = ? AND used_at IS NULL AND expires_at > ?",
        (_digest(token), _timestamp()),
    ).fetchone()


def _check_request(user_id, intent, params, task_id, room_token, ttl_seconds) -> None:
    if not isinstance(user_id, str) or not user_id:
        raise SecretLinkError("invalid")
    if intent not in INTENTS or not isinstance(params, dict):
        raise SecretLinkError("invalid")
    if intent == "credential":
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise SecretLinkError("invalid")
    if type(task_id) is not int or not isinstance(room_token, str):
        raise SecretLinkError("invalid")
    if type(ttl_seconds) is not int or ttl_seconds <= 0:
        raise SecretLinkError("invalid")


def issue(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    intent: str,
    params: dict,
    task_id: int,
    room_token: str,
    ttl_seconds: int,
) -> tuple[str, SecretLink]:
    """Store a new link and return its raw token, which is never stored.

    A ``credential`` link supersedes the user's live link for the same name,
    and a ``google_connect`` link the user's live ``google_connect`` link.
    The cap of ``MAX_LIVE_PER_USER`` is counted without the link being
    superseded, and checked before anything is written.
    """
    _check_request(user_id, intent, params, task_id, room_token, ttl_seconds)
    try:
        params_json = json.dumps(params, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError):
        raise SecretLinkError("invalid") from None
    now = _timestamp()
    superseded: list[int] = []
    if intent == "credential":
        superseded = [r[0] for r in conn.execute(
            "SELECT id FROM secret_links WHERE user_id = ? AND intent = 'credential' "
            "AND json_extract(params, '$.name') = ? AND used_at IS NULL AND expires_at > ?",
            (user_id, params["name"], now),
        )]
    elif intent == "google_connect":
        superseded = [r[0] for r in conn.execute(
            "SELECT id FROM secret_links WHERE user_id = ? AND intent = 'google_connect' "
            "AND used_at IS NULL AND expires_at > ?",
            (user_id, now),
        )]
    live = conn.execute(
        "SELECT count(*) FROM secret_links WHERE user_id = ? AND used_at IS NULL AND expires_at > ?",
        (user_id, now),
    ).fetchone()[0]
    if live - len(superseded) >= MAX_LIVE_PER_USER:
        raise SecretLinkError("too_many_links")
    for link_id in superseded:
        spend(conn, link_id, "superseded")
    token = secrets.token_urlsafe(32)
    expires_at = _timestamp(ttl_seconds)
    cursor = conn.execute(
        "INSERT INTO secret_links (token_hash, user_id, intent, params, task_id, room_token, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (_digest(token), user_id, intent, params_json, task_id, room_token, expires_at),
    )
    link = SecretLink(
        id=cursor.lastrowid,
        user_id=user_id,
        intent=intent,
        params=json.loads(params_json),
        task_id=task_id,
        room_token=room_token,
        expires_at=expires_at,
    )
    return token, link


def peek(db_path: Path, token: object) -> SecretLink | None:
    """The live link for ``token``, or None. Writes nothing."""
    if not _plausible(token):
        return None
    with get_db(db_path) as conn:
        row = _live_row(conn, token)
        return _link(row) if row is not None else None


def is_live(conn: sqlite3.Connection, link_id: object, user_id: str) -> bool:
    """Whether ``user_id``'s link ``link_id`` is unspent and unexpired, by id.

    For the delivery of a link's message, which knows the id and never the token.
    """
    if type(link_id) is not int:
        return False
    return conn.execute(
        "SELECT 1 FROM secret_links WHERE id = ? AND user_id = ? AND used_at IS NULL AND expires_at > ?",
        (link_id, user_id, _timestamp()),
    ).fetchone() is not None


def spent_link(conn: sqlite3.Connection, link_id: object) -> tuple[SecretLink | None, str | None]:
    """``(link, outcome)`` for a spent link, by id; ``(None, None)`` for an unknown or unspent one.

    For the Google callback, which holds the id of the link its flow spent.
    """
    if type(link_id) is not int:
        return None, None
    row = conn.execute(
        "SELECT * FROM secret_links WHERE id = ? AND used_at IS NOT NULL", (link_id,),
    ).fetchone()
    return (_link(row), row["outcome"]) if row is not None else (None, None)


def live_for_update(conn: sqlite3.Connection, token: object) -> SecretLink | None:
    """The live link for ``token``, read inside the caller's ``BEGIN IMMEDIATE``."""
    row = _live_row(conn, token)
    return _link(row) if row is not None else None


def spend(conn: sqlite3.Connection, link_id: int, outcome: str) -> bool:
    """Mark the link used. True only when this call is the one that spent it."""
    if outcome not in OUTCOMES:
        raise ValueError("Unknown secret link outcome")
    cursor = conn.execute(
        "UPDATE secret_links SET used_at = ?, outcome = ? WHERE id = ? AND used_at IS NULL",
        (_timestamp(), outcome, link_id),
    )
    return cursor.rowcount == 1


def record_failure(conn: sqlite3.Connection, link_id: int) -> int:
    """Count one failed submit and return the new count.

    The ``MAX_FAILURES``th spends the link with ``outcome = 'failed'`` in the
    same statement. A link that is already spent counts nothing and returns 0.
    """
    row = conn.execute(
        "UPDATE secret_links SET failures = failures + 1, "
        "used_at = CASE WHEN failures + 1 >= ? THEN ? ELSE used_at END, "
        "outcome = CASE WHEN failures + 1 >= ? THEN 'failed' ELSE outcome END "
        "WHERE id = ? AND used_at IS NULL RETURNING failures",
        (MAX_FAILURES, _timestamp(), MAX_FAILURES, link_id),
    ).fetchone()
    return row[0] if row is not None else 0


def prune(db_path: Path, *, older_than_days: int = PRUNE_AFTER_DAYS) -> int:
    """Delete links that expired more than ``older_than_days`` ago.

    A spent link is spent before it expires, so its expiry bounds both.
    """
    if type(older_than_days) is not int or older_than_days < 0:
        raise ValueError("Invalid prune window")
    with get_db(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return conn.execute(
            "DELETE FROM secret_links WHERE expires_at <= ?",
            (_timestamp(-older_than_days * 86400),),
        ).rowcount


# Issuing from a task. The gates are numbered as in the secret-entry-links spec
# ("Gates at issue time"); each caller runs the ones its intent needs, in order.


class LinkRefused(Exception):
    """A link refused at issue time: a fixed ``reason`` and a message for the model."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(reason, message)
        self.reason = reason
        self.message = message


def turn_refusal(config, user_id: str, task_id: int) -> LinkRefused | None:
    """Gates 2 to 4: the user's own turn, not a scheduled run, not an incognito room.

    Only the user, answering a turn they wrote just now, can cause a link.
    A read error raises; the caller refuses on it.
    """
    from istota.credentials.broker.grants import _task_context
    from istota.mail.support import own_addresses
    from istota.sandbox.skill_proxy import _owners_own_turn

    with get_db(config.db_path) as conn:
        if not _owners_own_turn(conn, task_id, user_id, own_addresses=own_addresses(config, user_id)):
            return LinkRefused("not_owner_turn", "Only your own turn can ask for a link")
        task_row, scheduled = _task_context(conn, task_id, user_id)
    if scheduled:
        return LinkRefused("scheduled_turn", "A scheduled run cannot send a link")
    if task_row["incognito"]:
        return LinkRefused("incognito", "No link is sent from an incognito room")
    return None


def origin_refusal(config) -> LinkRefused | None:
    """Gate 6: the deployment has a web address of its own to link to."""
    from istota.webui.origin import external_origin

    try:
        external_origin(config)
    except ValueError:
        return LinkRefused("no_external_origin", "This deployment has no web address to link to")
    return None


def issue_and_queue(config, *, user_id: str, task_id: int, intent: str, params: dict,
                    compose, check=None) -> tuple[SecretLink, str]:
    """Gates 8 and 9, then the link and its message, in one ``BEGIN IMMEDIATE``.

    ``check(conn)`` runs first inside the transaction and refuses by raising
    ``LinkRefused``. ``compose(address, expires)`` writes the message from the
    link's address and its expiry in the user's clock; the address goes into
    the queued message and nowhere else. Returns ``(link, delivered_to)``. A
    refusal or any other error rolls back, leaving neither a link nor a message.
    """
    from istota import db
    from istota.relay.requests import RequestError
    from istota.rooms import private_replies
    from istota.webui.origin import external_origin

    try:
        origin_host, scheme = external_origin(config)
    except ValueError:
        raise LinkRefused("no_external_origin", "This deployment has no web address to link to") from None
    with get_db(config.db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if check is not None:
            check(conn)
        task = db.get_task(conn, task_id)
        about, dest = private_replies.secret_link_room(conn, config, task)
        if dest is None:
            raise LinkRefused("no_private_room", "You have no private chat to send the link to")
        try:
            token, link = issue(conn, user_id=user_id, intent=intent, params=params,
                                task_id=task_id, room_token=about,
                                ttl_seconds=config.secret_links.ttl_seconds)
        except SecretLinkError as exc:
            if exc.reason == "too_many_links":
                raise LinkRefused("too_many_links", "Too many unused links; use or let one expire") from None
            raise
        text = compose(f"{scheme}://{origin_host}/istota/secret?token={token}",
                       local_clock(config, user_id, expiry(link.expires_at)))
        try:
            queued = private_replies.enqueue_secret_link(
                conn, config, actor_user_id=user_id, task_id=task_id, link_id=link.id, text=text,
            )
        except RequestError as exc:
            raise LinkRefused(str(exc), "The link could not be queued") from None
    return link, queued["delivered_to"]


def iso_expiry(link: SecretLink) -> str:
    """The link's expiry as the ISO UTC string a reply to the model carries."""
    return expiry(link.expires_at).isoformat(timespec="seconds").replace("+00:00", "Z")
