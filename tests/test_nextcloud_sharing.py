"""Nextcloud calendar and address-book sharing over the user's own token.

A fake Nextcloud stands behind `httpx.stream`, the boundary
`nextcloud_files.dav_request` sends through. It answers the
`current-user-principal` lookup, the two home listings and the `oc:share`
POSTs from its own state, so a write is judged by the same re-read the
module does.
"""

import re
from contextlib import contextmanager
from hashlib import sha256
from urllib.parse import unquote
from xml.etree import ElementTree as ET
from xml.sax.saxutils import escape

import httpx
import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.webui import nextcloud_sharing, tokens
from istota.workspace.listing import ListingError

PREFIX = "/nc/remote.php/dav"
CAL_HOME = f"{PREFIX}/calendars/Alice/"
BOOK_HOME = f"{PREFIX}/addressbooks/users/Alice/"


def _id(href):
    return sha256(href.encode()).hexdigest()[:12]


class FakeNextcloud:
    def __init__(self):
        self.uid = "Alice"
        self.calendars = {
            "personal": {"name": "Personal", "comps": ["VEVENT", "VTODO"], "shares": {}},
            "work": {"name": "Work", "comps": ["VEVENT"], "shares": {"istota": "read-write"}},
            "journal": {"name": "Journal", "comps": ["VJOURNAL"], "shares": {}},
            "contact_birthdays": {"name": "Contact birthdays", "comps": ["VEVENT"], "shares": {}},
            "app-generated--deck--board-1": {"name": "Deck", "comps": ["VTODO"], "shares": {}},
        }
        self.books = {
            "contacts": {"name": "Contacts", "shares": {"istota": "read", "friends": "read-write"}},
        }
        self.extra_calendar_xml = ""
        self.extra_book_xml = ""
        self.ignore_shares = False
        self.post_status = {}
        self.requests = []

    def handle(self, request):
        self.requests.append(request)
        path = unquote(request.url.path)
        if request.headers.get("authorization") != "Bearer alice-access":
            return httpx.Response(401)
        if request.method == "PROPFIND" and path == f"{PREFIX}/":
            return httpx.Response(207, text=self._principal())
        if request.method == "PROPFIND" and path == CAL_HOME:
            return httpx.Response(207, text=self._calendars())
        if request.method == "PROPFIND" and path == BOOK_HOME:
            return httpx.Response(207, text=self._books())
        if request.method == "POST":
            return self._share(path, request)
        return httpx.Response(404)

    def _principal(self):
        return f"""<d:multistatus xmlns:d="DAV:"><d:response><d:href>{PREFIX}/</d:href>
          <d:propstat><d:prop><d:current-user-principal><d:href>{PREFIX}/principals/users/{self.uid}/</d:href>
          </d:current-user-principal></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"""

    @staticmethod
    def _invite(shares):
        users = "".join(
            f"<oc:user><d:href>principal:principals/{'groups' if who == 'friends' else 'users'}/{escape(who)}</d:href>"
            f"<oc:common-name>{escape(who)}</oc:common-name><oc:invite-accepted/>"
            f"<oc:access><oc:{access}/></oc:access></oc:user>"
            for who, access in shares.items()
        )
        return f"<oc:invite>{users}</oc:invite>"

    @staticmethod
    def _ok(props, missing=""):
        out = f"<d:propstat><d:prop>{props}</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat>"
        if missing:
            out += f"<d:propstat><d:prop>{missing}</d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat>"
        return out

    def _calendars(self):
        rows = [f"<d:response><d:href>{CAL_HOME}</d:href>{self._ok('<d:resourcetype><d:collection/></d:resourcetype>', '<oc:invite/>')}</d:response>"]
        for segment, cal in self.calendars.items():
            comps = "".join(f'<cal:comp name="{c}"/>' for c in cal["comps"])
            props = (
                f"<d:resourcetype><d:collection/><cal:calendar/></d:resourcetype><d:displayname>{cal['name']}</d:displayname>"
                f"<oc:owner-principal>principals/users/{self.uid}</oc:owner-principal>"
                f"<d:owner><d:href>{PREFIX}/principals/users/{self.uid}/</d:href></d:owner>"
                f"<cal:supported-calendar-component-set>{comps}</cal:supported-calendar-component-set>"
                + self._invite(cal["shares"])
            )
            rows.append(f"<d:response><d:href>{CAL_HOME}{segment}/</d:href>{self._ok(props)}</d:response>")
        return self._multistatus("".join(rows) + self.extra_calendar_xml)

    def _books(self):
        rows = [f"<d:response><d:href>{BOOK_HOME}</d:href>{self._ok('<d:resourcetype><d:collection/></d:resourcetype>', '<oc:invite/>')}</d:response>"]
        for segment, book in self.books.items():
            # Address books carry only DAV:owner, as Nextcloud 30 sends them.
            props = (
                f"<d:resourcetype><d:collection/><card:addressbook/></d:resourcetype><d:displayname>{book['name']}</d:displayname>"
                f"<d:owner><d:href>{PREFIX}/principals/users/{self.uid}/</d:href></d:owner>"
                + self._invite(book["shares"])
            )
            rows.append(f"<d:response><d:href>{BOOK_HOME}{segment}/</d:href>{self._ok(props)}</d:response>")
        return self._multistatus("".join(rows) + self.extra_book_xml)

    @staticmethod
    def _multistatus(body):
        return (
            '<d:multistatus xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" '
            'xmlns:cal="urn:ietf:params:xml:ns:caldav" xmlns:card="urn:ietf:params:xml:ns:carddav" '
            f'xmlns:cs="http://calendarserver.org/ns/">{body}</d:multistatus>'
        )

    def _share(self, path, request):
        if not request.headers.get("content-type", "").startswith("application/xml"):
            return httpx.Response(501)
        for home, store in ((CAL_HOME, self.calendars), (BOOK_HOME, self.books)):
            if path.startswith(home):
                segment = path[len(home):].rstrip("/")
                break
        else:
            return httpx.Response(404)
        if segment in self.post_status:
            return httpx.Response(self.post_status[segment])
        if segment not in store:
            return httpx.Response(404)
        root = ET.fromstring(request.content)
        if self.ignore_shares:
            return httpx.Response(200)
        oc = "{http://owncloud.org/ns}"
        for change in root:
            href = change.findtext("{DAV:}href")
            who = href.rsplit("/", 1)[-1]
            if change.tag == oc + "set":
                store[segment]["shares"][who] = "read-write" if change.find(oc + "read-write") is not None else "read"
            elif change.tag == oc + "remove":
                store[segment]["shares"].pop(who, None)
        return httpx.Response(200)


