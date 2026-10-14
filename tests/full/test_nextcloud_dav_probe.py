"""What Nextcloud 30 does with DAV collection sharing, observed rather than assumed.

The Nextcloud connection card lists a user's calendars and address books with
their token and creates or removes the DAV shares to the bot. Its filters, its
success rule and its request shape rest on server facts, and this file is where
they are witnessed: raw `httpx` against the full shape's real Nextcloud, as a
fresh user signed in with an app password, with nothing from `src/istota` in
the loop.

Every fact is collected before anything is asserted and recorded as the
`nextcloud_dav_facts` property, so one run with `--junitxml` keeps the whole
picture even when an assertion fails. `.claude/rules/web-ui.md` ("Nextcloud DAV
sharing, as observed") is the written-down copy.

Discretionary: `uv run pytest -m full -n0 tests/full/test_nextcloud_dav_probe.py`.
"""

from __future__ import annotations

import json
import secrets
import uuid
from xml.etree import ElementTree as ET

import httpx
import pytest

pytestmark = [pytest.mark.full, pytest.mark.profile("full")]

DAV = "{DAV:}"
OC = "{http://owncloud.org/ns}"
CAL = "{urn:ietf:params:xml:ns:caldav}"

XML = {"Content-Type": "application/xml; charset=utf-8"}

HOME_PROPS = (
    '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns" '
    'xmlns:cal="urn:ietf:params:xml:ns:caldav">'
    "<d:prop><d:resourcetype/><d:displayname/><d:owner/><oc:owner-principal/>"
    "<oc:invite/><cal:supported-calendar-component-set/></d:prop></d:propfind>"
)
BOT_PROPS = (
    '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
    "<d:prop><d:displayname/><oc:owner-principal/><d:current-user-privilege-set/>"
    "<oc:read-only/></d:prop></d:propfind>"
)
PRINCIPAL_PROPS = (
    '<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>'
)


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _text(element) -> str | None:
    if element is None:
        return None
    return element.findtext(DAV + "href") or element.text


def _propfind(client, path, body, depth="1"):
    """`{segment: {"href", "props": {tag: (status, element)}}}` for one PROPFIND."""
    response = client.request("PROPFIND", path, content=body,
                              headers={**XML, "Depth": depth})
    assert response.status_code == 207, (path, response.status_code)
    rows = {}
    for row in ET.fromstring(response.content).findall(DAV + "response"):
        href = row.findtext(DAV + "href") or ""
        props = {}
        for propstat in row.findall(DAV + "propstat"):
            status = (propstat.findtext(DAV + "status") or "").split()
            code = int(status[1]) if len(status) > 1 else 0
            for prop in propstat.find(DAV + "prop"):
                props[prop.tag] = (code, prop)
        rows[href.rstrip("/").rsplit("/", 1)[-1]] = {"href": href, "props": props}
    return rows


def _share(client, path, sharee, access, *, headers=XML):
    """One `oc:share` POST. `access` is `read`, `read-write` or `None` to remove."""
    href = f"principal:principals/users/{sharee}"
    if access is None:
        inner = f"<oc:remove><d:href>{href}</d:href></oc:remove>"
    else:
        inner = f"<oc:set><d:href>{href}</d:href><oc:{access}/></oc:set>"
    body = f'<oc:share xmlns:oc="http://owncloud.org/ns" xmlns:d="DAV:">{inner}</oc:share>'
    response = client.post(path, content=body.encode(), headers=headers)
    return {"status": response.status_code, "body": response.text[:300]}


def _invite(client, home, segment):
    """`(propstat status, oc:invite element)` on one collection, as its owner reads it."""
    rows = _propfind(client, home, HOME_PROPS)
    return rows[segment]["props"].get(OC + "invite", (0, None))


def _sharee(invite, sharee):
    """One sharee's `oc:user` entry in an `oc:invite`, reduced to plain data."""
    if invite is None:
        return None
    for user in invite.findall(OC + "user"):
        href = user.findtext(DAV + "href") or ""
        if not href.rstrip("/").endswith("principals/users/" + sharee):
            continue
        access = user.find(OC + "access")
        return {
            "href": href,
            "access_children": [_local(c.tag) for c in access] if access is not None else None,
            "access_text": (access.text or "").strip() if access is not None else None,
            "children": [_local(c.tag) for c in user],
        }
    return None


