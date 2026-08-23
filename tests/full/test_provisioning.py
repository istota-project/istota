"""What the Docker artifact does to itself before it will answer anything.

This is the coverage the full shape buys, and it is why the shape is asserted
first: `provision-nc.sh` is executed by nothing anywhere else in the repo —
`docker-compose.yml:48` mounts it as a Nextcloud post-install hook and that is
its only invocation — and `entrypoint.sh` is executed only as far as the
config-write marker, by the upgrade tier, against a stub that makes every room
take the *create* branch. So the room find-and-reuse path, the workspace
seeding, the OAuth2 registration and the exec into the scheduler have had no
witness at all.

**Everything here is an outcome assertion, and that is not a style preference.**
Every `occ` call in `provision-nc.sh` is `|| true` (`:25`, `:29`, `:34-36`,
`:56-57`, `:73-74`), and so is the OAuth PHP block (`:113`). The script writes
`/mnt/shared/.istota-provisioned` and reports success having done nothing at
all. The flag proves the script ran; only the outcomes prove it worked. That is
defensible production resilience — an operator would rather have a bot with no
calendar app than no bot — and it means this file is the only thing standing
between "provisioned" and "silently empty".

**One cold boot, not one per assertion.** The module-scoped `provisioned`
fixture takes a private stack (`fresh=True`) and every test in the file shares
it, because the boot is a minute and the assertions are seconds.

That means these tests do **not** use the `stack` fixture, so `Stack.reset` does
not run between them and the isolation the rest of the testbed relies on is not
in play here. What holds instead, stated so it is checkable rather than assumed:
the step that genuinely depends on order — the restart — captures its own
"before" rather than trusting an earlier test's, and nothing else in the file
reads task rows or endpoint state. `provision-nc.sh` does not re-run on an
installed instance, so the users, apps, mounts and OAuth2 row are fixed from the
first boot onwards, and the rooms are recovered by name rather than recreated.
A test added here that asserts on a task or on the scripted endpoint breaks that
and needs a per-test reset.
"""

from __future__ import annotations

import pytest

from testbed import profiles
from testbed import stack as compose_support

# One definition of the mask probe, not two. It is the lean tier's scenario
# that owns it — the spec puts the sandbox assertions there, on cost grounds —
# and importing it here is what stops the full shape's copy drifting into a
# weaker version of the same check. `tests/smoke/conftest.py` already reaches
# across packages the same way, into `tests/image`.
from ..smoke.test_sandbox_in_stack import (
    CONTAINER_DB_DIR,
    MASK_SCRIPT,
    probe_output,
)

pytestmark = pytest.mark.full

#: Where `entrypoint.sh` persists the tokens it provisioned. Deleting it is how
#: the recovery-by-name path is reached.
API_PROVISION_FLAG = "/data/config/.api-provisioned"

#: Where `entrypoint.sh:32-38` seeds the web admin allowlist.
ADMINS_FILE = "/data/config/admins"

#: The three group rooms `entrypoint.sh` creates, by display name. Not
#: user-prefixed: `find_room_by_name` scopes lookups by USER_NAME participation
#: instead, so each user gets their own set of identically-named rooms.
GROUP_ROOMS = ("general", "logs", "alerts")

#: Talk's room types. 1 is a one-to-one, 2 is a group room, 3 is public — and
#: the distinction is the point of one assertion below: #logs carries the
#: execution log and #alerts carries confirmations, and a public room is
#: joinable by anyone holding its token.
ROOM_TYPE_ONE_TO_ONE = 1
ROOM_TYPE_GROUP = 2

#: The three variables that would carry a real model credential if the overlay
#: interpolated them instead of hardcoding them, mapped to the literal it sets.
#: `docker-compose.yml` passes all three to this container and `entrypoint.sh`
#: writes the OAuth one into `~/.claude/.credentials.json`.
BRAIN_CREDENTIALS = {
    "ANTHROPIC_API_KEY": "",
    "CLAUDE_CODE_OAUTH_TOKEN": "",
    "ISTOTA_BRAIN_NATIVE_API_KEY": "unused-by-the-scripted-endpoint",
}


