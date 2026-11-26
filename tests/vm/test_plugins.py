"""`istota-stack plugins sync` on a provisioned VM (spec: external-skill-packages, Stage 7).

The operator's way, start to finish: the fixture wheel copied into
`plugins/src/` as root, `istota-stack plugins sync`, and the result read off
the host and out of the running daemon. A change exits 2 and restarts daemon
and web; doctor's `skills.plugins` then lists the plugin; a second sync is
`unchanged` and exits 0; deleting the wheel and syncing removes it again.

The module starts from no wheels and leaves none, whatever an earlier session
left behind.
"""

from __future__ import annotations

import shlex

import pytest

from tests.support.plugin_fixture import (
    WHEEL_DIST,
    WHEEL_NAME,
    WHEEL_PACKAGE,
    WHEEL_SKILL,
    WHEEL_VERSION,
    PluginBuilder,
)

from . import lima

pytestmark = pytest.mark.vm

PLUGINS = f"{lima.STACK}/plugins"
WHEEL = f"{PLUGINS}/src/{WHEEL_NAME}"
SITE = f"{PLUGINS}/installed/site"
LOADED = f"{WHEEL_SKILL}: {WHEEL_DIST} {WHEEL_VERSION} ({WHEEL_PACKAGE})"


def _sync(vm: lima.Vm):
    return vm.run("istota-stack plugins sync < /dev/null", check=False, timeout=900)


def _no_wheels(vm: lima.Vm) -> None:
    vm.run(f"find {PLUGINS}/src -mindepth 1 -maxdepth 1 -name '*.whl' -delete")
    result = _sync(vm)
    assert result.returncode in (0, 2), result.stdout + result.stderr
    lima.wait_healthy(vm)


def _started(vm: lima.Vm, service: str) -> str:
    """The service's container and when it last started, which a restart moves."""
    cid = lima.container(vm, service)
    assert cid, f"no running {service} container"
    return vm.out(f"docker inspect -f '{{{{.Id}}}} {{{{.State.StartedAt}}}}' {cid}")


@pytest.fixture(scope="module")
def wheel(tmp_path_factory) -> bytes:
    root = tmp_path_factory.mktemp("vm-plugin")
    return PluginBuilder(root / "build").wheel(root / "wheels").read_bytes()


@pytest.fixture(scope="module")
def stack(local_stack):
    _no_wheels(local_stack)
    try:
        yield local_stack
    finally:
        _no_wheels(local_stack)


class TestPluginsSync:
    def test_a_new_wheel_is_installed_and_restarts_daemon_and_web(self, stack, wheel):
        before = {service: _started(stack, service) for service in ("daemon", "web")}
        stack.write(WHEEL, wheel, mode="0644", owner="root")

        result = _sync(stack)

        output = result.stdout + result.stderr
        assert result.returncode == 2, output
        assert "changed" in result.stdout.splitlines(), output
        lima.wait_healthy(stack)
        after = {service: _started(stack, service) for service in ("daemon", "web")}
        for service in ("daemon", "web"):
            assert after[service] != before[service], f"{service} was not restarted: {before[service]}"

    def test_the_tree_is_the_daemons_and_holds_the_plugin(self, stack):
        assert stack.out(f"stat -c '%u %a' {SITE}") == "10001 755"
        assert stack.ok(f"test -f {SITE}/{WHEEL_PACKAGE}/skill.md")
        assert stack.out(f"find {PLUGINS}/installed -perm /022 -print") == ""
        assert stack.out(f"stat -c '%u %a' {PLUGINS}/.synced") == "0 644"

    def test_doctor_in_the_running_daemon_lists_the_plugin(self, stack):
        check = lima.doctor(stack, "skills.plugins")
        assert check["status"] == "ok", check
        assert check["detail"] == LOADED, check

    def test_the_running_daemon_runs_its_cli(self, stack):
        result = lima.stack_exec(stack, f"istota-skill {shlex.quote(WHEEL_SKILL)} ping")
        assert result.stdout.strip() == '{"status": "ok", "cred_fd": false}', result.stdout + result.stderr

    def test_a_second_sync_is_unchanged(self, stack):
        before = _started(stack, "daemon")

        result = _sync(stack)

        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip().splitlines()[-1] == "unchanged", result.stdout
        assert _started(stack, "daemon") == before

    def test_status_says_synced(self, stack):
        result = stack.run("istota-stack plugins status < /dev/null")
        assert result.stdout.strip().splitlines() == [WHEEL_NAME, "synced"], result.stdout

    def test_removing_the_wheel_and_syncing_removes_the_plugin(self, stack):
        stack.run(f"rm -f {shlex.quote(WHEEL)}")

        result = _sync(stack)

        assert result.returncode == 2, result.stdout + result.stderr
        lima.wait_healthy(stack)
        assert stack.out(f"ls -A {SITE}") == ""
        check = lima.doctor(stack, "skills.plugins")
        assert check["status"] == "ok", check
        assert check["detail"] == "none installed", check
