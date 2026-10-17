#!/bin/bash
# Generalize a provisioned VM into the Mac app's appliance disk.
#
# Run as root in the build VM the Mac app's release builder (istota-mac's
# scripts/build-appliance.sh) made from the release's own template and
# provision.sh, after its images are saved. The builder reads this script from
# the release tag, so it ships with the release:
#
#   limactl shell --workdir / <vm> sudo bash -s < host/appliance-generalize.sh
#
# What stays is the shape provision.sh made (Docker Engine, the host packages,
# the units, the istota user, the checkout at /srv/app/istota/src). What goes is
# everything that names this machine, its builder or a user: Docker's state,
# the stack's own files, Lima's mounts, ssh host keys, cloud-init's record of
# the instance, the machine id, logs, and the build user.
#
# The build user cannot be removed from its own session, so the last steps are
# a script started detached (`systemd-run --no-block`) that removes it, trims
# the free blocks and powers the VM off; the caller waits for Lima to see it
# stopped. /run is mounted noexec, so that script lives under /root and runs
# as bash's argument.
#
# ISTOTA_GENERALIZE_ROOT prefixes every path, for tests/test_appliance_generalize.py,
# which also puts stubs for the commands on PATH.
set -euo pipefail

ROOT="${ISTOTA_GENERALIZE_ROOT:-}"
STACK="${ROOT}/srv/app/istota"
FINISH="${ROOT}/root/istota-generalize-finish.sh"

if [ -z "$ROOT" ] && [ "$(id -u)" -ne 0 ]; then
    echo "appliance-generalize: run as root" >&2
    exit 77
fi
[ -d "${STACK}/src/.git" ] || { echo "appliance-generalize: ${STACK}/src is not a checkout; this is not a provisioned VM" >&2; exit 1; }

log() { echo "[generalize] $*"; }

# Docker: no images (they arrive from the release's archive) and no state tied
# to this host.
log "docker: pruning and stopping"
docker system prune -af --volumes >/dev/null
systemctl stop docker.service docker.socket containerd.service
rm -rf "${ROOT}/var/lib/docker/network" "${ROOT}/var/lib/docker/swarm" \
    "${ROOT}/var/lib/docker/engine-id" "${ROOT}/var/lib/docker/buildkit"
if [ -d "${ROOT}/var/lib/docker/tmp" ]; then
    find "${ROOT}/var/lib/docker/tmp" -mindepth 1 -delete
fi

# The stack directory keeps the checkout alone: host.env, the key, secrets,
# config, data and every marker are this build's, and provision.sh makes the
# layout again on the first boot.
log "stack: keeping ${STACK#"$ROOT"}/src only"
find "$STACK" -mindepth 1 -maxdepth 1 ! -name src -exec rm -rf {} +
rm -f "${ROOT}/etc/systemd/system/istota-stack.service.d/host-folder.conf"

# Lima's mounts are cloud-init's lines in fstab; the new instance writes its own.
if [ -f "${ROOT}/etc/fstab" ]; then
    fstab="$(grep -v 'comment=cloudconfig' "${ROOT}/etc/fstab" || true)"
    printf '%s\n' "$fstab" > "${ROOT}/etc/fstab"
fi

# apt: the build's snapshot pin, downloaded packages and package lists go. The
# lists name the snapshot's URLs, which a user's VM does not read, and the
# offline rule in provision.sh needs none. The timers' stamps go too, so the
# daily apt jobs do not catch up on a missed run at the first boot.
log "apt: cleaning"
apt-get clean
rm -f "${ROOT}/etc/apt/apt.conf.d/50istota-snapshot"
if [ -d "${ROOT}/var/lib/apt/lists" ]; then
    find "${ROOT}/var/lib/apt/lists" -mindepth 1 -maxdepth 1 -type f ! -name lock -delete
fi
rm -f "${ROOT}"/var/lib/systemd/timers/stamp-apt-daily.timer \
    "${ROOT}"/var/lib/systemd/timers/stamp-apt-daily-upgrade.timer
rm -rf "${ROOT}/var/lib/apt/periodic"

# Identity: ssh host keys, cloud-init's record of this instance, the machine id
# (cloud-init clean empties it), the random seed.
log "identity: ssh host keys, cloud-init, machine id"
rm -f "${ROOT}"/etc/ssh/ssh_host_*
cloud-init clean --logs --seed --machine-id
rm -f "${ROOT}/var/lib/systemd/random-seed" "${ROOT}/var/lib/systemd/credential.secret"
rm -rf "${ROOT}/root/.cache" "${ROOT}/root/.bash_history" "${ROOT}/root/.lesshst"
find "${ROOT}/var/tmp" "${ROOT}/tmp" -mindepth 1 -maxdepth 1 -exec rm -rf {} + 2>/dev/null || true

# Logs.
log "logs: emptied"
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true
find "${ROOT}/var/log" -type f \( -name '*.gz' -o -name '*.[0-9]' -o -name '*.old' \) -delete
find "${ROOT}/var/log" -type f -exec truncate -s 0 {} +

# The build user and Lima's own: an account with a home under /home and a
# login uid. A new Mac's Lima makes its own user from its cidata.
users="$(awk -F: '$3 >= 1000 && $3 < 60000 && $6 ~ /^\/home\// { print $1 }' "${ROOT}/etc/passwd" | tr '\n' ' ')"
log "removing users: ${users:-none}"

cat > "$FINISH" <<EOF
#!/bin/bash
# Written by host/appliance-generalize.sh; removes itself, then powers off.
set -x
ROOT="${ROOT}"
sleep 3
for user in ${users}; do
    pkill -KILL -u "\$user" || true
    sleep 1
    userdel -r -f "\$user" || true
    rm -f "\${ROOT}/etc/sudoers.d/\$user"
done
rm -f "\${ROOT}/etc/sudoers.d/90-lima-users"
rm -rf "\${ROOT}"/home/*.guest "\${ROOT}"/home/*.linux
journalctl --rotate >/dev/null 2>&1 || true
journalctl --vacuum-time=1s >/dev/null 2>&1 || true
rm -f "\$0"
fstrim -av
sync
systemctl poweroff
EOF
chmod 0700 "$FINISH"
systemd-run --no-block --unit=istota-generalize-finish --collect /bin/bash "${FINISH#"$ROOT"}"
log "finish scheduled; the VM powers off when it is done"
