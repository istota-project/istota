"""Live tests for the nextcloud skill CLI against a real Nextcloud instance.

Run with:
    pytest -m integration tests/test_nextcloud_skill_live.py -v -n0

Requires: NC_URL, NC_USER, NC_PASS (an app password, not the login password).

Everything the default run writes lands under one scratch folder in the bot
account's own file tree, removed at teardown. Verbs that touch account-wide or
shared state are opt-in:

    NC_OTHER_USER=<uid>     user-to-user shares, Talk invites
    NC_TEST_ADMIN=1         admin-only lookups (user get/groups, group list/members)
    NC_TEST_TALK=1          create/rename/send in a throwaway conversation
    NC_TEST_DESTRUCTIVE=1   trash empty, notify dismiss-all (account-wide, irreversible)

The tests drive ``main(argv)`` rather than the module functions, so argparse,
path scoping, the error envelope and the exit code are all in the path — the
same surface the model reaches through the skill proxy.
"""

import io
import json
import os
import uuid
from datetime import datetime, timedelta, timezone
from contextlib import redirect_stdout

import pytest

from istota.config import Config, NextcloudConfig
from istota.nextcloud import _http, capabilities as caps_mod
from istota.skills.nextcloud import main

_url = os.environ.get("NC_URL", "")
_user = os.environ.get("NC_USER", "")
_pass = os.environ.get("NC_PASS", "")

_skip_reason = None
if not _url:
    _skip_reason = "NC_URL not set"
elif not _user:
    _skip_reason = "NC_USER not set"
elif not _pass:
    _skip_reason = "NC_PASS not set"

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(_skip_reason is not None, reason=_skip_reason or ""),
]

_OTHER_USER = os.environ.get("NC_OTHER_USER", "")
_WANT_ADMIN = os.environ.get("NC_TEST_ADMIN") == "1"
_WANT_TALK = os.environ.get("NC_TEST_TALK") == "1"
_WANT_DESTRUCTIVE = os.environ.get("NC_TEST_DESTRUCTIVE") == "1"

needs_other_user = pytest.mark.skipif(not _OTHER_USER, reason="NC_OTHER_USER not set")
needs_admin = pytest.mark.skipif(not _WANT_ADMIN, reason="NC_TEST_ADMIN != 1")
needs_talk = pytest.mark.skipif(not _WANT_TALK, reason="NC_TEST_TALK != 1")
needs_destructive = pytest.mark.skipif(
    not _WANT_DESTRUCTIVE, reason="NC_TEST_DESTRUCTIVE != 1"
)


# --- harness ---------------------------------------------------------------


def run(*argv):
    """Invoke the CLI. Returns (exit_code, parsed_stdout)."""
    buf = io.StringIO()
    code = 0
    try:
        with redirect_stdout(buf):
            main(list(argv))
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    raw = buf.getvalue().strip()
    payload = json.loads(raw) if raw else None
    return code, payload


def ok(*argv):
    """Invoke the CLI and assert it succeeded."""
    code, payload = run(*argv)
    assert code == 0, f"{' '.join(argv)} failed: {payload}"
    return payload


def fails(*argv):
    """Invoke the CLI and assert it reported a structured failure."""
    code, payload = run(*argv)
    assert code != 0, f"{' '.join(argv)} unexpectedly succeeded: {payload}"
    assert isinstance(payload, dict), f"non-dict failure payload: {payload!r}"
    return payload


def share_link(*argv):
    """`share link`, skipping when Nextcloud's share rate limit kicks in.

    Share creation is capped at 20 per 10 minutes per account, which a couple
    of back-to-back runs of this file will exhaust. That is the environment
    refusing, not the code failing, so it skips rather than reporting red.
    """
    code, payload = run("share", "link", *argv)
    if code != 0 and payload.get("http_status") == 429:
        pytest.skip("Nextcloud share rate limit hit (20 per 10 min) — retry later")
    assert code == 0, f"share link {' '.join(argv)} failed: {payload}"
    return payload


