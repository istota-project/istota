"""User profile store (Phase 6 of the Docker onboarding spec).

Per-user profile fields (display_name, email_addresses, timezone, log_channel,
alerts_channel, worker overrides, disabled_skills, trusted_email_senders)
live in the ``user_profiles`` table.

Resolution order at config-load time:
    1. ``user_profiles`` table     (web-UI / Docker-seeded / ``istota user ensure``)
    2. ``[users.X]`` in main config (single-file ansible / docker entrypoint)

A user is "first seen" by the system via:
- Ansible runs ``istota user ensure`` against the DB.
- Docker entrypoint emits a ``[users.X]`` block; the scheduler startup
  auto-seeds an empty profile row from the bare key.
- Web login: the OAuth2 callback calls ``ensure_profile`` to create the
  row from the NC username + display_name.

The DB row, when present, wins.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

from istota.webui import avatars
from istota.lib import sqlite_util

logger = logging.getLogger(__name__)

# How an external-origin turn's body renders in web chat. Lives here rather than
# inline at each consumer because two surfaces validate against it — the web
# profile PUT (`web_app._PROFILE_EDITABLE_FIELDS`) and `istota user ensure`,
# which validates twice, at the parser and in the handler — and a hand-copied
# list is the one that drifts. The outbound counterpart is
# `outbound_policy.VALID_POLICIES`, which stays with the predicate that reads it.
#
# The column *default* is a separate concern and stays a literal at its read
# sites: it names one member rather than the set, and coupling it here would
# make reordering the tuple change what an unset row means.
EXTERNAL_TURN_DISPLAY_VALUES = ("full", "collapsed", "hidden")

# Where a relay question from another user reaches this one. '' is no
# preference: the asker's `--via`, else the default room. The non-empty members
# are also the destination kinds `relay_destinations` resolves, which reads
# them from here.
RELAY_DELIVERY_VALUES = ("", "room", "whatsapp", "sms")


@dataclass
class UserProfile:
    """Profile fields that live in the ``user_profiles`` table.

    Mirrors the subset of ``UserConfig`` that Phase 6 moves out of TOML.
    Briefings and resources stay in TOML and are not represented here.
    """

    user_id: str
    display_name: str = ""
    email_addresses: list[str] = field(default_factory=list)
    sms_phone_number: str = ""
    timezone: str = "UTC"
    log_channel: str = ""
    alerts_channel: str = ""
    max_foreground_workers: int = 0
    max_background_workers: int = 0
    disabled_skills: list[str] = field(default_factory=list)
    trusted_email_senders: list[str] = field(default_factory=list)
    quiet_email_senders: list[str] = field(default_factory=list)
    disabled_modules: list[str] = field(default_factory=list)
    # Purpose-keyed delivery routing: {purpose -> output_target descriptor}.
    routing: dict[str, str] = field(default_factory=dict)
    # Default delivery descriptor when no per-purpose route applies.
    default_destination: str = "talk"
    # The room a destination naming no room of its own lands in — a canonical
    # room token, '' for unset. Surface-agnostic: the room registry is unified,
    # so one token answers for web and for Talk, and a room promoted to Talk
    # does not need naming twice. Read only through
    # `db.configured_delivery_room`; see ISSUE-477.
    default_room: str = ""
    # Outbound email approval: '' (unset — follow the operator floor) | off |
    # untrusted | all. Unset is a real value here, not a missing one.
    outbound_approval: str = ""
    # External-origin turn body in web chat: full | collapsed | hidden.
    external_turn_display: str = "collapsed"
    # Relay questions from other users: '' | room | whatsapp | sms. Read by
    # `relay_destinations.resolve_destination`, which treats an unknown value
    # as no preference.
    relay_delivery: str = ""
    # Seed the shared [[default_briefings]] set into this user (default on).
    default_briefings: bool = True
    # Deliver briefing email as multipart/alternative (HTML + plain) — default on.
    briefing_email_html: bool = True
    # Follow the user's GPS timezone on travel (ISSUE-096). Default OFF: this
    # rewrites a setting the user chose, so it is opted into, not inferred.
    timezone_follow_location: bool = False
    # Per-user Google Workspace scope selection: {service -> off|readonly|full},
    # clamped at connect time to the operator's [google_workspace] scopes
    # ceiling. Empty is "unset" and resolves to the whole ceiling, which is
    # what every user had before the picker existed. See istota.credentials.google_scopes.
    google_scopes: dict[str, str] = field(default_factory=dict)


_PROFILE_COLUMNS = (
    "display_name", "email_addresses", "sms_phone_number", "timezone",
    "log_channel", "alerts_channel",
    "max_foreground_workers", "max_background_workers",
    "disabled_skills", "trusted_email_senders", "quiet_email_senders",
    "disabled_modules",
    "routing", "default_destination", "default_room",
    "outbound_approval", "external_turn_display", "relay_delivery",
    "default_briefings", "briefing_email_html",
    "timezone_follow_location",
    "google_scopes",
)

# Columns whose value is a JSON-encoded dict (vs the JSON-list columns).
_DICT_COLUMNS = frozenset({"routing", "google_scopes"})
_LIST_COLUMNS = frozenset({
    "email_addresses", "disabled_skills", "trusted_email_senders",
    "quiet_email_senders", "disabled_modules",
})
# Columns stored as INTEGER 0/1 booleans, mapped to the default a missing
# value means. Per-column rather than one shared default: the briefing pair are
# opt-*outs* (absent = on) while timezone-following is an opt-*in* (absent =
# off), and a single default would silently switch one of them on.
_BOOL_COLUMN_DEFAULTS = {
    "default_briefings": True,
    "briefing_email_html": True,
    "timezone_follow_location": False,
}
_BOOL_COLUMNS = frozenset(_BOOL_COLUMN_DEFAULTS)
_E164_PATTERN = re.compile(r"^\+[1-9][0-9]{7,14}$")

# Who may write which profile column: the one list the self PUT
# (`web_app._PROFILE_EDITABLE_FIELDS` is derived from it), the admin PATCH and
# `istota user ensure` check against, so the three cannot drift apart. Worker
# caps are a resource limit and are admin-only: a user raising their own cap is
# the wrong direction. The SMS number is an operator-bound identity. The
# WhatsApp number and the login email are not profile columns and are handled
# beside these tuples, not in them.
SELF_EDITABLE_FIELDS = (
    "display_name", "timezone", "log_channel", "alerts_channel",
    "email_addresses", "trusted_email_senders", "quiet_email_senders",
    "disabled_skills", "disabled_modules",
    "default_destination", "default_room", "routing",
    "briefing_email_html", "timezone_follow_location",
    "external_turn_display", "relay_delivery",
)
ADMIN_EDITABLE_FIELDS = (
    "display_name", "timezone", "email_addresses",
    "trusted_email_senders", "quiet_email_senders", "outbound_approval",
    "disabled_skills", "disabled_modules", "default_briefings",
    "max_foreground_workers", "max_background_workers", "sms_phone_number",
)


def is_e164(value: object) -> bool:
    """Whether ``value`` is an exact E.164 number.

    The one spelling of this rule. It was written three times — here, in
    ``config._validate_sms`` and in the SMS inbound path — which is three places
    to keep in step for a predicate that decides which user an authenticated
    message may act as.
    """
    return bool(_E164_PATTERN.fullmatch(str(value) if value is not None else ""))


def normalize_phone_number(
    value: object, *, label: str, allow_empty: bool = False
) -> str:
    """Validate an exact E.164 number without guessing or rewriting it.

    `label` is the only thing the two bindings differ in. An operator told
    "SMS phone number must be exact E.164" after typing `--whatsapp-number`
    has to work out which of the two the command refused.
    """
    number = str(value) if value is not None else ""
    if allow_empty and number == "":
        return ""
    if not is_e164(number):
        raise ValueError(
            f"{label} must be exact E.164: '+' followed by 8 to 15 digits, "
            "with a non-zero country code"
        )
    return number


def normalize_sms_phone_number(value: object, *, allow_empty: bool = False) -> str:
    return normalize_phone_number(
        value, label="SMS phone number", allow_empty=allow_empty,
    )


def normalize_whatsapp_phone_number(value: object, *, allow_empty: bool = False) -> str:
    """The bootstrap number for a WhatsApp binding.

    Meta's `wa_id` arrives without the leading `+`, so the inbound path
    prepends one before asking — it does not clean punctuation or guess a
    country code, and neither does this.
    """
    return normalize_phone_number(
        value, label="WhatsApp phone number", allow_empty=allow_empty,
    )


def mask_phone_number(number: str) -> str:
    """Mask an E.164 number while retaining its country marker and suffix."""
    if not number:
        return ""
    visible = number[-4:]
    return "+" + "*" * max(0, len(number) - 5) + visible


def mask_sms_phone_number(number: str) -> str:
    """:func:`mask_phone_number` under the name its callers already use."""
    return mask_phone_number(number)


def short_fingerprint(domain: str, value: str, *, length: int = 16) -> str:
    """A stable, truncated, one-way fingerprint of a private identifier.

    For a value that is not a credential but still identifies a person — a
    phone number, a WhatsApp BSUID, a Meta message id — so a log line or an
    operator's terminal can match two mentions of the same thing without
    carrying the thing itself. `domain` separates the namespaces: two
    surfaces fingerprinting the same phone number must not produce the same
    string, or one surface's log becomes a lookup table for the other's.
    """
    if not value:
        return ""
    return hashlib.sha256(f"{domain}\0{value}".encode()).hexdigest()[:length]


def mask_whatsapp_identifier(value: str) -> str:
    """The fingerprint every general surface shows for a BSUID or send id."""
    return short_fingerprint("istota-whatsapp-id-v1", value, length=12)


def _raise_phone_conflict(exc: sqlite3.IntegrityError) -> None:
    if "sms_phone_number" in str(exc):
        raise ValueError("SMS phone number is already assigned to another user") from None
    raise exc


def _rows_or_none(
    conn: sqlite3.Connection, sql: str, params: tuple = (),
) -> list | None:
    """``fetchall``, or None when the table is not there yet.

    `istota user ensure` runs against databases a deploy has not migrated, and
    a table that does not exist holds no identity.
    """
    try:
        return conn.execute(sql, params).fetchall()
    except sqlite3.OperationalError as exc:
        if "no such table" not in str(exc):
            raise
        return None


def _email_holdings(conn: sqlite3.Connection) -> dict[str, dict[str, set[str]]]:
    """Case-folded address -> holder -> how they hold it (``address``/``login``)."""
    from istota.webui.auth import normalize_email

    holdings: dict[str, dict[str, set[str]]] = {}
    for row in _rows_or_none(
        conn, "SELECT user_id, email_addresses FROM user_profiles",
    ) or []:
        for address in _parse_json_list(row[1]):
            key = normalize_email(address)
            if key:
                holdings.setdefault(key, {}).setdefault(row[0], set()).add("address")
    for row in _rows_or_none(
        conn, "SELECT user_id, email FROM web_auth_identities",
    ) or []:
        key = normalize_email(row[1] or "")
        if key:
            holdings.setdefault(key, {}).setdefault(row[0], set()).add("login")
    return holdings


def email_address_holders(conn: sqlite3.Connection) -> dict[str, set[str]]:
    """Every case-folded address mapped to the users who hold it.

    Both kinds of holder count. An inbound-routing address in
    ``email_addresses`` routes mail; a login email in ``web_auth_identities``
    names who the address belongs to. Mail routing to one user while the login
    belongs to another is never intended, so a login email held by somebody
    else is a holder too.
    """
    return {
        address: set(holders)
        for address, holders in _email_holdings(conn).items()
    }


def duplicate_email_addresses(conn: sqlite3.Connection) -> dict[str, list[str]]:
    """Addresses more than one user holds, each with its sorted holders.

    A holder whose only claim is the login email is labelled ``<user> (login)``,
    because the fix differs: a routing address is removed from the profile, a
    login email through the admin Users page or `istota auth`.
    """
    out: dict[str, list[str]] = {}
    for address, holders in sorted(_email_holdings(conn).items()):
        if len(holders) < 2:
            continue
        out[address] = [
            f"{user} (login)" if kinds == {"login"} else user
            for user, kinds in sorted(holders.items())
        ]
    return out


def find_identity_conflicts(
    conn: sqlite3.Connection,
    user_id: str,
    *,
    email_addresses: "list[str] | None" = None,
    sms: str | None = None,
    whatsapp: str | None = None,
) -> dict[str, str]:
    """``{value: holder_user_id}`` for each value another user already holds.

    The uniqueness rule the self PUT, the admin PATCH and `istota user ensure`
    apply before writing. Only *adding* a held value is a conflict: an address
    already on ``user_id``'s own stored list passes even when somebody else
    holds it too, because a deployment may carry a duplicate from before this
    rule and refusing the resubmit would fail every unrelated save by either
    holder (and every deploy re-asserting the inventory). Doctor's
    ``users.email_address_uniqueness`` is where those are surfaced.

    The SMS and WhatsApp numbers have unique indexes already; this pre-check
    exists so the refusal can name the holder instead of surfacing an
    ``IntegrityError``. Keys are the values as submitted.
    """
    from istota.webui.auth import normalize_email

    conflicts: dict[str, str] = {}
    if email_addresses:
        holders = email_address_holders(conn)
        stored = _rows_or_none(
            conn,
            "SELECT email_addresses FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ) or []
        own = {
            normalize_email(a)
            for row in stored for a in _parse_json_list(row[0])
        }
        for address in email_addresses:
            key = normalize_email(address or "")
            if not key or key in own:
                continue
            others = sorted(holders.get(key, set()) - {user_id})
            if others:
                conflicts[address] = others[0]
    if sms:
        rows = _rows_or_none(
            conn,
            "SELECT user_id FROM user_profiles "
            "WHERE sms_phone_number = ? AND user_id <> ? ORDER BY user_id",
            (sms, user_id),
        )
        if rows:
            conflicts[sms] = rows[0][0]
    if whatsapp:
        rows = _rows_or_none(
            conn,
            "SELECT user_id FROM whatsapp_user_bindings "
            "WHERE bootstrap_phone_number = ? AND user_id <> ? ORDER BY user_id",
            (whatsapp, user_id),
        )
        if rows:
            conflicts[whatsapp] = rows[0][0]
    return conflicts


def _coerce_bool(value: object, default: bool = True) -> bool:
    """Coerce a stored/int/None value to bool; None → default."""
    if value is None:
        return default
    if isinstance(value, str):
        return value.strip().lower() not in ("", "0", "false", "no", "off")
    return bool(value)


@contextmanager
def _connect(
    db_path: Path, *, busy_timeout_ms: int | None = None,
) -> Iterator[sqlite3.Connection]:
    """Open a connection with 30s timeout, matching db.get_db semantics.

    ``busy_timeout_ms`` overrides it, for reads on the scheduler's main loop.
    """
    with sqlite_util.open_db(
        db_path, busy_timeout_ms=busy_timeout_ms, foreign_keys=False, commit=True,
    ) as conn:
        yield conn


def _row_to_profile(row: sqlite3.Row) -> UserProfile:
    return UserProfile(
        user_id=row["user_id"],
        display_name=row["display_name"] or "",
        email_addresses=_parse_json_list(row["email_addresses"]),
        sms_phone_number=str(_row_get(row, "sms_phone_number") or ""),
        timezone=row["timezone"] or "UTC",
        log_channel=row["log_channel"] or "",
        alerts_channel=row["alerts_channel"] or "",
        max_foreground_workers=int(row["max_foreground_workers"] or 0),
        max_background_workers=int(row["max_background_workers"] or 0),
        disabled_skills=_parse_json_list(row["disabled_skills"]),
        trusted_email_senders=_parse_json_list(row["trusted_email_senders"]),
        quiet_email_senders=_parse_json_list(row["quiet_email_senders"]),
        disabled_modules=_parse_json_list(row["disabled_modules"]),
        routing=_parse_json_dict(row["routing"]),
        default_destination=row["default_destination"] or "talk",
        # `_row_get`, like the columns below it: a row from a database whose
        # ALTER was blocked has no such column, and '' is the right answer there
        # — the per-surface heuristic keeps answering, which is what every user
        # had before this column existed.
        default_room=str(_row_get(row, "default_room") or ""),
        # No `or` default: '' is the unset value the floor resolution reads.
        outbound_approval=str(_row_get(row, "outbound_approval") or ""),
        external_turn_display=(
            _row_get(row, "external_turn_display") or "collapsed"
        ),
        relay_delivery=str(_row_get(row, "relay_delivery") or ""),
        default_briefings=_coerce_bool(_row_get(row, "default_briefings"), True),
        briefing_email_html=_coerce_bool(
            _row_get(row, "briefing_email_html"), True,
        ),
        timezone_follow_location=_coerce_bool(
            _row_get(row, "timezone_follow_location"), False,
        ),
        google_scopes=_parse_json_dict(_row_get(row, "google_scopes")),
    )


def _row_get(row: sqlite3.Row, key: str) -> object:
    """Read a column that may be absent on a not-yet-migrated row."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def _parse_json_dict(value: object) -> dict[str, str]:
    # ``object`` rather than ``str | None``: a not-yet-migrated row reaches
    # here through ``_row_get``, which can only promise "some column value".
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError) as e:
        logger.warning(
            "user_profiles: failed to decode JSON dict column (%s); falling back to {}",
            e,
        )
        return {}
    if not isinstance(parsed, dict):
        logger.warning(
            "user_profiles: JSON dict column has non-dict type %s; falling back to {}",
            type(parsed).__name__,
        )
        return {}
    return {str(k): str(v) for k, v in parsed.items()}


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


