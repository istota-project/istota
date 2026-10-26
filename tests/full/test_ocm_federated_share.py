"""A real Nextcloud offers a folder to istota over Open Cloud Mesh: offer, accept, unshare.

What the default suite cannot hold is whether a real Nextcloud 30 accepts
istota's discovery document and posts an offer that `POST /ocm/shares` takes,
whether its public WebDAV endpoint opens to the share secret and reports
``oc:permissions`` as the accept reads them, and whether it posts
``SHARE_UNSHARED`` to ``/ocm/notifications`` and deletes a share on
``SHARE_DECLINED``. Here the testbed's Nextcloud shares folders with
`testuser@istota.test`; each offer must arrive as a `pending`
`shared_sources` row, accepting it through the web API must make it `active`
with the right access and a local credential, and unsharing on Nextcloud, or
removing it in istota, must end it.

Nextcloud needs four things before it sends anything (the open-protocol-sharing
spec, Stage 1, and its full-tier bullet), and this module's fixture sets each:

- `allow_local_remote_servers`, or every outbound request is refused as a
  local address;
- istota over https under a name the Nextcloud container resolves, since
  Nextcloud posts an offer only over https to the `endPoint` the discovery
  document names. The fixture runs a TLS front (`nginx:alpine`) on the stack's
  network as `istota.test`, proxying to the stack's own nginx, with a
  certificate from `testbed.certs`;
- that certificate in the Nextcloud container's system store with
  `update-ca-certificates`, because the OCM calls ignore
  `occ security:certificates:import`;
- `overwritehost` and `overwrite.cli.url` naming the in-network `nextcloud`,
  so the owner's cloud id is `testuser@http://nextcloud`, which istota can
  reach, and which is therefore in this profile's `[sharing] private_hosts`.

The profile is this module's own and boots fresh: its `[site] hostname` is
`istota.test` rather than the published `localhost:<port>` the other full
scenarios sign in on.

**Negative control**: `ISTOTA_OCM_CONTROL=empty_endpoint` makes the TLS front
answer `/ocm-provider/` with istota's document and an empty `endPoint`, which
Nextcloud refuses (it falls back to the legacy OCS share path, which answers
404), so the offer step must go red. `enabled: false` would not: Nextcloud
parses it and ignores it. Nextcloud caches a failed discovery for five minutes
in APCu, so the fixture restarts Apache before the share.

**A task reads and writes the share** between the accept and the unshare: a
scripted Bash turn runs `istota-skill shares ls`, `get` and `put` in a task
for testuser, so the skill proxy spawns the CLI with the private credential
channel, the channel serves `ocm_share_<id>` under the broker grant the accept
created, and the CLI reaches Nextcloud's public WebDAV with it. The owner then
lowers the share to read-only on Nextcloud, which sends istota nothing, and
the next task's `shares ls` must record `access = read` from its own root
PROPFIND. This is also the private channel's witness in a deployed sandbox:
the lean stack has no shipped way to seed an accepted share.

**The Files page browses a share** (#849) through the web API over the same public WebDAV, and a share of a single file is listed and downloaded as that file, by the web API and by the skill, since only a real Nextcloud says how it serves a file share's root.

Discretionary: `uv run pytest -m full -n0 tests/full/test_ocm_federated_share.py`.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from dataclasses import replace

import pytest

from testbed import certs, profiles
from testbed import stack as stack_support

pytestmark = [pytest.mark.full]

SITE = "istota.test"
FRONT_IMAGE = "nginx:alpine"
OFFER_WAIT_SECONDS = 90

OCM_PROFILE = replace(
    profiles.FULL,
    name="ocm",
    config={
        **profiles.FULL_CONFIG,
        "site": {"hostname": SITE},
        "sharing": {"private_hosts": ["nextcloud"], "ocm": {"enabled": True}},
    },
)


def _front_conf(control: str) -> str:
    override = ""
    if control == "empty_endpoint":
        document = {
            "enabled": True, "apiVersion": "1.1.0", "endPoint": "", "provider": "Istota",
            "resourceTypes": [
                {"name": "file", "shareTypes": ["user"], "protocols": {"webdav": "/webdav/"}},
            ],
        }
        body = json.dumps(document).replace("'", "\\'")
        override = (
            "    location = /ocm-provider/ {\n"
            "        default_type application/json;\n"
            f"        return 200 '{body}';\n"
            "    }\n"
        )
    return (
        "server {\n"
        "    listen 443 ssl;\n"
        f"    server_name {SITE};\n"
        "    ssl_certificate /certs/mail.crt;\n"
        "    ssl_certificate_key /certs/mail.key;\n"
        f"{override}"
        "    location / {\n"
        "        proxy_pass http://nginx:80;\n"
        f"        proxy_set_header Host {SITE};\n"
        "        proxy_set_header X-Forwarded-Proto https;\n"
        "    }\n"
        "}\n"
    )


def _docker(*argv: str, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *argv], capture_output=True, text=True, timeout=timeout)


def _compose(stack, *argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(stack.args + list(argv), capture_output=True, text=True, timeout=120)


@pytest.fixture(scope="module")
def ocm_stack(stacks, tmp_path_factory):
    """A fresh full stack receiving OCM, with the TLS front and Nextcloud's federation settings."""
    stack = stacks.get(OCM_PROFILE, fresh=True)
    nextcloud = stack.service("nextcloud")
    nextcloud.users()  # the service retries reads while the front door comes up
    workdir = tmp_path_factory.mktemp("ocm-front")
    cert_dir = workdir / "certs"
    crt, _key = certs.generate_self_signed(cert_dir, sans=(SITE,))
    conf = workdir / "front.conf"
    conf.write_text(_front_conf(os.environ.get("ISTOTA_OCM_CONTROL", "")))

    project = stack_support._project_of(stack.args)
    front = f"{project}-ocm-front"
    started = _docker(
        "run", "-d", "--rm", "--name", front, "--network", f"{project}_default",
        "--network-alias", SITE,
        "-v", f"{cert_dir}:/certs:ro", "-v", f"{conf}:/etc/nginx/conf.d/default.conf:ro",
        FRONT_IMAGE,
    )
    assert started.returncode == 0, started.stderr
    try:
        copied = _compose(stack, "cp", str(crt), "nextcloud:/usr/local/share/ca-certificates/istota-test.crt")
        assert copied.returncode == 0, copied.stderr
        trusted = stack.exec(["update-ca-certificates"], service="nextcloud", user="root")
        assert trusted.returncode == 0, trusted.stderr
        nextcloud._occ("config:system:set", "allow_local_remote_servers",
                       "--value=true", "--type=boolean")
        nextcloud._occ("config:system:set", "overwritehost", "--value=nextcloud")
        nextcloud._occ("config:system:set", "overwrite.cli.url", "--value=http://nextcloud")
        nextcloud._occ("config:system:set", "overwriteprotocol", "--value=http")
        # A failed discovery is cached in APCu for five minutes; a graceful
        # restart clears it, so a control run after a green one is not served
        # the green run's cache (or the reverse).
        stack.exec(["apachectl", "-k", "graceful"], service="nextcloud", user="root")
        yield stack
    finally:
        _docker("rm", "-f", front)
        for key in ("overwritehost", "overwrite.cli.url", "overwriteprotocol",
                    "allow_local_remote_servers"):
            try:
                nextcloud._occ("config:system:delete", key)
            except Exception:  # noqa: BLE001 — teardown of a stack being released
                pass
        stacks.release(stack)