def _search_results(payload):
    assert payload["scope"], "search result carries no scope"
    assert payload["count"] == len(payload["results"])
    return payload["results"]


def untrusted(payload, key):
    """Unwrap a read that returns other people's content.

    Anything the skill reads back out of Nextcloud is wrapped in the untrusted
    envelope before the model sees it. Asserting the wrapper here is the point:
    a read that quietly stopped carrying it would be a security regression, not
    a cosmetic one.
    """
    assert payload["untrusted"] is True, f"{key} read is not untrusted-framed"
    assert payload["notice"], f"{key} read carries no untrusted notice"
    items = payload[key]
    assert payload["count"] == len(items)
    return items


@pytest.fixture(autouse=True)
def _unscoped_admin_env(monkeypatch, tmp_path):
    """Address the bot's own tree directly, with the credentials put back.

    No ISTOTA_USER_ID plus the empty-admins-file back-compat rule puts the CLI
    in unscoped-admin mode, so paths resolve at the account root instead of
    /Users/<uid>/. The scoping tests below override this deliberately.

    `run()` calls the skill's `main()` **in process**, and
    `istota/skills/nextcloud/__init__.py` reads NC_URL/NC_USER/NC_PASS out of
    `os.environ` itself — so unlike every other integration module here, this
    one needs them present in a test body and not merely at import. Since
    ISSUE-301 the suite scrubs them before every test, which would leave every
    call in this file refused with "NC_URL, NC_USER, NC_PASS env vars
    required". Putting them back here rather than exempting them in
    `tests/support/env_isolation.py` keeps the scrub closed by default and puts
    the dependency on the page: this file needs the operator's real Nextcloud,
    which is what the `integration` marker already says.
    """
    monkeypatch.delenv("ISTOTA_USER_ID", raising=False)
    monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(tmp_path / "no-such-admins-file"))
    monkeypatch.setenv("NC_URL", _url)
    monkeypatch.setenv("NC_USER", _user)
    monkeypatch.setenv("NC_PASS", _pass)


@pytest.fixture(scope="module")
def config():
    return Config(
        nextcloud=NextcloudConfig(url=_url, username=_user, app_password=_pass)
    )


@pytest.fixture(scope="module")
def scratch(config):
    """A folder in the bot's tree, created and removed outside the skill.

    The skill deliberately exposes no mkcol/rm, so setup uses the raw DAV
    helper. Teardown deletes the folder; the copy that lands in the trash bin
    is left there rather than emptying an account-wide trash.
    """
    path = f"/istota-live-test-{uuid.uuid4().hex[:8]}"
    url = _http.dav_files_url(config, path)
    _http.dav_request(config, "MKCOL", url, ok_statuses=(405,))
    try:
        yield path
    finally:
        try:
            _http.dav_request(config, "DELETE", url, ok_statuses=(404,))
        except Exception as e:  # pragma: no cover - teardown best effort
            print(f"scratch cleanup failed for {path}: {e}")


@pytest.fixture
def seeded_notification(config):
    """Post a notification to the bot so the read/dismiss verbs have a subject.

    Uses the admin notifications API, which needs admin rights — without them
    there is no way to manufacture one, so the test skips rather than depending
    on whatever happens to be in the queue.
    """
    import httpx

    subject = f"live suite probe {uuid.uuid4().hex[:8]}"
    try:
        resp = httpx.post(
            f"{_http.nc_base_url(config)}"
            f"/ocs/v2.php/apps/notifications/api/v2/admin_notifications/{_user}",
            auth=_http.nc_auth(config),
            headers=_http.ocs_headers(),
            data={"shortMessage": subject, "longMessage": "seeded by the live suite"},
            timeout=10,
        )
        code = (resp.json().get("ocs") or {}).get("meta", {}).get("statuscode")
    except Exception as e:  # pragma: no cover - environment dependent
        pytest.skip(f"could not seed a notification: {e}")
    if code != 200:
        pytest.skip(f"seeding a notification needs admin rights (OCS {code})")

    listed = ok("notify", "list", "--limit", "25")["notifications"]
    match = [n for n in listed if subject in n["subject"]]
    if not match:
        pytest.skip("seeded notification did not appear in the queue")
    return match[0]["id"]


