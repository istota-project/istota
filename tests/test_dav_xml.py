from xml.etree import ElementTree as ET

import pytest

from istota.lib.dav_xml import DavXmlError, parse_multistatus, principal_user, safe_segment


def _multistatus(*responses: str) -> bytes:
    return ('<?xml version="1.0" encoding="utf-8"?>'
            '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
            + "".join(responses) + "</d:multistatus>").encode()


def _response(href: str, props: str, status: str = "HTTP/1.1 200 OK") -> str:
    return (f"<d:response><d:href>{href}</d:href>"
            f"<d:propstat><d:prop>{props}</d:prop><d:status>{status}</d:status></d:propstat>"
            "</d:response>")


def test_parse_multistatus_keeps_only_200_propstats():
    data = _multistatus(
        _response("/a/", "<d:displayname>A</d:displayname>"),
        _response("/b/", "<oc:invite/>", status="HTTP/1.1 404 Not Found"),
    )
    rows = parse_multistatus(data)
    assert [href for href, _ in rows] == ["/a/", "/b/"]
    assert rows[0][1]["{DAV:}displayname"].text == "A"
    assert rows[1][1] == {}


@pytest.mark.parametrize("data", [
    b'<!DOCTYPE x [<!ENTITY secret "private">]><d:multistatus xmlns:d="DAV:"/>',
    b'<!ENTITY secret "private"><d:multistatus xmlns:d="DAV:"/>',
    '<?xml version="1.0" encoding="utf-16"?><d:multistatus xmlns:d="DAV:"/>'.encode("utf-16"),
    b"broken",
    b"<x/>",
])
def test_parse_multistatus_refuses_unsafe_or_foreign_bodies(data):
    with pytest.raises(DavXmlError):
        parse_multistatus(data)


@pytest.mark.parametrize("value", [
    "principal:principals/users/alice",
    "/remote.php/dav/principals/users/alice/",
    "/nextcloud/remote.php/dav/principals/users/alice/",
])
def test_principal_user_accepts_both_href_forms(value):
    assert principal_user(value) == "alice"


def test_principal_user_reads_an_element_href_or_text():
    with_href = ET.fromstring('<o xmlns:d="DAV:"><d:href>/remote.php/dav/principals/users/bob/</d:href></o>')
    with_text = ET.fromstring("<o>principal:principals/users/bob</o>")
    assert principal_user(with_href) == "bob"
    assert principal_user(with_text) == "bob"
    assert principal_user(None) is None


@pytest.mark.parametrize("value", [
    "/remote.php/dav/principals/users/alice/?x=1",
    "/remote.php/dav/principals/users/alice/#frag",
    "/remote.php/dav/principals/users/../",
    "/remote.php/dav/principals/users/%2E%2E/",
    "/remote.php/dav/principals/users/a%2Fb/",
    "principal:principals/groups/admin",
    "",
])
def test_principal_user_rejects_unsafe_or_non_user_hrefs(value):
    assert principal_user(value) is None


@pytest.mark.parametrize("value", ["contacts", "Personal Calendar", "z-app-generated--x"])
def test_safe_segment_accepts_plain_names(value):
    assert safe_segment(value)


@pytest.mark.parametrize("value", ["", "a/b", "a\\b", "a%2Fb", "a?b", "a#b", "..", "a\x00b", "a\x7fb"])
def test_safe_segment_refuses_separators_escapes_and_controls(value):
    assert not safe_segment(value)
