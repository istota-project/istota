#!/bin/bash
# Stage 1 spike driver. Run as root inside the spike VM, from a copy of
# host/spike at /srv/istota-spike, after install-engines.sh and after the
# current image is loaded as istota-spike/istota:<tag>.
#
#   run-probes.sh setup <base-image>   build the patched image, derive profiles
#   run-probes.sh <variant>            bring the stack up as <variant>, probe, down
#
# Every probe's output is printed and kept under out/<variant>.log.
set -uo pipefail

HERE=/srv/istota-spike
cd "$HERE"
mkdir -p out profiles secrets

compose() { docker compose -f compose.spike.yml "${OVERLAYS[@]/#/-f}" "$@"; }

wait_for_daemon() {
    for _ in $(seq 1 60); do
        if compose exec -T --user 10001:10001 istota /spike/probes/procstatus.py daemon 2>/dev/null | grep -q '^daemon pid='; then
            return 0
        fi
        if [ "$(docker inspect -f '{{.State.Running}}' istota-spike-istota-1 2>/dev/null)" != "true" ]; then
            return 1
        fi
        sleep 2
    done
    return 1
}

section() { echo; echo "=== $* ==="; }

probe_all() {
    section "container start log"
    compose logs --no-color istota | sed 's/^istota-1  | //' | grep -E '^\[spike' || true
    section "daemon process (PID tree, found by cmdline)"
    compose exec -T --user 10001:10001 istota /spike/probes/procstatus.py daemon
    section "docker compose exec --user 10001:10001"
    compose exec -T --user 10001:10001 istota /spike/probes/procstatus.py self
    section "plain docker compose exec (no --user)"
    compose exec -T istota /spike/probes/procstatus.py self
    section "bwrap as 10001"
    compose exec -T --user 10001:10001 istota /spike/probes/bwrap.sh
    section "syscalls as 10001, outside bwrap"
    compose exec -T --user 10001:10001 istota /spike/probes/syscalls.py
    section "cgroup as 10001"
    compose exec -T --user 10001:10001 -e ISTOTA_TASK_CGROUP_ROOT=/sys/fs/cgroup istota /spike/probes/cgroup.py
    section "misc as 10001"
    compose exec -T --user 10001:10001 istota /spike/probes/misc.sh
    section "misc as plain exec (uid 0)"
    compose exec -T istota /spike/probes/misc.sh
}

probe_bwrap_only() {
    section "container start log"
    compose logs --no-color istota | sed 's/^istota-1  | //' | grep -E '^\[spike' || true
    section "bwrap as 10001"
    compose exec -T --user 10001:10001 istota /spike/probes/bwrap.sh
    section "syscalls as 10001, outside bwrap"
    compose exec -T --user 10001:10001 istota /spike/probes/syscalls.py
}

run_variant() {
    local name="$1" mode="$2"
    # A prefix assignment on a function call is not reliably in the
    # environment of the commands the function runs; export explicitly.
    export SPIKE_SECCOMP SPIKE_APPARMOR SPIKE_BOUNDING SPIKE_SKIP_CGROUP SPIKE_DAEMON
    {
        echo "##### variant ${name}"
        echo "overlays: ${OVERLAYS[*]:-none}"
        echo "env: SPIKE_SECCOMP=${SPIKE_SECCOMP:-draft} SPIKE_APPARMOR=${SPIKE_APPARMOR:-docker-default} SPIKE_BOUNDING=${SPIKE_BOUNDING:-drop} SPIKE_SKIP_CGROUP=${SPIKE_SKIP_CGROUP:-0} SPIKE_DAEMON=${SPIKE_DAEMON:-1}"
        compose down -v --remove-orphans >/dev/null 2>&1
        compose up -d 2>&1 | tail -1
        echo "applied: $(docker inspect -f 'SecurityOpt=[{{range .HostConfig.SecurityOpt}}{{printf "%.30s" .}}; {{end}}] CapAdd={{.HostConfig.CapAdd}} CapDrop={{.HostConfig.CapDrop}} ReadonlyRootfs={{.HostConfig.ReadonlyRootfs}} CgroupnsMode={{.HostConfig.CgroupnsMode}} AppArmorProfile={{.AppArmorProfile}}' istota-spike-istota-1)"
        if [ "$mode" = "expect-exit" ]; then
            sleep 8
            section "container state"
            docker inspect -f 'running={{.State.Running}} exit={{.State.ExitCode}}' istota-spike-istota-1
            section "container log"
            compose logs --no-color istota | sed 's/^istota-1  | //' | tail -20
        elif wait_for_daemon || [ "${SPIKE_DAEMON:-1}" = "0" ]; then
            sleep 2
            if [ "$mode" = "all" ]; then probe_all; else probe_bwrap_only; fi
        else
            section "daemon never came up"
            docker inspect -f 'running={{.State.Running}} exit={{.State.ExitCode}}' istota-spike-istota-1
            compose logs --no-color istota | tail -30
        fi
        compose down -v --remove-orphans >/dev/null 2>&1
    } 2>&1 | tee "out/${name}.log"
}