@pytest.fixture(scope="module")
def provisioned(stacks):
    """One cold boot of the full stack, shared by this module.

    `fresh=True` because everything here is about start-up: a stack another
    module had already used would have been provisioned by a previous test's
    boot, which is the one thing these assertions must not be reading.
    """
    stack = stacks.get(profiles.FULL, fresh=True)
    try:
        yield stack
    finally:
        stacks.release(stack)


def _nextcloud(stack):
    return stack.service("nextcloud")


class TestFirstInstallProvisioning:
    """What `provision-nc.sh` and `entrypoint.sh` left behind on a cold volume set."""

    def test_both_users_exist(self, provisioned):
        """`user:add` twice, both `|| true`.

        A bot user that was never created makes every later OCS call 401, which
        surfaces as "Talk is broken" rather than as "provisioning did nothing".
        """
        users = _nextcloud(provisioned).users()

        assert "istota" in users, users
        assert "testuser" in users, users

    def test_the_required_apps_are_enabled(self, provisioned):
        """`app:enable spreed`, `calendar`, `files_external`, all `|| true`.

        Two of the three are not bundled in `nextcloud:30-apache` and are
        fetched from the app store at install time — measured, not assumed: the
        image ships `files_external` under `apps/` and ships neither `spreed`
        nor `calendar` at all, and on a provisioned instance both turn up under
        `custom_apps/`, which is where downloads land. So this assertion is also
        the tier's only signal that the boot had the network it silently
        depends on.
        """
        apps = _nextcloud(provisioned).enabled_apps()

        assert "spreed" in apps, apps
        assert "calendar" in apps, apps
        assert "files_external" in apps, apps

    def test_external_storage_is_configured_for_both_users(self, provisioned):
        """Two mounts, and each applicable to exactly the user it is for.

        The bot gets the whole shared volume; the human user gets *only* the bot
        workspace directory, never the user base — the base holds `inbox/`,
        `memories/` and `shared/`, which are bot-internal. `provision-nc.sh:66-71`
        is emphatic about that, and nothing has ever checked it.
        """
        mounts = _nextcloud(provisioned).external_mounts()
        by_path = {mount.get("configuration", {}).get("datadir", ""): mount
                   for mount in mounts}

        bot_mount = by_path.get("/mnt/shared")
        assert bot_mount is not None, f"no /mnt/shared mount among {sorted(by_path)}"
        assert bot_mount.get("applicable_users") == ["istota"], bot_mount

        user_path = "/mnt/shared/Users/testuser/istota"
        user_mount = by_path.get(user_path)
        assert user_mount is not None, f"no {user_path} mount among {sorted(by_path)}"
        assert user_mount.get("applicable_users") == ["testuser"], user_mount

    def test_both_external_mounts_permit_sharing(self, provisioned):
        """`enable_sharing` defaults to *false* on a `files_external` mount.

        Left at the default it refuses every share of everything under
        `/mnt/shared`, which is the bot's entire workspace: the bot cannot hand
        anyone a file it produced and the `nextcloud` skill's `share link` verb
        answers "You are not allowed to share". So `provision-nc.sh` turns it on
        for both mounts it creates — the bot's, so the bot can share its own
        output, and the user's, so the user can share out of their own view of
        the workspace.

        Read off `occ files_external:list` rather than inferred from a
        successful share: this is the setting, and a share that happens to work
        for another reason would report the setting as present.
        """
        mounts = _nextcloud(provisioned).external_mounts()
        by_path = {mount.get("configuration", {}).get("datadir", ""): mount
                   for mount in mounts}

        for path in ("/mnt/shared", "/mnt/shared/Users/testuser/istota"):
            mount = by_path.get(path)
            assert mount is not None, f"no {path} mount among {sorted(by_path)}"
            options = mount.get("options") or {}
            assert "enable_sharing" in options, (
                f"occ reports no enable_sharing option for {path}; the whole "
                f"mount row is {mount}"
            )
            assert options["enable_sharing"] is True, (path, options)

    def test_the_boot_does_not_try_to_share_the_bot_dir_back(self, provisioned):
        """`[nextcloud] auto_share_bot_dir = false`, witnessed on a real boot.

        `ensure_user_directories_v2` shares the bot workspace back to the user
        over OCS every time the daemon starts, and on bare metal that share is
        how the user gets the directory at all. This shape does not need it:
        `provision-nc.sh:74` already mounts the very same directory into the
        user's tree at first provisioning. Before the guard the call failed on
        every boot and logged a warning; with sharing now enabled on the mount
        it would instead succeed and hand the user a second copy of their
        workspace, under the received-share name rather than the mount name.

        Both halves are asserted because either alone is satisfiable the wrong
        way: a silent log with the share still made, or a suppressed share on a
        boot that never reached that code.
        """
        daemon_log = provisioned.logs(4000)
        assert "Failed to share folder" not in daemon_log, (
            "the boot still attempts the OCS share-back:\n"
            + "\n".join(
                line for line in daemon_log.splitlines()
                if "share folder" in line
            )
        )

        nextcloud = _nextcloud(provisioned)
        received = [
            row for row in nextcloud.shares(user="testuser", shared_with_me=True)
            if (row.get("file_target") or "").strip("/").lower().startswith("istota")
        ]
        assert received == [], (
            "the bot workspace arrived as a received share as well as a mount: "
            f"{[row.get('file_target') for row in received]}"
        )

        user_tree = nextcloud.files("", user="testuser", depth="1")
        workspace = [
            entry for entry in user_tree
            if entry.strip("/").lower().replace("_", " ") == "istota"
        ]
        assert len(workspace) == 1, (
            f"the bot workspace appears {len(workspace)} times in the user's "
            f"file list: {user_tree}"
        )

    def test_the_directory_structure_is_present(self, provisioned):
        """`Channels/` and the pre-created bot workspace directory.

        Deliberately short. `inbox/`, `memories/` and `shared/` are created by
        the istota container's `ensure_user_directories_v2()`, not by the
        provisioning script — `provision-nc.sh:85-90` says why seeding them here
        would break that migration — so asserting on them would be asserting the
        wrong component's work.
        """
        result = provisioned.exec(
            ["test", "-d", "/mnt/shared/Channels", "-a",
             "-d", "/mnt/shared/Users/testuser/istota"]
        )

        assert result.returncode == 0, provisioned.exec(
            ["ls", "-la", "/mnt/shared"]
        ).stdout

    def test_the_oauth2_client_carries_the_callback_url_compose_was_given(
        self, provisioned
    ):
        """The assertion nothing has ever made, against a value baked in once.

        `provision-nc.sh:106` reads `ISTOTA_WEB_CALLBACK_URL` and writes it into
        the `oauth2_clients` row at first provisioning, and `docker-compose.yml`
        warns twice that changing the host afterwards leaves a stale
        registration that no restart repairs. A mismatch here is a deployment
        whose web UI cannot complete an OAuth2 login, and the only symptom is a
        redirect that fails in a browser nobody in this tier runs.

        Compared against `stack.env` rather than against a literal, because the
        port is ephemeral — and because the whole claim is "what compose was
        given is what Nextcloud stored", which a literal on both sides would
        not test.
        """
        expected = provisioned.env["ISTOTA_WEB_CALLBACK_URL"]
        clients = _nextcloud(provisioned).oauth_clients()
        istota_clients = [row for row in clients if row.get("name") == "istota-web"]

        assert len(istota_clients) == 1, clients
        assert istota_clients[0]["redirect_uri"] == expected

    def test_the_admin_allowlist_was_seeded_with_the_human_user(self, provisioned):
        """`entrypoint.sh:32-38`, before anything else it does.

        Up front deliberately: the web service polls for `config.toml` to start
        serving, so an admins file landing *after* it would let web cache an
        empty allowlist and 403 the dashboard until restart. `_user_is_web_admin`
        fails closed on an empty allowlist.
        """
        result = provisioned.exec(["cat", ADMINS_FILE])

        assert result.returncode == 0, result.stderr
        assert result.stdout.split() == ["testuser"], result.stdout

    def test_the_default_talk_rooms_exist_as_group_rooms(self, provisioned):
        """Three group rooms plus the one-to-one, with both users in each group.

        `roomType=2`, not 3: #logs carries the daemon's execution log and
        #alerts carries confirmations and security alerts, and a public room is
        joinable by anyone holding its token. `provision_rooms.py` is the
        Ansible path's implementation of the same rule, and it is asserted
        against `MagicMock`.
        """
        nextcloud = _nextcloud(provisioned)
        rooms = nextcloud.rooms()
        by_name = {room.get("displayName", ""): room for room in rooms}

        for name in GROUP_ROOMS:
            room = by_name.get(name)
            assert room is not None, f"no {name!r} room among {sorted(by_name)}"
            assert room.get("type") == ROOM_TYPE_GROUP, room
            participants = nextcloud.participants(room["token"])
            assert "istota" in participants, (name, participants)
            assert "testuser" in participants, (name, participants)

        assert any(
            room.get("type") == ROOM_TYPE_ONE_TO_ONE for room in rooms
        ), f"no 1:1 room among {[(r.get('displayName'), r.get('type')) for r in rooms]}"