def _offers_since(stack, mark: int, name: str) -> list[dict]:
    return stack.probe.query(
        "SELECT id, state, remote_host, access FROM shared_sources"
        " WHERE id > ? AND kind = 'ocm_share' AND user_id = 'testuser'"
        " AND json_extract(extras, '$.name') = ?",
        [mark, name],
    )


def test_a_folder_shared_from_nextcloud_arrives_as_a_pending_offer(ocm_stack):
    nextcloud = ocm_stack.service("nextcloud")
    owner = nextcloud.test_user
    folder = f"OCM offer {uuid.uuid4().hex[:8]}"
    _body, status = nextcloud._dav(folder, user=owner, method="MKCOL")
    assert status == 201, status
    [row] = ocm_stack.probe.query("SELECT COALESCE(MAX(id), 0) AS mark FROM shared_sources")
    mark = row["mark"]

    shared = nextcloud._ocs(
        "/ocs/v2.php/apps/files_sharing/api/v1/shares", user=owner, method="POST",
        body={"path": f"/{folder}", "shareType": 6, "shareWith": f"testuser@{SITE}",
              "permissions": 31},
        tolerate=(403, 404),
    )
    assert shared.status_code in (100, 200), (shared.status_code, shared.message)

    deadline = time.monotonic() + OFFER_WAIT_SECONDS
    offers = _offers_since(ocm_stack, mark, folder)
    while not offers and time.monotonic() < deadline:
        time.sleep(2)
        offers = _offers_since(ocm_stack, mark, folder)
    assert offers, (
        "no offer reached istota; Nextcloud logs:\n" + ocm_stack.logs(tail=40, service="nextcloud")
    )
    [offer] = offers
    assert offer["state"] == "pending"
    assert offer["remote_host"] == "nextcloud"
    assert offer["access"] == "read"
    notified = ocm_stack.probe.query(
        "SELECT title FROM notifications WHERE user_id = 'testuser' AND source = 'shared_source'"
    )
    assert [n["title"] for n in notified] == ["A folder was shared with you"]


