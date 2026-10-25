"""A real Nextcloud offers a folder to istota over Open Cloud Mesh: the offer step.

What the default suite cannot hold is whether a real Nextcloud 30 accepts
istota's discovery document and posts an offer that `POST /ocm/shares` takes.
Here the testbed's Nextcloud shares a folder with `testuser@istota.test` and
the offer must arrive as a `pending` `shared_sources` row.

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

Discretionary: `uv run pytest -m full -n0 tests/full/test_ocm_federated_share.py`.
The accept, read-write and unshare steps belong to Stages 7 and 8.
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
