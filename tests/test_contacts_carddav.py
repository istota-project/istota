"""CardDAV tests at the HTTP boundary."""
from hashlib import sha256
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx
import pytest

from istota.config import Config, NextcloudConfig
from istota.skills.contacts import carddav
from istota.skills.contacts.model import ContactInput, ContactField

BASE = "https://cloud.example.com/remote.php/dav"
HOME = "/remote.php/dav/addressbooks/users/bot/"
BOOK = HOME + "personal_shared_by_alice/"
KEY = sha256(BOOK.encode()).hexdigest()[:12]
CARD = "BEGIN:VCARD\r\nVERSION:3.0\r\nUID:one\r\nFN:Alice Example\r\nTEL:+1 (202) 555-0100\r\nX-CUSTOM:keep\r\nEND:VCARD\r\n"


def multistatus(*responses):
    return ('<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav" xmlns:oc="http://owncloud.org/ns">' + ''.join(responses) + '</d:multistatus>')


def response(href, props):
    return f'<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop>{props}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>'


def book(href=BOOK, owner="alice", *, dav_owner=None, readonly=False, privilege="write"):
    return response(href, '<d:resourcetype><c:addressbook/></d:resourcetype><d:displayname>Personal</d:displayname>'
                    f'<oc:owner-principal>/remote.php/dav/principals/users/{owner}/</oc:owner-principal>'
                    f'<d:owner><d:href>/remote.php/dav/principals/users/{dav_owner or owner}/</d:href></d:owner>'
                    f'<oc:read-only>{str(readonly).lower()}</oc:read-only>'
                    f'<d:current-user-privilege-set><d:privilege><d:{privilege}/></d:privilege></d:current-user-privilege-set>')


def card(href=BOOK + "one.vcf", data=CARD):
    return response(href, f'<d:getetag>"v1"</d:getetag><c:address-data>{escape(data)}</c:address-data>')


@pytest.fixture
def context():
    clients = []
    def make(handler, user="alice"):
        client = httpx.Client(transport=httpx.MockTransport(handler))
        clients.append(client)
        return carddav.BackendContext(env={"CARDDAV_URL": BASE, "CARDDAV_USERNAME": "bot", "CARDDAV_PASSWORD": "test-password", "ISTOTA_USER_ID": user}, client=client)
    yield make
    for client in clients:
        client.close()


def test_config_properties_are_nextcloud_only():
    cfg = Config()
    assert cfg.carddav_url == ""
    cfg.nextcloud = NextcloudConfig(url="https://cloud.example.com/", username="bot", app_password="test-password")
    cfg.caldav.url = "https://calendar.example.com"
    assert cfg.carddav_url == BASE
    assert cfg.carddav_username == "bot"
    assert cfg.carddav_password == "test-password"


def test_discovery_owner_and_privileges(context):
    def handler(req):
        assert req.method == "PROPFIND" and req.url.path == HOME
        assert req.headers["depth"] == "1" and "authorization" in req.headers
        return httpx.Response(207, text=multistatus(book(), book(HOME + "readonly/", readonly=True), book(HOME + "bob/", "bob"), book(HOME + "z-server-generated--system/", "system"), book(HOME + "own/", "bot"), book(HOME + "forged_shared_by_alice/", "bob", dav_owner="alice")))
    books = carddav.books(context(handler))
    assert len(books) == 2
    assert [(b.key, b.writable) for b in books] == [(KEY, True), (sha256((HOME + "readonly/").encode()).hexdigest()[:12], False)]


@pytest.mark.parametrize("owner", ["alice/extra", "alice%2Fextra", "alice/../bob", "", "Alice"])
def test_owner_is_exact_principal(context, owner):
    assert carddav.books(context(lambda req: httpx.Response(207, text=multistatus(book(owner=owner))))) == []


def test_dav_owner_fallback(context):
    props = '<d:resourcetype><c:addressbook/></d:resourcetype><d:owner><d:href>/remote.php/dav/principals/users/%61lice/</d:href></d:owner>'
    books = carddav.books(context(lambda req: httpx.Response(207, text=multistatus(response(BOOK, props)))))
    assert len(books) == 1 and not books[0].writable


@pytest.mark.parametrize("href", ["https://other.example.com/book/", HOME + "../bob/private/", HOME + "%2e%2e/private/", "/remote.php/dav/addressbooks/users/bob/private/", BOOK + "?x=1"])
def test_untrusted_collection_href_is_not_used(context, href):
    calls = []
    def handler(req):
        calls.append(req)
        return httpx.Response(207, text=multistatus(book(href)))
    assert carddav.search(context(handler), "Alice", limit=20) == []
    assert len(calls) == 1