@pytest.fixture
def remote_file(config, scratch, tmp_path):
    """A freshly uploaded file under the scratch folder, unique per test."""
    name = f"probe-{uuid.uuid4().hex[:8]}.txt"
    local = tmp_path / name
    local.write_text("istota live probe\n")
    remote = f"{scratch}/{name}"
    ok("files", "upload", str(local), remote)
    return remote


# --- capabilities ----------------------------------------------------------


class TestCapabilities:
    def test_summary_has_the_operator_facing_shape(self):
        payload = ok("capabilities")
        assert payload["server"]["version"], "server version missing"
        for section in ("sharing", "talk", "notifications", "activity", "files", "features"):
            assert section in payload, f"missing section {section}"
        assert payload["account"]["id"] == _user

    def test_raw_returns_the_untouched_payload(self):
        payload = ok("capabilities", "--raw")
        assert "capabilities" in payload
        assert "files_sharing" in payload["capabilities"]

    def test_check_passes_for_a_feature_the_server_reports(self):
        features = ok("capabilities")["features"]
        present = [name for name, on in features.items() if on]
        assert present, "server reports no capabilities at all"
        payload = ok("capabilities", "--check", present[0])
        assert payload["status"] == "ok"
        assert payload["missing"] == []

    def test_check_fails_closed_on_an_unknown_feature(self):
        payload = fails("capabilities", "--check", "definitely.not.a.feature")
        assert payload["missing"] == ["definitely.not.a.feature"]
        assert payload["known"] == caps_mod.known_feature_names()

    def test_check_is_a_usable_shell_gate(self):
        """--check's exit code is the whole point; assert both directions."""
        code_ok, _ = run("capabilities", "--check", "sharing.api")
        code_bad, _ = run("capabilities", "--check", "sharing.api,nope.nope")
        assert code_ok == 0
        assert code_bad == 1


# --- user / group ----------------------------------------------------------


class TestUserAndGroup:
    def test_whoami_reports_the_authenticated_account(self):
        payload = ok("user", "whoami")
        assert payload["id"] == _user

    def test_search_works_without_admin_rights(self):
        payload = ok("user", "search", _user, "--limit", "5")
        assert payload["query"] == _user
        assert payload["count"] == len(payload["results"])
        assert _user in [r["id"] for r in payload["results"]], (
            "search did not find the account it was asked for"
        )

    def test_search_honours_the_type_filter(self):
        payload = ok("user", "search", _user, "--types", "users", "--limit", "3")
        assert payload["types"] == ["users"]
        assert all(r["source"] == "users" for r in payload["results"])

    @needs_admin
    def test_get_returns_the_user_record(self):
        payload = ok("user", "get", _user)
        assert payload["id"] == _user

    @needs_admin
    def test_groups_defaults_to_the_bot(self):
        payload = ok("user", "groups")
        assert payload["user"] == _user
        assert isinstance(payload["groups"], list)

    @needs_admin
    def test_group_list_and_members_agree(self):
        groups = ok("group", "list")["groups"]
        assert groups, "an admin-visible server has at least the admin group"
        members = ok("group", "members", groups[0])
        assert members["group"] == groups[0]
        assert isinstance(members["members"], list)

    @needs_admin
    def test_membership_is_reported_consistently_from_both_ends(self):
        """`user groups` and `group members` must agree about the bot."""
        mine = ok("user", "groups")["groups"]
        if not mine:
            pytest.skip("bot account is in no groups")
        assert _user in ok("group", "members", mine[0])["members"]

    def test_reading_your_own_record_needs_no_admin_rights(self):
        """Nextcloud lets any account read itself through the provisioning API."""
        assert ok("user", "get", _user)["id"] == _user

    @pytest.mark.skipif(_WANT_ADMIN, reason="only meaningful without admin rights")
    @needs_other_user
    def test_reading_another_user_is_refused_legibly(self):
        """Reading *another* user is the verb that needs the right.

        Nextcloud answers 998 "not found" here rather than 997 — it hides the
        account's existence instead of admitting a permission problem. The
        admin hint only applies to a real 997, so assert what the caller can
        act on: which endpoint, and a status that identifies the refusal.
        """
        payload = fails("user", "get", _OTHER_USER)
        assert payload["ocs_status"] in (997, 998)
        assert payload["endpoint"] == f"/cloud/users/{_OTHER_USER}"
        if payload["ocs_status"] == 997:
            assert "admin" in payload["error"].lower()

    @pytest.mark.skipif(_WANT_ADMIN, reason="only meaningful without admin rights")
    def test_group_list_names_admin_rights_and_the_alternative(self):
        """The one verb that admits the permission problem outright.

        Nextcloud 30 answers 403 here (not the 997 the OCS docs suggest), which
        is inside the hint helper's trigger set — so the refusal must both name
        admin rights and point at the endpoint any user may call.
        """
        payload = fails("group", "list")
        assert payload["ocs_status"] in (403, 997)
        assert "admin rights" in payload["error"].lower()
        assert "user search" in payload["error"], "no usable alternative offered"


