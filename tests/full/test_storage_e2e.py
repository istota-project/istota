"""Storage, shares and notifications against the Nextcloud the deployment ships.

`src/istota/nextcloud/` is four modules of HTTP client — WebDAV, the sharing
OCS API, the notifications OCS API, the provisioning API — and every test it has
in the default suite asserts against a shape we chose ourselves. The two suites
that use a real server (`tests/test_nextcloud_client_integration.py`,
`tests/test_nextcloud_skill_live.py`) need a hand-configured external Nextcloud
and are deselected by default. This file runs the same code inside the shipped
container, against the server the shipped `provision-nc.sh` set up.

**The one thing to understand before reading any assertion below.** On this
shape `storage.py` never speaks WebDAV. `render-config.sh:119` writes
`nextcloud_mount_path = "/mnt/shared"` unconditionally, so `Config.use_mount` is
true and every write is an ordinary POSIX write onto a Docker volume. Nextcloud
reaches the same bytes through two `files_external` *local* mounts that
`provision-nc.sh:56-79` creates: the whole volume to the bot at `Shared Files/`,
and only the bot workspace directory to the human user, at the bot's name. So
the round trip this file asserts is **filesystem in, WebDAV out**, and the path
changes on the way: `/Users/testuser/inbox/x` on disk is
`Shared Files/Users/testuser/inbox/x` to the bot and is not in the human user's
tree at all. Measured here rather than assumed, because nothing else in the repo
states it and the mount points are chosen in a shell script nothing executes.

**Why so much runs through `stack.exec` into the container.** The code under
test is a client, and what makes it worth testing is the pair — the client and
the server it was written against. Running it in the container gets the shipped
config, the shipped credentials and the real network path for free; driving it
from the host would need all three re-derived, and would then be asserting
against a configuration no deployment has.
"""

from __future__ import annotations

import json
import time
import uuid

import pytest

from testbed.services.nextcloud import BOT_MOUNT_POINT

pytestmark = pytest.mark.full

FULL = pytest.mark.profile("full")

CONTAINER_CONFIG = "/data/config/config.toml"

#: The preamble every in-container snippet shares: the shipped config, loaded
#: the way the daemon loads it. `sys.path` is not manipulated — the image
#: installs the package — but the config path is explicit, because `load_config`
#: searches and a container has more than one candidate.
_PREAMBLE = (
    "import json, pathlib;"
    "from istota.config import load_config;"
    f"c = load_config(pathlib.Path('{CONTAINER_CONFIG}'));"
)

#: How long a Talk notification may take to appear. Nextcloud raises it as part
#: of handling the invite, so this is slack rather than a wait for a poller.
NOTIFICATION_TIMEOUT = 60


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _run(stack, snippet: str) -> str:
    """One Python snippet inside the istota container, or a readable failure."""
    result = stack.exec(
        ["uv", "run", "python", "-c", _PREAMBLE + snippet], timeout=180
    )
    assert result.returncode == 0, (
        f"the snippet exited {result.returncode}\n--- stdout ---\n{result.stdout}"
        f"\n--- stderr ---\n{result.stderr}"
    )
    return result.stdout


def _tagged(output: str, tag: str) -> str:
    for line in output.splitlines():
        if line.startswith(tag + " "):
            return line[len(tag) + 1:]
    raise AssertionError(f"no {tag!r} line in:\n{output}")


@FULL
class TestAFileWrittenThroughStorage:
    def test_it_is_served_over_webdav_from_the_external_mount(self, stack):
        """The write is POSIX, the read is WebDAV, and nothing rescans in between.

        Worth stating because it is the part that could plausibly not work:
        Nextcloud is being told about a file that appeared underneath it without
        its knowledge, and nothing in this repo runs `occ files:scan` or
        configures a watcher. It turns out a PROPFIND of the parent picks the
        file up immediately, which is what makes every other storage assertion
        in this tier possible.
        """
        nextcloud = stack.service("nextcloud")
        name = _unique("note") + ".txt"
        body = f"written by storage.py for {name}"

        remote = _tagged(_run(stack, (
            "from istota import storage;"
            f"p = pathlib.Path('/tmp/{name}');"
            f"p.write_text({body!r});"
            "print('REMOTE', storage.upload_file_to_inbox_v2(c, 'testuser', p))"
        )), "REMOTE").strip()

        assert remote == f"/Users/testuser/inbox/{name}", remote
        dav_path = f"{BOT_MOUNT_POINT}{remote}"
        listing = nextcloud.files(f"{BOT_MOUNT_POINT}/Users/testuser/inbox")
        assert dav_path in listing, (
            f"{remote} is on the volume but not in the bot's Nextcloud tree; "
            f"the inbox holds {listing}"
        )
        assert nextcloud.read_file(dav_path).decode() == body

    def test_the_two_trees_are_scoped_the_way_the_provisioning_intends(self, stack):
        """The human user gets the bot workspace and nothing else.

        `provision-nc.sh:66-71` is emphatic that the user's mount is the bot
        *workspace directory* and never the user base, because the base holds
        `inbox/`, `memories/` and `shared/`, which are bot-internal. Stage 3
        asserted the mount rows; this asserts the consequence, which is the
        thing an operator would actually notice. `bot_dir_name` is read out of
        the running config rather than written down here: the mount point is
        the *display* name (`ISTOTA_BOT_NAME`) while the directory is the
        sanitized one, and a test hardcoding either would pass for the wrong
        reason on a rename.
        """
        nextcloud = stack.service("nextcloud")
        bot_dir = _tagged(_run(stack, "print('BOTDIR', c.bot_dir_name)"), "BOTDIR").strip()

        user_tree = nextcloud.files("", user=nextcloud.test_user, depth="1")
        bot_tree = nextcloud.files("", user=nextcloud.bot_user, depth="1")

        assert BOT_MOUNT_POINT in bot_tree, bot_tree
        assert BOT_MOUNT_POINT not in user_tree, (
            "the human user can see the whole shared volume" , user_tree
        )
        for private in ("inbox", "memories", "shared", "Users", "Channels"):
            assert private not in user_tree, (private, user_tree)
        wanted = {bot_dir.lower(), bot_dir.replace("_", " ").lower()}
        workspace = [entry for entry in user_tree if entry.lower() in wanted]
        assert workspace, (
            f"the human user cannot see the bot workspace ({bot_dir}): {user_tree}"
        )
        # `files()` drops the requested collection, so this is "the mount has
        # contents" rather than "the mount was asked for" — the difference
        # between an assertion and a no-op.
        assert nextcloud.files(workspace[0], user=nextcloud.test_user), (
            "the bot workspace mount resolves to an empty directory"
        )


