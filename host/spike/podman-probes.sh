#!/bin/bash
# Stage 1 spike: the bwrap and cgroup probes under rootful Podman on the same
# VM. Run as root from /srv/istota-spike after run-probes.sh setup.
#
#   podman-probes.sh load            copy the patched image from Docker
#   podman-probes.sh <variant>       run one variant, probe, remove
set -uo pipefail

HERE=/srv/istota-spike
cd "$HERE"
mkdir -p out
NAME=istota-spike-podman
IMAGE=docker.io/istota-spike/istota-patched:latest
PROFILE="$HERE/seccomp-istota.draft.json"

section() { echo; echo "=== $* ==="; }

# The run contract in Podman's spelling. `unmask=ALL` is Podman's form of
# Docker's `systempaths=unconfined`.
CONTRACT=(
    --security-opt "seccomp=$PROFILE"
    --security-opt unmask=ALL
    --security-opt no-new-privileges
    --cap-drop ALL
    --read-only --tmpfs /tmp
    --cgroupns private
    -v istota-spike-podman-data:/data
)

run_p() {
    local name="$1"; shift
    {
        echo "##### podman variant ${name}"
        echo "extra flags: $*"
        podman rm -f "$NAME" >/dev/null 2>&1
        podman volume rm -f istota-spike-podman-data >/dev/null 2>&1
        podman run -d --name "$NAME" "${CONTRACT[@]}" "$@" "$IMAGE" >/dev/null
        sleep 15
        echo "applied: $(podman inspect -f 'caps={{.EffectiveCaps}} apparmor={{.AppArmorProfile}} cgroupns={{.HostConfig.CgroupnsMode}} ro={{.HostConfig.ReadonlyRootfs}}' "$NAME")"
        section "container log"
        podman logs "$NAME" 2>&1 | grep -E '^\[spike|setpriv|mount' | head -20
        if [ "$(podman inspect -f '{{.State.Running}}' "$NAME")" = "true" ]; then
            section "plain podman exec (uid 0)"
            podman exec "$NAME" /spike/probes/procstatus.py self | grep -E 'Cap(Prm|Eff|Bnd)|NoNewPrivs|Seccomp|apparmor'
            podman exec "$NAME" /usr/bin/python3 -c '
for line in open("/proc/self/mountinfo"):
    f = line.split()
    if f[4] == "/sys/fs/cgroup":
        s = f.index("-"); print("cgroup mount: root=%s opts=%s type=%s" % (f[3], f[5], f[s + 1]))
print("/proc/self/cgroup:", open("/proc/self/cgroup").read().strip())
import os
try:
    os.mkdir("/sys/fs/cgroup/probe-root"); print("root mkdir in /sys/fs/cgroup: ok"); os.rmdir("/sys/fs/cgroup/probe-root")
except OSError as e:
    print("root mkdir in /sys/fs/cgroup:", e.strerror)
'
            section "daemon"
            podman exec --user 10001:10001 "$NAME" /spike/probes/procstatus.py daemon | grep -E 'daemon|Uid|Cap(Prm|Eff|Bnd)|NoNewPrivs|apparmor'
            section "podman exec --user 10001:10001"
            podman exec --user 10001:10001 "$NAME" /spike/probes/procstatus.py self | grep -E 'Cap(Prm|Eff|Bnd)|NoNewPrivs'
            section "bwrap as 10001"
            podman exec --user 10001:10001 "$NAME" /spike/probes/bwrap.sh
            section "cgroup as 10001"
            podman exec --user 10001:10001 -e ISTOTA_TASK_CGROUP_ROOT=/sys/fs/cgroup "$NAME" /spike/probes/cgroup.py
        else
            section "container exited"
            podman inspect -f 'exit={{.State.ExitCode}}' "$NAME"
            podman logs "$NAME" 2>&1 | tail -8
        fi
        podman rm -f "$NAME" >/dev/null 2>&1
        podman volume rm -f istota-spike-podman-data >/dev/null 2>&1
    } 2>&1 | tee "out/podman-${name}.log"
}

ROOT_CAPS=(--cap-add CHOWN --cap-add FOWNER --cap-add SETUID --cap-add SETGID --cap-add SETPCAP)

case "${1:-}" in
    load)
        docker save istota-spike/istota-patched:latest | podman load
        podman --version
        podman info --format 'cgroup v{{.Host.CgroupsVersion}} manager={{.Host.CgroupManager}} rootless={{.Host.Security.Rootless}} apparmor={{.Host.Security.AppArmorEnabled}}'
        ;;
    no-sysadmin-default-apparmor)
        run_p no-sysadmin-default-apparmor "${ROOT_CAPS[@]}" ;;
    no-sysadmin-istota-apparmor)
        run_p no-sysadmin-istota-apparmor "${ROOT_CAPS[@]}" --security-opt apparmor=istota-spike-default ;;
    sysadmin-istota-apparmor)
        run_p sysadmin-istota-apparmor "${ROOT_CAPS[@]}" --cap-add SYS_ADMIN --security-opt apparmor=istota-spike-default ;;
    unmask-cgroup-no-sysadmin)
        # Whether Podman's own knob gives a writable cgroup mount without the cap.
        run_p unmask-cgroup-no-sysadmin "${ROOT_CAPS[@]}" --security-opt apparmor=istota-spike-default \
            --security-opt unmask=/sys/fs/cgroup ;;
    systemd-always-no-sysadmin)
        run_p systemd-always-no-sysadmin "${ROOT_CAPS[@]}" --security-opt apparmor=istota-spike-default --systemd=always ;;
    *)
        echo "unknown step: ${1:-}" >&2; exit 2 ;;
esac