# --- shares ----------------------------------------------------------------


class TestShares:
    def test_list_includes_a_share_that_exists(self, remote_file):
        link = share_link(remote_file, "--days", "1")
        try:
            ids = [str(s["id"]) for s in ok("share", "list")]
            assert str(link["share_id"]) in ids
        finally:
            ok("share", "revoke", str(link["share_id"]))

    def test_list_filtered_by_path_excludes_other_paths(self, scratch, remote_file, tmp_path):
        """The filter must actually filter — a list that ignores --path and
        returns everything would satisfy a bare isinstance check."""
        other = f"{scratch}/other-{uuid.uuid4().hex[:8]}.txt"
        local = tmp_path / "other.txt"
        local.write_text("other\n")
        ok("files", "upload", str(local), other)

        mine = share_link(remote_file, "--days", "1")
        theirs = share_link(other, "--days", "1")
        try:
            scoped = [str(s["id"]) for s in ok("share", "list", "--path", remote_file)]
            assert str(mine["share_id"]) in scoped
            assert str(theirs["share_id"]) not in scoped
        finally:
            ok("share", "revoke", str(mine["share_id"]))
            ok("share", "revoke", str(theirs["share_id"]))

    def test_shared_with_me_excludes_my_own_outgoing_shares(self, remote_file):
        """A distinct view, not the same list under another flag."""
        link = share_link(remote_file, "--days", "1")
        try:
            inbound = [str(s["id"]) for s in ok("share", "list", "--shared-with-me")]
            assert str(link["share_id"]) not in inbound
        finally:
            ok("share", "revoke", str(link["share_id"]))

    @needs_other_user
    def test_search_sharees_finds_a_real_account(self):
        payload = ok("share", "search", _OTHER_USER)
        found = [u["value"]["shareWith"] for u in payload.get("users", [])]
        found += [u["value"]["shareWith"] for u in payload.get("exact", {}).get("users", [])]
        assert _OTHER_USER in found

    def test_link_create_get_and_revoke_round_trip(self, remote_file):
        link = share_link(remote_file, "--days", "1")
        assert link["url"].startswith("http")
        assert link["download_url"], "no direct-download URL synthesized"
        assert link["expires"], "default expiry not applied"
        assert link["revoke_command"], "no revoke hint returned"

        fetched = ok("share", "get", str(link["share_id"]))
        assert str(fetched["id"]) == str(link["share_id"])
        assert fetched["token"] == link["token"]

        ok("share", "revoke", str(link["share_id"]))
        fails("share", "get", str(link["share_id"]))

    def test_link_never_expiring_when_asked(self, remote_file):
        link = share_link(remote_file, "--days", "0")
        try:
            assert not link.get("expires")
        finally:
            ok("share", "revoke", str(link["share_id"]))

    def test_link_generated_password_is_reported_once(self, remote_file):
        link = share_link(remote_file, "--days", "1", "--password-generate")
        try:
            assert link.get("password"), "generated password not returned to the caller"
            assert len(link["password"]) >= 12
        finally:
            ok("share", "revoke", str(link["share_id"]))

    def test_link_clamps_to_a_server_enforced_expiry(self, remote_file, config):
        limit = caps_mod.public_link_expiry_limit(caps_mod.fetch_capabilities(config))
        if limit is None:
            pytest.skip("server enforces no maximum public-link expiry")
        link = share_link(remote_file, "--days", str(limit + 30))
        try:
            # Silently handing back a shorter-lived link than asked for is the
            # failure mode here: the caller would promise the wrong date.
            assert link["notice"], "expiry was clamped without saying so"
            assert str(limit) in link["notice"]
            # UTC, matching what the server compares against — see
            # shares.expiry_date. Using the local date here reproduces the very
            # bug this suite found.
            expected = datetime.now(timezone.utc).date() + timedelta(days=limit)
            assert link["expires"] == expected.isoformat()
        finally:
            ok("share", "revoke", str(link["share_id"]))

    def test_revoke_by_path_refuses_without_confirmation(self, remote_file):
        link = share_link(remote_file, "--days", "1")
        try:
            payload = fails("share", "revoke", "--path", remote_file)
            assert payload["needs_confirmation"] is True
            # The refusal must not have removed anything.
            ok("share", "get", str(link["share_id"]))
        finally:
            ok("share", "revoke", str(link["share_id"]))

    def test_revoke_by_path_removes_every_link_once_confirmed(self, remote_file):
        first = share_link(remote_file, "--days", "1")
        second = share_link(remote_file, "--days", "1")
        ok("share", "revoke", "--path", remote_file, "--confirmed")
        fails("share", "get", str(first["share_id"]))
        fails("share", "get", str(second["share_id"]))

    def test_update_changes_an_existing_share(self, remote_file):
        link = share_link(remote_file, "--days", "1")
        try:
            ok("share", "update", str(link["share_id"]), "--note", "live test note")
            fetched = ok("share", "get", str(link["share_id"]))
            assert fetched.get("note") == "live test note"
        finally:
            ok("share", "revoke", str(link["share_id"]))

    def test_get_on_a_missing_share_carries_the_server_message(self):
        payload = fails("share", "get", "999999999")
        assert payload["status"] == "error"
        assert payload.get("error"), "error envelope has no message"

    @needs_other_user
    def test_user_share_create_and_delete(self, remote_file):
        share = ok(
            "share", "create", "--path", remote_file, "--type", "user",
            "--with", _OTHER_USER, "--permissions", "1",
        )
        try:
            assert str(share["share_with"]) == _OTHER_USER
        finally:
            ok("share", "delete", str(share["id"]))
        fails("share", "get", str(share["id"]))


