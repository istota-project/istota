"""The full shape: the deployment as shipped, booted through its own entrypoint.

`docker/docker-compose.yml` plus `testbed/compose/testbed.yml` — postgres,
redis, nextcloud, istota, web and nginx. Nothing is rendered on the host: the
container runs `render-config.sh` itself, from the environment compose passed
it, exactly as in production. That is the whole reason this shape exists, and it
is what makes `entrypoint.sh` and `provision-nc.sh` witnessable at all.

Everything the shape shares with the lean one lives in `tests/conftest.py` — the
`stacks` and `stack` fixtures, the xdist guards, the sweep. What is here is the
two things specific to *this* tier: the cold-boot cost stated where somebody
running it will read it, and the one combination the tier must refuse.

**Cost.** One cold boot of a six-container stack, most of it Nextcloud
installing itself and then fetching `spreed` and `calendar` from the app store.
`-m full -n0` belongs in the before-a-release set beside `-m image -n0`, not in
an edit loop.

**The outbound dependency, stated because it is real.** `provision-nc.sh` runs
`app:enable spreed`, `calendar` and `files_external`. Only the last is bundled
in `nextcloud:30-apache`; the other two are downloaded from the Nextcloud app
store at first install and land in `custom_apps/`. So this tier needs the
network, and the versions of the two apps its Talk assertions run against are
unpinned while the server image is pinned. It is worth knowing which way that
fails: every `occ` call in that script is `|| true`, so an install with no
network writes its flag and reports success having enabled nothing — which is
why `test_provisioning.py` asserts on `occ app:list` by name rather than on the
flag file.
"""

from __future__ import annotations

import os
import re

import pytest

from testbed import stack as stack_support
from testbed.stack import CONTAINER_CONFIG

from ..support import email_flow
from ..support import email_people as email_people_support


@pytest.fixture(scope="session", autouse=True)
def _refuse_keep_for_the_full_tier():
    """`ISTOTA_TESTBED_KEEP` and this tier are mutually exclusive.

    `KEEP` persists `postgres_data`, `nextcloud_html` and `nextcloud_data` so a
    second session skips the Nextcloud install. Everything in
    `test_provisioning.py` asserts on state `provision-nc.sh` writes *at first
    install* — the users, the enabled apps, the external mounts, the OAuth2
    client and its redirect URI — and the Nextcloud image runs its
    `post-installation` hooks only when it performs the install. So on a kept
    volume set the script does not run, and every assertion here is reading a
    previous session's work while claiming to witness this one's.

    Refused by name, at session scope, rather than left to fail as four
    unrelated-looking assertions after a boot. Autouse because a guard a test
    has to remember to request is a guard that a new test forgets.
    """
    if os.environ.get("ISTOTA_TESTBED_KEEP"):
        pytest.skip(
            "ISTOTA_TESTBED_KEEP persists the provisioned Nextcloud volumes, and "
            "this tier asserts on what first-install provisioning wrote. Unset it "
            "to run the full tier."
        )
    yield


# -- the email suite ---------------------------------------------------------


def alerts_token(stack) -> str:
    """The host's Talk alerts room, as the container rendered it.

    `entrypoint.sh` provisions `#alerts` and `render-config.sh` writes its token
    as testuser's `alerts_channel`, which is the default route an `alert` push
    takes on this shape.
    """
    rendered = stack.exec(["cat", CONTAINER_CONFIG])
    found = re.search(r'^alerts_channel = "([^"]+)"', rendered.stdout, re.M)
    if rendered.returncode != 0 or found is None:
        raise stack_support.StackError(
            f"no alerts_channel in the rendered config:\n{rendered.stdout}"
        )
    return found.group(1)


@pytest.fixture
def email_people(stack) -> email_people_support.EmailPeople:
    """The email suite's users on the full shape, seeded once per stack.

    As on lean (`tests/support/email_people.py`), with one difference: the
    host's alert route keeps the Talk alerts room this shape provisions and
    adds ntfy, rather than replacing Talk with email. A push confined to ntfy
    and email is then visibly not a push on the whole route.
    """
    seeded = getattr(stack, "_email_people", None)
    if seeded is None:
        try:
            seeded = email_people_support.seed_email_people(
                stack, alert_route=f"alert=talk:{alerts_token(stack)},ntfy",
            )
        except (TimeoutError, stack_support.StackError) as exc:
            pytest.fail(str(exc), pytrace=False)
        stack._email_people = seeded
    return seeded


@pytest.fixture(autouse=True)
def _no_unmatched_marked_requests(request):
    """The lean conftest's check, for the email files on this shape: a marked
    request this test sent that found no scripted turn fails the test."""
    module = request.module.__name__.rsplit(".", 1)[-1]
    if not module.startswith("test_email_") or "stack" not in request.fixturenames:
        yield
        return
    stack = request.getfixturevalue("stack")
    ours = email_flow.sent_markers(stack)
    ours.clear()
    yield
    unmatched = email_people_support.unmatched_marked_requests(stack, ours)
    assert not unmatched, email_people_support.describe_unmatched(unmatched)