def _privileges(element):
    if element is None:
        return None
    return sorted(_local(child.tag) for privilege in element.findall(DAV + "privilege")
                  for child in privilege)


def _bot_view(bot_client, bot, owner, kind):
    """What the bot's own home shows for `owner`'s collections."""
    home = (f"/remote.php/dav/calendars/{bot}/" if kind == "calendars"
            else f"/remote.php/dav/addressbooks/users/{bot}/")
    seen = []
    for segment, row in _propfind(bot_client, home, BOT_PROPS).items():
        owner_href = _text(row["props"].get(OC + "owner-principal", (0, None))[1]) or ""
        if not owner_href.rstrip("/").endswith("principals/users/" + owner):
            continue
        read_only = row["props"].get(OC + "read-only", (0, None))[1]
        seen.append({
            "segment": segment,
            "privileges": _privileges(
                row["props"].get(DAV + "current-user-privilege-set", (0, None))[1]),
            "read_only": read_only.text if read_only is not None else None,
        })
    return seen


def _home_listing(client, home):
    listing = {}
    for segment, row in _propfind(client, home, HOME_PROPS).items():
        props = row["props"]
        resourcetype = props.get(DAV + "resourcetype", (0, None))[1]
        components = props.get(CAL + "supported-calendar-component-set", (0, None))
        listing[segment] = {
            "href": row["href"],
            "resourcetype": sorted(_local(t.tag) for t in resourcetype)
            if resourcetype is not None else [],
            "displayname": _text(props.get(DAV + "displayname", (0, None))[1]),
            "owner_principal": _text(props.get(OC + "owner-principal", (0, None))[1]),
            "dav_owner": _text(props.get(DAV + "owner", (0, None))[1]),
            "invite_status": props.get(OC + "invite", (0, None))[0],
            "components": sorted(c.get("name") for c in components[1])
            if components[0] == 200 else None,
        }
    return listing