def get_profile(
    db_path: Path,
    user_id: str,
    *,
    conn: sqlite3.Connection | None = None,
) -> UserProfile | None:
    """Return the stored profile for ``user_id`` or None if no row exists.

    Pass ``conn`` to reuse an existing framework-DB connection (hot loops
    in the scheduler already hold one). Without it, opens a short-lived
    conn — keeps the API ergonomic for one-off callers.
    """
    if conn is not None:
        row = conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
        return _row_to_profile(row) if row else None
    with _connect(db_path) as cm_conn:
        row = cm_conn.execute(
            "SELECT * FROM user_profiles WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return _row_to_profile(row) if row else None


def read_profile_generation(
    db_path: Path, *, busy_timeout_ms: int | None = None,
) -> int:
    """The ``profile_generation`` counter, bumped by triggers on every write.

    Raises when the table is missing; the caller decides what that means.
    """
    with _connect(db_path, busy_timeout_ms=busy_timeout_ms) as conn:
        row = conn.execute(
            "SELECT generation FROM profile_generation WHERE id = 1"
        ).fetchone()
    return int(row[0]) if row else 0


def list_profiles(
    db_path: Path, *, busy_timeout_ms: int | None = None,
) -> dict[str, UserProfile]:
    """Return all stored profiles keyed by user_id."""
    out: dict[str, UserProfile] = {}
    with _connect(db_path, busy_timeout_ms=busy_timeout_ms) as conn:
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
    conn: sqlite3.Connection | None = None,
) -> UserProfile:
    """Insert a row for ``user_id`` if missing, return the resulting profile.

    Existing rows are NOT overwritten — this is the "first-login auto-seed"
    path. Use ``update_profile`` to change values explicitly.

    ``display_name`` and ``timezone`` are accepted as initial values for new
    rows; they are ignored when the row already exists. Caller should pass
    derived defaults (e.g. NC display_name).

    If ``seed_from`` is a ``UserConfig`` (or any object with the matching
    attributes), its list fields and channel/worker scalars are copied
    into the new row so the DB row carries the full TOML payload from the
    moment it exists. This eliminates the "DB has display_name but TOML
    has email_addresses" split-brain that would otherwise happen when the
    web callback auto-seeds before the scheduler's TOML import runs.

    Pass ``conn`` to read and insert inside the caller's transaction.
    """
    existing = get_profile(db_path, user_id, conn=conn)
    if existing is not None:
        return existing

    profile = _seeded_profile(
        user_id, display_name=display_name, timezone=timezone, seed_from=seed_from,
    )
    if conn is None:
        _insert(db_path, profile)
    else:
        insert_profile(conn, profile)
    logger.info("ensured user_profile user=%s (new row)", user_id)
    return profile