class TestReprovisioningIsIdempotent:
    """Boot it twice, then boot it having lost its own bookkeeping.

    One test rather than three, because each step depends on the previous one's
    side effect and a shuffled order would assert recovery before the thing to
    recover from had happened. `pytest-randomly` is active in this repo, so
    ordering between test functions is not something to rely on.
    """

    def test_a_restart_creates_no_duplicates_and_recovers_by_name(self, provisioned):
        nextcloud = _nextcloud(provisioned)
        before_rooms = {
            room["token"]: room.get("displayName", "") for room in nextcloud.rooms()
        }
        before_clients = _oauth_names(nextcloud)
        before_flag = provisioned.exec(["cat", API_PROVISION_FLAG]).stdout

        # --- a plain restart: the flag is present, so provisioning short-circuits
        provisioned.restart()
        provisioned.wait_healthy()

        assert _room_names(nextcloud) == sorted(before_rooms.values()), (
            "a restart created or lost a room"
        )
        assert _oauth_names(nextcloud) == before_clients

        # --- the flag is gone: every token is missing, so the whole API
        # provisioning block re-runs and has to find the rooms by name rather
        # than create a second set. This is the path `entrypoint.sh`'s helper
        # comments describe and nothing has executed — the upgrade tier's stub
        # returns one canned room, so `find_room_by_name` never matches there
        # and every room takes the create branch.
        removed = provisioned.exec(["rm", "-f", API_PROVISION_FLAG])
        assert removed.returncode == 0, removed.stderr

        provisioned.restart()
        provisioned.wait_healthy()

        after_rooms = {
            room["token"]: room.get("displayName", "") for room in nextcloud.rooms()
        }
        assert after_rooms == before_rooms, (
            "recovery by name created a second set of rooms rather than reusing "
            f"the existing ones: before={sorted(before_rooms.items())} "
            f"after={sorted(after_rooms.items())}"
        )
        assert _oauth_names(nextcloud) == before_clients, (
            "a re-provisioning boot registered a second OAuth2 client"
        )

        rewritten = provisioned.exec(["cat", API_PROVISION_FLAG])
        assert rewritten.returncode == 0, rewritten.stderr
        assert _tokens_of(rewritten.stdout) == _tokens_of(before_flag), (
            "the rewritten provisioning flag names different room tokens"
        )


