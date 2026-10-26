"""Browse and read an accepted Open Cloud Mesh share from the Files page (#849).

The web process reads the share's secret from the owner's stored credential
(`sharing.ocm.share_secret`), as `ocm.accept` and `ocm.remove` already do in
this process, and sends it only to the share's verified WebDAV base through
`sandbox.guarded_http`. It is read for the signed-in owner's own request and
nowhere else; the credential's grant in Settings > Credentials limits which
tasks may use it and does not apply to its owner here. A task still reads it
only through the private credential channel (`skills/shares`).

Every request starts with `sharing.ocm_files.check_root`, so the access shown
is the provider's live answer and a share the sharer ended is revoked on the
spot, exactly as the skill does. Names are the sharer's words and come back
as plain JSON strings for the page to render as text. A download streams
through under the skill's transfer limit and the `/chat/files` response policy
(`workspace_routes`): `attachment` unless the head sniffs as a raster, and
never the sharer's server's `Content-Type`.
"""

from __future__ import annotations

import logging
import re
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import timezone
from pathlib import PurePosixPath

from istota.lib.image_sniff import SNIFF_BYTES, sniff_raster
from istota.webui.nextcloud_files import Download
from istota.workspace.listing import LIST_MAX_ENTRIES, ListingError
from istota.workspace.places import share_label as share_name
from istota.workspace.preview import PREVIEW_MAX_BYTES, classify

logger = logging.getLogger("istota.webui.share_files")

_PLACE = re.compile(r"\Ashare:([0-9]{1,18})\Z")
_SINGLE_RANGE = re.compile(r"bytes=\d*-\d*")
_PASSED_HEADERS = ("content-range", "accept-ranges", "last-modified")
_STAT_BODY = (
    b'<?xml version="1.0" encoding="utf-8"?>'
    b'<d:propfind xmlns:d="DAV:"><d:prop>'
    b"<d:resourcetype/><d:getcontentlength/><d:getlastmodified/>"
    b"</d:prop></d:propfind>"
)

_GONE = "The sharer's server no longer accepts this share, so it was removed."
_REFUSALS = {
    "share_revoked": (410, _GONE),
    "secret_missing": (410, "The share's credential was deleted, so the share was removed."),
    "secret_unreadable": (503, "The share's credential cannot be read right now."),
    "too_large": (502, "This shared folder is too large to list."),
}
_UNREACHABLE = "The sharing server could not be reached."


class ShareListingError(ListingError):
    """A `ListingError` with the fixed code a share refusal carries."""

    def __init__(self, status: int, message: str, code: str):
        super().__init__(status, message)
        self.code = code


def _refused(code: str, *, file: bool = False) -> ShareListingError:
    if code == "too_large" and file:
        return ShareListingError(413, "This file is larger than the transfer limit.", code)
    status, message = _REFUSALS.get(code, (502, _UNREACHABLE))
    return ShareListingError(status, message, code)


def source_id_for(place: str) -> int | None:
    match = _PLACE.fullmatch(place) if isinstance(place, str) else None
    return int(match.group(1)) if match else None


def _parts(path_parts: list[str], missing: str) -> tuple[str, ...]:
    from istota.sharing.ocm_files import valid_segment

    if not all(valid_segment(part) for part in path_parts):
        raise ListingError(404, missing)
    return tuple(path_parts)


@dataclass
class _Share:
    config: object
    source: object
    secret: str
    root: str
    access: str
    kind: str


def _open(config, user_id: str, place: str) -> _Share:
    """The owner's reachable share, its secret and a fresh root check, in that order."""
    from istota import db
    from istota.credentials import local
    from istota.credentials import store as secrets_store
    from istota.sharing import ocm, ocm_files, sources

    source_id = source_id_for(place)
    if source_id is None:
        raise ListingError(404, "Place not found.")
    with db.get_db(config.db_path) as conn:
        source = sources.get_source(conn, source_id, user_id)
        if source is None or source.kind != ocm.KIND or source.state not in sources.REACHABLE_STATES:
            raise ListingError(404, "Place not found.")
        if not secrets_store.secret_key_available():
            raise _refused("secret_unreadable")
        secret = ocm.share_secret(conn, user_id, source_id)
        stored = secret is not None or local.is_local(conn, user_id, sources.credential_name(source_id))
    if secret is None:
        if stored:
            raise _refused("secret_unreadable")
        ocm_files.revoke(config.db_path, source, "secret_missing")
        raise _refused("secret_missing")
    try:
        root = ocm_files.checked_root(source)
        info = ocm_files.check_root(config.db_path, config, source, secret)
    except ocm_files.ShareRefused as exc:
        raise _refused(exc.reason) from None
    return _Share(config, source, secret, root, info.access, info.kind)


