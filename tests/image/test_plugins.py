"""Skill plugins in the built image (spec: external-skill-packages, Stage 7).

Three halves, each against the image as built:

- **The image** carries the mount point `/app/plugins` and the `.pth` that puts
  `/app/plugins/installed/site` on the venv's path, and with nothing bound the
  path is absent from `sys.path`.
- **The sync one-shot** is `host/istota-stack`'s `PLUGINS_SYNC_SH`, read out of
  the script by bash rather than retyped here, so the two cannot drift. It runs
  as `plugins sync` runs it: through `istota-drop`, `ISTOTA_SECRETS_DIR` set to
  nothing, the plugins directory read-only at `/app/plugins`, `installed/`
  writable at `/plugins-out`, and the wheel as its argument. On top of that the
  root is read-only with `/tmp` the only scratch space and there is no network,
  which is the question this file settles for `uv pip install --target`. It is
  run twice, because a re-sync runs with the live tree on `sys.path` and
  `check-tree` must not read that tree as a shadow of the one it checks.
- **The synced tree loads**: bound read-only at `/app/plugins`, `istota-skill
  skills list` shows the plugin with `origin: "plugin"`, doctor's
  `skills.plugins` lists it, and its CLI answers.

Negative control: `scripts/test-image-negative-control.sh plugins` builds the
image without the `.pth` and requires the loading half red and the sync half
green.
"""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path

import pytest

from tests.support.plugin_fixture import (
    WHEEL_DIST,
    WHEEL_PACKAGE,
    WHEEL_SKILL,
    WHEEL_VERSION,
    PluginBuilder,
)

from .conftest import REPO, assert_ok, run_in, sh

pytestmark = pytest.mark.image

# Literals, not imported from the code under test.
PLUGINS = "/app/plugins"
SITE = "/app/plugins/installed/site"
PTH = "istota-plugins.pth"
ISTOTA_DROP = "/usr/local/bin/istota-drop"
STACK_SCRIPT = REPO / "host" / "istota-stack"
CONFIG = "/tmp/data/config/config.toml"
SETUP = "istota setup --yes --data-dir /tmp/data --user testuser >/tmp/setup.log 2>&1 || { cat /tmp/setup.log; exit 1; }"

# What the sync's one-shot adds on top of `docker run`: the shipped daemon
# service is read-only with a /tmp tmpfs, and nothing here needs the network.
LOCKED_DOWN = ["--read-only", "--tmpfs", "/tmp", "--network", "none"]