class TestTheReadinessProbeItself:
    """The probe every assertion in this file is downstream of.

    `wait_healthy` waits for the compose health check *and* for pid 1 to be the
    scheduler, because the health check looks for the `tasks` table and the
    database survives a restart — so on its own it passes within seconds while
    the entrypoint is still re-provisioning, and the idempotence assertions
    would read pre-restart state and pass for the wrong reason.

    The first version of the second condition scanned `/proc/[0-9]*/cmdline` and
    matched the shell running it, so it answered "yes" for any container. That
    is a probe that cannot fail, which is worse than no probe, and it is what
    this pair is here to keep from coming back.
    """

    def test_the_probe_cannot_see_its_own_shell(self, provisioned):
        """The negative half. Run the real probe with a string that exists
        nowhere, in the real container: if it still says yes, it is matching
        itself."""
        impossible = compose_support._SCHEDULER_RUNNING.replace(
            "istota-scheduler", "zzz-no-process-has-this-name"
        )

        assert provisioned.exec(["sh", "-c", impossible]).returncode != 0

    def test_the_probe_does_find_the_running_scheduler(self, provisioned):
        """The positive half, so the pair does not pass by being broken the
        other way."""
        assert provisioned.exec(
            ["sh", "-c", compose_support._SCHEDULER_RUNNING]
        ).returncode == 0


