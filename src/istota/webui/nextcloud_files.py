"""Browse and read a user's Nextcloud files with their web-only OAuth credential."""

import mimetypes
import re
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field, replace
from datetime import timezone
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode, urlsplit

import httpx

from istota.lib.image_sniff import SNIFF_BYTES, sniff_raster
from istota.nextcloud import OcsError, dav
from istota.webui import tokens
from istota.workspace.listing import LIST_MAX_ENTRIES, ListingError
from istota.workspace.preview import PREVIEW_MAX_BYTES, classify

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_RECONNECT = "Nextcloud is not connected. Reconnect in Settings."
_UNREACHABLE = "Nextcloud is not reachable right now."
_FOLDER_ERRORS = {
    403: "You cannot read this Nextcloud folder.",
    404: "Folder not found.",
    None: "Could not load this Nextcloud folder.",
}
_FILE_ERRORS = {
    403: "You cannot read this Nextcloud file.",
    404: "File not found.",
    416: "That part of the file is not available.",
    None: "Could not load this Nextcloud file.",
}
_SINGLE_RANGE = re.compile(r"bytes=\d*-\d*")
_PASSED_HEADERS = ("content-length", "content-range", "accept-ranges", "last-modified")


@dataclass
class Download:
    status: int
    name: str
    inline_type: str | None
    headers: dict[str, str]
    body: Iterator[bytes]
    _stack: ExitStack = field(repr=False)

    def close(self) -> None:
        self._stack.close()


def _safe_parts(parts: list[str], errors: dict) -> None:
    if any(part in {".", ".."} or any(ord(c) < 32 or c == "\\" for c in part) for part in parts):
        raise ListingError(404, errors[404])


@contextmanager
def _dav(config, user_id: str, parts: list[str], method: str, *, ok: set[int],
         errors: dict, folder: bool, headers: dict | None = None, content: bytes | None = None):
    """One request to the caller's own DAV root, retried once on a refreshed token.

    Yields the open response. Only the user's own access token is ever sent:
    no bot credential, no storage prefix, and a missing or revoked token is
    the reconnect refusal rather than a fallback.
    """
    _safe_parts(parts, errors)
    if not config.nextcloud_enabled or not tokens.feature_enabled(config):
        raise ListingError(409, _RECONNECT)
    # This credential belongs to the OAuth provider, not the bot's internal URL.
    base = config.web.oauth2_provider.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise ListingError(503, "Nextcloud is not configured.")
    if not user_id or user_id in {".", ".."} or any(c in user_id for c in "/\\") or any(ord(c) < 32 for c in user_id):
        raise ListingError(404, errors[404])
    path = "/".join(parts)
    url = f"{base}/remote.php/dav/files/{quote(user_id, safe='')}/"
    if path:
        url += quote(path, safe="/") + ("/" if folder else "")
    token = tokens.get_access_token(config.db_path, config, user_id)
    if token is None:
        raise ListingError(409, _RECONNECT)
    for attempt in range(2):
        stack = ExitStack()
        try:
            response = stack.enter_context(httpx.stream(
                method, url, content=content,
                headers={**(headers or {}), "Authorization": f"Bearer {token}"},
                timeout=15, follow_redirects=False,
            ))
        except httpx.HTTPError:
            stack.close()
            raise ListingError(502, _UNREACHABLE) from None
        status = response.status_code
        if status in ok:
            with stack:
                try:
                    yield response
                except httpx.HTTPError:
                    raise ListingError(502, _UNREACHABLE) from None
            return
        stack.close()
        if status != 401:
            raise ListingError(status if status in errors else 502, errors.get(status, errors[None]))
        if attempt == 1:
            raise ListingError(409, _RECONNECT)
        token = tokens.get_access_token(config.db_path, config, user_id, force_refresh=True)
        if token is None:
            raise ListingError(409, _RECONNECT)


def _read_multistatus(config, user_id, response) -> list[dict]:
    body = bytearray()
    for chunk in response.iter_bytes():
        body.extend(chunk)
        if len(body) > _MAX_RESPONSE_BYTES:
            raise ListingError(502, "This Nextcloud folder is too large to list.")
    # Only the parser gets this copy. No bot credential or storage prefix is used.
    base = config.web.oauth2_provider.rstrip("/")
    user_config = replace(config, nextcloud=replace(
        config.nextcloud, url=base, username=user_id, app_password="", dav_prefix="",
    ))
    try:
        return dav.parse_multistatus(user_config, body.decode("utf-8"))
    except (OcsError, UnicodeError):
        raise ListingError(502, "Could not read the Nextcloud response.") from None


def _propfind(config, user_id, parts, *, depth: str, folder: bool, errors: dict) -> list[dict]:
    with _dav(
        config, user_id, parts, "PROPFIND", ok={207}, errors=errors, folder=folder,
        headers={"Depth": depth, "Content-Type": "application/xml"}, content=dav.PROPFIND_BODY,
    ) as response:
        return _read_multistatus(config, user_id, response)


