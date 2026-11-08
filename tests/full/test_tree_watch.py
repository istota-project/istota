"""The tree watcher's premise, and the watcher itself, against real Nextcloud 30.

`src/istota/nextcloud/tree_watch.py` decides whether anything under the bot's
root changed by comparing one folder's ETag with the one it stored (#939). That
holds only if Nextcloud changes the root's ETag for the two kinds of change the
pollers wait for, so both are measured here first:

1. A user editing their TASKS.md or CRON.md through the share of their bot
   directory (what `auto_share_bot_dir` makes) changes the bot root's ETag.
   The write lands in the bot's own storage, so this is owner-side propagation.
2. A new share to the bot, which appears as a new entry at the bot's root,
   changes that root's ETag. This is recipient-side: the bytes are in the
   sharer's storage, and the bot's root gains only a share mount.

The files here live in the bot's *home* storage, written over WebDAV, as the
rclone mount writes them on a VM. The full shape's own workspace is a
`files_external` local mount (`Shared Files/`), which no install ships, so it
is not used: each test makes its own folder under the bot's home and points a
config's `dav_prefix` at it, so the watcher's root is that folder.

Discretionary: `uv run pytest -m full -n0 tests/full/test_tree_watch.py`.
"""

from __future__ import annotations

import uuid
from xml.etree import ElementTree as ET

import pytest

pytestmark = [pytest.mark.full, pytest.mark.profile("full")]

_ETAG_BODY = (
    b'<?xml version="1.0"?>\n'
    b'<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>\n'
)
_NS = {"d": "DAV:"}


def _etag(nextcloud, path: str, *, user: str = "") -> str:
    body, status = nextcloud._dav(
        path, user=user or nextcloud.bot_user, method="PROPFIND", body=_ETAG_BODY,
        headers={"Depth": "0", "Content-Type": "application/xml"},
    )
    assert status == 207, (path, status, body[:300])
    node = ET.fromstring(body).find(".//d:getetag", _NS)
    assert node is not None and node.text, (path, body[:300])
    return node.text.strip('"')


def _mkcol(nextcloud, path: str, *, user: str = "") -> None:
    _body, status = nextcloud._dav(path, user=user or nextcloud.bot_user, method="MKCOL")
    assert status == 201, (path, status)


def _put(nextcloud, path: str, text: str, *, user: str = "") -> None:
    _body, status = nextcloud._dav(
        path, user=user or nextcloud.bot_user, method="PUT", body=text.encode(),
    )
    assert status in (201, 204), (path, status)


def _delete(nextcloud, path: str, *, user: str = "") -> None:
    nextcloud._dav(path, user=user or nextcloud.bot_user, method="DELETE")


def _share(nextcloud, path: str, *, owner: str, to: str) -> dict:
    answer = nextcloud._ocs(
        "/ocs/v2.php/apps/files_sharing/api/v1/shares", user=owner, method="POST",
        body={"path": path, "shareType": 0, "shareWith": to, "permissions": 31},
    )
    assert answer.status_code in (100, 200), answer.message
    share = answer.data
    if share.get("status") == 1:  # pending: accept it as the recipient
        accepted = nextcloud._ocs(
            f"/ocs/v2.php/apps/files_sharing/api/v1/shares/pending/{share['id']}",
            user=to, method="POST",
        )
        assert accepted.status_code in (100, 200), accepted.message
    return share


def _unshare(nextcloud, share_id, *, owner: str) -> None:
    nextcloud._ocs(
        f"/ocs/v2.php/apps/files_sharing/api/v1/shares/{share_id}",
        user=owner, method="DELETE", tolerate=(404,),
    )