# --- files (WebDAV) --------------------------------------------------------


class TestFiles:
    def test_quota_reports_the_bot_account(self):
        payload = ok("files", "quota")
        assert payload["used_bytes"] >= 0
        assert "available_bytes" in payload

    def test_stat_returns_server_side_properties(self, remote_file):
        payload = ok("files", "stat", remote_file)
        assert payload["path"].endswith(remote_file.split("/")[-1])
        assert payload.get("etag"), "no etag — PROPFIND parse likely wrong"
        assert payload.get("size") is not None

    def test_list_sees_the_uploaded_file(self, scratch, remote_file):
        payload = ok("files", "list", scratch)
        assert payload["path"] == scratch
        names = [e["path"] for e in payload["entries"]]
        assert any(remote_file.split("/")[-1] in n for n in names)

    def test_upload_download_round_trips_the_bytes(self, scratch, tmp_path):
        body = f"round trip {uuid.uuid4().hex}\n"
        local = tmp_path / "rt.txt"
        local.write_text(body)
        remote = f"{scratch}/rt-{uuid.uuid4().hex[:8]}.txt"
        ok("files", "upload", str(local), remote)
        back = tmp_path / "rt-back.txt"
        ok("files", "download", remote, str(back))
        assert back.read_text() == body

    def test_chunked_upload_produces_the_same_file(self, scratch, tmp_path):
        body = "x" * (6 * 1024 * 1024)
        local = tmp_path / "big.bin"
        local.write_text(body)
        remote = f"{scratch}/big-{uuid.uuid4().hex[:8]}.bin"
        ok("files", "upload", str(local), remote, "--chunked")
        stat = ok("files", "stat", remote)
        assert int(stat["size"]) == len(body)

    def test_search_finds_the_file_by_name(self, scratch, remote_file):
        name = remote_file.split("/")[-1]
        results = _search_results(ok("files", "search", "--scope", scratch, "--name", name))
        assert results, "indexed search returned nothing for a file that exists"
        assert any(name in r["path"] for r in results)

    def test_search_respects_the_scope(self, scratch, remote_file):
        name = remote_file.split("/")[-1]
        results = _search_results(ok("files", "search", "--scope", scratch, "--name", "*.txt"))
        assert all(scratch in r["path"] for r in results)
        assert any(name in r["path"] for r in results)

    def test_favorite_toggles_both_ways(self, remote_file):
        ok("files", "favorite", remote_file)
        assert ok("files", "stat", remote_file).get("favorite")
        ok("files", "favorite", remote_file, "--off")
        assert not ok("files", "stat", remote_file).get("favorite")

    def test_versions_appear_after_a_second_write(self, config, scratch, tmp_path):
        if not caps_mod.feature_map(caps_mod.fetch_capabilities(config))["files.versioning"]:
            pytest.skip("server has versioning disabled")
        remote = f"{scratch}/versioned-{uuid.uuid4().hex[:8]}.txt"
        local = tmp_path / "v.txt"
        local.write_text("first\n")
        ok("files", "upload", str(local), remote)
        local.write_text("second\n")
        ok("files", "upload", str(local), remote)

        payload = ok("files", "versions", remote)
        versions = payload["versions"]
        assert versions, "no stored version after rewriting the file"

        ok("files", "restore-version", remote, str(versions[0]["version"]))
        back = tmp_path / "v-back.txt"
        ok("files", "download", remote, str(back))
        assert back.read_text() == "first\n"

    def test_trash_lists_a_deleted_file(self, config, scratch, tmp_path):
        if not caps_mod.feature_map(caps_mod.fetch_capabilities(config))["files.undelete"]:
            pytest.skip("server has the trash bin disabled")
        name = f"doomed-{uuid.uuid4().hex[:8]}.txt"
        local = tmp_path / name
        local.write_text("delete me\n")
        remote = f"{scratch}/{name}"
        ok("files", "upload", str(local), remote)
        _http.dav_request(config, "DELETE", _http.dav_files_url(config, remote))

        entries = ok("files", "trash", "list")["entries"]
        match = [e for e in entries if name in e["name"]]
        assert match, f"{name} not in the trash after deletion"

        ok("files", "trash", "restore", match[0]["name"])
        ok("files", "stat", remote)

    def test_trash_empty_refuses_without_confirmation(self):
        payload = fails("files", "trash", "empty")
        assert payload["needs_confirmation"] is True

    @needs_destructive
    def test_trash_empty_once_confirmed(self):
        ok("files", "trash", "empty", "--confirmed")
        assert ok("files", "trash", "list")["entries"] == []

    def test_stat_on_a_missing_path_carries_the_status(self):
        payload = fails("files", "stat", "/no-such-file-here.txt")
        assert payload["status"] == "error"
        assert "404" in json.dumps(payload) or "not found" in json.dumps(payload).lower()


