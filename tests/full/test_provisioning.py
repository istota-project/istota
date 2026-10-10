"""What the Docker artifact does to itself before it will answer anything.

This is the coverage the full shape buys, and it is why the shape is asserted
first: `provision-nc.sh` is executed by nothing anywhere else in the repo —
`docker-compose.yml` mounts it as a Nextcloud post-install hook and that is
its only invocation — and the entrypoint's Talk room provisioning (`istota
nextcloud provision-rooms`, run for the first admin) meets a real Talk only
here. So the room find-and-reuse path, the OAuth2 registration and the exec
into the scheduler have their only witness in this file.

**The config is an input on this shape, as on every other.** The testbed
writes `config.toml` and the admins file the way `istota setup` would, and the
entrypoint reads them and writes neither. What the boot itself does is the
master key, `istota init`, the first admin's profile row, and the rooms.

**Everything here is an outcome assertion, and that is not a style preference.**
Every `occ` call in `provision-nc.sh` is `|| true`, and so is the OAuth PHP
block. The script writes `/mnt/shared/.istota-provisioned` and reports success
having done nothing at all. The flag proves the script ran; only the outcomes
prove it worked. That is defensible production resilience — an operator would
rather have a bot with no calendar app than no bot — and it means this file is
the only thing standing between "provisioned" and "silently empty".

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
first boot onwards, and the rooms are recovered by record or by name rather than
recreated. A test added here that asserts on a task or on the scripted endpoint
breaks that and needs a per-test reset.
"""

from __future__ import annotations

import json

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

#: Where `istota setup` writes the admin allowlist, and where the testbed does.
ADMINS_FILE = "/data/config/admins"

#: The three group rooms `istota nextcloud provision-rooms` creates, by display
#: name. Not user-prefixed: lookups are scoped by the user's participation, so
#: each user gets their own set of identically-named rooms.
GROUP_ROOMS = ("general", "logs", "alerts")

#: Talk's room types. 2 is a group room, 3 is public — and the distinction is
#: the point of one assertion below: #logs carries the execution log and
#: #alerts carries confirmations, and a public room is joinable by anyone
#: holding its token.
ROOM_TYPE_GROUP = 2

#: What the testbed writes as the scripted endpoint's key, the one non-empty
#: brain credential in this tier.
BRAIN_CREDENTIALS = {
    "ANTHROPIC_API_KEY": "",
    "CLAUDE_CODE_OAUTH_TOKEN": "",
    "ISTOTA_BRAIN_NATIVE_API_KEY": compose_support.SCRIPTED_ENDPOINT_KEY,
}

#: Clears what provisioning recorded, so the next boot takes the name path.
#: The record lives in a reserved `istota_kv` namespace the CLI refuses, so it
#: goes through SQLite, as the daemon's uid. The two channel columns are
#: cleared too: with them set, `provision-rooms` would not look `logs` and
#: `alerts` up at all.
FORGET_ROOMS = """
import sqlite3
conn = sqlite3.connect('/data/db/istota.db')
conn.execute("DELETE FROM istota_kv WHERE user_id = 'testuser' AND namespace = '_provisioned_rooms'")
conn.execute("UPDATE user_profiles SET log_channel = '', alerts_channel = '' WHERE user_id = 'testuser'")
conn.commit()
"""


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


def _recorded_rooms(stack) -> dict[str, str]:
    rows = stack.probe.query(
        "SELECT key, value FROM istota_kv WHERE user_id = ? AND namespace = ?",
        ["testuser", "_provisioned_rooms"],
    )
    recorded = {}
    for row in rows:
        value = json.loads(row["value"])
        recorded[row["key"]] = value.get("token") if isinstance(value, dict) else value
    return recorded


