"""The vm tier: the stack as provisioned on a dedicated VM, in a Lima VM.

The image and smoke tiers run the built image under Docker Desktop, which has
no AppArmor, no host firewall the stack writes to, no rclone mount, no systemd
and no `provision.sh`. This tier boots a Debian 13 VM from
`host/lima/istota.yaml`, lets `provision.sh` set it up at first boot, deploys a
signed tag of this checkout through `istota-stack update`, and witnesses the
security parity rows that only a real VM can hold (7, 9, 10, 11, 15, 18, 19,
20, and the halves of 12 and 17 Docker Desktop cannot see). `tests/vm/lima.py`
is the harness and says how the release and the modes work.

Run it with `scripts/test-vm.sh`; `scripts/test-vm-negative-control.sh` runs
the controls. Session-scoped and `-n0`, like the other artifact tiers, and it
skips with a reason where `limactl` is missing. The VM is kept between
sessions (stopped at the end of each, unless `ISTOTA_VM_KEEP_RUNNING=1`), so
only the first session pays for the boot and the builds.

Variables:

| Variable | Effect |
|---|---|
| `ISTOTA_VM_NAME` | the Lima instance (default `istota-vmtier-1`) |
| `ISTOTA_VM_WORKDIR` | the release repository and signing key (default `~/.cache/istota-vmtier`) |
| `ISTOTA_VM_KEEP_RUNNING` | `1` leaves the VM running after the session |
| `ISTOTA_VM_MODEL_PORT` | the scripted model's port on the Mac (default 18731) |
| `ISTOTA_VM_MEMORY` | memory for a VM this session creates (the template's 6GiB otherwise) |
| `ISTOTA_VM_CONTROL` | the negative control to apply; set by the control driver only |
"""

from __future__ import annotations

import os

import pytest

from . import lima

_XDIST_MESSAGE = (
    "the vm tier must run with -n0: its fixtures are session-scoped and drive one VM, "
    "and N workers would each reconfigure the same stack under the others."
)

DEFAULT_MODEL_PORT = 18731


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    if not any(item.get_closest_marker("vm") for item in items):
        return
    workers = getattr(config.option, "numprocesses", None)
    distribution = config.getoption("dist", "no")
    if workers or distribution not in ("no", None):
        raise pytest.UsageError(f"{_XDIST_MESSAGE} (saw -n {workers}, --dist {distribution})")


def _require_no_xdist(config) -> None:
    if hasattr(config, "workerinput"):
        worker = config.workerinput.get("workerid", "?")
        pytest.fail(f"{_XDIST_MESSAGE} (running in xdist worker {worker})", pytrace=False)


@pytest.fixture(scope="session")
def release(request) -> lima.Release:
    _require_no_xdist(request.config)
    if not lima.limactl_available():
        pytest.skip("limactl is not installed; the vm tier boots a Lima VM")
    return lima.snapshot_release()


@pytest.fixture(scope="session")
def vm(release) -> lima.Vm:
    machine = lima.ensure_vm(release)
    try:
        lima.ensure_release(machine, release)
        yield machine
    finally:
        if os.environ.get("ISTOTA_VM_KEEP_RUNNING", "").strip() != "1":
            machine.stop()


@pytest.fixture(scope="session")
def model(vm):
    """The scripted model endpoint, on the Mac's loopback.

    Lima forwards the VM's host address to the Mac's loopback, so the daemon in
    the VM reaches a listener bound to 127.0.0.1 here and nothing else on the
    network can. A fixed port keeps the config every mode writes the same
    across sessions; a port in use falls back to an ephemeral one, which costs
    the next mode a setup run.
    """
    from testbed.services.model_endpoint import serve_script

    port = int(os.environ.get("ISTOTA_VM_MODEL_PORT") or DEFAULT_MODEL_PORT)
    try:
        endpoint = serve_script([], port=port)
    except OSError:
        endpoint = serve_script([])
    endpoint.vm_url = f"http://{lima.mac_address(vm)}:{endpoint.port}/v1"
    try:
        yield endpoint
    finally:
        endpoint.close()


@pytest.fixture(scope="module")
def local_stack(vm, release, model) -> lima.Vm:
    """Local ingress, local storage, two devbox users, the browser and signaling profiles."""
    lima.ensure_mode(vm, release, lima.local_mode(model.vm_url), prepare=lima.ensure_metadata_namespace)
    return vm


@pytest.fixture(scope="module")
def proxied_stack(vm, release, model) -> lima.Vm:
    lima.ensure_mode(vm, release, lima.proxied_mode(model.vm_url), prepare=lima.ensure_proxied_namespaces)
    return vm


@pytest.fixture(scope="module")
def direct_stack(vm, release, model) -> lima.Vm:
    lima.ensure_mode(vm, release, lima.direct_mode(model.vm_url), prepare=lima.ensure_pebble)
    return vm


@pytest.fixture(scope="module")
def nextcloud_stack(vm, release, model) -> dict:
    """Full Nextcloud integration over the VM's rclone mount of the fixture's `Shared Files`."""
    fixture = lima.ensure_nextcloud(vm)
    lima.ensure_mode(vm, release, lima.nextcloud_mode(model.vm_url, fixture["url"], fixture["BOT_PASSWORD"]))
    return fixture