@pytest.fixture
def bot_tree(stack):
    """A bot root of its own under the bot's home, with testuser's bot dir
    shared to testuser the way `auto_share_bot_dir` shares it."""
    nextcloud = stack.service("nextcloud")
    root = f"tw-{uuid.uuid4().hex[:10]}"
    user = nextcloud.test_user
    bot_dir = f"{root}/Users/{user}/istota"
    for path in (root, f"{root}/Users", f"{root}/Users/{user}", bot_dir,
                 f"{bot_dir}/config", f"{bot_dir}/scripts", f"{bot_dir}/scripts/prompts"):
        _mkcol(nextcloud, path)
    _put(nextcloud, f"{bot_dir}/config/TASKS.md", "- [ ] first\n")
    _put(nextcloud, f"{bot_dir}/config/CRON.md", "# jobs\n")
    _put(nextcloud, f"{bot_dir}/scripts/prompts/daily.txt", "say hi\n")
    share = _share(nextcloud, f"/{bot_dir}", owner=nextcloud.bot_user, to=user)
    try:
        yield nextcloud, root, bot_dir, share["file_target"].strip("/")
    finally:
        _unshare(nextcloud, share["id"], owner=nextcloud.bot_user)
        _delete(nextcloud, root)


class TestTheRootEtagMovesOnAUsersEdit:
    """Check (1): owner-side propagation, through the user's share."""

    @pytest.mark.parametrize("name", ["TASKS.md", "CRON.md"])
    def test_an_edit_through_the_share_changes_every_folder_above_it(self, bot_tree, name):
        nextcloud, root, bot_dir, target = bot_tree
        user = nextcloud.test_user
        chain = [root, f"{root}/Users", f"{root}/Users/{user}", bot_dir, f"{bot_dir}/config"]
        before = {path: _etag(nextcloud, path) for path in chain}
        scripts_before = _etag(nextcloud, f"{bot_dir}/scripts")

        _put(nextcloud, f"{target}/config/{name}", "- [ ] edited by the user\n", user=user)

        after = {path: _etag(nextcloud, path) for path in chain}
        unchanged = [path for path in chain if after[path] == before[path]]
        assert not unchanged, f"ETag did not move on {unchanged}"
        assert _etag(nextcloud, f"{bot_dir}/scripts") == scripts_before, (
            "a sibling's ETag moved too, so the walk could not tell config/ from scripts/"
        )

    def test_a_prompt_file_two_levels_down_changes_scripts(self, bot_tree):
        nextcloud, root, bot_dir, target = bot_tree
        root_before = _etag(nextcloud, root)
        scripts_before = _etag(nextcloud, f"{bot_dir}/scripts")

        _put(nextcloud, f"{target}/scripts/prompts/daily.txt", "say hello\n",
             user=nextcloud.test_user)

        assert _etag(nextcloud, f"{bot_dir}/scripts") != scripts_before
        assert _etag(nextcloud, root) != root_before


class TestTheRootEtagMovesOnANewShare:
    """Check (2): recipient-side, a new incoming share at the bot's root.

    The bot's home root, not the per-test folder: Nextcloud mounts a received
    share at the recipient's `share_folder`, which defaults to the root.
    """

    def test_a_file_shared_to_the_bot_changes_the_bots_home_root(self, stack):
        nextcloud = stack.service("nextcloud")
        user = nextcloud.test_user
        name = f"tw-share-{uuid.uuid4().hex[:10]}.txt"
        _put(nextcloud, name, "for the bot\n", user=user)
        before = _etag(nextcloud, "")
        share = _share(nextcloud, f"/{name}", owner=user, to=nextcloud.bot_user)
        try:
            after = _etag(nextcloud, "")
            listed = nextcloud.files("", user=nextcloud.bot_user)
            assert share["file_target"].strip("/") in listed, listed
            assert after != before, "a new share to the bot left its root ETag alone"
        finally:
            _unshare(nextcloud, share["id"], owner=user)
            _delete(nextcloud, name, user=user)

    def test_a_folder_shared_to_the_bot_changes_the_bots_home_root(self, stack):
        nextcloud = stack.service("nextcloud")
        user = nextcloud.test_user
        name = f"tw-folder-{uuid.uuid4().hex[:10]}"
        _mkcol(nextcloud, name, user=user)
        before = _etag(nextcloud, "")
        share = _share(nextcloud, f"/{name}", owner=user, to=nextcloud.bot_user)
        try:
            assert _etag(nextcloud, "") != before, (
                "a new folder share to the bot left its root ETag alone"
            )
        finally:
            _unshare(nextcloud, share["id"], owner=user)
            _delete(nextcloud, name, user=user)


