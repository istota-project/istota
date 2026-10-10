"""Parity row 15: the host's journal cap and compressed swap, from provision.sh.

The Ansible role bounded the journal and installed zram on every host it
converged; on the new shape `host/provision.sh` does, and this reads the
result off the VM rather than off the script.

Control `no-host-bounds` (`scripts/test-vm-negative-control.sh`): the VM's
journal cap and zram are removed and provision.sh is run with those two
sections cut out, which is "provision without them"; the real script restores
them afterwards.
"""

from __future__ import annotations

import pytest

from tests.support import parity

from . import lima

pytestmark = pytest.mark.vm

_BOUNDS_START = "# --- Swap: zram"
_BOUNDS_END = "# --- The daemon's uid on the host"


def _provision_without_bounds(vm: lima.Vm) -> None:
    vm.run(f"""
systemctl stop dev-zram0.swap 2>/dev/null || true
rm -f /etc/systemd/zram-generator.conf /etc/systemd/journald.conf.d/istota.conf
systemctl daemon-reload
systemctl restart systemd-journald
copy={lima.STACK}/src/host/.provision-without-bounds.sh
awk -v start={_BOUNDS_START!r} -v stop={_BOUNDS_END!r} '
    index($0, start) == 1 {{ skip = 1 }}
    index($0, stop) == 1 {{ skip = 0 }}
    !skip' {lima.STACK}/src/host/provision.sh > "$copy"
grep -q 'zram-generator' "$copy" && {{ echo "the cut left zram in" >&2; exit 1; }}
chmod 0755 "$copy"
"$copy"
rm -f "$copy"
""", timeout=1800)


@pytest.fixture(scope="module")
def host(vm):
    if lima.control() != "no-host-bounds":
        yield vm
        return
    _provision_without_bounds(vm)
    try:
        yield vm
    finally:
        vm.run(f"{lima.STACK}/src/host/provision.sh", timeout=1800)


@parity.witness(15)
class TestTheHostIsBounded:
    def test_the_journal_is_capped(self, host):
        dropin = host.read("/etc/systemd/journald.conf.d/istota.conf")
        assert "SystemMaxUse=500M" in dropin, dropin
        effective = host.out(
            "systemd-analyze cat-config systemd/journald.conf | grep -E '^SystemMaxUse=' | tail -1 || true"
        )
        assert effective == "SystemMaxUse=500M", effective

    def test_zram_swap_is_on_at_the_roles_priority(self, host):
        swaps = [line.split() for line in host.out("swapon --show=NAME,TYPE,PRIO --noheadings --raw").splitlines()]
        zram = [fields for fields in swaps if fields and fields[0] == "/dev/zram0"]
        assert zram and zram[0][2] == "100", swaps
        assert "[zstd]" in host.read("/sys/block/zram0/comp_algorithm")
