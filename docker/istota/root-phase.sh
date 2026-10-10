#!/bin/bash
# The istota container's root phase: the only code in the container that runs
# as uid 0 before the drop. It does three things, then execs `istota-drop "$@"`,
# which becomes uid 10001 with every capability set empty:
#
#   1. Delegates the container's own cgroup to the daemon's uid, the container
#      equivalent of `Delegate=memory pids cpu` plus `DelegateSubgroup=supervisor`.
#      Refuses first, before writing anything, unless /sys/fs/cgroup is a cgroup2
#      mount rooted at `/` and this process sits at that namespace's root
#      (`0::/`): a bind of the host's tree (root `/../..`) and `cgroup: host`
#      (root `/`, process at the VM's path for the container) are both writable
#      once remounted, and both are the VM's whole hierarchy.
#   2. Fixes ownership of /data, the local workspace included, once, for a
#      volume an older image wrote as root (recorded by /data/.ownership-10001).
#      A full-integration workspace is the VM's rclone mount, whose owner the
#      mount unit sets; nothing here touches it.
#   3. Checks the sandbox grant: bubblewrap must be able to build the sandbox's
#      namespace as uid 10001, or the container exits naming the compose lines.
#      The drop itself is checked first, so a credential file uid 10001 cannot
#      read is reported as that rather than as a missing grant.
#
# Its capabilities are compose's cap_add (CHOWN, FOWNER, SETUID, SETGID,
# SETPCAP, SYS_ADMIN); SYS_ADMIN is for the remount alone. Every decision is a
# function taking its inputs as arguments, and `main` runs only when executed,
# so tests/test_container_root_phase.py sources this and calls them.
set -euo pipefail

ISTOTA_UID=10001
ISTOTA_GID=10001
CGROUP_MOUNT=/sys/fs/cgroup
OWNERSHIP_MARKER=/data/.ownership-10001

log() { echo "[istota-root] $*"; }

# mountinfo: id parent maj:min root mountpoint opts [optional...] - fstype source superopts
cgroup_mount_fields() {
    local mountinfo="$1" mountpoint="$2"
    awk -v mp="$mountpoint" '$5 == mp {
        for (i = 6; i <= NF; i++) if ($i == "-") { line = $4 " " $(i + 1) " " $6 }
    } END { if (line != "") print line }' "$mountinfo"
}

# This process's cgroup v2 path, as its cgroup namespace names it.
own_cgroup() {
    awk -F: '$1 == "0" && $2 == "" { sub(/^0::/, ""); print; exit }' "$1"
}

require_own_cgroup() {
    local mountinfo="$1" mountpoint="$2" cgroup_file="${3:-/proc/self/cgroup}" fields root fstype own
    fields="$(cgroup_mount_fields "$mountinfo" "$mountpoint")"
    root="${fields%% *}"
    fstype="$(echo "$fields" | awk '{print $2}')"
    if [ -z "$fields" ] || [ "$fstype" != "cgroup2" ] || [ "$root" != "/" ]; then
        log "REFUSE: ${mountpoint} is ${fstype:-not mounted} rooted at ${root:-nothing}, not this container's own cgroup2 at /."
        log "REFUSE: give the istota service \`cgroup: private\` and no volume on ${mountpoint}; a bind of the host's tree hands the daemon the VM's whole cgroup hierarchy."
        return 70
    fi
    # The mount root is `/` in the host's namespace too; there this process is
    # at the VM's path for the container rather than at the namespace's root.
    own="$(own_cgroup "$cgroup_file")"
    if [ "$own" != "/" ]; then
        log "REFUSE: this process's cgroup is ${own:-unknown}, not / in a cgroup namespace of its own, so ${mountpoint} is the VM's whole tree."
        log "REFUSE: give the istota service \`cgroup: private\`; \`cgroup: host\` hands the daemon the VM's whole cgroup hierarchy."
        return 70
    fi
}

# Remount read-write and delegate the root to the daemon's uid. Returns non-zero
# on failure without exiting: a daemon without per-task limits still starts
# (the doctor check `security.task_cgroups` FAILs and says so), which is better
# than no daemon at all.
delegate_cgroup() {
    local mountpoint="$1" opts
    opts="$(cgroup_mount_fields /proc/self/mountinfo "$mountpoint" | awk '{print $3}')"
    if [[ ",${opts}," != *",rw,"* ]]; then
        # util-linux 2.41 remounts through the new mount API (fspick, fsconfig,
        # mount_setattr), which the seccomp profile leaves denied, and EPERM
        # from it does not make libmount fall back. Classic mount(2) is what the
        # profile allows.
        if ! LIBMOUNT_FORCE_MOUNT2=always mount -o remount,rw "$mountpoint" 2>/tmp/istota-remount.err; then
            log "remount of ${mountpoint} failed ($(cat /tmp/istota-remount.err)); trying a fresh cgroup2 mount"
            # A private cgroup namespace roots a new cgroup2 mount at this
            # container's cgroup. Mounting over the existing one is EBUSY.
            umount "$mountpoint" 2>/dev/null \
                && mount -t cgroup2 -o rw,nosuid,nodev,noexec,relatime,nsdelegate cgroup2 "$mountpoint" \
                || return 1
            require_own_cgroup /proc/self/mountinfo "$mountpoint" || exit $?
        fi
    fi
    mkdir -p "$mountpoint/supervisor" || return 1
    echo $$ > "$mountpoint/supervisor/cgroup.procs" || return 1
    echo "+memory +pids +cpu" > "$mountpoint/cgroup.subtree_control" || return 1
    chown "$ISTOTA_UID:$ISTOTA_GID" "$mountpoint" "$mountpoint/cgroup.procs" \
        "$mountpoint/cgroup.subtree_control" "$mountpoint/cgroup.threads" || return 1
    chown -R "$ISTOTA_UID:$ISTOTA_GID" "$mountpoint/supervisor" || return 1
    log "cgroup ${mountpoint} delegated to ${ISTOTA_UID} ($(cat "$mountpoint/cgroup.subtree_control"))"
}

