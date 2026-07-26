"""WebDAV operations the mounted filesystem cannot express.

Deliberately *not* here: read, write, mkdir, rm, mv, cp. The mount does those
with ordinary POSIX calls, and an HTTP variant would give the model two ways to
do one thing with no rule for choosing. What lives here is what the filesystem
has no way to show or do: server-side properties (file id, share types,
favorite, owner), indexed server-side search, versions, the trash bin, quota —
plus upload/download, which exist for large files, files originating outside
the mount, and rclone mode, where there is no local path at all.
"""

from __future__ import annotations

import uuid
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

from ..config import Config
from ._http import (
    DAV_TIMEOUT,
    OcsError,
    dav_files_url,
    dav_request,
    nc_base_url,
    nc_configured,
)

DAV_NS = {
    "d": "DAV:",
    "oc": "http://owncloud.org/ns",
    "nc": "http://nextcloud.org/ns",
}

#: Files at or above this size go through chunked upload when the server
#: advertises dav.chunking.
CHUNKED_UPLOAD_THRESHOLD = 10 * 1024 * 1024
CHUNK_SIZE = 10 * 1024 * 1024

_PROPS = """    <d:getlastmodified/>
    <d:getcontentlength/>
    <d:getcontenttype/>
    <d:getetag/>
    <d:resourcetype/>
    <oc:fileid/>
    <oc:permissions/>
    <oc:size/>
    <oc:owner-id/>
    <oc:owner-display-name/>
    <oc:share-types/>
    <oc:favorite/>
    <nc:has-preview/>
    <nc:mount-type/>"""

PROPFIND_BODY = f"""<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">
  <d:prop>
{_PROPS}
  </d:prop>
</d:propfind>"""

QUOTA_BODY = """<?xml version="1.0"?>
<d:propfind xmlns:d="DAV:">
  <d:prop>
    <d:quota-available-bytes/>
    <d:quota-used-bytes/>
  </d:prop>
</d:propfind>"""


# --- URL helpers ---


def _dav_base(config: Config, area: str) -> str:
    user = quote(config.nextcloud.username, safe="")
    return f"{nc_base_url(config)}/remote.php/dav/{area}/{user}"


def versions_url(config: Config, file_id: str = "") -> str:
    base = f"{_dav_base(config, 'versions')}/versions"
    return f"{base}/{quote(str(file_id), safe='')}" if file_id else base


def trash_url(config: Config, name: str = "") -> str:
    base = f"{_dav_base(config, 'trashbin')}/trash"
    return f"{base}/{quote(name, safe='')}" if name else base


def uploads_url(config: Config, upload_id: str, part: str = "") -> str:
    base = f"{_dav_base(config, 'uploads')}/{quote(upload_id, safe='')}"
    return f"{base}/{quote(part, safe='')}" if part else base


def _files_prefix(config: Config) -> str:
    return f"/remote.php/dav/files/{config.nextcloud.username}"


def href_to_path(config: Config, href: str) -> str:
    """A multistatus href back to a Nextcloud path (``/Users/alice/a.txt``)."""
    raw = unquote(urlparse(href).path)
    prefix = _files_prefix(config)
    if raw.startswith(prefix):
        raw = raw[len(prefix):]
    if not raw.startswith("/"):
        raw = "/" + raw
    return raw.rstrip("/") or "/"


# --- PROPFIND parsing ---


def _text(node: ET.Element | None) -> str:
    return (node.text or "").strip() if node is not None else ""