def _seeded_profile(
    user_id: str,
    *,
    display_name: str = "",
    timezone: str = "",
    seed_from: "object | None" = None,
) -> UserProfile:
    """The row ``ensure_profile`` inserts for a user it has not seen."""
    return UserProfile(
        user_id=user_id,
        display_name=display_name or _attr(seed_from, "display_name") or user_id,
        timezone=timezone or _attr(seed_from, "timezone") or "UTC",
        log_channel=_attr(seed_from, "log_channel") or "",
        alerts_channel=_attr(seed_from, "alerts_channel") or "",
        max_foreground_workers=int(_attr(seed_from, "max_foreground_workers") or 0),
        max_background_workers=int(_attr(seed_from, "max_background_workers") or 0),
        email_addresses=list(_attr(seed_from, "email_addresses") or []),
        sms_phone_number=str(_attr(seed_from, "sms_phone_number") or ""),
        trusted_email_senders=list(_attr(seed_from, "trusted_email_senders") or []),
        quiet_email_senders=list(_attr(seed_from, "quiet_email_senders") or []),
        disabled_skills=list(_attr(seed_from, "disabled_skills") or []),
        disabled_modules=list(_attr(seed_from, "disabled_modules") or []),
        routing=dict(_attr(seed_from, "routing") or {}),
        default_destination=_attr(seed_from, "default_destination") or "talk",
        outbound_approval=str(_attr(seed_from, "outbound_approval") or ""),
        external_turn_display=(
            _attr(seed_from, "external_turn_display") or "collapsed"
        ),
        default_briefings=_coerce_bool(_attr(seed_from, "default_briefings"), True),
        briefing_email_html=_coerce_bool(
            _attr(seed_from, "briefing_email_html"), True,
        ),
        timezone_follow_location=_coerce_bool(
            _attr(seed_from, "timezone_follow_location"), False,
        ),
    )


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


