#!/bin/bash
# Stage 1 spike: the root phase the spec describes ("Non-root daemon",
# "Per-task cgroups in the container"), hand-written ahead of Stage 2.
#
#   1. Refuse unless the cgroup2 mount at /sys/fs/cgroup is rooted at `/`.
#   2. Remount it read-write (fallback: unmount and mount a fresh cgroup2).
#   3. Move this process into supervisor/, enable controllers on the root,
#      chown the root and its delegation files to 10001 (what Delegate= does).
#   4. Fix ownership of /data.
#   5. setpriv to 10001 with an empty bounding set and exec the unprivileged
#      phase.
#
# SPIKE_SKIP_CGROUP=1 skips steps 1-3 (used by the variants that measure what
# happens without CAP_SYS_ADMIN). SPIKE_BOUNDING=keep drops everything except
# the bounding set, to measure whether --bounding-set=-all needs CAP_SETPCAP.
set -euo pipefail

log() { echo "[spike-root] $*"; }
CG=/sys/fs/cgroup
UID_D=10001

cgroup_mount_fields() {
    # mountinfo: id parent maj:min root mountpoint opts [optional...] - fstype source superopts
    awk -v mp="$CG" '$5 == mp {
        for (i = 6; i <= NF; i++) if ($i == "-") { print $4, $(i + 1), $6; exit }
    }' /proc/self/mountinfo
}

log "uid=$(id -u) caps: $(grep -E '^Cap(Prm|Eff|Bnd)' /proc/self/status | tr '\n' ' ')"

if [ "${SPIKE_SKIP_CGROUP:-0}" != "1" ]; then
    read -r cg_root cg_type cg_opts < <(cgroup_mount_fields)
    log "cgroup mount before: root=${cg_root} type=${cg_type} opts=${cg_opts}"
    if [ "$cg_type" != "cgroup2" ] || [ "$cg_root" != "/" ]; then
        log "REFUSE: /sys/fs/cgroup is ${cg_type} rooted at ${cg_root}, not the container's own cgroup2 at /"
        exit 70
    fi

    # util-linux 2.41 mounts through the new mount API (fspick, fsconfig,
    # mount_setattr), which the draft profile leaves denied; forcing classic
    # mount(2) keeps the profile's addition to `mount` and `umount2`.
    export LIBMOUNT_FORCE_MOUNT2=always
    if [[ ",${cg_opts}," == *",rw,"* ]]; then
        # Podman hands some containers a writable cgroup2 mount; then no
        # remount, and so no CAP_SYS_ADMIN, is needed.
        log "cgroup mount already rw: no remount"
    elif mount -o remount,rw "$CG" 2>/tmp/remount.err; then
        log "remount,rw: ok"
    else
        log "remount,rw failed: $(cat /tmp/remount.err); trying umount + fresh cgroup2"
        umount "$CG"
        mount -t cgroup2 -o rw,nosuid,nodev,noexec,relatime,nsdelegate cgroup2 "$CG"
        log "fresh cgroup2 mount: ok"
    fi
    read -r cg_root cg_type cg_opts < <(cgroup_mount_fields)
    log "cgroup mount after: root=${cg_root} type=${cg_type} opts=${cg_opts}"
    if [ "$cg_root" != "/" ]; then
        log "REFUSE: remounted cgroup2 is rooted at ${cg_root}"
        exit 70
    fi

    mkdir -p "$CG/supervisor"
    echo $$ > "$CG/supervisor/cgroup.procs"
    log "controllers available: $(cat "$CG/cgroup.controllers")"
    echo "+memory +pids +cpu" > "$CG/cgroup.subtree_control"
    log "root subtree_control: $(cat "$CG/cgroup.subtree_control")"
    chown "$UID_D:$UID_D" "$CG" "$CG/cgroup.procs" "$CG/cgroup.subtree_control" "$CG/cgroup.threads"
    chown -R "$UID_D:$UID_D" "$CG/supervisor"
    export ISTOTA_TASK_CGROUP_ROOT="$CG"
fi

mkdir -p /data/db /data/config /data/home /data/workspace /data/repos
chown -R "$UID_D:$UID_D" /data

bounding="--bounding-set=-all"
if [ "${SPIKE_BOUNDING:-drop}" = "keep" ]; then
    bounding=""
fi
log "dropping: setpriv --reuid=$UID_D --regid=$UID_D --init-groups --inh-caps=-all ${bounding}"
# shellcheck disable=SC2086
exec setpriv --reuid="$UID_D" --regid="$UID_D" --init-groups --inh-caps=-all $bounding \
    -- /unprivileged.spike.sh "$@"
