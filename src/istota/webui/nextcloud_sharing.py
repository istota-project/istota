"""Share a user's Nextcloud calendars and address books with the bot account.

Web process only. Every request goes out as the user, through
`nextcloud_files.dav_request`, and the share state lives in Nextcloud: a level
is read back from `oc:invite` after every write, never stored here. The
Nextcloud facts this relies on are in `.claude/rules/web-ui.md` ("Nextcloud
DAV sharing, as observed").
"""

import logging
import re
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Literal
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

from istota.config import NEXTCLOUD_SHARING_LEVELS
from istota.lib.dav_xml import DavXmlError, parse_multistatus, principal_user, safe_segment
from istota.webui import nextcloud_files
from istota.workspace.listing import ListingError

logger = logging.getLogger("istota.webui.nextcloud_sharing")

DAV = "{DAV:}"
OC = "{http://owncloud.org/ns}"
CAL = "{urn:ietf:params:xml:ns:caldav}"
CARD = "{urn:ietf:params:xml:ns:carddav}"

Kind = Literal["calendars", "contacts"]
Level = Literal["off", "readonly", "full"]

KINDS: tuple[Kind, ...] = ("calendars", "contacts")
_RANK = {level: rank for rank, level in enumerate(NEXTCLOUD_SHARING_LEVELS)}
_ROOTS = {"calendars": "calendars", "contacts": "addressbooks/users"}
_TYPES = {"calendars": CAL + "calendar", "contacts": CARD + "addressbook"}
# Collections Nextcloud generates: the birthday calendar, app calendars such
# as Deck boards, "Recently contacted" and the system address book.
_GENERATED_PREFIXES = ("app-generated--", "z-app-generated--", "z-server-generated--")
_GENERATED_NAMES = {"contact_birthdays"}
_ID = re.compile(r"[0-9a-f]{12}")

_LIST_ERRORS = {
    403: "You cannot read your Nextcloud calendars and address books.",
    404: "Your Nextcloud calendars and address books were not found.",
    None: "Could not load your Nextcloud calendars and address books.",
}
_TOO_LARGE = "There are too many calendars or address books to list."
_UNREADABLE = "Could not read the Nextcloud response."
# A POST hands back every status but 401, which `dav_request` retries.
_POST_OK = frozenset(range(100, 600)) - {401}
_POST_REASONS = {
    403: "Nextcloud did not allow this share.",
    404: "This calendar or address book no longer exists.",
}
_POST_REFUSED = "Nextcloud did not accept the change."
_NOT_APPLIED = "Nextcloud did not apply this change."
_GONE = _POST_REASONS[404]

_PRINCIPAL_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
)


def _listing_body(kind: Kind) -> bytes:
    calendar = "<cal:supported-calendar-component-set/>" if kind == "calendars" else ""
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" '
        'xmlns:cal="urn:ietf:params:xml:ns:caldav"><d:prop>'
        "<d:resourcetype/><d:displayname/><oc:owner-principal/><d:owner/><oc:invite/>"
        f"{calendar}</d:prop></d:propfind>"
    ).encode()


@dataclass
class Collection:
    id: str
    kind: Kind
    name: str
    level: Level
    # The request target's last segment. Never sent to the browser.
    segment: str = field(repr=False)


@dataclass
class KindState:
    ceiling: str
    available: bool


@dataclass
class Failure:
    id: str
    name: str
    reason: str


@dataclass
class SetResult:
    kinds: dict[str, KindState]
    collections: list[Collection]
    failed: list[Failure]


def kind_states(config) -> dict[str, KindState]:
    """What the card may offer per kind: the ceiling, and whether the bot can use a share."""
    bot = config.nextcloud.username
    caldav = config.caldav
    calendars_usable = bool(bot) and not caldav.url and (not caldav.username or caldav.username == bot)
    return {
        "calendars": KindState(config.nextcloud.sharing.calendars, calendars_usable),
        "contacts": KindState(config.nextcloud.sharing.contacts, bool(bot)),
    }


def _is_bot(config, user_id: str) -> bool:
    return user_id.casefold() == config.nextcloud.username.casefold()