class TestTheDaemonTheDeploymentActuallyStarts:
    """Two properties of the booted container, both cheap and neither doctorable."""

    def test_a_bash_tool_call_succeeds_inside_a_task(self, provisioned):
        """The real sandbox witness, and the reason the overlay grants seccomp.

        `render-config.sh:230` renders `sandbox_enabled` true and
        `docker-compose.yml:168` leaves it true, so every filesystem-touching
        task on this shape goes through bubblewrap. Without
        `seccomp:unconfined`, Docker's default profile blocks the
        `unshare(CLONE_NEWUSER)` bwrap needs and every one of them fails —
        measured directly while this stage was written: `bwrap --unshare-user
        --ro-bind / / -- /bin/true` inside the shipped image exits 1 with "No
        permissions to create new namespace" without the grant and 0 with it.

        `doctor` cannot tell you this. It reports what is configured, and the
        configuration is identical either way; the only thing that knows is a
        task that tried.
        """
        marker = "sandbox-witness-ok"
        provisioned.reset(
            [
                {
                    "tool_calls": [
                        {
                            "id": "call-0",
                            "name": "Bash",
                            "arguments": {"command": f"echo {marker}"},
                        }
                    ]
                },
                # Not optional: a turn ending in `tool_calls` asks for another
                # round, and the scripted endpoint answers an unscripted round
                # with an error frame rather than replaying.
                {"text": "done"},
            ]
        )
        task_id = provisioned.submit("run the scripted command")

        task = provisioned.probe.wait_for_task(
            status="completed", task_id=task_id, timeout=240
        )

        assert task["status"] == "completed", provisioned.diagnostics(task)
        transcript = provisioned.endpoint.transcript()
        assert marker in transcript, (
            "the Bash tool result never came back, so the tool call did not run "
            "inside the sandbox\n" + provisioned.diagnostics(task)
        )

    def test_the_database_masks_are_in_the_namespace_on_this_shape_too(
        self, provisioned
    ):
        """The witness above is not one, and this is the correction.

        A task whose sandbox was *skipped* runs the same command through the
        same shell and returns the same bytes, so the assertion above holds
        just as well with bwrap disabled — which is the state both container
        shapes were in until Stage 7. What distinguishes them is the mask, and
        it is asserted here rather than only on the lean shape because the full
        shape's two `security_opt` concessions are otherwise checked by parsing
        the compose model: if Docker ignored one, every task here would go back
        to running unconfined and nothing would say so.

        The probe is imported from the lean scenario rather than copied. Any
        divergence between the two shapes' idea of what a mask looks like is a
        divergence this file could not report.
        """
        provisioned.reset(MASK_SCRIPT)
        task_id = provisioned.submit("look at the database directory")

        task = provisioned.probe.wait_for_task(
            status="completed", task_id=task_id, timeout=240
        )

        assert task["status"] == "completed", provisioned.diagnostics(task)
        observed = probe_output(provisioned)
        assert "fstype=tmpfs" in observed, (
            f"{CONTAINER_DB_DIR} inside the task is not a tmpfs, so this "
            "deployment is running its tasks unsandboxed. Check the daemon log "
            "for `Sandbox enabled but bubblewrap unavailable`, and check that "
            "both `seccomp:unconfined` and `systempaths=unconfined` reached the "
            f"container.\n--- probe ---\n{observed}\n"
            + provisioned.diagnostics(task)
        )
        assert "framework_db=unreadable" in observed, (
            f"the framework database is readable from inside a task\n{observed}"
        )
        assert "writable=no" in observed, (
            f"the mask is writable, so `--remount-ro` was not applied\n{observed}"
        )

    def test_no_real_brain_credential_reaches_the_container(self, provisioned):
        """Read the container, not the env-file, because the env-file loses.

        Compose lets the *process* environment outrank an `--env-file`, so a
        developer with `ANTHROPIC_API_KEY` exported in the shell that started
        pytest would beat anything `StackPool` writes into a file. The overlay
        hardcodes all three as literals for exactly that reason, and this is
        what proves the literals win — an assertion against the env-file could
        not see the thing that outranks the env-file.

        Values are compared against what the overlay sets rather than merely
        checked for emptiness: `ISTOTA_BRAIN_NATIVE_API_KEY` is deliberately
        non-empty (the daemon sends *something* to the scripted endpoint), so
        "not empty" would pass on a real key too.
        """
        result = provisioned.exec(["printenv"])
        assert result.returncode == 0, result.stderr
        seen = dict(
            line.split("=", 1)
            for line in result.stdout.splitlines()
            if "=" in line
        )

        for variable, expected in BRAIN_CREDENTIALS.items():
            assert seen.get(variable, "") == expected, variable
        # The credentials file `entrypoint.sh:690-700` writes when the OAuth
        # token is non-empty. Its absence is the second half of the claim: a
        # marker value rather than the empty string would have had the boot
        # write a fake credential and log that Claude Code was configured.
        assert provisioned.exec(
            ["test", "-e", "/root/.claude/.credentials.json"]
        ).returncode != 0


def _room_names(nextcloud) -> list[str]:
    return sorted(room.get("displayName", "") for room in nextcloud.rooms())


def _oauth_names(nextcloud) -> list[str]:
    return sorted(row.get("name", "") for row in nextcloud.oauth_clients())


def _tokens_of(flag_body: str) -> dict[str, str]:
    """The room-token lines out of `.api-provisioned`.

    Only the token lines. That file also carries `APP_PASSWORD`, which is a
    credential and has no business in a comparison whose failure message prints
    both sides.

    `LOCATION_INGEST_TOKEN` comes back with the rest and is empty on this
    profile, because `FULL_MODULE_SWITCHES` leaves `ISTOTA_LOCATION_ENABLED`
    false and `entrypoint.sh` only generates the value when it is true. An
    earlier version filtered it out, which was inert and hid something real:
    with location on, deleting `.api-provisioned` regenerates that token, but
    the config render is gated on `config.toml` not existing — so `config.toml`
    keeps the old value and the flag records one nothing reads. Noted rather
    than fixed; it is a defect in `entrypoint.sh`, not in this comparison.
    """
    values = {}
    for line in flag_body.splitlines():
        key, _, value = line.partition("=")
        if key.endswith("_TOKEN"):
            values[key] = value
    return values
