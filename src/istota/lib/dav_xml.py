"""Hardened parsing of DAV multistatus bodies, principal hrefs and path segments.

Shared by the contacts skill's CardDAV backend, the calendar skill and the web
process's sharing module, so all read a DAV response under one set of rules.
"""

import re
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

DAV = "{DAV:}"


class DavXmlError(Exception):
    pass


def parse_multistatus(data: bytes) -> list[tuple[str, dict[str, ET.Element]]]:
    # No DTDs/entities; DAV listings never need them. Also reject UTF-16/32 XML
    # so a declaration cannot evade this byte-level check.
    if b"\x00" in data or re.search(br"<!\s*(?:DOCTYPE|ENTITY)", data, re.IGNORECASE):
        raise DavXmlError("invalid_response")
    try:
        root = ET.fromstring(data)
    except ET.ParseError:
        raise DavXmlError("invalid_response") from None
    if root.tag != DAV + "multistatus":
        raise DavXmlError("invalid_response")
    rows = []
    for response in root.findall(DAV + "response"):
        props = {}
        for propstat in response.findall(DAV + "propstat"):
            status = (propstat.findtext(DAV + "status") or "").split()
            if len(status) < 2 or status[1] != "200":
                continue
            prop = propstat.find(DAV + "prop")
            if prop is not None:
                props.update({item.tag: item for item in prop})
        rows.append((response.findtext(DAV + "href") or "", props))
    return rows


def principal_user(prop: ET.Element | str | None) -> str | None:
    if prop is None:
        return None
    if isinstance(prop, str):
        value = prop
    else:
        value = prop.findtext(DAV + "href") or prop.text or ""
    parts = urlsplit(value.strip())
    if parts.query or parts.fragment:
        return None
    path = "/" + parts.path if parts.path.startswith("principals/users/") else parts.path
    match = re.fullmatch(r"(?:/[^/]+)*/principals/users/([^/]+)/?", path)
    if not match:
        return None
    user = unquote(match[1])
    return user if safe_segment(user) else None


_DEFAULT_PORTS = {"http": 80, "https": 443}


def _principal_path(url: str) -> tuple[tuple[str, int | None] | None, list[str]] | None:
    parts = urlsplit(url.strip())
    if parts.query or parts.fragment:
        return None
    try:
        port = parts.port
    except ValueError:
        return None
    host = None
    if parts.hostname:
        host = (parts.hostname.lower(), port or _DEFAULT_PORTS.get(parts.scheme.lower()))
    segments = [unquote(s) for s in parts.path.split("/") if s]
    if not segments or not all(safe_segment(s) for s in segments):
        return None
    return host, segments


def principal_beside(prop: ET.Element | str | None, own_principal_url: str) -> str | None:
    """The user id of a principal href laid out like ``own_principal_url``.

    For a server whose principals are not Nextcloud's ``principals/users/<id>``
    (a plain Radicale names them ``/<id>/``): the href must have the same
    host, the same parent path and the same depth as the connecting account's
    own principal, and its last segment is the id. A host-less href takes the
    principal's host.
    """
    if prop is None:
        return None
    value = prop if isinstance(prop, str) else (prop.findtext(DAV + "href") or prop.text or "")
    own = _principal_path(own_principal_url)
    named = _principal_path(value)
    if own is None or named is None:
        return None
    own_host, own_segments = own
    named_host, named_segments = named
    if named_host is not None and named_host != own_host:
        return None
    if len(named_segments) != len(own_segments) or named_segments[:-1] != own_segments[:-1]:
        return None
    return named_segments[-1]


def safe_segment(value: str) -> bool:
    # Reject nested escapes too: intermediaries must not reinterpret a separator.
    return bool(value) and not any(c in value for c in "/\\%?#") and ".." not in value and not any(ord(c) < 32 or ord(c) == 127 for c in value)