# --------------------------------------------------------- accept and unshare

API = "/istota/api"
ACCEPT_WAIT_SECONDS = 60


def _form_fields(page: str, action: str) -> dict[str, str]:
    import re
    from html import unescape

    form = re.search(r'<form\b[^>]*action="' + re.escape(action) + r'"[^>]*>(.*?)</form>',
                     page, re.S)
    assert form is not None, f"Missing form: {action}"
    return {name: unescape(value) for name, value
            in re.findall(r'<input[^>]*name="([^"]+)"[^>]*value="([^"]*)"', form[1])}


@pytest.fixture(scope="module")
def web(ocm_stack):
    """testuser signed in to the web app with an email identity, as a person accepts an offer.

    `Origin` is `[site] hostname`, which `_verify_origin` checks on every
    state-changing call; the client talks to the stack's own nginx.
    """
    import secrets

    import httpx

    from testbed.stack import CONTAINER_CONFIG

    password = secrets.token_urlsafe(24)
    added = subprocess.run(
        ocm_stack.args + ["exec", "-T", "daemon", "istota-drop", "uv", "run", "istota", "-c",
                          CONTAINER_CONFIG, "auth", "add", "testuser",
                          "--email", "testuser@ocm.example.test", "--password-stdin"],
        input=password + "\n", text=True, capture_output=True, timeout=120,
    )
    assert added.returncode == 0, added.stderr
    port = ocm_stack.published_port("nginx", 80)
    client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60, trust_env=False,
                          headers={"Origin": f"http://{SITE}"})
    page = client.get("/istota/login")
    assert page.status_code == 200, page.text
    signed_in = client.post("/istota/login/email", data={
        **_form_fields(page.text, "/istota/login/email"),
        "email": "testuser@ocm.example.test", "password": password,
    })
    assert signed_in.status_code == 302, signed_in.text
    yield client
    client.close()


def _share_folder(stack, permissions: int) -> tuple[str, int, dict]:
    """A fresh folder shared from Nextcloud; its name, the Nextcloud share id, the offer row."""
    nextcloud = stack.service("nextcloud")
    owner = nextcloud.test_user
    folder = f"OCM share {uuid.uuid4().hex[:8]}"
    _body, status = nextcloud._dav(folder, user=owner, method="MKCOL")
    assert status == 201, status
    [row] = stack.probe.query("SELECT COALESCE(MAX(id), 0) AS mark FROM shared_sources")
    shared = nextcloud._ocs(
        "/ocs/v2.php/apps/files_sharing/api/v1/shares", user=owner, method="POST",
        body={"path": f"/{folder}", "shareType": 6, "shareWith": f"testuser@{SITE}",
              "permissions": permissions},
        tolerate=(403, 404),
    )
    assert shared.status_code in (100, 200), (shared.status_code, shared.message)
    deadline = time.monotonic() + OFFER_WAIT_SECONDS
    offers = _offers_since(stack, row["mark"], folder)
    while not offers and time.monotonic() < deadline:
        time.sleep(2)
        offers = _offers_since(stack, row["mark"], folder)
    assert offers, "no offer reached istota:\n" + stack.logs(tail=40, service="nextcloud")
    return folder, int(shared.data["id"]), offers[0]


def _row(stack, source_id: int) -> dict:
    [row] = stack.probe.query(
        "SELECT state, access, last_reason FROM shared_sources WHERE id = ?", [source_id])
    return row


def _credential_names(stack, source_id: int) -> list[str]:
    return [r["name"] for r in stack.probe.query(
        "SELECT name FROM credential_bindings WHERE user_id = 'testuser' AND name = ?",
        [f"ocm_share_{source_id}"])]


TASK_WAIT_SECONDS = 180


def _bash_task(stack, command: str, prompt: str) -> str:
    """One scripted Bash turn in a task for testuser; the endpoint transcript after it."""
    stack.script([
        {"tool_calls": [{"id": "call-1", "name": "Bash", "arguments": {"command": command}}]},
        {"text": "done with the shared folder"},
    ])
    task_id = stack.submit(prompt)
    stack.probe.wait_for_task(status="completed", task_id=task_id, timeout=TASK_WAIT_SECONDS)
    return stack.endpoint.transcript()


