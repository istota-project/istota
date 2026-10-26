"""Reading an accepted Open Cloud Mesh share over WebDAV: the rules both readers share.

Two processes read a share's files: the `shares` skill, host-side for a task,
and the web process for the Files page (`webui.share_files`). Each gets the
secret its own way, the skill over the private credential channel and the web
process from the owner's stored credential, and everything after that is here
so the two cannot drift:

- **Remote paths** (`valid_segment`, `url_for`, `relative_parts`): no ``.``
  or ``..``, no empty segment, no backslash or control character, each
  segment percent-encoded on the wire, and every ``href`` in a listing
  reduced to segments under the share root or left out.
- **The root's state** (`check_root`): the depth-0 PROPFIND of the root that
  starts every use. Nextcloud tells a recipient nothing when the owner
  changes a share's permissions, so access is read again each time and
  recorded when it changed; a 401 or 404 there revokes the share, any other
  failure records ``error``, and a success after an error records ``active``.
- **Listing entries** (`entry_fields`): file or folder, size, modified time.

Every request goes through `sandbox.guarded_http` with ``[sharing]
private_hosts``, the secret as the Basic user with an empty password, to the
share's verified WebDAV base and nowhere else.
"""

from __future__ import annotations

import logging
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

logger = logging.getLogger("istota.sharing.ocm_files")

MAX_TRANSFER_BYTES = 64 * 1024 * 1024
LISTING_MAX_BYTES = 4 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 60.0
# Generous: a transfer moves a whole file.
REQUEST_DEADLINE_SECONDS = 600.0
MAX_SEGMENT_CHARS = 255

_DAV = "{DAV:}"
LISTING_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns"><d:prop>'
    b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
    b"</d:prop></d:propfind>"
)


class ShareRefused(Exception):
    """A share that could not be used; ``reason`` is a fixed code, the row already updated."""

    def __init__(self, reason: str, *, status: int | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status


# ------------------------------------------------------------------ the paths

def valid_segment(name: str) -> bool:
    """One path segment, decoded: a name a folder can hold and nothing more."""
    return (
        isinstance(name, str) and 0 < len(name) <= MAX_SEGMENT_CHARS
        and name not in (".", "..")
        and not any(c in name for c in "/\\")
        and not any(ord(c) < 32 or ord(c) == 127 for c in name)
    )


def url_for(root: str, parts: tuple[str, ...], *, folder: bool = False) -> str:
    """The URL of ``parts`` under the share root, each segment percent-encoded."""
    if not parts:
        return root
    return root + "/".join(quote(part, safe="") for part in parts) + ("/" if folder else "")


def relative_parts(root: str, href: object) -> tuple[str, ...] | None:
    """An ``href`` from a multistatus as segments under ``root``, or None outside it.

    Refuses a query, a fragment, user information, another origin, a path that
    does not start with the root's segments, and any segment that decodes to
    something `valid_segment` refuses (an encoded ``/`` or ``..`` included),
    checked segment by segment so a decoded separator cannot split one.
    """
    if not isinstance(href, str) or not href or "\\" in href \
            or any(ord(c) < 32 or ord(c) == 127 for c in href):
        return None
    parts = urlsplit(href)
    if parts.query or parts.fragment or parts.username or parts.password:
        return None
    base = urlsplit(root)
    if parts.scheme or parts.netloc:
        if (parts.scheme.lower(), parts.netloc.lower()) != (base.scheme.lower(), base.netloc.lower()):
            return None
    if not parts.path.startswith("/"):
        return None
    raw = parts.path.split("/")
    base_raw = base.path.rstrip("/").split("/")
    if [unquote(s) for s in raw[:len(base_raw)]] != [unquote(s) for s in base_raw]:
        return None
    rest = raw[len(base_raw):]
    if rest and rest[-1] == "":
        rest = rest[:-1]
    names = tuple(unquote(s) for s in rest)
    if not all(valid_segment(name) for name in names):
        return None
    return names


def checked_root(source) -> str:
    """The share root's URL, refused unless its origin is the share's own host."""
    from istota.sharing import ocm

    try:
        root = ocm.share_root_url(source)
    except ocm.ShareActionRefused:
        raise ShareRefused("invalid_response") from None
    origin = urlsplit(root)
    if origin.scheme not in ("http", "https") or origin.netloc.lower() != source.remote_host.lower():
        raise ShareRefused("credential_host_mismatch")
    return root


def entry_fields(props: dict) -> tuple[bool, int | None, datetime | None]:
    """A listing row's props: whether it is a folder, its size, when it was modified."""
    kind = props.get(_DAV + "resourcetype")
    folder = kind is not None and kind.find(_DAV + "collection") is not None
    size = None
    length = props.get(_DAV + "getcontentlength")
    if length is not None and (length.text or "").strip().isdigit():
        size = int(length.text.strip())
    modified = None
    stamp = props.get(_DAV + "getlastmodified")
    if stamp is not None and stamp.text:
        try:
            modified = parsedate_to_datetime(stamp.text.strip())
        except (TypeError, ValueError):
            modified = None
    return folder, None if folder else size, modified


# ------------------------------------------------------------- the row's state

def revoke(db_path: Path, source, reason: str) -> None:
    from istota import db
    from istota.sharing import sources

    with db.get_db(db_path) as conn:
        sources.revoke(conn, source.id, source.user_id, reason=reason)
    logger.info("ocm share source=%s revoked reason=%s", source.id, reason)


def record_error(db_path: Path, source, reason: str) -> None:
    from istota import db
    from istota.sharing import sources

    try:
        with db.get_db(db_path) as conn:
            sources.set_state(conn, source.id, source.user_id, "error", reason=reason)
    except ValueError:
        logger.info("ocm share source=%s error not recorded reason=%s", source.id, reason)
    logger.info("ocm share source=%s error reason=%s", source.id, reason)


def _record_root(db_path: Path, source, access: str, kind: str) -> None:
    from istota import db
    from istota.sharing import sources

    with db.get_db(db_path) as conn:
        # Under the write lock: a signed re-offer sets `reoffer_pending` in the
        # same extras this rewrites.
        conn.execute("BEGIN IMMEDIATE")
        current = sources.get_source(conn, source.id, source.user_id)
        if current is None:
            return
        if current.access != access:
            sources.set_access(conn, source.id, source.user_id, access)
            logger.info("ocm share source=%s access=%s", source.id, access)
        if current.extras.get("root_kind") != kind:
            sources.set_details(conn, source.id, source.user_id,
                                extras={**current.extras, "root_kind": kind})
        if current.state == "error":
            sources.set_state(conn, source.id, source.user_id, "active")


def check_root(db_path: Path, config, source, secret: str):
    """The depth-0 PROPFIND of the root that starts every use; `ocm.ShareRoot`, recorded.

    A 401 or 404 revokes the share (``share_revoked``); any other failure
    records ``error`` with its code. Either way `ShareRefused` carries it.
    """
    from istota.sharing import ocm

    try:
        root = ocm.share_root(config, source, secret)
    except ocm.ShareActionRefused as exc:
        if exc.status in (401, 404):
            revoke(db_path, source, "share_revoked")
            raise ShareRefused("share_revoked", status=exc.status) from None
        record_error(db_path, source, exc.reason)
        raise ShareRefused(exc.reason, status=exc.status) from None
    _record_root(db_path, source, root.access, root.kind)
    return root
