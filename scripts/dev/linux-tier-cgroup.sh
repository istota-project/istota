#!/bin/sh
# Build a delegated cgroup v2 subtree inside the Linux-tier container, so the
# `linux`-marked cgroup tests have something real to run against.
#
# Sourced (not executed) by scripts/test-linux.sh before pytest starts, because
# it exports ISTOTA_TEST_CGROUP_ROOT into the pytest process.
#
# Why this is needed at all: `task_cgroup.resolve_root()` reads
# /proc/self/cgroup and truncates at the `.service` / `.scope` component. Under
# Docker's default private cgroup namespace that file reads `0::/` — no unit
# component, so the function answers None however writable the tree is. That is
# correct behaviour on a host the daemon does not own, and it means the tests
# cannot find their own root here. The driver builds one and names it instead.
#
# What it builds mirrors what systemd's `Delegate=` + `DelegateSubgroup=`
# produce on the deployment, and for the same reason: cgroup v2 forbids a
# non-root cgroup from both holding processes and enabling controllers for its
# children, so the container's own cgroup has to be emptied into a leaf before
# `cgroup.subtree_control` will take anything. Task cgroups are then siblings of
# that leaf, exactly as they are under the real unit.
#
# Best-effort by design. A Docker without SYS_ADMIN or with a read-only cgroup2
# mount cannot do this, and that is a property of the machine rather than a
# defect in the tree — so this leaves ISTOTA_TEST_CGROUP_ROOT unset and the
# tests skip themselves. It is set only once the subtree demonstrably works,
# because the tests treat it as a promise and fail rather than skip when it is
# present.

_cgroup_root=/sys/fs/cgroup

setup_linux_tier_cgroup() {
    if [ ! -f "$_cgroup_root/cgroup.controllers" ]; then
        echo "linux tier: no cgroup2 at $_cgroup_root; cgroup tests will skip" >&2
        return 1
    fi

    # Docker mounts it read-only. SYS_ADMIN is what lets us change that.
    if ! mount -o remount,rw "$_cgroup_root" 2>/dev/null; then
        echo "linux tier: cannot remount $_cgroup_root rw (needs --cap-add=SYS_ADMIN);" \
             "cgroup tests will skip" >&2
        return 1
    fi

    # Empty the root into a leaf, the DelegateSubgroup= shape. Every pid moves,
    # including this shell and the pytest process it goes on to exec — which is
    # the point: the task cgroups the tests create are siblings of this leaf,
    # so nothing under test inherits membership from its parent by accident.
    mkdir -p "$_cgroup_root/supervisor" || return 1
    for _pid in $(cat "$_cgroup_root/cgroup.procs" 2>/dev/null); do
        echo "$_pid" > "$_cgroup_root/supervisor/cgroup.procs" 2>/dev/null || true
    done

    # One controller per write. A combined "+memory +pids +cpu" is
    # all-or-nothing, so a kernel missing `cpu` here would cost us `memory`,
    # which is the one the tests actually need. Same reasoning as
    # `task_cgroup.enable_controllers`.
    for _c in memory pids cpu; do
        echo "+$_c" > "$_cgroup_root/cgroup.subtree_control" 2>/dev/null || true
    done

    # Prove it rather than assume it: the kernel makes the interface files, so
    # a `memory.max` that writes is the evidence the controller is delegated
    # here. This is the same check `task_cgroup.probe` makes, done before we
    # promise the tests anything.
    if ! mkdir -p "$_cgroup_root/task-selftest" 2>/dev/null; then
        echo "linux tier: cannot create cgroups under $_cgroup_root; tests will skip" >&2
        return 1
    fi
    if ! echo max > "$_cgroup_root/task-selftest/memory.max" 2>/dev/null; then
        rmdir "$_cgroup_root/task-selftest" 2>/dev/null
        echo "linux tier: memory controller not delegated to $_cgroup_root;" \
             "cgroup tests will skip" >&2
        return 1
    fi
    rmdir "$_cgroup_root/task-selftest" 2>/dev/null

    ISTOTA_TEST_CGROUP_ROOT="$_cgroup_root"
    export ISTOTA_TEST_CGROUP_ROOT
    echo "linux tier: delegated cgroup subtree at $_cgroup_root" >&2
    return 0
}

setup_linux_tier_cgroup || true