def _parse(response) -> list[tuple[str, dict[str, ET.Element]]]:
    body = nextcloud_files.read_bounded(response, _TOO_LARGE)
    try:
        return parse_multistatus(body)
    except DavXmlError:
        raise ListingError(502, _UNREADABLE) from None


def _canonical_uid(config, user_id: str) -> str:
    """The Nextcloud uid the token belongs to, held to the session user.

    The OAuth callback stores the token under `resolve_user_id(uid) or uid`,
    so the same expression must give back the session user here.
    """
    with nextcloud_files.dav_request(
        config, user_id, [], "PROPFIND", ok={207}, errors=_LIST_ERRORS, folder=True, root=None,
        headers={"Depth": "0", "Content-Type": "application/xml; charset=utf-8"}, content=_PRINCIPAL_BODY,
    ) as response:
        rows = _parse(response)
    uid = None
    for _href, props in rows:
        uid = principal_user(props.get(DAV + "current-user-principal"))
        if uid:
            break
    if not uid or (config.resolve_user_id(uid) or uid) != user_id:
        logger.warning("Nextcloud principal does not match the session user %s", user_id)
        raise ListingError(409, nextcloud_files._RECONNECT)
    return uid


def _segment(user_id: str, home_path: str, href: str) -> str | None:
    """The child's last path segment, if `href` is a direct child of the home."""
    raw = urlsplit(href)
    if raw.query or raw.fragment or any(ord(c) < 32 or c == "\\" for c in href):
        return None
    path = raw.path
    if unquote(path).rstrip("/") == unquote(home_path).rstrip("/"):
        return ""
    segment = unquote(path.rstrip("/").rsplit("/", 1)[-1])
    if not safe_segment(segment) or unquote(path) != unquote(home_path) + segment + "/":
        logger.warning("Skipping a Nextcloud collection href for %s (length %d)", user_id, len(href))
        return None
    return segment


def _level(invite: ET.Element, bot: str) -> Level:
    level: Level = "off"
    for sharee in invite.findall(OC + "user"):
        who = principal_user(sharee.findtext(DAV + "href") or "")
        if who is None or who.casefold() != bot.casefold():
            continue
        access = sharee.find(OC + "access")
        if access is not None and access.find(OC + "read-write") is not None:
            return "full"
        # An access Nextcloud 30 never sends still grants something, so it
        # reads as readonly and stays removable rather than hidden.
        level = "readonly"
    return level


def _shareable(kind: Kind, segment: str, props: dict[str, ET.Element], uid: str) -> bool:
    resourcetype = props.get(DAV + "resourcetype")
    if resourcetype is None or resourcetype.find(_TYPES[kind]) is None:
        return False
    owner = principal_user(props.get(OC + "owner-principal")) or principal_user(props.get(DAV + "owner"))
    if owner != uid:
        return False
    if OC + "invite" not in props:
        return False
    if segment in _GENERATED_NAMES or segment.startswith(_GENERATED_PREFIXES):
        return False
    if kind == "calendars":
        components = props.get(CAL + "supported-calendar-component-set")
        if components is not None:
            names = {comp.get("name", "").upper() for comp in components.findall(CAL + "comp")}
            if not names & {"VEVENT", "VTODO"}:
                return False
    return True


def _display_name(props: dict[str, ET.Element], segment: str) -> str:
    label = props.get(DAV + "displayname")
    name = " ".join((label.text or "").split()) if label is not None else ""
    return name or segment


def _list_kind(config, user_id: str, uid: str, kind: Kind) -> list[Collection]:
    with nextcloud_files.dav_request(
        config, user_id, [], "PROPFIND", ok={207}, errors=_LIST_ERRORS, folder=True,
        root=_ROOTS[kind], account=uid,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"}, content=_listing_body(kind),
    ) as response:
        home_path = urlsplit(str(response.request.url)).path
        rows = _parse(response)
    found = []
    for href, props in rows:
        segment = _segment(user_id, home_path, href)
        if not segment or not _shareable(kind, segment, props, uid):
            continue
        found.append(Collection(
            id=sha256(href.encode()).hexdigest()[:12], kind=kind,
            name=_display_name(props, segment),
            level=_level(props[OC + "invite"], config.nextcloud.username), segment=segment,
        ))
    return found


