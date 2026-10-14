"""Real CardDAV sharing, owner properties and contact edits on Nextcloud 30.

Discretionary: run with -m full -n0 and --junitxml to retain the actual owner
properties. Collection alone does not witness the server's property shape.
"""

import json
import secrets
import textwrap
import uuid
from xml.etree import ElementTree as ET

import httpx
import pytest

pytestmark = [pytest.mark.full, pytest.mark.profile("full")]

DAV = "{DAV:}"
OC = "{http://owncloud.org/ns}"
CARD = "{urn:ietf:params:xml:ns:carddav}"


def _cli(stack, user, *args, reason=None):
    snippet = textwrap.dedent("""\
        import json, os, pathlib, subprocess, sys
        from istota.config import load_config
        config = load_config(pathlib.Path('/data/config/config.toml'))
        env = dict(os.environ, ISTOTA_USER_ID=sys.argv[1],
                   CARDDAV_URL=config.carddav_url,
                   CARDDAV_USERNAME=config.carddav_username,
                   CARDDAV_PASSWORD=config.carddav_password)
        for name in ('ISTOTA_SANDBOXED', 'GOOGLE_CONTACTS_TOKEN', 'GOOGLE_CONTACTS_SCOPES'):
            env.pop(name, None)
        result = subprocess.run(
            [sys.executable, '-m', 'istota.skills.contacts', *sys.argv[2:]],
            env=env, capture_output=True, text=True, timeout=120)
        print(json.dumps({'code': result.returncode, 'answer': json.loads(result.stdout)}))
    """)
    result = stack.exec(["uv", "run", "python", "-c", snippet, user, *args], timeout=180)
    assert result.returncode == 0, result.stderr
    result = json.loads(result.stdout)
    answer = result["answer"]
    assert result["code"] == (1 if reason else 0), answer
    assert answer["status"] == ("error" if reason else "ok"), answer
    if reason:
        assert answer["reason"] == reason, answer
    return answer


def _book(client, owner, bot, name, *, writable):
    path = f"/remote.php/dav/addressbooks/users/{owner}/{name}/"
    body = (
        '<d:mkcol xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:carddav">'
        '<d:set><d:prop><d:resourcetype><d:collection/><c:addressbook/>'
        f'</d:resourcetype><d:displayname>{name}</d:displayname>'
        '</d:prop></d:set></d:mkcol>'
    )
    response = client.request("MKCOL", path, content=body)
    assert response.status_code == 201, response.status_code
    access = "read-write" if writable else "read"
    body = (
        '<oc:share xmlns:oc="http://owncloud.org/ns" xmlns:d="DAV:">'
        f'<oc:set><d:href>principal:principals/users/{bot}</d:href>'
        f'<oc:{access}/></oc:set></oc:share>'
    )
    response = client.post(path, content=body)
    assert response.status_code in (200, 204), response.status_code


@pytest.fixture
def shared_books(stack):
    nextcloud = stack.service("nextcloud")
    nextcloud.users()  # The service retries reads while the front door comes up.
    suffix = uuid.uuid4().hex[:10]
    users = []
    try:
        for prefix in ("alice", "bob"):
            user = f"contacts-{prefix}-{suffix}"
            password = secrets.token_urlsafe(24)
            created = nextcloud._ocs(
                "/ocs/v2.php/cloud/users", user=nextcloud.admin_user,
                method="POST", body={"userid": user, "password": password},
            )
            assert created.status_code in (100, 200), created.status_code
            users.append(user)
            with httpx.Client(base_url=nextcloud.base_url, auth=(user, password),
                              headers={"Content-Type": "application/xml"}, timeout=30) as client:
                _book(client, user, nextcloud.bot_user, f"{prefix}-{suffix}", writable=True)
                if prefix == "alice":
                    _book(client, user, nextcloud.bot_user, f"readonly-{suffix}", writable=False)
        yield nextcloud, users[0], users[1], suffix
    finally:
        for user in users:
            deleted = nextcloud._ocs(
                f"/ocs/v2.php/cloud/users/{user}", user=nextcloud.admin_user,
                method="DELETE",
            )
            assert deleted.status_code in (100, 200), deleted.status_code