@FULL
class TestASharedFile:
    def test_it_reaches_the_intended_user_and_nobody_else(self, stack):
        """`nextcloud/shares.py`, against the server rather than against a mock.

        The file is uploaded into the bot's **own** Nextcloud home rather than
        onto the shared volume, and that is forced rather than chosen: a file on
        a `files_external` mount is refused with "You are not allowed to share",
        measured while this was written. It is the same constraint that makes
        `storage.share_folder_with_user` fail on every boot of this shape — see
        the stage notes; that is a product question this stage does not settle
        and deliberately does not encode as an expectation.
        """
        nextcloud = stack.service("nextcloud")
        name = _unique("shared") + ".txt"

        _run(stack, (
            "from istota.nextcloud import dav;"
            f"p = pathlib.Path('/tmp/{name}');"
            "p.write_text('a file the bot owns');"
            f"print('UPLOAD', json.dumps(dav.upload(c, p, '/{name}')))"
        ))
        share = json.loads(_tagged(_run(stack, (
            "from istota.nextcloud import shares;"
            "print('SHARE', json.dumps(shares.create_share("
            f"c, path='/{name}', share_type=shares.SHARE_TYPES['user'],"
            f" share_with='{nextcloud.test_user}', permissions=17)))"
        )), "SHARE"))

        assert share["share_with"] == nextcloud.test_user, share
        assert share["uid_owner"] == nextcloud.bot_user, share

        received = {
            row.get("path") for row in
            nextcloud.shares(user=nextcloud.test_user, shared_with_me=True)
        }
        assert f"/{name}" in received, received
        others = {
            row.get("path") for row in
            nextcloud.shares(user=nextcloud.admin_user, shared_with_me=True)
        }
        assert f"/{name}" not in others, others
        assert name in nextcloud.files("", user=nextcloud.test_user), (
            "the share exists but the file is not in the recipient's tree"
        )


@FULL
class TestNotifications:
    def test_the_client_reads_a_notification_the_deployment_raised(self, stack):
        """`nextcloud/notifications.py` is a *read* client, and that is the test.

        The spec's line for this scenario says "a notification sent through
        `nextcloud/notifications.py`". There is no send path: that module's own
        docstring records the decision — sending needs the `admin_notifications`
        app and admin rights, and the bot already has two working push channels
        to its own user — so it exposes list, get, dismiss and dismiss-all and
        nothing else. Inventing a fixture-side POST to an app the deployment
        does not enable would assert nothing about istota. What is real is that
        the deployment generates notifications on its own: inviting the bot to a
        room raises one, and this is the client the skill CLI reads it with.
        """
        nextcloud = stack.service("nextcloud")
        name = _unique("notify")

        nextcloud.create_room(name=name, participants=[nextcloud.bot_user])

        # `limit=0` means "no client-side slice", and it is not a stylistic
        # choice: the default is 25, the bot accumulates a notification per room
        # invite and per message for the whole session, and a test whose
        # notification fell off the end of that slice would report that it was
        # never raised.
        deadline = time.monotonic() + NOTIFICATION_TIMEOUT
        seen: list[list] = []
        while time.monotonic() < deadline:
            seen = json.loads(_tagged(_run(stack, (
                "from istota.nextcloud import notifications;"
                "print('NOTIFY', json.dumps([(r.get('app'), r.get('subject'))"
                " for r in notifications.list_notifications(c, limit=0)]))"
            )), "NOTIFY"))
            if any(name in (subject or "") for _, subject in seen):
                break
            time.sleep(2)

        assert any(name in (subject or "") for _, subject in seen), (
            f"no notification for {name!r} reached the bot through "
            f"nextcloud/notifications.py: {seen}"
        )
        assert any(app == "spreed" for app, _ in seen), seen
        # The same fact read a second way, from outside the container and
        # without going through the client under test. A client that filtered,
        # truncated or reshaped its answer would agree with itself and disagree
        # with this.
        direct = nextcloud.notifications(nextcloud.bot_user)
        assert any(name in (row.get("subject") or "") for row in direct), (
            "the client saw the notification and a plain OCS read did not: "
            f"{[(row.get('app'), row.get('subject')) for row in direct]}"
        )