def _send(share: _Share, method: str, url: str, *, depth: str, max_bytes: int,
          file: bool = False):
    from istota.sandbox import guarded_http
    from istota.sharing import ocm_files

    with guarded_http.new_client(timeout=ocm_files.REQUEST_TIMEOUT_SECONDS) as client:
        try:
            response = guarded_http.guarded_send(
                client, method, url,
                private_hosts=share.config.sharing.private_hosts,
                max_bytes=max_bytes, timeout=ocm_files.REQUEST_TIMEOUT_SECONDS,
                deadline_seconds=ocm_files.REQUEST_DEADLINE_SECONDS,
                auth=(share.secret, ""),
                headers={"Depth": depth, "Content-Type": "application/xml; charset=utf-8"},
                content=ocm_files.LISTING_BODY if depth == "1" else _STAT_BODY,
            )
        except guarded_http.GuardRefused as exc:
            if exc.code != "too_large":
                ocm_files.record_error(share.config.db_path, share.source, exc.code)
            raise _refused(exc.code, file=file) from None
    if response.status_code == 401:
        ocm_files.revoke(share.config.db_path, share.source, "share_revoked")
        raise _refused("share_revoked")
    return response


def _rows(share: _Share, response, missing: str):
    """A multistatus' rows; a 404 is ``missing``, a 403 refused here, a 5xx recorded."""
    from istota.lib import dav_xml
    from istota.sharing import ocm_files

    status = response.status_code
    if status == 404:
        raise ListingError(404, missing)
    if status == 403:
        raise ShareListingError(403, "The sharer's server does not let you read this.", "http_403")
    if status != 207:
        if status >= 500:
            ocm_files.record_error(share.config.db_path, share.source, f"http_{status}")
        raise _refused(f"http_{status}")
    try:
        return dav_xml.parse_multistatus(response.content)
    except dav_xml.DavXmlError:
        raise _refused("invalid_response") from None


def _entry(name: str, props: dict, share_path: str) -> dict:
    from istota.sharing.ocm_files import entry_fields

    folder, size, modified = entry_fields(props)
    return {
        "name": name, "kind": "dir" if folder else "file", "size": size,
        "modified": modified.timestamp() if modified is not None else 0,
        "hidden": name.startswith("."), "share_path": share_path,
    }


def list_folder(config, user_id: str, place: str, path_parts: list[str]) -> dict:
    """One folder of the share, its direct children only; a file share as its one file."""
    from istota.sharing.ocm_files import LISTING_MAX_BYTES, entry_fields, relative_parts, url_for

    parts = _parts(path_parts, "Folder not found.")
    share = _open(config, user_id, place)
    file_root = share.kind == "file"
    if file_root and parts:
        raise ListingError(404, "Folder not found.")
    url = url_for(share.root, parts, folder=True)
    rows = _rows(share, _send(share, "PROPFIND", url, depth="1", max_bytes=LISTING_MAX_BYTES),
                 "Folder not found.")
    entries, seen = [], set()
    skipped = 0
    truncated = False
    for href, props in rows:
        found = relative_parts(share.root, href)
        if found is None:
            skipped += 1
            continue
        if found == parts:
            if file_root:
                entries.append(_entry(share_name(share.source), props, ""))
            elif not entry_fields(props)[0]:
                raise ListingError(404, "Folder not found.")
            continue
        if found[:-1] != parts or found[-1] in seen:
            skipped += 1
            continue
        if len(entries) >= LIST_MAX_ENTRIES:
            truncated = True
            break
        seen.add(found[-1])
        entries.append(_entry(found[-1], props, "/".join(found)))
    entries.sort(key=lambda entry: (entry["kind"] != "dir", entry["name"].casefold()))
    return {
        "place": place, "path": "/".join(parts), "entries": entries,
        "truncated": truncated, "skipped_names": skipped,
        "access": share.access, "root_kind": share.kind,
    }


def _target(share: _Share, parts: tuple[str, ...]):
    """The file at ``parts`` (the root, for a file share): its name and props."""
    from istota.sharing.ocm_files import entry_fields, url_for

    if not parts:
        if share.kind != "file":
            raise ListingError(404, "File not found.")
        name = share_name(share.source)
    else:
        name = parts[-1]
    rows = _rows(share, _send(share, "PROPFIND", url_for(share.root, parts), depth="0",
                              max_bytes=64 * 1024, file=True), "File not found.")
    if not rows:
        raise _refused("invalid_response")
    props = rows[0][1]
    if entry_fields(props)[0]:
        raise ListingError(400, "This is a folder, not a file.")
    return name, props


def _too_large(size) -> bool:
    from istota.sharing import ocm_files

    return isinstance(size, int) and size > ocm_files.MAX_TRANSFER_BYTES