def test_accepting_through_the_web_api_and_unsharing_on_nextcloud(ocm_stack, web):
    nextcloud = ocm_stack.service("nextcloud")
    folder, share_id, offer = _share_folder(ocm_stack, permissions=31)
    marker = f"from-nextcloud-{uuid.uuid4().hex[:8]}"
    _body, status = nextcloud._dav(f"{folder}/hello.txt", user=nextcloud.test_user,
                                   method="PUT", body=marker.encode())
    assert status in (201, 204), status

    accepted = web.post(f"{API}/settings/sharing/folders/{offer['id']}/accept",
                        json={"allow_scheduled": False})
    assert accepted.status_code == 200, accepted.text
    # The PROPFIND proved the secret and read oc:permissions; 31 is an edit share.
    assert _row(ocm_stack, offer["id"])["state"] == "active"
    assert _row(ocm_stack, offer["id"])["access"] == "write"
    assert _credential_names(ocm_stack, offer["id"]) == [f"ocm_share_{offer['id']}"]
    assert accepted.json()["notified"] is True

    # A task reads and writes the share through the `shares` skill.
    ref = f"share:{offer['id']}"
    uploaded = f"from-istota-{uuid.uuid4().hex[:8]}"
    transcript = _bash_task(ocm_stack, (
        "set -eu\n"
        'W="${ISTOTA_WORKSPACE_PATH:-/mnt/shared}/Users/testuser/ocm-shares"\n'
        'mkdir -p "$W"\n'
        f"istota-skill shares ls {ref}\n"
        f'istota-skill shares get {ref}:hello.txt --dest "$W/in" --overwrite\n'
        'cat "$W/in/hello.txt"; echo\n'
        f'printf %s {uploaded} > "$W/up.txt"\n'
        f'istota-skill shares put "$W/up.txt" --to {ref} --overwrite\n'
    ), "read and write the shared folder")
    assert marker in transcript, transcript[-4000:]
    body, status = nextcloud._dav(f"{folder}/up.txt", user=nextcloud.test_user)
    assert (status, body.decode()) == (200, uploaded), (status, transcript[-4000:])

    # Lowering the share on Nextcloud sends istota nothing; the next call reads it.
    lowered = nextcloud._ocs(f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}",
                             user=nextcloud.test_user, method="PUT", body={"permissions": 1})
    assert lowered.status_code in (100, 200), (lowered.status_code, lowered.message)
    assert _row(ocm_stack, offer["id"])["access"] == "write"
    _bash_task(ocm_stack, f"istota-skill shares ls {ref}\n", "list the shared folder")
    assert _row(ocm_stack, offer["id"])["access"] == "read"

    # Unsharing on Nextcloud posts SHARE_UNSHARED with the share's secret.
    nextcloud._ocs(f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}",
                   user=nextcloud.test_user, method="DELETE")
    deadline = time.monotonic() + ACCEPT_WAIT_SECONDS
    while _row(ocm_stack, offer["id"])["state"] != "revoked" and time.monotonic() < deadline:
        time.sleep(2)
    revoked = _row(ocm_stack, offer["id"])
    assert (revoked["state"], revoked["last_reason"]) == ("revoked", "remote_unshared"), (
        "the unshare did not reach istota; Nextcloud logs:\n"
        + ocm_stack.logs(tail=40, service="nextcloud"))
    assert _credential_names(ocm_stack, offer["id"]) == []


def _share_path(stack, path: str, name: str, permissions: int) -> dict:
    """``path`` in testuser's Nextcloud shared to istota; the offer row named ``name``."""
    nextcloud = stack.service("nextcloud")
    [row] = stack.probe.query("SELECT COALESCE(MAX(id), 0) AS mark FROM shared_sources")
    shared = nextcloud._ocs(
        "/ocs/v2.php/apps/files_sharing/api/v1/shares", user=nextcloud.test_user, method="POST",
        body={"path": f"/{path}", "shareType": 6, "shareWith": f"testuser@{SITE}",
              "permissions": permissions},
        tolerate=(403, 404),
    )
    assert shared.status_code in (100, 200), (shared.status_code, shared.message)
    deadline = time.monotonic() + OFFER_WAIT_SECONDS
    offers = _offers_since(stack, row["mark"], name)
    while not offers and time.monotonic() < deadline:
        time.sleep(2)
        offers = _offers_since(stack, row["mark"], name)
    assert offers, "no offer reached istota:\n" + stack.logs(tail=40, service="nextcloud")
    return offers[0]


