"""`webui.nextcloud_sharing` against real Nextcloud 30, judged by what the bot sees.

The module runs unmodified in this process against the full shape's
Nextcloud. The one seam is the transport: a token row is stored for a fresh
user and `httpx.stream` is wrapped so a request carrying that token goes out
with the user's app password instead of the Bearer header, which a test
cannot mint without the OAuth flow. Every level the module sets is then
checked from the bot's side, through the contacts skill's `books` and the
calendar skill's `get_calendars_for_user`, the two places a task reads them.

Discretionary: `uv run pytest -m full -n0 tests/full/test_nextcloud_sharing.py`.
"""

from __future__ import annotations

import secrets
import uuid
from contextlib import contextmanager
from xml.etree import ElementTree as ET

import httpx
import pytest

from istota import db
from istota.config import Config, UserConfig
from istota.skills.calendar import get_caldav_client, get_calendars_for_user
from istota.skills.contacts import carddav
from istota.webui import nextcloud_sharing, tokens

pytestmark = [pytest.mark.full, pytest.mark.profile("full")]

ACCESS = "full-tier-access-token"


@pytest.fixture
def share_user(stack):
    """A fresh Nextcloud user with an app password, removed afterwards."""
    nextcloud = stack.service("nextcloud")
    nextcloud.users()  # The service retries reads while the front door comes up.
    user = f"share-{uuid.uuid4().hex[:10]}"
    password = secrets.token_urlsafe(24)
    created = nextcloud._ocs("/ocs/v2.php/cloud/users", method="POST",
                             body={"userid": user, "password": password})
    assert created.status_code in (100, 200), created.status_code
    try:
        answer = httpx.get(
            f"{nextcloud.base_url}/ocs/v2.php/core/getapppassword",
            auth=(user, password), timeout=30,
            headers={"OCS-APIRequest": "true", "Accept": "application/json"},
        )
        assert answer.status_code == 200, answer.status_code
        # The birthday calendar exists only once synced, and must be skipped.
        nextcloud._occ("dav:sync-birthday-calendar", user)
        yield nextcloud, user, answer.json()["ocs"]["data"]["apppassword"]
    finally:
        nextcloud._occ("config:app:set", "core", "shareapi_enabled", "--value=yes")
        deleted = nextcloud._ocs(f"/ocs/v2.php/cloud/users/{user}", method="DELETE")
        assert deleted.status_code in (100, 200), deleted.status_code