def _same_value(col: str, current: object, value: object) -> bool:
    """Whether writing ``value`` to ``col`` would leave ``current`` as it is.

    The rule `update_profile_with_status` reports ``noop`` by and the managed
    check passes a resubmitted value by, so a save that sends back what is
    stored is never an edit. Each column compares the way ``update_profile``
    would store it.
    """
    if col in _LIST_COLUMNS:
        return list(current or []) == list(value or [])
    if col in _DICT_COLUMNS:
        return dict(current or {}) == dict(value or {})
    if col in _BOOL_COLUMNS:
        bool_default = _BOOL_COLUMN_DEFAULTS[col]
        return _coerce_bool(current, bool_default) == _coerce_bool(value, bool_default)
    if col == "default_destination":
        return (current or "talk") == (value or "talk")
    if col == "external_turn_display":
        return (current or "collapsed") == (value or "collapsed")
    if col in {"max_foreground_workers", "max_background_workers"}:
        return int(current or 0) == int(value or 0)
    return (current or "") == (value or "")


def update_profile_with_status(
    db_path: Path,
    user_id: str,
    *,
    conn: sqlite3.Connection | None = None,
    **fields: object,
) -> "tuple[UserProfile, str]":
    """Idempotent partial update. Returns ``(profile, state)``.

    ``state`` is one of ``"created"``, ``"updated"``, ``"noop"`` — same
    contract as ``user_briefings.ensure_briefing``,
    ``db.upsert_user_resource``, and ``secrets_store.upsert_secret``.

    Behavior:
    - If no row exists: insert via :func:`ensure_profile` (seeded from
      ``display_name`` / ``timezone`` in ``fields`` when present), then
      apply remaining ``fields`` via :func:`update_profile`. State is
      ``"created"``.
    - If row exists and every field in ``fields`` already matches: no
      write. State is ``"noop"``.
    - Otherwise: apply via :func:`update_profile`. State is ``"updated"``.

    Pass ``conn`` to run the read, the insert and the update inside the
    caller's transaction, uncommitted; `istota user ensure` writes the managed
    set beside the profile that way.
    """
    existing = get_profile(db_path, user_id, conn=conn)
    if existing is None:
        seed_display = fields.get("display_name")
        seed_tz = fields.get("timezone")
        ensure_profile(
            db_path, user_id,
            display_name=seed_display if isinstance(seed_display, str) else "",
            timezone=seed_tz if isinstance(seed_tz, str) else "",
            conn=conn,
        )
        profile = update_profile(db_path, user_id, conn=conn, **fields)
        return profile, "created"

    if not fields:
        return existing, "noop"

    same = all(
        col in _PROFILE_COLUMNS and _same_value(col, getattr(existing, col), value)
        for col, value in fields.items()
    )
    if same:
        return existing, "noop"

    profile = update_profile(db_path, user_id, conn=conn, **fields)
    return profile, "updated"