# Whether a path is a read-only mount point (a config directory bound :ro).
is_ro_mount() {
    awk -v mp="$1" '$5 == mp { split($6, o, ","); for (i in o) if (o[i] == "ro") found = 1 }
        END { exit !found }' /proc/self/mountinfo
}

# chown -R, skipping read-only mounts below the path rather than failing on them.
chown_tree() {
    local path="$1" ro_mounts=() mp
    while read -r mp; do
        if [ "$mp" != "$path" ] && is_ro_mount "$mp"; then
            ro_mounts+=(-path "$mp" -prune -o)
        fi
    done < <(awk -v p="$path" 'index($5, p "/") == 1 { print $5 }' /proc/self/mountinfo)
    # Without CAP_DAC_READ_SEARCH this phase cannot list a 0700 directory 10001
    # already owns; what is in one was written by 10001 and needs nothing.
    find "$path" "${ro_mounts[@]}" \( ! -user "$ISTOTA_UID" -o ! -group "$ISTOTA_GID" \) \
        -exec chown -h "$ISTOTA_UID:$ISTOTA_GID" {} + 2>/tmp/istota-chown.err \
        || log "ownership walk of ${path} was incomplete: $(head -3 /tmp/istota-chown.err | tr '\n' ' ')"
}

# The mount points below /data that are writable: a tmpfs or a volume mounted
# there is a fresh root-owned directory on every container start.
writable_mounts_under() {
    awk -v p="$1" 'index($5, p "/") == 1 {
        split($6, o, ","); ro = 0; for (i in o) if (o[i] == "ro") ro = 1
        if (!ro) print $5
    }' /proc/self/mountinfo
}

# This phase holds CAP_CHOWN and CAP_FOWNER but not CAP_DAC_OVERRIDE, so it can
# hand a file to 10001 and cannot write into a directory 10001 owns. The marker
# is therefore written as 10001, after the walk, so a walk that dies part way
# is repeated on the next boot rather than recorded as done.
fix_ownership() {
    local mp
    while read -r mp; do
        [ "$(stat -c %u "$mp")" = "$ISTOTA_UID" ] || chown "$ISTOTA_UID:$ISTOTA_GID" "$mp"
    done < <(writable_mounts_under /data)
    if [ -e "$OWNERSHIP_MARKER" ]; then
        return 0
    fi
    log "one-time ownership fix: /data to ${ISTOTA_UID}"
    chown_tree /data
    if ! istota-drop sh -c "mkdir -p /data/home && : > $OWNERSHIP_MARKER" 2>/dev/null; then
        log "could not record ${OWNERSHIP_MARKER} (no writable state volume); the walk repeats next boot"
    fi
}

# The drop on its own, before anything else drops through it. `istota-drop`
# reads every credential file as 10001 and exits 78 on one it cannot read; the
# ownership marker and the bwrap probe below both drop the same way, and would
# report that as "no writable state volume" and a missing sandbox grant.
check_drop() {
    local err rc=0
    err="$(istota-drop true 2>&1)" || rc=$?
    if [ "$rc" -eq 0 ]; then
        return 0
    fi
    log "the drop to ${ISTOTA_UID} failed (exit ${rc}): ${err}"
    if [ "$rc" -eq 78 ]; then
        log "REFUSE: a credential file under /run/secrets is not readable by uid ${ISTOTA_UID}; on the VM, chown 10001:10001 and chmod 0400 it."
        return 78
    fi
    log "REFUSE: istota-drop itself failed; the istota service needs cap_add CHOWN, FOWNER, SETUID, SETGID, SETPCAP and SYS_ADMIN."
    return 71
}

grant_refusal() {
    log "REFUSE: bubblewrap cannot build the sandbox's namespace in this container, so every task would run unsandboxed."
    log "REFUSE: the istota service in docker-compose.yml needs:"
    log "    security_opt:"
    log "      - seccomp=./istota/seccomp-istota.json"
    log "      - apparmor=istota"
    log "      - systempaths=unconfined"
    log "      - no-new-privileges:true"
    log "REFUSE: and, on a host with AppArmor, docker/istota/apparmor-istota loaded (apparmor_parser -r -W)."
    return 78
}

probe_grant() {
    local err
    if err="$(istota-drop bwrap --unshare-user --ro-bind / / --unshare-pid \
            --proc /proc --dev /dev --tmpfs /tmp -- true 2>&1)"; then
        return 0
    fi
    log "bwrap probe as ${ISTOTA_UID}: ${err}"
    grant_refusal
}

main() {
    if [ "$(id -u)" != "0" ]; then
        log "REFUSE: the root phase must start as uid 0 (it delegates the cgroup and then drops to ${ISTOTA_UID}); remove \`user:\` from the istota service."
        exit 64
    fi
    require_own_cgroup /proc/self/mountinfo "$CGROUP_MOUNT" || exit $?
    if delegate_cgroup "$CGROUP_MOUNT"; then
        export ISTOTA_TASK_CGROUP_ROOT="$CGROUP_MOUNT"
    else
        log "WARNING: could not delegate ${CGROUP_MOUNT}; tasks will run without per-task limits (security.task_cgroups reports FAIL)"
    fi
    check_drop || exit $?
    fix_ownership
    probe_grant || exit $?
    exec istota-drop "$@"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
    main "$@"
fi