# -- the watcher itself, in this process, against the same server -------------


def _watch_config(nextcloud, tmp_path, prefix: str):
    from istota.config import Config, NextcloudConfig, UserConfig

    return Config(
        db_path=tmp_path / "istota.db",
        nextcloud=NextcloudConfig(
            url=nextcloud.base_url, username=nextcloud.bot_user,
            app_password=nextcloud._passwords[nextcloud.bot_user], dav_prefix=prefix,
        ),
        users={nextcloud.test_user: UserConfig()},
        bot_name="istota",
        workspace_path=tmp_path, workspace_mount_path=tmp_path,
    )


@pytest.fixture
def watcher(monkeypatch):
    from istota.nextcloud import rclone_rc, tree_watch

    # No rc in this process: the watcher records the paths it could not
    # freshen and carries on, which is the case under test anyway.
    monkeypatch.delenv(rclone_rc.RC_ENV_VAR, raising=False)
    tree_watch.reset()
    yield tree_watch
    tree_watch.reset()


class TestTheWatcherAgainstNextcloud:
    def test_a_users_edit_moves_config_and_nothing_else(self, bot_tree, watcher, tmp_path):
        nextcloud, root, bot_dir, target = bot_tree
        config = _watch_config(nextcloud, tmp_path, root)
        user = nextcloud.test_user
        config_dir = f"/Users/{user}/istota/config"
        scripts_dir = f"/Users/{user}/istota/scripts"

        first = watcher.run_pass(config)
        assert first.error == "", first
        before = watcher.snapshot([config_dir, scripts_dir])
        assert None not in before

        quiet = watcher.run_pass(config)
        assert (quiet.requests, quiet.changed) == (1, [])

        _put(nextcloud, f"{target}/config/TASKS.md", "- [ ] from Nextcloud\n", user=user)
        changed = watcher.run_pass(config)
        after = watcher.snapshot([config_dir, scripts_dir])
        assert after[0] != before[0], changed
        assert after[1] == before[1], changed
        assert changed.changed == [config_dir]

    def test_a_new_share_moves_the_roots_generation_and_carries_its_owner(
        self, stack, watcher, tmp_path,
    ):
        nextcloud = stack.service("nextcloud")
        user = nextcloud.test_user
        config = _watch_config(nextcloud, tmp_path, "")
        config.users = {}
        assert watcher.run_pass(config).error == ""
        before = watcher.snapshot(["/"])

        name = f"tw-share-{uuid.uuid4().hex[:10]}.txt"
        _put(nextcloud, name, "for the bot\n", user=user)
        share = _share(nextcloud, f"/{name}", owner=user, to=nextcloud.bot_user)
        try:
            assert watcher.run_pass(config).error == ""
            assert watcher.snapshot(["/"]) != before
            entries = {e["name"]: e for e in watcher.root_listing()}
            target = share["file_target"].strip("/")
            assert entries[target]["owner_id"] == user
        finally:
            _unshare(nextcloud, share["id"], owner=user)
            _delete(nextcloud, name, user=user)

    def test_a_root_on_external_storage_is_not_trusted(self, stack, watcher, tmp_path):
        """The full shape's own workspace is a `files_external` mount; a change
        on its disk does not move its ETag until a folder is listed, so the
        watcher stands down there and every poller reads on its interval."""
        nextcloud = stack.service("nextcloud")
        config = _watch_config(nextcloud, tmp_path, "Shared Files")
        assert watcher.run_pass(config).error == "untrusted_storage"
        assert watcher.snapshot(["/"]) is None
