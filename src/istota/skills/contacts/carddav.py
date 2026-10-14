"""Nextcloud address books, scoped to the task user's DAV owner principal."""

from collections.abc import Mapping
from dataclasses import dataclass, field
from hashlib import sha256
import os
import re
from urllib.parse import quote, unquote, urljoin, urlsplit
from xml.etree import ElementTree as ET

import httpx
import vobject

from istota.lib.dav_xml import DavXmlError, parse_multistatus, principal_user, safe_segment

from .model import (
    AddressBook, Contact, ContactInput, apply_changes_to_vcard,
    contact_from_vcard, match_score, new_vcard, one_line,
)

DAV = "{DAV:}"
CARD = "{urn:ietf:params:xml:ns:carddav}"
OC = "{http://owncloud.org/ns}"
MAX_RESPONSE_BYTES = 20 * 1024 * 1024
MAX_CARDS = 5000


@dataclass
class BackendContext:
    env: Mapping[str, str] = field(default_factory=lambda: dict(os.environ), repr=False)
    client: httpx.Client | None = field(default=None, repr=False)
    skipped_cards: int = 0
    truncated_books: list[str] = field(default_factory=list)


class CardDAVError(Exception):
    def __init__(self, reason: str, status_code: int | None = None):
        self.reason = reason
        self.status_code = status_code
        super().__init__(reason)


class _TooLarge(Exception):
    pass


@dataclass
class _Book:
    public: AddressBook
    url: str


def available(ctx: BackendContext) -> bool:
    return bool(ctx.env.get("CARDDAV_URL"))


def _require_user(ctx: BackendContext) -> str:
    user = ctx.env.get("ISTOTA_USER_ID", "")
    if not user.strip():
        raise CardDAVError("no_user")
    return user


def _home(ctx: BackendContext) -> str:
    _require_user(ctx)
    base = ctx.env.get("CARDDAV_URL", "").rstrip("/")
    parts = urlsplit(base)
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.query or parts.fragment or parts.username:
        raise CardDAVError("no_contact_source")
    username = ctx.env.get("CARDDAV_USERNAME", "")
    if not safe_segment(username):
        raise CardDAVError("no_contact_source")
    return base + "/addressbooks/users/" + quote(username, safe="") + "/"


def _child_url(parent: str, href: str, *, collection: bool = False) -> str | None:
    if any(ord(c) < 32 or c == "\\" for c in href):
        return None
    raw = urlsplit(href)
    # Check before urljoin, which would normalize away dot segments.
    if raw.query or raw.fragment or raw.username:
        return None
    if any(not safe_segment(unquote(part)) for part in raw.path.strip("/").split("/")):
        return None
    target = urlsplit(urljoin(parent, href))
    origin = urlsplit(parent)
    if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
        return None
    if not target.path.startswith(origin.path):
        return None
    suffix = target.path[len(origin.path):]
    if collection:
        suffix = suffix.rstrip("/")
    if not safe_segment(unquote(suffix)):
        return None
    return target._replace(path=target.path.rstrip("/") + "/" if collection else target.path).geturl()


def _dav(ctx, method, url, *, body=None, headers=None, depth=None):
    _require_user(ctx)
    request_headers = dict(headers or {})
    if depth is not None:
        request_headers["Depth"] = str(depth)
    if body is not None:
        request_headers.setdefault("Content-Type", "application/xml; charset=utf-8")
    client = ctx.client or httpx.Client(timeout=30.0, trust_env=False)
    try:
        with client.stream(method, url, content=body, headers=request_headers,
                           auth=(ctx.env.get("CARDDAV_USERNAME", ""), ctx.env.get("CARDDAV_PASSWORD", "")),
                           follow_redirects=False, timeout=30.0) as response:
            status = response.status_code
            if not 200 <= status < 300:
                reason = {401: "credentials_invalid", 403: "read_only", 404: "not_found",
                          412: "changed_since_read", 429: "rate_limited"}.get(status)
                raise CardDAVError(reason or ("server_error" if status >= 500 else "unexpected_response"), status)
            chunks = []
            size = 0
            for chunk in response.iter_bytes(chunk_size=65536):
                size += len(chunk)
                if size > MAX_RESPONSE_BYTES:
                    raise _TooLarge()
                chunks.append(chunk)
            return b"".join(chunks), response.headers
    except httpx.HTTPError:
        raise CardDAVError("unreachable") from None
    finally:
        if ctx.client is None:
            client.close()