def update_profile(
    db_path: Path,
    user_id: str,
    *,
    conn: sqlite3.Connection | None = None,
    **fields: object,
) -> UserProfile:
    """Partial update — only specified columns change. Returns the new profile.

    Raises ValueError if the user has no row yet (caller should ensure first)
    or if an unknown field is passed (defends against schema drift).

    Pass ``conn`` to write inside the caller's transaction; nothing is
    committed here then, and the returned profile is read on that connection.
    """
    if not fields:
        existing = get_profile(db_path, user_id, conn=conn)
        if existing is None:
            raise ValueError(f"no user_profile row for {user_id!r}")
        return existing

    unknown = set(fields) - set(_PROFILE_COLUMNS)
    if unknown:
        raise ValueError(f"unknown profile field(s): {sorted(unknown)}")

    sets: list[str] = []
    params: list[object] = []
    for col, value in fields.items():
        if col == "sms_phone_number":
            value = normalize_sms_phone_number(value, allow_empty=True)
        if col in _LIST_COLUMNS:
            value = json.dumps(list(value or []))
        elif col in _DICT_COLUMNS:
            value = json.dumps(dict(value or {}))
        elif col in _BOOL_COLUMNS:
            value = 1 if _coerce_bool(value, _BOOL_COLUMN_DEFAULTS[col]) else 0
        elif col in {"max_foreground_workers", "max_background_workers"}:
            value = int(value or 0)
        elif col == "default_destination":
            value = str(value) if value else "talk"
        elif col == "external_turn_display":
            value = str(value) if value else "collapsed"
        else:
            value = str(value or "")
        sets.append(f"{col} = ?")
        params.append(value)

    sets.append("updated_at = datetime('now')")
    params.append(user_id)
    sql = f"UPDATE user_profiles SET {', '.join(sets)} WHERE user_id = ?"

    def _apply(target: sqlite3.Connection) -> None:
        try:
            cur = target.execute(sql, params)
        except sqlite3.IntegrityError as exc:
            _raise_phone_conflict(exc)
        if cur.rowcount == 0:
            raise ValueError(f"no user_profile row for {user_id!r}")

    if conn is not None:
        _apply(conn)
    else:
        with _connect(db_path) as own:
            _apply(own)

    updated = get_profile(db_path, user_id, conn=conn)
    assert updated is not None  # row was just updated
    return updated