@pytest.fixture
def sharing(share_user, tmp_path, monkeypatch):
    """A web-process config for the user, and the Bearer-to-basic transport seam."""
    nextcloud, user, app_password = share_user
    bot = nextcloud.bot_user
    config = Config(db_path=tmp_path / "istota.db", users={user: UserConfig()})
    db.init_db(config.db_path)
    monkeypatch.setenv("ISTOTA_WEB_TOKEN_KEY", "x" * 64)
    config.web.token_storage = "encrypted"
    config.web.oauth2_provider = nextcloud.base_url
    config.nextcloud.url = nextcloud.base_url
    config.nextcloud.username = bot
    config.nextcloud.app_password = nextcloud._passwords[bot]
    tokens.store_tokens(config.db_path, user, ACCESS, "full-tier-refresh", 3600)

    real_stream = httpx.stream
    sent = []

    @contextmanager
    def stream(method, url, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        assert headers.pop("Authorization") == f"Bearer {ACCESS}"
        assert str(url).startswith(nextcloud.base_url + "/remote.php/dav/"), url
        sent.append((method, str(url)))
        with real_stream(method, url, headers=headers, auth=(user, app_password), **kwargs) as response:
            yield response

    monkeypatch.setattr(httpx, "stream", stream)
    return nextcloud, config, user, sent


def _bot_books(nextcloud, user):
    ctx = carddav.BackendContext(env={
        "CARDDAV_URL": f"{nextcloud.base_url}/remote.php/dav",
        "CARDDAV_USERNAME": nextcloud.bot_user,
        "CARDDAV_PASSWORD": nextcloud._passwords[nextcloud.bot_user],
        "ISTOTA_USER_ID": user,
    })
    # The bot sees a shared book as "<name> (<owner>)", so only writability is compared.
    return [book.writable for book in carddav.books(ctx)]


def _bot_calendars(nextcloud, user):
    client = get_caldav_client(f"{nextcloud.base_url}/remote.php/dav", nextcloud.bot_user,
                               nextcloud._passwords[nextcloud.bot_user], timeout=30)
    return [writable for _name, _url, writable in get_calendars_for_user(client, user)]


def _home_segments(nextcloud, user, app_password, home):
    """Every child segment of a home, read raw, so the skip assertions are not vacuous."""
    response = httpx.request(
        "PROPFIND", f"{nextcloud.base_url}/remote.php/dav/{home}/{user}/",
        auth=(user, app_password), timeout=30,
        headers={"Depth": "1", "Content-Type": "application/xml; charset=utf-8"},
        content=b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>',
    )
    assert response.status_code == 207, response.status_code
    hrefs = [row.findtext("{DAV:}href") or ""
             for row in ET.fromstring(response.content).findall("{DAV:}response")]
    return {href.rstrip("/").rsplit("/", 1)[-1] for href in hrefs}


def _levels(config, user):
    _kinds, collections = nextcloud_sharing.list_collections(config, user)
    return {(c.kind, c.name): c for c in collections}


def test_card_shares_are_what_the_bot_sees(sharing, share_user):
    nextcloud, config, user, sent = sharing
    _nc, _user, app_password = share_user

    # The generated collections are in the homes, and not on the card.
    calendars = _home_segments(nextcloud, user, app_password, "calendars")
    books = _home_segments(nextcloud, user, app_password, "addressbooks/users")
    assert "contact_birthdays" in calendars, calendars
    assert {"z-app-generated--contactsinteraction--recent", "z-server-generated--system"} <= books, books
    listed = _levels(config, user)
    assert set(listed) == {("calendars", "Personal"), ("contacts", "Contacts")}, listed
    assert {c.level for c in listed.values()} == {"off"}
    assert {c.segment for c in listed.values()} == {"personal", "contacts"}
    assert _bot_books(nextcloud, user) == []
    assert _bot_calendars(nextcloud, user) == []
    calendar = listed[("calendars", "Personal")].id
    book = listed[("contacts", "Contacts")].id

    # Read-only: the bot sees both, neither writable.
    result = nextcloud_sharing.set_levels(config, user, {calendar: "readonly", book: "readonly"})
    assert result.failed == []
    assert {c.level for c in result.collections} == {"readonly"}
    assert _bot_books(nextcloud, user) == [False]
    assert _bot_calendars(nextcloud, user) == [False]

    # Read and write: both writable, and the change shows on a fresh listing.
    result = nextcloud_sharing.set_levels(config, user, {calendar: "full", book: "full"})
    assert result.failed == []
    assert {c.level for c in _levels(config, user).values()} == {"full"}
    assert _bot_books(nextcloud, user) == [True]
    assert _bot_calendars(nextcloud, user) == [True]

    # No access: both gone from the bot's side.
    result = nextcloud_sharing.set_levels(config, user, {calendar: "off", book: "off"})
    assert result.failed == []
    assert _bot_books(nextcloud, user) == []
    assert _bot_calendars(nextcloud, user) == []

    # Every request went to the user's own DAV, as the user.
    assert sent and all(f"/{user}/" in url or url.endswith("/remote.php/dav/") for _m, url in sent)


def test_share_api_off_is_reported_by_the_reread(sharing):
    nextcloud, config, user, _sent = sharing
    calendar = _levels(config, user)[("calendars", "Personal")].id
    nextcloud._occ("config:app:set", "core", "shareapi_enabled", "--value=no")
    try:
        result = nextcloud_sharing.set_levels(config, user, {calendar: "readonly"})
    finally:
        nextcloud._occ("config:app:set", "core", "shareapi_enabled", "--value=yes")
    assert [(f.id, f.reason) for f in result.failed] == [
        (calendar, "Nextcloud did not apply this change."),
    ]
    assert _levels(config, user)[("calendars", "Personal")].level == "off"
    assert _bot_calendars(nextcloud, user) == []


def test_unshare_all_leaves_nothing_shared(sharing):
    nextcloud, config, user, _sent = sharing
    listed = _levels(config, user)
    result = nextcloud_sharing.set_levels(
        config, user, {listed[("calendars", "Personal")].id: "full",
                       listed[("contacts", "Contacts")].id: "readonly"},
    )
    assert result.failed == []
    assert _bot_books(nextcloud, user) and _bot_calendars(nextcloud, user)

    result = nextcloud_sharing.unshare_all(config, user)
    assert result.failed == []
    assert {c.level for c in result.collections} == {"off"}
    assert {c.level for c in _levels(config, user).values()} == {"off"}
    assert _bot_books(nextcloud, user) == []
    assert _bot_calendars(nextcloud, user) == []