def _int_or_none(value: str) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def parse_multistatus(config: Config, xml_text: str) -> list[dict[str, Any]]:
    """Parse a PROPFIND/SEARCH multistatus into entry dicts.

    Missing properties are simply absent from the server's response — every
    field is read defensively rather than assumed present, because which props
    a server returns varies by version and by mount type.
    """
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as e:
        raise OcsError(f"Could not parse the WebDAV response: {e}", None, None, "dav") from e

    entries: list[dict[str, Any]] = []
    for response in root.findall("d:response", DAV_NS):
        href = _text(response.find("d:href", DAV_NS))
        prop: ET.Element | None = None
        for propstat in response.findall("d:propstat", DAV_NS):
            status = _text(propstat.find("d:status", DAV_NS))
            if "200" in status:
                prop = propstat.find("d:prop", DAV_NS)
                break
        if prop is None:
            continue

        resourcetype = prop.find("d:resourcetype", DAV_NS)
        is_dir = resourcetype is not None and resourcetype.find("d:collection", DAV_NS) is not None

        share_types_node = prop.find("oc:share-types", DAV_NS)
        share_types: list[int] = []
        if share_types_node is not None:
            for st in share_types_node:
                value = _int_or_none(_text(st))
                if value is not None:
                    share_types.append(value)

        # oc:size is the recursive size for a folder; getcontentlength is the
        # byte length of a file and absent on collections.
        size = _int_or_none(_text(prop.find("oc:size", DAV_NS)))
        if size is None:
            size = _int_or_none(_text(prop.find("d:getcontentlength", DAV_NS)))

        path = href_to_path(config, href)
        entries.append({
            "path": path,
            "name": path.rstrip("/").rsplit("/", 1)[-1],
            "is_dir": is_dir,
            "size": size,
            "content_type": _text(prop.find("d:getcontenttype", DAV_NS)),
            "last_modified": _text(prop.find("d:getlastmodified", DAV_NS)),
            "etag": _text(prop.find("d:getetag", DAV_NS)).strip('"'),
            "fileid": _text(prop.find("oc:fileid", DAV_NS)),
            "permissions": _text(prop.find("oc:permissions", DAV_NS)),
            "share_types": share_types,
            "favorite": _text(prop.find("oc:favorite", DAV_NS)) == "1",
            "owner_id": _text(prop.find("oc:owner-id", DAV_NS)),
            "owner_display_name": _text(prop.find("oc:owner-display-name", DAV_NS)),
            "has_preview": _text(prop.find("nc:has-preview", DAV_NS)) == "true",
            "mount_type": _text(prop.find("nc:mount-type", DAV_NS)),
        })
    return entries


def propfind(
    config: Config,
    path: str,
    *,
    depth: int = 0,
    timeout: float = DAV_TIMEOUT,
) -> list[dict[str, Any]]:
    resp = dav_request(
        config,
        "PROPFIND",
        dav_files_url(config, path),
        content=PROPFIND_BODY,
        headers={"Content-Type": "application/xml", "Depth": str(depth)},
        timeout=timeout,
    )
    return parse_multistatus(config, resp.text)


def stat(config: Config, path: str, timeout: float = DAV_TIMEOUT) -> dict[str, Any]:
    entries = propfind(config, path, depth=0, timeout=timeout)
    if not entries:
        raise OcsError(f"No such path: {path}", 404, None, path)
    return entries[0]


def list_dir(config: Config, path: str, *, depth: int = 1, timeout: float = DAV_TIMEOUT):
    entries = propfind(config, path, depth=depth, timeout=timeout)
    # Depth>0 includes the collection itself as the first entry; the caller
    # asked for its contents.
    target = (path or "/").rstrip("/") or "/"
    return [e for e in entries if e["path"].rstrip("/") != target]


# --- server-side SEARCH ---


def _escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def glob_to_like(pattern: str) -> str:
    """A shell-ish glob to a SQL LIKE pattern; a bare term matches anywhere."""
    if "*" not in pattern and "?" not in pattern:
        return f"%{pattern}%"
    return pattern.replace("*", "%").replace("?", "_")


def build_search_body(
    config: Config,
    *,
    scope: str,
    name: str | None = None,
    mime: str | None = None,
    min_size: int | None = None,
    modified_since: str | None = None,
    limit: int = 100,
) -> str:
    """The ``d:basicsearch`` body for a scoped server-side search."""
    conditions: list[str] = []
    if name:
        conditions.append(
            "<d:like><d:prop><d:displayname/></d:prop>"
            f"<d:literal>{_escape_xml(glob_to_like(name))}</d:literal></d:like>"
        )
    if mime:
        conditions.append(
            "<d:like><d:prop><d:getcontenttype/></d:prop>"
            f"<d:literal>{_escape_xml(glob_to_like(mime))}</d:literal></d:like>"
        )
    if min_size is not None:
        conditions.append(
            "<d:gte><d:prop><d:getcontentlength/></d:prop>"
            f"<d:literal>{int(min_size)}</d:literal></d:gte>"
        )
    if modified_since:
        conditions.append(
            "<d:gt><d:prop><d:getlastmodified/></d:prop>"
            f"<d:literal>{_escape_xml(modified_since)}</d:literal></d:gt>"
        )

    if not conditions:
        where = ""
    elif len(conditions) == 1:
        where = f"<d:where>{conditions[0]}</d:where>"
    else:
        where = "<d:where><d:and>" + "".join(conditions) + "</d:and></d:where>"

    scope_href = _escape_xml(f"{_files_prefix(config)}{scope if scope.startswith('/') else '/' + scope}")

    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<d:searchrequest xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" '
        'xmlns:nc="http://nextcloud.org/ns">\n'
        "  <d:basicsearch>\n"
        f"    <d:select><d:prop>\n{_PROPS}\n    </d:prop></d:select>\n"
        "    <d:from><d:scope>"
        f"<d:href>{scope_href}</d:href><d:depth>infinity</d:depth>"
        "</d:scope></d:from>\n"
        f"    {where}\n"
        f"    <d:limit><d:nresults>{int(limit)}</d:nresults></d:limit>\n"
        "  </d:basicsearch>\n"
        "</d:searchrequest>"
    )