@pytest.mark.parametrize("verb", ["books", "search", "get", "create", "update", "delete"])
def test_missing_user_never_sends_request(context, verb):
    ctx = context(lambda req: pytest.fail("request without task identity"), user="")
    args = {"books": (), "search": ("Alice",), "get": ("nc:" + KEY + "/one.vcf",), "create": (KEY, ContactInput(name="Alice")), "update": ("nc:" + KEY + "/one.vcf", ContactInput(name="Alice")), "delete": ("nc:" + KEY + "/one.vcf",)}
    kwargs = {"search": {"limit": 20}, "update": {"etag": None}, "delete": {"etag": None}}
    with pytest.raises(carddav.CardDAVError, match="no_user"):
        getattr(carddav, verb)(ctx, *args[verb], **kwargs.get(verb, {}))


@pytest.mark.parametrize("contact_id", ["nc:unknown/one.vcf", "nc:" + KEY + "/%2fone.vcf", "nc:" + KEY + "/../one.vcf", "nc:" + KEY + "/%252fone.vcf", "nc:" + KEY + "/one.vcf?x=1"])
def test_bad_ids_and_other_users_are_not_found(context, contact_id):
    def handler(req):
        assert req.method == "PROPFIND"
        return httpx.Response(207, text=multistatus(book(), book(HOME + "bob/", "bob")))
    with pytest.raises(carddav.CardDAVError, match="not_found"):
        carddav.get(context(handler), contact_id)
    bob_key = sha256((HOME + "bob/").encode()).hexdigest()[:12]
    with pytest.raises(carddav.CardDAVError, match="not_found"):
        carddav.get(context(handler), f"nc:{bob_key}/one.vcf")


def test_search_matches_digits_skips_bad_cards_and_foreign_hrefs(context):
    def handler(req):
        if req.method == "PROPFIND":
            return httpx.Response(207, text=multistatus(book()))
        return httpx.Response(207, text=multistatus(card(), card(BOOK + "bad.vcf", "broken"), card(HOME + "bob/secret.vcf")))
    ctx = context(handler)
    contacts = carddav.search(ctx, "5550100", limit=20)
    assert [c.id for c in contacts] == [f"nc:{KEY}/one.vcf"]
    assert contacts[0].etag == '\"v1\"' and ctx.skipped_cards == 1


@pytest.mark.parametrize("cap", ["MAX_RESPONSE_BYTES", "MAX_CARDS"])
def test_cap_falls_back_to_filtered_query(context, monkeypatch, cap):
    monkeypatch.setattr(carddav, cap, 1800 if cap == "MAX_RESPONSE_BYTES" else 1)
    reports = []
    def handler(req):
        if req.method == "PROPFIND":
            return httpx.Response(207, text=multistatus(book()))
        reports.append(ET.fromstring(req.content))
        if len(reports) == 1:
            return httpx.Response(207, text=multistatus(card(), card(BOOK + "two.vcf", CARD + " " * 2000)))
        return httpx.Response(207, text=multistatus(card()))
    ctx = context(handler)
    assert len(carddav.search(ctx, "Alice", limit=20)) == 1 and ctx.truncated_books == [KEY]
    filters = reports[1].find('{urn:ietf:params:xml:ns:carddav}filter')
    assert filters.attrib['test'] == 'anyof'
    assert {prop.attrib['name'] for prop in filters} == {'FN', 'EMAIL', 'TEL', 'NICKNAME', 'ORG'}


def test_create_update_delete_through_http(context):
    stored = {}
    def handler(req):
        if req.method == "PROPFIND":
            return httpx.Response(207, text=multistatus(book()))
        if req.method == "PUT":
            if not stored:
                assert req.headers['if-none-match'] == '*'
                assert 'text/vcard' in req.headers['content-type']
            else:
                assert req.headers['if-match'] == '\"v1\"'
            stored['data'] = req.content.decode()
            stored['url'] = str(req.url)
            return httpx.Response(201, headers={'etag': '\"v1\"'})
        assert str(req.url) == stored['url']
        if req.method == "GET":
            return httpx.Response(200, text=stored['data'], headers={'etag': '\"v1\"'})
        assert req.method == "DELETE" and req.headers['if-match'] == '\"v1\"'
        return httpx.Response(204)
    ctx = context(handler)
    created = carddav.create(ctx, KEY, ContactInput(name="Alice Example"))
    assert created.etag == '\"v1\"'
    uid = next(line[4:] for line in stored['data'].splitlines() if line.startswith('UID:'))
    assert created.id.endswith('/' + uid + '.vcf')
    assert carddav.get(ctx, created.id).name == 'Alice Example'
    updated = carddav.update(ctx, created.id, ContactInput(add_emails=[ContactField('alice@example.com', 'work')]), etag=None)
    assert updated.emails[0].value == 'alice@example.com'
    carddav.delete(ctx, created.id, etag=None)


@pytest.mark.parametrize("verb", ["create", "update", "delete"])
def test_readonly_refuses_before_card_request(context, verb):
    def handler(req):
        assert req.method == 'PROPFIND'
        return httpx.Response(207, text=multistatus(book(readonly=True)))
    with pytest.raises(carddav.CardDAVError, match='read_only'):
        if verb == 'create':
            carddav.create(context(handler), KEY, ContactInput(name='Alice'))
        elif verb == 'update':
            carddav.update(context(handler), f'nc:{KEY}/one.vcf', ContactInput(name='Alice'), etag=None)
        else:
            carddav.delete(context(handler), f'nc:{KEY}/one.vcf', etag=None)