OVERLAYS=()
case "${1:-}" in
    setup)
        base="$2"
        docker build -q --build-arg BASE="$base" -f Dockerfile.spike -t istota-spike/istota-patched:latest .
        python3 derive_profile.py docker-default-29.9.0.json profiles/seccomp-istota.draft.json
        python3 derive_profile.py docker-default-29.9.0.json profiles/seccomp-keep-sysadmin-rule.json --keep-sysadmin-rule
        for drop in clone clone3 mount pivot_root setns umount2 unshare; do
            keep=()
            for s in clone clone3 mount pivot_root setns umount2 unshare; do
                [ "$s" != "$drop" ] && keep+=("$s")
            done
            python3 derive_profile.py docker-default-29.9.0.json "profiles/without-${drop}.json" "${keep[@]}"
        done
        cp profiles/seccomp-istota.draft.json seccomp-istota.draft.json
        apparmor_parser -r -W apparmor-istota.draft
        echo "loaded AppArmor profile: $(grep -c istota-spike-default /sys/kernel/security/apparmor/profiles) istota-spike-default"
        # Deliberately 0644 root on the VM side: does compose apply uid/gid/mode?
        printf 'spike-secret-value' > secrets/probe_secret
        chown 0:0 secrets/probe_secret
        chmod 0644 secrets/probe_secret
        echo "host side: $(stat -c 'uid=%u gid=%g mode=%a' secrets/probe_secret)"
        ;;
    main)
        # The spec's exact cap_add set.
        run_variant main-spec-caps all ;;
    main-setpcap)
        OVERLAYS=(overlay-setpcap.yml); run_variant main-setpcap all ;;
    mountprobe)
        # Hold a container whose root phase skipped the cgroup step, then try
        # the remount routes as a plain (uid 0, cap_add) exec. Extra env picks
        # the AppArmor and seccomp variant.
        OVERLAYS=(overlay-setpcap.yml)
        export SPIKE_SKIP_CGROUP=1 SPIKE_DAEMON=0
        {
            echo "##### mountprobe seccomp=${SPIKE_SECCOMP:-draft} apparmor=${SPIKE_APPARMOR:-docker-default}"
            compose down -v --remove-orphans >/dev/null 2>&1
            compose up -d 2>&1 | tail -1
            sleep 6
            compose exec -T istota /spike/probes/mountprobe.py
            compose down -v --remove-orphans >/dev/null 2>&1
        } 2>&1 | tee "out/mountprobe-${SPIKE_SECCOMP:+custom}-${SPIKE_APPARMOR:-docker-default}.log"
        ;;
    main-aa-unconfined)
        SPIKE_APPARMOR=unconfined run_variant main-aa-unconfined all ;;
    main-setpcap-aa-unconfined)
        OVERLAYS=(overlay-setpcap.yml); SPIKE_APPARMOR=unconfined run_variant main-setpcap-aa-unconfined all ;;
    main-setpcap-aa-istota)
        OVERLAYS=(overlay-setpcap.yml); SPIKE_APPARMOR=istota-spike-default run_variant main-setpcap-aa-istota all ;;
    apparmor-default-skip-cgroup)
        OVERLAYS=(overlay-setpcap.yml); SPIKE_SKIP_CGROUP=1 run_variant apparmor-default-skip-cgroup all ;;
    keep-bounding)
        SPIKE_BOUNDING=keep run_variant keep-bounding all ;;
    docker-default-with-sysadmin)
        OVERLAYS=(overlay-setpcap.yml); SPIKE_SECCOMP=builtin run_variant docker-default-with-sysadmin bwrap ;;
    docker-default-without-sysadmin)
        OVERLAYS=(overlay-no-sysadmin.yml); SPIKE_SECCOMP=builtin SPIKE_SKIP_CGROUP=1 run_variant docker-default-without-sysadmin bwrap ;;
    keep-sysadmin-rule)
        OVERLAYS=(overlay-setpcap.yml); SPIKE_SECCOMP="$HERE/profiles/seccomp-keep-sysadmin-rule.json" run_variant keep-sysadmin-rule bwrap ;;
    unconfined)
        OVERLAYS=(overlay-setpcap.yml); SPIKE_SECCOMP=unconfined run_variant unconfined bwrap ;;
    no-sysadmin-remount)
        OVERLAYS=(overlay-no-sysadmin.yml); run_variant no-sysadmin-remount expect-exit ;;
    no-sysadmin-skip-cgroup)
        OVERLAYS=(overlay-no-sysadmin.yml); SPIKE_SKIP_CGROUP=1 run_variant no-sysadmin-skip-cgroup all ;;
    cgroup-bind)
        OVERLAYS=(overlay-setpcap.yml overlay-cgroup-bind.yml); run_variant cgroup-bind expect-exit ;;
    apparmor-unconfined)
        OVERLAYS=(overlay-setpcap.yml); SPIKE_APPARMOR=unconfined run_variant apparmor-unconfined bwrap ;;
    without-*)
        # The root phase's remount is skipped so only bwrap's needs are measured.
        OVERLAYS=(overlay-setpcap.yml); SPIKE_SECCOMP="$HERE/profiles/$1.json" SPIKE_SKIP_CGROUP=1 run_variant "$1" bwrap ;;
    *)
        echo "unknown step: ${1:-}" >&2; exit 2 ;;
esac