def list_folder(config, user_id: str, parts: list[str]) -> dict:
    rows = _propfind(config, user_id, parts, depth="1", folder=True, errors=_FOLDER_ERRORS)
    base = config.web.oauth2_provider.rstrip("/")
    path = "/".join(parts)
    target = "/" + path
    if not any(row["path"] == target and row["is_dir"] for row in rows):
        raise ListingError(404, "Folder not found.")
    entries = []
    seen = set()
    skipped = 0
    truncated = False
    for row in rows:
        name = row["name"]
        if row["path"] == target:
            continue
        if (str(PurePosixPath(row["path"]).parent) != target or name in {".", ".."}
                or any(ord(c) < 32 or c == "\\" for c in name) or name in seen):
            skipped += 1
            continue
        if len(entries) >= LIST_MAX_ENTRIES:
            truncated = True
            break
        seen.add(name)
        try:
            modified = parsedate_to_datetime(row["last_modified"]).timestamp()
        except (ValueError, TypeError, OverflowError):
            modified = 0
        file_id = row["fileid"]
        if file_id and file_id.isascii() and file_id.isdecimal():
            web_url = f"{base}/index.php/f/{file_id}"
        else:
            directory = row["path"] if row["is_dir"] else target
            web_url = f"{base}/index.php/apps/files/?{urlencode({'dir': directory, 'scrollto': name})}"
        entries.append({
            "name": name, "kind": "dir" if row["is_dir"] else "file",
            "size": row["size"], "modified": modified, "hidden": name.startswith("."),
            "nextcloud_url": web_url,
        })
    return {"place": "nextcloud", "path": path, "entries": entries,
            "truncated": truncated, "skipped_names": skipped}


def _iso_modified(value) -> str:
    try:
        moment = parsedate_to_datetime(value).astimezone(timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return ""
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def preview_file(config, user_id: str, parts: list[str]) -> dict:
    """The viewer's preview payload for one file, read through the user's token."""
    if not parts:
        raise ListingError(404, _FILE_ERRORS[404])
    rows = _propfind(config, user_id, parts, depth="0", folder=False, errors=_FILE_ERRORS)
    row = next((r for r in rows if r["path"] == "/" + "/".join(parts)), None)
    if row is None:
        raise ListingError(404, _FILE_ERRORS[404])
    if row["is_dir"]:
        raise ListingError(400, "This is a folder, not a file.")
    head = bytearray()
    with _dav(config, user_id, parts, "GET", ok={200}, errors=_FILE_ERRORS, folder=False) as response:
        for chunk in response.iter_bytes():
            head.extend(chunk)
            if len(head) > PREVIEW_MAX_BYTES:
                break
    size = row["size"] if isinstance(row["size"], int) else len(head)
    truncated = len(head) > PREVIEW_MAX_BYTES or size > PREVIEW_MAX_BYTES
    return classify(
        parts[-1], size, _iso_modified(row["last_modified"]),
        bytes(head[:PREVIEW_MAX_BYTES]), truncated=truncated,
    )


def open_file(config, user_id: str, parts: list[str], range_header: str | None = None) -> Download:
    """Open one file's bytes for streaming to the browser.

    The caller must close the result. A single byte range is forwarded so
    media seeking and PDF range reads work; anything else is dropped and the
    whole file is sent. Nextcloud's own `Content-Type` is never passed on.
    """
    if not parts:
        raise ListingError(404, _FILE_ERRORS[404])
    # Identity, so the passed-on length and range count the bytes we send:
    # httpx would otherwise decompress a gzipped body behind them.
    headers = {"Accept-Encoding": "identity"}
    if range_header and _SINGLE_RANGE.fullmatch(range_header.strip()):
        headers["Range"] = range_header.strip()
    stack = ExitStack()
    try:
        response = stack.enter_context(_dav(
            config, user_id, parts, "GET", ok={200, 206}, errors=_FILE_ERRORS, folder=False,
            headers=headers,
        ))
        chunks = response.iter_bytes()
        head = bytearray()
        for chunk in chunks:
            head.extend(chunk)
            if len(head) >= SNIFF_BYTES:
                break
    except httpx.HTTPError:
        stack.close()
        raise ListingError(502, _UNREACHABLE) from None
    except BaseException:
        stack.close()
        raise
    inline_type = sniff_raster(bytes(head)) if response.status_code == 200 else None
    passed = {name: response.headers[name] for name in _PASSED_HEADERS if name in response.headers}

    def body():
        try:
            if head:
                yield bytes(head)
            yield from chunks
        except httpx.HTTPError:
            # Headers are already sent; ending the body short is all that is left.
            return
        finally:
            stack.close()

    return Download(response.status_code, parts[-1], inline_type, passed, body(), stack)


def download_media_type(download: Download) -> str:
    """The type a download is sent under: the sniffed raster, else a guess off the name."""
    if download.inline_type is not None:
        return download.inline_type
    return mimetypes.guess_type(download.name)[0] or "application/octet-stream"