def search(
    config: Config,
    *,
    scope: str,
    name: str | None = None,
    mime: str | None = None,
    min_size: int | None = None,
    modified_since: str | None = None,
    limit: int = 100,
    timeout: float = DAV_TIMEOUT,
) -> list[dict[str, Any]]:
    """Server-side, indexed search. A `find` over the FUSE mount walks the
    network filesystem and is unusably slow on a large tree; this is why the
    verb exists."""
    body = build_search_body(
        config,
        scope=scope,
        name=name,
        mime=mime,
        min_size=min_size,
        modified_since=modified_since,
        limit=limit,
    )
    resp = dav_request(
        config,
        "SEARCH",
        f"{nc_base_url(config)}/remote.php/dav/",
        content=body,
        headers={"Content-Type": "text/xml"},
        timeout=timeout,
    )
    return parse_multistatus(config, resp.text)


# --- upload / download ---


def upload(
    config: Config,
    local_path: Path,
    remote_path: str,
    *,
    chunked: bool | None = None,
    supports_chunking: bool = True,
    timeout: float = DAV_TIMEOUT,
) -> dict[str, Any]:
    """Upload a local file, chunking large ones when the server supports it."""
    local = Path(local_path)
    if not local.is_file():
        raise OcsError(f"No such local file: {local}", None, None, str(local))

    size = local.stat().st_size
    want_chunks = chunked if chunked is not None else size >= CHUNKED_UPLOAD_THRESHOLD
    use_chunks = want_chunks and supports_chunking

    if use_chunks:
        _upload_chunked(config, local, remote_path, timeout=timeout)
        method = "chunked"
    else:
        dav_request(
            config,
            "PUT",
            dav_files_url(config, remote_path),
            content=local.read_bytes(),
            headers={"Content-Type": "application/octet-stream"},
            timeout=timeout,
        )
        method = "plain"

    return {
        "status": "ok",
        "path": remote_path,
        "bytes": size,
        "method": method,
        # A server without dav.chunking gets the plain PUT rather than a failure.
        "chunking_available": supports_chunking,
    }


def _upload_chunked(
    config: Config, local: Path, remote_path: str, *, timeout: float
) -> None:
    """Chunked upload v2: a temp collection, per-chunk PUTs, a final MOVE."""
    upload_id = uuid.uuid4().hex
    destination = dav_files_url(config, remote_path)

    dav_request(
        config,
        "MKCOL",
        uploads_url(config, upload_id),
        timeout=timeout,
        ok_statuses=(201, 405),
    )
    try:
        index = 1
        with local.open("rb") as fh:
            while True:
                chunk = fh.read(CHUNK_SIZE)
                if not chunk:
                    break
                dav_request(
                    config,
                    "PUT",
                    uploads_url(config, upload_id, str(index)),
                    content=chunk,
                    headers={"Content-Type": "application/octet-stream"},
                    timeout=timeout,
                )
                index += 1

        dav_request(
            config,
            "MOVE",
            uploads_url(config, upload_id, ".file"),
            headers={"Destination": destination, "Overwrite": "T"},
            timeout=timeout,
        )
    except Exception:
        # Leave nothing behind on the server when the assembly fails.
        try:
            dav_request(
                config,
                "DELETE",
                uploads_url(config, upload_id),
                timeout=timeout,
                ok_statuses=(204, 404),
            )
        except Exception:
            pass
        raise


def download(
    config: Config,
    remote_path: str,
    local_path: Path,
    timeout: float = DAV_TIMEOUT,
) -> dict[str, Any]:
    resp = dav_request(
        config, "GET", dav_files_url(config, remote_path), timeout=timeout
    )
    local = Path(local_path)
    local.parent.mkdir(parents=True, exist_ok=True)
    content = resp.content if isinstance(resp.content, bytes) else str(resp.text).encode()
    local.write_bytes(content)
    return {"status": "ok", "path": remote_path, "local": str(local), "bytes": len(content)}