def test_shared_books_owner_witness_and_cli_round_trip(stack, shared_books, record_property):
    nextcloud, alice, bob, suffix = shared_books
    # Inspect the server response independently of the product's owner parser.
    body = (
        '<d:propfind xmlns:d="DAV:" xmlns:oc="http://owncloud.org/ns">'
        '<d:prop><d:resourcetype/><d:displayname/><d:owner/>'
        '<oc:owner-principal/></d:prop></d:propfind>'
    )
    with httpx.Client(base_url=nextcloud.base_url,
                      auth=(nextcloud.bot_user, nextcloud._passwords[nextcloud.bot_user]),
                      timeout=30) as client:
        response = client.request(
            "PROPFIND", f"/remote.php/dav/addressbooks/users/{nextcloud.bot_user}/",
            content=body, headers={"Depth": "1", "Content-Type": "application/xml"},
        )
    assert response.status_code == 207, response.status_code
    observed = {}
    for row in ET.fromstring(response.content).findall(DAV + "response"):
        props = {}
        for propstat in row.findall(DAV + "propstat"):
            if propstat.findtext(DAV + "status", "").split()[1:2] == ["200"]:
                props.update({p.tag: p for p in propstat.find(DAV + "prop")})
        label = props.get(DAV + "displayname")
        if label is None or suffix not in (label.text or ""):
            continue
        assert props[DAV + "resourcetype"].find(CARD + "addressbook") is not None
        observed[label.text] = {
            tag: prop.findtext(DAV + "href") or prop.text or ""
            for tag, prop in props.items() if tag in (DAV + "owner", OC + "owner-principal")
        }
    record_property("carddav_owner_properties", json.dumps(observed, sort_keys=True))
    assert len(observed) == 3, observed
    for label, owners in observed.items():
        owner = bob if label.startswith("bob-") else alice
        assert any(value.rstrip("/").endswith("/principals/users/" + owner)
                   for value in owners.values()), (label, owners)

    alice_books = _cli(stack, alice, "books")
    assert not alice_books["partial"], alice_books
    assert len(alice_books["books"]) == 2, alice_books
    writable = next(book for book in alice_books["books"] if book["label"].startswith("alice-"))
    readonly = next(book for book in alice_books["books"] if book["label"].startswith("readonly-"))
    assert writable["writable"] is True
    assert readonly["writable"] is False
    _cli(stack, alice, "create", "--book", readonly["key"], "--name", "Refused", reason="read_only")

    bob_books = _cli(stack, bob, "books")["books"]
    assert len(bob_books) == 1, bob_books
    foreign = _cli(stack, bob, "create", "--book", bob_books[0]["key"],
                   "--name", f"Hidden {suffix}")["contact"]
    _cli(stack, alice, "show", foreign["id"], reason="not_found")
    _cli(stack, alice, "update", foreign["id"], "--name", "Refused", reason="not_found")
    _cli(stack, alice, "delete", foreign["id"], reason="not_found")
    hidden = _cli(stack, alice, "search", f"Hidden {suffix}")
    assert not hidden["partial"] and hidden["contacts"] == [], hidden
    assert _cli(stack, bob, "show", foreign["id"])["contact"]["name"] == f"Hidden {suffix}"

    created = _cli(stack, alice, "create", "--book", writable["key"],
                   "--name", f"Alice {suffix}", "--email", "alice@example.com:work")["contact"]
    assert created["etag"] and created["writable"]
    found = _cli(stack, alice, "search", suffix)
    assert not found["partial"]
    assert [item["id"] for item in found["contacts"]] == [created["id"]], found
    shown = _cli(stack, alice, "show", created["id"])["contact"]
    assert shown["emails"] == [{"value": "alice@example.com", "kind": "work"}]
    updated = _cli(stack, alice, "update", created["id"], "--etag", shown["etag"],
                   "--add-phone", "+1 202 555 0100:cell")["contact"]
    assert updated["phones"] == [{"value": "+1 202 555 0100", "kind": "cell"}]
    assert updated["etag"] != shown["etag"]
    _cli(stack, alice, "update", created["id"], "--etag", shown["etag"],
         "--name", "Stale", reason="changed_since_read")
    _cli(stack, alice, "delete", created["id"], "--etag", shown["etag"], reason="changed_since_read")
    _cli(stack, alice, "delete", created["id"], "--etag", updated["etag"])
    _cli(stack, alice, "show", created["id"], reason="not_found")