@pytest.fixture
def config(tmp_path, monkeypatch):
    config = Config(db_path=tmp_path / "istota.db", users={"alice": UserConfig()})
    db.init_db(config.db_path)
    monkeypatch.setenv("ISTOTA_WEB_TOKEN_KEY", "x" * 64)
    config.web.token_storage = "encrypted"
    config.web.oauth2_provider = "https://cloud.example.com/nc"
    config.nextcloud.url = "https://internal.example.com"
    config.nextcloud.username = "istota"
    config.nextcloud.app_password = "bot-password"
    tokens.store_tokens(config.db_path, "alice", "alice-access", "alice-refresh", 3600)
    return config


@pytest.fixture
def nextcloud(monkeypatch):
    fake = FakeNextcloud()

    @contextmanager
    def stream(method, url, **kwargs):
        with httpx.Client(transport=httpx.MockTransport(fake.handle)) as client:
            with client.stream(method, url, **kwargs) as response:
                yield response

    monkeypatch.setattr(httpx, "stream", stream)
    return fake


def _by_name(collections):
    return {c.name: c for c in collections}


def _posts(fake):
    return [r for r in fake.requests if r.method == "POST"]


class TestListing:
    def test_lists_own_shareable_collections_with_levels(self, config, nextcloud):
        kinds, collections = nextcloud_sharing.list_collections(config, "alice")
        found = {(c.kind, c.name, c.level) for c in collections}
        assert found == {
            ("calendars", "Personal", "off"),
            ("calendars", "Work", "full"),
            ("contacts", "Contacts", "readonly"),
        }
        work = _by_name(collections)["Work"]
        assert work.id == _id(f"{CAL_HOME}work/")
        assert work.segment == "work"
        assert kinds["calendars"].ceiling == "full" and kinds["calendars"].available
        assert kinds["contacts"].ceiling == "full" and kinds["contacts"].available

    def test_requests_go_to_the_canonical_homes_with_the_user_token(self, config, nextcloud):
        nextcloud_sharing.list_collections(config, "alice")
        first, *rest = nextcloud.requests
        assert str(first.url) == "https://cloud.example.com/nc/remote.php/dav/"
        assert first.headers["depth"] == "0"
        assert b"current-user-principal" in first.content
        assert {r.url.path for r in rest} == {CAL_HOME, BOOK_HOME}
        assert all(r.headers["depth"] == "1" for r in rest)
        assert all(r.headers["authorization"] == "Bearer alice-access" for r in nextcloud.requests)
        assert all("bot-password" not in str(r.headers) for r in nextcloud.requests)

    def test_principal_not_resolving_to_the_session_user_is_a_reconnect(self, config, nextcloud):
        nextcloud.uid = "mallory"
        with pytest.raises(ListingError) as caught:
            nextcloud_sharing.list_collections(config, "alice")
        assert caught.value.status == 409
        assert "Reconnect" in caught.value.message
        assert len(nextcloud.requests) == 1

    def test_principal_resolves_for_an_unconfigured_user_by_exact_spelling(self, config, nextcloud):
        # The OAuth callback keeps the Nextcloud spelling for a user not in config.
        config.users = {}
        tokens.store_tokens(config.db_path, "Alice", "alice-access", "alice-refresh", 3600)
        _, collections = nextcloud_sharing.list_collections(config, "Alice")
        assert {c.name for c in collections} == {"Personal", "Work", "Contacts"}

    def test_filters_what_cannot_be_shared(self, config, nextcloud):
        other = f"{PREFIX}/principals/users/bob/"
        nextcloud.extra_calendar_xml = (
            # A subscription, a calendar shared with Alice by someone else,
            # one with no 200 invite, and hrefs failing the segment or parent check.
            f"<d:response><d:href>{CAL_HOME}sub/</d:href>{FakeNextcloud._ok('<d:resourcetype><d:collection/><cs:subscribed/></d:resourcetype><oc:invite/>')}</d:response>"
            f"<d:response><d:href>{CAL_HOME}team_shared_by_bob/</d:href>{FakeNextcloud._ok(f'<d:resourcetype><cal:calendar/></d:resourcetype><oc:owner-principal>principals/users/bob</oc:owner-principal><d:owner><d:href>{other}</d:href></d:owner><oc:invite/>')}</d:response>"
            f"<d:response><d:href>{CAL_HOME}inbox/</d:href>{FakeNextcloud._ok('<d:resourcetype><cal:calendar/></d:resourcetype><oc:owner-principal>principals/users/Alice</oc:owner-principal>', '<oc:invite/>')}</d:response>"
            f"<d:response><d:href>{CAL_HOME}a%2Fb/</d:href>{FakeNextcloud._ok('<d:resourcetype><cal:calendar/></d:resourcetype><oc:owner-principal>principals/users/Alice</oc:owner-principal><oc:invite/>')}</d:response>"
            f"<d:response><d:href>{PREFIX}/calendars/bob/stolen/</d:href>{FakeNextcloud._ok('<d:resourcetype><cal:calendar/></d:resourcetype><oc:owner-principal>principals/users/Alice</oc:owner-principal><oc:invite/>')}</d:response>"
            f"<d:response><d:href>{CAL_HOME}deep/nested/</d:href>{FakeNextcloud._ok('<d:resourcetype><cal:calendar/></d:resourcetype><oc:owner-principal>principals/users/Alice</oc:owner-principal><oc:invite/>')}</d:response>"
        )
        nextcloud.extra_book_xml = (
            f"<d:response><d:href>{BOOK_HOME}z-app-generated--contactsinteraction--recent/</d:href>{FakeNextcloud._ok(f'<d:resourcetype><card:addressbook/></d:resourcetype><d:owner><d:href>{PREFIX}/principals/users/Alice/</d:href></d:owner>', '<oc:invite/>')}</d:response>"
            f"<d:response><d:href>{BOOK_HOME}z-server-generated--system/</d:href>{FakeNextcloud._ok('<d:resourcetype><card:addressbook/></d:resourcetype><d:owner><d:href>/nc/remote.php/dav/principals/system/system/</d:href></d:owner><oc:invite/>')}</d:response>"
            f"<d:response><d:href>{BOOK_HOME}z-app-generated--other/</d:href>{FakeNextcloud._ok(f'<d:resourcetype><card:addressbook/></d:resourcetype><d:owner><d:href>{PREFIX}/principals/users/Alice/</d:href></d:owner><oc:invite/>')}</d:response>"
        )
        _, collections = nextcloud_sharing.list_collections(config, "alice")
        assert {c.name for c in collections} == {"Personal", "Work", "Contacts"}

    def test_group_sharees_are_ignored(self, config, nextcloud):
        nextcloud.books["contacts"]["shares"] = {"friends": "read-write"}
        _, collections = nextcloud_sharing.list_collections(config, "alice")
        assert _by_name(collections)["Contacts"].level == "off"

    def test_ceiling_and_availability_per_kind(self, config, nextcloud):
        config.nextcloud.sharing.calendars = "readonly"
        config.caldav.url = "https://caldav.example.com"
        kinds, collections = nextcloud_sharing.list_collections(config, "alice")
        assert kinds["calendars"].ceiling == "readonly" and not kinds["calendars"].available
        # Listed anyway, so existing shares stay removable.
        assert "Work" in _by_name(collections)
        config.caldav.url = ""
        config.caldav.username = "someone-else"
        assert not nextcloud_sharing.list_collections(config, "alice")[0]["calendars"].available
        config.caldav.username = "istota"
        assert nextcloud_sharing.list_collections(config, "alice")[0]["calendars"].available

    def test_no_bot_account_lists_nothing(self, config, nextcloud):
        config.nextcloud.username = ""
        kinds, collections = nextcloud_sharing.list_collections(config, "alice")
        assert collections == [] and not nextcloud.requests
        assert not kinds["calendars"].available and not kinds["contacts"].available

    def test_session_user_is_the_bot(self, config, nextcloud):
        kinds, collections = nextcloud_sharing.list_collections(config, "istota")
        assert collections == [] and not nextcloud.requests
        with pytest.raises(ListingError) as caught:
            nextcloud_sharing.set_levels(config, "istota", {_id(f"{CAL_HOME}work/"): "off"})
        assert caught.value.status == 400

    def test_oversized_and_malformed_listings_fail_whole(self, config, nextcloud, monkeypatch):
        from istota.webui import nextcloud_files
        nextcloud.extra_calendar_xml = "<d:response><d:href>" + "x" * 4096 + "</d:href></d:response>"
        monkeypatch.setattr(nextcloud_files, "_MAX_RESPONSE_BYTES", 2048)
        with pytest.raises(ListingError) as caught:
            nextcloud_sharing.list_collections(config, "alice")
        assert caught.value.status == 502
        assert "too many calendars or address books" in caught.value.message
        monkeypatch.setattr(nextcloud_files, "_MAX_RESPONSE_BYTES", 8 * 1024 * 1024)
        nextcloud.extra_calendar_xml = '<!DOCTYPE x [<!ENTITY a "b">]>'
        with pytest.raises(ListingError) as caught:
            nextcloud_sharing.list_collections(config, "alice")
        assert caught.value.status == 502
        assert caught.value.message == "Could not read the Nextcloud response."

    def test_401_is_retried_once_on_a_refreshed_token(self, config, nextcloud, monkeypatch):
        tokens.store_tokens(config.db_path, "alice", "stale", "alice-refresh", 3600)

        def refresh(url, **kwargs):
            assert kwargs["data"]["refresh_token"] == "alice-refresh"
            return httpx.Response(200, json={"access_token": "alice-access", "refresh_token": "rotated", "expires_in": 3600})

        monkeypatch.setattr(httpx, "post", refresh)
        _, collections = nextcloud_sharing.list_collections(config, "alice")
        assert len(collections) == 3
        assert nextcloud.requests[0].headers["authorization"] == "Bearer stale"
        assert nextcloud.requests[1].headers["authorization"] == "Bearer alice-access"

    def test_no_token_is_a_reconnect(self, config, nextcloud):
        tokens.delete_tokens(config.db_path, "alice")
        with pytest.raises(ListingError) as caught:
            nextcloud_sharing.list_collections(config, "alice")
        assert caught.value.status == 409
        assert not nextcloud.requests