def test_the_files_page_browses_a_shared_folder(ocm_stack, web):
    """#849: the web process lists and downloads a share over Nextcloud's public WebDAV."""
    nextcloud = ocm_stack.service("nextcloud")
    folder, _share_id, offer = _share_folder(ocm_stack, permissions=1)
    marker = f"browsed-{uuid.uuid4().hex[:8]}"
    _body, status = nextcloud._dav(f"{folder}/inner", user=nextcloud.test_user, method="MKCOL")
    assert status == 201, status
    _body, status = nextcloud._dav(f"{folder}/inner/note.txt", user=nextcloud.test_user,
                                   method="PUT", body=marker.encode())
    assert status in (201, 204), status
    accepted = web.post(f"{API}/settings/sharing/folders/{offer['id']}/accept",
                        json={"allow_scheduled": False})
    assert accepted.status_code == 200, accepted.text
    place = f"share:{offer['id']}"

    root = web.get(f"{API}/workspace/list", params={"place": place, "path": ""})
    assert root.status_code == 200, root.text
    assert (root.json()["access"], root.json()["root_kind"]) == ("read", "folder")
    assert [(e["name"], e["kind"]) for e in root.json()["entries"]] == [("inner", "dir")]
    inner = web.get(f"{API}/workspace/list", params={"place": place, "path": "inner"})
    assert [e["share_path"] for e in inner.json()["entries"]] == ["inner/note.txt"]
    got = web.get(f"{API}/workspace/share/file", params={"place": place, "path": "inner/note.txt"})
    assert (got.status_code, got.text) == (200, marker)
    assert got.headers["content-disposition"].startswith("attachment;")


def test_a_shared_file_is_listed_and_downloaded_as_that_file(ocm_stack, web):
    """#849: OCM offers a file as ``resourceType: file`` too; the root's PROPFIND says which."""
    nextcloud = ocm_stack.service("nextcloud")
    name = f"OCM file {uuid.uuid4().hex[:8]}.txt"
    marker = f"one-file-{uuid.uuid4().hex[:8]}"
    _body, status = nextcloud._dav(name, user=nextcloud.test_user, method="PUT",
                                   body=marker.encode())
    assert status in (201, 204), status
    offer = _share_path(ocm_stack, name, name, permissions=1)
    accepted = web.post(f"{API}/settings/sharing/folders/{offer['id']}/accept",
                        json={"allow_scheduled": False})
    assert accepted.status_code == 200, accepted.text
    assert accepted.json()["share"]["kind"] == "file"
    place = f"share:{offer['id']}"

    listing = web.get(f"{API}/workspace/list", params={"place": place, "path": ""})
    assert listing.status_code == 200, listing.text
    [entry] = listing.json()["entries"]
    assert (entry["name"], entry["kind"], entry["share_path"]) == (name, "file", "")
    got = web.get(f"{API}/workspace/share/file", params={"place": place, "path": ""})
    assert (got.status_code, got.text) == (200, marker)

    transcript = _bash_task(ocm_stack, (
        "set -eu\n"
        'W="${ISTOTA_WORKSPACE_PATH:-/mnt/shared}/Users/testuser/ocm-file"\n'
        f"istota-skill shares ls {place}\n"
        f'istota-skill shares get {place} --dest "$W" --overwrite\n'
        f'cat "$W/{name}"; echo\n'
    ), "fetch the shared file")
    assert marker in transcript, transcript[-4000:]


def test_a_read_share_is_accepted_read_only_and_removing_ends_it(ocm_stack, web):
    _folder, share_id, offer = _share_folder(ocm_stack, permissions=1)
    accepted = web.post(f"{API}/settings/sharing/folders/{offer['id']}/accept",
                        json={"allow_scheduled": False})
    assert accepted.status_code == 200, accepted.text
    assert _row(ocm_stack, offer["id"])["access"] == "read"

    removed = web.delete(f"{API}/settings/sharing/folders/{offer['id']}")
    assert removed.status_code == 200, removed.text
    assert removed.json()["notified"] is True
    assert _row(ocm_stack, offer["id"])["state"] == "revoked"
    # Nextcloud deletes the share on SHARE_DECLINED: the owner's list no longer has it.
    nextcloud = ocm_stack.service("nextcloud")
    listed = nextcloud._ocs("/ocs/v2.php/apps/files_sharing/api/v1/shares",
                            user=nextcloud.test_user)
    assert all(int(s["id"]) != share_id for s in (listed.data or []))
