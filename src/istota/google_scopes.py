"""Google Workspace service → OAuth scope map (ISSUE-240).

One table, three consumers: the per-user scope picker on the settings card,
the granted-scope display next to the Connected pill, and the docs. Before
this existed, ``[google_workspace] scopes`` was an opaque list of URLs that
nothing could group, compare or explain.

Two vocabularies meet here:

``level``
    What a user picks per service: ``off`` / ``readonly`` / ``full``. Ordered,
    so a selection can be clamped against the operator's ceiling.

``ceiling``
    ``GoogleWorkspaceConfig.scopes`` read through this map. It is a maximum,
    not a request: the OAuth client belongs to a Google Cloud project whose
    enabled APIs the operator controls, so a user asking for a service the
    instance does not offer would fail at Google's end with an error they can
    do nothing about. The UI renders "not offered" and "not granted" as
    different states for exactly that reason.

Everything here is pure — no DB, no network, no config import at module level.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Mapping, Sequence

LEVEL_OFF = "off"
LEVEL_READONLY = "readonly"
LEVEL_FULL = "full"

#: Levels in ascending order of access. Index order is the comparison order.
LEVELS: tuple[str, ...] = (LEVEL_OFF, LEVEL_READONLY, LEVEL_FULL)

_LEVEL_RANK = {level: rank for rank, level in enumerate(LEVELS)}

_BASE = "https://www.googleapis.com/auth/"

# Scopes Google appends to (or accepts alongside) any grant made through an
# OIDC-discovered client. They name no Workspace service, so they are neither
# offerable nor a shortfall — but they do arrive in the callback's `scope`
# field, and reporting them as "unrecognised" on the card would be noise.
BOILERPLATE_SCOPES: frozenset[str] = frozenset({
    "openid",
    f"{_BASE}userinfo.email",
    f"{_BASE}userinfo.profile",
})


@dataclass(frozen=True)
class GoogleService:
    """One Workspace service and the scopes that buy each access level."""

    key: str
    label: str
    #: Scopes that together grant read access.
    readonly: tuple[str, ...]
    #: Scopes that together grant read+write access. Google's write scopes
    #: subsume their read-only counterparts, so a level is one or the other,
    #: never both.
    full: tuple[str, ...]

    def scopes_for(self, level: str) -> tuple[str, ...]:
        if level == LEVEL_FULL:
            return self.full
        if level == LEVEL_READONLY:
            return self.readonly
        return ()


#: The map. Order is the order every surface renders in.
SERVICES: tuple[GoogleService, ...] = (
    GoogleService(
        key="drive",
        label="Drive",
        readonly=(f"{_BASE}drive.readonly",),
        full=(f"{_BASE}drive",),
    ),
    GoogleService(
        key="gmail",
        label="Gmail",
        readonly=(f"{_BASE}gmail.readonly",),
        # gmail.modify covers read, send and label changes without granting
        # the destructive full-mailbox scope (`.../auth/gmail`, which also
        # permits permanent deletion).
        full=(f"{_BASE}gmail.modify",),
    ),
    GoogleService(
        key="calendar",
        label="Calendar",
        readonly=(f"{_BASE}calendar.readonly",),
        full=(f"{_BASE}calendar",),
    ),
    GoogleService(
        key="sheets",
        label="Sheets",
        readonly=(f"{_BASE}spreadsheets.readonly",),
        full=(f"{_BASE}spreadsheets",),
    ),
    GoogleService(
        key="docs",
        label="Docs",
        readonly=(f"{_BASE}documents.readonly",),
        full=(f"{_BASE}documents",),
    ),
    GoogleService(
        key="chat",
        label="Chat",
        # Chat splits spaces from messages, so each level needs both — the
        # skill documents `chat spaces list` (spaces) and message creation
        # (messages). This is the one service with no scope in the shipped
        # default ceiling despite an open egress host and documented verbs.
        readonly=(f"{_BASE}chat.spaces.readonly", f"{_BASE}chat.messages.readonly"),
        full=(f"{_BASE}chat.spaces", f"{_BASE}chat.messages"),
    ),
)

_BY_KEY: dict[str, GoogleService] = {s.key: s for s in SERVICES}


def _build_scope_index() -> dict[str, tuple[str, str]]:
    index: dict[str, tuple[str, str]] = {}
    for svc in SERVICES:
        for scope in svc.readonly:
            index[scope] = (svc.key, LEVEL_READONLY)
        for scope in svc.full:
            index[scope] = (svc.key, LEVEL_FULL)
    return index


_SCOPE_INDEX = _build_scope_index()


def service(key: str) -> GoogleService | None:
    """Look up a service by key, or None if it is not in the map."""
    return _BY_KEY.get(key)


def scope_owner(scope: str) -> tuple[str, str] | None:
    """``(service_key, level)`` for a scope URL, or None if unrecognised."""
    return _SCOPE_INDEX.get(scope)


def levels_from_scopes(scopes: Iterable[str]) -> dict[str, str]:
    """Highest access level per service implied by ``scopes``.

    Services with no matching scope are absent from the result — the caller
    decides whether that means "off" or "not offered".
    """
    levels: dict[str, str] = {}
    for scope in scopes or ():
        owner = _SCOPE_INDEX.get(scope)
        if owner is None:
            continue
        key, level = owner
        if _LEVEL_RANK[level] > _LEVEL_RANK.get(levels.get(key, LEVEL_OFF), 0):
            levels[key] = level
    return levels


def offered_services(ceiling_scopes: Iterable[str]) -> list[dict]:
    """Every service with the maximum level this instance offers.

    Always names all of :data:`SERVICES`, with ``max_level == "off"`` for the
    ones the operator's list does not cover, so the UI can say "this instance
    does not offer it" rather than leaving a silent gap.
    """
    levels = levels_from_scopes(ceiling_scopes)
    return [
        {
            "service": svc.key,
            "label": svc.label,
            "max_level": levels.get(svc.key, LEVEL_OFF),
        }
        for svc in SERVICES
    ]


def unoffered_scopes(ceiling_scopes: Iterable[str]) -> list[str]:
    """Ceiling scopes this map does not know, in the operator's own order.

    No picker row can express these, so they are requested unconditionally
    rather than dropped — see :func:`resolve_selection`. Surfaced separately so
    the UI can name what it is asking for on the user's behalf.
    """
    seen: set[str] = set()
    out: list[str] = []
    for scope in ceiling_scopes or ():
        if scope in _SCOPE_INDEX or scope in seen:
            continue
        out.append(scope)
        seen.add(scope)
    return out


def default_selection(ceiling_scopes: Iterable[str]) -> dict[str, str]:
    """The selection an unconfigured user gets: everything the operator allows.

    Existing users have no stored selection, and this is what keeps their
    behaviour identical to before the picker existed.
    """
    levels = levels_from_scopes(ceiling_scopes)
    return {svc.key: levels.get(svc.key, LEVEL_OFF) for svc in SERVICES}


def normalize_selection(raw: object) -> dict[str, str]:
    """Coerce a stored/submitted selection to ``{service: level}``.

    Unknown services and unknown levels are dropped rather than rejected: the
    stored value outlives any one version of this map, and a selection that
    fails to parse must not lock a user out of reconnecting. An explicit
    ``off`` is kept — it is what distinguishes "I turned everything off" from
    "I never chose", which resolve treats differently.
    """
    if not isinstance(raw, Mapping):
        return {}
    out: dict[str, str] = {}
    for key, value in raw.items():
        if not isinstance(key, str) or key not in _BY_KEY:
            continue
        if not isinstance(value, str):
            continue
        level = value.strip().lower()
        if level not in _LEVEL_RANK:
            continue
        out[key] = level
    return out


def resolve_selection(
    selection: Mapping[str, str] | None,
    ceiling_scopes: Iterable[str],
) -> list[str]:
    """The scope list to request for a user, clamped to the operator ceiling.

    An empty (or absent) selection means "unset" and yields the whole ceiling.
    A non-empty selection is authoritative: a service it does not name is
    off, so widening the ceiling later never silently widens an existing
    user's request — they re-consent or they don't.

    **A ceiling scope this map does not know is requested unconditionally**,
    appended verbatim after the mapped ones. Dropping it would be wrong twice
    over: no picker row can turn it off, so its absence is not a user choice;
    and an operator running a perfectly ordinary list of narrow scopes
    (``drive.file``, ``gmail.send``, ``calendar.events``) would find every user
    silently downgraded to nothing, with connect refusing outright once the
    resolution came back empty. It is also what keeps ``openid`` in the request
    on an instance that lists it — authlib mints the OIDC nonce only when the
    resolved scope carries it.
    """
    ceiling_list = list(ceiling_scopes or ())
    ceiling = levels_from_scopes(ceiling_list)
    chosen = normalize_selection(selection)
    unset = not chosen

    scopes: list[str] = []
    for svc in SERVICES:
        max_level = ceiling.get(svc.key, LEVEL_OFF)
        if max_level == LEVEL_OFF:
            continue
        want = max_level if unset else chosen.get(svc.key, LEVEL_OFF)
        if _LEVEL_RANK[want] > _LEVEL_RANK[max_level]:
            want = max_level
        scopes.extend(svc.scopes_for(want))

    scopes.extend(unoffered_scopes(ceiling_list))
    return scopes


def summarize_granted(granted_scopes: Sequence[str]) -> dict:
    """Group a granted scope list into per-service rows for display.

    Returns ``{"services": [...], "unrecognized": [...]}``. Each row carries
    ``complete``: False when the grant holds only part of what that level
    needs, which is possible because Google's consent screen lets a user
    deselect individual boxes.

    An unrecognised scope is surfaced verbatim rather than dropped — the map
    will lag a hand-edited config, and silently hiding a granted scope is the
    failure this display exists to end. ``also`` is the same rule applied
    within a service: a scope belonging to a level *below* the one reported
    (Chat granted at ``chat.spaces`` + ``chat.messages.readonly``) is in the
    map, so it never reaches ``unrecognized``, and it is not in the reported
    level's tuple, so it would otherwise appear nowhere at all.
    """
    granted = list(granted_scopes or [])
    granted_set = set(granted)
    levels = levels_from_scopes(granted)

    rows: list[dict] = []
    for svc in SERVICES:
        level = levels.get(svc.key)
        if level is None:
            continue
        wanted = svc.scopes_for(level)
        held = [s for s in wanted if s in granted_set]
        rows.append({
            "service": svc.key,
            "label": svc.label,
            "level": level,
            "scopes": held,
            "complete": len(held) == len(wanted),
            "also": [
                s for s in granted
                if s not in wanted and _SCOPE_INDEX.get(s, (None, None))[0] == svc.key
            ],
        })

    unrecognized = [
        s for s in granted
        if s not in _SCOPE_INDEX and s not in BOILERPLATE_SCOPES
    ]
    return {"services": rows, "unrecognized": unrecognized}


def missing_scopes(
    requested: Sequence[str],
    granted: Sequence[str],
) -> list[str]:
    """Requested scopes the grant does not cover, in ``requested`` order.

    A full grant satisfies a read-only request (Google hands back the broader
    scope and the narrower one never appears), so an exact match is not the
    only way to be covered. But only a *strictly higher* granted level counts
    as cover: at the same level the scope has to be there by name, or a
    partially granted multi-scope service reads as satisfied — Chat granted
    ``chat.spaces`` alone would report ``chat.messages`` as covered, silencing
    the reconnect warning for exactly the case it exists to catch.

    Unrecognised requested scopes fall back to exact matching — there is
    nothing else to compare them by.
    """
    granted_set = set(granted or [])
    granted_levels = levels_from_scopes(granted or [])

    missing: list[str] = []
    for scope in requested or ():
        if scope in granted_set:
            continue
        owner = _SCOPE_INDEX.get(scope)
        if owner is None:
            missing.append(scope)
            continue
        key, level = owner
        have = granted_levels.get(key, LEVEL_OFF)
        if _LEVEL_RANK[have] <= _LEVEL_RANK[level]:
            missing.append(scope)
    return missing