# --- path scoping ----------------------------------------------------------


class TestPathScoping:
    """A non-admin caller must not reach outside their own workspace.

    The refusal happens before any request is issued, so these assert the
    boundary itself rather than the server's opinion of it.
    """

    @pytest.fixture(autouse=True)
    def _non_admin(self, monkeypatch, tmp_path):
        admins = tmp_path / "admins"
        admins.write_text("someone-else\n")
        monkeypatch.setenv("ISTOTA_ADMINS_FILE", str(admins))
        monkeypatch.setenv("ISTOTA_USER_ID", "livetestcaller")

    def test_absolute_path_outside_the_workspace_is_refused(self):
        payload = fails("files", "stat", "/Users/someone-else/secret.txt")
        assert "outside your workspace" in payload["error"]

    def test_traversal_out_of_the_workspace_is_refused(self):
        payload = fails("files", "stat", "../someone-else/secret.txt")
        assert "outside your workspace" in payload["error"]

    def test_share_paths_are_scoped_too(self):
        payload = fails("share", "link", "/Users/someone-else/secret.txt")
        assert "outside your workspace" in payload["error"]

    def test_own_workspace_path_is_allowed_through_to_the_server(self):
        """Reaching a 404 rather than a scope refusal proves it got past the gate."""
        payload = fails("files", "stat", "no-such-file.txt")
        assert "outside your workspace" not in payload.get("error", "")