# --- Provisioning ownership: managed fields --------------------------------
#
# A field `istota user ensure --managed` asserted is re-written on every deploy,
# so the web refuses to edit it rather than let an edit be reverted silently.
# The CLI is the only writer of `user_profile_managed_fields`.

# Not a profile column: the WhatsApp number lives on `whatsapp_user_bindings`.
WHATSAPP_NUMBER_FIELD = "whatsapp_number"
MANAGEABLE_FIELDS = frozenset({*_PROFILE_COLUMNS, WHATSAPP_NUMBER_FIELD})


def managed_fields(conn: sqlite3.Connection, user_id: str) -> set[str]:
    """The fields provisioning asserts for ``user_id``; empty before the migration."""
    rows = _rows_or_none(
        conn,
        "SELECT field FROM user_profile_managed_fields WHERE user_id = ?",
        (user_id,),
    )
    return {row[0] for row in rows or []}


def set_managed_fields(
    conn: sqlite3.Connection, user_id: str, fields: "Iterable[str]",
) -> bool:
    """Replace ``user_id``'s managed set with ``fields``. True when it changed.

    Writes on ``conn`` and commits nothing. An empty set on a database without
    the table is a no-op rather than an error: there is nothing to release.
    """
    wanted = set(fields)
    unknown = wanted - MANAGEABLE_FIELDS
    if unknown:
        raise ValueError(f"unknown managed field(s): {sorted(unknown)}")
    if not wanted and _rows_or_none(
        conn, "SELECT 1 FROM user_profile_managed_fields LIMIT 1",
    ) is None:
        return False
    if managed_fields(conn, user_id) == wanted:
        return False
    conn.execute(
        "DELETE FROM user_profile_managed_fields WHERE user_id = ?", (user_id,),
    )
    conn.executemany(
        "INSERT INTO user_profile_managed_fields (user_id, field) VALUES (?, ?)",
        [(user_id, field) for field in sorted(wanted)],
    )
    return True