def sync_script() -> str:
    """`PLUGINS_SYNC_SH` as `host/istota-stack` defines it, parsed by bash.

    The assignment is the line that starts it through the first line that does
    not continue, evaluated by bash on its own, so the quoting is bash's.
    """
    lines = STACK_SCRIPT.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.startswith("PLUGINS_SYNC_SH="))
    end = start
    while lines[end].endswith("\\"):
        end += 1
    assignment = "\n".join(lines[start:end + 1])
    result = subprocess.run(
        ["bash", "-c", assignment + '\nprintf %s "$PLUGINS_SYNC_SH"'],
        capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0 and "uv pip install" in result.stdout, (
        f"could not read PLUGINS_SYNC_SH out of {STACK_SCRIPT}: {result.stderr}"
    )
    return result.stdout


def run_sync(image, plugins: Path) -> subprocess.CompletedProcess:
    """One `plugins sync` one-shot over `plugins/src/`, as istota-stack runs it."""
    wheels = sorted(p.name for p in (plugins / "src").iterdir() if p.name.endswith(".whl"))
    return run_in(
        image,
        ["sh", "-c", sync_script(), "plugins-sync", *(f"{PLUGINS}/src/{name}" for name in wheels)],
        entrypoint=ISTOTA_DROP,
        env={"ISTOTA_SECRETS_DIR": "/nonexistent"},
        volumes=[(plugins, PLUGINS, "ro"), (plugins / "installed", "/plugins-out", "rw")],
        docker_args=LOCKED_DOWN,
    )


def with_plugins(image, plugins: Path, script: str) -> subprocess.CompletedProcess:
    """`script` as uid 10001 with the plugins directory bound as the overlay binds it.

    The log file goes to /tmp: the image's default is under /data, which a
    read-only root without the state bind does not have.
    """
    return run_in(
        image, ["sh", "-c", script], entrypoint=ISTOTA_DROP,
        env={"ISTOTA_DEFAULT_LOG_FILE": "/tmp/istota.log"},
        volumes=[(plugins, PLUGINS, "ro")],
        docker_args=LOCKED_DOWN,
    )


@pytest.fixture(scope="module")
def plugins(tmp_path_factory) -> Path:
    """A VM's `plugins/` layout holding the fixture wheel, before any sync."""
    root = tmp_path_factory.mktemp("plugins-layout")
    plugins = root / "plugins"
    (plugins / "src").mkdir(parents=True)
    installed = plugins / "installed"
    installed.mkdir()
    # The container writes as uid 10001, which owns installed/ on the VM.
    installed.chmod(0o777)
    builder = PluginBuilder(root / "build")
    builder.wheel(plugins / "src")
    return plugins


@pytest.fixture(scope="module")
def first_sync(istota_image, plugins) -> subprocess.CompletedProcess:
    return run_sync(istota_image, plugins)


@pytest.fixture(scope="module")
def synced(first_sync, plugins) -> Path:
    assert_ok(first_sync, "the plugins sync one-shot")
    return plugins


class TestTheImage:
    def test_the_mount_point_is_there_and_empty(self, istota_image):
        out = assert_ok(sh(istota_image, f"test -d {PLUGINS} && ls -A {PLUGINS}"), f"ls {PLUGINS}")
        assert out.strip() == ""

    def test_the_pth_names_the_synced_tree_alone(self, istota_image):
        out = assert_ok(
            sh(istota_image, "cat \"$(/app/.venv/bin/python -c "
                             "'import sysconfig; print(sysconfig.get_path(\"purelib\"))')\"/" + PTH),
            f"cat {PTH}",
        )
        assert out.splitlines() == [SITE]

    def test_with_nothing_bound_the_path_is_not_on_sys_path(self, istota_image):
        out = assert_ok(
            sh(istota_image, "/app/.venv/bin/python -c 'import sys; print(\"\\n\".join(sys.path))'"),
            "sys.path",
        )
        assert SITE not in out.splitlines()


class TestTheSyncOneShot:
    def test_it_installs_on_the_read_only_root_with_no_network(self, first_sync):
        assert_ok(first_sync, "the plugins sync one-shot")

    def test_it_leaves_one_tree_named_site(self, synced):
        assert sorted(p.name for p in (synced / "installed").iterdir()) == ["site"]

    def test_the_tree_holds_the_package_and_its_metadata(self, synced):
        site = synced / "installed" / "site"
        dist_info = site / f"{WHEEL_PACKAGE}-{WHEEL_VERSION}.dist-info"
        for path in ("__init__.py", "__main__.py", "skill.md"):
            assert (site / WHEEL_PACKAGE / path).is_file(), path
        assert f"Name: {WHEEL_DIST}" in (dist_info / "METADATA").read_text()
        assert (dist_info / "entry_points.txt").read_text().split() == [
            "[istota.skills]", WHEEL_SKILL, "=", WHEEL_PACKAGE,
        ]

    def test_nothing_in_the_tree_is_writable_by_group_or_others(self, istota_image, synced):
        out = assert_ok(
            with_plugins(istota_image, synced, f"find {SITE} -perm /022 -print"),
            "find the writable entries",
        )
        assert out.strip() == "", f"writable by others in the synced tree:\n{out}"

    def test_a_second_sync_with_the_tree_live_succeeds(self, istota_image, synced):
        """The live `site` is on `sys.path` in the one-shot; check-tree must leave it out."""
        again = run_sync(istota_image, synced)
        assert_ok(again, "the second plugins sync one-shot")
        assert sorted(p.name for p in (synced / "installed").iterdir()) == ["site"]
        assert (synced / "installed" / "site" / WHEEL_PACKAGE / "__init__.py").is_file()


class TestTheSyncedTreeLoads:
    def test_skills_list_shows_the_plugin(self, istota_image, synced):
        out = assert_ok(
            with_plugins(
                istota_image, synced,
                f"{SETUP}; ISTOTA_CONFIG_PATH={CONFIG} ISTOTA_USER_ID=testuser istota-skill skills list",
            ),
            "istota-skill skills list",
        )
        listed = {entry["name"]: entry for entry in json.loads(out)["skills"]}
        assert listed.get(WHEEL_SKILL) == {
            "name": WHEEL_SKILL,
            "description": "Example plugin skill",
            "cli": True,
            "origin": "plugin",
            "plugin": f"{WHEEL_DIST} {WHEEL_VERSION}",
        }, sorted(listed)

    def test_doctor_lists_it_and_accounts_for_the_wheel(self, istota_image, synced):
        out = assert_ok(
            with_plugins(
                istota_image, synced,
                f"{SETUP}; istota -c {CONFIG} doctor --only skills.plugins --json",
            ),
            "istota doctor --only skills.plugins",
        )
        results = {result["name"]: result for result in json.loads(out)}
        assert results["skills.plugins"]["status"] == "ok", results
        assert results["skills.plugins"]["detail"] == (
            f"{WHEEL_SKILL}: {WHEEL_DIST} {WHEEL_VERSION} ({WHEEL_PACKAGE})"
        )
        assert results["skills.plugins.wheels"]["status"] == "ok", results
        assert all(result["status"] == "ok" for result in results.values()), results

    def test_its_cli_runs_as_a_direct_run(self, istota_image, synced):
        out = assert_ok(
            with_plugins(istota_image, synced, f"istota-skill {shlex.quote(WHEEL_SKILL)} ping"),
            "istota-skill example ping",
        )
        assert json.loads(out) == {"status": "ok", "cred_fd": False}