def _observe(nextcloud, user, app_password, group):
    bot = nextcloud.bot_user
    cal_home = f"/remote.php/dav/calendars/{user}/"
    book_home = f"/remote.php/dav/addressbooks/users/{user}/"
    cal_path = f"{cal_home}personal/"
    book_path = f"{book_home}contacts/"
    facts = {}

    # The birthday calendar exists only once synced; ask for it so it can be seen.
    nextcloud._occ("dav:sync-birthday-calendar", user)

    with httpx.Client(base_url=nextcloud.base_url, auth=(user, app_password),
                      timeout=30) as client, \
         httpx.Client(base_url=nextcloud.base_url,
                      auth=(bot, nextcloud._passwords[bot]), timeout=30) as bot_client:
        principal = _propfind(client, "/remote.php/dav/", PRINCIPAL_PROPS, depth="0")
        facts["current_user_principal"] = [
            _text(row["props"][DAV + "current-user-principal"][1])
            for row in principal.values()
        ]
        facts["calendars_home"] = _home_listing(client, cal_home)
        facts["contacts_home"] = _home_listing(client, book_home)

        # A read share, a set to read-write on the same share, then removal.
        facts["calendar_read_post"] = _share(client, cal_path, bot, "read")
        status, invite = _invite(client, cal_home, "personal")
        facts["calendar_read_invite_status"] = status
        facts["calendar_read_invite"] = _sharee(invite, bot)
        facts["calendar_read_bot_view"] = _bot_view(bot_client, bot, user, "calendars")
        facts["calendar_full_post"] = _share(client, cal_path, bot, "read-write")
        facts["calendar_full_invite"] = _sharee(_invite(client, cal_home, "personal")[1], bot)
        facts["calendar_full_bot_view"] = _bot_view(bot_client, bot, user, "calendars")
        facts["calendar_remove_post"] = _share(client, cal_path, bot, None)
        facts["calendar_removed_invite"] = _sharee(_invite(client, cal_home, "personal")[1], bot)
        facts["calendar_removed_bot_view"] = _bot_view(bot_client, bot, user, "calendars")

        facts["book_read_post"] = _share(client, book_path, bot, "read")
        facts["book_read_invite"] = _sharee(_invite(client, book_home, "contacts")[1], bot)
        facts["book_read_bot_view"] = _bot_view(bot_client, bot, user, "contacts")
        facts["book_full_post"] = _share(client, book_path, bot, "read-write")
        facts["book_full_invite"] = _sharee(_invite(client, book_home, "contacts")[1], bot)
        facts["book_full_bot_view"] = _bot_view(bot_client, bot, user, "contacts")
        facts["book_remove_post"] = _share(client, book_path, bot, None)
        facts["book_removed_invite"] = _sharee(_invite(client, book_home, "contacts")[1], bot)

        # A share POST with no Content-Type at all.
        facts["no_content_type_post"] = _share(client, cal_path, bot, "read", headers={})
        facts["no_content_type_invite"] = _sharee(_invite(client, cal_home, "personal")[1], bot)
        _share(client, cal_path, bot, None)

        # A sharee that does not exist.
        ghost = f"ghost-{uuid.uuid4().hex[:8]}"
        facts["ghost_post"] = _share(client, cal_path, ghost, "read")
        facts["ghost_invite"] = _sharee(_invite(client, cal_home, "personal")[1], ghost)

        # Sharing restricted to group members, the user in a group the bot is not in.
        created = nextcloud._ocs("/ocs/v2.php/cloud/groups", method="POST",
                                 body={"groupid": group})
        assert created.status_code in (100, 200), created.status_code
        joined = nextcloud._ocs(f"/ocs/v2.php/cloud/users/{user}/groups", method="POST",
                                body={"groupid": group})
        assert joined.status_code in (100, 200), joined.status_code
        nextcloud._occ("config:app:set", "core", "shareapi_only_share_with_group_members",
                       "--value=yes")
        try:
            facts["group_only_setting"] = nextcloud._occ(
                "config:app:get", "core", "shareapi_only_share_with_group_members").strip()
            facts["group_only_post"] = _share(client, cal_path, bot, "read")
            facts["group_only_invite"] = _sharee(_invite(client, cal_home, "personal")[1], bot)
            facts["group_only_bot_view"] = _bot_view(bot_client, bot, user, "calendars")
        finally:
            nextcloud._occ("config:app:set", "core", "shareapi_only_share_with_group_members",
                           "--value=no")
            _share(client, cal_path, bot, None)

        # Sharing switched off for the whole instance.
        nextcloud._occ("config:app:set", "core", "shareapi_enabled", "--value=no")
        try:
            facts["sharing_off_post"] = _share(client, cal_path, bot, "read")
            facts["sharing_off_invite"] = _sharee(_invite(client, cal_home, "personal")[1], bot)
            facts["sharing_off_bot_view"] = _bot_view(bot_client, bot, user, "calendars")
        finally:
            nextcloud._occ("config:app:set", "core", "shareapi_enabled", "--value=yes")
            _share(client, cal_path, bot, None)
    return facts


@pytest.fixture
def dav_user(stack):
    """A fresh Nextcloud user, signed in with an app password, removed afterwards."""
    nextcloud = stack.service("nextcloud")
    nextcloud.users()  # The service retries reads while the front door comes up.
    suffix = uuid.uuid4().hex[:10]
    user = f"dav-probe-{suffix}"
    group = f"dav-probe-group-{suffix}"
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
        yield nextcloud, user, answer.json()["ocs"]["data"]["apppassword"], group
    finally:
        nextcloud._occ("config:app:set", "core", "shareapi_only_share_with_group_members",
                       "--value=no")
        nextcloud._occ("config:app:set", "core", "shareapi_enabled", "--value=yes")
        nextcloud._ocs(f"/ocs/v2.php/cloud/groups/{group}", method="DELETE", tolerate=(404,))
        deleted = nextcloud._ocs(f"/ocs/v2.php/cloud/users/{user}", method="DELETE")
        assert deleted.status_code in (100, 200), deleted.status_code


