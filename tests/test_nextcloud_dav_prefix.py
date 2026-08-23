"""`[nextcloud] dav_prefix`: where a logical path becomes an HTTP path.

The bot's storage root and the root of its Nextcloud file tree are the same
directory on bare metal — the rclone remote points at
``remote.php/dav/files/<bot>/`` and is mounted at ``nextcloud_mount_path``, so
``/Users/alice`` on disk is ``/Users/alice`` over DAV. On the Docker shape they
are not: ``/mnt/shared`` is an ordinary volume that Nextcloud serves through a
``files_external`` mount named ``Shared Files``, so the same directory is
``/Shared Files/Users/alice`` to the bot. Everything over the POSIX mount worked
regardless; only the sharing call and the ``nextcloud`` skill go over HTTP, and
both were broken on that shape.

``dav_prefix`` carries the difference. Two constraints shape where it may be
applied, and both are asserted here rather than left as prose:

* **not** on ``storage.BOT_USER_BASE``. ``_get_mount_path`` builds on-disk paths
  from the same helper, so prefixing the constant would write to
  ``/mnt/shared/Shared Files/Users/…`` and break every filesystem write.
* **not** in ``resolve_scoped_path``. That is the confinement boundary keeping
  the skill inside the calling user's workspace, and it keeps speaking the
  logical ``/Users/{uid}`` vocabulary whatever the prefix is.

So the prefix lives in the request layer, applied where a path is handed to the
server (the DAV URL builder, the SEARCH scope href, the OCS share ``path``) and
stripped again where one comes back (``href_to_path``). Default empty, which is
what keeps bare metal and Ansible unchanged.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from istota.config import Config, NextcloudConfig

REPO = Path(__file__).resolve().parents[1]

PREFIX = "Shared Files"


def _config(prefix: str = "") -> Config:
    return Config(
        nextcloud=NextcloudConfig(
            url="https://cloud.example.com",
            username="istota",
            app_password="secret",
            dav_prefix=prefix,
        )
    )


def _ocs_ok(data):
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = {
        "ocs": {"meta": {"statuscode": 200, "message": "OK"}, "data": data}
    }
    resp.text = ""
    return resp


class TestTheConfigKeys:
    def test_both_default_to_the_bare_metal_behaviour(self):
        assert NextcloudConfig().dav_prefix == ""
        assert NextcloudConfig().auto_share_bot_dir is True

    def test_they_load_from_the_nextcloud_block(self, tmp_path):
        from istota.config import load_config

        path = tmp_path / "config.toml"
        path.write_text(
            '[nextcloud]\nurl = "https://cloud.example.com"\n'
            f'dav_prefix = "{PREFIX}"\nauto_share_bot_dir = false\n'
        )
        config = load_config(path)

        assert config.nextcloud.dav_prefix == PREFIX
        assert config.nextcloud.auto_share_bot_dir is False


class TestTheDavUrl:
    def test_an_empty_prefix_leaves_the_url_exactly_as_it_was(self):
        from istota.nextcloud._http import dav_files_url

        assert dav_files_url(_config(), "/Users/alice/a.txt") == (
            "https://cloud.example.com/remote.php/dav/files/istota/Users/alice/a.txt"
        )

    def test_a_prefix_lands_between_the_account_and_the_logical_path(self):
        from istota.nextcloud._http import dav_files_url

        assert dav_files_url(_config(PREFIX), "/Users/alice/a.txt") == (
            "https://cloud.example.com/remote.php/dav/files/istota/"
            "Shared%20Files/Users/alice/a.txt"
        )

    def test_the_bare_root_becomes_the_prefix_itself(self):
        from istota.nextcloud._http import dav_files_url

        assert dav_files_url(_config(PREFIX), "") == (
            "https://cloud.example.com/remote.php/dav/files/istota/Shared%20Files"
        )

    @pytest.mark.parametrize("written", ["Shared Files", "/Shared Files", "Shared Files/"])
    def test_surrounding_slashes_in_the_configured_value_do_not_reach_the_url(
        self, written
    ):
        """An operator writing `/Shared Files` means the same mount. Without
        normalizing, that renders `//Shared%20Files//Users/...`, which Sabre
        answers 404 for."""
        from istota.nextcloud._http import dav_files_url

        assert dav_files_url(_config(written), "/Users/alice") == (
            "https://cloud.example.com/remote.php/dav/files/istota/"
            "Shared%20Files/Users/alice"
        )

    def test_another_accounts_tree_is_not_prefixed(self):
        """The prefix names a mount in the *bot's* file tree. A URL built for
        somebody else's account is a different tree, where that mount point
        does not exist."""
        from istota.nextcloud._http import dav_files_url

        assert dav_files_url(_config(PREFIX), "/x.txt", username="alice") == (
            "https://cloud.example.com/remote.php/dav/files/alice/x.txt"
        )

    def test_naming_the_bot_explicitly_still_prefixes(self):
        """The rule is which account, not whether the argument was passed.
        Threading the bot's own name through — the natural thing to write — must
        not silently produce an unprefixed URL."""
        from istota.nextcloud._http import dav_files_url

        assert dav_files_url(_config(PREFIX), "/x.txt", username="istota") == (
            "https://cloud.example.com/remote.php/dav/files/istota/"
            "Shared%20Files/x.txt"
        )

    def test_the_account_root_can_be_addressed_on_purpose(self):
        """`prefixed=False` is the opt-out the two account-level callers take."""
        from istota.nextcloud._http import dav_files_url

        assert dav_files_url(_config(PREFIX), "", prefixed=False) == (
            "https://cloud.example.com/remote.php/dav/files/istota"
        )


class TestTheAccountLevelCallers:
    """Two callers whose subject is the account, not the storage root.

    Both were silently redirected onto the mount by a blanket prefix, and
    neither would have failed loudly: the quota query answers with the
    underlying filesystem's numbers, and the status file writes successfully to
    a place nothing reads.
    """

    def test_quota_asks_the_account_not_the_mount(self):
        from istota.nextcloud import dav

        resp = MagicMock(status_code=207, text=(
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:"><d:response>'
            "<d:propstat><d:prop><d:quota-available-bytes>10</d:quota-available-bytes>"
            "<d:quota-used-bytes>5</d:quota-used-bytes></d:prop></d:propstat>"
            "</d:response></d:multistatus>"
        ))
        with patch("istota.nextcloud._http.httpx.request", return_value=resp) as request:
            assert dav.quota(_config(PREFIX))["total_bytes"] == 15

        assert request.call_args[0][1] == (
            "https://cloud.example.com/remote.php/dav/files/istota"
        )

    def test_the_status_file_goes_to_the_account_root(self):
        """An in-Nextcloud app reads it through `getUserFolder`, which does not
        look inside the mount — and a prefixed MKCOL would also leave a stray
        `config/` on the shared volume beside `Users/` and `Channels/`."""
        from istota import status_writer

        resp = MagicMock(status_code=201, text="")
        with patch("istota.nextcloud._http.httpx.request", return_value=resp) as request:
            status_writer.write_status(_config(PREFIX), 0, 0, 0)

        urls = [call[0][1] for call in request.call_args_list]
        assert urls == [
            "https://cloud.example.com/remote.php/dav/files/istota/config",
            "https://cloud.example.com/remote.php/dav/files/istota/config/status.json",
        ]


class TestTheReturnTrip:
    """A path the server names has to come back as the logical one.

    `list_dir` filters the requested collection out of a depth-1 PROPFIND by
    comparing the parsed path against the one it asked for, so a parse that kept
    the prefix would leave the directory itself in every listing. The skill's
    output is the other half: it speaks `/Users/{uid}` on the way in, and a
    caller cannot feed a `/Shared Files/...` answer back to a verb that
    `resolve_scoped_path` guards.
    """

    def _multistatus(self, *hrefs: str) -> str:
        responses = "".join(
            f"<d:response><d:href>{href}</d:href><d:propstat>"
            "<d:status>HTTP/1.1 200 OK</d:status>"
            "<d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop>"
            "</d:propstat></d:response>"
            for href in hrefs
        )
        return (
            '<?xml version="1.0"?><d:multistatus xmlns:d="DAV:" '
            'xmlns:oc="http://owncloud.org/ns" xmlns:nc="http://nextcloud.org/ns">'
            f"{responses}</d:multistatus>"
        )

    def test_the_prefix_is_stripped_back_off_an_href(self):
        from istota.nextcloud.dav import href_to_path

        assert href_to_path(
            _config(PREFIX),
            "/remote.php/dav/files/istota/Shared%20Files/Users/alice/a.txt",
        ) == "/Users/alice/a.txt"

    def test_a_sibling_whose_name_merely_starts_with_it_is_left_alone(self):
        """A bare `startswith` turned `Shared Files Backup/x` into `/ Backup/x`
        — a plausible-looking wrong path, not an error. Tolerable while the
        prefix was only the account name; the mount point is operator-typed."""
        from istota.nextcloud.dav import href_to_path

        assert href_to_path(
            _config(PREFIX),
            "/remote.php/dav/files/istota/Shared%20Files%20Backup/x",
        ) == "/Shared Files Backup/x"

    def test_the_prefix_itself_comes_back_as_the_root(self):
        from istota.nextcloud.dav import href_to_path

        assert href_to_path(
            _config(PREFIX), "/remote.php/dav/files/istota/Shared%20Files"
        ) == "/"

    def test_without_a_prefix_the_href_is_read_the_way_it_always_was(self):
        from istota.nextcloud.dav import href_to_path

        assert href_to_path(
            _config(), "/remote.php/dav/files/istota/Users/alice/a.txt"
        ) == "/Users/alice/a.txt"

    def test_a_listing_drops_the_collection_it_asked_for(self):
        from istota.nextcloud import dav

        body = self._multistatus(
            "/remote.php/dav/files/istota/Shared%20Files/Users/alice",
            "/remote.php/dav/files/istota/Shared%20Files/Users/alice/a.txt",
        )
        resp = MagicMock(status_code=207, text=body)
        with patch("istota.nextcloud._http.httpx.request", return_value=resp):
            entries = dav.list_dir(_config(PREFIX), "/Users/alice")

        assert [entry["path"] for entry in entries] == ["/Users/alice/a.txt"]


class TestTheSearchScope:
    def test_the_scope_href_carries_the_prefix_percent_encoded(self):
        """An href is a URI reference, and the URL builder next door already
        quotes its path. The scope did not, which went unnoticed while the only
        way in was a user's own file name — the mount point puts a space in
        every search on the shipped Docker shape."""
        from istota.nextcloud.dav import build_search_body

        body = build_search_body(_config(PREFIX), scope="/Users/alice", name="*.md")

        assert "<d:href>/files/istota/Shared%20Files/Users/alice</d:href>" in body

    def test_a_caller_scope_is_encoded_too(self):
        from istota.nextcloud.dav import build_search_body

        body = build_search_body(_config(), scope="/Users/alice/my notes", name="*.md")

        assert "<d:href>/files/istota/Users/alice/my%20notes</d:href>" in body

    def test_a_query_marker_cannot_truncate_the_href(self):
        """`?` and `#` are legal in a Nextcloud external-mount display name and
        in a file name, and raw in an href each ends the path."""
        from istota.nextcloud.dav import build_search_body

        body = build_search_body(_config(), scope="/Users/alice/q?x#y", name="*.md")

        assert "<d:href>/files/istota/Users/alice/q%3Fx%23y</d:href>" in body

    def test_without_a_prefix_a_plain_scope_is_unchanged(self):
        from istota.nextcloud.dav import build_search_body

        body = build_search_body(_config(), scope="/Users/alice", name="*.md")

        assert "<d:href>/files/istota/Users/alice</d:href>" in body


class TestTheOcsSharePath:
    """OCS names a file by a path relative to the sharer's own root, so it
    needs the same mapping the DAV URL does — the share endpoint takes no href.
    """

    def test_create_share_sends_the_prefixed_path(self):
        from istota.nextcloud import shares

        with patch(
            "istota.nextcloud._http.httpx.post", return_value=_ocs_ok({"id": 1})
        ) as post:
            shares.create_share(_config(PREFIX), path="/Users/alice/a.txt", share_type=0)

        assert post.call_args.kwargs["data"]["path"] == "/Shared Files/Users/alice/a.txt"

    def test_create_share_is_untouched_without_a_prefix(self):
        from istota.nextcloud import shares

        with patch(
            "istota.nextcloud._http.httpx.post", return_value=_ocs_ok({"id": 1})
        ) as post:
            shares.create_share(_config(), path="/Users/alice/a.txt", share_type=0)

        assert post.call_args.kwargs["data"]["path"] == "/Users/alice/a.txt"

    def test_list_shares_filters_on_the_prefixed_path(self):
        from istota.nextcloud import shares

        with patch(
            "istota.nextcloud._http.httpx.get", return_value=_ocs_ok([])
        ) as get:
            shares.list_shares(_config(PREFIX), path="/Users/alice/a.txt")

        assert get.call_args.kwargs["params"]["path"] == "/Shared Files/Users/alice/a.txt"

    def test_the_legacy_shim_maps_the_path_too(self):
        """`storage.share_folder_with_user` goes through the shim, not through
        `shares.py`, so a mapping applied only to the latter would leave the
        one call the Docker shape makes on every boot still broken."""
        from istota.nextcloud_client import ocs_create_share, ocs_list_shares

        with patch(
            "istota.nextcloud._http.httpx.post", return_value=_ocs_ok({"id": 1})
        ) as post:
            ocs_create_share(_config(PREFIX), "/Users/alice/istota", 0, share_with="alice")
        with patch(
            "istota.nextcloud._http.httpx.get", return_value=_ocs_ok([])
        ) as get:
            ocs_list_shares(_config(PREFIX), path="/Users/alice/istota")

        assert post.call_args.kwargs["data"]["path"] == "/Shared Files/Users/alice/istota"
        assert get.call_args.kwargs["params"]["path"] == "/Shared Files/Users/alice/istota"


class TestTheOcsShareResponse:
    """The mapping has to be inverted, not just applied.

    The skill reads these rows and speaks logical paths in every other verb, so
    a prefixed `path` coming out of `share list` is a value none of them
    accepts: `resolve_scoped_path` refuses it as outside the caller's
    workspace, and for an admin — whom it lets through — it is prefixed a
    second time on the way back out.
    """

    def test_a_row_the_bot_owns_comes_back_logical(self):
        from istota.nextcloud import shares

        row = {"id": 1, "uid_owner": "istota", "path": "/Shared Files/Users/alice/a.txt"}
        with patch("istota.nextcloud._http.httpx.get", return_value=_ocs_ok([row])):
            listed = shares.list_shares(_config(PREFIX))

        assert listed[0]["path"] == "/Users/alice/a.txt"

    def test_a_created_share_reports_the_logical_path(self):
        from istota.nextcloud import shares

        created = {"id": 2, "uid_owner": "istota", "path": "/Shared Files/Users/alice/a.txt"}
        with patch("istota.nextcloud._http.httpx.post", return_value=_ocs_ok(created)):
            answer = shares.create_share(
                _config(PREFIX), path="/Users/alice/a.txt", share_type=0
            )

        assert answer["path"] == "/Users/alice/a.txt"

    def test_a_row_somebody_else_owns_is_left_exactly_as_it_came(self):
        """A `shared_with_me` row names a path in the *recipient's* tree, which
        the bot's mount point has nothing to do with. Stripping a coincidental
        match there would corrupt it."""
        from istota.nextcloud import shares

        row = {"id": 3, "uid_owner": "alice", "path": "/Shared Files/notes.md"}
        with patch("istota.nextcloud._http.httpx.get", return_value=_ocs_ok([row])):
            listed = shares.list_shares(_config(PREFIX), shared_with_me=True)

        assert listed[0]["path"] == "/Shared Files/notes.md"

    def test_the_round_trip_is_stable(self):
        """The property the inversion exists for: what `share list` hands back
        is what `share revoke --path` can be given."""
        from istota.nextcloud._http import from_remote_path, to_remote_path

        config = _config(PREFIX)
        logical = "/Users/alice/a.txt"
        remote = to_remote_path(config, logical)

        assert remote == "/Shared Files/Users/alice/a.txt"
        assert from_remote_path(config, remote) == logical
        assert from_remote_path(config, logical) == logical

    def test_the_legacy_shim_inverts_too(self):
        from istota.nextcloud_client import ocs_create_share, ocs_list_shares

        row = {"id": 4, "uid_owner": "istota", "path": "/Shared Files/Users/alice/istota"}
        with patch("istota.nextcloud._http.httpx.get", return_value=_ocs_ok([row])):
            listed = ocs_list_shares(_config(PREFIX))
        with patch("istota.nextcloud._http.httpx.post", return_value=_ocs_ok(row)):
            created = ocs_create_share(_config(PREFIX), "/Users/alice/istota", 0)

        assert listed[0]["path"] == "/Users/alice/istota"
        assert created["path"] == "/Users/alice/istota"

    def test_nothing_is_rewritten_without_a_prefix(self):
        from istota.nextcloud import shares

        row = {"id": 5, "uid_owner": "istota", "path": "/Users/alice/a.txt"}
        with patch("istota.nextcloud._http.httpx.get", return_value=_ocs_ok([row])):
            listed = shares.list_shares(_config())

        assert listed[0]["path"] == "/Users/alice/a.txt"


class TestTheTalkAttachment:
    def test_share_file_sends_the_prefixed_path(self, monkeypatch):
        """A Talk attachment is share type 10 on the same OCS endpoint `share
        create` uses, posted by `TalkClient` rather than by `shares.py` — so it
        needed the mapping separately, and was the one writer of an OCS share
        path the first pass missed."""
        from istota.skills import nextcloud as skill

        monkeypatch.setenv("NC_URL", "https://cloud.example.com")
        monkeypatch.setenv("NC_USER", "istota")
        monkeypatch.setenv("NC_PASS", "secret")
        monkeypatch.setenv("NC_DAV_PREFIX", PREFIX)
        monkeypatch.setenv("ISTOTA_USER_ID", "alice")

        seen = {}

        def _fake_run(fn):
            class _Client:
                async def share_file(self, token, path):
                    seen["token"] = token
                    seen["path"] = path
                    return {"id": 7}

            import asyncio

            return asyncio.run(fn(_Client()))

        monkeypatch.setattr(skill, "_talk_run", _fake_run)
        args = argparse.Namespace(token="room1", path="a.txt")

        answer = skill.cmd_talk_share_file(args)

        assert seen["path"] == "/Shared Files/Users/alice/a.txt"
        assert answer["path"] == "/Users/alice/a.txt", (
            "the reply must name the path the caller asked about"
        )


class TestWhatThePrefixMustNotTouch:
    def test_the_on_disk_path_is_built_without_it(self):
        """The constraint that decided where the prefix goes. `_get_mount_path`
        and the DAV URL start from the same logical string; prefixing the
        shared helper would write to `/mnt/shared/Shared Files/Users/…`."""
        from istota import storage

        config = _config(PREFIX)
        config.nextcloud_mount_path = Path("/mnt/shared")

        assert storage._get_mount_path(
            config, storage.get_user_base_path("alice")
        ) == Path("/mnt/shared/Users/alice")

    def test_the_confinement_boundary_keeps_the_logical_vocabulary(self):
        from istota.nextcloud._http import PathScopeError, resolve_scoped_path

        assert resolve_scoped_path("notes.md", "alice") == "/Users/alice/notes.md"
        assert resolve_scoped_path("/Users/alice", "alice") == "/Users/alice"
        with pytest.raises(PathScopeError):
            resolve_scoped_path("/Users/bob/secret.txt", "alice")
        with pytest.raises(PathScopeError):
            resolve_scoped_path(f"/{PREFIX}/Users/alice/notes.md", "alice")


class TestTheSkillCarriesIt:
    """The skill CLI runs as a subprocess with an env manifest, not with the
    daemon's Config. A key absent from the manifest is a key the skill does not
    have, which is exactly how `files` and `share` would have stayed broken.
    """

    def test_the_manifest_declares_the_variable(self):
        from istota.skills._loader import _load_skill_meta

        skill = _load_skill_meta(REPO / "src" / "istota" / "skills" / "nextcloud")
        assert skill is not None
        declared = {spec.var: spec for spec in skill.env_specs}

        assert "NC_DAV_PREFIX" in declared, sorted(declared)
        assert declared["NC_DAV_PREFIX"].config_path == "nextcloud.dav_prefix"

    def test_the_cli_reads_it_out_of_the_environment(self, monkeypatch):
        from istota.skills.nextcloud import _config_from_env

        monkeypatch.setenv("NC_URL", "https://cloud.example.com")
        monkeypatch.setenv("NC_USER", "istota")
        monkeypatch.setenv("NC_PASS", "secret")
        monkeypatch.setenv("NC_DAV_PREFIX", PREFIX)

        assert _config_from_env().nextcloud.dav_prefix == PREFIX

    def test_an_absent_variable_leaves_the_prefix_empty(self, monkeypatch):
        from istota.skills.nextcloud import _config_from_env

        monkeypatch.setenv("NC_URL", "https://cloud.example.com")
        monkeypatch.setenv("NC_USER", "istota")
        monkeypatch.setenv("NC_PASS", "secret")
        monkeypatch.delenv("NC_DAV_PREFIX", raising=False)

        assert _config_from_env().nextcloud.dav_prefix == ""


class TestTheAutoShareGuard:
    """Row 2 of the brief's table. The `files_external` mount the provisioning
    creates already puts the bot workspace in the user's tree; the OCS share
    would put it there a second time, under a different name.
    """

    def _seeded(self, tmp_path, *, auto_share: bool) -> Config:
        config = _config()
        config.nextcloud.auto_share_bot_dir = auto_share
        config.nextcloud_mount_path = tmp_path
        return config

    def test_the_share_is_made_when_the_key_is_left_alone(self, tmp_path):
        from istota import storage

        config = self._seeded(tmp_path, auto_share=True)
        with patch.object(storage, "share_folder_with_user") as share:
            assert storage.ensure_user_directories_v2(config, "alice")

        assert share.call_count == 1
        assert share.call_args[0][1] == "/Users/alice/istota"

    def test_the_share_is_suppressed_when_the_key_is_false(self, tmp_path):
        from istota import storage

        config = self._seeded(tmp_path, auto_share=False)
        with patch.object(storage, "share_folder_with_user") as share:
            assert storage.ensure_user_directories_v2(config, "alice")

        assert share.call_count == 0
        assert (tmp_path / "Users" / "alice" / "istota").is_dir(), (
            "suppressing the share must not suppress the directory seeding"
        )


def test_the_provisioning_enables_sharing_on_both_mounts():
    """Without it Nextcloud refuses every share of everything under
    `/mnt/shared` — `files_external`'s `enable_sharing` defaults to false — so
    the bot cannot share a file it produced and `share link` is dead.

    The pair is the assertion. Enabling it on the bot's mount alone would leave
    the human user unable to share out of their own workspace view, which is a
    second mount over the same bytes.
    """
    script = (REPO / "docker" / "istota" / "provision-nc.sh").read_text()

    options = re.findall(
        r'files_external:option\s+"\$\{(\w+)\}"\s+enable_sharing\s+true', script
    )

    assert options == ["MOUNT_ID", "USER_MOUNT_ID"], options


def test_the_dav_prefix_is_documented_where_an_operator_looks():
    example = (REPO / "config" / "config.example.toml").read_text()
    reference = (REPO / "docs" / "configuration" / "reference.md").read_text()

    assert "dav_prefix" in example
    assert "auto_share_bot_dir" in example
    assert "dav_prefix" in reference
    assert "auto_share_bot_dir" in reference


def test_the_skill_manifest_json_stays_parseable():
    """The manifest is one JSON array on a frontmatter line; a hand edit that
    breaks it fails at skill-load time, in the daemon, not here."""
    line = next(
        raw for raw in
        (REPO / "src" / "istota" / "skills" / "nextcloud" / "skill.md").read_text().splitlines()
        if raw.startswith("env: ")
    )

    assert isinstance(json.loads(line[len("env: "):]), list)