# --- talk ------------------------------------------------------------------


class TestTalk:
    def test_rooms_lists_conversations(self):
        rooms = untrusted(ok("talk", "rooms"), "rooms")
        assert all(r["token"] for r in rooms), "a room came back with no token"
        assert all("UNTRUSTED" in r["name"] for r in rooms), "room names are not framed"

    def test_delete_refuses_without_confirmation(self):
        payload = fails("talk", "delete", "nonexistent")
        assert payload.get("needs_confirmation") is True

    @needs_talk
    def test_room_lifecycle(self):
        name = f"istota-live-{uuid.uuid4().hex[:8]}"
        token = ok("talk", "create", "--name", name, "--type", "group")["token"]
        try:
            assert ok("talk", "room", token)["room"]["token"] == token

            ok("talk", "rename", token, "--name", name + "-renamed")
            assert name + "-renamed" in ok("talk", "room", token)["room"]["name"]

            ok("talk", "describe", token, "--description", "live test room")

            assert ok("talk", "send", token, "hello from the live suite", "--silent")["message_id"]

            messages = untrusted(ok("talk", "read", token, "--limit", "10"), "messages")
            assert any("hello from the live suite" in m["message"] for m in messages)

            assert untrusted(ok("talk", "participants", token), "participants")

            if _OTHER_USER:
                ok("talk", "invite", token, _OTHER_USER)
                participants = untrusted(ok("talk", "participants", token), "participants")
                assert any(_OTHER_USER in json.dumps(p) for p in participants)
                assert _OTHER_USER in json.dumps(
                    ok("talk", "mentions", token, "--search", _OTHER_USER[:3])
                )
        finally:
            ok("talk", "delete", token, "--confirmed")
        fails("talk", "room", token)

    @needs_talk
    def test_share_file_posts_into_a_conversation(self, remote_file):
        name = f"istota-live-share-{uuid.uuid4().hex[:8]}"
        token = ok("talk", "create", "--name", name, "--type", "group")["token"]
        try:
            ok("talk", "share-file", token, "--path", remote_file)
            messages = untrusted(ok("talk", "read", token), "messages")
            # A file post renders as the placeholder "{file}"; the name it
            # stands for lives in Talk's message parameters. Asserting the
            # placeholder documents that the reader currently drops it — see
            # the note in the module docstring.
            assert any("{file}" in m["message"] for m in messages)
        finally:
            ok("talk", "delete", token, "--confirmed")

    @needs_talk
    def test_search_finds_a_posted_message(self):
        needle = f"needle{uuid.uuid4().hex[:8]}"
        name = f"istota-live-search-{uuid.uuid4().hex[:8]}"
        token = ok("talk", "create", "--name", name, "--type", "group")["token"]
        try:
            ok("talk", "send", token, f"searchable {needle}", "--silent")
            results = ok("talk", "search", needle, "--token", token)
            assert needle in json.dumps(untrusted(results, "results"))
        finally:
            ok("talk", "delete", token, "--confirmed")

    @needs_talk
    def test_leave_drops_the_bot_from_a_conversation(self):
        name = f"istota-live-leave-{uuid.uuid4().hex[:8]}"
        token = ok("talk", "create", "--name", name, "--type", "group")["token"]
        ok("talk", "leave", token)
        rooms = untrusted(ok("talk", "rooms"), "rooms")
        assert token not in [r["token"] for r in rooms]