def test_nextcloud_dav_sharing_facts(dav_user, record_property):
    nextcloud, user, app_password, group = dav_user
    bot = nextcloud.bot_user
    facts = _observe(nextcloud, user, app_password, group)
    record_property("nextcloud_dav_facts", json.dumps(facts, sort_keys=True))
    principal = f"/remote.php/dav/principals/users/{user}/"
    read = {"href": f"principal:principals/users/{bot}", "access_children": ["read"],
            "access_text": "", "children": ["href", "common-name", "invite-accepted", "access"]}
    full = {**read, "access_children": ["read-write"]}

    # The principal comes back in the rooted form, for the uid the token belongs to.
    assert facts["current_user_principal"] == [principal]

    # Calendars: `oc:invite` answers 200 on the birthday calendar too, so the
    # segment backstop is what keeps it off the card.
    calendars = facts["calendars_home"]
    assert calendars["personal"]["invite_status"] == 200
    assert calendars["personal"]["displayname"] == "Personal"
    # Calendars carry both owner properties, `oc:owner-principal` unrooted.
    assert calendars["personal"]["owner_principal"] == f"principals/users/{user}"
    assert calendars["personal"]["dav_owner"] == principal
    assert calendars["contact_birthdays"]["invite_status"] == 200
    assert calendars["contact_birthdays"]["displayname"] == "Contact birthdays"
    assert "calendar" in calendars["contact_birthdays"]["resourcetype"]
    for segment in ("inbox", "outbox", "trashbin"):
        assert "calendar" not in calendars[segment]["resourcetype"], segment
        assert calendars[segment]["invite_status"] == 404, segment

    # Address books: "Recently contacted" refuses `oc:invite`; the system
    # address book ("Accounts") answers 200, and only its owner keeps it off.
    books = facts["contacts_home"]
    assert books["contacts"]["invite_status"] == 200
    # The owner's own address books carry only `DAV:owner`.
    assert books["contacts"]["owner_principal"] is None
    assert books["contacts"]["dav_owner"] == principal
    recent = books["z-app-generated--contactsinteraction--recent"]
    assert recent["displayname"] == "Recently contacted"
    assert recent["invite_status"] == 404
    system = books["z-server-generated--system"]
    assert system["displayname"] == "Accounts"
    assert system["invite_status"] == 200
    assert system["owner_principal"] == "principals/system/system"
    assert system["dav_owner"] == "/remote.php/dav/principals/system/system/"

    # `oc:access` carries a child element, never text, and a set on an existing
    # share changes its access in place.
    assert facts["calendar_read_post"]["status"] == 200
    assert facts["calendar_read_invite_status"] == 200
    assert facts["calendar_read_invite"] == read
    assert facts["calendar_full_post"]["status"] == 200
    assert facts["calendar_full_invite"] == full
    assert facts["calendar_remove_post"]["status"] == 200
    assert facts["calendar_removed_invite"] is None
    assert facts["calendar_removed_bot_view"] == []
    assert facts["book_read_invite"] == read
    assert facts["book_full_invite"] == full
    assert facts["book_removed_invite"] is None

    # The bot's view of a read-only share: no `write`, but `write-properties`,
    # and `oc:read-only` set.
    for view in (facts["calendar_read_bot_view"], facts["book_read_bot_view"]):
        assert len(view) == 1, view
        assert "write" not in view[0]["privileges"], view
        assert "write-properties" in view[0]["privileges"], view
        assert view[0]["read_only"] == "1", view
    for view in (facts["calendar_full_bot_view"], facts["book_full_bot_view"]):
        assert len(view) == 1, view
        assert "write" in view[0]["privileges"], view
        assert view[0]["read_only"] is None, view
    assert facts["calendar_read_bot_view"][0]["segment"] == f"personal_shared_by_{user}"

    # Without a Content-Type the POST is refused outright, not ignored.
    assert facts["no_content_type_post"]["status"] == 501
    assert "NotImplemented" in facts["no_content_type_post"]["body"]
    assert facts["no_content_type_invite"] is None

    # A sharee that does not resolve: 200, and nothing applied.
    assert facts["ghost_post"]["status"] == 200
    assert facts["ghost_invite"] is None

    # The group restriction does not reach DAV sharing: 200 and applied.
    assert facts["group_only_setting"] == "yes"
    assert facts["group_only_post"]["status"] == 200
    assert facts["group_only_invite"] == read
    assert len(facts["group_only_bot_view"]) == 1

    # Switching the share API off does: 200, and nothing applied.
    assert facts["sharing_off_post"]["status"] == 200
    assert facts["sharing_off_invite"] is None
    assert facts["sharing_off_bot_view"] == []