def refused_managed_fields(
    conn: sqlite3.Connection, user_id: str, updates: "dict[str, object]",
) -> list[str]:
    """The managed fields in ``updates`` whose value differs from the stored one.

    The check both web writers run before writing, inside their transaction.
    A managed field resubmitted unchanged is not an edit and passes, by the
    rule `_same_value` states, because the settings page and the admin modal
    send back whatever they loaded and an unrelated save must not be refused.
    ``updates`` holds coerced values; ``whatsapp_number`` is compared with the
    binding's bootstrap number.
    """
    managed = managed_fields(conn, user_id)
    if not managed:
        return []
    stored = get_profile(Path(), user_id, conn=conn) or UserProfile(user_id=user_id)
    refused: list[str] = []
    for name, value in updates.items():
        if name not in managed:
            continue
        if name == WHATSAPP_NUMBER_FIELD:
            rows = _rows_or_none(
                conn,
                "SELECT bootstrap_phone_number FROM whatsapp_user_bindings "
                "WHERE user_id = ?",
                (user_id,),
            )
            current = (rows[0][0] if rows else "") or ""
            if (value or "") != current:
                refused.append(name)
        elif name in _PROFILE_COLUMNS:
            if not _same_value(name, getattr(stored, name), value):
                refused.append(name)
    return sorted(refused)


def delete_profile(db_path: Path, user_id: str) -> bool:
    """Remove a profile row and the user's avatars. True if a profile row went.

    The avatar delete rides this function's own connection, inside the same
    transaction. A *second* connection opened here would wait out the 30s busy
    timeout on the write lock this one is already holding and then raise — the
    hazard AGENTS.md documents for `notification_store` — which is why
    `avatars.delete_all_user_avatars` takes a connection rather than a path.

    The avatar rows go whether or not a profile row existed: they are keyed on
    the user id, not on the profile, so a user removed twice still leaves none
    behind. The return value stays a statement about the profile row.
    """
    with _connect(db_path) as conn:
        avatars.delete_all_user_avatars(conn, user_id)
        # The WhatsApp binding goes with the profile, on this function's own
        # connection for the reason the avatars do. It is a separate table
        # rather than a column only because BSUID and window state are
        # transport runtime data — leaving it behind would keep a deleted
        # user's phone number and BSUID reserved, and would leave a live
        # principal an authenticated inbound event could still resolve to.
        # `sms_phone_number` needs no line here because it is a column on the
        # row this deletes.
        conn.execute(
            "DELETE FROM whatsapp_user_bindings WHERE user_id = ?", (user_id,),
        )
        # A re-created user starts unlocked: the next `--managed` converge
        # records what it asserts again.
        set_managed_fields(conn, user_id, ())
        cur = conn.execute(
            "DELETE FROM user_profiles WHERE user_id = ?", (user_id,),
        )
        return cur.rowcount > 0


def _insert(db_path: Path, profile: UserProfile, *, replace: bool = False) -> None:
    """Insert (replace=False) or upsert (replace=True) a profile row.

    ``created_at`` survives an upsert because we use ON CONFLICT instead of
    INSERT OR REPLACE. ``updated_at`` is always set to ``datetime('now')``.
    """
    with _connect(db_path) as conn:
        insert_profile(conn, profile, replace=replace)