# --- notifications and activity -------------------------------------------


class TestNotificationsAndActivity:
    @pytest.fixture(autouse=True)
    def _needs_app(self, config, request):
        app = "activity" if "activity" in request.node.name else "notifications"
        if not caps_mod.feature_map(caps_mod.fetch_capabilities(config))[app]:
            pytest.skip(f"{app} app not installed")

    def test_notification_list(self):
        entries = untrusted(ok("notify", "list", "--limit", "5"), "notifications")
        assert all(e["id"] and e["app"] for e in entries)

    def test_notification_get_and_dismiss_round_trip(self, seeded_notification):
        """Seeds its own notification rather than waiting for one to exist.

        Skipping when the queue happened to be empty meant `notify get` and
        `notify dismiss` never once ran against a real server.
        """
        listed = untrusted(ok("notify", "list", "--limit", "25"), "notifications")
        mine = [n for n in listed if n["id"] == seeded_notification]
        assert mine, f"seeded notification {seeded_notification} not in the list"

        fetched = ok("notify", "get", str(seeded_notification))
        assert fetched["untrusted"] is True
        assert fetched["notification"]["id"] == seeded_notification
        # The subject is other people's text and must arrive framed.
        assert "UNTRUSTED" in fetched["notification"]["subject"]

        assert ok("notify", "dismiss", str(seeded_notification))["dismissed"] == (
            seeded_notification
        )
        remaining = untrusted(ok("notify", "list", "--limit", "25"), "notifications")
        assert seeded_notification not in [n["id"] for n in remaining]

    @needs_destructive
    def test_dismiss_all_clears_the_queue(self):
        ok("notify", "dismiss-all")
        assert untrusted(ok("notify", "list"), "notifications") == []

    def test_activity_list(self):
        entries = untrusted(ok("activity", "list", "--limit", "5"), "activity")
        assert entries, "the scratch-folder writes should have produced activity"
        assert all(e["id"] and e["app"] and e["datetime"] for e in entries)
        assert all("UNTRUSTED" in e["subject"] for e in entries)

    def test_activity_filtered_by_type(self):
        """The 'files' stream is a path segment, not a ?filter= param.

        Getting that wrong 404s on every call — which is what the first live
        run found.
        """
        entries = untrusted(ok("activity", "list", "--type", "files", "--limit", "5"), "activity")
        assert all(e["app"] == "files" for e in entries)


# Escape hatch for a verb that genuinely cannot be driven live, keyed
# (group, command) with the reason. Empty today — all 44 verbs are exercised.
# The guard that holds this honest runs in the default suite, not here, since
# this module is skipped without credentials — see
# test_nextcloud_skill_cli.py::TestLiveCoverage.
NOT_EXERCISED_LIVE: dict[tuple[str, str | None], str] = {}