@pytest.mark.parametrize('status,reason', [(412, 'changed_since_read'), (403, 'read_only'), (500, 'server_error'), (429, 'rate_limited'), (302, 'unexpected_response')])
def test_status_errors_are_fixed_and_redirects_not_followed(context, status, reason):
    def handler(req):
        assert req.url.host == 'cloud.example.com'
        if req.method == 'PROPFIND':
            return httpx.Response(207, text=multistatus(book()))
        assert req.headers['if-match'] == '\"old\"'
        return httpx.Response(status, text='private server detail', headers={'location': 'https://other.example.com/'})
    with pytest.raises(carddav.CardDAVError, match=reason) as error:
        carddav.delete(context(handler), f'nc:{KEY}/one.vcf', etag='\"old\"')
    assert 'private' not in str(error.value)


@pytest.mark.parametrize('etag', [None, '"shown"'])
def test_update_preserves_properties_and_uses_selected_etag(context, etag):
    def handler(req):
        if req.method == 'PROPFIND':
            return httpx.Response(207, text=multistatus(book()))
        if req.method == 'GET':
            return httpx.Response(200, text=CARD, headers={'etag': '"current"'})
        assert req.method == 'PUT'
        assert req.headers['if-match'] == (etag or '"current"')
        assert b'X-CUSTOM:keep' in req.content and b'FN:New name' in req.content
        return httpx.Response(412, text='upstream private detail')
    with pytest.raises(carddav.CardDAVError, match='changed_since_read'):
        carddav.update(context(handler), f'nc:{KEY}/one.vcf', ContactInput(name='New name'), etag=etag)


@pytest.mark.parametrize('etag', [None, '*', 'W/"weak"'])
def test_delete_never_becomes_unconditional(context, etag):
    def handler(req):
        if req.method == 'PROPFIND':
            return httpx.Response(207, text=multistatus(book()))
        assert req.method == 'GET'
        return httpx.Response(200, text=CARD)
    with pytest.raises(carddav.CardDAVError, match='changed_since_read'):
        carddav.delete(context(handler), f'nc:{KEY}/one.vcf', etag=etag)


def test_absent_nextcloud_and_network_errors_are_fixed(context):
    ctx = context(lambda req: (_ for _ in ()).throw(httpx.ConnectError('private error')))
    assert carddav.available(ctx)
    with pytest.raises(carddav.CardDAVError, match='^unreachable$'):
        carddav.books(ctx)
    ctx.env = {'ISTOTA_USER_ID': 'alice'}
    assert not carddav.available(ctx)
    with pytest.raises(carddav.CardDAVError, match='no_contact_source'):
        carddav.books(ctx)


@pytest.mark.parametrize('data', ['broken', '<!DOCTYPE x [<!ENTITY secret "private">]><x/>', '<x/>'])
def test_bad_xml_has_fixed_error(context, data):
    with pytest.raises(carddav.CardDAVError, match='^invalid_response$'):
        carddav.books(context(lambda req: httpx.Response(207, text=data)))


def test_owner_property_failure_does_not_admit_collection(context):
    invalid = book().replace('HTTP/1.1 200 OK', 'HTTP/1.1 404 Not Found')
    empty_owner = book(owner='').replace('/remote.php/dav/principals/users//', '')
    assert carddav.books(context(lambda req: httpx.Response(207, text=multistatus(invalid, empty_owner)))) == []


def test_response_cap_stops_stream_and_falls_back(context, monkeypatch):
    monkeypatch.setattr(carddav, 'MAX_RESPONSE_BYTES', 65536)
    consumed = []
    reports = []
    class OversizedStream(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(10):
                consumed.append(index)
                yield b'x' * 65536
    def handler(req):
        if req.method == 'PROPFIND':
            return httpx.Response(207, text=multistatus(book()))
        reports.append(req)
        if len(reports) == 1:
            return httpx.Response(207, stream=OversizedStream())
        return httpx.Response(207, text=multistatus(card()))
    ctx = context(handler)
    assert len(carddav.search(ctx, 'Alice', limit=20)) == 1
    assert consumed == [0, 1] and ctx.truncated_books == [KEY]


@pytest.mark.parametrize("owner,admitted", [("alice", True), ("%61lice", True), ("bob", False), ("alice%2Fextra", False)])
def test_nextcloud_relative_owner_principal(context, owner, admitted):
    props = ('<d:resourcetype><c:addressbook/></d:resourcetype>'
             f'<oc:owner-principal>principals/users/{owner}</oc:owner-principal>'
             '<d:owner><d:href>/remote.php/dav/principals/users/alice/</d:href></d:owner>')
    books = carddav.books(context(lambda req: httpx.Response(207, text=multistatus(response(BOOK, props)))))
    assert bool(books) == admitted
