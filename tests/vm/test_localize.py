"""`istota-stack localize-workspace`: a mounted VM moved onto a local workspace.

The stack starts in full Nextcloud integration as `istota-stack setup` writes
it (the role's config is refused by the verb, by design), over the VM's rclone
mount of the fixture's `Shared Files`. A file is written into a user's bot
folder through the mount, the verb runs as an operator would, and the stack
must come back on `/data/workspace` with that file in it, the mount unit off,
`MOVED.md` in Nextcloud, new writes staying local, and the Nextcloud connection
the `nextcloud` skill and Talk use still answering.

The move leaves the VM out of the `nextcloud-wizard` mode, so the fixture
removes the mode record afterwards and the next session sets the mode up again.
`/data/workspace` is emptied before the move, since the local mode's sessions
use it too.

Control `localize-without-config`: `istota-stack` with the config edit removed,
so the copy runs and the mount is turned off while `config.toml` still names
it. The stack must not come up on the local workspace.
"""

from __future__ import annotations

import shlex
import urllib.parse
import uuid

import pytest

from . import lima

pytestmark = pytest.mark.vm

CONTROL = "localize-without-config"


def _webdav_get(vm: lima.Vm, fixture: dict, name: str) -> tuple[str, str]:
    """(body, status) of a GET on the bot's `Shared Files/<name>`."""
    quoted = urllib.parse.quote(name)
    url = f"http://127.0.0.1:{lima.NC_PORT}/remote.php/dav/files/{lima.NC_BOT}/Shared%20Files/{quoted}"
    out = vm.out(
        f"curl -s -m 30 -u {shlex.quote(lima.NC_BOT + ':' + fixture['BOT_PASSWORD'])} "
        f"-w '\\n%{{http_code}}' {shlex.quote(url)}")
    body, _, status = out.rpartition("\n")
    return body, status


@pytest.fixture(scope="module")
def moved(vm, release, model) -> dict:
    fixture = lima.ensure_nextcloud(vm)

    def place_rclone_conf(machine: lima.Vm) -> None:
        machine.write(f"{lima.STACK}/rclone.conf", fixture["rclone_conf"], mode="0600", owner="10001")

    lima.ensure_mode(
        vm, release, lima.nextcloud_wizard_mode(model.vm_url, fixture["url"], fixture["BOT_PASSWORD"]),
        prepare=place_rclone_conf,
    )
    bot = lima.stack_exec(
        vm, f"for d in /mnt/shared/Users/{lima.USER}/*/; do if [ -d \"$d/config\" ]; then basename \"$d\"; fi; done"
    ).stdout.split()[0]
    name = f"vmtier-{uuid.uuid4().hex[:10]}.txt"
    lima.stack_exec(vm, f"mkdir -p /mnt/shared/Users/{lima.USER}/{bot}/notes && "
                        f"echo before-the-move > /mnt/shared/Users/{lima.USER}/{bot}/notes/{name}")
    # As 10001: the daemon service drops CAP_DAC_OVERRIDE, so its root cannot
    # delete what a local-mode session left under 10001's directories.
    vm.run("istota-stack compose run --rm --no-deps -T --entrypoint istota-drop daemon "
           "sh -c 'mkdir -p /data/workspace && find /data/workspace -mindepth 1 -delete'", timeout=600)
    command = lima.stack_command(vm, CONTROL if lima.control() == CONTROL else "")
    try:
        result = vm.run(f"{command} localize-workspace", check=False, timeout=lima.BUILD_TIMEOUT)
    finally:
        vm.run(f"rm -f {lima.MODE_FILE}")
    return {"result": result, "bot": bot, "name": name, "fixture": fixture}


class TestAMountedVmMovesToALocalWorkspace:
    def test_the_move_exits_0(self, vm, moved):
        result = moved["result"]
        assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]

    def test_the_config_names_the_local_workspace(self, vm, moved):
        workspace = vm.out(
            "python3 -c 'import tomllib; c = tomllib.load(open(\"/srv/app/istota/config/config.toml\", \"rb\")); "
            "print(c.get(\"workspace_path\"), c.get(\"workspace_mount_path\"), c.get(\"nextcloud_mount_path\"))'")
        assert workspace == "/data/workspace None None", workspace
        assert vm.ok(f"test -f {lima.STACK}/config/config.toml.pre-localize")
        assert lima.env_value(vm, f"{lima.STACK}/host.env", "STORAGE") == "local"

    def test_the_mount_unit_is_off(self, vm, moved):
        assert vm.out("systemctl is-active mount-nextcloud.service || true") != "active"
        unit = vm.out("systemctl show istota-stack.service -p Requires")
        assert "mount-nextcloud.service" not in unit, unit

    def test_a_file_written_before_the_move_is_read_from_the_local_workspace(self, vm, moved):
        lima.wait_healthy(vm)
        path = f"/data/workspace/Users/{lima.USER}/{moved['bot']}/notes/{moved['name']}"
        assert lima.stack_exec(vm, f"cat {path}").stdout.strip() == "before-the-move"
        assert lima.doctor(vm, "runtime.workspace_mode")["status"] in ("ok", "skip")

    def test_a_file_written_after_the_move_stays_local(self, vm, moved):
        lima.wait_healthy(vm)
        name = f"after-{uuid.uuid4().hex[:10]}.txt"
        rel = f"Users/{lima.USER}/{moved['bot']}/notes/{name}"
        lima.stack_exec(vm, f"echo after-the-move > /data/workspace/{rel}")
        assert lima.stack_exec(vm, f"cat /data/workspace/{rel}").stdout.strip() == "after-the-move"
        _, status = _webdav_get(vm, moved["fixture"], rel)
        assert status == "404", status

    def test_moved_md_is_in_the_users_nextcloud_folder(self, vm, moved):
        body, status = _webdav_get(vm, moved["fixture"], f"Users/{lima.USER}/{moved['bot']}/MOVED.md")
        assert status == "200", status
        assert "web UI" in body

    def test_the_nextcloud_connection_still_answers(self, vm, moved):
        """Talk and the `nextcloud` skill reach Nextcloud with the URL and app
        password, which the move keeps; the capabilities fetch is that call."""
        lima.wait_healthy(vm)
        out = lima.stack_exec(vm, "istota nextcloud capabilities --raw").stdout
        assert "spreed" in out, out[-2000:]