def insert_profile(conn: sqlite3.Connection, profile: UserProfile, *, replace: bool = False) -> None:
    """Write a profile inside the caller's transaction, preserving existing rows."""
    profile.sms_phone_number = normalize_sms_phone_number(
        profile.sms_phone_number, allow_empty=True,
    )
    insert_cols = ("user_id", *_PROFILE_COLUMNS)
    placeholders = ", ".join(["?"] * len(insert_cols))
    values = (
        profile.user_id,
        profile.display_name,
        json.dumps(list(profile.email_addresses)),
        profile.sms_phone_number,
        profile.timezone,
        profile.log_channel,
        profile.alerts_channel,
        int(profile.max_foreground_workers or 0),
        int(profile.max_background_workers or 0),
        json.dumps(list(profile.disabled_skills)),
        json.dumps(list(profile.trusted_email_senders)),
        json.dumps(list(profile.quiet_email_senders)),
        json.dumps(list(profile.disabled_modules)),
        json.dumps(dict(profile.routing)),
        profile.default_destination or "talk",
        profile.default_room or "",
        profile.outbound_approval or "",
        profile.external_turn_display or "collapsed",
        profile.relay_delivery or "",
        1 if profile.default_briefings else 0,
        1 if profile.briefing_email_html else 0,
        1 if profile.timezone_follow_location else 0,
        json.dumps(dict(profile.google_scopes)),
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
            INSERT INTO user_profiles ({cols_sql}, updated_at)
            VALUES ({placeholders}, datetime('now'))
            ON CONFLICT(user_id) DO NOTHING
        """

    try:
        conn.execute(sql, values)
    except sqlite3.IntegrityError as exc:
        _raise_phone_conflict(exc)


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
            sms_phone_number=str(getattr(user_config, "sms_phone_number", "") or ""),
            timezone=getattr(user_config, "timezone", "") or "UTC",
            log_channel=getattr(user_config, "log_channel", "") or "",
            alerts_channel=getattr(user_config, "alerts_channel", "") or "",
            max_foreground_workers=int(getattr(user_config, "max_foreground_workers", 0) or 0),
            max_background_workers=int(getattr(user_config, "max_background_workers", 0) or 0),
            disabled_skills=list(getattr(user_config, "disabled_skills", []) or []),
            trusted_email_senders=list(getattr(user_config, "trusted_email_senders", []) or []),
            quiet_email_senders=list(getattr(user_config, "quiet_email_senders", []) or []),
            disabled_modules=list(getattr(user_config, "disabled_modules", []) or []),
            routing=dict(getattr(user_config, "routing", {}) or {}),
            default_destination=getattr(user_config, "default_destination", "") or "talk",
            outbound_approval=str(getattr(user_config, "outbound_approval", "") or ""),
            external_turn_display=(
                getattr(user_config, "external_turn_display", "") or "collapsed"
            ),
            default_briefings=_coerce_bool(getattr(user_config, "default_briefings", True), True),
            briefing_email_html=_coerce_bool(
                getattr(user_config, "briefing_email_html", True), True,
            ),
            timezone_follow_location=_coerce_bool(
                getattr(user_config, "timezone_follow_location", False), False,
            ),
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
    setattr(user_config, "sms_phone_number", profile.sms_phone_number or "")
    setattr(user_config, "default_destination", profile.default_destination or "talk")
    setattr(user_config, "outbound_approval", profile.outbound_approval or "")
    setattr(
        user_config, "external_turn_display",
        profile.external_turn_display or "collapsed",
    )
    setattr(user_config, "default_briefings", bool(profile.default_briefings))
    setattr(user_config, "briefing_email_html", bool(profile.briefing_email_html))
    setattr(
        user_config, "timezone_follow_location",
        bool(profile.timezone_follow_location),
    )
    for attr in (
        "log_channel", "alerts_channel",
        "max_foreground_workers", "max_background_workers",
    ):
        setattr(user_config, attr, getattr(profile, attr))

    # List fields: DB row owns them once it exists. The auto-seed path
    # carries TOML lists into the row, so an empty DB list is a deliberate
    # "user cleared this" signal.
    for attr in (
        "email_addresses", "disabled_skills",
        "trusted_email_senders", "quiet_email_senders", "disabled_modules",
    ):
        setattr(user_config, attr, list(getattr(profile, attr) or []))

    # routing is a dict but follows the same "DB owns it once the row exists"
    # rule as the list fields.
    setattr(user_config, "routing", dict(profile.routing or {}))

    return user_config