def _list(config, user_id: str, uid: str) -> list[Collection]:
    return [collection for kind in KINDS for collection in _list_kind(config, user_id, uid, kind)]


def list_collections(config, user_id: str) -> tuple[dict[str, KindState], list[Collection]]:
    """The user's shareable calendars and address books, each at the bot's current level.

    Both kinds are always listed, whatever the ceiling or availability, so a
    share that exists can always be removed.
    """
    kinds = kind_states(config)
    if not config.nextcloud.username or _is_bot(config, user_id):
        return kinds, []
    uid = _canonical_uid(config, user_id)
    return kinds, _list(config, user_id, uid)


def _share_body(bot: str, level: str) -> bytes:
    href = f"<d:href>principal:principals/users/{escape(bot)}</d:href>"
    if level == "off":
        change = f"<oc:remove>{href}</oc:remove>"
    else:
        access = "<oc:read-write/>" if level == "full" else "<oc:read/>"
        change = f"<oc:set>{href}{access}</oc:set>"
    return f'<oc:share xmlns:oc="http://owncloud.org/ns" xmlns:d="DAV:">{change}</oc:share>'.encode()


def _post(config, user_id: str, uid: str, collection: Collection, level: str) -> int:
    with nextcloud_files.dav_request(
        config, user_id, [collection.segment], "POST", ok=_POST_OK, errors=_LIST_ERRORS, folder=True,
        root=_ROOTS[collection.kind], account=uid,
        headers={"Content-Type": "application/xml; charset=utf-8"},
        content=_share_body(config.nextcloud.username, level),
    ) as response:
        return response.status_code


def set_levels(config, user_id: str, changes: dict[str, str]) -> SetResult:
    """Set the bot's level on each named collection, judged by a re-read.

    A malformed id or level is a 400 and a raise above the ceiling or into an
    unavailable kind is a 403, both before anything is sent. A well-formed id
    no longer in the listing is reported as gone and the rest proceed.
    """
    for collection_id, level in changes.items():
        if not isinstance(collection_id, str) or not _ID.fullmatch(collection_id) or not isinstance(level, str) or level not in _RANK:
            raise ListingError(400, "Unknown calendar, address book or access level.")
    if not config.nextcloud.username or _is_bot(config, user_id):
        raise ListingError(400, "There is no bot account to share with.")
    kinds = kind_states(config)
    uid = _canonical_uid(config, user_id)
    current = {c.id: c for c in _list(config, user_id, uid)}
    failed: list[Failure] = []
    pending: list[tuple[Collection, str]] = []
    for collection_id, level in changes.items():
        collection = current.get(collection_id)
        if collection is None:
            failed.append(Failure(collection_id, "", _GONE))
            continue
        if level == collection.level:
            continue
        state = kinds[collection.kind]
        raising = _RANK[level] > _RANK[collection.level]
        if raising and (not state.available or _RANK[level] > _RANK.get(state.ceiling, 0)):
            raise ListingError(403, "This instance does not allow that access.")
        pending.append((collection, level))
    sent: dict[str, str] = {}
    for collection, level in pending:
        status = _post(config, user_id, uid, collection, level)
        if 200 <= status < 300:
            sent[collection.id] = level
            continue
        logger.warning("Nextcloud refused a share change for %s on %s: %d", user_id, collection.id, status)
        failed.append(Failure(collection.id, collection.name, _POST_REASONS.get(status, _POST_REFUSED)))
    reread = _list(config, user_id, uid) if pending else list(current.values())
    after = {c.id: c for c in reread}
    for collection_id, level in sent.items():
        collection = after.get(collection_id)
        if collection is None:
            failed.append(Failure(collection_id, current[collection_id].name, _GONE))
        elif collection.level != level:
            logger.warning("Nextcloud did not apply a share change for %s on %s", user_id, collection_id)
            failed.append(Failure(collection_id, collection.name, _NOT_APPLIED))
    return SetResult(kinds, reread, failed)


def unshare_all(config, user_id: str) -> SetResult:
    """Remove the bot's direct share from every collection, across both kinds and any ceiling."""
    kinds, collections = list_collections(config, user_id)
    changes = {c.id: "off" for c in collections if c.level != "off"}
    if not changes:
        return SetResult(kinds, collections, [])
    return set_levels(config, user_id, changes)