class TestFirstInstallProvisioning:
    """What `provision-nc.sh` and the entrypoint left behind on a cold volume set."""

    @pytest.mark.parametrize("service", ["istota", "web"])
    def test_both_login_methods_are_enabled(self, provisioned, service):
        # The venv's interpreter, not `uv run`: web's root is read-only, and uv
        # wants to write its cache and lock before it runs anything.
        result = provisioned.exec([
            "/app/.venv/bin/python", "-c",
            "import json; from pathlib import Path; from istota.config import load_config; "
            "print(json.dumps(load_config(Path('/data/config/config.toml')).web.auth))",
        ], service=service)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout) == ["nextcloud", "email"]

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
        # And it is the client the daemon's config names: the pair is handed to
        # `provision-nc.sh` and to the config alike, the route an operator takes
        # with `istota setup`, since nothing copies a minted client any more.
        config = provisioned.exec(["cat", "/data/config/config.toml"]).stdout
        assert f'oauth2_client_id = "{istota_clients[0]["client_identifier"]}"' in config

    def test_the_first_admin_was_ensured_from_the_allowlist(self, provisioned):
        """The entrypoint's one user bootstrap: with an empty profile table it
        ensures the first id in the admins file, the file `istota setup`
        writes. Nothing else creates a user, so a multi-user install is never
        rewritten by a restart."""
        admins = provisioned.exec(["cat", ADMINS_FILE])
        assert admins.returncode == 0, admins.stderr
        assert admins.stdout.split() == ["testuser"], admins.stdout

        rows = provisioned.probe.query("SELECT user_id FROM user_profiles")
        assert [row["user_id"] for row in rows] == ["testuser"]

    def test_the_config_is_the_one_the_testbed_wrote(self, provisioned):
        """The config is an input: the boot reads it and never writes it.
        Compared against the bound file's bytes, since the daemon reading a
        config it had rewritten would pass every other assertion here."""
        written = (provisioned.config_dir / "config.toml").read_text()
        in_container = provisioned.exec(["cat", "/data/config/config.toml"])

        assert in_container.returncode == 0, in_container.stderr
        assert in_container.stdout == written

    def test_the_default_talk_rooms_exist_as_group_rooms(self, provisioned):
        """Three group rooms, with both users in each, recorded and seeded.

        `roomType=2`, not 3: #logs carries the daemon's execution log and
        #alerts carries confirmations and security alerts, and a public room is
        joinable by anyone holding its token. `istota nextcloud provision-rooms`
        makes them, run by the entrypoint for the first admin; elsewhere it is
        asserted against `MagicMock`.
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

        recorded = _recorded_rooms(provisioned)
        assert {name: by_name[name]["token"] for name in GROUP_ROOMS} == recorded
        channels = provisioned.probe.query(
            "SELECT log_channel, alerts_channel FROM user_profiles WHERE user_id = ?",
            ["testuser"],
        )
        assert channels == [{
            "log_channel": recorded["logs"], "alerts_channel": recorded["alerts"],
        }]


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
        before_record = _recorded_rooms(provisioned)
        assert set(before_record) == set(GROUP_ROOMS), before_record

        # --- a plain restart: the record names every room, so they are reused.
        provisioned.restart()
        provisioned.wait_healthy()

        assert _room_names(nextcloud) == sorted(before_rooms.values()), (
            "a restart created or lost a room"
        )
        assert _oauth_names(nextcloud) == before_clients

        # --- the record and the channel columns are gone, so every room is
        # looked up again and has to be found by name rather than created. This
        # is the path `rooms/provision.py` takes on a first provision against a
        # Nextcloud that already has the rooms.
        forgot = provisioned.exec(["python3", "-c", FORGET_ROOMS])
        assert forgot.returncode == 0, forgot.stderr
        assert _recorded_rooms(provisioned) == {}

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
        assert _recorded_rooms(provisioned) == before_record, (
            "the rewritten record names different room tokens"
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
        """A task that runs a command, on the shape with a real boot behind it.

        The config writes `sandbox_enabled = true`, so every filesystem-touching
        task here goes through bubblewrap, under the shipped seccomp profile.
        Without the grant Docker's default profile blocks the
        `unshare(CLONE_NEWUSER)` bwrap needs: `bwrap --unshare-user --ro-bind /
        / -- /bin/true` inside the shipped image exits 1 with "No permissions to
        create new namespace" without it and 0 with it.

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
            "the seccomp profile and `systempaths=unconfined` reached the "
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
        """Read the dropped process's environment, which is what a task's
        parent sees.

        The credentials are secret files now, read into the environment by
        `istota-secrets` after the drop, so nothing a developer exports in the
        shell that started pytest can reach the container. Compared against
        what the testbed wrote rather than merely checked for emptiness:
        `ISTOTA_BRAIN_NATIVE_API_KEY` is deliberately non-empty (the daemon
        sends *something* to the scripted endpoint), so "not empty" would pass
        on a real key too.
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
        # The credentials file the entrypoint writes when the OAuth token is
        # non-empty. Its absence is the second half of the claim: a marker value
        # rather than an empty file would have had the boot write a fake
        # credential and log that Claude Code was configured.
        assert provisioned.exec(
            ["test", "-e", "/data/home/.claude/.credentials.json"]
        ).returncode != 0


def _room_names(nextcloud) -> list[str]:
    return sorted(room.get("displayName", "") for room in nextcloud.rooms())


def _oauth_names(nextcloud) -> list[str]:
    return sorted(row.get("name", "") for row in nextcloud.oauth_clients())
