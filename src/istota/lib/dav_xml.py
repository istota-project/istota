"""Hardened parsing of DAV multistatus bodies, principal hrefs and path segments.

Shared by the contacts skill's CardDAV backend and the web process's sharing
module, so both read a Nextcloud response under one set of rules.
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


def safe_segment(value: str) -> bool:
    # Reject nested escapes too: intermediaries must not reinterpret a separator.
    return bool(value) and not any(c in value for c in "/\\%?#") and ".." not in value and not any(ord(c) < 32 or ord(c) == 127 for c in value)