def _stream(share: _Share, stack: ExitStack, parts: tuple[str, ...], headers: dict):
    from istota.sandbox import guarded_http
    from istota.sharing import ocm_files

    client = stack.enter_context(guarded_http.new_client(timeout=ocm_files.REQUEST_TIMEOUT_SECONDS))
    try:
        streamed = stack.enter_context(guarded_http.guarded_stream(
            client, "GET", ocm_files.url_for(share.root, parts),
            private_hosts=share.config.sharing.private_hosts,
            max_bytes=ocm_files.MAX_TRANSFER_BYTES, timeout=ocm_files.REQUEST_TIMEOUT_SECONDS,
            deadline_seconds=ocm_files.REQUEST_DEADLINE_SECONDS,
            auth=(share.secret, ""), headers=headers,
        ))
    except guarded_http.GuardRefused as exc:
        ocm_files.record_error(share.config.db_path, share.source, exc.code)
        raise _refused(exc.code, file=True) from None
    status = streamed.status_code
    if status == 401:
        ocm_files.revoke(share.config.db_path, share.source, "share_revoked")
        raise _refused("share_revoked")
    if status == 404:
        raise ListingError(404, "File not found.")
    if status == 416:
        raise ListingError(416, "That part of the file is not available.")
    if status == 403:
        raise ShareListingError(403, "The sharer's server does not let you read this.", "http_403")
    if status not in (200, 206):
        if status >= 500:
            ocm_files.record_error(share.config.db_path, share.source, f"http_{status}")
        raise _refused(f"http_{status}")
    return streamed


def preview_file(config, user_id: str, place: str, path_parts: list[str]) -> dict:
    """The viewer's preview payload for one file in the share."""
    from istota.sandbox.guarded_http import GuardRefused
    from istota.sharing.ocm_files import entry_fields

    parts = _parts(path_parts, "File not found.")
    share = _open(config, user_id, place)
    name, props = _target(share, parts)
    _, size, modified = entry_fields(props)
    head = bytearray()
    with ExitStack() as stack:
        streamed = _stream(share, stack, parts, {"Accept-Encoding": "identity"})
        try:
            for chunk in streamed.chunks:
                head.extend(chunk)
                if len(head) > PREVIEW_MAX_BYTES:
                    break
        except GuardRefused as exc:
            raise _refused(exc.code, file=True) from None
    size = size if isinstance(size, int) else len(head)
    stamp = ""
    if modified is not None:
        moment = modified.astimezone(timezone.utc)
        stamp = moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"
    truncated = len(head) > PREVIEW_MAX_BYTES or size > PREVIEW_MAX_BYTES
    return classify(name, size, stamp, bytes(head[:PREVIEW_MAX_BYTES]), truncated=truncated)


def open_file(config, user_id: str, place: str, path_parts: list[str],
              range_header: str | None = None) -> Download:
    """Open one file's bytes for streaming to the browser; the caller closes the result.

    Refused before any byte when the share root's listing says it is larger
    than the transfer limit, or when the answer declares as much; one that
    declares nothing is cut off at the limit.
    """
    from istota.sandbox.guarded_http import GuardRefused
    from istota.sharing.ocm_files import entry_fields

    parts = _parts(path_parts, "File not found.")
    share = _open(config, user_id, place)
    name, props = _target(share, parts)
    if _too_large(entry_fields(props)[1]):
        raise ListingError(413, "This file is larger than the transfer limit.")
    headers = {"Accept-Encoding": "identity"}
    if range_header and _SINGLE_RANGE.fullmatch(range_header.strip()):
        headers["Range"] = range_header.strip()
    stack = ExitStack()
    try:
        streamed = _stream(share, stack, parts, headers)
        declared = streamed.headers.get("content-length", "")
        if declared.isdigit() and _too_large(int(declared)):
            raise ListingError(413, "This file is larger than the transfer limit.")
        head = bytearray()
        for chunk in streamed.chunks:
            head.extend(chunk)
            if len(head) >= SNIFF_BYTES:
                break
    except GuardRefused as exc:
        stack.close()
        raise _refused(exc.code, file=True) from None
    except BaseException:
        stack.close()
        raise
    status = streamed.status_code
    inline_type = sniff_raster(bytes(head)) if status == 200 else None
    passed = {key: streamed.headers[key] for key in _PASSED_HEADERS if key in streamed.headers}
    if declared.isdigit() and "content-encoding" not in streamed.headers:
        passed["content-length"] = declared

    def body():
        try:
            if head:
                yield bytes(head)
            yield from streamed.chunks
        except GuardRefused:
            # Headers are already sent; ending the body short is all that is left.
            return
        finally:
            stack.close()

    logger.info("ocm share file source=%s status=%s", share.source.id, status)
    return Download(status, PurePosixPath(name).name or "shared-file", inline_type, passed,
                    body(), stack)
