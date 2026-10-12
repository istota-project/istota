"""Browse a user's Nextcloud files with their web-only OAuth credential."""

from dataclasses import replace
from email.utils import parsedate_to_datetime
from pathlib import PurePosixPath
from urllib.parse import quote, urlencode, urlsplit

import httpx

from istota.nextcloud import OcsError, dav
from istota.webui import tokens
from istota.workspace.listing import LIST_MAX_ENTRIES, ListingError

_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_RECONNECT = "Nextcloud is not connected. Reconnect in Settings."


def list_folder(config, user_id: str, parts: list[str]) -> dict:
    if any(part in {".", ".."} or any(ord(c) < 32 or c == "\\" for c in part) for part in parts):
        raise ListingError(404, "Folder not found.")
    if not config.nextcloud_enabled or not tokens.feature_enabled(config):
        raise ListingError(409, _RECONNECT)
    # This credential belongs to the OAuth provider, not the bot's internal URL.
    base = config.web.oauth2_provider.rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username or parsed.query or parsed.fragment:
        raise ListingError(503, "Nextcloud is not configured.")
    if not user_id or user_id in {".", ".."} or any(c in user_id for c in "/\\") or any(ord(c) < 32 for c in user_id):
        raise ListingError(404, "Folder not found.")
    path = "/".join(parts)
    url = f"{base}/remote.php/dav/files/{quote(user_id, safe='')}/"
    if path:
        url += quote(path, safe="/") + "/"
    token = tokens.get_access_token(config.db_path, config, user_id)
    if token is None:
        raise ListingError(409, _RECONNECT)
    for attempt in range(2):
        try:
            with httpx.stream(
                "PROPFIND", url, content=dav.PROPFIND_BODY,
                headers={"Authorization": f"Bearer {token}", "Depth": "1", "Content-Type": "application/xml"},
                timeout=15, follow_redirects=False,
            ) as response:
                status = response.status_code
                if status == 207:
                    body = bytearray()
                    for chunk in response.iter_bytes():
                        body.extend(chunk)
                        if len(body) > _MAX_RESPONSE_BYTES:
                            raise ListingError(502, "This Nextcloud folder is too large to list.")
                    break
                if status != 401:
                    messages = {403: "You cannot read this Nextcloud folder.", 404: "Folder not found."}
                    raise ListingError(status if status in messages else 502,
                                       messages.get(status, "Could not load this Nextcloud folder."))
        except httpx.HTTPError:
            raise ListingError(502, "Nextcloud is not reachable right now.") from None
        if attempt == 1:
            raise ListingError(409, _RECONNECT)
        token = tokens.get_access_token(config.db_path, config, user_id, force_refresh=True)
        if token is None:
            raise ListingError(409, _RECONNECT)

    # Only the parser gets this copy. No bot credential or storage prefix is used.
    user_config = replace(config, nextcloud=replace(
        config.nextcloud, url=base, username=user_id, app_password="", dav_prefix="",
    ))
    try:
        rows = dav.parse_multistatus(user_config, body.decode("utf-8"))
    except (OcsError, UnicodeError):
        raise ListingError(502, "Could not read the Nextcloud response.") from None
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
