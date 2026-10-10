#!/bin/bash
# Prove the vm tier can see a broken VM.
#
# Each control breaks one thing a parity row depends on, on the tier's own VM,
# runs the witness nodes it must turn red, and requires every one of them on a
# FAILED line of pytest's summary (the convention of
# scripts/test-image-negative-control.sh: a control can otherwise pass on an
# unrelated failure). **A clean run of a witness here is the failure.**
#
# The break and its undo live in the witness module's own fixture, keyed on
# ISTOTA_VM_CONTROL, so each is written beside the assertion it targets. Every
# control restores what it broke when its session ends.
#
#   scripts/test-vm-negative-control.sh            # every control
#   scripts/test-vm-negative-control.sh <name>     # one, by name
#
# Controls, grouped by the stack mode they run in:
#
#   local:     mac-home-mounted (row 7), secret-owned-by-root (row 12),
#              apparmor-unconfined (row 20), no-egress-unit (row 10),
#              every-cred-volume-everywhere (row 11),
#              browser-on-default-network (row 17)
#   direct:    certbot-disabled (row 19)
#   any:       no-host-bounds (row 15)
#   nextcloud: no-storage-refusal (row 9)
#   proxied:   no-allow-list, no-docker-user-rule (row 18)
#
# Run scripts/test-vm.sh first: the controls reuse its VM and images. No arrays:
# macOS ships bash 3.2.
set -euo pipefail

cd "$(dirname "$0")/.."

only="${1:-}"
VM=tests/vm
# Each pytest session leaves the VM running for the next; this script stops it
# at the end unless the caller asked to keep it.
keep="${ISTOTA_VM_KEEP_RUNNING:-}"
export ISTOTA_VM_KEEP_RUNNING=1
ran=0

control() {
    name="$1"
    expect="$2"
    shift 2
    if [ -n "$only" ] && [ "$only" != "$name" ]; then
        return 0
    fi
    ran=1
    echo
    echo "[control] ${name}: ${expect}"
    out="$(mktemp)"
    set +e
    ISTOTA_VM_CONTROL="$name" uv run pytest -m vm -n0 -p no:randomly -q --no-header -rfE "$@" 2>&1 | tee "$out"
    set -e
    missing=""
    for node in "$@"; do
        if ! grep -Fq "FAILED ${node}" "$out"; then
            missing="${missing} ${node}"
        fi
    done
    rm -f "$out"
    if [ -n "$missing" ]; then
        echo "[control] FAILED: ${name} did not turn these red:"
        for node in $missing; do echo "[control]   ${node}"; done
        echo "[control] Expected: ${expect}"
        exit 1
    fi
    echo "[control] OK: ${name} turned every named witness red."
}

C="$VM/test_confinement.py"
control mac-home-mounted \
    "row 7: a Mac directory mounted into the VM" \
    "$C::TestTheDaemonCannotSeeTheMachine::test_the_vm_mounts_no_mac_path_but_the_read_only_checkout"
control secret-owned-by-root \
    "row 12: a secret file on the VM owned by root" \
    "$C::TestTheSecretFilesAreTheDaemons::test_every_secret_file_is_0400_and_owned_by_10001_inside_the_container"
control apparmor-unconfined \
    "row 20: the istota service with apparmor=unconfined" \
    "$C::TestTheContainerIsConfinedByAppArmor::test_the_istota_profile_is_in_force" \
    "$C::TestTheContainerIsConfinedByAppArmor::test_a_root_exec_cannot_write_core_pattern" \
    "$C::TestTheContainerIsConfinedByAppArmor::test_a_root_exec_cannot_write_the_sysrq_trigger"

D="$VM/test_devbox.py"
control no-egress-unit \
    "row 10: the VM without the devbox egress rules" \
    "$D::TestADevboxReachesNoPrivateRange::test_a_devbox_reaches_neither_and_does_reach_the_internet" \
    "$D::TestADevboxReachesNoPrivateRange::test_doctor_reports_the_egress_probe_ok"
control every-cred-volume-everywhere \
    "row 11: every credential volume mounted into every devbox" \
    "$D::TestACredentialSocketReachesOnlyItsOwner::test_each_devbox_mounts_only_its_own_credential_volume" \
    "$D::TestACredentialSocketReachesOnlyItsOwner::test_the_one_socket_a_devbox_sees_answers_as_its_owner"

control browser-on-default-network \
    "row 17: the browser container on the default network" \
    "$VM/test_browser.py::TestTheBrowserReachesNoPublishedPort::test_the_browser_reaches_nothing_on_the_default_network_by_address"

control certbot-disabled \
    "row 19: certbot masked before the first issuance" \
    "$VM/test_direct.py::TestDirectIngressServesTls::test_the_certificate_is_issued_and_served" \
    "$VM/test_direct.py::TestDirectIngressServesTls::test_doctor_reports_web_tls_ok"

control no-host-bounds \
    "row 15: provision.sh without the journal cap and zram" \
    "$VM/test_host.py::TestTheHostIsBounded::test_the_journal_is_capped" \
    "$VM/test_host.py::TestTheHostIsBounded::test_zram_swap_is_on_at_the_roles_priority"

control no-storage-refusal \
    "row 9: an entrypoint without the mount refusal" \
    "$VM/test_nextcloud.py::TestNothingIsWrittenIntoAnUnmountedStore::test_with_the_mount_stopped_the_daemon_does_not_start"

P="$VM/test_proxied.py"
control no-allow-list \
    "row 18: nginx without the UPSTREAM_PROXY allow list" \
    "$P::TestTheProxiedListenerAnswersOnlyTheUpstream::test_nginx_refuses_a_peer_outside_upstream_proxy"
control no-docker-user-rule \
    "row 18: the VM without the proxied listener's DOCKER-USER rule" \
    "$P::TestTheProxiedListenerAnswersOnlyTheUpstream::test_an_outsider_cannot_connect_and_the_upstream_can"
# No userland-proxy control: on Docker Engine 29.9 the setting does not change
# the address an upstream on another host arrives from (tests/vm/test_proxied.py).

if [ "$ran" -eq 0 ]; then
    echo "[control] no control named '${only}'" >&2
    exit 64
fi
if [ "$keep" != "1" ]; then
    limactl stop "${ISTOTA_VM_NAME:-istota-vmtier-1}" >/dev/null 2>&1 || true
fi
echo
echo "[control] all controls OK"