def _multistatus(data):
    try:
        return parse_multistatus(data)
    except DavXmlError:
        raise CardDAVError("invalid_response") from None


def _discover(ctx):
    home = _home(ctx)
    root = ET.Element(DAV + "propfind")
    props = ET.SubElement(root, DAV + "prop")
    for tag in (DAV + "resourcetype", DAV + "displayname", DAV + "owner", OC + "owner-principal",
                OC + "read-only", DAV + "current-user-privilege-set", DAV + "sync-token"):
        ET.SubElement(props, tag)
    try:
        data, _ = _dav(ctx, "PROPFIND", home, body=ET.tostring(root), depth=1)
    except _TooLarge:
        raise CardDAVError("response_too_large") from None
    found = {}
    for href, props in _multistatus(data):
        resource = props.get(DAV + "resourcetype")
        if resource is None or resource.find(CARD + "addressbook") is None:
            continue
        owner = props.get(OC + "owner-principal") if OC + "owner-principal" in props else props.get(DAV + "owner")
        if principal_user(owner) != _require_user(ctx):
            continue
        url = _child_url(home, href, collection=True)
        if url is None:
            continue
        privileges = props.get(DAV + "current-user-privilege-set")
        writable = privileges is not None and any(privileges.find(".//" + DAV + name) is not None for name in ("write", "all"))
        readonly = props.get(OC + "read-only")
        if readonly is not None and (readonly.text or "").strip().lower() in {"true", "1"}:
            writable = False
        label = props.get(DAV + "displayname")
        key = sha256(href.encode()).hexdigest()[:12]
        public = AddressBook(key, "nextcloud", one_line(label.text or "") if label is not None else key, writable)
        found[key] = _Book(public, url)
    return found


def books(ctx: BackendContext) -> list[AddressBook]:
    return [book.public for book in _discover(ctx).values()]


def _query(query=None):
    root = ET.Element(CARD + "addressbook-query")
    props = ET.SubElement(root, DAV + "prop")
    ET.SubElement(props, DAV + "getetag")
    ET.SubElement(props, CARD + "address-data")
    filters = ET.SubElement(root, CARD + "filter", {"test": "anyof"})
    if query is not None:
        for name in ("FN", "EMAIL", "TEL", "NICKNAME", "ORG"):
            prop = ET.SubElement(filters, CARD + "prop-filter", {"name": name})
            match = ET.SubElement(prop, CARD + "text-match", {"collation": "i;unicode-casemap", "match-type": "contains"})
            match.text = query
    return ET.tostring(root)


def _fetch(ctx, book, query=None):
    data, _ = _dav(ctx, "REPORT", book.url, body=_query(query), depth=1)
    rows = _multistatus(data)
    if len(rows) > MAX_CARDS:
        raise _TooLarge()
    return rows