# --- versions ---


def versions(config: Config, path: str, timeout: float = DAV_TIMEOUT) -> dict[str, Any]:
    entry = stat(config, path, timeout=timeout)
    file_id = entry.get("fileid", "")
    if not file_id:
        raise OcsError(
            f"Could not resolve a file id for {path}; versions are keyed on it",
            None,
            None,
            path,
        )

    resp = dav_request(
        config,
        "PROPFIND",
        versions_url(config, file_id),
        content=PROPFIND_BODY,
        headers={"Content-Type": "application/xml", "Depth": "1"},
        timeout=timeout,
    )
    raw = parse_multistatus(config, resp.text)
    listed = [e for e in raw if e["name"] and e["name"] != str(file_id)]
    return {
        "path": path,
        "fileid": file_id,
        "versions": [
            {
                "version": e["name"],
                "size": e["size"],
                "last_modified": e["last_modified"],
                "content_type": e["content_type"],
            }
            for e in listed
        ],
    }


def restore_version(
    config: Config, path: str, version: str, timeout: float = DAV_TIMEOUT
) -> dict[str, Any]:
    entry = stat(config, path, timeout=timeout)
    file_id = entry.get("fileid", "")
    if not file_id:
        raise OcsError(f"Could not resolve a file id for {path}", None, None, path)

    source = f"{versions_url(config, file_id)}/{quote(version, safe='')}"
    destination = f"{_dav_base(config, 'versions')}/restore/target"
    dav_request(
        config,
        "MOVE",
        source,
        headers={"Destination": destination},
        timeout=timeout,
    )
    return {"status": "ok", "path": path, "restored_version": version}


# --- trash ---


def trash_list(config: Config, timeout: float = DAV_TIMEOUT) -> list[dict[str, Any]]:
    resp = dav_request(
        config,
        "PROPFIND",
        trash_url(config),
        content=PROPFIND_BODY,
        headers={"Content-Type": "application/xml", "Depth": "1"},
        timeout=timeout,
    )
    entries = parse_multistatus(config, resp.text)
    return [e for e in entries if e["name"] and e["name"] != "trash"]


def trash_restore(config: Config, name: str, timeout: float = DAV_TIMEOUT) -> dict[str, Any]:
    destination = f"{_dav_base(config, 'trashbin')}/restore/target"
    dav_request(
        config,
        "MOVE",
        trash_url(config, name),
        headers={"Destination": destination},
        timeout=timeout,
    )
    return {"status": "ok", "restored": name}


def trash_empty(config: Config, timeout: float = DAV_TIMEOUT) -> dict[str, Any]:
    dav_request(config, "DELETE", trash_url(config), timeout=timeout)
    return {"status": "ok", "emptied": True}


# --- favorites ---


def set_favorite(
    config: Config, path: str, favorite: bool = True, timeout: float = DAV_TIMEOUT
) -> dict[str, Any]:
    body = (
        '<?xml version="1.0"?>\n'
        '<d:propertyupdate xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">\n'
        f"  <d:set><d:prop><oc:favorite>{1 if favorite else 0}</oc:favorite></d:prop></d:set>\n"
        "</d:propertyupdate>"
    )
    dav_request(
        config,
        "PROPPATCH",
        dav_files_url(config, path),
        content=body,
        headers={"Content-Type": "application/xml"},
        timeout=timeout,
    )
    return {"status": "ok", "path": path, "favorite": favorite}


# --- quota ---


def quota(config: Config, timeout: float = DAV_TIMEOUT) -> dict[str, Any]:
    if not nc_configured(config):
        raise OcsError("Nextcloud is not configured", None, None, "quota")

    resp = dav_request(
        config,
        "PROPFIND",
        dav_files_url(config, ""),
        content=QUOTA_BODY,
        headers={"Content-Type": "application/xml", "Depth": "0"},
        timeout=timeout,
    )
    try:
        root = ET.fromstring(resp.text)
    except ET.ParseError as e:
        raise OcsError(f"Could not parse the quota response: {e}", None, None, "quota") from e

    available = _int_or_none(_text(root.find(".//d:quota-available-bytes", DAV_NS)))
    used = _int_or_none(_text(root.find(".//d:quota-used-bytes", DAV_NS)))
    total = None
    # A negative "available" is Nextcloud's sentinel for unlimited/unknown.
    if available is not None and used is not None and available >= 0:
        total = available + used
    return {"used_bytes": used, "available_bytes": available, "total_bytes": total}