class TestSetLevels:
    def test_sends_exact_share_bodies_and_rereads(self, config, nextcloud):
        personal, contacts, work = _id(f"{CAL_HOME}personal/"), _id(f"{BOOK_HOME}contacts/"), _id(f"{CAL_HOME}work/")
        result = nextcloud_sharing.set_levels(config, "alice", {personal: "full", contacts: "full", work: "off"})
        assert result.failed == []
        assert {c.name: c.level for c in result.collections} == {"Personal": "full", "Work": "off", "Contacts": "full"}
        posts = {r.url.path: r for r in _posts(nextcloud)}
        assert set(posts) == {f"{CAL_HOME}personal/", f"{BOOK_HOME}contacts/", f"{CAL_HOME}work/"}
        for request in posts.values():
            assert request.headers["content-type"] == "application/xml; charset=utf-8"
            assert request.headers["authorization"] == "Bearer alice-access"
        assert posts[f"{CAL_HOME}personal/"].content.decode() == (
            '<oc:share xmlns:oc="http://owncloud.org/ns" xmlns:d="DAV:"><oc:set>'
            "<d:href>principal:principals/users/istota</d:href><oc:read-write/></oc:set></oc:share>"
        )
        assert posts[f"{CAL_HOME}work/"].content.decode() == (
            '<oc:share xmlns:oc="http://owncloud.org/ns" xmlns:d="DAV:"><oc:remove>'
            "<d:href>principal:principals/users/istota</d:href></oc:remove></oc:share>"
        )
        assert nextcloud.calendars["work"]["shares"] == {}
        assert nextcloud.books["contacts"]["shares"] == {"istota": "read-write", "friends": "read-write"}

    def test_readonly_body_and_escaped_bot(self, config, nextcloud):
        config.nextcloud.username = "bot&<x>"
        nextcloud_sharing.set_levels(config, "alice", {_id(f"{CAL_HOME}personal/"): "readonly"})
        (post,) = _posts(nextcloud)
        assert post.content.decode() == (
            '<oc:share xmlns:oc="http://owncloud.org/ns" xmlns:d="DAV:"><oc:set>'
            "<d:href>principal:principals/users/bot&amp;&lt;x&gt;</d:href><oc:read/></oc:set></oc:share>"
        )

    def test_an_unchanged_level_sends_nothing(self, config, nextcloud):
        result = nextcloud_sharing.set_levels(config, "alice", {_id(f"{CAL_HOME}work/"): "full"})
        assert result.failed == [] and not _posts(nextcloud)

    @pytest.mark.parametrize("changes,status", [
        ({"not-an-id": "full"}, 400),
        ({_id(f"{CAL_HOME}personal/"): "write"}, 400),
    ])
    def test_malformed_requests_are_refused_and_nothing_is_sent(self, config, nextcloud, changes, status):
        with pytest.raises(ListingError) as caught:
            nextcloud_sharing.set_levels(config, "alice", {**changes, _id(f"{BOOK_HOME}contacts/"): "off"})
        assert caught.value.status == status
        assert not _posts(nextcloud)

    def test_a_raise_above_the_ceiling_is_refused_and_nothing_is_sent(self, config, nextcloud):
        config.nextcloud.sharing.calendars = "readonly"
        with pytest.raises(ListingError) as caught:
            nextcloud_sharing.set_levels(config, "alice", {
                _id(f"{CAL_HOME}personal/"): "full", _id(f"{BOOK_HOME}contacts/"): "off",
            })
        assert caught.value.status == 403
        assert not _posts(nextcloud)

    def test_a_raise_into_an_unavailable_kind_is_refused(self, config, nextcloud):
        config.caldav.url = "https://caldav.example.com"
        with pytest.raises(ListingError) as caught:
            nextcloud_sharing.set_levels(config, "alice", {_id(f"{CAL_HOME}personal/"): "readonly"})
        assert caught.value.status == 403
        assert not _posts(nextcloud)

    def test_lowering_above_the_ceiling_or_in_an_unavailable_kind_is_allowed(self, config, nextcloud):
        config.nextcloud.sharing.calendars = "off"
        config.caldav.url = "https://caldav.example.com"
        work = _id(f"{CAL_HOME}work/")
        result = nextcloud_sharing.set_levels(config, "alice", {work: "readonly"})
        assert result.failed == []
        assert _by_name(result.collections)["Work"].level == "readonly"
        result = nextcloud_sharing.set_levels(config, "alice", {work: "off"})
        assert _by_name(result.collections)["Work"].level == "off"

    def test_a_200_that_was_not_applied_is_a_failure(self, config, nextcloud):
        nextcloud.ignore_shares = True
        personal = _id(f"{CAL_HOME}personal/")
        result = nextcloud_sharing.set_levels(config, "alice", {personal: "full"})
        assert [(f.id, f.name, f.reason) for f in result.failed] == [
            (personal, "Personal", "Nextcloud did not apply this change."),
        ]
        assert _by_name(result.collections)["Personal"].level == "off"

    @pytest.mark.parametrize("status,reason", [
        (403, "Nextcloud did not allow this share."),
        (404, "This calendar or address book no longer exists."),
        (500, "Nextcloud did not accept the change."),
        (501, "Nextcloud did not accept the change."),
    ])
    def test_a_refused_post_is_a_failure_and_the_rest_proceed(self, config, nextcloud, status, reason):
        nextcloud.post_status["personal"] = status
        personal, contacts = _id(f"{CAL_HOME}personal/"), _id(f"{BOOK_HOME}contacts/")
        result = nextcloud_sharing.set_levels(config, "alice", {personal: "full", contacts: "off"})
        assert [(f.id, f.reason) for f in result.failed] == [(personal, reason)]
        assert _by_name(result.collections)["Contacts"].level == "off"

    def test_a_collection_gone_since_the_load_is_a_failure(self, config, nextcloud):
        gone, contacts = _id(f"{CAL_HOME}deleted/"), _id(f"{BOOK_HOME}contacts/")
        result = nextcloud_sharing.set_levels(config, "alice", {gone: "full", contacts: "off"})
        assert [(f.id, f.reason) for f in result.failed] == [(gone, "This calendar or address book no longer exists.")]
        assert [r.url.path for r in _posts(nextcloud)] == [f"{BOOK_HOME}contacts/"]

    def test_a_401_on_the_post_is_retried_on_a_refreshed_token(self, config, nextcloud, monkeypatch):
        original = nextcloud.handle
        seen = []

        def handle(request):
            if request.method == "POST" and not seen:
                seen.append(request)
                return httpx.Response(401)
            return original(request)

        nextcloud.handle = handle
        monkeypatch.setattr(httpx, "post", lambda url, **kw: httpx.Response(
            200, json={"access_token": "alice-access", "refresh_token": "rotated", "expires_in": 3600}))
        result = nextcloud_sharing.set_levels(config, "alice", {_id(f"{CAL_HOME}personal/"): "readonly"})
        assert result.failed == []
        assert nextcloud.calendars["personal"]["shares"] == {"istota": "read"}


class TestUnshareAll:
    def test_removes_every_direct_share_across_both_kinds_regardless_of_ceiling(self, config, nextcloud):
        config.nextcloud.sharing.calendars = "off"
        config.nextcloud.sharing.contacts = "off"
        config.caldav.url = "https://caldav.example.com"
        result = nextcloud_sharing.unshare_all(config, "alice")
        assert result.failed == []
        assert all(c.level == "off" for c in result.collections)
        assert {r.url.path for r in _posts(nextcloud)} == {f"{CAL_HOME}work/", f"{BOOK_HOME}contacts/"}
        assert all(re.search(rb"<oc:remove>", r.content) for r in _posts(nextcloud))
        # The group share is the user's own arrangement and stays.
        assert nextcloud.books["contacts"]["shares"] == {"friends": "read-write"}

    def test_reports_what_is_left_shared(self, config, nextcloud):
        nextcloud.ignore_shares = True
        result = nextcloud_sharing.unshare_all(config, "alice")
        assert {f.name for f in result.failed} == {"Work", "Contacts"}