def search(ctx: BackendContext, query: str, *, limit: int) -> list[Contact]:
    _require_user(ctx)
    if not query.strip():
        raise CardDAVError("empty_query")
    contacts = []
    for book in _discover(ctx).values():
        try:
            rows = _fetch(ctx, book)
        except _TooLarge:
            ctx.truncated_books.append(book.public.key)
            try:
                rows = _fetch(ctx, book, query)
            except _TooLarge:
                raise CardDAVError("response_too_large") from None
        for href, props in rows:
            url = _child_url(book.url, href)
            if url is None:
                continue
            data = props.get(CARD + "address-data")
            if data is None:
                continue
            tag = props.get(DAV + "getetag")
            contact = contact_from_vcard(data.text or "", contact_id=f"nc:{book.public.key}/{urlsplit(url).path.rsplit('/', 1)[1]}",
                                         book=book.public.label, writable=book.public.writable,
                                         etag=tag.text if tag is not None else None)
            if contact is None:
                ctx.skipped_cards += 1
            elif match_score(contact, query):
                contacts.append(contact)
    contacts.sort(key=lambda c: (-match_score(c, query), c.name.casefold(), c.id))
    return contacts[:max(0, limit)]


def _resolve(ctx, contact_id):
    found = _discover(ctx)
    if not contact_id.startswith("nc:") or "/" not in contact_id:
        raise CardDAVError("not_found")
    key, name = contact_id[3:].split("/", 1)
    book = found.get(key)
    if book is None or not safe_segment(unquote(name)):
        raise CardDAVError("not_found")
    url = _child_url(book.url, name)
    if url is None:
        raise CardDAVError("not_found")
    return book, url


def _read(ctx, url):
    try:
        data, headers = _dav(ctx, "GET", url)
    except _TooLarge:
        raise CardDAVError("response_too_large") from None
    try:
        return data.decode("utf-8-sig"), headers.get("etag")
    except UnicodeDecodeError:
        raise CardDAVError("invalid_vcard") from None


def _contact(data, book, contact_id, etag):
    contact = contact_from_vcard(data, contact_id=contact_id, book=book.public.label,
                                 writable=book.public.writable, etag=etag)
    if contact is None:
        raise CardDAVError("invalid_vcard")
    return contact


def get(ctx: BackendContext, contact_id: str) -> Contact:
    book, url = _resolve(ctx, contact_id)
    data, etag = _read(ctx, url)
    return _contact(data, book, contact_id, etag)


def _writable(book):
    if not book.public.writable:
        raise CardDAVError("read_only")


def _etag(value):
    if not value or value.startswith("W/") or not re.fullmatch(r'"[^"\r\n]*"', value):
        raise CardDAVError("changed_since_read")
    return value


def _write(ctx, method, url, *, body=None, headers=None):
    try:
        return _dav(ctx, method, url, body=body, headers=headers)
    except _TooLarge:
        raise CardDAVError("response_too_large") from None


def create(ctx: BackendContext, book_key: str, fields: ContactInput) -> Contact:
    book = _discover(ctx).get(book_key)
    if book is None:
        raise CardDAVError("not_found")
    _writable(book)
    data = new_vcard(fields)
    uid = str(vobject.readOne(data).uid.value)
    name = uid + ".vcf"
    _, headers = _write(ctx, "PUT", book.url + name, body=data.encode(),
                        headers={"If-None-Match": "*", "Content-Type": "text/vcard; charset=utf-8"})
    return _contact(data, book, f"nc:{book_key}/{name}", headers.get("etag"))


def update(ctx: BackendContext, contact_id: str, changes: ContactInput, *, etag: str | None = None) -> Contact:
    book, url = _resolve(ctx, contact_id)
    _writable(book)
    data, current_etag = _read(ctx, url)
    _contact(data, book, contact_id, current_etag)
    data = apply_changes_to_vcard(data, changes)
    _, headers = _write(ctx, "PUT", url, body=data.encode(),
                        headers={"If-Match": _etag(etag if etag is not None else current_etag),
                                 "Content-Type": "text/vcard; charset=utf-8"})
    return _contact(data, book, contact_id, headers.get("etag"))


def delete(ctx: BackendContext, contact_id: str, *, etag: str | None = None) -> None:
    book, url = _resolve(ctx, contact_id)
    _writable(book)
    if etag is None:
        _, etag = _read(ctx, url)
    _write(ctx, "DELETE", url, headers={"If-Match": _etag(etag)})
